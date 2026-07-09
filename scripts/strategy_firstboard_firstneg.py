#!/usr/bin/env python3
"""
首板涨停后首阴低吸策略 - rule2 研究脚本 (Task #88)

策略思路:
  - T-2日: 首次涨停(首板, 非连板) —— 之前5个交易日内无涨停
  - T-1日(昨日): 未涨停, 且收跌(收阴 或 close_rate<0) —— 首阴, 获利盘/犹豫盘出逃
  - T日(今日): 候选, 博第三天修复行情, 在 T 日买入

T+1/未来数据合规:
  - 买入决策仅依赖 T-1 及之前已确定的信息(首板事实 + 首阴事实)
  - hour1 买入只用 T.hour1_open; hour2 买入只用 T.hour1(已收) + T.hour2_open
  - 买入日 T 不可当日卖出, 卖出至少 T+1

用法:
  python strategy_firstboard_firstneg.py 2026-04            # 单月: 打印候选hour明细 + 网格统计
  python strategy_firstboard_firstneg.py 2026-01 2026-12    # 区间: 仅网格统计 + 组合回测
  python strategy_firstboard_firstneg.py 2021-01 2026-12    # 全周期验证

结果输出: /home/AIWealth/scripts/logs/firstboard_firstneg.log
"""
import sys
import os
import sqlite3
from datetime import datetime

DB_PATH = '/home/AIWealth/data/stocks.db'
LOG_PATH = '/home/AIWealth/scripts/logs/firstboard_firstneg.log'

# ---- 策略参数 (候选筛选) ----
FIRST_BOARD_LOOKBACK = 5      # T-2 之前多少个交易日内无涨停 → 确认首板
MAX_DETAIL_PER_DAY = 6        # 单月模式下每天最多打印几只候选的hour明细

# ---- 参数网格 (买入时机 / 持有天数 / 止损) ----
ENTRY_OPTIONS = ['h1_open', 'h2_open']   # T日买入时机
HOLD_OPTIONS = [1, 2, 3]                 # 持有到 T+hold 收盘卖出
STOPLOSS_OPTIONS = [None, 0.03, 0.05, 0.07]  # 盘中触发止损(相对买入价)

# ---- 组合回测参数 ----
PORTFOLIO_SLOTS = 3          # 并发持仓档数
INIT_CAPITAL = 1_000_000.0


# ================= Tee: 同时输出到终端与日志文件 =================
class Tee:
    def __init__(self, path):
        self.file = open(path, 'w', encoding='utf-8')
    def write(self, s):
        sys.__stdout__.write(s)
        self.file.write(s)
    def flush(self):
        sys.__stdout__.flush()
        self.file.flush()
    def close(self):
        self.file.close()


def log(*args, **kwargs):
    print(*args, **kwargs)


# ================= 涨跌停判定 =================
def get_limit_ratio(code):
    if code.startswith('sz.300') or code.startswith('sz.301'):
        return 0.20
    if code.startswith('sh.688'):
        return 0.20
    if code.startswith('bj.'):
        return 0.30
    return 0.10


def calc_limit_up(preclose, code):
    return round(preclose * (1 + get_limit_ratio(code)), 2)


def is_limit_up(close, preclose, code):
    if preclose is None or preclose <= 0 or close is None:
        return False
    return round(close / preclose, 2) >= round(1 + get_limit_ratio(code), 2)


def is_one_word(o, h, l, c):
    if any(v is None for v in (o, h, l, c)):
        return True
    return (max(o, h, l, c) - min(o, h, l, c)) < 0.01


# ================= 交易日 =================
def get_trading_days_like(cur, prefix):
    cur.execute("SELECT DISTINCT date FROM stock_kline WHERE date LIKE ? ORDER BY date",
                (prefix + '%',))
    return [r[0] for r in cur.fetchall()]


def get_all_trading_days(cur):
    cur.execute("SELECT DISTINCT date FROM stock_kline ORDER BY date")
    return [r[0] for r in cur.fetchall()]


def month_range_days(cur, start_month, end_month):
    """返回 [start_month .. end_month] 之间的所有交易日 (含端点月)。"""
    cur.execute("SELECT DISTINCT date FROM stock_kline WHERE date >= ? AND date <= ? ORDER BY date",
                (start_month + '-01', end_month + '-31'))
    return [r[0] for r in cur.fetchall()]


