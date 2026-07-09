#!/usr/bin/env python3
"""
首板次日高开买入 - 候选股研究脚本
策略概念：
  - 昨日涨停（首板，非连板）
  - 今日竞价高开 >= X%
  - 今日hour1以open价买入
T+0合规：买入决策仅基于昨日收盘后已确定的涨停事实 + 今日9:25竞价确定的open价
"""
import sys
import sqlite3
from collections import defaultdict

DB_PATH = '/home/AIWealth/data/stocks.db'
GAP_UP_THRESHOLD = 2.0  # 高开阈值(%)
MAX_CANDIDATES_PER_DAY = 5  # 每天最多打印的候选股明细数

# Hour2确认条件类型
H2_CONFIRM_POSITIVE = 'h1_positive'   # hour1收阳: h1_close >= h1_open
H2_CONFIRM_NOT_BREAK = 'h1_not_break'  # hour1不破开盘价: h1_low >= today_open


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
    """判断是否一字涨停（open==high==low==close且>=涨停价）"""
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


def find_candidates(cur, today, yesterday, day_before_yesterday, all_days_set):
    """
    找出满足条件的候选股：
    1. 昨日涨停（首板）
    2. 前日未涨停（确认是首板）
    3. 今日高开 >= GAP_UP_THRESHOLD
    4. 非ST
    5. 非一字涨停
    """
    # 获取昨日涨停的股票
    cur.execute("""
        SELECT code, code_name, open, high, low, close, preclose, isST,
               hour1_open, hour1_high, hour1_low, hour1_close,
               hour2_open, hour2_high, hour2_low, hour2_close,
               hour3_open, hour3_high, hour3_low, hour3_close,
               hour4_open, hour4_high, hour4_low, hour4_close
        FROM stock_kline WHERE date = ?
    """, (today,))
    today_rows = {r[0]: r for r in cur.fetchall()}

    cur.execute("""
        SELECT code, close, preclose FROM stock_kline
        WHERE date = ? AND isST = 0 AND preclose > 0
    """, (yesterday,))
    yesterday_rows = cur.fetchall()

    # 获取前日数据（用于确认首板）
    cur.execute("""
        SELECT code, close, preclose FROM stock_kline
        WHERE date = ? AND preclose > 0
    """, (day_before_yesterday,))
    dby_map = {r[0]: (r[1], r[2]) for r in cur.fetchall()}

    candidates = []
    for code, yd_close, yd_preclose in yesterday_rows:
        # 昨日涨停判定
        if not is_limit_up(yd_close, yd_preclose, code):
            continue

        # 前日不能涨停（确认首板，非连板）
        if code in dby_map:
            dby_close, dby_preclose = dby_map[code]
            if dby_preclose and dby_preclose > 0:
                if is_limit_up(dby_close, dby_preclose, code):
                    continue  # 连板，跳过
        else:
            continue  # 无前日数据，无法确认首板，跳过

        # 今日数据
        if code not in today_rows:
            continue
        t = today_rows[code]
        code_name = t[1]
        t_open, t_high, t_low, t_close, t_preclose = t[2], t[3], t[4], t[5], t[6]
        t_isST = t[7]

        # 排除ST
        if t_isST:
            continue
        # 双重过滤：名称中含ST的也排除（防止isST字段漏标）
        if code_name and 'ST' in code_name.upper():
            continue

        # 必须有有效的today数据
        if t_preclose is None or t_preclose <= 0 or t_open is None or t_open <= 0:
            continue

        # 计算高开幅度
        open_rate = (t_open - t_preclose) / t_preclose * 100

        # 高开阈值过滤
        if open_rate < GAP_UP_THRESHOLD:
            continue

        # 排除一字涨停
        if is_yizi_limit_up(t_open, t_high, t_low, t_close, t_preclose, code):
            continue

        candidates.append({
            'code': code,
            'code_name': code_name,
            'today': today,
            'yd_close': yd_close,
            'yd_preclose': yd_preclose,
            'yd_pct': (yd_close - yd_preclose) / yd_preclose * 100,
            't_open': t_open,
            't_high': t_high,
            't_low': t_low,
            't_close': t_close,
            't_preclose': t_preclose,
            'open_rate': open_rate,
            'hour_data_today': {
                'h1': (t[8], t[9], t[10], t[11]),
                'h2': (t[12], t[13], t[14], t[15]),
                'h3': (t[16], t[17], t[18], t[19]),
                'h4': (t[20], t[21], t[22], t[23]),
            }
        })

    # 按高开幅度排序（从高到低）
    candidates.sort(key=lambda x: x['open_rate'], reverse=True)
    return candidates


