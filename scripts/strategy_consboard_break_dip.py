#!/usr/bin/env python3
"""连板断板后低吸策略 (Task #96) - rule2 Phase-1/Stats

策略思路:
  股票至少连续2天涨停(连板), 然后某天断板打开, 断板后次日低吸修复机会。
  游资逻辑: 连板股吸引关注, 断板当天获利盘/追高盘恐慌出逃,
           若核心资金未撤退, 次日(T+1)可能修复拉升。

时间轴定义 (T=候选买入日):
  T-3, T-2 : 至少连续2天涨停 (连板)
  T-1      : 断板 (未涨停 且 close_rate <= -3%)
  T        : 候选买入日 (今天)

涨停判定:
  主板   close >= round(preclose*1.10, 2)
  创/科   close >= round(preclose*1.20, 2)

用法:
  # 单月扫描 + 打印候选股前5后5日 hour 级明细 + 统计
  python3 scripts/strategy_consboard_break_dip.py 2026-04

  # 区间统计 (不打印明细, 只汇总收益) - 支持年份或年份区间
  python3 scripts/strategy_consboard_break_dip.py 2021-2026 --stats
  python3 scripts/strategy_consboard_break_dip.py 2024      --stats

输出:
  脚本: /home/AIWealth/scripts/strategy_consboard_break_dip.py
  日志: /home/AIWealth/scripts/logs/consboard_break_dip.log
"""
import sqlite3
import sys
import math
from collections import defaultdict
from datetime import datetime, timedelta

# ============== 配置区 ==============
DB_PATH = '/home/AIWealth/data/stocks.db'

# 连板断板筛选阈值
CONS_LIMITUP_MIN = 2         # T-1 之前至少连续涨停天数 (连板)
BREAK_DROP_MAX = -3.0        # T-1 断板日 close_rate 上限 (<= -3%)
TURN_MIN = 2.0               # T日最低换手率 (%)
PRINT_DAYS_BEFORE = 5        # 输出明细: 前5日
PRINT_DAYS_AFTER = 5         # 输出明细: 后5日

# 收益分析: 持有周期 (T+N 收盘卖出)
HOLD_DAYS = [1, 2, 3]
# 低开阈值 (open_rate < 该值视为低开)
LOW_OPEN_THRESHOLD = 0.0
# ===================================


def sv(v):
    """安全转float, 处理 None / NaN"""
    if v is None:
        return None
    try:
        f = float(v)
        if math.isnan(f):
            return None
        return f
    except (TypeError, ValueError):
        return None


def get_limit_up_threshold(code):
    """根据股票代码返回涨停阈值"""
    if code.startswith('sz.30') or code.startswith('sh.68'):
        return 1.20  # 创业板 / 科创板
    return 1.10      # 主板


def is_limit_up(close, preclose, code):
    """严格涨停判定: round(close/preclose, 2) >= 阈值"""
    pc = sv(preclose)
    cl = sv(close)
    if pc is None or cl is None or pc == 0:
        return False
    return round(cl / pc, 2) >= get_limit_up_threshold(code)


def is_one_word_board(row):
    """一字板判定: open == high == low == close"""
    o = sv(row.get('open'))
    h = sv(row.get('high'))
    l = sv(row.get('low'))
    c = sv(row.get('close'))
    if None in (o, h, l, c):
        return False
    return o == h == l == c


def has_hour_data(row):
    """检查是否有 hour 级数据"""
    return sv(row.get('hour1_open')) is not None


def fmt_pct(v):
    return '  N/A   ' if v is None else f'{v:+6.2f}%'


def fmt_price(v):
    return '  N/A   ' if v is None else f'{v:8.3f}'


def fmt_vol(v):
    return '   N/A    ' if v is None else f'{int(v):10d}'


def fmt_amt(v):
    return '     N/A     ' if v is None else f'{v:13.0f}'


def fmt_turn(v):
    return '  N/A ' if v is None else f'{v:5.2f}%'


FLDS = (
    'date,code,code_name,preclose,open,high,low,close,close_rate,open_rate,'
    'volume,amount,turn,isST,'
    'hour1_open,hour1_high,hour1_low,hour1_close,hour1_open_rate,hour1_close_rate,hour1_volume,hour1_amount,'
    'hour2_open,hour2_high,hour2_low,hour2_close,hour2_open_rate,hour2_close_rate,hour2_volume,hour2_amount,'
    'hour3_open,hour3_high,hour3_low,hour3_close,hour3_open_rate,hour3_close_rate,hour3_volume,hour3_amount,'
    'hour4_open,hour4_high,hour4_low,hour4_close,hour4_open_rate,hour4_close_rate,hour4_volume,hour4_amount'
)


