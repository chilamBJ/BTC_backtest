"""
工业级 7x24 超高频数据采集器 (data_collector.py)
架构：asyncio.Queue 解耦 + 后台 Writer + Telegram 远程监控与控制
数据流：Binance aggTrade / forceOrder / depth20 + Polymarket V2（动态路由 / 逐笔 / 双轨订单簿 / 历史基准）
"""

from __future__ import annotations

import asyncio
import json
import os
import signal
import time
from collections import deque
from pathlib import Path
from typing import Any

import pandas as pd

# ---------------------------------------------------------------------------
# 配置（环境变量或默认）
# ---------------------------------------------------------------------------
DATA_DIR = Path(os.environ.get("COLLECTOR_DATA_DIR", "./collector_data"))
BINANCE_AGG_WS = "wss://stream.binance.com:9443/ws/btcusdt@aggTrade"
BINANCE_FORCE_WS = "wss://fstream.binance.com/ws/!forceOrder@arr"
BINANCE_DEPTH_WS = "wss://stream.binance.com:9443/ws/btcusdt@depth20@100ms"
BINANCE_KLINE_5M_WS = "wss://stream.binance.com:9443/ws/btcusdt@kline_5m"
BINANCE_AGG_REST = "https://api.binance.com/api/v3/aggTrades"
BINANCE_KLINE_REST = "https://api.binance.com/api/v3/klines"

# Polymarket V2
PM_GAMMA_URL = os.environ.get("PM_GAMMA_URL", "https://gamma-api.polymarket.com")
PM_DATA_API_URL = os.environ.get("PM_DATA_API_URL", "https://data-api.polymarket.com")
PM_CLOB_URL = os.environ.get("PM_CLOB_URL", "https://clob.polymarket.com")
PM_MARKET_ROUTER_INTERVAL = 60  # 秒
PM_TRADES_POLL_INTERVAL = 10
PM_HISTORY_INTERVAL = 300  # 5 分钟
PM_ORDERBOOK_LOW_FREQ = 60
PM_ORDERBOOK_HIGH_FREQ_LAST_SEC = 30  # K 线最后 30 秒改为每秒
PM_MIDPOINT_INTERVAL = 1  # 实时价格采集间隔（秒），与 trade 文件夹一致，使用 CLOB midpoint

TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
ADMIN_CHAT_ID = os.environ.get("ADMIN_CHAT_ID", "")

BATCH_SIZE = 1000
FLUSH_INTERVAL_SEC = 60.0
SENTINEL = object()  # 优雅退出毒丸

# 全局控制与状态（供 Telegram 与 Producer 使用）
is_paused = False
shutdown_event = asyncio.Event()
start_time: float = 0.0
last_heal_ts: float | None = None
today_rows: dict[str, int] = {
    "agg": 0, "force": 0, "depth": 0, "binance_5m": 0,
    "pm_trades": 0, "pm_midpoint": 0, "pm_orderbook": 0, "pm_history": 0,
}
enqueued_rows: dict[str, int] = {
    "agg": 0, "force": 0, "depth": 0, "binance_5m": 0,
    "pm_trades": 0, "pm_midpoint": 0, "pm_orderbook": 0, "pm_history": 0,
}
last_enqueue_ts: dict[str, float | None] = {
    "agg": None, "force": None, "depth": None, "binance_5m": None,
    "pm_trades": None, "pm_midpoint": None, "pm_orderbook": None, "pm_history": None,
}
last_flush_ts: dict[str, float | None] = {
    "agg": None, "force": None, "depth": None, "binance_5m": None,
    "pm_trades": None, "pm_midpoint": None, "pm_orderbook": None, "pm_history": None,
}
queues: dict[str, asyncio.Queue] = {}
alert_sender = None  # 由 main 注入 send_alert 的占位

# Polymarket 动态市场路由：当前 Active 的 5m/15m 市场（由 pm_market_router 维护）
# 结构 {'5m': {'condition_id': str, 'token_ids': [yes_id, no_id]}, '15m': {...}}
active_pm_markets: dict[str, dict[str, Any]] = {}


async def enqueue_row(queue_name: str, row: dict[str, Any]) -> bool:
    """统一入队并记录实时入队计数/时间。"""
    q = queues.get(queue_name)
    if not q:
        return False
    await q.put(row)
    enqueued_rows[queue_name] = enqueued_rows.get(queue_name, 0) + 1
    last_enqueue_ts[queue_name] = time.time()
    return True


def _fmt_ts(ts: float | None) -> str:
    if not ts:
        return "从未"
    return time.strftime("%H:%M:%S", time.localtime(ts))


def send_alert_sync(msg: str) -> None:
    """供同步上下文调用的告警（在事件循环里 schedule 发送）。"""
    if alert_sender:
        asyncio.get_event_loop().call_soon_thread_safe(
            lambda: asyncio.create_task(alert_sender(msg))
        )


# ---------------------------------------------------------------------------
# 后台 Writer：从 Queue 取数，批量落盘 Parquet
# ---------------------------------------------------------------------------
def _parquet_append(file_path: Path, df_new: pd.DataFrame, schema_columns: list[str]) -> None:
    """将新数据追加到按日分片的 parquet 文件（读-合并-写）。"""
    df_new = df_new.reindex(columns=schema_columns)
    if file_path.exists():
        try:
            df_old = pd.read_parquet(file_path, engine="pyarrow")
            df = pd.concat([df_old, df_new], ignore_index=True)
        except Exception:
            df = df_new
    else:
        df = df_new
    file_path.parent.mkdir(parents=True, exist_ok=True)
    df.to_parquet(file_path, engine="pyarrow", index=False)


