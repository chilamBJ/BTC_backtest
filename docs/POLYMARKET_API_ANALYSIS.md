# Polymarket BTC 5min/15min 实盘数据 — 接口分析与可采集数据清单

本文档基于 Polymarket 官方文档整理，用于确定「BTC 5 分钟 / 15 分钟」预测市场的**实盘交易与行情数据**可从哪些接口获取、具体有哪些字段，便于你确认要采集哪些数据并接入 `data_collector.py`。

---

## 一、Polymarket 与 BTC 5m/15m 市场

- **Event**：一个「主题」（如某类 BTC 预测），下面可挂多个 **Market**。
- **Market**：一个二元市场，有唯一 **conditionId**，对应 YES/NO 两个 **Token（asset_id）**。
- **BTC 5min / 15min**：通常是「某根 K 线收阳/收阴」类的二元市场，每个到期时间一个 Market，需要先通过 **Gamma API** 或 **搜索** 拿到对应 Event/Market 的 **conditionId** 和 **YES/NO 的 token_id（即 CLOB 里的 asset_id）**。

获取市场列表/详情的入口：

- **Gamma API**（推荐用于按 slug/标签查 Event 与下属 Market）  
  - Base: `https://gamma-api.polymarket.com`  
  - `GET /events/slug/{slug}` — 按 event slug 取单个 Event（含 `markets[]`）  
  - `GET /markets`、`GET /markets/slug/{slug}` — 列表/按 slug 取 Market  
  - Event 的每个 Market 含：`conditionId`、`clobTokenIds`（YES/NO 两个 token 的 ID）、`question`、`outcomes`、`outcomePrices`、`endDate` 等。
- **Data API** 的 `GET /trades` 可按 **market（condition_id）** 过滤，得到该市场下的逐笔成交。

下面按「实盘成交」与「行情/历史」两类说明接口与字段。

---

## 二、实盘交易数据（逐笔成交）

### 1. Data API — 历史/实盘成交（REST）

- **接口**：`GET https://data-api.polymarket.com/trades`
- **认证**：不需要
- **主要参数**：
  - `market`：condition ID 列表（逗号分隔），与 `eventId` 二选一
  - `eventId`：event ID 列表，与 `market` 二选一
  - `limit`：1–10000，默认 100
  - `offset`：分页
  - `takerOnly`：是否只要 taker 成交，默认 true
  - `side`：`BUY` | `SELL`

**单条成交（Trade）可获得的字段：**

| 字段 | 类型 | 说明 |
|------|------|------|
| `price` | number | 成交价（0–1 概率价） |
| `size` | number | 成交数量（张/份额） |
| `side` | string | `BUY` \| `SELL` |
| `timestamp` | integer | 时间戳（毫秒或秒，需以实际返回为准） |
| `asset` | string | 该笔成交对应的 token（asset）ID，可区分 YES/NO |
| `conditionId` | string | 市场 condition ID |
| `outcome` | string | 结果标签，如 `"Yes"` / `"No"` |
| `outcomeIndex` | integer | 结果索引（0/1 等） |
| `proxyWallet` | string | 交易者代理钱包地址 |
| `transactionHash` | string | 链上交易哈希（如有） |
| `title` | string | 市场标题 |
| `slug` | string | 市场/事件 slug |
| `eventSlug` | string | 事件 slug |
| `name` / `pseudonym` / `profileImage` 等 | — | 用户/画像信息（可选） |

**要点**：  
- 用 **conditionId**（或 eventId）可限定在「某个 BTC 5min 或 15min 市场」；  
- 每条记录有 **price、size、side、timestamp、asset、outcome**，即可得到 **YES 与 NO 各自的成交价、数量、买卖方向及时间**；  
- 实盘采集可通过**轮询**该接口（按 `market` + `limit`/`offset` 或按时间窗口）增量拉取新成交。

---

### 2. CLOB WebSocket — 实时成交（last_trade_price）

- **地址**：`wss://ws-subscriptions-clob.polymarket.com/ws/market`
- **订阅**：按 **asset_id（token_id）** 订阅，可同时订阅 YES 和 NO 两个 token。

