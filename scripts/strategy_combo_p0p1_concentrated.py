#!/usr/bin/env python3
"""P0/P1 组合策略 - 仓位集中版 (Task #19)

在 strategy_combo_p0p1.py 基础上仅修改仓位管理, 策略逻辑完全保持:
  P0: ZhaBan V3 限价单 (炸板修复)
  P1: 首板真低吸 (hour2_open市价)

三种仓位模式:
  N2  : 固定 N=2 仓位 (每笔 50% 总资产)
  N1  : 全仓 N=1     (每笔接近 100% 总资产)
  DYN : 动态        (P0 用 N=1 全仓, P1 用 N=2 50%; 上限 2 仓)

合规铁律保持:
  - 限价P基于事先(上一hour)数据计算
  - T+1: T 日买入最早 T+1 卖出
  - 涨停判定: round(close/preclose, 2) >= 1.10(主板) / 1.20(创业板科创板)
  - 卖出止损/Trailing 均用 hourX_low 验证
  - 100万初始资金, 2020-2025 六年共享, 不重置

用法:
  python3 scripts/strategy_combo_p0p1_concentrated.py
"""
import sqlite3
import math
from collections import defaultdict
from datetime import datetime

# ============== 配置 ==============
DB_PATH = '/home/AIWealth/data/stocks.db'
INITIAL_CAPITAL = 1_000_000.0
START_DATE = '2020-01-01'
END_DATE = '2025-12-31'

# P0 - ZhaBan V3 参数
P0_TURN_MIN = 15.0
P0_CR_MIN = 5.0
P0_OPEN_MAX = 5.0
P0_BUY_OFFSET = 0.01
P0_SL_PCT = 2.0
P0_TRAIL_PCT = 2.0
P0_HOLD_MAX = 3

# P1 - 首板真低吸 参数
P1_PREV_TURN_MIN = 10.0
P1_OPEN_RATE_MIN = -5.0
P1_OPEN_RATE_MAX = 5.0
P1_FIRSTBOARD_LOOKBACK = 5
P1_H1CR_MAX = -1.0
P1_SL_PCT = 2.0
P1_TRAIL_PCT = 2.0
P1_HOLD_MAX = 3
# ==================================


def sv(v):
    if v is None:
        return None
    try:
        f = float(v)
        return None if math.isnan(f) else f
    except (TypeError, ValueError):
        return None


def get_limit_up_threshold(code):
    if code.startswith('sz.30') or code.startswith('sh.68'):
        return 1.20
    return 1.10


def hit_limit_up(high, preclose, code):
    pc, h = sv(preclose), sv(high)
    if not pc or not h:
        return False
    return round(h / pc, 2) >= get_limit_up_threshold(code)


def closed_limit_up(close, preclose, code):
    pc, cl = sv(preclose), sv(close)
    if not pc or not cl:
        return False
    return round(cl / pc, 2) >= get_limit_up_threshold(code)


def open_at_limit_up(open_p, preclose, code):
    pc, op = sv(preclose), sv(open_p)
    if not pc or not op:
        return False
    return round(op / pc, 2) >= get_limit_up_threshold(code)


# ============== 字段索引 ==============
I_DATE, I_CODE, I_NAME, I_PRECLOSE, I_OPEN, I_HIGH, I_LOW, I_CLOSE = 0, 1, 2, 3, 4, 5, 6, 7
I_CR, I_OR, I_TURN, I_ISST = 8, 9, 10, 11
I_H1O, I_H1H, I_H1L, I_H1C, I_H1CR = 12, 13, 14, 15, 16
I_H2O, I_H2H, I_H2L, I_H2C = 17, 18, 19, 20
I_H3O, I_H3H, I_H3L, I_H3C = 21, 22, 23, 24
I_H4O, I_H4H, I_H4L, I_H4C = 25, 26, 27, 28

HOUR_FIELDS = {
    1: (I_H1O, I_H1H, I_H1L, I_H1C),
    2: (I_H2O, I_H2H, I_H2L, I_H2C),
    3: (I_H3O, I_H3H, I_H3L, I_H3C),
    4: (I_H4O, I_H4H, I_H4L, I_H4C),
}


def get_hour_ohlc(row, h):
    oi, hi, li, ci = HOUR_FIELDS[h]
    return sv(row[oi]), sv(row[hi]), sv(row[li]), sv(row[ci])


