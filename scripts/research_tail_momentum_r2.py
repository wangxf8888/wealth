#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
尾盘拉升(hour4异动)次日策略研究 R2

策略思路
--------
游资在 hour4(14:00-15:00) 突然拉升股价 —— 目的是做 T+1 次日出货。
若尾盘拉升幅度大但全天未涨停, 说明有资金刻意在收盘前布局, 次日该股通常高开,
给跟随者提供潜在获利机会。本脚本验证该假设是否成立、是否可转化为可执行策略。

信号定义 (信号日 D)
-------------------
  hour4涨幅 = (hour4_close - hour4_open) / hour4_open >= 3%
  全天涨幅 = close_rate (即 (close-preclose)/preclose)
    要求: 0 < 全天涨幅 < 涨停幅度阈值 (主板8% / 创业板18% / 科创板18%)  --> 确保未涨停且收阳
  换手率 turn >= 0.5% (流动性)
  排除 ST, 排除北交所
  流通市值分组: <50亿 / 50-200亿 / 200-700亿 (>700亿剔除)

A股T+1规则 (rule2)
------------------
  D+1 买入当日不可卖出, 最早 D+2 才能卖出。
  因此:
    - 可执行配置: D+1 hour1_open(或hour2_open)买入, D+2/D+3/D+4 卖出。
    - 假设检验: 对 D+1 日内价格路径做"开高走低 vs 惯性上冲"的描述性分析(不成交)。

用法
----
  python3 research_tail_momentum_r2.py 2025-03   # 单月(打印候选股前5后5日hour明细)
  python3 research_tail_momentum_r2.py 2025       # 单年(仅汇总)
  python3 research_tail_momentum_r2.py all        # 全周期(仅汇总)

