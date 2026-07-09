#!/usr/bin/env python3
"""大盘暴跌日超跌反弹策略 - Phase1 研究脚本 (Task #76)

用法:
    python3 scripts/research_panic_dip_r2.py 2024      # 只跑 2024 年
    python3 scripts/research_panic_dip_r2.py all       # 跑 2020-2026 全周期
    python3 scripts/research_panic_dip_r2.py 2024 nodetail   # 不打印逐股明细

策略思路:
    大盘(上证 sh.000001)单日暴跌时, 所有股票恐慌性普跌。
    次日跌幅最深的小票反弹弹性最大 —— 恐慌释放后的均值回归(恐慌底吸)。

条件定义:
    暴跌日: sh.000001 当日 (close-preclose)/preclose <= -TH (TH=1.5%/2%/3%)
    超跌选股(暴跌日收盘后):
        板块: 创业板(sz.30/301) + 科创板(sh.688)
        市值: <50亿 / 50-200亿 (mcap = amount*100/turn/1e8, 单位亿)
        当日跌幅: 板块内排名前20%(最深) 或 绝对跌幅(创板>8% / 科创>10%)
        排除: ST, 跌停(明日可能继续跌停无法买入)
    次日操作(T+1):
        买入: T+1 hour1_open (或等回调 hour2_open)
        条件: T+1 open 不是跌停价(能买入)

T+0 合规:
    - 暴跌日/超跌判定 → 暴跌日收盘后已确定
    - 买入价 = T+1 hour1_open / hour2_open → 真实可执行价
    - 前视窗口仅用于统计反弹/回撤, 不用于选股
"""

import sqlite3
import sys
from collections import defaultdict

# ============================================================
# 配置
# ============================================================
DB_PATH = '/home/AIWealth/data/stocks.db'
LOG_PATH = '/home/AIWealth/scripts/logs/panic_dip_r2.log'
INDEX_CODE = 'sh.000001'

ARG = sys.argv[1] if len(sys.argv) > 1 else '2024'
PRINT_DETAIL = not (len(sys.argv) > 2 and sys.argv[2] == 'nodetail')

WINDOW_BEFORE = 5
WINDOW_AFTER = 5
HOLD_DAYS = 5              # 前视持有窗口 (T1..T1+4) 用于止盈/回撤统计
DETAIL_PER_DAY = 6        # 每个暴跌日最多打印明细的候选股数(按跌幅最深排序)

# 暴跌阈值(百分比, 正数): index 跌幅 <= -TH
CRASH_THRESHOLDS = [1.5, 2.0, 3.0, 4.0]
MASTER_TH = 1.5           # 主筛选阈值(最宽), 其余阈值在内存中过滤

# 绝对超跌阈值(百分比)
ABS_DROP_GEM = 8.0        # 创业板绝对跌幅
ABS_DROP_STAR = 10.0      # 科创板绝对跌幅

# 止盈档位
TP_LEVELS = [3, 5, 8, 10]

# ============================================================
# 涨跌停计算 (创业板/科创板均为 20%)
# ============================================================

def get_ratio(code):
    if code.startswith('sz.30') or code.startswith('sh.68'):
        return 0.20
    elif code.startswith('bj.') or code.startswith('43') or code.startswith('83') or code.startswith('87'):
        return 0.30
    return 0.10


def calc_limit_down(preclose, ratio):
    return round(preclose * (1 - ratio), 2)


def is_limit_down(close, preclose, code):
    if preclose is None or preclose == 0 or close is None:
        return False
    try:
        return float(close) <= calc_limit_down(float(preclose), get_ratio(code))
    except (TypeError, ValueError, ZeroDivisionError):
        return False


def board_of(code):
    if code.startswith('sz.30'):
        return 'GEM'    # 创业板
    if code.startswith('sh.68'):
        return 'STAR'   # 科创板
    return None


