#!/usr/bin/env python3
"""
连续缩量下跌后首次放量反弹策略 - 候选股研究脚本 (Task #90, rule2流程)

策略思路:
  股票连续多日缩量下跌(卖压逐步枯竭)，突然某天放量(新买入力量介入)且收阳线。
  这是经典的底部反转信号。

信号定义(T日=放量反弹信号日):
  1. 前5日累计跌幅 >= 10%: (close[T-1] - close[T-5]) / close[T-5] <= -10%
  2. 前5日成交量递减趋势: 递减步数 >= 3 或 5日均量 < 0.9 * 10日均量 (缩量)
  3. 今日(T)放量: volume[T] >= 2.0 * 前5日均量
  4. 今日(T)收阳: close[T] > open[T]
  5. 排除ST、次新股(上市不足60个交易日)、一字涨停

T+1合规:
  信号需用T日全天数据(收盘量、收阳)确认，故最早合规买入 = T+1开盘。
  分析中同时测量:
    方案A(参考): T日尾盘(收盘价)买入   —— 借助14:57可观察到放量收阳,弱合规
    方案B(合规): T+1开盘价买入          —— 完全合规,引擎首选
  卖出遵守T+1: 买入次日起才可卖出。

用法:
  单月详细:   python strategy_vol_surge_reversal.py 2026-04
  区间统计:   python strategy_vol_surge_reversal.py 2021-01 2026-06
"""
import sys
import sqlite3
import math
from collections import defaultdict

# ==================== 配置区 ====================
DB_PATH = '/home/AIWealth/data/stocks.db'

DECLINE_THRESHOLD = -10.0     # 前5日累计跌幅阈值(%)
VOL_SURGE_RATIO = 2.0         # 今日放量倍数(相对前5日均量)
VOL_SHRINK_RATIO = 0.9        # 缩量判定: 5日均量 < 0.9*10日均量
DECLINE_STEPS_MIN = 3         # 前5日成交量递减步数下限(0~4)
MIN_LISTING_DAYS = 60         # 次新股过滤: 上市交易日数下限
MAX_CANDIDATES_DETAIL = 6     # 每天最多打印明细数

# 分析用参数网格
HOLD_DAYS_GRID = [1, 2, 3, 5]
TP_GRID = [3.0, 5.0, 8.0, 10.0, 15.0, None]     # 止盈%
SL_GRID = [-3.0, -5.0, -8.0, None]              # 止损%
# ================================================


def get_limit_ratio(code):
    if code.startswith('sz.300') or code.startswith('sz.301'):
        return 0.20
    elif code.startswith('sh.688'):
        return 0.20
    elif code.startswith('bj.'):
        return 0.30
    else:
        return 0.10


def calc_limit_up(preclose, code):
    return round(preclose * (1 + get_limit_ratio(code)), 2)


def calc_limit_down(preclose, code):
    return round(preclose * (1 - get_limit_ratio(code)), 2)


def is_yizi_limit_up(open_p, high, low, close, preclose, code):
    """一字涨停判定: 四价相等且等于涨停价"""
    if any(v is None for v in [open_p, high, low, close, preclose]):
        return False
    if preclose <= 0:
        return False
    if open_p == high == low == close and close >= calc_limit_up(preclose, code):
        return True
    return False


def get_trading_days(cur, month_str):
    cur.execute("SELECT DISTINCT date FROM stock_kline WHERE date LIKE ? ORDER BY date",
                (month_str + '%',))
    return [r[0] for r in cur.fetchall()]


def get_all_trading_days(cur):
    cur.execute("SELECT DISTINCT date FROM stock_kline ORDER BY date")
    return [r[0] for r in cur.fetchall()]


def get_listing_day_count(cur, code, upto_date):
    """该股票截至upto_date在库交易日数量(用于次新过滤)"""
    cur.execute("SELECT COUNT(*) FROM stock_kline WHERE code = ? AND date <= ?",
                (code, upto_date))
    return cur.fetchone()[0]


