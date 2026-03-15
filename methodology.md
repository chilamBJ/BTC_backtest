


这份文档按照**专业量化研究产品需求文档（PRD）**的标准编写，逻辑严密、定义清晰，你可以直接将此文档复制发送给你的 Coding Agent（如 GPT-4, Claude 或 Cursor），它能够根据这份指南直接写出高质量的 Python 数据处理、特征工程和机器学习回测框架代码。

---

# 量化回测系统需求文档：BTC 5分钟连续趋势二元预测模型 (Polymarket 定制)

## 1. 项目概述 (Project Overview)
*   **业务背景**：本策略服务于 Polymarket 上的 BTC 5 分钟涨跌二元期权交易。交易规则为每 5 分钟 K 线收盘结算，若收盘价 > 开盘价，则 "UP" 合约结算为 1，否则为 0。
*   **策略核心假设**：在 5 分钟级别出现连续两根同向 K 线（如连涨两根）时，通过引入 1 秒级微观订单流（Order Flow）和宏观衍生品因子（Open Interest等），可以在第二根 K 线结束前的最后 1 秒（第 299 秒），有效判断微观动能是否耗尽，从而高胜率地预测第三根 K 线的方向（是否继续收阳）。
*   **任务目标**：编写一套完整的 Python 回测管道（Pipeline），使用过去一年的历史数据验证该假设。计算特征因子，训练二元分类模型（如 LightGBM / XGBoost 或 逻辑回归），并输出基于不同概率阈值的期望收益（EV）和胜率评估报告。

## 2. 数据源要求 (Data Requirements)
Agent 需要能够处理并合并以下三种时间精度的数据对齐（以 Binance 或预言机指定交易所数据为准）：

1.  **5 分钟 K 线数据 (5m OHLCV)**：用于定位宏观趋势和基础环境。
2.  **1 秒级交易数据 (1s Trades/OHLCV)**：必须包含 `taker_buy_volume` 和 `taker_sell_volume` 以及 `trade_count`（成交笔数），用于计算 CVD 和微观动能。
3.  **衍生品高频数据 (Derivatives Data)**：
    *   合约总持仓量 (Open Interest) 历史数据（1 分钟或 5 分钟级别）。
    *   现货与永续合约价格对齐数据（用于计算 Premium）。
    *   *(可选)* L2 盘口快照数据 (Top of Book Bid/Ask Volume)。如果获取困难，可先在 V1 版本中剔除此特征。

## 3. 事件触发器定义 (Event Trigger Definition)
我们要解决的是**条件概率**问题，因此不需要对全天候的时间序列进行预测，只需提取特定的**事件样本库 (Event Pool)**。

以**做多预测（预测第三根收阳）**为例。Agent 需遍历 5 分钟 K 线，找到满足以下条件的时刻点 $T_0$：
*   **Bar 1**：$T_{-10m}$ 到 $T_{-5m}$，收盘价 > 开盘价（实体 > 0）。
*   **Bar 2**：$T_{-5m}$ 到 $T_{0}$，收盘价 > 开盘价，且 Bar 2 的收盘价 > Bar 1 的收盘价。
*   **预测窗口 Bar 3**：$T_{0}$ 到 $T_{+5m}$。
*   **特征截面时间 (Snapshot Time)**：$T_0$ 减去 1 秒（即 Bar 2 的第 299 秒）。所有特征必须严格在此时刻及之前计算，绝不可引入未来数据（Look-ahead Bias）。

*(注：做空预测逻辑完全对称，请 Agent 编写时通过参数 `direction=1` 或 `-1` 兼容)*

## 4. 特征工程构建 (Feature Engineering - The "X")
Agent 需在每个触发事件的第 299 秒，计算并提取以下 5 个正交特征向量：

### 特征 1：尾盘微观买卖量失衡 (Last-Minute CVD Slope)
*   **逻辑**：Bar 2 最后 60 秒是否有真实的市价买盘净流入。
*   **计算公式**：在 Bar 2 的 `[第 240 秒, 第 299 秒]` 窗口内：
    $$Feature\_1 = \sum_{t=240}^{299} (Taker\_Buy\_Volume_t - Taker\_Sell\_Volume_t)$$
*   **标准化**：可除以该 60 秒内的总成交量以生成标准化比率 `[-1, 1]`。

### 特征 2：微观交易密集度突变率 (Tick Intensity Surge)
*   **逻辑**：验证临近收盘时，交易动作是加速（趋势延续）还是萎缩（动能衰退）。
*   **计算公式**：
    $$Feature\_2 = \frac{Mean\_Trade\_Count\_per\_sec(last\_60s)}{Mean\_Trade\_Count\_per\_sec(first\_240s)}$$
*   若 $Feature\_2 > 1$，说明尾盘交易加速。

