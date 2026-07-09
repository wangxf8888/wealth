#!/usr/bin/env python3
"""
MA5突破大盘股策略 - 精细化多维度交叉参数优化 (2021-2026)

7个维度交叉筛选:
  1. 市值细分: 700-1500亿 / 1500-3000亿 / 3000亿+
  2. 均线类型: MA5 / MA10 / 双确认 / MA5刚突破
  3. 成交量配合: 放量突破 / 缩量整理后突破 / 普通
  4. 突破强度: 微幅(0-1%) / 中度(1-3%) / 强突破(3%+)
  5. 前日K线: 阳线 / 阴线 / 十字星
  6. 连续性: 首次突破 / 回踩确认
  7. 行业板块: 金融 / 消费 / 科技 / 周期 / 医药 / 其他

T+0合规:
  - MA5/MA10用yesterday及之前的close计算
  - 突破判断: yesterday close < MA5_yesterday → today open > MA5_yesterday
  - 买入: today hour1_open
  - 收益: hour1_open → day close
"""
import sys
import sqlite3
from collections import defaultdict

# ========== 配置区 ==========
DB_PATH = '/home/AIWealth/data/stocks.db'
LOG_PATH = '/home/AIWealth/scripts/logs/ma_breakout_optimize.log'
MIN_TURNOVER = 1.0
MIN_MARKET_CAP = 700
MIN_SAMPLE_FOR_RANK = 100
TOP_N_COMBOS = 15
START_DATE = '2021-01-01'
END_DATE = '2026-06-30'
# ============================

INDUSTRY_KEYWORDS = {
    '金融': ['银行', '证券', '券商', '保险', '信托', '期货', '金融', '投资',
             '浦发', '招商银行', '工商银行', '建设银行', '农业银行', '中国银行',
             '交通银行', '民生银行', '兴业银行', '光大银行', '华夏银行', '平安银行',
             '中信银行', '北京银行', '南京银行', '宁波银行', '江苏银行', '上海银行',
             '中国人寿', '中国平安', '中国太保', '新华保险', '人保',
             '中信证券', '海通证券', '国泰君安', '华泰证券', '广发证券',
             '招商证券', '中金公司', '东方财富', '同花顺', '申万宏源'],
    '消费': ['白酒', '酒', '食品', '饮料', '乳业', '调味', '家电', '美的',
             '格力', '海尔', '茅台', '五粮液', '泸州', '洋河', '汾酒', '古井',
             '伊利', '蒙牛', '海天', '零售', '百货', '超市', '免税',
             '汽车', '长城汽车', '比亚迪', '长安汽车', '上汽', '广汽',
             '服装', '纺织', '家居', '旅游', '酒店'],
    '科技': ['科技', '电子', '芯片', '半导体', '软件', '信息', '通信', '光电',
             '互联网', '计算机', '海康', '大华', '科大讯飞', '韦尔', '兆易',
             '北方华创', '中微', '卓胜微', '澜起', '中芯', '长电',
             '传媒', '游戏', '网络', '数据', '中兴'],
    '周期': ['钢铁', '煤炭', '有色', '化工', '建材', '水泥', '石油', '石化',
             '矿业', '铝', '铜', '锂', '黄金', '稀土', '电力', '能源',
             '中国石油', '中国石化', '中国神华', '紫金矿业', '中国铝业',
             '万华化学', '宝钢', '海螺水泥', '天山股份',
             '航运', '港口', '铁路', '公路', '机场', '航空',
             '房地产', '地产', '万科', '保利'],
    '医药': ['医药', '药业', '制药', '生物', '医疗', '健康', '疫苗', '基因',
             '恒瑞', '药明', '迈瑞', '爱尔', '片仔癀', '云南白药',
             '长春高新', '智飞', '华兰', '器械', '诊断']
}


def classify_industry(code_name):
    if not code_name:
        return '其他'
    for industry, keywords in INDUSTRY_KEYWORDS.items():
        for kw in keywords:
            if kw in code_name:
                return industry
    return '其他'


