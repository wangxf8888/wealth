"""N仓位管理器 - 共享资金池模型"""
from typing import List, Optional


class PositionSlot:
    """单仓位槽 - 只记录持仓信息，不记录资金"""

    def __init__(self, slot_id: int):
        self.slot_id: int = slot_id
        self.code: Optional[str] = None
        self.code_name: Optional[str] = None
        self.buy_price: float = 0.0
        self.buy_date: Optional[str] = None
        self.buy_hour: int = 0
        self.shares: int = 0
        self.hold_trading_days: int = 0  # 持有交易日数（买入当天=0，次日=1...）
        self.strategy_name: str = ''        # 买入策略名
        self.target_hold_days: int = 1      # 目标持仓天数(T+N中的N)
        self.buy_reason: str = ''           # 买入理由(用于交易明细导出)

    @property
    def is_empty(self) -> bool:
        return self.code is None

    @property
    def is_occupied(self) -> bool:
        return self.code is not None

    def __repr__(self):
        if self.is_empty:
            return f"Slot{self.slot_id}(空仓)"
        return f"Slot{self.slot_id}({self.code} {self.code_name}, 买入:{self.buy_price:.2f}@{self.buy_date}H{self.buy_hour}, {self.shares}股)"


class Portfolio:
    """N仓位组合管理器 - 共享资金池，每次买入按总净值/N均分"""

    def __init__(self, initial_capital: float = 1_000_000, n_slots: int = 3, cost_rate: float = 0.001):
        self.initial_capital = initial_capital
        self.n_slots = n_slots
        self.cost_rate = cost_rate
        self.cash = initial_capital  # 共享资金池

        self.slots: List[PositionSlot] = [
            PositionSlot(i) for i in range(n_slots)
        ]

        # 交易记录
        self.all_trades: list = []
        # NAV跟踪
        self.nav_history: list = []
        self.prev_nav: float = initial_capital

    def occupied_slots(self) -> List[PositionSlot]:
        """返回所有持仓中的仓位"""
        return [s for s in self.slots if s.is_occupied]

    def empty_slots(self) -> List[PositionSlot]:
        """返回所有空仓位"""
        return [s for s in self.slots if s.is_empty]

    def held_codes(self) -> set:
        """返回当前所有持仓股票代码"""
        return {s.code for s in self.slots if s.is_occupied}

    def get_nav(self, current_prices: dict = None) -> float:
        """
        计算总净值 = 现金 + 所有持仓市值
        current_prices: {code: current_price}
        """
        total = self.cash
        for slot in self.slots:
            if slot.is_occupied:
                price = 0.0
                if current_prices and slot.code in current_prices:
                    price = current_prices[slot.code]
                elif slot.buy_price > 0:
                    price = slot.buy_price
                total += slot.shares * price
        return total

    def get_total_value(self, current_prices: dict = None) -> float:
        """总净值（get_nav的别名）"""
        return self.get_nav(current_prices)

    def execute_buy(self, slot: PositionSlot, code: str, code_name: str,
                    buy_price: float, date: str, hour: int):
        """
        执行买入操作
        买入金额 = 总净值 / N（确保仓位均衡）
        实际买入不超过可用现金
        """
        if buy_price <= 0:
            return

        # 按总净值的1/N计算目标买入金额
        total_value = self.get_total_value()
        target_amount = total_value / self.n_slots

        # 实际买入金额不能超过可用现金
        buy_amount = min(target_amount, self.cash)

        shares = int(buy_amount / (buy_price * (1 + self.cost_rate)) / 100) * 100
        if shares <= 0:
            return

        cost = shares * buy_price * (1 + self.cost_rate)

        slot.code = code
        slot.code_name = code_name
        slot.buy_price = buy_price
        slot.buy_date = date
        slot.buy_hour = hour
        slot.shares = shares
        self.cash -= cost

        self.all_trades.append({
            'type': 'buy',
            'slot_id': slot.slot_id,
            'code': code,
            'code_name': code_name,
            'price': buy_price,
            'shares': shares,
            'cost': cost,
            'date': date,
            'hour': hour,
        })

    def execute_sell(self, slot: PositionSlot, sell_price: float, date: str, hour: int):
        """
        执行卖出操作
        卖出收益归入共享资金池
        """
        if sell_price <= 0 or slot.is_empty:
            return

        proceeds = slot.shares * sell_price * (1 - self.cost_rate)
        pnl_pct = (sell_price / slot.buy_price - 1) * 100

        trade_record = {
            'type': 'sell',
            'slot_id': slot.slot_id,
            'code': slot.code,
            'code_name': slot.code_name,
            'buy_price': slot.buy_price,
            'sell_price': sell_price,
            'shares': slot.shares,
            'proceeds': proceeds,
            'pnl_pct': pnl_pct,
            'buy_date': slot.buy_date,
            'buy_hour': slot.buy_hour,
            'sell_date': date,
            'sell_hour': hour,
            'strategy_name': slot.strategy_name,
            'target_hold_days': slot.target_hold_days,
            'buy_reason': slot.buy_reason,
            'hold_days': slot.hold_trading_days,
        }
        self.all_trades.append(trade_record)

        # 卖出收益回到共享资金池
        self.cash += proceeds

        # 清空slot持仓信息
        slot.code = None
        slot.code_name = None
        slot.buy_price = 0.0
        slot.buy_date = None
        slot.buy_hour = 0
        slot.shares = 0
        slot.hold_trading_days = 0
        slot.strategy_name = ''
        slot.target_hold_days = 1
        slot.buy_reason = ''

        return trade_record

    def get_total_cash(self) -> float:
        """可用现金"""
        return self.cash

    def get_cumulative_return(self, current_prices: dict = None) -> float:
        """累计收益率(%)"""
        nav = self.get_nav(current_prices)
        return (nav / self.initial_capital - 1) * 100

    def record_nav(self, date: str, nav: float):
        """记录每日净值"""
        self.nav_history.append({'date': date, 'nav': nav})
        self.prev_nav = nav
