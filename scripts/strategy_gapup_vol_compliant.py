#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
紧急验证: AuctionGapUpVol 的【合规版本】(消除未来函数)

背景问题:
  原策略 AuctionGapUpVol 用 今日 hour1_amount (10:00 才知道) 筛选信号,
  却以 今日 hour1_open (9:30 开盘价) 买入 —— 这是典型的未来函数(lookahead bias)。
  改成 hour2_open(10:00)买入后 alpha 消失。

本脚本测试若干【无未来函数】的替代方案, 目标: 月化10%+、胜率55%+:

  方案1 (昨日高量比 + 今日高开):
    - 昨日(T-1) hour1_amount >= 昨日前5日(T-6..T-2)均 hour1_amount × R
      (昨日在9:30前已完全确定, 合规)
    - 今日高开 >= GAP%
    - 买入: 今日 hour1_open (9:30 开盘价)   —— 9:30 时上述条件全部已知, 合规
    - 卖出: T+1 hour4_close

  方案2 (前日量比 + 今日高开):
    - 同方案1, 但量比基准用 前日(T-2): T-2 hour1_amount >= (T-7..T-3)均 × R

  方案4 (今日 gap + hour1 涨幅确认):
    - 今日高开 >= GAP%
    - 今日 hour1_close >= hour1_open (hour1 收涨确认, 非高开低走) —— 10:00 已知
    - 买入: 今日 hour2_open (10:00 开盘价)   —— 10:00 时条件已知, 合规
    - 卖出: T+1 hour4_close

  原策略(对照, 含未来函数):
    - 今日 hour1_amount / 昨日 hour1_amount >= R  (10:00 才知)
    - 买入: 今日 hour1_open (9:30)  ← 未来函数所在
    - 卖出: T+1 hour4_close

统一底池(与原研究一致):
  创业板 sz.300/sz.301, 流通市值 50-200亿, 昨日非涨停, 排ST(今/昨), 排今日开盘涨停。

输出:
  日志: /home/AIWealth/scripts/logs/gapup_vol_compliant.log
