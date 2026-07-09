#!/usr/bin/env python3
"""
Task #71: 首板涨停次日高开接力策略研究 (rule2 流程)

策略思路:
  昨日首板涨停(非连板) + 今日高开 = 打板次日溢价效应。
  游资打板后 T+1 获利退出, 若次日竞价强势高开说明有新资金接力。

信号定义 (T+0 合规):
  - T-1(昨日): 首板涨停  round(close/preclose,2) >= 涨停阈值, 且 T-2 未涨停(确认首板非连板)
  - T(今日):   高开 open_rate >= 阈值, 非ST, 非涨停开盘, 非一字板
  - 买入:      today hour1_open (仅用昨日已确定涨停 + 今日9:25竞价open, 不含未来数据)
                另测 hour2_open 时机

涨停阈值:
  主板(sh.60/sz.00)      0.10
  创业板(sz.300/301)     0.20
  科创板(sh.688)         0.20

分组维度: 板块 × 市值(<50亿/50-200亿/200-700亿) × 高开幅度(>=1/2/3/5%)

用法:
  python3 research_firstboard_next_r2.py 2024-09   # 单月(打印候选股hour明细)
  python3 research_firstboard_next_r2.py 2024       # 整年(仅汇总)
  python3 research_firstboard_next_r2.py all        # 2021-2026 全周期
"""
import sys
import sqlite3
import math
from collections import defaultdict

DB_PATH = '/home/AIWealth/data/stocks.db'
LOG_PATH = '/home/AIWealth/scripts/logs/firstboard_next_r2.log'

GAP_THRESHOLDS = [1.0, 2.0, 3.0, 5.0]   # 高开幅度测试点(%)
MIN_GAP = 1.0                            # 最低入选高开幅度
MAX_HOLD_DAYS = 5                        # 前向模拟最大持仓天数
MAX_DETAIL_PER_DAY = 5                   # 单日最多打印hour明细的候选数

# 市值分组(亿元)
MCAP_BUCKETS = [
    ('<50亿',   0,   50),
    ('50-200亿', 50,  200),
    ('200-700亿', 200, 700),
]

# 12 种止盈止损+持仓期配置: (名称, 止盈%, 止损%, 持仓天数)  None 表示不设
CONFIGS = [
    ('TP3_SL3_H1',  3.0,  3.0, 1),
    ('TP3_SL3_H2',  3.0,  3.0, 2),
    ('TP5_SL3_H1',  5.0,  3.0, 1),
    ('TP5_SL3_H2',  5.0,  3.0, 2),
    ('TP5_SL5_H2',  5.0,  5.0, 2),
    ('TP5_SL5_H3',  5.0,  5.0, 3),
    ('TP8_SL5_H2',  8.0,  5.0, 2),
    ('TP8_SL5_H3',  8.0,  5.0, 3),
    ('TP8_SL8_H3',  8.0,  8.0, 3),
    ('TP10_SL5_H3', 10.0, 5.0, 3),
    ('TP3_SL5_H1',  3.0,  5.0, 1),
    ('NoTP_NoSL_H1', None, None, 1),   # 基线: 纯T+1次日收盘退出
]

_LOG_FH = None


def out(msg=''):
    """同时写入日志文件与stdout"""
    print(msg)
    if _LOG_FH:
        _LOG_FH.write(msg + '\n')


# ---------------- 涨停/合规判定 ----------------

def get_limit_ratio(code):
    if code.startswith('sz.300') or code.startswith('sz.301'):
        return 0.20
    if code.startswith('sh.688'):
        return 0.20
    return 0.10


def get_board(code):
    if code.startswith('sh.688'):
        return '科创板'
    if code.startswith('sz.300') or code.startswith('sz.301'):
        return '创业板'
    if code.startswith('sh.60') or code.startswith('sz.00'):
        return '主板'
    return '其他'


def is_limit_up(close, preclose, code):
    """涨停判定: round(close/preclose,2) >= 1+ratio"""
    if preclose is None or preclose <= 0 or close is None:
        return False
    return round(close / preclose, 2) >= round(1 + get_limit_ratio(code), 2)


def is_yizi(o, h, l, c):
    """一字板: 四价极接近, 无法成交"""
    vals = [o, h, l, c]
    if any(v is None for v in vals):
        return True
    return (max(vals) - min(vals)) < 0.01


def calc_mcap(amount, turn):
    """流通市值(亿元) = amount * 100 / turn / 1e8"""
    if amount is None or turn is None or turn <= 0:
        return None
    return amount * 100.0 / turn / 1e8


