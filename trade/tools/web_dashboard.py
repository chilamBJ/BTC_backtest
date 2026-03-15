#!/usr/bin/env python3
"""
交易监控 Web Dashboard
======================
读取 trend_live.py 生成的 SQLite 数据库，展示实时交易数据。

用法:
  source venv/bin/activate
  python3 web_dashboard.py                     # 自动选最新DB, 端口8080
  python3 web_dashboard.py --port 9090         # 指定端口
  python3 web_dashboard.py --db data/xxx.db    # 指定DB
  python3 web_dashboard.py --all               # 聚合所有DB

无额外依赖，仅使用 Python 标准库。
"""

import argparse
import json
import os
import sys
import urllib.request
from datetime import datetime
try:
    from http.server import ThreadingHTTPServer as HTTPServer
except ImportError:
    from http.server import HTTPServer
from http.server import BaseHTTPRequestHandler
from pathlib import Path
from urllib.parse import urlparse, parse_qs

# 本项目：trade 为子目录，项目根为 BTC_backtest
TRADE_DIR = Path(__file__).resolve().parent.parent
PROJECT_ROOT = TRADE_DIR.parent
sys.path.insert(0, str(TRADE_DIR))
DATA_DIR = PROJECT_ROOT / "data"

from dotenv import load_dotenv
load_dotenv(PROJECT_ROOT / ".env")

from utils.db import query as db_query

# ============================================================
# 钱包余额查询
# ============================================================

# Polygon USDC.e 合约 (Polymarket 使用)
USDC_CONTRACT = "0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174"
# balanceOf(address) function selector
BALANCE_OF_SIG = "0x70a08231"
# 公共 RPC 备选列表（已验证可从国内/海外服务器免费调用）
POLYGON_RPCS = [
    "https://polygon-bor-rpc.publicnode.com",
    "https://polygon.drpc.org",
    "https://polygon.meowrpc.com",
    "https://1rpc.io/matic",
]

