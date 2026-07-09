#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
换手率突变策略研究 (Task #77, rule2 流程)

核心思路：换手率从【极度冷清】突然【活跃】的转变，而非简单的"放大N倍"。
4 种变体：
  A 极低换手突增: 前5日均turn<0.5%, yesterday>=均×3, yesterday收阳, today高开>=1%,
                  板块=创业板+科创板, 流通市值<200亿
  B 低迷后放量突破: 前10日均turn<1.0%, 前10日均振幅<3%, yesterday>=均×4, today open>昨high
  C 换手阶梯放大:   T-3<1%, T-2>=T-3×1.5, T-1>=T-2×1.5, today高开>=1%
  D 极端换手突变:   前5日均turn<0.3%, yesterday>=2%

T+0 合规：信号仅由 yesterday(T-1) 及之前确认；today 仅用 open/open_rate(9:25) 与 hour1_open(9:30)。
两种买入: today h1_open(激进) / T+1 h1_open(保守)。

用法: python3 research_turnover_spike_r2.py 2024-09 | all
输出: stdout + /home/AIWealth/scripts/logs/turnover_spike_r2.log
"""
import sys
import os
import sqlite3
from collections import defaultdict

DB_PATH = '/home/AIWealth/data/stocks.db'
LOG_PATH = '/home/AIWealth/scripts/logs/turnover_spike_r2.log'

A_PREV5_MEAN_MAX = 0.5
A_SURGE_MULT = 3.0
A_TODAY_GAP_MIN = 1.0
A_MKTCAP_MAX_YI = 200.0
B_PREV10_MEAN_MAX = 1.0
B_PREV10_AMP_MAX = 3.0
B_SURGE_MULT = 4.0
C_T3_MAX = 1.0
C_STEP_MULT = 1.5
D_PREV5_MEAN_MAX = 0.3
D_YESTERDAY_MIN = 2.0
TP_PCT = 5.0
HOLD_MAX = 5
START_YEAR = 2021
END_YEAR = 2026
VARIANTS = ['A', 'B', 'C', 'D']
TIMINGS = ['today', 't1']

# 窗口列: 0date 1code 2name 3open 4open_rate 5high 6low 7close 8close_rate
#         9preclose 10turn 11amount 12isST 13hour1_open
WCOLS = ("date,code,code_name,open,open_rate,high,low,close,close_rate,"
         "preclose,turn,amount,isST,hour1_open")

_log_fh = None


def out(msg=""):
    print(msg)
    if _log_fh is not None:
        _log_fh.write(str(msg) + "\n")


def get_limit_ratio(code):
    if code.startswith('sz.300') or code.startswith('sz.301'):
        return 0.20
    if code.startswith('sh.688'):
        return 0.20
    if code.startswith('bj.'):
        return 0.30
    return 0.10


def is_yizi_limit(o, h, l, c, preclose, code):
    if any(v is None for v in (o, h, l, c, preclose)) or preclose <= 0:
        return False
    if o == h == l == c:
        lu = round(preclose * (1 + get_limit_ratio(code)), 2)
        ld = round(preclose * (1 - get_limit_ratio(code)), 2)
        if c >= lu or c <= ld:
            return True
    return False


def is_gem_or_star(code):
    return (code.startswith('sz.300') or code.startswith('sz.301')
            or code.startswith('sh.688'))


def get_all_trading_days(cur):
    cur.execute("SELECT DISTINCT date FROM stock_kline ORDER BY date")
    return [r[0] for r in cur.fetchall()]


def load_window(cur, dates):
    if not dates:
        return {}
    ph = ','.join(['?'] * len(dates))
    cur.execute(f"SELECT {WCOLS} FROM stock_kline "
                f"WHERE date IN ({ph}) AND preclose > 0", dates)
    data = defaultdict(dict)
    for row in cur.fetchall():
        data[row[1]][row[0]] = row
    return data


def mean(xs):
    return sum(xs) / len(xs) if xs else 0.0


def amplitude(row):
    h, l, pc = row[5], row[6], row[9]
    if None in (h, l, pc) or pc <= 0:
        return None
    return (h - l) / pc * 100.0


def check_variant_A(code, name, hist_dates, wdata, today_row):
    if not is_gem_or_star(code):
        return None
    yrow = wdata.get(hist_dates[-1])
    if yrow is None:
        return None
    prev5 = [wdata[d] for d in hist_dates[-6:-1] if d in wdata]
    if len(prev5) < 5:
        return None
    turns5 = [r[10] for r in prev5 if r[10] is not None]
    if len(turns5) < 5:
        return None
    m5 = mean(turns5)
    if m5 <= 0 or m5 >= A_PREV5_MEAN_MAX:
        return None
    yd_turn = yrow[10]
    if yd_turn is None or yd_turn < m5 * A_SURGE_MULT:
        return None
    if yrow[7] is None or yrow[3] is None or yrow[7] <= yrow[3]:
        return None
    if is_yizi_limit(yrow[3], yrow[5], yrow[6], yrow[7], yrow[9], code):
        return None
    if today_row[4] is None or today_row[4] < A_TODAY_GAP_MIN:
        return None
    if yrow[11] and yd_turn:
        mktcap_yi = yrow[11] * 100.0 / yd_turn / 1e8
        if mktcap_yi >= A_MKTCAP_MAX_YI:
            return None
    else:
        return None
    return {'surge': yd_turn / m5, 'yd_turn': yd_turn, 'base': m5,
            'mktcap': mktcap_yi}


def check_variant_B(code, name, hist_dates, wdata, today_row):
    yrow = wdata.get(hist_dates[-1])
    if yrow is None:
        return None
    prev10 = [wdata[d] for d in hist_dates[-11:-1] if d in wdata]
    if len(prev10) < 10:
        return None
    turns10 = [r[10] for r in prev10 if r[10] is not None]
    amps10 = [a for a in (amplitude(r) for r in prev10) if a is not None]
    if len(turns10) < 10 or len(amps10) < 10:
        return None
    m10 = mean(turns10)
    if m10 <= 0 or m10 >= B_PREV10_MEAN_MAX:
        return None
    if mean(amps10) >= B_PREV10_AMP_MAX:
        return None
    yd_turn = yrow[10]
    if yd_turn is None or yd_turn < m10 * B_SURGE_MULT:
        return None
    if is_yizi_limit(yrow[3], yrow[5], yrow[6], yrow[7], yrow[9], code):
        return None
    if today_row[3] is None or yrow[5] is None or today_row[3] <= yrow[5]:
        return None
    return {'surge': yd_turn / m10, 'yd_turn': yd_turn, 'base': m10,
            'amp10': mean(amps10)}


def check_variant_C(code, name, hist_dates, wdata, today_row):
    if len(hist_dates) < 3:
        return None
    r1 = wdata.get(hist_dates[-1])
    r2 = wdata.get(hist_dates[-2])
    r3 = wdata.get(hist_dates[-3])
    if None in (r1, r2, r3):
        return None
    t1, t2, t3 = r1[10], r2[10], r3[10]
    if None in (t1, t2, t3) or t3 <= 0:
        return None
    if t3 >= C_T3_MAX:
        return None
    if t2 < t3 * C_STEP_MULT or t1 < t2 * C_STEP_MULT:
        return None
    if is_yizi_limit(r1[3], r1[5], r1[6], r1[7], r1[9], code):
        return None
    if today_row[4] is None or today_row[4] < A_TODAY_GAP_MIN:
        return None
    return {'surge': t1 / t3, 'yd_turn': t1, 'base': t3, 't2': t2}


def check_variant_D(code, name, hist_dates, wdata, today_row):
    yrow = wdata.get(hist_dates[-1])
    if yrow is None:
        return None
    prev5 = [wdata[d] for d in hist_dates[-6:-1] if d in wdata]
    if len(prev5) < 5:
        return None
    turns5 = [r[10] for r in prev5 if r[10] is not None]
    if len(turns5) < 5:
        return None
    m5 = mean(turns5)
    if m5 <= 0 or m5 >= D_PREV5_MEAN_MAX:
        return None
    yd_turn = yrow[10]
    if yd_turn is None or yd_turn < D_YESTERDAY_MIN:
        return None
    if is_yizi_limit(yrow[3], yrow[5], yrow[6], yrow[7], yrow[9], code):
        return None
    return {'surge': yd_turn / m5, 'yd_turn': yd_turn, 'base': m5}


CHECKERS = {'A': check_variant_A, 'B': check_variant_B,
            'C': check_variant_C, 'D': check_variant_D}


def find_candidates(cur, all_days, i):
    today = all_days[i]
    hist_dates = all_days[max(0, i - 11):i]
    if len(hist_dates) < 5:
        return {v: [] for v in VARIANTS}
    wdata = load_window(cur, hist_dates + [today])
    result = {v: [] for v in VARIANTS}
    for code, dmap in wdata.items():
        today_row = dmap.get(today)
        if today_row is None or today_row[12]:
            continue
        name = today_row[2]
        if name and 'ST' in name.upper():
            continue
        if today_row[13] is None or today_row[13] <= 0:
            continue
        for v in VARIANTS:
            info = CHECKERS[v](code, name, hist_dates, dmap, today_row)
            if info is not None:
                info.update({'code': code, 'name': name,
                             'today_h1_open': today_row[13],
                             'today_open': today_row[3],
                             'today_open_rate': today_row[4]})
                result[v].append(info)
    return result


def fetch_forward(cur, code, dates):
    if not dates:
        return {}
    ph = ','.join(['?'] * len(dates))
    cur.execute(f"SELECT date,open,high,low,close,hour1_open FROM stock_kline "
                f"WHERE code = ? AND date IN ({ph})", [code] + dates)
    return {r[0]: r for r in cur.fetchall()}


def eval_trade(cur, code, all_days, i, timing):
    buy_idx = i if timing == 'today' else i + 1
    if buy_idx + HOLD_MAX >= len(all_days):
        return None
    need_dates = all_days[buy_idx: buy_idx + HOLD_MAX + 1]
    fmap = fetch_forward(cur, code, need_dates)
    brow = fmap.get(all_days[buy_idx])
    if brow is None or brow[5] is None or brow[5] <= 0:
        return None
    P = brow[5]
    rets = {}
    tp_hit = False
    max_dd = 0.0
    for n in range(1, HOLD_MAX + 1):
        row = fmap.get(all_days[buy_idx + n])
        if row is None:
            continue
        if row[4] is not None:
            rets[n] = (row[4] - P) / P * 100.0
        if row[2] is not None and row[2] >= P * (1 + TP_PCT / 100.0):
            tp_hit = True
        if row[3] is not None:
            dd = (row[3] - P) / P * 100.0
            if dd < max_dd:
                max_dd = dd
    if not rets:
        return None
    return {'buy': P, 'rets': rets, 'tp_hit': tp_hit, 'max_dd': max_dd}


def print_hour_detail(cur, code, name, all_days, i):
    lo = max(0, i - 5)
    hi = min(len(all_days), i + 6)
    dates = all_days[lo:hi]
    ph = ','.join(['?'] * len(dates))
    cur.execute(f"SELECT date,open,close,high,low,turn,open_rate,close_rate,"
                f"hour1_open,hour1_close,hour2_open,hour2_close,hour3_open,"
                f"hour3_close,hour4_open,hour4_close FROM stock_kline "
                f"WHERE code = ? AND date IN ({ph}) ORDER BY date",
                [code] + dates)
    rows = cur.fetchall()
    today = all_days[i]
    out(f"    {code} {name}  (前5+当日+后5 hour级OHLC)")
    out(f"    {'日期':<12}{'标记':<7}{'turn%':>7}{'开幅%':>7}{'收幅%':>7}"
        f"   {'h1 o/c':>16}{'h2 o/c':>16}{'h3 o/c':>16}{'h4 o/c':>16}")
    for r in rows:
        (d, o, c, h, l, tn, orr, crr, h1o, h1c, h2o, h2c,
         h3o, h3c, h4o, h4c) = r
        mark = 'TODAY' if d == today else ''

        def fmt(a, b):
            if a is None or b is None:
                return f"{'--':>16}"
            return f"{a:>7.2f}/{b:<8.2f}"
        out(f"    {d:<12}{mark:<7}{(tn or 0):>7.2f}{(orr or 0):>7.2f}"
            f"{(crr or 0):>7.2f}   {fmt(h1o, h1c)}{fmt(h2o, h2c)}"
            f"{fmt(h3o, h3c)}{fmt(h4o, h4c)}")
    out("")


def new_stat():
    return {'signals': 0,
            'ret_by_day': {n: [] for n in range(1, HOLD_MAX + 1)},
            'tp_hit': 0, 'evaluable': 0, 'max_dd': [],
            'by_year': defaultdict(lambda: {'n': 0, 'ret1': [], 'ret2': []})}


def accumulate(stat, year, trade):
    stat['evaluable'] += 1
    for n, r in trade['rets'].items():
        stat['ret_by_day'][n].append(r)
    if trade['tp_hit']:
        stat['tp_hit'] += 1
    stat['max_dd'].append(trade['max_dd'])
    yr = stat['by_year'][year]
    yr['n'] += 1
    if 1 in trade['rets']:
        yr['ret1'].append(trade['rets'][1])
    if 2 in trade['rets']:
        yr['ret2'].append(trade['rets'][2])


def pct_stats(xs):
    if not xs:
        return (0, 0.0, 0.0, 0.0)
    n = len(xs)
    return (n, mean(xs), sum(1 for x in xs if x > 0) / n * 100.0,
            sorted(xs)[n // 2])


def print_variant_summary(variant, stats_by_timing):
    out("\n" + "=" * 92)
    out(f"变体 {variant} 汇总")
    out("=" * 92)
    for timing in TIMINGS:
        st = stats_by_timing[timing]
        label = 'today h1_open(激进)' if timing == 'today' else 'T+1 h1_open(保守)'
        out(f"\n  [买入时机: {label}]  信号数={st['signals']}  "
            f"可评估={st['evaluable']}")
        if st['evaluable'] == 0:
            out("    无可评估样本")
            st['_best'] = None
            st['_pos_years'] = (0, 0)
            continue
        out(f"    {'持有':<8}{'样本':>6}{'均收益%':>10}{'胜率%':>9}{'中位%':>9}")
        best = None
        for n in range(1, HOLD_MAX + 1):
            cnt, avg, wr, med = pct_stats(st['ret_by_day'][n])
            out(f"    T+{n:<6}{cnt:>6}{avg:>+10.2f}{wr:>9.1f}{med:>+9.2f}")
            if cnt >= 20 and (best is None or avg > best[1]):
                best = (n, avg, wr, med, cnt)
        tp_rate = st['tp_hit'] / st['evaluable'] * 100.0
        avg_dd = mean(st['max_dd'])
        worst_dd = min(st['max_dd']) if st['max_dd'] else 0.0
        out(f"    止盈(+{TP_PCT:.0f}%)触及率: {tp_rate:.1f}%  "
            f"平均最大回撤: {avg_dd:+.2f}%  最差单笔: {worst_dd:+.2f}%")
        line = "    逐年(持有1日 收益/胜率/n): "
        pos_years = tot_years = 0
        for year in range(START_YEAR, END_YEAR + 1):
            yr = st['by_year'].get(year)
            if not yr or not yr['ret1']:
                continue
            a = mean(yr['ret1'])
            w = sum(1 for x in yr['ret1'] if x > 0) / len(yr['ret1']) * 100.0
            tot_years += 1
            if a > 0:
                pos_years += 1
            line += f"{year}:{a:+.1f}%/{w:.0f}%/n{len(yr['ret1'])}  "
        out(line)
        if best:
            n, avg, wr, med, cnt = best
            out(f"    >>> 最优持有: T+{n}  均收益{avg:+.2f}%  胜率{wr:.1f}%  "
                f"样本{cnt}  正收益年份{pos_years}/{tot_years}")
        st['_best'] = best
        st['_pos_years'] = (pos_years, tot_years)


def print_final_verdict(all_stats, monthly_mode):
    out("\n\n" + "=" * 92)
    out("最终对比与达标判定")
    out("=" * 92)
    out(f"{'变体':<6}{'时机':<8}{'信号':>7}{'最优持有':>9}{'均收益%':>10}"
        f"{'胜率%':>8}{'正年份':>9}{'判定':>10}")
    out("-" * 92)
    winner = None
    for v in VARIANTS:
        for timing in TIMINGS:
            st = all_stats[v][timing]
            best = st.get('_best')
            pos = st.get('_pos_years', (0, 0))
            tlabel = 'today' if timing == 'today' else 'T+1'
            if not best:
                out(f"{v:<6}{tlabel:<8}{st['signals']:>7}{'-':>9}{'-':>10}"
                    f"{'-':>8}{'-':>9}{'样本不足':>10}")
                continue
            n, avg, wr, med, cnt = best
            stable = pos[1] > 0 and pos[0] >= pos[1] * 0.6
            qualified = (wr >= 55.0 and avg > 0 and stable and cnt >= 30)
            verdict = '达标' if qualified else '未达标'
            out(f"{v:<6}{tlabel:<8}{st['signals']:>7}{'T+'+str(n):>9}"
                f"{avg:>+10.2f}{wr:>8.1f}{str(pos[0])+'/'+str(pos[1]):>9}"
                f"{verdict:>10}")
            score = avg * (wr / 100.0)
            if qualified and (winner is None or score > winner[0]):
                winner = (score, v, tlabel, n, avg, wr, cnt)
    out("-" * 92)
    if winner:
        _, v, t, n, avg, wr, cnt = winner
        out(f"\n★ 推荐配置: 变体{v} + {t} h1_open + 持有T+{n} "
            f"-> 均收益{avg:+.2f}% 胜率{wr:.1f}% 样本{cnt}")
        out("  判定: 达标 (胜率>=55% 且均收益>0 且跨年稳定)")
    else:
        out("\n4种变体均未达 rule2 达标线(胜率>=55% + 均收益>0 + 跨年稳定)")
        out("  结论: 换手率突变(即使基数极低)在 today/T+1 h1_open 买入下未能稳定盈利,")
        out("        与此前'放大5x不可执行'结论方向一致。")
    if monthly_mode:
        out("\n(注: 单月样本有限, 判定仅供参考, 以 all 全周期为准)")


def run(mode):
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    all_days = get_all_trading_days(cur)
    monthly_mode = (mode != 'all')
    if monthly_mode:
        scan_idxs = [i for i, d in enumerate(all_days) if d.startswith(mode)]
        if not scan_idxs:
            out(f"无 {mode} 的交易日数据")
            conn.close()
            return
    else:
        scan_idxs = [i for i, d in enumerate(all_days)
                     if START_YEAR <= int(d[:4]) <= END_YEAR and i >= 11]
    out("=" * 92)
    out("换手率突变策略研究 (Task #77)")
    out(f"模式: {mode}   扫描交易日: {len(scan_idxs)}   "
        f"数据范围: {all_days[0]} ~ {all_days[-1]}")
    out("变体: A极低突增 | B低迷放量突破 | C阶梯放大 | D极端突变")
    out(f"止盈阈值 +{TP_PCT:.0f}%  最长持有 T+{HOLD_MAX}")
    out("=" * 92)
    all_stats = {v: {t: new_stat() for t in TIMINGS} for v in VARIANTS}
    processed = 0
    for i in scan_idxs:
        if i < 11:
            continue
        today = all_days[i]
        year = int(today[:4])
        cand_map = find_candidates(cur, all_days, i)
        for v in VARIANTS:
            cands = cand_map[v]
            for c in cands:
                for t in TIMINGS:
                    all_stats[v][t]['signals'] += 1
                    trade = eval_trade(cur, c['code'], all_days, i, t)
                    if trade is not None:
                        accumulate(all_stats[v][t], year, trade)
            if monthly_mode and cands:
                out(f"\n--- {today}  变体{v}  候选 {len(cands)} 只 ---")
                for c in cands:
                    out(f"  {c['code']} {c['name']}  突增={c['surge']:.1f}x  "
                        f"yd_turn={c['yd_turn']:.2f}%  基数={c['base']:.3f}%  "
                        f"高开={c['today_open_rate']:.2f}%")
                    print_hour_detail(cur, c['code'], c['name'], all_days, i)
        processed += 1
        if not monthly_mode and processed % 200 == 0:
            out(f"  ...已处理 {processed}/{len(scan_idxs)} 交易日")
    for v in VARIANTS:
        print_variant_summary(v, all_stats[v])
    print_final_verdict(all_stats, monthly_mode)
    conn.close()
    out("\n研究完成。")


def main():
    global _log_fh
    if len(sys.argv) < 2:
        print("用法: python3 research_turnover_spike_r2.py <YYYY-MM|all>")
        sys.exit(1)
    os.makedirs(os.path.dirname(LOG_PATH), exist_ok=True)
    _log_fh = open(LOG_PATH, 'w', encoding='utf-8')
    try:
        run(sys.argv[1])
    finally:
        _log_fh.close()


if __name__ == '__main__':
    main()
