#!/usr/bin/env python3
"""炸板修复(ZhaBan) + 限价单机制 Phase-1+2+3 联合验证

核心创新: 限价单(Limit Order)机制
  - 买入: 基于上一个hour已知数据设定限价P, 若下一hour_low<=P则以P成交
  - 卖出止盈: limit_sell = buy_price*(1+tp%), 若hourX_high>=limit_sell则成交
  - 卖出止损: stop_price = buy_price*(1-sl%), 若hourX_low<=stop_price则成交
  - Trailing止盈: peak更新用hourX_high, 触发检查hourX_low

合规铁律:
  - 限价P基于事先(上一hour)数据计算, 非事后
  - T+1: T日买入最早T+1卖出
  - 涨停判定: round(close/preclose, 2)严格规则

用法:
  python3 scripts/strategy_zhaban_limit_order.py 2026-04
  python3 scripts/strategy_zhaban_limit_order.py 2025
"""
import sqlite3
import sys
import math
import statistics
from collections import defaultdict
from datetime import datetime, timedelta

# ============== 配置区 ==============
DB_PATH = '/home/AIWealth/data/stocks.db'
T_OPEN_RATE_MIN = -5.0
T_OPEN_RATE_MAX = 9.5
MONTHLY_TRADE_DAYS = 22
INDEX_CODE = 'sh.000001'
# 自动扩展阈值
AUTO_EXTEND_MONTHLY = 15.0  # 月化>15%自动扩展全年
AUTO_EXTEND_WIN_RATE = 50.0
# ===================================


def sv(v):
    if v is None:
        return None
    try:
        f = float(v)
        if math.isnan(f):
            return None
        return f
    except (TypeError, ValueError):
        return None


def get_limit_up_threshold(code):
    if code.startswith('sz.30') or code.startswith('sh.68'):
        return 1.20
    return 1.10


def hit_limit_up(high, preclose, code):
    pc = sv(preclose)
    h = sv(high)
    if pc is None or h is None or pc == 0:
        return False
    return round(h / pc, 2) >= get_limit_up_threshold(code)


def closed_limit_up(close, preclose, code):
    pc = sv(preclose)
    cl = sv(close)
    if pc is None or cl is None or pc == 0:
        return False
    return round(cl / pc, 2) >= get_limit_up_threshold(code)


def open_at_limit_up(open_p, preclose, code):
    pc = sv(preclose)
    op = sv(open_p)
    if pc is None or op is None or pc == 0:
        return False
    return round(op / pc, 2) >= get_limit_up_threshold(code)


def is_one_word_board(row):
    o = sv(row.get('open'))
    h = sv(row.get('high'))
    l = sv(row.get('low'))
    c = sv(row.get('close'))
    if None in (o, h, l, c):
        return False
    return o == h == l == c


def parse_months(arg):
    arg = arg.strip()
    if ',' in arg:
        return [m.strip() for m in arg.split(',') if m.strip()]
    if len(arg) == 4 and arg.isdigit():
        y = int(arg)
        return [f'{y:04d}-{m:02d}' for m in range(1, 13)]
    return [arg]


def month_bounds(target_month):
    year, mon = target_month.split('-')
    year, mon = int(year), int(mon)
    first_day = f'{year:04d}-{mon:02d}-01'
    if mon == 12:
        next_first = f'{year+1:04d}-01-01'
    else:
        next_first = f'{year:04d}-{mon+1:02d}-01'
    return first_day, next_first


