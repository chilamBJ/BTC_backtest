#!/usr/bin/env python3
"""
清空 Trade 相关数据库历史记录
==============================
清空 sessions、各策略 trades、action_log、model_seller_activity 等表，
用于重置 Dashboard 与策略历史。执行前会确认。

用法（在项目根目录）：
  python scripts/clear_trade_history.py
  python scripts/clear_trade_history.py --yes   # 跳过确认
"""

import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT / "trade"))

from dotenv import load_dotenv
load_dotenv(PROJECT_ROOT / ".env")

from utils.db import get_conn, release_conn


# 需要清空的表（有外键的放前面，最后清 sessions）
TABLES = [
    "action_log",
    "model_seller_activity",
    "model_seller_trades",
    "smart_seller_trades",
    "seller_trades",
    "sessions",
]


def main():
    import argparse
    p = argparse.ArgumentParser(description="清空 Trade 数据库历史记录")
    p.add_argument("--yes", "-y", action="store_true", help="跳过确认直接执行")
    args = p.parse_args()

    conn = get_conn()
    try:
        cur = conn.cursor()
        # 统计当前行数
        total = 0
        for tbl in TABLES:
            try:
                cur.execute(f"SELECT COUNT(*) AS c FROM `{tbl}`")
                row = cur.fetchone()
                n = int(row["c"]) if row else 0
                total += n
                print(f"  {tbl}: {n} 行")
            except Exception as e:
                print(f"  {tbl}: (表不存在或错误) {e}")
        cur.close()

        if total == 0:
            print("所有表均为空，无需清空。")
            return

        print(f"\n合计约 {total} 行将被清空。")
        if not args.yes:
            ok = input("确认清空？输入 yes 继续: ").strip().lower()
            if ok != "yes":
                print("已取消。")
                return

        cur = conn.cursor()
        cur.execute("SET FOREIGN_KEY_CHECKS = 0")
        for tbl in TABLES:
            try:
                cur.execute(f"TRUNCATE TABLE `{tbl}`")
                print(f"  已清空: {tbl}")
            except Exception as e:
                print(f"  清空 {tbl} 失败: {e}")
        cur.execute("SET FOREIGN_KEY_CHECKS = 1")
        conn.commit()
        cur.close()
        print("\n数据库历史记录已清空。")
    finally:
        release_conn(conn)


if __name__ == "__main__":
    main()