### 特征 3：持仓量增量动力 (Open Interest Delta - $\Delta OI$)
*   **逻辑**：连涨是由于“新多头建仓”还是“空头止损逼空”。
*   **计算公式**：对比 Bar 2 第 299 秒的全局 OI 与 Bar 1 第 0 秒的全局 OI。
    $$Feature\_3 = \frac{OI_{T\_299} - OI_{T\_-600}}{OI_{T\_-600}} \times 100\%$$

### 特征 4：高频期现溢价斜率 (Spot-Perp Premium Dynamics)
*   **逻辑**：现货主导的上涨持续性更强。
*   **计算公式**：计算这 10 分钟内的基差 $Basis = Perp\_Price - Spot\_Price$。
    $$Feature\_4 = Basis_{T\_299} - Basis_{T\_-600}$$
*   *(若基差缩小或为负，说明现货涨幅 > 合约，连涨势能更健康)*

### 特征 5：宏观 VWAP 偏离度 (Distance to HTF VWAP)
*   **逻辑**：过滤逆势的假突破。
*   **计算公式**：以过去 4 小时 (4H) 为窗口计算成交量加权平均价 $VWAP_{4H}$。
    $$Feature\_5 = \frac{Close_{T\_299} - VWAP_{4H}}{VWAP_{4H}} \times 10000 \text{ (以 bps 计)}$$

## 5. 目标变量定义 (Target Variable - The "Y")
严格的二元分类标签：
*   **目标时间段**：Bar 3 ($T_0$ 到 $T_{+5m}$)。
*   **结算规则**：
    *   $Y = 1$：如果 $Close_{Bar3} > Open_{Bar3}$
    *   $Y = 0$：如果 $Close_{Bar3} \le Open_{Bar3}$

## 6. 模型训练与回测架构设计 (Modeling & Validation Architecture)
请 Agent 使用 `scikit-learn` 和 `LightGBM/XGBoost` 搭建如下回测管道：

1.  **数据划分 (Data Splitting)**：
    *   **严禁随机洗牌 (No Shuffle)**。必须使用基于时间序列的滚动交叉验证（Time-Series Walk-Forward Validation，例如 `TimeSeriesSplit`），或严格按照时间线：前 8 个月 Train，后 4 个月 Test。
2.  **模型选择 (Model)**：
    *   采用 `LGBMClassifier`，因其对缺失值友好且支持非线性特征交互。
    *   设置 `objective='binary'`, `metric='binary_logloss'`。
3.  **概率输出 (Probability Calibration)**：
    *   模型必须输出概率 $P(Y=1)$，而不仅仅是 0 或 1 的硬分类。这对于二元期权交易的期望值 (EV) 计算至关重要。

## 7. 评估指标与回测报告输出要求 (Evaluation & Output Reports)
单纯的 Accuracy（准确率）在量化中意义不大，Agent 需在代码运行后输出以下特定的业务指标：

1.  **基础分类指标**：测试集上的 AUC-ROC, Precision, Recall。
2.  **置信度阈值分析 (Threshold Analysis)**：
    *   按预测概率 $P$ 分桶（例如：$P > 0.55$, $P > 0.60$, $P > 0.65$）。
    *   计算在不同概率阈值下的**真实胜率 (Win Rate)** 和 **触发次数 (Trade Count)**。
    *   *示例期望输出*：`当模型预测概率 P > 60% 时，样本外共触发 145 次，实际胜率为 63.4%。`
3.  **特征重要性 (Feature Importance)**：
    *   输出 SHAP Values 或 LightGBM 自带的特征重要性图表。我们需要明确看到：CVD 和 OI 数据是否比传统技术指标提供了更多的信息熵（Information Gain）？
4.  **基准对比 (Baseline Comparison)**：
    *   对比基准系统：不使用模型，只要发生连涨两根，无脑买入第三根（即计算全样本中连涨后继续涨的基础概率）。验证我们的特征是否带来了**显著的超额胜率 (Alpha)**。

## 8. 给 Coding Agent 的特定代码规范要求 (Instructions for Dev Agent)
*   **模块化代码**：将代码分为 `data_loader.py` (数据合并与对齐), `feature_engineering.py` (因子计算), `model_backtest.py` (训练与评估) 三个模块。
*   **向量化计算**：计算 `last_60s_CVD` 等 1 秒级高频特征时，必须使用 Pandas 的向量化操作（如 `.rolling()`, `.groupby()`, `.shift()`），严禁使用低效的 `for` 循环遍历 K 线。
*   **内存管理**：1 秒级数据一年可能达到数千万行，建议使用 `polars` 或分块读写（Chunking）处理数据，避免 OOM（内存溢出）。
*   **提供 Mock Data 选项**：在完整数据难以立即获取的情况下，请提供一个生成 1 周模拟高频数据（Mock Data）的函数，以便于我先跑通整个代码逻辑。

---

*（直接将上方虚线内的内容复制给任何强力大模型或编程 Agent，它将准确理解你的业务痛点，并为你生成企业级的回测系统代码。）*