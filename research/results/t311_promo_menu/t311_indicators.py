#!/usr/bin/env python3
"""Task#311 前置: 重建gate指标日频CSV(t244原件已被t300清理为0字节)。

口径一字不改承自t250_qa.py(已QA 6/6 PASS的纯SQL链路, 其时vs t249 CSV逐字段一致):
  promo_rate(D)   = D-1首板(D-1涨停∩D-2未涨停)中D再涨停占比; LU SQL逐字复制
  tail_mean_h4(D) = 全市场hour4均值涨幅(同宇宙过滤), round 4位(与原CSV精度一致)
范围: 2021-01-01 ~ 2026-07-10(t250 expanding_trig仅用>=2021-01-01行, D+1映射需
END后至少1个交易日)。sqlite全程ro。产物: indicators_daily.csv(date,promo_rate,
tail_mean_h4)。日历一致性由t311_runner.py的131天/分年逐位断言把关(G1终格)。
"""
import csv
import sqlite3

DB = '/home/AIWealth/data/stocks.db'
OUT = '/home/AIWealth/research/results/t311_promo_menu/indicators_daily.csv'

# 研究口径涨停集SQL(与t249 qa_audit/t250_qa逐字一致: ST三重剔除+剔bj)
LU = ("""SELECT code FROM stock_kline WHERE date=? AND preclose>0 AND close>0
 AND isST=0 AND upper(code_name) NOT LIKE '%ST%' AND code_name NOT LIKE '%退%'
 AND code NOT LIKE 'bj.%'
 AND close >= round(preclose*(CASE WHEN code LIKE 'sz.30%' OR code LIKE 'sh.688%'
     OR code LIKE 'sh.689%' THEN 1.20 ELSE 1.10 END),2)-0.001""")

conn = sqlite3.connect(f'file:{DB}?mode=ro', uri=True)
dates = [r[0] for r in conn.execute(
    "SELECT DISTINCT date FROM stock_kline WHERE date>='2021-01-01' "
    "AND date<='2026-07-10' ORDER BY date")]
lu_cache = {}


def lu(d):
    if d not in lu_cache:
        lu_cache[d] = {r[0] for r in conn.execute(LU, (d,))}
    return lu_cache[d]


rows = []
for D in dates:
    d1, d2 = [r[0] for r in conn.execute(
        "SELECT DISTINCT date FROM stock_kline WHERE date<? "
        "ORDER BY date DESC LIMIT 2", (D,))]
    fb = lu(d1) - lu(d2)
    promo = f"{len(fb & lu(D)) / len(fb):.6f}" if fb else ''
    tm = conn.execute(
        "SELECT AVG((hour4_close/hour4_open-1)*100) FROM stock_kline "
        "WHERE date=? AND hour3_open>0 AND hour4_open>0 AND isST=0 "
        " AND code_name NOT LIKE '%ST%' AND code_name NOT LIKE '%退%' "
        " AND code NOT LIKE 'bj.%'", (D,)).fetchone()[0]
    rows.append({'date': D, 'promo_rate': promo,
                 'tail_mean_h4': f"{tm:.4f}" if tm is not None else ''})

with open(OUT, 'w', newline='') as f:
    w = csv.DictWriter(f, ['date', 'promo_rate', 'tail_mean_h4'])
    w.writeheader()
    w.writerows(rows)
print(f"[t311_indicators] {len(rows)}行 -> {OUT} "
      f"({rows[0]['date']} ~ {rows[-1]['date']})")
