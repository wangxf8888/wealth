#!/usr/bin/env python3
"""
统一双模执行框架 - 共享执行核心 (Task#27)
==========================================
架构方向(用户定案): 实盘与回测走**同一份决策代码**, 只是数据来源不同——
实盘是实盘接口(10s行情聚合成进行中5min bar), 回测是minute.db的5min K线,
拿到bar后由同一个 ExitEngine 给出卖出决策。根治"双实现漂移"
(历史事故: 参数getattr默认值漂移、整点快照vs条件单时序漂移)。

组成:
  Bar / BarUpdate     统一bar数据结构(支持"进行中bar": 同一bar多次推送累计OHLC)
  BarStream           抽象接口: 按时间序提供5min bar
  MinuteDbBarStream   回测适配器: 从 data/minute.db 回放历史5min bar(final)
  LiveBarAggregator   实盘适配器: 把10秒行情tick聚合为进行中5min bar
  ExitEngine          共享卖出决策核心(双数据源驱动, 决策唯一)

ExitEngine 语义(从 position_tracker.evaluate_position + 策略._sell_trailing 提炼归一):
  * bar级挂单语义: 每根bar开始时, 以"已知peak"(此前完整bar的high + 当前bar开盘价,
    买入日T+0的冲高不计入)冻结触发线 trigger = max(硬SL, peak - buy*trailing_pp%);
    - bar开盘价 <= trigger → 跳空击穿, 按开盘价成交
    - bar内low  <= trigger → 盘中击穿, 按触发价成交
    - bar内新高**不抬升本bar触发线**(bar内OHLC顺序未知, 不做乐观假设),
      只并入下一bar的已知peak → 决策与bar内tick到达顺序无关,
      这是"实盘tick流"与"回测bar回放"结果逐字段一致的关键。
  * fixed模式: SL优先于TP(保守), 同样开盘跳空按开盘价/盘中按线价。
  * action归类: 语义成交价 <= 硬SL → stop_loss, 否则 trailing_stop (与
    evaluate_position 一致)。
  * T+0: 买入当日禁卖, 且当日open/high不计入peak(与回测T+1语义一致)。
  * 到期: bar.date >= expire_date 且当日最后bar(1500)收盘 → 按close强平
    (expired)。触线优先于到期(时间序天然保证)。
  * 跌停不可卖: 触发时若最新价已达跌停(卖单排队无法成交) → 本bar顺延,
    下一bar继续评估(与 evaluate_position 的框架拦截一致)。
  * 参数显式必填(fail-fast): trailing模式必须显式给trailing_pp,
    不做getattr默认值兜底 —— 防参数漂移事故重演。

边界差异(有意为之, 文档级说明):
  * fixed模式同一bar内既触SL又触TP: 回测(final bar)按SL优先保守处理;
    实盘tick流按tick到达先后触发(真实世界本就路径依赖)。
    一致性守护测试不构造该极端重叠场景。
  * 到期强平的1500 bar在实盘中daemon于15:00退出可能收不到final,
    实盘到期平仓由15:01收盘cron终检兜底(旧路径), 语义价一致(收盘价)。

本模块不依赖 engine.py / realtime包, 可被双方引用(Task#24再接入回测引擎)。
"""
import os
import sqlite3
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime
from typing import Iterator, List, Optional

import trading_rules

PROJECT_ROOT = os.path.dirname(os.path.abspath(__file__))
MINUTE_DB = os.path.join(PROJECT_ROOT, 'data', 'minute.db')

# A股5min bar结束时刻表(48根/日): 0935..1130, 1305..1500
BAR_TIMES_AM = [f"{9 + (30 + 5 * i) // 60:02d}{(30 + 5 * i) % 60:02d}"
                for i in range(1, 25)]          # 0935..1130
BAR_TIMES_PM = [f"{13 + (5 * i) // 60:02d}{(5 * i) % 60:02d}"
                for i in range(1, 25)]          # 1305..1500
BAR_TIMES = BAR_TIMES_AM + BAR_TIMES_PM
LAST_BAR_TIME = '1500'