async def writer_worker(
    name: str,
    queue: asyncio.Queue,
    file_prefix: str,
    columns: list[str],
    row_counter_key: str,
) -> None:
    """后台写入任务：累积 BATCH_SIZE 或 FLUSH_INTERVAL_SEC 后追加到按日 parquet。"""
    buffer: list[dict[str, Any]] = []
    last_flush = time.monotonic()
    while True:
        try:
            timeout = max(0.1, FLUSH_INTERVAL_SEC - (time.monotonic() - last_flush))
            try:
                item = await asyncio.wait_for(queue.get(), timeout=timeout)
            except asyncio.TimeoutError:
                item = None
            if item is SENTINEL:
                break
            if item is not None:
                buffer.append(item)
            if len(buffer) >= BATCH_SIZE or (
                buffer and (time.monotonic() - last_flush) >= FLUSH_INTERVAL_SEC
            ):
                day_str = time.strftime("%Y%m%d", time.gmtime())
                path = DATA_DIR / f"{file_prefix}_{day_str}.parquet"
                df = pd.DataFrame(buffer)
                _parquet_append(path, df, columns)
                today_rows[row_counter_key] = today_rows.get(row_counter_key, 0) + len(buffer)
                last_flush_ts[row_counter_key] = time.time()
                buffer = []
                last_flush = time.monotonic()
        except asyncio.CancelledError:
            break
        except Exception as e:
            if alert_sender:
                await alert_sender(f"⚠️ Writer [{name}] 异常: {e}")
    # 优雅退出：剩余 buffer 全部落盘
    if buffer:
        day_str = time.strftime("%Y%m%d", time.gmtime())
        path = DATA_DIR / f"{file_prefix}_{day_str}.parquet"
        df = pd.DataFrame(buffer)
        _parquet_append(path, df, columns)
        today_rows[row_counter_key] = today_rows.get(row_counter_key, 0) + len(buffer)
        last_flush_ts[row_counter_key] = time.time()
    queue.task_done()


# ---------------------------------------------------------------------------
# 1. Binance aggTrade：每秒聚合 buy_vol, sell_vol, trade_count + 断线自愈
# ---------------------------------------------------------------------------
AGG_COLUMNS = ["ts_sec", "buy_vol", "sell_vol", "trade_count"]


async def _agg_rest_fill(symbol: str, from_ts_ms: int, to_ts_ms: int) -> None:
    """REST 补齐 aggTrades 缺失区间，压入 queue_agg。"""
    global last_heal_ts
    try:
        import aiohttp
        url = f"{BINANCE_AGG_REST}?symbol={symbol.upper()}&startTime={from_ts_ms}&endTime={to_ts_ms}&limit=1000"
        async with aiohttp.ClientSession() as session:
            async with session.get(url) as resp:
                if resp.status != 200:
                    return
                data = await resp.json()
        if not data:
            return
        by_sec: dict[int, list] = {}
        for t in data:
            ts_sec = t["T"] // 1000
            q = float(t["q"])
            is_buy = t["m"]
            if ts_sec not in by_sec:
                by_sec[ts_sec] = [0.0, 0.0, 0]
            if is_buy:
                by_sec[ts_sec][0] += q
            else:
                by_sec[ts_sec][1] += q
            by_sec[ts_sec][2] += 1
        if is_paused:
            return
        for ts_sec in sorted(by_sec.keys()):
            bv, sv, tc = by_sec[ts_sec]
            await enqueue_row("agg", {"ts_sec": ts_sec, "buy_vol": bv, "sell_vol": sv, "trade_count": tc})
        last_heal_ts = time.time()
        if alert_sender:
            await alert_sender("🔧 REST 自愈已执行：aggTrade 缺失区间已补齐")
    except Exception as e:
        if alert_sender:
            await alert_sender(f"⚠️ REST 自愈失败: {e}")


async def producer_agg() -> None:
    """aggTrade WebSocket：按秒聚合，每 5 分钟检查连续性，断层则 REST 补齐。"""
    last_ts_sec = -1
    buf: dict[int, list] = {}  # ts_sec -> [buy_vol, sell_vol, trade_count]
    check_interval = 300  # 5 分钟
    last_check = time.monotonic()
    reconnect_count = 0
    while not shutdown_event.is_set():
        try:
            import aiohttp
            async with aiohttp.ClientSession() as session:
                async with session.ws_connect(BINANCE_AGG_WS) as ws:
                    if reconnect_count > 0 and alert_sender:
                        await alert_sender("🟢 WebSocket aggTrade 已重连")
                    reconnect_count += 1
                    async for raw in ws:
                        if shutdown_event.is_set():
                            break
                        if is_paused:
                            continue
                        msg = json.loads(raw.data)
                        ts_ms = msg["T"]
                        ts_sec = ts_ms // 1000
                        q = float(msg["q"])
                        is_buy = 0 if msg["m"] else 1  # Binance: m=true 表示买方是 maker（即卖方主动）
                        if msg["m"]:
                            sell_vol, buy_vol = q, 0.0
                        else:
                            sell_vol, buy_vol = 0.0, q
                        if ts_sec not in buf:
                            buf[ts_sec] = [0.0, 0.0, 0]
                        buf[ts_sec][0] += buy_vol
                        buf[ts_sec][1] += sell_vol
                        buf[ts_sec][2] += 1
                        # 若已进入新秒，则把上一秒写出
                        if last_ts_sec >= 0 and ts_sec > last_ts_sec:
                            for s in range(last_ts_sec + 1, ts_sec):
                                if s not in buf:
                                    buf[s] = [0.0, 0.0, 0]
                            for s in range(last_ts_sec, ts_sec):
                                row = buf.pop(s, None)
                                if row:
                                    await enqueue_row("agg", {
                                        "ts_sec": s,
                                        "buy_vol": row[0],
                                        "sell_vol": row[1],
                                        "trade_count": row[2],
                                    })
                        last_ts_sec = ts_sec
                        # 每 5 分钟检查时间连续性
                        if time.monotonic() - last_check >= check_interval:
                            last_check = time.monotonic()
                            now_sec = int(time.time())
                            if last_ts_sec >= 0 and now_sec - last_ts_sec > 120:
                                if alert_sender:
                                    await alert_sender("🔧 检测到 aggTrade 时间断层，正在 REST 补齐…")
                                await _agg_rest_fill(
                                    "btcusdt",
                                    (last_ts_sec + 1) * 1000,
                                    min(now_sec * 1000, (last_ts_sec + 3600) * 1000),
                                )
        except asyncio.CancelledError:
            break
        except Exception as e:
            if alert_sender:
                await alert_sender(f"⚠️ aggTrade WebSocket 异常: {e}")
        if not shutdown_event.is_set():
            await asyncio.sleep(5)


