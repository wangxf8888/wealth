#!/usr/bin/env python3
"""
T+1可执行真实收益研究
=======================
背景：A股T+1规则意味着当日买入不可当日卖出。之前研究测量的"日内收益"不可执行。
本脚本对三大策略重新测量T+1规则下的实际可执行收益。

三大策略:
  1. MA5缩量突破(>700亿): yesterday_close<MA5, today_open>MA5, 微幅0-1%, 前3日缩量
  2. 换手率突增5x+(50-200亿+大盘昨涨): 前5日均turn的5倍, 涨幅<3%, 大盘昨涨
  3. 大阴高开(创业板<50亿): 昨跌>=7%, 高开2-5%, 放量, 大盘昨跌

买入价 = day_T hour1_open

卖出选项:
  A. T+1 hour1_open (隔夜收益)
  B. T+1 hour2_open (持有到次日10点)
  C. T+1 hour4_close (持有到次日收盘)
  D. T+2 hour1_open (持有2夜)
  E. T+2 hour4_close (持有2天收盘)
  F. T+3 hour1_open (持有3夜)

跌停不可卖出: 如果卖出时刻为跌停价，延迟到下一时段
"""
import sys
import os
import sqlite3
import math
from collections import defaultdict
from datetime import datetime

# ========== 配置区 ==========
DB_PATH = '/home/AIWealth/data/stocks.db'
LOG_PATH = '/home/AIWealth/scripts/logs/t1_executable_returns.log'
START_DATE = '2021-01-01'
END_DATE = '2026-06-30'
INDEX_CODE = 'sh.000001'
# ============================


def get_limit_ratio(code):
    if code.startswith('sz.300') or code.startswith('sz.301') or code.startswith('sz.302'):
        return 0.20
    elif code.startswith('sh.688') or code.startswith('sh.689'):
        return 0.20
    elif code.startswith('bj.'):
        return 0.30
    return 0.10


def calc_limit_up(preclose, code):
    return round(preclose * (1 + get_limit_ratio(code)), 2)


def calc_limit_down(preclose, code):
    return round(preclose * (1 - get_limit_ratio(code)), 2)


def is_limit_down_price(price, preclose, code):
    """判断价格是否为跌停价"""
    if not price or not preclose or preclose <= 0:
        return False
    ld = calc_limit_down(preclose, code)
    return price <= ld


def get_board(code):
    if code.startswith('sh.60') or code.startswith('sz.00'):
        return 'main'
    elif code.startswith('sz.30'):
        return 'gem'
    elif code.startswith('sh.688') or code.startswith('sh.689'):
        return 'star'
    return 'other'


class DataLoader:
    """高效数据加载器，一次性加载所有需要的数据到内存"""

    def __init__(self, db_path):
        self.conn = sqlite3.connect(db_path)
        self.cur = self.conn.cursor()
        self.stock_data = {}  # code -> [row_dict, ...] sorted by date
        self.stock_date_idx = {}  # code -> {date: idx}
        self.all_days = []
        self.day_index = {}
        self.index_data = {}  # date -> close_rate

    def load(self):
        print("加载交易日列表...")
        self.cur.execute("SELECT DISTINCT date FROM stock_kline WHERE date >= ? AND date <= ? ORDER BY date",
                         (START_DATE, END_DATE))
        self.all_days = [r[0] for r in self.cur.fetchall()]
        self.day_index = {d: i for i, d in enumerate(self.all_days)}
        print(f"  交易日: {len(self.all_days)} ({self.all_days[0]} ~ {self.all_days[-1]})")

        print("加载上证指数数据...")
        self.cur.execute("SELECT date, close_rate FROM index_kline WHERE code = ?", (INDEX_CODE,))
        self.index_data = {r[0]: r[1] for r in self.cur.fetchall()}
        print(f"  指数数据天数: {len(self.index_data)}")

        # 需要缓冲前置数据(MA计算需要)
        buffer_start = '2020-06-01'
        print(f"加载股票K线数据 ({buffer_start} ~ {END_DATE})...")
        self.cur.execute("""
            SELECT code, code_name, date, open, high, low, close, preclose, isST, turn,
                   volume, amount,
                   hour1_open, hour2_open, hour4_close
            FROM stock_kline
            WHERE date >= ? AND date <= ?
            ORDER BY code, date
        """, (buffer_start, END_DATE))

        stock_data_raw = defaultdict(list)
        row_count = 0
        for r in self.cur.fetchall():
            stock_data_raw[r[0]].append({
                'code_name': r[1], 'date': r[2], 'open': r[3], 'high': r[4],
                'low': r[5], 'close': r[6], 'preclose': r[7], 'isST': r[8],
                'turn': r[9], 'volume': r[10], 'amount': r[11],
                'hour1_open': r[12], 'hour2_open': r[13], 'hour4_close': r[14]
            })
            row_count += 1

        self.stock_data = dict(stock_data_raw)
        for code, rows in self.stock_data.items():
            self.stock_date_idx[code] = {row['date']: i for i, row in enumerate(rows)}

        print(f"  加载完成: {len(self.stock_data)} 只股票, {row_count} 条记录")
        self.conn.close()

    def get_day_offset(self, date, offset):
        """获取date之后offset个交易日的日期"""
        idx = self.day_index.get(date)
        if idx is None:
            return None
        target_idx = idx + offset
        if 0 <= target_idx < len(self.all_days):
            return self.all_days[target_idx]
        return None

    def get_stock_row(self, code, date):
        """获取某股票某天数据"""
        idx_map = self.stock_date_idx.get(code)
        if idx_map is None:
            return None
        idx = idx_map.get(date)
        if idx is None:
            return None
        return self.stock_data[code][idx]


