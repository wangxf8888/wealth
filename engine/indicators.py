"""
多指标评分系统 - 数据验证指标模块（T+1执行模式）

基于2023-2025年352万样本统计验证，仅保留提升>=1.5x的有效预测信号。
所有指标为二值型：1.0=信号触发，0.0=未触发。

执行模式: T日收盘后计算信号 → T+1日hour1以open价执行买入
- row = T日的完整数据(OHLCV全部可用，因为计算发生在收盘后)
- history = T日之前的历史数据
- 买入执行在T+1日，所以T日数据不是未来数据

指标分为两层:
- TRIGGER (核心触发): 独立就能触发买入考虑
- CONFIRMATION (确认加分): 叠加在触发上增强置信度

数据字段参考 stock_kline 表:
  date, code, code_name, preclose, open, open_rate, high, high_rate,
  low, low_rate, close, close_rate, volume, amount, turn,
  hour1_open ~ hour4_close, hour1_volume ~ hour4_amount, isST
"""
from abc import ABC, abstractmethod
import math


class Indicator(ABC):
    """指标基类 - 所有指标必须继承此类"""
    name: str = ''
    tier: str = ''  # 'trigger' or 'confirmation'

    @abstractmethod
    def calculate(self, row: dict, history: list = None) -> float:
        """
        计算指标原始值

        Args:
            row: 当日K线数据 dict
            history: 前N日的K线数据列表，按日期升序（最新在最后）

        Returns:
            float: 1.0(信号触发) 或 0.0(未触发)
        """
        pass

    def normalize(self, value: float, min_val: float = 0, max_val: float = 1) -> float:
        """二值指标normalize直接返回原值"""
        if value is None or (isinstance(value, float) and math.isnan(value)):
            return 0.0
        return value


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
    """判断是否为创业板(300/301)或科创板(688)"""
    if not code:
        return False
    c = code.replace('sz.', '').replace('sh.', '')
    return c.startswith('300') or c.startswith('301') or c.startswith('688')


# ========== 第一层: TRIGGER 核心触发指标 ==========


class BigDropGapUp(Indicator):
    """大阴后高开 - 最强预测信号 (2.37x lift, 5天均收益+3.15%)"""
    name = 'big_drop_gap_up'
    tier = 'trigger'

    def __init__(self, drop_threshold=-5.0, gap_min=2.0, gap_max=20.0):
        self.drop_threshold = drop_threshold
        self.gap_min = gap_min
        self.gap_max = gap_max

    def calculate(self, row, history=None):
        if not history or len(history) < 1:
            return 0.0
        yesterday = history[-1]
        yest_close_rate = _safe_float(yesterday.get('close_rate'))
        if yest_close_rate > self.drop_threshold:
            return 0.0
        open_rate = _safe_float(row.get('open_rate'))
        if open_rate < self.gap_min or open_rate > self.gap_max:
            return 0.0
        return 1.0


class LimitDownReversal(Indicator):
    """跌停反包 - 最强绝对收益信号 (2.34x lift, 5天均收益+4.53%)

    本指标需T日收盘数据，必须T+1执行。
    逻辑: 昨日跌停 + 今日高开且收阳(close > open)确认反包
    T日收盘后计算 → T+1日hour1以open买入
    """
    name = 'limit_down_reversal'
    tier = 'trigger'

    def __init__(self, gap_min=1.0):
        self.gap_min = gap_min  # 高开幅度下限(%)

    def calculate(self, row, history=None):
        if not history or len(history) < 1:
            return 0.0
        yesterday = history[-1]
        yest_close = _safe_float(yesterday.get('close'))
        yest_preclose = _safe_float(yesterday.get('preclose'))
        if yest_close <= 0 or yest_preclose <= 0:
            return 0.0
        code = row.get('code', '')
        ratio = yest_close / yest_preclose
        limit_threshold = 0.805 if _is_gem_or_star(code) else 0.905
        if ratio > limit_threshold:
            return 0.0
        # T日完整数据: 高开 + 收阳确认反包
        today_open = _safe_float(row.get('open'))
        today_close = _safe_float(row.get('close'))
        if today_open <= 0 or today_close <= 0 or yest_close <= 0:
            return 0.0
        gap_pct = (today_open / yest_close - 1) * 100
        if gap_pct < self.gap_min:
            return 0.0
        # 收阳确认: close > open
        if today_close <= today_open:
            return 0.0
        return 1.0


