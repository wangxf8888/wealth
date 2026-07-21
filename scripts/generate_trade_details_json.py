#!/usr/bin/env python3
"""
生成2策略联合 + limitup_early_seal独立 的完整交易明细JSON
===========================================================
基于 verify_2strategy_combined.py 的已验证逻辑

输出:
1. /home/AIWealth/logs/backtest/combined_2strategy_trades.json
2. /home/AIWealth/logs/backtest/limitup_early_seal_trades.json
"""

import sqlite3
import json
import numpy as np
import time
import re
from collections import defaultdict
from datetime import datetime

# ============================================================
# 配置
# ============================================================
DB_PATH = '/home/AIWealth/data/stocks.db'
START_DATE = '2021-01-01'
END_DATE = '2026-06-30'
FEE_RATE = 0.001  # 单边0.1%
INIT_CAPITAL = 1_000_000.0

# 策略参数
S1_TP = 0.08       # limitup_early_seal 止盈8%
S1_SL = -0.15      # 止损15%
S1_MAX_HOLD = 4    # 最多持4小时

S3_TP = 0.105      # friday_limitup 止盈10.5%
S3_SL = -0.50      # 止损50%(相当于无止损)
S3_MAX_HOLD = 4    # 最多持4小时

OUTPUT_COMBINED = '/home/AIWealth/logs/backtest/combined_2strategy_trades.json'
OUTPUT_LIMITUP = '/home/AIWealth/logs/backtest/limitup_early_seal_trades.json'

# ============================================================
# 工具函数
# ============================================================
_CYB_RE = re.compile(r'^sz\.30')
_STAR_RE = re.compile(r'^sh\.688')


def limit_ratio(code):
    if _CYB_RE.match(code) or _STAR_RE.match(code):
        return 1.20
    return 1.10


def calc_limit_up(code, preclose):
    return round(preclose * limit_ratio(code), 2)


def calc_limit_down(code, preclose):
    r = limit_ratio(code)
    return round(preclose * (2 - r), 2)


def at_limit_up(code, price, preclose):
    if preclose <= 0 or price <= 0:
        return False
    return price >= calc_limit_up(code, preclose)


def at_limit_down(code, price, preclose):
    if preclose <= 0 or price <= 0:
        return False
    return price <= calc_limit_down(code, preclose)


# ============================================================
# 策略1: limitup_early_seal 信号生成
# ============================================================
def gen_early_seal_signals(conn, trade_dates):
    """昨日早封涨停+今日低开>3% → 买入hour1_open"""
    print("  生成 limitup_early_seal 信号...")
    t0 = time.time()

    date_set = set(trade_dates)
    date_idx = {d: i for i, d in enumerate(trade_dates)}
    signals = []

    cur = conn.execute("""
        SELECT code, date, open, close, preclose, open_rate, turn,
               hour1_open, hour1_close, code_name
        FROM stock_kline
        WHERE date >= ? AND date <= ? AND isST = 0
        ORDER BY code, date
    """, (START_DATE, END_DATE))

    by_code = defaultdict(list)
    for row in cur.fetchall():
        by_code[row[0]].append(row)

    for code, rows in by_code.items():
        ratio = limit_ratio(code)
        for i in range(1, len(rows)):
            prev = rows[i - 1]
            cur_day = rows[i]

            p_open = prev[2] or 0
            p_close = prev[3] or 0
            p_preclose = prev[4] or 0
            p_turn = prev[6] or 0
            p_h1_close = prev[8] or 0
            p_date = prev[1]

            t_date = cur_day[1]
            t_open_rate = cur_day[5] or 0
            t_preclose = cur_day[4] or 0
            t_h1_open = cur_day[7] or 0
            t_name = cur_day[9] or ''

            if t_date not in date_set:
                continue
            if p_preclose <= 0 or p_close <= 0:
                continue

            # 条件1: 昨日涨停
            lu_price = round(p_preclose * ratio, 2)
            if p_close < lu_price:
                continue
            # 条件2: 非一字板
            if p_open >= lu_price:
                continue
            # 条件3: 早封
            if p_h1_close <= 0 or round(p_h1_close, 2) != lu_price:
                continue
            # 条件4: 换手率<8%
            if p_turn <= 0 or p_turn >= 8.0:
                continue
            # 条件5: 今日低开>3%
            if t_open_rate >= -3.0:
                continue
            # 条件6: h1_open不涨停
            if t_preclose <= 0 or t_h1_open <= 0:
                continue
            today_lu = round(t_preclose * ratio, 2)
            if t_h1_open >= today_lu:
                continue

            signals.append({
                'strategy': 'limitup_early_seal',
                'priority': 1,
                'buy_date': t_date,
                'signal_date': p_date,
                'code': code,
                'name': t_name,
                'buy_price': t_h1_open,
                'tp_pct': S1_TP,
                'sl_pct': S1_SL,
                'max_hold_hours': S1_MAX_HOLD,
                'score': t_open_rate,
                'preclose': t_preclose,
            })

    print(f"    → {len(signals)} 个信号 ({time.time()-t0:.1f}s)")
    return signals


