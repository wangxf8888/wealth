#!/usr/bin/env python3
"""连续跌停后首次开板反弹策略 (Consecutive-LimitDown First-Open Rebound)

Task #107 / rule2 研发脚本

策略思路
--------
股票连续多日跌停(极端恐慌,卖压堰塞湖),某天首次打开跌停板(卖压枯竭),
是极端事件后的反转信号。开板当天集中出逃后若企稳,后续有反弹空间。

信号定义 (以候选买入日 T 为锚点)
------------------------------
- 开板日 D = T-1 (昨日):
    * D 本身"首次开板": 未跌停 (close > round(preclose*(1-跌停幅度),2))
    * D 之前至少连续 2 天跌停 (D-1, D-2 均跌停)
- 排除 ST (ST 仅 5% 跌停, 太容易连续跌停, 不是真正恐慌信号)
- 板块: 主板/创业板/科创板 (排除北交所); 创业板/科创板 20% 振幅更有操作价值
- T 日为候选买入日 (T+1 合规: 用 D 及之前的信号, T 日 hour1_open 买入)

跌停判定 (严格 round 规则)
--------------------------
    主板(sh.6/sz.0): close <= round(preclose*0.90, 2)
    创业板(sz.3)/科创板(sh.688): close <= round(preclose*0.80, 2)

用法
----
    # 单月: 打印 hour 级 OCHL 明细 + 统计
    python3 scripts/strategy_consdown_firstopen.py 2026-04

    # 区间: 仅统计 (2021-2026 全周期)
    python3 scripts/strategy_consdown_firstopen.py 2021-01 2026-06

    # 放宽模式 (样本不足时): 加 --relax
    python3 scripts/strategy_consdown_firstopen.py 2021-01 2026-06 --relax

输出
----
    stdout (可重定向到 scripts/logs/consdown_firstopen.log)
"""

import sqlite3
import sys
from collections import defaultdict

DB_PATH = '/home/AIWealth/data/stocks.db'

WINDOW_BEFORE = 5   # 明细: 候选日T前5个交易日
WINDOW_AFTER = 5    # 明细: 候选日T后5个交易日

MIN_CONS_DOWN = 2   # 开板日之前至少连续跌停天数


# ============================================================
# 跌停 / 涨停 判定 (严格 round 规则)
# ============================================================

def is_gem_or_star(code: str) -> bool:
    return code.startswith('sz.30') or code.startswith('sh.68')


def is_main_board_or_gem_or_star(code: str) -> bool:
    """主板/创业板/科创板 (排除北交所 bj.)"""
    return (code.startswith('sh.60') or code.startswith('sz.00')
            or code.startswith('sz.30') or code.startswith('sh.68'))


def board_name(code: str) -> str:
    if code.startswith('sz.30'):
        return '创业板'
    if code.startswith('sh.68'):
        return '科创板'
    if code.startswith('sh.60'):
        return '沪主板'
    if code.startswith('sz.00'):
        return '深主板'
    return '其他'


def limit_down_threshold(code: str) -> float:
    """跌停比值阈值 (close/preclose)."""
    return 0.80 if is_gem_or_star(code) else 0.90


def limit_up_threshold(code: str) -> float:
    """涨停比值阈值 (close/preclose)."""
    return 1.20 if is_gem_or_star(code) else 1.10


def _ratio(a, b):
    if a is None or b is None or b == 0:
        return None
    try:
        return round(float(a) / float(b), 2)
    except (TypeError, ValueError, ZeroDivisionError):
        return None


def is_limit_down(close, preclose, code: str) -> bool:
    r = _ratio(close, preclose)
    if r is None:
        return False
    return r <= limit_down_threshold(code)


def is_limit_up_price(price, preclose, code: str) -> bool:
    r = _ratio(price, preclose)
    if r is None:
        return False
    return r >= limit_up_threshold(code)


def is_one_word_down(o, h, l, c, preclose, code: str) -> bool:
    """一字跌停: 全天封死跌停 (open==high==low==close 且 跌停)"""
    if any(x is None for x in (o, h, l, c)):
        return False
    if not (float(o) == float(h) == float(l) == float(c)):
        return False
    return is_limit_down(c, preclose, code)