def mcap_bucket(mcap):
    if mcap is None:
        return None
    for name, lo, hi in MCAP_BUCKETS:
        if lo <= mcap < hi:
            return name
    return None  # 超出700亿不纳入


# ---------------- 交易日/数据 ----------------

def get_all_trading_days(cur):
    cur.execute("SELECT DISTINCT date FROM stock_kline ORDER BY date")
    return [r[0] for r in cur.fetchall()]


def days_for_period(all_days, period):
    """根据 period(YYYY-MM / YYYY / all) 返回目标交易日列表"""
    if period == 'all':
        return [d for d in all_days if d >= '2021-01-01']
    return [d for d in all_days if d.startswith(period)]


HOUR_COLS = (
    "hour1_open,hour1_high,hour1_low,hour1_close,"
    "hour2_open,hour2_high,hour2_low,hour2_close,"
    "hour3_open,hour3_high,hour3_low,hour3_close,"
    "hour4_open,hour4_high,hour4_low,hour4_close"
)


def find_candidates(cur, today, yesterday, dby):
    """找出当日候选: 昨日首板涨停 + 今日高开>=MIN_GAP + 合规"""
    # 昨日(信号日)数据: 涨停判定 + 市值
    cur.execute("""
        SELECT code, code_name, close, preclose, amount, turn, isST
        FROM stock_kline WHERE date=? AND preclose>0
    """, (yesterday,))
    yd_map = {}
    for code, name, close, preclose, amount, turn, isST in cur.fetchall():
        yd_map[code] = (name, close, preclose, amount, turn, isST)

    # 前日数据(确认首板: 前日未涨停)
    cur.execute("SELECT code, close, preclose FROM stock_kline WHERE date=? AND preclose>0", (dby,))
    dby_map = {r[0]: (r[1], r[2]) for r in cur.fetchall()}

    # 今日数据
    cur.execute(f"""
        SELECT code, code_name, open, high, low, close, preclose, isST, {HOUR_COLS}
        FROM stock_kline WHERE date=?
    """, (today,))
    today_map = {r[0]: r for r in cur.fetchall()}

    candidates = []
    for code, (yname, yclose, ypreclose, yamount, yturn, yisST) in yd_map.items():
        # 1. 昨日首板涨停
        if not is_limit_up(yclose, ypreclose, code):
            continue
        # 2. 前日未涨停(确认首板)
        if code not in dby_map:
            continue
        dclose, dpreclose = dby_map[code]
        if dpreclose and dpreclose > 0 and is_limit_up(dclose, dpreclose, code):
            continue  # 连板, 跳过
        # 3. 今日数据
        if code not in today_map:
            continue
        t = today_map[code]
        (_c, tname, t_open, t_high, t_low, t_close, t_preclose, t_isST) = t[:8]
        h = t[8:]  # 16 个 hour 字段
        # 排除ST
        if t_isST or yisST or (tname and 'ST' in tname.upper()):
            continue
        if t_preclose is None or t_preclose <= 0 or t_open is None or t_open <= 0:
            continue
        # 4. 高开幅度
        open_rate = (t_open - t_preclose) / t_preclose * 100
        if open_rate < MIN_GAP:
            continue
        # 5. 排除涨停开盘(无法买入)
        if is_limit_up(t_open, t_preclose, code):
            continue
        # 6. 排除一字板(hour1无法成交)
        h1 = (h[0], h[1], h[2], h[3])
        if is_yizi(*h1):
            continue
        h1_open = h[0]
        h2_open = h[4]
        if h1_open is None or h1_open <= 0:
            continue

        candidates.append({
            'code': code, 'name': tname or '',
            'board': get_board(code),
            'mcap': calc_mcap(yamount, yturn),
            'open_rate': open_rate,
            'yd_pct': (yclose - ypreclose) / ypreclose * 100,
            't_open': t_open, 't_close': t_close,
            'h1_open': h1_open, 'h2_open': h2_open,
        })

    candidates.sort(key=lambda x: x['open_rate'], reverse=True)
    return candidates


def get_window_rows(cur, code, days):
    if not days:
        return []
    ph = ','.join(['?'] * len(days))
    cur.execute(f"""
        SELECT date, open, high, low, close, preclose, {HOUR_COLS}
        FROM stock_kline WHERE code=? AND date IN ({ph}) ORDER BY date
    """, [code] + days)
    return cur.fetchall()


