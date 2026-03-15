#!/bin/bash
# 部署完整项目到香港 ECS（采集器 + trade 策略），便于在云端跑实盘
# 排除 trade_backup。从项目根 .env 读取配置。
# 用法: bash scripts/deploy_full_to_ecs.sh

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
REMOTE_DIR="/root/btc_collector"
PASSWORD="${DEPLOY_PASSWORD}"
TG_TOKEN="${TELEGRAM_BOT_TOKEN}"
ADMIN_CHAT="${ADMIN_CHAT_ID:-}"

if [ -z "$PASSWORD" ]; then
  echo "请在 .env 中配置 DEPLOY_PASSWORD"
  exit 1
fi

echo ">>> 1. 打包项目（排除 trade_backup、__pycache__、.env、.git）..."
tar --exclude='__pycache__' --exclude='*.pyc' --exclude='.env' --exclude='.git' \
  --exclude='trade_backup' --exclude='data' --exclude='*.parquet' \
  -czf /tmp/btc_full_deploy.tar.gz \
  collector/ trade/ scripts/ run_collector.py requirements.txt \
  scripts/btc-collector.service scripts/disk_monitor.sh scripts/check_pm_api.py

echo ">>> 2. 上传到服务器..."
expect << EOF
set timeout 90
spawn scp -o StrictHostKeyChecking=no /tmp/btc_full_deploy.tar.gz ${USER}@${SERVER}:/tmp/
expect "password:"
send "${PASSWORD}\r"
expect eof
EOF

echo ">>> 3. 解压并覆盖 trade/、scripts/ 等..."
expect << EOF
set timeout 60
spawn ssh -o StrictHostKeyChecking=no ${USER}@${SERVER} "mkdir -p ${REMOTE_DIR} && cd ${REMOTE_DIR} && tar xzf /tmp/btc_full_deploy.tar.gz && rm -f /tmp/btc_full_deploy.tar.gz && cp scripts/btc-collector.service /etc/systemd/system/ 2>/dev/null; true"
expect "password:"
send "${PASSWORD}\r"
expect eof
EOF

echo ">>> 4. 安装完整依赖（含 trade：pymysql, py-clob-client 等）..."
expect << EOF
set timeout 180
spawn ssh -o StrictHostKeyChecking=no ${USER}@${SERVER} "cd ${REMOTE_DIR} && (python3 -m venv venv 2>/dev/null || true) && source venv/bin/activate && pip install -q -r requirements.txt"
expect "password:"
send "${PASSWORD}\r"
expect eof
EOF

echo ">>> 5. 同步 .env 到服务器（含 Trade/MySQL/Polymarket 配置）..."
if [ -f .env ]; then
  expect << EOF
set timeout 30
spawn scp -o StrictHostKeyChecking=no .env ${USER}@${SERVER}:${REMOTE_DIR}/.env
expect "password:"
send "${PASSWORD}\r"
expect eof
EOF
  ssh_cmd="chmod 600 ${REMOTE_DIR}/.env"
  expect << EOF
set timeout 15
spawn ssh -o StrictHostKeyChecking=no ${USER}@${SERVER} "${ssh_cmd}"
expect "password:"
send "${PASSWORD}\r"
expect eof
EOF
else
  echo "    跳过：本地无 .env，请手动在服务器 ${REMOTE_DIR}/.env 配置"
fi

echo ">>> 6. 重启采集器（若存在）..."
expect << EOF
set timeout 20
spawn ssh -o StrictHostKeyChecking=no ${USER}@${SERVER} "systemctl daemon-reload 2>/dev/null; systemctl restart btc-collector 2>/dev/null; echo done"
expect "password:"
send "${PASSWORD}\r"
expect eof
EOF

echo ""
echo ">>> 部署完成！"
echo ">>> 项目目录: ${REMOTE_DIR}"
echo ">>> 跑采集器: ssh ${USER}@${SERVER} 'cd ${REMOTE_DIR} && source venv/bin/activate && python run_collector.py'"
echo ">>> 跑 trade 实盘: ssh ${USER}@${SERVER} 'cd ${REMOTE_DIR} && source venv/bin/activate && python scripts/run_trade.py seller --live --amount 2 --duration 4h'"
echo ">>> 跑 trade 模拟: ssh ${USER}@${SERVER} 'cd ${REMOTE_DIR} && source venv/bin/activate && python scripts/run_trade.py seller --duration 2h'"
echo ">>> 云端需能访问 Polymarket 且 MySQL 可用（若用远程 MySQL 请在 .env 配 MYSQL_HOST）。"
