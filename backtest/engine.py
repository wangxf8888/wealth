"""回测引擎 - 小时级主循环。

每个交易日按 hour [1,2,3,4] 推进：
  1. 用当前 hour 的 open 更新持仓市值 -> 记录 NAV
  2. 卖出检查（策略 should_sell），成交价 = 当前 hour open
  3. 买入检查（策略 should_buy），成交价 = 当前 hour open
  4. tick_hour：持仓 hours_held +1

合规：策略只能看到 hour-1 及更早数据；成交价用 hour 的 open（该 hour 开始即确定）。
"""
import logging
import time
from .portfolio import Portfolio

logger = logging.getLogger(__name__)


class BacktestEngine:
    def __init__(self, strategy, data_feed, n_slots: int = 1,
                 initial_capital: float = 1_000_000,
                 market_filter: bool = False,
                 market_filter_threshold: float = -1.0):
        self.strategy = strategy
        self.data_feed = data_feed
        self.portfolio = Portfolio(n_slots, initial_capital)
        self.nav_history = []      # [(date, hour, nav)]
        self._start_date = None
        self._end_date = None
        self.market_filter = market_filter
        self.market_filter_threshold = market_filter_threshold
        self._market_filter_skip_days = 0  # 统计被过滤天数

    def run(self, start_date: str, end_date: str) -> dict:
        self._start_date = start_date
        self._end_date = end_date
        t0 = time.time()
        trading_dates = self.data_feed.get_trading_dates(start_date, end_date)
        print(f"交易日总数: {len(trading_dates)} ({start_date} ~ {end_date})")

        for date in trading_dates:
            # 开盘前：筛选候选股（仅用 prev_day 数据）
            try:
                candidates = self.strategy.get_candidates(date, self.data_feed)
            except NotImplementedError:
                candidates = []
            candidates = candidates or []

            # 大盘过滤：前一交易日指数大跌则当日跳过买入（不影响卖出）
            market_down_today = False
            if self.market_filter:
                market_down_today = self.data_feed.is_market_down(
                    date, self.market_filter_threshold)
                if market_down_today:
                    self._market_filter_skip_days += 1

            for hour in (1, 2, 3, 4):
                self.portfolio.set_context(date, hour)

                # 1. 更新持仓市值 -> NAV
                cur_prices = self._current_prices(date, hour)
                nav = self.portfolio.get_nav(cur_prices)
                self.nav_history.append((date, hour, nav))

                # 2. 卖出检查（大盘过滤不影响卖出，止盈止损正常执行）
                for pos in self.portfolio.get_active_positions():
                    sell_signal = self.strategy.should_sell(
                        pos, date, hour, self.data_feed)
                    if sell_signal:
                        # T+1硬性校验: 卖出日必须严格晚于买入日(raise不可绕过)
                        if not (pos.buy_date < date):
                            raise RuntimeError(
                                f"T+0违规！{pos.code} buy={pos.buy_date} "
                                f"sell={date} - 违反A股T+1交易规则")
                        # 优先使用策略提供的价格，否则用 hour open
                        sell_price = sell_signal.price
                        if not sell_price or sell_price <= 0:
                            sell_price = self.data_feed.get_hour_open(
                                pos.code, date, hour)
                        if sell_price and sell_price > 0:
                            self.portfolio.sell(
                                pos.slot_id, sell_price, sell_signal.reason)

                # 3. 买入检查
                # 大盘过滤：跳过买入
                if market_down_today:
                    self.portfolio.tick_hour()
                    continue
                # 如果策略要求sell_day_no_buy，卖出当天不买入新仓
                can_buy = True
                if getattr(self.strategy, 'sell_day_no_buy', False):
                    if self.portfolio.sold_today():
                        can_buy = False
                if self.portfolio.has_empty_slot() and candidates and can_buy:
                    for code in candidates:
                        if not self.portfolio.has_empty_slot():
                            break
                        if code in self.portfolio.held_codes():
                            continue
                        # IPO过滤：上市前5个交易日不买入
                        if self.data_feed.is_ipo_period(code, date):
                            continue
                        signal = self.strategy.should_buy(
                            code, date, hour, self.data_feed, self.portfolio)
                        if signal:
                            # 优先使用策略提供的买入价(如MOC/尾盘买入)
                            # 否则用当前hour open（向后兼容）
                            if signal.price and signal.price > 0:
                                # BUG#1修复：策略指定价格时，仍需校验当日是否有交易数据
                                # 停牌股无小时K线，get_hour_open返回0
                                h1_check = self.data_feed.get_hour_open(code, date, 1)
                                if not h1_check or h1_check <= 0:
                                    continue  # 停牌，跳过该信号
                                buy_price = signal.price
                            else:
                                buy_price = self.data_feed.get_hour_open(
                                    code, date, hour)
                            if buy_price and buy_price > 0:
                                self.portfolio.buy(signal, buy_price)

                # 4. 小时结束
                self.portfolio.tick_hour()

        elapsed = time.time() - t0
        if self.market_filter:
            print(f"回测耗时: {elapsed:.1f}秒 | 大盘过滤跳过: {self._market_filter_skip_days}天")
        else:
            print(f"回测耗时: {elapsed:.1f}秒")
        return self.get_summary()

    # ------------------------------------------------------------------
    def _current_prices(self, date: str, hour: int) -> dict:
        """用当前 hour 的 open 作为持仓最新价（该价格 hour 开始即确定，合规）。"""
        prices = {}
        for pos in self.portfolio.get_active_positions():
            p = self.data_feed.get_hour_open(pos.code, date, hour)
            if p and p > 0:
                prices[pos.code] = p
        return prices

    # ------------------------------------------------------------------
    def get_summary(self) -> dict:
        """统计 CAGR / MaxDD / 胜率 / 平均收益 / 笔数等。"""
        trades = self.portfolio.get_all_trades()
        final_nav = self.nav_history[-1][2] if self.nav_history else \
            self.portfolio.initial_capital
        init_cap = self.portfolio.initial_capital
        total_return = (final_nav / init_cap - 1) * 100 if init_cap else 0.0

        # 年数：按交易日跨度估算（244 交易日/年）
        n_hours = len(self.nav_history)
        years = (n_hours / 4) / 244 if n_hours else 0.0
        if years > 0 and final_nav > 0 and init_cap > 0:
            cagr = ((final_nav / init_cap) ** (1 / years) - 1) * 100
        else:
            cagr = 0.0

        max_dd = self._max_drawdown()

        n = len(trades)
        wins = [t for t in trades if t.profit_pct > 0]
        win_rate = (len(wins) / n * 100) if n else 0.0
        avg_profit = (sum(t.profit_pct for t in trades) / n) if n else 0.0
        avg_hold = (sum(t.hold_hours for t in trades) / n) if n else 0.0

        summary = {
            'start_date': self._start_date,
            'end_date': self._end_date,
            'initial_capital': init_cap,
            'final_nav': round(final_nav, 2),
            'total_return_pct': round(total_return, 2),
            'cagr_pct': round(cagr, 2),
            'max_drawdown_pct': round(max_dd, 2),
            'n_trades': n,
            'win_rate_pct': round(win_rate, 2),
            'avg_profit_pct': round(avg_profit, 2),
            'avg_hold_hours': round(avg_hold, 2),
        }
        return summary

    def _max_drawdown(self) -> float:
        """最大回撤(%)，基于 NAV 序列。"""
        peak = None
        max_dd = 0.0
        for _, _, nav in self.nav_history:
            if peak is None or nav > peak:
                peak = nav
            if peak and peak > 0:
                dd = (peak - nav) / peak * 100
                if dd > max_dd:
                    max_dd = dd
        return max_dd

    def print_summary(self, summary: dict = None):
        s = summary or self.get_summary()
        print("=" * 50)
        print(f"回测区间   : {s['start_date']} ~ {s['end_date']}")
        print(f"初始资金   : {s['initial_capital']:,.0f}")
        print(f"期末净值   : {s['final_nav']:,.2f}")
        print(f"总收益率   : {s['total_return_pct']:+.2f}%")
        print(f"年化(CAGR) : {s['cagr_pct']:+.2f}%")
        print(f"最大回撤   : {s['max_drawdown_pct']:.2f}%")
        print(f"交易笔数   : {s['n_trades']}")
        print(f"胜率       : {s['win_rate_pct']:.2f}%")
        print(f"平均收益   : {s['avg_profit_pct']:+.2f}%")
        print(f"平均持有   : {s['avg_hold_hours']:.2f} 小时")
        print("=" * 50)
