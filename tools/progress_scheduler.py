#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
AIWealth 10分钟周期进展调度器（Task #6，取代旧 periodic_* / status_monitor 系列脚本）

每10分钟由cron触发一次，完成三件事：
1. 进展采集：扫描 logs/backtest/ 24h内有更新的 *.log（mtime+最后一行），
   列出存活的 python3 回测/网格/研究进程（run_unified|grid_|research_ 等）
2. STATUS更新：原子替换 PROJECT_STATUS.md 中本脚本专属分区（唯一标记行包裹），
   绝不触碰文件其他部分（写前读最新内容 -> 内存替换分区 -> 同目录临时文件+rename）
3. 停滞告警：进程存活但日志>20分钟无更新；或上个快照存在的进程消失且日志尾部
   无"完成/DONE/耗时"等收尾字样（疑似异常死亡）-> 分区告警区标⚠️ 并追加
   logs/realtime/scheduler_alerts.log

幂等性：分区靠唯一标记替换不追加；flock防并发重入。
快照历史：logs/realtime/progress_snapshots.log
上个快照状态：logs/realtime/scheduler_state.json
"""

import fcntl
import json
import os
import re
import sqlite3
import subprocess
import sys
import tempfile
import time
from datetime import datetime

ROOT = "/home/AIWealth"
STATUS_FILE = os.path.join(ROOT, "PROJECT_STATUS.md")
STOCKS_DB = os.path.join(ROOT, "data", "stocks.db")
BAN_STATE_FILE = os.path.join(ROOT, "data", "baostock_ban_status.json")
POSITIONS_FILE = os.path.join(ROOT, "data", "realtime", "positions.json")
SNAPSHOT_FILE = os.path.join(ROOT, "data", "realtime", "intraday_snapshot.json")
BACKTEST_LOG_DIR = os.path.join(ROOT, "logs", "backtest")
RT_LOG_DIR = os.path.join(ROOT, "logs", "realtime")
SNAPSHOT_LOG = os.path.join(RT_LOG_DIR, "progress_snapshots.log")
ALERT_LOG = os.path.join(RT_LOG_DIR, "scheduler_alerts.log")
STATE_FILE = os.path.join(RT_LOG_DIR, "scheduler_state.json")
LOCK_FILE = os.path.join(RT_LOG_DIR, ".progress_scheduler.lock")

MARK_START = "<!-- AUTO_PROGRESS_MONITOR_START (progress_scheduler.py 专属分区,勿手工编辑) -->"
MARK_END = "<!-- AUTO_PROGRESS_MONITOR_END -->"

# 进程匹配规则：python3 命令行含以下任一片段即视为回测/网格/研究进程
PROC_PATTERN = re.compile(r"run_unified|run_all5|run_combined|grid_|research_")
# 日志活跃窗口 / 停滞阈值
ACTIVE_WINDOW_SEC = 24 * 3600
STALL_SEC = 20 * 60
# 正常收尾关键字（日志尾部出现任一则认为进程正常结束）
DONE_PATTERN = re.compile(r"完成|结束|DONE|Done|done|耗时|elapsed|finish|Finish|complete|Complete|SUCCESS")


def now_str():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def ago_str(sec):
    sec = int(sec)
    if sec < 60:
        return f"{sec}秒前"
    if sec < 3600:
        return f"{sec // 60}分钟前"
    return f"{sec // 3600}小时{(sec % 3600) // 60}分前"


def last_line(path, max_len=120):
    """取文件最后一个非空行（截断），容错读取"""
    try:
        with open(path, "rb") as f:
            f.seek(0, os.SEEK_END)
            size = f.tell()
            f.seek(max(0, size - 8192))
            tail = f.read().decode("utf-8", errors="replace")
        for line in reversed(tail.splitlines()):
            line = line.strip()
            if line:
                line = line.replace("|", "\\|")
                return line[:max_len] + ("…" if len(line) > max_len else "")
    except Exception:
        pass
    return "(读取失败/空)"


def tail_text(path, nbytes=4096):
    try:
        with open(path, "rb") as f:
            f.seek(0, os.SEEK_END)
            size = f.tell()
            f.seek(max(0, size - nbytes))
            return f.read().decode("utf-8", errors="replace")
    except Exception:
        return ""


def scan_logs():
    """扫描 logs/backtest/ 下24h内有更新的 *.log"""
    now = time.time()
    items = []
    if os.path.isdir(BACKTEST_LOG_DIR):
        for name in sorted(os.listdir(BACKTEST_LOG_DIR)):
            if not name.endswith(".log"):
                continue
            p = os.path.join(BACKTEST_LOG_DIR, name)
            try:
                mtime = os.path.getmtime(p)
            except OSError:
                continue
            if now - mtime <= ACTIVE_WINDOW_SEC:
                items.append({
                    "name": name, "path": p, "mtime": mtime,
                    "mtime_str": datetime.fromtimestamp(mtime).strftime("%m-%d %H:%M:%S"),
                    "ago": ago_str(now - mtime),
                    "last": last_line(p),
                })
    items.sort(key=lambda x: -x["mtime"])
    return items


def proc_open_logs(pid):
    """通过 /proc/<pid>/fd 找进程实际写入的 logs/ 下的 .log 文件"""
    logs = []
    fd_dir = f"/proc/{pid}/fd"
    try:
        for fd in os.listdir(fd_dir):
            try:
                target = os.readlink(os.path.join(fd_dir, fd))
            except OSError:
                continue
            if target.startswith(os.path.join(ROOT, "logs")) and target.endswith(".log"):
                logs.append(target)
    except OSError:
        pass
    return sorted(set(logs))


def scan_procs():
    """列出存活的 python3 回测/网格/研究进程"""
    me = os.getpid()
    procs = []
    try:
        out = subprocess.run(
            ["ps", "-eo", "pid,lstart,pcpu,args"],
            capture_output=True, text=True, timeout=10).stdout
    except Exception:
        return procs
    for line in out.splitlines()[1:]:
        # 列结构: pid + lstart(5列: 星期 月 日 时间 年) + pcpu + args
        parts = line.split(None, 7)
        if len(parts) < 8:
            continue
        pid, lstart, pcpu, args = parts[0], " ".join(parts[1:6]), parts[6], parts[7]
        if int(pid) == me:
            continue
        if "python" not in args:
            continue
        if "progress_scheduler" in args or "grep" in args.split()[0]:
            continue
        open_logs = proc_open_logs(int(pid))
        # 纳入条件: 命令行匹配名称规则, 或进程打开着 logs/backtest/ 下的日志(功能性识别)
        if not PROC_PATTERN.search(args) and not any(
                l.startswith(BACKTEST_LOG_DIR) for l in open_logs):
            continue
        # 提取脚本名
        script = ""
        for tok in args.split():
            if tok.endswith(".py"):
                script = os.path.basename(tok)
                break
        # fallback: 按脚本名同名日志匹配
        if not open_logs and script:
            guess = os.path.join(BACKTEST_LOG_DIR, script.replace(".py", ".log"))
            if os.path.exists(guess):
                open_logs = [guess]
        procs.append({
            "pid": int(pid), "start": lstart, "cpu": pcpu,
            "script": script or args[:60],
            "cmd": args[:150].replace("|", "\\|"),
            "logs": open_logs,
        })
    return procs


def load_state():
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def save_state(state):
    tmp = STATE_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=1)
    os.replace(tmp, STATE_FILE)


def build_alerts(procs, prev_state):
    """停滞与异常死亡检测"""
    now = time.time()
    alerts = []
    # 1) 进程存活但日志>20分钟无更新
    for p in procs:
        if not p["logs"]:
            continue
        freshest = max((os.path.getmtime(lp) for lp in p["logs"] if os.path.exists(lp)),
                       default=0)
        if freshest and now - freshest > STALL_SEC:
            alerts.append(
                f"⚠️ 停滞: PID {p['pid']} {p['script']} 存活, 但日志 "
                f"{','.join(os.path.basename(l) for l in p['logs'])} 已 "
                f"{ago_str(now - freshest)} 无更新(阈值20分钟)")
    # 2) 上个快照存在、现已消失且日志尾部无收尾字样 -> 疑似异常死亡
    cur_pids = {p["pid"] for p in procs}
    for p in prev_state.get("processes", []):
        if p["pid"] in cur_pids:
            continue
        logs = p.get("logs") or []
        tail = ""
        for lp in logs:
            if os.path.exists(lp):
                tail += tail_text(lp)
        if logs and tail and DONE_PATTERN.search(tail[-2000:]):
            continue  # 正常收尾
        detail = f"日志尾部无完成标记" if logs else "未定位到日志"
        alerts.append(
            f"⚠️ 疑似异常死亡: PID {p['pid']} {p['script']} 已消失且{detail} "
            f"(logs: {','.join(os.path.basename(l) for l in logs) or '无'})")
    return alerts


def build_section(ts, procs, logs, alerts):
    lines = [MARK_START,
             "## ⏱️ 10分钟周期监控（自动生成）",
             f"> 由 `tools/progress_scheduler.py` 每10分钟cron自动更新, 仅原子替换本分区。",
             f"- **快照时间**: {ts}",
             "",
             "### 存活的回测/网格/研究进程"]
    if procs:
        lines.append("| PID | 启动时间 | CPU% | 脚本 | 关联日志 |")
        lines.append("|---|---|---|---|---|")
        for p in procs:
            log_s = ", ".join(os.path.basename(l) for l in p["logs"]) or "未定位"
            lines.append(f"| {p['pid']} | {p['start']} | {p['cpu']} | {p['script']} | {log_s} |")
    else:
        lines.append("- 无匹配进程 (run_unified/grid_/research_ 等)")
    lines += ["", "### 活跃日志进展 (logs/backtest/ 24h内有更新)"]
    if logs:
        lines.append("| 日志 | 最后更新 | 距今 | 最后一行 |")
        lines.append("|---|---|---|---|")
        for l in logs:
            lines.append(f"| {l['name']} | {l['mtime_str']} | {l['ago']} | {l['last']} |")
    else:
        lines.append("- 24h内无日志更新")
    lines += ["", "### 告警"]
    if alerts:
        lines += [f"- {a}" for a in alerts]
    else:
        lines.append("- 无告警 ✅")
    lines.append(MARK_END)
    return "\n".join(lines)


def build_db_status_line(content):
    """生成头部"**数据库状态**"行(根治写死日期过期问题):
    - 最新日期/当日行数: stocks.db MAX(date)实查
    - 源标注: baostock_ban_status.json recovered字段(false=腾讯降级源)
    - hour列状态: 近60天逐日hour1_close非NULL占比, 全>90%=完整,
      否则报最早不达标日"YYYY-MM-DD起待回补"
    - 尾部"; 历史..."手工回补说明从现有行提取原样保留
    任一环节失败返回None(保留原行不动, 防御优先)。"""
    try:
        conn = sqlite3.connect(f"file:{STOCKS_DB}?mode=ro", uri=True, timeout=5)
        max_date = conn.execute("SELECT MAX(date) FROM stock_kline").fetchone()[0]
        if not max_date:
            conn.close()
            return None
        n_rows = conn.execute(
            "SELECT COUNT(*) FROM stock_kline WHERE date=?", (max_date,)).fetchone()[0]
        cov = conn.execute(
            "SELECT date, AVG(hour1_close IS NOT NULL) FROM stock_kline "
            "WHERE date >= date(?, '-60 day') GROUP BY date ORDER BY date",
            (max_date,)).fetchall()
        conn.close()
    except Exception:
        return None
    # 源标注: recovered=false → 腾讯降级源; true/无状态文件 → BaoStock主源
    src = "BaoStock主源"
    try:
        with open(BAN_STATE_FILE, encoding="utf-8") as f:
            if not json.load(f).get("recovered", True):
                src = "腾讯降级源"
    except Exception:
        pass
    pending = [d for d, ratio in cov if (ratio or 0) <= 0.9]
    hour_s = "hour列完整" if not pending else f"hour列{pending[0]}起待回补"
    # 保留现有行尾部的手工历史回补说明(以"; 历史"开头至行尾)
    hist = ""
    m = re.search(r"^\*\*数据库状态\*\*: .*?(;\s*历史[^\n]*)$", content, re.M)
    if m:
        hist = m.group(1)
    return (f"**数据库状态**: 最新数据到 {max_date}（{n_rows}只, {src}）; "
            f"{hour_s}{hist}")


def build_positions_line():
    """生成头部"- **实盘持仓概况**"行(与数据库状态行同款自动接管):
    - 持仓明细+现金: data/realtime/positions.json (status=holding)
    - 盘中实时价: intraday_snapshot.json 存在且 trade_date=当日 → 用实时pnl盯市;
      否则降级按成本估值并标注"按成本"
    任一环节失败返回None(保留原行不动, 防御优先)。"""
    try:
        with open(POSITIONS_FILE, encoding="utf-8") as f:
            pos_data = json.load(f)
        cash = float(pos_data["account"]["cash"])
        holdings = [p for p in pos_data.get("positions", [])
                    if p.get("status") == "holding"]
    except Exception:
        return None
    # 实时快照: 仅当日快照可用于盯市
    live, snap_ts = {}, None
    try:
        with open(SNAPSHOT_FILE, encoding="utf-8") as f:
            snap = json.load(f)
        if snap.get("trade_date") == datetime.now().strftime("%Y-%m-%d"):
            live = {p["code"]: p for p in snap.get("positions", [])
                    if p.get("pnl_pct") is not None}
            snap_ts = snap.get("updated_at")
    except Exception:
        pass
    parts, mkt_val, all_live = [], 0.0, bool(holdings)
    for p in holdings:
        try:
            amt = float(p["buy_amount"])
        except Exception:
            return None
        lp = live.get(p.get("code"))
        if lp:
            pnl = float(lp["pnl_pct"])
            parts.append(f"{p.get('name', p.get('code', '?'))}{pnl:+.1f}%")
            mkt_val += amt * (1 + pnl / 100)
        else:
            all_live = False
            parts.append(f"{p.get('name', p.get('code', '?'))}(按成本)")
            mkt_val += amt
    nav = cash + mkt_val
    if all_live and snap_ts:
        ts_s = snap_ts[5:16]  # "MM-DD HH:MM"
    else:
        ts_s = datetime.now().strftime("%m-%d") + " 按成本"
    pos_s = f"{len(holdings)}仓({'/'.join(parts)})" if holdings else "0仓(空仓)"
    return (f"- **实盘持仓概况**: {pos_s} 现金¥{cash:,.0f} "
            f"盯市净值¥{nav:,.0f}({ts_s})")


def update_status(section, ts, n_procs, n_alerts):
    """原子替换 PROJECT_STATUS.md 专属分区 + 头部"**最后更新**"时间戳行
    + 头部"**数据库状态**"/"实盘持仓概况"行（同一次写入）"""
    # 写前读最新内容
    with open(STATUS_FILE, "r", encoding="utf-8") as f:
        content = f.read()
    if MARK_START in content and MARK_END in content:
        pre = content.split(MARK_START, 1)[0]
        post = content.split(MARK_END, 1)[1]
        new_content = pre + section + post
    else:
        sep = "" if content.endswith("\n\n") else ("\n" if content.endswith("\n") else "\n\n")
        new_content = content + sep + section + "\n"
    # 头部时间戳: 只替换第一处匹配行; 不存在则不添加(防御,避免破坏结构)
    new_content = re.sub(
        r"^\*\*最后更新\*\*: .*$",
        f"**最后更新**: {ts}（自动监控: 进程{n_procs}个/告警{n_alerts}条）",
        new_content, count=1, flags=re.M)
    # 头部数据库状态行: 同样只替第一处, 生成失败(None)则原行不动
    db_line = build_db_status_line(new_content)
    if db_line:
        new_content = re.sub(
            r"^\*\*数据库状态\*\*: .*$", lambda _m: db_line,
            new_content, count=1, flags=re.M)
    # 实盘持仓概况行: 同款模式, 生成失败(None)则原行不动
    pos_line = build_positions_line()
    if pos_line:
        new_content = re.sub(
            r"^- \*\*实盘持仓概况\*\*: .*$", lambda _m: pos_line,
            new_content, count=1, flags=re.M)
    if new_content == content:
        return
    # 同目录临时文件 + rename 原子替换
    d = os.path.dirname(STATUS_FILE)
    fd, tmp = tempfile.mkstemp(dir=d, prefix=".status_tmp_")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(new_content)
        os.chmod(tmp, 0o644)
        os.replace(tmp, STATUS_FILE)
    except Exception:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise


def append_snapshot(ts, procs, logs, alerts):
    os.makedirs(RT_LOG_DIR, exist_ok=True)
    with open(SNAPSHOT_LOG, "a", encoding="utf-8") as f:
        f.write(f"===== 快照 {ts} =====\n")
        f.write(f"进程({len(procs)}): " +
                ("; ".join(f"pid={p['pid']} {p['script']}" for p in procs) or "无") + "\n")
        for l in logs:
            f.write(f"日志 {l['name']} mtime={l['mtime_str']} ({l['ago']}) 尾行: {l['last']}\n")
        for a in alerts:
            f.write(f"告警: {a}\n")
        f.write("\n")


def append_alerts(ts, alerts):
    if not alerts:
        return
    with open(ALERT_LOG, "a", encoding="utf-8") as f:
        for a in alerts:
            f.write(f"[{ts}] {a}\n")


def main():
    os.makedirs(RT_LOG_DIR, exist_ok=True)
    # flock 防并发重入
    lock = open(LOCK_FILE, "w")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        print(f"[{now_str()}] 已有实例在运行, 跳过")
        return 0
    ts = now_str()
    prev_state = load_state()
    procs = scan_procs()
    logs = scan_logs()
    alerts = build_alerts(procs, prev_state)
    section = build_section(ts, procs, logs, alerts)
    update_status(section, ts, len(procs), len(alerts))
    append_snapshot(ts, procs, logs, alerts)
    append_alerts(ts, alerts)
    save_state({"ts": ts, "processes": [
        {"pid": p["pid"], "script": p["script"], "logs": p["logs"]} for p in procs]})
    print(f"[{ts}] OK 进程={len(procs)} 活跃日志={len(logs)} 告警={len(alerts)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
