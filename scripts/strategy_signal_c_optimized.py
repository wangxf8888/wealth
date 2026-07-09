#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Signal C (连续3日下跌均值回归) 过滤优化 + 完整N=3回测

基础信号:
  - T-2,T-1,T 连续3天 close 递减
  - (T_close / T-2_close - 1) <= -0.08
  - T+1 hour1_open 买, T+2 hour1_open 卖
  - 排除 ST、北交所(bj.)

Phase 1: 过滤条件单因子扫描
Phase 2: 选最优 Top3 组合
Phase 3: 完整 N=3 回测 (2020-2025)
Phase 4: 若 Phase3 CAGR>50%，执行 N=1 仓位集中测试
"""
import sqlite3
import sys
import time
import math
import warnings
warnings.filterwarnings('ignore')

import pandas as pd
import numpy as np

DB = '/home/AIWealth/data/stocks.db'
START = '2019-11-01'   # 留出20日窗口
END   = '2026-01-01'

# ============================================================
# 数据加载
# ============================================================
def load_data():
    print(f"[Load] reading stock_kline {START}~{END} ...", flush=True)
    t0 = time.time()
    conn = sqlite3.connect(DB)
    q = f"""
    SELECT date, code, preclose, open, close, low, high,
           volume, turn,
           hour1_open, hour4_close, hour4_low,
           isST
    FROM stock_kline
    WHERE date >= '{START}' AND date < '{END}'
      AND code NOT LIKE 'bj.%'
      AND (isST IS NULL OR isST = 0)
    """
    df = pd.read_sql(q, conn)
    conn.close()
    df = df.sort_values(['code','date']).reset_index(drop=True)
    print(f"[Load] rows={len(df):,}  codes={df['code'].nunique():,}  elapsed={time.time()-t0:.1f}s", flush=True)
    return df


# ============================================================
# 信号构造
# ============================================================
def build_signals(df):
    print("[Signal] building features...", flush=True)
    t0 = time.time()
    g = df.groupby('code', sort=False)

    df['close_t1']   = g['close'].shift(1)   # T-1 close
    df['close_t2']   = g['close'].shift(2)   # T-2 close
    df['volume_t1']  = g['volume'].shift(1)
    df['close_t10']  = g['close'].shift(10)
    df['close_t20']  = g['close'].shift(20)

    # T+1 买入价
    df['next_h1_open'] = g['hour1_open'].shift(-1)
    df['next_date']    = g['date'].shift(-1)
    df['next_close']   = g['close'].shift(-1)
    # T+2 卖出价
    df['nn_h1_open']   = g['hour1_open'].shift(-2)
    df['nn_date']      = g['date'].shift(-2)

    # 基础信号筛选
    cond_dec  = (df['close_t2'] > df['close_t1']) & (df['close_t1'] > df['close'])
    cum_ret_3d = df['close']/df['close_t2'] - 1
    cond_drop = cum_ret_3d <= -0.08
    cond_trade = (df['next_h1_open'].notna() & df['nn_h1_open'].notna()
                  & (df['next_h1_open'] > 0) & (df['nn_h1_open'] > 0)
                  & df['next_close'].notna())

    sig = df[cond_dec & cond_drop & cond_trade].copy()
    sig['cum_ret_3d'] = sig['close']/sig['close_t2'] - 1
    sig['trade_ret']  = sig['nn_h1_open']/sig['next_h1_open'] - 1
    sig['year']       = sig['date'].str[:4]
    sig['lim_ratio']  = (sig['close']/sig['preclose']).round(2)

    print(f"[Signal] base N={len(sig):,}  elapsed={time.time()-t0:.1f}s", flush=True)
    return sig


# ============================================================
# 统计工具
# ============================================================
def summarize(sub):
    if len(sub) == 0:
        return 0, np.nan, np.nan, 0, 0
    n = len(sub)
    mean = sub['trade_ret'].mean() * 100
    win  = (sub['trade_ret'] > 0).mean() * 100
    by_year = sub.groupby('year')['trade_ret'].mean() * 100
    pos_years = int((by_year > 0).sum())
    total_years = int(by_year.shape[0])
    return n, mean, win, pos_years, total_years


def print_row(label, sub, base_mean=None):
    n, mean, win, py, ty = summarize(sub)
    if n == 0:
        print(f"  {label:42s} N=0", flush=True)
        return None
    delta_str = ""
    if base_mean is not None:
        delta_str = f"  Δ={mean-base_mean:+.3f}pp"
    print(f"  {label:42s} N={n:>7d}  mean={mean:+.3f}%  win={win:5.2f}%  yrs={py}/{ty}{delta_str}",
          flush=True)
    return {'label': label, 'n': n, 'mean': mean, 'win': win,
            'pos_years': py, 'total_years': ty}


def yearly_breakdown(sub, label):
    by = sub.groupby('year').agg(
        n=('trade_ret','count'),
        mean=('trade_ret', lambda x: x.mean()*100),
        win=('trade_ret', lambda x: (x>0).mean()*100),
    )
    print(f"\n  {label} 逐年:")
    for y, row in by.iterrows():
        print(f"    {y}  N={int(row['n']):>6d}  mean={row['mean']:+.3f}%  win={row['win']:5.2f}%",
              flush=True)


# ============================================================
# Phase 1: 过滤条件扫描
# ============================================================
def phase1_filter_scan(sig):
    print("\n" + "="*78)
    print("PHASE 1  单因子过滤扫描 (vs BASE 信号)")
    print("="*78)
    base = print_row("BASE  3day-decline + cum<=-8%", sig, None)
    base_mean = base['mean'] if base else None

    results = []
    def add(label, sub):
        r = print_row(label, sub, base_mean)
        if r:
            r['sub'] = sub
            results.append(r)

    print("\n-- 1) 换手率过滤 --")
    for t in [3, 5, 8, 10]:
        add(f"turn > {t}",         sig[sig['turn'] > t])

    print("\n-- 2) 跌幅深度 --")
    for d in [-0.10, -0.12, -0.15]:
        add(f"cum_ret_3d <= {d:.2f}", sig[sig['cum_ret_3d'] <= d])

    print("\n-- 3) T日企稳 --")
    add("close > low (not low-of-day)",       sig[sig['close'] > sig['low']])
    add("hour4_close > hour4_low",            sig[sig['hour4_close'] > sig['hour4_low']])

    print("\n-- 4) 非跌停 --")
    add("round(c/pc) > 0.90 (非跌停)",         sig[sig['lim_ratio'] > 0.90])
    add("round(c/pc) > 0.80",                 sig[sig['lim_ratio'] > 0.80])

    print("\n-- 5) 缩量 --")
    add("volume < volume_t1 (缩量)",           sig[sig['volume'] < sig['volume_t1']])

    print("\n-- 6) 放量恐慌 --")
    add("volume > 1.5*volume_t1 (放量)",       sig[sig['volume'] > sig['volume_t1']*1.5])

    print("\n-- 7) 中期趋势向上 --")
    add("close_t10 > close_t20",              sig[sig['close_t10'] > sig['close_t20']])

    print("\n-- 8) 价格区间 --")
    add("close > 5 (排除低价股)",              sig[sig['close'] > 5])

    return base_mean, results


# ============================================================
# Phase 2: 最优组合 (按维度分类做正交组合)
# ============================================================
def phase2_best_combo(sig, base_mean, results):
    print("\n" + "="*78)
    print("PHASE 2  跨维度正交组合优选")
    print("="*78)

    # ----- 定义维度过滤函数 (mask) -----
    dim_filters = {
        'depth': [
            ('cum<=-0.10', sig['cum_ret_3d'] <= -0.10),
            ('cum<=-0.12', sig['cum_ret_3d'] <= -0.12),
            ('cum<=-0.15', sig['cum_ret_3d'] <= -0.15),
        ],
        'turn': [
            ('turn<3',    sig['turn'] < 3),
            ('turn<5',    sig['turn'] < 5),
            ('3<turn<8',  (sig['turn'] >= 3) & (sig['turn'] < 8)),
            ('turn>5',    sig['turn'] > 5),
        ],
        'limit': [
            ('lim>0.90',  sig['lim_ratio'] > 0.90),
            ('lim>0.95',  sig['lim_ratio'] > 0.95),
        ],
        'vol': [
            ('vol<vol_t1', sig['volume'] < sig['volume_t1']),
            ('vol>1.5x',   sig['volume'] > sig['volume_t1'] * 1.5),
        ],
        'trend': [
            ('t10>t20',   sig['close_t10'] > sig['close_t20']),
        ],
        'price': [
            ('close>5',   sig['close'] > 5),
            ('close>3',   sig['close'] > 3),
        ],
        'stable': [
            ('h4c>h4low', sig['hour4_close'] > sig['hour4_low']),
        ],
    }

    # 列出每个维度内表现
    print("\n-- 每维度内单因子表现 --")
    best_in_dim = {}
    for dim, opts in dim_filters.items():
        print(f"  [{dim}]")
        scored = []
        for name, mask in opts:
            sub = sig[mask]
            n, mean, win, py, ty = summarize(sub)
            if n < 1000:
                continue
            print(f"    {name:18s}  N={n:>7d}  mean={mean:+.3f}%  win={win:5.2f}%  yrs={py}/{ty}")
            scored.append((mean, name, mask, n, win, py, ty))
        if scored:
            scored.sort(reverse=True)
            top_mean, top_name, top_mask, *_ = scored[0]
            if top_mean > base_mean:
                best_in_dim[dim] = (top_name, top_mask, top_mean)
                print(f"    -> best: {top_name}  +{top_mean-base_mean:.3f}pp")

    # ----- 测试 depth 与其它维度的两两组合 (depth 是最有信号的) -----
    if 'depth' not in best_in_dim:
        print("\n[Phase2] depth 维度无提升, 退化")
        return sig, "BASE"

    depth_name, depth_mask, _ = best_in_dim['depth']
    print(f"\n-- depth ({depth_name}) × 其它维度 两两组合 --")

    combos = [{
        'labels': [depth_name], 'mask': depth_mask, 'n_dim': 1,
    }]
    # depth + 1 其它维度 (尝试该维度所有选项)
    for dim, opts in dim_filters.items():
        if dim == 'depth':
            continue
        for name, mask in opts:
            m = depth_mask & mask
            sub = sig[m]
            n, mean, win, py, ty = summarize(sub)
            if n < 1500:
                continue
            print(f"    depth & {name:18s}  N={n:>6d}  mean={mean:+.3f}%  win={win:5.2f}%  yrs={py}/{ty}")
            combos.append({'labels': [depth_name, name], 'mask': m, 'n_dim': 2})

    # depth + 2 其它维度 (best_in_dim 中各维度最优 互相搭配)
    print(f"\n-- depth + 其它2个维度的最优单因子 --")
    other_dims = [d for d in best_in_dim if d != 'depth']
    from itertools import combinations
    for d1, d2 in combinations(other_dims, 2):
        n1, m1, _ = best_in_dim[d1]
        n2, m2, _ = best_in_dim[d2]
        m = depth_mask & m1 & m2
        sub = sig[m]
        n, mean, win, py, ty = summarize(sub)
        if n < 800:
            continue
        print(f"    depth & {n1} & {n2:18s}  N={n:>6d}  mean={mean:+.3f}%  win={win:5.2f}%  yrs={py}/{ty}")
        combos.append({'labels': [depth_name, n1, n2], 'mask': m, 'n_dim': 3})

    # ----- 评分 + 选最优 -----
    scored = []
    for c in combos:
        sub = sig[c['mask']]
        n, mean, win, py, ty = summarize(sub)
        c.update({'sub': sub, 'n': n, 'mean': mean, 'win': win,
                  'pos_years': py, 'total_years': ty})
        # 综合评分: mean * sqrt(n) * (年正占比)
        score = mean * math.sqrt(min(n, 30000)) * (py/max(ty,1))
        scored.append((score, c))
    scored.sort(key=lambda x: x[0], reverse=True)

    print("\n-- 组合综合评分 Top-10 (score = mean * sqrt(min(N,30k)) * pos_yr_ratio) --")
    for sc, c in scored[:10]:
        print(f"  score={sc:7.2f}  N={c['n']:>6d}  mean={c['mean']:+.3f}%  win={c['win']:5.2f}%  "
              f"yrs={c['pos_years']}/{c['total_years']}  | {' & '.join(c['labels'])}")

    # 优先满足: mean>0.6, win>=53, yrs>=5, N>=4000
    qualified = [c for _, c in scored
                 if c['mean'] > 0.6 and c['win'] >= 53
                 and c['pos_years'] >= 5 and c['n'] >= 4000]
    if not qualified:
        print("\n(放宽: mean>0.5, win>=52, yrs>=5, N>=3000)")
        qualified = [c for _, c in scored
                     if c['mean'] > 0.5 and c['win'] >= 52
                     and c['pos_years'] >= 5 and c['n'] >= 3000]
    if not qualified:
        print("\n(继续放宽: 取综合评分 Top1)")
        qualified = [scored[0][1]]

    qualified.sort(key=lambda x: x['mean'], reverse=True)
    best = qualified[0]
    label = ' & '.join(best['labels'])
    print(f"\n[Phase2] 选定组合: {label}")
    print(f"  N={best['n']}  mean={best['mean']:+.3f}%  win={best['win']:.2f}%  "
          f"yrs={best['pos_years']}/{best['total_years']}")

    yearly_breakdown(best['sub'], "Best Combo")
    return best['sub'], label


# ============================================================
# 选股排序诊断: 为什么 Phase 1 均值与 Phase 3 实际交易不一致?
# ============================================================
def diagnose_ranking(sig_filtered):
    """在同一个 signal day, 按 cum_ret_3d 排序, 分档看 trade_ret"""
    print("\n-- 诊断: 同日候选中按 cum_ret_3d 排名的 trade_ret 分布 --")
    s = sig_filtered.copy()
    # 全集上的统计
    print(f"  全集 N={len(s):,}  mean={s['trade_ret'].mean()*100:+.3f}%  "
          f"win={(s['trade_ret']>0).mean()*100:.2f}%")
    s['n_in_day'] = s.groupby('date')['cum_ret_3d'].transform('count')

    # 各 n_in_day 分组表现
    print("  -- 按当日候选数分组 --")
    for ncat, cond in [('1只', s['n_in_day']==1),
                       ('2只', s['n_in_day']==2),
                       ('3-5只', (s['n_in_day']>=3) & (s['n_in_day']<=5)),
                       ('6-10只', (s['n_in_day']>=6) & (s['n_in_day']<=10)),
                       ('11-20只', (s['n_in_day']>=11) & (s['n_in_day']<=20)),
                       ('>20只', s['n_in_day']>20)]:
        sub = s[cond]
        if len(sub) == 0:
            continue
        print(f"    n_in_day={ncat:8s}  N={len(sub):>6d}  "
              f"mean={sub['trade_ret'].mean()*100:+.3f}%  "
              f"win={(sub['trade_ret']>0).mean()*100:.2f}%  "
              f"占比 {len(sub)/len(s)*100:.1f}%")

    # 多候选日上的排名表现
    multi = s[s['n_in_day'] >= 3].copy()
    if len(multi) > 0:
        multi['rank_deep'] = multi.groupby('date')['cum_ret_3d'].rank(method='first', ascending=True)
        multi['rank_shal'] = multi.groupby('date')['cum_ret_3d'].rank(method='first', ascending=False)
        multi['med_dist'] = (multi['cum_ret_3d']
                             - multi.groupby('date')['cum_ret_3d'].transform('median')).abs()
        multi['med_rank'] = multi.groupby('date')['med_dist'].rank(method='first', ascending=True)
        print("  -- 多候选日 (n>=3) 排名分档 --")
        for label, mask in [
            ('rank_deep<=3 (最深3)',  multi['rank_deep']<=3),
            ('rank_shal<=3 (最浅3)',  multi['rank_shal']<=3),
            ('med_rank<=3 (中位3)',   multi['med_rank']<=3),
            ('30-70% 区间',          (multi['rank_deep']>multi['n_in_day']*0.3)
                                       & (multi['rank_deep']<=multi['n_in_day']*0.7)),
        ]:
            sub = multi[mask]
            print(f"    {label:22s}  N={len(sub):>6d}  "
                  f"mean={sub['trade_ret'].mean()*100:+.3f}%  "
                  f"win={(sub['trade_ret']>0).mean()*100:.2f}%")


# ============================================================
# Phase 3: N=N 回测 (多种排序策略对比)
# ============================================================
def backtest(sig_filtered, df_full, N=3, init_cash=1_000_000,
             label="N=3", rank_by='deepest', min_panic_n=0):
    print("\n" + "="*78)
    print(f"PHASE 3  完整回测  {label}  rank_by={rank_by}  panic_n>={min_panic_n}  (init={init_cash:,})")
    print("="*78)

    # 仅 2020-2025 范围
    sigs = sig_filtered[(sig_filtered['date'] >= '2020-01-01')
                        & (sig_filtered['date']  < '2026-01-01')].copy()

    # “恐慌日”过滤: 仅在 T 日候选数 >= min_panic_n 的交易日才买入
    if min_panic_n > 0:
        cnt_per_day = sigs.groupby('date')['code'].transform('count')
        sigs = sigs[cnt_per_day >= min_panic_n].copy()
        print(f"[BT] after panic-day filter (n>={min_panic_n}): signals={len(sigs):,}, days={sigs['date'].nunique()}")

    # 根据 rank_by 计算排序键
    if rank_by == 'deepest':
        sigs['rank_key'] = sigs['cum_ret_3d']           # asc 取头 = 最负
        ascending = True
    elif rank_by == 'shallowest':
        sigs['rank_key'] = sigs['cum_ret_3d']
        ascending = False
    elif rank_by == 'low_turn':
        sigs['rank_key'] = sigs['turn'].fillna(99)
        ascending = True
    elif rank_by == 'high_turn':
        sigs['rank_key'] = sigs['turn'].fillna(-1)
        ascending = False
    elif rank_by == 'random':
        rng = np.random.default_rng(42)
        sigs['rank_key'] = rng.random(len(sigs))
        ascending = True
    elif rank_by == 'middle':
        # 取每日 cum_ret_3d 中位附近的 (距中位越近越优先)
        med = sigs.groupby('date')['cum_ret_3d'].transform('median')
        sigs['rank_key'] = (sigs['cum_ret_3d'] - med).abs()
        ascending = True
    elif rank_by == 'middle_high_turn':
        # 中位附近 + 高换手优先
        med = sigs.groupby('date')['cum_ret_3d'].transform('median')
        sigs['rank_key'] = (sigs['cum_ret_3d'] - med).abs() / (sigs['turn'].fillna(0.5) + 0.5)
        ascending = True
    else:
        sigs['rank_key'] = sigs['cum_ret_3d']
        ascending = True

    sigs = sigs.sort_values(['date', 'rank_key'], ascending=[True, ascending]).reset_index(drop=True)
    print(f"[BT] signals total in window: {len(sigs):,}", flush=True)

    sig_by_buy = {d: g for d, g in sigs.groupby('next_date')}

    all_dates = sorted(set(sigs['next_date'].dropna().unique())
                       | set(sigs['nn_date'].dropna().unique()))
    print(f"[BT] trading days: {len(all_dates)}", flush=True)

    cash = float(init_cash)
    holdings = []   # list of dicts
    trades = []
    equity_curve = []

    for d in all_dates:
        # 1) 卖出 sell_date == d 的持仓
        new_holdings = []
        for h in holdings:
            if h['sell_date'] == d:
                proceeds = h['shares'] * h['sell_price']
                cash += proceeds
                ret = h['sell_price'] / h['buy_price'] - 1
                trades.append({
                    'buy_date': h['buy_date'], 'sell_date': d,
                    'code': h['code'], 'shares': h['shares'],
                    'buy_price': h['buy_price'], 'sell_price': h['sell_price'],
                    'ret': ret, 'pnl': proceeds - h['shares']*h['buy_price'],
                })
            else:
                new_holdings.append(h)
        holdings = new_holdings

        # 2) 买入: 选当日候选中跌最深的 N 个 (按 cum_ret_3d 升序), 减去已持仓数
        slots_avail = N - len(holdings)
        if slots_avail > 0 and d in sig_by_buy:
            cands = sig_by_buy[d].sort_values('rank_key', ascending=ascending).head(slots_avail)
            n_buy = len(cands)
            if n_buy > 0:
                per_slot = cash / slots_avail   # 等额拆分可用现金
                for _, row in cands.iterrows():
                    bp = row['next_h1_open']
                    sp = row['nn_h1_open']
                    if not (bp and sp) or pd.isna(bp) or pd.isna(sp) or bp <= 0:
                        continue
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
                        'buy_date': d, 'sell_date': row['nn_date'],
                        'code': row['code'], 'shares': shares,
                        'buy_price': bp, 'sell_price': sp,
                        'next_close': row['next_close'],
                    })

        # 3) 当日 MTM: cash + Σ shares*next_close (买入当日收盘价)
        mtm = cash + sum(h['shares'] * (h['next_close'] if h['buy_date']==d
                                         else h['sell_price'])
                          for h in holdings)
        equity_curve.append((d, mtm))

    final_value = cash + sum(h['shares']*h['sell_price'] for h in holdings)

    # ============= 指标 =============
    eq = pd.DataFrame(equity_curve, columns=['date','equity'])
    eq['year'] = eq['date'].str[:4]
    # 每年首日/末日
    yearly_ret = []
    for y, sub in eq.groupby('year'):
        first = sub.iloc[0]['equity']
        last  = sub.iloc[-1]['equity']
        yearly_ret.append((y, first, last, (last/first - 1) * 100))

    # 总CAGR
    if len(eq) > 0:
        first_eq = eq.iloc[0]['equity']
        last_eq  = eq.iloc[-1]['equity']
        years = (pd.to_datetime(eq.iloc[-1]['date'])
                 - pd.to_datetime(eq.iloc[0]['date'])).days / 365.25
        cagr = (last_eq / first_eq) ** (1/years) - 1 if years > 0 else 0
    else:
        first_eq = init_cash; last_eq = init_cash; cagr = 0; years = 0

    # 最大回撤
    eq['peak'] = eq['equity'].cummax()
    eq['dd']   = eq['equity'] / eq['peak'] - 1
    mdd = eq['dd'].min() * 100

    # 交易统计
    tr = pd.DataFrame(trades)
    if len(tr) > 0:
        win_rate = (tr['ret'] > 0).mean() * 100
        avg_ret  = tr['ret'].mean() * 100
    else:
        win_rate = 0; avg_ret = 0

    print(f"\n[Result] {label}")
    print(f"  起始资金: {init_cash:>12,.0f}")
    print(f"  终值:     {last_eq:>12,.2f}")
    print(f"  总收益:   {(last_eq/init_cash - 1)*100:+.2f}%")
    print(f"  CAGR:     {cagr*100:+.2f}%   持续 {years:.2f} 年")
    print(f"  最大回撤: {mdd:.2f}%")
    print(f"  交易笔数: {len(tr):,}")
    print(f"  单笔均值: {avg_ret:+.3f}%   胜率: {win_rate:.2f}%")
    print(f"\n  逐年收益率:")
    for y, fe, le, ret in yearly_ret:
        print(f"    {y}  {fe:>12,.0f} -> {le:>12,.0f}   {ret:+.2f}%")

    return {
        'cagr': cagr*100, 'final': last_eq, 'mdd': mdd,
        'trades': len(tr), 'win': win_rate, 'avg_ret': avg_ret,
        'yearly': yearly_ret, 'equity': eq,
    }


# ============================================================
# main
# ============================================================
def main():
    t0 = time.time()
    df = load_data()
    sig = build_signals(df)

    base_mean, results = phase1_filter_scan(sig)
    best_sub, best_label = phase2_best_combo(sig, base_mean, results)

    print(f"\n[Filtered] selected combo total signals = {len(best_sub):,}")

    # 诊断候选排序与 trade_ret 关系
    diagnose_ranking(best_sub)

    # Phase 3: 多种排序策略对比
    print("\n" + "#"*78)
    print("# Phase 3a: 不加恐慌日过滤 - 多排序策略对比 (N=3)")
    print("#"*78)
    summary_rows = []
    for rb in ['middle', 'middle_high_turn', 'high_turn', 'deepest']:
        r = backtest(best_sub, df, N=3, init_cash=1_000_000,
                     label=f"N=3 [{rb}]", rank_by=rb, min_panic_n=0)
        summary_rows.append((f"{rb}_no_panic", r))

    print("\n" + "#"*78)
    print("# Phase 3b: 加入恐慌日过滤 (仅在候选>=阈值的日交易)")
    print("#"*78)
    for panic_n in [11, 20]:
        for rb in ['middle', 'middle_high_turn', 'high_turn']:
            r = backtest(best_sub, df, N=3, init_cash=1_000_000,
                         label=f"N=3 [{rb}, panic>={panic_n}]",
                         rank_by=rb, min_panic_n=panic_n)
            summary_rows.append((f"{rb}_panic{panic_n}", r))

    print("\n" + "="*78)
    print("★ 多策略汇总 (N=3, 2020-2025)")
    print("="*78)
    print(f"  {'strategy':30s} {'CAGR':>8s} {'final':>14s} {'MDD':>8s} {'trades':>8s} {'avg':>8s} {'win':>7s}")
    for rb, r in summary_rows:
        print(f"  {rb:30s} {r['cagr']:+7.2f}% {r['final']:>14,.0f} {r['mdd']:7.2f}% "
              f"{r['trades']:>8d} {r['avg_ret']:+7.3f}% {r['win']:6.2f}%")

    # 选出 CAGR 最高的策略
    best_strat = max(summary_rows, key=lambda x: x[1]['cagr'])
    res3 = best_strat[1]
    print(f"\n★ 最优策略: {best_strat[0]}  CAGR={res3['cagr']:+.2f}%")

    # Phase 4: 仓位集中与 panic 阈值调优
    print("\n" + "="*78)
    print("PHASE 4  仓位集中 (N=1) + panic 阈值网格")
    print("="*78)
    n1_rows = []
    for panic_n in [8, 11, 15, 20, 25]:
        for rb in ['high_turn', 'middle_high_turn', 'middle']:
            r = backtest(best_sub, df, N=1, init_cash=1_000_000,
                         label=f"N=1 [{rb}, panic>={panic_n}]",
                         rank_by=rb, min_panic_n=panic_n)
            n1_rows.append((f"N=1_{rb}_panic{panic_n}", r))

    print("\n" + "="*78)
    print("★ N=1 仓位集中汇总")
    print("="*78)
    print(f"  {'strategy':30s} {'CAGR':>8s} {'final':>14s} {'MDD':>8s} {'trades':>8s} {'avg':>8s} {'win':>7s}")
    for rb, r in n1_rows:
        print(f"  {rb:30s} {r['cagr']:+7.2f}% {r['final']:>14,.0f} {r['mdd']:7.2f}% "
              f"{r['trades']:>8d} {r['avg_ret']:+7.3f}% {r['win']:6.2f}%")

    best_n1 = max(n1_rows, key=lambda x: x[1]['cagr'])
    print(f"\n★ N=1 最优: {best_n1[0]}  CAGR={best_n1[1]['cagr']:+.2f}%")

    print(f"\n[Done] total elapsed = {time.time()-t0:.1f}s")


if __name__ == '__main__':
    main()