# =============================================================================
# 数据结构
# =============================================================================

@dataclass
class Bar:
    """一根5min bar。进行中bar: 同(date,time_end)多次推送, OHLC为当前累计值。

    time_end: bar结束时刻'HHMM'(与minute.db口径一致, 0935..1500)
    preclose: 该日昨收(涨跌停判定用), 适配器负责填充; 缺失填0(不拦截)
    is_final: True=bar已收盘(OHLC定稿), False=进行中(实盘tick累计)
    """
    code: str
    date: str          # 'YYYY-MM-DD'
    time_end: str      # 'HHMM'
    open: float
    high: float
    low: float
    close: float
    preclose: float = 0.0
    is_final: bool = True


@dataclass
class ExitDecision:
    """卖出决策(双模一致性断言的比对对象: 逐字段一致)。"""
    code: str
    action: str        # 'stop_loss' | 'trailing_stop' | 'take_profit' | 'expired'
    sell_price: float  # 语义成交价(开盘跳空→开盘价, 盘中→触发线价, 到期→收盘价)
    sell_date: str     # 触发bar日期
    sell_time: str     # 触发bar time_end
    trigger_ref: float # 参考触发线(expired时为0)
    pnl_pct: float     # 相对买入价盈亏%
    confirmed: bool = False  # Task#48: 经G2收盘确认后成交(次bar开盘价)


@dataclass
class EntryDecision:
    """买入决策(Task#45, 与ExitDecision对称; 设计: docs/entry_engine_design.md)。

    EntryEngine只产出决策不碰资金仓位——记账归执行层
    (回测: portfolio, 实盘: morning_decision/daemon下单确认链路)。
    """
    code: str
    strategy: str          # 策略name
    slot_id: str
    action: str            # 'buy'
    buy_price: float       # 语义成交价(§4: 触发bar N final → 成交bar N+1 open)
    buy_date: str
    buy_time: str          # 成交bar time_end
    trigger_ref: float     # 参考触发线(突破价/区间边界; open_range时为open_rate%)
    reason: str            # 'breakout_next_open' | 'open_range' | ...


class BarStream(ABC):
    """抽象接口: 按时间序提供5min bar(含进行中bar的当前累计OHLC)。"""

    @abstractmethod
    def __iter__(self) -> Iterator[Bar]:
        """按时间序yield Bar。同一bar允许多次yield(进行中→final)。"""


# =============================================================================
# 共享卖出决策核心
# =============================================================================

