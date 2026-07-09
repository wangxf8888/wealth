#!/usr/bin/env python3
"""
跳空高开 + 多指标共振 研究脚本
===================================
研究方向：跳空高开作为强势信号，配合不同技术指标共振，寻找高胜率买入机会

共振条件(12种)：
  A: 缩量+跳空    B: 放量+跳空    C: 站上MA5+跳空    D: 跳空突破MA5
  E: MA5金叉+跳空  F: 十字星+跳空   G: 缩量横盘+跳空   H: 大阳+跳空
  I: 换手放大+跳空  J: 换手低迷+跳空  K: 低位+跳空      L: 高位+跳空

T+0合规：
  - 所有判断条件只用yesterday及之前数据
  - 买入用today hour1_open
  - 涨停开盘排除
"""
import sys
import os
import sqlite3
import numpy as np
from collections import defaultdict
import time
import logging

# ========== 配置区 ==========
DB_PATH = '/home/AIWealth/data/stocks.db'
LOG_PATH = '/home/AIWealth/scripts/logs/gapup_resonance.log'
DATE_START = '2021-01-01'
DATE_END = '2026-06-30'
GAP_THRESHOLDS = [1.0, 2.0, 3.0, 5.0]
CAP_BINS = [0, 50, 200, 700, 1e18]
CAP_LABELS = ['<50亿', '50-200亿', '200-700亿', '>700亿']
TOP_N = 20
CHUNK_SIZE = 400  # 每次处理的股票数量
# ========== 配置区结束 ==========

os.makedirs(os.path.dirname(LOG_PATH), exist_ok=True)
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(message)s',
    handlers=[
        logging.FileHandler(LOG_PATH, mode='w', encoding='utf-8'),
        logging.StreamHandler()
    ]
)
log = logging.getLogger(__name__)

# 共振条件名
COND_NAMES = {
    'A': 'A:缩量+跳空', 'B': 'B:放量+跳空',
    'C': 'C:站上MA5+跳空', 'D': 'D:突破MA5+跳空',
    'E': 'E:MA5金叉+跳空', 'F': 'F:十字星+跳空',
    'G': 'G:横盘+跳空', 'H': 'H:大阳+跳空',
    'I': 'I:换手放大+跳空', 'J': 'J:换手低迷+跳空',
    'K': 'K:低位+跳空', 'L': 'L:高位+跳空',
}
BOARD_NAMES = {'main': '主板', 'gem': '创业板', 'star': '科创板', 'bse': '北交所'}


def get_board(code):
    if code.startswith('bj.'):
        return 'bse'
    elif code.startswith('sh.688'):
        return 'star'
    elif code.startswith('sz.300') or code.startswith('sz.301'):
        return 'gem'
    else:
        return 'main'


def get_limit_ratio_by_board(board):
    return {'main': 0.10, 'gem': 0.20, 'star': 0.20, 'bse': 0.30}[board]


def get_cap_group(cap_val):
    """市值分组"""
    if cap_val is None or np.isnan(cap_val) or cap_val <= 0:
        return None
    if cap_val < 50:
        return '<50亿'
    elif cap_val < 200:
        return '50-200亿'
    elif cap_val < 700:
        return '200-700亿'
    else:
        return '>700亿'


