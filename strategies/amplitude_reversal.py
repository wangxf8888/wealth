"""振幅强度选股策略 - 大上影冲高回落→次日低开反弹买入。

策略逻辑(方向2 - 已验证有效):
  信号: 昨日振幅强度2(上影-下影) >= 4% (冲高大幅回落, 上影线远长于下影线)
  条件: 今日开盘低开(open_rate < 0%) - T+1卖压释放后的恐慌性低开
  买入: 今日hour1 open (9:30开盘价)
  选股: amp2最大的top_n只中选turn最低的1只(低换手=筹码集中=弹性大)
  止盈: +5% (HIGH触发精确成交)
  止损: -15% (LOW触发精确成交) - 宽止损避免波动股过早出局
  到期: max_hold_hours=8 (D+1 hour4 close卖出)

核心逻辑:
  昨日冲高回落(大上影线)→ 被套资金T+1才能卖 → 次日恐慌低开 → 卖压过度 → 反弹

回测结果(2021-01-01 ~ 2026-07-01, slot=1, 引擎含手续费):
  amp2>=4%, TP=5%, SL=-15%, hold=8h:
  CAGR: +121.69% | 笔数: 507 | 胜率: 55.82% | MDD: 59.10% | 均收益: +1.26%/笔
  分年度(TP=5%,SL=-8%): 2021:+172%, 2022:-20%, 2023:-3%, 2024:+68%, 2025:+209%, 2026:+65%
"""
import re
from typing import Optional

from strategies.base import Strategy, Signal, SellSignal


_CYB_PATTERN = re.compile(r'^sz\.30[01]')
_STAR_PATTERN = re.compile(r'^sh\.688')


