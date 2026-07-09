# ============================================================
# ⚠️  已废弃 (DEPRECATED) — 回测年化收益 -63%，策略无效
#     废弃原因：大阴线反弹逻辑在实盘中持续亏损
#     保留本文件仅供参考，请勿在生产中使用
# ============================================================

"""大阴线反弹买入策略 (BigDropBounce)

逻辑：
- 昨日大跌 (close_rate <= -7%)，不一定跌停
- 排除ST股、一字板(O=H=L=C)
- 今日hour1买入（T+1开盘价）
- 检查今日开盘没有继续跌停(open未封死)
- 卖出：T+2 H4卖

核心假设：大跌后超卖修复反弹（类似Reversal但更严格只看跌幅不看反包）
"""
from typing import List, Optional
import pandas as pd
from .buy_module import BuyModule


class BigDropBounceBuyStrategy(BuyModule):
    """大阴线反弹策略 - 昨日大跌今日开盘买入，赌超卖修复"""

    def __init__(self, drop_threshold: float = -7.0, buy_hour: int = 1):
        self.drop_threshold = drop_threshold  # 昨日跌幅阈值(默认-7%)
        self.buy_hour = buy_hour

    def get_candidates(self, date: str, day_data: pd.DataFrame,
                       prev_data: pd.DataFrame, blacklist) -> List[dict]:
        """
        从昨日数据中筛选大跌股票：
        - 昨日 close_rate <= -7%（大阴线）
        - 排除isST=1
        - 排除北交所
        - 排除一字板(O=H=L=C)
        """
        if prev_data is None or prev_data.empty:
            return []

        df = prev_data.copy()

        # 必要字段检查
        required_cols = ['code', 'code_name', 'close', 'preclose']
        for col in required_cols:
            if col not in df.columns:
                return []

        # 确保数值类型
        for col in ['open', 'close', 'high', 'low', 'preclose']:
            if col in df.columns:
                df[col] = pd.to_numeric(df[col], errors='coerce')

        # close_rate字段
        if 'close_rate' in df.columns:
            df['close_rate'] = pd.to_numeric(df['close_rate'], errors='coerce')
        else:
            df['close_rate'] = (df['close'] - df['preclose']) / df['preclose'] * 100

        df = df.dropna(subset=['close', 'preclose', 'close_rate'])
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

        # 核心条件：昨日大跌 (close_rate <= drop_threshold)
        df = df[df['close_rate'] <= self.drop_threshold]
        if df.empty:
            return []

        # 排除一字板(昨日O=H=L=C表示跌停封死，不参考)
        if all(c in df.columns for c in ['open', 'high', 'low', 'close']):
            yizi_mask = (df['open'] == df['close']) & (df['open'] == df['high']) & (df['open'] == df['low']) & (df['open'] > 0)
            df = df[~yizi_mask]
        if df.empty:
            return []

        # 按跌幅排序（跌得越多修复空间越大）
        df = df.sort_values('close_rate', ascending=True)

        # 构建候选列表
        candidates = []
        for _, row in df.iterrows():
            candidates.append({
                'code': row['code'],
                'code_name': row.get('code_name', ''),
                'prev_close_rate': float(row['close_rate']),
                'prev_close': float(row['close']),
                'prev_preclose': float(row['preclose']),
            })

        return candidates

    def should_buy(self, candidates: List[dict], date: str, hour: int,
                   hour_data: dict, portfolio, day_data: pd.DataFrame = None) -> Optional[dict]:
        """
        在buy_hour（默认hour1）开盘买入：
        - 使用hour1的open作为买入价
        - 一字板检测(O=H=L=C → skip, 今日继续跌停封死无法买入)
        - 检查今日开盘未继续跌停（open_rate > -9%）
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

            # 从hour_data获取今日H1数据
            stock_hour = hour_data.get(code)
            if stock_hour is None:
                continue

            h_open = stock_hour.get('open', 0)
            h_close = stock_hour.get('close', 0)
            h_high = stock_hour.get('high', 0)
            h_low = stock_hour.get('low', 0)

            if not h_open or h_open <= 0:
                continue

            # 一字板检测：O=H=L=C说明跌停封死，无法买入
            if h_open == h_close == h_high == h_low and h_open > 0:
                continue

            # 检查今日开盘未继续跌停: open相对昨收的涨跌幅 > -9%
            prev_close = cand['prev_close']
            if prev_close > 0:
                open_rate_today = (h_open - prev_close) / prev_close * 100
                if open_rate_today <= -9.0:
                    continue  # 今日开盘接近跌停，放弃

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
                'prev_close_rate': cand['prev_close_rate'],
            }

        return None