def load_range(conn, dt_lo, dt_hi):
    """加载 [dt_lo, dt_hi) 数据到内存
    返回: (all_dates, date_idx, by_code)
    """
    cur = conn.cursor()
    cur.execute(
        'SELECT DISTINCT date FROM stock_kline WHERE date>=? AND date<? ORDER BY date',
        (dt_lo, dt_hi),
    )
    all_dates = [r[0] for r in cur.fetchall()]
    date_idx = {d: i for i, d in enumerate(all_dates)}

    keys = FLDS.split(',')
    cur.execute(
        f"SELECT {FLDS} FROM stock_kline WHERE date>=? AND date<? AND code NOT LIKE 'bj.%'",
        (dt_lo, dt_hi),
    )
    by_code = defaultdict(dict)
    n_rows = 0
    for row in cur.fetchall():
        d, c = row[0], row[1]
        rec = {keys[i]: row[i] for i in range(len(keys))}
        by_code[c][d] = rec
        n_rows += 1
    return all_dates, date_idx, by_code, n_rows


def find_candidates(by_code, all_dates, date_idx, target_date):
    """筛选 target_date 的连板断板后低吸候选股

    条件:
      T-3, T-2 均涨停 (连续 >= CONS_LIMITUP_MIN 天)
      T-1 断板: 未涨停 且 close_rate <= BREAK_DROP_MAX
      非ST / 非一字板(T日) / 非北交所 / T日 turn > TURN_MIN
    """
    t_idx = date_idx.get(target_date)
    # 需要足够历史: T-1(断板), T-2/T-3(连板)
    if t_idx is None or t_idx < CONS_LIMITUP_MIN + 1:
        return []

    break_date = all_dates[t_idx - 1]  # T-1 断板日

    candidates = []
    for code, dm in by_code.items():
        if code.startswith('bj.'):
            continue

        t_row = dm.get(target_date)
        break_row = dm.get(break_date)
        if not t_row or not break_row:
            continue

        # 1. T-1 断板: 未涨停 且 close_rate <= -3%
        if is_limit_up(break_row.get('close'), break_row.get('preclose'), code):
            continue
        break_cr = sv(break_row.get('close_rate'))
        if break_cr is None or break_cr > BREAK_DROP_MAX:
            continue

        # 2. T-2, T-3 ... 至少连续 CONS_LIMITUP_MIN 天涨停 (紧邻断板日之前)
        cons = 0
        for k in range(2, t_idx + 1):  # T-2, T-3, ...
            d = all_dates[t_idx - k]
            r = dm.get(d)
            if r and is_limit_up(r.get('close'), r.get('preclose'), code):
                cons += 1
            else:
                break
        if cons < CONS_LIMITUP_MIN:
            continue

        # 3. ST 过滤 (以断板日/今日信息判定)
        if break_row.get('isST') == 1 or t_row.get('isST') == 1:
            continue
        name = t_row.get('code_name') or break_row.get('code_name') or ''
        if 'ST' in name.upper():
            continue

        # 4. T日非一字板 (一字板无法买入)
        if is_one_word_board(t_row):
            continue

        # 5. T日换手率
        turn = sv(t_row.get('turn'))
        if turn is None or turn <= TURN_MIN:
            continue

        candidates.append({
            'code': code,
            'name': name,
            'cons_boards': cons,
            'break_rate': break_cr,
            'open_rate': sv(t_row.get('open_rate')),
            'turn': turn,
        })

    candidates.sort(key=lambda x: x['code'])
    return candidates


