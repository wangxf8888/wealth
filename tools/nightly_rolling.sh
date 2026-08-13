#!/bin/bash
# ====================================================================
# nightly_rolling.sh (Task#5) — 每日滚动回测编排器
# --------------------------------------------------------------------
# 编排: daily_rolling_backtest.py → backtest_vs_live_compare.py
# 每步单独容错: 失败写scheduler_alerts + 最终退出码非0, 但不中断后续步骤。
# 红线: 禁止touch交易链任何文件(data/realtime/只读, 不碰crontab/positions)。
# 用法:
#   bash tools/nightly_rolling.sh              # 正式: 全量2021-01-01~最新交易日
#   bash tools/nightly_rolling.sh --dry-run    # 只打印将执行的命令, 不实际运行
# ====================================================================

# ================= 配置区 =================
BASE_DIR="/home/AIWealth"
PY="python3"
ROLLING_SCRIPT="$BASE_DIR/tools/daily_rolling_backtest.py"
COMPARE_SCRIPT="$BASE_DIR/tools/backtest_vs_live_compare.py"
LOG_DIR="$BASE_DIR/logs/rolling"
RUN_LOG="$LOG_DIR/nightly_rolling_$(date +%Y%m%d).log"
ALERT_LOG="$BASE_DIR/logs/realtime/scheduler_alerts.log"
# ================= 配置区结束 =================

set -u   # 未定义变量报错; 不用 set -e (每步单独容错)

DRY_RUN=0
[ "${1:-}" = "--dry-run" ] && DRY_RUN=1

mkdir -p "$LOG_DIR"

ts() { date '+%Y-%m-%d %H:%M:%S'; }

log() {
    echo "[$(ts)] $*" | tee -a "$RUN_LOG"
}

alert() {
    # 与scheduler_alerts.log现有行格式一致: [ts] [TAG] ⚠️ msg
    echo "[$(ts)] [NIGHTLY_ROLLING] ⚠️ $*" >> "$ALERT_LOG"
}

EXIT_CODE=0

run_step() {
    # $1=步骤名  $2...=命令
    local name="$1"; shift
    log "===== 步骤开始: $name ====="
    log "命令: $*"
    if [ "$DRY_RUN" -eq 1 ]; then
        log "[dry-run] 跳过实际执行"
        return 0
    fi
    local t0 t1 rc
    t0=$(date +%s)
    "$@" >> "$RUN_LOG" 2>&1
    rc=$?
    t1=$(date +%s)
    if [ $rc -eq 0 ]; then
        log "===== 步骤成功: $name (耗时 $((t1 - t0))s) ====="
    else
        log "===== 步骤失败: $name (rc=$rc, 耗时 $((t1 - t0))s) ====="
        alert "nightly_rolling 步骤失败: $name (rc=$rc), 详见 $RUN_LOG"
        EXIT_CODE=1   # 记失败但继续后续步骤
    fi
    return $rc
}

log "############ nightly_rolling 启动 (dry_run=$DRY_RUN) ############"

# 步骤1: 每日滚动回测 (全量口径, 两遍约20-30分钟, 内置>40分钟自告警)
run_step "daily_rolling_backtest" \
    "$PY" "$ROLLING_SCRIPT"

# 步骤2: 回测vs实盘对照 (即使步骤1失败也尝试, 用最近一次可用rolling产出)
run_step "backtest_vs_live_compare" \
    "$PY" "$COMPARE_SCRIPT"

log "############ nightly_rolling 结束 (exit=$EXIT_CODE) ############"
exit $EXIT_CODE
