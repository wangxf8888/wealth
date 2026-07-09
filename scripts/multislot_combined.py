#!/usr/bin/env python3
"""V字+Surge7 双策略组合多仓回测
两种信号源共享同一个Portfolio，实现信号互补、分散风险。

V字信号: 创业板, 前4天累跌-3%, 高开4-20%, 换手率>=8%
Surge7信号: 昨日冲高11%回落, 换手率>=5%, 大盘过滤

测试矩阵: N_SLOTS = [5, 7, 10, 15]
回测区间: 2021-01-01 ~ 2026-06-30
"""
import sys, sqlite3, math, time
from datetime import datetime, timedelta
from collections import defaultdict

sys.path.insert(0, '/home/AIWealth/scripts')
from compliance_utils import (
    safe_float, is_one_word_board, is_limit_up, is_limit_down,
    is_st, get_limit_threshold, is_valid_price, check_t1
)

# ============================================================
# 策略参数
# ============================================================
VSHAPE_CONFIG = {
    'v_lookback_days': 4,
    'v_cum_drop': -3.0,
    'v_min_down_days': 2,
    'gap_up_min': 4.0,
    'gap_up_max': 20.0,
    'tp_pct': 10.0,
    'sl_pct': -2.0,
    'max_hold_days': 3,
    'board_filter': 'sz.300',
    'min_turn': 8.0,
}

SURGE7_CONFIG = {
    'surge_threshold': 11.0,
    'fallback_ratio': 0.6,
    'tp_pct': 8.0,
    'sl_pct': -3.0,
    'max_hold_days': 3,
    'buy_hour': 1,
    'min_turn': 5.0,
    'market_filter': True,
}

DB_PATH = '/home/AIWealth/data/stocks.db'
INITIAL_CAPITAL = 1_000_000.0
START_DATE = '2021-01-01'
END_DATE = '2026-06-30'
N_SLOTS_LIST = [5, 7, 10, 15]


def sf(val):
    if val is None:
        return 0.0
    try:
        f = float(val)
        return 0.0 if (math.isnan(f) or math.isinf(f)) else f
    except:
        return 0.0


def load_all_data():
    """加载全量数据"""
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    lookback_start = (datetime.strptime(START_DATE, "%Y-%m-%d") - timedelta(days=60)).strftime("%Y-%m-%d")
    cur.execute("SELECT DISTINCT date FROM stock_kline WHERE date>=? AND date<=? ORDER BY date",
                (lookback_start, END_DATE))
    all_dates = [r[0] for r in cur.fetchall()]
    date_to_idx = {d: i for i, d in enumerate(all_dates)}
    bt_dates = [d for d in all_dates if START_DATE <= d <= END_DATE]
    print(f"  总交易日: {len(all_dates)} | 回测日: {len(bt_dates)}")

    fields = ('date, code, code_name, preclose, open, close, '
              'open_rate, close_rate, high, low, turn, isST, '
              'hour1_open, hour1_high, hour1_low, hour1_close, '
              'hour2_open, hour2_high, hour2_low, hour2_close, '
              'hour3_open, hour3_high, hour3_low, hour3_close, '
              'hour4_open, hour4_high, hour4_low, hour4_close, '
              'high_rate')
    day_data = {}
    total_rows = 0
    years = sorted(set(d[:4] for d in all_dates))
    for year in years:
        y_start = max(lookback_start, f"{year}-01-01")
        y_end = min(END_DATE, f"{year}-12-31")
        if y_start > y_end:
            continue
        cur.execute(f"SELECT {fields} FROM stock_kline WHERE date>=? AND date<=?", (y_start, y_end))
        cnt = 0
        for r in cur:
            dt, code = r[0], r[1]
            di = date_to_idx.get(dt)
            if di is None:
                continue
            row = {
                'date': dt, 'code': code, 'code_name': r[2],
                'preclose': sf(r[3]), 'open': sf(r[4]), 'close': sf(r[5]),
                'open_rate': sf(r[6]) if r[6] is not None else None,
                'close_rate': sf(r[7]) if r[7] is not None else None,
                'high': sf(r[8]), 'low': sf(r[9]),
                'turn': sf(r[10]), 'isST': r[11],
                'h1_open': sf(r[12]), 'h1_high': sf(r[13]),
                'h1_low': sf(r[14]), 'h1_close': sf(r[15]),
                'h2_open': sf(r[16]), 'h2_high': sf(r[17]),
                'h2_low': sf(r[18]), 'h2_close': sf(r[19]),
                'h3_open': sf(r[20]), 'h3_high': sf(r[21]),
                'h3_low': sf(r[22]), 'h3_close': sf(r[23]),
                'h4_open': sf(r[24]), 'h4_high': sf(r[25]),
                'h4_low': sf(r[26]), 'h4_close': sf(r[27]),
                'high_rate': sf(r[28]) if r[28] is not None else None,
            }
            if di not in day_data:
                day_data[di] = {}
            day_data[di][code] = row
            cnt += 1
        total_rows += cnt
        print(f"    {year}: {cnt:,} 行")
    sys.stdout.flush()

    market_data = {}
    cur.execute("SELECT date, close_rate FROM index_kline WHERE code='sh.000001' AND date>=? AND date<=?",
                (lookback_start, END_DATE))
    for row in cur:
        market_data[row[0]] = sf(row[1])
    conn.close()
    print(f"  数据加载完成: {total_rows:,}行")
    sys.stdout.flush()
    return all_dates, date_to_idx, bt_dates, day_data, market_data


