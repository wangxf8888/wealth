#!/usr/bin/env python3
"""
科创板弱势市场跳空高开买入 - 候选股研究脚本 (Task #66)
策略概念：
  - 板块: 科创板 sh.688xxx
  - 市值: 200亿 ~ 700亿 (amount/(turn/100))
  - 大盘弱势: 沪指(sh.000001)最近5日累计涨幅 < -1%
      沪指无数据，用备选：所有sh.60开头股票的当日close/preclose中位数近似
  - 跳空高开: (today open - yesterday close)/yesterday close >= 5%
  - 排除: ST股, 涨停开盘(open >= round(preclose*1.20, 2))
T+0合规：
  - 买入价 = today hour1_open (开盘确认跳空后买入)
  - 所有判断条件用yesterday及之前数据 + today open
  - 科创板涨跌停: ±20%
用法:
  python3 research_star_weakmkt_gapup.py 2024-09   # 单月
  python3 research_star_weakmkt_gapup.py 2024        # 全年
  python3 research_star_weakmkt_gapup.py all         # 2021-2026全量
默认: 2024-09
"""
import sys
import sqlite3
import os
import statistics
from datetime import datetime

# ============ 配置区 ============
DB_PATH = "/home/AIWealth/data/stocks.db"
LOG_PATH = "/home/AIWealth/scripts/logs/star_weakmkt_gapup_研究.log"

MKTCAP_MIN = 200e8            # 市值下限
MKTCAP_MAX = 700e8           # 市值上限
GAP_UP_THRESHOLD = 5.0       # 跳空高开阈值(%)
WEAK_MKT_5D_THRESHOLD = -1.0  # 大盘弱势: 近5日累计涨幅 < -1%
WEAK_MKT_DAYS = 5            # 大盘弱势回看天数
STAR_LIMIT_PCT = 20.0        # 科创板涨跌停幅度(%)
CONTEXT_DAYS = 5             # 前后查看天数
FUTURE_DAYS = 5             # 持仓期最长T+5
# ================================


class Tee:
    """同时输出到控制台和日志文件"""
    def __init__(self, filepath, mode='w'):
        self.file = open(filepath, mode, encoding='utf-8')
        self.stdout = sys.stdout

    def write(self, data):
        self.file.write(data)
        self.stdout.write(data)

    def flush(self):
        self.file.flush()
        self.stdout.flush()

    def close(self):
        self.file.close()


def is_st(row):
    if row['isST'] == 1:
        return True
    name = row['code_name'] or ''
    if 'ST' in name.upper():
        return True
    return False


def get_star_limit_up_price(preclose):
    """科创板涨停价 ±20%"""
    return round(preclose * 1.20, 2)


def get_all_trading_days(cursor):
    cursor.execute("SELECT DISTINCT date FROM stock_kline ORDER BY date")
    return [r[0] for r in cursor.fetchall()]


def compute_market_daily_returns(cursor, all_days):
    """
    计算每个交易日的大盘近似涨幅。
    优先用 sh.000001；无数据则用所有 sh.60 开头股票当日 close_rate 的中位数近似。
    返回 {date: daily_return_pct}
    """
    # 尝试沪指
    cursor.execute("SELECT COUNT(*) FROM stock_kline WHERE code='sh.000001'")
    has_index = cursor.fetchone()[0] > 0

    market = {}
    if has_index:
        cursor.execute("SELECT date, close_rate FROM stock_kline WHERE code='sh.000001' ORDER BY date")
        for d, cr in cursor.fetchall():
            if cr is not None:
                market[d] = cr
        return market, 'sh.000001'

    # 备选：sh.60xxx 当日 close_rate 中位数
    cursor.execute("""
        SELECT date, close_rate FROM stock_kline
        WHERE code LIKE 'sh.60%' AND close_rate IS NOT NULL
        ORDER BY date
    """)
    buckets = {}
    for d, cr in cursor.fetchall():
        buckets.setdefault(d, []).append(cr)
    for d, vals in buckets.items():
        if vals:
            market[d] = statistics.median(vals)
    return market, 'sh.60median'


