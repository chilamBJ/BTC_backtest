"""
Trade 策略数据库模块（本项目专用）
==================================
仅支持 seller、smart_seller。配置从项目根 .env 加载。
"""

import os
from pathlib import Path
from contextlib import contextmanager

import pymysql
from pymysql.cursors import DictCursor
from dbutils.pooled_db import PooledDB

# 项目根目录 (BTC_backtest)，仅从此处加载 .env
_TRADE_DIR = Path(__file__).resolve().parent.parent
_PROJECT_ROOT = _TRADE_DIR.parent
_env_file = _PROJECT_ROOT / ".env"
if _env_file.exists():
    from dotenv import load_dotenv
    load_dotenv(_env_file)

MYSQL_HOST = os.getenv("MYSQL_HOST", "127.0.0.1")
MYSQL_PORT = int(os.getenv("MYSQL_PORT", "3306"))
MYSQL_USER = os.getenv("MYSQL_USER", "poly")
MYSQL_PASS = os.getenv("MYSQL_PASSWORD", "")
MYSQL_DB = os.getenv("MYSQL_DATABASE", "poly")

_pool = None


def _get_pool():
    global _pool
    if _pool is None:
        _pool = PooledDB(
            creator=pymysql,
            maxconnections=10,
            mincached=1,
            maxcached=5,
            blocking=True,
            host=MYSQL_HOST,
            port=MYSQL_PORT,
            user=MYSQL_USER,
            password=MYSQL_PASS,
            database=MYSQL_DB,
            charset="utf8mb4",
            cursorclass=DictCursor,
            autocommit=True,
        )
    return _pool


def get_conn():
    return _get_pool().connection()


def release_conn(conn):
    try:
        conn.close()
    except Exception:
        pass


@contextmanager
def connection():
    conn = get_conn()
    try:
        yield conn
    finally:
        release_conn(conn)


def execute(conn, sql, params=None):
    cur = conn.cursor()
    cur.execute(sql, params or ())
    return cur


def executemany(conn, sql, seq_params):
    cur = conn.cursor()
    cur.executemany(sql, seq_params)
    return cur


def query(conn_or_none, sql, params=None):
    auto = conn_or_none is None
    conn = get_conn() if auto else conn_or_none
    try:
        cur = conn.cursor()
        cur.execute(sql, params or ())
        return cur.fetchall()
    except Exception:
        return []
    finally:
        if auto:
            release_conn(conn)


def query_one(conn, sql, params=None):
    rows = query(conn, sql, params)
    return rows[0] if rows else None


def init_seller_tables(conn):
    cur = conn.cursor()
    cur.execute("""
        CREATE TABLE IF NOT EXISTS sessions (
            id INT AUTO_INCREMENT PRIMARY KEY,
            started_at DATETIME DEFAULT CURRENT_TIMESTAMP,
            ended_at DATETIME NULL,
            mode VARCHAR(20) NOT NULL,
            params JSON,
            total_trades INT DEFAULT 0,
            total_pnl DOUBLE DEFAULT 0,
            summary TEXT
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
    """)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS seller_trades (
            id INT AUTO_INCREMENT PRIMARY KEY,
            session_id INT,
            asset VARCHAR(20) NOT NULL,
            slug VARCHAR(255) NOT NULL,
            epoch INT NOT NULL,
            yes_token VARCHAR(255),
            no_token VARCHAR(255),
            question TEXT,
            discovered_at DATETIME DEFAULT CURRENT_TIMESTAMP,
            trigger_side VARCHAR(10),
            trigger_price DOUBLE,
            trigger_elapsed DOUBLE,
            trigger_phase VARCHAR(20),
            buy_side VARCHAR(10),
            buy_price DOUBLE,
            buy_amount DOUBLE,
            buy_shares DOUBLE,
            entry_at DATETIME NULL,
            order_id VARCHAR(255),
            order_success TINYINT,
            order_response TEXT,
            outcome VARCHAR(50),
            pnl DOUBLE,
            settled_at DATETIME NULL,
            settle_method VARCHAR(50),
            exit_type VARCHAR(20),
            exit_price DOUBLE,
            sl_reason VARCHAR(20),
            skip_reason VARCHAR(30),
            price_log JSON,
            status VARCHAR(20) DEFAULT 'watching',
            FOREIGN KEY (session_id) REFERENCES sessions(id)
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
    """)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS action_log (
            id INT AUTO_INCREMENT PRIMARY KEY,
            session_id INT,
            ts DATETIME DEFAULT CURRENT_TIMESTAMP,
            asset VARCHAR(20),
            market_slug VARCHAR(255),
            action VARCHAR(50) NOT NULL,
            detail TEXT,
            level VARCHAR(10) DEFAULT 'info'
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
    """)
    conn.commit()
    cur.close()


def init_smart_seller_tables(conn):
    cur = conn.cursor()
    cur.execute("""
        CREATE TABLE IF NOT EXISTS sessions (
            id INT AUTO_INCREMENT PRIMARY KEY,
            started_at DATETIME DEFAULT CURRENT_TIMESTAMP,
            ended_at DATETIME NULL,
            mode VARCHAR(20) NOT NULL,
            params JSON,
            total_trades INT DEFAULT 0,
            total_pnl DOUBLE DEFAULT 0,
            summary TEXT
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
    """)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS smart_seller_trades (
            id INT AUTO_INCREMENT PRIMARY KEY,
            session_id INT,
            asset VARCHAR(20) NOT NULL,
            slug VARCHAR(255) NOT NULL,
            epoch INT NOT NULL,
            yes_token VARCHAR(255),
            no_token VARCHAR(255),
            question TEXT,
            discovered_at DATETIME DEFAULT CURRENT_TIMESTAMP,
            trigger_side VARCHAR(10),
            trigger_price DOUBLE,
            trigger_elapsed DOUBLE,
            trigger_phase VARCHAR(20),
            buy_side VARCHAR(10),
            buy_price DOUBLE,
            buy_amount DOUBLE,
            buy_shares DOUBLE,
            entry_at DATETIME NULL,
            order_id VARCHAR(255),
            order_success TINYINT,
            order_response TEXT,
            outcome VARCHAR(50),
            pnl DOUBLE,
            settled_at DATETIME NULL,
            settle_method VARCHAR(50),
            exit_type VARCHAR(20),
            exit_price DOUBLE,
            sl_reason VARCHAR(20),
            skip_reason VARCHAR(30),
            macro_bias VARCHAR(20),
            trend_info JSON,
            price_log JSON,
            status VARCHAR(20) DEFAULT 'pending',
            FOREIGN KEY (session_id) REFERENCES sessions(id)
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
    """)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS action_log (
            id INT AUTO_INCREMENT PRIMARY KEY,
            session_id INT,
            ts DATETIME DEFAULT CURRENT_TIMESTAMP,
            asset VARCHAR(20),
            market_slug VARCHAR(255),
            action VARCHAR(50) NOT NULL,
            detail TEXT,
            level VARCHAR(10) DEFAULT 'info'
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
    """)
    conn.commit()
    cur.close()
