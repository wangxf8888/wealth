#!/usr/bin/env python3
"""跌停反弹策略 Phase-2: 买卖规则统计分析

用法:
    python3 scripts/strategy_limitdown_rebound_phase2.py [YYYY-MM]

示例:
    python3 scripts/strategy_limitdown_rebound_phase2.py 2026-04

分析内容:
    1) 买入x卖出 收益矩阵
    2) 止盈止损组合分析
    3) 条件过滤分析
    4) 最优策略推荐
"""

import sqlite3
import sys
from collections import defaultdict
import statistics

# ============================================================
# 配置区
# ============================================================
DB_PATH = '/home/AIWealth/data/stocks.db'
TARGET_MONTH = sys.argv[1] if len(sys.argv) > 1 else '2026-04'

# 止损阈值列表 (负数)
STOP_LOSS_LIST = [-2, -3, -4, -5, -7]
# 止盈阈值列表 (正数)
TAKE_PROFIT_LIST = [3, 4, 5, 6, 8, 10]


# ============================================================
# 跌停判定 (复用Phase-1逻辑)
# ============================================================

def get_limit_down_threshold(code: str) -> float:
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

def get_trading_days(conn, month: str):
    cur = conn.execute(
        "SELECT DISTINCT date FROM stock_kline WHERE date LIKE ? ORDER BY date ASC",
        (f"{month}-%",),
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
    cur = conn.execute(
        """SELECT date, code, code_name, preclose, open, high, low, close,
               close_rate, open_rate, volume, amount, turn, isST
        FROM stock_kline WHERE date = ?""",
        (day,),
    )
    cols = [d[0] for d in cur.description]
    return {row[1]: dict(zip(cols, row)) for row in cur.fetchall()}


def fetch_stock_days(conn, code: str, dates):
    """获取股票在给定日期列表的hour级行情"""
    if not dates:
        return {}
    placeholders = ','.join('?' * len(dates))
    sql = f"""
        SELECT date, open, high, low, close, preclose, turn, open_rate,
               hour1_open, hour1_close, hour1_close_rate,
               hour2_open, hour2_close, hour2_close_rate,
               hour3_open, hour3_close, hour3_close_rate,
               hour4_open, hour4_close, hour4_close_rate
        FROM stock_kline
        WHERE code = ? AND date IN ({placeholders})
        ORDER BY date ASC
    """
    cur = conn.execute(sql, [code] + list(dates))
    cols = [d[0] for d in cur.description]
    return {row[0]: dict(zip(cols, row)) for row in cur.fetchall()}


# ============================================================
# 候选股筛选 (复用Phase-1)
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

        candidates.append({
            'code': code,
            'code_name': cn,
            'prev_close': prev.get('close'),
            'prev_preclose': prev.get('preclose'),
            'today_open': t_open,
            'today_preclose': t_pre,
            'today_open_rate': today.get('open_rate'),
            'today_turn': today.get('turn'),
        })
    return candidates


# ============================================================
# 收益计算核心
# ============================================================

def get_hour_opens(day_data):
    """从日行情数据中提取hour1~4的open价序列"""
    if day_data is None:
        return []
    opens = []
    for h in range(1, 5):
        v = day_data.get(f'hour{h}_open')
        if v is not None and v > 0:
            opens.append((h, float(v)))
    return opens


def calc_return(buy_price, sell_price):
    """计算收益率(百分比)"""
    if buy_price is None or sell_price is None or buy_price <= 0:
        return None
    return (sell_price - buy_price) / buy_price * 100.0


def build_price_timeline(stock_days, t_day, next_days):
    """构建从T日开始的hour-open价格时间线
    返回: [(day_offset, hour, price), ...]
    day_offset: 0=T日, 1=T+1, 2=T+2, ...
    """
    timeline = []
    all_days = [t_day] + next_days
    for idx, d in enumerate(all_days):
        day_data = stock_days.get(d)
        if day_data is None:
            continue
        for h in range(1, 5):
            price = day_data.get(f'hour{h}_open')
            if price is not None and price > 0:
                timeline.append((idx, h, float(price)))
    return timeline


# ============================================================
# 分析逻辑
# ============================================================

def analyze_buy_sell_matrix(all_cands_data):
    """分析买入x卖出收益矩阵
    
    买入方案:
    A: T日hour1_open  B: T日hour2_open  C: T日hour3_open  D: T+1日hour1_open
    
    卖出时点: 基于合规(T+1规则)
    """
    # 买入方案定义: (day_offset, hour)
    buy_plans = {
        'A(T_h1)': (0, 1),
        'B(T_h2)': (0, 2),
        'C(T_h3)': (0, 3),
        'D(T+1_h1)': (1, 1),
    }
    
    # 卖出方案定义: (day_offset, hour)
    # 对A/B/C(T日买入), 最早T+1卖出
    sell_plans_abc = [
        ('T+1_h1', 1, 1), ('T+1_h2', 1, 2), ('T+1_h3', 1, 3), ('T+1_h4', 1, 4),
        ('T+2_h1', 2, 1), ('T+2_h4', 2, 4),
    ]
    # 对D(T+1买入), 最早T+2卖出
    sell_plans_d = [
        ('T+2_h1', 2, 1), ('T+2_h2', 2, 2), ('T+2_h3', 2, 3), ('T+2_h4', 2, 4),
        ('T+3_h1', 3, 1),
    ]
    
    results = []
    
    for buy_name, (buy_day_off, buy_hour) in buy_plans.items():
        if buy_name.startswith('D'):
            sell_plans = sell_plans_d
        else:
            sell_plans = sell_plans_abc
        
        for sell_name, sell_day_off, sell_hour in sell_plans:
            returns = []
            for cand_info in all_cands_data:
                timeline = cand_info['timeline']
                buy_price = None
                sell_price = None
                for (d_off, h, price) in timeline:
                    if d_off == buy_day_off and h == buy_hour:
                        buy_price = price
                    if d_off == sell_day_off and h == sell_hour:
                        sell_price = price
                if buy_price and sell_price:
                    ret = calc_return(buy_price, sell_price)
                    if ret is not None:
                        returns.append(ret)
            
            if returns:
                avg_ret = statistics.mean(returns)
                med_ret = statistics.median(returns)
                win_rate = sum(1 for r in returns if r > 0) / len(returns) * 100
                wins = [r for r in returns if r > 0]
                losses = [r for r in returns if r <= 0]
                avg_win = statistics.mean(wins) if wins else 0
                avg_loss = abs(statistics.mean(losses)) if losses else 0.001
                pnl_ratio = avg_win / avg_loss if avg_loss > 0 else 999
                results.append({
                    'buy': buy_name,
                    'sell': sell_name,
                    'avg_ret': avg_ret,
                    'med_ret': med_ret,
                    'win_rate': win_rate,
                    'pnl_ratio': pnl_ratio,
                    'count': len(returns),
                })
    return results


def analyze_stop_loss_profit(all_cands_data, buy_day_off, buy_hour, 
                              earliest_sell_day, max_sell_day, buy_label):
    """止盈止损分析
    
    在合法卖出区间内逐hour检查open价:
    先触发哪个算哪个，都未触发则在最后时刻按open卖出
    """
    results = []
    
    for sl in STOP_LOSS_LIST:
        for tp in TAKE_PROFIT_LIST:
            returns = []
            for cand_info in all_cands_data:
                timeline = cand_info['timeline']
                buy_price = None
                for (d_off, h, price) in timeline:
                    if d_off == buy_day_off and h == buy_hour:
                        buy_price = price
                        break
                
                if buy_price is None or buy_price <= 0:
                    continue
                
                # 在合法卖出区间内逐hour扫描
                triggered = False
                last_valid_price = None
                for (d_off, h, price) in timeline:
                    # 跳过买入日之前 & 不可卖出区间
                    if d_off < earliest_sell_day:
                        continue
                    if d_off > max_sell_day:
                        break
                    
                    last_valid_price = price
                    ret_pct = (price - buy_price) / buy_price * 100.0
                    
                    if ret_pct <= sl:
                        returns.append(ret_pct)
                        triggered = True
                        break
                    elif ret_pct >= tp:
                        returns.append(ret_pct)
                        triggered = True
                        break
                
                if not triggered and last_valid_price is not None:
                    ret_pct = (last_valid_price - buy_price) / buy_price * 100.0
                    returns.append(ret_pct)
            
            if not returns:
                continue
            
            avg_ret = statistics.mean(returns)
            win_rate = sum(1 for r in returns if r > 0) / len(returns) * 100
            wins = [r for r in returns if r > 0]
            losses = [r for r in returns if r <= 0]
            avg_win = statistics.mean(wins) if wins else 0
            avg_loss = abs(statistics.mean(losses)) if losses else 0.001
            pnl_ratio = avg_win / avg_loss if avg_loss > 0 else 999
            
            # 月化估算: 每月约20个交易日, 每笔持有约1~2天
            # 假设满仓每天可做一笔, 月化 = avg_ret * trades_per_month
            # 保守估算: 平均持有1.5天 -> 每月约13笔
            trades_per_month = 13
            monthly_ret = avg_ret * trades_per_month
            
            results.append({
                'sl': sl, 'tp': tp,
                'hold_limit': f'T+{max_sell_day}_h4',
                'win_rate': win_rate,
                'avg_ret': avg_ret,
                'monthly': monthly_ret,
                'pnl_ratio': pnl_ratio,
                'count': len(returns),
            })
    
    return results


def analyze_conditions(all_cands_data, buy_day_off, buy_hour, sell_day_off, sell_hour):
    """条件过滤分析"""
    results = {}
    
    # 按T日open_rate分组
    open_rate_groups = {
        '低开(-10%~-2%)': (-10, -2),
        '平开(-2%~+2%)': (-2, 2),
        '高开(+2%~+10%)': (2, 10),
    }
    
    group_data = defaultdict(list)
    for cand_info in all_cands_data:
        or_val = cand_info.get('open_rate')
        if or_val is None:
            continue
        for gname, (lo, hi) in open_rate_groups.items():
            if lo <= or_val < hi:
                group_data[gname].append(cand_info)
                break
    
    results['open_rate'] = {}
    for gname, items in group_data.items():
        rets = _calc_returns_for_group(items, buy_day_off, buy_hour, sell_day_off, sell_hour)
        if rets:
            results['open_rate'][gname] = _summarize_returns(rets)
    
    # 按板块分组
    board_groups = defaultdict(list)
    for cand_info in all_cands_data:
        code = cand_info['code']
        if code.startswith('sz.30') or code.startswith('sh.68'):
            board_groups['创业板/科创板'].append(cand_info)
        else:
            board_groups['主板'].append(cand_info)
    
    results['board'] = {}
    for gname, items in board_groups.items():
        rets = _calc_returns_for_group(items, buy_day_off, buy_hour, sell_day_off, sell_hour)
        if rets:
            results['board'][gname] = _summarize_returns(rets)
    
    # 按T日hour1_close_rate分组
    h1cr_groups = {
        '<-3%': (-999, -3),
        '-3%~0%': (-3, 0),
        '0%~+3%': (0, 3),
        '>+3%': (3, 999),
    }
    h1cr_data = defaultdict(list)
    for cand_info in all_cands_data:
        h1cr = cand_info.get('hour1_close_rate')
        if h1cr is None:
            continue
        for gname, (lo, hi) in h1cr_groups.items():
            if lo <= h1cr < hi:
                h1cr_data[gname].append(cand_info)
                break
    
    results['hour1_close_rate'] = {}
    for gname, items in h1cr_data.items():
        rets = _calc_returns_for_group(items, buy_day_off, buy_hour, sell_day_off, sell_hour)
        if rets:
            results['hour1_close_rate'][gname] = _summarize_returns(rets)
    
    # 按换手率分组
    turn_groups = {
        '<5%': (0, 5),
        '5%~10%': (5, 10),
        '10%~20%': (10, 20),
        '>20%': (20, 999),
    }
    turn_data = defaultdict(list)
    for cand_info in all_cands_data:
        turn = cand_info.get('turn')
        if turn is None:
            continue
        for gname, (lo, hi) in turn_groups.items():
            if lo <= turn < hi:
                turn_data[gname].append(cand_info)
                break
    
    results['turn'] = {}
    for gname, items in turn_data.items():
        rets = _calc_returns_for_group(items, buy_day_off, buy_hour, sell_day_off, sell_hour)
        if rets:
            results['turn'][gname] = _summarize_returns(rets)
    
    return results


def _calc_returns_for_group(items, buy_day_off, buy_hour, sell_day_off, sell_hour):
    """为一组候选股计算买入->卖出收益"""
    returns = []
    for cand_info in items:
        timeline = cand_info['timeline']
        buy_price = None
        sell_price = None
        for (d_off, h, price) in timeline:
            if d_off == buy_day_off and h == buy_hour:
                buy_price = price
            if d_off == sell_day_off and h == sell_hour:
                sell_price = price
        if buy_price and sell_price:
            ret = calc_return(buy_price, sell_price)
            if ret is not None:
                returns.append(ret)
    return returns


def _summarize_returns(returns):
    if not returns:
        return None
    avg_ret = statistics.mean(returns)
    med_ret = statistics.median(returns)
    win_rate = sum(1 for r in returns if r > 0) / len(returns) * 100
    wins = [r for r in returns if r > 0]
    losses = [r for r in returns if r <= 0]
    avg_win = statistics.mean(wins) if wins else 0
    avg_loss = abs(statistics.mean(losses)) if losses else 0.001
    pnl_ratio = avg_win / avg_loss if avg_loss > 0 else 999
    return {
        'avg_ret': avg_ret, 'med_ret': med_ret,
        'win_rate': win_rate, 'pnl_ratio': pnl_ratio, 'count': len(returns),
    }


# ============================================================
# 输出格式化
# ============================================================

def print_matrix(results):
    print("\n--- 1. 买入x卖出 收益矩阵 ---")
    print(f"{'买入':<10} | {'卖出':<8} | {'均收益':>7} | {'中位收益':>8} | {'胜率':>6} | {'盈亏比':>6} | {'数量':>4}")
    print('-' * 72)
    for r in results:
        print(f"{r['buy']:<10} | {r['sell']:<8} | {r['avg_ret']:>+6.2f}% | {r['med_ret']:>+7.2f}% | "
              f"{r['win_rate']:>5.1f}% | {r['pnl_ratio']:>5.2f} | {r['count']:>4d}")


def print_stop_analysis(results, label):
    print(f"\n--- 2. 止盈止损组合 ({label}) ---")
    print(f"{'止损':>5} | {'止盈':>5} | {'持有上限':<8} | {'胜率':>6} | {'均收益':>7} | {'月化':>8} | {'盈亏比':>6} | {'数量':>4}")
    print('-' * 80)
    # 按月化收益降序排序
    sorted_r = sorted(results, key=lambda x: x['monthly'], reverse=True)
    for r in sorted_r[:30]:  # top 30
        print(f"{r['sl']:>+4d}% | {r['tp']:>+4d}% | {r['hold_limit']:<8} | "
              f"{r['win_rate']:>5.1f}% | {r['avg_ret']:>+6.2f}% | {r['monthly']:>+7.1f}% | "
              f"{r['pnl_ratio']:>5.2f} | {r['count']:>4d}")


def print_conditions(cond_results):
    print("\n--- 3. 条件过滤分析 ---")
    
    section_names = {
        'open_rate': '按T日开盘涨幅分组',
        'board': '按板块分组',
        'hour1_close_rate': '按T日hour1收盘涨幅分组',
        'turn': '按T日换手率分组',
    }
    
    for key, title in section_names.items():
        data = cond_results.get(key, {})
        if not data:
            continue
        print(f"\n  [{title}]")
        print(f"  {'分组':<18} | {'均收益':>7} | {'中位收益':>8} | {'胜率':>6} | {'盈亏比':>6} | {'数量':>4}")
        print(f"  {'-'*66}")
        for gname, stats in sorted(data.items()):
            if stats:
                print(f"  {gname:<18} | {stats['avg_ret']:>+6.2f}% | {stats['med_ret']:>+7.2f}% | "
                      f"{stats['win_rate']:>5.1f}% | {stats['pnl_ratio']:>5.2f} | {stats['count']:>4d}")


def analyze_filtered_stop_profit(all_cands_data, filter_fn, filter_name,
                                  buy_day_off, buy_hour, earliest_sell_day, max_sell_day):
    """对满足条件的子集做止盈止损分析"""
    filtered = [c for c in all_cands_data if filter_fn(c)]
    if len(filtered) < 10:
        return None, len(filtered)
    results = analyze_stop_loss_profit(filtered, buy_day_off, buy_hour,
                                       earliest_sell_day, max_sell_day, filter_name)
    return results, len(filtered)


def find_best_strategy(matrix_results, stop_results_all, all_cands_data):
    """找到最优策略推荐(含条件过滤增强)"""
    print("\n--- 4. 最优策略推荐 ---")
    
    # 从矩阵中找最优
    best_matrix = max(matrix_results, key=lambda x: x['avg_ret'] * (x['win_rate']/100))
    print(f"\n  [最优裸买卖(无止盈止损)]")
    print(f"  买入: {best_matrix['buy']}")
    print(f"  卖出: {best_matrix['sell']}")
    print(f"  均收益: {best_matrix['avg_ret']:+.2f}%, 胜率: {best_matrix['win_rate']:.1f}%, "
          f"盈亏比: {best_matrix['pnl_ratio']:.2f}")
    est_monthly = best_matrix['avg_ret'] * 13
    print(f"  月化估算(13笔/月): {est_monthly:+.1f}%")
    
    # 从止盈止损中找最优(全量)
    best_stop = None
    best_score = -999
    for label, results in stop_results_all.items():
        for r in results:
            if r['monthly'] >= 10 and r['win_rate'] >= 50:
                score = r['monthly'] * (r['win_rate'] / 100) * min(r['pnl_ratio'], 3)
                if score > best_score:
                    best_score = score
                    best_stop = (label, r)
    
    if best_stop:
        label, r = best_stop
        print(f"\n  [最优止盈止损策略(全量)]")
        print(f"  买入: {label}")
        print(f"  止损: {r['sl']}%, 止盈: {r['tp']}%, 持有上限: {r['hold_limit']}")
        print(f"  均收益: {r['avg_ret']:+.2f}%, 胜率: {r['win_rate']:.1f}%, "
              f"盈亏比: {r['pnl_ratio']:.2f}")
        print(f"  月化: {r['monthly']:+.1f}%")
    else:
        all_stops = []
        for label, results in stop_results_all.items():
            for r in results:
                all_stops.append((label, r))
        if all_stops:
            all_stops.sort(key=lambda x: x[1]['monthly'], reverse=True)
            label, r = all_stops[0]
            print(f"\n  [月化最高策略(未达标)]")
            print(f"  买入: {label}")
            print(f"  止损: {r['sl']}%, 止盈: {r['tp']}%, 持有上限: {r['hold_limit']}")
            print(f"  均收益: {r['avg_ret']:+.2f}%, 胜率: {r['win_rate']:.1f}%, "
                  f"盈亏比: {r['pnl_ratio']:.2f}")
            print(f"  月化: {r['monthly']:+.1f}%")
    
    # === 条件过滤增强分析 ===
    # D方案(T+1买入)可以使用T日hour1_close_rate作为前置过滤(合规!)
    print(f"\n--- 5. 条件增强止盈止损 (方案D + hour1_close_rate过滤) ---")
    
    filters = [
        ('h1cr>=0%(反弹确认)', lambda c: c.get('hour1_close_rate') is not None and c['hour1_close_rate'] >= 0),
        ('h1cr>=-3%(非大跌)', lambda c: c.get('hour1_close_rate') is not None and c['hour1_close_rate'] >= -3),
        ('低开+h1cr>=0%', lambda c: (c.get('open_rate') is not None and c['open_rate'] < -2
                                     and c.get('hour1_close_rate') is not None and c['hour1_close_rate'] >= 0)),
        ('换手>10%+h1cr>=0%', lambda c: (c.get('turn') is not None and c['turn'] >= 10
                                          and c.get('hour1_close_rate') is not None and c['hour1_close_rate'] >= 0)),
    ]
    
    for fname, ffn in filters:
        results, n = analyze_filtered_stop_profit(
            all_cands_data, ffn, fname, 1, 1, 2, 3)
        if results and n >= 10:
            # 取top5
            sorted_r = sorted(results, key=lambda x: x['monthly'], reverse=True)[:5]
            print(f"\n  [{fname}] (N={n})")
            print(f"  {'止损':>5} | {'止盈':>5} | {'胜率':>6} | {'均收益':>7} | {'月化':>8} | {'盈亏比':>6}")
            for r in sorted_r:
                print(f"  {r['sl']:>+4d}% | {r['tp']:>+4d}% | {r['win_rate']:>5.1f}% | "
                      f"{r['avg_ret']:>+6.2f}% | {r['monthly']:>+7.1f}% | {r['pnl_ratio']:>5.2f}")
    
    # A方案条件增强: 用open_rate (买入前已知)
    print(f"\n--- 6. 条件增强 (方案A + 低开过滤) ---")
    filters_a = [
        ('低开(-10%~-2%)', lambda c: c.get('open_rate') is not None and -10 <= c['open_rate'] < -2),
        ('平开+低开(-5%~+2%)', lambda c: c.get('open_rate') is not None and -5 <= c['open_rate'] < 2),
    ]
    for fname, ffn in filters_a:
        results, n = analyze_filtered_stop_profit(
            all_cands_data, ffn, fname, 0, 1, 1, 2)
        if results and n >= 10:
            sorted_r = sorted(results, key=lambda x: x['monthly'], reverse=True)[:5]
            print(f"\n  [{fname}] (N={n})")
            print(f"  {'止损':>5} | {'止盈':>5} | {'胜率':>6} | {'均收益':>7} | {'月化':>8} | {'盈亏比':>6}")
            for r in sorted_r:
                print(f"  {r['sl']:>+4d}% | {r['tp']:>+4d}% | {r['win_rate']:>5.1f}% | "
                      f"{r['avg_ret']:>+6.2f}% | {r['monthly']:>+7.1f}% | {r['pnl_ratio']:>5.2f}")


# ============================================================
# 主流程
# ============================================================

def main():
    print(f"============ 跌停反弹 Phase-2 统计分析 ============")
    print(f"月份: {TARGET_MONTH}")
    print(f"数据库: {DB_PATH}")
    
    conn = sqlite3.connect(DB_PATH)
    try:
        days = get_trading_days(conn, TARGET_MONTH)
        if not days:
            print(f"[错误] {TARGET_MONTH} 无交易日数据。")
            return
        
        print(f"交易日: {len(days)} 天 ({days[0]} ~ {days[-1]})")
        
        # Phase 1: 筛选候选股
        all_cands_data = []
        total_candidates = 0
        
        for t_day in days:
            t_prev = get_prev_trading_day(conn, t_day)
            cands = screen_candidates(conn, t_day, t_prev)
            total_candidates += len(cands)
            
            # 获取T+1, T+2, T+3的日期
            next_days = get_next_trading_days(conn, t_day, 3)
            
            for c in cands:
                code = c['code']
                all_dates = [t_day] + next_days
                stock_days = fetch_stock_days(conn, code, all_dates)
                
                # 构建价格时间线
                timeline = build_price_timeline(stock_days, t_day, next_days)
                
                if not timeline:
                    continue
                
                # 获取T日数据用于条件分析
                t_data = stock_days.get(t_day, {})
                open_rate_val = None
                if c.get('today_preclose') and c.get('today_open'):
                    open_rate_val = (float(c['today_open']) / float(c['today_preclose']) - 1) * 100
                
                hour1_cr = t_data.get('hour1_close_rate')
                
                cand_entry = {
                    'code': code,
                    'code_name': c['code_name'],
                    't_day': t_day,
                    'timeline': timeline,
                    'open_rate': open_rate_val,
                    'hour1_close_rate': hour1_cr,
                    'turn': c.get('today_turn'),
                }
                all_cands_data.append(cand_entry)
        
        print(f"候选股总数: {total_candidates}, 有效数据: {len(all_cands_data)}")
        
        if not all_cands_data:
            print("[错误] 无有效候选股数据")
            return
        
        # === 1. 买入x卖出矩阵 ===
        matrix_results = analyze_buy_sell_matrix(all_cands_data)
        print_matrix(matrix_results)
        
        # === 2. 止盈止损分析 (多个买入方案) ===
        stop_results_all = {}
        
        # 方案A: T日h1买入, 卖出区间T+1~T+2
        stop_a = analyze_stop_loss_profit(all_cands_data, 0, 1, 1, 2, 'A(T_h1)')
        stop_results_all['A(T_h1)'] = stop_a
        print_stop_analysis(stop_a, '方案A: T日hour1_open买入, 卖出T+1~T+2')
        
        # 方案B: T日h2买入, 卖出区间T+1~T+2
        stop_b = analyze_stop_loss_profit(all_cands_data, 0, 2, 1, 2, 'B(T_h2)')
        stop_results_all['B(T_h2)'] = stop_b
        print_stop_analysis(stop_b, '方案B: T日hour2_open买入, 卖出T+1~T+2')
        
        # 方案C: T日h3买入, 卖出区间T+1~T+2
        stop_c = analyze_stop_loss_profit(all_cands_data, 0, 3, 1, 2, 'C(T_h3)')
        stop_results_all['C(T_h3)'] = stop_c
        print_stop_analysis(stop_c, '方案C: T日hour3_open买入, 卖出T+1~T+2')
        
        # 方案D: T+1日h1买入, 卖出区间T+2~T+3
        stop_d = analyze_stop_loss_profit(all_cands_data, 1, 1, 2, 3, 'D(T+1_h1)')
        stop_results_all['D(T+1_h1)'] = stop_d
        print_stop_analysis(stop_d, '方案D: T+1日hour1_open买入, 卖出T+2~T+3')
        
        # === 3. 条件过滤 (默认用最优裸矩阵的买卖方案) ===
        # 使用方案A + T+1_h1 作为默认分析 (普适性最强)
        cond_results = analyze_conditions(all_cands_data, 0, 1, 1, 1)
        print_conditions(cond_results)
        
        # 额外: 方案B + T+1_h1
        print("\n  [补充: 方案B(T_h2买入) -> T+1_h1卖出]")
        cond_results_b = analyze_conditions(all_cands_data, 0, 2, 1, 1)
        for key in ['open_rate', 'hour1_close_rate']:
            data = cond_results_b.get(key, {})
            if data:
                for gname, stats in sorted(data.items()):
                    if stats:
                        print(f"    {gname:<18} | {stats['avg_ret']:>+6.2f}% | 胜率{stats['win_rate']:>5.1f}% | N={stats['count']}")
        
        # === 4. 最优策略推荐 ===
        find_best_strategy(matrix_results, stop_results_all, all_cands_data)
        
        print(f"\n{'='*60}")
        print(f"分析完成。")
    finally:
        conn.close()


if __name__ == '__main__':
    main()
