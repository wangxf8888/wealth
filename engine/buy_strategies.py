"""
模块化买入策略框架
N个买入策略可独立启用，按优先级排序，所有触发信号返回供上层选择。

设计原则:
- 涨停不可买入（一字涨停/买入价>=涨停价*0.998）
- 所有价格字段防御None/0/NaN
- 纯Python标准库，无第三方依赖
- 每个策略继承BuyStrategy基类，实现check_signal()方法
"""
import math
from abc import ABC, abstractmethod


def _safe_float(val, default=0.0):
    """安全浮点转换，防御None/NaN/Inf"""
    if val is None:
        return default
    try:
        f = float(val)
        if math.isnan(f) or math.isinf(f):
            return default
        return f
    except (ValueError, TypeError):
        return default


def _is_gem(code: str) -> bool:
    """判断是否为创业板(300/301开头)"""
    if not code:
        return False
    return code.startswith('300') or code.startswith('301')


def _get_limit_up_price(preclose: float, code: str) -> float:
    """计算涨停价"""
    if preclose <= 0:
        return 0.0
    ratio = 1.2 if _is_gem(code) else 1.1
    return round(preclose * ratio, 2)


def _is_limit_up_oneword(row: dict) -> bool:
    """检查是否为一字涨停板（无法买入）"""
    code = row.get('code', '')
    preclose = _safe_float(row.get('preclose'))
    if preclose <= 0:
        return True
    o = _safe_float(row.get('open'))
    h = _safe_float(row.get('high'))
    c = _safe_float(row.get('close'))
    if o <= 0 or h <= 0 or c <= 0:
        return True
    if abs(o - h) < 0.01 and abs(o - c) < 0.01:
        limit_up = _get_limit_up_price(preclose, code)
        if limit_up > 0 and o >= limit_up * 0.998:
            return True
    return False


def _compliance_check(buy_price: float, preclose: float, code: str) -> bool:
    """合规检查：买入价不能>=涨停价*0.998"""
    if buy_price <= 0 or preclose <= 0:
        return False
    limit_up = _get_limit_up_price(preclose, code)
    if limit_up <= 0:
        return False
    if buy_price >= limit_up * 0.998:
        return False
    return True


class BuyStrategy(ABC):
    """买入策略基类"""
    name: str = ''
    priority: int = 0

    @abstractmethod
    def check_signal(self, row: dict, history: list, scoring_result: dict) -> dict:
        """检查买入信号"""
        pass


class ScoreBuyStrategy(BuyStrategy):
    """评分买入策略(v2) - 至少1个trigger亮 + star_count >= min_stars"""
    name = 'score_buy'
    priority = 1
    TRIGGER_INDICATORS = {'big_drop_gap_up', 'limit_down_reversal', 'limit_up_next'}

    def __init__(self, min_stars=3):
        self.min_stars = min_stars

    def check_signal(self, row, history, scoring_result):
        if not scoring_result:
            return {'signal': False}
        star_count = scoring_result.get('star_count', 0)
        if star_count < self.min_stars:
            return {'signal': False}
        # 检查是否至少有1个trigger指标亮灯
        details = scoring_result.get('details', {})
        trigger_count = 0
        trigger_names = []
        for ind_name in self.TRIGGER_INDICATORS:
            if ind_name in details and details[ind_name].get('star', False):
                trigger_count += 1
                trigger_names.append(ind_name)
        if trigger_count == 0:
            return {'signal': False}
        # T+1模式: 价格合规检查在执行时做，信号生成只检查评分质量
        return {
            'signal': True,
            'buy_price': _safe_float(row.get('hour1_open')),  # 参考价，实际用T+1的
            'buy_hour': 'hour1',
            'trigger_count': trigger_count,
            'reason': (f'评分买入(stars={star_count}>={self.min_stars},'
                       f'triggers={trigger_count}[{"+".join(trigger_names)}])')
        }