# ================= 候选筛选 =================
def had_limit_up_in_prev_days(cur, code, before_date, n):
    """检查 code 在 before_date 之前 n 个交易日内是否出现过涨停。"""
    cur.execute("""
        SELECT close, preclose FROM stock_kline
        WHERE code = ? AND date < ? ORDER BY date DESC LIMIT ?
    """, (code, before_date, n))
    for close, preclose in cur.fetchall():
        if is_limit_up(close, preclose, code):
            return True
    return False


def find_candidates_for_day(cur, all_days, idx):
    """
    对交易日 T=all_days[idx] 找候选:
      T-2 首板涨停, 之前5日无涨停; T-1 未涨停且收阴(首阴); T 为候选(买入日)。
    返回候选 dict 列表。
    """
    if idx < 2:
        return []
    T = all_days[idx]
    t1 = all_days[idx - 1]   # 昨日 (首阴)
    t2 = all_days[idx - 2]   # 前日 (首板涨停)

    # T-2 涨停股
    cur.execute("""
        SELECT code, code_name, open, close, preclose, isST
        FROM stock_kline WHERE date = ? AND preclose > 0
    """, (t2,))
    t2_rows = {r[0]: r for r in cur.fetchall()}

    # T-1 数据
    cur.execute("""
        SELECT code, open, close, preclose, close_rate FROM stock_kline WHERE date = ?
    """, (t1,))
    t1_map = {r[0]: r for r in cur.fetchall()}

    # T 数据 (含hour)
    cur.execute("""
        SELECT code, code_name, open, high, low, close, preclose, isST,
               hour1_open, hour1_high, hour1_low, hour1_close,
               hour2_open, hour2_high, hour2_low, hour2_close,
               hour3_open, hour3_high, hour3_low, hour3_close,
               hour4_open, hour4_high, hour4_low, hour4_close
        FROM stock_kline WHERE date = ?
    """, (T,))
    t_map = {r[0]: r for r in cur.fetchall()}

    candidates = []
    for code, r2 in t2_rows.items():
        code_name = r2[1]
        t2_close, t2_preclose, t2_isST = r2[3], r2[4], r2[5]

        # 排除ST
        if t2_isST or (code_name and 'ST' in code_name.upper()):
            continue
        # T-2 必须首板涨停
        if not is_limit_up(t2_close, t2_preclose, code):
            continue
        # T-2 之前5日无涨停 → 确认首板
        if had_limit_up_in_prev_days(cur, code, t2, FIRST_BOARD_LOOKBACK):
            continue

        # T-1 首阴: 未涨停 且 收跌
        if code not in t1_map:
            continue
        _, t1_open, t1_close, t1_preclose, t1_close_rate = t1_map[code]
        if t1_close is None or t1_open is None:
            continue
        if is_limit_up(t1_close, t1_preclose, code):
            continue  # T-1 连板了, 不符合"首阴"
        is_neg = (t1_close < t1_open)
        if t1_close_rate is not None:
            is_neg = is_neg or (t1_close_rate < 0)
        if not is_neg:
            continue

        # T 数据
        if code not in t_map:
            continue
        t = t_map[code]
        t_open, t_high, t_low, t_close, t_preclose, t_isST = t[2], t[3], t[4], t[5], t[6], t[7]
        if t_isST or (t[1] and 'ST' in t[1].upper()):
            continue
        if t_preclose is None or t_preclose <= 0 or t_open is None or t_open <= 0:
            continue

        candidates.append({
            'code': code,
            'code_name': code_name,
            'T': T, 't1': t1, 't2': t2, 'idx': idx,
            't2_pct': (t2_close - t2_preclose) / t2_preclose * 100,
            't1_pct': (t1_close - t1_preclose) / t1_preclose * 100 if t1_preclose else 0,
            't_open': t_open, 't_high': t_high, 't_low': t_low,
            't_close': t_close, 't_preclose': t_preclose,
            't_open_rate': (t_open - t_preclose) / t_preclose * 100,
            'hours': {
                'h1': (t[8], t[9], t[10], t[11]),
                'h2': (t[12], t[13], t[14], t[15]),
                'h3': (t[16], t[17], t[18], t[19]),
                'h4': (t[20], t[21], t[22], t[23]),
            }
        })
    candidates.sort(key=lambda x: x['t1_pct'])  # 首阴跌得越多排前(低吸)
    return candidates


