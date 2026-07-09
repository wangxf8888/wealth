#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
尾盘放量拉升(hour4 量能确认)次日策略研究  —— Task #102

策略思路
--------
尾盘 hour4(14:00-15:00) 出现价格拉升 + 成交量显著放大。
与"纯价格尾盘拉升"不同, 这里要求量能同步放大, 说明是真实资金入场而非虚拉。

历史教训: 项目中"尾盘拉升"策略曾被否决(72%次日低开), 但那个版本
          【没有用 hour 级 amount 做过滤】。本脚本用 hour4_amount 做量能确认,
          并与"纯价格版"直接对比, 检验加量能确认后是否更优。

信号定义 (信号日 D = 尾盘异动日, 即用户口径的"昨日")
----------------------------------------------------
  [价格] hour4_close > hour3_close * 1.03            (尾盘涨 >= 3%)
  [量能] hour4_amount >= (h1_amt+h2_amt+h3_amt)/3 * 3  (即 >= 前三小时之和)
  [板块] 创业板 (sz.300 / sz.301)
  [市值] 流通市值 50 - 300 亿 (换手率反推)
  [非涨停] close < round(preclose*1.20, 2)
  [温和] close_rate 在 [-2%, +8%]
  [排除] isST=1

两个版本对比:
  PRICE 版  = 仅价格条件 (hour4_close>hour3_close*1.03) + 板块/市值/非涨停/温和/非ST
  VOL   版  = PRICE 版 + hour4 量能确认

A股 T+1 / rule2 合规
--------------------
  信号日 D 收盘后判定, 次日 D+1(=用户口径"T日") hour1_open 买入。
  T+1: 买入当日不可卖, 最早 D+2 才能卖出。
    - 可执行卖出配置: D+2 / D+3 收盘 或 止盈止损。
    - "买入当日(D+1) hour4/收盘" 仅作【描述性】路径分析(不成交, T+0非法)。

用法
----
  python3 strategy_tail_vol_surge.py 2026-04   # 单月(打印候选股前5后5日hour明细)
  python3 strategy_tail_vol_surge.py 2025       # 单年(仅汇总)
  python3 strategy_tail_vol_surge.py all        # 全周期(2021-2026, 仅汇总)

