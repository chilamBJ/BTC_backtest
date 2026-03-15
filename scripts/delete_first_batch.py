#!/usr/bin/env python3
"""
删除 Dashboard 中「第 1 轮次」相关的所有数据库记录：
- sessions 表中最早的一条 session（按 id 升序）
- 对应 session_id 的 seller_trades / smart_seller_trades / model_seller_trades / action_log 等记录

设计目的：彻底移除最早一轮有问题的数据，验证并消除其对后续轮次和「全部轮次」的影响。

使用方式（云端执行，更推荐）：
  cd /root/btc_collector
  source venv/bin/activate
  python scripts/delete_first_batch.py
"""

import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT / "trade"))

from dotenv import load_dotenv  # type: ignore

load_dotenv(PROJECT_ROOT / ".env")

from utils.db import get_conn, release_conn, query, execute  # type: ignore


def main() -> None:
    conn = get_conn()
    try:
        rows = query(conn, "SELECT id, started_at FROM sessions ORDER BY id ASC LIMIT 1")
        if not rows:
            print("sessions 表为空，无需删除。")
            return
        first = rows[0]
        sid = int(first["id"])
        print(f"发现最早 session: id={sid}, started_at={first.get('started_at')}")

        # 统计将要删除的数据量，方便确认
        counts_sql = [
            ("seller_trades", "SELECT COUNT(*) AS c FROM seller_trades WHERE session_id = %s"),
            ("smart_seller_trades", "SELECT COUNT(*) AS c FROM smart_seller_trades WHERE session_id = %s"),
            ("action_log", "SELECT COUNT(*) AS c FROM action_log WHERE session_id = %s"),
        ]
        for name, sql in counts_sql:
            try:
                c_row = query(conn, sql, (sid,))
                c = int(c_row[0]["c"]) if c_row else 0
                print(f"  {name}: 将删除 {c} 行")
            except Exception as e:  # 表可能不存在，忽略
                print(f"  {name}: 统计失败（可能表不存在）: {e}")

        # 依次删除子表，再删 sessions
        for tbl in ("seller_trades", "smart_seller_trades", "action_log"):
            try:
                execute(conn, f"DELETE FROM {tbl} WHERE session_id = %s", (sid,))
                print(f"已删除 {tbl} 中 session_id={sid} 的记录")
            except Exception as e:
                print(f"删除 {tbl} 失败（可忽略）: {e}")

        execute(conn, "DELETE FROM sessions WHERE id = %s", (sid,))
        print(f"已删除 sessions 中 id={sid} 的记录")
        print("第 1 轮次相关记录已全部清理。")
    finally:
        release_conn(conn)


if __name__ == "__main__":
    main()

