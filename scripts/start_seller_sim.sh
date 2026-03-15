#!/bin/bash
# 在项目目录下执行，用无缓冲方式跑 seller 模拟盘，日志实时写入 trade_seller.log
# 用法: nohup bash scripts/start_seller_sim.sh >> trade_seller.log 2>&1 &

cd "$(dirname "$0")/.."
source venv/bin/activate 2>/dev/null || true
export PYTHONUNBUFFERED=1
exec python3 scripts/run_trade.py seller --duration 2h
