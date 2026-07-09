#!/usr/bin/env python3
"""
大阴高开策略 - 单独完整回测（T+2退出）
条件：创业板<50亿，昨跌≥7%放量，今高开2-5%，大盘昨跌，非ST，非涨停开盘
买入：T日 hour1_open
卖出：T+2日 close（跌停延至T+3）
5仓位复利模式
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
NUM_SLOTS = 5
LOG_DIR = "/home/AIWealth/scripts/logs"

# 策略参数
DROP_THRESHOLD = -7.0          # 昨日跌幅阈值(%)
GAPUP_MIN = 2.0               # 今日高开下限(%)
GAPUP_MAX = 5.0               # 今日高开上限(%)
VOL_MULT = 1.5                # 放量倍数(vs 5日均量)
MKT_CAP_MAX = 50e8            # 流通市值上限50亿(元)
# ================================


def get_limit_up_price(code, preclose):
    """计算涨停价（创业板20%）"""
    if code.startswith("sz.300") or code.startswith("sz.301"):
        return round(preclose * 1.20, 2)
    elif code.startswith("sh.688") or code.startswith("sh.689"):
        return round(preclose * 1.20, 2)
    else:
        return round(preclose * 1.10, 2)


def get_limit_down_price(code, preclose):
    """计算跌停价（创业板20%）"""
    if code.startswith("sz.300") or code.startswith("sz.301"):
        return round(preclose * 0.80, 2)
    elif code.startswith("sh.688") or code.startswith("sh.689"):
        return round(preclose * 0.80, 2)
    else:
        return round(preclose * 0.90, 2)


def is_limit_down_close(code, preclose, close_price, low_price):
    """判断是否跌停收盘（close==跌停价 且 close==low）"""
    limit_price = get_limit_down_price(code, preclose)
    return close_price <= limit_price and abs(close_price - low_price) < 0.001


def calc_mkt_cap(amount, turn):
    """流通市值 = 成交额 / (换手率/100)"""
    if turn is None or turn <= 0 or amount is None or amount <= 0:
        return None
    return amount / (turn / 100.0)


class BigDropGapUpBacktest:
    def __init__(self):
        self.conn = sqlite3.connect(DB_PATH)
        self.conn.row_factory = sqlite3.Row
        self.cur = self.conn.cursor()

        self.capital = INITIAL_CAPITAL
        self.slots = [None] * NUM_SLOTS
        self.all_trades = []
        self.daily_nav = []
        self.position_log = []
        self.trading_days = []
        self.index_data = {}
        self.signals_count = 0  # 总信号数（符合条件的候选数）
        self.missed_signals = 0  # 因仓位满错过的信号数

    def load_trading_days(self):
        """加载交易日列表"""
        self.cur.execute("""
            SELECT DISTINCT date FROM stock_kline
            WHERE date >= ? AND date <= ?
            ORDER BY date
        """, (START_DATE, END_DATE))
        self.trading_days = [r[0] for r in self.cur.fetchall()]
        print(f"交易日数量: {len(self.trading_days)}")

    def load_index_data(self):
        """加载上证指数日K数据"""
        self.cur.execute("""
            SELECT date, close, preclose, close_rate
            FROM index_kline
            WHERE code = 'sh.000001' AND date >= '2020-01-01' AND date <= ?
            ORDER BY date
        """, (END_DATE,))
        for r in self.cur.fetchall():
            self.index_data[r['date']] = {
                'close': r['close'],
                'preclose': r['preclose'],
                'close_rate': r['close_rate']
            }

    def get_day_idx(self, date):
        try:
            return self.trading_days.index(date)
        except ValueError:
            return -1

    def _get_avg_volume_5d(self, code, day_idx):
        """获取day_idx之前5天的平均成交量"""
        if day_idx < 5:
            return None
        days = self.trading_days[day_idx - 5:day_idx]
        placeholders = ','.join(['?'] * len(days))
        self.cur.execute(f"""
            SELECT AVG(volume) FROM stock_kline
            WHERE code = ? AND date IN ({placeholders}) AND volume > 0
        """, [code] + days)
        r = self.cur.fetchone()
        return r[0] if r else None

    def find_candidates(self, today, yesterday, day_idx):
        """找大阴高开候选股"""
        if day_idx < 5:
            return []

        # 检查大盘前一日是否收跌（上证指数yesterday close < preclose）
        idx_data = self.index_data.get(yesterday)
        if not idx_data:
            return []
        if idx_data['close'] >= idx_data['preclose']:
            return []

        # 查询：昨日大阴+今日高开的创业板股票
        query = """
            SELECT
                t.date, t.code, t.code_name,
                t.open, t.high, t.low, t.close, t.preclose,
                t.volume, t.amount, t.turn, t.isST,
                t.hour1_open,
                y.close as y_close, y.preclose as y_preclose,
                y.close_rate as y_close_rate, y.volume as y_volume,
                y.amount as y_amount, y.turn as y_turn
            FROM stock_kline t
            JOIN stock_kline y ON t.code = y.code AND y.date = ?
            WHERE t.date = ?
              AND (t.code LIKE 'sz.300%' OR t.code LIKE 'sz.301%')
              AND y.preclose > 0
              AND ((y.close - y.preclose) / y.preclose * 100) <= ?
              AND t.preclose > 0
              AND t.isST = 0
        """
        self.cur.execute(query, (yesterday, today, DROP_THRESHOLD))
        candidates = []

        for row in self.cur.fetchall():
            row = dict(row)
            # 名称排除ST
            name = row['code_name'] or ''
            if 'ST' in name.upper():
                continue

            # 高开幅度检查：today_open / yesterday_close
            y_close = row['y_close']
            if not y_close or y_close <= 0:
                continue
            gap_ratio = row['open'] / y_close
            if gap_ratio < 1.02 or gap_ratio > 1.05:
                continue
            gap_pct = (gap_ratio - 1) * 100

            # 非涨停开盘（创业板20%）
            limit_up = round(y_close * 1.20, 2)
            if row['open'] >= limit_up:
                continue

            # hour1_open有效
            if not row['hour1_open'] or row['hour1_open'] <= 0:
                continue

            # 市值 < 50亿
            mkt_cap = calc_mkt_cap(row['amount'], row['turn'])
            if mkt_cap is None or mkt_cap >= MKT_CAP_MAX:
                continue

            # 昨日放量：yesterday_volume > 5日均量 × 1.5
            vol_5d = self._get_avg_volume_5d(row['code'], day_idx - 1)
            if vol_5d is None or vol_5d <= 0:
                continue
            if row['y_volume'] <= vol_5d * VOL_MULT:
                continue

            # 昨日跌幅(用于排序)
            y_drop_pct = (row['y_close'] - row['y_preclose']) / row['y_preclose'] * 100

            row['gap_pct'] = gap_pct
            row['y_drop_pct'] = y_drop_pct
            row['mkt_cap'] = mkt_cap
            row['buy_price'] = row['hour1_open']
            row['buy_reason'] = (f"昨跌{y_drop_pct:.1f}%放量,"
                                 f"今高开+{gap_pct:.1f}%,"
                                 f"市值{mkt_cap/1e8:.0f}亿,大盘昨跌")
            candidates.append(row)

        # 按跌幅绝对值从大到小排序（跌越多修复弹性越大）
        candidates.sort(key=lambda x: x['y_drop_pct'])  # 越负排越前
        return candidates

    def get_sell_data(self, code, sell_date):
        """获取卖出日数据"""
        self.cur.execute("""
            SELECT date, close, preclose, low, high, open, hour4_close
            FROM stock_kline WHERE code = ? AND date = ?
        """, (code, sell_date))
        r = self.cur.fetchone()
        if r:
            return dict(r)
        return None

    def try_sell(self, slot_idx, current_day_idx):
        """尝试在T+2卖出"""
        pos = self.slots[slot_idx]
        if pos is None:
            return None

        buy_day_idx = pos['buy_day_idx']
        hold_days = current_day_idx - buy_day_idx

        # T+2才能卖(买入日=D0, D1持有, D2卖出)
        if hold_days < 2:
            return None

        sell_date = self.trading_days[current_day_idx]
        sell_data = self.get_sell_data(pos['code'], sell_date)
        if sell_data is None:
            # 无数据，跳过这一天（延后一天再试）
            return None

        sell_price = sell_data['close']
        if not sell_price or sell_price <= 0:
            return None

        # 检查跌停：close <= 跌停价 且 close == low
        if sell_data['preclose'] and sell_data['preclose'] > 0:
            low = sell_data['low'] if sell_data['low'] else sell_price
            if is_limit_down_close(pos['code'], sell_data['preclose'], sell_price, low):
                # 跌停不可卖出，延迟到下一个交易日
                return None

        # 优先用hour4_close，否则用close
        if sell_data['hour4_close'] and sell_data['hour4_close'] > 0:
            sell_price = sell_data['hour4_close']
        else:
            sell_price = sell_data['close']

        # 计算收益
        ret_pct = (sell_price - pos['buy_price']) / pos['buy_price'] * 100
        profit = pos['shares'] * (sell_price - pos['buy_price'])

        trade = {
            'date': pos['buy_date'],
            'code': pos['code'],
            'name': pos['name'],
            'buy_reason': pos['buy_reason'],
            'buy_price': pos['buy_price'],
            'sell_date': sell_date,
            'sell_price': round(sell_price, 2),
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

    def _calc_total_assets(self, current_day_idx):
        """计算当前总资产"""
        total = self.capital
        current_date = self.trading_days[current_day_idx]
        for slot in self.slots:
            if slot is None:
                continue
            self.cur.execute("""
                SELECT close FROM stock_kline
                WHERE code = ? AND date = ?
            """, (slot['code'], current_date))
            r = self.cur.fetchone()
            if r and r['close'] and r['close'] > 0:
                total += slot['shares'] * r['close']
            else:
                total += slot['amount_invested']
        return total

    def _log_positions(self, today, total_assets, day_return, cum_return, day_trades):
        """记录仓位日志（用户指定格式）"""
        log_entry = (f"{today} | 总资产:{total_assets:,.0f} | "
                     f"日收益:{day_return:+.1f}% | 累计:{cum_return:+.1f}%\n")

        for i in range(NUM_SLOTS):
            slot = self.slots[i]
            sold = [t for t in day_trades if t['slot'] == i + 1]

            if sold:
                t = sold[0]
                log_entry += (f"  slot{i+1}: [卖出] {t['name']}({t['code']}) "
                              f"{t['date']}买@{t['buy_price']:.2f}→"
                              f"{t['sell_date']}卖@{t['sell_price']:.2f} "
                              f"{t['return_pct']:+.1f}%\n")
            elif slot is None:
                log_entry += f"  slot{i+1}: [空仓]\n"
            else:
                today_idx = self.get_day_idx(today)
                hold_days = today_idx - slot['buy_day_idx']
                # 获取当前价格
                self.cur.execute("SELECT close FROM stock_kline WHERE code=? AND date=?",
                                 (slot['code'], today))
                r = self.cur.fetchone()
                cur_price = r['close'] if r and r['close'] else slot['buy_price']
                cur_ret = (cur_price - slot['buy_price']) / slot['buy_price'] * 100

                if hold_days == 0:
                    status = "新入D0"
                    note = f" 原因:{slot['buy_reason']}"
                elif hold_days == 1:
                    status = "持有D1"
                    note = " 明日T+2卖出"
                else:
                    status = f"持有D{hold_days}"
                    note = " 今日应卖"

                log_entry += (f"  slot{i+1}: [{status}] {slot['name']}({slot['code']}) "
                              f"{slot['buy_date']}买@{slot['buy_price']:.2f} "
                              f"当前{cur_price:.2f} {cur_ret:+.1f}%{note}\n")

        self.position_log.append(log_entry)

    def run(self):
        """运行回测"""
        print("=" * 70)
        print("大阴高开策略 - 单独完整回测（T+2退出）")
        print(f"区间: {START_DATE} ~ {END_DATE}")
        print(f"初始资金: {INITIAL_CAPITAL:,.0f}")
        print(f"仓位数: {NUM_SLOTS}")
        print(f"条件: 创业板<50亿 | 昨跌≥7%放量 | 今高开2-5% | 大盘昨跌 | 非ST")
        print(f"买入: T日 hour1_open | 卖出: T+2 close(跌停延期)")
        print("=" * 70)

        self.load_trading_days()
        self.load_index_data()

        for day_idx, today in enumerate(self.trading_days):
            if day_idx < 6:
                continue

            yesterday = self.trading_days[day_idx - 1]

            # === Step 1: 执行卖出（收盘价卖出，逻辑上发生在买入之后，但先计算）===
            # 实际上：买入发生在开盘(hour1_open)，卖出发生在收盘(hour4_close/close)
            # 所以同一天可以"先买后卖"不冲突，但T+1限制决定了当天买的不能当天卖
            # 这里先检查可卖仓位
            day_trades = []
            for i in range(NUM_SLOTS):
                trade = self.try_sell(i, day_idx)
                if trade:
                    day_trades.append(trade)
                    self.all_trades.append(trade)

            # === Step 2: 寻找新信号并买入 ===
            empty_slots = [i for i in range(NUM_SLOTS) if self.slots[i] is None]

            candidates = self.find_candidates(today, yesterday, day_idx)
            self.signals_count += len(candidates)

            if candidates and empty_slots:
                # 去重：不买已持有的
                held_codes = set(s['code'] for s in self.slots if s is not None)

                for cand in candidates:
                    if not empty_slots:
                        break
                    if cand['code'] in held_codes:
                        continue

                    # 计算买入金额：当前总资产/5
                    total_assets = self._calc_total_assets(day_idx)
                    slot_amount = total_assets / NUM_SLOTS
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
                        'code': cand['code'],
                        'name': cand['code_name'] or '',
                        'buy_price': buy_price,
                        'buy_date': today,
                        'buy_day_idx': day_idx,
                        'shares': shares,
                        'amount_invested': actual_amount,
                        'buy_reason': cand['buy_reason']
                    }

            # 统计错过的信号
            if candidates and not empty_slots:
                # 有些信号因仓满未能买入
                held_codes = set(s['code'] for s in self.slots if s is not None)
                missed = sum(1 for c in candidates if c['code'] not in held_codes)
                # 但已经买了一些，只算剩余错过的
                bought_today = sum(1 for s in self.slots if s and s['buy_date'] == today)
                real_missed = len(candidates) - bought_today
                if real_missed > 0:
                    self.missed_signals += real_missed

            # === Step 3: 记录每日状态 ===
            total_assets = self._calc_total_assets(day_idx)
            prev_assets = self.daily_nav[-1]['total_assets'] if self.daily_nav else INITIAL_CAPITAL
            day_return = (total_assets - prev_assets) / prev_assets * 100 if prev_assets > 0 else 0
            cum_return = (total_assets - INITIAL_CAPITAL) / INITIAL_CAPITAL * 100

            self.daily_nav.append({
                'date': today,
                'total_assets': total_assets,
                'cash': self.capital,
                'day_return': day_return,
                'cum_return': cum_return
            })

            self._log_positions(today, total_assets, day_return, cum_return, day_trades)

            # 进度显示
            if day_idx % 200 == 0:
                print(f"  进度: {today} | 总资产: {total_assets:,.0f} | "
                      f"累计交易: {len(self.all_trades)}笔 | 信号: {self.signals_count}")

        # 最终统计
        self._print_summary()
        self._save_outputs()

    def _print_summary(self):
        """打印汇总统计"""
        if not self.daily_nav:
            print("无交易数据")
            return

        final_assets = self.daily_nav[-1]['total_assets']
        total_return = (final_assets - INITIAL_CAPITAL) / INITIAL_CAPITAL * 100

        # 年化CAGR
        days = len(self.daily_nav)
        years = days / 250.0
        if years > 0 and final_assets > 0:
            cagr = (final_assets / INITIAL_CAPITAL) ** (1.0 / years) - 1
        else:
            cagr = 0

        # 最大回撤
        peak = INITIAL_CAPITAL
        max_dd = 0
        max_dd_date = ""
        for nav in self.daily_nav:
            if nav['total_assets'] > peak:
                peak = nav['total_assets']
            dd = (peak - nav['total_assets']) / peak * 100
            if dd > max_dd:
                max_dd = dd
                max_dd_date = nav['date']

        # 胜率
        total_trades = len(self.all_trades)
        wins = sum(1 for t in self.all_trades if t['return_pct'] > 0)
        win_rate = wins / total_trades * 100 if total_trades > 0 else 0
        avg_ret = sum(t['return_pct'] for t in self.all_trades) / total_trades if total_trades > 0 else 0
        avg_win = 0
        avg_loss = 0
        if wins > 0:
            avg_win = sum(t['return_pct'] for t in self.all_trades if t['return_pct'] > 0) / wins
        losses = total_trades - wins
        if losses > 0:
            avg_loss = sum(t['return_pct'] for t in self.all_trades if t['return_pct'] <= 0) / losses

        # 逐年统计
        yearly = defaultdict(lambda: {'start_assets': None, 'end_assets': None,
                                       'trades': 0, 'wins': 0, 'total_ret': 0.0})
        for nav in self.daily_nav:
            year = nav['date'][:4]
            if yearly[year]['start_assets'] is None:
                yearly[year]['start_assets'] = nav['total_assets']
            yearly[year]['end_assets'] = nav['total_assets']
        for t in self.all_trades:
            year = t['date'][:4]
            yearly[year]['trades'] += 1
            yearly[year]['total_ret'] += t['return_pct']
            if t['return_pct'] > 0:
                yearly[year]['wins'] += 1

        # 输出
        summary = []
        summary.append("=" * 70)
        summary.append("大阴高开策略 - 单独回测结果（T+2退出）")
        summary.append("=" * 70)
        summary.append(f"回测区间: {START_DATE} ~ {END_DATE}")
        summary.append(f"初始资金: {INITIAL_CAPITAL:,.0f}")
        summary.append(f"最终资产: {final_assets:,.0f}")
        summary.append(f"总收益率: {total_return:+.2f}%")
        summary.append(f"年化收益(CAGR): {cagr*100:+.2f}%")
        summary.append(f"最大回撤: {max_dd:.2f}% (发生于{max_dd_date})")
        summary.append(f"")
        summary.append(f"--- 交易统计 ---")
        summary.append(f"总信号数: {self.signals_count}")
        summary.append(f"实际交易笔数: {total_trades}")
        summary.append(f"因仓满错过: ~{self.missed_signals}笔")
        summary.append(f"胜率: {win_rate:.1f}% ({wins}/{total_trades})")
        summary.append(f"平均收益/笔: {avg_ret:+.2f}%")
        summary.append(f"平均盈利/笔: {avg_win:+.2f}%")
        summary.append(f"平均亏损/笔: {avg_loss:+.2f}%")
        summary.append(f"盈亏比: {abs(avg_win/avg_loss):.2f}" if avg_loss != 0 else "盈亏比: N/A")
        summary.append(f"")
        summary.append(f"--- 逐年表现 ---")
        summary.append(f"{'年份':<6}| {'年收益':>8} | {'交易笔数':>6} | {'胜率':>6} | {'均收益/笔':>8}")
        summary.append(f"{'-'*6}+{'-'*10}+{'-'*8}+{'-'*8}+{'-'*10}")
        for year in sorted(yearly.keys()):
            ys = yearly[year]
            if ys['start_assets'] and ys['end_assets']:
                yr_ret = (ys['end_assets'] - ys['start_assets']) / ys['start_assets'] * 100
                wr = ys['wins'] / ys['trades'] * 100 if ys['trades'] > 0 else 0
                avg_r = ys['total_ret'] / ys['trades'] if ys['trades'] > 0 else 0
                summary.append(f"{year:<6}| {yr_ret:>+7.1f}% | {ys['trades']:>6} | {wr:>5.1f}% | {avg_r:>+7.2f}%")
        summary.append("=" * 70)

        # 理论 vs 实际对比
        summary.append(f"")
        summary.append(f"--- 理论 vs 实际对比 ---")
        trades_per_year = total_trades / years if years > 0 else 0
        summary.append(f"年均交易笔数: {trades_per_year:.0f}")
        summary.append(f"实际年化: {cagr*100:+.2f}%")
        theory_annual = trades_per_year * avg_ret * (1.0/NUM_SLOTS)
        summary.append(f"简单估算年化(笔数×均收益×仓位占比): {theory_annual:+.1f}%")
        summary.append("=" * 70)

        self.summary_text = "\n".join(summary)
        print(self.summary_text)

    def _save_outputs(self):
        """保存输出文件"""
        os.makedirs(LOG_DIR, exist_ok=True)

        # 1. Summary
        summary_path = os.path.join(LOG_DIR, "bigdrop_gapup_solo_summary.log")
        with open(summary_path, 'w', encoding='utf-8') as f:
            f.write(self.summary_text)
        print(f"\n汇总已保存: {summary_path}")

        # 2. Trades JSON
        trades_path = os.path.join(LOG_DIR, "bigdrop_gapup_solo_trades.json")
        clean_trades = []
        for t in self.all_trades:
            clean_trades.append({
                'date': t['date'],
                'code': t['code'],
                'name': t['name'],
                'buy_reason': t['buy_reason'],
                'buy_price': t['buy_price'],
                'sell_date': t['sell_date'],
                'sell_price': t['sell_price'],
                'return_pct': t['return_pct'],
                'holding_days': t['holding_days'],
                'slot': t['slot']
            })
        with open(trades_path, 'w', encoding='utf-8') as f:
            json.dump(clean_trades, f, ensure_ascii=False, indent=2)
        print(f"交易明细已保存: {trades_path} ({len(clean_trades)}笔)")

        # 3. Positions log
        pos_path = os.path.join(LOG_DIR, "bigdrop_gapup_solo_positions.log")
        with open(pos_path, 'w', encoding='utf-8') as f:
            for entry in self.position_log:
                f.write(entry + "\n")
        print(f"仓位日志已保存: {pos_path}")


if __name__ == "__main__":
    bt = BigDropGapUpBacktest()
    bt.run()
