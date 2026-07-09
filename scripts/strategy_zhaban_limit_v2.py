#!/usr/bin/env python3
"""ZhaBan限价单 Phase-4 向500%冲刺优化

核心创新:
  1. 自适应风控: 根据T-1日red_ratio切换风控模式
     - 强势市(red>55%): SL3%+TP6% (高胜率锁定)
     - 弱势/震荡市: SL2%+Trailing2% (高盈亏比)
  2. 条件过滤增强: close_rate/turn/open_rate多重过滤
  3. N=3仓位模拟: 连续6年(2020-2025)不重置资金
  4. 6版本并行对比

合规铁律:
  - 限价P基于事先(上一hour)数据计算, 非事后
  - T+1: T日买入最早T+1卖出
  - 涨停判定: round(close/preclose, 2)严格规则
  - red_ratio: 用T-1日(前一交易日), 非T日

用法:
  python3 scripts/strategy_zhaban_limit_v2.py
"""
import sqlite3
import sys
import math
import statistics
from collections import defaultdict
from datetime import datetime, timedelta

# ============== 配置区 ==============
DB_PATH = '/home/AIWealth/data/stocks.db'
INDEX_CODE = 'sh.000001'
INITIAL_CAPITAL = 1_000_000  # 100万
N_SLOTS = 3  # 最大持仓数
START_DATE = '2020-02-01'
END_DATE = '2025-12-31'
# ===================================

# ============== 6个版本参数定义 ==============
VERSIONS = {
    'V1': {'turn_min': 15.0, 'cr_min': None, 'open_max': 5.0, 'buy_offset': 0.01,
            'risk_mode': 'adaptive', 'red_threshold': 55,
            'sl_strong': 3.0, 'tp_strong': 6.0, 'sl_weak': 2.0, 'trail_weak': 2.0,
            'hold_max_days': 2},
    'V2': {'turn_min': 15.0, 'cr_min': 5.0, 'open_max': 5.0, 'buy_offset': 0.01,
            'risk_mode': 'adaptive', 'red_threshold': 55,
            'sl_strong': 3.0, 'tp_strong': 6.0, 'sl_weak': 2.0, 'trail_weak': 2.0,
            'hold_max_days': 2},
    'V3': {'turn_min': 15.0, 'cr_min': 5.0, 'open_max': 5.0, 'buy_offset': 0.01,
            'risk_mode': 'trailing', 'sl_pct': 2.0, 'trail_pct': 2.0,
            'hold_max_days': 3},
    'V4': {'turn_min': 15.0, 'cr_min': 5.0, 'open_max': 5.0, 'buy_offset': 0.01,
            'risk_mode': 'fixed', 'sl_pct': 3.0, 'tp_pct': 6.0,
            'hold_max_days': 1},
    'V5': {'turn_min': 20.0, 'cr_min': 8.0, 'open_max': 5.0, 'buy_offset': 0.01,
            'risk_mode': 'adaptive', 'red_threshold': 55,
            'sl_strong': 3.0, 'tp_strong': 6.0, 'sl_weak': 2.0, 'trail_weak': 2.0,
            'hold_max_days': 2},
    'V6': {'turn_min': 15.0, 'cr_min': None, 'open_max': 5.0, 'buy_offset': 0.02,
            'risk_mode': 'adaptive', 'red_threshold': 55,
            'sl_strong': 3.0, 'tp_strong': 6.0, 'sl_weak': 2.0, 'trail_weak': 2.0,
            'hold_max_days': 2},
}


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


# ============== 高效数据加载(仅加载必要字段) ==============

