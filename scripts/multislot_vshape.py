#!/usr/bin/env python3
"""V字形态高开策略 - 多仓资金管理模型回测
初始100万，分成N个仓位，每仓=当前总净值/N。
测试 N=[3,5,7,10]，按Calmar排序找最优配置。
"""
import sys, sqlite3, math, time
from datetime import datetime

sys.path.insert(0, '/home/AIWealth/scripts')
from compliance_utils import (
    safe_float, is_one_word_board, is_limit_up, is_limit_down,
    is_st, get_limit_threshold, is_valid_price
)

CONFIG = {
    'v_lookback_days': 4,
    'v_cum_drop': -3.0,
    'v_min_down_days': 2,
    'gap_up_min': 4.0,
    'gap_up_max': 20.0,
    'tp_pct': 10.0,
    'sl_pct': -2.0,
    'max_hold_days': 3,
    'board_filter': 'sz.300',
    'min_turn': 8.0,
}

DB_PATH = '/home/AIWealth/data/stocks.db'
INITIAL_CAPITAL = 1_000_000.0
START_DATE = '2021-01-01'
END_DATE = '2026-06-30'
N_SLOTS_LIST = [3, 5, 7, 10]


class Portfolio:
    def __init__(self, initial_capital=1_000_000, n_slots=5):
        self.total_equity = initial_capital
        self.cash = initial_capital
        self.n_slots = n_slots
        self.positions = []

    @property
    def slot_size(self):
        return self.total_equity / self.n_slots

    @property
    def available_slots(self):
        return self.n_slots - len(self.positions)

    def can_buy(self):
        return len(self.positions) < self.n_slots and self.cash >= self.slot_size * 0.95

    def buy(self, code, code_name, price, date, hour):
        amount = self.slot_size
        shares = int(amount / price / 100) * 100
        if shares < 100:
            return None
        cost = shares * price
        if cost > self.cash:
            return None
        self.cash -= cost
        pos = {
            'code': code, 'code_name': code_name,
            'price': price, 'shares': shares, 'cost': cost,
            'buy_date': date, 'buy_hour': hour,
            'hold_days': 0, 'last_price': price
        }
        self.positions.append(pos)
        return pos

    def sell(self, pos, price, date, hour, reason):
        revenue = pos['shares'] * price
        self.cash += revenue
        profit_pct = (price / pos['price'] - 1) * 100
        self.positions.remove(pos)
        return {'profit_pct': profit_pct, 'revenue': revenue, 'reason': reason}

    def update_equity(self, market_prices=None):
        position_value = 0.0
        for p in self.positions:
            if market_prices and p['code'] in market_prices:
                mp = market_prices[p['code']]
                if mp > 0:
                    p['last_price'] = mp
                    position_value += p['shares'] * mp
                else:
                    position_value += p['shares'] * p['last_price']
            else:
                position_value += p['shares'] * p['last_price']
        self.total_equity = self.cash + position_value


def get_hour_price(row, hour, field):
    return row.get(f'h{hour}_{field}', 0.0)


