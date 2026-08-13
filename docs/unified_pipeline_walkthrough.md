# 统一调度体系全链路演练 (Task#45)

> 用户裁决版架构: "同一套体系, 每5min决定一次信号; hour级策略等hour边界再决定,
> 5min级策略每周期决策"。**实盘当前保持hour级体系**(现有5策略参数为hour口径
> 校准), 5min级策略是未来独立体系, 经完整验收后再替换。
>
> 本文档按一个交易日的时间轴走一遍全链路, 每步标注:
> ①代码位置 ②决策频率归属 ③回测/实盘是否"同一份代码"。

## 0. 架构三根支柱

| 支柱 | 位置 | 作用 |
|---|---|---|
| 决策频率声明 | `strategies/base.py` `Strategy.decision_interval='hour'`(类属性默认) | 策略自己声明决策频率; 现有5策略继承默认即hour, 零改动。未来5min策略只需声明`decision_interval='5min'` |
| tick分发框架 | `tick_scheduler.py`(项目根) | 边界判定唯一来源: `day_ticks(interval)`产tick流, `strategy_due(decision_interval, tick)`判"这个tick是否是该策略的决策边界"。回测与实盘同源(`BAR_TIMES`来自`execution_core`) |
| 共享执行核心 | `execution_core.py` | 买入侧`evaluate_open_entry`(EntryEngine) + 卖出侧`ExitEngine`(Task#27), 回测/实盘各自适配行情、决策代码同一份 |

tick语义速查(`tick_scheduler.day_ticks`):
- `'hour'`: 4个tick/日, 每个tick双角色(`is_hour_start`=决策边界,
  `is_hour_end`=记账边界) —— 与历史`for hour in (1,2,3,4)`循环逐语句等价。
- `'5min'`: 48个bar tick/日; hour首bar(0935/1035/1305/1405)是hour级策略
  决策边界, hour尾bar(1030/1130/1400/1500)是`tick_hour`记账边界。
- 单元测试: `scripts/test_tick_scheduler.py`(19断言, 含48时刻逐一边界判定)。

---

## 1. 22:00 晚间候选生成

- **cron**: `0 22 * * 1-5 python3 realtime/generate_candidates.py`
- 产物: `data/realtime/candidates_YYYYMMDD.json`(按slot分策略候选)
- 决策频率归属: 日级准备工作, 不属任何盘中tick; 候选口径与回测策略
  `get_candidates`同一份策略类代码(动态加载`ACTIVE_STRATEGIES`声明的类)。

## 2. 9:25 统一入场决策 (= hour级策略的"日开盘tick")

- **实盘**: cron `25 9 * * 1-5 python3 realtime/morning_decision.py`
  - `morning_evaluate()`逐候选: live复筛(`strategy.get_candidates`)后, 入场
    守卫调 **`execution_core.evaluate_open_entry`** (`realtime/morning_decision.py`
    守卫段): 除权除息拦截 → 涨停开盘拦截 → 通过则给出EntryDecision。
  - 实盘适配: `exchange_preclose=行情昨收`, `prev_close=signal_close`,
    `limit_basis=signal_close`(涨停基准=信号日close, 历史口径精确保留)。
- **回测**: `backtest/run_unified.py`买入循环(hour=1的tick) 与
  `backtest/engine.py`买入守卫, 调 **同一份`evaluate_open_entry`**。
  - 回测适配: `open_price=get_hour_open`, `exchange_preclose=get_day_preclose`,
    `prev_close=get_prev_day().close`, 不传`limit_basis`(基准=行情昨收)。
- **同一份代码**: `execution_core.evaluate_open_entry`(守卫顺序/数学口径唯一)。
- 决策频率归属: hour级策略在**日开盘tick**的入场决策(概念上=当日第一个
  决策边界); 用`OpenRangeRule`(execution_core)可把该语义显式表达为
  "当日首bar 0935的开盘区间入场"——未来5min体系直接复用。
- **一致性守护**: `scripts/test_entry_consistency.py`
  (17断言: 合成用例×双侧调用签名 + 2026-07-24真实5197只双路径全一致 +
  近30日真实除权样本20只双拒)。

## 3. 9:25-15:00 盘中监控与卖出

- **实盘daemon**: cron `25 9 * * 1-5 python3 realtime/intraday_monitor.py`
  (10秒轮询)。卖出路径三态(`USE_EXECUTION_CORE`环境变量):
  - `0`(默认, off): 旧路径`pt.evaluate_position`日级区间判定(已验证);
  - `shadow`: **影子模式** —— 旧路径执行 + 新路径(tick→`LiveBarAggregator`
    →共享`ExitEngine.on_bar`)并行纯观察, 触发事件与比对写
    `logs/realtime/shadow_exit_YYYYMMDD.jsonl`;
  - `1`(on): 新路径执行(`poll_positions_core`, 正式切换态)。
- **回测**: `ExitEngine.run_stream(MinuteDbBarStream)`(--minute-exit)或hour级
  区间判定 —— bar级挂单语义与实盘`poll_positions_core`**同一份ExitEngine**。
- 决策频率归属: **卖出监控不是tick决策**——语义=hour级挂单(条件单)的及时
  执行, 持续监控不改变hour级口径; 一致性守护: `scripts/test_task27_consistency.py`。
- **(预留)5min买卖决策点**: `intraday_monitor.on_5min_boundary(time_end, today)`
  —— 主循环检测5min bar窗口切换时回调(空实现)。未来5min级策略在此按
  `strategy_due('5min', tick)=True`逐bar决策; hour级策略永不经此处。

## 4. 15:00 收盘

- daemon自然退出(`SESSION_END='15:00'`), 删心跳;
- 18:30 `tools/daily_update.sh`日K更新 → 22:00回到第1步。

---

## 5. 回测侧同一时间轴的对应关系

`backtest/run_unified.py`主循环(engine.py同款; Task#54起engine.py对
5min策略fail-fast, bar级链路仅run_unified实现):

```
for date in 交易日:
    bar_candidates = 5min策略候选.bar_day_prescreen(当日必要条件剪枝)  # Task#54性能通道
    for tick in tick_scheduler.day_ticks(self.tick_interval):   # 默认'hour'
        if 存在5min策略: _process_bar_tick(tick)  # Task#54 bar级分发(每tick)
            卖出: strategy.should_sell(每tick) + T+1/跌停兜底 + _exec_price_bar
            买入: strategy.should_buy_bar(code,date,tick,...)   # bar级决策入口
                  数据=get_bars_until(时刻裁剪: bar 1..N-1完整+bar N开盘)
                  守卫=evaluate_open_entry(open=bar N开盘)      # 与实盘同函数
                  成交=bar N开盘价, 钳制bar N真实区间(_exec_price_bar)
        if not tick.is_hour_start:          # hour级策略无决策点
            if tick.is_hour_end: tick_hour() # 记账边界
            continue
        卖出循环: strategy_due门控(bar级slot跳过, 已在上方处理)
        买入循环(hour==1 ↔ 实盘9:25):
            evaluate_open_entry(...)         # ↔ 实盘morning_decision同函数
            strategy.should_buy(...)         # 同一份策略类
        if tick.is_hour_end: tick_hour()
```

CLI开关: `--tick-interval {hour,5min}`(默认hour=关闭态, 行为与改造前
逐语句等价)。5min级策略三防线fail-fast: hour驱动拒绝/engine.py拒绝/
未实现should_buy_bar拒绝(防Task#53缺口①式静默降级)。

### 5.1 Task#54 bar级链路契约(5min策略开发者必读)

- **决策时点**: tick=bar N开始时点(tick.time_end为该bar结束时刻),
  每tick调用`should_buy_bar(code, date, tick, data_feed, portfolio)`。
- **可见数据**: `data_feed.get_bars_until(code, date, tick.time_end)`
  → (bar 1..N-1完整5元组列表, bar N开盘价)。未来数据(bar N的h/l/c及
  之后bars)在数据层封死; 覆盖缺口→(None, 0.0)并计入命中率统计。
- **成交语义**: 信号bar(N-1)收盘确认 → bar N开盘价成交 = 研究口径
  "次一bar开盘买"(A2保守式)。"当bar开盘买"语义=策略只用bar N开盘判定。
  成交价钳制到bar N真实区间(`_exec_price_bar`), 不再被hour区间钳制。
- **守卫**: evaluate_open_entry(open=bar N开盘)统一拦截除权/涨停开盘,
  与hour级回测、实盘9:25同一份函数。
- **性能**: 必须实现`bar_day_prescreen`(信号必要条件的日级剪枝,
  被剪code任何tick无信号→无未来函数), 否则2100候选×48tick不可跑。
  reseal实测: 预筛后全量637.7s(Task#53无预筛37.5min, 3.5×提速)。
- **ST判定**: 统一走`trading_rules.is_st_stock(code, name, isST)`双检
  (isST字段存在脏样本: sh.600053/sh.600340均isST=0但名称*ST)。

## 6. 回归与验收证据

| 验证 | 命令 | 结果 |
|---|---|---|
| tick边界单测 | `python3 scripts/test_tick_scheduler.py` | 19断言PASS |
| 入场一致性 | `python3 scripts/test_entry_consistency.py` | 17断言PASS(5197只真实样本) |
| 卖出一致性(Task#27) | `python3 scripts/test_task27_consistency.py` | 全PASS(改造后复跑确认) |
| 影子模式 | `python3 scripts/test_shadow_mode.py` | 3断言PASS(只执行旧路径/差异记日志/无泄漏) |
| 实盘9:25冒烟 | `morning_decision.py --test-date 2026-07-24` | decision JSON与改造前逐字段一致(仅时间戳不同) |
| hour级零侵入回归 | run_unified全量(2021-01-01~2026-07-23, 5策略) vs Task#40基线 | 见`scripts/t44_compare.py regression`输出(逐笔+逐日nav+summary全等) |
| bar级链路单测(Task#54) | `python3 scripts/test_bar_link.py` | 26断言PASS(时刻裁剪防未来数据/单bar读取/三防线fail-fast) |
| bar级验收对账(Task#54) | `python3 scripts/t54_reconcile.py` | 引擎15笔与研究A2清单逐笔逐分对齐(买入价零差/收益=毛-成本零误差); 24→15差异归因100%: 8笔slot=1同日竞争(其中7笔集中2024-09-30)+1笔*ST华幸名称双检拦截(引擎比研究更正确) |

---

## 7. 周一影子比对操作清单 (2026-07-27)

前置: 实盘卖出主路径仍为旧路径(cron里daemon无env注入=off)。影子模式需
显式开启, **只影响日志不影响执行**, 风险为零。

1. **周一9:20前**(或前一晚)修改daemon的cron行, 注入影子env:
   ```
   25 9 * * 1-5 cd /home/AIWealth && USE_EXECUTION_CORE=shadow python3 realtime/intraday_monitor.py >> logs/realtime/intraday_monitor.log 2>&1
   ```
   (只改这一行; morning_decision/position_tracker等cron不动)
2. **9:26 确认启动日志**:
   `grep "卖出路径" logs/realtime/intraday_monitor.log | tail -1`
   应显示 `卖出路径=旧路径执行+ExitEngine影子比对`。
3. **盘中无需干预**。若当日有持仓触线, 日志会出现`[影子比对]`行;
   事件明细在 `logs/realtime/shadow_exit_20260727.jsonl`。
4. **15:05 收盘后汇总**:
   ```
   python3 scripts/shadow_compare.py --date 2026-07-27
   ```
   - `PASS`(零差异) → 周二切换: cron该行env改为`USE_EXECUTION_CORE=1`;
   - `HOLD`(有差异) → 逐条归因(脚本头部注释列了已知可解释差异类别:
     启动首轮日级兜底legacy-only属预期), 未归因不切换, 继续shadow观察。
5. **周二切换后首日**: 保留旧路径代码不删(回退=env改回`0`), 观察一日
   `[ExitEngine]`触发日志与`position_tracker`账目一致后, 影子阶段结束。

注意: 当日若无持仓或无触线, jsonl不生成 → shadow_compare报"样本为空的
零差异", 建议继续shadow到出现真实触发样本再裁决切换。

---
*Task#45 交付物之一 | Bill | 2026-07-25*