def get_hour_data_for_days(cur, code, days_list):
    """获取指定日期列表的hour级数据"""
    if not days_list:
        return []
    placeholders = ','.join(['?'] * len(days_list))
    cur.execute(f"""
        SELECT date, open, close, preclose,
               hour1_open, hour1_high, hour1_low, hour1_close,
               hour2_open, hour2_high, hour2_low, hour2_close,
               hour3_open, hour3_high, hour3_low, hour3_close,
               hour4_open, hour4_high, hour4_low, hour4_close
        FROM stock_kline WHERE code = ? AND date IN ({placeholders})
        ORDER BY date
    """, [code] + days_list)
    return cur.fetchall()


def format_hour_table(hour_rows, buy_date):
    """格式化hour级明细表格"""
    lines = []
    lines.append(f"  {'日期':<12}| {'hour':<5}| {'open':<8}| {'high':<8}| {'low':<8}| {'close':<8}| 涨跌幅")
    lines.append(f"  {'-'*12}+{'-'*6}+{'-'*9}+{'-'*9}+{'-'*9}+{'-'*9}+{'-'*8}")

    for row in hour_rows:
        date = row[0]
        day_open, day_close, day_preclose = row[1], row[2], row[3]
        hours = [
            ('h1', row[4], row[5], row[6], row[7]),
            ('h2', row[8], row[9], row[10], row[11]),
            ('h3', row[12], row[13], row[14], row[15]),
            ('h4', row[16], row[17], row[18], row[19]),
        ]
        prev_close = day_preclose  # h1的参照是preclose
        for h_name, h_open, h_high, h_low, h_close in hours:
            if h_open is None or h_close is None:
                continue
            # hour涨跌幅：相对于该hour的open
            pct = (h_close - h_open) / h_open * 100 if h_open > 0 else 0
            marker = "  ← 买入日" if date == buy_date and h_name == 'h1' else ""
            lines.append(
                f"  {date:<12}| {h_name:<5}| {h_open:<8.2f}| {h_high:<8.2f}| "
                f"{h_low:<8.2f}| {h_close:<8.2f}| {pct:+.2f}%{marker}"
            )
    return '\n'.join(lines)


