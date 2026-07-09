#!/usr/bin/env python3
"""
龙回头 + 换手率企稳 - 候选股研究脚本
策略概念：
  - 前期强势股（近10日内有涨停或累涨>=15%）
  - 之后回调3-7天（回调幅度>=8%）
  - 换手率从高位回落到低位企稳（近3日均值 < 前期涨停时换手率的50%）
  - 今日高开（open > yesterday close）
T+0合规：
  - "前期强势"：yesterday及之前10日数据可判
  - "回调天数和幅度"：yesterday及之前数据可判
  - "换手率企稳"：yesterday及之前的turn数据可判
  - "今日高开"：today open已知（9:25竞价确定）
  - "hour1确认"：hour2买入时可用hour1数据
"""
import sys
import sqlite3

DB_PATH = '/home/AIWealth/data/stocks.db'
MAX_CANDIDATES_PER_DAY = 8  # 每天最多打印的候选股明细数

# ========== 策略参数 ==========
LOOKBACK_DAYS = 10          # 前期强势回望天数
CUM_GAIN_THRESHOLD = 15.0   # 累涨阈值(%)
PULLBACK_MIN_PCT = 8.0      # 最小回调幅度(%)
PULLBACK_MIN_DAYS = 3       # 最小回调天数
PULLBACK_MAX_DAYS = 7       # 最大回调天数
TURN_RATIO_THRESHOLD = 0.50 # 换手率缩量比例（近3日均值/前期高点换手率）
MIN_TURN = 0.5              # 最低换手率(%)


def get_limit_ratio(code):
    """根据股票代码确定涨跌幅比例"""
    if code.startswith('sz.300') or code.startswith('sz.301'):
        return 0.20  # 创业板
    elif code.startswith('sh.688'):
        return 0.20  # 科创板
    elif code.startswith('bj.'):
        return 0.30  # 北交所
    else:
        return 0.10  # 主板


def calc_limit_up(preclose, code):
    """计算涨停价"""
    ratio = get_limit_ratio(code)
    return round(preclose * (1 + ratio), 2)


def is_limit_up(close, preclose, code):
    """判断是否涨停"""
    if preclose is None or preclose <= 0 or close is None:
        return False
    limit_up = calc_limit_up(preclose, code)
    return close >= limit_up


def is_yizi_limit_up(open_p, high, low, close, preclose, code):
    """判断是否一字涨停"""
    if any(v is None for v in [open_p, high, low, close, preclose]):
        return False
    if preclose <= 0:
        return False
    limit_up = calc_limit_up(preclose, code)
    return (open_p == high == low == close) and close >= limit_up


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