def calc_mcap(amount, turn):
    """流通市值(亿) = amount * 100 / turn / 1e8"""
    if amount is None or turn is None or turn == 0:
        return None
    try:
        return float(amount) * 100.0 / float(turn) / 1e8
    except (TypeError, ValueError, ZeroDivisionError):
        return None


# ============================================================
# 数据访问
# ============================================================

def get_all_trading_days(conn):
    cur = conn.execute("SELECT DISTINCT date FROM stock_kline ORDER BY date ASC")
    return [r[0] for r in cur.fetchall()]


def get_crash_days(conn, th_pct, year=None):
    """返回 index 当日跌幅 <= -th_pct 的交易日 [(date, ret_pct), ...]"""
    sql = ("SELECT date, close, preclose FROM index_kline "
           "WHERE code = ? AND preclose > 0 ORDER BY date ASC")
    cur = conn.execute(sql, (INDEX_CODE,))
    out = []
    for date, close, preclose in cur.fetchall():
        if close is None or preclose is None or preclose == 0:
            continue
        ret = (float(close) / float(preclose) - 1) * 100
        if ret <= -th_pct:
            if year is None or date.startswith(str(year)):
                out.append((date, ret))
    return out


def fetch_day_board_data(conn, day):
    """获取某日创业板+科创板全部个股行情"""
    cur = conn.execute(
        """SELECT code, code_name, preclose, open, high, low, close, close_rate,
                  amount, turn, isST,
                  hour1_open, hour1_high, hour1_low, hour1_close,
                  hour2_open, hour2_high, hour2_low, hour2_close,
                  hour3_open, hour3_high, hour3_low, hour3_close,
                  hour4_open, hour4_high, hour4_low, hour4_close
           FROM stock_kline
           WHERE date = ? AND (code LIKE 'sz.30%' OR code LIKE 'sh.68%')""",
        (day,),
    )
    cols = [d[0] for d in cur.description]
    return {row[0]: dict(zip(cols, row)) for row in cur.fetchall()}


def fetch_stock_window(conn, code, dates):
    if not dates:
        return {}
    ph = ','.join('?' * len(dates))
    sql = f"""
        SELECT date, open, high, low, close, preclose,
               hour1_open, hour1_high, hour1_low, hour1_close,
               hour2_open, hour2_high, hour2_low, hour2_close,
               hour3_open, hour3_high, hour3_low, hour3_close,
               hour4_open, hour4_high, hour4_low, hour4_close
        FROM stock_kline WHERE code = ? AND date IN ({ph}) ORDER BY date ASC
    """
    cur = conn.execute(sql, [code] + list(dates))
    cols = [d[0] for d in cur.description]
    return {row[0]: dict(zip(cols, row)) for row in cur.fetchall()}


# ============================================================
# 候选股筛选 + 前视指标
# ============================================================

