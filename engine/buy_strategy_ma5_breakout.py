"""MA5均线突破买入策略 (MA5 Breakout >700亿大盘股)

逻辑（严格T+0合规）：
- 选股条件：
  1. 市值 > 700亿（流通市值，由成交额/换手率估算）
  2. MA5_yesterday = mean(close of [day-5, day-4, day-3, day-2, yesterday])
  3. yesterday_close < MA5_yesterday（昨日在MA5下方）
  4. today_open > MA5_yesterday（今日开盘突破MA5）
  5. today_open < limit_up_price（不是涨停开盘）
  6. 非ST、非停牌
  7. 排除一字板（open=high=low=close=涨停价）

- 买入执行：Hour1开盘买入，价格=hour1_open
- T+0合规：MA5仅用yesterday及之前数据计算，不使用today任何收盘数据
"""
from typing import List, Optional
import pandas as pd
from .buy_module import BuyModule


def _get_limit_ratio(code: str) -> float:
    """涨跌停比例: 主板10%, 创业板/科创20%, 北交所30%"""
    if not code:
        return 0.1
    c = code.replace('sz.', '').replace('sh.', '').replace('bj.', '')
    if c.startswith('300') or c.startswith('301') or c.startswith('688'):
        return 0.2
    if code.startswith('bj.'):
        return 0.3
    return 0.1


def _calc_limit_up(preclose: float, code: str) -> float:
    """计算涨停价: round(preclose * (1 + ratio), 2)"""
    ratio = _get_limit_ratio(code)
    return round(preclose * (1 + ratio), 2)


