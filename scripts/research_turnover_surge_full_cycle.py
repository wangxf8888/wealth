#!/usr/bin/env python3
"""
换手率突增5x+策略 - 2021-2026全周期验证脚本

目标：逐年验证换手率突增>=5倍（前5日平稳）但股价未明显上涨的选股策略
T+0合规：仅用yesterday及之前数据做判断，today仅取open作为买入价

输出：
  - 逐年逐月统计（候选数、日均收益、胜率、样本量）
  - 年度汇总表
  - 6年整体统计
  - 牛/熊/震荡周期对比
"""
import sys
import sqlite3
import math
from collections import defaultdict

# ==================== 配置区 ====================
DB_PATH = '/home/AIWealth/data/stocks.db'
TURN_SURGE_RATIO_MIN = 5.0     # 只统计5x+组
TURN_STD_MULTIPLE = 2.0
TURN_STABILITY_CV = 0.5
TURN_MIN_MEAN = 0.5
TURN_MAX_YESTERDAY = 20.0
PRICE_FLAT_THRESHOLD = 3.0
START_YEAR = 2021
END_YEAR = 2026
# ================================================


def get_limit_ratio(code):
    if code.startswith('sz.300') or code.startswith('sz.301'):
        return 0.20
    elif code.startswith('sh.688'):
        return 0.20
    elif code.startswith('bj.'):
        return 0.30
    else:
        return 0.10


def calc_limit_up(preclose, code):
    return round(preclose * (1 + get_limit_ratio(code)), 2)


def calc_limit_down(preclose, code):
    return round(preclose * (1 - get_limit_ratio(code)), 2)


def is_yizi_limit(open_p, high, low, close, preclose, code):
    if any(v is None for v in [open_p, high, low, close, preclose]):
        return False
    if preclose <= 0:
        return False
    limit_up = calc_limit_up(preclose, code)
    limit_down = calc_limit_down(preclose, code)
    if open_p == high == low == close:
        if close >= limit_up or close <= limit_down:
            return True
    return False


def get_all_trading_days(cur):
    cur.execute("SELECT DISTINCT date FROM stock_kline ORDER BY date")
    return [r[0] for r in cur.fetchall()]


def find_5x_candidates(cur, today, yesterday, prev_5days):
    """筛选换手率突增>=5x的候选股（仅5x+组）"""
    cur.execute("""
        SELECT code, code_name, open, high, low, close, preclose, turn, isST, close_rate
        FROM stock_kline WHERE date = ? AND isST = 0 AND preclose > 0
    """, (yesterday,))
    yesterday_rows = cur.fetchall()

    cur.execute("""
        SELECT code, open, close, preclose FROM stock_kline WHERE date = ?
    """, (today,))
    today_map = {r[0]: r for r in cur.fetchall()}

    if not prev_5days:
        return []
    placeholders = ','.join(['?'] * len(prev_5days))
    cur.execute(f"""
        SELECT code, date, turn FROM stock_kline
        WHERE date IN ({placeholders}) AND turn IS NOT NULL AND turn > 0
    """, prev_5days)
    prev_turns = defaultdict(list)
    for code, date, turn in cur.fetchall():
        prev_turns[code].append(turn)

    candidates = []
    for row in yesterday_rows:
        code, code_name, yd_open, yd_high, yd_low, yd_close, yd_preclose, yd_turn, yd_isST, yd_close_rate = row

        if yd_isST:
            continue
        if code_name and 'ST' in code_name.upper():
            continue
        if yd_turn is None or yd_turn <= 0:
            continue
        if yd_close_rate is None:
            continue
        if is_yizi_limit(yd_open, yd_high, yd_low, yd_close, yd_preclose, code):
            continue
        if yd_turn > TURN_MAX_YESTERDAY:
            continue
        if abs(yd_close_rate) > PRICE_FLAT_THRESHOLD:
            continue

        prev_turn_values = prev_turns.get(code, [])
        if len(prev_turn_values) < 4:
            continue

        mean_turn = sum(prev_turn_values) / len(prev_turn_values)
        if mean_turn < TURN_MIN_MEAN:
            continue

        variance = sum((t - mean_turn) ** 2 for t in prev_turn_values) / len(prev_turn_values)
        std_turn = math.sqrt(variance)
        cv = std_turn / mean_turn if mean_turn > 0 else 999
        if cv > TURN_STABILITY_CV:
            continue

        surge_ratio = yd_turn / mean_turn if mean_turn > 0 else 0
        # 只保留5x+
        if surge_ratio < TURN_SURGE_RATIO_MIN:
            continue

        today_data = today_map.get(code)
        if today_data is None:
            continue
        t_open = today_data[1]
        t_close = today_data[2]
        t_preclose = today_data[3]
        if t_open is None or t_open <= 0:
            continue

        candidates.append({
            'code': code,
            'code_name': code_name,
            'surge_ratio': surge_ratio,
            'yd_turn': yd_turn,
            'yd_close_rate': yd_close_rate,
            'mean_turn': mean_turn,
            't_open': t_open,
            't_close': t_close,
            't_preclose': t_preclose,
        })

    return candidates


