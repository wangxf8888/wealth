"""买入模块接口 + B策略(跳空高开持续)实现"""
from typing import List, Optional
import pandas as pd


class BuyModule:
    """买入模块基类"""

    def get_candidates(self, date: str, day_data: pd.DataFrame,
                       prev_data: pd.DataFrame, blacklist) -> List[dict]:
        """返回按序排列的候选股列表"""
        raise NotImplementedError

    def should_buy(self, candidates: List[dict], date: str, hour: int,
                   hour_data: dict, portfolio, day_data: pd.DataFrame = None) -> Optional[dict]:
        """返回买入信号 {code, code_name, price} 或 None"""
        raise NotImplementedError


class GapUpBuyStrategy(BuyModule):
    """B策略 - 跳空高开持续"""

    def __init__(self, open_gap_min: float = 4.0, close_rate_max: float = 7.0,
                 turn_max: float = 2.0, min_amount: float = 5_000_000,
                 buy_hour: int = 4):
        self.open_gap_min = open_gap_min      # 跳空>=4%
        self.close_rate_max = close_rate_max   # 日涨幅上限7%
        self.turn_max = turn_max               # 换手率<2%
        self.min_amount = min_amount           # 成交额>500万
        self.buy_hour = buy_hour               # 仅在hour4买入

    def get_candidates(self, date: str, day_data: pd.DataFrame,
                       prev_data: pd.DataFrame, blacklist) -> List[dict]:
        """
        候选筛选（向量化实现）：
        - open_rate >= open_gap_min (跳空>=4%)
        - 排除黑名单
        - 按turn ASC排序
        """
        if day_data is None or day_data.empty:
            return []

        # 向量化过滤: open_rate >= open_gap_min
        df = day_data[day_data['open_rate'].notna() & (day_data['open_rate'] >= self.open_gap_min)].copy()
        if df.empty:
            return []

        # 黑名单过滤（向量化）
        if blacklist:
            # ST过滤（code_name + isST双重检测）
            st_mask = df['code_name'].str.upper().str.contains('ST', na=False)
            if 'isST' in df.columns:
                st_mask = st_mask | (df['isST'].fillna(0).astype(int) == 1)
            # 北交所过滤
            bj_mask = df['code'].str.lower().str.startswith('bj.', na=False)
            df = df[~st_mask & ~bj_mask]
            if df.empty:
                return []

        # 填充NaN turn
        df['turn'] = df['turn'].fillna(0)

        # 按turn升序排序
        df = df.sort_values('turn', ascending=True)

        # 构建candidates列表（向量化提取）
        candidates = []
        codes = df['code'].values
        code_names = df['code_name'].values
        opens = df['open'].values
        open_rates = df['open_rate'].values
        turns = df['turn'].values
        precloses = df['preclose'].values

        for i in range(len(codes)):
            candidates.append({
                'code': codes[i],
                'code_name': code_names[i] if pd.notna(code_names[i]) else '',
                'open': float(opens[i]) if pd.notna(opens[i]) else 0,
                'open_rate': float(open_rates[i]),
                'turn': float(turns[i]),
                'preclose': float(precloses[i]) if pd.notna(precloses[i]) else 0,
            })

        return candidates

    def should_buy(self, candidates: List[dict], date: str, hour: int,
                   hour_data: dict, portfolio, day_data: pd.DataFrame = None) -> Optional[dict]:
        """
        仅在hour4执行买入决策：
        - hour4_close >= open (缺口未回补，hour4_close == 日收盘价)
        - 日级close_rate < 7.0% (非涨停附近)
        - 日级close_rate < 9.8% (非涨停封死)
        - turn < 2.0%
        - 日级amount > 5,000,000 (全天成交额)
        已被其他仓位持有的股票不能重复买入
        """
        if hour != self.buy_hour:
            return None

        if not candidates:
            return None

        held_codes = portfolio.held_codes()

        # 预处理day_data为快速查找dict（set_index比iterrows快100倍）
        day_lookup = {}
        if day_data is not None and not day_data.empty:
            day_lookup = day_data.set_index('code').to_dict('index')

        for cand in candidates:
            code = cand['code']

            # 已持有的不重复买入
            if code in held_codes:
                continue

            # 从hour_data获取hour4的close（= 日收盘价，用于买入价和close>=open判断）
            stock_hour = hour_data.get(code)
            if stock_hour is None:
                continue

            h_close = stock_hour.get('close', 0)
            if not h_close or h_close <= 0:
                continue

            # 一字板检测：hour4的O=H=L=C说明涨停/跌停封死，无法成交
            h_open = stock_hour.get('open', 0)
            h_high = stock_hour.get('high', 0)
            h_low = stock_hour.get('low', 0)
            if h_open == h_close == h_high == h_low:
                continue

            # 从day_data获取日级数据（amount和close_rate）
            day_row = day_lookup.get(code)
            if day_row is None:
                continue

            # isST检测：用BaoStock官方isST字段精确识别ST股
            is_st = int(day_row.get('isST', 0) or 0)
            if is_st == 1:
                continue

            # 日级一字板检测（额外保险）
            day_open = float(day_row.get('open', 0) or 0)
            day_close = float(day_row.get('close', 0) or 0)
            day_high = float(day_row.get('high', 0) or 0)
            day_low = float(day_row.get('low', 0) or 0)
            if day_open == day_close == day_high == day_low and day_open > 0:
                continue

            daily_close_rate = float(day_row.get('close_rate', 0) or 0)
            daily_amount = float(day_row.get('amount', 0) or 0)
            turn = cand['turn']
            stock_open = cand['open']

            # 缺口未回补: close >= open (hour4_close == daily close)
            if h_close < stock_open:
                continue

            # 日涨幅上限: close_rate < 7.0% (用日级close_rate!)
            if daily_close_rate >= self.close_rate_max:
                continue

            # 非涨停封死 (用日级close_rate!)
            if daily_close_rate >= 9.8:
                continue

            # 换手率限制
            if turn >= self.turn_max:
                continue

            # 日成交额限制 (用日级amount!)
            if daily_amount < self.min_amount:
                continue

            return {
                'code': code,
                'code_name': cand['code_name'],
                'price': h_close,
                'open_rate': cand['open_rate'],
                'turn': turn,
                'close_rate': daily_close_rate,
            }

        return None
