#!/usr/bin/env python3
"""
fetch_daily_kline.py - 从BaoStock获取日K+60minK线数据写入stock_kline表
优化版: 每只股票批量获取全日期范围数据(大幅减少API调用)

用法:
  单日模式: python3 fetch_daily_kline.py 2026-06-30
  回补模式: python3 fetch_daily_kline.py 2026-05-23 2026-06-30
"""
import sys
import os
import json
import time
import signal
import sqlite3
import math
import logging
from datetime import datetime, timedelta

import baostock as bs

# ============ 配置区 ============
DB_PATH = '/home/AIWealth/data/stocks.db'
PROGRESS_FILE = '/home/AIWealth/data/fetch_progress.json'
LOG_FILE = '/home/AIWealth/logs/fetch_daily_kline.log'
COMMIT_BATCH = 100          # 每100只股票commit一次
QUERY_TIMEOUT = 60          # 单次查询超时秒数
# 股票代码前缀过滤(只取A股主板/创业板/科创板)
STOCK_PREFIXES = ('sh.60', 'sh.68', 'sz.00', 'sz.30')
# 60分钟K线时间映射: BaoStock返回的结束时间 -> hour序号
TIME_MAP = {
    '103000': 'hour1',  # 9:30-10:30
    '113000': 'hour2',  # 10:30-11:30
    '140000': 'hour3',  # 13:00-14:00
    '150000': 'hour4',  # 14:00-15:00
}
# ================================

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(message)s',
    handlers=[
        logging.FileHandler(LOG_FILE, encoding='utf-8'),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger(__name__)


class QueryTimeoutError(Exception):
    pass


def timeout_handler(signum, frame):
    raise QueryTimeoutError("Query timeout")


def safe_float(val, default=None):
    if val is None or val == '':
        return default
    try:
        v = float(val)
        if math.isnan(v) or math.isinf(v):
            return default
        return v
    except (ValueError, TypeError):
        return default


def safe_int(val, default=None):
    if val is None or val == '':
        return default
    try:
        return int(float(val))
    except (ValueError, TypeError):
        return default


def calc_rate(price, preclose):
    if price is None or preclose is None or preclose == 0:
        return None
    return round((price - preclose) / preclose * 100, 2)


def get_trading_dates(start_date, end_date):
    rs = bs.query_trade_dates(start_date=start_date, end_date=end_date)
    dates = []
    while rs.error_code == '0' and rs.next():
        row = rs.get_row_data()
        if row[1] == '1':
            dates.append(row[0])
    return dates


def get_all_stocks(trading_dates):
    """获取所有交易日的股票并集, 返回 {code: code_name}

    BaoStock的query_all_stock在当天17:00可能返回空(数据未就绪),
    fallback机制: 如果目标日期返回空，自动尝试前1-5个交易日
    """
    stock_map = {}
    # 取首尾+中间日获取完整股票列表
    sample_dates = [trading_dates[0], trading_dates[-1]]
    if len(trading_dates) > 10:
        sample_dates.append(trading_dates[len(trading_dates)//2])

    for date in sample_dates:
        rs = bs.query_all_stock(day=date)
        while rs.error_code == '0' and rs.next():
            row = rs.get_row_data()
            code, trade_status, code_name = row[0], row[1], row[2]
            if trade_status == '1' and code.startswith(STOCK_PREFIXES):
                stock_map[code] = code_name

    # Fallback: 如果返回0只股票(BaoStock数据延迟), 尝试前几个交易日
    if not stock_map:
        logger.warning("当前日期股票列表为空, 尝试使用前几个交易日的列表...")
        from datetime import datetime, timedelta
        base_date = datetime.strptime(trading_dates[0], '%Y-%m-%d')
        for offset in range(1, 6):
            fallback_date = (base_date - timedelta(days=offset)).strftime('%Y-%m-%d')
            rs = bs.query_all_stock(day=fallback_date)
            while rs.error_code == '0' and rs.next():
                row = rs.get_row_data()
                code, trade_status, code_name = row[0], row[1], row[2]
                if trade_status == '1' and code.startswith(STOCK_PREFIXES):
                    stock_map[code] = code_name
            if stock_map:
                logger.info(f"使用 {fallback_date} 的股票列表({len(stock_map)}只)作为fallback")
                break
    return stock_map


def get_daily_kline_range(code, start_date, end_date):
    """批量获取单只股票多日日K线, 返回 {date: row_data}"""
    fields = 'date,code,open,high,low,close,preclose,volume,amount,turn,pctChg,isST'
    rs = bs.query_history_k_data_plus(
        code, fields,
        start_date=start_date, end_date=end_date,
        frequency='d', adjustflag='3'
    )
    result = {}
    if rs.error_code != '0':
        return result
    while rs.next():
        row = rs.get_row_data()
        result[row[0]] = row
    return result


def get_60min_kline_range(code, start_date, end_date):
    """批量获取单只股票多日60min K线, 返回 {date: {hour: data}}"""
    fields = 'date,time,code,open,high,low,close,volume,amount'
    rs = bs.query_history_k_data_plus(
        code, fields,
        start_date=start_date, end_date=end_date,
        frequency='60', adjustflag='3'
    )
    result = {}
    if rs.error_code != '0':
        return result
    while rs.next():
        row = rs.get_row_data()
        date = row[0]
        time_str = row[1][8:14] if len(row[1]) >= 14 else ''
        hour_key = TIME_MAP.get(time_str)
        if hour_key:
            if date not in result:
                result[date] = {}
            result[date][hour_key] = {
                'open': safe_float(row[3]),
                'high': safe_float(row[4]),
                'low': safe_float(row[5]),
                'close': safe_float(row[6]),
                'volume': safe_int(row[7]),
                'amount': safe_float(row[8]),
            }
    return result


def build_record(code, code_name, daily_row, hour_data):
    preclose = safe_float(daily_row[6])
    open_p = safe_float(daily_row[2])
    high_p = safe_float(daily_row[3])
    low_p = safe_float(daily_row[4])
    close_p = safe_float(daily_row[5])
    volume = safe_int(daily_row[7])
    amount = safe_float(daily_row[8])
    turn = safe_float(daily_row[9])
    isST_val = safe_int(daily_row[11], 0)

    if isST_val is None or isST_val == 0:
        if 'ST' in code_name.upper():
            isST_val = 1
        else:
            isST_val = 0

    record = {
        'date': daily_row[0],
        'code': code,
        'code_name': code_name,
        'preclose': preclose,
        'open': open_p,
        'open_rate': calc_rate(open_p, preclose),
        'high': high_p,
        'high_rate': calc_rate(high_p, preclose),
        'low': low_p,
        'low_rate': calc_rate(low_p, preclose),
        'close': close_p,
        'close_rate': calc_rate(close_p, preclose),
        'volume': volume,
        'amount': amount,
        'turn': turn,
        'isST': isST_val,
    }

    for h in ['hour1', 'hour2', 'hour3', 'hour4']:
        hd = hour_data.get(h, {}) if hour_data else {}
        h_open = hd.get('open')
        h_high = hd.get('high')
        h_low = hd.get('low')
        h_close = hd.get('close')
        record[f'{h}_open'] = h_open
        record[f'{h}_open_rate'] = calc_rate(h_open, preclose)
        record[f'{h}_high'] = h_high
        record[f'{h}_high_rate'] = calc_rate(h_high, preclose)
        record[f'{h}_low'] = h_low
        record[f'{h}_low_rate'] = calc_rate(h_low, preclose)
        record[f'{h}_close'] = h_close
        record[f'{h}_close_rate'] = calc_rate(h_close, preclose)
        record[f'{h}_volume'] = hd.get('volume')
        record[f'{h}_amount'] = hd.get('amount')

    return record


INSERT_SQL = """INSERT OR IGNORE INTO stock_kline (
    date, code, code_name, preclose,
    open, open_rate, high, high_rate, low, low_rate, close, close_rate,
    volume, amount, turn,
    hour1_open, hour1_open_rate, hour1_high, hour1_high_rate,
    hour1_low, hour1_low_rate, hour1_close, hour1_close_rate,
    hour2_open, hour2_open_rate, hour2_high, hour2_high_rate,
    hour2_low, hour2_low_rate, hour2_close, hour2_close_rate,
    hour3_open, hour3_open_rate, hour3_high, hour3_high_rate,
    hour3_low, hour3_low_rate, hour3_close, hour3_close_rate,
    hour4_open, hour4_open_rate, hour4_high, hour4_high_rate,
    hour4_low, hour4_low_rate, hour4_close, hour4_close_rate,
    hour1_volume, hour1_amount, hour2_volume, hour2_amount,
    hour3_volume, hour3_amount, hour4_volume, hour4_amount,
    isST
) VALUES (
    :date, :code, :code_name, :preclose,
    :open, :open_rate, :high, :high_rate, :low, :low_rate, :close, :close_rate,
    :volume, :amount, :turn,
    :hour1_open, :hour1_open_rate, :hour1_high, :hour1_high_rate,
    :hour1_low, :hour1_low_rate, :hour1_close, :hour1_close_rate,
    :hour2_open, :hour2_open_rate, :hour2_high, :hour2_high_rate,
    :hour2_low, :hour2_low_rate, :hour2_close, :hour2_close_rate,
    :hour3_open, :hour3_open_rate, :hour3_high, :hour3_high_rate,
    :hour3_low, :hour3_low_rate, :hour3_close, :hour3_close_rate,
    :hour4_open, :hour4_open_rate, :hour4_high, :hour4_high_rate,
    :hour4_low, :hour4_low_rate, :hour4_close, :hour4_close_rate,
    :hour1_volume, :hour1_amount, :hour2_volume, :hour2_amount,
    :hour3_volume, :hour3_amount, :hour4_volume, :hour4_amount,
    :isST
)"""


def update_progress(progress):
    try:
        with open(PROGRESS_FILE, 'w') as f:
            json.dump(progress, f, ensure_ascii=False, indent=2)
    except Exception:
        pass


def get_existing_codes(conn, start_date, end_date):
    """获取已有数据的(date,code)集合用于断点续传"""
    cur = conn.execute(
        "SELECT DISTINCT code FROM stock_kline WHERE date >= ? AND date <= ? GROUP BY code HAVING COUNT(*) >= ?",
        (start_date, end_date, 20)  # 如果一只股票已有>=20天数据(约26天的77%),认为已完成
    )
    return set(row[0] for row in cur.fetchall())


def main():
    if len(sys.argv) < 2:
        print("用法:")
        print("  单日模式: python3 fetch_daily_kline.py 2026-06-30")
        print("  回补模式: python3 fetch_daily_kline.py 2026-05-23 2026-06-30")
        sys.exit(1)

    start_date = sys.argv[1]
    end_date = sys.argv[2] if len(sys.argv) > 2 else start_date

    # 设置signal handler
    signal.signal(signal.SIGALRM, timeout_handler)

    # 登录BaoStock
    logger.info("登录BaoStock...")
    lg = bs.login()
    if lg.error_code != '0':
        logger.error(f"BaoStock登录失败: {lg.error_msg}")
        sys.exit(1)
    logger.info("BaoStock登录成功")

    # 获取交易日列表
    logger.info(f"获取交易日列表: {start_date} ~ {end_date}")
    trading_dates = get_trading_dates(start_date, end_date)
    logger.info(f"共{len(trading_dates)}个交易日")
    if not trading_dates:
        logger.warning("无交易日, 退出")
        bs.logout()
        sys.exit(0)

    # 获取所有股票列表
    logger.info("获取股票列表...")
    stock_map = get_all_stocks(trading_dates)
    total_stocks = len(stock_map)
    logger.info(f"共{total_stocks}只股票")

    # 连接数据库
    conn = sqlite3.connect(DB_PATH)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")

    # 断点续传: 跳过已完整获取的股票
    completed_codes = get_existing_codes(conn, start_date, end_date)
    logger.info(f"已完成股票: {len(completed_codes)}只, 待处理: {total_stocks - len(completed_codes)}只")

    # 初始化进度
    progress = {
        'start_date': start_date,
        'end_date': end_date,
        'total_days': len(trading_dates),
        'total_stocks': total_stocks,
        'completed': len(completed_codes),
        'speed': '',
        'errors': [],
        'started_at': datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
    }
    update_progress(progress)

    total_inserted = 0
    errors_count = 0
    start_time = time.time()
    stocks_list = [(code, name) for code, name in stock_map.items() if code not in completed_codes]

    for i, (code, code_name) in enumerate(stocks_list):
        try:
            signal.alarm(QUERY_TIMEOUT)

            # 批量获取日K (全日期范围)
            daily_data = get_daily_kline_range(code, start_date, end_date)
            if not daily_data:
                signal.alarm(0)
                continue

            # 批量获取60min K (全日期范围)
            hour_data_all = get_60min_kline_range(code, start_date, end_date)

            signal.alarm(0)

            # 对每个有效交易日组装记录并插入
            for date, daily_row in daily_data.items():
                preclose = safe_float(daily_row[6])
                if preclose is None or preclose == 0:
                    continue
                hour_data = hour_data_all.get(date, {})
                record = build_record(code, code_name, daily_row, hour_data)
                conn.execute(INSERT_SQL, record)
                total_inserted += 1

        except QueryTimeoutError:
            signal.alarm(0)
            errors_count += 1
            logger.warning(f"{code} 查询超时, 跳过")
            if len(progress['errors']) < 100:
                progress['errors'].append(f"{code}|timeout")
        except Exception as e:
            signal.alarm(0)
            errors_count += 1
            logger.warning(f"{code} 异常: {e}")
            if len(progress['errors']) < 100:
                progress['errors'].append(f"{code}|{str(e)[:80]}")

        # 每COMMIT_BATCH只commit + 更新进度
        if (i + 1) % COMMIT_BATCH == 0:
            conn.commit()
            elapsed = time.time() - start_time
            speed = (i + 1) / elapsed if elapsed > 0 else 0
            progress['completed'] = len(completed_codes) + i + 1
            progress['speed'] = f"{speed:.1f} stocks/s"
            progress['total_inserted'] = total_inserted
            progress['errors_count'] = errors_count
            eta_seconds = (len(stocks_list) - i - 1) / speed if speed > 0 else 0
            progress['eta_minutes'] = round(eta_seconds / 60, 1)
            update_progress(progress)
            logger.info(f"进度 {i+1}/{len(stocks_list)}, 插入{total_inserted}条, "
                       f"错误{errors_count}, 速度{speed:.1f}/s, ETA {eta_seconds/60:.0f}min")

    conn.commit()
    conn.close()
    bs.logout()

    elapsed = time.time() - start_time
    progress['completed'] = total_stocks
    progress['total_inserted'] = total_inserted
    progress['errors_count'] = errors_count
    progress['finished_at'] = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    progress['elapsed_minutes'] = round(elapsed / 60, 1)
    update_progress(progress)

    logger.info(f"全部完成! 处理{len(stocks_list)}只股票, 插入{total_inserted}条, "
               f"错误{errors_count}, 耗时{elapsed/60:.1f}分钟")


if __name__ == '__main__':
    main()
