#!/usr/bin/env python3
"""首板断板低吸策略 Phase-2: 买卖规则统计分析

基于 Phase-1 同样的候选股筛选逻辑, 对全月候选股进行多维统计:
  1) 买入时点 x 卖出时点 收益矩阵
  2) 止盈止损网格搜索 (买入方案 A: T 日 hour2_open)
  3) 条件过滤分析 (hour1_close_rate / turn / 昨日 turn 分组)
  4) 最优策略推荐

严格合规:
  - 买/卖价仅取 hourX_open
  - T+1 合规: 买入当日不卖出
  - 止盈止损逐 hour 检查 open 价格, 触及即以该 hour_open 为成交价
  - 不使用未来数据

用法:
  python3 scripts/strategy_firstboard_break_phase2.py 2026-04
"""
import sqlite3
import sys
import math
import statistics
from collections import defaultdict
from datetime import datetime, timedelta

# ============== 配置区 ==============
DB_PATH = '/home/AIWealth/data/stocks.db'
TARGET_MONTH = sys.argv[1] if len(sys.argv) > 1 else '2026-04'

# 筛选阈值 (与 Phase-1 完全一致)
OPEN_RATE_MIN = -5.0
OPEN_RATE_MAX = 5.0
TURN_MIN = 3.0
FIRSTBOARD_LOOKBACK = 5

# 止盈止损网格
STOP_LOSS_LIST = [-2.0, -3.0, -4.0, -5.0]
TAKE_PROFIT_LIST = [3.0, 4.0, 5.0, 6.0, 8.0]

# 月化收益换算: 假设持仓平均 1 个交易日, 每月 22 个交易日
# 月化 = avg_per_trade * (22 / avg_holding_days), 此处保守取 22 倍 (持仓 1 天)
MONTHLY_TRADE_DAYS = 22
DEFAULT_HOLDING_DAYS = 1.0  # 方案 A 卖出于 T+1, 持仓 ~1 天
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


def is_limit_up(close, preclose, code):
    pc = sv(preclose)
    cl = sv(close)
    if pc is None or cl is None or pc == 0:
        return False
    return round(cl / pc, 2) >= get_limit_up_threshold(code)


def is_one_word_board(row):
    o = sv(row.get('open'))
    h = sv(row.get('high'))
    l = sv(row.get('low'))
    c = sv(row.get('close'))
    if None in (o, h, l, c):
        return False
    return o == h == l == c


