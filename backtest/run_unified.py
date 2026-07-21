"""统一资金池5策略组合回测 - 等权再平衡模式。

关键区别：
- run_combined: 5个独立portfolio各自compound → 高CAGR策略独立复利后主导结果，不真实
- run_unified:  1个共享portfolio，每次买入=total_nav/5 → 等权再平衡，真实反映实盘

资金模型:
- 1个账户，初始100万
- N个slot（每个策略1个固定slot）
- 每次买入金额 = 当前总净值 / N（动态等权）
- 所有策略共用一个cash pool
- 一个策略赚的钱回到pool，下次任何策略买入都受益
- 最多同时持仓N只（每策略最多1只）

用法:
  python -m backtest.run_unified --strategies big_yang_low_open_v2,gem_star_limitup_low_open,gem_star_late_seal,amplitude_reversal,limitup_early_seal \
      --start 2021-01-01 --end 2026-07-01

  python -m backtest.run_unified --strategies big_yang_low_open_v2,amplitude_reversal \
      --start 2021-01-01 --end 2026-07-01
"""
import argparse
import importlib
import inspect
import json
import os
import time
from collections import defaultdict
from dataclasses import dataclass, asdict
from typing import Optional, List, Dict

from strategies.base import Strategy, Signal, SellSignal
from backtest.data_feed import BacktestDataFeed

DB_PATH = '/home/AIWealth/data/stocks.db'
LOG_DIR = '/home/AIWealth/logs/backtest'
FRONTEND_DIR = '/home/AIWealth/frontend/data'


# ==================================================================
# UnifiedPortfolio - 统一资金池仓位管理
# ==================================================================

@dataclass
class Position:
    slot_id: int
    code: str
    buy_price: float
    buy_date: str
    buy_hour: int
    strategy_name: str
    target_hold_hours: int
    hours_held: int = 0
    shares: int = 0


@dataclass
class TradeRecord:
    code: str
    strategy_name: str
    buy_date: str
    buy_hour: int
    buy_price: float
    sell_date: str
    sell_hour: int
    sell_price: float
    hold_hours: int
    profit_pct: float
    reason: str = ''


class UnifiedPortfolio:
    """统一资金池 - N个slot共享现金池，买入金额=总净值/N。"""

    def __init__(self, n_slots: int, initial_capital: float = 1_000_000,
                 cost_rate: float = 0.001):
        self.n_slots = n_slots
        self.initial_capital = initial_capital
        self.cost_rate = cost_rate
        self.cash = initial_capital
        self.slots: Dict[int, Optional[Position]] = {i: None for i in range(n_slots)}
        self.trades: List[TradeRecord] = []
        self._cur_date = ''
        self._cur_hour = 0
        self._last_sell_dates: Dict[int, str] = {}  # slot_id -> last sell date

    def set_context(self, date: str, hour: int):
        self._cur_date = date
        self._cur_hour = hour

    def is_slot_empty(self, slot_id: int) -> bool:
        return self.slots[slot_id] is None

    def get_position(self, slot_id: int) -> Optional[Position]:
        return self.slots[slot_id]

    def get_active_positions(self) -> List[Position]:
        return [p for p in self.slots.values() if p is not None]

    def held_codes(self) -> set:
        return {p.code for p in self.slots.values() if p is not None}

    def get_nav(self, current_prices: dict = None) -> float:
        """当前净值 = 现金 + 持仓市值。"""
        total = self.cash
        for pos in self.slots.values():
            if pos is None:
                continue
            price = pos.buy_price
            if current_prices and pos.code in current_prices:
                cp = current_prices[pos.code]
                if cp and cp > 0:
                    price = cp
            total += pos.shares * price
        return total

    def get_buy_amount(self, current_prices: dict = None) -> float:
        """每个slot的买入目标金额 = 总净值/N。"""
        return self.get_nav(current_prices) / self.n_slots

    def buy_slot(self, slot_id: int, signal: Signal, price: float,
                 current_prices: dict = None) -> bool:
        """在指定slot买入。买入金额 = 总净值/N。"""
        if price is None or price <= 0:
            return False
        if self.slots[slot_id] is not None:
            return False
        if signal.code in self.held_codes():
            return False

        target_amount = self.get_buy_amount(current_prices)
        buy_amount = min(target_amount, self.cash)
        shares = int(buy_amount / (price * (1 + self.cost_rate)) / 100) * 100
        if shares <= 0:
            return False

        cost = shares * price * (1 + self.cost_rate)
        if cost > self.cash:
            return False
        self.cash -= cost

        actual_buy_date = getattr(signal, 'signal_date', '') or self._cur_date
        self.slots[slot_id] = Position(
            slot_id=slot_id,
            code=signal.code,
            buy_price=price,
            buy_date=actual_buy_date,
            buy_hour=self._cur_hour,
            strategy_name=signal.strategy_name,
            target_hold_hours=signal.target_hold_hours,
            hours_held=0,
            shares=shares,
        )
        return True

    def sell(self, slot_id: int, price: float, reason: str) -> Optional[TradeRecord]:
        """卖出指定slot，收益回到共享现金池。"""
        pos = self.slots.get(slot_id)
        if pos is None or price is None or price <= 0:
            return None

        proceeds = pos.shares * price * (1 - self.cost_rate)
        self.cash += proceeds

        if pos.buy_price > 0:
            profit_pct = (price * (1 - self.cost_rate) /
                          (pos.buy_price * (1 + self.cost_rate)) - 1) * 100
        else:
            profit_pct = 0.0

        rec = TradeRecord(
            code=pos.code,
            strategy_name=pos.strategy_name,
            buy_date=pos.buy_date,
            buy_hour=pos.buy_hour,
            buy_price=pos.buy_price,
            sell_date=self._cur_date,
            sell_hour=self._cur_hour,
            sell_price=price,
            hold_hours=pos.hours_held,
            profit_pct=profit_pct,
            reason=reason,
        )
        self.trades.append(rec)
        self.slots[slot_id] = None
        self._last_sell_dates[slot_id] = self._cur_date
        return rec

    def sold_today(self, slot_id: int) -> bool:
        """指定slot当天是否已卖出。"""
        return self._last_sell_dates.get(slot_id, '') == self._cur_date

    def tick_hour(self):
        """每小时调用：所有持仓 hours_held += 1。"""
        for pos in self.slots.values():
            if pos is not None:
                pos.hours_held += 1


