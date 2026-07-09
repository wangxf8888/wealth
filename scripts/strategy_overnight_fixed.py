#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Task #31: O2 隔夜策略合规修复重测

【Bug 确认】
原 scripts/strategy_overnight_effect.py 的 O2 signal 同时使用了
  - hour4_close > hour4_open  (15:00 才知道)
  - close_rate > 0            (15:00 全天收盘才知道)
  - turn > 3.0                (全天换手率, 15:00 才知道)
但买入价用的是 T 日 hour4_open (14:00 价格)。
=> 典型的 lookahead bias: "先看到 15:00 的结果, 再回到 14:00 下单"。
=> 该 +322% CAGR 不可信。

【修复 A: 前3小时信号 + hour4_open 买入 (合规)】
  信号 (13:00 / hour3 收盘已知, 不依赖 hour4 任何字段):
    - hour3_close > hour1_open          (前 3 小时累计上涨)
    - hour3_close > open * 1.02         (强度: 前 3h 涨幅 >= 2%)
  买入: T 日 hour4_open (14:00)
  卖出: T+1 日 hour1_open
  收益: next_hour1_open / hour4_open - 1
  T+1 合规: T 日 14:00 买入, T+1 日 9:30-10:30 卖出 -> 不同自然日 ✓

【修复 B: 全天信号 + T+1 hour1_open 买入 (严格合规)】
  信号 (T 日 15:00 收盘后已知):
    - hour4_close > hour4_open  (尾盘 1h 上涨)
    - close_rate > 0            (全天收红)
    - turn > 3                  (换手 >3%)
  买入: T+1 日 hour1_open
  卖出: T+2 日 hour1_open
  收益: next2_hour1_open / next_hour1_open - 1
  T+1 合规: T+1 日 9:30 买入, 最早 T+2 日 9:30 卖出 -> 不同自然日 ✓

【数据】
  /home/AIWealth/data/stocks.db -> stock_kline
  排除: ST (isST=1), 北交所 (code LIKE 'bj.%')

【回测】
  N=3, 初始 100 万, seed=42
  逐年 N(候选数), mean(平均收益), win_rate
  6 年 CAGR
  若 CAGR > 50%, 加测 N=1