def load_data():
    t0 = time.time()
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()

    cur.execute("SELECT DISTINCT date FROM stock_kline WHERE date>=? AND date<=? ORDER BY date",
                (START_DATE, END_DATE))
    trading_dates = [r[0] for r in cur.fetchall()]
    print(f"  交易日: {len(trading_dates)}个")

    all_data = {}
    board_cond = "AND code LIKE 'sz.300%'"
    for year in range(2021, 2027):
        y_s, y_e = f"{year}-01-01", f"{year}-12-31"
        cur.execute(f"""SELECT date, code, code_name, preclose, open, close,
                        open_rate, close_rate, high, low, turn, isST,
                        hour1_open, hour1_high, hour1_low, hour1_close,
                        hour2_open, hour2_high, hour2_low, hour2_close,
                        hour3_open, hour3_high, hour3_low, hour3_close,
                        hour4_open, hour4_high, hour4_low, hour4_close
                    FROM stock_kline
                    WHERE date>=? AND date<=? {board_cond} AND isST=0""", (y_s, y_e))
        count = 0
        for r in cur:
            all_data[(r[0], r[1])] = {
                'date': r[0], 'code': r[1], 'code_name': r[2],
                'preclose': safe_float(r[3]), 'open': safe_float(r[4]),
                'close': safe_float(r[5]), 'open_rate': safe_float(r[6], None),
                'close_rate': safe_float(r[7], None),
                'high': safe_float(r[8]), 'low': safe_float(r[9]),
                'turn': safe_float(r[10], 0.0), 'isST': r[11],
                'h1_open': safe_float(r[12]), 'h1_high': safe_float(r[13]),
                'h1_low': safe_float(r[14]), 'h1_close': safe_float(r[15]),
                'h2_open': safe_float(r[16]), 'h2_high': safe_float(r[17]),
                'h2_low': safe_float(r[18]), 'h2_close': safe_float(r[19]),
                'h3_open': safe_float(r[20]), 'h3_high': safe_float(r[21]),
                'h3_low': safe_float(r[22]), 'h3_close': safe_float(r[23]),
                'h4_open': safe_float(r[24]), 'h4_high': safe_float(r[25]),
                'h4_low': safe_float(r[26]), 'h4_close': safe_float(r[27]),
            }
            count += 1
        print(f"    {year}: {count}条", flush=True)

    cr_history = {}
    for (dt, code), row in all_data.items():
        if row['close_rate'] is not None:
            cr_history.setdefault(code, []).append((dt, row['close_rate']))
    for code in cr_history:
        cr_history[code].sort()
    print(f"  股票数: {len(cr_history)}")

    date_stocks = {}
    for (dt, code), row in all_data.items():
        date_stocks.setdefault(dt, []).append((code, row))

    lb_days = CONFIG['v_lookback_days']
    lookback_cache = {}
    for code, hist in cr_history.items():
        for pos in range(lb_days, len(hist)):
            target_date = hist[pos][0]
            window = hist[pos - lb_days: pos]
            cum = sum(x[1] for x in window)
            dd = sum(1 for x in window if x[1] < 0)
            lookback_cache[(target_date, code)] = (cum, dd)
    print(f"  lookback缓存: {len(lookback_cache)}条")
    print(f"  数据加载耗时: {time.time()-t0:.1f}s", flush=True)

    conn.close()
    return trading_dates, all_data, date_stocks, lookback_cache