def load_data(conn, target_month):
    cur = conn.cursor()
    year, mon = target_month.split('-')
    year, mon = int(year), int(mon)
    first_day = f'{year:04d}-{mon:02d}-01'
    if mon == 12:
        next_first = f'{year+1:04d}-01-01'
    else:
        next_first = f'{year:04d}-{mon+1:02d}-01'

    dt_lo = (datetime.strptime(first_day, '%Y-%m-%d') - timedelta(days=30)).strftime('%Y-%m-%d')
    dt_hi = (datetime.strptime(next_first, '%Y-%m-%d') + timedelta(days=20)).strftime('%Y-%m-%d')

    cur.execute(
        'SELECT DISTINCT date FROM stock_kline WHERE date>=? AND date<? ORDER BY date',
        (dt_lo, dt_hi),
    )
    all_dates = [r[0] for r in cur.fetchall()]
    date_idx = {d: i for i, d in enumerate(all_dates)}
    target_dates = [d for d in all_dates if first_day <= d < next_first]

    flds = (
        'date,code,code_name,preclose,open,high,low,close,close_rate,open_rate,'
        'volume,amount,turn,isST,'
        'hour1_open,hour1_high,hour1_low,hour1_close,hour1_open_rate,hour1_close_rate,hour1_volume,hour1_amount,'
        'hour2_open,hour2_high,hour2_low,hour2_close,hour2_open_rate,hour2_close_rate,hour2_volume,hour2_amount,'
        'hour3_open,hour3_high,hour3_low,hour3_close,hour3_open_rate,hour3_close_rate,hour3_volume,hour3_amount,'
        'hour4_open,hour4_high,hour4_low,hour4_close,hour4_open_rate,hour4_close_rate,hour4_volume,hour4_amount'
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
    print(f'[加载] {dt_lo} ~ {dt_hi}  交易日={len(all_dates)}  目标日={len(target_dates)}  股票数={len(by_code)}  行数={n_rows}')
    return all_dates, date_idx, target_dates, by_code


def find_candidates(by_code, all_dates, date_idx, target_date):
    """筛选 target_date 候选股, 与 Phase-1 完全一致, 但额外保留 T-1 / T 日数据用于分析"""
    t_idx = date_idx.get(target_date)
    if t_idx is None or t_idx < 1:
        return []
    prev_date = all_dates[t_idx - 1]
    lookback_start = t_idx - 1 - FIRSTBOARD_LOOKBACK
    if lookback_start < 0:
        return []
    lookback_dates = all_dates[lookback_start:t_idx - 1]

    candidates = []
    for code, dm in by_code.items():
        if code.startswith('bj.'):
            continue
        prev_row = dm.get(prev_date)
        t_row = dm.get(target_date)
        if not prev_row or not t_row:
            continue
        if not is_limit_up(prev_row.get('close'), prev_row.get('preclose'), code):
            continue
        if prev_row.get('isST') == 1:
            continue
        name = prev_row.get('code_name') or ''
        if 'ST' in name.upper():
            continue
        is_first_board = True
        for ld in lookback_dates:
            lrow = dm.get(ld)
            if lrow and is_limit_up(lrow.get('close'), lrow.get('preclose'), code):
                is_first_board = False
                break
        if not is_first_board:
            continue
        opr = sv(t_row.get('open_rate'))
        if opr is None or opr < OPEN_RATE_MIN or opr > OPEN_RATE_MAX:
            continue
        if is_one_word_board(t_row):
            continue
        turn = sv(t_row.get('turn'))
        if turn is None or turn <= TURN_MIN:
            continue

        candidates.append({
            'code': code,
            'name': name,
            'target_date': target_date,
            't_idx': t_idx,
            'open_rate': opr,
            'turn': turn,
            'prev_turn': sv(prev_row.get('turn')),
            'hour1_close_rate': sv(t_row.get('hour1_close_rate')),
        })
    return candidates


def get_hour_open(by_code, all_dates, date_idx, code, day_offset_from_t, t_idx, hour):
    """获取 T+offset 日 hourN_open. day_offset_from_t: 0=T, 1=T+1, 2=T+2 ..."""
    di = t_idx + day_offset_from_t
    if di < 0 or di >= len(all_dates):
        return None
    d = all_dates[di]
    row = by_code.get(code, {}).get(d)
    if not row:
        return None
    return sv(row.get(f'hour{hour}_open'))


def collect_trade_records(by_code, all_dates, date_idx, candidates):
    """对每只候选股, 收集后续 T~T+3 各 hour_open 价格, 用于矩阵 / 网格计算"""
    records = []
    for c in candidates:
        code = c['code']
        t_idx = c['t_idx']
        rec = dict(c)
        # 收集 T 日 hour2/3/4 open, T+1/T+2/T+3 各 hour_open
        for off in (0, 1, 2, 3):
            for h in (1, 2, 3, 4):
                rec[f'd{off}_h{h}_open'] = get_hour_open(by_code, all_dates, date_idx, code, off, t_idx, h)
        records.append(rec)
    return records


# ============== 收益统计工具 ==============

def pct_return(buy, sell):
    if buy is None or sell is None or buy <= 0:
        return None
    return (sell - buy) / buy * 100.0


def stat_returns(returns):
    """统计一组收益率: 数量 / 均值 / 中位 / 胜率 / 盈亏比"""
    rs = [r for r in returns if r is not None]
    n = len(rs)
    if n == 0:
        return {'n': 0, 'mean': None, 'median': None, 'win_rate': None, 'pl_ratio': None}
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
    ('A(T_h2)', 0, 2),
    ('B(T_h3)', 0, 3),
    ('C(T+1_h1)', 1, 1),
]

