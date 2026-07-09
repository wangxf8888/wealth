#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
缩量十字星策略 Phase-2: 候选股买卖时点 + 止盈止损 + 条件过滤统计分析

用法:
    python3 scripts/strategy_shrink_doji_phase2.py [YYYY-MM[,YYYY-MM,...]]
    默认: 2026-04
    多月份示例: 2026-01,2026-02,2026-03,2026-04

筛选逻辑与 Phase-1 一致:
    前3日累计涨幅 > 10%, 当日振幅 < 3%, 当日换手率 < 前3日均值 * 0.6
    过滤 ST / 北交所 / 一字板 / preclose 异常

合规铁律:
    - 买入价只能用 hourX_open
    - 卖出价只能用 hourX_open
    - T+1 买入, 最早 T+2 卖出 (T+1 合规)
    - 止盈止损按 hour 的 open 价逐 hour 检查, 先止损后止盈
    - 不使用未来数据
"""

import os
import sys
import sqlite3
import statistics
from collections import defaultdict

DB_PATH = '/home/AIWealth/data/stocks.db'

# --------- 筛选阈值 (与 Phase-1 一致) ---------
CUM_GAIN_PCT_MIN = 10.0
AMPLITUDE_PCT_MAX = 3.0
TURN_SHRINK_RATIO = 0.6

# --------- 买入方案 ---------
BUY_PLANS = [
    ('A', 1, 'hour1_open'),  # T+1 hour1_open
    ('B', 1, 'hour2_open'),  # T+1 hour2_open
    ('C', 1, 'hour3_open'),  # T+1 hour3_open
]

# --------- 卖出时点 (按 (offset_day, hour) 排序) ---------
SELL_POINTS = [
    (2, 1), (2, 2), (2, 3), (2, 4),
    (3, 1), (3, 2), (3, 3), (3, 4),
]
SELL_POINT_LABELS = {
    (2, 1): 'T+2_h1', (2, 2): 'T+2_h2', (2, 3): 'T+2_h3', (2, 4): 'T+2_h4',
    (3, 1): 'T+3_h1', (3, 2): 'T+3_h2', (3, 3): 'T+3_h3', (3, 4): 'T+3_h4',
}

# --------- 止盈止损网格 ---------
STOP_LOSS_GRID = [-2.0, -3.0, -4.0, -5.0]
TAKE_PROFIT_GRID = [3.0, 5.0, 6.0, 8.0, 10.0]

# 卖出时点的最大持有 hour 序列 (方案A为 T+1 h1 买入, 持有期为 T+2~T+3)
HOLD_HOURS_FOR_PLAN_A = [(2, 1), (2, 2), (2, 3), (2, 4),
                         (3, 1), (3, 2), (3, 3), (3, 4)]


# ============================================================
# 数据访问
# ============================================================

def get_trading_dates_in_month(conn, month):
    rows = conn.execute(
        "SELECT DISTINCT date FROM stock_kline WHERE date LIKE ? ORDER BY date",
        (f"{month}%",),
    ).fetchall()
    return [r[0] for r in rows]


def get_all_trading_dates(conn):
    rows = conn.execute(
        "SELECT DISTINCT date FROM stock_kline ORDER BY date"
    ).fetchall()
    return [r[0] for r in rows]


def is_one_word_board(row):
    if row['high'] is None or row['low'] is None:
        return False
    return abs(row['high'] - row['low']) < 1e-6


def basic_filter(row):
    code = row['code'] or ''
    name = row['code_name'] or ''
    if code.startswith('bj.'):
        return False
    if (row['isST'] is not None and row['isST'] == 1) or 'ST' in name.upper():
        return False
    if row['preclose'] is None or row['preclose'] <= 0:
        return False
    return True


def screen_candidates_for_day(conn, t_day, all_dates):
    """筛选 T 日候选股, 返回包含特征字段的列表"""
    if t_day not in all_dates:
        return []
    idx = all_dates.index(t_day)
    if idx < 4:
        return []

    t_minus_1 = all_dates[idx - 1]
    t_minus_2 = all_dates[idx - 2]
    t_minus_3 = all_dates[idx - 3]
    t_minus_4 = all_dates[idx - 4]

    t_rows = conn.execute(
        "SELECT * FROM stock_kline WHERE date = ?", (t_day,)
    ).fetchall()

    prev_dates = [t_minus_4, t_minus_3, t_minus_2, t_minus_1]
    placeholder = ",".join(["?"] * len(prev_dates))
    prev_rows = conn.execute(
        f"SELECT date, code, close, turn FROM stock_kline "
        f"WHERE date IN ({placeholder})",
        prev_dates,
    ).fetchall()

    prev_map = defaultdict(dict)
    for r in prev_rows:
        prev_map[r['code']][r['date']] = {
            'close': r['close'], 'turn': r['turn'],
        }

    candidates = []
    for row in t_rows:
        if not basic_filter(row):
            continue
        if is_one_word_board(row):
            continue

        code = row['code']
        prev = prev_map.get(code)
        if not prev or not all(d in prev for d in prev_dates):
            continue

        c_t_minus_1 = prev[t_minus_1]['close']
        c_t_minus_4 = prev[t_minus_4]['close']
        if c_t_minus_1 is None or c_t_minus_4 is None or c_t_minus_4 <= 0:
            continue

        cum_gain_pct = (c_t_minus_1 / c_t_minus_4 - 1.0) * 100.0
        if cum_gain_pct <= CUM_GAIN_PCT_MIN:
            continue

        if row['high'] is None or row['low'] is None or row['preclose'] is None or row['preclose'] <= 0:
            continue
        amplitude_pct = abs(row['high'] - row['low']) / row['preclose'] * 100.0
        if amplitude_pct >= AMPLITUDE_PCT_MAX:
            continue

        turns_prev3 = [prev[t_minus_3]['turn'], prev[t_minus_2]['turn'], prev[t_minus_1]['turn']]
        if any(t is None or t <= 0 for t in turns_prev3):
            continue
        avg_turn = sum(turns_prev3) / 3.0
        t_turn = row['turn']
        if t_turn is None or t_turn <= 0:
            continue
        if t_turn >= avg_turn * TURN_SHRINK_RATIO:
            continue

        candidates.append({
            'code': code,
            'code_name': row['code_name'],
            't_day': t_day,
            'cum_gain_pct': cum_gain_pct,
            'amplitude_pct': amplitude_pct,
            't_turn': t_turn,
            'avg_turn_prev3': avg_turn,
            'turn_ratio': t_turn / avg_turn if avg_turn > 0 else 0,
        })

    return candidates


def fetch_future_rows(conn, code, t_day, all_dates, n_after=4):
    """获取 T+1 ~ T+n_after 日的 K 线(包含 hour 字段). 返回 {offset:row}"""
    if t_day not in all_dates:
        return {}
    idx = all_dates.index(t_day)
    future_dates = []
    offsets = []
    for k in range(1, n_after + 1):
        if idx + k < len(all_dates):
            future_dates.append(all_dates[idx + k])
            offsets.append(k)
    if not future_dates:
        return {}
    placeholder = ",".join(["?"] * len(future_dates))
    rows = conn.execute(
        f"SELECT * FROM stock_kline WHERE code = ? AND date IN ({placeholder})",
        (code, *future_dates),
    ).fetchall()
    by_date = {r['date']: r for r in rows}
    result = {}
    for off, d in zip(offsets, future_dates):
        if d in by_date:
            result[off] = by_date[d]
    return result


# ============================================================
# 收益计算
# ============================================================

def get_hour_open(row, hour):
    if row is None:
        return None
    val = row[f'hour{hour}_open']
    return val if val is not None and val > 0 else None


def calc_simple_return(buy_price, sell_price):
    if buy_price is None or sell_price is None or buy_price <= 0:
        return None
    return (sell_price - buy_price) / buy_price * 100.0


def evaluate_buy_sell_matrix(samples):
    """对每个 (买入方案, 卖出时点) 组合, 汇总收益统计.
    samples: list[dict] with future rows attached
    返回: list of stat dicts
    """
    results = []
    for plan_label, buy_offset, buy_field in BUY_PLANS:
        buy_hour = int(buy_field.replace('hour', '').replace('_open', ''))
        for sell_off, sell_hour in SELL_POINTS:
            returns = []
            for s in samples:
                future = s['future']
                buy_row = future.get(buy_offset)
                sell_row = future.get(sell_off)
                bp = get_hour_open(buy_row, buy_hour)
                sp = get_hour_open(sell_row, sell_hour)
                r = calc_simple_return(bp, sp)
                if r is not None:
                    returns.append(r)
            if not returns:
                continue
            wins = [r for r in returns if r > 0]
            losses = [r for r in returns if r <= 0]
            avg = statistics.mean(returns)
            med = statistics.median(returns)
            win_rate = len(wins) / len(returns) * 100.0
            avg_win = statistics.mean(wins) if wins else 0.0
            avg_loss = statistics.mean(losses) if losses else 0.0
            pl_ratio = (avg_win / abs(avg_loss)) if avg_loss != 0 else float('inf')
            results.append({
                'plan': plan_label,
                'buy_label': f"T+{buy_offset}_h{buy_hour}",
                'sell_label': SELL_POINT_LABELS[(sell_off, sell_hour)],
                'avg': avg, 'median': med, 'win_rate': win_rate,
                'avg_win': avg_win, 'avg_loss': avg_loss,
                'pl_ratio': pl_ratio, 'count': len(returns),
            })
    return results


def simulate_stop_grid(samples, buy_offset, buy_hour, hold_hours,
                      stop_loss_pct, take_profit_pct):
    """方案A 止盈止损模拟. 逐 hour 检查 open, 先止损后止盈.
    返回 (returns, hold_days_list)
    """
    returns = []
    hold_days = []
    for s in samples:
        future = s['future']
        buy_row = future.get(buy_offset)
        bp = get_hour_open(buy_row, buy_hour)
        if bp is None:
            continue
        exit_price = None
        exit_off = None
        for off, h in hold_hours:
            row = future.get(off)
            op = get_hour_open(row, h)
            if op is None:
                continue
            pnl_pct = (op - bp) / bp * 100.0
            # 先止损
            if pnl_pct <= stop_loss_pct:
                exit_price = op
                exit_off = off
                break
            if pnl_pct >= take_profit_pct:
                exit_price = op
                exit_off = off
                break
        if exit_price is None:
            # 持有到最后一个有效 hour (尾盘退出)
            for off, h in reversed(hold_hours):
                row = future.get(off)
                op = get_hour_open(row, h)
                if op is not None:
                    exit_price = op
                    exit_off = off
                    break
        if exit_price is None:
            continue
        ret = (exit_price - bp) / bp * 100.0
        returns.append(ret)
        hold_days.append(exit_off - buy_offset + 1)  # 大致持有日数
    return returns, hold_days


def evaluate_stop_grid(samples, months_count):
    results = []
    buy_offset, buy_hour = 1, 1  # 方案A
    n_candidates = len(samples)
    if n_candidates == 0 or months_count <= 0:
        return results
    for sl in STOP_LOSS_GRID:
        for tp in TAKE_PROFIT_GRID:
            returns, hold_days = simulate_stop_grid(
                samples, buy_offset, buy_hour,
                HOLD_HOURS_FOR_PLAN_A, sl, tp)
            if not returns:
                continue
            wins = [r for r in returns if r > 0]
            losses = [r for r in returns if r <= 0]
            avg = statistics.mean(returns)
            win_rate = len(wins) / len(returns) * 100.0
            avg_win = statistics.mean(wins) if wins else 0.0
            avg_loss = statistics.mean(losses) if losses else 0.0
            pl_ratio = (avg_win / abs(avg_loss)) if avg_loss != 0 else float('inf')
            # 月化: 假设每月有 (n/months) 笔, 单笔平均收益 avg, 顺序复利近似 = avg * trades_per_month
            trades_per_month = len(returns) / months_count
            monthly = avg * trades_per_month
            avg_hold = statistics.mean(hold_days) if hold_days else 0.0
            results.append({
                'sl': sl, 'tp': tp,
                'win_rate': win_rate, 'avg': avg,
                'monthly': monthly, 'pl_ratio': pl_ratio,
                'count': len(returns), 'avg_hold': avg_hold,
                'avg_win': avg_win, 'avg_loss': avg_loss,
            })
    return results


def evaluate_subset(samples, buy_offset, buy_hour, sell_off, sell_hour):
    """计算给定子集的 买A 卖 (sell_off, sell_hour) 平均收益和胜率"""
    returns = []
    for s in samples:
        future = s['future']
        bp = get_hour_open(future.get(buy_offset), buy_hour)
        sp = get_hour_open(future.get(sell_off), sell_hour)
        r = calc_simple_return(bp, sp)
        if r is not None:
            returns.append(r)
    if not returns:
        return None
    wins = [r for r in returns if r > 0]
    return {
        'count': len(returns),
        'avg': statistics.mean(returns),
        'win_rate': len(wins) / len(returns) * 100.0,
    }


# ============================================================
# 输出
# ============================================================

def print_buy_sell_matrix(stats):
    print("--- 1. 买入时点 x 卖出时点 收益矩阵 ---")
    header = (
        f"{'买入':<10} | {'卖出':<8} | {'均收益':>8} | {'中位':>8} | "
        f"{'胜率':>6} | {'盈亏比':>6} | {'交易数':>5}"
    )
    print(header)
    print('-' * len(header))
    plan_order = {'A': 0, 'B': 1, 'C': 2}
    sp_order = {SELL_POINT_LABELS[k]: i for i, k in enumerate(SELL_POINTS)}
    for s in sorted(stats, key=lambda x: (plan_order.get(x['plan'], 99), sp_order.get(x['sell_label'], 99))):
        buy_disp = f"{s['plan']}({s['buy_label']})"
        plr = f"{s['pl_ratio']:.2f}" if s['pl_ratio'] != float('inf') else 'inf'
        print(
            f"{buy_disp:<10} | {s['sell_label']:<8} | "
            f"{s['avg']:>+7.2f}% | {s['median']:>+7.2f}% | "
            f"{s['win_rate']:>5.1f}% | {plr:>6} | {s['count']:>5}"
        )
    print()


def print_stop_grid(grid_stats):
    print("--- 2. 止盈止损组合 (方案A: T+1_h1 买入, 持仓 T+2~T+3, 逐 hour open 检查) ---")
    header = (
        f"{'止损':>5} | {'止盈':>5} | {'胜率':>6} | {'均收益':>8} | "
        f"{'月化':>8} | {'盈亏比':>6} | {'平均持有日':>9} | {'交易数':>5}"
    )
    print(header)
    print('-' * len(header))
    # 按月化收益排序
    for s in sorted(grid_stats, key=lambda x: -x['monthly']):
        plr = f"{s['pl_ratio']:.2f}" if s['pl_ratio'] != float('inf') else 'inf'
        print(
            f"{s['sl']:>+5.1f}% | {s['tp']:>+5.1f}% | "
            f"{s['win_rate']:>5.1f}% | {s['avg']:>+7.2f}% | "
            f"{s['monthly']:>+7.2f}% | {plr:>6} | "
            f"{s['avg_hold']:>9.2f} | {s['count']:>5}"
        )
    print()


def print_filter_groups(samples, group_func, group_labels, title):
    print(f"--- {title} ---")
    header = (
        f"{'分组':<14} | {'数量':>4} | {'买A_卖T+2h4 均收益':>18} | {'胜率':>6}"
    )
    print(header)
    print('-' * len(header))
    groups = defaultdict(list)
    for s in samples:
        g = group_func(s)
        if g is not None:
            groups[g].append(s)
    for label in group_labels:
        sub = groups.get(label, [])
        if not sub:
            print(f"{label:<14} | {0:>4} | {'NA':>18} | {'NA':>6}")
            continue
        stats = evaluate_subset(sub, 1, 1, 2, 4)
        if stats is None:
            print(f"{label:<14} | {len(sub):>4} | {'NA':>18} | {'NA':>6}")
        else:
            print(
                f"{label:<14} | {stats['count']:>4} | "
                f"{stats['avg']:>+17.2f}% | {stats['win_rate']:>5.1f}%"
            )
    print()


def cum_gain_group(s):
    g = s['cum_gain_pct']
    if g <= 15: return '(10%,15%]'
    if g <= 20: return '(15%,20%]'
    if g <= 30: return '(20%,30%]'
    return '(30%+)'


def turn_ratio_group(s):
    r = s['turn_ratio']  # 缩量比例 (越小越缩量)
    if r < 0.4: return '<40%'
    if r < 0.5: return '40%-50%'
    return '50%-60%'


def amplitude_group(s):
    a = s['amplitude_pct']
    if a < 1: return '<1%'
    if a < 2: return '1%-2%'
    return '2%-3%'


def print_recommendation(buy_sell_stats, grid_stats, samples_count):
    print("--- 4. 最优策略推荐 ---")
    if not buy_sell_stats and not grid_stats:
        print("无足够数据.")
        return
    # 固定买卖时点最优
    if buy_sell_stats:
        best_bs = max(buy_sell_stats, key=lambda x: x['avg'])
        print(
            f"[固定时点] 买 {best_bs['plan']}({best_bs['buy_label']}) "
            f"-> 卖 {best_bs['sell_label']}: "
            f"均收益 {best_bs['avg']:+.2f}%, 胜率 {best_bs['win_rate']:.1f}%, "
            f"盈亏比 {best_bs['pl_ratio']:.2f}, 样本 {best_bs['count']}"
        )
    # 止盈止损最优 (按月化)
    if grid_stats:
        best_grid = max(grid_stats, key=lambda x: x['monthly'])
        print(
            f"[止盈止损] 方案A + 止损 {best_grid['sl']:+.1f}% / 止盈 {best_grid['tp']:+.1f}%: "
            f"胜率 {best_grid['win_rate']:.1f}%, 均收益 {best_grid['avg']:+.2f}%, "
            f"月化 {best_grid['monthly']:+.2f}%, 盈亏比 {best_grid['pl_ratio']:.2f}, "
            f"平均持有 {best_grid['avg_hold']:.2f} 日, 样本 {best_grid['count']}"
        )
        # 按胜率最优
        best_winrate = max(grid_stats, key=lambda x: x['win_rate'])
        if best_winrate is not best_grid:
            print(
                f"[最高胜率] 方案A + 止损 {best_winrate['sl']:+.1f}% / 止盈 {best_winrate['tp']:+.1f}%: "
                f"胜率 {best_winrate['win_rate']:.1f}%, 均收益 {best_winrate['avg']:+.2f}%, "
                f"月化 {best_winrate['monthly']:+.2f}%"
            )
    print()


# ============================================================
# 主流程
# ============================================================

def collect_samples(conn, months):
    all_dates = get_all_trading_dates(conn)
    samples = []
    daily_count = {}
    for month in months:
        target_dates = get_trading_dates_in_month(conn, month)
        for t_day in target_dates:
            cands = screen_candidates_for_day(conn, t_day, all_dates)
            daily_count[t_day] = len(cands)
            for c in cands:
                future = fetch_future_rows(conn, c['code'], t_day, all_dates, n_after=4)
                if not future:
                    continue
                c['future'] = future
                samples.append(c)
    return samples, daily_count


def main():
    arg = sys.argv[1] if len(sys.argv) > 1 else '2026-04'
    months = [m.strip() for m in arg.split(',') if m.strip()]
    months_count = len(months)

    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row

    print("=" * 70)
    print(f"========== 缩量十字星 Phase-2 统计分析 ==========")
    print(f"月份: {','.join(months)}  (共 {months_count} 个月)")
    print(f"筛选: 前3日累涨 > {CUM_GAIN_PCT_MIN}%, 振幅 < {AMPLITUDE_PCT_MAX}%, "
          f"换手率 < 前3日均值 * {TURN_SHRINK_RATIO}")
    print("=" * 70)

    samples, daily_count = collect_samples(conn, months)
    n_total = len(samples)
    print(f"候选股总数: {n_total}")
    if not samples:
        print("无候选股, 终止分析.")
        return

    # 1. 买卖时点矩阵
    buy_sell_stats = evaluate_buy_sell_matrix(samples)
    print_buy_sell_matrix(buy_sell_stats)

    # 2. 止盈止损网格
    grid_stats = evaluate_stop_grid(samples, months_count)
    print_stop_grid(grid_stats)

    # 3. 条件过滤
    print("--- 3. 条件过滤 ---")
    print()
    print_filter_groups(
        samples, cum_gain_group,
        ['(10%,15%]', '(15%,20%]', '(20%,30%]', '(30%+)'],
        "3.1 按前3日累计涨幅分组"
    )
    print_filter_groups(
        samples, turn_ratio_group,
        ['<40%', '40%-50%', '50%-60%'],
        "3.2 按缩量比例分组 (T日换手率 / 前3日均值)"
    )
    print_filter_groups(
        samples, amplitude_group,
        ['<1%', '1%-2%', '2%-3%'],
        "3.3 按当日振幅分组"
    )

    # 4. 推荐
    print_recommendation(buy_sell_stats, grid_stats, n_total)

    print("=" * 70)
    conn.close()


if __name__ == "__main__":
    main()
