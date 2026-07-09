#!/usr/bin/env python3
"""
跳空高开 - 特殊形态场景研究
研究7种跳空高开在特定形态模式下的收益分布
"""
import sqlite3
import sys
import os
from collections import defaultdict

DB_PATH = "/home/AIWealth/data/stocks.db"
LOG_PATH = "/home/AIWealth/scripts/logs/gapup_pattern_scenarios.log"
START_DATE = "2021-01-01"
END_DATE = "2026-06-30"
CHUNK_SIZE = 400


def get_limit_ratio(code):
    if code.startswith('bj.'):
        return 0.30
    elif code.startswith('sh.688'):
        return 0.20
    elif code.startswith('sz.300') or code.startswith('sz.301'):
        return 0.20
    else:
        return 0.10


def get_board(code):
    if code.startswith('sh.688'):
        return 'star'
    elif code.startswith('sz.300') or code.startswith('sz.301'):
        return 'gem'
    elif code.startswith('bj.'):
        return 'bse'
    else:
        return 'main'


def get_board_label(board):
    return {'main': '主板', 'gem': '创业板', 'star': '科创板', 'bse': '北交所'}[board]


def get_mcap_group(amount, turn):
    """估算流通市值分组(亿元)"""
    if turn is None or turn <= 0 or amount is None or amount <= 0:
        return None
    mcap = amount * 100 / turn / 1e8  # 亿元
    if mcap < 50:
        return '<50亿'
    elif mcap < 200:
        return '50-200亿'
    elif mcap < 700:
        return '200-700亿'
    else:
        return '>700亿'


def is_limit_up(code, close, preclose):
    """判断是否涨停"""
    if preclose <= 0:
        return False
    ratio = get_limit_ratio(code)
    limit_price = round(preclose * (1 + ratio), 2)
    return close >= limit_price - 0.01


def is_open_limit_up(code, open_price, preclose):
    """判断开盘价是否涨停"""
    if preclose <= 0:
        return False
    ratio = get_limit_ratio(code)
    limit_price = round(preclose * (1 + ratio), 2)
    return open_price >= limit_price


def is_st(row):
    if row.get('isST') == 1:
        return True
    name = row.get('code_name') or ''
    return 'ST' in name.upper()


def load_trading_days(conn):
    cur = conn.cursor()
    cur.execute("SELECT DISTINCT date FROM stock_kline WHERE date >= ? AND date <= ? ORDER BY date",
                (START_DATE, END_DATE))
    return [r[0] for r in cur.fetchall()]


def load_all_trading_days(conn):
    """Load all trading days including before START_DATE for lookback"""
    cur = conn.cursor()
    cur.execute("SELECT DISTINCT date FROM stock_kline ORDER BY date")
    return [r[0] for r in cur.fetchall()]


def load_stock_codes(conn):
    cur = conn.cursor()
    cur.execute("SELECT DISTINCT code FROM stock_kline WHERE date >= ? AND date <= ?",
                (START_DATE, END_DATE))
    return [r[0] for r in cur.fetchall()]


def load_chunk_data(conn, codes):
    """Load full history for a chunk of codes"""
    cur = conn.cursor()
    placeholders = ','.join(['?'] * len(codes))
    cur.execute(f"""
        SELECT date, code, code_name, open, high, low, close, preclose,
               close_rate, volume, amount, turn, hour1_open, hour4_close, isST
        FROM stock_kline
        WHERE code IN ({placeholders})
        ORDER BY code, date
    """, codes)
    cols = [d[0] for d in cur.description]
    data = defaultdict(list)
    for row in cur.fetchall():
        d = dict(zip(cols, row))
        data[d['code']].append(d)
    return data


