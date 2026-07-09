#!/usr/bin/env python3
"""炸板修复(ZhaBan) 策略 Phase-1+2 联合筛选与统计分析

核心概念:
  T-1 日盘中触及涨停(round(high/preclose,2)>=阈值)但收盘未封住涨停,
  T 日尝试再次冲击涨停 -> 修复性行情.

严格合规:
  - 买/卖价仅取 hourX_open (绝不使用 hour 内 high/low/close 作为成交价)
  - T+1 合规: T 日买入最早 T+1 卖出
  - 涨停判定: round(close/preclose, 2) / round(high/preclose, 2)
  - 大盘过滤使用 T-1 日 red_ratio (不使用未来数据)

用法:
  python3 scripts/strategy_zhaban_clean_phase12.py 2026-04
  python3 scripts/strategy_zhaban_clean_phase12.py 2025-01,2025-02,2025-03
  python3 scripts/strategy_zhaban_clean_phase12.py 2025  # 全年 (12 个月)
"""
import sqlite3
import sys
import math
import statistics
from collections import defaultdict
from datetime import datetime, timedelta

# ============== 配置区 ==============
DB_PATH = '/home/AIWealth/data/stocks.db'

# T 日 open 限制: 不能开盘就涨停 (会买不进)
# 开盘跌幅过深也排除 (代表恐慌, 修复行情低概率)
T_OPEN_RATE_MIN = -5.0
T_OPEN_RATE_MAX = 9.5   # 不能开盘就涨停, 主板 10% 涨停, 留 0.5% 缓冲

# 月化收益换算: 假设持仓 1 个交易日(T 日买入 T+1 卖出), 每月 22 个交易日
MONTHLY_TRADE_DAYS = 22

# 大盘 red_ratio 数据源 (上证综指)
INDEX_CODE = 'sh.000001'
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
    """T-1 日盘中触及涨停: round(high/preclose,2) >= 阈值"""
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
    """支持 '2026-04' / '2025-01,2025-02' / '2025' 三种形式, 返回月份列表"""
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
    """合并加载多个月份所需的全部 stock_kline + index_kline 数据."""
    cur = conn.cursor()
    months = sorted(set(target_months))
    first = month_bounds(months[0])[0]
    last_first, last_next = month_bounds(months[-1])
    # 前后留 30 / 20 日缓冲
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

    # 加载大盘 red_ratio
    cur.execute(
        'SELECT date, red_ratio FROM index_kline WHERE code=? AND date>=? AND date<?',
        (INDEX_CODE, dt_lo, dt_hi),
    )
    red_ratio_by_date = {r[0]: sv(r[1]) for r in cur.fetchall()}

    print(f'[加载] 区间 {dt_lo} ~ {dt_hi}  交易日={len(all_dates)}  '
          f'目标日={len(target_dates)}  股票数={len(by_code)}  行数={n_rows}  '
          f'red_ratio日数={len(red_ratio_by_date)}')
    return all_dates, date_idx, target_dates, by_code, red_ratio_by_date


def find_candidates(by_code, all_dates, date_idx, target_date, red_ratio_by_date):
    """筛选 target_date 候选股 (炸板修复信号)"""
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
        # 非 ST
        if prev_row.get('isST') == 1 or t_row.get('isST') == 1:
            continue
        name = (t_row.get('code_name') or prev_row.get('code_name') or '')
        if 'ST' in name.upper():
            continue
        # T-1 信号: 盘中触及涨停 + 收盘未封住
        prev_pc = sv(prev_row.get('preclose'))
        prev_high = sv(prev_row.get('high'))
        prev_close = sv(prev_row.get('close'))
        if prev_pc is None or prev_pc == 0 or prev_high is None or prev_close is None:
            continue
        if not hit_limit_up(prev_high, prev_pc, code):
            continue
        if closed_limit_up(prev_close, prev_pc, code):
            continue  # 收盘封住, 不是炸板
        # T 日过滤
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
        # hour1_open 必须有效 (作为最早买点)
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


def get_hour_open(by_code, all_dates, code, day_offset_from_t, t_idx, hour):
    di = t_idx + day_offset_from_t
    if di < 0 or di >= len(all_dates):
        return None
    d = all_dates[di]
    row = by_code.get(code, {}).get(d)
    if not row:
        return None
    return sv(row.get(f'hour{hour}_open'))


