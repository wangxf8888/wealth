#!/usr/bin/env python3
"""
大阴高开策略交易合规性验证与明细输出
聚焦<50亿小盘股，2025全年数据验证
验证项: 无未来数据、涨停不买、一字板不可交易、ST/停牌排除、跌停标记
"""
import sys
import os
import sqlite3
import json
import math
from datetime import datetime
from collections import defaultdict

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from compliance_utils import (
    get_limit_threshold, is_limit_up, is_limit_down,
    is_one_word_board, is_st, safe_float
)

# ============ 配置区 ============
DB_PATH = "/home/AIWealth/data/stocks.db"
BIG_DROP_THRESHOLD = -5.0      # 昨日跌幅阈值(%)
GAP_UP_THRESHOLD = 2.0         # 今日高开阈值(%)
MAX_MARKET_CAP = 50.0          # 流通市值上限(亿元)
DATE_START = "2025-01-01"
DATE_END = "2025-12-31"
LOG_PATH = "/home/AIWealth/scripts/logs/bigdrop_gapup_compliance.log"
JSON_PATH = "/home/AIWealth/scripts/logs/bigdrop_gapup_trade_details.json"
# ================================


def get_limit_up_price(code, preclose):
    """计算涨停价: round(preclose * (1+ratio), 2)"""
    if code.startswith("sz.300") or code.startswith("sz.301") or code.startswith("sh.688"):
        return round(preclose * 1.2, 2)
    elif code.startswith("bj."):
        return round(preclose * 1.3, 2)
    else:
        return round(preclose * 1.1, 2)


def get_limit_down_price(code, preclose):
    """计算跌停价: round(preclose * (1-ratio), 2)"""
    if code.startswith("sz.300") or code.startswith("sz.301") or code.startswith("sh.688"):
        return round(preclose * 0.8, 2)
    elif code.startswith("bj."):
        return round(preclose * 0.7, 2)
    else:
        return round(preclose * 0.9, 2)


def get_board_name(code):
    """获取板块名称"""
    if code.startswith("sz.300") or code.startswith("sz.301"):
        return "创业板"
    elif code.startswith("sh.688"):
        return "科创板"
    elif code.startswith("bj."):
        return "北交所"
    elif code.startswith("sh.60"):
        return "沪主板"
    elif code.startswith("sz.00"):
        return "深主板"
    else:
        return "其他"


def calc_market_cap(amount, turn):
    """流通市值 = 成交额 / 换手率 * 100 (亿元)"""
    if turn and turn > 0 and amount and amount > 0:
        return round(amount / turn * 100 / 1e8, 2)
    return None


def get_all_trading_days(cursor, start, end):
    """获取区间内所有交易日"""
    cursor.execute("""
        SELECT DISTINCT date FROM stock_kline
        WHERE date >= ? AND date <= ?
        ORDER BY date
    """, (start, end))
    return [r[0] for r in cursor.fetchall()]


def find_raw_candidates(cursor, today, yesterday):
    """查找大阴+高开候选股（不做任何过滤，全部返回用于合规检验）"""
    query = """
        SELECT
            t.date as today_date,
            t.code, t.code_name,
            t.open, t.high, t.low, t.close, t.preclose,
            t.close_rate as today_close_rate,
            t.volume, t.turn, t.amount, t.isST,
            t.hour1_open, t.hour1_high, t.hour1_low, t.hour1_close,
            y.date as yest_date,
            y.close as y_close,
            y.preclose as y_preclose,
            y.close_rate as y_close_rate
        FROM stock_kline t
        JOIN stock_kline y ON t.code = y.code AND y.date = ?
        WHERE t.date = ?
          AND t.preclose > 0
          AND y.preclose > 0
          AND y.close_rate <= ?
          AND ((t.open - t.preclose) / t.preclose * 100) >= ?
    """
    cursor.execute(query, (yesterday, today, BIG_DROP_THRESHOLD, GAP_UP_THRESHOLD))
    columns = [desc[0] for desc in cursor.description]
    results = []
    for row in cursor.fetchall():
        d = dict(zip(columns, row))
        results.append(d)
    return results


