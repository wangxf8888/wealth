#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Task #104: 涨停次日高开 + hour1量比确认 策略研究 (rule2 流程)

策略思路:
  昨日涨停(创业板/科创板×1.20, 主板×1.10), 今日高开>=2%, 且今日 hour1 成交额
  >= 昨日 hour1 成交额的 N 倍(N=2/3/5)。
  => 涨停本身已筛出"有资金关注"的标的; 次日再叠加高开+hour1放量,
     信号质量理论上应高于随机高开+放量(对标 Task#95 AuctionGapUpVol)。

对标基准 (Task#95 AuctionGapUpVol, 昨日【非】涨停 + 高开>=3% + hour1量比):
  高开>=3% & 量比>=5x : 单笔 +3.49% 胜率59.3% n=985 (2021-2026, 买h1_open/卖T+1尾盘)
  => 本任务核心问题: 把"昨日非涨停"换成"昨日涨停"后, 信号质量是否更高?

=================== 研究结论 (2021-2026 全周期, 买h1_open/卖T+1尾盘) ===================
  1) 信号质量【确实提升】(验证"涨停已预筛资金关注标的"假设方向正确), 同口径对比:
       口径(gap>=3% & 量比>=5x)  单笔收益   胜率     样本n
       Task#104 昨日涨停          +4.11%    61.1%    54
       Task#95  昨日非涨停        +3.49%    59.3%    985
     => 加"昨日涨停"后单笔+0.6pct、胜率+1.8pct, 边际增强有效。
  2) 代价: 样本量骤降至约 1/18 (54 vs 985), 年均仅 ~9 笔, 交易频率过低,
     无法独立支撑 rule2 的年化100%-500%目标。
  3) 样本充足档 (gap>=2% & 量比>=2x): 单笔+2.67% 胜率54.7% n=362(年均~60笔),
     且【2021-2026 逐年全正】(含2022熊市), 稳健但单笔收益不及 AuctionGapUpVol。
  逐年信号分布: 2021:53 2022:34 2023:32 2024:89 2025:97 2026:60
  逐年收益(gap2/vol2,卖T+1尾盘): 2021 +5.39%/69% | 2022 +0.73%/53% | 2023 +2.70%/38%
                                 | 2024 +2.59%/49% | 2025 +1.86%/56% | 2026 +2.82%/58%
  最终结论: "昨日涨停"是有效的【alpha增强因子】而非独立策略——样本过稀不宜单独成策略。
           落地建议: 作为 AuctionGapUpVol(Task#95)的仓位加权标记——当高开+hour1放量
           标的恰好"昨日涨停"时给予更高仓位权重, 而非拆分为独立信号。
=======================================================================================

合规 (rule2):
  - 买入价 = hour1_open (9:30 开盘价, 竞价后即确定)。
  - hour1_amount 于 hour1 结束(10:00)确认; V3引擎 hour=1 循环在 hour1 结束后执行
    should_buy, 此刻 hour1_open 与 hour1_amount 均已知, 用 hour1_open 成交 => 合规。
  - T+1 交易: 当日买入不可卖, 卖出时点仅 T+1 及以后。

选股条件:
  - 昨日涨停: tm1.close >= round(tm1.preclose * (1+涨停比), 2)
  - 今日高开>=GAP_MIN%: (open - preclose)/preclose*100 >= GAP_MIN
  - 今日 hour1_amount >= 昨日 hour1_amount * VOL_RATIO
  - 创业板(sz.300/sz.301) 或 科创板(sh.688)
  - 流通市值 50-300 亿
  - 排除 ST
  - 排除今日开盘即涨停 (无法买入)
  - 排除昨日一字涨停 (tm1 开盘即涨停 => 次日大概率继续一字, 无实操意义)

用法:
  python3 strategy_limitup_next_vol.py [YYYY-MM]   # 单月扫描+明细+分析 (默认2026-04)
  python3 strategy_limitup_next_vol.py fullcycle    # 2021-2026 全周期收益统计
  python3 strategy_limitup_next_vol.py optimize     # 参数敏感性寻优(gap/量比网格)

输出:
  日志: /home/AIWealth/scripts/logs/limitup_next_vol.log
"""
import sqlite3
import sys
import os
from collections import defaultdict

DB_PATH = "/home/AIWealth/data/stocks.db"
LOG_PATH = "/home/AIWealth/scripts/logs/limitup_next_vol.log"

# ===================== 可配置参数区 =====================
GAP_MIN = 2.0            # 今日高开阈值 (%): (open-preclose)/preclose*100 >= GAP_MIN
VOL_RATIO = 2.0          # hour1 量比基础门槛: today_hour1_amount >= yesterday_hour1_amount * VOL_RATIO
MCAP_MIN = 50.0          # 流通市值下限 (亿)
MCAP_MAX = 300.0         # 流通市值上限 (亿)
DETAIL_WINDOW = 5        # 明细打印: 前5后5日
# 收益分析网格
BUY_POINTS = ['h1_open', 'h2_open']          # 买入时点 (h2_open 仅作对照)
TP_LIST = [3, 5, 8, 10, 15]                  # 止盈 (%)
SL_LIST = [-3, -5, -8]                        # 止损 (%)
# 寻优网格
OPT_GAPS = [2, 3, 5]                          # 高开幅度
OPT_VOLS = [2, 3, 5, 7]                       # hour1 量比
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


def is_yiziban(code, row):
    """昨日一字涨停: 开盘即封涨停且收盘也涨停 (open==close==涨停价, 近似)。"""
    return (is_limit_up(code, row['close'], row['preclose']) and
            is_open_limit_up(code, row['open'], row['preclose']))


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
    """加载所有创业板+科创板股票全历史, 按 code 分组, 按 date 排序。"""
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
    """在 [start_date, end_date] 找出所有符合条件的信号 (T日)。"""
    signals = []
    for code, rows in board_data.items():
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
            # 昨日涨停 (核心区别: 与 Task#95 的"昨日非涨停"相反)
            if not is_limit_up(code, tm1['close'], tm1['preclose']):
                continue
            # 排除昨日一字涨停 (次日大概率继续一字, 无实操意义)
            if is_yiziban(code, tm1):
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
    """从 T+1 hour1 起逐小时模拟止盈止损 (T+1 合规)。未触及则持有到 T+2 尾盘平仓。"""
    rows = sig['rows']
    i = sig['idx']
    tp_price = buy_price * (1 + tp / 100)
    sl_price = buy_price * (1 + sl / 100)
    for day_off in (1, 2):
        j = i + day_off
        if j >= len(rows):
            break
        r = rows[j]
        for h in ('hour1', 'hour2', 'hour3', 'hour4'):
            hi_p = r[h + '_high']
            lo_p = r[h + '_low']
            if hi_p is None or lo_p is None:
                continue
            # 保守: 同一小时内若同时触及, 先判止损 (最坏情况)
            if lo_p <= sl_price:
                return (sl / 100) * 100, f"T+{day_off} {h} 止损"
            if hi_p >= tp_price:
                return (tp / 100) * 100, f"T+{day_off} {h} 止盈"
        if day_off == 2 and r['hour4_close'] is not None:
            return (r['hour4_close'] - buy_price) / buy_price * 100, "T+2 尾盘平仓"
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


def run_month(month):
    start = month + "-01"
    end = month + "-31"
    conn = sqlite3.connect(DB_PATH)
    print("=" * 70)
    print(f"Task#104 涨停次日高开+hour1量比确认  单月扫描: {month}")
    print(f"条件: 昨日涨停(排一字) & 高开>={GAP_MIN}% & hour1量比>={VOL_RATIO}x "
          f"& 创业板/科创板 & 市值{MCAP_MIN:.0f}-{MCAP_MAX:.0f}亿 & 排ST & 排开盘涨停")
    print("=" * 70)
    board = load_board_data(conn)
    conn.close()
    print(f"创业板+科创板股票数: {len(board)}")
    signals = find_signals(board, start, end)
    print(f"\n{month} 候选信号数: {len(signals)}")
    print("\n" + "=" * 70)
    print("Step2: 候选股前5后5日 hour级 OCHL 明细 (rate%)")
    print("=" * 70)
    for s in signals:
        print_detail(s)
    analyze(signals, month)


def run_fullcycle():
    conn = sqlite3.connect(DB_PATH)
    print("=" * 70)
    print("Task#104 涨停次日高开+hour1量比确认  全周期 2021-2026 收益验证")
    print(f"条件: 昨日涨停(排一字) & 高开>={GAP_MIN}% & hour1量比>={VOL_RATIO}x "
          f"& 创业板/科创板 & 市值{MCAP_MIN:.0f}-{MCAP_MAX:.0f}亿")
    print("=" * 70)
    board = load_board_data(conn)
    conn.close()
    print(f"创业板+科创板股票数: {len(board)}")
    signals = find_signals(board, "2021-01-01", "2026-06-30")
    print(f"全周期候选信号数: {len(signals)}")
    yearly = defaultdict(list)
    for s in signals:
        yearly[s['date'][:4]].append(s)
    print("\n逐年信号分布:")
    for y in sorted(yearly):
        print(f"  {y}: {len(yearly[y])} 笔")
    analyze(signals, "全周期 2021-2026")
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
    """参数敏感性寻优: 基于最宽基础条件(gap>=2,vol>=2)扫描, 内存中按更高阈值二次过滤。"""
    conn = sqlite3.connect(DB_PATH)
    print("=" * 70)
    print("Task#104 参数敏感性寻优 2021-2026 (买h1_open, 卖T+1尾盘, 严格T+1合规)")
    print("=" * 70)
    board = load_board_data(conn)
    conn.close()
    base = find_signals(board, "2021-01-01", "2026-06-30")
    print(f"基础信号数(gap>={GAP_MIN} & vol>={VOL_RATIO}): {len(base)}\n")
    print(f"  {'高开>=':<8}{'量比>=':<8}{'样本':<7}{'平均收益':<10}{'胜率':<9}{'逐年是否全正':<14}")
    print("  " + "-" * 58)
    best = None
    for gmin in OPT_GAPS:
        for vmin in OPT_VOLS:
            subset = [s for s in base if s['gap'] >= gmin and s['vol_ratio'] >= vmin]
            if len(subset) < 20:
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
            if st['win'] >= 55 and (best is None or st['avg'] > best[1]['avg']):
                best = ((gmin, vmin), st, all_pos)
    print()
    if best:
        (gmin, vmin), st, allp = best
        print(f"  ★ 胜率>=55%中收益最高: 高开>={gmin}% 量比>={vmin}x "
              f"→ {st['avg']:+.2f}% 胜率{st['win']:.1f}% n={st['n']} "
              f"{'(逐年全正)' if allp else ''}")
    else:
        print("  未找到胜率>=55%的参数组合。")


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