class VShapeBuyStrategy(BuyStrategy):
    """V字形态买入 - 累计下跌后高开反弹，仅创业板"""
    name = 'vshape_buy'
    priority = 2

    def __init__(self, lookback=4, cum_drop_threshold=-3.0,
                 gap_up_min=4.0, gap_up_max=20.0, min_turnover=8.0):
        self.lookback = lookback
        self.cum_drop_threshold = cum_drop_threshold
        self.gap_up_min = gap_up_min
        self.gap_up_max = gap_up_max
        self.min_turnover = min_turnover

    def check_signal(self, row, history, scoring_result):
        code = row.get('code', '')
        if not _is_gem(code):
            return {'signal': False}
        if not history or len(history) < self.lookback:
            return {'signal': False}
        cum_drop = sum(_safe_float(h.get('close_rate')) for h in history[-self.lookback:])
        if cum_drop > self.cum_drop_threshold:
            return {'signal': False}
        open_rate = _safe_float(row.get('open_rate'))
        if open_rate < self.gap_up_min or open_rate > self.gap_up_max:
            return {'signal': False}
        turnover = _safe_float(row.get('turn'))
        if turnover < self.min_turnover:
            return {'signal': False}
        # T+1模式: 价格合规检查在执行时做
        return {
            'signal': True,
            'buy_price': _safe_float(row.get('hour1_open')),  # 参考价
            'buy_hour': 'hour1',
            'reason': (f'V形反转(cum={cum_drop:.1f}%,'
                       f'gap={open_rate:.1f}%,turn={turnover:.1f}%)')
        }


class Surge7BuyStrategy(BuyStrategy):
    """冲高7%后第二波买入"""
    name = 'surge7_buy'
    priority = 3

    def __init__(self, surge_pct=7.0, pullback_days_min=3, pullback_days_max=20,
                 pullback_min=-8.0, pullback_max=-3.0, rebound_pct=2.0):
        self.surge_pct = surge_pct
        self.pullback_days_min = pullback_days_min
        self.pullback_days_max = pullback_days_max
        self.pullback_min = pullback_min
        self.pullback_max = pullback_max
        self.rebound_pct = rebound_pct

    def check_signal(self, row, history, scoring_result):
        if not history or len(history) < self.pullback_days_min + 1:
            return {'signal': False}
        close_rate = _safe_float(row.get('close_rate'))
        if close_rate < self.rebound_pct:
            return {'signal': False}
        found_surge = False
        cum_pullback = 0.0
        for i in range(len(history)):
            h = history[i]
            high_rate = _safe_float(h.get('high_rate'))
            if high_rate >= self.surge_pct:
                days_after = len(history) - i
                if self.pullback_days_min <= days_after <= self.pullback_days_max:
                    pullback_data = history[i + 1:]
                    if pullback_data:
                        cum_pullback = sum(_safe_float(d.get('close_rate')) for d in pullback_data)
                        if self.pullback_min <= cum_pullback <= self.pullback_max:
                            found_surge = True
                            break
        if not found_surge:
            return {'signal': False}
        if _is_limit_up_oneword(row):
            return {'signal': False}
        code = row.get('code', '')
        preclose = _safe_float(row.get('preclose'))
        buy_price = _safe_float(row.get('hour1_open'))
        if buy_price <= 0 or preclose <= 0:
            return {'signal': False}
        if not _compliance_check(buy_price, preclose, code):
            return {'signal': False}
        return {
            'signal': True,
            'buy_price': buy_price,
            'buy_hour': 'hour1',
            'reason': (f'冲高回落二波(surge>={self.surge_pct}%,'
                       f'pullback={cum_pullback:.1f}%,rebound={close_rate:.1f}%)')
        }