class ExitEngine:
    """消费bar流评估单一持仓的卖出。数据源无关: 实盘/回测喂同构Bar即可。

    参数全部显式必填(从策略类/持仓记录显式读取, 不做默认值兜底)。
    """

    def __init__(self, code: str, buy_date: str, buy_price: float,
                 sl_price: float, expire_date: str, sell_mode: str,
                 tp_price: Optional[float] = None,
                 trailing_pp: Optional[float] = None,
                 init_peak: Optional[float] = None,
                 name: str = '', is_st: Optional[bool] = None,
                 expire_time: str = LAST_BAR_TIME,
                 confirm_bars: int = 0, confirm_before: str = '',
                 confirm_scope: str = 'trailing'):
        if sell_mode not in ('trailing', 'fixed', 'timed'):
            raise ValueError(
                f"sell_mode必须为trailing/fixed/timed, got {sell_mode!r}")
        if sell_mode == 'trailing' and not trailing_pp:
            raise ValueError(f"{code}: trailing模式必须显式提供trailing_pp"
                             f"(防参数默认值漂移, 不兜底)")
        if not buy_price or buy_price <= 0:
            raise ValueError(f"{code}: buy_price非法: {buy_price}")
        if sell_mode != 'timed' and (not sl_price or sl_price <= 0):
            # timed(定时了结)无盘中TP/SL, sl_price仅作展示可缺省
            raise ValueError(f"{code}: sl_price非法: {sl_price}")
        if confirm_scope not in ('trailing', 'all'):
            raise ValueError(f"{code}: confirm_scope必须为trailing/all"
                             f"(fail-fast), got {confirm_scope!r}")
        if confirm_bars and sell_mode != 'trailing':
            raise ValueError(f"{code}: 确认机制仅支持trailing模式"
                             f"(T47结论: S1/S2跳空主导维持立即语义), "
                             f"got sell_mode={sell_mode!r}")
        self.code = code
        self.name = name
        self.buy_date = buy_date
        self.buy_price = float(buy_price)
        self.sl_price = float(sl_price) if sl_price else 0.0
        self.tp_price = float(tp_price) if tp_price else None
        self.expire_date = expire_date
        self.expire_time = expire_time or LAST_BAR_TIME
        self.sell_mode = sell_mode
        self.trailing_pp = float(trailing_pp) if trailing_pp else None
        self.is_st = (trading_rules.is_st_name(name) if is_st is None
                      else is_st)
        # G2收盘确认参数(Task#48, Lee T47设计; 默认confirm_bars=0全关零侵入):
        #   confirm_bars   N=需连续N根final bar收盘<=线才卖(次bar开盘成交)
        #   confirm_before 确认仅在time_end<=此值的bar启用(''=全时段)
        #   confirm_scope  'trailing'=仅trailing线确认(冻结线>硬SL),
        #                  硬SL恒立即(T47解剖: SL假摔率0%铁律); 'all'=不区分
        self.confirm_bars = int(confirm_bars or 0)
        self.confirm_before = confirm_before or ''
        self.confirm_scope = confirm_scope
        # 已知peak: 历史(截至上一交易日)peak注入; 买入日冲高不计入 →
        # 起点=买入价(与evaluate_position/回测T+1语义一致)
        self.peak_known = max(float(init_peak or 0), self.buy_price)
        # bar状态
        self._cur_key = None          # (date, time_end)
        self._prev_high = None        # 上一bar最后所见high(final值)
        self._prev_date = None
        self._open_merged = False     # 当前bar开盘价是否已并入peak_known
        self.done: Optional[ExitDecision] = None
        self.blocked_last = False     # 上次评估是否被跌停拦截(展示用)
        # G2确认状态机(confirm_bars=0时恒不进入)
        self._confirm_cnt = 0         # 连续收盘破线计数
        self._confirm_pending = False # 确认完成, 待次bar开盘卖
        self._pending_key = None      # 完成确认的bar(date,time_end)
        self._pending_trigger = 0.0   # 完成确认时的冻结线(trigger_ref留痕)

    # -- 内部: bar切换时把上一bar high并入已知peak(T+0日不计入) --------------
    def _roll_bar(self, bar: Bar):
        if self._cur_key is not None and self._prev_high:
            if self._prev_date and self._prev_date > self.buy_date:
                if self._prev_high > self.peak_known:
                    self.peak_known = self._prev_high
        self._cur_key = (bar.date, bar.time_end)
        self._open_merged = False

    def on_bar(self, bar: Bar) -> Optional[ExitDecision]:
        """推送一根bar(可为进行中bar的累计更新)。返回卖出决策或None。

        幂等: 已决策(done)后恒返回None。
        """
        if self.done is not None:
            return None
        key = (bar.date, bar.time_end)
        if key != self._cur_key:
            self._roll_bar(bar)
        self._prev_high = bar.high
        self._prev_date = bar.date

        # T+0: 买入当日禁卖, 当日open/high均不计入peak
        if bar.date <= self.buy_date:
            return None

        # G2确认完成的待执行卖出: 次bar开盘市价成交(开盘已封跌停→顺延下一bar)
        # 与t47_lib.replay pending路径逐bit对齐(拦截判据=开盘价, 非收盘价)
        if self._confirm_pending and key != self._pending_key \
                and bar.open and bar.open > 0:
            if not trading_rules.is_at_limit_down(
                    self.code, bar.open, bar.preclose, is_st=self.is_st):
                self.done = ExitDecision(
                    code=self.code,
                    action=('stop_loss' if bar.open <= self.sl_price
                            else 'trailing_stop'),
                    sell_price=round(bar.open, 2),
                    sell_date=bar.date, sell_time=bar.time_end,
                    trigger_ref=round(self._pending_trigger, 4),
                    pnl_pct=round((bar.open / self.buy_price - 1) * 100, 2),
                    confirmed=True)
                return self.done
            # 开盘已封跌停→开盘不可成交, 顺延; 与t47_lib一致继续本bar常规
            # 评估(立即路径/到期仍按各自跌停拦截规则判定)

        # 当前bar开盘价并入已知peak(bar开始时刻可观察, 与_sell_trailing一致)
        if not self._open_merged and bar.open and bar.open > 0:
            self._open_merged = True
            if bar.open > self.peak_known:
                self.peak_known = bar.open

        action = None
        sell_price = None
        trigger_ref = 0.0

        if self.sell_mode == 'trailing':
            trigger = self.sl_price
            if self.peak_known > self.buy_price:
                trail = self.peak_known - self.buy_price * self.trailing_pp / 100.0
                if trail > trigger:
                    trigger = trail
            if bar.open and 0 < bar.open <= trigger:
                sell_price = bar.open          # 跳空击穿 → 开盘价成交
            elif bar.low and 0 < bar.low <= trigger:
                sell_price = trigger           # 盘中击穿 → 触发价成交
            # G2收盘确认(Task#48, 默认confirm_bars=0整段不进入=现行立即语义):
            # 确认窗口(time_end<=confirm_before 且 scope放行)内触线不立即卖,
            # 连续confirm_bars根final bar收盘仍破线 → 次bar开盘卖(上方pending
            # 路径); 任一final bar收盘收复线上 → 计数清零(全时段, t47_lib同款);
            # 硬SL(scope='trailing'且冻结线==硬SL, 无浮盈垫)与窗口外时段
            # 维持立即语义不受影响。
            if self.confirm_bars > 0:
                in_window = ((not self.confirm_before
                              or bar.time_end <= self.confirm_before)
                             and (self.confirm_scope != 'trailing'
                                  or trigger > self.sl_price + 1e-9))
                if sell_price is not None and in_window:
                    sell_price = None          # 窗口内触线: 不卖, 走确认
                # 计数仅final bar且非pending(pending期间等开盘成交, 不重复armed)
                if sell_price is None and bar.is_final \
                        and not self._confirm_pending:
                    if in_window and bar.close and 0 < bar.close <= trigger:
                        self._confirm_cnt += 1
                        if self._confirm_cnt >= self.confirm_bars:
                            self._confirm_pending = True
                            self._pending_key = key
                            self._pending_trigger = trigger
                            self._confirm_cnt = 0
                    elif not (bar.close and 0 < bar.close <= trigger):
                        self._confirm_cnt = 0  # 收盘收复线上(或无效close)清零
            if sell_price is not None:
                trigger_ref = trigger
                action = ('stop_loss' if sell_price <= self.sl_price
                          else 'trailing_stop')
        elif self.sell_mode == 'fixed':  # fixed: SL优先(保守), 再TP
            if bar.open and 0 < bar.open <= self.sl_price:
                action, sell_price, trigger_ref = \
                    'stop_loss', bar.open, self.sl_price
            elif bar.low and 0 < bar.low <= self.sl_price:
                action, sell_price, trigger_ref = \
                    'stop_loss', self.sl_price, self.sl_price
            elif self.tp_price and bar.open and bar.open >= self.tp_price:
                action, sell_price, trigger_ref = \
                    'take_profit', bar.open, self.tp_price
            elif self.tp_price and bar.high and bar.high >= self.tp_price:
                action, sell_price, trigger_ref = \
                    'take_profit', self.tp_price, self.tp_price
        # timed(Task#36): 无盘中触线, 仅按定时点了结(下方到期段)

        # 到期了结(触线优先于到期, 时间序天然保证):
        #   trailing/fixed: 到期日最后bar(1500)收盘强平 —— 原语义不变
        #   timed: expire_date当日expire_time bar定稿按close了结
        #     (与回测exit_mode='d2h1_close'的H1收盘价一致); 已过点/顺延跨日
        #     (跌停封死/daemon中途启动) → 任意bar(含进行中)即刻按close现价
        #     尽快卖出(=回测deferred语义, action统一'expired')
        if action is None and bar.close > 0:
            if self.sell_mode == 'timed':
                if (bar.date > self.expire_date
                        or (bar.date == self.expire_date
                            and (bar.time_end > self.expire_time
                                 or (bar.time_end == self.expire_time
                                     and bar.is_final)))):
                    action, sell_price = 'expired', bar.close
            elif bar.is_final and bar.date >= self.expire_date \
                    and bar.time_end == LAST_BAR_TIME:
                action, sell_price = 'expired', bar.close

        if action is None:
            self.blocked_last = False
            return None

        # 框架级兜底: 已封跌停 → 卖单排队无法成交, 顺延下一bar
        if trading_rules.is_at_limit_down(self.code, bar.close, bar.preclose,
                                          is_st=self.is_st):
            self.blocked_last = True
            return None

        self.done = ExitDecision(
            code=self.code, action=action,
            sell_price=round(sell_price, 2),
            sell_date=bar.date, sell_time=bar.time_end,
            trigger_ref=round(trigger_ref, 4),
            pnl_pct=round((sell_price / self.buy_price - 1) * 100, 2),
        )
        return self.done

    # -- 便捷: 直接消费一个BarStream(回测/仿真) ------------------------------
    def run_stream(self, stream: 'BarStream') -> Optional[ExitDecision]:
        for bar in stream:
            dec = self.on_bar(bar)
            if dec:
                return dec
        return None