# ---------------------------------------------------------------------------
# 2. Binance forceOrder：强平事件
# ---------------------------------------------------------------------------
FORCE_COLUMNS = ["ts", "side", "qty", "price", "symbol"]


# ---------------------------------------------------------------------------
# 2b. Binance 5m K 线：宏观环境 + feat_5 / vol_regime
# ---------------------------------------------------------------------------
BINANCE_5M_COLUMNS = ["timestamp", "open", "high", "low", "close", "volume"]
_binance_5m_history: deque = deque(maxlen=288)  # 近期 5m K 线（24h），供实盘 feat_5 / vol_regime


async def _prewarm_binance_5m() -> None:
    """启动时拉取过去 24 小时 5m K 线，填充 _binance_5m_history。"""
    global _binance_5m_history
    try:
        import aiohttp
        async with aiohttp.ClientSession() as session:
            async with session.get(
                BINANCE_KLINE_REST,
                params={"symbol": "BTCUSDT", "interval": "5m", "limit": 288},
            ) as resp:
                if resp.status != 200:
                    return
                data = await resp.json()
        _binance_5m_history.clear()
        for bar in data:
            ts_ms, o, h, l, c, v = bar[0], float(bar[1]), float(bar[2]), float(bar[3]), float(bar[4]), float(bar[5])
            _binance_5m_history.append({"timestamp": ts_ms, "open": o, "high": h, "low": l, "close": c, "volume": v})
        if alert_sender:
            await alert_sender(f"🔧 Binance 5m 预热完成，加载 {len(_binance_5m_history)} 根 K 线")
    except Exception as e:
        if alert_sender:
            await alert_sender(f"⚠️ Binance 5m 预热失败: {e}")


async def producer_binance_5m() -> None:
    """订阅 kline_5m，仅在 x=True（收盘）时落盘并更新 deque。"""
    global _binance_5m_history
    reconnect_count = 0
    prewarm_done = False
    while not shutdown_event.is_set():
        try:
            if not prewarm_done:
                await _prewarm_binance_5m()
                prewarm_done = True
            import aiohttp
            async with aiohttp.ClientSession() as session:
                async with session.ws_connect(BINANCE_KLINE_5M_WS) as ws:
                    if reconnect_count > 0 and alert_sender:
                        await alert_sender("🟢 Binance 5m K 线 WebSocket 已重连")
                    reconnect_count += 1
                    async for raw in ws:
                        if shutdown_event.is_set() or is_paused:
                            continue
                        msg = json.loads(raw.data)
                        k = msg.get("k")
                        if not k or not k.get("x"):
                            continue
                        row = {
                            "timestamp": int(k["t"]),
                            "open": float(k["o"]),
                            "high": float(k["h"]),
                            "low": float(k["l"]),
                            "close": float(k["c"]),
                            "volume": float(k["v"]),
                        }
                        await enqueue_row("binance_5m", row)
                        _binance_5m_history.append(row)
        except asyncio.CancelledError:
            break
        except Exception as e:
            if alert_sender:
                await alert_sender(f"⚠️ Binance 5m K 线 WebSocket 异常: {e}")
        if not shutdown_event.is_set():
            await asyncio.sleep(5)


async def producer_force() -> None:
    """监听 !forceOrder@arr，每条压入 queue_force。"""
    while not shutdown_event.is_set():
        try:
            import aiohttp
            async with aiohttp.ClientSession() as session:
                async with session.ws_connect(BINANCE_FORCE_WS) as ws:
                    async for raw in ws:
                        if shutdown_event.is_set() or is_paused:
                            continue
                        msg = json.loads(raw.data)
                        o = msg.get("o", msg)
                        ts = o.get("T", o.get("E", 0))
                        side = o.get("S", "UNKNOWN")
                        qty = float(o.get("q", o.get("Q", 0)))
                        price = float(o.get("p", o.get("P", 0)))
                        symbol = o.get("s", "")
                        await enqueue_row("force", {
                                "ts": ts,
                                "side": str(side),
                                "qty": qty,
                                "price": price,
                                "symbol": str(symbol),
                            })
        except asyncio.CancelledError:
            break
        except Exception as e:
            if alert_sender:
                await alert_sender(f"⚠️ forceOrder WebSocket 异常: {e}")
        if not shutdown_event.is_set():
            await asyncio.sleep(5)


# ---------------------------------------------------------------------------
# 3. Binance L2 depth20@100ms：内存维护，每整秒取 Top10 压入队列
# ---------------------------------------------------------------------------
DEPTH_COLUMNS = [
    "ts_sec", "bid_1", "ask_1", "bid_size_1", "ask_size_1",
    "bid_2", "ask_2", "bid_size_2", "ask_size_2",
    "bid_3", "ask_3", "bid_size_3", "ask_size_3",
    "bid_4", "ask_4", "bid_size_4", "ask_size_4",
    "bid_5", "ask_5", "bid_size_5", "ask_size_5",
    "bid_6", "ask_6", "bid_size_6", "ask_size_6",
    "bid_7", "ask_7", "bid_size_7", "ask_size_7",
    "bid_8", "ask_8", "bid_size_8", "ask_size_8",
    "bid_9", "ask_9", "bid_size_9", "ask_size_9",
    "bid_10", "ask_10", "bid_size_10", "ask_size_10",
]

