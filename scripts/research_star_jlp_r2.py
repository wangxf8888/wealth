#!/usr/bin/env python3
"""
科创板 J+L+P 共振策略 — 止盈止损配置研究 (rule2 流程)
========================================================
思路: 科创板(<50亿) 同时满足
  J: 5日平均换手率 < 1.0%  (换手低迷, turns[i-5:i].mean())
  L: yesterday close >= max(highs[i-20:i]) * 0.98  (20日高位)
  P: 大盘 today open > 大盘 yesterday close * 1.003  (大盘高开, sh.000001)
  Gap: (today open - yesterday close)/yesterday close >= 0.02  (跳空>=2%)
  非涨停开盘: open < round(preclose*1.20, 2)
  非ST

条件严格对齐 research_gapup_combo_resonance.py (J/L/P 定义一致)。
T+1 合规: 信号日(day0) 买入, 当日不可卖, 最早 T+1 卖出。
止盈起算从 T+1 hour1 开始。

用法:
  python3 research_star_jlp_r2.py 2024-09
  python3 research_star_jlp_r2.py 2024
  python3 research_star_jlp_r2.py all
"""
import sys
import os
import sqlite3
import numpy as np
import logging
from collections import defaultdict

# ========== 配置区 ==========
DB_PATH = '/home/AIWealth/data/stocks.db'
LOG_PATH = '/home/AIWealth/scripts/logs/star_jlp_r2.log'
LOAD_START = '2020-06-01'   # 加载起点(含lookback), 信号仍受输入范围过滤
LOAD_END = '2026-06-30'
CAP_MAX = 50e8              # 流通市值上限 50亿
J_TURN_MAX = 1.0           # 5日均换手上限 %
L_HIGH_RATIO = 0.98        # 20日高位系数
P_INDEX_RATIO = 1.003      # 大盘高开系数
GAP_MIN = 0.02             # 跳空下限 2%
STAR_LIMIT = 0.20          # 科创板涨跌停 20%
DETAIL_PREV = 5            # 明细前N日
DETAIL_POST = 5            # 明细后N日
TP_LEVELS = [3, 5, 8, 10, 15, 20]   # 止盈触及率档位 %
# ========== 配置区结束 ==========

os.makedirs(os.path.dirname(LOG_PATH), exist_ok=True)
logging.basicConfig(
    level=logging.INFO,
    format='%(message)s',
    handlers=[
        logging.FileHandler(LOG_PATH, mode='w', encoding='utf-8'),
        logging.StreamHandler()
    ]
)
log = logging.getLogger(__name__)


def parse_period(arg):
    """返回 (label, match_fn)"""
    if arg == 'all':
        return 'all', (lambda d: True)
    if len(arg) == 7 and arg[4] == '-':      # 2024-09
        return arg, (lambda d, p=arg: d.startswith(p))
    if len(arg) == 4:                        # 2024
        return arg, (lambda d, p=arg: d.startswith(p))
    raise ValueError(f"无法解析周期: {arg} (支持 2024-09 / 2024 / all)")


def load_index(conn):
    """大盘 sh.000001: date -> (open, preclose)"""
    cur = conn.cursor()
    cur.execute("""
        SELECT date, open, preclose FROM index_kline
        WHERE code='sh.000001' AND date >= '2020-01-01'
        ORDER BY date
    """)
    idx = {}
    for d, o, pc in cur.fetchall():
        if o and pc and pc > 0:
            idx[d] = (o, pc)
    log.info(f"  大盘指数天数: {len(idx)}")
    return idx


# 需要的列索引
COLS = ('date,code,code_name,preclose,open,high,low,close,volume,amount,turn,isST,'
        'hour1_open,hour1_high,hour1_low,hour1_close,'
        'hour2_open,hour2_high,hour2_low,hour2_close,'
        'hour3_open,hour3_high,hour3_low,hour3_close,'
        'hour4_open,hour4_high,hour4_low,hour4_close')
CI = {name: i for i, name in enumerate(COLS.split(','))}


def hour_bars(row):
    """从一行取 4 个小时的 (open,high,low,close)"""
    bars = []
    for h in range(1, 5):
        o = row[CI[f'hour{h}_open']] or 0
        hi = row[CI[f'hour{h}_high']] or 0
        lo = row[CI[f'hour{h}_low']] or 0
        c = row[CI[f'hour{h}_close']] or 0
        bars.append((o, hi, lo, c))
    return bars


