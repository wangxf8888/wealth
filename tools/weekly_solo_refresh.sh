#!/bin/bash
# ====================================================================
# weekly_solo_refresh.sh (Task#5) — 每周solo档案刷新
# --------------------------------------------------------------------
# 循环5个在产策略逐个跑solo全周期回测(统一引擎 slot=1, 当前生产参数),
# 复现现有档案口径与产物格式:
#   python3 -m backtest.run_unified --strategies <名> --start 2021-01-01
#       --end <最新交易日> --output-prefix <名> --no-frontend
#   产物: logs/backtest/solo/<名>{.log,_trades.json,_detail.txt}
# 口径纪律:
#   - 大阳低吸(big_yang_low_open_v2)的minute发布口径为策略类级声明
#     (strategies/big_yang_low_open_v2.py: minute_exit=True + G2确认参数,
#     "声明即启用无需CLI"), 现档案133.29%即此口径产物 → 不加--minute-exit
#     旗标即自动保持一致, 全体策略统一命令。
#   - --position-scale off --dd-boost off --promo-gate off 必带(Task#204起
#     CLI默认开启生产overlay: ice35:0.3:repair_exempt + dd-boost 15:1.5;
#     Task#319起再加晋级率过热门控 promo-gate p70:0.3 默认开启; solo档案是
#     纯策略口径, 三overlay必须显式关闭, 否则与registry/看板solo语义漂移)。
#   - --no-frontend 必带(solo实验不得覆盖前端生产数据, 同现档案口径)。
# 随后用python片段更新 data/realtime/solo_strategy_registry.md 中
# 5个在产策略的指标行(只改数值与刷新时间戳行, 其余内容原样保留)。
# 串行执行预计25-40分钟。失败单策略: 写scheduler_alerts + 继续下一个。
# 用法:
#   bash tools/weekly_solo_refresh.sh              # 正式刷新
#   bash tools/weekly_solo_refresh.sh --dry-run    # 只打印命令不执行
# ====================================================================

# ================= 配置区 =================
BASE_DIR="/home/AIWealth"
PY="python3"
SOLO_DIR="$BASE_DIR/logs/backtest/solo"
REGISTRY="$BASE_DIR/data/realtime/solo_strategy_registry.md"
ALERT_LOG="$BASE_DIR/logs/realtime/scheduler_alerts.log"
RUN_LOG="$BASE_DIR/logs/rolling/weekly_solo_refresh_$(date +%Y%m%d).log"
START_DATE="2021-01-01"
# 在产5策略 (与生产slot顺序一致: S1..S5)
STRATEGIES=(
    firstboard_low_open_dip_v2
    amplitude_reversal
    gem_star_late_seal
    big_yang_low_open_v2
    two_board_pullback_dip_h1c
)
# ================= 配置区结束 =================

set -u

DRY_RUN=0
[ "${1:-}" = "--dry-run" ] && DRY_RUN=1

mkdir -p "$SOLO_DIR" "$(dirname "$RUN_LOG")"

ts() { date '+%Y-%m-%d %H:%M:%S'; }
log() { echo "[$(ts)] $*" | tee -a "$RUN_LOG"; }
alert() { echo "[$(ts)] [SOLO_REFRESH] ⚠️ $*" >> "$ALERT_LOG"; }

cd "$BASE_DIR" || { alert "cd $BASE_DIR 失败"; exit 1; }

