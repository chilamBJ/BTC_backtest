# BTC 5 分钟连续趋势二元预测回测系统

基于 methodology.md 的 BTC 5 分钟 K 线连续趋势预测回测与 Polymarket 二元期权策略研究。

命名规范见 [docs/NAMING.md](docs/NAMING.md)（策略 ID、轮次/批次、DB、界面用语等）。

## 项目结构

```
BTC_backtest/
├── backtest/           # 回测模块
│   ├── data_loader.py      # 数据加载、事件触发、Mock 生成
│   ├── feature_engineering.py  # 特征计算
│   └── model_backtest.py   # LGBM 训练、阈值分析、归因报告
├── collector/          # 数据采集模块
│   └── data_collector.py   # Binance + Polymarket 7x24 采集
├── trade/              # Polymarket 交易策略（本项目在用，seller/smart_seller/model_seller+Dashboard）
│   ├── strategies/seller/       # 卖方策略
│   ├── strategies/smart_seller/ # 智能卖方策略
│   ├── strategies/model_seller/ # 回测实盘化模型策略
│   ├── tools/web_dashboard.py   # 交易监控 Dashboard
│   └── utils/db.py              # MySQL 连接与表结构
├── trade_backup/       # 参考备份（可删除，删除后不影响运行）
├── scripts/            # 脚本（部署、SSH、启动策略等）
├── docs/               # 文档
├── data/               # 数据目录（5m/1s K 线等）
├── run_backtest.py     # 回测入口
├── run_collector.py    # 采集器入口
├── methodology.md      # 策略方法论
└── requirements.txt
```

## 快速开始

### 回测

```bash
# Mock 数据跑通
python run_backtest.py --mock

# 真实数据（需提供 5m + 1s CSV）
python run_backtest.py --kline-5m data/BINANCE_BTCUSDT_5m_*.csv --trades-1s data/BINANCE_BTCUSDT_1s_*.csv
```

### 数据采集

```bash
# 需配置 TELEGRAM_BOT_TOKEN、ADMIN_CHAT_ID（可选）
python run_collector.py
```

### Trade 策略（seller / smart_seller / model_seller）与 Dashboard

**统一配置**：全项目只用一份 `.env`（项目根）、一份 `requirements.txt`。策略与 Dashboard 从项目根读配置。`trade/` 为运行所需的最小文件；`trade_backup/` 为原始参考备份，可随时删除。

需先安装依赖并配置 MySQL（见下方）。

**1. 依赖与 MySQL**

```bash
pip install -r requirements.txt
```

本地需有 MySQL，并创建数据库（例如 `poly`）。在 `.env` 中配置：

- `MYSQL_HOST`、`MYSQL_PORT`、`MYSQL_USER`、`MYSQL_PASSWORD`、`MYSQL_DATABASE`

首次运行策略时会自动创建 `sessions`、`seller_trades` / `smart_seller_trades` / `model_seller_trades`、`action_log` 等表。

**检查 MySQL 是否就绪**（与 .env 一致时应能连上）：

```bash
mysql -u poly -p -h 127.0.0.1 -e "USE poly; SELECT 1;"
# 输入 .env 中的 MYSQL_PASSWORD
```

**2. 模拟盘（dry-run，不需 Polymarket 私钥）**

```bash
# 卖方策略，跑 2 小时
python scripts/run_trade.py seller --duration 2h

# 智能卖方策略
python scripts/run_trade.py smart_seller --duration 2h

# 模型卖方策略（需先配置模型路径）
python scripts/run_trade.py model_seller --duration 2h --market-window both
```

**3. 实盘（需在 .env 中配置 Polymarket 相关）**

实盘前在 `.env` 中填写（可后续找你要到后写入）：

- `PRIVATE_KEY`：钱包私钥（0x 开头或 64 位十六进制）
- `PROXY_ADDRESS` 或 `WALLET_ADDRESS`：Polymarket 下单用地址

示例：

```bash
python scripts/run_trade.py seller --live --amount 2 --duration 4h
python scripts/run_trade.py smart_seller --live --amount 2 --assets btc,eth
python scripts/run_trade.py model_seller --live --amount 2 --market-window both
```

**4. Dashboard（查看会话与成交）**

确保 MySQL 中已有策略表（至少跑过一次 seller / smart_seller / model_seller），然后：

