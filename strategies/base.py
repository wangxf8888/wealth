"""策略基类 - 定义统一接口"""
from abc import ABC, abstractmethod
from typing import Dict, List, Tuple

from engine.types import DailyBar, Position


class BaseStrategy(ABC):
    """策略基类"""

    def __init__(self):
        self._market_temperature: int = 50  # 默认中性温度

    def set_market_temperature(self, temperature: int):
        """设置当日市场温度（由引擎在每日开始时调用）

        Args:
            temperature: 0-100的市场温度分数
        """
        self._market_temperature = temperature

    @property
    @abstractmethod
    def name(self) -> str:
        """策略名称"""

    @property
    def market_temperature(self) -> int:
        """当前市场温度"""
        return self._market_temperature

    @property
    def stop_loss(self) -> float | None:
        """止损阈值(负数,如-0.05表示-5%)，None表示不设止损"""
        return None

    @property
    def stop_profit(self) -> float | None:
        """止盈阈值(正数,如0.15表示+15%)，None表示不设止盈"""
        return None

    @abstractmethod
    def screen_candidates(self, date: str, pool: List[str],
                          history: Dict[str, List[DailyBar]]) -> List[str]:
        """日级别: 从股票池中筛选今日候选股

        Args:
            date: 当前日期
            pool: 今日合格股票池(已过滤市值/ST/停牌)
            history: 股票历史数据 {code: [DailyBar按日期升序]}

        Returns:
            候选股代码列表
        """

    @abstractmethod
    def check_buy_signal(self, code: str, current_hour: int,
                         today: DailyBar, history: List[DailyBar]) -> Tuple[bool, str]:
        """小时级别: 检查买入信号

        信号基于上一个hour的数据做决策(已确认数据，非未来数据)

        Args:
            code: 股票代码
            current_hour: 当前hour(1-4), 决策用的是上一hour的数据
            today: 今日DailyBar(含已完成的hour数据)
            history: 该股票历史DailyBar列表(含今日之前的数据)

        Returns:
            (是否买入, 买入原因)
        """

    @abstractmethod
    def check_sell_signal(self, position: Position, current_hour: int,
                          today: DailyBar, history: List[DailyBar]) -> Tuple[bool, str]:
        """小时级别: 检查卖出信号

        Args:
            position: 当前持仓
            current_hour: 当前hour(1-4)
            today: 今日DailyBar
            history: 该股票历史数据

        Returns:
            (是否卖出, 卖出原因)
        """
