#!/usr/bin/env python3
"""
Polymarket API 连通性诊断
在云端或本地运行，检查 gamma-api / data-api / clob 是否可达。
用法: python3 scripts/check_pm_api.py
"""

import json
import sys
import time
import urllib.parse
import urllib.request

GAMMA = "https://gamma-api.polymarket.com"
EVENTS = f"{GAMMA}/events"
CLOB_MID = "https://clob.polymarket.com/midpoint"
DATA_API = "https://data-api.polymarket.com"

def fetch(url: str, params: dict = None) -> tuple[int, str]:
    full = f"{url}?{urllib.parse.urlencode(params)}" if params else url
    req = urllib.request.Request(
        full,
        headers={"User-Agent": "Mozilla/5.0 (compatible; CheckPM/1.0)"},
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            return r.status, r.read().decode()
    except Exception as e:
        return -1, str(e)

def main():
    now = int(time.time())
    base_5 = (now // 300) * 300
    base_15 = (now // 900) * 900
    slug_5 = f"btc-updown-5m-{base_5}"
    slug_15 = f"btc-updown-15m-{base_15}"

    print("=== Polymarket API 连通性诊断 ===\n")

    # 1. Gamma Events 5m
    print(f"1. Gamma Events (5m): GET {EVENTS}?slug={slug_5}")
    status, body = fetch(EVENTS, {"slug": slug_5})
    if status == 200:
        try:
            data = json.loads(body)
            events = data if isinstance(data, list) else []
            if events and events[0].get("markets"):
                m = events[0]["markets"][0]
                cid = m.get("conditionId", "")[:20]
                tokens = m.get("clobTokenIds", [])
                if isinstance(tokens, str):
                    tokens = json.loads(tokens) if tokens else []
                print(f"   ✅ OK | conditionId={cid}... | tokens={len(tokens)}")
            else:
                print(f"   ⚠️ 200 但无 markets")
        except Exception as e:
            print(f"   ⚠️ 解析失败: {e}")
    else:
        print(f"   ❌ 失败 status={status} body={body[:200]}")

    # 2. Gamma Events 15m
    print(f"\n2. Gamma Events (15m): GET {EVENTS}?slug={slug_15}")
    status, body = fetch(EVENTS, {"slug": slug_15})
    if status == 200:
        try:
            data = json.loads(body)
            events = data if isinstance(data, list) else []
            if events and events[0].get("markets"):
                m = events[0]["markets"][0]
                cid = m.get("conditionId", "")[:20]
                print(f"   ✅ OK | conditionId={cid}...")
            else:
                print(f"   ⚠️ 200 但无 markets")
        except Exception as e:
            print(f"   ⚠️ 解析失败: {e}")
    else:
        print(f"   ❌ 失败 status={status} body={body[:200]}")

    # 3. CLOB midpoint (需要 token_id，从上面拿)
    print("\n3. CLOB midpoint")
    status, body = fetch(EVENTS, {"slug": slug_5})
    if status == 200:
        try:
            data = json.loads(body)
            events = data if isinstance(data, list) else []
            if events and events[0].get("markets"):
                m = events[0]["markets"][0]
                tokens = m.get("clobTokenIds", [])
                if isinstance(tokens, str):
                    tokens = json.loads(tokens) if tokens else []
                if tokens:
                    mid_status, mid_body = fetch(CLOB_MID, {"token_id": tokens[0]})
                    if mid_status == 200:
                        d = json.loads(mid_body)
                        print(f"   ✅ OK | mid={d.get('mid', '?')}")
                    else:
                        print(f"   ❌ 失败 status={mid_status}")
                else:
                    print("   ⚠️ 无 token_id")
            else:
                print("   ⚠️ 先获取 Events 失败")
        except Exception as e:
            print(f"   ⚠️ {e}")
    else:
        print("   ⚠️ 需先有 Events 才能测 midpoint")

    print("\n=== 诊断结束 ===")
    print("若 1/2 失败，请检查：")
    print("  - 云端能否访问 gamma-api.polymarket.com")
    print("  - 防火墙/代理是否拦截")
    print("  - 运行: curl -I https://gamma-api.polymarket.com/events?slug=btc-updown-5m-1730000000")

if __name__ == "__main__":
    main()
    sys.exit(0)
