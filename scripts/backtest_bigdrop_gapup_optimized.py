#!/usr/bin/env python3
"""
大阴高开策略 - 多方案优化对比回测
解决信号聚集问题：通过放宽条件、缩短持仓期、增加slot等方案提升捕获率

方案A：T+1退出（其余条件不变）
方案B：放宽选股+T+2退出
方案C：放宽选股+T+1退出
方案D：放宽选股+T+2退出+10slot
方案E：动态仓位（爆发期加仓）+方案B选股+T+1
方案F：中间路线（适度放宽+T+1+5slot）
"""
import sys
import os
import sqlite3
import json
from datetime import datetime
from collections import defaultdict
from copy import deepcopy

# ============ 配置区 ============
DB_PATH = "/home/AIWealth/data/stocks.db"
START_DATE = "2021-01-01"
END_DATE = "2026-06-30"
INITIAL_CAPITAL = 1_000_000
LOG_DIR = "/home/AIWealth/scripts/logs"
# ================================

# 各方案参数定义
# 第一轮结果：放宽条件(≥5%+全板块)会破坏alpha，T+1退出让均收益从+1.79%降到+0.79%
# 第二轮策略：保留alpha来源(创业板小盘+大跌+高开放量=强反弹)，只移除信号聚集的约束
SCHEMES = {
    'A': {
        'name': '去大盘条件+T+2(原alpha)',
        'drop_threshold': -7.0,
        'gapup_min': 2.0,
        'gapup_max': 5.0,
        'vol_mult': 1.5,
        'mkt_cap_max': 50e8,
        'boards': ['sz.300', 'sz.301'],
        'require_index_down': False,  # 去掉大盘昨跌 → 信号翻倍
        'require_volume': True,
        'hold_days': 2,
        'num_slots': 5,
        'dynamic_slots': False,
    },
    'B': {
        'name': '去大盘+8slot+T+2',
        'drop_threshold': -7.0,
        'gapup_min': 2.0,
        'gapup_max': 5.0,
        'vol_mult': 1.5,
        'mkt_cap_max': 50e8,
        'boards': ['sz.300', 'sz.301'],
        'require_index_down': False,
        'require_volume': True,
        'hold_days': 2,
        'num_slots': 8,
        'dynamic_slots': False,
    },
    'C': {
        'name': '去大盘+跌≥6%+T+2+8slot',
        'drop_threshold': -6.0,
        'gapup_min': 2.0,
        'gapup_max': 5.0,
        'vol_mult': 1.5,
        'mkt_cap_max': 50e8,
        'boards': ['sz.300', 'sz.301'],
        'require_index_down': False,
        'require_volume': True,
        'hold_days': 2,
        'num_slots': 8,
        'dynamic_slots': False,
    },
    'D': {
        'name': '去大盘+高开1.5-5%+T+2+8slot',
        'drop_threshold': -7.0,
        'gapup_min': 1.5,
        'gapup_max': 5.0,
        'vol_mult': 1.5,
        'mkt_cap_max': 50e8,
        'boards': ['sz.300', 'sz.301'],
        'require_index_down': False,
        'require_volume': True,
        'hold_days': 2,
        'num_slots': 8,
        'dynamic_slots': False,
    },
    'E': {
        'name': '去大盘+创业板+科创板+T+2+8slot',
        'drop_threshold': -7.0,
        'gapup_min': 2.0,
        'gapup_max': 5.0,
        'vol_mult': 1.5,
        'mkt_cap_max': 50e8,
        'boards': ['sz.300', 'sz.301', 'sh.688'],  # 加科创板(同20%涨跌停)
        'require_index_down': False,
        'require_volume': True,
        'hold_days': 2,
        'num_slots': 8,
        'dynamic_slots': False,
    },
    'F': {
        'name': '去大盘+跌≥6%+高开1.5-5%+T+2+10slot',
        'drop_threshold': -6.0,
        'gapup_min': 1.5,
        'gapup_max': 5.0,
        'vol_mult': 1.5,
        'mkt_cap_max': 50e8,
        'boards': ['sz.300', 'sz.301'],
        'require_index_down': False,
        'require_volume': True,
        'hold_days': 2,
        'num_slots': 10,
        'dynamic_slots': False,
    },
    'G': {
        'name': '去大盘+跌≥6%+高开1.5-5%+市值80亿+T+2+10slot',
        'drop_threshold': -6.0,
        'gapup_min': 1.5,
        'gapup_max': 5.0,
        'vol_mult': 1.5,
        'mkt_cap_max': 80e8,
        'boards': ['sz.300', 'sz.301'],
        'require_index_down': False,
        'require_volume': True,
        'hold_days': 2,
        'num_slots': 10,
        'dynamic_slots': False,
    },
    'H': {
        'name': '动态slot(去大盘+跌≥6%+高开1.5-5%+T+2)',
        'drop_threshold': -6.0,
        'gapup_min': 1.5,
        'gapup_max': 5.0,
        'vol_mult': 1.5,
        'mkt_cap_max': 50e8,
        'boards': ['sz.300', 'sz.301'],
        'require_index_down': False,
        'require_volume': True,
        'hold_days': 2,
        'num_slots': 5,
        'dynamic_slots': True,  # 信号>=3时切换10slot
    },
}


