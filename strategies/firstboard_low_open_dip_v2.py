"""FB-A v2 (Task#50 挖潜): red_ratio 60→55 + tp6→7 + sl-10→-8。

buy/排序/持有窗全部不动(昨日首板→D1低开-4~-2→turn升序top1→H1_open买入,
tp/sl挂单监控D2..D6, D6收盘兜底)。改动仅3个数值参数。

研究口径walk-forward(训2021-2024/盲2025-2026, t50_scan.log):
  本组 训: n=180 WR63.9% avg+1.90% 年化+110.1% | 盲: n=71 WR63.4% 年化+102.3%
  头部平台连续(red50-55 x tp6-7 x sl-8 邻域全≥+84%训年化), 非孤峰。
引擎精测(2021-01-01~2026-07-23, slot=1, t50_engine.log E2):
  全期CAGR +102.80% | MDD 36.44% | 250笔 WR63.60%
  训(21-24) 年化+100.77% MDD36.02% | 盲(25~) 年化+100.29% MDD25.53%
  vs v1基线(+64.85%/MDD44.81%/226笔): CAGR +37.95pp 且 MDD -8.37pp
引擎邻域(单维偏移): red50→+102.6 / tp6→+93.1 / sl-10→+80.0 (平滑);
  red60(=v1)→+61.8 — v1恰位于red维弱点, v2修复该维并放宽止损减少洗出。
定位: 观察池 → S1(gem_star_late_seal系, +56.73%)替换候选, 待QA审计+用户审批。
"""
from strategies.firstboard_low_open_dip import FirstboardLowOpenDipStrategy


class FirstboardLowOpenDipV2Strategy(FirstboardLowOpenDipStrategy):
    name = "firstboard_low_open_dip_v2"
    red_ratio_min = 55.0         # 60→55: 放宽情绪门槛, 引擎+41pp的主贡献维
    take_profit_pct = 0.07       # 6%→7%
    stop_loss_pct = -0.08        # -10%→-8%: 更早认错, MDD同步改善
