# 多指标评分量化交易系统 - 交易机制说明书

## 一、系统概述

### 1.1 系统架构

本系统是一个多指标评分驱动的短线量化交易系统，核心机制为：
- **多指标评分评估** → **信号筛选排序** → **资金分配** → **买入执行** → **风险管理** → **卖出退出**

### 1.2 核心参数

| 参数 | 默认值 | 说明 |
|------|-------|------|
| 初始资金 | 100万元 | `portfolio.initial_capital` |
| 仓位数 | 5 | `portfolio.n_slots`，最多同时持仓5只股票 |
| 评分模式 | star | 星级二值评分模式 |
| 最低星数 | 3 | 买入触发阈值，即3星及以上 |
| 最大持仓天数 | 5 | `sell_params.time_limit.max_hold_days` |
| 买入滑点 | 0.2% | 现实成本，买价×(1+0.002) |
| 卖出滑点 | 0.2% | 现实成本，卖价×(1-0.002) |

### 1.3 交易约束

系统严格遵守A股交易规则，实现了完全合规的回测框架：
- **T+1约束**：当天买入的股票（hold_days==0）次日才能卖出
- **涨停不买**：买入价不能≥涨停价（一字涨停板更是无法成交）
- **跌停不卖**：卖出价不能≤跌停价（一字跌停板无法卖出）
- **涨跌停精确计算**：按照A股交易规则的板块差异精确计算


## 二、指标体系（评分机制）

### 2.1 指标分层结构

系统共包含 **7个指标**，分为两层：

#### TRIGGER层（核心触发，3个指标）
这三个指标是交易信号的核心来源，任意一个触发就可以考虑买入。

1. **big_drop_gap_up**（大阴后高开）
   - 机制：昨日大幅下跌（close_rate ≤ -5%），今日高开（open_rate 2~20%）
   - 预测力：2.37x lift，5天均收益 +3.15%
   - 计算：`yesterday.close_rate ≤ -5.0 AND 2.0 ≤ today.open_rate ≤ 20.0` → 1.0星

2. **limit_down_reversal**（跌停反包）
   - 机制：昨日跌停，今日竞价高开并收阳
   - 预测力：2.34x lift，5天均收益 +4.53%
   - 计算：
     ```
     yesterday.close ≤ 0.805×yesterday.preclose (主板0.905)
     AND today.close > today.open
     → 1.0星
     ```

3. **limit_up_next**（涨停次日）
   - 机制：昨日涨停，今日开盘破板
   - 预测力：2.10x lift（高波动，需极短持）
   - 计算：
     ```
     昨收 ≥ 1.20×昨preclose (主板1.10)
     AND 今日非一字涨停
     → 1.0星
     ```

#### CONFIRMATION层（确认加分，4个指标）
这些指标用于增强买入信号的置信度，叠加在触发信号之上。

4. **cum_drop_first_positive**（累计下跌后首阳）
   - 机制：前N日累计跌幅≤-10%，今日首次收阳
   - 预测力：1.72x lift，5天均收益 +1.47%
   - 计算：
     ```
     CUMSUM(history[-(lookback):].close_rate where <0) ≤ -10.0
     AND today.close_rate > 0
     → 1.0星
     ```

5. **volume_breakout**（放量突破）
   - 机制：今日成交量≥前5日均量的2.5倍以上
   - 预测力：1.61x lift
   - 计算：`today.volume ≥ 2.5 × AVG(history[-5:].volume)` → 1.0星

6. **amplitude_positive**（振幅扩大收阳）
   - 机制：日振幅≥5%且收盘价>开盘价
   - 预测力：1.52x lift
   - 计算：
     ```
     (high - low) / preclose × 100% ≥ 5.0%
     AND close > open
     → 1.0星
     ```

7. **rsi_oversold_bounce**（RSI超卖反弹）
   - 机制：RSI(14)从<30(超卖)反弹至≥30
   - 预测力：1.25x lift，5天均收益 +1.58%
   - 计算：`RSI_yesterday < 30 AND RSI_today ≥ 30` → 1.0星

### 2.2 评分规则

**星级模式（Star Mode）**：
- 每个指标是二值的：满足条件=1星，不满足=0星
- **总星数** = TRIGGER指标中满足的数量 + CONFIRMATION指标中满足的数量
- 最高7星（所有指标都满足），最低0星

