#!/bin/bash
# ============================================================
# AIWealth 每日盘后K线数据增量更新
# cron配置: 30 18 * * 1-5 /bin/bash /home/AIWealth/tools/daily_update.sh
# 功能: 每个交易日收盘后自动抓取当天日K+小时K数据
# 注意: BaoStock数据通常18:00后才可用，故定时18:30执行
# ============================================================

set -euo pipefail

SCRIPT_DIR="/home/AIWealth/tools"
LOG_DIR="/home/AIWealth/logs"
LOG_FILE="${LOG_DIR}/daily_update_$(date +%Y%m%d).log"

# 确保日志目录存在
mkdir -p "${LOG_DIR}"

echo "[$(date '+%Y-%m-%d %H:%M:%S')] === 每日数据更新开始 ===" >> "${LOG_FILE}"

# 获取今天日期
TODAY=$(date +%Y-%m-%d)

# 检查是否为交易日（周一至周五）
DOW=$(date +%u)
if [ "$DOW" -gt 5 ]; then
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] 今天是周末，跳过更新" >> "${LOG_FILE}"
    exit 0
fi

# 执行K线抓取
echo "[$(date '+%Y-%m-%d %H:%M:%S')] 开始抓取 ${TODAY} 日K数据..." >> "${LOG_FILE}"
/usr/bin/python3 -u "${SCRIPT_DIR}/fetch_daily_kline.py" "${TODAY}" >> "${LOG_FILE}" 2>&1
EXIT_CODE=$?

if [ $EXIT_CODE -eq 0 ]; then
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] K线抓取完成 (exit=$EXIT_CODE)" >> "${LOG_FILE}"
else
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] K线抓取异常 (exit=$EXIT_CODE)" >> "${LOG_FILE}"
fi

# 验证数据量
COUNT=$(/usr/bin/python3 -c "
import sqlite3
conn = sqlite3.connect('/home/AIWealth/data/stocks.db')
cur = conn.cursor()
cur.execute('SELECT COUNT(*) FROM stock_kline WHERE date=?', ('${TODAY}',))
print(cur.fetchone()[0])
conn.close()
")
echo "[$(date '+%Y-%m-%d %H:%M:%S')] 验证: ${TODAY} 共 ${COUNT} 条K线数据" >> "${LOG_FILE}"

if [ "$COUNT" -lt 1000 ]; then
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] 警告: 数据量不足1000条，可能存在异常！" >> "${LOG_FILE}"
fi

# 更新指数K线(上证综指)
echo "[$(date '+%Y-%m-%d %H:%M:%S')] 开始更新指数K线..." >> "${LOG_FILE}"
/usr/bin/python3 -u -c "
import sqlite3, math
import baostock as bs

DB_PATH = '/home/AIWealth/data/stocks.db'
conn = sqlite3.connect(DB_PATH)

def safe_float(val):
    if val is None or val == '': return None
    try:
        v = float(val)
        return None if math.isnan(v) or math.isinf(v) else v
    except: return None

def calc_rate(price, preclose):
    if price is None or preclose is None or preclose == 0: return None
    return round((price / preclose - 1) * 100, 4)

lg = bs.login()
today = '${TODAY}'
rs = bs.query_history_k_data_plus('sh.000001',
    'date,code,open,high,low,close,preclose,volume,amount',
    start_date=today, end_date=today, frequency='d')

count = 0
while rs.next():
    row = rs.get_row_data()
    date, code = row[0], row[1]
    o, h, l, c, pre = [safe_float(x) for x in row[2:7]]
    vol, amt = safe_float(row[7]), safe_float(row[8])
    vals = [date, code, '上证综指', o, h, l, c, pre, vol, amt,
            calc_rate(o,pre), calc_rate(h,pre), calc_rate(l,pre), calc_rate(c,pre)]
    vals.extend([None]*40)  # hour1-4 placeholders + red_ratio
    vals.append(None)
    conn.execute('INSERT OR REPLACE INTO index_kline VALUES (' + ','.join(['?']*55) + ')', vals)
    count += 1

conn.commit()
conn.close()
bs.logout()
print(f'指数K线更新: {count} 条')
" >> "${LOG_FILE}" 2>&1
echo "[$(date '+%Y-%m-%d %H:%M:%S')] 指数K线更新完成" >> "${LOG_FILE}"

echo "[$(date '+%Y-%m-%d %H:%M:%S')] === 每日数据更新结束 ===" >> "${LOG_FILE}"
