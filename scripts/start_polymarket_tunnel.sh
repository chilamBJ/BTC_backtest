#!/bin/bash
# 建立 SSH 隧道：本机 1080 端口为 SOCKS5 代理，流量经云服务器访问 Polymarket
# 用法: bash scripts/start_polymarket_tunnel.sh
# 保持前台运行，另开终端执行测试；测试时设置代理: export HTTP_PROXY=socks5://127.0.0.1:1080 HTTPS_PROXY=socks5://127.0.0.1:1080
# 或使用: bash scripts/run_via_tunnel.sh -- python scripts/run_trade.py seller --duration 5m

set -e
PROJECT_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$PROJECT_ROOT"
# 加载 .env 时临时关闭 set -e，避免 .env 中某行被误解析为命令导致脚本静默退出
if [ -f .env ]; then set -a; set +e; source .env 2>/dev/null; set -e; set +a; fi

SERVER="${SYNC_ECS_SERVER:-47.238.152.210}"
USER="${SYNC_ECS_USER:-root}"
PASSWORD="${DEPLOY_PASSWORD}"
SOCKS_PORT="${POLYMARKET_TUNNEL_PORT:-1080}"

if [ -z "$PASSWORD" ]; then
  echo ">>> 错误: 请在 .env 中配置 DEPLOY_PASSWORD"
  exit 1
fi

echo ">>> 启动 Polymarket 隧道: 本机 SOCKS5 127.0.0.1:${SOCKS_PORT} -> ${USER}@${SERVER}"
echo ">>> 在另一终端运行测试前请执行: export HTTP_PROXY=socks5://127.0.0.1:${SOCKS_PORT} HTTPS_PROXY=socks5://127.0.0.1:${SOCKS_PORT}"
echo ">>> 或: bash scripts/run_via_tunnel.sh -- python scripts/run_trade.py seller --duration 5m"
echo ">>> 按 Ctrl+C 关闭隧道"
echo ""

# SSH 保活，避免长时间空闲被中间网络断开（Operation timed out / Broken pipe）
SSH_OPTS="-o StrictHostKeyChecking=no -o ServerAliveInterval=30 -o ServerAliveCountMax=6 -o ConnectTimeout=15"

# 优先使用 sshpass（brew install sshpass），无需 expect、兼容密码中的特殊字符
if command -v sshpass >/dev/null 2>&1; then
  echo ">>> 使用 sshpass 建立隧道（保活 30s）..."
  exec sshpass -p "$PASSWORD" ssh $SSH_OPTS -D "$SOCKS_PORT" -N "${USER}@${SERVER}"
fi

# 备选：expect，在子 shell 中 export 密码确保 expect 能读到
echo ">>> 使用 expect 建立隧道（若未安装 sshpass 可: brew install sshpass）..."
(
  export DEPLOY_PASSWORD="$PASSWORD"
  expect -c '
set timeout 30
spawn ssh -o StrictHostKeyChecking=no -o ServerAliveInterval=30 -o ServerAliveCountMax=6 -o ConnectTimeout=15 -D '"${SOCKS_PORT}"' -N '"${USER}"'@'"${SERVER}"'
expect {
  -re "\[Pp\]assword:" { send -- "$env(DEPLOY_PASSWORD)\r"; expect eof }
  "Permission denied" { puts "认证失败"; exit 1 }
  timeout { puts "等待密码提示超时"; exit 1 }
  eof
}
'
)
