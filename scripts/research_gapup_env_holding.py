#!/usr/bin/env python3
"""
跳空高开 - 大盘环境 × 持仓周期 分层研究
研究不同大盘环境和持仓周期下跳空高开的表现差异
"""

import sqlite3
import numpy as np
import pandas as pd
from collections import defaultdict
import time
import os
import sys

DB_PATH = "/home/AIWealth/data/stocks.db"
LOG_PATH = "/home/AIWealth/scripts/logs/gapup_environment_holding.log"

# === 板块定义 ===
def get_board(code):
    """返回板块名称和涨跌停比例"""
    if code.startswith('sh.688') or code.startswith('sh.689'):
        return '科创板', 0.20
    elif code.startswith('sz.300') or code.startswith('sz.301') or code.startswith('sz.302'):
        return '创业板', 0.20
    elif code.startswith('bj.'):
        return '北交所', 0.30
    else:
        return '主板', 0.10

# === Gap阈值 ===
BOARD_GAPS = {
    '主板': [0.01, 0.02, 0.03, 0.05],
    '创业板': [0.02, 0.03, 0.05, 0.08],
    '科创板': [0.02, 0.05, 0.08, 0.10, 0.15],
    '北交所': [0.02, 0.05, 0.10, 0.15, 0.20],
}

# === 市值分组 ===
MCAP_GROUPS = [
    ('<50亿', 0, 50e8),
    ('50-200亿', 50e8, 200e8),
    ('200-700亿', 200e8, 700e8),
    ('>700亿', 700e8, 1e15),
]

def compute_market_env(conn):
    """计算每日大盘环境状态，用主板大票(>500亿)均涨跌做代理"""
    print("  计算大盘环境指标...")
    # 获取所有主板股票的日收益率
    query = """
    SELECT date, code, close_rate, amount, turn
    FROM stock_kline
    WHERE code NOT LIKE 'test%'
      AND (code LIKE 'sh.600%' OR code LIKE 'sh.601%' OR code LIKE 'sh.603%'
           OR code LIKE 'sh.605%' OR code LIKE 'sz.000%' OR code LIKE 'sz.001%'
           OR code LIKE 'sz.002%')
      AND isST = 0
      AND turn > 0 AND amount > 0
    ORDER BY date
    """
    df = pd.read_sql_query(query, conn)
    print(f"  主板数据行数: {len(df)}")

    # 估算流通市值 = amount / (turn/100)
    df['float_mcap'] = df['amount'] / (df['turn'] / 100.0)

    # 只选市值>500亿的大票
    big = df[df['float_mcap'] > 500e8].copy()
    print(f"  大票(>500亿)行数: {len(big)}")

    # 每日大盘收益 = 大票均收益
    daily_ret = big.groupby('date')['close_rate'].mean().reset_index()
    daily_ret.columns = ['date', 'market_ret']
    daily_ret = daily_ret.sort_values('date').reset_index(drop=True)

    # 计算5日累计收益
    daily_ret['cum5'] = daily_ret['market_ret'].rolling(5).sum()

    # 判定环境
    def classify_env(row):
        if row['market_ret'] < -1.5:
            return '恐慌'
        if pd.isna(row['cum5']):
            return '震荡'
        if row['cum5'] > 1.0:
            return '强势'
        elif row['cum5'] < -1.0:
            return '弱势'
        else:
            return '震荡'

    daily_ret['env'] = daily_ret.apply(classify_env, axis=1)
    env_map = dict(zip(daily_ret['date'], daily_ret['env']))

    # 统计
    from collections import Counter
    env_counts = Counter(env_map.values())
    print(f"  大盘环境分布: {dict(env_counts)}")

    return env_map