# ============================================================
# 策略3: friday_limitup 信号生成
# ============================================================
def gen_friday_signals(conn, trade_dates):
    """周五涨停+市值50~300亿 → 周一高开>=6%时买入"""
    print("  生成 friday_limitup 信号...")
    t0 = time.time()

    signals = []
    date_idx = {d: i for i, d in enumerate(trade_dates)}

    fridays = []
    for d in trade_dates:
        if d < START_DATE or d > END_DATE:
            continue
        if datetime.strptime(d, '%Y-%m-%d').weekday() == 4:
            fridays.append(d)

    if not fridays:
        return signals

    fri_str = "','".join(fridays)
    cur = conn.execute(f"""
        SELECT date, code, preclose, close, high, low, open,
               close_rate, turn, amount, code_name
        FROM stock_kline
        WHERE date IN ('{fri_str}') AND isST = 0
          AND close_rate >= 9.5
    """)

    fri_candidates = []
    for row in cur.fetchall():
        date, code, preclose, close, high, low, open_p, close_rate, turn, amount, name = row
        if not preclose or preclose <= 0 or not close or close <= 0:
            continue

        if _CYB_RE.match(code) or _STAR_RE.match(code):
            threshold = 19.5
        else:
            threshold = 9.5

        if (close_rate or 0) < threshold:
            continue
        if high == low:
            continue
        if not turn or turn <= 0:
            continue
        mcap = (amount or 0) / (turn / 100) / 1e8
        if mcap < 50 or mcap > 300:
            continue

        fri_candidates.append({'date': date, 'code': code, 'mcap': mcap, 'name': name or ''})

    for cand in fri_candidates:
        idx = date_idx.get(cand['date'])
        if idx is None or idx + 1 >= len(trade_dates):
            continue
        next_td = trade_dates[idx + 1]

        cur2 = conn.execute("""
            SELECT hour1_open, preclose, open_rate
            FROM stock_kline WHERE code = ? AND date = ?
        """, (cand['code'], next_td))
        row = cur2.fetchone()
        if not row:
            continue

        h1_open = row[0] or 0
        preclose = row[1] or 0
        open_rate = row[2] or 0

        if h1_open <= 0 or preclose <= 0:
            continue
        if open_rate < 6.0:
            continue
        if at_limit_up(cand['code'], h1_open, preclose):
            continue

        signals.append({
            'strategy': 'friday_limitup',
            'priority': 3,
            'buy_date': next_td,
            'signal_date': cand['date'],
            'code': cand['code'],
            'name': cand['name'],
            'buy_price': h1_open,
            'tp_pct': S3_TP,
            'sl_pct': S3_SL,
            'max_hold_hours': S3_MAX_HOLD,
            'score': cand['mcap'],
            'preclose': preclose,
        })

    print(f"    → {len(signals)} 个信号 ({time.time()-t0:.1f}s)")
    return signals


# ============================================================
# 加载小时级数据
# ============================================================
def load_hourly(conn, signals, trade_dates):
    print("  加载小时级行情数据...")
    t0 = time.time()

    date_idx = {d: i for i, d in enumerate(trade_dates)}
    needed = set()

    for sig in signals:
        buy_idx = date_idx.get(sig['buy_date'])
        if buy_idx is not None:
            for j in range(buy_idx, min(buy_idx + 5, len(trade_dates))):
                needed.add((sig['code'], trade_dates[j]))

    hourly = {}
    pairs_by_date = defaultdict(set)
    for code, date in needed:
        pairs_by_date[date].add(code)

    all_codes = sorted(set(c for cs in pairs_by_date.values() for c in cs))
    dates_sorted = sorted(pairs_by_date.keys())
    dates_str = "','".join(dates_sorted)

    batch_sz = 400
    for i in range(0, len(all_codes), batch_sz):
        batch = all_codes[i:i+batch_sz]
        codes_str = "','".join(batch)
        cur = conn.execute(f"""
            SELECT code, date,
                   hour1_open, hour1_high, hour1_low, hour1_close,
                   hour2_open, hour2_high, hour2_low, hour2_close,
                   hour3_open, hour3_high, hour3_low, hour3_close,
                   hour4_open, hour4_high, hour4_low, hour4_close,
                   preclose
            FROM stock_kline
            WHERE code IN ('{codes_str}') AND date IN ('{dates_str}')
        """)
        for row in cur.fetchall():
            code, date = row[0], row[1]
            pc = row[18] or 0
            for h in range(4):
                base = 2 + h * 4
                o = row[base] or 0
                hi = row[base + 1] or 0
                lo = row[base + 2] or 0
                c = row[base + 3] or 0
                hourly[(code, date, h + 1)] = (o, hi, lo, c, pc)

    print(f"    → {len(hourly)} 条bar ({time.time()-t0:.1f}s)")
    return hourly


