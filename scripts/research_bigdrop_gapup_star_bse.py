#!/usr/bin/env python3
"""
大阴高开策略在科创板和北交所的表现研究
============================================
研究目标：
  - 测试大阴高开策略在科创板(±20%)的效果
  - 北交所(±30%)数据如可用则一并测试
  - 这些板块涨跌幅限制更宽，大阴后反弹弹性可能更强

数据范围：2021-01-01 ~ 2026-06-30
板块1：科创板（代码sh.688开头，涨跌幅±20%）
板块2：北交所（代码bj.开头，涨跌幅±30%）

输出日志：/home/AIWealth/scripts/logs/bigdrop_gapup_star_bse.log
"""
import sys
import sqlite3
import os
from datetime import datetime
from collections import defaultdict

# ============ 配置区 ============
DB_PATH = "/home/AIWealth/data/stocks.db"
LOG_PATH = "/home/AIWealth/scripts/logs/bigdrop_gapup_star_bse.log"
DATE_START = "2021-01-01"
DATE_END = "2026-06-30"

# 科创板参数组合
STAR_DROP_THRESHOLDS = [-7, -10, -12, -15]    # 昨日跌幅阈值(%)
STAR_GAPUP_RANGES = [(1, 5), (2, 8)]          # 今日高开范围(%)
STAR_MCAP_LIMITS = [50, 100, 200]             # 市值上限(亿)

# 北交所参数组合
BSE_DROP_THRESHOLDS = [-7, -10, -15, -20]
BSE_GAPUP_RANGES = [(1, 5), (2, 10)]
BSE_MCAP_LIMITS = [50, 100]
# ================================


class Logger:
    """同时输出到stdout和文件"""
    def __init__(self, filepath):
        os.makedirs(os.path.dirname(filepath), exist_ok=True)
        self.terminal = sys.stdout
        self.log = open(filepath, 'w', encoding='utf-8')

    def write(self, message):
        self.terminal.write(message)
        self.log.write(message)

    def flush(self):
        self.terminal.flush()
        self.log.flush()

    def close(self):
        self.log.close()


def get_limit_up_price(code, preclose):
    """计算涨停价"""
    if code.startswith("sh.688"):
        return round(preclose * 1.2, 2)
    elif code.startswith("bj."):
        return round(preclose * 1.3, 2)
    elif code.startswith("sz.300") or code.startswith("sz.301"):
        return round(preclose * 1.2, 2)
    else:
        return round(preclose * 1.1, 2)


def get_limit_down_price(code, preclose):
    """计算跌停价"""
    if code.startswith("sh.688"):
        return round(preclose * 0.8, 2)
    elif code.startswith("bj."):
        return round(preclose * 0.7, 2)
    elif code.startswith("sz.300") or code.startswith("sz.301"):
        return round(preclose * 0.8, 2)
    else:
        return round(preclose * 0.9, 2)


def is_yizi_limit_up(code, open_p, high, low, close, preclose):
    """判断是否一字涨停板"""
    if open_p == high == low == close and preclose > 0:
        limit_price = get_limit_up_price(code, preclose)
        if close >= limit_price:
            return True
    return False


def estimate_mcap_yi(amount, turn):
    """用成交额和换手率反推流通市值(亿元)
    流通市值 = amount / (turn/100)，单位从元转为亿
    """
    if turn is None or turn <= 0 or amount is None or amount <= 0:
        return None
    mcap = amount / (turn / 100.0) / 1e8
    return mcap


def get_all_trading_days(cursor, date_start, date_end, code_prefix):
    """获取指定板块在日期范围内的所有交易日"""
    cursor.execute("""
        SELECT DISTINCT date FROM stock_kline
        WHERE code LIKE ? AND date >= ? AND date <= ?
        ORDER BY date
    """, (code_prefix + '%', date_start, date_end))
    return [r[0] for r in cursor.fetchall()]


