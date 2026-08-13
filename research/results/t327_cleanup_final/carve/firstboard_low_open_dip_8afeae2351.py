"""首板次日低开低吸策略 (FB-A) - Task#9 引擎精测。

形态(来自 Task#7 全维度网格复审, 研究口径见 data/realtime/task7_refine.txt):
  昨日(D0)首板涨停(连板数==1, 非一字) → 今日(D1)竞价低开 -4% <= open_rate < -2%
  → 昨日红盘占比 red_ratio >= 60(强势日错杀) → H1_open 买入
  → 退出 tp+6% / sl-10%, 最长5个监控日(D6收盘兜底)
排序: D0换手升序(低换手=筹码未松动优先) — task7_top1_probe:
  top1 +1.50%/日 2021-2026 六年全正; 注意换手降序方向则失效(-0.31%)。

研究口径(池子级 tp6sl-10, 毛收益): n=1574 胜率58.4% 均值+0.73% 5正1平
引擎精测(2021-01~2026-07-15, slot=1, 含成本): CAGR +72.53% | MDD 44.81%
  | 219笔 胜率65.75% +1.62%/笔 | 唯一负年 2023 -7.9% → 观察池第一顺位
Task#12 干净数据+全区间重跑(至2026-07-23): CAGR +64.85%, 2026 +16.9→-7.8
  (数据修复+区间延长), 2021-2025 分年不变; 卖点前移两变体(d2c/h1)引擎
  精测均劣于本对照(+56.8%/+61.3%), FB-A **保持现行 tpsl_d6 口径不动**——
  粗测代理曾示 d2c 更优(+86%), 引擎证伪: 变体笔数增加(226→332/447)但
  均收益稀释(+1.47→+0.92/+0.74%/笔), slot=1 路径依赖下 2021/2024/2026
  转弱, MDD 还升(44.8→45.0/55.7)。只信引擎数据。
red_ratio 权威源=index_kline(sh.000001) red_ratio 列, 2026-07-23 前已补齐
(Task#10 Robin 修复); 缺失日显式跳过不买入作为防御, 不引入未来数据。

Task#12 卖点前移(exit_mode 参数化, 买入逻辑不动):
  tpsl_d6   现行对照: tp6/sl-10 挂单监控 D2..D6, D6收盘兜底
  tpsl_h1   挂单仅监控 D2 H1, 未触发 H1收盘强平 (D2起slot当小时可复用)
  d2h1_open / d2h1_close / trail_h1  同 B2-A 语义
依据: Lee时段结构(H1唯一系统性正时段) x Task#9归因(hold长占slot致信号捕获减半)。
"""
import sqlite3

import numpy as np

from strategies.base import Strategy, Signal, SellSignal

_DB = '/home/AIWealth/data/stocks.db'


def _load_red_map() -> dict:
    """{date: red_ratio(float)}; 缺失日不在map中, 调用方显式跳过。"""
    out = {}
    try:
        conn = sqlite3.connect(_DB)
        for d, r in conn.execute(
                "SELECT date, red_ratio FROM index_kline "
                "WHERE code='sh.000001'"):
            if r is not None:
                out[d] = float(r)
        conn.close()
    except sqlite3.Error:
        pass
    return out


