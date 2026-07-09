#!/usr/bin/env python3
"""
多指标评分系统 - 大规模参数网格搜索优化器
两阶段搜索:
  阶段1: 训练集粗筛 (2023-01-01 ~ 2025-12-31)
    1a. 固定卖出/仓位参数, 搜指标组合+星数 -> Top20
    1b. 用Top20配置, 搜卖出参数 -> Top10
    1c. 用Top10配置, 搜仓位数 -> Top50
  阶段2: 全量验证 (2020-01-01 ~ 2026-06-30)
    对Top50跑全量, 输出Top10最终排行(按Calmar)
"""
import sys
import os
import sqlite3
import json
import math
import time
import itertools
import random
from collections import defaultdict
from datetime import datetime

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECT_ROOT = os.path.dirname(SCRIPT_DIR)
sys.path.insert(0, PROJECT_ROOT)

from engine.indicators import INDICATOR_REGISTRY, create_indicator
from engine.scoring_engine import ScoringEngine
from engine.buy_strategies import (
    BuyStrategyManager, ScoreBuyStrategy, VShapeBuyStrategy,
    Surge7BuyStrategy, VolBreakoutBuyStrategy, LimitUpPullbackBuyStrategy,
)
from engine.sell_strategies import (
    SellStrategyManager, FixedTPSL, TrailingStop, TimeLimitExit,
)
from engine.position_strategies import create_position_strategy

DB_PATH = os.path.join(PROJECT_ROOT, 'data', 'stocks.db')
RESULTS_OUTPUT = os.path.join(SCRIPT_DIR, 'optimize_scoring_results.json')
HISTORY_LOOKBACK = 20

INDICATOR_SEARCH_SPACE = {
    'turnover_increase': ['>1.5', '>2.0', '>2.5', '>3.0'],
    'above_ma5': ['>0', '>0.5'],
    'above_ma10': ['>0', '>0.5'],
    'market_cap': ['>3000000000', '>5000000000', '>8000000000'],
    'volume_ratio': ['>1.5', '>2.0', '>2.5'],
    'gap_up_rate': ['between(1,5)', 'between(2,8)', 'between(3,10)'],
    'rsi_14': ['between(30,70)', 'between(40,60)', 'between(20,80)'],
    'amplitude_rate': ['>3', '>5', '>7'],
    'cum_drop_n': ['<-3', '<-5', '<-8'],
    'momentum_score': ['>2', '>3', '>5'],
}
MIN_STARS_OPTIONS = [3, 4, 5, 6, 7, 8]
BUY_STRATEGY_COMBOS = [
    ['score'],
    ['score', 'vshape'],
    ['score', 'surge7'],
    ['score', 'vshape', 'surge7', 'vol_breakout', 'limitup_pullback'],
]
SELL_PARAM_GRID = {
    'tp_pct': [5, 8, 10, 15, 20],
    'sl_pct': [-2, -3, -5, -7],
    'max_hold_days': [3, 5, 7, 10],
    'trail_pct': [3, 5, 7],
}
SLOTS_OPTIONS = [3, 5, 7, 10]


def _safe_float(val, default=0.0):
    if val is None:
        return default
    try:
        f = float(val)
        if math.isnan(f) or math.isinf(f):
            return default
        return f
    except (ValueError, TypeError):
        return default


def _is_gem(code):
    if not code:
        return False
    c = code.replace('sz.', '').replace('sh.', '')
    return c.startswith('300') or c.startswith('301')


def _is_star(code):
    if not code:
        return False
    c = code.replace('sh.', '').replace('sz.', '')
    return c.startswith('688')


def _get_limit_up_ratio(code):
    return 0.2 if (_is_gem(code) or _is_star(code)) else 0.1


def _get_limit_down_ratio(code):
    return 0.2 if (_is_gem(code) or _is_star(code)) else 0.1


