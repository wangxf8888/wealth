#!/usr/bin/env python3
"""
Task #101: 低位首板(底部突破)次日策略 - 候选股研究脚本 (rule2 阶段一/二)

策略思路:
  股票处于近60日最低位置(底部20%区间), 突然首次涨停(首板)。
  这是"底部放量突破"的极端形式——大资金在低位强势入场的信号, 稀少但质量高。
  次日(T日)延续概率高(市场关注度从0暴增)。

候选定义(全部在 T-1 收盘后即可确定, T+0 合规):
  - T-1(昨日)首板涨停: round(close/preclose, 2) >= 涨停比
        主板 sh.60/sz.00 -> 1.10; 创业板 sz.30/科创板 sh.688 -> 1.20
  - T-1 之前5个交易日内无涨停(确认"首板"非连板)
  - T-1 close 处于近60个交易日的底部20%: position = (yd_close-min60)/(max60-min60) <= 0.20
  - 排除 ST
  - 排除 T-1 一字涨停: round(yd_open/yd_preclose,2) >= 涨停比 (次日大概率继续一字买不到)
  - 仅创业板(sz.3)或科创板(sh.688)(振幅空间大)

买入(T日, 首板次日):
  - 若 T 日开盘即涨停(round(T_open/T_preclose,2)>=涨停比)则无法买入 -> 跳过
  - 买入价候选: T_open / T_hour2_open(需hour1不涨停确认)

卖出(T+1 起, 遵守 T+1 不可当日卖):
  - 统计多种卖出时点: T+1_open / T+1_close / T+2_close / T+3_close

T+0 合规: 买入决策仅用 T-1 及之前历史 + T 日 open(9:25竞价确定)。
"""
import sys
import sqlite3

DB_PATH = '/home/AIWealth/data/stocks.db'

# ============ 可配置参数区 ============
BOTTOM_PCT = 0.05          # 底部区间阈值: 全周期验证显示 alpha 集中在绝对底部<=0.05(极端底部)
                           #   (0.20整体无alpha, 0.05-0.20区间胜率<50%; 仅<=0.05胜率>55%)
LOOKBACK_DAYS = 60         # 底部位置回看交易日数
FIRSTBOARD_CLEAN_DAYS = 5  # T-1 之前多少个交易日内无涨停(确认首板)
MAX_DETAIL_PER_DAY = 5     # 每日最多打印明细的候选股数
PRINT_DETAIL = True        # 是否打印hour级明细(全周期批量时可关)
# =====================================


def get_limit_ratio(code):
    """涨跌幅比例"""
    if code.startswith('sz.300') or code.startswith('sz.301'):
        return 0.20  # 创业板
    if code.startswith('sh.688'):
        return 0.20  # 科创板
    if code.startswith('bj.') or code.startswith('bj'):
        return 0.30  # 北交所
    return 0.10      # 主板


def is_target_board(code):
    """仅创业板或科创板"""
    return code.startswith('sz.300') or code.startswith('sz.301') or code.startswith('sh.688')


def is_limit_up(close, preclose, code):
    """涨停严格判定: round(close/preclose,2) >= 1+ratio"""
    if preclose is None or preclose <= 0 or close is None:
        return False
    thr = round(1 + get_limit_ratio(code), 2)
    return round(close / preclose, 2) >= thr


def is_yizi_limit_up(open_p, preclose, code):
    """一字涨停(开盘即涨停价): round(open/preclose,2) >= 1+ratio"""
    if preclose is None or preclose <= 0 or open_p is None:
        return False
    thr = round(1 + get_limit_ratio(code), 2)
    return round(open_p / preclose, 2) >= thr


def get_trading_days(cur, month_str):
    cur.execute("SELECT DISTINCT date FROM stock_kline WHERE date LIKE ? ORDER BY date",
                (month_str + '%',))
    return [r[0] for r in cur.fetchall()]


def get_all_trading_days(cur):
    cur.execute("SELECT DISTINCT date FROM stock_kline ORDER BY date")
    return [r[0] for r in cur.fetchall()]


def load_day(cur, date):
    """加载某交易日全市场基础数据, 返回 {code: row}"""
    cur.execute("""
        SELECT code, code_name, preclose, open, high, low, close, isST,
               hour1_open, hour1_close, hour2_open, hour2_close,
               hour3_open, hour3_close, hour4_open, hour4_close
        FROM stock_kline WHERE date = ?
    """, (date,))
    cols = ['code', 'code_name', 'preclose', 'open', 'high', 'low', 'close', 'isST',
            'h1_open', 'h1_close', 'h2_open', 'h2_close',
            'h3_open', 'h3_close', 'h4_open', 'h4_close']
    return {r[0]: dict(zip(cols, r)) for r in cur.fetchall()}


