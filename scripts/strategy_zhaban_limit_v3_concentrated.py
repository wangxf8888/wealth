#!/usr/bin/env python3
"""ZhaBan限价单 V3 仓位集中测试 (Task #16)

V3已确认基础参数(2020-2025 6年CAGR≈160.9%, N=3):
  - 筛选: turn>15%  + close_rate>5% + open_rate<5%
  - 买入: hour2限价 = hour1_close * 0.99 (offset=1%)
  - 风控: SL=2% + Trailing=2%
  - 持仓: hold_max=T+3
  - 涨停: round(close/preclose,2) >= 1.10/1.20

3个集中度版本(其余参数与V3完全一致):
  V3-N2  : 固定仓位 N=2 (每次净值/2)
  V3-N1  : 全仓     N=1 (每次100%净值)
  V3-DYN : 动态仓位
           - 强信号(prev_turn>25% AND prev_close_rate>8%) → N=1 全仓
           - 普通信号 → N=2

输出: 2020-2025逐年收益率 + 6年CAGR + 最大回撤 + 关键指标对比

合规铁律:
  - 限价P必须基于事先(上一hour)数据计算
  - T+1: T日买入最早T+1卖出
  - 涨停判定: round(close/preclose,2)严格规则
  - 卖出按规则验证: hour_low<=P 才以P成交

用法:
  python3 scripts/strategy_zhaban_limit_v3_concentrated.py
"""
import sqlite3
import sys
import math
import statistics
from collections import defaultdict
from datetime import datetime

# ============== 配置 ==============
DB_PATH = '/home/AIWealth/data/stocks.db'
INDEX_CODE = 'sh.000001'
INITIAL_CAPITAL = 1_000_000
START_DATE = '2020-02-01'
END_DATE = '2025-12-31'

# V3 基础参数
V3_BASE = {
    'turn_min': 15.0,
    'cr_min': 5.0,
    'open_max': 5.0,
    'buy_offset': 0.01,
    'sl_pct': 2.0,
    'trail_pct': 2.0,
    'hold_max_days': 3,
}

# 3个集中度版本
VERSIONS = {
    'V3-N2':  {'mode': 'fixed', 'n_slots': 2},
    'V3-N1':  {'mode': 'fixed', 'n_slots': 1},
    'V3-DYN': {'mode': 'dynamic', 'strong_turn': 25.0, 'strong_cr': 8.0,
               'strong_n': 1, 'normal_n': 2},
}
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


# 字段索引常量(精简tuple, 已去掉date/code/name前3字段)
I_PRECLOSE, I_OPEN, I_HIGH, I_LOW, I_CLOSE = 0, 1, 2, 3, 4
I_CR, I_OR, I_TURN, I_ISST = 5, 6, 7, 8
I_H1O, I_H1H, I_H1L, I_H1C, I_H1CR = 9, 10, 11, 12, 13
I_H2O, I_H2H, I_H2L, I_H2C = 14, 15, 16, 17
I_H3O, I_H3H, I_H3L, I_H3C = 18, 19, 20, 21
I_H4O, I_H4H, I_H4L, I_H4C = 22, 23, 24, 25

HOUR_FIELDS = {1: (I_H1O, I_H1H, I_H1L, I_H1C),
               2: (I_H2O, I_H2H, I_H2L, I_H2C),
               3: (I_H3O, I_H3H, I_H3L, I_H3C),
               4: (I_H4O, I_H4H, I_H4L, I_H4C)}


