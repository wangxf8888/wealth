"""多策略组合回测入口 - 资金隔离模式。

每个策略独立管理 1/N 资金，各自以 slot=1 运行，互不影响。
最终组合净值 = 各策略子账户净值之和。

用法:
  python -m backtest.run_combined --strategies dragon_pullback,limitup_next,first_board \
      --start 2021-01-01 --end 2026-07-01

  python -m backtest.run_combined --strategies dragon_pullback,limitup_next \
      --start 2021-01-01 --end 2026-07-01
"""
import argparse
import importlib
import inspect
import json
import os
import sqlite3
from collections import defaultdict
from dataclasses import asdict
from typing import List

from strategies.base import Strategy
from backtest.data_feed import BacktestDataFeed
from backtest.engine import BacktestEngine
from backtest.portfolio import Portfolio

DB_PATH = '/home/AIWealth/data/stocks.db'
LOG_DIR = '/home/AIWealth/logs/backtest'


def load_strategy(name: str) -> Strategy:
    """从 strategies.<name> 动态加载 Strategy 子类并实例化。"""
    module = importlib.import_module(f'strategies.{name}')
    for _, obj in inspect.getmembers(module, inspect.isclass):
        if issubclass(obj, Strategy) and obj is not Strategy \
                and obj.__module__ == module.__name__:
            return obj()
    raise ValueError(f"在 strategies.{name} 中未找到 Strategy 子类")


def compute_combined_max_drawdown(nav_histories: list) -> float:
    """从多个子策略的 NAV 历史合并计算组合最大回撤。"""
    if not nav_histories or not nav_histories[0]:
        return 0.0

    n_points = len(nav_histories[0])
    peak = None
    max_dd = 0.0

    for i in range(n_points):
        combined_nav = sum(h[i][2] for h in nav_histories)
        if peak is None or combined_nav > peak:
            peak = combined_nav
        if peak > 0:
            dd = (peak - combined_nav) / peak * 100
            if dd > max_dd:
                max_dd = dd

    return max_dd


def run_independent_backtest(strategy_name: str, data_feed, start: str, end: str,
                             capital: float, n_slots: int = 1,
                             market_filter: bool = False,
                             market_filter_threshold: float = -1.0) -> dict:
    """运行单个策略的独立回测，返回结果字典。"""
    strategy = load_strategy(strategy_name)
    engine = BacktestEngine(strategy, data_feed, n_slots=n_slots, initial_capital=capital,
                            market_filter=market_filter,
                            market_filter_threshold=market_filter_threshold)
    summary = engine.run(start, end)
    trades = engine.portfolio.get_all_trades()
    return {
        'strategy_name': strategy_name,
        'summary': summary,
        'trades': trades,
        'nav_history': engine.nav_history,
    }


# ------------------------------------------------------------------
# TXT 标准明细生成（回测标配输出 - 小时级OHLC rate格式）
# ------------------------------------------------------------------
def generate_trade_detail_txt(strategy_name: str, summary: dict, trades: list,
                               output_dir: str = LOG_DIR) -> str:
    """生成标准格式TXT交易明细（小时级OHLC rate + 前后10日）。

    委托 backtest.txt_formatter.TxtFormatter 实现。
    """
    from backtest.txt_formatter import TxtFormatter
    fmt = TxtFormatter(DB_PATH)
    try:
        return fmt.generate(strategy_name, summary, trades, output_dir)
    finally:
        fmt.close()


def print_combined_summary(combined_summary: dict, sub_results: list):
    """打印组合回测结果。"""
    print("\n" + "=" * 70)
    print(f"{'多策略组合回测结果（资金隔离模式）':^70}")
    print("=" * 70)
    cs = combined_summary
    print(f"回测区间     : {cs['start_date']} ~ {cs['end_date']}")
    print(f"策略数量     : {cs['n_strategies']}")
    print(f"总初始资金   : {cs['initial_capital']:,.0f}")
    print(f"各策略分配   : {cs['per_strategy_capital']:,.0f} (1/{cs['n_strategies']})")
    print(f"期末总净值   : {cs['final_nav']:,.2f}")
    print(f"总收益率     : {cs['total_return_pct']:+.2f}%")
    print(f"年化(CAGR)   : {cs['cagr_pct']:+.2f}%")
    print(f"最大回撤     : {cs['max_drawdown_pct']:.2f}%")
    print(f"总交易笔数   : {cs['n_trades']}")
    print(f"综合胜率     : {cs['win_rate_pct']:.2f}%")
    print(f"平均收益     : {cs['avg_profit_pct']:+.2f}%")
    print("=" * 70)

    # 各策略独立表现
    print(f"\n{'各策略独立表现':^70}")
    print("-" * 70)
    print(f"{'策略':<28}{'CAGR%':>8}{'MaxDD%':>8}{'笔数':>6}{'胜率%':>8}{'均收益%':>10}")
    print("-" * 70)
    for r in sub_results:
        s = r['summary']
        name = r['strategy_name']
        print(f"{name:<28}{s['cagr_pct']:>+8.2f}{s['max_drawdown_pct']:>8.2f}"
              f"{s['n_trades']:>6}{s['win_rate_pct']:>8.2f}{s['avg_profit_pct']:>+10.2f}")
    print("-" * 70)


