#!/bin/bash
# 在已启动 Polymarket 隧道的前提下，通过云服务器网络执行命令（自动设置 HTTP/HTTPS 代理）
# 用法: bash scripts/run_via_tunnel.sh -- python scripts/run_trade.py seller --duration 5m
#        bash scripts/run_via_tunnel.sh -- pytest tests/...

set -e
PROJECT_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$PROJECT_ROOT"
if [ -f .env ]; then set -a; source .env 2>/dev/null || true; set +a; fi

SOCKS_PORT="${POLYMARKET_TUNNEL_PORT:-1080}"
PROXY="socks5://127.0.0.1:${SOCKS_PORT}"

# 检测隧道是否已启动（本机 SOCKS 端口）
if command -v nc >/dev/null 2>&1; then
  if ! nc -z 127.0.0.1 ${SOCKS_PORT} 2>/dev/null; then
    echo "错误: 本机 127.0.0.1:${SOCKS_PORT} 未监听。请先启动隧道: bash scripts/start_polymarket_tunnel.sh"
    exit 1
  fi
else
  echo "提示: 若连接失败，请先运行 bash scripts/start_polymarket_tunnel.sh"
fi

export HTTP_PROXY="${PROXY}"
export HTTPS_PROXY="${PROXY}"
export ALL_PROXY="${PROXY}"

# 支持 run_via_tunnel.sh -- cmd arg1 arg2
while [ $# -gt 0 ]; do
  if [ "$1" = "--" ]; then
    shift
    echo ">>> 使用代理 ${PROXY} 执行: $*"
    exec "$@"
  fi
  shift
done
echo "用法: bash scripts/run_via_tunnel.sh -- <cmd> [args]"
exit 1