**买入触发条件（双重门禁）**：
```
signal_trigger = (至少1个TRIGGER指标亮) AND (total_star_count >= min_stars)
```

其中：
- TRIGGER指标亮：{big_drop_gap_up, limit_down_reversal, limit_up_next} 中至少有1个 = 1.0
- min_stars = 3（配置可调，通常 1 trigger + 2 confirmation = 3星）


## 三、买入机制

### 3.1 候选股筛选流程

**Step 1：基础过滤**
```python
# 排除条件
if isST == 1 or 'ST' in code_name:
    skip()
if turn < 1.0%:  # 最低换手率
    skip()
if market_cap < 50亿:  # 最低市值
    skip()
```

**Step 2：涨停检查**
- 一字涨停判定：`open == high == low == close` 且价格 ≥ 涨停价 → 无法买入
- 涨停价计算：`limit_up = round(preclose × (1 + ratio), 2)`
  - 主板(sz.0/sh.6开头)：ratio = 10% = 0.1
  - 创业板(sz.300/301开头)：ratio = 20% = 0.2
  - 科创板(sh.688开头)：ratio = 20% = 0.2
  - 北交所(bj.开头或43/83/87开头)：ratio = 30% = 0.3

**Step 3：多指标评分**
- 对每只候选股计算7个指标
- 统计star_count（总亮星数）
- 评分结果示例：`{total_score: 3.2, star_count: 3, details: {...}}`

**Step 4：阈值过滤**
- 筛选条件：`star_count >= 3` 且 `至少1个trigger亮`
- 通过过滤的股票进入排序队列

### 3.2 信号排序

**排序规则**（确定性排序，无随机性）：
```python
# 按降序排列，优先级递减
sort_key = (star_count, total_score)
all_signals.sort(key=sort_key, reverse=True)
```

**示例**：
- 信号A：star_count=5, score=3.8
- 信号B：star_count=5, score=3.2
- 信号C：star_count=4, score=4.5
排序结果：A > B > C

### 3.3 资金分配（持仓管理）

**分配策略**：等权分配（Equal Weight）

```python
slot_size = total_equity / n_slots
# 例：总净值100万，5仓 → 每仓20万
```

**分配逻辑**：
```python
available_slots = n_slots - len(current_positions)  # 可用仓位数
for sig in sorted_signals[:available_slots]:
    allocation_amount = slot_size
    allocations.append((sig, allocation_amount))
```

### 3.4 买入执行

**执行流程**（按排序顺序逐笔）：

```python
for i, (signal, amount_to_buy) in enumerate(allocations):
    if used_slots >= n_slots:
        break  # 仓位满
    
    # 再次合规检查
    buy_price = signal['buy_price']  # 通常为hour1_open
    preclose = signal['preclose']
    code = signal['code']
    
    if buy_price >= limit_up_price(code, preclose):
        continue  # 合规失败
    
    # 应用买入滑点
    actual_buy_price = buy_price × (1 + 0.002)
    
    # 应用单笔资金上限（如果配置）
    if max_per_trade > 0:
        slot_amount = min(amount_to_buy, max_per_trade)
    else:
        slot_amount = amount_to_buy
    
    # 计算股数（100股为最小单位）
    shares = int(slot_amount / actual_buy_price / 100) × 100
    if shares < 100:
        continue  # 资金不足
    
    # 记录持仓
    positions.append({
        'code': code,
        'buy_price': actual_buy_price,
        'shares': shares,
        'buy_date': date,
        'buy_hour': 'hour1',
        'hold_days': 0,
        'max_price': actual_buy_price,
        'strategy_name': signal['strategy_name'],
        'score_at_buy': signal['score'],
    })
    
    cash -= shares × actual_buy_price
    used_slots += 1
```

### 3.5 合规检查细节

**买入价合规性**（主核心引擎中实现）：

```python
def _check_buy_compliance(buy_price, preclose, code):
    """检查买入价是否合规"""
    ratio = _get_limit_ratio(code)
    limit_up_price = round(preclose * (1 + ratio), 2)
    if buy_price >= limit_up_price:
        return False  # 不能在涨停价或以上买入
    return True
```

