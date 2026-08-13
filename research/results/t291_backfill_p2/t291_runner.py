#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""[Task#291] 全市场普通日5min回补执行器 — 一期t233_runner参数化复用(非fork)。

与一期diff(仅本wrapper, t233_runner.py零改动):
1. 路径重定向: plan2.json/progress.json/night_budget.txt/.runner.lock指向本目录
2. 标签重写: log/alert/qywx/write_re_banned_event四个汇报出口把t233/Task#233/
   T233_BACKFILL统一改写为t291口径(核心逻辑走一期原码, 熔断/让路/限速/账本
   埋点/QA/断点续传逐字复用)
3. 磁盘闸(新增): minute.db所在盘余量-5GB安全线折算可用请求数
   (41日窗单页≈1968行*78B≈151KB/请求), 与night_budget取min作为当晚预算;
   余量耗尽→本晚不开工+告警(全市场回补+22GB > 当前余17GB, 段B需扩容)
4. 额外互斥自检(新增): pgrep t233_runner(一期在跑则退, 双保险;
   fetch_daily/minute互斥仍走一期detect_concurrent_fetch原码)

红线不变: 零并发/0.5s+抖动/批间15s+抖动/10001011熔断/t270 socket守护/
16:00闸(--force人工越过, 用户批准白天照跑)/18:25日更让路/统一账本batch埋点。

用法:
  nohup nice -n 19 python3 t291_runner.py --nightly [--force] >> nightX.log 2>&1 &
  python3 t291_runner.py --status | --qa
"""
import argparse
import fcntl
import json
import os
import subprocess
import sys
from datetime import datetime

sys.path.insert(0, '/home/AIWealth')
sys.path.insert(0, '/home/AIWealth/tools')
sys.path.insert(0, '/home/AIWealth/research/results/t233_touchboard_backfill')

import t233_runner as R      # noqa: E402  t270 socket守护patch随import生效

OUT_DIR = os.path.dirname(os.path.abspath(__file__))
DISK_RESERVE_GB = 5.0        # minute.db盘安全余量(WAL膨胀+系统其他写入)
BYTES_PER_REQ = 41 * 48 * 78  # 单请求最大入库量(41日*48bar*78B/行实测均摊)

# ---- 1. 路径重定向(t233_runner全部经模块级全局引用, 运行时改写即生效) ----
R.OUT_DIR = OUT_DIR
R.PLAN_JSON = os.path.join(OUT_DIR, 'plan2.json')
R.PROGRESS_JSON = os.path.join(OUT_DIR, 'progress.json')
R.LOCK_FILE = os.path.join(OUT_DIR, '.runner.lock')
R.BUDGET_FILE = os.path.join(OUT_DIR, 'night_budget.txt')

# ---- 2. 标签重写(四个汇报出口, 核心逻辑不动; log/alert前缀在一期函数体内
#      拼接, 故整体重定义而非包裹) ----
_orig_qywx, _orig_wrbe = R.qywx, R.write_re_banned_event


def _retag(m):
    return (str(m).replace('Task#233', 'Task#291')
            .replace('T233_BACKFILL', 'T291_BACKFILL')
            .replace('t233', 't291'))


def _t291_log(msg):
    print(f"{datetime.now().strftime('%Y-%m-%d %H:%M:%S')} [t291] "
          f"{_retag(msg)}", flush=True)


def _t291_alert(msg):
    try:
        with open(R.ALERT_LOG, 'a', encoding='utf-8') as f:
            f.write(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] "
                    f"[T291_BACKFILL] {_retag(msg)}\n")
    except OSError:
        pass


R.log = _t291_log
R.alert = _t291_alert
R.qywx = lambda m: _orig_qywx(_retag(m))
R.write_re_banned_event = lambda m: _orig_wrbe(_retag(m))


# ---- 3. 磁盘闸 ----
def disk_budget():
    """余量-5GB安全线折算可用请求数(0=不开工)。"""
    st = os.statvfs('/home/AIWealth/data')
    free_gb = st.f_bavail * st.f_frsize / 1024 ** 3
    n = int(max(0.0, free_gb - DISK_RESERVE_GB) * 1024 ** 3 / BYTES_PER_REQ)
    return free_gb, n


# ---- 4. 额外互斥自检 ----
def t233_running():
    r = subprocess.run(['pgrep', '-f', r't233_runner\.py'],
                       capture_output=True, text=True)
    me = str(os.getpid())
    return [p for p in r.stdout.split() if p and p != me]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--nightly', action='store_true')
    ap.add_argument('--status', action='store_true')
    ap.add_argument('--qa', action='store_true')
    ap.add_argument('--force', action='store_true',
                    help='越过16:00时间闸(白天跑经用户批准)')
    ap.add_argument('--budget', type=int, default=None)
    args = ap.parse_args()
    if args.status:
        R.show_status()
        return 0
    if args.qa:
        prog = R.load_progress()
        with open(R.PLAN_JSON, encoding='utf-8') as f:
            plan = json.load(f)
        done = set(prog['done_ids'])
        pool = [(iv['code'], d) for iv in plan['intervals']
                if iv['id'] in done for d in iv['dates']]
        return 0 if R.qa_sample(pool) else 1
    if not args.nightly:
        ap.print_help()
        return 2

    if t233_running():
        R.log(f"互斥: 一期t233_runner在跑(pid {','.join(t233_running())}), "
              f"零并发红线, 本晚顺延")
        return 0
    free_gb, disk_req = disk_budget()
    budget = args.budget if args.budget is not None else R.read_budget()
    if disk_req <= 0:
        R.log(f"⛔ 磁盘闸: 余量{free_gb:.1f}GB<=安全线{DISK_RESERVE_GB}GB, "
              f"本晚不开工(段B需扩容/清理)")
        R.alert(f"t291磁盘闸触发: 余量{free_gb:.1f}GB, 回补暂停待扩容")
        R.qywx(f"⚠️[Task#291]磁盘余量{free_gb:.1f}GB达安全线, "
               f"回补暂停, 需扩容/清理后续跑")
        return 0
    if disk_req < budget:
        R.log(f"磁盘闸: 余量{free_gb:.1f}GB, 预算{budget}→{disk_req}(受限)")
        budget = disk_req

    lock_f = open(R.LOCK_FILE, 'w')
    try:
        fcntl.flock(lock_f, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        R.log("另一实例在跑(flock), 退出")
        return 0
    try:
        return R.run_night(force=args.force, budget=budget)
    finally:
        fcntl.flock(lock_f, fcntl.LOCK_UN)


if __name__ == '__main__':
    sys.exit(main())