def _is_limit_up_cannot_buy(row):
    code = row.get('code', '')
    preclose = _safe_float(row.get('preclose'))
    if preclose <= 0:
        return True
    ratio = _get_limit_up_ratio(code)
    limit_up_price = round(preclose * (1 + ratio), 2)
    o = _safe_float(row.get('open'))
    h = _safe_float(row.get('high'))
    c = _safe_float(row.get('close'))
    if o > 0 and h > 0 and c > 0:
        if abs(o - h) < 0.01 and abs(o - c) < 0.01 and o >= limit_up_price * 0.998:
            return True
    return False


def _check_buy_compliance(buy_price, preclose, code):
    if buy_price <= 0 or preclose <= 0:
        return False
    ratio = _get_limit_up_ratio(code)
    limit_up = round(preclose * (1 + ratio), 2)
    return buy_price < limit_up * 0.998


def _is_limit_down_cannot_sell(sell_price, preclose, code):
    if sell_price <= 0 or preclose <= 0:
        return True
    ratio = _get_limit_down_ratio(code)
    limit_down = round(preclose * (1 - ratio), 2)
    return sell_price <= limit_down * 1.002


def _is_oneword_limit_down(row):
    code = row.get('code', '')
    preclose = _safe_float(row.get('preclose'))
    if preclose <= 0:
        return True
    ratio = _get_limit_down_ratio(code)
    limit_down = round(preclose * (1 - ratio), 2)
    o = _safe_float(row.get('open'))
    lo = _safe_float(row.get('low'))
    c = _safe_float(row.get('close'))
    if o > 0 and lo > 0 and c > 0:
        if abs(o - lo) < 0.01 and abs(o - c) < 0.01 and o <= limit_down * 1.002:
            return True
    return False


def _date_diff_days(d1, d2):
    try:
        dt1 = datetime.strptime(d1, '%Y-%m-%d')
        dt2 = datetime.strptime(d2, '%Y-%m-%d')
        return (dt2 - dt1).days
    except Exception:
        return 365


def _get_hour_data(row, hour):
    prefix = f'hour{hour}_'
    return {
        'open': _safe_float(row.get(f'{prefix}open')),
        'high': _safe_float(row.get(f'{prefix}high')),
        'low': _safe_float(row.get(f'{prefix}low')),
        'close': _safe_float(row.get(f'{prefix}close')),
    }


class DataStore:
    def __init__(self, db_path, start_date, end_date):
        self.start_date = start_date
        self.end_date = end_date
        print(f"[数据加载] 区间: {start_date} ~ {end_date}")
        t0 = time.time()
        conn = sqlite3.connect(db_path)
        cur = conn.execute(
            "SELECT DISTINCT date FROM stock_kline "
            "WHERE date >= ? AND date <= ? ORDER BY date",
            (start_date, end_date)
        )
        self.all_dates = [r[0] for r in cur.fetchall()]
        print(f"  交易日: {len(self.all_dates)}")
        self.year_data = {}
        self.year_dates = {}
        self.year_code_date_idx = {}
        years = sorted(set(d[:4] for d in self.all_dates))
        for year in years:
            ydates = [d for d in self.all_dates if d.startswith(year)]
            if not ydates:
                continue
            first_idx = self.all_dates.index(ydates[0])
            hist_start_idx = max(0, first_idx - HISTORY_LOOKBACK)
            hist_start_date = self.all_dates[hist_start_idx]
            cur = conn.execute(
                "SELECT * FROM stock_kline WHERE date >= ? AND date <= ? "
                "ORDER BY code, date", (hist_start_date, ydates[-1])
            )
            columns = [desc[0] for desc in cur.description]
            data_by_code = defaultdict(list)
            for row_tuple in cur.fetchall():
                row = dict(zip(columns, row_tuple))
                data_by_code[row['code']].append(row)
            code_date_idx = {}
            for code, rows in data_by_code.items():
                idx_map = {}
                for i, r in enumerate(rows):
                    idx_map[r.get('date', '')] = i
                code_date_idx[code] = idx_map
            self.year_data[year] = dict(data_by_code)
            self.year_dates[year] = ydates
            self.year_code_date_idx[year] = code_date_idx
            print(f"  [{year}] {len(data_by_code)} stocks")
        conn.close()
        print(f"[数据加载完成] 耗时 {time.time()-t0:.1f}s")


