#!/usr/bin/env python3
"""跌停反弹策略 Phase-1: 候选股筛选与前5后5日hour级OHLC明细输出

用法:
    python3 scripts/strategy_limitdown_rebound_phase1.py [YYYY-MM]

示例:
    python3 scripts/strategy_limitdown_rebound_phase1.py 2026-04

筛选逻辑:
    1) T-1日严格跌停 (主板10%, 创业/科创20%, 用 round(close/preclose,2) 判定)
    2) T日非ST、非一字板、非北交所、开盘未跌停封死

输出:
    每只候选股的当日及前5后5日 (共11个交易日) 的hour级OHLC明细
"""

import sqlite3
import sys
from collections import defaultdict

DB_PATH = '/home/AIWealth/data/stocks.db'
TARGET_MONTH = sys.argv[1] if len(sys.argv) > 1 else '2026-04'

WINDOW_BEFORE = 5  # 候选日前5个交易日
WINDOW_AFTER = 5   # 候选日后5个交易日


# ============================================================
# 跌停判定 (严格规则)
# ============================================================

def get_limit_down_threshold(code: str) -> float:
    """根据股票代码返回跌停阈值。

    创业板(sz.30)/科创板(sh.68): 20% -> 0.80
    主板(sh.60/sz.00): 10% -> 0.90
    """
    if code.startswith('sz.30') or code.startswith('sh.68'):
        return 0.80
    return 0.90


def is_limit_down(close, preclose, code: str) -> bool:
    """严格跌停判定: round(close/preclose, 2) <= 阈值"""
    if preclose is None or preclose == 0:
        return False
    try:
        ratio = round(float(close) / float(preclose), 2)
    except (TypeError, ValueError, ZeroDivisionError):
        return False
    return ratio <= get_limit_down_threshold(code)


def is_main_board_or_gem_or_star(code: str) -> bool:
    """是否为主板/创业板/科创板 (排除北交所bj.等)"""
    return (code.startswith('sh.60') or code.startswith('sz.00')
            or code.startswith('sz.30') or code.startswith('sh.68'))


def is_one_word_board(o, h, l, c) -> bool:
    """一字板: open == high == low == close"""
    if any(x is None for x in (o, h, l, c)):
        return False
    return float(o) == float(h) == float(l) == float(c)


# ============================================================
# 数据访问
# ============================================================

def get_trading_days(conn, month: str):
    """获取指定月份内所有交易日 (升序)"""
    cur = conn.execute(
        "SELECT DISTINCT date FROM stock_kline WHERE date LIKE ? ORDER BY date ASC",
        (f"{month}-%",),
    )
    return [r[0] for r in cur.fetchall()]


def get_prev_trading_day(conn, day: str):
    cur = conn.execute(
        "SELECT MAX(date) FROM stock_kline WHERE date < ?",
        (day,),
    )
    row = cur.fetchone()
    return row[0] if row and row[0] else None


def get_window_dates(conn, day: str, before: int, after: int):
    """获取day前后各before/after个交易日 (含day本身) 的日期列表"""
    cur = conn.execute(
        "SELECT DISTINCT date FROM stock_kline WHERE date < ? ORDER BY date DESC LIMIT ?",
        (day, before),
    )
    prev_days = [r[0] for r in cur.fetchall()][::-1]

    cur = conn.execute(
        "SELECT DISTINCT date FROM stock_kline WHERE date > ? ORDER BY date ASC LIMIT ?",
        (day, after),
    )
    next_days = [r[0] for r in cur.fetchall()]

    return prev_days + [day] + next_days


def fetch_day_rows(conn, day: str):
    """获取某日全市场行情 (返回 dict[code] -> row)"""
    cur = conn.execute(
        """
        SELECT date, code, code_name, preclose, open, high, low, close,
               close_rate, open_rate, volume, amount, turn, isST
        FROM stock_kline
        WHERE date = ?
        """,
        (day,),
    )
    cols = [d[0] for d in cur.description]
    return {row[1]: dict(zip(cols, row)) for row in cur.fetchall()}


