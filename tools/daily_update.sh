#!/bin/bash
# ============================================================
# AIWealth 每日盘后K线数据增量更新
# cron配置: 0 17 * * 1-5 /home/AIWealth/scripts/daily_update.sh
# 功能: 每个交易日收盘后自动抓取当天日K+小时K数据
# ============================================================

set -euo pipefail

SCRIPT_DIR="/home/AIWealth/scripts"
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

echo "[$(date '+%Y-%m-%d %H:%M:%S')] === 每日数据更新结束 ===" >> "${LOG_FILE}"