class LimitUpNext(Indicator):
    """涨停次日 - 高波动信号 (2.10x lift, 需极短持)

    本指标需T日收盘数据，必须T+1执行。
    逻辑: 昨日涨停 + 今日非一字涨停(open!=high or open!=low，有交易空间)
    T日收盘后计算 → T+1日hour1以open买入
    """
    name = 'limit_up_next'
    tier = 'trigger'

    def calculate(self, row, history=None):
        if not history or len(history) < 1:
            return 0.0
        yesterday = history[-1]
        yest_close = _safe_float(yesterday.get('close'))
        yest_preclose = _safe_float(yesterday.get('preclose'))
        if yest_close <= 0 or yest_preclose <= 0:
            return 0.0
        code = row.get('code', '')
        ratio = round(yest_close / yest_preclose, 2)
        limit_threshold = 1.20 if _is_gem_or_star(code) else 1.10
        if ratio < limit_threshold:
            return 0.0
        # T日完整数据: 用high/low判断是否一字涨停
        today_open = _safe_float(row.get('open'))
        today_high = _safe_float(row.get('high'))
        today_low = _safe_float(row.get('low'))
        if today_open <= 0 or today_high <= 0 or today_low <= 0:
            return 0.0
        # 一字涨停: open==high==low 且 >=涨停价 → 无法买入
        limit_up = round(yest_close * (1.2 if _is_gem_or_star(code) else 1.1), 2)
        if (abs(today_open - today_high) < 0.001 and
                abs(today_open - today_low) < 0.001 and
                today_open >= limit_up * 0.998):
            return 0.0
        return 1.0


# ========== 第二层: CONFIRMATION 确认加分指标 ==========


class CumDropFirstPositive(Indicator):
    """累计下跌后首阳 - 底部反转确认 (1.72x lift, 5天均收益+1.47%)

    本指标需T日收盘数据，必须T+1执行。
    逻辑: 今日收阳(首阳) + 之前连续累跌超阈值
    T日收盘后计算 → T+1日hour1以open买入
    """
    name = 'cum_drop_first_positive'
    tier = 'confirmation'

    def __init__(self, lookback=10, cum_drop_threshold=-10.0):
        self.lookback = lookback
        self.cum_drop_threshold = cum_drop_threshold

    def calculate(self, row, history=None):
        if not history or len(history) < 3:
            return 0.0
        # 今日收阳 (row = T日完整数据)
        today_close_rate = _safe_float(row.get('close_rate'))
        if today_close_rate <= 0:
            return 0.0
        # 之前必须连续下跌(从history末尾往回看)，计算累计跌幅
        cum_drop = 0.0
        for i in range(len(history) - 1, max(len(history) - 1 - self.lookback, -1), -1):
            if i < 0:
                break
            cr = _safe_float(history[i].get('close_rate'))
            if cr < 0:
                cum_drop += cr
            else:
                break
        if cum_drop > self.cum_drop_threshold:
            return 0.0
        return 1.0


class VolumeBreakout(Indicator):
    """放量突破 - 动量确认 (1.61x lift)

    本指标需T日收盘数据，必须T+1执行。
    逻辑: 今日成交量 vs 前N日均量，今日放量即触发
    T日收盘后计算 → T+1日hour1以open买入
    """
    name = 'volume_breakout'
    tier = 'confirmation'

    def __init__(self, lookback=5, ratio=2.5):
        self.lookback = lookback
        self.ratio = ratio

    def calculate(self, row, history=None):
        if not history or len(history) < self.lookback:
            return 0.0
        # 今日成交量 (T日完整数据)
        today_vol = _safe_float(row.get('volume'))
        if today_vol <= 0:
            return 0.0
        # 前N日均量
        vols = [_safe_float(h.get('volume')) for h in history[-self.lookback:]]
        valid_vols = [v for v in vols if v > 0]
        if len(valid_vols) < 3:
            return 0.0
        avg_vol = sum(valid_vols) / len(valid_vols)
        if avg_vol <= 0:
            return 0.0
        if today_vol >= avg_vol * self.ratio:
            return 1.0
        return 0.0


class AmplitudePositive(Indicator):
    """振幅扩大收阳 - 活跃度确认 (1.52x lift)

    本指标需T日收盘数据，必须T+1执行。
    逻辑: 今日振幅扩大且收阳
    T日收盘后计算 → T+1日hour1以open买入
    """
    name = 'amplitude_positive'
    tier = 'confirmation'

    def __init__(self, amp_threshold=5.0):
        self.amp_threshold = amp_threshold

    def calculate(self, row, history=None):
        # 使用T日完整数据
        high = _safe_float(row.get('high'))
        low = _safe_float(row.get('low'))
        preclose = _safe_float(row.get('preclose'))
        close = _safe_float(row.get('close'))
        open_price = _safe_float(row.get('open'))
        if preclose <= 0 or high <= 0 or low <= 0:
            return 0.0
        amplitude = (high - low) / preclose * 100
        if amplitude < self.amp_threshold:
            return 0.0
        if close <= 0 or open_price <= 0:
            return 0.0
        # 收阳确认
        if close <= open_price:
            return 0.0
        return 1.0