_l2_bids: list[tuple[float, float]] = []
_l2_asks: list[tuple[float, float]] = []


async def producer_depth() -> None:
    """depth20@100ms：维护最新深度，每 1 秒取 Top10 写入队列。"""
    global _l2_bids, _l2_asks

    def snapshot_row(ts_sec: int) -> dict[str, Any]:
        row: dict[str, Any] = {"ts_sec": ts_sec}
        for i in range(10):
            b = _l2_bids[i] if i < len(_l2_bids) else (0.0, 0.0)
            a = _l2_asks[i] if i < len(_l2_asks) else (0.0, 0.0)
            row[f"bid_{i+1}"] = b[0]
            row[f"ask_{i+1}"] = a[0]
            row[f"bid_size_{i+1}"] = b[1]
            row[f"ask_size_{i+1}"] = a[1]
        return row

    while not shutdown_event.is_set():
        try:
            import aiohttp
            async with aiohttp.ClientSession() as session:
                async with session.ws_connect(BINANCE_DEPTH_WS) as ws:
                    next_sec = int(time.time()) + 1
                    async for raw in ws:
                        if shutdown_event.is_set():
                            break
                        msg = json.loads(raw.data)
                        bids = [(float(p), float(q)) for p, q in msg.get("bids", [])]
                        asks = [(float(p), float(q)) for p, q in msg.get("asks", [])]
                        _l2_bids = sorted(bids, key=lambda x: -x[0])[:10]
                        _l2_asks = sorted(asks, key=lambda x: x[0])[:10]
                        now = int(time.time())
                        if now >= next_sec and not is_paused:
                            await enqueue_row("depth", snapshot_row(next_sec - 1))
                            next_sec = now + 1
        except asyncio.CancelledError:
            break
        except Exception as e:
            if alert_sender:
                await alert_sender(f"⚠️ depth WebSocket 异常: {e}")
        if not shutdown_event.is_set():
            await asyncio.sleep(5)


# ---------------------------------------------------------------------------
# 4. Polymarket V2：动态路由 + 逐笔提纯 + 双轨订单簿 + 历史基准
# ---------------------------------------------------------------------------
PM_TRADES_COLUMNS = ["timestamp", "price", "size", "side", "outcome", "conditionId", "market_type"]
PM_MIDPOINT_COLUMNS = ["ts_utc", "market_type", "bar_sec", "yes_mid", "no_mid", "spread"]
PM_ORDERBOOK_COLUMNS = [
    "ts_utc", "market_type", "bar_sec",
    "yes_bid_1", "yes_ask_1", "yes_bid_size_1", "yes_ask_size_1",
    "yes_bid_2", "yes_ask_2", "yes_bid_size_2", "yes_ask_size_2",
    "yes_bid_3", "yes_ask_3", "yes_bid_size_3", "yes_ask_size_3",
    "no_bid_1", "no_ask_1", "no_bid_size_1", "no_ask_size_1",
    "no_bid_2", "no_ask_2", "no_bid_size_2", "no_ask_size_2",
    "no_bid_3", "no_ask_3", "no_bid_size_3", "no_ask_size_3",
]
PM_HISTORY_COLUMNS = ["t", "p", "market_type", "condition_id"]


def _gamma_parse_token_ids(raw: Any) -> list[str]:
    """解析 Gamma 返回的 clobTokenIds（可能为 JSON 字符串或数组）。"""
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


def _market_text(m: dict) -> str:
    """拼接市场名称相关字段（用于匹配）。"""
    return " ".join(
        str(x or "") for x in [
            m.get("question"),
            m.get("slug"),
            m.get("title"),
        ]
    ).lower()


def _is_btc_5m_market(m: dict) -> bool:
    """判断是否为当前可用的 BTC 5 分钟市场。兼容 5m / 5 m / 5-m / 5min 等写法。"""
    q = _market_text(m)
    has_btc = "btc" in q or "bitcoin" in q
    has_5m = any(p in q for p in ("5m", "5 m", "5-m", "5min", "5 min"))
    return has_btc and has_5m


def _is_btc_15m_market(m: dict) -> bool:
    """判断是否为当前可用的 BTC 15 分钟市场。兼容 15m / 15 m / 15-m / 15min 等写法。"""
    q = _market_text(m)
    has_btc = "btc" in q or "bitcoin" in q
    has_15m = any(p in q for p in ("15m", "15 m", "15-m", "15min", "15 min"))
    return has_btc and has_15m


