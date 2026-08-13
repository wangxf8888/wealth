"""A股交易规则统一模块 - 回测引擎与实盘引擎共用的唯一规则源。

设计原则（框架兜底，策略无关）:
- 所有通用合规约束(涨跌停/T+1/成交价区间)由引擎层强制执行,
  策略层的自查只是双保险, 框架绝不依赖策略自觉。
- 回测(backtest/)与实盘(realtime/)都从本模块取规则,
  避免两处各写一份导致漂移。

涨跌幅规则:
- 北交所(bj.*)          : ±30%  (43/83/87/88/920号段, 前缀按交易所天然覆盖)
- 创业板(sz.30*)        : ±20%  (300/301/302号段均在sz.30前缀内)
- 科创板(sh.688/sh.689) : ±20%  (688普通股, 689为CDR如九号公司689009, 同为20cm)
- 主板ST                : ±5%
- 主板其他              : ±10%
- 老三板(400/420等)不在库内不可交易, 本模块不适用
"""

from decimal import Decimal, ROUND_HALF_UP


def limit_ratio(code: str, is_st: bool = False) -> float:
    """按板块返回涨跌幅限制比例。"""
    if code.startswith('bj.'):
        return 0.30
    # sh.689=科创板CDR也是20cm (Task#87修复: 旧版只认688导致689009被按10cm误算)
    if code.startswith(('sz.30', 'sh.688', 'sh.689')):
        return 0.20
    if is_st:
        return 0.05
    return 0.10


def limit_prices(code: str, preclose: float, is_st: bool = False) -> tuple:
    """返回 (涨停价, 跌停价)。preclose无效时返回 (0.0, 0.0) 表示无法判定。

    舍入口径=交易所四舍五入(ROUND_HALF_UP)。Task#286根因修复: 旧版float
    round()是银行家舍入+浮点误差(13.95*0.9→12.554999...→12.55), 而交易所
    真实跌停价12.56, 差1分钱导致is_at_limit_down漏判、跌停封死闸门被穿透
    (2026-08-11宁夏建材sh.600449假卖出事故)。Decimal(str())取十进制精确值
    再半上取整, 与交易所口径逐分对齐(t161研究已验证该口径)。
    """
    if not preclose or preclose <= 0:
        return 0.0, 0.0
    ratio = Decimal(str(limit_ratio(code, is_st)))
    pc = Decimal(str(preclose))
    cent = Decimal('0.01')
    return (float((pc * (1 + ratio)).quantize(cent, rounding=ROUND_HALF_UP)),
            float((pc * (1 - ratio)).quantize(cent, rounding=ROUND_HALF_UP)))


def is_at_limit_up(code: str, price: float, preclose: float,
                   is_st: bool = False) -> bool:
    """价格是否已达涨停价（含1分钱容差）。无法判定返回False。"""
    limit_up, _ = limit_prices(code, preclose, is_st)
    if limit_up <= 0 or not price or price <= 0:
        return False
    return price >= limit_up - 0.001


def is_at_limit_down(code: str, price: float, preclose: float,
                     is_st: bool = False) -> bool:
    """价格是否已达跌停价（含1分钱容差）。无法判定返回False。"""
    _, limit_down = limit_prices(code, preclose, is_st)
    if limit_down <= 0 or not price or price <= 0:
        return False
    return price <= limit_down + 0.001


def is_st_name(name: str) -> bool:
    """按证券名称判断是否ST（实盘无isST字段时的兜底）。"""
    return 'ST' in str(name or '').upper()


def is_st_stock(code: str, name: str = '', isST=None) -> bool:
    """ST判定双检(Task#54): isST字段 OR 证券名称含ST, 任一命中即ST。

    背景: stocks.db的isST字段存在不可靠样本(Task#53实锤sh.600053
    2025-08-11 isST=0但名称'*ST九鼎'), 单靠字段会漏判。依赖ST判定的
    策略统一调用本函数做双检(code参数保留未来接码表核查, 当前不使用)。
    """
    if isST:
        return True
    return is_st_name(name)