def run_backtest(data_store, params):
    n_slots = params.get('n_slots', 5)
    min_stars = params.get('min_stars', 5)
    initial_capital = 1_000_000
    indicator_configs = {}
    ind_params = params.get('indicators', {})
    for name, cfg in ind_params.items():
        if name not in INDICATOR_REGISTRY:
            continue
        entry = {'weight': cfg.get('weight', 1.0)}
        if 'star_condition' in cfg:
            entry['star_condition'] = cfg['star_condition']
        for k in ('lookback', 'min_cap', 'period'):
            if k in cfg:
                entry[k] = cfg[k]
        indicator_configs[name] = entry
    if not indicator_configs:
        return None
    scoring_engine = ScoringEngine(indicator_configs, min_stars, 'star')
    buy_names = params.get('buy_strategies', ['score', 'vshape'])
    buy_strats = []
    for sn in buy_names:
        if sn == 'score':
            buy_strats.append(ScoreBuyStrategy(min_stars=min_stars))
        elif sn == 'vshape':
            buy_strats.append(VShapeBuyStrategy())
        elif sn == 'surge7':
            buy_strats.append(Surge7BuyStrategy())
        elif sn == 'vol_breakout':
            buy_strats.append(VolBreakoutBuyStrategy())
        elif sn == 'limitup_pullback':
            buy_strats.append(LimitUpPullbackBuyStrategy())
    if not buy_strats:
        buy_strats = [ScoreBuyStrategy(min_stars=min_stars)]
    buy_manager = BuyStrategyManager(buy_strats)
    sp = params.get('sell_params', {})
    sell_manager = SellStrategyManager([
        FixedTPSL(tp_pct=sp.get('tp_pct', 10.0), sl_pct=sp.get('sl_pct', -3.0)),
        TrailingStop(trail_pct=sp.get('trail_pct', 5.0)),
        TimeLimitExit(max_hold_days=sp.get('max_hold_days', 5)),
    ])
    cash = float(initial_capital)
    positions = []
    trades = []
    nav_history = []
    peak_nav = float(initial_capital)
    t1_signals = {}
    start_date = params.get('start_date', '2023-01-01')
    end_date = params.get('end_date', '2025-12-31')
    active_dates = [d for d in data_store.all_dates if start_date <= d <= end_date]
    if not active_dates:
        return None
    years_in_range = sorted(set(d[:4] for d in active_dates))

    def _total_equity():
        return cash + sum(
            p['shares'] * p.get('current_price', p['buy_price']) for p in positions)

    for year in years_in_range:
        if year not in data_store.year_data:
            continue
        dbc = data_store.year_data[year]
        cdi = data_store.year_code_date_idx[year]
        ydates = [d for d in data_store.year_dates[year]
                  if start_date <= d <= end_date]
        for date in ydates:
            # === SELL ===
            for pos in list(positions):
                code = pos['code']
                if pos['hold_days'] == 0:
                    continue
                if code not in cdi:
                    continue
                idx_map = cdi[code]
                if date not in idx_map:
                    continue
                row = dbc[code][idx_map[date]]
                if _is_oneword_limit_down(row):
                    continue
                preclose = _safe_float(row.get('preclose'))
                sold = False
                for hour in range(1, 5):
                    hd = _get_hour_data(row, hour)
                    if hd['open'] <= 0:
                        continue
                    dd = {'volume': _safe_float(row.get('volume')),
                          'close_rate': _safe_float(row.get('close_rate')),
                          'prev_volume': 0}
                    idx = idx_map[date]
                    if idx > 0:
                        dd['prev_volume'] = _safe_float(
                            dbc[code][idx-1].get('volume'))
                    ok, sell_p, reason = sell_manager.check_position(
                        pos, hour, hd, dd)
                    if ok and sell_p > 0:
                        if _is_limit_down_cannot_sell(sell_p, preclose, code):
                            continue
                        cash += pos['shares'] * sell_p
                        pnl = round((sell_p/pos['buy_price']-1)*100, 2)
                        trades.append({'pnl_pct': pnl,
                                       'sell_date': date,
                                       'buy_date': pos['buy_date']})
                        positions.remove(pos)
                        sold = True
                        break
                if not sold:
                    for hour in range(1, 5):
                        hd = _get_hour_data(row, hour)
                        if hd['high'] > pos.get('max_price', 0):
                            pos['max_price'] = hd['high']
            # === BUY ===
            used_slots = len(positions)
            if used_slots >= n_slots:
                t1_signals.clear()
            else:
                all_sigs = []
                for code, sig in list(t1_signals.items()):
                    if any(p['code'] == code for p in positions):
                        continue
                    if code in cdi:
                        idx_map = cdi[code]
                        if date in idx_map:
                            row = dbc[code][idx_map[date]]
                            bp = _safe_float(row.get('hour1_open'))
                            pc = _safe_float(row.get('preclose'))
                            if (bp > 0 and pc > 0
                                    and _check_buy_compliance(bp, pc, code)
                                    and not _is_limit_up_cannot_buy(row)):
                                sig['buy_price'] = bp
                                sig['code'] = code
                                all_sigs.append(sig)
                t1_signals.clear()
                for code, rows in dbc.items():
                    if any(p['code'] == code for p in positions):
                        continue
                    if any(s.get('code') == code for s in all_sigs):
                        continue
                    idx_map = cdi.get(code)
                    if not idx_map or date not in idx_map:
                        continue
                    idx = idx_map[date]
                    row = rows[idx]
                    if row.get('isST') == 1 or row.get('isST') == '1':
                        continue
                    if _safe_float(row.get('turn')) < 1.0:
                        continue
                    if _is_limit_up_cannot_buy(row):
                        continue
                    hist_start = max(0, idx - HISTORY_LOOKBACK)
                    history = rows[hist_start:idx]
                    try:
                        sr = scoring_engine.score_stock(row, history)
                    except Exception:
                        continue
                    try:
                        sigs = buy_manager.scan_signals(row, history, sr)
                    except Exception:
                        continue
                    for sig in sigs:
                        if not sig.get('signal'):
                            continue
                        if sig.get('is_t1_signal'):
                            t1_signals[code] = {
                                'code': code,
                                'score': sr.get('total_score', 0),
                                'star_count': sr.get('star_count', 0),
                                'strategy_name': sig.get('strategy_name', ''),
                                'buy_hour': 'hour1'}
                            continue
                        bp = _safe_float(sig.get('buy_price'))
                        pc = _safe_float(row.get('preclose'))
                        if bp <= 0 or pc <= 0:
                            continue
                        if not _check_buy_compliance(bp, pc, code):
                            continue
                        all_sigs.append({
                            'code': code, 'buy_price': bp,
                            'score': sr.get('total_score', 0),
                            'star_count': sr.get('star_count', 0),
                            'strategy_name': sig.get('strategy_name', '')})
                if all_sigs:
                    all_sigs.sort(
                        key=lambda s: (s.get('star_count', 0),
                                       s.get('score', 0)), reverse=True)
                    eq = _total_equity()
                    slot_size = eq / n_slots if n_slots > 0 else eq
                    for sig in all_sigs:
                        if len(positions) >= n_slots:
                            break
                        code = sig['code']
                        if any(p['code'] == code for p in positions):
                            continue
                        bp = sig['buy_price']
                        if bp <= 0:
                            continue
                        shares = int(slot_size / bp / 100) * 100
                        if shares < 100:
                            continue
                        cost = shares * bp
                        if cost > cash:
                            shares = int(cash / bp / 100) * 100
                            if shares < 100:
                                continue
                            cost = shares * bp
                        cash -= cost
                        positions.append({
                            'code': code, 'buy_price': bp,
                            'shares': shares, 'buy_date': date,
                            'hold_days': 0, 'max_price': bp,
                            'current_price': bp})
            # === UPDATE DAILY ===
            for pos in positions:
                pos['hold_days'] += 1
                code = pos['code']
                if code in cdi:
                    idx_map = cdi[code]
                    if date in idx_map:
                        c = _safe_float(dbc[code][idx_map[date]].get('close'))
                        if c > 0:
                            pos['current_price'] = c
                            if c > pos.get('max_price', 0):
                                pos['max_price'] = c
            nav = _total_equity()
            if nav > peak_nav:
                peak_nav = nav
            dd_pct = (nav/peak_nav - 1)*100 if peak_nav > 0 else 0
            nav_history.append({'date': date, 'nav': nav, 'dd': dd_pct})

    if not nav_history:
        return None
    final_nav = nav_history[-1]['nav']
    days_diff = _date_diff_days(nav_history[0]['date'], nav_history[-1]['date'])
    years_elapsed = max(days_diff / 365.25, 0.01)
    cagr = ((final_nav / initial_capital) ** (1 / years_elapsed) - 1) * 100
    mdd = min(n['dd'] for n in nav_history)
    calmar = cagr / abs(mdd) if mdd != 0 else 0
    daily_returns = []
    for i in range(1, len(nav_history)):
        pv = nav_history[i-1]['nav']
        cv = nav_history[i]['nav']
        if pv > 0:
            daily_returns.append(cv / pv - 1)
    if daily_returns:
        avg_r = sum(daily_returns) / len(daily_returns)
        std_r = (sum((r-avg_r)**2 for r in daily_returns)/len(daily_returns))**0.5
        sharpe = (avg_r / std_r * (252**0.5)) if std_r > 0 else 0
    else:
        sharpe = 0
    total_trades = len(trades)
    wins = sum(1 for t in trades if t['pnl_pct'] > 0)
    win_rate = wins / total_trades * 100 if total_trades > 0 else 0
    yearly = {}
    ynm = defaultdict(list)
    for n in nav_history:
        ynm[n['date'][:4]].append(n)
    for yr, navs in ynm.items():
        ys = navs[0]['nav']
        ye = navs[-1]['nav']
        yearly[yr] = round((ye/ys-1)*100, 1) if ys > 0 else 0
    return {'cagr': round(cagr, 2), 'mdd': round(mdd, 2),
            'calmar': round(calmar, 2), 'sharpe': round(sharpe, 2),
            'trades': total_trades, 'win_rate': round(win_rate, 1),
            'yearly': yearly}