def print_candidate_detail(by_code, all_dates, date_idx, target_date, cand):
    """打印某候选股的前5后5日 hour 级明细"""
    code = cand['code']
    name = cand['name']
    t_idx = date_idx[target_date]

    print()
    print(f'========== {target_date} 候选股: {code} {name} ==========')
    print(f'连板天数={cand["cons_boards"]}  断板日close_rate={cand["break_rate"]:+.2f}%  '
          f'今日open_rate={fmt_pct(cand["open_rate"]).strip()}  turn={cand["turn"]:.2f}%')
    print()
    header = (
        f'{"日期":10s} | Hr | {"Open":>8s} | {"High":>8s} | {"Low":>8s} | {"Close":>8s} | '
        f'{"Open_Rt":>8s} | {"Close_Rt":>8s} | {"Volume":>10s} | {"Amount":>13s} | {"Turn":>6s}'
    )
    print(header)
    print('-' * len(header))

    lo_idx = max(0, t_idx - PRINT_DAYS_BEFORE)
    hi_idx = min(len(all_dates) - 1, t_idx + PRINT_DAYS_AFTER)
    dm = by_code.get(code, {})

    for di in range(lo_idx, hi_idx + 1):
        d = all_dates[di]
        row = dm.get(d)
        marker = '  <== T日' if d == target_date else ''
        if not row:
            print(f'{d:10s} |  -  |  无数据{marker}')
            continue
        if not has_hour_data(row):
            print(
                f'{d:10s} |  D | {fmt_price(sv(row.get("open")))} | {fmt_price(sv(row.get("high")))} | '
                f'{fmt_price(sv(row.get("low")))} | {fmt_price(sv(row.get("close")))} | '
                f'{fmt_pct(sv(row.get("open_rate")))} | {fmt_pct(sv(row.get("close_rate")))} | '
                f'{fmt_vol(sv(row.get("volume")))} | {fmt_amt(sv(row.get("amount")))} | {fmt_turn(sv(row.get("turn")))}{marker}'
            )
            continue
        day_turn = sv(row.get('turn'))
        day_vol = sv(row.get('volume'))
        for h in (1, 2, 3, 4):
            ho = sv(row.get(f'hour{h}_open'))
            hh = sv(row.get(f'hour{h}_high'))
            hl = sv(row.get(f'hour{h}_low'))
            hc = sv(row.get(f'hour{h}_close'))
            hor = sv(row.get(f'hour{h}_open_rate'))
            hcr = sv(row.get(f'hour{h}_close_rate'))
            hv = sv(row.get(f'hour{h}_volume'))
            ha = sv(row.get(f'hour{h}_amount'))
            ht = None
            if day_turn is not None and day_vol and day_vol > 0 and hv is not None:
                ht = day_turn * hv / day_vol
            date_show = d if h == 1 else ' ' * 10
            tail = marker if h == 1 else ''
            print(
                f'{date_show:10s} |  {h} | {fmt_price(ho)} | {fmt_price(hh)} | {fmt_price(hl)} | {fmt_price(hc)} | '
                f'{fmt_pct(hor)} | {fmt_pct(hcr)} | {fmt_vol(hv)} | {fmt_amt(ha)} | {fmt_turn(ht)}{tail}'
            )


def compute_returns(by_code, all_dates, date_idx, target_date, cand):
    """计算候选股各买入方案 / 持有周期的收益率

    买入方案:
      A_hr1open : T日 hour1_open 买入 (无低开条件)
      B_lowopen : 仅当T日低开(open_rate<LOW_OPEN_THRESHOLD), T日 hour2_open 买入
    卖出: 持有到 T+N 收盘 (close)
    返回: dict {plan: {N: ret_pct or None}}, 及是否低开标记
    """
    code = cand['code']
    dm = by_code.get(code, {})
    t_idx = date_idx[target_date]
    t_row = dm.get(target_date)

    result = {'is_low_open': False, 'A_hr1open': {}, 'B_lowopen': {}}
    if not t_row:
        return result

    opr = sv(t_row.get('open_rate'))
    is_low = opr is not None and opr < LOW_OPEN_THRESHOLD
    result['is_low_open'] = is_low

    # 买入价
    buy_a = sv(t_row.get('hour1_open'))      # 方案A: T hour1 开盘价
    buy_b = sv(t_row.get('hour2_open'))      # 方案B: T hour2 开盘价 (低开后)

    for N in HOLD_DAYS:
        sidx = t_idx + N
        if sidx >= len(all_dates):
            result['A_hr1open'][N] = None
            result['B_lowopen'][N] = None
            continue
        sell_row = dm.get(all_dates[sidx])
        sell_close = sv(sell_row.get('close')) if sell_row else None
        if sell_close is None:
            result['A_hr1open'][N] = None
            result['B_lowopen'][N] = None
            continue
        # 方案A
        if buy_a and buy_a > 0:
            result['A_hr1open'][N] = (sell_close - buy_a) / buy_a * 100.0
        else:
            result['A_hr1open'][N] = None
        # 方案B (仅低开时有效)
        if is_low and buy_b and buy_b > 0:
            result['B_lowopen'][N] = (sell_close - buy_b) / buy_b * 100.0
        else:
            result['B_lowopen'][N] = None
    return result


