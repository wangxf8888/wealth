"""创业板/科创板晚封涨停次日策略 (S3)

核心逻辑:
- 创业板(sz.30xxxx)或科创板(sh.688xxx)昨日涨停(20%板)
- 非H1封板(hour1_close < 涨停价, 即晚封板, 资金持续吸筹)
- 非一字板(open != close == limit 排除)
- 非ST(turn >= 0.1)
- 次日hour=1买入(open < 涨停价, 非一字开盘)
- 按换手率从低到高排序(低换手=强势锁仓)

参数:
- 止盈: +12%(日级HIGH触发, 以tp_target价成交)
- 止损: -20%(日级LOW触发, 以sl_target价成交)
- 最大持有: 8小时(2天: 买入后第2天h4 close平仓)
- TP优先: 同天TP和SL同时触发时取TP(日级TP-priority)
"""
import re
from typing import Optional

from strategies.base import Strategy, Signal, SellSignal


_CYB_PATTERN = re.compile(r'^sz\.30')
_STAR_PATTERN = re.compile(r'^sh\.688')


class GemStarLateSealStrategy(Strategy):
    name = "gem_star_late_seal"
    max_hold_hours = 8           # 2天 = 8小时
    sell_day_no_buy = True       # 卖出当天不买新仓(与独立脚本一致)

    buy_hour = 1
    take_profit_pct = 0.12       # +12%
    stop_loss_pct = -0.20        # -20%

    def __init__(self):
        self._cand_codes = set()
        self._cand_date = None

    def get_candidates(self, date, data_feed):
        """筛选昨日20%晚封涨停的创业板/科创板股票。"""
        snapshot = data_feed.get_market_snapshot(date, self.buy_hour)
        if not snapshot:
            self._cand_codes = set()
            self._cand_date = date
            return []

        # 需要前一交易日hour1_close判断是否晚封
        prev_date = data_feed._prev_trading_date(date)
        if not prev_date:
            self._cand_codes = set()
            self._cand_date = date
            return []

        prev_df = data_feed._load_day(prev_date)
        if prev_df is None or prev_df.empty:
            self._cand_codes = set()
            self._cand_date = date
            return []

        # 前日hour1_close字典(用于晚封判定)
        prev_h1_close = (prev_df['hour1_close'].to_dict()
                         if 'hour1_close' in prev_df.columns else {})

        candidates = []
        for code, info in snapshot.items():
            # 只要创业板或科创板
            if not (_CYB_PATTERN.match(code) or _STAR_PATTERN.match(code)):
                continue

            # ST 过滤: 仅使用isST字段(与独立脚本一致)
            # 注: CYB/STAR板ST股也是20%涨跌幅,不需要按名称排除
            if info.get('isST'):
                continue

            # 前日数据
            prev_close = info.get('prev_close') or 0.0
            prev_preclose = info.get('prev_preclose') or 0.0
            prev_open = info.get('prev_open') or 0.0
            prev_turn = info.get('prev_turn') or 0.0

            if prev_close <= 0 or prev_preclose <= 0:
                continue

            # 20%涨停判定(创业板/科创板) - 含0.01容差
            limit_price = round(prev_preclose * 1.20, 2)
            if prev_close < limit_price - 0.01:
                continue

            # 非一字板: 不是open=close=limit的情况
            if (abs(prev_open - prev_close) < 0.01
                    and abs(prev_close - limit_price) < 0.01):
                continue

            # 晚封: hour1_close < 涨停价(H1未封住) - 含0.01容差
            h1_close_val = prev_h1_close.get(code)
            if h1_close_val is None:
                continue
            try:
                h1_close = float(h1_close_val)
            except (TypeError, ValueError):
                continue
            if h1_close <= 0:
                continue
            if h1_close >= limit_price - 0.01:
                continue  # H1已封板, 不是晚封

            # 今日open < 今日涨停价(能买到, 非一字开)
            today_open = info.get('open') or 0.0
            preclose = info.get('preclose') or 0.0
            if today_open <= 0 or preclose <= 0:
                continue
            today_limit = round(preclose * 1.20, 2)
            if today_open >= today_limit - 0.01:
                continue

            # 今日open不能是跌停(买不到)
            today_limit_down = round(preclose * 0.80, 2)
            if today_open <= today_limit_down + 0.01:
                continue

            candidates.append({
                'code': code,
                'prev_turn': prev_turn,
            })

        if not candidates:
            self._cand_codes = set()
            self._cand_date = date
            return []

        # 按换手率从低到高排序
        candidates.sort(key=lambda x: x['prev_turn'])
        self._cand_codes = set(c['code'] for c in candidates)
        self._cand_date = date
        return [c['code'] for c in candidates]

    def should_buy(self, code, date, hour, data_feed, portfolio):
        """hour=1买入。"""
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
        """日级TP-priority卖出逻辑(使用daily high/low确保不遗漏竞价区间价格):
        - Hour1: Gap检测(open超TP/SL以open成交)
        - Hours1-4: TP检测(hourly high触发即卖)
        - Hour4: 用daily high补检TP, daily low检SL + 到期
        确保TP在SL之前有机会触发(日级TP优先)。
        """
        # T+1: 买入当日禁卖
        if position.buy_date == date:
            return None

        buy_price = position.buy_price
        if buy_price <= 0:
            return None

        tp_target = buy_price * (1 + self.take_profit_pct)
        sl_target = buy_price * (1 + self.stop_loss_pct)

        bar_open = data_feed.get_hour_open(position.code, date, hour)
        if not bar_open or bar_open <= 0:
            return None

        # 1) Hour1 Gap检测: open已超过TP/SL阈值
        if hour == 1:
            open_pnl = (bar_open - buy_price) / buy_price
            if open_pnl >= self.take_profit_pct:
                return SellSignal(reason='take_profit', price=float(bar_open))
            if open_pnl <= self.stop_loss_pct:
                return SellSignal(reason='stop_loss', price=float(bar_open))

        # 2) 每小时检测TP(从hourly high): TP一旦触发立即卖出
        bar_high = data_feed.get_hour_high(position.code, date, hour)
        if bar_high and bar_high > 0 and bar_high >= tp_target:
            return SellSignal(reason='take_profit', price=float(tp_target))

        # 3) Hour4: 用daily OHLC做最终TP/SL检测 + 到期
        if hour == 4:
            # daily high可能包含竞价区间的极值(hourly bars未覆盖)
            day_high = data_feed.get_day_high(position.code, date)
            if day_high and day_high > 0 and day_high >= tp_target:
                return SellSignal(reason='take_profit', price=float(tp_target))

            # SL: 用daily low检测(含竞价区间)
            day_low = data_feed.get_day_low(position.code, date)
            if day_low and day_low > 0 and day_low <= sl_target:
                return SellSignal(reason='stop_loss', price=float(sl_target))

            # 到期: hours_held >= max_hold_hours → close平仓
            if position.hours_held >= self.max_hold_hours:
                close_price = data_feed.get_day_close(position.code, date)
                if not close_price or close_price <= 0:
                    close_price = data_feed.get_hour_close(position.code, date, 4)
                sell_price = (close_price if close_price and close_price > 0
                              else bar_open)
                return SellSignal(reason='expired', price=float(sell_price))

        return None

    def get_buy_price(self, code, date, hour, data_feed):
        return data_feed.get_hour_open(code, date, hour)
