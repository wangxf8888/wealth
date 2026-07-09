#!/usr/bin/env python3
"""
换手率突增 + 股价未明显变化 → 拉升前兆 - 候选股研究脚本

策略概念：
  近5日换手率波动不大，突然换手率成倍增大，但股价未明显变化，则后续有拉升趋势。
  逻辑：主力资金在低位悄悄吸筹——成交量/换手率突然放大，但股价没有明显上涨或下跌，
  说明有人在大量买入的同时也有人大量卖出，但价格被控制住了。这是主力吸筹的典型信号。

T+0合规：
  - 近5日换手率用yesterday及之前数据
  - "突然放大"用yesterday vs 前5日均值
  - "股价未变"用yesterday close_rate
  - T日open确认（可选：高开加分）
  - 不使用today的close/high/low/volume/turn
"""
import sys
import sqlite3
import math
from collections import defaultdict

# ==================== 配置区 ====================
DB_PATH = '/home/AIWealth/data/stocks.db'
TURN_SURGE_RATIO = 2.0        # 换手率突增倍数阈值（至少翻倍）
TURN_STD_MULTIPLE = 2.0       # 突破标准差倍数
TURN_STABILITY_CV = 0.5       # 前5日换手率变异系数(std/mean)上限
TURN_MIN_MEAN = 0.5           # 前5日换手率均值下限(%)，排除僵尸股
TURN_MAX_YESTERDAY = 20.0     # yesterday换手率上限(%)，排除异常
PRICE_FLAT_THRESHOLD = 3.0    # yesterday涨跌幅绝对值上限(%)
MAX_CANDIDATES_PER_DAY = 8    # 每天最多展示明细数
# ================================================


def get_limit_ratio(code):
    """根据股票代码确定涨跌幅比例"""
    if code.startswith('sz.300') or code.startswith('sz.301'):
        return 0.20
    elif code.startswith('sh.688'):
        return 0.20
    elif code.startswith('bj.'):
        return 0.30
    else:
        return 0.10


def calc_limit_up(preclose, code):
    """计算涨停价"""
    ratio = get_limit_ratio(code)
    return round(preclose * (1 + ratio), 2)


def calc_limit_down(preclose, code):
    """计算跌停价"""
    ratio = get_limit_ratio(code)
    return round(preclose * (1 - ratio), 2)


def is_yizi_limit(open_p, high, low, close, preclose, code):
    """判断是否一字涨停/跌停"""
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


def get_trading_days(cur, month_str):
    """获取指定月份的交易日列表"""
    cur.execute("""
        SELECT DISTINCT date FROM stock_kline
        WHERE date LIKE ? ORDER BY date
    """, (month_str + '%',))
    return [r[0] for r in cur.fetchall()]


def get_all_trading_days(cur):
    """获取所有交易日列表"""
    cur.execute("SELECT DISTINCT date FROM stock_kline ORDER BY date")
    return [r[0] for r in cur.fetchall()]