def process_stock_chunk(conn, codes, trading_days, date_start_idx):
    """
    处理一批股票，返回所有信号记录
    每条记录: (cond, board, cap_group, gap_rate, year, ret_t1, ret_t2)
    """
    if not codes:
        return []

    placeholders = ','.join(['?'] * len(codes))
    cur = conn.cursor()
    cur.execute(f"""
        SELECT date, code, code_name, open, high, low, close, preclose,
               volume, amount, turn, hour1_open, hour4_close, isST
        FROM stock_kline
        WHERE code IN ({placeholders})
        ORDER BY code, date
    """, codes)

    rows = cur.fetchall()
    if not rows:
        return []

    # 按股票分组
    stock_data = defaultdict(list)
    for r in rows:
        stock_data[r[1]].append(r)

    signals = []
    td_set = set(trading_days)
    td_idx = {d: i for i, d in enumerate(trading_days)}

    for code, srows in stock_data.items():
        board = get_board(code)
        limit_ratio = get_limit_ratio_by_board(board)

        n = len(srows)
        if n < 25:
            continue

        # 提取为numpy数组
        dates = [r[0] for r in srows]
        names = [r[2] for r in srows]
        opens = np.array([r[3] or 0 for r in srows], dtype=np.float64)
        highs = np.array([r[4] or 0 for r in srows], dtype=np.float64)
        lows = np.array([r[5] or 0 for r in srows], dtype=np.float64)
        closes = np.array([r[6] or 0 for r in srows], dtype=np.float64)
        precloses = np.array([r[7] or 0 for r in srows], dtype=np.float64)
        volumes = np.array([r[8] or 0 for r in srows], dtype=np.float64)
        amounts = np.array([r[9] or 0 for r in srows], dtype=np.float64)
        turns = np.array([r[10] or 0 for r in srows], dtype=np.float64)
        h1_opens = np.array([r[11] or 0 for r in srows], dtype=np.float64)
        h4_closes = np.array([r[12] or 0 for r in srows], dtype=np.float64)
        is_st = np.array([r[13] or 0 for r in srows], dtype=np.int8)

        # 计算MA5, MA10, vol_ma5, turn_ma5
        ma5 = np.full(n, np.nan)
        ma10 = np.full(n, np.nan)
        vol_ma5 = np.full(n, np.nan)
        turn_ma5 = np.full(n, np.nan)
        high_20 = np.full(n, np.nan)
        low_20 = np.full(n, np.nan)

        # Rolling calculations
        for i in range(4, n):
            ma5[i] = closes[i-4:i+1].mean()
            vol_ma5[i] = volumes[i-4:i+1].mean()
            turn_ma5[i] = turns[i-4:i+1].mean()
        for i in range(9, n):
            ma10[i] = closes[i-9:i+1].mean()
        for i in range(19, n):
            high_20[i] = highs[i-19:i+1].max()
            low_20[i] = lows[i-19:i+1].min()

        # 振幅和实体
        amplitude = np.where(precloses > 0, (highs - lows) / precloses * 100, 0)
        body = np.where(precloses > 0, np.abs(closes - opens) / precloses * 100, 0)
        pct_chg = np.where(precloses > 0, (closes - precloses) / precloses * 100, 0)

        # 遍历每一天，检查信号
        for i in range(22, n):  # 需要至少22天历史
            today_date = dates[i]
            if today_date < DATE_START or today_date > DATE_END:
                continue
            if is_st[i]:
                continue
            name = names[i]
            if name and 'ST' in name.upper():
                continue
            if h1_opens[i] <= 0 or precloses[i] <= 0 or opens[i] <= 0:
                continue
            if closes[i-1] <= 0:
                continue

            # 跳空高开: today_open > yesterday_close * (1 + gap)
            yd_close = closes[i-1]
            gap_rate = (opens[i] - yd_close) / yd_close * 100

            # 至少满足最低gap阈值
            if gap_rate < GAP_THRESHOLDS[0]:
                continue

            # 涨停开盘排除
            limit_up = round(precloses[i] * (1 + limit_ratio), 2)
            if opens[i] >= limit_up:
                continue

            # 市值
            if turns[i] > 0:
                float_cap = amounts[i] / (turns[i] / 100) / 1e8
            else:
                float_cap = np.nan
            cap_grp = get_cap_group(float_cap)
            if cap_grp is None:
                continue

            year = int(today_date[:4])

            # 计算收益
            # buy_price = today hour1_open
            buy_price = h1_opens[i]
            # T+1: next day's hour4_close
            ret_t1 = np.nan
            ret_t2 = np.nan
            if i + 1 < n and h4_closes[i+1] > 0:
                ret_t1 = (h4_closes[i+1] - buy_price) / buy_price * 100
            if i + 2 < n and h4_closes[i+2] > 0:
                ret_t2 = (h4_closes[i+2] - buy_price) / buy_price * 100

            if np.isnan(ret_t1):
                continue

            # ===== 检查共振条件 =====
            # yesterday's indicators (index i-1)
            yd_vol = volumes[i-1]
            yd_vol_ma5 = vol_ma5[i-1]
            yd_turn = turns[i-1]
            yd_turn_ma5 = turn_ma5[i-1]
            yd_ma5 = ma5[i-1]
            yd_ma10 = ma10[i-1]
            yd_ma5_prev = ma5[i-2] if i >= 2 else np.nan
            yd_ma10_prev = ma10[i-2] if i >= 2 else np.nan
            yd_amp = amplitude[i-1]
            yd_body = body[i-1]
            yd_pct = pct_chg[i-1]
            yd_high20 = high_20[i-1]
            yd_low20 = low_20[i-1]

            matched_conds = []

            # A: yesterday缩量
            if not np.isnan(yd_vol_ma5) and yd_vol_ma5 > 0 and yd_vol < yd_vol_ma5 * 0.7:
                matched_conds.append('A')

            # B: yesterday放量
            if not np.isnan(yd_vol_ma5) and yd_vol_ma5 > 0 and yd_vol > yd_vol_ma5 * 1.5:
                matched_conds.append('B')

            # C: yesterday站上MA5
            if not np.isnan(yd_ma5) and yd_close > yd_ma5:
                matched_conds.append('C')

            # D: yesterday在MA5下方但today open突破MA5
            if not np.isnan(yd_ma5) and yd_close < yd_ma5 and opens[i] > yd_ma5:
                matched_conds.append('D')

            # E: MA5金叉(yesterday: ma5>ma10, 前天: ma5<=ma10)
            if (not np.isnan(yd_ma5) and not np.isnan(yd_ma10) and
                not np.isnan(yd_ma5_prev) and not np.isnan(yd_ma10_prev)):
                if yd_ma5 > yd_ma10 and yd_ma5_prev <= yd_ma10_prev:
                    matched_conds.append('E')

            # F: yesterday十字星
            if yd_amp < 2.0 and yd_body < 1.0:
                matched_conds.append('F')

            # G: 前3日缩量横盘(振幅均<3%)
            if i >= 3 and amplitude[i-1] < 3.0 and amplitude[i-2] < 3.0 and amplitude[i-3] < 3.0:
                matched_conds.append('G')

            # H: yesterday大阳线(涨>3%)
            if yd_pct > 3.0:
                matched_conds.append('H')

            # I: yesterday换手率 > 5日均换手率×2
            if not np.isnan(yd_turn_ma5) and yd_turn_ma5 > 0 and yd_turn > yd_turn_ma5 * 2:
                matched_conds.append('I')

            # J: 前5日换手率持续低迷(5日均换手率 < 1%)
            if not np.isnan(yd_turn_ma5) and yd_turn_ma5 < 1.0:
                matched_conds.append('J')

            # K: 近20日低位(接近最低价10%以内)
            if not np.isnan(yd_low20) and yd_low20 > 0:
                if (yd_close - yd_low20) / yd_low20 < 0.10:
                    matched_conds.append('K')

            # L: 近20日高位
            if not np.isnan(yd_high20) and yd_high20 > 0:
                if yd_close >= yd_high20 * 0.98:
                    matched_conds.append('L')

            # 记录所有匹配的条件
            for cond in matched_conds:
                signals.append((cond, board, cap_grp, gap_rate, year, ret_t1, ret_t2))

    return signals


