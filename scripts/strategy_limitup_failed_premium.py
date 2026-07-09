#!/usr/bin/env python3
"""
涨停次日大幅低开(溢价失败)反弹策略 - 候选股研究脚本 [Task #91]

策略思路:
  - 昨日(T-1)涨停
  - 今日(T)大幅低开: open_rate = (open - 昨close)/昨close*100 <= -5%
    (隔夜利空/情绪急转, 恐慌盘开盘集中抛售)
  - 若 hour1 跌幅收窄 或 hour2 开始反弹, 可能有超跌修复机会
  - 重点关注创业板(sz.30x)/科创板(sh.688), 振幅空间20% > 主板10%

rule2 合规要点:
  - T+1: 当日买入不可卖出, 故 T 日买入最早 T+1 卖出
  - 不使用未来数据: hour1买入只用 hour1_open + 昨日之前信号;
    hour2买入用 hour2_open + hour1 + 昨日之前信号
  - 排除一字跌停: 若 today open == 跌停价 则无法在 open 买入, 排除

买点方案对比:
  A. T日 hour1_open 买入 (直接抄底大幅低开)
  B. T日 hour2_open 买入 (hour1企稳确认: h1_close>=h1_open 或 h1不破新低)
  C. T+1 open 买入 (观察T日全天企稳)

用法:
  python strategy_limitup_failed_premium.py 2026-04            # 单月
  python strategy_limitup_failed_premium.py 2021-01 2026-12    # 多年区间(仅汇总)
"""
import sys
import sqlite3
from collections import defaultdict

DB_PATH = '/home/AIWealth/data/stocks.db'
LOW_OPEN_THRESHOLD = -5.0        # 大幅低开阈值(%), open_rate <= 此值
MAX_CANDIDATES_DETAIL = 4        # 每天最多打印明细的候选股数
DETAIL_MONTHS_LIMIT = 1          # 仅当扫描月份数 <= 此值时打印逐股hour明细

# 低开幅度分组(用于分组统计)
GAP_GROUPS = [
    ('-5%~-8%',  -8.0, -5.0),
    ('-8%~-12%', -12.0, -8.0),
    ('<-12%',    -100.0, -12.0),
]


def get_limit_ratio(code):
    """根据股票代码确定涨跌幅比例"""
    if code.startswith('sz.300') or code.startswith('sz.301'):
        return 0.20  # 创业板
    elif code.startswith('sh.688'):
        return 0.20  # 科创板
    elif code.startswith('bj.'):
        return 0.30  # 北交所
    else:
        return 0.10  # 主板


def calc_limit_up(preclose, code):
    return round(preclose * (1 + get_limit_ratio(code)), 2)


def calc_limit_down(preclose, code):
    return round(preclose * (1 - get_limit_ratio(code)), 2)


def is_limit_up(close, preclose, code):
    """判断是否涨停(严格: close >= round(preclose*(1+ratio),2))"""
    if preclose is None or preclose <= 0 or close is None:
        return False
    return close >= calc_limit_up(preclose, code)


def is_target_board(code):
    """仅创业板(sz.30x) / 科创板(sh.688)"""
    return code.startswith('sz.300') or code.startswith('sz.301') or code.startswith('sh.688')


def get_trading_days_range(cur, start_month, end_month):
    """获取 [start_month, end_month] 区间内所有交易日 (YYYY-MM)"""
    cur.execute("""
        SELECT DISTINCT date FROM stock_kline
        WHERE substr(date,1,7) >= ? AND substr(date,1,7) <= ?
        ORDER BY date
    """, (start_month, end_month))
    return [r[0] for r in cur.fetchall()]


def get_all_trading_days(cur):
    cur.execute("SELECT DISTINCT date FROM stock_kline ORDER BY date")
    return [r[0] for r in cur.fetchall()]


