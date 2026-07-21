"""实盘策略配置 - 修改此文件即可增减策略，无需改框架代码。

设计原则:
- 框架是策略无关的
- 新增策略 = 在此文件ACTIVE_STRATEGIES中加一项 + 在strategies/目录放策略文件
- 所有策略类必须继承 strategies.base.Strategy 并实现标准接口
"""

# =============================================================================
# 活跃策略列表（从strategies/目录动态import）
# =============================================================================
ACTIVE_STRATEGIES = [
    {
        'module': 'strategies.limitup_early_seal',
        'class': 'LimitupEarlySealStrategy',
        'slot_id': 'S1',
        'capital_ratio': 0.20,
        'enabled': True,
        'description': '涨停早封低开买入 | TP=+8% SL=-15% MaxHold=D+2',
    },
    {
        'module': 'strategies.amplitude_reversal',
        'class': 'AmplitudeReversalStrategy',
        'slot_id': 'S2',
        'capital_ratio': 0.20,
        'enabled': True,
        'description': '大上影冲高回落次日低开反弹 | TP=+5% SL=-15% MaxHold=D+2',
    },
    {
        'module': 'strategies.gem_star_late_seal',
        'class': 'GemStarLateSealStrategy',
        'slot_id': 'S3',
        'capital_ratio': 0.20,
        'enabled': True,
        'description': '创业板/科创板晚封涨停次日 | TP=+12% SL=-20% MaxHold=D+2',
    },
    {
        'module': 'strategies.big_yang_low_open_v2',
        'class': 'BigYangLowOpenV2Strategy',
        'slot_id': 'S4',
        'capital_ratio': 0.20,
        'enabled': True,
        'description': '大阳线次日低开反弹 | 参见策略文件',
    },
    {
        'module': 'strategies.gem_star_limitup_low_open',
        'class': 'GemStarLimitupLowOpenStrategy',
        'slot_id': 'S5',
        'capital_ratio': 0.20,
        'enabled': True,
        'description': '创/科涨停次日低开 | 参见策略文件',
    },
]

# =============================================================================
# 全局配置
# =============================================================================
TOTAL_CAPITAL = 1000000          # 总资金(元)
MAX_SLOT_PER_STRATEGY = 1       # 每策略最多同时持仓数
DATA_DB = '/home/AIWealth/data/stocks.db'
OUTPUT_DIR = '/home/AIWealth/data/realtime'
LOG_DIR = '/home/AIWealth/logs/realtime'

# 动态再平衡模式说明:
# - 回测引擎 (backtest/portfolio.py): buy_amount = get_nav() / n_slots  ✅ 已实现
# - 实盘 (realtime/position_tracker.py): buy_amount = total_nav / 5    ✅ 已实现
# - 两者逻辑完全一致: 每次买入时计算当前总净值/5，不是让每个slot独立运行
# - 举例: 初始100万, S1赚100%后总资金120万, 下次每slot买入24万(=120/5)

# 大盘过滤 - 前日上证指数跌幅超过此阈值则今日不开新仓
MARKET_FILTER_THRESHOLD = -1.0   # (%)

# 候选股排序后取top-N展示
CANDIDATE_TOP_N = 20