def load_data(conn, target_months):
    """加载stock_kline全量hour级OHLC + index_kline"""
    cur = conn.cursor()
    months = sorted(set(target_months))
    first = month_bounds(months[0])[0]
    last_first, last_next = month_bounds(months[-1])
    dt_lo = (datetime.strptime(first, '%Y-%m-%d') - timedelta(days=40)).strftime('%Y-%m-%d')
    dt_hi = (datetime.strptime(last_next, '%Y-%m-%d') + timedelta(days=20)).strftime('%Y-%m-%d')

    cur.execute(
        'SELECT DISTINCT date FROM stock_kline WHERE date>=? AND date<? ORDER BY date',
        (dt_lo, dt_hi),
    )
    all_dates = [r[0] for r in cur.fetchall()]
    date_idx = {d: i for i, d in enumerate(all_dates)}

    target_dates = []
    for m in months:
        lo, hi = month_bounds(m)
        for d in all_dates:
            if lo <= d < hi:
                target_dates.append(d)
    target_dates = sorted(set(target_dates))

    # 加载所有hour级OHLC数据
    flds = (
        'date,code,code_name,preclose,open,high,low,close,close_rate,open_rate,'
        'volume,amount,turn,isST,'
        'hour1_open,hour1_high,hour1_low,hour1_close,hour1_open_rate,hour1_close_rate,'
        'hour2_open,hour2_high,hour2_low,hour2_close,hour2_open_rate,hour2_close_rate,'
        'hour3_open,hour3_high,hour3_low,hour3_close,hour3_open_rate,hour3_close_rate,'
        'hour4_open,hour4_high,hour4_low,hour4_close,hour4_open_rate,hour4_close_rate'
    )
    cur.execute(
        f"SELECT {flds} FROM stock_kline WHERE date>=? AND date<? AND code NOT LIKE 'bj.%'",
        (dt_lo, dt_hi),
    )
    keys = flds.split(',')
    by_code = defaultdict(dict)
    n_rows = 0
    for row in cur.fetchall():
        d = row[0]
        c = row[1]
        rec = {keys[i]: row[i] for i in range(len(keys))}
        by_code[c][d] = rec
        n_rows += 1

    cur.execute(
        'SELECT date, red_ratio FROM index_kline WHERE code=? AND date>=? AND date<?',
        (INDEX_CODE, dt_lo, dt_hi),
    )
    red_ratio_by_date = {r[0]: sv(r[1]) for r in cur.fetchall()}

    print(f'[加载] 区间 {dt_lo}~{dt_hi}  交易日={len(all_dates)}  '
          f'目标日={len(target_dates)}  股票数={len(by_code)}  行数={n_rows}  '
          f'red_ratio日数={len(red_ratio_by_date)}')
    return all_dates, date_idx, target_dates, by_code, red_ratio_by_date


def find_candidates(by_code, all_dates, date_idx, target_date, red_ratio_by_date):
    """筛选target_date候选股(T-1炸板信号)"""
    t_idx = date_idx.get(target_date)
    if t_idx is None or t_idx < 1:
        return []
    prev_date = all_dates[t_idx - 1]
    prev_red_ratio = red_ratio_by_date.get(prev_date)

    candidates = []
    for code, dm in by_code.items():
        if code.startswith('bj.'):
            continue
        prev_row = dm.get(prev_date)
        t_row = dm.get(target_date)
        if not prev_row or not t_row:
            continue
        if prev_row.get('isST') == 1 or t_row.get('isST') == 1:
            continue
        name = (t_row.get('code_name') or prev_row.get('code_name') or '')
        if 'ST' in name.upper():
            continue
        prev_pc = sv(prev_row.get('preclose'))
        prev_high = sv(prev_row.get('high'))
        prev_close = sv(prev_row.get('close'))
        if prev_pc is None or prev_pc == 0 or prev_high is None or prev_close is None:
            continue
        if not hit_limit_up(prev_high, prev_pc, code):
            continue
        if closed_limit_up(prev_close, prev_pc, code):
            continue
        if is_one_word_board(t_row):
            continue
        t_pc = sv(t_row.get('preclose'))
        t_open = sv(t_row.get('open'))
        if t_pc is None or t_pc == 0 or t_open is None:
            continue
        if open_at_limit_up(t_open, t_pc, code):
            continue
        opr = sv(t_row.get('open_rate'))
        if opr is None or opr < T_OPEN_RATE_MIN or opr > T_OPEN_RATE_MAX:
            continue
        if sv(t_row.get('hour1_open')) is None:
            continue

        prev_close_rate = sv(prev_row.get('close_rate'))
        prev_turn = sv(prev_row.get('turn'))

        candidates.append({
            'code': code,
            'name': name,
            'target_date': target_date,
            'prev_date': prev_date,
            't_idx': t_idx,
            'open_rate': opr,
            'turn': sv(t_row.get('turn')),
            'prev_turn': prev_turn,
            'prev_close_rate': prev_close_rate,
            'hour1_close_rate': sv(t_row.get('hour1_close_rate')),
            'prev_red_ratio': prev_red_ratio,
        })
    return candidates


