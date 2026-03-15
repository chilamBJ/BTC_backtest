"""
主入口：按 methodology.md 串联 backtest 模块，运行回测管道。
"""

from __future__ import annotations

import argparse
import pickle
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
    load_trades_1s,
)
from backtest.feature_engineering import compute_features
from backtest.model_backtest import (
    run_backtest_pipeline,
    print_report,
    print_pure_attribution_report,
    V1_PURE_FEATURES,
)


def _save_model(model, save_path: str) -> None:
    if not save_path:
        return
    path = Path(save_path).expanduser().resolve()
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "wb") as f:
        pickle.dump(model, f)
    print(f"模型已保存: {path}")


def run_with_mock(
    direction: int = 1,
    train_months: int = 8,
    test_months: int = 4,
    save_model_path: str = "",
) -> None:
    """使用约 1 周 Mock 数据跑通管道（便于验证逻辑）。"""
    print("使用 Mock 数据（约 1 周）跑通管道...")
    kline_5m, trades_1s, oi, premium = generate_mock_data_1week()
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
    model, train_df, test_df, p_train, p_test = run_backtest_pipeline(
        features_df, train_months=train_months, test_months=test_months
    )
    _save_model(model, save_model_path)
    X_test = test_df[["feat_1", "feat_2", "feat_3", "feat_4", "feat_5"]]
    y_test = test_df["target"]
    print_report(model, X_test, y_test, p_test, train_df["target"], p_train)
    return


def run_with_real_data(
    path_5m: str,
    path_1s: str,
    path_oi: str = "",
    path_premium: str = "",
    direction: int = 1,
    train_months: int = 8,
    test_months: int = 4,
    save_model_path: str = "",
) -> None:
    """使用真实数据文件运行管道。仅需 5m + 1s；无 OI/Premium 时自动用 5m 生成占位数据。"""
    print("加载 5m K 线...")
    kline_5m = load_kline_5m(path_5m)
    kline_5m = compute_5m_vwap_feat5(kline_5m, window_bars=48)
    print("从 1s 文件流式聚合 bar 统计（可能需数分钟）...")
    bar_stats = load_bar_stats_from_1s_binance(path_1s)
    if path_oi and Path(path_oi).exists():
        oi = load_oi(path_oi)
    else:
        oi, _ = create_dummy_oi_premium_from_5m(kline_5m)
    if path_premium and Path(path_premium).exists():
        premium = load_premium(path_premium)
    else:
        _, premium = create_dummy_oi_premium_from_5m(kline_5m)
    event_df, meta = build_event_pool_and_1s_slices(
        kline_5m, trades_1s=None, oi=oi, premium=premium, bar_stats=bar_stats, direction=direction
    )
    print(f"事件数: {len(event_df)}")
    features_df = compute_features(event_df, meta, direction=direction)
    print(f"有效样本数: {len(features_df)}")
    model, train_df, test_df, p_train, p_test = run_backtest_pipeline(
        features_df, train_ratio=0.8
    )
    _save_model(model, save_model_path)
    X_test_pure = test_df[V1_PURE_FEATURES]
    y_test = test_df["target"]
    print_pure_attribution_report(test_df, p_test)
    print_report(model, X_test_pure, y_test, p_test, train_df["target"], p_train, feature_names=V1_PURE_FEATURES)
    return


def main() -> None:
    parser = argparse.ArgumentParser(description="BTC 5分钟连续趋势二元预测回测管道")
    parser.add_argument("--mock", action="store_true", help="使用 1 周 Mock 数据跑通")
    parser.add_argument("--direction", type=int, default=1, choices=[1, -1], help="1=做多(连涨预测收阳), -1=做空")
    parser.add_argument("--train-months", type=int, default=8, help="训练集月数")
    parser.add_argument("--test-months", type=int, default=4, help="测试集月数")
    parser.add_argument("--kline-5m", type=str, default="", help="5分钟K线 CSV 路径")
    parser.add_argument("--trades-1s", type=str, default="", help="1秒聚合交易 CSV 路径")
    parser.add_argument("--oi", type=str, default="", help="持仓量 CSV 路径（可选）")
    parser.add_argument("--premium", type=str, default="", help="期现溢价 CSV 路径（可选）")
    parser.add_argument(
        "--save-model",
        type=str,
        default="data/models/model_seller.pkl",
        help="模型保存路径（默认 data/models/model_seller.pkl）",
    )
    args = parser.parse_args()

    if args.mock or not (args.kline_5m and args.trades_1s):
        if args.mock:
            run_with_mock(
                direction=args.direction,
                train_months=args.train_months,
                test_months=args.test_months,
                save_model_path=args.save_model,
            )
        else:
            print("请提供 --kline-5m 与 --trades-1s 路径，或使用 --mock 跑 Mock 数据。")
    else:
        run_with_real_data(
            args.kline_5m,
            args.trades_1s,
            args.oi or "",
            args.premium or "",
            direction=args.direction,
            train_months=args.train_months,
            test_months=args.test_months,
            save_model_path=args.save_model,
        )


if __name__ == "__main__":
    main()
