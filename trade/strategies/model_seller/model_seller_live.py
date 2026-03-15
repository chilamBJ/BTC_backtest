#!/usr/bin/env python3
"""
模型策略 (Model Seller) — 实盘/模拟一体
=========================================
将今日回测思路实盘化到 trade 框架，支持 5m / 15m 市场。

核心流程:
1) 发现 Polymarket 市场 (5m/15m)
2) 在临近窗口结束时计算特征并做模型推理，得到 P(YES)
3) 按阈值买入 YES / NO
4) 持有到结算
"""

import argparse
import asyncio
import json
import os
import pickle
import signal
import time
import traceback
import warnings
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import pandas as pd
import aiohttp
from dotenv import load_dotenv

# 本项目：trade 为子目录，项目根为 BTC_backtest
TRADE_DIR = Path(__file__).resolve().parent.parent.parent
PROJECT_ROOT = TRADE_DIR.parent
import sys

sys.path.insert(0, str(TRADE_DIR))
from utils.db import get_conn, execute as db_execute, query as db_query, init_model_seller_tables


load_dotenv(PROJECT_ROOT / ".env")

TAKER_FEE = 0.02
MAKER_FEE = 0.0
MIN_ORDER_SIZE = 5
GAMMA_API = "https://gamma-api.polymarket.com/events"
GAMMA_API_SLUG_PATH = "https://gamma-api.polymarket.com/events/slug"  # 备选：路径式 GET /events/slug/{slug}
CLOB_HOST = "https://clob.polymarket.com"
CLOB_MIDPOINT = f"{CLOB_HOST}/midpoint"
POLYGON_CHAIN_ID = 137
BINANCE_KLINE = "https://api.binance.com/api/v3/klines"
WINDOW_MAP = {"5m": 300, "15m": 900}


def _patch_clob_http():
    try:
        import httpx
        from py_clob_client.http_helpers import helpers as _h

        _browser_ua = (
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/131.0.0.0 Safari/537.36"
        )
        transport = httpx.HTTPTransport(http2=True, local_address="0.0.0.0", retries=1)
        _h._http_client = httpx.Client(transport=transport, timeout=15.0)
        _orig = _h.overloadHeaders

        def _patched(method, headers):
            headers = _orig(method, headers)
            headers["User-Agent"] = _browser_ua
            return headers

        _h.overloadHeaders = _patched
        print("  ✅ CLOB HTTP: 浏览器UA + IPv4 模式")
    except Exception as exc:
        print(f"  ⚠️ HTTP patch 跳过: {exc}")


_patch_clob_http()


class TradingClient:
    """封装 py-clob-client，处理认证和下单。"""

    def __init__(self, dry_run: bool = True):
        self.dry_run = dry_run
        self.client = None
        self.address = None
        self._api_creds = None
        if not dry_run:
            self._init_clob_client()

    def _init_clob_client(self):
        from py_clob_client.client import ClobClient

        private_key = os.getenv("PRIVATE_KEY")
        if not private_key or private_key.startswith("0x000000000"):
            raise ValueError("❌ 请在 .env 中设置真实 PRIVATE_KEY")
        proxy_addr = os.getenv("PROXY_ADDRESS") or os.getenv("WALLET_ADDRESS")
        self.client = ClobClient(
            host=CLOB_HOST,
            chain_id=POLYGON_CHAIN_ID,
            key=private_key,
            signature_type=2,
            funder=proxy_addr,
        )
        self.address = self.client.get_address()
        self._api_creds = self.client.create_or_derive_api_creds()
        self.client.set_api_creds(self._api_creds)
        ok = self.client.get_ok()
        print(f"  API 连接: {'✅ OK' if ok == 'OK' else '❌ FAIL: ' + str(ok)}")

    def get_balance(self) -> Optional[dict]:
        if self.dry_run or not self.client:
            return {"balance": "dry-run", "allowance": "dry-run"}
        try:
            from py_clob_client.clob_types import AssetType, BalanceAllowanceParams

            params = BalanceAllowanceParams(asset_type=AssetType.COLLATERAL, signature_type=2)
            result = self.client.get_balance_allowance(params)
            raw = int(result.get("balance", "0"))
            return {"balance_usdc": raw / 1e6, "raw": result}
        except Exception as exc:
            print(f"  ⚠️ 获取余额失败: {exc}")
            return None

    def place_limit_buy(self, token_id: str, price: float, size: float) -> dict:
        if self.dry_run:
            return {
                "success": True,
                "order_id": f"dry-{int(time.time())}",
                "detail": f"dry-run limit buy {size:.2f}shares @{price:.3f}",
                "mode": "dry-run",
            }
        try:
            from py_clob_client.clob_types import OrderArgs, PartialCreateOrderOptions

            neg_risk = self.client.get_neg_risk(token_id)
            tick_size = self.client.get_tick_size(token_id)
            order_args = OrderArgs(token_id=token_id, price=price, size=size, side="BUY")
            options = PartialCreateOrderOptions(tick_size=tick_size, neg_risk=neg_risk)
            resp = self.client.create_and_post_order(order_args, options)
            return {
                "success": resp.get("success", False) if isinstance(resp, dict) else True,
                "order_id": resp.get("orderID", "") if isinstance(resp, dict) else str(resp),
                "detail": json.dumps(resp) if isinstance(resp, dict) else str(resp),
                "mode": "live",
            }
        except Exception as exc:
            return {
                "success": False,
                "order_id": "",
                "detail": f"ERROR: {exc}\n{traceback.format_exc()}",
                "mode": "live",
            }

    def get_order_status(self, order_id: str) -> Optional[dict]:
        if self.dry_run:
            return {"filled": True, "cancelled": False, "status": "MATCHED", "avg_price": 0, "size_matched": 0}
        if not self.client or not order_id:
            return None
        try:
            resp = self.client.get_order(order_id)
            if not resp:
                return None
            status = resp.get("status", "")
            return {
                "filled": status == "MATCHED",
                "cancelled": status == "CANCELLED",
                "status": status,
                "size_matched": float(resp.get("size_matched", "0") or "0"),
                "avg_price": float(resp.get("price", "0") or "0"),
                "raw": resp,
            }
        except Exception as exc:
            print(f"  ⚠️ 查询订单状态失败: {exc}")
            return None

    def cancel_order(self, order_id: str) -> bool:
        if self.dry_run or not self.client or not order_id:
            return True
        try:
            self.client.cancel(order_id)
            return True
        except Exception as exc:
            print(f"  ⚠️ 取消订单失败: {exc}")
            return False


