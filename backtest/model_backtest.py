"""
模型训练与回测评估模块 (model_backtest.py)
时间序列划分、LGBM 训练、概率输出、阈值分析、特征重要性与基准对比。
"""

from __future__ import annotations

import warnings
from typing import Optional

import numpy as np
import pandas as pd
from sklearn.metrics import (
    accuracy_score,
    precision_score,
    recall_score,
    roc_auc_score,
    confusion_matrix,
)
from sklearn.linear_model import LogisticRegression

try:
    import lightgbm as lgb
    HAS_LGB = True
except Exception:
    lgb = None
    HAS_LGB = False

try:
    import shap
    HAS_SHAP = True
except ImportError:
    HAS_SHAP = False

warnings.filterwarnings("ignore", category=UserWarning)

FEATURE_COLS = ["feat_1", "feat_2", "feat_3", "feat_4", "feat_5"]
# V2 全量特征（含 V1 + 扩展因子）
V2_ALL_FEATURES = [
    "feat_1", "feat_2", "feat_3", "feat_4", "feat_5",
    "feat_rsi_14", "feat_vol_ratio", "feat_body_str", "feat_mom_accel",
]
# V1 纯血模型：仅 feat_1/2/5，不引入时区/波动率等宏观特征（避免维度灾难与过拟合）
V1_PURE_FEATURES = ["feat_1", "feat_2", "feat_5"]
# 保留旧常量以兼容 Mock 等路径
PURIFIED_FEATURES = V1_PURE_FEATURES
TARGET_COL = "target"

# 事后归因用 session 映射：feat_session 1=Asia, 2=Europe, 3=US, 0=Other
SESSION_NAMES = {0: "Other", 1: "Asia", 2: "Europe", 3: "US"}


def time_split(
    df: pd.DataFrame,
    train_months: int = 8,
    test_months: int = 4,
    train_ratio: Optional[float] = None,
    date_col: str = "snapshot_time",
    feature_cols: Optional[list[str]] = None,
) -> tuple[pd.DataFrame, pd.DataFrame, pd.Series, pd.Series]:
    """
    按时间严格划分，不洗牌。优先使用 train_ratio（如 0.8 表示前 80% 训练、后 20% 测试）；
    否则按 train_months / test_months 划分。
    """
    df = df.sort_values(date_col).reset_index(drop=True)
    df[date_col] = pd.to_datetime(df[date_col])
    t_min, t_max = df[date_col].min(), df[date_col].max()
    if train_ratio is not None and 0 < train_ratio < 1:
        split_idx = int(len(df) * train_ratio)
        train_df = df.iloc[:split_idx]
        test_df = df.iloc[split_idx:]
    else:
        total_months = (t_max - t_min).days / 30.0 + (t_max - t_min).seconds / (30 * 86400)
        if total_months < train_months + test_months:
            split_idx = int(len(df) * 2 / 3)
            train_df = df.iloc[:split_idx]
            test_df = df.iloc[split_idx:]
            if train_df[TARGET_COL].nunique() < 2 and len(test_df) > 0:
                split_idx = int(len(df) * 0.5)
                train_df = df.iloc[:split_idx]
                test_df = df.iloc[split_idx:]
        else:
            split_time = t_min + pd.DateOffset(months=train_months)
            train_df = df.loc[df[date_col] < split_time]
            test_df = df.loc[df[date_col] >= split_time]
    cols = feature_cols or FEATURE_COLS
    avail = [c for c in cols if c in df.columns]
    X_train = train_df[avail]
    y_train = train_df[TARGET_COL]
    X_test = test_df[avail]
    y_test = test_df[TARGET_COL]
    return train_df, test_df, X_train, y_train, X_test, y_test