def collect_hour_data(by_code, all_dates, candidates):
    """收集T日+T+1+T+2的全部hour级OHLC"""
    records = []
    for c in candidates:
        code = c['code']
        t_idx = c['t_idx']
        rec = dict(c)
        for off in (0, 1, 2):
            di = t_idx + off
            if di >= len(all_dates):
                continue
            d = all_dates[di]
            row = by_code.get(code, {}).get(d)
            if not row:
                continue
            for h in (1, 2, 3, 4):
                rec[f'd{off}_h{h}_open'] = sv(row.get(f'hour{h}_open'))
                rec[f'd{off}_h{h}_high'] = sv(row.get(f'hour{h}_high'))
                rec[f'd{off}_h{h}_low'] = sv(row.get(f'hour{h}_low'))
                rec[f'd{off}_h{h}_close'] = sv(row.get(f'hour{h}_close'))
        records.append(rec)
    return records


# ============== 限价买入方案 ==============

def limit_buy_plan_a(rec):
    """方案A: hour1观察 -> hour2限价抄底(h1_close-1%)
    条件: hour1_close_rate > -3%
    限价: hour1_close * 0.99
    验证: hour2_low <= limit_buy
    """
    h1_close = rec.get('d0_h1_close')
    h1_cr = rec.get('hour1_close_rate')
    h2_low = rec.get('d0_h2_low')
    if h1_close is None or h1_cr is None or h2_low is None:
        return None, False
    if h1_cr <= -3.0:
        return None, False  # hour1崩盘,不买
    limit_buy = h1_close * 0.99
    if h2_low <= limit_buy:
        return limit_buy, True
    return limit_buy, False  # 未触及限价


def limit_buy_plan_b(rec):
    """方案B: hour1观察 -> hour2限价(h1_close-2%)"""
    h1_close = rec.get('d0_h1_close')
    h1_cr = rec.get('hour1_close_rate')
    h2_low = rec.get('d0_h2_low')
    if h1_close is None or h1_cr is None or h2_low is None:
        return None, False
    if h1_cr <= -3.0:
        return None, False
    limit_buy = h1_close * 0.98
    if h2_low <= limit_buy:
        return limit_buy, True
    return limit_buy, False


def limit_buy_plan_c(rec):
    """方案C: hour1+hour2观察 -> hour3限价(min(h1_close,h2_close)-1%)"""
    h1_close = rec.get('d0_h1_close')
    h2_close = rec.get('d0_h2_close')
    h3_low = rec.get('d0_h3_low')
    if h1_close is None or h2_close is None or h3_low is None:
        return None, False
    limit_buy = min(h1_close, h2_close) * 0.99
    if h3_low <= limit_buy:
        return limit_buy, True
    return limit_buy, False


def limit_buy_plan_d(rec):
    """方案D: hour2_open市价单(对照基线)"""
    h2_open = rec.get('d0_h2_open')
    if h2_open is None:
        return None, False
    return h2_open, True  # 市价单总是成交


BUY_PLANS = [
    ('A(h1-1%)', limit_buy_plan_a),
    ('B(h1-2%)', limit_buy_plan_b),
    ('C(h3限)', limit_buy_plan_c),
    ('D(h2_open)', limit_buy_plan_d),
]


# ============== 限价卖出机制 ==============

def get_exit_hours(buy_plan_label):
    """根据买入方案确定卖出起始
    A/B/D: T日hour2买入 -> T+1起可卖
    C: T日hour3买入 -> T+1起可卖
    """
    # T+1合规: 全部从T+1开始卖
    # 持有上限到T+2_h4
    hours = []
    for off in (1, 2):  # T+1, T+2
        for h in (1, 2, 3, 4):
            hours.append((off, h))
    return hours