def simulate_exit(buy_price, fwd_rows, tp_pct, sl_pct, hold_days, code):
    """
    T+1 前向模拟退出。fwd_rows: 买入日之后的交易日行(升序), 每行含 hour OHLC。
    逐hour扫描止盈止损; 同一bar内触及止损优先(保守)。
    未触发则在第 hold_days 天 hour4_close 退出。
    返回 (ret_pct, hold_used) 或 None。
    """
    if buy_price is None or buy_price <= 0 or not fwd_rows:
        return None
    tp_price = buy_price * (1 + tp_pct / 100.0) if tp_pct is not None else None
    sl_price = buy_price * (1 - sl_pct / 100.0) if sl_pct is not None else None

    hold_rows = fwd_rows[:hold_days]
    last_close = None
    for di, row in enumerate(hold_rows):
        # row: date,open,high,low,close,preclose, h1o,h1h,h1l,h1c, h2..., h3..., h4...
        preclose = row[5]
        close = row[4]
        hours = [
            (row[6], row[7], row[8], row[9]),
            (row[10], row[11], row[12], row[13]),
            (row[14], row[15], row[16], row[17]),
            (row[18], row[19], row[20], row[21]),
        ]
        for (ho, hh, hl, hc) in hours:
            if ho is None or hh is None or hl is None:
                continue
            # 止损优先(保守)
            if sl_price is not None and hl <= sl_price:
                return ((sl_price - buy_price) / buy_price * 100, di + 1)
            if tp_price is not None and hh >= tp_price:
                return ((tp_price - buy_price) / buy_price * 100, di + 1)
        if close is not None:
            last_close = close
    # 到期收盘退出
    if last_close is not None:
        return ((last_close - buy_price) / buy_price * 100, len(hold_rows))
    return None


# ---------------- hour 明细打印 ----------------

def print_hour_detail(cur, code, all_days, today):
    idx = all_days.index(today)
    win = all_days[max(0, idx - 5): min(len(all_days), idx + 6)]
    rows = get_window_rows(cur, code, win)
    if not rows:
        return
    out(f"    {'日期':<11}|{'hr':<3}|{'open':>8}|{'high':>8}|{'low':>8}|{'close':>8}| chg%")
    out(f"    {'-'*11}+{'-'*3}+{'-'*8}+{'-'*8}+{'-'*8}+{'-'*8}+------")
    for row in rows:
        date = row[0]
        hours = [('h1', row[6], row[7], row[8], row[9]),
                 ('h2', row[10], row[11], row[12], row[13]),
                 ('h3', row[14], row[15], row[16], row[17]),
                 ('h4', row[18], row[19], row[20], row[21])]
        for hn, ho, hh, hl, hc in hours:
            if ho is None or hc is None:
                continue
            pct = (hc - ho) / ho * 100 if ho > 0 else 0
            mark = '  <=买入' if (date == today and hn == 'h1') else ''
            out(f"    {date:<11}|{hn:<3}|{ho:>8.2f}|{hh:>8.2f}|{hl:>8.2f}|{hc:>8.2f}|{pct:+6.2f}{mark}")


# ---------------- 主流程 ----------------