def train_lgb(
    X_train: pd.DataFrame,
    y_train: pd.Series,
    X_test: Optional[pd.DataFrame] = None,
    y_test: Optional[pd.Series] = None,
    **kwargs,
):
    """训练 LGBM 二分类（调优超参：更平滑置信度、防过拟合），输出概率。"""
    if HAS_LGB:
        params = {
            "objective": "binary",
            "metric": "binary_logloss",
            "verbosity": -1,
            "n_estimators": 150,
            "learning_rate": 0.01,
            "num_leaves": 15,
            "max_depth": 4,
            "min_child_samples": 50,
            "subsample": 0.8,
            "colsample_bytree": 0.8,
            "random_state": 42,
            "class_weight": "balanced",
            "missing": np.nan,
        }
        params.update(kwargs)
        model = lgb.LGBMClassifier(**params)
        model.fit(
            X_train,
            y_train,
            eval_set=[(X_test, y_test)] if X_test is not None and y_test is not None else None,
            callbacks=[lgb.early_stopping(80, verbose=False)] if X_test is not None else None,
        )
    else:
        model = LogisticRegression(max_iter=500, random_state=42)
        X_tr = X_train.fillna(0)
        if y_train.nunique() < 2:
            # 单类别时仍拟合（用于管道跑通），预测时全为同一类概率
            import warnings
            warnings.warn("训练集仅含单一类别，模型仅作演示。请使用更长周期数据或检查标签分布。")
        model.fit(X_tr, y_train)
    p_train = model.predict_proba(X_train.fillna(0) if not HAS_LGB else X_train)[:, 1]
    X_t = (X_test.fillna(0) if not HAS_LGB else X_test) if X_test is not None else None
    p_test = model.predict_proba(X_t)[:, 1] if X_t is not None else np.array([])
    return model, p_train, p_test


def threshold_analysis(
    y_true: np.ndarray | pd.Series,
    p_pred: np.ndarray,
    thresholds: list[float] = (0.55, 0.60, 0.65, 0.70),
) -> pd.DataFrame:
    """按概率阈值分桶，计算触发次数与真实胜率。"""
    y_true = np.asarray(y_true)
    rows = []
    for th in thresholds:
        mask = p_pred > th
        n = mask.sum()
        if n == 0:
            rows.append({"threshold": th, "trade_count": 0, "win_rate": np.nan})
            continue
        win_rate = y_true[mask].mean()
        rows.append({"threshold": th, "trade_count": int(n), "win_rate": win_rate})
    return pd.DataFrame(rows)


def baseline_win_rate(y_true: np.ndarray | pd.Series) -> float:
    """基准：无脑买（全样本）胜率。"""
    return float(np.asarray(y_true).mean())


