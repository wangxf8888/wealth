#!/usr/bin/env python3
"""
缩量横盘后放量突破策略 - 候选股研究脚本 (Task #98)

策略思路：
  股票连续多日(>=5日)在窄幅区间内横盘震荡(箱体整理)，同时成交量逐步萎缩(缩量表示观望)。
  某天突然放量突破箱体上沿——经典的"蓄力突破"信号，表明多空平衡被打破，趋势可能启动。

信号构成(全部使用T日及之前的完整日线数据，T日收盘后形成信号)：
  1. 前5日(T-5~T-1)每日振幅 (high-low)/close < 3% —— 窄幅横盘
  2. 前5日amount逐步递减 或 5日均amount < 10日均amount —— 缩量蓄力
  3. T日放量: amount >= 前5日均amount * 2
  4. T日突破: close > max(前5日close) —— 突破箱体上沿
  5. T日收阳: close > open
  6. 创业板(sz.300/sz.301): 市值 50-300亿 (mcap = amount/(turn/100)/1e8)
  7. 排除ST、排除次新(上市不足60交易日)、排除涨停(close>=round(preclose*1.20,2)无法追)

T+0/T+1合规：
  - 信号在T日收盘后确认，买入最早在 T+1 hour1_open。
  - "T日尾盘追"仅作参考统计(T日hour4_open买入)，因T+0当天不可卖，持有至T+1及以后。

用法:
  python strategy_consolidation_breakout.py 2026-04        # 单月研究
  python strategy_consolidation_breakout.py full           # 2021-01 ~ 2026-06 全周期
  python strategy_consolidation_breakout.py 2021-01 2026-06  # 自定义区间
"""
import sys
import sqlite3
from collections import defaultdict

# ==================== 配置区 ====================
DB_PATH = '/home/AIWealth/data/stocks.db'

CONSOLID_DAYS = 5           # 横盘观察天数
AMPLITUDE_MAX = 0.03        # 前N日每日振幅上限 (high-low)/close
VOLUME_SURGE = 2.0          # T日放量倍数: amount >= 前N日均amount * 倍数
MCAP_MIN = 50.0             # 流通市值下限(亿)
MCAP_MAX = 300.0            # 流通市值上限(亿)
MIN_LISTING_DAYS = 60       # 最少上市交易日(排除次新)
LIMIT_UP_RATIO = 0.20       # 创业板涨停比例

MAX_DETAIL_PER_DAY = 5      # 每天最多打印明细的候选股数
PRINT_DETAIL = True         # 是否打印hour级明细(全周期模式建议关闭)
# ================================================


def round2(x):
    return round(x, 2)


def is_limit_up(close, preclose):
    """创业板涨停判定: close >= round(preclose*1.20, 2)"""
    if preclose is None or preclose <= 0 or close is None:
        return False
    return close >= round(preclose * (1 + LIMIT_UP_RATIO), 2)


def get_trading_days_range(cur, start_month=None, end_month=None):
    if start_month and end_month:
        cur.execute("""
            SELECT DISTINCT date FROM stock_kline
            WHERE date >= ? AND date <= ? ORDER BY date
        """, (start_month + '-01', end_month + '-31'))
    elif start_month:
        cur.execute("""
            SELECT DISTINCT date FROM stock_kline
            WHERE date LIKE ? ORDER BY date
        """, (start_month + '%',))
    else:
        cur.execute("SELECT DISTINCT date FROM stock_kline ORDER BY date")
    return [r[0] for r in cur.fetchall()]


def get_all_trading_days(cur):
    cur.execute("SELECT DISTINCT date FROM stock_kline ORDER BY date")
    return [r[0] for r in cur.fetchall()]


