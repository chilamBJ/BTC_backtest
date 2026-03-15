#!/usr/bin/env python3
"""
清理 Dashboard 所用 MySQL 表中的控制字符，修复「全部轮次」加载失败 (Unterminated string in JSON)。
可本地或云端执行，从项目根 .env 读取 MYSQL_*。
用法: python scripts/clean_dashboard_db.py
"""
import os
import re
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT / "trade"))

from dotenv import load_dotenv
load_dotenv(PROJECT_ROOT / ".env")

# 控制字符 U+0000～U+001F（保留 \t \n），替换为空格
_CONTROL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f]")


def clean_string(s):
    if s is None or not isinstance(s, str):
        return s
    return _CONTROL.sub(" ", s)


def main():
    from utils.db import get_conn, release_conn, query, execute

    conn = get_conn()
    try:
        # seller_trades: question, order_response 为 TEXT
        rows = query(conn, "SELECT id, question, order_response FROM seller_trades")
        for r in rows:
            q = clean_string(r.get("question"))
            o = clean_string(r.get("order_response"))
            if q is not r.get("question") or o is not r.get("order_response"):
                execute(
                    conn,
                    "UPDATE seller_trades SET question = %s, order_response = %s WHERE id = %s",
                    (q or "", o or "", r["id"]),
                )
                print(f"  seller_trades id={r['id']} 已清理")
        # action_log: detail 为 TEXT
        rows = query(conn, "SELECT id, detail FROM action_log")
        for r in rows:
            d = clean_string(r.get("detail"))
            if d is not r.get("detail"):
                execute(conn, "UPDATE action_log SET detail = %s WHERE id = %s", (d or "", r["id"]))
                print(f"  action_log id={r['id']} 已清理")
        print("清理完成。")
    finally:
        release_conn(conn)


if __name__ == "__main__":
    main()
