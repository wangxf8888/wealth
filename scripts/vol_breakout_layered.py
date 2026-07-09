#!/usr/bin/env python3
"""
Task #94 辅助: 放量突破20日新高策略 - 分层分析
基准(2026-04)未达标(月化7.1%/胜率50.2%)。本脚本对候选按多维度分层,
定位是否存在有 alpha 的子集(合规 T+1 h1买 持有3日)。
复用 strategy_vol_breakout_high.py 的候选筛选逻辑。
"""
import sqlite3
import sys
from collections import defaultdict

import strategy_vol_breakout_high as S


def collect(cur, target_days, all_days, all_days_idx, hold=3):
    """收集每个候选的特征 + 合规T+1 h1买 持有hold日收益"""
    recs = []  # dict: vol_ratio, breakout_pct, mcap, turn, day_change, ret
    for today in target_days:
        if today not in all_days_idx:
            continue
        t_idx = all_days_idx[today]
        cands = S.find_candidates(cur, today, all_days, all_days_idx)
        for c in cands:
            code = c['code']
            fdays = all_days[t_idx:t_idx + 1 + hold]
            drows = S.get_hour_rows(cur, code, fdays)
            dmap = {r[0]: r for r in drows}
            t1 = all_days[t_idx + 1] if t_idx + 1 < len(all_days) else None
            th = all_days[t_idx + hold] if t_idx + hold < len(all_days) else None
            if not (t1 and t1 in dmap and th and th in dmap):
                continue
            buyB = dmap[t1][4]  # T+1 hour1_open
            sell = dmap[th][1]  # T+hold close
            if not buyB or buyB <= 0 or sell is None:
                continue
            ret = (sell - buyB) / buyB * 100
            recs.append({
                'vol_ratio': c['vol_ratio'],
                'breakout_pct': c['breakout_pct'],
                'mcap': c['mcap'],
                'turn': c['turn'],
                'day_change': c['day_change'],
                'ret': ret,
            })
    return recs


def stat(rets):
    n = len(rets)
    if n == 0:
        return (0, 0.0, 0.0)
    avg = sum(rets) / n
    wr = sum(1 for r in rets if r > 0) / n * 100
    return (n, avg, wr)


def layer(recs, key, bins, labels):
    """按 key 分箱统计"""
    groups = defaultdict(list)
    for r in recs:
        v = r[key]
        for i in range(len(bins) - 1):
            if bins[i] <= v < bins[i + 1]:
                groups[labels[i]].append(r['ret'])
                break
    print(f"\n--- 按 {key} 分层 (合规T+1 h1买 持有3日) ---")
    print(f"  {'区间':<16}{'样本':<8}{'平均收益':<12}{'胜率':<10}")
    for lb in labels:
        n, avg, wr = stat(groups.get(lb, []))
        if n == 0:
            continue
        star = '  <== alpha' if (wr >= 55 and avg > 0.5 and n >= 15) else ''
        print(f"  {lb:<16}{n:<8}{avg:+.2f}%{'':<6}{wr:.1f}%{star}")


def combo_filter(c):
    """精选组合(基于分层alpha): 小市值+温和放量+换手适中+突破/涨幅不过强"""
    return (50 <= c['mcap'] < 100
            and 2.0 <= c['vol_ratio'] < 5.0
            and 5 <= c['turn'] < 20
            and c['breakout_pct'] < 6
            and c['day_change'] < 6)


def collect_combo(cur, target_days, all_days, all_days_idx, holds=(1, 2, 3)):
    """精选组合: 各持有期收益 + TP/SL(合规T+1 h1买)"""
    per_hold = {h: [] for h in holds}
    tpsl = defaultdict(list)
    maxh = max(holds)
    for today in target_days:
        if today not in all_days_idx:
            continue
        t_idx = all_days_idx[today]
        cands = [c for c in S.find_candidates(cur, today, all_days, all_days_idx) if combo_filter(c)]
        for c in cands:
            code = c['code']
            fdays = all_days[t_idx:t_idx + 1 + maxh]
            drows = S.get_hour_rows(cur, code, fdays)
            dmap = {r[0]: r for r in drows}
            t1 = all_days[t_idx + 1] if t_idx + 1 < len(all_days) else None
            if not (t1 and t1 in dmap):
                continue
            buyB = dmap[t1][4]
            if not buyB or buyB <= 0:
                continue
            for h in holds:
                th = all_days[t_idx + h] if t_idx + h < len(all_days) else None
                if th and th in dmap and dmap[th][1] is not None:
                    per_hold[h].append((dmap[th][1] - buyB) / buyB * 100)
            path = S.get_future_path(cur, code, all_days, t_idx, maxh)
            for tp in S.TP_GRID:
                for sl in S.SL_GRID:
                    ret, _ = S.simulate_tp_sl(buyB, path, tp, sl, maxh)
                    if ret is not None:
                        tpsl[(tp, sl)].append(ret)
    return per_hold, tpsl


