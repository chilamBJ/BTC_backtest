#!/usr/bin/env python3
"""
Paper Bot V2 — Binance 时钟驱动的模拟盘引擎
============================================
以 Binance 5m K 线时间轴为主导，在每根 5m 结束前 1 秒（第 299 秒）做决策，
映射到当前活跃的 Polymarket 5m 市场执行虚拟下单。

纯血模型：仅 feat_1 (币安 1s CVD)、feat_2 (币安 1s Tick 强度)、feat_5 (币安 5m VWAP 偏离)。
"""

from __future__ import annotations

import asyncio
import csv
import json
import os
import pickle
import signal
import time
import warnings
from collections import deque
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

import aiohttp
import pandas as pd

# 项目路径
SCRIPT_DIR = Path(__file__).resolve().parent
TRADE_DIR = SCRIPT_DIR.parent.parent
PROJECT_ROOT = TRADE_DIR.parent

# 配置
BINANCE_AGG_WS = "wss://stream.binance.com:9443/ws/btcusdt@aggTrade"
BINANCE_KLINE_5M_WS = "wss://stream.binance.com:9443/ws/btcusdt@kline_5m"
BINANCE_KLINE_REST = "https://api.binance.com/api/v3/klines"
GAMMA_API_SLUG = "https://gamma-api.polymarket.com/events/slug"
CLOB_BOOK = "https://clob.polymarket.com/book"

# 风控
PROB_REVERSAL_THRESHOLD = 0.50  # prob < 0.5 视为反转（买 NO）
AMERICAS_START_HOUR = 13
AMERICAS_START_MIN = 30
AMERICAS_END_HOUR = 22
VOL_REGIME_CAP = 1.5  # 反转时 vol_regime > 1.5 则拦截
ASK_MAX_FOR_NO = 0.52  # 买 NO 时 Ask 需 <= 0.52

# 数据缓冲
_agg_buffer: dict[int, list] = {}  # ts_sec -> [buy_vol, sell_vol, trade_count]
_kline_5m_history: deque = deque(maxlen=288)
_current_bar_close: Optional[float] = None  # 当前未收盘 bar 的实时 close
_shutdown = asyncio.Event()


