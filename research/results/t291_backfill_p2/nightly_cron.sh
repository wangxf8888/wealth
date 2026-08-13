#!/bin/bash
# [Task#291] 全市场普通日5min回补 — 每晚cron wrapper(完成态自删cron条目)
# 复制一期t233模式; 摘除方式: crontab -l | grep -v 'Task#291' | crontab -
cd /home/AIWealth || exit 1
DIR=research/results/t291_backfill_p2
STATE=$(python3 -c "import json;print(json.load(open('$DIR/progress.json')).get('state',''))" 2>/dev/null)
if [ "$STATE" = "all_done" ]; then
    # 自删逻辑: backup留证后摘除含Task#291标记的行(注释行+命令行)
    crontab -l > "data/crontab_backup_$(date +%Y%m%d)_t291_selfremove.txt"
    crontab -l | grep -v 'Task#291' | crontab -
    echo "$(date '+%F %T') [t291] 计划all_done, cron条目已自删(backup已留data/)" >> "$DIR/cron.log"
    exit 0
fi
LOG="$DIR/night_$(date +%Y%m%d).log"
nice -n 19 python3 "$DIR/t291_runner.py" --nightly >> "$LOG" 2>&1
