#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Task #27: Signal C + Signal D 组合 + 限价TP出场 完整网格回测

(内存优化版: 用 per-code numpy 数组代替全表 shift)

入场:
  C: T-2,T-1,T close 递减; cum_ret_3d <= -0.08; turn < 5;
     当日 C 候选 >= panic_th; T+1 hour1_open 买
  D: T 日 hour4_close/hour3_close - 1 ∈ (-0.04,-0.03); hour3_close < hour1_open;
     close_rate > -5; T+1 hour1_open 买

出场:
  - TP: TP=buy*(1+tp); 检查 T+2 hour1 ~ T+hold_max hour1, hourX_high>=TP → 卖 TP
  - SL (use_sl=True): SL=buy*0.92; hourX_open<SL → 卖 open(跳空); hourX_low<=SL 且 open>=SL → 卖 SL; SL优先
  - TIME: T+hold_max hour1 仍未触发 → 卖 hour1_open

排序:
  - C 优先, D 次之
  - C: 当日候选按 cum_ret_3d 取 25-75 分位区间, 区间内最深优先
  - D: 当日候选按 |tail_dive + 0.035| 升序 (-3.5% 附近优先)

网格: 3*3*3*2*2 = 108
"""
import sqlite3
import time
import gc
import warnings
from itertools import product

warnings.filterwarnings('ignore')

import pandas as pd
import numpy as np

DB = '/home/AIWealth/data/stocks.db'
LOAD_START = '2019-11-01'
LOAD_END   = '2026-01-01'
BT_START   = '2020-01-01'
BT_END     = '2026-01-01'
INIT_CASH  = 1_000_000.0


# ============================================================
# 数据加载: 直接用 sqlite cursor 构 per-code numpy 数组, 跳过 pandas
# ============================================================
HOUR_FIELD_ORDER = []
for h in range(1, 5):
    for f in ('open', 'high', 'low', 'close'):
        HOUR_FIELD_ORDER.append(f'hour{h}_{f}')

SELECT_COLS = (['date', 'code', 'close', 'close_rate', 'turn']
               + HOUR_FIELD_ORDER)

def load_code_arrays():
    """
    直接从 sqlite 流式读取, 按 code 累加构建 per-code 数组.
    返回:
      code_arr: dict code -> dict{
        dates: np.ndarray[object str],
        close, close_rate, turn: float32 1d,
        hours: float32 2d (n,16),
      }
      bt_close_lookup: dict[(code,date)] -> close (回测窗口内, 用于 daily MTM)
    """
    print(f"[Load] streaming stock_kline {LOAD_START}~{LOAD_END} ...", flush=True)
    t0 = time.time()
    conn = sqlite3.connect(DB)
    q = f"""
    SELECT {','.join(SELECT_COLS)}
    FROM stock_kline
    WHERE date >= '{LOAD_START}' AND date < '{LOAD_END}'
      AND code NOT LIKE 'bj.%'
      AND (isST IS NULL OR isST = 0)
    ORDER BY code, date
    """
    cur = conn.execute(q)

    # 临时累加用的列表
    cur_code = None
    buf_dates = []
    buf_close = []
    buf_cr = []
    buf_turn = []
    buf_hours = []  # list of 16-tuples

    code_arr = {}
    bt_close_lookup = {}
    n_rows = 0

    def flush_code(code, dates, close, cr, turn, hours):
        if code is None or len(dates) < 6:
            return
        code_arr[code] = {
            'dates':      np.asarray(dates, dtype=object),
            'close':      np.asarray(close, dtype=np.float32),
            'close_rate': np.asarray(cr, dtype=np.float32),
            'turn':       np.asarray(turn, dtype=np.float32),
            'hours':      np.asarray(hours, dtype=np.float32),
        }

    for row in cur:
        date, code, cl, crv, tn = row[0], row[1], row[2], row[3], row[4]
        hr = row[5:21]
        if code != cur_code:
            flush_code(cur_code, buf_dates, buf_close, buf_cr, buf_turn, buf_hours)
            cur_code = code
            buf_dates = []; buf_close = []; buf_cr = []
            buf_turn = []; buf_hours = []
        buf_dates.append(date)
        buf_close.append(cl if cl is not None else np.nan)
        buf_cr.append(crv if crv is not None else np.nan)
        buf_turn.append(tn if tn is not None else np.nan)
        buf_hours.append([(x if x is not None else np.nan) for x in hr])
        if BT_START <= date < BT_END and cl is not None:
            bt_close_lookup[(code, date)] = cl
        n_rows += 1
        if n_rows % 1_000_000 == 0:
            print(f"  [Load] rows={n_rows:,}  codes={len(code_arr)+1:,}", flush=True)

    flush_code(cur_code, buf_dates, buf_close, buf_cr, buf_turn, buf_hours)
    conn.close()
    print(f"[Load] rows={n_rows:,}  codes={len(code_arr):,}  "
          f"close_lookup={len(bt_close_lookup):,}  elapsed={time.time()-t0:.1f}s",
          flush=True)
    return code_arr, bt_close_lookup


# ============================================================
# 信号扫描: per-code 向量化
# ============================================================
def scan_signals(code_arr):
    """
    返回 candidate list: 每个候选 dict{
      code, signal_date, signal_type ('C' or 'D'),
      rank_metric (C:cum_ret_3d / D:tail_dive),
      buy_idx (T+1 在 dates 里的 idx),
      buy_date, buy_price, buy_close,
    }
    + 每个 code 的 hour 矩阵保留索引以便 walk
    """
    print("[Scan] scanning signals ...", flush=True)
    t0 = time.time()
    cands_c = []
    cands_d = []

    H1O, H1C = 0, 3
    H3C = 11  # hour3_close index
    H4C = 15  # hour4_close index

    for code, arr in code_arr.items():
        dates = arr['dates']
        close = arr['close']
        cr = arr['close_rate']
        turn = arr['turn']
        hours = arr['hours']
        n = len(close)
        if n < 6:
            continue

        h1o = hours[:, H1O]
        h3c = hours[:, H3C]
        h4c = hours[:, H4C]

        # ---- Signal C ----
        close_t1 = np.empty(n, dtype=np.float32); close_t1[:] = np.nan
        close_t2 = np.empty(n, dtype=np.float32); close_t2[:] = np.nan
        close_t1[1:] = close[:-1]
        close_t2[2:] = close[:-2]
        with np.errstate(invalid='ignore', divide='ignore'):
            cum3 = close / close_t2 - 1
        mask_c = ((close_t2 > close_t1) & (close_t1 > close)
                  & (cum3 <= -0.08) & (turn < 5))
        mask_c[:2] = False
        mask_c[-1:] = False  # 需要 T+1
        idx_c = np.where(mask_c)[0]

        for i in idx_c:
            buy_idx = i + 1
            bp = h1o[buy_idx]
            bc = close[buy_idx]
            if not (bp == bp) or bp <= 0:
                continue
            cands_c.append({
                'code': code, 'signal_type': 'C',
                'signal_date': dates[i],
                'rank_metric': float(cum3[i]),
                'buy_idx': int(buy_idx),
                'buy_date': dates[buy_idx],
                'buy_price': float(bp),
                'buy_close': float(bc) if (bc == bc) else float(bp),
            })

        # ---- Signal D ----
        with np.errstate(invalid='ignore', divide='ignore'):
            tail_dive = h4c / h3c - 1
        mask_d = ((tail_dive > -0.04) & (tail_dive < -0.03)
                  & (h3c < h1o)
                  & (cr > -5)
                  & (h3c == h3c) & (h4c == h4c) & (h1o == h1o))
        mask_d[-1:] = False
        idx_d = np.where(mask_d)[0]

        for i in idx_d:
            buy_idx = i + 1
            bp = h1o[buy_idx]
            bc = close[buy_idx]
            if not (bp == bp) or bp <= 0:
                continue
            cands_d.append({
                'code': code, 'signal_type': 'D',
                'signal_date': dates[i],
                'rank_metric': float(tail_dive[i]),
                'buy_idx': int(buy_idx),
                'buy_date': dates[buy_idx],
                'buy_price': float(bp),
                'buy_close': float(bc) if (bc == bc) else float(bp),
            })

    print(f"[Scan] C={len(cands_c):,}  D={len(cands_d):,}  "
          f"elapsed={time.time()-t0:.1f}s", flush=True)
    return cands_c, cands_d


# ============================================================
# 出场计算
# ============================================================
def compute_exit(rec, code_arr, tp_pct, hold_max, use_sl):
    arr = code_arr[rec['code']]
    dates = arr['dates']
    hours = arr['hours']
    n = len(dates)
    bp = rec['buy_price']
    tp_price = bp * (1 + tp_pct)
    sl_price = bp * 0.92 if use_sl else None
    last_offset = hold_max - 1
    buy_idx = rec['buy_idx']

    for off in range(1, last_offset + 1):
        d_row = buy_idx + off
        if d_row >= n:
            return None  # 数据不够 (尾部)
        sell_date = dates[d_row]
        max_hour = 1 if off == last_offset else 4
        for h in range(1, max_hour + 1):
            base = (h - 1) * 4
            ho = hours[d_row, base + 0]
            hh = hours[d_row, base + 1]
            hl = hours[d_row, base + 2]
            if not (ho == ho and hh == hh and hl == hl):
                continue
            # SL 优先
            if use_sl:
                if ho < sl_price:
                    return {'sell_date': sell_date, 'sell_price': float(ho),
                            'exit_type': 'SL_GAP'}
                if hl <= sl_price:
                    return {'sell_date': sell_date, 'sell_price': float(sl_price),
                            'exit_type': 'SL'}
            # TP
            if hh >= tp_price:
                return {'sell_date': sell_date, 'sell_price': float(tp_price),
                        'exit_type': 'TP'}
            # TIME
            if off == last_offset and h == 1:
                return {'sell_date': sell_date, 'sell_price': float(ho),
                        'exit_type': 'TIME'}
    return None


# ============================================================
# 选股: 按 panic 与 25-75% 过滤 C; D 按 dist 排序
# ============================================================
def build_selection(cands_c, cands_d, panic_th):
    """返回 dict[buy_date] -> [候选记录列表] (C 优先, D 次之)"""
    # C 按 signal_date 分组做 panic 与 25-75 过滤
    c_by_sig = {}
    for r in cands_c:
        c_by_sig.setdefault(r['signal_date'], []).append(r)

    sel = {}
    for sd, lst in c_by_sig.items():
        if len(lst) < panic_th:
            continue
        metrics = sorted(r['rank_metric'] for r in lst)
        n = len(metrics)
        q25 = metrics[int(n * 0.25)]
        q75 = metrics[int(min(n - 1, n * 0.75))]
        flt = [r for r in lst if q25 <= r['rank_metric'] <= q75]
        flt.sort(key=lambda r: r['rank_metric'])  # 区间内 asc, 越负优先
        for r in flt:
            sel.setdefault(r['buy_date'], []).append(r)

    # D 按 buy_date 分组并按 dist 排序
    d_by_buy = {}
    for r in cands_d:
        d_by_buy.setdefault(r['buy_date'], []).append(r)
    for bd, lst in d_by_buy.items():
        lst.sort(key=lambda r: abs(r['rank_metric'] + 0.035))
        if bd in sel:
            sel[bd] = sel[bd] + lst
        else:
            sel[bd] = lst

    return sel


# ============================================================
# 单组合回测
# ============================================================
def run_backtest(cands_c, cands_d, code_arr, trading_days,
                 tp_pct, hold_max, panic_th, N, use_sl,
                 close_lookup=None, want_full=False):
    sel = build_selection(cands_c, cands_d, panic_th)

    cash = INIT_CASH
    holdings = []
    trades = []
    equity_curve = []

    for d in trading_days:
        # 1) 出场
        new_h = []
        for h in holdings:
            if h['sell_date'] == d:
                cash += h['shares'] * h['sell_price']
                trades.append({
                    'buy_date': h['buy_date'], 'sell_date': d,
                    'code': h['code'],
                    'buy_price': h['buy_price'], 'sell_price': h['sell_price'],
                    'ret': h['sell_price'] / h['buy_price'] - 1,
                    'shares': h['shares'],
                    'pnl': h['shares'] * (h['sell_price'] - h['buy_price']),
                    'exit_type': h['exit_type'],
                    'signal_type': h['signal_type'],
                })
            else:
                new_h.append(h)
        holdings = new_h

        # 2) 进场
        slots = N - len(holdings)
        if slots > 0 and d in sel:
            held_codes = {hh['code'] for hh in holdings}
            per_slot = cash / slots
            picked = 0
            for rec in sel[d]:
                if picked >= slots:
                    break
                if rec['code'] in held_codes:
                    continue
                ex = compute_exit(rec, code_arr, tp_pct, hold_max, use_sl)
                if ex is None:
                    continue
                bp = rec['buy_price']
                shares = int(per_slot / bp / 100) * 100
                if shares <= 0:
                    continue
                cost = shares * bp
                if cost > cash:
                    shares = int(cash / bp / 100) * 100
                    cost = shares * bp
                    if shares <= 0:
                        continue
                cash -= cost
                holdings.append({
                    'code': rec['code'], 'buy_date': d,
                    'sell_date': ex['sell_date'],
                    'buy_price': bp, 'sell_price': ex['sell_price'],
                    'shares': shares, 'exit_type': ex['exit_type'],
                    'signal_type': rec['signal_type'],
                    'buy_close': rec['buy_close'],
                })
                held_codes.add(rec['code'])
                picked += 1

        # 3) 当日 MTM (仅 want_full)
        if want_full:
            mtm = cash
            for hh in holdings:
                if hh['buy_date'] == d:
                    mtm += hh['shares'] * hh['buy_close']
                else:
                    cl = close_lookup.get((hh['code'], d)) if close_lookup else None
                    if cl is None or cl != cl:
                        cl = hh['buy_price']
                    mtm += hh['shares'] * cl
            equity_curve.append((d, mtm))

    final_value = cash + sum(h['shares'] * h['sell_price'] for h in holdings)

    if not trades:
        return {'cagr': -100.0, 'final': INIT_CASH, 'mdd': 0,
                'trades': 0, 'win': 0, 'avg_ret': 0,
                'yearly': [], 'equity': pd.DataFrame(), 'trade_df': pd.DataFrame()}

    tr = pd.DataFrame(trades)

    if want_full and equity_curve:
        eq = pd.DataFrame(equity_curve, columns=['date', 'equity'])
    else:
        eq = tr.sort_values('sell_date').copy()
        eq['date'] = eq['sell_date']
        eq['equity'] = INIT_CASH + eq['pnl'].cumsum()
        eq = eq[['date', 'equity']]
        first_row = pd.DataFrame([{'date': trading_days[0], 'equity': INIT_CASH}])
        eq = pd.concat([first_row, eq], ignore_index=True)

    eq['year'] = eq['date'].astype(str).str[:4]
    yearly_ret = []
    for y, sub in eq.groupby('year'):
        if len(sub) == 0:
            continue
        first = sub.iloc[0]['equity']
        last = sub.iloc[-1]['equity']
        yearly_ret.append((y, first, last, (last / first - 1) * 100))

    first_eq = eq.iloc[0]['equity']
    last_eq = eq.iloc[-1]['equity']
    days_elapsed = (pd.to_datetime(eq.iloc[-1]['date'])
                    - pd.to_datetime(eq.iloc[0]['date'])).days
    years = days_elapsed / 365.25 if days_elapsed > 0 else 1.0
    if last_eq > 0 and first_eq > 0:
        cagr = (last_eq / first_eq) ** (1 / years) - 1
    else:
        cagr = -1.0

    eq['peak'] = eq['equity'].cummax()
    eq['dd'] = eq['equity'] / eq['peak'] - 1
    mdd = float(eq['dd'].min() * 100)
    win = float((tr['ret'] > 0).mean() * 100)
    avg = float(tr['ret'].mean() * 100)

    return {
        'cagr': cagr * 100, 'final': float(last_eq), 'mdd': mdd,
        'trades': len(tr), 'win': win, 'avg_ret': avg,
        'yearly': yearly_ret, 'equity': eq, 'trade_df': tr,
    }


# ============================================================
# main
# ============================================================
def main():
    t_total = time.time()
    code_arr, close_lookup = load_code_arrays()

    # 收集回测窗口的交易日 (从 lookup 拿)
    trading_days = sorted({d for (_, d) in close_lookup.keys()})
    print(f"[Days] backtest trading days: {len(trading_days)}", flush=True)

    gc.collect()
    cands_c, cands_d = scan_signals(code_arr)

    # 仅保留 buy_date 在回测窗口内的候选
    cands_c = [r for r in cands_c if BT_START <= r['buy_date'] < BT_END]
    cands_d = [r for r in cands_d if BT_START <= r['buy_date'] < BT_END]
    print(f"[Scan] in BT window: C={len(cands_c):,}  D={len(cands_d):,}", flush=True)

    # 网格
    tp_list    = [0.02, 0.03, 0.05]
    hold_list  = [2, 3, 5]
    panic_list = [5, 10, 15]
    N_list     = [1, 3]
    sl_list    = [False, True]
    grid = list(product(tp_list, hold_list, panic_list, N_list, sl_list))
    print(f"\n[Grid] running {len(grid)} combinations ...\n", flush=True)

    grid_results = []
    t0 = time.time()
    for i, (tp, hm, pt, N, sl) in enumerate(grid, 1):
        r = run_backtest(cands_c, cands_d, code_arr, trading_days,
                         tp_pct=tp, hold_max=hm, panic_th=pt,
                         N=N, use_sl=sl, close_lookup=None, want_full=False)
        grid_results.append({
            'tp_pct': tp, 'hold_max': hm, 'panic_th': pt,
            'N': N, 'use_sl': sl,
            'cagr': r['cagr'], 'final': r['final'], 'mdd': r['mdd'],
            'trades': r['trades'], 'win': r['win'], 'avg_ret': r['avg_ret'],
        })
        if i % 12 == 0 or i == len(grid):
            print(f"  [{i:3d}/{len(grid)}]  elapsed={time.time()-t0:.1f}s "
                  f"last: tp={tp:.02f} hm={hm} pt={pt} N={N} sl={sl} "
                  f"CAGR={r['cagr']:+.2f}% trades={r['trades']}",
                  flush=True)

    res_df = pd.DataFrame(grid_results).sort_values('cagr', ascending=False)
    print("\n" + "=" * 96)
    print("★ 全部 108 组合 CAGR 排行 (Top 20)")
    print("=" * 96)
    print(f"  {'rk':>3s} {'tp':>5s} {'hm':>3s} {'pnc':>4s} {'N':>2s} {'SL':>5s}  "
          f"{'CAGR':>8s} {'final':>14s} {'MDD':>8s} {'trades':>7s} {'avg':>8s} {'win':>7s}")
    for rk, (_, row) in enumerate(res_df.head(20).iterrows(), 1):
        print(f"  {rk:>3d} {row['tp_pct']:>5.2f} {row['hold_max']:>3d} "
              f"{row['panic_th']:>4d} {row['N']:>2d} {str(row['use_sl']):>5s}  "
              f"{row['cagr']:+7.2f}% {row['final']:>14,.0f} {row['mdd']:>7.2f}% "
              f"{row['trades']:>7d} {row['avg_ret']:+7.3f}% {row['win']:6.2f}%")

    print(f"\n  -- 末 5 名 (供参照) --")
    for rk, (_, row) in enumerate(res_df.tail(5).iterrows(),
                                  start=len(res_df) - 4):
        print(f"  {rk:>3d} {row['tp_pct']:>5.2f} {row['hold_max']:>3d} "
              f"{row['panic_th']:>4d} {row['N']:>2d} {str(row['use_sl']):>5s}  "
              f"{row['cagr']:+7.2f}% {row['final']:>14,.0f} {row['mdd']:>7.2f}% "
              f"{row['trades']:>7d} {row['avg_ret']:+7.3f}% {row['win']:6.2f}%")

    # ============================================================
    # 最优配置 full MTM 回测
    # ============================================================
    best = res_df.iloc[0]
    print("\n" + "=" * 96)
    print(f"★ 最优配置: tp={best['tp_pct']}, hold_max={best['hold_max']}, "
          f"panic_th={best['panic_th']}, N={best['N']}, use_sl={best['use_sl']}")
    print("=" * 96)
    full = run_backtest(cands_c, cands_d, code_arr, trading_days,
                        tp_pct=best['tp_pct'], hold_max=int(best['hold_max']),
                        panic_th=int(best['panic_th']), N=int(best['N']),
                        use_sl=bool(best['use_sl']),
                        close_lookup=close_lookup, want_full=True)

    print("\n[FullResult] (Daily MTM)")
    print(f"  起始资金: {INIT_CASH:>14,.2f}")
    print(f"  终值:     {full['final']:>14,.2f}")
    print(f"  总收益:   {(full['final']/INIT_CASH-1)*100:+.2f}%")
    print(f"  CAGR:     {full['cagr']:+.2f}%")
    print(f"  最大回撤: {full['mdd']:.2f}%")
    print(f"  交易笔数: {full['trades']:,}")
    print(f"  单笔均值: {full['avg_ret']:+.3f}%")
    print(f"  胜率:     {full['win']:.2f}%")

    tr = full['trade_df']
    if len(tr) > 0:
        print(f"\n  出场类型分布:")
        for et, cnt in tr['exit_type'].value_counts().items():
            sub = tr[tr['exit_type'] == et]
            print(f"    {et:<8s}  N={cnt:>5d} ({cnt/len(tr)*100:5.1f}%)  "
                  f"avg={sub['ret'].mean()*100:+6.3f}%  "
                  f"win={(sub['ret']>0).mean()*100:5.2f}%")
        print(f"\n  信号来源分布:")
        for st, cnt in tr['signal_type'].value_counts().items():
            sub = tr[tr['signal_type'] == st]
            print(f"    Signal {st}  N={cnt:>5d} ({cnt/len(tr)*100:5.1f}%)  "
                  f"avg={sub['ret'].mean()*100:+6.3f}%  "
                  f"win={(sub['ret']>0).mean()*100:5.2f}%")

    print(f"\n  逐年收益率:")
    for y, fe, le, ret in full['yearly']:
        print(f"    {y}  {fe:>14,.0f} -> {le:>14,.0f}   {ret:+7.2f}%")

    # ============================================================
    # Top 5 详细
    # ============================================================
    print("\n" + "=" * 96)
    print("★ Top 5 配置逐年收益详情 (full daily MTM)")
    print("=" * 96)
    for rk in range(min(5, len(res_df))):
        row = res_df.iloc[rk]
        rr = run_backtest(cands_c, cands_d, code_arr, trading_days,
                          tp_pct=row['tp_pct'], hold_max=int(row['hold_max']),
                          panic_th=int(row['panic_th']), N=int(row['N']),
                          use_sl=bool(row['use_sl']),
                          close_lookup=close_lookup, want_full=True)
        print(f"\n  [#{rk+1}] tp={row['tp_pct']} hm={row['hold_max']} "
              f"pnc={row['panic_th']} N={row['N']} sl={row['use_sl']}  "
              f"CAGR={rr['cagr']:+.2f}%  MDD={rr['mdd']:.2f}%  "
              f"trades={rr['trades']}  win={rr['win']:.2f}%  "
              f"avg={rr['avg_ret']:+.3f}%")
        for y, fe, le, ret in rr['yearly']:
            print(f"      {y}  {fe:>14,.0f} -> {le:>14,.0f}   {ret:+7.2f}%")

    # ============================================================
    # 达标判断
    # ============================================================
    print("\n" + "=" * 96)
    qualified = res_df[res_df['cagr'] >= 50.0]
    if len(qualified) > 0:
        print(f"★ 达标 (CAGR>=50%): {len(qualified)}/{len(res_df)} 组合")
    else:
        print(f"★ 未达标: 无 CAGR>=50% 配置, 最高 CAGR={res_df.iloc[0]['cagr']:+.2f}%")
    print("=" * 96)
    print(f"\n[Done] total elapsed={time.time()-t_total:.1f}s", flush=True)


if __name__ == '__main__':
    main()
