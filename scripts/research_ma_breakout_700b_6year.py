#!/usr/bin/env python3
"""
MA5/MA10均线突破 - 大盘股(>700亿)6年全周期验证 (2021-2026)

目标: 验证MA5/MA10突破策略在不同市场环境下的跨周期可靠性
筛选: 仅>700亿流通市值大盘股
T+0合规: MA5/MA10用yesterday及之前收盘价计算, 买入用today hour1 open

输出:
  - MA5 vs MA10逐年汇总对比表
  - 6年整体统计
  - 分市场环境(牛市2021H1/熊市2022/震荡2023-2024)稳定性分析
"""
import sys
import sqlite3
from collections import defaultdict
from datetime import datetime

# ========== 配置区 ==========
DB_PATH = '/home/AIWealth/data/stocks.db'
MIN_TURNOVER = 1.0          # 最低换手率(%)
MIN_RECENT_DROP = 5.0       # 近10日最少累跌幅度(%)
MIN_MARKET_CAP = 700        # 最低流通市值(亿元)
# 验证区间
YEAR_MONTHS = []
for y in range(2021, 2027):
    end_m = 6 if y == 2026 else 12
    for m in range(1, end_m + 1):
        YEAR_MONTHS.append(f"{y}-{m:02d}")
# ============================


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
    ratio = get_limit_ratio(code)
    return round(preclose * (1 + ratio), 2)


def is_limit_up(close, preclose, code):
    if preclose is None or preclose <= 0 or close is None:
        return False
    return close >= calc_limit_up(preclose, code)


def is_yizi_limit_up(open_p, high, low, close, preclose, code):
    if any(v is None for v in [open_p, high, low, close, preclose]):
        return False
    if preclose <= 0:
        return False
    limit_up = calc_limit_up(preclose, code)
    return (open_p == high == low == close) and close >= limit_up


def calc_ma(closes, period):
    if len(closes) < period:
        return None
    return sum(closes[-period:]) / period


def get_all_trading_days(cur):
    cur.execute("SELECT DISTINCT date FROM stock_kline ORDER BY date")
    return [r[0] for r in cur.fetchall()]


