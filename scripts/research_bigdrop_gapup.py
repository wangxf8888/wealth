#!/usr/bin/env python3
"""
大阴次日高开买入 - 候选股研究脚本
策略概念：
  - 昨日大阴（close_rate <= -5%）
  - 今日竞价高开 >= 2%
  - 买入价 = today open (hour1 open)
T+0合规：昨日大阴收盘后已确定，今日高开9:25竞价确定，买入价=today open
"""
import sys
import sqlite3
import os
from datetime import datetime

# ============ 配置区 ============
DB_PATH = "/home/AIWealth/data/stocks.db"
BIG_DROP_THRESHOLD = -5.0      # 昨日跌幅阈值(%)
GAP_UP_THRESHOLD = 2.0         # 今日高开阈值(%)
CONTEXT_DAYS = 5               # 前后查看天数
# Hour2确认条件：hour1不破开盘价(low>=open) 或 hour1收阳(close>=open)
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


def is_yizi_limit_up(row):
    """判断是否一字涨停板：open==high==low==close且>=涨停价"""
    o, h, l, c, preclose, code = row['open'], row['high'], row['low'], row['close'], row['preclose'], row['code']
    if o == h == l == c and preclose > 0:
        limit_price = get_limit_up_price(code, preclose)
        if c >= limit_price:
            return True
    return False


def is_st(row):
    """判断是否ST股"""
    if row['isST'] == 1:
        return True
    name = row['code_name'] or ''
    if 'ST' in name.upper():
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