输出日志: /home/AIWealth/scripts/logs/tail_momentum_r2.log
"""
import sys
import os
import sqlite3
from collections import defaultdict

# ==================== 配置区 ====================
DB_PATH = '/home/AIWealth/data/stocks.db'
LOG_PATH = '/home/AIWealth/scripts/logs/tail_momentum_r2.log'

HOUR4_GAIN_MIN = 3.0        # hour4涨幅下限(%)
DAY_GAIN_MIN = 0.0          # 全天涨幅下限(%), 收阳
TURN_MIN = 0.5             # 最小换手率(%)
MCAP_MAX = 700.0            # 流通市值上限(亿), 超过剔除

# 各板块"未涨停"判定阈值(全天涨幅须低于此值)
DAY_GAIN_MAX = {'主板': 8.0, '创业板': 18.0, '科创板': 18.0}

# 止盈档位(%)
TP_LEVELS = [2.0, 3.0, 5.0, 8.0]

DATA_START_YEAR = 2020
DATA_END_YEAR = 2026
# ================================================

_LOG_FH = None


def p(*args):
    """输出到日志文件"""
    s = ' '.join(str(x) for x in args)
    if _LOG_FH is not None:
        _LOG_FH.write(s + '\n')
    else:
        print(s)


def progress(msg):
    """进度输出到stderr"""
    sys.stderr.write(msg + '\n')
    sys.stderr.flush()


def classify_board(code):
    """板块分类; 返回 None 表示排除(北交所)"""
    if code.startswith('sz.300') or code.startswith('sz.301'):
        return '创业板'
    if code.startswith('sh.688'):
        return '科创板'
    if code.startswith('bj.'):
        return None  # 北交所排除
    if code.startswith('sh.60') or code.startswith('sz.00'):
        return '主板'
    return None


def mcap_group(mcap):
    if mcap is None:
        return None
    if mcap < 50:
        return '<50亿'
    if mcap < 200:
        return '50-200亿'
    if mcap <= 700:
        return '200-700亿'
    return None


def estimate_mcap(close, volume, turn):
    """通过 换手率 反推流通市值(亿): turn(%)=volume/流通股*100"""
    if close is None or volume is None or turn is None or turn <= 0:
        return None
    return close * volume * 100.0 / turn / 1e8


def get_all_trading_days(cur):
    cur.execute("SELECT DISTINCT date FROM stock_kline ORDER BY date")
    return [r[0] for r in cur.fetchall()]


# 未来日需要取的列
FUT_COLS = ['open', 'high', 'low', 'close', 'preclose', 'open_rate', 'close_rate',
            'hour1_open', 'hour1_high', 'hour1_low', 'hour1_close',
            'hour2_open', 'hour2_high', 'hour3_high', 'hour4_high']
_FUT_SQL = ','.join(FUT_COLS)


def fetch_future_days(cur, code, dates):
    """返回 {date: {col:val}}"""
    if not dates:
        return {}
    ph = ','.join(['?'] * len(dates))
    cur.execute(f"SELECT date,{_FUT_SQL} FROM stock_kline WHERE code=? AND date IN ({ph})",
                [code] + list(dates))
    out = {}
    for row in cur.fetchall():
        d = row[0]
        out[d] = {FUT_COLS[i]: row[i + 1] for i in range(len(FUT_COLS))}
    return out


def find_signals(cur, day):
    """筛选信号日 day 满足尾盘拉升条件的候选股"""
    cur.execute("""
        SELECT code, code_name, preclose, open, close, close_rate, volume, turn,
               hour4_open, hour4_close, hour4_high, hour4_low, isST
        FROM stock_kline
        WHERE date=? AND isST=0 AND preclose>0 AND turn>=?
          AND hour4_open IS NOT NULL AND hour4_open>0
          AND hour4_close IS NOT NULL AND close_rate IS NOT NULL
          AND close_rate>? AND volume>0
    """, (day, TURN_MIN, DAY_GAIN_MIN))

    signals = []
    for row in cur.fetchall():
        (code, name, preclose, open_p, close, close_rate, volume, turn,
         h4o, h4c, h4h, h4l, isST) = row

        board = classify_board(code)
        if board is None:
            continue
        if name and 'ST' in name.upper():
            continue

        # 全天涨幅未涨停
        if close_rate >= DAY_GAIN_MAX[board]:
            continue

        # hour4涨幅
        h4_gain = (h4c - h4o) / h4o * 100.0
        if h4_gain < HOUR4_GAIN_MIN:
            continue

        mcap = estimate_mcap(close, volume, turn)
        mg = mcap_group(mcap)
        if mg is None:
            continue  # >700亿 或无法估算 剔除

        signals.append({
            'code': code, 'name': name, 'board': board,
            'close': close, 'close_rate': close_rate,
            'h4_gain': h4_gain, 'turn': turn,
            'mcap': mcap, 'mcap_group': mg,
        })
    return signals


def h4_gain_bucket(g):
    if g < 5.0:
        return '3-5%'
    if g < 8.0:
        return '5-8%'
    return '>8%'


def day_high(d):
    return d.get('high') if d else None


def day_low(d):
    return d.get('low') if d else None


def simulate(fut, buy_price, sell_offset, tp_pct=None, sl_pct=None):
    """
    模拟一笔交易 (遵守T+1: 最早 D+2=offset2 卖出)
    fut: {offset: daydict}, offset1=买入日D+1
    返回 (收益%, 出场offset, 出场类型) 或 None
    止损优先(保守): 同日既触止盈又触止损时按止损计。
    """
    if buy_price is None or buy_price <= 0:
        return None
    tp_price = buy_price * (1 + tp_pct / 100.0) if tp_pct is not None else None
    sl_price = buy_price * (1 + sl_pct / 100.0) if sl_pct is not None else None

    for off in range(2, sell_offset + 1):
        d = fut.get(off)
        if d is None:
            continue
        lo = day_low(d)
        hi = day_high(d)
        if sl_price is not None and lo is not None and lo <= sl_price:
            return (sl_price - buy_price) / buy_price * 100.0, off, 'SL'
        if tp_price is not None and hi is not None and hi >= tp_price:
            return (tp_price - buy_price) / buy_price * 100.0, off, 'TP'

    d = fut.get(sell_offset)
    if d and d.get('close'):
        return (d['close'] - buy_price) / buy_price * 100.0, sell_offset, 'CLOSE'
    return None


def collect_period_signals(cur, all_days, day_index, period_filter):
    """遍历符合period的交易日, 返回带未来数据的信号列表"""
    results = []
    total_days = 0
    for i, day in enumerate(all_days):
        if i < 6 or i + 1 >= len(all_days):
            continue
        y = int(day[:4]); m = int(day[5:7])
        if not period_filter(y, m):
            continue
        total_days += 1
        sigs = find_signals(cur, day)
        if not sigs:
            continue
        # 未来4日日期
        fut_dates = all_days[i + 1:i + 5]
        for s in sigs:
            fut_rows = fetch_future_days(cur, s['code'], fut_dates)
            fut = {}
            for off, fd in enumerate(fut_dates, start=1):
                if fd in fut_rows:
                    fut[off] = fut_rows[fd]
            s['sig_date'] = day
            s['sig_idx'] = i
            s['fut'] = fut
            results.append(s)
        if total_days % 100 == 0:
            progress(f"  已扫描 {total_days} 个交易日, 累计信号 {len(results)}...")
    return results, total_days


# ---------------------------------------------------------------------------
# 候选股明细打印 (前5后5日 hour级OHLC)
# ---------------------------------------------------------------------------
def print_candidate_details(cur, signals, all_days):
    p("\n" + "=" * 100)
    p("候选股明细 (信号日D 前5后5日 hour级 OHLC)")
    p("=" * 100)
    detail_cols = ['open', 'open_rate', 'close', 'close_rate',
                   'hour1_open', 'hour1_high', 'hour1_low', 'hour1_close',
                   'hour2_open', 'hour2_high', 'hour2_low', 'hour2_close',
                   'hour3_open', 'hour3_high', 'hour3_low', 'hour3_close',
                   'hour4_open', 'hour4_high', 'hour4_low', 'hour4_close']
    dcsql = ','.join(detail_cols)

    for s in signals:
        i = s['sig_idx']
        lo = max(0, i - 5); hi = min(len(all_days) - 1, i + 5)
        win_dates = all_days[lo:hi + 1]
        ph = ','.join(['?'] * len(win_dates))
        cur.execute(f"SELECT date,{dcsql} FROM stock_kline WHERE code=? AND date IN ({ph}) ORDER BY date",
                    [s['code']] + win_dates)
        rows = cur.fetchall()
        p("\n" + "-" * 100)
        p(f"[{s['code']} {s['name']}] 信号日={s['sig_date']} 板块={s['board']} "
          f"hour4涨幅={s['h4_gain']:.2f}% 全天涨幅={s['close_rate']:.2f}% "
          f"换手={s['turn']:.2f}% 流通市值={s['mcap']:.0f}亿({s['mcap_group']})")
        p(f"{'日期':<12}{'标记':<5}{'开%':>7}{'收%':>7} | "
          f"{'h1(o/h/l/c)':>24} | {'h2':>24} | {'h3':>24} | {'h4':>24}")
        for row in rows:
            d = {detail_cols[k]: row[k + 1] for k in range(len(detail_cols))}
            date = row[0]
            mark = '***' if date == s['sig_date'] else ''
            if date < s['sig_date']:
                mark = mark or '-'
            elif date > s['sig_date']:
                mark = mark or '+'

            def hstr(pre):
                o = d.get(pre + '_open'); h = d.get(pre + '_high')
                l = d.get(pre + '_low'); c = d.get(pre + '_close')
                def f(x): return f"{x:.2f}" if x is not None else '  -  '
                return f"{f(o)}/{f(h)}/{f(l)}/{f(c)}"
            orate = d.get('open_rate'); crate = d.get('close_rate')
            os_ = f"{orate:+.1f}" if orate is not None else '  -'
            cs_ = f"{crate:+.1f}" if crate is not None else '  -'
            p(f"{date:<12}{mark:<5}{os_:>7}{cs_:>7} | "
              f"{hstr('hour1'):>24} | {hstr('hour2'):>24} | {hstr('hour3'):>24} | {hstr('hour4'):>24}")


# ---------------------------------------------------------------------------
# 汇总分析
# ---------------------------------------------------------------------------
def _stats(rets):
    if not rets:
        return None
    n = len(rets)
    avg = sum(rets) / n
    win = sum(1 for r in rets if r > 0) / n * 100
    med = sorted(rets)[n // 2]
    return {'n': n, 'avg': avg, 'win': win, 'med': med,
            'max': max(rets), 'min': min(rets)}


def _fmt_stat(st):
    if st is None:
        return f"{'N/A':>8}"
    return f"n={st['n']:<5} 均={st['avg']:+.2f}% 胜={st['win']:.1f}% 中={st['med']:+.2f}%"


def summarize(signals, period_desc):
    p("\n\n" + "=" * 100)
    p(f"汇总分析 — {period_desc}")
    p("=" * 100)
    p(f"信号总数(满足尾盘拉升条件): {len(signals)}")
    if not signals:
        p("无信号, 结束。")
        return

    # ---------- 一、次日高开率 ----------
    p("\n" + "-" * 100)
    p("一、次日(D+1)高开率 —— 检验'游资尾盘布局→次日高开'假设")
    p("-" * 100)
    have_d1 = [s for s in signals if 1 in s['fut'] and s['fut'][1].get('open_rate') is not None]
    n_d1 = len(have_d1)
    n_gap = sum(1 for s in have_d1 if s['fut'][1]['open_rate'] >= 0)
    n_gap3 = sum(1 for s in have_d1 if s['fut'][1]['open_rate'] >= 3)
    if n_d1:
        p(f"  有D+1数据信号: {n_d1}")
        p(f"  次日高开(open_rate>=0)率: {n_gap}/{n_d1} = {n_gap/n_d1*100:.1f}%")
        p(f"  次日高开>=3%率:          {n_gap3}/{n_d1} = {n_gap3/n_d1*100:.1f}%")
        gap_rates = [s['fut'][1]['open_rate'] for s in have_d1]
        p(f"  次日开盘涨幅 均值={sum(gap_rates)/len(gap_rates):+.2f}%  "
          f"中位={sorted(gap_rates)[len(gap_rates)//2]:+.2f}%")

    # 进场信号: 次日高开>=0 且 h1_open有效
    entries = []
    for s in have_d1:
        d1 = s['fut'][1]
        if d1['open_rate'] >= 0 and d1.get('hour1_open') and d1['hour1_open'] > 0:
            entries.append(s)
    p(f"\n  满足进场条件(次日高开>=0且h1_open有效)的信号: {len(entries)}")
    if not entries:
        p("  无可进场信号。")
        return

    # ---------- 二、开高走低 vs 惯性上冲 (核心假设检验) ----------
    p("\n" + "-" * 100)
    p("二、核心假设检验: D+1 是'开高走低'(游资出货) 还是'惯性上冲'?")
    p("   (基准: D+1 hour1_open 买入价, 观察当日路径; 注: T+1规则下当日不可卖, 此为描述性分析)")
    p("-" * 100)
    r_h1c, r_close, r_high, r_low = [], [], [], []
    n_downclose = 0   # 收盘 < h1_open (开高走低)
    n_upclose = 0     # 收盘 > h1_open (惯性上冲)
    n_peak_h1 = 0     # 当日最高出现在hour1 (早盘见顶)
    n_peak_valid = 0
    for s in entries:
        d1 = s['fut'][1]
        bp = d1['hour1_open']
        h1c = d1.get('hour1_close')
        dc = d1.get('close')
        dh = d1.get('high')
        dl = d1.get('low')
        if h1c: r_h1c.append((h1c - bp) / bp * 100)
        if dc:
            rc = (dc - bp) / bp * 100
            r_close.append(rc)
            if dc < bp: n_downclose += 1
            else: n_upclose += 1
        if dh: r_high.append((dh - bp) / bp * 100)
        if dl: r_low.append((dl - bp) / bp * 100)
        # 峰值位置
        hh = [d1.get('hour1_high'), d1.get('hour2_high'), d1.get('hour3_high'), d1.get('hour4_high')]
        hh = [x for x in hh if x is not None]
        if hh and d1.get('hour1_high') is not None:
            n_peak_valid += 1
            if d1['hour1_high'] >= max(hh) - 1e-9:
                n_peak_h1 += 1

    def _avg(a): return sum(a) / len(a) if a else 0.0
    p(f"  从D+1 h1_open买入的当日路径 (n={len(entries)}):")
    p(f"    → hour1收盘 平均收益: {_avg(r_h1c):+.2f}%")
    p(f"    → 全天收盘 平均收益: {_avg(r_close):+.2f}%")
    p(f"    → 当日最高 平均(最大potential): {_avg(r_high):+.2f}%")
    p(f"    → 当日最低 平均(最大回撤): {_avg(r_low):+.2f}%")
    if r_close:
        p(f"  收盘<买入价(开高走低)占比: {n_downclose}/{len(r_close)} = {n_downclose/len(r_close)*100:.1f}%")
        p(f"  收盘>买入价(惯性上冲)占比: {n_upclose}/{len(r_close)} = {n_upclose/len(r_close)*100:.1f}%")
    if n_peak_valid:
        p(f"  当日最高价出现在hour1(早盘见顶)占比: {n_peak_h1}/{n_peak_valid} = {n_peak_h1/n_peak_valid*100:.1f}%")
    # 结论倾向
    if r_h1c and r_close:
        if _avg(r_close) < _avg(r_h1c) - 0.3:
            p("  >> 倾向: 【开高走低】—— 买入后越持越亏, hour1后走弱")
        elif _avg(r_close) > _avg(r_h1c) + 0.3:
            p("  >> 倾向: 【惯性上冲】—— hour1后仍继续上行")
        else:
            p("  >> 倾向: 【震荡/中性】—— hour1后无明显方向")

    # ---------- 三、买入时机: h1_open vs h2_open ----------
    p("\n" + "-" * 100)
    p("三、买入时机对比 (持有至 D+2 收盘)")
    p("-" * 100)
    ret_h1, ret_h2 = [], []
    for s in entries:
        d1 = s['fut'][1]
        bp1 = d1.get('hour1_open'); bp2 = d1.get('hour2_open')
        r1 = simulate(s['fut'], bp1, 2)
        if r1: ret_h1.append(r1[0])
        if bp2 and bp2 > 0:
            r2 = simulate(s['fut'], bp2, 2)
            if r2: ret_h2.append(r2[0])
    p(f"  D+1 h1_open买入 → D+2收盘: {_fmt_stat(_stats(ret_h1))}")
    p(f"  D+1 h2_open买入 → D+2收盘: {_fmt_stat(_stats(ret_h2))}")

    # ---------- 四、止盈触及率 ----------
    p("\n" + "-" * 100)
    p("四、止盈触及率 (D+1 h1_open买入, 可卖窗口 D+2~D+4 内最高价触及)")
    p("-" * 100)
    tp_hit = {tp: 0 for tp in TP_LEVELS}
    tp_total = 0
    for s in entries:
        d1 = s['fut'][1]; bp = d1['hour1_open']
        highs = []
        for off in (2, 3, 4):
            d = s['fut'].get(off)
            if d and d.get('high'):
                highs.append(d['high'])
        if not highs:
            continue
        tp_total += 1
        maxh = max(highs)
        up = (maxh - bp) / bp * 100
        for tp in TP_LEVELS:
            if up >= tp:
                tp_hit[tp] += 1
    if tp_total:
        for tp in TP_LEVELS:
            p(f"  +{tp:.0f}% 触及率: {tp_hit[tp]}/{tp_total} = {tp_hit[tp]/tp_total*100:.1f}%")

    # ---------- 五、最大回撤分布 ----------
    p("\n" + "-" * 100)
    p("五、最大回撤分布 (D+1 h1_open买入, 持有 D+1~D+4 期间最低价相对买入价)")
    p("-" * 100)
    dds = []
    for s in entries:
        d1 = s['fut'][1]; bp = d1['hour1_open']
        lows = []
        for off in (1, 2, 3, 4):
            d = s['fut'].get(off)
            if d and d.get('low'):
                lows.append(d['low'])
        if lows:
            dds.append((min(lows) - bp) / bp * 100)
    if dds:
        buckets = [(-999, -10, '<-10%'), (-10, -5, '-10~-5%'), (-5, -3, '-5~-3%'),
                   (-3, 0, '-3~0%'), (0, 999, '>=0%(无回撤)')]
        p(f"  样本={len(dds)}  平均最大回撤={sum(dds)/len(dds):+.2f}%  中位={sorted(dds)[len(dds)//2]:+.2f}%")
        for lo, hi, lbl in buckets:
            c = sum(1 for x in dds if lo <= x < hi) if lbl != '>=0%(无回撤)' else sum(1 for x in dds if x >= 0)
            if lbl == '<-10%':
                c = sum(1 for x in dds if x < -10)
            p(f"    {lbl:<12}: {c:>5} ({c/len(dds)*100:.1f}%)")

    # ---------- 六、最优持仓期 ----------
    p("\n" + "-" * 100)
    p("六、最优持仓期 (D+1 h1_open买入, 分别持有至 D+2/D+3/D+4 收盘)")
    p("-" * 100)
    for off, lbl in [(2, 'D+2收盘'), (3, 'D+3收盘'), (4, 'D+4收盘')]:
        rets = []
        for s in entries:
            bp = s['fut'][1]['hour1_open']
            r = simulate(s['fut'], bp, off)
            if r: rets.append(r[0])
        p(f"  持有至{lbl}: {_fmt_stat(_stats(rets))}")

    # ---------- 七、12种配置对比 ----------
    p("\n" + "-" * 100)
    p("七、多配置对比 (遵守T+1: 最早D+2卖出)")
    p("-" * 100)
    configs = [
        ('C01 h1买/D+2收盘',            'h1', 2, None, None),
        ('C02 h1买/D+2 TP+3%否则收盘',  'h1', 2, 3.0, None),
        ('C03 h1买/D+2 TP+5%否则收盘',  'h1', 2, 5.0, None),
        ('C04 h1买/D+2 TP+3%SL-3%',     'h1', 2, 3.0, -3.0),
        ('C05 h1买/D+3收盘',            'h1', 3, None, None),
        ('C06 h1买/D+3 TP+3%否则收盘',  'h1', 3, 3.0, None),
        ('C07 h1买/D+3 TP+5%SL-5%',     'h1', 3, 5.0, -5.0),
        ('C08 h1买/D+4收盘',            'h1', 4, None, None),
        ('C09 h1买/D+4 TP+5%SL-3%',     'h1', 4, 5.0, -3.0),
        ('C10 h1买/D+4 TP+8%SL-5%',     'h1', 4, 8.0, -5.0),
        ('C11 h2买/D+2收盘',            'h2', 2, None, None),
        ('C12 h2买/D+2 TP+3%否则收盘',  'h2', 2, 3.0, None),
        ('C13 h2买/D+3 TP+3%SL-3%',     'h2', 3, 3.0, -3.0),
    ]
    p(f"  {'配置':<28}{'样本':>6}{'均收益%':>10}{'胜率%':>9}{'中位%':>9}{'累计%':>10}")
    p("  " + "-" * 78)
    best = None
    for name, buy_key, off, tp, sl in configs:
        rets = []
        for s in entries:
            d1 = s['fut'][1]
            bp = d1.get('hour1_open') if buy_key == 'h1' else d1.get('hour2_open')
            r = simulate(s['fut'], bp, off, tp, sl)
            if r: rets.append(r[0])
        st = _stats(rets)
        if st:
            tot = sum(rets)
            p(f"  {name:<28}{st['n']:>6}{st['avg']:>+10.2f}{st['win']:>9.1f}{st['med']:>+9.2f}{tot:>+10.1f}")
            if best is None or st['avg'] > best[1]['avg']:
                best = (name, st, tot)
        else:
            p(f"  {name:<28}{'N/A':>6}")
    if best:
        p(f"\n  >> 单笔均收益最优配置: {best[0]}  均收益={best[1]['avg']:+.2f}%  "
          f"胜率={best[1]['win']:.1f}%  样本={best[1]['n']}")

    # ---------- 八、hour4涨幅分层 ----------
    p("\n" + "-" * 100)
    p("八、按 hour4涨幅 分层 (基准配置: h1买/D+2收盘)")
    p("-" * 100)
    layer = defaultdict(list)
    for s in entries:
        bp = s['fut'][1]['hour1_open']
        r = simulate(s['fut'], bp, 2)
        if r:
            layer[h4_gain_bucket(s['h4_gain'])].append(r[0])
    for lbl in ['3-5%', '5-8%', '>8%']:
        p(f"  hour4涨幅 {lbl:<6}: {_fmt_stat(_stats(layer.get(lbl, [])))}")

    # ---------- 九、市值/板块分层 ----------
    p("\n" + "-" * 100)
    p("九、按 流通市值 / 板块 分层 (基准配置: h1买/D+2收盘)")
    p("-" * 100)
    mlayer = defaultdict(list); blayer = defaultdict(list)
    for s in entries:
        bp = s['fut'][1]['hour1_open']
        r = simulate(s['fut'], bp, 2)
        if r:
            mlayer[s['mcap_group']].append(r[0])
            blayer[s['board']].append(r[0])
    for lbl in ['<50亿', '50-200亿', '200-700亿']:
        p(f"  市值 {lbl:<10}: {_fmt_stat(_stats(mlayer.get(lbl, [])))}")
    p("")
    for lbl in ['主板', '创业板', '科创板']:
        p(f"  板块 {lbl:<6}: {_fmt_stat(_stats(blayer.get(lbl, [])))}")


def summarize_yearly(signals):
    """逐年稳定性 (基准配置: h1买/D+2收盘)"""
    p("\n" + "-" * 100)
    p("十、逐年稳定性 (基准配置: h1买/D+2收盘, 仅次日高开进场)")
    p("-" * 100)
    by_year = defaultdict(list)
    by_year_cand = defaultdict(int)
    for s in signals:
        y = int(s['sig_date'][:4])
        by_year_cand[y] += 1
        if 1 not in s['fut']:
            continue
        d1 = s['fut'][1]
        if d1.get('open_rate') is None or d1['open_rate'] < 0:
            continue
        if not d1.get('hour1_open') or d1['hour1_open'] <= 0:
            continue
        r = simulate(s['fut'], d1['hour1_open'], 2)
        if r:
            by_year[y].append(r[0])
    p(f"  {'年份':<8}{'信号数':>8}{'进场样本':>10}{'均收益%':>10}{'胜率%':>9}{'累计%':>10}")
    p("  " + "-" * 56)
    pos_years = 0; tot_years = 0
    for y in range(DATA_START_YEAR, DATA_END_YEAR + 1):
        rets = by_year.get(y, [])
        st = _stats(rets)
        if st:
            tot_years += 1
            if st['avg'] > 0:
                pos_years += 1
            p(f"  {y:<8}{by_year_cand.get(y,0):>8}{st['n']:>10}{st['avg']:>+10.2f}{st['win']:>9.1f}{sum(rets):>+10.1f}")
        else:
            p(f"  {y:<8}{by_year_cand.get(y,0):>8}{'0':>10}{'N/A':>10}")
    if tot_years:
        p(f"\n  正收益年份: {pos_years}/{tot_years}")
    return pos_years, tot_years


def final_conclusion(signals):
    p("\n\n" + "=" * 100)
    p("最终结论")
    p("=" * 100)
    entries = []
    for s in signals:
        if 1 not in s['fut']:
            continue
        d1 = s['fut'][1]
        if d1.get('open_rate') is None or d1['open_rate'] < 0:
            continue
        if not d1.get('hour1_open') or d1['hour1_open'] <= 0:
            continue
        entries.append(s)
    if not entries:
        p("  无进场样本, 无法判定。")
        return

    r_h1c = []; r_close = []; n_down = 0; n_up = 0
    for s in entries:
        d1 = s['fut'][1]; bp = d1['hour1_open']
        if d1.get('hour1_close'):
            r_h1c.append((d1['hour1_close'] - bp) / bp * 100)
        if d1.get('close'):
            rc = (d1['close'] - bp) / bp * 100
            r_close.append(rc)
            if d1['close'] < bp: n_down += 1
            else: n_up += 1
    avg_h1c = sum(r_h1c) / len(r_h1c) if r_h1c else 0
    avg_close = sum(r_close) / len(r_close) if r_close else 0

    # 基准配置收益
    base = [simulate(s['fut'], s['fut'][1]['hour1_open'], 2) for s in entries]
    base = [r[0] for r in base if r]
    base_avg = sum(base) / len(base) if base else 0
    base_win = sum(1 for r in base if r > 0) / len(base) * 100 if base else 0

    p(f"  进场样本数: {len(entries)}")
    p(f"  D+1 h1_open→hour1收盘 均值: {avg_h1c:+.2f}%")
    p(f"  D+1 h1_open→全天收盘 均值: {avg_close:+.2f}%")
    if r_close:
        p(f"  开高走低占比: {n_down/len(r_close)*100:.1f}%  惯性上冲占比: {n_up/len(r_close)*100:.1f}%")

    p("\n  【模式判定】")
    if avg_close < avg_h1c - 0.3:
        pattern = '开高走低'
        p("  >> D+1 呈【开高走低】: hour1后持续走弱, 印证'游资次日出货'。")
        p("     在 h1_open 追入会买在相对高位, 单纯h1_open买入+持有并不占优。")
    elif avg_close > avg_h1c + 0.3:
        pattern = '惯性上冲'
        p("  >> D+1 呈【惯性上冲】: hour1后仍上行, h1_open买入具备正向惯性。")
    else:
        pattern = '震荡中性'
        p("  >> D+1 呈【震荡/中性】: hour1后无明显方向。")

    p("\n  【策略可行性 — 基准配置 h1买/D+2收盘】")
    p(f"    单笔均收益 {base_avg:+.2f}%, 胜率 {base_win:.1f}%")
    feasible = base_avg > 0.8 and base_win > 52
    if feasible:
        p("    >> 具备一定正期望, 值得进一步优化止盈止损与选股分层。")
    else:
        p("    >> 单笔期望偏弱/胜率不足, 直接h1_open追入不成立; ")
        p("       建议结合分层(市值/hour4涨幅)择优, 或改为回调低吸而非追高。")
    p(f"\n  综合: 尾盘拉升次日 主导模式=【{pattern}】。")


def main():
    global _LOG_FH
    if len(sys.argv) < 2:
        print("用法: python3 research_tail_momentum_r2.py <2025-03 | 2025 | all>")
        sys.exit(1)
    arg = sys.argv[1].strip()

    # 解析period
    detail_mode = False
    if arg == 'all':
        period_desc = f"全周期 {DATA_START_YEAR}-{DATA_END_YEAR}"
        pf = lambda y, m: DATA_START_YEAR <= y <= DATA_END_YEAR
    elif '-' in arg:
        yy, mm = arg.split('-')
        yy = int(yy); mm = int(mm)
        period_desc = f"{yy}年{mm}月"
        pf = lambda y, m, _y=yy, _m=mm: y == _y and m == _m
        detail_mode = True
    else:
        yy = int(arg)
        period_desc = f"{yy}年"
        pf = lambda y, m, _y=yy: y == _y

    os.makedirs(os.path.dirname(LOG_PATH), exist_ok=True)
    _LOG_FH = open(LOG_PATH, 'w', encoding='utf-8')

    p("#" * 100)
    p("# 尾盘拉升(hour4异动)次日策略研究 R2")
    p(f"# 研究区间: {period_desc}")
    p(f"# 信号: hour4涨幅>={HOUR4_GAIN_MIN}%, 0<全天涨幅<涨停阈值(主板8%/双创18%), "
      f"换手>={TURN_MIN}%, 流通市值<={MCAP_MAX}亿, 排除ST/北交所")
    p(f"# 交易: 次日D+1高开>=0进场, h1_open买入; 遵守A股T+1(最早D+2卖出)")
    p("#" * 100)

    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    all_days = get_all_trading_days(cur)
    day_index = {d: i for i, d in enumerate(all_days)}
    progress(f"数据库交易日: {len(all_days)} ({all_days[0]}~{all_days[-1]})")

    progress("开始扫描信号...")
    signals, ndays = collect_period_signals(cur, all_days, day_index, pf)
    progress(f"扫描完成: {ndays} 个交易日, {len(signals)} 个信号")
    p(f"\n扫描交易日数: {ndays}, 命中信号数: {len(signals)}")

    if detail_mode and signals:
        # 单月模式打印明细(限制数量以免过大)
        MAX_DETAIL = 60
        print_candidate_details(cur, signals[:MAX_DETAIL], all_days)
        if len(signals) > MAX_DETAIL:
            p(f"\n(候选明细仅打印前{MAX_DETAIL}只, 共{len(signals)}只)")

    summarize(signals, period_desc)
    summarize_yearly(signals)
    final_conclusion(signals)

    conn.close()
    p("\n" + "#" * 100)
    p("# 研究完成")
    p("#" * 100)
    _LOG_FH.close()
    progress(f"完成, 日志写入: {LOG_PATH}")


if __name__ == '__main__':
    main()
