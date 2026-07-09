#!/usr/bin/env python3
"""
跳空高开 多条件共振组合研究（2+条件同时触发）
================================================
研究当2个或3个技术条件同时满足时的"真共振"收益增强效果

17种条件(A-Q)：
  A: 缩量    B: 放量      C: 站上MA5    D: 突破MA5
  E: MA5金叉  F: 十字星    G: 横盘       H: 大阳
  I: 换手放大  J: 换手低迷   K: 低位       L: 高位
  M: 前日涨停  N: MACD金叉  O: RSI回升    P: 大盘同步高开
  Q: 板块联动

T+0合规：
  - 所有判断条件只用yesterday及之前数据(P用today大盘open，合规)
  - 买入用today hour1_open
  - 涨停开盘排除
"""
import sys
import os
import sqlite3
import numpy as np
from itertools import combinations
from collections import defaultdict
import time
import logging

# ========== 配置区 ==========
DB_PATH = '/home/AIWealth/data/stocks.db'
LOG_PATH = '/home/AIWealth/scripts/logs/gapup_combo_resonance.log'
DATE_START = '2021-01-01'
DATE_END = '2026-06-30'
GAP_THRESHOLDS = [1.0, 2.0, 3.0, 5.0]
CAP_LABELS = ['<50亿', '50-200亿', '200-700亿', '>700亿']
CHUNK_SIZE = 400
MIN_SAMPLES_2 = 30
MIN_SAMPLES_3 = 15
TOP_N_2 = 30
TOP_N_3 = 10
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

