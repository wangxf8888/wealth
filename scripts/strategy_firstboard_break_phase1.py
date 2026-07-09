#!/usr/bin/env python3
"""首板断板低吸策略 Phase-1: 候选股筛选与前5后5日hour级OHLC明细输出

逻辑:
  T-1日首板涨停 (前5个交易日均未涨停)
  T日未能连板封板, open_rate 在 -5% ~ +5%
  非ST / 非一字板 / 非北交所 / turn > 3%

输出:
  对每只候选股, 打印 T-5 ~ T+5 共11个交易日的 hour 级 OHLC + 涨跌幅 + 换手率明细

用法:
  python3 scripts/strategy_firstboard_break_phase1.py 2026-04
"""
import sqlite3
import sys
import math
from collections import defaultdict
from datetime import datetime, timedelta

# ============== 配置区 ==============
DB_PATH = '/home/AIWealth/data/stocks.db'
TARGET_MONTH = sys.argv[1] if len(sys.argv) > 1 else '2026-04'

# 筛选阈值
OPEN_RATE_MIN = -5.0      # T日开盘最低跌幅 (-5%)
OPEN_RATE_MAX = 5.0       # T日开盘最高涨幅 (+5%), 未能连板封板
TURN_MIN = 3.0            # T日最低换手率 (%)
FIRSTBOARD_LOOKBACK = 5   # "首板"回溯天数 (T-1涨停日的前5个交易日)
PRINT_DAYS_BEFORE = 5     # 输出明细: 前5日
PRINT_DAYS_AFTER = 5      # 输出明细: 后5日
# ===================================


def sv(v):
    """安全转float, 处理 None / NaN / 0"""
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
    """检查是否有 hour 级数据 (至少 hour1_open 不为空)"""
    return sv(row.get('hour1_open')) is not None


def fmt_pct(v):
    if v is None:
        return '  N/A   '
    return f'{v:+6.2f}%'


def fmt_price(v):
    if v is None:
        return '  N/A   '
    return f'{v:8.3f}'


def fmt_vol(v):
    if v is None:
        return '   N/A    '
    return f'{int(v):10d}'


def fmt_amt(v):
    if v is None:
        return '     N/A     '
    return f'{v:13.0f}'


def fmt_turn(v):
    if v is None:
        return '  N/A '
    return f'{v:5.2f}%'


def load_data(conn, target_month):
    """加载月份相关数据
    范围: 月份起始日前 15 个交易日 ~ 月份结束日后 10 个交易日
    返回: (all_dates, date_idx, target_business_dates, by_code_date)
    """
    cur = conn.cursor()
    # 月份范围
    year, mon = target_month.split('-')
    year, mon = int(year), int(mon)
    first_day = f'{year:04d}-{mon:02d}-01'
    if mon == 12:
        next_first = f'{year+1:04d}-01-01'
    else:
        next_first = f'{year:04d}-{mon+1:02d}-01'

    # 扩展前后窗口
    dt_lo = (datetime.strptime(first_day, '%Y-%m-%d') - timedelta(days=30)).strftime('%Y-%m-%d')
    dt_hi = (datetime.strptime(next_first, '%Y-%m-%d') + timedelta(days=20)).strftime('%Y-%m-%d')

    cur.execute(
        'SELECT DISTINCT date FROM stock_kline WHERE date>=? AND date<? ORDER BY date',
        (dt_lo, dt_hi),
    )
    all_dates = [r[0] for r in cur.fetchall()]
    date_idx = {d: i for i, d in enumerate(all_dates)}
    target_dates = [d for d in all_dates if first_day <= d < next_first]

    # 加载所有字段
    flds = (
        'date,code,code_name,preclose,open,high,low,close,close_rate,open_rate,'
        'volume,amount,turn,isST,'
        'hour1_open,hour1_high,hour1_low,hour1_close,hour1_open_rate,hour1_close_rate,hour1_volume,hour1_amount,'
        'hour2_open,hour2_high,hour2_low,hour2_close,hour2_open_rate,hour2_close_rate,hour2_volume,hour2_amount,'
        'hour3_open,hour3_high,hour3_low,hour3_close,hour3_open_rate,hour3_close_rate,hour3_volume,hour3_amount,'
        'hour4_open,hour4_high,hour4_low,hour4_close,hour4_open_rate,hour4_close_rate,hour4_volume,hour4_amount'
    )
    cur.execute(
        f"SELECT {flds} FROM stock_kline WHERE date>=? AND date<? AND code NOT LIKE 'bj.%'",
        (dt_lo, dt_hi),
    )
    keys = flds.split(',')
    by_code = defaultdict(dict)
    n_rows = 0
    for row in cur.fetchall():
        d = row[0]
        c = row[1]
        rec = {keys[i]: row[i] for i in range(len(keys))}
        by_code[c][d] = rec
        n_rows += 1
    print(f'[加载] {dt_lo} ~ {dt_hi}  交易日={len(all_dates)}  目标日={len(target_dates)}  股票数={len(by_code)}  行数={n_rows}')
    return all_dates, date_idx, target_dates, by_code