def calc_key_metrics(cur, code, buy_date, buy_price, all_days_sorted):
    """计算关键指标：当日最高、当日收盘、次日最高、5日内最高最低"""
    if buy_price is None or buy_price <= 0:
        return None
    try:
        idx = all_days_sorted.index(buy_date)
    except ValueError:
        return None

    # 获取买入日+后5日的数据
    future_days = all_days_sorted[idx:idx+6]  # 含买入日共6天
    if len(future_days) < 2:
        return None

    placeholders = ','.join(['?'] * len(future_days))
    cur.execute(f"""
        SELECT date, open, high, low, close,
               hour1_open, hour1_high, hour1_low, hour1_close,
               hour2_open, hour2_high, hour2_low, hour2_close,
               hour3_high, hour4_high,
               hour3_low, hour4_low
        FROM stock_kline WHERE code = ? AND date IN ({placeholders})
        ORDER BY date
    """, [code] + future_days)
    rows = cur.fetchall()
    if not rows:
        return None

    metrics = {}
    # 当日数据
    today_row = rows[0] if rows[0][0] == buy_date else None
    if today_row:
        metrics['day_high'] = today_row[2]
        metrics['day_close'] = today_row[4]
        metrics['day_high_pct'] = (today_row[2] - buy_price) / buy_price * 100 if buy_price > 0 else 0
        metrics['day_close_pct'] = (today_row[4] - buy_price) / buy_price * 100 if buy_price > 0 else 0

    # 次日数据
    if len(rows) >= 2:
        next_row = rows[1]
        metrics['next_day_high'] = next_row[2]
        metrics['next_day_high_pct'] = (next_row[2] - buy_price) / buy_price * 100 if buy_price > 0 else 0

    # 5日内最高最低（含买入日）
    all_highs = [r[2] for r in rows if r[2] is not None]
    all_lows = [r[3] for r in rows if r[3] is not None]
    if all_highs:
        metrics['max_5d'] = max(all_highs)
        metrics['max_5d_pct'] = (max(all_highs) - buy_price) / buy_price * 100 if buy_price > 0 else 0
    if all_lows:
        metrics['min_5d'] = min(all_lows)
        metrics['min_5d_pct'] = (min(all_lows) - buy_price) / buy_price * 100 if buy_price > 0 else 0

    return metrics


def calc_h2_metrics(cur, code, buy_date, all_days_sorted, hour_data_today):
    """
    计算Hour2买入的指标。
    Hour2买入条件：hour1收阳(h1_close >= h1_open) 且 hour1不破开盘价(h1_low >= today_open)
    买入价 = hour2_open
    收益计算：从hour2_open开始，当日剩余(h2~h4 close) + 后续5日
    """
    h1 = hour_data_today.get('h1', (None, None, None, None))
    h2 = hour_data_today.get('h2', (None, None, None, None))
    h1_open, h1_high, h1_low, h1_close = h1
    h2_open, h2_high, h2_low, h2_close = h2

    # 检查数据有效性
    if any(v is None or v <= 0 for v in [h1_open, h1_close, h1_low, h2_open]):
        return None

    # Hour1确认条件
    h1_is_positive = h1_close >= h1_open  # hour1收阳
    h1_not_break_open = h1_low >= h1_open * 0.995  # hour1不破开盘价(允许0.5%容差贴合实际)

    result = {
        'h1_is_positive': h1_is_positive,
        'h1_not_break_open': h1_not_break_open,
        'h1_open': h1_open,
        'h1_close': h1_close,
        'h1_low': h1_low,
        'h1_pct': (h1_close - h1_open) / h1_open * 100,
        'h2_open': h2_open,
        'confirmed': h1_is_positive,  # 主确认条件: hour1收阳
    }

    # 如果通过确认，计算hour2买入后的收益
    if h1_is_positive and h2_open > 0:
        buy_price_h2 = h2_open
        # 当日收盘收益 = (day_close - h2_open) / h2_open
        # 但T+1不能当日卖出，所以关注的是次日+后续
        # 当日close用来判断趋势
        metrics_h2 = calc_key_metrics(cur, code, buy_date, buy_price_h2, all_days_sorted)
        if metrics_h2:
            result['metrics'] = metrics_h2
            result['buy_price'] = buy_price_h2

    return result


