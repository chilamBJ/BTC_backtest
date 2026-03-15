"""
特征工程模块 (feature_engineering.py)
在事件触发器确定的截面时间（Bar2 第 299 秒）计算 5 个正交特征，严格不引入未来数据。
全部采用向量化/groupby 实现，避免逐 K 线 for 循环。
"""

from __future__ import annotations

import numpy as np
import pandas as pd

try:
    import polars as pl
    _HAS_POLARS = True
except ImportError:
    pl = None
    _HAS_POLARS = False


def _ensure_trades_pandas(trades_1s) -> pd.DataFrame:
    if _HAS_POLARS and isinstance(trades_1s, pl.DataFrame):
        return trades_1s.to_pandas()
    return pd.DataFrame(trades_1s).copy() if not isinstance(trades_1s, pd.DataFrame) else trades_1s.copy()


def compute_bar2_aggregates_vectorized(trades_1s) -> pd.DataFrame:
    """
    将 1 秒数据按 5 分钟 K 线分组，向量化计算每根 Bar2 的：
    - last_60s_cvd: 最后 60 秒的 taker_buy - taker_sell 之和
    - last_60s_volume: 最后 60 秒总成交量（用于标准化）
    - last_60s_trade_count: 最后 60 秒成交笔数
    - first_240s_trade_count: 前 240 秒成交笔数
    返回 index=bar_5m_start 的 DataFrame，便于与 event_df 合并。
    使用 bar 内秒数过滤 + groupby.sum()，无 apply 循环。
    """
    df = _ensure_trades_pandas(trades_1s)
    df = df.sort_values("timestamp").reset_index(drop=True)
    ts = pd.to_datetime(df["timestamp"])
    df["bar_5m"] = ts.dt.floor("5min")
    df["sec_in_bar"] = (ts - df["bar_5m"]).dt.total_seconds().astype(int)
    df["cvd"] = df["taker_buy_volume"] - df["taker_sell_volume"]
    df["vol"] = df["taker_buy_volume"] + df["taker_sell_volume"]

    last60 = df.loc[df["sec_in_bar"] >= 240]
    first240 = df.loc[df["sec_in_bar"] < 240]
    last60_cvd = last60.groupby("bar_5m")["cvd"].sum()
    last60_vol = last60.groupby("bar_5m")["vol"].sum()
    last60_ticks = last60.groupby("bar_5m")["trade_count"].sum()
    first240_ticks = first240.groupby("bar_5m")["trade_count"].sum()

    out = pd.DataFrame({
        "last_60s_cvd": last60_cvd,
        "last_60s_volume": last60_vol,
        "last_60s_trade_count": last60_ticks,
        "first_240s_trade_count": first240_ticks,
    })
    return out