def get_close_series(cur, code, end_date, n):
    """获取 code 在 end_date(含) 及之前 n 个交易日的 close 序列"""
    cur.execute("""
        SELECT close FROM stock_kline
        WHERE code = ? AND date <= ? AND close IS NOT NULL
        ORDER BY date DESC LIMIT ?
    """, (code, end_date, n))
    return [r[0] for r in cur.fetchall()]


def had_limit_up_in_range(cur, code, start_date, end_date):
    """判断 code 在 (start_date, end_date] 区间内是否出现过涨停"""
    cur.execute("""
        SELECT close, preclose FROM stock_kline
        WHERE code = ? AND date > ? AND date <= ? AND preclose > 0
    """, (code, start_date, end_date))
    for close, preclose in cur.fetchall():
        if is_limit_up(close, preclose, code):
            return True
    return False


def find_candidates(cur, today, all_days):
    """找出 today(=T日, 首板次日) 的候选股"""
    idx = all_days.index(today)
    if idx < FIRSTBOARD_CLEAN_DAYS + 2:
        return []
    yesterday = all_days[idx - 1]                 # T-1 首板日
    daybefore = all_days[idx - 2]                 # T-2, 突破前收盘参照
    clean_start = all_days[idx - 1 - FIRSTBOARD_CLEAN_DAYS]  # 首板判定回看起点

    yd_map = load_day(cur, yesterday)
    today_map = load_day(cur, today)

    candidates = []
    for code, yd in yd_map.items():
        if not is_target_board(code):
            continue
        if yd['isST']:
            continue
        if yd['code_name'] and 'ST' in yd['code_name'].upper():
            continue
        yd_close, yd_pre, yd_open = yd['close'], yd['preclose'], yd['open']
        if yd_pre is None or yd_pre <= 0 or yd_close is None:
            continue

        # 1. T-1 首板涨停
        if not is_limit_up(yd_close, yd_pre, code):
            continue
        # 2. T-1 之前5日内无涨停(确认首板), 区间(clean_start, yesterday)不含yesterday
        cur.execute("""
            SELECT close, preclose FROM stock_kline
            WHERE code = ? AND date > ? AND date < ? AND preclose > 0
        """, (code, clean_start, yesterday))
        prior_limit = any(is_limit_up(c, p, code) for c, p in cur.fetchall())
        if prior_limit:
            continue  # 连板/近期有涨停, 非首板

        # 3. 排除 T-1 一字涨停
        if is_yizi_limit_up(yd_open, yd_pre, code):
            continue

        # 4. 底部位置: 用突破前价格(preclose=T-2收盘)在近60日中的相对位置
        #    首板当日close被+10~20%拉高, 用其算position天然偏高(最低约0.29),
        #    无法表达"低位盘整后突然涨停突破"本意, 故用突破前价格为基准。
        closes = get_close_series(cur, code, daybefore, LOOKBACK_DAYS)
        if len(closes) < 40:  # 数据不足(次新股等)跳过
            continue
        min60, max60 = min(closes), max(closes)
        if max60 <= min60:
            continue
        position = (yd_pre - min60) / (max60 - min60)
        if position > BOTTOM_PCT:
            continue

        # 5. T日必须有数据且可买入(开盘非涨停)
        if code not in today_map:
            continue
        t = today_map[code]
        t_open, t_pre, t_close, t_high, t_low = t['open'], t['preclose'], t['close'], t['high'], t['low']
        if t_pre is None or t_pre <= 0 or t_open is None or t_open <= 0:
            continue
        if t['isST']:
            continue
        # T日开盘即涨停 -> 无法买入, 跳过
        if is_yizi_limit_up(t_open, t_pre, code):
            continue

        open_rate = (t_open - t_pre) / t_pre * 100
        candidates.append({
            'code': code,
            'code_name': yd['code_name'] or '',
            'position': position,
            'yd_close': yd_close, 'yd_pre': yd_pre,
            'yd_pct': (yd_close - yd_pre) / yd_pre * 100,
            't': t,
            't_open': t_open, 't_close': t_close, 't_high': t_high, 't_low': t_low, 't_pre': t_pre,
            'open_rate': open_rate,
        })

    candidates.sort(key=lambda x: x['position'])  # 越低越优先
    return candidates


def get_window_hours(cur, code, days):
    if not days:
        return []
    ph = ','.join(['?'] * len(days))
    cur.execute(f"""
        SELECT date, open, close, preclose,
               hour1_open, hour1_high, hour1_low, hour1_close,
               hour2_open, hour2_high, hour2_low, hour2_close,
               hour3_open, hour3_high, hour3_low, hour3_close,
               hour4_open, hour4_high, hour4_low, hour4_close
        FROM stock_kline WHERE code = ? AND date IN ({ph}) ORDER BY date
    """, [code] + days)
    return cur.fetchall()


