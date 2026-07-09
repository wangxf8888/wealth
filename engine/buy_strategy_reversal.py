# ============================================================
# ⚠️  已废弃 (DEPRECATED) — 全参数网格搜索均亏损，策略无效
#     废弃原因：全参数空间回测结果均为负收益
#     保留本文件仅供参考，请勿在生产中使用
# ============================================================

"""弱转强反包买入策略 (Reversal)

逻辑：
- 昨日大跌（close_rate <= drop_threshold，如-3%）
- 今日收阳（close_rate >= reversal_threshold，如0%）
- 在hour4确认今日收阳后以收盘价买入
- 配合min_hold_days=2的卖出策略（T+2卖出）
"""
from typing import List, Optional
import pandas as pd
from .buy_module import BuyModule


class ReversalBuyStrategy(BuyModule):
    """弱转强反包策略 - 昨日大跌今日收阳，hour4确认后买入"""

    def __init__(self, drop_threshold: float = -3.0,
                 reversal_threshold: float = 0.0,
                 buy_hour: int = 4):
        self.drop_threshold = drop_threshold      # 昨日跌幅阈值（如-3%）
        self.reversal_threshold = reversal_threshold  # 今日最低收阳阈值（如0%）
        self.buy_hour = buy_hour                  # hour4确认后买入

    def get_candidates(self, date: str, day_data: pd.DataFrame,
                       prev_data: pd.DataFrame, blacklist) -> List[dict]:
        """
        从昨日数据中筛选大跌股票（候选预筛）：
        - 昨日 close_rate <= drop_threshold（大跌）
        - 排除isST=1
        - 排除北交所
        
        注意：今日是否收阳需要在should_buy中通过day_data确认
        """
        if prev_data is None or prev_data.empty:
            return []

        df = prev_data.copy()

        # 必要字段检查
        if 'close_rate' not in df.columns:
            return []

        # 确保数值类型
        df['close_rate'] = pd.to_numeric(df['close_rate'], errors='coerce')
        df = df.dropna(subset=['close_rate'])

        # ST过滤
        if 'isST' in df.columns:
            df = df[df['isST'].fillna(0).astype(int) != 1]
        st_mask = df['code_name'].str.upper().str.contains('ST', na=False)
        df = df[~st_mask]

        # 北交所过滤
        bj_mask = df['code'].str.lower().str.startswith('bj.', na=False)
        df = df[~bj_mask]

        if df.empty:
            return []

        # 核心条件：昨日大跌
        df = df[df['close_rate'] <= self.drop_threshold]
        if df.empty:
            return []

        # 构建候选列表
        candidates = []
        for _, row in df.iterrows():
            candidates.append({
                'code': row['code'],
                'code_name': row.get('code_name', ''),
                'prev_close_rate': float(row['close_rate']),
            })

        return candidates

    def should_buy(self, candidates: List[dict], date: str, hour: int,
                   hour_data: dict, portfolio, day_data: pd.DataFrame = None) -> Optional[dict]:
        """
        在hour4确认今日收阳后买入：
        - 只在buy_hour（hour4）执行
        - 从day_data确认今日close_rate >= reversal_threshold
        - 一字板检测
        - 排除已持仓股
        """
        if hour != self.buy_hour:
            return None
        if not candidates:
            return None
        if day_data is None or day_data.empty:
            return None

        held_codes = portfolio.held_codes()

        # 预处理day_data快速查找
        day_lookup = day_data.set_index('code').to_dict('index')

        for cand in candidates:
            code = cand['code']

            if code in held_codes:
                continue

            # 从今日day_data确认close_rate（反包确认）
            day_row = day_lookup.get(code)
            if day_row is None:
                continue

            # isST检测
            is_st = int(day_row.get('isST', 0) or 0)
            if is_st == 1:
                continue

            today_close_rate = float(day_row.get('close_rate', -999) or -999)
            if today_close_rate < self.reversal_threshold:
                continue

            # 日级一字板检测
            d_open = float(day_row.get('open', 0) or 0)
            d_close = float(day_row.get('close', 0) or 0)
            d_high = float(day_row.get('high', 0) or 0)
            d_low = float(day_row.get('low', 0) or 0)
            if d_open == d_close == d_high == d_low and d_open > 0:
                continue

            # 从hour_data获取hour4的close作为买入价
            stock_hour = hour_data.get(code)
            if stock_hour is None:
                continue

            h_close = stock_hour.get('close', 0)
            if not h_close or h_close <= 0:
                continue

            # hour级一字板检测
            h_open = stock_hour.get('open', 0)
            h_high = stock_hour.get('high', 0)
            h_low = stock_hour.get('low', 0)
            if h_open == h_close == h_high == h_low and h_close > 0:
                continue

            return {
                'code': code,
                'code_name': cand['code_name'],
                'price': h_close,  # hour4收盘价买入
                'prev_close_rate': cand['prev_close_rate'],
                'today_close_rate': today_close_rate,
            }

        return None
