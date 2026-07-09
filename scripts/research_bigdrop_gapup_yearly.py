#!/usr/bin/env python3
"""
大阴次日高开买入 - 2025全年扩展验证
遍历2025-01~06，输出月度汇总表 + 市值分组分析
"""
import sys
import sqlite3
import os
from datetime import datetime

# ============ 配置区 ============
DB_PATH = "/home/AIWealth/data/stocks.db"
BIG_DROP_THRESHOLD = -5.0      # 昨日跌幅阈值(%)
GAP_UP_THRESHOLD = 2.0         # 今日高开阈值(%)
CONTEXT_DAYS = 5               # 后续查看天数
MONTHS = ["2025-01", "2025-02", "2025-03", "2025-04", "2025-05", "2025-06"]
# ================================


def get_limit_up_price(code, preclose):
    if code.startswith("sz.300") or code.startswith("sz.301"):
        return round(preclose * 1.2, 2)
    elif code.startswith("sh.688"):
        return round(preclose * 1.2, 2)
    elif code.startswith("bj."):
        return round(preclose * 1.3, 2)
    else:
        return round(preclose * 1.1, 2)


def is_yizi_limit_up(row):
    o, h, l, c, preclose, code = row['open'], row['high'], row['low'], row['close'], row['preclose'], row['code']
    if o == h == l == c and preclose > 0:
        limit_price = get_limit_up_price(code, preclose)
        if c >= limit_price:
            return True
    return False


def is_st(row):
    if row['isST'] == 1:
        return True
    name = row['code_name'] or ''
    if 'ST' in name.upper():
        return True
    return False


def get_trading_days(cursor, month_str):
    cursor.execute("""
        SELECT DISTINCT date FROM stock_kline
        WHERE date LIKE ? ORDER BY date
    """, (month_str + '%',))
    return [r[0] for r in cursor.fetchall()]


def get_all_trading_days(cursor):
    cursor.execute("SELECT DISTINCT date FROM stock_kline ORDER BY date")
    return [r[0] for r in cursor.fetchall()]


def find_candidates(cursor, today, yesterday):
    query = """
        SELECT
            t.date as today_date,
            t.code, t.code_name,
            t.open, t.high, t.low, t.close, t.preclose,
            t.close_rate as today_close_rate,
            t.volume, t.turn, t.amount, t.isST,
            t.hour1_open, t.hour1_high, t.hour1_low, t.hour1_close,
            y.date as yest_date,
            y.close_rate as y_close_rate
        FROM stock_kline t
        JOIN stock_kline y ON t.code = y.code AND y.date = ?
        WHERE t.date = ?
          AND y.close_rate <= ?
          AND t.preclose > 0
          AND ((t.open - t.preclose) / t.preclose * 100) >= ?
    """
    cursor.execute(query, (yesterday, today, BIG_DROP_THRESHOLD, GAP_UP_THRESHOLD))
    columns = [desc[0] for desc in cursor.description]
    results = []
    for row in cursor.fetchall():
        d = dict(zip(columns, row))
        if is_st(d):
            continue
        if is_yizi_limit_up(d):
            continue
        results.append(d)
    return results


def get_future_days_data(cursor, code, all_days, today_idx, n=5):
    """获取后n日数据"""
    end_idx = min(len(all_days) - 1, today_idx + n)
    days_range = all_days[today_idx + 1:end_idx + 1]
    if not days_range:
        return []
    placeholders = ','.join(['?'] * len(days_range))
    cursor.execute(f"""
        SELECT date, open, high, low, close, preclose
        FROM stock_kline
        WHERE code = ? AND date IN ({placeholders})
        ORDER BY date
    """, [code] + days_range)
    columns = [desc[0] for desc in cursor.description]
    return [dict(zip(columns, row)) for row in cursor.fetchall()]