**事件类型** `last_trade_price` 可获得的字段：

| 字段 | 说明 |
|------|------|
| `asset_id` | 对应 YES 或 NO 的 token ID |
| `price` | 最新成交价 |
| `side` | `BUY` \| `SELL` |
| `size` | 成交数量 |
| `timestamp` | 时间戳（毫秒） |
| `market` | condition ID |
| `fee_rate_bps` | 费率（bps） |

**要点**：  
- 只能拿到**每个 token 的「最后一笔」成交**，不是全量逐笔；  
- 若需**全量逐笔**，仍需以 **Data API `/trades`** 为主，WebSocket 可作为实时提醒或补最后一笔。

---

## 三、行情与历史价格（非逐笔）

### 1. CLOB REST — 订单簿（当前盘口）

- **接口**：`GET https://clob.polymarket.com/book?token_id={token_id}`
- **参数**：`token_id` = YES 或 NO 的 asset_id。

**响应示例与可获得数据：**

| 字段 | 说明 |
|------|------|
| `market` | condition ID |
| `asset_id` | 当前 token ID |
| `bids` | `[{ "price", "size" }, ...]` 买单档位 |
| `asks` | `[{ "price", "size" }, ...]` 卖单档位 |
| `timestamp` | 快照时间 |
| `tick_size` / `min_order_size` | 最小价格步长、最小下单量 |
| `last_trade_price` | 最近成交价（若有在响应里） |
| `hash` | 订单簿状态哈希 |

可采集：**YES/NO 各自的 best bid/ask、档位深度、last_trade_price（若提供）**。你当前采集器已在 5m K 线最后 10 秒轮询 orderbook，可沿用此接口。

---

### 2. CLOB REST — 历史价格序列（K 线式）

- **接口**：`GET https://clob.polymarket.com/prices-history`
- **参数**：
  - `market`：**token_id**（注意：参数名叫 market，实际传的是 asset_id）
  - `interval`：`1m` | `6h` | `1d` | `1w` | `1h` | `max` | `all`
  - `startTs` / `endTs`：时间范围（与 interval 二选一）
  - `fidelity`：精度（分钟），默认 1

**响应：**

```json
{
  "history": [
    { "t": 1234567890, "p": 0.52 },
    ...
  ]
}
```

| 字段 | 说明 |
|------|------|
| `t` | 时间戳（秒） |
| `p` | 该时段价格（一般为收盘/中间价） |

**要点**：  
- 这是**聚合后的价格序列**，不是逐笔成交；  
- 适合做 5min/15min **K 线或 mid 序列**，不能替代逐笔成交；  
- 若你要的是「每笔成交的成交价」，应以 **Data API `/trades`** 为准。

---

### 3. CLOB REST — 最后成交价（单点）

- **接口**：`GET https://clob.polymarket.com/price?token_id=...&side=BUY|SELL`（或 last-trade 端点，以文档为准）
- **文档中还有**：`getLastTradePrice(token_id)` → 返回最近一笔的 **price**、**side**。

可获得：**YES/NO 各自的「最后成交价 + 方向」**，适合做「当前价」快照。

---

## 四、可采集数据汇总（供你勾选）

下面按「数据类别」和「接口」列出**你实际可拿到**的字段，便于你确认要采哪些。

### A. 逐笔成交（实盘交易）

- **来源**：Data API `GET /trades`（主）+ 可选 WebSocket `last_trade_price`（补最后一笔/实时提醒）。
- **可按 market（conditionId）过滤**，只采「BTC 5min」或「BTC 15min」对应市场。

| 数据项 | 字段名 | 说明 |
|--------|--------|------|
| 成交时间 | `timestamp` | 建议统一为秒或毫秒存库 |
| 成交价 | `price` | 0–1 概率价 |
| 成交量 | `size` | 张数/份额 |
| 方向 | `side` | BUY / SELL |
| YES/NO 标识 | `outcome` 或 `asset` | 用 asset 可精确对应到 token（YES/NO） |
| 市场 | `conditionId` | 对应哪个 5m/15m 市场 |
| 市场/事件标识 | `slug`, `eventSlug`, `title` | 便于人工区分 |
| 交易者 | `proxyWallet` | 可选 |
| 链上交易哈希 | `transactionHash` | 可选 |