def find_candidates(cur, today, all_days, today_idx):
    """
    筛选T日=放量反弹信号日的候选股
    """
    # 需要 T-10 .. T-1 共10日历史 + today
    if today_idx < 11:
        return []

    prev_days = all_days[today_idx - 10:today_idx]   # T-10 .. T-1 (10天)
    yesterday = all_days[today_idx - 1]              # T-1
    d5_ago = all_days[today_idx - 5]                 # T-5

    # today全天数据(信号确认用) + T+1开盘价查询在外部
    cur.execute("""
        SELECT code, code_name, open, high, low, close, preclose, volume, turn, isST
        FROM stock_kline WHERE date = ? AND preclose > 0
    """, (today,))
    today_rows = cur.fetchall()

    # 前10日的 close/volume 批量拉取
    placeholders = ','.join(['?'] * len(prev_days))
    cur.execute(f"""
        SELECT code, date, close, volume FROM stock_kline
        WHERE date IN ({placeholders})
    """, prev_days)
    hist = defaultdict(dict)
    for code, date, close, volume in cur.fetchall():
        hist[code][date] = (close, volume)

    candidates = []
    for row in today_rows:
        (code, code_name, t_open, t_high, t_low, t_close,
         t_preclose, t_volume, t_turn, t_isST) = row

        # --- 排除ST ---
        if t_isST:
            continue
        if code_name and 'ST' in code_name.upper():
            continue
        # 排除北交所(流动性差,可选)——保留主板/创业/科创
        if code.startswith('bj.'):
            continue

        if t_open is None or t_close is None or t_volume is None or t_volume <= 0:
            continue

        # --- 条件4: 今日收阳 ---
        if not (t_close > t_open):
            continue

        # --- 排除一字涨停(无法买入次日不受影响,但信号日一字通常追高) ---
        if is_yizi_limit_up(t_open, t_high, t_low, t_close, t_preclose, code):
            continue

        # --- 取前10日数据 ---
        code_hist = hist.get(code, {})
        # 需前10日齐全
        if len(code_hist) < 10:
            continue
        # 按日期排序
        ordered = [code_hist[d] for d in prev_days if d in code_hist]
        if len(ordered) < 10:
            continue
        closes = [c for c, v in ordered]     # T-10..T-1 close
        vols = [v for c, v in ordered]       # T-10..T-1 volume
        if any(c is None or c <= 0 for c in closes):
            continue
        if any(v is None or v <= 0 for v in vols):
            continue

        # --- 条件1: 前5日累计跌幅 (close[T-5] -> close[T-1]) ---
        close_t5 = closes[5]   # prev_days[5] = T-5
        close_t1 = closes[9]   # prev_days[9] = T-1
        decline_pct = (close_t1 - close_t5) / close_t5 * 100
        if decline_pct > DECLINE_THRESHOLD:
            continue

        # --- 前5日/前10日成交量 ---
        vol_prev5 = vols[5:10]     # T-5..T-1
        vol_prev10 = vols          # T-10..T-1
        avg_vol5 = sum(vol_prev5) / len(vol_prev5)
        avg_vol10 = sum(vol_prev10) / len(vol_prev10)

        # --- 条件2: 缩量趋势 ---
        # 递减步数: 相邻日下降计数(T-5..T-1)
        dec_steps = sum(1 for i in range(1, len(vol_prev5)) if vol_prev5[i] < vol_prev5[i - 1])
        shrink_ok = (dec_steps >= DECLINE_STEPS_MIN) or (avg_vol5 < VOL_SHRINK_RATIO * avg_vol10)
        if not shrink_ok:
            continue

        # --- 条件3: 今日放量 >= 5日均量 * 2 ---
        surge_ratio = t_volume / avg_vol5 if avg_vol5 > 0 else 0
        if surge_ratio < VOL_SURGE_RATIO:
            continue

        # --- 排除次新股 ---
        if get_listing_day_count(cur, code, yesterday) < MIN_LISTING_DAYS:
            continue

        t_close_rate = (t_close - t_preclose) / t_preclose * 100 if t_preclose > 0 else 0

        candidates.append({
            'code': code,
            'code_name': code_name,
            'today': today,
            'yesterday': yesterday,
            'decline_pct': decline_pct,
            'avg_vol5': avg_vol5,
            'avg_vol10': avg_vol10,
            'dec_steps': dec_steps,
            'surge_ratio': surge_ratio,
            't_open': t_open,
            't_close': t_close,
            't_high': t_high,
            't_low': t_low,
            't_close_rate': t_close_rate,
            't_turn': t_turn,
        })

    candidates.sort(key=lambda x: x['surge_ratio'], reverse=True)
    return candidates


def get_next_day_open(cur, code, next_day):
    """T+1开盘价及是否一字涨停(不可买)"""
    cur.execute("""
        SELECT open, high, low, close, preclose FROM stock_kline
        WHERE code = ? AND date = ?
    """, (code, next_day))
    r = cur.fetchone()
    if not r:
        return None, False
    o, h, l, c, pc = r
    if o is None or o <= 0:
        return None, False
    yizi = is_yizi_limit_up(o, h, l, c, pc, code)
    return o, yizi


