"""
回测模块：数据加载、特征工程、模型训练与评估。
"""

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
    V1_PURE_FEATURES,
    print_pure_attribution_report,
    print_report,
    run_backtest_pipeline,
)

__all__ = [
    "build_event_pool_and_1s_slices",
    "compute_5m_vwap_feat5",
    "compute_features",
    "create_dummy_oi_premium_from_5m",
    "generate_mock_data_1week",
    "load_bar_stats_from_1s_binance",
    "load_kline_5m",
    "load_oi",
    "load_premium",
    "load_trades_1s",
    "print_pure_attribution_report",
    "print_report",
    "run_backtest_pipeline",
    "V1_PURE_FEATURES",
]
