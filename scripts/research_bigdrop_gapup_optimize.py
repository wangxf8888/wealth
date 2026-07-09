#!/usr/bin/env python3
"""
大阴高开策略(<50亿) - 精细化多维度参数优化
在2021-2026数据上做6个维度交叉筛选，找出最优子集
T+0合规：大阴线用yesterday数据，高开用today open vs yesterday close
         买入用today hour1_open(或hour2_open)，收益=买入价到当日close
"""
import sys
import sqlite3
import os
from datetime import datetime
from collections import defaultdict
from itertools import product

# ============ 配置区 ============
DB_PATH = "/home/AIWealth/data/stocks.db"
LOG_PATH = "/home/AIWealth/scripts/logs/bigdrop_gapup_optimize.log"
YEARS = [2021, 2022, 2023, 2024, 2025, 2026]
INDEX_CODE = "sh.000001"
# ================================

# 维度定义
DIM_DROP = [5, 6, 7, 8]              # 前日跌幅阈值(%)
DIM_GAP = [(1, 2), (2, 3), (3, 5), (5, 99)]  # 高开幅度区间(%)
DIM_BOARD = ['main', 'gem', 'star']   # 主板/创业板/科创板
DIM_VOLUME = ['vol_up', 'vol_down']   # 放量/缩量
DIM_MARKET = ['mkt_down', 'mkt_up']   # 大盘跌/涨
DIM_BUY = ['hour1', 'hour2']          # 买入时段


def setup_logging():
    """将stdout重定向到日志文件"""
    os.makedirs(os.path.dirname(LOG_PATH), exist_ok=True)
    log_file = open(LOG_PATH, 'w', encoding='utf-8')

    class TeeOutput:
        def __init__(self, *files):
            self.files = files
        def write(self, text):
            for f in self.files:
                f.write(text)
                f.flush()
        def flush(self):
            for f in self.files:
                f.flush()

    sys.stdout = TeeOutput(sys.stdout, log_file)
    return log_file


def get_board(code):
    """判断板块: main/gem/star/bj/unknown"""
    if code.startswith("sh.60") or code.startswith("sz.00"):
        return "main"
    elif code.startswith("sz.30"):
        return "gem"
    elif code.startswith("sh.688") or code.startswith("sh.689"):
        return "star"
    elif code.startswith("bj.") or code.startswith("sh.8") or code.startswith("sz.8") or code.startswith("sh.4") or code.startswith("sz.4"):
        return "bj"
    return "unknown"


def is_st(row):
    if row.get('isST') == 1:
        return True
    name = row.get('code_name') or ''
    if 'ST' in name.upper():
        return True
    return False


def calc_market_cap(amount, turn):
    """流通市值 = 成交额 / 换手率 * 100 (亿元)"""
    if turn and turn > 0 and amount and amount > 0:
        return amount / turn * 100 / 1e8
    return None


def get_all_trading_days(cursor):
    cursor.execute("SELECT DISTINCT date FROM stock_kline ORDER BY date")
    return [r[0] for r in cursor.fetchall()]


def load_index_daily(cursor):
    """加载上证指数日线 close_rate，用于判断大盘涨跌"""
    cursor.execute("SELECT date, close_rate FROM index_kline WHERE code=? ORDER BY date", (INDEX_CODE,))
    return {r[0]: r[1] for r in cursor.fetchall()}