def find_candidates(cur, today, yesterday, prev_5days, all_days):
    """
    筛选候选股：
    1. 前5日(T-6~T-2)换手率平稳：std/mean < 0.5，mean > 0.5%
    2. Yesterday换手率突增：>= 前5日均值*2 或 >= mean+2*std
    3. Yesterday股价未明显变化：|close_rate| <= 3%
    4. 排除ST、一字涨停/跌停、换手率>20%
    """
    # 获取yesterday数据
    cur.execute("""
        SELECT code, code_name, open, high, low, close, preclose, turn, isST, close_rate
        FROM stock_kline WHERE date = ? AND isST = 0 AND preclose > 0
    """, (yesterday,))
    yesterday_rows = cur.fetchall()

    # 获取today数据（仅用open）
    cur.execute("""
        SELECT code, open, close, preclose,
               hour1_open, hour1_high, hour1_low, hour1_close,
               hour2_open, hour2_high, hour2_low, hour2_close,
               hour3_open, hour3_high, hour3_low, hour3_close,
               hour4_open, hour4_high, hour4_low, hour4_close
        FROM stock_kline WHERE date = ?
    """, (today,))
    today_map = {r[0]: r for r in cur.fetchall()}

    # 获取前5日换手率数据（批量查询）
    if not prev_5days:
        return []
    placeholders = ','.join(['?'] * len(prev_5days))
    cur.execute(f"""
        SELECT code, date, turn FROM stock_kline
        WHERE date IN ({placeholders}) AND turn IS NOT NULL AND turn > 0
    """, prev_5days)
    # 按code分组存储前5日换手率
    prev_turns = defaultdict(list)
    for code, date, turn in cur.fetchall():
        prev_turns[code].append((date, turn))

    candidates = []
    for row in yesterday_rows:
        code, code_name, yd_open, yd_high, yd_low, yd_close, yd_preclose, yd_turn, yd_isST, yd_close_rate = row

        # 排除ST（双重过滤）
        if yd_isST:
            continue
        if code_name and 'ST' in code_name.upper():
            continue

        # 排除无效数据
        if yd_turn is None or yd_turn <= 0:
            continue
        if yd_close_rate is None:
            continue

        # 排除一字涨停/跌停
        if is_yizi_limit(yd_open, yd_high, yd_low, yd_close, yd_preclose, code):
            continue

        # 排除换手率过高（可能是利空出逃）
        if yd_turn > TURN_MAX_YESTERDAY:
            continue

        # 条件3：Yesterday股价未明显变化
        if abs(yd_close_rate) > PRICE_FLAT_THRESHOLD:
            continue

        # 获取前5日换手率
        code_prev = prev_turns.get(code, [])
        # 排序保证按日期顺序
        code_prev.sort(key=lambda x: x[0])
        prev_turn_values = [t for _, t in code_prev]

        # 需要至少4天数据
        if len(prev_turn_values) < 4:
            continue

        # 条件1：前5日换手率平稳
        mean_turn = sum(prev_turn_values) / len(prev_turn_values)
        if mean_turn < TURN_MIN_MEAN:
            continue  # 排除僵尸股

        variance = sum((t - mean_turn) ** 2 for t in prev_turn_values) / len(prev_turn_values)
        std_turn = math.sqrt(variance)
        cv = std_turn / mean_turn if mean_turn > 0 else 999

        if cv > TURN_STABILITY_CV:
            continue  # 前5日换手率波动太大

        # 条件2：Yesterday换手率突增
        surge_ratio = yd_turn / mean_turn if mean_turn > 0 else 0
        exceed_std = yd_turn >= (mean_turn + TURN_STD_MULTIPLE * std_turn)

        if surge_ratio < TURN_SURGE_RATIO and not exceed_std:
            continue  # 没有突增

        # Today数据（仅用open）
        today_data = today_map.get(code)
        if today_data is None:
            continue
        t_open = today_data[1]
        t_close = today_data[2]
        t_preclose = today_data[3]

        if t_open is None or t_open <= 0:
            continue

        # 条件4（可选加分）：today open >= yesterday close
        open_vs_yd = (t_open - yd_close) / yd_close * 100 if yd_close > 0 else 0

        candidates.append({
            'code': code,
            'code_name': code_name,
            'today': today,
            'yesterday': yesterday,
            'yd_close': yd_close,
            'yd_preclose': yd_preclose,
            'yd_close_rate': yd_close_rate,
            'yd_turn': yd_turn,
            'prev_turn_values': prev_turn_values,
            'prev_turn_dates': [d for d, _ in code_prev],
            'mean_turn': mean_turn,
            'std_turn': std_turn,
            'surge_ratio': surge_ratio,
            't_open': t_open,
            't_preclose': t_preclose,
            'open_vs_yd': open_vs_yd,
            'not_low_open': t_open >= yd_close,
            'hour_data_today': {
                'h1': (today_data[4], today_data[5], today_data[6], today_data[7]),
                'h2': (today_data[8], today_data[9], today_data[10], today_data[11]),
                'h3': (today_data[12], today_data[13], today_data[14], today_data[15]),
                'h4': (today_data[16], today_data[17], today_data[18], today_data[19]),
            }
        })

    # 按突增倍数降序排序
    candidates.sort(key=lambda x: x['surge_ratio'], reverse=True)
    return candidates


