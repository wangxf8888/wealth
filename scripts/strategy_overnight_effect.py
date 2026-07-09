#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Task #30: 隔夜收益效应策略探索

测试A股隔夜收益(overnight return)效应。
overnight_return = T+1 hour1_open / T hour4_open - 1
(用 hour4_open 而非 hour4_close, 因为 14:00 价格已知, 可下单买入)

输出:
1. 全样本/分年份基础统计 (确认是否存在系统性正向隔夜收益)
2. 5种条件型 Signal (O1-O5) 的 N、mean、win_rate、按年份
3. 对 O1-O4 做 N=3 回测 (初始100万, 2020-2025), 输出逐年收益和 CAGR
4. O5 仅做统计 (因 hour4_open 时尚未发生跳水)

排除: ST、北交所
随机种子: 42
"""
import sqlite3
import numpy as np
import pandas as pd
import random
import os
import sys
from collections import defaultdict

DB_PATH = '/home/AIWealth/data/stocks.db'
START_DATE = '2020-01-01'
END_DATE = '2025-12-31'
N_POSITIONS = 3
INITIAL_CAPITAL = 1_000_000.0
SEED = 42


def load_data():
    """加载所需字段, 过滤 ST/北交所"""
    print(f"[LOAD] 连接数据库 {DB_PATH}")
    conn = sqlite3.connect(DB_PATH)
    sql = """
    SELECT date, code, preclose, close, close_rate, turn, volume,
           hour1_open, hour3_close, hour4_open, hour4_close
    FROM stock_kline
    WHERE date BETWEEN ? AND ?
      AND (isST IS NULL OR isST = 0)
      AND (code LIKE 'sh.%' OR code LIKE 'sz.%')
    """
    df = pd.read_sql(sql, conn, params=(START_DATE, END_DATE))
    conn.close()
    print(f"[LOAD] 共 {len(df):,} 条记录, 股票数 {df['code'].nunique():,}, "
          f"日期范围 {df['date'].min()} ~ {df['date'].max()}")
    # 类型优化
    for col in ['preclose', 'close', 'close_rate', 'turn', 'hour1_open',
                'hour3_close', 'hour4_open', 'hour4_close']:
        df[col] = df[col].astype(np.float32)
    df['volume'] = df['volume'].astype(np.float64)
    return df


def add_derived_columns(df):
    """加入 next_hour1_open, 滚动统计列等"""
    df = df.sort_values(['code', 'date']).reset_index(drop=True)
    g = df.groupby('code', sort=False)

    # 次日 hour1_open (下一条记录, 即下一个交易日的 hour1_open)
    df['next_hour1_open'] = g['hour1_open'].shift(-1)

    # 隔夜收益: T+1 hour1_open / T hour4_open - 1
    df['overnight_return'] = df['next_hour1_open'] / df['hour4_open'] - 1.0

    # T-1 close_rate (用于 O3)
    df['close_rate_prev'] = g['close_rate'].shift(1)

    # 近20日 close 最大值 (T-1 ~ T-20), 用于 O4
    df['max_close_20d'] = g['close'].shift(1).rolling(20, min_periods=20).max().reset_index(level=0, drop=True)

    # 前5日平均成交量 (T-1 ~ T-5), 用于 O4
    df['avg_vol_5d'] = g['volume'].shift(1).rolling(5, min_periods=5).mean().reset_index(level=0, drop=True)

    # year 列, 后续按年份汇总用 signal date 的年份
    df['year'] = df['date'].str.slice(0, 4)
    return df


def basic_overnight_stats(df):
    """全样本+按年份的基础隔夜收益统计"""
    print("\n" + "=" * 80)
    print("[STATS] 全样本基础隔夜收益统计 (overnight_return = T+1 h1_open / T h4_open - 1)")
    print("=" * 80)
    valid = df.dropna(subset=['overnight_return']).copy()
    # 防御: 极端值过滤 (>50% 的隔夜跳跃通常是数据异常或除权除息)
    extreme_mask = valid['overnight_return'].abs() > 0.5
    n_ext = int(extreme_mask.sum())
    if n_ext > 0:
        print(f"[WARN] 剔除 {n_ext:,} 条 |overnight_return|>50% 异常记录 (可能除权)")
        valid = valid.loc[~extreme_mask].copy()

    def _stat(s):
        return {
            'N': len(s),
            'mean': float(s.mean()),
            'median': float(s.median()),
            'win_rate': float((s > 0).mean()),
        }

    full = _stat(valid['overnight_return'])
    print(f"[ALL]  N={full['N']:>10,} mean={full['mean']*100:+.4f}% "
          f"median={full['median']*100:+.4f}% win_rate={full['win_rate']*100:.2f}%")

    print("\n[BY YEAR]")
    print(f"{'YEAR':<6}{'N':>12}{'MEAN':>12}{'MEDIAN':>12}{'WIN_RATE':>12}")
    for y, g in valid.groupby('year'):
        s = _stat(g['overnight_return'])
        print(f"{y:<6}{s['N']:>12,}{s['mean']*100:>11.4f}%"
              f"{s['median']*100:>11.4f}%{s['win_rate']*100:>11.2f}%")

    if full['mean'] > 0 and full['win_rate'] > 0.5:
        print("\n[CONCLUSION] >> 存在系统性正向隔夜溢价 (mean>0 且 win_rate>50%)")
    elif full['mean'] > 0:
        print("\n[CONCLUSION] >> 平均隔夜收益为正, 但 win_rate<=50% (可能受少数大涨样本主导)")
    else:
        print("\n[CONCLUSION] >> 未观察到正向隔夜溢价")

    return valid


# ---------------------- Signal 定义 ----------------------
def signal_O1(df):
    """全市场基线: 任意有 overnight_return 的样本"""
    return df.dropna(subset=['overnight_return']).copy()


def signal_O2(df):
    """尾盘上涨型: h4_close>h4_open, close_rate>0, turn>3%"""
    m = (df['hour4_close'] > df['hour4_open']) & \
        (df['close_rate'] > 0) & \
        (df['turn'] > 3.0) & \
        df['overnight_return'].notna()
    return df.loc[m].copy()


def signal_O3(df):
    """连涨动量延续: T-1 close>preclose 且 T close>preclose, T close_rate>2%"""
    m = (df['close_rate_prev'] > 0) & \
        (df['close_rate'] > 2.0) & \
        df['overnight_return'].notna()
    return df.loc[m].copy()


def signal_O4(df):
    """低位放量翻红: T close < max20d*0.85, vol>5d_avg*1.5, close_rate>0"""
    m = (df['close'] < df['max_close_20d'] * 0.85) & \
        (df['volume'] > df['avg_vol_5d'] * 1.5) & \
        (df['close_rate'] > 0) & \
        df['overnight_return'].notna()
    return df.loc[m].copy()


def signal_O5(df):
    """尾盘跳水(反转隔夜): h4_close/h3_close-1<=-2%, close_rate>-5%
    仅做统计, 因为 h4_open 时尚未发生跳水, 不可作为入场点"""
    h3c = df['hour3_close']
    h4c = df['hour4_close']
    tail_drop = h4c / h3c - 1.0
    m = (tail_drop <= -0.02) & \
        (df['close_rate'] > -5.0) & \
        df['overnight_return'].notna() & \
        h3c.notna() & (h3c > 0)
    return df.loc[m].copy()


# ---------------------- 统计 ----------------------
def signal_stats(name, sub_df):
    print("\n" + "-" * 80)
    print(f"[{name}] 候选样本统计")
    print("-" * 80)
    if len(sub_df) == 0:
        print(f"  无候选样本")
        return
    # 极端值过滤
    sub_df = sub_df.loc[sub_df['overnight_return'].abs() <= 0.5].copy()
    s = sub_df['overnight_return']
    print(f"  全样本: N={len(s):,}  mean={s.mean()*100:+.4f}%  "
          f"median={s.median()*100:+.4f}%  win_rate={(s>0).mean()*100:.2f}%")
    print(f"  按年份:  {'YEAR':<6}{'N':>10}{'MEAN':>12}{'WIN_RATE':>12}{'AVG_DAILY_CAND':>16}")
    n_days_total = sub_df['date'].nunique()
    print(f"  (该 Signal 总命中交易日数: {n_days_total})")
    for y, g in sub_df.groupby('year'):
        rs = g['overnight_return']
        n_days = g['date'].nunique()
        avg_per_day = len(g) / n_days if n_days > 0 else 0.0
        print(f"          {y:<6}{len(g):>10,}{rs.mean()*100:>11.4f}%"
              f"{(rs>0).mean()*100:>11.2f}%{avg_per_day:>16.2f}")


# ---------------------- 回测 ----------------------
def backtest_signal(name, sub_df, n_pos=N_POSITIONS, capital=INITIAL_CAPITAL, seed=SEED):
    """N=N_POSITIONS 等权回测: 每个 signal date 选 n_pos 只, T h4_open 买入, T+1 h1_open 卖出"""
    print("\n" + "=" * 80)
    print(f"[BACKTEST {name}] N={n_pos}  初始资金={capital:,.0f}  seed={seed}")
    print("=" * 80)
    if len(sub_df) == 0:
        print("  无候选, 跳过")
        return
    # 极端值过滤
    sub_df = sub_df.loc[sub_df['overnight_return'].abs() <= 0.5].copy()
    rng = random.Random(seed)
    cap = capital
    by_date = sub_df.groupby('date')
    yearly = defaultdict(lambda: {'start': None, 'end': None, 'n_trade_days': 0, 'n_picks': 0})
    daily_results = []
    for date, g in by_date:
        # 候选 list[(code, ret)]
        cands = list(zip(g['code'].tolist(), g['overnight_return'].tolist()))
        cands = [(c, r) for c, r in cands if r == r]  # 排除 NaN
        if not cands:
            continue
        if len(cands) <= n_pos:
            picks = cands
        else:
            picks = rng.sample(cands, n_pos)
        # 等权: 每只 cap/n_pos
        per_pos = cap / n_pos
        # 卖出后总资金: 已买 n_pos 只, 多余资金为 cap - per_pos*n_pos = 0
        # 每只在 T+1 h1_open 价值变化为 (1+r)
        ret = float(np.mean([r for _, r in picks]))
        # 注意: 实际只用了 len(picks) 仓位, 其余仓位空仓收益0
        if len(picks) < n_pos:
            ret = ret * len(picks) / n_pos
        cap_new = cap * (1 + ret)
        y = date[:4]
        yr = yearly[y]
        if yr['start'] is None:
            yr['start'] = cap
        yr['end'] = cap_new
        yr['n_trade_days'] += 1
        yr['n_picks'] += len(picks)
        daily_results.append((date, ret, cap_new, len(picks)))
        cap = cap_new

    # 输出年度
    print(f"  {'YEAR':<6}{'TRADE_DAYS':>12}{'TOTAL_PICKS':>12}"
          f"{'YEAR_RET':>12}{'CAPITAL_END':>16}")
    for y in sorted(yearly.keys()):
        yr = yearly[y]
        ret_y = yr['end'] / yr['start'] - 1.0 if yr['start'] else 0
        print(f"  {y:<6}{yr['n_trade_days']:>12}{yr['n_picks']:>12}"
              f"{ret_y*100:>11.2f}%{yr['end']:>16,.0f}")
    # 总收益 / CAGR
    if daily_results:
        total_ret = cap / capital - 1.0
        n_years = len(yearly)
        cagr = (cap / capital) ** (1.0 / n_years) - 1.0 if n_years > 0 else 0.0
        # 胜率 (按交易日)
        win_days = sum(1 for _, r, _, _ in daily_results if r > 0)
        print(f"  ----")
        print(f"  TOTAL: 期末资金 {cap:,.0f}  总收益 {total_ret*100:+.2f}%  "
              f"CAGR {cagr*100:+.2f}%  日胜率 {win_days/len(daily_results)*100:.2f}% "
              f"(交易日 {len(daily_results)})")


def main():
    df = load_data()
    df = add_derived_columns(df)

    # 基础统计
    valid_full = basic_overnight_stats(df)

    # 5 种 Signal
    print("\n" + "#" * 80)
    print("# 5 种条件型隔夜 Signal")
    print("#" * 80)
    sigs = {
        'O1_RANDOM_BASELINE': signal_O1(df),
        'O2_TAIL_UP':         signal_O2(df),
        'O3_MOMENTUM_2DAY':   signal_O3(df),
        'O4_LOW_VOL_REBOUND': signal_O4(df),
        'O5_TAIL_CRASH(STAT)':signal_O5(df),
    }
    for name, sub in sigs.items():
        signal_stats(name, sub)

    # O1-O4 回测; O5 仅统计 (用户说明 hour4_open 时跳水尚未发生, 不可交易)
    print("\n" + "#" * 80)
    print("# 回测 (O1-O4)")
    print("#" * 80)
    for name in ['O1_RANDOM_BASELINE', 'O2_TAIL_UP', 'O3_MOMENTUM_2DAY', 'O4_LOW_VOL_REBOUND']:
        backtest_signal(name, sigs[name])

    print("\n" + "#" * 80)
    print("# 完成")
    print("#" * 80)


if __name__ == '__main__':
    main()