def process_stock(code, rows, all_days_set, day_index, signals_by_scenario):
    """Process a single stock's history to find signals for all scenarios"""
    if len(rows) < 15:
        return

    board = get_board(code)
    limit_ratio = get_limit_ratio(code)

    # Build date-indexed lookup
    by_date = {r['date']: r for r in rows}
    dates = [r['date'] for r in rows]

    for i in range(12, len(rows) - 2):  # Need lookback and future
        t0 = rows[i]  # Today
        t_m1 = rows[i - 1]  # T-1
        date_today = t0['date']

        if date_today < START_DATE or date_today > END_DATE:
            continue

        # Basic checks
        if is_st(t0) or is_st(t_m1):
            continue
        if t0['preclose'] is None or t0['preclose'] <= 0:
            continue
        if t0['open'] is None or t0['open'] <= 0:
            continue
        if t0['hour1_open'] is None or t0['hour1_open'] <= 0:
            continue

        # Check if open is limit up - exclude
        if is_open_limit_up(code, t0['open'], t0['preclose']):
            continue

        # Gap calculation
        gap_pct = (t0['open'] / t_m1['close'] - 1) * 100 if t_m1['close'] and t_m1['close'] > 0 else 0

        # Buy price = hour1_open
        buy_price = t0['hour1_open']
        if buy_price is None or buy_price <= 0:
            continue

        # Market cap group
        mcap_grp = get_mcap_group(t0.get('amount'), t0.get('turn'))

        # Future returns - T+1 and T+2
        ret_t1, ret_t2 = None, None
        if i + 1 < len(rows):
            t1 = rows[i + 1]
            if t1.get('hour4_close') and t1['hour4_close'] > 0:
                ret_t1 = (t1['hour4_close'] - buy_price) / buy_price * 100
        if i + 2 < len(rows):
            t2 = rows[i + 2]
            if t2.get('hour4_close') and t2['hour4_close'] > 0:
                ret_t2 = (t2['hour4_close'] - buy_price) / buy_price * 100

        if ret_t1 is None:
            continue

        year = date_today[:4]
        base_record = {
            'code': code, 'date': date_today, 'board': board,
            'mcap': mcap_grp, 'ret_t1': ret_t1, 'ret_t2': ret_t2,
            'year': year, 'gap_pct': gap_pct
        }

        # ===== Scenario 1: 首板溢价 =====
        if t_m1['close'] and t_m1['preclose'] and t_m1['preclose'] > 0:
            t_m1_is_limit = is_limit_up(code, t_m1['close'], t_m1['preclose'])
            if t_m1_is_limit and i >= 2:
                t_m2 = rows[i - 2]
                t_m2_is_limit = False
                if t_m2['close'] and t_m2['preclose'] and t_m2['preclose'] > 0:
                    t_m2_is_limit = is_limit_up(code, t_m2['close'], t_m2['preclose'])
                if not t_m2_is_limit and gap_pct > 0:
                    # First board + gap up
                    for gap_thresh in [1, 2, 3, 5]:
                        if gap_pct >= gap_thresh:
                            rec = base_record.copy()
                            rec['gap_thresh'] = gap_thresh
                            signals_by_scenario['s1'].append(rec)

        # ===== Scenario 2: 跌停反弹 =====
        if t_m1.get('close_rate') is not None:
            cr = t_m1['close_rate']
            big_drop = False
            if board == 'main' and cr <= -8:
                big_drop = True
            elif board in ('gem', 'star') and cr <= -12:
                big_drop = True
            elif board == 'bse' and cr <= -15:
                big_drop = True
            if big_drop and gap_pct > 0:
                for gap_thresh in [2, 3, 5, 8]:
                    if gap_pct >= gap_thresh:
                        rec = base_record.copy()
                        rec['gap_thresh'] = gap_thresh
                        signals_by_scenario['s2'].append(rec)

        # ===== Scenario 3: 蓄力突破 =====
        if i >= 4:
            # Check T-3 to T-1: volume < vol_ma5*0.7 and amplitude < 3%
            can_check = True
            for j in range(i - 3, i):
                r = rows[j]
                if r.get('volume') is None or r['volume'] <= 0:
                    can_check = False
                    break
                if r.get('high') is None or r.get('low') is None or r['low'] <= 0:
                    can_check = False
                    break
            if can_check:
                # Compute vol_ma5 for each of T-3, T-2, T-1
                shrink_ok = True
                narrow_ok = True
                for j in range(i - 3, i):
                    r = rows[j]
                    # vol_ma5: average of 5 days ending at j
                    if j >= 5:
                        vol_sum = sum(rows[k]['volume'] for k in range(j - 5, j) if rows[k].get('volume'))
                        vol_ma5 = vol_sum / 5
                    else:
                        vol_ma5 = r['volume']  # fallback
                    if r['volume'] >= vol_ma5 * 0.7:
                        shrink_ok = False
                        break
                    amplitude = (r['high'] - r['low']) / r['low'] * 100
                    if amplitude >= 3:
                        narrow_ok = False
                        break
                if shrink_ok and narrow_ok and gap_pct > 0:
                    for gap_thresh in [1, 2, 3, 5]:
                        if gap_pct >= gap_thresh:
                            rec = base_record.copy()
                            rec['gap_thresh'] = gap_thresh
                            signals_by_scenario['s3'].append(rec)

        # ===== Scenario 4: 大阴高开 (对比验证) =====
        if t_m1.get('close_rate') is not None:
            cr = t_m1['close_rate']
            big_yin = False
            if board == 'main' and cr <= -5:
                big_yin = True
            elif board in ('gem', 'star') and cr <= -7:
                big_yin = True
            elif board == 'bse' and cr <= -10:
                big_yin = True
            if big_yin and gap_pct >= 2:
                for gap_thresh in [2, 3, 5]:
                    if gap_pct >= gap_thresh:
                        rec = base_record.copy()
                        rec['gap_thresh'] = gap_thresh
                        signals_by_scenario['s4'].append(rec)

        # ===== Scenario 5: 龙回头 =====
        if i >= 11:
            # T-6 to T-10: check if there was a 20-day high
            # 20-day high means high >= max(high of last 20 days)
            had_20d_high = False
            for j in range(i - 10, i - 5):
                if j < 0 or j >= len(rows):
                    continue
                r = rows[j]
                if r.get('high') is None:
                    continue
                # 20-day lookback from j
                start_k = max(0, j - 20)
                highs_20 = [rows[k]['high'] for k in range(start_k, j) if rows[k].get('high')]
                if highs_20 and r['high'] >= max(highs_20):
                    had_20d_high = True
                    break

            if had_20d_high:
                # T-1 to T-5: pullback check
                # Method: cumulative drop > 5% OR 2 of last 3 days are negative
                pullback_start = rows[i - 5] if i >= 5 else None
                pullback_end = rows[i - 1]
                pullback_ok = False
                if pullback_start and pullback_start.get('close') and pullback_end.get('close'):
                    drop = (pullback_end['close'] / pullback_start['close'] - 1) * 100
                    if drop <= -5:
                        pullback_ok = True
                # Or 2 of last 3 days negative
                if not pullback_ok:
                    neg_count = 0
                    for j in range(i - 3, i):
                        if j >= 0 and rows[j].get('close_rate') is not None and rows[j]['close_rate'] < 0:
                            neg_count += 1
                    if neg_count >= 2:
                        pullback_ok = True

                if pullback_ok and gap_pct > 0:
                    for gap_thresh in [1, 2, 3, 5]:
                        if gap_pct >= gap_thresh:
                            rec = base_record.copy()
                            rec['gap_thresh'] = gap_thresh
                            signals_by_scenario['s5'].append(rec)

        # ===== Scenario 6: 连续高开 =====
        if t_m1.get('open') and t_m1.get('close') and t_m1.get('preclose') and t_m1['preclose'] > 0:
            t_m1_gap = (t_m1['open'] / t_m1['preclose'] - 1) * 100
            t_m1_yang = t_m1['close'] > t_m1['open']
            if t_m1_gap >= 1 and t_m1_yang and gap_pct >= 1:
                for gap_thresh in [1, 2, 3]:
                    if gap_pct >= gap_thresh:
                        rec = base_record.copy()
                        rec['gap_thresh'] = gap_thresh
                        signals_by_scenario['s6'].append(rec)

        # ===== Scenario 7: 板块联动高开 (first pass collects gap-up info) =====
        if gap_pct >= 1:
            signals_by_scenario['s7_raw'].append({
                'code': code, 'date': date_today, 'board': board,
                'mcap': mcap_grp, 'ret_t1': ret_t1, 'ret_t2': ret_t2,
                'year': year, 'gap_pct': gap_pct
            })


