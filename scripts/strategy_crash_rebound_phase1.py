#!/usr/bin/env python3
"""大盘暴跌次日超跌反弹策略 Phase-1: 候选股筛选与前5后5日hour级OHLC明细输出

逻辑:
  1) 从 index_kline (sh.000001) 找指定月份范围内 close_rate < -2% 的暴跌日(若 <3 天自动放宽到 -1.5%)
  2) 每个暴跌日 T 取全市场跌幅最大、换手率>2%、非ST、非北交所、非一字板的 TOP10
  3) 打印候选股在 T 日及前5后5日(共11天)的 hour 级 OHLC 明细 + 换手率

用法:
  python3 scripts/strategy_crash_rebound_phase1.py [START_MONTH] [END_MONTH]
  默认: 2026-01 ~ 2026-04
"""
import sqlite3
import sys
from datetime import datetime
from calendar import monthrange

DB_PATH = '/home/AIWealth/data/stocks.db'
INDEX_CODE = 'sh.000001'
CRASH_THRESHOLD_PRIMARY = -2.0      # 主阈值: 大盘 close_rate < -2%
CRASH_THRESHOLD_FALLBACK = -1.5     # 备用阈值: 暴跌日 <3 天时自动放宽
MIN_CRASH_DAYS = 3
TOP_N = 10                          # 每个暴跌日取 TOP 候选数
STOCK_DROP_THRESHOLD = -5.0         # 个股 close_rate < -5%
MIN_TURN = 2.0                      # 换手率 > 2%
WINDOW_BEFORE = 5
WINDOW_AFTER = 5


def month_to_range(start_month: str, end_month: str):
    """'2026-01' -> '2026-01-01'; '2026-04' -> '2026-04-30'"""
    sy, sm = map(int, start_month.split('-'))
    ey, em = map(int, end_month.split('-'))
    s = f"{sy:04d}-{sm:02d}-01"
    e = f"{ey:04d}-{em:02d}-{monthrange(ey, em)[1]:02d}"
    return s, e


def find_crash_days(conn, start_date, end_date, threshold):
    cur = conn.cursor()
    cur.execute(
        """
        SELECT date, close_rate
        FROM index_kline
        WHERE code = ? AND date >= ? AND date <= ? AND close_rate < ?
        ORDER BY date
        """,
        (INDEX_CODE, start_date, end_date, threshold),
    )
    return cur.fetchall()


def load_trade_dates(conn):
    cur = conn.cursor()
    cur.execute(
        "SELECT DISTINCT date FROM index_kline WHERE code=? ORDER BY date",
        (INDEX_CODE,),
    )
    return [r[0] for r in cur.fetchall()]


def pick_window(all_dates, t_date, before, after):
    """返回 [T-before, ..., T, ..., T+after] 实际存在的交易日列表."""
    if t_date not in all_dates:
        return []
    idx = all_dates.index(t_date)
    lo = max(0, idx - before)
    hi = min(len(all_dates), idx + after + 1)
    return all_dates[lo:hi]


def get_top_candidates(conn, t_date):
    cur = conn.cursor()
    cur.execute(
        """
        SELECT date, code, code_name, preclose, open, high, low, close,
               close_rate, open_rate, volume, amount, turn, isST
        FROM stock_kline
        WHERE date = ?
          AND close_rate < ?
          AND turn > ?
          AND preclose IS NOT NULL AND preclose > 0
          AND (isST IS NULL OR isST != 1)
          AND code_name NOT LIKE '%ST%'
          AND code NOT LIKE 'bj.%'
          AND NOT (open = high AND high = low)
        ORDER BY close_rate ASC
        LIMIT ?
        """,
        (t_date, STOCK_DROP_THRESHOLD, MIN_TURN, TOP_N),
    )
    cols = [c[0] for c in cur.description]
    return [dict(zip(cols, row)) for row in cur.fetchall()]


def get_hour_detail(conn, code, dates):
    if not dates:
        return {}
    ph = ','.join('?' * len(dates))
    cur = conn.cursor()
    cur.execute(
        f"""
        SELECT date,
               hour1_open, hour1_high, hour1_low, hour1_close, hour1_open_rate, hour1_close_rate, hour1_volume, hour1_amount,
               hour2_open, hour2_high, hour2_low, hour2_close, hour2_open_rate, hour2_close_rate, hour2_volume, hour2_amount,
               hour3_open, hour3_high, hour3_low, hour3_close, hour3_open_rate, hour3_close_rate, hour3_volume, hour3_amount,
               hour4_open, hour4_high, hour4_low, hour4_close, hour4_open_rate, hour4_close_rate, hour4_volume, hour4_amount,
               turn
        FROM stock_kline
        WHERE code = ? AND date IN ({ph})
        ORDER BY date
        """,
        [code] + list(dates),
    )
    res = {}
    for row in cur.fetchall():
        d = row[0]
        hours = []
        # 4 hours, each 8 fields after date
        for i in range(4):
            base = 1 + i * 8
            hours.append({
                'open': row[base],
                'high': row[base + 1],
                'low': row[base + 2],
                'close': row[base + 3],
                'open_rate': row[base + 4],
                'close_rate': row[base + 5],
                'volume': row[base + 6],
                'amount': row[base + 7],
            })
        res[d] = {'hours': hours, 'turn': row[33]}
    return res


