#!/usr/bin/env python3
"""
卖方策略 (Seller / Contrarian) — 实盘版
=========================================
当某一方 (YES/NO) 价格跌到低位 (如 ≤0.20) 时，
买入反向 (即高价方, ~$0.80)，等待结算。

等价于传统期权的"卖出虚值期权":
  - 胜率高 (~82%)，每次赚小钱 (~$0.20/share)
  - 偶尔反转亏大钱 (~$0.80/share)
  - 凯利值 ~4%，正期望策略

与 trend_live.py 的区别:
  - trend_live: 跟随趋势，买>50%的那侧
  - smart_seller_live: 逆势卖方，当一侧跌到低位时买反向(相当于卖低价方)

核心逻辑:
  1. 持续监控 15 分钟市场的 YES/NO midpoint
  2. 当 YES ≤ threshold → 买入 NO (认为 YES 会归零)
  3. 当 NO ≤ threshold → 买入 YES (认为 NO 会归零)
  4. 使用限价单 (Maker免手续费)
  5. 持有到结算
  6. 支持多资产同时监控 (BTC/ETH/SOL/XRP)

回测表现 (536个15分钟市场, 阈值0.20):
  - 胜率: 82.5%
  - 凯利值: 4.31% (正期望)
  - mid阶段入场胜率: 83.8%

用法:
  source venv/bin/activate

  # Dry-run 测试
  python3 smart_seller_live.py --duration 2h

  # 实盘 $2 (小资金测试)
  python3 smart_seller_live.py --live --amount 2 --duration 4h

  # 多资产同时跑
  python3 smart_seller_live.py --live --amount 2 --assets btc,eth,sol,xrp

  # 保守模式: 只在mid阶段入场, 阈值0.15
  python3 smart_seller_live.py --live --amount 2 --threshold 0.15 --phase mid_only
"""

import asyncio
import aiohttp
import json
import time
import argparse
import signal
import os
import traceback
from datetime import datetime, timezone
from typing import Optional, Dict, List, Tuple
from pathlib import Path
from dotenv import load_dotenv

# 本项目：trade 为子目录，项目根为 BTC_backtest
TRADE_DIR = Path(__file__).resolve().parent.parent.parent
PROJECT_ROOT = TRADE_DIR.parent
import sys
sys.path.insert(0, str(TRADE_DIR))
from utils.db import get_conn, release_conn, execute as db_execute, init_smart_seller_tables

# 配置：仅从项目根 .env 加载
load_dotenv(PROJECT_ROOT / ".env")

TAKER_FEE = 0.02
MAKER_FEE = 0.0
MIN_ORDER_SIZE = 5     # Polymarket 最低下单 5 shares
WINDOW_SEC = 900       # 15分钟
GAMMA_API = "https://gamma-api.polymarket.com/events"
CLOB_HOST = "https://clob.polymarket.com"
CLOB_MIDPOINT = f"{CLOB_HOST}/midpoint"
POLYGON_CHAIN_ID = 137

BINANCE_KLINES = "https://api.binance.com/api/v3/klines"
DERIBIT_DVOL = "https://www.deribit.com/api/v2/public/get_volatility_index_data"

CRYPTO_SYMBOLS = {
    "btc": "BTCUSDT", "eth": "ETHUSDT", "sol": "SOLUSDT",
    "xrp": "XRPUSDT", "doge": "DOGEUSDT",
}

DATA_DIR = PROJECT_ROOT / "data"
LOG_DIR = PROJECT_ROOT / "logs"
DATA_DIR.mkdir(parents=True, exist_ok=True)
LOG_DIR.mkdir(parents=True, exist_ok=True)


# ============================================================
# Monkey-patch py-clob-client HTTP 层
# ============================================================
def _patch_clob_http():
    try:
        import httpx
        from py_clob_client.http_helpers import helpers as _h
        _BROWSER_UA = (
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/131.0.0.0 Safari/537.36"
        )
        transport = httpx.HTTPTransport(
            http2=True, local_address="0.0.0.0", retries=1,
        )
        _h._http_client = httpx.Client(transport=transport, timeout=15.0)
        _orig = _h.overloadHeaders
        def _patched(method, headers):
            headers = _orig(method, headers)
            headers["User-Agent"] = _BROWSER_UA
            return headers
        _h.overloadHeaders = _patched
        print("  ✅ CLOB HTTP: 浏览器UA + IPv4 模式")
    except Exception as e:
        print(f"  ⚠️ HTTP patch 跳过: {e}")

_patch_clob_http()



# ============================================================
# 技术指标计算
# ============================================================

def calc_rsi(closes: list, period: int = 14) -> float:
    if len(closes) < period + 1:
        return 50.0
    gains, losses = [], []
    for i in range(-period, 0):
        d = closes[i] - closes[i - 1]
        gains.append(max(d, 0))
        losses.append(max(-d, 0))
    avg_gain = sum(gains) / period
    avg_loss = sum(losses) / period
    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    return 100.0 - 100.0 / (1.0 + rs)

def calc_bollinger(closes: list, period: int = 20, std_mult: float = 2.0) -> dict:
    if len(closes) < period:
        return {"upper": 0, "lower": 0, "mid": 0, "bandwidth": 0}
    window = closes[-period:]
    mid = sum(window) / period
    std = (sum((x - mid) ** 2 for x in window) / period) ** 0.5
    upper = mid + std_mult * std
    lower = mid - std_mult * std
    bandwidth = (upper - lower) / mid if mid > 0 else 0
    return {"upper": upper, "lower": lower, "mid": mid, "bandwidth": bandwidth, "std": std}

def calc_macd(closes: list, fast: int = 12, slow: int = 26, signal: int = 9) -> dict:
    if len(closes) < slow + signal:
        return {"dif": 0, "dea": 0, "hist": 0}
    def ema(data, period):
        k = 2.0 / (period + 1)
        result = [data[0]]
        for i in range(1, len(data)):
            result.append(data[i] * k + result[-1] * (1 - k))
        return result
    ema_fast = ema(closes, fast)
    ema_slow = ema(closes, slow)
    dif_values = [f - s for f, s in zip(ema_fast, ema_slow)]
    dea_values = ema(dif_values, signal)
    dif = dif_values[-1]
    dea = dea_values[-1]
    return {"dif": dif, "dea": dea, "hist": dif - dea}

# ============================================================
# 数据源: DVOL (Deribit)
# ============================================================

class DVOLTracker:
    def __init__(self):
        self.current_dvol = None
        self.dvol_1h_ago = None
        self.last_fetch = 0
        self.fetch_interval = 120

    async def update(self, session: aiohttp.ClientSession):
        now = time.time()
        if now - self.last_fetch < self.fetch_interval:
            return
        try:
            end_ts = int(now * 1000)
            start_ts = end_ts - 3600_000 * 2
            params = {
                "currency": "BTC",
                "start_timestamp": start_ts,
                "end_timestamp": end_ts,
                "resolution": "3600",
            }
            async with session.get(
                DERIBIT_DVOL, params=params,
                timeout=aiohttp.ClientTimeout(total=8),
            ) as resp:
                if resp.status != 200:
                    return
                data = await resp.json()
                result = data.get("result", {})
                dvol_data = result.get("data", [])
                if not dvol_data:
                    return
                self.current_dvol = dvol_data[-1][4]
                if len(dvol_data) >= 2:
                    self.dvol_1h_ago = dvol_data[-2][4]
                else:
                    self.dvol_1h_ago = self.current_dvol
                self.last_fetch = now
        except Exception:
            pass

    def is_rising(self) -> bool:
        if self.current_dvol is None or self.dvol_1h_ago is None:
            return False
        return self.current_dvol > self.dvol_1h_ago

    def get_regime(self) -> str:
        if self.current_dvol is None:
            return "normal"
        if self.current_dvol > 65:
            return "high_vol"
        elif self.current_dvol < 50:
            return "low_vol"
        return "normal"

    def status_str(self) -> str:
        dvol = f"{self.current_dvol:.1f}" if self.current_dvol else "N/A"
        regime = self.get_regime()
        arrow = "↑" if self.is_rising() else "↓"
        return f"DVOL={dvol}{arrow} [{regime}]"

# ============================================================
# 数据源: Binance 技术指标
# ============================================================

class BinanceIndicators:
    def __init__(self, asset: str = "btc"):
        self.symbol = CRYPTO_SYMBOLS.get(asset.lower(), f"{asset.upper()}USDT")
        self.last_fetch = 0
        self.fetch_interval = 30
        self.rsi_1m = 50.0
        self.bb_5m = {}
        self.macd_5m = {}
        self._prev_bandwidth = 0

    async def update(self, session: aiohttp.ClientSession):
        now = time.time()
        if now - self.last_fetch < self.fetch_interval:
            return
        try:
            tasks = [
                self._fetch_klines(session, "1m", 30),
                self._fetch_klines(session, "5m", 40),
            ]
            results = await asyncio.gather(*tasks, return_exceptions=True)
            klines_1m = results[0] if not isinstance(results[0], Exception) else []
            klines_5m = results[1] if not isinstance(results[1], Exception) else []

            if klines_1m:
                closes_1m = [float(k[4]) for k in klines_1m]
                self.rsi_1m = calc_rsi(closes_1m, 14)

            if klines_5m:
                closes_5m = [float(k[4]) for k in klines_5m]
                self.bb_5m = calc_bollinger(closes_5m, 20, 2)
                self.macd_5m = calc_macd(closes_5m, 12, 26, 9)
                new_bw = self.bb_5m.get("bandwidth", 0)
                self._prev_bandwidth = new_bw

            self.last_fetch = now
        except Exception:
            pass

    async def _fetch_klines(self, session: aiohttp.ClientSession, interval: str, limit: int) -> list:
        async with session.get(
            BINANCE_KLINES,
            params={"symbol": self.symbol, "interval": interval, "limit": limit},
            timeout=aiohttp.ClientTimeout(total=5),
        ) as resp:
            if resp.status == 200:
                return await resp.json()
        return []

    def bb_expanding(self) -> bool:
        return self.bb_5m.get("bandwidth", 0) > self._prev_bandwidth

    def price_above_upper(self, price: float) -> bool:
        return price > self.bb_5m.get("upper", float('inf'))

    def status_str(self) -> str:
        rsi = f"RSI={self.rsi_1m:.0f}"
        bw = f"BB={self.bb_5m.get('bandwidth', 0):.4f}"
        dif = f"DIF={self.macd_5m.get('dif', 0):.2f}"
        return f"{rsi} {bw} {dif}"


# ============================================================
# 大趋势追踪器 (Binance K线 + 近期结算统计)
# ============================================================

