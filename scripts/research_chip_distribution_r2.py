#\!/usr/bin/env python3
"""
筹码分布模型 + 持仓者心理推断 + 拉升信号验证 (rule2 流程) —— Task #80
用法: python3 research_chip_distribution_r2.py [2024-09 | all | verify]
输出日志: /home/AIWealth/scripts/logs/chip_distribution_r2.log
"""
import sys
import sqlite3
import numpy as np
from collections import deque

DB_PATH = '/home/AIWealth/data/stocks.db'
LOG_PATH = '/home/AIWealth/scripts/logs/chip_distribution_r2.log'

LOOKBACK = 20
N_BINS = 100
MAX_MKTCAP_YI = 200.0
MIN_MKTCAP_YI = 5.0

S1_PROFIT_MIN = 0.85
S1_TURN5_MAX = 1.0
S3_OLD_PROFIT_MAX = 0.30
S3_NEW_PROFIT_MIN = 0.70
S4_COST_GAP_MIN = 20.0
S4_CONCENTR_MAX = 0.15
S5_NEAR_MAX = 0.05
S5_PROFIT_LO = 0.30
S5_PROFIT_HI = 0.70

DETAIL_LIMIT_PER_SIGNAL = 30
START_YEAR = 2021
END_YEAR = 2026

SIGNAL_NAMES = {
    'S1': 'S1 高获利+低换手(强手惜售)',
    'S2': 'S2 突破筹码峰(阻力消化)',
    'S3': 'S3 套牢转获利(比价翻转)',
    'S4': 'S4 集中低成本(龙回头变体)',
    'S5': 'S5 筹码真空区(V形快速移动)',
}


class Logger:
    def __init__(self, path):
        self.f = open(path, 'w', encoding='utf-8')

    def log(self, msg=''):
        print(msg)
        self.f.write(str(msg) + '\n')
        self.f.flush()

    def close(self):
        self.f.close()


def get_limit_ratio(code):
    if code.startswith('sz.300') or code.startswith('sz.301'):
        return 0.20
    elif code.startswith('sh.688'):
        return 0.20
    elif code.startswith('bj.'):
        return 0.30
    else:
        return 0.10


def is_limit_up(close, preclose, code):
    if close is None or preclose is None or preclose <= 0:
        return False
    return round(close / preclose, 2) >= round(1 + get_limit_ratio(code), 2)


def build_chip_distribution(lows, highs, turns):
    price_min = float(np.min(lows))
    price_max = float(np.max(highs))
    if price_max <= price_min:
        price_max = price_min * 1.001 + 0.01
    bins = np.linspace(price_min, price_max, N_BINS + 1)
    bin_centers = (bins[:-1] + bins[1:]) / 2.0

    dist = np.ones(N_BINS) / N_BINS
    for k in range(len(lows)):
        turn = turns[k] / 100.0
        if turn <= 0 or turn > 1:
            turn = 0.01
        low, high = lows[k], highs[k]
        day_mask = (bin_centers >= low) & (bin_centers <= high)
        day_dist = day_mask.astype(float)
        s = day_dist.sum()
        if s > 0:
            day_dist /= s
        else:
            idx = int(np.argmin(np.abs(bin_centers - (low + high) / 2.0)))
            day_dist[idx] = 1.0
        dist = dist * (1 - turn) + day_dist * turn

    ssum = dist.sum()
    if ssum > 0:
        dist /= ssum
    return bin_centers, dist


def calc_chip_metrics(bin_centers, dist, current_price):
    profit_mask = bin_centers < current_price
    profit_ratio = float(dist[profit_mask].sum())
    trapped_ratio = 1.0 - profit_ratio

    avg_cost = float((bin_centers * dist).sum())
    if avg_cost > 0:
        var = float(((bin_centers - avg_cost) ** 2 * dist).sum())
        concentration = (var ** 0.5) / avg_cost
    else:
        concentration = 999.0

    near_mask = (bin_centers > current_price * 0.97) & (bin_centers < current_price * 1.03)
    near_breakeven = float(dist[near_mask].sum())

    peak_idx = int(dist.argmax())
    peak_price = float(bin_centers[peak_idx])
    distance_to_peak = (current_price - peak_price) / peak_price * 100 if peak_price > 0 else 0.0

    return {
        'profit_ratio': profit_ratio,
        'trapped_ratio': trapped_ratio,
        'avg_cost': avg_cost,
        'concentration': concentration,
        'near_breakeven': near_breakeven,
        'peak_price': peak_price,
        'distance_to_peak': distance_to_peak,
    }


