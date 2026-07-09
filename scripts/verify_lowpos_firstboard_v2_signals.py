#!/usr/bin/env python3
"""核对 LowPosFirstBoardV2Strategy.get_candidates 是否复现研究的405信号,
并按 open->t3_close 聚合验证 57.5%/+2.75% 基准(等权全信号, 无slot占用)。"""
import sys
import sqlite3
sys.path.insert(0, '/home/AIWealth')

from engine.buy_strategy_lowpos_firstboard_v2 import LowPosFirstBoardV2Strategy

DB = '/home/AIWealth/data/stocks.db'
strat = LowPosFirstBoardV2Strategy()

conn = sqlite3.connect(DB)
cur = conn.cursor()
cur.execute("SELECT DISTINCT date FROM stock_kline WHERE date BETWEEN '2021-01-01' AND '2026-06-30' ORDER BY date")
all_days_full = [r[0] for r in cur.fetchall()]

import pandas as pd

total = 0
rets = []  # open->t3_close


def close_series_future(code, today):
    cur.execute("SELECT DISTINCT date FROM stock_kline WHERE date > ? ORDER BY date LIMIT 3", (today,))
    fut = [r[0] for r in cur.fetchall()]
    if len(fut) < 3:
        return None
    cur.execute("SELECT close FROM stock_kline WHERE code=? AND date=?", (code, fut[2]))
    r = cur.fetchone()
    return r[0] if r else None


for date in all_days_full:
    day_data = pd.read_sql("SELECT * FROM stock_kline WHERE date=?", conn, params=(date,))
    if day_data.empty:
        continue
    cands = strat.get_candidates(date, day_data, None, None)
    if not cands:
        continue
    total += len(cands)
    # 每个候选取 T日open 作为买入价, T+3 close 作为卖出价
    lk = day_data.set_index('code').to_dict('index')
    for c in cands:
        code = c['code']
        t_open = lk[code].get('open')
        t3c = close_series_future(code, date)
        if t_open and t3c:
            rets.append((t3c - t_open) / t_open * 100)

n = len(rets)
mean = sum(rets) / n if n else 0
win = sum(1 for r in rets if r > 0) / n * 100 if n else 0
print(f"BuyModule生成候选总数: {total}")
print(f"open->t3_close 样本: {n}  均值: {mean:+.2f}%  胜率: {win:.1f}%")
print("研究基准: 405信号, open->t3_close 样本402 +2.75% 57.5%")
conn.close()
