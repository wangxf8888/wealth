# ============================================================
# ⚠️  已废弃 (DEPRECATED) — 全参数网格搜索均亏损，策略无效
#     废弃原因：全参数空间回测结果均为负收益
#     保留本文件仅供参考，请勿在生产中使用
# ============================================================

"""连板首阴低吸买入策略 (FirstNeg)

逻辑：
- 连续N日(N>=min_streak)涨停(close_rate >= 9.5%)后，昨日首次未涨停
- 今日hour1开盘买入
- 维护一个streak状态字典，通过on_day_start每日更新
- 无未来数据泄露：今日on_day_start用今日数据识别首阴→明日才买入
"""
from typing import List, Optional
import pandas as pd
from .buy_module import BuyModule


class FirstNegBuyStrategy(BuyModule):
    """连板首阴低吸策略 - 连续涨停后首次未涨停，次日开盘买入"""

    def __init__(self, min_streak: int = 2, first_neg_type: str = 'all',
                 buy_hour: int = 1, limit_up_threshold: float = 9.5):
        self.min_streak = min_streak          # 最低连板天数
        self.first_neg_type = first_neg_type  # 'positive'=首阴需收阳, 'all'=全部
        self.buy_hour = buy_hour
        self.limit_up_threshold = limit_up_threshold  # 涨停判断阈值(%)

        # 状态维护
        self.streaks = {}              # {code: consecutive_limit_up_days}
        self._buy_candidates = {}      # 今日的买入候选（昨日首阴）
        self._next_day_candidates = {} # 今日首阴 → 明日买入候选

    def on_day_start(self, date: str, day_data: pd.DataFrame):
        """
        每个交易日开始时更新连板状态（无未来数据泄露）。
        
        流程：
        1. 将上一轮_next_day_candidates转为今日的_buy_candidates
        2. 用今日day_data更新streaks，识别今日首阴的股票存入_next_day_candidates
        """
        # Step 1: 上一轮的"今日首阴"变为"今天可买入"
        self._buy_candidates = self._next_day_candidates.copy()
        self._next_day_candidates = {}

        # Step 2: 用今日数据更新streaks
        if day_data is None or day_data.empty:
            return

        if 'close_rate' not in day_data.columns:
            return

        for _, row in day_data.iterrows():
            code = row.get('code')
            if not code:
                continue

            close_rate = pd.to_numeric(row.get('close_rate', 0), errors='coerce')
            if pd.isna(close_rate):
                close_rate = 0.0

            prev_streak = self.streaks.get(code, 0)

            if close_rate >= self.limit_up_threshold:
                # 今日涨停，连板+1
                self.streaks[code] = prev_streak + 1
            else:
                # 今日未涨停
                if prev_streak >= self.min_streak:
                    # 之前连板>=min_streak，今日首次未涨停 → 首阴
                    is_valid = True

                    # first_neg_type条件
                    if self.first_neg_type == 'positive' and close_rate < 0:
                        is_valid = False

                    # isST预过滤
                    if is_valid:
                        is_st = int(row.get('isST', 0) or 0)
                        code_name = row.get('code_name', '')
                        if is_st == 1 or (code_name and 'ST' in str(code_name).upper()):
                            is_valid = False

                    # 北交所过滤
                    if is_valid and str(code).lower().startswith('bj.'):
                        is_valid = False

                    if is_valid:
                        self._next_day_candidates[code] = {
                            'code': code,
                            'code_name': row.get('code_name', ''),
                            'streak_days': prev_streak,
                            'first_neg_rate': close_rate,
                        }

                # 重置streak
                self.streaks[code] = 0

    def get_candidates(self, date: str, day_data: pd.DataFrame,
                       prev_data: pd.DataFrame, blacklist) -> List[dict]:
        """
        返回昨日首阴的股票列表（从内部状态获取）。
        on_day_start中已将前一日识别的首阴股票存入_buy_candidates。
        """
        if not self._buy_candidates:
            return []

        return list(self._buy_candidates.values())

    def should_buy(self, candidates: List[dict], date: str, hour: int,
                   hour_data: dict, portfolio, day_data: pd.DataFrame = None) -> Optional[dict]:
        """
        在buy_hour（默认hour1）开盘买入：
        - 使用hour1的open作为买入价
        - 一字板检测
        - 排除已持仓股
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

            # 一字板检测：O=H=L=C无法成交
            if h_open == h_close == h_high == h_low and h_open > 0:
                continue

            # isST检测（今日数据二次确认）
            day_row = day_lookup.get(code)
            if day_row:
                is_st = int(day_row.get('isST', 0) or 0)
                if is_st == 1:
                    continue
                # 日级一字板
                d_open = float(day_row.get('open', 0) or 0)
                d_close = float(day_row.get('close', 0) or 0)
                d_high = float(day_row.get('high', 0) or 0)
                d_low = float(day_row.get('low', 0) or 0)
                if d_open == d_close == d_high == d_low and d_open > 0:
                    continue

            return {
                'code': code,
                'code_name': cand['code_name'],
                'price': h_open,  # hour1开盘价买入
                'streak_days': cand.get('streak_days', 0),
                'first_neg_rate': cand.get('first_neg_rate', 0),
            }

        return None
