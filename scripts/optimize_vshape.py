#!/usr/bin/env python3
"""V字形态高开买入策略 - 大规模参数网格搜索优化(预计算版v2)
核心优化: 预计算信号特征 + 分步精筛
阶段1: 粗筛(2023-2025, ~41K组合)
阶段2A: Top100 x slots x gap_max (2000组合, 6年全量)
阶段2B: Top50 x 变种(board/mf/turn/shape, ~3600组合)
"""
import sys, sqlite3, math, time, itertools, json
from collections import defaultdict
from datetime import datetime
sys.path.insert(0, '/home/AIWealth/scripts')
from compliance_utils import (
    safe_float, is_one_word_board, is_limit_up, is_limit_down,
    is_st, get_limit_threshold
)

DB_PATH = '/home/AIWealth/data/stocks.db'
INITIAL_CAPITAL = 1_000_000.0
HOUR_FIELDS = {
    1: ('hour1_open', 'hour1_high', 'hour1_low', 'hour1_close'),
    2: ('hour2_open', 'hour2_high', 'hour2_low', 'hour2_close'),
    3: ('hour3_open', 'hour3_high', 'hour3_low', 'hour3_close'),
    4: ('hour4_open', 'hour4_high', 'hour4_low', 'hour4_close'),
}

PARAM_GRID_PHASE1 = {
    'v_lookback_days': [2, 3, 4, 5],
    'v_cum_drop': [-3.0, -5.0, -7.0, -10.0],
    'v_min_down_days': [1, 2, 3],
    'gap_up_min': [1.0, 2.0, 3.0, 4.0],
    'tp_pct': [2.0, 3.0, 5.0, 8.0, 10.0, 999.0],
    'sl_pct': [-1.5, -2.0, -3.0, -5.0, -7.0, -999.0],
    'max_hold_days': [1, 2, 3, 5, 7, 10],
}
PHASE2_N_SLOTS = [1, 2, 3, 5]
PHASE2_GAP_UP_MAX = [4.0, 5.0, 7.0, 10.0, 20.0]
BOARD_FILTERS = ['all', 'sz.300', 'sh.688']
MARKET_FILTERS = [False, True]
MIN_TURNS = [0, 3.0, 5.0, 8.0]
SHAPE_TYPES = ['V', 'N', 'both']
LB_OFFSET = {2: 0, 3: 2, 4: 4, 5: 6}