# ============== 数据加载 ==============
def load_data(conn):
    cur = conn.cursor()
    print('[1/2] 加载交易日列表...', flush=True)
    cur.execute(
        "SELECT DISTINCT date FROM stock_kline WHERE date>='2019-12-01' AND date<='2026-01-31' ORDER BY date"
    )
    all_dates = [r[0] for r in cur.fetchall()]
    date_idx = {d: i for i, d in enumerate(all_dates)}
    print(f'  交易日数: {len(all_dates)}', flush=True)

    print('[2/2] 加载股票数据...', flush=True)
    sql = """SELECT date, code, code_name, preclose, open, high, low, close,
             close_rate, open_rate, turn, isST,
             hour1_open, hour1_high, hour1_low, hour1_close, hour1_close_rate,
             hour2_open, hour2_high, hour2_low, hour2_close,
             hour3_open, hour3_high, hour3_low, hour3_close,
             hour4_open, hour4_high, hour4_low, hour4_close
             FROM stock_kline
             WHERE date>='2019-12-01' AND date<='2026-01-31'
             AND code NOT LIKE 'bj.%'"""
    cur.execute(sql)
    by_code = defaultdict(dict)
    n_rows = 0
    batch = cur.fetchmany(500000)
    while batch:
        for row in batch:
            by_code[row[1]][row[0]] = row
            n_rows += 1
        print(f'  已加载 {n_rows:,} 行...', flush=True)
        batch = cur.fetchmany(500000)
    print(f'  总计: 股票数={len(by_code)} 行数={n_rows:,}', flush=True)
    return all_dates, date_idx, by_code


# ============== P0 候选股: ZhaBan ==============
def find_p0_candidates(by_code, all_dates, date_idx, target_date):
    t_idx = date_idx.get(target_date)
    if t_idx is None or t_idx < 1:
        return []
    prev_date = all_dates[t_idx - 1]

    candidates = []
    for code, dm in by_code.items():
        prev_row = dm.get(prev_date)
        t_row = dm.get(target_date)
        if not prev_row or not t_row:
            continue
        if prev_row[I_ISST] == 1 or t_row[I_ISST] == 1:
            continue
        name = t_row[I_NAME] or prev_row[I_NAME] or ''
        if 'ST' in name.upper():
            continue
        prev_pc = sv(prev_row[I_PRECLOSE])
        prev_high = sv(prev_row[I_HIGH])
        prev_close = sv(prev_row[I_CLOSE])
        if not prev_pc or not prev_high or prev_close is None:
            continue
        if not hit_limit_up(prev_high, prev_pc, code):
            continue
        if closed_limit_up(prev_close, prev_pc, code):
            continue
        prev_turn = sv(prev_row[I_TURN])
        prev_cr = sv(prev_row[I_CR])
        if prev_turn is None or prev_turn < P0_TURN_MIN:
            continue
        if prev_cr is None or prev_cr < P0_CR_MIN:
            continue
        t_o = sv(t_row[I_OPEN]); t_h = sv(t_row[I_HIGH])
        t_l = sv(t_row[I_LOW]); t_c = sv(t_row[I_CLOSE])
        if t_o and t_h and t_l and t_c and t_o == t_h == t_l == t_c:
            continue
        t_pc = sv(t_row[I_PRECLOSE])
        if not t_pc or not t_o:
            continue
        if open_at_limit_up(t_o, t_pc, code):
            continue
        opr = sv(t_row[I_OR])
        if opr is None or opr >= P0_OPEN_MAX:
            continue
        h1c = sv(t_row[I_H1C]); h1cr = sv(t_row[I_H1CR]); h2l = sv(t_row[I_H2L])
        if h1c is None or h1cr is None or h2l is None:
            continue
        if h1cr <= -3.0:
            continue
        candidates.append({
            'priority': 'P0', 'code': code, 'name': name,
            't_idx': t_idx, 'buy_date': target_date,
            'open_rate': opr, 'prev_cr': prev_cr,
            'h1_close': h1c, 'h1_cr': h1cr, 'h2_low': h2l,
        })
    candidates.sort(key=lambda x: x['prev_cr'] or 0, reverse=True)
    return candidates


