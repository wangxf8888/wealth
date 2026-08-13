# Task#319 滚动链兼容检查清单（只列不改 — 按任务书红线，改动权在leader后续调度）

> 勘察时间：2026-08-12 | 席位：Tobias
> 背景：#319已将回测侧默认口径升级为新基线（生产R2 + 晋级率门控p70:0.3 + S5 h1_touch）。
> CLI路径（run_unified默认）已=新基线；但**API路径（run_unified_backtest函数）promo_gate默认None**
> （向后兼容设计），故所有API调用方需显式传参才能对齐新基线。

## P0 — 必须同步（口径漂移会直接影响生产档案/告警） — ✅已全部收口(2026-08-12 Task#324, 修复详单见P0_FIX_LOG.md)

1. **tools/weekly_solo_refresh.sh 第81行**（cron: 周日20:00） — ✅已修复(2026-08-12 #324: 第82行追加--promo-gate off+口径纪律注释, dry-run验证5命令全带)
   - 现状：`--position-scale off --dd-boost off`，**缺 `--promo-gate off`**
   - 影响：#319后CLI默认promo_gate=p70:0.3，下次周日solo刷新会被门控污染
     → solo纯策略口径漂移，registry/看板solo数字失真。
   - 同步方式：第81行追加 `--promo-gate off`；同时第15-17行口径纪律注释补promo-gate说明。
   - 注：h1_touch经策略类自动生效，solo刷新**应当**吃到（solo=纯策略口径含出场结构），无需动作。

2. **tools/daily_rolling_backtest.py**（cron经nightly_rolling.sh: 工作日22:30） — ✅已修复(2026-08-12 #324: run_one传promo_gate+PROMO_GATE常量+锚更新182.22(旧锚保留对照注记)+caliber标签升级含gate键名不动, 短窗口试跑日志[promo-gate] p70:0.3确认生效)
   - 第137/150-152行：`run_one()` → `run_unified_backtest(position_scale=, dd_boost=)`
     **未传promo_gate** → API默认None，滚动"生产口径R2"块缺门控
     → 与新基线组合档案（本任务刷新的unified_5slot_*）出现口径撕裂。
   - 第57-58行：ICE_SPEC/DD_BOOST常量旁需加 PROMO_GATE='p70:0.3' 常量。
   - 第12行docstring锚 `171.73/26.50/6.48/2889` → 需更新为新锚 `182.22/20.25/9.00/2893`
     （第67行注释"偏离R2锚171.73"同理）。
   - 第386-387行 caliber标签 `生产口径R2(...)` → 需升级为含gate的标签（键名稳定防消费方断裂，
     沿用#204模式：内容升级、键名不动）。
   - 注：h1_touch经策略类自动生效 → **在同步promo_gate之前，滚动跑处于"半新口径"状态**
     （有h1_touch无gate），偏离告警基线可能触发，建议尽快同步。

## P1 — 建议同步（非生产关键，但口径标注过时）

3. **backtest/run_all5.py 第63行**：API调用无任何overlay参数（无ice/boost/gate），
   本就是老全默认无overlay口径的遗留脚本；无cron调用。若继续保留，建议docstring注明
   "非生产口径，生产基线走run_unified CLI默认"。
4. **tools/backtest_vs_live_compare.py 第277行**：仅读decision里的position_scale字段做对照展示，
   不跑回测；gate生效日实盘侧由Duke#320落地后，对照口径自然对齐，无需改代码。
   建议#320落地后观察一个gate日的对照报告确认。
5. **tools/progress_scheduler.py**：仅进程名监控（run_unified|run_all5|...），无口径依赖，零动作。
6. **backtest/run_combined.py**：资金隔离老模式（BacktestEngine，非统一资金池），
   从不支持ice/boost/gate任何overlay（结构性不适用，门控是组合层仓位系数概念）；
   **h1_touch经策略类自动生效**（出场结构属策略本体）→ 任务书"run_combined默认口径=新基线"
   在其能力边界内已满足（策略层新口径自动继承，组合overlay层本模式不存在），无cron调用，零动作。

## 结论

- ~~立即需要动的只有2处~~ → **✅P0×2已于2026-08-12 Task#324收口**（weekly_solo_refresh.sh加`--promo-gate off`；daily_rolling_backtest.py
  传promo_gate+锚数字+caliber标签），修复与验证证据见同目录P0_FIX_LOG.md。P1×4不动（维持#319零紧急认定）。
- 时间窗提示：下一次nightly_rolling=今晚22:30，下一次weekly_solo=周日20:00。
  ✅今晚22:30滚动将以完整新基线口径（ice+boost+gate+h1_touch）运行，与新基线锚182.22可比。
