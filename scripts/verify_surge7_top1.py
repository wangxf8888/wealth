#!/usr/bin/env python3
"""Surge7策略 Top1参数 独立回测验证
从零实现，独立验证 optimize_surge7.py 的 Top1 结果。

Top1参数（从 optimize_surge7.log 提取）:
  surge_threshold = 11.0%
  fallback_ratio  = 0.6
  tp_pct          = 8.0%
  sl_pct          = -3.0%
  max_hold_days   = 3
  buy_hour        = 1
  n_slots         = 1
  wait_days       = 0
  min_turn        = 5.0
  market_filter   = True

验证集: 2021-01-01 ~ 2026-06-30
期望: CAGR=+120.2% WR=39.9% MDD=-84.9% Trades=759 AvgPnl=+1.09%
"""
import sys, sqlite3, math
from datetime import datetime, timedelta
from collections import defaultdict

sys.path.insert(0, '/home/AIWealth/scripts')
from compliance_utils import (
    safe_float, is_limit_up, is_limit_down, is_one_word_board, is_st,
    get_limit_threshold, is_valid_price, check_t1
)

DB_PATH = '/home/AIWealth/data/stocks.db'
INITIAL_CAPITAL = 1_000_000.0

SURGE_THRESHOLD = 11.0
FALLBACK_RATIO = 0.6
TP_PCT = 8.0
SL_PCT = -3.0
MAX_HOLD_DAYS = 3
BUY_HOUR = 1
N_SLOTS = 1
WAIT_DAYS = 0
MIN_TURN = 5.0
MARKET_FILTER = True

BT_START = '2021-01-01'
BT_END = '2026-06-30'

def sf(val):
    if val is None: return 0.0
    try:
        f = float(val)
        return 0.0 if (math.isnan(f) or math.isinf(f)) else f
    except: return 0.0

def load_data():
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    lookback_start = (datetime.strptime(BT_START, "%Y-%m-%d") - timedelta(days=60)).strftime("%Y-%m-%d")
    cur.execute("SELECT DISTINCT date FROM stock_kline WHERE date>=? AND date<=? ORDER BY date", (lookback_start, BT_END))
    all_dates = [r[0] for r in cur.fetchall()]
    date_to_idx = {d: i for i, d in enumerate(all_dates)}
    bt_dates = [d for d in all_dates if BT_START <= d <= BT_END]
    print(f"  总交易日: {len(all_dates)} | 回测日: {len(bt_dates)}")
    fields = ('date, code, preclose, '
              'hour1_open, hour1_high, hour1_low, hour1_close, '
              'hour2_open, hour2_high, hour2_low, hour2_close, '
              'hour3_open, hour3_high, hour3_low, hour3_close, '
              'hour4_open, hour4_high, hour4_low, hour4_close, '
              'open_rate, turn, high_rate, close_rate, high, low, open, close, isST, code_name')
    day_data = {}
    total_rows = 0
    years = sorted(set(d[:4] for d in all_dates))
    for year in years:
        y_start = max(lookback_start, f"{year}-01-01")
        y_end = min(BT_END, f"{year}-12-31")
        if y_start > y_end: continue
        cur.execute(f"SELECT {fields} FROM stock_kline WHERE date>=? AND date<=?", (y_start, y_end))
        cnt = 0
        for row in cur:
            d, code = row[0], row[1]
            di = date_to_idx.get(d)
            if di is None: continue
            t = (row[2], row[3],row[4],row[5],row[6], row[7],row[8],row[9],row[10],
                 row[11],row[12],row[13],row[14], row[15],row[16],row[17],row[18],
                 row[19], row[20], row[21], row[22], row[23], row[24], row[25], row[26], row[27], row[28])
            if di not in day_data: day_data[di] = {}
            day_data[di][code] = t
            cnt += 1
        total_rows += cnt
        print(f"    {year}: {cnt:,} 行")
    sys.stdout.flush()
    signal_cands = {}
    for di, stocks in day_data.items():
        cands = []
        for code, t in stocks.items():
            if code.startswith('bj.'): continue
            ist = t[25]
            cname = t[26] or ''
            if ist == 1 or 'ST' in cname.upper(): continue
            high_rate = sf(t[19])
            if high_rate < 5.0: continue
            close_rate = sf(t[20])
            preclose = sf(t[0])
            close_p = sf(t[24])
            high_p = sf(t[21])
            low_p = sf(t[22])
            open_p = sf(t[23])
            if preclose <= 0: continue
            if close_p > 0 and is_limit_up(code, close_p, preclose): continue
            if is_one_word_board(open_p, high_p, low_p, close_p): continue
            turn = sf(t[18])
            cands.append((code, high_rate, close_rate, turn))
        if cands:
            cands.sort(key=lambda x: -x[1])
            signal_cands[di] = cands
    market_data = {}
    cur.execute("SELECT date, close_rate FROM index_kline WHERE code='sh.000001' AND date>=? AND date<=?", (lookback_start, BT_END))
    for row in cur:
        market_data[row[0]] = sf(row[1])
    conn.close()
    print(f"  数据加载完成: {total_rows:,}行 | 信号日: {len(signal_cands)}天")
    sys.stdout.flush()
    return all_dates, date_to_idx, bt_dates, day_data, signal_cands, market_data