# ============================================================
# 数据访问 (逐日缓存, 内存友好)
# ============================================================

LIGHT_COLS = ('code', 'code_name', 'preclose', 'open', 'high', 'low',
              'close', 'close_rate', 'isST')

_day_cache = {}


def get_all_trading_days(conn):
    cur = conn.execute("SELECT DISTINCT date FROM stock_kline ORDER BY date ASC")
    return [r[0] for r in cur.fetchall()]


def fetch_day_light(conn, day: str):
    """获取某日全市场行情 (轻量列), 带缓存。返回 dict[code]->row"""
    if day in _day_cache:
        return _day_cache[day]
    cur = conn.execute(
        f"SELECT {','.join(LIGHT_COLS)} FROM stock_kline WHERE date=?",
        (day,),
    )
    cols = [d[0] for d in cur.description]
    result = {row[0]: dict(zip(cols, row)) for row in cur.fetchall()}
    # 简单容量控制: 缓存超过 60 天则清空最旧一半
    if len(_day_cache) > 60:
        for k in list(_day_cache.keys())[:30]:
            del _day_cache[k]
    _day_cache[day] = result
    return result


def count_consecutive_down_before(conn, code: str, before_day: str, max_look=12):
    """统计 before_day (不含) 之前, 该股连续跌停的天数。"""
    cur = conn.execute(
        """SELECT date, close, preclose FROM stock_kline
           WHERE code=? AND date < ? ORDER BY date DESC LIMIT ?""",
        (code, before_day, max_look),
    )
    n = 0
    for _d, close, preclose in cur.fetchall():
        if is_limit_down(close, preclose, code):
            n += 1
        else:
            break
    return n


def fetch_forward_hours(conn, code: str, dates):
    """获取指定日期的 hour 级明细 (用于分析/明细打印)"""
    if not dates:
        return {}
    ph = ','.join('?' * len(dates))
    sql = f"""
        SELECT date, open, high, low, close, close_rate, turn, preclose,
               hour1_open, hour1_high, hour1_low, hour1_close,
               hour1_open_rate, hour1_close_rate, hour1_volume, hour1_amount,
               hour2_open, hour2_high, hour2_low, hour2_close,
               hour2_open_rate, hour2_close_rate, hour2_volume, hour2_amount,
               hour3_open, hour3_high, hour3_low, hour3_close,
               hour3_open_rate, hour3_close_rate, hour3_volume, hour3_amount,
               hour4_open, hour4_high, hour4_low, hour4_close,
               hour4_open_rate, hour4_close_rate, hour4_volume, hour4_amount
        FROM stock_kline
        WHERE code=? AND date IN ({ph}) ORDER BY date ASC
    """
    cur = conn.execute(sql, [code] + list(dates))
    cols = [d[0] for d in cur.description]
    return {row[0]: dict(zip(cols, row)) for row in cur.fetchall()}


# ============================================================
# 候选筛选
# ============================================================

