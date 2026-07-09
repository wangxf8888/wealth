#!/usr/bin/env python3
"""
策略1: 缩量上涨策略 (基于数据分析最强信号)
- 当日涨幅3-8%
- 换手率 < 5日均值 × 1.5 (缩量)
- 当日换手率2-8%
- 开盘幅度0-3%
- 次日hour4卖出
预期: 胜率64.3%, 收益+0.68%/次
"""
import sys
sys.path.insert(0, '/home/AIWealth')

from engine.buy_module import BuyModule
from typing import List, Optional
import pandas as pd


class ShrinkVolumeBuyStrategy(BuyModule):
    """缩量上涨买入策略"""

    def __init__(self, buy_hour: int = 4):
        self.buy_hour = buy_hour

    def get_candidates(self, date: str, day_data: pd.DataFrame,
                       prev_data: pd.DataFrame, blacklist) -> List[dict]:
        """筛选候选股"""
        if day_data is None or day_data.empty:
            return []

        df = day_data.copy()

        # 基础过滤
        mask = (
            (df['close_rate'].notna()) &
            (df['close_rate'] >= 3.0) & (df['close_rate'] <= 8.0) &  # 涨幅3-8%
            (df['open_rate'].notna()) &
            (df['open_rate'] >= 0) & (df['open_rate'] <= 3.0) &  # 开盘0-3%
            (df['turn'].notna()) &
            (df['turn'] >= 2.0) & (df['turn'] <= 8.0)  # 换手率2-8%
        )
        df = df[mask]

        if df.empty:
            return []

        # 黑名单过滤
        if blacklist:
            st_mask = df['code_name'].str.upper().str.contains('ST', na=False)
            if 'isST' in df.columns:
                st_mask = st_mask | (df['isST'].fillna(0).astype(int) == 1)
            bj_mask = df['code'].str.lower().str.startswith('bj.', na=False)
            df = df[~st_mask & ~bj_mask]

        if df.empty:
            return []

        # 计算5日平均换手率（需要prev_data）
        if prev_data is not None and not prev_data.empty:
            prev_lookup = prev_data.set_index('code')['turn'].to_dict()
            df['prev_turn'] = df['code'].map(prev_lookup)
            # 简化：只用前一日换手率作为参考
            df['vol_ratio'] = df['turn'] / df['prev_turn'].replace(0, 1)
            df = df[df['vol_ratio'] <= 1.5]  # 缩量条件
        else:
            # 没有prev_data时，只用绝对换手率过滤
            df = df[df['turn'] <= 5.0]

        if df.empty:
            return []

        # 按涨幅排序（优先选择涨幅适中的）
        df = df.sort_values('close_rate', ascending=True)

        candidates = []
        for _, row in df.iterrows():
            candidates.append({
                'code': row['code'],
                'code_name': row['code_name'] if pd.notna(row['code_name']) else '',
                'close_rate': float(row['close_rate']),
                'open_rate': float(row['open_rate']),
                'turn': float(row['turn']),
                'amount': float(row.get('amount', 0)),
            })

        return candidates

    def should_buy(self, candidates: List[dict], date: str, hour: int,
                   hour_data: dict, portfolio, day_data: pd.DataFrame = None) -> Optional[dict]:
        """买入决策"""
        if hour != self.buy_hour:
            return None

        if not candidates:
            return None

        held_codes = portfolio.held_codes()

        for cand in candidates:
            code = cand['code']

            if code in held_codes:
                continue

            stock_hour = hour_data.get(code)
            if stock_hour is None:
                continue

            h_close = stock_hour.get('close', 0)
            if not h_close or h_close <= 0:
                continue

            # 一字板检测
            h_open = stock_hour.get('open', 0)
            h_high = stock_hour.get('high', 0)
            h_low = stock_hour.get('low', 0)
            if h_open == h_close == h_high == h_low:
                continue

            return {
                'code': code,
                'code_name': cand['code_name'],
                'price': h_close,
                'close_rate': cand['close_rate'],
                'open_rate': cand['open_rate'],
                'turn': cand['turn'],
            }

        return None
