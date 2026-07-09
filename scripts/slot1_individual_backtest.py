#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Task #87: 4个已实现BuyModule策略分别slot=1(单股满仓)全量回测(2021-2026)

对每个策略单独用n_slots=1跑BacktestEngineV3, 验证单策略能否达到年化100%。
结果写入 /home/AIWealth/scripts/logs/slot1_individual_backtest.log
"""
import sys
import os
import traceback
from datetime import datetime

# 保证可import engine包
sys.path.insert(0, '/home/AIWealth')

from engine.backtest_engine import BacktestEngineV3
from engine.sell_module import PerSlotSellStrategy
from engine.blacklist import Blacklist
from engine.buy_strategy_bigdrop_gapup_v2 import BigDropGapUpV2Strategy
from engine.buy_strategy_limitdown_rebound_v2 import LimitDownReboundV2Strategy
from engine.buy_strategy_shrink_reversal_v2 import ShrinkReversalV2Strategy
from engine.buy_strategy_dragon_pullback_v2 import DragonPullbackV2Strategy

DB_PATH = '/home/AIWealth/data/stocks.db'
START_DATE = '2021-01-01'
END_DATE = '2026-06-30'
INITIAL_CAPITAL = 1_000_000
YEARS = 5.5  # CAGR计算年数
LOG_PATH = '/home/AIWealth/scripts/logs/slot1_individual_backtest.log'

# 日志文件句柄
_log_f = open(LOG_PATH, 'w', encoding='utf-8')


def log(msg=''):
    """同时输出到终端和日志文件"""
    print(msg)
    _log_f.write(str(msg) + '\n')
    _log_f.flush()


def compute_stats(name, portfolio):
    """从portfolio的交易记录和净值曲线计算统计指标"""
    nav_history = portfolio.nav_history
    all_trades = portfolio.all_trades
    sell_trades = [t for t in all_trades if t['type'] == 'sell']

    if not nav_history:
        return {
            'name': name, 'final_nav': INITIAL_CAPITAL, 'total_return': 0.0,
            'cagr': 0.0, 'max_dd': 0.0, 'n_trades': 0, 'win_rate': 0.0,
            'avg_pnl': 0.0, 'annual_freq': 0.0,
        }

    final_nav = nav_history[-1]['nav']
    total_return = (final_nav / INITIAL_CAPITAL - 1) * 100
    cagr = ((final_nav / INITIAL_CAPITAL) ** (1 / YEARS) - 1) * 100

    # 最大回撤
    max_dd = 0.0
    peak = INITIAL_CAPITAL
    for rec in nav_history:
        if rec['nav'] > peak:
            peak = rec['nav']
        dd = (peak - rec['nav']) / peak * 100
        if dd > max_dd:
            max_dd = dd

    # 交易统计
    n_trades = len(sell_trades)
    win_trades = [t for t in sell_trades if t['pnl_pct'] > 0]
    win_rate = len(win_trades) / n_trades * 100 if n_trades > 0 else 0.0
    avg_pnl = sum(t['pnl_pct'] for t in sell_trades) / n_trades if n_trades > 0 else 0.0
    annual_freq = n_trades / YEARS

    return {
        'name': name, 'final_nav': final_nav, 'total_return': total_return,
        'cagr': cagr, 'max_dd': max_dd, 'n_trades': n_trades,
        'win_rate': win_rate, 'avg_pnl': avg_pnl, 'annual_freq': annual_freq,
    }


def run_one(name, strategy):
    """运行单个策略的slot=1回测, 返回统计dict; 报错则返回None"""
    log('\n' + '#' * 70)
    log(f'# 开始回测: {name}')
    log('#' * 70)
    try:
        engine = BacktestEngineV3(
            buy_module=strategy,
            sell_module=PerSlotSellStrategy(sell_hour=4),
            blacklist=Blacklist(),
            db_path=DB_PATH,
            start_date=START_DATE,
            end_date=END_DATE,
            initial_capital=INITIAL_CAPITAL,
            n_slots=1,       # 关键: 单股满仓
            output_path=None,  # 不生成完整日志
        )
        engine.run()
        stats = compute_stats(name, engine.portfolio)

        log('')
        log(f'  策略名          : {stats["name"]}')
        log(f'  最终净值        : {stats["final_nav"]:,.2f}')
        log(f'  总收益率        : {stats["total_return"]:+.2f}%')
        log(f'  CAGR(年化)      : {stats["cagr"]:+.2f}%')
        log(f'  最大回撤        : {stats["max_dd"]:.2f}%')
        log(f'  交易笔数        : {stats["n_trades"]}')
        log(f'  胜率            : {stats["win_rate"]:.1f}%')
        log(f'  平均每笔收益    : {stats["avg_pnl"]:+.2f}%')
        log(f'  年均交易频率    : {stats["annual_freq"]:.1f}笔/年')
        return stats
    except Exception as e:
        log(f'  [错误] 策略 {name} 回测失败: {e}')
        log(traceback.format_exc())
        return None


def main():
    log('=' * 70)
    log('Task #87: 4策略 slot=1(单股满仓) 全量回测验证年化')
    log(f'回测区间: {START_DATE} ~ {END_DATE} | 初始资金: {INITIAL_CAPITAL:,} | CAGR年数: {YEARS}')
    log(f'生成时间: {datetime.now().strftime("%Y-%m-%d %H:%M:%S")}')
    log('=' * 70)

    # 5个配置
    configs = [
        ('BigDropGapUp(drop=-10,hold=2)',
         BigDropGapUpV2Strategy(drop_threshold=-10.0, target_hold_days=2)),
        ('BigDropGapUp(drop=-8,hold=2)',
         BigDropGapUpV2Strategy(drop_threshold=-8.0, target_hold_days=2)),
        ('LimitDownRebound(hold=5)',
         LimitDownReboundV2Strategy(target_hold_days=5)),
        ('ShrinkReversal(hold=5)',
         ShrinkReversalV2Strategy(target_hold_days=5)),
        ('DragonPullback(hold=2)',
         DragonPullbackV2Strategy(target_hold_days=2)),
    ]

    results = []
    for name, strat in configs:
        stats = run_one(name, strat)
        if stats is not None:
            results.append(stats)

    # 汇总对比表
    log('\n\n' + '=' * 110)
    log('========== 汇总对比表 (slot=1 单股满仓) ==========')
    log('=' * 110)
    header = (f'{"策略":<32}{"最终净值":>16}{"总收益%":>12}{"CAGR%":>10}'
              f'{"回撤%":>9}{"笔数":>7}{"胜率%":>8}{"均笔%":>8}{"年频":>7}  达标')
    log(header)
    log('-' * 110)
    for s in results:
        hit = '★年化100%' if s['cagr'] >= 100.0 else ''
        line = (f'{s["name"]:<32}{s["final_nav"]:>16,.0f}{s["total_return"]:>+12.1f}'
                f'{s["cagr"]:>+10.1f}{s["max_dd"]:>9.1f}{s["n_trades"]:>7}'
                f'{s["win_rate"]:>8.1f}{s["avg_pnl"]:>+8.2f}{s["annual_freq"]:>7.1f}  {hit}')
        log(line)
    log('=' * 110)

    # 达标策略清单
    hits = [s for s in results if s['cagr'] >= 100.0]
    if hits:
        log('\n达到年化100%的策略:')
        for s in hits:
            log(f'  ★ {s["name"]}: CAGR={s["cagr"]:+.1f}%')
    else:
        log('\n没有单策略达到年化100%目标。')
        if results:
            best = max(results, key=lambda x: x['cagr'])
            log(f'最佳单策略: {best["name"]} CAGR={best["cagr"]:+.1f}%')

    log(f'\n结果已写入: {LOG_PATH}')
    _log_f.close()


if __name__ == '__main__':
    main()
