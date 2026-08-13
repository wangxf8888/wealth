"""卖出触线分钟级精化 (Task#24) - 统一执行框架(execution_core)接入回测引擎。

架构(用户定案): 实盘与回测走同一份决策代码 ExitEngine, 只是数据来源不同。
本模块是回测侧的hour主循环适配器: 引擎每个(date,hour)调用一次 check(),
把该hour的12根5min bar(data_feed.get_minute_bars, 来源minute.db)依序喂给
持仓对应的 ExitEngine, 得到分钟级触线决策(更早触发、更保守、更接近实盘)。

语义要点:
  * 触线类(stop_loss/trailing_stop/take_profit)判定完全归ExitEngine分钟路径,
    策略hour级should_sell在分钟模式下不再被调用(防双路径状态漂移)。
  * 到期强平: ExitEngine在expire_date的1500 final bar按收盘价平仓, 与策略
    "hour==4 且 hours_held>=max_hold_hours 按h4 close平仓"等价——
    expire_date由buy_date+max_hold_hours经交易日历换算(见_expire_date)。
  * 分钟数据缺口fallback: 该hour整体作为一根bar喂入同一个ExitEngine——
    bar级挂单语义在hour粒度上与策略_sell_trailing逐bit等价(Task#27提炼源),
    且peak状态不断裂; 缺口计数由data_feed统计, 回测结束打印告警防静默偏差。
  * 跌停不可卖: ExitEngine按触发bar的close+当日preclose逐bar判定顺延
    (比hour级"整小时封死"更精细); preclose取stocks.db权威值,
    规避minute.db除权除息日preclose偏差(D1遗留)。
  * 参数显式读取自策略实例(stop_loss_pp/trailing_stop_pp/take_profit_pp/
    sell_mode/max_hold_hours), 缺失即不接管(supports=False走原路径), 不兜底。

买入侧完全不动(EntryEngine见docs/entry_engine_design.md, 另行任务)。
"""
import math

from strategies.base import SellSignal
from execution_core import Bar, ExitEngine

# hour -> 该hour最后一根5min bar的time_end(缺口fallback合成hour bar用)
_HOUR_END = {1: '1030', 2: '1130', 3: '1400', 4: '1500'}


