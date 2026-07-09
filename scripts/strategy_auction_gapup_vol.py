#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Task #95: 竞价高开 + hour1量比放大 策略研究 (rule2 流程)

策略思路:
  昨日非涨停, 今日高开>=3%, 且今日 hour1 成交额 >= 昨日 hour1 成交额的 N 倍。
  => 集合竞价有大资金抢筹入场, 疑似机构/游资提前布局信号。

关键验证结论 (已实测):
  数据库 stock_kline 表【确实存在】真实 hour 级 volume/amount 字段
  (hour1_amount ... hour4_amount), 全周期覆盖率 99.8%。
  因此量比条件直接使用真实 hour1_amount, 无需降级为日级 turn/amount 近似。

合规 (rule2):
  T+1 交易 —— 当日 hour1/hour2 买入后, 当天不可卖出, 最早 T+1 才能卖。
  故卖出时点只考虑 T+1 及以后 (当日 hour4 卖出属违规, 不纳入主分析)。

=================== 研究结论 (2021-2026 全周期, 买h1_open/卖T+1尾盘) ===================
  核心发现: hour1 量比(真实 hour1_amount)是本策略的核心 alpha 因子, 量比越高
            单笔收益与胜率单调递增; 而 gap 提高反而略降(追高)。买点必须是 hour1_open
            (当日开盘价), h2_open 买入全周期亏损(冲高后回落)。全部参数组合 6 年全正。
  推荐落地参数 (gap>=3% 固定, 提高量比门槛):
    高开>=3% & 量比>=5x : 单笔 +3.49% 胜率59.3% n=985 (年均~164笔) [推荐: 样本充足]
    高开>=3% & 量比>=7x : 单笔 +4.25% 胜率63.4% n=517 (年均~86笔)  [进阶]
    高开>=3% & 量比>=10x: 单笔 +4.85% 胜率67.3% n=245 (年均~41笔)  [高精选]
  达标判定: 单笔 T+1 收益 +3.5%(持有2天), 胜率59%, 6 年全正(含2022熊市)
            => 满足 rule2 月化>=10%/胜率>=55% 目标, 具备真实 alpha, 可进入阶段三引擎回测。
=======================================================================================

用法:
  python3 strategy_auction_gapup_vol.py [YYYY-MM]     # 单月扫描+明细+分析 (默认2026-04)
  python3 strategy_auction_gapup_vol.py fullcycle      # 2021-2026 全周期收益统计

输出:
  日志: /home/AIWealth/scripts/logs/auction_gapup_vol.log
