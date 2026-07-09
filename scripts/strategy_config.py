"""
量化交易系统 - 策略配置文件
所有策略参数集中管理，支持快速A/B对比和网格搜索
"""

CONFIG = {
    # ===== 资金与仓位 =====
    'portfolio': {
        'initial_capital': 1_000_000,  # 初始资金100万
        'n_slots': 5,                  # 仓位数
    },

    # ===== 多指标评分配置 (v2 - 数据验证指标) =====
    # 7颗星: 3个trigger + 4个confirmation
    # 买入条件: 至少1个trigger亮 + 总星数 >= min_stars
    'indicators': {
        # --- TRIGGER 核心触发 (每个独立可触发买入考虑) ---
        'big_drop_gap_up': {
            'weight': 1.0, 'star_condition': '>0.5',
            'drop_threshold': -5.0, 'gap_min': 2.0, 'gap_max': 20.0,
        },
        'limit_down_reversal': {
            'weight': 1.0, 'star_condition': '>0.5',
        },
        'limit_up_next': {
            'weight': 1.0, 'star_condition': '>0.5',
        },
        # --- CONFIRMATION 确认加分 ---
        'cum_drop_first_positive': {
            'weight': 1.0, 'star_condition': '>0.5',
            'lookback': 10, 'cum_drop_threshold': -10.0,
        },
        'volume_breakout': {
            'weight': 1.0, 'star_condition': '>0.5',
            'lookback': 5, 'ratio': 2.5,
        },
        'amplitude_positive': {
            'weight': 1.0, 'star_condition': '>0.5',
            'amp_threshold': 5.0,
        },
        'rsi_oversold_bounce': {
            'weight': 1.0, 'star_condition': '>0.5',
            'period': 14, 'oversold': 30.0,
        },
    },
    'score_threshold': 3,  # 星级模式: 最低星数（至少1 trigger + 2 confirmation = 3星）

    # ===== 买入策略组合 =====
    'buy_strategies': ['score', 'vshape'],  # 启用的买入策略列表
    # 可选: 'score', 'vshape', 'surge7', 'vol_breakout', 'limitup_pullback'

    # ===== 卖出策略组合 =====
    'sell_strategies': ['fixed_tpsl', 'time_limit'],  # 启用的卖出策略
    'sell_params': {
        'fixed_tpsl': {'tp_pct': 10.0, 'sl_pct': -3.0},
        'trailing_stop': {'trail_pct': 5.0},
        'time_limit': {'max_hold_days': 5, 'exit_hour': 4},
        'volume_shrink': {'vol_shrink_ratio': 0.5, 'price_drop_pct': -1.0},
        'score_decay': {'min_score': 2.0},
    },

    # ===== 持仓管理策略 =====
    'position_strategy': 'equal_weight',
    # 可选: 'equal_weight', 'score_proportional', 'risk_parity', 'concentrated_top'

    # ===== 选股过滤 =====
    'filters': {
        'exclude_st': True,            # 排除ST股
        'min_market_cap': 5_000_000_000,  # 最低市值50亿
        'min_turn': 1.0,               # 最低换手率1%
        'board_filter': None,          # None=全部, 'sz.300'=创业板, 'sh.688'=科创板
    },

    # ===== 回测区间 =====
    'backtest_period': ('2021-01-01', '2026-06-30'),

    # ===== 合规配置 =====
    'compliance': {
        'check_limit_up': True,    # 涨停不买
        'check_limit_down': True,  # 跌停不卖
        'check_t1': True,          # T+1约束
        'check_one_word': True,    # 一字板不成交
    },

    # ===== 信号排序 =====
    'signal_sort': 'score_desc',  # 按评分降序选股
    # 可选: 'score_desc', 'turnover_desc', 'gap_up_asc'
}
