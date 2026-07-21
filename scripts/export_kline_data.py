#!/usr/bin/env python3
"""
导出K线数据到结构化txt文件，供数据分析师持续分析。

数据库: /home/AIWealth/data/stocks.db
表: stock_kline (宽表格式，日K + 4小时K在同一行)

输出: /home/AIWealth/data/kline_export.txt
格式: pipe分隔，每行一个股票日记录
"""

import sqlite3
import os
import time

DB_PATH = '/home/AIWealth/data/stocks.db'
OUTPUT_PATH = '/home/AIWealth/data/kline_export.txt'

# 表头
HEADER = 'date|code|mcap_yi|turn|day_open_rate|day_close_rate|day_high_rate|day_low_rate|h1_open_rate|h1_close_rate|h1_high_rate|h1_low_rate|h2_open_rate|h2_close_rate|h2_high_rate|h2_low_rate|h3_open_rate|h3_close_rate|h3_high_rate|h3_low_rate|h4_open_rate|h4_close_rate|h4_high_rate|h4_low_rate'


def calc_mcap_yi(amount, turn):
    """流通市值(亿元) = amount / (turn/100) / 1e8"""
    if turn is None or turn == 0:
        return 0.0
    return round(amount / (turn / 100) / 100000000, 1)


def fmt_rate(val):
    """格式化rate字段，NULL填0"""
    if val is None:
        return '0'
    return str(val)


def main():
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()

    # Step 1: 统计总行数决定导出范围
    cur.execute("SELECT COUNT(*) FROM stock_kline WHERE date >= '2020-01-01' AND date <= '2026-07-01'")
    total_rows = cur.fetchone()[0]
    print(f"[INFO] 全量行数(2020-2026): {total_rows:,}")

    if total_rows > 5_000_000:
        date_start = '2024-01-01'
        print(f"[INFO] 行数>500万，缩小范围至 2024-01-01 ~ 2026-07-01")
    else:
        date_start = '2020-01-01'

    date_end = '2026-07-01'

    # Step 2: 统计有效行数(同时有日线和小时线的记录)
    cur.execute(f"""
        SELECT COUNT(*) FROM stock_kline
        WHERE date >= '{date_start}' AND date <= '{date_end}'
        AND hour1_open_rate IS NOT NULL
    """)
    valid_rows = cur.fetchone()[0]
    print(f"[INFO] 有效行数(有小时数据): {valid_rows:,}")

    # Step 3: 估算文件大小 (每行约200字节)
    est_size_gb = valid_rows * 200 / 1e9
    print(f"[INFO] 估算文件大小: {est_size_gb:.2f} GB")

    if est_size_gb > 5:
        # 只导出创业板+科创板
        code_filter = "AND (code LIKE 'sz.30%' OR code LIKE 'sh.688%')"
        print("[INFO] 文件>5GB，仅导出创业板+科创板")
    else:
        code_filter = ""

    # Step 4: 查询并导出
    sql = f"""
        SELECT date, code, amount, turn,
               open_rate, close_rate, high_rate, low_rate,
               hour1_open_rate, hour1_close_rate, hour1_high_rate, hour1_low_rate,
               hour2_open_rate, hour2_close_rate, hour2_high_rate, hour2_low_rate,
               hour3_open_rate, hour3_close_rate, hour3_high_rate, hour3_low_rate,
               hour4_open_rate, hour4_close_rate, hour4_high_rate, hour4_low_rate
        FROM stock_kline
        WHERE date >= '{date_start}' AND date <= '{date_end}'
        AND hour1_open_rate IS NOT NULL
        {code_filter}
        ORDER BY date ASC, code ASC
    """

    print(f"[INFO] 开始导出...")
    start_time = time.time()

    row_count = 0
    with open(OUTPUT_PATH, 'w') as f:
        f.write(HEADER + '\n')

        for row in cur.execute(sql):
            date, code, amount, turn = row[0], row[1], row[2], row[3]
            day_open_rate, day_close_rate, day_high_rate, day_low_rate = row[4], row[5], row[6], row[7]

            # 计算流通市值
            mcap_yi = calc_mcap_yi(amount, turn)

            # 构建输出行
            fields = [
                date,
                code,
                str(mcap_yi),
                str(round(turn, 4)) if turn else '0',
                fmt_rate(day_open_rate),
                fmt_rate(day_close_rate),
                fmt_rate(day_high_rate),
                fmt_rate(day_low_rate),
            ]
            # h1-h4 rates (index 8-23)
            for i in range(8, 24):
                fields.append(fmt_rate(row[i]))

            f.write('|'.join(fields) + '\n')
            row_count += 1

            if row_count % 500000 == 0:
                elapsed = time.time() - start_time
                print(f"[INFO] 已写入 {row_count:,} 行, 耗时 {elapsed:.1f}s")

    elapsed = time.time() - start_time
    file_size_mb = os.path.getsize(OUTPUT_PATH) / 1e6
    print(f"[DONE] 导出完成: {row_count:,} 行, 文件大小 {file_size_mb:.1f} MB, 耗时 {elapsed:.1f}s")
    print(f"[DONE] 输出文件: {OUTPUT_PATH}")

    conn.close()


if __name__ == '__main__':
    main()