**建议至少采集**：`timestamp`, `price`, `size`, `side`, `outcome`（或 `asset`）, `conditionId`，即可还原 YES/NO 的逐笔成交价与方向。

---

### B. 订单簿快照（当前盘口）

- **来源**：`GET https://clob.polymarket.com/book?token_id={token_id}`（YES 和 NO 各请求一次）。

| 数据项 | 说明 |
|--------|------|
| 时间 | 请求时间或响应内 `timestamp` |
| token_id / 市场 | `asset_id`, `market` |
| 买盘 | `bids[]`：price, size |
| 卖盘 | `asks[]`：price, size |
| 最近成交价 | 若响应含 `last_trade_price` |
| tick_size / min_order_size | 可选 |

你可选：**只采 Top N 档（如各 10 档）** 或整本，按你现有「每整秒/每 5m 末 10 秒」策略即可。

---

### C. 历史价格序列（K 线/mid 序列）

- **来源**：CLOB `GET /prices-history?market={token_id}&interval=...&fidelity=...`。

| 数据项 | 说明 |
|--------|------|
| 时间 | `t`（秒） |
| 价格 | `p` |

适合做 5min/15min 的 **mid 或 close 序列**，不做逐笔时可用。

---

### D. 最后成交价（单点）

- **来源**：CLOB `getLastTradePrice` 或 `/price` + side。
- 可获得：**YES/NO 各自的最后成交价、side**，适合「当前价」快照。

---

## 五、BTC 5min / 15min 市场 ID 的获取方式

1. **已知 event slug**  
   - 调用 `GET https://gamma-api.polymarket.com/events/slug/{slug}`，从返回的 `markets[]` 里取每个 market 的 `conditionId` 和 `clobTokenIds`（YES/NO 两个 token_id）。

2. **搜索/列表**  
   - 使用 `GET https://gamma-api.polymarket.com/markets` 或搜索接口，用关键词（如 "Bitcoin", "BTC", "5 min", "15 min"）或标签过滤，得到对应 Market 的 `conditionId` 和 token IDs。

3. **配置到采集器**  
   - 在 `data_collector.py` 中配置：  
     - `POLYMARKET_BTC_5MIN_CONDITION_IDS` / `POLYMARKET_BTC_15MIN_CONDITION_IDS`（或按 event 配置），以及  
     - 每个 market 的 YES/NO `token_id`，用于：  
       - Data API `market=` 过滤成交；  
       - CLOB book / prices-history / last-trade 按 token 请求。

---

## 六、你可确认的采集清单（请勾选）

请直接回复要采哪些，我按你的选择设计队列与落盘表结构并写进 `data_collector.py`。

**实盘交易（逐笔）**  
- [ ] **Data API `/trades`**：`timestamp`, `price`, `size`, `side`, `outcome`（或 `asset`）, `conditionId`  
- [ ] 是否还要：`slug`, `eventSlug`, `title`, `proxyWallet`, `transactionHash`？

**订单簿**  
- [ ] 当前已有：5m K 线最后 10 秒轮询 orderbook（YES/NO 的 bid/ask 深度）  
- [ ] 是否额外增加：**按 5min/15min 为周期**的 orderbook 快照（例如每 5min 整点采一次）？

**历史价格序列**  
- [ ] CLOB `prices-history`：按 5min/15min 的 `interval`+`fidelity` 拉取 YES/NO 的 `(t, p)` 序列？

**实时最后一笔**  
- [ ] WebSocket `last_trade_price`：只补「实时最后一笔」或仅做告警，不落库？

**市场范围**  
- [ ] 只采 **BTC 5min** 对应市场（需你提供 condition_id 或 event slug）  
- [ ] 只采 **BTC 15min** 对应市场  
- [ ] 两者都采，并在落盘时用 `market_type`（5m/15m）区分  

你确认上述勾选后，我会在 `data_collector.py` 里增加对应 Producer + 队列 + Parquet 表结构（含 YES/NO 成交价及其他你选的字段），并说明如何配置 5min/15min 的 condition_id 与 token_id。