# ============== P1 候选股: 首板真低吸 ==============
def find_p1_candidates(by_code, all_dates, date_idx, target_date):
    t_idx = date_idx.get(target_date)
    if t_idx is None or t_idx < 1:
        return []
    prev_date = all_dates[t_idx - 1]
    lookback_start = t_idx - 1 - P1_FIRSTBOARD_LOOKBACK
    if lookback_start < 0:
        return []
    lookback_dates = all_dates[lookback_start:t_idx - 1]

    candidates = []
    for code, dm in by_code.items():
        prev_row = dm.get(prev_date)
        t_row = dm.get(target_date)
        if not prev_row or not t_row:
            continue
        prev_pc = sv(prev_row[I_PRECLOSE])
        prev_close = sv(prev_row[I_CLOSE])
        if not prev_pc or prev_close is None:
            continue
        if not closed_limit_up(prev_close, prev_pc, code):
            continue
        prev_turn = sv(prev_row[I_TURN])
        if prev_turn is None or prev_turn <= P1_PREV_TURN_MIN:
            continue
        if prev_row[I_ISST] == 1:
            continue
        name = prev_row[I_NAME] or ''
        if 'ST' in name.upper():
            continue
        is_first_board = True
        for ld in lookback_dates:
            lrow = dm.get(ld)
            if lrow:
                lpc = sv(lrow[I_PRECLOSE])
                lcl = sv(lrow[I_CLOSE])
                if lpc and lcl is not None and closed_limit_up(lcl, lpc, code):
                    is_first_board = False
                    break
        if not is_first_board:
            continue
        opr = sv(t_row[I_OR])
        if opr is None or opr < P1_OPEN_RATE_MIN or opr > P1_OPEN_RATE_MAX:
            continue
        t_o = sv(t_row[I_OPEN]); t_h = sv(t_row[I_HIGH])
        t_l = sv(t_row[I_LOW]); t_c = sv(t_row[I_CLOSE])
        if t_o and t_h and t_l and t_c and t_o == t_h == t_l == t_c:
            continue
        h1cr = sv(t_row[I_H1CR])
        h2_open = sv(t_row[I_H2O])
        if h1cr is None or h1cr > P1_H1CR_MAX:
            continue
        if h2_open is None or h2_open <= 0:
            continue
        candidates.append({
            'priority': 'P1', 'code': code, 'name': name,
            't_idx': t_idx, 'buy_date': target_date,
            'open_rate': opr, 'h1_cr': h1cr, 'h2_open': h2_open,
        })
    candidates.sort(key=lambda x: x['h1_cr'] if x['h1_cr'] is not None else 999)
    return candidates


# ============== Position ==============
class Position:
    __slots__ = ['priority', 'code', 'name', 'buy_price', 'buy_date', 'buy_idx',
                 'amount', 'shares', 'peak', 'sl_pct', 'trail_pct', 'hold_max']

    def __init__(self, priority, code, name, buy_price, buy_date, buy_idx, amount,
                 sl_pct, trail_pct, hold_max):
        self.priority = priority
        self.code = code
        self.name = name
        self.buy_price = buy_price
        self.buy_date = buy_date
        self.buy_idx = buy_idx
        self.amount = amount
        self.shares = amount / buy_price
        self.peak = buy_price
        self.sl_pct = sl_pct
        self.trail_pct = trail_pct
        self.hold_max = hold_max


# ============== 卖出逻辑 ==============
def check_exit_today(by_code, pos, today, today_idx):
    code = pos.code
    row = by_code.get(code, {}).get(today)
    if not row:
        return None, None

    days_held = today_idx - pos.buy_idx
    is_last_day = (days_held >= pos.hold_max)

    sl_pct = pos.sl_pct / 100.0
    trail_pct = pos.trail_pct / 100.0
    buy_price = pos.buy_price
    stop_price = buy_price * (1 - sl_pct)
    peak = pos.peak

    for h in (1, 2, 3, 4):
        ho, hh, hl, hc = get_hour_ohlc(row, h)
        if hh is None or hl is None:
            continue
        if is_last_day and h == 4:
            sell_p = ho if ho else hc
            if sell_p:
                pos.peak = max(peak, hh) if hh else peak
                return sell_p, 'force_close'
            continue
        if hl <= stop_price:
            pos.peak = peak
            return stop_price, 'stop_loss'
        if hh > peak:
            peak = hh
        if peak > buy_price:
            trailing_price = peak * (1 - trail_pct)
            if hl <= trailing_price:
                pos.peak = peak
                return trailing_price, 'trailing'

    pos.peak = peak

    if is_last_day:
        for h in (4, 3, 2, 1):
            ho, _, _, hc = get_hour_ohlc(row, h)
            p = ho or hc
            if p:
                return p, 'force_close'
    return None, None