def clamp_price_to_bar(price: float, bar_low: float, bar_high: float) -> float:
    """把成交价钳制到该时段真实成交价区间[low, high]内。

    实盘中任何订单只可能以区间内的价格成交; 策略若给出越界价格
    (bug或不可执行假设), 框架自动修正到最近边界。
    区间数据缺失时原样返回。
    """
    if price is None or price <= 0:
        return price
    if bar_low and bar_low > 0 and price < bar_low:
        return bar_low
    if bar_high and bar_high > 0 and price > bar_high:
        return bar_high
    return price


# 除权除息判定容差: preclose与前日close的相对差异超过此值视为除权除息
# (0.2%排除厘位舍入; 实测2021-2026全市场20948个除权股·日, Task#4)
EX_DIVIDEND_TOL = 0.002


def is_ex_dividend_gap(preclose: float, prev_close: float,
                       tol: float = EX_DIVIDEND_TOL) -> bool:
    """判定是否除权除息缺口: 交易所preclose != 前一交易日close。

    背景(Task#4): 库为不复权价(adjustflag='3'), 除权除息日交易所会把
    preclose调整为除权参考价 → 当日open_rate=open/preclose-1的语义失真,
    小比例分红产生-1%~-3%的假"低开", 会被低吸类策略误判为买入信号。
    任一价格缺失/非正时返回False(无法判定, 调用方不拦截)。
    """
    if not preclose or preclose <= 0 or not prev_close or prev_close <= 0:
        return False
    return abs(preclose - prev_close) / prev_close > tol


if __name__ == '__main__':
    # 单元断言(公共模块改动纪律, Task#4): python3 trading_rules.py 静默通过即OK
    # 实证案例1: sh.600004 2026-07-24除息(10派2.85) preclose=7.68 前日close=7.96
    assert is_ex_dividend_gap(7.68, 7.96) is True
    # 实证案例2: sz.300925 2021-06-15高送转 preclose=30.05 前日close=51.18 (-41.29%)
    assert is_ex_dividend_gap(30.05, 51.18) is True
    # 正常日: preclose==前日close
    assert is_ex_dividend_gap(10.0, 10.0) is False
    # 厘位舍入容差内(0.1% < 0.2%)不误判
    assert is_ex_dividend_gap(10.01, 10.0) is False
    # 边界外(0.3% > 0.2%)判定为除权
    assert is_ex_dividend_gap(9.97, 10.0) is True
    # 无效输入(缺数据/停牌)不拦截
    assert is_ex_dividend_gap(0.0, 10.0) is False
    assert is_ex_dividend_gap(10.0, 0.0) is False
    assert is_ex_dividend_gap(None, 10.0) is False
    # 既有函数回归断言(涨跌幅规则)
    assert limit_ratio('sz.300196') == 0.20
    assert limit_ratio('sh.600004') == 0.10
    assert limit_ratio('sh.600004', is_st=True) == 0.05
    assert limit_prices('sh.600004', 7.68) == (8.45, 6.91)
    # Task#87: 科创板CDR sh.689也是20cm (九号公司689009, 旧版按10cm误算)
    assert limit_ratio('sh.689009') == 0.20
    assert limit_ratio('sh.688618') == 0.20
    assert limit_ratio('sz.301234') == 0.20  # 创业板301号段
    assert limit_ratio('sz.302132') == 0.20  # 创业板302号段(2024新增)
    assert limit_ratio('bj.920008') == 0.30  # 北交920新号段
    assert limit_ratio('sh.689009', is_st=True) == 0.20  # 创科ST仍20cm(先判板块)
    # 实证: 689009 2022-05-18 preclose=37.38(库核实), 20cm涨停44.86;
    # 旧版按10cm误算41.12, 当日close=43.79被误判为涨停(真实high=44.18未触板)
    assert limit_prices('sh.689009', 37.38) == (44.86, 29.90)
    print("trading_rules 单元断言全部通过")
