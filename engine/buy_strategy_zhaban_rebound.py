#!/usr/bin/env python3
"""
策略2: 炸板股反弹策略 (高收益信号)
- 盘中触及涨停(high_rate >= 9.8%)
- 收盘未封住(close_rate < 9.5%)
- 换手率 > 5%
- 持有3天后卖出
预期: 3天收益+10.24%, 胜率55.6%
"""
import sys
sys.path.insert(0, '/home/AIWealth')

from engine.buy_module import BuyModule
from typing import List, Optional
import pandas as pd


class ZhaBanReboundBuyStrategy(BuyModule):
    """炸板股反弹买入策略"""

    def __init__(self, buy_hour: int = 4):
        self.buy_hour = buy_hour

    def get_candidates(self, date: str, day_data: pd.DataFrame,
                       prev_data: pd.DataFrame, blacklist) -> List[dict]:
        """筛选炸板股"""
        if day_data is None or day_data.empty:
            return []

        df = day_data.copy()

        # 炸板条件
        mask = (
            (df['high_rate'].notna()) &
            (df['high_rate'] >= 9.8) &  # 盘中触及涨停
            (df['close_rate'].notna()) &
            (df['close_rate'] < 9.5) &  # 未封住涨停
            (df['turn'].notna()) &
            (df['turn'] >= 5.0)  # 换手率>5%
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

        # 按换手率排序（优先选择换手率适中的）
        df = df.sort_values('turn', ascending=True)

        candidates = []
        for _, row in df.iterrows():
            candidates.append({
                'code': row['code'],
                'code_name': row['code_name'] if pd.notna(row['code_name']) else '',
                'high_rate': float(row['high_rate']),
                'close_rate': float(row['close_rate']),
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
                'high_rate': cand['high_rate'],
                'close_rate': cand['close_rate'],
                'turn': cand['turn'],
            }

        return None
