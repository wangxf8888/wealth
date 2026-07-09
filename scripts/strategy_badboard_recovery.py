#!/usr/bin/env python3
"""
烂板次日低开企稳策略 - 研究脚本 (Task #89, 按 rule2 流程)

策略思路:
  昨日涨停但盘中多次打开(烂板)，说明多空分歧大。
  今日如果低开后 hour1 企稳(不再下杀)，可能有修复行情。

候选股定义(T日):
  1. 昨日(T-1)涨停:  close >= round(preclose*ratio, 2)
       主板(sh.6/sz.0) ratio=0.10; 创业板(sz.30/301)/科创板(sh.688) ratio=0.20
  2. 昨日(T-1)烂板:  low < close * 0.97  (盘中回落超过3%又被拉回涨停)
  3. 今日(T)低开:    open < 昨日(T-1)close

T+1 合规:
  - 昨日涨停/烂板是收盘后确定的事实, 今日open是9:25竞价确定的价格
  - hour1企稳(hour1_close>=hour1_open)由hour1收盘(10:00)确定,
    买入最早只能在 hour2_open 及之后, 不使用任何未来数据
  - T日买入当日不可卖出, 卖出最早 T+1

用法:
  单月扫描(含明细): python strategy_badboard_recovery.py 2026-04
  区间统计(仅汇总):  python strategy_badboard_recovery.py 2021-01 2026-06 --summary
"""
import sys
import sqlite3

DB_PATH = '/home/AIWealth/data/stocks.db'

# 烂板阈值: 昨日 low < close * (1 - OPEN_DROP)  => 盘中打开超过 3%
BADBOARD_DROP = 0.03
MAX_DETAIL_PER_DAY = 5   # 每天最多打印明细的候选股数


# ----------------------- 涨跌停判定 -----------------------
def get_limit_ratio(code):
    if code.startswith('sz.300') or code.startswith('sz.301'):
        return 0.20  # 创业板
    if code.startswith('sh.688'):
        return 0.20  # 科创板
    if code.startswith('bj.'):
        return 0.30  # 北交所
    return 0.10      # 主板


def calc_limit_up(preclose, code):
    return round(preclose * (1 + get_limit_ratio(code)), 2)


def is_limit_up(close, preclose, code):
    if preclose is None or preclose <= 0 or close is None:
        return False
    return close >= calc_limit_up(preclose, code)


# ----------------------- 交易日工具 -----------------------
def get_all_trading_days(cur):
    cur.execute("SELECT DISTINCT date FROM stock_kline ORDER BY date")
    return [r[0] for r in cur.fetchall()]


def get_month_trading_days(cur, month_str):
    cur.execute(
        "SELECT DISTINCT date FROM stock_kline WHERE substr(date,1,7)=? ORDER BY date",
        (month_str,))
    return [r[0] for r in cur.fetchall()]


def get_range_trading_days(cur, start_month, end_month):
    cur.execute(
        "SELECT DISTINCT date FROM stock_kline "
        "WHERE substr(date,1,7)>=? AND substr(date,1,7)<=? ORDER BY date",
        (start_month, end_month))
    return [r[0] for r in cur.fetchall()]


