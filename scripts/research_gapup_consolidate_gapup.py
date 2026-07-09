#!/usr/bin/env python3
"""
T-2高开 + T-1震荡 + T日高开 - 候选股研究脚本
策略概念：
  - T-2日：高开(open_rate>=2%)且当日收涨(close_rate>=3%)
  - T-1日：震荡整理(|close_rate|<=2%且振幅<=5%)
  - T日：再次高开(open_rate>=1%) → 买入信号(买入价=T日open)
T+0合规：T-2/T-1为历史数据，T日只用open(9:25竞价确定)
"""
import sys
import sqlite3

# ============ 配置区 ============
DB_PATH = "/home/AIWealth/data/stocks.db"
# T-2日条件
T2_OPEN_RATE_MIN = 2.0       # T-2日高开幅度阈值(%)
T2_CLOSE_RATE_MIN = 3.0      # T-2日收涨阈值(%)
# T-1日条件
T1_CLOSE_RATE_ABS_MAX = 2.0  # T-1日|close_rate|上限(%)
T1_AMPLITUDE_MAX = 5.0       # T-1日振幅上限(%)
# T日条件
T0_OPEN_RATE_MIN = 1.0       # T日高开阈值(%)
# 通用过滤
MIN_TURNOVER = 0.5           # 最低换手率(%)
CONTEXT_DAYS = 5             # 前后查看天数
# ================================


def get_limit_up_price(code, preclose):
    """计算涨停价"""
    if code.startswith("sz.300") or code.startswith("sz.301"):
        return round(preclose * 1.2, 2)
    elif code.startswith("sh.688"):
        return round(preclose * 1.2, 2)
    elif code.startswith("bj."):
        return round(preclose * 1.3, 2)
    else:
        return round(preclose * 1.1, 2)


def is_yizi_limit_up(code, open_p, high, low, close, preclose):
    """判断是否一字涨停板"""
    if open_p == high == low == close and preclose > 0:
        limit_price = get_limit_up_price(code, preclose)
        if close >= limit_price:
            return True
    return False


def get_trading_days(cursor, month_str):
    """获取指定月份的所有交易日"""
    cursor.execute("""
        SELECT DISTINCT date FROM stock_kline
        WHERE date LIKE ?
        ORDER BY date
    """, (month_str + '%',))
    return [r[0] for r in cursor.fetchall()]


def get_all_trading_days(cursor):
    """获取所有交易日列表"""
    cursor.execute("SELECT DISTINCT date FROM stock_kline ORDER BY date")
    return [r[0] for r in cursor.fetchall()]


def find_candidates(cursor, t0_date, t1_date, t2_date):
    """找出满足三日模式的候选股"""
    query = """
        SELECT
            t0.code, t0.code_name,
            -- T-2日数据
            t2.date as t2_date,
            t2.open as t2_open, t2.high as t2_high, t2.low as t2_low,
            t2.close as t2_close, t2.preclose as t2_preclose,
            t2.close_rate as t2_close_rate, t2.turn as t2_turn, t2.isST as t2_isST,
            -- T-1日数据
            t1.date as t1_date,
            t1.open as t1_open, t1.high as t1_high, t1.low as t1_low,
            t1.close as t1_close, t1.preclose as t1_preclose,
            t1.close_rate as t1_close_rate, t1.turn as t1_turn, t1.isST as t1_isST,
            -- T日数据
            t0.date as t0_date,
            t0.open as t0_open, t0.high as t0_high, t0.low as t0_low,
            t0.close as t0_close, t0.preclose as t0_preclose,
            t0.close_rate as t0_close_rate, t0.turn as t0_turn, t0.isST as t0_isST,
            t0.hour1_open, t0.hour1_high, t0.hour1_low, t0.hour1_close,
            t0.hour2_open, t0.hour2_high, t0.hour2_low, t0.hour2_close,
            t0.hour3_open, t0.hour3_high, t0.hour3_low, t0.hour3_close,
            t0.hour4_open, t0.hour4_high, t0.hour4_low, t0.hour4_close,
            t0.amount as t0_amount
        FROM stock_kline t0
        JOIN stock_kline t1 ON t0.code = t1.code AND t1.date = ?
        JOIN stock_kline t2 ON t0.code = t2.code AND t2.date = ?
        WHERE t0.date = ?
          -- T-2日：高开>=2% 且 收涨>=3%
          AND t2.preclose > 0
          AND ((t2.open - t2.preclose) / t2.preclose * 100) >= ?
          AND t2.close_rate >= ?
          -- T-1日：震荡整理 |close_rate|<=2% 且 振幅<=5%
          AND t1.preclose > 0
          AND ABS(t1.close_rate) <= ?
          AND ((t1.high - t1.low) / t1.preclose * 100) <= ?
          -- T日：高开>=1%
          AND t0.preclose > 0
          AND ((t0.open - t0.preclose) / t0.preclose * 100) >= ?
          -- 换手率过滤
          AND t0.turn > ?
    """
    cursor.execute(query, (
        t1_date, t2_date, t0_date,
        T2_OPEN_RATE_MIN, T2_CLOSE_RATE_MIN,
        T1_CLOSE_RATE_ABS_MAX, T1_AMPLITUDE_MAX,
        T0_OPEN_RATE_MIN,
        MIN_TURNOVER
    ))
    columns = [desc[0] for desc in cursor.description]
    results = []
    for row in cursor.fetchall():
        d = dict(zip(columns, row))
        # 排除ST
        if d['t0_isST'] == 1 or d['t1_isST'] == 1 or d['t2_isST'] == 1:
            continue
        name = d.get('code_name') or ''
        if 'ST' in name.upper():
            continue
        # 排除T日一字涨停
        if is_yizi_limit_up(d['code'], d['t0_open'], d['t0_high'],
                            d['t0_low'], d['t0_close'], d['t0_preclose']):
            continue
        results.append(d)
    return results


