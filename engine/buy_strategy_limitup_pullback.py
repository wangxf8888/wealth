"""涨停回调买入策略 (LimitUpPullback)

逻辑：
- 过去20个交易日内有N次涨停（close_rate >= 9.5%）
- T-1日缩量回调（振幅小、从月内高点大幅回落）
- 流通市值50-700亿，非ST
- T日小幅低开/平开（open_rate在-2%~0%）时hour1买入
- 通过on_day_start维护每只股票最近20日历史数据
"""
from collections import deque
from typing import List, Optional
import pandas as pd
from .buy_module import BuyModule


class LimitUpPullbackBuyStrategy(BuyModule):
    """涨停回调策略 - 月内多次涨停后缩量回调买入"""

    def __init__(self, min_limitup_count: int = 2,
                 max_amplitude: float = 4.0,
                 min_pullback_pct: float = 20.0,
                 buy_hour: int = 1):
        self.min_limitup_count = min_limitup_count  # N: 月内最少涨停次数
        self.max_amplitude = max_amplitude           # M: T-1日最大振幅%
        self.min_pullback_pct = min_pullback_pct     # X: 较月内高点最小跌幅%
        self.buy_hour = buy_hour
        # 维护每只股票最近21日数据（on_day_start加入当天，get_candidates用[:-1]取前20日）
        self.history = {}  # {code: deque(maxlen=21) of {'close': float, 'close_rate': float}}

    def on_day_start(self, date: str, day_data: pd.DataFrame):
        """每日开始时将当天数据加入历史记录。

        注意：on_day_start在get_candidates之前调用，因此当天数据会进入history。
        get_candidates中通过history[:-1]排除当天，使用前20日数据，避免未来数据泄露。
        """
        if day_data is None or day_data.empty:
            return

        # 向量化提取，避免逐行iterrows
        codes = day_data['code'].values
        closes = pd.to_numeric(day_data['close'], errors='coerce').fillna(0).values
        close_rates = pd.to_numeric(day_data.get('close_rate', pd.Series(dtype=float)),
                                     errors='coerce').fillna(0).values

        for i in range(len(codes)):
            code = codes[i]
            if code not in self.history:
                self.history[code] = deque(maxlen=21)
            self.history[code].append({
                'close': float(closes[i]),
                'close_rate': float(close_rates[i]),
            })

    def get_candidates(self, date: str, day_data: pd.DataFrame,
                       prev_data: pd.DataFrame, blacklist) -> List[dict]:
        """
        从T-1日数据中筛选候选股（向量化 + 历史数据校验）：
        1. 非ST (isST != 1)
        2. 流通市值50-700亿: amount * 100 / turn
        3. T-1日振幅 <= M%: (high - low) / preclose * 100
        4. 月内涨停次数 >= N（从history前20日计算）
        5. T-1日close较月内最高close下跌 >= X%
        """
        if prev_data is None or prev_data.empty:
            return []

        df = prev_data.copy()

        # 必要字段检查
        required_cols = ['code', 'code_name', 'close', 'high', 'low', 'preclose', 'amount', 'turn']
        for col in required_cols:
            if col not in df.columns:
                return []

        # --- 向量化过滤阶段 ---

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

        # 数值转换
        for col in ['close', 'high', 'low', 'preclose', 'amount', 'turn']:
            df[col] = pd.to_numeric(df[col], errors='coerce').fillna(0)

        # 排除无效数据
        df = df[(df['turn'] > 0) & (df['preclose'] > 0) & (df['close'] > 0)]
        if df.empty:
            return []

        # 流通市值过滤：market_cap = amount * 100 / turn（单位：元）
        # amount单位是元，turn是百分比如3.5表示3.5%
        df = df.copy()
        df['_market_cap'] = df['amount'] * 100.0 / df['turn']
        df = df[(df['_market_cap'] >= 5e9) & (df['_market_cap'] <= 7e10)]
        if df.empty:
            return []

        # T-1日振幅过滤: (high - low) / preclose * 100 <= M
        df['_amplitude'] = (df['high'] - df['low']) / df['preclose'] * 100
        df = df[df['_amplitude'] <= self.max_amplitude]
        if df.empty:
            return []

        # --- 逐股历史数据校验阶段 ---
        candidates = []
        for _, row in df.iterrows():
            code = row['code']
            hist = self.history.get(code)
            # 需要至少21条记录（当天1条+前20日20条）
            if not hist or len(hist) < 21:
                continue

            # 遍历history前20条（排除最后一条即当天数据）
            limitup_count = 0
            month_high = 0.0
            hist_len = len(hist)
            for j, d in enumerate(hist):
                if j == hist_len - 1:
                    break  # 跳过当天数据
                if d['close_rate'] >= 9.5:
                    limitup_count += 1
                if d['close'] > month_high:
                    month_high = d['close']

            # 条件4: 月内涨停次数 >= N
            if limitup_count < self.min_limitup_count:
                continue

            # 条件5: T-1日close较月内最高close下跌 >= X%
            if month_high <= 0:
                continue
            prev_close = float(row['close'])
            pullback_pct = (month_high - prev_close) / month_high * 100
            if pullback_pct < self.min_pullback_pct:
                continue

            candidates.append({
                'code': code,
                'code_name': row.get('code_name', ''),
                'limitup_count': limitup_count,
                'month_high': month_high,
                'pullback_pct': pullback_pct,
                'amplitude': float(row['_amplitude']),
                'market_cap_b': float(row['_market_cap']) / 1e9,
                'prev_close': prev_close,
            })

        # 按回调幅度降序排序（回调越深优先级越高）
        candidates.sort(key=lambda x: x['pullback_pct'], reverse=True)
        return candidates

    def should_buy(self, candidates: List[dict], date: str, hour: int,
                   hour_data: dict, portfolio, day_data: pd.DataFrame = None) -> Optional[dict]:
        """
        在buy_hour（默认hour1）检查T日开盘价：
        - open_rate在-2%~0%之间（小幅低开或平开）
        - 一字板检测（O=H=L=C无法成交）
        - 排除已持仓
        - 排除isST
        """
        if hour != self.buy_hour:
            return None
        if not candidates:
            return None

        held_codes = portfolio.held_codes()

        # 预处理day_data为快速查找dict
        day_lookup = {}
        if day_data is not None and not day_data.empty:
            day_lookup = day_data.set_index('code').to_dict('index')

        for cand in candidates:
            code = cand['code']
            if code in held_codes:
                continue

            # 从hour_data获取今日hour1数据
            stock_hour = hour_data.get(code)
            if stock_hour is None:
                continue

            h_open = stock_hour.get('open', 0)
            h_close = stock_hour.get('close', 0)
            h_high = stock_hour.get('high', 0)
            h_low = stock_hour.get('low', 0)

            if not h_open or h_open <= 0:
                continue

            # 一字板检测：O=H=L=C无法成交
            if h_open == h_close == h_high == h_low and h_open > 0:
                continue

            # 从day_data获取今日完整数据
            day_row = day_lookup.get(code)
            if day_row is None:
                continue

            # isST检测（今日数据二次确认）
            is_st = int(day_row.get('isST', 0) or 0)
            if is_st == 1:
                continue

            # 日级一字板检测（涨停/跌停封死）
            d_open = float(day_row.get('open', 0) or 0)
            d_close = float(day_row.get('close', 0) or 0)
            d_high = float(day_row.get('high', 0) or 0)
            d_low = float(day_row.get('low', 0) or 0)
            if d_open == d_close == d_high == d_low and d_open > 0:
                continue

            # T日开盘价检查：open_rate在-2%~0%之间
            open_rate = float(day_row.get('open_rate', 0) or 0)
            if open_rate < -2.0 or open_rate > 0.0:
                continue

            return {
                'code': code,
                'code_name': cand['code_name'],
                'price': d_open,  # 以T日开盘价买入
                'limitup_count': cand['limitup_count'],
                'pullback_pct': cand['pullback_pct'],
                'market_cap_b': cand['market_cap_b'],
                'open_rate': open_rate,
            }

        return None