def run_combined_backtest(strategy_names: list, start_date: str, end_date: str,
                          initial_capital: float = 1_000_000, slots: int = 1,
                          fee_rate: float = 0.001,
                          market_filter: bool = False,
                          market_filter_threshold: float = -1.0) -> dict:
    """便捷API：运行多策略组合回测并返回结果字典。

    Args:
        strategy_names: 策略名称列表
        start_date: 起始日期
        end_date: 结束日期
        initial_capital: 总初始资金
        slots: 每策略持仓槽位数
        fee_rate: 手续费率(单边)
        market_filter: 是否启用大盘过滤
        market_filter_threshold: 大盘过滤阈值(%)

    Returns:
        dict: {cagr, total_trades, win_rate, max_drawdown, final_nav, ...}
    """
    data_feed = BacktestDataFeed(DB_PATH)
    n_strategies = len(strategy_names)
    per_cap = initial_capital / n_strategies

    sub_results = []
    for name in strategy_names:
        result = run_independent_backtest(
            name, data_feed, start_date, end_date, per_cap,
            n_slots=slots,
            market_filter=market_filter,
            market_filter_threshold=market_filter_threshold)
        sub_results.append(result)

    # 合并计算
    total_final_nav = sum(r['summary']['final_nav'] for r in sub_results)
    total_return_pct = (total_final_nav / initial_capital - 1) * 100

    nav_hist_0 = sub_results[0]['nav_history']
    n_hours = len(nav_hist_0)
    years = (n_hours / 4) / 244 if n_hours else 0.0

    if years > 0 and total_final_nav > 0:
        cagr = ((total_final_nav / initial_capital) ** (1 / years) - 1) * 100
    else:
        cagr = 0.0

    nav_histories = [r['nav_history'] for r in sub_results]
    max_dd = compute_combined_max_drawdown(nav_histories)

    all_trades = []
    for r in sub_results:
        all_trades.extend(r['trades'])
    n_trades = len(all_trades)
    wins = sum(1 for t in all_trades if t.profit_pct > 0)
    win_rate = (wins / n_trades * 100) if n_trades else 0.0

    data_feed.close()

    # 标配TXT明细输出（每个策略独立一份）
    for r in sub_results:
        generate_trade_detail_txt(
            r['strategy_name'], r['summary'], r['trades'])

    # JSON输出到frontend/data/（供前端看板使用）
    frontend_dir = '/home/AIWealth/frontend/data'
    os.makedirs(frontend_dir, exist_ok=True)
    for r in sub_results:
        json_path = os.path.join(frontend_dir, f'{r["strategy_name"]}_trades.json')
        payload = {
            'summary': r['summary'],
            'trades': [asdict(t) for t in r['trades']],
        }
        with open(json_path, 'w', encoding='utf-8') as f:
            json.dump(payload, f, ensure_ascii=False, indent=2)

    return {
        'cagr': round(cagr, 2),
        'total_trades': n_trades,
        'win_rate': round(win_rate, 2),
        'max_drawdown': round(max_dd, 2),
        'final_nav': round(total_final_nav, 2),
        'total_return_pct': round(total_return_pct, 2),
        'sub_results': [r['summary'] for r in sub_results],
    }