# ----------------------- 候选股筛选 -----------------------
def find_candidates(cur, today, yesterday):
    """按策略定义筛选 T 日候选股。"""
    # 昨日全量(用于判定涨停 + 烂板)
    cur.execute("""
        SELECT code, code_name, preclose, close, high, low, isST
        FROM stock_kline WHERE date = ? AND preclose > 0
    """, (yesterday,))
    yd_map = {r[0]: r for r in cur.fetchall()}

    # 今日全量
    cur.execute("""
        SELECT code, code_name, preclose, open, high, low, close, isST,
               hour1_open, hour1_high, hour1_low, hour1_close
        FROM stock_kline WHERE date = ? AND preclose > 0
    """, (today,))
    td_rows = cur.fetchall()

    candidates = []
    for tr in td_rows:
        code = tr[0]
        yd = yd_map.get(code)
        if yd is None:
            continue
        _, yd_name, yd_pre, yd_close, yd_high, yd_low, yd_st = yd

        # 排除 ST
        if (tr[7] or yd_st):
            continue
        name = tr[1] or yd_name or ''
        if 'ST' in name.upper():
            continue

        # 1) 昨日涨停
        if not is_limit_up(yd_close, yd_pre, code):
            continue
        # 2) 昨日烂板: 盘中打开超过 BADBOARD_DROP
        if yd_low is None or yd_close is None or yd_low >= yd_close * (1 - BADBOARD_DROP):
            continue

        # 3) 今日低开
        t_open = tr[3]
        if t_open is None or t_open <= 0:
            continue
        if not (t_open < yd_close):
            continue

        t_pre, t_high, t_low, t_close = tr[2], tr[4], tr[5], tr[6]
        h1_open, h1_high, h1_low, h1_close = tr[8], tr[9], tr[10], tr[11]

        open_rate = (t_open - t_pre) / t_pre * 100 if t_pre else 0.0
        # 昨日盘中最大回撤幅度(打开深度)
        yd_dip = (yd_low - yd_close) / yd_close * 100 if yd_close else 0.0

        # hour1 企稳判定(收盘 >= 开盘)
        h1_stable = (h1_open is not None and h1_close is not None
                     and h1_open > 0 and h1_close >= h1_open)
        h1_pct = (h1_close - h1_open) / h1_open * 100 if (h1_open and h1_open > 0
                                                          and h1_close is not None) else None

        candidates.append({
            'code': code, 'name': name, 'today': today,
            'yd_close': yd_close, 'yd_low': yd_low, 'yd_dip': yd_dip,
            't_open': t_open, 't_high': t_high, 't_low': t_low, 't_close': t_close,
            'open_rate': open_rate,
            'h1_open': h1_open, 'h1_close': h1_close, 'h1_low': h1_low,
            'h1_stable': h1_stable, 'h1_pct': h1_pct,
            'h2_open': None,  # 延后填充
        })

    candidates.sort(key=lambda x: x['open_rate'])  # 低开越深越靠前
    return candidates


# ----------------------- hour 明细 & rate 表 -----------------------
def fetch_window(cur, code, days):
    if not days:
        return {}
    ph = ','.join(['?'] * len(days))
    cur.execute(f"""
        SELECT date, open, high, low, close, preclose, turn,
               hour1_open, hour1_high, hour1_low, hour1_close,
               hour2_open, hour2_high, hour2_low, hour2_close,
               hour3_open, hour3_high, hour3_low, hour3_close,
               hour4_open, hour4_high, hour4_low, hour4_close
        FROM stock_kline WHERE code = ? AND date IN ({ph}) ORDER BY date
    """, [code] + days)
    return {r[0]: r for r in cur.fetchall()}


def rate(v, base):
    if v is None or base is None or base <= 0:
        return None
    return (v - base) / base * 100


def fmt_rate(v):
    return f"{v:+6.2f}" if v is not None else "   -  "


def print_hour_detail(win_map, window_days, t_day, base_price):
    """打印 T-5~T+5 每小时 OCHL, 以相对 T日open 的 rate(%) 表示。"""
    print(f"    {'日期':<11}{'标':<3}{'H':<3}| {'O%':>7} {'H%':>7} {'L%':>7} {'C%':>7} | turn")
    print(f"    {'-'*11}---{'-'*3}+{'-'*33}+------")
    for d in window_days:
        row = win_map.get(d)
        if row is None:
            continue
        tag = '★T' if d == t_day else ''
        offset = window_days.index(d) - window_days.index(t_day) if t_day in window_days else 0
        turn = row[6]
        hours = [
            ('h1', row[7], row[8], row[9], row[10]),
            ('h2', row[11], row[12], row[13], row[14]),
            ('h3', row[15], row[16], row[17], row[18]),
            ('h4', row[19], row[20], row[21], row[22]),
        ]
        for i, (hn, o, h, l, c) in enumerate(hours):
            label = tag if i == 0 else ''
            offlabel = (f"{offset:+d}" if i == 0 and t_day in window_days else '')
            turnstr = f"{turn:.1f}%" if (i == 0 and turn is not None) else ''
            print(f"    {d:<11}{label:<3}{hn:<3}| "
                  f"{fmt_rate(rate(o, base_price))} {fmt_rate(rate(h, base_price))} "
                  f"{fmt_rate(rate(l, base_price))} {fmt_rate(rate(c, base_price))} | "
                  f"{offlabel} {turnstr}")


# ----------------------- 收益统计(多买卖配置) -----------------------
# 买入配置: (名称, 需要企稳确认, 取价字段) —— 均满足 T+1 合规
BUY_CONFIGS = [
    ('T_open',   False, 'open'),          # 低开直接买(T日open)
    ('T_h2open', True,  'hour2_open'),    # hour1企稳后 hour2_open 买入
]
# 卖出配置: (名称, 未来第几日, 取价字段) —— 全部 >= T+1
SELL_CONFIGS = [
    ('T1_open',  1, 'open'),
    ('T1_close', 1, 'close'),
    ('T2_open',  2, 'open'),
    ('T2_close', 2, 'close'),
    ('T3_close', 3, 'close'),
    ('T5_close', 5, 'close'),
]

