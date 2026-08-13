# Task#322 前端四问题修复报告

- 执行人: Wilbur | 完成时间: 2026-08-13 16:00
- 修改文件: `frontend/signal.html`（唯一改动文件，纯前端，未动server.py/未重启任何服务）
- 修复后快照: `backup/signal.html.after_t322_20260813`
- 改动清单留证: `evidence/t322_change_blocks.txt`（文件内14处 `Task#322` 注释标记 + 2处h1_touch）

---

## 一、bug1 股价刷新闪烁（先显示'--'再更新）

**根因**：Task#285的回填 `applyQuotes(lastQuotes)` 挂在 masterRefresh 链尾
`Promise.allSettled(domJobs)` 之后。loadPositions 重建表格写入'--'占位后，须等
loadCandidates（4个串行fetch，慢时1.5s+）一起完成才回填，窗口期浏览器paint出'--'
→ 肉眼闪烁。

**修复**：DOM重建函数内部 `innerHTML` 赋值后**同一JS任务内**立即
`applyQuotes(lastQuotes)`（中间无await无paint = 原子替换）。两处：
- loadPositions 表格重建后（~L865）
- loadCandidates 列表重建后（~L1378）

失败降级保持原语义：行情请求异常时 lastQuotes 不动，保留旧值；真缺数据才显示'--'。

**验收**：harness baseline场景（候选API注入1.5s慢响应，50ms采样价格单元格35秒）
`flickerCount=0`（修复前同场景可复现'--'回退）。见 `evidence/t322_baseline.log`。

## 二、bug2 收益曲线当日不更新

**根因**（三点叠加）：
1. `updateNavRealtime` 盲改 `liveNavData` 最后一点，不校验日期（可能污染昨日历史点，
   且当日点不存在时永不追加）；
2. `livePositions`/`liveCash` 仅首屏 `loadLiveOverview` 取一次，日内买卖后失真；
3. `livePositions.length===0` 直接return，全平仓日曲线死。

**修复**（`updateNavRealtime` 重写 + 数据同步）：
- 严格按 `todayKey` 日期键：尾点是今天→改写；尾点<今天→追加新点并同步
  `xAxis.data`（tooltip formatter同步改用navData行对象支持追加点）；
- 每10秒的 `/api/positions` 响应内同步 `liveCash`/`livePositions`/`liveInitialCapital`
  （**复用现有轮询，未新增API请求**）；
- guard 改 `liveDataReady`（账户数据就绪即可，空仓日nav=现金照常更新，
  防cash=0算出-100%脏点）；
- 仅 `isTradeSession()` 时盯市，收盘后不再改点。

**验收**：harness两场景（页面时钟mock到盘中10:30）：
- baseline: `uniqueNavTails=5`（尾点随行情扰动持续变化 11.73→11.91），pageErrors=0
- posfail（首次/api/positions返回500）: 后续10s轮询自愈，`uniqueNavTails=5`，
  曲线不冻结——直接证明"只取一次"旧缺陷已消除

## 三、bug3 持仓饼图每刷必晃

**根因**：每次 loadPositions 都 `initPieChart` 重建实例（清空→重画中间态）+
ECharts默认动画。

**修复**：
- `initPieChart` option 增加 `animation: false`；
- 持仓构成签名 `lastPieCompo`（codes排序join）：仅首次/构成变化才重建实例；
- 份额量化签名 `pieDataSig`（`value/total*1000` 取整 ≈ 0.1pp粒度）：签名不变不
  `setOption`，实质变化才重绘。

**验收**：harness中echarts stub记录——盘中35秒行情4次扰动下饼图无反复重建；
语法与运行时 `pageErrors=0`。

## 四、bug4 累计收益三处不一致（卡片12.30% vs 曲线/通知12.13%）★裁决

