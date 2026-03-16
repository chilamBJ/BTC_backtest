"""
连续趋势概率分析模块 (streak_analysis.py)

核心目标
--------
给定一个"已发现 Bar1 + Bar2 同向"的事件样本库（即 features_df），
找出在哪些因子值区间的组合下，第三根 K 线延续同向的概率 P > 50%。

分析方法
--------
1. **单因子分位数分析**：将每个因子按等频分位数分成 N 个桶，
   分别计算每桶中的：样本量、P(Y=1) 胜率、与全样本基准的差值（Alpha）。

2. **双因子交叉矩阵**：对最重要的两个因子做 2D 条件概率矩阵，
   展示哪个象限（低/中/高）的组合胜率最高。

3. **多因子组合穷举**：对所有因子组合（支持 2～3 个因子）进行分位数分桶，
   找出满足 P > target_prob 且样本量 >= min_samples 的所有组合，
   按胜率降序输出 Top-N。同时输出每个因子的精确数值阈值（bin_thresholds），
   可直接用于实盘过滤条件。

4. **走势滚动验证（Walk-Forward Validation）**：将数据按时间切分成 N 个折叠，
   用全量数据确定的分位数阈值在各时间段内单独测试胜率，评估组合的跨时稳健性。

5. **交易过滤器导出（Trading Filters Export）**：将高胜率组合转为可直接执行的
   IF 条件规则，导出为人类可读格式及可选的 JSON 文件。

6. **LGBM 决策路径分析**：使用 LGBM 训练后的叶节点规则，
   输出自动挖掘到的高概率叶节点的因子阈值条件。

因子说明
--------
V1 核心因子（原有）：
  feat_1  尾盘 60s CVD 比率，[-1,1]        — 微观买卖量失衡
  feat_2  尾盘交易密集度突变率              — Tick Intensity Surge
  feat_3  持仓量增量 %                      — OI Delta（⚠ 无真实 OI 时为占位）
  feat_4  期现溢价变化                      — Basis Change（⚠ 无真实数据时为占位）
  feat_5  4H VWAP 偏离度（bps）             — Distance to HTF VWAP

V2 扩展因子（新增）：
  feat_rsi_14   RSI(14) on 5m close         — 动量超买超卖状态
  feat_vol_ratio  Bar2 volume / 20-bar avg  — 成交量确信度
  feat_body_str   Bar2 candle body ratio    — K 线形态强度
  feat_mom_accel  Bar2_ret - Bar1_ret       — 动量加速度

推荐使用场景
-----------
  from backtest.streak_analysis import print_streak_analysis_report
  print_streak_analysis_report(features_df)
"""

from __future__ import annotations

import ast
import itertools
import warnings
from typing import Optional

import numpy as np
import pandas as pd

try:
    import lightgbm as lgb
    _HAS_LGB = True
except ImportError:
    lgb = None
    _HAS_LGB = False

warnings.filterwarnings("ignore", category=UserWarning)

# 全量因子集（含 V2 扩展）
ALL_FEATURE_COLS = [
    "feat_1", "feat_2", "feat_3", "feat_4", "feat_5",
    "feat_rsi_14", "feat_vol_ratio", "feat_body_str", "feat_mom_accel",
]

# V1 纯血因子（无扩展）
V1_FEATURE_COLS = ["feat_1", "feat_2", "feat_5"]

# 因子中文描述（用于报告）
FEATURE_DESC = {
    "feat_1":        "尾盘CVD比率 (Last-Min CVD Ratio)",
    "feat_2":        "交易密集度突变率 (Tick Intensity Surge)",
    "feat_3":        "持仓量增量 % (OI Delta %)",
    "feat_4":        "期现溢价变化 (Basis Change)",
    "feat_5":        "4H VWAP偏离度bps (VWAP Deviation)",
    "feat_rsi_14":   "RSI-14 on 5m (Momentum State)",
    "feat_vol_ratio":"成交量确信度 (Volume Conviction Ratio)",
    "feat_body_str": "K线实体强度 (Candle Body Strength)",
    "feat_mom_accel":"动量加速度 (Momentum Acceleration)",
}

BIN_LABELS_3 = ["低 (Low)", "中 (Mid)", "高 (High)"]
BIN_LABELS_5 = ["极低 (VLow)", "低 (Low)", "中 (Mid)", "高 (High)", "极高 (VHigh)"]


# ---------------------------------------------------------------------------
# 1. 单因子条件概率分析
# ---------------------------------------------------------------------------