def get_limit_up_price(code, preclose):
    """计算涨停价"""
    if code.startswith("sz.300") or code.startswith("sz.301"):
        return round(preclose * 1.20, 2)
    elif code.startswith("sh.688") or code.startswith("sh.689"):
        return round(preclose * 1.20, 2)
    elif code.startswith("bj."):
        return round(preclose * 1.30, 2)
    else:
        return round(preclose * 1.10, 2)


def get_limit_down_price(code, preclose):
    """计算跌停价"""
    if code.startswith("sz.300") or code.startswith("sz.301"):
        return round(preclose * 0.80, 2)
    elif code.startswith("sh.688") or code.startswith("sh.689"):
        return round(preclose * 0.80, 2)
    elif code.startswith("bj."):
        return round(preclose * 0.70, 2)
    else:
        return round(preclose * 0.90, 2)


def get_limit_ratio(code):
    """获取涨跌停比例"""
    if code.startswith("sz.300") or code.startswith("sz.301"):
        return 0.20
    elif code.startswith("sh.688") or code.startswith("sh.689"):
        return 0.20
    elif code.startswith("bj."):
        return 0.30
    else:
        return 0.10


def is_limit_down_close(code, preclose, close_price, low_price):
    """判断是否跌停收盘"""
    limit_price = get_limit_down_price(code, preclose)
    return close_price <= limit_price and abs(close_price - low_price) < 0.001


def calc_mkt_cap(amount, turn):
    """流通市值 = 成交额 / (换手率/100)"""
    if turn is None or turn <= 0 or amount is None or amount <= 0:
        return None
    return amount / (turn / 100.0)