def fetch_stock_window(conn, code: str, dates):
    """获取股票在给定日期列表的hour级行情"""
    if not dates:
        return {}
    placeholders = ','.join('?' * len(dates))
    sql = f"""
        SELECT date, open, high, low, close, turn,
               hour1_open, hour1_high, hour1_low, hour1_close,
               hour1_open_rate, hour1_close_rate, hour1_volume, hour1_amount,
               hour2_open, hour2_high, hour2_low, hour2_close,
               hour2_open_rate, hour2_close_rate, hour2_volume, hour2_amount,
               hour3_open, hour3_high, hour3_low, hour3_close,
               hour3_open_rate, hour3_close_rate, hour3_volume, hour3_amount,
               hour4_open, hour4_high, hour4_low, hour4_close,
               hour4_open_rate, hour4_close_rate, hour4_volume, hour4_amount
        FROM stock_kline
        WHERE code = ? AND date IN ({placeholders})
        ORDER BY date ASC
    """
    cur = conn.execute(sql, [code] + list(dates))
    cols = [d[0] for d in cur.description]
    return {row[0]: dict(zip(cols, row)) for row in cur.fetchall()}


# ============================================================
# 候选股筛选
# ============================================================

def screen_candidates(conn, t_day: str, t_prev: str):
    """筛选某交易日T的候选股 (T-1跌停 + T日合规)"""
    if not t_prev:
        return []

    prev_rows = fetch_day_rows(conn, t_prev)
    if not prev_rows:
        return []

    today_rows = fetch_day_rows(conn, t_day)
    if not today_rows:
        return []

    candidates = []
    for code, prev in prev_rows.items():
        # 1) 排除北交所
        if not is_main_board_or_gem_or_star(code):
            continue

        # 2) T-1日严格跌停
        if not is_limit_down(prev.get('close'), prev.get('preclose'), code):
            continue

        # 3) T日数据存在
        today = today_rows.get(code)
        if today is None:
            continue

        # 4) 非ST
        if today.get('isST') == 1:
            continue
        cn = (today.get('code_name') or '')
        if 'ST' in cn.upper():
            continue

        # 5) 非一字板
        if is_one_word_board(today.get('open'), today.get('high'),
                             today.get('low'), today.get('close')):
            continue

        # 6) T日开盘未跌停封死: open > preclose * 跌停阈值
        t_pre = today.get('preclose')
        t_open = today.get('open')
        if t_pre is None or t_pre == 0 or t_open is None:
            continue
        thr = get_limit_down_threshold(code)
        # 严格大于 跌停封盘价 (用round判定避免边界)
        if round(float(t_open) / float(t_pre), 2) <= thr:
            continue

        candidates.append({
            'code': code,
            'code_name': cn,
            'prev_close': prev.get('close'),
            'prev_preclose': prev.get('preclose'),
            'prev_ratio': round(float(prev['close']) / float(prev['preclose']), 4),
            'today_open': t_open,
            'today_preclose': t_pre,
        })
    return candidates


# ============================================================
# 输出格式化
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
        sign = '+' if f >= 0 else ''
        s = f"{sign}{f:.{prec}f}%"
        return s.rjust(width)
    except (TypeError, ValueError):
        return str(v).rjust(width)


def fmt_int(v, width=10):
    if v is None:
        return ' ' * (width - 1) + '-'
    try:
        return f"{int(v):>{width}d}"
    except (TypeError, ValueError):
        return str(v).rjust(width)


def fmt_amount(v, width=12):
    if v is None:
        return ' ' * (width - 1) + '-'
    try:
        return f"{float(v):>{width}.0f}"
    except (TypeError, ValueError):
        return str(v).rjust(width)