async def pm_market_router() -> None:
    """
    动态市场路由：每 PM_MARKET_ROUTER_INTERVAL 秒通过 Gamma API 拉取当前 BTC 5m/15m 市场。
    使用 Events API + 计算 slug（btc-updown-5m-{ts} / btc-updown-15m-{ts}），
    因 Polymarket 的 5m/15m 市场不出现在 /markets 列表中。
    """
    global active_pm_markets
    import aiohttp
    events_url = f"{PM_GAMMA_URL}/events"
    fail_count = 0
    headers = {"User-Agent": "Mozilla/5.0 (compatible; BTCCollector/1.0)"}
    timeout = aiohttp.ClientTimeout(total=15, connect=8)
    while not shutdown_event.is_set():
        try:
            new_5m = None
            new_15m = None
            last_err = None
            async with aiohttp.ClientSession(headers=headers, timeout=timeout) as session:
                now_ts = int(time.time())
                base_5 = (now_ts // 300) * 300
                base_15 = (now_ts // 900) * 900
                for slug_prefix, base, window in [
                    ("btc-updown-5m", base_5, 300),
                    ("btc-updown-15m", base_15, 900),
                ]:
                    for offset in (0, -window, window, -2 * window, 2 * window):
                        ts = base + offset
                        slug = f"{slug_prefix}-{ts}"
                        try:
                            async with session.get(events_url, params={"slug": slug}) as resp:
                                if resp.status != 200:
                                    last_err = f"HTTP {resp.status}"
                                    continue
                                data = await resp.json()
                        except Exception as ex:
                            last_err = str(ex)[:80]
                            continue
                        events = data if isinstance(data, list) else (data.get("data") or data.get("events") or [])
                        if not isinstance(events, list) or not events:
                            last_err = "empty"
                            continue
                        e = events[0]
                        markets = e.get("markets") or []
                        if not markets:
                            last_err = "no markets"
                            continue
                        m = markets[0]
                        cid = m.get("conditionId") or m.get("condition_id")
                        tokens = _gamma_parse_token_ids(m.get("clobTokenIds") or m.get("clob_token_ids"))
                        if cid and len(tokens) >= 2:
                            info = {"condition_id": cid, "token_ids": [tokens[0], tokens[1]]}
                            if "5m" in slug_prefix:
                                new_5m = info
                            else:
                                new_15m = info
                            break
            if new_5m and not new_15m:
                async with aiohttp.ClientSession(headers=headers, timeout=timeout) as retry_session:
                    for offset in (0, -900, 900, -1800, 1800):
                        ts = base_15 + offset
                        slug = f"btc-updown-15m-{ts}"
                        try:
                            async with retry_session.get(events_url, params={"slug": slug}) as resp:
                                if resp.status != 200:
                                    continue
                                data = await resp.json()
                        except Exception:
                            continue
                        events = data if isinstance(data, list) else (data.get("data") or data.get("events") or [])
                        if isinstance(events, list) and events and events[0].get("markets"):
                            m = events[0]["markets"][0]
                            cid = m.get("conditionId") or m.get("condition_id")
                            tokens = _gamma_parse_token_ids(m.get("clobTokenIds") or m.get("clob_token_ids"))
                            if cid and len(tokens) >= 2:
                                new_15m = {"condition_id": cid, "token_ids": [tokens[0], tokens[1]]}
                                break
            prev_5m = active_pm_markets.get("5m", {}).get("condition_id")
            prev_15m = active_pm_markets.get("15m", {}).get("condition_id")
            updated = {}
            if new_5m:
                updated["5m"] = new_5m
                fail_count = 0
            if new_15m:
                updated["15m"] = new_15m
                fail_count = 0
            if updated:
                active_pm_markets.update(updated)
                if not prev_5m and not prev_15m:
                    print(f"[PM Router] 首次获取成功: 5m={bool(new_5m)} 15m={bool(new_15m)}")
            if (new_5m and new_5m["condition_id"] != prev_5m) or (new_15m and new_15m["condition_id"] != prev_15m):
                if alert_sender:
                    await alert_sender("🔄 PM 路由更新：检测到 5m/15m 市场切换，已更新 active_pm_markets")
            if not updated:
                fail_count += 1
                if fail_count >= 3 and alert_sender:
                    await alert_sender(
                        f"⚠️ PM 路由连续 {fail_count} 次失败，5m/15m 仍为 N/A。"
                        f"最后错误: {last_err or 'unknown'}。请检查云端能否访问 gamma-api.polymarket.com"
                    )
                    fail_count = 0
        except asyncio.CancelledError:
            break
        except Exception as e:
            fail_count += 1
            if alert_sender:
                await alert_sender(f"⚠️ PM 市场路由异常: {e}")
        await asyncio.sleep(PM_MARKET_ROUTER_INTERVAL)


def _purify_trade(raw: dict, market_type: str) -> dict[str, Any]:
    """只保留 timestamp, price, size, side, outcome, conditionId, market_type。"""
    ts = raw.get("timestamp") or raw.get("ts") or 0
    if isinstance(ts, str) and ts.isdigit():
        ts = int(ts)
    outcome = (raw.get("outcome") or "Unknown").strip()
    if not outcome and raw.get("asset"):
        outcome = "Yes"  # 可由调用方按 asset 与 token_ids 区分
    return {
        "timestamp": int(ts),
        "price": float(raw.get("price", 0)),
        "size": float(raw.get("size", 0)),
        "side": str(raw.get("side", "BUY"))[:4].upper(),
        "outcome": outcome,
        "conditionId": str(raw.get("conditionId", raw.get("condition_id", ""))),
        "market_type": market_type,
    }


async def pm_trades_poller() -> None:
    """轮询 Data API /trades，按 conditionId 拉取 5m/15m 实盘成交，提纯后压入 pm_trades 队列。"""
    import aiohttp
    while not shutdown_event.is_set():
        if is_paused:
            await asyncio.sleep(PM_TRADES_POLL_INTERVAL)
            continue
        q = queues.get("pm_trades")
        if not q:
            await asyncio.sleep(PM_TRADES_POLL_INTERVAL)
            continue
        for market_type, info in list(active_pm_markets.items()):
            cid = info.get("condition_id")
            token_ids = info.get("token_ids", [])
            if not cid:
                continue
            try:
                async with aiohttp.ClientSession() as session:
                    async with session.get(
                        f"{PM_DATA_API_URL}/trades",
                        params={"market": cid, "limit": 500, "takerOnly": "true"},
                    ) as resp:
                        if resp.status != 200:
                            continue
                        data = await resp.json()
                if not isinstance(data, list):
                    continue
                for t in data:
                    if not t.get("outcome") and token_ids and t.get("asset"):
                        t = dict(t)
                        t["outcome"] = "Yes" if str(t["asset"]) == str(token_ids[0]) else "No"
                    row = _purify_trade(t, market_type)
                    await enqueue_row("pm_trades", row)
            except asyncio.CancelledError:
                raise
            except Exception as e:
                if alert_sender:
                    await alert_sender(f"⚠️ PM Trades 拉取异常 ({market_type}): {e}")
        await asyncio.sleep(PM_TRADES_POLL_INTERVAL)


async def pm_midpoint_poller() -> None:
    """
    实时价格采集：使用 CLOB midpoint API（与 trade/collect_data_v3、smart_seller_live 一致）。
    比 orderbook 更高效：1 次请求/市场 vs 2 次，返回 ~20 字节 vs 完整盘口。
    """
    import aiohttp
    while not shutdown_event.is_set():
        if is_paused:
            await asyncio.sleep(PM_MIDPOINT_INTERVAL)
            continue
        q = queues.get("pm_midpoint")
        if not q:
            await asyncio.sleep(PM_MIDPOINT_INTERVAL)
            continue
        now = time.time()
        now_sec = int(now)
        bar_5m = now_sec % 300
        bar_15m = now_sec % 900
        items = [
            (mt, info) for mt, info in list(active_pm_markets.items())
            if info.get("token_ids")
        ]
        if not items:
            await asyncio.sleep(PM_MIDPOINT_INTERVAL)
            continue
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=5)) as session:
            async def fetch_one(market_type: str, token_ids: list):
                yes_mid = None
                try:
                    async with session.get(
                        f"{PM_CLOB_URL}/midpoint",
                        params={"token_id": token_ids[0]},
                    ) as resp:
                        if resp.status == 200:
                            data = await resp.json()
                            mid = float(data.get("mid", 0))
                            if 0 < mid < 1:
                                yes_mid = mid
                except Exception:
                    pass
                return market_type, yes_mid

            results = await asyncio.gather(
                *[fetch_one(mt, info["token_ids"]) for mt, info in items],
                return_exceptions=True,
            )
        for r in results:
            if isinstance(r, Exception):
                continue
            market_type, yes_mid = r
            if yes_mid is None:
                continue
            no_mid = round(1.0 - yes_mid, 6)
            spread = round(abs(yes_mid - no_mid), 4)
            bar_sec = bar_5m if market_type == "5m" else bar_15m
            await enqueue_row("pm_midpoint", {
                "ts_utc": now_sec,
                "market_type": market_type,
                "bar_sec": bar_sec,
                "yes_mid": yes_mid,
                "no_mid": no_mid,
                "spread": spread,
            })
        await asyncio.sleep(PM_MIDPOINT_INTERVAL)


