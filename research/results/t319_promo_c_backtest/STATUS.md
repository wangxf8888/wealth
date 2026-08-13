# Task#319 批准C落地A：回测侧双改动转正+档案看板刷新 — STATUS

- 席位：Tobias | 开始：2026-08-12
- 规格源（一字不改）：门控=t311_promo_menu(t311_indicators.py纯SQL+t311_runner.py注入,格C) | h1_touch=t307 METHOD_REPORT§8+t307_engine_runner.py分支
- 锚（必须逐位复现）：CAGR 182.22 / MDD 20.25 / Calmar 9.00 / 2893笔
- 红线：实盘侧(realtime/,execution_core.py,server.py)零触碰 | 前端零触碰 | sqlite ro | 零BaoStock | nice -19

## 进度

- [x] 阶段0完成：任务书TaskGet读取、t311/t307规格源逐行研读、
  run_unified/position_scale/strategies耦合勘察完成。
  关键勘察结论：realtime/*.py 全目录零调用 strategy.should_sell()（S5实盘卖出走
  position_tracker sell_mode='timed'自有路径）→ 策略类h1_touch改动仅回测侧生效，
  不越Duke#320边界。
- [x] 阶段1完成：backtest/promo_gate.py新建(t311口径逐字移植) + strategies/two_board_pullback_dip_h1c.py
      h1_touch覆盖(t307 S5H1Touch逐字) + run_unified.py默认接线(--promo-gate默认p70:0.3)。
      备份5件：backup/{run_unified.py,two_board_pullback_dip_h1c.py,PROJECT_STATUS.md,
      solo_strategy_registry.md,ideas_README.md}.bak_20260812_t319。py_compile三件全过。
- [x] 阶段2完成：t319_verify_gate.py 双PASS —
      V1 指标行1336日x2列与t311 CSV逐位一致；
      V2 门控日历∩引擎交易日=131天, 分年{2021:4,2022:34,2023:15,2024:43,2025:28,2026:7}逐位=G1终格。
- [x] 阶段3完成（~12:00）：官方转正跑（生产默认CLI路径, --no-frontend）EXIT=0，
      锚逐位复现：CAGR 182.22 / MDD 20.25 / Calmar 9.00 / 2893笔；
      S5 1157笔(expired 1074+touch_board 83)；summary 14字段与t311 run_combo.json逐位一致；
      分年 231.99/125.84/127.52/122.27/378.33/57.37；新基线组合档案=logs/backtest/unified_5slot_*。
      RUN_LOG_official.txt存档。
- [进行中] 阶段4：S5 solo档案h1_touch口径重跑（2021-01-01~2026-08-11, 三off纯策略口径,
      旧档案已备份 logs/backtest/solo/*_preh1touch_20260812.*）
- [ ] 阶段5：PROJECT_STATUS.md看板 + 双注册表登记（registry_append.py PASS回读）
- [x] 阶段6勘察完成（清单已固化ROLLING_CHAIN_COMPAT.md, 只列不改）：
      P0×2 = weekly_solo_refresh.sh第81行缺--promo-gate off（周日cron会污染solo口径）+
      daily_rolling_backtest.py run_one未传promo_gate（今晚22:30滚动为半新口径）。
- [x] 阶段4完成（~12:55）：S5 solo重跑EXIT=0 → +252.74%/MDD 78.78%/1192笔(touch_board 84),
      旧档案*_preh1touch_20260812备份, log头口径注记。
- [x] 阶段5完成（~13:10）：看板4处(L21/L25/L26/L55) + README.md两条note PASS回读(L267/L268) +
      solo_strategy_registry.md L19(locked_edit)。
- [x] 阶段6结题：FINAL_REPORT.md + ROLLING_CHAIN_COMPAT.md固化。任务完成。

---

# Task#324 滚动链P0口径同步 — STATUS（同席位Tobias, 2026-08-12午后）

- [x] 备份2件: backup/{weekly_solo_refresh.sh,daily_rolling_backtest.py}.bak_20260812_t324 (md5核对一致)
- [x] P0-1 weekly_solo_refresh.sh: L82 CMD追加--promo-gate off + L15-18口径纪律注释;
      bash -n PASS + --dry-run 5命令全带三off。
- [x] P0-2 daily_rolling_backtest.py: PROMO_GATE='p70:0.3'常量 + run_one透传promo_gate +
      main生产块b传参 + 锚更新182.22/20.25/9.00/2893(旧锚存档对照) + caliber标签升级含gate(键名不动);
      py_compile PASS + 短窗口试跑(8/5~8/11, tag=t324smoke, EXIT=0)日志[promo-gate] p70:0.3生效,
      label=production_R2(...+gatep70:0.3), 非生产守卫全held。
- [x] ROLLING_CHAIN_COMPAT.md: P0标题/两条目/结论标注✅已收口+日期, P1×4原文不动。
- [x] P0_FIX_LOG.md固化(备份清单+diff摘要+验证证据)。任务完成, 今晚22:30滚动=完整新基线口径。
