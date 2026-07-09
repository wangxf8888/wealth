"""
评分引擎 - 多指标加权评分组合器
支持两种模式:
1. 加权连续评分: 各指标normalize后加权求和
2. 星级二值评分: 每个指标达标=1星，不达标=0星，N星全亮才买入
"""
import math
from engine.indicators import Indicator, INDICATOR_REGISTRY, create_indicator


class ScoringEngine:
    """多指标评分引擎"""

    def __init__(self, indicator_configs: dict, threshold: float, mode: str = 'star'):
        """
        Args:
            indicator_configs: {
                'turnover_increase': {'weight': 2.0, 'lookback': 5, 'star_condition': '>2.0'},
                'above_ma5': {'weight': 1.5, 'star_condition': '==1'},
                'market_cap': {'weight': 1.0, 'min_cap': 5e9, 'star_condition': '>5e9'},
                ...
            }
            threshold: 评分阈值(加权模式)或最低星数(星级模式)
            mode: 'weighted'=加权连续评分, 'star'=星级二值评分
        """
        self.threshold = threshold
        self.mode = mode
        self.indicators = {}   # name -> Indicator instance
        self.weights = {}      # name -> weight
        self.star_conditions = {}  # name -> condition string

        for name, cfg in indicator_configs.items():
            weight = cfg.get('weight', 1.0)
            star_cond = cfg.get('star_condition', '>0')

            indicator_kwargs = {k: v for k, v in cfg.items()
                                if k not in ('weight', 'star_condition')}
            self.indicators[name] = create_indicator(name, **indicator_kwargs)
            self.weights[name] = weight
            self.star_conditions[name] = star_cond

    def _safe_value(self, value) -> float:
        """防御None/NaN，返回安全的float值"""
        if value is None:
            return 0.0
        if isinstance(value, float) and math.isnan(value):
            return 0.0
        return float(value)

    def _eval_star_condition(self, value: float, condition: str) -> bool:
        """评估星级条件: '>2.0', '>=1', '==1', '<70', 'between(30,70)'"""
        try:
            value = self._safe_value(value)
            if condition.startswith('between('):
                parts = condition[8:-1].split(',')
                lo, hi = float(parts[0]), float(parts[1])
                return lo <= value <= hi
            elif condition.startswith('>='):
                return value >= float(condition[2:])
            elif condition.startswith('>'):
                return value > float(condition[1:])
            elif condition.startswith('<='):
                return value <= float(condition[2:])
            elif condition.startswith('<'):
                return value < float(condition[1:])
            elif condition.startswith('=='):
                return abs(value - float(condition[2:])) < 0.001
            else:
                return value > float(condition)
        except Exception:
            return False

    def score_stock(self, row: dict, history: list = None) -> dict:
        """
        对单只股票计算评分
        Returns: {
            'total_score': float,   # 加权总分
            'star_count': int,      # 亮星数
            'star_total': int,      # 总星数
            'details': {name: {'value': x, 'star': bool, 'normalized': x, 'weighted': x}, ...}
        }
        """
        details = {}
        total_weighted = 0.0
        star_count = 0

        for name, indicator in self.indicators.items():
            value = self._safe_value(indicator.calculate(row, history))
            weight = self.weights[name]
            condition = self.star_conditions[name]

            star = self._eval_star_condition(value, condition)
            if star:
                star_count += 1

            normalized = indicator.normalize(value)
            normalized = self._safe_value(normalized)
            weighted = normalized * weight
            total_weighted += weighted

            details[name] = {
                'value': value,
                'star': star,
                'normalized': normalized,
                'weighted': weighted
            }

        return {
            'total_score': total_weighted,
            'star_count': star_count,
            'star_total': len(self.indicators),
            'details': details
        }

    def passes_threshold(self, score_result: dict) -> bool:
        """检查是否通过阈值"""
        if self.mode == 'star':
            return score_result['star_count'] >= self.threshold
        else:
            return score_result['total_score'] >= self.threshold

    def rank_candidates(self, stocks_data: list, history_map: dict = None) -> list:
        """
        对一批股票打分并排序
        Args:
            stocks_data: 当日所有股票数据列表 [{row_dict}, ...]
            history_map: {code: [history_rows]} 每只股票的历史数据
        Returns:
            通过阈值的候选列表，按得分降序，每个元素增加'score_info'字段
        """
        candidates = []

        for row in stocks_data:
            code = row.get('code', '')
            history = history_map.get(code, []) if history_map else None

            score_result = self.score_stock(row, history)

            if self.passes_threshold(score_result):
                row_copy = dict(row)
                row_copy['score_info'] = score_result
                row_copy['score'] = score_result['total_score']
                row_copy['star_count'] = score_result['star_count']
                candidates.append(row_copy)

        if self.mode == 'star':
            candidates.sort(key=lambda x: (x['star_count'], x['score']), reverse=True)
        else:
            candidates.sort(key=lambda x: x['score'], reverse=True)

        return candidates

    def describe_score(self, score_result: dict) -> str:
        """生成评分描述字符串（用于日志）"""
        stars = score_result['star_count']
        total = score_result['star_total']
        details = score_result['details']

        star_str = '\u2605' * stars + '\u2606' * (total - stars)
        parts = []
        for name, info in details.items():
            mark = '\u2713' if info['star'] else '\u2717'
            parts.append(f"{name}={info['value']:.2f}{mark}")

        return f"[{star_str} {stars}/{total}] " + ' | '.join(parts)
