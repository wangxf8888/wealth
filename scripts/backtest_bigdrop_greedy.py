#!/usr/bin/env python3
"""
大阴高开策略 - 5slot贪婪填充回测（对比 >=8% / >=10% / >=12% 跌幅阈值）

验证优化思路：策略A(大阴高开)当日多候选时，用所有候选填满5个slot，不浪费空仓位。

策略条件:
  板块: 创业板 sz.300xxx / sz.301xxx ; 市值 <50亿
  yesterday: (close-preclose)/preclose <= -DROP (跌幅>=DROP)
  today: (open-yesterday_close)/yesterday_close 在 [2%,8%] (高开2-8%)
  非ST, 非涨停开盘(open < round(preclose*1.20,2))
买入: today hour1_open ; 卖出: T+2日 hour4_close (无止盈无止损)
跌停延期: T+2收盘跌停(close==low 且 <=round(preclose*0.80,2)) 则 T+3卖
仓位: 5个slot; 贪婪填充(按|跌幅|降序, 有空slot就买, 最多5只/日); 复利 slot金额=total/5
"""
import os
import sqlite3
import json
from collections import defaultdict

DB_PATH = "/home/AIWealth/data/stocks.db"
START_DATE = "2021-01-01"
END_DATE = "2026-06-30"
INITIAL_CAPITAL = 1_000_000
NUM_SLOTS = 5
LOG_DIR = "/home/AIWealth/scripts/logs"
SUMMARY_PATH = os.path.join(LOG_DIR, "bigdrop_greedy_summary.log")
TRADES_JSON_PATH = "/home/AIWealth/frontend/bigdrop_greedy_trades.json"

GAPUP_MIN = 2.0
GAPUP_MAX = 8.0
MKT_CAP_MAX = 50e8

VARIANTS = [
    ("V1", 10.0, "跌>=10% + 5slot贪婪"),
    ("V2", 8.0,  "跌>=8% + 5slot贪婪"),
    ("V3", 12.0, "跌>=12% + 5slot贪婪"),
]


def get_limit_down_price(preclose):
    return round(preclose * 0.80, 2)


def is_limit_down_close(preclose, close_price, low_price):
    limit_price = get_limit_down_price(preclose)
    return close_price <= limit_price and abs(close_price - low_price) < 0.001


def calc_mkt_cap(amount, turn):
    if turn is None or turn <= 0 or amount is None or amount <= 0:
        return None
    return amount / (turn / 100.0)


