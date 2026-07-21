#\!/bin/bash
# 生产版：每600秒更新PROJECT_STATUS.md时间戳 + 记录心跳日志
STATUS="/home/AIWealth/PROJECT_STATUS.md"
HEARTBEAT="/home/AIWealth/logs/periodic_heartbeat.log"

echo "=== 周期更新器启动 $(date '+%Y-%m-%d %H:%M:%S') ===" > "$HEARTBEAT"

while true; do
    sleep 600
    NOW=$(date '+%Y-%m-%d %H:%M:%S')
    NOW_SHORT=$(date '+%H:%M')
    
    # 更新STATUS文件时间戳
    sed -i "s/\*\*最后更新\*\*: .*/\*\*最后更新\*\*: $NOW/" "$STATUS"
    
    # 更新下次检查时间
    NEXT=$(date -d '+10 minutes' '+%H:%M' 2>/dev/null || date '+%H:%M')
    sed -i "s/本次检查: .*/本次检查: $NOW/" "$STATUS"
    sed -i "s/下次检查: .*/下次检查: ~$NEXT/" "$STATUS"
    
    # 写心跳日志
    echo "心跳#$(grep -c '心跳#' "$HEARTBEAT" 2>/dev/null | awk '{print $1+1}') $NOW" >> "$HEARTBEAT"
done
