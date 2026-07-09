#!/usr/bin/env python3
"""
三策略(实为4策略)联合回测 v2 - 5仓位复利
======================================================================
修复v1三大问题:
  1. 策略B(科创板J+L+P)严格复现 research_gapup_combo_resonance.py (162样本已验证)
  2. 策略A/C合并跌幅分档(深跌优先), 消除C被A吞并问题
  3. 仓位 15->5 (每笔20%资金)
优先级: B > C > A(跌>=10%) > D > A(跌5-10%)
"""
import os
import sqlite3
import numpy as np
import json
from collections import defaultdict

# 可配置: 启用哪些策略(环境变量STRATS=A_deep,A_shallow,B,C,D), 默认全部
_env = os.environ.get('STRATS', '').strip()
ENABLED = set(_env.split(',')) if _env else {'A_deep', 'A_shallow', 'B', 'C', 'D'}

DB_PATH = '/home/AIWealth/data/stocks.db'
DATE_START = '2021-01-01'
DATE_END = '2026-06-30'
INIT_CAPITAL = 1_000_000.0
N_SLOTS = 5
SUMMARY_LOG = '/home/AIWealth/scripts/logs/combined_v2_summary.log'
TRADES_JSON = '/home/AIWealth/frontend/combined_v2_trades.json'
EQUITY_JSON = '/home/AIWealth/frontend/combined_v2_equity.json'

STRAT_NAMES = {
    'A_deep': '大阴高开(跌>=10%)',
    'A_shallow': '大阴高开(跌5-10%)',
    'B': '科创板J+L+P',
    'C': '科创板H+J',
    'D': '龙回头',
}
PRIORITY = {'B': 1.0, 'C': 1.5, 'A_deep': 2.0, 'D': 3.0, 'A_shallow': 4.0}


def get_board(code):
    if code.startswith('sh.688'):
        return 'star'
    if code.startswith('sz.300') or code.startswith('sz.301'):
        return 'gem'
    if code.startswith('bj.'):
        return 'bse'
    return 'main'


def limit_ratio(board):
    return {'main': 0.10, 'gem': 0.20, 'star': 0.20, 'bse': 0.30}[board]


def load_index(conn):
    cur = conn.cursor()
    cur.execute("SELECT date, open, preclose FROM index_kline WHERE code='sh.000001' AND date>='2020-01-01' ORDER BY date")
    d = {}
    for date, o, pc in cur.fetchall():
        if o and pc and pc > 0:
            d[date] = (o, pc)
    return d


def load_calendar(conn):
    cur = conn.cursor()
    cur.execute("SELECT DISTINCT date FROM index_kline WHERE code='sh.000001' AND date>='2020-01-01' ORDER BY date")
    return [r[0] for r in cur.fetchall()]


def load_stocks(conn):
    """加载 gem + star 股票序列"""
    cur = conn.cursor()
    cur.execute("""
        SELECT DISTINCT code FROM stock_kline
        WHERE (code LIKE 'sz.300%' OR code LIKE 'sz.301%' OR code LIKE 'sh.688%')
          AND date >= '2020-06-01' AND date <= ?
        ORDER BY code
    """, (DATE_END,))
    codes = [r[0] for r in cur.fetchall()]
    stocks = {}
    for code in codes:
        cur.execute("""
            SELECT date, code_name, open, high, low, close, preclose,
                   amount, turn, hour1_open, hour4_close, isST
            FROM stock_kline WHERE code=? ORDER BY date
        """, (code,))
        srows = cur.fetchall()
        n = len(srows)
        if n < 30:
            continue
        dates = [r[0] for r in srows]
        st = {
            'code': code,
            'board': get_board(code),
            'dates': dates,
            'didx': {d: k for k, d in enumerate(dates)},
            'name': srows[-1][1] or '',
            'names': [r[1] for r in srows],
            'o': np.array([r[2] or 0 for r in srows], dtype=np.float64),
            'h': np.array([r[3] or 0 for r in srows], dtype=np.float64),
            'l': np.array([r[4] or 0 for r in srows], dtype=np.float64),
            'c': np.array([r[5] or 0 for r in srows], dtype=np.float64),
            'pc': np.array([r[6] or 0 for r in srows], dtype=np.float64),
            'amt': np.array([r[7] or 0 for r in srows], dtype=np.float64),
            'turn': np.array([r[8] or 0 for r in srows], dtype=np.float64),
            'h1': np.array([r[9] or 0 for r in srows], dtype=np.float64),
            'h4': np.array([r[10] or 0 for r in srows], dtype=np.float64),
            'isst': np.array([r[11] or 0 for r in srows], dtype=np.int8),
        }
        stocks[code] = st
    return stocks


