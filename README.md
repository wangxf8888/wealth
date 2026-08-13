# AIWealth - A股量化5策略组合交易系统

## 项目概述

A股量化5策略组合交易系统，采用**统一资金池等权再平衡模式**运行。

| 指标 | 数值 |
|------|------|
| 组合CAGR | **196.71%** |
| 最大回撤 | 24.82% |
| 总交易笔数 | 2082 |
| 胜率 | 54.32% |
| 回测区间 | 2021-01 ~ 2026-07 |

## 5个生产策略

| Slot | 策略模块 | 中文名 | 单策略CAGR | 笔数 |
|------|----------|--------|-----------|------|
| S1 | limitup_early_seal | 涨停早封低开 | 107% | 288 |
| S2 | amplitude_reversal | 振幅反转 | 122% | 507 |
| S3 | gem_star_late_seal | 创科尾封 | 152% | 462 |
| S4 | big_yang_low_open_v2 | 大阳低开V2 | 599% | 574 |
| S5 | gem_star_limitup_low_open | 创科涨停低开 | 218% | 458 |

## 目录结构

```
AIWealth/
├── strategies/        # 5个策略模块 + base基类
├── backtest/          # 统一回测引擎（小时级推进）
├── realtime/          # 实盘交易系统（候选生成+早间决策+持仓跟踪）
├── frontend/          # Web仪表盘（ECharts图表+深色主题）
├── tools/             # 数据更新工具（BaoStock日K+60min K线）
├── scripts/           # 研究与辅助脚本
├── docs/              # 策略文档
├── data/              # SQLite数据库（需本地初始化）
│   ├── stocks.db      # 日K+小时K线数据
│   └── realtime/      # 实盘持仓与候选数据
├── logs/              # 运行日志
├── server.py          # Flask Web服务（端口80）
└── PROJECT_STATUS.md  # 项目状态看板
```

## 快速部署

### 1. 环境准备

```bash
git clone <repo_url> ~/AIWealth
cd ~/AIWealth
pip install -r requirements.txt
```

**依赖清单** (`requirements.txt`)：
- flask - Web服务
- pandas - 数据处理
- numpy - 数值计算
- baostock - A股数据源

**Python版本**：3.8+

### 2. 数据初始化

```bash
# 创建日志目录
mkdir -p logs/backtest logs/realtime

# 初始化股票数据库（从BaoStock获取日K+60min K线）
# 首次运行需要较长时间（全量约5000只股票）
python tools/fetch_daily_kline.py 2021-01-01 2026-07-21

# 单日增量更新
python tools/fetch_daily_kline.py 2026-07-21
```

数据写入 `data/stocks.db`，包含 `stock_kline` 表（日K + 4个小时段OHLC）。

### 3. 运行回测验证

```bash
# 5策略统一资金池组合回测（推荐首次运行验证环境正确性）
python -m backtest.run_unified

# 指定策略和时间范围
python -m backtest.run_unified \
    --strategies big_yang_low_open_v2,gem_star_limitup_low_open,gem_star_late_seal,amplitude_reversal,limitup_early_seal \
    --start 2021-01-01 --end 2026-07-15

# 单策略回测
python -m backtest.run --strategy limitup_early_seal --slots 1 --start 2021-01-01 --end 2026-07-15
```

回测结果输出至 `logs/backtest/` 目录。

### 4. 启动Web服务

```bash
# 启动Flask服务（监听0.0.0.0:80，需root权限或端口转发）
nohup python server.py > logs/webserver.log 2>&1 &

# 访问仪表盘
# http://<your-ip>/
```

仪表盘包含：资金曲线、策略分布、持仓状态、历史交易明细。

### 5. 配置定时任务（实盘）

```bash
crontab -e
```

添加以下定时任务（仅交易日运行）：

```cron
# 盘后18:30 - 更新日K线数据
30 18 * * 1-5 /bin/bash /home/AIWealth/tools/daily_update.sh

# 盘后21:30 - 生成次日候选股（需等待日K数据更新完成，通常18:30~20:55；脚本内置完整性门兜底）
30 21 * * 1-5 cd /home/AIWealth && python3 realtime/generate_candidates.py

# 盘前9:25 - 早间决策（获取实时开盘价，确认买入）
25 9 * * 1-5 cd /home/AIWealth && python3 realtime/morning_decision.py

# 盘中每小时 - 持仓监控（止盈止损检查）
0 10,11,13,14,15 * * 1-5 cd /home/AIWealth && python3 realtime/position_tracker.py check
```

### 6. 初始化实盘持仓

编辑 `data/realtime/positions.json`：

```json
{
  "account": {
    "initial_capital": 1000000,
    "cash": 1000000,
    "total_nav": 1000000
  },
  "positions": [],
  "history": []
}
```

## 系统架构

### 交易时序

```
盘后 18:30   daily_update.sh         → 增量更新日K线+小时K线数据（耗时约1~3小时）
盘后 21:30   generate_candidates.py  → 扫描5策略生成候选股列表（依赖日K数据更新完成，20:45财报日历先行刷新）
盘前  9:25   morning_decision.py     → 获取实时开盘价，确认买入信号
盘中 10-15   position_tracker.py     → 监控止盈/止损/到期卖出
```

### 资金模型

- 统一资金池，5个slot共享
- 买入金额 = `total_nav / 5`（动态等权再平衡）
- 一个策略赚的钱回到资金池，所有策略下次买入均受益
- 最多同时持仓5只（每策略最多1只）

### 回测引擎

- **小时级推进**：每个交易日按 hour 1→2→3→4 推进
- **合规保证**：策略只能看到 hour-1 及更早数据
- **成交价**：当前 hour 的 open（该小时开始即确定）
- **T+1约束**：买入当日不可卖出

## 配置说明

| 配置项 | 文件 | 说明 |
|--------|------|------|
| 活跃策略 | `realtime/config.py` | 修改 `ACTIVE_STRATEGIES` 列表增减策略 |
| 初始资金 | `data/realtime/positions.json` | `account.initial_capital` |
| 数据库路径 | `backtest/data_feed.py` | `DB_PATH` 常量 |
| 日志目录 | `backtest/txt_formatter.py` | `LOG_DIR` 常量 |

### 新增策略

1. 在 `strategies/` 目录创建策略文件，继承 `strategies.base.Strategy`
2. 实现 `should_buy()` 和 `should_sell()` 标准接口
3. 在 `realtime/config.py` 的 `ACTIVE_STRATEGIES` 中添加配置项
4. 无需修改框架代码

## 技术栈

| 组件 | 技术 |
|------|------|
| 语言 | Python 3.8+ |
| Web框架 | Flask |
| 数据库 | SQLite (stocks.db) |
| 数据源 | BaoStock (日K+60min K线) |
| 前端图表 | ECharts |
| 实时行情 | 新浪财经HTTP接口 (urllib) |

## 注意事项

1. **端口权限**：默认监听80端口，Linux下需root权限或使用 `setcap` 授权
2. **数据量**：全量初始化约5000只股票×5年数据，首次运行需1-2小时
3. **交易日判断**：系统基于数据库中已有交易日判断，非交易日不触发信号
4. **IPO过滤**：自动过滤上市前5日股票（无涨跌幅限制期间）
5. **数据库备份**：`data/stocks.db` 为核心数据文件，建议定期备份