def market_5d_cum(market, all_days, today):
    """
    大盘弱势判定：截止 yesterday（含）最近5个交易日累计涨幅。
    T+0合规：不使用 today 当日涨幅（未来数据）。
    """
    if today not in all_days:
        return None
    idx = all_days.index(today)
    if idx < 1:
        return None
    # yesterday 及之前，取最近 WEAK_MKT_DAYS 个交易日
    end = idx  # exclusive of today -> range up to idx-1
    start = max(0, idx - WEAK_MKT_DAYS)
    window = all_days[start:end]  # 不含today
    rets = [market[d] for d in window if d in market]
    if len(rets) < WEAK_MKT_DAYS:
        # 数据不足时用可得数据求和（早期数据兼容）
        if not rets:
            return None
    return sum(rets)


def find_candidates(cursor, today, yesterday, market, all_days):
    """找出当日满足条件的科创板弱势跳空候选股"""
    mkt5d = market_5d_cum(market, all_days, today)
    if mkt5d is None or mkt5d >= WEAK_MKT_5D_THRESHOLD:
        return [], mkt5d  # 大盘不弱势

    query = """
        SELECT
            t.date as today_date,
            t.code, t.code_name,
            t.open, t.high, t.low, t.close, t.preclose,
            t.close_rate as today_close_rate,
            t.volume, t.amount, t.turn, t.isST,
            t.hour1_open, t.hour1_high, t.hour1_low, t.hour1_close,
            t.hour2_open, t.hour2_high, t.hour2_low, t.hour2_close,
            t.hour3_open, t.hour3_high, t.hour3_low, t.hour3_close,
            t.hour4_open, t.hour4_high, t.hour4_low, t.hour4_close,
            y.date as yest_date,
            y.close as y_close, y.close_rate as y_close_rate
        FROM stock_kline t
        JOIN stock_kline y ON t.code = y.code AND y.date = ?
        WHERE t.date = ?
          AND t.code LIKE 'sh.688%'
          AND t.preclose > 0
          AND t.turn > 0
          AND t.amount > 0
          AND ((t.open - t.preclose) / t.preclose * 100) >= ?
    """
    cursor.execute(query, (yesterday, today, GAP_UP_THRESHOLD))
    columns = [desc[0] for desc in cursor.description]
    results = []
    for row in cursor.fetchall():
        d = dict(zip(columns, row))
        if is_st(d):
            continue
        # 市值过滤
        mktcap = d['amount'] / (d['turn'] / 100.0) if d['turn'] else 0
        if not (MKTCAP_MIN <= mktcap <= MKTCAP_MAX):
            continue
        # 排除涨停开盘
        limit_up = get_star_limit_up_price(d['preclose'])
        if d['open'] >= limit_up:
            continue
        d['mktcap'] = mktcap
        d['mkt5d'] = mkt5d
        results.append(d)
    return results, mkt5d


def get_context_hours(cursor, code, all_days, today_idx, n=5):
    start_idx = max(0, today_idx - n)
    end_idx = min(len(all_days) - 1, today_idx + n)
    days_range = all_days[start_idx:end_idx + 1]
    if not days_range:
        return []
    placeholders = ','.join(['?'] * len(days_range))
    cursor.execute(f"""
        SELECT date, open, high, low, close, preclose, close_rate, turn,
               hour1_open, hour1_high, hour1_low, hour1_close,
               hour2_open, hour2_high, hour2_low, hour2_close,
               hour3_open, hour3_high, hour3_low, hour3_close,
               hour4_open, hour4_high, hour4_low, hour4_close
        FROM stock_kline
        WHERE code = ? AND date IN ({placeholders})
        ORDER BY date
    """, [code] + days_range)
    columns = [desc[0] for desc in cursor.description]
    return [dict(zip(columns, row)) for row in cursor.fetchall()]


