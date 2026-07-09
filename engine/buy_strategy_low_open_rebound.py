#!/usr/bin/env python3
"""
策略4: 低开反弹策略
- 开盘跌幅0~-3%
- hour1相对open上涨>1%
- 换手率>5%
- 次日卖出
"""
from engine.buy_module import BuyModule
from typing import List, Optional
import pandas as pd


class LowOpenReboundBuyStrategy(BuyModule):
    """低开反弹策略"""

    def __init__(self, buy_hour: int = 2):
        self.buy_hour = buy_hour

    def get_candidates(self, date: str, day_data: pd.DataFrame,
                       prev_data: pd.DataFrame, blacklist) -> List[dict]:
        if day_data is None or day_data.empty:
            return []
        
        df = day_data.copy()
        
        mask = (
            (df['open_rate'].notna()) &
            (df['open_rate'] >= -3.0) & (df['open_rate'] < 0) &  # 低开0~-3%
            (df['turn'].notna()) &
            (df['turn'] >= 5.0)
        )
        df = df[mask]
        
        if df.empty:
            return []
        
        if blacklist:
            st_mask = df['code_name'].str.upper().str.contains('ST', na=False)
            if 'isST' in df.columns:
                st_mask = st_mask | (df['isST'].fillna(0).astype(int) == 1)
            bj_mask = df['code'].str.lower().str.startswith('bj.', na=False)
            df = df[~st_mask & ~bj_mask]
        
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
            
            # 检查hour1是否相对open上涨>1%
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