# 卖出时点序列 (相对于 T 日的 day_offset, hour)
# 对 plan A/B (T 日买入), 卖出在 T+1, T+2; 对 plan C (T+1 日买入), 卖出在 T+2, T+3
def get_sell_points(buy_offset):
    """返回 [(label, offset, hour), ...], 满足 T+1 合规 (offset > buy_offset)"""
    base = buy_offset + 1
    return [
        (f'+1_h1', base, 1),
        (f'+1_h2', base, 2),
        (f'+1_h3', base, 3),
        (f'+1_h4', base, 4),
        (f'+2_h1', base + 1, 1),
    ]


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
        buy_label, b_off, b_h = plan
        sell_pts = get_sell_points(b_off)
        for sp in sell_pts:
            sell_label = f'T{sp[0]}'
            rets = []
            for r in records:
                bp = buy_price_of(r, plan)
                sp_price = sell_price_of(r, sp)
                rets.append(pct_return(bp, sp_price))
            s = stat_returns(rets)
            rows.append({'buy': buy_label, 'sell': sell_label, **s})
    return rows


def print_matrix(rows):
    print()
    print('--- 1. 买入时点 x 卖出时点 收益矩阵 ---')
    print(f'{"买入方案":<10s} | {"卖出时点":<8s} | {"均收益":>8s} | {"中位收益":>8s} | {"胜率":>6s} | {"盈亏比":>6s} | {"交易数":>6s}')
    print('-' * 76)
    for r in rows:
        mean = f'{r["mean"]:+.2f}%' if r['mean'] is not None else 'N/A'
        med = f'{r["median"]:+.2f}%' if r['median'] is not None else 'N/A'
        wr = f'{r["win_rate"]:.1f}%' if r['win_rate'] is not None else 'N/A'
        pl = f'{r["pl_ratio"]:.2f}' if r['pl_ratio'] is not None else 'N/A'
        print(f'{r["buy"]:<10s} | {r["sell"]:<8s} | {mean:>8s} | {med:>8s} | {wr:>6s} | {pl:>6s} | {r["n"]:>6d}')


# ============== 2. 止盈止损网格 (方案 A) ==============

def simulate_grid_for_plan_a(rec, sl_pct, tp_pct):
    """方案 A: T 日 hour2_open 买入. 出场窗口: T+1 h1/h2/h3/h4 open.
    逐 hour 检查 open 价格:
      - <= sl_price 触发止损, 以该 hour_open 卖出
      - >= tp_price 触发止盈, 以该 hour_open 卖出
    若至 T+1_h4 仍未触发, 强制以 T+1_h4_open 平仓
    """
    bp = rec.get('d0_h2_open')
    if bp is None or bp <= 0:
        return None
    sl_price = bp * (1 + sl_pct / 100.0)
    tp_price = bp * (1 + tp_pct / 100.0)
    sequence = [
        ('T+1_h1', rec.get('d1_h1_open')),
        ('T+1_h2', rec.get('d1_h2_open')),
        ('T+1_h3', rec.get('d1_h3_open')),
        ('T+1_h4', rec.get('d1_h4_open')),
    ]
    for label, p in sequence:
        if p is None:
            continue
        if p <= sl_price or p >= tp_price:
            return pct_return(bp, p)
    # 未触发: 以最后一个有效价格平仓
    for label, p in reversed(sequence):
        if p is not None:
            return pct_return(bp, p)
    return None


def analyze_grid(records):
    rows = []
    for sl in STOP_LOSS_LIST:
        for tp in TAKE_PROFIT_LIST:
            rets = [simulate_grid_for_plan_a(r, sl, tp) for r in records]
            s = stat_returns(rets)
            # 月化估算: 持仓 1 日, 每月 22 个交易日 (近似, 假设资金充分滚动)
            if s['mean'] is not None:
                monthly = s['mean'] * MONTHLY_TRADE_DAYS / DEFAULT_HOLDING_DAYS
            else:
                monthly = None
            rows.append({'sl': sl, 'tp': tp, 'monthly': monthly, **s})
    rows.sort(key=lambda x: (x['monthly'] if x['monthly'] is not None else -1e9), reverse=True)
    return rows