def simulate_limit_sell(rec, buy_price, sl_pct, tp_pct, hold_max_off=2):
    """限价止盈止损模拟
    止盈: limit_sell = buy_price*(1+tp_pct), 验证hourX_high >= limit_sell
    止损: stop_price = buy_price*(1-sl_pct), 验证hourX_low <= stop_price
    强平: T+hold_max_off日h4_open
    """
    if buy_price is None or buy_price <= 0:
        return None
    limit_sell = buy_price * (1 + tp_pct / 100.0)
    stop_price = buy_price * (1 - sl_pct / 100.0)

    for off in (1, 2):
        if off > hold_max_off:
            break
        for h in (1, 2, 3, 4):
            h_high = rec.get(f'd{off}_h{h}_high')
            h_low = rec.get(f'd{off}_h{h}_low')
            h_open = rec.get(f'd{off}_h{h}_open')
            if h_high is None or h_low is None:
                continue
            # 优先级: 如果open>buy_price先查止盈; 否则先查止损
            if h_open is not None and h_open >= buy_price:
                # 先检查止盈
                if h_high >= limit_sell:
                    return (limit_sell - buy_price) / buy_price * 100.0
                if h_low <= stop_price:
                    return (stop_price - buy_price) / buy_price * 100.0
            else:
                # 先检查止损
                if h_low <= stop_price:
                    return (stop_price - buy_price) / buy_price * 100.0
                if h_high >= limit_sell:
                    return (limit_sell - buy_price) / buy_price * 100.0

    # 强平: T+hold_max_off日h4_open
    force_price = rec.get(f'd{hold_max_off}_h4_open')
    if force_price is not None:
        return (force_price - buy_price) / buy_price * 100.0
    # 尝试T+hold_max_off日最后有效open
    for h in (4, 3, 2, 1):
        p = rec.get(f'd{hold_max_off}_h{h}_open')
        if p is not None:
            return (p - buy_price) / buy_price * 100.0
    return None


def simulate_limit_trailing(rec, buy_price, sl_pct, trailing_pct, hold_max_off=2):
    """限价Trailing止盈
    peak: 持仓期间hourX_high的最高值
    trailing_sell = peak * (1 - trailing_pct)
    触发: hourX_low <= trailing_sell
    止损: hourX_low <= stop_price
    """
    if buy_price is None or buy_price <= 0:
        return None
    stop_price = buy_price * (1 - sl_pct / 100.0)
    trail_drop = trailing_pct / 100.0
    peak = buy_price  # 初始peak = 买入价

    for off in (1, 2):
        if off > hold_max_off:
            break
        for h in (1, 2, 3, 4):
            h_high = rec.get(f'd{off}_h{h}_high')
            h_low = rec.get(f'd{off}_h{h}_low')
            if h_high is None or h_low is None:
                continue
            # 更新peak(用当前hour的high)
            if h_high > peak:
                peak = h_high
            # 计算trailing触发价
            trailing_sell = peak * (1 - trail_drop)
            # 检查止损
            if h_low <= stop_price:
                return (stop_price - buy_price) / buy_price * 100.0
            # 检查trailing触发
            if h_low <= trailing_sell and peak > buy_price:
                # trailing卖出价
                sell_p = trailing_sell
                return (sell_p - buy_price) / buy_price * 100.0

    # 强平
    force_price = rec.get(f'd{hold_max_off}_h4_open')
    if force_price is not None:
        return (force_price - buy_price) / buy_price * 100.0
    for h in (4, 3, 2, 1):
        p = rec.get(f'd{hold_max_off}_h{h}_open')
        if p is not None:
            return (p - buy_price) / buy_price * 100.0
    return None


# ============== 统计工具 ==============

def pct_return(buy, sell):
    if buy is None or sell is None or buy <= 0:
        return None
    return (sell - buy) / buy * 100.0


def stat_returns(returns):
    rs = [r for r in returns if r is not None]
    n = len(rs)
    if n == 0:
        return {'n': 0, 'mean': None, 'median': None, 'win_rate': None,
                'pl_ratio': None, 'avg_win': 0.0, 'avg_loss': 0.0}
    mean = statistics.mean(rs)
    median = statistics.median(rs)
    wins = [r for r in rs if r > 0]
    losses = [r for r in rs if r <= 0]
    win_rate = len(wins) / n * 100.0
    avg_win = statistics.mean(wins) if wins else 0.0
    avg_loss = statistics.mean(losses) if losses else 0.0
    pl_ratio = (avg_win / abs(avg_loss)) if avg_loss != 0 else None
    return {
        'n': n, 'mean': mean, 'median': median,
        'win_rate': win_rate, 'pl_ratio': pl_ratio,
        'avg_win': avg_win, 'avg_loss': avg_loss,
    }


# ============== 分析模块 ==============

