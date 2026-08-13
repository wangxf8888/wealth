# AIWealth 系统交接运维手册

> 面向对象: 新维护者/未来的自己。目标: 拿着这一份文档就能运维整个系统。
> 编写日期: 2026-08-02（项目转纯运行态前夜）。策略一律用中文正名。
> 工作目录固定: `/home/AIWealth`（下文相对路径均以此为根）。

---

## 1. 系统一页概览

### 1.1 系统是什么

A股T+1短线量化信号系统: 每晚生成候选股 → 次日9:25开盘决策买入 → 盘中10秒级守护监控卖出 → 盘后数据更新+复盘报告。5个资金位（P1~P5, 动态槽, 每笔买入金额=总净值/5），全链路自动化（cron驱动），前端网页实时展示。**系统只产生信号与模拟账本，不接实际券商下单。**

### 1.2 账户现状（positions.json, 2026-08-02快照）

| 项 | 值 |
|---|---|
| 初始资金 | ¥1,000,000（2026-07-22实盘启动） |
| 现金 | ¥399,544.89 |
| 已实现盈亏 | +¥1,601.88 |
| 净值(成本口径) | ¥1,001,601.88 |

当前3持仓+2空闲:

| 标的 | 策略 | 买入日 | 买入价 | 卖出规则 | 到期 |
|---|---|---|---|---|---|
| 翔港科技 sh.603499 | 首板低吸 | 07-30 | 11.34 | TP 12.13 / SL 10.43 | 08-06 |
| 真爱美家 sz.003041 | 巨振反转 | 07-31 | 35.38 | TP 37.15 / SL 26.54 | 08-04 |
| 塞力医疗 sh.603716 | 双板回调低吸 | 07-31 | 14.80 | **定时: 08-03 10:30卖出** | 08-03 |

> 口径分账（重要）: `positions.json`=现金/成本口径账本（预算用）; `drawdown_state.json` 的 nav_history=盯市口径（现金+持仓×最新收盘价, 风险用）, 由 strategy_health.py 每晚19:00快照追加。两者数字略有差异是正常的, 不得混写。

### 1.3 5策略清单（生产组合, slot=1 solo可执行口径）

| 策略(中文正名) | 一句话逻辑 | 关键买入参数 | 卖出规则 | CAGR/MDD/胜率 |
|---|---|---|---|---|
| **首板低吸** (firstboard_low_open_dip_v2) | 昨日首板涨停(非一字)+昨日红盘占比≥55, 次日低开低吸 | 低开买入, 监控D2..D6 | TP +7% / SL -8%, D6收盘兜底 | +102.80% / 36.44% / 63.60% |
| **巨振反转** (amplitude_reversal) | 昨日大上影冲高回落(振幅≥4%)+换手1-30%, 次日低开买反弹 | 开盘价<昨收即买 | TP +5% / SL -25%(六年零触发=纯安全网), 最长持有8小时(D+1全天)到期H4收盘卖 | +93.05% / 61.49% / 54.03% |
| **创科晚封** (gem_star_late_seal) | 创/科昨日涨停+尾盘晚封+非一字, 次日直接买 | 无需确认直接买 | TP +12% / SL -20%(挂单语义, 同小时双触发保守取SL), 最长2天H4收盘 | +130.77% / 72.56% / 51.51% |
| **大阳低吸** (big_yang_low_open_v2) | 创/科/北昨日大阳≥10%非涨停+换手≥3%, 今日低开吸 | 低开≤-2%且开盘>昨日最低 | trailing 4.0pp / 硬止损-6% / 最长3天H4收盘; **G2分钟确认**(8/3起): 10:00前trailing触线须连续2根5min bar收盘破线才卖, 硬止损不受确认约束每tick即卖 | +113.77% / 64.29% / 48.35%（发布口径以minute为准: +106.28%/69.51%） |
| **双板回调低吸** (two_board_pullback_dip_h1c) | 恰2板断板回调1~2天(第2板非一字且H1内首触涨停=早板过滤), D2低开低吸 | 低开-4%~0, H1买入 | **次日10:30定时卖出**(timed, 无TP/SL) | +211.27% / 79.87% / 47.84% |