def summarize(records, title):
    """统计各方案各持有周期的收益/胜率并打印"""
    print()
    print(f'================ {title} ================')
    print(f'候选样本总数: {len(records)}')
    low_cnt = sum(1 for r in records if r['is_low_open'])
    print(f'其中T日低开(open_rate<{LOW_OPEN_THRESHOLD:.1f}%): {low_cnt} '
          f'({low_cnt/max(1,len(records))*100:.1f}%)')
    print()
    header = f'{"方案":16s} | {"持有":>4s} | {"样本":>5s} | {"平均收益":>9s} | {"胜率":>7s} | {"中位数":>8s}'
    print(header)
    print('-' * len(header))
    for plan, plan_name in (('A_hr1open', 'A:T-hr1开盘买'), ('B_lowopen', 'B:低开T-hr2买')):
        for N in HOLD_DAYS:
            rets = [r[plan][N] for r in records if r[plan].get(N) is not None]
            if not rets:
                print(f'{plan_name:16s} | T+{N:<2d} | {0:>5d} | {"N/A":>9s} | {"N/A":>7s} | {"N/A":>8s}')
                continue
            avg = sum(rets) / len(rets)
            wins = sum(1 for x in rets if x > 0)
            win_rate = wins / len(rets) * 100.0
            med = sorted(rets)[len(rets) // 2]
            print(f'{plan_name:16s} | T+{N:<2d} | {len(rets):>5d} | {avg:>+8.2f}% | '
                  f'{win_rate:>6.1f}% | {med:>+7.2f}%')


def month_range(target_month):
    """月份 -> (first_day, next_first)"""
    year, mon = map(int, target_month.split('-'))
    first_day = f'{year:04d}-{mon:02d}-01'
    next_first = f'{year+1:04d}-01-01' if mon == 12 else f'{year:04d}-{mon+1:02d}-01'
    return first_day, next_first


def run_month(conn, target_month, print_detail=True):
    """扫描单月, 打印明细(可选)并返回收益记录"""
    first_day, next_first = month_range(target_month)
    dt_lo = (datetime.strptime(first_day, '%Y-%m-%d') - timedelta(days=30)).strftime('%Y-%m-%d')
    dt_hi = (datetime.strptime(next_first, '%Y-%m-%d') + timedelta(days=20)).strftime('%Y-%m-%d')

    all_dates, date_idx, by_code, n_rows = load_range(conn, dt_lo, dt_hi)
    target_dates = [d for d in all_dates if first_day <= d < next_first]
    print(f'[加载] {dt_lo} ~ {dt_hi}  交易日={len(all_dates)}  目标日={len(target_dates)}  '
          f'股票数={len(by_code)}  行数={n_rows}')

    records = []
    daily_stats = []
    for td in target_dates:
        cands = find_candidates(by_code, all_dates, date_idx, td)
        daily_stats.append((td, len(cands)))
        if print_detail:
            if not cands:
                print(f'\n---------- {td}: 无候选股 ----------')
            else:
                print(f'\n#################### {td}: 共 {len(cands)} 只候选股 ####################')
        for c in cands:
            if print_detail:
                print_candidate_detail(by_code, all_dates, date_idx, td, c)
            rec = compute_returns(by_code, all_dates, date_idx, td, c)
            rec['date'] = td
            rec['code'] = c['code']
            records.append(rec)

    if print_detail:
        print()
        print(f'---- {target_month} 每日候选分布 ----')
        for d, n in daily_stats:
            print(f'  {d}: {n} 只')
    return records


def iter_months(period):
    """period 可为 '2026-04'(单月) / '2024'(单年) / '2021-2026'(年区间)
    返回月份列表 ['2026-04', ...]
    """
    if '-' in period and len(period.split('-')[0]) == 4 and len(period.split('-')) == 2 \
            and len(period.split('-')[1]) <= 2:
        # 形如 2026-04 单月
        return [period]
    # 年份或年区间
    if '-' in period:
        y0, y1 = map(int, period.split('-'))
    else:
        y0 = y1 = int(period)
    months = []
    for y in range(y0, y1 + 1):
        for m in range(1, 13):
            months.append(f'{y:04d}-{m:02d}')
    return months


def main():
    period = sys.argv[1] if len(sys.argv) > 1 else '2026-04'
    stats_only = '--stats' in sys.argv[2:]

    months = iter_months(period)
    single_month = len(months) == 1
    print_detail = single_month and not stats_only

    print(f'================ 连板断板后低吸策略 (Task #96) ================')
    print(f'周期: {period}  ({len(months)} 个月)  明细输出={print_detail}')
    print(f'参数: 连板>={CONS_LIMITUP_MIN}天  断板close_rate<={BREAK_DROP_MAX}%  '
          f'turn>{TURN_MIN}%  持有={HOLD_DAYS}  低开阈值<{LOW_OPEN_THRESHOLD}%')

    conn = sqlite3.connect(DB_PATH)
    all_records = []
    try:
        for mon in months:
            recs = run_month(conn, mon, print_detail=print_detail)
            all_records.extend(recs)
            if not single_month:
                print(f'[{mon}] 候选 {len(recs)} 只 (累计 {len(all_records)})')
    finally:
        conn.close()

    # 全体统计
    summarize(all_records, f'{period} 全样本收益统计')

    # 仅低开子集统计 (方案A在低开样本上的表现, 对比低开是否更优)
    low_records = [r for r in all_records if r['is_low_open']]
    if low_records:
        summarize(low_records, f'{period} 仅T日低开子集统计')


if __name__ == '__main__':
    main()
