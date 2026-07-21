#\!/bin/bash
# 10分钟周期STATUS更新
LOG=/home/AIWealth/logs/periodic_driver.log
while true; do
    NOW=$(date '+%Y-%m-%d %H:%M:%S')
    NEXT=$(date -d '+10 minutes' '+%H:%M')
    sed -i "s/^- 本次检查:.*/- 本次检查: $NOW/" /home/AIWealth/PROJECT_STATUS.md
    sed -i "s/^- 下次检查:.*/- 下次检查: ~$NEXT/" /home/AIWealth/PROJECT_STATUS.md
    sed -i "s/^\*\*最后更新\*\*:.*/\*\*最后更新\*\*: $NOW/" /home/AIWealth/PROJECT_STATUS.md
    echo "[$NOW] STATUS updated" >> $LOG
    sleep 600
done