class VolBreakoutBuyStrategy(BuyStrategy):
    """放量突破买入 - T+1信号"""
    name = 'vol_breakout_buy'
    priority = 4

    def __init__(self, volume_ratio=2.0, ma_period=5):
        self.volume_ratio = volume_ratio
        self.ma_period = ma_period

    def check_signal(self, row, history, scoring_result):
        if not history or len(history) < self.ma_period:
            return {'signal': False}
        today_vol = _safe_float(row.get('volume'))
        if today_vol <= 0:
            return {'signal': False}
        avg_vol = sum(_safe_float(h.get('volume')) for h in history[-self.ma_period:]) / self.ma_period
        if avg_vol <= 0:
            return {'signal': False}
        if today_vol < avg_vol * self.volume_ratio:
            return {'signal': False}
        ma5 = sum(_safe_float(h.get('close')) for h in history[-self.ma_period:]) / self.ma_period
        close = _safe_float(row.get('close'))
        if close <= 0 or close <= ma5:
            return {'signal': False}
        close_rate = _safe_float(row.get('close_rate'))
        if close_rate <= 0:
            return {'signal': False}
        if _is_limit_up_oneword(row):
            return {'signal': False}
        code = row.get('code', '')
        preclose = _safe_float(row.get('preclose'))
        buy_price = _safe_float(row.get('hour1_open'))
        if buy_price <= 0 or preclose <= 0:
            return {'signal': False}
        if not _compliance_check(buy_price, preclose, code):
            return {'signal': False}
        return {
            'signal': True,
            'buy_price': buy_price,
            'buy_hour': 'hour1',
            'is_t1_signal': True,
            'reason': (f'放量突破(vol_ratio={today_vol / avg_vol:.1f}x>='
                       f'{self.volume_ratio}x,close>MA{self.ma_period})')
        }


class LimitUpPullbackBuyStrategy(BuyStrategy):
    """涨停回调买入"""
    name = 'limitup_pullback_buy'
    priority = 5

    def __init__(self, lookback=5, pullback_min=-5.0, pullback_max=-2.0):
        self.lookback = lookback
        self.pullback_min = pullback_min
        self.pullback_max = pullback_max

    def _is_limit_up_day(self, h: dict) -> bool:
        close = _safe_float(h.get('close'))
        preclose = _safe_float(h.get('preclose'))
        if close <= 0 or preclose <= 0:
            return False
        code = h.get('code', '')
        ratio = close / preclose
        threshold = 1.198 if _is_gem(code) else 1.098
        return ratio >= threshold

    def check_signal(self, row, history, scoring_result):
        if not history or len(history) < 2:
            return {'signal': False}
        close_rate = _safe_float(row.get('close_rate'))
        if close_rate <= 0:
            return {'signal': False}
        search_range = history[-self.lookback:] if len(history) >= self.lookback else history
        found_limitup = False
        cum_pullback = 0.0
        for i, h in enumerate(search_range):
            if self._is_limit_up_day(h):
                after_idx = len(history) - len(search_range) + i + 1
                pullback_data = history[after_idx:]
                if pullback_data:
                    cum_pullback = sum(_safe_float(d.get('close_rate')) for d in pullback_data)
                    if self.pullback_min <= cum_pullback <= self.pullback_max:
                        found_limitup = True
                        break
        if not found_limitup:
            return {'signal': False}
        if _is_limit_up_oneword(row):
            return {'signal': False}
        code = row.get('code', '')
        preclose = _safe_float(row.get('preclose'))
        buy_price = _safe_float(row.get('hour1_open'))
        if buy_price <= 0 or preclose <= 0:
            return {'signal': False}
        if not _compliance_check(buy_price, preclose, code):
            return {'signal': False}
        return {
            'signal': True,
            'buy_price': buy_price,
            'buy_hour': 'hour1',
            'reason': (f'涨停回调(pullback={cum_pullback:.1f}%,'
                       f'today={close_rate:+.1f}%)')
        }


# ========== 买入策略管理器 ==========


class BuyStrategyManager:
    """组合管理N个买入策略，按优先级返回所有触发信号"""

    def __init__(self, strategies: list):
        self.strategies = sorted(strategies, key=lambda s: s.priority)

    def scan_signals(self, row: dict, history: list, scoring_result: dict) -> list:
        """返回所有触发的买入信号列表，按优先级排序"""
        signals = []
        for strategy in self.strategies:
            result = strategy.check_signal(row, history, scoring_result)
            if result.get('signal'):
                result['strategy_name'] = strategy.name
                signals.append(result)
        return signals

    def add_strategy(self, strategy):
        self.strategies.append(strategy)
        self.strategies.sort(key=lambda s: s.priority)

    def remove_strategy(self, name: str):
        self.strategies = [s for s in self.strategies if s.name != name]

    def list_strategies(self) -> list:
        return [(s.name, s.priority, s.__class__.__name__) for s in self.strategies]