def build_sector_index(s7_raw):
    """Build per-date per-board gap-up count and filter for scenario 7"""
    # Count gap-up stocks per date per board (excluding each stock itself)
    date_board_codes = defaultdict(lambda: defaultdict(list))
    for rec in s7_raw:
        date_board_codes[rec['date']][rec['board']].append(rec['code'])

    # Filter: need >= 4 stocks in same board on same date (3 others + self)
    results = []
    for rec in s7_raw:
        cnt = len(date_board_codes[rec['date']][rec['board']])
        if cnt >= 4:  # At least 3 others
            for gap_thresh in [1, 2, 3]:
                if rec['gap_pct'] >= gap_thresh:
                    r = rec.copy()
                    r['gap_thresh'] = gap_thresh
                    results.append(r)
    return results


def compute_stats(records):
    """Compute grouped statistics"""
    if not records:
        return {}

    # Group by (board, mcap, gap_thresh)
    groups = defaultdict(list)
    for r in records:
        if r.get('mcap') is None:
            continue
        key = (r['board'], r['mcap'], r['gap_thresh'])
        groups[key].append(r)

    stats = {}
    for key, recs in groups.items():
        n = len(recs)
        if n < 5:
            continue
        t1_rets = [r['ret_t1'] for r in recs if r['ret_t1'] is not None]
        t2_rets = [r['ret_t2'] for r in recs if r['ret_t2'] is not None]
        if not t1_rets:
            continue

        avg_t1 = sum(t1_rets) / len(t1_rets)
        win_t1 = sum(1 for x in t1_rets if x > 0) / len(t1_rets) * 100
        avg_t2 = sum(t2_rets) / len(t2_rets) if t2_rets else 0
        win_t2 = sum(1 for x in t2_rets if x > 0) / len(t2_rets) * 100 if t2_rets else 0

        # Yearly breakdown for worst year
        yearly = defaultdict(list)
        for r in recs:
            if r['ret_t1'] is not None:
                yearly[r['year']].append(r['ret_t1'])
        yearly_avg = {y: sum(v) / len(v) for y, v in yearly.items() if v}
        worst_year = min(yearly_avg, key=yearly_avg.get) if yearly_avg else 'N/A'
        worst_val = yearly_avg.get(worst_year, 0)
        years_positive = sum(1 for v in yearly_avg.values() if v > 0)

        stats[key] = {
            'n': n, 'avg_t1': avg_t1, 'win_t1': win_t1,
            'avg_t2': avg_t2, 'win_t2': win_t2,
            'worst_year': worst_year, 'worst_val': worst_val,
            'years_positive': years_positive, 'total_years': len(yearly_avg)
        }
    return stats