# ============================================================
# 卖出逻辑
# ============================================================
def try_sell(holding, date, hour, hourly):
    if holding['buy_date'] == date:
        return None

    bar = hourly.get((holding['code'], date, hour))
    if not bar:
        return None

    o, hi, lo, c, pc = bar
    if o <= 0:
        return None

    bp = holding['buy_price']
    tp_price = bp * (1 + holding['tp_pct'])
    sl_price = bp * (1 + holding['sl_pct'])

    # 跌停不卖
    if pc > 0 and at_limit_down(holding['code'], o, pc):
        if lo > 0 and lo >= o:
            return None

    # gap-aware
    open_ret = (o - bp) / bp
    if open_ret >= holding['tp_pct']:
        return (o, 'gap_tp')
    if open_ret <= holding['sl_pct']:
        if pc > 0 and at_limit_down(holding['code'], o, pc):
            return None
        return (o, 'gap_sl')

    # 盘中止损
    if lo > 0 and lo <= sl_price:
        if pc > 0:
            ld = calc_limit_down(holding['code'], pc)
            actual_sl = max(sl_price, ld)
        else:
            actual_sl = sl_price
        return (actual_sl, 'stop_loss')

    # 盘中止盈
    if hi > 0 and hi >= tp_price:
        return (tp_price, 'take_profit')

    # 到期
    if holding['hours_held'] >= holding['max_hold_hours'] and hour == 4:
        sp = c if c > 0 else o
        return (sp, 'expire')

    return None


