#!/usr/bin/env python3
"""
连续温和放量小阳线蓄力策略 (Gradual Accumulation) - Task #99

策略思路：
  连续3天以上每天小幅上涨(+0.5%~+3%)且成交量逐日递增(放量吸筹)。
  这是资金逐步建仓的"慢牛"形态，预示可能加速拉升。
  与"放量突破"不同，这里强调渐进式放量（每天比前一天多），而非突然放大。

选股条件(在T日筛选，全部使用T-1及之前数据，无未来函数)：
  - 前3日(T-3,T-2,T-1)每天都是小阳线: close_rate ∈ [+0.5%, +3%]
  - 前3日成交量递增: volume(T-1) > volume(T-2) > volume(T-3)
  - 创业板(sz.30开头)
  - 市值50-300亿: mcap = amount/(turn/100)/1e8 (用T-1数据)
  - 排除ST、排除涨停(T-1不能涨停)
  - T日为候选买入日

T+1交易合规：
  - 默认买入价 = T日 hour1_open (次日开盘无关，这里T日即买入日，用当日hour1开盘)
  - 确认变体：要求T日hour1收阳，用hour2_open买入(仅用hour1及之前信号)
  - 卖出：持有T+1/T+2/T+3 收盘，或触发止盈/止损

用法：
  python strategy_gradual_accumulation.py 2026-04              # 单月研究(打印明细)
  python strategy_gradual_accumulation.py range 2021-01 2026-06  # 全周期回测(仅统计)
"""
import sys
import sqlite3
from collections import defaultdict

# ==================== 配置区 ====================
DB_PATH = '/home/AIWealth/data/stocks.db'

# 选股条件
SMALL_YANG_MIN = 0.5      # 小阳线 close_rate 下限(%)
SMALL_YANG_MAX = 3.0      # 小阳线 close_rate 上限(%)
CONSEC_DAYS = 3           # 连续小阳线天数
MCAP_MIN = 50.0           # 流通市值下限(亿)
MCAP_MAX = 300.0          # 流通市值上限(亿)
BOARD_PREFIX = 'sz.30'    # 创业板

MAX_CANDIDATES_PER_DAY = 6  # 单月模式每天最多打印明细数

# 回测持仓槽位(全周期模式)
N_SLOTS = 5               # 同时持仓数
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


def is_limit_up(close, preclose, code):
    """涨停判定：round(close/preclose,2)比值法 + 价格法双重"""
    if close is None or preclose is None or preclose <= 0:
        return False
    return close >= calc_limit_up(preclose, code) - 1e-6


def get_trading_days(cur, month_str):
    cur.execute("""
        SELECT DISTINCT date FROM stock_kline
        WHERE date LIKE ? ORDER BY date
    """, (month_str + '%',))
    return [r[0] for r in cur.fetchall()]


def get_all_trading_days(cur):
    cur.execute("SELECT DISTINCT date FROM stock_kline ORDER BY date")
    return [r[0] for r in cur.fetchall()]