"""
import sqlite3
import numpy as np
import pandas as pd
import random
from collections import defaultdict

DB_PATH = '/home/AIWealth/data/stocks.db'
START_DATE = '2020-01-01'
END_DATE = '2025-12-31'
INITIAL_CAPITAL = 1_000_000.0
SEED = 42


# ==================== 数据加载 ====================
def load_data():
    print(f"[LOAD] 连接 {DB_PATH}")
    conn = sqlite3.connect(DB_PATH)
    sql = """
    SELECT date, code, preclose, open, close, close_rate, turn,
           hour1_open, hour3_close, hour4_open, hour4_close
    FROM stock_kline
    WHERE date BETWEEN ? AND ?
      AND (isST IS NULL OR isST = 0)
      AND (code LIKE 'sh.%' OR code LIKE 'sz.%')
    """
    df = pd.read_sql(sql, conn, params=(START_DATE, END_DATE))
    conn.close()
    print(f"[LOAD] {len(df):,} 行, 股票 {df['code'].nunique():,}, "
          f"日期 {df['date'].min()} ~ {df['date'].max()}")
    for c in ['preclose', 'open', 'close', 'close_rate', 'turn',
              'hour1_open', 'hour3_close', 'hour4_open', 'hour4_close']:
        df[c] = df[c].astype(np.float32)
    return df


def add_derived(df):
    """加 next/next2 hour1_open"""
    df = df.sort_values(['code', 'date']).reset_index(drop=True)
    g = df.groupby('code', sort=False)
    df['next_hour1_open'] = g['hour1_open'].shift(-1)
    df['next2_hour1_open'] = g['hour1_open'].shift(-2)
    df['year'] = df['date'].str.slice(0, 4)
    return df


# ==================== Signal 定义 ====================
def signal_fixA(df):
    """修复 A: 前3小时信号 (hour3 已知), hour4_open 买入, T+1 h1_open 卖出"""
    base = (
        df['hour3_close'].notna() & df['hour1_open'].notna() &
        df['hour4_open'].notna() & df['next_hour1_open'].notna() &
        (df['open'] > 0)
    )
    cond = (
        (df['hour3_close'] > df['hour1_open']) &
        (df['hour3_close'] > df['open'] * 1.02)
    )
    sub = df.loc[base & cond].copy()
    sub['ret'] = sub['next_hour1_open'] / sub['hour4_open'] - 1.0
    return sub


def signal_fixB(df):
    """修复 B: 全天信号, T+1 h1_open 买入, T+2 h1_open 卖出"""
    base = (
        df['hour4_open'].notna() & df['hour4_close'].notna() &
        df['next_hour1_open'].notna() & df['next2_hour1_open'].notna() &
        df['close_rate'].notna() & df['turn'].notna()
    )
    cond = (
        (df['hour4_close'] > df['hour4_open']) &
        (df['close_rate'] > 0) &
        (df['turn'] > 3.0)
    )
    sub = df.loc[base & cond].copy()
    sub['ret'] = sub['next2_hour1_open'] / sub['next_hour1_open'] - 1.0
    return sub


# ==================== 候选统计 ====================
def signal_stats(name, sub):
    print("\n" + "-" * 80)
    print(f"[{name}] 候选样本统计")
    print("-" * 80)
    if len(sub) == 0:
        print("  无候选")
        return
    # 极端值过滤 (除权或异常)
    n0 = len(sub)
    sub = sub.loc[sub['ret'].abs() <= 0.5].copy()
    if n0 != len(sub):
        print(f"  剔除 {n0 - len(sub):,} 条 |ret|>50% 异常 (除权可能)")
    s = sub['ret']
    print(f"  全样本: N={len(s):,}  mean={s.mean()*100:+.4f}%  "
          f"median={s.median()*100:+.4f}%  win_rate={(s>0).mean()*100:.2f}%")
    print(f"  按年份:")
    print(f"    {'YEAR':<6}{'N':>10}{'MEAN':>12}{'WIN_RATE':>12}{'AVG_DAILY':>12}")
    for y, gg in sub.groupby('year'):
        rs = gg['ret']
        nd = gg['date'].nunique()
        avg = len(gg) / nd if nd > 0 else 0
        print(f"    {y:<6}{len(gg):>10,}{rs.mean()*100:>11.4f}%"
              f"{(rs>0).mean()*100:>11.2f}%{avg:>12.2f}")
    return sub


# ==================== 回测 ====================
def backtest(name, sub, n_pos, capital=INITIAL_CAPITAL, seed=SEED):
    print("\n" + "=" * 80)
    print(f"[BACKTEST {name}] N={n_pos}  init={capital:,.0f}  seed={seed}")
    print("=" * 80)
    if len(sub) == 0:
        print("  无候选, 跳过")
        return None
    rng = random.Random(seed)
    cap = capital
    yearly = defaultdict(lambda: {'start': None, 'end': None,
                                   'days': 0, 'picks': 0, 'wins': 0})
    daily = []
    by_date = sub.sort_values('date').groupby('date')
    for date, g in by_date:
        cands = list(zip(g['code'].tolist(), g['ret'].tolist()))
        cands = [(c, r) for c, r in cands if r == r]
        if not cands:
            continue
        if len(cands) <= n_pos:
            picks = cands
        else:
            picks = rng.sample(cands, n_pos)
        # 等权: 每只 cap/n_pos, 未填满仓位空仓收益 0
        ret = float(np.mean([r for _, r in picks]))
        if len(picks) < n_pos:
            ret = ret * len(picks) / n_pos
        cap_new = cap * (1 + ret)
        y = date[:4]
        yr = yearly[y]
        if yr['start'] is None:
            yr['start'] = cap
        yr['end'] = cap_new
        yr['days'] += 1
        yr['picks'] += len(picks)
        if ret > 0:
            yr['wins'] += 1
        daily.append((date, ret, cap_new))
        cap = cap_new

    print(f"  {'YEAR':<6}{'DAYS':>8}{'PICKS':>10}{'YEAR_RET':>12}"
          f"{'DAY_WIN':>10}{'CAPITAL_END':>16}")
    for y in sorted(yearly.keys()):
        yr = yearly[y]
        ret_y = yr['end'] / yr['start'] - 1.0 if yr['start'] else 0
        wr = yr['wins'] / yr['days'] * 100 if yr['days'] else 0
        print(f"  {y:<6}{yr['days']:>8}{yr['picks']:>10}"
              f"{ret_y*100:>11.2f}%{wr:>9.2f}%{yr['end']:>16,.0f}")
    if daily:
        total_ret = cap / capital - 1.0
        n_y = len(yearly)
        cagr = (cap / capital) ** (1.0 / n_y) - 1.0 if n_y > 0 else 0
        wins = sum(1 for _, r, _ in daily if r > 0)
        print(f"  ----")
        print(f"  TOTAL: end={cap:,.0f}  total_ret={total_ret*100:+.2f}%  "
              f"CAGR={cagr*100:+.2f}%  日胜率={wins/len(daily)*100:.2f}% "
              f"(交易日={len(daily)})")
        return cagr
    return None


def main():
    df = load_data()
    df = add_derived(df)

    print("\n" + "#" * 80)
    print("# 修复 A: 前3小时信号 + hour4_open 买入 + T+1 h1_open 卖出")
    print("#" * 80)
    sub_a = signal_fixA(df)
    sub_a = signal_stats('FIX_A', sub_a)
    cagr_a_n3 = backtest('FIX_A', sub_a, n_pos=3)
    if cagr_a_n3 is not None and cagr_a_n3 > 0.5:
        print("\n[INFO] FIX_A CAGR>50%, 加测 N=1")
        backtest('FIX_A', sub_a, n_pos=1)

    print("\n" + "#" * 80)
    print("# 修复 B: 全天信号 + T+1 h1_open 买入 + T+2 h1_open 卖出")
    print("#" * 80)
    sub_b = signal_fixB(df)
    sub_b = signal_stats('FIX_B', sub_b)
    cagr_b_n3 = backtest('FIX_B', sub_b, n_pos=3)
    if cagr_b_n3 is not None and cagr_b_n3 > 0.5:
        print("\n[INFO] FIX_B CAGR>50%, 加测 N=1")
        backtest('FIX_B', sub_b, n_pos=1)

    print("\n" + "#" * 80)
    print("# 完成")
    print("#" * 80)


if __name__ == '__main__':
    main()
