#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
缩量十字星策略 Phase-1: 候选股筛选与前5后5日 hour 级 OHLC 明细输出

筛选逻辑（针对每一交易日 T）：
  - 前 3 日累计涨幅 > 10%:
        cum_gain = (T-1 日 close / T-4 日 close - 1) * 100 > 10
        即在进入 T 日之前，该股已经连续 3 个交易日合计涨了 10%+
  - 当日 (T 日) 振幅 < 3%:
        abs(high - low) / preclose * 100 < 3
  - 当日 (T 日) 换手率缩量:
        turn < 前 3 日 (T-3 ~ T-1) 换手率均值 * 0.6
  - 过滤:
        非 ST (isST!=1 且 code_name 不含 'ST')
        非北交所 (code 不以 'bj.' 开头)
        非一字板 (T 日 high != low)
        preclose 非 NULL 且 > 0

用法:
    python3 scripts/strategy_shrink_doji_phase1.py [YYYY-MM]
    默认月份 2026-04
"""

import sqlite3
import sys
from collections import defaultdict

DB_PATH = '/home/AIWealth/data/stocks.db'
TARGET_MONTH = sys.argv[1] if len(sys.argv) > 1 else '2026-04'

# --------- 阈值参数 ---------
CUM_GAIN_PCT_MIN = 10.0      # 前 3 日累计涨幅最低 (%)
AMPLITUDE_PCT_MAX = 3.0      # 当日振幅上限 (%)
TURN_SHRINK_RATIO = 0.6      # 当日换手率必须 < 前 3 日均值 * 该比例
WINDOW_BEFORE = 5            # 输出前 5 日
WINDOW_AFTER = 5             # 输出后 5 日


def get_trading_dates(conn, month):
    """获取目标月份所有交易日 (按升序)"""
    rows = conn.execute(
        "SELECT DISTINCT date FROM stock_kline WHERE date LIKE ? ORDER BY date",
        (f"{month}%",),
    ).fetchall()
    return [r[0] for r in rows]


def get_all_trading_dates(conn):
    """获取数据库内全部交易日 (升序)"""
    rows = conn.execute(
        "SELECT DISTINCT date FROM stock_kline ORDER BY date"
    ).fetchall()
    return [r[0] for r in rows]


def is_one_word_board(row):
    """一字板: high == low (全天没有波动)"""
    if row['high'] is None or row['low'] is None:
        return False
    return abs(row['high'] - row['low']) < 1e-6


def basic_filter(row):
    """基础过滤: ST/北交所/preclose 异常"""
    code = row['code'] or ''
    name = row['code_name'] or ''
    if code.startswith('bj.'):
        return False
    if (row['isST'] is not None and row['isST'] == 1) or 'ST' in name.upper():
        return False
    if row['preclose'] is None or row['preclose'] <= 0:
        return False
    return True


def screen_candidates_for_day(conn, t_day, all_dates):
    """筛选 T 日的缩量十字星候选股"""
    if t_day not in all_dates:
        return []
    idx = all_dates.index(t_day)
    if idx < 4:
        return []  # 不足 4 个前置交易日

    t_minus_1 = all_dates[idx - 1]
    t_minus_2 = all_dates[idx - 2]
    t_minus_3 = all_dates[idx - 3]
    t_minus_4 = all_dates[idx - 4]

    # 一次性拉取 T 日全部股票数据
    t_rows = conn.execute(
        "SELECT * FROM stock_kline WHERE date = ?", (t_day,)
    ).fetchall()

    # 拉取 T-1 ~ T-4 日所有数据，按 code 索引
    prev_dates = [t_minus_4, t_minus_3, t_minus_2, t_minus_1]
    placeholder = ",".join(["?"] * len(prev_dates))
    prev_rows = conn.execute(
        f"SELECT date, code, close, turn, preclose FROM stock_kline "
        f"WHERE date IN ({placeholder})",
        prev_dates,
    ).fetchall()

    prev_map = defaultdict(dict)  # code -> {date: row_dict}
    for r in prev_rows:
        prev_map[r['code']][r['date']] = {
            'close': r['close'],
            'turn': r['turn'],
            'preclose': r['preclose'],
        }

    candidates = []
    for row in t_rows:
        if not basic_filter(row):
            continue
        if is_one_word_board(row):
            continue

        code = row['code']
        prev = prev_map.get(code)
        if not prev:
            continue
        # 必须 4 个前置交易日全部齐全
        if not all(d in prev for d in prev_dates):
            continue

        c_t_minus_1 = prev[t_minus_1]['close']
        c_t_minus_4 = prev[t_minus_4]['close']
        if c_t_minus_1 is None or c_t_minus_4 is None or c_t_minus_4 <= 0:
            continue

        cum_gain_pct = (c_t_minus_1 / c_t_minus_4 - 1.0) * 100.0
        if cum_gain_pct <= CUM_GAIN_PCT_MIN:
            continue

        # 当日振幅
        if row['high'] is None or row['low'] is None or row['preclose'] is None or row['preclose'] <= 0:
            continue
        amplitude_pct = abs(row['high'] - row['low']) / row['preclose'] * 100.0
        if amplitude_pct >= AMPLITUDE_PCT_MAX:
            continue

        # 当日换手率缩量
        turns_prev3 = [
            prev[t_minus_3]['turn'],
            prev[t_minus_2]['turn'],
            prev[t_minus_1]['turn'],
        ]
        if any(t is None or t <= 0 for t in turns_prev3):
            continue
        avg_turn = sum(turns_prev3) / 3.0
        t_turn = row['turn']
        if t_turn is None or t_turn <= 0:
            continue
        if t_turn >= avg_turn * TURN_SHRINK_RATIO:
            continue

        candidates.append({
            'code': code,
            'code_name': row['code_name'],
            'cum_gain_pct': cum_gain_pct,
            'amplitude_pct': amplitude_pct,
            't_turn': t_turn,
            'avg_turn_prev3': avg_turn,
            'turn_ratio': t_turn / avg_turn if avg_turn > 0 else 0,
        })

    return candidates


def fetch_window_rows(conn, code, t_day, all_dates):
    """获取 [T-5, T+5] 窗口内的全部交易日数据"""
    if t_day not in all_dates:
        return []
    idx = all_dates.index(t_day)
    start = max(0, idx - WINDOW_BEFORE)
    end = min(len(all_dates) - 1, idx + WINDOW_AFTER)
    window_dates = all_dates[start:end + 1]
    placeholder = ",".join(["?"] * len(window_dates))
    rows = conn.execute(
        f"SELECT * FROM stock_kline WHERE code = ? AND date IN ({placeholder}) "
        f"ORDER BY date",
        (code, *window_dates),
    ).fetchall()
    return rows


def fmt_float(x, fmt="{:>8.2f}"):
    if x is None:
        return f"{'NA':>8}"
    try:
        return fmt.format(x)
    except Exception:
        return f"{'NA':>8}"


def fmt_pct(x):
    if x is None:
        return f"{'NA':>8}"
    sign = '+' if x >= 0 else ''
    return f"{sign}{x:.2f}%".rjust(8)


def fmt_int(x, width=11):
    if x is None:
        return f"{'NA':>{width}}"
    try:
        return f"{int(x):>{width}d}"
    except Exception:
        return f"{'NA':>{width}}"


def fmt_amount(x):
    if x is None:
        return f"{'NA':>13}"
    try:
        return f"{x:>13.0f}"
    except Exception:
        return f"{'NA':>13}"


def print_hour_detail(rows, t_day):
    """逐日逐 hour 打印 OHLC + Volume + Amount + Turn"""
    header = (
        f"{'日期':<10} | {'Hour':^4} | {'Open':>8} | {'High':>8} | "
        f"{'Low':>8} | {'Close':>8} | {'Open_Rate':>9} | {'Close_Rate':>10} | "
        f"{'Volume':>11} | {'Amount':>13} | {'Turn':>7}"
    )
    print(header)
    print('-' * len(header))

    for row in rows:
        date_str = row['date']
        marker = '<<<' if date_str == t_day else ''
        day_turn = row['turn']
        any_hour = False
        for h in range(1, 5):
            o = row[f'hour{h}_open']
            hi = row[f'hour{h}_high']
            lo = row[f'hour{h}_low']
            c = row[f'hour{h}_close']
            or_ = row[f'hour{h}_open_rate']
            cr = row[f'hour{h}_close_rate']
            vol = row[f'hour{h}_volume']
            amt = row[f'hour{h}_amount']
            if o is None and hi is None and c is None:
                continue
            any_hour = True
            turn_str = f"{day_turn:>6.2f}%" if (h == 4 and day_turn is not None) else f"{'':>7}"
            line = (
                f"{date_str:<10} | {h:^4} | {fmt_float(o)} | {fmt_float(hi)} | "
                f"{fmt_float(lo)} | {fmt_float(c)} | {fmt_pct(or_)} | {fmt_pct(cr)} | "
                f"{fmt_int(vol)} | {fmt_amount(amt)} | {turn_str}"
            )
            if marker and h == 1:
                line += f"  {marker} 候选日"
            print(line)
        if not any_hour:
            # 兜底打印日级
            line = (
                f"{date_str:<10} | {'D':^4} | {fmt_float(row['open'])} | "
                f"{fmt_float(row['high'])} | {fmt_float(row['low'])} | "
                f"{fmt_float(row['close'])} | {fmt_pct(row['open_rate'])} | "
                f"{fmt_pct(row['close_rate'])} | {fmt_int(row['volume'])} | "
                f"{fmt_amount(row['amount'])} | "
                f"{day_turn:>6.2f}%" if day_turn is not None else f"{'':>7}"
            )
            print(line + "  (无 hour 级数据)")


def main():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row

    print(f"========== 缩量十字星 Phase-1 候选股筛选: {TARGET_MONTH} ==========")
    print(f"参数: 前3日累计涨幅 > {CUM_GAIN_PCT_MIN}%, 当日振幅 < {AMPLITUDE_PCT_MAX}%, "
          f"当日换手率 < 前3日均值 * {TURN_SHRINK_RATIO}")
    print()

    target_dates = get_trading_dates(conn, TARGET_MONTH)
    if not target_dates:
        print(f"[WARN] 月份 {TARGET_MONTH} 无交易日数据。")
        return
    all_dates = get_all_trading_dates(conn)

    daily_count = {}
    total_candidates = 0

    for t_day in target_dates:
        cands = screen_candidates_for_day(conn, t_day, all_dates)
        daily_count[t_day] = len(cands)
        total_candidates += len(cands)

        if not cands:
            print(f"---------- {t_day} 候选股: 0 只 ----------")
            print()
            continue

        print(f"---------- {t_day} 候选股: {len(cands)} 只 ----------")
        for c in cands:
            print()
            print(
                f"========== {t_day} 候选股: {c['code']} {c['code_name']} =========="
            )
            print(
                f"形态: 前3日累计涨幅={c['cum_gain_pct']:+.2f}%, "
                f"当日振幅={c['amplitude_pct']:.2f}%, "
                f"当日换手率={c['t_turn']:.2f}%(前3日均值{c['avg_turn_prev3']:.2f}%的"
                f"{c['turn_ratio']*100:.1f}%)"
            )
            rows = fetch_window_rows(conn, c['code'], t_day, all_dates)
            print_hour_detail(rows, t_day)
        print()

    # 统计摘要
    print("=" * 70)
    print(f"【统计摘要】月份: {TARGET_MONTH}")
    print(f"  交易日数: {len(target_dates)}")
    print(f"  候选股总数: {total_candidates}")
    if target_dates:
        print(f"  每日平均: {total_candidates / len(target_dates):.2f} 只/日")
    print(f"  逐日分布:")
    for d, n in daily_count.items():
        print(f"    {d}: {n} 只")
    print("=" * 70)

    conn.close()


if __name__ == "__main__":
    main()