# ================= hour明细获取/打印 =================
def get_window_rows(cur, code, days):
    if not days:
        return []
    ph = ','.join('?' * len(days))
    cur.execute(f"""
        SELECT date, open, high, low, close, preclose,
               hour1_open, hour1_high, hour1_low, hour1_close,
               hour2_open, hour2_high, hour2_low, hour2_close,
               hour3_open, hour3_high, hour3_low, hour3_close,
               hour4_open, hour4_high, hour4_low, hour4_close
        FROM stock_kline WHERE code = ? AND date IN ({ph}) ORDER BY date
    """, [code] + days)
    return cur.fetchall()


def print_hour_table(rows, buy_date, ref_price):
    """打印hour级OCHL, 用相对T日开盘价(ref_price)的涨跌幅率表示。"""
    log(f"  {'日期':<11}|{'hr':<4}|{'open%':>8}|{'high%':>8}|{'low%':>8}|{'close%':>8}")
    log(f"  {'-'*11}+{'-'*4}+{'-'*8}+{'-'*8}+{'-'*8}+{'-'*8}")
    for row in rows:
        date = row[0]
        hours = [('h1', row[6], row[7], row[8], row[9]),
                 ('h2', row[10], row[11], row[12], row[13]),
                 ('h3', row[14], row[15], row[16], row[17]),
                 ('h4', row[18], row[19], row[20], row[21])]
        mark = '  <==T' if date == buy_date else ''
        for hn, o, h, l, c in hours:
            if o is None or c is None:
                continue
            def rate(x):
                return (x - ref_price) / ref_price * 100 if (x is not None and ref_price > 0) else 0.0
            log(f"  {date:<11}|{hn:<4}|{rate(o):>+7.2f}|{rate(h):>+7.2f}|{rate(l):>+7.2f}|{rate(c):>+7.2f}{mark if hn=='h1' else ''}")


# ================= 交易结果模拟 =================
def get_entry_price(cand, entry):
    """获取T日买入价; 返回 (price, ok)。合规: 不买一字板/无效价。"""
    h1 = cand['hours']['h1']
    h2 = cand['hours']['h2']
    if entry == 'h1_open':
        o, h, l, c = h1
        if is_one_word(o, h, l, c):
            return None, False
        return (o, True) if (o and o > 0) else (None, False)
    if entry == 'h2_open':
        # 需要 hour1 已收(确认), hour2_open 作为买入价
        if any(v is None for v in h1):
            return None, False
        o2 = h2[0]
        if o2 is None or o2 <= 0:
            return None, False
        # 一字板(全天封死)时 hour2 也无法成交
        if is_one_word(*h2):
            return None, False
        return o2, True
    return None, False


def simulate_trade(cur, cand, all_days, entry, hold, stoploss):
    """
    模拟一笔交易。买入 T日 entry, 持有到 T+hold 收盘卖出; 期间盘中触发止损则提前卖。
    返回收益率(%) 或 None(无法交易)。
    """
    buy_price, ok = get_entry_price(cand, entry)
    if not ok:
        return None
    idx = cand['idx']
    # 取 T .. T+hold 的日线
    fut = all_days[idx: idx + hold + 1]
    if len(fut) < hold + 1:
        return None
    rows = get_window_rows(cur, cand['code'], fut)
    row_by_date = {r[0]: r for r in rows}

    # 逐日检查(从 T+1 开始允许卖出)
    for d in range(1, hold + 1):
        day = all_days[idx + d]
        r = row_by_date.get(day)
        if r is None:
            continue
        d_open, d_high, d_low, d_close, d_preclose = r[1], r[2], r[3], r[4], r[5]
        # 止损: 盘中最低触及止损价
        if stoploss is not None and d_low is not None and buy_price > 0:
            stop_price = buy_price * (1 - stoploss)
            if d_low <= stop_price:
                # 跳空低开则按开盘价成交, 否则按止损价
                fill = d_open if (d_open is not None and d_open < stop_price) else stop_price
                return (fill - buy_price) / buy_price * 100
        # 到期卖出(T+hold 收盘)
        if d == hold:
            if d_close is None or d_close <= 0:
                return None
            return (d_close - buy_price) / buy_price * 100
    return None