# ==================================================================
# UnifiedBacktestEngine - 统一资金池多策略引擎
# ==================================================================

class UnifiedBacktestEngine:
    """统一资金池回测引擎 - 多策略共享一个Portfolio。

    每个策略分配一个固定slot:
    - slot 0 -> strategy[0]
    - slot 1 -> strategy[1]
    - ...
    """

    def __init__(self, strategies: Dict[int, Strategy], data_feed: BacktestDataFeed,
                 initial_capital: float = 1_000_000,
                 market_filter: bool = False,
                 market_filter_threshold: float = -1.0):
        self.strategies = strategies  # {slot_id: strategy_instance}
        self.data_feed = data_feed
        n_slots = len(strategies)
        self.portfolio = UnifiedPortfolio(n_slots, initial_capital)
        self.nav_history = []  # [(date, hour, nav)]
        self.daily_nav = {}    # {date: nav} 每日收盘NAV
        self._start_date = None
        self._end_date = None
        self.market_filter = market_filter
        self.market_filter_threshold = market_filter_threshold
        self._market_filter_skip_days = 0

    def run(self, start_date: str, end_date: str) -> dict:
        self._start_date = start_date
        self._end_date = end_date
        t0 = time.time()
        trading_dates = self.data_feed.get_trading_dates(start_date, end_date)
        print(f"[Unified] 交易日总数: {len(trading_dates)} ({start_date} ~ {end_date})")
        print(f"[Unified] 策略数: {len(self.strategies)}, 初始资金: {self.portfolio.initial_capital:,.0f}")

        for date in trading_dates:
            # 开盘前：每个策略独立筛选候选股
            candidates_map = {}  # {slot_id: [codes]}
            for slot_id, strategy in self.strategies.items():
                try:
                    cands = strategy.get_candidates(date, self.data_feed)
                except NotImplementedError:
                    cands = []
                candidates_map[slot_id] = cands or []

            # 大盘过滤
            market_down_today = False
            if self.market_filter:
                market_down_today = self.data_feed.is_market_down(
                    date, self.market_filter_threshold)
                if market_down_today:
                    self._market_filter_skip_days += 1

            for hour in (1, 2, 3, 4):
                self.portfolio.set_context(date, hour)

                # 1. 更新持仓市值 -> 计算NAV
                cur_prices = self._current_prices(date, hour)
                nav = self.portfolio.get_nav(cur_prices)
                self.nav_history.append((date, hour, nav))

                # 2. 卖出检查（遍历所有occupied slots）
                for slot_id, strategy in self.strategies.items():
                    pos = self.portfolio.get_position(slot_id)
                    if pos is None:
                        continue
                    sell_signal = strategy.should_sell(pos, date, hour, self.data_feed)
                    if sell_signal:
                        # T+1硬性校验
                        if not (pos.buy_date < date):
                            raise RuntimeError(
                                f"T+0违规！{pos.code} buy={pos.buy_date} "
                                f"sell={date} - 违反A股T+1交易规则")
                        sell_price = sell_signal.price
                        if not sell_price or sell_price <= 0:
                            sell_price = self.data_feed.get_hour_open(
                                pos.code, date, hour)
                        if sell_price and sell_price > 0:
                            self.portfolio.sell(slot_id, sell_price, sell_signal.reason)

                # 3. 买入检查（仅处理空slot + 非大盘过滤日）
                if market_down_today:
                    self.portfolio.tick_hour()
                    continue

                for slot_id, strategy in self.strategies.items():
                    if not self.portfolio.is_slot_empty(slot_id):
                        continue
                    # sell_day_no_buy: 卖出当天不买入
                    if getattr(strategy, 'sell_day_no_buy', False):
                        if self.portfolio.sold_today(slot_id):
                            continue

                    candidates = candidates_map.get(slot_id, [])
                    if not candidates:
                        continue

                    for code in candidates:
                        if code in self.portfolio.held_codes():
                            continue
                        # IPO过滤
                        if self.data_feed.is_ipo_period(code, date):
                            continue
                        signal = strategy.should_buy(
                            code, date, hour, self.data_feed, self.portfolio)
                        if signal:
                            if signal.price and signal.price > 0:
                                h1_check = self.data_feed.get_hour_open(code, date, 1)
                                if not h1_check or h1_check <= 0:
                                    continue
                                buy_price = signal.price
                            else:
                                buy_price = self.data_feed.get_hour_open(
                                    code, date, hour)
                            if buy_price and buy_price > 0:
                                success = self.portfolio.buy_slot(
                                    slot_id, signal, buy_price, cur_prices)
                                if success:
                                    break  # 该slot已占用，下一个策略

                # 4. 小时结束
                self.portfolio.tick_hour()

            # 日末记录daily nav
            day_end_prices = self._day_end_prices(date)
            day_end_nav = self.portfolio.get_nav(day_end_prices)
            self.daily_nav[date] = round(day_end_nav, 2)

        elapsed = time.time() - t0
        if self.market_filter:
            print(f"[Unified] 回测耗时: {elapsed:.1f}秒 | 大盘过滤跳过: {self._market_filter_skip_days}天")
        else:
            print(f"[Unified] 回测耗时: {elapsed:.1f}秒")
        return self.get_summary()

    def _current_prices(self, date: str, hour: int) -> dict:
        """用当前hour的open更新持仓价格。"""
        prices = {}
        for pos in self.portfolio.get_active_positions():
            p = self.data_feed.get_hour_open(pos.code, date, hour)
            if p and p > 0:
                prices[pos.code] = p
        return prices

    def _day_end_prices(self, date: str) -> dict:
        """用当日hour4 close作为日末价格。"""
        prices = {}
        for pos in self.portfolio.get_active_positions():
            p = self.data_feed.get_hour_close(pos.code, date, 4)
            if p and p > 0:
                prices[pos.code] = p
            else:
                # fallback: hour4 open
                p2 = self.data_feed.get_hour_open(pos.code, date, 4)
                if p2 and p2 > 0:
                    prices[pos.code] = p2
        return prices

    def get_summary(self) -> dict:
        """计算CAGR / MaxDD / 胜率等。"""
        trades = self.portfolio.trades
        final_nav = self.nav_history[-1][2] if self.nav_history else \
            self.portfolio.initial_capital
        init_cap = self.portfolio.initial_capital
        total_return = (final_nav / init_cap - 1) * 100 if init_cap else 0.0

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

        # 每策略统计
        per_strategy = defaultdict(lambda: {'n': 0, 'wins': 0, 'total_pnl': 0.0})
        for t in trades:
            ps = per_strategy[t.strategy_name]
            ps['n'] += 1
            if t.profit_pct > 0:
                ps['wins'] += 1
            ps['total_pnl'] += t.profit_pct

        strategy_stats = {}
        for name, ps in per_strategy.items():
            strategy_stats[name] = {
                'n_trades': ps['n'],
                'win_rate_pct': round(ps['wins'] / ps['n'] * 100, 2) if ps['n'] else 0,
                'avg_profit_pct': round(ps['total_pnl'] / ps['n'], 2) if ps['n'] else 0,
            }

        # 年度收益
        yearly_returns = self._yearly_returns()

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
            'per_strategy': strategy_stats,
            'yearly_returns': yearly_returns,
        }
        return summary

    def _max_drawdown(self) -> float:
        """最大回撤(%)，基于NAV序列。"""
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

    def _yearly_returns(self) -> dict:
        """按年统计收益率。"""
        if not self.daily_nav:
            return {}
        sorted_dates = sorted(self.daily_nav.keys())
        years = sorted(set(d[:4] for d in sorted_dates))
        yearly = {}
        init = self.portfolio.initial_capital
        for year in years:
            year_dates = [d for d in sorted_dates if d[:4] == year]
            if not year_dates:
                continue
            end_nav = self.daily_nav[year_dates[-1]]
            # 年初NAV：前一年末或初始资金
            prev_year_dates = [d for d in sorted_dates if d[:4] < year]
            start_nav = self.daily_nav[prev_year_dates[-1]] if prev_year_dates else init
            ret = (end_nav / start_nav - 1) * 100 if start_nav > 0 else 0
            yearly[year] = round(ret, 2)
        return yearly

    def print_summary(self, summary: dict = None):
        s = summary or self.get_summary()
        print("\n" + "=" * 60)
        print(f"{'统一资金池 5策略组合回测结果':^60}")
        print("=" * 60)
        print(f"回测区间   : {s['start_date']} ~ {s['end_date']}")
        print(f"初始资金   : {s['initial_capital']:,.0f}")
        print(f"期末净值   : {s['final_nav']:,.2f}")
        print(f"总收益率   : {s['total_return_pct']:+.2f}%")
        print(f"年化(CAGR) : {s['cagr_pct']:+.2f}%")
        print(f"最大回撤   : {s['max_drawdown_pct']:.2f}%")
        print(f"交易笔数   : {s['n_trades']}")
        print(f"胜率       : {s['win_rate_pct']:.2f}%")
        print(f"平均收益   : {s['avg_profit_pct']:+.2f}%")
        print(f"平均持有   : {s['avg_hold_hours']:.1f} 小时")
        print("=" * 60)

        # 各策略分项
        if s.get('per_strategy'):
            print(f"\n{'各策略分项表现':^60}")
            print("-" * 60)
            print(f"{'策略':<30}{'笔数':>6}{'胜率%':>8}{'均收益%':>10}")
            print("-" * 60)
            for name, ps in s['per_strategy'].items():
                print(f"{name:<30}{ps['n_trades']:>6}{ps['win_rate_pct']:>8.2f}"
                      f"{ps['avg_profit_pct']:>+10.2f}")
            print("-" * 60)

        # 年度收益
        if s.get('yearly_returns'):
            print(f"\n{'年度收益':^60}")
            print("-" * 60)
            for year, ret in sorted(s['yearly_returns'].items()):
                print(f"  {year}: {ret:+.2f}%")
            print("-" * 60)


