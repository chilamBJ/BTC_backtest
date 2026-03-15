#!/usr/bin/env python3
"""
从项目根目录启动 trade 策略与 Dashboard
========================================
使用项目根 .env，无需在 trade/ 下单独配置。

用法:
  # 模拟盘 (dry-run)
  python scripts/run_trade.py seller --duration 2h
  python scripts/run_trade.py smart_seller --duration 2h
  python scripts/run_trade.py model_seller --duration 2h
  python scripts/run_trade.py paper_bot_v2 --model data/models/model_seller.pkl  # Binance 时钟驱动模拟盘（写入 MySQL）

  # 实盘 (需在 .env 中配置 PRIVATE_KEY、PROXY_ADDRESS 等)
  python scripts/run_trade.py seller --live --amount 2 --duration 4h
  python scripts/run_trade.py smart_seller --live --amount 2 --assets btc,eth
  python scripts/run_trade.py model_seller --live --amount 2 --market-window both

  # Dashboard (需 MySQL 已有 seller/smart_seller 表)
  python scripts/run_trade.py dashboard
  python scripts/run_trade.py dashboard --port 9090
"""

import os
import subprocess
import sys
from pathlib import Path

# 项目根目录 (BTC_backtest)
PROJECT_ROOT = Path(__file__).resolve().parent.parent
TRADE_DIR = PROJECT_ROOT / "trade"

SCRIPT_MAP = {
    "seller": TRADE_DIR / "strategies" / "seller" / "seller_live.py",
    "smart_seller": TRADE_DIR / "strategies" / "smart_seller" / "smart_seller_live.py",
    "model_seller": TRADE_DIR / "strategies" / "model_seller" / "model_seller_live.py",
    "paper_bot_v2": TRADE_DIR / "strategies" / "model_seller" / "paper_bot_v2.py",
    "dashboard": TRADE_DIR / "tools" / "web_dashboard.py",
}


def main():
    # 加载项目根 .env
    env_file = PROJECT_ROOT / ".env"
    if env_file.exists():
        try:
            from dotenv import load_dotenv
            load_dotenv(env_file)
        except ImportError:
            pass
    os.chdir(PROJECT_ROOT)

    if len(sys.argv) < 2:
        print(__doc__)
        print("可用命令: seller | smart_seller | model_seller | dashboard")
        sys.exit(1)

    cmd_name = sys.argv[1].strip().lower()
    script = SCRIPT_MAP.get(cmd_name)
    if not script:
        print(f"未知命令: {cmd_name}", file=sys.stderr)
        print("可用: seller, smart_seller, model_seller, paper_bot_v2, dashboard", file=sys.stderr)
        sys.exit(1)
    if not script.exists():
        print(f"脚本不存在: {script}", file=sys.stderr)
        sys.exit(1)

    args = [sys.executable, str(script)] + sys.argv[2:]
    env = os.environ.copy()
    # 确保 trade 在 PYTHONPATH，便于部分 import
    prev = env.get("PYTHONPATH", "")
    env["PYTHONPATH"] = str(TRADE_DIR) + (os.pathsep + prev if prev else "")

    sys.exit(subprocess.run(args, cwd=PROJECT_ROOT, env=env).returncode)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\n已停止 (Ctrl+C)")
        sys.exit(130)
