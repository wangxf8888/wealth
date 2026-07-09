#!/usr/bin/env python3
"""
N字/V字形态高开买入盈利概率研究
识别V字形态(下跌->反转启动)和N字形态(上涨->回调->再启动)，
在启动日高开时开盘买入，统计后续盈利概率和收益分布。
"""

import sqlite3
import statistics
from collections import defaultdict

# ============ 配置参数 ============
DB_PATH = '/home/AIWealth/data/stocks.db'
# V字形态参数
V_DOWN_DAYS = 3              # 下跌观察天数
V_MIN_DOWN_DAYS = 2          # 至少N天下跌
V_CUM_DROP = -5.0            # 累计跌幅阈值(%)
# N字形态参数
N_RISE_WINDOW = [5, 3]       # 上涨窗口 T-5到T-3
N_RISE_THRESHOLD = 5.0       # 上涨阈值(%)
N_PULLBACK_DAYS = 2          # 回调天数
N_PULLBACK_THRESHOLD = -3.0  # 回调累计阈值(%)
# 通用参数
GAP_UP_MIN = 2.0             # 高开最低要求(%)
HOLD_PERIODS = [1, 2, 3, 5]  # 持有期(交易日)
DETAIL_MONTHS = ['2026-03', '2026-04']
# 时间范围
START_DATE = '2021-01-01'
END_DATE = '2026-05-31'
# ==================================


def get_connection():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def load_stock_data(conn):
    """加载所有符合条件的股票数据，按code+date排序"""
    sql = """
    SELECT date, code, code_name, preclose, open, open_rate, high, high_rate,
           low, low_rate, close, close_rate, volume, amount, turn,
           hour1_open_rate, hour1_close_rate, hour2_open_rate, hour2_close_rate,
           hour3_open_rate, hour3_close_rate, hour4_open_rate, hour4_close_rate,
           isST
    FROM stock_kline
    WHERE date >= ? AND date <= ?
      AND (code LIKE 'sz.300%' OR code LIKE 'sh.688%')
      AND isST = 0
    ORDER BY code, date
    """
    cursor = conn.execute(sql, (START_DATE, END_DATE))
    stock_data = defaultdict(list)
    for row in cursor:
        stock_data[row['code']].append(dict(row))
    return stock_data


def check_v_shape(rows, t_idx):
    """检查T日是否为V字形态启动日"""
    if t_idx < V_DOWN_DAYS:
        return False
    t_row = rows[t_idx]
    if t_row['open_rate'] is None or t_row['close_rate'] is None:
        return False
    if t_row['open_rate'] < GAP_UP_MIN or t_row['close_rate'] <= 0:
        return False

    down_count = 0
    cum_drop = 0.0
    for i in range(t_idx - V_DOWN_DAYS, t_idx):
        cr = rows[i]['close_rate']
        if cr is None:
            return False
        if cr < 0:
            down_count += 1
        cum_drop += cr

    if down_count < V_MIN_DOWN_DAYS:
        return False
    if cum_drop > V_CUM_DROP:
        return False
    return True


def check_n_shape(rows, t_idx):
    """检查T日是否为N字形态启动日"""
    if t_idx < 5:
        return False
    t_row = rows[t_idx]
    if t_row['open_rate'] is None or t_row['close_rate'] is None:
        return False
    if t_row['open_rate'] < GAP_UP_MIN or t_row['close_rate'] <= 0:
        return False

    rise_start = t_idx - N_RISE_WINDOW[0]  # T-5
    rise_end = t_idx - N_RISE_WINDOW[1]    # T-3
    if rise_start < 0:
        return False

    cum_rise = 0.0
    has_big_day = False
    for i in range(rise_start, rise_end + 1):
        cr = rows[i]['close_rate']
        hr = rows[i]['high_rate']
        if cr is None or hr is None:
            return False
        cum_rise += cr
        if hr >= N_RISE_THRESHOLD:
            has_big_day = True

    if cum_rise < N_RISE_THRESHOLD and not has_big_day:
        return False

    pullback_cum = 0.0
    all_down = True
    for i in range(t_idx - N_PULLBACK_DAYS, t_idx):
        cr = rows[i]['close_rate']
        if cr is None:
            return False
        pullback_cum += cr
        if cr >= 0:
            all_down = False

    if not all_down and pullback_cum > N_PULLBACK_THRESHOLD:
        return False
    return True