def get_buy_price(cand):
    """买入价 = today hour1_open，缺失则退回 day open"""
    h1o = cand.get('hour1_open')
    if h1o and h1o > 0:
        return h1o
    return cand['open']


def collect_future_stats(future_rows, buy_price):
    """
    按 T+1..T+FUTURE_DAYS 逐日计算最高/最低相对买入价收益。
    返回 dict: {t: {'high': pct, 'low': pct}}, 以及持仓期最大回撤（从买入价起）。
    """
    result = {}
    running_min = 0.0  # 从买入价起的最大回撤（负值）
    for i, r in enumerate(future_rows[:FUTURE_DAYS], start=1):
        h = r['high']
        l = r['low']
        hp = (h - buy_price) / buy_price * 100 if h and h > 0 else None
        lp = (l - buy_price) / buy_price * 100 if l and l > 0 else None
        result[i] = {'high': hp, 'low': lp}
        if lp is not None and lp < running_min:
            running_min = lp
    return result, running_min


def print_candidate(cand, context_rows, today):
    code = cand['code']
    name = cand['code_name'] or ''
    buy_price = get_buy_price(cand)
    gap_up_pct = (cand['open'] - cand['preclose']) / cand['preclose'] * 100
    mktcap_yi = cand['mktcap'] / 1e8

    print(f"\n--- {code} ({name}) --- 市值{mktcap_yi:.0f}亿, 昨收{cand['preclose']:.2f}, "
          f"今开{cand['open']:.2f}({gap_up_pct:+.1f}%), 大盘5日:{cand['mkt5d']:+.1f}%")
    print(f"  {'日期':<12}| {'hour':<5}| {'open':<8}| {'high':<8}| {'low':<8}| {'close':<8}| vs 买入价(h1_open={buy_price:.2f})")
    print(f"  {'-'*12}+{'-'*6}+{'-'*9}+{'-'*9}+{'-'*9}+{'-'*9}+{'-'*28}")

    for row in context_rows:
        d = row['date']
        is_buy_day = (d == today)
        for h in range(1, 5):
            ho = row.get(f'hour{h}_open')
            hh = row.get(f'hour{h}_high')
            hl = row.get(f'hour{h}_low')
            hc = row.get(f'hour{h}_close')
            if ho is None or ho == 0:
                continue
            vs_buy = (hc - buy_price) / buy_price * 100 if buy_price > 0 else 0
            tag = " ← 买入日hour1" if (h == 1 and is_buy_day) else f" {vs_buy:+.1f}%"
            print(f"  {d:<12}| h{h:<4}| {ho:<8.2f}| {hh:<8.2f}| {hl:<8.2f}| {hc:<8.2f}|{tag}")

    # 关键指标
    print()
    print(f"  关键指标:")
    print(f"    买入价(hour1 open): {buy_price:.2f}")
    today_high = cand['high']
    today_low = cand['low']
    print(f"    当日最高/最低: {today_high:.2f}({(today_high-buy_price)/buy_price*100:+.1f}%) / "
          f"{today_low:.2f}({(today_low-buy_price)/buy_price*100:+.1f}%)")

    future_rows = [r for r in context_rows if r['date'] > today]
    tstats, max_dd = collect_future_stats(future_rows, buy_price)
    for t in range(1, FUTURE_DAYS + 1):
        if t in tstats:
            hp = tstats[t]['high']
            lp = tstats[t]['low']
            hs = f"{hp:+.1f}%" if hp is not None else "N/A"
            ls = f"{lp:+.1f}%" if lp is not None else "N/A"
            print(f"    T+{t}最高: {hs}  T+{t}最低: {ls}")
    print(f"    持仓期最大回撤(从买入价起): {max_dd:+.1f}%")


