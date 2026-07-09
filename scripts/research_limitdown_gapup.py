#!/usr/bin/env python3
"""跌停次日高开买入策略 - Phase1研究脚本

用法:
    python3 scripts/research_limitdown_gapup.py [YYYY-MM]

示例:
    python3 scripts/research_limitdown_gapup.py 2025-06

策略逻辑:
    1) 昨日跌停: close <= round(preclose * (1-ratio), 2)
    2) 今日竞价高开 >= 1%: (open - preclose) / preclose >= 0.01
    3) 排除ST、一字涨停、一字跌停
    4) 以today hour1 open价买入

T+0合规:
    - "昨日是否跌停" → yesterday收盘后已确定
    - "今日高开多少" → 竞价9:25确定
    - 买入价=today open → 真实可执行价格
"""

import sqlite3
import sys
from collections import defaultdict

# ============================================================
# 配置
# ============================================================
DB_PATH = '/home/AIWealth/data/stocks.db'
TARGET_MONTH = sys.argv[1] if len(sys.argv) > 1 else '2025-06'

WINDOW_BEFORE = 5
WINDOW_AFTER = 5
GAP_UP_THRESHOLD = 1.0  # 高开阈值，百分比

# ============================================================
# 涨跌停计算
# ============================================================

def get_ratio(code: str) -> float:
    """根据股票代码返回涨跌停比例"""
    if code.startswith('sz.30') or code.startswith('sh.68'):
        return 0.20  # 创业板/科创板
    elif code.startswith('bj.') or code.startswith('43') or code.startswith('83') or code.startswith('87'):
        return 0.30  # 北交所
    else:
        return 0.10  # 主板


def calc_limit_down(preclose, ratio):
    """计算跌停价"""
    return round(preclose * (1 - ratio), 2)


def calc_limit_up(preclose, ratio):
    """计算涨停价"""
    return round(preclose * (1 + ratio), 2)


def is_limit_down(close, preclose, code: str) -> bool:
    """严格跌停判定: close <= round(preclose * (1-ratio), 2)"""
    if preclose is None or preclose == 0 or close is None:
        return False
    try:
        ratio = get_ratio(code)
        limit_down_price = calc_limit_down(float(preclose), ratio)
        return float(close) <= limit_down_price
    except (TypeError, ValueError, ZeroDivisionError):
        return False


def is_one_word_limit_up(o, h, l, c, preclose, code):
    """一字涨停: open==high==low==close 且 >= 涨停价"""
    if any(x is None for x in (o, h, l, c, preclose)):
        return False
    o, h, l, c, preclose = float(o), float(h), float(l), float(c), float(preclose)
    if preclose == 0:
        return False
    if not (o == h == l == c):
        return False
    ratio = get_ratio(code)
    limit_up_price = calc_limit_up(preclose, ratio)
    return c >= limit_up_price


def is_one_word_limit_down(o, h, l, c, preclose, code):
    """一字跌停: open==high==low==close 且 <= 跌停价"""
    if any(x is None for x in (o, h, l, c, preclose)):
        return False
    o, h, l, c, preclose = float(o), float(h), float(l), float(c), float(preclose)
    if preclose == 0:
        return False
    if not (o == h == l == c):
        return False
    ratio = get_ratio(code)
    limit_down_price = calc_limit_down(preclose, ratio)
    return c <= limit_down_price


# ============================================================
# 数据访问
# ============================================================

def get_trading_days(conn, month: str):
    """获取指定月份内所有交易日 (升序)"""
    cur = conn.execute(
        "SELECT DISTINCT date FROM stock_kline WHERE date LIKE ? ORDER BY date ASC",
        (f"{month}-%",),
    )
    return [r[0] for r in cur.fetchall()]


def get_prev_trading_day(conn, day: str):
    """获取day前一个交易日"""
    cur = conn.execute(
        "SELECT MAX(date) FROM stock_kline WHERE date < ?",
        (day,),
    )
    row = cur.fetchone()
    return row[0] if row and row[0] else None