def precompute_signals(conn, start_date, end_date, board_filter='all'):
    """预计算所有信号特征"""
    print(f"  [预计算] {start_date}~{end_date} board={board_filter} ...", flush=True)
    t0 = time.time()
    cur = conn.cursor()
    cur.execute("SELECT DISTINCT date FROM stock_kline WHERE date>=? AND date<=? ORDER BY date",
                (start_date, end_date))
    trading_dates = [r[0] for r in cur.fetchall()]
    date_idx_map = {d: i for i, d in enumerate(trading_dates)}
    n_dates = len(trading_dates)

    if board_filter == 'sz.300':
        board_cond = "AND code LIKE 'sz.300%'"
    elif board_filter == 'sh.688':
        board_cond = "AND code LIKE 'sh.688%'"
    else:
        board_cond = "AND (code LIKE 'sz.300%' OR code LIKE 'sh.688%')"

    code_daily = defaultdict(list)
    day_stocks_raw = defaultdict(list)

    for year in range(int(start_date[:4]), int(end_date[:4]) + 1):
        y_s, y_e = f"{year}-01-01", f"{year}-12-31"
        cur.execute(f"""SELECT date,code,preclose,open,close,open_rate,close_rate,high,low,turn,
                        hour1_open,hour1_high,hour1_low,hour1_close,
                        hour2_open,hour2_high,hour2_low,hour2_close,
                        hour3_open,hour3_high,hour3_low,hour3_close,
                        hour4_open,hour4_high,hour4_low,hour4_close
                    FROM stock_kline WHERE date>=? AND date<=? {board_cond} AND isST=0""", (y_s, y_e))
        for r in cur:
            dt = r[0]
            if dt not in date_idx_map:
                continue
            di = date_idx_map[dt]
            code = r[1]
            preclose = safe_float(r[2]); day_open = safe_float(r[3])
            day_close = safe_float(r[4]); open_rate = safe_float(r[5], None)
            close_rate = safe_float(r[6], None)
            day_high = safe_float(r[7]); day_low = safe_float(r[8])
            turn = safe_float(r[9], 0.0)
            if close_rate is not None:
                code_daily[code].append((di, close_rate))
            if open_rate is None or preclose <= 0 or day_open <= 0:
                continue
            if is_one_word_board(day_open, day_high, day_low, day_close):
                continue
            h1o = safe_float(r[10]); h1h = safe_float(r[11])
            h1l = safe_float(r[12]); h1c = safe_float(r[13])
            if h1o <= 0 or is_one_word_board(h1o, h1h, h1l, h1c):
                continue
            if is_limit_up(code, h1o, preclose):
                continue
            hour_prices = tuple(safe_float(r[i]) for i in range(10, 26))
            day_stocks_raw[di].append((code, open_rate, turn, preclose, h1o, hour_prices))

    for code in code_daily:
        code_daily[code].sort()
    print(f"    原始: {n_dates}天, {len(code_daily)}股票", flush=True)

    # 预计算lookback
    code_lookback = {}
    for code, hist in code_daily.items():
        lb_data = {}
        for pos in range(len(hist)):
            di = hist[pos][0]
            res_lb = []
            for lb in [2, 3, 4, 5]:
                if pos >= lb:
                    window = hist[pos-lb:pos]
                    cum = sum(x[1] for x in window)
                    dd = sum(1 for x in window if x[1] < 0)
                    res_lb.extend([cum, dd])
                else:
                    res_lb.extend([None, None])
            lb_data[di] = tuple(res_lb)
        code_lookback[code] = lb_data

    # 构建信号表
    day_signals = {}
    for di, stocks in day_stocks_raw.items():
        signals = []
        for code, open_rate, turn, preclose, h1o, hour_prices in stocks:
            lb = code_lookback.get(code, {}).get(di)
            if lb is None:
                continue
            signals.append((code, open_rate, turn, h1o, preclose,
                           lb[0],lb[1],lb[2],lb[3],lb[4],lb[5],lb[6],lb[7], hour_prices))
        if signals:
            day_signals[di] = signals

    # 卖出用hour数据
    day_all_hours = defaultdict(dict)
    for year in range(int(start_date[:4]), int(end_date[:4]) + 1):
        y_s, y_e = f"{year}-01-01", f"{year}-12-31"
        cur.execute(f"""SELECT date,code,preclose,
                        hour1_open,hour1_high,hour1_low,hour1_close,
                        hour2_open,hour2_high,hour2_low,hour2_close,
                        hour3_open,hour3_high,hour3_low,hour3_close,
                        hour4_open,hour4_high,hour4_low,hour4_close
                    FROM stock_kline WHERE date>=? AND date<=? {board_cond} AND isST=0""", (y_s, y_e))
        for r in cur:
            dt = r[0]
            if dt not in date_idx_map:
                continue
            day_all_hours[date_idx_map[dt]][r[1]] = tuple(safe_float(x) for x in r[2:])

    # 指数
    index_rates = {}
    cur.execute("SELECT date, close_rate FROM index_kline WHERE code='sh.000001' AND date>=? AND date<=?",
                (start_date, end_date))
    for r in cur:
        if r[0] in date_idx_map:
            index_rates[date_idx_map[r[0]]] = safe_float(r[1], 0.0)

    print(f"  [预计算完成] {len(day_signals)}天信号, 耗时{time.time()-t0:.1f}s", flush=True)
    return trading_dates, day_signals, day_all_hours, index_rates


