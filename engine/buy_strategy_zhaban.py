"""涨停炸板修复买入策略 (ZhaBan)

逻辑：
- 昨日盘中触及涨停（high >= preclose * 1.098）但收盘未封住（close < high * fallback）
- 今日hour1开盘买入（T+1开盘价）
- 排除ST股、一字板
"""
from typing import List, Optional
import pandas as pd
from .buy_module import BuyModule


class ZhaBanBuyStrategy(BuyModule):
    """涨停炸板修复策略 - 昨日触及涨停未封住，今日开盘买入"""

    def __init__(self, zhaban_fallback: float = 0.99, buy_hour: int = 1, sort_mode: str = 'default',
                 max_open_rate: float = None, min_amount_filter: float = None,
                 max_amplitude_filter: float = None):
        self.zhaban_fallback = zhaban_fallback  # 收盘 < high * fallback 视为炸板
        self.buy_hour = buy_hour
        self.sort_mode = sort_mode  # default / fallback_depth / amount / turn / close_rate / volume / amplitude / gap_down
        self.max_open_rate = max_open_rate  # 竞价高开过滤: None=不过滤, 如5.0=过滤open_rate>5%的
        self.min_amount_filter = min_amount_filter  # 成交额下限过滤: None=不过滤, 如1e8=过滤amount<1亿的
        self.max_amplitude_filter = max_amplitude_filter  # 振幅上限过滤: None=不过滤, 如10=过滤振幅>10%的

    def get_candidates(self, date: str, day_data: pd.DataFrame,
                       prev_data: pd.DataFrame, blacklist) -> List[dict]:
        """
        从昨日数据中筛选炸板股票：
        - 昨日 high >= preclose * 1.098（盘中触及涨停）
        - 昨日 close < high * zhaban_fallback（收盘未封住，回落）
        - 排除isST=1
        - 排除北交所
        """
        if prev_data is None or prev_data.empty:
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

        # 核心条件：昨日盘中触及涨停
        limit_up_price = df['preclose'] * 1.098
        touched_limit = df['high'] >= limit_up_price

        # 核心条件：收盘未封住（回落超过1%）
        not_sealed = df['close'] < df['high'] * self.zhaban_fallback

        df = df[touched_limit & not_sealed]
        if df.empty:
            return []

        # 成交额下限过滤
        if self.min_amount_filter is not None and 'amount' in df.columns:
            df['_amount'] = pd.to_numeric(df['amount'], errors='coerce').fillna(0)
            df = df[df['_amount'] >= self.min_amount_filter]
            if df.empty:
                return []

        # 振幅上限过滤: (high-low)/preclose*100
        if self.max_amplitude_filter is not None and 'low' in df.columns:
            df['low'] = pd.to_numeric(df['low'], errors='coerce')
            df['_amplitude'] = (df['high'] - df['low']) / df['preclose'] * 100
            df = df[df['_amplitude'] <= self.max_amplitude_filter]
            if df.empty:
                return []

        # 排序（稳定排序，相同值时按code排序保证可复现）
        df = df.copy()
        if self.sort_mode == 'fallback_depth':
            # 按回落幅度排序: (high - close) / high 越大=炸板越深=修复动力越强
            df['_sort_key'] = (df['high'] - df['close']) / df['high']
            df = df.sort_values(['_sort_key', 'code'], ascending=[False, True])
        elif self.sort_mode == 'amount':
            # 按成交额降序（大盘股更稳定）
            if 'amount' in df.columns:
                df['_sort_key'] = pd.to_numeric(df['amount'], errors='coerce').fillna(0)
                df = df.sort_values(['_sort_key', 'code'], ascending=[False, True])
        elif self.sort_mode == 'turn':
            # 按换手率降序（高换手=资金关注度高）
            if 'turn' in df.columns:
                df['_sort_key'] = pd.to_numeric(df['turn'], errors='coerce').fillna(0)
                df = df.sort_values(['_sort_key', 'code'], ascending=[False, True])
        elif self.sort_mode == 'close_rate':
            # 按昨日close_rate升序（跌幅最大=最负的在前，修复弹性最大）
            if 'close_rate' in df.columns:
                df['_sort_key'] = pd.to_numeric(df['close_rate'], errors='coerce').fillna(0)
                df = df.sort_values(['_sort_key', 'code'], ascending=[True, True])
        elif self.sort_mode == 'volume':
            # 按成交量降序
            if 'volume' in df.columns:
                df['_sort_key'] = pd.to_numeric(df['volume'], errors='coerce').fillna(0)
                df = df.sort_values(['_sort_key', 'code'], ascending=[False, True])
        elif self.sort_mode == 'amplitude':
            # 按昨日振幅 (high-low)/preclose 降序（波动越大修复越猛）
            df['_sort_key'] = (df['high'] - df['low']) / df['preclose']
            df = df.sort_values(['_sort_key', 'code'], ascending=[False, True])
        elif self.sort_mode == 'gap_down':
            # 按close相对涨停价的差距 (high*1.098 - close)/close 降序，差距越大优先
            df['_sort_key'] = (df['high'] * 1.098 - df['close']) / df['close']
            df = df.sort_values(['_sort_key', 'code'], ascending=[False, True])
        else:
            # default - 按code排序保证可复现
            df = df.sort_values('code', ascending=True)

        # 构建候选列表
        candidates = []
        for _, row in df.iterrows():
            prev_low_val = pd.to_numeric(row.get('low', 0), errors='coerce')
            try:
                prev_low_f = float(prev_low_val) if prev_low_val == prev_low_val else 0.0
            except (TypeError, ValueError):
                prev_low_f = 0.0
            candidates.append({
                'code': row['code'],
                'code_name': row.get('code_name', ''),
                'prev_high': float(row['high']),
                'prev_close': float(row['close']),
                'prev_low': prev_low_f,
                'prev_preclose': float(row['preclose']),
                'prev_amount': float(row.get('amount', 0) or 0),
                'prev_turn': float(row.get('turn', 0) or 0),
            })

        return candidates

    def describe_candidate(self, cand: dict) -> str:
        """返回候选股的简要描述（用于verbose日志的候选列表）"""
        prev_high = cand.get('prev_high', 0) or 0
        prev_close = cand.get('prev_close', 0) or 0
        prev_low = cand.get('prev_low', 0) or 0
        prev_preclose = cand.get('prev_preclose', 0) or 0
        prev_turn = cand.get('prev_turn', 0) or 0
        ch_ratio = (prev_close / prev_high) if prev_high > 0 else 0.0
        amplitude = ((prev_high - prev_low) / prev_preclose * 100) if prev_preclose > 0 else 0.0
        code = cand.get('code', '')
        name = cand.get('code_name', '') or ''
        return (f"{code} {name} | close/high={ch_ratio:.3f} | "
                f"换手:{prev_turn:.2f}% | 振幅:{amplitude:.2f}%")

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

            # 竞价高开过滤: 如果open_rate > max_open_rate则跳过
            if self.max_open_rate is not None:
                open_rate = stock_hour.get('open_rate', 0)
                if open_rate and open_rate > self.max_open_rate:
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
                'prev_high': cand['prev_high'],
                'prev_close': cand['prev_close'],
            }

        return None
