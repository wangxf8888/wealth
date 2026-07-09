#!/usr/bin/env python3
"""跌停反弹策略 Phase-3a: 全年验证

用法:
    python3 scripts/strategy_limitdown_rebound_phase3.py [YEAR]

示例:
    python3 scripts/strategy_limitdown_rebound_phase3.py 2025

策略规则(已确定):
    - T-1日跌停 + T日hour1_close_rate >= 0% + T日turn > 10%
    - 买入: T+1日 hour1_open
    - 止损: -7%, 止盈: +10%, 最大持有到 T+3日 hour4_open
    - T+1买入合规, T+2起可卖出
"""

import sqlite3
import sys
import statistics

# ============================================================
# 配置区
# ============================================================
DB_PATH = '/home/AIWealth/data/stocks.db'
TARGET_YEAR = sys.argv[1] if len(sys.argv) > 1 else '2025'

# 策略参数
STOP_LOSS = -7.0      # 止损 -7%
TAKE_PROFIT = 10.0    # 止盈 +10%
MAX_HOLD_DAY_OFFSET = 3  # 最大持有到T+3
MAX_HOLD_HOUR = 4        # T+3的hour4_open卖出
POSITION_COUNT = 3       # 仓位数

# 条件过滤
MIN_HOUR1_CLOSE_RATE = 0.0   # T日hour1_close_rate >= 0%
MIN_TURN = 10.0              # T日换手率 > 10%


# ============================================================
# 跌停判定
# ============================================================

def get_limit_down_threshold(code: str) -> float:
    """主板0.90, 创业板/科创板0.80"""
    if code.startswith('sz.30') or code.startswith('sh.68'):
        return 0.80
    return 0.90


def is_limit_down(close, preclose, code: str) -> bool:
    if preclose is None or preclose == 0:
        return False
    try:
        ratio = round(float(close) / float(preclose), 2)
    except (TypeError, ValueError, ZeroDivisionError):
        return False
    return ratio <= get_limit_down_threshold(code)


def is_main_board_or_gem_or_star(code: str) -> bool:
    return (code.startswith('sh.60') or code.startswith('sz.00')
            or code.startswith('sz.30') or code.startswith('sh.68'))


def is_one_word_board(o, h, l, c) -> bool:
    if any(x is None for x in (o, h, l, c)):
        return False
    return float(o) == float(h) == float(l) == float(c)


# ============================================================
# 数据访问
# ============================================================

def get_trading_days_range(conn, start_date: str, end_date: str):
    """获取日期范围内的交易日列表"""
    cur = conn.execute(
        "SELECT DISTINCT date FROM stock_kline WHERE date >= ? AND date <= ? ORDER BY date ASC",
        (start_date, end_date),
    )
    return [r[0] for r in cur.fetchall()]


def get_prev_trading_day(conn, day: str):
    cur = conn.execute(
        "SELECT MAX(date) FROM stock_kline WHERE date < ?", (day,),
    )
    row = cur.fetchone()
    return row[0] if row and row[0] else None


def get_next_trading_days(conn, day: str, n: int):
    """获取day之后n个交易日"""
    cur = conn.execute(
        "SELECT DISTINCT date FROM stock_kline WHERE date > ? ORDER BY date ASC LIMIT ?",
        (day, n),
    )
    return [r[0] for r in cur.fetchall()]


def fetch_day_rows(conn, day: str):
    """获取某天全市场行情"""
    cur = conn.execute(
        """SELECT date, code, code_name, preclose, open, high, low, close,
               close_rate, open_rate, volume, amount, turn, isST,
               hour1_open, hour1_close, hour1_close_rate
        FROM stock_kline WHERE date = ?""",
        (day,),
    )
    cols = [d[0] for d in cur.description]
    return {row[1]: dict(zip(cols, row)) for row in cur.fetchall()}


def fetch_stock_hour_data(conn, code: str, dates):
    """获取股票在给定日期的hour级open数据"""
    if not dates:
        return {}
    placeholders = ','.join('?' * len(dates))
    sql = f"""
        SELECT date, hour1_open, hour2_open, hour3_open, hour4_open
        FROM stock_kline
        WHERE code = ? AND date IN ({placeholders})
        ORDER BY date ASC
    """
    cur = conn.execute(sql, [code] + list(dates))
    cols = [d[0] for d in cur.description]
    return {row[0]: dict(zip(cols, row)) for row in cur.fetchall()}


