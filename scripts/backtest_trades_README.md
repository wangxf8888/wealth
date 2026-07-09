# backtest_scoring_trades.json 字段说明

本文件记录多指标评分量化系统的全部回测交易明细，共3215笔交易。

## 字段列表

| 字段名 | 类型 | 中文含义 | 示例值 |
|--------|------|----------|--------|
| `code` | string | 股票代码（含市场前缀） | "sh.600758" / "sz.000767" |
| `buy_date` | string | 买入日期（YYYY-MM-DD） | "2021-01-06" |
| `buy_price` | float | 实际买入价格（含滑点+0.2%） | 3.507 |
| `buy_hour` | string | 买入时段（hour1~hour4） | "hour1" |
| `sell_date` | string | 卖出日期（YYYY-MM-DD） | "2021-01-07" |
| `sell_price` | float | 实际卖出价格（含滑点-0.2%） | 3.395 |
| `sell_hour` | string | 卖出时段（hour1~hour4） | "hour1" |
| `sell_reason` | string | 卖出原因（策略:详情） | "fixed_tpsl:SL(-3.0%/-3.0%)" |
| `pnl_pct` | float | 本笔盈亏百分比（%） | -3.19 / 18.69 |
| `pnl_amount` | float | 本笔盈亏金额（元） | -6384.77 / 37351.79 |
| `hold_days` | int | 持仓天数（交易日） | 1 / 3 / 5 |
| `shares` | int | 买入股数 | 57000 |
| `strategy_name` | string | 买入策略名称 | "score_buy" |
| `score_at_buy` | float | 买入时综合评分 | 3.0 / 4.5 |
| `star_count` | int | 买入时星级（亮星数量） | 3 / 4 / 5 |
| `preclose_buy` | float | 买入日前收盘价（用于计算涨跌停） | 3.49 |
| `preclose_sell` | float | 卖出日前收盘价（用于计算涨跌停） | 3.52 |

## sell_reason 字段格式说明

格式为 `卖出策略:具体原因`，常见值：

- `fixed_tpsl:TP(+18.9%/10.0%)` — 固定止盈触发，实际涨幅18.9%，阈值10%
- `fixed_tpsl:SL(-3.0%/-3.0%)` — 固定止损触发，实际跌幅-3.0%，阈值-3%
- `time_limit:MAX_HOLD(5d)` — 达到最大持仓天数5天强制卖出

## 数据覆盖范围

- 回测区间：2021-01-01 ~ 2026-06-30
- 总交易笔数：3215
- 策略：多指标评分系统（score_buy）
- 仓位模式：5仓位并发持股
