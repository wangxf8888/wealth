#!/usr/bin/env python3
"""
大阴高开(创业板<50亿精选) - 完整多仓位回测
条件：创业板<50亿，昨跌≥5%，今高开2~8%，非ST，非涨停开盘
买入：T日 hour1_open
卖出：T+2日 hour4_close（跌停延至T+3）
10仓位复利模式
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
NUM_SLOTS = 10
LOG_DIR = "/home/AIWealth/scripts/logs"
FRONTEND_DIR = "/home/AIWealth/frontend"

# 策略参数
DROP_THRESHOLD = -5.0          # 昨日跌幅阈值(%)
GAPUP_MIN = 2.0               # 今日高开下限(%)
GAPUP_MAX = 8.0               # 今日高开上限(%)
MKT_CAP_MAX = 50e8            # 流通市值上限50亿(元)
# ================================


def get_limit_up_price(code, preclose):
    """计算涨停价（创业板/科创板20%，主板10%）"""
    if code.startswith("sz.300") or code.startswith("sz.301"):
        return round(preclose * 1.20, 2)
    elif code.startswith("sh.688") or code.startswith("sh.689"):
        return round(preclose * 1.20, 2)
    else:
        return round(preclose * 1.10, 2)


def get_limit_down_price(code, preclose):
    """计算跌停价（创业板/科创板20%，主板10%）"""
    if code.startswith("sz.300") or code.startswith("sz.301"):
        return round(preclose * 0.80, 2)
    elif code.startswith("sh.688") or code.startswith("sh.689"):
        return round(preclose * 0.80, 2)
    else:
        return round(preclose * 0.90, 2)


def is_limit_down_close(code, preclose, close_price, low_price):
    """判断是否跌停收盘（close<=跌停价 且 close==low）"""
    limit_price = get_limit_down_price(code, preclose)
    return close_price <= limit_price and abs(close_price - low_price) < 0.001


def calc_mkt_cap(amount, turn):
    """流通市值 = 成交额 / (换手率/100)"""
    if turn is None or turn <= 0 or amount is None or amount <= 0:
        return None
    return amount / (turn / 100.0)


class BigDropGemBacktest:
    def __init__(self):
        self.conn = sqlite3.connect(DB_PATH)
        self.conn.row_factory = sqlite3.Row
        self.cur = self.conn.cursor()

        self.capital = INITIAL_CAPITAL
        self.slots = [None] * NUM_SLOTS
        self.all_trades = []
        self.daily_nav = []
        self.trading_days = []
        self.signals_count = 0
        self.missed_signals = 0

    def load_trading_days(self):
        """加载交易日列表"""
        self.cur.execute("""
            SELECT DISTINCT date FROM stock_kline
            WHERE date >= ? AND date <= ?
            ORDER BY date
        """, (START_DATE, END_DATE))
        self.trading_days = [r[0] for r in self.cur.fetchall()]
        print(f"交易日数量: {len(self.trading_days)}")

    def get_day_idx(self, date):
        try:
            return self.trading_days.index(date)
        except ValueError:
            return -1

    def find_candidates(self, today, yesterday, day_idx):
        """找大阴高开候选股（创业板<50亿）"""
        if day_idx < 2:
            return []

        # 查询：昨日大阴+今日高开的创业板股票
        query = """
            SELECT
                t.date, t.code, t.code_name,
                t.open, t.high, t.low, t.close, t.preclose,
                t.volume, t.amount, t.turn, t.isST,
                t.hour1_open,
                y.close as y_close, y.preclose as y_preclose,
                y.volume as y_volume, y.amount as y_amount, y.turn as y_turn
            FROM stock_kline t
            JOIN stock_kline y ON t.code = y.code AND y.date = ?
            WHERE t.date = ?
              AND (t.code LIKE 'sz.300%' OR t.code LIKE 'sz.301%')
              AND y.preclose > 0
              AND y.close > 0
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
            gap_pct = (row['open'] - y_close) / y_close * 100
            if gap_pct < GAPUP_MIN or gap_pct > GAPUP_MAX:
                continue

            # 非涨停开盘（创业板20%）
            limit_up = round(row['preclose'] * 1.20, 2)
            if row['open'] >= limit_up:
                continue

            # hour1_open有效
            if not row['hour1_open'] or row['hour1_open'] <= 0:
                continue

            # 市值 < 50亿 (用今日amount和turn计算)
            mkt_cap = calc_mkt_cap(row['amount'], row['turn'])
            if mkt_cap is None or mkt_cap >= MKT_CAP_MAX:
                # 也尝试用昨日的数据
                mkt_cap = calc_mkt_cap(row['y_amount'], row['y_turn'])
                if mkt_cap is None or mkt_cap >= MKT_CAP_MAX:
                    continue

            # 昨日跌幅(用于排序)
            y_drop_pct = (row['y_close'] - row['y_preclose']) / row['y_preclose'] * 100

            row['gap_pct'] = gap_pct
            row['y_drop_pct'] = y_drop_pct
            row['mkt_cap'] = mkt_cap
            row['buy_price'] = row['hour1_open']
            row['buy_reason'] = (f"昨跌{y_drop_pct:.1f}%, "
                                 f"今高开+{gap_pct:.1f}%, "
                                 f"市值{mkt_cap/1e8:.0f}亿")
            candidates.append(row)

        # 按跌幅绝对值从大到小排序（跌越多排越前）
        candidates.sort(key=lambda x: x['y_drop_pct'])
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
            return None

        # 优先用hour4_close
        sell_price = None
        if sell_data['hour4_close'] and sell_data['hour4_close'] > 0:
            sell_price = sell_data['hour4_close']
        elif sell_data['close'] and sell_data['close'] > 0:
            sell_price = sell_data['close']
        else:
            return None

        # 检查跌停：close <= 跌停价 且 close == low
        if sell_data['preclose'] and sell_data['preclose'] > 0:
            close_price = sell_data['close'] if sell_data['close'] else sell_price
            low = sell_data['low'] if sell_data['low'] else close_price
            if is_limit_down_close(pos['code'], sell_data['preclose'], close_price, low):
                # 跌停不可卖出，延迟到下一个交易日
                return None

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
            'sell_reason': 'T+2收盘正常卖出' if hold_days == 2 else f'T+{hold_days}跌停延期卖出',
            'return_pct': round(ret_pct, 2),
            'holding_days': hold_days,
            'slot': slot_idx + 1,
            'profit': round(profit, 2),
            'amount_invested': pos['amount_invested'],
            'capital_before': round(pos['amount_invested'], 2),
            'capital_after': round(pos['amount_invested'] + profit, 2)
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

    def run(self):
        """运行回测"""
        print("=" * 70)
        print("大阴高开(创业板<50亿精选) - 完整多仓位回测")
        print(f"区间: {START_DATE} ~ {END_DATE}")
        print(f"初始资金: {INITIAL_CAPITAL:,.0f}")
        print(f"仓位数: {NUM_SLOTS}")
        print(f"条件: 创业板<50亿 | 昨跌≥5% | 今高开2~8% | 非ST | 非涨停开盘")
        print(f"买入: T日 hour1_open | 卖出: T+2 hour4_close(跌停延期)")
        print("=" * 70)

        self.load_trading_days()

        for day_idx, today in enumerate(self.trading_days):
            if day_idx < 2:
                continue

            yesterday = self.trading_days[day_idx - 1]

            # === Step 1: 执行卖出 ===
            day_trades_sell = []
            for i in range(NUM_SLOTS):
                trade = self.try_sell(i, day_idx)
                if trade:
                    day_trades_sell.append(trade)
                    self.all_trades.append(trade)

            # === Step 2: 寻找新信号并买入 ===
            empty_slots = [i for i in range(NUM_SLOTS) if self.slots[i] is None]
            candidates = self.find_candidates(today, yesterday, day_idx)
            self.signals_count += len(candidates)

            bought_today = 0
            if candidates and empty_slots:
                held_codes = set(s['code'] for s in self.slots if s is not None)

                for cand in candidates:
                    if not empty_slots:
                        break
                    if cand['code'] in held_codes:
                        continue

                    # 计算买入金额：当前总资产/NUM_SLOTS
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
                    bought_today += 1

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

            # 统计错过信号
            if len(candidates) > bought_today:
                self.missed_signals += len(candidates) - bought_today

            # === Step 3: 记录每日状态 ===
            total_assets = self._calc_total_assets(day_idx)
            self.daily_nav.append({
                'date': today,
                'total_assets': total_assets
            })

            # 进度显示
            if day_idx % 200 == 0:
                cum_ret = (total_assets - INITIAL_CAPITAL) / INITIAL_CAPITAL * 100
                print(f"  进度: {today} | 总资产: {total_assets:,.0f} | "
                      f"累计收益: {cum_ret:+.1f}% | 交易: {len(self.all_trades)}笔")

        # 最终统计和输出
        self._generate_all_outputs()

    def _generate_all_outputs(self):
        """生成所有输出文件"""
        if not self.daily_nav:
            print("无交易数据")
            return

        final_assets = self.daily_nav[-1]['total_assets']
        total_return = (final_assets - INITIAL_CAPITAL) / INITIAL_CAPITAL * 100

        # CAGR
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
        drawdowns = []
        for nav in self.daily_nav:
            if nav['total_assets'] > peak:
                peak = nav['total_assets']
            dd = (nav['total_assets'] - peak) / peak * 100  # negative
            drawdowns.append(dd)
            if abs(dd) > abs(max_dd):
                max_dd = dd
                max_dd_date = nav['date']

        # 交易统计
        total_trades = len(self.all_trades)
        wins = sum(1 for t in self.all_trades if t['return_pct'] > 0)
        losses_count = total_trades - wins
        win_rate = wins / total_trades * 100 if total_trades > 0 else 0
        avg_ret = sum(t['return_pct'] for t in self.all_trades) / total_trades if total_trades > 0 else 0

        total_profit = sum(t['return_pct'] for t in self.all_trades if t['return_pct'] > 0)
        total_loss = abs(sum(t['return_pct'] for t in self.all_trades if t['return_pct'] <= 0))
        profit_factor = total_profit / total_loss if total_loss > 0 else float('inf')

        avg_win = total_profit / wins if wins > 0 else 0
        avg_loss = -total_loss / losses_count if losses_count > 0 else 0

        # 逐年统计
        yearly = defaultdict(lambda: {'start_assets': None, 'end_assets': None,
                                       'trades': 0, 'wins': 0, 'total_ret': 0.0})
        for nav in self.daily_nav:
            year = int(nav['date'][:4])
            if yearly[year]['start_assets'] is None:
                yearly[year]['start_assets'] = nav['total_assets']
            yearly[year]['end_assets'] = nav['total_assets']
        for t in self.all_trades:
            year = int(t['date'][:4])
            yearly[year]['trades'] += 1
            yearly[year]['total_ret'] += t['return_pct']
            if t['return_pct'] > 0:
                yearly[year]['wins'] += 1

        # 逐月统计
        monthly = defaultdict(lambda: {'trades': 0, 'total_ret': 0.0, 'start': None, 'end': None})
        for nav in self.daily_nav:
            ym = nav['date'][:7]
            if monthly[ym]['start'] is None:
                monthly[ym]['start'] = nav['total_assets']
            monthly[ym]['end'] = nav['total_assets']

        # === 输出1: 汇总日志 ===
        os.makedirs(LOG_DIR, exist_ok=True)
        summary_lines = []
        summary_lines.append("大阴高开(创业板<50亿精选) - 完整回测")
        summary_lines.append("=" * 70)
        summary_lines.append(f"区间: {START_DATE} ~ {END_DATE}")
        summary_lines.append(f"初始: {INITIAL_CAPITAL:,.0f}")
        summary_lines.append(f"最终: {final_assets:,.0f}")
        summary_lines.append(f"CAGR: {cagr*100:+.1f}%")
        summary_lines.append(f"最大回撤: {max_dd:.1f}% ({max_dd_date})")
        summary_lines.append(f"总交易: {total_trades}笔")
        summary_lines.append(f"胜率: {win_rate:.1f}%")
        summary_lines.append(f"均收益/笔: {avg_ret:+.2f}%")
        summary_lines.append(f"盈亏比: {profit_factor:.2f}")
        summary_lines.append(f"信号总数 vs 捕获数: {self.signals_count} / {total_trades} "
                             f"(捕获率{total_trades/self.signals_count*100:.0f}%)" if self.signals_count > 0 else "")
        summary_lines.append("")
        summary_lines.append("逐年:")
        summary_lines.append(f"{'年':<6}{'交易':>6}  {'胜率':>7}  {'收益':>9}  {'均收益/笔':>9}")
        for year in sorted(yearly.keys()):
            ys = yearly[year]
            if ys['start_assets'] and ys['end_assets']:
                yr_ret = (ys['end_assets'] - ys['start_assets']) / ys['start_assets'] * 100
                wr = ys['wins'] / ys['trades'] * 100 if ys['trades'] > 0 else 0
                avg_r = ys['total_ret'] / ys['trades'] if ys['trades'] > 0 else 0
                summary_lines.append(f"{year:<6}{ys['trades']:>6}  {wr:>6.1f}%  {yr_ret:>+8.1f}%  {avg_r:>+8.2f}%")
        summary_lines.append("")
        summary_lines.append("逐月热力图:")
        # Build month heatmap
        all_years = sorted(yearly.keys())
        header = f"{'月':>6}" + "".join(f"{m:>8}月" for m in range(1, 13))
        summary_lines.append(header)
        for year in all_years:
            row = f"{year:>6}"
            for m in range(1, 13):
                ym = f"{year}-{m:02d}"
                if ym in monthly and monthly[ym]['start'] and monthly[ym]['end']:
                    mr = (monthly[ym]['end'] - monthly[ym]['start']) / monthly[ym]['start'] * 100
                    row += f"{mr:>+8.1f}%"
                else:
                    row += f"{'---':>9}"
                row += " "
            summary_lines.append(row)

        summary_text = "\n".join(summary_lines)
        summary_path = os.path.join(LOG_DIR, "bigdrop_gem_backtest_summary.log")
        with open(summary_path, 'w', encoding='utf-8') as f:
            f.write(summary_text)
        print(f"\n汇总已保存: {summary_path}")
        print(summary_text)

        # === 输出2: trade_log.json ===
        os.makedirs(FRONTEND_DIR, exist_ok=True)
        trade_log = []
        running_portfolio = INITIAL_CAPITAL
        for idx, t in enumerate(self.all_trades, 1):
            cap_before = t['amount_invested']
            cap_after = t['amount_invested'] + t['profit']
            running_portfolio += t['profit']
            trade_log.append({
                'id': idx,
                'date': t['date'],
                'code': t['code'],
                'name': t['name'],
                'strategy': '大阴高开',
                'buy_reason': t['buy_reason'],
                'buy_price': t['buy_price'],
                'sell_date': t['sell_date'],
                'sell_price': t['sell_price'],
                'sell_reason': t['sell_reason'],
                'return_pct': t['return_pct'],
                'holding_days': t['holding_days'],
                'slot': t['slot'],
                'capital_before': round(cap_before, 2),
                'capital_after': round(cap_after, 2),
                'portfolio_value_after': round(running_portfolio, 2)
            })

        trade_log_path = os.path.join(FRONTEND_DIR, "trade_log.json")
        with open(trade_log_path, 'w', encoding='utf-8') as f:
            json.dump(trade_log, f, ensure_ascii=False, indent=2)
        print(f"交易明细已保存: {trade_log_path} ({len(trade_log)}笔)")

        # === 输出3: daily_equity.json ===
        dates_list = [nav['date'] for nav in self.daily_nav]
        equity_list = [round(nav['total_assets'], 2) for nav in self.daily_nav]
        # benchmark: 简单标准化为1.0起始
        benchmark = [1.0] * len(self.daily_nav)  # placeholder
        dd_list = [round(d, 2) for d in drawdowns]

        daily_equity = {
            'dates': dates_list,
            'equity': equity_list,
            'benchmark': benchmark,
            'drawdown': dd_list
        }
        daily_equity_path = os.path.join(FRONTEND_DIR, "daily_equity.json")
        with open(daily_equity_path, 'w', encoding='utf-8') as f:
            json.dump(daily_equity, f, ensure_ascii=False)
        print(f"日频数据已保存: {daily_equity_path}")

        # === 输出4: dashboard_data.json ===
        yearly_stats = []
        for year in sorted(yearly.keys()):
            ys = yearly[year]
            if ys['start_assets'] and ys['end_assets'] and ys['trades'] > 0:
                yr_ret = (ys['end_assets'] - ys['start_assets']) / ys['start_assets'] * 100
                wr = ys['wins'] / ys['trades'] * 100
                avg_r = ys['total_ret'] / ys['trades']
                yearly_stats.append({
                    'year': year,
                    'return_pct': round(yr_ret, 2),
                    'trades': ys['trades'],
                    'win_rate': round(wr, 1),
                    'avg_return': round(avg_r, 2)
                })

        # daily_data: 有交易日 + 每周一快照
        daily_data = {}
        # Build trade lookup by date
        trades_by_buy_date = defaultdict(list)
        trades_by_sell_date = defaultdict(list)
        for t in trade_log:
            trades_by_buy_date[t['date']].append(t)
            trades_by_sell_date[t['sell_date']].append(t)

        for i, nav in enumerate(self.daily_nav):
            date = nav['date']
            has_trade = date in trades_by_buy_date or date in trades_by_sell_date
            # Check if Monday (using datetime)
            try:
                dt = datetime.strptime(date, '%Y-%m-%d')
                is_monday = (dt.weekday() == 0)
            except:
                is_monday = False

            if not has_trade and not is_monday:
                continue

            prev_val = self.daily_nav[i-1]['total_assets'] if i > 0 else INITIAL_CAPITAL
            daily_ret = (nav['total_assets'] - prev_val) / prev_val * 100 if prev_val > 0 else 0
            cum_ret = (nav['total_assets'] - INITIAL_CAPITAL) / INITIAL_CAPITAL * 100

            trades_today = []
            if date in trades_by_buy_date:
                for t in trades_by_buy_date[date]:
                    trades_today.append({'type': 'buy', 'code': t['code'], 'name': t['name'],
                                         'price': t['buy_price'], 'reason': t['buy_reason']})
            if date in trades_by_sell_date:
                for t in trades_by_sell_date[date]:
                    trades_today.append({'type': 'sell', 'code': t['code'], 'name': t['name'],
                                         'price': t['sell_price'], 'return_pct': t['return_pct']})

            daily_data[date] = {
                'portfolio_value': round(nav['total_assets'], 2),
                'daily_return_pct': round(daily_ret, 2),
                'cumulative_return_pct': round(cum_ret, 2),
                'trades_today': trades_today
            }

        dashboard = {
            'summary': {
                'strategy_name': '大阴高开(创业板<50亿精选)',
                'initial_capital': INITIAL_CAPITAL,
                'final_value': round(final_assets, 2),
                'total_return_pct': round(total_return, 2),
                'cagr_pct': round(cagr * 100, 2),
                'total_trades': total_trades,
                'win_rate': round(win_rate, 1),
                'max_drawdown': round(max_dd, 2),
                'avg_return_per_trade': round(avg_ret, 2),
                'profit_factor': round(profit_factor, 2)
            },
            'yearly_stats': yearly_stats,
            'daily_data': daily_data,
            'all_trades': trade_log
        }

        dashboard_path = os.path.join(FRONTEND_DIR, "dashboard_data.json")
        with open(dashboard_path, 'w', encoding='utf-8') as f:
            json.dump(dashboard, f, ensure_ascii=False, indent=2)
        print(f"Dashboard数据已保存: {dashboard_path}")

        print(f"\n{'='*70}")
        print(f"全部输出完成!")
        print(f"  1. {summary_path}")
        print(f"  2. {trade_log_path}")
        print(f"  3. {daily_equity_path}")
        print(f"  4. {dashboard_path}")
        print(f"{'='*70}")


if __name__ == "__main__":
    bt = BigDropGemBacktest()
    bt.run()