def get_context_hours(cursor, code, all_days, today_idx, n=5):
    """获取前n日+后n日的hour级数据"""
    start_idx = max(0, today_idx - n)
    end_idx = min(len(all_days) - 1, today_idx + n)
    days_range = all_days[start_idx:end_idx + 1]
    if not days_range:
        return []
    placeholders = ','.join(['?'] * len(days_range))
    cursor.execute(f"""
        SELECT date, open, high, low, close, preclose, close_rate, turn,
               hour1_open, hour1_high, hour1_low, hour1_close,
               hour2_open, hour2_high, hour2_low, hour2_close,
               hour3_open, hour3_high, hour3_low, hour3_close,
               hour4_open, hour4_high, hour4_low, hour4_close
        FROM stock_kline
        WHERE code = ? AND date IN ({placeholders})
        ORDER BY date
    """, [code] + days_range)
    columns = [desc[0] for desc in cursor.description]
    return [dict(zip(columns, row)) for row in cursor.fetchall()]


def check_hour2_confirm(cand):
    """检查Hour2确认条件：hour1收阳(close>=open) 且 hour1不破开盘价(low>=day_open)"""
    h1_open = cand.get('hour1_open')
    h1_low = cand.get('hour1_low')
    h1_close = cand.get('hour1_close')
    day_open = cand['t0_open']
    if h1_open is None or h1_open == 0 or h1_low is None or h1_close is None:
        return False, "h1数据缺失"
    cond_a = h1_low >= day_open  # hour1不破开盘价
    cond_b = h1_close >= h1_open  # hour1收阳
    passed = cond_a or cond_b
    reason = []
    if cond_a:
        reason.append(f"h1未破开盘(low={h1_low:.2f}>=open={day_open:.2f})")
    if cond_b:
        reason.append(f"h1收阳(close={h1_close:.2f}>=open={h1_open:.2f})")
    if not passed:
        reason = [f"h1破开盘(low={h1_low:.2f}<open={day_open:.2f})且h1收阴"]
    return passed, "; ".join(reason)


def fmt(val, width=6):
    """格式化浮点数"""
    if val is None or val == 0:
        return '-'.center(width)
    return f"{val:.2f}"


