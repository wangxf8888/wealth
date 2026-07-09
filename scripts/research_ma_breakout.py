#!/usr/bin/env python3
"""
MA5/MA10均线突破 + 高开确认 - 候选股研究脚本

策略概念：
  - 股价之前在MA5/MA10下方运行（弱势）
  - yesterday收盘站上MA5或MA10（突破）
  - today高开确认突破有效

T+0合规：
  - MA5/MA10用yesterday及之前5/10日的close计算
  - "yesterday站上MA"用yesterday close vs MA值判断
  - today open确认（高开=继续在MA上方）
  - 不得使用today的close/high/low/volume
"""
import sys
import sqlite3
from collections import defaultdict

# ========== 配置区 ==========
DB_PATH = '/home/AIWealth/data/stocks.db'
MAX_CANDIDATES_PER_DAY = 5  # 每天最多打印的候选股明细数
MIN_TURNOVER = 1.0          # 最低换手率(%)
MIN_RECENT_DROP = 5.0       # 近10日最少累跌幅度(%)
# ============================


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


def is_limit_up(close, preclose, code):
    """判断是否涨停"""
    if preclose is None or preclose <= 0 or close is None:
        return False
    return close >= calc_limit_up(preclose, code)


def is_yizi_limit_up(open_p, high, low, close, preclose, code):
    """判断是否一字涨停"""
    if any(v is None for v in [open_p, high, low, close, preclose]):
        return False
    if preclose <= 0:
        return False
    limit_up = calc_limit_up(preclose, code)
    return (open_p == high == low == close) and close >= limit_up


def calc_ma(closes, period):
    """计算移动平均线，closes为近N日收盘价列表"""
    if len(closes) < period:
        return None
    return sum(closes[-period:]) / period


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


def get_stock_history(cur, code, dates):
    """获取指定日期列表的close数据"""
    if not dates:
        return {}
    placeholders = ','.join(['?'] * len(dates))
    cur.execute(f"""
        SELECT date, close FROM stock_kline
        WHERE code = ? AND date IN ({placeholders})
        ORDER BY date
    """, [code] + dates)
    return {r[0]: r[1] for r in cur.fetchall()}


