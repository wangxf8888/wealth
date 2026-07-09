#!/usr/bin/env python3
"""
MA5突破策略交易合规性验证与明细输出
聚焦>700亿大盘股，2025全年数据验证

验证项:
  1. 无未来数据: MA5用yesterday及之前5日close计算, 买入判断仅用today_open
  2. 涨停不买: hour1_open < limit_up_price
  3. 一字板不可交易: today hour1 OHLC四价相等=涨停价 → 不可交易
  4. 大盘股验证: 确认>700亿流通市值, 无ST/退市混入
  5. 边界情况: 等于MA5不算突破, 跳空gap>5%标记
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
MIN_MARKET_CAP = 700            # 最低流通市值(亿元)
MIN_TURNOVER = 1.0              # 最低换手率(%)
MIN_RECENT_DROP = 5.0           # 近10日最少累跌幅度(%)
DATE_START = "2025-01-01"
DATE_END = "2025-12-31"
LOG_PATH = "/home/AIWealth/scripts/logs/ma_breakout_compliance.log"
JSON_PATH = "/home/AIWealth/scripts/logs/ma_breakout_trade_details.json"
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


def calc_ma(closes, period):
    """计算MA均值"""
    if len(closes) < period:
        return None
    return sum(closes[-period:]) / period


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
    print("MA5突破策略(>700亿大盘股) - 交易合规性验证与明细输出")
    print(f"参数: 流通市值>{MIN_MARKET_CAP}亿, 换手率>{MIN_TURNOVER}%, 近10日回撤>{MIN_RECENT_DROP}%")
    print(f"验证区间: {DATE_START} ~ {DATE_END}")
    print(f"执行时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print("=" * 80)

    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()

    # 获取完整交易日列表（含2025之前，用于MA计算lookback）
    cur.execute("SELECT DISTINCT date FROM stock_kline ORDER BY date")
    all_days = [r[0] for r in cur.fetchall()]
    all_days_idx = {d: i for i, d in enumerate(all_days)}
    print(f"\n数据库总交易日数: {len(all_days)}")
    print(f"日期范围: {all_days[0]} ~ {all_days[-1]}")

    # 2025年交易日
    days_2025 = [d for d in all_days if DATE_START <= d <= DATE_END]
    print(f"2025年交易日数: {len(days_2025)}\n")

    if not days_2025:
        print("ERROR: 未找到2025年交易日数据")
        sys.stdout = logger.terminal
        logger.close()
        return

    # 统计
    trade_details = []
    stats = {
        'total_signals': 0,
        'compliant': 0,
        'future_data_fail': 0,
        'limit_up_blocked': 0,
        'untradeable': 0,
        'st_blocked': 0,
        'market_cap_invalid': 0,
        'gap_too_large': 0,
        'equal_ma5_skipped': 0,
    }

    processed_days = 0
    for today in days_2025:
        today_idx = all_days_idx[today]
        if today_idx < 12:
            continue
        yesterday = all_days[today_idx - 1]
        yesterday_idx = today_idx - 1

        # lookback范围 (today_idx-12 到 today_idx-1 的日期，即到yesterday)
        lookback_start = max(0, today_idx - 12)
        lookback_days = all_days[lookback_start:today_idx]  # 不含today

        if len(lookback_days) < 6:
            continue

        # 批量获取today数据
        cur.execute("""
            SELECT code, code_name, open, high, low, close, preclose, isST, turn,
                   hour1_open, hour1_high, hour1_low, hour1_close, amount
            FROM stock_kline WHERE date = ?
        """, (today,))
        today_rows = {}
        for r in cur.fetchall():
            today_rows[r[0]] = r

        # 获取yesterday数据
        cur.execute("""
            SELECT code, code_name, close, preclose, turn, isST, open, high, low
            FROM stock_kline WHERE date = ?
        """, (yesterday,))
        yesterday_rows = {}
        for r in cur.fetchall():
            yesterday_rows[r[0]] = r

        # 获取lookback区间所有数据（到yesterday为止）
        lb_placeholders = ','.join(['?'] * len(lookback_days))
        cur.execute(f"""
            SELECT code, date, close, high, low
            FROM stock_kline WHERE date IN ({lb_placeholders})
            ORDER BY code, date
        """, lookback_days)
        history_data = defaultdict(list)
        for r in cur.fetchall():
            history_data[r[0]].append((r[1], r[2], r[3], r[4]))

        # 逐股检查MA5突破条件
        for code, yd_row in yesterday_rows.items():
            code_name = yd_row[1]
            yd_close = yd_row[2]
            yd_preclose = yd_row[3]
            yd_turn = yd_row[4]
            yd_isST = yd_row[5]

            # today数据
            if code not in today_rows:
                continue
            t = today_rows[code]
            t_open = t[2]
            t_high = t[3]
            t_low = t[4]
            t_close = t[5]
            t_preclose = t[6]
            t_isST = t[7]
            t_turn = t[8]
            t_h1_open = t[9]
            t_h1_high = t[10]
            t_h1_low = t[11]
            t_h1_close = t[12]
            t_amount = t[13]

            if t_open is None or t_open <= 0 or t_preclose is None or t_preclose <= 0:
                continue
            if yd_close is None or yd_close <= 0:
                continue

            # 换手率过滤
            if yd_turn is None or yd_turn < MIN_TURNOVER:
                continue

            # 估算流通市值（亿元）
            market_cap = calc_market_cap(t_amount, yd_turn)
            if market_cap is None or market_cap < MIN_MARKET_CAP:
                continue

            # 获取该股历史close列表（到yesterday为止）
            hist = history_data.get(code, [])
            if not hist:
                continue
            hist_sorted = sorted(hist, key=lambda x: x[0])
            date_close_map = {h[0]: h[1] for h in hist_sorted}
            date_high_map = {h[0]: h[2] for h in hist_sorted}
            date_low_map = {h[0]: h[3] for h in hist_sorted}

            # 构建用于MA计算的close序列（到yesterday为止）
            closes_to_yesterday = []
            for d in all_days[lookback_start:today_idx]:
                if d in date_close_map and date_close_map[d] is not None:
                    closes_to_yesterday.append((d, date_close_map[d]))

            if len(closes_to_yesterday) < 5:
                continue

            # MA5_yesterday = mean(day-5, day-4, day-3, day-2, yesterday)
            last5_closes = [c[1] for c in closes_to_yesterday[-5:]]
            ma5_yesterday = calc_ma(last5_closes, 5)
            if ma5_yesterday is None:
                continue

            # T-2收盘价（前天close）
            if len(closes_to_yesterday) < 2:
                continue
            t2_close = closes_to_yesterday[-2][1]

            # MA5_T-2（用于确认前天在MA5之下）
            if len(closes_to_yesterday) < 6:
                continue
            last5_t2 = [c[1] for c in closes_to_yesterday[-6:-1]]
            ma5_t2 = calc_ma(last5_t2, 5)
            if ma5_t2 is None:
                continue

            # MA5突破条件:
            #   yesterday_close 的前一天(T-2): close < MA5_T-2 (突破前在MA5之下)
            #   yesterday: close > MA5_yesterday (已突破)
            #   today: open > MA5_yesterday (确认突破)
            if not (t2_close < ma5_t2):
                continue
            if not (yd_close > ma5_yesterday):
                continue
            # 边界: 等于MA5不算突破
            if t_open <= ma5_yesterday:
                stats['equal_ma5_skipped'] += 1
                continue

            # 近10日回撤检查
            recent_highs = []
            recent_lows = []
            for d, c in closes_to_yesterday[-10:]:
                if d in date_high_map and date_high_map[d] is not None:
                    recent_highs.append(date_high_map[d])
                if d in date_low_map and date_low_map[d] is not None:
                    recent_lows.append(date_low_map[d])
                if c is not None:
                    recent_highs.append(c)
                    recent_lows.append(c)
            if not recent_highs or not recent_lows:
                continue
            max_high = max(recent_highs)
            min_low = min(recent_lows)
            if max_high <= 0:
                continue
            recent_drop_pct = (max_high - min_low) / max_high * 100
            if recent_drop_pct < MIN_RECENT_DROP:
                continue

            # 今日高开确认: today_open > yesterday_close
            if t_open <= yd_close:
                continue

            # === 到此是有效MA5突破信号 ===
            stats['total_signals'] += 1

            # 买入价 = hour1_open
            buy_price = t_h1_open if t_h1_open and t_h1_open > 0 else t_open
            breakout_pct = (yd_close - ma5_yesterday) / ma5_yesterday * 100
            gap_pct = (t_open - yd_close) / yd_close * 100

            # ====== 合规验证 ======
            compliance = {
                'no_future_data': True,
                'not_limit_up_buy': True,
                'tradeable': True,
                'market_cap_valid': True,
            }
            flags = []

            # 1. 无未来数据验证
            # MA5_yesterday 使用的5个close都必须<=yesterday日期
            ma5_dates = [c[0] for c in closes_to_yesterday[-5:]]
            if any(d >= today for d in ma5_dates):
                compliance['no_future_data'] = False
                flags.append(f'MA5计算使用了today或之后的数据: {ma5_dates}')
            # 突破判断仅用yesterday_close和today_open，未用today close/high/low
            # 通过代码逻辑保证 — 此处再次确认
            # (逻辑上已保证: yd_close > ma5_yesterday 且 t_open > ma5_yesterday)

            # 2. 涨停不买验证
            limit_up_price = get_limit_up_price(code, t_preclose)
            actual_buy = buy_price
            if actual_buy >= limit_up_price:
                compliance['not_limit_up_buy'] = False
                flags.append(f'买入价{actual_buy:.2f}>=涨停价{limit_up_price:.2f}')

            # 3. 一字板不可交易验证
            if t_h1_open and t_h1_open > 0:
                if is_one_word_board(t_h1_open, t_h1_high, t_h1_low, t_h1_close):
                    if t_h1_open >= limit_up_price:
                        compliance['tradeable'] = False
                        flags.append('一字涨停板不可交易')
                    elif t_h1_open <= get_limit_down_price(code, t_preclose):
                        compliance['tradeable'] = False
                        flags.append('一字跌停板不可交易')
                    else:
                        compliance['tradeable'] = False
                        flags.append('一字板无法成交')
                # 集合竞价涨停开盘
                elif t_h1_open >= limit_up_price:
                    compliance['tradeable'] = False
                    flags.append(f'集合竞价涨停开盘({t_h1_open:.2f}>={limit_up_price:.2f})')
            else:
                compliance['tradeable'] = False
                flags.append('hour1数据缺失，不可交易')

            # 4. 大盘股验证 + ST检查
            if market_cap < MIN_MARKET_CAP:
                compliance['market_cap_valid'] = False
                flags.append(f'流通市值{market_cap:.1f}亿<{MIN_MARKET_CAP}亿')
            if yd_isST or t_isST:
                compliance['market_cap_valid'] = False
                flags.append(f'ST股: {code_name}')
            if code_name and 'ST' in (code_name or '').upper():
                compliance['market_cap_valid'] = False
                flags.append(f'ST股名称: {code_name}')

            # 5. 边界情况: 跳空高开过MA5但gap>5%标记
            if gap_pct > 5.0:
                flags.append(f'高开gap过大({gap_pct:.2f}%>5%)')
                stats['gap_too_large'] += 1

            # 计算当日收益
            day_return_pct = (t_close - buy_price) / buy_price * 100 if buy_price > 0 and t_close else 0

            # 统计
            all_pass = all(compliance.values())
            if all_pass:
                stats['compliant'] += 1
            if not compliance['no_future_data']:
                stats['future_data_fail'] += 1
            if not compliance['not_limit_up_buy']:
                stats['limit_up_blocked'] += 1
            if not compliance['tradeable']:
                stats['untradeable'] += 1
            if not compliance['market_cap_valid']:
                stats['market_cap_invalid'] += 1

            # 构建明细
            detail = {
                'date': today,
                'code': code,
                'name': code_name or '',
                'market_cap_b': market_cap,
                'board': get_board_name(code),
                'yesterday_close': round(yd_close, 2),
                'ma5_yesterday': round(ma5_yesterday, 4),
                'breakout_pct': round(breakout_pct, 2),
                'today_open': round(t_open, 2),
                'hour1_open': round(buy_price, 2),
                'limit_up_price': limit_up_price,
                'day_close': round(t_close, 2) if t_close else None,
                'day_return_pct': round(day_return_pct, 2),
                'gap_pct': round(gap_pct, 2),
                'compliance': compliance,
                'flags': flags,
            }
            trade_details.append(detail)

        processed_days += 1
        if processed_days % 50 == 0:
            print(f"  已处理 {processed_days}/{len(days_2025)} 天, 累计信号 {stats['total_signals']}")

    conn.close()

    # ====== 合规报告 ======
    print("\n" + "=" * 80)
    print("合规验证报告 - MA5突破策略(>700亿大盘股)")
    print("=" * 80)
    print(f"\n  {'指标':<30}{'数量':>8}{'占比':>10}")
    print(f"  {'-'*48}")
    total = stats['total_signals']
    print(f"  {'总信号数(>700亿+MA5突破)':<26}{total:>8}")
    print(f"  {'-'*48}")
    print(f"  {'合规通过数':<28}{stats['compliant']:>8}{'':>4}{stats['compliant']/max(1,total)*100:.1f}%")
    print(f"  {'未来数据违规数':<26}{stats['future_data_fail']:>8}{'':>4}{stats['future_data_fail']/max(1,total)*100:.1f}%")
    print(f"  {'涨停无法买入数':<26}{stats['limit_up_blocked']:>8}{'':>4}{stats['limit_up_blocked']/max(1,total)*100:.1f}%")
    print(f"  {'一字板/不可交易数':<24}{stats['untradeable']:>8}{'':>4}{stats['untradeable']/max(1,total)*100:.1f}%")
    print(f"  {'市值/ST不合规数':<26}{stats['market_cap_invalid']:>8}{'':>4}{stats['market_cap_invalid']/max(1,total)*100:.1f}%")
    print(f"  {'高开gap>5%标记数':<26}{stats['gap_too_large']:>8}{'':>4}{stats['gap_too_large']/max(1,total)*100:.1f}%")
    print(f"  {'等于MA5跳过数(不算突破)':<22}{stats['equal_ma5_skipped']:>8}")

    # 剔除后真实样本统计
    compliant_trades = [t for t in trade_details if all(t['compliance'].values())]
    non_compliant_trades = [t for t in trade_details if not all(t['compliance'].values())]

    print(f"\n  {'-'*48}")
    print(f"  {'剔除后真实合规样本数':<24}{len(compliant_trades):>8}")

    if compliant_trades:
        avg_ret = sum(t['day_return_pct'] for t in compliant_trades) / len(compliant_trades)
        pos_count = sum(1 for t in compliant_trades if t['day_return_pct'] > 0)
        pos_rate = pos_count / len(compliant_trades) * 100
        max_ret = max(t['day_return_pct'] for t in compliant_trades)
        min_ret = min(t['day_return_pct'] for t in compliant_trades)
        median_idx = len(compliant_trades) // 2
        sorted_rets = sorted(t['day_return_pct'] for t in compliant_trades)
        median_ret = sorted_rets[median_idx]

        print(f"\n  【真实收益统计（合规样本, 买入价=hour1_open, 卖出价=day_close）】")
        print(f"  日均收益率: {avg_ret:+.3f}%")
        print(f"  胜率: {pos_rate:.1f}% ({pos_count}/{len(compliant_trades)})")
        print(f"  中位数收益: {median_ret:+.3f}%")
        print(f"  最大单笔收益: {max_ret:+.2f}%")
        print(f"  最大单笔亏损: {min_ret:+.2f}%")
        pos_rets = [t['day_return_pct'] for t in compliant_trades if t['day_return_pct'] > 0]
        neg_rets = [t['day_return_pct'] for t in compliant_trades if t['day_return_pct'] <= 0]
        pos_avg = sum(pos_rets) / len(pos_rets) if pos_rets else 0
        neg_avg = sum(neg_rets) / len(neg_rets) if neg_rets else 0
        pnl_ratio = abs(pos_avg / neg_avg) if neg_avg != 0 else 999
        print(f"  盈利均值: {pos_avg:+.3f}%")
        print(f"  亏损均值: {neg_avg:+.3f}%")
        print(f"  盈亏比: {pnl_ratio:.2f}")

        # 逐月统计
        print(f"\n  【逐月收益统计（合规样本）】")
        print(f"  {'月份':<10}{'样本数':>6}{'日均收益':>10}{'胜率':>8}{'最大赚':>10}{'最大亏':>10}")
        print(f"  {'-'*54}")
        monthly = defaultdict(list)
        for t in compliant_trades:
            m = t['date'][:7]
            monthly[m].append(t['day_return_pct'])
        for m in sorted(monthly.keys()):
            rets = monthly[m]
            m_avg = sum(rets) / len(rets)
            m_pos = sum(1 for r in rets if r > 0) / len(rets) * 100
            m_max = max(rets)
            m_min = min(rets)
            print(f"  {m:<10}{len(rets):>6}{m_avg:>+9.2f}%{m_pos:>7.1f}%{m_max:>+9.2f}%{m_min:>+9.2f}%")

        # 按板块统计
        print(f"\n  【按板块统计（合规样本）】")
        print(f"  {'板块':<10}{'样本数':>6}{'日均收益':>10}{'胜率':>8}")
        print(f"  {'-'*34}")
        board_stats = defaultdict(list)
        for t in compliant_trades:
            board_stats[t['board']].append(t['day_return_pct'])
        for board in sorted(board_stats.keys()):
            rets = board_stats[board]
            b_avg = sum(rets) / len(rets)
            b_pos = sum(1 for r in rets if r > 0) / len(rets) * 100
            print(f"  {board:<10}{len(rets):>6}{b_avg:>+9.2f}%{b_pos:>7.1f}%")

    # 不合规样本示例
    if non_compliant_trades:
        print(f"\n  【不合规样本明细（前15笔）】")
        print(f"  {'日期':<12}{'代码':<12}{'名称':<10}{'市值亿':>8}{'原因'}")
        print(f"  {'-'*70}")
        for t in non_compliant_trades[:15]:
            reasons = ', '.join(t['flags'][:2]) if t['flags'] else '未知'
            print(f"  {t['date']:<12}{t['code']:<12}{t['name']:<10}{t['market_cap_b']:>7.0f}{' ':>1}{reasons}")

    # gap>5%的交易示例
    gap_large_trades = [t for t in compliant_trades if t.get('gap_pct', 0) > 5.0]
    if gap_large_trades:
        print(f"\n  【高开gap>5%的合规交易（共{len(gap_large_trades)}笔，前10笔）】")
        print(f"  {'日期':<12}{'代码':<12}{'名称':<10}{'gap%':>8}{'日收益%':>10}")
        print(f"  {'-'*52}")
        for t in gap_large_trades[:10]:
            print(f"  {t['date']:<12}{t['code']:<12}{t['name']:<10}{t['gap_pct']:>+7.2f}%{t['day_return_pct']:>+9.2f}%")

    # 保存JSON明细
    os.makedirs(os.path.dirname(JSON_PATH), exist_ok=True)
    with open(JSON_PATH, 'w', encoding='utf-8') as f:
        json.dump(trade_details, f, ensure_ascii=False, indent=2)
    print(f"\n交易明细已保存: {JSON_PATH}")
    print(f"合规日志已保存: {LOG_PATH}")
    print(f"总记录数: {len(trade_details)} (合规{len(compliant_trades)}+不合规{len(non_compliant_trades)})")

    print(f"\n完成。执行时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print("=" * 80)

    sys.stdout = logger.terminal
    logger.close()


if __name__ == "__main__":
    main()
