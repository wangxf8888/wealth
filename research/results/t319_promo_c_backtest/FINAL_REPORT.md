# Task#319 批准C落地A：回测侧双改动转正+档案看板刷新 — 结题报告

- 席位：Tobias | 完成：2026-08-12午后 | 用户已批准方案C（晋级率过热门控+S5 h1_touch触板兑现上产）
- 规格源（一字不改）：门控=t311_promo_menu(t311_indicators.py纯SQL+t311_runner.py注入,格C) | h1_touch=t307 METHOD_REPORT§8+t307_engine_runner.py分支

## 1. 正式转正跑 — 锚逐位复现 ✅

- 命令（生产默认CLI路径，一跑同验步骤1+2）：`nice -19 python3 -m backtest.run_unified --strategies <五策略> --start 2021-01-01 --end 2026-07-01 --no-frontend`
- 结果：**CAGR 182.22 / MDD 20.25 / Calmar 9.00 / 2893笔** — t311格C锚逐位复现
- S5 = 1157笔（expired 1074 + touch_board 83）逐位=锚；summary 14字段与t311 run_combo.json逐位一致
- 分年：231.99 / 125.84 / 127.52 / 122.27 / 378.33 / 57.37 全正
- 前置快速验证双PASS（t319_verify_gate.py）：指标行1336日×2列与t311 indicators_daily.csv逐位一致；门控日历∩引擎交易日=131天，分年{2021:4,2022:34,2023:15,2024:43,2025:28,2026:7}逐位=G1终格
- 新基线组合档案：logs/backtest/unified_5slot_{trades.json,detail.txt}
- 日志：RUN_LOG_official.txt（EXIT=0）

## 2. 改动文件清单 + 备份

| 文件 | 改动 | 备份 |
|---|---|---|
| backtest/promo_gate.py | 新建（t311纯SQL口径+GateScaler注入逐字移植, sqlite ro） | 新文件无备份 |
| backtest/run_unified.py | promo_gate接线：API参数默认None(向后兼容)+CLI --promo-gate默认p70:0.3(off/none关闭)+模式打印；引擎本体零改动(gate>boost靠既有native分支) | backup/run_unified.py.bak_20260812_t319 |
| strategies/two_board_pullback_dip_h1c.py | h1_touch should_sell覆盖（t307 S5H1Touch逐字），回滚=删除覆盖 | backup/two_board_pullback_dip_h1c.py.bak_20260812_t319 |
| PROJECT_STATUS.md | 看板4处（见§4） | backup/PROJECT_STATUS.md.bak_20260812_t319 |
| data/realtime/solo_strategy_registry.md | S5行刷新（locked_edit同锁） | backup/solo_strategy_registry.md.bak_20260812_t319 |
| research/ideas/README.md | #94/#115转正note×2（registry_append PASS） | backup/ideas_README.md.bak_20260812_t319 |

红线守卫：实盘侧(realtime/,execution_core.py,server.py)与前端零触碰；官方/solo跑均--no-frontend；sqlite全程ro；零BaoStock；nice -19。
勘察实证：realtime/*.py零调用strategy.should_sell()（S5实盘卖出走position_tracker sell_mode='timed'自有路径）→h1_touch策略类改动仅回测侧生效，实盘侧由#320落地。

## 3. S5 solo档案刷新 ✅

- 口径：h1_touch + 纯策略（--position-scale off --dd-boost off --promo-gate off），2021-01-01~2026-08-11
- 新档案：**+252.74% / MDD 78.78% / 1192笔 / 胜率47.82% / +0.85%/笔**（touch_board 84）
- 旧档案（d2h1_close, 至8/7, +203.96%）备份：logs/backtest/solo/two_board_pullback_dip_h1c{_trades,_detail,}_preh1touch_20260812.*
- log头已加口径版本注记

## 4. 看板与注册表行号

- PROJECT_STATUS.md：L21 S5行(+252.74%+h1_touch注记) / L25 组合基线行(新基线182.22/20.25/9.00/2893, 旧基线171.73与173.10对照注记保留) / L26 晋级率门控总览注记(新增bullet) / L55 #319任务区块
- research/ideas/README.md：**L267**(#94门控转正note, registry_append PASS回读) / **L268**(#115 h1_touch转正note, PASS回读)
- data/realtime/solo_strategy_registry.md：**L19** S5行（locked_edit）

## 5. 滚动链兼容结论（只列不改，详单ROLLING_CHAIN_COMPAT.md）

- **P0×2**：①tools/weekly_solo_refresh.sh第81行缺`--promo-gate off`（周日20:00 cron会污染solo纯策略口径）②tools/daily_rolling_backtest.py run_one()未传promo_gate+锚数字171.73过时+caliber标签需升级（今晚22:30滚动将是"半新口径"：有h1_touch无gate，建议今日内同步）
- P1×4：run_all5.py(遗留无overlay脚本，仅需docstring注明)/backtest_vs_live_compare.py(#320落地后自然对齐)/progress_scheduler.py(零动作)/run_combined.py(资金隔离老模式无overlay层，h1_touch经策略类自动生效，能力边界内已=新基线，零动作)
