#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""baostock_budget.py - BaoStock统一日预算模块 (Task#259)

背景: BaoStock官方规则每日API请求≤5万次+禁止并发连接, 超限进黑名单。
用户裁决(2026-08-10): 按每日4万次预算控制, 放开过度保守限速, 并要有访问统计。
此前4个调用方(fetch_daily/minute_kline、baostock_recovery、t233_runner)各自
为政, 无全局跨进程日请求计数器 — 本模块补齐统一账本。

语义:
- priority='p0'   : 日更链(fetch_daily_kline当日更新/daily_update.sh指数查询),
                    仅受hard_limit=40000拦截
- priority='batch': 回补类(t233/recovery/minute回补), 受soft_limit=39000拦截;
                    且当日日更链尚未完成时, batch可用额=hard-used-P0_RESERVE
                    (为日更链~10550次请求预留, 宁可保守)
- login/探测调用也计1次; 为减少锁开销调用方可按块申请(如acquire(50)本地扣减)

持久化: data/baostock_daily_budget.json, 自然日重置(date字段比对);
跨日重置时前一日终值append到 data/baostock_usage_history.json。
并发安全: fcntl.LOCK_EX短持锁(data/.baostock_budget.lock) + 原子写(tmp+rename)。

CLI(用户要的"访问统计"入口):
  python3 tools/baostock_budget.py --status              # 今日用量/上限/百分比
  python3 tools/baostock_budget.py --acquire 3 --priority p0  # shell内联申请
