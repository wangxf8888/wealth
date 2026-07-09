#!/usr/bin/env python3
"""Surge7策略 多仓模型回测
初始100万，分成N个仓位，每仓=当前总净值/N。多仓分散风险。
测试矩阵: N=[3, 5, 7, 10]
回测区间: 2021-01-01 ~ 2026-06-30
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

CONFIG = {
    'surge_threshold': 11.0,
    'fallback_ratio': 0.6,
    'tp_pct': 8.0,
    'sl_pct': -3.0,
    'max_hold_days': 3,
    'buy_hour': 1,
    'min_turn': 5.0,
    'market_filter': True,
}

N_SLOTS_LIST = [3, 5, 7, 10]
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
    cur.execute("SELECT DISTINCT date FROM stock_kline WHERE date>=? AND date<=? ORDER BY date",
                (lookback_start, BT_END))
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
    years_list = sorted(set(d[:4] for d in all_dates))
    for year in years_list:
        y_start = max(lookback_start, f"{year}-01-01")
        y_end = min(BT_END, f"{year}-12-31")
        if y_start > y_end: continue
        cur.execute(f"SELECT {fields} FROM stock_kline WHERE date>=? AND date<=?", (y_start, y_end))
        cnt = 0
        for row in cur:
            d, code = row[0], row[1]
            di = date_to_idx.get(d)
            if di is None: continue
            t = (row[2], row[3], row[4], row[5], row[6], row[7], row[8], row[9], row[10],
                 row[11], row[12], row[13], row[14], row[15], row[16], row[17], row[18],
                 row[19], row[20], row[21], row[22], row[23], row[24], row[25], row[26], row[27], row[28])
            if di not in day_data:
                day_data[di] = {}
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
    cur.execute("SELECT date, close_rate FROM index_kline WHERE code='sh.000001' AND date>=? AND date<=?",
                (lookback_start, BT_END))
    for row in cur:
        market_data[row[0]] = sf(row[1])
    conn.close()
    print(f"  数据加载完成: {total_rows:,}行 | 信号日: {len(signal_cands)}天")
    sys.stdout.flush()
    return all_dates, date_to_idx, bt_dates, day_data, signal_cands, market_data


class Portfolio:
    def __init__(self, initial_capital=1_000_000, n_slots=5):
        self.total_equity = initial_capital
        self.cash = initial_capital
        self.n_slots = n_slots
        self.positions = []

    @property
    def slot_size(self):
        return self.total_equity / self.n_slots

    def can_buy(self):
        return len(self.positions) < self.n_slots and self.cash >= self.slot_size * 0.95

    def buy(self, code, price, date, hour, code_name='', buy_idx=0):
        amount = self.slot_size
        shares = int(amount / price / 100) * 100
        if shares < 100:
            return None
        cost = shares * price
        if cost > self.cash:
            return None
        self.cash -= cost
        pos = {'code': code, 'price': price, 'shares': shares, 'cost': cost,
               'buy_date': date, 'buy_hour': hour, 'code_name': code_name, 'buy_idx': buy_idx}
        self.positions.append(pos)
        return pos

    def sell(self, pos, price):
        revenue = pos['shares'] * price
        self.cash += revenue
        profit_pct = (price / pos['price'] - 1) * 100
        # NOTE: don't remove from self.positions here;
        # the caller manages survived list and overwrites portfolio.positions
        return profit_pct

    def update_equity(self, market_prices=None):
        if market_prices:
            pos_value = sum(p['shares'] * market_prices.get(p['code'], p['price'])
                           for p in self.positions)
        else:
            pos_value = sum(p['shares'] * p['price'] for p in self.positions)
        self.total_equity = self.cash + pos_value


def run_backtest(n_slots, all_dates, date_to_idx, bt_dates, day_data, signal_cands, market_data):
    surge_th = CONFIG['surge_threshold']
    fallback_r = CONFIG['fallback_ratio']
    tp = CONFIG['tp_pct']
    sl = CONFIG['sl_pct']
    max_hold = CONFIG['max_hold_days']
    buy_hour = CONFIG['buy_hour']
    min_turn = CONFIG['min_turn']
    mkt_filter = CONFIG['market_filter']
    bh_off = (buy_hour - 1) * 4 + 1
    portfolio = Portfolio(INITIAL_CAPITAL, n_slots)
    trades = []
    equity_curve = []
    tp_active = tp < 900
    sl_active = sl > -900

    for today in bt_dates:
        today_idx = date_to_idx.get(today, -1)
        today_stocks = day_data.get(today_idx)
        if not today_stocks:
            continue
        # 1. update equity with open
        mkt_prices = {}
        for pos in portfolio.positions:
            t = today_stocks.get(pos['code'])
            if t:
                op = sf(t[23])
                if op > 0:
                    mkt_prices[pos['code']] = op
        portfolio.update_equity(mkt_prices)
        # 2. sell check
        survived = []
        for pos in portfolio.positions:
            if pos['buy_idx'] == today_idx:
                survived.append(pos)
                continue
            hold_days = today_idx - pos['buy_idx']
            t = today_stocks.get(pos['code'])
            if not t:
                survived.append(pos)
                continue
            preclose = sf(t[0])
            sold = False
            sell_price = 0.0
            sell_reason = ''
            sell_hour = 0
            for h in range(4):
                off = h * 4 + 1
                hc = sf(t[off + 3])
                if hc <= 0: continue
                ho = sf(t[off])
                hh = sf(t[off + 1])
                hl = sf(t[off + 2])
                if ho > 0 and hh > 0 and hl > 0 and (hh - hl) < 0.01:
                    if preclose > 0 and is_limit_down(pos['code'], hc, preclose):
                        continue
                pnl_pct = (hc / pos['price'] - 1.0) * 100.0
                if tp_active and pnl_pct >= tp:
                    sold = True; sell_price = hc; sell_reason = f'止盈({pnl_pct:+.2f}%)'; sell_hour = h + 1; break
                elif sl_active and pnl_pct <= sl:
                    sold = True; sell_price = hc; sell_reason = f'止损({pnl_pct:+.2f}%)'; sell_hour = h + 1; break
                elif h == 3 and hold_days >= max_hold:
                    sold = True; sell_price = hc; sell_reason = f'到期({hold_days}天)'; sell_hour = h + 1; break
            if sold:
                pnl = portfolio.sell(pos, sell_price)
                trades.append({
                    'code': pos['code'], 'code_name': pos['code_name'],
                    'buy_date': pos['buy_date'], 'buy_price': pos['price'], 'buy_hour': buy_hour,
                    'sell_date': today, 'sell_price': sell_price, 'sell_hour': sell_hour,
                    'pnl_pct': pnl, 'hold_days': hold_days, 'sell_reason': sell_reason
                })
            else:
                survived.append(pos)
        portfolio.positions = survived
        # refresh equity after sells (so slot_size is accurate for buying)
        portfolio.update_equity(mkt_prices)
        # 3. buy
        if portfolio.can_buy():
            skip_buy = False
            if mkt_filter:
                mkt_rate = market_data.get(today, 0.0)
                if mkt_rate < -1.0:
                    skip_buy = True
            if not skip_buy:
                sig_di = today_idx - 1
                cands = signal_cands.get(sig_di)
                if cands:
                    held_codes = {p['code'] for p in portfolio.positions}
                    for code, hr, cr, turn in cands:
                        if not portfolio.can_buy():
                            break
                        if code in held_codes: continue
                        if hr < surge_th: continue
                        if cr >= hr * fallback_r: continue
                        if min_turn > 0 and turn < min_turn: continue
                        buy_t = today_stocks.get(code)
                        if not buy_t: continue
                        if buy_t[25] == 1: continue
                        cn2 = buy_t[26] or ''
                        if 'ST' in cn2.upper(): continue
                        h_o = sf(buy_t[bh_off])
                        h_h = sf(buy_t[bh_off + 1])
                        h_l = sf(buy_t[bh_off + 2])
                        h_c = sf(buy_t[bh_off + 3])
                        if h_o <= 0: continue
                        if is_one_word_board(h_o, h_h, h_l, h_c): continue
                        preclose_b = sf(buy_t[0])
                        if preclose_b > 0 and is_limit_up(code, h_o, preclose_b): continue
                        pos = portfolio.buy(code, h_o, today, buy_hour, cn2, today_idx)
                        if pos:
                            held_codes.add(code)
        # 4. end-of-day equity
        mkt_prices_close = {}
        for pos in portfolio.positions:
            t = today_stocks.get(pos['code'])
            if t:
                cp = sf(t[16]) or sf(t[24])
                if cp > 0:
                    mkt_prices_close[pos['code']] = cp
        portfolio.update_equity(mkt_prices_close)
        equity_curve.append((today, portfolio.total_equity))

    # force close
    if portfolio.positions and bt_dates:
        last_idx = date_to_idx.get(bt_dates[-1], -1)
        last_stocks = day_data.get(last_idx, {})
        for pos in list(portfolio.positions):
            t2 = last_stocks.get(pos['code'])
            sp = (sf(t2[16]) or sf(t2[24]) or pos['price']) if t2 else pos['price']
            pnl = (sp / pos['price'] - 1.0) * 100.0
            portfolio.cash += pos['shares'] * sp
            trades.append({
                'code': pos['code'], 'code_name': pos['code_name'],
                'buy_date': pos['buy_date'], 'buy_price': pos['price'], 'buy_hour': CONFIG['buy_hour'],
                'sell_date': bt_dates[-1], 'sell_price': sp, 'sell_hour': 4,
                'pnl_pct': pnl, 'hold_days': last_idx - pos['buy_idx'], 'sell_reason': '强制清仓'
            })
        portfolio.positions = []
    final_equity = portfolio.cash
    return trades, equity_curve, final_equity


def compute_metrics(trades, equity_curve, final_equity):
    trade_count = len(trades)
    wins = sum(1 for t in trades if t['pnl_pct'] > 0)
    losses = trade_count - wins
    win_rate = wins / trade_count * 100 if trade_count > 0 else 0
    avg_pnl = sum(t['pnl_pct'] for t in trades) / trade_count if trade_count > 0 else 0
    peak = 0.0; max_dd = 0.0
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
    calmar = cagr / max_dd if max_dd > 0 else 0.0
    year_start_eq = {}; year_end_eq = {}
    for d, eq in equity_curve:
        y = d[:4]
        if y not in year_start_eq: year_start_eq[y] = eq
        year_end_eq[y] = eq
    annual_returns = {}
    sorted_years = sorted(year_start_eq.keys())
    for i, y in enumerate(sorted_years):
        start_eq = INITIAL_CAPITAL if i == 0 else year_end_eq[sorted_years[i - 1]]
        end_eq = year_end_eq[y]
        annual_returns[y] = (end_eq / start_eq - 1.0) * 100.0 if start_eq > 0 else 0.0
    return {
        'trade_count': trade_count, 'wins': wins, 'losses': losses,
        'win_rate': win_rate, 'avg_pnl': avg_pnl, 'cagr': cagr,
        'max_dd': max_dd, 'calmar': calmar, 'final_equity': final_equity,
        'years': years, 'annual_returns': annual_returns,
    }


def main():
    print("=" * 60)
    print("  Surge7策略多仓模型回测")
    print("=" * 60)
    print(f"初始资金: {INITIAL_CAPITAL:,.0f}")
    print(f"参数: surge={CONFIG['surge_threshold']}% fb={CONFIG['fallback_ratio']} "
          f"tp={CONFIG['tp_pct']}% sl={CONFIG['sl_pct']}%")
    print(f"      hold={CONFIG['max_hold_days']}d buy_h={CONFIG['buy_hour']} "
          f"turn>{CONFIG['min_turn']} mkt={CONFIG['market_filter']}")
    print(f"回测: {BT_START} ~ {BT_END}")
    print(f"仓位测试: N={N_SLOTS_LIST}")
    print("=" * 60)
    sys.stdout.flush()
    print("\n[1] 加载数据...")
    sys.stdout.flush()
    all_dates, date_to_idx, bt_dates, day_data, signal_cands, market_data = load_data()
    results = []
    for n_slots in N_SLOTS_LIST:
        print(f"\n[回测] N={n_slots}仓 ...")
        sys.stdout.flush()
        trades, equity_curve, final_equity = run_backtest(
            n_slots, all_dates, date_to_idx, bt_dates, day_data, signal_cands, market_data)
        metrics = compute_metrics(trades, equity_curve, final_equity)
        metrics['n_slots'] = n_slots
        results.append(metrics)
        ann = metrics['annual_returns']
        ann_str = ' '.join(f"{y}:{v:+.0f}%" for y, v in sorted(ann.items()))
        print(f"  最终净值: {final_equity:,.0f}")
        print(f"  CAGR: {metrics['cagr']:+.1f}%  胜率: {metrics['win_rate']:.1f}%  "
              f"MDD: -{metrics['max_dd']:.1f}%  Calmar: {metrics['calmar']:.2f}")
        print(f"  交易数: {metrics['trade_count']}  平均盈亏: {metrics['avg_pnl']:+.2f}%")
        print(f"  年度: {ann_str}")
        sys.stdout.flush()

    print("\n" + "=" * 60)
    print("===== Surge7策略多仓模型回测 =====")
    print(f"初始资金: {INITIAL_CAPITAL:,.0f}")
    print("=" * 60)
    for m in results:
        ann = m['annual_returns']
        ann_str = ' '.join(f"{y}:{v:+.0f}%" for y, v in sorted(ann.items()))
        print(f"\n--- N={m['n_slots']}仓 ---")
        print(f"最终净值: {m['final_equity']:,.0f}")
        print(f"CAGR: {m['cagr']:+.1f}%  胜率: {m['win_rate']:.1f}%  "
              f"MDD: -{m['max_dd']:.1f}%  Calmar: {m['calmar']:.2f}")
        print(f"交易数: {m['trade_count']}  平均盈亏: {m['avg_pnl']:+.2f}%")
        print(f"年度: {ann_str}")

    print("\n" + "=" * 60)
    print("===== 最优 =====")
    print("按Calmar排序:")
    sorted_results = sorted(results, key=lambda x: -x['calmar'])
    for rank, m in enumerate(sorted_results, 1):
        print(f"#{rank}: N={m['n_slots']}  CAGR={m['cagr']:+.1f}%  "
              f"MDD=-{m['max_dd']:.1f}%  Calmar={m['calmar']:.2f}")
    print("=" * 60)
    print(f"\n完成时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    sys.stdout.flush()


if __name__ == '__main__':
    main()
