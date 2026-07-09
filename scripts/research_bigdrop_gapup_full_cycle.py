#!/usr/bin/env python3
"""
大阴次日高开买入 - 2021-2026全周期验证
聚焦50-200亿市值区间（最佳性价比区间）
T+0合规：大阴线用yesterday数据判断，高开用today open vs yesterday close
"""
import sys
import sqlite3
import os
from datetime import datetime
from collections import defaultdict

# ============ 配置区 ============
DB_PATH = "/home/AIWealth/data/stocks.db"
BIG_DROP_THRESHOLD = -5.0      # 昨日跌幅阈值(%)
GAP_UP_THRESHOLD = 2.0         # 今日高开阈值(%)
CONTEXT_DAYS = 5               # 后续查看天数
# 2021-01 ~ 2026-06
YEARS = [2021, 2022, 2023, 2024, 2025, 2026]
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
        return amount / turn * 100 / 1e8
    return None


def get_cap_group(cap_yi):
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


def process_month(cursor, month_str, all_days, all_days_set):
    """处理单月数据，返回候选列表"""
    month_days = get_trading_days(cursor, month_str)
    if not month_days:
        return []

    candidates_list = []
    for today in month_days:
        if today not in all_days_set:
            continue
        today_idx_all = all_days.index(today)
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

            # 当日最大涨幅（日内）
            today_max = (cand['high'] - buy_price) / buy_price * 100

            # 后5日数据
            future_rows = get_future_days_data(cursor, cand['code'], all_days, today_idx_all, CONTEXT_DAYS)
            future_max = None
            future_min = None
            next_day_ret = None
            if future_rows:
                highs = [r['high'] for r in future_rows if r['high'] and r['high'] > 0]
                lows = [r['low'] for r in future_rows if r['low'] and r['low'] > 0]
                if highs:
                    future_max = (max(highs) - buy_price) / buy_price * 100
                if lows:
                    future_min = (min(lows) - buy_price) / buy_price * 100
                # 次日收益(T+1卖出)
                next_day_ret = (future_rows[0]['close'] - buy_price) / buy_price * 100

            # 流通市值
            cap = calc_market_cap(cand.get('amount'), cand.get('turn'))

            candidates_list.append({
                'code': cand['code'],
                'name': cand['code_name'] or '',
                'date': today,
                'month': month_str,
                'year': int(month_str[:4]),
                'buy_price': buy_price,
                'today_ret': today_ret,
                'today_max': today_max,
                'today_positive': today_positive,
                'next_day_ret': next_day_ret,
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

    today_max_list = [c['today_max'] for c in candidates]
    avg_today_max = sum(today_max_list) / len(today_max_list) if today_max_list else 0

    fmax_list = [c['future_max'] for c in candidates if c['future_max'] is not None]
    fmin_list = [c['future_min'] for c in candidates if c['future_min'] is not None]
    avg_fmax = sum(fmax_list) / len(fmax_list) if fmax_list else 0
    avg_fmin = sum(fmin_list) / len(fmin_list) if fmin_list else 0

    next_day_list = [c['next_day_ret'] for c in candidates if c['next_day_ret'] is not None]
    avg_next_day = sum(next_day_list) / len(next_day_list) if next_day_list else 0
    next_day_pos = sum(1 for r in next_day_list if r > 0) / len(next_day_list) * 100 if next_day_list else 0

    return {
        'count': n,
        'avg_ret': avg_ret,
        'pos_rate': pos_rate,
        'avg_today_max': avg_today_max,
        'avg_fmax': avg_fmax,
        'avg_fmin': avg_fmin,
        'avg_next_day': avg_next_day,
        'next_day_pos': next_day_pos,
    }


def print_separator(char='=', width=90):
    print(char * width)


def main():
    print_separator()
    print("大阴次日高开买入 - 2021-2026全周期验证")
    print(f"参数: 大阴阈值={BIG_DROP_THRESHOLD}%, 高开阈值={GAP_UP_THRESHOLD}%")
    print(f"聚焦市值区间: 50-200亿（最佳性价比区间）")
    print(f"执行时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print_separator()

    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()

    all_days = get_all_trading_days(cursor)
    all_days_set = set(all_days)
    print(f"数据库总交易日数: {len(all_days)} ({all_days[0]} ~ {all_days[-1]})")

    # 生成所有月份列表
    all_months = []
    for year in YEARS:
        end_month = 6 if year == 2026 else 12
        for m in range(1, end_month + 1):
            all_months.append(f"{year}-{m:02d}")

    # 逐月处理
    all_candidates = []
    yearly_candidates = defaultdict(list)
    monthly_candidates = {}

    for month in all_months:
        print(f"  处理 {month} ...", end="", flush=True)
        month_cands = process_month(cursor, month, all_days, all_days_set)
        all_candidates.extend(month_cands)
        monthly_candidates[month] = month_cands
        year = int(month[:4])
        yearly_candidates[year].extend(month_cands)
        print(f" {len(month_cands)}个候选")

    conn.close()

    # ======================================================
    # 1. 逐年汇总表（全体 + 50-200亿）
    # ======================================================
    print("\n")
    print_separator()
    print("一、逐年汇总表")
    print_separator()

    print(f"\n{'年份':<6}| {'样本数':>6} | {'日均收益':>8} | {'胜率':>6} | {'日内最高':>8} | {'次日收益':>8} | {'次日胜率':>7} | {'5日最高':>7} | {'5日最低':>7}")
    print(f"{'-'*6}+{'-'*8}+{'-'*10}+{'-'*8}+{'-'*10}+{'-'*10}+{'-'*9}+{'-'*9}+{'-'*9}")

    print("\n--- 全体样本 ---")
    for year in YEARS:
        cands = yearly_candidates.get(year, [])
        s = calc_stats(cands)
        if s:
            print(f"{year:<6}| {s['count']:>6} | {s['avg_ret']:>+7.2f}% | {s['pos_rate']:>5.1f}% | {s['avg_today_max']:>+7.2f}% | {s['avg_next_day']:>+7.2f}% | {s['next_day_pos']:>5.1f}% | {s['avg_fmax']:>+6.2f}% | {s['avg_fmin']:>+6.2f}%")
        else:
            print(f"{year:<6}|      0 |     N/A |   N/A |      N/A |      N/A |    N/A |    N/A |    N/A")
    total_s = calc_stats(all_candidates)
    if total_s:
        print(f"{'-'*6}+{'-'*8}+{'-'*10}+{'-'*8}+{'-'*10}+{'-'*10}+{'-'*9}+{'-'*9}+{'-'*9}")
        print(f"{'合计':<5}| {total_s['count']:>6} | {total_s['avg_ret']:>+7.2f}% | {total_s['pos_rate']:>5.1f}% | {total_s['avg_today_max']:>+7.2f}% | {total_s['avg_next_day']:>+7.2f}% | {total_s['next_day_pos']:>5.1f}% | {total_s['avg_fmax']:>+6.2f}% | {total_s['avg_fmin']:>+6.2f}%")

    print("\n--- 50-200亿市值区间 ---")
    for year in YEARS:
        cands = [c for c in yearly_candidates.get(year, []) if c['cap_group'] == '50-200亿']
        s = calc_stats(cands)
        if s:
            print(f"{year:<6}| {s['count']:>6} | {s['avg_ret']:>+7.2f}% | {s['pos_rate']:>5.1f}% | {s['avg_today_max']:>+7.2f}% | {s['avg_next_day']:>+7.2f}% | {s['next_day_pos']:>5.1f}% | {s['avg_fmax']:>+6.2f}% | {s['avg_fmin']:>+6.2f}%")
        else:
            print(f"{year:<6}|      0 |     N/A |   N/A |      N/A |      N/A |    N/A |    N/A |    N/A")
    cap50_200 = [c for c in all_candidates if c['cap_group'] == '50-200亿']
    cap_s = calc_stats(cap50_200)
    if cap_s:
        print(f"{'-'*6}+{'-'*8}+{'-'*10}+{'-'*8}+{'-'*10}+{'-'*10}+{'-'*9}+{'-'*9}+{'-'*9}")
        print(f"{'合计':<5}| {cap_s['count']:>6} | {cap_s['avg_ret']:>+7.2f}% | {cap_s['pos_rate']:>5.1f}% | {cap_s['avg_today_max']:>+7.2f}% | {cap_s['avg_next_day']:>+7.2f}% | {cap_s['next_day_pos']:>5.1f}% | {cap_s['avg_fmax']:>+6.2f}% | {cap_s['avg_fmin']:>+6.2f}%")

    # ======================================================
    # 2. 各市值区间对比
    # ======================================================
    print("\n\n")
    print_separator()
    print("二、各市值区间对比（6年汇总）")
    print_separator()

    cap_groups_order = ["50亿以下", "50-200亿", "200-700亿", "700亿以上", "未知"]
    print(f"{'市值区间':<12}| {'样本数':>6} | {'日均收益':>8} | {'胜率':>6} | {'日内最高':>8} | {'次日收益':>8} | {'次日胜率':>7} | {'5日最高':>7} | {'5日最低':>7}")
    print(f"{'-'*12}+{'-'*8}+{'-'*10}+{'-'*8}+{'-'*10}+{'-'*10}+{'-'*9}+{'-'*9}+{'-'*9}")

    for group in cap_groups_order:
        cands = [c for c in all_candidates if c['cap_group'] == group]
        if not cands:
            continue
        s = calc_stats(cands)
        print(f"{group:<12}| {s['count']:>6} | {s['avg_ret']:>+7.2f}% | {s['pos_rate']:>5.1f}% | {s['avg_today_max']:>+7.2f}% | {s['avg_next_day']:>+7.2f}% | {s['next_day_pos']:>5.1f}% | {s['avg_fmax']:>+6.2f}% | {s['avg_fmin']:>+6.2f}%")

    # ======================================================
    # 3. 逐月统计（50-200亿）
    # ======================================================
    print("\n\n")
    print_separator()
    print("三、逐年逐月统计（50-200亿市值区间）")
    print_separator()

    for year in YEARS:
        end_month = 6 if year == 2026 else 12
        print(f"\n--- {year}年 ---")
        print(f"{'月份':<8}| {'候选数':>6} | {'日均收益':>8} | {'胜率':>6} | {'日内最高':>8} | {'次日收益':>8}")
        print(f"{'-'*8}+{'-'*8}+{'-'*10}+{'-'*8}+{'-'*10}+{'-'*10}")
        year_cands = []
        for m in range(1, end_month + 1):
            month_str = f"{year}-{m:02d}"
            cands = [c for c in monthly_candidates.get(month_str, []) if c['cap_group'] == '50-200亿']
            year_cands.extend(cands)
            s = calc_stats(cands)
            if s:
                print(f"{month_str:<8}| {s['count']:>6} | {s['avg_ret']:>+7.2f}% | {s['pos_rate']:>5.1f}% | {s['avg_today_max']:>+7.2f}% | {s['avg_next_day']:>+7.2f}%")
            else:
                print(f"{month_str:<8}|      0 |     N/A |   N/A |      N/A |      N/A")
        # 年度小计
        y_s = calc_stats(year_cands)
        if y_s:
            print(f"{'-'*8}+{'-'*8}+{'-'*10}+{'-'*8}+{'-'*10}+{'-'*10}")
            print(f"{'年小计':<7}| {y_s['count']:>6} | {y_s['avg_ret']:>+7.2f}% | {y_s['pos_rate']:>5.1f}% | {y_s['avg_today_max']:>+7.2f}% | {y_s['avg_next_day']:>+7.2f}%")

    # ======================================================
    # 4. 不同市场环境下的表现
    # ======================================================
    print("\n\n")
    print_separator()
    print("四、不同市场环境下的表现（50-200亿）")
    print_separator()

    market_envs = {
        '2021(牛市/结构行情)': [2021],
        '2022(熊市)': [2022],
        '2023-2024(震荡修复)': [2023, 2024],
        '2025-2026(反弹)': [2025, 2026],
    }

    print(f"{'市场环境':<24}| {'样本数':>6} | {'日均收益':>8} | {'胜率':>6} | {'日内最高':>8} | {'次日收益':>8} | {'次日胜率':>7}")
    print(f"{'-'*24}+{'-'*8}+{'-'*10}+{'-'*8}+{'-'*10}+{'-'*10}+{'-'*9}")

    for env_name, env_years in market_envs.items():
        cands = [c for c in cap50_200 if c['year'] in env_years]
        s = calc_stats(cands)
        if s:
            print(f"{env_name:<24}| {s['count']:>6} | {s['avg_ret']:>+7.2f}% | {s['pos_rate']:>5.1f}% | {s['avg_today_max']:>+7.2f}% | {s['avg_next_day']:>+7.2f}% | {s['next_day_pos']:>5.1f}%")
        else:
            print(f"{env_name:<24}|      0 |     N/A |   N/A |      N/A |      N/A |    N/A")

    # ======================================================
    # 5. 各市值区间 × 年度交叉表
    # ======================================================
    print("\n\n")
    print_separator()
    print("五、市值区间 × 年度 日均收益交叉表")
    print_separator()

    header = f"{'市值区间':<12}"
    for y in YEARS:
        header += f"| {y:>8}"
    header += f"| {'6年合计':>8}"
    print(header)
    print("-" * len(header))

    for group in cap_groups_order:
        if group == "未知":
            continue
        row_str = f"{group:<12}"
        for y in YEARS:
            cands = [c for c in yearly_candidates.get(y, []) if c['cap_group'] == group]
            if cands:
                s = calc_stats(cands)
                row_str += f"| {s['avg_ret']:>+7.2f}%"
            else:
                row_str += f"|      N/A"
        # 6年合计
        all_group = [c for c in all_candidates if c['cap_group'] == group]
        if all_group:
            s = calc_stats(all_group)
            row_str += f"| {s['avg_ret']:>+7.2f}%"
        else:
            row_str += f"|      N/A"
        print(row_str)

    # ======================================================
    # 6. 结论
    # ======================================================
    print("\n\n")
    print_separator()
    print("六、结论")
    print_separator()

    if cap_s:
        print(f"\n【50-200亿市值区间 6年汇总】")
        print(f"  总样本数: {cap_s['count']}")
        print(f"  日均收益(当日close vs buy open): {cap_s['avg_ret']:+.2f}%")
        print(f"  胜率(当日收阳占比): {cap_s['pos_rate']:.1f}%")
        print(f"  日内最大涨幅均值: {cap_s['avg_today_max']:+.2f}%")
        print(f"  次日收益均值: {cap_s['avg_next_day']:+.2f}%")
        print(f"  次日胜率: {cap_s['next_day_pos']:.1f}%")
        print(f"  5日内最大涨幅均值: {cap_s['avg_fmax']:+.2f}%")
        print(f"  5日内最大跌幅均值: {cap_s['avg_fmin']:+.2f}%")

        # 跨周期稳定性判断
        print(f"\n【跨周期稳定性判断】")
        yearly_rets = []
        yearly_posrates = []
        all_positive_years = True
        for year in YEARS:
            cands = [c for c in yearly_candidates.get(year, []) if c['cap_group'] == '50-200亿']
            s = calc_stats(cands)
            if s:
                yearly_rets.append(s['avg_ret'])
                yearly_posrates.append(s['pos_rate'])
                status = "✓" if s['avg_ret'] > 0 else "✗"
                if s['avg_ret'] <= 0:
                    all_positive_years = False
                print(f"  {year}: 日均{s['avg_ret']:+.2f}%, 胜率{s['pos_rate']:.1f}% {status}")
            else:
                print(f"  {year}: 无数据")

        if yearly_rets:
            min_ret = min(yearly_rets)
            max_ret = max(yearly_rets)
            std_ret = (sum((r - cap_s['avg_ret'])**2 for r in yearly_rets) / len(yearly_rets)) ** 0.5
            min_pos = min(yearly_posrates)

            print(f"\n  年度收益范围: [{min_ret:+.2f}%, {max_ret:+.2f}%]")
            print(f"  年度收益标准差: {std_ret:.2f}%")
            print(f"  最低年度胜率: {min_pos:.1f}%")
            print(f"  是否每年都为正收益: {'是' if all_positive_years else '否'}")

            if all_positive_years and min_pos >= 55:
                print(f"\n  ★★★ 结论：策略跨周期稳定可靠 ★★★")
                print(f"  每年均为正收益，最低胜率{min_pos:.1f}%≥55%，适合实盘部署。")
            elif all_positive_years:
                print(f"\n  ★★ 结论：策略跨周期正收益稳定，但部分年份胜率偏低 ★★")
                print(f"  建议配合止损/止盈优化以提高胜率。")
            elif min_ret > -0.5:
                print(f"\n  ★ 结论：策略大部分年份正收益，存在弱势期但回撤可控 ★")
                print(f"  建议在熊市环境减仓或暂停使用。")
            else:
                print(f"\n  ✗ 结论：策略跨周期不够稳定，存在明显负收益年份 ✗")
                print(f"  建议进一步优化信号过滤条件或限定使用场景。")

    print(f"\n\n完成。执行时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")


if __name__ == "__main__":
    main()
