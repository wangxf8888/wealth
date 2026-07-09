#!/usr/bin/env python3
"""
N字形态 / V字形态 + 今日高开 - 候选股研究脚本

策略概念：
  N字形态：涨→跌→涨，形成N形。
    - 第一波上涨≥8%（在近10日内）
    - 回调≥5%（2-5日）
    - 第二波启动：yesterday close > 回调低点L
    - today open > yesterday close（高开确认）

  V字形态：急跌→急反弹。
    - 连续下跌≥10%（近7日内）
    - 从最低点反弹≥3%（yesterday close vs 最低点）
    - today open > yesterday close（高开确认V底右侧）

T+0合规：形态通过yesterday及之前日K确认，today只用open确认方向。
"""
import sys
import sqlite3
import os
from datetime import datetime

# ============ 配置区 ============
DB_PATH = "/home/AIWealth/data/stocks.db"
# N字形态参数
N_FIRST_WAVE_MIN = 8.0       # 第一波涨幅最低(%)
N_PULLBACK_MIN = 5.0         # 回调幅度最低(%)
N_PULLBACK_DAYS_MIN = 2      # 回调最少天数
N_PULLBACK_DAYS_MAX = 5      # 回调最多天数
N_LOOKBACK = 10              # 形态回看天数
N_REBOUND_MIN = 2.0          # 第二波启动：从回调低点反弹最低(%)
# V字形态参数
V_DROP_MIN = 10.0            # 累计跌幅最低(%)
V_LOOKBACK = 7               # 下跌段回看天数
V_REBOUND_MIN = 3.0          # 反弹幅度最低(%)
# 通用参数
GAP_UP_MIN = 2.0             # today高开最低(%)
TURN_MIN = 3.0               # 最低换手率(%) 过滤冷门股
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


def is_st(row):
    """判断是否ST股"""
    if row.get('isST') == 1:
        return True
    name = row.get('code_name') or ''
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


def get_stock_history(cursor, code, days_list):
    """获取某只股票在指定日期列表的日K数据"""
    if not days_list:
        return []
    placeholders = ','.join(['?'] * len(days_list))
    cursor.execute(f"""
        SELECT date, code, code_name, open, high, low, close, preclose,
               close_rate, volume, turn, isST
        FROM stock_kline
        WHERE code = ? AND date IN ({placeholders})
        ORDER BY date
    """, [code] + days_list)
    columns = [desc[0] for desc in cursor.description]
    return [dict(zip(columns, row)) for row in cursor.fetchall()]


def find_n_shape(history_rows):
    """
    在近N_LOOKBACK日K线中寻找N形态
    返回: (found, info_dict) 或 (False, None)
    history_rows: 按日期升序排列的日K线，最后一条是yesterday
    """
    if len(history_rows) < 5:
        return False, None

    # 取最近N_LOOKBACK条
    rows = history_rows[-N_LOOKBACK:] if len(history_rows) >= N_LOOKBACK else history_rows
    closes = [r['close'] for r in rows]
    lows = [r['low'] for r in rows]
    highs = [r['high'] for r in rows]
    n = len(rows)

    best_result = None

    # 遍历寻找：局部低点 -> 高点（第一波涨≥8%） -> 回调低点（跌≥5%，2-5日）
    for i in range(n - 3):  # i = 第一波起始低点
        low_start = lows[i]
        if low_start <= 0:
            continue

        # 寻找i之后的高点
        for j in range(i + 1, min(i + 6, n)):  # 第一波在3-5日内到达高点
            high_point = highs[j]
            if high_point <= 0:
                continue
            first_wave_pct = (high_point - low_start) / low_start * 100
            if first_wave_pct < N_FIRST_WAVE_MIN:
                continue

            # 从高点j开始寻找回调低点
            for k in range(j + N_PULLBACK_DAYS_MIN, min(j + N_PULLBACK_DAYS_MAX + 1, n)):
                pullback_low = lows[k]
                if pullback_low <= 0:
                    continue
                pullback_pct = (high_point - pullback_low) / high_point * 100
                if pullback_pct < N_PULLBACK_MIN:
                    continue

                # 确认第二波启动：yesterday close > 回调低点L，且反弹≥N_REBOUND_MIN
                yesterday_close = closes[-1]
                if yesterday_close <= pullback_low:
                    continue
                rebound_from_low = (yesterday_close - pullback_low) / pullback_low * 100
                if rebound_from_low < N_REBOUND_MIN:
                    continue

                # 回调低点必须是近期的（距yesterday不超过3天）
                if (n - 1) - k > 3:
                    continue
                pullback_days = k - j
                result = {
                    'low_start_idx': i,
                    'low_start_date': rows[i]['date'],
                    'low_start_price': low_start,
                    'high_idx': j,
                    'high_date': rows[j]['date'],
                    'high_price': high_point,
                    'first_wave_pct': first_wave_pct,
                    'pullback_low_idx': k,
                    'pullback_low_date': rows[k]['date'],
                    'pullback_low_price': pullback_low,
                    'pullback_pct': pullback_pct,
                    'pullback_days': pullback_days,
                    'yesterday_close': yesterday_close,
                    'rebound_from_low': rebound_from_low,
                }
                # 选最优：第一波涨幅最大的
                if best_result is None or result['first_wave_pct'] > best_result['first_wave_pct']:
                    best_result = result

    if best_result:
        return True, best_result
    return False, None