def main():
    parser = argparse.ArgumentParser(description='多策略组合回测（资金隔离模式）')
    parser.add_argument('--strategies', required=True,
                        help='逗号分隔的策略名, 如 dragon_pullback,limitup_next,first_board')
    parser.add_argument('--start', required=True, help='起始日期 YYYY-MM-DD')
    parser.add_argument('--end', required=True, help='结束日期 YYYY-MM-DD')
    parser.add_argument('--capital', type=float, default=1_000_000, help='总初始资金')
    parser.add_argument('--db', default=DB_PATH, help='数据库路径')
    parser.add_argument('--market-filter', dest='market_filter', action='store_true',
                        default=False, help='启用大盘过滤(前日跌>阈值则跳过买入)')
    parser.add_argument('--no-market-filter', dest='market_filter', action='store_false',
                        help='禁用大盘过滤(默认)')
    parser.add_argument('--market-filter-threshold', type=float, default=-1.0,
                        help='大盘过滤阈值(%%), 默认-1.0(前日跌超1%%则跳过)')
    parser.add_argument('--weights', type=str, default=None,
                        help='各策略资金权重(逗号分隔), 如 0.375,0.25,0.375; 不指定则等权')
    parser.add_argument('--slots', type=int, default=1,
                        help='每个策略的持仓槽位数, 默认1')
    args = parser.parse_args()

    strategy_names = [s.strip() for s in args.strategies.split(',')]
    n_strategies = len(strategy_names)

    # 解析权重
    if args.weights:
        weights = [float(w.strip()) for w in args.weights.split(',')]
        if len(weights) != n_strategies:
            raise ValueError(f"权重数量({len(weights)})与策略数量({n_strategies})不匹配")
        weight_sum = sum(weights)
        if abs(weight_sum - 1.0) > 0.01:
            raise ValueError(f"权重之和({weight_sum:.4f})应为1.0")
        per_strategy_capitals = [args.capital * w for w in weights]
    else:
        weights = [1.0 / n_strategies] * n_strategies
        per_strategy_capitals = [args.capital / n_strategies] * n_strategies
    per_strategy_capital = args.capital / n_strategies  # 向后兼容打印

    print(f"组合模式: 资金隔离 | 策略: {', '.join(strategy_names)}")
    n_slots = args.slots
    if args.weights:
        weight_str = ', '.join(f'{w:.3f}' for w in weights)
        print(f"总资金: {args.capital:,.0f} | 权重: [{weight_str}] | 每策略slot={n_slots}")
        for i, name in enumerate(strategy_names):
            print(f"  {name}: {per_strategy_capitals[i]:,.0f} ({weights[i]*100:.1f}%)")
    else:
        print(f"总资金: {args.capital:,.0f} | 每策略: {per_strategy_capital:,.0f} | 各slot={n_slots}")
    if args.market_filter:
        print(f"大盘过滤: 开启 (前日跌>{abs(args.market_filter_threshold):.1f}%则跳过买入)")
    else:
        print(f"大盘过滤: 关闭")
    print()

    # 共享 data_feed（只读，线程安全）
    data_feed = BacktestDataFeed(args.db)

    # 逐策略独立回测
    sub_results = []
    for i, name in enumerate(strategy_names):
        cap = per_strategy_capitals[i]
        print(f"--- 运行策略: {name} (资金={cap:,.0f}, 权重={weights[i]*100:.1f}%, slot={n_slots}) ---")
        result = run_independent_backtest(name, data_feed, args.start, args.end,
                                          cap, n_slots=n_slots,
                                          market_filter=args.market_filter,
                                          market_filter_threshold=args.market_filter_threshold)
        sub_results.append(result)
        s = result['summary']
        print(f"    => CAGR={s['cagr_pct']:+.2f}%, 笔数={s['n_trades']}, "
              f"MaxDD={s['max_drawdown_pct']:.2f}%\n")

    # 合并计算组合指标
    total_final_nav = sum(r['summary']['final_nav'] for r in sub_results)
    total_return_pct = (total_final_nav / args.capital - 1) * 100

    # 年数：取第一个策略的 nav_history 长度推算
    nav_hist_0 = sub_results[0]['nav_history']
    n_hours = len(nav_hist_0)
    years = (n_hours / 4) / 244 if n_hours else 0.0

    if years > 0 and total_final_nav > 0:
        cagr = ((total_final_nav / args.capital) ** (1 / years) - 1) * 100
    else:
        cagr = 0.0

    # 组合最大回撤：基于合并 NAV 时序
    nav_histories = [r['nav_history'] for r in sub_results]
    max_dd = compute_combined_max_drawdown(nav_histories)

    # 综合交易统计
    all_trades = []
    for r in sub_results:
        all_trades.extend(r['trades'])
    n_trades = len(all_trades)
    wins = sum(1 for t in all_trades if t.profit_pct > 0)
    win_rate = (wins / n_trades * 100) if n_trades else 0.0
    avg_profit = (sum(t.profit_pct for t in all_trades) / n_trades) if n_trades else 0.0

    combined_summary = {
        'start_date': args.start,
        'end_date': args.end,
        'n_strategies': n_strategies,
        'initial_capital': args.capital,
        'per_strategy_capital': per_strategy_capital,
        'final_nav': round(total_final_nav, 2),
        'total_return_pct': round(total_return_pct, 2),
        'cagr_pct': round(cagr, 2),
        'max_drawdown_pct': round(max_dd, 2),
        'n_trades': n_trades,
        'win_rate_pct': round(win_rate, 2),
        'avg_profit_pct': round(avg_profit, 2),
    }

    print_combined_summary(combined_summary, sub_results)

    # 保存交易明细
    os.makedirs(LOG_DIR, exist_ok=True)
    strat_tag = '_'.join(strategy_names)
    path = os.path.join(LOG_DIR, f'combined_{strat_tag}_isolated_trades.json')
    payload = {
        'combined_summary': combined_summary,
        'strategies': strategy_names,
        'sub_summaries': [r['summary'] for r in sub_results],
        'trades': [asdict(t) for t in all_trades],
    }
    with open(path, 'w', encoding='utf-8') as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)
    print(f"\n交易明细已保存: {path} ({n_trades} 笔)")

    # 标配TXT明细输出（每个策略独立一份）
    for r in sub_results:
        generate_trade_detail_txt(
            r['strategy_name'], r['summary'], r['trades'])

    data_feed.close()


if __name__ == '__main__':
    main()
