#!/usr/bin/env python3
"""Task #93: 缩量放量反弹V2 slot=1 全周期回测验证

策略: VolSurgeReversalV2Strategy
  信号: 前5日跌幅>=10% + 缩量递减 + 放量>=2倍 + 收阳
  买入: T+1 hour1 open;  卖出: 持仓2交易日后 hour4(即T+3 hour4)
  裸持(无止盈止损), slot=1
"""
import sys
import os

sys.path.insert(0, '/home/AIWealth')

from engine.backtest_engine import BacktestEngineV3
from engine.buy_strategy_vol_surge_reversal_v2 import VolSurgeReversalV2Strategy
from engine.sell_module import PerSlotSellStrategy
from engine.blacklist import Blacklist


def main():
    output_path = '/home/AIWealth/scripts/logs/vol_surge_reversal_v2_slot1.log'
    os.makedirs(os.path.dirname(output_path), exist_ok=True)

    engine = BacktestEngineV3(
        buy_module=VolSurgeReversalV2Strategy(buy_hour=1, target_hold_days=2),
        sell_module=PerSlotSellStrategy(sell_hour=4),
        blacklist=Blacklist(),
        db_path='/home/AIWealth/data/stocks.db',
        start_date='2021-01-01',
        end_date='2026-06-30',
        initial_capital=1_000_000,
        n_slots=1,
        output_path=output_path,
    )
    engine.run()

    # ---- 汇总指标(引擎已打印最终统计，此处补充"平均每笔收益") ----
    pf = engine.portfolio
    nav_history = pf.nav_history
    sell_trades = [t for t in pf.all_trades if t['type'] == 'sell']

    final_nav = nav_history[-1]['nav'] if nav_history else pf.initial_capital
    total_return = (final_nav / pf.initial_capital - 1) * 100

    from datetime import datetime
    if nav_history:
        d0 = datetime.strptime(nav_history[0]['date'], '%Y-%m-%d')
        d1 = datetime.strptime(nav_history[-1]['date'], '%Y-%m-%d')
        years = max((d1 - d0).days / 365.25, 1e-9)
        cagr = ((final_nav / pf.initial_capital) ** (1 / years) - 1) * 100
    else:
        cagr = 0.0

    # 最大回撤
    max_dd = 0.0
    peak = pf.initial_capital
    for rec in nav_history:
        peak = max(peak, rec['nav'])
        dd = (peak - rec['nav']) / peak * 100
        max_dd = max(max_dd, dd)

    n = len(sell_trades)
    wins = [t for t in sell_trades if t['pnl_pct'] > 0]
    win_rate = len(wins) / n * 100 if n else 0.0
    avg_pnl = sum(t['pnl_pct'] for t in sell_trades) / n if n else 0.0

    print("\n" + "#" * 60)
    print("# Task #93 缩量放量反弹V2 slot=1 关键指标")
    print("#" * 60)
    print(f"最终净值    : {final_nav:,.2f}  (总收益 {total_return:+.2f}%)")
    print(f"CAGR(年化)  : {cagr:+.2f}%")
    print(f"MaxDD(回撤) : {max_dd:.2f}%")
    print(f"交易笔数    : {n}笔")
    print(f"胜率        : {win_rate:.1f}%")
    print(f"平均每笔收益: {avg_pnl:+.2f}%")
    print("#" * 60)


if __name__ == '__main__':
    main()