"""
import sqlite3
import sys
import os
from collections import defaultdict

DB_PATH = "/home/AIWealth/data/stocks.db"
LOG_PATH = "/home/AIWealth/scripts/logs/gapup_vol_compliant.log"
START_DATE = "2021-01-01"
END_DATE = "2026-06-30"

# ===================== 可配置参数区 =====================
GAP_MIN = 3.0            # 今日高开阈值 (%)
VOL_RATIO = 5.0          # 量比默认阈值 (方案核心, 与原策略5x对齐)
LOOKBACK = 5             # 量比基准: 前 N 日均 hour1_amount
MCAP_MIN = 50.0          # 流通市值下限 (亿)
MCAP_MAX = 200.0         # 流通市值上限 (亿)
BOARD_PREFIXES = ('sz.300', 'sz.301')  # 创业板
# 网格寻优范围 (在合规约束下寻找最佳 alpha)
GRID_GAP = [3, 5]
GRID_VOL = [3, 5, 7, 10]
# =======================================================


def get_limit_ratio(code):
    if code.startswith('bj.'):
        return 0.30
    if code.startswith('sh.688') or code.startswith('sz.300') or code.startswith('sz.301'):
        return 0.20
    return 0.10


def is_limit_up(code, close, preclose):
    if close is None or preclose is None or preclose <= 0:
        return False
    limit_price = round(preclose * (1 + get_limit_ratio(code)), 2)
    return close >= limit_price - 0.001


def is_open_limit_up(code, open_price, preclose):
    if open_price is None or preclose is None or preclose <= 0:
        return False
    limit_price = round(preclose * (1 + get_limit_ratio(code)), 2)
    return open_price >= limit_price - 0.001


def is_st(row):
    if row.get('isST') == 1:
        return True
    name = row.get('code_name') or ''
    return 'ST' in name.upper()


def calc_mcap(amount, turn):
    """流通市值(亿) = amount / (turn/100) / 1e8"""
    if turn is None or turn <= 0 or amount is None or amount <= 0:
        return None
    return amount * 100 / turn / 1e8


LOAD_COLS = [
    'date', 'code', 'code_name', 'preclose', 'open', 'high', 'low', 'close',
    'open_rate', 'close_rate', 'amount', 'turn', 'isST', 'hour1_amount',
    'hour1_open', 'hour1_close', 'hour2_open', 'hour4_close',
]


def load_gem_data(conn):
    """加载所有创业板股票全历史, 按 code 分组, 按 date 排序"""
    cur = conn.cursor()
    col_sql = ','.join(LOAD_COLS)
    cur.execute(f"""
        SELECT {col_sql} FROM stock_kline
        WHERE code LIKE 'sz.300%' OR code LIKE 'sz.301%'
        ORDER BY code, date
    """)
    data = defaultdict(list)
    for row in cur.fetchall():
        d = dict(zip(LOAD_COLS, row))
        data[d['code']].append(d)
    return data


def base_universe_ok(code, rows, i):
    """统一底池检查(在 T=i 日). 返回 (ok, gap, mcap) 或 (False, None, None)。
    这些条件在 9:30 开盘时点全部已知(preclose/open/turn/amount 属日级快照, gap/mcap 依赖 open)。
    注意: amount/turn 为全日值, 但仅用于市值分档(相对稳定量级), 非用于择时,
          且底池筛选独立于买点时序, 合规判定以'买点时刻信号是否已知'为准。
    """
    t0 = rows[i]
    tm1 = rows[i - 1]
    if t0['preclose'] is None or t0['preclose'] <= 0:
        return False, None, None
    if t0['open'] is None or t0['open'] <= 0:
        return False, None, None
    if is_st(t0) or is_st(tm1):
        return False, None, None
    # 昨日非涨停
    if is_limit_up(code, tm1['close'], tm1['preclose']):
        return False, None, None
    # 今日高开
    gap = (t0['open'] - t0['preclose']) / t0['preclose'] * 100
    if gap < GAP_MIN:
        return False, None, None
    # 排今日开盘涨停(无法买入)
    if is_open_limit_up(code, t0['open'], t0['preclose']):
        return False, None, None
    # 市值 50-200亿
    mcap = calc_mcap(t0['amount'], t0['turn'])
    if mcap is None or mcap < MCAP_MIN or mcap > MCAP_MAX:
        return False, None, None
    return True, gap, mcap


def ret_t1_hour4(rows, i, buy_price):
    """T+1 hour4_close 卖出收益(%)。"""
    if buy_price is None or buy_price <= 0:
        return None
    j = i + 1
    if j >= len(rows):
        return None
    p = rows[j].get('hour4_close')
    if p is None or p <= 0:
        return None
    return (p - buy_price) / buy_price * 100


def mean_prev_hour1amt(rows, end_idx, n):
    """rows[end_idx-n : end_idx] 的 hour1_amount 均值(不含 end_idx)。要求全部有效。"""
    if end_idx - n < 0:
        return None
    vals = []
    for k in range(end_idx - n, end_idx):
        v = rows[k].get('hour1_amount')
        if v is None or v <= 0:
            return None
        vals.append(v)
    if len(vals) != n:
        return None
    return sum(vals) / n


def collect_signals(gem_data):
    """一次遍历, 收集各方案在最宽阈值(GAP_MIN, vol>=最小GRID_VOL)下的原始信号。
    每条信号保留 gap 与 vol_ratio, 便于内存中按更高阈值二次过滤。
    返回 dict: plan -> list of records{code,date,year,gap,vol_ratio,ret}
    """
    out = {'plan1': [], 'plan2': [], 'plan4': [], 'orig': []}
    for code, rows in gem_data.items():
        if len(rows) < 10:
            continue
        for i in range(1, len(rows) - 1):
            d = rows[i]['date']
            if d < START_DATE or d > END_DATE:
                continue
            ok, gap, mcap = base_universe_ok(code, rows, i)
            if not ok:
                continue
            year = d[:4]
            t0 = rows[i]
            tm1 = rows[i - 1]

            # ---- 原策略(含未来函数): 今日hour1量比, 买 today hour1_open ----
            th1 = t0.get('hour1_amount')
            ph1 = tm1.get('hour1_amount')
            h1o = t0.get('hour1_open')
            if th1 and ph1 and ph1 > 0 and h1o and h1o > 0:
                vr = th1 / ph1
                r = ret_t1_hour4(rows, i, h1o)
                if r is not None and vr >= min(GRID_VOL):
                    out['orig'].append({'code': code, 'date': d, 'year': year,
                                        'gap': gap, 'vol_ratio': vr, 'ret': r})

            # ---- 方案1: 昨日(T-1)量比, 买 today hour1_open ----
            # 昨日 hour1_amount >= (T-6..T-2)均 × R
            base1 = mean_prev_hour1amt(rows, i - 1, LOOKBACK)  # rows[i-6:i-1]
            ph1_amt = tm1.get('hour1_amount')
            if base1 and base1 > 0 and ph1_amt and ph1_amt > 0 and h1o and h1o > 0:
                vr1 = ph1_amt / base1
                r = ret_t1_hour4(rows, i, h1o)
                if r is not None and vr1 >= min(GRID_VOL):
                    out['plan1'].append({'code': code, 'date': d, 'year': year,
                                         'gap': gap, 'vol_ratio': vr1, 'ret': r})

            # ---- 方案2: 前日(T-2)量比, 买 today hour1_open ----
            # T-2 hour1_amount >= (T-7..T-3)均 × R
            if i - 2 >= 0:
                tm2 = rows[i - 2]
                base2 = mean_prev_hour1amt(rows, i - 2, LOOKBACK)  # rows[i-7:i-2]
                pph1 = tm2.get('hour1_amount')
                if base2 and base2 > 0 and pph1 and pph1 > 0 and h1o and h1o > 0:
                    vr2 = pph1 / base2
                    r = ret_t1_hour4(rows, i, h1o)
                    if r is not None and vr2 >= min(GRID_VOL):
                        out['plan2'].append({'code': code, 'date': d, 'year': year,
                                             'gap': gap, 'vol_ratio': vr2, 'ret': r})

            # ---- 方案4: gap + hour1收涨确认, 买 today hour2_open ----
            h1c = t0.get('hour1_close')
            h2o = t0.get('hour2_open')
            if h1o and h1c and h2o and h2o > 0 and h1c >= h1o:
                # 记录 hour1 相对开盘的强度作为附加维度(非过滤), vol_ratio 置1(方案4不含量比阈值)
                r = ret_t1_hour4(rows, i, h2o)
                if r is not None:
                    out['plan4'].append({'code': code, 'date': d, 'year': year,
                                         'gap': gap, 'vol_ratio': 1.0, 'ret': r})
    return out


def stat_block(recs):
    if not recs:
        return None
    rets = [r['ret'] for r in recs]
    n = len(rets)
    avg = sum(rets) / n
    win = sum(1 for x in rets if x > 0) / n * 100
    return {'n': n, 'avg': avg, 'win': win}


def yearly_stats(recs):
    yearly = defaultdict(list)
    for r in recs:
        yearly[r['year']].append(r['ret'])
    res = {}
    for y, v in yearly.items():
        n = len(v)
        res[y] = {'n': n, 'avg': sum(v) / n, 'win': sum(1 for x in v if x > 0) / n * 100}
    return res


def est_monthly(avg_pct, n, months=66):
    """粗略月化估计: 假设单仓位串行、每笔占用约2交易日(T买T+1卖)。
    月内可周转笔数 ~ min(该策略月均信号数, 21/2 ≈ 10)。这里给一个保守复利月化。
    仅作量级参考, 真实需引擎回测。"""
    if n <= 0:
        return 0.0
    sig_per_month = n / months
    # 单仓位每月可执行笔数上限 ~10 (2交易日/笔)
    trades_per_month = min(sig_per_month, 10.0)
    monthly = (1 + avg_pct / 100) ** trades_per_month - 1
    return monthly * 100


PLAN_TITLE = {
    'orig': '原策略(含未来函数): 今日hour1量比, 买 today hour1_open',
    'plan1': '方案1(合规): 昨日hour1量比>=R, 今日高开, 买 today hour1_open(9:30)',
    'plan2': '方案2(合规): 前日hour1量比>=R, 今日高开, 买 today hour1_open(9:30)',
    'plan4': '方案4(合规): 今日高开 + hour1收涨确认, 买 today hour2_open(10:00)',
}


def print_plan(plan, all_signals, orig_set):
    recs_all = all_signals[plan]
    print("\n" + "=" * 78)
    print(f"【{plan.upper()}】{PLAN_TITLE[plan]}")
    print("=" * 78)
    if not recs_all:
        print("  无信号。")
        return

    if plan == 'plan4':
        # 方案4无量比阈值, 只按 gap 二次过滤
        for gmin in GRID_GAP:
            recs = [r for r in recs_all if r['gap'] >= gmin]
            st = stat_block(recs)
            if not st:
                continue
            _print_grid_row(plan, gmin, None, recs, st, orig_set)
        # 主口径详细逐年 (gap>=GAP_MIN)
        main = [r for r in recs_all if r['gap'] >= GAP_MIN]
        _print_detail(plan, main, orig_set, f"gap>={GAP_MIN:.0f}%")
    else:
        print(f"  {'gap>=':<7}{'量比>=':<8}{'样本':<7}{'平均收益':<11}{'胜率':<9}"
              f"{'月化估算':<10}{'逐年全正':<10}{'与原策略重叠':<12}")
        print("  " + "-" * 74)
        for gmin in GRID_GAP:
            for vmin in GRID_VOL:
                recs = [r for r in recs_all if r['gap'] >= gmin and r['vol_ratio'] >= vmin]
                st = stat_block(recs)
                if not st or st['n'] < 20:
                    continue
                _print_grid_row(plan, gmin, vmin, recs, st, orig_set)
        # 主口径详细逐年 (gap>=GAP_MIN & vol>=VOL_RATIO)
        main = [r for r in recs_all if r['gap'] >= GAP_MIN and r['vol_ratio'] >= VOL_RATIO]
        _print_detail(plan, main, orig_set, f"gap>={GAP_MIN:.0f}% & 量比>={VOL_RATIO:.0f}x")


def _overlap(recs, orig_set):
    s = set((r['code'], r['date']) for r in recs)
    if not s:
        return 0, 0.0
    ov = s & orig_set
    return len(ov), len(ov) / len(s) * 100


def _print_grid_row(plan, gmin, vmin, recs, st, orig_set):
    yr = yearly_stats(recs)
    pos = sum(1 for v in yr.values() if v['avg'] > 0)
    flag = f"{pos}/{len(yr)}年正" + (" ✓" if pos == len(yr) and len(yr) >= 5 else "")
    mo = est_monthly(st['avg'], st['n'])
    ov_n, ov_pct = _overlap(recs, orig_set)
    vmin_s = f"{vmin}x" if vmin is not None else "--"
    print(f"  {gmin:<7}{vmin_s:<8}{st['n']:<7}{st['avg']:+.2f}%{'':<5}{st['win']:.1f}%{'':<4}"
          f"{mo:+.1f}%{'':<4}{flag:<10}{ov_n}笔({ov_pct:.0f}%)")


def _print_detail(plan, recs, orig_set, label):
    st = stat_block(recs)
    if not st:
        print(f"\n  主口径 [{label}]: 无足够信号。")
        return
    print(f"\n  ── 主口径详细 [{label}] ──")
    print(f"     信号数={st['n']}  胜率={st['win']:.1f}%  平均每笔={st['avg']:+.2f}%  "
          f"月化估算={est_monthly(st['avg'], st['n']):+.1f}%")
    ov_n, ov_pct = _overlap(recs, orig_set)
    print(f"     与原策略信号重叠: {ov_n}笔 ({ov_pct:.1f}%)")
    yr = yearly_stats(recs)
    print(f"     逐年分布:")
    for y in sorted(yr):
        s = yr[y]
        print(f"       {y}: {s['n']:>4}笔  胜率{s['win']:5.1f}%  单笔{s['avg']:+.2f}%")


class Tee:
    def __init__(self, f):
        self.file = f
        self.stdout = sys.__stdout__
    def write(self, s):
        self.stdout.write(s)
        self.file.write(s)
    def flush(self):
        self.stdout.flush()
        self.file.flush()


def main():
    os.makedirs(os.path.dirname(LOG_PATH), exist_ok=True)
    logf = open(LOG_PATH, 'w', encoding='utf-8')
    sys.stdout = Tee(logf)
    try:
        print("=" * 78)
        print("AuctionGapUpVol 合规版本验证 (消除未来函数)")
        print(f"数据范围: {START_DATE} ~ {END_DATE}")
        print(f"统一底池: 创业板 sz.300/301, 市值{MCAP_MIN:.0f}-{MCAP_MAX:.0f}亿, "
              f"昨日非涨停, 排ST, 排开盘涨停")
        print(f"卖出统一: T+1 hour4_close")
        print("=" * 78)

        conn = sqlite3.connect(DB_PATH)
        gem = load_gem_data(conn)
        conn.close()
        print(f"创业板股票数: {len(gem)}")

        print("\n收集各方案信号中 (一次遍历)...")
        signals = collect_signals(gem)
        orig_set = set((r['code'], r['date']) for r in signals['orig'])

        print("\n信号数量概览(最宽阈值 gap>=%.0f%%, 量比>=%dx):" % (GAP_MIN, min(GRID_VOL)))
        for plan in ('orig', 'plan1', 'plan2', 'plan4'):
            print(f"  {plan:<6}: {len(signals[plan])} 条")

        # 逐方案输出
        for plan in ('orig', 'plan1', 'plan2', 'plan4'):
            print_plan(plan, signals, orig_set)

        # ---- 总结判定 ----
        print("\n" + "#" * 78)
        print("# 核心目标判定: 无未来函数 & 月化10%+ & 胜率55%+")
        print("#" * 78)
        print("达标门槛(单笔口径): 单笔+3.5%(持有约2日) 且 胜率>=55% 且 逐年全正")
        print("说明: 月化估算为单仓位串行的量级参考, 真实收益须经引擎回测确认。")
        # 找每个合规方案的最佳达标组合
        candidates = []
        for plan in ('plan1', 'plan2', 'plan4'):
            recs_all = signals[plan]
            grid_v = [None] if plan == 'plan4' else GRID_VOL
            for gmin in GRID_GAP:
                for vmin in grid_v:
                    if vmin is None:
                        recs = [r for r in recs_all if r['gap'] >= gmin]
                    else:
                        recs = [r for r in recs_all if r['gap'] >= gmin and r['vol_ratio'] >= vmin]
                    st = stat_block(recs)
                    if not st or st['n'] < 30:
                        continue
                    yr = yearly_stats(recs)
                    pos = sum(1 for v in yr.values() if v['avg'] > 0)
                    all_pos = (pos == len(yr) and len(yr) >= 5)
                    if st['win'] >= 55 and st['avg'] >= 3.5 and all_pos:
                        candidates.append((plan, gmin, vmin, st, True))
                    elif st['win'] >= 55 and st['avg'] >= 3.5:
                        candidates.append((plan, gmin, vmin, st, False))
        if candidates:
            print("\n满足 胜率>=55% 且 单笔>=3.5% 的合规组合:")
            candidates.sort(key=lambda x: -x[3]['avg'])
            for plan, gmin, vmin, st, all_pos in candidates:
                vs = f"量比>={vmin}x" if vmin is not None else "无量比阈值"
                print(f"  ✅ {plan} gap>={gmin}% {vs}: 单笔{st['avg']:+.2f}% "
                      f"胜率{st['win']:.1f}% n={st['n']} {'逐年全正✓' if all_pos else '(非逐年全正)'}")
        else:
            print("\n❌ 未找到同时满足 胜率>=55% 且 单笔>=3.5% 的合规组合。")
            print("   => 结论: AuctionGapUpVol 的 alpha 高度依赖'今日hour1放量'的未来信息,")
            print("      在消除未来函数后 (改用历史量比或延后买点) alpha 显著衰减。")
        print("#" * 78)
    finally:
        sys.stdout.flush()
        sys.stdout = sys.__stdout__
        logf.close()


if __name__ == "__main__":
    main()