def get_hour_data_for_days(cur, code, days_list):
    """获取指定日期列表的hour级数据"""
    if not days_list:
        return []
    placeholders = ','.join(['?'] * len(days_list))
    cur.execute(f"""
        SELECT date, open, close, preclose, turn,
               hour1_open, hour1_high, hour1_low, hour1_close,
               hour2_open, hour2_high, hour2_low, hour2_close,
               hour3_open, hour3_high, hour3_low, hour3_close,
               hour4_open, hour4_high, hour4_low, hour4_close
        FROM stock_kline WHERE code = ? AND date IN ({placeholders})
        ORDER BY date
    """, [code] + days_list)
    return cur.fetchall()


def format_hour_table(hour_rows, buy_date, buy_price, signal_date):
    """格式化hour级明细表格，含vs买入价"""
    lines = []
    lines.append(f"  {'日期':<12}| {'hour':<5}| {'open':<8}| {'high':<8}| {'low':<8}| {'close':<8}| vs买入价  | turn")
    lines.append(f"  {'-'*12}+{'-'*6}+{'-'*9}+{'-'*9}+{'-'*9}+{'-'*9}+{'-'*10}+{'-'*6}")

    for row in hour_rows:
        date = row[0]
        day_open, day_close, day_preclose, day_turn = row[1], row[2], row[3], row[4]
        hours = [
            ('h1', row[5], row[6], row[7], row[8]),
            ('h2', row[9], row[10], row[11], row[12]),
            ('h3', row[13], row[14], row[15], row[16]),
            ('h4', row[17], row[18], row[19], row[20]),
        ]

        for h_name, h_open, h_high, h_low, h_close in hours:
            if h_open is None or h_close is None:
                continue
            vs_buy = (h_close - buy_price) / buy_price * 100 if buy_price > 0 else 0
            turn_str = f"{day_turn:.1f}%" if day_turn and h_name == 'h1' else ""
            marker = ""
            if date == signal_date:
                marker = " ← 换手率突增日"
            elif date == buy_date and h_name == 'h1':
                marker = " ← 买入日"
            lines.append(
                f"  {date:<12}| {h_name:<5}| {h_open:<8.2f}| {h_high:<8.2f}| "
                f"{h_low:<8.2f}| {h_close:<8.2f}| {vs_buy:+6.2f}%   | {turn_str}{marker}"
            )
    return '\n'.join(lines)


def calc_key_metrics(cur, code, buy_date, buy_price, all_days_sorted):
    """计算关键指标：当日最高、5日内最高最低"""
    if buy_price is None or buy_price <= 0:
        return None
    try:
        idx = all_days_sorted.index(buy_date)
    except ValueError:
        return None

    future_days = all_days_sorted[idx:idx + 6]
    if len(future_days) < 2:
        return None

    placeholders = ','.join(['?'] * len(future_days))
    cur.execute(f"""
        SELECT date, open, high, low, close
        FROM stock_kline WHERE code = ? AND date IN ({placeholders})
        ORDER BY date
    """, [code] + future_days)
    rows = cur.fetchall()
    if not rows:
        return None

    metrics = {}
    today_row = rows[0] if rows[0][0] == buy_date else None
    if today_row:
        metrics['day_high'] = today_row[2]
        metrics['day_close'] = today_row[4]
        metrics['day_high_pct'] = (today_row[2] - buy_price) / buy_price * 100
        metrics['day_close_pct'] = (today_row[4] - buy_price) / buy_price * 100

    all_highs = [r[2] for r in rows if r[2] is not None]
    all_lows = [r[3] for r in rows if r[3] is not None]
    if all_highs:
        metrics['max_5d'] = max(all_highs)
        metrics['max_5d_pct'] = (max(all_highs) - buy_price) / buy_price * 100
    if all_lows:
        metrics['min_5d'] = min(all_lows)
        metrics['min_5d_pct'] = (min(all_lows) - buy_price) / buy_price * 100

    return metrics