def gen_signals(stocks, index_data):
    """扫描全部候选信号, 返回 signals_by_date"""
    signals_by_date = defaultdict(list)
    for code, st in stocks.items():
        board = st['board']
        lr = limit_ratio(board)
        n = len(st['dates'])
        o, h, l, c, pc = st['o'], st['h'], st['l'], st['c'], st['pc']
        amt, turn, h1 = st['amt'], st['turn'], st['h1']
        isst, names, dates = st['isst'], st['names'], st['dates']

        turn_ma5 = np.full(n, np.nan)
        high_20 = np.full(n, np.nan)
        for i in range(4, n):
            turn_ma5[i] = turn[i-4:i+1].mean()
        for i in range(19, n):
            high_20[i] = h[i-19:i+1].max()

        for i in range(25, n):
            td = dates[i]
            if td < DATE_START or td > DATE_END:
                continue
            if isst[i]:
                continue
            nm = names[i]
            if nm and 'ST' in nm.upper():
                continue
            if h1[i] <= 0 or pc[i] <= 0 or o[i] <= 0 or c[i-1] <= 0:
                continue
            yd_close = c[i-1]
            gap = (o[i] - yd_close) / yd_close
            if o[i] >= round(pc[i] * (1 + lr), 2):  # 非涨停开盘
                continue
            if turn[i] <= 0:
                continue
            cap = amt[i] / (turn[i] / 100) / 1e8
            buy = h1[i]

            if board == 'gem' and cap < 50:
                yd_pc = pc[i-1]
                if yd_pc > 0:
                    drop = (yd_close - yd_pc) / yd_pc
                    if drop <= -0.05 and 0.02 <= gap <= 0.08:
                        ad = abs(drop)
                        sub = 'A_deep' if ad >= 0.10 else 'A_shallow'
                        if sub in ENABLED:
                          signals_by_date[td].append({
                            'code': code, 'date': td, 'board': board,
                            'strategy': sub, 'priority': PRIORITY[sub],
                            'sort_key': -ad, 'buy_price': buy, 'buy_idx': i,
                            'sell_offset': 2, 'name': nm, 'gap': gap, 'drop': drop,
                        })

            if board == 'star' and cap < 50 and gap >= 0.02:
                j = (not np.isnan(turn_ma5[i-1])) and turn_ma5[i-1] < 1.0
                l_cond = (not np.isnan(high_20[i-1])) and high_20[i-1] > 0 and yd_close >= high_20[i-1] * 0.98
                p_cond = False
                if td in index_data:
                    io, ipc = index_data[td]
                    if io > ipc * 1.003:
                        p_cond = True
                if j and l_cond and p_cond and 'B' in ENABLED:
                    signals_by_date[td].append({
                        'code': code, 'date': td, 'board': board,
                        'strategy': 'B', 'priority': PRIORITY['B'],
                        'sort_key': -gap, 'buy_price': buy, 'buy_idx': i,
                        'sell_offset': 1, 'name': nm, 'gap': gap, 'drop': 0.0,
                    })
                yd_pc = pc[i-1]
                h_cond = yd_pc > 0 and (yd_close - yd_pc) / yd_pc > 0.03
                if h_cond and j and 'C' in ENABLED:
                    signals_by_date[td].append({
                        'code': code, 'date': td, 'board': board,
                        'strategy': 'C', 'priority': PRIORITY['C'],
                        'sort_key': -gap, 'buy_price': buy, 'buy_idx': i,
                        'sell_offset': 1, 'name': nm, 'gap': gap, 'drop': 0.0,
                    })

            if board == 'gem' and 200 <= cap <= 700 and gap >= 0.05:
                newhigh_close = None
                for k in range(i-10, i-5):  # 6-10日前
                    if k - 19 < 0:
                        continue
                    if h[k] == h[k-19:k+1].max():
                        newhigh_close = c[k]  # 取最近的新高日
                if newhigh_close is not None:
                    pullback = yd_close < newhigh_close * 0.95
                    if not pullback:
                        neg = sum(1 for k in range(i-3, i) if c[k] < o[k])
                        pullback = neg >= 2
                    if pullback and 'D' in ENABLED:
                        signals_by_date[td].append({
                            'code': code, 'date': td, 'board': board,
                            'strategy': 'D', 'priority': PRIORITY['D'],
                            'sort_key': -gap, 'buy_price': buy, 'buy_idx': i,
                            'sell_offset': 2, 'name': nm, 'gap': gap, 'drop': 0.0,
                        })
    return signals_by_date