def _top3_levels(levels: list) -> list[tuple[float, float]]:
    """从 CLOB book 的 bids 或 asks 取 Top3 (price, size)。"""
    out = []
    for x in (levels or [])[:3]:
        if isinstance(x, dict):
            p, s = float(x.get("price", 0)), float(x.get("size", 0))
        else:
            p, s = float(x[0]), float(x[1])
        out.append((p, s))
    while len(out) < 3:
        out.append((0.0, 0.0))
    return out[:3]


async def pm_orderbook_snapshot(market_type: str, token_ids: list, ts_utc: int, bar_sec: int) -> dict[str, Any] | None:
    """拉取 YES/NO 两个 token 的 orderbook，拼成 Top3 快照一行。"""
    import aiohttp
    if len(token_ids) < 2:
        return None
    row: dict[str, Any] = {
        "ts_utc": ts_utc,
        "market_type": market_type,
        "bar_sec": bar_sec,
        "yes_bid_1": 0, "yes_ask_1": 0, "yes_bid_size_1": 0, "yes_ask_size_1": 0,
        "yes_bid_2": 0, "yes_ask_2": 0, "yes_bid_size_2": 0, "yes_ask_size_2": 0,
        "yes_bid_3": 0, "yes_ask_3": 0, "yes_bid_size_3": 0, "yes_ask_size_3": 0,
        "no_bid_1": 0, "no_ask_1": 0, "no_bid_size_1": 0, "no_ask_size_1": 0,
        "no_bid_2": 0, "no_ask_2": 0, "no_bid_size_2": 0, "no_ask_size_2": 0,
        "no_bid_3": 0, "no_ask_3": 0, "no_bid_size_3": 0, "no_ask_size_3": 0,
    }
    try:
        async with aiohttp.ClientSession() as session:
            for i, tid in enumerate(token_ids[:2]):
                async with session.get(f"{PM_CLOB_URL}/book", params={"token_id": tid}) as resp:
                    if resp.status != 200:
                        continue
                    data = await resp.json()
                bids = data.get("bids", [])
                asks = data.get("asks", [])
                b = _top3_levels(bids)
                a = _top3_levels(asks)
                if i == 0:
                    prefix = "yes"
                else:
                    prefix = "no"
                for j in range(3):
                    row[f"{prefix}_bid_{j+1}"] = b[j][0]
                    row[f"{prefix}_ask_{j+1}"] = a[j][0]
                    row[f"{prefix}_bid_size_{j+1}"] = b[j][1]
                    row[f"{prefix}_ask_size_{j+1}"] = a[j][1]
        return row
    except Exception:
        return None