def run_single_backtest(n_slots, trading_dates, all_data, date_stocks, lookback_cache):
    portfolio = Portfolio(INITIAL_CAPITAL, n_slots)
    trades = []
    year_start_eq = {}
    year_end_eq = {}
    peak = INITIAL_CAPITAL
    max_dd = 0.0
    prev_year = None

    for di, today in enumerate(trading_dates):
        cur_year = today[:4]
        if prev_year is None:
            year_start_eq[cur_year] = INITIAL_CAPITAL
        elif cur_year != prev_year:
            year_start_eq[cur_year] = year_end_eq.get(prev_year, INITIAL_CAPITAL)
        prev_year = cur_year

        for pos in portfolio.positions:
            if pos['buy_date'] != today:
                pos['hold_days'] += 1

        portfolio.update_equity()

        # find candidates
        candidates = []
        stocks_today = date_stocks.get(today, [])
        held_codes = {p['code'] for p in portfolio.positions}
        for code, row in stocks_today:
            if code in held_codes:
                continue
            if is_st(row['code_name'], row.get('isST', 0)):
                continue
            open_rate = row['open_rate']
            if open_rate is None:
                continue
            if open_rate < CONFIG['gap_up_min']:
                continue
            gap_no_max = CONFIG['gap_up_max'] >= 20.0
            if not gap_no_max and open_rate > CONFIG['gap_up_max']:
                continue
            preclose = row['preclose']
            if preclose <= 0 or row['open'] <= 0:
                continue
            if is_one_word_board(row['open'], row['high'], row['low'], row['close']):
                continue
            h1o = row['h1_open']
            if h1o <= 0 or is_one_word_board(h1o, row['h1_high'], row['h1_low'], row['h1_close']):
                continue
            if is_limit_up(code, h1o, preclose):
                continue
            if row['turn'] < CONFIG['min_turn']:
                continue
            lb_val = lookback_cache.get((today, code))
            if lb_val is None:
                continue
            cum_drop, down_days = lb_val
            if cum_drop <= CONFIG['v_cum_drop'] and down_days >= CONFIG['v_min_down_days']:
                candidates.append({
                    'code': code, 'code_name': row['code_name'],
                    'h1_open': h1o, 'cum_drop': cum_drop,
                    'turn': row['turn'],
                })

        candidates.sort(key=lambda x: x['turn'], reverse=True)

        for hour in range(1, 5):
            survived = []
            for pos in list(portfolio.positions):
                if pos['buy_date'] == today:
                    survived.append(pos)
                    continue
                key = (today, pos['code'])
                row = all_data.get(key)
                if not row:
                    survived.append(pos)
                    continue
                hc = get_hour_price(row, hour, 'close')
                if hc <= 0:
                    survived.append(pos)
                    continue
                preclose_p = row['preclose']
                if preclose_p > 0 and is_limit_down(pos['code'], hc, preclose_p):
                    ho = get_hour_price(row, hour, 'open')
                    hh = get_hour_price(row, hour, 'high')
                    hl = get_hour_price(row, hour, 'low')
                    if is_one_word_board(ho, hh, hl, hc):
                        pos['last_price'] = hc
                        survived.append(pos)
                        continue

                pnl_pct = (hc - pos['price']) / pos['price'] * 100.0
                sell = False
                sell_reason = ''
                if pnl_pct >= CONFIG['tp_pct']:
                    sell = True
                    sell_reason = f'TP h{hour}({pnl_pct:+.1f}%>={CONFIG["tp_pct"]}%)'
                elif pnl_pct <= CONFIG['sl_pct']:
                    sell = True
                    sell_reason = f'SL h{hour}({pnl_pct:+.1f}%<={CONFIG["sl_pct"]}%)'
                elif pos['hold_days'] >= CONFIG['max_hold_days'] and hour == 4:
                    sell = True
                    sell_reason = f'到期h4({pos["hold_days"]}d>={CONFIG["max_hold_days"]})'

                if sell:
                    result = portfolio.sell(pos, hc, today, hour, sell_reason)
                    trades.append({
                        'code': pos['code'], 'code_name': pos['code_name'],
                        'buy_date': pos['buy_date'], 'buy_price': pos['price'],
                        'buy_hour': pos['buy_hour'],
                        'sell_date': today, 'sell_price': hc,
                        'sell_hour': hour, 'sell_reason': sell_reason,
                        'pnl_pct': result['profit_pct'],
                        'hold_days': pos['hold_days'], 'shares': pos['shares'],
                    })
                else:
                    pos['last_price'] = hc
                    survived.append(pos)
            portfolio.positions = survived

            if hour == 1 and candidates:
                for cand in candidates:
                    if not portfolio.can_buy():
                        break
                    if cand['code'] in {p['code'] for p in portfolio.positions}:
                        continue
                    if portfolio.slot_size < 5000:
                        continue
                    portfolio.buy(cand['code'], cand['code_name'],
                                  cand['h1_open'], today, 1)

        market_prices = {}
        for pos in portfolio.positions:
            key = (today, pos['code'])
            row = all_data.get(key)
            if row and row['close'] > 0:
                market_prices[pos['code']] = row['close']
        portfolio.update_equity(market_prices)

        year_end_eq[cur_year] = portfolio.total_equity
        if portfolio.total_equity > peak:
            peak = portfolio.total_equity
        dd = (peak - portfolio.total_equity) / peak * 100 if peak > 0 else 0
        if dd > max_dd:
            max_dd = dd

    if portfolio.positions:
        last_date = trading_dates[-1]
        for pos in list(portfolio.positions):
            key = (last_date, pos['code'])
            row = all_data.get(key)
            sp = pos['last_price']
            if row and row['close'] > 0:
                sp = row['close']
            result = portfolio.sell(pos, sp, last_date, 4, '回测结束清仓')
            trades.append({
                'code': pos['code'], 'code_name': pos['code_name'],
                'buy_date': pos['buy_date'], 'buy_price': pos['price'],
                'buy_hour': pos['buy_hour'],
                'sell_date': last_date, 'sell_price': sp,
                'sell_hour': 4, 'sell_reason': '回测结束清仓',
                'pnl_pct': result['profit_pct'],
                'hold_days': pos['hold_days'], 'shares': pos['shares'],
            })

    final_equity = portfolio.total_equity
    n_days = len(trading_dates)
    total_years = max(0.05, n_days / 245.0)
    cagr = ((final_equity / INITIAL_CAPITAL) ** (1.0 / total_years) - 1) * 100

    yearly_rets = {}
    for yr in sorted(year_start_eq.keys()):
        s = year_start_eq[yr]
        e = year_end_eq.get(yr, s)
        yearly_rets[yr] = ((e / s - 1) * 100) if s > 0 else 0.0

    calmar = cagr / max_dd if max_dd > 0 else 999.0
    n_wins = sum(1 for t in trades if t['pnl_pct'] > 0)
    win_rate = n_wins / len(trades) * 100 if trades else 0

    return {
        'n_slots': n_slots,
        'final_equity': final_equity,
        'cagr': cagr,
        'max_dd': max_dd,
        'calmar': calmar,
        'total_trades': len(trades),
        'win_rate': win_rate,
        'yearly_rets': yearly_rets,
        'trades': trades,
    }


