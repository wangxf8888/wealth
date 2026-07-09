#!/usr/bin/env python3
"""ZhaBan + 6种无歧义出场机制验证 (Task #21)

背景:
  之前所有策略的 check_exit_today 存在 2 个 BUG 导致收益虚高:
    1. trailing 先 update peak 再 check (应先 check 后 update)
    2. 跳空止损时直接返回 stop_price (应返回 min(hour_open, stop_price))
  本脚本完全弃用 trailing, 用 6 种 100% 无歧义的出场方案验证 ZhaBan 入场是否仍有 alpha.

入场(所有版本共用):
  - P0 ZhaBan: T-1 触及涨停+未封住 + prev_turn>15% + prev_close_rate>5%
  - T 日 hour2 限价 = hour1_close * 0.99, hour2_low <= P 则以 P 成交
  - 涨停判定 round(close/preclose,2) >= 1.10/1.20
  - 排除 ST / 一字板 / 北交所 / T 日开盘涨停

出场版本(全部无歧义):
  V1 纯时间      : T+1 hour1_open 卖出
  V2 限价止盈+时间: TP=+5%, hour_high>=TP→TP成交; T+3 hour1_open
  V3 限价TP+宽止损: TP=+6%, SL=-5%, T+3 hour1_open
  V4 窄TP+紧止损 : TP=+3%, SL=-2%, T+2 hour1_open
  V5 分批限价    : TP1=+3%(50%) TP2=+6%(50%), SL=-5%(全), T+5 hour1_open
  V6 高开兑现+TP : T+1 hour1_open >= +2%→直接卖; 否则 TP=+5%, SL=-5%, T+3

合规铁律(本脚本严格遵守):
  - 止损成交价 = min(hour_open, stop_price), hour_open<stop 时以 hour_open 成交(跳空)
  - 止盈用限价 hour_high>=TP → 以 TP 成交(挂限价单)
  - 同一 hour 同时触发止盈/止损: 止损优先(worst case)
  - T+1: 买入当日 (T 日) 不可卖出
  - 不使用 trailing
  - T+max 到期日: 仅在 hour1_open 强制时间出场, 不再检查 SL/TP

仓位: N=3 固定 (每次净值/3), 初始 100 万, 区间 2020-02-01 ~ 2025-12-31.
"""
import sqlite3
import sys
import math
import statistics
from collections import defaultdict

# ============== 配置 ==============
DB_PATH = '/home/AIWealth/data/stocks.db'
INITIAL_CAPITAL = 1_000_000
START_DATE = '2020-02-01'
END_DATE = '2025-12-31'
N_SLOTS = 3

# 入场过滤
ENTRY = {
    'turn_min': 15.0,
    'cr_min': 5.0,
    'open_max': 5.0,        # T 日 open_rate < 5%
    'open_min': -5.0,       # 排除大幅低开
    'h1cr_min': -3.0,       # T 日 hour1 已大跌 → 弃用
    'buy_offset': 0.01,     # hour2 限价 = h1_close*(1-offset)
}