def calc_market_cap(amount, turn):
    """流通市值 = 成交额 / 换手率 * 100 (亿元)"""
    if turn and turn > 0 and amount and amount > 0:
        return amount / turn * 100 / 1e8  # 转亿元
    return None


def get_cap_group(cap_yi):
    """按流通市值分组"""
    if cap_yi is None:
        return "未知"
    elif cap_yi < 50:
        return "50亿以下"
    elif cap_yi < 200:
        return "50-200亿"
    elif cap_yi < 700:
        return "200-700亿"
    else:
        return "700亿以上"


def process_month(cursor, month_str, all_days):
    """处理单月数据，返回候选列表"""
    month_days = get_trading_days(cursor, month_str)
    if not month_days:
        return []

    candidates_list = []
    for today in month_days:
        today_idx_all = all_days.index(today) if today in all_days else -1
        if today_idx_all <= 0:
            continue
        yesterday = all_days[today_idx_all - 1]
        candidates = find_candidates(cursor, today, yesterday)

        for cand in candidates:
            buy_price = cand['open']
            if buy_price <= 0:
                continue

            today_ret = (cand['close'] - buy_price) / buy_price * 100
            today_positive = 1 if cand['close'] > buy_price else 0

            # 后5日数据
            future_rows = get_future_days_data(cursor, cand['code'], all_days, today_idx_all, CONTEXT_DAYS)
            future_max = None
            future_min = None
            if future_rows:
                highs = [r['high'] for r in future_rows if r['high'] and r['high'] > 0]
                lows = [r['low'] for r in future_rows if r['low'] and r['low'] > 0]
                if highs:
                    future_max = (max(highs) - buy_price) / buy_price * 100
                if lows:
                    future_min = (min(lows) - buy_price) / buy_price * 100

            # 流通市值
            cap = calc_market_cap(cand.get('amount'), cand.get('turn'))

            candidates_list.append({
                'code': cand['code'],
                'name': cand['code_name'] or '',
                'date': today,
                'month': month_str,
                'buy_price': buy_price,
                'today_ret': today_ret,
                'today_positive': today_positive,
                'future_max': future_max,
                'future_min': future_min,
                'market_cap': cap,
                'cap_group': get_cap_group(cap),
            })

    return candidates_list


def calc_stats(candidates):
    """计算一组候选的统计指标"""
    if not candidates:
        return None
    n = len(candidates)
    avg_ret = sum(c['today_ret'] for c in candidates) / n
    pos_rate = sum(c['today_positive'] for c in candidates) / n * 100

    fmax_list = [c['future_max'] for c in candidates if c['future_max'] is not None]
    fmin_list = [c['future_min'] for c in candidates if c['future_min'] is not None]
    avg_fmax = sum(fmax_list) / len(fmax_list) if fmax_list else 0
    avg_fmin = sum(fmin_list) / len(fmin_list) if fmin_list else 0

    return {
        'count': n,
        'avg_ret': avg_ret,
        'pos_rate': pos_rate,
        'avg_fmax': avg_fmax,
        'avg_fmin': avg_fmin,
    }