def process_stocks_chunk(conn, codes, env_map, all_dates_sorted):
    """处理一批股票，返回信号列表"""
    signals = []

    placeholders = ','.join(['?'] * len(codes))
    query = f"""
    SELECT date, code, preclose, open, close, close_rate,
           hour1_open, hour4_close, amount, turn, isST
    FROM stock_kline
    WHERE code IN ({placeholders})
      AND isST = 0
    ORDER BY code, date
    """
    df = pd.read_sql_query(query, conn, params=codes)

    if df.empty:
        return signals

    for code, grp in df.groupby('code'):
        grp = grp.sort_values('date').reset_index(drop=True)
        board, limit_ratio = get_board(code)

        for i in range(1, len(grp)):
            row = grp.iloc[i]
            prev = grp.iloc[i-1]

            # ST排除
            if row['isST'] == 1:
                continue

            # 基本数据检查
            if pd.isna(row['open']) or pd.isna(prev['close']) or prev['close'] <= 0:
                continue
            if pd.isna(row['hour1_open']) or row['hour1_open'] <= 0:
                continue
            if pd.isna(row['preclose']) or row['preclose'] <= 0:
                continue

            # 涨停价排除: open >= round(preclose * (1+limit_ratio), 2)
            limit_up_price = round(row['preclose'] * (1 + limit_ratio), 2)
            if row['open'] >= limit_up_price:
                continue

            # 计算gap
            gap = (row['open'] - prev['close']) / prev['close']
            if gap <= 0:
                continue

            # 买入价 = hour1_open
            buy_price = row['hour1_open']

            # 估算市值
            mcap = None
            if row['turn'] and row['turn'] > 0 and row['amount'] and row['amount'] > 0:
                mcap = row['amount'] / (row['turn'] / 100.0)

            # 获取未来N天数据
            future_data = {}
            for j in range(1, 6):
                if i + j < len(grp):
                    fut = grp.iloc[i + j]
                    future_data[j] = {
                        'hour4_close': fut['hour4_close'],
                        'hour1_open': fut['hour1_open'],
                        'open': fut['open'],
                        'close': fut['close'],
                        'preclose': fut['preclose'],
                    }

            # 计算各周期收益
            returns = {}
            for t in [1, 2, 3, 5]:
                if t in future_data and future_data[t]['hour4_close'] and buy_price > 0:
                    h4c = future_data[t]['hour4_close']
                    if not pd.isna(h4c) and h4c > 0:
                        returns[f'T+{t}'] = (h4c - buy_price) / buy_price * 100

            # 动态退出
            if 1 in future_data and buy_price > 0:
                t1_open = future_data[1].get('open')
                t1_h1_open = future_data[1].get('hour1_open')
                t1_preclose = future_data[1].get('preclose')
                if t1_open and t1_preclose and t1_preclose > 0:
                    t1_open_gap = (t1_open - t1_preclose) / t1_preclose
                    if t1_open_gap > 0.03 and t1_h1_open and not pd.isna(t1_h1_open) and t1_h1_open > 0:
                        returns['动态'] = (t1_h1_open - buy_price) / buy_price * 100
                    elif 2 in future_data and future_data[2]['hour4_close']:
                        h4c = future_data[2]['hour4_close']
                        if not pd.isna(h4c) and h4c > 0:
                            returns['动态'] = (h4c - buy_price) / buy_price * 100

            date = row['date']
            env = env_map.get(date, '震荡')

            signals.append({
                'date': date,
                'code': code,
                'board': board,
                'gap': gap,
                'mcap': mcap,
                'env': env,
                'returns': returns,
            })

    return signals


def get_mcap_group(mcap):
    if mcap is None:
        return None
    for name, lo, hi in MCAP_GROUPS:
        if lo <= mcap < hi:
            return name
    return None