# ============================================================
# 模拟器: slot=1 (记录完整交易明细)
# ============================================================
def simulate(signals, trade_dates, hourly):
    print("  执行模拟回测...")
    t0 = time.time()

    sig_sorted = sorted(signals, key=lambda x: (x['buy_date'], x['priority'], x['score']))
    sig_by_date = defaultdict(list)
    for s in sig_sorted:
        sig_by_date[s['buy_date']].append(s)

    capital = INIT_CAPITAL
    peak = capital
    max_dd = 0.0
    trades = []
    equity_curve = [{'date': START_DATE, 'capital': INIT_CAPITAL}]
    holding = None
    trade_id = 0

    last_equity_date = None

    for date in trade_dates:
        if date < START_DATE or date > END_DATE:
            continue

        for hour in (1, 2, 3, 4):
            # 1) 检查卖出
            if holding is not None:
                result = try_sell(holding, date, hour, hourly)
                if result:
                    sell_price, reason = result
                    cost = holding['buy_price'] * (1 + FEE_RATE)
                    net = sell_price * (1 - FEE_RATE)
                    ret = (net - cost) / cost
                    capital_before = capital
                    capital *= (1 + ret)

                    trade_id += 1
                    trades.append({
                        'id': trade_id,
                        'code': holding['code'],
                        'name': holding['name'],
                        'strategy': holding['strategy'],
                        'signal_date': holding['signal_date'],
                        'buy_date': holding['buy_date'],
                        'buy_hour': 1,
                        'buy_price': round(holding['buy_price'], 4),
                        'sell_date': date,
                        'sell_hour': hour,
                        'sell_price': round(sell_price, 4),
                        'sell_reason': reason,
                        'return_pct': round(ret * 100, 2),
                        'hold_hours': holding['hours_held'],
                        'capital_before': round(capital_before, 2),
                        'capital_after': round(capital, 2),
                    })
                    holding = None

                    if capital > peak:
                        peak = capital
                    dd = (peak - capital) / peak
                    if dd > max_dd:
                        max_dd = dd

            # 2) 买入
            if holding is None and hour == 1 and date in sig_by_date:
                for sig in sig_by_date[date]:
                    if at_limit_up(sig['code'], sig['buy_price'], sig['preclose']):
                        continue
                    holding = {
                        'code': sig['code'],
                        'name': sig.get('name', ''),
                        'buy_price': sig['buy_price'],
                        'buy_date': date,
                        'signal_date': sig.get('signal_date', ''),
                        'strategy': sig['strategy'],
                        'tp_pct': sig['tp_pct'],
                        'sl_pct': sig['sl_pct'],
                        'max_hold_hours': sig['max_hold_hours'],
                        'hours_held': 0,
                    }
                    break

            # 3) 计时
            if holding is not None:
                holding['hours_held'] += 1

        # 每日收盘记录equity
        if date != last_equity_date:
            equity_curve.append({'date': date, 'capital': round(capital, 2)})
            last_equity_date = date

    # 强制平仓
    if holding is not None:
        last_date = [d for d in trade_dates if START_DATE <= d <= END_DATE][-1]
        last_bar = hourly.get((holding['code'], last_date, 4))
        if last_bar and last_bar[3] > 0:
            cost = holding['buy_price'] * (1 + FEE_RATE)
            net = last_bar[3] * (1 - FEE_RATE)
            ret = (net - cost) / cost
            capital_before = capital
            capital *= (1 + ret)
            trade_id += 1
            trades.append({
                'id': trade_id,
                'code': holding['code'],
                'name': holding['name'],
                'strategy': holding['strategy'],
                'signal_date': holding['signal_date'],
                'buy_date': holding['buy_date'],
                'buy_hour': 1,
                'buy_price': round(holding['buy_price'], 4),
                'sell_date': last_date,
                'sell_hour': 4,
                'sell_price': round(last_bar[3], 4),
                'sell_reason': 'force_close',
                'return_pct': round(ret * 100, 2),
                'hold_hours': holding['hours_held'],
                'capital_before': round(capital_before, 2),
                'capital_after': round(capital, 2),
            })

    if capital > peak:
        peak = capital
    dd = (peak - capital) / peak
    if dd > max_dd:
        max_dd = dd

    print(f"    → {len(trades)}笔交易, 期末资金{capital:,.0f} ({time.time()-t0:.1f}s)")
    return capital, trades, max_dd, equity_curve


# ============================================================
# 生成月度收益
# ============================================================
def calc_monthly_returns(trades):
    """按月计算复利收益"""
    month_rets = defaultdict(lambda: 1.0)
    for t in trades:
        m = t['buy_date'][:7]
        month_rets[m] *= (1 + t['return_pct'] / 100.0)

    monthly = []
    for m in sorted(month_rets.keys()):
        monthly.append({
            'month': m,
            'return_pct': round((month_rets[m] - 1) * 100, 2)
        })
    return monthly


# ============================================================
# 构建JSON输出
# ============================================================
def build_json(trades, equity_curve, strategy_name, cagr_pct, max_dd_pct):
    """构建完整JSON结构"""
    n = len(trades)
    wins = [t for t in trades if t['return_pct'] > 0]
    win_rate = len(wins) / n * 100 if n > 0 else 0
    avg_ret = np.mean([t['return_pct'] for t in trades]) if n > 0 else 0

    monthly = calc_monthly_returns(trades)

    # 简化equity_curve: 只保留有交易发生的日期 + 每月首日
    eq_dates = set()
    for t in trades:
        eq_dates.add(t['buy_date'])
        eq_dates.add(t['sell_date'])
    # 每月首个交易日
    seen_months = set()
    for e in equity_curve:
        m = e['date'][:7]
        if m not in seen_months:
            seen_months.add(m)
            eq_dates.add(e['date'])
    # 首末
    eq_dates.add(equity_curve[0]['date'])
    eq_dates.add(equity_curve[-1]['date'])

    filtered_eq = [e for e in equity_curve if e['date'] in eq_dates]

    final_capital = trades[-1]['capital_after'] if trades else INIT_CAPITAL

    result = {
        'trades': trades,
        'summary': {
            'strategy_name': strategy_name,
            'period': f"{START_DATE} ~ {END_DATE}",
            'initial_capital': INIT_CAPITAL,
            'final_capital': round(final_capital, 2),
            'cagr_pct': round(cagr_pct, 2),
            'total_trades': n,
            'win_rate_pct': round(win_rate, 2),
            'max_drawdown_pct': round(max_dd_pct, 2),
            'avg_return_pct': round(avg_ret, 2),
            'fee_rate': '0.1%双边',
            'slot': 1,
            'compliance': 'T+1/涨停不买/跌停不卖/gap-aware',
        },
        'equity_curve': filtered_eq,
        'monthly_returns': monthly,
    }
    return result


