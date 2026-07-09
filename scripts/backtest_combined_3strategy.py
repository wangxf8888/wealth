#!/usr/bin/env python3
"""
三策略联合回测引擎
策略A: 大阴高开(创业板<50亿) - 优先级2
策略B: 科创板共振(J+L+P) - 优先级1(最高)
策略C: 跌停反弹(创业板<50亿) - 优先级3

15仓位复利模式，区间2021-01-01~2026-06-30
"""
import sys
import os
import sqlite3
import json
from datetime import datetime
from collections import defaultdict

# ============ 配置区 ============
DB_PATH = "/home/AIWealth/data/stocks.db"
START_DATE = "2021-01-01"
END_DATE = "2026-06-30"
INITIAL_CAPITAL = 1_000_000
NUM_SLOTS = 15
B_RESERVED_SLOTS = 3  # 策略B专属slot 0-2
LOG_DIR = "/home/AIWealth/scripts/logs"
FRONTEND_DIR = "/home/AIWealth/frontend"
# ================================


def get_limit_up_price(code, preclose):
    if code.startswith("sz.300") or code.startswith("sz.301"):
        return round(preclose * 1.20, 2)
    elif code.startswith("sh.688") or code.startswith("sh.689"):
        return round(preclose * 1.20, 2)
    else:
        return round(preclose * 1.10, 2)


def get_limit_down_price(code, preclose):
    if code.startswith("sz.300") or code.startswith("sz.301"):
        return round(preclose * 0.80, 2)
    elif code.startswith("sh.688") or code.startswith("sh.689"):
        return round(preclose * 0.80, 2)
    else:
        return round(preclose * 0.90, 2)


def is_limit_down_close(code, preclose, close_price, low_price):
    limit_price = get_limit_down_price(code, preclose)
    return close_price <= limit_price and abs(close_price - low_price) < 0.001


def calc_mkt_cap(amount, turn):
    if turn is None or turn <= 0 or amount is None or amount <= 0:
        return None
    return amount / (turn / 100.0)


