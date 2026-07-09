"""回测引擎V3 - 模块化多策略引擎"""
from .backtest_engine import BacktestEngineV3
from .portfolio import Portfolio
from .buy_module import BuyModule, GapUpBuyStrategy
from .buy_strategy_zhaban import ZhaBanBuyStrategy
from .buy_strategy_reversal import ReversalBuyStrategy
from .buy_strategy_first_neg import FirstNegBuyStrategy
from .buy_strategy_combined import CombinedBuyStrategy
from .buy_strategy_limitdown import LimitDownReversalBuyStrategy
from .buy_strategy_intraday import IntradayMomentumBuyStrategy
from .buy_strategy_bigdrop import BigDropBounceBuyStrategy
from .buy_strategy_big_drop import BigDropBuyStrategy
from .buy_strategy_limitup_pullback import LimitUpPullbackBuyStrategy
from .buy_strategy_limitup_next import LimitUpNextBuyStrategy
# 数据分析TOP5策略
from .buy_strategy_shrink_volume import ShrinkVolumeBuyStrategy
from .buy_strategy_zhaban_rebound import ZhaBanReboundBuyStrategy
from .buy_strategy_low_turnover import LowTurnoverStableBuyStrategy
from .buy_strategy_low_open_rebound import LowOpenReboundBuyStrategy
from .buy_strategy_mid_turnover_momentum import MidTurnoverMomentumBuyStrategy
from .buy_strategy_zhaban_deep import ZhaBanDeepBuyStrategy
from .buy_strategy_vol_breakout import VolBreakoutBuyStrategy
from .buy_strategy_firstboard_dip import FirstBoardDipBuyStrategy
from .buy_strategy_firstboard_gapup import FirstBoardGapUpBuyStrategy
# 尾盘异动次日策略
from .buy_strategy_tail_momentum import TailMomentumBuyStrategy
# MA5均线突破大盘股策略
from .buy_strategy_ma5_breakout import MA5BreakoutBuyStrategy
from .sell_module import SellModule, NextDayCloseSellStrategy, StopLossSellStrategy
from .sell_strategies import (
    SellStrategy, FixedTPSL, TrailingStop, TimeLimitExit,
    VolumeShrinkExit, ScoreDecayExit, SellStrategyManager,
    SELL_STRATEGY_REGISTRY, create_sell_strategy, create_default_manager
)
from .blacklist import Blacklist
from .output_formatter import OutputFormatter
# 持仓管理策略
from .position_strategies import (
    PositionStrategy, EqualWeight, ScoreProportional,
    RiskParity, ConcentratedTop,
    POSITION_STRATEGY_REGISTRY, create_position_strategy
)
