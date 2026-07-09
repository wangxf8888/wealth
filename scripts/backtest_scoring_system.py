#!/usr/bin/env python3
"""
多指标评分量化系统 - 核心回测引擎 (T+1执行模式)
整合 indicators / scoring_engine / buy_strategies / sell_strategies / position_strategies

T+1执行逻辑:
  T日收盘后: 用T日完整OHLCV计算所有指标评分，生成信号
  T+1日hour1: 以T+1日open价执行买入
这样T日的close/volume/high/low都不是未来数据(因为买入发生在T+1)

用法:
    python backtest_scoring_system.py [--slots N] [--start YYYY-MM-DD] [--end YYYY-MM-DD]
                                       [--mode star|weighted] [--min-stars N]
"""
import sys
import os
import sqlite3
import json
import math
import time
import argparse
from collections import defaultdict

# ===== 路径设置 =====
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECT_ROOT = os.path.dirname(SCRIPT_DIR)
sys.path.insert(0, PROJECT_ROOT)

from engine.indicators import INDICATOR_REGISTRY, create_indicator
from engine.scoring_engine import ScoringEngine
from engine.buy_strategies import (
    BuyStrategyManager, ScoreBuyStrategy, VShapeBuyStrategy,
    Surge7BuyStrategy, VolBreakoutBuyStrategy, LimitUpPullbackBuyStrategy,
    create_buy_strategy
)
from engine.sell_strategies import (
    SellStrategyManager, FixedTPSL, TrailingStop, TimeLimitExit,
    VolumeShrinkExit, ScoreDecayExit, create_sell_strategy
)
from engine.position_strategies import create_position_strategy
from scripts.strategy_config import CONFIG

# ===== 常量 =====
DB_PATH = os.path.join(PROJECT_ROOT, 'data', 'stocks.db')
TRADES_OUTPUT = os.path.join(SCRIPT_DIR, 'backtest_scoring_trades.json')
NAV_OUTPUT = os.path.join(SCRIPT_DIR, 'backtest_scoring_nav.json')
CANDIDATES_OUTPUT = os.path.join(SCRIPT_DIR, 'backtest_daily_candidates.json')
HISTORY_LOOKBACK = 20


def _safe_float(val, default=0.0):
    if val is None:
        return default
    try:
        f = float(val)
        if math.isnan(f) or math.isinf(f):
            return default
        return f
    except (ValueError, TypeError):
        return default


def _is_gem(code):
    if not code:
        return False
    c = code.replace('sz.', '').replace('sh.', '')
    return c.startswith('300') or c.startswith('301')


def _is_star(code):
    if not code:
        return False
    c = code.replace('sh.', '').replace('sz.', '')
    return c.startswith('688')


def _is_bse(code):
    """北交所判断"""
    if not code:
        return False
    c = code.replace('bj.', '').replace('sh.', '').replace('sz.', '')
    return c.startswith('43') or c.startswith('83') or c.startswith('87')


def _get_limit_ratio(code):
    """获取涨跌停比例: 主板10%, 创业板/科创板20%, 北交所30%"""
    if _is_bse(code):
        return 0.3
    if _is_gem(code) or _is_star(code):
        return 0.2
    return 0.1


def _get_limit_up_ratio(code):
    return _get_limit_ratio(code)


def _get_limit_down_ratio(code):
    return _get_limit_ratio(code)


def _is_limit_up_cannot_buy(row):
    """一字涨停判定：open==high==low==close 且价格>=涨停价"""
    code = row.get('code', '')
    preclose = _safe_float(row.get('preclose'))
    if preclose <= 0:
        return True
    ratio = _get_limit_ratio(code)
    limit_up_price = round(preclose * (1 + ratio), 2)
    o = _safe_float(row.get('open'))
    h = _safe_float(row.get('high'))
    lo = _safe_float(row.get('low'))
    c = _safe_float(row.get('close'))
    if o > 0 and h > 0 and lo > 0 and c > 0:
        # 一字涨停: 四价相等 且 价格>=涨停价
        if abs(o - h) < 0.001 and abs(o - lo) < 0.001 and abs(o - c) < 0.001 and o >= limit_up_price:
            return True
    return False


def _check_buy_compliance(buy_price, preclose, code):
    """合规检查：买入价不能>=涨停价"""
    if buy_price <= 0 or preclose <= 0:
        return False
    ratio = _get_limit_ratio(code)
    limit_up_price = round(preclose * (1 + ratio), 2)
    if buy_price >= limit_up_price:
        return False
    return True