def load_all_candidates(cursor, all_days, index_daily):
    """一次性加载所有候选数据，附加多维度标签"""
    all_days_set = set(all_days)
    day_index = {d: i for i, d in enumerate(all_days)}

    # 预加载5日均量数据（使用窗口查询太慢，改用逐日处理）
    # 为提高效率，采用SQL一次性获取所有符合基本条件的候选对
    print("  加载候选数据中...")

    # 生成月份列表
    all_months = []
    for year in YEARS:
        end_month = 6 if year == 2026 else 12
        for m in range(1, end_month + 1):
            all_months.append(f"{year}-{m:02d}")

    candidates = []
    processed_days = 0

    for month in all_months:
        # 获取该月交易日
        cursor.execute("SELECT DISTINCT date FROM stock_kline WHERE date LIKE ? ORDER BY date", (month + '%',))
        month_days = [r[0] for r in cursor.fetchall()]

        for today in month_days:
            if today not in all_days_set:
                continue
            today_idx = day_index[today]
            if today_idx < 5:  # 需要至少5天前数据算均量
                continue
            yesterday = all_days[today_idx - 1]

            # 大盘情绪：前日大盘涨跌
            mkt_rate = index_daily.get(yesterday)
            mkt_tag = 'mkt_up' if (mkt_rate is not None and mkt_rate >= 0) else 'mkt_down'

            # 查询：前日跌幅>=5%（最宽松阈值），今日高开>=1%
            query = """
                SELECT
                    t.date, t.code, t.code_name, t.preclose,
                    t.open, t.high, t.low, t.close, t.close_rate,
                    t.volume, t.amount, t.turn, t.isST,
                    t.hour1_open, t.hour2_open,
                    y.close as y_close, y.close_rate as y_close_rate,
                    y.volume as y_volume, y.preclose as y_preclose,
                    y.open as y_open, y.close as y_close_price
                FROM stock_kline t
                JOIN stock_kline y ON t.code = y.code AND y.date = ?
                WHERE t.date = ?
                  AND t.preclose > 0
                  AND y.preclose > 0
                  AND y.close_rate <= -5.0
                  AND ((t.open - t.preclose) / t.preclose * 100) >= 1.0
            """
            cursor.execute(query, (yesterday, today))
            columns = [desc[0] for desc in cursor.description]

            for row in cursor.fetchall():
                d = dict(zip(columns, row))

                # 过滤ST
                if is_st(d):
                    continue

                # 板块过滤
                board = get_board(d['code'])
                if board in ('bj', 'unknown'):
                    continue

                # 市值过滤：仅<50亿
                cap = calc_market_cap(d.get('amount'), d.get('turn'))
                if cap is None or cap >= 50:
                    continue

                # 前日跌幅(%)
                y_drop = d['y_close_rate']  # 已经是close_rate，即(close-preclose)/preclose*100

                # 高开幅度(%)
                gap_pct = (d['open'] - d['preclose']) / d['preclose'] * 100

                # 买入价
                h1_buy = d['hour1_open']
                h2_buy = d['hour2_open']

                # 跳过无效小时数据
                if not h1_buy or h1_buy <= 0:
                    continue

                # 计算收益
                close_price = d['close']
                if close_price <= 0:
                    continue

                h1_ret = (close_price - h1_buy) / h1_buy * 100
                h2_ret = None
                if h2_buy and h2_buy > 0:
                    h2_ret = (close_price - h2_buy) / h2_buy * 100

                # 获取前5日均量（用于判断放量）
                vol_5d_days = all_days[max(0, today_idx-5):today_idx]
                if len(vol_5d_days) >= 3:
                    placeholders = ','.join(['?'] * len(vol_5d_days))
                    cursor.execute(f"""
                        SELECT AVG(volume) FROM stock_kline
                        WHERE code=? AND date IN ({placeholders})
                    """, [d['code']] + vol_5d_days)
                    avg_vol_5d = cursor.fetchone()[0]
                else:
                    avg_vol_5d = None

                # 放量判断：前日成交量 > 5日均量*1.5
                y_vol = d['y_volume']
                if avg_vol_5d and avg_vol_5d > 0 and y_vol:
                    vol_tag = 'vol_up' if y_vol > avg_vol_5d * 1.5 else 'vol_down'
                else:
                    vol_tag = 'vol_down'  # 无数据默认缩量

                candidates.append({
                    'code': d['code'],
                    'name': d['code_name'] or '',
                    'date': today,
                    'year': int(today[:4]),
                    'board': board,
                    'y_drop': y_drop,        # 前日跌幅(负数)
                    'gap_pct': gap_pct,      # 高开幅度
                    'vol_tag': vol_tag,      # 放量/缩量
                    'mkt_tag': mkt_tag,      # 大盘涨/跌
                    'h1_ret': h1_ret,        # hour1买入收益
                    'h2_ret': h2_ret,        # hour2买入收益
                    'h1_buy': h1_buy,
                    'h2_buy': h2_buy,
                    'cap': cap,
                })

            processed_days += 1

        print(f"    {month}: 累计候选 {len(candidates)}")

    print(f"  总候选数: {len(candidates)}, 处理交易日: {processed_days}")
    return candidates