def generate_indicator_combos(n_samples=200):
    all_indicators = list(INDICATOR_SEARCH_SPACE.keys())
    base_weights = {
        'turnover_increase': 2.0, 'above_ma5': 1.5, 'above_ma10': 1.0,
        'market_cap': 1.0, 'volume_ratio': 1.5, 'gap_up_rate': 1.0,
        'rsi_14': 1.0, 'amplitude_rate': 0.5, 'cum_drop_n': 1.5,
        'momentum_score': 1.0}
    base_params = {
        'turnover_increase': {'lookback': 5}, 'above_ma5': {},
        'above_ma10': {}, 'market_cap': {'min_cap': 5000000000},
        'volume_ratio': {'lookback': 5}, 'gap_up_rate': {},
        'rsi_14': {'period': 14}, 'amplitude_rate': {},
        'cum_drop_n': {'lookback': 4}, 'momentum_score': {'lookback': 5}}
    combos = []
    default_ind = {}
    for name in all_indicators:
        entry = {'weight': base_weights[name],
                 'star_condition': INDICATOR_SEARCH_SPACE[name][0]}
        entry.update(base_params.get(name, {}))
        default_ind[name] = entry
    combos.append(default_ind)
    for _ in range(n_samples):
        n_ind = random.randint(6, 10)
        selected = random.sample(all_indicators, n_ind)
        indicators = {}
        for name in selected:
            cond = random.choice(INDICATOR_SEARCH_SPACE[name])
            entry = {'weight': base_weights.get(name, 1.0),
                     'star_condition': cond}
            entry.update(base_params.get(name, {}))
            indicators[name] = entry
        combos.append(indicators)
    return combos


