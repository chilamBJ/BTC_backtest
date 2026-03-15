#!/usr/bin/env python3
"""
Paper Bot V2 — Binance 时钟驱动的模拟盘引擎
============================================
以 Binance 5m K 线时间轴为主导，在每根 5m 结束前 2 秒（第 298 秒）做决策，
映射到当前活跃的 Polymarket 5m 市场执行虚拟下单。

纯血模型：仅 feat_1 (币安 1s CVD)、feat_2 (币安 1s Tick 强度)、feat_5 (币安 5m VWAP 偏离)。
"""

from __future__ import annotations

import asyncio
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

# 数据库（与其他策略一致）
import sys
sys.path.insert(0, str(TRADE_DIR))
from utils.db import get_conn, execute as db_execute, init_paper_bot_tables

# 配置
BINANCE_AGG_WS = "wss://stream.binance.com:9443/ws/btcusdt@aggTrade"
BINANCE_KLINE_5M_WS = "wss://stream.binance.com:9443/ws/btcusdt@kline_5m"
BINANCE_KLINE_REST = "https://api.binance.com/api/v3/klines"
GAMMA_API_SLUG = "https://gamma-api.polymarket.com/events/slug"
GAMMA_API_MARKETS = "https://gamma-api.polymarket.com/markets"
CLOB_BOOK = "https://clob.polymarket.com/book"

# 风控
ASK_MAX = 0.52  # 任何方向的 Ask 都需 <= 0.52
MIN_ASK_SIZE = 10  # 盘口厚度：Ask 档位 size 需 >= 10
AMERICAS_START_HOUR = 13
AMERICAS_START_MIN = 30
AMERICAS_END_HOUR = 22
VOL_REGIME_CAP = 1.5  # 反转时 vol_regime > 1.5 则拦截

# 数据缓冲
_agg_buffer: dict[int, list] = {}  # ts_sec -> [buy_vol, sell_vol, trade_count]
_kline_5m_history: deque = deque(maxlen=288)
_current_bar_close: Optional[float] = None  # 当前未收盘 bar 的实时 close
_shutdown = asyncio.Event()

# 数据库会话（与 model_seller 一致）
_db = None
_session_id: Optional[int] = None

# 代理（经 run_via_tunnel.sh 时 SOCKS5 必须用 aiohttp-socks）
_http_session: Optional[aiohttp.ClientSession] = None


def _init_http_session() -> None:
    """根据 HTTP_PROXY/HTTPS_PROXY/ALL_PROXY 创建单一共享 aiohttp 会话。"""
    global _http_session
    proxy_url = (
        os.environ.get("HTTPS_PROXY") or os.environ.get("HTTP_PROXY") or os.environ.get("ALL_PROXY") or ""
    ).strip()
    if proxy_url and "socks" in proxy_url.lower():
        try:
            from aiohttp_socks import ProxyConnector
            connector = ProxyConnector.from_url(proxy_url)
            _http_session = aiohttp.ClientSession(connector=connector)
            print(f"  ✅ 请求经代理: {proxy_url.split('@')[-1] if '@' in proxy_url else proxy_url}")
        except ImportError:
            _http_session = aiohttp.ClientSession(trust_env=True)
            print("  ⚠️ 未安装 aiohttp-socks，SOCKS 代理未生效，请: pip install aiohttp-socks")
    else:
        _http_session = aiohttp.ClientSession(trust_env=bool(proxy_url))