def load_data_efficient(conn):
    """分步加载数据, 内存优化"""
    cur = conn.cursor()
    print('[1/3] 加载交易日列表...', flush=True)
    cur.execute("SELECT DISTINCT date FROM stock_kline WHERE date>='2019-12-01' AND date<='2026-01-31' ORDER BY date")
    all_dates = [r[0] for r in cur.fetchall()]
    date_idx = {d: i for i, d in enumerate(all_dates)}
    print(f'  交易日数: {len(all_dates)}', flush=True)

    # 加载red_ratio
    print('[2/3] 加载red_ratio...', flush=True)
    cur.execute("SELECT date, red_ratio FROM index_kline WHERE code=? AND date>='2019-12-01'", (INDEX_CODE,))
    red_ratio = {r[0]: sv(r[1]) for r in cur.fetchall()}
    print(f'  red_ratio日数: {len(red_ratio)}', flush=True)

    # 加载日级筛选数据(code, date, preclose, open, high, low, close, close_rate, open_rate, turn, isST, code_name)
    # + hour级OHLC (必要字段)
    print('[3/3] 加载股票数据(精简字段)...', flush=True)
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

    # 紧凑存储: by_code[code][date] = tuple(values)
    # 索引: 0=date,1=code,2=name,3=preclose,4=open,5=high,6=low,7=close,
    #        8=close_rate,9=open_rate,10=turn,11=isST,
    #        12=h1o,13=h1h,14=h1l,15=h1c,16=h1cr,
    #        17=h2o,18=h2h,19=h2l,20=h2c,
    #        21=h3o,22=h3h,23=h3l,24=h3c,
    #        25=h4o,26=h4h,27=h4l,28=h4c
    by_code = defaultdict(dict)
    n_rows = 0
    batch = cur.fetchmany(500000)
    while batch:
        for row in batch:
            by_code[row[1]][row[0]] = row  # tuple存储(比dict省内存)
            n_rows += 1
        print(f'  已加载 {n_rows:,} 行...', flush=True)
        batch = cur.fetchmany(500000)

    print(f'  总计: 股票数={len(by_code)}  行数={n_rows:,}', flush=True)
    return all_dates, date_idx, by_code, red_ratio


# 字段索引常量
I_DATE, I_CODE, I_NAME, I_PRECLOSE, I_OPEN, I_HIGH, I_LOW, I_CLOSE = 0, 1, 2, 3, 4, 5, 6, 7
I_CR, I_OR, I_TURN, I_ISST = 8, 9, 10, 11
I_H1O, I_H1H, I_H1L, I_H1C, I_H1CR = 12, 13, 14, 15, 16
I_H2O, I_H2H, I_H2L, I_H2C = 17, 18, 19, 20
I_H3O, I_H3H, I_H3L, I_H3C = 21, 22, 23, 24
I_H4O, I_H4H, I_H4L, I_H4C = 25, 26, 27, 28

# hour字段索引映射: hour_idx[day_offset][hour] = (open_idx, high_idx, low_idx, close_idx)
# day_offset=0 means same row; we need to get the row for the specific date
HOUR_FIELDS = {1: (I_H1O, I_H1H, I_H1L, I_H1C),
               2: (I_H2O, I_H2H, I_H2L, I_H2C),
               3: (I_H3O, I_H3H, I_H3L, I_H3C),
               4: (I_H4O, I_H4H, I_H4L, I_H4C)}


def find_zhaban_candidates(by_code, all_dates, date_idx, target_date, red_ratio):
    """筛选target_date的炸板候选股, 返回list of dict"""
    t_idx = date_idx.get(target_date)
    if t_idx is None or t_idx < 1:
        return []
    prev_date = all_dates[t_idx - 1]
    prev_red = red_ratio.get(prev_date)

    candidates = []
    for code, dm in by_code.items():
        prev_row = dm.get(prev_date)
        t_row = dm.get(target_date)
        if not prev_row or not t_row:
            continue
        # ST过滤
        if prev_row[I_ISST] == 1 or t_row[I_ISST] == 1:
            continue
        name = t_row[I_NAME] or prev_row[I_NAME] or ''
        if 'ST' in name.upper():
            continue
        # 炸板: T-1日触及涨停但未封住
        prev_pc = sv(prev_row[I_PRECLOSE])
        prev_high = sv(prev_row[I_HIGH])
        prev_close = sv(prev_row[I_CLOSE])
        if not prev_pc or not prev_high or prev_close is None:
            continue
        if not hit_limit_up(prev_high, prev_pc, code):
            continue
        if closed_limit_up(prev_close, prev_pc, code):
            continue
        # T日不能一字板
        t_o, t_h, t_l, t_c = sv(t_row[I_OPEN]), sv(t_row[I_HIGH]), sv(t_row[I_LOW]), sv(t_row[I_CLOSE])
        if t_o and t_h and t_l and t_c and t_o == t_h == t_l == t_c:
            continue
        # T日不能涨停开盘
        t_pc = sv(t_row[I_PRECLOSE])
        if not t_pc or not t_o:
            continue
        if open_at_limit_up(t_o, t_pc, code):
            continue
        # 开盘幅度
        opr = sv(t_row[I_OR])
        if opr is None or opr < -5.0 or opr > 9.5:
            continue
        # hour1数据可用
        if sv(t_row[I_H1O]) is None:
            continue

        candidates.append({
            'code': code, 'name': name, 't_idx': t_idx,
            'open_rate': opr,
            'prev_turn': sv(prev_row[I_TURN]),
            'prev_close_rate': sv(prev_row[I_CR]),
            'prev_red_ratio': prev_red,
        })
    return candidates