def get_drop_tag(y_drop, threshold):
    """前日跌幅是否满足阈值"""
    return y_drop <= -threshold


def get_gap_tag(gap_pct, gap_range):
    """高开幅度是否在指定区间"""
    return gap_range[0] <= gap_pct < gap_range[1]


def filter_candidates(candidates, drop_th=None, gap_range=None, board=None,
                      vol_tag=None, mkt_tag=None, buy_hour=None):
    """按多维度过滤候选"""
    result = candidates
    if drop_th is not None:
        result = [c for c in result if c['y_drop'] <= -drop_th]
    if gap_range is not None:
        result = [c for c in result if gap_range[0] <= c['gap_pct'] < gap_range[1]]
    if board is not None:
        result = [c for c in result if c['board'] == board]
    if vol_tag is not None:
        result = [c for c in result if c['vol_tag'] == vol_tag]
    if mkt_tag is not None:
        result = [c for c in result if c['mkt_tag'] == mkt_tag]
    return result


def calc_stats(candidates, buy_hour='hour1'):
    """计算统计指标"""
    if not candidates:
        return None

    ret_key = 'h1_ret' if buy_hour == 'hour1' else 'h2_ret'
    valid = [c for c in candidates if c[ret_key] is not None]
    if not valid:
        return None

    n = len(valid)
    rets = [c[ret_key] for c in valid]
    avg_ret = sum(rets) / n
    pos_count = sum(1 for r in rets if r > 0)
    pos_rate = pos_count / n * 100
    # 收益×胜率 综合分
    score = avg_ret * (pos_rate / 100)

    return {
        'count': n,
        'avg_ret': avg_ret,
        'pos_rate': pos_rate,
        'score': score,
        'max_ret': max(rets),
        'min_ret': min(rets),
    }


def calc_yearly_stats(candidates, buy_hour='hour1'):
    """计算逐年统计"""
    yearly = defaultdict(list)
    for c in candidates:
        yearly[c['year']].append(c)
    result = {}
    for year in YEARS:
        s = calc_stats(yearly.get(year, []), buy_hour)
        result[year] = s
    return result


def has_disaster_year(yearly_stats):
    """检查是否有灾难年(年度收益<-5%)"""
    for year, s in yearly_stats.items():
        if s and s['avg_ret'] < -5.0:
            return True
    return False


def print_separator(char='=', width=100):
    print(char * width)


def format_gap_label(gap_range):
    if gap_range[1] >= 99:
        return f"{gap_range[0]}%+"
    return f"{gap_range[0]}-{gap_range[1]}%"


def board_label(b):
    labels = {'main': '主板(60/00)', 'gem': '创业板(30)', 'star': '科创板(68)'}
    return labels.get(b, b)