# ============== 买入价 ==============
def try_buy_p0(cand):
    h1c = cand['h1_close']; h2l = cand['h2_low']
    if h1c is None or h1c <= 0 or h2l is None:
        return None
    limit_p = h1c * (1 - P0_BUY_OFFSET)
    if h2l <= limit_p:
        return limit_p
    return None


def try_buy_p1(cand):
    h2_open = cand['h2_open']
    if h2_open is None or h2_open <= 0:
        return None
    return h2_open


# ============== 仓位计算 (核心改动) ==============
def calc_buy_amount(mode, priority, capital, positions):
    """根据仓位模式计算单笔买入金额.
    返回 (slot_amt, max_slots) 中的 slot_amt; 若不应买入, 返回 0.
    """
    n_pos = len(positions)
    pos_value = sum(p.amount for p in positions)
    total_assets = capital + pos_value

    if mode == 'N2':
        max_slots = 2
        if n_pos >= max_slots:
            return 0
        slot_amt = total_assets / 2.0
    elif mode == 'N1':
        max_slots = 1
        if n_pos >= max_slots:
            return 0
        slot_amt = total_assets  # 全仓
    elif mode == 'DYN':
        # P0 全仓 (单仓上限 1); P1 半仓 (上限 2 仓)
        if priority == 'P0':
            # P0 来时若已有 P0 持仓 -> 不再买; 若已有 P1 持仓且半仓被占, 用剩余资金按 P0 全仓买入(但限制不超过总资产 100%)
            for p in positions:
                if p.priority == 'P0':
                    return 0
            # 允许在已持有 P1 时再买 P0, 但总仓位不超过总资产
            slot_amt = capital  # 用所有可用现金作为 P0 仓位
        else:  # P1
            max_slots = 2
            if n_pos >= max_slots:
                return 0
            slot_amt = total_assets / 2.0
    else:
        raise ValueError(f'unknown mode {mode}')

    if slot_amt > capital:
        slot_amt = capital
    if slot_amt < 1000:
        return 0
    return slot_amt