def find_candidates(cur, today, today_idx, all_days):
    """
    找出满足龙回头条件的候选股：
    1. 前期强势：过去10日内有涨停 或 累涨>=15%
    2. 回调：从高点回调>=8%，回调3-7天
    3. 换手率企稳：近3日均turn < 前期高点turn * 50%
    4. 今日信号：open > yesterday close（高开）
    排除：ST、一字涨停、换手率<0.5%
    """
    yesterday_idx = today_idx - 1
    if yesterday_idx < LOOKBACK_DAYS:
        return []

    yesterday = all_days[yesterday_idx]

    # 需要回望的天数范围：yesterday往前LOOKBACK_DAYS天
    lookback_start_idx = yesterday_idx - LOOKBACK_DAYS
    lookback_days = all_days[lookback_start_idx:yesterday_idx + 1]  # 包含yesterday

    # 获取today数据（用于判断高开 + 获取hour数据）
    cur.execute("""
        SELECT code, code_name, open, high, low, close, preclose, turn, isST,
               hour1_open, hour1_high, hour1_low, hour1_close,
               hour2_open, hour2_high, hour2_low, hour2_close,
               hour3_open, hour3_high, hour3_low, hour3_close,
               hour4_open, hour4_high, hour4_low, hour4_close
        FROM stock_kline WHERE date = ?
    """, (today,))
    today_map = {}
    for r in cur.fetchall():
        today_map[r[0]] = r

    # 获取回望期所有数据
    placeholders = ','.join(['?'] * len(lookback_days))
    cur.execute(f"""
        SELECT date, code, open, high, low, close, preclose, turn, isST, code_name
        FROM stock_kline
        WHERE date IN ({placeholders}) AND isST = 0 AND preclose > 0
        ORDER BY code, date
    """, lookback_days)

    # 按code分组
    from collections import defaultdict
    code_history = defaultdict(list)
    for r in cur.fetchall():
        code_history[r[1]].append({
            'date': r[0], 'code': r[1], 'open': r[2], 'high': r[3],
            'low': r[4], 'close': r[5], 'preclose': r[6], 'turn': r[7],
            'isST': r[8], 'code_name': r[9]
        })

    candidates = []

    for code, history in code_history.items():
        if len(history) < PULLBACK_MIN_DAYS + 2:
            continue

        # 排除ST
        if history[-1]['isST']:
            continue

        # 排除不在today数据中的
        if code not in today_map:
            continue

        t_row = today_map[code]
        t_code_name = t_row[1]
        t_open, t_high, t_low, t_close, t_preclose, t_turn = t_row[2], t_row[3], t_row[4], t_row[5], t_row[6], t_row[7]
        t_isST = t_row[8]

        if t_isST:
            continue
        if t_code_name and 'ST' in t_code_name.upper():
            continue
        if t_preclose is None or t_preclose <= 0 or t_open is None or t_open <= 0:
            continue

        # 排除一字涨停（today）
        if is_yizi_limit_up(t_open, t_high, t_low, t_close, t_preclose, code):
            continue

        # 排除换手率过低
        if t_turn is not None and t_turn < MIN_TURN:
            continue

        # ===== 条件1: 前期强势 =====
        # 在history中找涨停日或累涨>=15%
        limit_up_info = None  # (date, pct, turn)
        max_cum_gain = 0
        peak_price = 0
        peak_date = None
        peak_turn = 0

        for i, day in enumerate(history):
            if day['close'] is None or day['preclose'] is None:
                continue
            # 检查涨停
            if is_limit_up(day['close'], day['preclose'], code):
                # 排除一字涨停日
                if not is_yizi_limit_up(day['open'], day['high'], day['low'],
                                        day['close'], day['preclose'], code):
                    pct = (day['close'] - day['preclose']) / day['preclose'] * 100
                    turn_val = day['turn'] if day['turn'] else 0
                    if limit_up_info is None or day['date'] > limit_up_info[0]:
                        limit_up_info = (day['date'], pct, turn_val)

            # 跟踪最高点
            if day['high'] and day['high'] > peak_price:
                peak_price = day['high']
                peak_date = day['date']
                peak_turn = day['turn'] if day['turn'] else 0

        # 计算累涨(从lookback开始到peak)
        if history[0]['close'] and history[0]['close'] > 0 and peak_price > 0:
            max_cum_gain = (peak_price - history[0]['close']) / history[0]['close'] * 100

        # 必须满足：有涨停 或 累涨>=15%
        has_strong = False
        strong_info = ""
        strong_turn = 0  # 前期强势时的换手率

        if limit_up_info:
            has_strong = True
            strong_info = f"{limit_up_info[0]}涨停(+{limit_up_info[1]:.1f}%)"
            strong_turn = limit_up_info[2]
        if max_cum_gain >= CUM_GAIN_THRESHOLD:
            has_strong = True
            cum_info = f"累涨+{max_cum_gain:.1f}%"
            strong_info = f"{strong_info}, {cum_info}" if strong_info else cum_info
            if strong_turn == 0:
                strong_turn = peak_turn

        if not has_strong:
            continue

        # ===== 条件2: 回调阶段 =====
        # 从peak_date之后开始计算回调
        if peak_date is None or peak_price <= 0:
            continue

        # 找到peak之后的天数
        peak_idx_in_hist = None
        for i, day in enumerate(history):
            if day['date'] == peak_date:
                peak_idx_in_hist = i
                break

        if peak_idx_in_hist is None:
            continue

        # 回调天数 = peak之后到yesterday的天数
        pullback_days_count = len(history) - 1 - peak_idx_in_hist
        if pullback_days_count < PULLBACK_MIN_DAYS or pullback_days_count > PULLBACK_MAX_DAYS:
            continue

        # 回调幅度 = (peak - yesterday_close) / peak
        yesterday_close = history[-1]['close']
        if yesterday_close is None or yesterday_close <= 0:
            continue

        pullback_pct = (peak_price - yesterday_close) / peak_price * 100
        if pullback_pct < PULLBACK_MIN_PCT:
            continue

        # ===== 条件3: 换手率企稳 =====
        # 近3日换手率均值
        recent_turns = []
        for day in history[-3:]:
            if day['turn'] is not None and day['turn'] > 0:
                recent_turns.append(day['turn'])

        if len(recent_turns) < 2:
            continue

        avg_recent_turn = sum(recent_turns) / len(recent_turns)

        # 前期强势时的换手率（取涨停日或peak附近最高换手率）
        if strong_turn <= 0:
            # 如果没有有效的强势换手率，取前期最高换手率
            all_turns = [d['turn'] for d in history if d['turn'] and d['turn'] > 0]
            if all_turns:
                strong_turn = max(all_turns)
            else:
                continue

        turn_ratio = avg_recent_turn / strong_turn if strong_turn > 0 else 999
        if turn_ratio >= TURN_RATIO_THRESHOLD:
            continue

        # ===== 条件4: 今日高开 =====
        yd_close = history[-1]['close']
        open_rate = (t_open - yd_close) / yd_close * 100
        if open_rate <= 0:
            continue  # 必须高开

        # ===== 通过所有条件，加入候选 =====
        candidates.append({
            'code': code,
            'code_name': t_code_name,
            'today': today,
            'strong_info': strong_info,
            'peak_price': peak_price,
            'peak_date': peak_date,
            'yesterday_close': yd_close,
            'pullback_pct': pullback_pct,
            'pullback_days': pullback_days_count,
            'strong_turn': strong_turn,
            'avg_recent_turn': avg_recent_turn,
            'turn_ratio': turn_ratio,
            'open_rate': open_rate,
            't_open': t_open,
            't_high': t_high,
            't_low': t_low,
            't_close': t_close,
            't_preclose': t_preclose,
            't_turn': t_turn,
            'hour_data_today': {
                'h1': (t_row[9], t_row[10], t_row[11], t_row[12]),
                'h2': (t_row[13], t_row[14], t_row[15], t_row[16]),
                'h3': (t_row[17], t_row[18], t_row[19], t_row[20]),
                'h4': (t_row[21], t_row[22], t_row[23], t_row[24]),
            }
        })

    # 按高开幅度排序
    candidates.sort(key=lambda x: x['open_rate'], reverse=True)
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