class ModelEngine:
    """加载离线模型并执行概率推理。"""

    def __init__(self, model_path: str):
        self.model_path = model_path
        self.model = None
        self.feature_names: List[str] = []
        self._load()

    def _load(self):
        p = Path(self.model_path)
        if not p.exists():
            raise FileNotFoundError(f"模型文件不存在: {p}")
        with open(p, "rb") as f:
            with warnings.catch_warnings():
                warnings.simplefilter("ignore")
                self.model = pickle.load(f)
        if hasattr(self.model, "feature_name_"):
            self.feature_names = list(getattr(self.model, "feature_name_"))
        elif hasattr(self.model, "feature_names_in_"):
            self.feature_names = [str(x) for x in getattr(self.model, "feature_names_in_")]
        else:
            self.feature_names = ["feat_1", "feat_2", "feat_5"]

    @staticmethod
    def _default_feature(name: str) -> float:
        defaults = {
            "feat_1": 0.0,
            "feat_2": 1.0,
            "feat_3": 0.0,
            "feat_4": 0.0,
            "feat_5": 0.0,
            "feat_hour_sin": 0.0,
            "feat_hour_cos": 1.0,
            "feat_day_of_week": 0.0,
            "feat_session": 0.0,
            "feat_vol_regime": 1.0,
        }
        return float(defaults.get(name, 0.0))

    def predict_yes_prob(self, feature_dict: dict) -> float:
        if self.model is None:
            raise RuntimeError("模型未加载")
        row = [float(feature_dict.get(k, self._default_feature(k))) for k in self.feature_names]
        X = pd.DataFrame([row], columns=self.feature_names)
        probs = self.model.predict_proba(X)
        return float(probs[0][1])


def init_db():
    conn = get_conn()
    init_model_seller_tables(conn)
    return conn


