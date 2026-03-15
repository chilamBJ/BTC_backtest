# 量化回测项目 Prompt 历史与上下文压缩

本文档记录用户所有 prompt 及 AI 回复的阶段性总结，用于上下文压缩与后续协作。

---

## 1. 初始实现 (methodology.md)

**用户 Prompt**：请按照 methodology.md 中的说明，完成任务。

**AI 回复总结**：
- 创建完整回测管道：`data_loader.py`（数据加载/对齐/Mock）、`feature_engineering.py`（5 个正交特征）、`model_backtest.py`（LGBM 训练/评估）、`run_pipeline.py`（主入口）、`requirements.txt`
- 事件触发器：Bar1/Bar2 同向且 Bar2 更强，截面时间 = Bar2 第 299 秒
- 5 特征：feat_1(CVD)、feat_2(Tick 强度比)、feat_3(OI 增量)、feat_4(基差变化)、feat_5(4H VWAP 偏离)
- 时间划分不 shuffle，LGBM 输出概率

---

## 2. 真实数据运行与 LightGBM

**用户 Prompt**：我已把 1s 和 5m 的 1 年 K 线文件放在 /data 文件夹中，请安装 LightGBM 并运行回测。

**AI 回复总结**：
- 适配 Binance 格式：`time_key`、`taker_buy_base_asset_volume`、`num_trades`
- 新增 `load_bar_stats_from_1s_binance()`：用 polars 流式聚合 3.9GB 1s 文件，避免 OOM
- 无 OI/Premium 时用 `create_dummy_oi_premium_from_5m()` 生成占位数据
- LightGBM 需 libomp（Mac），未安装时回退逻辑回归
- 修复 polars bar_5m：epoch 秒转微秒再 cast Datetime("us")

---

## 3. 重新运行（libomp 已安装）

**用户 Prompt**：我已安装 libomp，请重新运行回测。

**AI 回复总结**：使用 LightGBM 重新跑通，输出特征重要性等报告。

---

## 4. 训练前 X_train 检查

**用户 Prompt**：请在送入 LightGBM 训练之前，先执行并打印 `print(X_train.describe())` 以及 `print(X_train.isnull().sum())`。我必须确认 feat_1 到 feat_4 不是全为 NaN，且标准差不为 0。

**AI 回复总结**：
- 在 `model_backtest.py` 的 `run_backtest_pipeline` 中、`train_lgb` 之前添加上述打印
- 发现：feat_1/2/3 全常数(std=0)，feat_4 全 NaN

---

## 5. 数据工程修复 (Bug Fix Task)

**用户 Prompt**：量化回测数据工程修复指令——修复 feat_1/2 时间对齐、废弃 feat_3/4 全 0 占位、增加动量阈值过滤 |Δ|>0.3%。

**AI 回复总结**：
- feat_1/2：5m 与 1s 统一 UTC，修正 polars bar_5m 微秒，合并后空切片断言
- feat_3/4：`create_dummy_oi_premium_from_5m` 生成带方差 Mock，OI/premium 用 `asof` 对齐
- 事件触发：`abs((Bar2_Close - Bar1_Open) / Bar1_Open) > 0.003`
- 事件数从 52,283 降至 4,450

---

## 6. feat_5 修复与最终报告

**用户 Prompt**：修复 feat_5 (4H VWAP) 的计算与对齐；80/20 时间划分；输出《最终评估报告》。

**AI 回复总结**：
- 新增 `compute_5m_vwap_feat5()`：在 5m 上用 48 根 K 线 rolling 算 typical_price、pv、vwap_4h、feat_5
- 事件表按 bar2_start 与 5m 的 feat_5 merge
- `train_ratio=0.8`，新增 `print_final_evaluation_report()`

---

## 7. 模型调优与反向 Alpha

**用户 Prompt**：量化回测调优指令——优化 LGBM 超参、增加反向阈值分析(P<0.50/0.48/0.45)、输出 feat_2 分布相关性。

