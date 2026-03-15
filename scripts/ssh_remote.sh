#!/bin/bash
# 使用 .env 中的 DEPLOY_PASSWORD 执行 SSH 命令
# 用法: bash scripts/ssh_remote.sh "command to run"
# 示例: bash scripts/ssh_remote.sh "tail -20 /root/btc_collector/collector.log"

set -e
PROJECT_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$PROJECT_ROOT"

if [ -f .env ]; then
  set -a
  source .env 2>/dev/null || true
  set +a
fi

SERVER="${SYNC_ECS_SERVER:-47.238.152.210}"
USER="${SYNC_ECS_USER:-root}"
PASSWORD="${DEPLOY_PASSWORD}"

if [ -z "$PASSWORD" ]; then
  echo "请在 .env 中配置 DEPLOY_PASSWORD"
  exit 1
fi

if [ -z "$1" ]; then
  echo "用法: bash scripts/ssh_remote.sh \"command\""
  exit 1
fi

CMD="$*"
expect << EXPECTEOF
set timeout 30
spawn ssh -o StrictHostKeyChecking=no ${USER}@${SERVER} "$CMD"
expect "password:"
send "${PASSWORD}\r"
expect eof
EXPECTEOF
