#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Task #109: 低开 + hour1 极端量比 (抄底版, AuctionGapUpVol 镜像) 策略研究 (rule2 流程)

策略思路 (AuctionGapUpVol 的镜像):
  AuctionGapUpVol(高开>=3% + hour1量比>=5x) 实现 CAGR 692%。
  本策略测试其镜像: 今日【低开】(gap down) 但 hour1 成交量极大(>=5倍近5日均量)。
  逻辑: 低开 + 巨量 = 有大资金在低位疯狂扫货(机构抄底), 而非恐慌性抛售
        (若是抛售, 价格会继续跌)。两个策略信号应完全不重叠(一高开一低开),
        若都有效则可互补组合。

合规 (rule2):
  T+1 交易 —— 当日 hour1 买入后, 当天不可卖出, 最早 T+1 才能卖。
  买入: T日 hour1_open (开盘价)。
  卖出: T+1 hour4_close (持仓1天, 同 AuctionGapUpVol 基准), 另测 T+2/T+3 hour4_close。

选股条件:
  - 今日低开: (open - preclose)/preclose <= -GAP_DOWN (低开 >= 2%)
  - today hour1_amount >= 近5个交易日均 hour1_amount * VOL_RATIO (量比)
  - 创业板 (sz.300/sz.301) / 科创板 (sh.688)
  - 流通市值 50-300 亿: mcap = amount/(turn/100)/1e8
  - 昨日非跌停 (排除连续跌停砸盘)
  - 今日开盘不是跌停价 (否则可能封死跌停无法买入)
  - 排除 ST

用法:
  python3 strategy_gapdown_vol_buyin.py [YYYY-MM]   # 单月扫描+明细+分析 (默认2026-04)
  python3 strategy_gapdown_vol_buyin.py fullcycle    # 2021-2026 全周期统计+逐年+分组

输出:
  日志: /home/AIWealth/scripts/logs/gapdown_vol_buyin.log