def screen_crash_day(conn, all_days, day_idx, day, index_ret):
    """在暴跌日 day 收盘后筛选超跌候选, 并计算 T+1 买入与前视窗口指标。

    返回候选列表 (已排除 ST / 当日跌停, 但未按 mcap/选法过滤 —— 留给 config)。
    """
    if day_idx + 1 >= len(all_days):
        return []
    t1 = all_days[day_idx + 1]

    day_data = fetch_day_board_data(conn, day)
    t1_data = fetch_day_board_data(conn, t1)
    if not day_data or not t1_data:
        return []

    # 前视窗口日期 (T1..T1+HOLD_DAYS-1)
    fwd_dates = all_days[day_idx + 1: day_idx + 1 + HOLD_DAYS]

    raw = []
    for code, d in day_data.items():
        board = board_of(code)
        if board is None:
            continue
        if d.get('isST') == 1:
            continue
        close = d.get('close')
        preclose = d.get('preclose')
        if close is None or preclose is None or preclose == 0:
            continue
        # 排除当日跌停
        if is_limit_down(close, preclose, code):
            continue
        drop = d.get('close_rate')
        if drop is None:
            drop = (float(close) / float(preclose) - 1) * 100
        drop = float(drop)
        # 只关心下跌的股票 (普跌背景下)
        if drop >= 0:
            continue
        mcap = calc_mcap(d.get('amount'), d.get('turn'))

        t1 = all_days[day_idx + 1]
        t1row = t1_data.get(code)
        if t1row is None:
            continue
        t1_open = t1row.get('hour1_open') or t1row.get('open')
        t1_preclose = t1row.get('preclose')
        if t1_open is None or t1_preclose is None or t1_preclose == 0:
            continue
        # T+1 open 是否跌停(无法买入)
        t1_ld_price = calc_limit_down(float(t1_preclose), get_ratio(code))
        buyable = float(t1_open) > t1_ld_price
        buy_h1 = float(t1row.get('hour1_open')) if t1row.get('hour1_open') is not None else None
        buy_h2 = float(t1row.get('hour2_open')) if t1row.get('hour2_open') is not None else None
        t1_gap = (float(t1_open) / float(t1_preclose) - 1) * 100  # T+1 高/低开

        raw.append({
            'code': code, 'name': d.get('code_name'), 'board': board,
            'drop': drop, 'mcap': mcap,
            'crash_day': day, 'index_ret': index_ret, 't1': t1,
            'close': float(close), 'preclose': float(preclose),
            'buyable': buyable, 'buy_h1': buy_h1, 'buy_h2': buy_h2,
            't1_gap': t1_gap,
            'fwd_dates': fwd_dates,
        })

    # 计算板块内跌幅前20%分位阈值 (最深的20%: drop <= 20分位数)
    for bd in ('GEM', 'STAR'):
        drops = sorted([r['drop'] for r in raw if r['board'] == bd])
        if not drops:
            continue
        idx = max(0, int(len(drops) * 0.20) - 1)
        p20 = drops[idx]  # 第20分位(越负越深)
        for r in raw:
            if r['board'] == bd:
                r['p20_drop'] = p20
                r['is_deep20'] = r['drop'] <= p20

    # 计算前视指标 (T1..T1+HOLD_DAYS 的 high/low/close 相对买入价)
    for r in raw:
        win = fetch_stock_window(conn, r['code'], r['fwd_dates'])
        highs, lows = [], []
        fwd_daily = []   # 逐日时序 (high, low, close), 用于时序止盈/止损判定
        last_close = None
        for dt in r['fwd_dates']:
            row = win.get(dt)
            if not row:
                continue
            hh = float(row['high']) if row.get('high') is not None else None
            ll = float(row['low']) if row.get('low') is not None else None
            cc = float(row['close']) if row.get('close') is not None else None
            fwd_daily.append((hh, ll, cc))
            if hh is not None:
                highs.append(hh)
            if ll is not None:
                lows.append(ll)
            if cc is not None:
                last_close = cc
        r['fwd_daily'] = fwd_daily
        r['fwd_last_close'] = last_close
        r['fwd_max_high'] = max(highs) if highs else None
        r['fwd_min_low'] = min(lows) if lows else None
        # T1 当日
        t1row = win.get(r['t1'], {})
        r['t1_high'] = float(t1row['high']) if t1row.get('high') is not None else None
        r['t1_close'] = float(t1row['close']) if t1row.get('close') is not None else None
        r['t1_low'] = float(t1row['low']) if t1row.get('low') is not None else None
    return raw


# ============================================================
# 明细打印
# ============================================================

def fmt(v, w=7):
    if v is None:
        return '-'.center(w)
    try:
        return f"{float(v):>{w}.2f}"
    except (TypeError, ValueError):
        return str(v).rjust(w)


