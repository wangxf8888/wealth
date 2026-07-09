"""
持仓管理策略模块
决定如何分配资金到各个信号（仓位大小）
"""
from abc import ABC, abstractmethod


class PositionStrategy(ABC):
    """持仓分配策略基类"""
    name: str = ''
    
    @abstractmethod
    def allocate(self, portfolio_info: dict, signals: list) -> list:
        """
        决定买入分配
        Args:
            portfolio_info: {
                'total_equity': float,  # 总净值
                'cash': float,          # 可用现金
                'n_slots': int,         # 总仓位数
                'used_slots': int,      # 已用仓位数
                'slot_size': float,     # 单仓金额(total_equity/n_slots)
            }
            signals: 候选信号列表 [{
                'code': str, 'price': float, 'score': float, ...
            }]
        Returns:
            [(signal, amount_to_buy), ...] — amount是金额（元）
        """
        pass


class EqualWeight(PositionStrategy):
    """等权分配 - 每仓=净值/N，均匀分配"""
    name = 'equal_weight'
    
    def allocate(self, portfolio_info, signals):
        available = portfolio_info['n_slots'] - portfolio_info['used_slots']
        slot_size = portfolio_info['slot_size']
        cash = portfolio_info['cash']
        
        result = []
        for sig in signals[:available]:
            if cash < slot_size * 0.95:
                break
            result.append((sig, slot_size))
            cash -= slot_size
        return result


class ScoreProportional(PositionStrategy):
    """评分比例分配 - 高分股多分配资金"""
    name = 'score_proportional'
    
    def __init__(self, max_ratio=2.0):
        self.max_ratio = max_ratio  # 最高分股最多分配基础仓位的N倍
    
    def allocate(self, portfolio_info, signals):
        available = portfolio_info['n_slots'] - portfolio_info['used_slots']
        slot_size = portfolio_info['slot_size']
        cash = portfolio_info['cash']
        
        if not signals:
            return []
        
        # 按评分计算权重
        scores = [max(s.get('score', 1), 0.1) for s in signals[:available]]
        max_score = max(scores)
        min_score = min(scores)
        
        result = []
        for sig, score in zip(signals[:available], scores):
            if cash < slot_size * 0.5:
                break
            # 评分越高，分配比例越大
            if max_score > min_score:
                ratio = 1.0 + (score - min_score) / (max_score - min_score) * (self.max_ratio - 1)
            else:
                ratio = 1.0
            amount = min(slot_size * ratio, cash)
            result.append((sig, amount))
            cash -= amount
        return result


class RiskParity(PositionStrategy):
    """风险平价 - 波动大的股票少买"""
    name = 'risk_parity'
    
    def allocate(self, portfolio_info, signals):
        available = portfolio_info['n_slots'] - portfolio_info['used_slots']
        slot_size = portfolio_info['slot_size']
        cash = portfolio_info['cash']
        
        result = []
        for sig in signals[:available]:
            if cash < slot_size * 0.5:
                break
            # 用振幅作为波动率代理
            amplitude = sig.get('amplitude', 5.0) or 5.0
            # 波动越大，分配越少（反比）
            ratio = min(1.5, max(0.5, 5.0 / amplitude))
            amount = min(slot_size * ratio, cash)
            result.append((sig, amount))
            cash -= amount
        return result


class ConcentratedTop(PositionStrategy):
    """集中投注 - 只买评分最高的1-2只，重仓"""
    name = 'concentrated_top'
    
    def __init__(self, max_picks=2):
        self.max_picks = max_picks
    
    def allocate(self, portfolio_info, signals):
        available = min(portfolio_info['n_slots'] - portfolio_info['used_slots'], self.max_picks)
        cash = portfolio_info['cash']
        
        if not signals or available <= 0:
            return []
        
        # 集中分配：平分可用现金给Top N
        picks = signals[:available]
        amount_each = cash / len(picks)
        
        return [(sig, amount_each) for sig in picks]


# ========== 注册表 ==========

POSITION_STRATEGY_REGISTRY = {
    'equal_weight': EqualWeight,
    'score_proportional': ScoreProportional,
    'risk_parity': RiskParity,
    'concentrated_top': ConcentratedTop,
}

def create_position_strategy(name: str, **kwargs) -> PositionStrategy:
    cls = POSITION_STRATEGY_REGISTRY.get(name)
    if cls is None:
        raise ValueError(f"Unknown position strategy: {name}")
    return cls(**kwargs)
