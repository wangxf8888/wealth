"""涨停早封低开买入策略 - 基于h1封板+低换手+T+1低开的反弹策略。

核心逻辑:
- h1封板(hour1_close==涨停价) + 换手<8% 的强势涨停
- 次日(T+1)若低开>3%(open_rate<-3%), 为恐慌性低开, 高概率修复
- 止盈止损用HIGH/LOW精确触发, TP优先

参数:
- 选股: 昨日涨停(非一字板), h1封板, 换手<8%, 全板块
- 买入: T+1 hour1, open_rate < -3%(低开才买)
- 止盈: +8%(盘中HIGH触发, 以目标价成交)
- 止损: -15%(盘中LOW触发, 以目标价成交)
- 到期: T+2 h4 close (max_hold_hours=4)
- 排序: 纯turn从低到高(低换手=强势锁仓=alpha核心)
"""
import re
from typing import Optional

from strategies.base import Strategy, Signal, SellSignal


_CYB_PATTERN = re.compile(r'^sz\.30[01]')
_STAR_PATTERN = re.compile(r'^sh\.688')


class LimitupEarlySealStrategy(Strategy):
    name = "limitup_early_seal"
    max_hold_hours = 4           # hours_held>=4 且 hour==4 → D+2 h4 卖出

    buy_hour = 1
    max_prev_turn = 8.0          # 昨日换手率上限(%)
    max_open_rate = -3.0         # T+1 open_rate 上限(必须低开>3%)
    take_profit_pct = 0.08       # 止盈 +8% (HIGH触发)
    stop_loss_pct = -0.15        # 止损 -15% (LOW触发)

    def __init__(self):
        self._cand_codes = set()
        self._cand_date = None

    @staticmethod
    def _get_limit_ratio(code):
        """根据板块返回涨跌幅限制比例。"""
        if _CYB_PATTERN.match(code) or _STAR_PATTERN.match(code):
            return 1.20  # 创业板/科创板 20%
        return 1.10      # 主板 10%

    def get_candidates(self, date, data_feed):
        snapshot = data_feed.get_market_snapshot(date, self.buy_hour)
        if not snapshot:
            self._cand_codes = set()
            self._cand_date = date
            return []

        # 获取前一交易日 DataFrame(含 hourly 数据)
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

        # 转为 dict 加速查找
        prev_dict = prev_df['hour1_close'].to_dict() if 'hour1_close' in prev_df.columns else {}

        # 对于昨日停牌的股票, 往前多看几天(与独立脚本per-code prev一致)
        # 构建 extra prev data: {code: {close, preclose, open, turn, h1_close}}
        extra_prev = {}
        today_df = data_feed._load_day(date)
        if today_df is not None and not today_df.empty:
            # 找出今日有数据但昨日没数据(停牌)的代码
            suspended_codes = set(today_df.index) - set(prev_df.index)
            if suspended_codes:
                date_idx = data_feed._date_index.get(date, 0)
                for back in range(2, min(6, date_idx + 1)):
                    if not suspended_codes:
                        break
                    check_date = data_feed._trading_dates[date_idx - back]
                    check_df = data_feed._load_day(check_date)
                    if check_df is None or check_df.empty:
                        continue
                    for code in list(suspended_codes):
                        if code in check_df.index:
                            row = check_df.loc[code]
                            extra_prev[code] = {
                                'close': float(row.get('close') or 0),
                                'preclose': float(row.get('preclose') or 0),
                                'open': float(row.get('open') or 0),
                                'turn': float(row.get('turn') or 0),
                                'h1_close': float(row.get('hour1_close') or 0),
                            }
                            suspended_codes.discard(code)

        # 今日 hour1_open 字典 (用于limit-up判定, 与独立脚本一致用h1_open而非daily open)
        today_h1_open_dict = (today_df['hour1_open'].to_dict()
                              if today_df is not None and not today_df.empty
                              and 'hour1_open' in today_df.columns else {})

        candidates = []

        for code, info in snapshot.items():
            # ST 过滤: 仅使用isST字段(与独立脚本一致, 不按名称排除)
            if info.get('isST'):
                continue

            # 涨跌幅比例
            limit_ratio = self._get_limit_ratio(code)

            # 昨日涨停判定 - 优先用snapshot中的prev_*,停牌股用extra_prev
            prev_close = info.get('prev_close') or 0.0
            prev_preclose = info.get('prev_preclose') or 0.0
            prev_open = info.get('prev_open') or 0.0
            prev_turn = info.get('prev_turn') or 0.0
            h1_close_val = prev_dict.get(code)

            # 如果prev数据缺失(停牌), 从extra_prev获取
            if (prev_close <= 0 or prev_preclose <= 0) and code in extra_prev:
                ep = extra_prev[code]
                prev_close = ep['close']
                prev_preclose = ep['preclose']
                prev_open = ep['open']
                prev_turn = ep['turn']
                h1_close_val = ep['h1_close']

            if prev_close <= 0 or prev_preclose <= 0:
                continue
            limit_price = round(prev_preclose * limit_ratio, 2)
            if prev_close < limit_price:
                continue  # 昨日未涨停

            # 非一字板
            if prev_open >= limit_price:
                continue

            # h1 早封: 昨日 hour1_close == 涨停价
            if h1_close_val is None:
                continue
            try:
                h1_close = float(h1_close_val)
            except (TypeError, ValueError):
                continue
            if h1_close <= 0 or round(h1_close, 2) != limit_price:
                continue

            # 换手率 < max_prev_turn
            if prev_turn <= 0 or prev_turn >= self.max_prev_turn:
                continue

            # 今日 open_rate < max_open_rate (必须低开)
            open_rate = info.get('open_rate') or 0.0
            if open_rate >= self.max_open_rate:
                continue

            # 今日h1_open有效且不涨停(与独立脚本一致用h1_open)
            preclose = info.get('preclose') or 0.0
            h1_open_val = today_h1_open_dict.get(code)
            if h1_open_val is None:
                continue
            try:
                h1_open = float(h1_open_val)
            except (TypeError, ValueError):
                continue
            if h1_open <= 0 or preclose <= 0:
                continue
            today_limit = round(preclose * limit_ratio, 2)
            if h1_open >= today_limit:
                continue

            # 收集候选
            candidates.append({
                'code': code,
                'prev_turn': prev_turn,
                'open_rate': open_rate,
            })

        if not candidates:
            self._cand_codes = set()
            self._cand_date = date
            return []

        # 按open_rate升序排序(最负=跌幅最大=反弹空间最大=alpha核心)
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
        return Signal(
            code=code,
            price=float(price),
            strategy_name=self.name,
            target_hold_hours=self.max_hold_hours,
        )

    def should_sell(self, position, date, hour, data_feed):
        # T+1 合规：买入当日禁止卖出
        if position.buy_date == date:
            return None

        buy_price = position.buy_price
        if buy_price <= 0:
            return None

        # 止盈/止损目标价
        tp_target = buy_price * (1 + self.take_profit_pct)
        sl_target = buy_price * (1 + self.stop_loss_pct)

        # 用当前hour的HIGH/LOW/OPEN检测是否触发
        bar_high = data_feed.get_hour_high(position.code, date, hour)
        bar_low = data_feed.get_hour_low(position.code, date, hour)
        bar_open = data_feed.get_hour_open(position.code, date, hour)

        if not bar_open or bar_open <= 0:
            return None

        # 获取今日preclose用于跌停价计算
        preclose = self._get_preclose(position.code, date, data_feed)
        limit_down = 0.0
        is_limit_down = False
        if preclose and preclose > 0:
            limit_down = self._calc_limit_down(position.code, preclose)
            if bar_open <= limit_down:
                if bar_low and bar_low > 0 and bar_low >= bar_open:
                    is_limit_down = True

        # 跌停板封死时: 跳过SL/TP(无法以目标价成交), 但到期仍可以close卖出
        if not is_limit_down:
            # 开盘价已触发止盈/止损 → 以open成交（gap跳空场景）
            open_pnl = (bar_open - buy_price) / buy_price
            if open_pnl >= self.take_profit_pct:
                return SellSignal(reason='take_profit', price=float(bar_open))
            if open_pnl <= self.stop_loss_pct:
                return SellSignal(reason='stop_loss', price=float(bar_open))

            # 盘中TP优先检测
            tp_hit = bar_high and bar_high > 0 and bar_high >= tp_target
            sl_hit = bar_low and bar_low > 0 and bar_low <= sl_target

            if tp_hit:
                return SellSignal(reason='take_profit', price=float(tp_target))

            if sl_hit:
                actual_sl = sl_target
                if limit_down > 0:
                    actual_sl = max(sl_target, limit_down)
                return SellSignal(reason='stop_loss', price=float(actual_sl))

        # 到期: hours_held >= max_hold_hours 且 hour == 4 → close卖(即使跌停也强制退出)
        if position.hours_held >= self.max_hold_hours and hour == 4:
            close_price = data_feed.get_hour_close(position.code, date, hour)
            sell_price = close_price if close_price and close_price > 0 else bar_open
            return SellSignal(reason='expired', price=float(sell_price))

        return None

    @staticmethod
    def _calc_limit_down(code, preclose):
        """计算跌停价。"""
        if _CYB_PATTERN.match(code) or _STAR_PATTERN.match(code):
            return round(preclose * 0.80, 2)  # 20%板
        return round(preclose * 0.90, 2)      # 10%板

    def _get_preclose(self, code, date, data_feed):
        """获取当日preclose（用于计算跌停价）。"""
        today = data_feed._load_day(date)
        if today is None or today.empty or code not in today.index:
            return 0.0
        row = today.loc[code]
        return float(row.get('preclose') or 0.0)

    def get_buy_price(self, code, date, hour, data_feed):
        return data_feed.get_hour_open(code, date, hour)