def main():
    print("=" * 70)
    print("大阴次日高开买入 - 2025全年扩展验证")
    print(f"参数: 大阴阈值={BIG_DROP_THRESHOLD}%, 高开阈值={GAP_UP_THRESHOLD}%")
    print("=" * 70)

    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()

    all_days = get_all_trading_days(cursor)
    print(f"数据库总交易日数: {len(all_days)}")

    # 逐月处理
    all_candidates = []
    monthly_stats = {}

    for month in MONTHS:
        print(f"\n正在处理 {month} ...")
        month_cands = process_month(cursor, month, all_days)
        all_candidates.extend(month_cands)

        stats = calc_stats(month_cands)
        monthly_stats[month] = stats
        if stats:
            print(f"  候选数: {stats['count']}, 日均收益: {stats['avg_ret']:+.2f}%, "
                  f"收阳率: {stats['pos_rate']:.1f}%, "
                  f"5日最高涨: {stats['avg_fmax']:+.2f}%, 5日最大跌: {stats['avg_fmin']:+.2f}%")
        else:
            print(f"  无候选股")

    conn.close()

    # ======== 月度汇总表 ========
    print("\n\n" + "=" * 70)
    print("月度汇总表")
    print("=" * 70)
    print(f"{'月份':<10}| {'候选数':>6} | {'日均收益':>8} | {'收阳率':>6} | {'5日最高涨':>9} | {'5日最大跌':>9}")
    print(f"{'-'*10}+{'-'*8}+{'-'*10}+{'-'*8}+{'-'*11}+{'-'*11}")

    for month in MONTHS:
        s = monthly_stats.get(month)
        if s:
            print(f"{month:<10}| {s['count']:>6} | {s['avg_ret']:>+7.2f}% | {s['pos_rate']:>5.1f}% | {s['avg_fmax']:>+8.2f}% | {s['avg_fmin']:>+8.2f}%")
        else:
            print(f"{month:<10}|      0 |     N/A |   N/A |       N/A |       N/A")

    # 全年汇总
    total_stats = calc_stats(all_candidates)
    if total_stats:
        print(f"{'-'*10}+{'-'*8}+{'-'*10}+{'-'*8}+{'-'*11}+{'-'*11}")
        print(f"{'全年汇总':<8}| {total_stats['count']:>6} | {total_stats['avg_ret']:>+7.2f}% | {total_stats['pos_rate']:>5.1f}% | {total_stats['avg_fmax']:>+8.2f}% | {total_stats['avg_fmin']:>+8.2f}%")

    # ======== 市值分组分析 ========
    print("\n\n" + "=" * 70)
    print("市值分组分析（全年）")
    print("=" * 70)

    cap_groups_order = ["50亿以下", "50-200亿", "200-700亿", "700亿以上", "未知"]
    cap_grouped = {}
    for c in all_candidates:
        g = c['cap_group']
        if g not in cap_grouped:
            cap_grouped[g] = []
        cap_grouped[g].append(c)

    print(f"{'市值区间':<12}| {'候选数':>6} | {'日均收益':>8} | {'收阳率':>6} | {'5日最高涨':>9} | {'5日最大跌':>9}")
    print(f"{'-'*12}+{'-'*8}+{'-'*10}+{'-'*8}+{'-'*11}+{'-'*11}")

    for group in cap_groups_order:
        cands = cap_grouped.get(group, [])
        if not cands:
            continue
        s = calc_stats(cands)
        print(f"{group:<12}| {s['count']:>6} | {s['avg_ret']:>+7.2f}% | {s['pos_rate']:>5.1f}% | {s['avg_fmax']:>+8.2f}% | {s['avg_fmin']:>+8.2f}%")

    # ======== 市值分组 × 月度交叉分析 ========
    print("\n\n" + "=" * 70)
    print("市值分组 × 月度 日均收益交叉表")
    print("=" * 70)
    header = f"{'市值区间':<12}"
    for m in MONTHS:
        header += f"| {m:>8}"
    header += f"| {'全年':>8}"
    print(header)
    print("-" * len(header))

    for group in cap_groups_order:
        if group == "未知":
            continue
        row_str = f"{group:<12}"
        for m in MONTHS:
            month_group_cands = [c for c in all_candidates if c['month'] == m and c['cap_group'] == group]
            if month_group_cands:
                s = calc_stats(month_group_cands)
                row_str += f"| {s['avg_ret']:>+7.2f}%"
            else:
                row_str += f"|      N/A"
        # 全年
        group_all = cap_grouped.get(group, [])
        if group_all:
            s = calc_stats(group_all)
            row_str += f"| {s['avg_ret']:>+7.2f}%"
        else:
            row_str += f"|      N/A"
        print(row_str)

    print("\n完成。")


if __name__ == "__main__":
    main()