class MinuteExitChecker:
    """持仓级ExitEngine管理器: 每个slot的在持仓位挂一个ExitEngine, 逐hour喂bar。"""

    def __init__(self, data_feed):
        self.data_feed = data_feed
        self._engines = {}   # slot_id -> (pos_key, ExitEngine)
        self.decisions = []  # 全部ExitDecision留痕(QA手工对照触发时点/价格)

    # ------------------------------------------------------------------
    @staticmethod
    def supports(strategy) -> bool:
        """策略是否可被分钟路径接管: 须显式暴露trailing/fixed卖出参数。"""
        mode = getattr(strategy, 'sell_mode', None)
        if mode not in ('trailing', 'fixed'):
            return False
        if not getattr(strategy, 'stop_loss_pp', None):
            return False
        if mode == 'trailing' and not getattr(strategy, 'trailing_stop_pp', None):
            return False
        if not getattr(strategy, 'max_hold_hours', None):
            return False
        return True

    # ------------------------------------------------------------------
    def _expire_date(self, pos, strategy) -> str:
        """buy_date + max_hold_hours → 到期交易日。

        hour级到期语义: 第k个交易日h4检查时 hours_held>=H 即平仓,
        hours_held(k日h4检查时) = (5-buy_hour) + 4*(k-1) + 3
        → k = ceil((H + buy_hour - 4) / 4), 至少T+1。
        """
        feed = self.data_feed
        idx = feed._date_index.get(pos.buy_date)
        if idx is None:
            return '9999-12-31'
        k = max(1, math.ceil((strategy.max_hold_hours + pos.buy_hour - 4) / 4))
        j = idx + k
        if j >= len(feed._trading_dates):
            return '9999-12-31'   # 超出数据范围: 回测期内不到期(与hour级一致)
        return feed._trading_dates[j]

    def _is_st(self, code: str, date: str) -> bool:
        day = self.data_feed._load_day(date)
        if day is None or day.empty or code not in day.index:
            return False
        return bool(int(day.loc[code].get('isST', 0) or 0))

    def _get_engine(self, pos, strategy) -> ExitEngine:
        key = (pos.code, pos.buy_date, pos.buy_hour, pos.buy_price)
        cur = self._engines.get(pos.slot_id)
        if cur is not None and cur[0] == key:
            return cur[1]
        buy = pos.buy_price
        tp_pp = getattr(strategy, 'take_profit_pp', None)
        eng = ExitEngine(
            code=pos.code,
            buy_date=pos.buy_date,
            buy_price=buy,
            sl_price=buy * (1 - strategy.stop_loss_pp / 100.0),
            expire_date=self._expire_date(pos, strategy),
            sell_mode=strategy.sell_mode,
            tp_price=buy * (1 + tp_pp / 100.0) if tp_pp else None,
            trailing_pp=getattr(strategy, 'trailing_stop_pp', None),
            is_st=self._is_st(pos.code, pos.buy_date),
            # Task#48 G2收盘确认: 从策略类属性透传(缺省全关=现行立即语义)
            confirm_bars=int(getattr(strategy, 'confirm_bars', 0) or 0),
            confirm_before=getattr(strategy, 'confirm_before', '') or '',
            confirm_scope=(getattr(strategy, 'confirm_scope', 'trailing')
                           or 'trailing'),
        )
        self._engines[pos.slot_id] = (key, eng)
        return eng

    # ------------------------------------------------------------------
    def check(self, pos, strategy, date: str, hour: int):
        """该(date,hour)的分钟级卖出检查。返回SellSignal或None。

        调用方须已通过supports(strategy)检查。
        """
        feed = self.data_feed
        h_open = feed.get_hour_open(pos.code, date, hour)
        if not h_open or h_open <= 0:
            return None   # 停牌/无当日数据: 与hour级路径一致跳过

        eng = self._get_engine(pos, strategy)
        if eng.done is not None:
            # 极端防御: 此前决策未能成交(如价格非法被引擎拒绝) → 重发同一决策
            return self._to_signal(eng.done, strategy)
        preclose = feed.get_day_preclose(pos.code, date) or 0.0
        rows = feed.get_minute_bars(pos.code, date, hour)

        decision = None
        if rows is not None:
            for t, o, h, l, c in rows:
                bar = Bar(code=pos.code, date=date, time_end=t,
                          open=o or 0, high=h or 0, low=l or 0, close=c or 0,
                          preclose=preclose, is_final=True)
                decision = eng.on_bar(bar)
                if decision is not None:
                    break
        else:
            # 缺口fallback: 该hour整体当作一根bar喂入(hour级挂单语义等价降级)
            h_high = feed.get_hour_high(pos.code, date, hour)
            h_low = feed.get_hour_low(pos.code, date, hour)
            h_close = feed.get_hour_close(pos.code, date, hour)
            if h_high > 0 and h_low > 0 and h_close > 0:
                bar = Bar(code=pos.code, date=date, time_end=_HOUR_END[hour],
                          open=h_open, high=h_high, low=h_low, close=h_close,
                          preclose=preclose, is_final=True)
                decision = eng.on_bar(bar)

        if decision is None:
            return None
        self.decisions.append(decision)
        return self._to_signal(decision, strategy)

    @staticmethod
    def _to_signal(decision, strategy) -> SellSignal:
        reason = decision.action
        if reason == 'expired':
            # 与策略hour级到期reason命名对齐(交易明细可比)
            reason = ('max_hold_3days' if strategy.sell_mode == 'trailing'
                      else 'max_hold')
        elif getattr(decision, 'confirmed', False):
            # Task#48: G2确认后成交, reason带_confirm后缀(QA可辨识, t47同款)
            reason += '_confirm'
        return SellSignal(reason=reason, price=float(decision.sell_price))
