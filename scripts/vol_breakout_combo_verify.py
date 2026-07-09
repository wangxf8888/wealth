#!/usr/bin/env python3
"""
Task #94 辅助: 放量突破20日新高 精选组合 - 按年度稳健性验证
精选组合(基于2026-04分层alpha):
  市值50-100亿 & 放量2-5x & 换手5-20% & 突破<6% & 涨幅<6%
合规买点: T+1 hour1_open; 卖出: TP15%/SL-7%, 最长持有3日; 兜底持有3日收盘。
单次遍历, 按年度分组统计, 验证是否稳健(避免单月过拟合)。
"""
import sqlite3
import sys
from collections import defaultdict

import strategy_vol_breakout_high as S

TP = 0.15
SL = -0.07
MAXH = 3


def combo_filter(c):
    return (50 <= c['mcap'] < 100
            and 2.0 <= c['vol_ratio'] < 5.0
            and 5 <= c['turn'] < 20
            and c['breakout_pct'] < 6
            and c['day_change'] < 6)


def stat(rets):
    n = len(rets)
    if n == 0:
        return (0, 0.0, 0.0)
    avg = sum(rets) / n
    wr = sum(1 for r in rets if r > 0) / n * 100
    return (n, avg, wr)


def main():
    args = sys.argv[1:]
    start = args[0] if args else '2021-01'
    end = args[1] if len(args) > 1 else '2026-06'

    conn = sqlite3.connect(S.DB_PATH)
    cur = conn.cursor()
    all_days = S.get_all_trading_days(cur)
    all_days_idx = {d: i for i, d in enumerate(all_days)}
    target_days = S.get_days_in_range(cur, start, end)

    print(f"{'='*70}")
    print(f"放量突破20日新高 精选组合 - 按年度稳健性验证 [{start} ~ {end}]")
    print(f"精选: 市值50-100 & 放量2-5x & 换手5-20% & 突破<6% & 涨幅<6%")
    print(f"卖出: TP{TP*100:.0f}%/SL{SL*100:.0f}% 最长持有{MAXH}日")
    print(f"{'='*70}")

    by_year_tpsl = defaultdict(list)   # year -> [ret(TP/SL)]
    by_year_hold3 = defaultdict(list)  # year -> [ret(纯持有3日)]

    for today in target_days:
        if today not in all_days_idx:
            continue
        t_idx = all_days_idx[today]
        year = today[:4]
        cands = [c for c in S.find_candidates(cur, today, all_days, all_days_idx) if combo_filter(c)]
        for c in cands:
            code = c['code']
            fdays = all_days[t_idx:t_idx + 1 + MAXH]
            drows = S.get_hour_rows(cur, code, fdays)
            dmap = {r[0]: r for r in drows}
            t1 = all_days[t_idx + 1] if t_idx + 1 < len(all_days) else None
            if not (t1 and t1 in dmap):
                continue
            buyB = dmap[t1][4]
            if not buyB or buyB <= 0:
                continue
            # 纯持有3日
            th = all_days[t_idx + MAXH] if t_idx + MAXH < len(all_days) else None
            if th and th in dmap and dmap[th][1] is not None:
                by_year_hold3[year].append((dmap[th][1] - buyB) / buyB * 100)
            # TP/SL
            path = S.get_future_path(cur, code, all_days, t_idx, MAXH)
            ret, _ = S.simulate_tp_sl(buyB, path, TP, SL, MAXH)
            if ret is not None:
                by_year_tpsl[year].append(ret)

    print(f"\n--- 按年度 (纯持有3日 vs TP15%/SL-7%) ---")
    print(f"  {'年度':<8}{'样本':<8}{'持有3日均收益':<16}{'胜率':<8}| {'TP/SL均收益':<14}{'胜率':<8}")
    all_h, all_t = [], []
    for year in sorted(set(list(by_year_hold3.keys()) + list(by_year_tpsl.keys()))):
        h = by_year_hold3.get(year, [])
        t = by_year_tpsl.get(year, [])
        all_h += h
        all_t += t
        nh, ah, wh = stat(h)
        nt, at, wt = stat(t)
        print(f"  {year:<8}{nh:<8}{ah:+.2f}%{'':<10}{wh:.1f}%{'':<3}| {at:+.2f}%{'':<8}{wt:.1f}%")

    print(f"  {'-'*66}")
    nh, ah, wh = stat(all_h)
    nt, at, wt = stat(all_t)
    print(f"  {'全周期':<8}{nh:<8}{ah:+.2f}%{'':<10}{wh:.1f}%{'':<3}| {at:+.2f}%{'':<8}{wt:.1f}%")

    cycles = 20.0 / MAXH
    monthly = ((1 + at / 100) ** cycles - 1) * 100
    print(f"\n  全周期TP/SL: 收益{at:+.2f}% 胜率{wt:.1f}% n={nt}")
    print(f"  月化估算(单仓复利,每月{cycles:.1f}轮): {monthly:+.1f}%")
    print(f"  目标月化10%+且胜率55%+ → {'✓ 达标' if monthly >= 10 and wt >= 55 else '✗ 未达标'}")

    conn.close()
    print(f"\n{'='*70}\n验证完成。")


if __name__ == '__main__':
    main()