def main():
    if len(sys.argv) < 2:
        print("用法: python research_firstboard_gapup.py 2025-06")
        print("参数: 月份(YYYY-MM格式)")
        sys.exit(1)

    month_str = sys.argv[1]
    if len(month_str) != 7 or month_str[4] != '-':
        print(f"错误: 月份格式应为YYYY-MM，收到: {month_str}")
        sys.exit(1)

    print(f"{'='*80}")
    print(f"首板次日高开买入 - 候选股研究")
    print(f"研究月份: {month_str}")
    print(f"高开阈值: >= {GAP_UP_THRESHOLD}%")
    print(f"每天最多展示: {MAX_CANDIDATES_PER_DAY} 个候选股明细")
    print(f"{'='*80}\n")

    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()

    # 获取该月交易日
    month_days = get_trading_days(cur, month_str)
    if not month_days:
        print(f"错误: 未找到 {month_str} 的交易日数据")
        conn.close()
        sys.exit(1)

    # 获取全部交易日（用于定位前后5日）
    all_days = get_all_trading_days(cur)
    all_days_set = set(all_days)

    print(f"该月交易日数: {len(month_days)}")
    print(f"数据库总交易日数: {len(all_days)}\n")

    # 统计汇总
    total_candidates = 0
    daily_stats = []

    # Hour1 vs Hour2 对比统计
    h1_stats = []  # (day_close_pct, max_5d_pct, is_positive_close)
    h2_stats = []  # same structure, only for h1-confirmed candidates
    h2_confirm_count = 0  # hour1收阳的候选股数
    h2_confirm_not_break = 0  # hour1不破开的候选股数

    for today in month_days:
        # 找到yesterday和day_before_yesterday
        try:
            idx = all_days.index(today)
        except ValueError:
            continue
        if idx < 2:
            continue
        yesterday = all_days[idx - 1]
        day_before_yesterday = all_days[idx - 2]

        candidates = find_candidates(cur, today, yesterday, day_before_yesterday, all_days_set)
        total_candidates += len(candidates)

        print(f"\n{'='*60}")
        print(f"{'='*10} {today} {'='*10}")
        print(f"{'='*60}")
        print(f"候选股数量: {len(candidates)}")

        if not candidates:
            daily_stats.append((today, 0, None))
            continue

        # 打印摘要
        print(f"\n  {'排名':<4} {'代码':<12} {'名称':<10} {'高开%':<8} {'昨涨幅%':<8}")
        print(f"  {'-'*55}")
        for i, c in enumerate(candidates):
            print(f"  {i+1:<4} {c['code']:<12} {c['code_name'] or '':<10} "
                  f"{c['open_rate']:<8.2f} {c['yd_pct']:<8.2f}")

        # 每天最多展示MAX_CANDIDATES_PER_DAY个明细
        shown = min(MAX_CANDIDATES_PER_DAY, len(candidates))
        for i in range(shown):
            c = candidates[i]
            print(f"\n--- {c['code']} ({c['code_name'] or 'N/A'}) ---")
            print(f"  昨日涨停: close={c['yd_close']:.2f} preclose={c['yd_preclose']:.2f} "
                  f"涨幅{c['yd_pct']:+.2f}%")
            print(f"  今日高开: open={c['t_open']:.2f} preclose={c['t_preclose']:.2f} "
                  f"高开{c['open_rate']:+.2f}%")

            # 获取前5日+后5日的hour级数据
            try:
                today_idx = all_days.index(today)
            except ValueError:
                continue
            prev5_start = max(0, today_idx - 5)
            next5_end = min(len(all_days), today_idx + 6)
            window_days = all_days[prev5_start:next5_end]

            hour_rows = get_hour_data_for_days(cur, c['code'], window_days)
            if hour_rows:
                print(f"\n  前5日+后5日 hour级明细:")
                print(format_hour_table(hour_rows, today))

            # 关键指标
            buy_price = c['t_open']
            metrics = calc_key_metrics(cur, c['code'], today, buy_price, all_days)
            if metrics:
                print(f"\n  关键指标 (Hour1买入价={buy_price:.2f}):")
                print(f"    买入价(h1 open): {buy_price:.2f}")
                if 'day_high' in metrics:
                    print(f"    当日最高: {metrics['day_high']:.2f} ({metrics['day_high_pct']:+.2f}% vs 买入价)")
                if 'day_close' in metrics:
                    print(f"    当日收盘: {metrics['day_close']:.2f} ({metrics['day_close_pct']:+.2f}%)")
                if 'next_day_high' in metrics:
                    print(f"    次日最高: {metrics['next_day_high']:.2f} ({metrics['next_day_high_pct']:+.2f}%)")
                if 'max_5d' in metrics:
                    print(f"    5日内最高: {metrics['max_5d']:.2f} ({metrics['max_5d_pct']:+.2f}%)")
                if 'min_5d' in metrics:
                    print(f"    5日内最低: {metrics['min_5d']:.2f} ({metrics['min_5d_pct']:+.2f}%)")

            # Hour2确认买入分析
            h2_result = calc_h2_metrics(cur, c['code'], today, all_days, c['hour_data_today'])
            if h2_result:
                h1_status = "✔ 收阳" if h2_result['h1_is_positive'] else "✘ 收阴"
                h1_break = "✔ 不破开" if h2_result['h1_not_break_open'] else "✘ 破开"
                print(f"\n  Hour2确认分析:")
                print(f"    Hour1表现: {h1_status} (h1涨跌:{h2_result['h1_pct']:+.2f}%) | {h1_break}")
                if h2_result['confirmed'] and 'metrics' in h2_result:
                    m2 = h2_result['metrics']
                    print(f"    → Hour2买入价: {h2_result['buy_price']:.2f}")
                    if 'day_close_pct' in m2:
                        print(f"    → 当日收盘vs H2买入: {m2['day_close_pct']:+.2f}%")
                    if 'max_5d_pct' in m2:
                        print(f"    → 5日最大涨幅vs H2买入: {m2['max_5d_pct']:+.2f}%")
                else:
                    print(f"    → 未通过确认，不买入")
        # 统计该天所有候选股的平均收益 + Hour1/Hour2对比数据
        day_returns = []
        for c in candidates:
            buy_price = c['t_open']
            if not (buy_price and buy_price > 0 and c['t_close'] and c['t_close'] > 0):
                continue
            day_ret = (c['t_close'] - buy_price) / buy_price * 100
            day_returns.append(day_ret)

            # Hour1 指标
            m1 = calc_key_metrics(cur, c['code'], today, buy_price, all_days)
            if m1:
                is_pos = m1.get('day_close_pct', 0) > 0
                h1_stats.append((
                    m1.get('day_close_pct', 0),
                    m1.get('max_5d_pct', 0),
                    is_pos
                ))

            # Hour2 指标
            h2_result = calc_h2_metrics(cur, c['code'], today, all_days, c['hour_data_today'])
            if h2_result:
                if h2_result['h1_is_positive']:
                    h2_confirm_count += 1
                if h2_result['h1_not_break_open']:
                    h2_confirm_not_break += 1
                if h2_result['confirmed'] and 'metrics' in h2_result:
                    m2 = h2_result['metrics']
                    is_pos2 = m2.get('day_close_pct', 0) > 0
                    h2_stats.append((
                        m2.get('day_close_pct', 0),
                        m2.get('max_5d_pct', 0),
                        is_pos2
                    ))

        avg_ret = sum(day_returns) / len(day_returns) if day_returns else 0
        daily_stats.append((today, len(candidates), avg_ret))

    # 月度汇总
    print(f"\n\n{'='*80}")
    print(f"月度汇总统计")
    print(f"{'='*80}")
    print(f"总候选股数: {total_candidates}")
    print(f"日均候选股: {total_candidates / len(month_days):.1f}")
    print(f"\n{'日期':<12} {'候选数':<8} {'当日平均收益%':<15}")
    print(f"{'-'*40}")
    all_returns = []
    win_count = 0
    for date, count, avg_ret in daily_stats:
        if avg_ret is not None:
            print(f"{date:<12} {count:<8} {avg_ret:+.2f}%")
            if count > 0:
                all_returns.append(avg_ret)
                if avg_ret > 0:
                    win_count += 1
        else:
            print(f"{date:<12} {count:<8} N/A")

    if all_returns:
        overall_avg = sum(all_returns) / len(all_returns)
        win_rate = win_count / len(all_returns) * 100
        print(f"\n{'='*40}")
        print(f"有候选股的交易日数: {len(all_returns)}")
        print(f"整月平均日收益: {overall_avg:+.2f}%")
        print(f"胜率(日维度): {win_rate:.1f}% ({win_count}/{len(all_returns)})")
        print(f"月化收益估算: {overall_avg * len(all_returns):+.2f}%")

    # ========== Hour1 vs Hour2 买入时机对比 ==========
    print(f"\n\n{'='*80}")
    print(f"买入时机对比分析")
    print(f"{'='*80}")

    print(f"\n  Hour1直接买入(today open价):")
    if h1_stats:
        h1_day_rets = [s[0] for s in h1_stats]
        h1_max5d = [s[1] for s in h1_stats]
        h1_pos_count = sum(1 for s in h1_stats if s[2])
        print(f"    - 样本数: {len(h1_stats)}")
        print(f"    - 当日收益均值: {sum(h1_day_rets)/len(h1_day_rets):+.2f}%")
        print(f"    - 当日收益中位数: {sorted(h1_day_rets)[len(h1_day_rets)//2]:+.2f}%")
        print(f"    - 5日最大涨幅均值: {sum(h1_max5d)/len(h1_max5d):+.2f}%")
        print(f"    - 收阳占比(当日): {h1_pos_count/len(h1_stats)*100:.1f}% ({h1_pos_count}/{len(h1_stats)})")
    else:
        print(f"    - 无有效数据")

    print(f"\n  Hour2确认买入(hour1收阳后以hour2 open买入):")
    if h2_stats:
        h2_day_rets = [s[0] for s in h2_stats]
        h2_max5d = [s[1] for s in h2_stats]
        h2_pos_count = sum(1 for s in h2_stats if s[2])
        print(f"    - 候选股中hour1收阳占比: {h2_confirm_count}/{total_candidates} "
              f"({h2_confirm_count/total_candidates*100:.1f}%)" if total_candidates > 0 else "")
        print(f"    - 候选股中hour1不破开占比: {h2_confirm_not_break}/{total_candidates} "
              f"({h2_confirm_not_break/total_candidates*100:.1f}%)" if total_candidates > 0 else "")
        print(f"    - 通过确认的样本数: {len(h2_stats)}")
        print(f"    - 当日收益均值: {sum(h2_day_rets)/len(h2_day_rets):+.2f}%")
        print(f"    - 当日收益中位数: {sorted(h2_day_rets)[len(h2_day_rets)//2]:+.2f}%")
        print(f"    - 5日最大涨幅均值: {sum(h2_max5d)/len(h2_max5d):+.2f}%")
        print(f"    - 收阳占比(当日): {h2_pos_count/len(h2_stats)*100:.1f}% ({h2_pos_count}/{len(h2_stats)})")
    else:
        print(f"    - 无满足hour1收阳确认条件的数据")

    # 对比结论
    if h1_stats and h2_stats:
        h1_avg = sum(s[0] for s in h1_stats) / len(h1_stats)
        h2_avg = sum(s[0] for s in h2_stats) / len(h2_stats)
        h1_wr = sum(1 for s in h1_stats if s[2]) / len(h1_stats) * 100
        h2_wr = sum(1 for s in h2_stats if s[2]) / len(h2_stats) * 100
        print(f"\n  对比结论:")
        print(f"    - 收益差异: Hour2({h2_avg:+.2f}%) vs Hour1({h1_avg:+.2f}%) = {h2_avg-h1_avg:+.2f}%")
        print(f"    - 胜率差异: Hour2({h2_wr:.1f}%) vs Hour1({h1_wr:.1f}%) = {h2_wr-h1_wr:+.1f}%")
        print(f"    - Hour2过滤掉: {len(h1_stats)-len(h2_stats)}个候选 "
              f"({(len(h1_stats)-len(h2_stats))/len(h1_stats)*100:.1f}%)")

    conn.close()
    print(f"\n{'='*80}")
    print("研究完成。")


if __name__ == '__main__':
    main()