def _rpc_call(rpc_url, payload_bytes):
    """发送一次 RPC 请求，成功返回 result，失败抛异常"""
    req = urllib.request.Request(rpc_url, data=payload_bytes,
                                headers={"Content-Type": "application/json",
                                         "User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=6) as resp:
        body = json.loads(resp.read())
        if "error" in body:
            raise RuntimeError(body["error"].get("message", str(body["error"])))
        return body["result"]

def _query_usdc_balance(addr, rpcs):
    """查询地址的 USDC 余额，自动尝试多个 RPC"""
    padded = "0" * 24 + addr.lower().replace("0x", "")
    data = BALANCE_OF_SIG + padded
    payload = json.dumps({
        "jsonrpc": "2.0", "id": 1, "method": "eth_call",
        "params": [{"to": USDC_CONTRACT, "data": data}, "latest"]
    }).encode()
    last_err = None
    for rpc in rpcs:
        try:
            raw = _rpc_call(rpc, payload)
            return int(raw, 16) / 1e6  # USDC 6 decimals
        except Exception as e:
            last_err = e
    raise last_err  # type: ignore

import time

_balance_cache = {"data": None, "ts": 0}

def get_wallet_balance():
    """通过 RPC 查询 PROXY_ADDRESS 的 USDC 余额（即 Polymarket 可用资金）"""
    global _balance_cache
    now = time.time()
    if now - _balance_cache["ts"] < 30 and _balance_cache["data"]:
        return _balance_cache["data"]

    configured_rpc = os.getenv("RPC_URL", "")
    rpcs = ([configured_rpc] if configured_rpc else []) + POLYGON_RPCS
    # 去重保序
    seen = set()
    rpcs = [r for r in rpcs if r and r not in seen and not seen.add(r)]  # type: ignore

    proxy = os.getenv("PROXY_ADDRESS", "")
    wallet = os.getenv("WALLET_ADDRESS", "")
    if not proxy and not wallet:
        return {"error": "未配置 PROXY_ADDRESS / WALLET_ADDRESS"}

    result = {"wallet": wallet, "proxy": proxy}
    for label, addr in [("proxy_usdc", proxy), ("wallet_usdc", wallet)]:
        if not addr:
            continue
        try:
            result[label] = _query_usdc_balance(addr, rpcs)
        except Exception as e:
            result[label] = None
            result[label + "_error"] = str(e)
            
    _balance_cache["data"] = result
    _balance_cache["ts"] = now
    return result


# ============================================================
# 数据查询
# ============================================================

def get_available_strategies():
    """返回有数据表的策略列表（本项目仅 seller / smart_seller）"""
    strategies = []
    try:
        tables = db_query(None, "SHOW TABLES")
        table_names = {list(t.values())[0] for t in tables}
        if 'seller_trades' in table_names:
            strategies.append('seller')
        if 'smart_seller_trades' in table_names:
            strategies.append('smart_seller')
    except Exception:
        pass
    return strategies


def query_db(sql, params=()):
    """查询 MySQL，返回 list[dict]"""
    return db_query(None, sql, params)


def get_dashboard_data(strategy="seller", session_id=None):
    """汇集所有展示数据, 按策略和session过滤"""
    avail = get_available_strategies()
    if strategy not in avail:
        return {"error": f"策略 '{strategy}' 数据表不存在", "strategies": avail,
                "sessions": [], "current_strategy": strategy, "current_session": "all"}

    all_sessions = []
    all_markets = []
    all_actions = []

    sessions = query_db("SELECT * FROM sessions ORDER BY id")
    all_sessions.extend(sessions)

    # session 过滤条件
    sid_filter = ""
    sid_params = ()
    if session_id is not None and session_id != "all":
        sid_filter = " AND session_id = %s"
        sid_params = (int(session_id),)

    if strategy == 'seller':
        markets = query_db(f"""
            SELECT id, slug, asset, epoch, question, session_id,
                   trigger_side, trigger_price, trigger_elapsed, trigger_phase,
                   buy_side AS entry_side, buy_price AS entry_price,
                   buy_amount AS entry_amount, buy_shares,
                   entry_at,
                   order_id, order_success,
                   outcome, pnl, settled_at, settle_method,
                   status, discovered_at,
                   'seller' AS strategy_type,
                   exit_type, exit_price, sl_reason, skip_reason
            FROM seller_trades WHERE status != 'skipped' {sid_filter}
            AND id IN (SELECT MAX(id) FROM seller_trades WHERE status != 'skipped' {sid_filter} GROUP BY slug)
            ORDER BY id
        """, sid_params + sid_params)
        for m in markets:
            m["entry_confidence"] = m.get("trigger_price")
            m["entry_decision"] = f"sell_{m.get('trigger_side', '').lower()}" if m.get('trigger_side') else None
            m["entry_momentum"] = None
        all_markets.extend(markets)
    elif strategy == 'smart_seller':
        markets = query_db(f"""
            SELECT id, slug, asset, epoch, question, session_id,
                   trigger_side, trigger_price, trigger_elapsed, trigger_phase,
                   buy_side AS entry_side, buy_price AS entry_price,
                   buy_amount AS entry_amount, buy_shares,
                   entry_at,
                   order_id, order_success,
                   outcome, pnl, settled_at, settle_method,
                   status, discovered_at,
                   'smart_seller' AS strategy_type,
                   exit_type, exit_price, sl_reason, skip_reason
            FROM smart_seller_trades WHERE status != 'skipped' {sid_filter}
            AND id IN (SELECT MAX(id) FROM smart_seller_trades WHERE status != 'skipped' {sid_filter} GROUP BY slug)
            ORDER BY id
        """, sid_params + sid_params)
        for m in markets:
            m["entry_confidence"] = m.get("trigger_price")
            m["entry_decision"] = f"sell_{m.get('trigger_side', '').lower()}" if m.get('trigger_side') else None
            m["entry_momentum"] = None
        all_markets.extend(markets)
    actions = query_db(f"""
        SELECT id, ts, market_slug, action, detail, level
        FROM action_log WHERE 1=1 {sid_filter} ORDER BY id
    """, sid_params)
    all_actions.extend(actions)

    # 统计
    entered = [m for m in all_markets if m.get("status") in ("entered", "settled")]
    settled = [m for m in all_markets if m.get("status") == "settled" and m.get("pnl") is not None]
    wins = [m for m in settled if m["pnl"] > 0]
    losses = [m for m in settled if m["pnl"] <= 0]
    total_pnl = sum(m["pnl"] for m in settled) if settled else 0
    total_cost = sum(m.get("entry_amount") or 0 for m in entered)
    win_rate = len(wins) / len(settled) * 100 if settled else 0

    # 止损分析 (seller策略)
    sl_trades = [m for m in settled if m.get("exit_type") == "stop_loss"]
    settle_trades = [m for m in settled if m.get("exit_type") != "stop_loss"]
    sl_pnl = sum(m["pnl"] for m in sl_trades) if sl_trades else 0
    settle_pnl = sum(m["pnl"] for m in settle_trades) if settle_trades else 0
    sl_normal = [m for m in sl_trades if m.get("sl_reason") == "normal"]
    sl_fast = [m for m in sl_trades if m.get("sl_reason") == "fast_track"]
    skipped_reentry = [m for m in all_markets if m.get("skip_reason") == "reentry_block"]
    skipped_cooldown = [m for m in all_markets if m.get("skip_reason") == "cooldown"]

    # 止损滑点分析
    sl_slippage = []
    for m in sl_trades:
        bp = m.get("entry_price") or 0
        ep = m.get("exit_price") or 0
        if bp > 0 and ep > 0:
            sl_slippage.append(round((bp - ep) / bp * 100, 1))
    avg_sl_slippage = round(sum(sl_slippage) / len(sl_slippage), 1) if sl_slippage else 0

    # 按资产统计
    asset_stats = {}
    for m in settled:
        a = m.get("asset", "unknown")
        if a not in asset_stats:
            asset_stats[a] = {"cnt": 0, "wins": 0, "pnl": 0, "sl": 0}
        asset_stats[a]["cnt"] += 1
        if m["pnl"] > 0:
            asset_stats[a]["wins"] += 1
        asset_stats[a]["pnl"] += m["pnl"]
        if m.get("exit_type") == "stop_loss":
            asset_stats[a]["sl"] += 1

    _tbl_map = {'seller': 'seller_trades', 'smart_seller': 'smart_seller_trades'}
    _tbl = _tbl_map.get(strategy)
    if _tbl:
        _cnt_rows = query_db(
            f"SELECT COUNT(DISTINCT slug) AS total,"
            f" COUNT(DISTINCT CASE WHEN status IN ('entered','settled') THEN slug END) AS entered_cnt"
            f" FROM {_tbl} WHERE 1=1{sid_filter}",
            sid_params,
        )
        _cnt = _cnt_rows[0] if _cnt_rows else {}
        real_total = int(_cnt.get("total") or 0)
        real_entered = int(_cnt.get("entered_cnt") or 0)
    else:
        real_total = len(all_markets)
        real_entered = len(entered)

    stats = {
        "total_markets": real_total,
        "entered": real_entered,
        "settled": len(settled),
        "wins": len(wins),
        "losses": len(losses),
        "win_rate": round(win_rate, 1),
        "total_pnl": round(total_pnl, 4),
        "total_cost": round(total_cost, 4),
        "roi": round(total_pnl / total_cost * 100, 2) if total_cost > 0 else 0,
        # 止损详情
        "sl_count": len(sl_trades),
        "sl_pnl": round(sl_pnl, 4),
        "settle_count": len(settle_trades),
        "settle_pnl": round(settle_pnl, 4),
        "sl_normal": len(sl_normal),
        "sl_fast": len(sl_fast),
        "avg_sl_slippage": avg_sl_slippage,
        "skipped_reentry": len(skipped_reentry),
        "skipped_cooldown": len(skipped_cooldown),
        "asset_stats": {k: {**v, "pnl": round(v["pnl"], 4)} for k, v in asset_stats.items()},
    }

    return {
        "stats": stats,
        "sessions": all_sessions,
        "markets": all_markets,
        "actions": all_actions,
        "strategies": get_available_strategies(),
        "current_strategy": strategy,
        "current_session": str(session_id) if session_id else "all",
    }


def _paginate(items, page, per_page):
    """通用分页，返回 (分页后列表, 分页信息 dict)"""
    total = len(items)
    total_pages = max(1, (total + per_page - 1) // per_page)
    page = max(1, min(page, total_pages))
    start = (page - 1) * per_page
    return items[start:start + per_page], {
        "page": page, "per_page": per_page,
        "total": total, "total_pages": total_pages,
    }


def paginate_data(data, page=1, per_page=30, log_page=1, log_per_page=30):
    """对 markets 和 actions 分别做分页"""
    # --- markets ---
    all_markets = data.get("markets", [])

    # 累计盈亏曲线数据（基于全部已结算市场）
    settled_pnls = [m["pnl"] for m in all_markets if m.get("status") == "settled" and m.get("pnl") is not None]
    data["pnl_series"] = settled_pnls

    # 全局最大绝对盈亏（用于 pnl bar 宽度计算）
    all_pnls = [m["pnl"] for m in all_markets if m.get("pnl") is not None]
    data["max_abs_pnl"] = max((abs(p) for p in all_pnls), default=1)

    data["markets"], data["pagination"] = _paginate(list(reversed(all_markets)), page, per_page)

    # --- actions ---
    data["actions"], data["log_pagination"] = _paginate(list(reversed(data.get("actions", []))), log_page, log_per_page)

    return data


# ============================================================
# HTML
# ============================================================

HTML_PAGE = r"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Poly Trader Dashboard</title>
<style>
:root {
  --bg: #0d1117; --surface: #161b22; --border: #30363d;
  --text: #e6edf3; --text2: #8b949e; --green: #3fb950;
  --red: #f85149; --blue: #58a6ff; --yellow: #d29922;
  --orange: #db6d28;
}
* { margin:0; padding:0; box-sizing:border-box; }
body { background:var(--bg); color:var(--text); font-family:-apple-system,'SF Pro','Segoe UI',sans-serif; font-size:14px; }
a { color:var(--blue); text-decoration:none; }

.container { max-width:1400px; margin:0 auto; padding:16px; }

/* Header */
.header { display:flex; justify-content:space-between; align-items:center; padding:12px 0; border-bottom:1px solid var(--border); margin-bottom:20px; flex-wrap:wrap; gap:8px; }
.header h1 { font-size:20px; font-weight:600; }
.header h1 span { color:var(--blue); }
.header-right { display:flex; gap:12px; align-items:center; }
.header-right select { background:var(--surface); color:var(--text); border:1px solid var(--border); border-radius:6px; padding:5px 10px; font-size:13px; cursor:pointer; }
.badge { font-size:11px; padding:2px 8px; border-radius:12px; font-weight:500; }
.badge-live { background:rgba(63,185,80,0.15); color:var(--green); }
.badge-dry { background:rgba(210,153,34,0.15); color:var(--yellow); }
.refresh-info { color:var(--text2); font-size:12px; }

/* Stats */
.stats-grid { display:grid; grid-template-columns:repeat(auto-fit,minmax(155px,1fr)); gap:12px; margin-bottom:24px; }
.stat-card { background:var(--surface); border:1px solid var(--border); border-radius:8px; padding:16px; }
.stat-card .label { color:var(--text2); font-size:11px; margin-bottom:4px; text-transform:uppercase; letter-spacing:.5px; }
.stat-card .value { font-size:26px; font-weight:700; line-height:1.2; }
.stat-card .sub { color:var(--text2); font-size:12px; margin-top:4px; }
.positive { color:var(--green); }
.negative { color:var(--red); }
.neutral  { color:var(--text2); }

/* Tables */
.section { margin-bottom:24px; }
.section-header { display:flex; justify-content:space-between; align-items:center; margin-bottom:8px; }
.section-title { font-size:16px; font-weight:600; }
.section-count { color:var(--text2); font-size:13px; }
.table-wrap { overflow-x:auto; border-radius:8px; border:1px solid var(--border); }
table { width:100%; border-collapse:collapse; background:var(--surface); }
th { background:rgba(48,54,61,0.5); color:var(--text2); font-weight:500; font-size:11px; text-transform:uppercase; letter-spacing:.5px; text-align:left; padding:10px 12px; position:sticky; top:0; }
td { padding:8px 12px; border-top:1px solid var(--border); font-size:13px; }
tr:hover td { background:rgba(48,54,61,0.3); }
.mono { font-family:'SF Mono','Cascadia Code','Fira Code',monospace; font-size:12px; }

/* Action badges */
.act { display:inline-block; padding:1px 7px; border-radius:4px; font-size:11px; font-weight:500; }
.act-discover       { background:rgba(88,166,255,0.15); color:var(--blue); }
.act-entry_signal   { background:rgba(210,153,34,0.15); color:var(--yellow); }
.act-place_order    { background:rgba(219,109,40,0.15); color:var(--orange); }
.act-order_result   { background:rgba(63,185,80,0.15); color:var(--green); }
.act-settle         { background:rgba(63,185,80,0.2); color:var(--green); }
.act-skip           { background:rgba(139,148,158,0.15); color:var(--text2); }
.act-error          { background:rgba(248,81,73,0.15); color:var(--red); }
.act-session_start  { background:rgba(88,166,255,0.1); color:var(--blue); }
.act-session_end    { background:rgba(139,148,158,0.1); color:var(--text2); }
.act-stop_loss      { background:rgba(248,81,73,0.2); color:var(--red); }
.act-sl_fast_track  { background:rgba(219,109,40,0.2); color:var(--orange); }
.act-sl_warning     { background:rgba(210,153,34,0.15); color:var(--yellow); }
.act-cooldown_start { background:rgba(88,166,255,0.15); color:var(--blue); }
.act-no_trigger     { background:rgba(139,148,158,0.1); color:var(--text2); }

.st-entered    { color:var(--yellow); }
.st-settled    { color:var(--green); }
.st-skipped    { color:var(--text2); }
.st-error      { color:var(--red); }
.st-discovered { color:var(--blue); }

/* Detail */
.detail-cell { max-width:420px; cursor:pointer; vertical-align:top; }
.detail-cell .dt-sum { overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }
.detail-cell:hover .dt-sum { white-space:normal; word-break:break-all; }
.detail-cell .dt-full { display:none; }
.detail-cell.expanded .dt-sum { display:none; }
.detail-cell.expanded .dt-full { display:block; white-space:pre-wrap; word-break:break-all; font-family:'SF Mono','Cascadia Code','Fira Code',monospace; font-size:11px; }

/* PnL bar */
.pnl-wrap { display:flex; align-items:center; gap:6px; }
.pnl-bar { height:12px; border-radius:2px; min-width:2px; }
.pnl-bar.positive { background:var(--green); }
.pnl-bar.negative { background:var(--red); }

/* Cumulative PnL chart */
.chart-container { background:var(--surface); border:1px solid var(--border); border-radius:8px; padding:16px; margin-bottom:24px; }
.chart-container h3 { font-size:14px; color:var(--text2); margin-bottom:12px; }
.chart-svg { width:100%; height:120px; }

/* Tabs */
.tabs { display:flex; gap:4px; margin-bottom:12px; }
.tab { padding:6px 14px; border-radius:6px; font-size:13px; cursor:pointer; border:1px solid var(--border); background:transparent; color:var(--text2); }
.tab.active { background:var(--surface); color:var(--text); border-color:var(--blue); }

/* ---- Mobile / Responsive ---- */
@media (max-width:768px) {
  .container { padding:10px 8px; }
  .stats-grid { grid-template-columns:repeat(2,1fr); gap:8px; }
  .stat-card { padding:10px 12px; }
  .stat-card .value { font-size:20px; }
  .stat-card .label { font-size:10px; }
  .stat-card .sub { font-size:11px; }
  .header { flex-direction:column; align-items:flex-start; gap:10px; padding:8px 0; }
  .header h1 { font-size:17px; }
  .header-right { width:100%; flex-wrap:wrap; gap:8px; }
  .strat-tabs { width:100%; justify-content:stretch; }
  .strat-tab { flex:1; text-align:center; padding:8px 6px; font-size:12px; min-height:36px; }
  #sessionSelect { flex:1; min-width:0; font-size:12px; padding:8px 6px; min-height:36px; margin-left:0; }
  .section-title { font-size:14px; }
  .section-count { font-size:11px; }
  td,th { padding:6px 6px; font-size:11px; white-space:nowrap; }
  .detail-cell { max-width:200px; font-size:11px; }
  .mono { font-size:11px; }
  .pnl-wrap { gap:3px; }
  .chart-container { padding:10px; }
  .chart-container h3 { font-size:12px; }
  /* Hide less important columns on small screens */
  .hide-mobile { display:none !important; }
  /* Pagination touch-friendly */
  .strat-tab { min-width:36px; min-height:36px; display:inline-flex; align-items:center; justify-content:center; }
  .table-wrap { margin:0 -8px; border-radius:0; border-left:none; border-right:none; }
  /* Session info bar */
  .session-info-bar { font-size:11px !important; padding:6px 10px !important; flex-direction:column; gap:4px !important; }
}
@media (max-width:480px) {
  .stats-grid { grid-template-columns:repeat(2,1fr); gap:6px; }
  .stat-card .value { font-size:17px; }
  td,th { padding:5px 4px; font-size:10px; }
  .mono { font-size:10px; }
}
.strat-tabs { display:flex; gap:6px; }
.strat-tab { padding:5px 14px; border-radius:6px; font-size:13px; cursor:pointer; border:1px solid var(--border); background:transparent; color:var(--text2); transition:all .15s; }
.strat-tab:hover { border-color:var(--text2); }
.strat-tab.active { background:var(--blue); color:#fff; border-color:var(--blue); }
#sessionSelect { background:var(--surface); color:var(--text); border:1px solid var(--border); border-radius:6px; padding:5px 10px; font-size:13px; margin-left:8px; }
</style>
</head>
<body>
<div class="container">
  <div class="header">
    <h1>&#x1F4CA; <span>Poly Trader</span> Dashboard</h1>
    <div class="header-right">
      <span id="balanceInfo" style="font-size:13px;color:var(--text2)"></span>
      <div class="strat-tabs" id="stratTabs"></div>
      <select id="sessionSelect" onchange="switchSession()"></select>
      <span class="refresh-info" id="refreshInfo">每15秒刷新</span>
    </div>
  </div>
  <div id="content"><p style="color:var(--text2);padding:40px;text-align:center">加载中...</p></div>
</div>

<script>
const RI = 15000;
let timer = null;
let curStrategy = localStorage.getItem('dash_strategy') || 'seller';
let curSession = localStorage.getItem('dash_session') || 'all';
let curPage = 1;
let curPerPage = 30;
let logPage = 1;
let logPerPage = 30;
let _prevSessionIds = '';  // track session list changes

function _saveState() {
  localStorage.setItem('dash_strategy', curStrategy);
  localStorage.setItem('dash_session', curSession);
}

function switchStrategy(s) {
  curStrategy = s;
  curSession = 'all';
  curPage = 1;
  logPage = 1;
  _saveState();
  fetchData();
}
function switchSession() {
  curSession = document.getElementById('sessionSelect').value;
  curPage = 1;
  logPage = 1;
  _saveState();
  fetchData();
}
function goPage(p) {
  curPage = p;
  fetchData();
}
function goLogPage(p) {
  logPage = p;
  fetchData();
}

function fetchData() {
  let url = '/api/data?strategy=' + encodeURIComponent(curStrategy);
  if (curSession && curSession !== 'all') url += '&session=' + encodeURIComponent(curSession);
  url += '&page=' + curPage + '&per_page=' + curPerPage;
  url += '&log_page=' + logPage + '&log_per_page=' + logPerPage;
  fetch(url).then(r=>r.json()).then(render).catch(e=>{
    document.getElementById('content').innerHTML = '<p style="color:var(--red);padding:40px">加载失败: '+e+'</p>';
  });
}

const pc = v => v>0?'positive':v<0?'negative':'neutral';
const fp = v => v==null?'-':(v>0?'+':'')+v.toFixed(4);
const fpr = v => v==null?'-':v.toFixed(3);
const fpct = v => v==null?'-':v.toFixed(1)+'%';
const ft = t => t?t.replace('T',' ').substring(0,19):'-';
function ss(s) { return s?s.replace(/btc-updown-15m-/,'btc-').replace(/eth-updown-15m-/,'eth-').replace(/sol-updown-15m-/,'sol-').replace(/xrp-updown-15m-/,'xrp-'):'-'; }
function ab(a) { return '<span class="act act-'+(a||'').replace(/ /g,'_')+'">'+(a||'-')+'</span>'; }
function sb(s) { return '<span class="st-'+(s||'')+'">'+(s||'-')+'</span>'; }
function esc(s) { const d=document.createElement('div'); d.textContent=s; return d.innerHTML; }

function pnlBar(v, mx) {
  if(v==null||!mx) return '-';
  const w=Math.max(2,Math.abs(v)/mx*80);
  const c=v>=0?'positive':'negative';
  return '<div class="pnl-wrap"><div class="pnl-bar '+c+'" style="width:'+w+'px"></div><span class="'+c+' mono">'+fp(v)+'</span></div>';
}

function detailHtml(d) {
  if(!d) return { summary: '<span class="neutral">-</span>', full: '-' };
  let summary = '';
  let fullStr = typeof d === 'string' ? d : JSON.stringify(d);
  let full = esc(fullStr);
  try {
    const o=JSON.parse(fullStr);
    full = esc(JSON.stringify(o, null, 2));
    if(o.error) summary = '<span class="negative">'+esc(String(o.error).substring(0,150))+'</span>';
    else if(o.reason) summary = esc(o.reason);
    else if(o.side && (o.price||o.entry_price)) summary = o.side+' @'+(o.price||o.entry_price);
    else if(o.order_id) summary = '<span class="positive">'+o.order_id.substring(0,18)+'...</span>';
    else if(o.outcome) summary = o.outcome+' pnl='+fp(o.pnl);
    else if(o.mode) summary = o.mode + (o.asset?' '+o.asset:'');
    else {
      const k=Object.keys(o);
      summary = esc(k.slice(0,4).map(x=>x+'='+JSON.stringify(o[x]).substring(0,20)).join(', '));
    }
  } catch(e) { summary = esc(String(fullStr).substring(0,120)); }
  return { summary, full };
}

function cumPnlChart(pnlSeries) {
  if(!pnlSeries||pnlSeries.length<2) return '';
  let cum=0, pts=[];
  pnlSeries.forEach((p,i)=>{ cum+=p; pts.push({x:i,y:cum}); });
  const minY=Math.min(0,...pts.map(p=>p.y)), maxY=Math.max(0,...pts.map(p=>p.y));
  const rangeY=maxY-minY||1;
  const W=800, H=100, padL=40, padR=10, padT=10, padB=20;
  const iw=(W-padL-padR)/(pts.length-1||1);
  function sx(i){return padL+i*iw}
  function sy(v){return padT+(1-(v-minY)/rangeY)*(H-padT-padB)}
  let path='M';
  pts.forEach((p,i)=>{path+=(i?'L':'')+sx(p.x).toFixed(1)+','+sy(p.y).toFixed(1);});
  // area
  let area=path+'L'+sx(pts.length-1).toFixed(1)+','+sy(0).toFixed(1)+'L'+sx(0).toFixed(1)+','+sy(0).toFixed(1)+'Z';
  const lineColor=cum>=0?'var(--green)':'var(--red)';
  const areaColor=cum>=0?'rgba(63,185,80,0.1)':'rgba(248,81,73,0.1)';
  // zero line
  const zy=sy(0);

  let svg='<svg class="chart-svg" viewBox="0 0 '+W+' '+H+'" preserveAspectRatio="none">';
  // zero line
  svg+='<line x1="'+padL+'" y1="'+zy.toFixed(1)+'" x2="'+(W-padR)+'" y2="'+zy.toFixed(1)+'" stroke="var(--border)" stroke-dasharray="4"/>';
  // area
  svg+='<path d="'+area+'" fill="'+areaColor+'"/>';
  // line
  svg+='<path d="'+path+'" fill="none" stroke="'+lineColor+'" stroke-width="2"/>';
  // dots at start & end
  const last=pts[pts.length-1];
  svg+='<circle cx="'+sx(0).toFixed(1)+'" cy="'+sy(pts[0].y).toFixed(1)+'" r="3" fill="'+lineColor+'"/>';
  svg+='<circle cx="'+sx(last.x).toFixed(1)+'" cy="'+sy(last.y).toFixed(1)+'" r="3" fill="'+lineColor+'"/>';
  // labels
  svg+='<text x="'+(padL-4)+'" y="'+(padT+4)+'" fill="var(--text2)" font-size="10" text-anchor="end">'+maxY.toFixed(2)+'</text>';
  svg+='<text x="'+(padL-4)+'" y="'+(H-padB+4)+'" fill="var(--text2)" font-size="10" text-anchor="end">'+minY.toFixed(2)+'</text>';
  svg+='<text x="'+sx(last.x).toFixed(1)+'" y="'+(sy(last.y)-8).toFixed(1)+'" fill="'+lineColor+'" font-size="11" text-anchor="middle" font-weight="600">'+fp(cum)+'</text>';
  svg+='</svg>';

  return '<div class="chart-container"><h3>累计盈亏曲线 ('+pnlSeries.length+' 笔已结算)</h3>'+svg+'</div>';
}

function statCard(label,value,sub,cls) {
  return '<div class="stat-card"><div class="label">'+label+'</div><div class="value '+(cls||'')+'">'+value+'</div>'+(sub?'<div class="sub">'+sub+'</div>':'')+'</div>';
}

function render(data) {
  if(data.error){
    document.getElementById('content').innerHTML='<p style="color:var(--text2);padding:40px;text-align:center">'+data.error+'</p>';
    // still update strategy tabs even on error
    if(data.strategies) updateStratTabs(data.strategies, data.current_strategy||curStrategy);
    return;
  }
  const s=data.stats;

  // Strategy tabs
  updateStratTabs(data.strategies||[], data.current_strategy||curStrategy);
  curStrategy = data.current_strategy||curStrategy;

  // Session selector — only rebuild when session list actually changed
  const sel=document.getElementById('sessionSelect');
  const newSids=(data.sessions||[]).map(s=>s.id+':'+(s.total_pnl||'')).join(',');
  if(newSids !== _prevSessionIds) {
    _prevSessionIds = newSids;
    sel.innerHTML='';
    const oAll=document.createElement('option');
    oAll.value='all'; oAll.textContent='\uD83D\uDCDA 全部轮次'; sel.add(oAll);
    (data.sessions||[]).slice().reverse().forEach(sess=>{
      const o=document.createElement('option');
      o.value=String(sess.id);
      const t=sess.started_at?sess.started_at.replace('T',' ').substring(5,16):'';
      const m=sess.mode==='live'?'[LIVE]':'[DRY]';
      const p=sess.total_pnl!=null?(sess.total_pnl>=0?'+':'')+Number(sess.total_pnl).toFixed(2):'';
      o.textContent='#'+sess.id+' '+m+' '+t+(p?' ('+p+')':'');
      sel.add(o);
    });
  }
  // restore / keep selection
  const targetSid = curSession;
  let found = false;
  for(let i=0;i<sel.options.length;i++){if(sel.options[i].value===targetSid){sel.selectedIndex=i;found=true;break;}}
  if(!found){ sel.selectedIndex=0; curSession=sel.value; _saveState(); }
  else { curSession=targetSid; }

  const isLive=data.sessions.some(x=>x.mode==='live');
  const modeBadge=isLive?'<span class="badge badge-live">LIVE</span>':'<span class="badge badge-dry">DRY-RUN</span>';
  const maxAbs=data.max_abs_pnl||1;

  let h='';

  // Session info bar
  if(data.sessions.length) {
    const sids=curSession==='all'?data.sessions:[data.sessions.find(x=>String(x.id)===curSession)].filter(Boolean);
    if(sids.length===1){
      const ss=sids[0];
      let pm='-';
      const paramHelp={
        threshold:'低价方入场阈值',min_threshold:'最低阈值下限',bet_amount:'每笔金额($)',
        max_trades:'最大交易数(0=不限)',max_loss:'最大亏损(0=不限)',limit_offset:'限价偏移',
        reprice_gap:'重挂单价差',assets:'监控资产',phase_filter:'阶段过滤',min_elapsed:'最少等待(秒)',
        sl_trigger:'止损触发价',sl_confirm:'止损确认次数',sl_fast_track:'极端止损价',
        cooldown_losses:'连亏暂停阈值',max_sl_loss:'单笔最大止损比',dry_run:'模拟模式',
        a_trigger:'策略A入场阈值',a_hedge_ceiling:'对冲成本上限',a_time_stop:'持仓超时(秒)',
        b_entry_low:'策略B入场下限',b_entry_high:'策略B入场上限',b_tp:'策略B止盈',b_sl:'策略B止损',
        asset:'监控资产'
      };
      try{
        const p=JSON.parse(ss.params||'{}');const ks=Object.keys(p);
        if(ks.length) pm=ks.map(k=>{
          const v=JSON.stringify(p[k]);
          const tip=paramHelp[k]||'';
          return tip?'<span title="'+tip+'" style="cursor:help;border-bottom:1px dotted var(--text2)">'+k+'</span>='+v:''+k+'='+v;
        }).join(', ');
      }catch(e){}
      h+='<div style="display:flex;align-items:center;gap:16px;padding:8px 14px;background:var(--surface);border:1px solid var(--border);border-radius:8px;margin-bottom:12px;font-size:13px;flex-wrap:wrap">';
      h+='<span>'+modeBadge+'</span>';
      h+='<span style="color:var(--text2)">轮次 #'+ss.id+'</span>';
      h+='<span class="mono">'+ft(ss.started_at)+' → '+(ss.ended_at?ft(ss.ended_at):'<span style="color:var(--green)">运行中</span>')+'</span>';
      h+='<span style="color:var(--text2);font-size:12px">'+pm+'</span>';
      h+='</div>';
    } else if(sids.length>1){
      const first=sids[0], last=sids[sids.length-1];
      h+='<div style="display:flex;align-items:center;gap:16px;padding:8px 14px;background:var(--surface);border:1px solid var(--border);border-radius:8px;margin-bottom:12px;font-size:13px">';
      h+='<span>'+modeBadge+'</span>';
      h+='<span style="color:var(--text2)">全部 '+sids.length+' 轮</span>';
      h+='<span class="mono">'+ft(first.started_at)+' → '+(last.ended_at?ft(last.ended_at):'<span style="color:var(--green)">运行中</span>')+'</span>';
      h+='</div>';
    }
  }

  // Stats row
  h+='<div class="stats-grid">';
  h+=statCard('总市场',s.total_markets,s.entered+' 入场');
  h+=statCard('已结算',s.settled,s.wins+' 胜 / '+s.losses+' 负');
  h+=statCard('胜率',fpct(s.win_rate),'',s.win_rate>=50?'positive':s.win_rate>0?'negative':'neutral');
  h+=statCard('总盈亏',fp(s.total_pnl),'USDC',pc(s.total_pnl));
  h+=statCard('总投入','$'+s.total_cost.toFixed(2),'');
  h+=statCard('ROI',fpct(s.roi),'',pc(s.roi));
  h+='</div>';

  // Stop-loss analysis panel (seller strategy)
  if(s.sl_count > 0 || s.settle_count > 0) {
    h+='<div class="section"><div class="section-header"><span class="section-title">&#x1F6E1;&#xFE0F; 止损 &amp; 保护分析</span></div>';
    h+='<div class="stats-grid" style="grid-template-columns:repeat(auto-fit,minmax(140px,1fr))">';
    h+=statCard('结算盈利',fp(s.settle_pnl),s.settle_count+'笔 (全胜)',s.settle_pnl>=0?'positive':'negative');
    h+=statCard('止损亏损',fp(s.sl_pnl),s.sl_count+'笔','negative');
    h+=statCard('平均滑点',s.avg_sl_slippage+'%','止损买卖价差',s.avg_sl_slippage>20?'negative':'neutral');
    h+=statCard('正常止损',s.sl_normal+'笔','确认后触发','neutral');
    h+=statCard('快速止损',s.sl_fast+'笔','极端行情直接触发',s.sl_fast>0?'negative':'neutral');
    h+=statCard('重入阻止',s.skipped_reentry+'笔','止损后禁入',s.skipped_reentry>0?'positive':'neutral');
    h+=statCard('冷却跳过',s.skipped_cooldown+'笔','连亏暂停',s.skipped_cooldown>0?'positive':'neutral');
    h+='</div>';

    // Asset breakdown
    if(s.asset_stats && Object.keys(s.asset_stats).length>0) {
      h+='<div class="table-wrap" style="margin-top:12px"><table><tr><th>资产</th><th>交易数</th><th>胜率</th><th>PnL</th><th>止损数</th><th>止损率</th></tr>';
      Object.entries(s.asset_stats).sort((a,b)=>b[1].pnl-a[1].pnl).forEach(([k,v])=>{
        const wr=v.cnt?((v.wins/v.cnt*100).toFixed(1)+'%'):'-';
        const slr=v.cnt?((v.sl/v.cnt*100).toFixed(0)+'%'):'-';
        h+='<tr><td class="mono" style="font-weight:600">'+k.toUpperCase()+'</td><td>'+v.cnt+'</td><td class="'+pc(v.wins/v.cnt-0.5)+'">'+wr+'</td><td class="'+pc(v.pnl)+' mono">'+fp(v.pnl)+'</td><td>'+v.sl+'</td><td class="'+(v.sl/v.cnt>0.3?'negative':'neutral')+'">'+slr+'</td></tr>';
      });
      h+='</table></div>';
    }
    h+='</div>';
  }

  // Chart
  h+=cumPnlChart(data.pnl_series||[]);

  // Markets (paginated)
  const pg=data.pagination||{page:1,per_page:30,total:data.markets.length,total_pages:1};
  h+='<div class="section"><div class="section-header"><span class="section-title">市场记录</span><span class="section-count">共 '+pg.total+' 条 · 第 '+pg.page+'/'+pg.total_pages+' 页</span></div>';
  if(data.markets.length){
    h+='<div class="table-wrap"><table><tr><th>市场</th><th class="hide-mobile">策略</th><th>状态</th><th>方向</th><th>买入价</th><th class="hide-mobile">金额</th><th class="hide-mobile">触发/信心</th><th>结果</th><th class="hide-mobile">退出</th><th>盈亏</th></tr>';
    data.markets.forEach(m=>{
      const side=m.entry_side?'<span class="'+(m.entry_side==='YES'?'positive':'negative')+'">'+m.entry_side+'</span>':'-';
      let stName = m.strategy_type==='seller'?'卖方':m.strategy_type==='smart_seller'?'智能卖方':'趋势';
      let stCls = m.strategy_type==='seller'?'act-settle':m.strategy_type==='smart_seller'?'act-place_order':'act-discover';
      const st='<span class="act '+stCls+'" style="font-size:10px">'+stName+'</span>';
      const conf=(m.strategy_type==='seller'||m.strategy_type==='smart_seller')?(m.trigger_side?'卖'+m.trigger_side+'@'+fpr(m.trigger_price):'-'):(m.entry_confidence!=null?m.entry_confidence.toFixed(3):'-');
      // Exit info
      let exitInfo='-';
      if(m.exit_type==='stop_loss'){
        const reason=m.sl_reason==='fast_track'?'<span class="act act-sl_fast_track">极端</span>':'<span class="act act-stop_loss">止损</span>';
        exitInfo=reason+(m.exit_price?'@'+fpr(m.exit_price):'');
      } else if(m.exit_type==='settlement'){
        exitInfo='<span class="act act-settle">结算</span>';
      } else if(m.skip_reason==='reentry_block'){
        exitInfo='<span class="act act-no_trigger">禁入</span>';
      } else if(m.skip_reason==='cooldown'){
        exitInfo='<span class="act act-cooldown_start">冷却</span>';
      }
      h+='<tr><td class="mono">'+ss(m.slug)+'</td><td class="hide-mobile">'+st+'</td><td>'+sb(m.status)+'</td><td>'+side+'</td><td class="mono">'+fpr(m.entry_price)+'</td><td class="hide-mobile mono">'+(m.entry_amount!=null?'$'+m.entry_amount.toFixed(2):'-')+'</td><td class="hide-mobile mono">'+conf+'</td><td>'+(m.outcome||'-')+'</td><td class="hide-mobile">'+exitInfo+'</td><td>'+pnlBar(m.pnl,maxAbs)+'</td></tr>';
    });
    h+='</table></div>';
    // Pagination controls
    if(pg.total_pages>1){
      h+='<div style="display:flex;justify-content:center;align-items:center;gap:8px;margin-top:12px;flex-wrap:wrap">';
      h+='<button class="strat-tab'+(pg.page<=1?' disabled':'')+'" onclick="goPage(1)" '+(pg.page<=1?'disabled':'')+' style="'+(pg.page<=1?'opacity:.4;cursor:default':'')+'">&laquo;</button>';
      h+='<button class="strat-tab'+(pg.page<=1?' disabled':'')+'" onclick="goPage('+(pg.page-1)+')" '+(pg.page<=1?'disabled':'')+' style="'+(pg.page<=1?'opacity:.4;cursor:default':'')+'">&lsaquo; 上一页</button>';
      // page numbers
      let ps=Math.max(1,pg.page-3),pe=Math.min(pg.total_pages,pg.page+3);
      if(ps>1) h+='<span style="color:var(--text2)">...</span>';
      for(let i=ps;i<=pe;i++){
        h+='<button class="strat-tab'+(i===pg.page?' active':'')+'" onclick="goPage('+i+')">'+i+'</button>';
      }
      if(pe<pg.total_pages) h+='<span style="color:var(--text2)">...</span>';
      h+='<button class="strat-tab'+(pg.page>=pg.total_pages?' disabled':'')+'" onclick="goPage('+(pg.page+1)+')" '+(pg.page>=pg.total_pages?'disabled':'')+' style="'+(pg.page>=pg.total_pages?'opacity:.4;cursor:default':'')+'">&rsaquo; 下一页</button>';
      h+='<button class="strat-tab'+(pg.page>=pg.total_pages?' disabled':'')+'" onclick="goPage('+pg.total_pages+')" '+(pg.page>=pg.total_pages?'disabled':'')+' style="'+(pg.page>=pg.total_pages?'opacity:.4;cursor:default':'')+'">&raquo;</button>';
      h+='<span style="color:var(--text2);font-size:12px;margin-left:8px">每页 <select onchange="curPerPage=+this.value;curPage=1;fetchData()" style="background:var(--surface);color:var(--text);border:1px solid var(--border);border-radius:4px;padding:2px 4px;font-size:12px">';
      [15,30,50,100].forEach(n=>{ h+='<option value="'+n+'"'+(n===pg.per_page?' selected':'')+'>'+n+'</option>'; });
      h+='</select> 条</span>';
      h+='</div>';
    }
  } else { h+='<p style="color:var(--text2);padding:20px">暂无市场记录</p>'; }
  h+='</div>';

  // Action Log (paginated)
  const lp=data.log_pagination||{page:1,per_page:30,total:data.actions.length,total_pages:1};
  h+='<div class="section"><div class="section-header"><span class="section-title">操作日志</span><span class="section-count">共 '+lp.total+' 条 · 第 '+lp.page+'/'+lp.total_pages+' 页</span></div>';
  if(data.actions.length){
    h+='<div class="table-wrap"><table><tr><th>时间</th><th>市场</th><th>操作</th><th>详情</th></tr>';
    data.actions.forEach(a=>{
      const ls=a.level==='error'?'style="background:rgba(248,81,73,0.05)"':'';
      const parsed = detailHtml(a.detail);
      h+='<tr '+ls+'><td class="mono" style="white-space:nowrap">'+ft(a.ts)+'</td><td class="mono">'+ss(a.market_slug)+'</td><td>'+ab(a.action)+'</td><td class="detail-cell" onclick="this.classList.toggle(\'expanded\')"><div class="dt-sum">'+parsed.summary+'</div><div class="dt-full">'+parsed.full+'</div></td></tr>';
    });
    h+='</table></div>';
    // Log pagination controls
    if(lp.total_pages>1){
      h+='<div style="display:flex;justify-content:center;align-items:center;gap:8px;margin-top:12px;flex-wrap:wrap">';
      h+='<button class="strat-tab'+(lp.page<=1?' disabled':'')+'" onclick="goLogPage(1)" '+(lp.page<=1?'disabled':'')+' style="'+(lp.page<=1?'opacity:.4;cursor:default':'')+'">&laquo;</button>';
      h+='<button class="strat-tab'+(lp.page<=1?' disabled':'')+'" onclick="goLogPage('+(lp.page-1)+')" '+(lp.page<=1?'disabled':'')+' style="'+(lp.page<=1?'opacity:.4;cursor:default':'')+'">&lsaquo; 上一页</button>';
      let lps=Math.max(1,lp.page-3),lpe=Math.min(lp.total_pages,lp.page+3);
      if(lps>1) h+='<span style="color:var(--text2)">...</span>';
      for(let i=lps;i<=lpe;i++){
        h+='<button class="strat-tab'+(i===lp.page?' active':'')+'" onclick="goLogPage('+i+')">'+i+'</button>';
      }
      if(lpe<lp.total_pages) h+='<span style="color:var(--text2)">...</span>';
      h+='<button class="strat-tab'+(lp.page>=lp.total_pages?' disabled':'')+'" onclick="goLogPage('+(lp.page+1)+')" '+(lp.page>=lp.total_pages?'disabled':'')+' style="'+(lp.page>=lp.total_pages?'opacity:.4;cursor:default':'')+'">&rsaquo; 下一页</button>';
      h+='<button class="strat-tab'+(lp.page>=lp.total_pages?' disabled':'')+'" onclick="goLogPage('+lp.total_pages+')" '+(lp.page>=lp.total_pages?'disabled':'')+' style="'+(lp.page>=lp.total_pages?'opacity:.4;cursor:default':'')+'">&raquo;</button>';
      h+='<span style="color:var(--text2);font-size:12px;margin-left:8px">每页 <select onchange="logPerPage=+this.value;logPage=1;fetchData()" style="background:var(--surface);color:var(--text);border:1px solid var(--border);border-radius:4px;padding:2px 4px;font-size:12px">';
      [30,50,100,200].forEach(n=>{ h+='<option value="'+n+'"'+(n===lp.per_page?' selected':'')+'>'+n+'</option>'; });
      h+='</select> 条</span>';
      h+='</div>';
    }
  } else { h+='<p style="color:var(--text2);padding:20px">暂无日志</p>'; }
  h+='</div>';

  document.getElementById('content').innerHTML=h;
  document.getElementById('refreshInfo').textContent='更新于 '+new Date().toLocaleTimeString()+' · 每15秒刷新';
}

function updateStratTabs(strategies, active) {
  const c=document.getElementById('stratTabs');
  const names={seller:'📉 卖方策略',trend:'📈 趋势策略',smart_seller:'🧠 智能卖方'};
  c.innerHTML=strategies.map(s=>
    '<button class="strat-tab'+(s===active?' active':'')+'" onclick="switchStrategy(\''+s+'\')">'+(names[s]||s)+'</button>'
  ).join('');
}

function fetchBalance(){
  fetch('/api/balance').then(r=>r.json()).then(d=>{
    const el=document.getElementById('balanceInfo');
    if(!el) return;
    let parts=[];
    if(d.proxy_usdc!=null) parts.push('Poly: <span style="color:var(--green);font-weight:600">$'+d.proxy_usdc.toFixed(2)+'</span>');
    if(d.wallet_usdc!=null) parts.push('钱包: $'+d.wallet_usdc.toFixed(2));
    if(parts.length) {
      const addr=d.proxy||d.wallet||'';
      const short=addr?addr.substring(0,6)+'...'+addr.substring(addr.length-4):'';
      el.innerHTML='💰 '+parts.join(' · ')+(short?' <span style="color:var(--text2);font-size:11px">('+short+')</span>':'');
    } else {
      el.innerHTML=d.error||'';
    }
  }).catch(()=>{});
}
fetchData();
fetchBalance();
timer=setInterval(()=>fetchData(),RI);
setInterval(()=>fetchBalance(),60000);  // 余额每60秒刷新
</script>
</body>
</html>"""


# ============================================================
# HTTP Server
# ============================================================

class DashboardHandler(BaseHTTPRequestHandler):

    def log_message(self, fmt, *args):
        ts = datetime.now().strftime("%H:%M:%S")
        method = args[0] if args else ""
        code = args[1] if len(args) > 1 else ""
        if "/api/data" not in str(method):
            print(f"  [{ts}] {method} {code}")

    def handle(self):
        """覆盖 handle 以捕获 ConnectionResetError / BrokenPipeError"""
        try:
            super().handle()
        except (ConnectionResetError, BrokenPipeError, ConnectionAbortedError):
            pass

    def do_GET(self):
        parsed = urlparse(self.path)
        path = parsed.path

        if path == "/" or path == "/index.html":
            self._respond(200, "text/html", HTML_PAGE.encode("utf-8"))

        elif path == "/api/data":
            qs = parse_qs(parsed.query)
            strategy = qs.get("strategy", ["seller"])[0]
            session = qs.get("session", ["all"])[0]
            sid = None if session == "all" else session
            page = int(qs.get("page", ["1"])[0])
            per_page = int(qs.get("per_page", ["30"])[0])
            log_page = int(qs.get("log_page", ["1"])[0])
            log_per_page = int(qs.get("log_per_page", ["30"])[0])
            data = get_dashboard_data(strategy=strategy, session_id=sid)
            data = paginate_data(data, page=page, per_page=per_page,
                                log_page=log_page, log_per_page=log_per_page)
            body = json.dumps(data, ensure_ascii=False, default=str).encode("utf-8")
            self._respond(200, "application/json", body,
                          extra={"Access-Control-Allow-Origin": "*"})

        elif path == "/api/balance":
            data = get_wallet_balance()
            body = json.dumps(data, ensure_ascii=False, default=str).encode("utf-8")
            self._respond(200, "application/json", body,
                          extra={"Access-Control-Allow-Origin": "*"})

        elif path == "/api/health":
            body = json.dumps({"status": "ok", "time": datetime.now().isoformat()}).encode()
            self._respond(200, "application/json", body)

        else:
            self._respond(404, "text/plain", b"Not Found")

    def _respond(self, code, content_type, body, extra=None):
        self.send_response(code)
        self.send_header("Content-Type", content_type + "; charset=utf-8")
        if extra:
            for k, v in extra.items():
                self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)


def main():
    parser = argparse.ArgumentParser(description="交易监控 Dashboard")
    parser.add_argument("--port", type=int, default=8080, help="HTTP 端口 (默认 8080)")
    parser.add_argument("--host", type=str, default="0.0.0.0", help="监听地址 (默认 0.0.0.0)")
    args = parser.parse_args()

    avail = get_available_strategies()
    print("=" * 50)
    print("  Poly Trader Dashboard")
    print("=" * 50)
    print(f"  地址:     http://localhost:{args.port}")
    if args.host == "0.0.0.0":
        print(f"  局域网:   http://<本机IP>:{args.port}")
    print(f"  策略:     {', '.join(avail) if avail else '无可用策略'}")
    print(f"  数据库:   MySQL")
    print(f"  刷新:     每15秒自动刷新")
    print("=" * 50)
    print("  Ctrl+C 退出\n")

    server = HTTPServer((args.host, args.port), DashboardHandler)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n已停止")
        server.server_close()


if __name__ == "__main__":
    main()