class GreedyBacktest:
    def __init__(self, conn, trading_days, drop_threshold):
        self.conn = conn
        self.cur = conn.cursor()
        self.trading_days = trading_days
        self.drop_threshold = drop_threshold
        self.capital = INITIAL_CAPITAL
        self.slots = [None] * NUM_SLOTS
        self.all_trades = []
        self.daily_nav = []
        self.signals_count = 0
        self.slot_day_occupied = 0
        self.total_slot_days = 0

    def find_candidates(self, today, yesterday, day_idx):
        query = """
            SELECT
                t.date, t.code, t.code_name,
                t.open, t.high, t.low, t.close, t.preclose,
                t.amount, t.turn, t.isST,
                t.hour1_open,
                y.close as y_close, y.preclose as y_preclose
            FROM stock_kline t
            JOIN stock_kline y ON t.code = y.code AND y.date = ?
            WHERE t.date = ?
              AND (t.code LIKE 'sz.300%' OR t.code LIKE 'sz.301%')
              AND y.preclose > 0
              AND ((y.close - y.preclose) / y.preclose * 100) <= ?
              AND t.preclose > 0
              AND t.isST = 0
        """
        self.cur.execute(query, (yesterday, today, -self.drop_threshold))
        candidates = []
        for row in self.cur.fetchall():
            row = dict(row)
            name = row['code_name'] or ''
            if 'ST' in name.upper():
                continue
            y_close = row['y_close']
            if not y_close or y_close <= 0:
                continue
            gap_ratio = row['open'] / y_close
            gap_pct = (gap_ratio - 1) * 100
            if gap_pct < GAPUP_MIN or gap_pct > GAPUP_MAX:
                continue
            limit_up = round(row['preclose'] * 1.20, 2)
            if row['open'] >= limit_up:
                continue
            if not row['hour1_open'] or row['hour1_open'] <= 0:
                continue
            mkt_cap = calc_mkt_cap(row['amount'], row['turn'])
            if mkt_cap is None or mkt_cap >= MKT_CAP_MAX:
                continue
            y_drop_pct = (row['y_close'] - row['y_preclose']) / row['y_preclose'] * 100
            row['gap_pct'] = gap_pct
            row['y_drop_pct'] = y_drop_pct
            row['mkt_cap'] = mkt_cap
            row['buy_price'] = row['hour1_open']
            row['buy_reason'] = (f"昨跌{y_drop_pct:.1f}%,今高开+{gap_pct:.1f}%,"
                                 f"市值{mkt_cap/1e8:.0f}亿")
            candidates.append(row)
        candidates.sort(key=lambda x: x['y_drop_pct'])
        return candidates

    def get_sell_data(self, code, sell_date):
        self.cur.execute("""
            SELECT date, close, preclose, low, high, open, hour4_close
            FROM stock_kline WHERE code = ? AND date = ?
        """, (code, sell_date))
        r = self.cur.fetchone()
        return dict(r) if r else None

    def try_sell(self, slot_idx, current_day_idx):
        pos = self.slots[slot_idx]
        if pos is None:
            return None
        hold_days = current_day_idx - pos['buy_day_idx']
        if hold_days < 2:
            return None
        sell_date = self.trading_days[current_day_idx]
        sell_data = self.get_sell_data(pos['code'], sell_date)
        if sell_data is None:
            return None
        close_price = sell_data['close']
        if not close_price or close_price <= 0:
            return None
        if sell_data['preclose'] and sell_data['preclose'] > 0:
            low = sell_data['low'] if sell_data['low'] else close_price
            if is_limit_down_close(sell_data['preclose'], close_price, low):
                return None
        if sell_data['hour4_close'] and sell_data['hour4_close'] > 0:
            sell_price = sell_data['hour4_close']
        else:
            sell_price = close_price
        ret_pct = (sell_price - pos['buy_price']) / pos['buy_price'] * 100
        profit = pos['shares'] * (sell_price - pos['buy_price'])
        trade = {
            'date': pos['buy_date'], 'code': pos['code'], 'name': pos['name'],
            'buy_reason': pos['buy_reason'], 'buy_price': round(pos['buy_price'], 2),
            'sell_date': sell_date, 'sell_price': round(sell_price, 2),
            'return_pct': round(ret_pct, 2), 'holding_days': hold_days,
            'slot': slot_idx + 1, 'profit': round(profit, 2),
        }
        self.capital += pos['amount_invested'] + profit
        self.slots[slot_idx] = None
        return trade

    def _calc_total_assets(self, current_date):
        total = self.capital
        for slot in self.slots:
            if slot is None:
                continue
            self.cur.execute(
                "SELECT close FROM stock_kline WHERE code = ? AND date = ?",
                (slot['code'], current_date))
            r = self.cur.fetchone()
            if r and r['close'] and r['close'] > 0:
                total += slot['shares'] * r['close']
            else:
                total += slot['amount_invested']
        return total

    def run(self):
        for day_idx, today in enumerate(self.trading_days):
            if day_idx < 6:
                continue
            yesterday = self.trading_days[day_idx - 1]
            for i in range(NUM_SLOTS):
                trade = self.try_sell(i, day_idx)
                if trade:
                    self.all_trades.append(trade)
            empty_slots = [i for i in range(NUM_SLOTS) if self.slots[i] is None]
            candidates = self.find_candidates(today, yesterday, day_idx)
            held_codes = set(s['code'] for s in self.slots if s is not None)
            valid_cands = [c for c in candidates if c['code'] not in held_codes]
            self.signals_count += len(valid_cands)
            if valid_cands and empty_slots:
                total_assets = self._calc_total_assets(today)
                slot_amount = total_assets / NUM_SLOTS
                for cand in valid_cands:
                    if not empty_slots:
                        break
                    if cand['code'] in held_codes:
                        continue
                    buy_price = cand['buy_price']
                    if buy_price <= 0:
                        continue
                    shares = int(slot_amount / buy_price / 100) * 100
                    if shares <= 0:
                        continue
                    actual_amount = shares * buy_price
                    if actual_amount > self.capital:
                        shares = int(self.capital / buy_price / 100) * 100
                        if shares <= 0:
                            continue
                        actual_amount = shares * buy_price
                    self.capital -= actual_amount
                    slot_idx = empty_slots.pop(0)
                    held_codes.add(cand['code'])
                    self.slots[slot_idx] = {
                        'code': cand['code'], 'name': cand['code_name'] or '',
                        'buy_price': buy_price, 'buy_date': today,
                        'buy_day_idx': day_idx, 'shares': shares,
                        'amount_invested': actual_amount,
                        'buy_reason': cand['buy_reason'],
                    }
            occupied = sum(1 for s in self.slots if s is not None)
            self.slot_day_occupied += occupied
            self.total_slot_days += NUM_SLOTS
            total_assets = self._calc_total_assets(today)
            self.daily_nav.append({'date': today, 'total_assets': total_assets})
        return self._compute_stats()

    def _compute_stats(self):
        if not self.daily_nav:
            return None
        final_assets = self.daily_nav[-1]['total_assets']
        total_return = (final_assets - INITIAL_CAPITAL) / INITIAL_CAPITAL * 100
        days = len(self.daily_nav)
        years = days / 250.0
        cagr = (final_assets / INITIAL_CAPITAL) ** (1.0 / years) - 1 if years > 0 and final_assets > 0 else 0
        peak = INITIAL_CAPITAL
        max_dd = 0
        for nav in self.daily_nav:
            if nav['total_assets'] > peak:
                peak = nav['total_assets']
            dd = (peak - nav['total_assets']) / peak * 100
            if dd > max_dd:
                max_dd = dd
        total_trades = len(self.all_trades)
        wins = sum(1 for t in self.all_trades if t['return_pct'] > 0)
        win_rate = wins / total_trades * 100 if total_trades > 0 else 0
        avg_ret = sum(t['return_pct'] for t in self.all_trades) / total_trades if total_trades > 0 else 0
        capture_rate = total_trades / self.signals_count * 100 if self.signals_count > 0 else 0
        slot_util = self.slot_day_occupied / self.total_slot_days * 100 if self.total_slot_days > 0 else 0
        yearly = defaultdict(lambda: {'start': None, 'end': None, 'trades': 0})
        for nav in self.daily_nav:
            y = nav['date'][:4]
            if yearly[y]['start'] is None:
                yearly[y]['start'] = nav['total_assets']
            yearly[y]['end'] = nav['total_assets']
        for t in self.all_trades:
            yearly[t['date'][:4]]['trades'] += 1
        yearly_out = []
        for year in sorted(yearly.keys()):
            ys = yearly[year]
            if ys['start'] and ys['end'] and ys['start'] > 0:
                yr_ret = (ys['end'] - ys['start']) / ys['start'] * 100
                yearly_out.append((year, yr_ret, ys['trades']))
        return {
            'final_assets': final_assets, 'total_return': total_return,
            'cagr': cagr * 100, 'max_dd': max_dd, 'total_trades': total_trades,
            'win_rate': win_rate, 'avg_ret': avg_ret, 'signals': self.signals_count,
            'captured': total_trades, 'capture_rate': capture_rate,
            'slot_util': slot_util, 'yearly': yearly_out,
        }