class AmplitudeReversalStrategy(Strategy):
    """大上影冲高回落 → 次日低开反弹买入策略。"""
    name = "amplitude_reversal"
    max_hold_hours = 8           # 最多持有8小时(D+1全天)

    # === 核心参数(已通过网格搜索验证最优) ===
    buy_hour = 1                 # hour1 买入
    min_amp2_pct = 4.0           # 昨日振幅强度2最低门槛(%) - 上影线减去下影线
    max_open_rate = 0.0          # 今日必须低开(open_rate < 0)
    take_profit_pct = 0.05       # 止盈 +5% (HIGH触发)
    stop_loss_pct = -0.15        # 止损 -15% (LOW触发) - 宽止损避免过早止损
    top_n = 5                    # 取amp2最大的前N只中turn最低的1只

    # 过滤条件
    min_turn = 1.0               # 最低换手率(%), 过滤僵尸股
    max_turn = 30.0              # 最高换手率(%), 过滤异常
    min_amount = 5000            # 最低成交额(万), 过滤流动性差的

    def __init__(self):
        self._cand_codes = []
        self._cand_date = None

    @staticmethod
    def _is_st(info) -> bool:
        if info.get('isST'):
            return True
        name = str(info.get('code_name') or '')
        if 'ST' in name.upper():
            return True
        return False

    @staticmethod
    def _get_limit_ratio(code):
        if _CYB_PATTERN.match(code) or _STAR_PATTERN.match(code):
            return 1.20
        return 1.10

    def get_candidates(self, date, data_feed):
        """筛选候选股 - 用prev_day计算大上影信号 + 今日低开过滤。"""
        snapshot = data_feed.get_market_snapshot(date, self.buy_hour)
        if not snapshot:
            self._cand_codes = []
            self._cand_date = date
            return []

        candidates = []

        for code, info in snapshot.items():
            # ST过滤
            if self._is_st(info):
                continue

            # 北交所过滤
            if code.startswith('bj.'):
                continue

            # 今日必须低开
            open_rate = info.get('open_rate') or 0.0
            if open_rate >= self.max_open_rate:
                continue

            # 今日不能涨停开盘
            open_price = info.get('open') or 0.0
            preclose = info.get('preclose') or 0.0
            if open_price <= 0 or preclose <= 0:
                continue
            limit_ratio = self._get_limit_ratio(code)
            if open_price >= round(preclose * limit_ratio, 2):
                continue

            # 获取前日OHLC
            prev_open = info.get('prev_open') or 0.0
            prev_high = info.get('prev_high') or 0.0
            prev_low = info.get('prev_low') or 0.0
            prev_close = info.get('prev_close') or 0.0
            prev_preclose = info.get('prev_preclose') or 0.0
            prev_turn = info.get('prev_turn') or 0.0
            prev_amount = info.get('prev_amount') or 0.0

            # 基本数据校验
            if prev_open <= 0 or prev_high <= 0 or prev_low <= 0:
                continue
            if prev_close <= 0 or prev_preclose <= 0:
                continue

            # 换手率过滤
            if prev_turn < self.min_turn or prev_turn > self.max_turn:
                continue

            # 成交额过滤(万)
            if prev_amount / 10000 < self.min_amount:
                continue

            # 计算振幅强度2 = (上影线 - 下影线) / preclose * 100
            body_top = max(prev_open, prev_close)
            body_bottom = min(prev_open, prev_close)
            upper_shadow = prev_high - body_top        # 上影线长度
            lower_shadow = body_bottom - prev_low      # 下影线长度
            amp2 = (upper_shadow - lower_shadow) / prev_preclose * 100

            # 门槛过滤
            if amp2 < self.min_amp2_pct:
                continue

            candidates.append({
                'code': code,
                'amp2': amp2,
                'turn': prev_turn,
                'open_rate': open_rate,
            })

        if not candidates:
            self._cand_codes = []
            self._cand_date = date
            return []

        # 排序: amp2降序取top_n, 再选turn最低
        candidates.sort(key=lambda x: -x['amp2'])
        top_cands = candidates[:self.top_n]
        top_cands.sort(key=lambda x: x['turn'])
        final = [top_cands[0]['code']]

        self._cand_codes = final
        self._cand_date = date
        return final

    def should_buy(self, code, date, hour, data_feed, portfolio):
        if hour != self.buy_hour:
            return None
        if date != self._cand_date or code not in self._cand_codes:
            return None

        # 防止同日再入场：卖出当天不买新股（与快速模拟一致）
        if portfolio.trades:
            last_trade = portfolio.trades[-1]
            if last_trade.sell_date == date:
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
        # T+1合规：买入当日禁止卖出
        if position.buy_date == date:
            return None

        buy_price = position.buy_price
        if buy_price <= 0:
            return None

        # 止盈/止损目标价
        tp_target = buy_price * (1 + self.take_profit_pct)
        sl_target = buy_price * (1 + self.stop_loss_pct)

        # 当前hour的HIGH/LOW/OPEN
        bar_high = data_feed.get_hour_high(position.code, date, hour)
        bar_low = data_feed.get_hour_low(position.code, date, hour)
        bar_open = data_feed.get_hour_open(position.code, date, hour)

        if not bar_open or bar_open <= 0:
            return None

        # 开盘价已触发止盈/止损 → 以open成交（gap跳空场景）
        open_pnl = (bar_open - buy_price) / buy_price
        if open_pnl >= self.take_profit_pct:
            return SellSignal(reason='take_profit', price=float(bar_open))
        if open_pnl <= self.stop_loss_pct:
            return SellSignal(reason='stop_loss', price=float(bar_open))

        # 盘中HIGH/LOW检测（保守：先检查止损）
        sl_hit = bar_low > 0 and bar_low <= sl_target
        tp_hit = bar_high > 0 and bar_high >= tp_target

        if sl_hit and tp_hit:
            return SellSignal(reason='stop_loss', price=float(sl_target))
        if sl_hit:
            return SellSignal(reason='stop_loss', price=float(sl_target))
        if tp_hit:
            return SellSignal(reason='take_profit', price=float(tp_target))

        # 到期: hours_held >= max_hold_hours 且 hour == 4 → close 卖
        if position.hours_held >= self.max_hold_hours and hour == 4:
            close_price = data_feed.get_hour_close(position.code, date, hour)
            sell_price = close_price if close_price and close_price > 0 else bar_open
            return SellSignal(reason='expired', price=float(sell_price))

        return None

    def get_buy_price(self, code, date, hour, data_feed):
        return data_feed.get_hour_open(code, date, hour)