def get_window_dates(conn, day: str, before: int, after: int):
    """获取day前后各before/after个交易日 (含day本身) 的日期列表"""
    cur = conn.execute(
        "SELECT DISTINCT date FROM stock_kline WHERE date < ? ORDER BY date DESC LIMIT ?",
        (day, before),
    )
    prev_days = [r[0] for r in cur.fetchall()][::-1]

    cur = conn.execute(
        "SELECT DISTINCT date FROM stock_kline WHERE date > ? ORDER BY date ASC LIMIT ?",
        (day, after),
    )
    next_days = [r[0] for r in cur.fetchall()]

    return prev_days + [day] + next_days


def fetch_day_data(conn, day: str):
    """获取某日全市场行情"""
    cur = conn.execute(
        """SELECT code, open, high, low, close, preclose, volume, turn, isST,
                  hour1_open, hour1_high, hour1_low, hour1_close,
                  hour2_open, hour2_high, hour2_low, hour2_close,
                  hour3_open, hour3_high, hour3_low, hour3_close,
                  hour4_open, hour4_high, hour4_low, hour4_close
           FROM stock_kline WHERE date = ?""",
        (day,),
    )
    cols = [d[0] for d in cur.description]
    return {row[0]: dict(zip(cols, row)) for row in cur.fetchall()}


def fetch_stock_window(conn, code: str, dates):
    """获取股票在给定日期列表的hour级行情"""
    if not dates:
        return {}
    placeholders = ','.join('?' * len(dates))
    sql = f"""
        SELECT date, open, high, low, close, preclose, volume, turn,
               hour1_open, hour1_high, hour1_low, hour1_close,
               hour2_open, hour2_high, hour2_low, hour2_close,
               hour3_open, hour3_high, hour3_low, hour3_close,
               hour4_open, hour4_high, hour4_low, hour4_close
        FROM stock_kline
        WHERE code = ? AND date IN ({placeholders})
        ORDER BY date ASC
    """
    cur = conn.execute(sql, [code] + list(dates))
    cols = [d[0] for d in cur.description]
    return {row[0]: dict(zip(cols, row)) for row in cur.fetchall()}


# ============================================================
# 候选股筛选
# ============================================================

def screen_candidates(conn, t_day: str, t_prev: str):
    """筛选某交易日T的候选股"""
    if not t_prev:
        return []

    prev_data = fetch_day_data(conn, t_prev)
    today_data = fetch_day_data(conn, t_day)
    if not prev_data or not today_data:
        return []

    candidates = []
    for code, prev in prev_data.items():
        # 1) 昨日跌停判定
        prev_close = prev.get('close')
        prev_preclose = prev.get('preclose')
        if not is_limit_down(prev_close, prev_preclose, code):
            continue

        # 2) 今日数据存在
        today = today_data.get(code)
        if today is None:
            continue

        # 3) 排除ST
        if today.get('isST') == 1:
            continue

        # 4) 今日高开判定
        today_open = today.get('open')
        today_preclose = today.get('preclose')
        if today_open is None or today_preclose is None or today_preclose == 0:
            continue
        gap_up_pct = (float(today_open) - float(today_preclose)) / float(today_preclose) * 100
        if gap_up_pct < GAP_UP_THRESHOLD:
            continue

        # 5) 排除一字涨停
        if is_one_word_limit_up(today_open, today.get('high'), today.get('low'),
                                today.get('close'), today_preclose, code):
            continue

        # 6) 排除一字跌停
        if is_one_word_limit_down(today_open, today.get('high'), today.get('low'),
                                  today.get('close'), today_preclose, code):
            continue

        ratio = get_ratio(code)
        limit_down_price = calc_limit_down(float(prev_preclose), ratio)
        prev_drop_pct = (float(prev_close) / float(prev_preclose) - 1) * 100

        # hour1数据用于h2确认买入判断
        h1_open = today.get('hour1_open')
        h1_close = today.get('hour1_close')
        h2_open = today.get('hour2_open')
        h1_positive = (h1_close is not None and h1_open is not None
                       and float(h1_close) > float(h1_open))  # hour1收阳

        candidates.append({
            'code': code,
            'prev_close': float(prev_close),
            'prev_preclose': float(prev_preclose),
            'prev_drop_pct': prev_drop_pct,
            'limit_down_price': limit_down_price,
            'today_open': float(today_open),
            'today_preclose': float(today_preclose),
            'gap_up_pct': gap_up_pct,
            'today_high': float(today.get('high') or 0),
            'today_low': float(today.get('low') or 0),
            'today_close': float(today.get('close') or 0),
            'today_turn': today.get('turn'),
            'hour1_open': float(h1_open) if h1_open is not None else None,
            'hour1_close': float(h1_close) if h1_close is not None else None,
            'hour1_positive': h1_positive,
            'hour2_open': float(h2_open) if h2_open is not None else None,
        })

    # 按高开幅度降序排列
    candidates.sort(key=lambda x: x['gap_up_pct'], reverse=True)
    return candidates