# ================= 组合回测(得到真实月化/年化) =================
def portfolio_backtest(cur, cand_by_day, scan_days, all_days, entry, hold, stoploss):
    """
    简单等权组合: 最多 PORTFOLIO_SLOTS 档并发。每档买入后持有到卖出释放。
    按 scan_days 顺序推进; 同日多候选按首阴幅度优先。
    返回 (final_capital, n_trades, win_trades)
    """
    capital = INIT_CAPITAL
    # 每档: {'code','sell_idx','buy_price','shares_value'} 简化为记录卖出日与收益
    open_positions = []  # list of (sell_day_idx, ret_pct_realized_amount) -> 用金额
    # 用逐日现金流模型: 买入占用一份资金, 卖出日归还本金*(1+ret)
    pending_sells = {}   # sell_idx -> list of (invested_amount, ret_pct)
    slots_used = 0
    n_trades = 0
    win = 0

    for idx in range(len(all_days)):
        day = all_days[idx]
        # 先结算今日到期卖出
        if idx in pending_sells:
            for invested, ret in pending_sells[idx]:
                capital += invested * (1 + ret / 100.0)
                slots_used -= 1
            del pending_sells[idx]
        if day not in scan_days:
            continue
        cands = cand_by_day.get(day, [])
        for cand in cands:
            if slots_used >= PORTFOLIO_SLOTS:
                break
            # 计算该笔交易实际卖出日与收益
            res = simulate_trade_with_exit(cur, cand, all_days, entry, hold, stoploss)
            if res is None:
                continue
            ret, sell_idx = res
            invest = capital / (PORTFOLIO_SLOTS - slots_used)
            capital -= invest
            pending_sells.setdefault(sell_idx, []).append((invest, ret))
            slots_used += 1
            n_trades += 1
            if ret > 0:
                win += 1
    # 结算剩余
    for sell_idx in sorted(pending_sells):
        for invested, ret in pending_sells[sell_idx]:
            capital += invested * (1 + ret / 100.0)
    return capital, n_trades, win


def simulate_trade_with_exit(cur, cand, all_days, entry, hold, stoploss):
    """同 simulate_trade, 但同时返回卖出日索引。"""
    buy_price, ok = get_entry_price(cand, entry)
    if not ok:
        return None
    idx = cand['idx']
    fut = all_days[idx: idx + hold + 1]
    if len(fut) < hold + 1:
        return None
    rows = get_window_rows(cur, cand['code'], fut)
    row_by_date = {r[0]: r for r in rows}
    for d in range(1, hold + 1):
        day = all_days[idx + d]
        r = row_by_date.get(day)
        if r is None:
            continue
        d_open, d_low, d_close = r[1], r[3], r[4]
        if stoploss is not None and d_low is not None and buy_price > 0:
            stop_price = buy_price * (1 - stoploss)
            if d_low <= stop_price:
                fill = d_open if (d_open is not None and d_open < stop_price) else stop_price
                return (fill - buy_price) / buy_price * 100, idx + d
        if d == hold:
            if d_close is None or d_close <= 0:
                return None
            return (d_close - buy_price) / buy_price * 100, idx + d
    return None


# ================= 主流程 =================
def collect_candidates(cur, scan_days, all_days):
    all_idx = {d: i for i, d in enumerate(all_days)}
    cand_by_day = {}
    total = 0
    for day in scan_days:
        idx = all_idx.get(day)
        if idx is None:
            continue
        cands = find_candidates_for_day(cur, all_days, idx)
        if cands:
            cand_by_day[day] = cands
            total += len(cands)
    return cand_by_day, total


def run_grid_stats(cur, cand_by_day, all_days):
    """对参数网格统计每笔胜率/平均收益。返回结果列表(按平均收益降序)。"""
    flat = [c for cs in cand_by_day.values() for c in cs]
    results = []
    for entry in ENTRY_OPTIONS:
        for hold in HOLD_OPTIONS:
            for sl in STOPLOSS_OPTIONS:
                rets = []
                for c in flat:
                    r = simulate_trade(cur, c, all_days, entry, hold, sl)
                    if r is not None:
                        rets.append(r)
                if not rets:
                    continue
                n = len(rets)
                win = sum(1 for r in rets if r > 0)
                avg = sum(rets) / n
                results.append({
                    'entry': entry, 'hold': hold, 'sl': sl,
                    'n': n, 'win_rate': win / n * 100, 'avg': avg,
                    'total_pnl': sum(rets),
                })
    results.sort(key=lambda x: x['avg'], reverse=True)
    return results