def print_candidate(cand, context_rows, t0_date, all_days, today_idx):
    """打印单个候选股的详细信息"""
    code = cand['code']
    name = cand.get('code_name') or ''
    buy_price = cand['t0_open']  # 买入价=T日open

    t2_open_rate = (cand['t2_open'] - cand['t2_preclose']) / cand['t2_preclose'] * 100
    t1_amplitude = (cand['t1_high'] - cand['t1_low']) / cand['t1_preclose'] * 100
    t0_open_rate = (cand['t0_open'] - cand['t0_preclose']) / cand['t0_preclose'] * 100

    print(f"\n--- {code} ({name}) ---")
    print(f"  T-2 ({cand['t2_date']}): 高开{t2_open_rate:+.1f}%, 收涨{cand['t2_close_rate']:+.1f}% "
          f"(open={cand['t2_open']:.2f} close={cand['t2_close']:.2f} preclose={cand['t2_preclose']:.2f})")
    print(f"  T-1 ({cand['t1_date']}): 震荡整理, close_rate={cand['t1_close_rate']:+.1f}%, "
          f"振幅{t1_amplitude:.1f}% "
          f"(open={cand['t1_open']:.2f} high={cand['t1_high']:.2f} "
          f"low={cand['t1_low']:.2f} close={cand['t1_close']:.2f})")
    print(f"  T日 ({cand['t0_date']}): 高开{t0_open_rate:+.1f}% "
          f"(open={cand['t0_open']:.2f} preclose={cand['t0_preclose']:.2f})")
    print()

    # Hour级明细
    print(f"  前{CONTEXT_DAYS}日+后{CONTEXT_DAYS}日 hour级明细:")
    print(f"  {'日期':<12}| {'hour':<5}| {'open':<8}| {'high':<8}| {'low':<8}| {'close':<8}| vs T日买入价")
    print(f"  {'-'*12}+{'-'*6}+{'-'*9}+{'-'*9}+{'-'*9}+{'-'*9}+{'-'*12}")

    for row in context_rows:
        d = row['date']
        is_buy_day = (d == t0_date)
        marker = " ← 买入日" if is_buy_day else ""
        for h in range(1, 5):
            ho = row.get(f'hour{h}_open')
            hh = row.get(f'hour{h}_high')
            hl = row.get(f'hour{h}_low')
            hc = row.get(f'hour{h}_close')
            if ho is None or ho == 0:
                continue
            vs_buy = (hc - buy_price) / buy_price * 100 if buy_price > 0 else 0
            tag = marker if h == 1 and is_buy_day else ""
            print(f"  {d:<12}| h{h:<4}| {ho:<8.2f}| {hh:<8.2f}| {hl:<8.2f}| {hc:<8.2f}| {vs_buy:+.2f}%{tag}")

    # 关键指标
    print()
    print(f"  关键指标:")
    print(f"    买入价(T日h1 open): {buy_price:.2f}")

    today_high = cand['t0_high']
    today_close = cand['t0_close']
    if today_high and buy_price > 0:
        print(f"    当日最高: {today_high:.2f} ({(today_high - buy_price) / buy_price * 100:+.2f}%)")
    if today_close and buy_price > 0:
        print(f"    当日收盘: {today_close:.2f} ({(today_close - buy_price) / buy_price * 100:+.2f}%)")

    # 后续天指标
    future_rows = [r for r in context_rows if r['date'] > t0_date]
    if future_rows:
        all_highs = [r['high'] for r in future_rows if r['high'] and r['high'] > 0]
        all_lows = [r['low'] for r in future_rows if r['low'] and r['low'] > 0]
        if all_highs:
            max_high = max(all_highs)
            print(f"    5日内最高: {max_high:.2f} ({(max_high - buy_price) / buy_price * 100:+.2f}%)")
        if all_lows:
            min_low = min(all_lows)
            print(f"    5日内最低: {min_low:.2f} ({(min_low - buy_price) / buy_price * 100:+.2f}%)")
    else:
        print(f"    (无后续交易日数据)")


def get_board(code):
    """获取板块分类"""
    if code.startswith('sh.6'):
        return '主板'
    elif code.startswith('sz.0'):
        return '主板'
    elif code.startswith('sz.300') or code.startswith('sz.301'):
        return '创业板'
    elif code.startswith('sh.688'):
        return '科创板'
    elif code.startswith('bj.'):
        return '北交所'
    return '其他'


def get_cap_group(market_cap):
    """按流通市值分组(亿元)"""
    if market_cap is None or market_cap <= 0:
        return None
    if market_cap < 50:
        return '<50亿'
    elif market_cap < 200:
        return '50-200亿'
    elif market_cap < 700:
        return '200-700亿'
    else:
        return '>=700亿'