def collect_trade_records(by_code, all_dates, candidates):
    """收集 T 日 hour1~4 + T+1/T+2/T+3 各 hour_open"""
    records = []
    for c in candidates:
        code = c['code']
        t_idx = c['t_idx']
        rec = dict(c)
        for off in (0, 1, 2, 3):
            for h in (1, 2, 3, 4):
                rec[f'd{off}_h{h}_open'] = get_hour_open(by_code, all_dates, code, off, t_idx, h)
        records.append(rec)
    return records


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


# 买入方案: (label, day_offset, hour)
BUY_PLANS = [
    ('A(T_h1)', 0, 1),
    ('B(T_h2)', 0, 2),
    ('C(T_h3)', 0, 3),
]


def get_sell_points(buy_offset):
    """T+1 合规: 卖出 offset > buy_offset"""
    base = buy_offset + 1
    pts = []
    for off in (base, base + 1):
        for h in (1, 2, 3, 4):
            label = f'+{off}_h{h}'
            pts.append((label, off, h))
    return pts


def buy_price_of(rec, plan):
    _, off, h = plan
    return rec.get(f'd{off}_h{h}_open')


def sell_price_of(rec, sell_pt):
    _, off, h = sell_pt
    return rec.get(f'd{off}_h{h}_open')


# ============== 1. 买卖时点矩阵 ==============

def analyze_buy_sell_matrix(records):
    rows = []
    for plan in BUY_PLANS:
        b_label, b_off, _ = plan
        for sp in get_sell_points(b_off):
            sell_label = f'T{sp[0]}'
            rets = [pct_return(buy_price_of(r, plan), sell_price_of(r, sp)) for r in records]
            s = stat_returns(rets)
            rows.append({'buy': b_label, 'sell': sell_label, 'sell_off': sp[1], **s})
    return rows


def print_matrix(rows):
    print()
    print('--- Phase-2.1 买入时点 x 卖出时点 收益矩阵 ---')
    print(f'{"买入":<10s} | {"卖出":<8s} | {"均收益":>8s} | {"中位":>8s} | {"胜率":>6s} | {"盈亏比":>6s} | {"N":>6s}')
    print('-' * 70)
    for r in rows:
        mean = f'{r["mean"]:+.2f}%' if r['mean'] is not None else 'N/A'
        med = f'{r["median"]:+.2f}%' if r['median'] is not None else 'N/A'
        wr = f'{r["win_rate"]:.1f}%' if r['win_rate'] is not None else 'N/A'
        pl = f'{r["pl_ratio"]:.2f}' if r['pl_ratio'] is not None else 'N/A'
        print(f'{r["buy"]:<10s} | {r["sell"]:<8s} | {mean:>8s} | {med:>8s} | {wr:>6s} | {pl:>6s} | {r["n"]:>6d}')


# ============== 2. 止盈止损 / Trailing 网格 (方案 B 为主) ==============

def build_exit_sequence(rec, buy_off, hold_max_off):
    """生成买入后到 hold_max_off 日 h4 的所有可用 hourX_open 序列"""
    seq = []
    for off in range(buy_off + 1, hold_max_off + 1):
        for h in (1, 2, 3, 4):
            p = rec.get(f'd{off}_h{h}_open')
            seq.append(((off, h), p))
    return seq


def simulate_sl_tp(rec, plan, sl_pct, tp_pct, hold_max_off):
    """固定止盈止损模拟"""
    bp = buy_price_of(rec, plan)
    if bp is None or bp <= 0:
        return None
    sl_price = bp * (1 + sl_pct / 100.0)
    tp_price = bp * (1 + tp_pct / 100.0)
    seq = build_exit_sequence(rec, plan[1], hold_max_off)
    for _, p in seq:
        if p is None:
            continue
        if p <= sl_price or p >= tp_price:
            return pct_return(bp, p)
    for _, p in reversed(seq):
        if p is not None:
            return pct_return(bp, p)
    return None


def simulate_trailing(rec, plan, sl_pct, trailing_pct, hold_max_off):
    """Trailing 止盈 + 固定止损 模拟
    - peak_open 持续更新为所有已观察 hourX_open 的最高值 (包括买入价)
    - 当 hourX_open <= peak * (1 - trailing_pct) 触发 trailing 卖出
    - 当 hourX_open <= bp * (1 + sl_pct) 触发固定止损
    - 到 hold_max_off 上限仍未触发, 以最后一个有效 hourX_open 卖出
    """
    bp = buy_price_of(rec, plan)
    if bp is None or bp <= 0:
        return None
    sl_price = bp * (1 + sl_pct / 100.0)
    trail_drop = trailing_pct / 100.0
    peak = bp
    seq = build_exit_sequence(rec, plan[1], hold_max_off)
    last_valid = None
    for _, p in seq:
        if p is None:
            continue
        last_valid = p
        # 固定止损优先
        if p <= sl_price:
            return pct_return(bp, p)
        # Trailing 触发
        if p <= peak * (1 - trail_drop):
            return pct_return(bp, p)
        if p > peak:
            peak = p
    if last_valid is not None:
        return pct_return(bp, last_valid)
    return None