```bash
# 推荐：若 8080 已被占用会先 kill 再启动，保证 Dashboard 独占 8080
bash scripts/start_dashboard.sh

# 或直接指定端口
python scripts/run_trade.py dashboard
# 默认 http://localhost:8080，可加 --port 9090
```

更多参数见各策略 `--help`，例如：

- `python scripts/run_trade.py seller --help`
- `python scripts/run_trade.py smart_seller --help`
- `python scripts/run_trade.py model_seller --help`

`model_seller` 额外配置：

- `.env` 增加 `MODEL_SELLER_MODEL_PATH=/absolute/path/to/model.pkl`
- 或启动时使用 `--model-path /absolute/path/to/model.pkl`

**5. 本地测试需连 Polymarket 时：SSH 隧道**

本机若无法直连 Polymarket，或希望与云端环境一致，可经云服务器网络访问。

**操作步骤：**

1. **终端 1**：启动隧道（保持运行）
   ```bash
   bash scripts/start_polymarket_tunnel.sh
   ```
   需 `.env` 中有 `DEPLOY_PASSWORD`（或按提示输入密码）。成功后会在 `127.0.0.1:1080` 建立 SOCKS5 代理。

2. **终端 2**：经隧道运行策略
   ```bash
   # 模拟盘
   bash scripts/run_via_tunnel.sh -- python scripts/run_trade.py seller --duration 1h

   # 实盘（加 --live）
   bash scripts/run_via_tunnel.sh -- python scripts/run_trade.py seller --live --amount 2 --duration 1h
   ```

3. **确认走代理**：终端输出中应出现 `✅ 请求经代理: socks5://127.0.0.1:1080`。若提示 `⚠️ aiohttp-socks 未安装`，先执行：
   ```bash
   pip install aiohttp-socks
   ```

4. **查看结果**：本地启动 Dashboard（`bash scripts/start_dashboard.sh`）访问 http://localhost:8080，或直接看策略终端日志。

**推荐流程**：本地改代码 → 经隧道在本地跑测试 → 确认无误后执行 `bash scripts/deploy_full_to_ecs.sh` 一次性推送到云端。

**6. 云端跑模拟盘**

部署到 ECS 后，需在服务器上配置 MySQL（库 `poly`、用户 `poly`），再启动模拟盘：

```bash
# 本地执行：在云端创建 MySQL 库和用户（与 .env 中 MYSQL_* 一致）
bash scripts/remote_mysql_setup.sh

# 本地执行：部署完整项目到云端
bash scripts/deploy_full_to_ecs.sh

# 云端跑模拟盘（SSH 上去后执行，或使用 scripts/ssh_remote.sh）
ssh root@<服务器> 'cd /root/btc_collector && source venv/bin/activate && python3 scripts/run_trade.py seller --duration 2h'
# 后台跑（务必加 PYTHONUNBUFFERED=1 否则日志不实时）：PYTHONUNBUFFERED=1 nohup python3 scripts/run_trade.py seller --duration 2h >> trade_seller.log 2>&1 &
# 本地看日志（命令末尾的引号必须闭合）：
#   bash scripts/ssh_remote.sh "tail -f /root/btc_collector/trade_seller.log"
# 若日志一直为空，需用无缓冲重启模拟盘后再 tail：
#   bash scripts/ssh_remote.sh "cd /root/btc_collector && pkill -f 'run_trade.py seller' 2>/dev/null; source venv/bin/activate && PYTHONUNBUFFERED=1 nohup python3 scripts/run_trade.py seller --duration 2h >> trade_seller.log 2>&1 &"
```

**7. 访问云端 Dashboard 与实时看模拟盘输出**

两种方式：

- **方式一：实时日志（模拟盘终端输出）**  
  在本地执行（末尾引号要闭合）：
  ```bash
  bash scripts/ssh_remote.sh "tail -f /root/btc_collector/trade_seller.log"
  ```
  即可在本地终端里实时看到云端模拟盘的打印内容（发现市场、监控、挂单等），Ctrl+C 退出。