def print_grid(rows):
    print()
    print('--- 2. 止盈止损组合 (买入方案 A: T_h2_open, 出场窗口 T+1) ---')
    print(f'{"止损":>5s} | {"止盈":>5s} | {"胜率":>6s} | {"均收益":>8s} | {"月化":>8s} | {"盈亏比":>6s} | {"交易数":>6s}')
    print('-' * 64)
    for r in rows:
        wr = f'{r["win_rate"]:.1f}%' if r['win_rate'] is not None else 'N/A'
        mean = f'{r["mean"]:+.2f}%' if r['mean'] is not None else 'N/A'
        monthly = f'{r["monthly"]:+.1f}%' if r['monthly'] is not None else 'N/A'
        pl = f'{r["pl_ratio"]:.2f}' if r['pl_ratio'] is not None else 'N/A'
        print(f'{r["sl"]:+5.1f}% | {r["tp"]:+5.1f}% | {wr:>6s} | {mean:>8s} | {monthly:>8s} | {pl:>6s} | {r["n"]:>6d}')


# ============== 3. 条件过滤分析 ==============

def bucket_hour1_close_rate(v):
    if v is None:
        return 'NA'
    if v < -5: return '(-inf,-5%)'
    if v < -2: return '[-5%,-2%)'
    if v < 0:  return '[-2%,0%)'
    if v < 2:  return '[0%,2%)'
    if v < 5:  return '[2%,5%)'
    return '[5%,+inf)'


def bucket_turn(v):
    if v is None:
        return 'NA'
    if v <= 5:  return '(3%,5%]'
    if v <= 10: return '(5%,10%]'
    if v <= 20: return '(10%,20%]'
    return '(20%,+inf)'


def bucket_prev_turn(v):
    if v is None:
        return 'NA'
    if v <= 3:  return '(0%,3%]'
    if v <= 6:  return '(3%,6%]'
    if v <= 10: return '(6%,10%]'
    if v <= 15: return '(10%,15%]'
    return '(15%,+inf)'


def analyze_buckets(records, key_fn, label):
    """按 key_fn 分组, 计算 "买A 卖 T+1_h4" 的收益分布"""
    buckets = defaultdict(list)
    for r in records:
        bp = buy_price_of(r, BUY_PLANS[0])  # plan A
        sp = sell_price_of(r, ('+1_h4', 1, 4))
        ret = pct_return(bp, sp)
        if ret is None:
            continue
        buckets[key_fn(r)].append(ret)
    out = []
    for k, rs in buckets.items():
        s = stat_returns(rs)
        out.append({'group': k, **s})
    # 排序: 自然分组顺序 (字典序近似)
    out.sort(key=lambda x: x['group'])
    print()
    print(f'--- 3.{label} ---')
    print(f'{"分组":<14s} | {"数量":>5s} | {"均收益":>8s} | {"中位收益":>8s} | {"胜率":>6s} | {"盈亏比":>6s}')
    print('-' * 60)
    for r in out:
        mean = f'{r["mean"]:+.2f}%' if r['mean'] is not None else 'N/A'
        med = f'{r["median"]:+.2f}%' if r['median'] is not None else 'N/A'
        wr = f'{r["win_rate"]:.1f}%' if r['win_rate'] is not None else 'N/A'
        pl = f'{r["pl_ratio"]:.2f}' if r['pl_ratio'] is not None else 'N/A'
        print(f'{r["group"]:<14s} | {r["n"]:>5d} | {mean:>8s} | {med:>8s} | {wr:>6s} | {pl:>6s}')
    return out


# ============== 4. 涨跌幅分布 ==============

def analyze_extremes(records):
    """方案 A 买入后到 T+1_h4_open 期间, 各 hour open 相对买入价的最大涨/跌幅"""
    max_ups = []
    max_dns = []
    for r in records:
        bp = r.get('d0_h2_open')
        if bp is None or bp <= 0:
            continue
        prices = [r.get(f'd1_h{h}_open') for h in (1, 2, 3, 4)]
        prices = [p for p in prices if p is not None]
        if not prices:
            continue
        rets = [(p - bp) / bp * 100.0 for p in prices]
        max_ups.append(max(rets))
        max_dns.append(min(rets))

    def pct_buckets(values, buckets, label):
        n = len(values)
        if n == 0:
            print(f'  {label}: 无数据')
            return
        print(f'  {label} (n={n}):')
        for lo, hi in buckets:
            cnt = sum(1 for v in values if lo <= v < hi)
            pct = cnt / n * 100.0
            print(f'    [{lo:+.1f}%, {hi:+.1f}%): {cnt:5d}  {pct:5.1f}%')
    print()
    print('--- 4. 方案 A 买入后到 T+1_h4 期间最大涨/跌幅分布 ---')
    pct_buckets(max_ups, [(-100, 0), (0, 2), (2, 4), (4, 6), (6, 8), (8, 12), (12, 100)], '最大涨幅')
    pct_buckets(max_dns, [(-100, -8), (-8, -5), (-5, -3), (-3, -1), (-1, 0), (0, 100)], '最大跌幅')


