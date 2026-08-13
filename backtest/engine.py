"""回测引擎 - 小时级主循环。

每个交易日按 hour [1,2,3,4] 推进：
  1. 用当前 hour 的 open 更新持仓市值 -> 记录 NAV
  2. 卖出检查（策略 should_sell），成交价 = 当前 hour open
  3. 买入检查（策略 should_buy），成交价 = 当前 hour open
  4. tick_hour：持仓 hours_held +1

合规：策略只能看到 hour-1 及更早数据；成交价用 hour 的 open（该 hour 开始即确定）。

框架级兜底约束（策略无关，任何策略都无法绕过）:
  - T+1: 买入当日的卖出信号被拦截（模拟券商拒单），并计数告警
  - 涨停开盘不可买入（挂单排队买不到）
  - 整小时封死跌停不可卖出（卖单顺延）
  - 成交价钳制到该小时真实成交区间 [low, high]（越界即不可执行价格）
"""
import logging
import time

import trading_rules
import tick_scheduler
import execution_core
from .portfolio import Portfolio

logger = logging.getLogger(__name__)


class BacktestEngine:
    def __init__(self, strategy, data_feed, n_slots: int = 1,
                 initial_capital: float = 1_000_000,
                 market_filter: bool = False,
                 market_filter_threshold: float = -1.0,
                 minute_exit: bool = False,
                 position_scaler=None,
                 tick_interval: str = 'hour'):
        self.strategy = strategy
        self.data_feed = data_feed
        self.portfolio = Portfolio(n_slots, initial_capital)
        # Task#44: 逐日仓位系数(冰点减仓overlay), None=关闭零侵入
        self.position_scaler = position_scaler
        # Task#45 统一调度: tick驱动粒度(默认'hour'=现行hour循环, 零变化)
        self.tick_interval = tick_scheduler.validate_interval(tick_interval)
        # Task#54: 单策略引擎未实现bar级分发链路, 5min级策略fail-fast
        # (防Task#53缺口①式静默降级为hour边界分发)
        if getattr(strategy, 'decision_interval', 'hour') == '5min':
            raise ValueError(
                f"策略{strategy.name}声明decision_interval='5min': engine.py"
                f"未实现bar级分发, 请用 backtest.run_unified --tick-interval "
                f"5min (bar级链路见Task#54)")
        self.nav_history = []      # [(date, hour, nav)]
        self._start_date = None
        self._end_date = None
        self.market_filter = market_filter
        self.market_filter_threshold = market_filter_threshold
        self._market_filter_skip_days = 0  # 统计被过滤天数
        self._t0_sell_blocked = 0          # 框架拦截的T+0卖出信号数(策略bug指示)
        self._price_clamped = 0            # 框架钳制的越界成交价数(策略bug指示)
        self._exdiv_buy_blocked = 0        # 框架拦截的除权除息日买入数(Task#4)
        # Task#24: 卖出触线分钟级精化(默认关闭=零侵入, 走现行hour级路径)
        # 2026-07-31批准: 策略可类级声明minute_exit=True, 声明即启用
        # (与run_unified同款识别, 现仅大阳低吸)
        self.minute_exit = minute_exit
        self._minute_checker = None
        if minute_exit or getattr(strategy, 'minute_exit', False):
            from .minute_exit import MinuteExitChecker
            if MinuteExitChecker.supports(strategy):
                self._minute_checker = MinuteExitChecker(data_feed)
                print(f"[minute-exit] 卖出检查走分钟级路径(execution_core.ExitEngine)"
                      + ("(策略声明)" if not minute_exit else ""))
            else:
                print(f"[minute-exit] 策略卖出参数不完整, 回退hour级路径")

    def _exec_price(self, code: str, date: str, hour: int, price: float) -> float:
        """框架级成交价校验: 钳制到该小时真实成交区间[low, high]。"""
        low = self.data_feed.get_hour_low(code, date, hour)
        high = self.data_feed.get_hour_high(code, date, hour)
        clamped = trading_rules.clamp_price_to_bar(price, low, high)
        if clamped != price:
            self._price_clamped += 1
        return clamped

    def run(self, start_date: str, end_date: str) -> dict:
        self._start_date = start_date
        self._end_date = end_date
        t0 = time.time()
        trading_dates = self.data_feed.get_trading_dates(start_date, end_date)
        print(f"交易日总数: {len(trading_dates)} ({start_date} ~ {end_date})")

        for date in trading_dates:
            # Task#44: 开盘前设置当日买入系数(D-1涨停家数决定, 关闭态恒1.0)
            if self.position_scaler is not None:
                self.portfolio.buy_scale = self.position_scaler.get(date)

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

            # Task#45 统一调度: tick分发驱动('hour'模式与历史hour循环逐语句等价)
            for tick in tick_scheduler.day_ticks(self.tick_interval):
                hour = tick.hour
                if not tick.is_hour_start:
                    # 非hour边界tick: hour级策略无决策点((预留)5min级策略挂载点)
                    if tick.is_hour_end:
                        self.portfolio.tick_hour()
                    continue
                self.portfolio.set_context(date, hour)

                # 1. 更新持仓市值 -> NAV
                cur_prices = self._current_prices(date, hour)
                nav = self.portfolio.get_nav(cur_prices)
                self.nav_history.append((date, hour, nav))

                # 2. 卖出检查（大盘过滤不影响卖出，止盈止损正常执行）
                for pos in self.portfolio.get_active_positions():
                    if self._minute_checker is not None:
                        # Task#24: 分钟级触线判定(缺数据时checker内fallback hour级)
                        sell_signal = self._minute_checker.check(
                            pos, self.strategy, date, hour)
                    else:
                        sell_signal = self.strategy.should_sell(
                            pos, date, hour, self.data_feed)
                    if sell_signal:
                        # 框架T+1兜底: 买入当日卖出信号 → 拦截(模拟券商拒单)
                        # 不依赖策略自查; 计数供回测结束时告警排查策略bug
                        if not (pos.buy_date < date):
                            self._t0_sell_blocked += 1
                            logger.warning(
                                "T+0卖出被框架拦截: %s buy=%s sell=%s (策略=%s)",
                                pos.code, pos.buy_date, date, pos.strategy_name)
                            continue
                        # 框架涨跌停兜底: 该hour封死跌停 → 卖单无法成交，顺延
                        if self.data_feed.is_sell_blocked_limit_down(
                                pos.code, date, hour):
                            continue
                        # 优先使用策略提供的价格，否则用 hour open
                        sell_price = sell_signal.price
                        if not sell_price or sell_price <= 0:
                            sell_price = self.data_feed.get_hour_open(
                                pos.code, date, hour)
                        # 框架成交价兜底: 钳制到该小时真实成交区间
                        sell_price = self._exec_price(
                            pos.code, date, hour, sell_price)
                        if sell_price and sell_price > 0:
                            self.portfolio.sell(
                                pos.slot_id, sell_price, sell_signal.reason)

                # 3. 买入检查
                # 大盘过滤：跳过买入
                if market_down_today:
                    if tick.is_hour_end:
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
                        # 入场守卫(Task#45统一): 除权除息拦截+涨停拦截走
                        # execution_core.evaluate_open_entry(与实盘
                        # morning_decision同一份入场评估函数, 语义不变)
                        _, entry_reject = execution_core.evaluate_open_entry(
                            code=code, strategy=self.strategy.name,
                            slot_id='0', date=date,
                            open_price=self.data_feed.get_hour_open(
                                code, date, hour),
                            exchange_preclose=self.data_feed.get_day_preclose(
                                code, date),
                            prev_close=self.data_feed.get_prev_day(
                                code, date).get('close'),
                            is_st=self.data_feed.is_st_day(code, date))
                        if entry_reject == 'ex_dividend':
                            self._exdiv_buy_blocked += 1
                            logger.info(
                                "除权除息日买入被框架拦截: %s %s (策略=%s)",
                                code, date, self.strategy.name)
                            continue
                        if entry_reject == 'limit_up_open':
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
                            # 框架成交价兜底: 钳制到该小时真实成交区间
                            buy_price = self._exec_price(
                                code, date, hour, buy_price)
                            if buy_price and buy_price > 0:
                                self.portfolio.buy(signal, buy_price)

                # 4. 小时结束(5min模式下仅hour尾bar记账, 计时口径不变)
                if tick.is_hour_end:
                    self.portfolio.tick_hour()

        elapsed = time.time() - t0
        if self.market_filter:
            print(f"回测耗时: {elapsed:.1f}秒 | 大盘过滤跳过: {self._market_filter_skip_days}天")
        else:
            print(f"回测耗时: {elapsed:.1f}秒")
        # 框架兜底触发统计: 非0说明策略存在合规bug, 必须排查
        if self._t0_sell_blocked or self._price_clamped:
            print(f"[框架兜底告警] T+0卖出拦截: {self._t0_sell_blocked}次 | "
                  f"越界成交价钳制: {self._price_clamped}次 → 请排查策略逻辑")
        # 除权除息日买入拦截统计(Task#4, 非bug — 假"低开"信号被框架过滤属预期)
        if self._exdiv_buy_blocked:
            print(f"[框架守卫] 除权除息日买入拦截: {self._exdiv_buy_blocked}次"
                  f"(按hour尝试计数, 该日open_rate语义失真)")
        # Task#24: 分钟数据命中/缺口统计(防静默偏差)
        if self._minute_checker is not None:
            print(f"[minute-exit] {self.data_feed.minute_coverage_report()}")
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