class MA5BreakoutBuyStrategy(BuyModule):
    """MA5均线突破大盘股策略 - 昨日在MA5下方 + 今日开盘突破MA5 → Hour1买入"""

    def __init__(self, min_market_cap: float = 700.0, buy_hour: int = 1,
                 min_turnover: float = 1.0, min_recent_drop: float = 5.0):
        """
        参数：
          min_market_cap: 最低流通市值(亿元)，默认700亿
          buy_hour: 买入hour（默认1）
          min_turnover: 最低换手率(%)，默认1.0%
          min_recent_drop: 近10日最少回撤幅度(%)，默认5.0%
        """
        self.min_market_cap = min_market_cap
        self.buy_hour = buy_hour
        self.min_turnover = min_turnover
        self.min_recent_drop = min_recent_drop
        # 内部状态：缓存前日数据用于MA5计算
        self._prev_closes_cache = {}  # {code: [recent_closes]}
        self._history_loaded = False
        self._db_path = None
        self._all_days = None
        self._all_days_idx = None

    def set_db_path(self, db_path: str):
        """设置数据库路径，用于查询历史MA5数据"""
        self._db_path = db_path

    def _load_all_days(self):
        """加载全部交易日列表"""
        if self._all_days is not None:
            return
        import sqlite3
        conn = sqlite3.connect(self._db_path)
        cur = conn.cursor()
        cur.execute("SELECT DISTINCT date FROM stock_kline ORDER BY date")
        self._all_days = [r[0] for r in cur.fetchall()]
        self._all_days_idx = {d: i for i, d in enumerate(self._all_days)}
        conn.close()

    def get_candidates(self, date: str, day_data: pd.DataFrame,
                       prev_data: pd.DataFrame, blacklist) -> List[dict]:
        """
        从prev_data（昨日）和day_data（今日）筛选MA5突破候选：
        - 需要prev 5日的close数据计算MA5_yesterday
        - 昨日close < MA5_yesterday
        - 今日open > MA5_yesterday
        - 流通市值 > 700亿
        - 非ST/非北交所/非一字板
        """
        if day_data is None or day_data.empty:
            return []
        if prev_data is None or prev_data.empty:
            return []
        if self._db_path is None:
            return []

        import sqlite3
        self._load_all_days()

        if date not in self._all_days_idx:
            return []
        today_idx = self._all_days_idx[date]
        if today_idx < 6:
            return []

        # 需要yesterday及之前5日的close来计算MA5
        # yesterday = all_days[today_idx - 1]
        lookback_start = max(0, today_idx - 6)
        lookback_days = self._all_days[lookback_start:today_idx]  # 不含today

        if len(lookback_days) < 5:
            return []

        yesterday = lookback_days[-1]

        # 批量查询lookback数据
        conn = sqlite3.connect(self._db_path)
        cur = conn.cursor()
        lb_placeholders = ','.join(['?'] * len(lookback_days))
        cur.execute(f"""
            SELECT code, date, close
            FROM stock_kline WHERE date IN ({lb_placeholders})
            ORDER BY code, date
        """, lookback_days)

        from collections import defaultdict
        history_data = defaultdict(list)
        for r in cur.fetchall():
            if r[2] is not None:
                history_data[r[0]].append((r[1], float(r[2])))
        conn.close()

        # 今日数据预处理
        df_today = day_data.copy()

        # ST过滤
        if 'isST' in df_today.columns:
            df_today = df_today[df_today['isST'].fillna(0).astype(int) != 1]
        st_mask = df_today['code_name'].str.upper().str.contains('ST', na=False)
        df_today = df_today[~st_mask]

        # 北交所过滤
        bj_mask = df_today['code'].str.lower().str.startswith('bj.', na=False)
        df_today = df_today[~bj_mask]

        if df_today.empty:
            return []

        # 确保数值列
        for col in ['open', 'preclose', 'high', 'low', 'close', 'amount', 'turn']:
            if col in df_today.columns:
                df_today[col] = pd.to_numeric(df_today[col], errors='coerce')

        # 基本过滤：open和preclose有效
        df_today = df_today[
            df_today['open'].notna() & (df_today['open'] > 0) &
            df_today['preclose'].notna() & (df_today['preclose'] > 0)
        ]

        if df_today.empty:
            return []

        # 构建today索引
        today_lookup = df_today.set_index('code').to_dict('index')

        # 昨日数据预处理
        df_prev = prev_data.copy()
        if 'isST' in df_prev.columns:
            df_prev = df_prev[df_prev['isST'].fillna(0).astype(int) != 1]
        prev_st = df_prev['code_name'].str.upper().str.contains('ST', na=False)
        df_prev = df_prev[~prev_st]
        prev_bj = df_prev['code'].str.lower().str.startswith('bj.', na=False)
        df_prev = df_prev[~prev_bj]

        for col in ['close', 'preclose', 'turn', 'amount']:
            if col in df_prev.columns:
                df_prev[col] = pd.to_numeric(df_prev[col], errors='coerce')

        prev_lookup = df_prev.set_index('code').to_dict('index')

        candidates = []

        for code, today_row in today_lookup.items():
            # 昨日数据
            prev_row = prev_lookup.get(code)
            if prev_row is None:
                continue

            yd_close = prev_row.get('close')
            if yd_close is None or yd_close <= 0:
                continue

            # 换手率过滤（用昨日换手率）
            yd_turn = prev_row.get('turn', 0) or 0
            if yd_turn < self.min_turnover:
                continue

            # 估算流通市值（亿元）: amount * 100 / turn / 1e8
            t_amount = today_row.get('amount', 0) or 0
            if t_amount <= 0 or yd_turn <= 0:
                continue
            market_cap = t_amount * 100 / yd_turn / 1e8

            # 市值过滤
            if market_cap < self.min_market_cap:
                continue

            # 获取历史close，计算MA5_yesterday
            hist = history_data.get(code, [])
            if len(hist) < 5:
                continue

            # 取最后5日close
            hist_sorted = sorted(hist, key=lambda x: x[0])
            last5 = [h[1] for h in hist_sorted[-5:]]
            if len(last5) < 5:
                continue
            ma5_yesterday = sum(last5) / 5.0

            # 条件: yesterday_close < MA5_yesterday
            if yd_close >= ma5_yesterday:
                continue

            # 今日open
            t_open = today_row.get('open', 0) or 0
            t_preclose = today_row.get('preclose', 0) or 0
            if t_open <= 0 or t_preclose <= 0:
                continue

            # 条件: today_open > MA5_yesterday
            if t_open <= ma5_yesterday:
                continue

            # 涨停开盘排除
            limit_up = _calc_limit_up(t_preclose, code)
            if t_open >= limit_up:
                continue

            # 一字板排除 (今日)
            t_high = today_row.get('high', 0) or 0
            t_low = today_row.get('low', 0) or 0
            t_close = today_row.get('close', 0) or 0
            if t_open > 0 and t_open == t_high == t_low == t_close:
                continue

            open_rate = (t_open / t_preclose - 1) * 100

            candidates.append({
                'code': code,
                'code_name': today_row.get('code_name', '') or '',
                'open_rate': open_rate,
                'ma5_yesterday': ma5_yesterday,
                'yd_close': yd_close,
                'market_cap': market_cap,
                'preclose': t_preclose,
                'open': t_open,
                'yd_turn': yd_turn,
            })

        # 按市值降序排序（优先买入大盘股）
        candidates.sort(key=lambda x: -x['market_cap'])
        return candidates

    def describe_candidate(self, cand: dict) -> str:
        """返回候选股的简要描述"""
        code = cand.get('code', '')
        name = cand.get('code_name', '') or ''
        cap = cand.get('market_cap', 0)
        open_rate = cand.get('open_rate', 0)
        ma5 = cand.get('ma5_yesterday', 0)
        return (f"{code} {name} | 市值:{cap:.0f}亿 | "
                f"开:{open_rate:+.2f}% | MA5:{ma5:.2f}")

    def should_buy(self, candidates: List[dict], date: str, hour: int,
                   hour_data: dict, portfolio, day_data: pd.DataFrame = None) -> Optional[dict]:
        """
        在buy_hour（默认hour1）检查并执行买入：
        - 仅在hour1执行
        - 使用hour1_open作为买入价
        - 一字板检测(O=H=L=C → skip)
        - 涨停检测：hour1_open >= limit_up → skip
        - 排除已持仓股
        """
        if hour != self.buy_hour:
            return None
        if not candidates:
            return None

        held_codes = portfolio.held_codes()

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

            # 一字板检测：O=H=L=C
            if h_open == h_close == h_high == h_low and h_open > 0:
                continue

            # 涨停检测：hour1_open >= limit_up_price
            preclose = cand.get('preclose', 0)
            if preclose > 0:
                limit_up = _calc_limit_up(preclose, code)
                if h_open >= limit_up:
                    continue

            # 买入价 = hour1_open
            return {
                'code': code,
                'code_name': cand['code_name'],
                'price': h_open,
                'open_rate': cand['open_rate'],
                'market_cap': cand['market_cap'],
                'ma5_yesterday': cand['ma5_yesterday'],
            }

        return None
