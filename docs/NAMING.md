# 项目命名规范

统一代码、数据库、界面与文档中的命名，便于协作与维护。

---

## 一、术语（中英文对应）

| 中文 | 英文（代码/DB） | 说明 |
|------|-----------------|------|
| **轮次** | session | 一次策略运行的会话，对应 DB 表 `sessions` 的一行；界面用「全部轮次」「第 N 轮次」。 |
| 市场 | market | 单个 Polymarket 二元市场，由 slug 唯一标识。 |
| 策略 | strategy | 交易策略：seller / smart_seller / model_seller。 |

**约定**：界面与文档统一用「轮次」，不再使用「批次」。

---

## 二、策略与命令

| 类型 | 命名 | 界面显示名 | 说明 |
|------|------|------------|------|
| 策略 ID | `seller` | 卖方策略 | 卖方策略（15m）。 |
| 策略 ID | `smart_seller` | 智能卖方 | 智能卖方策略（15m）。 |
| 策略 ID | `model_seller` | 模型卖方 | 回测实盘化模型策略（5m/15m）。 |
| 命令 | `dashboard` | — | 交易监控 Dashboard。 |

- **策略 ID**：小写 + 下划线（snake_case），与 `run_trade.py` 子命令、DB 表名后缀一致。
- **界面显示名**：简短中文，在 Dashboard 策略 Tab、市场记录「策略」列中使用。

---

## 三、代码与仓库

- **Python**：模块/变量/函数用 **snake_case**，类名用 **PascalCase**。
- **目录与脚本**：**snake_case**，如 `run_trade.py`、`start_dashboard.sh`、`model_seller_live.py`。
- **环境变量**：**大写下划线**，如 `MYSQL_HOST`、`MODEL_SELLER_MODEL_PATH`。

---

## 四、数据库

- **表名**：**snake_case**，复数语义用复数形式。
  - `sessions`：轮次表
  - `seller_trades` / `smart_seller_trades` / `model_seller_trades`：各策略交易/监控记录
  - `model_seller_activity`：模型策略运行状态与决策摘要
  - `action_log`：操作日志
- **列名**：**snake_case**，如 `session_id`、`market_type`、`trigger_price`。
- **model_seller_trades 按市场唯一**：`slug` 唯一约束，同一市场只存一条记录。写入时先按 `slug` 查，存在则更新为当前轮次并重置为 watching，不存在才插入，从源头杜绝重复。

---

## 五、市场 slug 与展示

- **存储**：Polymarket 约定，如 `btc-updown-5m-{epoch}`、`btc-updown-15m-{epoch}`。
- **界面展示（市场列）**：统一为 **`{asset}-{epoch} [DD HH:MM - HH:MM] 5m|15m`**
  - 由后端字段 `asset`、`epoch`、`market_type` 生成，不依赖 slug 字符串格式，避免历史或不同策略命名不一致。
  - 示例：`btc-1773591300 [16 00:15 - 00:30] 15m`、`btc-1773591300 [16 00:15 - 00:20] 5m`。

---

## 六、运行模式

| 模式 | 代码/DB | 界面 |
|------|---------|------|
| 模拟盘 | `dry-run` | DRY-RUN |
| 实盘 | `live` | LIVE |

比较时大小写不敏感（如 `mode.toLowerCase() === 'live'`）。