def main():
    log_file = setup_logging()

    print_separator()
    print("大阴高开策略(<50亿) - 精细化多维度参数优化")
    print(f"执行时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"数据范围: 2021-01 ~ 2026-06")
    print(f"目标: 日均>1.5%, 胜率>58%, 无灾难年(<-5%)")
    print_separator()

    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()

    all_days = get_all_trading_days(cursor)
    print(f"数据库交易日: {len(all_days)} ({all_days[0]} ~ {all_days[-1]})")

    # 加载大盘日线
    print("加载上证指数日线...")
    index_daily = load_index_daily(cursor)
    print(f"  上证指数数据天数: {len(index_daily)}")

    # 加载所有候选
    candidates = load_all_candidates(cursor, all_days, index_daily)
    conn.close()

    if not candidates:
        print("ERROR: 未找到任何候选！")
        return

    # ======================================================
    # 一、各单维度6年汇总统计表
    # ======================================================
    print("\n\n")
    print_separator()
    print("一、各单维度6年汇总统计表（<50亿市值，Hour1买入）")
    print_separator()

    # 维度1：大阴线跌幅阈值
    print(f"\n【维度1】前日跌幅阈值")
    print(f"{'阈值':<10}| {'样本数':>6} | {'日均收益':>8} | {'胜率':>6} | {'综合分':>7}")
    print(f"{'-'*10}+{'-'*8}+{'-'*10}+{'-'*8}+{'-'*9}")
    for th in DIM_DROP:
        filtered = filter_candidates(candidates, drop_th=th)
        s = calc_stats(filtered, 'hour1')
        if s:
            print(f"≥{th}%{'':>6}| {s['count']:>6} | {s['avg_ret']:>+7.2f}% | {s['pos_rate']:>5.1f}% | {s['score']:>+6.3f}")

    # 维度2：高开幅度
    print(f"\n【维度2】今日高开幅度")
    print(f"{'区间':<10}| {'样本数':>6} | {'日均收益':>8} | {'胜率':>6} | {'综合分':>7}")
    print(f"{'-'*10}+{'-'*8}+{'-'*10}+{'-'*8}+{'-'*9}")
    for gap in DIM_GAP:
        filtered = filter_candidates(candidates, gap_range=gap)
        s = calc_stats(filtered, 'hour1')
        if s:
            label = format_gap_label(gap)
            print(f"{label:<10}| {s['count']:>6} | {s['avg_ret']:>+7.2f}% | {s['pos_rate']:>5.1f}% | {s['score']:>+6.3f}")

    # 维度3：板块
    print(f"\n【维度3】板块")
    print(f"{'板块':<14}| {'样本数':>6} | {'日均收益':>8} | {'胜率':>6} | {'综合分':>7}")
    print(f"{'-'*14}+{'-'*8}+{'-'*10}+{'-'*8}+{'-'*9}")
    for b in DIM_BOARD:
        filtered = filter_candidates(candidates, board=b)
        s = calc_stats(filtered, 'hour1')
        if s:
            print(f"{board_label(b):<14}| {s['count']:>6} | {s['avg_ret']:>+7.2f}% | {s['pos_rate']:>5.1f}% | {s['score']:>+6.3f}")

    # 维度4：成交量特征
    print(f"\n【维度4】前日成交量特征")
    print(f"{'特征':<10}| {'样本数':>6} | {'日均收益':>8} | {'胜率':>6} | {'综合分':>7}")
    print(f"{'-'*10}+{'-'*8}+{'-'*10}+{'-'*8}+{'-'*9}")
    for vt in DIM_VOLUME:
        filtered = filter_candidates(candidates, vol_tag=vt)
        s = calc_stats(filtered, 'hour1')
        if s:
            label = '放量(>1.5x)' if vt == 'vol_up' else '缩量'
            print(f"{label:<10}| {s['count']:>6} | {s['avg_ret']:>+7.2f}% | {s['pos_rate']:>5.1f}% | {s['score']:>+6.3f}")

    # 维度5：市场情绪
    print(f"\n【维度5】前日大盘涨跌（上证指数）")
    print(f"{'情绪':<10}| {'样本数':>6} | {'日均收益':>8} | {'胜率':>6} | {'综合分':>7}")
    print(f"{'-'*10}+{'-'*8}+{'-'*10}+{'-'*8}+{'-'*9}")
    for mt in DIM_MARKET:
        filtered = filter_candidates(candidates, mkt_tag=mt)
        s = calc_stats(filtered, 'hour1')
        if s:
            label = '大盘跌日' if mt == 'mkt_down' else '大盘涨日'
            print(f"{label:<10}| {s['count']:>6} | {s['avg_ret']:>+7.2f}% | {s['pos_rate']:>5.1f}% | {s['score']:>+6.3f}")

    # 维度6：买入时段
    print(f"\n【维度6】买入时段")
    print(f"{'时段':<12}| {'样本数':>6} | {'日均收益':>8} | {'胜率':>6} | {'综合分':>7}")
    print(f"{'-'*12}+{'-'*8}+{'-'*10}+{'-'*8}+{'-'*9}")
    for bh in DIM_BUY:
        s = calc_stats(candidates, bh)
        if s:
            label = 'Hour1 Open' if bh == 'hour1' else 'Hour2 Open'
            print(f"{label:<12}| {s['count']:>6} | {s['avg_ret']:>+7.2f}% | {s['pos_rate']:>5.1f}% | {s['score']:>+6.3f}")

    # ======================================================
    # 二、交叉组合Top 15排行
    # ======================================================
    print("\n\n")
    print_separator()
    print("二、交叉组合Top 15排行（按 日均收益×胜率 综合分排序）")
    print_separator()

    # 生成所有交叉组合
    combo_results = []

    for drop_th in DIM_DROP:
        for gap in DIM_GAP:
            for board in DIM_BOARD:
                for vol in DIM_VOLUME:
                    for mkt in DIM_MARKET:
                        for buy in DIM_BUY:
                            filtered = filter_candidates(
                                candidates,
                                drop_th=drop_th,
                                gap_range=gap,
                                board=board,
                                vol_tag=vol,
                                mkt_tag=mkt
                            )
                            s = calc_stats(filtered, buy)
                            if s and s['count'] >= 10:  # 最少10个样本
                                yearly = calc_yearly_stats(filtered, buy)
                                combo_results.append({
                                    'params': {
                                        'drop': drop_th,
                                        'gap': gap,
                                        'board': board,
                                        'vol': vol,
                                        'mkt': mkt,
                                        'buy': buy,
                                    },
                                    'stats': s,
                                    'yearly': yearly,
                                    'disaster': has_disaster_year(yearly),
                                })

    # 按综合分排序
    combo_results.sort(key=lambda x: x['stats']['score'], reverse=True)

    print(f"\n总组合数: {len(combo_results)} (样本≥10)")
    print(f"\n{'排名':<4}| {'跌幅':>4} | {'高开':>6} | {'板块':>10} | {'量':>8} | {'盘':>6} | {'买入':>6} | {'样本':>5} | {'日均':>7} | {'胜率':>6} | {'综合分':>7} | {'灾难年':>4}")
    print(f"{'-'*4}+{'-'*6}+{'-'*8}+{'-'*12}+{'-'*10}+{'-'*8}+{'-'*8}+{'-'*7}+{'-'*9}+{'-'*8}+{'-'*9}+{'-'*6}")

    top15 = combo_results[:15]
    for i, combo in enumerate(top15, 1):
        p = combo['params']
        s = combo['stats']
        gap_label = format_gap_label(p['gap'])
        board_l = {'main': '主板', 'gem': '创业板', 'star': '科创板'}[p['board']]
        vol_l = '放量' if p['vol'] == 'vol_up' else '缩量'
        mkt_l = '盘跌' if p['mkt'] == 'mkt_down' else '盘涨'
        buy_l = 'H1' if p['buy'] == 'hour1' else 'H2'
        dis_l = '否' if not combo['disaster'] else '是'
        print(f" {i:>2} | ≥{p['drop']}% | {gap_label:>6} | {board_l:>10} | {vol_l:>8} | {mkt_l:>6} | {buy_l:>6} | {s['count']:>5} | {s['avg_ret']:>+6.2f}% | {s['pos_rate']:>5.1f}% | {s['score']:>+6.3f} | {dis_l:>4}")

    # ======================================================
    # 三、Top 5组合逐年表现
    # ======================================================
    print("\n\n")
    print_separator()
    print("三、Top 5组合逐年表现（确认无灾难年）")
    print_separator()

    top5 = combo_results[:5]
    for i, combo in enumerate(top5, 1):
        p = combo['params']
        s = combo['stats']
        gap_label = format_gap_label(p['gap'])
        board_l = {'main': '主板', 'gem': '创业板', 'star': '科创板'}[p['board']]
        vol_l = '放量' if p['vol'] == 'vol_up' else '缩量'
        mkt_l = '盘跌' if p['mkt'] == 'mkt_down' else '盘涨'
        buy_l = 'H1' if p['buy'] == 'hour1' else 'H2'

        print(f"\n--- Top {i}: 跌幅≥{p['drop']}% | 高开{gap_label} | {board_l} | {vol_l} | {mkt_l} | {buy_l}买入 ---")
        print(f"  6年汇总: 样本={s['count']}, 日均={s['avg_ret']:+.2f}%, 胜率={s['pos_rate']:.1f}%, 综合分={s['score']:+.3f}")

        print(f"  {'年份':<6}| {'样本':>5} | {'日均收益':>8} | {'胜率':>6} | {'状态':>4}")
        print(f"  {'-'*6}+{'-'*7}+{'-'*10}+{'-'*8}+{'-'*6}")

        yearly = combo['yearly']
        all_positive = True
        for year in YEARS:
            ys = yearly.get(year)
            if ys:
                status = '✓' if ys['avg_ret'] > 0 else ('⚠' if ys['avg_ret'] > -5 else '✗')
                if ys['avg_ret'] <= 0:
                    all_positive = False
                print(f"  {year:<6}| {ys['count']:>5} | {ys['avg_ret']:>+7.2f}% | {ys['pos_rate']:>5.1f}% | {status}")
            else:
                print(f"  {year:<6}|     0 |      N/A |   N/A |  -")

        if all_positive:
            print(f"  >>> 每年均正收益 <<<")
        if not combo['disaster']:
            print(f"  >>> 无灾难年(无<-5%) <<<")

    # ======================================================
    # 四、达标组合汇总（日均>1.5%, 胜率>58%, 无灾难年）
    # ======================================================
    print("\n\n")
    print_separator()
    print("四、达标组合汇总（日均>1.5%, 胜率>58%, 无灾难年<-5%）")
    print_separator()

    qualified = [c for c in combo_results
                 if c['stats']['avg_ret'] > 1.5
                 and c['stats']['pos_rate'] > 58
                 and not c['disaster']]

    if qualified:
        print(f"\n达标组合数: {len(qualified)}")
        print(f"\n{'排名':<4}| {'跌幅':>4} | {'高开':>6} | {'板块':>10} | {'量':>8} | {'盘':>6} | {'买入':>6} | {'样本':>5} | {'日均':>7} | {'胜率':>6} | {'综合分':>7}")
        print(f"{'-'*4}+{'-'*6}+{'-'*8}+{'-'*12}+{'-'*10}+{'-'*8}+{'-'*8}+{'-'*7}+{'-'*9}+{'-'*8}+{'-'*9}")

        for i, combo in enumerate(qualified[:20], 1):
            p = combo['params']
            s = combo['stats']
            gap_label = format_gap_label(p['gap'])
            board_l = {'main': '主板', 'gem': '创业板', 'star': '科创板'}[p['board']]
            vol_l = '放量' if p['vol'] == 'vol_up' else '缩量'
            mkt_l = '盘跌' if p['mkt'] == 'mkt_down' else '盘涨'
            buy_l = 'H1' if p['buy'] == 'hour1' else 'H2'
            print(f" {i:>2} | ≥{p['drop']}% | {gap_label:>6} | {board_l:>10} | {vol_l:>8} | {mkt_l:>6} | {buy_l:>6} | {s['count']:>5} | {s['avg_ret']:>+6.2f}% | {s['pos_rate']:>5.1f}% | {s['score']:>+6.3f}")
    else:
        print("\n⚠ 无完全达标组合。放宽条件查看接近目标的组合：")
        # 放宽到日均>1.0%, 胜率>55%
        near_qualified = [c for c in combo_results
                         if c['stats']['avg_ret'] > 1.0
                         and c['stats']['pos_rate'] > 55
                         and not c['disaster']]
        if near_qualified:
            print(f"\n接近达标（日均>1.0%, 胜率>55%, 无灾难年）: {len(near_qualified)}个")
            for i, combo in enumerate(near_qualified[:10], 1):
                p = combo['params']
                s = combo['stats']
                gap_label = format_gap_label(p['gap'])
                board_l = {'main': '主板', 'gem': '创业板', 'star': '科创板'}[p['board']]
                vol_l = '放量' if p['vol'] == 'vol_up' else '缩量'
                mkt_l = '盘跌' if p['mkt'] == 'mkt_down' else '盘涨'
                buy_l = 'H1' if p['buy'] == 'hour1' else 'H2'
                print(f" {i:>2} | ≥{p['drop']}% | {gap_label:>6} | {board_l} | {vol_l} | {mkt_l} | {buy_l} | n={s['count']} | 日均{s['avg_ret']:+.2f}% | 胜率{s['pos_rate']:.1f}%")

    # ======================================================
    # 五、结论
    # ======================================================
    print("\n\n")
    print_separator()
    print("五、结论与建议")
    print_separator()

    if top5:
        best = top5[0]
        p = best['params']
        s = best['stats']
        gap_label = format_gap_label(p['gap'])
        board_l = {'main': '主板', 'gem': '创业板', 'star': '科创板'}[p['board']]
        vol_l = '放量' if p['vol'] == 'vol_up' else '缩量'
        mkt_l = '盘跌' if p['mkt'] == 'mkt_down' else '盘涨'
        buy_l = 'Hour1' if p['buy'] == 'hour1' else 'Hour2'

        print(f"\n【最优组合（综合分最高）】")
        print(f"  前日跌幅: ≥{p['drop']}%")
        print(f"  今日高开: {gap_label}")
        print(f"  板块: {board_l}")
        print(f"  前日成交量: {vol_l}")
        print(f"  大盘情绪: {mkt_l}")
        print(f"  买入时段: {buy_l} Open")
        print(f"  ---")
        print(f"  样本数: {s['count']}")
        print(f"  日均收益: {s['avg_ret']:+.2f}%")
        print(f"  胜率: {s['pos_rate']:.1f}%")
        print(f"  综合分(日均×胜率): {s['score']:+.3f}")
        print(f"  有灾难年: {'是' if best['disaster'] else '否'}")

        # 是否达标
        if s['avg_ret'] > 1.5 and s['pos_rate'] > 58 and not best['disaster']:
            print(f"\n  ★★★ 达标！满足日均>1.5%, 胜率>58%, 无灾难年 ★★★")
        else:
            reasons = []
            if s['avg_ret'] <= 1.5:
                reasons.append(f"日均{s['avg_ret']:+.2f}%≤1.5%")
            if s['pos_rate'] <= 58:
                reasons.append(f"胜率{s['pos_rate']:.1f}%≤58%")
            if best['disaster']:
                reasons.append("存在灾难年")
            print(f"\n  未完全达标: {', '.join(reasons)}")
            print(f"  建议: 可进一步细分维度或增加时间过滤条件")

    print(f"\n\n完成。执行时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"日志已保存: {LOG_PATH}")


if __name__ == "__main__":
    main()