def screen_candidates(conn, all_days, target_days, relax=False):
    """筛选候选。

    target_days: 需要产生候选买入日 T 的日期集合。
    对每个 T, 令 D=T-1 为开板日, 检查 D 前的连续跌停。
    返回候选列表 [{...}]
    """
    day_index = {d: i for i, d in enumerate(all_days)}
    candidates = []

    for t_day in target_days:
        it = day_index.get(t_day)
        if it is None or it < MIN_CONS_DOWN or it + 1 >= len(all_days):
            continue
        d_day = all_days[it - 1]        # 开板日 D = T-1
        dm1 = all_days[it - 2]          # D-1
        dm2 = all_days[it - 3] if it - 3 >= 0 else None  # D-2

        d_rows = fetch_day_light(conn, d_day)
        dm1_rows = fetch_day_light(conn, dm1)
        t_rows = fetch_day_light(conn, t_day)

        for code, drow in d_rows.items():
            if not is_main_board_or_gem_or_star(code):
                continue

            # 排除 ST
            if drow.get('isST') == 1:
                continue
            cn = (drow.get('code_name') or '')
            if 'ST' in cn.upper():
                continue

            # 开板日 D 必须"未跌停"(首次开板)
            if is_limit_down(drow.get('close'), drow.get('preclose'), code):
                continue

            # D-1 必须跌停
            r1 = dm1_rows.get(code)
            if r1 is None or not is_limit_down(r1.get('close'), r1.get('preclose'), code):
                continue

            # 连续跌停天数 (D 之前)
            cons = count_consecutive_down_before(conn, code, d_day)

            if not relax:
                # 严格: 至少连续 2 天跌停
                if cons < MIN_CONS_DOWN:
                    continue
            else:
                # 放宽: 近3天(D-1,D-2,D-3)内至少2天跌停, 或 D-1跌停且D-2大跌(<=-8%)
                ok = False
                if cons >= MIN_CONS_DOWN:
                    ok = True
                else:
                    # 统计 D-1,D-2,D-3 跌停数
                    down_cnt = 1  # D-1 已确认跌停
                    for bk in (dm2,):
                        if bk is None:
                            continue
                        bkrow = fetch_day_light(conn, bk).get(code)
                        if bkrow and is_limit_down(bkrow.get('close'), bkrow.get('preclose'), code):
                            down_cnt += 1
                    if down_cnt >= 2:
                        ok = True
                    else:
                        # D-1跌停 + D-2大跌未封死
                        if dm2 is not None:
                            bkrow = fetch_day_light(conn, dm2).get(code)
                            cr = bkrow.get('close_rate') if bkrow else None
                            if cr is not None and float(cr) <= -8.0:
                                ok = True
                if not ok:
                    continue

            # T 日数据存在且可买入 (hour1_open 存在, 且非涨停开盘)
            trow = t_rows.get(code)
            if trow is None:
                continue

            candidates.append({
                't_day': t_day,
                'd_day': d_day,        # 开板日 = T-1
                'code': code,
                'code_name': cn,
                'board': board_name(code),
                'cons_down': cons,     # 开板日之前连续跌停天数
                'd_close': drow.get('close'),
                'd_preclose': drow.get('preclose'),
                'd_close_rate': drow.get('close_rate'),
                'idx': it,
            })
    return candidates


# ============================================================
# 分析: 前瞻收益计算
# ============================================================

def analyze_candidate(conn, all_days, cand):
    """计算候选的前瞻收益。

    主策略: T 日 hour1_open 买入 (合规: 信号来自 D 及之前)。
    卖出: T+1 / T+2 / T+3 / T+5 收盘。
    另计: T+1 hour1_open 卖出; 以及开板当天 D 的 hour4_open 买入对照。
    """
    it = cand['idx']
    code = cand['code']

    # 需要的日期: D(开板), T, T+1..T+5
    need_idx = [it - 1, it, it + 1, it + 2, it + 3, it + 4, it + 5]
    need_dates = [all_days[i] for i in need_idx if 0 <= i < len(all_days)]
    rows = fetch_forward_hours(conn, code, need_dates)

    def drow(offset):
        i = it + offset
        if 0 <= i < len(all_days):
            return rows.get(all_days[i])
        return None

    t = drow(0)
    if t is None:
        return None
    buy = t.get('hour1_open')
    if buy is None or float(buy) == 0:
        return None

    # T 日开盘涨停无法买入
    if is_limit_up_price(buy, t.get('preclose'), code):
        return None

    res = {'buy_price': float(buy)}

    def ret_close(offset):
        r = drow(offset)
        if r is None or r.get('close') is None:
            return None
        return (float(r['close']) / float(buy) - 1.0) * 100.0

    res['ret_T1'] = ret_close(1)
    res['ret_T2'] = ret_close(2)
    res['ret_T3'] = ret_close(3)
    res['ret_T5'] = ret_close(5)

    # T+1 开盘卖出
    r1 = drow(1)
    res['ret_T1open'] = None
    if r1 is not None and r1.get('hour1_open') is not None:
        res['ret_T1open'] = (float(r1['hour1_open']) / float(buy) - 1.0) * 100.0

    # 对照: 开板当天 D 的 hour4_open 买入, 次日T卖出(close)
    d = drow(-1)
    res['alt_D4_buy'] = None
    res['alt_D4_to_Tclose'] = None
    if d is not None and d.get('hour4_open') is not None and float(d['hour4_open']) != 0:
        b2 = float(d['hour4_open'])
        res['alt_D4_buy'] = b2
        if t.get('close') is not None:
            res['alt_D4_to_Tclose'] = (float(t['close']) / b2 - 1.0) * 100.0

    return res