def calc_sell_returns(loader, code, buy_date, buy_price):
    """
    计算6种卖出时机的收益。
    如果卖出时刻为跌停价，延迟到下一时段。
    返回 dict: {A: ret_or_None, B: ..., C: ..., D: ..., E: ..., F: ...}
    """
    results = {}

    # T+1
    t1_date = loader.get_day_offset(buy_date, 1)
    # T+2
    t2_date = loader.get_day_offset(buy_date, 2)
    # T+3
    t3_date = loader.get_day_offset(buy_date, 3)

    t1_row = loader.get_stock_row(code, t1_date) if t1_date else None
    t2_row = loader.get_stock_row(code, t2_date) if t2_date else None
    t3_row = loader.get_stock_row(code, t3_date) if t3_date else None

    # 卖出价序列（按时间顺序）: T1h1, T1h2, T1h4close, T2h1, T2h4close, T3h1
    sell_sequence = []
    if t1_row:
        sell_sequence.append(('A', t1_row.get('hour1_open'), t1_row.get('preclose')))
        sell_sequence.append(('B', t1_row.get('hour2_open'), t1_row.get('preclose')))
        sell_sequence.append(('C', t1_row.get('hour4_close') or t1_row.get('close'), t1_row.get('preclose')))
    if t2_row:
        sell_sequence.append(('D', t2_row.get('hour1_open'), t2_row.get('preclose')))
        sell_sequence.append(('E', t2_row.get('hour4_close') or t2_row.get('close'), t2_row.get('preclose')))
    if t3_row:
        sell_sequence.append(('F', t3_row.get('hour1_open'), t3_row.get('preclose')))

    # 处理跌停不可卖出逻辑
    pending_labels = set()  # 被跌停阻挡、需延迟的标签

    for i, (label, sell_price, preclose) in enumerate(sell_sequence):
        if sell_price is None or sell_price <= 0 or preclose is None or preclose <= 0:
            # 数据缺失，跳过
            results[label] = None
            continue

        if is_limit_down_price(sell_price, preclose, code):
            # 跌停卖不出，该标签无法在此时段成交
            results[label] = None
            pending_labels.add(label)
            # 同时把所有pending的也标记为None（它们也卖不出）
        else:
            # 可以卖出
            ret = (sell_price - buy_price) / buy_price * 100
            # 给自己赋值
            if label not in results or results[label] is None:
                results[label] = ret
            # 给所有pending的标签也用此价格成交
            for pl in list(pending_labels):
                results[pl] = ret
            pending_labels.clear()

    # 确保所有label都有值
    for label in ['A', 'B', 'C', 'D', 'E', 'F']:
        if label not in results:
            results[label] = None

    return results