**涨停价精确计算**：
```python
# ✓ 正确的做法（核心引擎采用）
limit_up = round(preclose * (1 + ratio), 2)
is_limit_up = buy_price >= limit_up

# ✗ 错误的做法（容差判断不合规）
is_limit_up = buy_price >= limit_up * 0.998  # 不允许
```


## 四、卖出机制

### 4.1 卖出策略组合

系统支持多个独立的卖出策略组合，任意一个策略触发则执行卖出。

#### 策略1：固定止盈止损（FixedTPSL）
**配置参数**：
- tp_pct = 10.0（止盈+10%）
- sl_pct = -3.0（止损-3%）

**触发逻辑**：
```python
tp_price = buy_price × (1 + 10% ) = buy_price × 1.10
sl_price = buy_price × (1 - 3%) = buy_price × 0.97

# 每个hour检查
if hour_low ≤ sl_price:
    # 止损优先触发
    if hour_open ≤ sl_price:
        actual_sell_price = hour_open  # 跳空低开已破止损线
    else:
        actual_sell_price = sl_price    # 正常止损成交
    sell(actual_sell_price, '止损')

elif hour_high ≥ tp_price:
    # 再检查止盈
    if hour_open ≥ tp_price:
        actual_sell_price = hour_open  # 跳空高开已达止盈
    else:
        actual_sell_price = tp_price    # 正常止盈成交
    sell(actual_sell_price, '止盈')
```

**特性**：
- 止损优先于止盈（同hour内先检查low，再检查high）
- 支持跳空开盘直接止损/止盈（使用open价）
- 自动追踪最高价（用于其他策略计算）

#### 策略2：移动止损（TrailingStop）
**配置参数**：
- trail_pct = 5.0（从最高价回撤5%止损）

**触发逻辑**：
```python
trail_price = max_price × (1 - 5%) = max_price × 0.95

if hour_low ≤ trail_price:
    if hour_open ≤ trail_price:
        actual_sell_price = hour_open
    else:
        actual_sell_price = trail_price
    sell(actual_sell_price, '移动止损')

# 如果未触发，更新最高价
if hour_high > max_price:
    max_price = hour_high
```

**特性**：
- 动态止损线随着价格上升而上移，锁定利润
- 需要维护持仓的max_price字段

#### 策略3：到期强平（TimeLimitExit）
**配置参数**：
- max_hold_days = 5（最多持仓5个交易日）
- exit_hour = 4（第4小时收盘强制平仓）

**触发逻辑**：
```python
if hour == 4 and hold_days >= 5:
    actual_sell_price = hour4_close
    sell(actual_sell_price, '到期强平')
```

**特性**：
- 作为风控底线，确保超期持仓被强制退出
- 固定在hour4（日收盘）执行
- 防止持仓过度延伸

#### 策略4：缩量跌破（VolumeShrinkExit）
**配置参数**：
- vol_shrink_ratio = 0.5（今日量<昨日50%）
- price_drop_pct = -1.0（价格跌1%以上）
- check_hour = 3（第3小时检查）

**触发逻辑**：
```python
vol_ratio = today_volume / yesterday_volume

if hour == 3 and vol_ratio < 0.5 and close_rate < -1.0%:
    actual_sell_price = hour3_close
    sell(actual_sell_price, '缩量下跌')
```

**特性**：
- 反映市场资金撤离，主力不再维护
- 需要day_data中的volume和prev_volume数据

#### 策略5：评分衰减（ScoreDecayExit）
**配置参数**：
- min_score = 2.0（最低持仓评分）
- check_hour = 1（第1小时检查）

**触发逻辑**：
```python
if hour == 1 and current_score < 2.0:
    actual_sell_price = hour1_open (或hour1_close)
    sell(actual_sell_price, '评分衰减')
```

**特性**：
- 适用于评分驱动型策略的动态管理
- 需要持仓记录中维护current_score字段

### 4.2 日内卖出时序

**卖出处理的执行顺序**：