def fast_backtest(trading_dates, day_signals, day_all_hours, index_rates, params):
    """极速回测引擎"""
    v_lookback = params['v_lookback_days']
    v_cum_drop = params['v_cum_drop']
    v_min_down = params['v_min_down_days']
    gap_up_min = params['gap_up_min']
    gap_up_max = params['gap_up_max']
    tp_pct = params['tp_pct']
    sl_pct = params['sl_pct']
    max_hold_days = params['max_hold_days']
    n_slots = params['n_slots']
    market_filter = params.get('market_filter', False)
    min_turn = params.get('min_turn', 0)
    shape_type = params.get('shape_type', 'V')
    no_tp = tp_pct > 900
    no_sl = sl_pct < -900
    gap_no_max = gap_up_max >= 20.0
    lb_off = LB_OFFSET[v_lookback]
    cum_col = 5 + lb_off
    down_col = 6 + lb_off

    cash = INITIAL_CAPITAL
    positions = []  # [code, buy_price, buy_di, hold_days, shares, last_price]
    n_trades = 0; n_wins = 0; total_pnl = 0.0
    year_start_eq = {}; year_end_eq = {}
    peak = INITIAL_CAPITAL; max_dd = 0.0; prev_year = None
    n_dates = len(trading_dates)

    for di in range(n_dates):
        today = trading_dates[di]
        cur_year = today[:4]
        if prev_year is None:
            year_start_eq[cur_year] = INITIAL_CAPITAL
        elif cur_year != prev_year:
            year_start_eq[cur_year] = year_end_eq.get(prev_year, INITIAL_CAPITAL)
        prev_year = cur_year

        skip_buy = market_filter and index_rates.get(di, 0.0) < -1.0
        for pos in positions:
            if pos[2] != di:
                pos[3] += 1

        candidates = []
        if not skip_buy and len(positions) < n_slots:
            signals = day_signals.get(di)
            if signals:
                held_codes = {p[0] for p in positions}
                for sig in signals:
                    code = sig[0]
                    if code in held_codes:
                        continue
                    opr = sig[1]
                    if opr < gap_up_min:
                        continue
                    if not gap_no_max and opr > gap_up_max:
                        continue
                    if min_turn > 0 and sig[2] < min_turn:
                        continue
                    cum_val = sig[cum_col]
                    down_val = sig[down_col]
                    if cum_val is None:
                        continue
                    matched = False
                    if shape_type in ('V', 'both'):
                        if cum_val <= v_cum_drop and down_val >= v_min_down:
                            matched = True
                    if not matched and shape_type in ('N', 'both'):
                        if down_val >= v_min_down and cum_val <= v_cum_drop * 0.5:
                            matched = True
                    if not matched:
                        continue
                    candidates.append((code, sig[3], cum_val))
                candidates.sort(key=lambda x: x[2])

        all_hours = day_all_hours.get(di, {})
        for hour in range(1, 5):
            h_off = (hour - 1) * 4 + 1
            survived = []
            for pos in positions:
                if pos[2] == di:
                    survived.append(pos); continue
                hdata = all_hours.get(pos[0])
                if not hdata:
                    survived.append(pos); continue
                hc = hdata[h_off + 3]
                if hc <= 0:
                    survived.append(pos); continue
                preclose_p = hdata[0]
                if preclose_p > 0 and is_limit_down(pos[0], hc, preclose_p):
                    ho = hdata[h_off]; hh = hdata[h_off+1]; hl = hdata[h_off+2]
                    if is_one_word_board(ho, hh, hl, hc):
                        pos[5] = hc; survived.append(pos); continue
                pnl_pct = (hc - pos[1]) / pos[1] * 100.0
                sell = False
                if not no_tp and pnl_pct >= tp_pct: sell = True
                elif not no_sl and pnl_pct <= sl_pct: sell = True
                elif pos[3] >= max_hold_days and hour == 4: sell = True
                if sell:
                    cash += pos[4] * hc; n_trades += 1
                    if pnl_pct > 0: n_wins += 1
                    total_pnl += pnl_pct
                else:
                    pos[5] = hc; survived.append(pos)
            positions = survived

            if hour == 1 and candidates:
                free_slots = n_slots - len(positions)
                if free_slots > 0:
                    held_now = {p[0] for p in positions}; bought = 0
                    for code, h1o, _ in candidates:
                        if bought >= free_slots: break
                        if code in held_now: continue
                        slot_cap = cash / max(1, free_slots - bought)
                        shares = int(slot_cap / h1o // 100) * 100
                        if shares <= 0: continue
                        cost = shares * h1o
                        if cost > cash: continue
                        cash -= cost
                        positions.append([code, h1o, di, 0, shares, h1o])
                        held_now.add(code); bought += 1

        equity = cash
        for pos in positions:
            hdata = all_hours.get(pos[0])
            if hdata and hdata[13] > 0:
                pos[5] = hdata[13]; equity += pos[4] * hdata[13]
            else:
                equity += pos[4] * pos[5]
        year_end_eq[cur_year] = equity
        if equity > peak: peak = equity
        dd = (peak - equity) / peak * 100 if peak > 0 else 0
        if dd > max_dd: max_dd = dd

    if positions:
        last_hours = day_all_hours.get(n_dates - 1, {})
        for pos in positions:
            sp = pos[5]
            hdata = last_hours.get(pos[0])
            if hdata and hdata[13] > 0: sp = hdata[13]
            pnl_pct = (sp - pos[1]) / pos[1] * 100.0
            cash += pos[4] * sp; n_trades += 1
            if pnl_pct > 0: n_wins += 1
            total_pnl += pnl_pct

    final_eq = cash
    yearly_rets = {}
    for yr in sorted(year_start_eq.keys()):
        s = year_start_eq[yr]; e = year_end_eq.get(yr, s)
        yearly_rets[yr] = ((e / s - 1) * 100) if s > 0 else 0.0
    total_years = max(0.05, n_dates / 245.0)
    cagr = ((final_eq / INITIAL_CAPITAL) ** (1.0 / total_years) - 1) * 100 if final_eq > 0 else -100
    win_rate = (n_wins / n_trades * 100) if n_trades > 0 else 0

    return {'n_trades': n_trades, 'cagr': cagr, 'win_rate': win_rate,
            'max_dd': max_dd, 'total_return': (final_eq/INITIAL_CAPITAL-1)*100,
            'yearly_rets': yearly_rets}


def phase1_search(conn):
    print("\n" + "=" * 70)
    print("  阶段1: 粗筛 (2023-2025训练集, n_slots=3, gap_up_max=20)")
    print("=" * 70, flush=True)
    td, ds, dah, ir = precompute_signals(conn, '2023-01-01', '2025-12-31', 'all')
    keys = list(PARAM_GRID_PHASE1.keys())
    values = [PARAM_GRID_PHASE1[k] for k in keys]
    combos = list(itertools.product(*values))
    total = len(combos)
    print(f"  搜索空间: {total} 组合", flush=True)
    results = []; t0 = time.time()
    for idx, combo in enumerate(combos):
        params = dict(zip(keys, combo))
        params['n_slots'] = 3; params['gap_up_max'] = 20.0
        params['market_filter'] = False; params['min_turn'] = 0; params['shape_type'] = 'V'
        res = fast_backtest(td, ds, dah, ir, params)
        if res['n_trades'] >= 10:
            results.append((params.copy(), res))
        if (idx + 1) % 500 == 0:
            elapsed = time.time() - t0; speed = (idx+1)/elapsed
            print(f"  [{idx+1}/{total}] {speed:.1f}/s ETA={int((total-idx-1)/speed)}s valid={len(results)}", flush=True)
    results.sort(key=lambda x: x[1]['cagr'], reverse=True)
    print(f"\n  阶段1完成: {time.time()-t0:.0f}s, 有效={len(results)}", flush=True)
    print(f"\n  Top 20:")
    for rank, (p, r) in enumerate(results[:20], 1):
        yr_s = " ".join(f"{y}:{v:+.0f}%" for y, v in sorted(r['yearly_rets'].items()))
        print(f"  #{rank:2d}: CAGR={r['cagr']:+.1f}% WR={r['win_rate']:.0f}% MDD=-{r['max_dd']:.1f}% T={r['n_trades']}"
              f" | lb={p['v_lookback_days']} drop={p['v_cum_drop']} down={p['v_min_down_days']}"
              f" gap={p['gap_up_min']} tp={p['tp_pct']} sl={p['sl_pct']} hold={p['max_hold_days']} | {yr_s}")
    return results[:100]


def phase2_search(conn, top100):
    print("\n" + "=" * 70)
    print("  阶段2: 精筛 (2021-2026全量, 分步)")
    print("=" * 70, flush=True)

    # Step A: 全板块, Top100 x slots x gap_max
    print("\n  [Step A] 加载全量数据 board=all...")
    td_all, ds_all, dah_all, ir_all = precompute_signals(conn, '2021-01-01', '2026-06-30', 'all')
    combos_a = []
    for bp, _ in top100:
        for ns in PHASE2_N_SLOTS:
            for gm in PHASE2_GAP_UP_MAX:
                p = bp.copy(); p['n_slots'] = ns; p['gap_up_max'] = gm
                p['board_filter'] = 'all'; p['market_filter'] = False
                p['min_turn'] = 0; p['shape_type'] = 'V'
                combos_a.append(p)
    total_a = len(combos_a)
    print(f"  [Step A] {total_a} 组合", flush=True)
    results_a = []; t0 = time.time()
    for idx, params in enumerate(combos_a):
        res = fast_backtest(td_all, ds_all, dah_all, ir_all, params)
        if res['n_trades'] >= 20:
            w = min(res['yearly_rets'].values()) if res['yearly_rets'] else -100
            results_a.append((params.copy(), res, w))
        if (idx+1) % 500 == 0:
            elapsed = time.time()-t0; speed = (idx+1)/elapsed
            print(f"    [{idx+1}/{total_a}] {speed:.1f}/s ETA={int((total_a-idx-1)/speed)}s valid={len(results_a)}", flush=True)
    results_a.sort(key=lambda x: x[1]['cagr'], reverse=True)
    print(f"\n  [Step A完成] {time.time()-t0:.0f}s, 有效={len(results_a)}", flush=True)
    print(f"\n  Step A Top 10:")
    for rank, (p, r, w) in enumerate(results_a[:10], 1):
        yr_s = " ".join(f"{y}:{v:+.0f}%" for y, v in sorted(r['yearly_rets'].items()))
        print(f"  #{rank}: CAGR={r['cagr']:+.1f}% Worst={w:+.1f}% slots={p['n_slots']} gap_max={p['gap_up_max']} | {yr_s}")

    # Step B: Top50 x 变种
    top50 = results_a[:50]
    print(f"\n  [Step B] Top50 变种展开 (board x mf x turn x shape)...")
    data_cache = {'all': (td_all, ds_all, dah_all, ir_all)}
    for bf in ['sz.300', 'sh.688']:
        print(f"    加载 {bf}...")
        data_cache[bf] = precompute_signals(conn, '2021-01-01', '2026-06-30', bf)
    combos_b = []
    for bp, _, _ in top50:
        for bf in BOARD_FILTERS:
            for mf in MARKET_FILTERS:
                for mt in MIN_TURNS:
                    for st in SHAPE_TYPES:
                        p = bp.copy(); p['board_filter'] = bf
                        p['market_filter'] = mf; p['min_turn'] = mt; p['shape_type'] = st
                        combos_b.append(p)
    total_b = len(combos_b)
    print(f"  [Step B] {total_b} 组合", flush=True)
    results_b = []; t0 = time.time()
    for idx, params in enumerate(combos_b):
        bf = params.get('board_filter', 'all')
        t, d, a, i = data_cache[bf]
        res = fast_backtest(t, d, a, i, params)
        if res['n_trades'] >= 20:
            w = min(res['yearly_rets'].values()) if res['yearly_rets'] else -100
            results_b.append((params.copy(), res, w))
        if (idx+1) % 1000 == 0:
            elapsed = time.time()-t0; speed = (idx+1)/elapsed
            print(f"    [{idx+1}/{total_b}] {speed:.1f}/s ETA={int((total_b-idx-1)/speed)}s valid={len(results_b)}", flush=True)
    print(f"\n  [Step B完成] {time.time()-t0:.0f}s, 有效={len(results_b)}", flush=True)

    # 合并去重
    all_r = results_a + results_b
    seen = set(); deduped = []
    for p, r, w in all_r:
        key = (p['v_lookback_days'], p['v_cum_drop'], p['v_min_down_days'],
               p['gap_up_min'], p['gap_up_max'], p['tp_pct'], p['sl_pct'],
               p['max_hold_days'], p['n_slots'], p.get('board_filter','all'),
               p.get('market_filter',False), p.get('min_turn',0), p.get('shape_type','V'))
        if key not in seen:
            seen.add(key); deduped.append((p, r, w))
    deduped.sort(key=lambda x: x[1]['cagr'], reverse=True)
    print(f"\n  阶段2总计: {len(deduped)} 去重有效结果", flush=True)
    return deduped


def print_final_results(results):
    print("\n" + "=" * 70)
    print("  ===== V字形态策略参数优化结果 =====")
    print("=" * 70)
    if not results:
        print("  无有效结果!"); return
    print(f"\n  有效结果: {len(results)}")
    print(f"\n  Top 10（按6年CAGR排序）:")
    for rank, (p, r, w) in enumerate(results[:10], 1):
        print(f"\n  #{rank}: CAGR={r['cagr']:+.1f}% WR={r['win_rate']:.0f}% MDD=-{r['max_dd']:.1f}% T={r['n_trades']} Worst={w:+.1f}%")
        print(f"       lookback={p['v_lookback_days']} cum_drop={p['v_cum_drop']}% min_down={p['v_min_down_days']}"
              f" gap=[{p['gap_up_min']}%,{p['gap_up_max']}%] tp={p['tp_pct']}% sl={p['sl_pct']}% hold={p['max_hold_days']} slots={p['n_slots']}")
        print(f"       board={p.get('board_filter','all')} mf={p.get('market_filter',False)} turn={p.get('min_turn',0)} shape={p.get('shape_type','V')}")
        yr_str = "  ".join(f"{y}:{v:+.0f}%" for y, v in sorted(r['yearly_rets'].items()))
        print(f"       年度: {yr_str}")

    stable = [(p,r,w) for p,r,w in results if r['cagr'] > 50 and w > -20]
    stable.sort(key=lambda x: x[1]['cagr'], reverse=True)
    print(f"\n  {'='*70}")
    print(f"  稳定组合 (CAGR>50% & 最差年>-20%): {len(stable)} 个")
    for rank, (p, r, w) in enumerate(stable[:10], 1):
        yr_str = "  ".join(f"{y}:{v:+.0f}%" for y, v in sorted(r['yearly_rets'].items()))
        print(f"  [稳定#{rank}]: CAGR={r['cagr']:+.1f}% Worst={w:+.1f}% MDD=-{r['max_dd']:.1f}% T={r['n_trades']}")
        print(f"       lb={p['v_lookback_days']} drop={p['v_cum_drop']} gap=[{p['gap_up_min']},{p['gap_up_max']}]"
              f" tp={p['tp_pct']} sl={p['sl_pct']} hold={p['max_hold_days']} slots={p['n_slots']}"
              f" board={p.get('board_filter','all')} mf={p.get('market_filter',False)} turn={p.get('min_turn',0)}")
        print(f"       {yr_str}")

    high = [(p,r,w) for p,r,w in results if r['cagr'] > 100]
    high.sort(key=lambda x: x[2], reverse=True)
    print(f"\n  {'='*70}")
    print(f"  高收益 (CAGR>100%): {len(high)} 个")
    for rank, (p, r, w) in enumerate(high[:10], 1):
        yr_str = "  ".join(f"{y}:{v:+.0f}%" for y, v in sorted(r['yearly_rets'].items()))
        print(f"  [高收益#{rank}]: CAGR={r['cagr']:+.1f}% Worst={w:+.1f}% MDD=-{r['max_dd']:.1f}% T={r['n_trades']} | {yr_str}")


def save_results_json(results, filepath):
    output = []
    for p, r, w in results[:200]:
        output.append({'params': p, 'cagr': round(r['cagr'],2), 'win_rate': round(r['win_rate'],1),
                      'max_dd': round(r['max_dd'],1), 'n_trades': r['n_trades'],
                      'worst_year': round(w,1), 'total_return': round(r['total_return'],1),
                      'yearly_rets': {k: round(v,1) for k,v in r['yearly_rets'].items()}})
    with open(filepath, 'w') as f:
        json.dump(output, f, indent=2, ensure_ascii=False)
    print(f"\n  结果保存: {filepath} ({len(output)}条)")


def main():
    t_start = time.time()
    print("=" * 70)
    print("  V字形态高开买入策略 - 大规模参数网格搜索")
    print(f"  启动: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print("=" * 70, flush=True)
    conn = sqlite3.connect(DB_PATH)

    top100 = phase1_search(conn)
    if not top100:
        print("\n  [ERROR] 阶段1无结果"); conn.close(); return

    results = phase2_search(conn, top100)
    if not results:
        print("\n  [ERROR] 阶段2无结果"); conn.close(); return

    print_final_results(results)
    save_results_json(results, '/home/AIWealth/scripts/optimize_vshape_results.json')
    conn.close()
    print(f"\n{'='*70}\n  总耗时: {(time.time()-t_start)/60:.1f}分钟\n{'='*70}")


if __name__ == '__main__':
    main()
