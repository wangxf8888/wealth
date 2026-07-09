#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
大跌日尾盘放量 + 次日低开买入 策略研究 (Task #108)

策略思路
--------
昨日(信号日D)大跌(全天涨幅<=-5%)且尾盘(hour4)出现大资金抄底
(hour4_amount 相对近5日均值异常放大), 今日(T=D+1)低开(open<昨日close)。
逻辑: 大跌中尾盘放量 = 聪明资金入场; 今日低开 = 恐慌散户出逃, 提供更好买点。
今日 hour1_open 买入, 遵守 A股T+1(买入次日才可卖)。

信号定义
--------
  信号日 D:
    - 全天大跌: close_rate <= -5.0 (即 (close-preclose)/preclose <= -5%)
    - 尾盘放量: hour4_amount >= 近5日均 hour4_amount × VOL_MULT (默认3)
    - 板块: 创业板(sz.300/sz.301) / 科创板(sh.688)
    - 流通市值 50-300 亿
    - 排除 ST
  买入日 T = D+1:
    - 低开: T.open < D.close (即 T.open < T.preclose, open_rate<0)
    - 排除开盘跌停(open<=跌停价, 无法买入)
    - T hour1_open 买入

A股规则(rule2): 买入日T当日不可卖, 最早 T+1 卖出。
持仓评估: T+1 / T+2 / T+3 收盘卖出。

★核心关注★: 信号的时间分布是否均匀。若信号集中在下跌市(某年占比>40%),
标记"聚集风险", slot=1 单仓回测可能失败。

用法
----
  python3 strategy_bigdrop_tail_buyin.py 2026-04   # 单月(打印候选股前5后5日hour明细)
  python3 strategy_bigdrop_tail_buyin.py 2026       # 单年(仅汇总)
  python3 strategy_bigdrop_tail_buyin.py all        # 全周期(仅汇总)

输出日志: /home/AIWealth/scripts/logs/bigdrop_tail_buyin.log
"""
import sys
import os
import sqlite3
from collections import defaultdict

# ==================== 配置区 ====================
DB_PATH = '/home/AIWealth/data/stocks.db'
LOG_PATH = '/home/AIWealth/scripts/logs/bigdrop_tail_buyin.log'

DAY_DROP_MAX = -5.0     # 信号日全天涨幅上限(%), <= 此值视为大跌
VOL_LOOKBACK = 5        # 尾盘量能对比回溯天数
VOL_MULT = 3.0          # hour4_amount 相对近5日均值放大倍数下限
MCAP_MIN = 50.0         # 流通市值下限(亿)
MCAP_MAX = 300.0        # 流通市值上限(亿)

BOARD_LIMIT_PCT = 0.20  # 创业板/科创板涨跌停幅度 20%

# 持仓卖出档位(相对买入日T的偏移: T+1/T+2/T+3)
HOLD_OFFSETS = [1, 2, 3]

DATA_START_YEAR = 2021
DATA_END_YEAR = 2026

CLUSTER_YEAR_RATIO = 0.40  # 单年信号占比超过此值 -> 聚集风险
# ================================================

_LOG_FH = None


def p(*args):
    s = ' '.join(str(x) for x in args)
    if _LOG_FH is not None:
        _LOG_FH.write(s + '\n')
    else:
        print(s)


def progress(msg):
    sys.stderr.write(msg + '\n')
    sys.stderr.flush()


def classify_board(code):
    """仅保留创业板/科创板; 其余返回 None(排除)"""
    if code.startswith('sz.300') or code.startswith('sz.301'):
        return '创业板'
    if code.startswith('sh.688'):
        return '科创板'
    return None


def estimate_mcap(close, volume, turn):
    """通过换手率反推流通市值(亿): turn(%)=volume/流通股*100"""
    if close is None or volume is None or turn is None or turn <= 0:
        return None
    return close * volume * 100.0 / turn / 1e8


def get_all_trading_days(cur):
    cur.execute("SELECT DISTINCT date FROM stock_kline ORDER BY date")
    return [r[0] for r in cur.fetchall()]


# 未来日(含买入日T)需要取的列
FUT_COLS = ['open', 'high', 'low', 'close', 'preclose', 'open_rate', 'close_rate',
            'hour1_open', 'hour1_high', 'hour1_low', 'hour1_close',
            'hour2_open', 'hour2_high', 'hour2_low', 'hour2_close',
            'hour3_open', 'hour3_high', 'hour3_low', 'hour3_close',
            'hour4_open', 'hour4_high', 'hour4_low', 'hour4_close']
_FUT_SQL = ','.join(FUT_COLS)


def fetch_days(cur, code, dates):
    """返回 {date: {col:val}}"""
    if not dates:
        return {}
    ph = ','.join(['?'] * len(dates))
    cur.execute(f"SELECT date,{_FUT_SQL} FROM stock_kline WHERE code=? AND date IN ({ph})",
                [code] + list(dates))
    out = {}
    for row in cur.fetchall():
        d = row[0]
        out[d] = {FUT_COLS[k]: row[k + 1] for k in range(len(FUT_COLS))}
    return out


def fetch_hist_hour4_amount(cur, code, dates):
    """返回按日期顺序的 hour4_amount 列表(过滤 None/<=0)"""
    if not dates:
        return []
    ph = ','.join(['?'] * len(dates))
    cur.execute(f"SELECT date,hour4_amount FROM stock_kline WHERE code=? AND date IN ({ph}) ORDER BY date",
                [code] + list(dates))
    return [r[1] for r in cur.fetchall() if r[1] is not None and r[1] > 0]


def find_signals(cur, day):
    """筛选信号日 day 满足'大跌+尾盘放量'的候选股(尚未校验低开/未来数据)"""
    cur.execute("""
        SELECT code, code_name, preclose, open, close, close_rate, volume, turn,
               hour4_amount, isST
        FROM stock_kline
        WHERE date=? AND isST=0 AND preclose>0
          AND close_rate IS NOT NULL AND close_rate<=?
          AND hour4_amount IS NOT NULL AND hour4_amount>0
          AND volume>0 AND turn>0
    """, (day, DAY_DROP_MAX))

    out = []
    for row in cur.fetchall():
        (code, name, preclose, open_p, close, close_rate, volume, turn,
         h4_amt, isST) = row
        board = classify_board(code)
        if board is None:
            continue
        if name and 'ST' in name.upper():
            continue
        mcap = estimate_mcap(close, volume, turn)
        if mcap is None or mcap < MCAP_MIN or mcap > MCAP_MAX:
            continue
        out.append({
            'code': code, 'name': name, 'board': board,
            'close': close, 'close_rate': close_rate,
            'h4_amt': h4_amt, 'turn': turn,
            'mcap': mcap,
        })
    return out


def _stats(rets):
    if not rets:
        return None
    n = len(rets)
    avg = sum(rets) / n
    win = sum(1 for r in rets if r > 0) / n * 100
    med = sorted(rets)[n // 2]
    return {'n': n, 'avg': avg, 'win': win, 'med': med, 'max': max(rets), 'min': min(rets)}


def _fmt_stat(st):
    if st is None:
        return f"{'N/A':>8}"
    return f"n={st['n']:<5} 均={st['avg']:+.2f}% 胜={st['win']:.1f}% 中={st['med']:+.2f}%"


def collect_period_signals(cur, all_days, period_filter):
    """
    遍历符合period的信号日D, 校验尾盘放量 + T=D+1低开 + 可买入,
    返回带买入/未来数据的进场信号列表。
    """
    results = []
    total_days = 0
    for i, day in enumerate(all_days):
        # 需要 i-VOL_LOOKBACK 历史 与 i+1..i+4 未来
        if i < VOL_LOOKBACK or i + 4 >= len(all_days):
            continue
        y = int(day[:4]); m = int(day[5:7])
        if not period_filter(y, m):
            continue
        total_days += 1
        cands = find_signals(cur, day)
        if not cands:
            continue
        hist_dates = all_days[i - VOL_LOOKBACK:i]        # 近5日(不含D)
        fut_dates = all_days[i + 1:i + 5]                # T=D+1 .. D+4
        for s in cands:
            # 1) 尾盘放量: hour4_amount >= 近5日均 × VOL_MULT
            hist_amts = fetch_hist_hour4_amount(cur, s['code'], hist_dates)
            if len(hist_amts) < VOL_LOOKBACK:
                continue
            avg_h4 = sum(hist_amts) / len(hist_amts)
            if avg_h4 <= 0 or s['h4_amt'] < avg_h4 * VOL_MULT:
                continue
            # 2) 取 T .. D+4 行情
            fut_rows = fetch_days(cur, s['code'], fut_dates)
            fut = {}
            for off, fd in enumerate(fut_dates, start=1):
                if fd in fut_rows:
                    fut[off] = fut_rows[fd]
            T = fut.get(1)  # 买入日
            if T is None:
                continue
            # 3) 今日低开: T.open < 昨日close (= T.preclose)
            t_open = T.get('open'); t_pre = T.get('preclose')
            if t_open is None or t_pre is None or t_open <= 0 or t_pre <= 0:
                continue
            if t_open >= t_pre:
                continue
            # 4) 排除开盘跌停(无法买入): open <= 跌停价 round(preclose*0.8,2)
            dn_price = round(t_pre * (1 - BOARD_LIMIT_PCT), 2)
            if t_open <= dn_price:
                continue
            # 5) 买入价 = T hour1_open
            bp = T.get('hour1_open')
            if bp is None or bp <= 0:
                continue

            s['sig_date'] = day
            s['sig_idx'] = i
            s['fut'] = fut          # off1=T(买入日), off2=T+1, off3=T+2, off4=T+3
            s['buy_price'] = bp
            s['vol_ratio'] = s['h4_amt'] / avg_h4
            s['t_open_rate'] = (t_open - t_pre) / t_pre * 100.0
            results.append(s)
        if total_days % 100 == 0:
            progress(f"  已扫描 {total_days} 个交易日, 累计进场信号 {len(results)}...")
    return results, total_days


def simulate(fut, buy_price, hold_off):
    """
    买入日 T = fut[1], 买入价=buy_price(T hour1_open)。
    hold_off: 持有至 T+hold_off 收盘卖出 (fut key = 1+hold_off)。
    返回收益% 或 None。
    """
    if buy_price is None or buy_price <= 0:
        return None
    sell_key = 1 + hold_off
    d = fut.get(sell_key)
    if d and d.get('close'):
        return (d['close'] - buy_price) / buy_price * 100.0
    return None


# ---------------------------------------------------------------------------
# 候选股明细打印 (信号日D 前5后5日 hour级OHLC)
# ---------------------------------------------------------------------------
def print_candidate_details(cur, signals, all_days):
    p("\n" + "=" * 110)
    p("候选股明细 (信号日D=大跌日 前5后5日 hour级 OHLC; T=D+1 为买入日)")
    p("=" * 110)
    detail_cols = ['open', 'open_rate', 'close', 'close_rate', 'hour4_amount',
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
        p("\n" + "-" * 110)
        p(f"[{s['code']} {s['name']}] 大跌日D={s['sig_date']} 板块={s['board']} "
          f"D全天涨幅={s['close_rate']:.2f}% 尾盘量比={s['vol_ratio']:.1f}x "
          f"T低开={s['t_open_rate']:.2f}% 换手={s['turn']:.2f}% 流通市值={s['mcap']:.0f}亿")
        p(f"{'日期':<12}{'标记':<5}{'开%':>7}{'收%':>7} | "
          f"{'h1(o/h/l/c)':>24} | {'h2':>24} | {'h3':>24} | {'h4':>24}")
        for row in rows:
            d = {detail_cols[k]: row[k + 1] for k in range(len(detail_cols))}
            date = row[0]
            if date == s['sig_date']:
                mark = 'D***'
            elif date < s['sig_date']:
                mark = '-'
            else:
                # 标记 T/T+1...
                off = win_dates.index(date) - win_dates.index(s['sig_date'])
                mark = 'T' if off == 1 else f'T+{off-1}'

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
def summarize(signals, period_desc):
    p("\n\n" + "=" * 110)
    p(f"汇总分析 — {period_desc}")
    p("=" * 110)
    p(f"进场信号总数(大跌+尾盘放量+次日低开可买入): {len(signals)}")
    if not signals:
        p("无进场信号, 结束。")
        return

    # ---------- 一、持仓期对比 (T hour1_open 买入) ----------
    p("\n" + "-" * 110)
    p("一、持仓期对比 (T=D+1 hour1_open买入, 持有至 T+1/T+2/T+3 收盘)")
    p("-" * 110)
    for off in HOLD_OFFSETS:
        rets = []
        for s in signals:
            r = simulate(s['fut'], s['buy_price'], off)
            if r is not None:
                rets.append(r)
        st = _stats(rets)
        tot = sum(rets) if rets else 0.0
        p(f"  持有至 T+{off}收盘: {_fmt_stat(st)}  累计={tot:+.1f}%")

    # ---------- 二、低开幅度分层 (基准: 持有T+1) ----------
    p("\n" + "-" * 110)
    p("二、按 T日低开幅度 分层 (基准: 持有至T+1收盘)")
    p("-" * 110)
    def open_bucket(r):
        if r > -2: return '0~-2%'
        if r > -4: return '-2~-4%'
        if r > -7: return '-4~-7%'
        return '<-7%'
    layer = defaultdict(list)
    for s in signals:
        r = simulate(s['fut'], s['buy_price'], 1)
        if r is not None:
            layer[open_bucket(s['t_open_rate'])].append(r)
    for lbl in ['0~-2%', '-2~-4%', '-4~-7%', '<-7%']:
        p(f"  低开 {lbl:<8}: {_fmt_stat(_stats(layer.get(lbl, [])))}")

    # ---------- 三、尾盘量比分层 ----------
    p("\n" + "-" * 110)
    p("三、按 D日尾盘量比(hour4_amount/近5日均) 分层 (基准: 持有至T+1收盘)")
    p("-" * 110)
    def vol_bucket(v):
        if v < 4: return '3-4x'
        if v < 6: return '4-6x'
        if v < 10: return '6-10x'
        return '>10x'
    vlayer = defaultdict(list)
    for s in signals:
        r = simulate(s['fut'], s['buy_price'], 1)
        if r is not None:
            vlayer[vol_bucket(s['vol_ratio'])].append(r)
    for lbl in ['3-4x', '4-6x', '6-10x', '>10x']:
        p(f"  量比 {lbl:<6}: {_fmt_stat(_stats(vlayer.get(lbl, [])))}")

    # ---------- 四、板块分层 ----------
    p("\n" + "-" * 110)
    p("四、按板块分层 (基准: 持有至T+1收盘)")
    p("-" * 110)
    blayer = defaultdict(list)
    for s in signals:
        r = simulate(s['fut'], s['buy_price'], 1)
        if r is not None:
            blayer[s['board']].append(r)
    for lbl in ['创业板', '科创板']:
        p(f"  {lbl:<6}: {_fmt_stat(_stats(blayer.get(lbl, [])))}")


def summarize_distribution(signals):
    """★核心★ 逐年 + 逐月 信号分布 & 逐年收益, 判断聚集风险"""
    p("\n" + "=" * 110)
    p("★★★ 逐年信号分布 & 逐年收益 (slot=1 可行性关键) ★★★")
    p("=" * 110)

    by_year = defaultdict(list)          # 年->收益(持有T+1)
    by_year_cnt = defaultdict(int)       # 年->信号数
    by_ym_cnt = defaultdict(int)         # 年月->信号数
    for s in signals:
        y = int(s['sig_date'][:4]); ym = s['sig_date'][:7]
        by_year_cnt[y] += 1
        by_ym_cnt[ym] += 1
        r = simulate(s['fut'], s['buy_price'], 1)
        if r is not None:
            by_year[y].append(r)

    total = len(signals)
    p(f"\n【逐年信号分布】总信号={total}")
    p(f"  {'年份':<8}{'信号数':>8}{'占比%':>9}{'进场样本':>10}{'均收益%':>10}{'胜率%':>9}{'累计%':>10}  聚集")
    p("  " + "-" * 78)
    pos_years = 0; tot_years = 0
    cluster_years = []
    for y in range(DATA_START_YEAR, DATA_END_YEAR + 1):
        cnt = by_year_cnt.get(y, 0)
        ratio = cnt / total * 100 if total else 0
        rets = by_year.get(y, [])
        st = _stats(rets)
        flag = ''
        if total and ratio > CLUSTER_YEAR_RATIO * 100:
            flag = '⚠聚集'
            cluster_years.append((y, ratio))
        if st:
            tot_years += 1
            if st['avg'] > 0:
                pos_years += 1
            p(f"  {y:<8}{cnt:>8}{ratio:>9.1f}{st['n']:>10}{st['avg']:>+10.2f}{st['win']:>9.1f}{sum(rets):>+10.1f}  {flag}")
        else:
            p(f"  {y:<8}{cnt:>8}{ratio:>9.1f}{'0':>10}{'N/A':>10}{'':>9}{'':>10}  {flag}")

    # 逐月分布(检查是否集中于个别月份)
    p(f"\n【逐月信号分布】(非零月)")
    p(f"  {'年月':<10}{'信号数':>8}{'占比%':>9}")
    p("  " + "-" * 27)
    max_ym = None
    for ym in sorted(by_ym_cnt.keys()):
        cnt = by_ym_cnt[ym]
        ratio = cnt / total * 100 if total else 0
        p(f"  {ym:<10}{cnt:>8}{ratio:>9.1f}")
        if max_ym is None or cnt > max_ym[1]:
            max_ym = (ym, cnt, ratio)

    # 分布均匀度评估
    p("\n【分布均匀度评估】")
    if tot_years:
        p(f"  有信号年份数: {tot_years}/{DATA_END_YEAR - DATA_START_YEAR + 1}")
        p(f"  正收益年份: {pos_years}/{tot_years}")
    if max_ym:
        p(f"  单月最多信号: {max_ym[0]} = {max_ym[1]}个 ({max_ym[2]:.1f}%)")
    if cluster_years:
        p("  ⚠ 聚集风险年份 (单年占比>40%):")
        for y, r in cluster_years:
            p(f"      {y}: {r:.1f}%")
        p("  >> 信号在个别年份聚集, slot=1 单仓回测极可能失败(信号扎堆时资金不足/踏空)。")
    else:
        p("  ✓ 无单年占比>40%, 年度分布相对均匀。")

    return pos_years, tot_years, cluster_years, total


def summarize_subset_yearly(signals, open_thr=-2.0):
    """深低开子集(T低开<=open_thr%)的逐年分布+收益; 分层显示深低开才是alpha来源"""
    subset = [s for s in signals if s['t_open_rate'] <= open_thr]
    p("\n" + "=" * 110)
    p(f"★ 深低开子集验证 (T低开<={open_thr:.0f}%) — 分层显示深低开为alpha核心, 检验其逐年稳定性")
    p("=" * 110)
    p(f"  子集信号数: {len(subset)} / 全信号 {len(signals)}")
    if not subset:
        p("  子集为空。")
        return
    by_year = defaultdict(list)
    by_year_cnt = defaultdict(int)
    for s in subset:
        y = int(s['sig_date'][:4])
        by_year_cnt[y] += 1
        r = simulate(s['fut'], s['buy_price'], 1)
        if r is not None:
            by_year[y].append(r)
    total = len(subset)
    p(f"\n  {'年份':<8}{'信号数':>8}{'占比%':>9}{'均收益%':>10}{'胜率%':>9}{'累计%':>10}  聚集")
    p("  " + "-" * 66)
    pos_years = 0; tot_years = 0; cluster = []
    for y in range(DATA_START_YEAR, DATA_END_YEAR + 1):
        cnt = by_year_cnt.get(y, 0)
        ratio = cnt / total * 100 if total else 0
        rets = by_year.get(y, [])
        st = _stats(rets)
        flag = ''
        if total and ratio > CLUSTER_YEAR_RATIO * 100:
            flag = '⚠聚集'; cluster.append((y, ratio))
        if st:
            tot_years += 1
            if st['avg'] > 0:
                pos_years += 1
            p(f"  {y:<8}{cnt:>8}{ratio:>9.1f}{st['avg']:>+10.2f}{st['win']:>9.1f}{sum(rets):>+10.1f}  {flag}")
        else:
            p(f"  {y:<8}{cnt:>8}{ratio:>9.1f}{'N/A':>10}{'':>9}{'':>10}  {flag}")
    allr = [r for s in subset for r in [simulate(s['fut'], s['buy_price'], 1)] if r is not None]
    st = _stats(allr)
    p(f"\n  子集整体(持有T+1): {_fmt_stat(st)}  累计={sum(allr):+.1f}%")
    p(f"  正收益年份: {pos_years}/{tot_years}" + ("  ⚠存在聚集" if cluster else "  ✓分布均匀"))
    return subset, st, pos_years, tot_years, cluster


def final_conclusion(signals, dist_result):
    pos_years, tot_years, cluster_years, total = dist_result
    p("\n\n" + "=" * 110)
    p("最终结论")
    p("=" * 110)
    if not signals:
        p("  无进场样本, 无法判定。")
        return

    base = [simulate(s['fut'], s['buy_price'], 1) for s in signals]
    base = [r for r in base if r is not None]
    base_avg = sum(base) / len(base) if base else 0
    base_win = sum(1 for r in base if r > 0) / len(base) * 100 if base else 0

    p(f"  进场信号总数: {total}")
    p(f"  基准(持有T+1)单笔均收益 {base_avg:+.2f}%, 胜率 {base_win:.1f}%")
    p(f"  正收益年份 {pos_years}/{tot_years}")

    p("\n  【slot=1 可行性判定】")
    feasible_ret = base_avg > 0.8 and base_win > 52
    if cluster_years:
        p("  ✗ 存在聚集风险(见上): 信号在个别年份/下跌市扎堆。")
        p("    即便等权全信号表现尚可, slot=1 单仓回测大概率失败, 不建议推进。")
    elif pos_years < max(1, tot_years) * 0.6:
        p(f"  ✗ 正收益年份不足 ({pos_years}/{tot_years}), 逐年稳定性差, 不建议推进。")
    elif not feasible_ret:
        p(f"  △ 单笔期望偏弱(均{base_avg:+.2f}%/胜{base_win:.1f}%), 分布尚可但收益不达标。")
        p("    需结合分层择优, 暂不满足 slot=1 推进门槛。")
    else:
        p(f"  ✓ 分布相对均匀且单笔期望正向(均{base_avg:+.2f}%/胜{base_win:.1f}%),")
        p("    具备进入 slot=1 回测验证的条件。")


def main():
    global _LOG_FH
    if len(sys.argv) < 2:
        print("用法: python3 strategy_bigdrop_tail_buyin.py <2026-04 | 2026 | all>")
        sys.exit(1)
    arg = sys.argv[1].strip()

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

    p("#" * 110)
    p("# 大跌日尾盘放量 + 次日低开买入 策略研究 (Task #108)")
    p(f"# 研究区间: {period_desc}")
    p(f"# 信号日D: 全天涨幅<={DAY_DROP_MAX}%, 尾盘hour4_amount>=近{VOL_LOOKBACK}日均×{VOL_MULT}, "
      f"创业板/科创板, 流通市值{MCAP_MIN:.0f}-{MCAP_MAX:.0f}亿, 排除ST")
    p(f"# 买入日T=D+1: 低开(open<昨close), 排除开盘跌停; T hour1_open买入; 遵守T+1(最早T+1卖)")
    p("#" * 110)

    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    all_days = get_all_trading_days(cur)
    progress(f"数据库交易日: {len(all_days)} ({all_days[0]}~{all_days[-1]})")

    progress("开始扫描信号...")
    signals, ndays = collect_period_signals(cur, all_days, pf)
    progress(f"扫描完成: {ndays} 个交易日, {len(signals)} 个进场信号")
    p(f"\n扫描交易日数: {ndays}, 命中进场信号数: {len(signals)}")

    if detail_mode and signals:
        MAX_DETAIL = 60
        print_candidate_details(cur, signals[:MAX_DETAIL], all_days)
        if len(signals) > MAX_DETAIL:
            p(f"\n(候选明细仅打印前{MAX_DETAIL}只, 共{len(signals)}只)")

    summarize(signals, period_desc)
    dist_result = summarize_distribution(signals)
    summarize_subset_yearly(signals, open_thr=-2.0)
    final_conclusion(signals, dist_result)

    conn.close()
    p("\n" + "#" * 110)
    p("# 研究完成")
    p("#" * 110)
    _LOG_FH.close()
    progress(f"完成, 日志写入: {LOG_PATH}")


if __name__ == '__main__':
    main()