def calc_day_return(cur, code, buy_date, buy_price, all_days_sorted):
    """计算当日收益（T日收盘 vs T日open）"""
    if buy_price is None or buy_price <= 0:
        return None
    cur.execute("""
        SELECT close FROM stock_kline WHERE code = ? AND date = ?
    """, (code, buy_date))
    row = cur.fetchone()
    if row and row[0] is not None:
        return (row[0] - buy_price) / buy_price * 100
    return None


def main():
    print("=" * 80)
    print("换手率突增5x+策略 - 2021-2026全周期验证")
    print(f"突增阈值: >= {TURN_SURGE_RATIO_MIN}倍")
    print(f"前5日换手率平稳: CV(std/mean) < {TURN_STABILITY_CV}, 均值 > {TURN_MIN_MEAN}%")
    print(f"股价变动阈值: |close_rate| <= {PRICE_FLAT_THRESHOLD}%")
    print("=" * 80)

    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()

    all_days = get_all_trading_days(cur)
    print(f"数据库总交易日数: {len(all_days)}")
    if all_days:
        print(f"数据范围: {all_days[0]} ~ {all_days[-1]}")

    # 构建year-month索引
    year_month_results = {}  # {(year, month): {'count': n, 'returns': [...], 'total_candidates': n}}
    year_results = {}        # {year: {'count': n, 'returns': [...], 'total_candidates': n}}

    for year in range(START_YEAR, END_YEAR + 1):
        year_results[year] = {'count': 0, 'returns': [], 'total_candidates': 0}
        max_month = 6 if year == 2026 else 12
        for month in range(1, max_month + 1):
            year_month_results[(year, month)] = {'count': 0, 'returns': [], 'total_candidates': 0}

    # 逐日扫描
    total_processed = 0
    for i, today in enumerate(all_days):
        if i < 7:
            continue
        year = int(today[:4])
        month = int(today[5:7])
        if year < START_YEAR or year > END_YEAR:
            continue
        if year == END_YEAR and month > 6:
            continue

        yesterday = all_days[i - 1]
        prev_5days = all_days[max(0, i - 6):i - 1]
        if len(prev_5days) < 4:
            continue

        candidates = find_5x_candidates(cur, today, yesterday, prev_5days)

        ym_key = (year, month)
        year_month_results[ym_key]['total_candidates'] += len(candidates)
        year_results[year]['total_candidates'] += len(candidates)

        for c in candidates:
            buy_price = c['t_open']
            ret = calc_day_return(cur, c['code'], today, buy_price, all_days)
            if ret is not None:
                year_month_results[ym_key]['returns'].append(ret)
                year_month_results[ym_key]['count'] += 1
                year_results[year]['returns'].append(ret)
                year_results[year]['count'] += 1

        total_processed += 1
        if total_processed % 200 == 0:
            print(f"  进度: 已处理 {total_processed} 个交易日...")

    # ========== 输出结果 ==========
    print(f"\n\n{'=' * 80}")
    print("一、逐年逐月统计（5x+组）")
    print("=" * 80)

    for year in range(START_YEAR, END_YEAR + 1):
        max_month = 6 if year == 2026 else 12
        print(f"\n--- {year}年 ---")
        print(f"{'月份':<8}{'5x+候选数':<12}{'样本量':<10}{'日均收益%':<12}{'胜率%':<10}")
        print("-" * 52)
        for month in range(1, max_month + 1):
            ym = year_month_results[(year, month)]
            n = ym['count']
            total_cand = ym['total_candidates']
            if n > 0:
                avg_ret = sum(ym['returns']) / n
                win_rate = sum(1 for r in ym['returns'] if r > 0) / n * 100
                print(f"{month:>2}月     {total_cand:<12}{n:<10}{avg_ret:+.2f}%       {win_rate:.1f}%")
            else:
                print(f"{month:>2}月     {total_cand:<12}{n:<10}{'N/A':<12}{'N/A'}")

    # 年度汇总
    print(f"\n\n{'=' * 80}")
    print("二、年度汇总表")
    print("=" * 80)
    print(f"{'年份':<8}{'总候选数':<12}{'有效样本':<12}{'日均收益%':<12}{'胜率%':<10}{'总收益%':<12}")
    print("-" * 66)

    all_returns = []
    for year in range(START_YEAR, END_YEAR + 1):
        yr = year_results[year]
        n = yr['count']
        total_cand = yr['total_candidates']
        if n > 0:
            avg_ret = sum(yr['returns']) / n
            win_rate = sum(1 for r in yr['returns'] if r > 0) / n * 100
            total_ret = sum(yr['returns'])
            all_returns.extend(yr['returns'])
            print(f"{year:<8}{total_cand:<12}{n:<12}{avg_ret:+.2f}%       {win_rate:.1f}%     {total_ret:+.1f}%")
        else:
            print(f"{year:<8}{total_cand:<12}{n:<12}{'N/A':<12}{'N/A':<10}{'N/A'}")

    # 6年整体统计
    print(f"\n\n{'=' * 80}")
    print("三、6年整体统计（2021-2026H1）")
    print("=" * 80)
    if all_returns:
        overall_avg = sum(all_returns) / len(all_returns)
        overall_win = sum(1 for r in all_returns if r > 0) / len(all_returns) * 100
        overall_total = sum(all_returns)
        overall_median = sorted(all_returns)[len(all_returns) // 2]
        max_ret = max(all_returns)
        min_ret = min(all_returns)
        print(f"  总样本数: {len(all_returns)}")
        print(f"  日均收益: {overall_avg:+.2f}%")
        print(f"  胜率: {overall_win:.1f}%")
        print(f"  中位数收益: {overall_median:+.2f}%")
        print(f"  最大单笔: {max_ret:+.2f}%")
        print(f"  最大亏损: {min_ret:+.2f}%")
        print(f"  累计收益: {overall_total:+.1f}%")

    # 周期对比分析
    print(f"\n\n{'=' * 80}")
    print("四、市场周期对比分析")
    print("=" * 80)

    cycle_groups = {
        '牛市(2021H1)': [],
        '调整期(2021H2)': [],
        '熊市(2022)': [],
        '震荡(2023)': [],
        '震荡(2024)': [],
        '反弹(2025)': [],
        '当前(2026H1)': [],
    }

    for year in range(START_YEAR, END_YEAR + 1):
        max_month = 6 if year == 2026 else 12
        for month in range(1, max_month + 1):
            ym = year_month_results[(year, month)]
            rets = ym['returns']
            if not rets:
                continue
            if year == 2021 and month <= 6:
                cycle_groups['牛市(2021H1)'].extend(rets)
            elif year == 2021 and month > 6:
                cycle_groups['调整期(2021H2)'].extend(rets)
            elif year == 2022:
                cycle_groups['熊市(2022)'].extend(rets)
            elif year == 2023:
                cycle_groups['震荡(2023)'].extend(rets)
            elif year == 2024:
                cycle_groups['震荡(2024)'].extend(rets)
            elif year == 2025:
                cycle_groups['反弹(2025)'].extend(rets)
            elif year == 2026:
                cycle_groups['当前(2026H1)'].extend(rets)

    print(f"{'周期':<16}{'样本数':<10}{'日均收益%':<12}{'胜率%':<10}{'中位数%':<10}")
    print("-" * 58)
    for label, rets in cycle_groups.items():
        if rets:
            avg = sum(rets) / len(rets)
            wr = sum(1 for r in rets if r > 0) / len(rets) * 100
            med = sorted(rets)[len(rets) // 2]
            print(f"{label:<16}{len(rets):<10}{avg:+.2f}%       {wr:.1f}%     {med:+.2f}%")
        else:
            print(f"{label:<16}{'0':<10}{'N/A':<12}{'N/A':<10}{'N/A'}")

    # 结论
    print(f"\n\n{'=' * 80}")
    print("五、结论")
    print("=" * 80)
    if all_returns:
        # 判断跨周期稳定性
        positive_years = 0
        total_years = 0
        for year in range(START_YEAR, END_YEAR + 1):
            yr = year_results[year]
            if yr['count'] > 0:
                total_years += 1
                if sum(yr['returns']) / yr['count'] > 0:
                    positive_years += 1

        stable = (positive_years >= total_years * 0.6 and
                  overall_avg > 1.0 and
                  overall_win > 55)

        print(f"  正收益年份: {positive_years}/{total_years}")
        print(f"  整体日均收益: {overall_avg:+.2f}% (标准>1%: {'通过' if overall_avg > 1.0 else '未通过'})")
        print(f"  整体胜率: {overall_win:.1f}% (标准>55%: {'通过' if overall_win > 55 else '未通过'})")
        print(f"  跨周期稳定性判定: {'✓ 策略跨周期稳定可靠' if stable else '✗ 策略跨周期稳定性不足'}")
    else:
        print("  无有效数据，无法判定。")

    conn.close()
    print(f"\n{'=' * 80}")
    print("全周期验证完成。")


if __name__ == '__main__':
    main()