def get_qualifying_codes(cur):
    cur.execute("""
        SELECT DISTINCT code FROM stock_kline
        WHERE (code LIKE 'sz.300%' OR code LIKE 'sz.301%' OR code LIKE 'sh.688%')
    """)
    codes = [r[0] for r in cur.fetchall()]
    good = []
    for code in codes:
        cur.execute("""
            SELECT close, volume, turn FROM stock_kline
            WHERE code = ? AND turn > 0 AND volume > 0 AND close > 0
            ORDER BY date DESC LIMIT 1
        """, (code,))
        r = cur.fetchone()
        if not r:
            continue
        close, vol, turn = r
        mktcap_yi = close * vol / (turn / 100.0) / 1e8
        if MIN_MKTCAP_YI <= mktcap_yi <= MAX_MKTCAP_YI:
            good.append(code)
    return good


def load_stock_series(cur, code):
    cur.execute("""
        SELECT date, code_name, open, high, low, close, preclose, volume, turn, isST,
               hour1_open, hour1_close, hour2_open, hour2_close,
               hour3_open, hour3_close, hour4_open, hour4_close
        FROM stock_kline WHERE code = ? ORDER BY date
    """, (code,))
    rows = []
    for r in cur.fetchall():
        rows.append({
            'date': r[0], 'code_name': r[1], 'open': r[2], 'high': r[3], 'low': r[4],
            'close': r[5], 'preclose': r[6], 'volume': r[7], 'turn': r[8], 'isST': r[9],
            'h1o': r[10], 'h1c': r[11], 'h2o': r[12], 'h2c': r[13],
            'h3o': r[14], 'h3c': r[15], 'h4o': r[16], 'h4c': r[17],
        })
    return rows


def detect_signals(rows, idx, metrics, profit_hist, turn5_mean):
    sigs = []
    today = rows[idx]
    yday = rows[idx - 1]
    cur_price = today['close']

    if (metrics['profit_ratio'] >= S1_PROFIT_MIN and
            turn5_mean is not None and turn5_mean < S1_TURN5_MAX):
        sigs.append('S1')

    peak = metrics['peak_price']
    if yday['close'] is not None and yday['close'] < peak < cur_price:
        sigs.append('S2')

    if len(profit_hist) >= 6:
        old_profit = profit_hist[-6]
        if old_profit < S3_OLD_PROFIT_MAX and metrics['profit_ratio'] > S3_NEW_PROFIT_MIN:
            sigs.append('S3')

    if metrics['avg_cost'] > 0:
        cost_gap = (cur_price - metrics['avg_cost']) / metrics['avg_cost'] * 100
        if cost_gap > S4_COST_GAP_MIN and metrics['concentration'] < S4_CONCENTR_MAX:
            sigs.append('S4')

    if (metrics['near_breakeven'] < S5_NEAR_MAX and
            S5_PROFIT_LO <= metrics['profit_ratio'] <= S5_PROFIT_HI):
        sigs.append('S5')

    return sigs


def calc_forward_returns(rows, idx):
    buy_idx = idx + 1
    if buy_idx >= len(rows):
        return None
    buy = rows[buy_idx]
    buy_price = buy['open']
    if buy_price is None or buy_price <= 0:
        return None
    res = {'buy_price': buy_price, 'buy_date': buy['date']}
    for tag, off in (('t1', 0), ('t2', 1), ('t5', 4)):
        j = buy_idx + off
        if j < len(rows) and rows[j]['close'] is not None:
            res[tag] = (rows[j]['close'] - buy_price) / buy_price * 100
        else:
            res[tag] = None
    return res


def fmt(v):
    return f"{v:+.2f}%" if v is not None else 'N/A'