def main():
    t0 = time.time()
    print("=" * 70)
    print("  V字策略多仓模型回测")
    print(f"  时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print("=" * 70)
    print(f"\n  初始资金: {INITIAL_CAPITAL:,.0f}")
    print(f"  回测区间: {START_DATE} ~ {END_DATE}")
    print(f"  仓位测试: {N_SLOTS_LIST}")
    print(f"\n  策略参数:")
    for k, v in CONFIG.items():
        print(f"    {k} = {v}")
    print("\n" + "-" * 70)
    print("  [1] 加载数据...", flush=True)

    trading_dates, all_data, date_stocks, lookback_cache = load_data()

    print("\n" + "-" * 70)
    print("  [2] 执行多仓回测...", flush=True)

    results = []
    for n_slots in N_SLOTS_LIST:
        t1 = time.time()
        print(f"\n  --- 回测 N={n_slots}仓 ---", flush=True)
        res = run_single_backtest(n_slots, trading_dates, all_data, date_stocks, lookback_cache)
        results.append(res)
        print(f"    耗时: {time.time()-t1:.1f}s | 交易{res['total_trades']}笔 | "
              f"CAGR={res['cagr']:+.1f}% | MDD={res['max_dd']:.1f}%")

    print("\n\n" + "=" * 70)
    print("  ===== V字策略多仓模型回测结果 =====")
    print("=" * 70)
    print(f"  初始资金: {INITIAL_CAPITAL:,.0f}")
    print()

    for res in results:
        print(f"  --- N={res['n_slots']}仓 ---")
        print(f"  最终净值: {res['final_equity']:>15,.0f}")
        print(f"  CAGR:     {res['cagr']:>+12.1f}%")
        print(f"  总交易数: {res['total_trades']:>12d}笔")
        print(f"  胜率:     {res['win_rate']:>12.1f}%")
        print(f"  最大回撤: {res['max_dd']:>12.1f}%")
        print(f"  Calmar:   {res['calmar']:>12.2f} (CAGR/MDD)")
        print(f"  年度明细:")
        yr_line = "    "
        for yr in sorted(res['yearly_rets'].keys()):
            yr_line += f"{yr}: {res['yearly_rets'][yr]:+.1f}%  "
        print(yr_line)
        print()

    print("=" * 70)
    print("  ===== 最优配置 =====")
    print("=" * 70)
    print("  按Calmar(风险调整收益)排序:")
    sorted_results = sorted(results, key=lambda x: x['calmar'], reverse=True)
    for rank, res in enumerate(sorted_results, 1):
        print(f"  #{rank}: N={res['n_slots']}  CAGR={res['cagr']:+.1f}%  "
              f"MDD={res['max_dd']:.1f}%  Calmar={res['calmar']:.2f}  "
              f"交易={res['total_trades']}笔  胜率={res['win_rate']:.1f}%")

    print("\n  按CAGR排序:")
    sorted_by_cagr = sorted(results, key=lambda x: x['cagr'], reverse=True)
    for rank, res in enumerate(sorted_by_cagr, 1):
        print(f"  #{rank}: N={res['n_slots']}  CAGR={res['cagr']:+.1f}%  "
              f"MDD={res['max_dd']:.1f}%  净值={res['final_equity']:,.0f}")

    log_path = '/home/AIWealth/scripts/multislot_vshape_trades.log'
    best = sorted_results[0]
    with open(log_path, 'w') as f:
        f.write(f"V字策略多仓回测交易明细 - 最优N={best['n_slots']}\n")
        f.write(f"CAGR={best['cagr']:+.1f}% MDD={best['max_dd']:.1f}% Calmar={best['calmar']:.2f}\n")
        f.write("=" * 140 + "\n")
        hdr = f"{'No':>5} | {'BuyDate':<10} | {'Code':<10} | {'Name':<8} | {'BuyPx':>8} | {'Shares':>6} | {'SellDate':<10} | {'SellPx':>8} | {'Hr':>4} | {'Reason':<28} | {'PnL':>8} | {'Days':>3}\n"
        f.write(hdr)
        f.write("-" * 140 + "\n")
        for i, t in enumerate(best['trades'], 1):
            name = (t['code_name'] or '')[:6]
            f.write(f"{i:>5} | {t['buy_date']:<10} | {t['code']:<10} | {name:<8} | "
                    f"{t['buy_price']:>8.2f} | {t['shares']:>6d} | {t['sell_date']:<10} | "
                    f"{t['sell_price']:>8.2f} | h{t['sell_hour']:>3} | {t['sell_reason']:<28} | "
                    f"{t['pnl_pct']:>+7.1f}% | {t['hold_days']:>3}\n")
        f.write("=" * 140 + "\n")
        f.write(f"Total: {len(best['trades'])} trades\n")

    print(f"\n  每笔交易明细输出到: {log_path}")
    print(f"\n  总耗时: {time.time()-t0:.1f}s")
    print("=" * 70)


if __name__ == '__main__':
    main()
