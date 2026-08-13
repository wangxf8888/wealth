#!/usr/bin/env python3
"""[Task #10] index_kline 缺行回补: 以 stock_kline 交易日为基准,
补齐 sh.000001 缺失日期行(BaoStock拉取, 失败重试)。

sz.399001 裁定更新[Task#295]: 已推翻废弃裁定恢复维护, 缺口回补与日常检测
由 backfill_index_kline_tencent.py(腾讯源, 双指数)承担; 本工具仍仅补sh.000001。

用法: python3 tools/backfill_index_kline.py
"""
import math
import sqlite3
import time

import baostock as bs

DB = '/home/AIWealth/data/stocks.db'


def safe_float(val):
    if val is None or val == '':
        return None
    try:
        v = float(val)
        return None if math.isnan(v) or math.isinf(v) else v
    except (ValueError, TypeError):
        return None


def calc_rate(price, preclose):
    if price is None or preclose is None or preclose == 0:
        return None
    return round((price / preclose - 1) * 100, 4)


def main():
    conn = sqlite3.connect(DB)
    missing = [r[0] for r in conn.execute(
        "SELECT DISTINCT s.date FROM stock_kline s "
        "LEFT JOIN index_kline i ON i.date=s.date AND i.code='sh.000001' "
        "WHERE i.date IS NULL ORDER BY s.date")]
    if not missing:
        print("index_kline sh.000001 无缺行")
        conn.close()
        return
    print(f"缺行: {missing}")

    lg = bs.login()
    if lg.error_code != '0':
        print(f"BaoStock登录失败: {lg.error_msg}")
        conn.close()
        return

    filled = 0
    for d in missing:
        for attempt in range(3):  # 限流/断线重试
            rs = bs.query_history_k_data_plus(
                'sh.000001',
                'date,code,open,high,low,close,preclose,volume,amount',
                start_date=d, end_date=d, frequency='d')
            rows = []
            while rs.error_code == '0' and rs.next():
                rows.append(rs.get_row_data())
            if rows:
                break
            print(f"{d}: 第{attempt + 1}次拉取为空/失败, 重试...")
            time.sleep(2)
        if not rows:
            print(f"{d}: 3次重试仍失败, 跳过")
            continue
        row = rows[0]
        o, h, l, c, pre = [safe_float(x) for x in row[2:7]]
        vol, amt = safe_float(row[7]), safe_float(row[8])
        vals = [row[0], row[1], '上证综指', o, h, l, c, pre, vol, amt,
                calc_rate(o, pre), calc_rate(h, pre),
                calc_rate(l, pre), calc_rate(c, pre)]
        vals.extend([None] * 40)  # hour1-4 占位
        vals.append(None)         # red_ratio 由 update_red_ratio.py 生成
        conn.execute(
            'INSERT OR REPLACE INTO index_kline VALUES ('
            + ','.join(['?'] * 55) + ')', vals)
        conn.commit()  # 短事务, 避免长锁
        filled += 1
        print(f"{d}: close={c} 已补")
    bs.logout()
    conn.close()
    print(f"完成: 补{filled}/{len(missing)}行")


if __name__ == '__main__':
    main()
