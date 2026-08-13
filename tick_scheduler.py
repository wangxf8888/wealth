#!/usr/bin/env python3
"""统一调度: tick分发框架 (Task#45)
=====================================
架构方向(用户定案): 同一套体系, 每5min决定一次信号——如果策略是hour级的
就等hour边界再决策, 如果是5min级的就每个5min周期决策。

本模块是"这个tick是否是该策略的决策边界"判定的唯一来源(回测/实盘共用):
  * 回测: engine/run_unified 主循环按 day_ticks(tick_interval) 驱动,
    默认'hour'=4个hour边界tick/日(与历史 for hour in (1,2,3,4) 完全等价);
    '5min'=48个bar tick/日, hour级策略只在hour首bar被调用(strategy_due)。
  * 实盘: intraday_monitor 10s轮询, 5min bar边界hook用 LiveBarAggregator
    的bar切换判定(与本模块 BAR_TIMES 同源), hour级策略盘中无新增决策点
    (买入=9:25 morning_decision, 卖出=ExitEngine持续监控=hour级挂单的及时执行)。

时段映射(A股, 与execution_core.BAR_TIMES同源, 48根bar/日):
  hour1 = 09:30-10:30 (bar 0935..1030)   hour2 = 10:30-11:30 (bar 1035..1130)
  hour3 = 13:00-14:00 (bar 1305..1400)   hour4 = 14:00-15:00 (bar 1405..1500)
hour级策略的决策点 = 每个hour的第一根bar(0935/1035/1305/1405), 语义与
"每小时开始时被调用, 成交价=该hour open"的现行口径一致。
"""
from dataclasses import dataclass
from typing import Tuple

from execution_core import BAR_TIMES

VALID_INTERVALS = ('hour', '5min')

# hour首bar(hour级策略在5min tick流中的决策边界)
HOUR_START_BARS = ('0935', '1035', '1305', '1405')
# hour尾bar(引擎hour级记账边界: portfolio.tick_hour在hour结束时调用)
HOUR_END_BARS = ('1030', '1130', '1400', '1500')

# bar结束时刻 → 所属hour(1..4): BAR_TIMES前24根为上午(hour1/2), 后24根下午
_HOUR_OF_BAR = {t: i // 12 + 1 for i, t in enumerate(BAR_TIMES)}


def validate_interval(interval: str) -> str:
    """校验tick驱动/策略决策频率取值(fail-fast, 不做默认值兜底)。"""
    if interval not in VALID_INTERVALS:
        raise ValueError(
            f"tick interval必须为{VALID_INTERVALS}, got {interval!r}")
    return interval


@dataclass(frozen=True)
class Tick:
    """一个调度tick。hour模式: 4个/日(hour边界); 5min模式: 48个/日(bar边界)。"""
    hour: int            # 所属hour 1..4
    time_end: str        # 对应5min bar结束时刻'HHMM'
    is_hour_start: bool  # 是否hour首bar(hour级策略的决策边界)
    is_hour_end: bool    # 是否hour尾bar(hour级记账边界: tick_hour/持有计时)


def hour_of_bar(time_end: str) -> int:
    """bar结束时刻'HHMM' → 所属hour(1..4)。非法时刻抛ValueError。"""
    try:
        return _HOUR_OF_BAR[time_end]
    except KeyError:
        raise ValueError(f"非法5min bar时刻: {time_end!r} (合法: BAR_TIMES)")


def is_hour_start(time_end: str) -> bool:
    """该bar是否为hour首bar(hour级策略在5min tick流中的决策边界)。"""
    return time_end in HOUR_START_BARS


def is_decision_boundary(decision_interval: str, time_end: str) -> bool:
    """核心判定: 这个tick(5min bar边界)是否是该决策频率策略的决策边界。

    'hour'级 → 仅hour首bar; '5min'级 → 每根bar都是。
    """
    validate_interval(decision_interval)
    hour_of_bar(time_end)  # 时刻合法性校验(非法即抛错)
    if decision_interval == '5min':
        return True
    return is_hour_start(time_end)


def strategy_due(decision_interval: str, tick: Tick) -> bool:
    """该策略在此tick是否应被调用决策(引擎主循环分发用)。"""
    validate_interval(decision_interval)
    if decision_interval == '5min':
        return True
    return tick.is_hour_start


def day_ticks(tick_interval: str) -> Tuple[Tick, ...]:
    """一个交易日的调度tick序列。

    'hour' → 4个hour边界tick(现行hour循环的等价表达, 行为零变化):
             单tick同时承担hour首(决策)与hour尾(tick_hour记账)角色;
    '5min' → 48个bar tick(hour级策略经strategy_due只在hour首bar被调用,
             tick_hour记账只在hour尾bar发生, 与hour模式计时口径一致)。
    """
    validate_interval(tick_interval)
    if tick_interval == 'hour':
        return tuple(Tick(hour=h, time_end=HOUR_START_BARS[h - 1],
                          is_hour_start=True, is_hour_end=True)
                     for h in (1, 2, 3, 4))
    return tuple(Tick(hour=_HOUR_OF_BAR[t], time_end=t,
                      is_hour_start=is_hour_start(t),
                      is_hour_end=(t in HOUR_END_BARS))
                 for t in BAR_TIMES)