FIELD_IDX = {
    'open': 1, 'high': 2, 'low': 3, 'close': 4,
    'hour2_open': 11,
}


def get_buy_price(row, price_field):
    return row[FIELD_IDX[price_field]] if row else None


def get_sell_price(row, price_field):
    return row[FIELD_IDX[price_field]] if row else None


def accumulate_stats(cur, cand, all_days, stats, only_buys=None):
    """对单只候选股, 逐买卖配置累加收益, 写入 stats(dict)。
    only_buys: 若指定(集合), 仅统计其中的买入配置名。"""
    code, t_day = cand['code'], cand['today']
    try:
        idx = all_days.index(t_day)
    except ValueError:
        return
    fut_days = all_days[idx: idx + 6]  # T..T+5
    win = fetch_window(cur, code, fut_days)
    t_row = win.get(t_day)
    if t_row is None:
        return

    for bname, need_stable, bfield in BUY_CONFIGS:
        if only_buys is not None and bname not in only_buys:
            continue
        if need_stable and not cand['h1_stable']:
            continue
        buy_price = get_buy_price(t_row, bfield)
        if buy_price is None or buy_price <= 0:
            continue
        for sname, sday_off, sfield in SELL_CONFIGS:
            if idx + sday_off >= len(all_days):
                continue
            s_day = all_days[idx + sday_off]
            s_row = win.get(s_day)
            if s_row is None:
                continue
            sell_price = get_sell_price(s_row, sfield)
            if sell_price is None or sell_price <= 0:
                continue
            ret = (sell_price - buy_price) / buy_price * 100
            key = (bname, sname)
            stats.setdefault(key, []).append(ret)