def print_candidate_detail(logger, code, rows, idx, metrics, sig, turn5_mean, rets):
    today = rows[idx]
    name = today['code_name'] or ''
    cur_price = today['close']
    m = metrics
    cost_gap = (cur_price - m['avg_cost']) / m['avg_cost'] * 100 if m['avg_cost'] > 0 else 0
    concentr_desc = ('极度集中' if m['concentration'] < 0.08 else
                     '较集中' if m['concentration'] < 0.15 else '分散')
    logger.log(f"\n--- {code} ({name}) --- {today['date']}  [{SIGNAL_NAMES[sig]}]")
    logger.log(f"筹码分布({LOOKBACK}日):")
    logger.log(f"  获利盘: {m['profit_ratio']*100:.1f}% | 套牢盘: {m['trapped_ratio']*100:.1f}%")
    logger.log(f"  平均成本: Y{m['avg_cost']:.2f} (当前Y{cur_price:.2f}, {cost_gap:+.1f}%)")
    logger.log(f"  集中度: {m['concentration']:.3f} ({concentr_desc})")
    logger.log(f"  近成本(pm3%): {m['near_breakeven']*100:.1f}%")
    logger.log(f"  最大筹码峰: Y{m['peak_price']:.2f} (距当前{-m['distance_to_peak']:+.1f}%)")
    if turn5_mean is not None:
        logger.log(f"  近5日均换手: {turn5_mean:.2f}%")
    if rets:
        logger.log(f"  收益: T+1 {fmt(rets['t1'])} | T+2 {fmt(rets['t2'])} | T+5 {fmt(rets['t5'])}  (买入Y{rets['buy_price']:.2f}@{rets['buy_date']})")

    logger.log(f"  hour级走势(signal_day前5后5):")
    logger.log(f"  {'日期':<12}{'h1o':>8}{'h1c':>8}{'h2o':>8}{'h2c':>8}{'h3o':>8}{'h3c':>8}{'h4o':>8}{'h4c':>8}  备注")
    for j in range(max(0, idx - 5), min(len(rows), idx + 6)):
        r = rows[j]
        mark = ''
        if j == idx:
            mark = '<= 信号日'
        elif j == idx + 1:
            mark = '<= 买入日(open)'
        vals = [r['h1o'], r['h1c'], r['h2o'], r['h2c'], r['h3o'], r['h3c'], r['h4o'], r['h4c']]
        vs = ''.join(f"{(v if v is not None else 0):>8.2f}" for v in vals)
        logger.log(f"  {r['date']:<12}{vs}  {mark}")


def summarize(logger, stats, title):
    logger.log(f"\n{'='*80}")
    logger.log(title)
    logger.log('=' * 80)
    logger.log(f"{'信号':<28}{'样本':<8}{'T+1均%':<10}{'T+1胜率':<10}{'T+2均%':<10}{'T+5均%':<10}{'T+5胜率':<10}")
    logger.log('-' * 90)
    for sig in ['S1', 'S2', 'S3', 'S4', 'S5']:
        t1 = [r['t1'] for r in stats[sig] if r['t1'] is not None]
        t2 = [r['t2'] for r in stats[sig] if r['t2'] is not None]
        t5 = [r['t5'] for r in stats[sig] if r['t5'] is not None]
        if t1:
            a1 = sum(t1) / len(t1)
            w1 = sum(1 for x in t1 if x > 0) / len(t1) * 100
            a2 = sum(t2) / len(t2) if t2 else 0
            a5 = sum(t5) / len(t5) if t5 else 0
            w5 = sum(1 for x in t5 if x > 0) / len(t5) * 100 if t5 else 0
            logger.log(f"{SIGNAL_NAMES[sig]:<28}{len(t1):<8}{a1:+.2f}%    {w1:.1f}%     {a2:+.2f}%    {a5:+.2f}%    {w5:.1f}%")
        else:
            logger.log(f"{SIGNAL_NAMES[sig]:<28}{0:<8}{'N/A':<10}{'N/A':<10}{'N/A':<10}{'N/A':<10}{'N/A'}")