def build_trade_record(cand, context_rows, today):
    """构建用于统计分析的交易记录"""
    buy_price = get_buy_price(cand)
    if buy_price <= 0:
        return None
    h1_close = cand.get('hour1_close')
    h2_open = cand.get('hour2_open')

    future_rows = [r for r in context_rows if r['date'] > today]

    rec = {
        'code': cand['code'],
        'date': today,
        'buy_price': buy_price,
        'h1_close': h1_close if (h1_close and h1_close > 0) else None,
        'h2_open': h2_open if (h2_open and h2_open > 0) else None,
        'today_high': cand['high'],
        'today_low': cand['low'],
        'today_close': cand['close'],
        # 逐日 T+1..T+5 的 high/low 绝对价，用于止盈止损模拟
        'future': [],  # list of (high, low, close)
    }
    for r in future_rows[:FUTURE_DAYS]:
        rec['future'].append((r['high'], r['low'], r['close']))
    return rec


def _ret_at_buy(price, buy):
    return (price - buy) / buy * 100 if buy > 0 else 0


def analyze_summary(records, scope_label):
    """在log末尾追加汇总分析"""
    print(f"\n\n{'='*60}")
    print(f"===== {scope_label} 候选股汇总分析 =====")
    n = len(records)
    dates = set(r['date'] for r in records)
    print(f"总候选股数: {n}只 ({len(dates)}日有信号)")
    if n == 0:
        print("无候选股数据，无法分析。")
        return

    # ---------- 买入时机分析 ----------
    print(f"\n--- 买入时机分析 ---")
    print(f"hour1 open买入 vs hour1 close买入 vs hour2 open买入:")

    def timing_stats(price_key):
        """针对不同买入价，计算平均 T+1 / T+3 收盘收益（以T+n收盘价 vs 买入价）"""
        t1s, t3s = [], []
        for r in records:
            if price_key == 'h1_open':
                bp = r['buy_price']
            elif price_key == 'h1_close':
                bp = r['h1_close']
            elif price_key == 'h2_open':
                bp = r['h2_open']
            if not bp or bp <= 0:
                continue
            fut = r['future']
            if len(fut) >= 1 and fut[0][2] and fut[0][2] > 0:
                t1s.append(_ret_at_buy(fut[0][2], bp))
            if len(fut) >= 3 and fut[2][2] and fut[2][2] > 0:
                t3s.append(_ret_at_buy(fut[2][2], bp))
        t1 = sum(t1s)/len(t1s) if t1s else 0
        t3 = sum(t3s)/len(t3s) if t3s else 0
        return t1, t3, len(t1s), len(t3s)

    for key, label in [('h1_open', 'h1_open'), ('h1_close', 'h1_close'), ('h2_open', 'h2_open')]:
        t1, t3, n1, n3 = timing_stats(key)
        print(f"  {label}: 平均T+1收益 {t1:+.2f}% (n={n1}), T+3收益 {t3:+.2f}% (n={n3})")

    # ---------- 止盈分析 ----------
    print(f"\n--- 止盈分析 ---")
    print(f"持仓期内(T+1到T+5)各股触及止盈线比例:")
    for tp in [3, 5, 8, 10]:
        hit = 0
        days_to_hit = []
        for r in records:
            bp = r['buy_price']
            hit_day = None
            for i, (h, l, c) in enumerate(r['future'], start=1):
                if h and h > 0 and _ret_at_buy(h, bp) >= tp:
                    hit_day = i
                    break
            if hit_day:
                hit += 1
                days_to_hit.append(hit_day)
        ratio = hit / n * 100
        avg_days = sum(days_to_hit)/len(days_to_hit) if days_to_hit else 0
        print(f"  +{tp}%止盈: {ratio:.1f}% 触及, 平均持仓{avg_days:.1f}天达到")

    # ---------- 止损分析 ----------
    print(f"\n--- 止损分析 ---")
    print(f"持仓期内(T+1到T+5)各股最大回撤分布:")
    dd_list = []
    for r in records:
        bp = r['buy_price']
        mn = 0.0
        for (h, l, c) in r['future']:
            if l and l > 0:
                lp = _ret_at_buy(l, bp)
                if lp < mn:
                    mn = lp
        dd_list.append(mn)
    for th in [-3, -5, -8]:
        cnt = sum(1 for d in dd_list if d < th)
        print(f"  最大回撤<{th}%: {cnt/n*100:.1f}%的交易")

    # -5%止损模拟对比
    sl = -5.0
    stopped = 0
    stop_results = []   # 止损后结果(=-5%)
    hold_results = []   # 不止损: T+5收盘收益
    for r in records:
        bp = r['buy_price']
        hit_stop = False
        for (h, l, c) in r['future']:
            if l and l > 0 and _ret_at_buy(l, bp) <= sl:
                hit_stop = True
                break
        # 不止损持有到T+5收盘
        final = None
        for (h, l, c) in reversed(r['future']):
            if c and c > 0:
                final = _ret_at_buy(c, bp)
                break
        if final is None:
            final = 0
        hold_results.append(final)
        if hit_stop:
            stopped += 1
            stop_results.append(sl)
        else:
            stop_results.append(final)
    stop_ratio = stopped / n * 100
    avg_stop = sum(stop_results)/len(stop_results) if stop_results else 0
    avg_hold = sum(hold_results)/len(hold_results) if hold_results else 0
    print(f"  如果设-5%止损: 止损比例{stop_ratio:.1f}%, "
          f"止损策略平均结果 {avg_stop:+.2f}% vs 不止损持有到T+5 {avg_hold:+.2f}%")

    # ---------- 最优配置候选 ----------
    print(f"\n--- 最优配置候选 ---")
    configs = [
        ('A', '+5%止盈, -3%止损, 最长T+3', 5, -3, 3),
        ('B', '+8%止盈, -5%止损, 最长T+5', 8, -5, 5),
        ('C', '+10%止盈, 无止损, 最长T+5', 10, None, 5),
    ]
    best = None
    for tag, desc, tp, slv, maxt in configs:
        ret, wr = simulate_config(records, tp, slv, maxt)
        print(f"配置{tag}: h1_open买入, {desc}")
        print(f"  模拟收益(每笔均值): {ret:+.2f}%, 胜率: {wr:.1f}%")
        if best is None or ret > best[1]:
            best = (tag, ret, wr, desc)
    if best:
        # 估算月化：假设平均持仓约3天，每月约20交易日 -> 约6.7次循环
        print(f"\n  >> 本区间最优: 配置{best[0]} ({best[3]}), 单笔均值{best[1]:+.2f}%, 胜率{best[2]:.1f}%")

    # ---------- 逐年验证（样本跨多年时） ----------
    years = sorted(set(r['date'][:4] for r in records))
    if len(years) > 1:
        print(f"\n--- 逐年验证 (配置B: +8%止盈/-5%止损/最长T+5) ---")
        for y in years:
            yrecs = [r for r in records if r['date'].startswith(y)]
            ret, wr = simulate_config(yrecs, 8, -5, 5)
            retA, wrA = simulate_config(yrecs, 5, -3, 3)
            print(f"  {y}: 样本{len(yrecs):>3}笔 | 配置B 单笔{ret:+.2f}% 胜率{wr:.1f}% "
                  f"| 配置A 单笔{retA:+.2f}% 胜率{wrA:.1f}%")


