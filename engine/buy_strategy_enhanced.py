#!/usr/bin/env python3
"""
增强版回测脚本 - 添加严格过滤条件
关键优化：
1. ST股过滤（isST字段 + 一字板5%涨幅识别）
2. 北交所过滤（bj.开头）
3. 成交额过滤（2-50亿，估算市值30-700亿）
4. 一字板过滤（涨停无法买入）
5. 极端换手率过滤
"""
import sys
sys.path.insert(0, '/home/AIWealth')

import pandas as pd
from engine.buy_module import BuyModule
from typing import List, Optional


def apply_strict_filters(df: pd.DataFrame) -> pd.DataFrame:
    """应用严格过滤条件"""
    if df.empty:
        return df

    # 1. ST股过滤
    st_mask = df['code_name'].str.upper().str.contains('ST', na=False)
    if 'isST' in df.columns:
        st_mask = st_mask | (df['isST'].fillna(0).astype(int) == 1)
    df = df[~st_mask]

    if df.empty:
        return df

    # 2. 北交所过滤
    bj_mask = df['code'].str.lower().str.startswith('bj.', na=False)
    df = df[~bj_mask]

    if df.empty:
        return df

    # 3. 一字板过滤（涨停无法买入）
    # 识别条件：O=H=L=C 且 close_rate >= 9.5%
    if 'open' in df.columns and 'close' in df.columns:
        one_word_mask = (
            (df['open'] == df['close']) &
            (df['open'] == df['high']) &
            (df['open'] == df['low']) &
            (df['close_rate'] >= 9.5)
        )
        df = df[~one_word_mask]

    if df.empty:
        return df

    # 4. 成交额过滤（2-50亿）
    if 'amount' in df.columns:
        df = df[(df['amount'] >= 2e8) & (df['amount'] <= 50e8)]

    if df.empty:
        return df

    # 5. 换手率过滤（排除<1%和>30%）
    if 'turn' in df.columns:
        df = df[(df['turn'] >= 1.0) & (df['turn'] <= 30.0)]

    return df


# 现在为每个策略创建增强版
class EnhancedLowTurnoverStableStrategy(BuyModule):
    """增强版低换手率稳健策略"""

    def __init__(self, buy_hour: int = 4):
        self.buy_hour = buy_hour

    def get_candidates(self, date: str, day_data: pd.DataFrame,
                       prev_data: pd.DataFrame, blacklist) -> List[dict]:
        if day_data is None or day_data.empty:
            return []

        df = day_data.copy()

        # 基础条件
        mask = (
            (df['turn'].notna()) &
            (df['turn'] >= 2.0) & (df['turn'] <= 5.0) &
            (df['open_rate'].notna()) &
            (df['open_rate'] >= 0) & (df['open_rate'] <= 3.0) &
            (df['close_rate'].notna()) &
            (df['close_rate'] > 0)
        )
        df = df[mask]

        if df.empty:
            return []

        # 应用严格过滤
        df = apply_strict_filters(df)

        if df.empty:
            return []

        df = df.sort_values('turn', ascending=True)

        candidates = []
        for _, row in df.iterrows():
            candidates.append({
                'code': row['code'],
                'code_name': row['code_name'] if pd.notna(row['code_name']) else '',
                'turn': float(row['turn']),
                'open_rate': float(row['open_rate']),
                'close_rate': float(row['close_rate']),
            })

        return candidates

    def should_buy(self, candidates: List[dict], date: str, hour: int,
                   hour_data: dict, portfolio, day_data: pd.DataFrame = None) -> Optional[dict]:
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

            h_open = stock_hour.get('open', 0)
            h_high = stock_hour.get('high', 0)
            h_low = stock_hour.get('low', 0)
            if h_open == h_close == h_high == h_low:
                continue

            return {
                'code': code,
                'code_name': cand['code_name'],
                'price': h_close,
                'turn': cand['turn'],
                'open_rate': cand['open_rate'],
            }

        return None


class EnhancedLowOpenReboundStrategy(BuyModule):
    """增强版低开反弹策略"""

    def __init__(self, buy_hour: int = 2):
        self.buy_hour = buy_hour

    def get_candidates(self, date: str, day_data: pd.DataFrame,
                       prev_data: pd.DataFrame, blacklist) -> List[dict]:
        if day_data is None or day_data.empty:
            return []

        df = day_data.copy()

        mask = (
            (df['open_rate'].notna()) &
            (df['open_rate'] >= -3.0) & (df['open_rate'] < 0) &
            (df['turn'].notna()) &
            (df['turn'] >= 5.0)
        )
        df = df[mask]

        if df.empty:
            return []

        df = apply_strict_filters(df)

        if df.empty:
            return []

        df = df.sort_values('open_rate', ascending=False)

        candidates = []
        for _, row in df.iterrows():
            candidates.append({
                'code': row['code'],
                'code_name': row['code_name'] if pd.notna(row['code_name']) else '',
                'open_rate': float(row['open_rate']),
                'open': float(row['open']),
                'turn': float(row['turn']),
            })

        return candidates

    def should_buy(self, candidates: List[dict], date: str, hour: int,
                   hour_data: dict, portfolio, day_data: pd.DataFrame = None) -> Optional[dict]:
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

            h_open = cand['open']
            if h_open <= 0:
                continue

            rebound_rate = (h_close - h_open) / h_open * 100
            if rebound_rate < 1.0:
                continue

            h_high = stock_hour.get('high', 0)
            h_low = stock_hour.get('low', 0)
            if h_open == h_close == h_high == h_low:
                continue

            return {
                'code': code,
                'code_name': cand['code_name'],
                'price': h_close,
                'open_rate': cand['open_rate'],
                'rebound_rate': rebound_rate,
            }

        return None


class EnhancedShrinkVolumeStrategy(BuyModule):
    """增强版缩量上涨策略"""

    def __init__(self, buy_hour: int = 4):
        self.buy_hour = buy_hour

    def get_candidates(self, date: str, day_data: pd.DataFrame,
                       prev_data: pd.DataFrame, blacklist) -> List[dict]:
        if day_data is None or day_data.empty:
            return []

        df = day_data.copy()

        mask = (
            (df['close_rate'].notna()) &
            (df['close_rate'] >= 3.0) & (df['close_rate'] <= 8.0) &
            (df['open_rate'].notna()) &
            (df['open_rate'] >= 0) & (df['open_rate'] <= 3.0) &
            (df['turn'].notna()) &
            (df['turn'] >= 2.0) & (df['turn'] <= 8.0)
        )
        df = df[mask]

        if df.empty:
            return []

        # 缩量条件
        if prev_data is not None and not prev_data.empty:
            prev_lookup = prev_data.set_index('code')['turn'].to_dict()
            df['prev_turn'] = df['code'].map(prev_lookup)
            df['vol_ratio'] = df['turn'] / df['prev_turn'].replace(0, 1)
            df = df[df['vol_ratio'] <= 1.5]
        else:
            df = df[df['turn'] <= 5.0]

        if df.empty:
            return []

        df = apply_strict_filters(df)

        if df.empty:
            return []

        df = df.sort_values('close_rate', ascending=True)

        candidates = []
        for _, row in df.iterrows():
            candidates.append({
                'code': row['code'],
                'code_name': row['code_name'] if pd.notna(row['code_name']) else '',
                'close_rate': float(row['close_rate']),
                'open_rate': float(row['open_rate']),
                'turn': float(row['turn']),
            })

        return candidates

    def should_buy(self, candidates: List[dict], date: str, hour: int,
                   hour_data: dict, portfolio, day_data: pd.DataFrame = None) -> Optional[dict]:
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