def print_group_stats(group_name, items):
    """打印某个分组的统计"""
    if not items:
        print(f"    {group_name}: 无样本")
        return
    n = len(items)
    avg_ret = sum(c['today_ret'] for c in items) / n
    avg_max = sum(c['today_max'] for c in items) / n
    pos_ratio = sum(1 for c in items if c['today_ret'] > 0) / n * 100
    fmax_list = [c['future_max'] for c in items if c['future_max'] is not None]
    avg_fmax = sum(fmax_list) / len(fmax_list) if fmax_list else 0
    fmin_list = [c['future_min'] for c in items if c['future_min'] is not None]
    avg_fmin = sum(fmin_list) / len(fmin_list) if fmin_list else 0
    gt5 = sum(1 for c in items if c['future_max'] is not None and c['future_max'] >= 5)
    gt5_total = len(fmax_list)
    gt5_pct = gt5 / gt5_total * 100 if gt5_total > 0 else 0
    print(f"    {group_name}: N={n}, 当日均值={avg_ret:+.2f}%, 日内最高均值={avg_max:+.2f}%, "
          f"收阳={pos_ratio:.0f}%, 5日最高={avg_fmax:+.2f}%, 5日最低={avg_fmin:+.2f}%, 5日涨≥5%={gt5_pct:.0f}%")


def print_statistics(all_candidates):
    """打印月度统计"""
    print(f"\n\n{'=' * 10} 月度统计 {'=' * 10}")
    print(f"总候选股: {len(all_candidates)}只")

    if not all_candidates:
        print("无候选股数据")
        return

    # === Hour1买入统计 ===
    h1_rets = [c['today_ret'] for c in all_candidates]
    h1_maxs = [c['today_max'] for c in all_candidates]
    future_max_list = [c['future_max'] for c in all_candidates if c['future_max'] is not None]
    future_min_list = [c['future_min'] for c in all_candidates if c['future_min'] is not None]
    positive_count = sum(1 for c in all_candidates if c['today_ret'] > 0)

    print(f"\nHour1买入(T日open价):")
    print(f"  - 当日收益均值: {sum(h1_rets)/len(h1_rets):+.2f}%")
    print(f"  - 当日最大涨幅均值: {sum(h1_maxs)/len(h1_maxs):+.2f}%")
    if future_max_list:
        print(f"  - 5日内最高涨均值: {sum(future_max_list)/len(future_max_list):+.2f}%")
    if future_min_list:
        print(f"  - 5日内最大跌均值: {sum(future_min_list)/len(future_min_list):+.2f}%")
    print(f"  - 当日收阳占比: {positive_count/len(all_candidates)*100:.1f}%")
    # 5日涨>=5%占比
    gt5_count = sum(1 for c in all_candidates if c['future_max'] is not None and c['future_max'] >= 5)
    gt5_total = len([c for c in all_candidates if c['future_max'] is not None])
    if gt5_total > 0:
        print(f"  - 5日涨≥5%占比: {gt5_count/gt5_total*100:.1f}%")

    # === Hour2确认买入统计 ===
    h2_cands = [c for c in all_candidates if c['h2_confirm'] and c['h2_today_ret'] is not None]
    print(f"\nHour2确认买入(hour1收阳/不破开):")
    print(f"  - 样本数: {len(h2_cands)} (占Hour1的{len(h2_cands)/len(all_candidates)*100:.0f}%)")
    if h2_cands:
        h2_rets = [c['h2_today_ret'] for c in h2_cands]
        h2_fmax_list = [c['h2_future_max'] for c in h2_cands if c['h2_future_max'] is not None]
        h2_positive = sum(1 for c in h2_cands if c['h2_today_ret'] > 0)

        print(f"  - 当日收益均值: {sum(h2_rets)/len(h2_rets):+.2f}%")
        if h2_fmax_list:
            print(f"  - 5日内最高涨均值: {sum(h2_fmax_list)/len(h2_fmax_list):+.2f}%")
        print(f"  - 当日收阳占比: {h2_positive/len(h2_cands)*100:.1f}%")
    else:
        print(f"  无通过确认的候选股")

    # === 按流通市值分组 ===
    print(f"\n  【按流通市值分组】")
    cap_groups = {}
    for c in all_candidates:
        g = c.get('cap_group')
        if g:
            cap_groups.setdefault(g, []).append(c)
    for g in ['<50亿', '50-200亿', '200-700亿', '>=700亿']:
        print_group_stats(g, cap_groups.get(g, []))

    # === 按板块分组 ===
    print(f"\n  【按板块分组】")
    board_groups = {}
    for c in all_candidates:
        b = c.get('board', '其他')
        board_groups.setdefault(b, []).append(c)
    for b in ['主板', '创业板', '科创板', '北交所']:
        print_group_stats(b, board_groups.get(b, []))