def calc_returns(rows, t_idx, buy_price):
    """计算T日买入后各持有期的收益"""
    max_period = max(HOLD_PERIODS)
    if t_idx + max_period >= len(rows):
        return None

    results = {}
    max_high = -999.0
    min_low = 999.0

    for d in range(1, max_period + 1):
        future_row = rows[t_idx + d]
        if future_row['close'] is None or future_row['high'] is None or future_row['low'] is None:
            return None
        if d in HOLD_PERIODS:
            ret = (future_row['close'] - buy_price) / buy_price * 100.0
            results[d] = ret
        high_ret = (future_row['high'] - buy_price) / buy_price * 100.0
        low_ret = (future_row['low'] - buy_price) / buy_price * 100.0
        if high_ret > max_high:
            max_high = high_ret
        if low_ret < min_low:
            min_low = low_ret

    results['max_high'] = max_high
    results['max_drop'] = min_low
    return results


def get_gap_group(open_rate):
    """按高开幅度分组"""
    if open_rate >= 5.0:
        return '5%+'
    elif open_rate >= 3.0:
        return '3-5%'
    else:
        return '2-3%'


def print_stats(events, shape_name):
    """打印统计结果"""
    print(f"\n{'='*70}")
    print(f"  {shape_name}形态统计")
    print(f"{'='*70}")
    print(f"  总事件数: {len(events)}")
    if not events:
        print("  无事件数据")
        return

    print(f"\n  {'持有期':<8} {'盈利数':<8} {'胜率':<10} {'平均收益':<10} {'中位收益':<10}")
    print(f"  {'-'*50}")
    for period in HOLD_PERIODS:
        rets = [e['returns'][period] for e in events if e['returns'] and period in e['returns']]
        if not rets:
            continue
        win_count = sum(1 for r in rets if r > 0)
        win_rate = win_count / len(rets) * 100
        avg_ret = sum(rets) / len(rets)
        med_ret = statistics.median(rets)
        print(f"  T+{period:<6} {win_count:<8} {win_rate:<9.1f}% {avg_ret:<9.2f}% {med_ret:<9.2f}%")

    max_highs = [e['returns']['max_high'] for e in events if e['returns'] and 'max_high' in e['returns']]
    max_drops = [e['returns']['max_drop'] for e in events if e['returns'] and 'max_drop' in e['returns']]
    if max_highs:
        print(f"\n  T+1~T+5最高点平均涨幅: {sum(max_highs)/len(max_highs):.2f}%")
        print(f"  T+1~T+5最高点中位涨幅: {statistics.median(max_highs):.2f}%")
    if max_drops:
        print(f"  T+1~T+5最大回撤平均值: {sum(max_drops)/len(max_drops):.2f}%")
        print(f"  T+1~T+5最大回撤中位值: {statistics.median(max_drops):.2f}%")

    print(f"\n  按高开幅度分组:")
    print(f"  {'分组':<8} {'事件数':<8} {'T+1胜率':<10} {'T+1均收益':<10} {'T+3胜率':<10} {'T+3均收益':<10}")
    print(f"  {'-'*60}")
    groups = defaultdict(list)
    for e in events:
        g = get_gap_group(e['open_rate'])
        groups[g].append(e)

    for g_name in ['2-3%', '3-5%', '5%+']:
        g_events = groups[g_name]
        if not g_events:
            continue
        rets1 = [e['returns'][1] for e in g_events if e['returns'] and 1 in e['returns']]
        rets3 = [e['returns'][3] for e in g_events if e['returns'] and 3 in e['returns']]
        wr1 = sum(1 for r in rets1 if r > 0) / len(rets1) * 100 if rets1 else 0
        avg1 = sum(rets1) / len(rets1) if rets1 else 0
        wr3 = sum(1 for r in rets3 if r > 0) / len(rets3) * 100 if rets3 else 0
        avg3 = sum(rets3) / len(rets3) if rets3 else 0
        print(f"  {g_name:<8} {len(g_events):<8} {wr1:<9.1f}% {avg1:<9.2f}% {wr3:<9.1f}% {avg3:<9.2f}%")