def simulate_config(records, tp, sl, max_t):
    """
    模拟单笔交易：hour1_open买入，持仓期内逐日检查。
    - 若某日最高触及止盈线 tp -> 以 tp 收益卖出
    - 否则若某日最低触及止损线 sl -> 以 sl 收益卖出（止盈优先判定）
    - 到 max_t 仍未触发 -> 以该日收盘价收益卖出
    返回 (平均收益%, 胜率%)
    """
    rets = []
    for r in records:
        bp = r['buy_price']
        fut = r['future'][:max_t]
        outcome = None
        for (h, l, c) in fut:
            hp = _ret_at_buy(h, bp) if (h and h > 0) else None
            lp = _ret_at_buy(l, bp) if (l and l > 0) else None
            # 止盈优先（保守假设日内先冲高）
            if hp is not None and hp >= tp:
                outcome = tp
                break
            if sl is not None and lp is not None and lp <= sl:
                outcome = sl
                break
        if outcome is None:
            # 到期收盘卖出
            final = None
            for (h, l, c) in reversed(fut):
                if c and c > 0:
                    final = _ret_at_buy(c, bp)
                    break
            outcome = final if final is not None else 0
        rets.append(outcome)
    if not rets:
        return 0, 0
    avg = sum(rets) / len(rets)
    wr = sum(1 for x in rets if x > 0) / len(rets) * 100
    return avg, wr


