"""
数据加载与对齐模块 (data_loader.py)
负责 5 分钟 K 线、1 秒级交易、衍生品数据的加载、合并与时间对齐。
支持 Mock 数据生成以便在无真实数据时跑通管道。
"""

from __future__ import annotations

import io
from pathlib import Path
from typing import Optional

import pandas as pd
import numpy as np

try:
    import polars as pl
except ImportError:
    pl = None


# ---------------------------------------------------------------------------
# 数据接口约定（供 feature_engineering 使用）
# ---------------------------------------------------------------------------
# - kline_5m: index=datetime, columns: open, high, low, close, volume
# - trades_1s: 每行为一秒聚合，columns: timestamp, taker_buy_volume, taker_sell_volume, trade_count, [volume]
# - oi: index=datetime, columns: open_interest (或 value)
# - premium: index=datetime, columns: spot_price, perp_price, basis = perp - spot
# ---------------------------------------------------------------------------


def load_kline_5m(path: str | Path) -> pd.DataFrame:
    """加载 5 分钟 K 线。支持 CSV：timestamp/open_time/time_key, open, high, low, close, volume。"""
    path = Path(path)
    if not path.exists():
        raise FileNotFoundError(f"5m K线文件不存在: {path}")
    df = pd.read_csv(path)
    # 统一时间列名（含 Binance time_key）
    time_col = next(
        (c for c in df.columns if str(c).lower() in ("timestamp", "open_time", "datetime", "time_key", "time")),
        df.columns[0],
    )
    df = df.rename(columns={time_col: "timestamp"})
    df["timestamp"] = pd.to_datetime(df["timestamp"], utc=True)
    df = df.set_index("timestamp").sort_index()
    # 列名小写
    df.columns = [str(c).lower() for c in df.columns]
    return df


def compute_5m_vwap_feat5(kline_5m: pd.DataFrame, window_bars: int = 48) -> pd.DataFrame:
    """
    在原始 5m OHLCV 上用严格行数窗口计算 4H VWAP 与 feat_5（截面时间前不可用未来数据）。
    4 小时 = 48 根 5 分钟 K 线。在提取事件之前调用，结果供事件表直接合并。
    """
    df = kline_5m.copy()
    df["typical_price"] = (df["high"] + df["low"] + df["close"]) / 3.0
    df["pv"] = df["typical_price"] * df["volume"]
    df["vwap_4h"] = (
        df["pv"].rolling(window=window_bars, min_periods=1).sum()
        / df["volume"].rolling(window=window_bars, min_periods=1).sum()
    )
    df["feat_5"] = np.where(
        df["vwap_4h"] > 0,
        (df["close"] - df["vwap_4h"]) / df["vwap_4h"] * 10000,
        np.nan,
    )
    df["feat_5"] = df["feat_5"].fillna(0.0)
    return df


def load_trades_1s(path: str | Path, chunksize: Optional[int] = 500_000):
    """
    加载 1 秒级聚合交易数据。列需包含：timestamp, taker_buy_volume, taker_sell_volume, trade_count。
    若已安装 polars 则返回 LazyFrame；否则返回 pandas DataFrame。
    """
    path = Path(path)
    if not path.exists():
        raise FileNotFoundError(f"1s 交易文件不存在: {path}")
    if pl is not None:
        lf = pl.scan_csv(path, try_parse_dates=True)
        col_map = {c: c.lower().replace(" ", "_") for c in lf.collect_schema().names()}
        ts_col = next((k for k in col_map if "time" in k or "timestamp" in k), None)
        if ts_col:
            col_map[ts_col] = "timestamp"
        lf = lf.rename(col_map)
        return lf
    df = pd.read_csv(path)
    df.columns = [str(c).lower().replace(" ", "_") for c in df.columns]
    ts_col = next((c for c in df.columns if "time" in c or "timestamp" in c), df.columns[0])
    df = df.rename(columns={ts_col: "timestamp"})
    df["timestamp"] = pd.to_datetime(df["timestamp"])
    return df


