"""大阳线(非涨停)次日低开策略 V2优化版 (S4-V2)

基于V1策略的特征工程与分层分析优化:
- 新增: 换手倍数过滤 (prev_turn / avg_turn_5d < 4.0) — 排除极端操纵放量
- 调整: 硬止损从5%放宽至6% (给高波动更多呼吸空间,减少假止损)
- 调整[2026-07-24]: trailing_stop 2.0pp→4.0pp — 可执行口径walk-forward重搜,
  2.0为乐观成交引擎旧产物; 邻域加密确认3.5~5.0稳定平台, 取平台中心4.0
  (训练2021-2024 CAGR +57.86%, 盲测2025-2026 +539.79%/MDD 26.1%)

优化逻辑:
- 涨停次日低开的股票日内波动极大(振幅15-20%)
- 冲高往往是短暂脉冲,2pp trailing比3pp更好捕捉(WR提升9%+)
- -5%硬止损容易被日内正常波动触发,-6%更合理
- 换手率>4倍5日均值意味着异常资金介入,回撤风险大

引擎验证结果(2021-01~2026-07, slot=1):
- V1基线: CAGR 172%, 611笔, WR 49.92%, MDD 65%
- V2优化: CAGR 599%, 574笔, WR 58.89%, MDD 58%

其余逻辑与V1一致:
- 标的: 创/科/北交所
- 信号: 昨涨>=10%非涨停, 今低开>=-2%, open>昨low, 5日累涨<30%, 昨换手>=3%
- 新增: 换手倍数<4.0
- 排序: 昨日换手率降序
- 买入: 当日 H1_open
- 卖出: trailing 4.0pp / 硬止损-6% / 最长3天H4_close
"""
import re
from typing import Optional

from strategies.base import Strategy, Signal, SellSignal


_CYB_PATTERN = re.compile(r'^sz\.30')
_STAR_PATTERN = re.compile(r'^sh\.688')
_BJ_PATTERN = re.compile(r'^bj\.')


