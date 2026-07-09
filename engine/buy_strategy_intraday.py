# ============================================================
# ⚠️  已废弃 (DEPRECATED) — 回测年化收益 -69%，策略无效
#     废弃原因：日内动量延续逻辑在实盘中持续亏损
#     保留本文件仅供参考，请勿在生产中使用
# ============================================================

"""日内动量延续买入策略 (IntradayMomentum)

逻辑：
- 当日开盘跳空 (open_rate >= 2%)
- hour1收盘价 >= open价格（动量延续）
- H1 close_rate (相对preclose) >= 3%
- 排除ST股、一字板(O=H=L=C)
- 卖出：T+1 H4卖（遵守T+1规则）

核心假设：早盘强势延续到尾盘，次日仍有惯性
"""
from typing import List, Optional
import pandas as pd
from .buy_module import BuyModule


class IntradayMomentumBuyStrategy(BuyModule):
    """日内动量延续策略 - 开盘跳空+H1动量确认买入"""

    def __init__(self, open_rate_min: float = 2.0, h1_close_rate_min: float = 3.0, buy_hour: int = 1):
        self.open_rate_min = open_rate_min  # 开盘涨幅>=2%
        self.h1_close_rate_min = h1_close_rate_min  # H1收盘相对preclose>=3%
        self.buy_hour = buy_hour

    def get_candidates(self, date: str, day_data: pd.DataFrame,
                       prev_data: pd.DataFrame, blacklist) -> List[dict]:
        """
        从当日day_data中筛选开盘跳空高开的股票：
        - open_rate >= 2%（开盘就涨2%以上）
        - 排除isST=1
        - 排除北交所
        """
        if day_data is None or day_data.empty:
            return []

        df = day_data.copy()

        # 必要字段检查
        required_cols = ['code', 'code_name', 'open', 'preclose']
        for col in required_cols:
            if col not in df.columns:
                return []

        # 确保数值类型
        for col in ['open', 'close', 'high', 'low', 'preclose']:
            if col in df.columns:
                df[col] = pd.to_numeric(df[col], errors='coerce')

        # open_rate
        if 'open_rate' in df.columns:
            df['open_rate'] = pd.to_numeric(df['open_rate'], errors='coerce')
        else:
            df['open_rate'] = (df['open'] - df['preclose']) / df['preclose'] * 100

        df = df.dropna(subset=['open', 'preclose', 'open_rate'])
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

        # 核心条件：开盘涨幅>=2%
        df = df[df['open_rate'] >= self.open_rate_min]
        if df.empty:
            return []

        # 排除涨停封死（日级一字板 O=H=L=C）
        if all(c in df.columns for c in ['open', 'high', 'low', 'close']):
            yizi_mask = (df['open'] == df['close']) & (df['open'] == df['high']) & (df['open'] == df['low']) & (df['open'] > 0)
            df = df[~yizi_mask]
        if df.empty:
            return []

        # 按open_rate降序排序（开盘越强，动量越大）
        df = df.sort_values('open_rate', ascending=False)

        # 构建候选列表
        candidates = []
        for _, row in df.iterrows():
            candidates.append({
                'code': row['code'],
                'code_name': row.get('code_name', ''),
                'open_price': float(row['open']),
                'open_rate': float(row['open_rate']),
                'preclose': float(row['preclose']),
            })

        return candidates

    def should_buy(self, candidates: List[dict], date: str, hour: int,
                   hour_data: dict, portfolio, day_data: pd.DataFrame = None) -> Optional[dict]:
        """
        在buy_hour（默认hour1）确认动量后买入：
        - H1 close >= open（没回落）
        - H1 close_rate (相对preclose) >= 3%
        - 一字板检测(O=H=L=C → skip)
        - 排除已持仓股
        - price = H1 close
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

            # 从hour_data获取H1数据
            stock_hour = hour_data.get(code)
            if stock_hour is None:
                continue

            h_open = stock_hour.get('open', 0)
            h_close = stock_hour.get('close', 0)
            h_high = stock_hour.get('high', 0)
            h_low = stock_hour.get('low', 0)

            if not h_close or h_close <= 0:
                continue
            if not h_open or h_open <= 0:
                continue

            # 一字板检测：O=H=L=C无法成交
            if h_open == h_close == h_high == h_low and h_open > 0:
                continue

            # 动量确认：H1 close >= open（没回落）
            if h_close < cand['open_price']:
                continue

            # H1 close_rate相对preclose >= 3%
            preclose = cand['preclose']
            if preclose <= 0:
                continue
            h1_close_rate = (h_close - preclose) / preclose * 100
            if h1_close_rate < self.h1_close_rate_min:
                continue

            # 排除涨停封死：H1 close_rate >= 9.8%说明涨停封死无法买入
            if h1_close_rate >= 9.8:
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
                'price': h_close,  # 以H1 close买入
                'open_rate': cand['open_rate'],
                'h1_close_rate': h1_close_rate,
            }

        return None