def fmt_num(v, fmt):
    if v is None:
        return 'NULL'
    try:
        return format(v, fmt)
    except Exception:
        return str(v)


def fmt_rate(v):
    if v is None:
        return 'NULL'
    return f"{v:+.2f}%"


def print_hour_detail(code, code_name, t_date, t_close_rate, t_turn, hour_data, window_dates):
    print(f"\n--- 候选股: {code} {code_name} (T日={t_date}, T日跌幅: {t_close_rate:+.2f}%, 换手率: {t_turn:.2f}%) ---")
    header = f"{'日期':<11}| Hour | {'Open':>8} | {'High':>8} | {'Low':>8} | {'Close':>8} | {'OpenRate':>9} | {'CloseRate':>9} | {'Volume':>12} | {'Amount':>14} | {'Turn':>7}"
    print(header)
    print('-' * len(header))
    for d in window_dates:
        rec = hour_data.get(d)
        marker = ' *T*' if d == t_date else ''
        if not rec:
            print(f"{d:<11}| --   | (无数据){marker}")
            continue
        turn = rec['turn']
        for hi, h in enumerate(rec['hours'], 1):
            row = (
                f"{d:<11}| {hi:^4} | "
                f"{fmt_num(h['open'], '>8.2f')} | "
                f"{fmt_num(h['high'], '>8.2f')} | "
                f"{fmt_num(h['low'], '>8.2f')} | "
                f"{fmt_num(h['close'], '>8.2f')} | "
                f"{fmt_rate(h['open_rate']):>9} | "
                f"{fmt_rate(h['close_rate']):>9} | "
                f"{fmt_num(h['volume'], '>12,d') if h['volume'] is not None else 'NULL':>12} | "
                f"{fmt_num(h['amount'], '>14,.0f') if h['amount'] is not None else 'NULL':>14} | "
                f"{fmt_num(turn, '>6.2f') + '%' if turn is not None else 'NULL':>7}"
            )
            if hi == 1 and d == t_date:
                row += '   <== T日'
            print(row)


def main():
    start_month = sys.argv[1] if len(sys.argv) > 1 else '2026-01'
    end_month = sys.argv[2] if len(sys.argv) > 2 else '2026-04'
    start_date, end_date = month_to_range(start_month, end_month)

    conn = sqlite3.connect(DB_PATH)
    print(f"========== 大盘暴跌次日超跌反弹策略 Phase-1 ==========")
    print(f"时间范围: {start_date} ~ {end_date}")

    threshold = CRASH_THRESHOLD_PRIMARY
    crash_days = find_crash_days(conn, start_date, end_date, threshold)
    if len(crash_days) < MIN_CRASH_DAYS:
        print(f"[INFO] 阈值 {threshold}% 仅找到 {len(crash_days)} 个暴跌日, 自动放宽到 {CRASH_THRESHOLD_FALLBACK}%")
        threshold = CRASH_THRESHOLD_FALLBACK
        crash_days = find_crash_days(conn, start_date, end_date, threshold)

    print(f"暴跌日阈值(close_rate < {threshold}%): 共找到 {len(crash_days)} 天")
    for d, cr in crash_days:
        print(f"  - {d}  上证close_rate = {cr:+.2f}%")

    if not crash_days:
        print("[WARN] 未找到任何暴跌日, 退出")
        return

    all_dates = load_trade_dates(conn)
    total_cands = 0

    for t_date, idx_close_rate in crash_days:
        print(f"\n========== 大盘暴跌日: {t_date} (上证跌幅: {idx_close_rate:+.2f}%) ==========")
        cands = get_top_candidates(conn, t_date)
        print(f"  TOP{TOP_N} 候选股数量: {len(cands)}")
        if not cands:
            continue
        window_dates = pick_window(all_dates, t_date, WINDOW_BEFORE, WINDOW_AFTER)
        for i, c in enumerate(cands, 1):
            total_cands += 1
            print(f"\n>>> #{i} [{t_date}] {c['code']} {c['code_name']} 跌幅={c['close_rate']:+.2f}% 换手={c['turn']:.2f}% 成交额={c['amount']/1e8:.2f}亿")
            hour_data = get_hour_detail(conn, c['code'], window_dates)
            print_hour_detail(
                c['code'], c['code_name'], t_date,
                c['close_rate'], c['turn'], hour_data, window_dates,
            )

    print(f"\n========== 统计摘要 ==========")
    print(f"暴跌日数量: {len(crash_days)}")
    print(f"候选股总数: {total_cands}")
    print(f"使用阈值  : 大盘 close_rate < {threshold}%, 个股 close_rate < {STOCK_DROP_THRESHOLD}%, 换手率 > {MIN_TURN}%")

    conn.close()


if __name__ == '__main__':
    main()