def _is_limit_down_cannot_sell(sell_price, preclose, code):
    """合规检查：卖出价不能<=跌停价"""
    if sell_price <= 0 or preclose <= 0:
        return True
    ratio = _get_limit_ratio(code)
    limit_down_price = round(preclose * (1 - ratio), 2)
    if sell_price <= limit_down_price:
        return True
    return False


def _is_oneword_limit_down(row):
    """一字跌停判定: open==high==low==close 且价格<=跌停价"""
    code = row.get('code', '')
    preclose = _safe_float(row.get('preclose'))
    if preclose <= 0:
        return True
    ratio = _get_limit_ratio(code)
    limit_down_price = round(preclose * (1 - ratio), 2)
    o = _safe_float(row.get('open'))
    h = _safe_float(row.get('high'))
    lo = _safe_float(row.get('low'))
    c = _safe_float(row.get('close'))
    if o > 0 and h > 0 and lo > 0 and c > 0:
        # 必须四价相等(open==high==low==close)才是真一字跌停
        if abs(o - h) < 0.001 and abs(o - lo) < 0.001 and abs(o - c) < 0.001 and o <= limit_down_price:
            return True
    return False


def get_dates(conn, start_date, end_date):
    cur = conn.execute(
        "SELECT DISTINCT date FROM stock_kline "
        "WHERE date >= ? AND date <= ? ORDER BY date",
        (start_date, end_date)
    )
    return [r[0] for r in cur.fetchall()]


def load_date_range_data(conn, start_date, end_date):
    cur = conn.execute(
        "SELECT * FROM stock_kline WHERE date >= ? AND date <= ? "
        "ORDER BY code, date",
        (start_date, end_date)
    )
    columns = [desc[0] for desc in cur.description]
    data_by_code = defaultdict(list)
    for row_tuple in cur.fetchall():
        row = dict(zip(columns, row_tuple))
        data_by_code[row['code']].append(row)
    return data_by_code, columns


class Portfolio:
    def __init__(self, initial_capital, n_slots):
        self.initial_capital = initial_capital
        self.cash = initial_capital
        self.n_slots = n_slots
        self.positions = []
        self.trades = []
        self.nav_history = []
        self.peak_nav = initial_capital

    @property
    def total_equity(self):
        return self.cash + sum(
            p['shares'] * p.get('current_price', p['buy_price'])
            for p in self.positions
        )

    @property
    def used_slots(self):
        return len(self.positions)

    @property
    def slot_size(self):
        eq = self.total_equity
        return eq / self.n_slots if self.n_slots > 0 else eq

    def get_portfolio_info(self):
        return {
            'total_equity': self.total_equity,
            'cash': self.cash,
            'n_slots': self.n_slots,
            'used_slots': self.used_slots,
            'slot_size': self.slot_size,
        }

    def has_position(self, code):
        return any(p['code'] == code for p in self.positions)

    def buy(self, code, price, amount, date, hour, strategy_name, score,
            star_count=0, preclose=0.0):
        if price <= 0:
            return 0
        shares = int(amount / price / 100) * 100
        if shares < 100:
            return 0
        cost = shares * price
        if cost > self.cash:
            shares = int(self.cash / price / 100) * 100
            if shares < 100:
                return 0
            cost = shares * price
        self.cash -= cost
        self.positions.append({
            'code': code, 'buy_price': price, 'shares': shares,
            'buy_date': date, 'buy_hour': hour, 'hold_days': 0,
            'max_price': price, 'strategy_name': strategy_name,
            'score_at_buy': score, 'current_price': price,
            'star_count': star_count, 'preclose_buy': preclose,
        })
        return shares

    def sell(self, code, price, date, hour, reason, preclose_sell=0.0):
        for i, pos in enumerate(self.positions):
            if pos['code'] == code:
                proceeds = pos['shares'] * price
                self.cash += proceeds
                pnl_pct = (price / pos['buy_price'] - 1) * 100
                pnl_amount = round(pos['shares'] * (price - pos['buy_price']), 2)
                trade = {
                    'code': code,
                    'buy_date': pos['buy_date'],
                    'buy_price': pos['buy_price'],
                    'buy_hour': pos['buy_hour'],
                    'sell_date': date,
                    'sell_price': round(price, 3),
                    'sell_hour': hour,
                    'sell_reason': reason,
                    'pnl_pct': round(pnl_pct, 2),
                    'pnl_amount': pnl_amount,
                    'hold_days': pos['hold_days'],
                    'shares': pos['shares'],
                    'strategy_name': pos['strategy_name'],
                    'score_at_buy': round(pos['score_at_buy'], 2),
                    'star_count': pos.get('star_count', 0),
                    'preclose_buy': pos.get('preclose_buy', 0.0),
                    'preclose_sell': preclose_sell,
                }
                self.trades.append(trade)
                self.positions.pop(i)
                return pnl_pct, trade
        return None

    def update_daily(self, date, price_map):
        for pos in self.positions:
            pos['hold_days'] += 1
            code = pos['code']
            if code in price_map:
                pos['current_price'] = price_map[code]
                if price_map[code] > pos['max_price']:
                    pos['max_price'] = price_map[code]
        nav = self.total_equity
        if nav > self.peak_nav:
            self.peak_nav = nav
        dd = (nav / self.peak_nav - 1) * 100 if self.peak_nav > 0 else 0
        self.nav_history.append({
            'date': date, 'nav': round(nav, 2), 'drawdown': round(dd, 2)
        })


