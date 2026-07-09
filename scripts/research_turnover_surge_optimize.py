#!/usr/bin/env python3
"""
换手率突增策略 - 多维度精细化参数优化与细分筛选
2021-2026全6年数据统计

目标：在已验证的换手率突增5x+策略（6年均正收益）基础上，通过多维度筛选找到最优子集，
      将日均收益从+0.60%提升到>1%，胜率从50.9%提升到>55%。

筛选维度：
  1. 市值分组：<50亿 / 50-200亿 / 200-700亿 / >700亿
  2. 板块分组：主板(60/00) / 创业板(30) / 科创板(68)
  3. 换手率突增倍数：3x / 4x / 5x / 7x / 10x
  4. 叠加过滤条件：股价涨幅<3%、MA5上下方、MA10上下方、前一日大盘涨跌
  5. 组合筛选：找出最优 [倍率] × [市值] × [过滤条件] 组合

T+0合规：
  - 换手率计算用yesterday及之前5日数据
  - 买入用today hour1 open
  - "日均收益"定义：hour1 open买入 → 当日close的收益
  - 不用today close做买入判断
"""
import sqlite3
import math
import os
from collections import defaultdict

# ==================== 配置区 ====================
DB_PATH = '/home/AIWealth/data/stocks.db'
LOG_PATH = '/home/AIWealth/scripts/logs/turnover_surge_optimize.log'
TURN_STABILITY_CV = 0.5
TURN_MIN_MEAN = 0.5
TURN_MAX_YESTERDAY = 20.0
PRICE_FLAT_THRESHOLD = 3.0
START_YEAR = 2021
END_YEAR = 2026
INDEX_CODE = 'sh.000001'
# ================================================

SURGE_THRESHOLDS = [3, 4, 5, 7, 10]
CAP_GROUPS = {
    '<50亿': (0, 50),
    '50-200亿': (50, 200),
    '200-700亿': (200, 700),
    '>700亿': (700, float('inf')),
}


def get_board(code):
    if code.startswith('sh.60') or code.startswith('sz.00') or code.startswith('sz.001') or code.startswith('sz.002') or code.startswith('sz.003'):
        return '主板'
    elif code.startswith('sz.300') or code.startswith('sz.301') or code.startswith('sz.302'):
        return '创业板'
    elif code.startswith('sh.688') or code.startswith('sh.689'):
        return '科创板'
    return '其他'


def get_limit_ratio(code):
    if code.startswith('sz.300') or code.startswith('sz.301') or code.startswith('sz.302'):
        return 0.20
    elif code.startswith('sh.688') or code.startswith('sh.689'):
        return 0.20
    elif code.startswith('bj.'):
        return 0.30
    return 0.10


def is_yizi_limit(open_p, high, low, close, preclose, code):
    if any(v is None for v in [open_p, high, low, close, preclose]):
        return False
    if preclose <= 0:
        return False
    limit_up = round(preclose * (1 + get_limit_ratio(code)), 2)
    limit_down = round(preclose * (1 - get_limit_ratio(code)), 2)
    if open_p == high == low == close:
        if close >= limit_up or close <= limit_down:
            return True
    return False


def get_cap_group(market_cap_yi):
    if market_cap_yi is None:
        return None
    for label, (lo, hi) in CAP_GROUPS.items():
        if lo <= market_cap_yi < hi:
            return label
    return None