def load_star_stocks(conn):
    cur = conn.cursor()
    cur.execute("""
        SELECT DISTINCT code FROM stock_kline
        WHERE code LIKE 'sh.688%' AND date >= ? AND date <= ?
        ORDER BY code
    """, (LOAD_START, LOAD_END))
    return [r[0] for r in cur.fetchall()]


def scan_signals(conn, codes, idx, match_fn):
    """扫描所有科创板股票, 返回满足 J+L+P+Gap 的信号列表(dict)"""
    signals = []
    cur = conn.cursor()
    CHUNK = 200
    for ci in range(0, len(codes), CHUNK):
        chunk = codes[ci:ci + CHUNK]
        ph = ','.join(['?'] * len(chunk))
        cur.execute(f"""
            SELECT {COLS} FROM stock_kline
            WHERE code IN ({ph}) AND date >= ? AND date <= ?
            ORDER BY code, date
        """, chunk + [LOAD_START, LOAD_END])
        rows = cur.fetchall()
        by_code = defaultdict(list)
        for r in rows:
            by_code[r[CI['code']]].append(r)

        for code, srows in by_code.items():
            n = len(srows)
            if n < 26:
                continue
            closes = np.array([r[CI['close']] or 0 for r in srows], dtype=np.float64)
            highs = np.array([r[CI['high']] or 0 for r in srows], dtype=np.float64)
            opens = np.array([r[CI['open']] or 0 for r in srows], dtype=np.float64)
            precloses = np.array([r[CI['preclose']] or 0 for r in srows], dtype=np.float64)
            turns = np.array([r[CI['turn']] or 0 for r in srows], dtype=np.float64)
            amounts = np.array([r[CI['amount']] or 0 for r in srows], dtype=np.float64)
            dates = [r[CI['date']] for r in srows]

            for i in range(21, n):
                today = dates[i]
                if not match_fn(today):
                    continue
                if srows[i][CI['isST']]:
                    continue
                name = srows[i][CI['code_name']] or ''
                if 'ST' in name.upper():
                    continue
                if precloses[i] <= 0 or opens[i] <= 0 or closes[i - 1] <= 0:
                    continue

                # Gap >= 2%
                yd_close = closes[i - 1]
                gap = (opens[i] - yd_close) / yd_close
                if gap < GAP_MIN:
                    continue
                # 非涨停开盘
                limit_up = round(precloses[i] * (1 + STAR_LIMIT), 2)
                if opens[i] >= limit_up:
                    continue
                # 市值 < 50亿
                if turns[i] <= 0:
                    continue
                float_cap = amounts[i] / (turns[i] / 100)
                if not (0 < float_cap < CAP_MAX):
                    continue
                # J: 5日均换手 < 1.0% (turns[i-5:i], 含yesterday)
                turn5 = turns[i - 5:i]
                if turn5.mean() >= J_TURN_MAX:
                    continue
                # L: yesterday close >= max(highs[i-20:i]) * 0.98
                high20 = highs[i - 20:i].max()
                if high20 <= 0 or yd_close < high20 * L_HIGH_RATIO:
                    continue
                # P: 大盘 today open > yesterday close * 1.003
                if today not in idx:
                    continue
                io, ipc = idx[today]
                if io <= ipc * P_INDEX_RATIO:
                    continue

                # 通过全部条件 -> 记录信号
                day0 = srows[i]
                buy_h1o = day0[CI['hour1_open']] or 0
                if buy_h1o <= 0:
                    continue
                fdays = []
                for d in range(1, DETAIL_POST + 1):
                    if i + d < n:
                        fdays.append(hour_bars(srows[i + d]))
                if not fdays:
                    continue

                detail = []
                for d in range(-DETAIL_PREV, DETAIL_POST + 1):
                    j = i + d
                    if 0 <= j < n:
                        detail.append((d, srows[j]))

                signals.append({
                    'code': code,
                    'name': name,
                    'date': today,
                    'year': today[:4],
                    'gap': gap * 100,
                    'turn5': turn5.mean(),
                    'cap': float_cap / 1e8,
                    'buy_h1o': buy_h1o,
                    'buy_h1c': day0[CI['hour1_close']] or 0,
                    'buy_h2o': day0[CI['hour2_open']] or 0,
                    'day0_hours': hour_bars(day0),
                    'fdays': fdays,
                    'detail': detail,
                })
    return signals