def find_candidates(by_code, all_dates, date_idx, target_date):
    """筛选 target_date 的候选股 (T-1日首板涨停, T日未封板)"""
    t_idx = date_idx.get(target_date)
    if t_idx is None or t_idx < 1:
        return []
    prev_date = all_dates[t_idx - 1]  # T-1日 (涨停日)

    # T-1日的前 FIRSTBOARD_LOOKBACK 个交易日 (T-6 ~ T-2)
    lookback_start = t_idx - 1 - FIRSTBOARD_LOOKBACK
    if lookback_start < 0:
        return []
    lookback_dates = all_dates[lookback_start:t_idx - 1]  # [T-6, T-5, T-4, T-3, T-2]

    candidates = []
    for code, dm in by_code.items():
        # 北交所 (load_data 已过滤, 这里二次保险)
        if code.startswith('bj.'):
            continue

        prev_row = dm.get(prev_date)
        t_row = dm.get(target_date)
        if not prev_row or not t_row:
            continue

        # 1. T-1日涨停 (首板候选)
        if not is_limit_up(prev_row.get('close'), prev_row.get('preclose'), code):
            continue

        # 2. ST 过滤
        is_st = prev_row.get('isST')
        if is_st == 1:
            continue
        name = prev_row.get('code_name') or ''
        if 'ST' in name.upper():
            continue

        # 3. "首板": T-1日的前5个交易日均未涨停
        is_first_board = True
        for ld in lookback_dates:
            lrow = dm.get(ld)
            if lrow and is_limit_up(lrow.get('close'), lrow.get('preclose'), code):
                is_first_board = False
                break
        if not is_first_board:
            continue

        # 4. T日 open_rate 区间
        opr = sv(t_row.get('open_rate'))
        if opr is None or opr < OPEN_RATE_MIN or opr > OPEN_RATE_MAX:
            continue

        # 5. T日非一字板
        if is_one_word_board(t_row):
            continue

        # 6. T日换手率
        turn = sv(t_row.get('turn'))
        if turn is None or turn <= TURN_MIN:
            continue

        # 收集候选
        candidates.append({
            'code': code,
            'name': name,
            'prev_close': sv(prev_row.get('close')),
            'prev_preclose': sv(prev_row.get('preclose')),
            'open_rate': opr,
            'turn': turn,
        })

    candidates.sort(key=lambda x: x['code'])
    return candidates


def print_candidate_detail(by_code, all_dates, date_idx, target_date, cand):
    """打印某候选股的前5后5日 hour 级明细"""
    code = cand['code']
    name = cand['name']
    pc = cand['prev_preclose']
    cl = cand['prev_close']
    ratio = round(cl / pc, 2) if (pc and cl) else 0
    t_idx = date_idx[target_date]

    print()
    print(f'========== {target_date} 候选股: {code} {name} ==========')
    print(f'昨日涨停(首板): close={cl:.3f}, preclose={pc:.3f}, ratio={ratio:.2f}')
    print(f'今日开盘: open_rate={cand["open_rate"]:+.2f}%, turn={cand["turn"]:.2f}%')
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
        if not row:
            print(f'{d:10s} |  -  |  无数据')
            continue
        if not has_hour_data(row):
            # 没有 hour 数据, 打印日级摘要
            print(
                f'{d:10s} |  D | {fmt_price(sv(row.get("open")))} | {fmt_price(sv(row.get("high")))} | '
                f'{fmt_price(sv(row.get("low")))} | {fmt_price(sv(row.get("close")))} | '
                f'{fmt_pct(sv(row.get("open_rate")))} | {fmt_pct(sv(row.get("close_rate")))} | '
                f'{fmt_vol(sv(row.get("volume")))} | {fmt_amt(sv(row.get("amount")))} | {fmt_turn(sv(row.get("turn")))}'
            )
            continue
        # 4个小时
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
            # 估算该小时占当日换手率比例
            ht = None
            if day_turn is not None and day_vol and day_vol > 0 and hv is not None:
                ht = day_turn * hv / day_vol
            date_show = d if h == 1 else ' ' * 10
            print(
                f'{date_show:10s} |  {h} | {fmt_price(ho)} | {fmt_price(hh)} | {fmt_price(hl)} | {fmt_price(hc)} | '
                f'{fmt_pct(hor)} | {fmt_pct(hcr)} | {fmt_vol(hv)} | {fmt_amt(ha)} | {fmt_turn(ht)}'
            )


def main():
    print(f'================ 首板断板低吸 Phase-1 ================')
    print(f'目标月份: {TARGET_MONTH}')
    print(f'参数: open_rate ∈ [{OPEN_RATE_MIN}%, {OPEN_RATE_MAX}%]  turn > {TURN_MIN}%  首板回溯={FIRSTBOARD_LOOKBACK}日')
    print()

    conn = sqlite3.connect(DB_PATH)
    try:
        all_dates, date_idx, target_dates, by_code = load_data(conn, TARGET_MONTH)
    finally:
        pass  # 数据已读入内存

    if not target_dates:
        print(f'[警告] 月份 {TARGET_MONTH} 无交易日数据')
        conn.close()
        return

    total_cnt = 0
    daily_stats = []
    for td in target_dates:
        cands = find_candidates(by_code, all_dates, date_idx, td)
        daily_stats.append((td, len(cands)))
        total_cnt += len(cands)
        if not cands:
            print(f'\n---------- {td}: 无候选股 ----------')
            continue
        print(f'\n#################### {td}: 共 {len(cands)} 只候选股 ####################')
        for c in cands:
            print_candidate_detail(by_code, all_dates, date_idx, td, c)

    conn.close()

    # 统计摘要
    print()
    print('================ 统计摘要 ================')
    print(f'月份: {TARGET_MONTH}')
    print(f'交易日数: {len(target_dates)}')
    print(f'候选股总数: {total_cnt}')
    print(f'日均候选: {total_cnt / max(1, len(target_dates)):.2f}')
    print()
    print('每日分布:')
    for d, n in daily_stats:
        print(f'  {d}: {n} 只')


if __name__ == '__main__':
    main()