def main():
    if len(sys.argv) < 2:
        print("用法: python research_turnover_surge_flat.py 2025-06")
        print("参数: 月份(YYYY-MM格式)")
        sys.exit(1)

    month_str = sys.argv[1]
    if len(month_str) != 7 or month_str[4] != '-':
        print(f"错误: 月份格式应为YYYY-MM，收到: {month_str}")
        sys.exit(1)

    print(f"{'=' * 80}")
    print(f"换手率突增+股价未变 → 拉升前兆 - 候选股研究")
    print(f"研究月份: {month_str}")
    print(f"突增阈值: >= {TURN_SURGE_RATIO}倍 或 超过均值+{TURN_STD_MULTIPLE}倍标准差")
    print(f"前5日换手率平稳: CV(std/mean) < {TURN_STABILITY_CV}, 均值 > {TURN_MIN_MEAN}%")
    print(f"股价变动阈值: |close_rate| <= {PRICE_FLAT_THRESHOLD}%")
    print(f"{'=' * 80}\n")

    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()

    month_days = get_trading_days(cur, month_str)
    if not month_days:
        print(f"错误: 未找到 {month_str} 的交易日数据")
        conn.close()
        sys.exit(1)

    all_days = get_all_trading_days(cur)

    print(f"该月交易日数: {len(month_days)}")
    print(f"数据库总交易日数: {len(all_days)}\n")

    # 统计汇总
    total_candidates = 0
    daily_stats = []

    # 按突增倍数分组统计
    surge_group_stats = {
        '2-3x': [],   # surge_ratio in [2, 3)
        '3-5x': [],   # surge_ratio in [3, 5)
        '5x+': [],    # surge_ratio >= 5
    }

    # Hour1买入统计
    h1_stats = []
    # Hour2确认买入统计
    h2_stats = []

    for today in month_days:
        try:
            idx = all_days.index(today)
        except ValueError:
            continue
        if idx < 7:
            continue  # 需要至少7天历史数据(5天前+yesterday+today)

        yesterday = all_days[idx - 1]
        # 前5日 = T-6到T-2（yesterday的前5天）
        prev_5days = all_days[max(0, idx - 6):idx - 1]
        if len(prev_5days) < 4:
            continue

        candidates = find_candidates(cur, today, yesterday, prev_5days, all_days)
        total_candidates += len(candidates)

        print(f"\n{'=' * 60}")
        print(f"{'=' * 10} {today} {'=' * 10}")
        print(f"{'=' * 60}")
        print(f"候选股: {len(candidates)}只")

        if not candidates:
            daily_stats.append((today, 0, None))
            continue

        # 展示明细
        shown = min(MAX_CANDIDATES_PER_DAY, len(candidates))
        for i in range(shown):
            c = candidates[i]
            print(f"\n--- {c['code']} ({c['code_name'] or 'N/A'}) ---")

            # 近5日换手率
            prev_turns_str = ', '.join([f"{t:.2f}%" for t in c['prev_turn_values']])
            print(f"  近5日换手率: [{prev_turns_str}] 均值={c['mean_turn']:.2f}%, 标准差={c['std_turn']:.2f}%")
            print(f"  Yesterday换手率: {c['yd_turn']:.2f}% (是均值的{c['surge_ratio']:.1f}倍！)")
            print(f"  Yesterday股价: close_rate={c['yd_close_rate']:+.1f}% (几乎没动)"
                  if abs(c['yd_close_rate']) <= 2 else
                  f"  Yesterday股价: close_rate={c['yd_close_rate']:+.1f}%")
            open_desc = f"高开{c['open_vs_yd']:+.1f}%" if c['open_vs_yd'] > 0 else f"低开{c['open_vs_yd']:+.1f}%"
            print(f"  Today open: {open_desc} (open={c['t_open']:.2f} preclose={c['t_preclose']:.2f})")
            print(f"")
            print(f"  解读: 换手率突增{c['surge_ratio']:.1f}倍但股价仅变{c['yd_close_rate']:+.1f}%，可能有资金在此价位大量吸筹")

            # 获取前5日+后5日hour级数据
            try:
                today_idx = all_days.index(today)
            except ValueError:
                continue
            prev5_start = max(0, today_idx - 5)
            next5_end = min(len(all_days), today_idx + 6)
            window_days = all_days[prev5_start:next5_end]

            hour_rows = get_hour_data_for_days(cur, c['code'], window_days)
            if hour_rows:
                buy_price = c['t_open']
                print(f"\n  前5日+后5日 hour级明细:")
                print(format_hour_table(hour_rows, today, buy_price, yesterday))

            # 关键指标
            buy_price = c['t_open']
            metrics = calc_key_metrics(cur, c['code'], today, buy_price, all_days)
            if metrics:
                print(f"\n  关键指标:")
                print(f"    买入价(T日h1 open): {buy_price:.2f}")
                if 'day_high' in metrics:
                    print(f"    当日最高: {metrics['day_high']:.2f} ({metrics['day_high_pct']:+.2f}%)")
                if 'day_close' in metrics:
                    print(f"    当日收盘: {metrics['day_close']:.2f} ({metrics['day_close_pct']:+.2f}%)")
                if 'max_5d' in metrics:
                    print(f"    5日内最高: {metrics['max_5d']:.2f} ({metrics['max_5d_pct']:+.2f}%)")
                if 'min_5d' in metrics:
                    print(f"    5日内最低: {metrics['min_5d']:.2f} ({metrics['min_5d_pct']:+.2f}%)")

        # 统计所有候选股的收益
        for c in candidates:
            buy_price = c['t_open']
            metrics = calc_key_metrics(cur, c['code'], today, buy_price, all_days)
            if not metrics:
                continue

            day_close_pct = metrics.get('day_close_pct', 0)
            max_5d_pct = metrics.get('max_5d_pct', 0)
            is_pos = day_close_pct > 0

            # 按突增倍数分组
            sr = c['surge_ratio']
            if sr >= 5:
                surge_group_stats['5x+'].append((day_close_pct, max_5d_pct, is_pos))
            elif sr >= 3:
                surge_group_stats['3-5x'].append((day_close_pct, max_5d_pct, is_pos))
            else:
                surge_group_stats['2-3x'].append((day_close_pct, max_5d_pct, is_pos))

            # Hour1统计
            h1_stats.append((day_close_pct, max_5d_pct, is_pos))

            # Hour2确认买入：hour1收阳
            h1_data = c['hour_data_today'].get('h1', (None, None, None, None))
            h2_data = c['hour_data_today'].get('h2', (None, None, None, None))
            h1_open, h1_high, h1_low, h1_close = h1_data
            h2_open = h2_data[0]

            if h1_open and h1_close and h1_close >= h1_open and h2_open and h2_open > 0:
                # hour1收阳 → hour2买入
                h2_buy = h2_open
                m2 = calc_key_metrics(cur, c['code'], today, h2_buy, all_days)
                if m2:
                    h2_day_pct = m2.get('day_close_pct', 0)
                    h2_max5d = m2.get('max_5d_pct', 0)
                    h2_stats.append((h2_day_pct, h2_max5d, h2_day_pct > 0))

        day_rets = [s[0] for s in h1_stats[len(h1_stats) - len(candidates):]] if candidates else []
        valid_rets = [r for r in day_rets if r is not None]
        avg_ret = sum(valid_rets) / len(valid_rets) if valid_rets else None
        daily_stats.append((today, len(candidates), avg_ret))

    # ========== 月度统计 ==========
    print(f"\n\n{'=' * 80}")
    print(f"{'=' * 10} 月度统计 {'=' * 10}")
    print(f"{'=' * 80}")
    print(f"总候选股: {total_candidates}只")

    # 突增倍数分布
    n_2_3 = len(surge_group_stats['2-3x'])
    n_3_5 = len(surge_group_stats['3-5x'])
    n_5p = len(surge_group_stats['5x+'])
    print(f"换手率突增倍数分布: 2-3倍{n_2_3}只, 3-5倍{n_3_5}只, 5倍以上{n_5p}只")

    print(f"\n按突增倍数分组统计:")
    for label, data in [('2-3倍', surge_group_stats['2-3x']),
                        ('3-5倍', surge_group_stats['3-5x']),
                        ('5倍+', surge_group_stats['5x+'])]:
        if data:
            avg_day = sum(d[0] for d in data) / len(data)
            avg_max5d = sum(d[1] for d in data) / len(data)
            pos_rate = sum(1 for d in data if d[2]) / len(data) * 100
            print(f"  {label}: 当日均值{avg_day:+.2f}%, 5日最高涨{avg_max5d:+.2f}%, 收阳占比{pos_rate:.1f}%")
        else:
            print(f"  {label}: 无数据")

    print(f"\n整体统计:")
    if h1_stats:
        h1_day_avg = sum(s[0] for s in h1_stats) / len(h1_stats)
        h1_max5d_avg = sum(s[1] for s in h1_stats) / len(h1_stats)
        h1_pos_rate = sum(1 for s in h1_stats if s[2]) / len(h1_stats) * 100
        print(f"  Hour1买入: 当日收益均值{h1_day_avg:+.2f}%, 5日最高涨均值{h1_max5d_avg:+.2f}%, 收阳占比{h1_pos_rate:.1f}%")
    else:
        print(f"  Hour1买入: 无有效数据")

    if h2_stats:
        h2_day_avg = sum(s[0] for s in h2_stats) / len(h2_stats)
        h2_max5d_avg = sum(s[1] for s in h2_stats) / len(h2_stats)
        h2_pos_rate = sum(1 for s in h2_stats if s[2]) / len(h2_stats) * 100
        print(f"  Hour2确认买入(hour1收阳): 当日收益均值{h2_day_avg:+.2f}%, 5日最高涨均值{h2_max5d_avg:+.2f}%, 收阳占比{h2_pos_rate:.1f}%")
    else:
        print(f"  Hour2确认买入(hour1收阳): 无满足条件的数据")

    # 日维度汇总
    print(f"\n{'日期':<12} {'候选数':<8} {'当日平均收益%':<15}")
    print(f"{'-' * 40}")
    valid_days = 0
    win_days = 0
    for date, count, avg_ret in daily_stats:
        if avg_ret is not None and count > 0:
            print(f"{date:<12} {count:<8} {avg_ret:+.2f}%")
            valid_days += 1
            if avg_ret > 0:
                win_days += 1
        else:
            print(f"{date:<12} {count:<8} N/A")

    if valid_days > 0:
        all_valid_rets = [r for _, c, r in daily_stats if r is not None and c > 0]
        overall_avg = sum(all_valid_rets) / len(all_valid_rets)
        print(f"\n有候选股的交易日数: {valid_days}")
        print(f"整月平均日收益: {overall_avg:+.2f}%")
        print(f"胜率(日维度): {win_days/valid_days*100:.1f}% ({win_days}/{valid_days})")
        print(f"月化收益估算: {overall_avg * valid_days:+.2f}%")

    conn.close()
    print(f"\n{'=' * 80}")
    print("研究完成。")


if __name__ == '__main__':
    main()
