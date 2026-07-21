"""策略基类与信号定义。

设计原则（合规第一）：
- 所有 should_buy / should_sell / get_candidates 只能读取「当前 hour 开始时已知」的数据，
  即 hour-1 及更早的完整数据 + 今日竞价结果(open/open_rate)。
- 买卖成交价永远是「当前 hour 的 open」，由引擎通过 data_feed.get_hour_open 提供。
"""
from dataclasses import dataclass
from typing import Optional


@dataclass
class Signal:
    """买入信号"""
    code: str
    price: float                 # 建议买入价（通常为当前 hour 的 open）
    strategy_name: str
    target_hold_hours: int       # 目标持有小时数
    signal_date: str = ''        # 经济买入日（MOC策略: D0尾盘锁资金，引擎D+1处理）


@dataclass
class SellSignal:
    """卖出信号"""
    reason: str                  # 'take_profit' | 'stop_loss' | 'expired' | 'custom'
    price: float


class Strategy:
    """策略基类。子类需实现 get_candidates / should_buy / should_sell / get_buy_price。"""

    name: str = "base"
    max_hold_hours: int = 8      # 默认最长持有 8 小时(2 个交易日)

    def get_candidates(self, date: str, data_feed) -> list:
        """筛选候选股，使用 prev_day 数据（开盘前执行）。返回股票代码列表。"""
        raise NotImplementedError

    def should_buy(self, code: str, date: str, hour: int,
                   data_feed, portfolio) -> Optional[Signal]:
        """策略决定是否买入。每小时开始时被调用，只能看到 hour-1 及之前的数据。"""
        raise NotImplementedError

    def should_sell(self, position, date: str, hour: int,
                    data_feed) -> Optional[SellSignal]:
        """策略决定是否卖出（含止盈/止损/到期）。每小时开始时被调用。"""
        raise NotImplementedError

    def get_buy_price(self, code: str, date: str, hour: int, data_feed) -> float:
        """返回买入价格（当前 hour 的 open）。"""
        return data_feed.get_hour_open(code, date, hour)
