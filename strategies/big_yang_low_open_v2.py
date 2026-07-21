"""大阳线(非涨停)次日低开策略 V2优化版 (S4-V2)

基于V1策略的特征工程与分层分析优化:
- 新增: 换手倍数过滤 (prev_turn / avg_turn_5d < 4.0) — 排除极端操纵放量
- 调整: trailing_stop 从3pp收紧至2.0pp (高波动股快速冲高后迅速锁利)
- 调整: 硬止损从5%放宽至6% (给高波动更多呼吸空间,减少假止损)

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
- 卖出: trailing 2.0pp / 硬止损-6% / 最长3天H4_close
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
    trailing_stop_pp = 2.0          # V2: 从3.0收紧至2.0(最优)
    stop_loss_pp = 6.0              # V2: 从5.0放宽至6.0
    close_rate_threshold = 10.0
    open_rate_threshold = -2.0
    cum5_threshold = 30.0
    min_turn = 3.0
    max_turn_ratio = 4.0           # V2新增: 换手倍数上限

    def __init__(self):
        self._cand_codes = set()
        self._cand_date = None

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

            if info.get('isST'):
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
        """Trailing stop 2.0pp + 硬止损-6% + 最长3天H4到期。"""
        buy_price = position.buy_price
        if buy_price <= 0:
            return None

        # T+1: 买入当日禁卖
        if position.buy_date == date:
            return None

        peak_pct = getattr(position, 'peak_pct', 0.0)

        # 用当前hour的HIGH更新peak
        h_high = data_feed.get_hour_high(position.code, date, hour)
        if h_high and h_high > 0:
            h_pct = (h_high - buy_price) / buy_price * 100
            if h_pct > peak_pct:
                peak_pct = h_pct
        position.peak_pct = peak_pct

        # 用当前hour的LOW检测止损
        h_low = data_feed.get_hour_low(position.code, date, hour)
        h_open = data_feed.get_hour_open(position.code, date, hour)
        if h_low and h_low > 0:
            low_pct = (h_low - buy_price) / buy_price * 100

            # 硬止损: -6%
            if low_pct <= -self.stop_loss_pp:
                sl_price = buy_price * (1 - self.stop_loss_pp / 100)
                if h_open and h_open > 0 and h_open < sl_price:
                    sl_price = h_open
                return SellSignal(reason='stop_loss', price=float(sl_price))

            # Trailing stop: peak>0 且从peak回落>=2.0个百分点
            if peak_pct > 0 and (peak_pct - low_pct) >= self.trailing_stop_pp:
                trail_price = buy_price * (1 + (peak_pct - self.trailing_stop_pp) / 100)
                if h_high and h_high > 0 and h_high < trail_price:
                    if h_open and h_open > 0:
                        trail_price = h_open
                return SellSignal(reason='trailing_stop', price=float(trail_price))

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