def apply_filter(candidates, ver_params):
    """按版本参数过滤+排序"""
    out = []
    for c in candidates:
        if c['prev_turn'] is None or c['prev_turn'] < ver_params['turn_min']:
            continue
        if ver_params['cr_min'] is not None:
            if c['prev_close_rate'] is None or c['prev_close_rate'] < ver_params['cr_min']:
                continue
        if c['open_rate'] >= ver_params['open_max']:
            continue
        out.append(c)
    out.sort(key=lambda x: (x['prev_close_rate'] or 0), reverse=True)
    return out


def try_limit_buy(by_code, all_dates, code, t_idx, offset):
    """限价买入验证"""
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
    __slots__ = ['code', 'name', 'buy_price', 'buy_date', 'buy_idx', 'amount', 'shares', 'risk_label', 'peak']
    def __init__(self, code, name, buy_price, buy_date, buy_idx, amount, risk_label):
        self.code = code
        self.name = name
        self.buy_price = buy_price
        self.buy_date = buy_date
        self.buy_idx = buy_idx
        self.amount = amount
        self.shares = amount / buy_price
        self.risk_label = risk_label
        self.peak = buy_price


def get_hour_ohlc(row, h):
    """获取tuple row中hour h的OHLC"""
    oi, hi, li, ci = HOUR_FIELDS[h]
    return sv(row[oi]), sv(row[hi]), sv(row[li]), sv(row[ci])


def check_exit_today(by_code, pos, today, today_idx, ver_params, red_ratio, all_dates, date_idx):
    """检查持仓pos今天是否触发退出, 返回(sell_price, reason) or (None, None)"""
    code = pos.code
    row = by_code.get(code, {}).get(today)
    if not row:
        return None, None

    days_held = today_idx - pos.buy_idx
    hold_max = ver_params['hold_max_days']
    is_last_day = (days_held >= hold_max)

    # 确定风控参数
    rm = ver_params['risk_mode']
    if rm == 'adaptive':
        # 用买入日T-1的red_ratio判定
        bi = pos.buy_idx
        prev_red = red_ratio.get(all_dates[bi - 1]) if bi > 0 else None
        is_strong = (prev_red is not None and prev_red > ver_params['red_threshold'])
        if is_strong:
            use_trailing = False
            sl_pct = ver_params['sl_strong'] / 100.0
            tp_pct = ver_params['tp_strong'] / 100.0
        else:
            use_trailing = True
            sl_pct = ver_params['sl_weak'] / 100.0
            trail_pct = ver_params['trail_weak'] / 100.0
    elif rm == 'trailing':
        use_trailing = True
        sl_pct = ver_params['sl_pct'] / 100.0
        trail_pct = ver_params['trail_pct'] / 100.0
    else:  # fixed
        use_trailing = False
        sl_pct = ver_params['sl_pct'] / 100.0
        tp_pct = ver_params['tp_pct'] / 100.0

    buy_price = pos.buy_price
    stop_price = buy_price * (1 - sl_pct)
    if not use_trailing:
        limit_sell = buy_price * (1 + tp_pct)

    peak = pos.peak

    for h in (1, 2, 3, 4):
        ho, hh, hl, hc = get_hour_ohlc(row, h)
        if hh is None or hl is None:
            continue

        # 强平
        if is_last_day and h == 4:
            sell_p = ho if ho else hc
            if sell_p:
                pos.peak = max(peak, hh) if hh else peak
                return sell_p, 'force_close'
            continue

        if use_trailing:
            if hh > peak:
                peak = hh
            trailing_price = peak * (1 - trail_pct)
            if hl <= stop_price:
                pos.peak = peak
                return stop_price, 'stop_loss'
            if peak > buy_price and hl <= trailing_price:
                pos.peak = peak
                return trailing_price, 'trailing'
        else:
            if ho is not None and ho >= buy_price:
                if hh >= limit_sell:
                    return limit_sell, 'take_profit'
                if hl <= stop_price:
                    return stop_price, 'stop_loss'
            else:
                if hl <= stop_price:
                    return stop_price, 'stop_loss'
                if hh >= limit_sell:
                    return limit_sell, 'take_profit'

        if hh and hh > peak:
            peak = hh

    pos.peak = peak

    # 最后一天强平(无h4数据)
    if is_last_day:
        for h in (4, 3, 2, 1):
            ho, _, _, hc = get_hour_ohlc(row, h)
            p = ho or hc
            if p:
                return p, 'force_close'

    return None, None