def sim(buy, fdays, tp=None, sl=None, hold=5, day0_hours=None, day0_trig=None, sell_first=False):
    """模拟卖出, 返回 (收益%, 持有天数). 数据不足返回 (None, 0)."""
    if not fdays or buy <= 0:
        return None, 0
    # 特殊: T+1 一开盘即卖
    if sell_first:
        o = fdays[0][0][0]
        if o <= 0:
            return None, 0
        return (o - buy) / buy * 100, 1
    # 日内触发: day0 的 h2/h3/h4 触及阈值 -> T+1 h1 开盘卖
    if day0_trig is not None and day0_hours is not None:
        reached = any(h > 0 and h >= buy * (1 + day0_trig) for (o, h, l, c) in day0_hours[1:])
        if reached:
            o = fdays[0][0][0]
            if o > 0:
                return (o - buy) / buy * 100, 1
    ndays = min(hold, len(fdays))
    for d in range(ndays):
        for (o, h, l, c) in fdays[d]:
            if sl is not None and l > 0 and l <= buy * (1 + sl):
                return sl * 100, d + 1
            if tp is not None and h > 0 and h >= buy * (1 + tp):
                return tp * 100, d + 1
    # 到期收盘卖
    last = fdays[ndays - 1]
    c = 0
    for (o, h, l, cc) in reversed(last):
        if cc > 0:
            c = cc
            break
    if c <= 0:
        return None, 0
    return (c - buy) / buy * 100, ndays


# 12 种配置定义: (名称, kwargs)
CONFIGS = [
    ('T+1开盘即卖',          dict(sell_first=True)),
    ('T+1收盘',              dict(hold=1)),
    ('T+2收盘',              dict(hold=2)),
    ('T+5收盘',              dict(hold=5)),
    ('+3%止盈/T+1收盘',      dict(tp=0.03, hold=1)),
    ('+5%止盈/T+2收盘',      dict(tp=0.05, hold=2)),
    ('+8%止盈/T+5收盘',      dict(tp=0.08, hold=5)),
    ('+5%止盈-5%止损/T+5',   dict(tp=0.05, sl=-0.05, hold=5)),
    ('+8%止盈-8%止损/T+5',   dict(tp=0.08, sl=-0.08, hold=5)),
    ('+10%止盈-8%止损/T+5',  dict(tp=0.10, sl=-0.08, hold=5)),
    ('日内+5%触发→T+1开盘',  dict(day0_trig=0.05, hold=2)),
    ('日内+8%触发→T+1开盘',  dict(day0_trig=0.08, hold=5)),
]


def fmt_row(d, row):
    """格式化一行明细: 相对天(d) + 日OHLC + 4小时OHLC"""
    tag = f"D{d:+d}" if d != 0 else "D0*"
    o = row[CI['open']] or 0
    h = row[CI['high']] or 0
    l = row[CI['low']] or 0
    c = row[CI['close']] or 0
    pc = row[CI['preclose']] or 0
    pct = (c - pc) / pc * 100 if pc > 0 else 0
    s = f"    {tag:<4} {row[CI['date']]} 日[O{o:.2f} H{h:.2f} L{l:.2f} C{c:.2f} {pct:+.1f}%] "
    parts = []
    for hh in range(1, 5):
        ho = row[CI[f'hour{hh}_open']] or 0
        hhi = row[CI[f'hour{hh}_high']] or 0
        hlo = row[CI[f'hour{hh}_low']] or 0
        hc = row[CI[f'hour{hh}_close']] or 0
        parts.append(f"h{hh}[O{ho:.2f} H{hhi:.2f} L{hlo:.2f} C{hc:.2f}]")
    return s + ' '.join(parts)


