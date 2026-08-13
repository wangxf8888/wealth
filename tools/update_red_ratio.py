#!/usr/bin/env python3
"""红盘占比(red_ratio)盘后生成工具

定义(与index_kline历史存量值逐位核验一致):
  red_ratio = 当日全市场(含ST) close>=preclose 的家数占比 * 100
存储位置: index_kline 表 code='sh.000001' 行的 red_ratio 列。
时效规范: 盘后计算当日值, 策略只允许使用昨日及之前的值(次日使用)。

用法:
  python3 tools/update_red_ratio.py            # 补齐所有red_ratio为NULL的日期
  python3 tools/update_red_ratio.py 2026-07-23 # 只计算指定日期
被 tools/daily_update.sh 在指数K线更新后自动调用。
"""
import sqlite3
import sys

DB = '/home/AIWealth/data/stocks.db'


def calc_red_ratio(conn, date):
    r, n = conn.execute(
        "SELECT SUM(close>=preclose), COUNT(*) FROM stock_kline "
        "WHERE date=? AND close IS NOT NULL AND preclose>0", (date,)).fetchone()
    if not n:
        return None
    return round(r / n * 100, 2)


def main():
    conn = sqlite3.connect(DB)
    if len(sys.argv) > 1:
        dates = [sys.argv[1]]
    else:
        dates = [r[0] for r in conn.execute(
            "SELECT date FROM index_kline WHERE code='sh.000001' "
            "AND red_ratio IS NULL ORDER BY date")]
    updated = 0
    for d in dates:
        rr = calc_red_ratio(conn, d)
        if rr is None:
            print(f"{d}: stock_kline无当日数据, 跳过")
            continue
        conn.execute(
            "UPDATE index_kline SET red_ratio=? WHERE code='sh.000001' "
            "AND date=?", (rr, d))
        updated += 1
        print(f"{d}: red_ratio={rr}")
    conn.commit()
    conn.close()
    print(f"完成: 更新{updated}个交易日")


if __name__ == '__main__':
    main()
