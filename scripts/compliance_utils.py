#!/usr/bin/env python3
"""A股回测合规验证公共模块
所有回测脚本共用的合规检查函数。
涨停判定规则: round(close/preclose, 2) >= threshold
"""
import sqlite3
import math

DB_PATH = '/home/AIWealth/data/stocks.db'

def get_limit_threshold(code):
    """根据股票代码获取涨跌停阈值
    创业板(sz.300*)/科创板(sh.688*) → 20%涨跌停
    主板/北交所/其他 → 10%涨跌停
    返回: (up_ratio, down_ratio) 如 (1.20, 0.80) 或 (1.10, 0.90)
    """
    if code.startswith('sz.30') or code.startswith('sh.688'):
        return (1.20, 0.80)
    return (1.10, 0.90)

def is_limit_up(code, close, preclose):
    """判断是否涨停封板
    规则: round(close/preclose, 2) >= up_threshold
    """
    if not preclose or preclose <= 0:
        return False
    up_th, _ = get_limit_threshold(code)
    return round(close / preclose, 2) >= up_th

def is_limit_down(code, close, preclose):
    """判断是否跌停
    规则: round(close/preclose, 2) <= down_threshold
    """
    if not preclose or preclose <= 0:
        return False
    _, down_th = get_limit_threshold(code)
    return round(close / preclose, 2) <= down_th

def is_one_word_board(h_open, h_high, h_low, h_close):
    """判断是否一字板（四价相等或极其接近，无法成交）
    规则: max-min < 0.01 (容差)
    """
    if not all([h_open, h_high, h_low, h_close]):
        return True  # 数据缺失视为不可交易
    prices = [h_open, h_high, h_low, h_close]
    return (max(prices) - min(prices)) < 0.01

def check_t1(buy_date, sell_date):
    """检查T+1合规: 卖出日必须严格晚于买入日"""
    return sell_date > buy_date

def is_valid_price(price, hour_low, hour_high):
    """检查成交价是否在当hour振幅范围内
    规则: hour_low <= price <= hour_high (含0.01容差)
    """
    if not all([price, hour_low, hour_high]):
        return False
    return (hour_low - 0.01) <= price <= (hour_high + 0.01)

def is_st(code_name, is_st_flag):
    """检查是否ST股
    规则: isST字段=1 或 code_name包含"ST"(不区分大小写)
    """
    if is_st_flag == 1:
        return True
    if code_name and 'ST' in code_name.upper():
        return True
    return False

def safe_float(val, default=0.0):
    """安全浮点转换，防御None和NaN"""
    if val is None:
        return default
    try:
        f = float(val)
        if math.isnan(f) or math.isinf(f):
            return default
        return f
    except (ValueError, TypeError):
        return default

def verify_buy_compliance(code, code_name, buy_price, day_row, hour_prefix='hour1'):
    """验证单笔买入合规性
    day_row: dict包含该日K线数据
    hour_prefix: 'hour1'/'hour2'等
    检查: 涨停不买、一字板不买、ST不买、成交价合理
    返回: (passed: bool, reasons: list[str])
    """
    reasons = []
    
    # ST检查
    if is_st(code_name, day_row.get('isST', 0)):
        reasons.append('ST股不可买入')
    
    # 涨停检查(用前收盘判定当日是否涨停开盘)
    preclose = safe_float(day_row.get('preclose'))
    close = safe_float(day_row.get('close'))
    if preclose > 0 and is_limit_up(code, close, preclose):
        reasons.append(f'涨停股不可买入(close/preclose={close/preclose:.4f})')
    
    # 一字板检查
    h_open = safe_float(day_row.get(f'{hour_prefix}_open'))
    h_high = safe_float(day_row.get(f'{hour_prefix}_high'))
    h_low = safe_float(day_row.get(f'{hour_prefix}_low'))
    h_close = safe_float(day_row.get(f'{hour_prefix}_close'))
    if is_one_word_board(h_open, h_high, h_low, h_close):
        reasons.append(f'一字板不可买入({hour_prefix} O=H=L=C)')
    
    # 成交价合理性
    if h_low > 0 and h_high > 0 and not is_valid_price(buy_price, h_low, h_high):
        reasons.append(f'买入价{buy_price:.2f}不在{hour_prefix}振幅[{h_low:.2f},{h_high:.2f}]内')
    
    return (len(reasons) == 0, reasons)

def verify_sell_compliance(code, sell_price, day_row, hour_prefix='hour4'):
    """验证单笔卖出合规性
    检查: 跌停不卖、成交价合理
    返回: (passed: bool, reasons: list[str])
    """
    reasons = []
    
    # 跌停检查
    preclose = safe_float(day_row.get('preclose'))
    close = safe_float(day_row.get('close'))
    if preclose > 0 and is_limit_down(code, close, preclose):
        reasons.append(f'跌停股不可卖出(close/preclose={close/preclose:.4f})')
    
    # 成交价合理性
    h_low = safe_float(day_row.get(f'{hour_prefix}_low'))
    h_high = safe_float(day_row.get(f'{hour_prefix}_high'))
    if h_low > 0 and h_high > 0 and not is_valid_price(sell_price, h_low, h_high):
        reasons.append(f'卖出价{sell_price:.2f}不在{hour_prefix}振幅[{h_low:.2f},{h_high:.2f}]内')
    
    return (len(reasons) == 0, reasons)