# ============================================================
# 候选股筛选 (含条件过滤)
# ============================================================

def screen_candidates(conn, t_day: str, t_prev: str):
    """筛选满足所有条件的候选股"""
    if not t_prev:
        return []
    prev_rows = fetch_day_rows(conn, t_prev)
    if not prev_rows:
        return []
    today_rows = fetch_day_rows(conn, t_day)
    if not today_rows:
        return []

    candidates = []
    for code, prev in prev_rows.items():
        # 板块过滤: 仅主板/创业板/科创板
        if not is_main_board_or_gem_or_star(code):
            continue
        # T-1日跌停判定
        if not is_limit_down(prev.get('close'), prev.get('preclose'), code):
            continue
        # T日数据
        today = today_rows.get(code)
        if today is None:
            continue
        # ST过滤
        if today.get('isST') == 1:
            continue
        cn = (today.get('code_name') or '')
        if 'ST' in cn.upper():
            continue
        # 一字板过滤
        if is_one_word_board(today.get('open'), today.get('high'),
                            today.get('low'), today.get('close')):
            continue
        # T日开盘未跌停封死
        t_pre = today.get('preclose')
        t_open = today.get('open')
        if t_pre is None or t_pre == 0 or t_open is None:
            continue
        thr = get_limit_down_threshold(code)
        if round(float(t_open) / float(t_pre), 2) <= thr:
            continue

        # 条件过滤: hour1_close_rate >= 0%
        h1_cr = today.get('hour1_close_rate')
        if h1_cr is None or float(h1_cr) < MIN_HOUR1_CLOSE_RATE:
            continue

        # 条件过滤: 换手率 > 10%
        turn = today.get('turn')
        if turn is None or float(turn) <= MIN_TURN:
            continue

        candidates.append({
            'code': code,
            'code_name': cn,
            't_day': t_day,
            'today_turn': float(turn),
            'hour1_close_rate': float(h1_cr),
        })
    return candidates


# ============================================================
# 交易模拟 (逐hour止盈止损)
# ============================================================

def simulate_trade(conn, cand, t_day: str):
    """模拟单笔交易，返回交易记录或None"""
    code = cand['code']
    # 获取T+1, T+2, T+3
    next_days = get_next_trading_days(conn, t_day, 3)
    if len(next_days) < 1:
        return None  # 至少需要T+1

    buy_day = next_days[0]  # T+1
    all_days = next_days  # T+1, T+2, T+3
    stock_data = fetch_stock_hour_data(conn, code, all_days)

    # 买入: T+1日 hour1_open
    buy_day_data = stock_data.get(buy_day)
    if buy_day_data is None:
        return None
    buy_price = buy_day_data.get('hour1_open')
    if buy_price is None or float(buy_price) <= 0:
        return None
    buy_price = float(buy_price)

    # 卖出: 从T+2日hour1开始逐hour检查open
    sell_price = None
    sell_day = None
    sell_hour = None
    sell_reason = '默认'

    # 构建可卖出的hour序列 (T+2起)
    sell_slots = []
    for i, d in enumerate(next_days):
        if i == 0:
            continue  # T+1买入日不可卖(T+1合规)
        day_data = stock_data.get(d)
        if day_data is None:
            continue
        for h in range(1, 5):
            price = day_data.get(f'hour{h}_open')
            if price is not None and float(price) > 0:
                sell_slots.append((d, h, float(price), i + 1))  # day_offset from T

    # 逐slot检查止盈止损
    for d, h, price, day_off in sell_slots:
        ret_pct = (price - buy_price) / buy_price * 100.0
        if ret_pct <= STOP_LOSS:
            sell_price = price
            sell_day = d
            sell_hour = h
            sell_reason = f'止损({ret_pct:.1f}%)'
            break
        elif ret_pct >= TAKE_PROFIT:
            sell_price = price
            sell_day = d
            sell_hour = h
            sell_reason = f'止盈({ret_pct:.1f}%)'
            break
        # 检查是否到达最大持有期限 T+3 hour4_open
        if day_off >= MAX_HOLD_DAY_OFFSET and h >= MAX_HOLD_HOUR:
            sell_price = price
            sell_day = d
            sell_hour = h
            sell_reason = f'到期({ret_pct:.1f}%)'
            break

    # 如果上面循环没触发（可能数据不全），用最后一个有效价格
    if sell_price is None and sell_slots:
        d, h, price, day_off = sell_slots[-1]
        sell_price = price
        sell_day = d
        sell_hour = h
        ret_pct = (price - buy_price) / buy_price * 100.0
        sell_reason = f'数据截止({ret_pct:.1f}%)'

    if sell_price is None:
        return None

    ret_pct = (sell_price - buy_price) / buy_price * 100.0
    return {
        'code': code,
        'code_name': cand['code_name'],
        't_day': t_day,
        'buy_price': buy_price,
        'buy_day': buy_day,
        'sell_price': sell_price,
        'sell_day': sell_day,
        'sell_hour': sell_hour,
        'return_pct': ret_pct,
        'sell_reason': sell_reason,
        'turn': cand['today_turn'],
        'h1_cr': cand['hour1_close_rate'],
    }


