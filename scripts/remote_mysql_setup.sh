#!/bin/bash
# 在云服务器上创建 poly 库和用户（与 .env 中 MYSQL_* 一致）
# 用法: bash scripts/remote_mysql_setup.sh

set -e
PROJECT_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$PROJECT_ROOT"
if [ -f .env ]; then set -a; source .env 2>/dev/null || true; set +a; fi

SERVER="${SYNC_ECS_SERVER:-47.238.152.210}"
USER="${SYNC_ECS_USER:-root}"
PASSWORD="${DEPLOY_PASSWORD}"
SQL_FILE="${PROJECT_ROOT}/scripts/remote_mysql_init.sql"

[ -z "$PASSWORD" ] && { echo "需要 DEPLOY_PASSWORD"; exit 1; }
[ ! -f "$SQL_FILE" ] && { echo "缺少 $SQL_FILE"; exit 1; }

echo ">>> 上传 SQL 并执行..."
expect << EXPECTEOF
set timeout 30
spawn scp -o StrictHostKeyChecking=no "$SQL_FILE" ${USER}@${SERVER}:/tmp/mysql_poly_init.sql
expect "password:"
send "${PASSWORD}\r"
expect eof
EXPECTEOF

expect << EXPECTEOF
set timeout 15
spawn ssh -o StrictHostKeyChecking=no ${USER}@${SERVER} "sudo mysql < /tmp/mysql_poly_init.sql && rm -f /tmp/mysql_poly_init.sql && echo OK"
expect "password:"
send "${PASSWORD}\r"
expect eof
EXPECTEOF

echo ">>> MySQL poly 用户与库已就绪"
