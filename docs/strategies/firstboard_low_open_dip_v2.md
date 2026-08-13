# 首板低吸（正名）·首板次日低开低吸 v2 (firstboard_low_open_dip_v2 / FB-A v2)

> **状态: 已批准发布(2026-07-28)，2026-07-30首个交易日生效**（QA放行 `data/realtime/QA_AUDIT_FBA_V2_REPORT.md`；Task#68完成实盘切换：S1槽位替换limitup_early_seal，全链路测试通过）
> 生产部署: 5-Slot组合 S1槽位，卖出=fixed TP+7%/SL-8% 挂单监控D2..D6、D6收盘兜底

## 1. 策略概览

| 指标 | 数值 |
|------|------|
| CAGR (slot=1, 可执行口径) | **+102.80%** |
| 交易笔数 | 250 |
| 胜率 | 63.60% |
| 最大回撤(MDD) | **36.44%**（全registry最低档） |
| 平均收益/笔 | +1.82% |
| 训/盲分段 | 训(21-24) 年化+100.77% MDD36.02% / 盲(25~) 年化+100.29% MDD25.53% |
| 回测区间 | 2021-01-01 ~ 2026-07-23（干净数据，双边成本0.2%） |

**一句话总结**：昨日首板涨停（非一字）且当日红盘占比≥55（强势日错杀），次日竞价低开-4%~-2%时H1开盘低吸，D0低换手优先取top1，TP+7%/SL-8%挂单监控5个交易日。

## 2. 核心逻辑（D1开盘前无未来数据）

| 条件 | 要求 |
|------|------|
| 昨日首板 | D0涨停(连板数==1, 非一字)，D-1未涨停 |
| 情绪门槛 | D0红盘占比 red_ratio ≥ 55（权威源 index_kline sh.000001，缺失日显式跳过不买入） |
| D1竞价低开 | -4% ≤ open_rate < -2% |
| 基础过滤 | 非ST、非北交所、上市≥20根K线 |
| 排序 | D0换手升序（低换手=筹码未松动优先），取top1 |

买入：D1 Hour1开盘价（开盘已涨停不追买）。
卖出：fixed模式，TP=+7%、SL=-8%，挂单监控D2..D6，D6收盘兜底（exit_mode='tpsl_d6'）。

## 3. v2 相对 v1 的改动（Task#50 挖潜）

仅3个数值参数：red_ratio 60→55、TP 6%→7%、SL -10%→-8%。
- vs v1基线(+64.85%/MDD44.81%)：CAGR +37.95pp 且 MDD -8.37pp
- 引擎邻域单维偏移平滑非孤峰：red50→+102.6 / tp6→+93.1 / sl-10→+80.0；red60(=v1)恰在red维弱点

## 4. 实盘执行要点（Task#68）

- 晚间候选：signal模式无法预筛次日开盘窗口 → 跳过窗口过滤只筛形态（同Task#36 B2-A模板），保留完整名单（candidate_top_n=300覆盖全局TOP20截断），9:25用真实开盘价复筛
- red_ratio 依赖昨日盘后 index_kline 值（Task#10/31 降级路径已根治）
- 持仓字段：sell_mode='fixed'，tp_price/sl_price 由 morning_decision get_sell_params 生成（tpsl_d6 → fixed 分支）

## 5. 档案与依据

- 引擎精测: `logs/backtest/solo/t68_fbav2_regress_trades.json`（+102.80%/36.44%/250笔，与审批档案逐分一致）
- 研究/挖潜: Task#50（walk-forward 训2021-2024/盲2025-2026, t50_scan.log / t50_engine.log）
- 验收流程: Task#51②（v2完整验收）; QA: `data/realtime/QA_AUDIT_FBA_V2_REPORT.md`
- 审批包: `data/realtime/APPROVAL_PACKAGE_20260728.md`（A项）
- 回滚: `realtime/config.py` S1条目恢复注释中的旧条目即可（<1分钟）

## 6. 已证伪的卖出改动（勿重复试错）

- **早盘冲高落袋R1（2026-07-31, SPIKE HARVEST线）**: 持有日早盘(≤10:30)浮盈≥X%市价止盈，X∈{3..7}回放。X=4是全网格唯一正改善（+0.23pp/WR+5.2pp），但分年2/6劣化、Clean-only弱化至+0.12pp且3/6年劣化，未过预注册线，不落地。改善来源是tp7全天等的expired/止损过山车笔（24笔+4.87~+13.51pp）与被抢跑的take_profit笔（35笔-2.65pp）对冲——真正议题是**tp7可能偏远**（S1卖侧重校准候选思路，需先解决minute覆盖仅76%）。详见 `data/realtime/RESEARCH_SPIKE_HARVEST.md`。
