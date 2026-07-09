#!/usr/bin/env python3
"""跌停反弹增强版 Phase-3b: 多年验证(2023-2025)

用法:
    python3 scripts/strategy_limitdown_rebound_phase3_v2.py

策略规则:
    - T-1日跌停 + T日hour1_close_rate >= 0% + T日turn > 10%
    - 买入: T+1日 hour1_open
    - 6组增强参数对比(含大盘过滤/trailing止盈)
    - T+1买入合规, T+2起可卖出
"""

import sqlite3
import statistics
from collections import defaultdict

# ============================================================
# 配置区
# ============================================================
DB_PATH = '/home/AIWealth/data/stocks.db'
YEARS = ['2023', '2024', '2025']
POSITION_COUNT = 3  # 仓位数

# 条件过滤(基础不变)
MIN_HOUR1_CLOSE_RATE = 0.0  # T日hour1_close_rate >= 0%
MIN_TURN = 10.0             # T日换手率 > 10%

# 参数矩阵定义
# (版本名, red_ratio_threshold, stop_loss%, take_profit%, trailing_pct, max_hold_day_offset, max_hold_hour)
# trailing_pct: None=固定止盈, float=trailing回撤比例
PARAM_VERSIONS = [
    ('V1', None,  -7.0, 10.0, None,  3, 4),  # 基线: 无过滤, -7%止损, +10%止盈, T+3_h4
    ('V2', 45.0,  -7.0, 10.0, None,  3, 4),  # red_ratio>45%, -7%, +10%, T+3_h4
    ('V3', 45.0,  -5.0,  8.0, None,  3, 4),  # red_ratio>45%, -5%, +8%, T+3_h4
    ('V4', 45.0,  -5.0, None, 0.03,  3, 4),  # red_ratio>45%, -5%, trailing3%, T+3_h4
    ('V5', 50.0,  -5.0,  8.0, None,  2, 4),  # red_ratio>50%, -5%, +8%, T+2_h4
    ('V6', 45.0,  -3.0,  6.0, None,  2, 4),  # red_ratio>45%, -3%, +6%, T+2_h4
]


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
    cur = conn.execute(
        "SELECT DISTINCT date FROM stock_kline WHERE date > ? ORDER BY date ASC LIMIT ?",
        (day, n),
    )
    return [r[0] for r in cur.fetchall()]


def get_red_ratio(conn, day: str):
    """获取指定日期的大盘red_ratio"""
    cur = conn.execute(
        "SELECT red_ratio FROM index_kline WHERE code='sh.000001' AND date=?", (day,),
    )
    row = cur.fetchone()
    if row and row[0] is not None:
        return float(row[0])
    return None


def fetch_day_rows(conn, day: str):
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
# 候选股筛选
# ============================================================

def screen_candidates(conn, t_day: str, t_prev: str):
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
        if not is_main_board_or_gem_or_star(code):
            continue
        if not is_limit_down(prev.get('close'), prev.get('preclose'), code):
            continue
        today = today_rows.get(code)
        if today is None:
            continue
        if today.get('isST') == 1:
            continue
        cn = (today.get('code_name') or '')
        if 'ST' in cn.upper():
            continue
        if is_one_word_board(today.get('open'), today.get('high'),
                            today.get('low'), today.get('close')):
            continue
        t_pre = today.get('preclose')
        t_open = today.get('open')
        if t_pre is None or t_pre == 0 or t_open is None:
            continue
        thr = get_limit_down_threshold(code)
        if round(float(t_open) / float(t_pre), 2) <= thr:
            continue
        h1_cr = today.get('hour1_close_rate')
        if h1_cr is None or float(h1_cr) < MIN_HOUR1_CLOSE_RATE:
            continue
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
# 交易模拟(支持6种参数)
# ============================================================