def main():
    os.makedirs(os.path.dirname(LOG_PATH), exist_ok=True)
    log_file = open(LOG_PATH, 'w', encoding='utf-8')

    def log(msg=""):
        print(msg)
        log_file.write(msg + '\n')
        log_file.flush()

    log("=" * 90)
    log("换手率突增策略 - 多维度精细化参数优化")
    log("数据范围: 2021-2026 全6年")
    log(f"前5日换手率平稳: CV < {TURN_STABILITY_CV}, 均值 > {TURN_MIN_MEAN}%")
    log(f"股价变动阈值: |close_rate| <= {PRICE_FLAT_THRESHOLD}%")
    log(f"突增倍数对比: {SURGE_THRESHOLDS}")
    log("=" * 90)

    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()

    # 1. 获取交易日列表
    cur.execute("SELECT DISTINCT date FROM stock_kline ORDER BY date")
    all_days = [r[0] for r in cur.fetchall()]
    day_index = {d: i for i, d in enumerate(all_days)}
    log(f"数据库总交易日数: {len(all_days)}")
    log(f"数据范围: {all_days[0]} ~ {all_days[-1]}")

    # 2. 加载指数数据
    cur.execute("SELECT date, close_rate FROM index_kline WHERE code = ?", (INDEX_CODE,))
    index_data = {r[0]: r[1] for r in cur.fetchall()}
    log(f"指数数据天数: {len(index_data)}")

    # 3. 批量加载所有需要的股票数据到内存（高效方案）
    log("\n加载股票数据到内存...")

    # 过滤出2021-2026时间段（含前缓冲）的数据
    start_date = f"{START_YEAR - 1}-01-01"  # 前1年缓冲
    cur.execute("""
        SELECT date, code, code_name, open, high, low, close, preclose,
               turn, isST, close_rate, amount, hour1_open
        FROM stock_kline
        WHERE date >= ? AND preclose > 0
        ORDER BY code, date
    """, (start_date,))

    # 按code组织数据
    stock_data = defaultdict(list)  # code -> [(date, open, high, low, close, preclose, turn, isST, close_rate, amount, hour1_open, code_name)]
    row_count = 0
    for row in cur.fetchall():
        date, code, code_name, open_p, high, low, close, preclose, turn, isST, close_rate, amount, h1_open = row
        stock_data[code].append((date, open_p, high, low, close, preclose, turn, isST, close_rate, amount, h1_open, code_name))
        row_count += 1

    log(f"加载完成: {len(stock_data)} 只股票, {row_count} 条K线记录")

    # 4. 逐日扫描 - 内存中处理
    log("\n开始逐日扫描计算...")
    all_records = []

    # 为每只股票建立日期索引
    stock_day_idx = {}  # code -> {date: idx_in_list}
    for code, rows in stock_data.items():
        stock_day_idx[code] = {r[0]: i for i, r in enumerate(rows)}

    # 确定有效日期范围
    valid_days = [d for d in all_days if START_YEAR <= int(d[:4]) <= END_YEAR]
    valid_days = [d for d in valid_days if not (int(d[:4]) == END_YEAR and int(d[5:7]) > 6)]

    processed = 0
    for today in valid_days:
        ti = day_index[today]
        if ti < 7:
            continue

        yesterday = all_days[ti - 1]
        prev_5days = all_days[max(0, ti - 6):ti - 1]
        if len(prev_5days) < 4:
            continue

        # 前一日大盘涨跌
        index_prev_ret = index_data.get(yesterday)
        index_up = (index_prev_ret > 0) if index_prev_ret is not None else None

        # 需要MA计算的日期列表(yesterday往前10天)
        ma_days_set = set(all_days[max(0, ti - 11):ti])  # 包含yesterday

        prev_5days_set = set(prev_5days)

        # 遍历所有股票
        for code, rows in stock_data.items():
            didx = stock_day_idx[code]

            # 找到yesterday的数据
            yd_pos = didx.get(yesterday)
            if yd_pos is None:
                continue
            yd_row = rows[yd_pos]
            # (date, open, high, low, close, preclose, turn, isST, close_rate, amount, h1_open, code_name)
            _, yd_open, yd_high, yd_low, yd_close, yd_preclose, yd_turn, yd_isST, yd_close_rate, yd_amount, _, yd_code_name = yd_row

            # 基本过滤
            if yd_isST:
                continue
            if yd_code_name and 'ST' in yd_code_name.upper():
                continue
            if yd_turn is None or yd_turn <= 0:
                continue
            if yd_close_rate is None:
                continue
            if yd_turn > TURN_MAX_YESTERDAY:
                continue
            if abs(yd_close_rate) > PRICE_FLAT_THRESHOLD:
                continue
            if is_yizi_limit(yd_open, yd_high, yd_low, yd_close, yd_preclose, code):
                continue

            # 获取前5日换手率
            prev_turn_values = []
            for r in rows[max(0, yd_pos - 5):yd_pos]:
                if r[0] in prev_5days_set and r[6] is not None and r[6] > 0:
                    prev_turn_values.append(r[6])

            if len(prev_turn_values) < 4:
                continue

            mean_turn = sum(prev_turn_values) / len(prev_turn_values)
            if mean_turn < TURN_MIN_MEAN:
                continue

            variance = sum((t - mean_turn) ** 2 for t in prev_turn_values) / len(prev_turn_values)
            std_turn = math.sqrt(variance)
            cv = std_turn / mean_turn if mean_turn > 0 else 999
            if cv > TURN_STABILITY_CV:
                continue

            surge_ratio = yd_turn / mean_turn if mean_turn > 0 else 0
            if surge_ratio < 3.0:
                continue

            # Today数据
            td_pos = didx.get(today)
            if td_pos is None:
                continue
            td_row = rows[td_pos]
            t_open = td_row[1]
            t_close = td_row[4]
            t_h1_open = td_row[10]

            buy_price = t_h1_open if t_h1_open and t_h1_open > 0 else t_open
            if buy_price is None or buy_price <= 0:
                continue
            if t_close is None or t_close <= 0:
                continue

            day_return = (t_close - buy_price) / buy_price * 100

            # 流通市值（亿）
            market_cap_yi = None
            if yd_amount and yd_amount > 0 and yd_turn > 0:
                market_cap_yi = (yd_amount / (yd_turn / 100)) / 1e8

            # MA5 / MA10（用yesterday及之前收盘价）
            close_values = []
            for r in rows[max(0, yd_pos - 9):yd_pos + 1]:  # 包含yesterday
                if r[0] in ma_days_set and r[4] is not None and r[4] > 0:
                    close_values.append(r[4])

            ma5 = sum(close_values[-5:]) / 5 if len(close_values) >= 5 else None
            ma10 = sum(close_values[-10:]) / 10 if len(close_values) >= 10 else None

            above_ma5 = (yd_close > ma5) if ma5 else None
            above_ma10 = (yd_close > ma10) if ma10 else None

            all_records.append({
                'code': code,
                'date': today,
                'year': int(today[:4]),
                'day_return': day_return,
                'surge_ratio': surge_ratio,
                'board': get_board(code),
                'cap_group': get_cap_group(market_cap_yi),
                'above_ma5': above_ma5,
                'above_ma10': above_ma10,
                'index_up': index_up,
            })

        processed += 1
        if processed % 100 == 0:
            log(f"  进度: {processed}/{len(valid_days)} 交易日, 已收集 {len(all_records)} 条记录")

    conn.close()
    log(f"\n扫描完成: {processed} 个交易日, {len(all_records)} 条有效记录")

    # ========== 统计输出 ==========
    def calc_stats(subset):
        """计算子集统计"""
        if not subset:
            return None
        n = len(subset)
        avg_ret = sum(r['day_return'] for r in subset) / n
        win_rate = sum(1 for r in subset if r['day_return'] > 0) / n * 100
        year_avgs = {}
        for yr in range(START_YEAR, END_YEAR + 1):
            yr_sub = [r for r in subset if r['year'] == yr]
            if yr_sub:
                year_avgs[yr] = sum(r['day_return'] for r in yr_sub) / len(yr_sub)
        worst = min(year_avgs.values()) if year_avgs else 0
        best = max(year_avgs.values()) if year_avgs else 0
        return {'n': n, 'avg_ret': avg_ret, 'win_rate': win_rate, 'worst': worst, 'best': best, 'year_avgs': year_avgs}

    # 一、市值分组
    log(f"\n\n{'='*90}")
    log("一、市值分组统计")
    log("=" * 90)
    log(f"{'市值分组':<12}{'样本数':<10}{'日均收益%':<12}{'胜率%':<10}{'年度最差%':<12}{'年度最好%':<12}")
    log("-" * 68)
    for cap_label in ['<50亿', '50-200亿', '200-700亿', '>700亿']:
        s = calc_stats([r for r in all_records if r['cap_group'] == cap_label])
        if s:
            log(f"{cap_label:<12}{s['n']:<10}{s['avg_ret']:+.2f}%       {s['win_rate']:.1f}%     {s['worst']:+.2f}%      {s['best']:+.2f}%")
        else:
            log(f"{cap_label:<12}0         N/A")

    # 二、板块分组
    log(f"\n\n{'='*90}")
    log("二、板块分组统计")
    log("=" * 90)
    log(f"{'板块':<12}{'样本数':<10}{'日均收益%':<12}{'胜率%':<10}{'年度最差%':<12}{'年度最好%':<12}")
    log("-" * 68)
    for board_label in ['主板', '创业板', '科创板', '其他']:
        s = calc_stats([r for r in all_records if r['board'] == board_label])
        if s:
            log(f"{board_label:<12}{s['n']:<10}{s['avg_ret']:+.2f}%       {s['win_rate']:.1f}%     {s['worst']:+.2f}%      {s['best']:+.2f}%")
        else:
            log(f"{board_label:<12}0         N/A")

    # 三、换手率突增倍数对比
    log(f"\n\n{'='*90}")
    log("三、换手率突增倍数对比")
    log("=" * 90)
    log(f"{'倍数阈值':<12}{'样本数':<10}{'日均收益%':<12}{'胜率%':<10}{'年度最差%':<12}{'年度最好%':<12}")
    log("-" * 68)
    for threshold in SURGE_THRESHOLDS:
        s = calc_stats([r for r in all_records if r['surge_ratio'] >= threshold])
        if s:
            label = f">={threshold}x"
            log(f"{label:<12}{s['n']:<10}{s['avg_ret']:+.2f}%       {s['win_rate']:.1f}%     {s['worst']:+.2f}%      {s['best']:+.2f}%")
        else:
            log(f">={threshold}x      0         N/A")

    # 四、叠加过滤条件
    log(f"\n\n{'='*90}")
    log("四、叠加过滤条件统计（基于>=5x）")
    log("=" * 90)
    filter_configs = [
        ('基准(无过滤)', lambda r: r['surge_ratio'] >= 5),
        ('MA5上方', lambda r: r['surge_ratio'] >= 5 and r['above_ma5'] == True),
        ('MA5下方', lambda r: r['surge_ratio'] >= 5 and r['above_ma5'] == False),
        ('MA10上方', lambda r: r['surge_ratio'] >= 5 and r['above_ma10'] == True),
        ('MA10下方', lambda r: r['surge_ratio'] >= 5 and r['above_ma10'] == False),
        ('大盘昨涨', lambda r: r['surge_ratio'] >= 5 and r['index_up'] == True),
        ('大盘昨跌', lambda r: r['surge_ratio'] >= 5 and r['index_up'] == False),
        ('MA5上+大盘涨', lambda r: r['surge_ratio'] >= 5 and r['above_ma5'] == True and r['index_up'] == True),
        ('MA5下+大盘跌', lambda r: r['surge_ratio'] >= 5 and r['above_ma5'] == False and r['index_up'] == False),
        ('MA10上+大盘涨', lambda r: r['surge_ratio'] >= 5 and r['above_ma10'] == True and r['index_up'] == True),
        ('MA5上+MA10上', lambda r: r['surge_ratio'] >= 5 and r['above_ma5'] == True and r['above_ma10'] == True),
        ('MA5下+MA10下', lambda r: r['surge_ratio'] >= 5 and r['above_ma5'] == False and r['above_ma10'] == False),
    ]
    log(f"{'过滤条件':<20}{'样本数':<10}{'日均收益%':<12}{'胜率%':<10}{'年度最差%':<12}{'年度最好%':<12}")
    log("-" * 76)
    for label, fn in filter_configs:
        s = calc_stats([r for r in all_records if fn(r)])
        if s:
            log(f"{label:<20}{s['n']:<10}{s['avg_ret']:+.2f}%       {s['win_rate']:.1f}%     {s['worst']:+.2f}%      {s['best']:+.2f}%")
        else:
            log(f"{label:<20}0         N/A")

    # 五、组合筛选 - Top 10
    log(f"\n\n{'='*90}")
    log("五、组合筛选 - Top 10最优组合排行（按日均收益×胜率排序）")
    log("=" * 90)

    surge_options = [(f'>={t}x', t) for t in SURGE_THRESHOLDS]
    cap_options = list(CAP_GROUPS.keys()) + ['全部']
    filter_options = [
        ('无过滤', lambda r: True),
        ('MA5上方', lambda r: r['above_ma5'] == True),
        ('MA5下方', lambda r: r['above_ma5'] == False),
        ('MA10上方', lambda r: r['above_ma10'] == True),
        ('MA10下方', lambda r: r['above_ma10'] == False),
        ('大盘昨涨', lambda r: r['index_up'] == True),
        ('大盘昨跌', lambda r: r['index_up'] == False),
        ('MA5上+大盘涨', lambda r: r['above_ma5'] == True and r['index_up'] == True),
        ('MA10上+大盘涨', lambda r: r['above_ma10'] == True and r['index_up'] == True),
        ('MA5上+MA10上', lambda r: r['above_ma5'] == True and r['above_ma10'] == True),
    ]

    MIN_SAMPLES = 50
    combo_results = []

    for surge_label, surge_thresh in surge_options:
        # 先按倍率过滤
        surge_subset = [r for r in all_records if r['surge_ratio'] >= surge_thresh]
        for cap_label in cap_options:
            cap_subset = [r for r in surge_subset if cap_label == '全部' or r['cap_group'] == cap_label]
            if len(cap_subset) < MIN_SAMPLES:
                continue
            for filter_label, filter_fn in filter_options:
                subset = [r for r in cap_subset if filter_fn(r)]
                if len(subset) < MIN_SAMPLES:
                    continue

                avg_ret = sum(r['day_return'] for r in subset) / len(subset)
                win_rate = sum(1 for r in subset if r['day_return'] > 0) / len(subset) * 100

                year_avgs = {}
                year_samples = {}
                for yr in range(START_YEAR, END_YEAR + 1):
                    yr_sub = [r for r in subset if r['year'] == yr]
                    if yr_sub:
                        year_avgs[yr] = sum(r['day_return'] for r in yr_sub) / len(yr_sub)
                        year_samples[yr] = len(yr_sub)

                if len(year_avgs) < 4:
                    continue

                worst_year = min(year_avgs.values())
                best_year = max(year_avgs.values())
                positive_years = sum(1 for v in year_avgs.values() if v > 0)
                score = avg_ret * (win_rate / 100)

                combo_results.append({
                    'surge': surge_label,
                    'cap': cap_label,
                    'filter': filter_label,
                    'n': len(subset),
                    'avg_ret': avg_ret,
                    'win_rate': win_rate,
                    'worst_year': worst_year,
                    'best_year': best_year,
                    'positive_years': positive_years,
                    'total_years': len(year_avgs),
                    'score': score,
                    'year_avgs': year_avgs,
                    'year_samples': year_samples,
                    'surge_thresh': surge_thresh,
                })

    combo_results.sort(key=lambda x: x['score'], reverse=True)
    log(f"\n总共评估 {len(combo_results)} 个有效组合（样本量>={MIN_SAMPLES}）")
    log(f"\n{'排名':<5}{'倍率':<7}{'市值':<10}{'过滤条件':<14}{'样本':<7}{'日均%':<8}{'胜率%':<8}{'正年':<7}{'最差%':<9}{'最好%':<9}{'得分':<7}")
    log("-" * 91)

    top10 = combo_results[:10]
    for rank, c in enumerate(top10, 1):
        log(f"{rank:<5}{c['surge']:<7}{c['cap']:<10}{c['filter']:<14}{c['n']:<7}"
            f"{c['avg_ret']:+.2f}%  {c['win_rate']:.1f}%   {c['positive_years']}/{c['total_years']}    "
            f"{c['worst_year']:+.2f}%   {c['best_year']:+.2f}%   {c['score']:.3f}")

    # 六、Top10逐年明细
    log(f"\n\n{'='*90}")
    log("六、Top 10组合逐年表现（确认跨周期稳定性）")
    log("=" * 90)

    for rank, c in enumerate(top10, 1):
        log(f"\n--- Top{rank}: [{c['surge']}] × [{c['cap']}] × [{c['filter']}] ---")
        log(f"    总样本={c['n']}, 日均收益={c['avg_ret']:+.2f}%, 胜率={c['win_rate']:.1f}%, 得分={c['score']:.3f}")
        log(f"    {'年份':<8}{'日均收益%':<12}{'胜率%':<10}{'样本数':<10}")
        log(f"    {'-'*44}")

        surge_thresh = c['surge_thresh']
        cap_label = c['cap']
        filter_fn = dict(filter_options)[c['filter']]

        for yr in range(START_YEAR, END_YEAR + 1):
            yr_subset = [r for r in all_records
                         if r['year'] == yr
                         and r['surge_ratio'] >= surge_thresh
                         and (cap_label == '全部' or r['cap_group'] == cap_label)
                         and filter_fn(r)]
            if yr_subset:
                yr_avg = sum(r['day_return'] for r in yr_subset) / len(yr_subset)
                yr_wr = sum(1 for r in yr_subset if r['day_return'] > 0) / len(yr_subset) * 100
                marker = " ✓" if yr_avg > 0 else " ✗"
                log(f"    {yr:<8}{yr_avg:+.2f}%       {yr_wr:.1f}%     {len(yr_subset):<10}{marker}")
            else:
                log(f"    {yr:<8}N/A         N/A       0")

    # 七、板块×倍率交叉
    log(f"\n\n{'='*90}")
    log("七、板块 × 突增倍率 交叉统计（日均收益%/胜率%/样本数）")
    log("=" * 90)
    col_hdr = '板块/倍率'
    line = f"{col_hdr:<12}"
    for t in SURGE_THRESHOLDS:
        line += f"{'>=' + str(t) + 'x':<20}"
    log(line)
    log("-" * 112)
    for board_label in ['主板', '创业板', '科创板']:
        line = f"{board_label:<12}"
        for t in SURGE_THRESHOLDS:
            subset = [r for r in all_records if r['board'] == board_label and r['surge_ratio'] >= t]
            if subset:
                avg_r = sum(r['day_return'] for r in subset) / len(subset)
                wr = sum(1 for r in subset if r['day_return'] > 0) / len(subset) * 100
                line += f"{avg_r:+.2f}/{wr:.0f}%({len(subset)})".ljust(20)
            else:
                line += "N/A".ljust(20)
        log(line)

    # 八、市值×倍率交叉
    log(f"\n\n{'='*90}")
    log("八、市值 × 突增倍率 交叉统计（日均收益%/胜率%/样本数）")
    log("=" * 90)
    col_hdr = '市值/倍率'
    line = f"{col_hdr:<12}"
    for t in SURGE_THRESHOLDS:
        line += f"{'>=' + str(t) + 'x':<20}"
    log(line)
    log("-" * 112)
    for cap_label in ['<50亿', '50-200亿', '200-700亿', '>700亿']:
        line = f"{cap_label:<12}"
        for t in SURGE_THRESHOLDS:
            subset = [r for r in all_records if r['cap_group'] == cap_label and r['surge_ratio'] >= t]
            if subset:
                avg_r = sum(r['day_return'] for r in subset) / len(subset)
                wr = sum(1 for r in subset if r['day_return'] > 0) / len(subset) * 100
                line += f"{avg_r:+.2f}/{wr:.0f}%({len(subset)})".ljust(20)
            else:
                line += "N/A".ljust(20)
        log(line)

    # 九、结论
    log(f"\n\n{'='*90}")
    log("九、优化结论")
    log("=" * 90)
    if top10:
        best = top10[0]
        log(f"\n最优组合: [{best['surge']}] × [{best['cap']}] × [{best['filter']}]")
        log(f"  日均收益: {best['avg_ret']:+.2f}%")
        log(f"  胜率: {best['win_rate']:.1f}%")
        log(f"  正收益年份: {best['positive_years']}/{best['total_years']}")
        log(f"  年度最差: {best['worst_year']:+.2f}%")
        log(f"  年度最好: {best['best_year']:+.2f}%")
        log(f"  样本量: {best['n']}")
        log(f"  得分(日均×胜率): {best['score']:.3f}")

        target_ret, target_wr = 1.0, 55.0
        log(f"\n目标达成检查:")
        log(f"  日均收益>1%: {'✓' if best['avg_ret'] >= target_ret else '✗'} (实际={best['avg_ret']:+.2f}%)")
        log(f"  胜率>55%: {'✓' if best['win_rate'] >= target_wr else '✗'} (实际={best['win_rate']:.1f}%)")

        qualified = [c for c in combo_results if c['avg_ret'] >= target_ret and c['win_rate'] >= target_wr]
        log(f"\n共有 {len(qualified)} 个组合同时满足日均>1% + 胜率>55%:")
        for i, c in enumerate(qualified[:20], 1):
            log(f"  {i:>2}. [{c['surge']}]×[{c['cap']}]×[{c['filter']}] "
                f"ret={c['avg_ret']:+.2f}% wr={c['win_rate']:.1f}% n={c['n']} "
                f"正年={c['positive_years']}/{c['total_years']}")

        # 最稳定组合（正收益年份最多）
        stable_sorted = sorted(combo_results, key=lambda x: (x['positive_years'], x['score']), reverse=True)
        log(f"\n最稳定组合Top5（正收益年份最多）:")
        for i, c in enumerate(stable_sorted[:5], 1):
            log(f"  {i}. [{c['surge']}]×[{c['cap']}]×[{c['filter']}] "
                f"正年={c['positive_years']}/{c['total_years']} ret={c['avg_ret']:+.2f}% wr={c['win_rate']:.1f}% n={c['n']}")
    else:
        log("无有效组合结果。")

    log(f"\n{'='*90}")
    log("优化分析完成。")
    log_file.close()
    print(f"\n日志已保存到: {LOG_PATH}")


if __name__ == '__main__':
    main()
