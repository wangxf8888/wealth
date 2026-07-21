#\!/bin/bash
# 周期检查脚本 - 先用10s验证机制可行
LOG="/home/AIWealth/logs/periodic_trigger.log"
echo "=== 周期检查启动 $(date '+%H:%M:%S') ===" > "$LOG"

for i in $(seq 1 6); do
    sleep 10
    echo "触发#$i $(date '+%H:%M:%S')" >> "$LOG"
done
echo "=== 6次完成 ===" >> "$LOG"
