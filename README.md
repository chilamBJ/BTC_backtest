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