def analyze_sl_tp_grid(records, plan, hold_max_off, monthly_hold_days):
    sl_list = [-2.0, -3.0, -4.0, -5.0]
    tp_list = [3.0, 4.0, 5.0, 6.0, 8.0, 10.0]
    rows = []
    for sl in sl_list:
        for tp in tp_list:
            rets = [simulate_sl_tp(r, plan, sl, tp, hold_max_off) for r in records]
            s = stat_returns(rets)
            monthly = (s['mean'] * MONTHLY_TRADE_DAYS / monthly_hold_days) if s['mean'] is not None else None
            rows.append({'type': 'SL+TP', 'sl': sl, 'tp': tp,
                         'trail': None, 'hold': hold_max_off,
                         'monthly': monthly, **s})
    # Trailing 矩阵
    trail_list = [2.0, 3.0]
    for sl in sl_list:
        for tr in trail_list:
            rets = [simulate_trailing(r, plan, sl, tr, hold_max_off) for r in records]
            s = stat_returns(rets)
            monthly = (s['mean'] * MONTHLY_TRADE_DAYS / monthly_hold_days) if s['mean'] is not None else None
            rows.append({'type': 'SL+TRAIL', 'sl': sl, 'tp': None,
                         'trail': tr, 'hold': hold_max_off,
                         'monthly': monthly, **s})
    rows.sort(key=lambda x: (x['monthly'] if x['monthly'] is not None else -1e9), reverse=True)
    return rows


def print_sl_tp_grid(rows, plan_label, hold_max_off):
    print()
    print(f'--- Phase-2.2 止盈止损网格 (买入={plan_label}, 持有上限 T+{hold_max_off}_h4) ---')
    print(f'{"类型":<10s} | {"止损":>5s} | {"止盈/Trail":>10s} | {"胜率":>6s} | {"均收益":>8s} | {"月化":>8s} | {"盈亏比":>6s} | {"N":>6s}')
    print('-' * 80)
    for r in rows:
        wr = f'{r["win_rate"]:.1f}%' if r['win_rate'] is not None else 'N/A'
        mean = f'{r["mean"]:+.2f}%' if r['mean'] is not None else 'N/A'
        monthly = f'{r["monthly"]:+.1f}%' if r['monthly'] is not None else 'N/A'
        pl = f'{r["pl_ratio"]:.2f}' if r['pl_ratio'] is not None else 'N/A'
        if r['type'] == 'SL+TP':
            tptext = f'+{r["tp"]:.1f}%'
        else:
            tptext = f'tr{r["trail"]:.1f}%'
        print(f'{r["type"]:<10s} | {r["sl"]:+5.1f}% | {tptext:>10s} | {wr:>6s} | {mean:>8s} | {monthly:>8s} | {pl:>6s} | {r["n"]:>6d}')


# ============== 3. 条件过滤分析 ==============

def bucket_prev_close_rate(v):
    if v is None: return 'NA'
    if v < 0: return '(-inf,0%)'
    if v < 3: return '[0%,3%)'
    if v < 5: return '[3%,5%)'
    if v < 8: return '[5%,8%)'
    return '[8%,9.5%]'


def bucket_prev_turn(v):
    if v is None: return 'NA'
    if v <= 5: return '(0%,5%]'
    if v <= 10: return '(5%,10%]'
    if v <= 20: return '(10%,20%]'
    return '(20%,+inf)'


def bucket_open_rate(v):
    if v is None: return 'NA'
    if v < -3: return '(-inf,-3%)'
    if v < 0: return '[-3%,0%)'
    if v < 3: return '[0%,3%)'
    if v < 5: return '[3%,5%)'
    return '[5%,9.5%]'


def bucket_hour1_close_rate(v):
    if v is None: return 'NA'
    if v < -5: return '(-inf,-5%)'
    if v < -2: return '[-5%,-2%)'
    if v < 0: return '[-2%,0%)'
    if v < 3: return '[0%,3%)'
    if v < 5: return '[3%,5%)'
    return '[5%,+inf)'