def exit_engine_from_position(pos: dict) -> ExitEngine:
    """从实盘持仓记录(positions.json条目)构建ExitEngine。

    参数显式读取, 缺失即抛错(fail-fast, 沿用参数防漂移规范):
    持仓记录由morning_decision从策略类显式写入, 是参数的唯一事实源。
    timed模式(Task#36): 持仓带 sell_at={'date','time'}, 定时点即到期点
    (expire_time='HH:MM'→'HHMM'), 无盘中TP/SL。
    """
    sell_mode = pos.get('sell_mode', 'fixed')
    expire_date = pos['expire_date']
    expire_time = LAST_BAR_TIME
    if sell_mode == 'timed':
        sell_at = pos.get('sell_at') or {}
        if not sell_at.get('date') or not sell_at.get('time'):
            raise ValueError(f"{pos['code']}: timed持仓缺sell_at字段"
                             f"(fail-fast, 不兜底): {sell_at!r}")
        expire_date = sell_at['date']
        expire_time = sell_at['time'].replace(':', '')
    return ExitEngine(
        code=pos['code'],
        name=pos.get('name', ''),
        buy_date=pos['buy_date'],
        buy_price=pos['buy_price'],
        sl_price=pos.get('sl_price'),
        expire_date=expire_date,
        sell_mode=sell_mode,
        tp_price=pos.get('tp_price'),
        trailing_pp=pos.get('trailing_pp'),
        init_peak=pos.get('peak_price'),
        expire_time=expire_time,
        # 2026-07-31批准(APPROVAL_G2_MINUTE): 从持仓记录注入G2确认三参;
        # 持仓不带confirm字段(存量持仓/其他策略)→confirm_bars=0零侵入
        confirm_bars=int(pos.get('confirm_bars') or 0),
        confirm_before=pos.get('confirm_before') or '',
        confirm_scope=pos.get('confirm_scope') or 'trailing',
    )