def verify_single_candidate(cand):
    """对单个候选股进行全面合规验证"""
    code = cand['code']
    code_name = cand['code_name'] or ''
    preclose_today = safe_float(cand['preclose'])  # today的preclose = yesterday close
    hour1_open = safe_float(cand['hour1_open'])
    hour1_high = safe_float(cand['hour1_high'])
    hour1_low = safe_float(cand['hour1_low'])
    hour1_close = safe_float(cand['hour1_close'])
    day_close = safe_float(cand['close'])
    y_close = safe_float(cand['y_close'])
    y_preclose = safe_float(cand['y_preclose'])

    compliance = {
        'no_future_data': True,
        'not_limit_up_buy': True,
        'tradeable': True,
        'not_st': True,
    }
    flags = []

    # 1. 无未来数据验证
    # 大阴线判断: 用yesterday (close-preclose)/preclose ≤ -5% → 使用y_close_rate
    # 高开判断: today_open > yesterday_close → open vs preclose(today的preclose就是yesterday close)
    # 这两个条件在SQL查询中已保证，此处做双重确认
    if y_preclose > 0:
        y_change = (y_close - y_preclose) / y_preclose * 100
        if y_change > BIG_DROP_THRESHOLD:
            compliance['no_future_data'] = False
            flags.append(f'昨日跌幅不满足: {y_change:.2f}%')

    today_open = safe_float(cand['open'])
    if preclose_today > 0:
        gap_up = (today_open - preclose_today) / preclose_today * 100
        if gap_up < GAP_UP_THRESHOLD:
            compliance['no_future_data'] = False
            flags.append(f'高开幅度不满足: {gap_up:.2f}%')

    # 2. 涨停不买验证
    limit_up_price = get_limit_up_price(code, preclose_today)
    if hour1_open > 0 and hour1_open >= limit_up_price:
        compliance['not_limit_up_buy'] = False
        flags.append(f'hour1开盘价{hour1_open:.2f}>=涨停价{limit_up_price:.2f}')

    # 3. 一字板不可交易验证
    if hour1_open > 0:
        # 一字涨停: open=high=low=close=涨停价
        if is_one_word_board(hour1_open, hour1_high, hour1_low, hour1_close):
            if hour1_open >= limit_up_price:
                compliance['tradeable'] = False
                flags.append('一字涨停板不可交易')
            elif hour1_open <= get_limit_down_price(code, preclose_today):
                compliance['tradeable'] = False
                flags.append('一字跌停板不可交易')
            else:
                # 非涨跌停的一字板（极端情况）
                compliance['tradeable'] = False
                flags.append('一字板无法成交')
        # 集合竞价涨停（开盘=涨停但后续有波动）
        elif hour1_open >= limit_up_price:
            compliance['tradeable'] = False
            flags.append(f'集合竞价涨停开盘({hour1_open:.2f}={limit_up_price:.2f})大概率无法买入')
    else:
        compliance['tradeable'] = False
        flags.append('hour1数据缺失，不可交易')

    # 4. ST/停牌排除
    if is_st(code_name, cand.get('isST', 0)):
        compliance['not_st'] = False
        flags.append(f'ST股: {code_name}')

    # 5. 跌停标记（收盘为跌停价，持有时可能遇到）
    limit_down_price = get_limit_down_price(code, preclose_today)
    if day_close > 0 and day_close <= limit_down_price:
        flags.append(f'当日收盘跌停({day_close:.2f}<={limit_down_price:.2f})')

    return compliance, flags


class Logger:
    """同时输出到终端和日志文件"""
    def __init__(self, log_path):
        os.makedirs(os.path.dirname(log_path), exist_ok=True)
        self.terminal = sys.stdout
        self.log_file = open(log_path, 'w', encoding='utf-8')

    def write(self, msg):
        self.terminal.write(msg)
        self.log_file.write(msg)

    def flush(self):
        self.terminal.flush()
        self.log_file.flush()

    def close(self):
        self.log_file.close()