def find_v_shape(history_rows):
    """
    在近V_LOOKBACK日K线中寻找V形态
    返回: (found, info_dict) 或 (False, None)
    history_rows: 按日期升序排列的日K线，最后一条是yesterday
    """
    if len(history_rows) < 4:
        return False, None

    rows = history_rows[-V_LOOKBACK:] if len(history_rows) >= V_LOOKBACK else history_rows
    n = len(rows)

    best_result = None

    # 找连续下跌段：从某个起始高点到最低点，累跌≥10%
    for i in range(n - 2):
        start_price = rows[i]['close']  # 下跌起始价
        if start_price <= 0:
            continue

        # 向后寻找最低点
        min_low = start_price
        min_low_idx = i
        for j in range(i + 1, n):
            if rows[j]['low'] <= 0:
                continue
            if rows[j]['low'] < min_low:
                min_low = rows[j]['low']
                min_low_idx = j

        if min_low_idx == i:
            continue

        drop_pct = (start_price - min_low) / start_price * 100
        if drop_pct < V_DROP_MIN:
            continue

        # 确认已触底反弹：yesterday close vs 最低点 涨≥3%
        yesterday_close = rows[-1]['close']
        rebound_pct = (yesterday_close - min_low) / min_low * 100
        if rebound_pct < V_REBOUND_MIN:
            continue

        # 最低点应在yesterday之前（不能是yesterday本身，否则没有反弹）
        if min_low_idx >= n - 1:
            continue

        drop_days = min_low_idx - i
        result = {
            'drop_start_idx': i,
            'drop_start_date': rows[i]['date'],
            'drop_start_price': start_price,
            'low_idx': min_low_idx,
            'low_date': rows[min_low_idx]['date'],
            'low_price': min_low,
            'drop_pct': drop_pct,
            'drop_days': drop_days,
            'yesterday_close': yesterday_close,
            'rebound_pct': rebound_pct,
        }
        if best_result is None or result['drop_pct'] > best_result['drop_pct']:
            best_result = result

    if best_result:
        return True, best_result
    return False, None


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


