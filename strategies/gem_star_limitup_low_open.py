"""GEM/STAR涨停(首板)次日低开买入策略

逻辑：GEM/STAR首板涨停(+20%)→次日低开≥2%→恐慌制造机会
与big_yang_low_open_v2互斥：那个要求非涨停(10-19.9%)，这个要求涨停(+20%)

引擎验证结果(2021-01~2026-07, slot=1):
- CAGR: 202.40%
- 交易笔数: 458
- 胜率: 55.02%
- 平均收益: +1.79%
- 最大回撤: 72.33%
- 年度: 2021=264%, 2022=112%, 2023=108%, 2024=113%, 2025=181%, 2026=40%(半年)

条件:
- 标的: GEM/STAR/BJ
- 信号: 昨日涨停(首板, 换手>=5%, 非一字板)
- 今日低开: -8% < open_rate < -2%
- 近5日累涨 < 40%
- 排序: 低开越深越优先
- 买入: 当日 H1_open
- 卖出: trailing 2.0pp / 硬止损-6% / 最长3天H4_close
"""
import re
from typing import Optional
from strategies.base import Strategy, Signal, SellSignal

_GEM_PAT = re.compile(r'^sz\.30')
_STAR_PAT = re.compile(r'^sh\.688')
_BJ_PAT = re.compile(r'^bj\.')

class GemStarLimitupLowOpenStrategy(Strategy):
    name = "gem_star_limitup_low_open"
    max_hold_hours = 12
    sell_day_no_buy = True
    buy_hour = 1

    trailing_stop_pp = 2.0
    stop_loss_pp = 6.0
    open_rate_min = -8.0       # 不能低开太多
    open_rate_max = -2.0       # 至少低开2%
    min_turn = 5.0             # 涨停日换手≥5%(非一字)
    cum5_threshold = 40.0      # GEM/STAR波动大，放宽累涨限制

    def __init__(self):
        self._cand_codes = set()
        self._cand_date = None

    @staticmethod
    def _is_gem_star_bj(code):
        return bool(_GEM_PAT.match(code) or _STAR_PAT.match(code) or _BJ_PAT.match(code))

    @staticmethod
    def _get_limit_ratio(code):
        if _BJ_PAT.match(code):
            return 1.30
        return 1.20  # GEM/STAR 20%

    @staticmethod
    def _is_limit_up(code, close, preclose):
        if not preclose or preclose <= 0:
            return False
        ratio = GemStarLimitupLowOpenStrategy._get_limit_ratio(code)
        return round(close / preclose, 2) >= ratio

    def get_candidates(self, date, data_feed):
        snapshot = data_feed.get_market_snapshot(date, self.buy_hour)
        if not snapshot:
            self._cand_codes = set()
            self._cand_date = date
            return []

        candidates = []
        for code, info in snapshot.items():
            if not self._is_gem_star_bj(code):
                continue
            if info.get('isST'):
                continue

            prev_close = info.get('prev_close') or 0.0
            prev_preclose = info.get('prev_preclose') or 0.0
            prev_turn = info.get('prev_turn') or 0.0

            if prev_close <= 0 or prev_preclose <= 0:
                continue

            # 条件：昨日必须是涨停
            if not self._is_limit_up(code, prev_close, prev_preclose):
                continue

            # 条件：昨日换手≥5%（排除一字板）
            if prev_turn < self.min_turn:
                continue

            # 今日数据
            today_open = info.get('open') or 0.0
            open_rate = info.get('open_rate') or 0.0
            if today_open <= 0:
                continue

            # 条件：今日低开在合理区间
            if open_rate >= self.open_rate_max or open_rate < self.open_rate_min:
                continue

            # 检查是否首板（前一日不是涨停）
            history = data_feed.get_stock_history(code, date, 3)
            if len(history) >= 3:
                # history[-1] = 昨日, history[-2] = 前日
                day_before = history[-2] if len(history) >= 2 else None
                if day_before:
                    db_close = day_before.get('close', 0)
                    db_preclose = day_before.get('preclose', 0)
                    if db_close and db_preclose and db_preclose > 0:
                        if self._is_limit_up(code, db_close, db_preclose):
                            continue  # 连板，跳过

            # 近5日累涨过滤
            if len(history) > 1:
                cum5 = sum(d.get('close_rate', 0) or 0 for d in history[:-1][-5:])
                if cum5 >= self.cum5_threshold:
                    continue

            candidates.append({
                'code': code,
                'prev_turn': prev_turn,
                'open_rate': open_rate,
            })

        if not candidates:
            self._cand_codes = set()
            self._cand_date = date
            return []

        # 排序：低开越深越好（恐慌越大→反弹越强）
        candidates.sort(key=lambda x: x['open_rate'])
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
        return Signal(code=code, price=float(price), strategy_name=self.name,
                     target_hold_hours=self.max_hold_hours)

    def should_sell(self, position, date, hour, data_feed):
        buy_price = position.buy_price
        if buy_price <= 0:
            return None
        if position.buy_date == date:
            return None

        peak_pct = getattr(position, 'peak_pct', 0.0)
        h_high = data_feed.get_hour_high(position.code, date, hour)
        if h_high and h_high > 0:
            h_pct = (h_high - buy_price) / buy_price * 100
            if h_pct > peak_pct:
                peak_pct = h_pct
        position.peak_pct = peak_pct

        h_low = data_feed.get_hour_low(position.code, date, hour)
        h_open = data_feed.get_hour_open(position.code, date, hour)
        if h_low and h_low > 0:
            low_pct = (h_low - buy_price) / buy_price * 100
            if low_pct <= -self.stop_loss_pp:
                sl_price = buy_price * (1 - self.stop_loss_pp / 100)
                if h_open and h_open > 0 and h_open < sl_price:
                    sl_price = h_open
                return SellSignal(reason='stop_loss', price=float(sl_price))
            if peak_pct > 0 and (peak_pct - low_pct) >= self.trailing_stop_pp:
                trail_price = buy_price * (1 + (peak_pct - self.trailing_stop_pp) / 100)
                if h_high and h_high > 0 and h_high < trail_price:
                    if h_open and h_open > 0:
                        trail_price = h_open
                return SellSignal(reason='trailing_stop', price=float(trail_price))

        if hour == 4 and position.hours_held >= self.max_hold_hours:
            close_price = data_feed.get_hour_close(position.code, date, 4)
            if not close_price or close_price <= 0:
                close_price = data_feed.get_hour_open(position.code, date, 4)
            if close_price and close_price > 0:
                return SellSignal(reason='max_hold_3days', price=float(close_price))

        return None

    def get_buy_price(self, code, date, hour, data_feed):
        return data_feed.get_hour_open(code, date, hour)
