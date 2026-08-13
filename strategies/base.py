"""策略基类与信号定义。

设计原则（合规第一）：
- 所有 should_buy / should_sell / get_candidates 只能读取「当前 hour 开始时已知」的数据，
  即 hour-1 及更早的完整数据 + 今日竞价结果(open/open_rate)。
- 买卖成交价永远是「当前 hour 的 open」，由引擎通过 data_feed.get_hour_open 提供。

框架级兜底（引擎强制执行，策略无法绕过，新策略无需重复实现但可自查作双保险）：
- T+1: 买入当日的卖出信号被引擎拦截（并计数告警，出现即策略bug）
- 涨停开盘不可买入 / 整小时封死跌停不可卖出（自动顺延）
- 停牌不可交易、IPO前5个交易日不买入
- 成交价被钳制到该小时真实成交区间 [low, high]（越界价格视为不可执行）
- 规则唯一来源: 项目根目录 trading_rules.py（回测/实盘共用）

策略声明卖出参数的标准方式（实盘引擎按此读取，缺失会报错）：
- fixed模式:    take_profit_pct / stop_loss_pct  （如 0.08 / -0.15）
- trailing模式: trailing_stop_pp / stop_loss_pp + sell_mode='trailing'
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
    # Task#45 统一调度: 策略声明决策频率, 引擎/实盘按此分发决策tick。
    #   'hour'(默认) = 只在hour边界(H1-H4开始时)被调用should_buy/should_sell;
    #                  现有全部策略继承此默认值, 行为与历史完全一致(零变化)。
    #   '5min'       = 每根5min bar边界均为决策点(需--tick-interval 5min驱动,
    #                  买入走should_buy_bar, 卖出语义bar级经ExitEngine/minute_exit)。
    # 未来5min级策略只需声明 decision_interval='5min' 即可接入统一调度,
    # 边界判定唯一来源: 项目根目录 tick_scheduler.py(回测/实盘共用)。
    decision_interval: str = 'hour'
    # 2026-07-31批准(APPROVAL_G2_MINUTE): 分钟级卖出+G2确认声明(默认全关=零侵入)。
    #   minute_exit=True → 引擎自动为该策略走分钟级卖出路径(Task#24,
    #                       无需全局--minute-exit; 参数不全时降级hour级并告警);
    #   confirm_bars/confirm_before/confirm_scope → ExitEngine G2确认状态机
    #   (仅trailing模式; confirm_bars=0=关闭, 触线立即语义与历史完全一致)。
    #   实盘侧同源读取: morning_decision.get_sell_params → 持仓落盘 →
    #   intraday_monitor G2分路(保守映射: 硬SL 10秒即卖, cron兜底触线即卖)。
    minute_exit: bool = False
    confirm_bars: int = 0
    confirm_before: str = ''
    confirm_scope: str = 'trailing'

    def get_candidates(self, date: str, data_feed) -> list:
        """筛选候选股，使用 prev_day 数据（开盘前执行）。返回股票代码列表。"""
        raise NotImplementedError

    def should_buy(self, code: str, date: str, hour: int,
                   data_feed, portfolio) -> Optional[Signal]:
        """策略决定是否买入。每小时开始时被调用，只能看到 hour-1 及之前的数据。"""
        raise NotImplementedError

    # ------------------------------------------------------------------
    # Task#54: 5min级策略bar级决策入口(decision_interval='5min'必须实现)
    # ------------------------------------------------------------------
    def should_buy_bar(self, code: str, date: str, tick,
                       data_feed, portfolio) -> Optional[Signal]:
        """bar级买入决策(仅decision_interval='5min'策略, 引擎每tick调用)。

        tick=tick_scheduler.Tick, 语义为"bar N(结束时刻tick.time_end)开始时点":
        - 可见数据: bar 1..N-1完整 + bar N开盘价, 经
          data_feed.get_bars_until(code, date, tick.time_end)获取,
          未来数据在数据层封死(与hour级snapshot裁剪同哲学);
        - 成交语义: 返回Signal即按bar N开盘价成交(=信号bar的次一bar开盘,
          与execution_core.BreakoutNextOpenRule的N+1 bar open语义一致);
          Signal.price可覆盖(引擎钳制到bar N真实区间[low,high]);
        - 涨停开盘拒单/除权除息拦截: 引擎经evaluate_open_entry统一守卫
          (open=bar N开盘价), 与hour级/实盘同一份入场评估函数。
        声明'5min'但未实现本方法 → 引擎初始化fail-fast报错。
        """
        raise NotImplementedError

    def bar_day_prescreen(self, code: str, date: str, data_feed) -> bool:
        """bar循环当日候选预筛钩子(Task#54性能通道, 纯计算剪枝)。

        引擎每日对5min级策略候选调用一次, 返回False表示"该code当日不可能
        产生任何bar级信号", 不进48-tick bar循环。⚠️必须是信号的必要条件
        (结果不变性由策略自证), 允许读当日日级数据(如day_high)做剪枝——
        因为被剪掉的code在任何tick都不会有信号, 不构成未来函数。
        默认True=不剪枝。
        """
        return True

    def should_sell(self, position, date: str, hour: int,
                    data_feed) -> Optional[SellSignal]:
        """策略决定是否卖出（含止盈/止损/到期）。每小时开始时被调用。"""
        raise NotImplementedError

    def get_buy_price(self, code: str, date: str, hour: int, data_feed) -> float:
        """返回买入价格（当前 hour 的 open）。"""
        return data_feed.get_hour_open(code, date, hour)