class ModelSellerStrategy:
    def __init__(
        self,
        assets: List[str],
        market_window: str = "both",
        bet_amount: float = 2.0,
        prob_buy_yes: float = 0.60,
        prob_buy_no: float = 0.50,
        limit_offset: float = 0.01,
        reprice_gap: float = 0.02,
        max_trades: int = 0,
        max_loss: float = 0.0,
        model_path: str = "",
        dry_run: bool = True,
    ):
        self.assets = [a.lower() for a in (assets or ["btc"])]
        self.market_window = market_window
        self.bet_amount = bet_amount
        self.prob_buy_yes = prob_buy_yes
        self.prob_buy_no = prob_buy_no
        self.limit_offset = limit_offset
        self.reprice_gap = reprice_gap
        self.max_trades = max_trades
        self.max_loss = max_loss
        self.dry_run = dry_run
        self.model_path = model_path

        if market_window == "both":
            self.windows: List[Tuple[str, int]] = [("5m", 300), ("15m", 900)]
        else:
            self.windows = [(market_window, WINDOW_MAP[market_window])]

        self.trading = TradingClient(dry_run=dry_run)
        self.db = init_db()
        self.http_session: Optional[aiohttp.ClientSession] = None
        self.model = ModelEngine(model_path=model_path) if model_path else None

        self.markets: Dict[str, dict] = {}
        self.trade_count = 0
        self.total_pnl = 0.0
        self.wins = 0
        self.losses = 0
        self.running = True
        self.start_time = time.time()

        params = json.dumps(
            {
                "assets": self.assets,
                "market_window": market_window,
                "bet_amount": bet_amount,
                "prob_buy_yes": prob_buy_yes,
                "prob_buy_no": prob_buy_no,
                "limit_offset": limit_offset,
                "reprice_gap": reprice_gap,
                "max_trades": max_trades,
                "max_loss": max_loss,
                "model_path": model_path,
                "dry_run": dry_run,
            }
        )
        cur = db_execute(
            self.db,
            "INSERT INTO sessions (mode, params) VALUES (%s, %s)",
            ("dry-run" if dry_run else "live", params),
        )
        self.session_id = cur.lastrowid

    def _log(self, action: str, slug: str = "", asset: str = "", detail: dict = None, level: str = "info"):
        detail_json = json.dumps(detail or {}, ensure_ascii=False)
        db_execute(
            self.db,
            "INSERT INTO action_log (session_id, asset, market_slug, action, detail, level) VALUES (%s,%s,%s,%s,%s,%s)",
            (self.session_id, asset, slug, action, detail_json, level),
        )
        ts = datetime.now().strftime("%H:%M:%S")
        mode = "🏷DRY" if self.dry_run else "💰LIVE"
        lvl = {"info": "ℹ️", "warn": "⚠️", "error": "❌"}.get(level, "")
        msg = detail.get("msg", action) if detail else action
        asset_tag = f"[{asset.upper()}]" if asset else ""
        print(f"  [{ts}] {mode} {lvl} {asset_tag} {msg}")

    async def _get_json(self, url: str, params: dict = None):
        try:
            async with self.http_session.get(url, params=params, timeout=aiohttp.ClientTimeout(total=8)) as resp:
                if resp.status == 200:
                    return await resp.json()
        except Exception:
            return None
        return None

    async def _get_json_with_status(self, url: str, params: dict = None):
        """返回 (status_code, data) 便于打日志诊断。"""
        try:
            async with self.http_session.get(url, params=params, timeout=aiohttp.ClientTimeout(total=8)) as resp:
                body = None
                if resp.status == 200:
                    try:
                        body = await resp.json()
                    except Exception:
                        pass
                return (resp.status, body)
        except Exception:
            return (0, None)  # 0 表示网络/解析异常

    async def _fetch_binance_klines(self, asset: str, interval: str, limit: int = 60) -> List[list]:
        symbol = f"{asset.upper()}USDT"
        data = await self._get_json(BINANCE_KLINE, {"symbol": symbol, "interval": interval, "limit": limit})
        if isinstance(data, list):
            return data
        return []

    async def fetch_midpoint(self, token_id: str) -> Optional[float]:
        data = await self._get_json(CLOB_MIDPOINT, {"token_id": token_id})
        if data and "mid" in data:
            try:
                raw = str(data["mid"])
                cleaned = "".join(c for c in raw if c.isdigit() or c == ".")
                if cleaned:
                    val = float(cleaned)
                    if 0 <= val <= 1:
                        return val
            except (ValueError, TypeError):
                return None
        return None

    async def _fetch_gamma_event(self, slug: str) -> Tuple[int, Optional[dict]]:
        """请求 Gamma 获取 event，先试路径式再试查询参数。返回 (status, event_dict 或 None)。"""
        # 先试路径式: GET /events/slug/{slug}
        url_path = f"{GAMMA_API_SLUG_PATH}/{slug}"
        status, data = await self._get_json_with_status(url_path)
        if status == 200 and data:
            events = data if isinstance(data, list) else [data]
            if events:
                return (status, events[0] if isinstance(events[0], dict) else None)
        # 再试查询参数: GET /events?slug=xxx
        status2, data2 = await self._get_json_with_status(GAMMA_API, {"slug": slug})
        if status2 == 200 and data2:
            events = data2 if isinstance(data2, list) else [data2]
            if events:
                return (status2, events[0] if isinstance(events[0], dict) else None)
        return (status or status2, None)

    async def discover_markets(self) -> List[dict]:
        now_ts = int(time.time())
        found = []
        slugs_checked = []
        gamma_results = []  # 用于日志与 activity
        for asset in self.assets:
            for mtype, window in self.windows:
                base_epoch = (now_ts // window) * window
                for offset in [0, 1]:
                    epoch = base_epoch + offset * window
                    slug = f"{asset}-updown-{mtype}-{epoch}"
                    if slug in self.markets:
                        continue
                    elapsed = now_ts - epoch
                    if elapsed < -30 or elapsed > window + 60:
                        continue
                    slugs_checked.append(slug)
                    status, event = await self._fetch_gamma_event(slug)
                    gamma_results.append({"slug": slug, "status": status, "has_event": event is not None})
                    if not event:
                        if status == 200:
                            self._log("discover_skip", slug, asset, {"msg": f"Gamma 200 但无 event (slug 可能不存在)", "status": status})
                        else:
                            self._log("discover_skip", slug, asset, {"msg": f"Gamma 请求 status={status}", "status": status})
                        continue
                    if event.get("closed"):
                        self._log("discover_skip", slug, asset, {"msg": "event 已 closed"})
                        continue
                    markets_list = event.get("markets", [])
                    if not markets_list:
                        self._log("discover_skip", slug, asset, {"msg": "event 无 markets[]"})
                        continue
                    mkt = markets_list[0]
                    tokens = mkt.get("clobTokenIds", [])
                    if isinstance(tokens, str):
                        try:
                            tokens = json.loads(tokens)
                        except Exception:
                            tokens = []
                    if not isinstance(tokens, list) or len(tokens) < 2:
                        self._log("discover_skip", slug, asset, {"msg": "无有效 clobTokenIds"})
                        continue
                    found.append(
                        {
                            "slug": slug,
                            "epoch": epoch,
                            "asset": asset,
                            "market_type": mtype,
                            "window_sec": window,
                            "yes_token": tokens[0],
                            "no_token": tokens[1],
                            "question": mkt.get("question", ""),
                        }
                    )
        # 每轮发现阶段打一行汇总，便于看“是否在工作”
        if slugs_checked:
            n_ok = sum(1 for r in gamma_results if r.get("has_event"))
            summary = f"发现阶段: 检查 {len(slugs_checked)} 个 slug, Gamma 返回有效 event {n_ok} 次, 本轮新发现 {len(found)} 个市场"
            self._log("discover_summary", "", "", {"msg": summary, "slugs_checked": slugs_checked, "found_count": len(found)})
        return found, slugs_checked

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
                        if yes_p < 0.15:
                            return "NO"
                except (ValueError, TypeError):
                    return None
        return None

    @staticmethod
    def _calc_feat5_from_klines(klines: List[list]) -> float:
        if len(klines) < 2:
            return 0.0
        closes = [float(k[4]) for k in klines]
        highs = [float(k[2]) for k in klines]
        lows = [float(k[3]) for k in klines]
        vols = [float(k[5]) for k in klines]
        if len(closes) > 48:
            closes = closes[-48:]
            highs = highs[-48:]
            lows = lows[-48:]
            vols = vols[-48:]
        typical = [(highs[i] + lows[i] + closes[i]) / 3.0 for i in range(len(closes))]
        pv = sum(typical[i] * vols[i] for i in range(len(closes)))
        vv = sum(vols)
        if vv <= 0:
            return 0.0
        vwap_4h = pv / vv
        if vwap_4h <= 0:
            return 0.0
        last_close = closes[-2] if len(closes) >= 2 else closes[-1]
        return (last_close - vwap_4h) / vwap_4h * 10000

    @staticmethod
    def _calc_event_condition_from_klines(klines: List[list]) -> bool:
        if len(klines) < 4:
            return False
        bar1 = klines[-3]
        bar2 = klines[-2]
        o1, c1 = float(bar1[1]), float(bar1[4])
        o2, c2 = float(bar2[1]), float(bar2[4])
        up = c1 > o1 and c2 > o2 and c2 > c1
        down = c1 < o1 and c2 < o2 and c2 < c1
        if not (up or down):
            return False
        if o1 <= 0:
            return False
        total_move = abs((c2 - o1) / o1)
        return total_move > 0.003

    @staticmethod
    def _calc_proxy_feat1_feat2(price_log: List[dict], elapsed: int) -> Tuple[float, float]:
        if not price_log:
            return 0.0, 1.0
        last60 = [p for p in price_log if elapsed - 60 <= p["t"] <= elapsed]
        prev240 = [p for p in price_log if elapsed - 300 <= p["t"] < elapsed - 60]
        feat1 = 0.0
        if len(last60) >= 2:
            dy = float(last60[-1]["y"]) - float(last60[0]["y"])
            feat1 = max(-1.0, min(1.0, dy * 10.0))
        c_last = len(last60)
        c_prev = len(prev240)
        feat2 = (c_last / 60.0) / (c_prev / 240.0) if c_prev > 0 else 1.0
        return feat1, feat2

    async def _build_features(self, mkt: dict, elapsed: int, yes_mid: float, no_mid: float) -> dict:
        mtype = mkt.get("market_type", "15m")
        interval = "5m" if mtype == "5m" else "15m"
        klines = await self._fetch_binance_klines(mkt["asset"], interval=interval, limit=60)
        feat5 = self._calc_feat5_from_klines(klines)
        feat1, feat2 = self._calc_proxy_feat1_feat2(mkt.get("price_log", []), elapsed)
        now = datetime.now(timezone.utc)
        hour = now.hour + now.minute / 60.0
        feat_hour_sin = __import__("math").sin(2 * __import__("math").pi * hour / 24.0)
        feat_hour_cos = __import__("math").cos(2 * __import__("math").pi * hour / 24.0)
        feat_session = 1 if hour < 8 else (2 if hour < 13.5 else (3 if hour < 22 else 0))
        feat_day_of_week = now.weekday()
        return {
            "feat_1": feat1,
            "feat_2": feat2,
            "feat_3": 0.0,
            "feat_4": 0.0,
            "feat_5": feat5,
            "feat_hour_sin": feat_hour_sin,
            "feat_hour_cos": feat_hour_cos,
            "feat_day_of_week": float(feat_day_of_week),
            "feat_session": float(feat_session),
            "feat_vol_regime": 1.0,
            "_event_ok": self._calc_event_condition_from_klines(klines),
            "_yes_mid": yes_mid,
            "_no_mid": no_mid,
        }

    def _model_decide(self, features: dict, elapsed: int, mkt: dict) -> Optional[dict]:
        if not features.get("_event_ok"):
            return None
        if self.model is None:
            return None
        p_yes = self.model.predict_yes_prob(features)
        if p_yes >= self.prob_buy_yes:
            return {
                "trigger_side": "MODEL",
                "trigger_price": p_yes,
                "buy_side": "YES",
                "buy_token_key": "yes_token",
                "buy_price": features["_yes_mid"],
                "phase": "decision",
                "p_yes": p_yes,
                "elapsed": elapsed,
            }
        if p_yes <= self.prob_buy_no:
            return {
                "trigger_side": "MODEL",
                "trigger_price": p_yes,
                "buy_side": "NO",
                "buy_token_key": "no_token",
                "buy_price": features["_no_mid"],
                "phase": "decision",
                "p_yes": p_yes,
                "elapsed": elapsed,
            }
        return None

    def _settle(self, slug: str, mkt: dict, outcome: str):
        buy_side = mkt.get("buy_side")
        buy_price = mkt.get("buy_price", 0)
        amount = mkt.get("buy_amount", self.bet_amount)
        if buy_side and buy_price > 0:
            shares = amount / buy_price
            pnl = (shares - amount) if outcome == buy_side else -amount
        else:
            pnl = 0.0
        mkt["status"] = "settled"
        mkt["outcome"] = outcome
        mkt["pnl"] = pnl
        self.total_pnl += pnl
        if pnl >= 0:
            self.wins += 1
        else:
            self.losses += 1
        self._log(
            "settle",
            slug,
            mkt.get("asset", ""),
            {
                "msg": f"✅ 结算 {outcome} PnL=${pnl:+.2f} 累计${self.total_pnl:+.2f}",
                "outcome": outcome,
                "pnl": pnl,
            },
        )
        db_execute(
            self.db,
            "UPDATE model_seller_trades SET outcome=%s, pnl=%s, settled_at=NOW(), settle_method='api_price', "
            "exit_type='settlement', status='settled', price_log=%s WHERE id=%s",
            (outcome, pnl, json.dumps(mkt.get("price_log", [])), mkt.get("db_id")),
        )

    async def _try_settle(self, slug: str, mkt: dict):
        outcome = await self._resolve_via_gamma(slug)
        if outcome in ("YES", "NO"):
            self._settle(slug, mkt, outcome)
            return
        elapsed = int(time.time()) - mkt["epoch"]
        if elapsed > mkt["window_sec"] + 180:
            last_y = mkt["price_log"][-1]["y"] if mkt.get("price_log") else 0.5
            forced = "YES" if last_y >= 0.5 else "NO"
            self._log("force_settle", slug, mkt.get("asset", ""), {"msg": f"⏰ 强制结算 →{forced}"}, level="warn")
            self._settle(slug, mkt, forced)

    async def run(self, duration_sec: float = 0):
        proxy_url = (
            os.environ.get("HTTPS_PROXY") or os.environ.get("HTTP_PROXY") or os.environ.get("ALL_PROXY") or ""
        ).strip()
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
            connector=connector, trust_env=bool(proxy_url and "socks" not in proxy_url.lower())
        )

        mode = "🏷️ DRY-RUN" if self.dry_run else "💰 LIVE TRADING"
        print("\n" + "=" * 60)
        print(f"🧠 模型策略 (Model Seller) {mode}")
        print("=" * 60)
        print(f"  资产:       {', '.join(a.upper() for a in self.assets)}")
        print(f"  市场周期:   {self.market_window}")
        print(f"  阈值:       P(YES)>={self.prob_buy_yes:.2f} 买YES, P(YES)<={self.prob_buy_no:.2f} 买NO")
        print(f"  每笔金额:   ${self.bet_amount:.2f}")
        print(f"  模型路径:   {self.model_path or '未配置'}")
        print(f"  数据库:     MySQL ({os.getenv('MYSQL_HOST','127.0.0.1')}:{os.getenv('MYSQL_PORT','3306')}/{os.getenv('MYSQL_DATABASE','poly')})")
        if self.model is None:
            print("  ⚠️ 模型未加载，策略仅监控不下单")
        if not self.dry_run:
            balance = self.trading.get_balance()
            if balance:
                print(f"  余额:       {balance}")
        print("=" * 60 + "\n")

        self._log(
            "session_start",
            detail={
                "msg": f"模型策略启动 {mode}",
                "assets": self.assets,
                "market_window": self.market_window,
                "prob_buy_yes": self.prob_buy_yes,
                "prob_buy_no": self.prob_buy_no,
            },
        )
        tick_count = 0
        try:
            while self.running:
                if duration_sec > 0 and time.time() - self.start_time > duration_sec:
                    print("\n⏰ 到达运行时限")
                    break

                if self.max_loss > 0 and self.total_pnl <= -self.max_loss:
                    active = [s for s, m in self.markets.items() if m.get("status") in ("entered", "order_pending")]
                    if not active:
                        print(f"\n🛑 止损! 亏损${-self.total_pnl:.2f} ≥ ${self.max_loss:.2f}")
                        break

                now_ts = int(time.time())
                self._decisions_this_round = []
                discovered_list, slugs_checked_this_round = await self.discover_markets()
                for info in discovered_list:
                    slug = info["slug"]
                    if slug in self.markets:
                        continue
                    elapsed = now_ts - info["epoch"]
                    self.markets[slug] = {
                        **info,
                        "status": "watching",
                        "price_log": [],
                        "db_id": None,
                        "order_retries": 0,
                        "decision_done": False,
                    }
                    # 按 slug 唯一：已存在则更新归属到当前 session，不存在才插入，绝不允许重复
                    existing = db_query(self.db, "SELECT id FROM model_seller_trades WHERE slug = %s LIMIT 1", (slug,))
                    if existing:
                        row_id = int(existing[0]["id"])
                        db_execute(
                            self.db,
                            "UPDATE model_seller_trades SET session_id=%s, asset=%s, market_type=%s, epoch=%s, "
                            "yes_token=%s, no_token=%s, question=%s, status='watching', discovered_at=NOW() WHERE id=%s",
                            (
                                self.session_id,
                                info["asset"].upper(),
                                info["market_type"],
                                info["epoch"],
                                info["yes_token"],
                                info["no_token"],
                                info["question"],
                                row_id,
                            ),
                        )
                        self.markets[slug]["db_id"] = row_id
                        self._log("discover", slug, info["asset"], {"msg": f"🔄 复用市场 {info['market_type']} (elapsed={elapsed}s)"})
                    else:
                        cur = db_execute(
                            self.db,
                            "INSERT INTO model_seller_trades "
                            "(session_id, asset, market_type, slug, epoch, yes_token, no_token, question, status) "
                            "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)",
                            (
                                self.session_id,
                                info["asset"].upper(),
                                info["market_type"],
                                slug,
                                info["epoch"],
                                info["yes_token"],
                                info["no_token"],
                                info["question"],
                                "watching",
                            ),
                        )
                        self.markets[slug]["db_id"] = cur.lastrowid
                        self._log("discover", slug, info["asset"], {"msg": f"🆕 发现市场 {info['market_type']} (elapsed={elapsed}s)"})

                for slug, mkt in list(self.markets.items()):
                    if mkt["status"] in ("settled", "skipped", "error"):
                        continue
                    elapsed = now_ts - mkt["epoch"]
                    window = int(mkt["window_sec"])
                    asset = mkt.get("asset", "")
                    if elapsed > window + 120:
                        if mkt["status"] == "entered":
                            await self._try_settle(slug, mkt)
                        elif mkt["status"] == "order_pending":
                            self.trading.cancel_order(mkt.get("order_id", ""))
                            mkt["status"] = "skipped"
                            db_execute(self.db, "UPDATE model_seller_trades SET status='skipped' WHERE id=%s", (mkt["db_id"],))
                        continue
                    if elapsed < 0:
                        continue

                    yes_mid = await self.fetch_midpoint(mkt["yes_token"])
                    if yes_mid is None:
                        continue
                    no_mid = round(1.0 - yes_mid, 6)
                    mkt["price_log"].append({"t": elapsed, "y": round(yes_mid, 4), "n": round(no_mid, 4)})

                    if mkt["status"] == "watching":
                        if self.max_trades > 0 and self.trade_count >= self.max_trades:
                            continue
                        decision_sec = window - 1
                        if (not mkt.get("decision_done")) and elapsed >= max(1, decision_sec - 12):
                            mkt["decision_done"] = True
                            features = await self._build_features(mkt, elapsed, yes_mid=yes_mid, no_mid=no_mid)
                            decision = self._model_decide(features, elapsed, mkt)
                            if decision:
                                p_yes = float(decision["p_yes"])
                                no_trigger_reason = None
                            else:
                                p_yes = float(self.model.predict_yes_prob(features)) if self.model else 0.0
                                if self.model is None:
                                    no_trigger_reason = "no_model"
                                elif not features.get("_event_ok"):
                                    no_trigger_reason = "event_ok_false"
                                else:
                                    no_trigger_reason = "prob_in_range"
                            if decision:
                                mkt["trigger_side"] = "MODEL"
                                mkt["trigger_price"] = p_yes
                                buy_side = decision["buy_side"]
                                buy_mid = decision["buy_price"]
                                limit_price = max(0.01, round(buy_mid - self.limit_offset, 4))
                                token_id = mkt[decision["buy_token_key"]]
                                size = round(self.bet_amount / limit_price, 4)
                                if size < MIN_ORDER_SIZE:
                                    size = MIN_ORDER_SIZE
                                    actual_cost = size * limit_price
                                else:
                                    actual_cost = self.bet_amount
                                self._log(
                                    "trigger",
                                    slug,
                                    asset,
                                    {"msg": f"🎯 模型触发 P(YES)={p_yes:.3f} → 买{buy_side} @{limit_price:.3f}"},
                                )
                                self._decisions_this_round.append({"slug": slug, "decision": f"buy_{buy_side.lower()}", "p_yes": round(p_yes, 4), "market_type": mkt.get("market_type", "")})
                                result = self.trading.place_limit_buy(token_id=token_id, price=limit_price, size=size)
                                if result["success"]:
                                    mkt["status"] = "order_pending"
                                    mkt["buy_side"] = buy_side
                                    mkt["buy_price"] = limit_price
                                    mkt["buy_amount"] = actual_cost
                                    mkt["buy_shares"] = size
                                    mkt["order_id"] = result.get("order_id", "")
                                    db_execute(
                                        self.db,
                                        "UPDATE model_seller_trades SET status='order_pending', trigger_side=%s, trigger_price=%s, "
                                        "trigger_elapsed=%s, trigger_phase=%s, p_yes=%s, feat_1=%s, feat_2=%s, feat_5=%s, "
                                        "buy_side=%s, buy_price=%s, buy_amount=%s, buy_shares=%s, order_id=%s, order_response=%s WHERE id=%s",
                                        (
                                            "MODEL",
                                            p_yes,
                                            elapsed,
                                            "decision",
                                            p_yes,
                                            features.get("feat_1"),
                                            features.get("feat_2"),
                                            features.get("feat_5"),
                                            buy_side,
                                            limit_price,
                                            actual_cost,
                                            size,
                                            result.get("order_id", ""),
                                            json.dumps(result),
                                            mkt["db_id"],
                                        ),
                                    )
                                else:
                                    self._log("order_failed", slug, asset, {"msg": f"❌ 下单失败: {result.get('detail','')[:160]}"}, level="error")
                                    db_execute(
                                        self.db,
                                        "UPDATE model_seller_trades SET p_yes=%s, feat_1=%s, feat_2=%s, feat_5=%s, order_response=%s WHERE id=%s",
                                        (p_yes, features.get("feat_1"), features.get("feat_2"), features.get("feat_5"), json.dumps(result), mkt["db_id"]),
                                    )
                            else:
                                db_execute(
                                    self.db,
                                    "UPDATE model_seller_trades SET p_yes=%s, feat_1=%s, feat_2=%s, feat_5=%s, no_trigger_reason=%s WHERE id=%s",
                                    (
                                        p_yes,
                                        features.get("feat_1"),
                                        features.get("feat_2"),
                                        features.get("feat_5"),
                                        no_trigger_reason,
                                        mkt["db_id"],
                                    ),
                                )
                                if elapsed > window - 8:
                                    mkt["status"] = "skipped"
                                    db_execute(
                                        self.db,
                                        "UPDATE model_seller_trades SET status='skipped', skip_reason='no_signal' WHERE id=%s",
                                        (mkt["db_id"],),
                                    )
                                    self._log("no_trigger", slug, asset, {"msg": "⏭️ 模型未给出交易信号"})
                                    self._decisions_this_round.append({"slug": slug, "decision": "no_signal", "p_yes": round(p_yes, 4), "market_type": mkt.get("market_type", "")})

                    elif mkt["status"] == "order_pending":
                        fill_info = self.trading.get_order_status(mkt.get("order_id", ""))
                        if fill_info and fill_info.get("filled"):
                            mkt["status"] = "entered"
                            self.trade_count += 1
                            avg_price = fill_info.get("avg_price")
                            if avg_price and avg_price > 0:
                                mkt["buy_price"] = avg_price
                            size_matched = fill_info.get("size_matched", 0)
                            if size_matched and size_matched > 0:
                                mkt["buy_shares"] = size_matched
                            self._log(
                                "order_filled",
                                slug,
                                asset,
                                {"msg": f"✅ 成交! 买{mkt['buy_side']}@{mkt['buy_price']:.3f} {mkt.get('buy_shares',0):.2f}股"},
                            )
                            db_execute(
                                self.db,
                                "UPDATE model_seller_trades SET status='entered', entry_at=NOW(), order_success=1 WHERE id=%s",
                                (mkt["db_id"],),
                            )
                        elif elapsed > window - 20:
                            self.trading.cancel_order(mkt.get("order_id", ""))
                            mkt["status"] = "skipped"
                            db_execute(self.db, "UPDATE model_seller_trades SET status='skipped', skip_reason='order_timeout' WHERE id=%s", (mkt["db_id"],))
                            self._log("order_timeout", slug, asset, {"msg": "⏭️ 订单超时，取消"})

                    elif mkt["status"] == "entered":
                        if elapsed > window - 5:
                            await self._try_settle(slug, mkt)

                active_count = len([m for m in self.markets.values() if m.get("status") in ("watching", "order_pending", "entered")])
                decisions_summary = getattr(self, "_decisions_this_round", [])[-30:]
                db_execute(
                    self.db,
                    "INSERT INTO model_seller_activity (session_id, markets_watching, slugs_checked, discovery_found, decisions_summary) "
                    "VALUES (%s,%s,%s,%s,%s)",
                    (self.session_id, active_count, len(slugs_checked_this_round), len(discovered_list), json.dumps(decisions_summary, ensure_ascii=False)),
                )

                tick_count += 1
                if tick_count % 10 == 0:
                    active = [m for m in self.markets.values() if m.get("status") in ("watching", "order_pending", "entered")]
                    print(
                        f"  ┌─ [{datetime.now().strftime('%H:%M:%S')}] 模型策略 · 运行{(time.time()-self.start_time)/60:.0f}min "
                        f"监控:{len(active)} 交易:{self.trade_count} 胜{self.wins}/负{self.losses} PnL:${self.total_pnl:+.2f}"
                    )
                await asyncio.sleep(4)
        finally:
            if self.http_session:
                await self.http_session.close()
            self._finalize()

    def _finalize(self):
        db_execute(
            self.db,
            "UPDATE sessions SET ended_at=NOW(), total_trades=%s, total_pnl=%s, summary=%s WHERE id=%s",
            (
                self.trade_count,
                self.total_pnl,
                json.dumps(
                    {
                        "wins": self.wins,
                        "losses": self.losses,
                        "pnl": self.total_pnl,
                        "strategy": "model_seller",
                        "market_window": self.market_window,
                    },
                    ensure_ascii=False,
                ),
                self.session_id,
            ),
        )
        mode = "DRY-RUN" if self.dry_run else "LIVE"
        print("\n" + "=" * 60)
        print("📊 模型策略 · 会话报告")
        print("=" * 60)
        print(f"  模式:       {mode}")
        print(f"  运行时间:   {(time.time()-self.start_time)/60:.1f} 分钟")
        print(f"  资产:       {', '.join(a.upper() for a in self.assets)}")
        print(f"  交易数:     {self.trade_count}")
        print(f"  胜负:       {self.wins}/{self.losses}")
        print(f"  总盈亏:     ${self.total_pnl:+.2f}")
        print(f"  💾 数据: MySQL {os.getenv('MYSQL_DATABASE','poly')}")