def run_scan(logger, target_ym=None, all_mode=False, print_detail=True):
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()

    logger.log("加载候选股票池(创业板+科创板, 流通市值5~200亿)...")
    codes = get_qualifying_codes(cur)
    logger.log(f"合格股票数: {len(codes)}")

    stats = {s: [] for s in ['S1', 'S2', 'S3', 'S4', 'S5']}
    detail_count = {s: 0 for s in stats}

    def in_range(date):
        y = int(date[:4])
        if all_mode:
            if y < START_YEAR or y > END_YEAR:
                return False
            if y == END_YEAR and int(date[5:7]) > 6:
                return False
            return True
        return date[:7] == target_ym

    processed = 0
    for code in codes:
        rows = load_stock_series(cur, code)
        for r in rows:
            r['code'] = code
        n = len(rows)
        if n < LOOKBACK + 2:
            continue

        eval_start = None
        for i in range(LOOKBACK, n - 1):
            if in_range(rows[i]['date']):
                eval_start = i
                break
        if eval_start is None:
            continue
        begin = max(LOOKBACK, eval_start - 6)

        profit_hist = deque(maxlen=8)
        for idx in range(begin, n - 1):
            win = rows[idx - LOOKBACK + 1: idx + 1]
            lows = np.array([w['low'] for w in win], dtype=float)
            highs = np.array([w['high'] for w in win], dtype=float)
            turns = np.array([(w['turn'] if w['turn'] else 0.0) for w in win], dtype=float)
            if np.any(np.isnan(lows)) or np.any(np.isnan(highs)):
                profit_hist.append(0.5)
                continue

            today = rows[idx]
            if today['close'] is None or today['close'] <= 0:
                profit_hist.append(0.5)
                continue

            bin_centers, dist = build_chip_distribution(lows, highs, turns)
            metrics = calc_chip_metrics(bin_centers, dist, today['close'])
            profit_hist.append(metrics['profit_ratio'])

            if not in_range(today['date']):
                continue
            if today['isST']:
                continue

            t5vals = [w['turn'] for w in rows[idx - 4: idx + 1] if w['turn']]
            turn5_mean = sum(t5vals) / len(t5vals) if t5vals else None

            sigs = detect_signals(rows, idx, metrics, profit_hist, turn5_mean)
            if not sigs:
                continue

            buy = rows[idx + 1]
            if is_limit_up(buy['open'], buy['preclose'], code) and buy['high'] == buy['low']:
                continue
            rets = calc_forward_returns(rows, idx)
            if rets is None or rets['t1'] is None:
                continue

            for sig in sigs:
                stats[sig].append(rets)
                if print_detail and detail_count[sig] < DETAIL_LIMIT_PER_SIGNAL:
                    print_candidate_detail(logger, code, rows, idx, metrics, sig, turn5_mean, rets)
                    detail_count[sig] += 1

        processed += 1
        if processed % 200 == 0:
            logger.log(f"  ...已处理 {processed}/{len(codes)} 只股票")

    conn.close()
    scope = 'ALL(2021-2026H1)' if all_mode else target_ym
    summarize(logger, stats, f"信号统计汇总 [{scope}]")
    return stats


def run_verify(logger):
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    codes = get_qualifying_codes(cur)
    sample = codes[:5] if len(codes) >= 5 else codes
    logger.log("=" * 80)
    logger.log("筹码分布合理性验证 (抽样5只)")
    logger.log("=" * 80)
    for code in sample:
        rows = load_stock_series(cur, code)
        if len(rows) < LOOKBACK + 2:
            continue
        idx = len(rows) - 2
        win = rows[idx - LOOKBACK + 1: idx + 1]
        lows = np.array([w['low'] for w in win], dtype=float)
        highs = np.array([w['high'] for w in win], dtype=float)
        turns = np.array([(w['turn'] if w['turn'] else 0.0) for w in win], dtype=float)
        today = rows[idx]
        if today['close'] is None:
            continue
        bc, dist = build_chip_distribution(lows, highs, turns)
        m = calc_chip_metrics(bc, dist, today['close'])
        logger.log(f"\n--- {code} ({today['code_name']}) @ {today['date']} 当前Y{today['close']:.2f} ---")
        logger.log(f"  分布区间: Y{bc[0]:.2f} ~ Y{bc[-1]:.2f}  (sum={dist.sum():.4f})")
        logger.log(f"  获利盘: {m['profit_ratio']*100:.1f}% | 套牢盘: {m['trapped_ratio']*100:.1f}%")
        logger.log(f"  平均成本: Y{m['avg_cost']:.2f} | 集中度: {m['concentration']:.3f}")
        logger.log(f"  最大筹码峰: Y{m['peak_price']:.2f} | 近成本pm3%: {m['near_breakeven']*100:.1f}%")
        comp = dist.reshape(20, -1).sum(axis=1)
        bc20 = bc.reshape(20, -1).mean(axis=1)
        peak = comp.max()
        logger.log("  分布(每格价格:占比):")
        for k in range(20):
            barlen = int(comp[k] / peak * 40) if peak > 0 else 0
            logger.log(f"    Y{bc20[k]:>8.2f} {comp[k]*100:>5.1f}% {'#'*barlen}")
    conn.close()


def main():
    if len(sys.argv) < 2:
        print("用法: python3 research_chip_distribution_r2.py [2024-09 | all | verify]")
        sys.exit(1)
    arg = sys.argv[1]
    logger = Logger(LOG_PATH)
    logger.log("#" * 80)
    logger.log("# 筹码分布模型 + 持仓心理 + 拉升信号验证 (Task #80, rule2流程)")
    logger.log("#" * 80)

    if arg == 'verify':
        run_verify(logger)
    elif arg == 'all':
        run_scan(logger, all_mode=True, print_detail=False)
    else:
        run_scan(logger, target_ym=arg, print_detail=True)

    logger.log("\n完成。")
    logger.close()


if __name__ == '__main__':
    main()