def main():
    start_time = time.time()
    os.makedirs(os.path.dirname(LOG_PATH), exist_ok=True)

    conn = sqlite3.connect(DB_PATH)

    print("=" * 60)
    print("跳空高开 - 大盘环境 × 持仓周期 分层研究")
    print("=" * 60)

    # Step 1: 计算大盘环境
    env_map = compute_market_env(conn)

    # Step 2: 获取所有股票代码
    codes_df = pd.read_sql_query(
        "SELECT DISTINCT code FROM stock_kline WHERE code NOT LIKE 'test%' AND isST=0",
        conn
    )
    all_codes = codes_df['code'].tolist()
    print(f"\n总股票数: {len(all_codes)}")

    # 获取所有交易日
    dates_df = pd.read_sql_query(
        "SELECT DISTINCT date FROM stock_kline ORDER BY date", conn
    )
    all_dates = dates_df['date'].tolist()

    # Step 3: 分批处理
    CHUNK_SIZE = 400
    all_signals = []

    for chunk_idx in range(0, len(all_codes), CHUNK_SIZE):
        chunk_codes = all_codes[chunk_idx:chunk_idx + CHUNK_SIZE]
        elapsed = time.time() - start_time
        print(f"  处理 {chunk_idx+1}-{chunk_idx+len(chunk_codes)}/{len(all_codes)} "
              f"({elapsed:.0f}s elapsed, signals={len(all_signals)})")

        signals = process_stocks_chunk(conn, chunk_codes, env_map, all_dates)
        all_signals.extend(signals)

    conn.close()

    elapsed = time.time() - start_time
    print(f"\n数据采集完成: {len(all_signals)} 个信号, 耗时 {elapsed:.0f}s")

    # Step 4: 分析并输出
    output_lines = []

    def out(line=""):
        output_lines.append(line)

    out("=" * 60)
    out("跳空高开 - 大盘环境 × 持仓周期 分层研究")
    out("=" * 60)
    out(f"数据区间: {all_dates[0]} ~ {all_dates[-1]}")
    out(f"总信号数: {len(all_signals)}")
    out(f"计算耗时: {elapsed:.0f}s")
    out()

    # ===== 一、大盘环境对收益的影响 =====
    out("=" * 60)
    out("=== 一、大盘环境对收益的影响 ===")
    out("=" * 60)
    out()

    for board in ['主板', '创业板', '科创板']:
        gaps = BOARD_GAPS[board]
        # 选几个代表性gap
        rep_gaps = gaps[1:3] if len(gaps) >= 3 else gaps[:2]

        for gap_thresh in rep_gaps:
            for mcap_name, mcap_lo, mcap_hi in MCAP_GROUPS:
                filtered = [s for s in all_signals
                           if s['board'] == board
                           and s['gap'] >= gap_thresh
                           and s['mcap'] is not None
                           and mcap_lo <= s['mcap'] < mcap_hi]

                if len(filtered) < 30:
                    continue

                out(f"--- {board} Gap>={gap_thresh*100:.0f}% {mcap_name} ---")
                out(f"{'环境':<8} {'样本':>6} {'T+1收益':>9} {'T+1胜率':>8} {'T+2收益':>9} {'T+2胜率':>8}")

                for env in ['强势', '弱势', '震荡', '恐慌']:
                    env_sigs = [s for s in filtered if s['env'] == env]
                    if len(env_sigs) < 10:
                        continue

                    t1_rets = [s['returns']['T+1'] for s in env_sigs if 'T+1' in s['returns']]
                    t2_rets = [s['returns']['T+2'] for s in env_sigs if 'T+2' in s['returns']]

                    t1_avg = np.mean(t1_rets) if t1_rets else 0
                    t1_wr = sum(1 for r in t1_rets if r > 0) / len(t1_rets) * 100 if t1_rets else 0
                    t2_avg = np.mean(t2_rets) if t2_rets else 0
                    t2_wr = sum(1 for r in t2_rets if r > 0) / len(t2_rets) * 100 if t2_rets else 0

                    out(f"{env:<8} {len(env_sigs):>6} {t1_avg:>+8.2f}% {t1_wr:>7.1f}% {t2_avg:>+8.2f}% {t2_wr:>7.1f}%")

                out()

    # ===== 二、持仓周期对比 =====
    out("=" * 60)
    out("=== 二、持仓周期对比 ===")
    out("=" * 60)
    out()

    for board in ['主板', '创业板', '科创板']:
        gaps = BOARD_GAPS[board]
        rep_gaps = gaps[1:3] if len(gaps) >= 3 else gaps[:2]

        for gap_thresh in rep_gaps:
            for mcap_name, mcap_lo, mcap_hi in MCAP_GROUPS:
                filtered = [s for s in all_signals
                           if s['board'] == board
                           and s['gap'] >= gap_thresh
                           and s['mcap'] is not None
                           and mcap_lo <= s['mcap'] < mcap_hi]

                if len(filtered) < 30:
                    continue

                out(f"--- {board} Gap>={gap_thresh*100:.0f}% {mcap_name} ---")
                out(f"{'周期':<8} {'样本':>6} {'收益':>9} {'胜率':>8} {'中位数':>9} {'最差年':>18}")

                for period in ['T+1', 'T+2', 'T+3', 'T+5', '动态']:
                    rets = [(s['returns'][period], s['date'][:4]) for s in filtered if period in s['returns']]
                    if len(rets) < 10:
                        continue

                    ret_vals = [r[0] for r in rets]
                    avg_ret = np.mean(ret_vals)
                    win_rate = sum(1 for r in ret_vals if r > 0) / len(ret_vals) * 100
                    median_ret = np.median(ret_vals)

                    # 最差年
                    yearly = defaultdict(list)
                    for r, y in rets:
                        yearly[y].append(r)
                    worst_year = min(yearly.keys(), key=lambda y: np.mean(yearly[y]))
                    worst_ret = np.mean(yearly[worst_year])

                    out(f"{period:<8} {len(rets):>6} {avg_ret:>+8.2f}% {win_rate:>7.1f}% {median_ret:>+8.2f}% {worst_year}({worst_ret:>+.2f}%)")

                out()

    # ===== 三、高波动板块专项 =====
    out("=" * 60)
    out("=== 三、高波动板块（科创板、北交所）专项 ===")
    out("=" * 60)
    out()

    # 科创板
    star_sigs = [s for s in all_signals if s['board'] == '科创板']
    if star_sigs:
        out("--- 科创板大幅高开 ---")
        gap_ranges = [(0.05, 0.08), (0.08, 0.10), (0.10, 0.15), (0.15, 0.20)]
        out(f"{'Gap区间':<10} {'样本':>6} {'T+1收益':>9} {'T+1胜率':>8} {'T+2收益':>9} {'T+2胜率':>8} {'最优退出':>10}")

        for lo, hi in gap_ranges:
            filtered = [s for s in star_sigs if lo <= s['gap'] < hi]
            if len(filtered) < 10:
                out(f"{lo*100:.0f}-{hi*100:.0f}%     {'样本不足':<30}")
                continue

            t1_rets = [s['returns']['T+1'] for s in filtered if 'T+1' in s['returns']]
            t2_rets = [s['returns']['T+2'] for s in filtered if 'T+2' in s['returns']]
            dyn_rets = [s['returns']['动态'] for s in filtered if '动态' in s['returns']]

            t1_avg = np.mean(t1_rets) if t1_rets else 0
            t1_wr = sum(1 for r in t1_rets if r > 0) / len(t1_rets) * 100 if t1_rets else 0
            t2_avg = np.mean(t2_rets) if t2_rets else 0
            t2_wr = sum(1 for r in t2_rets if r > 0) / len(t2_rets) * 100 if t2_rets else 0

            # 最优退出
            best_period = 'T+1'
            best_ret = t1_avg
            for p in ['T+2', 'T+3', 'T+5', '动态']:
                p_rets = [s['returns'][p] for s in filtered if p in s['returns']]
                if p_rets and np.mean(p_rets) > best_ret:
                    best_ret = np.mean(p_rets)
                    best_period = p

            out(f"{lo*100:.0f}-{hi*100:.0f}%    {len(filtered):>6} {t1_avg:>+8.2f}% {t1_wr:>7.1f}% {t2_avg:>+8.2f}% {t2_wr:>7.1f}% {best_period:>10}")
        out()

    # 北交所
    bse_sigs = [s for s in all_signals if s['board'] == '北交所']
    if bse_sigs:
        out("--- 北交所大幅高开 ---")
        gap_ranges = [(0.05, 0.10), (0.10, 0.15), (0.15, 0.20), (0.20, 0.30)]
        out(f"{'Gap区间':<10} {'样本':>6} {'T+1收益':>9} {'T+1胜率':>8} {'T+2收益':>9} {'T+2胜率':>8} {'最优退出':>10}")

        for lo, hi in gap_ranges:
            filtered = [s for s in bse_sigs if lo <= s['gap'] < hi]
            if len(filtered) < 10:
                out(f"{lo*100:.0f}-{hi*100:.0f}%     {'样本不足':<30}")
                continue

            t1_rets = [s['returns']['T+1'] for s in filtered if 'T+1' in s['returns']]
            t2_rets = [s['returns']['T+2'] for s in filtered if 'T+2' in s['returns']]

            t1_avg = np.mean(t1_rets) if t1_rets else 0
            t1_wr = sum(1 for r in t1_rets if r > 0) / len(t1_rets) * 100 if t1_rets else 0
            t2_avg = np.mean(t2_rets) if t2_rets else 0
            t2_wr = sum(1 for r in t2_rets if r > 0) / len(t2_rets) * 100 if t2_rets else 0

            best_period = 'T+1'
            best_ret = t1_avg
            for p in ['T+2', 'T+3', 'T+5', '动态']:
                p_rets = [s['returns'][p] for s in filtered if p in s['returns']]
                if p_rets and np.mean(p_rets) > best_ret:
                    best_ret = np.mean(p_rets)
                    best_period = p

            out(f"{lo*100:.0f}-{hi*100:.0f}%    {len(filtered):>6} {t1_avg:>+8.2f}% {t1_wr:>7.1f}% {t2_avg:>+8.2f}% {t2_wr:>7.1f}% {best_period:>10}")
        out()
    else:
        out("北交所: 无数据")
        out()

    # ===== 四、最优组合 TOP 20 =====
    out("=" * 60)
    out("=== 四、最优组合 TOP 20 ===")
    out("按 score = avg_ret × win_rate / 100 排序")
    out("=" * 60)
    out()

    combos = []

    for board in ['主板', '创业板', '科创板', '北交所']:
        gaps = BOARD_GAPS[board]
        for gap_thresh in gaps:
            for mcap_name, mcap_lo, mcap_hi in MCAP_GROUPS:
                for env in ['强势', '弱势', '震荡', '恐慌', '全部']:
                    for period in ['T+1', 'T+2', 'T+3', 'T+5', '动态']:
                        if env == '全部':
                            filtered = [s for s in all_signals
                                       if s['board'] == board
                                       and s['gap'] >= gap_thresh
                                       and s['mcap'] is not None
                                       and mcap_lo <= s['mcap'] < mcap_hi]
                        else:
                            filtered = [s for s in all_signals
                                       if s['board'] == board
                                       and s['gap'] >= gap_thresh
                                       and s['mcap'] is not None
                                       and mcap_lo <= s['mcap'] < mcap_hi
                                       and s['env'] == env]

                        rets = [s['returns'][period] for s in filtered if period in s['returns']]
                        if len(rets) < 50:
                            continue

                        avg_ret = np.mean(rets)
                        win_rate = sum(1 for r in rets if r > 0) / len(rets) * 100
                        score = avg_ret * win_rate / 100

                        combos.append({
                            'board': board,
                            'gap': gap_thresh,
                            'mcap': mcap_name,
                            'env': env,
                            'period': period,
                            'samples': len(rets),
                            'avg_ret': avg_ret,
                            'win_rate': win_rate,
                            'score': score,
                        })

    combos.sort(key=lambda x: x['score'], reverse=True)

    out(f"{'#':<3} {'板块':<5} {'Gap':>5} {'市值':<9} {'环境':<5} {'周期':<5} {'样本':>6} {'收益':>9} {'胜率':>8} {'Score':>8}")
    out("-" * 80)

    for i, c in enumerate(combos[:20], 1):
        out(f"{i:<3} {c['board']:<5} {c['gap']*100:>4.0f}% {c['mcap']:<9} {c['env']:<5} {c['period']:<5} "
            f"{c['samples']:>6} {c['avg_ret']:>+8.2f}% {c['win_rate']:>7.1f}% {c['score']:>+7.2f}")

    out()
    out("-" * 80)
    out(f"总分析完成, 耗时 {time.time()-start_time:.0f}s")

    # 写入日志
    log_content = "\n".join(output_lines)
    with open(LOG_PATH, 'w', encoding='utf-8') as f:
        f.write(log_content)

    print(f"\n日志已写入: {LOG_PATH}")
    print(log_content)


if __name__ == "__main__":
    main()