# ============================================================
# 全年验证主逻辑
# ============================================================

def run_year_validation(conn, year: str):
    """运行全年验证"""
    print(f"\n============ 跌停反弹策略 Phase-3a 全年验证 ============")
    print(f"年份: {year}, 参数: 止损{STOP_LOSS}% 止盈+{TAKE_PROFIT}% 持有上限T+{MAX_HOLD_DAY_OFFSET}")
    print(f"条件: T-1跌停 + T日h1_cr>={MIN_HOUR1_CLOSE_RATE}% + T日turn>{MIN_TURN}%")
    print(f"买入: T+1日hour1_open, 卖出: T+2起逐hour_open检查")

    all_trades = []
    monthly_stats = []

    for month_num in range(1, 13):
        month_str = f"{year}-{month_num:02d}"
        # 获取本月交易日
        start_date = f"{year}-{month_num:02d}-01"
        if month_num == 12:
            end_date = f"{year}-12-31"
        else:
            end_date = f"{year}-{month_num + 1:02d}-01"
        
        days = get_trading_days_range(conn, start_date, end_date)
        if not days:
            monthly_stats.append({
                'month': month_str, 'candidates': 0, 'trades': 0,
                'win_rate': 0, 'avg_ret': 0, 'month_ret': 0,
            })
            continue

        month_trades = []
        month_candidates = 0

        for t_day in days:
            t_prev = get_prev_trading_day(conn, t_day)
            cands = screen_candidates(conn, t_day, t_prev)
            month_candidates += len(cands)

            for c in cands:
                trade = simulate_trade(conn, c, t_day)
                if trade:
                    month_trades.append(trade)

        # 月度统计
        n_trades = len(month_trades)
        if n_trades > 0:
            returns = [t['return_pct'] for t in month_trades]
            win_count = sum(1 for r in returns if r > 0)
            win_rate = win_count / n_trades * 100
            avg_ret = statistics.mean(returns)
            total_ret = sum(returns)
            # 月化: 假设N=3仓位, 每次投入1/N, 月收益=所有交易收益之和/N
            month_ret = total_ret / POSITION_COUNT
        else:
            win_rate = 0
            avg_ret = 0
            month_ret = 0

        monthly_stats.append({
            'month': month_str,
            'candidates': month_candidates,
            'trades': n_trades,
            'win_rate': win_rate,
            'avg_ret': avg_ret,
            'month_ret': month_ret,
        })
        all_trades.extend(month_trades)

        print(f"  [{month_str}] 候选:{month_candidates:3d} 交易:{n_trades:3d} "
              f"胜率:{win_rate:5.1f}% 均收益:{avg_ret:+6.2f}% 月化:{month_ret:+6.1f}%")

    # ============ 输出汇总 ============
    print(f"\n{'='*60}")
    print(f"--- 月度明细 ---")
    print(f"{'月份':<8} | {'候选数':>5} | {'交易数':>5} | {'胜率':>6} | {'均收益':>7} | {'月化':>7} | {'累计':>7}")
    print('-' * 68)

    cumulative = 0
    for ms in monthly_stats:
        cumulative += ms['month_ret']
        print(f"{ms['month']:<8} | {ms['candidates']:>5d} | {ms['trades']:>5d} | "
              f"{ms['win_rate']:>5.1f}% | {ms['avg_ret']:>+6.2f}% | "
              f"{ms['month_ret']:>+6.1f}% | {cumulative:>+6.1f}%")

    # 全年汇总
    print(f"\n--- 全年汇总 ---")
    total_trades = len(all_trades)
    if total_trades > 0:
        all_returns = [t['return_pct'] for t in all_trades]
        total_win = sum(1 for r in all_returns if r > 0)
        total_win_rate = total_win / total_trades * 100
        total_avg_ret = statistics.mean(all_returns)
        wins = [r for r in all_returns if r > 0]
        losses = [r for r in all_returns if r <= 0]
        avg_win = statistics.mean(wins) if wins else 0
        avg_loss = abs(statistics.mean(losses)) if losses else 0.001
        pnl_ratio = avg_win / avg_loss if avg_loss > 0 else 999

        # 年化(复利): 按月复利
        monthly_rets = [ms['month_ret'] / 100 for ms in monthly_stats]
        compound = 1.0
        for mr in monthly_rets:
            compound *= (1 + mr)
        annual_compound = (compound - 1) * 100

        # 或用平均月化复利
        valid_months = [ms for ms in monthly_stats if ms['trades'] > 0]
        if valid_months:
            avg_monthly = statistics.mean([ms['month_ret'] for ms in valid_months])
            annual_from_avg = ((1 + avg_monthly / 100) ** 12 - 1) * 100
        else:
            avg_monthly = 0
            annual_from_avg = 0

        # 最大月亏损
        month_rets_list = [ms['month_ret'] for ms in monthly_stats]
        max_month_loss = min(month_rets_list) if month_rets_list else 0

        # 最大连亏
        max_consec_loss = 0
        curr_consec = 0
        for t in all_trades:
            if t['return_pct'] <= 0:
                curr_consec += 1
                max_consec_loss = max(max_consec_loss, curr_consec)
            else:
                curr_consec = 0

        print(f"总交易数: {total_trades}")
        print(f"总胜率: {total_win_rate:.1f}%")
        print(f"平均每笔收益: {total_avg_ret:+.2f}%")
        print(f"盈亏比: {pnl_ratio:.2f}")
        print(f"平均盈利: +{avg_win:.2f}%, 平均亏损: -{abs(avg_loss):.2f}%")
        print(f"最大月亏损: {max_month_loss:+.1f}%")
        print(f"最大连亏次数: {max_consec_loss}")
        print(f"平均月化(有交易月): {avg_monthly:+.1f}%")
        print(f"年化收益(实际复利): {annual_compound:+.1f}%")
        print(f"年化收益(月均复利): {annual_from_avg:+.1f}%")

        # 简单年化(累加)
        simple_annual = sum(month_rets_list)
        print(f"年化收益(简单累加): {simple_annual:+.1f}%")
    else:
        print("无交易记录")
        annual_compound = 0

    # 逐笔交易明细(前50笔)
    print(f"\n--- 逐笔交易明细(前50笔) ---")
    print(f"{'日期':<11} | {'代码':<10} | {'名称':<8} | {'买入价':>7} | {'买入日':<11} | "
          f"{'卖出价':>7} | {'卖出日':<11} | {'收益%':>7} | {'原因'}")
    print('-' * 110)
    for t in all_trades[:50]:
        print(f"{t['t_day']:<11} | {t['code']:<10} | {t['code_name']:<8} | "
              f"{t['buy_price']:>7.2f} | {t['buy_day']:<11} | "
              f"{t['sell_price']:>7.2f} | {t['sell_day']:<11} | "
              f"{t['return_pct']:>+6.2f}% | {t['sell_reason']}")

    # 判断是否达标
    print(f"\n{'='*60}")
    if annual_compound >= 100:
        print(f"*** 达标! 年化{annual_compound:+.1f}% >= 100% ***")
        return True
    else:
        print(f"未达标: 年化{annual_compound:+.1f}% < 100%")
        return False


# ============================================================
# 主入口
# ============================================================

def main():
    print(f"数据库: {DB_PATH}")
    conn = sqlite3.connect(DB_PATH)
    try:
        # 运行目标年份
        reached = run_year_validation(conn, TARGET_YEAR)

        # 如果达标，额外跑对照年
        if reached:
            contrast_year = str(int(TARGET_YEAR) - 1)
            print(f"\n\n{'#'*60}")
            print(f"# 达标! 额外运行 {contrast_year} 年对照验证")
            print(f"{'#'*60}")
            run_year_validation(conn, contrast_year)
        else:
            # 即使未达标，也跑2024对照看趋势
            contrast_year = str(int(TARGET_YEAR) - 1)
            print(f"\n\n{'#'*60}")
            print(f"# 附加运行 {contrast_year} 年对照验证")
            print(f"{'#'*60}")
            run_year_validation(conn, contrast_year)

    finally:
        conn.close()


if __name__ == '__main__':
    main()
