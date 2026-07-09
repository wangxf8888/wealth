# 多指标评分系统（已废弃）

## 策略描述
基于7个技术指标（大阴高开、跌停反转、涨停次日等）构建评分体系，综合评分决定买入。

## 相关脚本
- `/home/AIWealth/scripts/backtest_scoring_system.py`
- `/home/AIWealth/scripts/optimize_scoring_system.py`
- `/home/AIWealth/engine/indicators.py`

## 废弃原因
- 7个指标中6个存在未来数据泄露（用today close做hour1买入决策）
- 修复未来数据后，2025Q1收益-12.7%，胜率26.3%，证明无真实alpha
- T+1改造被否决（丧失当日盈利优势，变成完全不同策略）

## 当前状态
🚫 已废弃