def run(period):
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    all_days = get_all_trading_days(cur)
    all_idx = {d: i for i, d in enumerate(all_days)}
    target_days = days_for_period(all_days, period)
    if not target_days:
        out(f"错误: 未找到 {period} 的交易日数据")
        conn.close()
        return

    print_detail = (len(period) == 7 and period[4] == '-')  # 仅单月打印明细

    out('=' * 90)
    out(f"首板涨停次日高开接力策略研究 (Task #71)")
    out(f"研究区间: {period}  交易日数: {len(target_days)}  ({target_days[0]} ~ {target_days[-1]})")
    out(f"最低高开: >={MIN_GAP}%  |  明细打印: {'开启' if print_detail else '关闭(仅汇总)'}")
    out('=' * 90)

    # 收集所有交易样本: 每条记录含分组维度 + 各buy时机的各config收益
    # samples[i] = dict(board, mcap_bkt, open_rate, year, h1_ret{cfg}, h2_ret{cfg}, ...)
    samples = []
    daily_count = []

    for today in target_days:
        idx = all_idx.get(today)
        if idx is None or idx < 2:
            continue
        yesterday = all_days[idx - 1]
        dby = all_days[idx - 2]
        cands = find_candidates(cur, today, yesterday, dby)
        daily_count.append((today, len(cands)))
        if not cands:
            continue

        if print_detail:
            out(f"\n{'='*60}\n===== {today}  候选股 {len(cands)} 只 =====\n{'='*60}")
            out(f"  {'代码':<11}{'名称':<9}{'板块':<7}{'市值(亿)':>9}{'高开%':>8}{'昨涨%':>8}")
            for c in cands:
                mc = f"{c['mcap']:.1f}" if c['mcap'] else 'NA'
                out(f"  {c['code']:<11}{c['name']:<9}{c['board']:<7}{mc:>9}{c['open_rate']:>8.2f}{c['yd_pct']:>8.2f}")

        # 前向窗口(买入日之后 MAX_HOLD_DAYS 天)
        fwd_days = all_days[idx + 1: idx + 1 + MAX_HOLD_DAYS]

        for ci, c in enumerate(cands):
            fwd_rows = get_window_rows(cur, c['code'], fwd_days) if fwd_days else []
            year = today[:4]
            rec = {
                'date': today, 'year': year,
                'board': c['board'],
                'mcap_bkt': mcap_bucket(c['mcap']),
                'open_rate': c['open_rate'],
                'h1_cfg': {}, 'h2_cfg': {},
            }
            for cfg_name, tp, sl, hold in CONFIGS:
                r1 = simulate_exit(c['h1_open'], fwd_rows, tp, sl, hold, c['code'])
                rec['h1_cfg'][cfg_name] = r1
                if c['h2_open'] and c['h2_open'] > 0:
                    r2 = simulate_exit(c['h2_open'], fwd_rows, tp, sl, hold, c['code'])
                    rec['h2_cfg'][cfg_name] = r2
                else:
                    rec['h2_cfg'][cfg_name] = None
            samples.append(rec)

            # 明细打印(仅单月, 每日前N只)
            if print_detail and ci < MAX_DETAIL_PER_DAY:
                out(f"\n  --- {c['code']} {c['name']} [{c['board']}] 高开{c['open_rate']:+.2f}% ---")
                print_hour_detail(cur, c['code'], all_days, today)

    conn.close()

    out(f"\n\n{'#'*90}")
    out(f"汇总分析 (总样本 {len(samples)} 笔)")
    out(f"{'#'*90}")

    analyze(samples, daily_count, period)