def print_comparison(v_events, n_events):
    """打印V字 vs N字对比表"""
    print(f"\n{'='*70}")
    print(f"  V字形态 vs N字形态 对比")
    print(f"{'='*70}")
    print(f"  {'指标':<20} {'V字形态':<15} {'N字形态':<15}")
    print(f"  {'-'*50}")
    print(f"  {'事件总数':<20} {len(v_events):<15} {len(n_events):<15}")

    for period in HOLD_PERIODS:
        v_rets = [e['returns'][period] for e in v_events if e['returns'] and period in e['returns']]
        n_rets = [e['returns'][period] for e in n_events if e['returns'] and period in e['returns']]
        v_wr = f"{sum(1 for r in v_rets if r>0)/len(v_rets)*100:.1f}%" if v_rets else "N/A"
        n_wr = f"{sum(1 for r in n_rets if r>0)/len(n_rets)*100:.1f}%" if n_rets else "N/A"
        v_avg = f"{sum(v_rets)/len(v_rets):.2f}%" if v_rets else "N/A"
        n_avg = f"{sum(n_rets)/len(n_rets):.2f}%" if n_rets else "N/A"
        print(f"  {'T+'+str(period)+' 胜率':<20} {v_wr:<15} {n_wr:<15}")
        print(f"  {'T+'+str(period)+' 平均收益':<20} {v_avg:<15} {n_avg:<15}")

    v_mh = [e['returns']['max_high'] for e in v_events if e['returns'] and 'max_high' in e['returns']]
    n_mh = [e['returns']['max_high'] for e in n_events if e['returns'] and 'max_high' in e['returns']]
    v_md = [e['returns']['max_drop'] for e in v_events if e['returns'] and 'max_drop' in e['returns']]
    n_md = [e['returns']['max_drop'] for e in n_events if e['returns'] and 'max_drop' in e['returns']]
    v_mh_s = f"{sum(v_mh)/len(v_mh):.2f}%" if v_mh else "N/A"
    n_mh_s = f"{sum(n_mh)/len(n_mh):.2f}%" if n_mh else "N/A"
    v_md_s = f"{sum(v_md)/len(v_md):.2f}%" if v_md else "N/A"
    n_md_s = f"{sum(n_md)/len(n_md):.2f}%" if n_md else "N/A"
    print(f"  {'最高点平均涨幅':<20} {v_mh_s:<15} {n_mh_s:<15}")
    print(f"  {'最大回撤平均值':<20} {v_md_s:<15} {n_md_s:<15}")


def print_detail(event, rows, t_idx):
    """打印单个事件明细"""
    t_row = rows[t_idx]
    shape = event['shape']
    date = t_row['date']
    code = t_row['code']
    code_name = t_row['code_name']
    open_rate = t_row['open_rate']

    print(f"  形态:{shape} {date} {code} {code_name} | 开盘率:{open_rate:.1f}%")

    prev_strs = []
    for d in range(5, 0, -1):
        idx = t_idx - d
        if idx >= 0:
            cr = rows[idx]['close_rate']
            cr_s = f"{cr:.1f}" if cr is not None else "N/A"
            prev_strs.append(f"D-{d}:{cr_s}%")
        else:
            prev_strs.append(f"D-{d}:N/A")
    print(f"  前5日: {' '.join(prev_strs)}")

    def fmt_h(v):
        return f"{v:.1f}" if v is not None else "N/A"

    h1o = t_row['hour1_open_rate']
    h1c = t_row['hour1_close_rate']
    h2o = t_row['hour2_open_rate']
    h2c = t_row['hour2_close_rate']
    h3o = t_row['hour3_open_rate']
    h3c = t_row['hour3_close_rate']
    h4o = t_row['hour4_open_rate']
    h4c = t_row['hour4_close_rate']
    print(f"  T日hour: h1={fmt_h(h1o)}->{fmt_h(h1c)} h2={fmt_h(h2o)}->{fmt_h(h2c)} "
          f"h3={fmt_h(h3o)}->{fmt_h(h3c)} h4={fmt_h(h4o)}->{fmt_h(h4c)}")

    print(f"  后续5日:")
    max_period = max(HOLD_PERIODS)
    for d in range(1, min(max_period + 1, len(rows) - t_idx)):
        fr = rows[t_idx + d]
        or_s = f"{fr['open_rate']:.1f}" if fr['open_rate'] is not None else "N/A"
        hr_s = f"{fr['high_rate']:.1f}" if fr['high_rate'] is not None else "N/A"
        lr_s = f"{fr['low_rate']:.1f}" if fr['low_rate'] is not None else "N/A"
        cr_s = f"{fr['close_rate']:.1f}" if fr['close_rate'] is not None else "N/A"
        print(f"    T+{d}: O={or_s}% H={hr_s}% L={lr_s}% C={cr_s}%")

    rets = event['returns']
    if rets:
        r1 = f"{rets.get(1, 0):.1f}" if 1 in rets else "N/A"
        r2 = f"{rets.get(2, 0):.1f}" if 2 in rets else "N/A"
        r3 = f"{rets.get(3, 0):.1f}" if 3 in rets else "N/A"
        r5 = f"{rets.get(5, 0):.1f}" if 5 in rets else "N/A"
        mh = f"{rets.get('max_high', 0):.1f}"
        md = f"{rets.get('max_drop', 0):.1f}"
        print(f"  收益: T+1={r1}% T+2={r2}% T+3={r3}% T+5={r5}% 最高={mh}% 最低={md}%")
    else:
        print(f"  收益: 数据不足无法计算")
    print()


