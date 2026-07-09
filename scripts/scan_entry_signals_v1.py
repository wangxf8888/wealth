#!/usr/bin/env python3
"""
Task #23: 快速扫描5种新入场信号 V1纯alpha测试

测试5种经典事件驱动入场信号在最简单V1出场(持有1天)下的alpha表现。
目标: 找出 mean>0 且至少4/6年正收益的信号。

数据库: /home/AIWealth/data/stocks.db (stock_kline表)
回测区间: 2020-01-01 ~ 2026-05-22
"""

import sqlite3
from collections import defaultdict

DB = '/home/AIWealth/data/stocks.db'

# ----------- 涨跌停判定 (基于 round(close/preclose,2) 价格比值) -----------
def is_kc_or_cy(code: str) -> bool:
    """科创板(sh.68) 或 创业板(sz.30) - 20% 限制"""
    return code.startswith('sz.30') or code.startswith('sh.68')

def is_limit_up(row) -> bool:
    pc = row['preclose']
    if pc is None or pc <= 0 or row['close'] is None:
        return False
    ratio = round(row['close'] / pc, 2)
    th = 1.20 if is_kc_or_cy(row['code']) else 1.10
    return ratio >= th

def is_limit_down(row) -> bool:
    pc = row['preclose']
    if pc is None or pc <= 0 or row['close'] is None:
        return False
    ratio = round(row['close'] / pc, 2)
    th = 0.80 if is_kc_or_cy(row['code']) else 0.90
    return ratio <= th

def is_one_word(row) -> bool:
    """一字板: open==close==high==low"""
    o, c, h, l = row['open'], row['close'], row['high'], row['low']
    if None in (o, c, h, l):
        return False
    return o == c == h == l


# ----------- 主扫描 -----------
def scan():
    conn = sqlite3.connect(DB)
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()

    fields = ('date, code, code_name, isST, preclose, open, close, high, low, '
              'open_rate, close_rate, '
              'hour1_open, hour1_high, hour1_low, hour1_close, '
              'hour2_open, hour3_close, hour4_close')

    print("Loading data (streaming by code)...", flush=True)
    cur.execute(
        f"SELECT {fields} FROM stock_kline "
        f"WHERE date >= '2020-01-01' AND code NOT LIKE 'bj.%' "
        f"ORDER BY code, date"
    )

    results = {sig: [] for sig in 'ABCDE'}  # list of (year, ret)
    code_count = 0
    row_count = 0

    prev_code = None
    buf = []

    def process(rows):
        n = len(rows)
        if n < 3:
            return
        # 跳过ST
        first_name = rows[0]['code_name'] or ''
        if 'ST' in first_name.upper():
            return
        # isST 字段也判
        # 但isST可能按日变动；逐行检查
        for i in range(n):
            r = rows[i]
            if r['isST'] == 1:
                continue
            nm = r['code_name'] or ''
            if 'ST' in nm.upper():
                continue

            r1 = rows[i + 1] if i + 1 < n else None
            r2 = rows[i + 2] if i + 2 < n else None
            rm1 = rows[i - 1] if i - 1 >= 0 else None
            rm2 = rows[i - 2] if i - 2 >= 0 else None

            year = r['date'][:4]

            # ============ Signal A: 跌停反弹 ============
            # T日跌停 (排除一字跌停) → T+1 hour1_open买 → T+2 hour1_open卖
            if r1 is not None and r2 is not None:
                if is_limit_down(r) and not is_one_word(r):
                    buy = r1['hour1_open']
                    sell = r2['hour1_open']
                    if buy and sell and buy > 0:
                        results['A'].append((year, sell / buy - 1))

            # ============ Signal B: 大幅低开反弹 ============
            # T日open_rate<=-5%; T-1非跌停; hour1_close>hour1_low
            # 买 T日 hour2_open, 卖 T+1日 hour1_open
            if r1 is not None and rm1 is not None:
                if r['open_rate'] is not None and r['open_rate'] <= -5.0:
                    if not is_limit_down(rm1):
                        h1c, h1l = r['hour1_close'], r['hour1_low']
                        if h1c is not None and h1l is not None and h1c > h1l:
                            buy = r['hour2_open']
                            sell = r1['hour1_open']
                            if buy and sell and buy > 0:
                                results['B'].append((year, sell / buy - 1))

            # ============ Signal C: 连续下跌均值回归 ============
            # T-2, T-1, T close 严格递减; T_close/T-2_close - 1 <= -0.08
            # 买 T+1 hour1_open, 卖 T+2 hour1_open
            if r1 is not None and r2 is not None and rm1 is not None and rm2 is not None:
                c0, c1, c2 = rm2['close'], rm1['close'], r['close']
                if (c0 is not None and c1 is not None and c2 is not None
                        and c0 > c1 > c2 and c0 > 0
                        and (c2 / c0 - 1) <= -0.08):
                    buy = r1['hour1_open']
                    sell = r2['hour1_open']
                    if buy and sell and buy > 0:
                        results['C'].append((year, sell / buy - 1))

            # ============ Signal D: 尾盘跳水次日反弹 ============
            # T日 hour4_close/hour3_close - 1 <= -0.03; close_rate > -5%
            # 买 T+1 hour1_open, 卖 T+2 hour1_open
            if r1 is not None and r2 is not None:
                h3c, h4c = r['hour3_close'], r['hour4_close']
                if (h3c is not None and h4c is not None and h3c > 0
                        and (h4c / h3c - 1) <= -0.03
                        and r['close_rate'] is not None and r['close_rate'] > -5.0):
                    buy = r1['hour1_open']
                    sell = r2['hour1_open']
                    if buy and sell and buy > 0:
                        results['D'].append((year, sell / buy - 1))

            # ============ Signal E: 涨停次日高开(动量延续) ============
            # T涨停且非一字板; T+1 open_rate >= 2%
            # 买 T+1 hour1_open, 卖 T+2 hour1_open
            if r1 is not None and r2 is not None:
                if is_limit_up(r):
                    yi_zi = (r['open'] == r['close']) and (r['high'] == r['low'])
                    if not yi_zi:
                        if r1['open_rate'] is not None and r1['open_rate'] >= 2.0:
                            buy = r1['hour1_open']
                            sell = r2['hour1_open']
                            if buy and sell and buy > 0:
                                results['E'].append((year, sell / buy - 1))

    for r in cur:
        row_count += 1
        if r['code'] != prev_code:
            if buf:
                process(buf)
                code_count += 1
            buf = []
            prev_code = r['code']
        buf.append(dict(r))
    if buf:
        process(buf)
        code_count += 1

    print(f"Processed {row_count} rows / {code_count} codes\n", flush=True)
    conn.close()
    return results