class BigYangLowOpenV2Strategy(Strategy):
    name = "big_yang_low_open_v2"
    max_hold_hours = 12
    sell_day_no_buy = True

    buy_hour = 1
    trailing_stop_pp = 4.0          # 2026-07-24可执行口径重搜: 2.0→4.0
    # 依据(walk-forward 训练2021-2024/盲测2025-2026, 可执行挂单引擎 slot=1):
    #   旧2.0为乐观成交引擎调优产物, 可执行口径下训练段仅+19.21% CAGR;
    #   邻域加密确认3.5~5.0为稳定平台(训练+40%~+62%), 非孤峰;
    #   4.0为平台中心: 训练+57.86%(#2), 盲测+539.79%/MDD26.1%(#2), 双段排序一致;
    #   3.5盲测更高(+585.81%)但紧邻3.0凹陷(训练仅+8.59%), 稳健性次于4.0。
    #   详见 logs/backtest/grid_s4_executable.log + grid_s4_neighborhood.log
    stop_loss_pp = 6.0              # SL维度确认: 盲测SL6(+539.79%)优于7/8/10, 维持6.0
    take_profit_pp = None           # 固定止盈(None=不启用,使用trailing)
    sell_mode = 'trailing'          # 'trailing' 或 'fixed'
    # 2026-07-31批准转正(APPROVAL_G2_MINUTE.md, 基准档案 t48_s4_g2_1000):
    #   minute_exit: 卖出触线分钟级精化(Task#24路径, 声明即启用无需CLI);
    #   confirm(2@1000): 10:00前trailing触线须连续2根5min bar收盘破线确认
    #   后次bar开盘卖(假摔豁免), 硬止损不受影响恒立即; 10:00后触线即卖。
    #   回滚开关: confirm_bars=2→0 一行全链路关(minute_exit可单独保留)。
    minute_exit = True
    confirm_bars = 2
    confirm_before = '1000'
    confirm_scope = 'trailing'
    close_rate_threshold = 10.0
    open_rate_threshold = -2.0
    cum5_threshold = 30.0
    min_turn = 3.0
    max_turn_ratio = 4.0           # V2新增: 换手倍数上限

    def __init__(self, **kwargs):
        self._cand_codes = set()
        self._cand_date = None
        # 支持参数化覆盖
        for k, v in kwargs.items():
            if hasattr(self, k):
                setattr(self, k, v)

    @staticmethod
    def _is_st(info) -> bool:
        """ST双保险判定(Task#136): isST字段 ∨ 名称含ST(大写化) ∨ 名称含'退'。

        背景: 历史回填数据isST漏标(2021-2025单日120~210只isST=0但名称带*ST),
        对齐S2 amplitude_reversal._is_st写法并追加退市整理期'退'字过滤。
        name来源: 回测链backtest/data_feed与实盘链realtime/data_feed的
        snapshot均注入code_name(源自stock_kline.code_name, 按日历史名称,
        填充率100%); 若上游异常缺name则退化为纯isST判定(不误杀)。
        """
        if info.get('isST'):
            return True
        name = str(info.get('code_name') or '')
        if 'ST' in name.upper() or '退' in name:
            return True
        return False

    @staticmethod
    def _get_limit_ratio(code: str) -> float:
        if _BJ_PATTERN.match(code):
            return 1.30
        return 1.20

    @staticmethod
    def _is_limit_up(code: str, close: float, preclose: float) -> bool:
        if not preclose or preclose <= 0:
            return False
        return round(close / preclose, 2) >= BigYangLowOpenV2Strategy._get_limit_ratio(code)

    def get_candidates(self, date, data_feed):
        """筛选昨日大阳线非涨停 + 今日低开 + 换手倍数<4 的创/科/北交所股票。"""
        snapshot = data_feed.get_market_snapshot(date, self.buy_hour)
        if not snapshot:
            self._cand_codes = set()
            self._cand_date = date
            return []

        candidates = []
        for code, info in snapshot.items():
            if not (_CYB_PATTERN.match(code) or _STAR_PATTERN.match(code)
                    or _BJ_PATTERN.match(code)):
                continue

            # ST 过滤: isST+名称双保险(Task#136修复isST漏标风险敞口)
            if self._is_st(info):
                continue

            prev_close = info.get('prev_close') or 0.0
            prev_preclose = info.get('prev_preclose') or 0.0
            prev_close_rate = info.get('prev_close_rate') or 0.0
            prev_low = info.get('prev_low') or 0.0
            prev_turn = info.get('prev_turn') or 0.0

            if prev_close <= 0 or prev_preclose <= 0 or prev_low <= 0:
                continue

            # 条件7: 昨日换手率 >= 3%
            if prev_turn < self.min_turn:
                continue

            # 条件1: 昨日涨幅 >= 10%
            if prev_close_rate < self.close_rate_threshold:
                continue

            # 条件2: 昨日未涨停
            if self._is_limit_up(code, prev_close, prev_preclose):
                continue

            # 今日数据
            today_open = info.get('open') or 0.0
            open_rate = info.get('open_rate') or 0.0
            if today_open <= 0:
                continue

            # 条件3: 今日低开 2%+
            if open_rate > self.open_rate_threshold:
                continue

            # 条件4: 今日 open > 昨日 low
            if today_open <= prev_low:
                continue

            # 条件5: 近5日累计涨幅 < 30%
            history = data_feed.get_stock_history(code, date, 6)
            if len(history) > 1:
                pre_hist = history[:-1]
                if len(pre_hist) >= 3:
                    cum5 = sum(d.get('close_rate', 0) or 0
                              for d in pre_hist[-5:])
                    if cum5 >= self.cum5_threshold:
                        continue

            # 条件8 [V2新增]: 换手倍数 < 4.0
            # 计算5日平均换手率(不含昨日)
            if len(history) >= 3:
                # history是按时间升序的[T-6..T-1], 去掉最后一天(昨日)取前面的换手率
                prev_turns = [d.get('turn', 0) or 0 for d in history[:-1] if (d.get('turn', 0) or 0) > 0]
                if prev_turns:
                    avg_turn_5 = sum(prev_turns[-5:]) / len(prev_turns[-5:])
                    if avg_turn_5 > 0 and prev_turn / avg_turn_5 >= self.max_turn_ratio:
                        continue

            candidates.append({
                'code': code,
                'prev_turn': prev_turn,
            })

        if not candidates:
            self._cand_codes = set()
            self._cand_date = date
            return []

        candidates.sort(key=lambda x: x['prev_turn'], reverse=True)
        self._cand_codes = set(c['code'] for c in candidates)
        self._cand_date = date
        return [c['code'] for c in candidates]

    def should_buy(self, code, date, hour, data_feed, portfolio):
        if hour != self.buy_hour:
            return None
        if date != self._cand_date or code not in self._cand_codes:
            return None
        price = data_feed.get_hour_open(code, date, hour)
        if not price or price <= 0:
            return None
        return Signal(
            code=code,
            price=float(price),
            strategy_name=self.name,
            target_hold_hours=self.max_hold_hours,
        )

    def should_sell(self, position, date, hour, data_feed):
        """支持两种卖出模式:
        - trailing: trailing_stop + 硬止损 + 到期
        - fixed: 固定止盈 + 固定止损 + 到期
        """
        buy_price = position.buy_price
        if buy_price <= 0:
            return None

        # T+1: 买入当日禁卖
        if position.buy_date == date:
            return None

        if self.sell_mode == 'fixed':
            return self._sell_fixed(position, date, hour, data_feed, buy_price)
        else:
            return self._sell_trailing(position, date, hour, data_feed, buy_price)

    def _sell_fixed(self, position, date, hour, data_feed, buy_price):
        """固定止盈止损模式"""
        h_high = data_feed.get_hour_high(position.code, date, hour)
        h_low = data_feed.get_hour_low(position.code, date, hour)
        h_open = data_feed.get_hour_open(position.code, date, hour)

        if h_low and h_low > 0:
            # 固定止损
            sl_target = buy_price * (1 - self.stop_loss_pp / 100)
            if h_low <= sl_target:
                sl_price = sl_target
                if h_open and h_open > 0 and h_open < sl_target:
                    sl_price = h_open
                return SellSignal(reason='stop_loss', price=float(sl_price))

        if h_high and h_high > 0 and self.take_profit_pp:
            # 固定止盈
            tp_target = buy_price * (1 + self.take_profit_pp / 100)
            if h_high >= tp_target:
                tp_price = tp_target
                if h_open and h_open > 0 and h_open > tp_target:
                    tp_price = h_open
                return SellSignal(reason='take_profit', price=float(tp_price))

        # 到期卖出
        if hour == 4 and position.hours_held >= self.max_hold_hours:
            close_price = data_feed.get_hour_close(position.code, date, 4)
            if not close_price or close_price <= 0:
                close_price = h_open if h_open and h_open > 0 else None
            if close_price and close_price > 0:
                return SellSignal(reason='max_hold', price=float(close_price))

        return None

    def _sell_trailing(self, position, date, hour, data_feed, buy_price):
        """Trailing stop模式 - 可执行挂单语义。

        实盘等价操作: 每小时开始时, 依据"已知peak"(此前完整小时的最高价+当前
        小时开盘价)挂条件单: 触发价 = max(peak回落trailing_pp, 硬止损价)。
        - 当前小时open已低于触发价 → 以open成交(条件单开盘即触发)
        - 当前小时low下穿触发价 → 以触发价成交
        - 不用当前小时的high更新peak后又用同小时low触发(小时内OHLC顺序未知,
          那是不可执行的乐观假设); 当前小时high仅用于更新下一小时的peak。
        """
        # bar开始时已知的peak: 此前完整小时的peak + 当前bar开盘价
        peak_pct = getattr(position, 'peak_pct', 0.0)
        h_open = data_feed.get_hour_open(position.code, date, hour)
        if h_open and h_open > 0:
            open_pct = (h_open - buy_price) / buy_price * 100
            if open_pct > peak_pct:
                peak_pct = open_pct

        h_high = data_feed.get_hour_high(position.code, date, hour)
        h_low = data_feed.get_hour_low(position.code, date, hour)

        if h_low and h_low > 0 and h_open and h_open > 0:
            # 触发阈值: 硬止损 与 trailing(peak>0时启用, 必然高于硬止损)
            sl_price = buy_price * (1 - self.stop_loss_pp / 100)
            trigger_price = sl_price
            trigger_reason = 'stop_loss'
            if peak_pct > 0:
                trail_price = buy_price * (
                    1 + (peak_pct - self.trailing_stop_pp) / 100)
                if trail_price > trigger_price:
                    trigger_price = trail_price
                    trigger_reason = 'trailing_stop'

            if h_open <= trigger_price:
                # 开盘即触发 → 以open成交(跳空场景)
                reason = 'stop_loss' if h_open <= sl_price else trigger_reason
                return SellSignal(reason=reason, price=float(h_open))
            if h_low <= trigger_price:
                # 盘中下穿 → 以触发价成交
                return SellSignal(reason=trigger_reason,
                                  price=float(trigger_price))

        # 未卖出: 用当前小时high更新peak, 供下一小时挂单使用
        if h_high and h_high > 0:
            h_pct = (h_high - buy_price) / buy_price * 100
            if h_pct > peak_pct:
                peak_pct = h_pct
        position.peak_pct = peak_pct

        # 到期: D+3 H4_close 平仓
        if hour == 4 and position.hours_held >= self.max_hold_hours:
            close_price = data_feed.get_hour_close(position.code, date, 4)
            if not close_price or close_price <= 0:
                close_price = data_feed.get_hour_open(position.code, date, 4)
            if close_price and close_price > 0:
                return SellSignal(reason='max_hold_3days', price=float(close_price))

        return None

    def get_buy_price(self, code, date, hour, data_feed):
        return data_feed.get_hour_open(code, date, hour)