def format_hour_table(rows, buy_date, ref_open):
    """hour级明细, rate 相对 T日open"""
    lines = []
    lines.append(f"  {'日期':<12}| {'H':<3}| {'open':<8}| {'high':<8}| {'low':<8}| {'close':<8}| rate(vs T_open)")
    lines.append(f"  {'-'*12}+{'-'*4}+{'-'*9}+{'-'*9}+{'-'*9}+{'-'*9}+{'-'*16}")
    for row in rows:
        date = row[0]
        hs = [('h1', row[4], row[5], row[6], row[7]),
              ('h2', row[8], row[9], row[10], row[11]),
              ('h3', row[12], row[13], row[14], row[15]),
              ('h4', row[16], row[17], row[18], row[19])]
        for hn, ho, hh, hl, hc in hs:
            if ho is None or hc is None:
                continue
            rate = (hc - ref_open) / ref_open * 100 if ref_open else 0
            mark = "  <= T_open(买入基准)" if (date == buy_date and hn == 'h1') else ""
            lines.append(f"  {date:<12}| {hn:<3}| {ho:<8.2f}| {hh:<8.2f}| {hl:<8.2f}| {hc:<8.2f}| {rate:+.2f}%{mark}")
    return '\n'.join(lines)


def compute_returns(cur, code, today, all_days, buy_price):
    """给定买入价, 计算 T+1_open / T+1_close / T+2_close / T+3_close 的收益率(%)"""
    if buy_price is None or buy_price <= 0:
        return None
    idx = all_days.index(today)
    fut = all_days[idx + 1: idx + 4]  # T+1, T+2, T+3
    if not fut:
        return None
    ph = ','.join(['?'] * len(fut))
    cur.execute(f"""
        SELECT date, open, close FROM stock_kline
        WHERE code = ? AND date IN ({ph}) ORDER BY date
    """, [code] + fut)
    rows = {r[0]: (r[1], r[2]) for r in cur.fetchall()}
    res = {}
    if fut and fut[0] in rows and rows[fut[0]][0]:
        res['t1_open'] = (rows[fut[0]][0] - buy_price) / buy_price * 100
    if fut and fut[0] in rows and rows[fut[0]][1]:
        res['t1_close'] = (rows[fut[0]][1] - buy_price) / buy_price * 100
    if len(fut) >= 2 and fut[1] in rows and rows[fut[1]][1]:
        res['t2_close'] = (rows[fut[1]][1] - buy_price) / buy_price * 100
    if len(fut) >= 3 and fut[2] in rows and rows[fut[2]][1]:
        res['t3_close'] = (rows[fut[2]][1] - buy_price) / buy_price * 100
    return res