def find_candidates(cur, today, yesterday, all_days, all_days_idx):
    """
    找出满足MA5/MA10突破条件的候选股
    """
    today_idx = all_days_idx[today]
    yesterday_idx = all_days_idx[yesterday]

    # 需要yesterday前10日数据来计算MA10_yesterday和MA5_T-2
    # MA10_yesterday需要T-10到T-1共10日close
    # MA5_T-2需要T-6到T-2共5日close
    # 近10日跌幅需要T-10到T-1
    lookback_start = max(0, today_idx - 12)  # 多取2天buffer
    lookback_days = all_days[lookback_start:today_idx + 1]  # 含today

    if len(lookback_days) < 12:
        return [], []

    # 前日(T-2)
    if yesterday_idx < 1:
        return [], []
    day_t_minus_2 = all_days[yesterday_idx - 1]

    # 批量获取today的数据
    cur.execute("""
        SELECT code, code_name, open, high, low, close, preclose, isST, turn,
               hour1_open, hour1_high, hour1_low, hour1_close,
               hour2_open, hour2_high, hour2_low, hour2_close,
               hour3_open, hour3_high, hour3_low, hour3_close,
               hour4_open, hour4_high, hour4_low, hour4_close,
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

    # 获取lookback区间所有数据（批量查询优化性能）
    lb_placeholders = ','.join(['?'] * len(lookback_days))
    cur.execute(f"""
        SELECT code, date, close, high, low
        FROM stock_kline WHERE date IN ({lb_placeholders})
        ORDER BY code, date
    """, lookback_days)
    # 组织成 {code: [(date, close, high, low), ...]}
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
        yd_open = yd_row[6]
        yd_high = yd_row[7]
        yd_low = yd_row[8]

        # 排除ST
        if yd_isST:
            continue
        if code_name and 'ST' in code_name.upper():
            continue

        # 排除yesterday涨停（避免追高）
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

        if t_isST:
            continue
        if t_open is None or t_open <= 0 or t_preclose is None or t_preclose <= 0:
            continue

        # 排除一字涨停
        if is_yizi_limit_up(t_open, t_high, t_low, t_close, t_preclose, code):
            continue

        # 估算流通市值（亿元）: amount * 100 / turn / 1e8
        t_amount = t[25]  # amount字段
        market_cap = None
        if t_amount and yd_turn and yd_turn > 0:
            market_cap = t_amount * 100 / yd_turn / 1e8

        # today高开确认: open > yesterday close
        if t_open <= yd_close:
            continue

        # 获取该股的历史close列表
        hist = history_data.get(code, [])
        if not hist:
            continue

        # 按日期排序，构建日期->close映射
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

        # MA5_yesterday = mean of last 5 closes up to yesterday
        last5_closes = [c[1] for c in closes_to_yesterday[-5:]]
        ma5_yesterday = calc_ma(last5_closes, 5)

        # MA10_yesterday = mean of last 10 closes up to yesterday
        last10_closes = [c[1] for c in closes_to_yesterday[-10:]]
        ma10_yesterday = calc_ma(last10_closes, 10)

        # MA5_T-2 = mean of last 5 closes up to T-2
        # T-2 is yesterday_idx-1 in all_days
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

        # 近10日跌幅检查: 近10日内最高到最低的回撤>=MIN_RECENT_DROP%
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

        # 构建候选股基本信息
        base_info = {
            'code': code,
            'code_name': code_name,
            'today': today,
            'yesterday': yesterday,
            'yd_close': yd_close,
            'yd_preclose': yd_preclose,
            'yd_turn': yd_turn,
            't_open': t_open,
            't_preclose': t_preclose,
            'open_rate': (t_open - t_preclose) / t_preclose * 100,
            'recent_high': max_high,
            'recent_low': min_low,
            'recent_drop_pct': recent_drop_pct,
            'market_cap': market_cap,
            'hour_data_today': {
                'h1': (t[9], t[10], t[11], t[12]),
                'h2': (t[13], t[14], t[15], t[16]),
                'h3': (t[17], t[18], t[19], t[20]),
                'h4': (t[21], t[22], t[23], t[24]),
            }
        }

        # ========== MA5突破检查 ==========
        if ma5_yesterday is not None and ma5_t2 is not None and t2_close is not None:
            # 条件1: T-2 close < MA5_T-2
            # 条件2: yesterday close > MA5_yesterday
            # 条件3: today open > yesterday close (已检查)
            # 条件4: today open > MA5_yesterday
            if (t2_close < ma5_t2 and
                yd_close > ma5_yesterday and
                t_open > ma5_yesterday):
                info = base_info.copy()
                info['type'] = 'MA5'
                info['t2_close'] = t2_close
                info['ma_t2'] = ma5_t2
                info['ma_yesterday'] = ma5_yesterday
                info['breakout_pct'] = (yd_close - ma5_yesterday) / ma5_yesterday * 100
                ma5_candidates.append(info)

        # ========== MA10突破检查 ==========
        if ma10_yesterday is not None and ma10_t2 is not None and t2_close is not None:
            if (t2_close < ma10_t2 and
                yd_close > ma10_yesterday and
                t_open > ma10_yesterday):
                info = base_info.copy()
                info['type'] = 'MA10'
                info['t2_close'] = t2_close
                info['ma_t2'] = ma10_t2
                info['ma_yesterday'] = ma10_yesterday
                info['breakout_pct'] = (yd_close - ma10_yesterday) / ma10_yesterday * 100
                ma10_candidates.append(info)

    # 按突破幅度排序
    ma5_candidates.sort(key=lambda x: x['breakout_pct'], reverse=True)
    ma10_candidates.sort(key=lambda x: x['breakout_pct'], reverse=True)

    return ma5_candidates, ma10_candidates


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


def format_hour_table(hour_rows, buy_date, buy_price):
    """格式化hour级明细表格"""
    lines = []
    lines.append(f"  {'日期':<12}| {'hour':<5}| {'open':<8}| {'high':<8}| {'low':<8}| {'close':<8}| vs买入价  | turn")
    lines.append(f"  {'-'*12}+{'-'*6}+{'-'*9}+{'-'*9}+{'-'*9}+{'-'*9}+{'-'*10}+{'-'*8}")

    for row in hour_rows:
        date = row[0]
        day_open, day_close, day_preclose, day_turn = row[1], row[2], row[3], row[4]
        turn_str = f"{day_turn:.2f}%" if day_turn is not None else "--"
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
            marker = " ← 买入日" if date == buy_date and h_name == 'h1' else ""
            lines.append(
                f"  {date:<12}| {h_name:<5}| {h_open:<8.2f}| {h_high:<8.2f}| "
                f"{h_low:<8.2f}| {h_close:<8.2f}| {vs_buy:+.2f}%    | {turn_str}{marker}"
            )
            turn_str = ""  # 只在该日第一行显示turn
    return '\n'.join(lines)


def calc_key_metrics(cur, code, buy_date, buy_price, all_days):
    """计算关键指标"""
    if buy_price is None or buy_price <= 0:
        return None
    try:
        idx = all_days.index(buy_date)
    except ValueError:
        return None

    future_days = all_days[idx:idx + 6]
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
        metrics['day_high_pct'] = (today_row[2] - buy_price) / buy_price * 100 if buy_price > 0 else 0
        metrics['day_close_pct'] = (today_row[4] - buy_price) / buy_price * 100 if buy_price > 0 else 0

    all_highs = [r[2] for r in rows if r[2] is not None]
    all_lows = [r[3] for r in rows if r[3] is not None]
    if all_highs:
        metrics['max_5d'] = max(all_highs)
        metrics['max_5d_pct'] = (max(all_highs) - buy_price) / buy_price * 100 if buy_price > 0 else 0
    if all_lows:
        metrics['min_5d'] = min(all_lows)
        metrics['min_5d_pct'] = (min(all_lows) - buy_price) / buy_price * 100 if buy_price > 0 else 0

    return metrics


def print_candidate_detail(cur, c, all_days, today_idx):
    """打印单个候选股明细"""
    ma_type = c['type']
    print(f"\n--- [{ma_type}突破] {c['code']} ({c['code_name'] or 'N/A'}) ---")
    print(f"  前日状态: close={c['t2_close']:.2f}, {ma_type}={c['ma_t2']:.2f} (在{ma_type}下方)")
    print(f"  昨日突破: close={c['yd_close']:.2f}, {ma_type}={c['ma_yesterday']:.2f} "
          f"(站上{ma_type}, 突破{c['breakout_pct']:+.1f}%)")
    print(f"  今日确认: open={c['t_open']:.2f}, preclose={c['t_preclose']:.2f}, "
          f"高开{c['open_rate']:+.2f}%")
    print(f"  近10日走势: 高点{c['recent_high']:.2f}→低点{c['recent_low']:.2f} "
          f"回调-{c['recent_drop_pct']:.1f}%")
    print(f"  昨日换手率: {c['yd_turn']:.2f}%")

    # 前5日+后5日 hour级明细
    prev5_start = max(0, today_idx - 5)
    next5_end = min(len(all_days), today_idx + 6)
    window_days = all_days[prev5_start:next5_end]

    buy_price = c['t_open']
    hour_rows = get_hour_data_for_days(cur, c['code'], window_days)
    if hour_rows:
        print(f"\n  前5日+后5日 hour级明细:")
        print(format_hour_table(hour_rows, c['today'], buy_price))

    # 关键指标
    metrics = calc_key_metrics(cur, c['code'], c['today'], buy_price, all_days)
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

    return metrics


def main():
    if len(sys.argv) < 2:
        print("用法: python research_ma_breakout.py 2025-06")
        print("参数: 月份(YYYY-MM格式)")
        sys.exit(1)

    month_str = sys.argv[1]
    if len(month_str) != 7 or month_str[4] != '-':
        print(f"错误: 月份格式应为YYYY-MM，收到: {month_str}")
        sys.exit(1)

    print(f"{'='*80}")
    print(f"MA5/MA10均线突破 + 高开确认 - 候选股研究")
    print(f"研究月份: {month_str}")
    print(f"参数: 换手率>{MIN_TURNOVER}%, 近10日回撤>{MIN_RECENT_DROP}%")
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
    all_days_idx = {d: i for i, d in enumerate(all_days)}

    print(f"该月交易日数: {len(month_days)}")
    print(f"数据库总交易日数: {len(all_days)}\n")

    # 统计汇总
    total_ma5 = 0
    total_ma10 = 0

    # Hour1 vs Hour2 统计: (day_close_pct, max_5d_pct, is_positive, code, market_cap)
    h1_stats_ma5 = []
    h1_stats_ma10 = []
    h2_stats_ma5 = []
    h2_stats_ma10 = []

    for today in month_days:
        if today not in all_days_idx:
            continue
        today_idx = all_days_idx[today]
        if today_idx < 12:
            continue
        yesterday = all_days[today_idx - 1]

        ma5_cands, ma10_cands = find_candidates(cur, today, yesterday, all_days, all_days_idx)
        total_ma5 += len(ma5_cands)
        total_ma10 += len(ma10_cands)

        print(f"\n{'='*60}")
        print(f"========== {today} ==========")
        print(f"MA5突破候选: {len(ma5_cands)}只")
        print(f"MA10突破候选: {len(ma10_cands)}只")

        # 展示MA5候选明细
        shown_ma5 = min(MAX_CANDIDATES_PER_DAY, len(ma5_cands))
        for i in range(shown_ma5):
            c = ma5_cands[i]
            metrics = print_candidate_detail(cur, c, all_days, today_idx)

            # Hour2确认分析
            h1_data = c['hour_data_today']['h1']
            h2_data = c['hour_data_today']['h2']
            h1_open, h1_high, h1_low, h1_close = h1_data
            h2_open = h2_data[0] if h2_data[0] else None

            if h1_open and h1_close and h1_open > 0:
                h1_positive = h1_close >= h1_open
                h1_not_break = h1_low >= c['t_open'] if h1_low else False
                h1_pct = (h1_close - h1_open) / h1_open * 100
                print(f"\n  Hour2确认分析:")
                print(f"    Hour1: {'收阳' if h1_positive else '收阴'} ({h1_pct:+.2f}%) | "
                      f"{'不破开盘' if h1_not_break else '破开盘价'}")
                if h1_positive and h2_open and h2_open > 0:
                    h2_metrics = calc_key_metrics(cur, c['code'], today, h2_open, all_days)
                    if h2_metrics:
                        print(f"    → Hour2买入价: {h2_open:.2f}")
                        if 'day_close_pct' in h2_metrics:
                            print(f"    → 当日收盘vs H2买入: {h2_metrics['day_close_pct']:+.2f}%")
                        if 'max_5d_pct' in h2_metrics:
                            print(f"    → 5日最大涨幅vs H2买入: {h2_metrics['max_5d_pct']:+.2f}%")

        # 展示MA10候选明细
        shown_ma10 = min(MAX_CANDIDATES_PER_DAY, len(ma10_cands))
        for i in range(shown_ma10):
            c = ma10_cands[i]
            metrics = print_candidate_detail(cur, c, all_days, today_idx)

            h1_data = c['hour_data_today']['h1']
            h2_data = c['hour_data_today']['h2']
            h1_open, h1_high, h1_low, h1_close = h1_data
            h2_open = h2_data[0] if h2_data[0] else None

            if h1_open and h1_close and h1_open > 0:
                h1_positive = h1_close >= h1_open
                h1_not_break = h1_low >= c['t_open'] if h1_low else False
                h1_pct = (h1_close - h1_open) / h1_open * 100
                print(f"\n  Hour2确认分析:")
                print(f"    Hour1: {'收阳' if h1_positive else '收阴'} ({h1_pct:+.2f}%) | "
                      f"{'不破开盘' if h1_not_break else '破开盘价'}")
                if h1_positive and h2_open and h2_open > 0:
                    h2_metrics = calc_key_metrics(cur, c['code'], today, h2_open, all_days)
                    if h2_metrics:
                        print(f"    → Hour2买入价: {h2_open:.2f}")
                        if 'day_close_pct' in h2_metrics:
                            print(f"    → 当日收盘vs H2买入: {h2_metrics['day_close_pct']:+.2f}%")
                        if 'max_5d_pct' in h2_metrics:
                            print(f"    → 5日最大涨幅vs H2买入: {h2_metrics['max_5d_pct']:+.2f}%")

        # 收集所有候选股的统计数据(Hour1 & Hour2)
        for c in ma5_cands:
            buy_price = c['t_open']
            metrics = calc_key_metrics(cur, c['code'], today, buy_price, all_days)
            if metrics and 'day_close_pct' in metrics and 'max_5d_pct' in metrics:
                is_pos = metrics['day_close_pct'] > 0
                h1_stats_ma5.append((metrics['day_close_pct'], metrics['max_5d_pct'], is_pos, c['code'], c['market_cap']))

            # Hour2
            h1_data = c['hour_data_today']['h1']
            h2_data = c['hour_data_today']['h2']
            h1_open, _, h1_low, h1_close = h1_data
            h2_open = h2_data[0] if h2_data[0] else None
            if (h1_open and h1_close and h1_open > 0 and
                h1_close >= h1_open and h2_open and h2_open > 0):
                h2_metrics = calc_key_metrics(cur, c['code'], today, h2_open, all_days)
                if h2_metrics and 'day_close_pct' in h2_metrics and 'max_5d_pct' in h2_metrics:
                    is_pos2 = h2_metrics['day_close_pct'] > 0
                    h2_stats_ma5.append((h2_metrics['day_close_pct'], h2_metrics['max_5d_pct'], is_pos2, c['code'], c['market_cap']))

        for c in ma10_cands:
            buy_price = c['t_open']
            metrics = calc_key_metrics(cur, c['code'], today, buy_price, all_days)
            if metrics and 'day_close_pct' in metrics and 'max_5d_pct' in metrics:
                is_pos = metrics['day_close_pct'] > 0
                h1_stats_ma10.append((metrics['day_close_pct'], metrics['max_5d_pct'], is_pos, c['code'], c['market_cap']))

            h1_data = c['hour_data_today']['h1']
            h2_data = c['hour_data_today']['h2']
            h1_open, _, h1_low, h1_close = h1_data
            h2_open = h2_data[0] if h2_data[0] else None
            if (h1_open and h1_close and h1_open > 0 and
                h1_close >= h1_open and h2_open and h2_open > 0):
                h2_metrics = calc_key_metrics(cur, c['code'], today, h2_open, all_days)
                if h2_metrics and 'day_close_pct' in h2_metrics and 'max_5d_pct' in h2_metrics:
                    is_pos2 = h2_metrics['day_close_pct'] > 0
                    h2_stats_ma10.append((h2_metrics['day_close_pct'], h2_metrics['max_5d_pct'], is_pos2, c['code'], c['market_cap']))

    # ========== 月度汇总 ==========
    print(f"\n\n{'='*80}")
    print(f"月度汇总统计")
    print(f"{'='*80}")
    print(f"MA5突破总候选数: {total_ma5}")
    print(f"MA10突破总候选数: {total_ma10}")
    print(f"日均MA5候选: {total_ma5 / len(month_days):.1f}")
    print(f"日均MA10候选: {total_ma10 / len(month_days):.1f}")

    # ========== 按流通市值分组统计 ==========
    def get_cap_group(cap):
        if cap is None:
            return '未知'
        if cap < 50:
            return '<50亿'
        elif cap < 200:
            return '50-200亿'
        elif cap < 700:
            return '200-700亿'
        else:
            return '>700亿'

    def get_board(code):
        if code.startswith('sz.300') or code.startswith('sz.301'):
            return '创业板'
        elif code.startswith('sh.688'):
            return '科创板'
        elif code.startswith('bj.'):
            return '北交所'
        else:
            return '主板'

    def print_group_stats(label, stats_list):
        """stats_list: [(day_close_pct, max_5d_pct, is_pos, code, market_cap)]"""
        if not stats_list:
            print(f"  {label}: 无数据")
            return

        # 按市值分组
        cap_groups = defaultdict(list)
        for s in stats_list:
            cap_groups[get_cap_group(s[4])].append(s)

        # 按板块分组
        board_groups = defaultdict(list)
        for s in stats_list:
            board_groups[get_board(s[3])].append(s)

        print(f"\n  --- {label} 按流通市值分组 ---")
        print(f"  {'市值区间':<12} {'样本数':<8} {'当日收益均值':<14} {'5日最高均值':<14} {'收阳占比':<12}")
        print(f"  {'-'*60}")
        for grp in ['<50亿', '50-200亿', '200-700亿', '>700亿', '未知']:
            items = cap_groups.get(grp, [])
            if not items:
                continue
            avg_ret = sum(i[0] for i in items) / len(items)
            avg_max = sum(i[1] for i in items) / len(items)
            pos_rate = sum(1 for i in items if i[2]) / len(items) * 100
            print(f"  {grp:<12} {len(items):<8} {avg_ret:+.2f}%{'':>8} {avg_max:+.2f}%{'':>8} {pos_rate:.1f}%")

        print(f"\n  --- {label} 按板块分组 ---")
        print(f"  {'板块':<12} {'样本数':<8} {'当日收益均值':<14} {'5日最高均值':<14} {'收阳占比':<12}")
        print(f"  {'-'*60}")
        for grp in ['主板', '创业板', '科创板', '北交所']:
            items = board_groups.get(grp, [])
            if not items:
                continue
            avg_ret = sum(i[0] for i in items) / len(items)
            avg_max = sum(i[1] for i in items) / len(items)
            pos_rate = sum(1 for i in items if i[2]) / len(items) * 100
            print(f"  {grp:<12} {len(items):<8} {avg_ret:+.2f}%{'':>8} {avg_max:+.2f}%{'':>8} {pos_rate:.1f}%")

    # ========== Hour1 vs Hour2 对比 ==========
    print(f"\n\n{'='*80}")
    print(f"Hour1 vs Hour2 买入时机对比")
    print(f"{'='*80}")

    def print_stats(label, h1_list, h2_list):
        print(f"\n  === {label} ===")
        print(f"\n  Hour1买入(只看yesterday突破+today高开):")
        if h1_list:
            day_rets = [s[0] for s in h1_list]
            max5d = [s[1] for s in h1_list]
            pos_count = sum(1 for s in h1_list if s[2])
            print(f"    - 样本数: {len(h1_list)}")
            print(f"    - 当日收益均值: {sum(day_rets)/len(day_rets):+.2f}%")
            print(f"    - 5日最高涨均值: {sum(max5d)/len(max5d):+.2f}%")
            print(f"    - 收阳占比: {pos_count/len(h1_list)*100:.1f}% ({pos_count}/{len(h1_list)})")
        else:
            print(f"    - 无有效数据")

        print(f"\n  Hour2确认买入(加条件: hour1收阳):")
        if h2_list:
            day_rets = [s[0] for s in h2_list]
            max5d = [s[1] for s in h2_list]
            pos_count = sum(1 for s in h2_list if s[2])
            print(f"    - 样本数: {len(h2_list)}")
            print(f"    - 当日收益均值: {sum(day_rets)/len(day_rets):+.2f}%")
            print(f"    - 5日最高涨均值: {sum(max5d)/len(max5d):+.2f}%")
            print(f"    - 收阳占比: {pos_count/len(h2_list)*100:.1f}% ({pos_count}/{len(h2_list)})")
        else:
            print(f"    - 无满足hour1收阳条件的数据")

        # 对比
        if h1_list and h2_list:
            h1_avg = sum(s[0] for s in h1_list) / len(h1_list)
            h2_avg = sum(s[0] for s in h2_list) / len(h2_list)
            h1_wr = sum(1 for s in h1_list if s[2]) / len(h1_list) * 100
            h2_wr = sum(1 for s in h2_list if s[2]) / len(h2_list) * 100
            print(f"\n  对比:")
            print(f"    收益: Hour2({h2_avg:+.2f}%) vs Hour1({h1_avg:+.2f}%) 差{h2_avg-h1_avg:+.2f}%")
            print(f"    胜率: Hour2({h2_wr:.1f}%) vs Hour1({h1_wr:.1f}%) 差{h2_wr-h1_wr:+.1f}%")
            print(f"    Hour2过滤掉: {len(h1_list)-len(h2_list)}个 ({(len(h1_list)-len(h2_list))/len(h1_list)*100:.1f}%)")

    print_stats("MA5突破", h1_stats_ma5, h2_stats_ma5)
    print_stats("MA10突破", h1_stats_ma10, h2_stats_ma10)

    # ========== 分组维度统计 ==========
    print(f"\n\n{'='*80}")
    print(f"分组维度统计（寻找alpha）")
    print(f"{'='*80}")

    print_group_stats("MA5突破 Hour1", h1_stats_ma5)
    print_group_stats("MA5突破 Hour2", h2_stats_ma5)
    print_group_stats("MA10突破 Hour1", h1_stats_ma10)
    print_group_stats("MA10突破 Hour2", h2_stats_ma10)

    conn.close()
    print(f"\n{'='*80}")
    print("研究完成。")


if __name__ == '__main__':
    main()