def simulate_tp_sl(conn, all_days, cand, tp, sl, max_hold=3):
    """止盈止损模拟 (买 T.hour1_open, 最长持有 max_hold 天)。

    用 hour 级 high/low 判定是否触发。触发止损优先 (保守)。
    返回 (ret_pct, exit_reason)。
    """
    it = cand['idx']
    code = cand['code']
    need_idx = [it + k for k in range(0, max_hold + 1)]
    need_dates = [all_days[i] for i in need_idx if 0 <= i < len(all_days)]
    rows = fetch_forward_hours(conn, code, need_dates)

    t = rows.get(all_days[it]) if it < len(all_days) else None
    if t is None:
        return None
    buy = t.get('hour1_open')
    if buy is None or float(buy) == 0:
        return None
    buy = float(buy)
    tp_price = buy * (1 + tp / 100.0)
    sl_price = buy * (1 + sl / 100.0)

    # T+1 合规: T 日买入当天不卖 (跳过 T 日盘中). 从 T+1 开始检查
    for k in range(1, max_hold + 1):
        i = it + k
        if i >= len(all_days):
            break
        r = rows.get(all_days[i])
        if r is None:
            continue
        for h in (1, 2, 3, 4):
            hl = r.get(f'hour{h}_low')
            hh = r.get(f'hour{h}_high')
            if hl is not None and float(hl) <= sl_price:
                return ((sl_price / buy - 1) * 100.0, f'SL@T+{k}h{h}')
            if hh is not None and float(hh) >= tp_price:
                return ((tp_price / buy - 1) * 100.0, f'TP@T+{k}h{h}')
    # 未触发 -> max_hold 收盘退出
    i = it + max_hold
    if i < len(all_days):
        r = rows.get(all_days[i])
        if r is not None and r.get('close') is not None:
            return ((float(r['close']) / buy - 1) * 100.0, f'HOLD_close@T+{max_hold}')
    return None


# ============================================================
# 明细输出 (Step 2)
# ============================================================

def fmt_num(v, width=8, prec=2):
    if v is None:
        return ' ' * (width - 1) + '-'
    try:
        return f"{float(v):>{width}.{prec}f}"
    except (TypeError, ValueError):
        return str(v).rjust(width)


def fmt_pct(v, width=8, prec=2):
    if v is None:
        return ' ' * (width - 1) + '-'
    try:
        f = float(v)
        s = f"{'+' if f >= 0 else ''}{f:.{prec}f}%"
        return s.rjust(width)
    except (TypeError, ValueError):
        return str(v).rjust(width)


def print_candidate_detail(conn, all_days, cand):
    code = cand['code']
    it = cand['idx']
    print()
    print(f"========== 买入日T={cand['t_day']} 候选: {code} {cand['code_name']} "
          f"[{cand['board']}] 开板日D={cand['d_day']} 连续跌停={cand['cons_down']}天 ==========")
    dcr = cand['d_close_rate']
    print(f"开板日D涨跌幅: {fmt_pct(dcr).strip()}  (D未跌停=首次开板)")

    # rate 相对 T日 open
    t_row = fetch_forward_hours(conn, code, [cand['t_day']]).get(cand['t_day'])
    t_open = t_row.get('open') if t_row else None
    base = float(t_open) if t_open else None
    print(f"T日open基准价: {fmt_num(base).strip()}  (hour级rate% 相对T.open)")
    print()
    header = ("日期       | Hr | Open_r%  | Close_r% | High_r%  | Low_r%   | Amount")
    print(header)
    print('-' * len(header))

    lo = max(0, it - WINDOW_BEFORE)
    hi = min(len(all_days) - 1, it + WINDOW_AFTER)
    win_dates = all_days[lo:hi + 1]
    rows = fetch_forward_hours(conn, code, win_dates)

    def rate(v):
        if v is None or base is None or base == 0:
            return None
        return (float(v) / base - 1.0) * 100.0

    for d in win_dates:
        r = rows.get(d)
        if r is None or r.get('hour1_open') is None:
            continue
        marker = ' <T' if d == cand['t_day'] else (' <D开板' if d == cand['d_day'] else '')
        for h in (1, 2, 3, 4):
            ho = r.get(f'hour{h}_open')
            hc = r.get(f'hour{h}_close')
            hh = r.get(f'hour{h}_high')
            hl = r.get(f'hour{h}_low')
            ha = r.get(f'hour{h}_amount')
            if ho is None and hc is None:
                continue
            tag = marker if h == 1 else ''
            print(f"{d} | {h}  | {fmt_pct(rate(ho))} | {fmt_pct(rate(hc))} | "
                  f"{fmt_pct(rate(hh))} | {fmt_pct(rate(hl))} | {fmt_num(ha, 12, 0)}{tag}")