# 6 种出场版本
VERSIONS = {
    'V1': {'desc': '纯时间T+1开盘',     'tp': None, 'sl': None, 'hold': 1, 'special': None},
    'V2': {'desc': 'TP+5% / T+3',       'tp': 0.05, 'sl': None, 'hold': 3, 'special': None},
    'V3': {'desc': 'TP+6%/SL-5%/T+3',   'tp': 0.06, 'sl': 0.05, 'hold': 3, 'special': None},
    'V4': {'desc': 'TP+3%/SL-2%/T+2',   'tp': 0.03, 'sl': 0.02, 'hold': 2, 'special': None},
    'V5': {'desc': '分批TP+3/+6 SL-5 T+5', 'tp1': 0.03, 'tp2': 0.06, 'sl': 0.05,
           'hold': 5, 'special': 'partial'},
    'V6': {'desc': '高开+2%兑现 TP+5/SL-5 T+3', 'tp': 0.05, 'sl': 0.05, 'hold': 3,
           'special': 'gap_up_exit', 'gap_pct': 0.02},
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


# 字段索引(精简tuple, 已去掉 date/code/name 前 3 字段)
I_PRECLOSE, I_OPEN, I_HIGH, I_LOW, I_CLOSE = 0, 1, 2, 3, 4
I_CR, I_OR, I_TURN, I_ISST = 5, 6, 7, 8
I_H1O, I_H1H, I_H1L, I_H1C, I_H1CR = 9, 10, 11, 12, 13
I_H2O, I_H2H, I_H2L, I_H2C = 14, 15, 16, 17
I_H3O, I_H3H, I_H3L, I_H3C = 18, 19, 20, 21
I_H4O, I_H4H, I_H4L, I_H4C = 22, 23, 24, 25

HOUR_FIELDS = {
    1: (I_H1O, I_H1H, I_H1L, I_H1C),
    2: (I_H2O, I_H2H, I_H2L, I_H2C),
    3: (I_H3O, I_H3H, I_H3L, I_H3C),
    4: (I_H4O, I_H4H, I_H4L, I_H4C),
}


def get_hour_ohlc(row, h):
    oi, hi, li, ci = HOUR_FIELDS[h]
    return sv(row[oi]), sv(row[hi]), sv(row[li]), sv(row[ci])


def load_data(conn):
    cur = conn.cursor()
    print('[1/2] 加载交易日列表...', flush=True)
    cur.execute("SELECT DISTINCT date FROM stock_kline "
                "WHERE date>='2019-12-01' AND date<='2026-01-31' ORDER BY date")
    all_dates = [sys.intern(r[0]) for r in cur.fetchall()]
    date_idx = {d: i for i, d in enumerate(all_dates)}
    print(f'  交易日数: {len(all_dates)}', flush=True)

    print('[2/2] 加载股票数据(精简存储)...', flush=True)
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
            by_code[code][d] = row[3:]
            n_rows += 1
        print(f'  已加载 {n_rows:,} 行...', flush=True)
        batch = cur.fetchmany(500000)
    print(f'  股票数={len(by_code)} 行数={n_rows:,}', flush=True)
    return all_dates, date_idx, by_code, name_map


def find_zhaban_candidates(by_code, name_map, all_dates, date_idx, target_date):
    """筛选 target_date 的 ZhaBan 候选股: T-1 触及涨停但未封住, T 日有 hour1 数据."""
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
        # 炸板: T-1 触及涨停但未封住
        if not hit_limit_up(prev_high, prev_pc, code):
            continue
        if closed_limit_up(prev_close, prev_pc, code):
            continue
        # T 日: 排除一字板
        t_o = sv(t_row[I_OPEN])
        t_h = sv(t_row[I_HIGH])
        t_l = sv(t_row[I_LOW])
        t_c = sv(t_row[I_CLOSE])
        if t_o and t_h and t_l and t_c and t_o == t_h == t_l == t_c:
            continue
        t_pc = sv(t_row[I_PRECLOSE])
        if not t_pc or not t_o:
            continue
        # T 日开盘即涨停 → 无法买入
        if open_at_limit_up(t_o, t_pc, code):
            continue
        opr = sv(t_row[I_OR])
        if opr is None or opr < ENTRY['open_min'] or opr > 9.5:
            continue
        if sv(t_row[I_H1O]) is None:
            continue
        cands.append({
            'code': code,
            'name': name,
            't_idx': t_idx,
            'open_rate': opr,
            'prev_turn': sv(prev_row[I_TURN]),
            'prev_close_rate': sv(prev_row[I_CR]),
        })
    return cands


def apply_entry_filter(cands):
    out = []
    for c in cands:
        if c['prev_turn'] is None or c['prev_turn'] < ENTRY['turn_min']:
            continue
        if c['prev_close_rate'] is None or c['prev_close_rate'] < ENTRY['cr_min']:
            continue
        if c['open_rate'] >= ENTRY['open_max']:
            continue
        out.append(c)
    out.sort(key=lambda x: (x['prev_close_rate'] or 0), reverse=True)
    return out


def try_limit_buy(by_code, all_dates, code, t_idx, offset):
    """T 日 hour2 限价 P=h1_close*(1-offset), 若 h2_low<=P 则以 P 成交."""
    d = all_dates[t_idx]
    row = by_code.get(code, {}).get(d)
    if not row:
        return None
    h1c = sv(row[I_H1C])
    h1cr = sv(row[I_H1CR])
    h2l = sv(row[I_H2L])
    if h1c is None or h2l is None:
        return None
    if h1cr is not None and h1cr <= ENTRY['h1cr_min']:
        return None
    limit_p = h1c * (1 - offset)
    if h2l <= limit_p:
        return limit_p
    return None


class Position:
    __slots__ = ['code', 'name', 'buy_price', 'buy_date', 'buy_idx',
                 'amount', 'shares', 'qty_left', 'tp1_done']

    def __init__(self, code, name, buy_price, buy_date, buy_idx, amount):
        self.code = code
        self.name = name
        self.buy_price = buy_price
        self.buy_date = buy_date
        self.buy_idx = buy_idx
        self.amount = amount
        self.shares = amount / buy_price
        self.qty_left = 1.0   # 剩余仓位比例 (V5 分批用)
        self.tp1_done = False  # V5 TP1 是否已触发


def check_exit_hour(pos, today_idx, h, row, ver):
    """返回该 hour 内的所有出场事件 list[(sell_price, qty_pct, reason)].

    严格遵守:
      - T+max hour1: 强制时间出场, 该日不再检查 SL/TP
      - SL 优先于 TP (同 hour 双触发时取 worst case)
      - SL 跳空: hour_open<sl_p 时以 hour_open 成交
      - TP 限价: hour_high>=tp_p 时以 tp_p 成交
    """
    days = today_idx - pos.buy_idx
    ho, hh, hl, hc = get_hour_ohlc(row, h)
    if ho is None or hh is None or hl is None:
        return []

    bp = pos.buy_price

    # 时间出场: T+max 当日仅在 hour1_open 卖出, 不再处理其他事件
    if days >= ver['hold']:
        if h == 1:
            return [(ho, pos.qty_left, 'time')]
        return []  # T+max 后续 hour 都已出清

    # V6 高开兑现: T+1 hour1_open >= bp*(1+gap_pct) 直接卖
    if ver.get('special') == 'gap_up_exit' and days == 1 and h == 1:
        if ho >= bp * (1 + ver['gap_pct']):
            return [(ho, pos.qty_left, 'gap_up_exit')]

    # 止损 (优先)
    sl = ver.get('sl')
    if sl is not None:
        sl_p = bp * (1 - sl)
        if ho < sl_p:
            return [(ho, pos.qty_left, 'sl_gap')]
        if hl <= sl_p:
            return [(sl_p, pos.qty_left, 'sl')]

    # 止盈
    if ver.get('special') == 'partial':
        tp1 = bp * (1 + ver['tp1'])
        tp2 = bp * (1 + ver['tp2'])
        events = []
        if (not pos.tp1_done) and hh >= tp1:
            events.append((tp1, 0.5, 'tp1'))
        # tp2: 如本 hour 已先卖 0.5, 剩 qty_left-0.5; 否则全部
        qty_after_tp1 = pos.qty_left - (0.5 if events else 0.0)
        if qty_after_tp1 > 1e-6 and hh >= tp2:
            events.append((tp2, qty_after_tp1, 'tp2'))
        return events

    tp = ver.get('tp')
    if tp is not None:
        tp_p = bp * (1 + tp)
        if hh >= tp_p:
            return [(tp_p, pos.qty_left, 'tp')]

    return []


def compute_total_nav(capital, positions, by_code, today):
    """以当日 close 估值持仓 (按 qty_left)."""
    pos_value = 0.0
    for pos in positions:
        if pos.qty_left <= 1e-6:
            continue
        row = by_code.get(pos.code, {}).get(today)
        c = sv(row[I_CLOSE]) if row else None
        if c:
            pos_value += pos.shares * pos.qty_left * c
        else:
            pos_value += pos.shares * pos.qty_left * pos.buy_price
    return capital + pos_value


def run_simulation(by_code, name_map, all_dates, date_idx, ver_name, ver):
    capital = float(INITIAL_CAPITAL)
    positions = []
    trades = []
    yearly_nav = {}
    daily_nav_log = []

    target_dates = [d for d in all_dates if START_DATE <= d <= END_DATE]
    if not target_dates:
        return trades, yearly_nav, capital, daily_nav_log

    current_year = target_dates[0][:4]

    for td in target_dates:
        t_idx = date_idx[td]
        year = td[:4]
        if year != current_year:
            prev_d = all_dates[t_idx - 1] if t_idx > 0 else td
            yearly_nav[current_year] = compute_total_nav(capital, positions, by_code, prev_d)
            current_year = year

        # 1) 出场检查
        new_positions = []
        for pos in positions:
            if pos.buy_idx >= t_idx:  # T+1: 买入当日不可卖
                new_positions.append(pos)
                continue
            row = by_code.get(pos.code, {}).get(td)
            if not row:
                new_positions.append(pos)
                continue
            closed = False
            for h in (1, 2, 3, 4):
                if pos.qty_left <= 1e-6:
                    closed = True
                    break
                events = check_exit_hour(pos, t_idx, h, row, ver)
                for (sp, qpct, reason) in events:
                    qpct = min(qpct, pos.qty_left)
                    if qpct <= 1e-6:
                        continue
                    proceeds = pos.shares * qpct * sp
                    capital += proceeds
                    trades.append({
                        'buy_date': pos.buy_date, 'sell_date': td,
                        'code': pos.code, 'name': pos.name,
                        'buy_price': pos.buy_price, 'sell_price': sp,
                        'qty_pct': qpct,
                        'pnl_pct': (sp - pos.buy_price) / pos.buy_price * 100.0,
                        'reason': reason,
                    })
                    pos.qty_left -= qpct
                    if reason == 'tp1':
                        pos.tp1_done = True
                    if pos.qty_left <= 1e-6:
                        closed = True
                        break
                if closed:
                    break
            if not closed and pos.qty_left > 1e-6:
                new_positions.append(pos)
        positions = new_positions

        # 2) 入场
        cands_raw = find_zhaban_candidates(by_code, name_map, all_dates, date_idx, td)
        cands = apply_entry_filter(cands_raw)
        for cand in cands:
            if len(positions) >= N_SLOTS:
                break
            bp = try_limit_buy(by_code, all_dates, cand['code'], t_idx, ENTRY['buy_offset'])
            if bp is None:
                continue
            # 仓位资金 = (capital + 持仓成本残值) / N
            invested = sum(p.shares * p.qty_left * p.buy_price for p in positions)
            total_assets = capital + invested
            slot_amt = total_assets / N_SLOTS
            if slot_amt > capital:
                slot_amt = capital
            if slot_amt < 1000:
                continue
            positions.append(Position(cand['code'], cand['name'], bp, td, t_idx, slot_amt))
            capital -= slot_amt

        # 3) 日末净值
        nav_today = compute_total_nav(capital, positions, by_code, td)
        daily_nav_log.append((td, nav_today))

    # 期末强平 (按最后交易日 close)
    last_date = target_dates[-1]
    for pos in positions:
        if pos.qty_left <= 1e-6:
            continue
        row = by_code.get(pos.code, {}).get(last_date)
        sp = sv(row[I_CLOSE]) if row else None
        if not sp:
            sp = pos.buy_price
        proceeds = pos.shares * pos.qty_left * sp
        capital += proceeds
        trades.append({
            'buy_date': pos.buy_date, 'sell_date': last_date,
            'code': pos.code, 'name': pos.name,
            'buy_price': pos.buy_price, 'sell_price': sp,
            'qty_pct': pos.qty_left,
            'pnl_pct': (sp - pos.buy_price) / pos.buy_price * 100.0,
            'reason': 'final_close',
        })
        pos.qty_left = 0.0
    yearly_nav[current_year] = capital
    return trades, yearly_nav, capital, daily_nav_log


def calc_max_drawdown(nav_log):
    if not nav_log:
        return 0.0
    peak = nav_log[0][1]
    max_dd = 0.0
    for _, n in nav_log:
        if n > peak:
            peak = n
        dd = (peak - n) / peak if peak > 0 else 0
        if dd > max_dd:
            max_dd = dd
    return max_dd


def yearly_breakdown(trades, yearly_nav):
    """按 sell_date 年份汇总成交 (qty_pct 加权)."""
    rows = []
    prev_nav = INITIAL_CAPITAL
    for year in sorted(yearly_nav.keys()):
        yt = [t for t in trades if t['sell_date'][:4] == year]
        n = len(yt)
        # 按 qty_pct 加权胜率与平均收益
        total_w = sum(t['qty_pct'] for t in yt)
        if total_w > 0:
            wins_w = sum(t['qty_pct'] for t in yt if t['pnl_pct'] > 0)
            wr = wins_w / total_w * 100.0
            avg_r = sum(t['pnl_pct'] * t['qty_pct'] for t in yt) / total_w
        else:
            wr = 0.0
            avg_r = 0.0
        nav = yearly_nav[year]
        yr = (nav - prev_nav) / prev_nav * 100.0 if prev_nav > 0 else 0.0
        rows.append((year, n, wr, avg_r, nav, yr))
        prev_nav = nav
    return rows


def print_results(all_results):
    print('\n' + '=' * 110)
    print('============ ZhaBan 6 种无歧义出场机制验证 (Task #21) ============')
    print('=' * 110)
    print('入场: T-1 涨停+炸板 + prev_turn>15% + prev_cr>5% + open_rate<5%')
    print('买入: T 日 hour2 限价 P=hour1_close*0.99, h2_low<=P 则以 P 成交')
    print(f'仓位: N={N_SLOTS} 固定; 初始资金 100 万; 区间 {START_DATE} ~ {END_DATE}')
    print('合规: SL=min(open,sl_p) 处理跳空; TP 限价(hh>=TP); SL 优先; T+max 仅 h1_open 强平')
    print()

    # 逐年明细
    print('=' * 110)
    print('=== 逐年明细 (跨年净值复利) ===')
    print(f'{"版本":<5s} | {"描述":<28s} | {"年份":<5s} | {"成交":>5s} | '
          f'{"胜率":>6s} | {"均收益":>7s} | {"年末净值":>10s} | {"年化":>8s}')
    print('-' * 110)
    for vn, ver in VERSIONS.items():
        info = all_results[vn]
        rows = yearly_breakdown(info['trades'], info['yearly_nav'])
        for (year, n, wr, avg_r, nav, yr) in rows:
            print(f'{vn:<5s} | {ver["desc"]:<28s} | {year:<5s} | {n:>5d} | '
                  f'{wr:>5.1f}% | {avg_r:>+6.2f}% | {nav/10000:>8.1f}万 | {yr:>+7.1f}%')
        print()

    # 6 年汇总
    print('=' * 110)
    print('=== 6 年汇总 ===')
    print(f'{"版本":<5s} | {"描述":<28s} | {"最终净值":>10s} | {"6yCAGR":>8s} | '
          f'{"MaxDD":>7s} | {"总成交":>6s} | {"总胜率":>6s} | {"均收益":>7s} | {"正α":<3s}')
    print('-' * 110)
    summary = []
    for vn, ver in VERSIONS.items():
        info = all_results[vn]
        trades = info['trades']
        fc = info['final_capital']
        cagr = (fc / INITIAL_CAPITAL) ** (1.0 / 6.0) - 1 if fc > 0 else -1
        max_dd = calc_max_drawdown(info['daily_nav_log'])
        n = len(trades)
        total_w = sum(t['qty_pct'] for t in trades)
        if total_w > 0:
            wins_w = sum(t['qty_pct'] for t in trades if t['pnl_pct'] > 0)
            wr = wins_w / total_w * 100.0
            avg_r = sum(t['pnl_pct'] * t['qty_pct'] for t in trades) / total_w
        else:
            wr = 0.0
            avg_r = 0.0
        positive_alpha = 'Y' if cagr > 0 else 'N'
        print(f'{vn:<5s} | {ver["desc"]:<28s} | {fc/10000:>8.1f}万 | {cagr*100:>+6.1f}% | '
              f'{max_dd*100:>5.1f}% | {n:>6d} | {wr:>5.1f}% | {avg_r:>+6.2f}% | {positive_alpha:<3s}')
        summary.append((vn, cagr, max_dd, fc, n, wr, avg_r))

    # CAGR 排序
    print('\n' + '=' * 110)
    print('=== 按 CAGR 排序 ===')
    summary.sort(key=lambda x: x[1], reverse=True)
    for vn, cagr, dd, fc, n, wr, avg_r in summary:
        calmar = cagr / dd if dd > 0 else float('inf')
        flag = '★ 正 alpha' if cagr > 0 else ''
        print(f'  {vn:<5s} CAGR={cagr*100:>+7.1f}%  MaxDD={dd*100:>5.1f}%  '
              f'Calmar={calmar:>5.2f}  胜率={wr:>5.1f}%  均收益={avg_r:>+5.2f}%  '
              f'最终={fc/10000:.1f}万  {flag}')

    # 出场原因分布
    print('\n' + '=' * 110)
    print('=== 出场原因分布 (按 qty_pct 加权) ===')
    for vn in VERSIONS:
        trades = all_results[vn]['trades']
        if not trades:
            continue
        reason_w = defaultdict(float)
        reason_pnl = defaultdict(float)
        for t in trades:
            reason_w[t['reason']] += t['qty_pct']
            reason_pnl[t['reason']] += t['pnl_pct'] * t['qty_pct']
        total_w = sum(reason_w.values())
        parts = []
        for r in sorted(reason_w.keys()):
            w = reason_w[r]
            avg = reason_pnl[r] / w if w > 0 else 0
            parts.append(f'{r}={w/total_w*100:.0f}%(均{avg:+.1f}%)')
        print(f'  {vn:<5s} {" ".join(parts)}')

    print('=' * 110)


def main():
    print('=' * 110, flush=True)
    print('ZhaBan + 6 种无歧义出场机制验证 (Task #21)', flush=True)
    print(f'初始资金: {INITIAL_CAPITAL/10000:.0f} 万   区间: {START_DATE} ~ {END_DATE}   '
          f'仓位: N={N_SLOTS}', flush=True)
    print('=' * 110, flush=True)

    conn = sqlite3.connect(DB_PATH)
    all_dates, date_idx, by_code, name_map = load_data(conn)
    conn.close()

    all_results = {}
    for vn, ver in VERSIONS.items():
        print(f'\n>>> 运行 {vn} ({ver["desc"]}) ...', flush=True)
        trades, yearly_nav, fc, nav_log = run_simulation(
            by_code, name_map, all_dates, date_idx, vn, ver)
        all_results[vn] = {
            'trades': trades, 'yearly_nav': yearly_nav,
            'final_capital': fc, 'daily_nav_log': nav_log,
        }
        cagr = (fc / INITIAL_CAPITAL) ** (1.0 / 6.0) - 1 if fc > 0 else -1
        dd = calc_max_drawdown(nav_log)
        print(f'    {vn}: 成交={len(trades)}  最终={fc/10000:.1f}万  '
              f'CAGR={cagr*100:+.1f}%  MaxDD={dd*100:.1f}%', flush=True)

    print_results(all_results)


if __name__ == '__main__':
    main()