def factor_conditional_prob(
    features_df: pd.DataFrame,
    factor: str,
    n_bins: int = 5,
    target_col: str = "target",
    min_samples: int = 10,
) -> pd.DataFrame:
    """
    将 factor 按等频分位数分成 n_bins 桶，计算每桶：
      - count: 样本数
      - win_rate: P(Y=1)
      - alpha: win_rate - 全样本基准胜率
      - bin_range: 因子数值区间

    返回排好序（按 bin 序号）的 DataFrame。
    """
    if factor not in features_df.columns:
        return pd.DataFrame()
    df = features_df[[factor, target_col]].dropna()
    if len(df) < n_bins * min_samples:
        n_bins = max(2, len(df) // min_samples)
    base_wr = float(df[target_col].mean())

    try:
        bins, edges = pd.qcut(df[factor], q=n_bins, retbins=True, duplicates="drop")
    except ValueError:
        return pd.DataFrame()

    df = df.copy()
    df["_bin"] = bins
    grouped = df.groupby("_bin", observed=True)[target_col].agg(["count", "mean"])
    grouped.columns = ["count", "win_rate"]
    grouped["alpha"] = grouped["win_rate"] - base_wr
    grouped["bin_label"] = [f"分位 {i+1}/{len(grouped)}" for i in range(len(grouped))]
    grouped["bin_range"] = [str(b) for b in grouped.index]
    grouped = grouped.reset_index(drop=True)
    return grouped


# ---------------------------------------------------------------------------
# 2. 双因子交叉概率矩阵
# ---------------------------------------------------------------------------

def factor_pair_prob_matrix(
    features_df: pd.DataFrame,
    factor_a: str,
    factor_b: str,
    n_bins: int = 3,
    target_col: str = "target",
    min_samples: int = 10,
) -> tuple[pd.DataFrame, pd.DataFrame]:
    """
    对 factor_a（行）× factor_b（列）做 2D 条件概率矩阵。
    返回 (prob_matrix, count_matrix)，行/列标签为低/中/高等分位区间。
    """
    if factor_a not in features_df.columns or factor_b not in features_df.columns:
        return pd.DataFrame(), pd.DataFrame()
    df = features_df[[factor_a, factor_b, target_col]].dropna()
    if len(df) < 4:
        return pd.DataFrame(), pd.DataFrame()

    try:
        df = df.copy()
        df["_bin_a"] = pd.qcut(df[factor_a], q=n_bins, labels=BIN_LABELS_3[:n_bins], duplicates="drop")
        df["_bin_b"] = pd.qcut(df[factor_b], q=n_bins, labels=BIN_LABELS_3[:n_bins], duplicates="drop")
    except ValueError:
        return pd.DataFrame(), pd.DataFrame()

    pivot_mean = df.pivot_table(
        index="_bin_a", columns="_bin_b", values=target_col, aggfunc="mean", observed=True
    )
    pivot_count = df.pivot_table(
        index="_bin_a", columns="_bin_b", values=target_col, aggfunc="count", observed=True
    )
    # 将样本量不足的格子置为 NaN（避免误导）
    pivot_mean[pivot_count < min_samples] = np.nan
    pivot_mean.index.name = f"{factor_a} →"
    pivot_mean.columns.name = f"{factor_b} ↓"
    pivot_count.index.name = f"{factor_a} →"
    pivot_count.columns.name = f"{factor_b} ↓"
    return pivot_mean, pivot_count


# ---------------------------------------------------------------------------
# 3. 多因子组合穷举（条件概率 > 阈值）
# ---------------------------------------------------------------------------

def find_combinations_above_threshold(
    features_df: pd.DataFrame,
    factor_cols: Optional[list[str]] = None,
    n_bins: int = 3,
    min_samples: int = 30,
    target_prob: float = 0.55,
    max_combo_size: int = 3,
    target_col: str = "target",
) -> pd.DataFrame:
    """
    对 factor_cols 中所有因子按 n_bins 等频分桶，
    枚举所有 1～max_combo_size 因子组合的桶组合，
    找出满足 P(Y=1) > target_prob 且样本量 >= min_samples 的所有条件。

    返回 DataFrame，列：
      factors        — 因子名称（逗号分隔）
      bin_key        — 可读的桶区间描述
      bin_thresholds — dict[str, (float, float)]，每个因子的 (下界, 上界)，可直接用于实盘过滤
      combo_size     — 组合因子数
      count          — 该桶的样本数
      win_rate       — P(Y=1) 胜率
      alpha          — win_rate - 全样本基准胜率
    按 win_rate 降序排列。
    """
    if factor_cols is None:
        factor_cols = [c for c in ALL_FEATURE_COLS if c in features_df.columns]

    df = features_df[factor_cols + [target_col]].dropna().copy()
    base_wr = float(df[target_col].mean())

    # 预先分桶，保存 pd.Interval 类型便于提取阈值
    bin_cat_map: dict[str, pd.Series] = {}   # factor -> Categorical[Interval] series
    bin_str_map: dict[str, pd.Series] = {}   # factor -> str series (for readable key)
    for col in factor_cols:
        try:
            binned, _ = pd.qcut(df[col], q=n_bins, retbins=True, duplicates="drop")
            bin_cat_map[col] = binned
            bin_str_map[col] = binned.astype(str)
        except ValueError:
            pass

    valid_cols = list(bin_cat_map.keys())
    results = []

    for size in range(1, max_combo_size + 1):
        for combo in itertools.combinations(valid_cols, size):
            # Build a temporary DataFrame with the categorical bins for this combo
            tmp = df[[target_col]].copy()
            for col in combo:
                tmp[f"_b_{col}"] = bin_cat_map[col]
            bin_cols = [f"_b_{col}" for col in combo]
            grouped = tmp.groupby(bin_cols, observed=True)[target_col].agg(["count", "mean"])
            grouped.columns = ["count", "win_rate"]
            mask = (grouped["win_rate"] > target_prob) & (grouped["count"] >= min_samples)
            for group_idx, row in grouped[mask].iterrows():
                # group_idx is a tuple of pd.Interval (or single Interval if size=1)
                intervals = (group_idx,) if size == 1 else group_idx
                # Extract actual numeric thresholds
                thresholds: dict[str, tuple[float, float]] = {}
                parts: list[str] = []
                for i, col in enumerate(combo):
                    iv = intervals[i]
                    lo, hi = float(iv.left), float(iv.right)
                    thresholds[col] = (lo, hi)
                    parts.append(f"{col}: ({lo:.4f}, {hi:.4f}]")
                bin_key_str = " & ".join(parts)
                results.append({
                    "factors": ", ".join(combo),
                    "bin_key": bin_key_str,
                    "bin_thresholds": thresholds,
                    "combo_size": size,
                    "count": int(row["count"]),
                    "win_rate": round(float(row["win_rate"]), 4),
                    "alpha": round(float(row["win_rate"]) - base_wr, 4),
                })

    if not results:
        return pd.DataFrame(columns=[
            "factors", "bin_key", "bin_thresholds", "combo_size", "count", "win_rate", "alpha"
        ])
    out = pd.DataFrame(results).sort_values("win_rate", ascending=False).reset_index(drop=True)
    return out


# ---------------------------------------------------------------------------
# 4. Walk-Forward Validation for Top Combos（时间滚动稳健性验证）
# ---------------------------------------------------------------------------

def combo_walkforward_validation(
    features_df: pd.DataFrame,
    top_combos: pd.DataFrame,
    n_folds: int = 4,
    target_col: str = "target",
    time_col: str = "t0",
    target_prob: float = 0.55,
    top_k: int = 10,
    min_robust_fold_ratio: float = 0.5,
) -> pd.DataFrame:
    """
    对 top_combos 中的高胜率组合做时间分层稳健性验证。

    原理
    ----
    用全量数据学到的分位数阈值（存储在 bin_thresholds 列），
    在按时间等分的 n_folds 个时段内分别计算实际胜率，
    评估该组合是否在各个时间段都稳定有效，还是只在某段数据中过拟合。

    参数
    ----
    top_combos           : find_combinations_above_threshold() 的输出（需含 bin_thresholds 列）
    n_folds              : 时间切片数量（建议 3~5）
    top_k                : 仅对前 k 个组合做验证（节省计算）
    target_prob          : 稳健性判断阈值（各折胜率 > 此值才算该折通过）
    min_robust_fold_ratio: 通过折数占有效折数的最低比例（默认 0.5 = 超过半数通过则视为稳健）

    返回
    ----
    DataFrame，列：
      factors, bin_key, full_win_rate, mean_fold_wr, std_fold_wr,
      n_folds_above_target, is_robust, fold_details
    """
    if "bin_thresholds" not in top_combos.columns:
        warnings.warn("top_combos 缺少 bin_thresholds 列，请先调用新版 find_combinations_above_threshold()")
        return pd.DataFrame()

    if time_col not in features_df.columns:
        # fallback to row-order split
        features_df = features_df.copy()
        features_df[time_col] = pd.RangeIndex(len(features_df))

    # Sort by time and split into folds
    df_sorted = features_df.sort_values(time_col).reset_index(drop=True)
    fold_size = len(df_sorted) // n_folds
    if fold_size < 5:
        warnings.warn(f"数据量不足（每折仅 {fold_size} 行），走势验证可能不可靠。")

    fold_boundaries = [i * fold_size for i in range(n_folds)] + [len(df_sorted)]
    folds = [df_sorted.iloc[fold_boundaries[i]:fold_boundaries[i + 1]] for i in range(n_folds)]

    results = []
    for _, combo_row in top_combos.head(top_k).iterrows():
        thresholds: dict = combo_row["bin_thresholds"]
        if not thresholds:
            continue

        fold_win_rates = []
        fold_counts = []
        for fold_df in folds:
            # Apply threshold filters to this fold
            mask = pd.Series([True] * len(fold_df), index=fold_df.index)
            for col, (lo, hi) in thresholds.items():
                if col not in fold_df.columns:
                    mask[:] = False
                    break
                mask = mask & (fold_df[col] > lo) & (fold_df[col] <= hi)
            filtered = fold_df.loc[mask, target_col].dropna()
            if len(filtered) >= 5:
                fold_win_rates.append(float(filtered.mean()))
                fold_counts.append(len(filtered))
            else:
                fold_win_rates.append(np.nan)
                fold_counts.append(0)

        valid_fold_wrs = [w for w in fold_win_rates if not np.isnan(w)]
        mean_wr = float(np.mean(valid_fold_wrs)) if valid_fold_wrs else np.nan
        std_wr = float(np.std(valid_fold_wrs)) if len(valid_fold_wrs) > 1 else np.nan
        n_above = sum(1 for w in valid_fold_wrs if w > target_prob)
        # A combo is robust if it beats target_prob in at least min_robust_fold_ratio of valid folds
        min_folds_needed = max(1, int(np.ceil(len(valid_fold_wrs) * min_robust_fold_ratio)))
        is_robust = (n_above >= min_folds_needed) and not np.isnan(mean_wr)
        fold_details = "; ".join(
            f"折{i+1}(n={fold_counts[i]})={fold_win_rates[i]:.2%}" if not np.isnan(fold_win_rates[i])
            else f"折{i+1}(n=0)=N/A"
            for i in range(n_folds)
        )
        results.append({
            "factors": combo_row["factors"],
            "bin_key": combo_row["bin_key"],
            "full_win_rate": combo_row["win_rate"],
            "full_count": combo_row["count"],
            "mean_fold_wr": round(mean_wr, 4) if not np.isnan(mean_wr) else np.nan,
            "std_fold_wr": round(std_wr, 4) if not np.isnan(std_wr) else np.nan,
            "n_folds_above_target": n_above,
            "n_valid_folds": len(valid_fold_wrs),
            "is_robust": is_robust,
            "fold_details": fold_details,
        })

    if not results:
        return pd.DataFrame()
    result_df = pd.DataFrame(results)
    # Sort: robust first, then by mean_fold_wr desc
    result_df = result_df.sort_values(
        ["is_robust", "mean_fold_wr"], ascending=[False, False]
    ).reset_index(drop=True)
    return result_df


# ---------------------------------------------------------------------------
# 5. 交易过滤器导出（Trading Filters Export）
# ---------------------------------------------------------------------------

def export_trading_filters(
    top_combos: pd.DataFrame,
    target_prob: float = 0.55,
    top_k: int = 10,
    save_path: Optional[str] = None,
) -> pd.DataFrame:
    """
    将高胜率因子组合转换为可直接使用的 IF 条件规则，
    并可选导出到 JSON 文件。

    返回 DataFrame，列：
      rank, factors, conditions_human, conditions_dict, expected_win_rate, sample_count, alpha

    conditions_human 示例：
      "feat_1 > -0.05 AND feat_1 <= 0.02 AND feat_2 > 0.95 AND feat_2 <= 1.04"

    conditions_dict 可直接用于代码中的过滤：
      {"feat_1": (-0.05, 0.02), "feat_2": (0.95, 1.04)}
    """
    if "bin_thresholds" not in top_combos.columns:
        warnings.warn("top_combos 缺少 bin_thresholds 列，请使用新版 find_combinations_above_threshold()")
        return pd.DataFrame()

    rows = []
    for rank, (_, combo_row) in enumerate(top_combos.head(top_k).iterrows(), start=1):
        thresholds: dict = combo_row["bin_thresholds"]
        conditions_parts = []
        for col, (lo, hi) in thresholds.items():
            desc = FEATURE_DESC.get(col, col)
            conditions_parts.append(f"{col} > {lo:.6f} AND {col} <= {hi:.6f}  # {desc}")
        conditions_human = " AND \n    ".join(conditions_parts)
        rows.append({
            "rank": rank,
            "factors": combo_row["factors"],
            "expected_win_rate": combo_row["win_rate"],
            "alpha": combo_row["alpha"],
            "sample_count": combo_row["count"],
            "combo_size": combo_row.get("combo_size", len(thresholds)),
            "conditions_human": conditions_human,
            "conditions_dict": thresholds,
        })

    if not rows:
        return pd.DataFrame()

    filters_df = pd.DataFrame(rows)

    if save_path:
        import json
        # Build a clean serializable dict
        output = {
            "generated_at": pd.Timestamp.utcnow().isoformat(),
            "target_prob_threshold": target_prob,
            "filters": [
                {
                    "rank": r["rank"],
                    "factors": r["factors"],
                    "expected_win_rate": r["expected_win_rate"],
                    "alpha": r["alpha"],
                    "sample_count": r["sample_count"],
                    "conditions": {col: {"gt": lo, "lte": hi} for col, (lo, hi) in r["conditions_dict"].items()},
                    "conditions_human": r["conditions_human"],
                }
                for r in rows
            ],
        }
        try:
            with open(save_path, "w", encoding="utf-8") as f:
                json.dump(output, f, ensure_ascii=False, indent=2)
            print(f"  [✓] 交易过滤器已保存到: {save_path}")
        except OSError as e:
            warnings.warn(f"保存过滤器文件失败: {e}")

    return filters_df


# ---------------------------------------------------------------------------
# 6. 高概率叶节点分析（LGBM 决策路径）
# ---------------------------------------------------------------------------

def lgbm_leaf_analysis(
    features_df: pd.DataFrame,
    factor_cols: Optional[list[str]] = None,
    target_col: str = "target",
    top_n: int = 10,
    min_samples: int = 30,
    target_prob: float = 0.55,
) -> pd.DataFrame:
    """
    训练一棵浅层 LGBM（max_depth=3），从每棵树的叶节点中
    找出样本量 >= min_samples 且平均预测概率 > target_prob 的叶节点，
    输出对应的因子条件范围。

    若未安装 lightgbm，返回空 DataFrame。
    """
    if not _HAS_LGB:
        return pd.DataFrame()
    if factor_cols is None:
        factor_cols = [c for c in ALL_FEATURE_COLS if c in features_df.columns]

    df = features_df[factor_cols + [target_col]].dropna()
    if df[target_col].nunique() < 2 or len(df) < 50:
        return pd.DataFrame()

    X = df[factor_cols]
    y = df[target_col]
    params = {
        "objective": "binary",
        "metric": "binary_logloss",
        "verbosity": -1,
        "n_estimators": 50,
        "learning_rate": 0.05,
        "num_leaves": 8,
        "max_depth": 3,
        "min_child_samples": min_samples,
        "subsample": 0.8,
        "random_state": 42,
        "class_weight": "balanced",
    }
    try:
        model = lgb.LGBMClassifier(**params)
        model.fit(X, y)
        leaf_preds = model.predict_proba(X)[:, 1]

        # 从叶节点 ID 分析高概率叶节点
        leaf_ids = model.predict(X, pred_leaf=True)  # shape: (n_samples, n_trees)
        n_trees = leaf_ids.shape[1]

        rows = []
        for tree_idx in range(n_trees):
            tree_leaves = leaf_ids[:, tree_idx]
            unique_leaves = np.unique(tree_leaves)
            for leaf_id in unique_leaves:
                mask = tree_leaves == leaf_id
                count = int(mask.sum())
                if count < min_samples:
                    continue
                leaf_prob = float(leaf_preds[mask].mean())
                actual_wr = float(y.values[mask].mean())
                if actual_wr > target_prob:
                    # 找出该叶节点各因子的数值范围
                    sub = X.values[mask]
                    ranges = {col: (float(sub[:, i].min()), float(sub[:, i].max()))
                              for i, col in enumerate(factor_cols)}
                    rows.append({
                        "tree_idx": tree_idx,
                        "leaf_id": leaf_id,
                        "count": count,
                        "pred_prob_avg": round(leaf_prob, 4),
                        "actual_win_rate": round(actual_wr, 4),
                        "factor_ranges": str(ranges),
                    })

        if not rows:
            return pd.DataFrame()
        result = pd.DataFrame(rows).sort_values("actual_win_rate", ascending=False)
        return result.head(top_n).reset_index(drop=True)
    except Exception as e:
        warnings.warn(f"LGBM 叶节点分析失败: {e}")
        return pd.DataFrame()


# ---------------------------------------------------------------------------
# 7. 综合报告打印
# ---------------------------------------------------------------------------

def print_streak_analysis_report(
    features_df: pd.DataFrame,
    factor_cols: Optional[list[str]] = None,
    n_bins: int = 3,
    min_samples: int = 20,
    target_prob: float = 0.55,
    top_n_combos: int = 20,
    pair_factors: Optional[tuple[str, str]] = None,
    n_walk_folds: int = 4,
    show_walk_forward: bool = True,
    show_trading_filters: bool = True,
    save_filters_path: Optional[str] = None,
    mock_oi: bool = False,
    mock_premium: bool = False,
) -> None:
    """
    打印完整的连续趋势概率分析报告。包含：
      【0】因子合理性评估与建议（含数据质量警告）
      【1】样本基础信息（事件数、基准胜率）
      【2】各因子单独的分位数条件概率分析
      【3】最优双因子交叉矩阵
      【4】满足 P > target_prob 的多因子组合排行（含实际阈值）
      【5】LGBM 高概率叶节点解读（若安装 lightgbm）
      【6】Walk-Forward 时间分层稳健性验证（Top 组合）
      【7】交易过滤器导出（可直接用于策略过滤条件）

    参数
    ----
    features_df         : compute_features() 输出的 DataFrame（含 target）
    factor_cols         : 分析的因子列名；默认自动检测所有已知因子
    n_bins              : 每个因子的分位数桶数（3 = 低/中/高）
    min_samples         : 每个桶的最小样本量阈值
    target_prob         : 目标胜率阈值（默认 0.55 = 超过 55%）
    top_n_combos        : 输出 Top-N 因子组合
    pair_factors        : 双因子分析指定的 (factor_a, factor_b)；若 None 则自动选前两个
    n_walk_folds        : Walk-Forward 时间切片数量
    show_walk_forward   : 是否展示 Walk-Forward 验证结果
    show_trading_filters: 是否展示交易过滤器导出
    save_filters_path   : 若非 None，将过滤条件保存到此 JSON 文件路径
    mock_oi             : 若 True，提示 feat_3 使用了占位 OI 数据
    mock_premium        : 若 True，提示 feat_4 使用了占位 Premium 数据
    """
    if factor_cols is None:
        factor_cols = [c for c in ALL_FEATURE_COLS if c in features_df.columns]

    available = [c for c in factor_cols if c in features_df.columns]
    if not available:
        print("未找到任何可用因子列，请检查 features_df。")
        return

    base_wr = float(features_df["target"].mean())
    total_n = len(features_df)

    SEP = "=" * 70

    # ── 【0】因子合理性评估 ───────────────────────────────────────────────
    print("\n" + SEP)
    print("  连续 K 线趋势概率分析报告 (Streak Probability Analysis Report)")
    print(SEP)
    print()
    print("【0】因子合理性评估与建议")
    print(SEP)
    _print_factor_evaluation(mock_oi=mock_oi, mock_premium=mock_premium)

    # ── 【1】样本基础信息 ──────────────────────────────────────────────────
    print("\n【1】样本基础信息")
    print(SEP)
    print(f"  • 总事件样本数 (连涨/连跌后 Bar2 结束时)          : {total_n}")
    print(f"  • 基准胜率 (Baseline Win Rate, 无脑顺势买第三根)  : {base_wr:.2%}")
    print(f"  • 分析目标阈值 (Target Prob)                      : {target_prob:.0%}")
    print(f"  • 分析因子数量                                     : {len(available)}")
    print(f"  • 分析因子列表: {available}")
    if mock_oi or mock_premium:
        print()
        print("  ⚠ 数据质量提示:")
        if mock_oi:
            print("    feat_3 (OI Delta): 使用占位数据，信号可能不反映真实持仓变化。")
            print("    → 建议提供真实 OI CSV 以获得可靠的 feat_3 信号。")
        if mock_premium:
            print("    feat_4 (Basis Change): 使用占位数据，信号可能不反映真实期现溢价。")
            print("    → 建议提供真实 Premium CSV 以获得可靠的 feat_4 信号。")
    print()
    print("  [因子缺失值统计]")
    has_missing = False
    for col in available:
        na = int(features_df[col].isna().sum())
        if na > 0:
            print(f"    {col}: {na} 个缺失值 ({na/total_n:.1%})")
            has_missing = True
    if not has_missing:
        print("    (无缺失值)")
    print()

    # ── 【2】单因子条件概率 ───────────────────────────────────────────────
    print("【2】单因子分位数条件概率分析 (Single-Factor Conditional Probability)")
    print(SEP)
    for col in available:
        desc = FEATURE_DESC.get(col, col)
        mock_flag = " [⚠占位数据]" if (col == "feat_3" and mock_oi) or (col == "feat_4" and mock_premium) else ""
        print(f"\n  ▶ {col} — {desc}{mock_flag}")
        cond_df = factor_conditional_prob(features_df, col, n_bins=n_bins, min_samples=min_samples)
        if cond_df.empty:
            print("    (样本量不足，跳过)")
            continue
        for _, row in cond_df.iterrows():
            alpha_sign = "↑" if row["alpha"] > 0.01 else ("↓" if row["alpha"] < -0.01 else "≈")
            wr_str = f"{row['win_rate']:.2%}"
            alpha_str = f"{row['alpha']:+.2%}"
            print(f"    [{row['bin_label']}] n={int(row['count']):>5}  "
                  f"P(Y=1)={wr_str}  Alpha={alpha_str} {alpha_sign}  "
                  f"区间: {row['bin_range']}")
    print()

    # ── 【3】双因子交叉矩阵 ──────────────────────────────────────────────
    print("【3】双因子交叉概率矩阵 (Two-Factor Cross Probability Matrix)")
    print(SEP)
    if pair_factors is None:
        # 自动选择：feat_1 + feat_2（微观 CVD + Tick 强度，是 V1 核心组合）
        default_pairs = [("feat_1", "feat_2"), ("feat_1", "feat_rsi_14"),
                         ("feat_2", "feat_vol_ratio"), ("feat_5", "feat_rsi_14")]
        pairs_to_show = [(a, b) for a, b in default_pairs if a in available and b in available][:2]
    else:
        pairs_to_show = [pair_factors] if (pair_factors[0] in available and pair_factors[1] in available) else []

    if not pairs_to_show:
        pairs_to_show = list(itertools.combinations(available[:3], 2))[:2]

    for fA, fB in pairs_to_show:
        print(f"\n  ▶ {fA} (行) × {fB} (列)")
        print(f"    {FEATURE_DESC.get(fA, fA)} × {FEATURE_DESC.get(fB, fB)}")
        prob_mat, count_mat = factor_pair_prob_matrix(
            features_df, fA, fB, n_bins=n_bins, min_samples=min_samples
        )
        if prob_mat.empty:
            print("    (样本量不足，跳过)")
            continue
        print("\n    --- 胜率矩阵 P(Y=1) ---")
        with pd.option_context("display.float_format", "{:.2%}".format, "display.max_columns", 10):
            for row_label in prob_mat.index:
                row_strs = []
                for col_label in prob_mat.columns:
                    val = prob_mat.loc[row_label, col_label]
                    cnt = count_mat.loc[row_label, col_label] if (row_label in count_mat.index and col_label in count_mat.columns) else 0
                    if pd.isna(val):
                        row_strs.append(f"    {'N/A':>12} (n<{min_samples})")
                    else:
                        marker = " ★" if val > target_prob else "  "
                        row_strs.append(f"    {val:.2%}(n={int(cnt):>4}){marker}")
                print(f"    行[{row_label}] |" + "|".join(row_strs))
    print()

    # ── 【4】多因子组合穷举 ───────────────────────────────────────────────
    print("【4】多因子组合穷举 — P > {:.0%} 的因子值区间组合（含精确阈值）".format(target_prob))
    print(SEP)
    combos = find_combinations_above_threshold(
        features_df,
        factor_cols=available,
        n_bins=n_bins,
        min_samples=min_samples,
        target_prob=target_prob,
        max_combo_size=3,
    )
    if combos.empty:
        print(f"  未找到满足 P > {target_prob:.0%} 且 n >= {min_samples} 的因子组合。")
        print(f"  建议：降低 target_prob 或 min_samples，或增加数据量。")
        combos = pd.DataFrame()
    else:
        print(f"  共找到 {len(combos)} 个满足条件的因子组合，展示 Top-{top_n_combos}：\n")
        for i, row in combos.head(top_n_combos).iterrows():
            print(f"  #{i+1:>3}  [{row['combo_size']}因子]  P={row['win_rate']:.2%}  "
                  f"Alpha={row['alpha']:+.2%}  n={row['count']:>5}  因子: {row['factors']}")
            print(f"        阈值: {row['bin_key']}")
    print()

    # ── 【5】LGBM 叶节点 ──────────────────────────────────────────────────
    print("【5】LGBM 决策路径高概率叶节点分析")
    print(SEP)
    if not _HAS_LGB:
        print("  (未安装 lightgbm，跳过。pip install lightgbm)")
    else:
        leaf_df = lgbm_leaf_analysis(
            features_df,
            factor_cols=available,
            min_samples=min_samples,
            target_prob=target_prob,
        )
        if leaf_df.empty:
            print(f"  未找到 P > {target_prob:.0%} 的高概率叶节点（可能样本量不足或数据无区分度）。")
        else:
            print(f"  LGBM 自动挖掘到 {len(leaf_df)} 个高概率叶节点：\n")
            for _, row in leaf_df.iterrows():
                print(f"  树{row['tree_idx']:>2} 叶{row['leaf_id']:>3}  "
                      f"n={row['count']:>5}  预测概率={row['pred_prob_avg']:.2%}  "
                      f"实际胜率={row['actual_win_rate']:.2%}")
                try:
                    ranges = ast.literal_eval(row["factor_ranges"])
                    for fname, (lo, hi) in ranges.items():
                        desc = FEATURE_DESC.get(fname, fname)
                        print(f"      {fname}: [{lo:.4f}, {hi:.4f}]  ({desc})")
                except Exception:
                    print(f"      {row['factor_ranges']}")
                print()
    print()

    # ── 【6】Walk-Forward 稳健性验证 ─────────────────────────────────────
    print("【6】Walk-Forward 时间分层稳健性验证 (Walk-Forward Validation)")
    print(SEP)
    if not show_walk_forward:
        print("  (已跳过，使用 --walk-forward 开启)")
    elif combos is None or (hasattr(combos, 'empty') and combos.empty):
        print("  (无满足条件的组合，跳过验证)")
    else:
        print(f"  按时间等分 {n_walk_folds} 折，验证 Top 组合的跨时稳健性（样本量不足的折叠标注 N/A）\n")
        wf_df = combo_walkforward_validation(
            features_df,
            top_combos=combos,
            n_folds=n_walk_folds,
            target_prob=target_prob,
            top_k=min(10, len(combos)),
        )
        if wf_df.empty:
            print("  (走势验证无结果，可能数据量不足)")
        else:
            robust_count = int(wf_df["is_robust"].sum())
            print(f"  验证 Top-{len(wf_df)} 组合，其中 {robust_count} 个通过稳健性检验：\n")
            for _, r in wf_df.iterrows():
                robust_label = "✅ 稳健" if r["is_robust"] else "⚠ 不稳定"
                mean_wr = f"{r['mean_fold_wr']:.2%}" if not pd.isna(r['mean_fold_wr']) else "N/A"
                std_wr = f"{r['std_fold_wr']:.2%}" if not pd.isna(r['std_fold_wr']) else "N/A"
                print(f"  {robust_label}  全量P={r['full_win_rate']:.2%}  "
                      f"折均P={mean_wr}  折标准差={std_wr}  "
                      f"超阈折数={r['n_folds_above_target']}/{r['n_valid_folds']}")
                print(f"    因子: {r['factors']}")
                print(f"    各折: {r['fold_details']}")
                print()
        print("  📌 注意：折均P 与全量P 差距过大，或标准差过高，说明组合可能存在时间依赖性过拟合。")
    print()

    # ── 【7】交易过滤器导出 ───────────────────────────────────────────────
    print("【7】交易过滤器导出 (Trading Filter Conditions)")
    print(SEP)
    if not show_trading_filters:
        print("  (已跳过，使用 --save-filters 开启)")
    elif combos is None or (hasattr(combos, 'empty') and combos.empty):
        print("  (无满足条件的组合，跳过过滤器导出)")
    else:
        # 优先展示 Walk-Forward 验证通过的组合
        if show_walk_forward and not wf_df.empty:
            robust_combos = wf_df[wf_df["is_robust"]].head(5)
            filters_source = combos[combos["factors"].isin(robust_combos["factors"])].head(5)
            if filters_source.empty:
                filters_source = combos.head(5)
            label = "稳健性验证通过的"
        else:
            filters_source = combos.head(5)
            label = "全量胜率最高的"
        print(f"  展示 {label} Top-5 组合的可执行过滤条件：\n")
        filters_df = export_trading_filters(
            filters_source,
            target_prob=target_prob,
            top_k=5,
            save_path=save_filters_path,
        )
        for _, frow in filters_df.iterrows():
            print(f"  [过滤器 #{frow['rank']}]  预期胜率={frow['expected_win_rate']:.2%}  "
                  f"Alpha={frow['alpha']:+.2%}  样本量={frow['sample_count']}  "
                  f"[{frow['combo_size']}因子]")
            print(f"    IF:")
            print(f"    {frow['conditions_human']}")
            print(f"    → 预期第三根 K 线延续同向概率 = {frow['expected_win_rate']:.2%}")
            print()
        if save_filters_path:
            print(f"  [✓] 完整过滤条件已保存到 JSON: {save_filters_path}")
        else:
            print("  💡 提示: 使用 --save-filters filters.json 可将过滤条件保存到文件。")
    print(SEP + "\n")


def _print_factor_evaluation(mock_oi: bool = False, mock_premium: bool = False) -> None:
    """打印因子合理性评估与研究建议（基于市场微结构文献综述）。"""
    mock_oi_flag = " [⚠ 当前使用占位数据，信号不可靠]" if mock_oi else ""
    mock_premium_flag = " [⚠ 当前使用占位数据，信号不可靠]" if mock_premium else ""
    print(f"""
  ┌─────────────────────────────────────────────────────────────────────┐
  │             因子体系评估（V1 + V2 共 9 个因子）                       │
  └─────────────────────────────────────────────────────────────────────┘

  [原 V1 因子 — 评估]
  ✅ feat_1  尾盘 CVD 比率
       → 微观订单流失衡是短期动量最强预测因子之一（Chordia et al.2002）。
         CVD > 0 且加速是趋势延续的强信号。
  ✅ feat_2  交易密集度突变率
       → Tick 频率突增表明流动性吸收方向明确，动能加速（Hasbrouck 2007）。
  ⚠  feat_3  OI 增量 %{mock_oi_flag}
       → OI 增加 + 上涨 = 新多头建仓（健康趋势）；OI 减少 + 上涨 = 空头回补。
         1 分钟或 5 分钟粒度 OI 数据延迟可能降低信号质量。
         提供真实 OI 数据可显著提升 feat_3 的预测能力。
  ⚠  feat_4  期现溢价变化{mock_premium_flag}
       → 基差缩小（Perp 弱于 Spot）表明衍生品过度定价正在修正，
         连涨动能耗尽风险增大。信号较弱，需配合其他因子。
  ✅ feat_5  4H VWAP 偏离度
       → 重要的均值回归风险过滤器：偏离过大（>50bps）时连涨延续概率下降。

  [新增 V2 因子 — 理由与评估]
  ✅ feat_rsi_14  RSI(14) on 5m
       → 经典动量强度指标。RSI 40-60（中性区间）延续概率最高；
         RSI > 70 超买时反转风险大幅上升（Wilder 1978）。
  ✅ feat_vol_ratio  成交量确信度
       → 放量突破（> 1.5x 均量）是趋势延续的强确认信号
         （O'Neil CANSLIM 方法论；CMT Level II）。
  ✅ feat_body_str  K 线实体强度
       → 实体占比高（> 0.7）表明当前 K 线方向坚定、wick 少，
         做市商未能阻止价格移动（Japanese Candlestick Analysis）。
  ✅ feat_mom_accel  动量加速度
       → Bar2 比 Bar1 涨幅更大（正加速）说明买压在增强，
         是趋势延续的 leading indicator。

  [建议追加的高价值因子（数据许可时）]
  💡 资金费率 (Funding Rate)：极高正费率（>0.1%/8h）是强劲反转预警。
  💡 盘口委托量不平衡 (Order Book Imbalance)：Buy_depth/Sell_depth > 1.5 表示做市商
       偏向买方，连涨延续概率显著提升（Huang & Stoll 1997）。
  💡 Liquidation 数量：大额多头强制平仓后出现急涨，往往是虚假信号。
  💡 1s CVD 斜率（线性回归斜率）：比单纯求和更能反映 CVD 的加速度方向。
""")


# ---------------------------------------------------------------------------
# 便捷函数：对 features_df 做快速单列分析（可用于 Notebook 探索）
# ---------------------------------------------------------------------------

def quick_factor_summary(
    features_df: pd.DataFrame,
    factor_cols: Optional[list[str]] = None,
    n_bins: int = 5,
    target_col: str = "target",
) -> pd.DataFrame:
    """
    对所有因子做分位数分析，返回一个汇总 DataFrame：
    每个因子的最优桶（win_rate 最高）及其胜率、Alpha、样本量。
    适合在 Jupyter Notebook 中快速对比。
    """
    if factor_cols is None:
        factor_cols = [c for c in ALL_FEATURE_COLS if c in features_df.columns]
    rows = []
    base_wr = float(features_df[target_col].mean())
    for col in factor_cols:
        cond_df = factor_conditional_prob(features_df, col, n_bins=n_bins, target_col=target_col)
        if cond_df.empty:
            continue
        best = cond_df.loc[cond_df["win_rate"].idxmax()]
        rows.append({
            "factor": col,
            "description": FEATURE_DESC.get(col, ""),
            "best_bin": best["bin_label"],
            "best_bin_range": best["bin_range"],
            "best_win_rate": round(float(best["win_rate"]), 4),
            "best_alpha": round(float(best["alpha"]), 4),
            "best_count": int(best["count"]),
            "base_win_rate": round(base_wr, 4),
        })
    return pd.DataFrame(rows).sort_values("best_alpha", ascending=False).reset_index(drop=True)