def _parse_duration(dur: str) -> float:
    if dur.endswith("h"):
        return float(dur[:-1]) * 3600
    if dur.endswith("m"):
        return float(dur[:-1]) * 60
    return float(dur)


def main():
    parser = argparse.ArgumentParser(description="模型策略 (model_seller)")
    parser.add_argument("--live", action="store_true", help="实盘模式 (默认dry-run)")
    parser.add_argument("--assets", default="btc", help="资产列表, 逗号分隔 (默认btc)")
    parser.add_argument("--amount", type=float, default=2.0, help="每笔金额 (默认$2)")
    parser.add_argument("--market-window", choices=["5m", "15m", "both"], default="both", help="市场周期")
    parser.add_argument("--prob-buy-yes", type=float, default=0.60, help="P(YES) >= 该值时买YES")
    parser.add_argument("--prob-buy-no", type=float, default=0.50, help="P(YES) <= 该值时买NO")
    parser.add_argument("--limit-offset", type=float, default=0.01, help="限价偏移")
    parser.add_argument("--reprice-gap", type=float, default=0.02, help="重挂阈值")
    parser.add_argument("--max-trades", type=int, default=0, help="最大交易数 (0=无限)")
    parser.add_argument("--max-loss", type=float, default=0, help="止损金额 (0=无)")
    parser.add_argument("--model-path", default=os.getenv("MODEL_SELLER_MODEL_PATH", ""), help="模型文件路径(pickle)")
    parser.add_argument("--duration", type=str, default="0", help="运行时长 (例: 30m, 2h)")
    args = parser.parse_args()

    assets = [a.strip() for a in args.assets.split(",") if a.strip()]
    duration_sec = _parse_duration(args.duration)

    if args.live:
        print("\n⚠️  实盘模式!")
        print(f"  资产: {', '.join(a.upper() for a in assets)}")
        print(f"  每笔: ${args.amount}")
        print(f"  周期: {args.market_window}")
        print(f"  阈值: 买YES>={args.prob_buy_yes}, 买NO<={args.prob_buy_no}")
        confirm = input("\n  确认开始? (y/N): ")
        if confirm.lower() != "y":
            print("  已取消")
            return

    strategy = ModelSellerStrategy(
        assets=assets,
        market_window=args.market_window,
        bet_amount=args.amount,
        prob_buy_yes=args.prob_buy_yes,
        prob_buy_no=args.prob_buy_no,
        limit_offset=args.limit_offset,
        reprice_gap=args.reprice_gap,
        max_trades=args.max_trades,
        max_loss=args.max_loss,
        model_path=args.model_path,
        dry_run=not args.live,
    )

    signal.signal(signal.SIGINT, lambda _s, _f: setattr(strategy, "running", False))
    asyncio.run(strategy.run(duration_sec))


if __name__ == "__main__":
    main()