def run_simulation(by_code, all_dates, date_idx, red_ratio, ver_name, ver_params):
    """运行N=3仓位模拟"""
    capital = float(INITIAL_CAPITAL)
    positions = []
    trades = []
    yearly_nav = {}

    target_dates = [d for d in all_dates if START_DATE <= d <= END_DATE]
    if not target_dates:
        return trades, yearly_nav, capital

    current_year = target_dates[0][:4]

    for td in target_dates:
        t_idx = date_idx[td]
        year = td[:4]

        # 年度切换
        if year != current_year:
            # 计算净值(capital + 持仓市值估算)
            pos_value = 0
            for pos in positions:
                row = by_code.get(pos.code, {}).get(all_dates[t_idx - 1])  # 用上一交易日close
                if row:
                    c = sv(row[I_CLOSE])
                    if c:
                        pos_value += pos.shares * c
                    else:
                        pos_value += pos.amount
                else:
                    pos_value += pos.amount
            yearly_nav[current_year] = capital + pos_value
            current_year = year

        # 1. 检查持仓卖出
        new_positions = []
        for pos in positions:
            if pos.buy_idx >= t_idx:  # T+1限制
                new_positions.append(pos)
                continue
            days_held = t_idx - pos.buy_idx
            if days_held < 1:
                new_positions.append(pos)
                continue

            sell_price, reason = check_exit_today(by_code, pos, td, t_idx, ver_params, red_ratio, all_dates, date_idx)
            if sell_price:
                pnl_pct = (sell_price - pos.buy_price) / pos.buy_price * 100
                pnl_amount = pos.shares * (sell_price - pos.buy_price)
                capital += pos.amount + pnl_amount
                trades.append({
                    'buy_date': pos.buy_date, 'sell_date': td,
                    'code': pos.code, 'name': pos.name,
                    'buy_price': pos.buy_price, 'sell_price': sell_price,
                    'pnl_pct': pnl_pct, 'reason': reason,
                    'risk_mode': pos.risk_label,
                })
            else:
                new_positions.append(pos)
        positions = new_positions

        # 2. 买入新仓位
        empty_slots = N_SLOTS - len(positions)
        if empty_slots > 0:
            candidates = find_zhaban_candidates(by_code, all_dates, date_idx, td, red_ratio)
            candidates = apply_filter(candidates, ver_params)
            bought = 0
            for cand in candidates:
                if bought >= empty_slots:
                    break
                bp = try_limit_buy(by_code, all_dates, cand['code'], t_idx, ver_params['buy_offset'])
                if bp is None:
                    continue
                total_assets = capital + sum(p.amount for p in positions)
                slot_amt = total_assets / N_SLOTS
                if slot_amt > capital:
                    slot_amt = capital
                if slot_amt < 1000:
                    break

                # 风控标签
                rm = ver_params['risk_mode']
                if rm == 'adaptive':
                    prev_red = cand['prev_red_ratio']
                    if prev_red is not None and prev_red > ver_params['red_threshold']:
                        rl = f'strong({prev_red:.0f})'
                    else:
                        rl = f'weak({prev_red:.0f})' if prev_red else 'weak(?)'
                elif rm == 'trailing':
                    rl = 'trailing'
                else:
                    rl = 'fixed'

                positions.append(Position(cand['code'], cand['name'], bp, td, t_idx, slot_amt, rl))
                capital -= slot_amt
                bought += 1

    # 期末强平
    last_date = target_dates[-1]
    for pos in positions:
        row = by_code.get(pos.code, {}).get(last_date)
        sp = sv(row[I_CLOSE]) if row else pos.buy_price
        if not sp:
            sp = pos.buy_price
        pnl_pct = (sp - pos.buy_price) / pos.buy_price * 100
        pnl_amount = pos.shares * (sp - pos.buy_price)
        capital += pos.amount + pnl_amount
        trades.append({'buy_date': pos.buy_date, 'sell_date': last_date,
                       'code': pos.code, 'name': pos.name,
                       'buy_price': pos.buy_price, 'sell_price': sp,
                       'pnl_pct': pnl_pct, 'reason': 'final_close',
                       'risk_mode': pos.risk_label})
    positions = []
    yearly_nav[current_year] = capital

    return trades, yearly_nav, capital


