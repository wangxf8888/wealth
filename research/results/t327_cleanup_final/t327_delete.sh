#\!/bin/bash
# Task#327 删除执行器: 严格按DELETE_MANIFEST执行, 逐条留证
R=/home/AIWealth
LOG=$R/research/results/t327_cleanup_final/DELETION_EXECUTED.log
KEEP_RESULTS="shadow_surge t228_surge_replay board_lab t291_backfill_p2 t311_promo_menu t313_sentinel_0812 t319_promo_c_backtest t320_promo_c_live t321_promo_c_qa t322_frontend_fix t327_cleanup_final"
NF=0; ND=0
del() {
  local p="$R/$1"
  if [ -d "$p" ]; then
    local n=$(find "$p" -type f | wc -l)
    rm -rf "$p" && { echo "[DIR-DEL] $1 (${n} files)" >> $LOG; ND=$((ND+1)); NF=$((NF+n)); }
  elif [ -f "$p" ]; then
    rm -f "$p" && { echo "[FILE-DEL] $1" >> $LOG; NF=$((NF+1)); }
  else
    echo "[SKIP-NOTFOUND] $1" >> $LOG
  fi
}
echo "=== Task#327 deletion started $(date '+%F %T') ===" > $LOG
df -B1 --output=used / | tail -1 > $R/research/results/t327_cleanup_final/used_before.txt

del strategies/firstboard_low_open_dip.py
del strategies/two_board_pullback_dip.py
rm -f $R/strategies/__pycache__/firstboard_low_open_dip.cpython*.pyc $R/strategies/__pycache__/two_board_pullback_dip.cpython*.pyc 2>/dev/null
echo "[NOTE] strategies/__pycache__ 旧版pyc已清(防僵尸字节码)" >> $LOG

cd $R/research/results
for e in *; do
  skip=0; for k in $KEEP_RESULTS; do [ "$e" = "$k" ] && skip=1; done
  [ $skip -eq 1 ] && continue
  del "research/results/$e"
done
cd $R

cd $R/research
for e in *; do
  [ "$e" = "results" ] && continue
  del "research/$e"
done
cd $R

del .trash_t210
del .trash_t300

for f in anchor_run.out build_w31.log combo.log full_r1b.log full_r2.log full_v1.log log_v2.txt s5_gate4.log nohup.out server.py.bak_20260808_t232; do del "$f"; done

for f in tools/t128_backfill_hour_20260805.py tools/emergency_timed_sz300789.py tools/sentinel_20260806.sh tools/task71_archive.sh tools/pipeline_checker.sh tools/seal_reseal_detector.py tools/shadow_tracker.py tools/backfill_block_trade.py tools/fetch_block_trade_daily.py tools/fetch_margin_daily.py tools/backfill_margin.py tools/backfill_minute_touchdays.py; do del "$f"; done

cd $R/backup
for e in *; do
  case "$e" in *t319*|*t320*|*t324*) echo "[KEEP] backup/$e" >> $LOG; continue;; esac
  del "backup/$e"
done
cd $R

df -B1 --output=used / | tail -1 > $R/research/results/t327_cleanup_final/used_after.txt
echo "=== done $(date '+%F %T') files=$NF dirs=$ND ===" >> $LOG
echo "FILES_DELETED=$NF DIRS_DELETED=$ND"