def get_future_daily(cur, code, days_list):
    """获取未来若干日 open/high/low/close/preclose,按日期排序"""
    if not days_list:
        return []
    placeholders = ','.join(['?'] * len(days_list))
    cur.execute(f"""
        SELECT date, open, high, low, close, preclose FROM stock_kline
        WHERE code = ? AND date IN ({placeholders}) ORDER BY date
    """, [code] + days_list)
    return cur.fetchall()


def preload_sell_window(cur, code, buy_day_idx, all_days, max_hold):
    """预加载买入日次日起 max_hold 个交易日的 (h,l,c),供内存模拟复用"""
    sell_days = all_days[buy_day_idx + 1: buy_day_idx + 1 + max_hold]
    if not sell_days:
        return []
    rows = get_future_daily(cur, code, sell_days)
    # 返回按日期序的 (high, low, close)
    return [(r[2], r[3], r[4]) for r in rows]


def simulate_trade(sell_rows, buy_price, hold_days, tp, sl):
    """
    纯内存模拟一笔交易:
      sell_rows: 买入日次日起的 (high, low, close) 列表(已按日期序)
      hold_days: 最大持有交易日数,第hold_days日收盘强制平仓
      tp/sl: 止盈/止损百分比(None=不设)
    合规: T+1才可卖出,sell_rows已从买入次日开始。
    返回: (ret_pct, exit_reason, hold_used)  无数据返回None
    """
    if buy_price is None or buy_price <= 0:
        return None
    rows = sell_rows[:hold_days]
    if not rows:
        return None

    tp_price = buy_price * (1 + tp / 100) if tp is not None else None
    sl_price = buy_price * (1 + sl / 100) if sl is not None else None

    last_close = None
    for i, (h, l, c) in enumerate(rows):
        if c is None:
            continue
        last_close = c
        # 止损优先(保守: 同日先判止损)
        if sl_price is not None and l is not None and l <= sl_price:
            return ((sl_price - buy_price) / buy_price * 100, 'SL', i + 1)
        if tp_price is not None and h is not None and h >= tp_price:
            return ((tp_price - buy_price) / buy_price * 100, 'TP', i + 1)
    if last_close is None:
        return None
    return ((last_close - buy_price) / buy_price * 100, 'HOLD', len(rows))


def print_hour_window(cur, code, today_idx, all_days, base_open):
    """打印 T-5..T+5 共11天 hour级OCHL,以相对T日开盘价(base_open)的rate表示"""
    start = max(0, today_idx - 5)
    end = min(len(all_days), today_idx + 6)
    window = all_days[start:end]
    if not window:
        return
    placeholders = ','.join(['?'] * len(window))
    cur.execute(f"""
        SELECT date, turn,
               hour1_open, hour1_high, hour1_low, hour1_close,
               hour2_open, hour2_high, hour2_low, hour2_close,
               hour3_open, hour3_high, hour3_low, hour3_close,
               hour4_open, hour4_high, hour4_low, hour4_close
        FROM stock_kline WHERE code = ? AND date IN ({placeholders})
        ORDER BY date
    """, [code] + window)
    rows = cur.fetchall()
    today = all_days[today_idx]

    def rate(p):
        if p is None or base_open <= 0:
            return "  N/A "
        return f"{(p - base_open) / base_open * 100:+6.1f}"

    print(f"  hour级OCHL明细(相对T日开盘价 {base_open:.2f} 的rate%):")
    print(f"  {'日期':<12}| {'偏移':<5}| {'H1(O/H/L/C)':<28}| {'H2(O/H/L/C)':<28}| turn")
    for row in rows:
        date = row[0]
        turn = row[1]
        offset_idx = all_days.index(date) - today_idx
        offset = f"T{offset_idx:+d}" if offset_idx != 0 else "T ★"
        h1 = f"{rate(row[2])}/{rate(row[3])}/{rate(row[4])}/{rate(row[5])}"
        h2 = f"{rate(row[6])}/{rate(row[7])}/{rate(row[8])}/{rate(row[9])}"
        h3 = f"{rate(row[10])}/{rate(row[11])}/{rate(row[12])}/{rate(row[13])}"
        h4 = f"{rate(row[14])}/{rate(row[15])}/{rate(row[16])}/{rate(row[17])}"
        turn_str = f"{turn:.1f}%" if turn else "-"
        print(f"  {date:<12}| {offset:<5}| {h1:<28}| {h2:<28}| {turn_str}")
        print(f"  {'':<12}| {'':<5}| H3:{h3:<25}| H4:{h4:<25}|")