# ============== 5a. 组合策略 (过滤 + 卖出方案 + 止盈止损) ==============

def simulate_combo(rec, plan, sl_pct, tp_pct, force_exit_pt):
    """通用模拟: 给定买入方案 plan, 止损 sl, 止盈 tp, 强制平仓时点 force_exit_pt.
    出场窗口为买入次日 (b_off+1) 的 h1~h4 + 强制平仓点之间的所有 hour_open.
    """
    bp = buy_price_of(rec, plan)
    if bp is None or bp <= 0:
        return None
    sl_price = bp * (1 + sl_pct / 100.0)
    tp_price = bp * (1 + tp_pct / 100.0)
    b_off = plan[1]
    sequence = []
    for off in range(b_off + 1, force_exit_pt[1] + 1):
        max_h = force_exit_pt[2] if off == force_exit_pt[1] else 4
        for h in range(1, max_h + 1):
            sequence.append(rec.get(f'd{off}_h{h}_open'))
    for p in sequence:
        if p is None:
            continue
        if p <= sl_price or p >= tp_price:
            return pct_return(bp, p)
    for p in reversed(sequence):
        if p is not None:
            return pct_return(bp, p)
    return None


def analyze_combo_strategy(records):
    """在最优过滤子集上, 跑方案 A 的止盈止损网格 + T+1_h4 强平"""
    print()
    print('--- 5. 组合策略分析 (条件过滤 + 方案 A 止盈止损 + T+1_h4 强平) ---')
    # 重要过滤维度
    filters = [
        ('hour1_close_rate ∈ [0%,2%)',  lambda r: r.get('hour1_close_rate') is not None and 0 <= r['hour1_close_rate'] < 2),
        ('hour1_close_rate ∈ [-5%,-2%)', lambda r: r.get('hour1_close_rate') is not None and -5 <= r['hour1_close_rate'] < -2),
        ('T 日 turn ∈ (10%,20%]',       lambda r: r.get('turn') is not None and 10 < r['turn'] <= 20),
        ('T-1 日 turn > 15%',           lambda r: r.get('prev_turn') is not None and r['prev_turn'] > 15),
        ('hour1_close_rate ∈ [-5%,2%) & T 日 turn>10%',
         lambda r: (r.get('hour1_close_rate') is not None and -5 <= r['hour1_close_rate'] < 2)
                   and (r.get('turn') is not None and r['turn'] > 10)),
    ]
    plan_a = BUY_PLANS[0]
    force_exit = ('+1_h4', 1, 4)
    best_overall = None
    for fname, fn in filters:
        subset = [r for r in records if fn(r)]
        if len(subset) < 30:
            continue
        # 重跑网格
        results = []
        for sl in STOP_LOSS_LIST:
            for tp in TAKE_PROFIT_LIST:
                rets = [simulate_combo(r, plan_a, sl, tp, force_exit) for r in subset]
                s = stat_returns(rets)
                monthly = (s['mean'] * MONTHLY_TRADE_DAYS / DEFAULT_HOLDING_DAYS) if s['mean'] is not None else None
                results.append({'sl': sl, 'tp': tp, 'monthly': monthly, **s})
        # 选胜率最高且月化达标的前 3
        results.sort(key=lambda x: (x['win_rate'] or 0, x['monthly'] or 0), reverse=True)
        print(f'\n[过滤条件] {fname}  样本={len(subset)}')
        print(f'  {"止损":>5s} | {"止盈":>5s} | {"胜率":>6s} | {"均收益":>8s} | {"月化":>8s} | {"盈亏比":>6s} | {"样本":>5s}')
        for r in results[:5]:
            wr = f'{r["win_rate"]:.1f}%' if r['win_rate'] is not None else 'N/A'
            mean = f'{r["mean"]:+.2f}%' if r['mean'] is not None else 'N/A'
            monthly = f'{r["monthly"]:+.1f}%' if r['monthly'] is not None else 'N/A'
            pl = f'{r["pl_ratio"]:.2f}' if r['pl_ratio'] else 'N/A'
            print(f'  {r["sl"]:+5.1f}% | {r["tp"]:+5.1f}% | {wr:>6s} | {mean:>8s} | {monthly:>8s} | {pl:>6s} | {r["n"]:>5d}')
        # 记录达标的最优
        for r in results:
            if r['win_rate'] is not None and r['win_rate'] >= 55.0 and r['monthly'] is not None and r['monthly'] >= 10.0:
                cand = {'filter': fname, 'subset_size': len(subset), **r}
                if best_overall is None or cand['monthly'] > best_overall['monthly']:
                    best_overall = cand
                break
    return best_overall


