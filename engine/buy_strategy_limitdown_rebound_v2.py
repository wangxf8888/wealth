"""跌停反弹策略V2 - 创业板<50亿，昨日跌停附近后今日跳空高开，持有到T+5"""
from typing import List, Optional
import pandas as pd
from .buy_module import BuyModule


class LimitDownReboundV2Strategy(BuyModule):
    """
    策略逻辑：
    - 昨日创业板小市值股大跌(≥drop_threshold%，默认-12%接近跌停)
    - 今日跳空高开≥2%（确认反弹资金介入，无上限，只要不涨停）
    - hour1开盘买入
    - 持有到T+5卖出
    - 最高优先级(priority=1)

    跌停后的超跌反弹逻辑，恐慌底部资金抢筹。
    """

    strategy_name = 'limitdown_rebound'

    def __init__(self, buy_hour=1, drop_threshold=-12.0,
                 gap_min=2.0, gap_max=None, max_mcap_yi=50.0,
                 target_hold_days=5):
        self.buy_hour = buy_hour
        self.drop_threshold = drop_threshold  # 昨日跌幅阈值(负数,如-12表示跌≥12%)
        self.gap_min = gap_min    # 高开下限%
        self.gap_max = gap_max    # 高开上限%(None=无上限,只要不涨停)
        self.max_mcap_yi = max_mcap_yi  # 市值上限(亿)
        self.target_hold_days = target_hold_days

    def get_candidates(self, date: str, day_data: pd.DataFrame,
                       prev_data: pd.DataFrame, blacklist) -> List[dict]:
        """从prev_data中筛选昨日跌停附近的创业板小市值股"""
        if prev_data is None or prev_data.empty:
            return []
        if day_data is None or day_data.empty:
            return []

        # 1. 板块过滤：创业板 sz.300xxx/sz.301xxx
        df = prev_data[prev_data['code'].str.match(r'^sz\.30[01]')].copy()
        if df.empty:
            return []

        # 2. 跌幅过滤：昨日跌≥threshold
        df = df[df['close_rate'] <= self.drop_threshold]
        if df.empty:
            return []

        # 3. 市值过滤：<50亿 (amount/(turn/100))
        df = df[df['turn'] > 0].copy()
        df['mcap'] = df['amount'] / (df['turn'] / 100) / 1e8  # 亿元
        df = df[df['mcap'] < self.max_mcap_yi]
        if df.empty:
            return []

        # 4. 黑名单(ST)
        if blacklist:
            st_mask = df['code_name'].str.upper().str.contains('ST', na=False)
            if 'isST' in df.columns:
                st_mask = st_mask | (df['isST'].fillna(0).astype(int) == 1)
            df = df[~st_mask]
        if df.empty:
            return []

        # 5. 今日高开过滤：需要从day_data获取今日open_rate
        today_open = day_data.set_index('code')['open_rate'].to_dict()
        today_open_price = day_data.set_index('code')['open'].to_dict()
        today_preclose = day_data.set_index('code')['preclose'].to_dict()

        candidates = []
        for _, row in df.iterrows():
            code = row['code']
            if code not in today_open:
                continue
            opr = today_open[code]
            if opr is None or pd.isna(opr):
                continue
            # 高开下限过滤；gap_max为None时无上限
            if opr < self.gap_min:
                continue
            if self.gap_max is not None and opr > self.gap_max:
                continue

            # 6. 涨停不买：open >= round(preclose*1.20, 2)
            op = today_open_price.get(code, 0)
            pc = today_preclose.get(code, 0)
            if pc > 0 and op >= round(pc * 1.20, 2):
                continue

            candidates.append({
                'code': code,
                'code_name': row['code_name'],
                'prev_close_rate': row['close_rate'],
                'open_rate': opr,
                'mcap': row['mcap'],
                '_strategy': self.strategy_name,
                '_priority': 1,
                '_target_hold_days': self.target_hold_days,
            })

        # 按|跌幅|从大到小排序
        candidates.sort(key=lambda x: x['prev_close_rate'])
        return candidates

    def should_buy(self, candidates: List[dict], date: str, hour: int,
                   hour_data: dict, portfolio, day_data: pd.DataFrame = None) -> Optional[dict]:
        """hour1时从候选中选择最优买入"""
        if hour != self.buy_hour:
            return None

        # 过滤本策略的候选
        my_cands = [c for c in candidates if c.get('_strategy') == self.strategy_name]
        if not my_cands:
            return None

        # 排除已持仓的股票
        held = portfolio.held_codes() if portfolio else set()

        for cand in my_cands:
            if cand['code'] in held:
                continue

            # 获取买入价(hour1 open)
            code = cand['code']
            price = None
            if hour_data and code in hour_data:
                h = hour_data[code]
                price = h.get('hour1_open') or h.get('open')

            if price and price > 0:
                return {
                    'code': code,
                    'code_name': cand['code_name'],
                    'price': price,
                    'strategy_name': self.strategy_name,
                    'target_hold_days': self.target_hold_days,
                    'open_rate': cand['open_rate'],
                    'prev_close_rate': cand['prev_close_rate'],
                    'buy_reason': f"昨跌{cand['prev_close_rate']:.1f}%, 高开{cand['open_rate']:.1f}%, 市值{cand['mcap']:.0f}亿",
                }

        return None