class MacroTrendTracker:
    """
    通过外部价格API + 近期结算统计判断资产大方向。
    避免逆势交易 (如大盘跌时买YES, 大盘涨时买NO)。

    改进 v2 (2026-02):
    1. 阈值大幅提高: 1h≥0.3%, 4h≥0.8% 才算趋势 (原0.08%/0.1%太敏感)
    2. 加入日线(24h)K线确认大方向
    3. require_confluence=True: 价格趋势 + 结算结果偏向必须双重对齐才发 bias
       → 单独价格信号或单独结算偏向均不足以触发 NO_ONLY/YES_ONLY
    """

    CRYPTO_SYMBOLS = {
        "btc": "BTCUSDT", "eth": "ETHUSDT", "xrp": "XRPUSDT",
        "sol": "SOLUSDT", "doge": "DOGEUSDT", "matic": "MATICUSDT",
        "ada": "ADAUSDT", "link": "LINKUSDT", "avax": "AVAXUSDT",
    }
    BINANCE_KLINES = "https://api.binance.com/api/v3/klines"

    def __init__(self, asset: str, lookback: int = 6,
                 price_threshold_1h: float = 0.003,   # 提高: 0.08% → 0.3%
                 price_threshold_4h: float = 0.008,   # 提高: 0.10% → 0.8%
                 price_threshold_1d: float = 0.015,   # 新增: 日线阈值 1.5%
                 require_confluence: bool = True):     # 新增: 双重确认
        self.asset = asset.lower()
        self.symbol = self.CRYPTO_SYMBOLS.get(
            self.asset, f"{self.asset.upper()}USDT")
        self.lookback = lookback
        self.price_threshold_1h = price_threshold_1h
        self.price_threshold_4h = price_threshold_4h
        self.price_threshold_1d = price_threshold_1d
        self.require_confluence = require_confluence

        # 近期结算结果 (滚动)
        self.outcomes: List[str] = []

        # 外部价格数据
        self.price_trend: Optional[str] = None   # "UP" / "DOWN" / None
        self.price_trend_1d: Optional[str] = None  # 日线趋势
        self.price_change_1h: float = 0.0
        self.price_change_4h: float = 0.0
        self.price_change_1d: float = 0.0
        self.last_price_fetch: float = 0
        self.fetch_interval: float = 180  # 每 3 分钟刷新一次
        self.enabled: bool = True

    # ---------- 数据更新 ----------

    def add_outcome(self, outcome: str):
        """添加一期结算结果"""
        self.outcomes.append(outcome)
        if len(self.outcomes) > 50:
            self.outcomes = self.outcomes[-30:]

    async def update(self, session: aiohttp.ClientSession):
        """从 Binance 获取 1h + 1d K 线并计算价格变化"""
        if not self.enabled:
            return
        now = time.time()
        if now - self.last_price_fetch < self.fetch_interval:
            return
        try:
            # ── 1h K线 (5根 ≈ 4小时) ─────────────────────────
            async with session.get(
                self.BINANCE_KLINES,
                params={"symbol": self.symbol, "interval": "1h", "limit": 5},
                timeout=aiohttp.ClientTimeout(total=5),
            ) as resp:
                if resp.status != 200:
                    return
                klines = await resp.json()
                if not klines or len(klines) < 2:
                    return

                price_now = float(klines[-1][4])   # 最新收盘价
                price_1h  = float(klines[-2][1])   # 上一根K线开盘价
                price_4h  = float(klines[0][1])    # 最早K线开盘价

                self.price_change_1h = (price_now - price_1h) / price_1h
                self.price_change_4h = (price_now - price_4h) / price_4h

                # 短期趋势: 1h 和 4h 同向且超过阈值
                if (self.price_change_1h < -self.price_threshold_1h
                        and self.price_change_4h < -self.price_threshold_4h):
                    self.price_trend = "DOWN"
                elif (self.price_change_1h > self.price_threshold_1h
                      and self.price_change_4h > self.price_threshold_4h):
                    self.price_trend = "UP"
                else:
                    self.price_trend = None

            # ── 1d K线 (3根 ≈ 3天) ───────────────────────────
            async with session.get(
                self.BINANCE_KLINES,
                params={"symbol": self.symbol, "interval": "1d", "limit": 3},
                timeout=aiohttp.ClientTimeout(total=5),
            ) as resp_1d:
                if resp_1d.status == 200:
                    klines_1d = await resp_1d.json()
                    if klines_1d and len(klines_1d) >= 2:
                        p_now_1d = float(klines_1d[-1][4])   # 今日收盘
                        p_prev_1d = float(klines_1d[0][1])   # 前天开盘
                        self.price_change_1d = (p_now_1d - p_prev_1d) / p_prev_1d
                        if self.price_change_1d < -self.price_threshold_1d:
                            self.price_trend_1d = "DOWN"
                        elif self.price_change_1d > self.price_threshold_1d:
                            self.price_trend_1d = "UP"
                        else:
                            self.price_trend_1d = None

            self.last_price_fetch = now
        except Exception:
            pass  # 静默，沿用上次趋势

    # ---------- 偏向判断 ----------

    def get_outcome_bias(self) -> Optional[str]:
        """近期结算 >65% 一致时返回 UP/DOWN"""
        recent = self.outcomes[-self.lookback:]
        if len(recent) < 3:
            return None
        yes_pct = sum(1 for o in recent if o == "YES") / len(recent)
        if yes_pct >= 0.65:
            return "UP"
        elif yes_pct <= 0.35:
            return "DOWN"
        return None

    def get_bias(self) -> Tuple[Optional[str], dict]:
        """
        综合判断，返回 (allowed_side, info_dict)
        allowed_side: "YES_ONLY" / "NO_ONLY" / None (不限)

        require_confluence=True (默认):
          必须 短期价格趋势(1h+4h) AND 日线趋势(1d) 同向，才触发强 bias。
          若日线方向未知，则退回到单独使用短期趋势 + 结算偏向的弱信号。

        require_confluence=False (旧行为):
          价格趋势优先，其次结算偏向，任一满足即触发。
        """
        outcome_bias = self.get_outcome_bias()
        price_bias = self.price_trend        # 短期 (1h+4h)
        daily_bias = self.price_trend_1d     # 日线 (1d)

        info = {
            "outcome_bias": outcome_bias,
            "price_trend": price_bias,
            "daily_trend": daily_bias,
            "price_1h": f"{self.price_change_1h:+.4%}",
            "price_4h": f"{self.price_change_4h:+.4%}",
            "price_1d": f"{self.price_change_1d:+.4%}",
            "recent_outcomes": self.outcomes[-self.lookback:],
        }

        if self.require_confluence:
            # ── 强信号: 短期 + 日线 + 结算 三者对齐 ───────────
            # 优先级最高: 短期价格与日线都看跌/涨
            if price_bias == "DOWN" and daily_bias == "DOWN":
                return "NO_ONLY", info
            if price_bias == "UP" and daily_bias == "UP":
                return "YES_ONLY", info

            # 次级信号: 短期价格与结算偏向对齐 (日线中性不确定)
            if price_bias == "DOWN" and outcome_bias == "DOWN":
                return "NO_ONLY", info
            if price_bias == "UP" and outcome_bias == "UP":
                return "YES_ONLY", info

            # 日线与结算偏向对齐 (短期价格震荡)
            if daily_bias == "DOWN" and outcome_bias == "DOWN":
                return "NO_ONLY", info
            if daily_bias == "UP" and outcome_bias == "UP":
                return "YES_ONLY", info

            # 任何单独信号都不足以触发 bias → 中性
            return None, info
        else:
            # ── 旧行为 (向后兼容) ───────────────────────────
            if price_bias == "DOWN":
                return "NO_ONLY", info
            if price_bias == "UP":
                return "YES_ONLY", info
            if outcome_bias == "DOWN":
                return "NO_ONLY", info
            if outcome_bias == "UP":
                return "YES_ONLY", info
            return None, info

    def trend_str(self) -> str:
        """简短趋势摘要"""
        bias, _ = self.get_bias()
        p = (f"1h:{self.price_change_1h:+.2%} "
             f"4h:{self.price_change_4h:+.2%} "
             f"1d:{self.price_change_1d:+.2%}")
        recent = self.outcomes[-self.lookback:]
        yes_n = sum(1 for o in recent if o == "YES")
        no_n = len(recent) - yes_n
        o = f"近{len(recent)}期:{yes_n}Y/{no_n}N"
        tag = {"YES_ONLY": "🟢看涨→只买YES",
               "NO_ONLY":  "🔴看跌→只买NO"}.get(bias, "⚪中性→双向")
        return f"{tag} ({p} | {o})"


# ============================================================
# 交易客户端 (复用 trend_live.py 的 TradingClient)
# ============================================================

class TradingClient:
    """封装 py-clob-client, 处理认证和下单"""

    def __init__(self, dry_run: bool = True):
        self.dry_run = dry_run
        self.client = None
        self.address = None
        self._api_creds = None
        if not dry_run:
            self._init_clob_client()

    def _init_clob_client(self):
        from py_clob_client.client import ClobClient
        from py_clob_client.clob_types import ApiCreds

        private_key = os.getenv("PRIVATE_KEY")
        if not private_key or private_key.startswith("0x000000000"):
            raise ValueError(
                "❌ 请在 .env 中设置真实的 PRIVATE_KEY!\n"
                "   当前值是占位符，无法进行实盘交易。"
            )
        proxy_addr = os.getenv('PROXY_ADDRESS') or os.getenv('WALLET_ADDRESS')
        self.client = ClobClient(
            host=CLOB_HOST, chain_id=POLYGON_CHAIN_ID,
            key=private_key, signature_type=2, funder=proxy_addr,
        )
        self.address = self.client.get_address()
        print(f"  钱包地址: {self.address}")
        print(f"  Proxy地址: {proxy_addr}")

        print("  获取 API credentials...")
        self._api_creds = self.client.create_or_derive_api_creds()
        self.client.set_api_creds(self._api_creds)
        print(f"  API Key: {self._api_creds.api_key[:8]}...")

        ok = self.client.get_ok()
        print(f"  API 连接: {'✅ OK' if ok == 'OK' else '❌ FAIL: ' + str(ok)}")

    def get_balance(self) -> Optional[dict]:
        if self.dry_run or not self.client:
            return {"balance": "dry-run", "allowance": "dry-run"}
        try:
            from py_clob_client.clob_types import BalanceAllowanceParams, AssetType
            params = BalanceAllowanceParams(
                asset_type=AssetType.COLLATERAL, signature_type=2,
            )
            result = self.client.get_balance_allowance(params)
            raw = int(result.get('balance', '0'))
            return {"balance_usdc": raw / 1e6, "raw": result}
        except Exception as e:
            print(f"  ⚠️ 获取余额失败: {e}")
            return None

    def place_limit_buy(self, token_id: str, price: float,
                        size: float) -> dict:
        if self.dry_run:
            return {
                "success": True,
                "order_id": f"dry-{int(time.time())}",
                "detail": f"dry-run limit buy {size:.2f}shares @{price:.3f}",
                "mode": "dry-run",
            }
        try:
            from py_clob_client.clob_types import (
                OrderArgs, PartialCreateOrderOptions
            )
            neg_risk = self.client.get_neg_risk(token_id)
            tick_size = self.client.get_tick_size(token_id)
            order_args = OrderArgs(
                token_id=token_id, price=price, size=size, side="BUY",
            )
            options = PartialCreateOrderOptions(
                tick_size=tick_size, neg_risk=neg_risk,
            )
            resp = self.client.create_and_post_order(order_args, options)
            return {
                "success": resp.get("success", False) if isinstance(resp, dict) else True,
                "order_id": resp.get("orderID", "") if isinstance(resp, dict) else str(resp),
                "detail": json.dumps(resp) if isinstance(resp, dict) else str(resp),
                "mode": "live",
            }
        except Exception as e:
            return {
                "success": False, "order_id": "",
                "detail": f"ERROR: {e}\n{traceback.format_exc()}",
                "mode": "live",
            }

    def get_order_status(self, order_id: str) -> Optional[dict]:
        if self.dry_run:
            return {"filled": True, "cancelled": False, "status": "MATCHED",
                    "avg_price": 0, "size_matched": 0, "fill_pct": 1.0}
        if not self.client or not order_id:
            return None
        try:
            resp = self.client.get_order(order_id)
            if not resp:
                return None
            status = resp.get("status", "")
            original_size = float(resp.get("original_size", "0") or "0")
            size_matched = float(resp.get("size_matched", "0") or "0")
            fill_pct = size_matched / original_size if original_size > 0 else 0
            avg_price = float(resp.get("price", "0") or "0")
            return {
                "filled": status == "MATCHED",
                "cancelled": status == "CANCELLED",
                "status": status,
                "size_matched": size_matched,
                "original_size": original_size,
                "fill_pct": fill_pct,
                "avg_price": avg_price,
                "raw": resp,
            }
        except Exception as e:
            print(f"  ⚠️ 查询订单状态失败: {e}")
            return None

    def place_limit_sell(self, token_id: str, price: float,
                         size: float) -> dict:
        """限价卖出 (止损时使用)"""
        if self.dry_run:
            return {
                "success": True,
                "order_id": f"dry-sell-{int(time.time())}",
                "detail": f"dry-run limit sell {size:.2f}shares @{price:.3f}",
                "mode": "dry-run",
            }
        try:
            from py_clob_client.clob_types import (
                OrderArgs, PartialCreateOrderOptions
            )
            neg_risk = self.client.get_neg_risk(token_id)
            tick_size = self.client.get_tick_size(token_id)
            order_args = OrderArgs(
                token_id=token_id, price=price, size=size, side="SELL",
            )
            options = PartialCreateOrderOptions(
                tick_size=tick_size, neg_risk=neg_risk,
            )
            resp = self.client.create_and_post_order(order_args, options)
            return {
                "success": resp.get("success", False) if isinstance(resp, dict) else True,
                "order_id": resp.get("orderID", "") if isinstance(resp, dict) else str(resp),
                "detail": json.dumps(resp) if isinstance(resp, dict) else str(resp),
                "mode": "live",
            }
        except Exception as e:
            return {
                "success": False, "order_id": "",
                "detail": f"ERROR: {e}\n{traceback.format_exc()}",
                "mode": "live",
            }

    def cancel_order(self, order_id: str) -> bool:
        if self.dry_run or not self.client or not order_id:
            return True
        try:
            self.client.cancel(order_id)
            return True
        except Exception as e:
            print(f"  ⚠️ 取消订单失败: {e}")
            return False


# ============================================================
# 数据库 (MySQL via utils.db)
# ============================================================

def init_db():
    """初始化MySQL连接并创建表"""
    conn = get_conn()
    init_smart_seller_tables(conn)
    return conn


# ============================================================
# 卖方策略
# ============================================================