def print_all_results(all_results):
    """格式化输出所有结果"""
    print('\n' + '=' * 90)
    print('============ ZhaBan限价单 Phase-4 向500%冲刺 ============')
    print('=' * 90)

    # 版本参数
    print('\n--- 版本参数说明 ---')
    for vn, vp in VERSIONS.items():
        rm = vp['risk_mode']
        if rm == 'adaptive':
            rs = f"adaptive(red>{vp['red_threshold']}%: SL{vp['sl_strong']}+TP{vp['tp_strong']} / SL{vp['sl_weak']}+Tr{vp['trail_weak']})"
        elif rm == 'trailing':
            rs = f"SL{vp['sl_pct']}%+Tr{vp['trail_pct']}%"
        else:
            rs = f"SL{vp['sl_pct']}%+TP{vp['tp_pct']}%"
        cr_s = f"cr>{vp['cr_min']}%" if vp['cr_min'] else '-'
        print(f"  {vn}: turn>{vp['turn_min']}% {cr_s} offset={vp['buy_offset']*100}% hold=T+{vp['hold_max_days']} {rs}")

    # 6版本x6年
    print(f'\n{"="*90}')
    print('=== 6版本 x 6年 仓位模拟结果 (初始100万, N=3) ===')
    print(f'{"版本":<4s} | {"年份":<5s} | {"交易数":>5s} | {"胜率":>6s} | {"均收益":>8s} | {"年末净值":>10s} | {"年化":>8s}')
    print('-' * 68)

    best_ver, best_cagr = None, -999

    for vn in VERSIONS:
        info = all_results[vn]
        trades = info['trades']
        yn = info['yearly_nav']
        fc = info['final_capital']

        prev_nav = INITIAL_CAPITAL
        for year in sorted(yn.keys()):
            yt = [t for t in trades if t['sell_date'][:4] == year]
            nt = len(yt)
            wins = sum(1 for t in yt if t['pnl_pct'] > 0)
            wr = wins / nt * 100 if nt > 0 else 0
            avg_r = statistics.mean([t['pnl_pct'] for t in yt]) if yt else 0
            nav = yn[year]
            yr = (nav - prev_nav) / prev_nav * 100 if prev_nav > 0 else 0
            print(f'{vn:<4s} | {year:<5s} | {nt:>5d} | {wr:>5.1f}% | {avg_r:>+7.2f}% | {nav/10000:>8.1f}万 | {yr:>+7.1f}%')
            prev_nav = nav
        cagr = (fc / INITIAL_CAPITAL) ** (1.0 / 6.0) - 1 if fc > 0 else -1
        if cagr > best_cagr:
            best_cagr = cagr
            best_ver = vn
        print()

    # 复合年化汇总
    print(f'{"="*90}')
    print('=== 各版本6年复合年化 ===')
    print(f'{"版本":<4s} | {"6年最终净值":>12s} | {"6年CAGR":>10s} | {"最差年":>16s} | {"最佳年":>16s} | {"每年>0?":<6s}')
    print('-' * 82)

    for vn in VERSIONS:
        info = all_results[vn]
        yn = info['yearly_nav']
        fc = info['final_capital']
        cagr = (fc / INITIAL_CAPITAL) ** (1.0 / 6.0) - 1 if fc > 0 else -1

        prev_nav = INITIAL_CAPITAL
        yr_map = {}
        all_pos = True
        for y in sorted(yn.keys()):
            r = (yn[y] - prev_nav) / prev_nav * 100 if prev_nav > 0 else 0
            yr_map[y] = r
            if r <= 0:
                all_pos = False
            prev_nav = yn[y]

        if yr_map:
            worst = min(yr_map, key=yr_map.get)
            best = max(yr_map, key=yr_map.get)
            ws = f'{worst}({yr_map[worst]:+.0f}%)'
            bs = f'{best}({yr_map[best]:+.0f}%)'
        else:
            ws = bs = 'N/A'

        print(f'{vn:<4s} | {fc/10000:>10.1f}万 | {cagr*100:>+8.1f}% | {ws:>16s} | {bs:>16s} | {"Y" if all_pos else "N":<6s}')

    # 最优版本逐月明细
    if best_ver:
        print(f'\n{"="*90}')
        print(f'=== 最优版本 {best_ver} 逐月明细 ===')
        trades = all_results[best_ver]['trades']
        monthly = defaultdict(list)
        for t in trades:
            monthly[t['sell_date'][:7]].append(t['pnl_pct'])

        print(f'{"年月":<8s} | {"交易数":>5s} | {"胜率":>6s} | {"均收益":>8s} | {"月总PnL":>8s}')
        print('-' * 50)
        for ym in sorted(monthly.keys()):
            rs = monthly[ym]
            n = len(rs)
            w = sum(1 for r in rs if r > 0)
            wr = w / n * 100 if n > 0 else 0
            avg = statistics.mean(rs) if rs else 0
            total = sum(rs)
            print(f'{ym:<8s} | {n:>5d} | {wr:>5.1f}% | {avg:>+7.2f}% | {total:>+7.2f}%')

        # 逐笔(前50)
        print(f'\n{"="*90}')
        print(f'=== 最优版本 {best_ver} 逐笔交易(前50笔) ===')
        print(f'{"买入日":<11s}| {"卖出日":<11s}| {"代码":<10s}| {"名称":<8s}| {"买入价":>7s}| {"卖出价":>7s}| {"收益%":>7s}| {"原因":<12s}| {"风控":<12s}')
        print('-' * 100)
        for t in trades[:50]:
            nm = (t['name'] or '')[:6]
            print(f'{t["buy_date"]:<11s}| {t["sell_date"]:<11s}| {t["code"]:<10s}| '
                  f'{nm:<8s}| {t["buy_price"]:>7.2f}| {t["sell_price"]:>7.2f}| '
                  f'{t["pnl_pct"]:>+6.2f}%| {t["reason"]:<12s}| {t["risk_mode"]:<12s}')

    print(f'\n{"="*90}')
    if best_ver:
        fc = all_results[best_ver]['final_capital']
        print(f'>>> 最优版本: {best_ver}  6年CAGR: {best_cagr*100:+.1f}%  最终净值: {fc/10000:.1f}万 (初始100万)')
    print('=' * 90)


def main():
    print('=' * 90, flush=True)
    print('ZhaBan限价单 Phase-4 向500%冲刺', flush=True)
    print(f'初始资金: {INITIAL_CAPITAL/10000:.0f}万  仓位: N={N_SLOTS}  区间: {START_DATE} ~ {END_DATE}', flush=True)
    print('=' * 90, flush=True)

    conn = sqlite3.connect(DB_PATH)
    all_dates, date_idx, by_code, red_ratio = load_data_efficient(conn)
    conn.close()

    all_results = {}
    for vn, vp in VERSIONS.items():
        print(f'\n>>> 运行版本 {vn} ...', flush=True)
        trades, yearly_nav, final_capital = run_simulation(by_code, all_dates, date_idx, red_ratio, vn, vp)
        all_results[vn] = {'trades': trades, 'yearly_nav': yearly_nav, 'final_capital': final_capital}
        cagr = (final_capital / INITIAL_CAPITAL) ** (1.0 / 6.0) - 1 if final_capital > 0 else -1
        print(f'    {vn}: 交易={len(trades)}  最终={final_capital/10000:.1f}万  CAGR={cagr*100:+.1f}%', flush=True)

    print_all_results(all_results)


if __name__ == '__main__':
    main()