def load_data(conn):
    cur = conn.cursor()
    print('[1/2] 加载交易日列表...', flush=True)
    cur.execute("SELECT DISTINCT date FROM stock_kline WHERE date>='2019-12-01' AND date<='2026-01-31' ORDER BY date")
    all_dates = [sys.intern(r[0]) for r in cur.fetchall()]
    date_idx = {d: i for i, d in enumerate(all_dates)}
    print(f'  交易日数: {len(all_dates)}', flush=True)

    print('[2/2] 加载股票数据(精简存储)...', flush=True)
    # 内存优化: 不存code_name(用单独map), 不存date/code(由key唯一确定)
    # 每行 -> (preclose, open, high, low, close, cr, opr, turn, isST,
    #          h1o,h1h,h1l,h1c,h1cr, h2o,h2h,h2l,h2c, h3o,h3h,h3l,h3c, h4o,h4h,h4l,h4c)
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
    name_map = {}
    n_rows = 0
    batch = cur.fetchmany(500000)
    while batch:
        for row in batch:
            d = sys.intern(row[0])
            code = sys.intern(row[1])
            if row[2] and code not in name_map:
                name_map[code] = row[2]
            # 存精简tuple: 去掉date/code/name
            by_code[code][d] = row[3:]
            n_rows += 1
        print(f'  已加载 {n_rows:,} 行...', flush=True)
        batch = cur.fetchmany(500000)
    print(f'  股票数={len(by_code)} 行数={n_rows:,}', flush=True)
    return all_dates, date_idx, by_code, name_map


def find_zhaban_candidates(by_code, name_map, all_dates, date_idx, target_date):
    """筛选target_date的炸板候选股"""
    t_idx = date_idx.get(target_date)
    if t_idx is None or t_idx < 1:
        return []
    prev_date = all_dates[t_idx - 1]

    cands = []
    for code, dm in by_code.items():
        prev_row = dm.get(prev_date)
        t_row = dm.get(target_date)
        if not prev_row or not t_row:
            continue
        if prev_row[I_ISST] == 1 or t_row[I_ISST] == 1:
            continue
        name = name_map.get(code, '')
        if 'ST' in name.upper():
            continue
        prev_pc = sv(prev_row[I_PRECLOSE])
        prev_high = sv(prev_row[I_HIGH])
        prev_close = sv(prev_row[I_CLOSE])
        if not prev_pc or not prev_high or prev_close is None:
            continue
        # 炸板: T-1日触及涨停但未封住
        if not hit_limit_up(prev_high, prev_pc, code):
            continue
        if closed_limit_up(prev_close, prev_pc, code):
            continue
        # T日不能一字板
        t_o, t_h, t_l, t_c = sv(t_row[I_OPEN]), sv(t_row[I_HIGH]), sv(t_row[I_LOW]), sv(t_row[I_CLOSE])
        if t_o and t_h and t_l and t_c and t_o == t_h == t_l == t_c:
            continue
        t_pc = sv(t_row[I_PRECLOSE])
        if not t_pc or not t_o:
            continue
        if open_at_limit_up(t_o, t_pc, code):
            continue
        opr = sv(t_row[I_OR])
        if opr is None or opr < -5.0 or opr > 9.5:
            continue
        if sv(t_row[I_H1O]) is None:
            continue
        cands.append({
            'code': code, 'name': name, 't_idx': t_idx,
            'open_rate': opr,
            'prev_turn': sv(prev_row[I_TURN]),
            'prev_close_rate': sv(prev_row[I_CR]),
        })
    return cands



def apply_v3_filter(cands, base):
    out = []
    for c in cands:
        if c['prev_turn'] is None or c['prev_turn'] < base['turn_min']:
            continue
        if c['prev_close_rate'] is None or c['prev_close_rate'] < base['cr_min']:
            continue
        if c['open_rate'] >= base['open_max']:
            continue
        out.append(c)
    out.sort(key=lambda x: (x['prev_close_rate'] or 0), reverse=True)
    return out


def try_limit_buy(by_code, all_dates, code, t_idx, offset):
    d = all_dates[t_idx]
    row = by_code.get(code, {}).get(d)
    if not row:
        return None
    h1c = sv(row[I_H1C])
    h1cr = sv(row[I_H1CR])
    h2l = sv(row[I_H2L])
    if h1c is None or h1cr is None or h2l is None:
        return None
    if h1cr <= -3.0:
        return None
    limit_p = h1c * (1 - offset)
    if h2l <= limit_p:
        return limit_p
    return None


class Position:
    __slots__ = ['code', 'name', 'buy_price', 'buy_date', 'buy_idx', 'amount', 'shares', 'peak', 'is_strong']
    def __init__(self, code, name, buy_price, buy_date, buy_idx, amount, is_strong=False):
        self.code = code
        self.name = name
        self.buy_price = buy_price
        self.buy_date = buy_date
        self.buy_idx = buy_idx
        self.amount = amount
        self.shares = amount / buy_price
        self.peak = buy_price
        self.is_strong = is_strong