def analyze_buy_plans(records):
    """对比4种买入方案"""
    print('\n--- 买入方案对比(全量候选) ---')
    print(f'{"方案":<12s} | {"实际成交":>6s} | {"填充率":>6s} | {"均收益":>8s} | {"胜率":>6s} | {"月化":>8s} | {"中位":>8s}')
    print('-' * 72)

    plan_results = {}
    for label, plan_fn in BUY_PLANS:
        filled = 0
        total = 0
        returns_tp5sl3 = []  # 默认: TP5% SL3%
        for rec in records:
            total += 1
            buy_price, is_filled = plan_fn(rec)
            if not is_filled or buy_price is None:
                continue
            filled += 1
            ret = simulate_limit_sell(rec, buy_price, 3.0, 5.0, hold_max_off=2)
            if ret is not None:
                returns_tp5sl3.append(ret)

        s = stat_returns(returns_tp5sl3)
        fill_rate = filled / total * 100.0 if total > 0 else 0
        monthly = (s['mean'] * MONTHLY_TRADE_DAYS / 1.5) if s['mean'] is not None else None
        mean_s = f'{s["mean"]:+.2f}%' if s['mean'] is not None else 'N/A'
        med_s = f'{s["median"]:+.2f}%' if s['median'] is not None else 'N/A'
        wr_s = f'{s["win_rate"]:.1f}%' if s['win_rate'] is not None else 'N/A'
        mo_s = f'{monthly:+.1f}%' if monthly is not None else 'N/A'
        print(f'{label:<12s} | {filled:>6d} | {fill_rate:>5.1f}% | {mean_s:>8s} | {wr_s:>6s} | {mo_s:>8s} | {med_s:>8s}')
        plan_results[label] = {'filled': filled, 'fill_rate': fill_rate, 'stat': s, 'monthly': monthly}

    return plan_results


def analyze_sl_tp_grid(records, plan_fn, plan_label, hold_max_off=2):
    """针对特定买入方案, 遍历止盈止损网格"""
    sl_list = [2.0, 3.0, 4.0, 5.0]
    tp_list = [3.0, 4.0, 5.0, 6.0, 8.0]
    trail_list = [2.0, 3.0]

    rows = []
    for sl in sl_list:
        for tp in tp_list:
            rets = []
            for rec in records:
                buy_price, is_filled = plan_fn(rec)
                if not is_filled or buy_price is None:
                    continue
                ret = simulate_limit_sell(rec, buy_price, sl, tp, hold_max_off)
                if ret is not None:
                    rets.append(ret)
            s = stat_returns(rets)
            monthly = (s['mean'] * MONTHLY_TRADE_DAYS / 1.5) if s['mean'] is not None else None
            rows.append({'type': 'SL+TP', 'sl': -sl, 'tp': tp, 'trail': None,
                         'monthly': monthly, **s})

        for tr in trail_list:
            rets = []
            for rec in records:
                buy_price, is_filled = plan_fn(rec)
                if not is_filled or buy_price is None:
                    continue
                ret = simulate_limit_trailing(rec, buy_price, sl, tr, hold_max_off)
                if ret is not None:
                    rets.append(ret)
            s = stat_returns(rets)
            monthly = (s['mean'] * MONTHLY_TRADE_DAYS / 1.5) if s['mean'] is not None else None
            rows.append({'type': 'SL+TRAIL', 'sl': -sl, 'tp': None, 'trail': tr,
                         'monthly': monthly, **s})

    rows.sort(key=lambda x: (x['monthly'] if x['monthly'] is not None else -1e9), reverse=True)
    return rows


def print_sl_tp_grid(rows, plan_label, hold_max_off):
    print(f'\n--- 最优买入方案({plan_label})止盈止损网格 (持有上限T+{hold_max_off}_h4) ---')
    print(f'{"止损":>6s} | {"止盈/Trail":>10s} | {"胜率":>6s} | {"均收益":>8s} | {"月化":>8s} | {"盈亏比":>6s} | {"N":>5s}')
    print('-' * 68)
    for r in rows[:20]:
        wr = f'{r["win_rate"]:.1f}%' if r['win_rate'] is not None else 'N/A'
        mean = f'{r["mean"]:+.2f}%' if r['mean'] is not None else 'N/A'
        monthly = f'{r["monthly"]:+.1f}%' if r['monthly'] is not None else 'N/A'
        pl = f'{r["pl_ratio"]:.2f}' if r['pl_ratio'] is not None else 'N/A'
        if r['type'] == 'SL+TP':
            tptext = f'+{r["tp"]:.1f}%'
        else:
            tptext = f'tr{r["trail"]:.1f}%'
        print(f'{r["sl"]:+5.1f}% | {tptext:>10s} | {wr:>6s} | {mean:>8s} | {monthly:>8s} | {pl:>6s} | {r["n"]:>5d}')