def find_candidates(cursor, today, yesterday):
    """找出当日满足条件的候选股"""
    # 找昨日大阴 + 今日高开的股票
    query = """
        SELECT
            t.date as today_date,
            t.code, t.code_name,
            t.open, t.high, t.low, t.close, t.preclose,
            t.close_rate as today_close_rate,
            t.volume, t.turn, t.isST,
            t.hour1_open, t.hour1_high, t.hour1_low, t.hour1_close,
            t.hour2_open, t.hour2_high, t.hour2_low, t.hour2_close,
            t.hour3_open, t.hour3_high, t.hour3_low, t.hour3_close,
            t.hour4_open, t.hour4_high, t.hour4_low, t.hour4_close,
            y.date as yest_date,
            y.open as y_open, y.high as y_high, y.low as y_low, y.close as y_close,
            y.preclose as y_preclose, y.close_rate as y_close_rate
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
        # 排除ST
        if is_st(d):
            continue
        # 排除一字涨停
        if is_yizi_limit_up(d):
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


def format_pct(val):
    """格式化百分比"""
    if val is None:
        return "N/A"
    return f"{val:+.2f}%"


def check_hour2_confirm(cand):
    """检查Hour2确认条件：hour1不破开盘价 或 hour1收阳"""
    h1_open = cand.get('hour1_open')
    h1_low = cand.get('hour1_low')
    h1_close = cand.get('hour1_close')
    day_open = cand['open']
    if h1_open is None or h1_open == 0 or h1_low is None or h1_close is None:
        return False, "h1数据缺失"
    # 条件A：hour1不破开盘价
    cond_a = h1_low >= day_open
    # 条件B：hour1收阳
    cond_b = h1_close >= h1_open
    passed = cond_a or cond_b
    reason = []
    if cond_a:
        reason.append(f"h1未破开盘(low={h1_low:.2f}>=open={day_open:.2f})")
    if cond_b:
        reason.append(f"h1收阳(close={h1_close:.2f}>=open={h1_open:.2f})")
    if not passed:
        reason = [f"h1破开盘(low={h1_low:.2f}<open={day_open:.2f})且h1收阴(close={h1_close:.2f}<open={h1_open:.2f})"]
    return passed, "; ".join(reason)


def print_candidate(cand, context_rows, today, all_days, today_idx):
    """打印单个候选股的详细信息"""
    code = cand['code']
    name = cand['code_name'] or ''
    buy_price = cand['open']  # h1 open = day open
    gap_up_pct = (cand['open'] - cand['preclose']) / cand['preclose'] * 100

    # Hour2确认
    h2_confirm, h2_reason = check_hour2_confirm(cand)
    h2_buy_price = cand.get('hour2_open')

    print(f"\n--- {code} ({name}) ---")
    print(f"  昨日大阴: close_rate={cand['y_close_rate']:.1f}% "
          f"(open={cand['y_open']:.2f} high={cand['y_high']:.2f} "
          f"low={cand['y_low']:.2f} close={cand['y_close']:.2f})")
    print(f"  今日高开: open={cand['open']:.2f} preclose={cand['preclose']:.2f} "
          f"高开{gap_up_pct:+.2f}%")
    print(f"  Hour2确认: {'✓ 通过' if h2_confirm else '✗ 未通过'} - {h2_reason}")
    if h2_buy_price and h2_buy_price > 0:
        print(f"  Hour2买入价: {h2_buy_price:.2f} ({(h2_buy_price - buy_price) / buy_price * 100:+.2f}% vs h1 open)")
    print()
    print(f"  前{CONTEXT_DAYS}日+后{CONTEXT_DAYS}日 hour级明细:")
    print(f"  {'日期':<12}| {'hour':<5}| {'open':<8}| {'high':<8}| {'low':<8}| {'close':<8}| vs买入价")
    print(f"  {'-'*12}+{'-'*6}+{'-'*9}+{'-'*9}+{'-'*9}+{'-'*9}+{'-'*10}")

    for row in context_rows:
        d = row['date']
        is_buy_day = (d == today)
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
    print(f"  关键指标(Hour1 open买入):")
    print(f"    买入价(h1 open): {buy_price:.2f}")

    # 当日指标
    today_high = cand['high']
    today_close = cand['close']
    print(f"    当日最高: {today_high:.2f} ({(today_high - buy_price) / buy_price * 100:+.2f}% vs 买入价)")
    print(f"    当日收盘: {today_close:.2f} ({(today_close - buy_price) / buy_price * 100:+.2f}%)")

    # Hour2买入关键指标
    if h2_buy_price and h2_buy_price > 0 and h2_confirm:
        h2_high_remaining = max(filter(lambda x: x and x > 0, [
            cand.get('hour2_high', 0), cand.get('hour3_high', 0), cand.get('hour4_high', 0)
        ]), default=0)
        print(f"  关键指标(Hour2 open买入, 确认后):")
        print(f"    买入价(h2 open): {h2_buy_price:.2f}")
        print(f"    当日最高(h2起): {h2_high_remaining:.2f} ({(h2_high_remaining - h2_buy_price) / h2_buy_price * 100:+.2f}%)")
        print(f"    当日收盘: {today_close:.2f} ({(today_close - h2_buy_price) / h2_buy_price * 100:+.2f}%)")

    # 后续天指标
    future_rows = [r for r in context_rows if r['date'] > today]
    if future_rows:
        # 次日最高
        next_day = future_rows[0]
        next_high = next_day['high']
        print(f"    次日最高: {next_high:.2f} ({(next_high - buy_price) / buy_price * 100:+.2f}%)")

        # 5日内最高/最低
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


def main():
    if len(sys.argv) < 2:
        print("用法: python research_bigdrop_gapup.py <月份>")
        print("示例: python research_bigdrop_gapup.py 2025-06")
        sys.exit(1)

    month_str = sys.argv[1]
    print(f"=" * 60)
    print(f"大阴次日高开买入 - 候选股研究")
    print(f"参数: 月份={month_str}, 大阴阈值={BIG_DROP_THRESHOLD}%, 高开阈值={GAP_UP_THRESHOLD}%")
    print(f"=" * 60)

    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()

    # 获取所有交易日和该月交易日
    all_days = get_all_trading_days(cursor)
    month_days = get_trading_days(cursor, month_str)

    if not month_days:
        print(f"错误: 未找到 {month_str} 的交易日数据")
        conn.close()
        sys.exit(1)

    print(f"该月交易日数: {len(month_days)}")
    print()

    # 统计用
    all_candidates = []

    for i, today in enumerate(month_days):
        # 找昨日：在all_days中today的前一个交易日
        today_idx_all = all_days.index(today) if today in all_days else -1
        if today_idx_all <= 0:
            continue
        yesterday = all_days[today_idx_all - 1]

        candidates = find_candidates(cursor, today, yesterday)

        print(f"\n{'=' * 10} {today} {'=' * 10}")
        print(f"候选股数量: {len(candidates)}")

        for cand in candidates:
            context_rows = get_context_hours(cursor, cand['code'], all_days, today_idx_all, CONTEXT_DAYS)
            print_candidate(cand, context_rows, today, all_days, today_idx_all)

            # 收集统计数据
            buy_price = cand['open']
            if buy_price > 0:
                today_ret = (cand['close'] - buy_price) / buy_price * 100
                today_max = (cand['high'] - buy_price) / buy_price * 100
                today_is_positive = 1 if cand['close'] > buy_price else 0

                # Hour2确认及统计
                h2_confirm, _ = check_hour2_confirm(cand)
                h2_buy_price = cand.get('hour2_open')
                h2_today_ret = None
                h2_today_max = None
                h2_today_positive = None
                h2_future_max = None
                h2_future_min = None
                if h2_confirm and h2_buy_price and h2_buy_price > 0:
                    h2_today_ret = (cand['close'] - h2_buy_price) / h2_buy_price * 100
                    # h2起的日内最高=max(h2_high, h3_high, h4_high)
                    h2_highs = [v for v in [cand.get('hour2_high'), cand.get('hour3_high'), cand.get('hour4_high')] if v and v > 0]
                    h2_today_max = (max(h2_highs) - h2_buy_price) / h2_buy_price * 100 if h2_highs else 0
                    h2_today_positive = 1 if cand['close'] > h2_buy_price else 0

                # 后5日最高最低
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

                # Hour2的后5日统计（用h2_buy_price计算）
                if h2_confirm and h2_buy_price and h2_buy_price > 0 and future_rows:
                    highs2 = [r['high'] for r in future_rows if r['high'] and r['high'] > 0]
                    lows2 = [r['low'] for r in future_rows if r['low'] and r['low'] > 0]
                    if highs2:
                        h2_future_max = (max(highs2) - h2_buy_price) / h2_buy_price * 100
                    if lows2:
                        h2_future_min = (min(lows2) - h2_buy_price) / h2_buy_price * 100

                all_candidates.append({
                    'code': cand['code'],
                    'date': today,
                    'buy_price': buy_price,
                    'today_ret': today_ret,
                    'today_max': today_max,
                    'today_positive': today_is_positive,
                    'future_max': future_max,
                    'future_min': future_min,
                    'h2_confirm': h2_confirm,
                    'h2_buy_price': h2_buy_price,
                    'h2_today_ret': h2_today_ret,
                    'h2_today_max': h2_today_max,
                    'h2_today_positive': h2_today_positive,
                    'h2_future_max': h2_future_max,
                    'h2_future_min': h2_future_min,
                })

    # 月度统计汇总
    print(f"\n\n{'=' * 10} 月度统计汇总 {'=' * 10}")
    print(f"总候选股数: {len(all_candidates)}")

    if all_candidates:
        # === Hour1 统计 ===
        avg_today_ret = sum(c['today_ret'] for c in all_candidates) / len(all_candidates)
        avg_today_max = sum(c['today_max'] for c in all_candidates) / len(all_candidates)

        future_max_list = [c['future_max'] for c in all_candidates if c['future_max'] is not None]
        future_min_list = [c['future_min'] for c in all_candidates if c['future_min'] is not None]

        avg_future_max = sum(future_max_list) / len(future_max_list) if future_max_list else 0
        avg_future_min = sum(future_min_list) / len(future_min_list) if future_min_list else 0

        positive_count = sum(c['today_positive'] for c in all_candidates)
        positive_ratio = positive_count / len(all_candidates) * 100

        print(f"\n【方案A】Hour1 open直接买入 (仅基于'昨日大阴+今日高开'):")
        print(f"  样本数: {len(all_candidates)}")
        print(f"  - 当日收益(close/open-1)均值: {avg_today_ret:+.2f}%")
        print(f"  - 当日最大涨幅(high/open-1)均值: {avg_today_max:+.2f}%")
        print(f"  - 5日内最大涨幅均值: {avg_future_max:+.2f}%")
        print(f"  - 5日内最大跌幅均值: {avg_future_min:+.2f}%")
        print(f"  - 当日收阳占比: {positive_ratio:.1f}%")

        # === Hour2 统计 ===
        h2_candidates = [c for c in all_candidates if c['h2_confirm'] and c['h2_today_ret'] is not None]
        h2_rejected = len(all_candidates) - len(h2_candidates)

        print(f"\n【方案B】Hour2 open确认后买入 (加条件: hour1不破开盘价 或 hour1收阳):")
        print(f"  样本数: {len(h2_candidates)} (过滤掉{h2_rejected}个未通过确认)")
        if h2_candidates:
            h2_avg_ret = sum(c['h2_today_ret'] for c in h2_candidates) / len(h2_candidates)
            h2_avg_max = sum(c['h2_today_max'] for c in h2_candidates) / len(h2_candidates)

            h2_fmax_list = [c['h2_future_max'] for c in h2_candidates if c['h2_future_max'] is not None]
            h2_fmin_list = [c['h2_future_min'] for c in h2_candidates if c['h2_future_min'] is not None]
            h2_avg_fmax = sum(h2_fmax_list) / len(h2_fmax_list) if h2_fmax_list else 0
            h2_avg_fmin = sum(h2_fmin_list) / len(h2_fmin_list) if h2_fmin_list else 0

            h2_positive = sum(c['h2_today_positive'] for c in h2_candidates)
            h2_pos_ratio = h2_positive / len(h2_candidates) * 100

            print(f"  - 当日收益(close/h2open-1)均值: {h2_avg_ret:+.2f}%")
            print(f"  - 当日最大涨幅(h2起high/h2open-1)均值: {h2_avg_max:+.2f}%")
            print(f"  - 5日内最大涨幅均值: {h2_avg_fmax:+.2f}%")
            print(f"  - 5日内最大跌幅均值: {h2_avg_fmin:+.2f}%")
            print(f"  - 当日收阳占比: {h2_pos_ratio:.1f}%")
        else:
            print(f"  无通过确认的候选股")

        # === 对比 ===
        print(f"\n【对比结论】")
        if h2_candidates:
            delta_ret = h2_avg_ret - avg_today_ret
            delta_pos = h2_pos_ratio - positive_ratio
            print(f"  Hour2确认过滤率: {h2_rejected}/{len(all_candidates)} = {h2_rejected/len(all_candidates)*100:.1f}%")
            print(f"  当日收益提升: {delta_ret:+.2f}% (H2 vs H1)")
            print(f"  胜率提升: {delta_pos:+.1f}% (H2 vs H1)")
            if h2_avg_ret > avg_today_ret and h2_pos_ratio > positive_ratio:
                print(f"  → Hour2确认有效：收益和胜率均提升")
            elif h2_avg_ret > avg_today_ret:
                print(f"  → Hour2确认部分有效：收益提升但胜率变化不明显")
            else:
                print(f"  → Hour2确认效果待观察")
    else:
        print("无候选股数据")

    conn.close()
    print(f"\n完成。")


if __name__ == "__main__":
    main()