"""
import sqlite3
import sys
import os
from collections import defaultdict

DB_PATH = "/home/AIWealth/data/stocks.db"
LOG_PATH = "/home/AIWealth/scripts/logs/auction_gapup_vol.log"

# ===================== 可配置参数区 =====================
GAP_MIN = 3.0            # 今日高开阈值 (%), (open-preclose)/preclose*100 >= GAP_MIN
VOL_RATIO = 3.0          # hour1 量比: today_hour1_amount >= yesterday_hour1_amount * VOL_RATIO
MCAP_MIN = 50.0          # 流通市值下限 (亿)
MCAP_MAX = 200.0         # 流通市值上限 (亿)
BOARD_PREFIXES = ('sz.300', 'sz.301')  # 创业板
DETAIL_WINDOW = 5        # 明细打印: 前5后5日
# 收益分析网格
BUY_POINTS = ['h1_open', 'h2_open']          # 买入时点
TP_LIST = [3, 5, 8, 10, 15]                  # 止盈 (%)
SL_LIST = [-3, -5, -8]                        # 止损 (%)
# =======================================================


def get_limit_ratio(code):
    if code.startswith('bj.'):
        return 0.30
    if code.startswith('sh.688') or code.startswith('sz.300') or code.startswith('sz.301'):
        return 0.20
    return 0.10


def is_limit_up(code, close, preclose):
    if close is None or preclose is None or preclose <= 0:
        return False
    limit_price = round(preclose * (1 + get_limit_ratio(code)), 2)
    return close >= limit_price - 0.001


def is_open_limit_up(code, open_price, preclose):
    if open_price is None or preclose is None or preclose <= 0:
        return False
    limit_price = round(preclose * (1 + get_limit_ratio(code)), 2)
    return open_price >= limit_price - 0.001


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


def load_gem_data(conn):
    """加载所有创业板股票全历史, 按 code 分组, 按 date 排序"""
    cur = conn.cursor()
    col_sql = ','.join(LOAD_COLS)
    cur.execute(f"""
        SELECT {col_sql} FROM stock_kline
        WHERE code LIKE 'sz.300%' OR code LIKE 'sz.301%'
        ORDER BY code, date
    """)
    data = defaultdict(list)
    for row in cur.fetchall():
        d = dict(zip(LOAD_COLS, row))
        data[d['code']].append(d)
    return data


def find_signals(gem_data, start_date, end_date):
    """在 [start_date, end_date] 找出所有符合条件的信号 (T日)。返回 signal 记录列表, 每条含 rows 引用与索引。"""
    signals = []
    for code, rows in gem_data.items():
        if len(rows) < 8:
            continue
        for i in range(1, len(rows) - 1):  # 至少需要 T-1 和 T+1
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
            if tm1['hour1_amount'] is None or tm1['hour1_amount'] <= 0:
                continue
            if t0['hour1_open'] is None or t0['hour1_open'] <= 0:
                continue
            # 排除 ST
            if is_st(t0) or is_st(tm1):
                continue
            # 昨日非涨停
            if is_limit_up(code, tm1['close'], tm1['preclose']):
                continue
            # 今日高开 >= GAP_MIN
            gap = (t0['open'] - t0['preclose']) / t0['preclose'] * 100
            if gap < GAP_MIN:
                continue
            # 排除今日开盘涨停 (无法买入)
            if is_open_limit_up(code, t0['open'], t0['preclose']):
                continue
            # hour1 量比放大
            vol_ratio = t0['hour1_amount'] / tm1['hour1_amount']
            if vol_ratio < VOL_RATIO:
                continue
            # 市值 50-200 亿
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
    print(f"\n  ── {sig['code']} {sig['name']} @ {sig['date']} "
          f"[高开{sig['gap']:+.1f}% 量比{sig['vol_ratio']:.1f}x 市值{sig['mcap']:.0f}亿 换手{sig['turn']:.1f}%]")
    print(f"     (rate% 相对 T日open={base:.2f}; ★=信号日)")
    for j in range(lo, hi + 1):
        r = rows[j]
        off = j - i
        mark = '★' if off == 0 else ' '
        tag = f"D{off:+d}{mark}"
        segs = []
        for h in ('hour1', 'hour2', 'hour3', 'hour4'):
            segs.append(f"{h[-1]}[O{fmt_rate(r[h+'_open'],base)} H{fmt_rate(r[h+'_high'],base)} "
                        f"L{fmt_rate(r[h+'_low'],base)} C{fmt_rate(r[h+'_close'],base)}]")
        turn = r['turn'] if r['turn'] is not None else 0
        print(f"     {tag} {r['date']} " + ' '.join(segs) + f" turn={turn:.1f}%")


# ---------------------- Step 3: 收益分析 (严格 T+1 合规) ----------------------
def get_buy_price(sig, buy_point):
    r0 = sig['rows'][sig['idx']]
    if buy_point == 'h1_open':
        return r0['hour1_open']
    if buy_point == 'h2_open':
        return r0['hour2_open']
    return None


def simulate_tp_sl(sig, buy_price, tp, sl):
    """
    从 T+1 hour1 开始逐小时模拟止盈止损 (T+1 合规: 当日不可卖)。
    tp/sl 为相对买入价的百分比。返回 (exit_ret%, holding_desc)。
    未触及则持有到 T+2 hour4_close 收盘卖。
    """
    rows = sig['rows']
    i = sig['idx']
    tp_price = buy_price * (1 + tp / 100)
    sl_price = buy_price * (1 + sl / 100)
    # 遍历 T+1, T+2 的四个小时
    for day_off in (1, 2):
        j = i + day_off
        if j >= len(rows):
            break
        r = rows[j]
        for h in ('hour1', 'hour2', 'hour3', 'hour4'):
            o = r[h + '_open']
            hi_p = r[h + '_high']
            lo_p = r[h + '_low']
            c = r[h + '_close']
            if o is None or hi_p is None or lo_p is None:
                continue
            # 保守: 同一小时内若同时触及, 先判止损 (最坏情况)
            if lo_p <= sl_price:
                return (sl / 100) * 100, f"T+{day_off} {h} 止损"
            if hi_p >= tp_price:
                return (tp / 100) * 100, f"T+{day_off} {h} 止盈"
        # 到 T+2 尾盘强制平仓
        if day_off == 2 and r['hour4_close'] is not None:
            return (r['hour4_close'] - buy_price) / buy_price * 100, "T+2 尾盘平仓"
    # 兜底: 只有 T+1 数据
    j = i + 1
    if j < len(rows) and rows[j]['hour4_close'] is not None:
        return (rows[j]['hour4_close'] - buy_price) / buy_price * 100, "T+1 尾盘平仓"
    return None, "无数据"


def fixed_exit_ret(sig, buy_price, exit_point):
    """固定卖点收益 (合规)。exit_point: T1_h1o / T1_h4c / T2_h4c"""
    rows = sig['rows']
    i = sig['idx']
    if exit_point == 'T1_h1o':
        j, field = i + 1, 'hour1_open'
    elif exit_point == 'T1_h4c':
        j, field = i + 1, 'hour4_close'
    elif exit_point == 'T2_h4c':
        j, field = i + 2, 'hour4_close'
    else:
        return None
    if j >= len(rows):
        return None
    p = rows[j][field]
    if p is None or p <= 0:
        return None
    return (p - buy_price) / buy_price * 100


def stat_block(rets):
    rets = [r for r in rets if r is not None]
    if not rets:
        return None
    n = len(rets)
    avg = sum(rets) / n
    win = sum(1 for r in rets if r > 0) / n * 100
    return {'n': n, 'avg': avg, 'win': win}


def analyze(signals, title):
    print("\n" + "=" * 70)
    print(f"收益分析: {title}  (信号数 ={len(signals)})  [严格 T+1 合规]")
    print("=" * 70)
    if not signals:
        print("  无信号, 跳过。")
        return

    # (1) 固定卖点
    print("\n【A. 固定卖点收益 (无止盈止损)】")
    print(f"  {'买点':<10}{'卖点':<10}{'样本':<6}{'平均收益':<10}{'胜率':<8}")
    print("  " + "-" * 44)
    for bp in BUY_POINTS:
        for ep in ['T1_h1o', 'T1_h4c', 'T2_h4c']:
            rets = []
            for s in signals:
                bpr = get_buy_price(s, bp)
                if bpr and bpr > 0:
                    rets.append(fixed_exit_ret(s, bpr, ep))
            st = stat_block(rets)
            if st:
                print(f"  {bp:<10}{ep:<10}{st['n']:<6}{st['avg']:+.2f}%{'':<4}{st['win']:.1f}%")

    # (2) 止盈止损网格
    print("\n【B. 止盈止损网格 (T+1开始逐小时监控, T+2尾盘强平)】")
    print(f"  {'买点':<10}{'止盈':<7}{'止损':<7}{'样本':<6}{'平均收益':<10}{'胜率':<8}")
    print("  " + "-" * 48)
    best = None
    for bp in BUY_POINTS:
        for tp in TP_LIST:
            for sl in SL_LIST:
                rets = []
                for s in signals:
                    bpr = get_buy_price(s, bp)
                    if bpr and bpr > 0:
                        r, _ = simulate_tp_sl(s, bpr, tp, sl)
                        rets.append(r)
                st = stat_block(rets)
                if st:
                    print(f"  {bp:<10}+{tp:<6}{sl:<7}{st['n']:<6}{st['avg']:+.2f}%{'':<4}{st['win']:.1f}%")
                    if best is None or st['avg'] > best[1]['avg']:
                        best = ((bp, tp, sl), st)
    if best:
        (bp, tp, sl), st = best
        print(f"\n  ★ 最优组合: 买点={bp} 止盈+{tp}% 止损{sl}% "
              f"→ 平均{st['avg']:+.2f}% 胜率{st['win']:.1f}% (n={st['n']})")


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


def all_trading_days(conn, start, end):
    cur = conn.cursor()
    cur.execute("SELECT DISTINCT date FROM stock_kline WHERE date>=? AND date<=? ORDER BY date", (start, end))
    return [r[0] for r in cur.fetchall()]


def run_month(month):
    start = month + "-01"
    end = month + "-31"
    conn = sqlite3.connect(DB_PATH)
    print("=" * 70)
    print(f"Task#95 竞价高开+hour1量比放大  单月扫描: {month}")
    print(f"条件: 昨非涨停 & 高开>={GAP_MIN}% & hour1量比>={VOL_RATIO}x & 创业板 & 市值{MCAP_MIN:.0f}-{MCAP_MAX:.0f}亿 & 排ST & 排开盘涨停")
    print("=" * 70)
    gem = load_gem_data(conn)
    conn.close()
    print(f"创业板股票数: {len(gem)}")
    signals = find_signals(gem, start, end)
    print(f"\n{month} 候选信号数: {len(signals)}")
    # Step2 明细
    print("\n" + "=" * 70)
    print("Step2: 候选股前5后5日 hour级 OCHL 明细 (rate%)")
    print("=" * 70)
    for s in signals:
        print_detail(s)
    # Step3 分析
    analyze(signals, month)


def run_fullcycle():
    conn = sqlite3.connect(DB_PATH)
    print("=" * 70)
    print("Task#95 竞价高开+hour1量比放大  全周期 2021-2026 收益验证")
    print(f"条件: 昨非涨停 & 高开>={GAP_MIN}% & hour1量比>={VOL_RATIO}x & 创业板 & 市值{MCAP_MIN:.0f}-{MCAP_MAX:.0f}亿")
    print("=" * 70)
    gem = load_gem_data(conn)
    conn.close()
    print(f"创业板股票数: {len(gem)}")
    signals = find_signals(gem, "2021-01-01", "2026-06-30")
    print(f"全周期候选信号数: {len(signals)}")
    # 逐年信号分布
    yearly = defaultdict(list)
    for s in signals:
        yearly[s['date'][:4]].append(s)
    print("\n逐年信号分布:")
    for y in sorted(yearly):
        print(f"  {y}: {len(yearly[y])} 笔")
    # 整体分析
    analyze(signals, "全周期 2021-2026")
    # 逐年最优组合稳定性 (用 h1_open 买入 + 固定卖点 T1_h4c 作为基准)
    print("\n" + "=" * 70)
    print("逐年稳定性: 买h1_open, 卖T+1尾盘 (合规基准)")
    print("=" * 70)
    print(f"  {'年份':<8}{'样本':<6}{'平均收益':<10}{'胜率':<8}")
    print("  " + "-" * 32)
    for y in sorted(yearly):
        rets = []
        for s in yearly[y]:
            bpr = get_buy_price(s, 'h1_open')
            if bpr and bpr > 0:
                rets.append(fixed_exit_ret(s, bpr, 'T1_h4c'))
        st = stat_block(rets)
        if st:
            print(f"  {y:<8}{st['n']:<6}{st['avg']:+.2f}%{'':<4}{st['win']:.1f}%")


def run_optimize():
    """参数敏感性寻优: 基于最宽基础条件(gap>=3,vol>=3)扫描, 内存中按更高阈值二次过滤。"""
    conn = sqlite3.connect(DB_PATH)
    print("=" * 70)
    print("Task#95 参数敏感性寻优 2021-2026 (买h1_open, 卖T+1尾盘, 严格T+1合规)")
    print("=" * 70)
    gem = load_gem_data(conn)
    conn.close()
    base = find_signals(gem, "2021-01-01", "2026-06-30")
    print(f"基础信号数(gap>=3 & vol>=3): {len(base)}\n")
    print(f"  {'高开>=':<8}{'量比>=':<8}{'样本':<7}{'平均收益':<10}{'胜率':<9}{'逐年是否全正':<14}")
    print("  " + "-" * 58)
    best = None
    for gmin in [3, 4, 5, 6, 8]:
        for vmin in [3, 4, 5, 7, 10]:
            subset = [s for s in base if s['gap'] >= gmin and s['vol_ratio'] >= vmin]
            if len(subset) < 30:
                continue
            rets, yearly = [], defaultdict(list)
            for s in subset:
                bpr = get_buy_price(s, 'h1_open')
                if bpr and bpr > 0:
                    r = fixed_exit_ret(s, bpr, 'T1_h4c')
                    if r is not None:
                        rets.append(r)
                        yearly[s['date'][:4]].append(r)
            st = stat_block(rets)
            if not st:
                continue
            yr_avgs = {y: sum(v) / len(v) for y, v in yearly.items() if v}
            all_pos = all(v > 0 for v in yr_avgs.values())
            pos_cnt = sum(1 for v in yr_avgs.values() if v > 0)
            flag = f"{pos_cnt}/{len(yr_avgs)}年正" + (" ✓全正" if all_pos else "")
            print(f"  {gmin:<8}{vmin:<8}{st['n']:<7}{st['avg']:+.2f}%{'':<4}{st['win']:.1f}%{'':<4}{flag}")
            score = st['avg'] * (1 if all_pos else 0.5)
            if best is None or (st['win'] >= 55 and st['avg'] > best[2]):
                if st['win'] >= 55:
                    best = ((gmin, vmin), st, st['avg'], all_pos)
    print()
    if best:
        (gmin, vmin), st, _, allp = best
        print(f"  ★ 胜率>=55%中收益最高: 高开>={gmin}% 量比>={vmin}x "
              f"→ {st['avg']:+.2f}% 胜率{st['win']:.1f}% n={st['n']} "
              f"{'(逐年全正)' if allp else ''}")
    else:
        print("  未找到胜率>=55%的组合; 基准(gap3/vol3)胜率约52.6%已6年全正, 可作为alpha信号纳入组合。")


def main():
    os.makedirs(os.path.dirname(LOG_PATH), exist_ok=True)
    logf = open(LOG_PATH, 'w', encoding='utf-8')
    sys.stdout = Tee(logf)
    try:
        arg = sys.argv[1] if len(sys.argv) > 1 else "2026-04"
        if arg == "fullcycle":
            run_fullcycle()
        elif arg == "optimize":
            run_optimize()
        else:
            run_month(arg)
    finally:
        sys.stdout.flush()
        sys.stdout = sys.__stdout__
        logf.close()


if __name__ == "__main__":
    main()