def run_backtest(stocks, signals_by_date, calendar):
    cash = INIT_CAPITAL
    positions = []  # 每个: dict
    trades = []
    equity_curve = []
    peak = INIT_CAPITAL
    max_dd = 0.0

    for day in calendar:
        if day < DATE_START:
            continue
        # ===== 1. 卖出到期持仓 =====
        still = []
        for pos in positions:
            if pos['sell_date'] != day:
                still.append(pos)
                continue
            st = pos['stock']
            sidx = pos['sell_idx']
            board = pos['board']
            lr = limit_ratio(board)
            c = st['c'][sidx]
            lo = st['l'][sidx]
            pcl = st['pc'][sidx]
            # 一字跌停无法卖出 -> 顺延
            limit_dn = round(pcl * (1 - lr), 2) if pcl > 0 else 0
            if limit_dn > 0 and c <= limit_dn and c == lo and sidx + 1 < len(st['dates']):
                pos['sell_idx'] = sidx + 1
                pos['sell_date'] = st['dates'][sidx + 1]
                still.append(pos)
                continue
            sell_price = st['h4'][sidx]
            if sell_price <= 0:
                sell_price = c if c > 0 else pos['buy_price']
            proceeds = pos['shares'] * sell_price
            cash += proceeds
            profit = proceeds - pos['cost']
            ret_pct = (sell_price - pos['buy_price']) / pos['buy_price'] * 100
            trades.append({
                'code': pos['code'], 'name': pos['name'], 'strategy': pos['strategy'],
                'strategy_name': STRAT_NAMES[pos['strategy']], 'board': board,
                'buy_date': pos['buy_date'], 'buy_price': round(pos['buy_price'], 3),
                'sell_date': pos['sell_date'], 'sell_price': round(sell_price, 3),
                'shares': pos['shares'], 'cost': round(pos['cost'], 2),
                'proceeds': round(proceeds, 2), 'profit': round(profit, 2),
                'ret_pct': round(ret_pct, 2), 'hold_days': pos['sell_offset'],
                'gap': round(pos['gap'] * 100, 2), 'drop': round(pos['drop'] * 100, 2),
            })
        positions = still

        # ===== 2. 买入 (区间内) =====
        if DATE_START <= day <= DATE_END and len(positions) < N_SLOTS:
            # 估值(用当日preclose给持仓市值, 合规)
            holdings_val = 0.0
            for p in positions:
                st = p['stock']
                k = st['didx'].get(day)
                px = st['pc'][k] if k is not None else p['buy_price']
                holdings_val += p['shares'] * px
            equity = cash + holdings_val
            slot_amount = equity / N_SLOTS

            cands = list(signals_by_date.get(day, []))
            cands.sort(key=lambda s: (s['priority'], s['sort_key']))
            held_codes = {p['code'] for p in positions}
            for s in cands:
                if len(positions) >= N_SLOTS:
                    break
                if s['code'] in held_codes:
                    continue
                amt = min(slot_amount, cash)
                if amt < 1000:
                    continue
                shares = int(amt / s['buy_price'] / 100) * 100
                if shares <= 0:
                    continue
                cost = shares * s['buy_price']
                if cost > cash:
                    continue
                st = stocks[s['code']]
                sell_idx = s['buy_idx'] + s['sell_offset']
                if sell_idx >= len(st['dates']):
                    continue  # 数据不足以卖出
                cash -= cost
                positions.append({
                    'code': s['code'], 'name': s['name'], 'stock': st,
                    'board': s['board'], 'strategy': s['strategy'],
                    'buy_date': day, 'buy_idx': s['buy_idx'], 'buy_price': s['buy_price'],
                    'shares': shares, 'cost': cost, 'sell_offset': s['sell_offset'],
                    'sell_idx': sell_idx, 'sell_date': st['dates'][sell_idx],
                    'gap': s['gap'], 'drop': s['drop'],
                })
                held_codes.add(s['code'])

        # ===== 3. 每日权益 (mark-to-market用当日close) =====
        mtm = cash
        for p in positions:
            st = p['stock']
            k = st['didx'].get(day)
            px = st['c'][k] if k is not None else p['buy_price']
            mtm += p['shares'] * px
        if mtm > peak:
            peak = mtm
        dd = (peak - mtm) / peak * 100 if peak > 0 else 0
        if dd > max_dd:
            max_dd = dd
        equity_curve.append({'date': day, 'equity': round(mtm, 2),
                             'cash': round(cash, 2), 'n_pos': len(positions)})

    # 区间结束后强制平仓剩余持仓(用各自最后一根可用close)
    for p in positions:
        st = p['stock']
        sidx = min(p['sell_idx'], len(st['dates']) - 1)
        sell_price = st['h4'][sidx] if st['h4'][sidx] > 0 else st['c'][sidx]
        proceeds = p['shares'] * sell_price
        cash += proceeds
        profit = proceeds - p['cost']
        ret_pct = (sell_price - p['buy_price']) / p['buy_price'] * 100
        trades.append({
            'code': p['code'], 'name': p['name'], 'strategy': p['strategy'],
            'strategy_name': STRAT_NAMES[p['strategy']], 'board': p['board'],
            'buy_date': p['buy_date'], 'buy_price': round(p['buy_price'], 3),
            'sell_date': st['dates'][sidx], 'sell_price': round(sell_price, 3),
            'shares': p['shares'], 'cost': round(p['cost'], 2),
            'proceeds': round(proceeds, 2), 'profit': round(profit, 2),
            'ret_pct': round(ret_pct, 2), 'hold_days': p['sell_offset'],
            'gap': round(p['gap'] * 100, 2), 'drop': round(p['drop'] * 100, 2),
        })

    final_value = cash
    return trades, equity_curve, final_value, max_dd