def generate_sell_param_combos():
    keys = ['tp_pct', 'sl_pct', 'max_hold_days', 'trail_pct']
    values = [SELL_PARAM_GRID[k] for k in keys]
    return [dict(zip(keys, combo)) for combo in itertools.product(*values)]


def phase1a_indicator_search(data_store, n_samples=200):
    print("\n" + "=" * 70)
    print("阶段1a: 指标组合+星数搜索 (2024-2025, tp=10,sl=-3,hold=5,trail=5,N=5)")
    print("=" * 70)
    indicator_combos = generate_indicator_combos(n_samples)
    total_combos = []
    for ind_cfg in indicator_combos:
        n_ind = len(ind_cfg)
        valid_stars = [s for s in MIN_STARS_OPTIONS if s <= n_ind]
        if not valid_stars:
            valid_stars = [n_ind]
        for ms in valid_stars:
            for bc in BUY_STRATEGY_COMBOS:
                total_combos.append({
                    'indicators': ind_cfg, 'min_stars': ms,
                    'buy_strategies': bc,
                    'sell_params': {'tp_pct': 10, 'sl_pct': -3,
                                    'max_hold_days': 5, 'trail_pct': 5},
                    'n_slots': 5, 'start_date': '2024-01-01',
                    'end_date': '2025-12-31'})
    max_phase1 = 150
    if len(total_combos) > max_phase1:
        random.shuffle(total_combos[4:])
        total_combos = total_combos[:max_phase1]
    print(f"  总组合数: {len(total_combos)}")
    results = []
    t0 = time.time()
    last_print = t0
    for i, params in enumerate(total_combos):
        result = run_backtest(data_store, params)
        if result and result['trades'] >= 50 and result['mdd'] > -40:
            results.append({'params': params, 'metrics': result})
        now = time.time()
        if (i+1) % max(1, len(total_combos)//10) == 0 or now - last_print > 1800:
            elapsed = now - t0
            speed = (i+1) / elapsed if elapsed > 0 else 0
            eta = (len(total_combos)-i-1) / speed / 60 if speed > 0 else 0
            bc = max((r['metrics']['cagr'] for r in results), default=0)
            bk = max((r['metrics']['calmar'] for r in results), default=0)
            print(f"  [{(i+1)/len(total_combos)*100:.0f}%] {i+1}/{len(total_combos)} | "
                  f"合格:{len(results)} | CAGR*:{bc:+.1f}% "
                  f"Calmar*:{bk:.2f} | {speed:.2f}/s ETA:{eta:.0f}min")
            last_print = now
    results.sort(key=lambda r: r['metrics']['calmar'], reverse=True)
    top20 = results[:20]
    print(f"\n  阶段1a完成: {(time.time()-t0)/60:.1f}min, 合格{len(results)}个")
    if top20:
        m = top20[0]['metrics']
        print(f"  Top1: CAGR={m['cagr']:+.1f}% MDD={m['mdd']:.1f}% "
              f"Calmar={m['calmar']:.2f} Trades={m['trades']}")
    return top20


def phase1b_sell_search(data_store, top20):
    print("\n" + "=" * 70)
    print("阶段1b: 卖出参数搜索 (Top20 x sell combos)")
    print("=" * 70)
    sell_combos = generate_sell_param_combos()
    if len(sell_combos) > 30:
        random.shuffle(sell_combos)
        sell_combos = sell_combos[:30]
    sell_combos.insert(0, {'tp_pct': 10, 'sl_pct': -3,
                           'max_hold_days': 5, 'trail_pct': 5})
    total_combos = []
    for entry in top20:
        bp = entry['params']
        for sc in sell_combos:
            p = dict(bp)
            p['sell_params'] = sc
            total_combos.append(p)
    print(f"  总组合数: {len(total_combos)}")
    results = []
    t0 = time.time()
    for i, params in enumerate(total_combos):
        result = run_backtest(data_store, params)
        if result and result['trades'] >= 50 and result['mdd'] > -40:
            results.append({'params': params, 'metrics': result})
        if (i+1) % max(1, len(total_combos)//5) == 0:
            bc = max((r['metrics']['cagr'] for r in results), default=0)
            print(f"  [{(i+1)/len(total_combos)*100:.0f}%] "
                  f"{i+1}/{len(total_combos)} | "
                  f"合格:{len(results)} | CAGR*:{bc:+.1f}%")
    results.sort(key=lambda r: r['metrics']['calmar'], reverse=True)
    top10 = results[:10]
    print(f"\n  阶段1b完成: {(time.time()-t0)/60:.1f}min, 合格{len(results)}个")
    if top10:
        m = top10[0]['metrics']
        print(f"  Top1: CAGR={m['cagr']:+.1f}% Calmar={m['calmar']:.2f}")
    return top10


def phase1c_slots_search(data_store, top10):
    print("\n" + "=" * 70)
    print("阶段1c: 仓位数搜索 (Top10 x [3,5,7,10])")
    print("=" * 70)
    total_combos = []
    for entry in top10:
        bp = entry['params']
        for n in SLOTS_OPTIONS:
            p = dict(bp)
            p['n_slots'] = n
            total_combos.append(p)
    print(f"  总组合数: {len(total_combos)}")
    results = []
    t0 = time.time()
    for params in total_combos:
        result = run_backtest(data_store, params)
        if result and result['trades'] >= 30:
            results.append({'params': params, 'metrics': result})
    results.sort(key=lambda r: r['metrics']['calmar'], reverse=True)
    top50 = results[:50]
    print(f"  阶段1c完成: {(time.time()-t0)/60:.1f}min, 合格{len(results)}个")
    if top50:
        m = top50[0]['metrics']
        print(f"  Top1: CAGR={m['cagr']:+.1f}% "
              f"Calmar={m['calmar']:.2f} "
              f"Slots={top50[0]['params']['n_slots']}")
    return top50


def phase2_full_validation(data_store_full, top50):
    print("\n" + "=" * 70)
    print("阶段2: 全量验证 (2020-01-01 ~ 2026-06-30)")
    print("=" * 70)
    print(f"  待验证: {len(top50)}")
    results = []
    t0 = time.time()
    for i, entry in enumerate(top50):
        params = dict(entry['params'])
        params['start_date'] = '2020-01-01'
        params['end_date'] = '2026-06-30'
        result = run_backtest(data_store_full, params)
        if result and result['trades'] >= 50:
            results.append({'params': params,
                            'train_metrics': entry['metrics'],
                            'full_metrics': result})
        if (i+1) % 10 == 0:
            print(f"  [{i+1}/{len(top50)}] done")
    results.sort(key=lambda r: r['full_metrics']['calmar'], reverse=True)
    print(f"  阶段2完成: {(time.time()-t0)/60:.1f}min, 合格{len(results)}个")
    return results


def print_ranking(results, top_n=10):
    print("\n" + "=" * 70)
    n = min(top_n, len(results))
    print(f"最终排行 Top{n} (Calmar排序)")
    print("=" * 70)
    hdr = (f"{'#':<3}{'CAGR%':>8}{'MDD%':>8}{'Calmar':>8}{'Sharpe':>8}"
           f"{'Trades':>7}{'WR%':>6}{'N':>4}{'TP':>5}{'SL':>5}"
           f"{'Hold':>5}{'Trail':>6}{'Stars':>6}  {'Buy':<20}")
    print(hdr)
    print("-" * len(hdr))
    for i, e in enumerate(results[:top_n]):
        m = e['full_metrics']
        p = e['params']
        sp2 = p.get('sell_params', {})
        bs = '+'.join(p.get('buy_strategies', []))
        print(f"{i+1:<3}{m['cagr']:>+7.1f}{m['mdd']:>8.1f}"
              f"{m['calmar']:>8.2f}{m['sharpe']:>8.2f}"
              f"{m['trades']:>7}{m['win_rate']:>6.1f}"
              f"{p.get('n_slots',5):>4}{sp2.get('tp_pct',10):>5}"
              f"{sp2.get('sl_pct',-3):>5}{sp2.get('max_hold_days',5):>5}"
              f"{sp2.get('trail_pct',5):>6}{p.get('min_stars',5):>6}"
              f"  {bs:<20}")
    print("\n--- 年度收益 (Top3) ---")
    for i, e in enumerate(results[:min(3, n)]):
        yr = e['full_metrics'].get('yearly', {})
        s = ' | '.join(f"{y}:{r:+.0f}%" for y, r in sorted(yr.items()))
        print(f"  #{i+1}: {s}")


def save_results(results, path):
    output = []
    for i, e in enumerate(results[:50]):
        p = e['params']
        ind = {}
        for k, v in p.get('indicators', {}).items():
            ind[k] = {kk: vv for kk, vv in v.items() if kk != '_row'}
        output.append({
            'rank': i+1,
            'params': {
                'indicators': ind,
                'min_stars': p.get('min_stars'),
                'buy_strategies': p.get('buy_strategies'),
                'sell_params': p.get('sell_params'),
                'n_slots': p.get('n_slots')},
            'train_cagr': e.get('train_metrics', {}).get('cagr'),
            'train_mdd': e.get('train_metrics', {}).get('mdd'),
            'full_cagr': e['full_metrics']['cagr'],
            'full_mdd': e['full_metrics']['mdd'],
            'full_calmar': e['full_metrics']['calmar'],
            'full_sharpe': e['full_metrics']['sharpe'],
            'trades': e['full_metrics']['trades'],
            'win_rate': e['full_metrics']['win_rate'],
            'yearly': e['full_metrics'].get('yearly', {})})
    with open(path, 'w', encoding='utf-8') as f:
        json.dump(output, f, ensure_ascii=False, indent=2)
    print(f"\n[输出] {path} ({len(output)}条)")


def main():
    t_start = time.time()
    print("=" * 70)
    print("多指标评分系统 - 参数网格搜索优化器")
    print(f"时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print("=" * 70)
    print("\n[Phase 0] 加载训练集数据...")
    train_store = DataStore(DB_PATH, '2023-12-01', '2025-12-31')
    top20 = phase1a_indicator_search(train_store, n_samples=60)
    if not top20:
        print("[FATAL] 阶段1a无结果, 退出")
        return
    top10 = phase1b_sell_search(train_store, top20)
    if not top10:
        top10 = top20[:10]
    top50 = phase1c_slots_search(train_store, top10)
    if not top50:
        top50 = top10
    del train_store
    print("\n[Phase 2] 加载全量数据...")
    full_store = DataStore(DB_PATH, '2019-12-01', '2026-06-30')
    final = phase2_full_validation(full_store, top50)
    if final:
        print_ranking(final, top_n=10)
        save_results(final, RESULTS_OUTPUT)
    else:
        print("[WARN] 全量验证无结果, 保存训练集Top50")
        fallback = [{'params': e['params'],
                     'train_metrics': e['metrics'],
                     'full_metrics': e['metrics']} for e in top50]
        save_results(fallback, RESULTS_OUTPUT)
    elapsed = time.time() - t_start
    print(f"\n{'='*70}")
    print(f"完成! 总耗时: {elapsed/60:.1f}min ({elapsed/3600:.2f}h)")
    print(f"{'='*70}")


if __name__ == '__main__':
    random.seed(42)
    main()