def verify_trade(trade_dict, conn=None):
    """综合验证单笔完整交易
    trade_dict: {code, code_name, buy_date, buy_price, buy_hour,
                 sell_date, sell_price, sell_hour, pnl_pct}
    返回: {passed: bool, checks: dict, reasons: list}
    """
    checks = {}
    reasons = []
    
    # T+1检查
    t1_ok = check_t1(trade_dict['buy_date'], trade_dict['sell_date'])
    checks['T+1'] = t1_ok
    if not t1_ok:
        reasons.append(f"T+0违规: 买入{trade_dict['buy_date']}卖出{trade_dict['sell_date']}")
    
    # 如果提供了数据库连接，从库中重读验证
    if conn:
        cur = conn.cursor()
        # 验证买入日
        cur.execute('SELECT * FROM stock_kline WHERE date=? AND code=?',
                   (trade_dict['buy_date'], trade_dict['code']))
        row = cur.fetchone()
        if row:
            col_names = [desc[0] for desc in cur.description]
            buy_day = dict(zip(col_names, row))
            buy_hour_prefix = f"hour{trade_dict.get('buy_hour', 1)}"
            buy_ok, buy_reasons = verify_buy_compliance(
                trade_dict['code'], trade_dict['code_name'],
                trade_dict['buy_price'], buy_day, buy_hour_prefix)
            checks['买入合规'] = buy_ok
            reasons.extend(buy_reasons)
        
        # 验证卖出日
        cur.execute('SELECT * FROM stock_kline WHERE date=? AND code=?',
                   (trade_dict['sell_date'], trade_dict['code']))
        row = cur.fetchone()
        if row:
            col_names = [desc[0] for desc in cur.description]
            sell_day = dict(zip(col_names, row))
            sell_hour_prefix = f"hour{trade_dict.get('sell_hour', 4)}"
            sell_ok, sell_reasons = verify_sell_compliance(
                trade_dict['code'], trade_dict['sell_price'],
                sell_day, sell_hour_prefix)
            checks['卖出合规'] = sell_ok
            reasons.extend(sell_reasons)
    
    # 收益率复算
    if trade_dict.get('buy_price') and trade_dict['buy_price'] > 0:
        calc_pnl = (trade_dict['sell_price'] - trade_dict['buy_price']) / trade_dict['buy_price'] * 100
        reported_pnl = trade_dict.get('pnl_pct', calc_pnl)
        pnl_match = abs(calc_pnl - reported_pnl) < 0.1
        checks['收益率一致'] = pnl_match
        if not pnl_match:
            reasons.append(f"收益率不一致: 报告{reported_pnl:.2f}% vs 复算{calc_pnl:.2f}%")
    
    return {
        'passed': len(reasons) == 0,
        'checks': checks,
        'reasons': reasons
    }

def generate_compliance_report(trades, conn=None):
    """生成完整合规报告
    trades: list of trade_dict
    返回并打印报告
    """
    report = {
        'total': len(trades),
        'passed': 0,
        'failed': 0,
        'violations': {
            'T+0违规': 0,
            '涨停买入': 0,
            '跌停卖出': 0,
            '一字板': 0,
            'ST买入': 0,
            '价格越界': 0,
            '收益率不一致': 0,
        }
    }
    
    failed_trades = []
    for t in trades:
        result = verify_trade(t, conn)
        if result['passed']:
            report['passed'] += 1
        else:
            report['failed'] += 1
            failed_trades.append((t, result['reasons']))
            for reason in result['reasons']:
                if 'T+0' in reason:
                    report['violations']['T+0违规'] += 1
                elif '涨停' in reason:
                    report['violations']['涨停买入'] += 1
                elif '跌停' in reason:
                    report['violations']['跌停卖出'] += 1
                elif '一字板' in reason:
                    report['violations']['一字板'] += 1
                elif 'ST' in reason:
                    report['violations']['ST买入'] += 1
                elif '振幅' in reason or '价格' in reason:
                    report['violations']['价格越界'] += 1
                elif '收益率' in reason:
                    report['violations']['收益率不一致'] += 1
    
    # 打印报告
    print("\n" + "="*50)
    print("       合规性验证报告")
    print("="*50)
    print(f"  总交易数: {report['total']}")
    print(f"  通过: {report['passed']} ({report['passed']/max(1,report['total'])*100:.1f}%)")
    print(f"  失败: {report['failed']}")
    print("-"*50)
    for k, v in report['violations'].items():
        status = '✓' if v == 0 else f'✗ ({v}笔)'
        print(f"  {k}: {status}")
    print("="*50)
    
    if failed_trades:
        print(f"\n前5笔违规交易明细:")
        for t, reasons in failed_trades[:5]:
            print(f"  {t['buy_date']} {t['code']} {t.get('code_name','')} | {', '.join(reasons)}")
    
    return report


if __name__ == '__main__':
    print("合规验证模块加载成功")
    print(f"涨停判定示例: sz.300001 close=12.0 preclose=10.0 -> is_limit_up={is_limit_up('sz.300001', 12.0, 10.0)}")
    print(f"涨停判定示例: sh.600001 close=11.0 preclose=10.0 -> is_limit_up={is_limit_up('sh.600001', 11.0, 10.0)}")
    print(f"跌停判定示例: sz.300001 close=8.0 preclose=10.0 -> is_limit_down={is_limit_down('sz.300001', 8.0, 10.0)}")
    print(f"一字板示例: O=H=L=C=10.0 -> is_one_word_board={is_one_word_board(10.0, 10.0, 10.0, 10.0)}")
    print(f"正常K线: O=10.0 H=10.5 L=9.8 C=10.2 -> is_one_word_board={is_one_word_board(10.0, 10.5, 9.8, 10.2)}")