def bucket_red_ratio(v):
    if v is None: return 'NA'
    if v < 30: return '[0,30)弱'
    if v < 50: return '[30,50)中'
    if v < 70: return '[50,70)强'
    return '[70,100]极强'


BUCKETS = [
    ('T-1 close_rate (炸板程度)', lambda r: bucket_prev_close_rate(r.get('prev_close_rate'))),
    ('T-1 turn (换手率)',         lambda r: bucket_prev_turn(r.get('prev_turn'))),
    ('T open_rate (开盘高低)',    lambda r: bucket_open_rate(r.get('open_rate'))),
    ('T hour1_close_rate',         lambda r: bucket_hour1_close_rate(r.get('hour1_close_rate'))),
    ('T-1 大盘 red_ratio',         lambda r: bucket_red_ratio(r.get('prev_red_ratio'))),
]


def analyze_buckets(records, key_fn, label, plan, sell_pt):
    buckets = defaultdict(list)
    for r in records:
        bp = buy_price_of(r, plan)
        sp = sell_price_of(r, sell_pt)
        ret = pct_return(bp, sp)
        if ret is None:
            continue
        buckets[key_fn(r)].append(ret)
    out = []
    for k, rs in buckets.items():
        s = stat_returns(rs)
        out.append({'group': k, **s})
    out.sort(key=lambda x: x['group'])
    print()
    print(f'--- Phase-2.3 条件过滤: {label}  (买={plan[0]} 卖={sell_pt[0]}) ---')
    print(f'{"分组":<18s} | {"N":>5s} | {"均收益":>8s} | {"中位":>8s} | {"胜率":>6s} | {"盈亏比":>6s}')
    print('-' * 64)
    for r in out:
        mean = f'{r["mean"]:+.2f}%' if r['mean'] is not None else 'N/A'
        med = f'{r["median"]:+.2f}%' if r['median'] is not None else 'N/A'
        wr = f'{r["win_rate"]:.1f}%' if r['win_rate'] is not None else 'N/A'
        pl = f'{r["pl_ratio"]:.2f}' if r['pl_ratio'] is not None else 'N/A'
        print(f'{str(r["group"]):<18s} | {r["n"]:>5d} | {mean:>8s} | {med:>8s} | {wr:>6s} | {pl:>6s}')
    return out


# ============== 4. 组合最优 (条件过滤 + 非对称风控) ==============

