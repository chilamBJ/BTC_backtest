#!/bin/bash
# 云端磁盘空间监控：每日扫描，可用空间 < 10G 时通过 Telegram 通知
# 部署到 ECS，由 cron 每日执行（如 0 2 * * * 每天 2:00）
# 依赖：.env 中的 TELEGRAM_BOT_TOKEN、ADMIN_CHAT_ID

set -e
THRESHOLD_GB=10
ENV_FILE="${ENV_FILE:-/root/btc_collector/.env}"

if [ ! -f "$ENV_FILE" ]; then
  exit 0
fi

# 读取 .env
TELEGRAM_BOT_TOKEN=$(grep '^TELEGRAM_BOT_TOKEN=' "$ENV_FILE" | cut -d= -f2-)
ADMIN_CHAT_ID=$(grep '^ADMIN_CHAT_ID=' "$ENV_FILE" | cut -d= -f2-)

if [ -z "$TELEGRAM_BOT_TOKEN" ] || [ -z "$ADMIN_CHAT_ID" ]; then
  exit 0
fi

# 获取根分区可用空间（MB）
AVAIL_MB=$(df -BM / | awk 'NR==2 {print $4}' | sed 's/M//')
AVAIL_GB=$((AVAIL_MB / 1024))

if [ "$AVAIL_GB" -lt "$THRESHOLD_GB" ]; then
  MSG="⚠️ ECS 磁盘空间不足
可用: ${AVAIL_GB}GB (阈值: ${THRESHOLD_GB}GB)
请运行本地 sync_parquet_from_ecs.py 拉取并清理 parquet 文件"
  curl -s -X POST "https://api.telegram.org/bot${TELEGRAM_BOT_TOKEN}/sendMessage" \
    --data-urlencode "chat_id=${ADMIN_CHAT_ID}" \
    --data-urlencode "text=${MSG}" \
    --connect-timeout 10 --max-time 15 > /dev/null || true
fi