**AI 回复总结**：
- LGBM 新参数：n_estimators=150, learning_rate=0.01, num_leaves=15, max_depth=4, min_child_samples=50, subsample=0.8, colsample_bytree=0.8, class_weight='balanced'
- 报告增加：顺势 P>0.50/0.55/0.60；反向 P<0.50/0.48/0.45 反转胜率
- feat_2 > 75%/90% 分位数时的 Y=1/Y=0 胜率

---

## 8. 策略提纯与 Polymarket EV

**用户 Prompt**：剥离 feat_3/feat_4 Mock 噪音；feat_2 的 90% 极端分位；Polymarket $0.50 恒定成本 EV 模拟。

**AI 回复总结**：
- `PURIFIED_FEATURES = ["feat_1", "feat_2", "feat_5"]`
- 报告增加 feat_2 > 75%/90% 统计、Polymarket EV（成本 $0.50、收益 $1.00、ROI）

---

## 9. 时区异象特征增强 (V2)

**用户 Prompt**：增加时区与时间周期特征、波动率环境 feat_vol_regime、亚洲盘 vs 美洲盘胜率对比；输出《投产前终极报告 V2》。

**AI 回复总结**：
- 新增：feat_hour_sin/cos、feat_day_of_week、feat_session(1=亚洲/2=欧洲/3=美洲/0=其他)、feat_vol_regime=ATR_1h/ATR_24h
- `PURIFIED_FEATURES` 扩展为 8 维
- 报告增加【6】亚洲盘 vs 美洲盘反转胜率、【7】Polymarket EV
- 报告标题改为《投产前终极报告 V2》

---

## 10. 阶段性总结与上下文压缩

**用户 Prompt**：做阶段性总结和上下文压缩；把以前所有 prompt 及回复总结写到 prompt_history.md；以后每次执行压缩之前把新增的 prompt 和回复总结追加到 prompt_history 文件里。

**AI 回复总结**：创建本 `prompt_history.md`，并约定：后续每次做上下文压缩时，将新增 prompt 与回复总结追加到本文件。

---

## 关键文件路径

| 文件 | 作用 |
|------|------|
| `methodology.md` | 原始 PRD |
| `data_loader.py` | 5m/1s 加载、bar_stats 流式聚合、Mock OI/Premium、事件触发、compute_5m_vwap_feat5 |
| `feature_engineering.py` | 5 特征 + 时区/波动率特征、target |
| `model_backtest.py` | time_split、train_lgb、print_final_evaluation_report、run_backtest_pipeline |
| `run_pipeline.py` | 主入口，--mock / --kline-5m --trades-1s |
| `data/BINANCE_BTCUSDT_5m_*.csv` | 5m K 线 |
| `data/BINANCE_BTCUSDT_1s_*.csv` | 1s 交易（约 3.9GB） |

---

## 当前状态

- 管道已跑通，支持真实 1 年数据
- 特征：feat_1, feat_2, feat_5, feat_hour_sin, feat_hour_cos, feat_day_of_week, feat_session, feat_vol_regime
- 报告：《投产前终极报告 V2》含顺势/反向阈值、feat_2 分位、亚洲盘 vs 美洲盘、Polymarket EV

---

## 11. 规则固化与 90% Context 自动执行

**Prompt 要点**：如何把「在 90% context 时自动执行 prompt_history 压缩」这条规则固化到流程里。

**回复总结**：
- 创建 `.cursor/rules/context-compression.mdc`，`alwaysApply: true`
- 触发条件：用户明确请求、自然断点、用户提及 context 满
- 执行动作：读取 prompt_history.md，将未记录的 prompt 与回复总结按格式追加
- 说明：AI 无法检测精确 90% context，需用户手动触发（如说「压缩」）

---

## 12. 执行上下文压缩

**Prompt 要点**：执行吧。

**回复总结**：按规则将本对话中 #11、#12 的 prompt 与回复总结追加到 prompt_history.md。