def find_candidates_700b(cur, today, yesterday, all_days, all_days_idx):
    """找出满足MA5/MA10突破条件的>700亿大盘股候选"""
    today_idx = all_days_idx[today]
    yesterday_idx = all_days_idx[yesterday]

    lookback_start = max(0, today_idx - 12)
    lookback_days = all_days[lookback_start:today_idx + 1]

    if len(lookback_days) < 12:
        return [], []

    if yesterday_idx < 1:
        return [], []

    # 批量获取today数据
    cur.execute("""
        SELECT code, code_name, open, high, low, close, preclose, isST, turn,
               hour1_open, hour1_high, hour1_low, hour1_close,
               amount
        FROM stock_kline WHERE date = ?
    """, (today,))
    today_rows = {r[0]: r for r in cur.fetchall()}

    # 获取yesterday数据
    cur.execute("""
        SELECT code, code_name, close, preclose, turn, isST, open, high, low
        FROM stock_kline WHERE date = ?
    """, (yesterday,))
    yesterday_rows = {r[0]: r for r in cur.fetchall()}

    # 获取lookback区间所有数据
    lb_placeholders = ','.join(['?'] * len(lookback_days))
    cur.execute(f"""
        SELECT code, date, close, high, low
        FROM stock_kline WHERE date IN ({lb_placeholders})
        ORDER BY code, date
    """, lookback_days)
    history_data = defaultdict(list)
    for r in cur.fetchall():
        history_data[r[0]].append((r[1], r[2], r[3], r[4]))

    ma5_candidates = []
    ma10_candidates = []

    for code, yd_row in yesterday_rows.items():
        code_name = yd_row[1]
        yd_close = yd_row[2]
        yd_preclose = yd_row[3]
        yd_turn = yd_row[4]
        yd_isST = yd_row[5]

        # 排除ST
        if yd_isST:
            continue
        if code_name and 'ST' in code_name.upper():
            continue

        # 排除yesterday涨停
        if yd_preclose and yd_preclose > 0 and yd_close:
            if is_limit_up(yd_close, yd_preclose, code):
                continue

        # 换手率过滤
        if yd_turn is None or yd_turn < MIN_TURNOVER:
            continue

        # today数据
        if code not in today_rows:
            continue
        t = today_rows[code]
        t_open = t[2]
        t_high = t[3]
        t_low = t[4]
        t_close = t[5]
        t_preclose = t[6]
        t_isST = t[7]
        t_turn = t[8]
        t_h1_open = t[9]
        t_amount = t[13]

        if t_isST:
            continue
        if t_open is None or t_open <= 0 or t_preclose is None or t_preclose <= 0:
            continue

        # 排除一字涨停
        if is_yizi_limit_up(t_open, t_high, t_low, t_close, t_preclose, code):
            continue

        # 估算流通市值（亿元）
        market_cap = None
        if t_amount and yd_turn and yd_turn > 0:
            market_cap = t_amount * 100 / yd_turn / 1e8

        # >700亿过滤
        if market_cap is None or market_cap < MIN_MARKET_CAP:
            continue

        # today高开确认: open > yesterday close
        if t_open <= yd_close:
            continue

        # Hour1 open作为买入价
        buy_price = t_h1_open if t_h1_open and t_h1_open > 0 else t_open

        # 获取该股的历史close列表
        hist = history_data.get(code, [])
        if not hist:
            continue

        hist_sorted = sorted(hist, key=lambda x: x[0])
        date_close_map = {h[0]: h[1] for h in hist_sorted}
        date_high_map = {h[0]: h[2] for h in hist_sorted}
        date_low_map = {h[0]: h[3] for h in hist_sorted}

        # 构建用于MA计算的close序列（到yesterday为止）
        closes_to_yesterday = []
        for d in all_days[lookback_start:today_idx]:
            if d in date_close_map and date_close_map[d] is not None:
                closes_to_yesterday.append((d, date_close_map[d]))

        if len(closes_to_yesterday) < 10:
            continue

        # MA5_yesterday
        last5_closes = [c[1] for c in closes_to_yesterday[-5:]]
        ma5_yesterday = calc_ma(last5_closes, 5)

        # MA10_yesterday
        last10_closes = [c[1] for c in closes_to_yesterday[-10:]]
        ma10_yesterday = calc_ma(last10_closes, 10)

        # MA5_T-2 / MA10_T-2
        closes_to_t2 = []
        for d in all_days[lookback_start:yesterday_idx]:
            if d in date_close_map and date_close_map[d] is not None:
                closes_to_t2.append((d, date_close_map[d]))

        ma5_t2 = None
        ma10_t2 = None
        t2_close = None
        if len(closes_to_t2) >= 5:
            last5_t2 = [c[1] for c in closes_to_t2[-5:]]
            ma5_t2 = calc_ma(last5_t2, 5)
            t2_close = closes_to_t2[-1][1]
        if len(closes_to_t2) >= 10:
            last10_t2 = [c[1] for c in closes_to_t2[-10:]]
            ma10_t2 = calc_ma(last10_t2, 10)

        # 近10日跌幅检查
        recent_highs = []
        recent_lows = []
        for d, c in closes_to_yesterday[-10:]:
            if d in date_high_map and date_high_map[d] is not None:
                recent_highs.append(date_high_map[d])
            if d in date_low_map and date_low_map[d] is not None:
                recent_lows.append(date_low_map[d])
            if c is not None:
                recent_highs.append(c)
                recent_lows.append(c)

        if not recent_highs or not recent_lows:
            continue
        max_high = max(recent_highs)
        min_low = min(recent_lows)
        if max_high <= 0:
            continue
        recent_drop_pct = (max_high - min_low) / max_high * 100
        if recent_drop_pct < MIN_RECENT_DROP:
            continue

        base_info = {
            'code': code,
            'code_name': code_name,
            'today': today,
            'buy_price': buy_price,
            'market_cap': market_cap,
            'open_rate': (t_open - t_preclose) / t_preclose * 100,
            'yd_turn': yd_turn,
        }

        # MA5突破
        if ma5_yesterday is not None and ma5_t2 is not None and t2_close is not None:
            if (t2_close < ma5_t2 and
                yd_close > ma5_yesterday and
                t_open > ma5_yesterday):
                info = base_info.copy()
                info['type'] = 'MA5'
                info['breakout_pct'] = (yd_close - ma5_yesterday) / ma5_yesterday * 100
                ma5_candidates.append(info)

        # MA10突破
        if ma10_yesterday is not None and ma10_t2 is not None and t2_close is not None:
            if (t2_close < ma10_t2 and
                yd_close > ma10_yesterday and
                t_open > ma10_yesterday):
                info = base_info.copy()
                info['type'] = 'MA10'
                info['breakout_pct'] = (yd_close - ma10_yesterday) / ma10_yesterday * 100
                ma10_candidates.append(info)

    return ma5_candidates, ma10_candidates