```python
def _process_sells(date):
    for position in positions:
        if position['hold_days'] == 0:
            continue  # T+1约束：当天买入跳过卖出
        
        # 一字跌停特殊处理
        if is_oneword_limit_down(row):
            if position['hold_days'] >= max_hold_days * 2:
                # 超时安全阀：即使跌停也强制退出
                sell(close_price, '强制退出')
            continue  # 正常跳停则跳过
        
        # 逐hour检查卖出条件（hour1-4）
        sold = False
        for hour in range(1, 5):
            hour_data = row[f'hour{hour}_*']
            if hour_data['open'] <= 0:
                continue  # 数据缺失
            
            # 调用所有卖出策略
            for sell_strategy in sell_strategies:
                should_sell, sell_price, reason = sell_strategy.should_sell(
                    position, hour, hour_data, day_data
                )
                if should_sell and sell_price > 0:
                    # 应用卖出滑点
                    actual_sell_price = sell_price × (1 - 0.002)
                    
                    # 合规检查：不能在跌停价或以下卖出
                    if is_limit_down_cannot_sell(actual_sell_price, preclose, code):
                        continue
                    
                    sell(actual_sell_price, reason)
                    sold = True
                    break  # 任一策略触发则退出hour循环
            
            if sold:
                break  # 已卖出，退出hour循环
        
        # 卖出失败时，日级数据fallback
        if not sold and all_hours_invalid:
            # 使用日级OHLC重新检查卖出
            ...
        
        # 超时安全阀
        if not sold and position['hold_days'] >= max_hold_days * 2:
            sell(day_close, '强制退出')
```

### 4.3 卖出合规检查

**跌停价精确计算**（与买入对应）：

```python
def _is_limit_down_cannot_sell(sell_price, preclose, code):
    """检查卖出价是否违反跌停规则"""
    if sell_price <= 0 or preclose <= 0:
        return True
    ratio = _get_limit_ratio(code)
    limit_down_price = round(preclose * (1 - ratio), 2)
    if sell_price <= limit_down_price:
        return True  # 不能以跌停价或以下卖出
    return False
```

**一字跌停检查**（无法卖出）：

```python
def _is_oneword_limit_down(row):
    """检查是否为一字跌停（四价相等且<=跌停价）"""
    code = row['code']
    preclose = row['preclose']
    ratio = _get_limit_ratio(code)
    limit_down_price = round(preclose * (1 - ratio), 2)
    
    o = row['open']
    h = row['high']
    l = row['low']
    c = row['close']
    
    # 必须四价相等
    if abs(o - h) < 0.001 and abs(o - l) < 0.001 and abs(o - c) < 0.001:
        # 且价格<=跌停价
        if o <= limit_down_price:
            return True  # 一字跌停
    return False
```

**一字涨停检查**（可以正常卖出）：

```python
# 一字涨停：四价相等且>=涨停价
# 由于持仓时涨停已发生，不会再有一字涨停的卖出问题
# 但为了完整性说明：一字涨停持仓是可以卖出的
# 因为卖出价<=一字涨停价，必然满足sell_price > limit_down_price的合规要求
```

### 4.4 卖出滑点处理

```python
# 卖出信号价计算完毕后应用滑点
signal_price = sell_strategy.should_sell(...)[1]
actual_sell_price = signal_price × (1 - 0.002)  # 单边0.2%

# 注意：滑点应用在合规检查之前
# 即：先确定signal_price，再应用滑点，最后检查跌停合规
```


## 五、日内处理时序

### 5.1 完整日期处理流程

```python
def _process_day(date):
    """每个交易日的完整处理流程"""
    
    # 步骤1：先处理卖出（优先级最高）
    _process_sells(date)
    
    # 步骤2：再处理买入
    _process_buys(date)
    
    # 步骤3：最后更新持仓信息
    _update_positions(date)
    
    # 步骤4：更新净值
    portfolio.update_daily(date)
```

### 5.2 更新持仓细节

```python
def _update_positions(date):
    """日终更新所有持仓的动态信息"""
    
    for position in positions:
        # (1) hold_days + 1
        position['hold_days'] += 1
        
        # (2) 更新current_price（用于计算持仓盈亏）
        code = position['code']
        if code in today_price_map:
            position['current_price'] = today_price_map[code]
            
            # (3) 追踪max_price（用于移动止损等）
            if today_price_map[code] > position['max_price']:
                position['max_price'] = today_price_map[code]
```