- 策略身份证总表: `data/realtime/solo_strategy_registry.md`; 逐笔明细档案: `logs/backtest/solo/<策略名>_{trades.json,detail.txt}`
- 策略详细文档: `docs/strategies/*.md`（每策略一份, 含参数演进史）
- 增删策略: 只改 `realtime/config.py` 的 `ACTIVE_STRATEGIES`（框架策略无关, 新策略文件放 `strategies/`）

### 1.4 组合回测口径数字（生产基线, Task#68升级后）

- **升级组合（5策略, 无overlay）: CAGR +190.56% / MDD 35.40% / Calmar 5.38**（2943笔, 组合QA全通过）
- **含冰点减仓overlay（当前实盘实际配置）: CAGR +168.77% / MDD 26.40% / Calmar 6.39（全项目最高）**
- 演进链: 旧污染基线+78.29% → 净化终版+153.00% → Task#68升级+190.56%
- 总审批包: `data/realtime/APPROVAL_PACKAGE_20260728.md`

---

## 2. 每日自动时刻表（crontab -l 逐条解读）

全部任务以 `cd /home/AIWealth` 为工作目录。按一天时间顺序排列（1-5=仅工作日）:

| 时刻 | 任务 | 干什么 | 产出 | 失败看哪个日志 |
|---|---|---|---|---|
| 每天 8:25~23:25 每小时:25 | `tools/baostock_recovery.py --resume` | BaoStock解禁后自动全量回补流水线续跑; 未解禁或已完成时秒级退出零成本 | stocks.db hour列回补 | `logs/realtime/baostock_recovery.log` |
| 每天 8:10~17:10 每小时:10 | `tools/baostock_recovery_probe.py` | BaoStock封禁恢复探测, 恢复后自动标注STATUS+告警, 幂等 | `data/baostock_ban_status.json` | `logs/realtime/baostock_recovery_probe.log` |
| 工作日 8:45 | `tools/daily_plan_report.py` | **早间计划报告**（9:25决策前）: 今日候选/持仓/风控状态 | `reports/YYYYMMDD_plan.md` | `logs/realtime/daily_report.log` |
| 工作日 9:24 | `tools/execution_quality.py archive` | 盘中执行质量快照归档拉起（15:06自然退出）, 只读交易链路 | 快照留存 | `logs/realtime/execution_quality.log` |
| 工作日 9:25 | `realtime/morning_decision.py` | **开盘决策**（核心）: 读昨晚候选+9:25竞价行情 → 买入决策落盘（含冰点/回撤/修复日系数、V-C槽分配） | `data/realtime/decision_YYYYMMDD.json` + positions.json写入 | `logs/realtime/morning_decision.log` |
| 工作日 9:25 | `realtime/intraday_monitor.py`（USE_EXECUTION_CORE=shadow） | **盘中10秒级守护进程**（内部flock防重复+自判交易日, 15:00自然退出）: 持仓触线监控卖出、大阳低吸G2分钟确认分路 | positions.json卖出更新+告警 | `logs/realtime/intraday_monitor.log` |
| 工作日 9:31 / 10~14点的:01和:31 / 15:01 | `realtime/position_tracker.py auto-close` | 卖出检查**cron兜底**: 9:31首检覆盖开盘跳空击穿; 守护进程存活期间(心跳<60s)自动跳过, daemon挂了才接管并告警 | positions.json | `logs/realtime/position_tracker.log` |
| 每10分钟 | `tools/progress_scheduler.py` | STATUS看板自动分区刷新（进程/日志/告警监控）+头部时间戳 | PROJECT_STATUS.md自动分区 | `logs/realtime/progress_scheduler.log`; 告警落 `logs/realtime/scheduler_alerts.log` |
| 工作日 15:10 | `tools/execution_quality.py report` | 执行质量收盘报告（滑点/时延/影子比对） | 报告文件 | `logs/realtime/execution_quality.log` |
| 工作日 18:30 | `tools/daily_update.sh` | **日K数据更新**（BaoStock封禁期自动降级腾讯源, hour列标记待回补） | stocks.db 当日日K | `logs/daily_update_YYYYMMDD.log` |
| 工作日 19:00 | `tools/strategy_health.py` | 策略健康度监控 + **盯市NAV快照**（喂回撤三档手册） | `data/realtime/drawdown_state.json` | `logs/realtime/strategy_health.log` |
| 每天 20:30 | `tools/lhb_backfill.py --budget 2500` | 龙虎榜增量回补（断点续跑幂等, 全部完成后0请求空转; 时段守卫8:00-15:30自动暂停） | lhb.db | `logs/lhb_backfill.log` |
| 工作日 21:30 | `tools/backfill_hour_tencent.py` | hour列腾讯m60最小集回补（当日降级日的创科涨停+候选池）, 在18:30降级入库后/22:00候选生成前; BaoStock恢复后无降级日秒退 | stocks.db hour列 | `logs/realtime/backfill_hour.log` |
| 工作日 22:00 | `realtime/generate_candidates.py` | **明日候选股生成**（5策略各自筛选+排序top20） | `data/realtime/candidates_YYYYMMDD.json`（**明日**日期） | `logs/realtime/generate_candidates.log` |
| 工作日 22:10 | `tools/daily_review_report.py` | **晚间复盘报告**（候选生成后, 含明日机会点段） | `reports/YYYYMMDD_review.md` + `reports/index.json` | `logs/realtime/daily_report.log` |

