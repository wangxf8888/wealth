"""仓位管理 - N 个 slot 共享资金池模型（精简版）。

- 买入金额 = 当前总净值 / n_slots（仓位均衡）
- 卖出收益回到共享现金池
- hours_held 由引擎每小时调用 tick_hour() 递增
"""
from dataclasses import dataclass
from typing import Optional, List


@dataclass
class Position:
    slot_id: int
    code: str
    buy_price: float
    buy_date: str
    buy_hour: int
    strategy_name: str
    target_hold_hours: int
    hours_held: int = 0          # 已持有小时数（每过一个 hour +1）
    shares: int = 0              # 持股数（内部用于市值计算）


@dataclass
class TradeRecord:
    code: str
    strategy_name: str
    buy_date: str
    buy_hour: int
    buy_price: float
    sell_date: str
    sell_hour: int
    sell_price: float
    hold_hours: int
    profit_pct: float            # 含双边手续费的真实收益率(%)
    reason: str = ''


class Portfolio:
    def __init__(self, n_slots: int, initial_capital: float = 1_000_000,
                 cost_rate: float = 0.001):
        self.n_slots = n_slots
        self.initial_capital = initial_capital
        self.cost_rate = cost_rate
        self.cash = initial_capital
        self.buy_scale = 1.0  # Task#44: 当日买入系数(引擎每日设置, 默认1.0=零侵入)
        # slot_id -> Position | None
        self.slots = {i: None for i in range(n_slots)}
        self.trades: List[TradeRecord] = []

    # ------------------------------------------------------------------
    def has_empty_slot(self) -> bool:
        return any(p is None for p in self.slots.values())

    def _empty_slot_id(self) -> Optional[int]:
        for sid, p in self.slots.items():
            if p is None:
                return sid
        return None

    def get_active_positions(self) -> List[Position]:
        return [p for p in self.slots.values() if p is not None]

    def held_codes(self) -> set:
        return {p.code for p in self.slots.values() if p is not None}

    def get_all_trades(self) -> List[TradeRecord]:
        return self.trades

    # ------------------------------------------------------------------
    def buy(self, signal, price: float) -> bool:
        """按 总净值/n_slots 分配资金买入。成功返回 True。"""
        if price is None or price <= 0:
            return False
        sid = self._empty_slot_id()
        if sid is None:
            return False
        if signal.code in self.held_codes():
            return False

        # Task#44: 买入金额 = NAV/n_slots × 当日仓位系数(默认1.0)
        target_amount = self.get_nav() / self.n_slots * self.buy_scale
        buy_amount = min(target_amount, self.cash)
        shares = int(buy_amount / (price * (1 + self.cost_rate)) / 100) * 100
        if shares <= 0:
            return False

        cost = shares * price * (1 + self.cost_rate)
        self.cash -= cost
        # signal_date覆盖: MOC策略资金在signal_date即锁定
        actual_buy_date = getattr(signal, 'signal_date', '') or self._cur_date
        self.slots[sid] = Position(
            slot_id=sid,
            code=signal.code,
            buy_price=price,
            buy_date=actual_buy_date,
            buy_hour=self._cur_hour,
            strategy_name=signal.strategy_name,
            target_hold_hours=signal.target_hold_hours,
            hours_held=0,
            shares=shares,
        )
        return True

    def sell(self, slot_id: int, price: float, reason: str) -> Optional[TradeRecord]:
        """卖出指定 slot，收益回到现金池，返回成交记录。"""
        pos = self.slots.get(slot_id)
        if pos is None or price is None or price <= 0:
            return None

        proceeds = pos.shares * price * (1 - self.cost_rate)
        self.cash += proceeds
        # 含双边手续费的真实收益率: net_sell / gross_buy - 1
        if pos.buy_price > 0:
            profit_pct = (price * (1 - self.cost_rate) /
                          (pos.buy_price * (1 + self.cost_rate)) - 1) * 100
        else:
            profit_pct = 0.0

        rec = TradeRecord(
            code=pos.code,
            strategy_name=pos.strategy_name,
            buy_date=pos.buy_date,
            buy_hour=pos.buy_hour,
            buy_price=pos.buy_price,
            sell_date=self._cur_date,
            sell_hour=self._cur_hour,
            sell_price=price,
            hold_hours=pos.hours_held,
            profit_pct=profit_pct,
            reason=reason,
        )
        self.trades.append(rec)
        self.slots[slot_id] = None
        self._last_sell_date = self._cur_date
        return rec

    # 引擎在每个 hour 循环开始时设置当前时点（供 buy/sell 记录成交日期）
    _cur_date: str = ''
    _cur_hour: int = 0
    _last_sell_date: str = ''   # 最近一次卖出的日期(用于sell_day_no_buy策略)

    def set_context(self, date: str, hour: int):
        self._cur_date = date
        self._cur_hour = hour

    def sold_today(self) -> bool:
        """当天是否已有卖出操作。"""
        return self._cur_date == self._last_sell_date

    # ------------------------------------------------------------------
    def get_nav(self, current_prices: dict = None) -> float:
        """当前净值 = 现金 + 持仓市值。current_prices: {code: price}。"""
        total = self.cash
        for pos in self.slots.values():
            if pos is None:
                continue
            price = pos.buy_price
            if current_prices and pos.code in current_prices:
                cp = current_prices[pos.code]
                if cp and cp > 0:
                    price = cp
            total += pos.shares * price
        return total

    def tick_hour(self):
        """每过一小时调用：所有持仓 hours_held += 1。"""
        for pos in self.slots.values():
            if pos is not None:
                pos.hours_held += 1