def load_oi(path: str | Path) -> pd.DataFrame:
    """加载持仓量。期望列：timestamp/datetime, open_interest 或 value。"""
    path = Path(path)
    if not path.exists():
        raise FileNotFoundError(f"OI 文件不存在: {path}")
    df = pd.read_csv(path)
    time_col = next((c for c in df.columns if str(c).lower() in ("timestamp", "open_time", "datetime", "time")), df.columns[0])
    df = df.rename(columns={time_col: "timestamp"})
    df["timestamp"] = pd.to_datetime(df["timestamp"])
    value_col = next((c for c in df.columns if "interest" in str(c).lower() or c == "value"), df.columns[-1])
    df = df.rename(columns={value_col: "open_interest"})
    df = df[["timestamp", "open_interest"]].set_index("timestamp").sort_index()
    return df


def create_dummy_oi_premium_from_5m(kline_5m: pd.DataFrame, seed: int = 42) -> tuple[pd.DataFrame, pd.DataFrame]:
    """
    无 OI/Premium 文件时，用 5m 时间戳生成带方差的 Mock 数据，保证 feat_3/feat_4 有变异、树可分裂。
    OI: 随机游走变化率约 ±0.5%；basis: 小幅随机溢价。
    """
    rng = np.random.default_rng(seed)
    idx = kline_5m.index
    n = len(idx)
    # OI 带随机趋势（-2%～2% 量级变化率），避免 feat_3 恒为 0
    oi_delta = rng.normal(loc=0.0, scale=0.005, size=n)
    oi_value = 1e6 * np.cumprod(1 + oi_delta)
    oi_value = np.maximum(oi_value, 5e5)
    oi = pd.DataFrame({"open_interest": oi_value}, index=idx.copy())
    oi.index.name = "timestamp"
    close = kline_5m["close"].values
    # 基差带方差，避免 feat_4 恒为 0
    mock_premium_slope = rng.normal(loc=0.0, scale=0.001, size=n)
    basis = close * mock_premium_slope
    premium = pd.DataFrame({
        "spot_price": close,
        "perp_price": close + basis,
        "basis": basis,
    }, index=idx.copy())
    premium.index.name = "timestamp"
    return oi, premium


