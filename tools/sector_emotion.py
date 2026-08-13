#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""[Task#75] 板块情绪指标引擎: 行业(stock_industry)+概念(stock_concept)双口径。

成分股等权日涨幅自算(从stock_kline回溯), 写 stocks.db 新表 sector_emotion_daily。
只写新表不动既有表。指标全部基于当日及以前数据, 策略侧仅允许用D-1行(次日使用)。

表结构 sector_emotion_daily:
  sector_type TEXT   'IND'行业 | 'CPT'概念
  sector_id   TEXT   行业名 | BK代码
  sector_name TEXT
  date        TEXT
  n_members   INT    当日有行成分股数
  ret         REAL   成分股等权平均涨幅%(close_rate均值)
  rank_pct    REAL   当日强度百分位(100=最强, 同type内)
  n_lu        INT    板块涨停家数(剔ST)
  lu_ratio    REAL   涨停占比%
  max_streak  INT    板块内最高连板高度
  amt_share   REAL   板块成交额占全市场%(多概念重叠, CPT口径占比总和>100属正常)
  amt_chg5    REAL   amt_share - 前5日amt_share均值 (pp)
  strong_days INT    连续强势天数(rank_pct>=80的连续日数)
  new_start   INT    新启动: 昨日rank_pct<=50 且 今日>=90 且 今日n_lu>=2
  rotate_out  INT    轮出: 昨日strong_days>=3 且 龙头断板(max_streak回落且昨日>=2)
                     且 今日n_lu<=昨日一半
  mainline    INT    主线: 近5日ret均值同type前3 且 近5日n_lu合计>=5

用法:
  python3 tools/sector_emotion.py --backfill 2021-01-01   # 全历史(分年块计算)
  python3 tools/sector_emotion.py --daily                 # 盘后增量(最近10日重算)
