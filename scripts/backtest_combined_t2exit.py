#!/usr/bin/env python3
"""
组合策略完整回测 - T+2收盘退出
策略1：大阴高开（创业板<50亿，昨跌≥7%放量，今高开2-5%，大盘昨跌）
策略2：MA5缩量突破（市值>700亿，昨收<MA5，今开>MA5突破0-1%，前3日缩量）

买入：T日 hour1_open
卖出：T+2日 hour4_close（即收盘价close）
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

# 大阴高开参数
BIGDROP_THRESHOLD = -7.0       # 昨日跌幅阈值(%)
BIGDROP_GAPUP_MIN = 2.0       # 今日高开下限(%)
BIGDROP_GAPUP_MAX = 5.0       # 今日高开上限(%)
BIGDROP_VOL_MULT = 1.5        # 放量倍数
BIGDROP_MKT_CAP_MAX = 50e8    # 市值上限50亿(元)

# MA5缩量突破参数
MA5_BREAK_MIN = 0.0            # 突破MA5幅度下限(%)
MA5_BREAK_MAX = 1.0            # 突破MA5幅度上限(%)
MA5_MKT_CAP_MIN = 700e8       # 市值下限700亿(元)
# ================================


def get_limit_up_price(code, preclose):
    """计算涨停价（精确到分）"""
    if code.startswith("sz.300") or code.startswith("sz.301"):
        return round(preclose * 1.2, 2)
    elif code.startswith("sh.688"):
        return round(preclose * 1.2, 2)
    elif code.startswith("bj."):
        return round(preclose * 1.3, 2)
    else:
        return round(preclose * 1.1, 2)


def get_limit_down_price(code, preclose):
    """计算跌停价（精确到分）"""
    if code.startswith("sz.300") or code.startswith("sz.301"):
        return round(preclose * 0.8, 2)
    elif code.startswith("sh.688"):
        return round(preclose * 0.8, 2)
    elif code.startswith("bj."):
        return round(preclose * 0.7, 2)
    else:
        return round(preclose * 0.9, 2)


def is_limit_up_open(code, preclose, open_price):
    """判断是否涨停开盘（不可买入）"""
    limit_price = get_limit_up_price(code, preclose)
    return open_price >= limit_price


def is_limit_down(code, preclose, close_price):
    """判断是否跌停（不可卖出）"""
    limit_price = get_limit_down_price(code, preclose)
    return close_price <= limit_price


def calc_mkt_cap(amount, turn):
    """通过成交额和换手率反推流通市值"""
    if turn is None or turn <= 0 or amount is None or amount <= 0:
        return None
    return amount / (turn / 100.0)


class CombinedBacktest:
    def __init__(self):
        self.conn = sqlite3.connect(DB_PATH)
        self.conn.row_factory = sqlite3.Row
        self.cur = self.conn.cursor()

        self.capital = INITIAL_CAPITAL
        self.slots = [None] * NUM_SLOTS  # each slot: dict or None
        self.all_trades = []
        self.daily_nav = []
        self.position_log = []
        self.trading_days = []
        self.index_data = {}  # date -> close_rate

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
        """获取交易日索引"""
        try:
            return self.trading_days.index(date)
        except ValueError:
            return -1

    def find_bigdrop_candidates(self, today, yesterday, day_before_yesterday):
        """找大阴高开候选股"""
        # 需要前5日数据来计算5日均量
        today_idx = self.get_day_idx(today)
        if today_idx < 5:
            return []

        # 检查大盘前一日是否收跌
        idx_data = self.index_data.get(yesterday)
        if not idx_data or idx_data['close_rate'] >= 0:
            return []

        # 查询昨日大阴+今日高开的创业板股票
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
              AND ((t.open - t.preclose) / t.preclose * 100) >= ?
              AND ((t.open - t.preclose) / t.preclose * 100) <= ?
              AND t.isST = 0
        """
        self.cur.execute(query, (yesterday, today, BIGDROP_THRESHOLD,
                                  BIGDROP_GAPUP_MIN, BIGDROP_GAPUP_MAX))
        candidates = []
        for row in self.cur.fetchall():
            row = dict(row)
            # 排除ST（名字检查）
            name = row['code_name'] or ''
            if 'ST' in name.upper():
                continue

            # 检查涨停开盘
            if is_limit_up_open(row['code'], row['preclose'], row['open']):
                continue

            # 检查hour1_open有效
            if not row['hour1_open'] or row['hour1_open'] <= 0:
                continue

            # 市值 < 50亿
            mkt_cap = calc_mkt_cap(row['amount'], row['turn'])
            if mkt_cap is None or mkt_cap >= BIGDROP_MKT_CAP_MAX:
                continue

            # 昨日放量：需要前5日均量
            vol_5d = self._get_avg_volume_5d(row['code'], today_idx - 1)
            if vol_5d is None or vol_5d <= 0:
                continue
            if row['y_volume'] <= vol_5d * BIGDROP_VOL_MULT:
                continue

            # 计算高开幅度和跌幅用于排序
            gap_pct = (row['open'] - row['preclose']) / row['preclose'] * 100
            row['gap_pct'] = gap_pct
            row['mkt_cap'] = mkt_cap
            row['strategy'] = '大阴高开'
            row['buy_price'] = row['hour1_open']
            row['buy_reason'] = (f"昨日跌{row['y_close_rate']:.1f}%放量,"
                                 f"今日高开+{gap_pct:.1f}%,"
                                 f"创业板<{mkt_cap/1e8:.0f}亿,大盘昨跌")
            candidates.append(row)

        return candidates

    def _get_avg_volume_5d(self, code, day_idx):
        """获取前5日平均成交量"""
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

    def preload_ma5_data(self):
        """预加载MA5策略需要的大盘股数据，避免逐股查询"""
        print("  预加载MA5数据...")
        # 加载所有非ST股的日K数据(只取需要的字段)
        self.cur.execute("""
            SELECT date, code, code_name, open, close, preclose, volume, amount, turn,
                   hour1_open, isST
            FROM stock_kline
            WHERE date >= ? AND date <= ? AND isST = 0
              AND preclose > 0 AND open > 0
            ORDER BY code, date
        """, ('2020-06-01', END_DATE))
        # 按code分组
        from collections import defaultdict as dd2
        self.stock_daily = dd2(list)
        for r in self.cur.fetchall():
            row = dict(r)
            self.stock_daily[row['code']].append(row)
        print(f"  已加载 {len(self.stock_daily)} 只股票数据")

    def find_ma5_candidates(self, today, yesterday):
        """找MA5缩量突破候选股"""
        today_idx = self.get_day_idx(today)
        if today_idx < 6:
            return []

        candidates = []
        for code, days_data in self.stock_daily.items():
            # 二分查找today
            # 由于数据已按date排序，用线性查找
            today_pos = None
            for i, d in enumerate(days_data):
                if d['date'] == today:
                    today_pos = i
                    break
            if today_pos is None or today_pos < 6:
                continue

            row = days_data[today_pos]
            name = row['code_name'] or ''
            if 'ST' in name.upper():
                continue

            # 检查hour1_open有效
            if not row['hour1_open'] or row['hour1_open'] <= 0:
                continue

            # 检查涨停开盘
            if is_limit_up_open(code, row['preclose'], row['open']):
                continue

            # 市值 > 700亿
            mkt_cap = calc_mkt_cap(row['amount'], row['turn'])
            if mkt_cap is None or mkt_cap < MA5_MKT_CAP_MIN:
                continue

            # 获取前6天数据
            hist = days_data[today_pos - 6:today_pos]  # 6 days before today
            if len(hist) < 6:
                continue

            # MA5_yesterday = 前5日close的均值
            closes_5 = [h['close'] for h in hist[-5:]]
            if any(c is None or c <= 0 for c in closes_5):
                continue
            ma5_yesterday = sum(closes_5) / 5.0

            # yesterday_close < MA5_yesterday
            yesterday_close = hist[-1]['close']
            if yesterday_close is None or yesterday_close >= ma5_yesterday:
                continue

            # today_open > MA5_yesterday，突破幅度0-1%
            break_pct = (row['open'] - ma5_yesterday) / ma5_yesterday * 100
            if break_pct <= 0 or break_pct > MA5_BREAK_MAX:
                continue

            # 前3日成交量逐日递减（严格递减）
            vols_3 = [h['volume'] for h in hist[-3:]]
            if any(v is None or v <= 0 for v in vols_3):
                continue
            if not (vols_3[0] > vols_3[1] > vols_3[2]):
                continue

            row_copy = dict(row)
            row_copy['strategy'] = 'MA5缩量突破'
            row_copy['buy_price'] = row['hour1_open']
            row_copy['mkt_cap'] = mkt_cap
            row_copy['break_pct'] = break_pct
            row_copy['buy_reason'] = (f"昨收{yesterday_close:.2f}<MA5={ma5_yesterday:.2f},"
                                      f"今开突破+{break_pct:.2f}%,"
                                      f"前3日缩量,市值{mkt_cap/1e8:.0f}亿")
            candidates.append(row_copy)

        return candidates

    def get_sell_data(self, code, sell_date):
        """获取卖出日的数据"""
        self.cur.execute("""
            SELECT date, close, preclose, hour4_close, open, high, low
            FROM stock_kline WHERE code = ? AND date = ?
        """, (code, sell_date))
        r = self.cur.fetchone()
        if r:
            return dict(r)
        return None

    def try_sell(self, slot_idx, current_day_idx):
        """尝试卖出某个仓位"""
        pos = self.slots[slot_idx]
        if pos is None:
            return None

        # 计算持有天数(从买入日开始)
        buy_day_idx = pos['buy_day_idx']
        hold_days = current_day_idx - buy_day_idx

        # T+2才卖出（买入日=D0, D1持有, D2卖出）
        if hold_days < 2:
            return None

        sell_date = self.trading_days[current_day_idx]
        sell_data = self.get_sell_data(pos['code'], sell_date)
        if sell_data is None:
            return None

        # 检查是否跌停（不可卖）
        sell_price = sell_data['close']  # hour4_close = close
        if sell_data['preclose'] and sell_data['preclose'] > 0:
            if is_limit_down(pos['code'], sell_data['preclose'], sell_price):
                # 跌停不卖，延迟
                return None

        # 使用hour4_close优先，否则用close
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
            'strategy': pos['strategy'],
            'buy_reason': pos['buy_reason'],
            'buy_price': pos['buy_price'],
            'buy_time': 'hour1_open',
            'sell_date': sell_date,
            'sell_price': round(sell_price, 2),
            'sell_time': 'hour4_close',
            'return_pct': round(ret_pct, 2),
            'holding_days': hold_days,
            'slot': slot_idx + 1,
            'profit': round(profit, 2),
            'amount_invested': pos['amount_invested']
        }

        # 更新资金
        self.capital += pos['amount_invested'] + profit
        self.slots[slot_idx] = None

        return trade

    def run(self):
        """运行回测"""
        print("=" * 60)
        print("组合策略回测 - T+2收盘退出")
        print(f"区间: {START_DATE} ~ {END_DATE}")
        print(f"初始资金: {INITIAL_CAPITAL:,.0f}")
        print(f"仓位数: {NUM_SLOTS}")
        print("=" * 60)

        self.load_trading_days()
        self.load_index_data()
        self.preload_ma5_data()

        for day_idx, today in enumerate(self.trading_days):
            if day_idx < 6:
                continue

            yesterday = self.trading_days[day_idx - 1]
            day_before = self.trading_days[day_idx - 2]

            # === Step 1: 先确定哪些仓位今天会卖（但先不执行，因为买在前卖在后）===
            # 找出空仓位（不包括今天要卖的，因为卖在收盘、买在开盘，时间上买先发生）
            pending_sell_slots = set()
            for i in range(NUM_SLOTS):
                pos = self.slots[i]
                if pos is not None:
                    hold_days = day_idx - pos['buy_day_idx']
                    if hold_days >= 2:
                        pending_sell_slots.add(i)

            # === Step 2: 寻找新信号并买入（只用当前空仓位）===
            empty_slots = [i for i in range(NUM_SLOTS)
                           if self.slots[i] is None and i not in pending_sell_slots]

            if empty_slots:
                # 查找候选股
                bigdrop_cands = self.find_bigdrop_candidates(today, yesterday, day_before)
                ma5_cands = self.find_ma5_candidates(today, yesterday)

                # 排序：大阴高开优先，同类按高开幅度排序
                all_cands = []
                for c in bigdrop_cands:
                    c['priority'] = 0  # higher priority
                    all_cands.append(c)
                for c in ma5_cands:
                    c['priority'] = 1
                    all_cands.append(c)

                # 按优先级+gap排序
                all_cands.sort(key=lambda x: (x['priority'], -x.get('gap_pct', 0)))

                # 去重（同一只股票只买一次）
                bought_codes = set(s['code'] for s in self.slots if s is not None)

                for cand in all_cands:
                    if not empty_slots:
                        break
                    if cand['code'] in bought_codes:
                        continue

                    # 计算买入金额：当前总资产/5
                    total_assets = self._calc_total_assets(day_idx)
                    slot_amount = total_assets / NUM_SLOTS
                    buy_price = cand['buy_price']

                    if buy_price <= 0:
                        continue

                    shares = int(slot_amount / buy_price / 100) * 100  # 整百股
                    if shares <= 0:
                        continue

                    actual_amount = shares * buy_price
                    if actual_amount > self.capital:
                        # 资金不足
                        shares = int(self.capital / buy_price / 100) * 100
                        if shares <= 0:
                            continue
                        actual_amount = shares * buy_price

                    # 扣除资金
                    self.capital -= actual_amount
                    slot_idx = empty_slots.pop(0)
                    bought_codes.add(cand['code'])

                    self.slots[slot_idx] = {
                        'code': cand['code'],
                        'name': cand['code_name'] or '',
                        'strategy': cand['strategy'],
                        'buy_price': buy_price,
                        'buy_date': today,
                        'buy_day_idx': day_idx,
                        'shares': shares,
                        'amount_invested': actual_amount,
                        'buy_reason': cand['buy_reason']
                    }

            # === Step 3: 执行卖出 ===
            day_trades = []
            for i in pending_sell_slots:
                trade = self.try_sell(i, day_idx)
                if trade:
                    day_trades.append(trade)
                    self.all_trades.append(trade)

            # === Step 4: 记录每日状态 ===
            total_assets = self._calc_total_assets(day_idx)
            prev_assets = self.daily_nav[-1]['total_assets'] if self.daily_nav else INITIAL_CAPITAL
            day_return = (total_assets - prev_assets) / prev_assets * 100 if prev_assets > 0 else 0

            self.daily_nav.append({
                'date': today,
                'total_assets': total_assets,
                'cash': self.capital,
                'day_return': day_return
            })

            # 仓位日志
            self._log_positions(today, total_assets, day_return, day_trades)

            # 进度显示
            if day_idx % 100 == 0:
                print(f"  进度: {today} | 总资产: {total_assets:,.0f} | "
                      f"累计交易: {len(self.all_trades)}笔")

        # 最终统计
        self._print_summary()
        self._save_outputs()

    def _calc_total_assets(self, current_day_idx):
        """计算当前总资产"""
        total = self.capital
        current_date = self.trading_days[current_day_idx]

        for slot in self.slots:
            if slot is None:
                continue
            # 获取当日收盘价估值
            self.cur.execute("""
                SELECT close FROM stock_kline
                WHERE code = ? AND date = ?
            """, (slot['code'], current_date))
            r = self.cur.fetchone()
            if r and r['close'] and r['close'] > 0:
                total += slot['shares'] * r['close']
            else:
                # 用买入价估值
                total += slot['amount_invested']
        return total

    def _log_positions(self, today, total_assets, day_return, day_trades):
        """记录仓位日志"""
        log_entry = f"{today} | 总资产:{total_assets:,.0f} | 日收益:{day_return:+.2f}%\n"

        for i in range(NUM_SLOTS):
            slot = self.slots[i]
            if slot is None:
                # 检查今天是否有卖出
                sold = [t for t in day_trades if t['slot'] == i + 1]
                if sold:
                    t = sold[0]
                    log_entry += (f"  仓位{i+1}: [卖出] {t['name']}({t['code']}) "
                                  f"{t['date']}买@{t['buy_price']:.2f} → "
                                  f"{t['sell_date']}卖@{t['sell_price']:.2f} "
                                  f"{t['return_pct']:+.1f}% {t['strategy']}\n")
                else:
                    log_entry += f"  仓位{i+1}: [空仓] 等待信号\n"
            else:
                today_idx = self.get_day_idx(today)
                hold_days = today_idx - slot['buy_day_idx']
                # 获取当前价格
                self.cur.execute("SELECT close FROM stock_kline WHERE code=? AND date=?",
                                 (slot['code'], today))
                r = self.cur.fetchone()
                cur_price = r['close'] if r and r['close'] else slot['buy_price']
                cur_ret = (cur_price - slot['buy_price']) / slot['buy_price'] * 100

                status = f"持有D{hold_days}"
                note = ""
                if hold_days >= 1:
                    note = " 明日卖出" if hold_days == 1 else " 今日应卖"

                log_entry += (f"  仓位{i+1}: [{status}] {slot['name']}({slot['code']}) "
                              f"买入{slot['buy_date']}@{slot['buy_price']:.2f} "
                              f"当前{cur_price:.2f} {cur_ret:+.1f}% "
                              f"{slot['strategy']}{note}\n")

        self.position_log.append(log_entry)

    def _print_summary(self):
        """打印并保存汇总统计"""
        if not self.daily_nav:
            print("无交易数据")
            return

        final_assets = self.daily_nav[-1]['total_assets']
        total_return = (final_assets - INITIAL_CAPITAL) / INITIAL_CAPITAL * 100

        # 计算年化收益
        days = len(self.daily_nav)
        years = days / 250.0
        if years > 0 and final_assets > 0:
            cagr = (final_assets / INITIAL_CAPITAL) ** (1.0 / years) - 1
        else:
            cagr = 0

        # 最大回撤
        peak = INITIAL_CAPITAL
        max_dd = 0
        for nav in self.daily_nav:
            if nav['total_assets'] > peak:
                peak = nav['total_assets']
            dd = (peak - nav['total_assets']) / peak * 100
            if dd > max_dd:
                max_dd = dd

        # 胜率
        wins = sum(1 for t in self.all_trades if t['return_pct'] > 0)
        total_trades = len(self.all_trades)
        win_rate = wins / total_trades * 100 if total_trades > 0 else 0

        # 按策略分类统计
        strat_stats = defaultdict(lambda: {'count': 0, 'wins': 0, 'total_ret': 0})
        for t in self.all_trades:
            s = t['strategy']
            strat_stats[s]['count'] += 1
            strat_stats[s]['total_ret'] += t['return_pct']
            if t['return_pct'] > 0:
                strat_stats[s]['wins'] += 1

        # 逐年统计
        yearly = defaultdict(lambda: {'start_assets': None, 'end_assets': None, 'trades': 0, 'wins': 0})
        for nav in self.daily_nav:
            year = nav['date'][:4]
            if yearly[year]['start_assets'] is None:
                yearly[year]['start_assets'] = nav['total_assets']
            yearly[year]['end_assets'] = nav['total_assets']
        for t in self.all_trades:
            year = t['date'][:4]
            yearly[year]['trades'] += 1
            if t['return_pct'] > 0:
                yearly[year]['wins'] += 1

        # 输出
        summary = []
        summary.append("=" * 60)
        summary.append("组合策略回测结果 - T+2收盘退出")
        summary.append("=" * 60)
        summary.append(f"回测区间: {START_DATE} ~ {END_DATE}")
        summary.append(f"初始资金: {INITIAL_CAPITAL:,.0f}")
        summary.append(f"最终资产: {final_assets:,.0f}")
        summary.append(f"总收益率: {total_return:.2f}%")
        summary.append(f"年化收益(CAGR): {cagr*100:.2f}%")
        summary.append(f"最大回撤: {max_dd:.2f}%")
        summary.append(f"总交易笔数: {total_trades}")
        summary.append(f"胜率: {win_rate:.1f}% ({wins}/{total_trades})")
        summary.append(f"平均收益/笔: {sum(t['return_pct'] for t in self.all_trades)/total_trades:.2f}%" if total_trades > 0 else "")
        summary.append("")
        summary.append("--- 策略分类统计 ---")
        for s, st in strat_stats.items():
            avg_ret = st['total_ret'] / st['count'] if st['count'] > 0 else 0
            wr = st['wins'] / st['count'] * 100 if st['count'] > 0 else 0
            summary.append(f"  {s}: {st['count']}笔, 胜率{wr:.1f}%, 平均收益{avg_ret:.2f}%")
        summary.append("")
        summary.append("--- 逐年表现 ---")
        for year in sorted(yearly.keys()):
            ys = yearly[year]
            if ys['start_assets'] and ys['end_assets']:
                yr_ret = (ys['end_assets'] - ys['start_assets']) / ys['start_assets'] * 100
                wr = ys['wins'] / ys['trades'] * 100 if ys['trades'] > 0 else 0
                summary.append(f"  {year}: 收益{yr_ret:+.1f}%, {ys['trades']}笔交易, 胜率{wr:.1f}%")
        summary.append("=" * 60)

        self.summary_text = "\n".join(summary)
        print(self.summary_text)

    def _save_outputs(self):
        """保存所有输出文件"""
        os.makedirs(LOG_DIR, exist_ok=True)

        # 1. Summary log
        summary_path = os.path.join(LOG_DIR, "combined_t2exit_summary.log")
        with open(summary_path, 'w', encoding='utf-8') as f:
            f.write(self.summary_text)
        print(f"\n汇总已保存: {summary_path}")

        # 2. Trades JSON
        trades_path = os.path.join(LOG_DIR, "combined_t2exit_trades.json")
        # 清理trades中的不可序列化字段
        clean_trades = []
        for t in self.all_trades:
            clean_trades.append({
                'date': t['date'],
                'code': t['code'],
                'name': t['name'],
                'strategy': t['strategy'],
                'buy_reason': t['buy_reason'],
                'buy_price': t['buy_price'],
                'buy_time': t['buy_time'],
                'sell_date': t['sell_date'],
                'sell_price': t['sell_price'],
                'sell_time': t['sell_time'],
                'return_pct': t['return_pct'],
                'holding_days': t['holding_days'],
                'slot': t['slot']
            })
        with open(trades_path, 'w', encoding='utf-8') as f:
            json.dump(clean_trades, f, ensure_ascii=False, indent=2)
        print(f"交易明细已保存: {trades_path}")

        # 3. Positions log
        pos_path = os.path.join(LOG_DIR, "combined_t2exit_positions.log")
        with open(pos_path, 'w', encoding='utf-8') as f:
            # 每10个交易日记录一次（太多太大）
            # 实际全量记录
            for entry in self.position_log:
                f.write(entry + "\n")
        print(f"仓位日志已保存: {pos_path}")


if __name__ == "__main__":
    bt = CombinedBacktest()
    bt.run()