def print_candidate(shape_type, code, code_name, shape_info, today_row, context_rows, today, buy_price):
    """打印单个候选股的详细信息"""
    name = code_name or ''
    gap_up_pct = (today_row['open'] - today_row['preclose']) / today_row['preclose'] * 100

    print(f"\n--- [{shape_type}] {code} ({name}) ---")

    if shape_type == 'N字':
        info = shape_info
        print(f"  第一波: {info['low_start_date']}低点{info['low_start_price']:.2f}"
              f"→{info['high_date']}高点{info['high_price']:.2f} 涨{info['first_wave_pct']:+.1f}%")
        print(f"  回调: {info['high_date']}高点{info['high_price']:.2f}"
              f"→{info['pullback_low_date']}低点{info['pullback_low_price']:.2f} "
              f"回调-{info['pullback_pct']:.1f}%({info['pullback_days']}天)")
        print(f"  第二波启动: yesterday close={info['yesterday_close']:.2f}"
              f"(vs低点+{info['rebound_from_low']:.1f}%)")
    else:  # V字
        info = shape_info
        print(f"  急跌段: {info['drop_start_date']}~{info['low_date']} "
              f"从{info['drop_start_price']:.2f}跌至{info['low_price']:.2f} "
              f"累跌-{info['drop_pct']:.1f}%")
        print(f"  反弹确认: yesterday close={info['yesterday_close']:.2f} "
              f"(vs低点+{info['rebound_pct']:.1f}%)")

    print(f"  今日高开: open={today_row['open']:.2f} preclose={today_row['preclose']:.2f} "
          f"高开{gap_up_pct:+.2f}%")
    print()

    # Hour级明细
    print(f"  前{CONTEXT_DAYS}日+后{CONTEXT_DAYS}日 hour级明细:")
    print(f"  {'日期':<12}| {'hour':<5}| {'open':<8}| {'high':<8}| {'low':<8}| {'close':<8}| {'turn':<6}| vs买入价")
    print(f"  {'-'*12}+{'-'*6}+{'-'*9}+{'-'*9}+{'-'*9}+{'-'*9}+{'-'*7}+{'-'*10}")

    for row in context_rows:
        d = row['date']
        is_buy_day = (d == today)
        marker = " ← 买入日" if is_buy_day else ""

        # 日级行
        day_turn = row.get('turn') or 0
        day_close_rate = row.get('close_rate') or 0
        print(f"  {d:<12}| {'day':<5}| {row['open']:<8.2f}| {row['high']:<8.2f}| "
              f"{row['low']:<8.2f}| {row['close']:<8.2f}| {day_turn:<6.2f}| "
              f"{(row['close'] - buy_price) / buy_price * 100:+.2f}% "
              f"(日涨幅{day_close_rate:+.1f}%){marker}")

        for h in range(1, 5):
            ho = row.get(f'hour{h}_open')
            hh = row.get(f'hour{h}_high')
            hl = row.get(f'hour{h}_low')
            hc = row.get(f'hour{h}_close')
            if ho is None or ho == 0:
                continue
            vs_buy = (hc - buy_price) / buy_price * 100 if buy_price > 0 else 0
            print(f"  {'':12}| h{h:<4}| {ho:<8.2f}| {hh:<8.2f}| {hl:<8.2f}| {hc:<8.2f}| {'':6}| {vs_buy:+.2f}%")

    # 关键指标
    print()
    print(f"  关键指标:")
    print(f"    买入价(today open): {buy_price:.2f}")
    today_high = today_row['high']
    today_close = today_row['close']
    print(f"    当日最高: {today_high:.2f} ({(today_high - buy_price) / buy_price * 100:+.2f}%)")
    print(f"    当日收盘: {today_close:.2f} ({(today_close - buy_price) / buy_price * 100:+.2f}%)")

    # 后续天指标
    future_rows = [r for r in context_rows if r['date'] > today]
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