class Combined3StrategyBacktest:
    def __init__(self):
        self.conn = sqlite3.connect(DB_PATH)
        self.conn.row_factory = sqlite3.Row
        self.cur = self.conn.cursor()

        self.capital = INITIAL_CAPITAL
        self.slots = [None] * NUM_SLOTS  # slot 0-2: B专属, 3-14: 通用
        self.all_trades = []
        self.daily_equity = []
        self.trading_days = []
        self.index_data = {}  # date -> {open, close, preclose}
        self.trade_id = 0

    def load_trading_days(self):
        self.cur.execute("""
            SELECT DISTINCT date FROM stock_kline
            WHERE date >= ? AND date <= ?
            ORDER BY date
        """, (START_DATE, END_DATE))
        self.trading_days = [r[0] for r in self.cur.fetchall()]
        print(f"交易日数量: {len(self.trading_days)}")

    def load_index_data(self):
        """加载上证指数数据用于大盘判断"""
        self.cur.execute("""
            SELECT date, open, close, preclose
            FROM index_kline
            WHERE code = 'sh.000001' AND date >= '2020-06-01' AND date <= ?
            ORDER BY date
        """, (END_DATE,))
        for r in self.cur.fetchall():
            self.index_data[r['date']] = {
                'open': r['open'],
                'close': r['close'],
                'preclose': r['preclose']
            }
        print(f"指数数据: {len(self.index_data)}天")

    def get_day_idx(self, date):
        try:
            return self.trading_days.index(date)
        except ValueError:
            return -1

    def _calc_total_assets(self, current_day_idx):
        total = self.capital
        current_date = self.trading_days[current_day_idx]
        for slot in self.slots:
            if slot is None:
                continue
            # 用买入金额+浮动盈亏估算
            self.cur.execute("""
                SELECT close FROM stock_kline WHERE code = ? AND date = ?
            """, (slot['code'], current_date))
            r = self.cur.fetchone()
            if r and r['close'] and r['close'] > 0:
                total += slot['shares'] * r['close']
            else:
                total += slot['amount_invested']
        return total

    # ===== 策略B: 科创板共振(J+L+P) =====
    def find_candidates_B(self, today, yesterday, day_idx):
        """科创板共振: 5日均换手<1%, 20日高位, 大盘高开>0.3%, 个股高开>=2%"""
        if day_idx < 21:
            return []

        # 大盘高开判断: today open > yesterday close * 1.003
        idx_today = self.index_data.get(today)
        idx_yesterday = self.index_data.get(yesterday)
        if not idx_today or not idx_yesterday:
            return []
        if idx_today['open'] <= idx_yesterday['close'] * 1.003:
            return []

        market_gap = (idx_today['open'] / idx_yesterday['close'] - 1) * 100

        # 获取过去20个交易日列表
        days_20 = self.trading_days[day_idx - 20:day_idx]
        days_5 = self.trading_days[day_idx - 5:day_idx]

        # 查询科创板当日数据
        self.cur.execute("""
            SELECT t.date, t.code, t.code_name,
                   t.open, t.high, t.low, t.close, t.preclose,
                   t.volume, t.amount, t.turn, t.isST,
                   t.hour1_open,
                   y.close as y_close, y.preclose as y_preclose, y.turn as y_turn
            FROM stock_kline t
            JOIN stock_kline y ON t.code = y.code AND y.date = ?
            WHERE t.date = ?
              AND t.code LIKE 'sh.688%'
              AND t.preclose > 0
              AND t.isST = 0
              AND t.hour1_open > 0
        """, (yesterday, today))

        candidates = []
        for row in self.cur.fetchall():
            row = dict(row)
            name = row['code_name'] or ''
            if 'ST' in name.upper():
                continue

            y_close = row['y_close']
            if not y_close or y_close <= 0:
                continue

            # 市值 < 50亿
            mkt_cap = calc_mkt_cap(row['amount'], row['turn'])
            if mkt_cap is None or mkt_cap >= 50e8:
                continue

            # 高开 >= 2%
            gap_ratio = row['open'] / y_close
            if gap_ratio < 1.02:
                continue
            gap_pct = (gap_ratio - 1) * 100

            # 非涨停开盘(科创板20%)
            limit_up = round(row['preclose'] * 1.20, 2)
            if row['open'] >= limit_up:
                continue

            # J: 过去5日平均换手率 < 1.0%
            placeholders_5 = ','.join(['?'] * len(days_5))
            self.cur.execute(f"""
                SELECT AVG(turn) FROM stock_kline
                WHERE code = ? AND date IN ({placeholders_5}) AND turn > 0
            """, [row['code']] + days_5)
            r = self.cur.fetchone()
            avg_turn_5 = r[0] if r and r[0] else None
            if avg_turn_5 is None or avg_turn_5 >= 1.0:
                continue

            # L: 20日高位 - yesterday close >= 20日最高close * 0.98
            placeholders_20 = ','.join(['?'] * len(days_20))
            self.cur.execute(f"""
                SELECT MAX(close) FROM stock_kline
                WHERE code = ? AND date IN ({placeholders_20}) AND close > 0
            """, [row['code']] + days_20)
            r = self.cur.fetchone()
            max_close_20 = r[0] if r and r[0] else None
            if max_close_20 is None or y_close < max_close_20 * 0.98:
                continue

            row['gap_pct'] = gap_pct
            row['mkt_cap'] = mkt_cap
            row['avg_turn_5'] = avg_turn_5
            row['buy_price'] = row['hour1_open']
            row['strategy'] = 'B'
            row['priority'] = 1
            row['hold_days_target'] = 1  # T+1卖
            row['buy_reason'] = (f"J+L+P: 5日均换手{avg_turn_5:.2f}%<1%, "
                                 f"近20日高位, 大盘高开+{market_gap:.1f}%, "
                                 f"跳空+{gap_pct:.1f}%")
            candidates.append(row)

        # 按gap_pct从大到小排序
        candidates.sort(key=lambda x: -x['gap_pct'])
        return candidates

    # ===== 策略A: 大阴高开(创业板<50亿) =====
    def find_candidates_A(self, today, yesterday, day_idx):
        """大阴高开: 昨跌>=5%, 今高开2-8%, 创业板<50亿"""
        if day_idx < 5:
            return []

        self.cur.execute("""
            SELECT t.date, t.code, t.code_name,
                   t.open, t.high, t.low, t.close, t.preclose,
                   t.volume, t.amount, t.turn, t.isST,
                   t.hour1_open,
                   y.close as y_close, y.preclose as y_preclose,
                   y.close_rate as y_close_rate
            FROM stock_kline t
            JOIN stock_kline y ON t.code = y.code AND y.date = ?
            WHERE t.date = ?
              AND (t.code LIKE 'sz.300%' OR t.code LIKE 'sz.301%')
              AND y.preclose > 0
              AND t.preclose > 0
              AND t.isST = 0
              AND t.hour1_open > 0
        """, (yesterday, today))

        candidates = []
        for row in self.cur.fetchall():
            row = dict(row)
            name = row['code_name'] or ''
            if 'ST' in name.upper():
                continue

            y_close = row['y_close']
            y_preclose = row['y_preclose']
            if not y_close or y_close <= 0 or not y_preclose or y_preclose <= 0:
                continue

            # 昨日跌幅 <= -5%
            y_drop = (y_close - y_preclose) / y_preclose
            if y_drop > -0.05:
                continue

            # 市值 < 50亿
            mkt_cap = calc_mkt_cap(row['amount'], row['turn'])
            if mkt_cap is None or mkt_cap >= 50e8:
                continue

            # 高开 2%~8%
            gap_ratio = row['open'] / y_close
            if gap_ratio < 1.02 or gap_ratio > 1.08:
                continue
            gap_pct = (gap_ratio - 1) * 100

            # 非涨停开盘(创业板20%)
            limit_up = round(row['preclose'] * 1.20, 2)
            if row['open'] >= limit_up:
                continue

            y_drop_pct = y_drop * 100
            row['gap_pct'] = gap_pct
            row['y_drop_pct'] = y_drop_pct
            row['mkt_cap'] = mkt_cap
            row['buy_price'] = row['hour1_open']
            row['strategy'] = 'A'
            row['priority'] = 2
            row['hold_days_target'] = 2  # T+2卖
            row['buy_reason'] = (f"昨跌{y_drop_pct:.1f}%, "
                                 f"今高开+{gap_pct:.1f}%, "
                                 f"市值{mkt_cap/1e8:.0f}亿")
            candidates.append(row)

        # 按|跌幅|从大到小排序
        candidates.sort(key=lambda x: x['y_drop_pct'])
        return candidates

    # ===== 策略C: 跌停反弹(创业板<50亿) =====
    def find_candidates_C(self, today, yesterday, day_idx):
        """跌停反弹: 昨跌>=12%, 今高开>=2%, 创业板<50亿"""
        if day_idx < 5:
            return []

        self.cur.execute("""
            SELECT t.date, t.code, t.code_name,
                   t.open, t.high, t.low, t.close, t.preclose,
                   t.volume, t.amount, t.turn, t.isST,
                   t.hour1_open,
                   y.close as y_close, y.preclose as y_preclose,
                   y.close_rate as y_close_rate
            FROM stock_kline t
            JOIN stock_kline y ON t.code = y.code AND y.date = ?
            WHERE t.date = ?
              AND (t.code LIKE 'sz.300%' OR t.code LIKE 'sz.301%')
              AND y.preclose > 0
              AND t.preclose > 0
              AND t.isST = 0
              AND t.hour1_open > 0
        """, (yesterday, today))

        candidates = []
        for row in self.cur.fetchall():
            row = dict(row)
            name = row['code_name'] or ''
            if 'ST' in name.upper():
                continue

            y_close = row['y_close']
            y_preclose = row['y_preclose']
            if not y_close or y_close <= 0 or not y_preclose or y_preclose <= 0:
                continue

            # 昨日跌幅 <= -12%
            y_drop = (y_close - y_preclose) / y_preclose
            if y_drop > -0.12:
                continue

            # 市值 < 50亿
            mkt_cap = calc_mkt_cap(row['amount'], row['turn'])
            if mkt_cap is None or mkt_cap >= 50e8:
                continue

            # 高开 >= 2%
            gap_ratio = row['open'] / y_close
            if gap_ratio < 1.02:
                continue
            gap_pct = (gap_ratio - 1) * 100

            # 非涨停开盘
            limit_up = round(row['preclose'] * 1.20, 2)
            if row['open'] >= limit_up:
                continue

            y_drop_pct = y_drop * 100
            row['gap_pct'] = gap_pct
            row['y_drop_pct'] = y_drop_pct
            row['mkt_cap'] = mkt_cap
            row['buy_price'] = row['hour1_open']
            row['strategy'] = 'C'
            row['priority'] = 3
            row['hold_days_target'] = 2  # T+2卖
            row['buy_reason'] = (f"昨跌{y_drop_pct:.1f}%(近跌停), "
                                 f"今高开+{gap_pct:.1f}%, "
                                 f"市值{mkt_cap/1e8:.0f}亿")
            candidates.append(row)

        # 按|跌幅|从大到小排序
        candidates.sort(key=lambda x: x['y_drop_pct'])
        return candidates

    def try_sell(self, slot_idx, current_day_idx):
        """尝试卖出持仓"""
        pos = self.slots[slot_idx]
        if pos is None:
            return None

        buy_day_idx = pos['buy_day_idx']
        hold_days = current_day_idx - buy_day_idx
        target = pos['hold_days_target']

        # 未到目标持有天数
        if hold_days < target:
            return None

        sell_date = self.trading_days[current_day_idx]
        self.cur.execute("""
            SELECT date, close, preclose, low, high, open, hour4_close
            FROM stock_kline WHERE code = ? AND date = ?
        """, (pos['code'], sell_date))
        r = self.cur.fetchone()
        if r is None:
            return None
        sell_data = dict(r)

        # 使用hour4_close作为卖出价
        sell_price = sell_data['hour4_close']
        if not sell_price or sell_price <= 0:
            sell_price = sell_data['close']
        if not sell_price or sell_price <= 0:
            return None

        # 跌停检查: close==low 且 <= 跌停价 → 不可卖出，延期
        if sell_data['preclose'] and sell_data['preclose'] > 0:
            low = sell_data['low'] if sell_data['low'] else sell_price
            limit_down = get_limit_down_price(pos['code'], sell_data['preclose'])
            if sell_price <= limit_down and abs(sell_price - low) < 0.001:
                return None  # 跌停，延期

        # 计算收益
        ret_pct = (sell_price - pos['buy_price']) / pos['buy_price'] * 100
        profit = pos['shares'] * (sell_price - pos['buy_price'])

        self.trade_id += 1
        trade = {
            'id': self.trade_id,
            'strategy': pos['strategy_name'],
            'strategy_code': pos['strategy_code'],
            'date': pos['buy_date'],
            'code': pos['code'],
            'name': pos['name'],
            'buy_reason': pos['buy_reason'],
            'buy_price': round(pos['buy_price'], 2),
            'sell_date': sell_date,
            'sell_price': round(sell_price, 2),
            'sell_reason': f"T+{hold_days}收盘卖出",
            'return_pct': round(ret_pct, 2),
            'holding_days': hold_days,
            'slot': slot_idx + 1,
            'profit': round(profit, 2),
            'amount_invested': pos['amount_invested']
        }

        # 资金回笼
        self.capital += pos['amount_invested'] + profit
        self.slots[slot_idx] = None
        return trade

    def buy_into_slot(self, slot_idx, cand, day_idx, today):
        """买入到指定slot"""
        total_assets = self._calc_total_assets(day_idx)
        slot_amount = total_assets / NUM_SLOTS
        buy_price = cand['buy_price']

        if buy_price <= 0:
            return False

        shares = int(slot_amount / buy_price / 100) * 100
        if shares <= 0:
            return False

        actual_amount = shares * buy_price

        strategy_names = {'A': '大阴高开', 'B': '科创板共振', 'C': '跌停反弹'}

        self.slots[slot_idx] = {
            'code': cand['code'],
            'name': cand['code_name'] or '',
            'buy_price': buy_price,
            'buy_date': today,
            'buy_day_idx': day_idx,
            'shares': shares,
            'amount_invested': actual_amount,
            'buy_reason': cand['buy_reason'],
            'strategy_code': cand['strategy'],
            'strategy_name': strategy_names[cand['strategy']],
            'hold_days_target': cand['hold_days_target']
        }
        self.capital -= actual_amount
        return True

    def run(self):
        print("=" * 70)
        print("三策略联合回测")
        print(f"区间: {START_DATE} ~ {END_DATE}")
        print(f"初始资金: {INITIAL_CAPITAL:,.0f}")
        print(f"仓位数: {NUM_SLOTS} (策略B专属{B_RESERVED_SLOTS}个)")
        print("策略A: 大阴高开(创业板<50亿) 昨跌>=5% 高开2-8% T+2")
        print("策略B: 科创板共振(J+L+P) 5日低换手+20日高位+大盘高开 T+1")
        print("策略C: 跌停反弹(创业板<50亿) 昨跌>=12% 高开>=2% T+2")
        print("=" * 70)

        self.load_trading_days()
        self.load_index_data()

        prev_total = INITIAL_CAPITAL
        peak = INITIAL_CAPITAL
        max_dd = 0

        for day_idx, today in enumerate(self.trading_days):
            if day_idx < 21:
                continue

            yesterday = self.trading_days[day_idx - 1]

            # === Step 1: 卖出 ===
            day_trades = []
            for i in range(NUM_SLOTS):
                trade = self.try_sell(i, day_idx)
                if trade:
                    day_trades.append(trade)
                    self.all_trades.append(trade)

            # === Step 2: 找候选 ===
            cands_B = self.find_candidates_B(today, yesterday, day_idx)
            cands_A = self.find_candidates_A(today, yesterday, day_idx)
            cands_C = self.find_candidates_C(today, yesterday, day_idx)

            # === Step 3: 分配仓位 ===
            held_codes = set(s['code'] for s in self.slots if s is not None)

            # 3a: 策略B填入专属slot (0-2)
            b_slots_empty = [i for i in range(B_RESERVED_SLOTS) if self.slots[i] is None]
            for cand in cands_B:
                if not b_slots_empty:
                    break
                if cand['code'] in held_codes:
                    continue
                slot_idx = b_slots_empty.pop(0)
                if self.buy_into_slot(slot_idx, cand, day_idx, today):
                    held_codes.add(cand['code'])

            # 3b: 通用slot (3-14): 按优先级 B > A > C
            general_slots_empty = [i for i in range(B_RESERVED_SLOTS, NUM_SLOTS) if self.slots[i] is None]

            # 剩余的B候选也可以用通用slot
            remaining_B = [c for c in cands_B if c['code'] not in held_codes]
            all_general_cands = remaining_B + cands_A + cands_C
            # 按priority排序(1最高)，同priority按gap_pct倒序
            all_general_cands.sort(key=lambda x: (x['priority'], -abs(x.get('y_drop_pct', 0) or x.get('gap_pct', 0))))

            for cand in all_general_cands:
                if not general_slots_empty:
                    break
                if cand['code'] in held_codes:
                    continue
                slot_idx = general_slots_empty.pop(0)
                if self.buy_into_slot(slot_idx, cand, day_idx, today):
                    held_codes.add(cand['code'])

            # === Step 4: 计算日权益 ===
            total = self._calc_total_assets(day_idx)
            if total > peak:
                peak = total
            dd = (peak - total) / peak * 100
            if dd > max_dd:
                max_dd = dd

            self.daily_equity.append({
                'date': today,
                'equity': round(total, 2),
                'drawdown': round(dd, 2)
            })

            # 进度输出
            if day_idx % 100 == 0:
                ret = (total / INITIAL_CAPITAL - 1) * 100
                print(f"  {today} | 资产:{total:,.0f} | 累计:{ret:+.1f}% | 回撤:{dd:.1f}% | 交易:{len(self.all_trades)}笔")

        # 最终统计
        final_total = self._calc_total_assets(len(self.trading_days) - 1)
        self.generate_output(final_total, max_dd)

    def generate_output(self, final_total, max_dd):
        """生成所有输出文件"""
        os.makedirs(LOG_DIR, exist_ok=True)
        os.makedirs(FRONTEND_DIR, exist_ok=True)

        # 计算CAGR
        days = len(self.trading_days)
        years = days / 244.0  # 约244个交易日/年
        cagr = (pow(final_total / INITIAL_CAPITAL, 1 / years) - 1) * 100

        # 分策略统计
        stats = {'A': [], 'B': [], 'C': []}
        for t in self.all_trades:
            stats[t['strategy_code']].append(t)

        strategy_names = {'A': '大阴高开', 'B': '科创板共振', 'C': '跌停反弹'}

        # === 1. 汇总日志 ===
        summary_lines = []
        summary_lines.append("三策略联合回测")
        summary_lines.append("=" * 70)
        summary_lines.append(f"区间: {START_DATE} ~ {END_DATE}")
        summary_lines.append(f"初始: {INITIAL_CAPITAL:,.0f}")
        summary_lines.append(f"最终: {final_total:,.0f}")
        summary_lines.append(f"CAGR: {cagr:.1f}%")
        summary_lines.append(f"最大回撤: {max_dd:.1f}%")
        summary_lines.append(f"总交易: {len(self.all_trades)}笔")
        summary_lines.append("")
        summary_lines.append("--- 分策略统计 ---")
        summary_lines.append(f"{'策略':<12}{'笔数':<8}{'胜率':<8}{'均收益':<10}{'贡献占比'}")

        total_profit = sum(t['profit'] for t in self.all_trades)
        for code in ['A', 'B', 'C']:
            trades = stats[code]
            if not trades:
                summary_lines.append(f"{strategy_names[code]:<10}  0       -       -         -")
                continue
            wins = sum(1 for t in trades if t['return_pct'] > 0)
            win_rate = wins / len(trades) * 100
            avg_ret = sum(t['return_pct'] for t in trades) / len(trades)
            strat_profit = sum(t['profit'] for t in trades)
            contrib = strat_profit / total_profit * 100 if total_profit != 0 else 0
            summary_lines.append(f"{strategy_names[code]:<10}  {len(trades):<6} {win_rate:.1f}%  {avg_ret:+.2f}%   {contrib:.0f}%")

        # 逐年表现
        summary_lines.append("")
        summary_lines.append("--- 逐年表现 ---")
        summary_lines.append(f"{'年':<6}{'总收益':<10}{'笔数':<6}{'胜率':<8}{'A笔/收益':<14}{'B笔/收益':<14}{'C笔/收益'}")

        yearly = defaultdict(list)
        for t in self.all_trades:
            year = t['date'][:4]
            yearly[year].append(t)

        # 计算逐年收益需要用权益曲线
        year_equity = {}
        for eq in self.daily_equity:
            y = eq['date'][:4]
            if y not in year_equity:
                year_equity[y] = {'start': eq['equity'], 'end': eq['equity']}
            year_equity[y]['end'] = eq['equity']

        # 获取每年初始值
        prev_year_end = INITIAL_CAPITAL
        for year in sorted(yearly.keys()):
            trades_y = yearly[year]
            total_trades = len(trades_y)
            wins = sum(1 for t in trades_y if t['return_pct'] > 0)
            win_rate = wins / total_trades * 100 if total_trades > 0 else 0

            # 用年末权益计算年收益
            if year in year_equity:
                year_ret = (year_equity[year]['end'] / prev_year_end - 1) * 100
                prev_year_end = year_equity[year]['end']
            else:
                year_ret = 0

            a_trades = [t for t in trades_y if t['strategy_code'] == 'A']
            b_trades = [t for t in trades_y if t['strategy_code'] == 'B']
            c_trades = [t for t in trades_y if t['strategy_code'] == 'C']

            a_str = f"{len(a_trades)}/{sum(t['return_pct'] for t in a_trades)/len(a_trades):+.1f}%" if a_trades else "0/-"
            b_str = f"{len(b_trades)}/{sum(t['return_pct'] for t in b_trades)/len(b_trades):+.1f}%" if b_trades else "0/-"
            c_str = f"{len(c_trades)}/{sum(t['return_pct'] for t in c_trades)/len(c_trades):+.1f}%" if c_trades else "0/-"

            summary_lines.append(f"{year:<6}{year_ret:+.1f}%    {total_trades:<6}{win_rate:.1f}%  {a_str:<14}{b_str:<14}{c_str}")

        # 逐月热力图
        summary_lines.append("")
        summary_lines.append("--- 逐月表现 ---")
        monthly_equity = {}
        for eq in self.daily_equity:
            m = eq['date'][:7]
            if m not in monthly_equity:
                monthly_equity[m] = {'start': eq['equity'], 'end': eq['equity']}
            monthly_equity[m]['end'] = eq['equity']

        prev_end = INITIAL_CAPITAL
        months_sorted = sorted(monthly_equity.keys())
        current_year = ""
        month_line = ""
        for m in months_sorted:
            y = m[:4]
            if y != current_year:
                if month_line:
                    summary_lines.append(month_line)
                current_year = y
                month_line = f"{y}: "
            m_ret = (monthly_equity[m]['end'] / prev_end - 1) * 100
            prev_end = monthly_equity[m]['end']
            month_line += f"{m[5:]}{m_ret:+.1f}% "
        if month_line:
            summary_lines.append(month_line)

        summary_text = "\n".join(summary_lines)
        summary_path = os.path.join(LOG_DIR, "combined_3strategy_summary.log")
        with open(summary_path, 'w') as f:
            f.write(summary_text)
        print("\n" + summary_text)
        print(f"\n[汇总已保存] {summary_path}")

        # === 2. 交易明细JSON ===
        trades_json = []
        for t in self.all_trades:
            trades_json.append({
                'id': t['id'],
                'strategy': t['strategy'],
                'date': t['date'],
                'code': t['code'],
                'name': t['name'],
                'buy_reason': t['buy_reason'],
                'buy_price': t['buy_price'],
                'sell_date': t['sell_date'],
                'sell_price': t['sell_price'],
                'sell_reason': t['sell_reason'],
                'return_pct': t['return_pct'],
                'holding_days': t['holding_days'],
                'slot': t['slot']
            })
        trades_path = os.path.join(FRONTEND_DIR, "combined_trades.json")
        with open(trades_path, 'w') as f:
            json.dump(trades_json, f, ensure_ascii=False, indent=2)
        print(f"[交易明细] {trades_path} ({len(trades_json)}笔)")

        # === 3. 权益曲线JSON ===
        equity_data = {
            'dates': [eq['date'] for eq in self.daily_equity],
            'equity': [eq['equity'] for eq in self.daily_equity],
            'drawdown': [eq['drawdown'] for eq in self.daily_equity]
        }
        equity_path = os.path.join(FRONTEND_DIR, "combined_equity.json")
        with open(equity_path, 'w') as f:
            json.dump(equity_data, f, ensure_ascii=False)
        print(f"[权益曲线] {equity_path}")


if __name__ == "__main__":
    bt = Combined3StrategyBacktest()
    bt.run()
    bt.conn.close()
