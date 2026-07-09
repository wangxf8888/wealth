#!/usr/bin/env python3
"""Task #100: 竞价高开+hour1量比放大 V2 slot=1 全周期回测验证

策略: AuctionGapUpVolV2Strategy
  信号: 创业板50-200亿 + 昨日非涨停 + 今日高开>=3% + hour1量比>=5x
  买入: T日 hour1 open;  卖出: 持仓1交易日后 hour4(即T+1 hour4)
  裸持(无止盈止损), slot=1

研究基准(strategy_auction_gapup_vol.py 全周期):
  胜率59.3%, 单笔+3.49%, n=985, 6年全正
"""
import sys
import os
from datetime import datetime

sys.path.insert(0, '/home/AIWealth')

from engine.backtest_engine import BacktestEngineV3
from engine.buy_strategy_auction_gapup_v2 import AuctionGapUpVolV2Strategy
from engine.sell_module import PerSlotSellStrategy
from engine.blacklist import Blacklist


def main():
    output_path = '/home/AIWealth/scripts/logs/auction_gapup_v2_slot1.log'
    os.makedirs(os.path.dirname(output_path), exist_ok=True)

    engine = BacktestEngineV3(
        buy_module=AuctionGapUpVolV2Strategy(
            gap_min=3.0, vol_ratio=5.0, target_hold_days=1),
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

    # ---- 汇总指标 ----
    pf = engine.portfolio
    nav_history = pf.nav_history
    sell_trades = [t for t in pf.all_trades if t['type'] == 'sell']

    final_nav = nav_history[-1]['nav'] if nav_history else pf.initial_capital
    total_return = (final_nav / pf.initial_capital - 1) * 100

    if nav_history:
        d0 = datetime.strptime(nav_history[0]['date'], '%Y-%m-%d')
        d1 = datetime.strptime(nav_history[-1]['date'], '%Y-%m-%d')
        years = max((d1 - d0).days / 365.25, 1e-9)
        cagr = ((final_nav / pf.initial_capital) ** (1 / years) - 1) * 100
    else:
        cagr = 0.0

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

    # 逐年胜率/收益分布
    yearly = {}
    for t in sell_trades:
        y = t['sell_date'][:4]
        yearly.setdefault(y, []).append(t['pnl_pct'])

    print("\n" + "#" * 60)
    print("# Task #100 竞价高开+hour1量比 V2 slot=1 关键指标")
    print("#" * 60)
    print(f"最终净值    : {final_nav:,.2f}  (总收益 {total_return:+.2f}%)")
    print(f"CAGR(年化)  : {cagr:+.2f}%")
    print(f"MaxDD(回撤) : {max_dd:.2f}%")
    print(f"交易笔数    : {n}笔")
    print(f"胜率        : {win_rate:.1f}%")
    print(f"平均每笔收益: {avg_pnl:+.2f}%")
    print("-" * 60)
    print("逐年分布(卖出日归类):")
    for y in sorted(yearly):
        rets = yearly[y]
        wr = sum(1 for r in rets if r > 0) / len(rets) * 100
        av = sum(rets) / len(rets)
        print(f"  {y}: {len(rets):>4}笔  胜率{wr:5.1f}%  单笔{av:+.2f}%")
    print("-" * 60)
    print("研究基准对比: n=985  胜率59.3%  单笔+3.49%  (6年全正)")
    print("注: slot=1单仓位受持仓占用制约, 交易笔数必然少于研究全信号数;")
    print("    应重点对比 胜率 与 平均每笔收益 是否接近研究结论。")
    print("#" * 60)


if __name__ == '__main__':
    main()