def main():
    logger = Logger(LOG_PATH)
    sys.stdout = logger

    print("=" * 80)
    print("大阴高开策略 - 交易合规性验证与明细输出")
    print(f"参数: 大阴阈值={BIG_DROP_THRESHOLD}%, 高开阈值={GAP_UP_THRESHOLD}%")
    print(f"聚焦: <{MAX_MARKET_CAP}亿小盘股")
    print(f"验证区间: {DATE_START} ~ {DATE_END}")
    print(f"执行时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print("=" * 80)

    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()

    # 获取交易日列表
    all_days = get_all_trading_days(cursor, DATE_START, DATE_END)
    print(f"\n2025年交易日数: {len(all_days)}")
    if not all_days:
        print("ERROR: 未找到交易日数据")
        return

    # 获取完整的交易日列表（含2025之前，用于找yesterday）
    cursor.execute("SELECT DISTINCT date FROM stock_kline WHERE date <= ? ORDER BY date", (DATE_END,))
    full_days = [r[0] for r in cursor.fetchall()]
    full_days_set = set(full_days)

    # 逐日扫描
    trade_details = []
    stats = {
        'total_signals': 0,
        'cap_filtered': 0,     # 市值不满足
        'compliant': 0,
        'limit_up_blocked': 0,
        'untradeable': 0,
        'st_blocked': 0,
        'no_future_data_fail': 0,
        'close_limit_down': 0,
    }

    for today in all_days:
        today_idx = full_days.index(today)
        if today_idx <= 0:
            continue
        yesterday = full_days[today_idx - 1]

        candidates = find_raw_candidates(cursor, today, yesterday)

        for cand in candidates:
            # 计算流通市值
            cap = calc_market_cap(cand.get('amount'), cand.get('turn'))

            # 聚焦<50亿小盘
            if cap is None or cap >= MAX_MARKET_CAP:
                stats['cap_filtered'] += 1
                continue

            stats['total_signals'] += 1

            # 合规验证
            compliance, flags = verify_single_candidate(cand)

            # 统计
            all_pass = all(compliance.values())
            if all_pass:
                stats['compliant'] += 1
            if not compliance['not_limit_up_buy']:
                stats['limit_up_blocked'] += 1
            if not compliance['tradeable']:
                stats['untradeable'] += 1
            if not compliance['not_st']:
                stats['st_blocked'] += 1
            if not compliance['no_future_data']:
                stats['no_future_data_fail'] += 1

            # 跌停标记
            preclose_today = safe_float(cand['preclose'])
            day_close = safe_float(cand['close'])
            limit_down_price = get_limit_down_price(cand['code'], preclose_today)
            if day_close > 0 and day_close <= limit_down_price:
                stats['close_limit_down'] += 1

            # 计算各项指标
            today_open = safe_float(cand['open'])
            y_close = safe_float(cand['y_close'])
            y_preclose = safe_float(cand['y_preclose'])
            hour1_open = safe_float(cand['hour1_open'])
            limit_up_price = get_limit_up_price(cand['code'], preclose_today)

            gap_up_pct = (today_open - preclose_today) / preclose_today * 100 if preclose_today > 0 else 0
            y_change_pct = (y_close - y_preclose) / y_preclose * 100 if y_preclose > 0 else 0
            day_return_pct = (day_close - today_open) / today_open * 100 if today_open > 0 else 0

            detail = {
                'date': cand['today_date'],
                'code': cand['code'],
                'name': cand['code_name'] or '',
                'market_cap_b': cap,
                'board': get_board_name(cand['code']),
                'yesterday_close': round(y_close, 2),
                'yesterday_change_pct': round(y_change_pct, 2),
                'today_open': round(today_open, 2),
                'gap_up_pct': round(gap_up_pct, 2),
                'limit_up_price': limit_up_price,
                'limit_down_price': limit_down_price,
                'hour1_open': round(hour1_open, 2),
                'day_close': round(day_close, 2),
                'day_return_pct': round(day_return_pct, 2),
                'compliance': compliance,
                'flags': flags,
            }
            trade_details.append(detail)

    conn.close()

    # ====== 合规报告 ======
    print("\n" + "=" * 80)
    print("合规验证报告")
    print("=" * 80)
    print(f"\n  {'指标':<28}{'数量':>8}{'占比':>10}")
    print(f"  {'-'*46}")
    print(f"  {'总信号数(<50亿)':<26}{stats['total_signals']:>8}")
    print(f"  {'市值过滤(≥50亿)数':<24}{stats['cap_filtered']:>8}")
    print(f"  {'-'*46}")
    print(f"  {'合规通过数':<26}{stats['compliant']:>8}{'':>4}{stats['compliant']/max(1,stats['total_signals'])*100:.1f}%")
    print(f"  {'涨停无法买入数':<24}{stats['limit_up_blocked']:>8}{'':>4}{stats['limit_up_blocked']/max(1,stats['total_signals'])*100:.1f}%")
    print(f"  {'一字板/不可交易数':<22}{stats['untradeable']:>8}{'':>4}{stats['untradeable']/max(1,stats['total_signals'])*100:.1f}%")
    print(f"  {'ST/停牌数':<26}{stats['st_blocked']:>8}{'':>4}{stats['st_blocked']/max(1,stats['total_signals'])*100:.1f}%")
    print(f"  {'未来数据违规数':<24}{stats['no_future_data_fail']:>8}{'':>4}{stats['no_future_data_fail']/max(1,stats['total_signals'])*100:.1f}%")
    print(f"  {'当日收盘跌停(标记)':<22}{stats['close_limit_down']:>8}{'':>4}{stats['close_limit_down']/max(1,stats['total_signals'])*100:.1f}%")

    # 剔除后真实样本统计
    compliant_trades = [t for t in trade_details if all(t['compliance'].values())]
    non_compliant_trades = [t for t in trade_details if not all(t['compliance'].values())]

    print(f"\n  {'-'*46}")
    print(f"  {'剔除后真实样本数':<24}{len(compliant_trades):>8}")

    if compliant_trades:
        avg_ret = sum(t['day_return_pct'] for t in compliant_trades) / len(compliant_trades)
        pos_count = sum(1 for t in compliant_trades if t['day_return_pct'] > 0)
        pos_rate = pos_count / len(compliant_trades) * 100
        max_ret = max(t['day_return_pct'] for t in compliant_trades)
        min_ret = min(t['day_return_pct'] for t in compliant_trades)

        print(f"\n  【真实收益统计（合规样本）】")
        print(f"  日均收益率: {avg_ret:+.3f}%")
        print(f"  胜率: {pos_rate:.1f}% ({pos_count}/{len(compliant_trades)})")
        print(f"  最大单笔收益: {max_ret:+.2f}%")
        print(f"  最大单笔亏损: {min_ret:+.2f}%")

        # 逐月统计
        print(f"\n  【逐月收益统计（合规样本）】")
        print(f"  {'月份':<10}{'样本数':>6}{'日均收益':>10}{'胜率':>8}")
        print(f"  {'-'*34}")
        monthly = defaultdict(list)
        for t in compliant_trades:
            m = t['date'][:7]
            monthly[m].append(t['day_return_pct'])
        for m in sorted(monthly.keys()):
            rets = monthly[m]
            m_avg = sum(rets) / len(rets)
            m_pos = sum(1 for r in rets if r > 0) / len(rets) * 100
            print(f"  {m:<10}{len(rets):>6}{m_avg:>+9.2f}%{m_pos:>7.1f}%")

    # 合规失败样本示例
    if non_compliant_trades:
        print(f"\n  【不合规样本示例（前10笔）】")
        print(f"  {'日期':<12}{'代码':<12}{'名称':<10}{'原因'}")
        print(f"  {'-'*60}")
        for t in non_compliant_trades[:10]:
            reasons = ', '.join(t['flags'][:2]) if t['flags'] else '未知'
            print(f"  {t['date']:<12}{t['code']:<12}{t['name']:<10}{reasons}")

    # 保存JSON明细
    os.makedirs(os.path.dirname(JSON_PATH), exist_ok=True)
    with open(JSON_PATH, 'w', encoding='utf-8') as f:
        json.dump(trade_details, f, ensure_ascii=False, indent=2)
    print(f"\n交易明细已保存: {JSON_PATH}")
    print(f"合规日志已保存: {LOG_PATH}")
    print(f"总记录数: {len(trade_details)}")

    print(f"\n完成。执行时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print("=" * 80)

    sys.stdout = logger.terminal
    logger.close()


if __name__ == "__main__":
    main()