> 时序依赖链（勿乱改时刻）: 18:30日K更新 → 19:00健康度 → 21:30 hour回补 → 22:00候选 → 22:10复盘; 次日 8:45计划 → 9:25决策 → 盘中守护/兜底。
> 报告类任务独立于交易链路, 失败只写 scheduler_alerts 告警, 不影响买卖。

---

## 3. 前端使用

### 3.1 启动与重启

- Web服务: Flask, **80端口**, 进程为 `python3 server.py`。
- 重启方式（无systemd）: `kill <旧PID> && cd /home/AIWealth && nohup python3 server.py >> logs/server.log 2>&1 &`
- 纪律: **盘中重启避开整点/半点**（cron卖出检查时刻）, 静态HTML/JS改动不需要重启, 只有 server.py 改动才需要。
- `frontend/data/` 下是指向 `data/realtime/` 的**只读软链**（positions.json / drawdown_state.json）, 静态直出, 删了要按 `ln -s` 重建。

### 3.2 页面与区块

| 地址 | 页面 | 内容 |
|---|---|---|
| `http://<主机>/` | signal.html **实盘信号主页** | 见下方区块说明 |
| `http://<主机>/backtest` | dashboard.html | 回测策略Dashboard（策略维度tab, 历史回测明细） |
| `http://<主机>/v9` | index.html | 旧版页面（保留） |

主页（signal.html）区块自上而下:

1. **头部**: 本地时钟每秒走秒 + 交易中/收盘角标; KPI栏（总资产/累计收益/持仓数/总交易/胜率 + **最大回撤格**: ≥10%橙/≥20%红, 与回撤手册L1/L2同色系）; 数据快照时间另标（与走秒钟口径分离）。
2. **指数行情栏**: 上证/深成/创业/科创50/北证50 + 韩国KOSPI/日经225（东财延迟源, 显示上与A股指数完全一致——用户裁决; |涨跌幅|>8%守卫仅console.warn静默） + 大A涨跌家数比。盘中30秒刷新, 收盘后停更。
3. **持仓区（动态槽）**: P1~P5资金位编号, 持仓行显策略中文名（从positions.json的strategy字段读）, **空闲槽只显"P× 空闲"不标策略**（V-C架构: 槽位无策略属性）; 今日买入的标的带"T+1 明日可卖"角标; 卖出规则文案从持仓实际参数动态生成。
4. **候选区**: 按策略分组展示明晚候选（策略维度, 与槽位无关）。
5. **每日报告入口**: reports.html, 读 `reports/index.json` 列出早间计划/晚间复盘。

---

## 4. 周一(2026-08-03)首跑须知

**8/3是四件套新变更的首个生效交易日**（QA终审6项审计全通过放行, 报告 `data/realtime/QA_AUDIT_LANDING_20260731.md`）。改动前五文件快照在 `backup/ab_20260731/`。

