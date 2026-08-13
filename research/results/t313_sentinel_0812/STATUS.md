# T313 哨兵值守 STATUS - 2026-08-12

## 最新心跳: 11:35 ✅✅✅ 晨间值守完成 | SENTINEL_REPORT.md已生成

### 最终状态 (11:35)
- 全链路 **ALL PASS** —— 0生产异常
- intraday_monitor 轮询#786 稳定运行中
- 持仓5只全部holding，无触线
- SENTINEL_REPORT.md 已生成: research/results/t313_sentinel_0812/SENTINEL_REPORT.md

### 09:27 核心窗口 morning_decision ✅✅✅
- 决策时间: 09:25 ✅ 硬时序尊守
- **南模生物到期卖出**: sell_price=46.4, reason=expired, pnl=+3.11% ✅
- **买入决策** (3笔):
  - P5: 诺唯赞(sh.688105) @22.31 (S2巨振反转) TP=23.43/SL=16.73
  - P1: 华中数控(sz.300161) @30.60 (S4大阳低吸) trailing 4pp/SL=28.76
  - P4: 百普赛斯(sz.301080) @79.90 (S5双板回调低吸) 定时卖出8/13 10:30
- **S3冻结行为** ✅: 斯菱智驱满足条件但转假想跟踪，不实买
- **公告预警** ✅: 宏昌科技(sz.301008)★减持计划→拒单；毕得医药★一致行动解除→拒单
- **企微通知** ✅: 4次群机器人发送成功(1卖+3买)
- 满仓: 5/5 slot占用，现金¥1,679

### 09:31 position_tracker ✅
- 检测到intraday_monitor存活，cron跳过，由daemon接管卖出检查 ✅

### 09:33 intraday_monitor运行状态 ✅
- PID=3077159 USE_EXECUTION_CORE=shadow ✅
- 轮询#53 10秒/次 ✅
- 行情22/22 持仓5(触线0) 候选20 ✅
- T+0禁卖标记正确(诺唯赞/华中数控/百普赛斯)
- board_lab_collector ✅ PID=3078212 09:28启动

### 08:46 daily_plan_report ✅
- 冰点overlay: 昨日非ST涨停60家≥35 → 满仓系数1.0
- 回撤guard: L0正常
- 生成: /reports/20260812_plan.md (5049字节)

### 08:12补充检查
- 系统负载: load=2.27, 内存充足(11.8G可用), 磁盘71%
- 高CPU进程: t311_runner.py(gate+combo) nice=19研究任务，不影响交易链路
- BaoStock探针: 8/7已确认恢复，正常
- T298 verify_rc=1: 仅因缺少t244旧CSV参照文件，不影响生产
- progress_scheduler: 08:10 OK ✅

---

### 巡检摘要 (08:00-08:05)

| 检查项 | 状态 | 详情 |
|--------|------|------|
| candidates_20260812.json | ✅ PASS | S1=0/S2=1/S3=3[冻结]/S4=8/S5=8 共20只，生成时间2026-08-11 22:54:59 |
| Crontab晨间条目 | ✅ PASS | morning_decision 9:25/intraday_monitor 9:25/position_tracker 9:31 均在列 |
| server.py进程 | ✅ PASS | PID=2869851 端口80 API /api/status 响应正常 |
| intraday_monitor | ⏳ 待启 | 9:25 cron拉起（预期） |
| position_tracker | ⏳ 待启 | 9:31 首检（预期） |
| morning_decision | ⏳ 待启 | 9:25 cron触发（预期） |
| 企微通知链 | ✅ PASS | .qywx_env.sh KEY存在，昨日决策通知群机器人发送成功 |
| 公告监控 | ✅ PASS | 07:30跑完，23只全覆盖，命中0条 |
| 持仓状态 | ✅ PASS | 南模生物(P5,今日到期)/洪都航空(P2)/宁波方正(P3) 共3只 |
| S3冻结行为 | ✅ PASS | frozen=true，代码确认冻结策略转入frozen_recommendations不实买 |
| S5定时卖出 | ℹ️ 无触发 | 当前无S5持仓，10:30不会触发 |
| 早间计划报告 | ⏳ 待触发 | 8:45 daily_plan_report.py 尚未执行（预期） |
| scheduler_alerts | ⚠️ 关注 | 昨日V-C拒单(现金不足)属正常逻辑；T298轨②verify_rc=1需留意但不阻塞交易 |

### 今日关键持仓事件
- **南模生物(sh.688265)**: 今日到期日！sell_mode=fixed, TP=50.4, SL=36.0, 买入价45.0, 昨收46.19(+2.64%)
  - 若开盘≥50.4 → 触发止盈卖出
  - 若盘中破36.0 → 触发止损卖出
  - 若收盘仍未触发 → 到期强制卖出（由morning_decision/position_tracker处理）

### 今日买入候选
- S2: 诺唯赞(sh.688105) — 需9:25开盘价确认低开条件
- S4: 8只候选（宏昌科技带★减持预警标注）
- S5: 8只候选
- S3: 3只冻结（仅假想跟踪，绝不实买）

### 下一动作
- 08:45 验证daily_plan_report生成
- 09:15 竞价开始观察
- 09:25 核心窗口：morning_decision输出 + intraday_monitor启动

---
*哨兵: Flynn | 心跳间隔: 10min*