def agg_stats(values):
    """返回 (n, mean, median, winrate%)"""
    vals = [v for v in values if v is not None]
    if not vals:
        return (0, 0.0, 0.0, 0.0)
    n = len(vals)
    mean = sum(vals) / n
    s = sorted(vals)
    median = s[n // 2] if n % 2 else (s[n // 2 - 1] + s[n // 2]) / 2
    win = sum(1 for v in vals if v > 0) / n * 100
    return (n, mean, median, win)


def main():
    if len(sys.argv) < 2:
        print("用法: python strategy_lowpos_firstboard.py 2026-04 [YYYY | YYYY-MM ...]")
        print("      python strategy_lowpos_firstboard.py ALL   # 2021-2026全周期")
        sys.exit(1)

    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    all_days = get_all_trading_days(cur)

    # 解析要扫描的月份列表
    args = sys.argv[1:]
    months = []
    if args[0].upper() == 'ALL':
        global PRINT_DETAIL
        PRINT_DETAIL = False
        for y in range(2021, 2027):
            for m in range(1, 13):
                months.append(f"{y}-{m:02d}")
    else:
        for a in args:
            if len(a) == 4 and a.isdigit():  # 整年
                PRINT_DETAIL = False
                for m in range(1, 13):
                    months.append(f"{a}-{m:02d}")
            else:
                months.append(a)

    print("=" * 80)
    print("Task #101 低位首板(底部突破)次日策略 - 候选股研究")
    print(f"底部区间: 近{LOOKBACK_DAYS}日 position<= {BOTTOM_PCT}")
    print(f"首板确认: T-1之前{FIRSTBOARD_CLEAN_DAYS}日内无涨停 | 仅创业板/科创板")
    print(f"扫描范围: {months[0]} ~ {months[-1]} ({len(months)}个月)")
    print("=" * 80)

    # 收益容器: buy_point -> sell_point -> [returns]
    buy_points = ['open', 'h2']
    sell_points = ['t1_open', 't1_close', 't2_close', 't3_close']
    bucket = {bp: {sp: [] for sp in sell_points} for bp in buy_points}

    total_candidates = 0
    signal_dates = 0

    for month_str in months:
        month_days = get_trading_days(cur, month_str)
        if not month_days:
            continue
        for today in month_days:
            if today not in all_days:
                continue
            cands = find_candidates(cur, today, all_days)
            if not cands:
                continue
            total_candidates += len(cands)
            signal_dates += 1

            if PRINT_DETAIL:
                print(f"\n{'='*60}")
                print(f"===== {today}  候选数={len(cands)} =====")
                print(f"{'排名':<4}{'代码':<12}{'名称':<10}{'底部位置':<9}{'昨涨幅%':<9}{'T高开%':<8}")
                for i, c in enumerate(cands):
                    print(f"{i+1:<4}{c['code']:<12}{c['code_name']:<10}"
                          f"{c['position']*100:<8.1f}%{c['yd_pct']:<9.2f}{c['open_rate']:<8.2f}")

            shown = min(MAX_DETAIL_PER_DAY, len(cands)) if PRINT_DETAIL else 0
            for i, c in enumerate(cands):
                code = c['code']
                # ---- 买入价 ----
                bp_prices = {'open': c['t_open']}
                # hour2买入需 hour1 不涨停(用hour1_close判断趋势, 且hour1_open可交易)
                t = c['t']
                h1_close = t.get('h1_close')
                h2_open = t.get('h2_open')
                # hour1收阳且hour2可买入
                if h2_open and h2_open > 0 and t.get('h1_open') and h1_close and h1_close >= t['h1_open']:
                    bp_prices['h2'] = h2_open

                # ---- 明细打印 ----
                if PRINT_DETAIL and i < shown:
                    print(f"\n--- {code} ({c['code_name']}) 底部位置={c['position']*100:.1f}% ---")
                    print(f"  T-1首板: close={c['yd_close']:.2f} pre={c['yd_pre']:.2f} 涨幅{c['yd_pct']:+.2f}%")
                    print(f"  T日买入基准 open={c['t_open']:.2f} 高开{c['open_rate']:+.2f}% "
                          f"| T收盘{(c['t_close']-c['t_pre'])/c['t_pre']*100:+.2f}%")
                    ti = all_days.index(today)
                    win = all_days[max(0, ti-5): min(len(all_days), ti+6)]
                    rows = get_window_hours(cur, code, win)
                    if rows:
                        print(format_hour_table(rows, today, c['t_open']))

                # ---- 收益统计(所有候选都纳入) ----
                for bp, price in bp_prices.items():
                    rets = compute_returns(cur, code, today, all_days, price)
                    if not rets:
                        continue
                    for sp in sell_points:
                        if sp in rets:
                            bucket[bp][sp].append(rets[sp])

    # ================= 汇总统计 =================
    print(f"\n\n{'='*80}")
    print("汇总统计")
    print(f"{'='*80}")
    n_months = len([m for m in months if get_trading_days(cur, m)])
    print(f"扫描月数: {n_months} | 出现信号的交易日: {signal_dates} | 总候选(信号)数: {total_candidates}")
    if n_months > 0:
        print(f"月均信号数: {total_candidates / n_months:.1f} | 年均信号数: {total_candidates / n_months * 12:.1f}")

    print(f"\n{'买入点':<8}{'卖出点':<10}{'样本':<7}{'均值%':<9}{'中位%':<9}{'胜率%':<8}")
    print('-' * 55)
    best = None
    for bp in buy_points:
        for sp in sell_points:
            n, mean, median, win = agg_stats(bucket[bp][sp])
            if n == 0:
                continue
            print(f"{bp:<8}{sp:<10}{n:<7}{mean:<+9.2f}{median:<+9.2f}{win:<8.1f}")
            # 择优: 样本足够 且 均值*胜率 综合
            if n >= 20:
                score = mean
                if best is None or (mean > best['mean'] and win >= 50):
                    if win >= 50:
                        best = {'bp': bp, 'sp': sp, 'n': n, 'mean': mean, 'median': median, 'win': win}

    print(f"\n{'='*80}")
    if best:
        # 估算月化: 每月信号数 * 每笔均值 (slot=1 假设逐笔轮动, 粗略)
        per_month = total_candidates / n_months if n_months else 0
        print(f"推荐配置: 买入={best['bp']} 卖出={best['sp']}")
        print(f"  样本={best['n']} 平均收益={best['mean']:+.2f}% 中位={best['median']:+.2f}% 胜率={best['win']:.1f}%")
        print(f"  月均信号≈{per_month:.1f}笔, 若slot=1逐笔吃到, 粗估月化≈{best['mean']*min(per_month,4):+.2f}%")
    else:
        print("未找到 胜率>=50% 且 样本>=20 的稳健配置")
    print(f"{'='*80}")
    print("研究完成。")
    conn.close()


if __name__ == '__main__':
    main()