def find_candidates(cur, today, all_days):
    """
    在T日筛选候选股。使用T-1,T-2,T-3三天的日线数据(均在T日之前)。
    返回候选列表(含T日hour数据用于买入)。
    """
    try:
        idx = all_days.index(today)
    except ValueError:
        return []
    if idx < CONSEC_DAYS:
        return []

    t1 = all_days[idx - 1]   # T-1
    t2 = all_days[idx - 2]   # T-2
    t3 = all_days[idx - 3]   # T-3

    # 一次性拉取T-1/T-2/T-3三天所有创业板股票日线
    cur.execute("""
        SELECT date, code, code_name, close_rate, volume, amount, turn, isST,
               close, preclose
        FROM stock_kline
        WHERE date IN (?,?,?) AND code LIKE ?
    """, (t1, t2, t3, BOARD_PREFIX + '%'))
    rows = cur.fetchall()

    # 按code聚合
    by_code = defaultdict(dict)
    for r in rows:
        (date, code, code_name, close_rate, volume, amount, turn, isST,
         close, preclose) = r
        by_code[code][date] = {
            'code_name': code_name, 'close_rate': close_rate,
            'volume': volume, 'amount': amount, 'turn': turn,
            'isST': isST, 'close': close, 'preclose': preclose,
        }

    # 拉取T日hour数据(买入用)
    cur.execute("""
        SELECT code, open,
               hour1_open, hour1_high, hour1_low, hour1_close,
               hour2_open, hour2_high, hour2_low, hour2_close
        FROM stock_kline WHERE date = ? AND code LIKE ?
    """, (today, BOARD_PREFIX + '%'))
    today_map = {r[0]: r for r in cur.fetchall()}

    candidates = []
    for code, days in by_code.items():
        if t1 not in days or t2 not in days or t3 not in days:
            continue
        d1, d2, d3 = days[t1], days[t2], days[t3]

        # 排除ST
        if d1['isST'] or (d1['code_name'] and 'ST' in d1['code_name'].upper()):
            continue

        # 三日均为小阳线
        crs = [d3['close_rate'], d2['close_rate'], d1['close_rate']]
        if any(cr is None for cr in crs):
            continue
        if not all(SMALL_YANG_MIN <= cr <= SMALL_YANG_MAX for cr in crs):
            continue

        # 成交量递增 vol(T-1)>vol(T-2)>vol(T-3)
        v1, v2, v3 = d1['volume'], d2['volume'], d3['volume']
        if None in (v1, v2, v3) or v3 <= 0:
            continue
        if not (v1 > v2 > v3):
            continue

        # 排除T-1涨停(涨停无法追)
        if is_limit_up(d1['close'], d1['preclose'], code):
            continue

        # 市值(用T-1) mcap = amount/(turn/100)/1e8
        amt, turn = d1['amount'], d1['turn']
        if amt is None or turn is None or turn <= 0:
            continue
        mcap = amt / (turn / 100.0) / 1e8
        if not (MCAP_MIN <= mcap <= MCAP_MAX):
            continue

        # T日买入数据
        td = today_map.get(code)
        if td is None:
            continue
        t_open, h1o, h1h, h1l, h1c, h2o = (
            td[1], td[2], td[3], td[4], td[5], td[6])
        if h1o is None or h1o <= 0:
            continue

        vol_ratio = v1 / v3 if v3 > 0 else 0
        candidates.append({
            'code': code,
            'code_name': d1['code_name'],
            'today': today,
            't1': t1, 't2': t2, 't3': t3,
            'crs': crs,             # [T-3,T-2,T-1] close_rate
            'vols': [v3, v2, v1],   # [T-3,T-2,T-1] volume
            'vol_ratio': vol_ratio,
            'mcap': mcap,
            't_open': t_open,
            'h1_open': h1o, 'h1_high': h1h, 'h1_low': h1l, 'h1_close': h1c,
            'h2_open': h2o,
        })

    candidates.sort(key=lambda x: x['vol_ratio'], reverse=True)
    return candidates


def get_daily_rows(cur, code, days_list):
    if not days_list:
        return {}
    ph = ','.join(['?'] * len(days_list))
    cur.execute(f"""
        SELECT date, open, high, low, close, preclose
        FROM stock_kline WHERE code = ? AND date IN ({ph}) ORDER BY date
    """, [code] + days_list)
    return {r[0]: r for r in cur.fetchall()}


def simulate_trade(cur, cand, all_days, buy_hour, hold_days,
                   stop_profit=None, stop_loss=None):
    """
    模拟单笔交易，返回收益率(%)或None。
    buy_hour: 'h1' 用T日hour1_open买入; 'h2_confirm' 要求hour1收阳后用hour2_open买入
    hold_days: 持有天数(卖出于 T+hold_days 收盘)
    stop_profit/stop_loss: 盘中止盈/止损百分比(基于日内high/low近似, 用日线)
    """
    code = cand['code']
    today = cand['today']
    try:
        idx = all_days.index(today)
    except ValueError:
        return None

    # 买入价
    if buy_hour == 'h1':
        buy_price = cand['h1_open']
    elif buy_hour == 'h2_confirm':
        h1o, h1c = cand['h1_open'], cand['h1_close']
        if h1o is None or h1c is None or h1c < h1o:
            return None  # hour1未收阳，不买
        buy_price = cand['h2_open']
    else:
        return None
    if buy_price is None or buy_price <= 0:
        return None

    # 卖出窗口：T+1 .. T+hold_days
    sell_days = all_days[idx + 1: idx + 1 + hold_days]
    if len(sell_days) < hold_days:
        return None  # 数据不足(尾部)

    drows = get_daily_rows(cur, code, sell_days)

    for i, d in enumerate(sell_days):
        row = drows.get(d)
        if row is None:
            continue
        _, o, hi, lo, cl, pc = row
        if cl is None:
            continue
        # 盘中止损(近似：当日最低触发)
        if stop_loss is not None and lo is not None:
            if (lo - buy_price) / buy_price * 100 <= -stop_loss:
                return -stop_loss
        # 盘中止盈(近似：当日最高触发)
        if stop_profit is not None and hi is not None:
            if (hi - buy_price) / buy_price * 100 >= stop_profit:
                return stop_profit
        # 到期收盘卖出
        if i == len(sell_days) - 1:
            return (cl - buy_price) / buy_price * 100
    return None


