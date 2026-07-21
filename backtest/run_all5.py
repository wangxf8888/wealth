"""5策略逐个回测运行器 - 输出JSON+TXT明细。

用法:
  cd /home/AIWealth && python -m backtest.run_all5
"""
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
from backtest.engine import BacktestEngine

DB_PATH = '/home/AIWealth/data/stocks.db'
JSON_DIR = '/home/AIWealth/frontend/data'
TXT_DIR = '/home/AIWealth/logs/backtest'

# 回测参数
START_DATE = '2021-01-01'
END_DATE = '2026-07-01'
INITIAL_CAPITAL = 1_000_000
N_SLOTS = 1
FEE_RATE = 0.001

STRATEGIES = [
    'two_yang_one_yin',
    'big_yang_low_open',
    'gem_star_late_seal',
    'amplitude_reversal',
    'limitup_early_seal',
]


def load_strategy(name: str) -> Strategy:
    module = importlib.import_module(f'strategies.{name}')
    for _, obj in inspect.getmembers(module, inspect.isclass):
        if issubclass(obj, Strategy) and obj is not Strategy \
                and obj.__module__ == module.__name__:
            return obj()
    raise ValueError(f"在 strategies.{name} 中未找到 Strategy 子类")


def save_json(strategy_name: str, summary: dict, trades: list):
    """保存JSON交易明细到 frontend/data/{name}_trades.json"""
    os.makedirs(JSON_DIR, exist_ok=True)
    path = os.path.join(JSON_DIR, f'{strategy_name}_trades.json')
    trade_dicts = [asdict(t) for t in trades]
    payload = {
        'strategy': strategy_name,
        'summary': summary,
        'trades': trade_dicts,
    }
    with open(path, 'w', encoding='utf-8') as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)
    print(f"  JSON已保存: {path} ({len(trades)}笔)")


def save_txt(strategy_name: str, summary: dict, trades: list):
    """生成人类可读TXT明细到 logs/backtest/{name}_detail.txt"""
    os.makedirs(TXT_DIR, exist_ok=True)
    path = os.path.join(TXT_DIR, f'{strategy_name}_detail.txt')

    lines = []
    # Header summary
    lines.append("=" * 80)
    lines.append(f"策略: {strategy_name}")
    lines.append(f"区间: {summary['start_date']} ~ {summary['end_date']}")
    lines.append(f"CAGR: {summary['cagr_pct']:+.2f}%")
    lines.append(f"笔数: {summary['n_trades']}")
    lines.append(f"胜率: {summary['win_rate_pct']:.2f}%")
    lines.append(f"MDD:  {summary['max_drawdown_pct']:.2f}%")
    lines.append(f"期末净值: {summary['final_nav']:,.2f}")
    lines.append(f"总收益率: {summary['total_return_pct']:+.2f}%")
    lines.append(f"初始资金: {summary['initial_capital']:,.0f}")
    lines.append("=" * 80)
    lines.append("")

    # Column header
    hdr = (f"{'日期':<12}{'代码':<12}{'买入价':>10}{'卖出价':>10}"
           f"{'收益率%':>10}{'持有h':>6}{'卖出原因':<20}")
    lines.append(hdr)
    lines.append("-" * 80)

    # Trades
    for t in trades:
        td = asdict(t)
        line = (f"{td['buy_date']:<12}{td['code']:<12}"
                f"{td['buy_price']:>10.3f}{td['sell_price']:>10.3f}"
                f"{td['profit_pct']:>+10.2f}{td['hold_hours']:>6}"
                f"  {td['reason']:<20}")
        lines.append(line)

    lines.append("-" * 80)
    lines.append(f"合计: {len(trades)}笔 | 胜率: {summary['win_rate_pct']:.2f}%")

    with open(path, 'w', encoding='utf-8') as f:
        f.write('\n'.join(lines) + '\n')
    print(f"  TXT已保存: {path}")


def check_t1_compliance(trades: list) -> int:
    """检查是否有buy_date==sell_date的违规交易。"""
    violations = 0
    for t in trades:
        td = asdict(t)
        if td['buy_date'] == td['sell_date']:
            violations += 1
    return violations


def run_single(strategy_name: str, data_feed) -> dict:
    """运行单策略回测，返回summary。"""
    print(f"\n{'='*60}")
    print(f"运行策略: {strategy_name} | slot={N_SLOTS} | {START_DATE}~{END_DATE}")
    print(f"{'='*60}")

    strategy = load_strategy(strategy_name)
    engine = BacktestEngine(strategy, data_feed, n_slots=N_SLOTS,
                            initial_capital=INITIAL_CAPITAL)
    summary = engine.run(START_DATE, END_DATE)
    trades = engine.portfolio.get_all_trades()

    # Print summary
    engine.print_summary(summary)

    # T+1 check
    violations = check_t1_compliance(trades)
    if violations > 0:
        print(f"  ⚠️ T+1违规: {violations}笔 buy_date==sell_date!")
    else:
        print(f"  ✓ T+1合规: 0笔违规")

    # Save outputs
    save_json(strategy_name, summary, trades)
    save_txt(strategy_name, summary, trades)

    return summary


def main():
    print(f"=" * 60)
    print(f"5策略完整回测 | {START_DATE}~{END_DATE} | slot={N_SLOTS}")
    print(f"初始资金: {INITIAL_CAPITAL:,.0f} | 手续费: {FEE_RATE}")
    print(f"=" * 60)

    data_feed = BacktestDataFeed(DB_PATH)
    results = {}

    for name in STRATEGIES:
        t0 = time.time()
        try:
            summary = run_single(name, data_feed)
            results[name] = summary
        except Exception as e:
            print(f"  ❌ 策略 {name} 运行失败: {e}")
            import traceback
            traceback.print_exc()
            results[name] = None
        elapsed = time.time() - t0
        print(f"  耗时: {elapsed:.1f}秒")

    data_feed.close()

    # Final comparison table
    print(f"\n\n{'='*80}")
    print(f"{'5策略对比汇总':^80}")
    print(f"{'='*80}")
    print(f"{'策略名':<24}{'CAGR%':>8}{'笔数':>6}{'胜率%':>8}{'MDD%':>8}{'期末净值':>14}")
    print(f"{'-'*80}")
    for name in STRATEGIES:
        s = results.get(name)
        if s:
            print(f"{name:<24}{s['cagr_pct']:>+8.2f}{s['n_trades']:>6}"
                  f"{s['win_rate_pct']:>8.2f}{s['max_drawdown_pct']:>8.2f}"
                  f"{s['final_nav']:>14,.2f}")
        else:
            print(f"{name:<24}{'FAILED':>8}")
    print(f"{'-'*80}")


if __name__ == '__main__':
    main()