class SmartSellerStrategy:
    """
    卖方逻辑:
    1. 监控多个资产的 15 分钟市场
    2. 当 YES ≤ threshold → 买 NO @(1-YES)
       当 NO ≤ threshold → 买 YES @(1-NO)
    3. 限价单入场 (Maker免手续费)
    4. 持有到结算

    这等价于"卖出低价方"收权利金。
    """

    def __init__(self,
                 threshold: float = 0.20,
                 min_threshold: float = 0.05,
                 bet_amount: float = 2.0,
                 fixed_shares: float = 0.0,    # >0 时固定每笔份额数, 忽略 bet_amount
                 max_trades: int = 0,
                 max_loss: float = 0,
                 limit_offset: float = 0.01,
                 reprice_gap: float = 0.02,
                 assets: List[str] = None,
                 phase_filter: str = "mid",     # all / mid / late  (默认mid: 禁止close阶段)
                 min_elapsed_pct: float = 0.333,  # 入场窗口开始: 占窗口百分比 (300/900≈33%)
                 max_elapsed_pct: float = 0.667,  # 入场窗口结束: 占窗口百分比 (600/900≈67%)
                 max_buy_price: float = 0.85,   # 最高买入价, 超过跳过 (0.85+实盘PnL下降)
                 sl_trigger: float = 0.0,      # 止损触发: 低价方涨回此值时平仓 (0=不止损)
                 sl_confirm: int = 3,           # 止损确认: 连续N次检测才触发 (防正常波动)
                 sl_fast_track: float = 0.0,   # 极端行情: 低价方≥此值立即止损, 跳过确认
                 cooldown_losses: int = 3,      # 连续N次止损后暂停交易1轮
                 max_sl_loss: float = 0.20,     # 单笔止损最大亏损比例 (超过视为滑点过大)
                 require_bias_for_no: bool = False,  # 买NO需要明确NO_ONLY偏向 (过滤无偏向时的买NO)
                 night_skip_start: int = -1,    # 夜间跳过开始(UTC小时, -1=不限), 如16=CST00:00
                 night_skip_end: int = -1,      # 夜间跳过结束(UTC小时, -1=不限), 如24=CST08:00
                 tp_trigger: float = 0.0,       # 止盈触发: 低价方跌到此值时提前卖出 (0=不止盈)
                 tp_confirm: int = 2,           # 止盈确认: 连续N次检测才触发 (防止瞬时错误)
                 tp_offset: float = 0.01,       # 止盈卖价偏移: 略低于高价方mid (提高成交率)
                 dry_run: bool = True):

        self.threshold = threshold
        self.min_threshold = min_threshold
        self.bet_amount = bet_amount
        self.fixed_shares = fixed_shares      # 固定份额模式
        self.max_trades = max_trades
        self.max_loss = max_loss
        self.limit_offset = limit_offset
        self.reprice_gap = reprice_gap
        self.assets = [a.lower() for a in (assets or ["btc"])]
        self.phase_filter = phase_filter.lower()
        self.min_elapsed_pct = min_elapsed_pct
        self.max_elapsed_pct = max_elapsed_pct  # 入场窗口占窗口百分比
        self.max_buy_price = max_buy_price      # 最高买入价
        self.sl_trigger = sl_trigger            # 止损触发价 (低价方)
        self.sl_confirm = max(sl_confirm, 1)    # 至少1次
        self.sl_fast_track = sl_fast_track      # 极端行情立即止损阈值
        self.cooldown_losses = cooldown_losses  # 连续止损暂停阈值
        self.max_sl_loss = max_sl_loss          # 单笔止损最大容许亏损比例
        self.require_bias_for_no = require_bias_for_no  # 买NO需要明确NO_ONLY偏向
        self.night_skip_start = night_skip_start        # 夜间跳过 UTC起始小时
        self.night_skip_end = night_skip_end            # 夜间跳过 UTC结束小时
        self.tp_trigger = tp_trigger            # 止盈触发价 (低价方)
        self.tp_confirm = max(tp_confirm, 1)    # 止盈确认次数
        self.tp_offset = tp_offset              # 止盈卖价偏移
        self.window = WINDOW_SEC
        self.dry_run = dry_run

        # 止损后禁止同市场重入
        self.stopped_slugs: set = set()
        # 连续止损冷却
        self.consecutive_sl = 0
        self.cooldown_until = 0.0  # time.time() 截止时间

        # 交易客户端
        self.trading = TradingClient(dry_run=dry_run)

        # 大趋势追踪器
        self.trackers = {a: MacroTrendTracker(a) for a in self.assets}
        
        # 微观动量与波动率追踪器
        self.indicators = {a: BinanceIndicators(a) for a in self.assets}
        self.dvol_tracker = DVOLTracker()

        # 数据库 (MySQL)
        self.db = init_db()

        params = json.dumps({
            "threshold": threshold,
            "min_threshold": min_threshold,
            "bet_amount": bet_amount,
            "fixed_shares": fixed_shares,
            "max_trades": max_trades,
            "max_loss": max_loss,
            "limit_offset": limit_offset,
            "reprice_gap": reprice_gap,
            "assets": self.assets,
            "phase_filter": phase_filter,
            "min_elapsed_pct": min_elapsed_pct,
            "max_elapsed_pct": max_elapsed_pct,
            "max_buy_price": max_buy_price,
            "sl_trigger": sl_trigger,
            "sl_confirm": sl_confirm,
            "sl_fast_track": sl_fast_track,
            "cooldown_losses": cooldown_losses,
            "max_sl_loss": max_sl_loss,
            "require_bias_for_no": require_bias_for_no,
            "night_skip_start": night_skip_start,
            "night_skip_end": night_skip_end,
            "tp_trigger": tp_trigger,
            "tp_confirm": tp_confirm,
            "tp_offset": tp_offset,
            "dry_run": dry_run,
        })
        cur = db_execute(self.db,
            "INSERT INTO sessions (mode, params) VALUES (%s, %s)",
            ("dry-run" if dry_run else "live", params)
        )
        self.session_id = cur.lastrowid

        self.http_session: Optional[aiohttp.ClientSession] = None

        # slug → market state
        self.markets: Dict[str, dict] = {}

        # 统计
        self.trade_count = 0
        self.total_pnl = 0.0
        self.wins = 0
        self.losses = 0
        self.running = True
        self.start_time = time.time()

    # ------ 日志 ------

    def _log(self, action: str, slug: str = "", asset: str = "",
             detail: dict = None, level: str = "info"):
        detail_json = json.dumps(detail or {}, ensure_ascii=False)
        db_execute(self.db,
            "INSERT INTO action_log (session_id, asset, market_slug, action, detail, level) "
            "VALUES (%s, %s, %s, %s, %s, %s)",
            (self.session_id, asset, slug, action, detail_json, level)
        )
        ts = datetime.now().strftime("%H:%M:%S")
        mode = "🏷DRY" if self.dry_run else "💰LIVE"
        lvl = {"info": "ℹ️", "warn": "⚠️", "error": "❌"}.get(level, "")
        msg = detail.get("msg", action) if detail else action
        asset_tag = f"[{asset.upper()}]" if asset else ""
        print(f"  [{ts}] {mode} {lvl} {asset_tag} {msg}")

    # ------ HTTP helpers ------

    async def _get_json(self, url: str, params: dict = None):
        try:
            async with self.http_session.get(
                url, params=params,
                timeout=aiohttp.ClientTimeout(total=8)
            ) as resp:
                if resp.status == 200:
                    return await resp.json()
        except Exception as e:
            pass
        return None

    async def fetch_midpoint(self, token_id: str) -> Optional[float]:
        data = await self._get_json(CLOB_MIDPOINT, {"token_id": token_id})
        if data and "mid" in data:
            try:
                raw = str(data["mid"])
                # 清理非数字/非小数点的垃圾字节
                cleaned = ''.join(c for c in raw if c.isdigit() or c == '.')
                if cleaned:
                    val = float(cleaned)
                    if 0 <= val <= 1:
                        return val
            except (ValueError, TypeError):
                pass
        return None

    # ------ 市场发现 ------

    async def discover_markets(self) -> List[dict]:
        """发现所有资产当前epoch的市场"""
        now_ts = int(time.time())
        base_epoch = (now_ts // self.window) * self.window
        found = []

        for asset in self.assets:
            for offset in [0, 1]:
                epoch = base_epoch + offset * self.window
                slug = f"{asset}-updown-15m-{epoch}"

                if slug in self.markets:
                    continue

                elapsed = now_ts - epoch
                if elapsed < -30 or elapsed > self.window + 60:
                    continue

                data = await self._get_json(GAMMA_API, {"slug": slug})
                if not data:
                    continue
                events = data if isinstance(data, list) else [data]
                if not events:
                    continue
                event = events[0]
                if event.get("closed"):
                    continue

                markets_list = event.get("markets", [])
                if not markets_list:
                    continue
                mkt = markets_list[0]
                tokens = mkt.get("clobTokenIds", [])
                if isinstance(tokens, str):
                    tokens = json.loads(tokens)
                if not isinstance(tokens, list) or len(tokens) < 2:
                    continue

                found.append({
                    'slug': slug, 'epoch': epoch, 'asset': asset,
                    'yes_token': tokens[0], 'no_token': tokens[1],
                    'question': mkt.get("question", ""),
                })
        return found

    # ------ 阶段判断 ------

    def _get_phase(self, elapsed: float) -> str:
        """根据已过时间判断阶段"""
        if elapsed < 60:
            return "open"
        elif elapsed < self.window * 0.5:  # < 7.5 min
            return "mid"
        elif elapsed < self.window - 120:  # < 13 min
            return "late"
        else:
            return "close"

    def _phase_allowed(self, phase: str) -> bool:
        """检查当前阶段是否允许入场"""
        if self.phase_filter == "all":
            return True
        if self.phase_filter == "mid":
            return phase in ("mid", "late")
        if self.phase_filter == "late":
            return phase == "late"
        if self.phase_filter == "mid_only":
            return phase == "mid"
        return True

    # ------ 核心决策 ------

    def _check_trigger(self, yes_mid: float, no_mid: float,
                       elapsed: float, asset: str, slug: str) -> Optional[dict]:
        """
        检查是否触发卖方信号。

        返回:
          None — 不触发
          dict — 触发信号:
            trigger_side: 哪一侧跌破阈值 (YES or NO)
            trigger_price: 低价方的价格
            buy_side: 应该买的方向 (反向)
            buy_token_key: 'yes_token' or 'no_token'
            buy_price: 买入价 (midpoint)
            skip_reason: 跳过原因 (仅当返回None时内部使用)
        """
        # 夜间时段过滤: 静默不交易 (在最前面检查避免无谓 API 调用)
        if self.night_skip_start >= 0 and self.night_skip_end >= 0:
            utc_hour = datetime.now(timezone.utc).hour
            if self.night_skip_start <= self.night_skip_end:
                # 正常区间: e.g. 16~24
                in_night = self.night_skip_start <= utc_hour < self.night_skip_end
            else:
                # 跨午夜区间: e.g. 22~6
                in_night = utc_hour >= self.night_skip_start or utc_hour < self.night_skip_end
            if in_night:
                return None  # 夜间静默，不记录日志（高频触发）

        # 时间窗口过滤: 太早或太晚都不入场 (按窗口百分比)
        min_sec = self.window * self.min_elapsed_pct
        max_sec = self.window * self.max_elapsed_pct
        if elapsed < min_sec:
            return None
        if self.max_elapsed_pct > 0 and elapsed > max_sec:
            return None

        phase = self._get_phase(elapsed)
        if not self._phase_allowed(phase):
            return None

        # 获取大趋势
        tracker = self.trackers.get(asset)
        macro_bias, trend_info = tracker.get_bias() if tracker else (None, {})

        # 获取微观指标与波动率
        ind = self.indicators.get(asset)
        dvol_regime = self.dvol_tracker.get_regime()
        
        # 波动率过滤: 高波动率时暂停交易
        if dvol_regime == "high_vol":
            self._log("skip_high_vol", slug, asset, {"msg": f"⏭️ DVOL过高 ({self.dvol_tracker.current_dvol}), 暂停交易"})
            return None

        # YES 跌破阈值 → 卖 YES (= 买 NO)
        if self.min_threshold < yes_mid <= self.threshold:
            buy_price = no_mid
            # 买入价过高过滤: 避免高价入场 (实盘0.90+均PnL为负)
            if self.max_buy_price > 0 and buy_price > self.max_buy_price:
                self._log("skip_max_price", slug, asset, {"msg": f"⏭️ 买入价过高 ({buy_price:.3f} > {self.max_buy_price})"})
                return None
            # ★ 大趋势过滤: 信号方向与大趋势冲突时跳过
            if macro_bias == "YES_ONLY":
                self._log("skip_macro_bias", slug, asset, {"msg": f"⏭️ 大趋势看涨, 放弃买NO"})
                return None # 大趋势看涨，不买NO

            # ★ 买NO偏向过滤: 要求明确的看跌偏向才买NO (过滤无偏向时的买NO)
            if self.require_bias_for_no and macro_bias != "NO_ONLY":
                self._log("skip_no_requires_bias", slug, asset,
                          {"msg": f"⏭️ 买NO需要NO_ONLY偏向, 当前={macro_bias}, 跳过"})
                return None
                
            # ★ 微观动量过滤: 避免买入正在暴跌的资产
            # 买 NO 意味着看跌资产。如果资产正在暴涨 (RSI > 70 或 MACD 金叉)，则不买 NO
            if ind:
                if ind.rsi_1m > 70:
                    self._log("skip_micro_momentum", slug, asset, {"msg": f"⏭️ 1m RSI过高 ({ind.rsi_1m:.1f} > 70), 放弃买NO"})
                    return None
                if ind.macd_5m.get("hist", 0) > 0:
                    self._log("skip_micro_momentum", slug, asset, {"msg": f"⏭️ 5m MACD金叉, 放弃买NO"})
                    return None
                    
            return {
                "trigger_side": "YES",
                "trigger_price": yes_mid,
                "buy_side": "NO",
                "buy_token_key": "no_token",
                "buy_price": buy_price,
                "phase": phase,
                "macro_bias": macro_bias,
                "trend_info": trend_info,
            }

        # NO 跌破阈值 → 卖 NO (= 买 YES)
        if self.min_threshold < no_mid <= self.threshold:
            buy_price = yes_mid
            # 买入价过高过滤
            if self.max_buy_price > 0 and buy_price > self.max_buy_price:
                self._log("skip_max_price", slug, asset, {"msg": f"⏭️ 买入价过高 ({buy_price:.3f} > {self.max_buy_price})"})
                return None
            # ★ 大趋势过滤: 信号方向与大趋势冲突时跳过
            if macro_bias == "NO_ONLY":
                self._log("skip_macro_bias", slug, asset, {"msg": f"⏭️ 大趋势看跌, 放弃买YES"})
                return None # 大趋势看跌，不买YES
                
            # ★ 微观动量过滤: 避免买入正在暴跌的资产
            # 买 YES 意味着看涨资产。如果资产正在暴跌 (RSI < 30 或 MACD 死叉)，则不买 YES
            if ind:
                if ind.rsi_1m < 30:
                    self._log("skip_micro_momentum", slug, asset, {"msg": f"⏭️ 1m RSI过低 ({ind.rsi_1m:.1f} < 30), 放弃买YES"})
                    return None
                if ind.macd_5m.get("hist", 0) < 0:
                    self._log("skip_micro_momentum", slug, asset, {"msg": f"⏭️ 5m MACD死叉, 放弃买YES"})
                    return None
                    
            return {
                "trigger_side": "NO",
                "trigger_price": no_mid,
                "buy_side": "YES",
                "buy_token_key": "yes_token",
                "buy_price": buy_price,
                "phase": phase,
                "macro_bias": macro_bias,
                "trend_info": trend_info,
            }

        return None

    # ------ Gamma结算查询 ------

    async def _resolve_via_gamma(self, slug: str) -> Optional[str]:
        data = await self._get_json(GAMMA_API, {"slug": slug})
        if not data:
            return None
        events = data if isinstance(data, list) else [data]
        if not events:
            return None
        event = events[0]
        markets_list = event.get("markets", [])
        if not markets_list:
            return None
        mkt = markets_list[0]
        outcome = mkt.get("outcome")
        if outcome and isinstance(outcome, str) and outcome.lower() in ("yes", "no"):
            return outcome.upper()
        if event.get("closed") or mkt.get("closed"):
            prices = mkt.get("outcomePrices")
            if prices:
                try:
                    if isinstance(prices, str):
                        prices = json.loads(prices)
                    if isinstance(prices, list) and len(prices) >= 2:
                        yes_p = float(prices[0])
                        if yes_p > 0.85:
                            return "YES"
                        elif yes_p < 0.15:
                            return "NO"
                except (ValueError, IndexError, TypeError):
                    pass
        return None

    # ------ 结算 ------

    def _settle(self, slug: str, mkt: dict, outcome: str):
        buy_side = mkt.get('buy_side')
        buy_price = mkt.get('buy_price', 0)
        asset = mkt.get('asset', '')

        if asset in self.trackers:
            self.trackers[asset].add_outcome(outcome)

        if buy_side and buy_price > 0:
            shares = mkt.get('buy_shares', mkt.get('buy_amount', self.bet_amount) / buy_price)
            # 使用实际花费 (shares × buy_price) 而不是下单意图金额 (buy_amount)
            # 避免部分成交时 PnL 计算错误
            actual_cost = shares * buy_price
            pnl = (shares - actual_cost) if outcome == buy_side else -actual_cost
        else:
            pnl = 0

        mkt['status'] = 'settled'
        mkt['outcome'] = outcome
        mkt['pnl'] = pnl
        self.total_pnl += pnl

        if pnl >= 0:
            self.wins += 1
            self.consecutive_sl = 0  # 正常结算赢利, 重置连续止损计数
        else:
            self.losses += 1

        trigger_price = mkt.get('trigger_price', 0)
        icon = "✅" if pnl >= 0 else "❌"
        self._log("settle", slug, mkt.get('asset', ''), {
            "msg": f"{icon} 结算: 卖{mkt.get('trigger_side','')}@{trigger_price:.3f} "
                   f"(买{buy_side}@{buy_price:.3f}) →{outcome} PnL=${pnl:+.2f} "
                   f"累计${self.total_pnl:+.2f}",
            "outcome": outcome,
            "buy_side": buy_side,
            "buy_price": buy_price,
            "trigger_side": mkt.get('trigger_side'),
            "trigger_price": trigger_price,
            "pnl": pnl,
        })

        db_execute(self.db,
            "UPDATE smart_seller_trades SET "
            "outcome=%s, pnl=%s, settled_at=NOW(), "
            "settle_method='api_price', exit_type='settlement', "
            "status='settled', "
            "price_log=%s WHERE id=%s",
            (outcome, pnl, json.dumps(mkt.get('price_log', [])),
             mkt.get('db_id')))

    async def _execute_stop_loss(self, slug: str, mkt: dict,
                                low_side_price: float,
                                sl_reason: str = "normal"):
        """
        止损: 低价方涨回 sl_trigger → 卖出持有的高价方
        
        改进: 使用状态机模式, 防止重复挂单
        - 第一次触发: 挂限价卖单, 记录 sl_order_id, 进入 sl_pending 状态
        - 后续循环: 检查卖单是否成交, 而不是重复挂新单
        - 卖单未成交且价格恶化: 取消旧单, 用更低价重挂(最多重试sl_max_retries次)
        - 超过重试次数: 以极低价挂市价等效单兜底

        sl_reason: 'normal' (确认触发) / 'fast_track' (极端行情立即触发)
        """
        # 已放弃止损, 不再执行
        if mkt.get('sl_abandoned'):
            return

        buy_side = mkt.get('buy_side')
        buy_price = mkt.get('buy_price', 0)
        amount = mkt.get('buy_amount', self.bet_amount)
        shares = mkt.get('buy_shares', amount / buy_price if buy_price > 0 else 0)

        # 确定卖出的token
        if buy_side == 'NO':
            sell_token = mkt['no_token']
        else:
            sell_token = mkt['yes_token']

        # 首次止损前: 查询实际持仓份额, 用真实份额卖出
        if mkt.get('sl_retries', 0) == 0 and not mkt.get('sl_order_id') and not mkt.get('_sl_balance_checked'):
            try:
                from py_clob_client.clob_types import BalanceAllowanceParams, AssetType
                params = BalanceAllowanceParams(
                    asset_type=AssetType.CONDITIONAL, token_id=sell_token, signature_type=2,
                )
                bal_resp = self.trading.client.get_balance_allowance(params)
                real_balance = int(bal_resp.get('balance', '0')) / 1e6
                mkt['_sl_balance_checked'] = True
                if real_balance <= 0:
                    # 确实没有份额, 直接放弃
                    mkt['sl_abandoned'] = True
                    self._log("sl_no_shares", slug, mkt.get('asset', ''), {
                        "msg": f"🚫 持仓查询: 份额=0, 直接放弃止损",
                    }, level="error")
                    return
                elif abs(real_balance - shares) > 0.5:
                    self._log("sl_shares_mismatch", slug, mkt.get('asset', ''), {
                        "msg": f"⚠️ 持仓数量不一致: 记录={shares:.2f} 实际={real_balance:.2f}, 用实际值",
                    }, level="warn")
                    shares = real_balance
                    mkt['buy_shares'] = real_balance
            except Exception as e:
                mkt['_sl_balance_checked'] = True  # 查询失败也标记, 避免重复查
                self._log("sl_balance_query_fail", slug, mkt.get('asset', ''), {
                    "msg": f"⚠️ 持仓查询失败: {e}, 继续用记录值={shares:.2f}",
                }, level="warn")


        # -------- 已有止损挂单: 检查成交状态 --------
        if mkt.get('sl_order_id'):
            fill_info = self.trading.get_order_status(mkt['sl_order_id'])

            if fill_info and fill_info.get('filled'):
                # ✅ 止损单已成交
                avg_price = fill_info.get('avg_price', mkt.get('sl_sell_price', 0))
                if avg_price and avg_price > 0:
                    actual_sell_price = avg_price
                else:
                    actual_sell_price = mkt.get('sl_sell_price', 0)
                self._finalize_stop_loss(slug, mkt, actual_sell_price, sl_reason)
                return

            if fill_info and fill_info.get('cancelled'):
                # 被系统取消, 清除挂单状态, 下次重新挂
                mkt['sl_order_id'] = None
                mkt['sl_retries'] = mkt.get('sl_retries', 0) + 1
                self._log("sl_order_cancelled", slug, mkt.get('asset', ''), {
                    "msg": f"⚠️ 止损卖单被取消, 将重挂 (第{mkt['sl_retries']}次)",
                }, level="warn")
                # 继续往下重新挂单

            elif fill_info is None:
                # API查询失败, 增加计数器, 超过阈值则取消重挂
                mkt['sl_api_fails'] = mkt.get('sl_api_fails', 0) + 1
                if mkt['sl_api_fails'] >= 5:
                    self.trading.cancel_order(mkt['sl_order_id'])
                    mkt['sl_order_id'] = None
                    mkt['sl_api_fails'] = 0
                    mkt['sl_retries'] = mkt.get('sl_retries', 0) + 1
                    self._log("sl_api_timeout", slug, mkt.get('asset', ''), {
                        "msg": f"⚠️ 止损单状态查询连续{5}次失败, 取消重挂",
                    }, level="warn")
                    # 继续往下重新挂单
                else:
                    return  # 等下次再查

            else:
                # 仍在挂单中, 检查是否需要追价
                old_sell_price = mkt.get('sl_sell_price', 0)
                new_high_side = 1 - low_side_price
                new_sell_price = round(new_high_side - self.limit_offset, 4)
                new_sell_price = max(new_sell_price, 0.01)
                
                # 如果价格恶化超过 reprice_gap, 取消旧单追价
                if old_sell_price - new_sell_price >= self.reprice_gap:
                    self.trading.cancel_order(mkt['sl_order_id'])
                    # 检查取消前是否已成交
                    fill_check = self.trading.get_order_status(mkt['sl_order_id'])
                    if fill_check and fill_check.get('filled'):
                        avg_price = fill_check.get('avg_price', old_sell_price)
                        self._finalize_stop_loss(slug, mkt, avg_price if avg_price > 0 else old_sell_price, sl_reason)
                        return
                    mkt['sl_order_id'] = None
                    mkt['sl_retries'] = mkt.get('sl_retries', 0) + 1
                    self._log("sl_reprice", slug, mkt.get('asset', ''), {
                        "msg": f"🔄 止损追价: {old_sell_price:.3f}→{new_sell_price:.3f} "
                               f"(第{mkt['sl_retries']}次)",
                    })
                    # 继续往下重新挂单
                else:
                    # 价格变化不大, 继续等待成交
                    return

        # -------- 挂新的止损卖单 --------
        sl_retries = mkt.get('sl_retries', 0)
        sl_max_retries = 5       # 超过后用$0.01兜底
        sl_hard_limit = 10       # 绝对上限: 超过放弃止损, 等结算

        # 高价方当前价 ≈ 1 - low_side_price
        high_side_price = 1 - low_side_price

        if sl_retries >= sl_hard_limit:
            # 🚫 超过绝对上限: 放弃止损, 标记abandoned, 让结算逻辑接管
            mkt['sl_order_id'] = None
            mkt['sl_abandoned'] = True
            self._log("sl_give_up", slug, mkt.get('asset', ''), {
                "msg": f"🚫 止损重试{sl_retries}次全部失败, 放弃止损, 等待结算",
            }, level="error")
            return

        if sl_retries >= sl_max_retries:
            # 超过最大重试: 以极低价挂单, 几乎等于市价单兜底
            sell_price = 0.01
            self._log("sl_market_fallback", slug, mkt.get('asset', ''), {
                "msg": f"🚨 止损重试{sl_retries}次, 以$0.01兜底卖出",
            }, level="warn")
        else:
            sell_price = round(high_side_price - self.limit_offset, 4)
            sell_price = max(sell_price, 0.01)

        # 安全裁剪: 向下取整到0.01, 防止手续费/舍入导致 "not enough balance"
        # 例: 请求买5股, 实际到账4.97 → 卖5会失败
        import math
        shares = math.floor(shares * 100) / 100  # 4.97→4.97, 5.0→5.0
        shares = max(shares - 0.01, 1)            # 再减0.01安全余量: 4.97→4.96

        self._log("stop_loss_trigger", slug, mkt.get('asset', ''), {
            "msg": f"🛑 止损{'触发' if sl_retries == 0 else f'重挂(第{sl_retries}次)'}! "
                   f"低价方={low_side_price:.3f} → 卖{buy_side} @{sell_price:.3f} ({shares:.2f}股)",
            "low_side_price": low_side_price,
            "sell_price": sell_price,
            "shares": shares,
            "retry": sl_retries,
        })

        result = self.trading.place_limit_sell(
            token_id=sell_token, price=sell_price, size=shares,
        )

        if result['success']:
            # 记录挂单, 等下次循环确认成交
            mkt['sl_order_id'] = result.get('order_id', '')
            mkt['sl_sell_price'] = sell_price
            mkt['sl_reason'] = sl_reason
            mkt['sl_balance_fails'] = 0  # 成功挂单, 重置balance失败计数

            self._log("sl_order_placed", slug, mkt.get('asset', ''), {
                "msg": f"📋 止损卖单已挂: {result.get('order_id', '')} @{sell_price:.3f}",
            })

            # dry-run 模式下直接视为成交
            if self.dry_run:
                self._finalize_stop_loss(slug, mkt, sell_price, sl_reason)
        else:
            error_detail = result.get('detail', '')

            # 🔑 识别 "not enough balance" — 份额不存在, 无需继续重试
            if 'not enough balance' in error_detail.lower() or 'allowance' in error_detail.lower():
                mkt['sl_balance_fails'] = mkt.get('sl_balance_fails', 0) + 1
                if mkt['sl_balance_fails'] >= 3:
                    # 连续3次余额不足 → 份额确实不存在, 标记abandoned
                    mkt['sl_order_id'] = None
                    mkt['sl_abandoned'] = True
                    self._log("sl_no_balance", slug, mkt.get('asset', ''), {
                        "msg": f"🚫 连续{mkt['sl_balance_fails']}次'not enough balance', "
                               f"份额可能已不存在, 放弃止损等结算",
                    }, level="error")
                    return
                # balance错误但未到阈值, 不计入sl_retries, 等下次重试
                self._log("stop_loss_fail", slug, mkt.get('asset', ''), {
                    "msg": f"⚠️ 止损卖出失败 (balance不足 {mkt['sl_balance_fails']}/3): "
                           f"{error_detail[:200]}",
                }, level="warn")
                return

            # 其他错误: 累加retries
            mkt['sl_retries'] = sl_retries + 1
            mkt['sl_balance_fails'] = 0  # 非balance错误, 重置balance计数
            self._log("stop_loss_fail", slug, mkt.get('asset', ''), {
                "msg": f"⚠️ 止损卖出失败 (第{mkt['sl_retries']}次): "
                       f"{error_detail[:200]}",
            }, level="warn")

    def _finalize_stop_loss(self, slug: str, mkt: dict,
                            actual_sell_price: float,
                            sl_reason: str):
        """止损成交后的结算逻辑"""
        buy_side = mkt.get('buy_side')
        buy_price = mkt.get('buy_price', 0)
        shares = mkt.get('buy_shares', mkt.get('buy_amount', self.bet_amount) / buy_price if buy_price > 0 else 0)

        # 使用实际花费 (shares × buy_price) 而不是下单意图金额，避免部分成交时 PnL 计算错误
        actual_cost = shares * buy_price
        sell_revenue = shares * actual_sell_price
        pnl = sell_revenue - actual_cost

        mkt['status'] = 'settled'
        mkt['outcome'] = 'stop_loss'
        mkt['pnl'] = pnl
        mkt['exit_price'] = actual_sell_price
        mkt['sl_order_id'] = None  # 清除
        self.total_pnl += pnl

        if pnl >= 0:
            self.wins += 1
            self.consecutive_sl = 0
        else:
            self.losses += 1
            self.consecutive_sl += 1

        # 🔒 禁止同市场重入
        self.stopped_slugs.add(slug)

        # 🧊 连续止损冷却
        if self.consecutive_sl >= self.cooldown_losses:
            cooldown_sec = 900
            self.cooldown_until = time.time() + cooldown_sec
            self._log("cooldown_start", slug, mkt.get('asset', ''), {
                "msg": f"🧊 连续{self.consecutive_sl}次止损, "
                       f"冷却{cooldown_sec}s 暂停新入场",
            })

        # 检查滑点
        loss_ratio = abs(pnl) / amount if amount > 0 else 0
        slippage_warn = ""
        if loss_ratio > self.max_sl_loss:
            slippage_warn = f" ⚠️滑点过大({loss_ratio:.0%}>{self.max_sl_loss:.0%})"

        retries = mkt.get('sl_retries', 0)
        self._log("stop_loss_exit", slug, mkt.get('asset', ''), {
            "msg": f"🛑 止损成交! 买{buy_side}@{buy_price:.3f} "
                   f"→ 卖@{actual_sell_price:.3f} PnL=${pnl:+.2f} "
                   f"累计${self.total_pnl:+.2f}"
                   f" (连续SL:{self.consecutive_sl}, 重试:{retries}){slippage_warn}",
            "buy_price": buy_price,
            "sell_price": actual_sell_price,
            "pnl": pnl,
            "consecutive_sl": self.consecutive_sl,
            "loss_ratio": round(loss_ratio, 4),
            "retries": retries,
        })

        db_execute(self.db,
            "UPDATE smart_seller_trades SET "
            "outcome='stop_loss', pnl=%s, settled_at=NOW(), "
            "settle_method='stop_loss', exit_type='stop_loss', "
            "exit_price=%s, sl_reason=%s, price_log=%s, status='settled' WHERE id=%s",
            (pnl, actual_sell_price, sl_reason,
             json.dumps(mkt.get('price_log', [])),
             mkt.get('db_id')))

    # ------ 止盈 (Take Profit) ------

    async def _execute_take_profit(self, slug: str, mkt: dict, low_side_price: float):
        """
        止盈: 低价方跌到 tp_trigger → 卖出持有的高价方锁定利润
        """
        buy_side = mkt.get('buy_side')
        buy_price = mkt.get('buy_price', 0)
        amount = mkt.get('buy_amount', self.bet_amount)
        shares = mkt.get('buy_shares', amount / buy_price if buy_price > 0 else 0)

        if buy_side == 'NO':
            sell_token = mkt['no_token']
        else:
            sell_token = mkt['yes_token']

        # 已有止盈挂单: 检查成交状态
        if mkt.get('tp_order_id'):
            fill_info = self.trading.get_order_status(mkt['tp_order_id'])

            if fill_info and fill_info.get('filled'):
                avg_price = fill_info.get('avg_price', mkt.get('tp_sell_price', 0))
                self._finalize_take_profit(slug, mkt,
                    avg_price if avg_price and avg_price > 0 else mkt.get('tp_sell_price', 0))
                return

            if fill_info and fill_info.get('cancelled'):
                mkt['tp_order_id'] = None
                # 继续往下重新挂单

            elif fill_info is None:
                mkt['tp_api_fails'] = mkt.get('tp_api_fails', 0) + 1
                if mkt['tp_api_fails'] < 5:
                    return
                # 连续查询失败, 取消重挂
                self.trading.cancel_order(mkt['tp_order_id'])
                mkt['tp_order_id'] = None
                mkt['tp_api_fails'] = 0

            else:
                # 挂单中: 检查是否可以追价 (低价方跌得更深 → 高价方更值钱)
                old_sell_price = mkt.get('tp_sell_price', 0)
                new_high = 1 - low_side_price
                new_sell_price = round(new_high - self.tp_offset, 4)
                if new_sell_price > old_sell_price + 0.01:  # 有明显改善才追价
                    self.trading.cancel_order(mkt['tp_order_id'])
                    fill_check = self.trading.get_order_status(mkt['tp_order_id'])
                    if fill_check and fill_check.get('filled'):
                        avg_price = fill_check.get('avg_price', old_sell_price)
                        self._finalize_take_profit(slug, mkt,
                            avg_price if avg_price and avg_price > 0 else old_sell_price)
                        return
                    mkt['tp_order_id'] = None
                    self._log('tp_reprice', slug, mkt.get('asset', ''), {
                        'msg': f'🔼 止盈追价: {old_sell_price:.3f}→{new_sell_price:.3f}'
                    })
                    # 继续往下重新挂单
                else:
                    return  # 价格无改善, 等待成交

        # 挂新的止盈卖单
        high_side_price = 1 - low_side_price
        sell_price = round(high_side_price - self.tp_offset, 4)
        sell_price = max(sell_price, 0.01)

        import math
        shares = math.floor(shares * 100) / 100
        shares = max(shares - 0.01, 1)

        self._log('take_profit_trigger', slug, mkt.get('asset', ''), {
            'msg': f'💰 止盈触发! 低价方={low_side_price:.3f} ≤ {self.tp_trigger} '
                   f'→ 卖{buy_side} @{sell_price:.3f} ({shares:.2f}股)',
            'low_side_price': low_side_price,
            'sell_price': sell_price,
            'shares': shares,
        })

        result = self.trading.place_limit_sell(
            token_id=sell_token, price=sell_price, size=shares,
        )

        if result['success']:
            mkt['tp_order_id'] = result.get('order_id', '')
            mkt['tp_sell_price'] = sell_price
            self._log('tp_order_placed', slug, mkt.get('asset', ''), {
                'msg': f'📋 止盈卖单已挂: {result.get("order_id","")} @{sell_price:.3f}'
            })
            if self.dry_run:
                self._finalize_take_profit(slug, mkt, sell_price)
        else:
            self._log('take_profit_fail', slug, mkt.get('asset', ''), {
                'msg': f'⚠️ 止盈卖出失败: {result.get("detail","")[:200]}'
            }, level='warn')

    def _finalize_take_profit(self, slug: str, mkt: dict, actual_sell_price: float):
        """止盈成交后的结算逻辑"""
        buy_side = mkt.get('buy_side')
        buy_price = mkt.get('buy_price', 0)
        shares = mkt.get('buy_shares', mkt.get('buy_amount', self.bet_amount) / buy_price if buy_price > 0 else 0)

        # 使用实际花费 (shares × buy_price) 而不是下单意图金额，避免部分成交时 PnL 计算错误
        actual_cost = shares * buy_price
        sell_revenue = shares * actual_sell_price
        pnl = sell_revenue - actual_cost

        mkt['status'] = 'settled'
        mkt['outcome'] = 'take_profit'
        mkt['pnl'] = pnl
        mkt['exit_price'] = actual_sell_price
        mkt['tp_order_id'] = None
        self.total_pnl += pnl
        self.wins += 1          # 止盈总是盈利的 (触发时已经在赢面上)
        self.consecutive_sl = 0  # 止盈重置连续止损计数

        self._log('take_profit_exit', slug, mkt.get('asset', ''), {
            'msg': f'💰 止盈成交! 买{buy_side}@{buy_price:.3f} '
                   f'→ 卖@{actual_sell_price:.3f} PnL=${pnl:+.2f} '
                   f'累计${self.total_pnl:+.2f}',
            'buy_price': buy_price,
            'sell_price': actual_sell_price,
            'pnl': pnl,
        })

        db_execute(self.db,
            "UPDATE smart_seller_trades SET "
            "outcome='take_profit', pnl=%s, settled_at=NOW(), "
            "settle_method='take_profit', exit_type='take_profit', "
            "exit_price=%s, price_log=%s, status='settled' WHERE id=%s",
            (pnl, actual_sell_price,
             json.dumps(mkt.get('price_log', [])),
             mkt.get('db_id')))
    async def _try_settle(self, slug: str, mkt: dict):
        outcome = await self._resolve_via_gamma(slug)
        if outcome:
            self._settle(slug, mkt, outcome)
            return

        yes_mid = await self.fetch_midpoint(mkt['yes_token'])
        if yes_mid is not None:
            if yes_mid > 0.90:
                self._settle(slug, mkt, "YES")
                return
            elif yes_mid < 0.10:
                self._settle(slug, mkt, "NO")
                return

        elapsed = int(time.time()) - mkt['epoch']
        if elapsed > self.window + 300:
            last_y = mkt['price_log'][-1]['y'] if mkt.get('price_log') else 0.5
            forced = "YES" if last_y >= 0.5 else "NO"
            self._log("force_settle", slug, mkt.get('asset', ''), {
                "msg": f"⏰ 超时强制结算: YES={last_y:.3f} →{forced}",
            }, level="warn")
            self._settle(slug, mkt, forced)

    # ------ 主循环 ------

    async def run(self, duration_sec: float = 0):
        proxy_url = (os.environ.get("HTTPS_PROXY") or os.environ.get("HTTP_PROXY") or os.environ.get("ALL_PROXY") or "").strip()
        if proxy_url and "socks" in proxy_url.lower():
            try:
                from aiohttp_socks import ProxyConnector
                connector = ProxyConnector.from_url(proxy_url)
                print("  ✅ 请求经代理: " + (proxy_url.split("@")[-1] if "@" in proxy_url else proxy_url))
            except ImportError:
                connector = aiohttp.TCPConnector(family=2)
                print("  ⚠️ 未安装 aiohttp-socks，SOCKS 代理未生效，请: pip install aiohttp-socks")
        else:
            connector = aiohttp.TCPConnector(family=2)
        self.http_session = aiohttp.ClientSession(
            connector=connector,
            trust_env=bool(proxy_url and "socks" not in proxy_url.lower()),
        )

        mode = "🏷️ DRY-RUN" if self.dry_run else "💰 LIVE TRADING"
        print(f"\n{'='*60}")
        print(f"📉 卖方策略 (Seller) {mode}")
        print(f"🚀 版本:      v2.0 (已集成 DVOL + RSI + MACD 微观动量过滤)")
        print(f"{'='*60}")
        print(f"  资产:       {', '.join(a.upper() for a in self.assets)}")
        print(f"  触发阈值:   ≤ {self.threshold:.0%} (低价方)")
        print(f"  最低阈值:   ≥ {self.min_threshold:.0%} (过滤噪音)")
        print(f"  最高买价:   ≤ {self.max_buy_price:.0%} (过滤高价入场)")
        print(f"  入场窗口:   {self.min_elapsed_pct:.0%} ~ {self.max_elapsed_pct:.0%} (占窗口)")
        print(f"  阶段过滤:   {self.phase_filter}")
        if self.fixed_shares > 0:
            print(f"  下单方式:   固定 {self.fixed_shares:.0f} shares/笔 (忽略 bet_amount)")
        else:
            print(f"  每笔金额:   ${self.bet_amount:.2f}")
        print(f"  最大交易:   {self.max_trades if self.max_trades > 0 else '无限'}")
        print(f"  止损线:     {'$' + str(self.max_loss) if self.max_loss > 0 else '无'}")
        print(f"  限价偏移:   {self.limit_offset:.3f}")
        sl_info = f"低价方≥{self.sl_trigger:.0%} (连续{self.sl_confirm}次确认)" if self.sl_trigger > 0 else "无(持有到期)"
        print(f"  止损触发:   {sl_info}")
        if self.sl_trigger > 0:
            print(f"  极端快通:   低价方≥{self.sl_fast_track:.0%} → 立即止损")
            print(f"  冷却机制:   连续{self.cooldown_losses}次止损后暂停1窗口")
            print(f"  禁止重入:   止损后同市场不再入场")
        if self.require_bias_for_no:
            print(f"  买NO过滤:   ✅ 需要明确 NO_ONLY 偏向 (无偏向则跳过)")
        if self.night_skip_start >= 0 and self.night_skip_end >= 0:
            cst_start = (self.night_skip_start + 8) % 24
            cst_end   = (self.night_skip_end   + 8) % 24
            print(f"  夜间跳过:   UTC {self.night_skip_start:02d}:00~{self.night_skip_end:02d}:00 "
                  f"(CST {cst_start:02d}:00~{cst_end:02d}:00)")
        if self.tp_trigger > 0:
            tp_info = f"低价方≤{self.tp_trigger:.0%} (连续{self.tp_confirm}次确认, 偏移{self.tp_offset:.3f})"
            print(f"  止盈触发:   {tp_info}")
        print(f"  数据库:     MySQL ({os.getenv('MYSQL_HOST','127.0.0.1')}:{os.getenv('MYSQL_PORT','3306')}/{os.getenv('MYSQL_DATABASE','poly')})")
        print()
        print(f"  策略逻辑:")
        print(f"  当 YES ≤ {self.threshold:.0%} → 买入 NO @~{1-self.threshold:.0%}")
        print(f"  当 NO  ≤ {self.threshold:.0%} → 买入 YES @~{1-self.threshold:.0%}")
        if self.sl_trigger > 0:
            print(f"  止损: 低价方涨回 ≥{self.sl_trigger:.0%} → 卖出高价方 @~{1-self.sl_trigger:.0%}")
        else:
            print(f"  持有到结算")
        print(f"  赢: 赚 ~${self.bet_amount * self.threshold / (1-self.threshold):.2f}  "
              f"输: 亏 ~${self.bet_amount:.2f}")

        if not self.dry_run:
            balance = self.trading.get_balance()
            if balance:
                print(f"  余额:       {balance}")

        print(f"{'='*60}\n")

        self._log("session_start", detail={
            "msg": f"卖方策略启动 {mode}",
            "assets": self.assets,
            "threshold": self.threshold,
            "amount": self.bet_amount,
        })

        tick_count = 0
        try:
            while self.running:
                # 时限
                if duration_sec > 0 and time.time() - self.start_time > duration_sec:
                    print(f"\n⏰ 到达运行时限")
                    break

                # 止损
                if self.max_loss > 0 and self.total_pnl <= -self.max_loss:
                    active = [s for s, m in self.markets.items()
                              if m.get('status') in ('entered', 'order_pending')]
                    if not active:
                        print(f"\n🛑 止损! 亏损${-self.total_pnl:.2f} ≥ ${self.max_loss:.2f}")
                        self._log("stop_loss", detail={
                            "msg": f"止损触发, PnL=${self.total_pnl:+.2f}",
                        })
                        break

                # 最大交易数
                if self.max_trades > 0 and self.trade_count >= self.max_trades:
                    active = [s for s, m in self.markets.items()
                              if m.get('status') in ('entered', 'order_pending')]
                    if not active:
                        print(f"\n🎯 达到最大交易数 {self.max_trades}")
                        break

                now_ts = int(time.time())

                # ===== 0. 更新大趋势与微观指标 =====
                for tracker in self.trackers.values():
                    await tracker.update(self.http_session)
                for ind in self.indicators.values():
                    await ind.update(self.http_session)
                await self.dvol_tracker.update(self.http_session)

                # ===== 1. 发现新市场 =====
                new_markets = await self.discover_markets()
                for info in new_markets:
                    slug = info['slug']
                    if slug in self.markets:
                        continue
                    elapsed = now_ts - info['epoch']

                    self.markets[slug] = {
                        **info,
                        'status': 'watching',
                        'price_log': [],
                        'db_id': None,
                        'order_retries': 0,
                        'last_order_attempt': 0,
                        'sl_confirm_count': 0,
                        'tp_confirm_count': 0,
                    }

                    cur = db_execute(self.db,
                        "INSERT INTO smart_seller_trades "
                        "(session_id, asset, slug, epoch, yes_token, no_token, question, status) "
                        "VALUES (%s,%s,%s,%s,%s,%s,%s,%s)",
                        (self.session_id, info['asset'].upper(), slug,
                         info['epoch'], info['yes_token'], info['no_token'],
                         info['question'], 'watching')
                    )
                    self.markets[slug]['db_id'] = cur.lastrowid

                    self._log("discover", slug, info['asset'], {
                        "msg": f"🆕 发现市场 (elapsed={elapsed}s)",
                    })

                # ===== 2. 处理每个市场 =====
                for slug, mkt in list(self.markets.items()):
                    if mkt['status'] in ('settled', 'skipped', 'error'):
                        continue

                    elapsed = now_ts - mkt['epoch']
                    asset = mkt.get('asset', '')

                    # 过期清理
                    if elapsed > self.window + 180:
                        if mkt['status'] == 'entered':
                            await self._try_settle(slug, mkt)
                        elif mkt['status'] == 'order_pending':
                            self.trading.cancel_order(mkt.get('order_id', ''))
                            mkt['status'] = 'skipped'
                            self._log("cancel_expired", slug, asset, {
                                "msg": "⏭️ 限价单过期, 取消",
                            })
                        elif mkt['status'] == 'watching':
                            mkt['status'] = 'skipped'
                            db_execute(self.db,
                                "UPDATE smart_seller_trades SET status='skipped' WHERE id=%s",
                                (mkt['db_id'],))
                        continue

                    if elapsed < 0:
                        continue

                    # 获取价格
                    yes_mid = await self.fetch_midpoint(mkt['yes_token'])
                    if yes_mid is None:
                        # 处理挂单和已入场的情况 (同 trend_live)
                        if mkt['status'] == 'order_pending':
                            fill_info = self.trading.get_order_status(mkt.get('order_id', ''))
                            if fill_info and fill_info.get('filled'):
                                mkt['status'] = 'entered'
                                self.trade_count += 1
                                self._log("order_filled", slug, asset, {
                                    "msg": f"✅ 限价单成交 (midpoint不可用)",
                                })
                                db_execute(self.db,
                                    "UPDATE smart_seller_trades SET status='entered', "
                                    "entry_at=NOW(), order_success=1 WHERE id=%s",
                                    (mkt['db_id'],))
                        elif mkt['status'] == 'entered' and elapsed > self.window - 5:
                            await self._try_settle(slug, mkt)
                        continue

                    no_mid = round(1.0 - yes_mid, 6)

                    mkt['price_log'].append({
                        "t": elapsed, "y": round(yes_mid, 4), "n": round(no_mid, 4)
                    })

                    # === watching: 等待触发信号 ===
                    if mkt['status'] == 'watching':
                        # 最大交易数检查
                        if self.max_trades > 0 and self.trade_count >= self.max_trades:
                            continue

                        # 止损后禁止同市场重入
                        if slug in self.stopped_slugs:
                            if elapsed > self.window - 30:
                                mkt['status'] = 'skipped'
                                db_execute(self.db,
                                    "UPDATE smart_seller_trades SET status='skipped', "
                                    "skip_reason='reentry_block' WHERE id=%s",
                                    (mkt['db_id'],))
                                self._log("no_trigger", slug, asset, {
                                    "msg": f"⏭️ 已止损过, 跳过重入",
                                })
                            continue

                        # 连续止损冷却期
                        if self.cooldown_until > time.time():
                            remaining_cd = self.cooldown_until - time.time()
                            if elapsed > self.window - 30:
                                mkt['status'] = 'skipped'
                                db_execute(self.db,
                                    "UPDATE smart_seller_trades SET status='skipped', "
                                    "skip_reason='cooldown' WHERE id=%s",
                                    (mkt['db_id'],))
                                self._log("no_trigger", slug, asset, {
                                    "msg": f"⏭️ 冷却中 ({remaining_cd:.0f}s), 跳过",
                                })
                            continue

                        trigger = self._check_trigger(yes_mid, no_mid, elapsed, asset, slug)
                        if not trigger:
                            # 即将过期, 标记跳过
                            if elapsed > self.window - 30:
                                mkt['status'] = 'skipped'
                                db_execute(self.db,
                                    "UPDATE smart_seller_trades SET status='skipped' WHERE id=%s",
                                    (mkt['db_id'],))
                                self._log("no_trigger", slug, asset, {
                                    "msg": f"⏭️ 市场结束, 未触发 (YES={yes_mid:.3f})",
                                })
                            continue

                        # --- 触发! ---
                        mkt['status'] = 'triggered'
                        mkt['trigger_side'] = trigger['trigger_side']
                        mkt['trigger_price'] = trigger['trigger_price']

                        self._log("trigger", slug, asset, {
                            "msg": f"🎯 触发! {trigger['trigger_side']}跌至"
                                   f"${trigger['trigger_price']:.3f} ≤ {self.threshold} "
                                   f"→ 买{trigger['buy_side']} "
                                   f"@{trigger['buy_price']:.3f} "
                                   f"({trigger['phase']}阶段, {elapsed:.0f}s)",
                        })

                        # 立即尝试下单
                        buy_side = trigger['buy_side']
                        buy_mid = trigger['buy_price']
                        limit_price = round(buy_mid - self.limit_offset, 4)
                        limit_price = max(limit_price, 0.01)
                        token_id = mkt[trigger['buy_token_key']]
                        if self.fixed_shares > 0:
                            # 固定份额模式: 直接用指定份额数
                            size = self.fixed_shares
                            actual_cost = round(size * limit_price, 4)
                        else:
                            size = round(self.bet_amount / limit_price, 4)
                            if size < MIN_ORDER_SIZE:
                                size = MIN_ORDER_SIZE  # 最低5 shares
                                actual_cost = size * limit_price
                            else:
                                actual_cost = self.bet_amount

                        self._log("place_order", slug, asset, {
                            "msg": f"🔔 限价单: 买{buy_side} "
                                   f"@{limit_price:.3f} (mid={buy_mid:.3f}) "
                                   f"{size:.2f}股 ${actual_cost:.2f}",
                        })

                        result = self.trading.place_limit_buy(
                            token_id=token_id, price=limit_price, size=size,
                        )

                        if result['success']:
                            mkt['status'] = 'order_pending'
                            mkt['buy_side'] = buy_side
                            mkt['buy_price'] = limit_price
                            mkt['buy_amount'] = actual_cost
                            mkt['buy_shares'] = size
                            mkt['order_id'] = result.get('order_id', '')
                            mkt['order_placed_at'] = time.time()

                            self._log("order_placed", slug, asset, {
                                "msg": f"✅ 限价单已挂: {result.get('order_id', '')}",
                            })
                            db_execute(self.db,
                                "UPDATE smart_seller_trades SET "
                                "status='order_pending', trigger_side=%s, trigger_price=%s, "
                                "trigger_elapsed=%s, trigger_phase=%s, "
                                "buy_side=%s, buy_price=%s, buy_amount=%s, buy_shares=%s, "
                                "order_id=%s, order_response=%s, macro_bias=%s, trend_info=%s WHERE id=%s",
                                (trigger['trigger_side'], trigger['trigger_price'],
                                 elapsed, trigger['phase'],
                                 buy_side, limit_price, actual_cost, size,
                                 result.get('order_id', ''),
                                 json.dumps(result),
                                 trigger.get('macro_bias'),
                                 json.dumps(trigger.get('trend_info', {})),
                                 mkt['db_id']))
                        else:
                            # 下单失败, 回到 watching 等下次触发
                            mkt['status'] = 'watching'
                            mkt['order_retries'] = mkt.get('order_retries', 0) + 1
                            self._log("order_failed", slug, asset, {
                                "msg": f"❌ 下单失败: {result.get('detail', '')[:200]}",
                            }, level="error")

                    # === order_pending: 等成交 ===
                    elif mkt['status'] == 'order_pending':
                        remaining = self.window - elapsed
                        order_id = mkt.get('order_id', '')

                        fill_info = self.trading.get_order_status(order_id)

                        if fill_info and fill_info.get('filled'):
                            mkt['status'] = 'entered'
                            self.trade_count += 1
                            avg_price = fill_info.get('avg_price')
                            if avg_price and avg_price > 0:
                                mkt['buy_price'] = avg_price
                            # 用实际成交量更新 buy_shares (防止请求量≠实際成交量)
                            size_matched = fill_info.get('size_matched', 0)
                            if size_matched and size_matched > 0:
                                mkt['buy_shares'] = size_matched

                            self._log("order_filled", slug, asset, {
                                "msg": f"✅ 成交! 买{mkt['buy_side']}"
                                       f"@{mkt['buy_price']:.3f} "
                                       f"{mkt.get('buy_shares',0):.2f}股 "
                                       f"(卖{mkt.get('trigger_side','')}策略)",
                            })
                            db_execute(self.db,
                                "UPDATE smart_seller_trades SET status='entered', "
                                "entry_at=NOW(), order_success=1 WHERE id=%s",
                                (mkt['db_id'],))

                        elif fill_info and fill_info.get('cancelled'):
                            mkt['status'] = 'watching'
                            self._log("order_cancelled", slug, asset, {
                                "msg": "⚠️ 限价单被取消, 回到监控",
                            }, level="warn")

                        elif remaining < 60:
                            # 快到期, 取消
                            self.trading.cancel_order(order_id)
                            fill_check = self.trading.get_order_status(order_id)
                            if fill_check and fill_check.get('filled'):
                                mkt['status'] = 'entered'
                                self.trade_count += 1
                                self._log("order_filled_late", slug, asset, {
                                    "msg": f"✅ 取消前成交!",
                                })
                                db_execute(self.db,
                                    "UPDATE smart_seller_trades SET status='entered', "
                                    "entry_at=NOW(), order_success=1 WHERE id=%s",
                                    (mkt['db_id'],))
                            else:
                                mkt['status'] = 'skipped'
                                self._log("order_timeout", slug, asset, {
                                    "msg": f"⏭️ 限价单未成交, 剩{remaining:.0f}s, 取消",
                                })
                                db_execute(self.db,
                                    "UPDATE smart_seller_trades SET status='skipped' WHERE id=%s",
                                    (mkt['db_id'],))

                        else:
                            # 检查是否需要重新定价
                            current_mid = yes_mid if mkt['buy_side'] == 'YES' else no_mid
                            current_limit = mkt.get('buy_price', 0)
                            ideal_limit = round(current_mid - self.limit_offset, 4)
                            ideal_limit = max(ideal_limit, 0.01)

                            if abs(ideal_limit - current_limit) >= self.reprice_gap:
                                # ★ 先检查旧单是否已部分成交, 防止重复建仓
                                fill_before_cancel = self.trading.get_order_status(order_id)
                                already_filled = 0.0
                                if fill_before_cancel:
                                    if fill_before_cancel.get('filled'):
                                        # 旧单已全部成交, 无需重挂, 直接进入entered
                                        mkt['status'] = 'entered'
                                        self.trade_count += 1
                                        avg_price = fill_before_cancel.get('avg_price')
                                        if avg_price and avg_price > 0:
                                            mkt['buy_price'] = avg_price
                                        size_matched = fill_before_cancel.get('size_matched', 0)
                                        if size_matched and size_matched > 0:
                                            mkt['buy_shares'] = size_matched
                                        self._log("order_filled", slug, asset, {
                                            "msg": f"✅ 重挂前发现已成交! "
                                                   f"{mkt.get('buy_shares',0):.2f}股",
                                        })
                                        db_execute(self.db,
                                            "UPDATE smart_seller_trades SET status='entered', "
                                            "entry_at=NOW(), order_success=1 WHERE id=%s",
                                            (mkt['db_id'],))
                                        continue
                                    already_filled = float(fill_before_cancel.get('size_matched', 0) or 0)

                                self.trading.cancel_order(order_id)
                                token_key = 'yes_token' if mkt['buy_side'] == 'YES' else 'no_token'

                                # ★ 计算目标总份额 (尊重 fixed_shares)
                                if self.fixed_shares > 0:
                                    target_size = self.fixed_shares
                                else:
                                    target_size = round(self.bet_amount / ideal_limit, 4)
                                    if target_size < MIN_ORDER_SIZE:
                                        target_size = MIN_ORDER_SIZE

                                # ★ 扣除已成交部分, 只补挂剩余
                                remaining_size = round(target_size - already_filled, 4)
                                if remaining_size < 1.0:
                                    # 已成交量接近目标, 不再补挂
                                    if already_filled > 0:
                                        mkt['status'] = 'entered'
                                        self.trade_count += 1
                                        mkt['buy_shares'] = already_filled
                                        self._log("reprice_skip_filled", slug, asset, {
                                            "msg": f"✅ 已成交{already_filled:.1f}股"
                                                   f"(目标{target_size:.0f}), 不再补挂",
                                        })
                                        db_execute(self.db,
                                            "UPDATE smart_seller_trades SET status='entered', "
                                            "entry_at=NOW(), order_success=1, "
                                            "buy_shares=%s WHERE id=%s",
                                            (already_filled, mkt['db_id']))
                                    else:
                                        mkt['status'] = 'watching'
                                        self._log("reprice_skip", slug, asset, {
                                            "msg": f"⚠️ 剩余{remaining_size:.1f}股太少, 跳过",
                                        })
                                    continue

                                new_result = self.trading.place_limit_buy(
                                    token_id=mkt[token_key],
                                    price=ideal_limit, size=remaining_size,
                                )
                                if new_result['success']:
                                    mkt['order_id'] = new_result.get('order_id', '')
                                    mkt['buy_price'] = ideal_limit
                                    mkt['buy_shares'] = target_size  # 记录目标总份额
                                    filled_note = f" (已成交{already_filled:.1f}, 补挂{remaining_size:.1f})" if already_filled > 0 else ""
                                    self._log("reprice", slug, asset, {
                                        "msg": f"🔄 重挂: {current_limit:.3f}"
                                               f"→{ideal_limit:.3f}{filled_note}",
                                    })
                                else:
                                    if already_filled > 0:
                                        # 重挂失败但已有部分成交, 按已成交量入场
                                        mkt['status'] = 'entered'
                                        self.trade_count += 1
                                        mkt['buy_shares'] = already_filled
                                        self._log("reprice_fail_partial", slug, asset, {
                                            "msg": f"⚠️ 重挂失败, 按已成交{already_filled:.1f}股入场",
                                        }, level="warn")
                                        db_execute(self.db,
                                            "UPDATE smart_seller_trades SET status='entered', "
                                            "entry_at=NOW(), order_success=1, "
                                            "buy_shares=%s WHERE id=%s",
                                            (already_filled, mkt['db_id']))
                                    else:
                                        mkt['status'] = 'watching'
                                        mkt['order_retries'] = mkt.get('order_retries', 0) + 1
                                        self._log("reprice_fail", slug, asset, {
                                            "msg": "⚠️ 重挂失败, 回到监控",
                                        }, level="warn")

                    # === entered: 等结算 / 监控止盈止损 ===
                    elif mkt['status'] == 'entered':
                        _settling_from_sl_cancel = False

                        # --- 止盈挂单追踪 (|优先级最高) ---
                        if mkt.get('tp_order_id'):
                            if elapsed > self.window + 30:
                                # 市场到期: 取消止盈单, 走结算流程
                                self.trading.cancel_order(mkt['tp_order_id'])
                                fill_check = self.trading.get_order_status(mkt['tp_order_id'])
                                if fill_check and fill_check.get('filled'):
                                    avg_price = fill_check.get('avg_price', mkt.get('tp_sell_price', 0))
                                    self._finalize_take_profit(slug, mkt,
                                        avg_price if avg_price and avg_price > 0 else mkt.get('tp_sell_price', 0))
                                    continue
                                mkt['tp_order_id'] = None
                                self._log('tp_cancel_for_settle', slug, asset, {
                                    'msg': '⏰ 市场到期, 取消止盈卖单, 转为结算'
                                })
                                # 不 continue, 继续往下走结算逻辑
                            else:
                                buy_side = mkt.get('buy_side')
                                low_side_price = yes_mid if buy_side == 'NO' else no_mid
                                await self._execute_take_profit(slug, mkt, low_side_price)
                                continue  # 止盈进行中, 跟踪成交, 跳过SL和结算

                        # --- 止盈触发检测 ---
                        elif self.tp_trigger > 0:
                            buy_side = mkt.get('buy_side')
                            low_side_price = yes_mid if buy_side == 'NO' else no_mid
                            if low_side_price <= self.tp_trigger:
                                mkt['tp_confirm_count'] = mkt.get('tp_confirm_count', 0) + 1
                                if mkt['tp_confirm_count'] >= self.tp_confirm:
                                    await self._execute_take_profit(slug, mkt, low_side_price)
                                    if mkt.get('status') == 'settled':
                                        continue
                                else:
                                    self._log('tp_warning', slug, asset, {
                                        'msg': f'💰 止盈预备 {mkt["tp_confirm_count"]}/{self.tp_confirm} '
                                               f'低价方={low_side_price:.3f} ≤ {self.tp_trigger}'
                                    })
                            else:
                                if mkt.get('tp_confirm_count', 0) > 0:
                                    mkt['tp_confirm_count'] = 0

                        # --- 已有止损挂单: 优先追踪成交 ---
                        if mkt.get('sl_order_id'):
                            # ⏰ 如果市场已到期, 取消止损卖单, 走结算流程
                            if elapsed > self.window + 30:
                                self.trading.cancel_order(mkt['sl_order_id'])
                                # 检查取消前是否已成交
                                fill_check = self.trading.get_order_status(mkt['sl_order_id'])
                                if fill_check and fill_check.get('filled'):
                                    avg_price = fill_check.get('avg_price', mkt.get('sl_sell_price', 0))
                                    self._finalize_stop_loss(
                                        slug, mkt,
                                        avg_price if avg_price and avg_price > 0 else mkt.get('sl_sell_price', 0),
                                        mkt.get('sl_reason', 'normal'))
                                    continue
                                # 未成交, 清除止损状态, 跳过止损检测直接走结算
                                mkt['sl_order_id'] = None
                                _settling_from_sl_cancel = True
                                self._log("sl_cancel_for_settle", slug, asset, {
                                    "msg": f"⏰ 市场到期, 取消止损卖单, 转为结算",
                                })
                                # 不continue, 跳过止损检测, 直接走结算逻辑
                            else:
                                buy_side = mkt.get('buy_side')
                                if buy_side == 'NO':
                                    low_side_price = yes_mid
                                else:
                                    low_side_price = no_mid
                                await self._execute_stop_loss(
                                    slug, mkt, low_side_price,
                                    sl_reason=mkt.get('sl_reason', 'normal'))
                                continue

                        # --- 止损检测 (需连续确认 / 极端行情快速通道) ---
                        # 跳过条件: 市场到期取消SL / 已放弃止损(balance不足等) / 垃圾时间(最后5%)
                        _garbage_time = elapsed > self.window * 0.95  # 最后5%时间
                        if self.sl_trigger > 0 and not _settling_from_sl_cancel and not mkt.get('sl_abandoned') and not _garbage_time:
                            buy_side = mkt.get('buy_side')
                            if buy_side == 'NO':
                                low_side_price = yes_mid   # 低价方 = YES
                            else:
                                low_side_price = no_mid    # 低价方 = NO

                            if low_side_price >= self.sl_fast_track:
                                # 🚨 极端行情: 低价方暴涨超过快速通道阈值, 立即止损
                                self._log("sl_fast_track", slug, asset, {
                                    "msg": f"🚨 极端行情! 低价方={low_side_price:.3f} "
                                           f"≥ {self.sl_fast_track} → 立即止损 (跳过确认)",
                                })
                                await self._execute_stop_loss(slug, mkt, low_side_price, sl_reason='fast_track')
                                continue
                            elif low_side_price >= self.sl_trigger:
                                mkt['sl_confirm_count'] = mkt.get('sl_confirm_count', 0) + 1
                                if mkt['sl_confirm_count'] >= self.sl_confirm:
                                    # 连续N次确认 → 执行止损
                                    await self._execute_stop_loss(slug, mkt, low_side_price, sl_reason='normal')
                                    continue
                                else:
                                    self._log("sl_warning", slug, asset, {
                                        "msg": f"⚠️ 止损预警 {mkt['sl_confirm_count']}/{self.sl_confirm} "
                                               f"低价方={low_side_price:.3f} ≥ {self.sl_trigger}",
                                    })
                            else:
                                # 价格回落, 重置确认计数
                                if mkt.get('sl_confirm_count', 0) > 0:
                                    mkt['sl_confirm_count'] = 0

                        if elapsed > self.window - 5:
                            if yes_mid > 0.90:
                                self._settle(slug, mkt, "YES")
                            elif yes_mid < 0.10:
                                self._settle(slug, mkt, "NO")
                            elif elapsed > self.window + 30:
                                outcome = await self._resolve_via_gamma(slug)
                                if outcome:
                                    self._settle(slug, mkt, outcome)
                                elif elapsed > self.window + 300:
                                    last_y = mkt['price_log'][-1]['y'] if mkt.get('price_log') else 0.5
                                    forced = "YES" if last_y >= 0.5 else "NO"
                                    self._log("force_settle", slug, asset, {
                                        "msg": f"⏰ 强制结算 →{forced}",
                                    }, level="warn")
                                    self._settle(slug, mkt, forced)

                # ===== 3. 状态报告 =====
                tick_count += 1
                if tick_count % 20 == 0:
                    self._print_status()

                # ===== 4. 清理旧市场 =====
                to_remove = []
                for s, m in self.markets.items():
                    age = now_ts - m['epoch']
                    if age > self.window + 600:
                        if m['status'] == 'entered':
                            last_y = m['price_log'][-1]['y'] if m.get('price_log') else 0.5
                            forced = "YES" if last_y >= 0.5 else "NO"
                            self._settle(s, m, forced)
                        elif m['status'] == 'order_pending':
                            self.trading.cancel_order(m.get('order_id', ''))
                        to_remove.append(s)
                for s in to_remove:
                    del self.markets[s]

                await asyncio.sleep(2.0)  # 多资产, 间隔稍长避免被限速

        except KeyboardInterrupt:
            print("\n\n⛔ 用户中断")
        except Exception as e:
            self._log("fatal_error", detail={
                "msg": f"致命错误: {e}",
                "traceback": traceback.format_exc(),
            }, level="error")
            print(f"\n❌ 致命错误: {e}")
            traceback.print_exc()
        finally:
            for slug, mkt in self.markets.items():
                if mkt.get('status') == 'order_pending':
                    self.trading.cancel_order(mkt.get('order_id', ''))
                if mkt.get('sl_order_id'):
                    self.trading.cancel_order(mkt.get('sl_order_id', ''))
                if mkt.get('tp_order_id'):
                    self.trading.cancel_order(mkt.get('tp_order_id', ''))
            await self.http_session.close()
            self._finalize()
            release_conn(self.db)

    # ------ 状态打印 ------

    def _print_status(self):
        ts = datetime.now().strftime("%H:%M:%S")
        total = self.wins + self.losses
        wr = self.wins / total if total > 0 else 0
        runtime = time.time() - self.start_time

        watching = sum(1 for m in self.markets.values() if m['status'] == 'watching')
        pending = sum(1 for m in self.markets.values() if m['status'] == 'order_pending')
        entered = sum(1 for m in self.markets.values() if m['status'] == 'entered')

        print(f"\n  ┌─ [{ts}] 智能卖方策略 · 运行{runtime/60:.0f}min ──────────")
        print(f"  │ 资产: {', '.join(a.upper() for a in self.assets)} | 阈值: {self.threshold}")
        for asset, tracker in self.trackers.items():
            print(f"  │ 大势 [{asset.upper()}]: {tracker.trend_str()}")
        print(f"  │ 监控:{watching} 挂单:{pending} 持仓:{entered}")
        print(f"  │ 交易:{self.trade_count} 胜{self.wins}/负{self.losses} ({wr:.0%}) PnL:${self.total_pnl:+.2f}")

        for slug, mkt in self.markets.items():
            if mkt['status'] in ('entered', 'order_pending', 'watching'):
                elapsed = int(time.time()) - mkt['epoch']
                remaining = self.window - elapsed
                last_y = mkt['price_log'][-1]['y'] if mkt.get('price_log') else '?'
                asset_tag = mkt.get('asset', '').upper()

                if mkt['status'] == 'entered':
                    low_side = min(last_y, 1 - last_y) if isinstance(last_y, float) else '?'
                    sl_warn = ''
                    if self.sl_trigger > 0 and isinstance(low_side, float):
                        if low_side >= self.sl_trigger * 0.8:
                            sl_warn = ' ⚠️止损临近'
                    print(f"  │  💰 [{asset_tag}] 买{mkt.get('buy_side','')}@{mkt.get('buy_price',0):.3f}"
                          f" (卖{mkt.get('trigger_side','')}) 剩{remaining}s YES={last_y}"
                          f" low={low_side}{sl_warn}")
                elif mkt['status'] == 'order_pending':
                    print(f"  │  📋 [{asset_tag}] 限价{mkt.get('buy_side','')}@{mkt.get('buy_price',0):.3f}"
                          f" 等成交 剩{remaining}s")
                elif mkt['status'] == 'watching' and remaining > 0:
                    if isinstance(last_y, float):
                        low = min(last_y, 1 - last_y)
                        print(f"  │  👀 [{asset_tag}] YES={last_y:.3f} low={low:.3f} 剩{remaining}s")

        print(f"  └{'─'*45}")

    def _finalize(self):
        total = self.wins + self.losses

        summary = json.dumps({
            "trades": self.trade_count,
            "wins": self.wins,
            "losses": self.losses,
            "pnl": self.total_pnl,
            "runtime_min": (time.time() - self.start_time) / 60,
        })
        db_execute(self.db,
            "UPDATE sessions SET ended_at=NOW(), "
            "total_trades=%s, total_pnl=%s, summary=%s WHERE id=%s",
            (self.trade_count, self.total_pnl, summary, self.session_id))

        wr = self.wins / total if total > 0 else 0
        runtime = time.time() - self.start_time

        print(f"\n{'='*60}")
        print(f"📊 卖方策略 · 会话报告")
        print(f"{'='*60}")
        print(f"  模式:       {'DRY-RUN' if self.dry_run else 'LIVE'}")
        print(f"  运行时间:   {runtime/60:.1f} 分钟")
        print(f"  资产:       {', '.join(a.upper() for a in self.assets)}")
        print(f"  阈值:       {self.threshold}")
        print(f"  交易数:     {self.trade_count}")
        if total > 0:
            print(f"  胜率:       {self.wins}/{total} = {wr:.0%}")
            print(f"  总PnL:      ${self.total_pnl:+.2f}")

        if total > 0:
            print(f"\n  交易明细:")
            settled = [(s, m) for s, m in self.markets.items() if m.get('outcome')]
            for slug, mkt in sorted(settled, key=lambda x: x[1]['epoch']):
                pnl = mkt.get('pnl', 0)
                exit_type = mkt.get('outcome', '')
                if exit_type == 'stop_loss':
                    icon = "🛑"
                    detail = (f"卖{mkt.get('trigger_side','')}@{mkt.get('trigger_price',0):.3f} "
                              f"买{mkt.get('buy_side','')}@{mkt.get('buy_price',0):.3f} │ "
                              f"止损@{mkt.get('exit_price',0):.3f}")
                else:
                    icon = "✅" if pnl >= 0 else "❌"
                    detail = (f"卖{mkt.get('trigger_side','')}@{mkt.get('trigger_price',0):.3f} "
                              f"买{mkt.get('buy_side','')}@{mkt.get('buy_price',0):.3f} │ "
                              f"→{mkt.get('outcome','')}")
                print(f"    {icon} [{mkt.get('asset','').upper()}] "
                      f"{detail} │ PnL=${pnl:+.2f}")

        print(f"\n  💾 数据: MySQL {os.getenv('MYSQL_DATABASE','poly')}")
        print(f"     mysql -u {os.getenv('MYSQL_USER','poly')} -p -h {os.getenv('MYSQL_HOST','127.0.0.1')} {os.getenv('MYSQL_DATABASE','poly')}")
        print(f"       .headers on")
        print(f"       SELECT * FROM smart_seller_trades WHERE status='settled';")


# ============================================================
# Main
# ============================================================

def main():
    parser = argparse.ArgumentParser(
        description="卖方策略 (Seller) — 买入高价方，等价于卖出低价方收权利金",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
示例:
  # Dry-run 测试 BTC
  python3 smart_seller_live.py --duration 2h

  # 实盘 $2, 4个资产
  python3 smart_seller_live.py --live --amount 2 --assets btc,eth,sol,xrp --duration 4h

  # 保守: 只在mid阶段, 阈值0.15
  python3 smart_seller_live.py --live --amount 2 --threshold 0.15 --phase mid

  # 小资金测试: $1, 最多5笔, 亏$5止损
  python3 smart_seller_live.py --live --amount 1 --max-trades 5 --max-loss 5

  # 不止损 (持有到结算)
  python3 smart_seller_live.py --live --amount 2 --sl-trigger 0
        """
    )

    parser.add_argument("--live", action="store_true",
                        help="实盘模式 (默认dry-run)")
    parser.add_argument("--assets", default="btc",
                        help="资产列表, 逗号分隔 (默认btc)")
    parser.add_argument("--amount", type=float, default=2.0,
                        help="每笔金额 (默认$2, 与 --shares 二选一)")
    parser.add_argument("--shares", type=float, default=0.0,
                        help="固定每笔份额数, >0 时优先于 --amount (例: --shares 10)")
    parser.add_argument("--threshold", type=float, default=0.20,
                        help="触发阈值: 低于此价格视为虚值 (默认0.20)")
    parser.add_argument("--min-threshold", type=float, default=0.05,
                        help="最低阈值: 低于此价格不交易,避免噪音 (默认0.05)")
    parser.add_argument("--min-elapsed", type=float, default=0.333,
                        help="入场窗口开始: 占窗口百分比 0~1 (默认0.333=33%%)")
    parser.add_argument("--max-elapsed", type=float, default=0.667,
                        help="入场窗口结束: 占窗口百分比 0~1 (默认0.667=67%%)")
    parser.add_argument("--max-buy-price", type=float, default=0.85,
                        help="最高买入价, 超过跳过 (默认0.85, 0.85+PnL下降)")
    parser.add_argument("--phase", default="mid",
                        choices=["all", "mid", "late", "mid_only"],
                        help="阶段过滤: all=不限, mid=mid+late(禁close), late=仅late, mid_only=仅mid (默认mid)")
    parser.add_argument("--max-trades", type=int, default=0,
                        help="最大交易数 (0=无限)")
    parser.add_argument("--max-loss", type=float, default=0,
                        help="止损金额 (0=无止损)")
    parser.add_argument("--limit-offset", type=float, default=0.01,
                        help="限价偏移 (默认0.01)")
    parser.add_argument("--sl-trigger", type=float, default=0.0,
                        help="止损触发: 低价方涨回此值时平仓 (0=不止损, 默认0.0)")
    parser.add_argument("--sl-confirm", type=int, default=3,
                        help="止损确认: 连续N次检测超过止损线才触发 (默认3, 防正常波动)")
    parser.add_argument("--sl-fast-track", type=float, default=0.60,
                        help="极端行情: 低价方≥此值立即止损, 跳过确认 (默认0.60)")
    parser.add_argument("--cooldown-losses", type=int, default=3,
                        help="连续N次止损后暂停交易1个窗口 (默认3)")
    parser.add_argument("--max-sl-loss", type=float, default=0.20,
                        help="单笔止损最大容许亏损比例 (默认0.20, 超过报警滑点)")
    parser.add_argument("--reprice-gap", type=float, default=0.02,
                        help="重挂单阈值 (默认0.02)")
    parser.add_argument("--require-bias-for-no", action="store_true",
                        help="买NO时要求明确的NO_ONLY偏向, 无偏向则跳过 (过滤看涨行情下的逆势买NO)")
    parser.add_argument("--night-skip", type=str, default=None,
                        help="夜间跳过UTC小时范围, 格式'start-end', 如'16-24'跳过UTC16~24(=CST00~08)")
    parser.add_argument("--tp-trigger", type=float, default=0.0,
                        help="止盈触发: 低价方跌到此值时提前卖出锁利 (0=不止盈, 建议0.05~0.08)")
    parser.add_argument("--tp-confirm", type=int, default=2,
                        help="止盈确认: 连续N次检测才触发 (默认2, 防止瞬时错误)")
    parser.add_argument("--tp-offset", type=float, default=0.01,
                        help="止盈卖价偏移, 略低于高价方mid提高成交率 (默认0.01)")
    parser.add_argument("--duration", type=str, default="0",
                        help="运行时长 (例: 30m, 2h)")

    args = parser.parse_args()

    # 解析duration
    dur = args.duration
    if dur.endswith('h'):
        duration_sec = float(dur[:-1]) * 3600
    elif dur.endswith('m'):
        duration_sec = float(dur[:-1]) * 60
    else:
        duration_sec = float(dur)

    # 解析 night_skip (格式: "start-end", UTC 小时)
    night_skip_start = night_skip_end = -1
    if args.night_skip:
        try:
            parts = args.night_skip.strip().split("-")
            night_skip_start = int(parts[0])
            night_skip_end = int(parts[1])
            if not (0 <= night_skip_start <= 24 and 0 <= night_skip_end <= 24):
                raise ValueError("小时必须在 0~24")
        except Exception as e:
            print(f"❌ --night-skip 格式错误: {e}，应为 'start-end'，如 '16-24'")
            return

    # 解析资产列表
    assets = [a.strip() for a in args.assets.split(',') if a.strip()]

    if args.live:
        print(f"\n⚠️  实盘模式!")
        print(f"  资产: {', '.join(a.upper() for a in assets)}")
        print(f"  每笔: ${args.amount}")
        print(f"  阈值: {args.threshold}")
        print(f"  阶段: {args.phase}")
        if args.max_trades > 0:
            print(f"  最多: {args.max_trades}笔")
        if args.max_loss > 0:
            print(f"  止损: ${args.max_loss}")
        if args.sl_trigger > 0:
            print(f"  单笔止损: 低价方≥{args.sl_trigger:.0%}")
        else:
            print(f"  单笔止损: 无(持有到期)")
        if args.tp_trigger > 0:
            print(f"  单笔止盈: 低价方≤{args.tp_trigger:.0%}")
        confirm = input("\n  确认开始? (y/N): ")
        if confirm.lower() != 'y':
            print("  已取消")
            return

    strategy = SmartSellerStrategy(
        threshold=args.threshold,
        min_threshold=args.min_threshold,
        bet_amount=args.amount,
        fixed_shares=args.shares,
        max_trades=args.max_trades,
        max_loss=args.max_loss,
        limit_offset=args.limit_offset,
        reprice_gap=args.reprice_gap,
        assets=assets,
        phase_filter=args.phase,
        min_elapsed_pct=args.min_elapsed,
        max_elapsed_pct=args.max_elapsed,
        max_buy_price=args.max_buy_price,
        sl_trigger=args.sl_trigger,
        sl_confirm=args.sl_confirm,
        sl_fast_track=args.sl_fast_track,
        cooldown_losses=args.cooldown_losses,
        max_sl_loss=args.max_sl_loss,
        require_bias_for_no=args.require_bias_for_no,
        night_skip_start=night_skip_start,
        night_skip_end=night_skip_end,
        tp_trigger=args.tp_trigger,
        tp_confirm=args.tp_confirm,
        tp_offset=args.tp_offset,
        dry_run=not args.live,
    )

    signal.signal(signal.SIGINT, lambda s, f: setattr(strategy, 'running', False))
    asyncio.run(strategy.run(duration_sec))


if __name__ == "__main__":
    main()