def get_hour_ohlc(row, h):
    oi, hi, li, ci = HOUR_FIELDS[h]
    return sv(row[oi]), sv(row[hi]), sv(row[li]), sv(row[ci])


def check_exit_today(by_code, pos, today, today_idx, base):
    """V3 trailing 风控: SL=2% + Trailing=2%, hold_max_days=3 强平"""
    code = pos.code
    row = by_code.get(code, {}).get(today)
    if not row:
        return None, None

    days_held = today_idx - pos.buy_idx
    is_last_day = (days_held >= base['hold_max_days'])

    sl_pct = base['sl_pct'] / 100.0
    trail_pct = base['trail_pct'] / 100.0

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

        if hh > peak:
            peak = hh
        trailing_price = peak * (1 - trail_pct)
        if hl <= stop_price:
            pos.peak = peak
            return stop_price, 'stop_loss'
        if peak > buy_price and hl <= trailing_price:
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


def compute_total_nav(capital, positions, by_code, today):
    """以当日close估值持仓"""
    pos_value = 0.0
    for pos in positions:
        row = by_code.get(pos.code, {}).get(today)
        if row:
            c = sv(row[I_CLOSE])
            if c:
                pos_value += pos.shares * c
                continue
        pos_value += pos.amount
    return capital + pos_value


def decide_n_slots(cand, ver):
    """根据版本+候选股特性决定本次持仓上限"""
    mode = ver['mode']
    if mode == 'fixed':
        return ver['n_slots'], False
    # dynamic
    is_strong = (cand['prev_turn'] is not None and cand['prev_turn'] > ver['strong_turn']
                 and cand['prev_close_rate'] is not None and cand['prev_close_rate'] > ver['strong_cr'])
    if is_strong:
        return ver['strong_n'], True
    return ver['normal_n'], False


def run_simulation(by_code, name_map, all_dates, date_idx, ver_name, ver_params, base):
    """运行集中度版本模拟"""
    capital = float(INITIAL_CAPITAL)
    positions = []
    trades = []
    yearly_nav = {}
    daily_nav_log = []  # (date, nav) 用于最大回撤

    target_dates = [d for d in all_dates if START_DATE <= d <= END_DATE]
    if not target_dates:
        return trades, yearly_nav, capital, daily_nav_log

    current_year = target_dates[0][:4]

    for td in target_dates:
        t_idx = date_idx[td]
        year = td[:4]

        # 年度切换 -> 记录上一年末净值(用上一交易日close估值)
        if year != current_year:
            prev_d = all_dates[t_idx - 1] if t_idx > 0 else td
            yearly_nav[current_year] = compute_total_nav(capital, positions, by_code, prev_d)
            current_year = year

        # 1) 卖出检查
        new_positions = []
        for pos in positions:
            if pos.buy_idx >= t_idx:  # T+1
                new_positions.append(pos)
                continue
            sell_price, reason = check_exit_today(by_code, pos, td, t_idx, base)
            if sell_price:
                pnl_pct = (sell_price - pos.buy_price) / pos.buy_price * 100
                pnl_amount = pos.shares * (sell_price - pos.buy_price)
                capital += pos.amount + pnl_amount
                trades.append({
                    'buy_date': pos.buy_date, 'sell_date': td,
                    'code': pos.code, 'name': pos.name,
                    'buy_price': pos.buy_price, 'sell_price': sell_price,
                    'pnl_pct': pnl_pct, 'reason': reason,
                    'is_strong': pos.is_strong,
                })
            else:
                new_positions.append(pos)
        positions = new_positions

        # 2) 买入新仓位
        cands_raw = find_zhaban_candidates(by_code, name_map, all_dates, date_idx, td)
        cands = apply_v3_filter(cands_raw, base)

        for cand in cands:
            n_slots, is_strong = decide_n_slots(cand, ver_params)
            # 已持仓数已达n_slots则跳过
            if len(positions) >= n_slots:
                continue
            bp = try_limit_buy(by_code, all_dates, cand['code'], t_idx, base['buy_offset'])
            if bp is None:
                continue
            # 计算本次仓位资金
            total_assets = capital + sum(p.amount for p in positions)
            slot_amt = total_assets / n_slots
            if slot_amt > capital:
                slot_amt = capital
            if slot_amt < 1000:
                continue
            positions.append(Position(cand['code'], cand['name'], bp, td, t_idx, slot_amt, is_strong))
            capital -= slot_amt

        # 3) 记录当日净值(用今日close估值)
        nav_today = compute_total_nav(capital, positions, by_code, td)
        daily_nav_log.append((td, nav_today))

    # 期末强平
    last_date = target_dates[-1]
    for pos in positions:
        row = by_code.get(pos.code, {}).get(last_date)
        sp = sv(row[I_CLOSE]) if row else None
        if not sp:
            sp = pos.buy_price
        pnl_pct = (sp - pos.buy_price) / pos.buy_price * 100
        pnl_amount = pos.shares * (sp - pos.buy_price)
        capital += pos.amount + pnl_amount
        trades.append({'buy_date': pos.buy_date, 'sell_date': last_date,
                       'code': pos.code, 'name': pos.name,
                       'buy_price': pos.buy_price, 'sell_price': sp,
                       'pnl_pct': pnl_pct, 'reason': 'final_close',
                       'is_strong': pos.is_strong})
    positions = []
    yearly_nav[current_year] = capital

    return trades, yearly_nav, capital, daily_nav_log


