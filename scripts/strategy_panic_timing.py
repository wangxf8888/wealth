#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
策略: 市场恐慌择时 + 全仓反弹限价止盈
核心: 等市场恐慌(大量股票同时暴跌)→ 抄底Signal C候选 → 限价TP止盈 / 超时兜底

T+1合规: 买入当日不可卖出, 最早T+2 hour1开始检测TP
限价止盈: 当 hourX_high >= TP 时, 按 TP 价格成交 (无歧义)
"""

import sqlite3
import pandas as pd
import numpy as np
from collections import defaultdict
from datetime import datetime
import sys
import os
import itertools

DB_PATH = '/home/AIWealth/data/stocks.db'
INIT_CAPITAL = 1_000_000.0
START_YEAR = 2020
END_YEAR = 2025

# ==================== 数据加载 ====================
def load_data():
    print(f"[{datetime.now():%H:%M:%S}] 加载stock_kline...", flush=True)
    conn = sqlite3.connect(DB_PATH)
    sk = pd.read_sql_query("""
        SELECT date, code, preclose, close, close_rate, turn, isST,
               hour1_open, hour1_high,
               hour2_high,
               hour3_high,
               hour4_high
        FROM stock_kline
        WHERE date >= '2019-12-01' AND date <= '2025-12-31'
    """, conn)
    print(f"  stock_kline rows: {len(sk):,}", flush=True)

    idx = pd.read_sql_query("""
        SELECT date, close_rate FROM index_kline WHERE code='sh.000001'
    """, conn)
    conn.close()
    idx['close_rate'] = idx['close_rate'].astype(float)

    # 排除ST、北交所
    sk = sk[sk['isST'] == 0].copy()
    sk = sk[~sk['code'].str.startswith('bj.')].copy()
    sk.drop(columns=['isST'], inplace=True)
    sk['date'] = sk['date'].astype(str)
    # 压缩为 float32
    for col in ['preclose', 'close', 'close_rate', 'turn',
                'hour1_open', 'hour1_high', 'hour2_high', 'hour3_high', 'hour4_high']:
        sk[col] = sk[col].astype('float32')
    sk = sk.sort_values(['code', 'date']).reset_index(drop=True)
    print(f"[{datetime.now():%H:%M:%S}] 过滤后 rows: {len(sk):,}", flush=True)

    # 3日累计跌幅 = close / close_{t-3} - 1
    g = sk.groupby('code')['close']
    c1 = g.shift(1)
    c2 = g.shift(2)
    c3 = g.shift(3)
    sk['cumret_3d'] = (sk['close'] / c3 - 1.0).astype('float32')
    # 3日连跌
    sk['is_3down'] = (sk['close'] < c1) & (c1 < c2) & (c2 < c3)
    # Signal C
    sk['signal_c'] = sk['is_3down'] & (sk['cumret_3d'] <= -0.08)
    sk.drop(columns=['is_3down'], inplace=True)

    # 跌停标识 (向量化)
    ratio = (sk['close'] / sk['preclose']).round(2)
    is_gem_star = sk['code'].str.startswith('sz.30') | sk['code'].str.startswith('sh.688')
    sk['is_limit_down'] = ((is_gem_star & (ratio <= 0.80)) | (~is_gem_star & (ratio <= 0.90))).fillna(False)

    print(f"[{datetime.now():%H:%M:%S}] 加载+衡算完毕", flush=True)
    return sk, idx

# ==================== 恐慌日判定 ====================
def compute_panic_metrics(sk, idx):
    """返回每日: signal_c_count, drop5pct_ratio, index_close_rate"""
    # 每日 signal_c_count
    daily_sc = sk[sk['signal_c']].groupby('date').size().rename('signal_c_count')
    # 每日 drop5pct_ratio = close_rate <= -5 占比 (close_rate是百分比形式)
    g = sk.groupby('date').agg(
        total=('code', 'count'),
        drop5=('close_rate', lambda x: (x <= -5).sum())
    )
    g['drop5pct_ratio'] = g['drop5'] / g['total'] * 100  # 百分点
    daily = g[['drop5pct_ratio']].copy()
    daily = daily.join(daily_sc, how='left').fillna({'signal_c_count': 0})
    daily['signal_c_count'] = daily['signal_c_count'].astype(int)
    idx2 = idx.set_index('date').rename(columns={'close_rate': 'idx_close_rate'})
    daily = daily.join(idx2, how='left')
    return daily.reset_index()

def print_panic_distribution(daily):
    print("\n========== 恐慌日年度分布 ==========")
    daily['year'] = daily['date'].str[:4]
    thresholds = {
        'signal_c>=10': daily['signal_c_count'] >= 10,
        'signal_c>=15': daily['signal_c_count'] >= 15,
        'signal_c>=20': daily['signal_c_count'] >= 20,
        'signal_c>=30': daily['signal_c_count'] >= 30,
        'drop5pct>=15%': daily['drop5pct_ratio'] >= 15,
        'drop5pct>=20%': daily['drop5pct_ratio'] >= 20,
        'drop5pct>=25%': daily['drop5pct_ratio'] >= 25,
        'idx<=-2%': daily['idx_close_rate'] <= -2,
        'idx<=-3%': daily['idx_close_rate'] <= -3,
    }
    rows = []
    for name, mask in thresholds.items():
        sub = daily[mask].groupby('year').size()
        row = {'method': name}
        total = 0
        for yr in [str(y) for y in range(START_YEAR, END_YEAR + 1)]:
            v = int(sub.get(yr, 0))
            row[yr] = v
            total += v
        row['total'] = total
        rows.append(row)
    df = pd.DataFrame(rows)
    print(df.to_string(index=False))
    print()
    return df

# ==================== 回测 ====================
def precompute_candidates(sk, max_N=3):
    """为每个交易日预计算 top-N 选股结果 (仅依赖于当日数据), 返回 dict[date]->list[code]"""
    out = {}
    sub = sk[sk['signal_c'] & ~sk['is_limit_down']][['date', 'code', 'cumret_3d', 'turn']]
    for d, g in sub.groupby('date', sort=False):
        if len(g) == 0:
            out[d] = []
            continue
        g = g.sort_values('cumret_3d').reset_index(drop=True)
        n = len(g)
        lo = n // 4
        hi = n - n // 4
        mid = g.iloc[lo:hi] if hi > lo else g
        mid = mid.sort_values('turn', ascending=True).head(max_N * 3)
        mid = mid.sort_values('code').head(max_N)
        out[d] = mid['code'].tolist()
    return out

def backtest(daily, panic_method, tp_pct, hold_max, N,
             trading_dates, market, candidates_by_date):
    """
    单次回测. market = (data, code2idx, date2idx, ndates, ncodes)
    data shape: (ncodes, ndates, NCOL); NCOL=9: [preclose, close, h1o, h1h, h2h, h3h, h4h, turn, has_data]
    """
    data, code2idx, date2idx, ndates, ncodes = market
    PRECLOSE, CLOSE, H1O, H1H, H2H, H3H, H4H, TURN, HAS = range(9)

    # 计算panic_day集合
    if panic_method.startswith('signal_c>='):
        K = int(panic_method.split('>=')[1])
        panic_days = set(daily[daily['signal_c_count'] >= K]['date'].tolist())
    elif panic_method.startswith('drop5pct>='):
        P = float(panic_method.split('>=')[1].rstrip('%'))
        panic_days = set(daily[daily['drop5pct_ratio'] >= P]['date'].tolist())
    else:
        raise ValueError(panic_method)

    cash = INIT_CAPITAL
    positions = []  # list of {code_idx, shares, buy_price, buy_date, ti_buy, tp_price}
    trades = []
    nav_curve = []

    for ti, today in enumerate(trading_dates):
        # ===== 阶段A: 强制超时卖出 (在hour1开盘卖) =====
        kept = []
        for pos in positions:
            if pos.get('force_sell_now'):
                ci = pos['code_idx']
                if data[ci, ti, HAS]:
                    sell_price = float(data[ci, ti, H1O])
                    if sell_price > 0:
                        cash += pos['shares'] * sell_price
                        trades.append({
                            'buy_date': pos['buy_date'],
                            'sell_date': today,
                            'code': pos['code'],
                            'buy_price': pos['buy_price'],
                            'sell_price': sell_price,
                            'ret': sell_price / pos['buy_price'] - 1,
                            'exit_type': 'TIMEOUT',
                            'hold_days': ti - pos['ti_buy'],
                        })
                        continue
                # 停牌, 推到下一日
            kept.append(pos)
        positions = kept

        # ===== 阶段B: 检查TP止盈 (T+1当日不可卖) =====
        new_positions = []
        for pos in positions:
            held = ti - pos['ti_buy']
            if held < 1:
                new_positions.append(pos)
                continue
            ci = pos['code_idx']
            if not data[ci, ti, HAS]:
                new_positions.append(pos)
                continue
            tp = pos['tp_price']
            sold = False
            for col in (H1H, H2H, H3H, H4H):
                hi = data[ci, ti, col]
                if hi >= tp and hi > 0:
                    cash += pos['shares'] * tp
                    trades.append({
                        'buy_date': pos['buy_date'],
                        'sell_date': today,
                        'code': pos['code'],
                        'buy_price': pos['buy_price'],
                        'sell_price': tp,
                        'ret': tp / pos['buy_price'] - 1,
                        'exit_type': 'TP',
                        'hold_days': held,
                    })
                    sold = True
                    break
            if sold:
                continue
            if held >= hold_max:
                pos['force_sell_now'] = True
            new_positions.append(pos)
        positions = new_positions

        # ===== 阶段C: 买入 =====
        if ti >= 1:
            yesterday = trading_dates[ti - 1]
            if yesterday in panic_days:
                cand_codes = candidates_by_date.get(yesterday, [])[:N]
                slots_avail = N - len(positions)
                if slots_avail > 0 and cand_codes:
                    picks = cand_codes[:slots_avail]
                    per_slot_cash = cash / slots_avail
                    for code in picks:
                        ci = code2idx.get(code)
                        if ci is None or not data[ci, ti, HAS]:
                            continue
                        h1o = float(data[ci, ti, H1O])
                        if h1o <= 0:
                            continue
                        pc = float(data[ci, ti, PRECLOSE])
                        if pc > 0:
                            open_ratio = round(h1o / pc, 2)
                            up_lim = 1.20 if (code.startswith('sz.30') or code.startswith('sh.688')) else 1.10
                            if open_ratio >= up_lim:
                                continue
                        shares = int(per_slot_cash / h1o / 100) * 100
                        if shares <= 0:
                            continue
                        cost = shares * h1o
                        if cost > cash:
                            continue
                        cash -= cost
                        positions.append({
                            'code': code,
                            'code_idx': ci,
                            'shares': shares,
                            'buy_price': h1o,
                            'buy_date': today,
                            'ti_buy': ti,
                            'tp_price': h1o * (1 + tp_pct),
                        })

        # ===== 阶段D: NAV =====
        mv = cash
        for pos in positions:
            ci = pos['code_idx']
            if data[ci, ti, HAS]:
                cl = float(data[ci, ti, CLOSE])
                mv += pos['shares'] * (cl if cl > 0 else pos['buy_price'])
            else:
                mv += pos['shares'] * pos['buy_price']
        nav_curve.append({'date': today, 'nav': mv})

    return trades, nav_curve

# ==================== 结果统计 ====================
def analyze_result(trades, nav_curve, config):
    if not nav_curve:
        return None
    df_nav = pd.DataFrame(nav_curve)
    df_nav['year'] = df_nav['date'].str[:4]

    # 年度收益
    yearly = {}
    for yr in [str(y) for y in range(START_YEAR, END_YEAR + 1)]:
        sub = df_nav[df_nav['year'] == yr]
        if len(sub) < 2:
            yearly[yr] = 0.0
            continue
        ret = sub['nav'].iloc[-1] / sub['nav'].iloc[0] - 1
        yearly[yr] = ret

    nav_start = df_nav['nav'].iloc[0]
    nav_end = df_nav['nav'].iloc[-1]
    n_years = (END_YEAR - START_YEAR + 1)
    cagr = (nav_end / nav_start) ** (1 / n_years) - 1 if nav_end > 0 else -1.0

    # 最大回撤
    df_nav['peak'] = df_nav['nav'].cummax()
    df_nav['dd'] = df_nav['nav'] / df_nav['peak'] - 1
    max_dd = df_nav['dd'].min()

    # 交易统计
    if trades:
        df_t = pd.DataFrame(trades)
        df_t['year'] = df_t['buy_date'].str[:4]
        yearly_trades = df_t.groupby('year').size().to_dict()
        n_trades = len(df_t)
        win_rate = (df_t['ret'] > 0).sum() / n_trades
        avg_ret = df_t['ret'].mean()
        tp_rate = (df_t['exit_type'] == 'TP').sum() / n_trades
    else:
        yearly_trades = {}
        n_trades = 0
        win_rate = 0
        avg_ret = 0
        tp_rate = 0

    return {
        'config': config,
        'cagr': cagr,
        'max_dd': max_dd,
        'n_trades': n_trades,
        'win_rate': win_rate,
        'avg_ret': avg_ret,
        'tp_rate': tp_rate,
        'yearly': yearly,
        'yearly_trades': yearly_trades,
        'final_nav': nav_end,
    }

def print_top_results(results, top_n=10):
    results = [r for r in results if r is not None]
    results.sort(key=lambda x: x['cagr'], reverse=True)
    print(f"\n========== TOP {top_n} 配置 (按CAGR排序) ==========")
    for i, r in enumerate(results[:top_n], 1):
        cfg = r['config']
        print(f"\n#{i} {cfg}")
        print(f"  CAGR: {r['cagr']*100:.2f}%  最大回撤: {r['max_dd']*100:.2f}%  最终NAV: {r['final_nav']:,.0f}")
        print(f"  总交易: {r['n_trades']}  胜率: {r['win_rate']*100:.1f}%  平均单笔: {r['avg_ret']*100:.2f}%  TP命中率: {r['tp_rate']*100:.1f}%")
        yr_str = "  年度收益: "
        for yr in [str(y) for y in range(START_YEAR, END_YEAR + 1)]:
            v = r['yearly'].get(yr, 0) * 100
            t = r['yearly_trades'].get(yr, 0)
            yr_str += f"{yr}={v:+.1f}%({t}) "
        print(yr_str)

# ==================== 主流程 ====================
def main():
    sk, idx = load_data()
    daily = compute_panic_metrics(sk, idx)
    print_panic_distribution(daily)

    # 仅保留回测期数据
    sk_bt = sk[(sk['date'] >= f'{START_YEAR}-01-01') & (sk['date'] <= f'{END_YEAR}-12-31')].copy()
    trading_dates = sorted(sk_bt['date'].unique().tolist())
    print(f"[{datetime.now():%H:%M:%S}] 回测交易日数: {len(trading_dates)}", flush=True)

    # 参数网格
    panic_methods = [
        'signal_c>=10', 'signal_c>=15', 'signal_c>=20', 'signal_c>=30',
        'drop5pct>=15%', 'drop5pct>=20%', 'drop5pct>=25%',
    ]
    tp_pcts = [0.02, 0.03, 0.04, 0.05]
    hold_maxs = [2, 3, 5, 10]
    Ns = [1, 3]
    configs = list(itertools.product(panic_methods, tp_pcts, hold_maxs, Ns))
    print(f"[{datetime.now():%H:%M:%S}] 总配置数: {len(configs)}", flush=True)

    # 预计算每日候选 (仅依赖N)
    print(f"[{datetime.now():%H:%M:%S}] 预计算每日候选...", flush=True)
    candidates_by_date = precompute_candidates(sk_bt, max_N=max(Ns))
    print(f"[{datetime.now():%H:%M:%S}] 候选预计算完毕", flush=True)

    # 构建紧凑的 numpy 矩阵 (ncodes, ndates, NCOL=9)
    print(f"[{datetime.now():%H:%M:%S}] 构建矩阵索引...", flush=True)
    codes = sorted(sk_bt['code'].unique().tolist())
    code2idx = {c: i for i, c in enumerate(codes)}
    date2idx = {d: i for i, d in enumerate(trading_dates)}
    ncodes = len(codes)
    ndates = len(trading_dates)
    NCOL = 9  # PRECLOSE, CLOSE, H1O, H1H, H2H, H3H, H4H, TURN, HAS
    data = np.zeros((ncodes, ndates, NCOL), dtype=np.float32)
    # 向量化填充
    ci_arr = sk_bt['code'].map(code2idx).values.astype(np.int32)
    di_arr = sk_bt['date'].map(date2idx).values.astype(np.int32)
    cols_fill = [('preclose', 0), ('close', 1), ('hour1_open', 2),
                 ('hour1_high', 3), ('hour2_high', 4), ('hour3_high', 5),
                 ('hour4_high', 6), ('turn', 7)]
    for col, idx in cols_fill:
        vals = sk_bt[col].fillna(0).values.astype(np.float32)
        data[ci_arr, di_arr, idx] = vals
    data[ci_arr, di_arr, 8] = 1.0  # HAS
    print(f"[{datetime.now():%H:%M:%S}] 矩阵构建完毕: shape={data.shape}, mem={data.nbytes/1024/1024:.0f}MB", flush=True)

    # 释放大对象
    del sk, sk_bt
    import gc; gc.collect()

    market = (data, code2idx, date2idx, ndates, ncodes)

    results = []
    for i, (pm, tp, hm, n) in enumerate(configs, 1):
        cfg = {'panic': pm, 'tp': tp, 'hold_max': hm, 'N': n}
        if i % 10 == 0 or i == 1:
            print(f"[{datetime.now():%H:%M:%S}] 进度 {i}/{len(configs)}: {cfg}", flush=True)
        trades, nav_curve = backtest(
            daily, pm, tp, hm, n,
            trading_dates, market, candidates_by_date
        )
        r = analyze_result(trades, nav_curve, cfg)
        if r:
            results.append(r)

    print_top_results(results, top_n=10)

    # 输出全表
    print("\n========== 全部配置结果 ==========")
    rows = []
    for r in results:
        c = r['config']
        rows.append({
            'panic': c['panic'], 'tp': c['tp'], 'hold_max': c['hold_max'], 'N': c['N'],
            'CAGR%': round(r['cagr']*100, 2),
            'MaxDD%': round(r['max_dd']*100, 2),
            'Trades': r['n_trades'],
            'Win%': round(r['win_rate']*100, 1),
            'AvgRet%': round(r['avg_ret']*100, 2),
            'TP%': round(r['tp_rate']*100, 1),
        })
    full = pd.DataFrame(rows).sort_values('CAGR%', ascending=False)
    print(full.to_string(index=False))

if __name__ == '__main__':
    main()
