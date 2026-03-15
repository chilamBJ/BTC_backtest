#!/usr/bin/env python3
"""临时脚本：拉取 Gamma API 查找 5m/15m 市场"""
import asyncio
import aiohttp

async def main():
    async with aiohttp.ClientSession() as s:
        for params in [{"limit": 300}, {"closed": "false", "active": "true", "limit": 300}]:
            async with s.get("https://gamma-api.polymarket.com/markets", params=params) as r:
                data = await r.json()
            print(f"\n=== params={params} => {len(data)} markets ===")
            for m in data:
                q = (m.get("question") or "") + " " + (m.get("slug") or "")
                ql = q.lower()
                if any(p in ql for p in ["5m", "15m", "5 m", "15 m", "5min", "15min"]):
                    print("  ", m.get("question", "")[:60], "| slug:", m.get("slug"), "| active:", m.get("active"), "closed:", m.get("closed"))

asyncio.run(main())
