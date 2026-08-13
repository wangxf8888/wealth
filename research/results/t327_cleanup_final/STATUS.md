# Task#327 收尾清理 STATUS

**更新时间**: 2026-08-13 16:40
**阶段**: ✅ 任务完成——删除+事故恢复+三关验收+冒烟全清单67项零FAIL

## 进度总览
| 步骤 | 状态 |
|---|---|
| 1. 白名单构建（crontab 26活跃条目+AST import链+在产资产） | ✅ 完成 |
| 2. DELETE_MANIFEST.md落盘 | ✅ 完成（371行） |
| 3. 删除执行 | ✅ 完成：4257文件/134目录，释放368MB |
| 4. 冒烟1 crontab引用文件存在性 | ✅ 26/26 PASS |
| 5. 冒烟2 核心模块import | ✅ 恢复后20/20 PASS（含v2/h1c继承链断言） |
| 6. 冒烟3 server API | ✅ 9/9真实路由HTTP 200（positions/candidates/decision/equity/status等） |
| 7. 冒烟4 明晨链路文件 | ✅ realtime 8文件+data/realtime就位，candidates_20260813.json在 |
| 8. 三关验收 | ✅ 关1 import+py_compile；关2 solo对拍PASS（S5逐笔0错；S1四处分歧经滚动回测仲裁=数据漂移非代码差异）；关3 git commit 4d94852+冒烟重跑67项零FAIL |

## 最终数字
- 净删除：4255文件/134目录（4257删除-2恢复），净释放磁盘约390MB
- UNSURE保留清单：见DELETE_MANIFEST.md末节（10类，含periodic_*运维脚本、
  backfill_index_kline.py备用回补、crontab历史备份、em_snapshot备源等）
- 冒烟结论：全PASS，生产链完整（22:30滚动回测与明晨9:25链路无阻碍）

## 事故与恢复（已闭环）
- **FAIL项**: strategies/firstboard_low_open_dip.py 与 two_board_pullback_dip.py 被删，
  但在产v2/h1c策略继承它们作基类。git历史/backup均无副本。
- **恢复方式**: /dev/vda3裸设备只读雕刻（514处命中→205候选blob→AST精配），
  两文件均恢复出与DELETE_MANIFEST记录**字节数精确一致**版本（13903B/13506B），
  且确认含Task#299 trading_rules.limit_prices修复（另雕出13464B旧版留档弃用）。
- **验收结果**: 关1 PASS；关2 PASS——S5逐笔0错(仅尾部end日期差1笔未平仓属预期)；
  S1 252笔中4处分歧经昨晚22:30滚动回测（删除前旧代码+新数据）仲裁：4处全部
  与恢复代码一致，分歧源=solo档案8/12 13:55刷新早于18:30日更的数据漂移，
  恢复代码与删除前代码行为逐笔一致；关3 PASS——git commit 4d94852，
  冒烟全清单重跑67项零FAIL（见SMOKE_CHECK.md）。
- **生产影响**: 0（恢复完成时间16:14，早于22:30 nightly_rolling与明晨9:25）。

## 保留决策（leader已批准）
research/results/保留11项 = 7审计留证（t311/t313/t319/t320/t321/t322/t327_cleanup_final）
+ 4运行时依赖（shadow_surge/t228_surge_replay/board_lab/t291_backfill_p2，依赖方见DELETE_MANIFEST §0）。

## 产物
- DELETE_MANIFEST.md（黑名单+UNSURE清单，删除前落盘）
- DELETION_EXECUTED.log（逐条删除留证，0条SKIP-NOTFOUND，含RESTORE记录）
- t327_delete.sh / t327_carve.py / t327_extract.py / t327_refine.py / carve_scan.log（执行器与恢复留证）
- SMOKE_CHECK.md + smoke_raw.txt（67项全PASS原始输出）
- verify_s1.log / verify_s5.log + logs/backtest/solo/t327_verify_*（对拍回测留证）