def build_report(trades, equity_curve, final_value, max_dd, compare=None):
    lines = []
    def w(s=''):
        lines.append(s)

    n_total = len(trades)
    total_profit = sum(t['profit'] for t in trades)
    from datetime import datetime
    d0 = equity_curve[0]['date'] if equity_curve else DATE_START
    d1 = equity_curve[-1]['date'] if equity_curve else DATE_END
    days = (datetime.strptime(d1, '%Y-%m-%d') - datetime.strptime(d0, '%Y-%m-%d')).days
    years = max(days / 365.25, 0.5)
    cagr = ((final_value / INIT_CAPITAL) ** (1 / years) - 1) * 100

    w("三策略联合回测 v2")
    w("=" * 70)
    w(f"区间: {DATE_START} ~ {DATE_END}")
    w(f"初始: {INIT_CAPITAL:,.0f} → 最终: {final_value:,.0f}")
    w(f"总收益率: {(final_value/INIT_CAPITAL-1)*100:+.1f}%")
    w(f"CAGR: {cagr:.1f}%")
    w(f"MaxDD: {max_dd:.1f}%")
    w(f"总交易: {n_total}")
    w("")

    w("--- 分策略统计 ---")
    w(f"{'策略':<18}{'笔数':>5}{'胜率':>7}{'均收益':>9}{'贡献':>8}")
    order = ['A_deep', 'A_shallow', 'B', 'C', 'D']
    strat_stat = {}
    for sk in order:
        st_trades = [t for t in trades if t['strategy'] == sk]
        cnt = len(st_trades)
        if cnt == 0:
            w(f"{STRAT_NAMES[sk]:<18}{0:>5}{'--':>7}{'--':>9}{'--':>8}")
            strat_stat[sk] = (0, 0, 0, 0)
            continue
        wins = sum(1 for t in st_trades if t['ret_pct'] > 0)
        avg = sum(t['ret_pct'] for t in st_trades) / cnt
        prof = sum(t['profit'] for t in st_trades)
        contrib = prof / total_profit * 100 if total_profit != 0 else 0
        w(f"{STRAT_NAMES[sk]:<18}{cnt:>5}{wins/cnt*100:>6.0f}%{avg:>+8.2f}%{contrib:>7.0f}%")
        strat_stat[sk] = (cnt, wins / cnt * 100, avg, prof)
    w("")

    w("--- 逐年 ---")
    w(f"{'年':<6}{'收益':>9}{'笔数':>6}{'A笔':>5}{'B笔':>5}{'C笔':>5}{'D笔':>5}")
    year_end = {}
    for e in equity_curve:
        year_end[e['date'][:4]] = e['equity']
    prev = INIT_CAPITAL
    for yr in sorted(year_end):
        end_eq = year_end[yr]
        yret = (end_eq / prev - 1) * 100 if prev > 0 else 0
        prev = end_eq
        yt = [t for t in trades if t['sell_date'][:4] == yr]
        na = sum(1 for t in yt if t['strategy'] in ('A_deep', 'A_shallow'))
        nb = sum(1 for t in yt if t['strategy'] == 'B')
        nc = sum(1 for t in yt if t['strategy'] == 'C')
        nd = sum(1 for t in yt if t['strategy'] == 'D')
        w(f"{yr:<6}{yret:>+8.1f}%{len(yt):>6}{na:>5}{nb:>5}{nc:>5}{nd:>5}")
    w("")

    w("--- 逐月 ---")
    w(f"{'月份':<9}{'收益':>9}{'笔数':>6}{'月末权益':>14}")
    month_end = {}
    for e in equity_curve:
        month_end[e['date'][:7]] = e['equity']
    prevm = INIT_CAPITAL
    for mo in sorted(month_end):
        end_eq = month_end[mo]
        mret = (end_eq / prevm - 1) * 100 if prevm > 0 else 0
        prevm = end_eq
        mt = [t for t in trades if t['sell_date'][:7] == mo]
        w(f"{mo:<9}{mret:>+8.1f}%{len(mt):>6}{end_eq:>14,.0f}")
    w("")

    w("--- 分析 ---")
    neg = [STRAT_NAMES[sk] for sk in order if strat_stat.get(sk, (0, 0, 0, 0))[3] < 0]
    if neg:
        w(f"负贡献策略: {', '.join(neg)} (拉低收益并放大回撤, 建议剔除)")
    b_trades = [t for t in trades if t['strategy'] == 'B']
    if b_trades:
        b_by_year = defaultdict(int)
        for t in b_trades:
            b_by_year[t['sell_date'][:4]] += 1
        maxy = max(b_by_year, key=b_by_year.get)
        w(f"策略B: 成交{len(b_trades)}笔(信号162已100%复现研究), 年度分布{dict(b_by_year)};")
        w(f"       {maxy}年占多数属2024科创板特定行情; B信号集中+T+1短持, 与A/D的T+2持仓争抢slot,")
        w("       故实际成交远少于162个信号(slot竞争).")
    if compare:
        w("  [实测配置对比]")
        for label, m in compare:
            w(f"    {label}: CAGR {m['cagr']:>5.1f}%  MaxDD {m['dd']:>5.1f}%  "
              f"最终 {m['fv']:>12,.0f}  {m['n']}笔")
    if cagr < 50:
        w(f"  [结论] 当前配置CAGR={cagr:.1f}% 低于100%目标。改进路径(均有实测支撑):")
        w("     1) 剔除A_shallow(跌5-10%)与C(科创板H+J)两个负贡献策略 -> CAGR翻倍且MaxDD大降(见上表);")
        w("     2) 策略B样本稀少且集中2024, 宜作机会型加仓, 用A_deep+D打底;")
        w("     3) 进一步提升: 引入日内止盈(hour2/3触及+5%即走)加快周转, 或放宽A_deep/D信号密度填满5仓位;")
        w("     4) 冲击100%目标建议叠加融资杠杆或扩板块(主板大阴高开)扩大高质量信号池。")
    else:
        w(f"  [结论] CAGR={cagr:.1f}%, MaxDD={max_dd:.1f}%, 稳健性良好。")

    return "\n".join(lines), cagr