def get_limit_ratio(code):
    if code.startswith('sz.300') or code.startswith('sz.301'):
        return 0.20
    elif code.startswith('sh.688'):
        return 0.20
    elif code.startswith('bj.'):
        return 0.30
    return 0.10


def calc_limit_up(preclose, code):
    return round(preclose * (1 + get_limit_ratio(code)), 2)


def log_print(f, msg):
    print(msg)
    f.write(msg + '\n')


def main():
    log_file = open(LOG_PATH, 'w', encoding='utf-8')
    def lp(msg): log_print(log_file, msg)

    lp(f"{'='*90}")
    lp(f"MA5突破大盘股策略 - 精细化多维度交叉参数优化")
    lp(f"区间: {START_DATE} ~ {END_DATE} | 最低市值: {MIN_MARKET_CAP}亿")
    lp(f"T+0合规: MA用yesterday及之前数据, 买入=today hour1_open, 收益=h1open→close")
    lp(f"{'='*90}\n")

    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()

    # 加载所有交易日
    cur.execute("SELECT DISTINCT date FROM stock_kline WHERE date >= ? AND date <= ? ORDER BY date",
                (START_DATE, END_DATE))
    all_days = [r[0] for r in cur.fetchall()]
    all_days_idx = {d: i for i, d in enumerate(all_days)}
    lp(f"交易日数: {len(all_days)} ({all_days[0]} ~ {all_days[-1]})")

    # 批量加载所有数据到内存
    lp("加载数据到内存...")
    cur.execute("""
        SELECT code, code_name, date, open, high, low, close, preclose, isST, turn,
               hour1_open, volume, amount
        FROM stock_kline
        WHERE date >= ? AND date <= ?
        ORDER BY code, date
    """, (START_DATE, END_DATE))

    # stock_data[code] = [(date, open, high, low, close, preclose, isST, turn, h1_open, volume, amount, code_name), ...]
    stock_data = defaultdict(list)
    for r in cur.fetchall():
        stock_data[r[0]].append({
            'code_name': r[1], 'date': r[2], 'open': r[3], 'high': r[4],
            'low': r[5], 'close': r[6], 'preclose': r[7], 'isST': r[8],
            'turn': r[9], 'h1_open': r[10], 'volume': r[11], 'amount': r[12]
        })

    lp(f"加载完成: {len(stock_data)} 只股票")
    conn.close()

    # 遍历每只股票,找出所有MA突破信号
    all_signals = []
    processed_stocks = 0

    for code, days_data in stock_data.items():
        if len(days_data) < 15:
            continue

        code_name = days_data[0]['code_name']
        # 排除ST
        if code_name and 'ST' in code_name.upper():
            continue

        industry = classify_industry(code_name)
        processed_stocks += 1

        # 逐日扫描 (从第15天开始, 确保有足够历史)
        for idx in range(15, len(days_data)):
            today = days_data[idx]
            yesterday = days_data[idx - 1]

            # 基础过滤
            if today['isST'] or yesterday['isST']:
                continue
            if today['date'] < START_DATE:
                continue

            t_open = today['open']
            t_close = today['close']
            t_preclose = today['preclose']
            t_h1_open = today['h1_open']
            t_high = today['high']
            t_low = today['low']
            t_volume = today['volume']
            t_amount = today['amount']
            t_turn = today['turn']

            yd_close = yesterday['close']
            yd_preclose = yesterday['preclose']
            yd_turn = yesterday['turn']
            yd_open = yesterday['open']
            yd_high = yesterday['high']
            yd_low = yesterday['low']
            yd_volume = yesterday['volume']

            if not all([t_open, t_close, t_preclose, yd_close, yd_preclose]):
                continue
            if t_open <= 0 or t_preclose <= 0 or yd_close <= 0:
                continue

            # 换手率过滤
            if not yd_turn or yd_turn < MIN_TURNOVER:
                continue

            # 排除yesterday涨停
            if yd_close >= calc_limit_up(yd_preclose, code):
                continue

            # 排除一字涨停
            if t_open == t_high == t_low == t_close and t_close >= calc_limit_up(t_preclose, code):
                continue

            # 估算流通市值
            if not t_amount or not yd_turn or yd_turn <= 0:
                continue
            market_cap = t_amount * 100 / yd_turn / 1e8
            if market_cap < MIN_MARKET_CAP:
                continue

            # 买入价
            buy_price = t_h1_open if t_h1_open and t_h1_open > 0 else t_open
            if buy_price <= 0:
                continue

            # 收益
            ret = (t_close - buy_price) / buy_price * 100

            # 构建close/volume历史序列 (到yesterday为止)
            closes = [days_data[i]['close'] for i in range(max(0, idx-15), idx)
                      if days_data[i]['close'] is not None]
            volumes = [days_data[i]['volume'] for i in range(max(0, idx-15), idx)
                       if days_data[i]['volume'] is not None]

            if len(closes) < 10:
                continue

            # MA计算 (用到yesterday为止的数据)
            ma5_yd = sum(closes[-5:]) / 5 if len(closes) >= 5 else None
            ma10_yd = sum(closes[-10:]) / 10 if len(closes) >= 10 else None

            # MA_T-2 (用到T-2为止)
            closes_t2 = closes[:-1]
            ma5_t2 = sum(closes_t2[-5:]) / 5 if len(closes_t2) >= 5 else None
            ma10_t2 = sum(closes_t2[-10:]) / 10 if len(closes_t2) >= 10 else None
            t2_close = closes_t2[-1] if closes_t2 else None

            # === 维度2: 均线类型判定 ===
            ma_types = []
            # MA5突破: T-2 close < MA5_T-2 且 today open > MA5_yesterday
            ma5_break = False
            if ma5_yd and ma5_t2 and t2_close is not None:
                if t2_close < ma5_t2 and t_open > ma5_yd:
                    ma5_break = True
                    ma_types.append('MA5')

            # MA10突破
            ma10_break = False
            if ma10_yd and ma10_t2 and t2_close is not None:
                if t2_close < ma10_t2 and t_open > ma10_yd:
                    ma10_break = True
                    ma_types.append('MA10')

            # 双确认
            if ma5_break and ma10_break:
                ma_types.append('双确认')

            # MA5刚突破(yesterday close < MA5_yesterday, today open > MA5_yesterday)
            if ma5_yd and yd_close < ma5_yd and t_open > ma5_yd:
                if 'MA5刚突破' not in ma_types:
                    ma_types.append('MA5刚突破')

            if not ma_types:
                continue

            # === 维度1: 市值细分 ===
            if market_cap < 1500:
                cap_cat = '700-1500亿'
            elif market_cap < 3000:
                cap_cat = '1500-3000亿'
            else:
                cap_cat = '3000亿+'

            # === 维度3: 成交量配合 ===
            vol_cat = '普通'
            if len(volumes) >= 5 and yd_volume and yd_volume > 0:
                vol_ma5 = sum(volumes[-5:]) / 5 if volumes[-5:] else 0
                if vol_ma5 > 0:
                    if yd_volume > vol_ma5 * 1.3:
                        vol_cat = '放量突破'
                    # 缩量整理后突破
                    if len(volumes) >= 3:
                        v_list = volumes[-3:]
                        if all(v and v > 0 for v in v_list):
                            if v_list[0] > v_list[1] > v_list[2]:
                                if t_volume and vol_ma5 > 0 and t_volume > vol_ma5 * 1.3:
                                    vol_cat = '缩量整理后突破'

            # === 维度4: 突破强度 ===
            ref_ma = ma5_yd if ma5_break else (ma10_yd if ma10_break else ma5_yd)
            if ref_ma and ref_ma > 0:
                pct_above = (t_open - ref_ma) / ref_ma * 100
                if pct_above < 1:
                    strength_cat = '微幅0-1%'
                elif pct_above < 3:
                    strength_cat = '中度1-3%'
                else:
                    strength_cat = '强3%+'
            else:
                strength_cat = '微幅0-1%'

            # === 维度5: 前日K线 ===
            kline_cat = '其他'
            if yd_open and yd_close and yd_high and yd_low and yd_open > 0:
                amplitude = (yd_high - yd_low) / yd_open * 100
                body = abs(yd_close - yd_open) / yd_open * 100
                if amplitude < 2 and body < 1:
                    kline_cat = '十字星'
                elif yd_close > yd_open:
                    kline_cat = '阳线'
                else:
                    kline_cat = '阴线'

            # === 维度6: 连续性 ===
            continuity_cat = '回踩确认'
            if ma5_yd and len(closes) >= 6:
                # 前5日(T-6到T-2)都在各自MA5下
                all_below = True
                for k in range(1, 6):
                    if idx - k - 1 < 0:
                        all_below = False
                        break
                    hist_idx = len(closes) - 1 - k  # closes中对应位置
                    if hist_idx < 4:
                        all_below = False
                        break
                    local_closes = closes[:hist_idx+1]
                    if len(local_closes) >= 5:
                        local_ma5 = sum(local_closes[-5:]) / 5
                        if local_closes[-1] >= local_ma5:
                            all_below = False
                            break
                    else:
                        all_below = False
                        break
                if all_below:
                    continuity_cat = '首次突破'

            # 为每个MA类型生成信号
            for ma_type in ma_types:
                all_signals.append({
                    'ret': ret,
                    'year': today['date'][:4],
                    'month': today['date'][:7],
                    'dim1_cap': cap_cat,
                    'dim2_ma': ma_type,
                    'dim3_vol': vol_cat,
                    'dim4_strength': strength_cat,
                    'dim5_kline': kline_cat,
                    'dim6_cont': continuity_cat,
                    'dim7_industry': industry,
                })

        if processed_stocks % 500 == 0:
            print(f"  已处理 {processed_stocks} 只股票, 信号数 {len(all_signals)}")

    lp(f"\n处理完成: {processed_stocks} 只股票, 总信号 {len(all_signals)}")

    if not all_signals:
        lp("无信号, 退出。")
        log_file.close()
        return

    # ========== 单维度分析 ==========
    dims = [
        ('dim1_cap', '维度1:市值细分'),
        ('dim2_ma', '维度2:均线类型'),
        ('dim3_vol', '维度3:成交量配合'),
        ('dim4_strength', '维度4:突破强度'),
        ('dim5_kline', '维度5:前日K线'),
        ('dim6_cont', '维度6:连续性'),
        ('dim7_industry', '维度7:行业板块'),
    ]

    lp(f"\n\n{'='*90}")
    lp(f"各单维度6年汇总表")
    lp(f"{'='*90}")

    for dim_key, dim_name in dims:
        lp(f"\n--- {dim_name} ---")
        lp(f"{'类别':<16}| {'样本数':<8}| {'日均收益':<12}| {'胜率':<10}| {'盈亏比':<10}| {'中位数':<10}| {'得分':<10}")
        lp(f"{'-'*86}")

        groups = defaultdict(list)
        for s in all_signals:
            groups[s[dim_key]].append(s['ret'])

        sorted_groups = sorted(groups.items(),
            key=lambda x: (sum(x[1])/len(x[1])) * (sum(1 for r in x[1] if r>0)/len(x[1])),
            reverse=True)

        for cat, rets in sorted_groups:
            n = len(rets)
            avg = sum(rets) / n
            wr = sum(1 for r in rets if r > 0) / n * 100
            median = sorted(rets)[n // 2]
            pos_avg = sum(r for r in rets if r > 0) / max(1, sum(1 for r in rets if r > 0))
            neg_avg = sum(r for r in rets if r <= 0) / max(1, sum(1 for r in rets if r <= 0))
            pnl = abs(pos_avg / neg_avg) if neg_avg != 0 else 99.9
            score = avg * wr / 100
            lp(f"{cat:<16}| {n:<8}| {avg:+.3f}%{'':>5}| {wr:.1f}%{'':>4}| {pnl:.2f}{'':>5}| {median:+.3f}%{'':>3}| {score:.4f}")

    # ========== 交叉组合分析 ==========
    lp(f"\n\n{'='*90}")
    lp(f"交叉组合Top {TOP_N_COMBOS}排行 (按 日均收益×胜率 排序, 样本>{MIN_SAMPLE_FOR_RANK})")
    lp(f"{'='*90}")

    dim_keys = ['dim1_cap', 'dim2_ma', 'dim3_vol', 'dim4_strength', 'dim5_kline', 'dim6_cont', 'dim7_industry']
    combo_results = []

    # 2维交叉
    for i in range(len(dim_keys)):
        for j in range(i+1, len(dim_keys)):
            dk1, dk2 = dim_keys[i], dim_keys[j]
            groups = defaultdict(list)
            for s in all_signals:
                groups[(s[dk1], s[dk2])].append(s['ret'])
            for (v1, v2), rets in groups.items():
                if len(rets) < MIN_SAMPLE_FOR_RANK:
                    continue
                avg = sum(rets) / len(rets)
                wr = sum(1 for r in rets if r > 0) / len(rets) * 100
                score = avg * wr / 100
                combo_results.append({
                    'desc': f"{v1} + {v2}",
                    'filters': {dk1: v1, dk2: v2},
                    'n': len(rets), 'avg': avg, 'wr': wr, 'score': score
                })

    # 3维交叉 (MA类型×市值×另一个)
    for od in dim_keys[2:]:
        groups = defaultdict(list)
        for s in all_signals:
            groups[(s['dim2_ma'], s['dim1_cap'], s[od])].append(s['ret'])
        for (v1, v2, v3), rets in groups.items():
            if len(rets) < MIN_SAMPLE_FOR_RANK:
                continue
            avg = sum(rets) / len(rets)
            wr = sum(1 for r in rets if r > 0) / len(rets) * 100
            score = avg * wr / 100
            combo_results.append({
                'desc': f"{v1} + {v2} + {v3}",
                'filters': {'dim2_ma': v1, 'dim1_cap': v2, od: v3},
                'n': len(rets), 'avg': avg, 'wr': wr, 'score': score
            })

    combo_results.sort(key=lambda x: x['score'], reverse=True)
    top_combos = combo_results[:TOP_N_COMBOS]

    lp(f"\n{'排名':<4}| {'组合描述':<50}| {'样本':<7}| {'日均收益':<10}| {'胜率':<8}| {'得分':<8}")
    lp(f"{'-'*100}")
    for rank, c in enumerate(top_combos, 1):
        lp(f"{rank:<4}| {c['desc']:<50}| {c['n']:<7}| {c['avg']:+.3f}%{'':>3}| {c['wr']:.1f}%{'':>2}| {c['score']:.4f}")

    # ========== Top 5逐年表现 ==========
    lp(f"\n\n{'='*90}")
    lp(f"Top 5 组合逐年表现")
    lp(f"{'='*90}")

    years = ['2021', '2022', '2023', '2024', '2025', '2026']

    for rank, combo in enumerate(top_combos[:5], 1):
        lp(f"\n--- 第{rank}名: {combo['desc']} (总样本{combo['n']}, 均收{combo['avg']:+.3f}%, 胜率{combo['wr']:.1f}%) ---")
        lp(f"{'年份':<7}| {'样本':<7}| {'日均收益':<10}| {'胜率':<8}| {'最大赚':<10}| {'最大亏':<10}| {'盈亏比':<8}")
        lp(f"{'-'*70}")

        filters = combo['filters']
        for year in years:
            year_rets = []
            for s in all_signals:
                if s['year'] != year:
                    continue
                match = all(s[k] == v for k, v in filters.items())
                if match:
                    year_rets.append(s['ret'])

            if not year_rets:
                lp(f"{year:<7}| {'0':<7}| {'N/A':<10}| {'N/A':<8}| {'N/A':<10}| {'N/A':<10}| {'N/A':<8}")
                continue

            avg = sum(year_rets) / len(year_rets)
            wr = sum(1 for r in year_rets if r > 0) / len(year_rets) * 100
            max_g = max(year_rets)
            max_l = min(year_rets)
            pos_a = sum(r for r in year_rets if r > 0) / max(1, sum(1 for r in year_rets if r > 0))
            neg_a = sum(r for r in year_rets if r <= 0) / max(1, sum(1 for r in year_rets if r <= 0))
            pnl = abs(pos_a / neg_a) if neg_a != 0 else 99.9
            lp(f"{year:<7}| {len(year_rets):<7}| {avg:+.3f}%{'':>3}| {wr:.1f}%{'':>2}| {max_g:+.2f}%{'':>3}| {max_l:+.2f}%{'':>3}| {pnl:.2f}")

    # ========== 目标筛选 ==========
    lp(f"\n\n{'='*90}")
    lp(f"目标筛选: 日均>1.5%, 胜率>62%, 样本>200")
    lp(f"{'='*90}")

    target = [c for c in combo_results if c['avg'] > 1.5 and c['wr'] > 62 and c['n'] > 200]
    target.sort(key=lambda x: x['score'], reverse=True)

    if target:
        lp(f"\n达标组合 {len(target)} 个:")
        lp(f"{'排名':<4}| {'组合描述':<50}| {'样本':<7}| {'日均收益':<10}| {'胜率':<8}| {'得分':<8}")
        lp(f"{'-'*100}")
        for rank, c in enumerate(target, 1):
            lp(f"{rank:<4}| {c['desc']:<50}| {c['n']:<7}| {c['avg']:+.3f}%{'':>3}| {c['wr']:.1f}%{'':>2}| {c['score']:.4f}")
    else:
        lp(f"\n未找到完全达标组合。放宽: 日均>1.2%, 胜率>60%, 样本>100:")
        relaxed = [c for c in combo_results if c['avg'] > 1.2 and c['wr'] > 60 and c['n'] > 100]
        relaxed.sort(key=lambda x: x['score'], reverse=True)
        if relaxed:
            lp(f"放宽后找到 {len(relaxed)} 个:")
            lp(f"{'排名':<4}| {'组合描述':<50}| {'样本':<7}| {'日均收益':<10}| {'胜率':<8}| {'得分':<8}")
            lp(f"{'-'*100}")
            for rank, c in enumerate(relaxed[:15], 1):
                lp(f"{rank:<4}| {c['desc']:<50}| {c['n']:<7}| {c['avg']:+.3f}%{'':>3}| {c['wr']:.1f}%{'':>2}| {c['score']:.4f}")
        else:
            lp("  放宽后仍无满足条件组合。")
            # 打印最接近的
            close_ones = [c for c in combo_results if c['avg'] > 1.0 and c['wr'] > 55 and c['n'] > 50]
            close_ones.sort(key=lambda x: x['score'], reverse=True)
            if close_ones:
                lp(f"\n  最接近(日均>1.0%, 胜率>55%, 样本>50) Top 10:")
                for rank, c in enumerate(close_ones[:10], 1):
                    lp(f"  {rank}. {c['desc']} | N={c['n']} | 均收{c['avg']:+.3f}% | 胜率{c['wr']:.1f}%")

    # ========== 总结 ==========
    lp(f"\n\n{'='*90}")
    lp(f"总结")
    lp(f"{'='*90}")
    if top_combos:
        best = top_combos[0]
        lp(f"\n  最优组合: {best['desc']}")
        lp(f"  样本数: {best['n']}, 日均收益: {best['avg']:+.3f}%, 胜率: {best['wr']:.1f}%")
        lp(f"  综合得分(均收×胜率): {best['score']:.4f}")
        lp(f"\n  基准(全量MA5>700亿): 日均+1.12%, 胜率59.6%")
        lp(f"  提升: 日均{best['avg']-1.12:+.3f}%, 胜率{best['wr']-59.6:+.1f}%")

    lp(f"\n{'='*90}")
    lp(f"完成。日志: {LOG_PATH}")
    log_file.close()


if __name__ == '__main__':
    main()