def main():
    print("=" * 70)
    print("  N字/V字形态高开买入盈利概率研究")
    print(f"  分析区间: {START_DATE} ~ {END_DATE}")
    print("  股票范围: 创业板(sz.300*) + 科创板(sh.688*), 排除ST")
    print("=" * 70)

    conn = get_connection()
    print("\n[1/4] 加载数据...")
    stock_data = load_stock_data(conn)
    total_stocks = len(stock_data)
    total_rows = sum(len(v) for v in stock_data.values())
    print(f"  加载完成: {total_stocks} 只股票, {total_rows} 条日线数据")

    print("\n[2/4] 扫描形态...")
    v_events = []
    n_events = []
    detail_events = []

    processed = 0
    for code, rows in stock_data.items():
        processed += 1
        if processed % 500 == 0:
            print(f"  进度: {processed}/{total_stocks} ({processed*100//total_stocks}%)")

        for t_idx in range(5, len(rows)):
            t_row = rows[t_idx]
            if t_row['open'] is None or t_row['open'] <= 0:
                continue
            if t_row['open_rate'] is None:
                continue

            buy_price = t_row['open']

            is_v = check_v_shape(rows, t_idx)
            if is_v:
                rets = calc_returns(rows, t_idx, buy_price)
                event = {
                    'shape': 'V',
                    'date': t_row['date'],
                    'code': code,
                    'code_name': t_row['code_name'],
                    'open_rate': t_row['open_rate'],
                    'returns': rets,
                    't_idx': t_idx,
                }
                v_events.append(event)
                if any(t_row['date'].startswith(m) for m in DETAIL_MONTHS):
                    detail_events.append((event, rows, t_idx))

            is_n = check_n_shape(rows, t_idx)
            if is_n:
                rets = calc_returns(rows, t_idx, buy_price)
                event = {
                    'shape': 'N',
                    'date': t_row['date'],
                    'code': code,
                    'code_name': t_row['code_name'],
                    'open_rate': t_row['open_rate'],
                    'returns': rets,
                    't_idx': t_idx,
                }
                n_events.append(event)
                if any(t_row['date'].startswith(m) for m in DETAIL_MONTHS):
                    detail_events.append((event, rows, t_idx))

    print(f"  扫描完成: V字形态 {len(v_events)} 个, N字形态 {len(n_events)} 个")

    print("\n[3/4] 统计分析...")
    v_valid = [e for e in v_events if e['returns'] is not None]
    n_valid = [e for e in n_events if e['returns'] is not None]
    print(f"  有效事件(有后续5日数据): V字={len(v_valid)}, N字={len(n_valid)}")

    print_stats(v_valid, "V字")
    print_stats(n_valid, "N字")
    print_comparison(v_valid, n_valid)

    print(f"\n{'='*70}")
    print(f"  明细输出 (月份: {', '.join(DETAIL_MONTHS)})")
    print(f"{'='*70}")
    detail_events.sort(key=lambda x: (x[0]['date'], x[0]['code']))
    print(f"  共 {len(detail_events)} 条明细\n")
    for event, rows, t_idx in detail_events[:100]:
        print_detail(event, rows, t_idx)

    if len(detail_events) > 100:
        print(f"  ... 省略 {len(detail_events) - 100} 条")

    conn.close()
    print(f"\n{'='*70}")
    print("  分析完成!")
    print(f"{'='*70}")


if __name__ == '__main__':
    main()