def print_final_evaluation_report(
    model,
    X_test: pd.DataFrame,
    y_test: pd.Series,
    p_test: np.ndarray,
    feature_names: list[str] = None,
) -> None:
    """
    《投产前终极报告》— 剥离 Mock 噪音后的 AUC、feat_2 极端分位、Polymarket EV 模拟。
    """
    if feature_names is None:
        feature_names = PURIFIED_FEATURES if X_test.shape[1] == len(PURIFIED_FEATURES) else FEATURE_COLS
    y_test = np.asarray(y_test)
    base_wr = baseline_win_rate(y_test)
    test_size = len(y_test)

    print("\n")
    print("=" * 60)
    print("  《投产前终极报告 V2》Pre-Production Ultimate Report V2")
    print("  (剥离 Mock + 时区/波动率特征增强)")
    print("=" * 60)
    print()
    print("【1】基础样本信息")
    print("  • 总测试集样本数 (Test Size):", test_size)
    print("  • 测试集基准胜率 (Baseline Win Rate):", f"{base_wr:.2%}",
          "（动能>0.3% 连涨/连跌事件中，第三根 K 线无脑顺势买入的真实胜率）")
    print()
    print("【2】模型表现指标")
    if len(np.unique(y_test)) > 1:
        print("  • AUC-ROC:", f"{roc_auc_score(y_test, p_test):.4f}")
    else:
        print("  • AUC-ROC: N/A (测试集单类别)")
    print()
    print("【3】置信度阈值分析 (Threshold Analysis)")
    print("  --- 顺势（押注继续涨 Y=1）---")
    for th in (0.50, 0.55, 0.60):
        mask = p_test > th
        n = int(mask.sum())
        wr = y_test[mask].mean() if n > 0 else np.nan
        wr_str = f"{wr:.2%}" if not np.isnan(wr) else "N/A"
        print(f"  • 当模型预测概率 P > {th:.2f} 时，触发次数 = {n}，实际胜率(Y=1) = {wr_str}")
    print("  --- 反向（押注反转 Y=0，买不涨/做空）---")
    for th in (0.50, 0.48, 0.45):
        mask_low = p_test < th
        n = int(mask_low.sum())
        # 反转胜率 = 该区间内 Y=0 的比例（押注不涨赢的概率）
        wr_inverse = (y_test[mask_low] == 0).mean() if n > 0 else np.nan
        wr_str = f"{wr_inverse:.2%}" if not np.isnan(wr_inverse) else "N/A"
        print(f"  • 当模型预测概率 P < {th:.2f} 时，触发次数 = {n}，反转实际胜率(Y=0) = {wr_str}")
    print()
    print("【4】特征分布与反转 (feat_2 极端分位数)")
    if "feat_2" in X_test.columns:
        for q_name, q_val in (("75%", 0.75), ("90%", 0.90)):
            q_th = X_test["feat_2"].quantile(q_val)
            mask_high = X_test["feat_2"] > q_th
            n_high = int(mask_high.sum())
            wr_y1 = y_test[mask_high].mean() if n_high > 0 else np.nan
            wr_y0 = (y_test[mask_high] == 0).mean() if n_high > 0 else np.nan
            wr1_str = f"{wr_y1:.2%}" if not np.isnan(wr_y1) else "N/A"
            wr0_str = f"{wr_y0:.2%}" if not np.isnan(wr_y0) else "N/A"
            print(f"  • feat_2 > {q_name} 分位数 ({q_th:.4f}): 样本数 = {n_high}, 继续涨(Y=1)胜率 = {wr1_str}, 反转(Y=0)胜率 = {wr0_str}")
    print()
    print("【5】特征重要性 (Feature Importance)")
    if hasattr(model, "feature_importances_"):
        imp = model.feature_importances_
        try:
            if hasattr(model, "booster_") and model.booster_ is not None:
                gain = model.booster_.feature_importance(importance_type="gain")
                for i, name in enumerate(feature_names):
                    print(f"  • {name}: {gain[i]:.2f} (gain)")
            else:
                for name, val in sorted(zip(feature_names, imp), key=lambda x: -x[1]):
                    print(f"  • {name}: {int(val)} (split)")
        except Exception:
            for name, val in sorted(zip(feature_names, imp), key=lambda x: -x[1]):
                print(f"  • {name}: {int(val)} (split)")
    else:
        if hasattr(model, "coef_"):
            imp = np.abs(model.coef_[0])
            for name, val in sorted(zip(feature_names, imp), key=lambda x: -x[1]):
                print(f"  • {name} (|coef|): {val}")
    print()
    print("【6】亚洲盘 vs 美洲盘 反转胜率 (P<0.50 触发样本)")
    if "feat_session" in X_test.columns:
        mask_rev = p_test < 0.50
        idx_rev = np.where(mask_rev)[0]
        X_rev = X_test.iloc[idx_rev] if hasattr(X_test, "iloc") else X_test.values[idx_rev]
        y_rev = y_test.iloc[idx_rev] if hasattr(y_test, "iloc") else np.asarray(y_test)[idx_rev]
        if hasattr(X_rev, "iloc"):
            mask_asia = (X_rev["feat_session"] == 1).values
            mask_americas = (X_rev["feat_session"] == 3).values
        else:
            sess_col = list(X_test.columns).index("feat_session") if hasattr(X_test, "columns") else -1
            mask_asia = X_rev[:, sess_col] == 1 if sess_col >= 0 else np.zeros(len(X_rev), dtype=bool)
            mask_americas = X_rev[:, sess_col] == 3 if sess_col >= 0 else np.zeros(len(X_rev), dtype=bool)
        n_asia = int(mask_asia.sum())
        n_americas = int(mask_americas.sum())
        wr_asia = (np.asarray(y_rev)[mask_asia] == 0).mean() if n_asia > 0 else np.nan
        wr_americas = (np.asarray(y_rev)[mask_americas] == 0).mean() if n_americas > 0 else np.nan
        print(f"  • 亚洲盘 (00:00-08:00 UTC): 样本数 = {n_asia}, 反转胜率(Y=0) = {wr_asia:.2%}" if not np.isnan(wr_asia) else f"  • 亚洲盘: 样本数 = {n_asia}")
        print(f"  • 美洲盘 (13:30-22:00 UTC): 样本数 = {n_americas}, 反转胜率(Y=0) = {wr_americas:.2%}" if not np.isnan(wr_americas) else f"  • 美洲盘: 样本数 = {n_americas}")
    else:
        print("  • (无 feat_session，跳过)")
    print()
    print("【7】Polymarket 期望值 (EV) 商业模拟")
    cost_per_share = 0.50
    payout_per_win = 1.00
    mask_reversal = p_test < 0.50
    reversal_trades = int(mask_reversal.sum())
    reversal_wins = int((y_test[mask_reversal] == 0).sum())
    reversal_win_rate = reversal_wins / reversal_trades if reversal_trades > 0 else np.nan
    total_cost = reversal_trades * cost_per_share
    total_revenue = reversal_wins * payout_per_win
    net_profit = total_revenue - total_cost
    roi = (net_profit / total_cost) * 100 if total_cost > 0 else np.nan
    print(f"  • 设定: 反转信号(P<0.50)时以 $0.50 买入 NO/DOWN 合约，正确得 $1.00")
    print(f"  • 触发次数 = {reversal_trades}, 反转正确次数 = {reversal_wins}, 反转胜率 = {reversal_win_rate:.2%}" if not np.isnan(reversal_win_rate) else f"  • 触发次数 = {reversal_trades}")
    print(f"  • 总成本 = ${total_cost:.2f}, 总收益 = ${total_revenue:.2f}, 净利润 = ${net_profit:.2f}, ROI = {roi:.2f}%")
    print("  • (投产前终极报告 V2 - 含时区/波动率特征)")
    print()
    print("=" * 60)