def simulate_trade(conn, cand, t_day: str, stop_loss, take_profit, trailing_pct,
                   max_hold_day_offset, max_hold_hour):
    """模拟单笔交易
    trailing_pct: None=固定止盈, float=trailing回撤比例(如0.03表示从peak回落3%卖出)
    """
    code = cand['code']
    next_days = get_next_trading_days(conn, t_day, max_hold_day_offset)
    if len(next_days) < 1:
        return None

    buy_day = next_days[0]  # T+1
    stock_data = fetch_stock_hour_data(conn, code, next_days)

    buy_day_data = stock_data.get(buy_day)
    if buy_day_data is None:
        return None
    buy_price = buy_day_data.get('hour1_open')
    if buy_price is None or float(buy_price) <= 0:
        return None
    buy_price = float(buy_price)

    # 构建可卖出的hour序列 (T+2起, day_offset从T算)
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

    if not sell_slots:
        return None

    sell_price = None
    sell_day = None
    sell_hour = None
    sell_reason = '默认'

    # Trailing止盈: peak从买入价开始追踪
    peak_price = buy_price

    for d, h, price, day_off in sell_slots:
        ret_pct = (price - buy_price) / buy_price * 100.0

        # 更新peak(用于trailing)
        if price > peak_price:
            peak_price = price

        # 止损检查(所有版本都有)
        if ret_pct <= stop_loss:
            sell_price = price
            sell_day = d
            sell_hour = h
            sell_reason = f'止损({ret_pct:.1f}%)'
            break

        # 止盈检查
        if trailing_pct is not None:
            # Trailing止盈: 从peak回落trailing_pct触发
            if peak_price > buy_price:  # peak有上升才触发trailing
                trail_trigger = peak_price * (1 - trailing_pct)
                if price <= trail_trigger:
                    sell_price = price
                    sell_day = d
                    sell_hour = h
                    peak_ret = (peak_price - buy_price) / buy_price * 100.0
                    sell_reason = f'trailing({ret_pct:.1f}%,peak{peak_ret:.1f}%)'
                    break
        else:
            # 固定止盈
            if take_profit is not None and ret_pct >= take_profit:
                sell_price = price
                sell_day = d
                sell_hour = h
                sell_reason = f'止盈({ret_pct:.1f}%)'
                break

        # 最大持有期限
        if day_off >= max_hold_day_offset and h >= max_hold_hour:
            sell_price = price
            sell_day = d
            sell_hour = h
            sell_reason = f'到期({ret_pct:.1f}%)'
            break

    # 兜底
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
    }


# ============================================================
# 年度运行(单版本单年)
# ============================================================

def run_version_year(conn, year: str, version_name: str, red_ratio_thr,
                     stop_loss, take_profit, trailing_pct,
                     max_hold_day_offset, max_hold_hour,
                     red_ratio_cache):
    """运行单个版本单年, 返回所有交易列表"""
    start_date = f"{year}-01-01"
    end_date = f"{year}-12-31"
    days = get_trading_days_range(conn, start_date, end_date)
    if not days:
        return []

    all_trades = []
    for t_day in days:
        t_prev = get_prev_trading_day(conn, t_day)
        if not t_prev:
            continue

        # 大盘过滤: 用T-1日的red_ratio
        if red_ratio_thr is not None:
            if t_prev not in red_ratio_cache:
                red_ratio_cache[t_prev] = get_red_ratio(conn, t_prev)
            rr = red_ratio_cache[t_prev]
            if rr is None or rr <= red_ratio_thr:
                continue

        cands = screen_candidates(conn, t_day, t_prev)
        for c in cands:
            trade = simulate_trade(conn, c, t_day, stop_loss, take_profit,
                                   trailing_pct, max_hold_day_offset, max_hold_hour)
            if trade:
                all_trades.append(trade)

    return all_trades


# ============================================================
# 统计计算
# ============================================================