# ==================================================================
# 策略加载与主入口
# ==================================================================

def load_strategy(name: str) -> Strategy:
    """从 strategies.<name> 动态加载 Strategy 子类并实例化。"""
    module = importlib.import_module(f'strategies.{name}')
    for _, obj in inspect.getmembers(module, inspect.isclass):
        if issubclass(obj, Strategy) and obj is not Strategy \
                and obj.__module__ == module.__name__:
            return obj()
    raise ValueError(f"在 strategies.{name} 中未找到 Strategy 子类")


def run_unified_backtest(strategy_names: list, start_date: str, end_date: str,
                         initial_capital: float = 1_000_000,
                         market_filter: bool = False,
                         market_filter_threshold: float = -1.0) -> dict:
    """统一资金池回测 - 便捷API。

    Returns:
        dict: {summary, daily_nav, trades}
    """
    data_feed = BacktestDataFeed(DB_PATH)

    # 每个策略分配一个固定slot
    strategies = {}
    for i, name in enumerate(strategy_names):
        strategies[i] = load_strategy(name)
        print(f"  Slot {i}: {name} ({strategies[i].name})")

    engine = UnifiedBacktestEngine(
        strategies, data_feed,
        initial_capital=initial_capital,
        market_filter=market_filter,
        market_filter_threshold=market_filter_threshold,
    )

    summary = engine.run(start_date, end_date)
    engine.print_summary(summary)

    data_feed.close()

    return {
        'summary': summary,
        'daily_nav': engine.daily_nav,
        'trades': engine.portfolio.trades,
        'nav_history': engine.nav_history,
    }


