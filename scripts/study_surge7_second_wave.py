#!/usr/bin/env python3
"""
冲高7%后20天内第二波机会与胜率分析
分析创业板(300/301)、科创板(688)股票冲高回落后的第二波上涨规律
"""
import sqlite3
import sys
from collections import defaultdict
from datetime import datetime

# ============ 配置参数 ============
DB_PATH = '/home/AIWealth/data/stocks.db'
SURGE_THRESHOLD = 7.0        # 冲高阈值(%)
FALLBACK_RATIO = 0.7         # 冲高回落系数(close_rate < high_rate * 此值才算回落)
LOOKFORWARD_DAYS = 20        # 观察窗口(交易日)
DETAIL_MONTHS = ['2026-03', '2026-04']  # 明细输出的月份
START_DATE = '2021-01-01'
END_DATE = '2026-05-31'
# ==================================

def get_board(code):
    if code.startswith('bj.'):
        return '北交所'
    elif code.startswith('sz.300') or code.startswith('sz.301') or code.startswith('sz.302'):
        return '创业板'
    elif code.startswith('sh.688') or code.startswith('sh.689'):
        return '科创板'
    return None

def median(lst):
    if not lst:
        return 0
    s = sorted(lst)
    n = len(s)
    if n % 2 == 0:
        return (s[n//2 - 1] + s[n//2]) / 2
    return s[n//2]

def main():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()

    print("=" * 80)
    print("冲高7%后20天内第二波机会与胜率分析")
    print(f"分析时间范围: {START_DATE} ~ {END_DATE}")
    print(f"冲高阈值: {SURGE_THRESHOLD}%  回落系数: {FALLBACK_RATIO}")
    print(f"观察窗口: T+1 ~ T+{LOOKFORWARD_DAYS}")
    print("=" * 80)

    board_conditions = """
        (code LIKE 'sz.300%' OR code LIKE 'sz.301%' OR code LIKE 'sz.302%'
         OR code LIKE 'sh.688%' OR code LIKE 'sh.689%'
         OR code LIKE 'bj.%')
    """

    # Step 1: 找到所有冲高回落事件
    print("\n[1] 筛选冲高回落事件...")
    sql_events = f"""
        SELECT date, code, code_name, preclose, open, open_rate, high, high_rate,
               low, low_rate, close, close_rate, volume, amount, turn,
               hour1_open_rate, hour1_close_rate, hour2_open_rate, hour2_close_rate,
               hour3_open_rate, hour3_close_rate, hour4_open_rate, hour4_close_rate,
               isST
        FROM stock_kline
        WHERE {board_conditions}
          AND date >= ? AND date <= ?
          AND high_rate >= ?
          AND isST = 0
          AND code_name NOT LIKE '%ST%'
        ORDER BY code, date
    """
    cur.execute(sql_events, (START_DATE, END_DATE, SURGE_THRESHOLD))
    raw_events = cur.fetchall()
    print(f"  原始冲高>=7%事件数: {len(raw_events)}")

    events = []
    for row in raw_events:
        hr = row['high_rate']
        cr = row['close_rate']
        if cr is not None and hr is not None and cr < hr * FALLBACK_RATIO:
            events.append(dict(row))
    print(f"  冲高回落事件数(close_rate < high_rate*{FALLBACK_RATIO}): {len(events)}")

    # Step 2: 加载各股票后续行情
    print("\n[2] 加载各股票后续行情数据...")
    codes = list(set(e['code'] for e in events))
    print(f"  涉及股票数: {len(codes)}")

    sql_all = f"""
        SELECT date, code, high, high_rate, close, close_rate, open_rate
        FROM stock_kline
        WHERE {board_conditions}
          AND date >= ? AND date <= '2026-06-30'
        ORDER BY code, date
    """
    cur.execute(sql_all, (START_DATE,))

    code_timeseries = defaultdict(list)
    for r in cur:
        code_timeseries[r['code']].append({
            'date': r['date'],
            'high': r['high'],
            'high_rate': r['high_rate'],
            'close': r['close'],
            'close_rate': r['close_rate'],
            'open_rate': r['open_rate']
        })

    code_date_idx = {}
    for code, series in code_timeseries.items():
        code_date_idx[code] = {s['date']: i for i, s in enumerate(series)}

    print(f"  总行情记录数: {sum(len(v) for v in code_timeseries.values())}")

    # Step 3: 分析第二波
    print("\n[3] 分析第二波触发情况...")

    stats = {
        'total': 0,
        'A_triggered': 0, 'A_days': [],
        'B_triggered': 0, 'B_days': [],
        'C_triggered': 0, 'C_days': [],
        'A_dist': defaultdict(int),
        'B_dist': defaultdict(int),
        'C_dist': defaultdict(int),
    }
    board_stats = defaultdict(lambda: {
        'total': 0, 'A_triggered': 0, 'B_triggered': 0, 'C_triggered': 0
    })

    detail_events = []

    for ev in events:
        code = ev['code']
        date = ev['date']
        board = get_board(code)
        if board is None:
            continue

        if code not in code_date_idx or date not in code_date_idx[code]:
            continue

        idx = code_date_idx[code][date]
        series = code_timeseries[code]

        forward = series[idx+1: idx+1+LOOKFORWARD_DAYS]
        if len(forward) < 3:
            continue

        stats['total'] += 1
        board_stats[board]['total'] += 1

        t_high = ev['high']
        t_close = ev['close']

        a_triggered = False
        b_triggered = False
        c_triggered = False
        a_day = 0
        b_day = 0
        c_day = 0

        forward_details = []
        for i, fd in enumerate(forward, 1):
            forward_details.append(fd)
            if not a_triggered and fd['high_rate'] is not None and fd['high_rate'] >= SURGE_THRESHOLD:
                a_triggered = True
                a_day = i
            if not b_triggered and fd['close'] is not None and t_high is not None and fd['close'] > t_high:
                b_triggered = True
                b_day = i
            if not c_triggered and fd['close'] is not None and t_close is not None and t_close > 0:
                gain = (fd['close'] - t_close) / t_close * 100
                if gain >= 10:
                    c_triggered = True
                    c_day = i

        if a_triggered:
            stats['A_triggered'] += 1
            stats['A_days'].append(a_day)
            stats['A_dist'][a_day] += 1
            board_stats[board]['A_triggered'] += 1
        if b_triggered:
            stats['B_triggered'] += 1
            stats['B_days'].append(b_day)
            stats['B_dist'][b_day] += 1
            board_stats[board]['B_triggered'] += 1
        if c_triggered:
            stats['C_triggered'] += 1
            stats['C_days'].append(c_day)
            stats['C_dist'][c_day] += 1
            board_stats[board]['C_triggered'] += 1

        if any(date.startswith(m) for m in DETAIL_MONTHS):
            detail_events.append({
                'event': ev,
                'board': board,
                'forward': forward_details,
                'a_triggered': a_triggered, 'a_day': a_day,
                'b_triggered': b_triggered, 'b_day': b_day,
                'c_triggered': c_triggered, 'c_day': c_day,
            })

    # Step 4: 统计结果
    print("\n" + "=" * 80)
    print("统计结果汇总")
    print("=" * 80)

    total = stats['total']
    print(f"\n总事件数: {total}")

    print(f"\n{'方式':<16} {'触发次数':<10} {'胜率':<10} {'平均T+天数':<12} {'中位数天数':<12}")
    print("-" * 62)

    for label, key in [('A(再次冲高7%)', 'A'), ('B(突破首高)', 'B'), ('C(涨>=10%)', 'C')]:
        triggered = stats[f'{key}_triggered']
        days_list = stats[f'{key}_days']
        rate = triggered / total * 100 if total > 0 else 0
        avg_d = sum(days_list) / len(days_list) if days_list else 0
        med_d = median(days_list)
        print(f"{label:<16} {triggered:<10} {rate:<9.1f}% {avg_d:<12.1f} {med_d:<12.1f}")

    # 分板块
    print(f"\n\n分板块统计:")
    print(f"{'板块':<8} {'事件数':<8} {'A胜率':<10} {'B胜率':<10} {'C胜率':<10}")
    print("-" * 50)
    for board in ['创业板', '科创板', '北交所']:
        bs = board_stats[board]
        if bs['total'] == 0:
            continue
        t = bs['total']
        ar = bs['A_triggered'] / t * 100
        br = bs['B_triggered'] / t * 100
        cr = bs['C_triggered'] / t * 100
        print(f"{board:<8} {t:<8} {ar:<9.1f}% {br:<9.1f}% {cr:<9.1f}%")

    # T+n 分布
    print(f"\n\n第二波发生日分布 (T+1 ~ T+{LOOKFORWARD_DAYS}):")
    print(f"{'T+n':<6} {'方式A':<8} {'方式B':<8} {'方式C':<8}")
    print("-" * 35)
    for n in range(1, LOOKFORWARD_DAYS + 1):
        a = stats['A_dist'].get(n, 0)
        b = stats['B_dist'].get(n, 0)
        c = stats['C_dist'].get(n, 0)
        if a > 0 or b > 0 or c > 0:
            print(f"T+{n:<4} {a:<8} {b:<8} {c:<8}")

    # Step 5: 明细输出
    print(f"\n\n{'=' * 80}")
    print(f"明细输出 (月份: {', '.join(DETAIL_MONTHS)})")
    print(f"{'=' * 80}")
    print(f"共 {len(detail_events)} 个事件")

    max_detail = 50
    if len(detail_events) > max_detail:
        print(f"(仅展示前{max_detail}个)")

    for i, de in enumerate(detail_events[:max_detail]):
        ev = de['event']
        print(f"\n{'─' * 70}")
        print(f"事件：{ev['date']} {ev['code']} {ev['code_name']} | "
              f"冲高:{ev['high_rate']:.1f}% 收盘:{ev['close_rate']:.1f}% "
              f"板块:{de['board']}")

        h1o = ev.get('hour1_open_rate') or 0
        h1c = ev.get('hour1_close_rate') or 0
        h2o = ev.get('hour2_open_rate') or 0
        h2c = ev.get('hour2_close_rate') or 0
        h3o = ev.get('hour3_open_rate') or 0
        h3c = ev.get('hour3_close_rate') or 0
        h4o = ev.get('hour4_open_rate') or 0
        h4c = ev.get('hour4_close_rate') or 0
        print(f"  T日hour明细: h1={h1o:.1f}->{h1c:.1f} h2={h2o:.1f}->{h2c:.1f} "
              f"h3={h3o:.1f}->{h3c:.1f} h4={h4o:.1f}->{h4c:.1f}")

        print(f"  后续{len(de['forward'])}日行情:")
        for j, fd in enumerate(de['forward'], 1):
            or_val = fd['open_rate'] if fd['open_rate'] is not None else 0
            hr_val = fd['high_rate'] if fd['high_rate'] is not None else 0
            cr_val = fd['close_rate'] if fd['close_rate'] is not None else 0
            marker = ""
            if de['a_triggered'] and de['a_day'] == j:
                marker += " *A"
            if de['b_triggered'] and de['b_day'] == j:
                marker += " *B"
            if de['c_triggered'] and de['c_day'] == j:
                marker += " *C"
            print(f"    T+{j:>2}: open_rate={or_val:>6.1f}% high_rate={hr_val:>6.1f}% "
                  f"close_rate={cr_val:>6.1f}%{marker}")

        triggers = []
        if de['a_triggered']:
            triggers.append(f"方式A在T+{de['a_day']}")
        if de['b_triggered']:
            triggers.append(f"方式B在T+{de['b_day']}")
        if de['c_triggered']:
            triggers.append(f"方式C在T+{de['c_day']}")
        if not triggers:
            triggers.append("均未触发")
        print(f"  第二波触发: {' / '.join(triggers)}")

    # 时间窗口分组分析
    print(f"\n\n{'=' * 80}")
    print("按时间窗口分组的触发率分析")
    print("=" * 80)
    print(f"{'窗口':<12} {'方式A占比':<12} {'方式B占比':<12} {'方式C占比':<12}")
    print("-" * 50)

    windows = [('T+1~5', 1, 5), ('T+6~10', 6, 10), ('T+11~15', 11, 15), ('T+16~20', 16, 20)]
    for wname, ws, we in windows:
        a_cnt = sum(stats['A_dist'].get(n, 0) for n in range(ws, we+1))
        b_cnt = sum(stats['B_dist'].get(n, 0) for n in range(ws, we+1))
        c_cnt = sum(stats['C_dist'].get(n, 0) for n in range(ws, we+1))
        a_pct = a_cnt / stats['A_triggered'] * 100 if stats['A_triggered'] > 0 else 0
        b_pct = b_cnt / stats['B_triggered'] * 100 if stats['B_triggered'] > 0 else 0
        c_pct = c_cnt / stats['C_triggered'] * 100 if stats['C_triggered'] > 0 else 0
        print(f"{wname:<12} {a_pct:<11.1f}% {b_pct:<11.1f}% {c_pct:<11.1f}%")

    conn.close()
    print(f"\n\n分析完成! 时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")

if __name__ == '__main__':
    main()