def analyze_filters(records, plan_fn, plan_label):
    """条件过滤增强"""
    filters = [
        ('无过滤(全集)', lambda r: True),
        ('T-1 cr∈[3%,5%)', lambda r: r.get('prev_close_rate') is not None and 3 <= r['prev_close_rate'] < 5),
        ('T-1 cr∈[5%,8%)', lambda r: r.get('prev_close_rate') is not None and 5 <= r['prev_close_rate'] < 8),
        ('T-1 cr∈[8%,9.5%]', lambda r: r.get('prev_close_rate') is not None and 8 <= r['prev_close_rate'] <= 9.5),
        ('T-1 turn>10%', lambda r: r.get('prev_turn') is not None and r['prev_turn'] > 10),
        ('T-1 turn>15%', lambda r: r.get('prev_turn') is not None and r['prev_turn'] > 15),
        ('T-1 turn>20%', lambda r: r.get('prev_turn') is not None and r['prev_turn'] > 20),
        ('T open<3%', lambda r: r.get('open_rate') is not None and r['open_rate'] < 3),
        ('T open<5%', lambda r: r.get('open_rate') is not None and r['open_rate'] < 5),
        ('T-1 red_ratio>45', lambda r: r.get('prev_red_ratio') is not None and r['prev_red_ratio'] > 45),
        ('T-1 red_ratio>50', lambda r: r.get('prev_red_ratio') is not None and r['prev_red_ratio'] > 50),
        ('h1_cr>-3% & open<5%', lambda r: (r.get('hour1_close_rate') is not None and r['hour1_close_rate'] > -3)
                                           and (r.get('open_rate') is not None and r['open_rate'] < 5)),
        ('cr>=5%&turn>10%&open<5%', lambda r: (r.get('prev_close_rate') is not None and r['prev_close_rate'] >= 5)
                                               and (r.get('prev_turn') is not None and r['prev_turn'] > 10)
                                               and (r.get('open_rate') is not None and r['open_rate'] < 5)),
    ]

    # 对每个过滤条件 x 多组风控 网格 (含高胜率组合)
    risk_configs = [
        ('SL3%+TP5%', 3.0, 5.0, 'tp'),
        ('SL3%+TP4%', 3.0, 4.0, 'tp'),
        ('SL3%+TP3%', 3.0, 3.0, 'tp'),
        ('SL3%+TP6%', 3.0, 6.0, 'tp'),
        ('SL4%+TP4%', 4.0, 4.0, 'tp'),
        ('SL4%+TP3%', 4.0, 3.0, 'tp'),
        ('SL5%+TP3%', 5.0, 3.0, 'tp'),
        ('SL5%+TP4%', 5.0, 4.0, 'tp'),
        ('SL5%+TP5%', 5.0, 5.0, 'tp'),
        ('SL2%+TP5%', 2.0, 5.0, 'tp'),
        ('SL4%+TP5%', 4.0, 5.0, 'tp'),
        ('SL3%+Tr3%', 3.0, 3.0, 'trail'),
        ('SL3%+Tr2%', 3.0, 2.0, 'trail'),
        ('SL2%+Tr3%', 2.0, 3.0, 'trail'),
        ('SL2%+Tr2%', 2.0, 2.0, 'trail'),
    ]

    results = []
    for fname, ffn in filters:
        subset = [r for r in records if ffn(r)]
        if len(subset) < 10:
            continue
        for rname, sl, tp_or_trail, rtype in risk_configs:
            rets = []
            for rec in subset:
                buy_price, is_filled = plan_fn(rec)
                if not is_filled or buy_price is None:
                    continue
                if rtype == 'tp':
                    ret = simulate_limit_sell(rec, buy_price, sl, tp_or_trail, hold_max_off=2)
                else:
                    ret = simulate_limit_trailing(rec, buy_price, sl, tp_or_trail, hold_max_off=2)
                if ret is not None:
                    rets.append(ret)
            s = stat_returns(rets)
            monthly = (s['mean'] * MONTHLY_TRADE_DAYS / 1.5) if s['mean'] is not None else None
            results.append({
                'filter': fname, 'risk': rname,
                'n': s['n'], 'mean': s['mean'], 'win_rate': s['win_rate'],
                'pl_ratio': s['pl_ratio'], 'monthly': monthly,
            })

    results.sort(key=lambda x: ((x['monthly'] or -1e9), (x['win_rate'] or 0)), reverse=True)

    print(f'\n--- 条件过滤增强 (买入={plan_label}) ---')
    print(f'{"过滤条件":<26s} | {"风控":<12s} | {"N":>4s} | {"均收益":>8s} | {"胜率":>6s} | {"月化":>8s} | {"盈亏比":>6s}')
    print('-' * 85)
    for r in results[:25]:
        mean = f'{r["mean"]:+.2f}%' if r['mean'] is not None else 'N/A'
        wr = f'{r["win_rate"]:.1f}%' if r['win_rate'] is not None else 'N/A'
        monthly = f'{r["monthly"]:+.1f}%' if r['monthly'] is not None else 'N/A'
        pl = f'{r["pl_ratio"]:.2f}' if r['pl_ratio'] is not None else 'N/A'
        print(f'{r["filter"]:<26s} | {r["risk"]:<12s} | {r["n"]:>4d} | {mean:>8s} | {wr:>6s} | {monthly:>8s} | {pl:>6s}')

    # 找达标组合: 月化>10%或(月化>8%且胜率>45%)
    qualified = [r for r in results
                 if r['monthly'] is not None and r['monthly'] >= 10.0
                 and r['win_rate'] is not None and r['win_rate'] >= 45.0
                 and r['n'] >= 15]
    # 也找年化>100%组合 (月化>8.33%)
    annual_qualified = [r for r in results
                        if r['monthly'] is not None and r['monthly'] >= 8.33
                        and r['n'] >= 30]
    return results, qualified, annual_qualified