def format_hour_table(cur, code, window_days, buy_price, today, t1):
    """打印T-5到T+5 hour级明细(相对T日open的rate)"""
    ph = ','.join(['?'] * len(window_days))
    cur.execute(f"""
        SELECT date, close_rate,
               hour1_open, hour1_high, hour1_low, hour1_close,
               hour2_open, hour2_high, hour2_low, hour2_close,
               hour3_open, hour3_high, hour3_low, hour3_close,
               hour4_open, hour4_high, hour4_low, hour4_close
        FROM stock_kline WHERE code = ? AND date IN ({ph}) ORDER BY date
    """, [code] + window_days)
    rows = cur.fetchall()

    lines = []
    lines.append(f"  {'日期':<12}| {'hour':<5}| {'open%':>8}| {'high%':>8}| {'low%':>8}| {'close%':>8}| 标记")
    lines.append(f"  {'-'*12}+{'-'*6}+{'-'*9}+{'-'*9}+{'-'*9}+{'-'*9}+{'-'*10}")
    for row in rows:
        date = row[0]
        hours = [
            ('h1', row[2], row[3], row[4], row[5]),
            ('h2', row[6], row[7], row[8], row[9]),
            ('h3', row[10], row[11], row[12], row[13]),
            ('h4', row[14], row[15], row[16], row[17]),
        ]
        for hn, ho, hh, hl, hc in hours:
            if ho is None or hc is None:
                continue
            def r(v):
                return (v - buy_price) / buy_price * 100 if buy_price else 0
            marker = ""
            if date == today and hn == 'h1':
                marker = " ← T日买入(h1开盘)"
            elif date == t1 and hn == 'h4':
                marker = " ← T-1(信号末日)"
            lines.append(
                f"  {date:<12}| {hn:<5}| {r(ho):>+7.2f} | {r(hh):>+7.2f} | "
                f"{r(hl):>+7.2f} | {r(hc):>+7.2f} |{marker}")
    return '\n'.join(lines)


# 回测配置矩阵
CONFIGS = [
    ('h1买入-持有T+1', 'h1', 1, None, None),
    ('h1买入-持有T+2', 'h1', 2, None, None),
    ('h1买入-持有T+3', 'h1', 3, None, None),
    ('h1买入-T+2-止盈8止损5', 'h1', 2, 8.0, 5.0),
    ('h1买入-T+3-止盈10止损6', 'h1', 3, 10.0, 6.0),
    ('h2确认(h1阳)-持有T+2', 'h2_confirm', 2, None, None),
    ('h2确认(h1阳)-T+2-止盈8止损5', 'h2_confirm', 2, 8.0, 5.0),
]


def run_month(cur, month_str, all_days, verbose=True):
    """单月研究：打印候选明细 + 统计各配置"""
    month_days = get_trading_days(cur, month_str)
    if not month_days:
        return None

    # 各配置累积交易收益
    cfg_trades = {c[0]: [] for c in CONFIGS}
    total_candidates = 0

    for today in month_days:
        cands = find_candidates(cur, today, all_days)
        total_candidates += len(cands)

        if verbose:
            print(f"\n{'='*64}")
            print(f"{'='*12} {today}  候选股: {len(cands)}只 {'='*12}")
            print(f"{'='*64}")

        # 打印明细
        if verbose and cands:
            idx = all_days.index(today)
            for c in cands[:MAX_CANDIDATES_PER_DAY]:
                crs = c['crs']
                vols = c['vols']
                print(f"\n--- {c['code']} ({c['code_name'] or 'N/A'}) 市值{c['mcap']:.0f}亿 ---")
                print(f"  小阳线(close_rate) T-3/T-2/T-1: "
                      f"{crs[0]:+.2f}% / {crs[1]:+.2f}% / {crs[2]:+.2f}%")
                print(f"  成交量(手) T-3/T-2/T-1: "
                      f"{vols[0]/100:.0f} < {vols[1]/100:.0f} < {vols[2]/100:.0f} "
                      f"(放量比{c['vol_ratio']:.2f}x)")
                print(f"  T日买入价(h1开盘): {c['h1_open']:.2f}")
                prev5 = max(0, idx - 5)
                nxt5 = min(len(all_days), idx + 6)
                window = all_days[prev5:nxt5]
                print(format_hour_table(cur, c['code'], window,
                                        c['h1_open'], today, c['t1']))

        # 统计所有候选(不止打印的)
        for c in cands:
            for name, bh, hd, sp, sl in CONFIGS:
                ret = simulate_trade(cur, c, all_days, bh, hd, sp, sl)
                if ret is not None:
                    cfg_trades[name].append(ret)

    # 月度汇总
    print(f"\n\n{'='*72}")
    print(f"{'='*15} {month_str} 各配置统计 (候选总数:{total_candidates}) {'='*15}")
    print(f"{'='*72}")
    print(f"{'配置':<32}{'笔数':>6}{'均收益%':>10}{'胜率%':>9}{'月化%*':>10}")
    print(f"{'-'*72}")
    results = {}
    for name, _, _, _, _ in CONFIGS:
        trades = cfg_trades[name]
        if not trades:
            print(f"{name:<32}{'0':>6}{'N/A':>10}")
            continue
        n = len(trades)
        avg = sum(trades) / n
        win = sum(1 for t in trades if t > 0) / n * 100
        # 月化估算：n笔/该月，按N_SLOTS并发，每笔平均avg
        # 简化：月化 ≈ avg * (n / N_SLOTS)  (资金周转近似)
        turns = n / N_SLOTS
        monthly = avg * turns
        results[name] = {'n': n, 'avg': avg, 'win': win, 'monthly': monthly}
        print(f"{name:<32}{n:>6}{avg:>+9.2f} {win:>8.1f} {monthly:>+9.1f}")
    print(f"{'-'*72}")
    print(f"* 月化估算 = 均收益 × (笔数/{N_SLOTS}槽位), 仅粗略参考")
    return results


