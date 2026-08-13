#!/bin/bash
# =============================================================================
# Task#38 首跑取证快照 (2026-08-03 四件套变更首跑观察, 一次性工具)
# 用途: 9:45(决策后)/10:40(塞力医疗10:30定时卖后)由dated cron触发,
#       把决策/持仓/监控日志关键证据快照到 logs/realtime/firstrun_obs_HHMM.md
# 原则: 只读取证, 零写入业务文件; 源文件不存在时优雅标注"未生成"不报错退出
# =============================================================================
BASE=/home/AIWealth
TODAY=$(date +%Y%m%d)
OUT=$BASE/logs/realtime/firstrun_obs_$(date +%H%M).md
DECISION=$BASE/data/realtime/decision_${TODAY}.json
POSITIONS=$BASE/data/realtime/positions.json
MON_LOG=$BASE/logs/realtime/intraday_monitor.log
MD_LOG=$BASE/logs/realtime/morning_decision.log
ALERT_LOG=$BASE/logs/realtime/scheduler_alerts.log
SAILI_CODE=sh.603716            # 塞力医疗(10:30定时卖出观察对象)

{
echo "# 首跑取证快照 — $(date '+%Y-%m-%d %H:%M:%S') (Task#38)"
echo

# ---- 1. 决策文件关键字段(vc_allocation全量 / position_scale链 / repair_exempt) ----
echo "## 1. decision_${TODAY}.json 关键字段"
if [ -f "$DECISION" ]; then
    python3 - "$DECISION" <<'PYEOF'
import json, sys
d = json.load(open(sys.argv[1]))

def find_key(obj, key, path=''):
    """递归查找键, 返回[(路径, 值)] — 兼容新字段落点变动。"""
    hits = []
    if isinstance(obj, dict):
        for k, v in obj.items():
            p = f'{path}.{k}' if path else k
            if k == key:
                hits.append((p, v))
            hits.extend(find_key(v, key, p))
    elif isinstance(obj, list):
        for i, v in enumerate(obj):
            hits.extend(find_key(v, key, f'{path}[{i}]'))
    return hits

print(f"- trade_date={d.get('trade_date')} decided_at={d.get('decided_at')} "
      f"framework_version={d.get('framework_version')}")

for key in ('vc_allocation', 'position_scale', 'repair_exempt'):
    hits = find_key(d, key)
    if not hits:
        print(f"\n### {key}: [字段缺失]")
        continue
    for p, v in hits:
        print(f"\n### {key} (@{p})")
        print('```json')
        print(json.dumps(v, ensure_ascii=False, indent=2))
        print('```')
        if key == 'repair_exempt' and isinstance(v, dict):
            print(f"(键数={len(v)}, 预期7键)")
PYEOF
else
    echo "[未生成] $DECISION"
fi
echo

# ---- 2. 持仓概览 ----
echo "## 2. positions.json 持仓概览"
if [ -f "$POSITIONS" ]; then
    python3 - "$POSITIONS" <<'PYEOF'
import json, sys
p = json.load(open(sys.argv[1]))
acc = p.get('account', {})
print(f"- 现金={acc.get('cash')} 总净值={acc.get('total_nav')} "
      f"已实现盈亏={acc.get('realized_pnl')}")
print('| 代码 | 名称 | 策略 | 状态 | 买入日 | 买价 | 卖出方式 |')
print('|---|---|---|---|---|---|---|')
for pos in p.get('positions', []):
    print(f"| {pos.get('code')} | {pos.get('name')} | {pos.get('strategy')} "
          f"| {pos.get('status')} | {pos.get('buy_date')} "
          f"| {pos.get('buy_price')} | {pos.get('sell_at') or pos.get('sell_mode')} |")
PYEOF
else
    echo "[未生成] $POSITIONS"
fi
echo

# ---- 3. 塞力医疗状态行 ----
echo "## 3. 塞力医疗 $SAILI_CODE 状态"
if [ -f "$POSITIONS" ]; then
    if grep -q "$SAILI_CODE" "$POSITIONS"; then
        python3 - "$POSITIONS" "$SAILI_CODE" <<'PYEOF'
import json, sys
p = json.load(open(sys.argv[1]))
code = sys.argv[2]
found = [x for x in p.get('positions', []) if x.get('code') == code]
for pos in found:
    print('```json'); print(json.dumps(pos, ensure_ascii=False, indent=2)); print('```')
closed = [t for t in p.get('closed_trades', []) if t.get('code') == code]
for t in closed[-2:]:
    print('已平仓记录:')
    print('```json'); print(json.dumps(t, ensure_ascii=False, indent=2)); print('```')
if not found and not closed:
    print(f'[提示] {code} 出现在文件中但不在positions/closed_trades结构内')
PYEOF
    else
        echo "[提示] positions.json 中无 $SAILI_CODE 记录"
    fi
else
    echo "[未生成] $POSITIONS"
fi
echo

# ---- 4. 日志尾部 ----
echo "## 4. intraday_monitor.log 尾30行"
if [ -f "$MON_LOG" ]; then
    echo '```'; tail -30 "$MON_LOG"; echo '```'
else
    echo "[未生成] $MON_LOG"
fi
echo

echo "## 5. morning_decision.log 尾20行"
if [ -f "$MD_LOG" ]; then
    echo '```'; tail -20 "$MD_LOG"; echo '```'
else
    echo "[未生成] $MD_LOG"
fi
echo

echo "## 6. scheduler_alerts.log 尾10行"
if [ -f "$ALERT_LOG" ]; then
    echo '```'; tail -10 "$ALERT_LOG"; echo '```'
else
    echo "[未生成] $ALERT_LOG"
fi
} > "$OUT"

echo "[observe_firstrun] 快照已落盘: $OUT ($(date '+%H:%M:%S'))"