def print_pure_attribution_report(
    test_df: pd.DataFrame,
    p_test: np.ndarray,
    cost_per_share: float = 0.50,
    payout_per_win: float = 1.00,
) -> None:
    """
    《纯血模型事后归因报告》— 仅用 feat_1/2/5 训练，事后按 session/vol_regime 切片分析。
    时区与波动率不参与预测，仅作为 Post-Trade 归因维度。
    """
    test_df = test_df.reset_index(drop=True)
    y_test = np.asarray(test_df["target"])
    prob = np.asarray(p_test)

    # 构建 df_results：prob, actual_y, session, vol_regime（上下文维度，不参与预测）
    df_results = pd.DataFrame({
        "prob": prob,
        "actual_y": y_test,
    })
    # session: 从 feat_session 映射 (1=Asia 00:00-08:00, 2=Europe 08:00-13:30, 3=US 13:30-22:00, 0=Other)
    if "feat_session" in test_df.columns:
        df_results["session"] = test_df["feat_session"].map(SESSION_NAMES).fillna("Other")
    else:
        df_results["session"] = "Other"
    # vol_regime: ATR_1h / ATR_24h
    df_results["vol_regime"] = test_df["feat_vol_regime"].values if "feat_vol_regime" in test_df.columns else 1.0

    # 过滤反转策略触发订单 (prob < 0.50)
    rev = df_results[df_results["prob"] < 0.50].copy()
    rev["win"] = (rev["actual_y"] == 0).astype(int)
    rev["cost"] = cost_per_share
    rev["revenue"] = rev["win"] * payout_per_win
    rev["net_profit"] = rev["revenue"] - rev["cost"]

    print("\n")
    print("=" * 60)
    print("  《纯血模型事后归因报告》Pure V1 Post-Trade Attribution Report")
    print("  (仅 feat_1/2/5 训练，时区/波动率为事后切片维度)")
    print("=" * 60)
    print()

    # 【1】整体反转策略表现 (V1 基准复查)
    n_rev = len(rev)
    rev_wins = int(rev["win"].sum())
    rev_wr = rev_wins / n_rev if n_rev > 0 else np.nan
    total_cost = n_rev * cost_per_share
    total_revenue = rev_wins * payout_per_win
    net_profit = total_revenue - total_cost
    roi = (net_profit / total_cost) * 100 if total_cost > 0 else np.nan
    print("【1】整体反转策略表现 (V1 基准复查)")
    print(f"  • 触发次数: {n_rev}")
    print(f"  • 反转胜率 (Y=0): {rev_wr:.2%}" if not np.isnan(rev_wr) else f"  • 反转胜率: N/A")
    print(f"  • 总成本: ${total_cost:.2f}, 总收益: ${total_revenue:.2f}, 净利润: ${net_profit:.2f}, ROI: {roi:.2f}%")
    print()

    # 【2】按时区切片 (Session Analysis)
    print("【2】按时区切片的表现 (Session Analysis)")
    for sess_name in ["Asia", "Europe", "US", "Other"]:
        sub = rev[rev["session"] == sess_name]
        n = len(sub)
        if n == 0:
            print(f"  • {sess_name}: 触发 0 次")
            continue
        wr = (sub["win"].sum() / n) * 100
        net = sub["net_profit"].sum()
        print(f"  • {sess_name}: 触发 {n} 次, 反转胜率 {wr:.2f}%, 净利润 ${net:.2f}")
    print()

    # 【3】按波动率切片 (Volatility Analysis)
    print("【3】按波动率切片的表现 (Volatility Analysis)")
    high_vol = rev[rev["vol_regime"] > 1.5]
    low_vol = rev[rev["vol_regime"] <= 1.5]
    for label, sub in [("高波动 (vol_regime > 1.5)", high_vol), ("平稳波动 (vol_regime <= 1.5)", low_vol)]:
        n = len(sub)
        if n == 0:
            print(f"  • {label}: 触发 0 次")
            continue
        wr = (sub["win"].sum() / n) * 100
        net = sub["net_profit"].sum()
        print(f"  • {label}: 触发 {n} 次, 反转胜率 {wr:.2f}%, 净利润 ${net:.2f}")
    print()
    print("=" * 60)