### 三条数据路径权威源
| 路径 | 数据源 | 收盘后口径 |
|---|---|---|
| 前端"累计收益"卡片 | `refreshHeaderStats`: cash + Σ股数×`intraday_snapshot.json`价格 | **锁死在snapshot最后一帧盘中价**（`kpiClosedRendered`渲染一次后停轮询） |
| 资金曲线尾点 | `/api/live/equity`: 历史点=drawdown_state 19:00收盘盯市快照；当日点=server现抓qt实时价（收盘后=收盘价） | 收盘价盯市 ✅ |
| 盘后通知日报 | `realtime/notify.py` L436-448: cash + Σ实时价×股数 | 收盘价盯市 ✅ |

### 复算裁决（8/12盘后数据，用户给的对照数字）
1. **基数排除**：baseA(cash+Σbuy_amount)=1,115,312.50 vs
   baseB(initial+Σclosed_pnl)=1,115,312.49，Δ=0.01元 → 初始基数非嫌疑。
2. **实时无分歧**：盘中三路径（卡片snapshot口径/曲线API/qt直连）全部
   =¥1,119,310.38（11.93%）→ 分歧只出现在收盘后。
3. **收盘价盯市手算**：5只持仓（sh.600316/sz.300998/sh.688105/sz.301080/sz.300161）
   ×8/12收盘价 + 现金1,679.14 = **nav=1,121,346.25 = 12.1346%**，与
   drawdown_state 8/12快照及曲线/通知的12.13完全吻合。

**裁决：12.13正确（曲线/通知），卡片12.30错误。**
根因=收盘后卡片停留在intraday_snapshot最后一帧（≈14:59盘中价，不含收盘集合竞价），
8/12差¥1,654≈0.17pp。纯前端缺陷，**无需改server.py/position_tracker**。

### 8/13当日现场再复现（修复佐证）
- snapshot末帧=14:59:52，600316末帧28.77 vs 收盘28.70；
- 无修复时卡片=1,117,794.25(11.78%) vs 收盘盯市=1,117,250.19(11.73%)，又差0.054pp；
- `/api/live/equity` 实测返回 current_nav=1,117,250.19=11.73%，与独立收盘价手算
  **分毫不差** → 修复后卡片=曲线=通知三处统一。

### 修复
`refreshHeaderStats` 内：收盘后（`!inSession`，该路径只渲染一次）改用
`/api/live/equity` 的 `current_nav` 覆盖snapshot口径nav——与曲线尾点、盘后通知
同源同口径（收盘价盯市）。盘中行为不变（10s快照轮询），无新增轮询。

## 顺手项：reasonMap h1_touch
两处reasonMap（持仓表/交易流水）均已加 `h1_touch:'触板兑现'`（~L871、~L969），
静态文件刷新即生效，S5卖出原因不再裸显代号。

---

## 备份事项说明（如实）
8/12会话创建的 `backup/signal.html.bak_20260812_t322` 在本次收尾时已不存在
（同目录其他8/12备份仍在，疑似期间磁盘清理任务波及；git基线619e19c为43KB旧版
含大量他人未提交改动，不可作干净diff基线）。补救留证：
- 修复后完整快照 `backup/signal.html.after_t322_20260813`（md5=16c6db76eb228008e4201a02f3943539）
- 全部改动块带行号清单 `evidence/t322_change_blocks.txt`（14处Task#322标记）

## 自测汇总
- JS全量语法检查（node vm.compileFunction）: PASS
- harness baseline 35s: flicker=0, navTails=5, pageErrors=0（`evidence/t322_baseline.log`）
- harness posfail 35s: 首次500自愈, flicker=0, navTails=5, pageErrors=0（`evidence/t322_posfail.log`）
- bug4权威口径实测: /api/live/equity=11.73% 与独立收盘价复算1,117,250.19完全一致
- 约束遵守: server.py零改动零重启；红涨绿亏配色未动；10s轮询节奏未动；无新前端依赖
  （jsdom仅harness本地测试工具）