增量模式挂 daily_update.sh (red_ratio之后)。streak/rolling指标需前置60日数据,
增量重算窗口自动多取90日, 结果幂等覆盖。
"""
import argparse
import sqlite3
import sys
import time

import numpy as np
import pandas as pd

DB = '/home/AIWealth/data/stocks.db'
TOP_STRONG = 80.0   # rank_pct>=80 视为强势(前20%)
NEW_HI, NEW_LO = 90.0, 50.0
MIN_MEMBERS = 8     # 当日有行成分股不足8只的板块日不产行(指标无意义)

DDL = ('CREATE TABLE IF NOT EXISTS sector_emotion_daily ('
       'sector_type TEXT, sector_id TEXT, sector_name TEXT, date TEXT, '
       'n_members INTEGER, ret REAL, rank_pct REAL, n_lu INTEGER, '
       'lu_ratio REAL, max_streak INTEGER, amt_share REAL, amt_chg5 REAL, '
       'strong_days INTEGER, new_start INTEGER, rotate_out INTEGER, '
       'mainline INTEGER, PRIMARY KEY(sector_type, sector_id, date))')


def load_market(conn, start, end):
    """全市场日线+涨停/连板标注(与t70/QA体系同口径)。"""
    df = pd.read_sql(
        'SELECT date, code, code_name, preclose, close, close_rate, amount, isST '
        'FROM stock_kline WHERE date>=? AND date<=? ORDER BY code, date',
        conn, params=(start, end))
    df['st'] = (df['isST'].fillna(0).astype(bool)
                | df['code_name'].astype(str).str.upper().str.contains('ST'))
    ratio = np.where(df['code'].str.startswith(('sz.30', 'sh.688')), 0.20,
                     np.where(df['st'], 0.05, 0.10))
    lp = (df['preclose'] * (1 + ratio)).round(2)
    df['is_lu'] = ((df['preclose'].fillna(0) > 0) & df['close'].notna()
                   & (df['close'] >= lp - 0.001) & ~df['st'])
    blocks = (~df['is_lu']).groupby(df['code']).cumsum()
    df['streak'] = (df['is_lu'].astype(int)
                    .groupby([df['code'], blocks]).cumsum()
                    .where(df['is_lu'], 0))
    return df[['date', 'code', 'close_rate', 'amount', 'is_lu', 'streak']]


def aggregate(df, mapping, stype):
    """按板块×日聚合基础指标。mapping: DataFrame(code, sector_id, sector_name)。"""
    m = df.merge(mapping, on='code', how='inner')
    day_amt = df.groupby('date')['amount'].sum().rename('mkt_amt')
    g = m.groupby(['sector_id', 'date'])
    agg = g.agg(n_members=('code', 'size'),
                ret=('close_rate', 'mean'),
                n_lu=('is_lu', 'sum'),
                max_streak=('streak', 'max'),
                amt=('amount', 'sum')).reset_index()
    agg = agg[agg['n_members'] >= MIN_MEMBERS]
    agg['lu_ratio'] = agg['n_lu'] / agg['n_members'] * 100
    agg = agg.merge(day_amt, on='date')
    agg['amt_share'] = agg['amt'] / agg['mkt_amt'] * 100
    names = mapping.drop_duplicates('sector_id')[['sector_id', 'sector_name']]
    agg = agg.merge(names, on='sector_id')
    agg['sector_type'] = stype
    return agg.drop(columns=['amt', 'mkt_amt'])


def derive(agg):
    """派生指标: rank_pct/amt_chg5/strong_days/new_start/rotate_out/mainline。

    输入须为单一sector_type、按(sector_id,date)完整历史(含前置60日)。
    """
    agg = agg.sort_values(['sector_id', 'date']).reset_index(drop=True)
    agg['rank_pct'] = agg.groupby('date')['ret'].rank(pct=True) * 100

    g = agg.groupby('sector_id', group_keys=False)
    ma5_prev = g['amt_share'].apply(
        lambda s: s.shift(1).rolling(5, min_periods=3).mean())
    agg['amt_chg5'] = agg['amt_share'] - ma5_prev

    strong = agg['rank_pct'] >= TOP_STRONG
    sblocks = (~strong).groupby(agg['sector_id']).cumsum()
    agg['strong_days'] = (strong.astype(int)
                          .groupby([agg['sector_id'], sblocks]).cumsum()
                          .where(strong, 0))

    prev_rank = g['rank_pct'].shift(1)
    prev_sd = g['strong_days'].shift(1)
    prev_ms = g['max_streak'].shift(1)
    prev_lu = g['n_lu'].shift(1)
    agg['new_start'] = ((prev_rank <= NEW_LO) & (agg['rank_pct'] >= NEW_HI)
                        & (agg['n_lu'] >= 2)).astype(int)
    agg['rotate_out'] = ((prev_sd >= 3) & (prev_ms >= 2)
                         & (agg['max_streak'] < prev_ms)
                         & (agg['n_lu'] <= prev_lu / 2)).astype(int)

    ret5 = g['ret'].apply(lambda s: s.rolling(5, min_periods=3).mean())
    lu5 = g['n_lu'].apply(lambda s: s.rolling(5, min_periods=3).sum())
    agg['_ret5'] = ret5
    r5rank = agg.groupby('date')['_ret5'].rank(ascending=False)
    agg['mainline'] = ((r5rank <= 3) & (lu5 >= 5)).astype(int)
    return agg.drop(columns=['_ret5'])


def write_rows(conn, agg, start):
    """幂等写入[start,∞)区间行(前置日只参与计算不写入)。"""
    out = agg[agg['date'] >= start]
    cols = ['sector_type', 'sector_id', 'sector_name', 'date', 'n_members',
            'ret', 'rank_pct', 'n_lu', 'lu_ratio', 'max_streak', 'amt_share',
            'amt_chg5', 'strong_days', 'new_start', 'rotate_out', 'mainline']
    rows = [tuple(r) for r in out[cols].round(
        {'ret': 4, 'rank_pct': 2, 'lu_ratio': 2, 'amt_share': 4,
         'amt_chg5': 4}).itertuples(index=False)]
    conn.executemany(
        f"INSERT OR REPLACE INTO sector_emotion_daily VALUES "
        f"({','.join('?' * len(cols))})", rows)
    conn.commit()
    return len(rows)


def get_mappings(conn):
    maps = {}
    ind = pd.read_sql(
        "SELECT code, industry AS sector_id, industry AS sector_name "
        "FROM stock_industry WHERE industry != ''", conn)
    maps['IND'] = ind
    try:
        cpt = pd.read_sql(
            'SELECT code, concept_code AS sector_id, concept_name AS sector_name '
            'FROM stock_concept', conn)
        if len(cpt):
            maps['CPT'] = cpt
    except Exception:
        pass  # stock_concept未就绪时仅行业口径
    return maps


def run(write_start, calc_start, calc_end, types=None):
    conn = sqlite3.connect(DB, timeout=60)
    conn.execute(DDL)
    maps = get_mappings(conn)
    if types:
        maps = {k: v for k, v in maps.items() if k in types}
    total = 0
    # 分年块计算(概念口径explode后内存可控); 每块前置90日保证streak/rolling正确
    years = sorted({d[:4] for d in
                    pd.date_range(calc_start, calc_end, freq='YS')
                    .strftime('%Y-%m-%d')} | {calc_start[:4]})
    for stype, mapping in maps.items():
        for y in years:
            blk_start = max(f'{y}-01-01', calc_start)
            blk_end = min(f'{y}-12-31', calc_end)
            if blk_start > blk_end:
                continue
            pre = (pd.Timestamp(blk_start) - pd.Timedelta(days=90)
                   ).strftime('%Y-%m-%d')
            t0 = time.time()
            df = load_market(conn, pre, blk_end)
            agg = aggregate(df, mapping, stype)
            agg = derive(agg)
            n = write_rows(conn, agg, max(blk_start, write_start))
            total += n
            print(f'[{stype} {y}] 写入{n}行 耗时{time.time() - t0:.0f}s',
                  flush=True)
            del df, agg
    conn.close()
    return total


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--backfill', metavar='START', help='全历史回补起点')
    ap.add_argument('--daily', action='store_true', help='盘后增量(最近10日重算)')
    ap.add_argument('--types', help='限定口径: IND / CPT / IND,CPT')
    args = ap.parse_args()
    types = args.types.split(',') if args.types else None

    if args.backfill:
        end = time.strftime('%Y-%m-%d')
        n = run(args.backfill, args.backfill, end, types)
        print(f'回补完成: 共{n}行')
    elif args.daily:
        conn = sqlite3.connect(f'file:{DB}?mode=ro', uri=True)
        last = conn.execute('SELECT MAX(date) FROM stock_kline').fetchone()[0]
        conn.close()
        start = (pd.Timestamp(last) - pd.Timedelta(days=16)).strftime('%Y-%m-%d')
        n = run(start, start, last, types)
        print(f'增量完成({start}~{last}): {n}行')
    else:
        ap.print_help()
        sys.exit(1)


if __name__ == '__main__':
    main()