# ============== 回测主循环 ==============
def run_combo_simulation(by_code, all_dates, date_idx, mode):
    capital = float(INITIAL_CAPITAL)
    positions = []
    trades = []
    daily_equity = []

    target_dates = [d for d in all_dates if START_DATE <= d <= END_DATE]
    print(f'\n[回测-{mode}] 目标交易日: {len(target_dates)} 天 '
          f'({target_dates[0]} ~ {target_dates[-1]})', flush=True)

    progress_year = None
    for td in target_dates:
        t_idx = date_idx[td]
        cur_year = td[:4]
        if cur_year != progress_year:
            progress_year = cur_year
            equity_now = capital + sum(p.amount for p in positions)
            print(f'  进入 {cur_year}, 当前资产 {equity_now/10000:.2f}万', flush=True)

        # 1. 卖出
        new_positions = []
        for pos in positions:
            if t_idx <= pos.buy_idx:
                new_positions.append(pos)
                continue
            sell_price, reason = check_exit_today(by_code, pos, td, t_idx)
            if sell_price:
                pnl_pct = (sell_price - pos.buy_price) / pos.buy_price * 100.0
                pnl_amount = pos.shares * (sell_price - pos.buy_price)
                capital += pos.amount + pnl_amount
                trades.append({
                    'priority': pos.priority,
                    'buy_date': pos.buy_date, 'sell_date': td,
                    'code': pos.code, 'name': pos.name,
                    'buy_price': pos.buy_price, 'sell_price': sell_price,
                    'pnl_pct': pnl_pct, 'pnl_amount': pnl_amount,
                    'reason': reason,
                })
            else:
                new_positions.append(pos)
        positions = new_positions

        # 2. 买入: P0 优先
        held_codes = {p.code for p in positions}

        p0_cands = find_p0_candidates(by_code, all_dates, date_idx, td)
        for cand in p0_cands:
            if cand['code'] in held_codes:
                continue
            bp = try_buy_p0(cand)
            if bp is None:
                continue
            slot_amt = calc_buy_amount(mode, 'P0', capital, positions)
            if slot_amt <= 0:
                break
            positions.append(Position(
                'P0', cand['code'], cand['name'], bp, td, t_idx, slot_amt,
                P0_SL_PCT, P0_TRAIL_PCT, P0_HOLD_MAX
            ))
            capital -= slot_amt
            held_codes.add(cand['code'])

        p1_cands = find_p1_candidates(by_code, all_dates, date_idx, td)
        for cand in p1_cands:
            if cand['code'] in held_codes:
                continue
            bp = try_buy_p1(cand)
            if bp is None:
                continue
            slot_amt = calc_buy_amount(mode, 'P1', capital, positions)
            if slot_amt <= 0:
                break
            positions.append(Position(
                'P1', cand['code'], cand['name'], bp, td, t_idx, slot_amt,
                P1_SL_PCT, P1_TRAIL_PCT, P1_HOLD_MAX
            ))
            capital -= slot_amt
            held_codes.add(cand['code'])

        # 3. 估值
        pos_value = 0.0
        for pos in positions:
            row = by_code.get(pos.code, {}).get(td)
            if row:
                c = sv(row[I_CLOSE])
                pos_value += pos.shares * c if c else pos.amount
            else:
                pos_value += pos.amount
        daily_equity.append((td, capital + pos_value))

    # 期末强平
    last_date = target_dates[-1]
    for pos in positions:
        row = by_code.get(pos.code, {}).get(last_date)
        sp = sv(row[I_CLOSE]) if row else pos.buy_price
        if not sp:
            sp = pos.buy_price
        pnl_pct = (sp - pos.buy_price) / pos.buy_price * 100.0
        pnl_amount = pos.shares * (sp - pos.buy_price)
        capital += pos.amount + pnl_amount
        trades.append({
            'priority': pos.priority,
            'buy_date': pos.buy_date, 'sell_date': last_date,
            'code': pos.code, 'name': pos.name,
            'buy_price': pos.buy_price, 'sell_price': sp,
            'pnl_pct': pnl_pct, 'pnl_amount': pnl_amount,
            'reason': 'final_close',
        })
    if daily_equity:
        daily_equity[-1] = (daily_equity[-1][0], capital)

    return trades, daily_equity, capital


# ============== 统计 ==============
def calc_max_drawdown(daily_equity):
    if not daily_equity:
        return 0.0, None, None
    peak = -float('inf'); peak_date = None
    max_dd = 0.0; dd_peak_date = None; dd_trough_date = None
    for d, e in daily_equity:
        if e > peak:
            peak = e; peak_date = d
        dd = (peak - e) / peak * 100.0 if peak > 0 else 0
        if dd > max_dd:
            max_dd = dd; dd_peak_date = peak_date; dd_trough_date = d
    return max_dd, dd_peak_date, dd_trough_date


def summarize_priority(trades, priority):
    sub = [t for t in trades if t['priority'] == priority]
    n = len(sub)
    if n == 0:
        return {'n': 0, 'wr': 0.0, 'avg': 0.0, 'pnl': 0.0, 'win': 0, 'loss': 0}
    pnl_total = sum(t['pnl_amount'] for t in sub)
    wins = sum(1 for t in sub if t['pnl_pct'] > 0)
    avg = sum(t['pnl_pct'] for t in sub) / n
    return {'n': n, 'wr': wins / n * 100.0, 'avg': avg,
            'pnl': pnl_total, 'win': wins, 'loss': n - wins}


def calc_yearly_returns(daily_equity):
    if not daily_equity:
        return {}
    yearly = {}
    by_year = defaultdict(list)
    for d, e in daily_equity:
        by_year[d[:4]].append((d, e))
    prev_end = INITIAL_CAPITAL
    for y in sorted(by_year.keys()):
        days = by_year[y]
        year_end = days[-1][1]
        yearly[y] = {
            'start': prev_end, 'end': year_end,
            'return': (year_end / prev_end - 1) * 100.0 if prev_end > 0 else 0,
        }
        prev_end = year_end
    return yearly


