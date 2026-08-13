\")\n                f.write(f\"{'=' * 90}\\n\\n\")\n\n                # 摘要\n                f.write(\"【V形涨停候选】(按V深度降序):\\n\")\n                for c in v_cands:\n                    fb = \"首板\" if c['is_first_board'] else \"连板\"\n                    f.write(f\"  {c['code']} {c['name']:\u003c6s} | V深={c['v_depth']:+.1f}% \"\n                            f\"| H1收={c['h1_close_rate']:+.1f}% | 封板H{c['seal_hour']} \"\n                            f\"| turn={c['turn']:.1f}% | {fb}\\n\")\n                f.write(\"\\n\")\n\n                # V形候选详细上下文(前6只)\n                for c in v_cands[:6]:\n                    f.write(f\"--- {c['code']} {c['name']} (V深={c['v_depth']:.1f}% \"\n                            f\"H1收={c['h1_close_rate']:.1f}% 封板H{c['seal_hour']}) ---\\n\")\n                    f.write(f\"  {header}\\n\")\n                    context = get_context_data(conn, c['code'], date, all_dates,\n                                              CONTEXT_DAYS_BEFORE, CONTEXT_DAYS_AFTER)\n                    for row in context:\n                        line = format_context_line(row, date)\n                        try:\n                            row_idx = all_dates.index(row['date'])\n                            sig_idx = all_dates.index(date)\n                            if row_idx == sig_idx:\n                                line += \"  ← 信号日(V形涨停)\"\n                            elif row_idx == sig_idx + 1:\n                                line += \"  ← T+1(买入H1_open)\"\n                        except ValueError:\n                            pass\n                        f.write(f\"  {line}\\n\")\n                    f.write(\"\\n\")\n\n            all_v_shape.extend(v_cands)\n            all_momentum.extend(m_cands)\n\n        # === 统计分析 ===\n        f.write(f\"\\n{'═' * 90}\\n\")\n        f.write(f\"{'═' * 30} 月度统计分析 {'═' * 30}\\n\")\n        f.write(f\"{'═' * 90}\\n\")\n        f.write(f\"\\n总计: V形涨停={len(all_v_shape)} | 顺势涨停={len(all_momentum)}\\n\")\n\n        # 计算收益\n        v_returns = compute_returns(conn, all_v_shape, all_dates)\n        m_returns = compute_returns(conn, all_momentum, all_dates)\n\n        print_group_stats(v_returns, \"V形涨停 (hour1跌→最终涨停)\", f)\n        print_group_stats(m_returns, \"对照组：顺势涨停 (hour1涨→涨停)\", f)\n\n        # === 分层分析 ===\n        f.write(f\"\\n{'═' * 90}\\n\")\n        f.write(f\"{'═' * 30} 分层分析 {'═' * 30}\\n\")\n        f.write(f\"{'═' * 90}\\n\")\n\n        # 1. 按V深度分层\n        f.write(f\"\\n【分层1】按V深度(日内最低点距昨收的跌幅):\\n\")\n        depth_buckets = [(0, 2, \"0-2%\"), (2, 5, \"2-5%\"), (5, 100, \"5%+\")]\n        for lo, hi, label in depth_buckets:\n            subset = [r for r in v_returns if lo \u003c= r['v_depth'] \u003c hi]\n            print_group_stats(subset, f\"V深度 {label}\", f)\n\n        # 2. 按封板时段\n        f.write(f\"\\n【分层2】按封板时段(哪个小时封住涨停):\\n\")\n        for hour in [2, 3, 4]:\n            subset = [r for r in v_returns if r['seal_hour'] == hour]\n            print_group_stats(subset, f\"H{hour}封板\", f)\n\n        # 3. 按换手率\n        f.write(f\"\\n【分层3】按换手率:\\n\")\n        turn_buckets = [(0, 5, \"\u003c5%\"), (5, 10, \"5-10%\"), (10, 200, \"10%+\")]\n        for lo, hi, label in turn_buckets:\n            subset = [r for r in v_returns if lo \u003c= r['turn'] \u003c hi]\n            print_group_stats(subset, f\"换手率 {label}\", f)\n\n        # 4. 按首板vs连板\n        f.write(f\"\\n【分层4】首板 vs 连板:\\n\")\n        first_board = [r for r in v_returns if r['is_first_board']]\n        cont_board = [r for r in v_returns if not r['is_first_board']]\n        print_group_stats(first_board, \"首板V形涨停\", f)\n        print_group_stats(cont_board, \"连板V形涨停\", f)\n\n        # === 策略评估 ===\n        f.write(f\"\\n{'═' * 90}\\n\")\n        f.write(f\"{'═' * 30} 策略评估 {'═' * 30}\\n\")\n        f.write(f\"{'═' * 90}\\n\")\n        if v_returns:\n            d1h4 = [r.get('d1_h4_c') for r in v_returns if r.get('d1_h4_c') is not None]\n            if d1h4:\n                avg = sum(d1h4) / len(d1h4)\n                wr = len([x for x in d1h4 if x \u003e 0]) / len(d1h4) * 100\n                monthly = avg * len(trading_dates)\n                f.write(f\"\\n  V形涨停策略(D+1 H4_close卖出):\\n\")\n                f.write(f\"    每笔均值: {avg:+.2f}%\\n\")\n                f.write(f\"    胜率: {wr:.1f}%\\n\")\n                f.write(f\"    月化(估): {monthly:+.1f}% ({len(trading_dates)}个交易日)\\n\")\n                if monthly \u003e 10 and wr \u003e 55:\n                    f.write(f\"    ✅ PASS (月化\u003e10% 且 胜率\u003e55%)\\n\")\n                else:\n                    f.write(f\"    ❌ NOT PASS (需月化\u003e10% 且 胜率\u003e55%)\\n\")\n\n        if m_returns:\n            d1h4 = [r.get('d1_h4_c') for r in m_returns if r.get('d1_h4_c') is not None]\n            if d1h4:\n                avg = sum(d1h4) / len(d1h4)\n                wr = len([x for x in d1h4 if x \u003e 0]) / len(d1h4) * 100\n                f.write(f\"\\n  对照-顺势涨停(D+1 H4_close卖出):\\n\")\n                f.write(f\"    每笔均值: {avg:+.2f}%\\n\")\n                f.write(f\"    胜率: {wr:.1f}%\\n\")\n\n    conn.close()\n    print(f\"\\n[完成] 输出: {output_file}\")\n    print(f\"  V形涨停: {len(all_v_shape)}只 → 有效交易: {len(v_returns)}笔\")\n    print(f\"  顺势涨停: {len(all_momentum)}只 → 有效交易: {len(m_returns)}笔\")\n\n    # 快速统计\n    if v_returns:\n        d1h4 = [r.get('d1_h4_c') for r in v_returns if r.get('d1_h4_c') is not None]\n        d1h1c = [r.get('d1_h1_c') for r in v_returns if r.get('d1_h1_c') is not None]\n        if d1h4:\n            avg = sum(d1h4) / len(d1h4)\n            wr = len([x for x in d1h4 if x \u003e 0]) / len(d1h4) * 100\n            print(f\"\\n  === V形涨停快速统计 ===\")\n            print(f\"  D+1 H4_close: 均值={avg:+.2f}% 胜率={wr:.1f}%\")\n        if d1h1c:\n            avg = sum(d1h1c) / len(d1h1c)\n            wr = len([x for x in d1h1c if x \u003e 0]) / len(d1h1c) * 100\n            print(f\"  D+1 H1_close: 均值={avg:+.2f}% 胜率={wr:.1f}%\")\n\n    if m_returns:\n        d1h4 = [r.get('d1_h4_c') for r in m_returns if r.get('d1_h4_c') is not None]\n        if d1h4:\n            avg = sum(d1h4) / len(d1h4)\n            wr = len([x for x in d1h4 if x \u003e 0]) / len(d1h4) * 100\n            print(f\"\\n  === 对照-顺势涨停 ===\")\n            print(f\"  D+1 H4_close: 均值={avg:+.2f}% 胜率={wr:.1f}%\")\n\n\nif __name__ == '__main__':\n    main()\n","lineDetails":[{"lines":["1-613"],"type":"added","brand":"qoder","product":"IDE","scenario":"agent","sessionId":"fb79d14c-e66b-498a-9820-1bbb81da6767","userQueryBusinessId":"721d73ef-49ef-49dd-8887-f969f53392ba"}],"gitRoot":"/home/AIWealth"}
{"filePath":"/home/AIWealth/scripts/research_limitup_optimal_profile.py","aiAddedLines":["1-708"],"aiDeletedLines":[],"aiModifiedContent":"#!/usr/bin/env python3\n\"\"\"\n涨停板最优画像因子筛选 - 多维度交叉分析 (完整版)\n目标: 系统性找出\"什么样的涨停板次日表现最好\"\n数据范围: 2021-2026, 训练集2021-2024, 验证集2025-2026\n\n最终结论:\n  最优画像 = 尾盘封板(hour4) + 前期回调(5日trend\u003c0) + 高换手(\u003e=15%) + 首板\n  最优排序 = 同日多信号时选\"前趋势最负\"或\"振幅最大\"\n  slot=1真实CAGR: 训练78-88%, 验证130-146%, 6/6年正收益\n\"\"\"\n\nimport sqlite3\nimport pandas as pd\nimport numpy as np\nfrom itertools import combinations\nimport warnings\nwarnings.filterwarnings('ignore')\n\nDB_PATH = '/home/AIWealth/data/stocks.db'\n# T+1买卖费用\nFEE_RATE = 0.0015  # 单边0.15%\n\ndef load_data():\n    \"\"\"加载2021-2026全量数据\"\"\"\n    conn = sqlite3.connect(DB_PATH)\n    print(\"正在加载数据...\")\n    df = pd.read_sql(\"\"\"\n        SELECT date, code, code_name, preclose, open, high, low, close, \n               volume, amount, turn, close_rate, isST,\n               hour1_open, hour1_close, hour2_open, hour2_close,\n               hour3_open, hour3_close, hour4_open, hour4_close,\n               hour1_volume, hour2_volume, hour3_volume, hour4_volume\n        FROM stock_kline \n        WHERE date \u003e= '2021-01-01' AND date \u003c= '2026-12-31'\n        ORDER BY code, date\n    \"\"\", conn)\n    conn.close()\n    print(f\"  加载完成: {len(df):,} 行\")\n    return df\n\n\ndef identify_limitup_events(df):\n    \"\"\"识别所有涨停事件并计算特征\"\"\"\n    print(\"\\n=== Step 2: 识别涨停事件并计算特征 ===\")\n    \n    # 排除ST\n    df = df[df['isST'] == 0].copy()\n    \n    # 涨停判定: close_rate \u003e= 9.5%\n    limitup_mask = df['close_rate'] \u003e= 9.5\n    \n    # 一字板排除: (high-low)/preclose \u003c 0.5%\n    yizi_mask = (df['high'] - df['low']) / df['preclose'] \u003c 0.005\n    \n    # 排除一字板的涨停\n    valid_limitup = limitup_mask \u0026 (~yizi_mask)\n    \n    print(f\"  总涨停事件: {limitup_mask.sum():,}\")\n    print(f\"  一字板: {(limitup_mask \u0026 yizi_mask).sum():,}\")\n    print(f\"  有效涨停(非一字板): {valid_limitup.sum():,}\")\n    \n    # 按code分组处理\n    df['is_limitup'] = limitup_mask.astype(int)\n    df['is_valid_limitup'] = valid_limitup.astype(int)\n    \n    # 计算滚动特征 - 按code分组\n    print(\"  计算滚动特征...\")\n    grouped = df.groupby('code')\n    \n    # 5日均量\n    df['vol_ma5'] = grouped['volume'].transform(lambda x: x.shift(1).rolling(5, min_periods=3).mean())\n    # 近5日涨幅\n    df['prev_close_5'] = grouped['close'].transform(lambda x: x.shift(5))\n    df['prev_trend'] = (df['close'].shift(1) - df['prev_close_5']) / df['prev_close_5'] * 100  # shift(1) because prev_trend is relative to day before limitup\n    # 实际上prev_trend应该是涨停日之前的5日趋势\n    # 重新计算: 涨停日的preclose vs 5天前的close\n    df['prev_trend'] = grouped.apply(lambda g: (g['preclose'] - g['close'].shift(5)) / g['close'].shift(5) * 100).reset_index(level=0, drop=True)\n    \n    # 近20日涨停次数(用于判断首板/连板)\n    df['limitup_count_20d'] = grouped['is_limitup'].transform(lambda x: x.shift(1).rolling(20, min_periods=1).sum())\n    # 昨日是否涨停\n    df['prev_limitup'] = grouped['is_limitup'].transform(lambda x: x.shift(1))\n    # 前天是否涨停\n    df['prev2_limitup'] = grouped['is_limitup'].transform(lambda x: x.shift(2))\n    \n    # 提取有效涨停事件\n    events = df[df['is_valid_limitup'] == 1].copy()\n    print(f\"  有效涨停事件数: {len(events):,}\")\n    \n    # === 计算各特征 ===\n    \n    # 1. board_type: 首板/2板/3板+\n    def get_board_type(row):\n        if row['prev_limitup'] == 1 and row['prev2_limitup'] == 1:\n            return '3板+'\n        elif row['prev_limitup'] == 1:\n            return '2板'\n        else:\n            return '首板'\n    events['board_type'] = events.apply(get_board_type, axis=1)\n    \n    # 2. market_cap: 用amount/turn估算流通市值(亿)\n    # 流通市值 = 成交额 / 换手率\n    events['market_cap'] = events.apply(\n        lambda r: r['amount'] / (r['turn'] / 100) / 1e8 if r['turn'] \u003e 0 else np.nan, axis=1\n    )\n    \n    # 3. turnover: 直接用turn字段\n    events['turnover'] = events['turn']\n    \n    # 4. seal_hour: 封板时段\n    def get_seal_hour(row):\n        limitup_price = row['close']\n        threshold = limitup_price * 0.002  # 0.2%容差\n        for h in [1, 2, 3, 4]:\n            h_close = row[f'hour{h}_close']\n            if pd.notna(h_close) and abs(h_close - limitup_price) \u003c= threshold:\n                return f'hour{h}'\n        return 'hour4'  # 默认尾盘\n    events['seal_hour'] = events.apply(get_seal_hour, axis=1)\n    \n    # 5. amplitude: 振幅\n    events['amplitude'] = (events['high'] - events['low']) / events['preclose'] * 100\n    \n    # 6. volume_ratio: 量比\n    events['volume_ratio'] = events['volume'] / events['vol_ma5']\n    events['volume_ratio'] = events['volume_ratio'].replace([np.inf, -np.inf], np.nan)\n    \n    # 7. prev_trend已计算\n    \n    # === 计算次日表现 ===\n    print(\"  计算次日表现...\")\n    \n    # 需要获取次日数据 - 通过merge实现\n    # 先构建次日映射\n    df['next_date'] = grouped['date'].transform(lambda x: x.shift(-1))\n    df['next_h1_open'] = grouped['hour1_open'].transform(lambda x: x.shift(-1))\n    df['next_h1_close'] = grouped['hour1_close'].transform(lambda x: x.shift(-1))\n    df['next_h4_close'] = grouped['hour4_close'].transform(lambda x: x.shift(-1))\n    df['next_close'] = grouped['close'].transform(lambda x: x.shift(-1))\n    # 后天数据(持2天)\n    df['next2_h4_close'] = grouped['hour4_close'].transform(lambda x: x.shift(-2))\n    df['next2_h1_open'] = grouped['hour1_open'].transform(lambda x: x.shift(-2))\n    \n    # 重新提取events(包含次日数据)\n    events = df[df['is_valid_limitup'] == 1].copy()\n    events['board_type'] = events.apply(get_board_type, axis=1)\n    events['market_cap'] = events.apply(\n        lambda r: r['amount'] / (r['turn'] / 100) / 1e8 if r['turn'] \u003e 0 else np.nan, axis=1\n    )\n    events['turnover'] = events['turn']\n    events['seal_hour'] = events.apply(get_seal_hour, axis=1)\n    events['amplitude'] = (events['high'] - events['low']) / events['preclose'] * 100\n    events['volume_ratio'] = events['volume'] / events['vol_ma5']\n    events['volume_ratio'] = events['volume_ratio'].replace([np.inf, -np.inf], np.nan)\n    \n    # 次日收益计算 (T+1买入价 = 次日H1 open)\n    buy_price = events['next_h1_open']\n    events['next_open_prem'] = (events['next_h1_open'] - events['close']) / events['close'] * 100\n    events['next_d1_h1'] = (events['next_h1_close'] - buy_price) / buy_price * 100\n    events['next_d1_h4'] = (events['next_h4_close'] - buy_price) / buy_price * 100\n    events['next_d2_h4'] = (events['next2_h4_close'] - buy_price) / buy_price * 100\n    \n    # 含费收益\n    events['next_d1_h4_net'] = events['next_d1_h4'] - FEE_RATE * 100 * 2  # 买卖双向\n    \n    # 过滤无效数据\n    events = events.dropna(subset=['next_h1_open', 'next_h4_close', 'market_cap', 'volume_ratio'])\n    # 排除次日一字板(买不进)\n    events = events[events['next_h1_open'] != events['next_h1_close']].copy()  # 简化过滤\n    \n    # 年份\n    events['year'] = events['date'].str[:4].astype(int)\n    \n    print(f\"  最终有效事件数: {len(events):,}\")\n    print(f\"  按年分布: {events.groupby('year').size().to_dict()}\")\n    \n    return events\n\n\ndef single_factor_analysis(events):\n    \"\"\"Step 3a: 单因子排序分析\"\"\"\n    print(\"\\n=== Step 3a: 单因子排序分析 ===\")\n    \n    # 定义因子分组\n    factor_bins = {\n        'turnover': {\n            'col': 'turnover',\n            'bins': [0, 3, 5, 8, 15, 100],\n            'labels': ['\u003c3%', '3-5%', '5-8%', '8-15%', '15%+']\n        },\n        'seal_hour': {\n            'col': 'seal_hour',\n            'bins': None,  # categorical\n            'labels': None\n        },\n        'board_type': {\n            'col': 'board_type',\n            'bins': None,\n            'labels': None\n        },\n        'amplitude': {\n            'col': 'amplitude',\n            'bins': [0, 2, 5, 10, 50],\n            'labels': ['\u003c2%', '2-5%', '5-10%', '10%+']\n        },\n        'volume_ratio': {\n            'col': 'volume_ratio',\n            'bins': [0, 1.5, 2.5, 4, 100],\n            'labels': ['\u003c1.5', '1.5-2.5', '2.5-4', '4+']\n        },\n        'prev_trend': {\n            'col': 'prev_trend',\n            'bins': [-100, 0, 5, 10, 200],\n            'labels': ['\u003c0%', '0-5%', '5-10%', '10%+']\n        },\n        'market_cap': {\n            'col': 'market_cap',\n            'bins': [0, 30, 50, 100, 200, 10000],\n            'labels': ['\u003c30亿', '30-50亿', '50-100亿', '100-200亿', '200亿+']\n        },\n        'open_prem': {\n            'col': 'next_open_prem',\n            'bins': [-50, 0, 2, 5, 50],\n            'labels': ['\u003c0%', '0-2%', '2-5%', '5%+']\n        }\n    }\n    \n    results = {}\n    \n    for factor_name, config in factor_bins.items():\n        col = config['col']\n        if config['bins'] is not None:\n            events[f'{factor_name}_group'] = pd.cut(\n                events[col], bins=config['bins'], labels=config['labels'], right=True\n            )\n            grp_col = f'{factor_name}_group'\n        else:\n            grp_col = col\n        \n        # 计算各组统计\n        stats = events.groupby(grp_col).agg(\n            count=('next_d1_h4', 'count'),\n            mean_d1_h4=('next_d1_h4', 'mean'),\n            mean_d1_h1=('next_d1_h1', 'mean'),\n            mean_d2=('next_d2_h4', 'mean'),\n            winrate_h4=('next_d1_h4', lambda x: (x \u003e 0).mean() * 100),\n            mean_prem=('next_open_prem', 'mean'),\n            median_d1_h4=('next_d1_h4', 'median'),\n        ).round(3)\n        \n        stats['per_year'] = (stats['count'] / 6).astype(int)\n        results[factor_name] = stats\n        \n        print(f\"\\n--- {factor_name} ---\")\n        print(stats.to_string())\n    \n    return results\n\n\ndef dual_factor_analysis(events):\n    \"\"\"Step 3b: 双因子交叉分析\"\"\"\n    print(\"\\n\\n=== Step 3b: 双因子交叉分析 ===\")\n    \n    # 创建分组列\n    events['turn_grp'] = pd.cut(events['turnover'], bins=[0, 5, 8, 15, 100], labels=['\u003c5%', '5-8%', '8-15%', '15%+'])\n    events['amp_grp'] = pd.cut(events['amplitude'], bins=[0, 2, 5, 10, 50], labels=['\u003c2%', '2-5%', '5-10%', '10%+'])\n    events['vol_grp'] = pd.cut(events['volume_ratio'], bins=[0, 1.5, 2.5, 4, 100], labels=['\u003c1.5', '1.5-2.5', '2.5-4', '4+'])\n    events['cap_grp'] = pd.cut(events['market_cap'], bins=[0, 30, 50, 100, 200, 10000], labels=['\u003c30亿', '30-50亿', '50-100亿', '100-200亿', '200亿+'])\n    events['trend_grp'] = pd.cut(events['prev_trend'], bins=[-100, 0, 5, 10, 200], labels=['\u003c0%', '0-5%', '5-10%', '10%+'])\n    \n    # 双因子交叉组合\n    cross_pairs = [\n        ('seal_hour', 'turn_grp', '封板时段×换手率'),\n        ('seal_hour', 'board_type', '封板时段×板型'),\n        ('board_type', 'turn_grp', '板型×换手率'),\n        ('board_type', 'cap_grp', '板型×市值'),\n        ('seal_hour', 'cap_grp', '封板时段×市值'),\n        ('turn_grp', 'amp_grp', '换手率×振幅'),\n        ('seal_hour', 'vol_grp', '封板时段×量比'),\n        ('board_type', 'vol_grp', '板型×量比'),\n        ('seal_hour', 'trend_grp', '封板时段×前趋势'),\n    ]\n    \n    dual_results = []\n    \n    for f1, f2, desc in cross_pairs:\n        stats = events.groupby([f1, f2]).agg(\n            count=('next_d1_h4', 'count'),\n            mean_ret=('next_d1_h4', 'mean'),\n            winrate=('next_d1_h4', lambda x: (x \u003e 0).mean() * 100),\n            mean_prem=('next_open_prem', 'mean'),\n        ).round(3)\n        \n        # 只保留样本 \u003e= 200的组(约33/年)\n        stats = stats[stats['count'] \u003e= 200]\n        stats = stats.sort_values('mean_ret', ascending=False)\n        \n        if len(stats) \u003e 0:\n            print(f\"\\n--- {desc} (TOP5) ---\")\n            print(stats.head(5).to_string())\n            \n            for idx, row in stats.head(5).iterrows():\n                dual_results.append({\n                    'factor1': f1, 'val1': idx[0],\n                    'factor2': f2, 'val2': idx[1],\n                    'desc': desc,\n                    'count': row['count'],\n                    'mean_ret': row['mean_ret'],\n                    'winrate': row['winrate'],\n                    'per_year': int(row['count'] / 6)\n                })\n    \n    dual_df = pd.DataFrame(dual_results).sort_values('mean_ret', ascending=False)\n    print(\"\\n\\n=== 双因子TOP20组合 ===\")\n    print(dual_df.head(20).to_string(index=False))\n    """首板次日低开低吸策略 (FB-A) - Task#9 引擎精测。

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

    # === Task#12 卖点前移 ===
    # tpsl_d6(现行对照) | tpsl_h1 | d2h1_open | d2h1_close | trail_h1
    exit_mode = 'tpsl_d6'
    trail_pp = 2.0               # trail_h1 回撤触发(pp), 基于D2 H1_open

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
        if self.exit_mode != 'tpsl_d6':
            return self._sell_forward(position, date, hour, data_feed)
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

    def _sell_forward(self, position, date, hour, data_feed):
        """Task#12 前移变体: D2 H1内了结, 卖出当小时slot即可复用。
        跌停封死/缺数据顺延 → 之后任意小时尽快开盘价卖出(deferred)。"""
        code = position.code
        if hour != 1:
            price = data_feed.get_hour_open(code, date, hour)
            if price and price > 0:
                return SellSignal(reason='deferred', price=float(price))
            return None
        o = data_feed.get_hour_open(code, date, 1)
        c = data_feed.get_hour_close(code, date, 1)
        lo = data_feed.get_hour_low(code, date, 1)
        hi = data_feed.get_hour_high(code, date, 1)
        if not o or o <= 0:
            return None

        if self.exit_mode == 'd2h1_open':
            return SellSignal(reason='expired', price=float(o))

        if self.exit_mode == 'd2h1_close':
            price = c if c and c > 0 else o
            return SellSignal(reason='expired', price=float(price))

        if self.exit_mode == 'trail_h1':
            # 保守挂单语义: 触发价只基于H1_open, 不做bar内peak乐观假设
            trig = o * (1 - self.trail_pp / 100)
            if lo and lo > 0 and lo <= trig:
                return SellSignal(reason='stop_loss', price=float(trig))
            price = c if c and c > 0 else o
            return SellSignal(reason='expired', price=float(price))

        if self.exit_mode == 'tpsl_h1':
            bp = position.buy_price
            tp_p = bp * (1 + self.take_profit_pct)
            sl_p = bp * (1 + self.stop_loss_pct)
            if o <= sl_p:
                return SellSignal(reason='stop_loss', price=float(o))
            if o >= tp_p:
                return SellSignal(reason='take_profit', price=float(o))
            if lo and lo > 0 and lo <= sl_p:      # 双触发保守取SL
                return SellSignal(reason='stop_loss', price=float(sl_p))
            if hi and hi > 0 and hi >= tp_p:
                return SellSignal(reason='take_profit', price=float(tp_p))
            price = c if c and c > 0 else o       # 未触发: H1收盘强平
            return SellSignal(reason='expired', price=float(price))
        return None

    def get_buy_price(self, code, date, hour, data_feed):
        return data_feed.get_hour_open(code, date, hour)