class BacktestScoringEngine:
    def __init__(self, config, args):
        self.config = config
        self.args = args
        n_slots = args.slots or config['portfolio']['n_slots']
        initial_cap = config['portfolio']['initial_capital']
        self.portfolio = Portfolio(initial_cap, n_slots)

        # 现实约束参数
        self.slippage = getattr(args, 'slippage', 0.002)
        self.max_per_trade = getattr(args, 'max_per_trade', 0)

        mode = args.mode or 'star'
        indicator_configs = self._build_indicator_configs(config)
        if mode == 'star':
            threshold = args.min_stars if args.min_stars else 5
        else:
            threshold = config.get('score_threshold', 5.0)
        self.scoring_engine = ScoringEngine(indicator_configs, threshold, mode)
        self.buy_manager = self._build_buy_manager(config)
        self.sell_manager = self._build_sell_manager(config)
        pos_name = config.get('position_strategy', 'equal_weight')
        self.position_strategy = create_position_strategy(pos_name)
        self.filters = config.get('filters', {})
        self.stats = {'total_signals': 0, 'total_buys': 0, 'skipped_compliance': 0}
        self.daily_candidates = []

    def _build_indicator_configs(self, config):
        raw = config.get('indicators', {})
        default_conditions = {
            'turnover_increase': '>1.5',
            'above_ma5': '==1',
            'above_ma10': '==1',
            'market_cap': '>5000000000',
            'volume_ratio': '>1.5',
            'gap_up_rate': '>2.0',
            'rsi_14': 'between(30,70)',
            'amplitude_rate': '>2.0',
            'cum_drop_n': '<-3.0',
            'momentum_score': '>0.5',
        }
        valid_params = {
            'turnover_increase': {'lookback'},
            'above_ma5': set(),
            'above_ma10': set(),
            'market_cap': {'min_cap'},
            'volume_ratio': {'lookback'},
            'gap_up_rate': set(),
            'rsi_14': {'period'},
            'amplitude_rate': set(),
            'cum_drop_n': {'lookback'},
            'momentum_score': {'lookback'},
        }
        built = {}
        for name, cfg in raw.items():
            if name not in INDICATOR_REGISTRY:
                continue
            allowed = valid_params.get(name, set())
            entry = {}
            entry['weight'] = cfg.get('weight', 1.0)
            if 'star_condition' in cfg:
                entry['star_condition'] = cfg['star_condition']
            else:
                entry['star_condition'] = default_conditions.get(name, '>0')
            for k, v in cfg.items():
                if k in allowed:
                    entry[k] = v
            built[name] = entry
        return built

    def _build_buy_manager(self, config):
        enabled = config.get('buy_strategies', ['score', 'vshape'])
        name_map = {
            'score': 'score_buy', 'vshape': 'vshape_buy',
            'surge7': 'surge7_buy', 'vol_breakout': 'vol_breakout_buy',
            'limitup_pullback': 'limitup_pullback_buy',
        }
        strategies = []
        for short_name in enabled:
            full_name = name_map.get(short_name, short_name)
            try:
                strategies.append(create_buy_strategy(full_name))
            except ValueError:
                pass
        if not strategies:
            strategies = [ScoreBuyStrategy(min_stars=5)]
        return BuyStrategyManager(strategies)

    def _build_sell_manager(self, config):
        enabled = config.get('sell_strategies', ['fixed_tpsl', 'time_limit'])
        params = config.get('sell_params', {})
        strategies = []
        for name in enabled:
            kwargs = params.get(name, {})
            try:
                strategies.append(create_sell_strategy(name, **kwargs))
            except ValueError:
                pass
        if not strategies:
            strategies = [FixedTPSL(), TimeLimitExit()]
        return SellStrategyManager(strategies)

    def _passes_filter(self, row):
        if self.filters.get('exclude_st'):
            if row.get('isST') == 1 or row.get('isST') == '1':
                return False
            name = row.get('code_name', '') or ''
            if 'ST' in name.upper():
                return False
        min_turn = self.filters.get('min_turn', 0)
        if min_turn > 0:
            turn = _safe_float(row.get('turn'))
            if turn < min_turn:
                return False
        return True

    def _get_hour_data(self, row, hour):
        prefix = f'hour{hour}_'
        return {
            'open': _safe_float(row.get(f'{prefix}open')),
            'high': _safe_float(row.get(f'{prefix}high')),
            'low': _safe_float(row.get(f'{prefix}low')),
            'close': _safe_float(row.get(f'{prefix}close')),
        }

    def run(self):
        start_date = self.args.start or '2020-01-01'
        end_date = self.args.end or '2026-06-30'
        conn = sqlite3.connect(DB_PATH)

        print(f"[回测引擎] 多指标评分系统")
        print(f"  区间: {start_date} ~ {end_date}")
        print(f"  仓位: {self.portfolio.n_slots}")
        print(f"  模式: {self.scoring_engine.mode} | 阈值: {self.scoring_engine.threshold}")
        print(f"  买入: {[s.name for s in self.buy_manager.strategies]}")
        print(f"  卖出: {[s.name for s in self.sell_manager.strategies]}")
        print(f"  滑点: {self.slippage*100:.1f}% (双边{self.slippage*200:.1f}%)")
        max_pt_str = f"{self.max_per_trade:,.0f}元" if self.max_per_trade > 0 else "不限制"
        print(f"  单笔上限: {max_pt_str}")
        print("=" * 70)

        all_dates = get_dates(conn, start_date, end_date)
        if not all_dates:
            print("[ERROR] 无交易日数据")
            conn.close()
            return
        print(f"  交易日总数: {len(all_dates)}")

        years = sorted(set(d[:4] for d in all_dates))
        t1_signals = {}

        for year in years:
            year_start_time = time.time()
            year_dates = [d for d in all_dates if d.startswith(year)]
            if not year_dates:
                continue

            first_idx = all_dates.index(year_dates[0])
            hist_start_idx = max(0, first_idx - HISTORY_LOOKBACK)
            hist_start_date = all_dates[hist_start_idx]

            data_by_code, columns = load_date_range_data(
                conn, hist_start_date, year_dates[-1]
            )
            print(f"\n[{year}] 加载 {len(data_by_code)} 只股票, "
                  f"{hist_start_date}~{year_dates[-1]}")

            code_date_idx = {}
            for code, rows in data_by_code.items():
                idx_map = {}
                for i, r in enumerate(rows):
                    td = r.get('date', '')
                    idx_map[td] = i
                code_date_idx[code] = idx_map

            for date in year_dates:
                try:
                    self._process_day(date, data_by_code, code_date_idx, t1_signals)
                except Exception as e:
                    print(f"  [WARN] {date} error: {e}")

            elapsed = time.time() - year_start_time
            year_navs = [n for n in self.portfolio.nav_history
                         if n['date'].startswith(year)]
            if year_navs:
                y_start = year_navs[0]['nav']
                y_end = year_navs[-1]['nav']
                y_ret = (y_end / y_start - 1) * 100 if y_start > 0 else 0
                y_trades = [t for t in self.portfolio.trades
                            if t['sell_date'].startswith(year)]
                y_wins = sum(1 for t in y_trades if t['pnl_pct'] > 0)
                y_wr = y_wins / len(y_trades) * 100 if y_trades else 0
                print(f"  [{year}] 收益:{y_ret:+.1f}% | 交易:{len(y_trades)} | "
                      f"胜率:{y_wr:.1f}% | 耗时:{elapsed:.1f}s")

            del data_by_code, code_date_idx

        conn.close()
        self._print_summary()
        self._save_results()

    def _process_day(self, date, data_by_code, code_date_idx, t1_signals):
        # 1. 先处理卖出
        self._process_sells(date, data_by_code, code_date_idx)
        # 2. 执行昨日产生的T+1买入信号
        self._execute_t1_buys(date, data_by_code, code_date_idx, t1_signals)
        # 3. 扫描今日数据，生成明日的T+1信号
        self._scan_signals(date, data_by_code, code_date_idx, t1_signals)
        # 4. 更新持仓价格
        price_map = {}
        for pos in self.portfolio.positions:
            code = pos['code']
            if code in code_date_idx:
                idx_map = code_date_idx[code]
                if date in idx_map:
                    row = data_by_code[code][idx_map[date]]
                    close = _safe_float(row.get('close'))
                    if close > 0:
                        price_map[code] = close
        self.portfolio.update_daily(date, price_map)

    def _execute_t1_buys(self, date, data_by_code, code_date_idx, t1_signals):
        """T+1执行: 取出昨日产生的信号，用今日hour1_open买入"""
        pending = t1_signals.pop('_pending', [])
        if not pending:
            return
        if self.portfolio.used_slots >= self.portfolio.n_slots:
            return

        # 信号已按(star_count, score)降序排好
        all_executable = []
        for sig in pending:
            code = sig['code']
            if self.portfolio.has_position(code):
                continue
            if code not in code_date_idx:
                continue
            idx_map = code_date_idx[code]
            if date not in idx_map:
                continue
            row = data_by_code[code][idx_map[date]]
            # T+1日的hour1_open作为买入价
            buy_price = _safe_float(row.get('hour1_open'))
            preclose = _safe_float(row.get('preclose'))
            if buy_price <= 0 or preclose <= 0:
                continue
            # T+1日合规检查
            if not _check_buy_compliance(buy_price, preclose, code):
                self.stats['skipped_compliance'] += 1
                continue
            if _is_limit_up_cannot_buy(row):
                continue
            sig['buy_price'] = buy_price
            sig['_row'] = row
            sig['preclose'] = preclose
            all_executable.append(sig)

        if not all_executable:
            return

        self.stats['total_signals'] += len(all_executable)

        # 记录每日候选股选择情况
        day_record = {
            'date': date,
            'available_slots': self.portfolio.n_slots - self.portfolio.used_slots,
            'candidates': [
                {'code': s['code'], 'star_count': s.get('star_count', 0),
                 'score': round(s.get('score', 0), 2),
                 'strategy': s.get('strategy_name', ''),
                 'buy_price': round(s['buy_price'], 3)}
                for s in all_executable
            ],
            'selected': [],
            'skipped_full': [],
        }

        # 仓位分配
        portfolio_info = self.portfolio.get_portfolio_info()
        formatted = []
        for sig in all_executable:
            r = sig.get('_row', {})
            formatted.append({
                'code': sig['code'],
                'price': sig['buy_price'],
                'score': sig.get('score', 0),
                'amplitude': _safe_float(r.get('amplitude_rate', 5.0)) if r else 5.0,
            })
        allocations = self.position_strategy.allocate(portfolio_info, formatted)

        for i, (alloc_sig, amount) in enumerate(allocations):
            if i >= len(all_executable):
                break
            orig_sig = all_executable[i]
            code = orig_sig['code']
            if self.portfolio.has_position(code):
                continue
            if self.portfolio.used_slots >= self.portfolio.n_slots:
                day_record['skipped_full'].append(code)
                continue
            # 应用买入滑点
            actual_buy_price = self._apply_buy_slippage(orig_sig['buy_price'])
            preclose = orig_sig.get('preclose', 0.0)
            # 应用单笔资金上限
            if self.max_per_trade > 0:
                slot_amount = min(amount, self.max_per_trade)
            else:
                slot_amount = amount
            shares = self.portfolio.buy(
                code, actual_buy_price, slot_amount, date,
                'hour1',
                orig_sig.get('strategy_name', ''),
                orig_sig.get('score', 0),
                star_count=orig_sig.get('star_count', 0),
                preclose=preclose,
            )
            if shares > 0:
                self.stats['total_buys'] += 1
                day_record['selected'].append(code)

        if day_record['candidates']:
            self.daily_candidates.append(day_record)

    def _scan_signals(self, date, data_by_code, code_date_idx, t1_signals):
        """T日扫描: 用今日完整数据计算评分，生成明日的T+1信号"""
        signals_for_tomorrow = []
        for code, rows in data_by_code.items():
            if self.portfolio.has_position(code):
                continue
            idx_map = code_date_idx.get(code)
            if not idx_map or date not in idx_map:
                continue
            idx = idx_map[date]
            row = rows[idx]
            if not self._passes_filter(row):
                continue
            # 注意: 这里不做一字涨停检查，因为我们是用T日数据计算信号
            # 一字涨停检查在T+1执行时做
            hist_start = max(0, idx - HISTORY_LOOKBACK)
            history = rows[hist_start:idx]
            try:
                scoring_result = self.scoring_engine.score_stock(row, history)
            except Exception:
                continue
            try:
                signals = self.buy_manager.scan_signals(row, history, scoring_result)
            except Exception:
                continue
            for sig in signals:
                if not sig.get('signal'):
                    continue
                # 所有信号都走T+1路径
                signals_for_tomorrow.append({
                    'code': code,
                    'score': scoring_result.get('total_score', 0),
                    'star_count': scoring_result.get('star_count', 0),
                    'strategy_name': sig.get('strategy_name', ''),
                    'reason': sig.get('reason', ''),
                })

        # 按(star_count, score)降序排序，确定明日执行优先级
        signals_for_tomorrow.sort(
            key=lambda s: (s.get('star_count', 0), s.get('score', 0)),
            reverse=True
        )
        # 存入pending，供明日执行
        t1_signals['_pending'] = signals_for_tomorrow

    def _get_max_hold_days(self):
        """从sell_manager中获取max_hold_days配置"""
        for s in self.sell_manager.strategies:
            if hasattr(s, 'max_hold_days'):
                return s.max_hold_days
        return 5  # 默认值

    def _apply_sell_slippage(self, price):
        """卖出滑点: 实际成交价 = 信号价 * (1 - slippage)"""
        return price * (1 - self.slippage)

    def _apply_buy_slippage(self, price):
        """买入滑点: 实际成交价 = 信号价 * (1 + slippage)"""
        return price * (1 + self.slippage)

    def _process_sells(self, date, data_by_code, code_date_idx):
        for pos in list(self.portfolio.positions):
            code = pos['code']
            if pos['hold_days'] == 0:
                continue
            if code not in code_date_idx:
                continue
            idx_map = code_date_idx[code]
            if date not in idx_map:
                continue
            row = data_by_code[code][idx_map[date]]
            preclose = _safe_float(row.get('preclose'))

            # 一字跌停: 正常情况跳过(无法卖出)，但超时安全阀仍可触发
            if _is_oneword_limit_down(row):
                # Fix 3: 超时安全阀 - 即使一字跌停，超过max_hold*2也强制退出
                max_hd = self._get_max_hold_days()
                if pos['hold_days'] >= max_hd * 2:
                    close_price = _safe_float(row.get('close'))
                    if close_price > 0:
                        actual_sell_price = self._apply_sell_slippage(close_price)
                        result = self.portfolio.sell(
                            code, actual_sell_price, date, 'forced_exit',
                            f'强制退出(hold={pos["hold_days"]}d,一字跌停)',
                            preclose_sell=preclose)
                        if result:
                            continue
                continue

            sold = False
            for hour in range(1, 5):
                hour_data = self._get_hour_data(row, hour)
                if hour_data['open'] <= 0:
                    continue
                day_data = {
                    'volume': _safe_float(row.get('volume')),
                    'close_rate': _safe_float(row.get('close_rate')),
                    'prev_volume': 0,
                }
                idx = idx_map[date]
                rows = data_by_code[code]
                if idx > 0:
                    day_data['prev_volume'] = _safe_float(
                        rows[idx - 1].get('volume'))
                should_sell, sell_price, reason = self.sell_manager.check_position(
                    pos, hour, hour_data, day_data,
                    preclose=preclose, code=code
                )
                if should_sell and sell_price > 0:
                    actual_sell_price = self._apply_sell_slippage(sell_price)
                    if _is_limit_down_cannot_sell(actual_sell_price, preclose, code):
                        continue
                    result = self.portfolio.sell(
                        code, actual_sell_price, date, f'hour{hour}', reason,
                        preclose_sell=preclose)
                    if result:
                        sold = True
                        break

            # Fix 1: 日级数据fallback - 当所有hourly数据无效时用daily OHLC
            if not sold:
                all_hours_invalid = all(
                    _safe_float(row.get(f'hour{h}_open')) <= 0 for h in range(1, 5)
                )
                if all_hours_invalid:
                    day_open = _safe_float(row.get('open'))
                    day_high = _safe_float(row.get('high'))
                    day_low = _safe_float(row.get('low'))
                    day_close = _safe_float(row.get('close'))
                    if day_open > 0:
                        fallback_hour_data = {
                            'open': day_open, 'high': day_high,
                            'low': day_low, 'close': day_close
                        }
                        day_data = {
                            'volume': _safe_float(row.get('volume')),
                            'close_rate': _safe_float(row.get('close_rate')),
                            'prev_volume': 0,
                        }
                        idx = idx_map[date]
                        rows = data_by_code[code]
                        if idx > 0:
                            day_data['prev_volume'] = _safe_float(
                                rows[idx - 1].get('volume'))
                        should_sell, sell_price, reason = self.sell_manager.check_position(
                            pos, 4, fallback_hour_data, day_data,
                            preclose=preclose, code=code
                        )
                        if should_sell and sell_price > 0:
                            actual_sell_price = self._apply_sell_slippage(sell_price)
                            if not _is_limit_down_cannot_sell(actual_sell_price, preclose, code):
                                result = self.portfolio.sell(
                                    code, actual_sell_price, date, 'daily_fallback', reason,
                                    preclose_sell=preclose)
                                if result:
                                    sold = True

            # Fix 3: 超时强制退出安全阀 - 超过max_hold_days*2强制以close退出
            if not sold:
                max_hd = self._get_max_hold_days()
                if pos['hold_days'] >= max_hd * 2:
                    close_price = _safe_float(row.get('close'))
                    if close_price > 0:
                        actual_sell_price = self._apply_sell_slippage(close_price)
                        result = self.portfolio.sell(
                            code, actual_sell_price, date, 'forced_exit',
                            f'强制退出(hold={pos["hold_days"]}d)',
                            preclose_sell=preclose)
                        if result:
                            sold = True

            # 更新max_price
            if not sold:
                for hour in range(1, 5):
                    hd = self._get_hour_data(row, hour)
                    if hd['high'] > pos['max_price']:
                        pos['max_price'] = hd['high']
                # 日级high也检查
                day_high = _safe_float(row.get('high'))
                if day_high > pos['max_price']:
                    pos['max_price'] = day_high

    def _print_summary(self):
        trades = self.portfolio.trades
        nav_history = self.portfolio.nav_history
        print("\n" + "=" * 70)
        print("回测结果摘要")
        print("=" * 70)
        if not nav_history:
            print("[WARN] 无净值数据")
            return
        final_nav = nav_history[-1]['nav']
        initial = self.portfolio.initial_capital
        total_return = (final_nav / initial - 1) * 100
        start_d = nav_history[0]['date']
        end_d = nav_history[-1]['date']
        days_diff = self._date_diff_days(start_d, end_d)
        years_elapsed = max(days_diff / 365.25, 0.01)
        cagr = ((final_nav / initial) ** (1 / years_elapsed) - 1) * 100
        mdd = min(n['drawdown'] for n in nav_history)
        calmar = cagr / abs(mdd) if mdd != 0 else 0
        daily_returns = []
        for i in range(1, len(nav_history)):
            prev = nav_history[i - 1]['nav']
            curr = nav_history[i]['nav']
            if prev > 0:
                daily_returns.append(curr / prev - 1)
        if daily_returns:
            avg_r = sum(daily_returns) / len(daily_returns)
            std_r = (sum((r - avg_r) ** 2 for r in daily_returns)
                     / len(daily_returns)) ** 0.5
            sharpe = (avg_r / std_r * (252 ** 0.5)) if std_r > 0 else 0
        else:
            sharpe = 0
        total_trades = len(trades)
        wins = sum(1 for t in trades if t['pnl_pct'] > 0)
        win_rate = wins / total_trades * 100 if total_trades > 0 else 0
        print(f"\n{'指标':<20}{'值':>15}")
        print("-" * 40)
        print(f"{'总收益率':<18}{total_return:>+14.1f}%")
        print(f"{'CAGR':<18}{cagr:>+14.2f}%")
        print(f"{'最大回撤MDD':<18}{mdd:>14.1f}%")
        print(f"{'Calmar比率':<18}{calmar:>14.2f}")
        print(f"{'Sharpe比率':<18}{sharpe:>14.2f}")
        print(f"{'总交易数':<18}{total_trades:>15}")
        print(f"{'胜率':<18}{win_rate:>14.1f}%")
        print(f"{'终值资金':<18}{final_nav:>15,.0f}")
        print(f"{'滑点(单边)':<18}{self.slippage*100:>13.1f}%")
        max_pt_str = f"{self.max_per_trade:,.0f}" if self.max_per_trade > 0 else "无限制"
        print(f"{'单笔上限':<18}{max_pt_str:>15}")
        # 年度汇总
        print(f"\n{'年份':<8}{'收益%':>10}{'交易数':>8}{'胜率%':>8}")
        print("-" * 38)
        year_nav_map = defaultdict(list)
        for n in nav_history:
            year_nav_map[n['date'][:4]].append(n)
        year_trades_map = defaultdict(list)
        for t in trades:
            year_trades_map[t['sell_date'][:4]].append(t)
        for yr in sorted(year_nav_map.keys()):
            navs = year_nav_map[yr]
            ys = navs[0]['nav']
            ye = navs[-1]['nav']
            yr_ret = (ye / ys - 1) * 100 if ys > 0 else 0
            yt = year_trades_map[yr]
            yw = sum(1 for t in yt if t['pnl_pct'] > 0)
            ywr = yw / len(yt) * 100 if yt else 0
            print(f"{yr:<8}{yr_ret:>+10.1f}{len(yt):>8}{ywr:>8.1f}")
        # Top10
        if trades:
            st = sorted(trades, key=lambda t: t['pnl_pct'], reverse=True)
            print(f"\n--- Top10 盈利 ---")
            for t in st[:10]:
                print(f"  {t['code']} {t['buy_date']}->{t['sell_date']} "
                      f"pnl={t['pnl_pct']:+.1f}% [{t['strategy_name']}]")
            print(f"\n--- Top10 亏损 ---")
            for t in st[-10:]:
                print(f"  {t['code']} {t['buy_date']}->{t['sell_date']} "
                      f"pnl={t['pnl_pct']:+.1f}% [{t['strategy_name']}]")

    def _save_results(self):
        with open(TRADES_OUTPUT, 'w', encoding='utf-8') as f:
            json.dump(self.portfolio.trades, f, ensure_ascii=False, indent=2)
        print(f"\n[输出] 交易明细: {TRADES_OUTPUT} ({len(self.portfolio.trades)} 笔)")
        with open(NAV_OUTPUT, 'w', encoding='utf-8') as f:
            json.dump(self.portfolio.nav_history, f, ensure_ascii=False, indent=2)
        print(f"[输出] 净值序列: {NAV_OUTPUT} ({len(self.portfolio.nav_history)} 天)")
        with open(CANDIDATES_OUTPUT, 'w', encoding='utf-8') as f:
            json.dump(self.daily_candidates, f, ensure_ascii=False, indent=2)
        print(f"[输出] 候选股日志: {CANDIDATES_OUTPUT} ({len(self.daily_candidates)} 天有信号)")

    @staticmethod
    def _date_diff_days(d1, d2):
        from datetime import datetime
        try:
            dt1 = datetime.strptime(d1, '%Y-%m-%d')
            dt2 = datetime.strptime(d2, '%Y-%m-%d')
            return (dt2 - dt1).days
        except Exception:
            return 365


def parse_args():
    parser = argparse.ArgumentParser(description='多指标评分量化系统回测引擎')
    parser.add_argument('--slots', type=int, default=None, help='仓位数')
    parser.add_argument('--start', type=str, default=None, help='开始日期')
    parser.add_argument('--end', type=str, default=None, help='结束日期')
    parser.add_argument('--mode', choices=['star', 'weighted'], default=None)
    parser.add_argument('--min-stars', type=int, default=None)
    parser.add_argument('--max-per-trade', type=float, default=0,
                        help='max amount per trade (0=unlimited)')
    parser.add_argument('--slippage', type=float, default=0.002,
                        help='one-side slippage (default 0.002)')
    return parser.parse_args()


def main():
    args = parse_args()
    engine = BacktestScoringEngine(CONFIG, args)
    engine.run()


if __name__ == '__main__':
    main()