def print_detail(conn, all_days, day_idx, r):
    code = r['code']
    day = r['crash_day']
    lo = max(0, day_idx - WINDOW_BEFORE)
    hi = min(len(all_days), day_idx + 1 + WINDOW_AFTER)
    win_dates = all_days[lo:hi]
    data = fetch_stock_window(conn, code, win_dates)
    buy = r['buy_h1']
    mcap_s = f"{r['mcap']:.1f}亿" if r['mcap'] else "-"
    print(f"\n  --- {code} {r['name']} [{r['board']}] mcap={mcap_s} ---")
    print(f"    暴跌日跌幅={r['drop']:+.2f}%  T+1开盘={r['t1_gap']:+.2f}%  "
          f"可买={'是' if r['buyable'] else '否(跌停)'}  买入价(h1)={fmt(buy)}")
    print("    日期       |hr| open   | high   | low    | close  | vs买入")
    for dt in win_dates:
        row = data.get(dt)
        if not row:
            continue
        mark = ' <T1买入' if dt == r['t1'] else (' <暴跌日' if dt == day else '')
        for h in range(1, 5):
            ho = row.get(f'hour{h}_open'); hh = row.get(f'hour{h}_high')
            hl = row.get(f'hour{h}_low'); hc = row.get(f'hour{h}_close')
            if all(x is None for x in (ho, hh, hl, hc)):
                continue
            vs = ''
            if buy and hc is not None and buy > 0:
                vs = f"{(float(hc)/buy-1)*100:+.1f}%"
            m = mark if h == 1 else ''
            print(f"    {dt} |h{h}|{fmt(ho)}|{fmt(hh)}|{fmt(hl)}|{fmt(hc)}|{vs:>7s}{m}")


# ============================================================
# 配置评估
# ============================================================

def match_config(r, cfg):
    """判断候选 r 是否满足 config 的筛选条件 (板块/市值/选法/暴跌阈值/可买)"""
    if r['index_ret'] > -cfg['crash_th']:
        return False
    if r['board'] not in cfg['boards']:
        return False
    # 市值
    lo, hi = cfg['mcap']
    if lo is not None or hi is not None:
        if r['mcap'] is None:
            return False
        if lo is not None and r['mcap'] < lo:
            return False
        if hi is not None and r['mcap'] >= hi:
            return False
    # 选法
    if cfg['sel'] == 'abs':
        thr = ABS_DROP_GEM if r['board'] == 'GEM' else ABS_DROP_STAR
        if r['drop'] > -thr:
            return False
    elif cfg['sel'] == 'pct20':
        if not r.get('is_deep20'):
            return False
    # 可买
    if not r['buyable']:
        return False
    return True


def eval_config(cands, cfg):
    """对满足 config 的候选做聚合统计, 含 TP+SL 模拟收益。"""
    tp = cfg['tp']; sl = cfg['sl']; buy_key = cfg['buy']
    n = 0
    sim_rets = []          # TP+SL 模拟收益
    t1_close_rets = []     # T1 当日 close 收益
    rebound_pos = 0        # T1 close > buy
    touched_tp = defaultdict(int)  # 各档止盈触及数
    drawdowns = []         # 窗口内最大回撤 (min_low/buy-1)
    by_year = defaultdict(lambda: {'n': 0, 'ret': 0.0, 'win': 0})

    for r in cands:
        if not match_config(r, cfg):
            continue
        buy = r.get(buy_key)
        if buy is None or buy <= 0:
            continue
        n += 1
        yr = r['crash_day'][:4]

        # T1 close 收益
        if r.get('t1_close'):
            tc = (r['t1_close'] / buy - 1) * 100
            t1_close_rets.append(tc)
            if tc > 0:
                rebound_pos += 1

        # 止盈触及 (窗口内最高价)
        mx = r.get('fwd_max_high')
        if mx:
            mx_ret = (mx / buy - 1) * 100
            for lv in TP_LEVELS:
                if mx_ret >= lv:
                    touched_tp[lv] += 1

        # 回撤 (窗口内最低价)
        mn = r.get('fwd_min_low')
        if mn:
            drawdowns.append((mn / buy - 1) * 100)

        # TP+SL 模拟收益: 保守——若窗口最低触及SL则记SL, 否则若最高触及TP记TP, 否则末日close
        sim = None
        hit_sl = (mn is not None and (mn / buy - 1) * 100 <= sl)
        hit_tp = (mx is not None and (mx / buy - 1) * 100 >= tp)
        if hit_sl:
            sim = sl
        elif hit_tp:
            sim = tp
        elif r.get('t1_close') is not None:
            # 用窗口末日 close 近似离场; 无则用 t1_close
            sim = (r['t1_close'] / buy - 1) * 100
        if sim is not None:
            sim_rets.append(sim)
            by_year[yr]['n'] += 1
            by_year[yr]['ret'] += sim
            if sim > 0:
                by_year[yr]['win'] += 1

    if n == 0:
        return None
    res = {
        'n': n,
        'avg_sim': sum(sim_rets) / len(sim_rets) if sim_rets else 0,
        'win_rate': (sum(1 for x in sim_rets if x > 0) / len(sim_rets) * 100) if sim_rets else 0,
        'avg_t1_close': sum(t1_close_rets) / len(t1_close_rets) if t1_close_rets else 0,
        'rebound_rate': rebound_pos / len(t1_close_rets) * 100 if t1_close_rets else 0,
        'tp_hit': {lv: touched_tp[lv] / n * 100 for lv in TP_LEVELS},
        'avg_dd': sum(drawdowns) / len(drawdowns) if drawdowns else 0,
        'worst_dd': min(drawdowns) if drawdowns else 0,
        'by_year': dict(by_year),
    }
    return res


