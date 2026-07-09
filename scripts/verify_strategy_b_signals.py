#!/usr/bin/env python3
"""验证策略B(科创板 J+L+P 共振)信号复现。研究基准: 162样本, T+1 +14.91%, 胜率80.9%"""
import sqlite3
import numpy as np
from collections import defaultdict

DB_PATH = '/home/AIWealth/data/stocks.db'
DATE_START = '2021-01-01'
DATE_END = '2026-06-30'


def load_index(conn):
    cur = conn.cursor()
    cur.execute("SELECT date, open, preclose FROM index_kline WHERE code='sh.000001' AND date>='2020-01-01' ORDER BY date")
    d = {}
    for date, o, pc in cur.fetchall():
        if o and pc and pc > 0:
            d[date] = (o, pc)
    return d


def main():
    conn = sqlite3.connect(DB_PATH)
    index_data = load_index(conn)
    print(f"index days: {len(index_data)}")
    cur = conn.cursor()
    cur.execute("SELECT DISTINCT code FROM stock_kline WHERE code LIKE 'sh.688%' AND date>=? AND date<=? ORDER BY code", (DATE_START, DATE_END))
    codes = [r[0] for r in cur.fetchall()]
    print(f"star stocks: {len(codes)}")
    n_sig = 0; sum_r1 = 0.0; win1 = 0
    yearly = defaultdict(lambda: [0, 0.0, 0])
    for code in codes:
        cur.execute("SELECT date, code_name, open, high, low, close, preclose, volume, amount, turn, hour1_open, hour4_close, isST FROM stock_kline WHERE code=? ORDER BY date", (code,))
        srows = cur.fetchall()
        n = len(srows)
        if n < 30:
            continue
        dates = [r[0] for r in srows]
        opens = np.array([r[2] or 0 for r in srows], dtype=np.float64)
        highs = np.array([r[3] or 0 for r in srows], dtype=np.float64)
        closes = np.array([r[5] or 0 for r in srows], dtype=np.float64)
        precloses = np.array([r[6] or 0 for r in srows], dtype=np.float64)
        amounts = np.array([r[8] or 0 for r in srows], dtype=np.float64)
        turns = np.array([r[9] or 0 for r in srows], dtype=np.float64)
        h1 = np.array([r[10] or 0 for r in srows], dtype=np.float64)
        h4 = np.array([r[11] or 0 for r in srows], dtype=np.float64)
        is_st = np.array([r[12] or 0 for r in srows], dtype=np.int8)
        names = [r[1] for r in srows]
        turn_ma5 = np.full(n, np.nan)
        high_20 = np.full(n, np.nan)
        for i in range(4, n):
            turn_ma5[i] = turns[i-4:i+1].mean()
        for i in range(19, n):
            high_20[i] = highs[i-19:i+1].max()
        limit_ratio = 0.20
        for i in range(25, n):
            td = dates[i]
            if td < DATE_START or td > DATE_END:
                continue
            if is_st[i]:
                continue
            nm = names[i]
            if nm and 'ST' in nm.upper():
                continue
            if h1[i] <= 0 or precloses[i] <= 0 or opens[i] <= 0 or closes[i-1] <= 0:
                continue
            yd_close = closes[i-1]
            gap = (opens[i] - yd_close) / yd_close * 100
            if gap < 2.0:
                continue
            if opens[i] >= round(precloses[i] * 1.20, 2):
                continue
            if turns[i] <= 0:
                continue
            cap = amounts[i] / (turns[i] / 100) / 1e8
            if not (cap < 50):
                continue
            buy = h1[i]
            if buy <= 0:
                continue
            if not (i + 1 < n and h4[i+1] > 0):
                continue
            ret1 = (h4[i+1] - buy) / buy * 100
            j = (not np.isnan(turn_ma5[i-1])) and turn_ma5[i-1] < 1.0
            l = (not np.isnan(high_20[i-1])) and high_20[i-1] > 0 and yd_close >= high_20[i-1] * 0.98
            p = False
            if td in index_data:
                io, ipc = index_data[td]
                if io > ipc * 1.003:
                    p = True
            if j and l and p:
                n_sig += 1; sum_r1 += ret1
                if ret1 > 0:
                    win1 += 1
                yr = int(td[:4])
                yearly[yr][0] += 1; yearly[yr][1] += ret1
                if ret1 > 0:
                    yearly[yr][2] += 1
    conn.close()
    print("=" * 60)
    print("策略B(J+L+P star <50亿 gap>=2%) 信号复现:")
    print(f"  样本数: {n_sig}  (研究基准: 162)")
    if n_sig:
        print(f"  T+1均收益: {sum_r1/n_sig:+.2f}%  (研究基准: +14.91%)")
        print(f"  胜率: {win1/n_sig*100:.1f}%  (研究基准: 80.9%)")
    for yr in sorted(yearly):
        c, s, w = yearly[yr]
        print(f"    {yr}: {c}笔 {s/c:+.2f}% 胜率{w/c*100:.0f}%")


if __name__ == '__main__':
    main()