def print_hour_lines(date_str: str, day_row: dict, turn_today: float):
    """打印一天4个hour的明细行 (该天无hour数据则跳过)"""
    if day_row is None:
        return 0
    if day_row.get('hour1_open') is None:
        return 0

    printed = 0
    for h in (1, 2, 3, 4):
        ho = day_row.get(f'hour{h}_open')
        hh = day_row.get(f'hour{h}_high')
        hl = day_row.get(f'hour{h}_low')
        hc = day_row.get(f'hour{h}_close')
        hor = day_row.get(f'hour{h}_open_rate')
        hcr = day_row.get(f'hour{h}_close_rate')
        hv = day_row.get(f'hour{h}_volume')
        ha = day_row.get(f'hour{h}_amount')
        if ho is None and hh is None and hl is None and hc is None:
            continue
        # 换手率显示当日整体 turn (按需求统一附加)
        line = (
            f"{date_str} | {h}  | "
            f"{fmt_num(ho)} | {fmt_num(hh)} | {fmt_num(hl)} | {fmt_num(hc)} | "
            f"{fmt_pct(hor)} | {fmt_pct(hcr)} | "
            f"{fmt_int(hv)} | {fmt_amount(ha)} | "
            f"{fmt_pct(turn_today, width=7)}"
        )
        print(line)
        printed += 1
    return printed


def print_candidate_detail(conn, t_day: str, cand: dict):
    code = cand['code']
    cn = cand['code_name']
    print()
    print(f"========== {t_day} 候选股: {code} {cn} ==========")
    print(
        f"昨日跌停: close={fmt_num(cand['prev_close']).strip()}, "
        f"preclose={fmt_num(cand['prev_preclose']).strip()}, "
        f"ratio={cand['prev_ratio']:.4f}"
    )
    print(
        f"T日开盘: open={fmt_num(cand['today_open']).strip()}, "
        f"preclose={fmt_num(cand['today_preclose']).strip()}, "
        f"open_rate={(float(cand['today_open'])/float(cand['today_preclose'])-1)*100:+.2f}%"
    )
    print()
    header = (
        "日期       | Hr | Open     | High     | Low      | Close    | "
        "Open_Rate | Close_Rate | Volume     | Amount       | Turn   "
    )
    print(header)
    print('-' * len(header))

    win_dates = get_window_dates(conn, t_day, WINDOW_BEFORE, WINDOW_AFTER)
    rows = fetch_stock_window(conn, code, win_dates)
    total_hour_lines = 0
    for d in win_dates:
        row = rows.get(d)
        if row is None:
            continue
        turn = row.get('turn')
        printed = print_hour_lines(d, row, turn)
        total_hour_lines += printed
    return total_hour_lines


# ============================================================
# 主流程
# ============================================================

def main():
    print(f"[跌停反弹策略 Phase-1] 目标月份: {TARGET_MONTH}")
    print(f"[DB] {DB_PATH}")
    print(f"[窗口] 候选日前{WINDOW_BEFORE}日 + 当日 + 后{WINDOW_AFTER}日 (共{WINDOW_BEFORE+1+WINDOW_AFTER}个交易日)")

    conn = sqlite3.connect(DB_PATH)
    try:
        days = get_trading_days(conn, TARGET_MONTH)
        if not days:
            print(f"[警告] {TARGET_MONTH} 无交易日数据。")
            return
        print(f"[交易日] {TARGET_MONTH} 共 {len(days)} 个交易日: {days[0]} ~ {days[-1]}")

        total_candidates = 0
        per_day_count = defaultdict(int)
        all_candidates = []

        for t_day in days:
            t_prev = get_prev_trading_day(conn, t_day)
            cands = screen_candidates(conn, t_day, t_prev)
            per_day_count[t_day] = len(cands)
            total_candidates += len(cands)
            for c in cands:
                all_candidates.append((t_day, c))

        # 打印每只候选股的明细
        for t_day, c in all_candidates:
            print_candidate_detail(conn, t_day, c)

        # 统计摘要
        print()
        print('=' * 70)
        print(f"[统计] {TARGET_MONTH} 共 {len(days)} 个交易日, 候选股合计 {total_candidates} 只")
        if days:
            print(f"[统计] 日均候选: {total_candidates / len(days):.2f} 只/日")
        print('-' * 70)
        print("[每日明细]")
        for d in days:
            print(f"  {d}: {per_day_count[d]:>3d} 只")
        print('=' * 70)
    finally:
        conn.close()


if __name__ == '__main__':
    main()