def report(results):
    sig_names = {
        'A': '跌停反弹',
        'B': '大幅低开反弹',
        'C': '连续下跌均值回归',
        'D': '尾盘跳水次日反弹',
        'E': '涨停次日高开(动量延续)',
    }

    summary_lines = []
    for sig in 'ABCDE':
        rs = results[sig]
        print(f"=== Signal {sig}: {sig_names[sig]} ===")
        if not rs:
            print("TOTAL: N=0")
            print("结论: 无 alpha (无样本)\n")
            summary_lines.append(f"{sig} {sig_names[sig]}: N=0, 无alpha")
            continue
        by_year = defaultdict(list)
        for y, ret in rs:
            by_year[y].append(ret)
        years = sorted(by_year.keys())
        positive_years = 0
        for y in years:
            arr = by_year[y]
            mean = sum(arr) / len(arr)
            wr = sum(1 for x in arr if x > 0) / len(arr)
            if mean > 0:
                positive_years += 1
            print(f"{y}: N={len(arr)}, mean={mean*100:+.2f}%, win_rate={wr*100:.1f}%")
        all_arr = [x[1] for x in rs]
        total_mean = sum(all_arr) / len(all_arr)
        total_wr = sum(1 for x in all_arr if x > 0) / len(all_arr)
        print(f"TOTAL: N={len(all_arr)}, mean={total_mean*100:+.2f}%, win_rate={total_wr*100:.1f}%")
        n_years = len(years)
        has_alpha = total_mean > 0 and positive_years >= 4
        print(f"结论: {'有' if has_alpha else '无'} alpha "
              f"(总均值{total_mean*100:+.2f}%, 正收益年份{positive_years}/{n_years})\n")
        summary_lines.append(
            f"{sig} {sig_names[sig]}: N={len(all_arr)}, mean={total_mean*100:+.2f}%, "
            f"win_rate={total_wr*100:.1f}%, 正年份{positive_years}/{n_years}, "
            f"{'有alpha' if has_alpha else '无alpha'}"
        )

    print("=" * 60)
    print("汇总:")
    for line in summary_lines:
        print("  " + line)


if __name__ == '__main__':
    res = scan()
    report(res)