def scan_strategy_ma5(loader):
    """
    策略1：MA5缩量突破(>700亿)
    - yesterday_close < MA5_yesterday
    - today_open > MA5_yesterday (突破)
    - today_open高于MA5幅度 0-1% (微幅)
    - 前3日成交量逐日递减(缩量整理)
    - 市值 > 700亿
    - 非ST，非涨停开盘
    """
    signals = []
    processed = 0

    for code, days_data in loader.stock_data.items():
        if len(days_data) < 15:
            continue

        code_name = days_data[0]['code_name']
        if code_name and 'ST' in code_name.upper():
            continue

        processed += 1

        for idx in range(15, len(days_data)):
            today = days_data[idx]
            yesterday = days_data[idx - 1]

            if today['date'] < START_DATE or today['date'] > END_DATE:
                continue
            if today['isST'] or yesterday['isST']:
                continue

            t_open = today['open']
            t_preclose = today['preclose']
            t_h1_open = today['hour1_open']
            t_amount = today['amount']
            yd_close = yesterday['close']
            yd_preclose = yesterday['preclose']
            yd_turn = yesterday['turn']

            if not all([t_open, t_preclose, t_h1_open, yd_close, yd_preclose]):
                continue
            if t_open <= 0 or t_preclose <= 0 or yd_close <= 0:
                continue
            if not t_h1_open or t_h1_open <= 0:
                continue

            # 换手率过滤
            if not yd_turn or yd_turn < 1.0:
                continue

            # 排除yesterday涨停
            if yd_close >= calc_limit_up(yd_preclose, code):
                continue

            # 排除一字涨停开盘
            t_high = today['high']
            t_low = today['low']
            t_close = today['close']
            if t_open == t_high == t_low == t_close and t_close and t_close >= calc_limit_up(t_preclose, code):
                continue

            # 市值 > 700亿
            if not t_amount or not yd_turn or yd_turn <= 0:
                continue
            market_cap = t_amount * 100 / yd_turn / 1e8
            if market_cap < 700:
                continue

            # MA5计算(用yesterday及之前)
            closes = [days_data[i]['close'] for i in range(max(0, idx - 15), idx)
                      if days_data[i]['close'] is not None and days_data[i]['close'] > 0]
            if len(closes) < 5:
                continue
            ma5_yd = sum(closes[-5:]) / 5

            # yesterday_close < MA5_yesterday
            if yd_close >= ma5_yd:
                continue

            # today_open > MA5_yesterday (突破)
            if t_open <= ma5_yd:
                continue

            # 高于MA5幅度 0-1%
            pct_above = (t_open - ma5_yd) / ma5_yd * 100
            if pct_above < 0 or pct_above >= 1:
                continue

            # 前3日成交量逐日递减(缩量整理)
            volumes = [days_data[i]['volume'] for i in range(max(0, idx - 4), idx)
                       if days_data[i]['volume'] is not None and days_data[i]['volume'] > 0]
            if len(volumes) < 3:
                continue
            v3 = volumes[-3:]
            if not (v3[0] > v3[1] > v3[2]):
                continue

            # 买入价
            buy_price = t_h1_open
            buy_date = today['date']

            signals.append({
                'code': code,
                'date': buy_date,
                'buy_price': buy_price,
                'year': buy_date[:4],
            })

        if processed % 500 == 0:
            print(f"  [MA5] 已处理 {processed} 只, 信号 {len(signals)}")

    print(f"  [MA5] 完成: {processed} 只股票, {len(signals)} 信号")
    return signals