def find_candidates(cur, today, yesterday):
    """
    候选股筛选:
      1. 昨日(T-1)涨停
      2. 今日(T)大幅低开 open_rate <= LOW_OPEN_THRESHOLD
      3. 创业板/科创板
      4. 非ST
      5. 排除一字跌停(open == 跌停价, 无法在open买入)
    """
    # 昨日涨停股 (仅目标板块)
    cur.execute("""
        SELECT code, close, preclose FROM stock_kline
        WHERE date = ? AND preclose > 0
    """, (yesterday,))
    yd_map = {}
    for code, close, preclose in cur.fetchall():
        if not is_target_board(code):
            continue
        if is_limit_up(close, preclose, code):
            yd_map[code] = (close, preclose)

    if not yd_map:
        return []

    # 今日数据
    cur.execute("""
        SELECT code, code_name, open, high, low, close, preclose, isST,
               turn,
               hour1_open, hour1_high, hour1_low, hour1_close,
               hour2_open, hour2_high, hour2_low, hour2_close,
               hour3_open, hour3_high, hour3_low, hour3_close,
               hour4_open, hour4_high, hour4_low, hour4_close
        FROM stock_kline WHERE date = ?
    """, (today,))
    candidates = []
    for r in cur.fetchall():
        code = r[0]
        if code not in yd_map:
            continue
        code_name = r[1]
        t_open, t_high, t_low, t_close, t_preclose, t_isST, t_turn = \
            r[2], r[3], r[4], r[5], r[6], r[7], r[8]

        # 排除ST
        if t_isST:
            continue
        if code_name and 'ST' in code_name.upper():
            continue
        # 数据有效性
        if t_preclose is None or t_preclose <= 0 or t_open is None or t_open <= 0:
            continue

        # 大幅低开判定
        open_rate = (t_open - t_preclose) / t_preclose * 100
        if open_rate > LOW_OPEN_THRESHOLD:
            continue

        # 排除一字跌停(open即跌停价, 无法买入)
        limit_down = calc_limit_down(t_preclose, code)
        if t_open <= limit_down:
            continue

        yd_close, yd_preclose = yd_map[code]
        candidates.append({
            'code': code,
            'code_name': code_name,
            'today': today,
            'yd_close': yd_close,
            'yd_preclose': yd_preclose,
            't_open': t_open,
            't_high': t_high,
            't_low': t_low,
            't_close': t_close,
            't_preclose': t_preclose,
            't_turn': t_turn,
            'open_rate': open_rate,
            'hour_today': {
                'h1': (r[9], r[10], r[11], r[12]),
                'h2': (r[13], r[14], r[15], r[16]),
                'h3': (r[17], r[18], r[19], r[20]),
                'h4': (r[21], r[22], r[23], r[24]),
            }
        })

    # 按低开幅度排序(最深低开在前)
    candidates.sort(key=lambda x: x['open_rate'])
    return candidates


def get_hour_data_for_days(cur, code, days_list):
    if not days_list:
        return []
    placeholders = ','.join(['?'] * len(days_list))
    cur.execute(f"""
        SELECT date, open, close, preclose, turn,
               hour1_open, hour1_high, hour1_low, hour1_close,
               hour2_open, hour2_high, hour2_low, hour2_close,
               hour3_open, hour3_high, hour3_low, hour3_close,
               hour4_open, hour4_high, hour4_low, hour4_close
        FROM stock_kline WHERE code = ? AND date IN ({placeholders})
        ORDER BY date
    """, [code] + days_list)
    return cur.fetchall()


def format_hour_table(hour_rows, signal_date):
    """打印 T-5..T+5 hour级OCHL, 涨跌幅相对该hour的open"""
    lines = []
    lines.append(f"  {'日期':<12}|{'H':<4}|{'open':>8}|{'high':>8}|{'low':>8}|{'close':>8}| hour涨跌  换手")
    lines.append(f"  {'-'*12}+{'-'*4}+{'-'*8}+{'-'*8}+{'-'*8}+{'-'*8}+{'-'*16}")
    for row in hour_rows:
        date = row[0]
        turn = row[4]
        hours = [
            ('h1', row[5], row[6], row[7], row[8]),
            ('h2', row[9], row[10], row[11], row[12]),
            ('h3', row[13], row[14], row[15], row[16]),
            ('h4', row[17], row[18], row[19], row[20]),
        ]
        mark_day = "★" if date == signal_date else " "
        for h_name, ho, hh, hl, hc in hours:
            if ho is None or hc is None:
                continue
            pct = (hc - ho) / ho * 100 if ho and ho > 0 else 0
            turn_str = f"{turn:.1f}%" if (h_name == 'h1' and turn is not None) else ""
            lines.append(
                f" {mark_day}{date:<11}|{h_name:<4}|{ho:>8.2f}|{hh:>8.2f}|"
                f"{hl:>8.2f}|{hc:>8.2f}| {pct:+6.2f}%  {turn_str}"
            )
    return '\n'.join(lines)