# ============================================================
# 主流程
# ============================================================

def build_configs():
    """构造 12+ 种配置对比"""
    cfgs = []
    # 基准维度: 板块 / 市值 / 选法 / 买点 / 暴跌阈值
    BOTH = {'GEM', 'STAR'}
    base_tp, base_sl = 5, -6
    # 1-3: 暴跌阈值对比 (BOTH, 小市值<50亿, abs选法, h1买)
    for th in (2.0, 3.0, 4.0):
        cfgs.append(dict(name=f"暴跌<-{th:.0f}% 小票<50亿 abs h1", crash_th=th,
                         boards=BOTH, mcap=(None, 50), sel='abs', buy='buy_h1',
                         tp=base_tp, sl=base_sl))
    # 4-5: 选法对比 (abs vs pct20, 暴跌-2%, 小票, h1)
    cfgs.append(dict(name="暴跌<-2% 小票<50亿 pct20 h1", crash_th=2.0,
                     boards=BOTH, mcap=(None, 50), sel='pct20', buy='buy_h1',
                     tp=base_tp, sl=base_sl))
    # 6-7: 买点对比 (h1 vs h2, 暴跌-2%, 小票, abs)
    cfgs.append(dict(name="暴跌<-2% 小票<50亿 abs h2(等回调)", crash_th=2.0,
                     boards=BOTH, mcap=(None, 50), sel='abs', buy='buy_h2',
                     tp=base_tp, sl=base_sl))
    # 8-9: 市值分层 (50-200亿, 全市值)
    cfgs.append(dict(name="暴跌<-2% 中票50-200亿 abs h1", crash_th=2.0,
                     boards=BOTH, mcap=(50, 200), sel='abs', buy='buy_h1',
                     tp=base_tp, sl=base_sl))
    cfgs.append(dict(name="暴跌<-2% 全市值 abs h1", crash_th=2.0,
                     boards=BOTH, mcap=(None, None), sel='abs', buy='buy_h1',
                     tp=base_tp, sl=base_sl))
    # 10-11: 板块分层 (只创业板 / 只科创板)
    cfgs.append(dict(name="暴跌<-2% 只创业板<50亿 abs h1", crash_th=2.0,
                     boards={'GEM'}, mcap=(None, 50), sel='abs', buy='buy_h1',
                     tp=base_tp, sl=base_sl))
    cfgs.append(dict(name="暴跌<-2% 只科创板<50亿 abs h1", crash_th=2.0,
                     boards={'STAR'}, mcap=(None, 50), sel='abs', buy='buy_h1',
                     tp=base_tp, sl=base_sl))
    # 12-14: 止盈档位对比 (tp=3/8/10, 暴跌-2%, 小票, abs, h1)
    for tp in (3, 8, 10):
        cfgs.append(dict(name=f"暴跌<-2% 小票<50亿 abs h1 TP={tp}%", crash_th=2.0,
                         boards=BOTH, mcap=(None, 50), sel='abs', buy='buy_h1',
                         tp=tp, sl=base_sl))
    # 15: pct20 + h2 组合
    cfgs.append(dict(name="暴跌<-2% 小票<50亿 pct20 h2", crash_th=2.0,
                     boards=BOTH, mcap=(None, 50), sel='pct20', buy='buy_h2',
                     tp=base_tp, sl=base_sl))
    return cfgs