def scan_strategy_turnover_surge(loader):
    """
    策略2：换手率突增5x+(50-200亿+大盘昨涨)
    - 近5日换手率均值为X，yesterday换手率 >= 5*X
    - yesterday股价涨幅 < 3%
    - 市值50-200亿
    - 前一日上证指数收涨
    - 非ST
    """
    signals = []
    processed = 0

    for code, days_data in loader.stock_data.items():
        if len(days_data) < 10:
            continue

        code_name = days_data[0]['code_name']
        if code_name and 'ST' in code_name.upper():
            continue

        processed += 1

        for idx in range(7, len(days_data)):
            today = days_data[idx]
            yesterday = days_data[idx - 1]

            if today['date'] < START_DATE or today['date'] > END_DATE:
                continue
            if today['isST'] or yesterday['isST']:
                continue

            t_h1_open = today['hour1_open']
            if not t_h1_open or t_h1_open <= 0:
                continue

            yd_turn = yesterday['turn']
            yd_close = yesterday['close']
            yd_preclose = yesterday['preclose']
            yd_amount = yesterday['amount']

            if not yd_turn or yd_turn <= 0:
                continue
            if not yd_close or not yd_preclose or yd_preclose <= 0:
                continue

            # yesterday涨幅 < 3%
            yd_ret = (yd_close - yd_preclose) / yd_preclose * 100
            if abs(yd_ret) >= 3.0:
                continue

            # 市值50-200亿
            if not yd_amount or yd_amount <= 0:
                continue
            market_cap = yd_amount * 100 / yd_turn / 1e8
            if market_cap < 50 or market_cap >= 200:
                continue

            # 前5日换手率
            prev_turns = []
            for i in range(max(0, idx - 6), idx - 1):
                t = days_data[i]['turn']
                if t is not None and t > 0:
                    prev_turns.append(t)
            if len(prev_turns) < 4:
                continue

            mean_turn = sum(prev_turns) / len(prev_turns)
            if mean_turn < 0.5:
                continue

            # yesterday换手率 >= 5*X
            if yd_turn < 5 * mean_turn:
                continue

            # 前一日上证指数收涨
            yesterday_date = yesterday['date']
            idx_ret = loader.index_data.get(yesterday_date)
            if idx_ret is None or idx_ret <= 0:
                continue

            # 排除一字板
            yd_open = yesterday['open']
            yd_high = yesterday['high']
            yd_low = yesterday['low']
            if yd_open and yd_high and yd_low and yd_close:
                if yd_open == yd_high == yd_low == yd_close:
                    lu = calc_limit_up(yd_preclose, code)
                    ld = calc_limit_down(yd_preclose, code)
                    if yd_close >= lu or yd_close <= ld:
                        continue

            buy_price = t_h1_open
            buy_date = today['date']

            signals.append({
                'code': code,
                'date': buy_date,
                'buy_price': buy_price,
                'year': buy_date[:4],
            })

        if processed % 500 == 0:
            print(f"  [TurnSurge] 已处理 {processed} 只, 信号 {len(signals)}")

    print(f"  [TurnSurge] 完成: {processed} 只股票, {len(signals)} 信号")
    return signals


def scan_strategy_bigdrop_gapup(loader):
    """
    策略3：大阴高开(创业板<50亿+跌>=7%+高开2-5%+放量+大盘跌)
    - yesterday跌幅 >= 7%
    - today_open高于yesterday_close 2-5%
    - yesterday成交量 > 5日均量×1.5
    - 创业板(30开头)
    - 市值<50亿
    - 前一日上证指数收跌
    - 非ST
    """
    signals = []
    processed = 0

    for code, days_data in loader.stock_data.items():
        if len(days_data) < 10:
            continue

        # 仅创业板
        if not (code.startswith('sz.300') or code.startswith('sz.301') or code.startswith('sz.302')):
            continue

        code_name = days_data[0]['code_name']
        if code_name and 'ST' in code_name.upper():
            continue

        processed += 1

        for idx in range(7, len(days_data)):
            today = days_data[idx]
            yesterday = days_data[idx - 1]

            if today['date'] < START_DATE or today['date'] > END_DATE:
                continue
            if today['isST'] or yesterday['isST']:
                continue

            t_open = today['open']
            t_h1_open = today['hour1_open']
            t_preclose = today['preclose']
            yd_close = yesterday['close']
            yd_preclose = yesterday['preclose']
            yd_turn = yesterday['turn']
            yd_volume = yesterday['volume']
            yd_amount = yesterday['amount']

            if not all([t_open, t_h1_open, t_preclose, yd_close, yd_preclose]):
                continue
            if t_open <= 0 or t_preclose <= 0 or yd_close <= 0 or yd_preclose <= 0:
                continue
            if not t_h1_open or t_h1_open <= 0:
                continue

            # yesterday跌幅 >= 7%
            yd_ret = (yd_close - yd_preclose) / yd_preclose * 100
            if yd_ret > -7.0:
                continue

            # today高开 = (today_open - yesterday_close) / yesterday_close
            gap_pct = (t_open - yd_close) / yd_close * 100
            if gap_pct < 2 or gap_pct >= 5:
                continue

            # 市值 < 50亿
            if not yd_amount or not yd_turn or yd_turn <= 0:
                continue
            market_cap = yd_amount * 100 / yd_turn / 1e8
            if market_cap >= 50:
                continue

            # 放量: yesterday成交量 > 5日均量×1.5
            if not yd_volume or yd_volume <= 0:
                continue
            prev_volumes = []
            for i in range(max(0, idx - 6), idx - 1):
                v = days_data[i]['volume']
                if v is not None and v > 0:
                    prev_volumes.append(v)
            if len(prev_volumes) < 3:
                continue
            avg_vol_5d = sum(prev_volumes) / len(prev_volumes)
            if yd_volume <= avg_vol_5d * 1.5:
                continue

            # 前一日上证指数收跌
            yesterday_date = yesterday['date']
            idx_ret = loader.index_data.get(yesterday_date)
            if idx_ret is None or idx_ret >= 0:
                continue

            buy_price = t_h1_open
            buy_date = today['date']

            signals.append({
                'code': code,
                'date': buy_date,
                'buy_price': buy_price,
                'year': buy_date[:4],
            })

        if processed % 500 == 0:
            print(f"  [BigDrop] 已处理 {processed} 只, 信号 {len(signals)}")

    print(f"  [BigDrop] 完成: {processed} 只股票, {len(signals)} 信号")
    return signals