async def pm_orderbook_dual_track() -> None:
    """
    双轨订单簿：常规每 60 秒一次；5m/15m K 线最后 30 秒内改为每 1 秒一次。
    快照为 Top3 Bid/Ask + Size，带 market_type。
    """
    last_low_snap = 0.0
    while not shutdown_event.is_set():
        if is_paused:
            await asyncio.sleep(1)
            continue
        q = queues.get("pm_orderbook")
        if not q:
            await asyncio.sleep(1)
            continue
        now = time.time()
        bar_5m = int(now) % 300
        bar_15m = int(now) % 900
        in_high_freq_5m = (300 - PM_ORDERBOOK_HIGH_FREQ_LAST_SEC) <= bar_5m < 300
        in_high_freq_15m = (900 - PM_ORDERBOOK_HIGH_FREQ_LAST_SEC) <= bar_15m < 900
        use_high_freq = in_high_freq_5m or in_high_freq_15m
        interval = 1.0 if use_high_freq else float(PM_ORDERBOOK_LOW_FREQ)
        if not use_high_freq and (now - last_low_snap) < PM_ORDERBOOK_LOW_FREQ:
            await asyncio.sleep(1)
            continue
        if not use_high_freq:
            last_low_snap = now
        for market_type, info in list(active_pm_markets.items()):
            token_ids = info.get("token_ids", [])
            if not token_ids:
                continue
            bar_sec = bar_5m if market_type == "5m" else bar_15m
            row = await pm_orderbook_snapshot(market_type, token_ids, int(now), bar_sec)
            if row:
                await enqueue_row("pm_orderbook", row)
        await asyncio.sleep(interval)


async def pm_history_poller() -> None:
    """每 5 分钟拉取当前 5m/15m 的 prices-history，压入 pm_history 队列。"""
    import aiohttp
    while not shutdown_event.is_set():
        await asyncio.sleep(PM_HISTORY_INTERVAL)
        if is_paused:
            continue
        q = queues.get("pm_history")
        if not q:
            continue
        for market_type, info in list(active_pm_markets.items()):
            token_ids = info.get("token_ids", [])
            cid = info.get("condition_id", "")
            if not token_ids:
                continue
            token_id = token_ids[0]
            try:
                async with aiohttp.ClientSession() as session:
                    async with session.get(
                        f"{PM_CLOB_URL}/prices-history",
                        params={"market": token_id, "interval": "1h", "fidelity": 5},
                    ) as resp:
                        if resp.status != 200:
                            continue
                        data = await resp.json()
                history = data.get("history", [])
                for h in history:
                    await enqueue_row("pm_history", {
                        "t": int(h.get("t", 0)),
                        "p": float(h.get("p", 0)),
                        "market_type": market_type,
                        "condition_id": cid,
                    })
            except asyncio.CancelledError:
                raise
            except Exception as e:
                if alert_sender:
                    await alert_sender(f"⚠️ PM History 拉取异常 ({market_type}): {e}")


# ---------------------------------------------------------------------------
# Telegram Bot：报警 + /status /pause /resume /stop
# ---------------------------------------------------------------------------
async def send_alert(msg: str) -> None:
    if not TELEGRAM_BOT_TOKEN or not ADMIN_CHAT_ID:
        print(f"[ALERT] {msg}")
        return
    try:
        from telegram import Bot
        bot = Bot(token=TELEGRAM_BOT_TOKEN)
        await bot.send_message(chat_id=ADMIN_CHAT_ID, text=msg)
    except Exception as e:
        print(f"[ALERT 发送失败] {e}\n{msg}")


async def run_telegram_bot() -> tuple[asyncio.Task | None, object]:
    """在已有事件循环中启动 Telegram Bot 监听（需在 main 中 await 后得到 polling 任务）。"""
    if not TELEGRAM_BOT_TOKEN or not ADMIN_CHAT_ID:
        print("未配置 TELEGRAM_BOT_TOKEN / ADMIN_CHAT_ID，Telegram 功能关闭")
        return None, None

    from telegram import Update
    from telegram.ext import Application, CommandHandler, ContextTypes, filters

    admin_id = int(ADMIN_CHAT_ID) if ADMIN_CHAT_ID.isdigit() else None
    admin_filter = filters.Chat(chat_id=admin_id) if admin_id else None

    app = (
        Application.builder()
        .token(TELEGRAM_BOT_TOKEN)
        .connect_timeout(30.0)
        .read_timeout(30.0)
        .write_timeout(30.0)
        .get_updates_connect_timeout(30.0)
        .get_updates_read_timeout(65.0)
        .get_updates_write_timeout(30.0)
        .build()
    )

    async def status_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        uptime = time.time() - start_time
        q_sizes = {k: v.qsize() for k, v in queues.items()}
        heal_str = time.ctime(last_heal_ts) if last_heal_ts else "从未"
        pm_5m = active_pm_markets.get("5m") or {}
        pm_15m = active_pm_markets.get("15m") or {}
        pm_status = (
            f"PM 5m: cond={pm_5m.get('condition_id', 'N/A')[:16]}… tokens={len(pm_5m.get('token_ids', []))}\n"
            f"PM 15m: cond={pm_15m.get('condition_id', 'N/A')[:16]}… tokens={len(pm_15m.get('token_ids', []))}"
        )
        enq_text = (
            f"agg={enqueued_rows.get('agg',0)} force={enqueued_rows.get('force',0)} depth={enqueued_rows.get('depth',0)} 5m={enqueued_rows.get('binance_5m',0)} "
            f"pm_trades={enqueued_rows.get('pm_trades',0)} pm_mid={enqueued_rows.get('pm_midpoint',0)} "
            f"pm_ob={enqueued_rows.get('pm_orderbook',0)} pm_hist={enqueued_rows.get('pm_history',0)}"
        )
        flush_text = (
            f"agg={_fmt_ts(last_flush_ts.get('agg'))} force={_fmt_ts(last_flush_ts.get('force'))} depth={_fmt_ts(last_flush_ts.get('depth'))} "
            f"pm_trades={_fmt_ts(last_flush_ts.get('pm_trades'))} pm_mid={_fmt_ts(last_flush_ts.get('pm_midpoint'))} "
            f"pm_ob={_fmt_ts(last_flush_ts.get('pm_orderbook'))} pm_hist={_fmt_ts(last_flush_ts.get('pm_history'))}"
        )
        text = (
            "📊 采集器状态\n"
            f"运行时间: {uptime/3600:.2f} 小时\n"
            f"今日行数: agg={today_rows.get('agg',0)} force={today_rows.get('force',0)} depth={today_rows.get('depth',0)} 5m={today_rows.get('binance_5m',0)} "
            f"pm_trades={today_rows.get('pm_trades',0)} pm_mid={today_rows.get('pm_midpoint',0)} pm_ob={today_rows.get('pm_orderbook',0)} pm_hist={today_rows.get('pm_history',0)}\n"
            f"实时入队: {enq_text}\n"
            f"最近落盘: {flush_text}\n"
            f"Queue 积压: {q_sizes}\n"
            f"最近自愈: {heal_str}\n"
            f"暂停: {is_paused}\n"
            f"---\n{pm_status}"
        )
        try:
            await update.message.reply_text(text)
        except Exception:
            pass

    async def pause_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        global is_paused
        is_paused = True
        await update.message.reply_text("⏸ 已暂停：各流不再压入队列")

    async def resume_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        global is_paused
        is_paused = False
        await update.message.reply_text("▶ 已恢复压入队列")

    async def stop_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        await update.message.reply_text("🛑 正在优雅退出…")
        shutdown_event.set()

    cmd_filter = admin_filter or filters.ALL
    app.add_handler(CommandHandler("status", status_cmd, filters=cmd_filter))
    app.add_handler(CommandHandler("pause", pause_cmd, filters=cmd_filter))
    app.add_handler(CommandHandler("resume", resume_cmd, filters=cmd_filter))
    app.add_handler(CommandHandler("stop", stop_cmd, filters=cmd_filter))
    await app.initialize()
    await app.start()
    polling_task = asyncio.create_task(
        app.updater.start_polling(drop_pending_updates=True, timeout=55)
    )
    return polling_task, app


