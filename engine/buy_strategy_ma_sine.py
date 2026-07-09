#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
MA正弦策略 - 买入模块
======================
对接回测引擎的买入策略模块。
核心：MA3连续下跌3天后企稳，按斜率回升排序选最优。

继承 engine.buy_strategy_base.BuyStrategyBase
"""

from __future__ import annotations
import sqlite3
from collections import defaultdict
from typing import Dict, List, Tuple, Optional

# 配置参数（可被回测引擎覆盖）
N_MA = 3
DECLINE_DAYS = 3
DECLINE_THRESHOLD = -0.05
STABILIZE_CR_MIN = -2.0
STABILIZE_CR_MAX = 2.0
MA_DIST_MIN = -5.0
MA_DIST_MAX = 5.0
SLOPE_IMPROVE_MIN = 0.0
MCAP_MIN = 30
MCAP_MAX = 500
TURN_MIN = 1.0
TURN_MAX = 30.0
DB_PATH = "/home/AIWealth/data/stocks.db"

# 排除规则
EXCLUDE_GEM = True
EXCLUDE_STAR = True
EXCLUDE_BJ = True


def get_conn():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def is_excluded(code: str, code_name: str, isST: int) -> bool:
    if not code:
        return True
    if EXCLUDE_BJ and code.startswith("bj."):
        return True
    if EXCLUDE_GEM and code.startswith("sz.30"):
        return True
    if EXCLUDE_STAR and code.startswith("sh.68"):
        return True
    if isST == 1:
        return True
    if code_name and ("ST" in str(code_name).upper() or "退" in str(code_name)):
        return True
    return False


def compute_ma(closes: List[float], n: int) -> List[Optional[float]]:
    """计算N日均线"""
    ma = [None] * len(closes)
    for i in range(n - 1, len(closes)):
        window = closes[i - n + 1 : i + 1]
        if all(v is not None for v in window):
            ma[i] = sum(window) / n
    return ma


class MASineBuyStrategy:
    """
    MA正弦策略买入模块

    用法:
        strategy = MASineBuyStrategy()
        strategy.prepare(all_dates, daily_data)  # 预计算MA
        candidates = strategy.get_candidates(date)  # 获取当日候选股
    """

    def __init__(self):
        self.n_ma = N_MA
        self.decline_days = DECLINE_DAYS
        self.decline_threshold = DECLINE_THRESHOLD
        self.stabilize_cr_min = STABILIZE_CR_MIN
        self.stabilize_cr_max = STABILIZE_CR_MAX
        self.ma_dist_min = MA_DIST_MIN
        self.ma_dist_max = MA_DIST_MAX
        self.slope_improve_min = SLOPE_IMPROVE_MIN
        self.mcap_min = MCAP_MIN
        self.mcap_max = MCAP_MAX
        self.turn_min = TURN_MIN
        self.turn_max = TURN_MAX

        # 预计算缓存
        self._stock_closes: Dict[str, tuple] = {}
        self._stock_dates: Dict[str, tuple] = {}
        self._stock_date_idx: Dict[str, Dict[str, int]] = {}
        self._stock_ma: Dict[str, tuple] = {}
        self._stock_slopes: Dict[str, tuple] = {}
        self._all_dates: List[str] = []
        self._daily_data: Dict[str, Dict] = {}
        self._prepared = False

    def prepare(self, all_dates: List[str], daily_data: Dict[str, Dict]) -> None:
        """
        预计算所有股票的MA(N)和斜率。
        应在回测开始前调用一次。

        Args:
            all_dates: 所有交易日列表
            daily_data: date -> {code -> row_dict}（直接引用不复制）
        """
        self._all_dates = all_dates
        self._daily_data = daily_data

        # 构建每只股票的close序列（用array减少内存）
        stock_closes = defaultdict(list)
        stock_dates = defaultdict(list)

        for d in all_dates:
            rows = daily_data.get(d, {})
            for code, row in rows.items():
                if is_excluded(code, row.get("code_name", ""), row.get("isST", 0)):
                    continue
                close = row.get("close")
                if close is None:
                    continue
                stock_closes[code].append(close)
                stock_dates[code].append(d)

        # 转成tuple节省内存，同时建立date->idx映射
        stock_date_idx = {}
        for code, dates in stock_dates.items():
            stock_closes[code] = tuple(stock_closes[code])
            stock_dates[code] = tuple(dates)
            stock_date_idx[code] = {d: i for i, d in enumerate(dates)}
        self._stock_date_idx = stock_date_idx

        # 计算MA和斜率
        for code in list(stock_closes.keys()):
            closes = stock_closes[code]
            n = len(closes)
            ma = compute_ma(closes, self.n_ma)
            slopes = [None] * n
            for i in range(1, n):
                if ma[i] is not None and ma[i - 1] is not None and ma[i - 1] != 0:
                    slopes[i] = (ma[i] - ma[i - 1]) / ma[i - 1] * 100

            self._stock_closes[code] = closes
            self._stock_dates[code] = stock_dates[code]
            self._stock_ma[code] = tuple(ma)
            self._stock_slopes[code] = tuple(slopes)

        self._prepared = True

    def _get_stock_idx(self, code: str, date: str) -> Optional[int]:
        """获取某股票在某日期的数据索引(O(1))"""
        return self._stock_date_idx.get(code, {}).get(date)

    def get_candidates(self, date: str) -> List[dict]:
        """
        获取当日候选股列表，按质量评分降序排列。

        Returns:
            [{"code": ..., "name": ..., "close": ..., "score": ..., ...}, ...]
        """
        if not self._prepared:
            return []

        candidates = []
        today_rows = self._daily_data.get(date, {})

        for code, row in today_rows.items():
            if is_excluded(code, row.get("code_name", ""), row.get("isST", 0)):
                continue

            idx = self._get_stock_idx(code, date)
            if idx is None:
                continue

            n_ma = self.n_ma
            dd = self.decline_days
            if idx < n_ma + dd + 1:
                continue

            closes = self._stock_closes[code]
            ma = self._stock_ma[code]
            slopes = self._stock_slopes[code]

            # 条件1: MA前dd天连续下跌
            declining = True
            for offset in range(1, dd + 1):
                s = slopes[idx - offset]
                if s is None or s >= self.decline_threshold:
                    declining = False
                    break
            if not declining:
                continue

            # 条件2: 今日企稳
            cr = row.get("close_rate")
            if cr is None:
                continue
            if not (self.stabilize_cr_min <= cr <= self.stabilize_cr_max):
                continue

            # 条件3: close在MA附近
            if ma[idx] is None:
                continue
            cvs_ma = (closes[idx] - ma[idx]) / ma[idx] * 100
            if not (self.ma_dist_min <= cvs_ma <= self.ma_dist_max):
                continue

            # 条件4: 斜率回升
            if slopes[idx] is None or slopes[idx - 1] is None:
                continue
            slope_imp = slopes[idx] - slopes[idx - 1]
            if slope_imp < self.slope_improve_min:
                continue

            # 条件5: 市值过滤
            amt = row.get("amount")
            turn = row.get("turn")
            mcap = (amt * 100 / turn / 1e8) if (amt and turn and amt > 0 and turn > 0) else 0
            if mcap and (mcap < self.mcap_min or mcap > self.mcap_max):
                continue

            # 条件6: 换手率过滤
            if turn is None:
                continue
            if not (self.turn_min <= turn <= self.turn_max):
                continue

            # 质量评分: 斜率回升大 + close低于MA(超卖) + 换手适中
            score = slope_imp * 10 - cvs_ma - abs(turn - 5) * 0.1

            candidates.append({
                "code": code,
                "name": row.get("code_name", ""),
                "close": closes[idx],
                "close_rate": cr,
                "close_vs_ma": cvs_ma,
                "ma_slope": slopes[idx],
                "slope_improve": slope_imp,
                "turn": turn,
                "amount": amt,
                "mcap": mcap,
                "score": score,
                "open_rate": row.get("open_rate"),
                "high_rate": row.get("high_rate"),
                "low_rate": row.get("low_rate"),
                "hour1_close_rate": row.get("hour1_close_rate"),
                "hour1_open_rate": row.get("hour1_open_rate"),
            })

        candidates.sort(key=lambda x: x["score"], reverse=True)
        return candidates

    def get_top_candidate(self, date: str) -> Optional[dict]:
        """获取当日最优候选股"""
        cands = self.get_candidates(date)
        return cands[0] if cands else None

    def get_ma_info(self, code: str, date: str) -> Optional[dict]:
        """获取某股票在某日的MA信息"""
        idx = self._get_stock_idx(code, date)
        if idx is None:
            return None

        closes = self._stock_closes.get(code, [])
        ma = self._stock_ma.get(code, [])
        slopes = self._stock_slopes.get(code, [])

        if idx >= len(closes):
            return None

        return {
            "close": closes[idx],
            "ma": ma[idx] if idx < len(ma) else None,
            "slope": slopes[idx] if idx < len(slopes) else None,
        }