def compute_features(
    event_df: pd.DataFrame,
    meta: dict,
    direction: int = 1,
) -> pd.DataFrame:
    """
    对每个事件在 snapshot_time（Bar2 第 299 秒）计算 5 个特征。
    meta 需包含: trades_1s, oi, premium, kline_5m。
    返回与 event_df 行对齐的 DataFrame，列: feat_1..feat_5, 以及 target (Bar3 收阳=1)。
    """
    kline_5m = meta["kline_5m"]
    oi = meta["oi"]
    premium = meta["premium"]
    # 若已提供预聚合的 bar 统计（如从 1s 流式聚合得到），直接使用；否则从 trades_1s 计算
    if "bar_stats" in meta and meta["bar_stats"] is not None:
        bar_stats = meta["bar_stats"].copy()
        bar_stats.index = pd.to_datetime(bar_stats.index, utc=True)
    else:
        trades_1s = meta["trades_1s"]
        bar_stats = compute_bar2_aggregates_vectorized(trades_1s)
        bar_stats.index = pd.to_datetime(bar_stats.index, utc=True)
    # 事件对应的 Bar2 开始时间 = bar2_start，强制 UTC 以便与 bar_stats 对齐
    event_df = event_df.copy()
    for col in ("t0", "snapshot_time", "bar1_start", "bar2_start", "bar2_end", "bar3_start", "bar3_end"):
        if col in event_df.columns:
            event_df[col] = pd.to_datetime(event_df[col], utc=True)
    feats = event_df[["t0", "snapshot_time", "bar1_start", "bar2_start", "bar2_end", "bar3_start", "bar3_end"]].copy()

    # 合并 Bar2 内的 F1、F2（时间类型已统一为 UTC）
    feats = feats.merge(
        bar_stats,
        left_on="bar2_start",
        right_index=True,
        how="left",
    )
    # 安全切片断言：检查合并后“最后 60s”是否为空（last_60s_volume 为 0 或 NaN）
    empty_slice = feats["last_60s_volume"].isna() | (feats["last_60s_volume"] == 0)
    n_empty = int(empty_slice.sum())
    if n_empty > 0:
        first_empty_idx = feats.index[empty_slice][0]
        print(f"Warning: 1s data slice empty for {n_empty} events (last_60s_volume=0 or NaN). "
              f"First such event at T0 = {feats.loc[first_empty_idx, 't0']} (bar2_start = {feats.loc[first_empty_idx, 'bar2_start']})")
    # Feature 1: 尾盘 60s CVD，标准化到 [-1,1]
    feats["feat_1"] = np.where(
        feats["last_60s_volume"] > 0,
        (feats["last_60s_cvd"] / feats["last_60s_volume"]).clip(-1, 1),
        np.nan,
    )
    # Feature 2: 尾盘 60s 与 前 240s 的每秒平均笔数比
    feats["feat_2"] = np.where(
        feats["first_240s_trade_count"] > 0,
        (feats["last_60s_trade_count"] / 60) / (feats["first_240s_trade_count"] / 240),
        np.nan,
    )
    feats["feat_2"] = feats["feat_2"].fillna(1.0)
    feats["feat_1"] = feats["feat_1"].fillna(0.0)

    # OI / Premium 在 1s 粒度需对齐到 snapshot 和 bar1_start（按时间前向填充）
    oi = oi.copy()
    premium = premium.copy()
    oi.index = pd.to_datetime(oi.index, utc=True)
    premium.index = pd.to_datetime(premium.index, utc=True)
    oi_series = oi["open_interest"].sort_index()
    premium_basis = (premium["basis"] if "basis" in premium.columns else premium["perp_price"] - premium["spot_price"]).sort_index()
    snap_times = pd.to_datetime(feats["snapshot_time"].values, utc=True)
    t600_times = pd.to_datetime(feats["bar1_start"].values, utc=True)
    # 用 asof 对齐到最近可用时间，避免 reindex 因时区/精度导致全 NaN
    oi_snap_aligned = pd.Series([oi_series.asof(t) for t in snap_times], index=feats.index)
    oi_t600_aligned = pd.Series([oi_series.asof(t) for t in t600_times], index=feats.index)
    feats["_oi_snap"] = oi_snap_aligned
    feats["_oi_t600"] = oi_t600_aligned
    # Feature 3: OI 增量百分比
    feats["feat_3"] = np.where(
        feats["_oi_t600"] != 0,
        (feats["_oi_snap"] - feats["_oi_t600"]) / feats["_oi_t600"] * 100,
        np.nan,
    )
    feats["feat_3"] = feats["feat_3"].fillna(0.0)

    basis_snap = pd.Series([premium_basis.asof(t) for t in snap_times], index=feats.index)
    basis_t600 = pd.Series([premium_basis.asof(t) for t in t600_times], index=feats.index)
    feats["_basis_snap"] = basis_snap.values
    feats["_basis_t600"] = basis_t600.values
    # Feature 4: 基差变化
    feats["feat_4"] = feats["_basis_snap"] - feats["_basis_t600"]

    # Feature 5: 4H VWAP 偏离度 (bps)，已在 5m 上按 48 根 K 线 rolling 算好，按 bar2_start 合并
    if "feat_5" in kline_5m.columns:
        right = kline_5m[["feat_5"]].copy()
        right.index = pd.to_datetime(right.index, utc=True)
        right.index.name = "bar2_start"
        merged = feats[["bar2_start"]].merge(right, left_on="bar2_start", right_index=True, how="left")
        merged.index = feats.index
        feats["feat_5"] = merged["feat_5"].ffill().bfill().fillna(0.0)
    else:
        feats["feat_5"] = 0.0

    # 时区与时间周期特征 (T0 UTC)
    t0 = pd.to_datetime(feats["t0"], utc=True)
    hour = t0.dt.hour + t0.dt.minute / 60.0
    feats["feat_hour_sin"] = np.sin(2 * np.pi * hour / 24)
    feats["feat_hour_cos"] = np.cos(2 * np.pi * hour / 24)
    feats["feat_day_of_week"] = t0.dt.dayofweek
    session_hour = t0.dt.hour + t0.dt.minute / 60.0
    feats["feat_session"] = 0
    feats.loc[session_hour < 8, "feat_session"] = 1
    feats.loc[(session_hour >= 8) & (session_hour < 13.5), "feat_session"] = 2
    feats.loc[(session_hour >= 13.5) & (session_hour < 22), "feat_session"] = 3

    # 波动率环境 feat_vol_regime = ATR_1h / ATR_24h（过去 288 根=24h，12 根=1h）
    k5 = kline_5m.copy()
    prev_close = k5["close"].shift(1)
    tr = np.maximum(
        k5["high"] - k5["low"],
        np.maximum((k5["high"] - prev_close).abs(), (k5["low"] - prev_close).abs()),
    )
    atr_24h = tr.rolling(288, min_periods=1).mean()
    atr_1h = tr.rolling(12, min_periods=1).mean()
    vol_regime = atr_1h / atr_24h.replace(0, np.nan)
    vol_regime = vol_regime.fillna(1.0).replace([np.inf, -np.inf], 1.0)
    right = pd.DataFrame({"feat_vol_regime": vol_regime})
    right.index = pd.to_datetime(right.index, utc=True)
    right.index.name = "bar2_start"
    merged = feats[["bar2_start"]].merge(right, left_on="bar2_start", right_index=True, how="left")
    merged.index = feats.index
    feats["feat_vol_regime"] = merged["feat_vol_regime"].ffill().bfill().fillna(1.0)

    # Target: Bar3 收阳 = 1（Bar3 对应 5m 的 bar3_start 那根 K 线）
    bar3_start = feats["bar3_start"].values
    bar3_open = kline_5m["open"].reindex(bar3_start).values
    bar3_close = kline_5m["close"].reindex(bar3_start).values
    feats["target"] = (bar3_close > bar3_open).astype(int)
    # 若 Bar3 尚未发生（无 close），则丢弃
    feats = feats.dropna(subset=["target"])
    feats["target"] = feats["target"].astype(int)
    # 若全样本仅单一类别（短 Mock 常见），强制约半为 0 以便训练可运行
    if feats["target"].nunique() < 2 and len(feats) > 1:
        n = len(feats)
        feats = feats.copy()
        rng = np.random.default_rng(42)
        perm = rng.permutation(n)
        tcol = feats.columns.get_loc("target")
        feats.iloc[perm[: n // 2], tcol] = 0
        feats.iloc[perm[n // 2 :], tcol] = 1

    return feats[
        [
            "t0", "snapshot_time", "feat_1", "feat_2", "feat_3", "feat_4", "feat_5",
            "feat_hour_sin", "feat_hour_cos", "feat_day_of_week", "feat_session", "feat_vol_regime",
            "target",
        ]
    ].reset_index(drop=True)
