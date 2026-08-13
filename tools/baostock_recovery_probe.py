#!/usr/bin/env python3
"""baostock_recovery_probe.py - BaoStock封禁恢复探测 [Task #31 → Task#194冷静期改造]

背景: 2026-07-24 BaoStock账号被封禁(login 10001011黑名单, 诱因3.2req/s轰炸)。
本脚本每小时探测一次login是否恢复(单次login请求, 无数据拉取, 不加重封禁)。

[Task#194] 2026-08-05二次封禁事故教训: 17:10解禁探测成功→钩子立即全量回补
5193只股(87min高强度请求)→18:31再次被封。改造为冷静期二次确认状态机:
  banned ──login成功──> pending_confirm (写pending_since时间戳, 不回补)
  pending_confirm ──冷静期(≥55min)后再次login成功──> recovered (启动回补钩子)
  pending_confirm ──任何一次login失败──> banned (清除pending, 记history事件)
每小时cron两次探测间隔恰好60min = 冷静期; 回补钩子调起的baostock_recovery.py
静默窗口已延至16:00(Task#194), 交易时段确认解禁时回补自动推迟到16:00后由
resume cron续跑, 探测本身不受限。

- 未恢复: 仅写本脚本日志, 不告警(避免告警刷屏)
- 进入pending: 写alert [RECOVERY_PENDING]行(提示冷静期观察中)
- 恢复确认:   写alert [RECOVERY]行 + 更新状态文件 + STATUS区块标注
- 状态文件已标记recovered后直接退出(幂等, cron行无需摘除)

cron: 10 8-17 * * * python3 /home/AIWealth/tools/baostock_recovery_probe.py

dry-run(不真实请求BaoStock, Task#194验证用):
  python3 tools/baostock_recovery_probe.py --dry-run --mock-login ok \
      --state-file /tmp/mock_ban_status.json
"""
import argparse
import json
import os
import sys
from datetime import datetime

# [Task#265] 统一日预算接入(Emil-Major: "login/探测调用也计数"):
# 模块导入fail-open — 缺失/异常不阻断探测(探测可用性优先)
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
try:
    import baostock_budget as _bb
except Exception:
    _bb = None

STATE_FILE = '/home/AIWealth/data/baostock_ban_status.json'
ALERT_FILE = '/home/AIWealth/logs/realtime/scheduler_alerts.log'
LOG_FILE = '/home/AIWealth/logs/realtime/baostock_recovery_probe.log'
STATUS_MD = '/home/AIWealth/PROJECT_STATUS.md'
PLACEHOLDER = '恢复观察中(baostock_recovery_probe每小时探测)'
# [Task#194] 冷静期阈值: 标称60min(小时cron自然间隔), 留5min容差抗cron抖动
COOLDOWN_MINUTES = 55


def log(msg):
    line = f"{datetime.now().strftime('%Y-%m-%d %H:%M:%S')} {msg}"
    print(line)
    try:
        with open(LOG_FILE, 'a') as f:
            f.write(line + '\n')
    except Exception:
        pass


def now_str():
    return datetime.now().strftime('%Y-%m-%d %H:%M:%S')


def save_state(st, path):
    json.dump(st, open(path, 'w'), ensure_ascii=False, indent=2)


def append_history(st, event):
    # [Task#194] 只增不改: history数组追加事件, 字段风格与既有事件一致
    st.setdefault('history', []).append(event)


def write_alert(msg, dry_run=False):
    if dry_run:
        log(f"[dry-run] (mock)本应写alert: {msg}")
        return
    try:
        with open(ALERT_FILE, 'a') as f:
            f.write(f"{now_str()} [DATA_INTEGRITY] {msg}\n")
    except Exception:
        pass


def do_login_probe(dry_run, mock_login):
    """探测login。dry-run模式绝不import baostock/发真实请求(Task#194)。"""
    if dry_run:
        ok = (mock_login == 'ok')
        log(f"[dry-run] mock login结果: {'成功' if ok else '失败'}")
        return ok
    # [Task#265] 真实login前申请统一日预算(batch口径): 预算不足本轮探测
    # 跳过返回False; 预算模块自身异常fail-open放行(不阻断恢复探测链)
    if _bb is not None:
        try:
            if not _bb.acquire(1, 'batch'):
                log("[Task#259] 统一日预算耗尽, 本轮login探测跳过")
                return False
        except Exception as e:
            log(f"[Task#259] 预算模块异常(fail-open放行探测): {e}")
    import baostock as bs
    lg = bs.login()
    if lg.error_code != '0':
        log(f"[未恢复] login error_code={lg.error_code} {lg.error_msg}")
        return False
    bs.logout()
    return True


def launch_backfill_hook(dry_run):
    """[Task#77]钩子: 调起恢复流水线(幂等: 流水线自带flock+状态机)。
    [Task#194] 流水线静默窗口已延至16:00, 交易时段确认时自动paused_market,
    由resume cron(每小时:25)在16:25后续跑, 实现回补推迟到16:00后启动。"""
    if dry_run:
        log("[dry-run] (mock)本应启动回补流水线: baostock_recovery.py --auto "
            "(dry-run不真实调起)")
        return
    os.system('nohup /usr/bin/python3 /home/AIWealth/tools/baostock_recovery.py'
              ' --auto >> /home/AIWealth/logs/realtime/baostock_recovery.log'
              ' 2>&1 &')