# ============================================================
# 从climitup_early_seal V2 已有数据生成独立版JSON (110.37% CAGR)
# ============================================================
def generate_limitup_standalone_from_v2(conn, trade_dates):
    """基于已验证的V2 trades数据，用整手约束重算资金曲线"""
    V2_PATH = '/home/AIWealth/logs/backtest/limitup_early_seal_v2_trades.json'
    print(f"  加载 V2 trades: {V2_PATH}")

    with open(V2_PATH, 'r') as f:
        v2_data = json.load(f)

    v2_trades = v2_data['trades']
    v2_trades_sorted = sorted(v2_trades, key=lambda x: (x['sell_date'], x['sell_hour']))
    print(f"    V2交易笔数: {len(v2_trades_sorted)}")

    # 获取股票名称
    codes = list(set(t['code'] for t in v2_trades_sorted))
    code_names = {}
    for i in range(0, len(codes), 100):
        batch = codes[i:i+100]
        placeholders = ','.join(['?'] * len(batch))
        cur = conn.execute(f"""
            SELECT code, code_name FROM stock_kline
            WHERE code IN ({placeholders}) AND code_name IS NOT NULL AND code_name != ''
            GROUP BY code
        """, batch)
        for row in cur.fetchall():
            code_names[row[0]] = row[1]

    # 获取信号日(buy_date前一个交易日)
    date_idx = {d: i for i, d in enumerate(trade_dates)}

    # 用整手约束计算资金曲线
    cash = float(INIT_CAPITAL)
    peak = cash
    max_dd = 0.0
    new_trades = []
    equity_curve = [{'date': START_DATE, 'capital': INIT_CAPITAL}]

    for i, t in enumerate(v2_trades_sorted):
        buy_price = t['buy_price']
        sell_price = t['sell_price']

        # 整手约束: 贰整百股
        shares = int(cash / (buy_price * (1 + FEE_RATE)) / 100) * 100
        if shares <= 0:
            continue

        capital_before = cash
        cost = shares * buy_price * (1 + FEE_RATE)
        cash -= cost
        proceeds = shares * sell_price * (1 - FEE_RATE)
        cash += proceeds
        capital_after = cash

        ret_pct = (capital_after - capital_before) / capital_before * 100

        if cash > peak:
            peak = cash
        dd = (peak - cash) / peak
        if dd > max_dd:
            max_dd = dd

        # 信号日 = buy_date前一个交易日
        buy_idx = date_idx.get(t['buy_date'])
        signal_date = trade_dates[buy_idx - 1] if buy_idx and buy_idx > 0 else ''

        new_trades.append({
            'id': i + 1,
            'code': t['code'],
            'name': code_names.get(t['code'], ''),
            'strategy': 'limitup_early_seal',
            'signal_date': signal_date,
            'buy_date': t['buy_date'],
            'buy_hour': t.get('buy_hour', 1),
            'buy_price': round(buy_price, 4),
            'sell_date': t['sell_date'],
            'sell_hour': t.get('sell_hour', 4),
            'sell_price': round(sell_price, 4),
            'sell_reason': t.get('reason', 'expired'),
            'return_pct': round(ret_pct, 2),
            'hold_hours': t.get('hold_hours', 7),
            'capital_before': round(capital_before, 2),
            'capital_after': round(capital_after, 2),
        })

        equity_curve.append({'date': t['sell_date'], 'capital': round(cash, 2)})

    # CAGR: 用trading_days/244计算年数(与引擎一致)
    n_days = len([d for d in trade_dates if START_DATE <= d <= END_DATE])
    years = n_days / 244.0
    cagr = ((cash / INIT_CAPITAL) ** (1 / years) - 1) * 100

    print(f"    → {len(new_trades)}笔交易, CAGR={cagr:.2f}%, MaxDD={max_dd*100:.2f}%")
    print(f"    期末资金: {cash:,.0f}")
    return new_trades, equity_curve, cagr, max_dd * 100