def load_bar_stats_from_1s_binance(path: str | Path) -> pd.DataFrame:
    """
    从 Binance 格式 1s K 线 CSV 流式聚合出每根 5m bar 的 last_60s / first_240s 统计，
    避免将整份 3GB+ 文件载入内存。需安装 polars。
    """
    path = Path(path)
    if not path.exists():
        raise FileNotFoundError(f"1s 文件不存在: {path}")
    if pl is None:
        raise ImportError("流式处理 1s 大文件需要安装 polars: pip install polars")
    # Binance 列: code, time_key, open, high, low, close, volume, close_time, quote_asset_volume, num_trades, taker_buy_base_asset_volume, ...
    lf = pl.scan_csv(path, try_parse_dates=True)
    # 统一到我们需要的列名（Binance 原列名）
    lf = lf.rename({
        "time_key": "timestamp",
        "taker_buy_base_asset_volume": "taker_buy_volume",
        "num_trades": "trade_count",
    })
    lf = lf.with_columns(
        pl.col("timestamp").cast(pl.Datetime("us")).dt.epoch("s").alias("_epoch"),
        (pl.col("volume") - pl.col("taker_buy_volume")).alias("taker_sell_volume"),
    )
    # bar_5m: 5 分钟 bar 起点；Polars Datetime("us") 需微秒，故乘 1e6
    lf = lf.with_columns(
        ((pl.col("_epoch") // 300 * 300) * 1_000_000).cast(pl.Datetime("us")).alias("bar_5m"),
        (pl.col("_epoch") % 300).alias("sec_in_bar"),
        (pl.col("taker_buy_volume") - pl.col("taker_sell_volume")).alias("cvd"),
        (pl.col("taker_buy_volume") + pl.col("taker_sell_volume")).alias("vol"),
    )
    last60 = lf.filter(pl.col("sec_in_bar") >= 240).group_by("bar_5m").agg(
        pl.col("cvd").sum().alias("last_60s_cvd"),
        pl.col("vol").sum().alias("last_60s_volume"),
        pl.col("trade_count").sum().alias("last_60s_trade_count"),
    )
    first240 = lf.filter(pl.col("sec_in_bar") < 240).group_by("bar_5m").agg(
        pl.col("trade_count").sum().alias("first_240s_trade_count"),
    )
    out = last60.join(first240, on="bar_5m", how="left")
    df = out.collect().to_pandas()
    df = df.set_index("bar_5m")
    df.index = pd.to_datetime(df.index, utc=True)
    df["first_240s_trade_count"] = df["first_240s_trade_count"].fillna(0)
    return df


def load_premium(path: str | Path) -> pd.DataFrame:
    """加载期现数据。期望列：timestamp, spot_price, perp_price；或 spot, perp。"""
    path = Path(path)
    if not path.exists():
        raise FileNotFoundError(f"Premium 文件不存在: {path}")
    df = pd.read_csv(path)
    time_col = next((c for c in df.columns if str(c).lower() in ("timestamp", "datetime", "time")), df.columns[0])
    df = df.rename(columns={time_col: "timestamp"})
    df["timestamp"] = pd.to_datetime(df["timestamp"])
    cols = [c for c in df.columns if str(c).lower() != "timestamp"]
    spot_c = next((c for c in cols if "spot" in str(c).lower()), cols[0])
    perp_c = next((c for c in cols if "perp" in str(c).lower() or "future" in str(c).lower()), cols[1] if len(cols) > 1 else cols[0])
    df = df.rename(columns={spot_c: "spot_price", perp_c: "perp_price"})
    df["basis"] = df["perp_price"] - df["spot_price"]
    df = df[["timestamp", "spot_price", "perp_price", "basis"]].set_index("timestamp").sort_index()
    return df


def align_oi_to_seconds(oi_1m_or_5m: pd.DataFrame, index_1s: pd.DatetimeIndex) -> pd.Series:
    """将 1m/5m OI 前向填充到每一秒时间戳。"""
    oi = oi_1m_or_5m["open_interest"]
    return oi.reindex(index_1s, method="ffill")


def align_premium_to_seconds(premium_df: pd.DataFrame, index_1s: pd.DatetimeIndex) -> pd.DataFrame:
    """将 premium 数据前向填充到每一秒。"""
    return premium_df.reindex(index_1s, method="ffill")


def generate_mock_data_1week(
    seed: int = 42,
    start: Optional[str] = None,
) -> tuple[pd.DataFrame, pl.DataFrame, pd.DataFrame, pd.DataFrame]:
    """
    生成约 1～4 周的模拟数据，便于在无真实数据时跑通管道。
    返回: (kline_5m, trades_1s, oi, premium)
    """
    rng = np.random.default_rng(seed)
    if start is None:
        start = "2024-01-01 00:00:00"
    base = pd.Timestamp(start)
    # 约 1 周 5m K 线，便于快速跑通
    n_5m = 2100
    idx_5m = pd.date_range(base, periods=n_5m, freq="5min")

    # 5m OHLCV：随机游走
    log_ret = rng.standard_normal(n_5m) * 0.002
    close = 43000 * np.exp(np.cumsum(log_ret))
    open_ = np.roll(close, 1)
    open_[0] = 43000
    # 约一半 K 线强制收阴，使 Bar3 的 target 分布均衡
    down_bar = rng.choice([True, False], size=n_5m)
    close = np.where(down_bar, open_ - rng.uniform(1, 50, n_5m), close)
    high = np.maximum(close + rng.uniform(0, 30, n_5m), open_)
    low = np.minimum(close - rng.uniform(0, 30, n_5m), open_)
    volume = rng.uniform(100, 1000, n_5m)
    kline_5m = pd.DataFrame(
        {"open": open_, "high": high, "low": low, "close": close, "volume": volume},
        index=idx_5m,
    )
    kline_5m.index.name = "timestamp"

    # 1s 数据：每根 5m 有 300 秒
    rows = []
    for i in range(n_5m):
        t0 = idx_5m[i]
        for s in range(300):
            ts = t0 + pd.Timedelta(seconds=s)
            tb = rng.uniform(10, 100)
            tsell = rng.uniform(10, 100)
            rows.append({
                "timestamp": ts,
                "taker_buy_volume": tb,
                "taker_sell_volume": tsell,
                "trade_count": int(rng.integers(5, 50)),
                "volume": tb + tsell,
            })
    if pl is not None:
        trades_1s = pl.DataFrame(rows)
    else:
        trades_1s = pd.DataFrame(rows)

    # OI：1 分钟粒度，前向填充到秒
    n_1m = n_5m * 5
    idx_1m = pd.date_range(base, periods=n_1m, freq="1min")
    oi_value = 1e6 + np.cumsum(rng.standard_normal(n_1m) * 100)
    oi_value = np.maximum(oi_value, 5e5)
    oi = pd.DataFrame({"open_interest": oi_value}, index=idx_1m)
    oi.index.name = "timestamp"

    # Premium：与 5m 对齐即可，后面会按秒填充
    basis = rng.standard_normal(n_5m) * 5
    premium = pd.DataFrame({
        "spot_price": close,
        "perp_price": close + basis,
        "basis": basis,
    }, index=idx_5m)
    premium.index.name = "timestamp"

    return kline_5m, trades_1s, oi, premium


def build_event_pool_and_1s_slices(
    kline_5m: pd.DataFrame,
    trades_1s=None,
    oi: Optional[pd.DataFrame] = None,
    premium: Optional[pd.DataFrame] = None,
    bar_stats: Optional[pd.DataFrame] = None,
    direction: int = 1,
) -> tuple[pd.DataFrame, dict]:
    """
    根据 methodology 定义的事件触发器，从 5m K 线中筛选事件点 T0，
    并返回每个事件对应的 Bar1/Bar2/Bar3 时间范围及 1s 数据切片索引信息，供特征计算使用。
    trades_1s 与 bar_stats 二选一：有 1s 大文件时传 bar_stats（流式聚合结果），Mock 时传 trades_1s。
    direction: 1=做多(连涨预测第三根收阳), -1=做空(连跌预测第三根收阴)
    """
    if trades_1s is not None and pl is not None and hasattr(trades_1s, "collect"):
        trades_1s = trades_1s.collect()
    df = kline_5m.copy()
    df["bar1_up"] = df["close"] > df["open"]
    df["bar2_up"] = df["close"] > df["open"]
    df["prev_close"] = df["close"].shift(1)
    df["bar2_higher"] = (df["close"] > df["prev_close"]) if direction == 1 else (df["close"] < df["prev_close"])
    if direction == -1:
        df["bar1_up"] = df["close"] < df["open"]
        df["bar2_up"] = df["close"] < df["open"]
    # Bar1 开盘价、Bar2 收盘价（用于动量阈值过滤）
    df["bar1_open"] = df["open"].shift(2)
    df["bar2_close"] = df["close"].shift(1)
    momentum_pct = (df["bar2_close"] - df["bar1_open"]) / df["bar1_open"].replace(0, np.nan)
    momentum_ok = momentum_pct.abs() > 0.003
    # 触发器：Bar1 与 Bar2 同向、Bar2 更强，且前两根 K 线累计涨跌幅 |Δ| > 0.3%
    trigger = df["bar1_up"] & df["bar2_up"] & df["bar2_higher"] & momentum_ok
    trigger = trigger & df["prev_close"].notna() & df["bar1_open"].notna()
    event_times = df.index[trigger].tolist()
    events = []
    for t0 in event_times:
        t0 = pd.Timestamp(t0)
        bar1_start = t0 - pd.Timedelta(minutes=10)
        bar1_end = t0 - pd.Timedelta(minutes=5)
        bar2_start = t0 - pd.Timedelta(minutes=5)
        bar2_end = t0
        bar3_start = t0
        bar3_end = t0 + pd.Timedelta(minutes=5)
        snapshot_time = t0 - pd.Timedelta(seconds=1)  # Bar2 第 299 秒
        events.append({
            "t0": t0,
            "bar1_start": bar1_start, "bar1_end": bar1_end,
            "bar2_start": bar2_start, "bar2_end": bar2_end,
            "bar3_start": bar3_start, "bar3_end": bar3_end,
            "snapshot_time": snapshot_time,
        })
    event_df = pd.DataFrame(events)
    if oi is None or premium is None:
        oi, premium = create_dummy_oi_premium_from_5m(kline_5m)
    meta = {
        "trades_1s": trades_1s,
        "bar_stats": bar_stats,
        "oi": oi,
        "premium": premium,
        "kline_5m": kline_5m,
    }
    return event_df, meta


if __name__ == "__main__":
    # 快速测试：生成 Mock 数据并跑一遍事件检测
    k5, t1, oi, prem = generate_mock_data_1week()
    event_df, meta = build_event_pool_and_1s_slices(k5, t1, oi, prem, direction=1)
    print("Mock 1 week - 5m 根数:", len(k5), "事件数:", len(event_df))
    if len(event_df) > 0:
        print("示例事件 t0:", event_df["t0"].iloc[0], "snapshot:", event_df["snapshot_time"].iloc[0])