# ============== 主流程 ==============

def run_analysis(months_str, auto_extend=True):
    months = parse_months(months_str)
    print('=' * 80)
    print(f'============ ZhaBan+限价单 分析 ============')
    print(f'月份: {months_str}, 目标月: {len(months)}')
    print(f'限价买入机制: A(h1-1%), B(h1-2%), C(h3限价), D(h2_open基线)')
    print(f'限价卖出: 止盈(hourX_high>=P), 止损(hourX_low<=P), Trailing(peak追踪)')
    print('=' * 80)

    conn = sqlite3.connect(DB_PATH)
    all_dates, date_idx, target_dates, by_code, red_ratio_by_date = load_data(conn, months)
    conn.close()

    if not target_dates:
        print('[警告] 无目标交易日数据')
        return None

    all_candidates = []
    for td in target_dates:
        cs = find_candidates(by_code, all_dates, date_idx, td, red_ratio_by_date)
        all_candidates.extend(cs)

    print(f'\n候选股总数: {len(all_candidates)} 只  '
          f'日均: {len(all_candidates)/max(1,len(target_dates)):.1f} 只  '
          f'交易日: {len(target_dates)} 天')

    if not all_candidates:
        print('[警告] 无候选股')
        return None

    records = collect_hour_data(by_code, all_dates, all_candidates)
    print(f'hour数据收集完成: {len(records)} 条记录')

    # 1. 买入方案对比
    plan_results = analyze_buy_plans(records)

    # 2. 找最优买入方案进行深度分析
    best_plan_label = None
    best_monthly = -1e9
    for label, info in plan_results.items():
        if info['monthly'] is not None and info['monthly'] > best_monthly and info['filled'] >= 10:
            best_monthly = info['monthly']
            best_plan_label = label

    # 如果限价方案都不好, 也分析A方案
    if best_plan_label is None:
        best_plan_label = 'A(h1-1%)'

    # 获取最佳方案的plan_fn
    plan_fn_map = {label: fn for label, fn in BUY_PLANS}
    best_plan_fn = plan_fn_map[best_plan_label]

    # 对所有方案(不止最优)都跑网格
    print(f'\n>>> 选择买入方案 {best_plan_label} 进行深度网格分析')
    grid_rows = analyze_sl_tp_grid(records, best_plan_fn, best_plan_label, hold_max_off=2)
    print_sl_tp_grid(grid_rows, best_plan_label, 2)

    # 同时对D基线也跑网格对比
    if best_plan_label != 'D(h2_open)':
        grid_d = analyze_sl_tp_grid(records, limit_buy_plan_d, 'D(h2_open)', hold_max_off=2)
        print_sl_tp_grid(grid_d, 'D(h2_open)', 2)

    # 3. 条件过滤增强
    all_filter_results, qualified, annual_qualified = analyze_filters(records, best_plan_fn, best_plan_label)

    # 4. 推荐总结
    print('\n' + '=' * 80)
    print('========== 最终推荐 ==========')
    if qualified:
        print(f'[达标组合] 共 {len(qualified)} 组 (月化>=10% 且 胜率>=45%):')
        for q in qualified[:8]:
            pl = f'{q["pl_ratio"]:.2f}' if q["pl_ratio"] else 'N/A'
            print(f'  过滤:{q["filter"]:<26s} 风控:{q["risk"]:<12s} '
                  f'N={q["n"]:>4d} 均={q["mean"]:+.2f}% 胜={q["win_rate"]:.1f}% '
                  f'月化={q["monthly"]:+.1f}% PL={pl}')
    elif annual_qualified:
        print(f'[年化>100%组合] 共 {len(annual_qualified)} 组 (月化>=8.33%):')
        for q in annual_qualified[:8]:
            pl = f'{q["pl_ratio"]:.2f}' if q["pl_ratio"] else 'N/A'
            wr = f'{q["win_rate"]:.1f}%' if q['win_rate'] is not None else 'N/A'
            print(f'  过滤:{q["filter"]:<26s} 风控:{q["risk"]:<12s} '
                  f'N={q["n"]:>4d} 均={q["mean"]:+.2f}% 胜={wr} '
                  f'月化={q["monthly"]:+.1f}% 年化≈{q["monthly"]*12:.0f}% PL={pl}')
    else:
        print('[未达标] 无组合满足月化>=8.33%')
        if all_filter_results:
            print('\n前5名:')
            for q in all_filter_results[:5]:
                mo = f'{q["monthly"]:+.1f}%' if q['monthly'] is not None else 'N/A'
                wr = f'{q["win_rate"]:.1f}%' if q['win_rate'] is not None else 'N/A'
                mean = f'{q["mean"]:+.2f}%' if q['mean'] is not None else 'N/A'
                print(f'  {q["filter"]:<26s} {q["risk"]:<12s} N={q["n"]} 均={mean} 胜={wr} 月化={mo}')

    print('=' * 80)

    result = {
        'months': months_str,
        'candidate_count': len(all_candidates),
        'qualified': qualified,
        'annual_qualified': annual_qualified,
        'best_plan': best_plan_label,
        'all_filter_results': all_filter_results,
    }

    # 自动扩展逻辑: 单月达标 -> 2025全年
    if auto_extend and (qualified or annual_qualified) and len(months) <= 3:
        top = (qualified or annual_qualified)[0]
        print(f'\n>>> 达标! 月化={top["monthly"]:+.1f}% 胜率={top["win_rate"]:.1f}%')
        if len(months) == 1:
            print(f'>>> 自动扩展至 2025 全年...')
            run_analysis('2025', auto_extend=False)

    return result


def main():
    if len(sys.argv) < 2:
        print('用法: python3 scripts/strategy_zhaban_limit_order.py <月份>')
        print('示例: 2026-04 / 2025-01,2025-02 / 2025')
        sys.exit(1)

    result = run_analysis(sys.argv[1])

    # 如果全年达标,继续扩展
    if result:
        months = parse_months(sys.argv[1])
        annual_q = result.get('annual_qualified') or result.get('qualified')
        if len(months) == 12 and annual_q:
            top = annual_q[0]
            if top['monthly'] is not None:
                annual = top['monthly'] * 12
                print(f'\n>>> 全年年化估算: {annual:.1f}%')
                if annual > 100:
                    year = int(months[0][:4])
                    print(f'>>> 年化>100%, 继续验证 {year-1} ...')
                    run_analysis(str(year - 1), auto_extend=False)
                    print(f'\n>>> 继续验证 {year-2} ...')
                    run_analysis(str(year - 2), auto_extend=False)


if __name__ == '__main__':
    main()