# ============================================================
# MAIN
# ============================================================
def main():
    t_total = time.time()
    print("=" * 80)
    print("生成交易明细JSON: 2策略联合 + limitup_early_seal独立")
    print(f"数据库: {DB_PATH}")
    print(f"区间: {START_DATE} ~ {END_DATE}")
    print("=" * 80)

    conn = sqlite3.connect(DB_PATH)

    # 交易日历
    cur = conn.execute(
        "SELECT DISTINCT date FROM stock_kline WHERE date >= '2020-06-01' ORDER BY date")
    trade_dates = [r[0] for r in cur.fetchall()]
    print(f"交易日: {len(trade_dates)}")

    # Step 1: 生成信号
    print("\n[Step 1] 生成信号")
    s1_signals = gen_early_seal_signals(conn, trade_dates)
    s3_signals = gen_friday_signals(conn, trade_dates)
    all_signals = s1_signals + s3_signals
    print(f"  总信号: {len(all_signals)} (S1={len(s1_signals)}, S3={len(s3_signals)})")

    # Step 2: 加载hourly
    print("\n[Step 2] 加载数据")
    hourly = load_hourly(conn, all_signals, trade_dates)
    conn.close()

    # Step 3: 联合回测
    print("\n[Step 3] 2策略联合回测")
    capital, trades, max_dd, equity_curve = simulate(all_signals, trade_dates, hourly)

    # 计算CAGR
    if trades:
        d1 = datetime.strptime(trades[0]['buy_date'], '%Y-%m-%d')
        d2 = datetime.strptime(trades[-1]['sell_date'], '%Y-%m-%d')
        years = max((d2 - d1).days / 365.25, 0.5)
        cagr_combined = ((capital / INIT_CAPITAL) ** (1 / years) - 1) * 100
    else:
        cagr_combined = 0

    # Step 4: 生成联合JSON
    print("\n[Step 4] 生成JSON文件")
    combined_json = build_json(
        trades, equity_curve,
        '2策略联合(limitup_early_seal+friday_limitup)',
        cagr_combined, max_dd * 100
    )

    with open(OUTPUT_COMBINED, 'w', encoding='utf-8') as f:
        json.dump(combined_json, f, ensure_ascii=False, indent=2)
    print(f"  ✓ 已写入: {OUTPUT_COMBINED}")

    # Step 5: limitup_early_seal独立 (V2, 110.37% CAGR)
    print("\n[Step 5] limitup_early_seal V2 独立版本 (含整手约束)")
    conn2 = sqlite3.connect(DB_PATH)
    lu_trades, lu_equity, lu_cagr, lu_dd = generate_limitup_standalone_from_v2(conn2, trade_dates)
    conn2.close()

    lu_json = build_json(
        lu_trades, lu_equity,
        'limitup_early_seal(独立)',
        lu_cagr, lu_dd
    )

    with open(OUTPUT_LIMITUP, 'w', encoding='utf-8') as f:
        json.dump(lu_json, f, ensure_ascii=False, indent=2)
    print(f"  ✓ 已写入: {OUTPUT_LIMITUP}")

    # Summary
    print("\n" + "=" * 80)
    print("【Summary - 2策略联合】")
    print(f"  策略: limitup_early_seal + friday_limitup")
    print(f"  区间: {START_DATE} ~ {END_DATE}")
    print(f"  交易笔数: {len(trades)}")
    print(f"  CAGR: {cagr_combined:.2f}%")
    print(f"  MaxDD: {max_dd*100:.2f}%")
    wins = [t for t in trades if t['return_pct'] > 0]
    print(f"  胜率: {len(wins)/len(trades)*100:.1f}%")
    print(f"  期末资金: {capital:,.0f}")
    s1_count = len([t for t in trades if t['strategy'] == 'limitup_early_seal'])
    s3_count = len([t for t in trades if t['strategy'] == 'friday_limitup'])
    print(f"  策略分布: limitup_early_seal={s1_count}笔, friday_limitup={s3_count}笔")

    print("\n【Summary - limitup_early_seal独立】")
    print(f"  交易笔数: {len(lu_trades)}")
    print(f"  CAGR: {lu_cagr:.2f}%")
    print(f"  MaxDD: {lu_dd:.2f}%")
    lu_wins = [t for t in lu_trades if t['return_pct'] > 0]
    if lu_trades:
        print(f"  胜率: {len(lu_wins)/len(lu_trades)*100:.1f}%")
        print(f"  期末资金: {lu_trades[-1]['capital_after']:,.0f}")

    print("=" * 80)
    print(f"\n总耗时: {time.time()-t_total:.1f}秒")


if __name__ == '__main__':
    main()