# ---------------------------------------------------------------------------
# 1. Binance 时钟：精确等待第 298 秒（留 2s 执行安全垫）
# ---------------------------------------------------------------------------
def _next_trigger_utc() -> float:
    """返回下一个 5m 周期第 298 秒的 Unix 时间戳。"""
    now = time.time()
    bar_start = (int(now) // 300) * 300
    target = bar_start + 298
    if now >= target:
        target = bar_start + 300 + 298  # 下一根
    return float(target)


async def wait_until_trigger() -> None:
    """阻塞直到下一个 5m 第 298 秒。"""
    target = _next_trigger_utc()
    delay = target - time.time()
    if delay > 0:
        ts_str = datetime.fromtimestamp(target, tz=timezone.utc).strftime("%H:%M:%S")
        print(f"  [PaperBot] 等待下一决策点 ({ts_str} UTC，约 {int(delay)}s 后)...")
        await asyncio.sleep(delay)


# ---------------------------------------------------------------------------
# 2. 数据采集：aggTrade + 5m K 线（自维护，不依赖 collector 进程）
# ---------------------------------------------------------------------------
async def _prewarm_5m() -> None:
    """启动时 REST 拉取过去 24h 5m K 线。"""
    try:
        async with _http_session.get(
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
            async with _http_session.ws_connect(BINANCE_AGG_WS) as ws:
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
            _log("agg_error", "", detail={"msg": str(e)}, level="error")
        if not _shutdown.is_set():
            await asyncio.sleep(5)


async def _kline_producer() -> None:
    """5m K 线 WebSocket，收盘时更新 history，未收盘时更新 current_bar_close。"""
    global _current_bar_close
    while not _shutdown.is_set():
        try:
            async with _http_session.ws_connect(BINANCE_KLINE_5M_WS) as ws:
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
            _log("kline_error", "", detail={"msg": str(e)}, level="error")
        if not _shutdown.is_set():
            await asyncio.sleep(5)


# ---------------------------------------------------------------------------
# 3. 事件过滤（第 298 秒时当前 bar 未收盘，需动态合成 bar2）
# ---------------------------------------------------------------------------
def _event_ok() -> tuple[bool, Optional[str]]:
    """
    过去两根 5m K 线同向且累计涨跌幅 > 0.003。
    第 298 秒时：bar1 = history[-1]（上一根已收盘），bar2 = 当前 bar（open=bar1.close, close=_current_bar_close）。
    返回 (is_event_triggered, trend_direction)，trend_direction 为 'UP' 或 'DOWN'。
    """
    if len(_kline_5m_history) < 1:
        return False, None
    if _current_bar_close is None:
        return False, None
    bar1 = _kline_5m_history[-1]
    o1, c1 = bar1["open"], bar1["close"]
    o2 = c1  # 当前 bar 的 open = 上一根 bar 的 close
    c2 = _current_bar_close
    up = c1 > o1 and c2 > o2 and c2 > c1
    down = c1 < o1 and c2 < o2 and c2 < c1
    if not (up or down):
        return False, None
    if o1 <= 0:
        return False, None
    total_move = abs((c2 - o1) / o1)
    if total_move <= 0.003:
        return False, None
    trend = "UP" if up else "DOWN"
    return True, trend


# ---------------------------------------------------------------------------
# 4. 特征计算（纯 Binance，无 PM）
# ---------------------------------------------------------------------------
def _compute_feat1_feat2(bar_start_ts: int) -> tuple[float, float]:
    """
    从 _agg_buffer 提取当前 bar 的 last_60s / first_240s，计算 feat_1, feat_2。
    第 298 秒唤醒时，last_60s = 唤醒前 60 秒（bar_start+238 到 bar_start+297），
    first_240s = 前半段（bar_start 到 bar_start+239）。
    """
    last_60s_cvd, last_60s_vol, last_60s_ticks = 0.0, 0.0, 0
    first_240s_ticks = 0
    for s in range(bar_start_ts, bar_start_ts + 300):
        row = _agg_buffer.get(s, [0.0, 0.0, 0])
        buy, sell, tc = row[0], row[1], row[2]
        cvd = buy - sell
        vol = buy + sell
        if bar_start_ts + 238 <= s <= bar_start_ts + 297:
            last_60s_cvd += cvd
            last_60s_vol += vol
            last_60s_ticks += tc
        elif bar_start_ts <= s <= bar_start_ts + 239:
            first_240s_ticks += tc
    feat_1 = max(-1, min(1, last_60s_cvd / last_60s_vol)) if last_60s_vol > 0 else 0.0
    feat_2 = (last_60s_ticks / 60) / (first_240s_ticks / 240) if first_240s_ticks > 0 else 1.0
    return feat_1, feat_2


def _compute_feat5() -> float:
    """基于 5m history 计算 4H VWAP 偏离度 (bps)。当前 bar 的 close 用 _current_bar_close 动态合成。"""
    if len(_kline_5m_history) < 1:
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


def _hard_filter_reversal(is_reversal: bool, vol_regime: float) -> Optional[str]:
    """反转交易时检查美洲盘 + vol_regime，命中则返回拦截原因。"""
    if not is_reversal:
        return None
    if _is_americas_utc():
        return "美洲盘禁止反转"
    if vol_regime > VOL_REGIME_CAP:
        return f"vol_regime={vol_regime:.2f} > {VOL_REGIME_CAP}"
    return None


# ---------------------------------------------------------------------------
# 6. Polymarket 执行映射（动态路由，不依赖硬编码 slug）
# ---------------------------------------------------------------------------
def _gamma_parse_tokens(raw: Any) -> list:
    """解析 clobTokenIds（可能为 JSON 字符串或数组）。"""
    if raw is None:
        return []
    if isinstance(raw, list):
        return [str(x) for x in raw]
    if isinstance(raw, str):
        try:
            out = json.loads(raw)
            return [str(x) for x in out] if isinstance(out, list) else []
        except Exception:
            return []
    return []


async def _fetch_pm_5m_market(next_bar_epoch: int) -> Optional[dict]:
    """
    动态查询当前活跃的 BTC 5m 市场。
    调用 GET markets?closed=false&active=true，过滤 slug 包含 btc、5m 且 epoch 时间戳匹配的市场。
    【终极硬校验】确保 next_bar_epoch 一定在 slug 里，避免抓到 10:05 结算的过期市场而漏掉 10:10 结算的目标市场。
    """
    try:
        async with _http_session.get(
            GAMMA_API_MARKETS,
            params={"closed": "false", "active": "true"},
        ) as resp:
            if resp.status != 200:
                return None
            data = await resp.json()
        markets = data if isinstance(data, list) else (data.get("data") or data.get("markets") or [])
        epoch_str = str(next_bar_epoch)
        for m in markets:
            slug = (m.get("slug") or m.get("question") or "").lower()
            if "btc" not in slug:
                continue
            if not any(p in slug for p in ("5m", "5 m", "5-m", "5min")):
                continue
            if "15m" in slug or "15 m" in slug:
                continue
            # 🔥 极其关键的护城河：确保 epoch 时间戳一定在 slug 里！
            if epoch_str not in slug:
                continue
            tokens = _gamma_parse_tokens(m.get("clobTokenIds") or m.get("clob_token_ids"))
            if len(tokens) >= 2:
                return {"yes_token": tokens[0], "no_token": tokens[1], "slug": m.get("slug", slug)}
        return None
    except Exception:
        return None


async def _fetch_best_ask(token_id: str) -> Optional[tuple[float, float]]:
    """获取 token 的 best ask，返回 (price, size)。用于盘口厚度校验。"""
    try:
        async with _http_session.get(CLOB_BOOK, params={"token_id": token_id}) as resp:
            if resp.status != 200:
                return None
            data = await resp.json()
        asks = data.get("asks", [])
        if not asks:
            return None
        first = asks[0]
        if isinstance(first, dict):
            price = float(first.get("price", 0))
            size = float(first.get("size", first.get("amount", 0)))
        elif isinstance(first, (list, tuple)):
            price = float(first[0])
            size = float(first[1]) if len(first) > 1 else 0.0
        else:
            return None
        return (price, size)
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


def _log(action: str, market_slug: str = "", asset: str = "BTC", detail: dict = None, level: str = "info") -> None:
    """写入 action_log，与其他策略一致。"""
    if _db is None or _session_id is None:
        return
    detail_json = json.dumps(detail or {}, ensure_ascii=False)
    db_execute(
        _db,
        "INSERT INTO action_log (session_id, asset, market_slug, action, detail, level) VALUES (%s,%s,%s,%s,%s,%s)",
        (_session_id, asset, market_slug, action, detail_json, level),
    )


def _insert_paper_trade(
    bar_epoch: int,
    action: str,
    direction: Optional[str] = None,
    prob: Optional[float] = None,
    feat_1: Optional[float] = None,
    feat_2: Optional[float] = None,
    feat_5: Optional[float] = None,
    trend: Optional[str] = None,
    is_reversal: Optional[bool] = None,
    vol_regime: Optional[float] = None,
    skip_reason: Optional[str] = None,
    ask: Optional[float] = None,
    ask_size: Optional[float] = None,
    slug: Optional[str] = None,
    yes_token: Optional[str] = None,
    no_token: Optional[str] = None,
    question: Optional[str] = None,
) -> None:
    """写入 paper_bot_trades。"""
    if _db is None or _session_id is None:
        return
    status = "filled" if action == "filled" else "skipped"
    db_execute(
        _db,
        """INSERT INTO paper_bot_trades (
            session_id, bar_epoch, action, direction, prob, feat_1, feat_2, feat_5,
            trend, is_reversal, vol_regime, skip_reason, ask, ask_size,
            slug, yes_token, no_token, question, status
        ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
        (
            _session_id,
            bar_epoch,
            action,
            direction,
            prob,
            feat_1,
            feat_2,
            feat_5,
            trend,
            1 if is_reversal else 0 if is_reversal is not None else None,
            vol_regime,
            skip_reason,
            ask,
            ask_size,
            slug,
            yes_token,
            no_token,
            question,
            status,
        ),
    )


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


async def _run_decision_cycle(model) -> None:
    """
    单次决策周期：298 秒唤醒 → 过滤 → 特征 → 预测 → 风控 → 执行。
    prob = P(下一根收阳)。趋势 UP 时 prob<0.5 为反转(买NO)，prob>=0.6 为顺势(买YES)；
    趋势 DOWN 时 prob>=0.5 为反转(买YES)，prob<0.4 为顺势(买NO)。
    """
    now_ts = int(time.time())
    bar_start = (now_ts // 300) * 300
    next_bar_epoch = bar_start + 300

    is_ok, trend = _event_ok()
    if not is_ok or not trend:
        msg = "事件不满足，跳过"
        print(f"  [{datetime.now(timezone.utc).strftime('%H:%M:%S')} UTC] {msg}")
        _log("event_skip", f"btc-5m-{next_bar_epoch}", detail={"msg": msg})
        _insert_paper_trade(next_bar_epoch, "event_skip")
        return

    feat_1, feat_2 = _compute_feat1_feat2(bar_start)
    feat_5 = _compute_feat5()
    for k in list(_agg_buffer.keys()):
        if k < bar_start - 60:
            del _agg_buffer[k]
    vol_regime = _compute_vol_regime()

    prob = _predict(model, feat_1, feat_2, feat_5)

    # 方向判定：顺势 vs 反转
    direction: Optional[str] = None
    token_key: Optional[str] = None
    is_reversal = False

    if trend == "UP":
        if prob < 0.50:
            direction, token_key, is_reversal = "NO", "no_token", True
        elif prob >= 0.60:
            direction, token_key, is_reversal = "YES", "yes_token", False
    else:  # DOWN
        if prob >= 0.50:
            direction, token_key, is_reversal = "YES", "yes_token", True
        elif prob < 0.40:
            direction, token_key, is_reversal = "NO", "no_token", False

    if direction is None:
        msg = f"prob={prob:.3f} 趋势={trend} 在区间内，不下单"
        print(f"  [{datetime.now(timezone.utc).strftime('%H:%M:%S')} UTC] {msg}")
        _log("prob_in_range", f"btc-5m-{next_bar_epoch}", detail={"msg": msg, "prob": prob, "trend": trend})
        _insert_paper_trade(next_bar_epoch, "prob_in_range", prob=prob, trend=trend, feat_1=feat_1, feat_2=feat_2, feat_5=feat_5)
        return

    block_reason = _hard_filter_reversal(is_reversal, vol_regime)
    if block_reason:
        msg = f"风控拦截: {block_reason} (prob={prob:.3f} 反转={is_reversal})"
        print(f"  [{datetime.now(timezone.utc).strftime('%H:%M:%S')} UTC] {msg}")
        _log("risk_block", f"btc-5m-{next_bar_epoch}", detail={"msg": msg, "reason": block_reason}, level="warn")
        _insert_paper_trade(next_bar_epoch, "risk_block", direction=direction, prob=prob, trend=trend, is_reversal=is_reversal, vol_regime=vol_regime, skip_reason=block_reason, feat_1=feat_1, feat_2=feat_2, feat_5=feat_5)
        return

    pm = await _fetch_pm_5m_market(next_bar_epoch)
    if not pm:
        msg = f"PM 市场未找到 (epoch={next_bar_epoch})"
        print(f"  [{datetime.now(timezone.utc).strftime('%H:%M:%S')} UTC] {msg}")
        _log("pm_not_found", f"btc-5m-{next_bar_epoch}", detail={"msg": msg, "epoch": next_bar_epoch})
        _insert_paper_trade(next_bar_epoch, "pm_not_found", direction=direction, prob=prob, trend=trend, feat_1=feat_1, feat_2=feat_2, feat_5=feat_5)
        return

    token_id = pm[token_key]
    ask_result = await _fetch_best_ask(token_id)
    if ask_result is None:
        msg = "无法获取 Ask"
        print(f"  [{datetime.now(timezone.utc).strftime('%H:%M:%S')} UTC] {msg}")
        _log("no_ask", pm.get("slug", ""), detail={"msg": msg})
        _insert_paper_trade(next_bar_epoch, "no_ask", direction=direction, prob=prob, trend=trend, slug=pm.get("slug"), yes_token=pm.get("yes_token"), no_token=pm.get("no_token"), feat_1=feat_1, feat_2=feat_2, feat_5=feat_5)
        return
    ask_price, ask_size = ask_result

    if ask_size < MIN_ASK_SIZE:
        msg = f"盘口厚度不足: size={ask_size:.0f} < {MIN_ASK_SIZE}"
        print(f"  [{datetime.now(timezone.utc).strftime('%H:%M:%S')} UTC] {msg}")
        _log("thin_book_skip", pm.get("slug", ""), detail={"msg": msg, "ask_size": ask_size})
        _insert_paper_trade(next_bar_epoch, "thin_book_skip", direction=direction, prob=prob, ask=ask_price, ask_size=ask_size, slug=pm.get("slug"), yes_token=pm.get("yes_token"), no_token=pm.get("no_token"), feat_1=feat_1, feat_2=feat_2, feat_5=feat_5)
        return

    if ask_price > ASK_MAX:
        msg = f"滑点放弃: Ask={ask_price:.3f} > {ASK_MAX}"
        print(f"  [{datetime.now(timezone.utc).strftime('%H:%M:%S')} UTC] {msg}")
        _log("slippage_skip", pm.get("slug", ""), detail={"msg": msg, "ask": ask_price})
        _insert_paper_trade(next_bar_epoch, "slippage_skip", direction=direction, prob=prob, ask=ask_price, ask_size=ask_size, slug=pm.get("slug"), yes_token=pm.get("yes_token"), no_token=pm.get("no_token"), feat_1=feat_1, feat_2=feat_2, feat_5=feat_5)
        return

    msg = f"虚拟成交: {direction} @ {ask_price:.3f} size={ask_size:.0f} (prob={prob:.3f} 趋势={trend})"
    print(f"  [{datetime.now(timezone.utc).strftime('%H:%M:%S')} UTC] {msg}")
    _log("filled", pm.get("slug", ""), detail={"msg": msg, "direction": direction, "ask": ask_price, "prob": prob})
    _insert_paper_trade(next_bar_epoch, "filled", direction=direction, prob=prob, ask=ask_price, ask_size=ask_size, slug=pm.get("slug"), yes_token=pm.get("yes_token"), no_token=pm.get("no_token"), trend=trend, feat_1=feat_1, feat_2=feat_2, feat_5=feat_5)


def _update_activity(last_action: str = None, last_bar_epoch: int = None) -> None:
    """更新 paper_bot_activity 供 Dashboard 展示运行时状态。"""
    if _db is None or _session_id is None:
        return
    kline_count = len(_kline_5m_history)
    agg_sec = len(_agg_buffer)
    db_execute(
        _db,
        "INSERT INTO paper_bot_activity (session_id, kline_count, agg_buffer_sec, last_decision_action, last_bar_epoch) VALUES (%s,%s,%s,%s,%s)",
        (_session_id, kline_count, agg_sec, last_action, last_bar_epoch),
    )


async def main_loop(model_path: str) -> None:
    """主循环：Binance 时钟驱动。"""
    global _db, _session_id
    _db = get_conn()
    init_paper_bot_tables(_db)
    cur = db_execute(_db, "INSERT INTO sessions (mode, params) VALUES (%s, %s)", ("paper_bot", json.dumps({"model": str(model_path)})))
    _session_id = cur.lastrowid
    _log("session_start", "", detail={"msg": "Paper Bot V2 启动，按 Binance 5m 第 298 秒驱动"})

    _init_http_session()
    model = _load_model(model_path)
    await _prewarm_5m()
    _update_activity()

    agg_task = asyncio.create_task(_agg_producer())
    kline_task = asyncio.create_task(_kline_producer())

    try:
        while not _shutdown.is_set():
            await wait_until_trigger()
            if _shutdown.is_set():
                break
            now_ts = int(time.time())
            next_bar_epoch = (now_ts // 300) * 300 + 300
            _log("decision_cycle", f"btc-5m-{next_bar_epoch}", detail={"msg": f"决策点 {next_bar_epoch}"})
            await _run_decision_cycle(model)
            _update_activity(last_bar_epoch=next_bar_epoch)
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
        if _http_session and not _http_session.closed:
            await _http_session.close()
        if _session_id:
            _log("session_end", "", detail={"msg": "Paper Bot V2 退出"})


def main() -> None:
    import argparse
    parser = argparse.ArgumentParser(description="Paper Bot V2 — Binance 时钟驱动")
    parser.add_argument("--model", default="models/model_seller.pkl", help="模型路径")
    parser.add_argument("--log", help="(已废弃，现用 MySQL) 虚拟成交日志路径")
    args = parser.parse_args()

    model_path = PROJECT_ROOT / args.model
    if not model_path.exists():
        print(f"模型不存在: {model_path}")
        return

    def do_shutdown():
        _shutdown.set()

    try:
        signal.signal(signal.SIGINT, lambda *a: do_shutdown())
        signal.signal(signal.SIGTERM, lambda *a: do_shutdown())
    except (ValueError, OSError):
        pass

    print("Paper Bot V2 启动，按 Binance 5m 第 298 秒驱动...")
    asyncio.run(main_loop(str(model_path)))
    print("Bye.")


if __name__ == "__main__":
    main()