def main():
    log.info("=" * 60)
    log.info("  跳空高开+多指标共振 研究脚本启动")
    log.info(f"  数据范围: {DATE_START} ~ {DATE_END}")
    log.info(f"  Gap阈值: {GAP_THRESHOLDS}")
    log.info("=" * 60)

    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()

    # 获取所有交易日
    cur.execute("SELECT DISTINCT date FROM stock_kline WHERE date >= '2020-01-01' ORDER BY date")
    trading_days = [r[0] for r in cur.fetchall()]
    date_start_idx = next(i for i, d in enumerate(trading_days) if d >= DATE_START)
    log.info(f"Trading days: {len(trading_days)}, start index: {date_start_idx}")

    # 获取所有股票列表(排除纯B股等)
    cur.execute("""
        SELECT DISTINCT code FROM stock_kline
        WHERE date >= ? AND date <= ?
        AND (code LIKE 'sh.6%' OR code LIKE 'sz.0%' OR code LIKE 'sz.3%'
             OR code LIKE 'sh.688%' OR code LIKE 'bj.%')
        ORDER BY code
    """, (DATE_START, DATE_END))
    all_codes = [r[0] for r in cur.fetchall()]
    log.info(f"Total stocks to process: {len(all_codes)}")

    # 分块处理
    all_signals = []
    n_chunks = (len(all_codes) + CHUNK_SIZE - 1) // CHUNK_SIZE
    t0 = time.time()

    for chunk_i in range(n_chunks):
        chunk_codes = all_codes[chunk_i * CHUNK_SIZE: (chunk_i + 1) * CHUNK_SIZE]
        chunk_signals = process_stock_chunk(conn, chunk_codes, trading_days, date_start_idx)
        all_signals.extend(chunk_signals)
        elapsed = time.time() - t0
        eta = elapsed / (chunk_i + 1) * n_chunks - elapsed
        log.info(f"  Chunk {chunk_i+1}/{n_chunks}: {len(chunk_codes)} stocks, "
                 f"signals so far: {len(all_signals):,}, "
                 f"elapsed: {elapsed:.0f}s, ETA: {eta:.0f}s")

    conn.close()
    log.info(f"\nTotal signals collected: {len(all_signals):,}")

    # ===== 聚合统计 =====
    log.info("Aggregating results...")

    # 按 (cond, board, cap_group, gap_bucket) 分组统计
    # gap_bucket: 对应4个阈值
    results = []

    # 将signals转为结构化形式便于筛选
    # (cond, board, cap_grp, gap_rate, year, ret_t1, ret_t2)
    for gap_thresh in GAP_THRESHOLDS:
        for cond in COND_NAMES.keys():
            for board in BOARD_NAMES.keys():
                for cap in CAP_LABELS:
                    # 筛选匹配的信号
                    subset = [(s[4], s[5], s[6]) for s in all_signals
                              if s[0] == cond and s[1] == board and s[2] == cap
                              and s[3] >= gap_thresh]
                    n = len(subset)
                    if n < 10:
                        continue

                    years = [s[0] for s in subset]
                    ret1s = np.array([s[1] for s in subset])
                    ret2s = np.array([s[2] for s in subset if not np.isnan(s[2])])

                    # 分年统计
                    yearly_ret = defaultdict(list)
                    for y, r1, _ in subset:
                        yearly_ret[y].append(r1)
                    worst_year_ret = float('inf')
                    worst_year = ''
                    for y, rets in yearly_ret.items():
                        avg = np.mean(rets)
                        if avg < worst_year_ret:
                            worst_year_ret = avg
                            worst_year = y

                    results.append({
                        'condition': COND_NAMES[cond],
                        'board': BOARD_NAMES[board],
                        'cap': cap,
                        'gap': f"{gap_thresh}%",
                        'samples': n,
                        'ret_t1_mean': np.mean(ret1s),
                        'ret_t1_median': np.median(ret1s),
                        'ret_t1_win': (ret1s > 0).sum() / n * 100,
                        'ret_t2_mean': np.mean(ret2s) if len(ret2s) > 0 else np.nan,
                        'ret_t2_win': (ret2s > 0).sum() / len(ret2s) * 100 if len(ret2s) > 0 else np.nan,
                        'worst_year': f"{worst_year}({worst_year_ret:.2f}%)",
                        'worst_year_ret': worst_year_ret,
                    })

    # 计算score并排序
    for r in results:
        r['score'] = r['ret_t1_mean'] * r['ret_t1_win'] / 100

    results.sort(key=lambda x: x['score'], reverse=True)

    # ===== 输出 =====
    sep = '-' * 130
    header = (f"{'共振条件':<16} {'板块':<6} {'市值':<10} {'Gap':<5} "
              f"{'样本':>6} {'T+1收益':>8} {'T+1胜率':>8} {'T+1中位':>8} "
              f"{'T+2收益':>8} {'T+2胜率':>8} {'最差年':<18}")

    log.info("\n" + sep)
    log.info("全量对比表（按score=收益×胜率排序，样本>=10）")
    log.info(sep)
    log.info(header)
    log.info(sep)

    for r in results[:100]:
        t2_str = f"{r['ret_t2_mean']:>7.2f}%" if not np.isnan(r['ret_t2_mean']) else '    N/A'
        t2w_str = f"{r['ret_t2_win']:>7.1f}%" if not np.isnan(r['ret_t2_win']) else '    N/A'
        line = (f"{r['condition']:<16} {r['board']:<6} {r['cap']:<10} {r['gap']:<5} "
                f"{r['samples']:>6} {r['ret_t1_mean']:>7.2f}% {r['ret_t1_win']:>7.1f}% "
                f"{r['ret_t1_median']:>7.2f}% {t2_str} "
                f"{t2w_str} {r['worst_year']:<18}")
        log.info(line)

    # Top 20
    log.info("\n" + "=" * 70)
    log.info("  TOP 20 最优共振组合")
    log.info("  筛选: 样本>50, 胜率>55%, 最差年平均>-3%")
    log.info("=" * 70)

    top_results = [r for r in results
                   if r['samples'] > 50
                   and r['ret_t1_win'] > 55
                   and r['worst_year_ret'] > -3.0]

    log.info(f"符合严格条件的组合数: {len(top_results)}")

    if not top_results:
        log.info("严格条件无结果，放宽至：样本>30, 胜率>52%, 最差年>-5%")
        top_results = [r for r in results
                       if r['samples'] > 30
                       and r['ret_t1_win'] > 52
                       and r['worst_year_ret'] > -5.0]
        log.info(f"放宽后: {len(top_results)}")

    if not top_results:
        log.info("再次放宽：样本>20, 胜率>50%")
        top_results = [r for r in results
                       if r['samples'] > 20
                       and r['ret_t1_win'] > 50]
        log.info(f"再放宽后: {len(top_results)}")

    log.info(sep)
    log.info(header)
    log.info(sep)
    for i, r in enumerate(top_results[:TOP_N], 1):
        t2_str = f"{r['ret_t2_mean']:>7.2f}%" if not np.isnan(r['ret_t2_mean']) else '    N/A'
        t2w_str = f"{r['ret_t2_win']:>7.1f}%" if not np.isnan(r['ret_t2_win']) else '    N/A'
        line = (f"#{i:<2} {r['condition']:<14} {r['board']:<6} {r['cap']:<10} {r['gap']:<5} "
                f"{r['samples']:>6} {r['ret_t1_mean']:>7.2f}% {r['ret_t1_win']:>7.1f}% "
                f"{r['ret_t1_median']:>7.2f}% {t2_str} "
                f"{t2w_str} {r['worst_year']:<18}")
        log.info(line)

    # 汇总：各条件不分板块市值的表现
    log.info("\n" + "=" * 70)
    log.info("  各共振条件总体汇总（不分板块市值）")
    log.info("=" * 70)

    for gap_thresh in GAP_THRESHOLDS:
        log.info(f"\n--- Gap >= {gap_thresh}% ---")
        log.info(f"{'条件':<16} {'总样本':>8} {'加权T+1收益':>10} {'加权T+1胜率':>10}")
        log.info("-" * 50)
        for cond in COND_NAMES.keys():
            subset_sigs = [(s[5], s[6]) for s in all_signals
                           if s[0] == cond and s[3] >= gap_thresh]
            n = len(subset_sigs)
            if n == 0:
                continue
            ret1s = np.array([s[0] for s in subset_sigs])
            avg_ret = np.mean(ret1s)
            win_rate = (ret1s > 0).sum() / n * 100
            log.info(f"{COND_NAMES[cond]:<16} {n:>8} {avg_ret:>9.2f}% {win_rate:>9.1f}%")

    log.info("\n" + "=" * 60)
    log.info("  研究完成！")
    log.info("=" * 60)


if __name__ == '__main__':
    main()
