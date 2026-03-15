# Model Seller 状态检查与策略逻辑说明

## 一、当前运行状态与日志

### 1. 进程状态（你本机）

- **model_seller**：已启动并在运行  
  - 命令：`python3 scripts/run_trade.py model_seller --duration 2h --market-window both`  
  - 终端输出（节选）：
    ```
    ✅ CLOB HTTP: 浏览器UA + IPv4 模式
    🧠 模型策略 (Model Seller) 🏷️ DRY-RUN
    资产: BTC  市场周期: both
    阈值: P(YES)>=0.60 买YES, P(YES)<=0.50 买NO
    模型路径: .../data/models/model_seller.pkl
    [23:28:16] 🏷DRY ℹ️ 模型策略启动 🏷️ DRY-RUN
    ┌─ [23:32:10] 模型策略 · 运行4min 监控:0 交易:0 胜0/负0 PnL:$+0.00
    ```
- **Dashboard**：已启动  
  - 命令：`python scripts/run_trade.py dashboard --port 8080`  
  - 监听 8080，页面可访问。

### 2. 为何 Dashboard 对 model_seller「没有任何反应」

- 策略日志里 **监控:0** 表示：**没有发现任何 Polymarket 市场**。
- 发现市场需要访问 Polymarket 的 Gamma API（`gamma-api.polymarket.com`）。  
  你现在是**直接本机跑 model_seller、没有走 SSH 隧道**，本地网络很可能访问不到该 API，所以：
  - `discover_markets()` 一直返回空；
  - 不会往 `model_seller_trades` 表里插入任何一行；
  - Dashboard 在「模型卖方」下只显示**在本策略表里出现过 session_id 的 sessions**。  
  因为 `model_seller_trades` 没有任何记录，所以没有 session 通过筛选，页面就表现为：选 model_seller 时没有任何轮次、没有任何反应。

结论：**不是 Dashboard 坏了，而是 model_seller 当前没连上 Polymarket，导致没有可展示的数据。**

### 3. 建议操作（让 Dashboard 有数据）

- 先在一个终端里**启动 Polymarket 隧道**（保持运行）：
  ```bash
  bash scripts/start_polymarket_tunnel.sh
  ```
- 在**另一个终端**里用隧道跑 model_seller（这样请求会走代理，能访问 Polymarket）：
  ```bash
  bash scripts/run_via_tunnel.sh -- python3 scripts/run_trade.py model_seller --duration 2h --market-window both
  ```
- 跑起来后，终端里应出现类似：`🆕 发现市场 5m (elapsed=xxx s)`，且「监控」数 > 0。  
  此时 MySQL 里会有 `model_seller_trades` 和对应 session，Dashboard 选「模型卖方」就会出现轮次与市场记录。

---

## 二、模型策略（model_seller）执行逻辑（自然语言）

下面用「遇到什么情况就做什么」的方式说明当前实现。

### 1. 市场与周期

- 只做 **Polymarket 上 BTC 的 5 分钟 / 15 分钟「涨跌」二元市场**（根据 `--market-window` 选 5m、15m 或 both）。
- 每个市场有固定时长（5m=300 秒，15m=900 秒），从 epoch 对齐的整点开始、到结束算一个市场。

### 2. 发现市场

- 每隔几秒用 Polymarket Gamma API 查当前时间附近的 5m/15m 市场（slug 形如 `btc-updown-5m-<epoch>`）。
- **只有能连上 API 时才会发现市场**；连不上就一直是「监控:0」，也不会写库，Dashboard 就无数据。
- 发现后会在 MySQL `model_seller_trades` 里插入一行（状态 watching），并开始监控该市场的 YES/NO 中间价。

### 3. 何时做一次「是否下单」的决策

- 对每个**正在监控、且尚未做过决策**的市场，在**距离该市场结束还有约 12 秒以内**时（即 `elapsed >= window - 12` 左右），**只做一次**决策：
  - 用当前 Binance K 线、Polymarket 中间价等算出一组特征（含 feat_1/2/5 等）；
  - 用 Binance 最近几根 K 线判断是否满足「事件条件」：前两根 K 线同向（连涨或连跌）且第二根更强，且从第一根开盘到第二根收盘的涨跌幅超过约 0.3%；
  - **只有事件条件满足时**，才用已加载的模型（LGBM/逻辑回归）算 **P(YES)**；
  - 再根据 P(YES) 与阈值决定：买 YES、买 NO、或不交易。

### 4. 具体执行什么操作（按情况）

- **P(YES) ≥  prob_buy_yes（默认 0.60）**  
  - **执行**：下**买 YES** 的限价单（价格约为当前 YES 中间价减一点偏移，避免追高）。  
  - **含义**：模型认为「这根 K 线收阳」概率高，押注 YES。

- **P(YES) ≤ prob_buy_no（默认 0.50）**  
  - **执行**：下**买 NO** 的限价单（价格约为当前 NO 中间价减一点偏移）。  
  - **含义**：模型认为「收阳」概率偏低，押注 NO（即收阴）。

- **prob_buy_no < P(YES) < prob_buy_yes**  
  - **执行**：本市场**不交易**；只把该市场标记为已决策（decision_done），并可在库里记一下 p_yes/特征等；若已临近结束则标记为 skipped（未触发）。

- **事件条件不满足**（例如 Binance 上不是「连涨/连跌且幅度够」）  
  - **执行**：本市场**不调用模型、不下单**；同样只做一次决策标记，避免重复计算。

### 5. 下单之后

- **挂单中（order_pending）**  
  - 每隔一段时间查一次订单是否成交。  
  - 若**成交**：状态改为已入场（entered），等市场到期结算。  
  - 若**快到市场结束（例如剩不到 20 秒）仍未成交**：取消订单，该市场标记为 skipped（订单超时）。

- **已入场（entered）**  
  - **持有到市场结束**；到期后用 Gamma API 查该市场的结算结果（YES 赢还是 NO 赢）。  
  - 根据结果算盈亏：押对的一侧按份额兑现，押错的一侧亏掉本金。  
  - 若 API 一时查不到结果，会等一段时间；超时则用当前 YES 价做强制结算（例如 YES>0.5 判 YES 赢）。

### 6. 风控与限制（当前实现）

- **max_trades**：若设成 >0，达到该笔数后不再新开仓（仍会结算已有仓位）。  
- **max_loss**：若设成 >0，累计亏损达到该金额且当前无持仓时，策略会停止新交易并退出。  
- **dry-run（模拟盘）**：不真实下单，只打日志、写库，用于看「本会怎么下单、怎么结算」。

---

## 三、小结

- **model_seller 和 Dashboard 进程都在跑**；Dashboard 对 model_seller「没反应」是因为**没有通过隧道连 Polymarket**，发现市场数为 0，库里没有 model_seller_trades，页面就无数据。
- 用 **`start_polymarket_tunnel.sh` + `run_via_tunnel.sh` 跑 model_seller** 后，一旦出现「发现市场」和「监控:1」以上，Dashboard 选「模型卖方」就会看到轮次和市场记录。
- 策略逻辑就是：**只在每个 5m/15m 市场临近结束前做一次模型判断；满足事件条件且 P(YES) 在阈值外就下对应买 YES 或买 NO 的限价单，否则不下单；下单后持有到结算，按结算结果算盈亏。**