# =============================================================================
# 回测适配器: minute.db 历史5min bar回放
# =============================================================================

class MinuteDbBarStream(BarStream):
    """从 data/minute.db 按时间序回放单只标的的历史5min bar(全部final)。

    preclose: 每日昨收 = 库内该code上一个有数据交易日的最后bar close
    (注: 除权除息日会与真实昨收有偏差——D1遗留问题, 影响仅跌停拦截判定;
    可通过 preclose_map 显式覆盖)。
    """

    def __init__(self, code: str, start_date: str, end_date: str = None,
                 db_path: str = MINUTE_DB, preclose_map: dict = None):
        self.code = code
        self.start_date = start_date
        self.end_date = end_date or '9999-12-31'
        self.db_path = db_path
        self.preclose_map = preclose_map or {}

    def __iter__(self) -> Iterator[Bar]:
        conn = sqlite3.connect(f"file:{self.db_path}?mode=ro", uri=True)
        try:
            cur = conn.cursor()
            rows = cur.execute(
                "SELECT date, time, open, high, low, close FROM minute_kline "
                "WHERE code=? AND date>=? AND date<=? ORDER BY date, time",
                (self.code, self.start_date, self.end_date)).fetchall()
            # 前一日收盘(preclose)映射: 库内前一个有数据日的最后bar close
            prev_close_row = cur.execute(
                "SELECT close FROM minute_kline WHERE code=? AND date<? "
                "ORDER BY date DESC, time DESC LIMIT 1",
                (self.code, self.start_date)).fetchone()
        finally:
            conn.close()
        prev_day_close = prev_close_row[0] if prev_close_row else 0.0
        cur_date, day_last_close = None, prev_day_close
        preclose = prev_day_close
        for date, t, o, h, l, c in rows:
            if date != cur_date:
                if cur_date is not None:
                    preclose = day_last_close
                cur_date = date
            pc = self.preclose_map.get(date, preclose)
            yield Bar(code=self.code, date=date, time_end=t,
                      open=o or 0, high=h or 0, low=l or 0, close=c or 0,
                      preclose=pc or 0, is_final=True)
            day_last_close = c or day_last_close