def print_report(
    model,
    X_test: pd.DataFrame,
    y_test: pd.Series,
    p_test: np.ndarray,
    y_train: pd.Series,
    p_train: np.ndarray,
    feature_names: list[str] = FEATURE_COLS,
) -> None:
    """输出 PRD 要求的评估报告：AUC/Precision/Recall、阈值分析、特征重要性、基准对比。"""
    y_test = np.asarray(y_test)
    y_train = np.asarray(y_train)

    print("=" * 60)
    print("【1】基础分类指标 (测试集)")
    print("=" * 60)
    if len(np.unique(y_test)) > 1:
        print(f"  AUC-ROC:    {roc_auc_score(y_test, p_test):.4f}")
    else:
        print("  AUC-ROC:    N/A (单类别)")
    print(f"  Precision:  {precision_score(y_test, (p_test > 0.5).astype(int), zero_division=0):.4f}")
    print(f"  Recall:     {recall_score(y_test, (p_test > 0.5).astype(int), zero_division=0):.4f}")
    print(f"  Accuracy:   {accuracy_score(y_test, (p_test > 0.5).astype(int)):.4f}")
    print(f"  Confusion: {confusion_matrix(y_test, (p_test > 0.5).astype(int)).tolist()}")

    print("\n" + "=" * 60)
    print("【2】置信度阈值分析 (Threshold Analysis)")
    print("=" * 60)
    th_df = threshold_analysis(y_test, p_test)
    for _, r in th_df.iterrows():
        wr = f"{r['win_rate']:.1%}" if not pd.isna(r["win_rate"]) else "N/A"
        print(f"  当模型预测概率 P > {r['threshold']:.0%} 时，样本外触发 {int(r['trade_count'])} 次，实际胜率 {wr}")
    print(th_df.to_string(index=False))

    print("\n" + "=" * 60)
    print("【3】特征重要性 (Feature Importance)")
    print("=" * 60)
    if hasattr(model, "feature_importances_"):
        imp = model.feature_importances_
        for name, val in sorted(zip(feature_names, imp), key=lambda x: -x[1]):
            print(f"  {name}: {val}")
    else:
        if hasattr(model, "coef_"):
            imp = np.abs(model.coef_[0])
            for name, val in sorted(zip(feature_names, imp), key=lambda x: -x[1]):
                print(f"  {name} (|coef|): {val}")
        else:
            print("  (当前模型无 feature_importances_)")
    if HAS_SHAP and hasattr(model, "predict_proba") and X_test.shape[0] <= 5000:
        try:
            explainer = shap.TreeExplainer(model, X_test)
            shap_vals = explainer.shap_values(X_test)
            if isinstance(shap_vals, list):
                shap_vals = shap_vals[1]
            print("\n  SHAP 重要性 (mean |SHAP|):")
            for i, name in enumerate(feature_names):
                print(f"    {name}: {np.abs(shap_vals[:, i]).mean():.4f}")
        except Exception as e:
            print(f"  (SHAP 计算跳过: {e})")

    print("\n" + "=" * 60)
    print("【4】基准对比 (Baseline)")
    print("=" * 60)
    base_wr = baseline_win_rate(y_test)
    print(f"  基准（连涨后无脑买第三根）全样本胜率: {base_wr:.1%}")
    model_wr_05 = (y_test[p_test > 0.5].mean() if (p_test > 0.5).sum() > 0 else np.nan)
    if not np.isnan(model_wr_05):
        print(f"  模型 P>50% 时胜率: {model_wr_05:.1%}，超额: {model_wr_05 - base_wr:+.1%}")
    print("=" * 60)


