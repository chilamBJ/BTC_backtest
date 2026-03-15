#!/usr/bin/env python3
"""
清理 model_seller_trades 中重复的市场记录（同一 slug 多轮次多次写入）
======================================================================
只保留每个 slug 下 id 最大的那条，删除其余。不删 sessions 等其他表。

用法（在项目根目录）：
  python scripts/prune_duplicate_markets.py
  python scripts/prune_duplicate_markets.py --yes
"""

import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT / "trade"))

from dotenv import load_dotenv
load_dotenv(PROJECT_ROOT / ".env")

from utils.db import get_conn, release_conn


def main():
    import argparse
    p = argparse.ArgumentParser(description="清理 model_seller_trades 重复市场（按 slug 只保留最新一条）")
    p.add_argument("--yes", "-y", action="store_true", help="跳过确认直接执行")
    args = p.parse_args()

    conn = get_conn()
    try:
        cur = conn.cursor()
        cur.execute("""
            SELECT slug, COUNT(*) AS cnt, COUNT(*) - 1 AS to_delete
            FROM model_seller_trades
            GROUP BY slug HAVING COUNT(*) > 1
        """)
        dupes = cur.fetchall()
        if not dupes:
            print("model_seller_trades 中无重复 slug，无需清理。")
            return

        total_delete = sum(d["to_delete"] for d in dupes)
        print(f"发现 {len(dupes)} 个 slug 存在重复，共可删除 {total_delete} 条记录（每 slug 保留 id 最大的一条）")
        if not args.yes:
            ok = input("确认清理？输入 yes 继续: ").strip().lower()
            if ok != "yes":
                print("已取消。")
                return

        # 删除 id 不在「每个 slug 的 max(id)」集合中的行
        cur.execute("""
            DELETE t FROM model_seller_trades t
            INNER JOIN (
                SELECT slug, MAX(id) AS keep_id
                FROM model_seller_trades
                GROUP BY slug
            ) k ON t.slug = k.slug AND t.id <> k.keep_id
        """)
        deleted = cur.rowcount
        conn.commit()
        print(f"已删除 {deleted} 条重复记录。")
    finally:
        release_conn(conn)


if __name__ == "__main__":
    main()