def analyze_best_combo(records):
    """以方案 B + 非对称风控 (紧止损 + trailing) 为主, 叠加条件过滤"""
    print()
    print('--- Phase-2.4 组合最优 (方案 B + 紧止损 + Trailing + 条件过滤) ---')
    plan = ('B(T_h2)', 0, 2)
    hold = 2  # T+2_h4 持有上限
    # 非对称风控候选
    risk_grids = [
        ('SL-3%+Tr3%', lambda r: simulate_trailing(r, plan, -3.0, 3.0, hold)),
        ('SL-3%+Tr2%', lambda r: simulate_trailing(r, plan, -3.0, 2.0, hold)),
        ('SL-2%+Tr3%', lambda r: simulate_trailing(r, plan, -2.0, 3.0, hold)),
        ('SL-4%+Tr3%', lambda r: simulate_trailing(r, plan, -4.0, 3.0, hold)),
        ('SL-3%+TP5%', lambda r: simulate_sl_tp(r, plan, -3.0, 5.0, hold)),
        ('SL-3%+TP8%', lambda r: simulate_sl_tp(r, plan, -3.0, 8.0, hold)),
    ]
    filters = [
        ('无过滤(全集)', lambda r: True),
        ('T-1 close_rate ∈ [3%,5%)',
         lambda r: r.get('prev_close_rate') is not None and 3 <= r['prev_close_rate'] < 5),
        ('T-1 close_rate ∈ [5%,8%)',
         lambda r: r.get('prev_close_rate') is not None and 5 <= r['prev_close_rate'] < 8),
        ('T-1 close_rate >= 5%',
         lambda r: r.get('prev_close_rate') is not None and r['prev_close_rate'] >= 5),
        ('T open_rate ∈ [0%,3%)',
         lambda r: r.get('open_rate') is not None and 0 <= r['open_rate'] < 3),
        ('T open_rate ∈ [3%,5%)',
         lambda r: r.get('open_rate') is not None and 3 <= r['open_rate'] < 5),
        ('T hour1_close_rate ∈ [0%,5%)',
         lambda r: r.get('hour1_close_rate') is not None and 0 <= r['hour1_close_rate'] < 5),
        ('T hour1_close_rate >= 0%',
         lambda r: r.get('hour1_close_rate') is not None and r['hour1_close_rate'] >= 0),
        ('T-1 turn > 10%',
         lambda r: r.get('prev_turn') is not None and r['prev_turn'] > 10),
        ('T-1 red_ratio >= 50',
         lambda r: r.get('prev_red_ratio') is not None and r['prev_red_ratio'] >= 50),
        ('强势复合: prev_cr>=3% & hour1_cr>=0%',
         lambda r: (r.get('prev_close_rate') is not None and r['prev_close_rate'] >= 3)
                   and (r.get('hour1_close_rate') is not None and r['hour1_close_rate'] >= 0)),
    ]

    results = []
    for fname, ffn in filters:
        subset = [r for r in records if ffn(r)]
        if len(subset) < 20:
            continue
        for rname, rfn in risk_grids:
            rets = [rfn(r) for r in subset]
            s = stat_returns(rets)
            # 估算持仓: 平均 1.5 天 (T+1_h2 ~ T+2_h2)
            monthly = (s['mean'] * MONTHLY_TRADE_DAYS / 1.5) if s['mean'] is not None else None
            results.append({
                'filter': fname, 'risk': rname,
                'n': s['n'], 'mean': s['mean'], 'win_rate': s['win_rate'],
                'pl_ratio': s['pl_ratio'], 'monthly': monthly,
                'subset_size': len(subset),
            })

    # 按月化 + 胜率 排序
    results.sort(key=lambda x: ((x['monthly'] or -1e9), (x['win_rate'] or 0)), reverse=True)
    print(f'{"过滤条件":<36s} | {"风控":<13s} | {"N":>4s} | {"均收益":>8s} | {"胜率":>6s} | {"月化":>8s} | {"盈亏比":>6s}')
    print('-' * 96)
    for r in results[:20]:
        mean = f'{r["mean"]:+.2f}%' if r['mean'] is not None else 'N/A'
        wr = f'{r["win_rate"]:.1f}%' if r['win_rate'] is not None else 'N/A'
        monthly = f'{r["monthly"]:+.1f}%' if r['monthly'] is not None else 'N/A'
        pl = f'{r["pl_ratio"]:.2f}' if r['pl_ratio'] is not None else 'N/A'
        print(f'{r["filter"]:<36s} | {r["risk"]:<13s} | {r["n"]:>4d} | {mean:>8s} | {wr:>6s} | {monthly:>8s} | {pl:>6s}')

    # 找满足月化 >=10% 且 胜率 >=50% 的最佳
    qualified = [r for r in results
                 if r['monthly'] is not None and r['monthly'] >= 10.0
                 and r['win_rate'] is not None and r['win_rate'] >= 50.0
                 and r['n'] >= 20]
    return results, qualified


# ============== 5. 推荐 ==============

def recommend(matrix_rows, grid_rows_b, best_combo_results, qualified):
    print()
    print('========== Phase-2.5 最优策略推荐 ==========')
    # 矩阵最优 (按月化)
    best_matrix = None
    for r in matrix_rows:
        if r['mean'] is None or r['win_rate'] is None:
            continue
        hold = 1.0 if r['sell_off'] == 1 else (r['sell_off'])
        monthly = r['mean'] * MONTHLY_TRADE_DAYS / hold
        cand = {**r, 'monthly': monthly}
        if (best_matrix is None) or (cand['monthly'] > best_matrix['monthly']):
            best_matrix = cand
    if best_matrix:
        pl = f'{best_matrix["pl_ratio"]:.2f}' if best_matrix["pl_ratio"] else 'N/A'
        print(f'[纯时点矩阵最优] 买={best_matrix["buy"]} 卖={best_matrix["sell"]} '
              f'均收益={best_matrix["mean"]:+.2f}% 胜率={best_matrix["win_rate"]:.1f}% '
              f'盈亏比={pl} 月化≈{best_matrix["monthly"]:+.1f}%')

    # 网格最优
    if grid_rows_b:
        top = grid_rows_b[0]
        pl = f'{top["pl_ratio"]:.2f}' if top["pl_ratio"] else 'N/A'
        tag = f'TP+{top["tp"]:.1f}%' if top['type'] == 'SL+TP' else f'TRAIL{top["trail"]:.1f}%'
        print(f'[网格最优] 方案B SL{top["sl"]:+.1f}% {tag} 持有≤T+{top["hold"]}_h4  '
              f'胜率={top["win_rate"]:.1f}% 均收益={top["mean"]:+.2f}% 月化≈{top["monthly"]:+.1f}% 盈亏比={pl}')

    if qualified:
        print(f'\n[组合达标策略 共 {len(qualified)} 组] (月化>=10% 且 胜率>=50%, N>=20):')
        for q in qualified[:5]:
            pl = f'{q["pl_ratio"]:.2f}' if q["pl_ratio"] else 'N/A'
            print(f'  - 过滤:{q["filter"]:<36s} 风控:{q["risk"]:<13s} '
                  f'N={q["n"]:>4d} 均={q["mean"]:+.2f}% 胜={q["win_rate"]:.1f}% '
                  f'月化≈{q["monthly"]:+.1f}% PL={pl}')
        top = qualified[0]
        print(f'\n>>> 综合推荐 <<<')
        print(f'  筛选: 炸板修复信号 + {top["filter"]}')
        print(f'  买入: T 日 hour2_open')
        print(f'  风控: {top["risk"]}, 持有上限 T+2_h4')
        print(f'  指标: 月化≈{top["monthly"]:+.1f}%  胜率={top["win_rate"]:.1f}%  样本={top["n"]}')
        return True
    else:
        print('\n[未达标] 当前样本下无组合同时满足 月化>=10% 且 胜率>=50% (N>=20)')
        return False


