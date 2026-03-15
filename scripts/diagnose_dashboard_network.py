#!/usr/bin/env python3
"""
Dashboard 网络诊断：复现「加载失败: 网络请求失败」根因
========================================================
从本机对云端 :8080 连续请求 /api/health、/api/debug/small、/api/data，
统计成功率与失败类型。若小响应(health/small)几乎全成功而 /api/data 常失败，
则根因为：公网链路上大响应被截断（IncompleteRead），需改为小响应或分片请求。

用法:
  # 对云端（需能访问 BASE_URL）
  python scripts/diagnose_dashboard_network.py
  python scripts/diagnose_dashboard_network.py --base http://47.238.152.210:8080 --rounds 60
"""
import argparse
import gzip
import json
import sys
import time
import urllib.request

def fetch(url, accept_gzip=True, timeout=10):
    req = urllib.request.Request(url)
    if accept_gzip:
        req.add_header("Accept-Encoding", "gzip")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            raw = r.read()
            enc = r.headers.get("Content-Encoding", "")
            if enc == "gzip":
                raw = gzip.decompress(raw)
            return raw, len(raw), None
    except Exception as e:
        return None, 0, e

def main():
    p = argparse.ArgumentParser(description="Dashboard 网络诊断")
    p.add_argument("--base", default="http://47.238.152.210:8080", help="Dashboard 根 URL")
    p.add_argument("--rounds", type=int, default=50, help="每种请求的轮数")
    p.add_argument("--delay", type=float, default=0.12, help="请求间隔秒")
    args = p.parse_args()
    base = args.base.rstrip("/")

    health_url = f"{base}/api/health"
    small_url = f"{base}/api/debug/small"
    data_url = f"{base}/api/data?strategy=smart_seller&session=all&page=1&per_page=30&log_page=1&log_per_page=30"

    print("Dashboard 网络诊断")
    print("=" * 60)
    print(f"BASE: {base}")
    print(f"轮数: {args.rounds}  间隔: {args.delay}s")
    print()

    results = {}
    for name, url in [("health", health_url), ("small", small_url), ("data", data_url)]:
        ok = fail = 0
        errors = []
        sizes = []
        for i in range(args.rounds):
            _, size, err = fetch(url)
            if err is None:
                ok += 1
                sizes.append(size)
            else:
                fail += 1
                errors.append(str(err)[:80])
            time.sleep(args.delay)
        results[name] = {"ok": ok, "fail": fail, "errors": errors[:5], "sizes": sizes}
        print(f"{name:8}  成功={ok:3}  失败={fail:3}  成功率={100*ok/(ok+fail):.1f}%  ", end="")
        if sizes:
            print(f"  响应长度 min={min(sizes)} max={max(sizes)}")
        else:
            print()
        if errors:
            for e in errors[:3]:
                print(f"          例: {e}")

    print()
    # 根因结论
    h_ok = results["health"]["ok"]
    s_ok = results["small"]["ok"]
    d_ok = results["data"]["ok"]
    d_fail = results["data"]["fail"]
    if h_ok == args.rounds and s_ok == args.rounds and d_fail > 0:
        print("结论: 小响应(health/small)全成功，/api/data 有失败 → 根因为「大响应在公网链路上被截断」")
        print("建议: 将 /api/data 拆成多个小请求（如 sessions+stats / markets / actions）或减小单次响应体积。")
    elif d_fail > 0 and (h_ok < args.rounds or s_ok < args.rounds):
        print("结论: 小响应也有失败 → 链路不稳定或超时，建议用 SSH 隧道访问 Dashboard。")
    elif d_fail == 0:
        print("结论: 本轮全部成功，无法复现；可增加 --rounds 或在不同网络下再测。")
    else:
        print("结论: 见上统计，需结合具体错误类型判断。")
    return 0 if d_fail == 0 else 1

if __name__ == "__main__":
    sys.exit(main())
