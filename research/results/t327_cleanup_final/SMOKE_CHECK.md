# Task#327 冒烟验证报告 SMOKE_CHECK

**验证时间**: 2026-08-13 16:35（恢复+commit后全清单重跑）
**总结论**: ✅ 全部PASS（67项，0 FAIL），生产链完整。原始逐项输出见 smoke_raw.txt。

## 冒烟1：crontab活跃条目引用文件存在性 — 26/26 PASS
crontab -l全部活跃条目（26条，注释条目不计）引用的.sh/.py逐一存在，
含 research/results/t291_backfill_p2/nightly_cron.sh（运行时依赖保留目录）。

## 冒烟2：核心生产模块import — 25/25 PASS
- 在产五策略：S1首板低吸v2 / S2巨振反转 / S3创科晚封 / S4大阳低吸 / S5双板回调h1c ✅
- 两个恢复的基类（firstboard_low_open_dip / two_board_pullback_dip）✅ 含继承链断言
- backtest.run_unified/engine/promo_gate/run/portfolio ✅
- realtime全模块（generate_candidates/morning_decision/position_tracker/
  intraday_monitor/notify/promo_gate/data_feed/config/earnings_calendar）✅
- trading_rules / execution_core / tick_scheduler ✅
- tools 18个cron脚本 py_compile 全PASS（首轮已验，见STATUS）

## 冒烟3：server API — 9/9 HTTP 200
/api/positions /api/candidates/latest /api/decision/latest /api/live/equity
/api/status /api/strategies /api/backtest/summary /api/index_quotes /api/test_signals
（首轮猜测路由/api/candidates与/api/equity的404非故障——server.py真实路由为
/api/candidates/latest与/api/live/equity，已按真实路由表复验）

## 冒烟4：明晨链路 — 7/7 PASS
generate_candidates/morning_decision/position_tracker/notify/intraday_monitor就位，
data/realtime/candidates_20260813.json 与 positions.json 在位。

## 事故与恢复验收（详见STATUS.md）
冒烟2首轮曾FAIL 2项：在产v2/h1c的基类被误删（任务书黑名单第1条源头误判，
leader已认领；本人grep核查为第二道防线亦漏判）。恢复过程：
- git历史/悬空对象/backup均无副本 → /dev/vda3裸设备只读雕刻恢复，
  两文件与DELETE_MANIFEST记录字节数精确一致（13903B/13506B），含t299修复。
- 三关验收：①import+继承断言+py_compile PASS；
  ②solo口径全量回测对拍：S5逐笔0错（仅尾部因end日期多1笔未平仓，属预期）；
  S1 252笔中4处分歧经昨晚22:30滚动回测（删除前旧代码+新数据）仲裁，
  4处全部与恢复代码一致 → 分歧源=solo档案8/12 13:55刷新早于当日18:30日更的
  数据漂移，恢复代码与删除前代码行为逐笔一致 PASS；
  ③git commit 4d94852防再丢 + 本报告的全清单重跑 PASS。