# ============== 6. 最优策略推荐 ==============

def recommend(matrix_rows, grid_rows, hour1_buckets, best_combo):
    print()
    print('--- 6. 最优策略推荐 ---')
    # 矩阵中的最优 (按月化估算)
    best_matrix = None
    for r in matrix_rows:
        if r['mean'] is None or r['win_rate'] is None:
            continue
        # 月化: 估算持仓天数. T+1_* 持仓 1 天, T+2_* 持仓 2 天. 方案 C 买在 T+1, 卖 T+2/T+3 同理
        if '+1_' in r['sell']:
            hold = 1.0
        elif '+2_' in r['sell']:
            hold = 2.0
        else:
            hold = 1.0
        monthly = r['mean'] * MONTHLY_TRADE_DAYS / hold
        score = (r['win_rate'], monthly)
        cand = {**r, 'monthly': monthly, 'score': score}
        if best_matrix is None or (cand['monthly'], cand['win_rate']) > (best_matrix['monthly'], best_matrix['win_rate']):
            best_matrix = cand

    # 满足目标 (月化>=10% 且 胜率>=55%) 的网格组合
    qualified_grid = [r for r in grid_rows if r['monthly'] is not None and r['monthly'] >= 10.0 and r['win_rate'] is not None and r['win_rate'] >= 55.0]
    qualified_grid.sort(key=lambda x: (x['monthly'], x['win_rate']), reverse=True)

    if best_matrix:
        pl_str = f'{best_matrix["pl_ratio"]:.2f}' if best_matrix["pl_ratio"] else 'N/A'
        print(f'[基础矩阵最优] 买入={best_matrix["buy"]}  卖出={best_matrix["sell"]}  '
              f'均收益={best_matrix["mean"]:+.2f}%  胜率={best_matrix["win_rate"]:.1f}%  '
              f'盈亏比={pl_str}  月化≈{best_matrix["monthly"]:+.1f}%')

    if qualified_grid:
        top = qualified_grid[0]
        pl_str = f'{top["pl_ratio"]:.2f}' if top["pl_ratio"] else 'N/A'
        print(f'[止盈止损最优] 方案 A (T_h2_open 买入)  止损={top["sl"]:+.1f}%  止盈={top["tp"]:+.1f}%  '
              f'胜率={top["win_rate"]:.1f}%  均收益={top["mean"]:+.2f}%  月化≈{top["monthly"]:+.1f}%  '
              f'盈亏比={pl_str}')
        print(f'[达标组合数] 共 {len(qualified_grid)} 组止盈止损满足 月化>=10% 且 胜率>=55%')
    else:
        print('[止盈止损最优] 当月无组合同时满足 月化>=10% 且 胜率>=55%, 列出按月化排名前 3:')
        for r in grid_rows[:3]:
            print(f'    止损={r["sl"]:+.1f}%  止盈={r["tp"]:+.1f}%  胜率={r["win_rate"]:.1f}%  月化≈{r["monthly"]:+.1f}%')

    # 条件过滤推荐
    qualified_buckets = [b for b in hour1_buckets
                         if b['mean'] is not None and b['win_rate'] is not None
                         and b['mean'] > 0 and b['n'] >= 30]
    qualified_buckets.sort(key=lambda x: (x['mean'], x['win_rate']), reverse=True)
    if qualified_buckets:
        b = qualified_buckets[0]
        print(f'[最佳条件子集] hour1_close_rate ∈ {b["group"]}  '
              f'均收益={b["mean"]:+.2f}%  胜率={b["win_rate"]:.1f}%  样本={b["n"]}')

    print()
    print('[综合建议 - 本月最优策略]')
    if best_combo:
        pl_str = f'{best_combo["pl_ratio"]:.2f}' if best_combo["pl_ratio"] else 'N/A'
        print(f'  买入: T 日 hour2_open (观察 hour1 后买入)')
        print(f'  过滤: {best_combo["filter"]}  (子集样本 {best_combo["subset_size"]} 只)')
        print(f'  卖出: 逐 hour 检查 T+1 各 hour_open, 触及 止损 {best_combo["sl"]:+.1f}% 或 止盈 {best_combo["tp"]:+.1f}% 即出, 否则 T+1_h4_open 强平')
        print(f'  月化预估: {best_combo["monthly"]:+.1f}%  胜率: {best_combo["win_rate"]:.1f}%  均收益: {best_combo["mean"]:+.2f}%  盈亏比: {pl_str}')
        print(f'  ✅ 达标: 月化≥ 10% 且 胜率≥ 55%')
    elif qualified_grid:
        top = qualified_grid[0]
        pl_str2 = f'{top["pl_ratio"]:.2f}' if top["pl_ratio"] else 'N/A'
        print(f'  买入: T 日 hour2_open (观察 hour1 后买入)')
        print(f'  卖出: 逐 hour 检查 T+1 各 hour_open, 触及 止损 {top["sl"]:+.1f}% 或 止盈 {top["tp"]:+.1f}% 即出, 否则 T+1_h4_open 平仓')
        if qualified_buckets:
            print(f'  过滤: 仅取 hour1_close_rate ∈ {qualified_buckets[0]["group"]} 的候选股')
        print(f'  预估月化: {top["monthly"]:+.1f}%  胜率: {top["win_rate"]:.1f}%  盈亏比: {pl_str2}')
    else:
        print('  本月样本下未发现达标组合, 建议:')
        print('   1) 扩大样本至全年回测稳定性')
        print('   2) 加入更严苛的过滤条件 (低 turn / 弱反弹)')
        print('   3) 调整买入时点至 hour3 或次日 hour1 重新评估')