def print_stats(stats, title):
    print(f"\n{'='*78}")
    print(f"{title}")
    print(f"{'='*78}")
    print(f"{'买入':<10}{'卖出':<10}{'样本':>6}{'胜率%':>9}{'均收益%':>10}{'中位%':>9}{'月化估算%':>11}")
    print(f"{'-'*78}")
    rows = []
    for (bname, sname), rets in stats.items():
        if not rets:
            continue
        n = len(rets)
        wr = sum(1 for r in rets if r > 0) / n * 100
        avg = sum(rets) / n
        med = sorted(rets)[n // 2]
        rows.append((bname, sname, n, wr, avg, med))
    # 按均收益排序
    rows.sort(key=lambda x: x[4], reverse=True)
    for bname, sname, n, wr, avg, med in rows:
        # 月化估算: 假设每月约20交易日, 单笔平均持有天数按卖出配置粗估
        print(f"{bname:<10}{sname:<10}{n:>6}{wr:>9.1f}{avg:>10.2f}{med:>9.2f}"
              f"{avg*(20/ max(1,int(sname[1]) if sname[1].isdigit() else 1)):>11.2f}")


# ----------------------- 主流程 -----------------------
def run_scan(month_str, detail=True):
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    all_days = get_all_trading_days(cur)
    month_days = get_month_trading_days(cur, month_str)
    if not month_days:
        print(f"错误: 未找到 {month_str} 的交易日数据")
        conn.close()
        return

    print(f"{'='*78}")
    print(f"烂板次日低开企稳策略 - 候选股研究  月份={month_str}")
    print(f"定义: 昨日涨停 + 昨日烂板(low<close*{1-BADBOARD_DROP:.2f}) + 今日低开(open<昨close)")
    print(f"{'='*78}")
    print(f"该月交易日数: {len(month_days)}  数据库总交易日: {len(all_days)}\n")

    stats = {}
    stable_stats = {}
    total_cand = 0
    daily_summary = []

    for t_day in month_days:
        idx = all_days.index(t_day)
        if idx < 1:
            continue
        yesterday = all_days[idx - 1]
        cands = find_candidates(cur, t_day, yesterday)
        total_cand += len(cands)
        daily_summary.append((t_day, len(cands)))

        if detail:
            print(f"\n{'='*60}")
            print(f"===== {t_day}  候选股 {len(cands)} 只 =====")
            print(f"{'='*60}")
            if cands:
                print(f"  {'代码':<11}{'名称':<9}{'低开%':>7}{'昨打开%':>8}"
                      f"{'H1':>5}{'H1涨%':>7}")
                for c in cands:
                    st = '企稳' if c['h1_stable'] else '下杀'
                    h1p = f"{c['h1_pct']:+.1f}" if c['h1_pct'] is not None else '-'
                    print(f"  {c['code']:<11}{c['name']:<9}{c['open_rate']:>7.2f}"
                          f"{c['yd_dip']:>8.2f}{st:>5}{h1p:>7}")

        # 明细 + 统计
        shown = 0
        for c in cands:
            accumulate_stats(cur, c, all_days, stats)
            if c['h1_stable']:
                # 合规: 企稳由hour1收盘确定, 只能于hour2_open买入(T_h2open)
                accumulate_stats(cur, c, all_days, stable_stats, only_buys={'T_h2open'})

            if detail and shown < MAX_DETAIL_PER_DAY:
                shown += 1
                prev5 = max(0, idx - 5)
                next5 = min(len(all_days), idx + 6)
                window_days = all_days[prev5:next5]
                win_map = fetch_window(cur, c['code'], window_days)
                print(f"\n  --- {c['code']} ({c['name']}) 低开{c['open_rate']:+.2f}% "
                      f"昨打开{c['yd_dip']:+.2f}% hour1={'企稳' if c['h1_stable'] else '下杀'} ---")
                print(f"  (rate 相对 T日open={c['t_open']:.2f})")
                print_hour_detail(win_map, window_days, t_day, c['t_open'])

    # 月度候选概览
    print(f"\n\n{'='*78}")
    print(f"月度候选概览  总候选 {total_cand} 只, 日均 {total_cand/max(1,len(month_days)):.1f}")
    print(f"{'='*78}")
    for d, n in daily_summary:
        print(f"  {d}  候选 {n}")

    print(f"\n【合规说明】'企稳'由hour1收盘(10:00)确定, 属未来信息, 不可在T日open(9:25)买入;")
    print(f"           因此企稳策略只能于hour2_open买入(T_h2open), 下方仅列合规配置。")
    print_stats(stats, f"[{month_str}] 全部候选股(低开即入 T_open 合规) 各买卖配置收益统计")
    print_stats(stable_stats, f"[{month_str}] 仅hour1企稳→hour2_open买入(T_h2open, 合规) 收益统计")

    conn.close()
    print(f"\n研究完成: {month_str}")


def run_range(start_month, end_month):
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    all_days = get_all_trading_days(cur)
    days = get_range_trading_days(cur, start_month, end_month)
    if not days:
        print(f"错误: 区间 {start_month}~{end_month} 无数据")
        conn.close()
        return

    print(f"{'='*78}")
    print(f"烂板次日低开企稳策略 - 区间验证  {start_month} ~ {end_month}")
    print(f"{'='*78}")
    print(f"区间交易日数: {len(days)}\n")

    stats = {}
    stable_stats = {}
    total_cand = 0
    # 按年统计企稳配置
    year_stable = {}

    for t_day in days:
        idx = all_days.index(t_day)
        if idx < 1:
            continue
        yesterday = all_days[idx - 1]
        cands = find_candidates(cur, t_day, yesterday)
        total_cand += len(cands)
        year = t_day[:4]
        for c in cands:
            accumulate_stats(cur, c, all_days, stats)
            if c['h1_stable']:
                accumulate_stats(cur, c, all_days, stable_stats, only_buys={'T_h2open'})
                year_stable.setdefault(year, {})
                accumulate_stats(cur, c, all_days, year_stable[year], only_buys={'T_h2open'})

    print(f"总候选股: {total_cand}\n")
    print(f"【合规说明】企稳策略只能于hour2_open买入(T_h2open); T_open仅用于全量低开候选。")
    print_stats(stats, f"[{start_month}~{end_month}] 全部候选股(低开即入 T_open) 收益统计")
    print_stats(stable_stats, f"[{start_month}~{end_month}] 仅hour1企稳→hour2_open(T_h2open, 合规) 收益统计")

    # 分年度(仅企稳 + 代表性卖出配置)
    print(f"\n{'='*78}")
    print(f"分年度稳定性 (仅hour1企稳)")
    print(f"{'='*78}")
    for year in sorted(year_stable.keys()):
        print(f"\n--- {year} ---")
        print_stats(year_stable[year], f"{year} 企稳候选")

    conn.close()
    print(f"\n区间验证完成: {start_month}~{end_month}")


def main():
    if len(sys.argv) < 2:
        print("用法:")
        print("  单月扫描(含明细): python strategy_badboard_recovery.py 2026-04")
        print("  区间统计:         python strategy_badboard_recovery.py 2021-01 2026-06 --summary")
        sys.exit(1)

    if '--summary' in sys.argv:
        args = [a for a in sys.argv[1:] if a != '--summary']
        if len(args) >= 2:
            run_range(args[0], args[1])
        else:
            run_range(args[0], args[0])
    else:
        run_scan(sys.argv[1], detail=True)


if __name__ == '__main__':
    main()