### 5.3 T+1约束实现

```python
def _process_sells(date):
    for position in positions:
        # T+1约束：hold_days==0表示今天买入，明天才能卖
        if position['hold_days'] == 0:
            continue  # 跳过卖出检查
        
        # 其余持仓正常检查卖出条件
        ...
```

**约束的含义**：
- hold_days在买入当天 = 0
- 下一个交易日 = 1，此时可以卖出
- 保证了中国股市的T+1结算制度


## 六、确定性保证

系统设计完全确定性，无任何随机因素：

### 6.1 排序的确定性

```python
# 排序键为元组 (star_count, score)，降序排列
sort_key = (signal['star_count'], signal['score'])

# 特点：
# 1. 同star_count的信号按score降序
# 2. 同star_count同score的信号顺序由输入列表决定（通常按code字母序）
# 3. Python的sort是稳定排序，保证相等元素顺序不变
```

### 6.2 指标计算的确定性

所有指标都是纯数学运算，无任何随机性：
- 不依赖随机数生成
- 不依赖系统时间的微秒级别
- 不依赖浮点运算的舍入方向（已通过round()确定舍入）

### 6.3 买入分配的确定性

```python
# 按排序结果逐笔分配
for i, signal in enumerate(sorted_signals):
    if used_slots >= n_slots:
        break
    allocations.append((signal, slot_size))
    used_slots += 1
```

固定顺序、固定金额，不因任何外部状态改变。


## 七、合规规则汇总

### 7.1 合规规则一览表

| 规则 | 触发条件 | 实现方式 | 代码位置 |
|------|---------|---------|---------|
| **T+1约束** | hold_days == 0 | _process_sells中skip检查 | L512-513 |
| **涨停不买** | buy_price ≥ limit_up_price | _check_buy_compliance() | L115-123 |
| **一字涨停不买** | 四价相等且≥涨停价 | _is_limit_up_cannot_buy() | L96-112 |
| **跌停不卖** | sell_price ≤ limit_down_price | _is_limit_down_cannot_sell() | L126-134 |
| **一字跌停不卖** | 四价相等且≤跌停价 | _is_oneword_limit_down() | L137-153 |
| **超期强制退出** | hold_days ≥ 2×max_hold_days | 在_process_sells中触发 | L524-534,604-614 |
| **北交所±30%** | code以43/83/87开头 | _get_limit_ratio()返回0.3 | L79-85 |

### 7.2 涨跌停价计算规则

```python
# 通用公式
limit_up_price = round(preclose × (1 + ratio), 2)
limit_down_price = round(preclose × (1 - ratio), 2)

# 比例表（按上市地点）
# 主板（sh.6*或sz.0*）：ratio = 0.10 (±10%)
# 创业板（sz.30*或sz.301*）：ratio = 0.20 (±20%)
# 科创板（sh.688*）：ratio = 0.20 (±20%)
# 北交所（bj.*或43/83/87开头）：ratio = 0.30 (±30%)

def _get_limit_ratio(code):
    if _is_bse(code):
        return 0.3
    if _is_gem(code) or _is_star(code):
        return 0.2
    return 0.1
```

**关键规则**：
- 精确到小数点后两位（使用round(..., 2)）
- 不允许使用容差系数（如0.998倍）判断
- 主要区别：创业板和科创板为±20%，北交所为±30%

### 7.3 一字板的定义与判定

**一字板的严格定义**：
```
four_prices_equal = (abs(open - high) < 0.001 AND 
                     abs(open - low) < 0.001 AND 
                     abs(open - close) < 0.001)
```

**一字涨停**（无法买入）：
```
four_prices_equal AND price >= limit_up_price
```

**一字跌停**（无法卖出）：
```
four_prices_equal AND price <= limit_down_price
```

**一字平开**（特殊情况）：
```
# 如果四价相等但价格在±10%以内
# 可能是一字平开，可以正常交易
```


## 八、数据驱动优化

### 8.1 指标的统计来源

所有7个指标都基于2023-2025年352万样本的统计验证：

