"""龙回头策略V2 - 创业板200-700亿，近期新高后回调+跳空高开"""
from typing import List, Optional
import pandas as pd
import sqlite3
from .buy_module import BuyModule


class DragonPullbackV2Strategy(BuyModule):
    """
    策略逻辑：
    - 创业板200-700亿中盘股
    - 过去6-10日内某日创了20日新高(high >= max(past 20d highs))
    - 回调: 昨收 < 新高日收盘 × 0.95 或 近3日中≥2日收阴
    - 今日跳空高开≥5%
    - hour1买入, T+2卖出

    研究结论：76笔/+4.11%/61.8%胜率
    """

    strategy_name = 'dragon_pullback'

    def __init__(self, buy_hour=1, gap_min=5.0,
                 min_mcap_yi=200.0, max_mcap_yi=700.0,
                 target_hold_days=2, db_path='/home/AIWealth/data/stocks.db'):
        self.buy_hour = buy_hour
        self.gap_min = gap_min
        self.min_mcap_yi = min_mcap_yi
        self.max_mcap_yi = max_mcap_yi
        self.target_hold_days = target_hold_days
        self.db_path = db_path
        self._history_cache = {}  # {code: [(date, open, high, low, close, volume, amount, turn), ...]}
        self._loaded_range = None

    def _ensure_history(self, current_date):
        """预加载历史数据(如果尚未加载, 按自然月缓存)"""
        if self._loaded_range and self._loaded_range == current_date[:7]:
            return
        # 加载前50天数据: 检测20日新高需要20+10日回看
        conn = sqlite3.connect(self.db_path)
        # 只加载创业板
        query = """
            SELECT date, code, open, high, low, close, volume, amount, turn
            FROM stock_kline
            WHERE code LIKE 'sz.30%'
            AND date <= ? AND date >= date(?, '-50 days')
            ORDER BY code, date
        """
        rows = conn.execute(query, (current_date, current_date)).fetchall()
        conn.close()

        self._history_cache = {}
        for row in rows:
            date, code, op, hi, lo, cl, vol, amt, turn = row
            if code not in self._history_cache:
                self._history_cache[code] = []
            self._history_cache[code].append((date, op, hi, lo, cl, vol, amt, turn))
        self._loaded_range = current_date[:7]

    def _check_dragon_pattern(self, code, current_date):
        """检查某股是否满足龙回头模式:
        1. 6-10日前是否有某日创20日新高
        2. 回调条件(昨收<新高日close×0.95 或 近3日≥2阴)
        字段索引: 0=date 1=open 2=high 3=low 4=close 5=volume 6=amount 7=turn
        """
        history = self._history_cache.get(code, [])
        # 找到current_date之前的数据(不含today)
        hist = [h for h in history if h[0] < current_date]
        # 需要至少: 新高检测窗口(6~10日前)再往前20日 = 约30日
        if len(hist) < 30:
            return False

        n = len(hist)
        # 检查6-10日前(索引 n-10 ~ n-6)是否有某日high创20日新高
        high_day_close = None  # 记录新高日收盘价
        found_high = False
        # d_idx 表示该"新高日"在hist中的索引, 回看范围 6~10日前
        for offset in range(6, 11):
            d_idx = n - offset
            if d_idx < 20:
                continue
            day_high = hist[d_idx][2]
            if day_high is None or day_high <= 0:
                continue
            # 过去20日(不含当日)的最高high: 索引 d_idx-20 ~ d_idx-1
            past20_highs = [hist[j][2] for j in range(d_idx - 20, d_idx)
                            if hist[j][2] is not None]
            if len(past20_highs) < 20:
                continue
            if day_high >= max(past20_highs):
                found_high = True
                high_day_close = hist[d_idx][4]
                break

        if not found_high:
            return False

        # 回调条件
        yesterday_close = hist[-1][4]  # 昨收
        # 条件A: 昨收 < 新高日收盘 × 0.95
        cond_a = False
        if high_day_close and high_day_close > 0 and yesterday_close is not None:
            cond_a = yesterday_close < high_day_close * 0.95
        # 条件B: 近3日中≥2日收阴 (close < open)
        recent3 = hist[-3:]
        down_days = sum(1 for d in recent3
                        if d[4] is not None and d[1] is not None and d[4] < d[1])
        cond_b = down_days >= 2

        if not (cond_a or cond_b):
            return False

        return True

    def get_candidates(self, date: str, day_data: pd.DataFrame,
                       prev_data: pd.DataFrame, blacklist) -> List[dict]:
        """筛选满足龙回头模式的候选"""
        if day_data is None or day_data.empty:
            return []

        self._ensure_history(date)

        # 板块过滤: 仅创业板
        df = day_data[
            day_data['code'].str.match(r'^sz\.30[01]')
        ].copy()
        if df.empty:
            return []

        # 跳空高开过滤
        df['open_rate'] = pd.to_numeric(df['open_rate'], errors='coerce')
        df = df[df['open_rate'].notna() & (df['open_rate'] >= self.gap_min)]
        if df.empty:
            return []

        # 市值过滤 (流通市值 ≈ amount / (turn/100), 单位: 亿), 200~700亿中盘
        df['turn'] = pd.to_numeric(df['turn'], errors='coerce')
        df['amount'] = pd.to_numeric(df['amount'], errors='coerce')
        df = df[(df['turn'] > 0) & df['amount'].notna()].copy()
        if df.empty:
            return []
        df['mcap'] = df['amount'] / (df['turn'] / 100) / 1e8
        df = df[(df['mcap'] >= self.min_mcap_yi) & (df['mcap'] <= self.max_mcap_yi)]
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

        # 涨停不买 (创业板 20%)
        df['open'] = pd.to_numeric(df['open'], errors='coerce')
        df['preclose'] = pd.to_numeric(df['preclose'], errors='coerce')
        df = df[df['open'] < (df['preclose'] * 1.20).round(2)]
        if df.empty:
            return []

        candidates = []
        for _, row in df.iterrows():
            code = row['code']
            if self._check_dragon_pattern(code, date):
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
                f"市值:{cand.get('mcap', 0):.0f}亿 | 龙回头")

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
                    'buy_reason': f"20日新高回调, 高开{cand['open_rate']:.1f}%, 市值{cand['mcap']:.0f}亿",
                }
        return None