def find_candidates_for_day(cursor, today, all_days, today_idx):
    """找出当日满足N字或V字形态的候选股"""
    if today_idx < 1:
        return [], []

    yesterday = all_days[today_idx - 1]
    # 回看需要的天数范围
    lookback = max(N_LOOKBACK, V_LOOKBACK) + 5  # 多取几天余量
    start_idx = max(0, today_idx - lookback)
    history_days = all_days[start_idx:today_idx]  # 不含today

    # 批量获取today数据
    cursor.execute("""
        SELECT code, code_name, open, high, low, close, preclose,
               close_rate, volume, turn, isST,
               hour1_open, hour1_high, hour1_low, hour1_close,
               hour2_open, hour2_high, hour2_low, hour2_close,
               hour3_open, hour3_high, hour3_low, hour3_close,
               hour4_open, hour4_high, hour4_low, hour4_close
        FROM stock_kline
        WHERE date = ? AND preclose > 0
    """, (today,))
    columns = [desc[0] for desc in cursor.description]
    today_stocks = [dict(zip(columns, row)) for row in cursor.fetchall()]

    n_candidates = []
    v_candidates = []

    for ts in today_stocks:
        code = ts['code']
        # 排除ST
        if is_st(ts):
            continue
        # 排除一字涨停
        if is_yizi_limit_up(code, ts['open'], ts['high'], ts['low'], ts['close'], ts['preclose']):
            continue
        # 高开确认
        gap_up_pct = (ts['open'] - ts['preclose']) / ts['preclose'] * 100
        if gap_up_pct < GAP_UP_MIN:
            continue
        # 高开不超过7%（排除过高开一字板嫌疑）
        if gap_up_pct > 7.0:
            continue
        # 换手率过滤
        today_turn = ts.get('turn') or 0
        if today_turn < TURN_MIN:
            continue

        # 获取历史数据
        history = get_stock_history(cursor, code, history_days)
        if len(history) < 4:
            continue

        # yesterday close必须存在
        if not history or history[-1]['date'] != yesterday:
            continue
        yesterday_close = history[-1]['close']
        if yesterday_close <= 0:
            continue

        # today open > yesterday close
        if ts['open'] <= yesterday_close:
            continue

        # 检测N字形态
        found_n, n_info = find_n_shape(history)
        if found_n:
            n_candidates.append((code, ts.get('code_name', ''), ts, n_info))

        # 检测V字形态
        found_v, v_info = find_v_shape(history)
        if found_v:
            v_candidates.append((code, ts.get('code_name', ''), ts, v_info))

    return n_candidates, v_candidates


def compute_stats(candidates_list, shape_type):
    """计算统计数据"""
    if not candidates_list:
        return

    # Hour1买入统计
    h1_rets = []
    h1_positive = 0
    h1_future_max = []

    # Hour2确认买入统计
    h2_rets = []
    h2_positive = 0

    for item in candidates_list:
        buy_price = item['buy_price']
        today_close = item['today_close']
        today_high = item['today_high']

        if buy_price <= 0:
            continue

        ret = (today_close - buy_price) / buy_price * 100
        h1_rets.append(ret)
        if today_close > buy_price:
            h1_positive += 1
        if item['future_max'] is not None:
            h1_future_max.append(item['future_max'])

        # Hour2: 使用hour2_open作为买入价，需hour1确认（hour1不破开盘价或hour1收阳）
        h1_open = item.get('hour1_open')
        h1_low = item.get('hour1_low')
        h1_close = item.get('hour1_close')
        h2_open = item.get('hour2_open')
        if h1_open and h1_open > 0 and h1_low and h1_close and h2_open and h2_open > 0:
            h1_not_break = h1_low >= buy_price
            h1_yang = h1_close >= h1_open
            if h1_not_break or h1_yang:
                h2_ret = (today_close - h2_open) / h2_open * 100
                h2_rets.append(h2_ret)
                if today_close > h2_open:
                    h2_positive += 1

    total = len(h1_rets)
    print(f"\n{shape_type}形态统计:")
    print(f"  总候选: {total}只")
    if total > 0:
        avg_ret = sum(h1_rets) / total
        pos_ratio = h1_positive / total * 100
        avg_future_max = sum(h1_future_max) / len(h1_future_max) if h1_future_max else 0
        print(f"  Hour1买入 - 当日收益均值: {avg_ret:+.2f}%, "
              f"收阳占比: {pos_ratio:.1f}%, "
              f"5日最高涨均值: {avg_future_max:+.2f}%")

    h2_total = len(h2_rets)
    if h2_total > 0:
        h2_avg_ret = sum(h2_rets) / h2_total
        h2_pos_ratio = h2_positive / h2_total * 100
        print(f"  Hour2确认买入 - 当日收益均值: {h2_avg_ret:+.2f}%, "
              f"收阳占比: {h2_pos_ratio:.1f}% (样本{h2_total}只)")
    else:
        print(f"  Hour2确认买入 - 无有效样本")