# ---------------------------------------------------------------------------
# 1. Binance 时钟：精确等待第 299 秒
# ---------------------------------------------------------------------------
def _next_299_utc() -> float:
    """返回下一个 5m 周期第 299 秒的 Unix 时间戳。"""
    now = time.time()
    bar_start = (int(now) // 300) * 300
    target = bar_start + 299
    if now >= target:
        target = bar_start + 300 + 299  # 下一根
    return float(target)


async def wait_until_299() -> None:
    """阻塞直到下一个 5m 第 299 秒。"""
    target = _next_299_utc()
    delay = target - time.time()
    if delay > 0:
        await asyncio.sleep(delay)


# ---------------------------------------------------------------------------
# 2. 数据采集：aggTrade + 5m K 线（自维护，不依赖 collector 进程）
# ---------------------------------------------------------------------------
async def _prewarm_5m() -> None:
    """启动时 REST 拉取过去 24h 5m K 线。"""
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(
                BINANCE_KLINE_REST,
                params={"symbol": "BTCUSDT", "interval": "5m", "limit": 288},
            ) as resp:
                if resp.status != 200:
                    return
                data = await resp.json()
        _kline_5m_history.clear()
        for bar in data:
            ts, o, h, l, c, v = bar[0], float(bar[1]), float(bar[2]), float(bar[3]), float(bar[4]), float(bar[5])
            _kline_5m_history.append({"timestamp": ts, "open": o, "high": h, "low": l, "close": c, "volume": v})
        print(f"  [PaperBot] 5m 预热完成，{len(_kline_5m_history)} 根")
    except Exception as e:
        print(f"  [PaperBot] 5m 预热失败: {e}")


async def _agg_producer() -> None:
    """aggTrade WebSocket，按秒聚合到 _agg_buffer。"""
    last_ts = -1
    buf: dict[int, list] = {}
    while not _shutdown.is_set():
        try:
            async with aiohttp.ClientSession() as session:
                async with session.ws_connect(BINANCE_AGG_WS) as ws:
                    async for raw in ws:
                        if _shutdown.is_set():
                            return
                        msg = json.loads(raw.data)
                        ts_sec = msg["T"] // 1000
                        q = float(msg["q"])
                        is_buy = not msg["m"]
                        if ts_sec not in buf:
                            buf[ts_sec] = [0.0, 0.0, 0]
                        buf[ts_sec][0] += q if is_buy else 0.0
                        buf[ts_sec][1] += 0.0 if is_buy else q
                        buf[ts_sec][2] += 1
                        if last_ts >= 0 and ts_sec > last_ts:
                            for s in range(last_ts, ts_sec):
                                row = buf.pop(s, [0.0, 0.0, 0])
                                _agg_buffer[s] = row
                        last_ts = ts_sec
        except asyncio.CancelledError:
            return
        except Exception as e:
            print(f"  [PaperBot] aggTrade 异常: {e}")
        if not _shutdown.is_set():
            await asyncio.sleep(5)


async def _kline_producer() -> None:
    """5m K 线 WebSocket，收盘时更新 history，未收盘时更新 current_bar_close。"""
    global _current_bar_close
    while not _shutdown.is_set():
        try:
            async with aiohttp.ClientSession() as session:
                async with session.ws_connect(BINANCE_KLINE_5M_WS) as ws:
                    async for raw in ws:
                        if _shutdown.is_set():
                            return
                        msg = json.loads(raw.data)
                        k = msg.get("k")
                        if not k:
                            continue
                        row = {
                            "timestamp": int(k["t"]),
                            "open": float(k["o"]),
                            "high": float(k["h"]),
                            "low": float(k["l"]),
                            "close": float(k["c"]),
                            "volume": float(k["v"]),
                        }
                        if k.get("x"):
                            _kline_5m_history.append(row)
                            _current_bar_close = None
                        else:
                            _current_bar_close = row["close"]
        except asyncio.CancelledError:
            return
        except Exception as e:
            print(f"  [PaperBot] K 线异常: {e}")
        if not _shutdown.is_set():
            await asyncio.sleep(5)


# ---------------------------------------------------------------------------
# 3. 事件过滤
# ---------------------------------------------------------------------------
def _event_ok() -> bool:
    """过去两根 5m K 线同向且累计涨跌幅 > 0.003。"""
    if len(_kline_5m_history) < 2:
        return False
    bar1 = _kline_5m_history[-2]
    bar2 = _kline_5m_history[-1]
    o1, c1 = bar1["open"], bar1["close"]
    o2, c2 = bar2["open"], bar2["close"]
    up = c1 > o1 and c2 > o2 and c2 > c1
    down = c1 < o1 and c2 < o2 and c2 < c1
    if not (up or down):
        return False
    if o1 <= 0:
        return False
    total_move = abs((c2 - o1) / o1)
    return total_move > 0.003


# ---------------------------------------------------------------------------
# 4. 特征计算（纯 Binance，无 PM）
# ---------------------------------------------------------------------------
def _compute_feat1_feat2(bar_start_ts: int) -> tuple[float, float]:
    """从 _agg_buffer 提取当前 bar 的 last_60s / first_240s，计算 feat_1, feat_2。"""
    last_60s_cvd, last_60s_vol, last_60s_ticks = 0.0, 0.0, 0
    first_240s_ticks = 0
    for s in range(bar_start_ts, bar_start_ts + 300):
        row = _agg_buffer.get(s, [0.0, 0.0, 0])
        buy, sell, tc = row[0], row[1], row[2]
        cvd = buy - sell
        vol = buy + sell
        if bar_start_ts + 240 <= s <= bar_start_ts + 299:
            last_60s_cvd += cvd
            last_60s_vol += vol
            last_60s_ticks += tc
        elif bar_start_ts <= s <= bar_start_ts + 239:
            first_240s_ticks += tc
    feat_1 = (last_60s_cvd / last_60s_vol).clip(-1, 1) if last_60s_vol > 0 else 0.0
    feat_2 = (last_60s_ticks / 60) / (first_240s_ticks / 240) if first_240s_ticks > 0 else 1.0
    return feat_1, feat_2


def _compute_feat5() -> float:
    """基于 5m history 计算 4H VWAP 偏离度 (bps)。"""
    if len(_kline_5m_history) < 2:
        return 0.0
    bars = list(_kline_5m_history)[-48:]
    typical = [(b["high"] + b["low"] + b["close"]) / 3.0 for b in bars]
    pv = sum(typical[i] * bars[i]["volume"] for i in range(len(bars)))
    vv = sum(b["volume"] for b in bars)
    if vv <= 0:
        return 0.0
    vwap_4h = pv / vv
    close = _current_bar_close if _current_bar_close is not None else bars[-1]["close"]
    return (close - vwap_4h) / vwap_4h * 10000


def _compute_vol_regime() -> float:
    """1h ATR / 24h ATR。"""
    if len(_kline_5m_history) < 288:
        return 1.0
    bars = list(_kline_5m_history)
    atr_1h = sum(bars[-1 - i]["high"] - bars[-1 - i]["low"] for i in range(min(12, len(bars)))) / 12
    atr_24h = sum(b["high"] - b["low"] for b in bars[-288:]) / 288
    if atr_24h <= 0:
        return 1.0
    return atr_1h / atr_24h


# ---------------------------------------------------------------------------
# 5. 风控拦截
# ---------------------------------------------------------------------------
def _is_americas_utc() -> bool:
    """当前 UTC 是否在美洲盘 13:30-22:00。"""
    now = datetime.now(timezone.utc)
    h, m = now.hour, now.minute
    if h < AMERICAS_START_HOUR:
        return False
    if h == AMERICAS_START_HOUR and m < AMERICAS_START_MIN:
        return False
    if h > AMERICAS_END_HOUR:
        return False
    if h == AMERICAS_END_HOUR and m > 0:
        return False
    return True


def _hard_filter_reversal(prob: float, vol_regime: float) -> Optional[str]:
    """prob < 0.5 时检查美洲盘 + vol_regime，命中则返回拦截原因。"""
    if prob >= PROB_REVERSAL_THRESHOLD:
        return None
    if _is_americas_utc():
        return "美洲盘禁止反转"
    if vol_regime > VOL_REGIME_CAP:
        return f"vol_regime={vol_regime:.2f} > {VOL_REGIME_CAP}"
    return None


# ---------------------------------------------------------------------------
# 6. Polymarket 执行映射
# ---------------------------------------------------------------------------
async def _fetch_pm_5m_market(next_bar_epoch: int) -> Optional[dict]:
    """查询下一根 5m 对应的 PM 市场 token_ids。"""
    slug = f"btc-updown-5m-{next_bar_epoch}"
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(f"{GAMMA_API_SLUG}/{slug}") as resp:
                if resp.status != 200:
                    return None
                data = await resp.json()
        events = data if isinstance(data, list) else [data]
        if not events:
            return None
        mkt = (events[0].get("markets") or [{}])[0]
        tokens = mkt.get("clobTokenIds") or []
        if isinstance(tokens, str):
            import json as _j
            tokens = _j.loads(tokens) if tokens else []
        if len(tokens) < 2:
            return None
        return {"yes_token": tokens[0], "no_token": tokens[1], "slug": slug}
    except Exception:
        return None


async def _fetch_ask_price(token_id: str) -> Optional[float]:
    """获取 token 的 best ask。"""
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(CLOB_BOOK, params={"token_id": token_id}) as resp:
                if resp.status != 200:
                    return None
                data = await resp.json()
        asks = data.get("asks", [])
        if not asks:
            return None
        first = asks[0]
        price = first.get("price") if isinstance(first, dict) else (first[0] if isinstance(first, (list, tuple)) else first)
        return float(price)
    except Exception:
        return None


# ---------------------------------------------------------------------------
# 7. 主循环
# ---------------------------------------------------------------------------
def _load_model(model_path: str):
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        with open(model_path, "rb") as f:
            return pickle.load(f)


def _predict(model, feat_1: float, feat_2: float, feat_5: float) -> float:
    """模型预测 P(YES)。仅使用 feat_1, feat_2, feat_5。"""
    cols = getattr(model, "feature_names_in_", None) or getattr(model, "feature_name_", None)
    if cols is not None:
        cols = list(cols)
        # 仅保留 feat_1/2/5，其余填 0
        row = {c: (feat_1 if c == "feat_1" else feat_2 if c == "feat_2" else feat_5 if c == "feat_5" else 0.0) for c in cols}
        X = pd.DataFrame([row], columns=cols)
    else:
        X = pd.DataFrame([[feat_1, feat_2, feat_5]], columns=["feat_1", "feat_2", "feat_5"])
    probs = model.predict_proba(X)
    return float(probs[0][1])


def _append_paper_trade(log_path: Path, row: dict) -> None:
    """追加到 paper_trades_log.csv。"""
    log_path.parent.mkdir(parents=True, exist_ok=True)
    exists = log_path.exists()
    with open(log_path, "a", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=row.keys())
        if not exists:
            w.writeheader()
        w.writerow(row)


async def _run_decision_cycle(model, log_path: Path) -> None:
    """单次决策周期：299 秒唤醒 → 过滤 → 特征 → 预测 → 风控 → 执行。"""
    now_ts = int(time.time())
    bar_start = (now_ts // 300) * 300
    next_bar_epoch = bar_start + 300

    if not _event_ok():
        print(f"  [{datetime.now(timezone.utc).strftime('%H:%M:%S')} UTC] 事件不满足，跳过")
        return

    feat_1, feat_2 = _compute_feat1_feat2(bar_start)
    feat_5 = _compute_feat5()
    #  prune 过期 agg 数据，避免内存膨胀
    for k in list(_agg_buffer.keys()):
        if k < bar_start - 60:
            del _agg_buffer[k]
    vol_regime = _compute_vol_regime()

    prob = _predict(model, feat_1, feat_2, feat_5)
    block_reason = _hard_filter_reversal(prob, vol_regime)
    if block_reason:
        print(f"  [{datetime.now(timezone.utc).strftime('%H:%M:%S')} UTC] 风控拦截: {block_reason} (prob={prob:.3f})")
        return

    # 判定方向
    if prob >= 0.60:
        direction = "YES"
        token_key = "yes_token"
    elif prob <= PROB_REVERSAL_THRESHOLD:
        direction = "NO"
        token_key = "no_token"
    else:
        print(f"  [{datetime.now(timezone.utc).strftime('%H:%M:%S')} UTC] prob={prob:.3f} 在区间内，不下单")
        return

    # 查询 PM 市场并执行
    pm = await _fetch_pm_5m_market(next_bar_epoch)
    if not pm:
        print(f"  [{datetime.now(timezone.utc).strftime('%H:%M:%S')} UTC] PM 市场未找到 (epoch={next_bar_epoch})")
        return

    token_id = pm[token_key]
    ask = await _fetch_ask_price(token_id)
    if ask is None:
        print(f"  [{datetime.now(timezone.utc).strftime('%H:%M:%S')} UTC] 无法获取 Ask")
        return

    if direction == "NO" and ask > ASK_MAX_FOR_NO:
        print(f"  [{datetime.now(timezone.utc).strftime('%H:%M:%S')} UTC] 滑点放弃: Ask={ask:.3f} > {ASK_MAX_FOR_NO}")
        _append_paper_trade(log_path, {
            "ts": datetime.now(timezone.utc).isoformat(),
            "bar_epoch": next_bar_epoch,
            "direction": direction,
            "prob": round(prob, 4),
            "ask": round(ask, 4),
            "action": "slippage_skip",
        })
        return

    # 虚拟成交
    print(f"  [{datetime.now(timezone.utc).strftime('%H:%M:%S')} UTC] 虚拟成交: {direction} @ {ask:.3f} (prob={prob:.3f})")
    _append_paper_trade(log_path, {
        "ts": datetime.now(timezone.utc).isoformat(),
        "bar_epoch": next_bar_epoch,
        "direction": direction,
        "prob": round(prob, 4),
        "ask": round(ask, 4),
        "action": "filled",
    })


async def main_loop(model_path: str, log_path: Path) -> None:
    """主循环：Binance 时钟驱动。"""
    model = _load_model(model_path)
    await _prewarm_5m()

    agg_task = asyncio.create_task(_agg_producer())
    kline_task = asyncio.create_task(_kline_producer())

    try:
        while not _shutdown.is_set():
            await wait_until_299()
            if _shutdown.is_set():
                break
            await _run_decision_cycle(model, log_path)
    finally:
        agg_task.cancel()
        kline_task.cancel()
        try:
            await agg_task
        except asyncio.CancelledError:
            pass
        try:
            await kline_task
        except asyncio.CancelledError:
            pass


def main() -> None:
    import argparse
    parser = argparse.ArgumentParser(description="Paper Bot V2 — Binance 时钟驱动")
    parser.add_argument("--model", default="models/model_seller.pkl", help="模型路径")
    parser.add_argument("--log", default="paper_trades_log.csv", help="虚拟成交日志")
    args = parser.parse_args()

    model_path = PROJECT_ROOT / args.model
    log_path = PROJECT_ROOT / args.log
    if not model_path.exists():
        print(f"模型不存在: {model_path}")
        return

    def do_shutdown():
        _shutdown.set()

    try:
        loop = asyncio.get_event_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            loop.add_signal_handler(sig, do_shutdown)
    except (NotImplementedError, OSError):
        signal.signal(signal.SIGINT, lambda *a: do_shutdown())
        signal.signal(signal.SIGTERM, lambda *a: do_shutdown())

    print("Paper Bot V2 启动，按 Binance 5m 第 299 秒驱动...")
    asyncio.run(main_loop(str(model_path), log_path))
    print("Bye.")


if __name__ == "__main__":
    main()