def collect_trades(cur, month_days, all_days, detail=False):
    """
    遍历给定交易日,收集所有信号的买入基准价,用于后续参数网格分析。
    返回 trades 列表: 每条含 code, signal_idx(T), buyA_price(T收盘), buyB_idx/buyB_price(T+1开盘)
    """
    trades = []
    day_signal_count = defaultdict(int)

    for today in month_days:
        try:
            today_idx = all_days.index(today)
        except ValueError:
            continue

        candidates = find_candidates(cur, today, all_days, today_idx)
        day_signal_count[today] = len(candidates)

        if detail:
            print(f"\n{'=' * 70}")
            print(f"{'=' * 12} {today}  候选股: {len(candidates)}只 {'=' * 12}")
            print(f"{'=' * 70}")

        shown = 0
        for c in candidates:
            code = c['code']
            # 方案A: T日收盘价买入(买入日=today_idx)
            buyA_price = c['t_close']
            # 方案B: T+1开盘价买入(买入日=today_idx+1)
            next_idx = today_idx + 1
            buyB_price, buyB_yizi = (None, False)
            if next_idx < len(all_days):
                next_day = all_days[next_idx]
                buyB_price, buyB_yizi = get_next_day_open(cur, code, next_day)

            max_hold = max(HOLD_DAYS_GRID)
            # 预加载卖出窗口(方案A买入日=today_idx; 方案B买入日=next_idx)
            sellA = preload_sell_window(cur, code, today_idx, all_days, max_hold)
            sellB = preload_sell_window(cur, code, next_idx, all_days, max_hold) if next_idx < len(all_days) else []

            trades.append({
                'code': code,
                'code_name': c['code_name'],
                'today': today,
                'signal_idx': today_idx,
                'buyA_price': buyA_price,
                'buyB_price': None if buyB_yizi else buyB_price,
                'buyB_yizi': buyB_yizi,
                'sellA': sellA,
                'sellB': sellB,
                'surge_ratio': c['surge_ratio'],
                'decline_pct': c['decline_pct'],
            })

            if detail and shown < MAX_CANDIDATES_DETAIL:
                shown += 1
                print(f"\n--- {code} ({c['code_name'] or 'N/A'}) ---")
                print(f"  前5日累计跌幅: {c['decline_pct']:.1f}% | "
                      f"缩量递减步数: {c['dec_steps']}/4 | "
                      f"5日均量/10日均量: {c['avg_vol5']/c['avg_vol10']:.2f}")
                print(f"  今日放量: {c['surge_ratio']:.1f}倍(vs前5日均量) | "
                      f"今日收阳 close_rate={c['t_close_rate']:+.1f}% | turn={c['t_turn']:.1f}%")
                buyb_desc = "一字涨停不可买" if buyB_yizi else (f"{buyB_price:.2f}" if buyB_price else "N/A")
                print(f"  买入基准: T日收盘={buyA_price:.2f} | T+1开盘={buyb_desc}")
                print_hour_window(cur, code, today_idx, all_days, c['t_open'])

    return trades, day_signal_count