class FirstboardLowOpenDipStrategy(Strategy):
    """FB-A: 昨日首板 → 低开-4~-2 → 红盘>=60 → D0低换手优先 → H1低吸。"""

    name = "firstboard_low_open_dip"
    max_hold_hours = 20          # 监控D2..D6, D6收盘兜底(同研究口径5日窗口)
    sell_day_no_buy = True
    buy_hour = 1

    # === 核心参数(Task#7 网格最优) ===
    open_rate_min = -4.0         # 竞价低开下限
    open_rate_max = -2.0         # 竞价低开上限(不含)
    red_ratio_min = 60.0         # 昨日红盘占比下限(D0收盘后已知)
    min_history_bars = 20        # 上市未满20根K线不买(对齐研究口径 nth>=20)

    # 卖出参数显式声明(fixed模式)
    take_profit_pct = 0.06       # 止盈 +6%
    stop_loss_pct = -0.10        # 止损 -10%

    # === Task#50 挖潜维度: D0封板时间过滤(默认None=不过滤, 零侵入) ===
    # 'early'=H1/H2首次触板(早板筹码强), 'late'=H3/H4触板; 无hour数据按不过关处理
    seal_filter = None

    # === Task#12 卖点前移 ===
    # tpsl_d6(现行对照) | tpsl_d2c(到期提前到D2收盘) | tpsl_h1 | d2h1_open
    # | d2h1_close | trail_h1
    exit_mode = 'tpsl_d6'
    trail_pp = 2.0               # trail_h1 回撤触发(pp), 基于D2 H1_open
    _H1_MODES = ('tpsl_h1', 'd2h1_open', 'd2h1_close', 'trail_h1')

    def __init__(self):
        self._cand_codes = []
        self._cand_date = None
        self._red_map = None

    def _red_ratio(self, date: str):
        if self._red_map is None:
            self._red_map = _load_red_map()
        return self._red_map.get(date)

    def get_candidates(self, date, data_feed):
        self._cand_codes, self._cand_date = [], date

        d0 = data_feed._prev_trading_date(date)   # 昨日(首板日)
        if not d0:
            return []
        dm1 = data_feed._prev_trading_date(d0)    # 前日(须未涨停)
        if not dm1:
            return []

        # 情绪过滤: 昨日红盘占比 >= 60; 缺失日显式跳过(不引入未来数据)
        red = self._red_ratio(d0)
        if red is None or red < self.red_ratio_min:
            return []

        f_today = data_feed._load_day(date)
        f_d0 = data_feed._load_day(d0)
        f_dm1 = data_feed._load_day(dm1)
        if any(f is None or f.empty for f in (f_today, f_d0, f_dm1)):
            return []

        codes = f_today.index
        is20 = codes.str.startswith('sz.30') | codes.str.startswith('sh.688')
        ratio = np.where(is20, 0.20, 0.10)

        def limit_flags(frame):
            sub = frame.reindex(codes)
            pre = sub['preclose'].to_numpy(dtype=float, na_value=0.0)
            clo = sub['close'].to_numpy(dtype=float, na_value=0.0)
            opn = sub['open'].to_numpy(dtype=float, na_value=0.0)
            lp = np.round(pre * (1 + ratio), 2)
            valid = (pre > 0) & (clo > 0)
            is_lim = valid & (clo >= lp - 0.001)
            is_yizi = is_lim & (opn > 0) & (opn >= lp - 0.001)
            return valid, is_lim, is_yizi

        v0, lim0, yizi0 = limit_flags(f_d0)
        v1, lim1, _ = limit_flags(f_dm1)

        # 昨日首板(非一字) + 前日未涨停(=恰好第1板)
        pattern = v0 & v1 & lim0 & ~yizi0 & ~lim1

        # 今日竞价窗口 + 基础池过滤
        orate = f_today['open_rate'].to_numpy(dtype=float, na_value=np.nan)
        opn = f_today['open'].to_numpy(dtype=float, na_value=0.0)
        win = (orate >= self.open_rate_min) & (orate < self.open_rate_max)
        not_bj = ~codes.str.startswith('bj.')
        st = f_today['isST'].fillna(0).astype(int).to_numpy() > 0
        if 'code_name' in f_today.columns:
            st = st | f_today['code_name'].fillna('').astype(str) \
                .str.upper().str.contains('ST').to_numpy()

        mask = pattern & win & (opn > 0) & not_bj & ~st
        if not mask.any():
            return []

        # 排序: D0换手升序(低换手优先, 买入时刻可得)
        d0_turn = f_d0.reindex(codes)['turn'] \
            .to_numpy(dtype=float, na_value=np.inf)
        idx = np.where(mask)[0]
        idx = idx[np.argsort(d0_turn[idx], kind='stable')]

        final = []
        for i in idx:
            code = codes[i]
            hist = data_feed.get_stock_history(code, date,
                                               self.min_history_bars)
            if len(hist) < self.min_history_bars:
                continue
            final.append(code)

        self._cand_codes = final
        return final

    def should_buy(self, code, date, hour, data_feed, portfolio):
        if hour != self.buy_hour:
            return None
        if date != self._cand_date or code not in self._cand_codes:
            return None
        price = data_feed.get_hour_open(code, date, hour)
        if not price or price <= 0:
            return None
        limit_up, _ = data_feed.get_limit_prices(code, date)
        if limit_up > 0 and price >= limit_up - 0.001:
            return None
        return Signal(code=code, price=float(price), strategy_name=self.name,
                      target_hold_hours=self.max_hold_hours)

    def should_sell(self, position, date, hour, data_feed):
        # T+1: 买入当日不卖(框架另有兜底)
        if position.buy_date >= date:
            return None
        if self.exit_mode in self._H1_MODES:
            return self._sell_forward(position, date, hour, data_feed)
        buy_price = position.buy_price
        if buy_price <= 0:
            return None

        tp_target = buy_price * (1 + self.take_profit_pct)
        sl_target = buy_price * (1 + self.stop_loss_pct)

        bar_open = data_feed.get_hour_open(position.code, date, hour)
        bar_high = data_feed.get_hou#!/usr/bin/env python3
