# 🛡️ 晨间值守报告 — 2026-08-12（周三）

> 哨兵: Flynn | 值守时段: 08:00-11:35 | 最终状态: ✅ **全链路PASS**

---

## 一、总览

| 链路 | 状态 | 关键结果 |
|------|------|----------|
| 候选股生成 | ✅ PASS | 昨晚22:54生成，5策略20只完整 |
| 早间计划报告 | ✅ PASS | 08:45准时，冰点系数1.0/回撤L0 |
| morning_decision | ✅ PASS | 09:25硬时序执行，3买1卖 |
| 公告预警 | ✅ PASS | 宏昌科技★/毕得医药★正确拒单 |
| S3冻结行为 | ✅ PASS | 斯菱智驱转假想跟踪，绝不实买 |
| 企微通知 | ✅ PASS | 4次群机器人发送成功 |
| intraday_monitor | ✅ PASS | 09:25启动→11:35+连续运行，轮询#786 |
| position_tracker | ✅ PASS | 5次cron正确交接daemon |
| 持仓监控 | ✅ PASS | 5只持仓全程无TP/SL触发 |
| S5定时卖出 | ✅ PASS | 10:30不触发（无昨日S5持仓） |
| 南模生物到期 | ✅ PASS | 09:25到期卖出@46.4，+3.11% |
| 服务进程 | ✅ PASS | server.py/intraday_monitor/board_lab全程存活 |
| 系统心跳 | ✅ PASS | progress_scheduler全周期OK |
| sector_limitup | ✅ PASS | 09:30/10:00/10:30/11:00/11:30五轮采样正常 |

**异常清单: 0项（无生产异常）**

---

## 二、核心事件时间线

### 08:00-08:45 就位巡检
- 08:05 candidates_20260812.json验证: S1=0/S2=1/S3=3[FROZEN]/S4=8/S5=8 ✅
- 08:05 crontab关键条目确认在列 ✅
- 08:05 server.py(PID 2869851, port 80) API响应正常 ✅
- 08:05 企微KEY配置存在(.qywx_env.sh) ✅
- 08:05 公告监控07:30完成23只全覆盖，0命中 ✅
- 08:45 daily_plan_report生成(5049字节) ✅

### 09:25 核心窗口 — morning_decision
- **时序**: 09:25 cron触发，09:26完成 ✅ (硬时序要求遵守)
- **卖出**: 南模生物(sh.688265) expired → sell@46.4 → PnL +3.11% (买入45.0)
- **买入决策** (3笔/3槽):
  - P5: 诺唯赞(sh.688105) @22.31 — S2巨振反转 | TP=23.43/SL=16.73
  - P1: 华中数控(sz.300161) @30.60 — S4大阳低吸 | trailing 4pp/SL=28.76
  - P4: 百普赛斯(sz.301080) @79.90 — S5双板回调低吸 | 定时卖出8/13 10:30
- **公告预警★排除**:
  - sz.301008 宏昌科技: 「关于部分高管股份减持计划实施结果的公告」→ 拒单
  - sh.688073 毕得医药: 「一致行动协议解除法律意见书」→ 拒单
- **S3冻结行为**: sz.301550 斯菱智驱满足条件→转frozen_recommendations(假想跟踪) ✅
- **V-C槽位分配**: 持仓2→卖1→空3(P1/P4/P5)→买3→满仓5/5 ✅
- **企微通知**: 4次「群机器人消息发送成功」(1卖出+3买入) ✅
- 满仓后现金: ¥1,679 | 总净值: ¥1,084,544

### 09:25-09:33 盘中系统启动
- 09:25 intraday_monitor(PID 3077159, USE_EXECUTION_CORE=shadow) ✅
- 09:28 board_lab_collector(PID 3078212) ✅
- 09:31 position_tracker首检→检测daemon存活→cron跳过 ✅
- T+0禁卖标记: 诺唯赞/华中数控/百普赛斯 ✅

### 09:30-11:30 盘中监控
- **轮询稳定性**: 轮询#1→#786，10秒/次，无中断 ✅
- **行情覆盖**: 22/22全程有效 ✅
- **触发状态**: 持仓5(触线0)，全程无TP/SL触发 ✅
- **position_tracker cron**: 09:31/10:01/10:31/11:01/11:31 共5次均正确跳过 ✅
- **10:30 S5定时卖出**: 不触发（当前无昨日S5持仓，百普赛斯为T+0） ✅

### 持仓盘中浮盈走势
| 持仓 | 09:33 | 10:02 | 10:32 | 11:00 | 11:35 |
|------|-------|-------|-------|-------|-------|
| 洪都航空(P2) | +1.7% | — | — | — | — |
| 宁波方正(P3) | +0.4% | — | — | — | — |
| 诺唯赞(P5) | +3.3% | — | — | — | — |
| 华中数控(P1) | +3.4% | +1.9% | +2.0% | +1.9% | +4.8% |
| 百普赛斯(P4) | +6.5% | +3.3% | +4.3% | +4.3% | +5.1% |

---

## 三、S3假想池(shadow_ledger)行为验证

- morning_decision正确处理S3 frozen=True ✅
- 斯菱智驱(sz.301550)满足开盘窗口条件 → 转入frozen_recommendations ✅
- decision_20260812.json中frozen_recommendations字段已记录 ✅
- intraday_monitor以shadow模式跟踪 ✅
- shadow_ledger.py将于15:20盘后记账（读取frozen_recommendations计算纸面PnL）✅
- **绝无实际买入** ✅

---

## 四、关注项（非阻断性）

| 项目 | 严重度 | 详情 |
|------|--------|------|
| T298 verify_rc=1 | ℹ️ 低 | 缺少t244旧CSV参照文件导致验证脚本报错，不影响生产交易 |
| BT_LIVE_DIVERGENCE | ℹ️ 低 | 8/7锐捷网络引擎119.21 vs 实盘116.83(+2.04%差异)，历史数据偏差 |

---

## 五、日志证据路径

| 事件 | 日志路径 |
|------|----------|
| 候选股生成 | data/realtime/candidates_20260812.json |
| 早间计划 | reports/20260812_plan.md |
| 决策输出 | logs/realtime/morning_decision.log (tail -80) |
| 决策JSON | data/realtime/decision_20260812.json |
| 持仓快照 | data/realtime/positions.json |
| 盘中轮询 | logs/realtime/intraday_monitor.log |
| 卖出检查 | logs/realtime/position_tracker.log |
| 系统心跳 | logs/realtime/progress_scheduler.log |
| 板块涨停 | data/sector_limitup/sector_20260812_*.json |
| 公告监控 | logs/realtime/announcement_monitor.log |
| 告警日志 | logs/realtime/scheduler_alerts.log |
| 哨兵心跳 | research/results/t313_sentinel_0812/STATUS.md |

---

## 六、结论

**8/12晨间实盘链路全部正常，无异常无干预。** 所有自动化组件按设计时序精确运行：
- 先卖后买机制 ✅（南模到期→释放P5→诺唯赞入驻P5）
- 公告预警防护 ✅（宏昌科技/毕得医药被正确排除）
- S3冻结纪律 ✅（仅假想，零实买）
- T+0合规 ✅（今日买入标记禁卖）
- daemon接管制 ✅（intraday_monitor存活期间cron全部跳过）
- 满仓运作 ✅（5/5 slot占满，动态再平衡¥216,909/slot）

---
*报告生成: 2026-08-12 11:35 | 哨兵: Flynn*
