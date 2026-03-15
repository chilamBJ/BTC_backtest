# BTC 5 分钟连续趋势二元预测回测系统

基于 methodology.md 的 BTC 5 分钟 K 线连续趋势预测回测与 Polymarket 二元期权策略研究。

## 项目结构

```
BTC_backtest/
├── backtest/           # 回测模块
│   ├── data_loader.py      # 数据加载、事件触发、Mock 生成
│   ├── feature_engineering.py  # 特征计算
│   └── model_backtest.py   # LGBM 训练、阈值分析、归因报告
├── collector/          # 数据采集模块
│   └── data_collector.py   # Binance + Polymarket 7x24 采集
├── docs/               # 文档
│   └── POLYMARKET_API_ANALYSIS.md
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

## 依赖

```bash
pip install -r requirements.txt
```

## 许可证

MIT

## telegram交互指令设计 (Commands)：
*   `/status`：回复当前系统健康度。包括：运行时间、今日已采集的数据行数、当前 4 个 Queue 的积压长度（极其重要，若 Queue 持续变长说明 IO 堵死了）、最近一次自愈发生的时间。
*   `/pause`：设置一个全局标志位 `is_paused = True`。各大网络接收流暂时丢弃数据，不压入队列（用于极端行情下服务器扛不住时人工介入）。
*   `/resume`：恢复压入队列。
*   `/stop`：触发优雅退出（Graceful Shutdown）。