def format_variant(name, desc, s):
    lines = []
    lines.append(f"=== {name}: {desc} ===")
    lines.append(f"最终资产: {s['final_assets']:,.0f} | CAGR: {s['cagr']:.1f}% | MaxDD: {s['max_dd']:.1f}%")
    lines.append(f"总交易: {s['total_trades']}笔 | 胜率: {s['win_rate']:.1f}% | 均收益: {s['avg_ret']:+.2f}%")
    lines.append(f"信号总数: {s['signals']} | 实际捕获: {s['captured']} (捕获率{s['capture_rate']:.0f}%)")
    lines.append(f"slot利用率: {s['slot_util']:.0f}% (slot-天占用/总slot-天)")
    lines.append("逐年:")
    for year, yr_ret, trades in s['yearly']:
        lines.append(f"  {year}: {yr_ret:+.1f}%, {trades}笔")
    return "\n".join(lines)


def main():
    os.makedirs(LOG_DIR, exist_ok=True)
    os.makedirs(os.path.dirname(TRADES_JSON_PATH), exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()
    cur.execute("""
        SELECT DISTINCT date FROM stock_kline
        WHERE date >= ? AND date <= ? ORDER BY date
    """, (START_DATE, END_DATE))
    trading_days = [r[0] for r in cur.fetchall()]
    print(f"交易日数量: {len(trading_days)}")

    results = {}
    all_trades_out = {}
    for name, drop, desc in VARIANTS:
        print(f"\n运行 {name}: {desc} ...")
        bt = GreedyBacktest(conn, trading_days, drop)
        stats = bt.run()
        results[name] = (desc, stats)
        all_trades_out[name] = bt.all_trades
        print(f"  {name} 完成: 最终资产{stats['final_assets']:,.0f} "
              f"CAGR{stats['cagr']:.1f}% 交易{stats['total_trades']}笔")

    best_name = max(results.keys(), key=lambda k: results[k][1]['cagr'])
    best_desc, best_stats = results[best_name]

    out = []
    out.append("大阴高开(创业板<50亿) - 贪婪填充回测对比")
    out.append("=" * 70)
    out.append(f"回测区间: {START_DATE} ~ {END_DATE} | 初始资金: {INITIAL_CAPITAL:,.0f} | slot数: {NUM_SLOTS}")
    out.append(f"高开范围: [{GAPUP_MIN:.0f}%, {GAPUP_MAX:.0f}%] | 卖出: T+2 hour4_close(跌停延T+3)")
    out.append("")
    for name, drop, desc in VARIANTS:
        d, s = results[name]
        out.append(format_variant(name, d, s))
        out.append("")
    out.append("=== 对比结论 ===")
    out.append(f"最优方案: {best_name} ({best_desc})")
    reason = (f"CAGR最高({best_stats['cagr']:.1f}%), MaxDD {best_stats['max_dd']:.1f}%, "
              f"胜率{best_stats['win_rate']:.1f}%, 均收益{best_stats['avg_ret']:+.2f}%/笔, "
              f"捕获率{best_stats['capture_rate']:.0f}%")
    out.append(f"原因: {reason}")
    out.append("")
    out.append("--- 汇总对比表 ---")
    out.append(f"{'变体':<5}| {'CAGR':>8} | {'MaxDD':>7} | {'交易':>6} | {'胜率':>6} | {'均收益':>7} | {'信号':>6} | {'捕获率':>6}")
    out.append("-" * 70)
    for name, drop, desc in VARIANTS:
        d, s = results[name]
        out.append(f"{name:<5}| {s['cagr']:>+7.1f}% | {s['max_dd']:>6.1f}% | {s['total_trades']:>6} | "
                   f"{s['win_rate']:>5.1f}% | {s['avg_ret']:>+6.2f}% | {s['signals']:>6} | {s['capture_rate']:>5.0f}%")
    out.append("=" * 70)

    summary_text = "\n".join(out)
    with open(SUMMARY_PATH, 'w', encoding='utf-8') as f:
        f.write(summary_text)
    print("\n" + summary_text)
    print(f"\n汇总已保存: {SUMMARY_PATH}")

    trades_clean = {}
    for name in all_trades_out:
        trades_clean[name] = [{
            'date': t['date'], 'code': t['code'], 'name': t['name'],
            'buy_reason': t['buy_reason'], 'buy_price': t['buy_price'],
            'sell_date': t['sell_date'], 'sell_price': t['sell_price'],
            'return_pct': t['return_pct'], 'holding_days': t['holding_days'],
            'slot': t['slot'], 'profit': t['profit'],
        } for t in all_trades_out[name]]
    with open(TRADES_JSON_PATH, 'w', encoding='utf-8') as f:
        json.dump(trades_clean, f, ensure_ascii=False, indent=2)
    print(f"交易明细已保存: {TRADES_JSON_PATH}")
    conn.close()


if __name__ == "__main__":
    main()