def main():
    conn = sqlite3.connect(DB_PATH)
    all_days = get_all_trading_days(conn)

    year = None if ARG == 'all' else ARG

    print("=" * 70)
    print("大盘暴跌日超跌反弹策略 - Phase1 研究 (Task #76)")
    print(f"范围: {'2020-2026 全周期' if year is None else year}   明细打印: {'开' if PRINT_DETAIL else '关'}")
    print(f"持有窗口: T1..T1+{HOLD_DAYS-1} ({HOLD_DAYS}日)   数据库: {DB_PATH}")
    print("=" * 70)

    # ---------- 1) 暴跌日频率统计 ----------
    print("\n" + "=" * 30 + " 1) 暴跌日频率 " + "=" * 30)
    years = [str(y) for y in range(2020, 2027)]
    for th in CRASH_THRESHOLDS:
        days = get_crash_days(conn, th)
        by_yr = defaultdict(int)
        for dt, _ in days:
            by_yr[dt[:4]] += 1
        line = "  ".join(f"{y}:{by_yr.get(y,0)}" for y in years)
        print(f"  暴跌<=-{th:>4.1f}%  共{len(days):>3d}次 | {line}")

    # ---------- 2) 逐暴跌日筛选候选 (MASTER_TH 主筛选) ----------
    print("\n" + "=" * 30 + " 2) 逐暴跌日候选筛选 " + "=" * 25)
    crash_days = get_crash_days(conn, MASTER_TH, year)
    day_index = {d: i for i, d in enumerate(all_days)}
    all_cands = []
    for dt, iret in crash_days:
        if dt not in day_index:
            continue
        di = day_index[dt]
        cands = screen_crash_day(conn, all_days, di, dt, iret)
        all_cands.extend(cands)
        gem = sum(1 for c in cands if c['board'] == 'GEM')
        star = sum(1 for c in cands if c['board'] == 'STAR')
        deep = sum(1 for c in cands if c.get('is_deep20'))
        print(f"\n  {'='*8} 暴跌日 {dt} (上证 {iret:+.2f}%) {'='*8}")
        print(f"  超跌候选(下跌未跌停): {len(cands)} 只  [创板{gem} 科创{star} 深跌前20%={deep}]")

        if PRINT_DETAIL and cands:
            shown = sorted(cands, key=lambda x: x['drop'])[:DETAIL_PER_DAY]
            for r in shown:
                print_detail(conn, all_days, di, r)

    print(f"\n  >> 全周期超跌候选样本合计: {len(all_cands)} 只 (来自 {len(crash_days)} 个暴跌日 @-{MASTER_TH}%)")

    # ---------- 3) 多配置对比 ----------
    print("\n" + "=" * 30 + " 3) 多配置对比 " + "=" * 30)
    print(f"  模拟离场规则: 窗口内先触及SL则止损, 否则触及TP止盈, 否则末日close")
    cfgs = build_configs()
    results = []
    header = f"  {'#':>2} {'配置':<38} {'样本':>5} {'胜率':>6} {'均收益':>7} {'反弹率':>6} {'均回撤':>7}"
    print(header)
    print("  " + "-" * (len(header) - 2))
    for i, cfg in enumerate(cfgs, 1):
        res = eval_config(all_cands, cfg)
        if res is None:
            print(f"  {i:>2} {cfg['name']:<38} {'无样本':>5}")
            continue
        results.append((cfg, res))
        print(f"  {i:>2} {cfg['name']:<38} {res['n']:>5} {res['win_rate']:>5.1f}% "
              f"{res['avg_sim']:>+6.2f}% {res['rebound_rate']:>5.1f}% {res['avg_dd']:>+6.2f}%")

    # ---------- 4) 止盈触及率 & 回撤 (基准配置) ----------
    print("\n" + "=" * 30 + " 4) 止盈触及率/回撤明细 " + "=" * 23)
    for cfg, res in results:
        tp_s = "  ".join(f"+{lv}%:{res['tp_hit'][lv]:.0f}%" for lv in TP_LEVELS)
        print(f"\n  [{cfg['name']}] 样本{res['n']}")
        print(f"    止盈触及率(窗口内): {tp_s}")
        print(f"    T1当日close均收益: {res['avg_t1_close']:+.2f}%  次日反弹率: {res['rebound_rate']:.1f}%")
        print(f"    平均回撤: {res['avg_dd']:+.2f}%  最差回撤: {res['worst_dd']:+.2f}%")

    # ---------- 5) 逐年稳定性 (取综合最优配置) ----------
    print("\n" + "=" * 30 + " 5) 逐年稳定性 (最优配置) " + "=" * 21)
    if results:
        # 综合排序: 样本>=20 且 均收益优先, 胜率次之
        ranked = sorted([r for r in results if r[1]['n'] >= 10],
                        key=lambda x: (x[1]['avg_sim'], x[1]['win_rate']), reverse=True)
        if not ranked:
            ranked = sorted(results, key=lambda x: x[1]['avg_sim'], reverse=True)
        best_cfg, best_res = ranked[0]
        print(f"  最优配置: {best_cfg['name']}")
        print(f"  {'年份':>6} {'样本':>5} {'胜率':>7} {'均收益':>8}")
        for yr in years:
            y = best_res['by_year'].get(yr)
            if not y or y['n'] == 0:
                continue
            wr = y['win'] / y['n'] * 100
            ar = y['ret'] / y['n']
            print(f"  {yr:>6} {y['n']:>5} {wr:>6.1f}% {ar:>+7.2f}%")

        # ---------- 6) 结论 ----------
        print("\n" + "=" * 30 + " 6) 结论与达标判定 " + "=" * 28)
        print(f"  综合最优: {best_cfg['name']}")
        print(f"    样本={best_res['n']}  胜率={best_res['win_rate']:.1f}%  "
              f"单次均收益={best_res['avg_sim']:+.2f}%  次日反弹率={best_res['rebound_rate']:.1f}%")
        # rule2 目标: 月化10%+/胜率55%+ (信号稀疏, 折算单笔期望)
        pass_win = best_res['win_rate'] >= 55
        pass_ret = best_res['avg_sim'] >= 2.0  # 单笔期望>=2% 视为有正alpha
        verdict = "达标" if (pass_win and pass_ret) else ("部分达标" if (pass_win or pass_ret) else "不达标")
        print(f"    达标判定: 胜率{'✓' if pass_win else '✗'}(≥55%)  "
              f"单笔期望{'✓' if pass_ret else '✗'}(≥2%)  => {verdict}")
        print(f"    信号频率: 全周期候选{len(all_cands)}只, 信号稀疏(暴跌日/年约10-20次)")

    print("\n" + "=" * 70)
    print("研究完成。")
    conn.close()


if __name__ == '__main__':
    # 输出重定向到日志文件 (用 open/write, 不用 IDE 大文件写入)
    _out = open(LOG_PATH, 'w', encoding='utf-8')
    _orig = sys.stdout
    sys.stdout = _out
    try:
        main()
    finally:
        sys.stdout = _orig
        _out.close()
    print(f"完成, 日志写入: {LOG_PATH}")