# =============================================================================
# 实盘适配器: 10秒行情tick → 进行中5min bar
# =============================================================================

class LiveBarAggregator:
    """把实盘轮询tick(现价)聚合为进行中5min bar, 输出与minute.db同构。

    用法: 每轮行情到达时 bars = agg.on_tick(now, price, preclose);
    依序把bars喂给 ExitEngine.on_bar()。
    返回0-2根bar: [上一bar(final定稿)], [当前bar(进行中累计)]。

    bar边界: tick时刻∈[09:30,11:30)∪[13:00,15:00) 归属所在5min窗口
    (time_end=窗口右端); 11:30/15:00整点tick归属1130/1500 bar;
    竞价期(09:30前)与午休tick忽略。
    中途启动/重启: 当前bar的open=首个tick价(近似); 启动前的日内区间触发
    由调用方先跑一次日级兜底判定(evaluate_position)补齐。
    """

    def __init__(self, code: str):
        self.code = code
        self._bar: Optional[Bar] = None

    @staticmethod
    def bar_key(ts: datetime):
        """tick时刻 → (date, time_end) 或 None(非交易时段)。"""
        hm = ts.hour * 60 + ts.minute
        date = ts.strftime('%Y-%m-%d')
        if 570 <= hm < 690:                     # [09:30, 11:30)
            end = 570 + ((hm - 570) // 5 + 1) * 5
        elif hm == 690:                         # 11:30整点 → 1130 bar
            end = 690
        elif 780 <= hm < 900:                   # [13:00, 15:00)
            end = 780 + ((hm - 780) // 5 + 1) * 5
        elif hm == 900:                         # 15:00收盘竞价 → 1500 bar
            end = 900
        else:
            return None
        return date, f"{end // 60:02d}{end % 60:02d}"

    def on_tick(self, ts: datetime, price: float,
                preclose: float = 0.0) -> List[Bar]:
        if not price or price <= 0:
            return []
        key = self.bar_key(ts)
        if key is None:
            return []
        date, time_end = key
        out: List[Bar] = []
        b = self._bar
        if b is not None and (b.date, b.time_end) != key:
            b.is_final = True                   # 上一bar定稿
            out.append(b)
            b = None
        if b is None:
            b = Bar(code=self.code, date=date, time_end=time_end,
                    open=price, high=price, low=price, close=price,
                    preclose=preclose or 0, is_final=False)
            self._bar = b
        else:
            if price > b.high:
                b.high = price
            if price < b.low:
                b.low = price
            b.close = price
            if preclose:
                b.preclose = preclose
        out.append(b)
        return out

    def flush(self) -> List[Bar]:
        """会话结束(如15:00退出前)把最后一根进行中bar定稿输出。"""
        if self._bar is not None and not self._bar.is_final:
            self._bar.is_final = True
            b, self._bar = self._bar, None
            return [b]
        return []


# =============================================================================
# 共享买入决策核心 (Task#45, 设计: docs/entry_engine_design.md)
# =============================================================================

def evaluate_open_entry(code: str, strategy: str, slot_id: str, date: str,
                        open_price: float, exchange_preclose: float,
                        prev_close: float, signal_ref_close: float = None,
                        is_st: bool = False, limit_basis: float = None):
    """共享入场评估函数: hour级策略在"日开盘tick"(9:25竞价)的入场决策。

    回测engine买入循环与实盘morning_decision调**同一份**本函数——策略条件
    判断部分(get_candidates/should_buy)已由同一策略类共享, 本函数归一其余
    重复实现的框架级守卫+决策构造, 行情获取各自适配:
      回测: open_price=hour open(库), exchange_preclose=日K preclose,
            prev_close=前一交易日close, is_st=库isST
      实盘: open_price=9:25竞价价, exchange_preclose=行情接口昨收,
            prev_close=signal_ref_close=信号日库内close, is_st=按名称判定,
            limit_basis=信号日close(历史口径: 行情源昨收可能缺失, 沿用
            信号日close作涨停基准——非除权日两者一致, 除权日已被守卫1拦截)

    守卫顺序(两侧历史行为一致, 不可调换):
      1. ex_dividend    除权除息日(交易所preclose≠前日close) → open_rate
                        语义失真, 假"低开"拦截 (Task#4)
      2. limit_up_open  开盘已涨停 → 买单排队买不到
                        (基准=limit_basis, 缺省用exchange_preclose)

    Returns:
        (EntryDecision, None)  通过 → 决策(buy_price=open_price,
                               trigger_ref=open_rate%, reason='open_range')
        (None, reject_reason)  拦截 → 'ex_dividend' | 'limit_up_open'
    """
    if trading_rules.is_ex_dividend_gap(exchange_preclose, prev_close):
        return None, 'ex_dividend'
    # limit_basis显式传入时(含0=无法判定→不拦截)不回退, 保历史口径逐分不差
    basis = limit_basis if limit_basis is not None else exchange_preclose
    if trading_rules.is_at_limit_up(code, open_price, basis, is_st=is_st):
        return None, 'limit_up_open'
    ref = signal_ref_close if signal_ref_close else prev_close
    open_rate = ((open_price / ref - 1) * 100
                 if ref and ref > 0 and open_price and open_price > 0 else 0.0)
    return EntryDecision(
        code=code, strategy=strategy, slot_id=slot_id, action='buy',
        buy_price=round(open_price, 2), buy_date=date, buy_time='0925',
        trigger_ref=round(open_rate, 2), reason='open_range'), None


class EntryRule(ABC):
    """单标的bar级买入规则(未来5min级策略接入点)。

    EntryEngine语义: 每根bar调用一次on_bar, 状态机自持; 只产出EntryDecision
    不碰资金仓位。与ExitEngine同一套语义成交价规则:
      触发在bar N(final确认) → 成交在bar N+1 open(下一可执行时点, 无前视);
      成交价由调用方经trading_rules.clamp_price_to_bar钳制;
      涨停拦截(is_at_limit_up)对称于卖出侧跌停拦截。
    """

    @abstractmethod
    def on_bar(self, bar: Bar) -> Optional[EntryDecision]:
        """推送一根bar(可为进行中bar累计更新)。返回买入决策或None。"""


class BreakoutNextOpenRule(EntryRule):
    """hour1半路板类: bar N(final)突破确认 → bar N+1 open成交。

    状态机: WATCHING → ARMED → DONE
      WATCHING: final bar且high>=breakout_price且time_end<=watch_deadline
                → ARMED (突破确认必须用final bar: 进行中bar的high未定稿,
                bar内冲高回落的假突破不作数——双模一致的关键)
      ARMED:    下一根bar到达 → 按其open成交; 开盘已达涨停(排板买不进)
                → 本bar放弃, 保持ARMED顺延下一bar
    """

    def __init__(self, code: str, strategy: str, slot_id: str,
                 breakout_price: float, watch_deadline: str = '1030',
                 is_st: bool = False):
        if not breakout_price or breakout_price <= 0:
            raise ValueError(f"{code}: breakout_price非法(fail-fast): "
                             f"{breakout_price}")
        self.code = code
        self.strategy = strategy
        self.slot_id = slot_id
        self.breakout_price = float(breakout_price)
        self.watch_deadline = watch_deadline
        self.is_st = is_st
        self.state = 'WATCHING'
        self.done: Optional[EntryDecision] = None
        self._armed_key = None      # 触发bar的(date,time_end), 成交须在其后

    def on_bar(self, bar: Bar) -> Optional[EntryDecision]:
        if self.done is not None:
            return None
        key = (bar.date, bar.time_end)
        if self.state == 'ARMED' and key != self._armed_key:
            # 成交bar: 按open成交; 开盘已涨停 → 买单排不上, 本bar放弃顺延
            if bar.open and bar.open > 0 and not trading_rules.is_at_limit_up(
                    self.code, bar.open, bar.preclose, is_st=self.is_st):
                self.done = EntryDecision(
                    code=self.code, strategy=self.strategy,
                    slot_id=self.slot_id, action='buy',
                    buy_price=round(bar.open, 2),
                    buy_date=bar.date, buy_time=bar.time_end,
                    trigger_ref=round(self.breakout_price, 4),
                    reason='breakout_next_open')
                self.state = 'DONE'
                return self.done
            return None
        if self.state == 'WATCHING' and bar.is_final:
            if bar.time_end > self.watch_deadline:
                self.state = 'DONE'      # 观察窗结束, 放弃
                return None
            if bar.high and bar.high >= self.breakout_price:
                self.state = 'ARMED'
                self._armed_key = key
        return None


class OpenRangeRule(EntryRule):
    """现有低吸系(S4/S5)口径的bar级表达: 首bar open的open_rate∈声明区间。

    与现行morning_decision 9:25决策语义一致, 作为迁移锚点(先影子比对后切换)。
    open_rate基准=signal_close(信号日收盘, 与实盘口径一致)。
    """

    def __init__(self, code: str, strategy: str, slot_id: str,
                 signal_close: float, rate_min: float, rate_max: float,
                 is_st: bool = False):
        if not signal_close or signal_close <= 0:
            raise ValueError(f"{code}: signal_close非法(fail-fast): "
                             f"{signal_close}")
        self.code = code
        self.strategy = strategy
        self.slot_id = slot_id
        self.signal_close = float(signal_close)
        self.rate_min = rate_min
        self.rate_max = rate_max
        self.is_st = is_st
        self.done: Optional[EntryDecision] = None
        self._first_bar_seen = False

    def on_bar(self, bar: Bar) -> Optional[EntryDecision]:
        if self.done is not None or self._first_bar_seen:
            return None
        if bar.time_end != BAR_TIMES[0]:     # 只看当日首bar(0935)
            self._first_bar_seen = True
            return None
        self._first_bar_seen = True
        if not bar.open or bar.open <= 0:
            return None
        dec, reject = evaluate_open_entry(
            code=self.code, strategy=self.strategy, slot_id=self.slot_id,
            date=bar.date, open_price=bar.open,
            exchange_preclose=bar.preclose, prev_close=bar.preclose,
            signal_ref_close=self.signal_close, is_st=self.is_st)
        if reject:
            return None
        if not (self.rate_min <= dec.trigger_ref < self.rate_max):
            return None
        # 成交=首bar open(9:25竞价语义), buy_time记bar时刻
        self.done = EntryDecision(
            code=dec.code, strategy=dec.strategy, slot_id=dec.slot_id,
            action='buy', buy_price=dec.buy_price, buy_date=dec.buy_date,
            buy_time=bar.time_end, trigger_ref=dec.trigger_ref,
            reason='open_range')
        return self.done
