# Task#324 滚动链P0口径同步 — 修复日志

> 席位：Tobias | 执行：2026-08-12 午后（22:30滚动跑前收口） | 承#319 ROLLING_CHAIN_COMPAT.md P0×2
> 红线守卫：只动两个调度脚本；实盘侧(realtime/,execution_core.py — Duke#320)与前端(Wilbur#322)零触碰；sqlite ro

## 备份清单（改前，md5与原文件核对一致后再动手）

| 备份文件 | 大小 |
|---|---|
| backup/weekly_solo_refresh.sh.bak_20260812_t324 | 8419 B |
| backup/daily_rolling_backtest.py.bak_20260812_t324 | 24850 B |

## P0-1 tools/weekly_solo_refresh.sh（周日20:00 cron）

- **第82行**（原81行区段CMD数组）：`--position-scale off --dd-boost off` → 追加 `--promo-gate off`
  （solo档案=纯策略口径，三overlay必须显式关闭，防#319后CLI默认p70:0.3污染周日solo刷新）
- **第15-18行**口径纪律注释：补promo-gate off说明（Task#319起默认开启）
- **验证**：
  - `bash -n` 语法检 PASS
  - `grep`确认：L15注释 + L82 CMD行均含 `--promo-gate off`
  - `--dry-run`实跑：5策略命令全部带 `--position-scale off --dd-boost off --promo-gate off`

## P0-2 tools/daily_rolling_backtest.py（工作日22:30 cron经nightly_rolling.sh）

改动5组（键名/产物文件名全部稳定不动，沿#204"内容升级键名不动"模式）：

1. **L63 新增常量** `PROMO_GATE = 'p70:0.3'`（ICE_SPEC/DD_BOOST旁，规格冻结链#244→#249→#250→#311注记）
2. **run_one()** 加 `promo_gate=None` 形参 → 透传 `run_unified_backtest(..., promo_gate=promo_gate)`；
   label三态升级：`production_R2(ice...+boost...+gatep70:0.3)`
3. **main()** 生产口径块b调用：`run_one(ICE_SPEC, ..., dd_boost=DD_BOOST, promo_gate=PROMO_GATE)`；
   无overlay对照块a不变（纯策略对照语义保留）
4. **锚更新**：L12-14 docstring 171.73/26.50/6.48/2889 → **182.22/20.25/9.00/2893**（旧#204锚降级为存档对照注记保留）；
   L72 slot改序警示注释同步（新基线锚182.22为主，旧锚保留）
5. **caliber标签**：前端summary `caliber` → `生产口径R2(ice35:0.3:repair_exempt+boost15:1.5+gatep70:0.3)`；
   `ice_overlay`子键内加 `promo_gate: 'p70:0.3'` 字段（键名'ice_overlay'/'caliber'不动防消费方断裂）；
   md报告表头行同步为 `生产口径R2(ice+boost+gate)`

- **验证**（短窗口试跑 2026-08-05~2026-08-11, `--skip-frontend --tag t324smoke`, nice -19, EXIT=0, 184.4s）：
  - `py_compile` PASS
  - 日志出现 `[promo-gate] p70:0.3: 指标重算+expanding日历构建完成 ... 全历史过热作用日134天`
    —— 与#319官方转正跑RUN_LOG_official.txt同数字（134天全历史口径），门控确认生效
  - 口径label：`production_R2(ice35:0.3:repair_exempt+boost15:1.5+gatep70:0.3)`（json两块label核对通过）
  - 非生产守卫全held：跳过meta写入/前端summary/组合trades刷新（生产meta零触碰）
  - 短窗口无gate作用日（8/5~8/11无过热日），两口径同值+810.18%属预期（gate只在过热日缩仓）
  - 试跑产物：logs/rolling/rolling_20260811_t324smoke.{json,md}（带tag调试产物，可留档可清理）

## 锚断言说明

daily_rolling本体无硬断言（锚在docstring/注释中作人读参照），本次已全部更新为新基线182.22/20.25/9.00/2893；
真锚验证=今晚22:30全量生产跑的CAGR应逐位复现182.22（数据end延伸至8/11后数字自然漂移属正常，
与#319官方跑2021-01-01~2026-07-01锁定窗口口径的对比以meta ΔCAGR报告呈现）。

**注**：任务书笔误"backtest/daily_rolling_backtest.py"，实际路径=tools/daily_rolling_backtest.py
（与ROLLING_CHAIN_COMPAT.md权威清单一致，backtest/下无此文件）。

## 结论

P0×2已收口，今晚22:30 nightly_rolling将以完整新基线口径（ice+boost+gate+h1_touch）运行；
周日20:00 weekly_solo刷新不会再污染solo纯策略口径。P1×4维持#319零紧急认定不动。
ROLLING_CHAIN_COMPAT.md已标注✅收口+日期。