def run_range(cur, start_month, end_month, all_days):
    """全周期回测：跨月聚合各配置的交易统计"""
    # 生成月份列表
    def month_iter(s, e):
        sy, sm = int(s[:4]), int(s[5:7])
        ey, em = int(e[:4]), int(e[5:7])
        cur_y, cur_m = sy, sm
        while (cur_y, cur_m) <= (ey, em):
            yield f"{cur_y:04d}-{cur_m:02d}"
            cur_m += 1
            if cur_m > 12:
                cur_m = 1
                cur_y += 1

    months = list(month_iter(start_month, end_month))
    cfg_trades = {c[0]: [] for c in CONFIGS}
    total_candidates = 0

    for m in months:
        month_days = get_trading_days(cur, m)
        if not month_days:
            continue
        for today in month_days:
            cands = find_candidates(cur, today, all_days)
            total_candidates += len(cands)
            for c in cands:
                for name, bh, hd, sp, sl in CONFIGS:
                    ret = simulate_trade(cur, c, all_days, bh, hd, sp, sl)
                    if ret is not None:
                        cfg_trades[name].append(ret)

    n_months = len(months)
    print(f"\n{'='*78}")
    print(f"{'='*12} 全周期 {start_month} ~ {end_month} ({n_months}个月) 回测 {'='*12}")
    print(f"{'='*78}")
    print(f"候选总数(去重前累计): {total_candidates}")
    print(f"\n{'配置':<32}{'笔数':>7}{'均收益%':>10}{'胜率%':>9}{'月化%*':>10}{'年化%*':>11}")
    print(f"{'-'*78}")
    for name, _, _, _, _ in CONFIGS:
        trades = cfg_trades[name]
        if not trades:
            print(f"{name:<32}{'0':>7}")
            continue
        n = len(trades)
        avg = sum(trades) / n
        win = sum(1 for t in trades if t > 0) / n * 100
        # 月化：总笔数分摊到月，按N_SLOTS并发
        trades_per_month = n / n_months
        turns_per_month = trades_per_month / N_SLOTS
        monthly = avg * turns_per_month
        yearly = ((1 + monthly / 100) ** 12 - 1) * 100
        print(f"{name:<32}{n:>7}{avg:>+9.2f} {win:>8.1f} {monthly:>+9.1f} {yearly:>+10.1f}")
    print(f"{'-'*78}")
    print(f"* 月化 = 均收益 × (月均笔数/{N_SLOTS}槽位); 年化 = (1+月化)^12-1")


def main():
    if len(sys.argv) < 2:
        print("用法:")
        print("  python strategy_gradual_accumulation.py 2026-04")
        print("  python strategy_gradual_accumulation.py range 2021-01 2026-06")
        sys.exit(1)

    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    all_days = get_all_trading_days(cur)

    print(f"{'='*72}")
    print(f"连续温和放量小阳线蓄力策略 (Gradual Accumulation) - Task #99")
    print(f"选股: 前3日小阳线[{SMALL_YANG_MIN}%,{SMALL_YANG_MAX}%] + 量递增 + 创业板 "
          f"+ 市值[{MCAP_MIN},{MCAP_MAX}]亿 + 排除ST/涨停")
    print(f"{'='*72}")

    if sys.argv[1] == 'range':
        if len(sys.argv) < 4:
            print("range模式需要: range 起始月 结束月")
            sys.exit(1)
        run_range(cur, sys.argv[2], sys.argv[3], all_days)
    else:
        run_month(cur, sys.argv[1], all_days, verbose=True)

    conn.close()
    print(f"\n{'='*72}")
    print("研究完成。")


if __name__ == '__main__':
    main()
