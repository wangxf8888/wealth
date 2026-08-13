# EntryEngine 买入侧接入设计（Task#27 交付件，只设计不实现）

> 目标：让 Lee 正在研究的 5min 级策略（如 hour1 半路板：bar N 突破 → bar N+1 开盘买入）
> 天然双模可执行——研究回测与实盘执行走**同一份决策代码**，只是 BarStream 数据源不同。
> 卖出侧已由 `execution_core.ExitEngine` 落地验证（一致性守护测试三案例双路径逐字段一致），
> 买入侧沿用同一框架，本文档为 Task#24/后续实现任务的设计输入。

## 1. 定位与边界

```
                     ┌──────────────────────────────┐
                     │        execution_core        │
   BarStream ──bar──▶│  EntryEngine   ExitEngine    │──决策──▶ 执行层
   (数据源可插拔)     │  (买入决策)     (卖出决策,已落地) │        (回测: portfolio记账
                     └──────────────────────────────┘         实盘: close/open_position_locked)
   回测: MinuteDbBarStream(minute.db, final bar)
   实盘: LiveBarAggregator(10s行情tick → 进行中5min bar)
```

- EntryEngine 只产出 `EntryDecision`（买什么/何时/什么价/什么原因），**不碰资金与仓位**。
  记账归执行层：回测归 portfolio（Task#24 接入），实盘归 morning_decision/daemon 的下单确认链路。
- 候选池不归 EntryEngine：日级候选仍由 22:00 generate_candidates 产出（策略 pick_candidates 逻辑不变），
  EntryEngine 消费"当日候选 + bar流"做**盘中择时**这一件事。

## 2. 数据结构（与 ExitDecision 对称）

```python
@dataclass
class EntryDecision:
    code: str
    strategy: str          # 策略name
    slot_id: str
    action: str            # 'buy'
    buy_price: float       # 语义成交价(见§4)
    buy_date: str
    buy_time: str          # 触发bar time_end
    trigger_ref: float     # 参考触发线(突破价/区间边界)
    reason: str            # 'breakout_next_open' | 'open_range' | ...
```

## 3. EntryRule 接口：策略以"bar级买入规则"声明接入

策略类新增可选接口（strategies/ 不动，新策略按此声明；老的 hour 级 should_buy 不受影响）：

```python
class EntryRule(ABC):
    """单标的bar级买入规则。EntryEngine每根bar调用一次, 状态机自持。"""
    @abstractmethod
    def on_bar(self, bar: Bar) -> Optional[EntryDecision]: ...
```

内置两个通用规则（覆盖已知形态需求）：

### 3.1 BreakoutNextOpenRule —— hour1 半路板类（Lee 的 T26 方向）
```
状态机: WATCHING → ARMED → DONE
  WATCHING: bar.is_final 且 bar.high >= breakout_price(如首板日高点/涨停价-ε)
            且 bar.time_end <= watch_deadline(如'1030', hour1内)  → ARMED
  ARMED:    下一根bar的open到达 → EntryDecision(buy_price=next_bar.open,
            reason='breakout_next_open')                          → DONE
```
关键语义：**突破确认必须用 final bar**（进行中bar的high可能是未定稿的假突破——
实盘tick流里bar内冲高回落，final后high不变才作数）；成交在 N+1 bar open，
回测与实盘天然同一执行点，无前视。

### 3.2 OpenRangeRule —— 现有低开系（S4/S5）口径的bar级表达
```
09:25竞价/0935首bar open 相对昨收(或信号收盘)的open_rate ∈ 策略声明区间
→ EntryDecision(buy_price=首bar open, reason='open_range')
```
与现行 morning_decision 的 9:25 决策语义一致，作为迁移锚点（先并行影子运行比对，后切换）。

## 4. 语义成交价（与 ExitEngine 同一套规则）

- 触发点在 bar N（final确认）→ 成交在 bar N+1 **open**（下一可执行时点，无前视）；
- 限价买入场景（如"回落到X买入"）：bar open ≤ X → 按 open 成交（跳空有利），
  bar low ≤ X < open → 按 X 成交（盘中触及）——与 ExitEngine 触发线语义镜像对称；
- 买入成交价一律 `trading_rules.clamp_price_to_bar` 钳制到该bar真实区间；
- 框架级拦截（对称于卖出侧跌停拦截）：目标价已达**涨停**（买单排不上）→ 本bar放弃/顺延，
  用 `trading_rules.is_at_limit_up`；
- T+0 约束在卖出侧已有（ExitEngine），买入侧无需处理。

## 5. 双模驱动方式

| | 回测 | 实盘 |
|---|---|---|
| bar源 | MinuteDbBarStream(final bar) | LiveBarAggregator(进行中bar, 10s tick) |
| final判定 | 天然final | bar切换时上一bar定稿(is_final=True)才喂突破确认 |
| 成交 | 下一bar open(库内已知) | bar切换后新bar首tick≈open(10s粒度, 偏差<一个tick) |
| 资金/槽位 | portfolio.get_nav()/n_slots | positions.json 动态再平衡, 现金池检查 |
| 幂等 | 无需 | buying_locked(镜像selling_locked), daemon/morning_decision先到者锁定 |

实盘挂载点：intraday_monitor 主循环在 `poll_positions` 后增加 `poll_entries`
（feature开关 `USE_ENTRY_ENGINE`，默认False），对当日候选逐code维护
`{code: EntryRule}` 运行态；产出 EntryDecision 后走新函数
`open_position_locked`（在 position_tracker 实现，镜像 close_position_locked：
资金检查→锁定→写入positions→企微通知）。

## 6. 一致性守护（沿用 Task#27 模式）

新增 `scripts/test_entry_consistency.py`：选3个历史突破案例（minute.db有数据），
同一数据分别走"回测路径"（MinuteDbBarStream）与"实盘路径"（bar拆tick→LiveBarAggregator），
断言 EntryDecision 逐字段一致。**任何 EntryRule 合入前必须过此测试**——
这是本框架对"双实现漂移"的 CI 级防线，买卖两侧同一标准。

## 7. 实施顺序建议（归后续任务，本文档不实现）

1. Task#24: ExitEngine 接入回测引擎循环（日级hour模拟 → 5min bar精化）；
2. EntryEngine 骨架 + BreakoutNextOpenRule + 一致性测试（Lee 的 T26 策略若成形，作为首个用户）；
3. OpenRangeRule 影子运行：与 morning_decision 并行比对一周，差异为零后切换；
4. buying_locked/open_position_locked 实盘链路（含涨停拦截、现金池检查）。
