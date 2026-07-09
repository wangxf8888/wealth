"""四策略组合买入模块 - 聚合4个已验证策略，优先级调度"""
from typing import List, Optional
import pandas as pd
from .buy_module import BuyModule
from .buy_strategy_limitdown_rebound_v2 import LimitDownReboundV2Strategy
from .buy_strategy_bigdrop_gapup_v2 import BigDropGapUpV2Strategy
from .buy_strategy_shrink_reversal_v2 import ShrinkReversalV2Strategy
from .buy_strategy_dragon_pullback_v2 import DragonPullbackV2Strategy


class FourStrategyBuyer(BuyModule):
    """
    四策略组合(优先级按Task#86实际回测单笔收益重排):
    龙回头(1,+4.14%) > 缩量反转(2,+1.15%) > 跌停反弹(3,+0.78%) > 大阴高开_深跌(4,+0.46%) > 大阴高开_浅跌(5)
    原优先级(跌停反弹1>大阴深跌2)把最差策略排在最前, 在slot稀缺时挤占了最优的龙回头, 故按实际收益重排。
    """
    strategy_name = 'four_combined'
    buy_hour = 1

    def __init__(self):
        self.strategies = [
            (1, DragonPullbackV2Strategy(buy_hour=1, target_hold_days=2)),
            (2, ShrinkReversalV2Strategy(buy_hour=1, target_hold_days=5)),
            (3, LimitDownReboundV2Strategy(buy_hour=1, target_hold_days=5)),
            (4, BigDropGapUpV2Strategy(buy_hour=1, drop_threshold=-10.0, target_hold_days=2)),
            (5, BigDropGapUpV2Strategy(buy_hour=1, drop_threshold=-8.0, target_hold_days=2)),
        ]

    def get_candidates(self, date, day_data, prev_data, blacklist):
        result = []
        for priority, strat in self.strategies:
            try:
                cands = strat.get_candidates(date, day_data, prev_data, blacklist)
                for c in cands:
                    c['_priority'] = priority
                result.extend(cands)
            except Exception as e:
                sname = getattr(strat, 'strategy_name', strat.__class__.__name__)
                print(f"  [WARN] {sname} get_candidates error: {e}")
        return result

    def should_buy(self, candidates, date, hour, hour_data, portfolio, day_data=None):
        if hour != 1:
            return None
        # 按优先级排序
        sorted_cands = sorted(candidates, key=lambda x: x.get('_priority', 99))
        held = portfolio.held_codes() if portfolio else set()

        for cand in sorted_cands:
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
                    'code_name': cand.get('code_name', ''),
                    'price': float(price),
                    'strategy_name': cand.get('_strategy', 'unknown'),
                    'target_hold_days': cand.get('_target_hold_days', 2),
                    'buy_reason': self._build_reason(cand),
                }
        return None

    def _build_reason(self, cand) -> str:
        """由候选已有字段拼接可解释买入理由"""
        parts = [cand.get('_strategy', 'unknown')]
        if cand.get('prev_close_rate') is not None:
            parts.append(f"昨跌{cand['prev_close_rate']:.1f}%")
        if cand.get('open_rate') is not None:
            parts.append(f"高开{cand['open_rate']:.1f}%")
        if cand.get('mcap') is not None:
            parts.append(f"市值{cand['mcap']:.0f}亿")
        parts.append(f"优先级{cand.get('_priority', 0)}")
        return ', '.join(parts)