def calc_max_drawdown(nav_log):
    """日级最大回撤(基于日末净值)"""
    if not nav_log:
        return 0.0, None, None
    peak = nav_log[0][1]
    peak_date = nav_log[0][0]
    max_dd = 0.0
    dd_peak_date = peak_date
    dd_trough_date = peak_date
    cur_peak_date = peak_date
    for d, n in nav_log:
        if n > peak:
            peak = n
            cur_peak_date = d
        dd = (peak - n) / peak if peak > 0 else 0
        if dd > max_dd:
            max_dd = dd
            dd_peak_date = cur_peak_date
            dd_trough_date = d
    return max_dd, dd_peak_date, dd_trough_date


def print_results(all_results):
    print('\n' + '=' * 100)
    print('============ ZhaBan V3 仓位集中测试 (Task #16) ============')
    print('=' * 100)
    print('基础参数: turn>15% + cr>5% + open<5% + buy_offset=1% + SL2%/Trail2% + hold_max=T+3')
    print()

    # 逐版本逐年
    print(f'{"="*100}')
    print('=== 逐年明细 (初始100万, 年内复利, 跨年不重置) ===')
    print(f'{"版本":<8s} | {"年份":<5s} | {"交易":>4s} | {"胜率":>6s} | {"均收益":>7s} | '
          f'{"年末净值":>10s} | {"年化":>8s}')
    print('-' * 80)

    for vn in VERSIONS:
        info = all_results[vn]
        trades = info['trades']
        yn = info['yearly_nav']
        prev_nav = INITIAL_CAPITAL
        for year in sorted(yn.keys()):
            yt = [t for t in trades if t['sell_date'][:4] == year]
            nt = len(yt)
            wins = sum(1 for t in yt if t['pnl_pct'] > 0)
            wr = wins / nt * 100 if nt > 0 else 0
            avg_r = statistics.mean([t['pnl_pct'] for t in yt]) if yt else 0
            nav = yn[year]
            yr = (nav - prev_nav) / prev_nav * 100 if prev_nav > 0 else 0
            print(f'{vn:<8s} | {year:<5s} | {nt:>4d} | {wr:>5.1f}% | {avg_r:>+6.2f}% | '
                  f'{nav/10000:>8.1f}万 | {yr:>+7.1f}%')
        print()

    # 6年汇总
    print(f'{"="*100}')
    print('=== 6年汇总: CAGR + 最大回撤 + 关键指标 ===')
    print(f'{"版本":<8s} | {"最终净值":>10s} | {"6年CAGR":>9s} | {"最大回撤":>9s} | '
          f'{"回撤区间":<24s} | {"总交易":>5s} | {"总胜率":>6s} | {"年年正收益":<6s}')
    print('-' * 110)

    summary = []
    for vn in VERSIONS:
        info = all_results[vn]
        trades = info['trades']
        yn = info['yearly_nav']
        fc = info['final_capital']
        nav_log = info['daily_nav_log']

        cagr = (fc / INITIAL_CAPITAL) ** (1.0 / 6.0) - 1 if fc > 0 else -1
        max_dd, dd_peak_date, dd_trough_date = calc_max_drawdown(nav_log)

        prev_nav = INITIAL_CAPITAL
        all_pos = True
        for y in sorted(yn.keys()):
            r = (yn[y] - prev_nav) / prev_nav * 100 if prev_nav > 0 else 0
            if r <= 0:
                all_pos = False
            prev_nav = yn[y]

        nt = len(trades)
        wins = sum(1 for t in trades if t['pnl_pct'] > 0)
        wr = wins / nt * 100 if nt > 0 else 0

        dd_range = f'{dd_peak_date}~{dd_trough_date}' if dd_peak_date else 'N/A'
        print(f'{vn:<8s} | {fc/10000:>8.1f}万 | {cagr*100:>+7.1f}% | {max_dd*100:>7.1f}% | '
              f'{dd_range:<24s} | {nt:>5d} | {wr:>5.1f}% | {"Y" if all_pos else "N":<6s}')
        summary.append((vn, cagr, max_dd, fc, nt, wr))

    # 推荐
    print('\n' + '=' * 100)
    summary.sort(key=lambda x: x[1], reverse=True)
    print('=== CAGR排序 ===')
    for vn, cagr, dd, fc, nt, wr in summary:
        # Calmar比 = CAGR / MaxDD
        calmar = cagr / dd if dd > 0 else float('inf')
        print(f'  {vn:<8s}  CAGR={cagr*100:>+7.1f}%  MaxDD={dd*100:>5.1f}%  '
              f'Calmar={calmar:>5.2f}  最终净值={fc/10000:.1f}万')

    # 动态版本: 强信号交易统计
    if 'V3-DYN' in all_results:
        dyn_trades = all_results['V3-DYN']['trades']
        strong = [t for t in dyn_trades if t.get('is_strong')]
        normal = [t for t in dyn_trades if not t.get('is_strong')]
        print('\n' + '=' * 100)
        print('=== V3-DYN 信号分布(强=N1全仓 / 普通=N2) ===')
        for tag, lst in [('强信号(N=1)', strong), ('普通信号(N=2)', normal)]:
            if not lst:
                print(f'  {tag}: 0笔')
                continue
            n = len(lst)
            w = sum(1 for t in lst if t['pnl_pct'] > 0)
            avg = statistics.mean([t['pnl_pct'] for t in lst])
            print(f'  {tag}: {n}笔  胜率={w/n*100:.1f}%  均收益={avg:+.2f}%')

    print('=' * 100)