def calc_stats(trades, year=None):
    """计算交易统计"""
    n = len(trades)
    if n == 0:
        return {
            'trades': 0, 'win_rate': 0, 'avg_ret': 0,
            'annual': 0, 'max_month_loss': 0, 'pnl_ratio': 0,
        }

    returns = [t['return_pct'] for t in trades]
    wins = [r for r in returns if r > 0]
    losses = [r for r in returns if r <= 0]
    win_rate = len(wins) / n * 100
    avg_ret = statistics.mean(returns)
    avg_win = statistics.mean(wins) if wins else 0
    avg_loss = abs(statistics.mean(losses)) if losses else 0.001
    pnl_ratio = avg_win / avg_loss if avg_loss > 0 else 999

    # 按月统计月化
    monthly_rets = defaultdict(float)
    monthly_counts = defaultdict(int)
    for t in trades:
        month = t['buy_day'][:7]
        monthly_rets[month] += t['return_pct']
        monthly_counts[month] += 1

    # 月化 = 该月所有交易收益之和 / N
    month_ret_list = []
    if year:
        for m in range(1, 13):
            mk = f"{year}-{m:02d}"
            month_ret_list.append(monthly_rets.get(mk, 0) / POSITION_COUNT)
    else:
        for mk in sorted(monthly_rets.keys()):
            month_ret_list.append(monthly_rets[mk] / POSITION_COUNT)

    # 年化(复利)
    compound = 1.0
    for mr in month_ret_list:
        compound *= (1 + mr / 100)
    annual = (compound - 1) * 100

    # 最大月亏损
    max_month_loss = min(month_ret_list) if month_ret_list else 0

    return {
        'trades': n,
        'win_rate': win_rate,
        'avg_ret': avg_ret,
        'annual': annual,
        'max_month_loss': max_month_loss,
        'pnl_ratio': pnl_ratio,
        'month_ret_list': month_ret_list,
    }


# ============================================================
# 仓位模拟(N=3, 3年连续)
# ============================================================

def simulate_portfolio(trades_3years):
    """3年连续仓位模拟, 返回每年末净值"""
    # 按buy_day排序
    sorted_trades = sorted(trades_3years, key=lambda t: (t['buy_day'], t.get('sell_day', '')))
    
    capital = 1000000.0  # 初始100万
    yearly_end = {}
    
    # 简化模拟: 按交易顺序, 每笔投入 capital/N
    for t in sorted_trades:
        position_size = capital / POSITION_COUNT
        pnl = position_size * t['return_pct'] / 100.0
        capital += pnl
        if capital < 0:
            capital = 0

    # 实际上需要按年份分段记录
    # 重新跑: 按年份分段
    capital = 1000000.0
    results = []
    
    for year in YEARS:
        year_trades = [t for t in sorted_trades if t['buy_day'].startswith(year)]
        for t in year_trades:
            position_size = capital / POSITION_COUNT
            pnl = position_size * t['return_pct'] / 100.0
            capital += pnl
            if capital < 0:
                capital = 0
        yearly_end[year] = capital
        results.append((year, capital))
    
    return results


# ============================================================
# 月度明细输出
# ============================================================

def print_monthly_detail(trades, years):
    """打印月度明细"""
    print(f"\n=== 最优版本月度明细(3年) ===")
    print(f"{'年月':<8} | {'交易数':>5} | {'胜率':>6} | {'均收益':>7} | {'月累计':>7}")
    print('-' * 50)
    
    cumulative = 0
    for year in years:
        for m in range(1, 13):
            mk = f"{year}-{m:02d}"
            month_trades = [t for t in trades if t['buy_day'].startswith(mk)]
            n = len(month_trades)
            if n > 0:
                rets = [t['return_pct'] for t in month_trades]
                wr = sum(1 for r in rets if r > 0) / n * 100
                avg = statistics.mean(rets)
                month_ret = sum(rets) / POSITION_COUNT
            else:
                wr = 0
                avg = 0
                month_ret = 0
            cumulative += month_ret
            print(f"{mk:<8} | {n:>5d} | {wr:>5.1f}% | {avg:>+6.2f}% | {cumulative:>+6.1f}%")


# ============================================================
# 主流程
# ============================================================