def calc_day_return(cur, code, buy_date, buy_price, all_days):
    """计算买入当日收益(用day close vs buy_price)"""
    if buy_price is None or buy_price <= 0:
        return None
    cur.execute("""
        SELECT close FROM stock_kline WHERE code = ? AND date = ?
    """, (code, buy_date))
    row = cur.fetchone()
    if row and row[0]:
        return (row[0] - buy_price) / buy_price * 100
    return None


def main():
    print(f"{'='*80}")
    print(f"MA5/MA10均线突破 - 大盘股(>700亿) 6年全周期验证")
    print(f"验证区间: 2021-01 ~ 2026-06")
    print(f"筛选条件: 流通市值>700亿, 换手率>{MIN_TURNOVER}%, 近10日回撤>{MIN_RECENT_DROP}%")
    print(f"买入逻辑: T+0合规, MA用yesterday之前数据计算, 买入价=today hour1 open")
    print(f"{'='*80}\n")

    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()

    all_days = get_all_trading_days(cur)
    all_days_idx = {d: i for i, d in enumerate(all_days)}
    print(f"数据库总交易日数: {len(all_days)}")
    print(f"日期范围: {all_days[0]} ~ {all_days[-1]}\n")

    # 按月统计: {month: {'ma5': [...], 'ma10': [...]}}
    monthly_stats = defaultdict(lambda: {'ma5': [], 'ma10': []})
    # 逐年统计
    yearly_stats = defaultdict(lambda: {'ma5': [], 'ma10': []})

    for month_str in YEAR_MONTHS:
        # 获取该月交易日
        cur.execute("""
            SELECT DISTINCT date FROM stock_kline
            WHERE date LIKE ? ORDER BY date
        """, (month_str + '%',))
        month_days = [r[0] for r in cur.fetchall()]

        if not month_days:
            print(f"  {month_str}: 无交易数据, 跳过")
            continue

        month_ma5_count = 0
        month_ma10_count = 0
        month_ma5_returns = []
        month_ma10_returns = []

        for today in month_days:
            if today not in all_days_idx:
                continue
            today_idx = all_days_idx[today]
            if today_idx < 12:
                continue
            yesterday = all_days[today_idx - 1]

            ma5_cands, ma10_cands = find_candidates_700b(
                cur, today, yesterday, all_days, all_days_idx)

            month_ma5_count += len(ma5_cands)
            month_ma10_count += len(ma10_cands)

            # 计算每个候选股的日收益
            for c in ma5_cands:
                ret = calc_day_return(cur, c['code'], c['today'], c['buy_price'], all_days)
                if ret is not None:
                    month_ma5_returns.append(ret)
                    monthly_stats[month_str]['ma5'].append(ret)
                    yearly_stats[month_str[:4]]['ma5'].append(ret)

            for c in ma10_cands:
                ret = calc_day_return(cur, c['code'], c['today'], c['buy_price'], all_days)
                if ret is not None:
                    month_ma10_returns.append(ret)
                    monthly_stats[month_str]['ma10'].append(ret)
                    yearly_stats[month_str[:4]]['ma10'].append(ret)

        # 月度简报
        ma5_avg = sum(month_ma5_returns) / len(month_ma5_returns) if month_ma5_returns else 0
        ma5_wr = sum(1 for r in month_ma5_returns if r > 0) / len(month_ma5_returns) * 100 if month_ma5_returns else 0
        ma10_avg = sum(month_ma10_returns) / len(month_ma10_returns) if month_ma10_returns else 0
        ma10_wr = sum(1 for r in month_ma10_returns if r > 0) / len(month_ma10_returns) * 100 if month_ma10_returns else 0

        print(f"  {month_str} | 交易日{len(month_days):>2}天 | "
              f"MA5: {month_ma5_count:>3}只 均收{ma5_avg:+.2f}% 胜率{ma5_wr:.0f}% | "
              f"MA10: {month_ma10_count:>3}只 均收{ma10_avg:+.2f}% 胜率{ma10_wr:.0f}%")

    # ========== 逐年汇总对比表 ==========
    print(f"\n\n{'='*80}")
    print(f"逐年汇总对比表 (MA5 vs MA10, >700亿大盘股)")
    print(f"{'='*80}")
    print(f"{'年份':<8}| {'MA类型':<8}| {'样本数':<8}| {'日均收益':<12}| {'胜率':<10}| {'亏损月数':<10}| {'最大单日亏':<12}| {'最大单日赚':<12}")
    print(f"{'-'*96}")

    all_ma5 = []
    all_ma10 = []

    for year in ['2021', '2022', '2023', '2024', '2025', '2026']:
        for ma_type in ['MA5', 'MA10']:
            key = 'ma5' if ma_type == 'MA5' else 'ma10'
            rets = yearly_stats[year][key]
            if not rets:
                print(f"{year:<8}| {ma_type:<8}| {'0':<8}| {'N/A':<12}| {'N/A':<10}| {'N/A':<10}| {'N/A':<12}| {'N/A':<12}")
                continue

            if ma_type == 'MA5':
                all_ma5.extend(rets)
            else:
                all_ma10.extend(rets)

            avg_ret = sum(rets) / len(rets)
            win_rate = sum(1 for r in rets if r > 0) / len(rets) * 100
            max_loss = min(rets)
            max_gain = max(rets)

            # 计算亏损月数
            loss_months = 0
            year_months = [m for m in YEAR_MONTHS if m.startswith(year)]
            for m in year_months:
                m_rets = monthly_stats[m][key]
                if m_rets and sum(m_rets) / len(m_rets) < 0:
                    loss_months += 1

            print(f"{year:<8}| {ma_type:<8}| {len(rets):<8}| {avg_ret:+.2f}%{'':>6}| "
                  f"{win_rate:.1f}%{'':>4}| {loss_months}{'':>8}| {max_loss:+.2f}%{'':>5}| {max_gain:+.2f}%{'':>5}")

    # ========== 6年整体统计 ==========
    print(f"\n\n{'='*80}")
    print(f"6年整体统计 (2021-01 ~ 2026-06)")
    print(f"{'='*80}")

    for label, rets in [("MA5突破", all_ma5), ("MA10突破", all_ma10)]:
        if not rets:
            print(f"\n  {label}: 无有效数据")
            continue
        avg_ret = sum(rets) / len(rets)
        win_rate = sum(1 for r in rets if r > 0) / len(rets) * 100
        median_ret = sorted(rets)[len(rets) // 2]
        max_loss = min(rets)
        max_gain = max(rets)
        positive_avg = sum(r for r in rets if r > 0) / max(1, sum(1 for r in rets if r > 0))
        negative_avg = sum(r for r in rets if r <= 0) / max(1, sum(1 for r in rets if r <= 0))

        print(f"\n  === {label} (>700亿大盘股) ===")
        print(f"  总样本数:     {len(rets)}")
        print(f"  日均收益:     {avg_ret:+.3f}%")
        print(f"  中位数收益:   {median_ret:+.3f}%")
        print(f"  胜率:         {win_rate:.1f}%")
        print(f"  盈利均值:     {positive_avg:+.3f}%")
        print(f"  亏损均值:     {negative_avg:+.3f}%")
        print(f"  盈亏比:       {abs(positive_avg/negative_avg):.2f}" if negative_avg != 0 else "  盈亏比: N/A")
        print(f"  最大单日赚:   {max_gain:+.2f}%")
        print(f"  最大单日亏:   {max_loss:+.2f}%")

    # ========== 市场环境分析 ==========
    print(f"\n\n{'='*80}")
    print(f"分市场环境稳定性分析")
    print(f"{'='*80}")

    env_periods = {
        '牛市2021H1': ['2021-01', '2021-02', '2021-03', '2021-04', '2021-05', '2021-06'],
        '调整2021H2': ['2021-07', '2021-08', '2021-09', '2021-10', '2021-11', '2021-12'],
        '熊市2022': [f'2022-{m:02d}' for m in range(1, 13)],
        '震荡2023': [f'2023-{m:02d}' for m in range(1, 13)],
        '震荡2024': [f'2024-{m:02d}' for m in range(1, 13)],
        '反弹2025': [f'2025-{m:02d}' for m in range(1, 13)],
        '2026H1': [f'2026-{m:02d}' for m in range(1, 7)],
    }

    print(f"\n{'环境':<14}| {'MA类型':<8}| {'样本数':<8}| {'日均收益':<12}| {'胜率':<10}| {'盈亏比':<10}")
    print(f"{'-'*72}")

    for env_name, months in env_periods.items():
        for ma_type in ['MA5', 'MA10']:
            key = 'ma5' if ma_type == 'MA5' else 'ma10'
            rets = []
            for m in months:
                rets.extend(monthly_stats[m][key])

            if not rets:
                print(f"{env_name:<14}| {ma_type:<8}| {'0':<8}| {'N/A':<12}| {'N/A':<10}| {'N/A':<10}")
                continue

            avg_ret = sum(rets) / len(rets)
            win_rate = sum(1 for r in rets if r > 0) / len(rets) * 100
            pos_avg = sum(r for r in rets if r > 0) / max(1, sum(1 for r in rets if r > 0))
            neg_avg = sum(r for r in rets if r <= 0) / max(1, sum(1 for r in rets if r <= 0))
            pnl_ratio = abs(pos_avg / neg_avg) if neg_avg != 0 else 999

            print(f"{env_name:<14}| {ma_type:<8}| {len(rets):<8}| {avg_ret:+.2f}%{'':>6}| "
                  f"{win_rate:.1f}%{'':>4}| {pnl_ratio:.2f}")

    # ========== 逐月明细 ==========
    print(f"\n\n{'='*80}")
    print(f"逐月明细统计")
    print(f"{'='*80}")
    print(f"{'月份':<10}| {'MA5样本':<8}| {'MA5均收':<10}| {'MA5胜率':<10}| {'MA10样本':<9}| {'MA10均收':<10}| {'MA10胜率':<10}")
    print(f"{'-'*78}")

    for month_str in YEAR_MONTHS:
        ma5_rets = monthly_stats[month_str]['ma5']
        ma10_rets = monthly_stats[month_str]['ma10']

        ma5_n = len(ma5_rets)
        ma5_avg = sum(ma5_rets) / ma5_n if ma5_n else 0
        ma5_wr = sum(1 for r in ma5_rets if r > 0) / ma5_n * 100 if ma5_n else 0
        ma10_n = len(ma10_rets)
        ma10_avg = sum(ma10_rets) / ma10_n if ma10_n else 0
        ma10_wr = sum(1 for r in ma10_rets if r > 0) / ma10_n * 100 if ma10_n else 0

        print(f"{month_str:<10}| {ma5_n:<8}| {ma5_avg:+.2f}%{'':>4}| {ma5_wr:.0f}%{'':>5}| "
              f"{ma10_n:<9}| {ma10_avg:+.2f}%{'':>4}| {ma10_wr:.0f}%{'':>5}")

    # ========== 结论 ==========
    print(f"\n\n{'='*80}")
    print(f"结论")
    print(f"{'='*80}")

    if all_ma5 and all_ma10:
        ma5_avg = sum(all_ma5) / len(all_ma5)
        ma10_avg = sum(all_ma10) / len(all_ma10)
        ma5_wr = sum(1 for r in all_ma5 if r > 0) / len(all_ma5) * 100
        ma10_wr = sum(1 for r in all_ma10 if r > 0) / len(all_ma10) * 100

        # 检查一致性:所有环境是否都盈利
        all_env_positive_ma5 = True
        all_env_positive_ma10 = True
        for env_name, months in env_periods.items():
            ma5_env = []
            ma10_env = []
            for m in months:
                ma5_env.extend(monthly_stats[m]['ma5'])
                ma10_env.extend(monthly_stats[m]['ma10'])
            if ma5_env and sum(ma5_env) / len(ma5_env) < 0:
                all_env_positive_ma5 = False
            if ma10_env and sum(ma10_env) / len(ma10_env) < 0:
                all_env_positive_ma10 = False

        # 亏损月数统计
        ma5_loss_months = sum(1 for m in YEAR_MONTHS
                             if monthly_stats[m]['ma5'] and
                             sum(monthly_stats[m]['ma5']) / len(monthly_stats[m]['ma5']) < 0)
        ma10_loss_months = sum(1 for m in YEAR_MONTHS
                              if monthly_stats[m]['ma10'] and
                              sum(monthly_stats[m]['ma10']) / len(monthly_stats[m]['ma10']) < 0)
        total_months_with_data_ma5 = sum(1 for m in YEAR_MONTHS if monthly_stats[m]['ma5'])
        total_months_with_data_ma10 = sum(1 for m in YEAR_MONTHS if monthly_stats[m]['ma10'])

        winner = "MA10" if ma10_avg > ma5_avg else "MA5"

        print(f"\n  1. MA5 vs MA10:")
        print(f"     MA5:  日均收益{ma5_avg:+.3f}%, 胜率{ma5_wr:.1f}%, 样本{len(all_ma5)}")
        print(f"     MA10: 日均收益{ma10_avg:+.3f}%, 胜率{ma10_wr:.1f}%, 样本{len(all_ma10)}")
        print(f"     >>> {winner}更优")

        print(f"\n  2. 跨周期可靠性:")
        print(f"     MA5:  亏损月{ma5_loss_months}/{total_months_with_data_ma5}, "
              f"所有环境盈利={'是' if all_env_positive_ma5 else '否'}")
        print(f"     MA10: 亏损月{ma10_loss_months}/{total_months_with_data_ma10}, "
              f"所有环境盈利={'是' if all_env_positive_ma10 else '否'}")

        print(f"\n  3. 策略适用性:")
        if all_env_positive_ma5 or all_env_positive_ma10:
            print(f"     ✓ 策略在多种市场环境下均有正期望收益，具备跨周期可靠性")
        else:
            print(f"     △ 策略在部分市场环境下存在负收益，需注意择时或加入过滤条件")

    conn.close()
    print(f"\n{'='*80}")
    print("验证完成。")


if __name__ == '__main__':
    main()