def forward_returns(cur, code, buy_date, buy_price, all_days):
    """
    计算从 buy_date 买入(buy_price)后的前向收益 (T+1 起可卖).
    返回: T日close(纸面), T+1 open/high/close, T+2 close, 5日内max/min 相对买入价的涨跌幅%
    仅使用买入日及之后数据(其中卖出至少T+1, 合规).
    """
    if buy_price is None or buy_price <= 0:
        return None
    try:
        idx = all_days.index(buy_date)
    except ValueError:
        return None
    window = all_days[idx:idx + 7]  # 买入日 + 后6日
    if len(window) < 2:
        return None
    placeholders = ','.join(['?'] * len(window))
    cur.execute(f"""
        SELECT date, open, high, low, close
        FROM stock_kline WHERE code = ? AND date IN ({placeholders})
        ORDER BY date
    """, [code] + window)
    rows = cur.fetchall()
    if len(rows) < 2:
        return None

    def pct(p):
        return (p - buy_price) / buy_price * 100 if (p is not None and buy_price > 0) else None

    m = {}
    m['t_close_pct'] = pct(rows[0][4]) if rows[0][0] == buy_date else None  # 纸面, 不可卖
    # T+1
    n1 = rows[1]
    m['t1_open_pct'] = pct(n1[1])
    m['t1_high_pct'] = pct(n1[2])
    m['t1_close_pct'] = pct(n1[4])
    # T+2
    if len(rows) >= 3:
        m['t2_close_pct'] = pct(rows[2][4])
    # 5日内(T+1..T+5)可卖区间最高/最低
    sellable = rows[1:6]
    highs = [r[2] for r in sellable if r[2] is not None]
    lows = [r[3] for r in sellable if r[3] is not None]
    if highs:
        m['max5_pct'] = pct(max(highs))
    if lows:
        m['min5_pct'] = pct(min(lows))
    return m


def analyze_buy_scenarios(cur, c, all_days):
    """
    对单个候选股计算三种买点的前向收益:
      A. T日 hour1_open 买入
      B. T日 hour2_open 买入 (需 hour1 企稳: h1_close>=h1_open)
      C. T+1 open 买入
    """
    code = c['code']
    today = c['today']
    h1 = c['hour_today']['h1']
    h2 = c['hour_today']['h2']
    h1_open, h1_high, h1_low, h1_close = h1
    h2_open = h2[0]

    res = {}

    # A. hour1_open 买入
    if h1_open and h1_open > 0:
        res['A'] = {
            'buy_price': h1_open,
            'metrics': forward_returns(cur, code, today, h1_open, all_days),
        }

    # B. hour2_open 买入 (hour1收阳企稳确认)
    h1_stabilized = (h1_open and h1_close and h1_close >= h1_open)
    if h2_open and h2_open > 0:
        res['B'] = {
            'buy_price': h2_open,
            'h1_stabilized': bool(h1_stabilized),
            'metrics': forward_returns(cur, code, today, h2_open, all_days) if h1_stabilized else None,
        }

    # C. T+1 open 买入
    try:
        idx = all_days.index(today)
        if idx + 1 < len(all_days):
            t1 = all_days[idx + 1]
            cur.execute("SELECT open FROM stock_kline WHERE code=? AND date=?", (code, t1))
            row = cur.fetchone()
            if row and row[0] and row[0] > 0:
                res['C'] = {
                    'buy_price': row[0],
                    'metrics': forward_returns(cur, code, t1, row[0], all_days),
                }
    except ValueError:
        pass

    return res