def main():
    if len(sys.argv) < 2:
        print("用法: python research_nv_shape.py <月份>")
        print("示例: python research_nv_shape.py 2025-06")
        sys.exit(1)

    month_str = sys.argv[1]
    print(f"{'=' * 60}")
    print(f"N字/V字形态 + 今日高开 - 候选股研究")
    print(f"参数: 月份={month_str}")
    print(f"  N字: 第一波涨≥{N_FIRST_WAVE_MIN}%, 回调≥{N_PULLBACK_MIN}%, 回看{N_LOOKBACK}日")
    print(f"  V字: 累跌≥{V_DROP_MIN}%, 反弹≥{V_REBOUND_MIN}%, 回看{V_LOOKBACK}日")
    print(f"  高开确认≥{GAP_UP_MIN}%")
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

    # 统计收集
    all_n_stats = []
    all_v_stats = []

    for today in month_days:
        if today not in all_days:
            continue
        today_idx = all_days.index(today)
        if today_idx < 1:
            continue

        n_cands, v_cands = find_candidates_for_day(cursor, today, all_days, today_idx)

        print(f"\n{'=' * 10} {today} {'=' * 10}")
        print(f"N字形态候选: {len(n_cands)}只")
        print(f"V字形态候选: {len(v_cands)}只")

        # 打印N字候选
        for code, code_name, today_row, shape_info in n_cands:
            buy_price = today_row['open']
            context_rows = get_context_hours(cursor, code, all_days, today_idx, CONTEXT_DAYS)
            print_candidate('N字', code, code_name, shape_info, today_row, context_rows, today, buy_price)

            # 收集统计
            future_rows = [r for r in context_rows if r['date'] > today]
            future_max = None
            if future_rows:
                highs = [r['high'] for r in future_rows if r['high'] and r['high'] > 0]
                if highs:
                    future_max = (max(highs) - buy_price) / buy_price * 100

            all_n_stats.append({
                'code': code,
                'date': today,
                'buy_price': buy_price,
                'today_close': today_row['close'],
                'today_high': today_row['high'],
                'future_max': future_max,
                'hour1_open': today_row.get('hour1_open'),
                'hour1_low': today_row.get('hour1_low'),
                'hour1_close': today_row.get('hour1_close'),
                'hour2_open': today_row.get('hour2_open'),
            })

        # 打印V字候选
        for code, code_name, today_row, shape_info in v_cands:
            buy_price = today_row['open']
            context_rows = get_context_hours(cursor, code, all_days, today_idx, CONTEXT_DAYS)
            print_candidate('V字', code, code_name, shape_info, today_row, context_rows, today, buy_price)

            future_rows = [r for r in context_rows if r['date'] > today]
            future_max = None
            if future_rows:
                highs = [r['high'] for r in future_rows if r['high'] and r['high'] > 0]
                if highs:
                    future_max = (max(highs) - buy_price) / buy_price * 100

            all_v_stats.append({
                'code': code,
                'date': today,
                'buy_price': buy_price,
                'today_close': today_row['close'],
                'today_high': today_row['high'],
                'future_max': future_max,
                'hour1_open': today_row.get('hour1_open'),
                'hour1_low': today_row.get('hour1_low'),
                'hour1_close': today_row.get('hour1_close'),
                'hour2_open': today_row.get('hour2_open'),
            })

    # 月度统计汇总
    print(f"\n\n{'=' * 60}")
    print(f"{'=' * 10} 月度统计汇总 {'=' * 10}")
    print(f"{'=' * 60}")

    compute_stats(all_n_stats, 'N字')
    compute_stats(all_v_stats, 'V字')

    # 合并统计
    all_combined = all_n_stats + all_v_stats
    if all_combined:
        print(f"\n合计(N+V):")
        print(f"  总候选: {len(all_combined)}只 (N={len(all_n_stats)}, V={len(all_v_stats)})")
        total_positive = sum(1 for c in all_combined if c['today_close'] > c['buy_price'] and c['buy_price'] > 0)
        valid = [c for c in all_combined if c['buy_price'] > 0]
        if valid:
            avg_ret = sum((c['today_close'] - c['buy_price']) / c['buy_price'] * 100 for c in valid) / len(valid)
            print(f"  Hour1综合 - 当日收益均值: {avg_ret:+.2f}%, "
                  f"收阳占比: {total_positive / len(valid) * 100:.1f}%")

    conn.close()
    print(f"\n完成。")


if __name__ == "__main__":
    main()