def collect_signals(cursor, all_days, code_prefix, drop_threshold, gapup_min, gapup_max, mcap_limit):
    """
    收集满足条件的所有信号
    返回列表，每个元素包含：信号日信息 + 后续T+1/T+2/T+3数据
    """
    signals = []

    for i in range(1, len(all_days)):
        today = all_days[i]
        yesterday = all_days[i - 1]

        # 查询：昨日大阴 + 今日高开
        query = """
            SELECT
                t.date as today_date, t.code, t.code_name,
                t.open, t.high, t.low, t.close, t.preclose,
                t.close_rate as today_close_rate,
                t.volume, t.amount, t.turn, t.isST,
                t.hour1_open, t.hour1_close, t.hour4_close,
                y.close_rate as y_close_rate,
                y.close as y_close, y.preclose as y_preclose
            FROM stock_kline t
            JOIN stock_kline y ON t.code = y.code AND y.date = ?
            WHERE t.date = ?
              AND t.code LIKE ?
              AND y.close_rate <= ?
              AND t.preclose > 0
              AND ((t.open - t.preclose) / t.preclose * 100) >= ?
              AND ((t.open - t.preclose) / t.preclose * 100) <= ?
        """
        cursor.execute(query, (yesterday, today, code_prefix + '%',
                               drop_threshold, gapup_min, gapup_max))
        columns = [desc[0] for desc in cursor.description]
        rows = cursor.fetchall()

        for row in rows:
            d = dict(zip(columns, row))

            # 排除ST
            if d['isST'] == 1:
                continue
            name = d['code_name'] or ''
            if 'ST' in name.upper():
                continue

            # 排除一字涨停
            if is_yizi_limit_up(d['code'], d['open'], d['high'], d['low'],
                                d['close'], d['preclose']):
                continue

            # 市值过滤
            mcap = estimate_mcap_yi(d['amount'], d['turn'])
            if mcap_limit is not None and mcap is not None:
                if mcap > mcap_limit:
                    continue

            # 高开百分比
            gap_pct = (d['open'] - d['preclose']) / d['preclose'] * 100

            signals.append({
                'code': d['code'],
                'name': d['code_name'],
                'date': today,
                'yesterday': yesterday,
                'y_close_rate': d['y_close_rate'],
                'gap_pct': gap_pct,
                'buy_price': d['open'],  # 买入价 = today open
                'today_close': d['close'],
                'mcap': mcap,
                'today_idx': i,
            })

    # 获取T+1, T+2, T+3的数据
    for sig in signals:
        idx = sig['today_idx']
        code = sig['code']
        sig['t1_h1_open'] = None
        sig['t1_h4_close'] = None
        sig['t2_h4_close'] = None
        sig['t3_h4_close'] = None
        sig['t1_is_yizi'] = False

        for offset, key_prefix in [(1, 't1'), (2, 't2'), (3, 't3')]:
            future_idx = idx + offset
            if future_idx >= len(all_days):
                continue
            future_date = all_days[future_idx]
            cursor.execute("""
                SELECT open, high, low, close, preclose,
                       hour1_open, hour4_close
                FROM stock_kline
                WHERE code = ? AND date = ?
            """, (code, future_date))
            frow = cursor.fetchone()
            if frow:
                f_open, f_high, f_low, f_close, f_preclose, f_h1_open, f_h4_close = frow
                if key_prefix == 't1':
                    sig['t1_h1_open'] = f_h1_open
                    sig['t1_h4_close'] = f_h4_close
                    # 检查T+1是否一字涨停（无法买入）
                    if f_open and f_high and f_low and f_close and f_preclose:
                        sig['t1_is_yizi'] = is_yizi_limit_up(
                            code, f_open, f_high, f_low, f_close, f_preclose)
                elif key_prefix == 't2':
                    sig['t2_h4_close'] = f_h4_close
                elif key_prefix == 't3':
                    sig['t3_h4_close'] = f_h4_close

    return signals