def main():
    parser = argparse.ArgumentParser(description='统一资金池多策略组合回测（等权再平衡）')
    parser.add_argument('--strategies', required=True,
                        help='逗号分隔的策略名')
    parser.add_argument('--start', required=True, help='起始日期 YYYY-MM-DD')
    parser.add_argument('--end', required=True, help='结束日期 YYYY-MM-DD')
    parser.add_argument('--capital', type=float, default=1_000_000, help='总初始资金')
    parser.add_argument('--db', default=DB_PATH, help='数据库路径')
    parser.add_argument('--market-filter', dest='market_filter', action='store_true',
                        default=False, help='启用大盘过滤')
    parser.add_argument('--market-filter-threshold', type=float, default=-1.0,
                        help='大盘过滤阈值(%%)')
    parser.add_argument('--output', type=str, default=None,
                        help='输出JSON文件路径(默认自动生成)')
    args = parser.parse_args()

    strategy_names = [s.strip() for s in args.strategies.split(',')]
    n = len(strategy_names)

    print(f"=" * 60)
    print(f"统一资金池回测 | 策略数: {n} | 初始资金: {args.capital:,.0f}")
    print(f"模式: 共享现金池 + 等权再平衡 (每次买入=总净值/{n})")
    print(f"策略: {', '.join(strategy_names)}")
    if args.market_filter:
        print(f"大盘过滤: 开启 (阈值={args.market_filter_threshold}%)")
    print(f"=" * 60 + "\n")

    result = run_unified_backtest(
        strategy_names, args.start, args.end,
        initial_capital=args.capital,
        market_filter=args.market_filter,
        market_filter_threshold=args.market_filter_threshold,
    )

    # ===== 输出文件 =====
    # 1. daily_nav JSON (供前端使用)
    os.makedirs(FRONTEND_DIR, exist_ok=True)
    nav_json_path = os.path.join(FRONTEND_DIR, 'unified_5slot_nav.json')
    nav_payload = {
        'mode': 'unified_pool',
        'initial_capital': args.capital,
        'n_slots': n,
        'strategies': strategy_names,
        'summary': result['summary'],
        'daily_nav': result['daily_nav'],
        'trades': [asdict(t) for t in result['trades']],
    }
    with open(nav_json_path, 'w', encoding='utf-8') as f:
        json.dump(nav_payload, f, ensure_ascii=False, indent=2)
    print(f"\n[输出] daily_nav JSON: {nav_json_path}")

    # 2. 兼容旧格式: combined_5slot_new_trades.json (供server.py /api/nav_history使用)
    compat_path = os.path.join(FRONTEND_DIR, 'combined_5slot_new_trades.json')
    compat_payload = {
        'mode': 'unified_pool',
        'combined_summary': result['summary'],
        'strategies': strategy_names,
        'daily_nav': result['daily_nav'],
        'trades': [asdict(t) for t in result['trades']],
    }
    with open(compat_path, 'w', encoding='utf-8') as f:
        json.dump(compat_payload, f, ensure_ascii=False, indent=2)
    print(f"[输出] 兼容JSON: {compat_path}")

    # 3. 交易明细日志
    os.makedirs(LOG_DIR, exist_ok=True)
    log_path = os.path.join(LOG_DIR, 'unified_5slot_trades.json')
    with open(log_path, 'w', encoding='utf-8') as f:
        json.dump(nav_payload, f, ensure_ascii=False, indent=2)
    print(f"[输出] 完整日志: {log_path}")

    # 4. TXT明细（标配输出）
    try:
        from backtest.txt_formatter import TxtFormatter
        fmt = TxtFormatter(DB_PATH)
        txt_path = fmt.generate('unified_5slot', result['summary'],
                                result['trades'], LOG_DIR)
        fmt.close()
        print(f"[输出] TXT明细: {txt_path}")
    except Exception as e:
        print(f"[警告] TXT明细生成失败: {e}")

    print("\n[完成] 统一资金池回测全部完成。")


if __name__ == '__main__':
    main()