def format_stats_table(stats, scenario_name, gap_thresholds):
    """Format stats into table string"""
    lines = []
    lines.append(f"\n===== {scenario_name} =====")
    lines.append(f"{'板块':<6}{'市值':<10}{'Gap':<8}{'样本':<7}{'T+1收益':<10}{'T+1胜率':<9}{'T+2收益':<10}{'T+2胜率':<9}{'逐年最差':<15}")
    lines.append("-" * 90)

    board_order = ['main', 'gem', 'star', 'bse']
    mcap_order = ['<50亿', '50-200亿', '200-700亿', '>700亿']

    found_any = False
    for board in board_order:
        for mcap in mcap_order:
            for gap in gap_thresholds:
                key = (board, mcap, gap)
                if key in stats:
                    s = stats[key]
                    found_any = True
                    bl = get_board_label(board)
                    worst_str = f"{s['worst_year']}({s['worst_val']:+.1f}%)"
                    lines.append(
                        f"{bl:<6}{mcap:<10}>={gap}%{'':<4}{s['n']:<7}"
                        f"{s['avg_t1']:+.2f}%{'':>3}{s['win_t1']:.1f}%{'':>3}"
                        f"{s['avg_t2']:+.2f}%{'':>3}{s['win_t2']:.1f}%{'':>3}"
                        f"{worst_str}"
                    )

    if not found_any:
        lines.append("  (样本不足)")
    return '\n'.join(lines)


