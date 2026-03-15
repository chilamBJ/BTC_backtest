#!/bin/bash
# 本地启动 Dashboard：若 8080 已被占用则先 kill，再在 8080 上启动
# 用法: bash scripts/start_dashboard.sh

set -e
PROJECT_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$PROJECT_ROOT"
PORT="${1:-8080}"

# 查找占用端口的 PID 并 kill
if command -v lsof >/dev/null 2>&1; then
  PIDS=$(lsof -ti :"${PORT}" 2>/dev/null || true)
  if [ -n "$PIDS" ]; then
    echo ">>> 端口 ${PORT} 已被占用，结束进程: $PIDS"
    echo "$PIDS" | xargs kill -9 2>/dev/null || true
    sleep 1
  fi
fi

# 若 8080 仍被占用可再试一次
if command -v lsof >/dev/null 2>&1; then
  PIDS=$(lsof -ti :"${PORT}" 2>/dev/null || true)
  if [ -n "$PIDS" ]; then
    echo ">>> 端口 ${PORT} 仍被占用，强制结束: $PIDS"
    echo "$PIDS" | xargs kill -9 2>/dev/null || true
    sleep 1
  fi
fi

# 使用项目虚拟环境的 Python（确保有 dotenv 等依赖），支持 venv 或 .venv
if [ -f "$PROJECT_ROOT/.venv/bin/activate" ]; then
  source "$PROJECT_ROOT/.venv/bin/activate"
elif [ -f "$PROJECT_ROOT/venv/bin/activate" ]; then
  source "$PROJECT_ROOT/venv/bin/activate"
fi

echo ">>> 在端口 ${PORT} 启动 Dashboard..."
exec python3 scripts/run_trade.py dashboard --port "${PORT}"