| 指标 | Lift倍数 | 5日均收益 | 最小样本 |
|------|---------|---------|---------|
| big_drop_gap_up | 2.37x | +3.15% | - |
| limit_down_reversal | 2.34x | +4.53% | - |
| limit_up_next | 2.10x | - | - |
| cum_drop_first_positive | 1.72x | +1.47% | - |
| volume_breakout | 1.61x | - | - |
| amplitude_positive | 1.52x | - | - |
| rsi_oversold_bounce | 1.25x | +1.58% | - |

**筛选原则**：仅保留提升≥1.5x的有效预测信号

### 8.2 回测框架的年度处理

```python
# 为了提高效率和准确性的处理方式
for year in sorted(years):
    # 加载该年度及其前HISTORY_LOOKBACK(20)天的数据
    hist_start_idx = max(0, year_start_idx - 20)
    hist_start_date = dates[hist_start_idx]
    
    # 加载数据到内存
    data_by_code = load_date_range_data(hist_start_date, year_end)
    
    # 年度内日期循环
    for date in year_dates:
        process_day(date)
    
    # 处理完后释放数据
    del data_by_code
```

**设计意图**：
- 分年度加载数据，降低内存峰值
- 每年保留前20天的历史用于指标计算
- 每年完成后释放内存，准备下一年


## 九、关键配置参数详解

### 9.1 配置文件位置

- **策略配置**：`/home/AIWealth/scripts/strategy_config.py`
- **核心引擎**：`/home/AIWealth/scripts/backtest_scoring_system.py`

### 9.2 配置模板

```python
CONFIG = {
    # ===== 资金与仓位 =====
    'portfolio': {
        'initial_capital': 1_000_000,  # 初始资金
        'n_slots': 5,                  # 最大仓位数
    },
    
    # ===== 7个指标配置 =====
    'indicators': {
        'big_drop_gap_up': {
            'weight': 1.0,
            'star_condition': '>0.5',
            'drop_threshold': -5.0,
            'gap_min': 2.0,
            'gap_max': 20.0,
        },
        # ... 其余6个指标 ...
    },
    'score_threshold': 3,  # 星级模式最低阈值
    
    # ===== 买入策略 =====
    'buy_strategies': ['score', 'vshape'],
    
    # ===== 卖出策略 =====
    'sell_strategies': ['fixed_tpsl', 'time_limit'],
    'sell_params': {
        'fixed_tpsl': {'tp_pct': 10.0, 'sl_pct': -3.0},
        'time_limit': {'max_hold_days': 5, 'exit_hour': 4},
    },
    
    # ===== 持仓策略 =====
    'position_strategy': 'equal_weight',
    
    # ===== 选股过滤 =====
    'filters': {
        'exclude_st': True,
        'min_market_cap': 5_000_000_000,
        'min_turn': 1.0,
    },
}
```

### 9.3 命令行覆盖参数

```bash
# 基础运行
python backtest_scoring_system.py

# 自定义参数
python backtest_scoring_system.py \
    --slots 3 \                        # 仓位数
    --start 2023-01-01 \               # 开始日期
    --end 2026-06-30 \                 # 结束日期
    --mode star \                      # 评分模式
    --min-stars 3 \                    # 最低星数
    --slippage 0.002 \                 # 单边滑点
    --max-per-trade 100000             # 单笔资金上限
```


## 十、附录：代码文件映射

| 功能模块 | 文件路径 | 行数 |
|---------|---------|------|
| 核心回测引擎 | `/home/AIWealth/scripts/backtest_scoring_system.py` | 868 |
| 策略配置 | `/home/AIWealth/scripts/strategy_config.py` | 89 |
| 评分引擎 | `/home/AIWealth/engine/scoring_engine.py` | 166 |
| 指标计算 | `/home/AIWealth/engine/indicators.py` | 346 |
| 买入策略 | `/home/AIWealth/engine/buy_strategies.py` | 451 |
| 卖出策略 | `/home/AIWealth/engine/sell_strategies.py` | 500 |
| 持仓管理 | `/home/AIWealth/engine/position_strategies.py` | 144 |

### 文件间的调用关系

```
backtest_scoring_system.py（主驱动）
├── strategy_config.py（配置）
├── indicators.py（指标库）
├── scoring_engine.py（评分）
├── buy_strategies.py（买入）
├── sell_strategies.py（卖出）
└── position_strategies.py（仓位）
```