"""
import argparse
import fcntl
import json
import os
import sys
import tempfile
from datetime import date, datetime

DATA_DIR = '/home/AIWealth/data'
BUDGET_FILE = os.path.join(DATA_DIR, 'baostock_daily_budget.json')
LOCK_FILE = os.path.join(DATA_DIR, '.baostock_budget.lock')
HISTORY_FILE = os.path.join(DATA_DIR, 'baostock_usage_history.json')

SOFT_LIMIT = 39000        # batch类软停线(用户红线40000留1000余量)
HARD_LIMIT = 40000        # p0类硬停线(=用户4万红线; 官方5万线之下)
P0_RESERVE = 11000        # 日更链完成前为其预留额度(~10550次+余量)

# 日更链完成判定: 复用daily_update.sh日志结束标记(与baostock_recovery
# daily_update_window_pending同款口径, 此处独立实现避免循环依赖)
DAILY_LOG_DIR = '/home/AIWealth/logs'
DAILY_DONE_MARK = '每日数据更新结束'


class _FileLock:
    """fcntl.LOCK_EX短持锁上下文(跨进程互斥, 进程退出自动释放)。"""

    def __init__(self, path=LOCK_FILE):
        self.path = path
        self.fh = None

    def __enter__(self):
        self.fh = open(self.path, 'a')
        fcntl.flock(self.fh, fcntl.LOCK_EX)
        return self

    def __exit__(self, *exc):
        # [Task#265] 评审修复(Sven): LOCK_UN失败不向外传播, finally保证close
        # (锁随fd关闭自动释放, 避免长期daemon句柄泄漏)
        try:
            fcntl.flock(self.fh, fcntl.LOCK_UN)
        except Exception as e:
            sys.stderr.write(f"[baostock_budget] 解锁失败(close兜底释放): {e}\n")
        finally:
            self.fh.close()
        return False


def _today():
    return date.today().isoformat()


def _atomic_write(path, obj):
    """原子写: tmp+rename(项目惯例, 与save_progress/State.save同款)。"""
    d = os.path.dirname(path)
    fd, tmp = tempfile.mkstemp(dir=d, prefix='.bb_')
    with os.fdopen(fd, 'w', encoding='utf-8') as f:
        json.dump(obj, f, ensure_ascii=False, indent=2)
    os.chmod(tmp, 0o644)
    os.replace(tmp, path)


def _fresh_state():
    return {'date': _today(), 'used': 0,
            'soft_limit': SOFT_LIMIT, 'hard_limit': HARD_LIMIT}


def _load_state():
    try:
        with open(BUDGET_FILE, encoding='utf-8') as f:
            d = json.load(f)
        if not isinstance(d, dict) or 'date' not in d:
            return _fresh_state()
        d.setdefault('used', 0)
        d.setdefault('soft_limit', SOFT_LIMIT)
        d.setdefault('hard_limit', HARD_LIMIT)
        return d
    except (json.JSONDecodeError, OSError):
        return _fresh_state()


def _append_history(prev):
    """跨日重置时把前一日终值append到usage_history(list结构, 失败不阻断)。"""
    try:
        try:
            with open(HISTORY_FILE, encoding='utf-8') as f:
                hist = json.load(f)
            if not isinstance(hist, list):
                hist = []
        except (json.JSONDecodeError, OSError):
            hist = []
        hist.append({'date': prev.get('date'), 'used': prev.get('used', 0),
                     'soft_limit': prev.get('soft_limit', SOFT_LIMIT),
                     'hard_limit': prev.get('hard_limit', HARD_LIMIT),
                     'closed_at': datetime.now().strftime(
                         '%Y-%m-%d %H:%M:%S')})
        _atomic_write(HISTORY_FILE, hist)
    except OSError:
        pass


def _roll_if_new_day(d):
    """自然日重置: date不等于今日 → 归档前值+重置。返回(可能重置后的)state。"""
    if d.get('date') != _today():
        if d.get('used', 0) > 0:
            _append_history(d)
        d = _fresh_state()
    return d


def _daily_update_done(now=None):
    """当日日更链是否已完成: 周末无日更视为已完成(不预留);
    交易日看logs/daily_update_YYYYMMDD.log是否含结束标记。"""
    now = now or datetime.now()
    if now.weekday() >= 5:
        return True
    log_path = os.path.join(DAILY_LOG_DIR,
                            f"daily_update_{now.strftime('%Y%m%d')}.log")
    try:
        with open(log_path, encoding='utf-8', errors='replace') as f:
            return DAILY_DONE_MARK in f.read()
    except OSError:
        return False   # 日志尚未生成=日更还没跑, 保留预留(宁可保守)


def _limit_for(priority, d):
    """当前优先级的可用上限(绝对值口径, 与used比较)。"""
    if priority == 'p0':
        return d.get('hard_limit', HARD_LIMIT)
    # batch: 软停线; 日更未完成时再叠加P0_RESERVE预留
    limit = d.get('soft_limit', SOFT_LIMIT)
    if not _daily_update_done():
        limit = min(limit, d.get('hard_limit', HARD_LIMIT) - P0_RESERVE)
    return limit


def acquire(n=1, priority='batch'):
    """申请n次请求额度。成功扣减并返回True; 额度不足返回False(不扣减)。"""
    n = max(0, int(n))
    with _FileLock():
        d = _roll_if_new_day(_load_state())
        if d['used'] + n > _limit_for(priority, d):
            _atomic_write(BUDGET_FILE, d)   # 落盘可能发生的跨日重置
            return False
        d['used'] += n
        _atomic_write(BUDGET_FILE, d)
        return True


def used():
    """今日已用请求数(自然日口径)。"""
    with _FileLock():
        d = _roll_if_new_day(_load_state())
        _atomic_write(BUDGET_FILE, d)
        return d['used']


def remaining(priority='batch'):
    """今日该优先级剩余可用额度(不扣减)。"""
    with _FileLock():
        d = _roll_if_new_day(_load_state())
        _atomic_write(BUDGET_FILE, d)
        return max(0, _limit_for(priority, d) - d['used'])


def status():
    """结构化统计(dict), CLI --status与heartbeat板块共用。"""
    with _FileLock():
        d = _roll_if_new_day(_load_state())
        _atomic_write(BUDGET_FILE, d)
    daily_done = _daily_update_done()
    return {
        'date': d['date'],
        'used': d['used'],
        'soft_limit': d['soft_limit'],
        'hard_limit': d['hard_limit'],
        'pct_of_hard': round(d['used'] / d['hard_limit'] * 100, 1),
        'daily_update_done': daily_done,
        'remaining_p0': max(0, d['hard_limit'] - d['used']),
        'remaining_batch': max(0, _limit_for('batch', d) - d['used']),
    }


def main():
    ap = argparse.ArgumentParser(description='BaoStock统一日预算(Task#259)')
    ap.add_argument('--status', action='store_true',
                    help='打印今日用量/上限/百分比(访问统计入口)')
    ap.add_argument('--acquire', type=int, metavar='N',
                    help='申请N次额度(shell内联用, 成功exit 0/不足exit 1)')
    ap.add_argument('--priority', default='batch', choices=['p0', 'batch'],
                    help='申请优先级(默认batch)')
    ap.add_argument('--json', action='store_true', help='status以JSON输出')
    args = ap.parse_args()
    if args.acquire is not None:
        ok = acquire(args.acquire, args.priority)
        print(f"acquire({args.acquire}, {args.priority}) -> "
              f"{'OK' if ok else 'DENIED'} (used={used()})")
        return 0 if ok else 1
    st = status()
    if args.json:
        print(json.dumps(st, ensure_ascii=False, indent=2))
        return 0
    daily_str = ('已完成' if st['daily_update_done']
                 else f'未完成(batch预留{P0_RESERVE})')
    print(f"BaoStock当日API用量 [{st['date']}]: "
          f"{st['used']}/{st['hard_limit']} ({st['pct_of_hard']}%)")
    print(f"  软停线(batch): {st['soft_limit']} | 硬停线(p0): "
          f"{st['hard_limit']} | 日更链: {daily_str}")
    print(f"  剩余额度: p0={st['remaining_p0']} | batch={st['remaining_batch']}")
    return 0


if __name__ == '__main__':
    sys.exit(main())