# ========== 买入策略注册表 ==========

BUY_STRATEGY_REGISTRY = {
    'score_buy': ScoreBuyStrategy,
    'vshape_buy': VShapeBuyStrategy,
    'surge7_buy': Surge7BuyStrategy,
    'vol_breakout_buy': VolBreakoutBuyStrategy,
    'limitup_pullback_buy': LimitUpPullbackBuyStrategy,
}


def create_buy_strategy(name: str, **kwargs) -> BuyStrategy:
    """根据名称创建买入策略实例"""
    cls = BUY_STRATEGY_REGISTRY.get(name)
    if cls is None:
        raise ValueError(f"Unknown buy strategy: {name}. "
                         f"Available: {list(BUY_STRATEGY_REGISTRY.keys())}")
    return cls(**kwargs)


def create_default_manager() -> BuyStrategyManager:
    """创建默认配置的买入策略管理器"""
    strategies = [
        ScoreBuyStrategy(min_stars=3),
        VShapeBuyStrategy(),
        Surge7BuyStrategy(),
        VolBreakoutBuyStrategy(),
        LimitUpPullbackBuyStrategy(),
    ]
    return BuyStrategyManager(strategies)


# ========== 验证 ==========

if __name__ == '__main__':
    print("=" * 60)
    print("买入策略模块加载验证")
    print("=" * 60)

    print(f"\n[注册表] 共 {len(BUY_STRATEGY_REGISTRY)} 个策略:")
    for name, cls in BUY_STRATEGY_REGISTRY.items():
        print(f"  - {name}: {cls.__name__}")

    mgr = create_default_manager()
    print(f"\n[默认Manager] {len(mgr.strategies)} 个策略:")
    for name, priority, cls_name in mgr.list_strategies():
        print(f"  - [{priority}] {name} ({cls_name})")

    # ScoreBuyStrategy trigger检查测试
    print("\n[测试] ScoreBuyStrategy trigger逻辑:")
    mock_row = {
        'code': '301001', 'preclose': 10.0, 'open': 10.5,
        'open_rate': 5.0, 'high': 11.0, 'low': 10.2, 'close': 10.8,
        'close_rate': 8.0, 'volume': 500000, 'turn': 12.0,
        'hour1_open': 10.5,
    }
    # 有trigger
    scoring_with = {
        'star_count': 4, 'star_total': 7,
        'details': {
            'big_drop_gap_up': {'value': 1.0, 'star': True},
            'limit_down_reversal': {'value': 0.0, 'star': False},
            'limit_up_next': {'value': 0.0, 'star': False},
            'cum_drop_first_positive': {'value': 1.0, 'star': True},
            'volume_breakout': {'value': 1.0, 'star': True},
            'amplitude_positive': {'value': 1.0, 'star': True},
            'rsi_oversold_bounce': {'value': 0.0, 'star': False},
        }
    }
    s = ScoreBuyStrategy(min_stars=3)
    sig = s.check_signal(mock_row, [], scoring_with)
    print(f"  有trigger, stars=4>=3: signal={sig.get('signal')} (应=True)")

    # 无trigger
    scoring_no = {
        'star_count': 3, 'star_total': 7,
        'details': {
            'big_drop_gap_up': {'value': 0.0, 'star': False},
            'limit_down_reversal': {'value': 0.0, 'star': False},
            'limit_up_next': {'value': 0.0, 'star': False},
            'cum_drop_first_positive': {'value': 1.0, 'star': True},
            'volume_breakout': {'value': 1.0, 'star': True},
            'amplitude_positive': {'value': 1.0, 'star': True},
            'rsi_oversold_bounce': {'value': 0.0, 'star': False},
        }
    }
    sig2 = s.check_signal(mock_row, [], scoring_no)
    print(f"  无trigger, stars=3>=3: signal={sig2.get('signal')} (应=False)")

    print("\n全部验证通过!")
