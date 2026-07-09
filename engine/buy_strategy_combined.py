"""双策略组合买入模块 (Combined: ZhaBan + GapUp)

逻辑：
- 每个交易日，同时生成两类候选股
- 买入时先尝试ZhaBan信号，如果没有再尝试GapUp信号
- 不同策略的卖出参数可以独立配置
"""
from typing import List, Optional
import pandas as pd
from .buy_module import BuyModule, GapUpBuyStrategy
from .buy_strategy_zhaban import ZhaBanBuyStrategy


class CombinedBuyStrategy(BuyModule):
    """ZhaBan + GapUp 双策略组合买入"""

    def __init__(self, zhaban_fallback: float = 0.99,
                 open_gap_min: float = 4.0, close_rate_max: float = 7.0,
                 turn_max: float = 2.0, min_amount: float = 5_000_000):
        self.zhaban = ZhaBanBuyStrategy(zhaban_fallback=zhaban_fallback, buy_hour=1)
        self.gapup = GapUpBuyStrategy(
            open_gap_min=open_gap_min,
            close_rate_max=close_rate_max,
            turn_max=turn_max,
            min_amount=min_amount,
            buy_hour=4,
        )
        # 分别存储候选股
        self._zhaban_candidates = []
        self._gapup_candidates = []

    def get_candidates(self, date: str, day_data: pd.DataFrame,
                       prev_data: pd.DataFrame, blacklist) -> List[dict]:
        """合并两个策略的候选股"""
        self._zhaban_candidates = self.zhaban.get_candidates(date, day_data, prev_data, blacklist)
        self._gapup_candidates = self.gapup.get_candidates(date, day_data, prev_data, blacklist)

        # 返回合并列表（标记来源策略）
        combined = []
        for c in self._zhaban_candidates:
            c['_strategy'] = 'zhaban'
            combined.append(c)
        for c in self._gapup_candidates:
            c['_strategy'] = 'gapup'
            combined.append(c)
        return combined

    def should_buy(self, candidates: List[dict], date: str, hour: int,
                   hour_data: dict, portfolio, day_data: pd.DataFrame = None) -> Optional[dict]:
        """
        优先尝试ZhaBan策略，没有信号再尝试GapUp策略
        ZhaBan在hour=1买入，GapUp在hour=4买入
        """
        # Hour 1: 尝试ZhaBan
        if hour == 1:
            zhaban_cands = [c for c in candidates if c.get('_strategy') == 'zhaban']
            signal = self.zhaban.should_buy(zhaban_cands, date, hour, hour_data, portfolio, day_data)
            if signal:
                signal['_strategy'] = 'zhaban'
                return signal

        # Hour 4: 尝试GapUp
        if hour == 4:
            gapup_cands = [c for c in candidates if c.get('_strategy') == 'gapup']
            signal = self.gapup.should_buy(gapup_cands, date, hour, hour_data, portfolio, day_data)
            if signal:
                signal['_strategy'] = 'gapup'
                return signal

        return None
