# Task#320 部署报告 — 实盘侧生产化（晋级率过热门控 + S5 h1_touch触板兑现）

**部署时间**: 2026-08-12 15:01:58（收盘后，两阶段纪律合规：盘中零生产文件落地）
**批准依据**: 用户批准方案C（#311/#187转正包实盘侧落地B路）

## 一、改动文件清单与备份

| 文件 | 改动 | 备份 |
|---|---|---|
| `realtime/promo_gate.py` | **新增**(122行): t311口径晋级率计算+门控判定 | 无旧版（回滚=删除） |
| `realtime/config.py` | +22行: PROMO_GATE_ENABLED=True/THRESHOLD=0.30, S5_H1_TOUCH_ENABLED=True | `realtime/config.py.bak_20260812_t320` |
| `realtime/morning_decision.py` | +47/-1: 防御式导入+门控块(L2白名单后/V-C分配前)+decision json留痕+通知传参 | `realtime/morning_decision.py.bak_20260812_t320` |
| `realtime/generate_candidates.py` | +52/-3: 候选照常生成, 仅加顶层promo_gate信息性标注字段 | `realtime/generate_candidates.py.bak_20260812_t320` |
| `realtime/position_tracker.py` | +28/-1: evaluate_position timed分支h1_touch判定(仅S5使用timed) | `realtime/position_tracker.py.bak_20260812_t320` |
| `realtime/notify.py` | +5/-1: notify_morning_summary加gate_note可选参数(缺省''=旧格式逐字节兼容) | `realtime/notify.py.bak_20260812_t320` |

## 二、实现要点

### 买入端：晋级率过热门控
- **口径同源铁证**: LU涨停集SQL逐字复制自`research/results/t311_promo_menu/t311_indicators.py`；涨停价=`round(preclose*ratio,2)-0.001`容差，与trading_rules.limit_prices Decimal ROUND_HALF_UP口径经t161验证对齐。自检A1：与t311 indicators_daily.csv随机抽样12日晋级率**逐值一致**。
- **时序合规**: 交易日T 9:25决策用promo_rate(昨日)，数据全部来自昨日18:30日更，零未来函数。
- **触发行为**: ≥30%→各slot recommendations转`skipped_by_promo_gate`留痕（与L2白名单同模式，V-C分配前清空不占槽），通知加"🛑 过热门控生效(晋级率xx%), 今日不开新仓"，decision json写`promo_gate`全留痕，scheduler_alerts留一条。
- **fail-open铁律**: 开关关/日K缺失/陈旧>10天/无首板/任何异常→不拦截+note留痕，绝不炸9:25决策主链。
- **候选端**: generate_candidates仅加顶层`promo_gate`信息性标注（would_trigger预告），候选内容/条数/通知零变化，异常安全跳过。

### 卖出端：S5 h1_touch触板兑现
- **插入点**: `position_tracker.evaluate_position`的timed分支（当前仅S5 two_board_pullback_dip_h1c使用），daemon 10s轮询与cron兜底共享此函数——**零新增轮询**。
- **判定**: 卖出日(due_date)且`'09:30'<=now_hm<'10:30'`且`is_at_limit_up(实时价, preclose, ST联动)`→`action='h1_touch'`按板价(limit_prices[0])卖出；未触板→10:30定时卖(expired)照旧。
- **fail-safe**: preclose缺失不触发走定时；开关关=完全旧行为。sell_reason='h1_touch'为自由文本，前端reasonMap降级显示原文，无需碰前端（建议#322后续加中文映射"触板兑现"）。

## 三、dry-run自检结论（postdeploy_selftest.py, 生产代码, 22/22 PASS）
- **过热日验证**: 2026-03-04（昨日2026-03-03晋级率46.27%: 03-02首板67只→31只再板）→ triggered=True，通知文案"过热门控生效(晋级率46.3%)" ✅
- **正常日验证**: 2026-08-12（昨日8/11晋级率13.95%=86只首板→12只再板）→ triggered=False ✅（明日9:25首跑预计正常放行）
- **S5双路径走查**: 触板→h1_touch按板价 / 未触板H1内持有→10:30 expired照旧 / 10:30整点边界归定时 / 非卖出日不动 / 9:25竞价期不触发 / preclose缺失fail-safe / 开关关闭旧行为 / ST股5%板价联动 / fixed与trailing模式零影响 — 全PASS
- **生产手工入口**: `python3 realtime/promo_gate.py 2026-03-04`与今日均输出正确

## 四、进程与链路影响
- 5个改动文件**全部为cron拉起、无常驻进程**（morning_decision 9:25 / position_tracker整点班 / generate_candidates 21:30 / intraday_monitor 9:25拉起15:00已自然退出）→ **无需重启任何进程**，明日9:25 cron自动加载新代码。
- server.py（常驻）未改动；16:10 BaoStock回补、18:30日更链、21:30候选cron零文件交集，天然不受影响。
- 红线合规: backtest//frontend//server.py零Task#320痕迹（grep验证）；企微通知gate_note缺省空串=旧格式逐字节一致。
- **明日关注点**: S5持仓百普赛斯sz.301080卖出日=2026-08-13 10:30 → h1_touch明日H1即首战值守（daemon 10s轮询判定）。

## 五、回滚步骤（三档任选）
1. **仅关门控**（最轻）: `realtime/config.py`中`PROMO_GATE_ENABLED = False`一行
2. **仅关触板兑现**: `realtime/config.py`中`S5_H1_TOUCH_ENABLED = False`一行
3. **全量回滚**:
   ```bash
   cd /home/AIWealth
   for f in config.py morning_decision.py generate_candidates.py position_tracker.py notify.py; do
       cp realtime/$f.bak_20260812_t320 realtime/$f
   done
   rm realtime/promo_gate.py
   ```
   （全部cron拉起，回滚后无需重启任何进程）