def build_vshape_lookback(all_dates, date_to_idx, day_data):
    """构建V字策略的lookback缓存"""
    print("  构建V字lookback缓存...", flush=True)
    lb_days = VSHAPE_CONFIG['v_lookback_days']
    board_prefix = VSHAPE_CONFIG['board_filter']
    cr_history = {}
    for di, stocks in day_data.items():
        for code, row in stocks.items():
            if not code.startswith(board_prefix):
                continue
            cr = row.get('close_rate')
            if cr is not None:
                cr_history.setdefault(code, []).append((di, cr))
    for code in cr_history:
        cr_history[code].sort()
    lookback_cache = {}
    for code, hist in cr_history.items():
        for pos in range(lb_days, len(hist)):
            target_di = hist[pos][0]
            window = hist[pos - lb_days: pos]
            cum = sum(x[1] for x in window)
            dd = sum(1 for x in window if x[1] < 0)
            lookback_cache[(target_di, code)] = (cum, dd)
    print(f"    缓存 {len(lookback_cache):,} 条lookback记录")
    return lookback_cache


def build_surge7_signals(all_dates, date_to_idx, day_data):
    """构建Surge7策略的信号日缓存"""
    print("  构建Surge7信号缓存...", flush=True)
    signal_cands = {}
    for di, stocks in day_data.items():
        cands = []
        for code, row in stocks.items():
            if code.startswith('bj.'):
                continue
            if row.get('isST') == 1:
                continue
            cname = row.get('code_name', '') or ''
            if 'ST' in cname.upper():
                continue
            high_rate = row.get('high_rate')
            if high_rate is None or high_rate < 5.0:
                continue
            close_rate = row.get('close_rate')
            if close_rate is None:
                continue
            preclose = row['preclose']
            close_p = row['close']
            high_p = row['high']
            low_p = row['low']
            open_p = row['open']
            if preclose <= 0:
                continue
            if close_p > 0 and is_limit_up(code, close_p, preclose):
                continue
            if is_one_word_board(open_p, high_p, low_p, close_p):
                continue
            cands.append((code, high_rate, close_rate, row['turn']))
        if cands:
            cands.sort(key=lambda x: -x[1])
            signal_cands[di] = cands
    print(f"    信号日: {len(signal_cands)} 天")
    return signal_cands