def main():
    print("============ 跌停反弹增强版 Phase-3b 多年验证 ============")
    print(f"数据库: {DB_PATH}")
    print(f"验证年份: {', '.join(YEARS)}")
    print(f"参数版本: {len(PARAM_VERSIONS)}组")
    print(f"基础条件: T-1跌停 + T日h1_cr>=0% + T日turn>10%")
    print(f"买入: T+1日hour1_open, 卖出: T+2起逐hour_open检查")
    print()

    conn = sqlite3.connect(DB_PATH)
    red_ratio_cache = {}  # 缓存red_ratio查询

    # 存储所有版本所有年的结果
    all_results = {}  # {(version, year): {'trades': [...], 'stats': {...}}}

    try:
        for v_name, rr_thr, sl, tp, trail, max_day, max_hour in PARAM_VERSIONS:
            desc = f"大盘>{rr_thr}%" if rr_thr else "无过滤"
            tp_desc = f"trailing{int(trail*100)}%" if trail else f"+{tp}%"
            print(f"--- 运行 {v_name}: {desc}, 止损{sl}%, 止盈{tp_desc}, 持有T+{max_day}_h{max_hour} ---")

            for year in YEARS:
                trades = run_version_year(conn, year, v_name, rr_thr,
                                          sl, tp, trail, max_day, max_hour,
                                          red_ratio_cache)
                stats = calc_stats(trades, year)
                all_results[(v_name, year)] = {'trades': trades, 'stats': stats}
                print(f"  {year}: 交易{stats['trades']:>4d}笔, "
                      f"胜率{stats['win_rate']:>5.1f}%, "
                      f"均收益{stats['avg_ret']:>+6.2f}%, "
                      f"年化{stats['annual']:>+7.1f}%")
            print()

        # ============ 输出汇总 ============
        print(f"\n{'='*80}")
        print(f"=== 参数组合三年对比 ===")
        print(f"{'版本':<4} | {'年份':<5} | {'交易数':>5} | {'胜率':>6} | "
              f"{'均收益':>7} | {'年化(N=3)':>10} | {'最大月亏':>7} | {'盈亏比':>5}")
        print('-' * 75)

        for v_name, _, _, _, _, _, _ in PARAM_VERSIONS:
            for year in YEARS:
                s = all_results[(v_name, year)]['stats']
                print(f"{v_name:<4} | {year:<5} | {s['trades']:>5d} | "
                      f"{s['win_rate']:>5.1f}% | {s['avg_ret']:>+6.2f}% | "
                      f"{s['annual']:>+9.1f}% | {s['max_month_loss']:>+6.1f}% | "
                      f"{s['pnl_ratio']:>5.2f}")

        # === 各版本三年综合 ===
        print(f"\n=== 各版本三年综合 ===")
        print(f"{'版本':<4} | {'3年总交易':>7} | {'3年胜率':>6} | {'3年均收益':>8} | "
              f"{'3年年化':>8} | {'最差年':>10} | {'最佳年':>10}")
        print('-' * 80)

        best_version = None
        best_composite = -9999

        for v_name, _, _, _, _, _, _ in PARAM_VERSIONS:
            total_trades = 0
            total_wins = 0
            total_returns = []
            year_annuals = {}

            for year in YEARS:
                s = all_results[(v_name, year)]['stats']
                trades = all_results[(v_name, year)]['trades']
                total_trades += s['trades']
                total_wins += sum(1 for t in trades if t['return_pct'] > 0)
                total_returns.extend([t['return_pct'] for t in trades])
                year_annuals[year] = s['annual']

            if total_trades > 0:
                wr_3y = total_wins / total_trades * 100
                avg_3y = statistics.mean(total_returns)
            else:
                wr_3y = 0
                avg_3y = 0

            # 3年复合年化
            compound_3y = 1.0
            for year in YEARS:
                compound_3y *= (1 + year_annuals[year] / 100)
            # 复合年化 = compound^(1/3) - 1
            if compound_3y > 0:
                annual_3y = (compound_3y ** (1.0/3) - 1) * 100
            else:
                annual_3y = -100

            worst_year = min(year_annuals.items(), key=lambda x: x[1])
            best_year = max(year_annuals.items(), key=lambda x: x[1])

            print(f"{v_name:<4} | {total_trades:>7d} | {wr_3y:>5.1f}% | "
                  f"{avg_3y:>+7.2f}% | {annual_3y:>+7.1f}% | "
                  f"{worst_year[0]}({worst_year[1]:+.0f}%) | "
                  f"{best_year[0]}({best_year[1]:+.0f}%)")

            # 评估: 每年都为正且复合年化>=100%
            all_positive = all(year_annuals[y] > 0 for y in YEARS)
            composite_score = annual_3y if all_positive else annual_3y - 1000

            if composite_score > best_composite:
                best_composite = composite_score
                best_version = v_name

        # === 最优版本月度明细 ===
        if best_version:
            print(f"\n*** 最优版本: {best_version} ***")
            # 检查是否达标
            all_pos = all(all_results[(best_version, y)]['stats']['annual'] > 0 for y in YEARS)
            compound_check = 1.0
            for y in YEARS:
                compound_check *= (1 + all_results[(best_version, y)]['stats']['annual'] / 100)
            ann_check = (compound_check ** (1.0/3) - 1) * 100 if compound_check > 0 else -100

            if all_pos and ann_check >= 100:
                print(f"*** 达标! 每年为正, 3年复合年化: {ann_check:+.1f}% ***")
            elif all_pos:
                print(f"每年为正, 但3年复合年化{ann_check:+.1f}% < 100%")
            else:
                neg_years = [y for y in YEARS if all_results[(best_version, y)]['stats']['annual'] <= 0]
                print(f"未达标: {neg_years}年为负")

            # 合并3年交易
            best_trades = []
            for y in YEARS:
                best_trades.extend(all_results[(best_version, y)]['trades'])

            print_monthly_detail(best_trades, YEARS)

            # === 仓位模拟 ===
            print(f"\n=== 仓位模拟(N={POSITION_COUNT}, 初始100万, {best_version}, 3年连续) ===")
            portfolio_results = simulate_portfolio(best_trades)
            print(f"2023-01-01 初始: 100.0万")
            prev_cap = 1000000.0
            for year, cap in portfolio_results:
                year_ret = (cap - prev_cap) / prev_cap * 100 if prev_cap > 0 else 0
                print(f"{year}-12-31 年末: {cap/10000:.1f}万 (年化{year_ret:+.1f}%)")
                prev_cap = cap

            # 3年复合年化
            final_cap = portfolio_results[-1][1] if portfolio_results else 1000000
            total_ret_3y = final_cap / 1000000.0
            compound_annual = (total_ret_3y ** (1.0/3) - 1) * 100 if total_ret_3y > 0 else -100
            print(f"3年复合年化: {compound_annual:+.1f}%")

        # === 达标判断 ===
        print(f"\n{'='*80}")
        print("=== 达标分析 ===")
        found_qualified = False
        for v_name, rr_thr, sl, tp, trail, max_day, max_hour in PARAM_VERSIONS:
            year_annuals = {y: all_results[(v_name, y)]['stats']['annual'] for y in YEARS}
            all_pos = all(year_annuals[y] > 0 for y in YEARS)
            compound = 1.0
            for y in YEARS:
                compound *= (1 + year_annuals[y] / 100)
            ann = (compound ** (1.0/3) - 1) * 100 if compound > 0 else -100

            if all_pos and ann >= 100:
                desc = f"大盘>{rr_thr}%" if rr_thr else "无过滤"
                tp_desc = f"trailing{int(trail*100)}%" if trail else f"+{tp}%"
                print(f"  {v_name} 达标! {desc}, 止损{sl}%, 止盈{tp_desc}, "
                      f"持有T+{max_day}_h{max_hour}, 3年复合年化{ann:+.1f}%")
                found_qualified = True

        if not found_qualified:
            print("  无版本达标(3年每年为正且复合年化>=100%)")
            print("\n  --- 原因分析 ---")
            for v_name, _, _, _, _, _, _ in PARAM_VERSIONS:
                year_annuals = {y: all_results[(v_name, y)]['stats']['annual'] for y in YEARS}
                neg_years = [(y, year_annuals[y]) for y in YEARS if year_annuals[y] <= 0]
                if neg_years:
                    print(f"  {v_name}: 亏损年份 -> {', '.join(f'{y}({a:+.1f}%)' for y, a in neg_years)}")
                else:
                    compound = 1.0
                    for y in YEARS:
                        compound *= (1 + year_annuals[y] / 100)
                    ann = (compound ** (1.0/3) - 1) * 100 if compound > 0 else -100
                    print(f"  {v_name}: 每年为正但复合年化{ann:+.1f}%不足100%")

            print("\n  --- 建议 ---")
            print("  1. 尝试更严格的red_ratio过滤(>55%)")
            print("  2. 增加T日条件(如hour1_close_rate>3%)")
            print("  3. 缩短持有期降低回撤")
            print("  4. 考虑组合多策略分散风险")

    finally:
        conn.close()

    print(f"\n============ 验证完成 ============")


if __name__ == '__main__':
    main()