class MultiSchemeBacktest:
    def __init__(self, scheme_key, scheme_params):
        self.scheme_key = scheme_key
        self.params = scheme_params
        self.conn = sqlite3.connect(DB_PATH)
        self.conn.row_factory = sqlite3.Row
        self.cur = self.conn.cursor()

        self.num_slots = scheme_params['num_slots']
        self.capital = INITIAL_CAPITAL
        self.slots = [None] * self.num_slots
        self.all_trades = []
        self.daily_nav = []
        self.position_log = []
        self.trading_days = []
        self.index_data = {}
        self.signals_count = 0
        self.missed_signals = 0

    def load_trading_days(self):
        self.cur.execute("""
            SELECT DISTINCT date FROM stock_kline
            WHERE date >= ? AND date <= ?
            ORDER BY date
        """, (START_DATE, END_DATE))
        self.trading_days = [r[0] for r in self.cur.fetchall()]

    def load_index_data(self):
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

    def _build_board_filter(self):
        """构建板块SQL过滤条件"""
        conditions = []
        for prefix in self.params['boards']:
            conditions.append(f"t.code LIKE '{prefix}%'")
        return "(" + " OR ".join(conditions) + ")"

    def _get_avg_volume_5d(self, code, day_idx):
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
        """找大阴高开候选股（根据方案参数）"""
        if day_idx < 5:
            return []

        p = self.params

        # 大盘前一日是否收跌
        if p['require_index_down']:
            idx_data = self.index_data.get(yesterday)
            if not idx_data:
                return []
            if idx_data['close'] >= idx_data['preclose']:
                return []

        # 构建板块过滤
        board_filter = self._build_board_filter()

        query = f"""
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
              AND {board_filter}
              AND y.preclose > 0
              AND ((y.close - y.preclose) / y.preclose * 100) <= ?
              AND t.preclose > 0
              AND t.isST = 0
        """
        self.cur.execute(query, (yesterday, today, p['drop_threshold']))
        candidates = []

        for row in self.cur.fetchall():
            row = dict(row)
            name = row['code_name'] or ''
            if 'ST' in name.upper():
                continue

            # 高开幅度检查
            y_close = row['y_close']
            if not y_close or y_close <= 0:
                continue
            gap_ratio = row['open'] / y_close
            gapup_min_ratio = 1.0 + p['gapup_min'] / 100.0
            gapup_max_ratio = 1.0 + p['gapup_max'] / 100.0
            if gap_ratio < gapup_min_ratio or gap_ratio > gapup_max_ratio:
                continue
            gap_pct = (gap_ratio - 1) * 100

            # 非涨停开盘
            limit_up = get_limit_up_price(row['code'], y_close)
            if row['open'] >= limit_up:
                continue

            # hour1_open有效
            if not row['hour1_open'] or row['hour1_open'] <= 0:
                continue

            # 涨停不买：hour1_open不能是涨停价
            limit_up_today = get_limit_up_price(row['code'], row['preclose'])
            if row['hour1_open'] >= limit_up_today:
                continue

            # 市值检查
            mkt_cap = calc_mkt_cap(row['amount'], row['turn'])
            if mkt_cap is None or mkt_cap >= p['mkt_cap_max']:
                continue

            # 放量检查
            if p['require_volume']:
                vol_5d = self._get_avg_volume_5d(row['code'], day_idx - 1)
                if vol_5d is None or vol_5d <= 0:
                    continue
                if row['y_volume'] <= vol_5d * p['vol_mult']:
                    continue

            # 昨日跌幅
            y_drop_pct = (row['y_close'] - row['y_preclose']) / row['y_preclose'] * 100

            row['gap_pct'] = gap_pct
            row['y_drop_pct'] = y_drop_pct
            row['mkt_cap'] = mkt_cap
            row['buy_price'] = row['hour1_open']
            row['buy_reason'] = (f"昨跌{y_drop_pct:.1f}%,"
                                 f"今高开+{gap_pct:.1f}%,"
                                 f"市值{mkt_cap/1e8:.0f}亿")
            candidates.append(row)

        # 按跌幅绝对值从大到小排序
        candidates.sort(key=lambda x: x['y_drop_pct'])
        return candidates

    def get_sell_data(self, code, sell_date):
        self.cur.execute("""
            SELECT date, close, preclose, low, high, open, hour4_close
            FROM stock_kline WHERE code = ? AND date = ?
        """, (code, sell_date))
        r = self.cur.fetchone()
        if r:
            return dict(r)
        return None

    def try_sell(self, slot_idx, current_day_idx):
        """尝试卖出"""
        pos = self.slots[slot_idx]
        if pos is None:
            return None

        buy_day_idx = pos['buy_day_idx']
        hold_days = current_day_idx - buy_day_idx
        target_hold = self.params['hold_days']

        # 持仓天数未到
        if hold_days < target_hold:
            return None

        sell_date = self.trading_days[current_day_idx]
        sell_data = self.get_sell_data(pos['code'], sell_date)
        if sell_data is None:
            return None

        sell_price = sell_data['close']
        if not sell_price or sell_price <= 0:
            return None

        # 跌停不卖
        if sell_data['preclose'] and sell_data['preclose'] > 0:
            low = sell_data['low'] if sell_data['low'] else sell_price
            if is_limit_down_close(pos['code'], sell_data['preclose'], sell_price, low):
                return None

        # 优先hour4_close
        if sell_data['hour4_close'] and sell_data['hour4_close'] > 0:
            sell_price = sell_data['hour4_close']
        else:
            sell_price = sell_data['close']

        # 再次检查卖出价是否为跌停(hour4_close也可能跌停)
        if sell_data['preclose'] and sell_data['preclose'] > 0:
            ld_price = get_limit_down_price(pos['code'], sell_data['preclose'])
            if sell_price <= ld_price:
                return None

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

        self.capital += pos['amount_invested'] + profit
        self.slots[slot_idx] = None
        return trade

    def _calc_total_assets(self, current_day_idx):
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
        self.load_trading_days()
        self.load_index_data()
        print(f"\n方案{self.scheme_key}: {self.params['name']} | "
              f"交易日:{len(self.trading_days)} | slots:{self.num_slots} | "
              f"持仓T+{self.params['hold_days']}")

        for day_idx, today in enumerate(self.trading_days):
            if day_idx < 6:
                continue

            yesterday = self.trading_days[day_idx - 1]

            # Step 1: 卖出
            day_trades = []
            for i in range(self.num_slots):
                trade = self.try_sell(i, day_idx)
                if trade:
                    day_trades.append(trade)
                    self.all_trades.append(trade)

            # Step 2: 买入
            candidates = self.find_candidates(today, yesterday, day_idx)
            self.signals_count += len(candidates)

            # 动态slot逻辑（方案H）
            active_num_slots = self.num_slots
            if self.params['dynamic_slots'] and len(candidates) >= 3:
                # 临时扩展到10 slot
                active_num_slots = 10
                while len(self.slots) < active_num_slots:
                    self.slots.append(None)

            empty_slots = [i for i in range(active_num_slots) if
                           i < len(self.slots) and self.slots[i] is None]

            if candidates and empty_slots:
                held_codes = set(s['code'] for s in self.slots if s is not None)

                for cand in candidates:
                    if not empty_slots:
                        break
                    if cand['code'] in held_codes:
                        continue

                    total_assets = self._calc_total_assets(day_idx)
                    slot_amount = total_assets / active_num_slots
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

                    # 确保slots列表足够长
                    while slot_idx >= len(self.slots):
                        self.slots.append(None)

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
            if candidates:
                bought_today = sum(1 for s in self.slots if s and s.get('buy_date') == today)
                real_missed = len(candidates) - bought_today
                if real_missed > 0:
                    self.missed_signals += real_missed

            # 记录NAV
            total_assets = self._calc_total_assets(day_idx)
            self.daily_nav.append({
                'date': today,
                'total_assets': total_assets,
            })

            # 动态缩回slot（方案H）
            if self.params['dynamic_slots']:
                # 缩回到基础5 slot（只要扩展位空了就缩）
                while len(self.slots) > self.num_slots and self.slots[-1] is None:
                    self.slots.pop()

            if day_idx % 250 == 0:
                print(f"  {today} | 资产:{total_assets:,.0f} | 交易:{len(self.all_trades)}笔")

        self.conn.close()
        return self._calc_stats()

    def _calc_stats(self):
        """计算统计结果"""
        if not self.daily_nav:
            return None

        final_assets = self.daily_nav[-1]['total_assets']
        total_return = (final_assets - INITIAL_CAPITAL) / INITIAL_CAPITAL * 100

        days = len(self.daily_nav)
        years = days / 250.0
        if years > 0 and final_assets > INITIAL_CAPITAL * 0.01:
            cagr = (final_assets / INITIAL_CAPITAL) ** (1.0 / years) - 1
        else:
            cagr = -1.0

        # 最大回撤
        peak = INITIAL_CAPITAL
        max_dd = 0
        for nav in self.daily_nav:
            if nav['total_assets'] > peak:
                peak = nav['total_assets']
            dd = (peak - nav['total_assets']) / peak * 100
            if dd > max_dd:
                max_dd = dd

        # 交易统计
        total_trades = len(self.all_trades)
        wins = sum(1 for t in self.all_trades if t['return_pct'] > 0)
        win_rate = wins / total_trades * 100 if total_trades > 0 else 0
        avg_ret = sum(t['return_pct'] for t in self.all_trades) / total_trades if total_trades > 0 else 0
        capture_rate = total_trades / self.signals_count * 100 if self.signals_count > 0 else 0

        # 逐年收益
        yearly_returns = {}
        yearly_nav = defaultdict(list)
        for nav in self.daily_nav:
            year = nav['date'][:4]
            yearly_nav[year].append(nav['total_assets'])

        for year in sorted(yearly_nav.keys()):
            navs = yearly_nav[year]
            if len(navs) >= 2:
                yr_ret = (navs[-1] - navs[0]) / navs[0] * 100
                yearly_returns[year] = yr_ret

        return {
            'scheme': self.scheme_key,
            'name': self.params['name'],
            'signals': self.signals_count,
            'trades': total_trades,
            'capture_rate': capture_rate,
            'win_rate': win_rate,
            'avg_ret': avg_ret,
            'cagr': cagr * 100,
            'max_dd': max_dd,
            'yearly': yearly_returns,
            'final_assets': final_assets,
            'all_trades': self.all_trades,
            'daily_nav': self.daily_nav,
            'position_log': self.position_log,
        }