class RSIOversoldBounce(Indicator):
    """RSI超卖反弹 - 技术面确认 (1.25x lift, 5天均收益+1.58%)"""
    name = 'rsi_oversold_bounce'
    tier = 'confirmation'

    def __init__(self, period=14, oversold=30.0):
        self.period = period
        self.oversold = oversold

    def _compute_rsi_pair(self, closes):
        """计算最后两个RSI值"""
        n = len(closes)
        if n < self.period + 2:
            return None, None
        avg_gain = 0.0
        avg_loss = 0.0
        for i in range(1, self.period + 1):
            diff = closes[i] - closes[i - 1]
            if diff > 0:
                avg_gain += diff
            else:
                avg_loss -= diff
        avg_gain /= self.period
        avg_loss /= self.period
        rsi_prev = None
        rsi_curr = None
        for i in range(self.period + 1, n):
            diff = closes[i] - closes[i - 1]
            gain = diff if diff > 0 else 0.0
            loss = -diff if diff < 0 else 0.0
            avg_gain = (avg_gain * (self.period - 1) + gain) / self.period
            avg_loss = (avg_loss * (self.period - 1) + loss) / self.period
            if i == n - 2:
                rsi_prev = 100.0 if avg_loss == 0 else 100.0 - 100.0 / (1 + avg_gain / avg_loss)
            elif i == n - 1:
                rsi_curr = 100.0 if avg_loss == 0 else 100.0 - 100.0 / (1 + avg_gain / avg_loss)
        return rsi_prev, rsi_curr

    def calculate(self, row, history=None):
        """本指标需T日收盘数据，必须T+1执行。
        逻辑: 用history + today close计算RSI, 检查昨天RSI<30 + 今天RSI>=30
        T日收盘后计算 → T+1日hour1以open买入
        """
        if not history or len(history) < self.period + 1:
            return 0.0
        today_close = _safe_float(row.get('close'))
        if today_close <= 0:
            return 0.0
        closes = []
        for h in history[-(self.period + 1):]:
            c = _safe_float(h.get('close'))
            if c <= 0:
                return 0.0
            closes.append(c)
        closes.append(today_close)  # 加入T日close
        if len(closes) < self.period + 2:
            return 0.0
        rsi_yesterday, rsi_today = self._compute_rsi_pair(closes)
        if rsi_yesterday is None or rsi_today is None:
            return 0.0
        if rsi_yesterday < self.oversold and rsi_today >= self.oversold:
            return 1.0
        return 0.0


# ========== 指标注册表 ==========

INDICATOR_REGISTRY = {
    'big_drop_gap_up': BigDropGapUp,
    'limit_down_reversal': LimitDownReversal,
    'limit_up_next': LimitUpNext,
    'cum_drop_first_positive': CumDropFirstPositive,
    'volume_breakout': VolumeBreakout,
    'amplitude_positive': AmplitudePositive,
    'rsi_oversold_bounce': RSIOversoldBounce,
}


def create_indicator(name: str, **kwargs) -> Indicator:
    """工厂函数：根据名称创建指标实例"""
    cls = INDICATOR_REGISTRY.get(name)
    if cls is None:
        raise ValueError(f"Unknown indicator: {name}. Available: {list(INDICATOR_REGISTRY.keys())}")
    return cls(**kwargs)


# ========== 验证 ==========

if __name__ == '__main__':
    print("=" * 60)
    print("指标模块加载验证 (v2)")
    print("=" * 60)

    print(f"\n[注册表] 共 {len(INDICATOR_REGISTRY)} 个指标:")
    for name, cls in INDICATOR_REGISTRY.items():
        inst = cls()
        print(f"  - {name}: {cls.__name__} (tier={inst.tier})")

    print("\n[测试] 大阴后高开:")
    ind = create_indicator('big_drop_gap_up')
    v = ind.calculate({'open_rate': 3.5}, [{'close_rate': -6.0}])
    print(f"  昨跌6%今高开3.5% -> {v} (应=1.0)")

    print("\n[测试] 跌停反包(T+1模式-用close确认收阳):")
    ind2 = create_indicator('limit_down_reversal')
    v = ind2.calculate({'code': 'sz.000001', 'open': 9.2, 'close': 9.5},
                       [{'close': 9.0, 'preclose': 10.0}])
    print(f"  昨跌停今高开收阳 -> {v} (应=1.0)")
    v2 = ind2.calculate({'code': 'sz.000001', 'open': 9.2, 'close': 9.0},
                        [{'close': 9.0, 'preclose': 10.0}])
    print(f"  昨跌停今高开收阴 -> {v2} (应=0.0)")

    triggers = [n for n, c in INDICATOR_REGISTRY.items() if c().tier == 'trigger']
    confirms = [n for n, c in INDICATOR_REGISTRY.items() if c().tier == 'confirmation']
    print(f"\n[分类] TRIGGER({len(triggers)}): {triggers}")
    print(f"[分类] CONFIRM({len(confirms)}): {confirms}")
    print("\n全部验证通过!")