# ============================================================
# 统计 (Step 3 & 4)
# ============================================================

def _stat(vals):
    vals = [v for v in vals if v is not None]
    n = len(vals)
    if n == 0:
        return (0, None, None, None)
    avg = sum(vals) / n
    win = sum(1 for v in vals if v > 0) / n * 100.0
    med = sorted(vals)[n // 2]
    return (n, avg, win, med)


def print_stats(conn, all_days, cands, label=''):
    print()
    print('=' * 78)
    print(f"[统计] {label}  候选样本合计: {len(cands)} 只")
    print('=' * 78)

    analyses = []
    for c in cands:
        a = analyze_candidate(conn, all_days, c)
        if a is not None:
            analyses.append((c, a))
    print(f"可分析样本(T日可买入): {len(analyses)} 只")
    if not analyses:
        print("无可分析样本。")
        return

    # --- 各持有期收益 (买T.hour1_open, 卖T+n收盘) ---
    print("\n[买入=T.hour1_open, 卖出=T+n收盘]  样本/均值/胜率/中位数")
    for key, name in (('ret_T1open', 'T+1开盘'), ('ret_T1', 'T+1收盘'),
                      ('ret_T2', 'T+2收盘'), ('ret_T3', 'T+3收盘'),
                      ('ret_T5', 'T+5收盘')):
        n, avg, win, med = _stat([a[key] for _c, a in analyses])
        if n:
            print(f"  {name:8s}: n={n:4d}  均值={avg:+6.2f}%  胜率={win:5.1f}%  中位={med:+6.2f}%")

    # --- 对照: 开板当天D.hour4买入, T日收盘卖 ---
    n, avg, win, med = _stat([a['alt_D4_to_Tclose'] for _c, a in analyses])
    if n:
        print(f"\n[对照 开板日D.hour4买入 -> T.close卖]: n={n}  均值={avg:+.2f}%  胜率={win:.1f}%  中位={med:+.2f}%")

    # --- 分板块 (T+2收盘) ---
    print("\n[分板块 T+2收盘收益]")
    by_board = defaultdict(list)
    for c, a in analyses:
        by_board[c['board']].append(a['ret_T2'])
    for b, vals in sorted(by_board.items()):
        n, avg, win, med = _stat(vals)
        if n:
            print(f"  {b:6s}: n={n:4d}  均值={avg:+6.2f}%  胜率={win:5.1f}%  中位={med:+6.2f}%")

    # --- 分连续跌停天数 (T+2收盘) ---
    print("\n[分连续跌停天数(开板日之前) T+2收盘收益]")
    by_cons = defaultdict(list)
    for c, a in analyses:
        bucket = '2天' if c['cons_down'] == 2 else ('3天' if c['cons_down'] == 3 else '4天+')
        by_cons[bucket].append(a['ret_T2'])
    for b in ('2天', '3天', '4天+'):
        vals = by_cons.get(b, [])
        n, avg, win, med = _stat(vals)
        if n:
            print(f"  {b:5s}: n={n:4d}  均值={avg:+6.2f}%  胜率={win:5.1f}%  中位={med:+6.2f}%")

    # --- 止盈止损网格 (买T.hour1_open, 最长持有3天) ---
    print("\n[止盈止损网格 买=T.hour1_open 最长持有T+3]  TP/SL -> 均值/胜率")
    grids = [(5, -5), (8, -5), (10, -7), (6, -4), (12, -8), (15, -10)]
    for tp, sl in grids:
        rets = []
        for c, _a in analyses:
            r = simulate_tp_sl(conn, all_days, c, tp, sl, max_hold=3)
            if r is not None:
                rets.append(r[0])
        n, avg, win, med = _stat(rets)
        if n:
            print(f"  TP+{tp:>2d}%/SL{sl:>3d}%: n={n:4d}  均值={avg:+6.2f}%  胜率={win:5.1f}%  中位={med:+6.2f}%")

    # --- 精选子集: 开板前连续跌停 >=3 天 (核心 alpha 区) ---
    sel = [(c, a) for c, a in analyses if c['cons_down'] >= 3]
    print(f"\n[精选: 开板前连续跌停>=3天]  样本 {len(sel)} 只")
    if sel:
        for key, name in (('ret_T1open', 'T+1开盘'), ('ret_T1', 'T+1收盘'),
                          ('ret_T2', 'T+2收盘'), ('ret_T3', 'T+3收盘'),
                          ('ret_T5', 'T+5收盘')):
            n, avg, win, med = _stat([a[key] for _c, a in sel])
            if n:
                print(f"  {name:8s}: n={n:4d}  均值={avg:+6.2f}%  胜率={win:5.1f}%  中位={med:+6.2f}%")
        print("  --- 止盈止损(最长持有T+3) ---")
        for tp, sl in grids:
            rets = []
            for c, _a in sel:
                r = simulate_tp_sl(conn, all_days, c, tp, sl, max_hold=3)
                if r is not None:
                    rets.append(r[0])
            n, avg, win, med = _stat(rets)
            if n:
                print(f"  TP+{tp:>2d}%/SL{sl:>3d}%: n={n:4d}  均值={avg:+6.2f}%  胜率={win:5.1f}%  中位={med:+6.2f}%")
        # 年度稳定性 (T+2收盘)
        print("  --- 年度稳定性 (T+2收盘) ---")
        by_year = defaultdict(list)
        for c, a in sel:
            by_year[c['t_day'][:4]].append(a['ret_T2'])
        for y in sorted(by_year):
            n, avg, win, med = _stat(by_year[y])
            if n:
                print(f"  {y}: n={n:4d}  均值={avg:+6.2f}%  胜率={win:5.1f}%  中位={med:+6.2f}%")


# ============================================================
# 主流程
# ============================================================

def parse_args(argv):
    relax = '--relax' in argv
    args = [a for a in argv[1:] if not a.startswith('--')]
    if len(args) == 0:
        return '2026-04', None, relax
    if len(args) == 1:
        return args[0], None, relax
    return args[0], args[1], relax


def month_days(all_days, month: str):
    return [d for d in all_days if d.startswith(month + '-')]


def range_days(all_days, start_month: str, end_month: str):
    return [d for d in all_days if start_month + '-01' <= d <= end_month + '-31']


def main():
    start, end, relax = parse_args(sys.argv)
    conn = sqlite3.connect(DB_PATH)
    try:
        all_days = get_all_trading_days(conn)

        if end is None:
            # 单月: 明细 + 统计
            target = month_days(all_days, start)
            mode = f"单月 {start}"
        else:
            target = range_days(all_days, start, end)
            mode = f"区间 {start} ~ {end}"

        print("=" * 78)
        print(f"连续跌停后首次开板反弹策略 [Task#107 / rule2]")
        print(f"模式: {mode}   {'[放宽模式]' if relax else '[严格模式: 开板前连续>=2跌停]'}")
        print(f"目标交易日: {len(target)} 天" + (f" ({target[0]} ~ {target[-1]})" if target else ""))
        print("=" * 78)

        if not target:
            print("[警告] 目标区间无交易日数据。")
            return

        cands = screen_candidates(conn, all_days, target, relax=relax)

        # 单月模式打印明细
        if end is None:
            # 按买入日排序打印
            for c in sorted(cands, key=lambda x: (x['t_day'], x['code'])):
                print_candidate_detail(conn, all_days, c)
            # 每日候选数
            per_day = defaultdict(int)
            for c in cands:
                per_day[c['t_day']] += 1
            print("\n[每日候选数]")
            for d in target:
                if per_day.get(d):
                    print(f"  {d}: {per_day[d]} 只")

        print_stats(conn, all_days, cands, label=mode)
    finally:
        conn.close()


if __name__ == '__main__':
    main()