def get_vshape_signals(today_idx, day_data, lookback_cache, held_codes):
    """获取当日V字策略信号"""
    cfg = VSHAPE_CONFIG
    stocks = day_data.get(today_idx, {})
    signals = []
    for code, row in stocks.items():
        if not code.startswith(cfg['board_filter']):
            continue
        if code in held_codes:
            continue
        if is_st(row.get('code_name', ''), row.get('isST', 0)):
            continue
        open_rate = row.get('open_rate')
        if open_rate is None:
            continue
        if open_rate < cfg['gap_up_min']:
            continue
        gap_no_max = cfg['gap_up_max'] >= 20.0
        if not gap_no_max and open_rate > cfg['gap_up_max']:
            continue
        preclose = row['preclose']
        if preclose <= 0 or row['open'] <= 0:
            continue
        if is_one_word_board(row['open'], row['high'], row['low'], row['close']):
            continue
        h1o = row['h1_open']
        if h1o <= 0:
            continue
        if is_one_word_board(h1o, row['h1_high'], row['h1_low'], row['h1_close']):
            continue
        if is_limit_up(code, h1o, preclose):
            continue
        if row['turn'] < cfg['min_turn']:
            continue
        lb_val = lookback_cache.get((today_idx, code))
        if lb_val is None:
            continue
        cum_drop, down_days = lb_val
        if cum_drop <= cfg['v_cum_drop'] and down_days >= cfg['v_min_down_days']:
            signals.append({
                'code': code, 'code_name': row.get('code_name', ''),
                'buy_price': h1o, 'strategy': 'vshape',
                'score': -cum_drop,
            })
    signals.sort(key=lambda x: -x['score'])
    return signals


def get_surge7_signals(today_idx, day_data, surge_signal_cands, market_data, all_dates, held_codes):
    """获取当日Surge7策略信号"""
    cfg = SURGE7_CONFIG
    if cfg['market_filter']:
        today_date = all_dates[today_idx] if today_idx < len(all_dates) else ''
        mkt_rate = market_data.get(today_date, 0.0)
        if mkt_rate < -1.0:
            return []
    sig_di = today_idx - 1
    if sig_di < 0:
        return []
    cands = surge_signal_cands.get(sig_di)
    if not cands:
        return []
    stocks_today = day_data.get(today_idx, {})
    signals = []
    for code, hr, cr, turn in cands:
        if code in held_codes:
            continue
        if hr < cfg['surge_threshold']:
            continue
        if cr >= hr * cfg['fallback_ratio']:
            continue
        if cfg['min_turn'] > 0 and turn < cfg['min_turn']:
            continue
        buy_row = stocks_today.get(code)
        if not buy_row:
            continue
        if buy_row.get('isST') == 1:
            continue
        cn2 = buy_row.get('code_name', '') or ''
        if 'ST' in cn2.upper():
            continue
        h1o = buy_row['h1_open']
        h1h = buy_row['h1_high']
        h1l = buy_row['h1_low']
        h1c = buy_row['h1_close']
        if h1o <= 0:
            continue
        if is_one_word_board(h1o, h1h, h1l, h1c):
            continue
        preclose_b = buy_row['preclose']
        if preclose_b > 0 and is_limit_up(code, h1o, preclose_b):
            continue
        signals.append({
            'code': code, 'code_name': cn2,
            'buy_price': h1o, 'strategy': 'surge7',
            'score': hr,
        })
    signals.sort(key=lambda x: -x['score'])
    return signals