def calc_returns(signals):
    """计算各出场时点的收益统计"""
    # T+1 hour1_open 卖出收益（T+0买入today open, T+1 h1 open卖出）
    t1_h1_rets = []
    t1_h4_rets = []
    t2_h4_rets = []
    t3_h4_rets = []
    yizi_count = 0

    for sig in signals:
        buy_price = sig['buy_price']
        if buy_price is None or buy_price <= 0:
            continue

        if sig['t1_is_yizi']:
            yizi_count += 1

        if sig['t1_h1_open'] and sig['t1_h1_open'] > 0:
            t1_h1_rets.append({
                'ret': (sig['t1_h1_open'] - buy_price) / buy_price * 100,
                'date': sig['date'],
                'code': sig['code'],
            })
        if sig['t1_h4_close'] and sig['t1_h4_close'] > 0:
            t1_h4_rets.append({
                'ret': (sig['t1_h4_close'] - buy_price) / buy_price * 100,
                'date': sig['date'],
                'code': sig['code'],
            })
        if sig['t2_h4_close'] and sig['t2_h4_close'] > 0:
            t2_h4_rets.append({
                'ret': (sig['t2_h4_close'] - buy_price) / buy_price * 100,
                'date': sig['date'],
                'code': sig['code'],
            })
        if sig['t3_h4_close'] and sig['t3_h4_close'] > 0:
            t3_h4_rets.append({
                'ret': (sig['t3_h4_close'] - buy_price) / buy_price * 100,
                'date': sig['date'],
                'code': sig['code'],
            })

    return t1_h1_rets, t1_h4_rets, t2_h4_rets, t3_h4_rets, yizi_count