def analyze_param_grid(cur, trades, all_days, num_months, label):
    """
    对方案A/方案B分别做 (hold_days x tp x sl) 网格分析,输出满足月化10%+ & 胜率55%+ 的组合。
    月化收益估算 = 单笔平均收益 * 月均信号数
    """
    print(f"\n{'#' * 80}")
    print(f"# 参数网格分析: {label}")
    print(f"# 信号总数: {len(trades)}  跨越月数: {num_months}  月均信号: {len(trades)/num_months:.1f}")
    print(f"{'#' * 80}")

    for scheme, price_key, sell_key, scheme_name in [
        ('B', 'buyB_price', 'sellB', '方案B: T+1开盘买入(合规,引擎首选)'),
        ('A', 'buyA_price', 'sellA', '方案A: T日收盘买入(弱合规,参考)'),
    ]:
        print(f"\n{'=' * 70}")
        print(f"【{scheme_name}】")
        print(f"{'=' * 70}")
        print(f"{'持仓':<5}{'止盈':<7}{'止损':<7}{'样本':<6}{'胜率%':<8}{'单笔均收%':<11}{'月化估算%':<10}{'达标'}")
        print(f"{'-' * 70}")

        qualified = []
        results = []
        for hold in HOLD_DAYS_GRID:
            for tp in TP_GRID:
                for sl in SL_GRID:
                    rets = []
                    for t in trades:
                        bp = t[price_key]
                        sell_rows = t[sell_key]
                        if bp is None or not sell_rows:
                            continue
                        r = simulate_trade(sell_rows, bp, hold, tp, sl)
                        if r is not None:
                            rets.append(r[0])
                    if len(rets) < 10:
                        continue
                    n = len(rets)
                    avg = sum(rets) / n
                    win = sum(1 for x in rets if x > 0) / n * 100
                    monthly = avg * (n / num_months)
                    tp_s = f"{tp:.0f}%" if tp is not None else "无"
                    sl_s = f"{sl:.0f}%" if sl is not None else "无"
                    ok = (monthly >= 10.0 and win >= 55.0)
                    results.append((hold, tp_s, sl_s, n, win, avg, monthly, ok))
                    if ok:
                        qualified.append((monthly, hold, tp_s, sl_s, n, win, avg))

        # 按月化收益排序打印前若干
        results.sort(key=lambda x: x[6], reverse=True)
        for (hold, tp_s, sl_s, n, win, avg, monthly, ok) in results[:15]:
            mark = "★达标" if ok else ""
            print(f"{hold:<5}{tp_s:<7}{sl_s:<7}{n:<6}{win:<8.1f}{avg:<11.2f}{monthly:<10.1f}{mark}")

        if qualified:
            qualified.sort(reverse=True)
            print(f"\n  >> 满足[月化10%+ & 胜率55%+]的组合: {len(qualified)}个")
            best = qualified[0]
            print(f"  >> 最优: 持仓{best[1]}日 止盈{best[2]} 止损{best[3]} "
                  f"→ 月化{best[0]:.1f}% 胜率{best[5]:.1f}% 单笔{best[6]:.2f}% (样本{best[4]})")
        else:
            print(f"\n  >> 无满足[月化10%+ & 胜率55%+]的组合")


def main():
    args = sys.argv[1:]
    if len(args) < 1:
        print("用法:")
        print("  单月详细: python strategy_vol_surge_reversal.py 2026-04")
        print("  区间统计: python strategy_vol_surge_reversal.py 2021-01 2026-06")
        sys.exit(1)

    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    all_days = get_all_trading_days(cur)

    print("=" * 80)
    print("连续缩量下跌后首次放量反弹策略 - 研究 (Task #90)")
    print("信号: 前5日跌幅>=10% + 前5日缩量递减 + 今日放量>=2倍 + 今日收阳, 排ST/次新")
    print("=" * 80)

    if len(args) == 1:
        # 单月详细模式
        month_str = args[0]
        month_days = get_trading_days(cur, month_str)
        if not month_days:
            print(f"错误: 未找到 {month_str} 的交易日数据")
            conn.close()
            sys.exit(1)
        print(f"\n研究月份: {month_str}  交易日数: {len(month_days)}")
        trades, day_count = collect_trades(cur, month_days, all_days, detail=True)
        print(f"\n\n{'=' * 80}")
        print(f"月度信号统计: 共 {len(trades)} 个信号")
        for d in sorted(day_count.keys()):
            if day_count[d] > 0:
                print(f"  {d}: {day_count[d]}只")
        analyze_param_grid(cur, trades, all_days, num_months=1, label=month_str)
    else:
        # 区间模式: args[0]=起始月, args[1]=结束月
        start_m, end_m = args[0], args[1]
        # 生成月份内所有交易日
        cur.execute("SELECT DISTINCT date FROM stock_kline WHERE date >= ? AND date <= ? ORDER BY date",
                    (start_m + '-01', end_m + '-31'))
        range_days = [r[0] for r in cur.fetchall()]
        if not range_days:
            print(f"错误: 未找到 {start_m}~{end_m} 的交易日数据")
            conn.close()
            sys.exit(1)
        # 计算跨越月数
        months = set(d[:7] for d in range_days)
        num_months = len(months)
        print(f"\n研究区间: {start_m} ~ {end_m}  交易日数: {len(range_days)}  月数: {num_months}")
        trades, day_count = collect_trades(cur, range_days, all_days, detail=False)
        print(f"\n信号总数: {len(trades)}  年均信号: {len(trades)/(num_months/12):.1f}")
        analyze_param_grid(cur, trades, all_days, num_months=num_months,
                           label=f"{start_m}~{end_m}")

    conn.close()
    print(f"\n{'=' * 80}")
    print("研究完成。")


if __name__ == '__main__':
    main()