def calc_exit_stats(returns_by_exit):
    """计算每种卖出方式的统计"""
    stats = {}
    for exit_label in ['A', 'B', 'C', 'D', 'E', 'F']:
        rets = [r for r in returns_by_exit[exit_label] if r is not None]
        if not rets:
            stats[exit_label] = None
            continue

        n = len(rets)
        avg = sum(rets) / n
        win_rate = sum(1 for r in rets if r > 0) / n * 100
        sorted_rets = sorted(rets)
        median = sorted_rets[n // 2]
        max_loss = min(rets)

        # 逐年
        yearly = defaultdict(list)
        # 需要year信息，但这里只有returns，需要外部传入
        stats[exit_label] = {
            'n': n,
            'avg': avg,
            'win_rate': win_rate,
            'median': median,
            'max_loss': max_loss,
        }
    return stats


def main():
    os.makedirs(os.path.dirname(LOG_PATH), exist_ok=True)
    log_file = open(LOG_PATH, 'w', encoding='utf-8')

    def lp(msg="", end='\n'):
        print(msg, end=end)
        log_file.write(msg + end)
        log_file.flush()

    lp("=" * 100)
    lp("T+1 可执行真实收益研究")
    lp(f"执行时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    lp(f"数据区间: {START_DATE} ~ {END_DATE}")
    lp("=" * 100)
    lp()
    lp("买入价 = day_T hour1_open")
    lp("卖出选项:")
    lp("  A. T+1 hour1_open (隔夜最快退出)")
    lp("  B. T+1 hour2_open (持有到次日10点)")
    lp("  C. T+1 hour4_close (持有到次日收盘)")
    lp("  D. T+2 hour1_open (持有2夜)")
    lp("  E. T+2 hour4_close (持有2天收盘)")
    lp("  F. T+3 hour1_open (持有3夜)")
    lp()

    # 加载数据
    loader = DataLoader(DB_PATH)
    loader.load()
    lp()

    # 扫描三大策略
    strategies = {}

    lp("=" * 100)
    lp("扫描策略1: MA5缩量突破(>700亿)")
    lp("=" * 100)
    strategies['MA5缩量突破'] = scan_strategy_ma5(loader)

    lp()
    lp("=" * 100)
    lp("扫描策略2: 换手率突增5x+(50-200亿)")
    lp("=" * 100)
    strategies['换手率突增5x'] = scan_strategy_turnover_surge(loader)

    lp()
    lp("=" * 100)
    lp("扫描策略3: 大阴高开(创业板<50亿)")
    lp("=" * 100)
    strategies['大阴高开'] = scan_strategy_bigdrop_gapup(loader)

    lp()

    # 对每个策略计算卖出收益
    EXIT_LABELS = ['A', 'B', 'C', 'D', 'E', 'F']
    EXIT_NAMES = {
        'A': 'T+1 h1_open',
        'B': 'T+1 h2_open',
        'C': 'T+1 h4_close',
        'D': 'T+2 h1_open',
        'E': 'T+2 h4_close',
        'F': 'T+3 h1_open',
    }

    all_results = {}  # strategy_name -> {exit_label -> [{ret, year}, ...]}

    for strat_name, signals in strategies.items():
        lp(f"\n计算 [{strat_name}] 的T+1可执行收益 (共{len(signals)}信号)...")
        exit_returns = {label: [] for label in EXIT_LABELS}  # label -> [(ret, year), ...]

        for i, sig in enumerate(signals):
            rets = calc_sell_returns(loader, sig['code'], sig['date'], sig['buy_price'])
            for label in EXIT_LABELS:
                r = rets.get(label)
                exit_returns[label].append({'ret': r, 'year': sig['year']})

            if (i + 1) % 1000 == 0:
                print(f"    计算进度: {i+1}/{len(signals)}")

        all_results[strat_name] = exit_returns
        lp(f"  [{strat_name}] 收益计算完成")

    # ======= 输出大对比表 =======
    lp(f"\n\n{'='*100}")
    lp("综合对比表：三大策略 × 六种卖出时机")
    lp(f"{'='*100}")
    lp()

    header = f"{'策略':<14}| {'卖出时机':<14}| {'6年样本':<8}| {'平均收益':<10}| {'中位数':<9}| {'胜率':<8}| {'最大亏损':<10}| {'最差年':<9}| {'最好年':<9}"
    lp(header)
    lp("-" * 106)

    summary_rows = []

    for strat_name in ['MA5缩量突破', '换手率突增5x', '大阴高开']:
        exit_returns = all_results[strat_name]

        for label in EXIT_LABELS:
            data = exit_returns[label]
            valid = [(d['ret'], d['year']) for d in data if d['ret'] is not None]

            if not valid:
                lp(f"{strat_name:<14}| {EXIT_NAMES[label]:<14}| {'0':<8}| {'N/A':<10}| {'N/A':<9}| {'N/A':<8}| {'N/A':<10}| {'N/A':<9}| {'N/A':<9}")
                continue

            rets = [v[0] for v in valid]
            years_data = defaultdict(list)
            for r, y in valid:
                years_data[y].append(r)

            n = len(rets)
            avg = sum(rets) / n
            sorted_rets = sorted(rets)
            median = sorted_rets[n // 2]
            win_rate = sum(1 for r in rets if r > 0) / n * 100
            max_loss = min(rets)

            # 逐年平均
            year_avgs = {}
            for y, yr_rets in years_data.items():
                year_avgs[y] = sum(yr_rets) / len(yr_rets)

            worst_year = min(year_avgs.values()) if year_avgs else 0
            best_year = max(year_avgs.values()) if year_avgs else 0

            lp(f"{strat_name:<14}| {EXIT_NAMES[label]:<14}| {n:<8}| {avg:+.3f}%{'':>3}| {median:+.3f}%{'':>2}| {win_rate:.1f}%{'':>2}| {max_loss:+.2f}%{'':>2}| {worst_year:+.2f}%{'':>2}| {best_year:+.2f}%")

            summary_rows.append({
                'strat': strat_name,
                'exit': EXIT_NAMES[label],
                'n': n, 'avg': avg, 'median': median,
                'win_rate': win_rate, 'max_loss': max_loss,
                'worst_year': worst_year, 'best_year': best_year,
                'year_avgs': year_avgs,
            })

        lp("-" * 106)

    # ======= 逐年详情 =======
    lp(f"\n\n{'='*100}")
    lp("逐年详细表现")
    lp(f"{'='*100}")

    years = ['2021', '2022', '2023', '2024', '2025', '2026']

    for strat_name in ['MA5缩量突破', '换手率突增5x', '大阴高开']:
        lp(f"\n--- {strat_name} ---")
        lp(f"{'卖出时机':<14}| ", end='')
        for y in years:
            lp(f"{y:>12}", end='')
        lp()
        lp("-" * 86)

        exit_returns = all_results[strat_name]
        for label in EXIT_LABELS:
            data = exit_returns[label]
            valid = [(d['ret'], d['year']) for d in data if d['ret'] is not None]
            years_data = defaultdict(list)
            for r, y in valid:
                years_data[y].append(r)

            line = f"{EXIT_NAMES[label]:<14}| "
            for y in years:
                yr_rets = years_data.get(y, [])
                if yr_rets:
                    yr_avg = sum(yr_rets) / len(yr_rets)
                    yr_wr = sum(1 for r in yr_rets if r > 0) / len(yr_rets) * 100
                    line += f"{yr_avg:+.2f}/{yr_wr:.0f}%  "
                else:
                    line += f"{'N/A':>12}"
            lp(line)

    # ======= 达标判断 =======
    lp(f"\n\n{'='*100}")
    lp("达标筛选: 平均收益 > 0.5% 且 胜率 > 55%")
    lp(f"{'='*100}")

    qualified = [r for r in summary_rows if r['avg'] > 0.5 and r['win_rate'] > 55]
    qualified.sort(key=lambda x: x['avg'] * x['win_rate'] / 100, reverse=True)

    if qualified:
        lp(f"\n达标组合 {len(qualified)} 个:")
        lp(f"{'排名':<4}| {'策略':<14}| {'卖出时机':<14}| {'样本':<7}| {'平均收益':<10}| {'胜率':<8}| {'综合分':<8}")
        lp("-" * 75)
        for i, r in enumerate(qualified, 1):
            score = r['avg'] * r['win_rate'] / 100
            lp(f"{i:<4}| {r['strat']:<14}| {r['exit']:<14}| {r['n']:<7}| {r['avg']:+.3f}%{'':>3}| {r['win_rate']:.1f}%{'':>2}| {score:.4f}")
    else:
        lp("\n未找到达标组合（平均收益>0.5%且胜率>55%）")
        lp("\n放宽条件查看（平均收益>0.3%或胜率>52%中最优者）:")
        near = [r for r in summary_rows if r['avg'] > 0.3 or r['win_rate'] > 52]
        near.sort(key=lambda x: x['avg'] * max(x['win_rate'], 50) / 100, reverse=True)
        for i, r in enumerate(near[:10], 1):
            score = r['avg'] * r['win_rate'] / 100
            lp(f"  {i}. [{r['strat']}] {r['exit']}: avg={r['avg']:+.3f}%, wr={r['win_rate']:.1f}%, n={r['n']}, score={score:.4f}")

    # ======= 最终结论 =======
    lp(f"\n\n{'='*100}")
    lp("最终结论")
    lp(f"{'='*100}")

    if qualified:
        best = qualified[0]
        lp(f"\n最优可执行策略+卖出组合:")
        lp(f"  策略: {best['strat']}")
        lp(f"  卖出时机: {best['exit']}")
        lp(f"  6年样本: {best['n']}")
        lp(f"  平均收益: {best['avg']:+.3f}%")
        lp(f"  中位数: {best['median']:+.3f}%")
        lp(f"  胜率: {best['win_rate']:.1f}%")
        lp(f"  最大亏损: {best['max_loss']:+.2f}%")
        lp(f"  最差年: {best['worst_year']:+.2f}%")
        lp(f"  最好年: {best['best_year']:+.2f}%")
        lp(f"\n逐年表现:")
        for y in years:
            if y in best['year_avgs']:
                lp(f"    {y}: {best['year_avgs'][y]:+.3f}%")
    else:
        lp("\n结论: 三大策略在T+1约束下，没有组合能同时达到平均收益>0.5%且胜率>55%的标准。")
        lp("这证实了之前研究中的'日内收益'数字确实不可执行。")
        # 列出所有策略×卖出最好的结果
        if summary_rows:
            best_overall = max(summary_rows, key=lambda x: x['avg'] * max(x['win_rate'], 40) / 100)
            lp(f"\n参考-最接近的组合:")
            lp(f"  策略: {best_overall['strat']}")
            lp(f"  卖出: {best_overall['exit']}")
            lp(f"  平均收益: {best_overall['avg']:+.3f}%")
            lp(f"  胜率: {best_overall['win_rate']:.1f}%")

    lp(f"\n\n{'='*100}")
    lp(f"完成。日志: {LOG_PATH}")
    log_file.close()


if __name__ == '__main__':
    main()
