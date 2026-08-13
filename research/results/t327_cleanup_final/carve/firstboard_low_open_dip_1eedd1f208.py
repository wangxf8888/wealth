 if rets:\n                avg = np.mean(rets)\n                win = sum(1 for x in rets if x \u003e 0) / len(rets) * 100\n                print(f\"    {layer['label']}: 样本{len(rets)}, 收益{avg:+.2f}%, 胜率{win:.1f}%\")\n\n                # 跟踪最佳组合\n                if len(rets) \u003e= 20:  # 最少20样本\n                    monthly_ret = avg * monthly_avg_count / n_months if n_months \u003e 0 else 0\n                    # 简单月化估算: 平均收益 * 月均信号数\n                    est_monthly = avg * (monthly_avg_count if monthly_avg_count \u003c 20 else 20)\n                    if est_monthly \u003e best_monthly:\n                        best_monthly = est_monthly\n                        best_combo = {\n                            'scenario': key,\n                            'scenario_name': r['name'],\n                            'turn_layer': layer['label'],\n                            'sample': len(rets),\n                            'avg_ret': avg,\n                            'win_rate': win,\n                            'monthly_signals': monthly_avg_count,\n                            'est_monthly': est_monthly,\n                        }\n            else:\n                print(f\"    {layer['label']}: 无数据\")\n\n        # 月度明细\n        print(f\"\\n  月度明细(T+1→T+2h4):\")\n        for month in sorted(r['monthly_stats'].keys()):\n            ms = r['monthly_stats'][month]\n            rets = ms['rets_t2h4']\n            if rets:\n                avg = np.mean(rets)\n                win = sum(1 for x in rets if x \u003e 0) / len(rets) * 100\n                print(f\"    {month}: 信号{ms['count']:3d}, 收益{avg:+.2f}%, 胜率{win:.0f}%\")\n\n    # 最佳组合\n    print(f\"\\n{'═' * 50}\")\n    print(f\"--- 最佳组合 ---\")\n    if best_combo:\n        print(f\"  场景{best_combo['scenario']}({best_combo['scenario_name']}) + {best_combo['turn_layer']}\")\n        print(f\"  样本: {best_combo['sample']}, 均收益: {best_combo['avg_ret']:+.2f}%, 胜率: {best_combo['win_rate']:.1f}%\")\n        print(f\"  月均信号: {best_combo['monthly_signals']:.1f}\")\n        print(f\"  估算月化收益: {best_combo['est_monthly']:+.2f}% (=均收益×min(月信号,20))\")\n        if best_combo['est_monthly'] \u003e 10:\n            print(f\"  ✓ 月化\u003e10%达标，有进一步研发价值\")\n        else:\n            print(f\"  ✗ 月化\u003c10%，alpha偏弱\")\n    else:\n        print(\"  无满足条件的组合(样本\u003e=20)\")\n\n\ndef print_cases(signals):\n    \"\"\"打印前15个案例的详细hour级数据\"\"\"\n    print(f\"\\n{'═' * 50}\")\n    print(f\"--- 前15个案例(场景B标准版) ---\")\n\n    conn = get_conn()\n    sigs = signals['B'][:15]\n\n    for i, sig in enumerate(sigs):\n        code = sig['code']\n        signal_date = sig['signal_date']\n\n        # 获取前5日+信号日+后5日\n        cursor = conn.cursor()\n        cursor.execute(\"\"\"\n            SELECT date, open_rate, close_rate, high_rate, low_rate,\n                   hour1_open_rate, hour1_close_rate, \n                   hour2_open_rate, hour2_close_rate,\n                   hour3_open_rate, hour3_close_rate,\n                   hour4_open_rate, hour4_close_rate,\n                   volume, turn\n            FROM stock_kline\n            WHERE code = ? AND date \u003e= date(?, '-10 days') AND date \u003c= date(?, '+10 days')\n            ORDER BY date\n        \"\"\", (code, signal_date, signal_date))\n        all_rows = cursor.fetchall()\n\n        # 找到信号日索引\n        sig_idx = None\n        for j, r in enumerate(all_rows):\n            if r['date'] == signal_date:\n                sig_idx = j\n                break\n        if sig_idx is None:\n            continue\n\n        # 取前5后5\n        start = max(0, sig_idx - 5)\n        end = min(len(all_rows), sig_idx + 6)\n        window_rows = all_rows[start:end]\n\n        print(f\"\\n[案例{i+1}] {code} {sig.get('code_name','')} | 信号日:{signal_date} | turn:{sig['turn']:.1f}% | vol_ratio:{sig['signal_vol_ratio']:.1f}x\")\n        print(f\"  {'日期':\u003c12} {'日OHLC_rate%':\u003e14} {'H1o/c%':\u003e10} {'H2o/c%':\u003e10} {'H3o/c%':\u003e10} {'H4o/c%':\u003e10} {'量(万)':\u003e8} {'换手':\u003e5}\")\n        print(f\"  {'─'*90}\")\n\n        for r in window_rows:\n            marker = \" \u003c\u003c\u003c\" if r['date'] == signal_date else \"\"\n            day_ohlc = f\"{r['open_rate'] or 0:+.1f}/{r['close_rate'] or 0:+.1f}/{r['high_rate'] or 0:+.1f}/{r['low_rate'] or 0:+.1f}\"\n            h1 = f\"{r['hour1_open_rate'] or 0:+.1f}/{r['hour1_close_rate'] or 0:+.1f}\"\n            h2 = f\"{r['hour2_open_rate'] or 0:+.1f}/{r['hour2_close_rate'] or 0:+.1f}\"\n            h3 = f\"{r['hour3_open_rate'] or 0:+.1f}/{r['hour3_close_rate'] or 0:+.1f}\"\n            h4 = f\"{r['hour4_open_rate'] or 0:+.1f}/{r['hour4_close_rate'] or 0:+.1f}\"\n            vol = (r['volume'] or 0) / 10000\n            turn = r['turn'] or 0\n            print(f\"  {r['date']:\u003c12} {day_ohlc:\u003e14} {h1:\u003e10} {h2:\u003e10} {h3:\u003e10} {h4:\u003e10} {vol:\u003e7.0f} {turn:\u003e5.1f}{marker}\")\n\n    conn.close()\n\n\ndef main():\n    print(\"=\" * 60)\n    print(\"缩量调整后放量阳线 - 次日交易机会研究\")\n    print(\"=\" * 60)\n\n    # 扫描信号\n    print(\"\\n[STEP1] 扫描信号...\")\n    signals = scan_signals()\n\n    for key in ['A', 'B', 'C']:\n        print(f\"  场景{key}({SCENARIOS[key]['name']}): {len(signals[key])}个信号\")\n\n    # 分析收益\n    print(\"\\n[STEP2] 分析T+1收益...\")\n    results = analyze_signals(signals)\n\n    # 输出结果\n    print_results(results)\n\n    # 案例展示\n    print_cases(signals)\n\n    # 结论\n    print(f\"\\n{'═' * 50}\")\n    print(\"--- 研究结论 ---\")\n    # 找全局最优\n    best = None\n    for key in ['A', 'B', 'C']:\n        r = results[key]\n        for layer_key in ['low', 'mid', 'high']:\n            rets = r['turn_layers'][layer_key]['rets_t2h4']\n            if rets and len(rets) \u003e= 15:\n                avg = np.mean(rets)\n                win = sum(1 for x in rets if x \u003e 0) / len(rets) * 100\n                if best is None or avg \u003e best['avg']:\n                    best = {'scenario': key, 'layer': layer_key, 'avg': avg, 'win': win, 'n': len(rets)}\n\n    if best:\n        print(f\"  最强alpha: 场景{best['scenario']} + {best['layer']}, 均值{best['avg']:+.2f}%, 胜率{best['win']:.1f}%, N={best['n']}\")\n        n_months = 24\n        monthly_est = best['avg'] * (best['n'] / n_months)\n        print(f\"  月化估算: {monthly_est:+.2f}% (样本{best['n']}/{n_months}月={best['n']/n_months:.1f}笔/月 × {best['avg']:+.2f}%)\")\n        if monthly_est \u003e 10:\n            print(f\"  → 月化\u003e10%，建议推进到第二阶段回测验证\")\n        else:\n            print(f\"  → 月化\u003c10%，alpha偏弱，需进一步优化或放弃\")\n    else:\n        print(\"  样本不足，无法得出可靠结论\")\n\n\nif __name__ == '__main__':\n    main()\n","lineDetails":[{"lines":["1-516"],"type":"added","brand":"qoder","product":"IDE","scenario":"agent","sessionId":"fb79d14c-e66b-498a-9820-1bbb81da6767","userQueryBusinessId":"3d993535-2031-452c-852f-eb242d274a47"}],"gitRoot":"/home/AIWealth"}
{"filePath":"/home/AIWealth/scripts/research_limitdown_bounce.py","aiAddedLines":["1-330"],"aiDeletedLines":[],"aiModifiedContent":"#!/usr/bin/env python3\n\"\"\"\n跌停反弹延续性研究 (优化版 - 向量化)\n逻辑: Day0跌停 -\u003e Day1反弹确认(高开或收阳) -\u003e Day2 hour1买入 -\u003e Day3/Day4卖出\n\"\"\"\n\nimport sqlite3\nimport pandas as pd\nimport numpy as np\n\nDB_PATH = '/home/AIWealth/data/stocks.db'\nSTART_DATE = '2024-01-01'\nEND_DATE = '2025-12-31'\n\n\ndef load_data():\n    \"\"\"加载数据 - 只取需要的字段\"\"\"\n    conn = sqlite3.connect(DB_PATH)\n    query = f\"\"\"\n    SELECT date, code, code_name, preclose, open, close, high, low, turn,\n           close_rate, open_rate,\n           hour1_open_rate, hour1_close_rate,\n           hour4_close_rate, isST\n    FROM stock_kline\n    WHERE date \u003e= '{START_DATE}' AND date \u003c= '{END_DATE}'\n      AND isST = 0\n      AND code NOT LIKE 'sh.68%'\n      AND code NOT LIKE 'bj.%'\n    ORDER BY code, date\n    \"\"\"\n    df = pd.read_sql(query, conn)\n    conn.close()\n    print(f\"加载数据: {len(df)} 行, {df['code'].nunique()} 只股票\")\n    return df\n\n\ndef find_signals_vectorized(df):\n    \"\"\"向量化查找信号\"\"\"\n    # 计算跌停标记\n    df['ratio'] = (df['close'] / df['preclose']).round(2)\n    \n    # 跌停阈值\n    is_gem = df['code'].str.startswith('sz.30')  # 创业板\n    is_star = df['code'].str.startswith('sh.68')  # 科创板\n    limit_threshold = np.where(is_gem | is_star, 0.80, 0.90)\n    df['is_limitdown'] = df['ratio'] \u003c= limit_threshold\n    \n    # 一字跌停判定\n    df['is_yizi'] = (df['open'] == df['close']) \u0026 (df['close'] == df['low'])\n    \n    # 用shift构造多日窗口 (同一code内)\n    df['next1_open_rate'] = df.groupby('code')['open_rate'].shift(-1)\n    df['next1_close_rate'] = df.groupby('code')['close_rate'].shift(-1)\n    df['next1_turn'] = df.groupby('code')['turn'].shift(-1)\n    df['next2_h1_open_rate'] = df.groupby('code')['hour1_open_rate'].shift(-2)\n    df['next2_h1_close_rate'] = df.groupby('code')['hour1_close_rate'].shift(-2)\n    df['next2_h4_close_rate'] = df.groupby('code')['hour4_close_rate'].shift(-2)\n    df['next2_close'] = df.groupby('code')['close'].shift(-2)\n    df['next2_close_rate'] = df.groupby('code')['close_rate'].shift(-2)\n    df['next1_close'] = df.groupby('code')['close'].shift(-1)\n    df['next3_h1_open_rate'] = df.groupby('code')['hour1_open_rate'].shift(-3)\n    df['next3_h1_close_rate'] = df.groupby('code')['hour1_close_rate'].shift(-3)\n    df['next3_h4_close_rate'] = df.groupby('code')['hour4_close_rate'].shift(-3)\n    df['next3_close'] = df.groupby('code')['close'].shift(-3)\n    df['next4_h1_close_rate'] = df.groupby('code')['hour1_close_rate'].shift(-4)\n    df['next4_close'] = df.groupby('code')['close'].shift(-4)\n    \n    # 用shift获取日期\n    df['next1_date'] = df.groupby('code')['date'].shift(-1)\n    df['next2_date'] = df.groupby('code')['date'].shift(-2)\n    df['next3_date'] = df.groupby('code')['date'].shift(-3)\n    \n    # 信号条件\n    # Day0: 跌停\n    cond_day0 = df['is_limitdown']\n    # Day1: 反弹确认 (高开或收阳)\n    cond_day1 = (df['next1_open_rate'] \u003e 0) | (df['next1_close_rate'] \u003e 0)\n    # Day2: hour1数据存在\n    cond_day2 = df['next2_h1_open_rate'].notna()\n    # Day3: hour1数据存在\n    cond_day3 = df['next3_h1_close_rate'].notna()\n    \n    signals = df[cond_day0 \u0026 cond_day1 \u0026 cond_day2 \u0026 cond_day3].copy()\n    print(f\"跌停事件总数: {df['is_limitdown'].sum()}\")\n    print(f\"跌停+Day1反弹: {(cond_day0 \u0026 cond_day1).sum()}\")\n    print(f\"有效信号(含Day2/3数据): {len(signals)}\")\n    \n    # 计算收益\n    # 买入: Day2 hour1 open price = Day1_close * (1 + next2_h1_open_rate/100)\n    signals['buy_price'] = signals['next1_close'] * (1 + signals['next2_h1_open_rate'] / 100)\n    \n    # 卖出1: Day3 hour1 close = Day2_close * (1 + next3_h1_close_rate/100)\n    signals['sell_d3h1'] = signals['next2_close'] * (1 + signals['next3_h1_close_rate'] / 100)\n    signals['ret_d3h1'] = (signals['sell_d3h1'] / signals['buy_price'] - 1) * 100\n    \n    # 卖出2: Day3 hour4 close = Day2_close * (1 + next3_h4_close_rate/100)\n    signals['sell_d3h4'] = signals['next2_close'] * (1 + signals['next3_h4_close_rate'] / 100)\n    signals['ret_d3h4'] = (signals['sell_d3h4'] / signals['buy_price'] - 1) * 100\n    \n    # 卖出3: Day4 hour1 close = Day3_close * (1 + next4_h1_close_rate/100)\n    mask_d4 = signals['next4_h1_close_rate'].notna() \u0026 signals['next3_close'].notna()\n    signals.loc[mask_d4, 'sell_d4h1'] = signals.loc[mask_d4, 'next3_close'] * (1 + signals.loc[mask_d4, 'next4_h1_close_rate'] / 100)\n    signals['ret_d4h1'] = np.nan\n    signals.loc[mask_d4, 'ret_d4h1'] = (signals.loc[mask_d4, 'sell_d4h1'] / signals.loc[mask_d4, 'buy_price'] - 1) * 100\n    \n    # 分类标签\n    signals['day1_open_class'] = pd.cut(\n        signals['next1_open_rate'],\n        bins=[-999, -1, 1, 3, 999],\n        labels=['低开\u003c-1%', '平开-1~+1%', '高开1-3%', '高开\u003e3%']\n    )\n    signals['day1_close_class'] = pd.cut(\n        signals['next1_close_rate'],\n        bins=[-999, -1, 1, 5, 999],\n        labels=['收跌\u003c-1%', '平收-1~+1%', '小涨1-5%', '大涨\u003e5%']\n    )\n    signals['day0_type'] = np.where(signals['is_yizi'], '一字跌停', '非一字跌停')\n    \n    # 换手率分类 (turn字段已是百分比，如4.26=4.26%)\n    signals['day0_turn_class'] = pd.cut(\n        signals['turn'],\n        bins=[-1, 3, 8, 9999],\n        labels=['换手\u003c3%', '换手3-8%', '换手\u003e8%']\n    )\n    \n    signals['month'] = signals['date'].str[:7]\n    \n    return signals\n\n\ndef print_group_stats(sdf, group_col, title):\n    \"\"\"按分组打印统计\"\"\"\n    print(f\"\\n{'='*60}\")\n    print(f\"--- {title} ---\")\n    print(f\"{'='*60}\")\n    print(f\"{'分组':\u003c16} {'样本':\u003e6} {'D3h1收益':\u003e9} {'D3h4收益':\u003e9} {'D4h1收益':\u003e9} {'D3h1胜率':\u003e9} {'D3h4胜率':\u003e9}\")\n    print('-' * 80)\n    \n    order_map = {\n        '高开\u003e3%': 0, '高开1-3%': 1, '平开-1~+1%': 2, '低开\u003c-1%': 3,\n        '大涨\u003e5%': 0, '小涨1-5%': 1, '平收-1~+1%': 2, '收跌\u003c-1%': 3,\n        '一字跌停': 0, '非一字跌停': 1,\n        '换手\u003c3%': 0, '换手3-8%': 1, '换手\u003e8%': 2,\n    }\n    \n    groups = sdf.groupby(group_col, observed=True)\n    sorted_keys = sorted(groups.groups.keys(), key=lambda x: order_map.get(str(x), 99))\n    \n    for key in sorted_keys:\n        g = groups.get_group(key)\n        n = len(g)\n        ret_d3h1 = g['ret_d3h1'].mean()\n        ret_d3h4 = g['ret_d3h4'].dropna().mean()\n        ret_d4h1 = g['ret_d4h1'].dropna().mean()\n        wr_d3h1 = (g['ret_d3h1'] \u003e 0).mean() * 100\n        wr_d3h4 = (g['ret_d3h4'].dropna() \u003e 0).mean() * 100\n        print(f\"{str(key):\u003c16} {n:\u003e6} {ret_d3h1:\u003e8.2f}% {ret_d3h4:\u003e8.2f}% {ret_d4h1:\u003e8.2f}% {wr_d3h1:\u003e8.1f}% {wr_d3h4:\u003e8.1f}%\")\n\n\ndef print_combo_stats(sdf):\n    \"\"\"最优条件组合\"\"\"\n    print(f\"\\n{'='*60}\")\n    print(\"--- 最优条件组合 (样本\u003e=15) ---\")\n    print(f\"{'='*60}\")\n    \n    combos = []\n    # 三维组合\n    for d1c, g1 in sdf.groupby('day1_close_class', observed=True):\n        for d0t, g2 in g1.groupby('day0_type', observed=True):\n            for turn, g3 in g2.groupby('day0_turn_class', observed=True):\n                if len(g3) \u003e= 15:\n                    combos.append({\n                        'combo': f\"{d1c}+{d0t}+{turn}\",\n                        'n': len(g3),\n                        'ret_d3h1': g3['ret_d3h1'].mean(),\n                        'ret_d3h4': g3['ret_d3h4'].dropna().mean(),\n                        'wr': (g3['ret_d3h1'] \u003e 0).mean() * 100,\n                    })\n    \n    # 二维组合: Day1收盘 + Day1开盘\n    for d1c, g1 in sdf.groupby('day1_close_class', observed=True):\n        for d1o, g2 in g1.groupby('day1_open_class', observed=True):\n            if len(g2) \u003e= 15:\n                combos.append({\n                    'combo': f\"{d1c}+{d1o}\",\n                    'n': len(g2),\n                    'ret_d3h1': g2['ret_d3h1'].mean(),\n                    'ret_d3h4': g2['ret_d3h4'].dropna().mean(),\n                    'wr': (g2['ret_d3h1'] \u003e 0).mean() * 100,\n                })\n    \n    # 二维: 跌停类型 + 换手\n    for d0t, g1 in sdf.groupby('day0_type', observed=True):\n        for turn, g2 in g1.groupby('day0_turn_class', observed=True):\n            if len(g2) \u003e= 15:\n                combos.append({\n                    'combo': f\"{d0t}+{turn}\",\n                    'n': len(g2),\n                    'ret_d3h1': g2['ret_d3h1'].mean(),\n                    'ret_d3h4': g2['ret_d3h4'].dropna().mean(),\n                    'wr': (g2['ret_d3h1'] \u003e 0).mean() * 100,\n                })\n    \n    if not combos:\n        print(\"无足够样本的组合\")\n        return\n    \n    combos_df = pd.DataFrame(combos).sort_values('ret_d3h1', ascending=False)\n    print(f\"{'条件组合':\u003c40} {'样本':\u003e5} {'D3h1收益':\u003e9} {'D3h4收益':\u003e9} {'胜率':\u003e7}\")\n    print('-' * 80)\n    for _, row in combos_df.head(25).iterrows():\n        d3h4_str = f\"{row['ret_d3h4']:.2f}%\" if not pd.isna(row['ret_d3h4']) else \"N/A\"\n        print(f\"{row['combo']:\u003c40} {row['n']:\u003e5} {row['ret_d3h1']:\u003e8.2f}% {d3h4_str:\u003e9} {row['wr']:\u003e6.1f}%\")\n\n\ndef print_monthly(sdf):\n    \"\"\"月度分布\"\"\"\n    print(f\"\\n{'='*60}\")\n    print(\"--- 月度分布 ---\")\n    print(f\"{'='*60}\")\n    print(f\"{'月份':\u003c10} {'样本':\u003e6} {'D3h1收益':\u003e9} {'D3h4收益':\u003e9} {'胜率':\u003e7}\")\n    print('-' * 50)\n    \n    for month in sorted(sdf['month'].unique()):\n        g = sdf[sdf['month'] == month]\n        n = len(g)\n        ret = g['ret_d3h1'].mean()\n        ret4 = g['ret_d3h4'].dropna().mean()\n        wr = (g['ret_d3h1'] \u003e 0).mean() * 100\n        print(f\"{month:\u003c10} {n:\u003e6} {ret:\u003e8.2f}% {ret4:\u003e8.2f}% {wr:\u003e6.1f}%\")\n\n\ndef print_top_cases(sdf, n=15):\n    \"\"\"Top案例hour级明细\"\"\"\n    print(f\"\\n{'='*60}\")\n    print(f\"--- 前{n}个最佳案例 (按D3h4收益) ---\")\n    print(f\"{'='*60}\")\n    \n    top = sdf.dropna(subset=['ret_d3h4']).nlargest(n, 'ret_d3h4')\n    \n    for _, row in top.iterrows():\n        print(f\"\\n{row['code']} {row['code_name']} | Day0={row['date']} 跌停{row['close_rate']:.1f}%\")\n        print(f\"  Day1({row['next1_date']}): 开{row['next1_open_rate']:+.1f}% 收{row['next1_close_rate']:+.1f}% | 类型={row['day0_type']}\")\n        print(f\"  Day2({row['next2_date']}): 买入h1open_rate={row['next2_h1_open_rate']:+.2f}%\")\n        d2h1 = row.get('next2_h1_close_rate', np.nan)\n        d2h4 = row.get('next2_h4_close_rate', np.nan)\n        print(f\"           h1_close_rate={d2h1:+.2f}% h4_close_rate={d2h4}\" if not pd.isna(d2h4) else f\"           h1_close_rate={d2h1:+.2f}%\")\n        d3h1 = row.get('next3_h1_close_rate', np.nan)\n        d3h4 = row.get('next3_h4_close_rate', np.nan)\n        print(f\"  Day3({row['next3_date']}): h1_rate={d3h1:+.2f}% h4_rate={d3h4}\" if not pd.isna(d3h4) else f\"  Day3: h1_rate={d3h1:+.2f}%\")\n        r4 = row['ret_d4h1']\n        if not pd.isna(r4):\n            print(f\"  收益: D3h1={row['ret_d3h1']:+.2f}% D3h4={row['ret_d3h4']:+.2f}% D4h1={r4:+.2f}%\")\n        else:\n            print(f\"  收益: D3h1={row['ret_d3h1']:+.2f}% D3h4={row['ret_d3h4']:+.2f}%\")\n\n\ndef main():\n    print(\"=\" * 60)\n    print(\"=== 跌停反弹延续性研究 ===\")\n    print(f\"=== 时间范围: {START_DATE} ~ {END_DATE} ===\")\n    print(\"=== 信号: Day0跌停 → Day1反弹 → Day2h1买 → Day3/4卖 ===\")\n    print(\"=\" * 60)\n    \n    df = load_data()\n    \n    print(\"\\n正在扫描跌停反弹信号(向量化)...\")\n    signals = find_signals_vectorized(df)\n    \n    if signals.empty:\n        print(\"无信号，退出\")\n        return\n    \n    # 过滤异常值\n    signals = signals[signals['ret_d3h1'].between(-30, 30)].copy()\n    \n    # 总体统计\n    print(f\"\\n{'='*60}\")\n    print(\"--- 总体统计 ---\")\n    print(f\"{'='*60}\")\n    print(f\"总样本: {len(signals)}\")\n    print(f\"D3h1平均收益: {signals['ret_d3h1'].mean():.3f}%\")\n    print(f\"D3h4平均收益: {signals['ret_d3h4'].dropna().mean():.3f}%\")\n    print(f\"D4h1平均收益: {signals['ret_d4h1'].dropna().mean():.3f}%\")\n    print(f\"D3h1胜率: {(signals['ret_d3h1']\u003e0).mean()*100:.1f}%\")\n    print(f\"D3h4胜率: {(signals['ret_d3h4'].dropna()\u003e0).mean()*100:.1f}%\")\n    print(f\"D3h1中位收益: {signals['ret_d3h1'].median():.3f}%\")\n    print(f\"D3h1标准差: {signal"""首板次日低开低吸策略 (FB-A) - Task#9 引擎精测。

形态(来自 Task#7 全维度网格复审, 研究口径见 data/realtime/task7_refine.txt):
  昨日(D0)首板涨停(连板数==1, 非一字) → 今日(D1)竞价低开 -4% <= open_rate < -2%
  → 昨日红盘占比 red_ratio >= 60(强势日错杀) → H1_open 买入
  → 退出 tp+6% / sl-10%, 最长5个监控日(D6收盘兜底)
排序: D0换手升序(低换手=筹码未松动优先) — task7_top1_probe:
  top1 +1.50%/日 2021-2026 六年全正; 注意换手降序方向则失效(-0.31%)。

研究口径(池子级 tp6sl-10, 毛收益): n=1574 胜率58.4% 均值+0.73% 5正1平
引擎精测(2021-01~2026-07-15, slot=1, 含成本): CAGR +72.53% | MDD 44.81%
  | 219笔 胜率65.75% +1.62%/笔 | 唯一负年 2023 -7.9% → 观察池第一顺位
red_ratio 权威源=index_kline(sh.000001) red_ratio 列, 2026-07-23 前已补齐
(Task#10 Robin 修复); 缺失日显式跳过不买入作为防御, 不引入未来数据。

Task#12 卖点前移(exit_mode 参数化, 买入逻辑不动):
  tpsl_d6   现行对照: tp6/sl-10 挂单监控 D2..D6, D6收盘兜底
  tpsl_h1   挂单仅监控 D2 H1, 未触发 H1收盘强平 (D2起slot当小时可复用)
  d2h1_open / d2h1_close / trail_h1  同 B2-A 语义
依据: Lee时段结构(H1唯一系统性正时段) x Task#9归因(hold长占slot致信号捕获减半)。
"""
import sqlite3

import numpy as np

from strategies.base import Strategy, Signal, SellSignal

_DB = '/home/AIWealth/data/stocks.db'


def _load_red_map() -> dict:
    """{date: red_ratio(float)}; 缺失日不在map中, 调用方显式跳过。"""
    out = {}
    try:
        conn = sqlite3.connect(_DB)
        for d, r in conn.execute(
                "SELECT date, red_ratio FROM index_kline "
                "WHERE code='sh.000001'"):
            if r is not None:
                out[d] = float(r)
        conn.close()
    except sqlite3.Error:
        pass
    return out


class FirstboardLowOpenDipStrategy(Strategy):
    """FB-A: 昨日首板 → 低开-4~-2 → 红盘>=60 → D0低换手优先 → H1低吸。"""

    name = "firstboard_low_open_dip"
    max_hold_hours = 20          # 监控D2..D6, D6收盘兜底(同研究口径5日窗口)
    sell_day_no_buy = True
    buy_hour = 1

    # === 核心参数(Task#7 网格最优) ===
    open_rate_min = -4.0         # 竞价低开下限
    open_rate_max = -2.0         # 竞价低开上限(不含)
    red_ratio_min = 60.0         # 昨日红盘占比下限(D0收盘后已知)
    min_history_bars = 20        # 上市未满20根K线不买(对齐研究口径 nth>=20)

    # 卖出参数显式声明(fixed模式)
    take_profit_pct = 0.06       # 止盈 +6%
    stop_loss_pct = -0.10        # 止损 -10%

    def __init__(self):
        self._cand_codes = []
        self._cand_date = None
        self._red_map = None

    def _red_ratio(self, date: str):
        if self._red_map is None:
            self._red_map = _load_red_map()
        return self._red_map.get(date)

    def get_candidates(self, date, data_feed):
        self._cand_codes, self._cand_date = [], date

        d0 = data_feed._prev_trading_date(date)   # 昨日(首板日)
        if not d0:
            return []
        dm1 = data_feed._prev_trading_date(d0)    # 前日(须未涨停)
        if not dm1:
            return []

        # 情绪过滤: 昨日红盘占比 >= 60; 缺失日显式跳过(不引入未来数据)
        red = self._red_ratio(d0)
        if red is None or red < self.red_ratio_min:
            return []

        f_today = data_feed._load_day(date)
        f_d0 = data_feed._load_day(d0)
        f_dm1 = data_feed._load_day(dm1)
        if any(f is None or f.empty for f in (f_today, f_d0, f_dm1)):
            return []

        codes = f_today.index
        is20 = codes.str.startswith('sz.30') | codes.str.startswith('sh.688')
        ratio = np.where(is20, 0.20, 0.10)

        def limit_flags(frame):
            sub = frame.reindex(codes)
            pre = sub['preclose'].to_numpy(dtype=float, na_value=0.0)
            clo = sub['close'].to_numpy(dtype=float, na_value=0.0)
            opn = sub['open'].to_numpy(dtype=float, na_value=0.0)
            lp = np.round(pre * (1 + ratio), 2)
            valid = (pre > 0) & (clo > 0)
            is_lim = valid & (clo >= lp - 0.001)
            is_yizi = is_lim & (opn > 0) & (opn >= lp - 0.001)
            return valid, is_lim, is_yizi

        v0, lim0, yizi0 = limit_flags(f_d0)
        v1, lim1, _ = limit_flags(f_dm1)

        # 昨日首板(非一字) + 前日未涨停(=恰好第1板)
        pattern = v0 & v1 & lim0 & ~yizi0 & ~lim1

        # 今日竞价窗口 + 基础池过滤
        orate = f_today['open_rate'].to_numpy(dtype=float, na_value=np.nan)
        opn = f_today['open'].to_numpy(dtype=float, na_value=0.0)
        win = (orate >= self.open_rate_min) & (orate < self.open_rate_max)
        not_bj = ~codes.str.startswith('bj.')
        st = f_today['isST'].fillna(0).astype(int).to_numpy() > 0
        if 'code_name' in f_today.columns:
            st = st | f_today['code_name'].fillna('').astype(str) \
                .str.upper().str.contains('ST').to_numpy()

        mask = pattern & win & (opn > 0) & not_bj & ~st
        if not mask.any():
            return []

        # 排序: D0换手升序(低换手优先, 买入时刻可得)
        d0_turn = f_d0.reindex(codes)['turn'] \
            .to_numpy(dtype=float, na_value=np.inf)
        idx = np.where(mask)[0]
        idx = idx[np.argsort(d0_turn[idx], kind='stable')]

        final = []
        for i in idx:
            code = codes[i]
            hist = data_feed.get_stock_history(code, date,
                                               self.min_history_bars)
            if len(hist) < self.min_history_bars:
                continue
            final.append(code)

        self._cand_codes = final
        return final

    def should_buy(self, code, date, hour, data_feed, portfolio):
        if hour != self.buy_hour:
            return None
        if date != self._cand_date or code not in self._cand_codes:
            return None
        price = data_feed.get_hour_open(code, date, hour)
        if not price or price <= 0:
            return None
        limit_up, _ = data_feed.get_limit_prices(code, date)
        if limit_up > 0 and price >= limit_up - 0.001:
            return None
        return Signal(code=code, price=float(price), strategy_name=self.name,
                      target_hold_hours=self.max_hold_hours)

    def should_sell(self, position, date, hour, data_feed):
        # T+1: 买入当日不卖(框架另有兜底)
        if position.buy_date >= date:
            return None
        buy_price = position.buy_price
        if buy_price <= 0:
            return None

        tp_target = buy_price * (1 + self.take_profit_pct)
        sl_target = buy_price * (1 + self.stop_loss_pct)

        bar_open = data_feed.get_hour_open(position.code, date, hour)
        bar_high = data_feed.get_hour_high(position.code, date, hour)
        bar_low = data_feed.get_hour_low(position.code, date, hour)
        if not bar_open or bar_open <= 0:
            return None

        # 跳空穿越按开盘成交(先判SL, 保守)
        open_pnl = (bar_open - buy_price) / buy_price
        if open_pnl <= self.stop_loss_pct:
            return SellSignal(reason='stop_loss', price=float(bar_open))
        if open_pnl >= self.take_profit_pct:
            return SellSignal(reason='take_profit', price=float(bar_open))

        # 盘中触发: 同小时TP/SL双触发保守取SL(小时内OHLC顺序未知)
        sl_hit = bar_low and bar_low > 0 and bar_low <= sl_target
        tp_hit = bar_high and bar_high > 0 and bar_high >= tp_target
        if sl_hit:
            return SellSignal(reason='stop_loss', price=float(sl_target))
        if tp_hit:
            return SellSignal(reason='take_profit', price=float(tp_target))

        # 5日未触发: D6 hour4 收盘兜底退出
        if position.hours_held >= self.max_hold_hours and hour == 4:
            price = data_feed.get_hour_close(position.code, date, 4)
            if not price or price <= 0:
                price = bar_open
            return SellSignal(reason='expired', price=float(price))
        return None

    def get_buy_price(self, code, date, hour, data_feed):
        return data_feed.get_hour_open(code, date, hour)