# ============== 主流程 ==============

def run_for_months(months):
    print('=' * 80)
    print(f'============ 炸板修复(ZhaBan) Phase-1+2 联合分析 ============')
    print(f'目标月份: {months}')
    print(f'筛选: T-1 触及涨停未封住 + T 非ST/非北交所/非一字板/开盘未涨停 + open_rate∈[{T_OPEN_RATE_MIN}%,{T_OPEN_RATE_MAX}%]')
    print('=' * 80)

    conn = sqlite3.connect(DB_PATH)
    all_dates, date_idx, target_dates, by_code, red_ratio_by_date = load_data(conn, months)
    conn.close()

    if not target_dates:
        print(f'[警告] 无目标交易日数据')
        return None

    all_candidates = []
    daily_cnt = []
    for td in target_dates:
        cs = find_candidates(by_code, all_dates, date_idx, td, red_ratio_by_date)
        all_candidates.extend(cs)
        daily_cnt.append((td, len(cs)))

    print()
    print('--- Phase-1 候选股统计 ---')
    print(f'交易日: {len(target_dates)} 天')
    print(f'候选股总数: {len(all_candidates)} 只  日均: {len(all_candidates)/max(1,len(target_dates)):.1f} 只')
    # 打印每日候选股数 (前 30 行)
    print('每日候选股数 (前 30 行):')
    for d, n in daily_cnt[:30]:
        print(f'  {d}: {n}')
    if len(daily_cnt) > 30:
        print(f'  ... (共 {len(daily_cnt)} 个交易日)')

    if not all_candidates:
        print('[警告] 无候选股')
        return None

    records = collect_trade_records(by_code, all_dates, all_candidates)

    matrix_rows = analyze_buy_sell_matrix(records)
    print_matrix(matrix_rows)

    # 方案 B + T+2_h4 持有上限的网格
    grid_b = analyze_sl_tp_grid(records, ('B(T_h2)', 0, 2), hold_max_off=2, monthly_hold_days=1.5)
    print_sl_tp_grid(grid_b, 'B(T_h2)', 2)

    # 条件过滤分析 (使用方案 B + 卖 T+1_h4)
    for label, fn in BUCKETS:
        analyze_buckets(records, fn, label, ('B(T_h2)', 0, 2), ('+1_h4', 1, 4))

    combo_results, qualified = analyze_best_combo(records)
    has_qualified = recommend(matrix_rows, grid_b, combo_results, qualified)
    print()
    print('============ 分析结束 ============')
    return {
        'has_qualified': has_qualified,
        'candidate_count': len(all_candidates),
        'qualified': qualified,
        'best_monthly': max((q['monthly'] for q in qualified), default=None),
        'best_win_rate': max((q['win_rate'] for q in qualified), default=None),
    }


def main():
    if len(sys.argv) < 2:
        print('用法: python3 strategy_zhaban_clean_phase12.py <月份>')
        print('示例: 2026-04 / 2025-01,2025-02 / 2025')
        sys.exit(1)
    months = parse_months(sys.argv[1])
    run_for_months(months)


if __name__ == '__main__':
    main()