def run_backtest(all_dates, date_to_idx, bt_dates, day_data, signal_cands, market_data):
    surge_th = SURGE_THRESHOLD
    fallback_r = FALLBACK_RATIO
    tp = TP_PCT
    sl = SL_PCT
    max_hold = MAX_HOLD_DAYS
    buy_hour = BUY_HOUR
    n_slots = N_SLOTS
    wait_days = WAIT_DAYS
    min_turn = MIN_TURN
    mkt_filter = MARKET_FILTER
    bh_off = (buy_hour - 1) * 4 + 1
    cash = INITIAL_CAPITAL
    positions = []
    trades = []
    equity_curve = []
    tp_active = tp < 900
    sl_active = sl > -900

    for today in bt_dates:
        today_idx = date_to_idx.get(today, -1)
        today_stocks = day_data.get(today_idx)
        if not today_stocks: continue

        # Sell
        if positions:
            survived = []
            for pos_code, pos_bp, pos_bd_idx, pos_shares, pos_bd_str, pos_cname in positions:
                if pos_bd_idx == today_idx:
                    survived.append((pos_code, pos_bp, pos_bd_idx, pos_shares, pos_bd_str, pos_cname))
                    continue
                hold_days = today_idx - pos_bd_idx
                t = today_stocks.get(pos_code)
                if not t:
                    survived.append((pos_code, pos_bp, pos_bd_idx, pos_shares, pos_bd_str, pos_cname))
                    continue
                preclose = sf(t[0])
                sold = False; sell_price = 0.0; sell_reason = ''; sell_hour = 0
                for h in range(4):
                    off = h * 4 + 1
                    hc = sf(t[off+3])
                    if hc <= 0: continue
                    ho = sf(t[off]); hh = sf(t[off+1]); hl = sf(t[off+2])
                    if ho > 0 and hh > 0 and hl > 0 and (hh - hl) < 0.01:
                        if preclose > 0 and is_limit_down(pos_code, hc, preclose):
                            continue
                    pnl_pct = (hc / pos_bp - 1.0) * 100.0
                    if tp_active and pnl_pct >= tp:
                        sold = True; sell_price = hc; sell_reason = f'止盈({pnl_pct:+.2f}%)'; sell_hour = h+1; break
                    elif sl_active and pnl_pct <= sl:
                        sold = True; sell_price = hc; sell_reason = f'止损({pnl_pct:+.2f}%)'; sell_hour = h+1; break
                    elif h == 3 and hold_days >= max_hold:
                        sold = True; sell_price = hc; sell_reason = f'到期({hold_days}天)'; sell_hour = h+1; break
                if sold:
                    pnl = (sell_price / pos_bp - 1.0) * 100.0
                    cash += pos_shares * sell_price
                    trades.append({'code': pos_code, 'code_name': pos_cname,
                                   'buy_date': pos_bd_str, 'buy_price': pos_bp, 'buy_hour': buy_hour,
                                   'sell_date': today, 'sell_price': sell_price, 'sell_hour': sell_hour,
                                   'pnl_pct': pnl, 'hold_days': hold_days, 'sell_reason': sell_reason})
                else:
                    survived.append((pos_code, pos_bp, pos_bd_idx, pos_shares, pos_bd_str, pos_cname))
            positions = survived

        # Buy
        if len(positions) < n_slots:
            skip_buy = False
            if mkt_filter:
                mkt_rate = market_data.get(today, 0.0)
                if mkt_rate < -1.0: skip_buy = True
            if not skip_buy:
                signal_offset = 1 + wait_days
                sig_di = today_idx - signal_offset
                if sig_di >= 0:
                    cands = signal_cands.get(sig_di)
                    if cands:
                        held_codes = {p[0] for p in positions} if positions else set()
                        check_callback = wait_days > 0
                        for code, hr, cr, turn in cands:
                            if len(positions) >= n_slots: break
                            if code in held_codes: continue
                            if hr < surge_th: continue
                            if cr >= hr * fallback_r: continue
                            if min_turn > 0 and turn < min_turn: continue
                            buy_t = today_stocks.get(code)
                            if not buy_t: continue
                            if buy_t[25] == 1: continue
                            cn2 = buy_t[26] or ''
                            if 'ST' in cn2.upper(): continue
                            if check_callback:
                                opr = sf(buy_t[17])
                                if opr > 0: continue
                            h_o = sf(buy_t[bh_off])
                            h_h = sf(buy_t[bh_off+1])
                            h_l = sf(buy_t[bh_off+2])
                            h_c = sf(buy_t[bh_off+3])
                            if h_o <= 0: continue
                            if is_one_word_board(h_o, h_h, h_l, h_c): continue
                            preclose_b = sf(buy_t[0])
                            if preclose_b > 0 and is_limit_up(code, h_o, preclose_b): continue
                            free_slots = n_slots - len(positions)
                            alloc = cash / free_slots if free_slots > 0 else cash
                            shares = int(alloc / h_o // 100) * 100
                            if shares <= 0: continue
                            cost = shares * h_o
                            if cost > cash: continue
                            cash -= cost
                            positions.append((code, h_o, today_idx, shares, today, cn2))
                            held_codes.add(code)

        # Equity
        equity = cash
        for pc, pbp, _, psh, _, _ in positions:
            t2 = today_stocks.get(pc)
            if t2:
                p = sf(t2[16]) or sf(t2[24]) or pbp
            else:
                p = pbp
            equity += psh * p
        equity_curve.append((today, equity))

    # Force close
    if positions and bt_dates:
        last_idx = date_to_idx.get(bt_dates[-1], -1)
        last_stocks = day_data.get(last_idx, {})
        for pos_code, pos_bp, pos_bd_idx, pos_shares, pos_bd_str, pos_cname in positions:
            t2 = last_stocks.get(pos_code)
            sp = (sf(t2[16]) or sf(t2[24]) or pos_bp) if t2 else pos_bp
            pnl = (sp / pos_bp - 1.0) * 100.0
            cash += pos_shares * sp
            trades.append({'code': pos_code, 'code_name': pos_cname,
                           'buy_date': pos_bd_str, 'buy_price': pos_bp, 'buy_hour': buy_hour,
                           'sell_date': bt_dates[-1], 'sell_price': sp, 'sell_hour': 4,
                           'pnl_pct': pnl, 'hold_days': last_idx - pos_bd_idx, 'sell_reason': '强制清仓'})
    final_equity = cash
    return trades, equity_curve, final_equity

def print_trade_details(trades, max_show=30):
    print(f"\n{'='*90}")
    print(f"  交易明细（共{len(trades)}笔，显示前{min(max_show, len(trades))}笔）")
    print(f"{'='*90}")
    print(f"{'序号':>4} | {'买入日期':<10} | {'股票':<12} | {'买入价':>8} | {'卖出日期':<10} | {'卖出价':>8} | {'卖出原因':<14} | {'盈亏%':>7} | {'天数':>4}")
    print("-" * 90)
    for i, t in enumerate(trades[:max_show], 1):
        print(f"{i:4d} | {t['buy_date']:<10} | {t['code']:<12} | {t['buy_price']:8.2f} | "
              f"{t['sell_date']:<10} | {t['sell_price']:8.2f} | {t['sell_reason']:<14} | "
              f"{t['pnl_pct']:+7.2f} | {t['hold_days']:4d}")
    if len(trades) > max_show:
        print(f"  ... 省略 {len(trades) - max_show} 笔 ...")

def print_annual_summary(trades, equity_curve):
    print(f"\n{'='*70}")
    print("  年度汇总")
    print(f"{'='*70}")
    year_trades = defaultdict(list)
    for t in trades:
        year_trades[t['sell_date'][:4]].append(t)
    year_equity = defaultdict(list)
    for d, eq in equity_curve:
        year_equity[d[:4]].append(eq)
    print(f"{'年份':>6} | {'交易数':>6} | {'胜率':>6} | {'总收益%':>10} | {'平均盈亏%':>10} | {'最大回撤%':>10}")
    print("-" * 70)
    for year in sorted(year_trades.keys()):
        ts = year_trades[year]
        n = len(ts)
        wins = sum(1 for t in ts if t['pnl_pct'] > 0)
        wr = wins / n * 100 if n > 0 else 0
        total_pnl = sum(t['pnl_pct'] for t in ts)
        avg_pnl = total_pnl / n if n > 0 else 0
        eqs = year_equity.get(year, [])
        peak = 0; mdd = 0
        for eq in eqs:
            if eq > peak: peak = eq
            dd = (peak - eq) / peak * 100 if peak > 0 else 0
            if dd > mdd: mdd = dd
        print(f"{year:>6} | {n:6d} | {wr:5.1f}% | {total_pnl:+10.2f} | {avg_pnl:+10.2f} | {mdd:10.1f}%")

def print_compliance_report(trades):
    print(f"\n{'='*60}")
    print("  合规性报告")
    print(f"{'='*60}")
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    issues = {'T+0违规': 0, '涨停买入': 0, '跌停卖出': 0, '一字板买入': 0, 'ST买入': 0, '价格范围异常': 0}
    sample_violations = []
    for t in trades:
        if not check_t1(t['buy_date'], t['sell_date']):
            issues['T+0违规'] += 1
            sample_violations.append(f"T+0: {t['buy_date']} {t['code']}")
            continue
        cur.execute("SELECT preclose, hour1_open, hour1_high, hour1_low, hour1_close, isST, code_name "
                    "FROM stock_kline WHERE date=? AND code=?", (t['buy_date'], t['code']))
        row = cur.fetchone()
        if row:
            preclose = sf(row[0])
            h_o, h_h, h_l, h_c = sf(row[1]), sf(row[2]), sf(row[3]), sf(row[4])
            ist, cname = row[5], row[6] or ''
            if is_st(cname, ist):
                issues['ST买入'] += 1
                sample_violations.append(f"ST: {t['buy_date']} {t['code']} {cname}")
            if preclose > 0 and is_limit_up(t['code'], t['buy_price'], preclose):
                issues['涨停买入'] += 1
                sample_violations.append(f"涨停买入: {t['buy_date']} {t['code']}")
            if is_one_word_board(h_o, h_h, h_l, h_c):
                issues['一字板买入'] += 1
                sample_violations.append(f"一字板: {t['buy_date']} {t['code']}")
            if h_l > 0 and h_h > 0 and not is_valid_price(t['buy_price'], h_l, h_h):
                issues['价格范围异常'] += 1
                sample_violations.append(f"价格越界: {t['buy_date']} {t['code']} bp={t['buy_price']:.2f} [{h_l:.2f},{h_h:.2f}]")
        cur.execute("SELECT preclose, close FROM stock_kline WHERE date=? AND code=?", (t['sell_date'], t['code']))
        row2 = cur.fetchone()
        if row2:
            pre_s, close_s = sf(row2[0]), sf(row2[1])
            if pre_s > 0 and is_limit_down(t['code'], close_s, pre_s):
                issues['跌停卖出'] += 1
                sample_violations.append(f"跌停卖出: {t['sell_date']} {t['code']}")
    conn.close()
    total = len(trades)
    total_issues = sum(issues.values())
    print(f"  总交易: {total}")
    print(f"  合规通过: {total - total_issues} ({(total-total_issues)/max(1,total)*100:.1f}%)")
    print(f"  存在问题: {total_issues}")
    print("-" * 60)
    for k, v in issues.items():
        status = '+' if v == 0 else f'X ({v}笔)'
        print(f"  {k}: {status}")
    if sample_violations:
        print(f"\n  前5条违规样本:")
        for s in sample_violations[:5]:
            print(f"    {s}")

def compute_metrics(trades, equity_curve, final_equity):
    trade_count = len(trades)
    wins = sum(1 for t in trades if t['pnl_pct'] > 0)
    losses = trade_count - wins
    total_pnl = sum(t['pnl_pct'] for t in trades)
    win_rate = wins / trade_count * 100 if trade_count > 0 else 0
    avg_pnl = total_pnl / trade_count if trade_count > 0 else 0
    peak = 0; max_dd = 0
    for _, eq in equity_curve:
        if eq > peak: peak = eq
        dd = (peak - eq) / peak * 100 if peak > 0 else 0
        if dd > max_dd: max_dd = dd
    if len(equity_curve) >= 2:
        d0 = datetime.strptime(equity_curve[0][0], "%Y-%m-%d")
        d1 = datetime.strptime(equity_curve[-1][0], "%Y-%m-%d")
        years = max(0.1, (d1 - d0).days / 365.25)
    else:
        years = 1.0
    ratio = final_equity / INITIAL_CAPITAL
    cagr = (ratio ** (1.0 / years) - 1.0) * 100.0 if ratio > 0 else -100.0
    total_return = (ratio - 1.0) * 100.0
    return {'trade_count': trade_count, 'wins': wins, 'losses': losses, 'win_rate': win_rate,
            'avg_pnl': avg_pnl, 'total_return': total_return, 'cagr': cagr, 'max_dd': max_dd,
            'final_equity': final_equity, 'years': years}

def main():
    print("=" * 70)
    print("  Surge7 策略 Top1 参数 独立回测验证")
    print("=" * 70)
    print(f"参数: surge={SURGE_THRESHOLD}% fb={FALLBACK_RATIO} tp={TP_PCT}% sl={SL_PCT}%")
    print(f"      hold={MAX_HOLD_DAYS}d buy_h={BUY_HOUR} slots={N_SLOTS} wait={WAIT_DAYS} turn>{MIN_TURN} mkt={MARKET_FILTER}")
    print(f"回测: {BT_START} ~ {BT_END} | 初始资金: {INITIAL_CAPITAL:,.0f}")
    print(f"期望: CAGR=+120.2% WR=39.9% Trades=759")
    print("=" * 70)
    sys.stdout.flush()

    print("\n[1] 加载数据...")
    sys.stdout.flush()
    all_dates, date_to_idx, bt_dates, day_data, signal_cands, market_data = load_data()

    print("\n[2] 执行回测...")
    sys.stdout.flush()
    trades, equity_curve, final_equity = run_backtest(all_dates, date_to_idx, bt_dates, day_data, signal_cands, market_data)

    metrics = compute_metrics(trades, equity_curve, final_equity)
    print_trade_details(trades)
    print_annual_summary(trades, equity_curve)
    print_compliance_report(trades)

    print(f"\n{'='*70}")
    print("  最终结果汇总")
    print(f"{'='*70}")
    print(f"  交易笔数:   {metrics['trade_count']}")
    print(f"  胜/负:      {metrics['wins']}/{metrics['losses']}")
    print(f"  胜率:       {metrics['win_rate']:.1f}%")
    print(f"  平均盈亏:   {metrics['avg_pnl']:+.2f}%")
    print(f"  总收益:     {metrics['total_return']:+.1f}%")
    print(f"  最大回撤:   -{metrics['max_dd']:.1f}%")
    print(f"  最终权益:   {metrics['final_equity']:,.0f}")
    print(f"  回测年数:   {metrics['years']:.2f}")
    print(f"  CAGR:       {metrics['cagr']:+.1f}%")

    print(f"\n{'='*70}")
    print("  与优化脚本结果对比")
    print(f"{'='*70}")
    exp_cagr, exp_trades, exp_wr, exp_mdd = 120.2, 759, 39.9, 84.9
    cagr_diff = abs(metrics['cagr'] - exp_cagr)
    print(f"  指标     | 优化脚本   | 本次验证   | 偏差")
    print(f"  ---------|-----------|-----------|------")
    print(f"  CAGR     | {exp_cagr:+.1f}%  | {metrics['cagr']:+.1f}%  | {cagr_diff:.1f}%")
    print(f"  交易数   |    {exp_trades}    |    {metrics['trade_count']}    | {abs(metrics['trade_count']-exp_trades)}")
    print(f"  胜率     | {exp_wr:.1f}%   | {metrics['win_rate']:.1f}%   | {abs(metrics['win_rate']-exp_wr):.1f}%")
    print(f"  最大回撤 | -{exp_mdd:.1f}%  | -{metrics['max_dd']:.1f}%  | {abs(metrics['max_dd']-exp_mdd):.1f}%")

    if cagr_diff > 5:
        print(f"\n  WARNING: CAGR偏差 {cagr_diff:.1f}% > 5%, 需要排查原因!")
    else:
        print(f"\n  PASS: CAGR偏差 {cagr_diff:.1f}% <= 5%, 验证通过!")

    print(f"\n完成时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    sys.stdout.flush()

if __name__ == '__main__':
    main()