def run_combined_backtest(n_slots, all_dates, date_to_idx, bt_dates, day_data,
                          lookback_cache, surge_signal_cands, market_data):
    """运行组合策略回测"""
    cash = INITIAL_CAPITAL
    positions = []
    trades = []
    equity_curve = []
    peak = INITIAL_CAPITAL
    max_dd = 0.0

    for today in bt_dates:
        today_idx = date_to_idx[today]
        stocks_today = day_data.get(today_idx, {})
        if not stocks_today:
            eq = cash + sum(p['shares'] * p['last_price'] for p in positions)
            equity_curve.append((today, eq))
            continue

        for pos in positions:
            if pos['buy_date'] != today:
                pos['hold_days'] += 1

        # 逐hour卖出
        for hour in range(1, 5):
            survived = []
            for pos in positions:
                if pos['buy_date'] == today:
                    survived.append(pos)
                    continue
                row = stocks_today.get(pos['code'])
                if not row:
                    survived.append(pos)
                    continue
                hc = row.get(f'h{hour}_close', 0.0)
                if hc <= 0:
                    survived.append(pos)
                    continue
                preclose_p = row['preclose']
                if preclose_p > 0 and is_limit_down(pos['code'], hc, preclose_p):
                    ho = row.get(f'h{hour}_open', 0.0)
                    hh = row.get(f'h{hour}_high', 0.0)
                    hl = row.get(f'h{hour}_low', 0.0)
                    if is_one_word_board(ho, hh, hl, hc):
                        pos['last_price'] = hc
                        survived.append(pos)
                        continue
                pnl_pct = (hc - pos['buy_price']) / pos['buy_price'] * 100.0
                if pos['strategy'] == 'vshape':
                    tp = VSHAPE_CONFIG['tp_pct']
                    sl = VSHAPE_CONFIG['sl_pct']
                    max_hold = VSHAPE_CONFIG['max_hold_days']
                else:
                    tp = SURGE7_CONFIG['tp_pct']
                    sl = SURGE7_CONFIG['sl_pct']
                    max_hold = SURGE7_CONFIG['max_hold_days']
                sell = False
                sell_reason = ''
                if pnl_pct >= tp:
                    sell = True
                    sell_reason = f'TP h{hour}({pnl_pct:+.1f}%>={tp}%)'
                elif pnl_pct <= sl:
                    sell = True
                    sell_reason = f'SL h{hour}({pnl_pct:+.1f}%<={sl}%)'
                elif pos['hold_days'] >= max_hold and hour == 4:
                    sell = True
                    sell_reason = f'到期h4({pos["hold_days"]}d>={max_hold})'
                if sell:
                    cash += pos['shares'] * hc
                    trades.append({
                        'code': pos['code'], 'code_name': pos['code_name'],
                        'buy_date': pos['buy_date'], 'buy_price': pos['buy_price'],
                        'buy_hour': 1, 'sell_date': today, 'sell_price': hc,
                        'sell_hour': hour, 'sell_reason': sell_reason,
                        'pnl_pct': pnl_pct, 'hold_days': pos['hold_days'],
                        'shares': pos['shares'], 'strategy': pos['strategy'],
                    })
                else:
                    pos['last_price'] = hc
                    survived.append(pos)
            positions = survived

        # 买入(hour1)
        free_slots = n_slots - len(positions)
        if free_slots > 0:
            held_codes = {p['code'] for p in positions}
            vshape_sigs = get_vshape_signals(today_idx, day_data, lookback_cache, held_codes)
            surge7_sigs = get_surge7_signals(today_idx, day_data, surge_signal_cands,
                                            market_data, all_dates, held_codes)
            all_signals = []
            seen_codes = set(held_codes)
            for s in vshape_sigs:
                if s['code'] not in seen_codes:
                    seen_codes.add(s['code'])
                    all_signals.append(s)
            for s in surge7_sigs:
                if s['code'] not in seen_codes:
                    seen_codes.add(s['code'])
                    all_signals.append(s)
            bought = 0
            for sig in all_signals:
                if bought >= free_slots:
                    break
                slot_cap = cash / max(1, free_slots - bought)
                shares = int(slot_cap / sig['buy_price'] // 100) * 100
                if shares <= 0:
                    continue
                cost = shares * sig['buy_price']
                if cost > cash:
                    continue
                cash -= cost
                positions.append({
                    'code': sig['code'], 'code_name': sig['code_name'],
                    'buy_price': sig['buy_price'], 'buy_date': today,
                    'buy_date_idx': today_idx, 'hold_days': 0,
                    'shares': shares, 'strategy': sig['strategy'],
                    'last_price': sig['buy_price'],
                })
                bought += 1

        # 日末权益
        equity = cash
        for pos in positions:
            row = stocks_today.get(pos['code'])
            if row and row['h4_close'] > 0:
                pos['last_price'] = row['h4_close']
                equity += pos['shares'] * row['h4_close']
            elif row and row['close'] > 0:
                pos['last_price'] = row['close']
                equity += pos['shares'] * row['close']
            else:
                equity += pos['shares'] * pos['last_price']
        equity_curve.append((today, equity))
        if equity > peak:
            peak = equity
        dd = (peak - equity) / peak * 100 if peak > 0 else 0
        if dd > max_dd:
            max_dd = dd

    # 清仓
    if positions:
        last_date = bt_dates[-1]
        last_idx = date_to_idx[last_date]
        last_stocks = day_data.get(last_idx, {})
        for pos in positions:
            row = last_stocks.get(pos['code'])
            sp = pos['last_price']
            if row and row['h4_close'] > 0:
                sp = row['h4_close']
            pnl_pct = (sp - pos['buy_price']) / pos['buy_price'] * 100.0
            cash += pos['shares'] * sp
            trades.append({
                'code': pos['code'], 'code_name': pos['code_name'],
                'buy_date': pos['buy_date'], 'buy_price': pos['buy_price'],
                'buy_hour': 1, 'sell_date': last_date, 'sell_price': sp,
                'sell_hour': 4, 'sell_reason': '回测结束清仓',
                'pnl_pct': pnl_pct, 'hold_days': pos['hold_days'],
                'shares': pos['shares'], 'strategy': pos['strategy'],
            })
    final_equity = cash
    return trades, equity_curve, final_equity, max_dd


def run_single_backtest(strategy_name, n_slots, all_dates, date_to_idx, bt_dates,
                        day_data, lookback_cache, surge_signal_cands, market_data):
    """运行单策略回测（用于对比）"""
    cash = INITIAL_CAPITAL
    positions = []
    trades = []
    equity_curve = []
    peak = INITIAL_CAPITAL
    max_dd = 0.0

    for today in bt_dates:
        today_idx = date_to_idx[today]
        stocks_today = day_data.get(today_idx, {})
        if not stocks_today:
            eq = cash + sum(p['shares'] * p['last_price'] for p in positions)
            equity_curve.append((today, eq))
            continue
        for pos in positions:
            if pos['buy_date'] != today:
                pos['hold_days'] += 1
        # 卖出
        for hour in range(1, 5):
            survived = []
            for pos in positions:
                if pos['buy_date'] == today:
                    survived.append(pos)
                    continue
                row = stocks_today.get(pos['code'])
                if not row:
                    survived.append(pos)
                    continue
                hc = row.get(f'h{hour}_close', 0.0)
                if hc <= 0:
                    survived.append(pos)
                    continue
                preclose_p = row['preclose']
                if preclose_p > 0 and is_limit_down(pos['code'], hc, preclose_p):
                    ho = row.get(f'h{hour}_open', 0.0)
                    hh = row.get(f'h{hour}_high', 0.0)
                    hl = row.get(f'h{hour}_low', 0.0)
                    if is_one_word_board(ho, hh, hl, hc):
                        pos['last_price'] = hc
                        survived.append(pos)
                        continue
                pnl_pct = (hc - pos['buy_price']) / pos['buy_price'] * 100.0
                if strategy_name == 'vshape':
                    tp = VSHAPE_CONFIG['tp_pct']
                    sl = VSHAPE_CONFIG['sl_pct']
                    max_hold = VSHAPE_CONFIG['max_hold_days']
                else:
                    tp = SURGE7_CONFIG['tp_pct']
                    sl = SURGE7_CONFIG['sl_pct']
                    max_hold = SURGE7_CONFIG['max_hold_days']
                sell = False
                sell_reason = ''
                if pnl_pct >= tp:
                    sell = True
                    sell_reason = f'TP h{hour}({pnl_pct:+.1f}%>={tp}%)'
                elif pnl_pct <= sl:
                    sell = True
                    sell_reason = f'SL h{hour}({pnl_pct:+.1f}%<={sl}%)'
                elif pos['hold_days'] >= max_hold and hour == 4:
                    sell = True
                    sell_reason = f'到期h4({pos["hold_days"]}d>={max_hold})'
                if sell:
                    cash += pos['shares'] * hc
                    trades.append({
                        'code': pos['code'], 'code_name': pos['code_name'],
                        'buy_date': pos['buy_date'], 'buy_price': pos['buy_price'],
                        'buy_hour': 1, 'sell_date': today, 'sell_price': hc,
                        'sell_hour': hour, 'sell_reason': sell_reason,
                        'pnl_pct': pnl_pct, 'hold_days': pos['hold_days'],
                        'shares': pos['shares'], 'strategy': strategy_name,
                    })
                else:
                    pos['last_price'] = hc
                    survived.append(pos)
            positions = survived
        # 买入
        free_slots = n_slots - len(positions)
        if free_slots > 0:
            held_codes = {p['code'] for p in positions}
            if strategy_name == 'vshape':
                signals = get_vshape_signals(today_idx, day_data, lookback_cache, held_codes)
            else:
                signals = get_surge7_signals(today_idx, day_data, surge_signal_cands,
                                            market_data, all_dates, held_codes)
            bought = 0
            for sig in signals:
                if bought >= free_slots:
                    break
                slot_cap = cash / max(1, free_slots - bought)
                shares = int(slot_cap / sig['buy_price'] // 100) * 100
                if shares <= 0:
                    continue
                cost = shares * sig['buy_price']
                if cost > cash:
                    continue
                cash -= cost
                positions.append({
                    'code': sig['code'], 'code_name': sig['code_name'],
                    'buy_price': sig['buy_price'], 'buy_date': today,
                    'buy_date_idx': today_idx, 'hold_days': 0,
                    'shares': shares, 'strategy': strategy_name,
                    'last_price': sig['buy_price'],
                })
                bought += 1
        # 日末权益
        equity = cash
        for pos in positions:
            row = stocks_today.get(pos['code'])
            if row and row['h4_close'] > 0:
                pos['last_price'] = row['h4_close']
                equity += pos['shares'] * row['h4_close']
            elif row and row['close'] > 0:
                pos['last_price'] = row['close']
                equity += pos['shares'] * row['close']
            else:
                equity += pos['shares'] * pos['last_price']
        equity_curve.append((today, equity))
        if equity > peak:
            peak = equity
        dd = (peak - equity) / peak * 100 if peak > 0 else 0
        if dd > max_dd:
            max_dd = dd
    # 清仓
    if positions:
        last_date = bt_dates[-1]
        last_idx = date_to_idx[last_date]
        last_stocks = day_data.get(last_idx, {})
        for pos in positions:
            row = last_stocks.get(pos['code'])
            sp = pos['last_price']
            if row and row['h4_close'] > 0:
                sp = row['h4_close']
            pnl_pct = (sp - pos['buy_price']) / pos['buy_price'] * 100.0
            cash += pos['shares'] * sp
            trades.append({
                'code': pos['code'], 'code_name': pos['code_name'],
                'buy_date': pos['buy_date'], 'buy_price': pos['buy_price'],
                'buy_hour': 1, 'sell_date': last_date, 'sell_price': sp,
                'sell_hour': 4, 'sell_reason': '回测结束清仓',
                'pnl_pct': pnl_pct, 'hold_days': pos['hold_days'],
                'shares': pos['shares'], 'strategy': strategy_name,
            })
    final_equity = cash
    return trades, equity_curve, final_equity, max_dd


def compute_metrics(trades, equity_curve, final_equity, max_dd):
    """计算核心指标"""
    n_trades = len(trades)
    if n_trades == 0:
        return {'cagr': 0, 'win_rate': 0, 'mdd': 0, 'calmar': 0,
                'trades': 0, 'final_equity': final_equity, 'yearly': {}}
    wins = sum(1 for t in trades if t['pnl_pct'] > 0)
    win_rate = wins / n_trades * 100
    if len(equity_curve) >= 2:
        d0 = datetime.strptime(equity_curve[0][0], "%Y-%m-%d")
        d1 = datetime.strptime(equity_curve[-1][0], "%Y-%m-%d")
        years = max(0.1, (d1 - d0).days / 365.25)
    else:
        years = 1.0
    ratio = final_equity / INITIAL_CAPITAL
    cagr = (ratio ** (1.0 / years) - 1.0) * 100.0 if ratio > 0 else -100.0
    calmar = cagr / max_dd if max_dd > 0 else 999.0
    year_equity = defaultdict(list)
    for d, eq in equity_curve:
        year_equity[d[:4]].append(eq)
    yearly = {}
    for yr in sorted(year_equity.keys()):
        eqs = year_equity[yr]
        if len(eqs) >= 2:
            yr_ret = (eqs[-1] / eqs[0] - 1.0) * 100.0
        else:
            yr_ret = 0.0
        yearly[yr] = yr_ret
    return {
        'cagr': cagr, 'win_rate': win_rate, 'mdd': max_dd,
        'calmar': calmar, 'trades': n_trades,
        'final_equity': final_equity, 'yearly': yearly,
    }


def main():
    t0 = time.time()
    print("=" * 70)
    print("  V字+Surge7 双策略组合多仓回测")
    print(f"  时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print("=" * 70)
    print(f"  回测区间: {START_DATE} ~ {END_DATE}")
    print(f"  初始资金: {INITIAL_CAPITAL:,.0f}")
    print(f"  仓位测试: {N_SLOTS_LIST}")
    print(f"  V字参数: TP={VSHAPE_CONFIG['tp_pct']}% SL={VSHAPE_CONFIG['sl_pct']}% Hold={VSHAPE_CONFIG['max_hold_days']}d")
    print(f"  Surge7参数: TP={SURGE7_CONFIG['tp_pct']}% SL={SURGE7_CONFIG['sl_pct']}% Hold={SURGE7_CONFIG['max_hold_days']}d")
    print("=" * 70, flush=True)

    print("\n[1] 加载数据...")
    sys.stdout.flush()
    all_dates, date_to_idx, bt_dates, day_data, market_data = load_all_data()

    print("\n[2] 构建信号缓存...")
    sys.stdout.flush()
    lookback_cache = build_vshape_lookback(all_dates, date_to_idx, day_data)
    surge_signal_cands = build_surge7_signals(all_dates, date_to_idx, day_data)

    print("\n[3] 执行组合回测...")
    sys.stdout.flush()
    combo_results = {}
    for n in N_SLOTS_LIST:
        print(f"  N={n}仓...", end='', flush=True)
        trades, eq_curve, final_eq, mdd = run_combined_backtest(
            n, all_dates, date_to_idx, bt_dates, day_data,
            lookback_cache, surge_signal_cands, market_data)
        metrics = compute_metrics(trades, eq_curve, final_eq, mdd)
        v_trades = [t for t in trades if t['strategy'] == 'vshape']
        s_trades = [t for t in trades if t['strategy'] == 'surge7']
        v_profit = sum(t['pnl_pct'] * t['shares'] * t['buy_price'] / 100 for t in v_trades)
        s_profit = sum(t['pnl_pct'] * t['shares'] * t['buy_price'] / 100 for t in s_trades)
        combo_results[n] = {
            'metrics': metrics, 'trades': trades,
            'v_count': len(v_trades), 's_count': len(s_trades),
            'v_profit': v_profit, 's_profit': s_profit,
        }
        print(f" CAGR={metrics['cagr']:+.1f}% MDD={metrics['mdd']:.1f}% Calmar={metrics['calmar']:.2f} "
              f"交易={metrics['trades']} (V:{len(v_trades)} S:{len(s_trades)})")
        sys.stdout.flush()

    print("\n[4] 单策略对比回测(N=5)...")
    sys.stdout.flush()
    single_results = {}
    for sname in ['vshape', 'surge7']:
        print(f"  单{sname} N=5...", end='', flush=True)
        trades, eq_curve, final_eq, mdd = run_single_backtest(
            sname, 5, all_dates, date_to_idx, bt_dates, day_data,
            lookback_cache, surge_signal_cands, market_data)
        metrics = compute_metrics(trades, eq_curve, final_eq, mdd)
        single_results[sname] = metrics
        print(f" CAGR={metrics['cagr']:+.1f}% MDD={metrics['mdd']:.1f}% Calmar={metrics['calmar']:.2f} 交易={metrics['trades']}")
        sys.stdout.flush()

    print("\n[5] 额外分析...")
    sys.stdout.flush()
    overlap_days = 0
    total_signal_days = 0
    for today in bt_dates:
        today_idx = date_to_idx[today]
        held = set()
        vs = get_vshape_signals(today_idx, day_data, lookback_cache, held)
        ss = get_surge7_signals(today_idx, day_data, surge_signal_cands, market_data, all_dates, held)
        if vs or ss:
            total_signal_days += 1
            v_codes = {s['code'] for s in vs}
            s_codes = {s['code'] for s in ss}
            if v_codes & s_codes:
                overlap_days += 1

    elapsed = time.time() - t0

    # ===== 输出结果 =====
    print("\n" + "=" * 70)
    print("  ===== V字+Surge7 双策略组合多仓回测 =====")
    print("=" * 70)
    print(f"  初始资金: {INITIAL_CAPITAL:,.0f}")
    print(f"  回测耗时: {elapsed:.1f}秒")

    for n in N_SLOTS_LIST:
        r = combo_results[n]
        m = r['metrics']
        print(f"\n  --- N={n}仓 ---")
        print(f"  最终净值: {m['final_equity']:,.0f}")
        print(f"  CAGR: {m['cagr']:+.1f}%  胜率: {m['win_rate']:.1f}%  MDD: -{m['mdd']:.1f}%  Calmar: {m['calmar']:.2f}")
        print(f"  V字交易: {r['v_count']}笔  Surge7交易: {r['s_count']}笔")
        yr_str = '  '.join(f"{yr}:{ret:+.0f}%" for yr, ret in sorted(m['yearly'].items()))
        print(f"  年度: {yr_str}")
        total_profit = r['v_profit'] + r['s_profit']
        if total_profit != 0:
            v_pct = r['v_profit'] / abs(total_profit) * 100 if total_profit > 0 else 0
            s_pct = r['s_profit'] / abs(total_profit) * 100 if total_profit > 0 else 0
            print(f"  盈利贡献: V字={v_pct:.1f}% Surge7={s_pct:.1f}%")

    print(f"\n{'='*70}")
    print("  ===== 对比 =====")
    print(f"{'='*70}")
    print(f"  {'模式':<12} | {'N':>3} | {'CAGR':>8} | {'MDD':>8} | {'Calmar':>7} | {'交易数':>6}")
    print(f"  {'-'*12}-+-{'-'*3}-+-{'-'*8}-+-{'-'*8}-+-{'-'*7}-+-{'-'*6}")
    sv = single_results['vshape']
    print(f"  {'单V字':<10} | {'5':>3} | {sv['cagr']:>+7.1f}% | {sv['mdd']:>7.1f}% | {sv['calmar']:>7.2f} | {sv['trades']:>6}")
    ss_r = single_results['surge7']
    print(f"  {'单Surge7':<10} | {'5':>3} | {ss_r['cagr']:>+7.1f}% | {ss_r['mdd']:>7.1f}% | {ss_r['calmar']:>7.2f} | {ss_r['trades']:>6}")
    for n in N_SLOTS_LIST:
        cm = combo_results[n]['metrics']
        print(f"  {'组合':<10} | {n:>3} | {cm['cagr']:>+7.1f}% | {cm['mdd']:>7.1f}% | {cm['calmar']:>7.2f} | {cm['trades']:>6}")

    print(f"\n{'='*70}")
    print("  ===== 额外分析 =====")
    print(f"{'='*70}")
    overlap_rate = overlap_days / max(1, total_signal_days) * 100
    print(f"  信号重叠率(同日同股): {overlap_rate:.1f}% ({overlap_days}/{total_signal_days}天)")

    best_single_calmar = max(sv['calmar'], ss_r['calmar'])
    combo_5_calmar = combo_results[5]['metrics']['calmar']
    if combo_5_calmar > best_single_calmar:
        print(f"  组合Calmar({combo_5_calmar:.2f}) > 单策略最优({best_single_calmar:.2f}) -> 组合有效!")
    else:
        print(f"  组合Calmar({combo_5_calmar:.2f}) <= 单策略最优({best_single_calmar:.2f}) -> 组合效果待优化")

    best_single_cagr = max(sv['cagr'], ss_r['cagr'])
    combo_5_cagr = combo_results[5]['metrics']['cagr']
    if combo_5_cagr > best_single_cagr:
        print(f"  组合CAGR({combo_5_cagr:+.1f}%) > 单策略最优({best_single_cagr:+.1f}%) -> CAGR提升!")
    else:
        print(f"  组合CAGR({combo_5_cagr:+.1f}%) vs 单策略最优({best_single_cagr:+.1f}%)")

    print(f"\n  完成时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print("=" * 70)
    sys.stdout.flush()


if __name__ == '__main__':
    main()