- **方式二：Web Dashboard（会话 / 成交 / PnL）**  
  **A. 公网直连（推荐，无需 SSH 隧道）**  
  1）在云端启动 Dashboard（默认已监听 0.0.0.0）：
  ```bash
  bash scripts/ssh_remote.sh "cd /root/btc_collector && source venv/bin/activate && nohup python3 scripts/run_trade.py dashboard --port 8080 >> dashboard.log 2>&1 &"
  ```
  2）在云服务器控制台（如阿里云 ECS）的**安全组**里放行**入方向**端口 **8080**（协议 TCP）。  
  3）在浏览器打开：**http://47.238.152.210:8080**（将 IP 换成你的服务器公网 IP）。  
  即可直接访问 Poly Trader Dashboard。

  **B. SSH 隧道（不开放公网端口时使用）**  
  1）同上在云端启动 Dashboard。  
  2）本地执行并保持终端不关：`ssh -L 8080:localhost:8080 root@47.238.152.210`  
  3）浏览器打开：**http://localhost:8080**

  说明：Dashboard 无登录验证，公网开放后任何人知悉地址即可访问，建议仅在可信环境使用或通过安全组限制来源 IP。

## 依赖

```bash
pip install -r requirements.txt
```

## 云端部署与代码同步

采集器与 Trade/Dashboard 部署在香港 ECS。**本地为代码源，云端通过部署脚本同步。** 需要连接 Polymarket 的改动：先在本地经 SSH 隧道测试（见上文「本地测试需连 Polymarket 时：SSH 隧道」），通过后再执行全量部署。

### 部署到云端

在项目根目录创建 `.env`（可复制 `.env.example`），填写后直接运行：

```bash
# .env 示例见 .env.example，配置后无需每次输入密码
bash scripts/deploy_to_ecs.sh
```

`.env` 需包含：`DEPLOY_PASSWORD`、`TELEGRAM_BOT_TOKEN`、`ADMIN_CHAT_ID`（可选）

### 代码同步流程

| 操作 | 命令 |
|------|------|
| 本地改代码 | 正常编辑 |
| 同步到云端 | `bash scripts/deploy_to_ecs.sh` |
| 提交到 Git | `git add -A && git commit -m "..." && git push` |

**推荐流程**：本地修改 → 部署到云端验证 → 提交并推送到 Git。保持本地、云端、Git 三者一致。

### 云端管理

```bash
# 查看状态
ssh root@47.238.152.210 'systemctl status btc-collector'

# 查看日志（从 .env 读取密码，无需交互）
bash scripts/ssh_remote.sh "tail -f /root/btc_collector/collector.log"

# 重启
bash scripts/ssh_remote.sh "systemctl restart btc-collector"

# PM 5m/15m 为 N/A 时，在云端诊断 API 连通性
bash scripts/ssh_remote.sh "cd /root/btc_collector && python3 scripts/check_pm_api.py"
```

### PM 5m/15m 为 N/A 的排查

若 `/status` 显示 `PM 5m: cond=N/A… tokens=0`：

1. **确认已部署最新代码**：`bash scripts/deploy_to_ecs.sh`
2. **在云端跑诊断**：`ssh root@47.238.152.210 'cd /root/btc_collector && python3 scripts/check_pm_api.py'`
3. 若诊断失败，检查云端能否访问 `gamma-api.polymarket.com`（防火墙/代理）
4. 连续 3 次失败会收到 Telegram 告警，含最后错误信息

### 磁盘空间与 parquet 同步

ECS 仅 40GB，parquet 易占满。自动化流程：

1. **云端监控**：cron 每日 2:00 扫描磁盘，可用 < 10GB 时 Telegram 通知
2. **本地同步**：收到通知后运行，下载完整 parquet 到本地并删除云端

```bash
# 从 .env 读取配置，直接运行
python scripts/sync_parquet_from_ecs.py
```

- 仅同步「昨日及更早」文件（当日文件仍在写入，不碰）
- 下载后校验大小一致才删除云端
- 本地默认保存到 `data/collector_data/`

## 许可证

MIT

## telegram交互指令设计 (Commands)：
*   `/status`：回复当前系统健康度。包括：运行时间、今日已采集的数据行数、当前 4 个 Queue 的积压长度（极其重要，若 Queue 持续变长说明 IO 堵死了）、最近一次自愈发生的时间。
*   `/pause`：设置一个全局标志位 `is_paused = True`。各大网络接收流暂时丢弃数据，不压入队列（用于极端行情下服务器扛不住时人工介入）。
*   `/resume`：恢复压入队列。
*   `/stop`：触发优雅退出（Graceful Shutdown）。