def mark_status_md(ts):
    # STATUS标注: 将Task#31区块占位文本替换为恢复时间(仅首次, 幂等)
    # 原子写(mkstemp+rename): probe跑在:10, 与progress_scheduler(*/10)同分钟
    try:
        import tempfile
        with open(STATUS_MD, encoding='utf-8') as f:
            content = f.read()
        if PLACEHOLDER in content:
            content = content.replace(
                PLACEHOLDER, f"已于 {ts} 恢复(冷静期二次确认通过)", 1)
            fd, tmp = tempfile.mkstemp(dir=os.path.dirname(STATUS_MD),
                                       prefix='.status_probe_')
            with os.fdopen(fd, 'w', encoding='utf-8') as f:
                f.write(content)
            os.chmod(tmp, 0o644)
            os.replace(tmp, STATUS_MD)
            log("PROJECT_STATUS.md Task#31区块已标注恢复时间")
    except Exception as exc:
        log(f"STATUS标注失败(不影响主流程): {exc}")


def main():
    ap = argparse.ArgumentParser(description='BaoStock封禁恢复探测(冷静期二次确认)')
    ap.add_argument('--dry-run', action='store_true',
                    help='mock模式: 不真实请求BaoStock/不调起回补(Task#194验证)')
    ap.add_argument('--mock-login', choices=['ok', 'fail'], default='fail',
                    help='dry-run时mock的login结果')
    ap.add_argument('--state-file', default=None,
                    help='测试: 指定状态文件路径(默认生产ban_status)')
    args = ap.parse_args()
    state_path = args.state_file or STATE_FILE

    # 幂等: 已恢复则不再探测
    try:
        st = json.load(open(state_path))
        if st.get('recovered'):
            return 0
    except Exception:
        st = {'banned_at': '2026-07-24 19:56', 'error': '10001011 黑名单用户'}

    ok = do_login_probe(args.dry_run, args.mock_login)
    ts = now_str()
    st['last_probe'] = ts

    if not ok:
        # [Task#194] 冷静期内任何失败 → 清除pending回到banned
        if st.get('pending_confirm'):
            log(f"[冷静期打断] pending期间(since {st.get('pending_since')})"
                f"login失败, 清除pending回到banned")
            append_history(st, {
                'event': 'pending_reverted', 'at': ts,
                'note': (f"冷静期二次确认失败(pending_since="
                         f"{st.get('pending_since')}), 回到banned, "
                         f"未触发回补(Task#194)")})
            st['pending_confirm'] = False
            st['pending_since'] = None
        st['recovered'] = False
        save_state(st, state_path)
        return 1

    # login成功
    if not st.get('pending_confirm'):
        # [Task#194] 首次成功: 只置pending, 绝不立即回补(8/5二次封禁教训)
        st['pending_confirm'] = True
        st['pending_since'] = ts
        st['recovered'] = False
        append_history(st, {
            'event': 'pending_confirm', 'at': ts,
            'note': ('login探测首次成功, 进入冷静期待60min后二次确认, '
                     '本轮不回补(Task#194防再封禁)')})
        save_state(st, state_path)
        log(f"[冷静期] login首次成功, pending_confirm已置位(since {ts}), "
            f"等待下一轮探测二次确认, 本轮不启动回补")
        write_alert("[RECOVERY_PENDING] BaoStock login探测首次成功, "
                    "进入60min冷静期观察, 二次确认后才启动回补(Task#194)",
                    args.dry_run)
        return 0

    # 已在pending: 校验冷静期时长
    try:
        since = datetime.strptime(st['pending_since'], '%Y-%m-%d %H:%M:%S')
        elapsed_min = (datetime.now() - since).total_seconds() / 60
    except (KeyError, TypeError, ValueError):
        elapsed_min = COOLDOWN_MINUTES  # pending_since缺损: 不阻塞确认
    if elapsed_min < COOLDOWN_MINUTES:
        log(f"[冷静期未满] 距首次成功仅{elapsed_min:.0f}min "
            f"(<{COOLDOWN_MINUTES}min), 保持pending不确认")
        save_state(st, state_path)
        return 0

    # [Task#194] 二次确认成功 → recovered + 启动回补(限速版流水线)
    st['recovered'] = True
    st['recovered_at'] = ts
    st['pending_confirm'] = False
    append_history(st, {
        'event': 'recovery_confirmed', 'at': ts,
        'note': (f"冷静期({elapsed_min:.0f}min)后二次login成功, 确认解禁; "
                 f"回补走分批限速流水线(50只/批+批间30s, Task#194)")})
    save_state(st, state_path)
    log(f"[已恢复] 冷静期二次确认通过! BaoStock解禁确认, 启动限速回补流水线")
    launch_backfill_hook(args.dry_run)
    write_alert("[RECOVERY] BaoStock封禁已解除(冷静期二次确认通过)。"
                "回补流水线已调起(分批限速+16:00后窗口, Task#194)。"
                "待办: 重补降级日hour1-4列", args.dry_run)
    if not args.dry_run:
        mark_status_md(ts)
    return 0


if __name__ == '__main__':
    sys.exit(main())
