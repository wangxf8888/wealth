#\!/bin/bash
# Task#320 收盘后部署脚本 (仅15:00后执行\!)
# 逐文件备份.bak_20260812_t320 → 落地staging → ast语法验证
set -e
cd /home/AIWealth
HM=$(date '+%H%M')
if [ "$HM" -lt 1500 ]; then
    echo "[ABORT] 当前${HM}, 未到15:00收盘, 禁止落地生产文件"; exit 1
fi
STG=research/results/t320_promo_c_live/staging/realtime
FILES="config.py morning_decision.py generate_candidates.py position_tracker.py notify.py"
echo "== 备份 =="
for f in $FILES; do
    cp -p realtime/$f realtime/$f.bak_20260812_t320
    echo "  realtime/$f -> realtime/$f.bak_20260812_t320"
done
echo "== 落地 =="
for f in $FILES; do
    cp $STG/$f realtime/$f
    python3 -c "import ast; ast.parse(open('realtime/$f').read())" && echo "  realtime/$f 落地+语法OK"
done
# 新文件promo_gate.py(无旧版可备份)
cp $STG/promo_gate.py realtime/promo_gate.py
python3 -c "import ast; ast.parse(open('realtime/promo_gate.py').read())" && echo "  realtime/promo_gate.py 新增+语法OK"
echo "== 生产导入冒烟 =="
python3 -c "
import sys; sys.path.insert(0, '/home/AIWealth')
from realtime.config import PROMO_GATE_ENABLED, PROMO_GATE_THRESHOLD, S5_H1_TOUCH_ENABLED
from realtime import promo_gate
import realtime.position_tracker as pt
import realtime.notify as nt
import inspect
assert PROMO_GATE_ENABLED and PROMO_GATE_THRESHOLD == 0.30 and S5_H1_TOUCH_ENABLED
assert 'gate_note' in inspect.signature(nt.notify_morning_summary).parameters
assert pt.S5_H1_TOUCH_ENABLED is True
print('生产导入冒烟OK')
"
echo "== 部署完成 $(date '+%F %T') =="
