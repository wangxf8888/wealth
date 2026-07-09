"""尾盘异动次日买入策略 (TailMomentum)

逻辑：
- 昨日(D0)尾盘异动: hour3_close_rate < h3_max 且 hour4-hour3 >= tail_min
- 今日(D1) hour1_open 买入 (T+1合规)
- 排除ST股、北交所、一字板、已涨停

最佳搭配卖出: NextDayCloseSellStrategy(sell_hour=1, min_hold_days=2)
即 D2 hour1_close 卖出
"""
from typing import List, Optional
import pandas as pd
from .buy_module import BuyModule


class TailMomentumBuyStrategy(BuyModule):
    """尾盘异动次日买入策略 - 昨日尾盘拉升，今日开盘买入"""

    def __init__(self, tail_min: float = 3.0, tail_max: float = None,
                 h3_max: float = 0.0, buy_hour: int = 1,
                 min_turn: float = 2.0, max_close_rate: float = 9.5):
        self.tail_min = tail_min          # 尾盘拉升下限% (hour4-hour3 >= tail_min)
        self.tail_max = tail_max          # 尾盘拉升上限% (None=不限)
        self.h3_max = h3_max              # hour3_close_rate上限%
        self.buy_hour = buy_hour          # 买入时机(默认hour1)
        self.min_turn = min_turn          # 最低换手率%
        self.max_close_rate = max_close_rate  # 日涨幅上限%(排除涨停)

    def get_candidates(self, date: str, day_data: pd.DataFrame,
                       prev_data: pd.DataFrame, blacklist) -> List[dict]:
        """
        从昨日数据(prev_data)中筛选尾盘异动股：
        - hour3_close_rate < h3_max
        - (hour4_close_rate - hour3_close_rate) >= tail_min
        - (hour4_close_rate - hour3_close_rate) < tail_max (可选)
        - close_rate < max_close_rate (未涨停)
        - turn > min_turn (有活跃度)
        - 排除ST/北交所/isST=1
        按 (hour4_close_rate - hour3_close_rate) 降序排列
        """
        if prev_data is None or prev_data.empty:
            return []

        df = prev_data.copy()

        # 必要字段检查
        required_cols = ['code', 'code_name', 'hour3_close_rate', 'hour4_close_rate',
                         'close_rate', 'turn']
        for col in required_cols:
            if col not in df.columns:
                return []

        # 确保数值类型
        for col in ['hour3_close_rate', 'hour4_close_rate', 'close_rate', 'turn']:
            df[col] = pd.to_numeric(df[col], errors='coerce')

        # 过滤NaN
        df = df.dropna(subset=['hour3_close_rate', 'hour4_close_rate', 'close_rate', 'turn'])

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

        # 核心条件1: hour3_close_rate < h3_max
        df = df[df['hour3_close_rate'] < self.h3_max]
        if df.empty:
            return []

        # 计算尾盘拉升幅度
        df = df.copy()
        df['_tail_amp'] = df['hour4_close_rate'] - df['hour3_close_rate']

        # 核心条件2: 尾盘拉升 >= tail_min
        df = df[df['_tail_amp'] >= self.tail_min]
        if df.empty:
            return []

        # 核心条件3: 尾盘拉升 < tail_max (可选)
        if self.tail_max is not None:
            df = df[df['_tail_amp'] < self.tail_max]
            if df.empty:
                return []

        # 日涨幅限制: close_rate < max_close_rate (排除涨停)
        df = df[df['close_rate'] < self.max_close_rate]
        if df.empty:
            return []

        # 换手率限制: turn > min_turn
        df = df[df['turn'] > self.min_turn]
        if df.empty:
            return []

        # 按尾盘拉升幅度降序排列
        df = df.sort_values(['_tail_amp', 'code'], ascending=[False, True])

        # 构建候选列表
        candidates = []
        for _, row in df.iterrows():
            candidates.append({
                'code': row['code'],
                'code_name': row.get('code_name', '') or '',
                'tail_amp': float(row['_tail_amp']),
                'h3_rate': float(row['hour3_close_rate']),
                'h4_rate': float(row['hour4_close_rate']),
                'prev_close_rate': float(row['close_rate']),
                'prev_turn': float(row['turn']),
            })

        return candidates

    def describe_candidate(self, cand: dict) -> str:
        """返回候选股的简要描述（用于verbose日志）"""
        code = cand.get('code', '')
        name = cand.get('code_name', '') or ''
        tail_amp = cand.get('tail_amp', 0)
        h3_rate = cand.get('h3_rate', 0)
        prev_turn = cand.get('prev_turn', 0)
        return (f"{code} {name} | 尾盘拉升:{tail_amp:.2f}% | "
                f"H3:{h3_rate:.2f}% | 换手:{prev_turn:.2f}%")

    def should_buy(self, candidates: List[dict], date: str, hour: int,
                   hour_data: dict, portfolio, day_data: pd.DataFrame = None) -> Optional[dict]:
        """
        在buy_hour（默认hour1）开盘买入：
        - 使用hour1的open作为买入价
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
                'tail_amp': cand['tail_amp'],
                'h3_rate': cand['h3_rate'],
            }

        return None
