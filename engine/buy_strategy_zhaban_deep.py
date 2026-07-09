"""涨停炸板深度回调买入策略 (ZhaBanDeep)

逻辑：
- 昨日盘中触及涨停（high >= preclose * 1.098）
- 昨日深度炸板回落: close回落幅度在[retreat_min, retreat_max)范围
- 今日低开: open_rate在[open_min, open_max]范围（默认-2%到-1%）
- 今日hour1_open买入
- 排除ST股、北交所、一字板、低换手
"""
from typing import List, Optional
import pandas as pd
from .buy_module import BuyModule


class ZhaBanDeepBuyStrategy(BuyModule):
    """涨停炸板深度回调策略 - 昨日炸板回落3-5%且今日低开-2%~-1%"""

    def __init__(self, retreat_min: float = 0.03, retreat_max: float = 0.05,
                 open_min: float = -2.0, open_max: float = -1.0,
                 buy_hour: int = 1, min_turn: float = 3.0):
        self.retreat_min = retreat_min    # 最小回落比例（从high算）
        self.retreat_max = retreat_max    # 最大回落比例（从high算）
        self.open_min = open_min          # 今日open_rate下限%
        self.open_max = open_max          # 今日open_rate上限%
        self.buy_hour = buy_hour          # 买入hour
        self.min_turn = min_turn          # 最低换手率%

    def get_candidates(self, date: str, day_data: pd.DataFrame,
                       prev_data: pd.DataFrame, blacklist) -> List[dict]:
        """
        从昨日数据筛选炸板深度回调股票，再结合今日open_rate过滤：
        1. 昨日 high >= preclose * 1.098（盘中触及涨停）
        2. 昨日回落幅度: retreat_min <= (high - close) / high < retreat_max
        3. 今日 open_rate 在 [open_min, open_max] 范围
        4. 昨日换手率 >= min_turn
        5. 排除ST/北交所/isST=1
        6. 按回落深度降序排列
        """
        if prev_data is None or prev_data.empty:
            return []
        if day_data is None or day_data.empty:
            return []

        df = prev_data.copy()

        # 必要字段检查
        required_cols = ['code', 'code_name', 'high', 'close', 'preclose']
        for col in required_cols:
            if col not in df.columns:
                return []

        # 确保数值类型
        df['high'] = pd.to_numeric(df['high'], errors='coerce')
        df['close'] = pd.to_numeric(df['close'], errors='coerce')
        df['preclose'] = pd.to_numeric(df['preclose'], errors='coerce')

        # 过滤NaN
        df = df.dropna(subset=['high', 'close', 'preclose'])
        df = df[df['preclose'] > 0]
        df = df[df['high'] > 0]

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

        # 核心条件1：昨日盘中触及涨停
        limit_up_price = df['preclose'] * 1.098
        touched_limit = df['high'] >= limit_up_price

        df = df[touched_limit]
        if df.empty:
            return []

        # 核心条件2：回落幅度在[retreat_min, retreat_max)
        df = df.copy()
        df['_retreat'] = (df['high'] - df['close']) / df['high']
        df = df[(df['_retreat'] >= self.retreat_min) & (df['_retreat'] < self.retreat_max)]
        if df.empty:
            return []

        # 换手率过滤
        if 'turn' in df.columns:
            df['turn'] = pd.to_numeric(df['turn'], errors='coerce').fillna(0)
            df = df[df['turn'] >= self.min_turn]
            if df.empty:
                return []

        # 结合今日数据过滤open_rate
        today_df = day_data[['code', 'open_rate']].copy()
        today_df['open_rate'] = pd.to_numeric(today_df['open_rate'], errors='coerce')
        today_df = today_df.dropna(subset=['open_rate'])
        today_df = today_df[(today_df['open_rate'] >= self.open_min) &
                            (today_df['open_rate'] <= self.open_max)]

        if today_df.empty:
            return []

        # 取交集: 昨日炸板 + 今日低开
        valid_codes = set(today_df['code'].values)
        df = df[df['code'].isin(valid_codes)]
        if df.empty:
            return []

        # 按回落深度降序排列
        df = df.sort_values(['_retreat', 'code'], ascending=[False, True])

        # 构建候选列表
        today_open_rate_map = today_df.set_index('code')['open_rate'].to_dict()
        candidates = []
        for _, row in df.iterrows():
            code = row['code']
            candidates.append({
                'code': code,
                'code_name': row.get('code_name', ''),
                'prev_high': float(row['high']),
                'prev_close': float(row['close']),
                'prev_preclose': float(row['preclose']),
                'retreat_pct': float(row['_retreat']),
                'prev_turn': float(row.get('turn', 0) or 0),
                'today_open_rate': float(today_open_rate_map.get(code, 0)),
            })

        return candidates

    def describe_candidate(self, cand: dict) -> str:
        """返回候选股简要描述"""
        code = cand.get('code', '')
        name = cand.get('code_name', '') or ''
        retreat = cand.get('retreat_pct', 0) * 100
        open_rate = cand.get('today_open_rate', 0)
        prev_turn = cand.get('prev_turn', 0)
        return (f"{code} {name} | 回落:{retreat:.2f}% | "
                f"今开:{open_rate:.2f}% | 昨换手:{prev_turn:.2f}%")

    def should_buy(self, candidates: List[dict], date: str, hour: int,
                   hour_data: dict, portfolio, day_data: pd.DataFrame = None) -> Optional[dict]:
        """
        在buy_hour（默认hour1）用hour1_open买入：
        - 一字板检测(O=H=L=C → skip)
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

            # 从hour_data获取今日买入价
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

            # isST检测（今日数据二次确认）
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
                'price': h_open,  # 以hour1_open买入
                'retreat_pct': cand['retreat_pct'],
                'today_open_rate': cand['today_open_rate'],
            }

        return None
