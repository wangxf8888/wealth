"""涨停封板次日高开买入策略 (LimitUpNext)

逻辑：
- 昨日涨停封板（close >= preclose*1.098 且 close == high，即封板到收盘）
- 排除ST、北交所、一字板
- 今日 hour1 open_rate 在 min_open_rate ~ max_open_rate 范围内（高开但不过热）
- 买入价：hour1_open
- 卖出：次日Hour2 close（与ZhaBan一致）
"""
from typing import List, Optional
import pandas as pd
from .buy_module import BuyModule


class LimitUpNextBuyStrategy(BuyModule):
    """涨停封板次日高开策略 - 昨日封板到收盘，今日小幅高开买入"""

    def __init__(self, max_open_rate: float = 0.5, min_open_rate: float = 0.0,
                 buy_hour: int = 1):
        self.max_open_rate = max_open_rate  # 最大高开幅度(%)
        self.min_open_rate = min_open_rate  # 最小高开幅度(%)
        self.buy_hour = buy_hour

    def get_candidates(self, date: str, day_data: pd.DataFrame,
                       prev_data: pd.DataFrame, blacklist) -> List[dict]:
        """
        从昨日数据中筛选封板股票：
        - 昨日 close >= preclose * 1.098（涨停）
        - 昨日 close == high（封板到收盘）
        - 排除isST=1
        - 排除北交所
        - 排除一字板（open == close == high == low）
        """
        if prev_data is None or prev_data.empty:
            return []

        df = prev_data.copy()

        # 必要字段检查
        required_cols = ['code', 'code_name', 'high', 'close', 'preclose', 'open', 'low']
        for col in required_cols:
            if col not in df.columns:
                return []

        # 确保数值类型
        for col in ['high', 'close', 'preclose', 'open', 'low']:
            df[col] = pd.to_numeric(df[col], errors='coerce')

        # 过滤NaN
        df = df.dropna(subset=['high', 'close', 'preclose', 'open', 'low'])
        df = df[df['preclose'] > 0]

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

        # 排除一字板：open == close == high == low
        yizi_mask = (df['open'] == df['close']) & (df['close'] == df['high']) & (df['high'] == df['low'])
        df = df[~yizi_mask]

        if df.empty:
            return []

        # 核心条件1：昨日涨停 close >= preclose * 1.098
        limit_up_price = df['preclose'] * 1.098
        at_limit = df['close'] >= limit_up_price

        # 核心条件2：封板到收盘 close == high
        sealed = df['close'] == df['high']

        df = df[at_limit & sealed]
        if df.empty:
            return []

        # 按code排序保证可复现
        df = df.sort_values('code', ascending=True)

        # 构建候选列表
        candidates = []
        for _, row in df.iterrows():
            candidates.append({
                'code': row['code'],
                'code_name': row.get('code_name', ''),
                'prev_close': float(row['close']),
                'prev_preclose': float(row['preclose']),
                'prev_high': float(row['high']),
                'prev_amount': float(row.get('amount', 0) or 0),
            })

        return candidates

    def should_buy(self, candidates: List[dict], date: str, hour: int,
                   hour_data: dict, portfolio, day_data: pd.DataFrame = None) -> Optional[dict]:
        """
        在buy_hour（默认hour1）开盘买入：
        - 使用hour1的open作为买入价
        - 检查open_rate在min_open_rate ~ max_open_rate范围内
        - 一字板检测(O=H=L=C -> skip)
        - 排除已持仓股
        - 排除isST
        """
        if hour != self.buy_hour:
            return None
        if not candidates:
            return None

        held_codes = portfolio.held_codes()

        # 预处理day_data用于isST检测
        day_lookup = {}
        if day_data is not None and not day_data.empty:
            day_lookup = day_data.set_index('code').to_dict('index')

        for cand in candidates:
            code = cand['code']

            if code in held_codes:
                continue

            # 从hour_data获取今日开盘价
            stock_hour = hour_data.get(code)
            if stock_hour is None:
                continue

            h_open = stock_hour.get('open', 0)
            h_close = stock_hour.get('close', 0)
            h_high = stock_hour.get('high', 0)
            h_low = stock_hour.get('low', 0)

            if not h_open or h_open <= 0:
                continue

            # open_rate范围过滤
            open_rate = stock_hour.get('open_rate', 0)
            if open_rate is None:
                open_rate = 0
            if open_rate < self.min_open_rate or open_rate > self.max_open_rate:
                continue

            # 一字板检测：O=H=L=C无法成交
            if h_open == h_close == h_high == h_low and h_open > 0:
                continue

            # isST检测（今日数据）
            day_row = day_lookup.get(code)
            if day_row:
                is_st = int(day_row.get('isST', 0) or 0)
                if is_st == 1:
                    continue
                # 今日日级一字板检测
                d_open = float(day_row.get('open', 0) or 0)
                d_close = float(day_row.get('close', 0) or 0)
                d_high = float(day_row.get('high', 0) or 0)
                d_low = float(day_row.get('low', 0) or 0)
                if d_open == d_close == d_high == d_low and d_open > 0:
                    continue

            return {
                'code': code,
                'code_name': cand['code_name'],
                'price': h_open,  # 以hour1的open买入
                'prev_close': cand['prev_close'],
                'open_rate': open_rate,
            }

        return None
