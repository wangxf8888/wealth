"""首板高开反包买入策略 (FirstBoardGapUp)

逻辑：
- 昨日首板涨停 (close_rate >= 9.5% 且非一字板)
- 今日高开 (open_rate >= gap_min, 默认3%)
- hour1期间持续走强 (hour1_close_rate > hour1_open_rate)
- 在hour1用hour1_close价格买入
- 次日hour4卖出 (T+1合规)
- 排除ST股、北交所、一字板
"""
from typing import List, Optional
import pandas as pd
from .buy_module import BuyModule


class FirstBoardGapUpBuyStrategy(BuyModule):
    """首板高开反包策略 - 昨日首板涨停+今日高开+hour1走强 → 买入追涨"""

    def __init__(self, gap_min: float = 3.0, min_h1_gain: float = 0.0,
                 buy_hour: int = 1, min_turn: float = 0.0,
                 min_amount: float = 0, max_open_rate: float = 9.5,
                 sort_mode: str = 'h1_gain_desc'):
        """
        参数：
          gap_min: 今日open_rate下限（默认3%）
          min_h1_gain: hour1期间最低涨幅(close_rate-open_rate)（默认0，即保持走强）
          buy_hour: 买入hour（默认1）
          min_turn: 昨日换手率下限（默认0，不过滤）
          min_amount: 昨日成交额下限（默认0，不过滤）
          max_open_rate: 高开上限，排除开盘涨停（默认9.5%）
          sort_mode: 排序模式
        """
        self.gap_min = gap_min
        self.min_h1_gain = min_h1_gain
        self.buy_hour = buy_hour
        self.min_turn = min_turn
        self.min_amount = min_amount
        self.max_open_rate = max_open_rate
        self.sort_mode = sort_mode

    def get_candidates(self, date: str, day_data: pd.DataFrame,
                       prev_data: pd.DataFrame, blacklist) -> List[dict]:
        """
        从昨日数据筛选首板涨停股，再从今日数据确认高开：
        - prev_data中: close_rate >= 9.5% (昨日涨停)
        - prev_data中: 非一字板 (非O=H=L=C)
        - day_data中: open_rate >= gap_min (高开)
        - day_data中: open_rate <= max_open_rate (排除开盘涨停)
        - 排除ST/北交所/isST=1
        """
        if prev_data is None or prev_data.empty:
            return []
        if day_data is None or day_data.empty:
            return []

        # --- 昨日数据筛选 ---
        df_prev = prev_data.copy()

        # 必要字段检查
        required_prev = ['code', 'code_name', 'close', 'open', 'high', 'low', 'preclose']
        for col in required_prev:
            if col not in df_prev.columns:
                return []

        # 数值转换
        for col in ['close', 'open', 'high', 'low', 'preclose']:
            df_prev[col] = pd.to_numeric(df_prev[col], errors='coerce')

        df_prev = df_prev.dropna(subset=['close', 'open', 'high', 'low', 'preclose'])
        df_prev = df_prev[df_prev['preclose'] > 0]

        # ST过滤（昨日）
        if 'isST' in df_prev.columns:
            df_prev = df_prev[df_prev['isST'].fillna(0).astype(int) != 1]
        st_mask = df_prev['code_name'].str.upper().str.contains('ST', na=False)
        df_prev = df_prev[~st_mask]

        # 北交所过滤
        bj_mask = df_prev['code'].str.lower().str.startswith('bj.', na=False)
        df_prev = df_prev[~bj_mask]

        if df_prev.empty:
            return []

        # 核心条件1: 昨日收盘涨停 close_rate >= 9.5%
        if 'close_rate' in df_prev.columns:
            df_prev['close_rate'] = pd.to_numeric(df_prev['close_rate'], errors='coerce').fillna(0)
        else:
            df_prev['close_rate'] = (df_prev['close'] / df_prev['preclose'] - 1) * 100
        df_prev = df_prev[df_prev['close_rate'] >= 9.5]

        if df_prev.empty:
            return []

        # 核心条件2: 非一字板 (open != close 或 high != low)
        yizi_mask = (
            (abs(df_prev['open'] - df_prev['close']) < 1e-6) &
            (abs(df_prev['open'] - df_prev['high']) < 1e-6) &
            (abs(df_prev['open'] - df_prev['low']) < 1e-6)
        )
        df_prev = df_prev[~yizi_mask]

        if df_prev.empty:
            return []

        # 换手率过滤
        if self.min_turn > 0 and 'turn' in df_prev.columns:
            df_prev['turn'] = pd.to_numeric(df_prev['turn'], errors='coerce').fillna(0)
            df_prev = df_prev[df_prev['turn'] >= self.min_turn]

        # 成交额过滤
        if self.min_amount > 0 and 'amount' in df_prev.columns:
            df_prev['amount'] = pd.to_numeric(df_prev['amount'], errors='coerce').fillna(0)
            df_prev = df_prev[df_prev['amount'] >= self.min_amount]

        if df_prev.empty:
            return []

        # --- 今日数据匹配 ---
        df_today = day_data.copy()
        if 'open_rate' not in df_today.columns:
            return []
        df_today['open_rate'] = pd.to_numeric(df_today['open_rate'], errors='coerce')

        # 今日isST过滤
        if 'isST' in df_today.columns:
            df_today = df_today[df_today['isST'].fillna(0).astype(int) != 1]
        today_st_mask = df_today['code_name'].str.upper().str.contains('ST', na=False)
        df_today = df_today[~today_st_mask]

        today_lookup = df_today.set_index('code')[['open_rate', 'open', 'close', 'high', 'low']].to_dict('index')

        # --- 匹配并筛选 ---
        candidates = []
        for _, row in df_prev.iterrows():
            code = row['code']
            today_info = today_lookup.get(code)
            if today_info is None:
                continue

            open_rate = today_info.get('open_rate')
            if open_rate is None or pd.isna(open_rate):
                continue

            # 核心条件3: 今日高开 open_rate >= gap_min
            if open_rate < self.gap_min:
                continue

            # 排除开盘涨停（无法买入）
            if open_rate > self.max_open_rate:
                continue

            # 今日一字板排除 (O=H=L=C)
            t_open = float(today_info.get('open', 0) or 0)
            t_close = float(today_info.get('close', 0) or 0)
            t_high = float(today_info.get('high', 0) or 0)
            t_low = float(today_info.get('low', 0) or 0)
            if t_open > 0 and t_open == t_close == t_high == t_low:
                continue

            candidates.append({
                'code': code,
                'code_name': row.get('code_name', ''),
                'prev_close_rate': float(row['close_rate']),
                'today_open_rate': float(open_rate),
                'prev_turn': float(row.get('turn', 0) or 0),
                'prev_amount': float(row.get('amount', 0) or 0),
                'prev_close': float(row['close']),
                'prev_preclose': float(row['preclose']),
            })

        if not candidates:
            return []

        # 排序: 默认按open_rate降序（高开越多动量越强）
        if self.sort_mode == 'h1_gain_desc':
            # 初始排序用open_rate降序，should_buy中会根据h1实际表现选择
            candidates.sort(key=lambda x: (-x['today_open_rate'], x['code']))
        elif self.sort_mode == 'open_rate_desc':
            candidates.sort(key=lambda x: (-x['today_open_rate'], x['code']))
        else:
            candidates.sort(key=lambda x: x['code'])

        return candidates

    def describe_candidate(self, cand: dict) -> str:
        """返回候选股的简要描述（用于verbose日志）"""
        code = cand.get('code', '')
        name = cand.get('code_name', '') or ''
        prev_cr = cand.get('prev_close_rate', 0)
        open_rate = cand.get('today_open_rate', 0)
        prev_turn = cand.get('prev_turn', 0)
        return (f"{code} {name} | 昨收率:{prev_cr:+.2f}% | "
                f"今开:{open_rate:+.2f}% | 换手:{prev_turn:.2f}%")

    def should_buy(self, candidates: List[dict], date: str, hour: int,
                   hour_data: dict, portfolio, day_data: pd.DataFrame = None) -> Optional[dict]:
        """
        在buy_hour（默认hour1）检查hour1走势：
        - hour1_close_rate > hour1_open_rate (hour1期间持续走强)
        - hour1增量涨幅 >= min_h1_gain
        - 使用hour1的close作为买入价
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

            # 从hour_data获取hour1数据
            stock_hour = hour_data.get(code)
            if stock_hour is None:
                continue

            h_open = stock_hour.get('open', 0)
            h_close = stock_hour.get('close', 0)
            h_high = stock_hour.get('high', 0)
            h_low = stock_hour.get('low', 0)

            if not h_open or h_open <= 0:
                continue
            if not h_close or h_close <= 0:
                continue

            # 一字板检测：O=H=L=C无法成交
            if h_open == h_close == h_high == h_low and h_open > 0:
                continue

            # 核心条件: hour1期间走强 (close > open)
            h_open_rate = stock_hour.get('open_rate', 0) or 0
            h_close_rate = stock_hour.get('close_rate', 0) or 0
            h1_gain = h_close_rate - h_open_rate

            if h1_gain < self.min_h1_gain:
                continue

            # close必须高于open (价格层面确认走强)
            if h_close <= h_open:
                continue

            # 排除hour1已涨停的（无法成交）
            if h_close_rate >= 9.8:
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
                'price': h_close,  # 以hour1的close买入
                'prev_close_rate': cand['prev_close_rate'],
                'today_open_rate': cand['today_open_rate'],
                'h1_gain': h1_gain,
            }

        return None