# ============================================================
# 输出格式化
# ============================================================

def fmt(v, width=8, prec=2):
    if v is None:
        return '-'.center(width)
    try:
        return f"{float(v):>{width}.{prec}f}"
    except (TypeError, ValueError):
        return str(v).rjust(width)


def fmt_pct(v, width=8, prec=2):
    if v is None:
        return '-'.center(width)
    try:
        f = float(v)
        sign = '+' if f >= 0 else ''
        return f"{sign}{f:.{prec}f}%".rjust(width)
    except (TypeError, ValueError):
        return str(v).rjust(width)


def print_candidate(conn, t_day, cand, buy_price):
    """打印候选股明细"""
    code = cand['code']
    h1_tag = "✓收阳" if cand['hour1_positive'] else "✗收阴"
    print(f"\n--- {code} ---")
    print(f"  昨日跌停: close={cand['prev_close']:.2f} preclose={cand['prev_preclose']:.2f} "
          f"跌幅{cand['prev_drop_pct']:+.2f}% (跌停价={cand['limit_down_price']:.2f})")
    print(f"  今日高开: open={cand['today_open']:.2f} preclose={cand['today_preclose']:.2f} "
          f"高开{cand['gap_up_pct']:+.2f}%")
    h1o_str = f"{cand['hour1_open']:.2f}" if cand['hour1_open'] else '-'
    h1c_str = f"{cand['hour1_close']:.2f}" if cand['hour1_close'] else '-'
    h2o_str = f"{cand['hour2_open']:.2f}" if cand['hour2_open'] else '-'
    print(f"  Hour1: open={h1o_str} close={h1c_str} [{h1_tag}]  Hour2 open={h2o_str}")
    print()

    # 获取窗口日期和数据
    win_dates = get_window_dates(conn, t_day, WINDOW_BEFORE, WINDOW_AFTER)
    stock_data = fetch_stock_window(conn, code, win_dates)

    print(f"  前{WINDOW_BEFORE}日+后{WINDOW_AFTER}日 hour级明细:")
    header = "  日期       | hour | open   | high   | low    | close  | vs买入价"
    print(header)
    print("  " + "-" * (len(header) - 2))

    # 收集后5日数据用于统计
    after_days_highs = []
    after_days_lows = []

    for d in win_dates:
        row = stock_data.get(d)
        if row is None:
            continue

        is_buy_day = (d == t_day)
        marker = " ← 买入日" if is_buy_day else ""

        # 收集后5日极值(不含买入日)
        if d > t_day:
            if row.get('high') is not None:
                after_days_highs.append(float(row['high']))
            if row.get('low') is not None:
                after_days_lows.append(float(row['low']))

        for h in range(1, 5):
            ho = row.get(f'hour{h}_open')
            hh = row.get(f'hour{h}_high')
            hl = row.get(f'hour{h}_low')
            hc = row.get(f'hour{h}_close')
            if all(x is None for x in (ho, hh, hl, hc)):
                continue

            # vs买入价
            vs = ''
            if buy_price and hc is not None and buy_price > 0:
                vs_pct = (float(hc) / buy_price - 1) * 100
                vs = f"{vs_pct:+.1f}%"

            h_marker = marker if h == 1 else ""
            print(f"  {d} | h{h}   | {fmt(ho, 6)} | {fmt(hh, 6)} | {fmt(hl, 6)} | {fmt(hc, 6)} | {vs:>7s}{h_marker}")

    # 关键指标
    h2_buy_price = cand['hour2_open']
    print()
    print(f"  关键指标:")
    print(f"    [H1买入] 买入价(h1 open): {buy_price:.2f}")
    if cand['today_high'] and buy_price > 0:
        print(f"      当日最高: {cand['today_high']:.2f} ({(cand['today_high']/buy_price-1)*100:+.2f}%)")
    if cand['today_close'] and buy_price > 0:
        print(f"      当日收盘: {cand['today_close']:.2f} ({(cand['today_close']/buy_price-1)*100:+.2f}%)")
    if after_days_highs and buy_price > 0:
        max5 = max(after_days_highs)
        print(f"      5日内最高: {max5:.2f} ({(max5/buy_price-1)*100:+.2f}%)")
    if after_days_lows and buy_price > 0:
        min5 = min(after_days_lows)
        print(f"      5日内最低: {min5:.2f} ({(min5/buy_price-1)*100:+.2f}%)")

    if h2_buy_price and h2_buy_price > 0 and cand['hour1_positive']:
        print(f"    [H2确认买入] 买入价(h2 open): {h2_buy_price:.2f} (hour1收阳确认)")
        if cand['today_high'] and h2_buy_price > 0:
            print(f"      当日最高: {cand['today_high']:.2f} ({(cand['today_high']/h2_buy_price-1)*100:+.2f}%)")
        if cand['today_close'] and h2_buy_price > 0:
            print(f"      当日收盘: {cand['today_close']:.2f} ({(cand['today_close']/h2_buy_price-1)*100:+.2f}%)")
        if after_days_highs:
            print(f"      5日内最高: {max(after_days_highs):.2f} ({(max(after_days_highs)/h2_buy_price-1)*100:+.2f}%)")
        if after_days_lows:
            print(f"      5日内最低: {min(after_days_lows):.2f} ({(min(after_days_lows)/h2_buy_price-1)*100:+.2f}%)")

    return {
        'buy_price': buy_price,
        'h2_buy_price': h2_buy_price if cand['hour1_positive'] else None,
        'hour1_positive': cand['hour1_positive'],
        'today_high': cand['today_high'],
        'today_close': cand['today_close'],
        'after_max': max(after_days_highs) if after_days_highs else None,
        'after_min': min(after_days_lows) if after_days_lows else None,
    }


