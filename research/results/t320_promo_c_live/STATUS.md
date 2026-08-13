# Task #320 批准C落地B：实盘侧生产化 — STATUS

**更新时间**: 2026-08-12 12:25 （盘中第一阶段：仅staging编码，零生产文件落地）

## 进度
- [x] TaskGet任务书 + 全链路现状阅读（morning_decision/intraday_monitor/position_tracker/notify/generate_candidates/crontab/前端消费）
- [x] staging编码完成（6文件，语法全过ast.parse）：
  - `staging/realtime/promo_gate.py` 新建：t311 LU SQL逐字同源，check_promo_gate fail-open铁律
  - `staging/realtime/config.py`：+PROMO_GATE_ENABLED/THRESHOLD(0.30)、S5_H1_TOUCH_ENABLED
  - `staging/realtime/morning_decision.py`：门控块插入L2白名单后/V-C分配前，清recommendations留痕(skipped_by_promo_gate)，decision json全留痕，通知传gate_note
  - `staging/realtime/notify.py`：notify_morning_summary加gate_note参数（缺省''完全兼容旧格式）
  - `staging/realtime/generate_candidates.py`：候选照常生成+顶层promo_gate信息性标注（9:25重算为准）
  - `staging/realtime/position_tracker.py`：evaluate_position timed分支加h1_touch（仅S5用timed），H1窗口'09:30'<=now_hm<due_time触板→按limit_prices板价卖，未触板走原定时；daemon 10s轮询/cron兜底共享此函数，零新轮询
- [ ] **当前**: mock/dry-run自测（promo_gate历史过热日+正常日；h1_touch合成quote双路径）+ diff自审
- [ ] 15:00后：备份.bak_20260812_t320→落地→dry-run自检→DEPLOY_REPORT.md

## 红线自查
- #319零交集：未碰backtest/与档案看板 ✅
- #322零交集：未碰frontend/、signal.html、dashboard.js、server.py（sell_reason前端reasonMap降级显示原文，无需新字段）✅
- 21:30候选cron与18:30日更链：generate_candidates仅加信息性标注字段，异常安全跳过 ✅
- 企微通知格式兼容：gate_note缺省空串=旧格式逐字节一致 ✅

## 风险备忘
- SearchReplace工具在本目录有"报错但延迟落盘"现象 → 每次编辑后grep+ast双验（已执行）
- --test-date会写pending_buys.json → dry-run用独立只读脚本，不用--test-date打生产

## 2026-08-12 13:30 — 第一阶段完成, 等待15:00收盘
- [x] mock自测 22/22 PASS（mock_selftest.py, 全程只读）:
  - A1: 与t311 indicators_daily.csv抽样12日晋级率逐值一致（口径同源实证）
  - A3: 历史过热日2026-03-03(46.27%)次日门控triggered=True; A4: 正常日2026-08-11(13.95%)不触发
  - A5: fail-open双路径PASS; 今日真实判定triggered=False（明日9:25首跑预计正常放行）
  - B1-B9: h1_touch触板/未触板/边界10:30/非卖出日/竞价期/preclose缺失/开关关闭/ST股5%板/fixed模式不受影响 全PASS
  - C: gate_note格式兼容PASS
- [x] diff自审: 5文件仅Task#320预期hunks（config+22 / morning_decision+48 / generate_candidates+55 / position_tracker+29 / notify+6），生产区零Task#320痕迹
- [x] conn生命周期核验: 门控块插入点conn未关闭（此前close均为早退分支）
- [x] 进程盘点: 5改动文件全部cron拉起、无常驻进程（server.py未改动; intraday_monitor 15:00自然退出明日9:25自动加载新代码）→ 无需重启任何进程, 16:10回补/18:30日更/21:30候选链天然不受影响
- [x] deploy.sh就绪（内置15:00时间闸门+备份+ast验证+生产导入冒烟）; postdeploy_selftest.py就绪（生产版22项自检）
- [ ] 15:00后: 执行deploy.sh → postdeploy_selftest.py → DEPLOY_REPORT.md
- [13:56] 心跳: 等待15:00收盘, staging就绪, mock 22/22 PASS
- [14:06] 心跳: 等待15:00收盘, staging就绪, mock 22/22 PASS
- [14:16] 心跳: 等待15:00收盘, staging就绪, mock 22/22 PASS
- [14:26] 心跳: 等待15:00收盘, staging就绪, mock 22/22 PASS
- [14:36] 心跳: 等待15:00收盘, staging就绪, mock 22/22 PASS
- [14:46] 心跳: 等待15:00收盘, staging就绪, mock 22/22 PASS
- [14:56] 心跳: 等待15:00收盘, staging就绪, mock 22/22 PASS
- [15:05] 部署完成+22/22自检PASS, DEPLOY_REPORT.md已写
- [15:06] 15:00已过, 进入部署阶段
- [15:06] Task#320全部完成: 部署+自检+报告齐备, 明日9:25首跑观察点已注记
