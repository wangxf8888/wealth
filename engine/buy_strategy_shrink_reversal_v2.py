"""缩量反转策略V2 - 连续缩量下跌后放量高开"""
from typing import List, Optional
import pandas as pd
import sqlite3
from .buy_module import BuyModule


class ShrinkReversalV2Strategy(BuyModule):
    """
    策略逻辑：
    - 过去5日中≥3日收阴(close < open)
    - 近5日总成交量 < 此前5日总成交量 × 0.6 (缩量)
    - 期间累计跌幅 >= 5%
    - 今日高开≥3%
    - 创业板+科创板, 市值<200亿

    研究结论：830笔/+2.35%/52%胜率/7年全正
    """

    strategy_name = 'shrink_reversal'

    def __init__(self, buy_hour=1, gap_min=3.0,
                 max_mcap_yi=200.0, target_hold_days=5,
                 db_path='/home/AIWealth/data/stocks.db'):
        self.buy_hour = buy_hour
        self.gap_min = gap_min
        self.max_mcap_yi = max_mcap_yi
        self.target_hold_days = target_hold_days
        self.db_path = db_path
        self._history_cache = {}  # {code: [(date, open, close, volume, amount, turn), ...]}
        self._loaded_range = None

    def _ensure_history(self, current_date):
        """预加载历史数据(如果尚未加载, 按自然月缓存)"""
        if self._loaded_range and self._loaded_range == current_date[:7]:
            return
        # 加载前40天数据用于5日+5日回看
        conn = sqlite3.connect(self.db_path)
        # 只加载创业板+科创板
        query = """
            SELECT date, code, open, close, volume, amount, turn
            FROM stock_kline
            WHERE (code LIKE 'sz.30%' OR code LIKE 'sh.688%')
            AND date <= ? AND date >= date(?, '-40 days')
            ORDER BY code, date
        """
        rows = conn.execute(query, (current_date, current_date)).fetchall()
        conn.close()

        self._history_cache = {}
        for row in rows:
            date, code, op, cl, vol, amt, turn = row
            if code not in self._history_cache:
                self._history_cache[code] = []
            self._history_cache[code].append((date, op, cl, vol, amt, turn))
        self._loaded_range = current_date[:7]

    def _check_shrink_pattern(self, code, current_date):
        """检查某股是否满足5日缩量下跌模式"""
        history = self._history_cache.get(code, [])
        # 找到current_date之前的数据(不含today)
        hist = [h for h in history if h[0] < current_date]
        if len(hist) < 10:
            return False

        recent5 = hist[-5:]   # 最近5日
        prev5 = hist[-10:-5]  # 再前5日

        # 条件1: 5日中≥3日收阴 (close < open)
        down_days = sum(1 for d in recent5 if d[2] is not None and d[1] is not None and d[2] < d[1])
        if down_days < 3:
            return False

        # 条件2: 近5日总量 < 前5日总量 × 0.6
        vol_recent = sum(d[3] for d in recent5 if d[3])
        vol_prev = sum(d[3] for d in prev5 if d[3])
        if vol_prev <= 0 or vol_recent >= vol_prev * 0.6:
            return False

        # 条件3: 累计跌幅≥5% (从5日前的open到最近一日的close)
        if not recent5[0][1] or recent5[0][1] <= 0:  # open of 5 days ago
            return False
        cum_return = (recent5[-1][2] - recent5[0][1]) / recent5[0][1] * 100
        if cum_return > -5:
            return False

        return True

    def get_candidates(self, date: str, day_data: pd.DataFrame,
                       prev_data: pd.DataFrame, blacklist) -> List[dict]:
        """筛选满足缩量反转模式的候选"""
        if day_data is None or day_data.empty:
            return []

        self._ensure_history(date)

        # 板块过滤: 创业板+科创板
        df = day_data[
            day_data['code'].str.match(r'^(sz\.30[01]|sh\.688)')
        ].copy()
        if df.empty:
            return []

        # 高开过滤
        df['open_rate'] = pd.to_numeric(df['open_rate'], errors='coerce')
        df = df[df['open_rate'].notna() & (df['open_rate'] >= self.gap_min)]
        if df.empty:
            return []

        # 市值过滤 (流通市值 ≈ amount / (turn/100), 单位: 亿)
        df['turn'] = pd.to_numeric(df['turn'], errors='coerce')
        df['amount'] = pd.to_numeric(df['amount'], errors='coerce')
        df = df[(df['turn'] > 0) & df['amount'].notna()].copy()
        if df.empty:
            return []
        df['mcap'] = df['amount'] / (df['turn'] / 100) / 1e8
        df = df[df['mcap'] < self.max_mcap_yi]
        if df.empty:
            return []

        # ST过滤
        if blacklist:
            st_mask = df['code_name'].astype(str).str.upper().str.contains('ST', na=False)
            if 'isST' in df.columns:
                st_mask = st_mask | (df['isST'].fillna(0).astype(int) == 1)
            df = df[~st_mask]
        if df.empty:
            return []

        # 涨停不买 (创业板/科创板 20%)
        df['open'] = pd.to_numeric(df['open'], errors='coerce')
        df['preclose'] = pd.to_numeric(df['preclose'], errors='coerce')
        df = df[df['open'] < (df['preclose'] * 1.20).round(2)]
        if df.empty:
            return []

        candidates = []
        for _, row in df.iterrows():
            code = row['code']
            if self._check_shrink_pattern(code, date):
                candidates.append({
                    'code': code,
                    'code_name': row['code_name'] if pd.notna(row['code_name']) else '',
                    'open_rate': float(row['open_rate']),
                    'mcap': float(row['mcap']),
                    '_strategy': self.strategy_name,
                    '_priority': 4,
                    '_target_hold_days': self.target_hold_days,
                })

        # 按高开幅度排序(越高越强)
        candidates.sort(key=lambda x: -x['open_rate'])
        return candidates

    def describe_candidate(self, cand: dict) -> str:
        """简要描述用于日志"""
        code = cand.get('code', '')
        name = cand.get('code_name', '') or ''
        return (f"{code} {name} | 高开:{cand.get('open_rate', 0):+.2f}% | "
                f"市值:{cand.get('mcap', 0):.0f}亿 | 5日缩量反转")

    def should_buy(self, candidates, date, hour, hour_data, portfolio, day_data=None):
        if hour != self.buy_hour:
            return None
        my_cands = [c for c in candidates if c.get('_strategy') == self.strategy_name]
        if not my_cands:
            return None
        held = portfolio.held_codes() if portfolio else set()

        for cand in my_cands:
            if cand['code'] in held:
                continue
            code = cand['code']
            price = None
            if hour_data and code in hour_data:
                h = hour_data[code]
                price = h.get('hour1_open') or h.get('open')
            if price and price > 0:
                return {
                    'code': code,
                    'code_name': cand['code_name'],
                    'price': float(price),
                    'strategy_name': self.strategy_name,
                    'target_hold_days': self.target_hold_days,
                    'buy_reason': f"5日缩量跌, 高开{cand['open_rate']:.1f}%, 市值{cand['mcap']:.0f}亿",
                }
        return None
