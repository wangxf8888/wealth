"""5策略统一资金池回测运行器 - 支持先卖后买模式对比。

用法:
  cd /home/AIWealth && python -m backtest.run_all5
  cd /home/AIWealth && python -m backtest.run_all5 --sell-first
  cd /home/AIWealth && python backtest/run_all5.py --sell-first
"""
import argparse
import importlib
import inspect
import json
import os
import sys
import time
from dataclasses import asdict
from typing import List

sys.path.insert(0, '/home/AIWealth')

from strategies.base import Strategy
from backtest.data_feed import BacktestDataFeed
from backtest.run_unified import run_unified_backtest

DB_PATH = '/home/AIWealth/data/stocks.db'
JSON_DIR = '/home/AIWealth/frontend/data'
TXT_DIR = '/home/AIWealth/logs/backtest'

# 回测参数
START_DATE = '2021-01-01'
END_DATE = '2026-07-01'
INITIAL_CAPITAL = 1_000_000

STRATEGIES = [
    'big_yang_low_open_v2',
    'firstboard_low_open_dip_v2',
    'gem_star_late_seal',
    'amplitude_reversal',
    'two_board_pullback_dip_h1c',
]


def main():
    parser = argparse.ArgumentParser(description='5策略统一资金池回测')
    parser.add_argument('--sell-first', action='store_true',
                        help='允许卖出当天同Slot立即买入')
    parser.add_argument('--market-filter', action='store_true',
                        help='启用T-1涨跌比+成交额MA20风控过滤')
    args = parser.parse_args()

    sell_day_no_buy = not args.sell_first
    mode_label = "先卖后买（卖出当天可买入）" if args.sell_first else "默认（卖出当天不买入）"
    filter_label = " + T-1风控过滤" if args.market_filter else ""

    print(f"=" * 60)
    print(f"统一资金池5策略回测 | {START_DATE}~{END_DATE}")
    print(f"模式: {mode_label}{filter_label}")
    print(f"初始资金: {INITIAL_CAPITAL:,.0f} | sell_day_no_buy={sell_day_no_buy}")
    if args.market_filter:
        print(f"风控: T-1涨跌比+成交额MA20过滤 已启用")
    print(f"策略: {', '.join(STRATEGIES)}")
    print(f"=" * 60)

    result = run_unified_backtest(
        STRATEGIES, START_DATE, END_DATE,
        initial_capital=INITIAL_CAPITAL,
        market_filter=args.market_filter,
        sell_day_no_buy=sell_day_no_buy,
    )

    summary = result['summary']

    # 输出核心指标
    print(f"\n{'='*60}")
    print(f"{'核心指标汇总':^60}")
    print(f"{'='*60}")
    print(f"模式       : {mode_label}")
    print(f"CAGR       : {summary['cagr_pct']:+.2f}%")
    print(f"MDD        : {summary['max_drawdown_pct']:.2f}%")
    print(f"交易笔数   : {summary['n_trades']}")
    print(f"胜率       : {summary['win_rate_pct']:.2f}%")
    print(f"期末净值   : {summary['final_nav']:,.2f}")
    print(f"总收益率   : {summary['total_return_pct']:+.2f}%")
    print(f"{'='*60}")


if __name__ == '__main__':
    main()