def print_results(mode, trades, daily_equity, final_capital):
    print('\n' + '=' * 90)
    print(f'============ P0/P1 组合策略 [仓位模式={mode}] 回测结果 ============')
    print('=' * 90)

    total_ret = (final_capital / INITIAL_CAPITAL - 1) * 100.0
    n_years = 6
    cagr = ((final_capital / INITIAL_CAPITAL) ** (1.0 / n_years) - 1) * 100.0
    print(f'\n初始资金: {INITIAL_CAPITAL/10000:.2f} 万')
    print(f'最终资产: {final_capital/10000:.2f} 万')
    print(f'总收益率: {total_ret:+.2f}%')
    print(f'6年 CAGR: {cagr:+.2f}%')

    max_dd, dd_peak, dd_trough = calc_max_drawdown(daily_equity)
    print(f'最大回撤: -{max_dd:.2f}% (峰值 {dd_peak} → 谷值 {dd_trough})')

    print(f'\n--- 逐年收益 ---')
    yearly = calc_yearly_returns(daily_equity)
    print(f'{"年份":<6s} | {"年初(万)":>10s} | {"年末(万)":>10s} | {"年收益":>8s}')
    print('-' * 50)
    for y in sorted(yearly.keys()):
        info = yearly[y]
        print(f'{y:<6s} | {info["start"]/10000:>10.2f} | {info["end"]/10000:>10.2f} | {info["return"]:>+7.2f}%')

    print(f'\n--- 策略贡献分解 ---')
    p0_stat = summarize_priority(trades, 'P0')
    p1_stat = summarize_priority(trades, 'P1')
    total_n = p0_stat['n'] + p1_stat['n']
    print(f'{"策略":<6s} | {"交易笔数":>8s} | {"占比":>6s} | {"胜率":>6s} | {"均收益":>7s} | {"累计盈亏(万)":>12s}')
    print('-' * 65)
    for label, s in [('P0', p0_stat), ('P1', p1_stat)]:
        pct = s['n'] / total_n * 100.0 if total_n > 0 else 0
        print(f'{label:<6s} | {s["n"]:>8d} | {pct:>5.1f}% | {s["wr"]:>5.1f}% | '
              f'{s["avg"]:>+6.2f}% | {s["pnl"]/10000:>+11.2f}')

    n_all = len(trades)
    if n_all > 0:
        wins_all = sum(1 for t in trades if t['pnl_pct'] > 0)
        avg_all = sum(t['pnl_pct'] for t in trades) / n_all
        print(f'\n--- 总交易统计 ---')
        print(f'总交易笔数: {n_all}')
        print(f'胜率: {wins_all/n_all*100.0:.1f}% ({wins_all}胜/{n_all-wins_all}负)')
        print(f'均收益: {avg_all:+.2f}%')
        reasons = defaultdict(int)
        for t in trades:
            reasons[t['reason']] += 1
        print(f'\n--- 退出原因分布 ---')
        for r in sorted(reasons.keys()):
            print(f'  {r:<15s}: {reasons[r]:>4d} 笔 ({reasons[r]/n_all*100.0:.1f}%)')

    print(f'\n--- 年度交易笔数 (P0 / P1) ---')
    yearly_n = defaultdict(lambda: {'P0': 0, 'P1': 0})
    yearly_pnl = defaultdict(lambda: {'P0': 0.0, 'P1': 0.0})
    for t in trades:
        y = t['buy_date'][:4]
        yearly_n[y][t['priority']] += 1
        yearly_pnl[y][t['priority']] += t['pnl_amount']
    print(f'{"年份":<6s} | {"P0笔数":>6s} | {"P0盈亏(万)":>11s} | {"P1笔数":>6s} | {"P1盈亏(万)":>11s}')
    print('-' * 60)
    for y in sorted(yearly_n.keys()):
        info_n = yearly_n[y]; info_p = yearly_pnl[y]
        print(f'{y:<6s} | {info_n["P0"]:>6d} | {info_p["P0"]/10000:>+10.2f} | '
              f'{info_n["P1"]:>6d} | {info_p["P1"]/10000:>+10.2f}')

    return {
        'mode': mode, 'final': final_capital, 'cagr': cagr,
        'max_dd': max_dd, 'n_trades': n_all,
        'win_rate': (wins_all/n_all*100.0) if n_all else 0.0,
        'p0': p0_stat, 'p1': p1_stat,
        'yearly': yearly,
    }


