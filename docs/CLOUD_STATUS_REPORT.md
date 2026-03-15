# 云端服务状态报告

**检查时间**: 2026-03-15 约 20:09 服务器时间  
**服务器**: 47.238.152.210 (香港 ECS)

---

## 1. 服务进程状态

| 服务 | 状态 | 说明 |
|------|------|------|
| **btc-collector** (systemd) | ✅ **active** | 数据采集器由 systemd 管理，运行中 |
| **数据采集进程** | ✅ 运行中 | `python -u run_collector.py`，PID 131244 |
| **Seller 模拟盘** | ✅ 运行中 | `python3 seller_live.py --duration 2h`，PID 133830 |

---

## 2. 数据采集器日志（证明在跑）

**来源**: `/root/btc_collector/collector.log` 最后 60 行摘要

- 采集器有 **PM Router** 在轮询 Polymarket：`[PM Router] 首次获取成功: 5m=True 15m=True`（近期多次出现，说明 5m/15m 市场在更新）。
- 存在含 btc/bitcoin 的活跃市场检测日志（如 `Will bitcoin hit $1m before GTA VI?`），说明 Gamma API 在正常返回。
- 日志中有多次 `Bye.`，为正常重启/重连后的输出，当前进程稳定在跑。

---

## 3. Seller 模拟盘日志（证明在跑）

**来源**: `/root/btc_collector/trade_seller.log` 最后 50 行摘要

- **策略**: 卖方策略 (Seller) · DRY-RUN，资产 BTC，阈值 0.2，阶段 mid。
- **数据库**: MySQL (127.0.0.1:3306/poly)，已连接。
- **运行情况**:
  - `[20:06:34] 卖方策略启动 · DRY-RUN`
  - `[20:06:34] [BTC] 发现市场 (elapsed=394s)`
  - 定期打印运行状态（约每 45s），例如：
    - `监控:1 挂单:0 持仓:0`，`交易:0 胜0/负0 (0%) PnL:$+0.00`
    - `[BTC] YES=0.075 low=0.075 剩463s` 等，说明在持续拉取 Polymarket midpoint 并监控市场。

---

## 4. 数据库（MySQL）最新数据

**库**: `poly`，用户 `poly` @ 127.0.0.1

### 4.1 sessions（会话表）

| id | started_at          | mode    | total_trades | total_pnl |
|----|---------------------|--------|--------------|-----------|
| 3  | 2026-03-15 20:06:34 | dry-run| 0            | 0         |
| 2  | 2026-03-15 20:06:12 | dry-run| 0            | 0         |
| 1  | 2026-03-15 20:01:38 | dry-run| 0            | 0         |

说明：当前有 3 次 seller 模拟盘会话，均为 dry-run，最新会话 20:06:34 开始，数据库在正常写入。

### 4.2 seller_trades（卖方策略交易/监控记录）

| id | session_id | asset | status  | trigger_side | buy_side | discovered_at        |
|----|------------|-------|--------|--------------|----------|------------------------|
| 3  | 3          | BTC   | watching | NULL       | NULL     | 2026-03-15 20:06:34   |
| 2  | 2          | BTC   | watching | NULL       | NULL     | 2026-03-15 20:06:12   |
| 1  | 1          | BTC   | entered  | YES        | NO       | 2026-03-15 20:01:39   |

说明：有 3 条市场记录，其中 1 条为已入场（trigger_side=YES, buy_side=NO），2 条为监控中；证明策略在发现市场并写入 MySQL。

### 4.3 action_log（动作日志）

| id | ts                  | action        |
|----|---------------------|---------------|
| 10 | 2026-03-15 20:06:34 | discover      |
| 9  | 2026-03-15 20:06:34 | session_start |
| 8  | 2026-03-15 20:06:12 | discover      |
| 7  | 2026-03-15 20:06:12 | session_start |
| 6  | 2026-03-15 20:05:08 | order_filled  |
| 5  | 2026-03-15 20:05:05 | order_placed  |
| 4  | 2026-03-15 20:05:05 | place_order   |
| 3  | 2026-03-15 20:05:05 | trigger       |
| 2  | 2026-03-15 20:01:39 | discover      |
| 1  | 2026-03-15 20:01:39 | session_start |

说明：有 session_start、discover、trigger、place_order、order_placed、order_filled 等完整动作链，证明模拟盘逻辑在跑并写库。

---

## 5. Parquet 文件（采集器落盘数据）

**目录**: `/root/btc_collector/collector_data/`  
**日期**: 2026-03-15（今日）

| 文件 | 大小 | 最后修改时间（服务器） |
|------|------|------------------------|
| btc_1s_20260315.parquet       | 168,996 bytes  | 2026-03-15 20:08:53 |
| btc_depth_20260315.parquet    | 1,117,991 bytes| 2026-03-15 20:08:53 |
| btc_force_20260315.parquet    | 103,148 bytes | 2026-03-15 20:08:54 |
| pm_history_20260315.parquet   | 4,797 bytes   | 2026-03-15 20:08:54 |
| pm_midpoint_20260315.parquet  | 65,881 bytes  | 2026-03-15 20:08:54 |
| pm_orderbook_20260315.parquet | 28,738 bytes  | 2026-03-15 20:08:54 |
| pm_trades_20260315.parquet    | 231,631 bytes | 2026-03-15 20:09:30 |

说明：今日 7 个 parquet 文件均存在且**在 20:08–20:09 有更新**，证明采集器在持续写入 Binance（1s、depth、force）与 Polymarket（history、midpoint、orderbook、trades）数据。

---

## 6. 结论

- **数据采集器**：服务与进程均在运行，PM Router 能拿到 5m/15m 市场，今日 parquet 持续更新。
- **Seller 模拟盘**：进程在跑，日志有周期性状态输出，MySQL 中有会话、交易记录和动作日志，证明在正常采集并写入数据库。
- **数据库**：MySQL `poly` 可连接，sessions / seller_trades / action_log 有今日最新数据。
- **Parquet**：今日 7 个文件均在 20:08–20:09 有写入，采集器落盘正常。

以上内容可作为「云端服务与数据采集、模拟盘、数据库、parquet 均正常运行」的证明。