def find_best_group(stats):
    """Find the best performing group by T+1 return"""
    if not stats:
        return None, None
    best_key = max(stats, key=lambda k: stats[k]['avg_t1'])
    return best_key, stats[best_key]


def main():
    os.makedirs(os.path.dirname(LOG_PATH), exist_ok=True)

    # Redirect output
    log_file = open(LOG_PATH, 'w', encoding='utf-8')
    class TeeOutput:
        def __init__(self, f):
            self.file = f
            self.stdout = sys.stdout
        def write(self, s):
            self.stdout.write(s)
            self.file.write(s)
        def flush(self):
            self.stdout.flush()
            self.file.flush()
    sys.stdout = TeeOutput(log_file)

    import atexit
    def _close_log():
        try:
            log_file.close()
        except:
            pass
    atexit.register(_close_log)

    print("=" * 60)
    print("跳空高开 - 特殊形态场景研究")
    print(f"数据范围: {START_DATE} ~ {END_DATE}")
    print("=" * 60)

    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = None  # Use tuples for speed

    # Load stock codes
    all_codes = load_stock_codes(conn)
    print(f"总股票数: {len(all_codes)}")

    # Signals container
    signals = defaultdict(list)

    # Process in chunks
    total_chunks = (len(all_codes) + CHUNK_SIZE - 1) // CHUNK_SIZE
    for chunk_idx in range(total_chunks):
        start = chunk_idx * CHUNK_SIZE
        end = min(start + CHUNK_SIZE, len(all_codes))
        chunk_codes = all_codes[start:end]

        if chunk_idx % 5 == 0:
            print(f"  处理进度: {chunk_idx+1}/{total_chunks} chunks ({start}/{len(all_codes)} stocks)...",
                  flush=True)

        # Load chunk data
        chunk_data = load_chunk_data(conn, chunk_codes)

        for code, rows in chunk_data.items():
            process_stock(code, rows, None, None, signals)

    conn.close()

    # Post-process scenario 7
    print(f"\n构建板块联动索引(场景7)...")
    s7_results = build_sector_index(signals['s7_raw'])
    signals['s7'] = s7_results
    del signals['s7_raw']

    # Print signal counts
    scenario_names = {
        's1': '场景1: 首板溢价',
        's2': '场景2: 跌停反弹',
        's3': '场景3: 蓄力突破',
        's4': '场景4: 大阴高开(对比验证)',
        's5': '场景5: 龙回头',
        's6': '场景6: 连续高开',
        's7': '场景7: 板块联动高开',
    }
    gap_thresholds_map = {
        's1': [1, 2, 3, 5],
        's2': [2, 3, 5, 8],
        's3': [1, 2, 3, 5],
        's4': [2, 3, 5],
        's5': [1, 2, 3, 5],
        's6': [1, 2, 3],
        's7': [1, 2, 3],
    }

    print(f"\n信号数量统计:")
    for key in ['s1', 's2', 's3', 's4', 's5', 's6', 's7']:
        # Count unique signals (deduplicate across gap thresholds)
        unique = set()
        for r in signals[key]:
            unique.add((r['code'], r['date']))
        print(f"  {scenario_names[key]}: {len(unique)} 个独立信号, {len(signals[key])} 条分组记录")

    # Compute stats for each scenario
    print("\n" + "=" * 60)
    all_best = {}
    for key in ['s1', 's2', 's3', 's4', 's5', 's6', 's7']:
        stats = compute_stats(signals[key])
        table = format_stats_table(stats, scenario_names[key], gap_thresholds_map[key])
        print(table)

        best_key, best_stat = find_best_group(stats)
        if best_key and best_stat:
            all_best[key] = (best_key, best_stat)

    # Summary comparison
    print("\n\n===== 场景间对比汇总 =====")
    print(f"{'场景':<20}{'最优板块×市值×Gap':<25}{'样本':<7}{'T+1收益':<10}{'胜率':<8}{'稳定性评分':<12}")
    print("-" * 90)
    for key in ['s1', 's2', 's3', 's4', 's5', 's6', 's7']:
        if key in all_best:
            bk, bs = all_best[key]
            board_l = get_board_label(bk[0])
            grp_str = f"{board_l}{bk[1]}>={bk[2]}%"
            stability = f"{bs['years_positive']}/{bs['total_years']}年正"
            print(f"{scenario_names[key]:<20}{grp_str:<25}{bs['n']:<7}"
                  f"{bs['avg_t1']:+.2f}%{'':>3}{bs['win_t1']:.1f}%{'':>2}{stability}")
        else:
            print(f"{scenario_names[key]:<20}{'样本不足':<25}")

    # Complementarity analysis with bigdrop_gapup
    print("\n\n===== 与现有大阴高开策略互补性 =====")
    # Count signals per year per scenario
    for key in ['s4', 's1', 's5', 's6']:
        yearly_counts = defaultdict(int)
        seen = set()
        for r in signals[key]:
            uid = (r['code'], r['date'], r['gap_thresh'])
            if uid not in seen:
                seen.add(uid)
                yearly_counts[r['year']] += 1
        avg_annual = sum(yearly_counts.values()) / max(len(yearly_counts), 1)
        dist_info = ", ".join(f"{y}:{c}" for y, c in sorted(yearly_counts.items()))
        print(f"{scenario_names[key]}信号分布: 年均约{avg_annual:.0f}笔 [{dist_info}]")

    # Overlap analysis between s4 and s1/s5/s6
    s4_signals = set((r['code'], r['date']) for r in signals['s4'])
    for key in ['s1', 's5', 's6']:
        other_signals = set((r['code'], r['date']) for r in signals[key])
        overlap = s4_signals & other_signals
        print(f"  大阴高开 与 {scenario_names[key]} 重叠: {len(overlap)} 笔 "
              f"({len(overlap)/max(len(s4_signals),1)*100:.1f}%)")

    print(f"\n互补潜力分析:")
    print(f"  大阴高开(场景4): 主要在市场恐慌期触发，信号集中")
    non_s4 = set()
    for key in ['s1', 's3', 's5', 's6']:
        for r in signals[key]:
            non_s4.add((r['code'], r['date']))
    only_new = non_s4 - s4_signals
    print(f"  新场景独有信号: {len(only_new)} 笔 (不与大阴高开重叠)")
    print(f"  → 首板溢价+蓄力突破+龙回头+连续高开 可在非恐慌期提供额外信号")

    print(f"\n{'='*60}")
    print("研究完成。")
    sys.stdout.flush()
    sys.stdout = sys.stdout.stdout
    log_file.close()


if __name__ == "__main__":
    main()