def report_combo(cur, target_days, all_days, all_days_idx):
    print(f"\n{'#'*70}")
    print("精选组合 [市值50-100 & 放量2-5x & 换手5-20% & 突破<6% & 涨幅<6%]")
    print(f"{'#'*70}")
    per_hold, tpsl = collect_combo(cur, target_days, all_days, all_days_idx)
    print(f"\n--- 精选组合 各持有期 (合规T+1 h1买) ---")
    print(f"  {'持有':<8}{'样本':<8}{'平均收益':<12}{'胜率':<10}")
    for h, rets in per_hold.items():
        n, avg, wr = stat(rets)
        if n:
            print(f"  {h}日{'':<6}{n:<8}{avg:+.2f}%{'':<6}{wr:.1f}%")
    print(f"\n--- 精选组合 TP/SL网格 (最长持有3日) ---")
    print(f"  {'止盈/止损':<16}{'样本':<8}{'平均收益':<12}{'胜率':<10}")
    best = None
    for tp in S.TP_GRID:
        for sl in S.SL_GRID:
            rets = tpsl.get((tp, sl), [])
            if not rets:
                continue
            n, avg, wr = stat(rets)
            print(f"  TP{tp*100:.0f}%/SL{sl*100:.0f}%{'':<4}{n:<8}{avg:+.2f}%{'':<6}{wr:.1f}%")
            if best is None or avg > best[1]:
                best = ((tp, sl), avg, wr, n)
    if best:
        (tp, sl), avg, wr, n = best
        cycles = 20.0 / 3
        monthly = ((1 + avg / 100) ** cycles - 1) * 100
        print(f"\n  >>> 最优TP/SL: TP{tp*100:.0f}%/SL{sl*100:.0f}% | 收益{avg:+.2f}% | 胜率{wr:.1f}% | n={n}")
        print(f"  月化估算(单仓复利,每月{cycles:.1f}轮): {monthly:+.1f}%")
        print(f"  目标月化10%+且胜率55%+ → {'✓ 达标' if monthly >= 10 and wr >= 55 else '✗ 未达标'}")


def main():
    args = sys.argv[1:]
    start = args[0] if args else '2026-04'
    end = args[1] if len(args) > 1 else None

    conn = sqlite3.connect(S.DB_PATH)
    cur = conn.cursor()
    all_days = S.get_all_trading_days(cur)
    all_days_idx = {d: i for i, d in enumerate(all_days)}

    if end:
        target_days = S.get_days_in_range(cur, start, end)
        mode = f"{start} ~ {end}"
    else:
        target_days = S.get_trading_days(cur, start)
        mode = f"单月 {start}"

    print(f"{'='*70}")
    print(f"放量突破20日新高 - 分层分析 [{mode}]")
    print(f"{'='*70}")

    recs = collect(cur, target_days, all_days, all_days_idx, hold=3)
    n, avg, wr = stat([r['ret'] for r in recs])
    print(f"总样本: {n} | 整体平均收益: {avg:+.2f}% | 整体胜率: {wr:.1f}%")

    layer(recs, 'vol_ratio', [2.0, 3.0, 5.0, 8.0, 999], ['2-3x', '3-5x', '5-8x', '8x+'])
    layer(recs, 'breakout_pct', [0, 2, 4, 6, 999], ['0-2%', '2-4%', '4-6%', '6%+'])
    layer(recs, 'mcap', [50, 100, 150, 200, 300], ['50-100', '100-150', '150-200', '200-300'])
    layer(recs, 'turn', [0, 5, 10, 20, 999], ['0-5%', '5-10%', '10-20%', '20%+'])
    layer(recs, 'day_change', [-99, 0, 3, 6, 999], ['<0%', '0-3%', '3-6%', '6%+'])

    report_combo(cur, target_days, all_days, all_days_idx)

    conn.close()
    print(f"\n{'='*70}\n分层分析完成。")


if __name__ == '__main__':
    main()