输出日志: /home/AIWealth/scripts/logs/tail_vol_surge.log
"""
import sys
import os
import sqlite3
from collections import defaultdict

# ==================== 配置区 ====================
DB_PATH = '/home/AIWealth/data/stocks.db'
LOG_PATH = '/home/AIWealth/scripts/logs/tail_vol_surge.log'

HOUR4_PRICE_RATIO = 1.03    # hour4_close / hour3_close 下限 (尾盘涨>=3%)
VOL_MULT = 3.0              # hour4_amount >= 前三小时均值 * VOL_MULT
MCAP_MIN = 50.0             # 流通市值下限(亿)
MCAP_MAX = 300.0            # 流通市值上限(亿)
DAY_GAIN_MIN = -2.0         # 全天涨幅下限(%)
DAY_GAIN_MAX = 8.0          # 全天涨幅上限(%)
LIMIT_UP_RATIO = 1.20      # 创业板涨停: preclose*1.20 (round 2位)

# 止盈档位(%) 用于触及率统计
TP_LEVELS = [3.0, 5.0, 8.0, 10.0]

DATA_START_YEAR = 2021
DATA_END_YEAR = 2026
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


def is_gem(code):
    """仅创业板 sz.300 / sz.301"""
    return code.startswith('sz.300') or code.startswith('sz.301')


def estimate_mcap(close, volume, turn):
    """换手率反推流通市值(亿): turn(%)=volume/流通股*100"""
    if close is None or volume is None or turn is None or turn <= 0:
        return None
    return close * volume * 100.0 / turn / 1e8


def get_all_trading_days(cur):
    cur.execute("SELECT DISTINCT date FROM stock_kline ORDER BY date")
    return [r[0] for r in cur.fetchall()]


# 未来日需要取的列 (含 hour4 用于描述性路径 + 买入价)
FUT_COLS = ['open', 'high', 'low', 'close', 'preclose', 'open_rate', 'close_rate',
            'hour1_open', 'hour1_high', 'hour1_low', 'hour1_close',
            'hour2_open', 'hour2_high', 'hour3_high',
            'hour4_high', 'hour4_close']
_FUT_SQL = ','.join(FUT_COLS)


def fetch_future_days(cur, code, dates):
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
    """
    筛选信号日 day 的候选股。
    返回 (price_signals, vol_signals):
      price_signals: 仅满足价格版条件
      vol_signals  : 价格版 + 量能确认 (vol_signals 是 price_signals 子集)
    """
    cur.execute("""
        SELECT code, code_name, preclose, open, close, close_rate, volume, turn,
               hour3_close, hour4_close,
               hour1_amount, hour2_amount, hour3_amount, hour4_amount, isST
        FROM stock_kline
        WHERE date=? AND isST=0 AND preclose>0
          AND hour3_close IS NOT NULL AND hour3_close>0
          AND hour4_close IS NOT NULL
          AND close_rate IS NOT NULL AND volume>0 AND turn>0
    """, (day,))

    price_sigs = []
    vol_sigs = []
    for row in cur.fetchall():
        (code, name, preclose, open_p, close, close_rate, volume, turn,
         h3c, h4c, h1a, h2a, h3a, h4a, isST) = row

        if not is_gem(code):
            continue
        if name and 'ST' in name.upper():
            continue

        # 非涨停 (创业板 20%)
        limit_up = round(preclose * LIMIT_UP_RATIO, 2)
        if close >= limit_up:
            continue

        # 全天涨幅温和
        if close_rate < DAY_GAIN_MIN or close_rate > DAY_GAIN_MAX:
            continue

        # 尾盘价格拉升: hour4_close > hour3_close * 1.03
        if h4c <= h3c * HOUR4_PRICE_RATIO:
            continue

        # 市值
        mcap = estimate_mcap(close, volume, turn)
        if mcap is None or mcap < MCAP_MIN or mcap > MCAP_MAX:
            continue

        h4_gain = (h4c - h3c) / h3c * 100.0

        sig = {
            'code': code, 'name': name,
            'close': close, 'close_rate': close_rate,
            'h4_gain': h4_gain, 'turn': turn,
            'mcap': mcap,
            'h4_amt': h4a,
        }
        price_sigs.append(sig)

        # 量能确认: 需 h1~h3 amount 均有效
        if None in (h1a, h2a, h3a, h4a):
            continue
        mean3 = (h1a + h2a + h3a) / 3.0
        if mean3 <= 0:
            continue
        vol_ratio = h4a / mean3
        if vol_ratio >= VOL_MULT:
            sig2 = dict(sig)
            sig2['vol_ratio'] = vol_ratio
            vol_sigs.append(sig2)

    return price_sigs, vol_sigs


def vol_ratio_bucket(r):
    if r < 4.0:
        return '3-4x'
    if r < 6.0:
        return '4-6x'
    return '>=6x'


def simulate(fut, buy_price, sell_offset, tp_pct=None, sl_pct=None):
    """
    模拟一笔交易 (T+1: 买入日=offset1, 最早 offset2 卖出)
    fut: {offset: daydict}, offset1=买入日D+1
    止损优先(保守)。返回 (收益%, 出场offset, 出场类型) 或 None
    """
    if buy_price is None or buy_price <= 0:
        return None
    tp_price = buy_price * (1 + tp_pct / 100.0) if tp_pct is not None else None
    sl_price = buy_price * (1 + sl_pct / 100.0) if sl_pct is not None else None

    for off in range(2, sell_offset + 1):
        d = fut.get(off)
        if d is None:
            continue
        lo = d.get('low')
        hi = d.get('high')
        if sl_price is not None and lo is not None and lo <= sl_price:
            return (sl_price - buy_price) / buy_price * 100.0, off, 'SL'
        if tp_price is not None and hi is not None and hi >= tp_price:
            return (tp_price - buy_price) / buy_price * 100.0, off, 'TP'

    d = fut.get(sell_offset)
    if d and d.get('close'):
        return (d['close'] - buy_price) / buy_price * 100.0, sell_offset, 'CLOSE'
    return None


def collect_period_signals(cur, all_days, period_filter):
    """
    遍历符合period的交易日, 返回 (price_results, vol_results, total_days)
    每个 result 元素带未来数据 fut。
    """
    price_results = []
    vol_results = []
    total_days = 0
    for i, day in enumerate(all_days):
        if i < 6 or i + 1 >= len(all_days):
            continue
        y = int(day[:4]); m = int(day[5:7])
        if not period_filter(y, m):
            continue
        total_days += 1
        price_sigs, vol_sigs = find_signals(cur, day)
        if not price_sigs:
            continue
        fut_dates = all_days[i + 1:i + 5]

        # 为 price 信号构建 fut (vol 是子集, 用 code 复用)
        fut_cache = {}
        for s in price_sigs:
            fut_rows = fetch_future_days(cur, s['code'], fut_dates)
            fut = {}
            for off, fd in enumerate(fut_dates, start=1):
                if fd in fut_rows:
                    fut[off] = fut_rows[fd]
            fut_cache[s['code']] = fut
            s['sig_date'] = day
            s['sig_idx'] = i
            s['fut'] = fut
            price_results.append(s)
        for s in vol_sigs:
            s['sig_date'] = day
            s['sig_idx'] = i
            s['fut'] = fut_cache.get(s['code'], {})
            vol_results.append(s)

        if total_days % 100 == 0:
            progress(f"  已扫描 {total_days} 交易日, price信号 {len(price_results)}, vol信号 {len(vol_results)}...")
    return price_results, vol_results, total_days


# ---------------------------------------------------------------------------
# 候选股明细打印 (前5后5日 hour级OHLC) — 仅单月模式, 打印 VOL 版信号
# ---------------------------------------------------------------------------
def print_candidate_details(cur, signals, all_days):
    p("\n" + "=" * 110)
    p("候选股明细 (VOL版信号日D 前5后5日 hour级 OHLC; rate 相对 D日 open 的 %)")
    p("=" * 110)
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
        # D日 open 作为基准
        base_open = None
        for row in rows:
            if row[0] == s['sig_date']:
                base_open = row[1]
                break
        p("\n" + "-" * 110)
        p(f"[{s['code']} {s['name']}] 信号日D={s['sig_date']} "
          f"hour4涨幅(vs h3c)={s['h4_gain']:.2f}% 量比(h4/mean3)={s.get('vol_ratio',0):.2f}x "
          f"全天涨幅={s['close_rate']:.2f}% 换手={s['turn']:.2f}% 流通市值={s['mcap']:.0f}亿")
        p(f"{'日期':<12}{'标记':<5}{'开%':>7}{'收%':>7} | "
          f"{'h1 o/h/l/c(rate%)':>28} | {'h2':>28} | {'h3':>28} | {'h4':>28}")
        for row in rows:
            d = {detail_cols[k]: row[k + 1] for k in range(len(detail_cols))}
            date = row[0]
            mark = '***' if date == s['sig_date'] else ('-' if date < s['sig_date'] else '+')

            def hr(pre):
                def rr(x):
                    if x is None or base_open in (None, 0):
                        return '  -  '
                    return f"{(x-base_open)/base_open*100:+.1f}"
                return (f"{rr(d.get(pre+'_open'))}/{rr(d.get(pre+'_high'))}/"
                        f"{rr(d.get(pre+'_low'))}/{rr(d.get(pre+'_close'))}")

            orate = d.get('open_rate'); crate = d.get('close_rate')
            os_ = f"{orate:+.1f}" if orate is not None else '  -'
            cs_ = f"{crate:+.1f}" if crate is not None else '  -'
            p(f"{date:<12}{mark:<5}{os_:>7}{cs_:>7} | "
              f"{hr('hour1'):>28} | {hr('hour2'):>28} | {hr('hour3'):>28} | {hr('hour4'):>28}")


# ---------------------------------------------------------------------------
# 统计工具
# ---------------------------------------------------------------------------
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
        return "N/A"
    return f"n={st['n']:<5} 均={st['avg']:+.2f}% 胜={st['win']:.1f}% 中={st['med']:+.2f}%"


def build_entries(signals, require_low_open=False):
    """
    构建进场样本。
    require_low_open=False: 无附加过滤(全部有D+1 h1_open的信号进场)
    require_low_open=True : 要求 D+1 低开 (open_rate<0) 作为买点确认
    """
    entries = []
    for s in signals:
        if 1 not in s['fut']:
            continue
        d1 = s['fut'][1]
        if not d1.get('hour1_open') or d1['hour1_open'] <= 0:
            continue
        if require_low_open:
            if d1.get('open_rate') is None or d1['open_rate'] >= 0:
                continue
        entries.append(s)
    return entries


# 交易配置定义: (名称, 买入key, 卖出offset, tp, sl)
CONFIGS = [
    ('C01 h1买/D+2收盘',        'h1', 2, None, None),
    ('C02 h1买/D+2 TP5%',       'h1', 2, 5.0, None),
    ('C03 h1买/D+2 TP5%SL-3%',  'h1', 2, 5.0, -3.0),
    ('C04 h1买/D+3收盘',        'h1', 3, None, None),
    ('C05 h1买/D+3 TP5%',       'h1', 3, 5.0, None),
    ('C06 h1买/D+3 TP8%SL-5%',  'h1', 3, 8.0, -5.0),
    ('C07 h1买/D+3 TP10%SL-5%', 'h1', 3, 10.0, -5.0),
    ('C08 h1买/D+4 TP8%SL-5%',  'h1', 4, 8.0, -5.0),
]


def run_configs(entries, label):
    """对一组进场样本跑所有配置, 返回 {配置名: st}, 并打印表格"""
    p(f"\n  [{label}] 进场样本={len(entries)}")
    p(f"  {'配置':<24}{'样本':>6}{'均收益%':>10}{'胜率%':>9}{'中位%':>9}{'累计%':>10}")
    p("  " + "-" * 68)
    result = {}
    best = None
    for name, buy_key, off, tp, sl in CONFIGS:
        rets = []
        for s in entries:
            d1 = s['fut'][1]
            bp = d1.get('hour1_open') if buy_key == 'h1' else d1.get('hour2_open')
            r = simulate(s['fut'], bp, off, tp, sl)
            if r:
                rets.append(r[0])
        st = _stats(rets)
        result[name] = st
        if st:
            tot = sum(rets)
            p(f"  {name:<24}{st['n']:>6}{st['avg']:>+10.2f}{st['win']:>9.1f}{st['med']:>+9.2f}{tot:>+10.1f}")
            if best is None or st['avg'] > best[1]['avg']:
                best = (name, st)
        else:
            p(f"  {name:<24}{'N/A':>6}")
    if best:
        p(f"  >> [{label}] 单笔均收益最优: {best[0]}  {_fmt_stat(best[1])}")
    return result, best


def describe_nextday_path(entries, label):
    """描述性: D+1 高开率 + D+1 买入日内路径 (T+0不成交, 仅观察)"""
    n = len(entries)
    if n == 0:
        p(f"  [{label}] 无样本")
        return
    n_gap = sum(1 for s in entries if s['fut'][1].get('open_rate') is not None and s['fut'][1]['open_rate'] >= 0)
    n_low = n - n_gap
    p(f"  [{label}] n={n}  次日高开(open_rate>=0): {n_gap} ({n_gap/n*100:.1f}%)  "
      f"次日低开: {n_low} ({n_low/n*100:.1f}%)")


# ---------------------------------------------------------------------------
# 汇总对比: PRICE 版 vs VOL 版
# ---------------------------------------------------------------------------
def summarize_compare(price_sigs, vol_sigs, period_desc):
    p("\n\n" + "=" * 110)
    p(f"汇总对比 — {period_desc}")
    p("=" * 110)
    p(f"PRICE版信号(仅价格): {len(price_sigs)}   VOL版信号(+量能确认): {len(vol_sigs)}")
    if len(price_sigs) == 0:
        p("PRICE版无信号, 结束。")
        return None

    # ---------- 一、次日高开/低开率对比 (核心: 历史否决理由是72%次日低开) ----------
    p("\n" + "-" * 110)
    p("一、次日(D+1)高开率对比 —— 历史纯价格版被否决理由是'72%次日低开'")
    p("-" * 110)
    pe_all = build_entries(price_sigs)
    ve_all = build_entries(vol_sigs)
    describe_nextday_path(pe_all, 'PRICE版')
    describe_nextday_path(ve_all, 'VOL版  ')

    # ---------- 二、无附加过滤: 全配置对比 ----------
    p("\n" + "-" * 110)
    p("二、交易配置对比 (次日 h1_open 买入, 无低开过滤; 遵守T+1最早D+2卖出)")
    p("-" * 110)
    p("\n  >>> PRICE 版 (纯价格) <<<")
    price_res, price_best = run_configs(pe_all, 'PRICE')
    p("\n  >>> VOL 版 (价格+量能确认) <<<")
    vol_res, vol_best = run_configs(ve_all, 'VOL')

    # ---------- 三、加低开过滤 (VOL版, T日低开作买点确认) ----------
    p("\n" + "-" * 110)
    p("三、VOL版 + 次日低开(open_rate<0)买点确认 对比")
    p("-" * 110)
    ve_low = build_entries(vol_sigs, require_low_open=True)
    describe_nextday_path(ve_low, 'VOL低开')
    vol_low_res, vol_low_best = run_configs(ve_low, 'VOL+低开')

    # ---------- 四、止盈触及率 (VOL版, h1买入, D+2~D+4窗口) ----------
    p("\n" + "-" * 110)
    p("四、止盈触及率 (VOL版, D+1 h1_open买入, 可卖窗口 D+2~D+4 最高价)")
    p("-" * 110)
    tp_hit = {tp: 0 for tp in TP_LEVELS}; tp_total = 0
    for s in ve_all:
        bp = s['fut'][1]['hour1_open']
        highs = [s['fut'][o].get('high') for o in (2, 3, 4) if o in s['fut'] and s['fut'][o].get('high')]
        if not highs:
            continue
        tp_total += 1
        up = (max(highs) - bp) / bp * 100
        for tp in TP_LEVELS:
            if up >= tp:
                tp_hit[tp] += 1
    if tp_total:
        for tp in TP_LEVELS:
            p(f"  +{tp:>4.0f}% 触及率: {tp_hit[tp]}/{tp_total} = {tp_hit[tp]/tp_total*100:.1f}%")

    # ---------- 五、量比分层 (VOL版, 基准 h1买/D+2收盘) ----------
    p("\n" + "-" * 110)
    p("五、按 hour4量比(h4_amt/前3小时均值) 分层 (VOL版, 基准 h1买/D+2收盘)")
    p("-" * 110)
    layer = defaultdict(list)
    for s in ve_all:
        bp = s['fut'][1]['hour1_open']
        r = simulate(s['fut'], bp, 2)
        if r:
            layer[vol_ratio_bucket(s.get('vol_ratio', 0))].append(r[0])
    for lbl in ['3-4x', '4-6x', '>=6x']:
        p(f"  量比 {lbl:<6}: {_fmt_stat(_stats(layer.get(lbl, [])))}")

    return {
        'price_best': price_best, 'vol_best': vol_best, 'vol_low_best': vol_low_best,
        'pe_all': pe_all, 've_all': ve_all, 've_low': ve_low,
    }


def summarize_yearly(price_sigs, vol_sigs):
    """逐年稳定性对比 (基准配置: h1买/D+2收盘, 无低开过滤)"""
    p("\n" + "-" * 110)
    p("六、逐年稳定性对比 (基准配置: h1买/D+2收盘)")
    p("-" * 110)

    def yearly(sigs):
        by_year = defaultdict(list)
        cand = defaultdict(int)
        for s in sigs:
            y = int(s['sig_date'][:4])
            cand[y] += 1
            if 1 not in s['fut']:
                continue
            d1 = s['fut'][1]
            if not d1.get('hour1_open') or d1['hour1_open'] <= 0:
                continue
            r = simulate(s['fut'], d1['hour1_open'], 2)
            if r:
                by_year[y].append(r[0])
        return by_year, cand

    p_year, p_cand = yearly(price_sigs)
    v_year, v_cand = yearly(vol_sigs)

    p(f"  {'年份':<6}| {'PRICE信号':>9}{'PRICE均%':>10}{'PRICE胜%':>9} | {'VOL信号':>8}{'VOL均%':>9}{'VOL胜%':>8}")
    p("  " + "-" * 74)
    v_pos = 0; v_tot = 0
    for y in range(DATA_START_YEAR, DATA_END_YEAR + 1):
        ps = _stats(p_year.get(y, [])); vs = _stats(v_year.get(y, []))
        ps_s = f"{ps['avg']:>+10.2f}{ps['win']:>9.1f}" if ps else f"{'N/A':>10}{'':>9}"
        vs_s = f"{vs['avg']:>+9.2f}{vs['win']:>8.1f}" if vs else f"{'N/A':>9}{'':>8}"
        p(f"  {y:<6}| {p_cand.get(y,0):>9}{ps_s} | {v_cand.get(y,0):>8}{vs_s}")
        if vs:
            v_tot += 1
            if vs['avg'] > 0:
                v_pos += 1
    p(f"\n  VOL版正收益年份: {v_pos}/{v_tot}")
    return v_pos, v_tot


def final_conclusion(cmp_res, yearly_res, period_desc):
    p("\n\n" + "=" * 110)
    p("最终结论 — 加量能确认后是否比纯价格版更好?")
    p("=" * 110)
    if cmp_res is None:
        p("  无信号, 无法判定。")
        return
    pb = cmp_res['price_best']; vb = cmp_res['vol_best']; vlb = cmp_res['vol_low_best']

    # 高开率对比
    def gap_rate(entries):
        n = len(entries)
        if n == 0:
            return None, 0
        g = sum(1 for s in entries if s['fut'][1].get('open_rate') is not None and s['fut'][1]['open_rate'] >= 0)
        return g / n * 100, n
    pg, pn = gap_rate(cmp_res['pe_all'])
    vg, vn = gap_rate(cmp_res['ve_all'])

    p("\n  【1. 次日高开率】(历史否决线: 纯价格版72%次日低开=28%高开)")
    if pg is not None:
        p(f"    PRICE版: 高开率 {pg:.1f}% (低开 {100-pg:.1f}%), n={pn}")
    if vg is not None:
        p(f"    VOL版  : 高开率 {vg:.1f}% (低开 {100-vg:.1f}%), n={vn}")
    if pg is not None and vg is not None:
        if vg > pg + 2:
            p(f"    >> 量能确认【改善】次日高开率 (+{vg-pg:.1f}pp)")
        elif vg < pg - 2:
            p(f"    >> 量能确认【恶化】次日高开率 ({vg-pg:.1f}pp)")
        else:
            p("    >> 量能确认对次日高开率无显著影响")

    p("\n  【2. 最优配置单笔均收益对比】")
    if pb:
        p(f"    PRICE版最优 : {pb[0]}  {_fmt_stat(pb[1])}")
    if vb:
        p(f"    VOL版最优   : {vb[0]}  {_fmt_stat(vb[1])}")
    if vlb:
        p(f"    VOL+低开最优: {vlb[0]}  {_fmt_stat(vlb[1])}")

    # 判定量能是否更优
    p("\n  【3. 量能确认是否更优】")
    if pb and vb:
        d_avg = vb[1]['avg'] - pb[1]['avg']
        d_win = vb[1]['win'] - pb[1]['win']
        p(f"    VOL vs PRICE (各自最优): 均收益 {d_avg:+.2f}pp, 胜率 {d_win:+.1f}pp")
        if d_avg > 0.3 and vb[1]['win'] >= pb[1]['win'] - 1:
            p("    >> 量能确认【更优】: 提升单笔期望且未牺牲胜率")
        elif d_avg < -0.3:
            p("    >> 量能确认【更差】: 单笔期望反而下降")
        else:
            p("    >> 量能确认【无实质改善】: 差异在噪声范围内")

    # 达标判定 (rule2: 月化>=10% 胜率>=55%; 单笔近似换算)
    p("\n  【4. 达标判定 (rule2门槛: 月化>=10%, 胜率>=55%)】")
    best_overall = None
    for cand in (vb, vlb, pb):
        if cand and (best_overall is None or cand[1]['avg'] > best_overall[1]['avg']):
            best_overall = cand
    if best_overall:
        st = best_overall[1]
        vpos, vtot = yearly_res
        p(f"    综合最优配置: {best_overall[0]}")
        p(f"      单笔均收益={st['avg']:+.2f}%  胜率={st['win']:.1f}%  样本={st['n']}")
        p(f"      VOL版正收益年份: {vpos}/{vtot}")
        qualified = st['avg'] >= 1.5 and st['win'] >= 55 and (vtot == 0 or vpos >= vtot - 1)
        if qualified:
            p("    >> 【达标/接近达标】: 建议推进至回测引擎对接完整验证。")
        elif st['avg'] > 0.5 and st['win'] >= 50:
            p("    >> 【边际正期望但未达标】: 单笔期望/胜率不足以支撑月化10%, 需进一步分层择优或否决。")
        else:
            p("    >> 【不达标】: 加量能确认后仍无稳定alpha, 依rule2不推进。")
    p(f"\n  研究区间: {period_desc}")


def main():
    global _LOG_FH
    if len(sys.argv) < 2:
        print("用法: python3 strategy_tail_vol_surge.py <2026-04 | 2025 | all>")
        sys.exit(1)
    arg = sys.argv[1].strip()

    detail_mode = False
    if arg == 'all':
        period_desc = f"全周期 {DATA_START_YEAR}-{DATA_END_YEAR}"
        pf = lambda y, m: DATA_START_YEAR <= y <= DATA_END_YEAR
    elif '-' in arg:
        yy, mm = arg.split('-'); yy = int(yy); mm = int(mm)
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
    p("# 尾盘放量拉升(hour4量能确认)次日策略研究 — Task #102")
    p(f"# 研究区间: {period_desc}")
    p(f"# 信号(信号日D): hour4_close>hour3_close*{HOUR4_PRICE_RATIO}, close_rate∈[{DAY_GAIN_MIN},{DAY_GAIN_MAX}]%, "
      f"非涨停(preclose*{LIMIT_UP_RATIO}), 创业板, 流通市值{MCAP_MIN:.0f}-{MCAP_MAX:.0f}亿, 排除ST")
    p(f"# 量能确认(VOL版): hour4_amount >= (h1+h2+h3)/3 * {VOL_MULT}  (即>=前三小时之和)")
    p(f"# 交易: 次日D+1 hour1_open买入; T+1最早D+2卖出")
    p("#" * 110)

    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()

    # 验证 hour amount 字段
    cur.execute("SELECT COUNT(*) FROM stock_kline WHERE hour4_amount IS NOT NULL")
    n_h4a = cur.fetchone()[0]
    p(f"\n[字段验证] hour4_amount 非空记录数: {n_h4a}")
    if n_h4a == 0:
        p("!! hour4_amount 全为空, 需降级为日级amount/turn估算 —— 当前数据不满足, 终止。")
        _LOG_FH.close()
        progress("hour amount 字段为空, 终止")
        return

    all_days = get_all_trading_days(cur)
    progress(f"数据库交易日: {len(all_days)} ({all_days[0]}~{all_days[-1]})")

    progress("开始扫描信号...")
    price_sigs, vol_sigs, ndays = collect_period_signals(cur, all_days, pf)
    progress(f"扫描完成: {ndays} 交易日, PRICE {len(price_sigs)}, VOL {len(vol_sigs)}")
    p(f"扫描交易日数: {ndays}  PRICE版信号: {len(price_sigs)}  VOL版信号: {len(vol_sigs)}")

    if detail_mode and vol_sigs:
        MAX_DETAIL = 40
        print_candidate_details(cur, vol_sigs[:MAX_DETAIL], all_days)
        if len(vol_sigs) > MAX_DETAIL:
            p(f"\n(候选明细仅打印前{MAX_DETAIL}只 VOL版, 共{len(vol_sigs)}只)")

    cmp_res = summarize_compare(price_sigs, vol_sigs, period_desc)
    yearly_res = summarize_yearly(price_sigs, vol_sigs)
    final_conclusion(cmp_res, yearly_res, period_desc)

    conn.close()
    p("\n" + "#" * 110)
    p("# 研究完成")
    p("#" * 110)
    _LOG_FH.close()
    progress(f"完成, 日志写入: {LOG_PATH}")


if __name__ == '__main__':
    main()