"""
import sqlite3
import sys
import os
from collections import defaultdict

DB_PATH = "/home/AIWealth/data/stocks.db"
LOG_PATH = "/home/AIWealth/scripts/logs/gapdown_vol_buyin.log"

# ===================== 可配置参数区 =====================
GAP_DOWN = 2.0          # 今日低开阈值 (%), (open-preclose)/preclose*100 <= -GAP_DOWN
VOL_RATIO = 5.0         # hour1 量比: today_hour1_amount >= 近5日均hour1_amount * VOL_RATIO
AVG_DAYS = 5            # 计算 hour1_amount 均值的回溯交易日数
MCAP_MIN = 50.0         # 流通市值下限 (亿)
MCAP_MAX = 300.0        # 流通市值上限 (亿)
# 创业板 + 科创板
BOARD_PREFIXES = ('sz.300', 'sz.301', 'sh.688')
DETAIL_WINDOW = 5       # 明细打印: 前5后5日
# 分组网格 (fullcycle 使用)
GAP_GROUPS = [2, 3, 5]          # 低开幅度分组 (%)
VOL_GROUPS = [3, 5, 7, 10]      # 量比分组 (x)
# =======================================================


def get_limit_ratio(code):
    if code.startswith('bj.'):
        return 0.30
    if code.startswith('sh.688') or code.startswith('sz.300') or code.startswith('sz.301'):
        return 0.20
    return 0.10


def is_limit_down(code, close, preclose):
    """收盘跌停判定 (基于精确跌停价 round(preclose*(1-r),2))。"""
    if close is None or preclose is None or preclose <= 0:
        return False
    limit_price = round(preclose * (1 - get_limit_ratio(code)), 2)
    return close <= limit_price + 0.001


def is_open_limit_down(code, open_price, preclose):
    """开盘跌停判定 (封死跌停可能无法买入)。"""
    if open_price is None or preclose is None or preclose <= 0:
        return False
    limit_price = round(preclose * (1 - get_limit_ratio(code)), 2)
    return open_price <= limit_price + 0.001


def is_st(row):
    if row.get('isST') == 1:
        return True
    name = row.get('code_name') or ''
    return 'ST' in name.upper()


def calc_mcap(amount, turn):
    """流通市值(亿) = amount / (turn/100) / 1e8"""
    if turn is None or turn <= 0 or amount is None or amount <= 0:
        return None
    return amount * 100 / turn / 1e8


LOAD_COLS = [
    'date', 'code', 'code_name', 'preclose', 'open', 'high', 'low', 'close',
    'open_rate', 'close_rate', 'amount', 'turn', 'isST', 'hour1_amount',
    'hour1_open', 'hour1_high', 'hour1_low', 'hour1_close',
    'hour2_open', 'hour2_high', 'hour2_low', 'hour2_close',
    'hour3_open', 'hour3_high', 'hour3_low', 'hour3_close',
    'hour4_open', 'hour4_high', 'hour4_low', 'hour4_close',
]


def load_board_data(conn):
    """加载创业板 + 科创板全历史, 按 code 分组, 按 date 排序。"""
    cur = conn.cursor()
    col_sql = ','.join(LOAD_COLS)
    cur.execute(f"""
        SELECT {col_sql} FROM stock_kline
        WHERE code LIKE 'sz.300%' OR code LIKE 'sz.301%' OR code LIKE 'sh.688%'
        ORDER BY code, date
    """)
    data = defaultdict(list)
    for row in cur.fetchall():
        d = dict(zip(LOAD_COLS, row))
        data[d['code']].append(d)
    return data


def find_signals(board_data, start_date, end_date):
    """在 [start_date, end_date] 找出所有符合条件的信号 (T日)。
    基础门槛使用最宽松条件 (低开>=2% & 量比>=3x), 便于 fullcycle 内存二次分组。"""
    signals = []
    for code, rows in board_data.items():
        if len(rows) < AVG_DAYS + 3:
            continue
        for i in range(AVG_DAYS, len(rows) - 1):  # 需要 T 前 AVG_DAYS 天 及 T+1
            t0 = rows[i]
            tm1 = rows[i - 1]
            d = t0['date']
            if d < start_date or d > end_date:
                continue
            # 基础有效性
            if t0['preclose'] is None or t0['preclose'] <= 0:
                continue
            if t0['open'] is None or t0['open'] <= 0:
                continue
            if t0['hour1_amount'] is None or t0['hour1_amount'] <= 0:
                continue
            if t0['hour1_open'] is None or t0['hour1_open'] <= 0:
                continue
            # 排除 ST
            if is_st(t0) or is_st(tm1):
                continue
            # 昨日非跌停 (排除连续跌停砸盘)
            if is_limit_down(code, tm1['close'], tm1['preclose']):
                continue
            # 今日低开 >= GAP_DOWN
            gap = (t0['open'] - t0['preclose']) / t0['preclose'] * 100
            if gap > -GAP_DOWN:
                continue
            # 排除今日开盘跌停 (封死无法买入)
            if is_open_limit_down(code, t0['open'], t0['preclose']):
                continue
            # 近 AVG_DAYS 日均 hour1_amount
            prev_h1 = []
            for k in range(i - 1, -1, -1):
                v = rows[k]['hour1_amount']
                if v is not None and v > 0:
                    prev_h1.append(v)
                if len(prev_h1) >= AVG_DAYS:
                    break
            if len(prev_h1) < AVG_DAYS:
                continue
            avg_h1 = sum(prev_h1) / len(prev_h1)
            if avg_h1 <= 0:
                continue
            vol_ratio = t0['hour1_amount'] / avg_h1
            if vol_ratio < min(VOL_GROUPS):
                continue
            # 市值 50-300 亿
            mcap = calc_mcap(t0['amount'], t0['turn'])
            if mcap is None or mcap < MCAP_MIN or mcap > MCAP_MAX:
                continue

            signals.append({
                'code': code, 'date': d, 'idx': i, 'rows': rows,
                'gap': gap, 'vol_ratio': vol_ratio, 'mcap': mcap,
                'turn': t0['turn'], 'name': t0['code_name'],
            })
    signals.sort(key=lambda s: (s['date'], s['code']))
    return signals


# ---------------------- Step 2: 明细打印 ----------------------
def fmt_rate(price, base):
    if price is None or base is None or base <= 0:
        return '  --  '
    return f"{(price - base) / base * 100:+5.1f}"


def print_detail(sig, win=DETAIL_WINDOW):
    rows = sig['rows']
    i = sig['idx']
    base = rows[i]['open']  # 相对 T日 open
    lo = max(0, i - win)
    hi = min(len(rows) - 1, i + win)
    print(f"\n  \u2500\u2500 {sig['code']} {sig['name']} @ {sig['date']} "
          f"[\u4f4e\u5f00{sig['gap']:+.1f}% \u91cf\u6bd4{sig['vol_ratio']:.1f}x \u5e02\u503c{sig['mcap']:.0f}\u4ebf \u6362\u624b{sig['turn']:.1f}%]")
    print(f"     (rate% \u76f8\u5bf9 T\u65e5open={base:.2f}; \u2605=\u4fe1\u53f7\u65e5)")
    for j in range(lo, hi + 1):
        r = rows[j]
        off = j - i
        mark = '\u2605' if off == 0 else ' '
        tag = f"D{off:+d}{mark}"
        segs = []
        for h in ('hour1', 'hour2', 'hour3', 'hour4'):
            segs.append(f"{h[-1]}[O{fmt_rate(r[h+'_open'],base)} H{fmt_rate(r[h+'_high'],base)} "
                        f"L{fmt_rate(r[h+'_low'],base)} C{fmt_rate(r[h+'_close'],base)}]")
        turn = r['turn'] if r['turn'] is not None else 0
        print(f"     {tag} {r['date']} " + ' '.join(segs) + f" turn={turn:.1f}%")


# ---------------------- Step 3: 收益 (严格 T+1 合规) ----------------------
def buy_price(sig):
    """买入价 = T日 hour1_open (开盘价)。"""
    return sig['rows'][sig['idx']]['hour1_open']


def exit_ret(sig, bpr, day_off):
    """卖出 = T+day_off hour4_close, 返回收益% (合规: day_off>=1)。"""
    rows = sig['rows']
    j = sig['idx'] + day_off
    if j >= len(rows):
        return None
    p = rows[j]['hour4_close']
    if p is None or p <= 0 or bpr is None or bpr <= 0:
        return None
    return (p - bpr) / bpr * 100


def stat_block(rets):
    rets = [r for r in rets if r is not None]
    if not rets:
        return None
    n = len(rets)
    avg = sum(rets) / n
    win = sum(1 for r in rets if r > 0) / n * 100
    return {'n': n, 'avg': avg, 'win': win}


def analyze(signals, title):
    print("\n" + "=" * 74)
    print(f"\u6536\u76ca\u5206\u6790: {title}  (\u4fe1\u53f7\u6570={len(signals)})  [\u4e70 h1_open, \u4e25\u683c T+1 \u5408\u89c4]")
    print("=" * 74)
    if not signals:
        print("  \u65e0\u4fe1\u53f7, \u8df3\u8fc7\u3002")
        return
    # 持有 1/2/3 天 (T+1/T+2/T+3 hour4 收盘卖)
    print("\n\u3010\u6301\u4ed3\u5929\u6570 (\u4e70 hour1_open, \u5356 T+N hour4_close)\u3011")
    print(f"  {'持仓':<8}{'样本':<7}{'平均收益':<11}{'胜率':<8}")
    print("  " + "-" * 34)
    for dn, label in [(1, 'T+1'), (2, 'T+2'), (3, 'T+3')]:
        rets = [exit_ret(s, buy_price(s), dn) for s in signals]
        st = stat_block(rets)
        if st:
            print(f"  {label:<8}{st['n']:<7}{st['avg']:+.2f}%{'':<4}{st['win']:.1f}%")


# ---------------------- 参数分组 (低开 x 量比) ----------------------
def analyze_groups(signals, hold_day=1):
    print("\n" + "=" * 74)
    print(f"\u53c2\u6570\u5206\u7ec4: \u4f4e\u5f00\u5e45\u5ea6 x \u91cf\u6bd4  (\u4e70 h1_open, \u5356 T+{hold_day} hour4_close)")
    print("=" * 74)
    print(f"  {'低开>=':<8}{'量比>=':<8}{'样本':<7}{'平均收益':<11}{'胜率':<9}{'逐年是否全正':<14}")
    print("  " + "-" * 60)
    best = None
    for g in GAP_GROUPS:
        for v in VOL_GROUPS:
            subset = [s for s in signals if s['gap'] <= -g and s['vol_ratio'] >= v]
            if len(subset) < 20:
                continue
            rets, yearly = [], defaultdict(list)
            for s in subset:
                r = exit_ret(s, buy_price(s), hold_day)
                if r is not None:
                    rets.append(r)
                    yearly[s['date'][:4]].append(r)
            st = stat_block(rets)
            if not st:
                continue
            yr_avgs = {y: sum(vv) / len(vv) for y, vv in yearly.items() if vv}
            all_pos = all(a > 0 for a in yr_avgs.values())
            pos_cnt = sum(1 for a in yr_avgs.values() if a > 0)
            flag = f"{pos_cnt}/{len(yr_avgs)}\u5e74\u6b63" + (" \u2713\u5168\u6b63" if all_pos else "")
            print(f"  {g:<8}{v:<8}{st['n']:<7}{st['avg']:+.2f}%{'':<4}{st['win']:.1f}%{'':<4}{flag}")
            if st['win'] >= 55 and (best is None or st['avg'] > best[1]['avg']):
                best = ((g, v), st, all_pos)
    print()
    if best:
        (g, v), st, allp = best
        tail = '(逐年全正)' if allp else ''
        print(f"  \u2605 \u80dc\u7387>=55%\u4e2d\u6536\u76ca\u6700\u9ad8: \u4f4e\u5f00>={g}% \u91cf\u6bd4>={v}x "
              f"\u2192 {st['avg']:+.2f}% \u80dc\u7387{st['win']:.1f}% n={st['n']} {tail}")
    else:
        print("  \u672a\u627e\u5230\u80dc\u7387>=55%\u7684\u5206\u7ec4\u3002")
    return best


# ---------------------- 主流程 ----------------------
class Tee:
    def __init__(self, f):
        self.file = f
        self.stdout = sys.__stdout__
    def write(self, s):
        self.stdout.write(s)
        self.file.write(s)
    def flush(self):
        self.stdout.flush()
        self.file.flush()


def run_month(month):
    start = month + "-01"
    end = month + "-31"
    conn = sqlite3.connect(DB_PATH)
    print("=" * 74)
    print(f"Task#109 \u4f4e\u5f00+hour1\u6781\u7aef\u91cf\u6bd4(\u62c4\u5e95\u7248)  \u5355\u6708\u626b\u63cf: {month}")
    print(f"\u6761\u4ef6: \u6628\u975e\u8dcc\u505c & \u4f4e\u5f00>={GAP_DOWN}% & hour1\u91cf\u6bd4>={min(VOL_GROUPS)}x(\u57fa\u7840) & \u521b/\u79d1 & \u5e02\u503c{MCAP_MIN:.0f}-{MCAP_MAX:.0f}\u4ebf & \u6392ST & \u6392\u5f00\u76d8\u8dcc\u505c")
    print("=" * 74)
    board = load_board_data(conn)
    conn.close()
    print(f"\u521b\u4e1a\u677f+\u79d1\u521b\u677f\u80a1\u7968\u6570: {len(board)}")
    signals = find_signals(board, start, end)
    print(f"\n{month} \u5019\u9009\u4fe1\u53f7\u6570: {len(signals)}")
    print("\n" + "=" * 74)
    print("Step2: \u5019\u9009\u80a1\u524d5\u540e5\u65e5 hour\u7ea7 OCHL \u660e\u7ec6 (rate%)")
    print("=" * 74)
    for s in signals:
        print_detail(s)
    analyze(signals, month)
    analyze_groups(signals, hold_day=1)


def run_fullcycle():
    conn = sqlite3.connect(DB_PATH)
    print("=" * 74)
    print("Task#109 \u4f4e\u5f00+hour1\u6781\u7aef\u91cf\u6bd4(\u62c4\u5e95\u7248)  \u5168\u5468\u671f 2021-2026 \u9a8c\u8bc1")
    print(f"\u6761\u4ef6: \u6628\u975e\u8dcc\u505c & \u4f4e\u5f00>={GAP_DOWN}% & hour1\u91cf\u6bd4(\u8fd15\u65e5\u5747) & \u521b/\u79d1 & \u5e02\u503c{MCAP_MIN:.0f}-{MCAP_MAX:.0f}\u4ebf & \u6392ST")
    print("=" * 74)
    board = load_board_data(conn)
    conn.close()
    print(f"\u521b\u4e1a\u677f+\u79d1\u521b\u677f\u80a1\u7968\u6570: {len(board)}")
    signals = find_signals(board, "2021-01-01", "2026-06-30")
    print(f"\u5168\u5468\u671f\u57fa\u7840\u5019\u9009\u4fe1\u53f7\u6570(\u4f4e\u5f00>=2% & \u91cf\u6bd4>=3x): {len(signals)}")
    # 逐年信号分布
    yearly = defaultdict(list)
    for s in signals:
        yearly[s['date'][:4]].append(s)
    print("\n\u9010\u5e74\u4fe1\u53f7\u5206\u5e03 (\u57fa\u7840\u6761\u4ef6):")
    total = len(signals)
    for y in sorted(yearly):
        n = len(yearly[y])
        pct = n / total * 100 if total else 0
        flag = '  \u26a0\ufe0f\u96c6\u4e2d\u98ce\u9669' if pct > 40 else ''
        print(f"  {y}: {n:>4} \u7b14 ({pct:4.1f}%){flag}")
    # 整体分析
    analyze(signals, "\u5168\u5468\u671f 2021-2026")
    # 参数分组
    best = analyze_groups(signals, hold_day=1)
    # 逐年稳定性 (推荐组合 or 基础)
    if best:
        (g, v), _, _ = best
    else:
        g, v = GAP_DOWN, VOL_RATIO
    print("\n" + "=" * 74)
    print(f"\u9010\u5e74\u7a33\u5b9a\u6027: \u4f4e\u5f00>={g}% \u91cf\u6bd4>={v}x, \u4e70h1_open \u5356T+1\u5c3e\u76d8")
    print("=" * 74)
    print(f"  {'年份':<8}{'样本':<7}{'平均收益':<11}{'胜率':<8}")
    print("  " + "-" * 34)
    sub = [s for s in signals if s['gap'] <= -g and s['vol_ratio'] >= v]
    subyear = defaultdict(list)
    for s in sub:
        subyear[s['date'][:4]].append(s)
    for y in sorted(subyear):
        rets = [exit_ret(s, buy_price(s), 1) for s in subyear[y]]
        st = stat_block(rets)
        if st:
            print(f"  {y:<8}{st['n']:<7}{st['avg']:+.2f}%{'':<4}{st['win']:.1f}%")
    # 与 AuctionGapUpVol 对比
    print("\n" + "=" * 74)
    print("\u4e0e AuctionGapUpVol (\u9ad8\u5f00\u7248, Task#95) \u5bf9\u6bd4")
    print("=" * 74)
    print("  AuctionGapUpVol: \u9ad8\u5f00>=3% + hour1\u91cf\u6bd4>=5x, \u4e70h1_open \u5356T+1\u5c3e\u76d8")
    print("                   \u5355\u7b14 +3.49% \u80dc\u7387 59.3% n=985 (6\u5e74\u5168\u6b63) => \u771f\u5b9e alpha")
    print("  \u672c\u7b56\u7565(\u4f4e\u5f00\u955c\u50cf): \u89c1\u4e0a\u65b9\u53c2\u6570\u5206\u7ec4\u4e0e\u9010\u5e74\u7ed3\u679c\u3002")
    print("  \u4fe1\u53f7\u91cd\u53e0\u6027: \u9ad8\u5f00 vs \u4f4e\u5f00 \u4e92\u65a5, \u4e24\u7b56\u7565\u4fe1\u53f7\u96c6\u5408\u96f6\u4ea4\u96c6, \u82e5\u672c\u7b56\u7565\u6709 alpha \u5219\u53ef\u4e92\u8865\u7ec4\u5408\u3002")


def main():
    os.makedirs(os.path.dirname(LOG_PATH), exist_ok=True)
    logf = open(LOG_PATH, 'w', encoding='utf-8')
    sys.stdout = Tee(logf)
    try:
        arg = sys.argv[1] if len(sys.argv) > 1 else "2026-04"
        if arg == "fullcycle":
            run_fullcycle()
        else:
            run_month(arg)
    finally:
        sys.stdout.flush()
        sys.stdout = sys.__stdout__
        logf.close()


if __name__ == "__main__":
    main()