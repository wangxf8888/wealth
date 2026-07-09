#!/usr/bin/env python3
"""Task #20 QA - 跳空止损 + Trailing顺序 双BUG影响评估
对比原策略 vs 修复版本, 量化BUG对收益的虚高影响.
"""
import sqlite3
import sys
import os
import copy
from collections import defaultdict
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import strategy_combo_p0p1_concentrated as S


def check_exit_today_FIXED(by_code, pos, today, today_idx):
    """修复版本:
    BUG#1修复: trailing先用旧peak判定再更新peak
    BUG#2修复: 跳空止损/trailing成交价用 min(触发价, hour_open)
    """
    code = pos.code
    row = by_code.get(code, {}).get(today)
    if not row:
        return None, None

    days_held = today_idx - pos.buy_idx
    is_last_day = (days_held >= pos.hold_max)

    sl_pct = pos.sl_pct / 100.0
    trail_pct = pos.trail_pct / 100.0
    buy_price = pos.buy_price
    stop_price = buy_price * (1 - sl_pct)
    peak = pos.peak

    for h in (1, 2, 3, 4):
        ho, hh, hl, hc = S.get_hour_ohlc(row, h)
        if hh is None or hl is None:
            continue
        if is_last_day and h == 4:
            sell_p = ho if ho else hc
            if sell_p:
                pos.peak = max(peak, hh) if hh else peak
                return sell_p, 'force_close'
            continue

        # BUG#2 修复: 止损价用 min(stop_price, ho) 处理跳空
        if hl <= stop_price:
            pos.peak = peak
            fill = stop_price
            if ho is not None and ho < stop_price:
                fill = ho  # 开盘已跳空, 实际只能开盘价成交
            return fill, 'stop_loss'

        # BUG#1 修复: 先用旧peak算trailing价并判定
        if peak > buy_price:
            trailing_price = peak * (1 - trail_pct)
            if hl <= trailing_price:
                pos.peak = peak
                fill = trailing_price
                if ho is not None and ho < trailing_price:
                    fill = ho
                return fill, 'trailing'

        # 然后再更新peak (本bar high只对后续bar有效)
        if hh > peak:
            peak = hh

    pos.peak = peak

    if is_last_day:
        for h in (4, 3, 2, 1):
            ho, _, _, hc = S.get_hour_ohlc(row, h)
            p = ho or hc
            if p:
                return p, 'force_close'
    return None, None


def run_modified(by_code, all_dates, date_idx, mode, fixed=False):
    """复用 S.run_combo_simulation 但临时替换 check_exit_today."""
    if fixed:
        original = S.check_exit_today
        S.check_exit_today = check_exit_today_FIXED
    try:
        trades, daily_eq, final_cap = S.run_combo_simulation(
            by_code, all_dates, date_idx, mode
        )
    finally:
        if fixed:
            S.check_exit_today = original
    return trades, daily_eq, final_cap


def stats(trades, final_cap):
    n = len(trades)
    wins = sum(1 for t in trades if t['pnl_pct'] > 0)
    avg = sum(t['pnl_pct'] for t in trades) / n if n else 0
    cagr = ((final_cap / S.INITIAL_CAPITAL) ** (1.0 / 6) - 1) * 100.0
    reasons = defaultdict(int)
    for t in trades:
        reasons[t['reason']] += 1
    return n, wins / n * 100 if n else 0, avg, cagr, dict(reasons)


def main():
    print('=' * 100)
    print('Task #20 QA - 双BUG影响评估: 原版 vs 修复版')
    print('=' * 100)

    print('\n[加载数据...]')
    conn = sqlite3.connect(S.DB_PATH)
    all_dates, date_idx, by_code = S.load_data(conn)
    conn.close()

    for mode in ('DYN',):
        print(f'\n{"="*100}\n>>> 模式 {mode}\n{"="*100}')

        print(f'\n[原版: 含BUG]')
        t0 = datetime.now()
        trades_orig, eq_orig, cap_orig = run_modified(
            by_code, all_dates, date_idx, mode, fixed=False
        )
        n0, wr0, avg0, cagr0, r0 = stats(trades_orig, cap_orig)
        print(f'  trades={n0} 胜率={wr0:.1f}% 均收益={avg0:+.2f}% CAGR={cagr0:+.2f}% 最终={cap_orig/10000:.0f}万 耗时={(datetime.now()-t0).total_seconds():.0f}s')
        print(f'  退出: {r0}')

        print(f'\n[修复版: BUG#1+#2 已修]')
        t1 = datetime.now()
        trades_fix, eq_fix, cap_fix = run_modified(
            by_code, all_dates, date_idx, mode, fixed=True
        )
        n1, wr1, avg1, cagr1, r1 = stats(trades_fix, cap_fix)
        print(f'  trades={n1} 胜率={wr1:.1f}% 均收益={avg1:+.2f}% CAGR={cagr1:+.2f}% 最终={cap_fix/10000:.0f}万 耗时={(datetime.now()-t1).total_seconds():.0f}s')
        print(f'  退出: {r1}')

        print(f'\n[BUG影响]')
        print(f'  CAGR 缩水:   {cagr0:+.2f}% → {cagr1:+.2f}%   (差 {cagr1-cagr0:+.2f}pp)')
        print(f'  最终资产:    {cap_orig/10000:,.0f}万 → {cap_fix/10000:,.0f}万   (×{cap_fix/cap_orig:.4f})')
        print(f'  均收益:      {avg0:+.2f}% → {avg1:+.2f}%   (差 {avg1-avg0:+.2f}pp)')
        print(f'  胜率:        {wr0:.1f}% → {wr1:.1f}%')


if __name__ == '__main__':
    main()
