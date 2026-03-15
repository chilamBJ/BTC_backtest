"""
主入口：启动 7x24 数据采集器（Binance + Polymarket）。
"""

from __future__ import annotations

import asyncio

from collector.data_collector import main

if __name__ == "__main__":
    asyncio.run(main())
