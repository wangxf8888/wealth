#!/bin/bash
# 每10分钟自动更新PROJECT_STATUS.md的时间戳和运行状态
# 启动方式: nohup bash tools/status_monitor.sh &

STATUS_FILE="/home/AIWealth/PROJECT_STATUS.md"

while true; do
    NOW=$(date '+%Y-%m-%d %H:%M')

    # 更新时间戳（第2行）
    sed -i "s/^\*\*最后更新:.*/**最后更新: ${NOW}**/" "$STATUS_FILE"

    # 统计当前运行的python进程数（策略研究/回测相关）
    RUNNING=$(ps aux | grep -E 'strategy_|backtest|research_' | grep python | grep -v grep | wc -l)

    # 检查scripts/目录下最新修改的文件
    LATEST_SCRIPT=$(find /home/AIWealth/scripts/ -name "*.py" -newer /home/AIWealth/PROJECT_STATUS.md 2>/dev/null | head -3 | tr '\n' ', ')

    # 检查logs/backtest/最新日志
    LATEST_LOG=$(ls -t /home/AIWealth/logs/backtest/*.log 2>/dev/null | head -1)
    LATEST_LOG_TIME=""
    if [ -n "$LATEST_LOG" ]; then
        LATEST_LOG_TIME=$(stat -c '%Y' "$LATEST_LOG" 2>/dev/null)
        LATEST_LOG_NAME=$(basename "$LATEST_LOG")
    fi

    # 写入监控信息到文件末尾的固定区块
    # 先删除旧的监控区块
    sed -i '/^---$/,/^<!-- END MONITOR -->$/d' "$STATUS_FILE"

    # 追加新的监控区块
    cat >> "$STATUS_FILE" << EOF
---

_自动监控 ${NOW} | 活跃进程: ${RUNNING} | 最新日志: ${LATEST_LOG_NAME:-无}_
<!-- END MONITOR -->
EOF

    sleep 600  # 10分钟
done