def resolve_scope(arg):
    """解析命令行参数 -> (like_pattern列表, 标签)"""
    if arg == 'all':
        return [f"{y}-%" for y in range(2021, 2027)], "2021-2026全量"
    if len(arg) == 4 and arg.isdigit():
        return [f"{arg}-%"], f"{arg}全年"
    # 单月 YYYY-MM
    return [f"{arg}%"], arg


def main():
    arg = sys.argv[1] if len(sys.argv) >= 2 else "2024-09"
    patterns, scope_label = resolve_scope(arg)

    os.makedirs(os.path.dirname(LOG_PATH), exist_ok=True)
    tee = Tee(LOG_PATH, 'w')
    old_stdout = sys.stdout
    sys.stdout = tee

    try:
        print("=" * 60)
        print("科创板弱势市场跳空高开买入 - 候选股研究 (Task #66)")
        print(f"参数范围: {scope_label}")
        print(f"条件: 板块=sh.688, 市值[{MKTCAP_MIN/1e8:.0f},{MKTCAP_MAX/1e8:.0f}]亿, "
              f"大盘近{WEAK_MKT_DAYS}日累计<{WEAK_MKT_5D_THRESHOLD}%, 跳空>={GAP_UP_THRESHOLD}%")
        print(f"排除: ST, 涨停开盘(open>=round(preclose*1.20,2)); 买入价=hour1_open")
        print("=" * 60)

        conn = sqlite3.connect(DB_PATH)
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()

        all_days = get_all_trading_days(cursor)
        market, mkt_source = compute_market_daily_returns(cursor, all_days)
        print(f"大盘指标来源: {mkt_source}")

        # 收集范围内交易日
        scope_days = []
        for d in all_days:
            for p in patterns:
                pref = p.rstrip('%')
                if d.startswith(pref):
                    scope_days.append(d)
                    break
        print(f"范围内交易日数: {len(scope_days)}")
        print()

        records = []
        for today in scope_days:
            idx = all_days.index(today)
            if idx <= 0:
                continue
            yesterday = all_days[idx - 1]
            candidates, mkt5d = find_candidates(cursor, today, yesterday, market, all_days)
            if not candidates:
                continue
            print(f"\n{'='*10} {today} {'='*10}")
            m5 = f"{mkt5d:+.1f}%" if mkt5d is not None else "N/A"
            print(f"候选股: {len(candidates)}只 (大盘5日累计{m5})")
            for cand in candidates:
                context_rows = get_context_hours(cursor, cand['code'], all_days, idx, CONTEXT_DAYS)
                print_candidate(cand, context_rows, today)
                rec = build_trade_record(cand, context_rows, today)
                if rec:
                    records.append(rec)

        analyze_summary(records, scope_label)
        conn.close()
        print(f"\n完成。日志已保存至: {LOG_PATH}")
    finally:
        sys.stdout = old_stdout
        tee.close()


if __name__ == "__main__":
    main()