| # | 变更 | 生效开关（所在文件） | 一行回滚 |
|---|---|---|---|
| 1 | **V-C匿名动态槽位制**: 5槽与策略解绑, 谁有信号谁占空闲槽; 无空闲槽时复用信号当日放弃(skipped_no_idle_slot留痕), 杜绝先买后卖 | `realtime/config.py` `VC_DYNAMIC_SLOTS = True` | 改 `False`（恢复策略通道即槽位） |
| 2 | **候选生成修复**（创科晚封/大阳低吸回归）: permissive开盘价 0.80→0.995, 修复0.80恰等于创科跌停价撞死过滤条件导致连续9信号日零候选 | `realtime/config.py` `PERMISSIVE_OPEN_RATIO = 0.995` | 改回 `0.80` |
| 3 | **大阳低吸G2分钟确认卖出**: 10:00前trailing触线须连续2根5min bar收盘破线确认, 次bar开盘卖; 硬止损每tick即卖（只紧不松） | `strategies/big_yang_low_open_v2.py` `confirm_bars = 2` | 改 `0`（恢复触线即卖; 现有持仓confirm字段为None恒走legacy路径, 零影响） |
| 4 | **修复日豁免**: 冰点日若同时是修复日（昨日弱势池今晨9:25浅高开[2,3)达50只）→ 买入系数豁免恢复1.0满仓; 判定失败安全降级为不豁免 | `realtime/config.py` `REPAIR_EXEMPT_ENABLED = True` | 改 `False` |

**首跑观察点（9:25决策后马上看）:**

1. `data/realtime/decision_20260803.json` 里的三个字段:
   - `vc_allocation`: V-C槽分配结果（谁占了哪个空闲槽/有无skipped_no_idle_slot）
   - `position_scale` / `final_scale`: 冰点×回撤×修复日豁免的最终买入系数链（`repair_exempt` 七键留痕, 注意其低置信度警示是设计内的）
2. **塞力医疗 10:30 定时卖出**是否执行（首个V-C日的存量timed单）。
3. `logs/realtime/intraday_monitor.log` 守护进程正常拉起、G2分路无异常（现有3持仓均不带confirm字段, G2代码路径本周仅新开的大阳低吸持仓会走到）。
4. 有异常先查 `logs/realtime/scheduler_alerts.log`。

---

## 5. 常见故障手册

| 症状/告警 | 原因与处置 |
|---|---|
| **BaoStock封禁**（当前状态: **仍封禁中**, 2026-07-24起, "10001011 黑名单用户"） | **无需人工**。状态文件 `data/baostock_ban_status.json`; 封禁期18:30日K自动降级腾讯源+21:30 hour最小集回补, 候选/决策链正常运转; 每小时探测(:10), 解禁后恢复流水线(:25)自动全量回补hour列并在STATUS标注。唯一纪律: **不要手工高频调BaoStock接口**（就是这么被封的）。 |
| **9:25行情源异常** | morning_decision 内置Sina备用K线源+异常熔断（Task#38）: 主源失败自动切换, 双源均异常时熔断不下单只告警。看 `logs/realtime/morning_decision.log`。 |
| **数据缺行告警** | `logs/realtime/scheduler_alerts.log`（progress_scheduler每10分钟巡检写入）。多数为降级日hour列缺失, 21:30回补自愈; 连续多日同一告警才需人工查 `logs/daily_update_YYYYMMDD.log`。 |
| **盘中守护进程死亡** | **cron自动兜底**: position_tracker auto-close 每30分钟接管卖出检查并告警, 不会裸奔。可手工重拉: `cd /home/AIWealth && USE_EXECUTION_CORE=shadow python3 realtime/intraday_monitor.py &`（内部flock防重复, 直接跑不会双开）。 |
| **CASH_GUARD / VC_CASH_GUARD 告警** | **需人工对账**（这是tripwire, 不自愈）: 现金守恒校验失败=positions.json账本可能被并发写坏。核对 positions.json 的 account.cash 与逐笔买卖流水, 修正后观察下一交易日。 |
| **L3熔断触发**（回撤≥30%） | 停止一切新开仓, 粘滞不自动解除。人工确认后: `python3 tools/drawdown_guard.py ack` 解锁。 |
| **前端页面数据不动** | ①server进程死了→按§3.1重启; ②软链断了→重建 `frontend/data/` 软链; ③收盘后指数条停更/候选区收起是**正常设计**。 |
| **指数条韩日数字与行情软件不一致** | 东财延迟源, 历史上出现过脏数据（2026-07-31 KOSPI事件）。server.py源2内 `INTL_REALTIME` 开关: True=实时口径(现状, 用户裁决), False=昨收口径。前端>8%守卫在console.warn留痕。 |