def run_backtest_pipeline(
    features_df: pd.DataFrame,
    train_months: int = 8,
    test_months: int = 4,
    train_ratio: Optional[float] = None,
):
    """
    完整回测管道：时间划分 → 训练 LGBM/逻辑回归 → 输出概率 → 返回模型、训练/测试集、预测概率。
    严格使用 V1 纯血特征 feat_1/2/5，不引入时区/波动率（事后归因用）。
    train_ratio=0.8 表示前 80% 时间训练、后 20% 测试。
    """
    train_df, test_df, X_train, y_train, X_test, y_test = time_split(
        features_df, train_months=train_months, test_months=test_months, train_ratio=train_ratio,
        feature_cols=V1_PURE_FEATURES,
    )
    # 若训练集仅单一类别，用全量数据训练并在全量上评估（仅用于 Mock 等短数据演示）
    if y_train.nunique() < 2 and len(features_df) > 10:
        train_df = features_df
        test_df = features_df
        avail = [c for c in V1_PURE_FEATURES if c in features_df.columns]
        X_train = train_df[avail]
        y_train = train_df[TARGET_COL]
        X_test = X_train
        y_test = y_train
    # X_train/X_test 仅含 V1 纯血特征 feat_1/2/5
    print("\n" + "=" * 60)
    print("【训练前】X_train 描述统计 (V1 纯血 feat_1/2/5)")
    print("=" * 60)
    print(X_train.describe())
    print("\n" + "=" * 60)
    print("【训练前】X_train 缺失值统计 (V1 纯血)")
    print("=" * 60)
    print(X_train.isnull().sum())
    print("=" * 60 + "\n")
    model, p_train, p_test = train_lgb(X_train, y_train, X_test=X_test, y_test=y_test)
    return model, train_df, test_df, p_train, p_test
