#!/bin/bash
# 部署数据采集器到香港 ECS
# 用法: DEPLOY_PASSWORD='xxx' TELEGRAM_BOT_TOKEN='xxx' [ADMIN_CHAT_ID='xxx'] ./deploy_to_ecs.sh

set -e
SERVER="47.238.152.210"
USER="root"
REMOTE_DIR="/root/btc_collector"
PROJECT_ROOT="$(cd "$(dirname "$0")/.." && pwd)"

PASSWORD="${DEPLOY_PASSWORD}"
TG_TOKEN="${TELEGRAM_BOT_TOKEN}"
ADMIN_CHAT="${ADMIN_CHAT_ID:-}"

if [ -z "$PASSWORD" ] || [ -z "$TG_TOKEN" ]; then
  echo "请设置: DEPLOY_PASSWORD TELEGRAM_BOT_TOKEN"
  exit 1
fi

echo ">>> 1. 打包待部署文件..."
cd "$PROJECT_ROOT"
tar --exclude='__pycache__' --exclude='*.pyc' -czf /tmp/btc_collector_deploy.tar.gz \
  collector/ run_collector.py requirements.txt scripts/btc-collector.service

echo ">>> 2. 上传到服务器..."
expect << EOF
set timeout 60
spawn scp -o StrictHostKeyChecking=no /tmp/btc_collector_deploy.tar.gz ${USER}@${SERVER}:/tmp/
expect "password:"
send "${PASSWORD}\r"
expect eof
EOF

echo ">>> 3. 在服务器上解压并安装依赖..."
expect << EOF
set timeout 90
spawn ssh -o StrictHostKeyChecking=no ${USER}@${SERVER} "mkdir -p ${REMOTE_DIR} && cd ${REMOTE_DIR} && tar xzf /tmp/btc_collector_deploy.tar.gz && rm -f /tmp/btc_collector_deploy.tar.gz && cp scripts/btc-collector.service /etc/systemd/system/"
expect "password:"
send "${PASSWORD}\r"
expect eof
EOF

echo ">>> 4. 安装 Python 依赖..."
REMOTE_CMD="cd ${REMOTE_DIR} && (python3 -m venv venv 2>/dev/null || true) && source venv/bin/activate 2>/dev/null && pip install -q pandas pyarrow aiohttp python-telegram-bot"
expect << EOF
set timeout 180
spawn ssh -o StrictHostKeyChecking=no ${USER}@${SERVER} "${REMOTE_CMD}"
expect "password:"
send "${PASSWORD}\r"
expect eof
EOF

echo ">>> 5. 创建 .env 并配置 systemd 服务..."
REMOTE_CMD="echo 'TELEGRAM_BOT_TOKEN=${TG_TOKEN}' > ${REMOTE_DIR}/.env && echo 'ADMIN_CHAT_ID=${ADMIN_CHAT}' >> ${REMOTE_DIR}/.env && chmod 600 ${REMOTE_DIR}/.env && systemctl daemon-reload && systemctl enable btc-collector && systemctl restart btc-collector"
expect << EOF
set timeout 30
spawn ssh -o StrictHostKeyChecking=no ${USER}@${SERVER} "${REMOTE_CMD}"
expect "password:"
send "${PASSWORD}\r"
expect eof
EOF

echo ">>> 6. 验证进程..."
sleep 4
expect << EOF
set timeout 15
spawn ssh -o StrictHostKeyChecking=no ${USER}@${SERVER} "systemctl status btc-collector --no-pager; echo '---'; tail -8 ${REMOTE_DIR}/collector.log"
expect "password:"
send "${PASSWORD}\r"
expect eof
EOF

echo ""
echo ">>> 部署完成！"
echo ">>> 采集器目录: ${REMOTE_DIR}"
echo ">>> 查看日志: ssh ${USER}@${SERVER} 'tail -f ${REMOTE_DIR}/collector.log'"
echo ">>> 管理服务: ssh ${USER}@${SERVER} 'systemctl status btc-collector'"
if [ -z "$ADMIN_CHAT" ]; then
  echo ">>> 提示: 未设置 ADMIN_CHAT_ID，Telegram 告警将不会发送。"
  echo "    获取方式: 在 Telegram 中给你的机器人发 /start，然后访问:"
  echo "    https://api.telegram.org/bot${TG_TOKEN}/getUpdates"
  echo "    在返回的 JSON 中找 chat.id，设为 ADMIN_CHAT_ID 后重启采集器。"
fi