def run_all_schemes():
    """运行所有方案并对比"""
    results = []

    for key in sorted(SCHEMES.keys()):
        print(f"\n{'='*60}")
        print(f"运行方案 {key}: {SCHEMES[key]['name']}")
        print(f"{'='*60}")
        bt = MultiSchemeBacktest(key, SCHEMES[key])
        stats = bt.run()
        if stats:
            results.append(stats)
            print(f"  → CAGR: {stats['cagr']:+.1f}% | "
                  f"交易:{stats['trades']}笔 | "
                  f"捕获率:{stats['capture_rate']:.1f}% | "
                  f"胜率:{stats['win_rate']:.1f}% | "
                  f"MaxDD:{stats['max_dd']:.1f}%")

    # 生成对比报告
    generate_comparison_report(results)

    # 找最优方案
    best = find_best_scheme(results)
    if best:
        save_best_details(best)

    return results


def generate_comparison_report(results):
    """生成方案对比报告"""
    os.makedirs(LOG_DIR, exist_ok=True)

    lines = []
    lines.append("=" * 120)
    lines.append("大阴高开策略 - 多方案优化对比")
    lines.append(f"回测区间: {START_DATE} ~ {END_DATE} | 初始资金: {INITIAL_CAPITAL:,.0f}")
    lines.append("=" * 120)
    lines.append("")

    # 表头
    header = (f"{'方案':<4} | {'名称':<22} | {'总信号':>6} | {'执行笔数':>6} | "
              f"{'捕获率':>6} | {'胜率':>5} | {'均收益':>6} | {'CAGR':>7} | {'MaxDD':>6} | ")
    years = sorted(set(y for r in results for y in r['yearly'].keys()))
    for y in years:
        header += f" {y} |"
    lines.append(header)
    lines.append("-" * len(header))

    for r in results:
        row = (f"  {r['scheme']:<2} | {r['name']:<22} | {r['signals']:>6} | {r['trades']:>6} | "
               f"{r['capture_rate']:>5.1f}% | {r['win_rate']:>4.1f}% | "
               f"{r['avg_ret']:>+5.2f}% | {r['cagr']:>+6.1f}% | {r['max_dd']:>5.1f}% | ")
        for y in years:
            yr = r['yearly'].get(y, 0)
            row += f"{yr:>+5.1f}%|"
        lines.append(row)

    lines.append("-" * len(header))
    lines.append("")

    # 详细方案说明
    lines.append("=" * 80)
    lines.append("各方案参数说明:")
    lines.append("=" * 80)
    for key in sorted(SCHEMES.keys()):
        p = SCHEMES[key]
        lines.append(f"\n方案{key}: {p['name']}")
        lines.append(f"  跌幅≥{abs(p['drop_threshold'])}% | 高开{p['gapup_min']}-{p['gapup_max']}%")
        lines.append(f"  市值<{p['mkt_cap_max']/1e8:.0f}亿 | Slot:{p['num_slots']}")
        lines.append(f"  板块: {','.join(p['boards'])}")
        lines.append(f"  大盘昨跌: {'是' if p['require_index_down'] else '否'} | "
                     f"放量: {'是' if p['require_volume'] else '否'}")
        lines.append(f"  持仓: T+{p['hold_days']}退出")
        if p['dynamic_slots']:
            lines.append(f"  动态仓位: 信号≥5时扩展至10slot")

    lines.append("")
    lines.append("=" * 80)

    # 结论
    best = max(results, key=lambda x: x['cagr']) if results else None
    if best:
        all_positive = all(v > 0 for v in best['yearly'].values())
        lines.append(f"\n最优方案: {best['scheme']} ({best['name']})")
        lines.append(f"  CAGR: {best['cagr']:+.2f}% | MaxDD: {best['max_dd']:.2f}%")
        lines.append(f"  所有年份正收益: {'是 ✓' if all_positive else '否 ✗'}")
        if not all_positive:
            # 找所有年份正收益且CAGR最高的
            positive_all = [r for r in results if all(v > 0 for v in r['yearly'].values())]
            if positive_all:
                alt = max(positive_all, key=lambda x: x['cagr'])
                lines.append(f"  所有年份正收益的最优方案: {alt['scheme']} ({alt['name']}) CAGR:{alt['cagr']:+.2f}%")

    report = "\n".join(lines)
    print("\n" + report)

    report_path = os.path.join(LOG_DIR, "bigdrop_gapup_optimized_summary.log")
    with open(report_path, 'w', encoding='utf-8') as f:
        f.write(report)
    print(f"\n对比报告已保存: {report_path}")


def find_best_scheme(results):
    """找最优方案：优先所有年份正收益+CAGR最高"""
    if not results:
        return None

    # 先找所有年份正收益的
    all_positive = [r for r in results if all(v > 0 for v in r['yearly'].values())]
    if all_positive:
        return max(all_positive, key=lambda x: x['cagr'])
    # 否则直接选CAGR最高
    return max(results, key=lambda x: x['cagr'])


def save_best_details(best):
    """保存最优方案的交易明细和仓位日志"""
    os.makedirs(LOG_DIR, exist_ok=True)

    # 交易明细JSON
    trades_path = os.path.join(LOG_DIR, "bigdrop_gapup_optimized_trades.json")
    clean_trades = []
    for t in best['all_trades']:
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
    print(f"最优方案({best['scheme']})交易明细: {trades_path} ({len(clean_trades)}笔)")

    # NAV JSON
    nav_path = os.path.join(LOG_DIR, "bigdrop_gapup_optimized_nav.json")
    with open(nav_path, 'w', encoding='utf-8') as f:
        json.dump(best['daily_nav'], f, ensure_ascii=False, indent=2)
    print(f"最优方案NAV: {nav_path}")


if __name__ == "__main__":
    results = run_all_schemes()
