# FINAL_STATE_20260802 — 收官清理终态清单
生成: 2026-08-02 21:40 | 执行依据: CLEANUP_FINAL_20260731.txt + 用户白名单批量法授权(21:25)
删除审计记录: CLEANUP_DELETED_20260802.txt（每批删除前 ls 快照）
磁盘: 清理前 93%用满/2.7G 剩 → 清理后 92%/3.2G 剩（AIWealth 目录 6.1G→5.6G）

## 验收结论（四层断链全绿）
1. crontab 17条(14个唯一脚本) 引用存在性: 全 OK
2. import 试载: realtime全模块+backtest+execution_core/tick_scheduler/trading_rules+5策略动态加载(S1..S5) 全 PASS
3. 前端: index/dashboard/positions.json/reports 均 HTTP 200（3个软链 ls -L 可解析）
4. py_compile: realtime/tools/scripts/backtest/strategies/根4文件 全 PASS
附: backtest/run_all5.py STRATEGIES 常量已由退役策略名修正为在产5策略（否则 importlib 断链）

## 根目录（9文件）
- server.py — 前端Web服务(port 80, 运行中 pid 249220)
- execution_core.py — 统一双模执行核心(回测/实盘同一决策代码)
- tick_scheduler.py — bar边界tick分发(hour/5min)
- trading_rules.py — A股涨跌停/交易规则唯一实现
- requirements.txt — 依赖清单
- README.md / PROJECT_STATUS.md / PROGRESS_LOG.md — 项目说明/状态看板/进展日志
- CLEANUP_DELETED_20260802.txt — 本次删除审计快照
- FINAL_STATE_20260802.md — 本清单

## strategies/（9文件 = 在产5+母类2+base+init）
- __init__.py — 包入口(导出Strategy/Signal/SellSignal)
- base.py — 策略基类
- firstboard_low_open_dip_v2.py — S1 首板低吸(在产)
- amplitude_reversal.py — S2 巨振反转(在产)
- gem_star_late_seal.py — S3 创科晚封(在产)
- big_yang_low_open_v2.py — S4 大阳低吸(在产)
- two_board_pullback_dip_h1c.py — S5 双板回调低吸(在产)
- firstboard_low_open_dip.py — S1母类(v2继承依赖)
- two_board_pullback_dip.py — S5母类(h1c继承依赖)

## realtime/（9文件, 红线全保留）— 实盘链路
- config.py — ACTIVE_STRATEGIES 5槽位配置+路径/资金参数
- generate_candidates.py — 每晚22:00候选生成(cron)
- morning_decision.py — 9:25开盘决策(cron)
- intraday_monitor.py — 盘中10秒监控守护(9:25拉起, cron)
- position_tracker.py — 持仓跟踪/auto-close兜底(cron)
- data_feed.py / notify.py — 实时行情/告警通知
- crontab_config.sh / __init__.py — cron配置存档/包入口

## backtest/（12文件, 红线全保留）— 统一回测引擎
- engine.py / portfolio.py / data_feed.py — 引擎/资金池/数据层
- run.py / run_unified.py / run_all5.py / run_combined.py — 运行入口(run_all5已改为在产5策略)
- minute_exit.py / position_scale.py / context_enrichment.py / txt_formatter.py / __init__.py — 分钟卖出/仓位/上下文/明细格式化/包入口

## tools/（31项, 红线全保留）— 运行链工具
- daily_update.sh — 18:30日K更新链(cron)
- fetch_daily_kline.py / fetch_daily_kline_fallback.py / fetch_minute_kline.py — 日K/降级/分钟K抓取
- backfill_hour_tencent.py — 21:30 hour回补(cron)
- backfill_index_kline.py / backfill_index_kline_tencent.py — 指数K回补
- baostock_recovery.py / baostock_recovery_probe.py — BaoStock解禁续跑/探测(cron)
- lhb_backfill.py — 龙虎榜回补(cron)
- generate候选后台: progress_scheduler.py — 10分钟进展监控(cron)
- daily_plan_report.py / daily_review_report.py — 8:45计划/22:10复盘双报告(cron)
- strategy_health.py / drawdown_guard.py — 19:00健康度+回撤手册(cron)
- execution_quality.py — 9:24快照归档/15:10执行质量报告(cron)
- sector_emotion.py / update_red_ratio.py — 板块情绪/红盘比数据链
- periodic_check.sh / periodic_status.sh / periodic_status_updater.sh / status_monitor.sh — 状态巡检脚本
- crontab_backup_*.txt(7) / crontab_new.txt / task71_archive.sh — cron历史存档(小文件留档)

## scripts/（2文件）
- shadow_compare.py — 影子执行差异汇总(intraday_monitor引用)
- generate_trade_details_json.py — 前端交易明细JSON生成

## data/（红线库全保留）
- stocks.db(4.8G) — 日K+hour宽表主库(wal=0/shm=32K, 无需checkpoint)
- minute.db(369M) — 分钟K库
- lhb.db(298M) — 龙虎榜库
- baostock_ban_status.json / fetch_progress.json / minute_fetch_progress.json / limitup_signals_cache.pkl / crontab_backup_20260729_t7*.txt(3) — 数据链状态/缓存/存档
- 已删: research_bj.db、research_t26_min5.db(0字节)、atlas_work/(25M)、根备份py/sh/kline_export.txt

## data/realtime/（44文件）— 实盘运行态
- positions.json / pending_buys.json / drawdown_state.json / intraday_snapshot.json — 持仓/待买/回撤状态/盘中快照(前端软链目标)
- candidates_2026*.json(17, 含周一20260803) / decision_2026*.json(16) — 每日候选与决策存档
- emotion_cycle_daily.csv / emotion_indicators_daily.csv / emotion_phase_daily.csv — 情绪指标公共资产(position_scale口径基准)
- scheduler_alerts.log — 调度告警
- solo_strategy_registry.md — 策略注册表
- QA_AUDIT_LANDING_20260731.md — 落地QA审计(唯一保留的QA报告)
- CLEANUP_FINAL_20260731.txt — 0731清理清单(审计留档)

## logs/（运行日志全保留, 研究日志已清）
- realtime/(15M) — 实盘链路日志+snapshots归档
- backtest/solo/(38文件,42M) — 在产5策略当前口径档案(detail.txt/trades.json/log, 含min/exdiv/preguard变体)
- daily_update_202607*.log(6) / fetch_*.log / lhb_backfill.log / server.log / webserver.log — 运行日志

## frontend/（红线全保留）
- index.html / dashboard.html / reports.html / signal.html / dashboard.js / echarts.min.js — 页面与图表
- data/ — combined_5slot_new_trades.json / unified_5slot_nav.json / strategies_summary.json + 3软链(positions/intraday_snapshot/drawdown_state → data/realtime/)

## reports/ + docs/（红线全保留）
- reports/ — 每日双报告存档+index.json
- docs/ — HANDOVER.md/交易机制/统一流水线/移动止盈实盘操作/strategies策略档案(9)

## 磁盘增长诊断（任务二结论）
- 增长源在工作区外(只报告不动手): /root/.qoder-server 13G + /root/.config 5.6G + /root/.cache 0.5G(IDE agent缓存, 48h内活跃写入) + /var/log/journal 2.8G(journald, 48h内滚动)
- /home/AIWealth 内无增长源: logs 59M、data稳定(stocks.db 7/31后未增)、WAL 0字节健康
- 建议(需用户授权系统层操作): journalctl --vacuum-size=500M 可回收约2.3G; 清理 /root/.qoder-server、/root/.cache 旧版本可回收数GB