def main():
    if len(sys.argv) < 2:
        print("用法: python research_gapup_consolidate_gapup.py <月份>")
        print("示例: python research_gapup_consolidate_gapup.py 2025-06")
        sys.exit(1)

    month_str = sys.argv[1]
    print(f"{'=' * 60}")
    print(f"T-2高开 + T-1震荡 + T日高开 - 候选股研究")
    print(f"参数: 月份={month_str}")
    print(f"  T-2: 高开>={T2_OPEN_RATE_MIN}%, 收涨>={T2_CLOSE_RATE_MIN}%")
    print(f"  T-1: |close_rate|<={T1_CLOSE_RATE_ABS_MAX}%, 振幅<={T1_AMPLITUDE_MAX}%")
    print(f"  T日: 高开>={T0_OPEN_RATE_MIN}%")
    print(f"  换手率>={MIN_TURNOVER}%")
    print(f"{'=' * 60}")

    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()

    all_days = get_all_trading_days(cursor)
    month_days = get_trading_days(cursor, month_str)

    if not month_days:
        print(f"错误: 未找到 {month_str} 的交易日数据")
        conn.close()
        sys.exit(1)

    print(f"该月交易日数: {len(month_days)}")
    print()

    all_candidates_stats = []

    for today in month_days:
        today_idx = all_days.index(today) if today in all_days else -1
        if today_idx < 2:
            continue
        # T-1是前一个交易日, T-2是前两个交易日
        t1_date = all_days[today_idx - 1]
        t2_date = all_days[today_idx - 2]

        candidates = find_candidates(cursor, today, t1_date, t2_date)

        print(f"\n{'=' * 10} {today} (T日) {'=' * 10}")
        print(f"候选股: {len(candidates)}只")

        for cand in candidates:
            context_rows = get_context_hours(cursor, cand['code'], all_days, today_idx, CONTEXT_DAYS)
            print_candidate(cand, context_rows, today, all_days, today_idx)

            # 收集统计数据
            buy_price = cand['t0_open']
            if buy_price and buy_price > 0:
                today_ret = (cand['t0_close'] - buy_price) / buy_price * 100
                today_max = (cand['t0_high'] - buy_price) / buy_price * 100

                # 后5日最高/最低
                future_rows = [r for r in context_rows if r['date'] > today]
                future_max = None
                future_min = None
                if future_rows:
                    highs = [r['high'] for r in future_rows if r['high'] and r['high'] > 0]
                    lows = [r['low'] for r in future_rows if r['low'] and r['low'] > 0]
                    if highs:
                        future_max = (max(highs) - buy_price) / buy_price * 100
                    if lows:
                        future_min = (min(lows) - buy_price) / buy_price * 100

                # Hour2确认
                h2_confirm, _ = check_hour2_confirm(cand)
                h2_buy_price = cand.get('hour2_open')
                h2_today_ret = None
                h2_future_max = None
                if h2_confirm and h2_buy_price and h2_buy_price > 0:
                    h2_today_ret = (cand['t0_close'] - h2_buy_price) / h2_buy_price * 100
                    if future_rows:
                        highs2 = [r['high'] for r in future_rows if r['high'] and r['high'] > 0]
                        if highs2:
                            h2_future_max = (max(highs2) - h2_buy_price) / h2_buy_price * 100

                # 计算流通市值(亿元) = amount * 100 / turn / 1e8
                t0_amount = cand.get('t0_amount') or 0
                t0_turn = cand.get('t0_turn') or 0
                market_cap = None
                if t0_turn > 0 and t0_amount > 0:
                    market_cap = t0_amount * 100 / t0_turn / 1e8

                all_candidates_stats.append({
                    'code': cand['code'],
                    'date': today,
                    'buy_price': buy_price,
                    'today_ret': today_ret,
                    'today_max': today_max,
                    'future_max': future_max,
                    'future_min': future_min,
                    'h2_confirm': h2_confirm,
                    'h2_buy_price': h2_buy_price,
                    'h2_today_ret': h2_today_ret,
                    'h2_future_max': h2_future_max,
                    'market_cap': market_cap,
                    'cap_group': get_cap_group(market_cap),
                    'board': get_board(cand['code']),
                })

    # 月度统计
    print_statistics(all_candidates_stats)

    conn.close()
    print(f"\n完成。")


if __name__ == "__main__":
    main()