# ---------------------------------------------------------------------------
# 优雅退出：停止 Producer → 投毒丸 → 等待 Writer 刷盘 → 发 Telegram
# ---------------------------------------------------------------------------
async def main() -> None:
    global start_time, alert_sender, queues
    start_time = time.time()
    alert_sender = send_alert

    DATA_DIR.mkdir(parents=True, exist_ok=True)
    queues = {
        "agg": asyncio.Queue(maxsize=100_000),
        "force": asyncio.Queue(maxsize=50_000),
        "depth": asyncio.Queue(maxsize=50_000),
        "binance_5m": asyncio.Queue(maxsize=10_000),
        "pm_trades": asyncio.Queue(maxsize=50_000),
        "pm_midpoint": asyncio.Queue(maxsize=50_000),
        "pm_orderbook": asyncio.Queue(maxsize=20_000),
        "pm_history": asyncio.Queue(maxsize=10_000),
    }

    # 启动 8 个 Writer（Binance 4 + PM 4）
    writers = [
        asyncio.create_task(writer_worker("agg", queues["agg"], "btc_1s", AGG_COLUMNS, "agg")),
        asyncio.create_task(writer_worker("force", queues["force"], "btc_force", FORCE_COLUMNS, "force")),
        asyncio.create_task(writer_worker("depth", queues["depth"], "btc_depth", DEPTH_COLUMNS, "depth")),
        asyncio.create_task(writer_worker("binance_5m", queues["binance_5m"], "binance_5m", BINANCE_5M_COLUMNS, "binance_5m")),
        asyncio.create_task(writer_worker("pm_trades", queues["pm_trades"], "pm_trades", PM_TRADES_COLUMNS, "pm_trades")),
        asyncio.create_task(writer_worker("pm_midpoint", queues["pm_midpoint"], "pm_midpoint", PM_MIDPOINT_COLUMNS, "pm_midpoint")),
        asyncio.create_task(writer_worker("pm_orderbook", queues["pm_orderbook"], "pm_orderbook", PM_ORDERBOOK_COLUMNS, "pm_orderbook")),
        asyncio.create_task(writer_worker("pm_history", queues["pm_history"], "pm_history", PM_HISTORY_COLUMNS, "pm_history")),
    ]

    # 启动 Producer：Binance 4 + PM 路由 + PM 逐笔 + PM midpoint(实时价) + PM 订单簿 + PM 历史
    producers = [
        asyncio.create_task(producer_agg()),
        asyncio.create_task(producer_force()),
        asyncio.create_task(producer_depth()),
        asyncio.create_task(producer_binance_5m()),
        asyncio.create_task(pm_market_router()),
        asyncio.create_task(pm_trades_poller()),
        asyncio.create_task(pm_midpoint_poller()),
        asyncio.create_task(pm_orderbook_dual_track()),
        asyncio.create_task(pm_history_poller()),
    ]

    # Telegram Bot
    bot_task, bot_app = await run_telegram_bot()

    # 信号处理：SIGINT/SIGTERM 触发优雅退出（Unix；Windows 下 Ctrl+C 会抛 CancelledError）
    def do_shutdown():
        shutdown_event.set()

    try:
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            loop.add_signal_handler(sig, do_shutdown)
    except (NotImplementedError, OSError):
        signal.signal(signal.SIGINT, lambda *a: shutdown_event.set())
        signal.signal(signal.SIGTERM, lambda *a: shutdown_event.set())

    await shutdown_event.wait()

    # 取消所有 Producer
    for t in producers:
        t.cancel()
        try:
            await t
        except asyncio.CancelledError:
            pass
    if bot_task and bot_app:
        try:
            await bot_app.updater.stop()
        except Exception:
            pass
        bot_task.cancel()
        try:
            await bot_task
        except asyncio.CancelledError:
            pass

    # 向每个 Queue 投毒丸，让 Writer 退出并刷盘
    for q in queues.values():
        await q.put(SENTINEL)
    await asyncio.gather(*writers)

    await send_alert("✅ 数据已全部安全落盘，采集器已关机。")
    print("Bye.")


if __name__ == "__main__":
    asyncio.run(main())