def find_candidates(cur, today, prev_days, listing_ref_day):
    """
    筛选T日缩量横盘放量突破候选股。
    prev_days: 前5日日期列表(T-5~T-1)
    listing_ref_day: T-MIN_LISTING_DAYS日期，用于排除次新
    """
    # T日全量数据(创业板)
    cur.execute("""
        SELECT code, code_name, open, high, low, close, preclose, amount, turn, isST
        FROM stock_kline
        WHERE date = ? AND (code LIKE 'sz.300%' OR code LIKE 'sz.301%')
              AND isST = 0 AND preclose > 0 AND amount > 0 AND turn > 0
    """, (today,))
    today_rows = cur.fetchall()
    if not today_rows:
        return []

    # 前5日 + 前10日 amount / high / low / close（批量）
    prev10_days = prev_days  # 调用方已传入所需窗口
    placeholders = ','.join(['?'] * len(prev10_days))
    cur.execute(f"""
        SELECT code, date, high, low, close, amount
        FROM stock_kline
        WHERE date IN ({placeholders})
              AND (code LIKE 'sz.300%' OR code LIKE 'sz.301%')
    """, prev10_days)
    hist = defaultdict(list)
    for code, date, high, low, close, amount in cur.fetchall():
        hist[code].append((date, high, low, close, amount))

    # 次新排除：listing_ref_day当日存在记录的股票视为已上市>=MIN_LISTING_DAYS
    listed_codes = set()
    if listing_ref_day:
        cur.execute("""
            SELECT code FROM stock_kline WHERE date = ?
                  AND (code LIKE 'sz.300%' OR code LIKE 'sz.301%')
        """, (listing_ref_day,))
        listed_codes = {r[0] for r in cur.fetchall()}

    candidates = []
    for row in today_rows:
        (code, code_name, t_open, t_high, t_low, t_close,
         t_preclose, t_amount, t_turn, t_isST) = row

        if code_name and 'ST' in code_name.upper():
            continue
        # 排除次新
        if listed_codes and code not in listed_codes:
            continue
        # 排除涨停(无法追)
        if is_limit_up(t_close, t_preclose):
            continue
        # T日收阳
        if t_open is None or t_close <= t_open:
            continue

        # 市值 50-300亿
        mcap = t_amount / (t_turn / 100.0) / 1e8
        if mcap < MCAP_MIN or mcap > MCAP_MAX:
            continue

        # 历史数据
        recs = hist.get(code, [])
        recs.sort(key=lambda x: x[0])
        # 拆分前5日 与 前10日
        prev5 = [r for r in recs if r[0] in prev_days[-CONSOLID_DAYS:]]
        prev10 = recs  # 全窗口(最多10日)
        if len(prev5) < CONSOLID_DAYS:
            continue

        # 条件1：前5日每日振幅 < 3%
        ok_amp = True
        for _, h, l, c, a in prev5:
            if c is None or c <= 0 or h is None or l is None:
                ok_amp = False
                break
            if (h - l) / c >= AMPLITUDE_MAX:
                ok_amp = False
                break
        if not ok_amp:
            continue

        prev5_closes = [r[3] for r in prev5]
        prev5_amounts = [r[4] for r in prev5]
        if any(a is None or a <= 0 for a in prev5_amounts):
            continue
        avg5_amount = sum(prev5_amounts) / len(prev5_amounts)

        # 条件2：缩量 —— 前5日amount整体递减 或 5日均量 < 10日均量
        amount10 = [r[4] for r in prev10 if r[4] is not None and r[4] > 0]
        avg10_amount = sum(amount10) / len(amount10) if amount10 else avg5_amount
        # 整体递减: 线性回归斜率<0 用首尾均值近似
        first_half = prev5_amounts[:len(prev5_amounts)//2] or prev5_amounts[:1]
        second_half = prev5_amounts[len(prev5_amounts)//2:]
        decreasing = (sum(second_half)/len(second_half)) < (sum(first_half)/len(first_half))
        shrink = decreasing or (avg5_amount < avg10_amount)
        if not shrink:
            continue

        # 条件3：T日放量
        if t_amount < avg5_amount * VOLUME_SURGE:
            continue
        vol_ratio = t_amount / avg5_amount

        # 条件4：T日突破前5日最高close
        if t_close <= max(prev5_closes):
            continue

        box_high = max(prev5_closes)
        box_low = min(prev5_closes)
        breakout_pct = (t_close - box_high) / box_high * 100
        t_close_rate = (t_close - t_preclose) / t_preclose * 100

        candidates.append({
            'code': code,
            'code_name': code_name or 'N/A',
            'today': today,
            't_open': t_open,
            't_close': t_close,
            't_high': t_high,
            't_low': t_low,
            't_preclose': t_preclose,
            't_close_rate': t_close_rate,
            'mcap': mcap,
            'avg5_amount': avg5_amount,
            'avg10_amount': avg10_amount,
            'vol_ratio': vol_ratio,
            'box_high': box_high,
            'box_low': box_low,
            'breakout_pct': breakout_pct,
            'prev5_amounts': prev5_amounts,
            'shrink_mode': 'decr' if decreasing else 'ma5<ma10',
        })

    candidates.sort(key=lambda x: x['vol_ratio'], reverse=True)
    return candidates


def get_row(cur, code, date):
    cur.execute("""
        SELECT date, open, high, low, close, preclose,
               hour1_open, hour1_high, hour1_low, hour1_close,
               hour2_open, hour2_high, hour2_low, hour2_close,
               hour3_open, hour3_high, hour3_low, hour3_close,
               hour4_open, hour4_high, hour4_low, hour4_close, turn
        FROM stock_kline WHERE code = ? AND date = ?
    """, (code, date))
    return cur.fetchone()


def get_window_rows(cur, code, days_list):
    if not days_list:
        return []
    placeholders = ','.join(['?'] * len(days_list))
    cur.execute(f"""
        SELECT date, open, close, turn,
               hour1_open, hour1_high, hour1_low, hour1_close,
               hour2_open, hour2_high, hour2_low, hour2_close,
               hour3_open, hour3_high, hour3_low, hour3_close,
               hour4_open, hour4_high, hour4_low, hour4_close
        FROM stock_kline WHERE code = ? AND date IN ({placeholders})
        ORDER BY date
    """, [code] + days_list)
    return cur.fetchall()


def format_hour_table(rows, ref_close, signal_date):
    """hour级明细，用相对T日close的涨跌幅率表示"""
    lines = []
    lines.append(f"  {'日期':<12}|{'hour':<5}|{'涨跌幅率%(vs T日close)':<22}| turn")
    lines.append(f"  {'-'*12}+{'-'*5}+{'-'*22}+{'-'*6}")
    for row in rows:
        date = row[0]
        day_turn = row[3]
        hours = [
            ('h1', row[4], row[5], row[6], row[7]),
            ('h2', row[8], row[9], row[10], row[11]),
            ('h3', row[12], row[13], row[14], row[15]),
            ('h4', row[16], row[17], row[18], row[19]),
        ]
        for hn, ho, hh, hl, hc in hours:
            if ho is None or hc is None:
                continue
            o_r = (ho - ref_close) / ref_close * 100
            h_r = (hh - ref_close) / ref_close * 100 if hh else 0
            l_r = (hl - ref_close) / ref_close * 100 if hl else 0
            c_r = (hc - ref_close) / ref_close * 100
            turn_str = f"{day_turn:.1f}%" if day_turn and hn == 'h1' else ""
            marker = " ← 突破日T" if date == signal_date and hn == 'h1' else ""
            cell = f"O{o_r:+.1f} H{h_r:+.1f} L{l_r:+.1f} C{c_r:+.1f}"
            lines.append(f"  {date:<12}|{hn:<5}|{cell:<22}| {turn_str}{marker}")
    return '\n'.join(lines)


def eval_holding(cur, code, buy_date_idx, all_days, buy_price, hold_days, tp=None, sl=None):
    """
    从buy_date(含)起持有hold_days个交易日。
    tp: 止盈百分比(如8表示+8%)，触及则以tp价卖出。
    sl: 止损百分比(如-5表示-5%)，触及则以sl价卖出。
    返回收益率%(相对buy_price)。同日先判止损(保守)。
    """
    if buy_price is None or buy_price <= 0:
        return None
    end_idx = buy_date_idx + hold_days
    hold_dates = all_days[buy_date_idx:end_idx]
    if not hold_dates:
        return None
    placeholders = ','.join(['?'] * len(hold_dates))
    cur.execute(f"""
        SELECT date, open, high, low, close FROM stock_kline
        WHERE code = ? AND date IN ({placeholders}) ORDER BY date
    """, [code] + hold_dates)
    rows = cur.fetchall()
    if not rows:
        return None

    tp_price = buy_price * (1 + tp / 100.0) if tp is not None else None
    sl_price = buy_price * (1 + sl / 100.0) if sl is not None else None

    for i, (d, o, h, l, c) in enumerate(rows):
        if None in (o, h, l, c):
            continue
        # 买入当日(i==0)从买入价起算，用当日high/low判触发
        if sl_price is not None and l <= sl_price:
            return (sl_price - buy_price) / buy_price * 100
        if tp_price is not None and h >= tp_price:
            return (tp_price - buy_price) / buy_price * 100
    # 未触发，末日收盘卖出
    last_close = rows[-1][4]
    if last_close is None:
        return None
    return (last_close - buy_price) / buy_price * 100


def summarize(name, rets):
    valid = [r for r in rets if r is not None]
    if not valid:
        return f"  {name:<28}: 无有效样本"
    n = len(valid)
    avg = sum(valid) / n
    win = sum(1 for r in valid if r > 0) / n * 100
    return f"  {name:<28}: n={n:<4} 平均={avg:+6.2f}% 胜率={win:5.1f}% 月化~{avg*n/max(1,n):+.2f}%(单笔)"


def main():
    args = sys.argv[1:]
    if not args:
        print("用法: python strategy_consolidation_breakout.py 2026-04 | full | 2021-01 2026-06")
        sys.exit(1)

    global PRINT_DETAIL
    if args[0] == 'full':
        start_month, end_month = '2021-01', '2026-06'
        PRINT_DETAIL = False
    elif len(args) == 2:
        start_month, end_month = args[0], args[1]
        PRINT_DETAIL = False
    else:
        start_month = end_month = args[0]

    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()

    all_days = get_all_trading_days(cur)
    if start_month == end_month:
        scan_days = get_trading_days_range(cur, start_month)
    else:
        scan_days = get_trading_days_range(cur, start_month, end_month)

    if not scan_days:
        print(f"错误: 未找到 {start_month}~{end_month} 的交易日数据")
        conn.close()
        sys.exit(1)

    print("=" * 90)
    print("缩量横盘后放量突破策略 - 候选股研究 (Task #98)")
    print(f"研究区间: {start_month} ~ {end_month}  扫描交易日: {len(scan_days)}")
    print(f"信号: 前{CONSOLID_DAYS}日振幅<{AMPLITUDE_MAX*100:.0f}% + 缩量 + T日放量>={VOLUME_SURGE}x"
          f" + 突破箱体 + 收阳 + 创业板{MCAP_MIN:.0f}-{MCAP_MAX:.0f}亿")
    print("=" * 90)

    total_cand = 0
    # 买入策略收益容器
    stats = {
        't1h1_hold1': [], 't1h1_hold2': [], 't1h1_hold3': [], 't1h1_hold5': [],
        'tail_hold1': [], 'tail_hold2': [], 'tail_hold3': [],
    }
    # T+1高开分组(基于T+1 hour1_open相对T日close)
    gap_groups = {'低开<0': [], '平开0-2%': [], '高开2-5%': [], '高开>5%': []}
    # TP/SL网格(T+1 hour1买入, 最长持有5日)
    grid_tp = [6, 8, 10, 15]
    grid_sl = [-3, -5, -7]
    grid_stats = {(tp, sl): [] for tp in grid_tp for sl in grid_sl}

    for today in scan_days:
        try:
            idx = all_days.index(today)
        except ValueError:
            continue
        if idx < 11 or idx + 6 >= len(all_days):
            continue
        prev_window = all_days[idx - 10:idx]  # 前10日窗口(含前5日)
        listing_ref = all_days[idx - MIN_LISTING_DAYS] if idx >= MIN_LISTING_DAYS else None

        candidates = find_candidates(cur, today, prev_window, listing_ref)
        total_cand += len(candidates)

        if PRINT_DETAIL:
            print(f"\n{'='*70}")
            print(f"===== {today}  候选股: {len(candidates)}只 =====")
            print(f"{'='*70}")

        if not candidates:
            continue

        # 打印明细
        if PRINT_DETAIL:
            for c in candidates[:MAX_DETAIL_PER_DAY]:
                print(f"\n--- {c['code']} ({c['code_name']}) 市值{c['mcap']:.0f}亿 ---")
                amt_str = ', '.join(f"{a/1e8:.2f}" for a in c['prev5_amounts'])
                print(f"  前5日amount(亿): [{amt_str}] 均值={c['avg5_amount']/1e8:.2f}亿 "
                      f"(缩量模式:{c['shrink_mode']})")
                print(f"  T日放量倍数: {c['vol_ratio']:.1f}x  箱体[{c['box_low']:.2f}~{c['box_high']:.2f}]"
                      f"  突破幅度:+{c['breakout_pct']:.2f}%")
                print(f"  T日: open={c['t_open']:.2f} close={c['t_close']:.2f} "
                      f"涨幅{c['t_close_rate']:+.1f}%")
                win_days = all_days[max(0, idx - 5):min(len(all_days), idx + 6)]
                rows = get_window_rows(cur, c['code'], win_days)
                if rows:
                    print("  T-5~T+5 hour级明细:")
                    print(format_hour_table(rows, c['t_close'], today))

        # 统计（所有候选股）
        t1_idx = idx + 1
        t1_date = all_days[t1_idx]
        for c in candidates:
            code = c['code']
            # T+1 hour1 open 买入
            t1_row = get_row(cur, code, t1_date)
            if t1_row:
                t1_h1_open = t1_row[6]
                if t1_h1_open and t1_h1_open > 0:
                    stats['t1h1_hold1'].append(eval_holding(cur, code, t1_idx, all_days, t1_h1_open, 1))
                    stats['t1h1_hold2'].append(eval_holding(cur, code, t1_idx, all_days, t1_h1_open, 2))
                    stats['t1h1_hold3'].append(eval_holding(cur, code, t1_idx, all_days, t1_h1_open, 3))
                    stats['t1h1_hold5'].append(eval_holding(cur, code, t1_idx, all_days, t1_h1_open, 5))
                    # 高开分组(vs T日close)
                    gap = (t1_h1_open - c['t_close']) / c['t_close'] * 100
                    r2 = eval_holding(cur, code, t1_idx, all_days, t1_h1_open, 2)
                    if gap < 0:
                        gap_groups['低开<0'].append(r2)
                    elif gap < 2:
                        gap_groups['平开0-2%'].append(r2)
                    elif gap < 5:
                        gap_groups['高开2-5%'].append(r2)
                    else:
                        gap_groups['高开>5%'].append(r2)
                    # TP/SL网格(最长持有5日)
                    for (tp, sl) in grid_stats:
                        grid_stats[(tp, sl)].append(
                            eval_holding(cur, code, t1_idx, all_days, t1_h1_open, 5, tp=tp, sl=sl))

            # T日尾盘追(hour4_open买入, 持有至T+1/2/3)
            t_row = get_row(cur, code, today)
            if t_row:
                tail_open = t_row[18]  # hour4_open
                if tail_open and tail_open > 0:
                    stats['tail_hold1'].append(eval_holding(cur, code, idx, all_days, tail_open, 2))
                    stats['tail_hold2'].append(eval_holding(cur, code, idx, all_days, tail_open, 3))
                    stats['tail_hold3'].append(eval_holding(cur, code, idx, all_days, tail_open, 4))

    # ================= 汇总 =================
    print(f"\n\n{'='*90}")
    print(f"{'='*20} 汇总统计 (区间 {start_month}~{end_month}) {'='*20}")
    print(f"{'='*90}")
    print(f"总候选股(信号数): {total_cand}只")

    print(f"\n【方案A】T+1 hour1开盘买入(确认突破), 持有N日收盘卖出:")
    print(summarize('T+1买入·持有1日', stats['t1h1_hold1']))
    print(summarize('T+1买入·持有2日', stats['t1h1_hold2']))
    print(summarize('T+1买入·持有3日', stats['t1h1_hold3']))
    print(summarize('T+1买入·持有5日', stats['t1h1_hold5']))

    print(f"\n【方案B】T日尾盘(hour4)追入, 持有N日收盘卖出:")
    print(summarize('尾盘追·持有~T+1', stats['tail_hold1']))
    print(summarize('尾盘追·持有~T+2', stats['tail_hold2']))
    print(summarize('尾盘追·持有~T+3', stats['tail_hold3']))

    print(f"\n【T+1高开幅度分组】(T+1 hour1_open vs T日close, 持有2日):")
    for g, rets in gap_groups.items():
        print(summarize(g, rets))

    print(f"\n【止盈止损网格】T+1 hour1买入, 最长持有5日:")
    print(f"  {'TP/SL':<8}", end='')
    for sl in grid_sl:
        print(f"{sl:>3}%          ", end='')
    print()
    best = None
    for tp in grid_tp:
        print(f"  +{tp:<6}%", end='')
        for sl in grid_sl:
            rets = [r for r in grid_stats[(tp, sl)] if r is not None]
            if rets:
                avg = sum(rets) / len(rets)
                win = sum(1 for r in rets if r > 0) / len(rets) * 100
                print(f" {avg:+5.2f}%/{win:4.1f}%", end='')
                if best is None or avg > best[0]:
                    best = (avg, win, tp, sl, len(rets))
            else:
                print(f" {'--':>11}", end='')
        print()

    print(f"\n{'='*90}")
    if best:
        print(f"网格最优: TP+{best[2]}% SL{best[3]}% -> 平均{best[0]:+.2f}%/笔 胜率{best[1]:.1f}% (n={best[4]})")
    # 月化估算(方案A持有2日为主)
    a2 = [r for r in stats['t1h1_hold2'] if r is not None]
    if a2:
        avg2 = sum(a2) / len(a2)
        win2 = sum(1 for r in a2 if r > 0) / len(a2) * 100
        # 持有2日 -> 约每月10次轮动
        print(f"方案A持有2日: 单笔平均{avg2:+.2f}% 胜率{win2:.1f}% -> 月化估算~{avg2*10:+.1f}%(月约10轮)")
    print("研究完成。")
    conn.close()


if __name__ == '__main__':
    main()