def print_grid(results, months):
    log(f"\n{'='*92}")
    log("参数网格统计 (每笔交易维度)")
    log(f"{'='*92}")
    log(f"{'买入':<9}{'持有':<6}{'止损':<8}{'样本':<7}{'胜率%':<9}{'均收益%':<10}{'月化估算%':<12}")
    log(f"{'-'*92}")
    for r in results:
        sl = '无' if r['sl'] is None else f"{r['sl']*100:.0f}%"
        # 月化估算: 每笔平均收益 * (月均信号数); 信号频率=样本/月数
        sig_per_month = r['n'] / months if months > 0 else 0
        monthly = r['avg'] * sig_per_month / PORTFOLIO_SLOTS
        log(f"{r['entry']:<9}{('T+'+str(r['hold'])):<6}{sl:<8}{r['n']:<7}"
            f"{r['win_rate']:<9.1f}{r['avg']:<+10.2f}{monthly:<+12.2f}")


def analyze_hourly_pattern(cur, cand_by_day, all_days):
    """Step3: 统计候选股在不同买入时机/持有天数的收益规律(汇总)。"""
    flat = [c for cs in cand_by_day.values() for c in cs]
    log(f"\n{'='*92}")
    log("买入时机 × 持有天数 规律分析 (无止损)")
    log(f"{'='*92}")
    log(f"{'买入':<10}{'持有':<8}{'样本':<8}{'胜率%':<10}{'均收益%':<10}{'中位%':<10}")
    log(f"{'-'*92}")
    for entry in ENTRY_OPTIONS:
        for hold in HOLD_OPTIONS:
            rets = []
            for c in flat:
                r = simulate_trade(cur, c, all_days, entry, hold, None)
                if r is not None:
                    rets.append(r)
            if not rets:
                continue
            rets.sort()
            n = len(rets)
            win = sum(1 for r in rets if r > 0)
            med = rets[n // 2]
            log(f"{entry:<10}{('T+'+str(hold)):<8}{n:<8}{win/n*100:<10.1f}"
                f"{sum(rets)/n:<+10.2f}{med:<+10.2f}")


def print_month_detail(cur, cand_by_day, all_days):
    """Step2: 打印候选股 T-5~T+5 的hour级明细。"""
    for day in sorted(cand_by_day):
        cands = cand_by_day[day]
        log(f"\n{'='*70}")
        log(f"===== 交易日 {day}  候选 {len(cands)} 只 =====")
        log(f"{'='*70}")
        log(f"  {'代码':<12}{'名称':<10}{'首板涨幅%':<10}{'首阴涨幅%':<10}{'T开盘%':<8}")
        for c in cands:
            log(f"  {c['code']:<12}{(c['code_name'] or ''):<10}"
                f"{c['t2_pct']:<+10.2f}{c['t1_pct']:<+10.2f}{c['t_open_rate']:<+8.2f}")
        for c in cands[:MAX_DETAIL_PER_DAY]:
            idx = c['idx']
            lo = max(0, idx - 5)
            hi = min(len(all_days), idx + 6)
            window = all_days[lo:hi]
            rows = get_window_rows(cur, c['code'], window)
            ref = c['t_open']
            log(f"\n  --- {c['code']} ({c['code_name'] or 'N/A'})  "
                f"首板{c['t2_pct']:+.1f}% 首阴{c['t1_pct']:+.1f}% ---")
            log(f"  (涨跌幅率均相对 T日开盘价 {ref:.2f})")
            print_hour_table(rows, day, ref)


def run_portfolio(cur, cand_by_day, scan_days, all_days, top_results, months):
    log(f"\n{'='*92}")
    log(f"组合回测 (等权 {PORTFOLIO_SLOTS} 档, 初始资金 {INIT_CAPITAL:,.0f})")
    log(f"{'='*92}")
    log(f"{'买入':<9}{'持有':<6}{'止损':<8}{'笔数':<7}{'胜率%':<9}{'总收益%':<11}{'月化%':<10}{'年化%':<10}")
    log(f"{'-'*92}")
    years = months / 12.0
    for r in top_results:
        final, n, win = portfolio_backtest(
            cur, cand_by_day, scan_days, all_days, r['entry'], r['hold'], r['sl'])
        total_ret = (final / INIT_CAPITAL - 1) * 100
        monthly = ((final / INIT_CAPITAL) ** (1 / months) - 1) * 100 if months > 0 else 0
        annual = ((final / INIT_CAPITAL) ** (1 / years) - 1) * 100 if years > 0 else 0
        sl = '无' if r['sl'] is None else f"{r['sl']*100:.0f}%"
        wr = win / n * 100 if n else 0
        log(f"{r['entry']:<9}{('T+'+str(r['hold'])):<6}{sl:<8}{n:<7}"
            f"{wr:<9.1f}{total_ret:<+11.1f}{monthly:<+10.2f}{annual:<+10.1f}")


def main():
    args = sys.argv[1:]
    if not args:
        print("用法: python strategy_firstboard_firstneg.py 2026-04 [2026-12]")
        sys.exit(1)

    start_month = args[0]
    end_month = args[1] if len(args) > 1 else args[0]
    detail_mode = (len(args) == 1)

    tee = Tee(LOG_PATH)
    sys.stdout = tee
    try:
        conn = sqlite3.connect(DB_PATH)
        cur = conn.cursor()

        log(f"{'='*92}")
        log("首板涨停后首阴低吸策略 - rule2 研究 (Task #88)")
        log(f"运行时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
        log(f"研究区间: {start_month} ~ {end_month}   模式: {'单月详细' if detail_mode else '区间统计'}")
        log(f"筛选逻辑: T-2首板涨停(前{FIRST_BOARD_LOOKBACK}日无涨停) + T-1首阴(未涨停且收跌) → T日买入")
        log(f"{'='*92}")

        all_days = get_all_trading_days(cur)
        scan_days = month_range_days(cur, start_month, end_month)
        if not scan_days:
            log(f"错误: 区间 {start_month}~{end_month} 无交易日数据")
            return
        # 统计月份数
        months_set = set(d[:7] for d in scan_days)
        months = len(months_set)
        log(f"扫描交易日: {len(scan_days)} 天, 覆盖 {months} 个月")

        cand_by_day, total = collect_candidates(cur, scan_days, all_days)
        log(f"候选总数: {total} 只  (日均 {total/max(1,len(scan_days)):.2f})")
        log(f"信号频率: {total/max(1,months):.1f} 笔/月, {total/max(1,months)*12:.0f} 笔/年")

        if total == 0:
            log("无候选股, 结束。")
            return

        if detail_mode:
            print_month_detail(cur, cand_by_day, all_days)

        # Step3 规律分析
        analyze_hourly_pattern(cur, cand_by_day, all_days)

        # Step4 参数网格
        results = run_grid_stats(cur, cand_by_day, all_days)
        print_grid(results, months)

        # 组合回测 (取每笔均收益 Top6 组合)
        top = results[:6]
        run_portfolio(cur, cand_by_day, scan_days, all_days, top, months)

        # 结论
        log(f"\n{'='*92}")
        log("最优配置小结 (按每笔平均收益)")
        log(f"{'='*92}")
        if results:
            best = results[0]
            sl = '无' if best['sl'] is None else f"{best['sl']*100:.0f}%"
            log(f"  买入时机: {best['entry']}  持有: T+{best['hold']}  止损: {sl}")
            log(f"  样本笔数: {best['n']}  胜率: {best['win_rate']:.1f}%  每笔均收益: {best['avg']:+.2f}%")
            log(f"  信号频率: 约 {total/max(1,months)*12:.0f} 笔/年")
            达标 = best['win_rate'] >= 55 and best['avg'] > 0
            log(f"  胜率≥55%达标: {'是' if best['win_rate']>=55 else '否'}")
        conn.close()
        log(f"\n研究完成。日志: {LOG_PATH}")
    finally:
        sys.stdout = sys.__stdout__
        tee.close()


if __name__ == '__main__':
    main()
