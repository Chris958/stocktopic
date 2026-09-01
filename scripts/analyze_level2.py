from __future__ import annotations

import argparse

from stocktopic.config import Settings
from stocktopic.level2 import format_level2_report
from stocktopic.service import StockTopicService


def main() -> None:
    parser = argparse.ArgumentParser(description="分析单只A股的猫爪Level-2主动委托资金")
    parser.add_argument("--code", default="603269.SH", help="6位股票代码或带.SH/.SZ后缀")
    parser.add_argument("--date", help="交易日期，YYYYMMDD或YYYY-MM-DD；默认最近可用交易日")
    args = parser.parse_args()
    settings = Settings.from_env()
    service = StockTopicService(settings)
    settings.ensure_directories()
    service.database.initialize()
    report = service.analyze_level2_stock(args.code, args.date)
    print(format_level2_report(report))
    profile = report["raw_profile"]
    print(f"BS标志分布：{profile['bs_flag']}")
    print(f"成交代码分布：{profile['trade_code']}")
    print(f"委托方向分布：{profile['order_side']}")
    print(f"委托类型分布：{profile['order_type']}")


if __name__ == "__main__":
    main()