# 最新交易日 (stocks.db只读; 空表/NULL防护: python侧None一律输出空串)
END_DATE=$("$PY" -c "
import sqlite3
conn = sqlite3.connect('file:$BASE_DIR/data/stocks.db?mode=ro', uri=True)
row = conn.execute('SELECT MAX(date) FROM stock_kline').fetchone()
print(row[0] if row and row[0] is not None else '')
conn.close()")
# 双保险: 空串 或 字符串"None"(防护被绕过时的兜底) 均告警退出
if [ -z "$END_DATE" ] || [ "$END_DATE" = "None" ]; then
    alert "weekly_solo_refresh 查询最新交易日失败 (END_DATE='$END_DATE')"
    exit 1
fi

log "############ weekly_solo_refresh 启动 (区间 $START_DATE ~ $END_DATE, dry_run=$DRY_RUN) ############"

EXIT_CODE=0
OK_LIST=()

for st in "${STRATEGIES[@]}"; do
    CMD=("$PY" -m backtest.run_unified
         --strategies "$st"
         --start "$START_DATE" --end "$END_DATE"
         --output-prefix "$st" --no-frontend
         --position-scale off --dd-boost off --promo-gate off)
    log "----- solo刷新: $st -----"
    log "命令: ${CMD[*]} > $SOLO_DIR/$st.log"
    if [ "$DRY_RUN" -eq 1 ]; then
        log "[dry-run] 跳过实际执行"
        continue
    fi
    t0=$(date +%s)
    "${CMD[@]}" > "$SOLO_DIR/$st.log" 2>&1
    rc=$?
    t1=$(date +%s)
    if [ $rc -eq 0 ] && [ -s "$SOLO_DIR/${st}_trades.json" ]; then
        log "----- $st 成功 (耗时 $((t1 - t0))s) -----"
        OK_LIST+=("$st")
    else
        log "----- $st 失败 (rc=$rc, 耗时 $((t1 - t0))s), 继续下一个 -----"
        alert "weekly_solo_refresh 策略 $st 刷新失败 (rc=$rc), 详见 $SOLO_DIR/$st.log"
        EXIT_CODE=1
    fi
done

# ===== 更新registry指标行 (只改数值与刷新时间戳行, 其余原样) =====
if [ "$DRY_RUN" -eq 1 ]; then
    log "[dry-run] 跳过registry更新"
else
    log "更新registry: $REGISTRY (成功策略: ${OK_LIST[*]:-无})"
    OK_CSV=$(IFS=,; echo "${OK_LIST[*]:-}")
    "$PY" - "$REGISTRY" "$SOLO_DIR" "$OK_CSV" <<'PYEOF' >> "$RUN_LOG" 2>&1
# 只改在产策略指标行数值 + 刷新时间戳行; 文档其余内容原样保留
import json, re, sys
from datetime import datetime

registry, solo_dir, ok_csv = sys.argv[1], sys.argv[2], sys.argv[3]
names = [n for n in ok_csv.split(',') if n]
if not names:
    print('[registry] 无成功刷新的策略, 跳过更新')
    sys.exit(0)

with open(registry, 'r', encoding='utf-8') as f:
    lines = f.read().split('\n')

metrics = {}
for n in names:
    try:
        with open(f'{solo_dir}/{n}_trades.json', 'r', encoding='utf-8') as f:
            s = json.load(f)['summary']
        metrics[n] = s
    except Exception as e:
        print(f'[registry] 读{n}档案失败: {e}, 该行跳过')

stamp = f"> 最近solo刷新: {datetime.now().strftime('%Y-%m-%d %H:%M')} (weekly_solo_refresh, 区间 {next(iter(metrics.values()))['start_date']} ~ {next(iter(metrics.values()))['end_date']})" if metrics else None
updated, stamped, appended = [], False, []

in_main = False  # 指标行更新仅在"## 总表"小节内生效(避免误改"分年收益"等其他9列表格)
for i, line in enumerate(lines):
    if line.startswith('## '):
        in_main = line.strip() == '## 总表'
    # 刷新时间戳行: 已存在则原位替换
    if line.startswith('> 最近solo刷新:') and stamp:
        lines[i] = stamp
        stamped = True
        continue
    if not in_main:
        continue
    # 总表指标行: | slot | 策略名 | CAGR | MDD | 笔数 | 胜率 | 均收益 | 备注 |
    cells = [c.strip() for c in line.split('|')]
    if len(cells) >= 9 and cells[2] in metrics:
        n = cells[2]
        s = metrics[n]
        cells[3] = f" **{s['cagr_pct']:+.2f}%** "
        cells[4] = f" {s['max_drawdown_pct']:.2f}% "
        cells[5] = f" {s['n_trades']} "
        cells[6] = f" {s['win_rate_pct']:.2f}% "
        cells[7] = f" {s['avg_profit_pct']:+.2f}% "
        lines[i] = '|' + '|'.join(
            f' {c.strip()} ' if c.strip() else '' for c in cells[1:-1]) + '|'
        updated.append(n)

# 总表中没有行的在产策略 → 追加到"## 总表"小节的表格末尾
# (只在该小节内扫描, 避免误匹配"分年收益"等其他9列表格)
missing = [n for n in metrics if n not in updated]
if missing:
    tbl_end = None
    in_main = False
    for i, line in enumerate(lines):
        if line.startswith('## '):
            in_main = line.strip() == '## 总表'
            continue
        if not in_main:
            continue
        if re.match(r'^\|\s*\S+\s*\|', line) and '---' not in line:
            cells = [c.strip() for c in line.split('|')]
            if len(cells) >= 9 and cells[2] != '策略':
                tbl_end = i
    if tbl_end is not None:
        for n in missing:
            s = metrics[n]
            row = (f"| 在产 | {n} | **{s['cagr_pct']:+.2f}%** "
                   f"| {s['max_drawdown_pct']:.2f}% | {s['n_trades']} "
                   f"| {s['win_rate_pct']:.2f}% | {s['avg_profit_pct']:+.2f}% "
                   f"| weekly_solo_refresh追加 |")
            lines.insert(tbl_end + 1, row)
            tbl_end += 1
            appended.append(n)

if stamp and not stamped:
    # 无时间戳行则插入到文件头引用块(> 开头行)之后
    for i, line in enumerate(lines):
        if line.startswith('>'):
            continue
        if i > 0:
            lines.insert(i, stamp)
            break

with open(registry, 'w', encoding='utf-8') as f:
    f.write('\n'.join(lines))
print(f'[registry] 更新行: {updated} 追加行: {appended} 时间戳: {"替换" if stamped else "新增"}')
PYEOF
    rc=$?
    if [ $rc -ne 0 ]; then
        alert "weekly_solo_refresh registry更新失败 (rc=$rc), 详见 $RUN_LOG"
        EXIT_CODE=1
    fi
fi

log "############ weekly_solo_refresh 结束 (exit=$EXIT_CODE) ############"
exit $EXIT_CODE
