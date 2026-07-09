"""
模块化卖出策略框架
M个卖出策略可独立启用，任一触发则执行卖出。

设计原则:
- 止损优先于止盈（同一hour先检查止损）
- 跌停一字板无法卖出（自动跳过）
- 所有价格计算防御None/0/NaN
- 纯Python标准库，无第三方依赖
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


def _is_gem_or_star(code: str) -> bool:
    """判断创业板(300/301)或科创板(688)"""
    if not code:
        return False
    c = code.replace('sz.', '').replace('sh.', '')
    return c.startswith('300') or c.startswith('301') or c.startswith('688')


def _is_bse(code: str) -> bool:
    """北交所判断"""
    if not code:
        return False
    c = code.replace('bj.', '').replace('sh.', '').replace('sz.', '')
    return c.startswith('43') or c.startswith('83') or c.startswith('87')


def _get_limit_ratio(code: str) -> float:
    """获取涨跌停比例: 主板10%, 创业板/科创板20%, 北交所30%"""
    if _is_bse(code):
        return 0.3
    if _is_gem_or_star(code):
        return 0.2
    return 0.1


def _get_limit_down_ratio(code: str) -> float:
    """获取跌停比例(向后兼容)"""
    return _get_limit_ratio(code)


def _is_limit_down_oneword(hour_data: dict, preclose: float = 0, code: str = '') -> bool:
    """检查当前hour是否为跌停一字板（无法卖出）

    判定条件: open=high=low=close 且价格<=跌停价
    """
    h_open = _safe_float(hour_data.get('open'))
    h_high = _safe_float(hour_data.get('high'))
    h_low = _safe_float(hour_data.get('low'))
    h_close = _safe_float(hour_data.get('close'))

    if h_open <= 0 or h_high <= 0 or h_low <= 0 or h_close <= 0:
        return True  # 数据缺失视为不可交易

    # 四价相等判断
    prices = [h_open, h_high, h_low, h_close]
    if (max(prices) - min(prices)) >= 0.001:
        return False  # 非一字板

    # 有preclose时精确判断是否在跌停价位
    if preclose > 0:
        ratio = _get_limit_ratio(code)
        limit_down_price = round(preclose * (1 - ratio), 2)
        if h_open <= limit_down_price:
            return True  # 一字跌停
        return False  # 一字涨停或一字平开，可以卖出

    # 无preclose信息时，保守处理（不阻止卖出）
    return False


class SellStrategy(ABC):
    """卖出策略基类

    所有卖出策略必须继承此类并实现should_sell方法。
    策略通过SellStrategyManager组合使用，任一策略触发即执行卖出。
    """
    name: str = ''
    priority: int = 0  # 优先级，数值越小优先级越高

    @abstractmethod
    def should_sell(self, position: dict, hour: int, hour_data: dict,
                    day_data: dict = None) -> tuple:
        """
        检查是否应该卖出

        Args:
            position: 持仓信息 {
                'code': str,         # 股票代码
                'buy_price': float,  # 买入价格
                'shares': int,       # 持仓股数
                'buy_date': str,     # 买入日期
                'hold_days': int,    # 已持有交易日数
                'max_price': float,  # 持仓期间最高价
                'strategy': str,     # 买入来源策略名称
            }
            hour: 当前hour (1-4)
            hour_data: 当前hour的OHLC {
                'open': float, 'high': float, 'low': float, 'close': float
            }
            day_data: 当日完整数据(可选) {
                'volume': float, 'prev_volume': float, 'close_rate': float, ...
            }

        Returns:
            (should_sell: bool, sell_price: float, reason: str)
            - should_sell: 是否触发卖出
            - sell_price: 实际卖出价格(需在当hour振幅内)
            - reason: 卖出原因描述
        """
        pass


class FixedTPSL(SellStrategy):
    """固定止盈止损策略

    以买入价为基准，设定固定百分比的止盈和止损线。
    - 止损: 当hour的low触及止损线时触发
    - 止盈: 当hour的high触及止盈线时触发
    - 同一hour内，止损优先于止盈
    """
    name = 'fixed_tpsl'
    priority = 1  # 最高优先级

    def __init__(self, tp_pct=10.0, sl_pct=-3.0):
        """
        Args:
            tp_pct: 止盈百分比（正数），如10.0表示涨10%止盈
            sl_pct: 止损百分比（负数），如-3.0表示跌3%止损
        """
        self.tp_pct = tp_pct
        self.sl_pct = sl_pct

    def should_sell(self, position, hour, hour_data, day_data=None):
        buy_price = _safe_float(position.get('buy_price'))
        if buy_price <= 0:
            return (False, 0, '')

        tp_price = buy_price * (1 + self.tp_pct / 100)
        sl_price = buy_price * (1 + self.sl_pct / 100)

        h_high = _safe_float(hour_data.get('high'))
        h_low = _safe_float(hour_data.get('low'))
        h_open = _safe_float(hour_data.get('open'))

        if h_low <= 0 or h_high <= 0 or h_open <= 0:
            return (False, 0, '')

        # 先检查止损（用low触发，止损优先）
        if h_low <= sl_price:
            # 跳空低开已低于止损价: 用open成交
            if h_open <= sl_price:
                actual_price = h_open
            else:
                actual_price = sl_price
            pnl = (actual_price / buy_price - 1) * 100
            return (True, actual_price, f'SL({pnl:+.1f}%/{self.sl_pct}%)')

        # 再检查止盈（用high触发）
        if h_high >= tp_price:
            # 跳空高开已高于止盈价: 用open成交
            if h_open >= tp_price:
                actual_price = h_open
            else:
                actual_price = tp_price
            pnl = (actual_price / buy_price - 1) * 100
            return (True, actual_price, f'TP({pnl:+.1f}%/{self.tp_pct}%)')

        return (False, 0, '')


class TrailingStop(SellStrategy):
    """移动止损策略（追踪止损）

    从持仓期间最高价回撤N%则触发卖出。
    随着价格上涨，止损线会自动上移，锁定利润。
    """
    name = 'trailing_stop'
    priority = 2

    def __init__(self, trail_pct=5.0):
        """
        Args:
            trail_pct: 回撤止损百分比，如5.0表示从高点回撤5%止损
        """
        self.trail_pct = trail_pct

    def should_sell(self, position, hour, hour_data, day_data=None):
        buy_price = _safe_float(position.get('buy_price'))
        if buy_price <= 0:
            return (False, 0, '')

        max_price = _safe_float(position.get('max_price', buy_price))
        if max_price <= 0:
            max_price = buy_price
        trail_price = max_price * (1 - self.trail_pct / 100)

        h_low = _safe_float(hour_data.get('low'))
        h_open = _safe_float(hour_data.get('open'))
        h_high = _safe_float(hour_data.get('high'))

        if h_low <= 0 or h_open <= 0:
            return (False, 0, '')

        # 检查是否触发回撤止损
        if h_low <= trail_price:
            if h_open <= trail_price:
                actual_price = h_open
            else:
                actual_price = trail_price
            pnl = (actual_price / buy_price - 1) * 100
            return (True, actual_price,
                    f'Trail({self.trail_pct}%从高点{max_price:.2f},pnl={pnl:+.1f}%)')

        # 未触发止损，更新max_price
        if h_high > 0 and h_high > max_price:
            position['max_price'] = h_high

        return (False, 0, '')


class TimeLimitExit(SellStrategy):
    """到期强平策略

    持仓达到最大天数后，在指定hour以close价强制平仓。
    适用于短线策略控制最大持仓时间。
    """
    name = 'time_limit'
    priority = 10  # 低优先级，作为兜底

    def __init__(self, max_hold_days=5, exit_hour=4):
        """
        Args:
            max_hold_days: 最大持有天数
            exit_hour: 强制平仓的hour (1-4)，默认hour4收盘平仓
        """
        self.max_hold_days = max_hold_days
        self.exit_hour = exit_hour

    def should_sell(self, position, hour, hour_data, day_data=None):
        if hour != self.exit_hour:
            return (False, 0, '')

        hold_days = position.get('hold_days', 0)
        if hold_days < self.max_hold_days:
            return (False, 0, '')

        close = _safe_float(hour_data.get('close'))
        if close <= 0:
            return (False, 0, '')

        buy_price = _safe_float(position.get('buy_price'))
        pnl = (close / buy_price - 1) * 100 if buy_price > 0 else 0
        return (True, close, f'到期({hold_days}d>={self.max_hold_days}d,pnl={pnl:+.1f}%)')


class VolumeShrinkExit(SellStrategy):
    """缩量跌破策略

    当日成交量相比前日大幅萎缩，同时价格下跌时触发卖出。
    反映市场资金撤离，主力不再维护。
    """
    name = 'volume_shrink'
    priority = 5

    def __init__(self, vol_shrink_ratio=0.5, price_drop_pct=-1.0, check_hour=3):
        """
        Args:
            vol_shrink_ratio: 缩量比例阈值，如0.5表示今日量<昨日50%
            price_drop_pct: 价格跌幅阈值(负数)，如-1.0表示跌1%以上
            check_hour: 检查时段(1-4)，默认hour3(有足够成交数据)
        """
        self.vol_shrink_ratio = vol_shrink_ratio
        self.price_drop_pct = price_drop_pct
        self.check_hour = check_hour

    def should_sell(self, position, hour, hour_data, day_data=None):
        if hour != self.check_hour or day_data is None:
            return (False, 0, '')

        today_vol = _safe_float(day_data.get('volume'))
        prev_vol = _safe_float(day_data.get('prev_volume'))
        close_rate = _safe_float(day_data.get('close_rate'))

        if prev_vol <= 0 or today_vol <= 0:
            return (False, 0, '')

        vol_ratio = today_vol / prev_vol
        if vol_ratio < self.vol_shrink_ratio and close_rate < self.price_drop_pct:
            close = _safe_float(hour_data.get('close'))
            if close <= 0:
                return (False, 0, '')
            buy_price = _safe_float(position.get('buy_price'))
            pnl = (close / buy_price - 1) * 100 if buy_price > 0 else 0
            return (True, close,
                    f'缩量下跌(vol={vol_ratio:.0%},cr={close_rate:+.1f}%,pnl={pnl:+.1f}%)')

        return (False, 0, '')


class ScoreDecayExit(SellStrategy):
    """评分衰减策略

    持仓股的实时评分（如技术面/基本面综合得分）降低到阈值以下时卖出。
    适用于评分驱动型选股策略的动态持仓管理。
    """
    name = 'score_decay'
    priority = 6

    def __init__(self, min_score=2.0, check_hour=1):
        """
        Args:
            min_score: 最低持仓评分阈值
            check_hour: 评分检查时段(1-4)
        """
        self.min_score = min_score
        self.check_hour = check_hour

    def should_sell(self, position, hour, hour_data, day_data=None):
        if hour != self.check_hour:
            return (False, 0, '')

        current_score = _safe_float(position.get('current_score'), default=999.0)
        if current_score >= self.min_score:
            return (False, 0, '')

        # 评分低于阈值，卖出
        price = _safe_float(hour_data.get('open'))
        if price <= 0:
            price = _safe_float(hour_data.get('close'))
        if price <= 0:
            return (False, 0, '')

        buy_price = _safe_float(position.get('buy_price'))
        pnl = (price / buy_price - 1) * 100 if buy_price > 0 else 0
        return (True, price,
                f'评分衰减({current_score:.1f}<{self.min_score},pnl={pnl:+.1f}%)')


# ========== 卖出策略管理器 ==========

class SellStrategyManager:
    """组合管理M个卖出策略，任一触发则执行

    特性:
    - 策略按priority排序，数值越小优先级越高
    - 止损类策略应设置更高优先级（更小priority值）
    - 自动跳过跌停一字板（无法卖出的情况）
    - 第一个触发的策略决定卖出价和原因
    """

    def __init__(self, strategies: list):
        """
        Args:
            strategies: SellStrategy实例列表，将按priority排序
        """
        # 按优先级排序（数值小的在前）
        self.strategies = sorted(strategies, key=lambda s: s.priority)

    def check_position(self, position: dict, hour: int, hour_data: dict,
                       day_data: dict = None, preclose: float = 0,
                       code: str = '') -> tuple:
        """
        对单个持仓检查所有卖出策略

        Args:
            position: 持仓信息
            hour: 当前hour (1-4)
            hour_data: 当前hour的OHLC数据
            day_data: 当日完整数据(可选)
            preclose: 前收盘价(用于跌停判定)
            code: 股票代码(用于跌停比例判定)

        Returns:
            (should_sell: bool, sell_price: float, reason: str)
            如果多个策略同时触发，取优先级最高的（列表顺序最前的）
        """
        # 跌停一字板检查：无法卖出
        if _is_limit_down_oneword(hour_data, preclose=preclose, code=code):
            return (False, 0, '')

        for strategy in self.strategies:
            should_sell, price, reason = strategy.should_sell(
                position, hour, hour_data, day_data)
            if should_sell and price > 0:
                return (True, price, f'{strategy.name}:{reason}')
        return (False, 0, '')

    def check_all_positions(self, positions: list, hour: int,
                            hour_data_map: dict,
                            day_data_map: dict = None,
                            preclose_map: dict = None) -> list:
        """
        批量检查所有持仓

        Args:
            positions: 持仓列表 [position_dict, ...]
            hour: 当前hour (1-4)
            hour_data_map: {code: {'open': ..., 'high': ..., 'low': ..., 'close': ...}}
            day_data_map: {code: {day_data}} (可选)
            preclose_map: {code: preclose_float} (可选)

        Returns:
            [(position, sell_price, reason), ...] 需要卖出的列表
        """
        sells = []
        for pos in positions:
            code = pos.get('code', '')
            hd = hour_data_map.get(code, {})
            dd = day_data_map.get(code, {}) if day_data_map else None
            pc = preclose_map.get(code, 0) if preclose_map else 0
            should_sell, price, reason = self.check_position(
                pos, hour, hd, dd, preclose=pc, code=code)
            if should_sell:
                sells.append((pos, price, reason))
        return sells

    def add_strategy(self, strategy):
        """动态添加策略并重新排序"""
        self.strategies.append(strategy)
        self.strategies.sort(key=lambda s: s.priority)

    def remove_strategy(self, name: str):
        """按名称移除策略"""
        self.strategies = [s for s in self.strategies if s.name != name]

    def list_strategies(self) -> list:
        """列出所有已注册策略"""
        return [(s.name, s.priority, s.__class__.__name__) for s in self.strategies]


# ========== 卖出策略注册表 ==========

SELL_STRATEGY_REGISTRY = {
    'fixed_tpsl': FixedTPSL,
    'trailing_stop': TrailingStop,
    'time_limit': TimeLimitExit,
    'volume_shrink': VolumeShrinkExit,
    'score_decay': ScoreDecayExit,
}


def create_sell_strategy(name: str, **kwargs):
    """根据名称创建卖出策略实例

    Args:
        name: 策略名称（见SELL_STRATEGY_REGISTRY）
        **kwargs: 策略构造参数

    Returns:
        SellStrategy实例

    Raises:
        ValueError: 未知策略名称
    """
    cls = SELL_STRATEGY_REGISTRY.get(name)
    if cls is None:
        raise ValueError(f"Unknown sell strategy: {name}. "
                         f"Available: {list(SELL_STRATEGY_REGISTRY.keys())}")
    return cls(**kwargs)


def create_default_manager(tp_pct=10.0, sl_pct=-3.0, trail_pct=5.0,
                           max_hold_days=5):
    """创建默认配置的卖出策略管理器

    包含: 固定止盈止损 + 移动止损 + 到期强平

    Args:
        tp_pct: 止盈百分比
        sl_pct: 止损百分比(负数)
        trail_pct: 移动止损回撤百分比
        max_hold_days: 最大持有天数

    Returns:
        配置好的SellStrategyManager
    """
    strategies = [
        FixedTPSL(tp_pct=tp_pct, sl_pct=sl_pct),
        TrailingStop(trail_pct=trail_pct),
        TimeLimitExit(max_hold_days=max_hold_days),
    ]
    return SellStrategyManager(strategies)