class ScenarioStat:
    """收集某买点方案的收益样本"""
    def __init__(self, name):
        self.name = name
        self.t1_close = []   # T+1收盘卖出收益
        self.max5 = []       # 5日内最高(理论最优)
        self.min5 = []       # 5日内最低(最大回撤)

    def add(self, metrics):
        if not metrics:
            return
        if metrics.get('t1_close_pct') is not None:
            self.t1_close.append(metrics['t1_close_pct'])
        if metrics.get('max5_pct') is not None:
            self.max5.append(metrics['max5_pct'])
        if metrics.get('min5_pct') is not None:
            self.min5.append(metrics['min5_pct'])

    def report(self):
        n = len(self.t1_close)
        if n == 0:
            return f"    [{self.name}] 无样本"
        avg = sum(self.t1_close) / n
        wins = sum(1 for x in self.t1_close if x > 0)
        wr = wins / n * 100
        med = sorted(self.t1_close)[n // 2]
        avg_max5 = sum(self.max5) / len(self.max5) if self.max5 else 0
        avg_min5 = sum(self.min5) / len(self.min5) if self.min5 else 0
        return (f"    [{self.name}] 样本{n} | T+1收盘均值{avg:+.2f}% 中位{med:+.2f}% "
                f"胜率{wr:.1f}%({wins}/{n}) | 5日均最高{avg_max5:+.2f}% 均最低{avg_min5:+.2f}%")


def gap_group_of(open_rate):
    for name, lo, hi in GAP_GROUPS:
        if lo < open_rate <= hi:
            return name
    return None


def main():
    if len(sys.argv) < 2:
        print("用法: python strategy_limitup_failed_premium.py 2026-04 [end_month]")
        sys.exit(1)

    start_month = sys.argv[1]
    end_month = sys.argv[2] if len(sys.argv) >= 3 else start_month
    if len(start_month) != 7 or start_month[4] != '-':
        print(f"错误: 月份格式应为YYYY-MM, 收到: {start_month}")
        sys.exit(1)

    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()

    scan_days = get_trading_days_range(cur, start_month, end_month)
    if not scan_days:
        print(f"错误: 未找到 {start_month}~{end_month} 的交易日数据")
        conn.close()
        sys.exit(1)
    all_days = get_all_trading_days(cur)

    # 月份数(用于决定是否打印逐股明细)
    months = sorted(set(d[:7] for d in scan_days))
    print_detail = len(months) <= DETAIL_MONTHS_LIMIT

    print(f"{'='*84}")
    print(f"涨停次日大幅低开(溢价失败)反弹策略研究 [Task #91]")
    print(f"扫描区间: {start_month} ~ {end_month}  (交易日 {len(scan_days)} 天, 月份 {len(months)} 个)")
    print(f"低开阈值: open_rate <= {LOW_OPEN_THRESHOLD}%   板块: 创业板(sz.30x)+科创板(sh.688)")
    print(f"数据库总交易日: {len(all_days)}")
    print(f"{'='*84}")

    # 全局方案统计 + 分组统计
    stat_A = ScenarioStat('A:T日hour1_open买')
    stat_B = ScenarioStat('B:T日hour2_open买(h1企稳)')
    stat_C = ScenarioStat('C:T+1_open买')
    group_A = {g[0]: ScenarioStat(f'A/{g[0]}') for g in GAP_GROUPS}
    group_C = {g[0]: ScenarioStat(f'C/{g[0]}') for g in GAP_GROUPS}
    group_count = defaultdict(int)

    total_candidates = 0

    for today in scan_days:
        try:
            idx = all_days.index(today)
        except ValueError:
            continue
        if idx < 1:
            continue
        yesterday = all_days[idx - 1]

        candidates = find_candidates(cur, today, yesterday)
        total_candidates += len(candidates)

        if print_detail:
            print(f"\n{'='*70}")
            print(f"{'='*12} {today}   候选股: {len(candidates)} {'='*12}")
            print(f"{'='*70}")
            if candidates:
                print(f"\n  {'排名':<4}{'代码':<12}{'名称':<10}{'低开%':>8}{'换手%':>8}")
                print(f"  {'-'*46}")
                for i, c in enumerate(candidates):
                    tn = c['t_turn'] if c['t_turn'] is not None else 0
                    print(f"  {i+1:<4}{c['code']:<12}{(c['code_name'] or ''):<10}"
                          f"{c['open_rate']:>8.2f}{tn:>8.1f}")

        # 逐股明细 + 三方案分析
        for i, c in enumerate(candidates):
            grp = gap_group_of(c['open_rate'])
            if grp:
                group_count[grp] += 1

            scenarios = analyze_buy_scenarios(cur, c, all_days)
            if 'A' in scenarios:
                stat_A.add(scenarios['A']['metrics'])
                if grp:
                    group_A[grp].add(scenarios['A']['metrics'])
            if 'B' in scenarios and scenarios['B'].get('metrics'):
                stat_B.add(scenarios['B']['metrics'])
            if 'C' in scenarios:
                stat_C.add(scenarios['C']['metrics'])
                if grp:
                    group_C[grp].add(scenarios['C']['metrics'])

            # 打印明细(仅前 MAX_CANDIDATES_DETAIL 个)
            if print_detail and i < MAX_CANDIDATES_DETAIL:
                print(f"\n--- {c['code']} ({c['code_name'] or 'N/A'}) 低开{c['open_rate']:+.2f}% ---")
                yd_pct = (c['yd_close'] - c['yd_preclose']) / c['yd_preclose'] * 100
                print(f"  昨涨停: close={c['yd_close']:.2f} pre={c['yd_preclose']:.2f} ({yd_pct:+.2f}%)")
                print(f"  今低开: open={c['t_open']:.2f} pre={c['t_preclose']:.2f} "
                      f"| 当日 high={c['t_high']:.2f} low={c['t_low']:.2f} close={c['t_close']:.2f}")
                prev5 = max(0, idx - 5)
                next5 = min(len(all_days), idx + 6)
                window = all_days[prev5:next5]
                hour_rows = get_hour_data_for_days(cur, c['code'], window)
                if hour_rows:
                    print(f"  T-5..T+5 hour级明细 (★=信号日T):")
                    print(format_hour_table(hour_rows, today))
                # 三方案收益
                print(f"  买点方案收益:")
                for key, label in [('A', 'A hour1_open买'), ('B', 'B hour2_open买'), ('C', 'C T+1_open买')]:
                    s = scenarios.get(key)
                    if not s:
                        continue
                    bp = s['buy_price']
                    m = s.get('metrics')
                    if key == 'B' and not s.get('h1_stabilized'):
                        print(f"    [{label}] 买价{bp:.2f} → hour1未企稳(收阴), 不触发")
                        continue
                    if not m:
                        print(f"    [{label}] 买价{bp:.2f} → 无后续数据")
                        continue
                    parts = []
                    if m.get('t1_close_pct') is not None:
                        parts.append(f"T+1收盘{m['t1_close_pct']:+.2f}%")
                    if m.get('t1_high_pct') is not None:
                        parts.append(f"T+1最高{m['t1_high_pct']:+.2f}%")
                    if m.get('max5_pct') is not None:
                        parts.append(f"5日最高{m['max5_pct']:+.2f}%")
                    if m.get('min5_pct') is not None:
                        parts.append(f"5日最低{m['min5_pct']:+.2f}%")
                    print(f"    [{label}] 买价{bp:.2f} → " + " | ".join(parts))

    # ===================== 汇总 =====================
    print(f"\n\n{'='*84}")
    print(f"汇总统计  ({start_month}~{end_month})")
    print(f"{'='*84}")
    print(f"总候选股数: {total_candidates}  日均: {total_candidates/len(scan_days):.2f}")

    print(f"\n【买点方案对比 (T+1收盘卖出为主口径)】")
    print(stat_A.report())
    print(stat_B.report())
    print(stat_C.report())

    print(f"\n【低开幅度分组统计】")
    for name, lo, hi in GAP_GROUPS:
        print(f"  ── 分组 {name}  (候选数 {group_count.get(name,0)})")
        print(group_A[name].report())
        print(group_C[name].report())

    print(f"\n{'='*84}")
    print("研究完成。")
    conn.close()


if __name__ == '__main__':
    main()