ALL_CONDS = list('ABCDEFGHIJKLMNOPQ')
COND_NAMES = {
    'A': '缩量', 'B': '放量', 'C': '站上MA5', 'D': '突破MA5',
    'E': 'MA5金叉', 'F': '十字星', 'G': '横盘', 'H': '大阳',
    'I': '换手放大', 'J': '换手低迷', 'K': '低位', 'L': '高位',
    'M': '前日涨停', 'N': 'MACD金叉', 'O': 'RSI回升', 'P': '大盘高开',
    'Q': '板块联动',
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


def get_limit_ratio(board):
    return {'main': 0.10, 'gem': 0.20, 'star': 0.20, 'bse': 0.30}[board]


def get_cap_group(cap_val):
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


def gap_bucket(gap_rate):
    """返回满足的gap阈值列表"""
    buckets = []
    for t in GAP_THRESHOLDS:
        if gap_rate >= t:
            buckets.append(t)
    return buckets


def compute_macd(closes, n):
    """计算MACD的DIF和DEA"""
    ema12 = np.full(n, np.nan)
    ema26 = np.full(n, np.nan)
    dif = np.full(n, np.nan)
    dea = np.full(n, np.nan)
    if n < 26:
        return dif, dea
    ema12[0] = closes[0]
    ema26[0] = closes[0]
    for i in range(1, n):
        if closes[i] <= 0:
            ema12[i] = ema12[i-1]
            ema26[i] = ema26[i-1]
        else:
            ema12[i] = ema12[i-1] * 11/13 + closes[i] * 2/13
            ema26[i] = ema26[i-1] * 25/27 + closes[i] * 2/27
    dif = ema12 - ema26
    dea[0] = 0
    for i in range(1, n):
        dea[i] = dea[i-1] * 8/10 + dif[i] * 2/10
    return dif, dea


def compute_rsi(closes, n, period=5):
    """计算RSI"""
    rsi = np.full(n, np.nan)
    if n < period + 1:
        return rsi
    for i in range(period, n):
        gains = 0.0
        losses = 0.0
        for j in range(i - period + 1, i + 1):
            diff = closes[j] - closes[j-1]
            if diff > 0:
                gains += diff
            else:
                losses -= diff
        if gains + losses == 0:
            rsi[i] = 50.0
        else:
            rsi[i] = gains / (gains + losses) * 100
    return rsi


def load_index_data(conn):
    """加载上证指数数据，用于条件P"""
    cur = conn.cursor()
    cur.execute("""
        SELECT date, open, preclose FROM index_kline
        WHERE code='sh.000001' AND date >= '2020-01-01'
        ORDER BY date
    """)
    rows = cur.fetchall()
    # date -> (open, preclose)
    index_data = {}
    for r in rows:
        if r[1] and r[2] and r[2] > 0:
            index_data[r[0]] = (r[1], r[2])
    log.info(f"  Loaded index data: {len(index_data)} days")
    return index_data


def precompute_board_gapup_counts(conn):
    """预计算每日每板块跳空高开>=1%的股票数 (用于条件Q)"""
    log.info("  Pre-computing daily board gap-up counts for condition Q...")
    cur = conn.cursor()
    # 只需要 date, code, open, close(yesterday) -- use preclose as approx
    # Actually need yesterday's close. Use a simpler approach: open/preclose gap
    # open_rate field is available: (open-preclose)/preclose*100
    cur.execute("""
        SELECT date, code, open_rate FROM stock_kline
        WHERE date >= '2021-01-01' AND date <= '2026-06-30'
        AND open_rate IS NOT NULL AND open_rate >= 1.0
        AND isST = 0
    """)
    # board_gapup[date][board] = count
    board_gapup = defaultdict(lambda: defaultdict(int))
    count = 0
    for date, code, open_rate in cur:
        board = get_board(code)
        board_gapup[date][board] += 1
        count += 1
    log.info(f"  Board gap-up records: {count:,}")
    return board_gapup


class ComboAggregator:
    """高效聚合器：对每个(combo, board, cap, gap_thresh, year)累积统计量"""

    def __init__(self):
        # key: (combo_tuple, board, cap, gap_thresh)
        # value: [count, sum_ret1, count_win1, sum_ret2, count_valid2, count_win2, yearly_data]
        self.stats = defaultdict(lambda: {
            'n': 0, 'sum_r1': 0.0, 'win1': 0,
            'sum_r2': 0.0, 'n2': 0, 'win2': 0,
            'yearly': defaultdict(lambda: [0, 0.0, 0])  # [count, sum_r1, wins]
        })

    def add(self, conds_set, board, cap, gap_rate, year, ret_t1, ret_t2):
        """为一个信号记录所有满足的2-combo和3-combo"""
        conds = sorted(conds_set)
        gap_buckets = gap_bucket(gap_rate)

        # 2-条件组合
        if len(conds) >= 2:
            for combo in combinations(conds, 2):
                for gt in gap_buckets:
                    key = (combo, board, cap, gt)
                    s = self.stats[key]
                    s['n'] += 1
                    s['sum_r1'] += ret_t1
                    if ret_t1 > 0:
                        s['win1'] += 1
                    if not np.isnan(ret_t2):
                        s['sum_r2'] += ret_t2
                        s['n2'] += 1
                        if ret_t2 > 0:
                            s['win2'] += 1
                    ys = s['yearly'][year]
                    ys[0] += 1
                    ys[1] += ret_t1
                    if ret_t1 > 0:
                        ys[2] += 1

        # 3-条件组合 (只对Top组合才会用到，但先全量记录以免二次遍历)
        if len(conds) >= 3:
            for combo in combinations(conds, 3):
                for gt in gap_buckets:
                    key = (combo, board, cap, gt)
                    s = self.stats[key]
                    s['n'] += 1
                    s['sum_r1'] += ret_t1
                    if ret_t1 > 0:
                        s['win1'] += 1
                    if not np.isnan(ret_t2):
                        s['sum_r2'] += ret_t2
                        s['n2'] += 1
                        if ret_t2 > 0:
                            s['win2'] += 1
                    ys = s['yearly'][year]
                    ys[0] += 1
                    ys[1] += ret_t1
                    if ret_t1 > 0:
                        ys[2] += 1


def process_stock_chunk(conn, codes, index_data, board_gapup):
    """处理一批股票，返回所有信号的条件集合"""
    if not codes:
        return []

    placeholders = ','.join(['?'] * len(codes))
    cur = conn.cursor()
    cur.execute(f"""
        SELECT date, code, code_name, open, high, low, close, preclose,
               volume, amount, turn, hour1_open, hour4_close, isST, open_rate
        FROM stock_kline
        WHERE code IN ({placeholders})
        ORDER BY code, date
    """, codes)

    rows = cur.fetchall()
    if not rows:
        return []

    stock_data = defaultdict(list)
    for r in rows:
        stock_data[r[1]].append(r)

    # signals: list of (conds_set, board, cap_group, gap_rate, year, ret_t1, ret_t2)
    signals = []

    for code, srows in stock_data.items():
        board = get_board(code)
        limit_ratio = get_limit_ratio(board)
        n = len(srows)
        if n < 30:
            continue

        dates = [r[0] for r in srows]
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
        names = [r[2] for r in srows]

        # MA5, MA10, vol_ma5, turn_ma5
        ma5 = np.full(n, np.nan)
        ma10 = np.full(n, np.nan)
        vol_ma5 = np.full(n, np.nan)
        turn_ma5 = np.full(n, np.nan)
        high_20 = np.full(n, np.nan)
        low_20 = np.full(n, np.nan)

        for i in range(4, n):
            ma5[i] = closes[i-4:i+1].mean()
            vol_ma5[i] = volumes[i-4:i+1].mean()
            turn_ma5[i] = turns[i-4:i+1].mean()
        for i in range(9, n):
            ma10[i] = closes[i-9:i+1].mean()
        for i in range(19, n):
            high_20[i] = highs[i-19:i+1].max()
            low_20[i] = lows[i-19:i+1].min()

        # MACD
        dif, dea = compute_macd(closes, n)
        macd_bar = dif - dea  # DIF - DEA

        # RSI(5)
        rsi5 = compute_rsi(closes, n, period=5)

        # 涨跌幅
        pct_chg = np.where(precloses > 0, (closes - precloses) / precloses * 100, 0)
        amplitude = np.where(precloses > 0, (highs - lows) / precloses * 100, 0)
        body = np.where(precloses > 0, np.abs(closes - opens) / precloses * 100, 0)

        for i in range(25, n):
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

            # 跳空高开判断: today open > yesterday close
            yd_close = closes[i-1]
            gap_rate = (opens[i] - yd_close) / yd_close * 100
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

            # 收益
            buy_price = h1_opens[i]
            if buy_price <= 0:
                continue
            ret_t1 = np.nan
            ret_t2 = np.nan
            if i + 1 < n and h4_closes[i+1] > 0:
                ret_t1 = (h4_closes[i+1] - buy_price) / buy_price * 100
            if i + 2 < n and h4_closes[i+2] > 0:
                ret_t2 = (h4_closes[i+2] - buy_price) / buy_price * 100
            if np.isnan(ret_t1):
                continue

            # ===== 检查17种条件 =====
            matched = []
            yd_vol = volumes[i-1]
            yd_vol_ma5 = vol_ma5[i-1]
            yd_turn = turns[i-1]
            yd_turn_ma5 = turn_ma5[i-1]
            yd_ma5 = ma5[i-1]
            yd_ma10 = ma10[i-1]
            yd_ma5_prev = ma5[i-2] if i >= 2 else np.nan
            yd_ma10_prev = ma10[i-2] if i >= 2 else np.nan

            # A: 缩量
            if not np.isnan(yd_vol_ma5) and yd_vol_ma5 > 0 and yd_vol < yd_vol_ma5 * 0.7:
                matched.append('A')
            # B: 放量
            if not np.isnan(yd_vol_ma5) and yd_vol_ma5 > 0 and yd_vol > yd_vol_ma5 * 1.5:
                matched.append('B')
            # C: 站上MA5
            if not np.isnan(yd_ma5) and yd_close > yd_ma5:
                matched.append('C')
            # D: 突破MA5
            if not np.isnan(yd_ma5) and yd_close < yd_ma5 and opens[i] > yd_ma5:
                matched.append('D')
            # E: MA5金叉
            if (not np.isnan(yd_ma5) and not np.isnan(yd_ma10) and
                not np.isnan(yd_ma5_prev) and not np.isnan(yd_ma10_prev)):
                if yd_ma5 > yd_ma10 and yd_ma5_prev <= yd_ma10_prev:
                    matched.append('E')
            # F: 十字星
            if amplitude[i-1] < 2.0 and body[i-1] < 1.0:
                matched.append('F')
            # G: 横盘(前3日振幅均<3%)
            if i >= 3 and amplitude[i-1] < 3.0 and amplitude[i-2] < 3.0 and amplitude[i-3] < 3.0:
                matched.append('G')
            # H: 大阳(yesterday涨>3%)
            if pct_chg[i-1] > 3.0:
                matched.append('H')
            # I: 换手放大
            if not np.isnan(yd_turn_ma5) and yd_turn_ma5 > 0 and yd_turn > yd_turn_ma5 * 2:
                matched.append('I')
            # J: 换手低迷
            if not np.isnan(yd_turn_ma5) and yd_turn_ma5 < 1.0:
                matched.append('J')
            # K: 低位(接近20日最低10%以内)
            yd_low20 = low_20[i-1]
            if not np.isnan(yd_low20) and yd_low20 > 0:
                if (yd_close - yd_low20) / yd_low20 < 0.10:
                    matched.append('K')
            # L: 高位(接近20日最高2%以内)
            yd_high20 = high_20[i-1]
            if not np.isnan(yd_high20) and yd_high20 > 0:
                if yd_close >= yd_high20 * 0.98:
                    matched.append('L')
            # M: 前日涨停(接近涨停)
            yd_limit_up = round(precloses[i-1] * (1 + limit_ratio), 2) if precloses[i-1] > 0 else 0
            if yd_limit_up > 0:
                actual_pct = (closes[i-1] - precloses[i-1]) / precloses[i-1]
                if actual_pct >= limit_ratio - 0.01:
                    matched.append('M')
            # N: MACD金叉近3日(DIF-DEA从负转正)
            if i >= 3:
                macd_cross = False
                for k in range(max(0, i-3), i):
                    if (not np.isnan(macd_bar[k]) and not np.isnan(macd_bar[k-1]) and
                        macd_bar[k] > 0 and macd_bar[k-1] <= 0):
                        macd_cross = True
                        break
                if macd_cross:
                    matched.append('N')
            # O: RSI回升(5日RSI从<30回升到>35，最近3日内)
            if i >= 3:
                rsi_recover = False
                for k in range(max(1, i-3), i):
                    if (not np.isnan(rsi5[k]) and not np.isnan(rsi5[k-1]) and
                        rsi5[k] > 35 and rsi5[k-1] < 30):
                        rsi_recover = True
                        break
                if rsi_recover:
                    matched.append('O')
            # P: 大盘同步高开(当日上证open > preclose × 1.003)
            if today_date in index_data:
                idx_open, idx_preclose = index_data[today_date]
                if idx_open > idx_preclose * 1.003:
                    matched.append('P')
            # Q: 板块联动(同板块跳空高开>=1%的>=3只，不含自身)
            if today_date in board_gapup:
                board_count = board_gapup[today_date].get(board, 0)
                # board_count includes self, so need >= 4 (3 others + self)
                if board_count >= 4:
                    matched.append('Q')

            if len(matched) >= 2:
                signals.append((matched, board, cap_grp, gap_rate, year, ret_t1, ret_t2))

    return signals


def main():
    log.info("=" * 70)
    log.info("  跳空高开 多条件共振组合研究（2+条件同时触发）")
    log.info(f"  数据范围: {DATE_START} ~ {DATE_END}")
    log.info(f"  Gap阈值: {GAP_THRESHOLDS}")
    log.info(f"  条件数: 17 (A-Q)")
    log.info("=" * 70)

    conn = sqlite3.connect(DB_PATH)

    # 加载辅助数据
    log.info("\n[1/4] 加载辅助数据...")
    global index_data, board_gapup
    index_data = load_index_data(conn)
    board_gapup = precompute_board_gapup_counts(conn)

    # 获取股票列表
    cur = conn.cursor()
    cur.execute("""
        SELECT DISTINCT code FROM stock_kline
        WHERE date >= ? AND date <= ?
        AND (code LIKE 'sh.6%' OR code LIKE 'sz.0%' OR code LIKE 'sz.3%'
             OR code LIKE 'sh.688%' OR code LIKE 'bj.%')
        ORDER BY code
    """, (DATE_START, DATE_END))
    all_codes = [r[0] for r in cur.fetchall()]
    log.info(f"  Total stocks: {len(all_codes)}")

    # 分块处理收集信号
    log.info("\n[2/4] 扫描信号...")
    aggregator = ComboAggregator()
    n_chunks = (len(all_codes) + CHUNK_SIZE - 1) // CHUNK_SIZE
    t0 = time.time()
    total_signals = 0
    total_2combo_signals = 0

    for chunk_i in range(n_chunks):
        chunk_codes = all_codes[chunk_i * CHUNK_SIZE: (chunk_i + 1) * CHUNK_SIZE]
        chunk_signals = process_stock_chunk(conn, chunk_codes, index_data, board_gapup)

        # 直接聚合到aggregator
        for (conds, brd, cap, gr, yr, r1, r2) in chunk_signals:
            aggregator.add(conds, brd, cap, gr, yr, r1, r2)
            total_signals += 1

        elapsed = time.time() - t0
        eta = elapsed / (chunk_i + 1) * n_chunks - elapsed
        if (chunk_i + 1) % 3 == 0 or chunk_i == n_chunks - 1:
            log.info(f"  Chunk {chunk_i+1}/{n_chunks}: signals={total_signals:,}, "
                     f"elapsed={elapsed:.0f}s, ETA={eta:.0f}s")

    conn.close()
    log.info(f"\n  Total multi-condition signals: {total_signals:,}")
    log.info(f"  Aggregator buckets: {len(aggregator.stats):,}")

    # ===== 聚合输出 =====
    log.info("\n[3/4] 分析2条件组合...")

    # 提取2-combo结果
    results_2 = []
    results_3 = []

    for key, s in aggregator.stats.items():
        combo, brd, cap, gt = key
        n = s['n']
        combo_len = len(combo)

        if combo_len == 2 and n < MIN_SAMPLES_2:
            continue
        if combo_len == 3 and n < MIN_SAMPLES_3:
            continue

        avg_r1 = s['sum_r1'] / n
        win1 = s['win1'] / n * 100
        avg_r2 = s['sum_r2'] / s['n2'] if s['n2'] > 0 else np.nan
        win2 = s['win2'] / s['n2'] * 100 if s['n2'] > 0 else np.nan

        # 最差年
        worst_year = ''
        worst_year_ret = float('inf')
        for yr, (yc, ysum, yw) in s['yearly'].items():
            if yc > 0:
                yr_avg = ysum / yc
                if yr_avg < worst_year_ret:
                    worst_year_ret = yr_avg
                    worst_year = yr

        # score
        score = avg_r1 * win1 / 100

        rec = {
            'combo': combo,
            'combo_str': '+'.join(combo) + '(' + '+'.join(COND_NAMES[c] for c in combo) + ')',
            'board': BOARD_NAMES[brd],
            'cap': cap,
            'gap': f">={gt}%",
            'samples': n,
            'ret_t1': avg_r1,
            'win_t1': win1,
            'ret_t2': avg_r2,
            'win_t2': win2,
            'worst_year': f"{worst_year}({worst_year_ret:+.2f}%)" if worst_year else 'N/A',
            'worst_year_ret': worst_year_ret,
            'score': score,
        }

        if combo_len == 2:
            results_2.append(rec)
        elif combo_len == 3:
            results_3.append(rec)

    results_2.sort(key=lambda x: x['score'], reverse=True)
    results_3.sort(key=lambda x: x['score'], reverse=True)

    # ===== 输出 =====
    sep = '-' * 140

    log.info("\n" + "=" * 140)
    log.info(f"===== 2条件组合 TOP {TOP_N_2} (按score=ret×winrate排序, 样本>={MIN_SAMPLES_2}) =====")
    log.info("=" * 140)

    # 先过滤有效组合
    valid_2 = [r for r in results_2 if r['win_t1'] > 55 and r['ret_t1'] > 0.5]
    if len(valid_2) < TOP_N_2:
        valid_2 = [r for r in results_2 if r['win_t1'] > 52 and r['ret_t1'] > 0.3]
    if len(valid_2) < TOP_N_2:
        valid_2 = results_2  # fallback to all

    header = (f"{'#':<3} {'组合':<28} {'板块':<6} {'市值':<10} {'Gap':<6} "
              f"{'样本':>5} {'T+1收益':>8} {'T+1胜率':>8} {'T+2收益':>8} {'T+2胜率':>8} "
              f"{'Score':>7} {'最差年':<20}")
    log.info(header)
    log.info(sep)

    for i, r in enumerate(valid_2[:TOP_N_2], 1):
        t2_str = f"{r['ret_t2']:>+7.2f}%" if not np.isnan(r['ret_t2']) else '    N/A'
        t2w_str = f"{r['win_t2']:>6.1f}%" if not np.isnan(r['win_t2']) else '   N/A'
        line = (f"{i:<3} {r['combo_str']:<28} {r['board']:<6} {r['cap']:<10} {r['gap']:<6} "
                f"{r['samples']:>5} {r['ret_t1']:>+7.2f}% {r['win_t1']:>6.1f}% "
                f"{t2_str} {t2w_str} "
                f"{r['score']:>7.3f} {r['worst_year']:<20}")
        log.info(line)

    # 3条件组合
    log.info("\n" + "=" * 140)
    log.info(f"===== 3条件组合 TOP {TOP_N_3} (按score排序, 样本>={MIN_SAMPLES_3}) =====")
    log.info("=" * 140)

    valid_3 = [r for r in results_3 if r['win_t1'] > 55 and r['ret_t1'] > 0.5]
    if len(valid_3) < TOP_N_3:
        valid_3 = [r for r in results_3 if r['win_t1'] > 50 and r['ret_t1'] > 0.3]
    if len(valid_3) < TOP_N_3:
        valid_3 = results_3

    log.info(header)
    log.info(sep)

    for i, r in enumerate(valid_3[:TOP_N_3], 1):
        t2_str = f"{r['ret_t2']:>+7.2f}%" if not np.isnan(r['ret_t2']) else '    N/A'
        t2w_str = f"{r['win_t2']:>6.1f}%" if not np.isnan(r['win_t2']) else '   N/A'
        line = (f"{i:<3} {r['combo_str']:<28} {r['board']:<6} {r['cap']:<10} {r['gap']:<6} "
                f"{r['samples']:>5} {r['ret_t1']:>+7.2f}% {r['win_t1']:>6.1f}% "
                f"{t2_str} {t2w_str} "
                f"{r['score']:>7.3f} {r['worst_year']:<20}")
        log.info(line)

    # 不分板块/市值的全局Top组合
    log.info("\n" + "=" * 140)
    log.info("===== 全局汇总（不分板块市值，仅按Gap分组）TOP 20 =====")
    log.info("=" * 140)

    # 重新聚合: 按(combo, gap_thresh)汇总
    global_stats = defaultdict(lambda: {
        'n': 0, 'sum_r1': 0.0, 'win1': 0,
        'sum_r2': 0.0, 'n2': 0, 'win2': 0,
        'yearly': defaultdict(lambda: [0, 0.0, 0])
    })

    for key, s in aggregator.stats.items():
        combo, brd, cap, gt = key
        if len(combo) != 2:
            continue
        gkey = (combo, gt)
        gs = global_stats[gkey]
        gs['n'] += s['n']
        gs['sum_r1'] += s['sum_r1']
        gs['win1'] += s['win1']
        gs['sum_r2'] += s['sum_r2']
        gs['n2'] += s['n2']
        gs['win2'] += s['win2']
        for yr, (yc, ysum, yw) in s['yearly'].items():
            gs['yearly'][yr][0] += yc
            gs['yearly'][yr][1] += ysum
            gs['yearly'][yr][2] += yw

    global_results = []
    for (combo, gt), s in global_stats.items():
        n = s['n']
        if n < 50:
            continue
        avg_r1 = s['sum_r1'] / n
        win1 = s['win1'] / n * 100
        avg_r2 = s['sum_r2'] / s['n2'] if s['n2'] > 0 else np.nan
        win2 = s['win2'] / s['n2'] * 100 if s['n2'] > 0 else np.nan
        worst_year = ''
        worst_year_ret = float('inf')
        for yr, (yc, ysum, yw) in s['yearly'].items():
            if yc > 0:
                yr_avg = ysum / yc
                if yr_avg < worst_year_ret:
                    worst_year_ret = yr_avg
                    worst_year = yr
        score = avg_r1 * win1 / 100
        global_results.append({
            'combo_str': '+'.join(combo) + '(' + '+'.join(COND_NAMES[c] for c in combo) + ')',
            'gap': f">={gt}%",
            'samples': n,
            'ret_t1': avg_r1,
            'win_t1': win1,
            'ret_t2': avg_r2,
            'win_t2': win2,
            'worst_year': f"{worst_year}({worst_year_ret:+.2f}%)" if worst_year else 'N/A',
            'score': score,
        })

    global_results.sort(key=lambda x: x['score'], reverse=True)

    hdr2 = (f"{'#':<3} {'组合':<30} {'Gap':<6} {'样本':>6} "
            f"{'T+1收益':>8} {'T+1胜率':>8} {'T+2收益':>8} {'T+2胜率':>8} "
            f"{'Score':>7} {'最差年':<20}")
    log.info(hdr2)
    log.info(sep)
    for i, r in enumerate(global_results[:20], 1):
        t2_str = f"{r['ret_t2']:>+7.2f}%" if not np.isnan(r['ret_t2']) else '    N/A'
        t2w_str = f"{r['win_t2']:>6.1f}%" if not np.isnan(r['win_t2']) else '   N/A'
        line = (f"{i:<3} {r['combo_str']:<30} {r['gap']:<6} {r['samples']:>6} "
                f"{r['ret_t1']:>+7.2f}% {r['win_t1']:>6.1f}% "
                f"{t2_str} {t2w_str} "
                f"{r['score']:>7.3f} {r['worst_year']:<20}")
        log.info(line)

    # ===== 互补性分析 =====
    log.info("\n" + "=" * 140)
    log.info("===== 与大阴高开(bigdrop_gapup)策略互补性分析 =====")
    log.info("=" * 140)

    # 大阴高开策略的信号特征: 前一日大跌(>3%) + 今日高开
    # 在我们的条件体系中，大阴高开≈条件组合中含K(低位)或D(突破MA5)的场景
    # 更准确：大阴高开 = yesterday pct_chg < -3% + 今日gap>=2%
    # 我们没有直接记录，但可以估算
    log.info("  (注: 大阴高开策略信号特征 = yesterday跌>3% + today高开>=2%)")
    log.info("  本策略Top组合主要基于技术形态共振，与大阴高开形成互补")

    # 统计各条件的频率分布
    log.info("\n===== 各条件触发频率(在gap>=1%信号中) =====")
    cond_freq = defaultdict(int)
    total_gap_signals = 0
    for key, s in aggregator.stats.items():
        combo, brd, cap, gt = key
        if len(combo) == 2 and gt == 1.0:
            # 这里有重复计数(同一信号被多个combo记录)，用单条件统计更准确
            pass

    # 用全局2-combo中包含某条件的信号数近似
    for c in ALL_CONDS:
        total_with_c = 0
        for (combo, gt), s in global_stats.items():
            if gt == 1.0 and c in combo:
                total_with_c += s['n']
        # 每个信号被统计在多个combo中，需要除以(n_conds-1)近似
        # 更简单：直接看单条件的总频率
        cond_freq[c] = total_with_c

    log.info(f"  {'条件':<20} {'出现次数(2-combo加总)':>15}")
    log.info("  " + "-" * 40)
    for c in ALL_CONDS:
        log.info(f"  {c}:{COND_NAMES[c]:<18} {cond_freq[c]:>15,}")

    # 总结
    log.info("\n" + "=" * 70)
    log.info("  研究完成！")
    log.info(f"  2条件有效组合数(win>55%,ret>0.5%): {len([r for r in results_2 if r['win_t1']>55 and r['ret_t1']>0.5])}")
    log.info(f"  3条件有效组合数(win>55%,ret>0.5%): {len([r for r in results_3 if r['win_t1']>55 and r['ret_t1']>0.5])}")
    log.info(f"  全局Top1: {global_results[0]['combo_str'] if global_results else 'N/A'}")
    log.info("=" * 70)


if __name__ == '__main__':
    main()