def format_hour_table(hour_rows, buy_price):
    """格式化hour级明细表格"""
    lines = []
    lines.append(f"  {'日期':<12}| {'hour':<5}| {'open':<8}| {'high':<8}| {'low':<8}| {'close':<8}| vs买入价  | 换手率")
    lines.append(f"  {'-'*12}+{'-'*6}+{'-'*9}+{'-'*9}+{'-'*9}+{'-'*9}+{'-'*10}+{'-'*8}")

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
            turn_str = f"{day_turn:.2f}%" if day_turn else "N/A"
            lines.append(
                f"  {date:<12}| {h_name:<5}| {h_open:<8.2f}| {h_high:<8.2f}| "
                f"{h_low:<8.2f}| {h_close:<8.2f}| {vs_buy:+7.2f}%  | {turn_str}"
            )
    return '\n'.join(lines)


def calc_key_metrics(cur, code, buy_date, buy_price, all_days):
    """计算关键指标：当日最高、5日内最高最低"""
    if buy_price is None or buy_price <= 0:
        return None
    try:
        idx = all_days.index(buy_date)
    except ValueError:
        return None

    future_days = all_days[idx:idx + 6]  # 含买入日共6天
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

    # 5日内最高最低
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
        print("用法: python research_dragon_pullback.py 2025-06")
        print("参数: 月份(YYYY-MM格式)")
        sys.exit(1)

    month_str = sys.argv[1]
    if len(month_str) != 7 or month_str[4] != '-':
        print(f"错误: 月份格式应为YYYY-MM，收到: {month_str}")
        sys.exit(1)

    print(f"{'='*80}")
    print(f"龙回头 + 换手率企稳 - 候选股研究")
    print(f"研究月份: {month_str}")
    print(f"参数: 回望{LOOKBACK_DAYS}日 | 累涨阈值{CUM_GAIN_THRESHOLD}% | "
          f"回调{PULLBACK_MIN_PCT}%+ | 回调天数{PULLBACK_MIN_DAYS}-{PULLBACK_MAX_DAYS}天 | "
          f"换手率缩至{TURN_RATIO_THRESHOLD*100:.0f}%以下")
    print(f"每天最多展示: {MAX_CANDIDATES_PER_DAY} 个候选股明细")
    print(f"{'='*80}\n")

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

    # Hour1 统计
    h1_day_close_pcts = []
    h1_max_5d_pcts = []
    h1_min_5d_pcts = []
    h1_positive_count = 0
    h1_5d_ge5_count = 0

    # Hour2 统计（hour1收阳确认后买入）
    h2_day_close_pcts = []
    h2_max_5d_pcts = []
    h2_positive_count = 0
    h2_sample_count = 0

    for today in month_days:
        try:
            today_idx = all_days.index(today)
        except ValueError:
            continue

        candidates = find_candidates(cur, today, today_idx, all_days)
        total_candidates += len(candidates)

        print(f"\n{'='*60}")
        print(f"========== {today} ==========")
        print(f"候选股: {len(candidates)}只")

        if not candidates:
            continue

        shown = min(MAX_CANDIDATES_PER_DAY, len(candidates))
        for i in range(shown):
            c = candidates[i]
            print(f"\n--- {c['code']} ({c['code_name'] or 'N/A'}) ---")
            print(f"  前期强势: {c['strong_info']}")
            print(f"  回调情况: 高点{c['peak_price']:.2f}→当前{c['yesterday_close']:.2f}, "
                  f"回调-{c['pullback_pct']:.1f}%, 回调{c['pullback_days']}天")
            print(f"  换手率: 涨停日{c['strong_turn']:.2f}% → 近3日均值{c['avg_recent_turn']:.2f}%"
                  f"（缩至{c['turn_ratio']*100:.1f}%）")
            print(f"  今日信号: open={c['t_open']:.2f}, preclose={c['t_preclose']:.2f}, "
                  f"高开{c['open_rate']:+.2f}%")

            # 前5日+后5日 hour级明细
            prev5_start = max(0, today_idx - 5)
            next5_end = min(len(all_days), today_idx + 6)
            window_days = all_days[prev5_start:next5_end]

            hour_rows = get_hour_data_for_days(cur, c['code'], window_days)
            buy_price = c['t_open']
            if hour_rows:
                print(f"\n  前5日+后5日 hour级明细:")
                print(format_hour_table(hour_rows, buy_price))

            # 关键指标
            metrics = calc_key_metrics(cur, c['code'], today, buy_price, all_days)
            if metrics:
                print(f"\n  关键指标:")
                print(f"    买入价: {buy_price:.2f} (hour1 open)")
                if 'day_high' in metrics:
                    print(f"    当日最高: {metrics['day_high']:.2f} ({metrics['day_high_pct']:+.2f}%)")
                if 'day_close' in metrics:
                    print(f"    当日收盘: {metrics['day_close']:.2f} ({metrics['day_close_pct']:+.2f}%)")
                if 'max_5d' in metrics:
                    print(f"    5日内最高: {metrics['max_5d']:.2f} ({metrics['max_5d_pct']:+.2f}%)")
                if 'min_5d' in metrics:
                    print(f"    5日内最低: {metrics['min_5d']:.2f} ({metrics['min_5d_pct']:+.2f}%)")

        # 统计所有候选股的收益指标
        for c in candidates:
            buy_price = c['t_open']
            if not (buy_price and buy_price > 0):
                continue

            metrics = calc_key_metrics(cur, c['code'], today, buy_price, all_days)
            if metrics:
                # Hour1 统计
                if 'day_close_pct' in metrics:
                    h1_day_close_pcts.append(metrics['day_close_pct'])
                    if metrics['day_close_pct'] > 0:
                        h1_positive_count += 1
                if 'max_5d_pct' in metrics:
                    h1_max_5d_pcts.append(metrics['max_5d_pct'])
                    if metrics['max_5d_pct'] >= 5:
                        h1_5d_ge5_count += 1
                if 'min_5d_pct' in metrics:
                    h1_min_5d_pcts.append(metrics['min_5d_pct'])

            # Hour2 统计（hour1收阳确认）
            h1_data = c['hour_data_today']['h1']
            h2_data = c['hour_data_today']['h2']
            h1_open, h1_high, h1_low, h1_close = h1_data
            h2_open = h2_data[0] if h2_data else None

            if (h1_open and h1_close and h1_open > 0 and h1_close > 0
                    and h1_close >= h1_open  # hour1收阳
                    and h2_open and h2_open > 0):
                h2_buy_price = h2_open
                h2_metrics = calc_key_metrics(cur, c['code'], today, h2_buy_price, all_days)
                if h2_metrics:
                    h2_sample_count += 1
                    if 'day_close_pct' in h2_metrics:
                        h2_day_close_pcts.append(h2_metrics['day_close_pct'])
                        if h2_metrics['day_close_pct'] > 0:
                            h2_positive_count += 1
                    if 'max_5d_pct' in h2_metrics:
                        h2_max_5d_pcts.append(h2_metrics['max_5d_pct'])

    # ========== 月度统计 ==========
    print(f"\n\n{'='*80}")
    print(f"========== 月度统计 ==========")
    print(f"{'='*80}")
    print(f"总候选股: {total_candidates}只\n")

    print(f"Hour1直接买入:")
    if h1_day_close_pcts:
        n = len(h1_day_close_pcts)
        print(f"  - 样本数: {n}只")
        print(f"  - 当日收益均值: {sum(h1_day_close_pcts)/n:.2f}%")
        if h1_max_5d_pcts:
            print(f"  - 5日内最大涨幅均值: {sum(h1_max_5d_pcts)/len(h1_max_5d_pcts):.2f}%")
        if h1_min_5d_pcts:
            print(f"  - 5日内最大跌幅均值: {sum(h1_min_5d_pcts)/len(h1_min_5d_pcts):.2f}%")
        print(f"  - 当日收阳占比: {h1_positive_count/n*100:.1f}% ({h1_positive_count}/{n})")
        if h1_max_5d_pcts:
            print(f"  - 5日涨>=5%占比: {h1_5d_ge5_count/len(h1_max_5d_pcts)*100:.1f}% "
                  f"({h1_5d_ge5_count}/{len(h1_max_5d_pcts)})")
    else:
        print(f"  - 无有效数据")

    print(f"\nHour2确认买入(hour1收阳):")
    if h2_day_close_pcts:
        n2 = len(h2_day_close_pcts)
        print(f"  - 样本数: {h2_sample_count}只")
        print(f"  - 当日收益均值: {sum(h2_day_close_pcts)/n2:.2f}%")
        if h2_max_5d_pcts:
            print(f"  - 5日内最大涨幅均值: {sum(h2_max_5d_pcts)/len(h2_max_5d_pcts):.2f}%")
        print(f"  - 当日收阳占比: {h2_positive_count/n2*100:.1f}% ({h2_positive_count}/{n2})")
    else:
        print(f"  - 无满足hour1收阳确认条件的数据")

    conn.close()
    print(f"\n{'='*80}")
    print("研究完成。")


if __name__ == '__main__':
    main()