def main():
    print('=' * 100, flush=True)
    print('ZhaBan V3 仓位集中测试 (Task #16)', flush=True)
    print(f'初始资金: {INITIAL_CAPITAL/10000:.0f}万  区间: {START_DATE} ~ {END_DATE}', flush=True)
    print(f'测试版本: V3-N2(固定2仓)  V3-N1(全仓)  V3-DYN(强信号全仓/普通2仓)', flush=True)
    print('=' * 100, flush=True)

    conn = sqlite3.connect(DB_PATH)
    all_dates, date_idx, by_code, name_map = load_data(conn)
    conn.close()

    all_results = {}
    for vn, vp in VERSIONS.items():
        print(f'\n>>> 运行 {vn} ...', flush=True)
        trades, yearly_nav, fc, nav_log = run_simulation(
            by_code, name_map, all_dates, date_idx, vn, vp, V3_BASE)
        all_results[vn] = {'trades': trades, 'yearly_nav': yearly_nav,
                           'final_capital': fc, 'daily_nav_log': nav_log}
        cagr = (fc / INITIAL_CAPITAL) ** (1.0 / 6.0) - 1 if fc > 0 else -1
        dd, _, _ = calc_max_drawdown(nav_log)
        print(f'    {vn}: 交易={len(trades)}  最终={fc/10000:.1f}万  '
              f'CAGR={cagr*100:+.1f}%  MaxDD={dd*100:.1f}%', flush=True)

    print_results(all_results)


if __name__ == '__main__':
    main()
