"""竞价高开+hour1量比放大 策略V2 (Task#100)

研究结论 (strategy_auction_gapup_vol.py, 2021-2026全周期, 买h1_open/卖T+1尾盘):
  创业板50-200亿, 昨日非涨停, 今日高开>=3%, 今日hour1量比>=5x
  => 胜率59.3%, 单笔+3.49%, n=985, 6年全正。

合规时序说明:
  研究中"买hour1_open + 用hour1_amount筛选"存在表面矛盾: hour1_amount要到10:00
  (hour1结束)才知道, 而hour1_open在9:30已确定。
  但回测引擎BacktestEngineV3在 hour==1 循环内(即hour1时段结束后)才调用should_buy,
  此时 hour_data[code] 中 'amount'=hour1_amount、'open'=hour1_open 均已就绪;
  day_data(SELECT *)本身也已含 hour1_amount 列。
  因此在 hour1 结束时点做量比筛选、并以 hour1_open 作为成交价, 与研究口径一致。
"""
from typing import List, Optional
import pandas as pd
from .buy_module import BuyModule


class AuctionGapUpVolV2Strategy(BuyModule):
    """竞价高开+hour1量比放大 (创业板)"""

    strategy_name = 'auction_gapup_vol'
    buy_hour = 1

    def __init__(self, gap_min: float = 3.0, vol_ratio: float = 5.0,
                 min_mcap_yi: float = 50.0, max_mcap_yi: float = 200.0,
                 buy_hour: int = 1, target_hold_days: int = 1):
        self.gap_min = gap_min                # 今日高开阈值(%)
        self.vol_ratio = vol_ratio            # hour1量比: 今日h1_amount >= 昨日h1_amount * vol_ratio
        self.min_mcap_yi = min_mcap_yi        # 流通市值下限(亿)
        self.max_mcap_yi = max_mcap_yi        # 流通市值上限(亿)
        self.buy_hour = buy_hour
        self.target_hold_days = target_hold_days

    @staticmethod
    def _limit_price(preclose: float) -> float:
        """创业板涨停价 = round(preclose*1.20, 2)"""
        return round(preclose * 1.20, 2)

    def get_candidates(self, date: str, day_data: pd.DataFrame,
                       prev_data: pd.DataFrame, blacklist) -> List[dict]:
        """
        筛选逻辑(向量化+prev查表):
        - 创业板 sz.300/sz.301
        - 今日高开 open_rate >= gap_min
        - 排ST(今日+昨日)、排今日开盘涨停
        - 流通市值(amount/(turn/100)) 在 [min_mcap_yi, max_mcap_yi]
        - 昨日非涨停 (需 prev_data)
        - 今日hour1量比 >= vol_ratio (今日hour1_amount / 昨日hour1_amount)
        """
        if day_data is None or day_data.empty or prev_data is None or prev_data.empty:
            return []
        if 'hour1_amount' not in day_data.columns:
            return []

        # 昨日数据查表 (close/preclose/hour1_amount/isST/code_name)
        prev = prev_data.set_index('code')
        prev_close = prev['close'].to_dict() if 'close' in prev.columns else {}
        prev_preclose = prev['preclose'].to_dict() if 'preclose' in prev.columns else {}
        prev_h1amt = prev['hour1_amount'].to_dict() if 'hour1_amount' in prev.columns else {}
        prev_isst = prev['isST'].to_dict() if 'isST' in prev.columns else {}
        prev_name = prev['code_name'].to_dict() if 'code_name' in prev.columns else {}

        # 创业板过滤
        df = day_data[day_data['code'].str.match(r'^sz\.30[01]', na=False)].copy()
        if df.empty:
            return []

        # 数值化
        for col in ('open_rate', 'open', 'preclose', 'amount', 'turn', 'hour1_amount'):
            df[col] = pd.to_numeric(df[col], errors='coerce')

        # 今日高开
        df = df[df['open_rate'].notna() & (df['open_rate'] >= self.gap_min)]
        if df.empty:
            return []

        # ST过滤(今日)
        st_mask = df['code_name'].astype(str).str.upper().str.contains('ST', na=False)
        if 'isST' in df.columns:
            st_mask = st_mask | (df['isST'].fillna(0).astype(int) == 1)
        df = df[~st_mask]
        if df.empty:
            return []

        # 有效性: open/preclose/turn/hour1_amount
        df = df[(df['open'] > 0) & (df['preclose'] > 0) &
                (df['turn'] > 0) & df['amount'].notna() &
                (df['hour1_amount'] > 0)]
        if df.empty:
            return []

        # 排今日开盘涨停 (open >= 涨停价-0.001)
        df = df[df['open'] < (df['preclose'] * 1.20).round(2) - 0.001]
        if df.empty:
            return []

        # 流通市值 50-200亿
        df['mcap'] = df['amount'] / (df['turn'] / 100) / 1e8
        df = df[(df['mcap'] >= self.min_mcap_yi) & (df['mcap'] <= self.max_mcap_yi)]
        if df.empty:
            return []

        candidates = []
        for _, row in df.iterrows():
            code = row['code']
            pc = prev_close.get(code)
            ppc = prev_preclose.get(code)
            ph1 = prev_h1amt.get(code)
            # 昨日数据必须齐全
            if pc is None or ppc is None or ph1 is None:
                continue
            if not (pc > 0 and ppc > 0 and ph1 > 0):
                continue
            # 昨日ST过滤
            if int(prev_isst.get(code, 0) or 0) == 1:
                continue
            pname = str(prev_name.get(code, '') or '')
            if 'ST' in pname.upper():
                continue
            # 昨日非涨停
            if pc >= self._limit_price(ppc) - 0.001:
                continue
            # 今日hour1量比 (用 day_data 的 hour1_amount, hour1结束后引擎才调用买入)
            today_h1 = float(row['hour1_amount'])
            vr = today_h1 / float(ph1)
            if vr < self.vol_ratio:
                continue

            candidates.append({
                'code': code,
                'code_name': row['code_name'] if pd.notna(row['code_name']) else '',
                'open_rate': float(row['open_rate']),
                'mcap': float(row['mcap']),
                'vol_ratio': vr,
                'prev_h1_amount': float(ph1),
                '_strategy': self.strategy_name,
                '_target_hold_days': self.target_hold_days,
            })

        # 量比越高越强, 优先买入
        candidates.sort(key=lambda x: -x['vol_ratio'])
        return candidates

    def describe_candidate(self, cand: dict) -> str:
        code = cand.get('code', '')
        name = cand.get('code_name', '') or ''
        return (f"{code} {name} | 高开:{cand.get('open_rate', 0):+.2f}% | "
                f"量比:{cand.get('vol_ratio', 0):.1f}x | 市值:{cand.get('mcap', 0):.0f}亿")

    def should_buy(self, candidates: List[dict], date: str, hour: int,
                   hour_data: dict, portfolio, day_data: pd.DataFrame = None) -> Optional[dict]:
        """hour==1(hour1结束时点)以hour1_open买入, 并用hour_data的hour1_amount复核量比。"""
        if hour != self.buy_hour:
            return None
        my_cands = [c for c in candidates if c.get('_strategy') == self.strategy_name]
        if not my_cands:
            return None
        held = portfolio.held_codes() if portfolio else set()

        for cand in my_cands:
            code = cand['code']
            if code in held:
                continue
            h = hour_data.get(code) if hour_data else None
            if not h:
                continue
            h1_open = h.get('open', 0)
            h1_amount = h.get('amount', 0)
            if not h1_open or h1_open <= 0:
                continue
            # 一字板封死(O=H=L=C)无法成交
            if h1_open == h.get('high', 0) == h.get('low', 0) == h.get('close', 0):
                continue
            # 用真实hour1_amount复核量比 (与get_candidates口径一致)
            prev_h1 = cand.get('prev_h1_amount', 0)
            if prev_h1 and prev_h1 > 0 and h1_amount and h1_amount > 0:
                if (h1_amount / prev_h1) < self.vol_ratio:
                    continue

            return {
                'code': code,
                'code_name': cand['code_name'],
                'price': float(h1_open),
                'strategy_name': self.strategy_name,
                'target_hold_days': self.target_hold_days,
                'buy_reason': (f"竞价高开{cand['open_rate']:.1f}%, hour1量比"
                               f"{cand['vol_ratio']:.1f}x, 市值{cand['mcap']:.0f}亿"),
                'open_rate': cand['open_rate'],
                'vol_ratio': cand['vol_ratio'],
                'mcap': cand['mcap'],
            }
        return None