"""
对比两个引擎传给策略的数据是否一致
"""

from services.backtest_engine import BacktestEngine
from services.unified_backtest_v2 import UnifiedBacktestV2
from services.strategies.turnover_shrink import TurnoverShrinkStrategy

# 保存原始引擎的数据
original_data = {}

class PatchedStrategy1(TurnoverShrinkStrategy):
    def select_candidates(self, date, pool_data, k_data_map, history_map):
        if date == "2024-04-09":
            original_data['date'] = date
            original_data['pool_count'] = len(pool_data)
            original_data['kline_count'] = len(k_data_map)
            original_data['history_count'] = len(history_map)
            original_data['pool_codes'] = sorted([p['code'] for p in pool_data])[:5]  # 前5个
            print(f"\n【原始引擎】date={date}, pool={len(pool_data)}, kline={len(k_data_map)}, history={len(history_map)}")
            print(f"  前5个pool: {original_data['pool_codes']}")

        return super().select_candidates(date, pool_data, k_data_map, history_map)

# 保存统一引擎的数据
unified_data = {}

class PatchedStrategy2(TurnoverShrinkStrategy):
    def select_candidates(self, date, pool_data, k_data_map, history_map):
        if date == "2024-04-09":
            unified_data['date'] = date
            unified_data['pool_count'] = len(pool_data)
            unified_data['kline_count'] = len(k_data_map)
            unified_data['history_count'] = len(history_map)
            unified_data['pool_codes'] = sorted([p['code'] for p in pool_data])[:5]
            print(f"\n【统一引擎】date={date}, pool={len(pool_data)}, kline={len(k_data_map)}, history={len(history_map)}")
            print(f"  前5个pool: {unified_data['pool_codes']}")

        return super().select_candidates(date, pool_data, k_data_map, history_map)

print("="*100)
print("测试04-09传给策略的数据")
print("="*100)

print("\n【原始引擎】")
strategy1 = PatchedStrategy1(use_core_pool=True)
engine1 = BacktestEngine(strategy=strategy1, start_date="2024-04-09", end_date="2024-04-10", initial_capital=1000000.0)
engine1.run()

print("\n【统一引擎】")
strategy2 = PatchedStrategy2(use_core_pool=True)
engine2 = UnifiedBacktestV2(strategy=strategy2, start_date="2024-04-09", end_date="2024-04-10", initial_capital=1000000.0, mode='fast')
engine2.run()

print("\n" + "="*100)
print("数据对比")
print("="*100)
print(f"pool数量: 原始={original_data.get('pool_count')}, 统一={unified_data.get('pool_count')}")
print(f"kline数量: 原始={original_data.get('kline_count')}, 统一={unified_data.get('kline_count')}")
print(f"history数量: 原始={original_data.get('history_count')}, 统一={unified_data.get('history_count')}")
print(f"pool前5个: 原始={original_data.get('pool_codes')}, 统一={unified_data.get('pool_codes')}")

if original_data == unified_data:
    print("\n✅ 数据完全一致！")
else:
    print("\n❌ 数据不一致！")