# ============== 主入口 ==============
def main():
    print('=' * 90)
    print('Task #19: P0/P1 组合策略 - 仓位集中冲刺 500% CAGR')
    print('=' * 90)
    print(f'P0: ZhaBan V3 (炸板修复)  filter: turn>{P0_TURN_MIN}% & cr>{P0_CR_MIN}% & opr<{P0_OPEN_MAX}%, '
          f'buy h2 limit h1c*(1-{P0_BUY_OFFSET}), SL{P0_SL_PCT}%/Trail{P0_TRAIL_PCT}%/T+{P0_HOLD_MAX}')
    print(f'P1: 首板真低吸  filter: 首板+turn>{P1_PREV_TURN_MIN}% & opr∈[{P1_OPEN_RATE_MIN},{P1_OPEN_RATE_MAX}] & h1cr<={P1_H1CR_MAX}%, '
          f'buy h2_open, SL{P1_SL_PCT}%/Trail{P1_TRAIL_PCT}%/T+{P1_HOLD_MAX}')
    print(f'初始资金 {INITIAL_CAPITAL/10000:.0f}万, 区间 {START_DATE} ~ {END_DATE}, 三模式: N2 / N1 / DYN')

    t_start = datetime.now()
    conn = sqlite3.connect(DB_PATH)
    all_dates, date_idx, by_code = load_data(conn)
    conn.close()
    print(f'\n[加载完成] 耗时 {(datetime.now()-t_start).total_seconds():.1f} 秒', flush=True)

    summaries = []
    for mode in ('N2', 'N1', 'DYN'):
        ms = datetime.now()
        trades, daily_equity, final_capital = run_combo_simulation(
            by_code, all_dates, date_idx, mode
        )
        summary = print_results(mode, trades, daily_equity, final_capital)
        summary['elapsed'] = (datetime.now() - ms).total_seconds()
        summaries.append(summary)
        print(f'\n[{mode}] 单跑耗时 {summary["elapsed"]:.1f} 秒')

    # 三模式对比
    print('\n' + '=' * 90)
    print('============ 三模式对比 (Task #19 总览) ============')
    print('=' * 90)
    print(f'{"模式":<5s} | {"最终(万)":>10s} | {"CAGR":>8s} | {"最大回撤":>9s} | '
          f'{"交易":>5s} | {"胜率":>6s} | {"P0笔":>5s} | {"P1笔":>5s}')
    print('-' * 80)
    for s in summaries:
        print(f'{s["mode"]:<5s} | {s["final"]/10000:>10.2f} | {s["cagr"]:>+7.2f}% | '
              f'-{s["max_dd"]:>7.2f}% | {s["n_trades"]:>5d} | {s["win_rate"]:>5.1f}% | '
              f'{s["p0"]["n"]:>5d} | {s["p1"]["n"]:>5d}')

    print(f'\n--- 各模式逐年收益对比 ---')
    years = sorted(summaries[0]['yearly'].keys())
    header = f'{"年份":<6s}'
    for s in summaries:
        header += f' | {s["mode"]+"收益":>9s}'
    print(header)
    print('-' * (8 + 12 * len(summaries)))
    for y in years:
        line = f'{y:<6s}'
        for s in summaries:
            r = s['yearly'].get(y, {}).get('return', 0.0)
            line += f' | {r:>+8.2f}%'
        print(line)

    # 最优
    best = max(summaries, key=lambda x: x['cagr'])
    print(f'\n[最优] 模式 {best["mode"]} CAGR={best["cagr"]:+.2f}% '
          f'(最终 {best["final"]/10000:.2f}万, 回撤 -{best["max_dd"]:.2f}%)')
    if best['cagr'] >= 500:
        print('🎯 ★ 已突破 500% CAGR 目标 ★')
    elif best['cagr'] >= 300:
        print(f'⚠ 距 500% 目标尚差 {500 - best["cagr"]:.2f} 个百分点')
    else:
        print(f'✗ 未达 500% 目标 (差 {500 - best["cagr"]:.2f} 个百分点)')

    print(f'\n[总耗时] {(datetime.now()-t_start).total_seconds():.1f} 秒')


if __name__ == '__main__':
    main()