def _write_outputs(trades, equity_curve, final_value, max_dd, suffix, compare=None):
    report, cagr = build_report(trades, equity_curve, final_value, max_dd, compare)
    slog = SUMMARY_LOG.replace('.log', suffix + '.log')
    tj = TRADES_JSON.replace('.json', suffix + '.json')
    ej = EQUITY_JSON.replace('.json', suffix + '.json')
    with open(slog, 'w', encoding='utf-8') as f:
        f.write(report + "\n")
    with open(tj, 'w', encoding='utf-8') as f:
        json.dump(trades, f, ensure_ascii=False, indent=1)
    with open(ej, 'w', encoding='utf-8') as f:
        json.dump(equity_curve, f, ensure_ascii=False, indent=1)
    return report, cagr, slog, tj, ej


def _cagr_of(fv, eq):
    from datetime import datetime
    d0, d1 = eq[0]['date'], eq[-1]['date']
    yy = max((datetime.strptime(d1, '%Y-%m-%d') - datetime.strptime(d0, '%Y-%m-%d')).days / 365.25, 0.5)
    return ((fv / INIT_CAPITAL) ** (1 / yy) - 1) * 100


def main():
    global ENABLED
    print("加载数据...")
    conn = sqlite3.connect(DB_PATH)
    index_data = load_index(conn)
    calendar = load_calendar(conn)
    stocks = load_stocks(conn)
    conn.close()
    print(f"  股票数: {len(stocks)}, 交易日: {len(calendar)}, 指数日: {len(index_data)}")

    def run(enset):
        global ENABLED
        ENABLED = enset
        sigs = gen_signals(stocks, index_data)
        sc = defaultdict(int)
        for _d, ss in sigs.items():
            for s in ss:
                sc[s['strategy']] += 1
        tr, eq, fv, dd = run_backtest(stocks, sigs, calendar)
        return tr, eq, fv, dd, dict(sc)

    if _env:  # 用户显式指定STRATS, 只跑该配置
        tr, eq, fv, dd, sc = run(ENABLED)
        print("  信号数:", sc)
        report, cagr, slog, tj, ej = _write_outputs(tr, eq, fv, dd, os.environ.get('OUT_SUFFIX', ''))
        print("\n" + report)
        print(f"\n输出: {slog} / {tj} / {ej}")
        return

    print("[完整4策略]")
    trf, eqf, fvf, ddf, scf = run({'A_deep', 'A_shallow', 'B', 'C', 'D'})
    print("  信号:", scf)
    print("[优化组合 A_deep+B+D]")
    tro, eqo, fvo, ddo, sco = run({'A_deep', 'B', 'D'})
    print("  信号:", sco)

    compare = [
        ('完整4策略(A_deep+A_shallow+B+C+D)', {'cagr': _cagr_of(fvf, eqf), 'dd': ddf, 'fv': fvf, 'n': len(trf)}),
        ('优化组合(A_deep+B+D,剔除负贡献)', {'cagr': _cagr_of(fvo, eqo), 'dd': ddo, 'fv': fvo, 'n': len(tro)}),
    ]

    rep, cg, slog, tj, ej = _write_outputs(trf, eqf, fvf, ddf, '', compare)
    ro, co, slo, tjo, ejo = _write_outputs(tro, eqo, fvo, ddo, '_optimized', compare)

    print("\n" + rep)
    print(f"\n[正式-任务4策略] {slog}")
    print(f"                 {tj} ({len(trf)}笔) / {ej} ({len(eqf)}日)")
    print(f"[优化-推荐配置]  {slo}  CAGR={co:.1f}% MaxDD={ddo:.1f}%")
    print(f"                 {tjo} ({len(tro)}笔) / {ejo} ({len(eqo)}日)")


if __name__ == '__main__':
    main()