# ============== 主流程 ==============

def main():
    print('================ 首板断板低吸 Phase-2 统计分析 ================')
    print(f'目标月份: {TARGET_MONTH}')
    print(f'参数: open_rate ∈ [{OPEN_RATE_MIN}%, {OPEN_RATE_MAX}%]  turn > {TURN_MIN}%  首板回溯={FIRSTBOARD_LOOKBACK}日')

    conn = sqlite3.connect(DB_PATH)
    all_dates, date_idx, target_dates, by_code = load_data(conn, TARGET_MONTH)
    conn.close()

    if not target_dates:
        print(f'[警告] 月份 {TARGET_MONTH} 无交易日数据')
        return

    all_candidates = []
    daily_cnt = []
    for td in target_dates:
        cs = find_candidates(by_code, all_dates, date_idx, td)
        all_candidates.extend(cs)
        daily_cnt.append((td, len(cs)))

    print(f'候选股总数: {len(all_candidates)}  交易日数: {len(target_dates)}  日均: {len(all_candidates)/max(1,len(target_dates)):.1f}')

    records = collect_trade_records(by_code, all_dates, date_idx, all_candidates)

    matrix_rows = analyze_buy_sell_matrix(records)
    print_matrix(matrix_rows)

    grid_rows = analyze_grid(records)
    print_grid(grid_rows)

    print()
    print('================ 3. 条件过滤分析 (基于方案 A 卖 T+1_h4) ================')
    h1_buckets = analyze_buckets(records, lambda r: bucket_hour1_close_rate(r.get('hour1_close_rate')), '3.1 hour1_close_rate 分组')
    analyze_buckets(records, lambda r: bucket_turn(r.get('turn')), '3.2 T 日换手率分组')
    analyze_buckets(records, lambda r: bucket_prev_turn(r.get('prev_turn')), '3.3 T-1 日 (涨停日) 换手率分组')

    analyze_extremes(records)

    best_combo = analyze_combo_strategy(records)

    recommend(matrix_rows, grid_rows, h1_buckets, best_combo)
    print()
    print('================ 分析结束 ================')


if __name__ == '__main__':
    main()