def summarize_rets(rets_list):
    """汇总收益列表的统计指标"""
    if not rets_list:
        return {'n': 0, 'mean': 0, 'winrate': 0, 'median': 0,
                'worst_year': 'N/A', 'best_year': 'N/A',
                'yearly': {}}

    n = len(rets_list)
    values = [r['ret'] for r in rets_list]
    mean = sum(values) / n
    winrate = sum(1 for v in values if v > 0) / n * 100
    sorted_vals = sorted(values)
    median = sorted_vals[n // 2]

    # 按年统计
    yearly = defaultdict(list)
    for r in rets_list:
        year = r['date'][:4]
        yearly[year].append(r['ret'])

    yearly_stats = {}
    for year, vals in sorted(yearly.items()):
        yr_mean = sum(vals) / len(vals)
        yr_winrate = sum(1 for v in vals if v > 0) / len(vals) * 100
        yearly_stats[year] = {'n': len(vals), 'mean': yr_mean, 'winrate': yr_winrate}

    worst_year = min(yearly_stats.items(), key=lambda x: x[1]['mean']) if yearly_stats else ('N/A', {'mean': 0})
    best_year = max(yearly_stats.items(), key=lambda x: x[1]['mean']) if yearly_stats else ('N/A', {'mean': 0})

    return {
        'n': n,
        'mean': mean,
        'winrate': winrate,
        'median': median,
        'worst_year': f"{worst_year[0]}({worst_year[1]['mean']:+.2f}%)",
        'best_year': f"{best_year[0]}({best_year[1]['mean']:+.2f}%)",
        'yearly': yearly_stats,
    }


def print_param_table(results, board_name):
    """打印参数组合统计表"""
    print(f"\n{'='*100}")
    print(f"  {board_name} - 各参数组合统计表")
    print(f"{'='*100}")
    print(f"{'跌幅阈值':>8} | {'高开范围':>10} | {'市值(亿)':>8} | {'样本数':>6} | "
          f"{'T+1h1收益':>9} | {'T+1h4收益':>9} | {'T+2h4收益':>9} | {'T+3h4收益':>9} | "
          f"{'T+1胜率':>7} | {'最差年':>14} | {'最好年':>14} | {'一字板%':>7}")
    print(f"{'-'*8}-+-{'-'*10}-+-{'-'*8}-+-{'-'*6}-+-"
          f"{'-'*9}-+-{'-'*9}-+-{'-'*9}-+-{'-'*9}-+-"
          f"{'-'*7}-+-{'-'*14}-+-{'-'*14}-+-{'-'*7}")

    for r in results:
        drop = r['drop']
        gapup = f"{r['gapup_min']}-{r['gapup_max']}%"
        mcap = f"<{r['mcap']}" if r['mcap'] else "不限"
        n = r['stats_t1_h1']['n']
        t1h1 = r['stats_t1_h1']['mean']
        t1h4 = r['stats_t1_h4']['mean']
        t2h4 = r['stats_t2_h4']['mean']
        t3h4 = r['stats_t3_h4']['mean']
        winrate = r['stats_t1_h4']['winrate']
        worst = r['stats_t1_h4']['worst_year']
        best = r['stats_t1_h4']['best_year']
        yizi_pct = r['yizi_pct']
        print(f"{drop:>7}% | {gapup:>10} | {mcap:>8} | {n:>6} | "
              f"{t1h1:>+8.2f}% | {t1h4:>+8.2f}% | {t2h4:>+8.2f}% | {t3h4:>+8.2f}% | "
              f"{winrate:>6.1f}% | {worst:>14} | {best:>14} | {yizi_pct:>6.1f}%")


def print_yearly_detail(results, board_name):
    """打印最优参数的年度明细"""
    if not results:
        return

    # 找T+1 h4 mean最优的
    best = max(results, key=lambda x: x['stats_t1_h4']['mean'] if x['stats_t1_h4']['n'] >= 20 else -999)
    print(f"\n{'='*80}")
    print(f"  {board_name} - 最优参数年度明细")
    print(f"  参数: 跌幅≤{best['drop']}%, 高开{best['gapup_min']}-{best['gapup_max']}%, "
          f"市值<{best['mcap']}亿" if best['mcap'] else f"市值不限")
    print(f"{'='*80}")
    print(f"{'年份':>6} | {'样本数':>6} | {'T+1h1均值':>10} | {'T+1h4均值':>10} | {'T+1h4胜率':>9}")
    print(f"{'-'*6}-+-{'-'*6}-+-{'-'*10}-+-{'-'*10}-+-{'-'*9}")

    yearly_t1h1 = defaultdict(list)
    yearly_t1h4 = defaultdict(list)

    # 需要从signals重新计算... 用stats_t1_h4的yearly
    for year, ystats in sorted(best['stats_t1_h4']['yearly'].items()):
        # T+1 h1 yearly
        t1h1_yr = best['stats_t1_h1']['yearly'].get(year, {'n': 0, 'mean': 0, 'winrate': 0})
        print(f"{year:>6} | {ystats['n']:>6} | {t1h1_yr['mean']:>+9.2f}% | "
              f"{ystats['mean']:>+9.2f}% | {ystats['winrate']:>8.1f}%")


def run_board_research(cursor, board_name, code_prefix, drop_thresholds,
                       gapup_ranges, mcap_limits, all_days):
    """运行单个板块的研究"""
    print(f"\n{'#'*100}")
    print(f"#  {board_name} 大阴高开策略研究")
    print(f"#  代码前缀: {code_prefix}*  日期范围: {DATE_START} ~ {DATE_END}")
    print(f"#  交易日数: {len(all_days)}")
    print(f"{'#'*100}")

    # 检查数据量
    cursor.execute("SELECT COUNT(DISTINCT code) FROM stock_kline WHERE code LIKE ? AND date >= ? AND date <= ?",
                   (code_prefix + '%', DATE_START, DATE_END))
    stock_count = cursor.fetchone()[0]
    print(f"  股票数量: {stock_count}")

    if stock_count == 0:
        print(f"  *** 数据库中无{board_name}数据，跳过 ***")
        return []

    results = []

    for drop_thresh in drop_thresholds:
        for gapup_min, gapup_max in gapup_ranges:
            for mcap_limit in mcap_limits + [None]:  # None表示不限市值
                print(f"\n  --- 测试: 跌幅≤{drop_thresh}%, 高开{gapup_min}-{gapup_max}%, "
                      f"市值{'<'+str(mcap_limit)+'亿' if mcap_limit else '不限'} ---")

                signals = collect_signals(cursor, all_days, code_prefix,
                                          drop_thresh, gapup_min, gapup_max, mcap_limit)
                print(f"  信号数: {len(signals)}")

                if not signals:
                    results.append({
                        'drop': drop_thresh,
                        'gapup_min': gapup_min,
                        'gapup_max': gapup_max,
                        'mcap': mcap_limit,
                        'stats_t1_h1': summarize_rets([]),
                        'stats_t1_h4': summarize_rets([]),
                        'stats_t2_h4': summarize_rets([]),
                        'stats_t3_h4': summarize_rets([]),
                        'yizi_pct': 0,
                    })
                    continue

                t1_h1, t1_h4, t2_h4, t3_h4, yizi_count = calc_returns(signals)
                yizi_pct = yizi_count / len(signals) * 100 if signals else 0

                stats_t1_h1 = summarize_rets(t1_h1)
                stats_t1_h4 = summarize_rets(t1_h4)
                stats_t2_h4 = summarize_rets(t2_h4)
                stats_t3_h4 = summarize_rets(t3_h4)

                print(f"  T+1 h1_open卖: n={stats_t1_h1['n']}, mean={stats_t1_h1['mean']:+.2f}%, "
                      f"winrate={stats_t1_h1['winrate']:.1f}%")
                print(f"  T+1 h4_close卖: n={stats_t1_h4['n']}, mean={stats_t1_h4['mean']:+.2f}%, "
                      f"winrate={stats_t1_h4['winrate']:.1f}%")
                print(f"  一字涨停(无法买入): {yizi_count}/{len(signals)} = {yizi_pct:.1f}%")

                results.append({
                    'drop': drop_thresh,
                    'gapup_min': gapup_min,
                    'gapup_max': gapup_max,
                    'mcap': mcap_limit,
                    'stats_t1_h1': stats_t1_h1,
                    'stats_t1_h4': stats_t1_h4,
                    'stats_t2_h4': stats_t2_h4,
                    'stats_t3_h4': stats_t3_h4,
                    'yizi_pct': yizi_pct,
                })

    return results


def print_comparison(star_results, bse_results):
    """打印跨板块对比"""
    print(f"\n\n{'='*100}")
    print(f"  跨板块对比：科创板 vs 北交所 vs 创业板(参考)")
    print(f"{'='*100}")
    print(f"{'板块':>8} | {'最优条件':>30} | {'6年样本':>7} | {'平均收益':>8} | {'胜率':>6} | {'结论':>20}")
    print(f"{'-'*8}-+-{'-'*30}-+-{'-'*7}-+-{'-'*8}-+-{'-'*6}-+-{'-'*20}")

    # 找各板块最优（按T+1 h4 mean，需样本≥20）
    for name, results in [("科创板", star_results), ("北交所", bse_results)]:
        valid = [r for r in results if r['stats_t1_h4']['n'] >= 20]
        if valid:
            best = max(valid, key=lambda x: x['stats_t1_h4']['mean'])
            cond = f"跌≤{best['drop']}%,开{best['gapup_min']}-{best['gapup_max']}%," \
                   f"市值{'<'+str(best['mcap'])+'亿' if best['mcap'] else '不限'}"
            n = best['stats_t1_h4']['n']
            mean = best['stats_t1_h4']['mean']
            wr = best['stats_t1_h4']['winrate']
            conclusion = "有alpha" if mean > 1.0 and wr > 55 else "待验证" if mean > 0 else "无alpha"
            print(f"{name:>8} | {cond:>30} | {n:>7} | {mean:>+7.2f}% | {wr:>5.1f}% | {conclusion:>20}")
        else:
            print(f"{name:>8} | {'无有效数据':>30} | {'-':>7} | {'-':>8} | {'-':>6} | {'数据不足':>20}")

    # 创业板参考（从现有数据中取一个基线）
    print(f"{'创业板(参考)':>8} | {'跌≤-5%,开2-8%,不限':>30} | {'(见原脚本)':>7} | {'(待对比)':>8} | {'(待对比)':>6} | {'基线参照':>20}")


def print_liquidity_analysis(signals, board_name):
    """流动性分析：一字板比例"""
    if not signals:
        print(f"\n  {board_name} 流动性分析: 无信号数据")
        return

    yizi_count = sum(1 for s in signals if s.get('t1_is_yizi', False))
    total = len(signals)
    print(f"\n  {board_name} 流动性分析:")
    print(f"    信号总数: {total}")
    print(f"    T+1一字涨停(无法买入): {yizi_count} ({yizi_count/total*100:.1f}%)")
    print(f"    结论: {'流动性充足' if yizi_count/total < 0.05 else '存在较多无法买入情况' if yizi_count/total < 0.15 else '流动性严重不足'}")


def main():
    print(f"{'='*100}")
    print(f"  大阴高开策略 - 科创板/北交所专项研究")
    print(f"  日期范围: {DATE_START} ~ {DATE_END}")
    print(f"  执行时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"{'='*100}")

    # 设置日志
    logger = Logger(LOG_PATH)
    sys.stdout = logger

    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()

    # ============ 科创板研究 ============
    star_days = get_all_trading_days(cursor, DATE_START, DATE_END, 'sh.688')
    print(f"\n科创板交易日数: {len(star_days)}")

    star_results = run_board_research(
        cursor, "科创板(sh.688*)", "sh.688",
        STAR_DROP_THRESHOLDS, STAR_GAPUP_RANGES, STAR_MCAP_LIMITS, star_days
    )

    if star_results:
        print_param_table(star_results, "科创板(±20%)")
        print_yearly_detail(star_results, "科创板(±20%)")

    # ============ 北交所研究 ============
    bse_days = get_all_trading_days(cursor, DATE_START, DATE_END, 'bj.')
    print(f"\n北交所交易日数: {len(bse_days)}")

    bse_results = run_board_research(
        cursor, "北交所(bj.*)", "bj.",
        BSE_DROP_THRESHOLDS, BSE_GAPUP_RANGES, BSE_MCAP_LIMITS, bse_days
    )

    if bse_results:
        print_param_table(bse_results, "北交所(±30%)")
        print_yearly_detail(bse_results, "北交所(±30%)")

    # ============ 跨板块对比 ============
    print_comparison(star_results, bse_results)

    # ============ 关键问题验证 ============
    print(f"\n\n{'='*100}")
    print(f"  关键问题验证")
    print(f"{'='*100}")

    # 1. 流动性问题
    print(f"\n【1. 流动性分析】")
    # 对科创板最宽泛条件收集一次信号用于流动性分析
    if star_days:
        all_star_signals = collect_signals(cursor, star_days, "sh.688", -7, 1, 20, None)
        print_liquidity_analysis(all_star_signals, "科创板")

    if bse_days:
        all_bse_signals = collect_signals(cursor, bse_days, "bj.", -7, 1, 30, None)
        print_liquidity_analysis(all_bse_signals, "北交所")

    # 2. 样本量
    print(f"\n【2. 样本量评估】")
    for name, results in [("科创板", star_results), ("北交所", bse_results)]:
        valid = [r for r in results if r['stats_t1_h4']['n'] >= 20]
        low_sample = [r for r in results if 0 < r['stats_t1_h4']['n'] < 20]
        print(f"  {name}: {len(valid)}个参数组合样本≥20, {len(low_sample)}个组合样本<20(统计意义有限)")

    # 3. 年度稳定性
    print(f"\n【3. 年度稳定性分析】")
    for name, results in [("科创板", star_results), ("北交所", bse_results)]:
        valid = [r for r in results if r['stats_t1_h4']['n'] >= 30]
        if valid:
            best = max(valid, key=lambda x: x['stats_t1_h4']['mean'])
            yearly = best['stats_t1_h4']['yearly']
            print(f"  {name} 最优参数(跌≤{best['drop']}%, 开{best['gapup_min']}-{best['gapup_max']}%, "
                  f"市值{'<'+str(best['mcap'])+'亿' if best['mcap'] else '不限'}):")
            positive_years = sum(1 for y in yearly.values() if y['mean'] > 0)
            total_years = len(yearly)
            print(f"    年度正收益: {positive_years}/{total_years}年")
            for year, ys in sorted(yearly.items()):
                print(f"    {year}: n={ys['n']:>3}, mean={ys['mean']:>+6.2f}%, winrate={ys['winrate']:.1f}%")
            stability = "稳定" if positive_years >= total_years * 0.7 else "不稳定"
            print(f"    结论: {stability}")
        else:
            print(f"  {name}: 无足够样本的参数组合")

    conn.close()
    sys.stdout = logger.terminal
    logger.close()
    print(f"\n研究完成。日志已保存至: {LOG_PATH}")


if __name__ == "__main__":
    main()