def _stats(rets):
    """返回 (n, avg, median, winrate, avg_abs) — rets 为收益率列表(%)"""
    vals = [r for r in rets if r is not None]
    if not vals:
        return None
    n = len(vals)
    avg = sum(vals) / n
    srt = sorted(vals)
    med = srt[n // 2]
    win = sum(1 for v in vals if v > 0) / n * 100
    return (n, avg, med, win)


def analyze(samples, daily_count, period):
    if not samples:
        out("无样本, 结束。")
        return

    # ---- 信号频率 ----
    days_with = sum(1 for _, n in daily_count if n > 0)
    total_days = len(daily_count)
    total_sig = sum(n for _, n in daily_count)
    out(f"\n[信号频率] 交易日 {total_days}, 有信号日 {days_with}, "
        f"总信号 {total_sig}, 日均 {total_sig/max(1,total_days):.1f} 只")

    # ---- 买入时机对比 (h1_open vs h2_open), 用基线config NoTP_NoSL_H1 ----
    out(f"\n[买入时机对比] (次日收盘退出基线 NoTP_NoSL_H1)")
    h1b = _stats([s['h1_cfg'].get('NoTP_NoSL_H1') and s['h1_cfg']['NoTP_NoSL_H1'][0] for s in samples])
    h2b = _stats([s['h2_cfg'].get('NoTP_NoSL_H1') and s['h2_cfg']['NoTP_NoSL_H1'][0] for s in samples])
    if h1b:
        out(f"  h1_open买入: n={h1b[0]} 均值{h1b[1]:+.2f}% 中位{h1b[2]:+.2f}% 胜率{h1b[3]:.1f}%")
    if h2b:
        out(f"  h2_open买入: n={h2b[0]} 均值{h2b[1]:+.2f}% 中位{h2b[2]:+.2f}% 胜率{h2b[3]:.1f}%")

    # ---- 止盈触及率 (基于 h1_open, 前向MAX_HOLD天内 hour_high 触及) ----
    # 用 NoTP 的持仓路径不便; 直接用配置 TP 命中率近似: 统计各TP config正收益中达到TP的比例
    out(f"\n[止盈触及率] (h1_open买入, {MAX_HOLD_DAYS}日内)")
    for tp in [3.0, 5.0, 8.0]:
        hit = 0
        tot = 0
        for s in samples:
            r = s['h1_cfg'].get(f'TP{int(tp)}_SL5_H3') or s['h1_cfg'].get(f'TP{int(tp)}_SL5_H2') \
                or s['h1_cfg'].get(f'TP{int(tp)}_SL3_H2')
            if r is None:
                continue
            tot += 1
            if r[0] >= tp - 0.01:
                hit += 1
        if tot:
            out(f"  +{tp:.0f}% 触及率: {hit/tot*100:.1f}% ({hit}/{tot})")

    # ---- 最大回撤(单笔最差收益)分布, h1_open + 基线 ----
    out(f"\n[单笔收益分布] (h1_open, NoTP_NoSL_H1 基线)")
    base_rets = sorted([s['h1_cfg']['NoTP_NoSL_H1'][0] for s in samples if s['h1_cfg'].get('NoTP_NoSL_H1')])
    if base_rets:
        n = len(base_rets)
        def pctile(p):
            return base_rets[min(n - 1, int(n * p))]
        out(f"  P5={pctile(0.05):+.2f}% P25={pctile(0.25):+.2f}% 中位={pctile(0.5):+.2f}% "
            f"P75={pctile(0.75):+.2f}% P95={pctile(0.95):+.2f}%  最差={base_rets[0]:+.2f}%")

    # ---- 12种配置对比 (全样本, h1_open) ----
    out(f"\n[12种配置对比] (h1_open买入, 全样本)")
    out(f"  {'配置':<15}{'n':>6}{'均值%':>9}{'中位%':>9}{'胜率%':>8}{'均持仓':>8}{'月化%':>9}")
    for cfg_name, tp, sl, hold in CONFIGS:
        rows = [s['h1_cfg'].get(cfg_name) for s in samples]
        st = _stats([r[0] if r else None for r in rows])
        if not st:
            continue
        holds = [r[1] for r in rows if r]
        avg_hold = sum(holds) / len(holds) if holds else hold
        # 月化: 单笔均值按 21/持仓天数 次线性外推
        monthly = st[1] * (21.0 / max(0.5, avg_hold))
        out(f"  {cfg_name:<15}{st[0]:>6}{st[1]:>+9.2f}{st[2]:>+9.2f}{st[3]:>8.1f}{avg_hold:>8.2f}{monthly:>+9.1f}")

    # ---- 分组分析: 板块 × 市值 × 高开幅度 × 配置, 找达标子集 ----
    out(f"\n[分组寻优] 目标: 胜率>55% 且 月化>10%  (h1_open, 各高开阈值累计)")
    out(f"  {'板块':<7}{'市值':<10}{'高开>=':<8}{'配置':<15}{'n':>6}{'胜率%':>8}{'均值%':>8}{'月化%':>9}")

    qualified = []
    boards = ['主板', '创业板', '科创板']
    for board in boards:
        for mc_name, _, _ in MCAP_BUCKETS:
            for gap_th in GAP_THRESHOLDS:
                subset = [s for s in samples
                          if s['board'] == board and s['mcap_bkt'] == mc_name
                          and s['open_rate'] >= gap_th]
                if len(subset) < 20:  # 样本太少不评估
                    continue
                for cfg_name, tp, sl, hold in CONFIGS:
                    rows = [s['h1_cfg'].get(cfg_name) for s in subset]
                    st = _stats([r[0] if r else None for r in rows])
                    if not st or st[0] < 20:
                        continue
                    holds = [r[1] for r in rows if r]
                    avg_hold = sum(holds) / len(holds) if holds else hold
                    monthly = st[1] * (21.0 / max(0.5, avg_hold))
                    if st[3] > 55.0 and monthly > 10.0:
                        qualified.append((board, mc_name, gap_th, cfg_name, st[0], st[3], st[1], monthly, avg_hold))

    # 达标子集按月化降序
    qualified.sort(key=lambda x: x[7], reverse=True)
    if qualified:
        for q in qualified[:30]:
            out(f"  {q[0]:<7}{q[1]:<10}{q[2]:<8.0f}{q[3]:<15}{q[4]:>6}{q[5]:>8.1f}{q[6]:>+8.2f}{q[7]:>+9.1f}")
    else:
        out("  无满足 胜率>55% 且 月化>10% 的子集 (按当前分组阈值)")

    # ---- 重点关注: 创业板 50-200亿 + 高开>=3% ----
    out(f"\n[重点关注] 创业板 50-200亿 + 高开>=3%")
    focus = [s for s in samples if s['board'] == '创业板'
             and s['mcap_bkt'] == '50-200亿' and s['open_rate'] >= 3.0]
    out(f"  样本数: {len(focus)}")
    if focus:
        for cfg_name, tp, sl, hold in CONFIGS:
            rows = [s['h1_cfg'].get(cfg_name) for s in focus]
            st = _stats([r[0] if r else None for r in rows])
            if not st:
                continue
            holds = [r[1] for r in rows if r]
            avg_hold = sum(holds) / len(holds) if holds else hold
            monthly = st[1] * (21.0 / max(0.5, avg_hold))
            flag = '  ★达标' if (st[3] > 55.0 and monthly > 10.0) else ''
            out(f"    {cfg_name:<15} n={st[0]:>5} 胜率{st[3]:>5.1f}% 均值{st[1]:>+6.2f}% 月化{monthly:>+7.1f}%{flag}")

    # ---- 逐年稳定性 (最优达标配置, 若无则用全样本最佳月化config) ----
    out(f"\n[逐年稳定性]")
    if qualified:
        best = qualified[0]
        best_board, best_mc, best_gap, best_cfg = best[0], best[1], best[2], best[3]
        out(f"  基于达标最优子集: {best_board} {best_mc} 高开>={best_gap:.0f}% {best_cfg}")
        yearly_subset = lambda yr: [s for s in samples if s['year'] == yr
                                    and s['board'] == best_board and s['mcap_bkt'] == best_mc
                                    and s['open_rate'] >= best_gap]
        cfg_use = best_cfg
    else:
        # 全样本下月化最佳config
        best_cfg = None
        best_m = -1e9
        for cfg_name, tp, sl, hold in CONFIGS:
            rows = [s['h1_cfg'].get(cfg_name) for s in samples]
            st = _stats([r[0] if r else None for r in rows])
            if not st:
                continue
            holds = [r[1] for r in rows if r]
            avg_hold = sum(holds) / len(holds) if holds else hold
            m = st[1] * (21.0 / max(0.5, avg_hold))
            if m > best_m:
                best_m = m
                best_cfg = cfg_name
        out(f"  无达标子集, 采用全样本最佳月化配置: {best_cfg}")
        yearly_subset = lambda yr: [s for s in samples if s['year'] == yr]
        cfg_use = best_cfg

    years = sorted(set(s['year'] for s in samples))
    out(f"  {'年份':<8}{'n':>6}{'胜率%':>8}{'均值%':>9}{'月化%':>9}")
    for yr in years:
        sub = yearly_subset(yr)
        rows = [s['h1_cfg'].get(cfg_use) for s in sub]
        st = _stats([r[0] if r else None for r in rows])
        if not st:
            out(f"  {yr:<8}{'0':>6}{'-':>8}{'-':>9}{'-':>9}")
            continue
        holds = [r[1] for r in rows if r]
        avg_hold = sum(holds) / len(holds) if holds else 1
        monthly = st[1] * (21.0 / max(0.5, avg_hold))
        out(f"  {yr:<8}{st[0]:>6}{st[3]:>8.1f}{st[1]:>+9.2f}{monthly:>+9.1f}")

    # ---- 最终结论 ----
    out(f"\n{'='*90}\n[最终结论]")
    if qualified:
        b = qualified[0]
        out(f"  ✔ 找到达标最优配置:")
        out(f"    子集: {b[0]} {b[1]} 高开>={b[2]:.0f}%  |  退出: {b[3]}")
        out(f"    样本 n={b[4]}  胜率 {b[5]:.1f}%  单笔均值 {b[6]:+.2f}%  月化 {b[7]:+.1f}%  均持仓 {b[8]:.2f}日")
        out(f"    达标线: 胜率>55% ✔ 且 月化>10% ✔")
        out(f"  候选达标子集共 {len(qualified)} 个(见上表), 建议取样本量充足者进入回测引擎对接阶段。")
    else:
        out(f"  ✘ 未找到同时满足 胜率>55% 且 月化>10% 的子集。")
        out(f"    建议: 收紧高开幅度/板块/市值组合, 或引入 hour1 收阳等二次确认过滤。")
    out('=' * 90)


def main():
    global _LOG_FH
    if len(sys.argv) < 2:
        print("用法: python3 research_firstboard_next_r2.py <2024-09|2024|all>")
        sys.exit(1)
    period = sys.argv[1]
    _LOG_FH = open(LOG_PATH, 'w', encoding='utf-8')
    try:
        run(period)
    finally:
        _LOG_FH.close()


if __name__ == '__main__':
    main()