# ============================================================
# 主流程
# ============================================================

def main():
    print(f"=" * 60)
    print(f"跌停次日高开买入策略 - 候选股研究")
    print(f"目标月份: {TARGET_MONTH}")
    print(f"高开阈值: >= {GAP_UP_THRESHOLD}%")
    print(f"数据库: {DB_PATH}")
    print(f"=" * 60)

    conn = sqlite3.connect(DB_PATH)
    try:
        days = get_trading_days(conn, TARGET_MONTH)
        if not days:
            print(f"[警告] {TARGET_MONTH} 无交易日数据。")
            return
        print(f"\n交易日: {TARGET_MONTH} 共 {len(days)} 个交易日: {days[0]} ~ {days[-1]}")

        all_stats = []  # 收集所有候选股的统计数据

        for t_day in days:
            t_prev = get_prev_trading_day(conn, t_day)
            cands = screen_candidates(conn, t_day, t_prev)

            print(f"\n{'=' * 10} {t_day} {'=' * 10}")
            print(f"候选股数量: {len(cands)}")

            for cand in cands:
                buy_price = cand['today_open']
                stats = print_candidate(conn, t_day, cand, buy_price)
                all_stats.append(stats)

        # ============================================================
        # 月度统计汇总
        # ============================================================
        print(f"\n\n{'=' * 10} 月度统计汇总 {'=' * 10}")
        print(f"总候选股数: {len(all_stats)}")
        h2_count = sum(1 for s in all_stats if s.get('hour1_positive'))
        print(f"Hour1收阳确认(H2可买入)数: {h2_count}")

        if all_stats:
            def calc_strategy_stats(stats_list, get_buy_price_fn, label):
                """通用统计函数"""
                day_returns = []
                day_max_returns = []
                five_day_max_returns = []
                five_day_min_returns = []
                day_positive_count = 0
                valid_count = 0

                for s in stats_list:
                    bp = get_buy_price_fn(s)
                    if bp is None or bp == 0:
                        continue
                    valid_count += 1

                    if s['today_close'] and s['today_close'] > 0:
                        ret = (s['today_close'] / bp - 1) * 100
                        day_returns.append(ret)
                        if ret > 0:
                            day_positive_count += 1

                    if s['today_high'] and s['today_high'] > 0:
                        day_max_returns.append((s['today_high'] / bp - 1) * 100)

                    if s['after_max'] and s['after_max'] > 0:
                        five_day_max_returns.append((s['after_max'] / bp - 1) * 100)

                    if s['after_min'] and s['after_min'] > 0:
                        five_day_min_returns.append((s['after_min'] / bp - 1) * 100)

                print(f"\n{label} (样本数: {valid_count})")
                if day_returns:
                    avg_day = sum(day_returns) / len(day_returns)
                    print(f"  - 当日收益(close/buy-1)均值: {avg_day:+.2f}%")
                if day_max_returns:
                    avg_max = sum(day_max_returns) / len(day_max_returns)
                    print(f"  - 当日最高涨(high/buy-1)均值: {avg_max:+.2f}%")
                if five_day_max_returns:
                    avg_5max = sum(five_day_max_returns) / len(five_day_max_returns)
                    print(f"  - 5日内最高涨均值: {avg_5max:+.2f}%")
                if five_day_min_returns:
                    avg_5min = sum(five_day_min_returns) / len(five_day_min_returns)
                    print(f"  - 5日内最大跌均值: {avg_5min:+.2f}%")
                if day_returns:
                    positive_ratio = day_positive_count / len(day_returns) * 100
                    print(f"  - 当日收阳占比: {positive_ratio:.1f}%")
                if five_day_max_returns:
                    gt5_count = sum(1 for r in five_day_max_returns if r >= 5)
                    print(f"  - 5日内涨≥5%占比: {gt5_count/len(five_day_max_returns)*100:.1f}%")
                if five_day_min_returns:
                    lt5_count = sum(1 for r in five_day_min_returns if r <= -5)
                    print(f"  - 5日内跌≥5%占比: {lt5_count/len(five_day_min_returns)*100:.1f}%")

            # 策略A: H1 open买入 (全部候选股)
            calc_strategy_stats(
                all_stats,
                lambda s: s['buy_price'],
                "策略A: H1 open买入 (昨日跌停+今日高开, 无确认)"
            )

            # 策略B: H2 open买入 (hour1收阳确认)
            h2_stats = [s for s in all_stats if s.get('hour1_positive') and s.get('h2_buy_price')]
            calc_strategy_stats(
                h2_stats,
                lambda s: s['h2_buy_price'],
                "策略B: H2 open买入 (hour1收阳确认, 过滤高开低走)"
            )

            # 策略对比
            h1_not_positive = [s for s in all_stats if not s.get('hour1_positive')]
            if h1_not_positive:
                calc_strategy_stats(
                    h1_not_positive,
                    lambda s: s['buy_price'],
                    "参考: H1买入但hour1收阴的股票(被H2策略过滤掉的)"
                )

        print(f"\n{'=' * 60}")
        print("研究完成。")

    finally:
        conn.close()


if __name__ == '__main__':
    main()