def print_details(signals):
    log.info("\n" + "=" * 120)
    log.info(f"  候选股明细 (共 {len(signals)} 个信号, 每个含前{DETAIL_PREV}日/当日D0*/后{DETAIL_POST}日 hour级OHLC)")
    log.info("=" * 120)
    for s in signals:
        log.info(f"\n[{s['date']}] {s['code']} {s['name']}  "
                 f"gap={s['gap']:+.2f}% 5日均换手={s['turn5']:.2f}% 流通市值={s['cap']:.1f}亿 "
                 f"买入h1_open={s['buy_h1o']:.2f}")
        for d, row in s['detail']:
            log.info(fmt_row(d, row))


def analyze(signals, label):
    log.info("\n" + "=" * 120)
    log.info(f"  汇总分析 [周期={label}]  信号数 = {len(signals)}")
    log.info("=" * 120)
    if not signals:
        log.info("  无信号, 跳过分析。")
        return

    # ---- 1. 买入时机对比 (T+1收盘卖出) ----
    log.info("\n----- 1. 买入时机对比 (统一 T+1 收盘卖出) -----")
    for tag, key in [('day0 h1_open', 'buy_h1o'), ('day0 h1_close', 'buy_h1c'), ('day0 h2_open', 'buy_h2o')]:
        rets = []
        for s in signals:
            r, _ = sim(s[key], s['fdays'], hold=1)
            if r is not None:
                rets.append(r)
        if rets:
            arr = np.array(rets)
            log.info(f"  {tag:<16} 样本={len(arr):>4}  均值={arr.mean():+.2f}%  "
                     f"胜率={(arr > 0).mean() * 100:.1f}%  中位={np.median(arr):+.2f}%")

    # ---- 2. 止盈触及率 (T+1~T+5 内 intraday high 触及, 基准=day0 h1_open) ----
    log.info("\n----- 2. 止盈触及率 (T+1~T+5 盘中最高触及, 基准 day0 h1_open) -----")
    n = len(signals)
    for lv in TP_LEVELS:
        cnt = 0
        for s in signals:
            buy = s['buy_h1o']
            hit = any(h > 0 and h >= buy * (1 + lv / 100)
                      for day in s['fdays'] for (o, h, l, c) in day)
            if hit:
                cnt += 1
        log.info(f"  +{lv:>2}%  触及 {cnt:>4}/{n}  = {cnt / n * 100:.1f}%")
    # day0 当日盘中触及(参考, 说明日内已冲高)
    log.info("  --- day0 当日盘中触及(参考) ---")
    for lv in [5, 8, 10]:
        cnt = 0
        for s in signals:
            buy = s['buy_h1o']
            hit = any(h > 0 and h >= buy * (1 + lv / 100) for (o, h, l, c) in s['day0_hours'])
            if hit:
                cnt += 1
        log.info(f"  day0 +{lv:>2}%  触及 {cnt:>4}/{n} = {cnt / n * 100:.1f}%")

    # ---- 3. 最大回撤分布 (T+1~T+5 盘中最低, 基准 day0 h1_open) ----
    log.info("\n----- 3. 最大回撤分布 (T+1~T+5 盘中最低 vs day0 h1_open) -----")
    dd = []
    for s in signals:
        buy = s['buy_h1o']
        lows = [l for day in s['fdays'] for (o, h, l, c) in day if l > 0]
        if lows and buy > 0:
            dd.append((min(lows) - buy) / buy * 100)
    if dd:
        arr = np.array(dd)
        buckets = [(-100, -15), (-15, -10), (-10, -5), (-5, 0), (0, 100)]
        labels = ['<-15%', '-15~-10%', '-10~-5%', '-5~0%', '>=0%']
        log.info(f"  平均最大回撤={arr.mean():+.2f}%  中位={np.median(arr):+.2f}%")
        for (lo, hi), lb in zip(buckets, labels):
            c = ((arr >= lo) & (arr < hi)).sum()
            log.info(f"    {lb:<10} {c:>4}/{len(arr)} = {c / len(arr) * 100:.1f}%")

    # ---- 4. 12种配置对比 (买入 day0 h1_open) ----
    log.info("\n----- 4. 十二种止盈止损配置对比 (买入 day0 h1_open) -----")
    log.info(f"  {'#':<3} {'配置':<24} {'样本':>5} {'均值收益':>9} {'胜率':>7} {'中位':>8} {'平均持有天':>10}")
    log.info("  " + "-" * 78)
    cfg_results = []
    for ci, (nm, kw) in enumerate(CONFIGS, 1):
        rets, holds = [], []
        for s in signals:
            r, hd = sim(s['buy_h1o'], s['fdays'], day0_hours=s['day0_hours'], **kw)
            if r is not None:
                rets.append(r)
                holds.append(hd)
        if rets:
            arr = np.array(rets)
            cfg_results.append((nm, arr.mean(), (arr > 0).mean() * 100, np.median(arr)))
            log.info(f"  {ci:<3} {nm:<24} {len(arr):>5} {arr.mean():>+8.2f}% "
                     f"{(arr > 0).mean() * 100:>6.1f}% {np.median(arr):>+7.2f}% "
                     f"{np.mean(holds):>9.2f}")

    # ---- 5. 逐年稳定性 (T+1收盘, 买入 day0 h1_open) ----
    log.info("\n----- 5. 逐年稳定性 (T+1收盘卖出, 买入 day0 h1_open) -----")
    log.info(f"  {'年份':<6} {'信号数':>6} {'均值收益':>9} {'胜率':>7} {'中位':>8}")
    log.info("  " + "-" * 44)
    by_year = defaultdict(list)
    for s in signals:
        r, _ = sim(s['buy_h1o'], s['fdays'], hold=1)
        if r is not None:
            by_year[s['year']].append(r)
    for yr in sorted(by_year.keys()):
        arr = np.array(by_year[yr])
        log.info(f"  {yr:<6} {len(arr):>6} {arr.mean():>+8.2f}% "
                 f"{(arr > 0).mean() * 100:>6.1f}% {np.median(arr):>+7.2f}%")
    # 年份信号计数(全信号, 不受T+1过滤)
    yr_cnt = defaultdict(int)
    for s in signals:
        yr_cnt[s['year']] += 1
    log.info("  --- 全信号年份分布 ---")
    for yr in sorted(yr_cnt.keys()):
        log.info(f"    {yr}: {yr_cnt[yr]} 个信号")

    # ---- 6. 最终结论 ----
    log.info("\n----- 6. 最终结论 -----")
    best = max(cfg_results, key=lambda x: x[1]) if cfg_results else None
    if best:
        log.info(f"  最优配置(按均值): {best[0]}  均值={best[1]:+.2f}% 胜率={best[2]:.1f}% 中位={best[3]:+.2f}%")
    non2024 = [s for s in signals if s['year'] != '2024']
    log.info(f"  2024年信号 = {len([s for s in signals if s['year'] == '2024'])} / {len(signals)}")
    log.info(f"  非2024年信号 = {len(non2024)}")
    if non2024:
        rets = [sim(s['buy_h1o'], s['fdays'], hold=1)[0] for s in non2024]
        rets = [r for r in rets if r is not None]
        if rets:
            arr = np.array(rets)
            log.info(f"  非2024年 T+1收盘: 均值={arr.mean():+.2f}% 胜率={(arr > 0).mean() * 100:.1f}% 样本={len(arr)}")
    else:
        log.info("  非2024年无信号 -> 该策略高度集中于2024年!")


def main():
    if len(sys.argv) < 2:
        print("用法: python3 research_star_jlp_r2.py <2024-09|2024|all>")
        sys.exit(1)
    label, match_fn = parse_period(sys.argv[1])

    log.info("=" * 120)
    log.info("  科创板 J(换手低迷)+L(20日高位)+P(大盘高开)+Gap>=2% 共振策略 — 止盈止损研究")
    log.info(f"  周期 = {label}   市值<50亿   非涨停开盘   非ST")
    log.info("=" * 120)

    conn = sqlite3.connect(DB_PATH)
    idx = load_index(conn)
    codes = load_star_stocks(conn)
    log.info(f"  科创板股票数: {len(codes)}")
    signals = scan_signals(conn, codes, idx, match_fn)
    conn.close()
    log.info(f"  命中信号数: {len(signals)}")

    signals.sort(key=lambda s: (s['date'], s['code']))
    print_details(signals)
    analyze(signals, label)
    log.info("\n研究完成。日志: " + LOG_PATH)


if __name__ == '__main__':
    main()
