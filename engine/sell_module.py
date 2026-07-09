"""卖出模块接口 + 次日收盘卖出策略"""


class SellModule:
    """卖出模块基类"""

    def should_sell(self, slot, date: str, hour: int, hour_data: dict) -> bool:
        """判断是否应该卖出"""
        raise NotImplementedError


class NextDayCloseSellStrategy(SellModule):
    """次日指定hour卖出策略（默认hour4收盘）
    
    支持min_hold_days参数：
    - min_hold_days=1: T+1卖出（持有1个交易日后卖出，默认）
    - min_hold_days=2: T+2卖出（持有2个交易日后卖出）
    """

    def __init__(self, sell_hour: int = 4, min_hold_days: int = 1):
        self.sell_hour = sell_hour
        self.min_hold_days = min_hold_days

    def should_sell(self, slot, date: str, hour: int, hour_data: dict) -> bool:
        """
        T+N卖出策略：
        - 只在指定sell_hour执行
        - 持有交易日数 >= min_hold_days 时触发卖出
        - hold_trading_days由引擎在每日循环开始时更新
        """
        if hour != self.sell_hour:
            return False
        if not slot.buy_date or slot.buy_date >= date:
            return False
        # 使用hold_trading_days判断持有天数
        hold_days = getattr(slot, 'hold_trading_days', 1)
        if hold_days >= self.min_hold_days:
            return True
        return False


class StopLossSellStrategy(SellModule):
    """止损卖出策略：继承NextDayCloseSellStrategy逻辑，额外添加止损条件
    
    - 正常卖出逻辑: 持有>=min_hold_days后在sell_hour卖出
    - 止损逻辑: 任何时段，如果当前价 < 买入价 * (1 - stop_loss_pct)，立即卖出
    """

    def __init__(self, sell_hour: int = 4, min_hold_days: int = 1, stop_loss_pct: float = 0.05):
        self.sell_hour = sell_hour
        self.min_hold_days = min_hold_days
        self.stop_loss_pct = stop_loss_pct  # 止损百分比，如0.05=5%

    def should_sell(self, slot, date: str, hour: int, hour_data: dict) -> bool:
        """止损 + 正常卖出策略"""
        if not slot.buy_date or slot.buy_date >= date:
            return False

        # 止损检查: 任何hour都可以触发
        if self.stop_loss_pct > 0:
            stock_h = hour_data.get(slot.code)
            if stock_h:
                h_low = stock_h.get('low', 0)
                if h_low and h_low > 0:
                    loss_pct = (slot.buy_price - h_low) / slot.buy_price
                    if loss_pct >= self.stop_loss_pct:
                        return True

        # 正常卖出逻辑
        if hour != self.sell_hour:
            return False
        hold_days = getattr(slot, 'hold_trading_days', 1)
        if hold_days >= self.min_hold_days:
            return True
        return False


class PerSlotSellStrategy(SellModule):
    """按每个slot自带的target_hold_days决定卖出时机

    适用于多策略组合：不同策略买入时设置不同的target_hold_days,
    本卖出模块根据各slot独立的持仓天数要求决定是否卖出。

    跌停保护：如果卖出日该股跌停(close==low且close<=round(preclose*涨跌停比,2))，
    则延期到下一个非跌停日卖出。
    """

    def __init__(self, sell_hour: int = 4):
        self.sell_hour = sell_hour

    def should_sell(self, slot, date: str, hour: int, hour_data: dict) -> bool:
        if hour != self.sell_hour:
            return False
        if not slot.buy_date or slot.buy_date >= date:
            return False
        # 使用slot自带的target_hold_days
        target = getattr(slot, 'target_hold_days', 1)
        hold_days = getattr(slot, 'hold_trading_days', 0)
        return hold_days >= target
