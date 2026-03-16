"""
连续趋势概率分析入口 (run_streak_analysis.py)

功能
----
在已有的 BTC 回测管道基础上，增加**因子组合条件概率分析**：
找出哪些因子值区间的组合，能使「连续 >= 3 根同向 K 线」的概率 > 50%。

用法示例
--------
# 使用 Mock 数据（快速验证）
python run_streak_analysis.py --mock

# 使用真实数据（做多场景）
python run_streak_analysis.py \\
    --kline-5m data/BTCUSDT_5m.csv \\
    --trades-1s data/BTCUSDT_1s.csv \\
    --direction 1

# 指定目标阈值与分桶数
python run_streak_analysis.py --mock --target-prob 0.55 --n-bins 3

输出说明
--------
  【0】因子合理性评估与建议
  【1】样本基础信息（事件数、基准胜率）
  【2】各因子单独的分位数条件概率分析
  【3】最优双因子交叉矩阵
  【4】满足 P > target_prob 的多因子组合排行
  【5】LGBM 高概率叶节点解读
"""

from __future__ import annotations

import argparse
from pathlib import Path

from backtest.data_loader import (
    build_event_pool_and_1s_slices,
    compute_5m_vwap_feat5,
    create_dummy_oi_premium_from_5m,
    generate_mock_data_1week,
    load_bar_stats_from_1s_binance,
    load_kline_5m,
    load_oi,
    load_premium,
)
from backtest.feature_engineering import compute_features
from backtest.streak_analysis import print_streak_analysis_report, ALL_FEATURE_COLS


def run_streak_with_mock(
    direction: int = 1,
    target_prob: float = 0.55,
    n_bins: int = 3,
    min_samples: int = 10,
) -> None:
    """使用约 1 周 Mock 数据跑通连续趋势概率分析管道。"""
    print("使用 Mock 数据（约 1 周）跑通连续趋势概率分析管道...")
    kline_5m, trades_1s, oi, premium = generate_mock_data_1week()
    kline_5m = compute_5m_vwap_feat5(kline_5m, window_bars=48)
    event_df, meta = build_event_pool_and_1s_slices(
        kline_5m, trades_1s, oi, premium, direction=direction
    )
    if len(event_df) == 0:
        print("Mock 数据下未检测到符合条件的事件，请增加 Mock 数据长度或放宽条件。")
        return
    print(f"事件数: {len(event_df)}")
    features_df = compute_features(event_df, meta, direction=direction)
    if len(features_df) == 0:
        print("特征计算后无有效样本（可能 Bar3 尚未收盘）。")
        return
    print(f"有效样本数: {len(features_df)}")
    print(f"已计算特征列: {[c for c in features_df.columns if c.startswith('feat')]}")

    available_factors = [c for c in ALL_FEATURE_COLS if c in features_df.columns]
    print_streak_analysis_report(
        features_df,
        factor_cols=available_factors,
        n_bins=n_bins,
        min_samples=min_samples,
        target_prob=target_prob,
        top_n_combos=20,
    )


def run_streak_with_real_data(
    path_5m: str,
    path_1s: str,
    path_oi: str = "",
    path_premium: str = "",
    direction: int = 1,
    target_prob: float = 0.55,
    n_bins: int = 3,
    min_samples: int = 30,
) -> None:
    """使用真实数据运行连续趋势概率分析管道。"""
    print("加载 5m K 线...")
    kline_5m = load_kline_5m(path_5m)
    kline_5m = compute_5m_vwap_feat5(kline_5m, window_bars=48)

    print("从 1s 文件流式聚合 bar 统计（可能需数分钟）...")
    bar_stats = load_bar_stats_from_1s_binance(path_1s)

    if path_oi and Path(path_oi).exists():
        oi = load_oi(path_oi)
    else:
        print("未提供 OI 文件，使用 5m K 线生成占位 OI 数据。")
        oi, _ = create_dummy_oi_premium_from_5m(kline_5m)

    if path_premium and Path(path_premium).exists():
        premium = load_premium(path_premium)
    else:
        print("未提供 Premium 文件，使用 5m K 线生成占位期现数据。")
        _, premium = create_dummy_oi_premium_from_5m(kline_5m)

    event_df, meta = build_event_pool_and_1s_slices(
        kline_5m, trades_1s=None, oi=oi, premium=premium,
        bar_stats=bar_stats, direction=direction,
    )
    print(f"事件数: {len(event_df)}")
    features_df = compute_features(event_df, meta, direction=direction)
    print(f"有效样本数: {len(features_df)}")
    print(f"已计算特征列: {[c for c in features_df.columns if c.startswith('feat')]}")

    available_factors = [c for c in ALL_FEATURE_COLS if c in features_df.columns]
    print_streak_analysis_report(
        features_df,
        factor_cols=available_factors,
        n_bins=n_bins,
        min_samples=min_samples,
        target_prob=target_prob,
        top_n_combos=30,
    )


def main() -> None:
    parser = argparse.ArgumentParser(
        description="BTC 连续 K 线趋势因子组合条件概率分析",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument("--mock", action="store_true",
                        help="使用 1 周 Mock 数据跑通（无需真实数据文件）")
    parser.add_argument("--direction", type=int, default=1, choices=[1, -1],
                        help="1=做多(预测第三根收阳), -1=做空(预测第三根收阴)")
    parser.add_argument("--kline-5m", type=str, default="",
                        help="5 分钟 K 线 CSV 路径（Binance 格式）")
    parser.add_argument("--trades-1s", type=str, default="",
                        help="1 秒聚合交易 CSV 路径")
    parser.add_argument("--oi", type=str, default="",
                        help="持仓量 CSV 路径（可选，无则自动生成占位数据）")
    parser.add_argument("--premium", type=str, default="",
                        help="期现溢价 CSV 路径（可选）")
    parser.add_argument("--target-prob", type=float, default=0.55,
                        help="目标胜率阈值，默认 0.55（超过 55%%）")
    parser.add_argument("--n-bins", type=int, default=3,
                        help="每个因子的分位数桶数（2=低/高，3=低/中/高，5=五档）")
    parser.add_argument("--min-samples", type=int, default=20,
                        help="每个桶的最小样本量，低于此值的组合不纳入统计")
    args = parser.parse_args()

    if args.mock or not (args.kline_5m and args.trades_1s):
        if args.mock:
            run_streak_with_mock(
                direction=args.direction,
                target_prob=args.target_prob,
                n_bins=args.n_bins,
                min_samples=args.min_samples,
            )
        else:
            print("请提供 --kline-5m 与 --trades-1s 路径，或使用 --mock 跑 Mock 数据。")
            print("示例: python run_streak_analysis.py --mock")
    else:
        run_streak_with_real_data(
            path_5m=args.kline_5m,
            path_1s=args.trades_1s,
            path_oi=args.oi or "",
            path_premium=args.premium or "",
            direction=args.direction,
            target_prob=args.target_prob,
            n_bins=args.n_bins,
            min_samples=args.min_samples,
        )


if __name__ == "__main__":
    main()