---

## 6. 文件地图（终态目录结构）

```
/home/AIWealth/
├── server.py                  # Flask前端服务(80端口), 所有/api/*接口
├── execution_core.py          # 共享卖出引擎ExitEngine(回测/实盘同一决策代码)
├── tick_scheduler.py          # 统一调度: 5min最小tick+策略声明决策频率
├── trading_rules.py           # 涨停/跌停价精确计算等交易规则(单一来源)
├── PROJECT_STATUS.md          # 项目看板: 顶部总览栏+自动监控分区+任务明细(时间倒序)
├── realtime/                  # ★实盘链路(改动最谨慎的目录)
│   ├── config.py              #   策略清单+全部生产开关(V-C/冰点/修复日/permissive)
│   ├── generate_candidates.py #   22:00晚间候选生成
│   ├── morning_decision.py    #   9:25开盘决策(买入)
│   ├── intraday_monitor.py    #   盘中10秒守护(卖出+G2分路)
│   ├── position_tracker.py    #   持仓账本读写+auto-close兜底+现金卫兵
│   ├── data_feed.py           #   实时行情源(腾讯主+Sina备+熔断)
│   └── notify.py              #   告警通道
├── strategies/                # 策略类(5生产+历史归档), base.py为标准接口
├── backtest/                  # 统一回测引擎(engine/run_unified/portfolio/position_scale/minute_exit)
├── tools/                     # 运维工具(crontab里跑的都在这): daily_update.sh/
│                              #   strategy_health/drawdown_guard/daily_*_report/
│                              #   baostock_recovery*/backfill_hour_tencent/lhb_backfill/
│                              #   progress_scheduler/execution_quality
├── frontend/                  # 网页: signal.html(主页)/dashboard.html(回测)/reports.html
│   └── data/                  #   → data/realtime/ 只读软链
├── data/
│   ├── stocks.db              # ★主库: 日K+4小时K宽表(见§8)
│   ├── minute.db              # 5分钟K线独立库
│   ├── lhb.db                 # 龙虎榜库
│   ├── baostock_ban_status.json
│   └── realtime/              # ★实盘状态与档案: positions.json(账本)/
│                              #   candidates_*.json/decision_*.json/drawdown_state.json/
│                              #   solo_strategy_registry.md/各审批包与QA报告(*.md)
├── docs/                      # 本手册/strategies策略文档/trading_mechanism交易机制/
│                              #   entry_engine_design/unified_pipeline_walkthrough
├── reports/                   # 每日双报告(YYYYMMDD_plan.md / YYYYMMDD_review.md + index.json)
├── logs/
│   ├── realtime/              # ★实盘各环节日志+scheduler_alerts.log(告警汇总)
│   ├── backtest/              #   回测明细档案(solo/逐笔trades.json+detail.txt)
│   ├── daily_update_*.log     #   每日数据更新
│   └── server.log             #   前端服务
├── backup/ab_20260731/        # 8/3生效变更的改动前快照(回滚用)
├── scripts/                   # QA审计脚本(qa_audit_*)+测试脚本(test_*)+研究遗留
└── archive/scripts/           # 已归档的历史任务脚本(INDEX.md为清单)
```

保留红线: `realtime/` `strategies/` `backtest/` `tools/`(crontab在用的) `data/*.db` `data/realtime/` `frontend/` `docs/` `reports/` 不可删; `archive/` `scripts/`内test_*与研究脚本可按 `archive/scripts/INDEX.md` 追溯。

---

## 7. 风控现状（全部已代码化, 叠乘生效）

买入金额最终系数 `final_scale = 冰点系数 × 回撤系数`（下限0.1）, 每项独立开关:

| 风控项 | 规则 | 状态/开关 |
|---|---|---|
| **冰点减仓overlay** | 昨日非ST涨停家数<35 → 当日新开仓买入金额×0.3 | 启用中: `config.py POSITION_SCALE_ENABLED=True / ICE=35 / W=0.3` |
| **修复日豁免** | 冰点日若同时是修复日(昨日弱势池9:25浅高开[2,3)≥50只) → 豁免恢复满仓1.0; 判定失败安全降级不豁免 | 8/3生效: `REPAIR_EXEMPT_ENABLED=True`（低统计置信度, decision json留痕） |
| **回撤作战手册三档** | 盯市NAV距HWM回撤: **L1≥10%**新开仓×0.5 / **L2≥20%**仅白名单(创科晚封/大阳低吸)开仓×0.5 / **L3≥30%**熔断停止一切新开仓+粘滞 | `tools/drawdown_guard.py`常量; L3解锁: `python3 tools/drawdown_guard.py ack`; L1/L2随回撤回落自然解除; 状态 `drawdown_state.json`（当前L0, 回撤约1.3%） |
| **T+1禁卖** | 今日买入禁卖, 前端持仓行带"T+1 明日可卖"角标; 引擎硬校验 | 恒定规则, 无开关 |
| **V-C先卖后买禁令** | 无空闲槽时复用信号当日放弃, 不做先卖后买 | `VC_DYNAMIC_SLOTS=True`内含 |
| **现金卫兵** | 买入前现金守恒校验, 不足即拒单+CASH_GUARD告警(tripwire, 见§5) | position_tracker内置 |
| （已移除）大盘过滤 | "前日上证跌>1%不开新仓"经数据裁决移除(6年成本-41.6pp), 被冰点overlay替代 | 阈值保留在config.py仅供回滚 |

---

## 8. 数据库

| 库 | 表 | 一句话 |
|---|---|---|
| **stocks.db**（主库, 5187只） | `stock_kline` | ★日K+4小时K归一化宽表: date/code/preclose/OHLC及各自_rate + hour1~4各小时OHLC列（策略/回测唯一行情来源; hour列2026-07-27起部分待BaoStock解禁回补） |
| | `index_kline` | 指数日K(上证等, 大盘过滤/冰点判定用) |
| | `stock_list` / `stock_industry` / `stock_concept` | 股票名录/行业/概念维表 |
| | `sector_emotion_daily` | 板块情绪日频指标(涨停密度/轮转, 研究用) |
| | `temp_*` / `lianban*` | 研究中间表, 可随archive清理 |
| **minute.db** | `minute_kline` | 5分钟K线(code/date/time/OHLCV), 按需回补(`tools/fetch_minute_kline.py`), G2确认与minute口径复测用 |
| **lhb.db** | `lhb_daily` / `lhb_seats` | 龙虎榜每日榜单/席位明细(含d1/d5后验收益) |
| | `lhb_done` / `lhb_seat_profile` | 回补断点账本/席位月度画像 |

**更新链路**: 工作日18:30 `daily_update.sh` 写当日日K（BaoStock封禁期腾讯降级源, hour列缺）→ 21:30 `backfill_hour_tencent.py` 补当日关键股hour列 → BaoStock解禁后 `baostock_recovery.py` 自动全量补历史hour列; lhb.db每天20:30增量; minute.db无定时任务, 按需手工回补。

**数据纪律**: BaoStock hour数据2020年起才有; 所有接口调用须限速（封禁教训）; 库文件伴生的-shm/-wal是SQLite正常产物勿删。

---

## 附: 新维护者第一天检查清单

1. `crontab -l` 与本手册§2对照, 确认17条有效条目都在。
2. 打开 `http://<主机>/` , 确认KPI/指数条/持仓区有数、头部时钟走秒。
3. 看 `PROJECT_STATUS.md` 头部时间戳（10分钟内为健康）与"告警"分区。
4. `tail logs/realtime/scheduler_alerts.log` 无新增告警。
5. `cat data/baostock_ban_status.json` 确认封禁/解禁状态与降级链是否还需运转。
6. 交易日9:25后看 `data/realtime/decision_YYYYMMDD.json` 决策留痕字段（§4观察点）。
7. 任何生产开关改动: 只动 `realtime/config.py` / 策略文件里的注明开关, 改前备份, 改后 `py_compile` + 看下一周期日志。
