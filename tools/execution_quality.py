#!/usr/bin/env python3
"""[Task#81] 盘中执行质量监测 —— 决策/守护/影子/快照四路只读观测.

模块(全部只读交易链路文件, 只写自有产物):
  1. 决策→成交链路质量: 9:25决策价 vs 当日实际开盘价滑点、决策耗时、
     行情源命中(腾讯/Sina轮次成功率、未获取清单)
  2. 守护进程质量: 轮询间隔分布(p50/p90/max/超时缺口)、行情命中率、
     触线→动作时延(日志行时间差)
  3. 影子比对汇总: shadow_exit_YYYYMMDD.jsonl 自动归因
     (action一致性/价格差/触发时间差); 无触发日如实记录
  4. 快照归档: data/realtime/intraday_snapshot.json 10秒快照 →
     logs/realtime/snapshots/snapshots_YYYYMMDD.jsonl.gz 可回放数据集
     (archive模式盘中守护, poll_seq去重, 15:06自动收尾退出)
  5. 晚间复盘报告接口: execution_quality_YYYYMMDD.json (schema_version=1,
     含md_section字段) → daily_review_report.py 22:10读取渲染

用法:
  python3 tools/execution_quality.py report [--date 2026-07-30]
  python3 tools/execution_quality.py archive     # 盘中快照归档守护(cron 9:24拉起)

cron:
  24 9  * * 1-5  archive   (早于9:25 daemon启动, 覆盖全盘中)
  10 15 * * 1-5  report    (收盘后, 早于22:10晚间复盘)

只读: morning_decision.log / intraday_monitor.log / decision_*.json /
      positions.json / intraday_snapshot.json / shadow_exit_*.jsonl / stocks.db
只写: execution_quality_*.json / snapshots/*.jsonl.gz。不触碰交易主链。
"""
import argparse
import glob
import gzip
import json
import os
import re
import sqlite3
import sys
import time
from datetime import datetime

BASE = '/home/AIWealth'
LOG_DIR = f'{BASE}/logs/realtime'
SNAP_SRC = f'{BASE}/data/realtime/intraday_snapshot.json'
SNAP_DIR = f'{LOG_DIR}/snapshots'
POSITIONS_FILE = f'{BASE}/data/realtime/positions.json'
MD_LOG = f'{LOG_DIR}/morning_decision.log'
IM_LOG = f'{LOG_DIR}/intraday_monitor.log'
DB = f'{BASE}/data/stocks.db'

DECISION_CRON = '09:25:00'      # 决策cron拉起时刻(耗时基准)
POLL_INTERVAL = 10              # daemon标称轮询间隔(秒)
GAP_THRESHOLD = 15              # 超过视为轮询缺口


def _pct(a, b):
    return round((a - b) / b * 100, 3) if b else None


def _percentile(sorted_vals, q):
    if not sorted_vals:
        return None
    idx = min(int(len(sorted_vals) * q), len(sorted_vals) - 1)
    return sorted_vals[idx]


# ── 模块1: 决策→成交链路 ─────────────────────────────────────
def _day_open(code, date):
    """当日实际开盘价: 优先日K(22:10报告重放时可用), 回退盘中快照candidates。"""
    try:
        conn = sqlite3.connect(f'file:{DB}?mode=ro', uri=True)
        row = conn.execute("SELECT open FROM stock_kline WHERE code=? AND date=?",
                           (code, date)).fetchone()
        conn.close()
        if row and row[0]:
            return float(row[0])
    except Exception:
        pass
    try:
        snap = json.load(open(SNAP_SRC))
        if snap.get('trade_date') == date:
            for c in snap.get('candidates', []):
                if c.get('code') == code and c.get('open'):
                    return float(c['open'])
    except Exception:
        pass
    return None


def decision_chain(date):
    """决策链质量: 滑点/耗时/行情源命中。"""
    out = {'buys': [], 'timing': {}, 'quote_sources': {}}
    # 当日买入: positions.json中buy_date=date的持仓+closed
    try:
        pos = json.load(open(POSITIONS_FILE))
        trades = ([p for p in pos.get('positions', [])
                   if p.get('buy_date') == date]
                  + [t for t in pos.get('closed_trades', [])
                     if t.get('buy_date') == date])
    except Exception:
        trades = []
    for p in trades:
        opn = _day_open(p['code'], date)
        out['buys'].append({
            'code': p['code'], 'name': p.get('name'),
            'strategy': p.get('strategy'), 'slot': p.get('slot_id'),
            'decision_price': p.get('buy_price'),
            'day_open': opn,
            'slippage_pct': _pct(opn, p['buy_price']) if opn else None,
            'note': '决策价=9:25竞价快照, 成交按决策价入账(模拟盘); '
                    'slippage=若9:30开盘实际成交的偏差',
        })
    # 决策耗时: decision json的decided_at - 09:25:00 cron
    dfile = f"{BASE}/data/realtime/decision_{date.replace('-', '')}.json"
    try:
        dec = json.load(open(dfile))
        t0 = datetime.strptime(f'{date} {DECISION_CRON}', '%Y-%m-%d %H:%M:%S')
        t1 = datetime.strptime(dec['decided_at'], '%Y-%m-%d %H:%M:%S')
        out['timing'] = {'cron_at': DECISION_CRON,
                         'decided_at': dec['decided_at'],
                         'duration_sec': (t1 - t0).total_seconds()}
    except Exception as e:
        out['timing'] = {'error': f'decision文件解析失败: {e}'}
    # 行情源命中: 解析morning_decision.log当日段
    try:
        txt = open(MD_LOG, encoding='utf-8').read()
        seg = txt.split(f'实盘模式, 交易日: {date}')[-1]
        nxt = seg.find('实盘模式, 交易日:')
        if nxt > 0:
            seg = seg[:nxt]
        qq = sum(int(m) for m in re.findall(r'腾讯本轮获取 (\d+) 只', seg))
        sina = sum(int(m) for m in re.findall(r'Sina本轮获取 (\d+) 只', seg))
        sina_fail = seg.count('Sina快照接口全部失败')
        got = re.search(r'获取到 (\d+)/(\d+) 只开盘价', seg)
        missed = re.findall(r"5轮后仍有 \d+ 只未获取到开盘价: (\[[^\]]*\])", seg)
        out['quote_sources'] = {
            'tencent_hits': qq, 'sina_hits': sina,
            'sina_all_fail_rounds': sina_fail,
            'final': f"{got.group(1)}/{got.group(2)}" if got else None,
            'missed_after_5_rounds': missed[0] if missed else None,
        }
    except Exception as e:
        out['quote_sources'] = {'error': f'日志解析失败: {e}'}
    return out


# ── 模块2: 守护进程质量 ──────────────────────────────────────
def daemon_quality(date):
    """轮询间隔分布/行情命中/触线→动作时延(取日志最后一个启动段=当日)。"""
    out = {}
    try:
        lines = open(IM_LOG, encoding='utf-8').read().splitlines()
    except Exception as e:
        return {'error': f'日志读取失败: {e}'}
    starts = [i for i, ln in enumerate(lines) if '] 启动 pid=' in ln]
    if not starts:
        return {'error': '未找到daemon启动段'}
    seg = lines[starts[-1]:]
    out['daemon_start'] = seg[0][:10].strip('[]')
    polls, quote_hits, quote_total = [], 0, 0
    trigger_ts, action_latency = None, []
    for ln in seg:
        m = re.match(r'\[(\d\d):(\d\d):(\d\d)\] 轮询#(\d+) 行情(\d+)/(\d+)', ln)
        if m:
            h, mi, s = int(m[1]), int(m[2]), int(m[3])
            polls.append(h * 3600 + mi * 60 + s)
            quote_hits += int(m[5])
            quote_total += int(m[6])
            continue
        tm = re.match(r'\[(\d\d):(\d\d):(\d\d)\].*(触线|🔔.*触发)', ln)
        if tm:
            trigger_ts = int(tm[1]) * 3600 + int(tm[2]) * 60 + int(tm[3])
        am = re.match(r'\[(\d\d):(\d\d):(\d\d)\].*(平仓|已卖出|selling)', ln)
        if am and trigger_ts is not None:
            ts = int(am[1]) * 3600 + int(am[2]) * 60 + int(am[3])
            action_latency.append(ts - trigger_ts)
            trigger_ts = None
    gaps = sorted(b - a for a, b in zip(polls, polls[1:]))
    out['polls'] = {
        'count': len(polls),
        'interval_p50': _percentile(gaps, 0.5),
        'interval_p90': _percentile(gaps, 0.9),
        'interval_max': gaps[-1] if gaps else None,
        'gaps_over_threshold': sum(1 for g in gaps if g > GAP_THRESHOLD),
        'nominal': POLL_INTERVAL,
    }
    out['quote_hit_rate'] = round(quote_hits / quote_total * 100, 2) \
        if quote_total else None
    out['trigger_to_action_sec'] = action_latency or '当日无触线事件'
    return out


# ── 模块3: 影子比对汇总 ──────────────────────────────────────
def shadow_summary(date):
    """shadow_exit jsonl → 一致性统计+差异归因。"""
    f = f"{LOG_DIR}/shadow_exit_{date.replace('-', '')}.jsonl"
    if not os.path.exists(f):
        return {'events': 0,
                'note': '当日无影子事件文件(无持仓触发, T+0禁卖日属正常)'}
    events = [json.loads(ln) for ln in open(f, encoding='utf-8')
              if ln.strip()]
    compares = [e for e in events if e.get('event') == 'compare']
    cores = {e['code']: e for e in events if e.get('event') == 'core_trigger'}
    legacies = {e['code']: e for e in events
                if e.get('event') == 'legacy_trigger'}
    diffs = []
    for c in compares:
        if not c.get('match'):
            core, leg = c.get('core', {}), c.get('legacy', {})
            diffs.append({
                'code': c['code'],
                'core_action': core.get('action'),
                'legacy_action': leg.get('action'),
                'price_diff': (core.get('price') or 0) - (leg.get('price') or 0),
                'attribution': 'bar级(core)与tick级(legacy)判定时点差'
                               if core.get('action') == leg.get('action')
                               else 'action分歧: 需人工复核触发线语义',
            })
    only_core = sorted(set(cores) - set(legacies))
    only_legacy = sorted(set(legacies) - set(cores))
    return {'events': len(events), 'compares': len(compares),
            'matches': sum(1 for c in compares if c.get('match')),
            'mismatches': diffs,
            'core_only_triggers': only_core,
            'legacy_only_triggers': only_legacy}


# ── 模块4: 快照归档守护(archive模式) ─────────────────────────
def archive_loop():
    """盘中守护: 监视快照mtime, poll_seq去重追加gzip jsonl, 15:06收尾退出。

    独立进程, 与intraday_monitor无任何写交集(只读其产物)。
    flock防重复实例(cron+手工双拉起时后者秒退)。
    """
    import fcntl
    os.makedirs(SNAP_DIR, exist_ok=True)
    lockf = open(f'{SNAP_DIR}/.archive.lock', 'w')
    try:
        fcntl.flock(lockf, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        print('[archive] 已有实例在运行, 退出')
        return
    today = datetime.now().strftime('%Y-%m-%d')
    out = f"{SNAP_DIR}/snapshots_{today.replace('-', '')}.jsonl.gz"
    last_mtime, last_seq, n = 0, -1, 0
    print(f"[archive] 启动 pid={os.getpid()} -> {out}")
    while True:
        now = datetime.now()
        if now.strftime('%H:%M') >= '15:06':
            break
        try:
            st = os.stat(SNAP_SRC)
            if st.st_mtime_ns != last_mtime:
                last_mtime = st.st_mtime_ns
                snap = json.load(open(SNAP_SRC))
                seq = snap.get('poll_seq')
                if snap.get('trade_date') == today and seq != last_seq:
                    last_seq = seq
                    with gzip.open(out, 'at', encoding='utf-8') as f:
                        f.write(json.dumps(snap, ensure_ascii=False) + '\n')
                    n += 1
        except (OSError, json.JSONDecodeError):
            pass                      # 写入瞬间读到半文件, 下轮重读
        time.sleep(3)                 # 3秒采样(快照10秒更新, 无遗漏)
    print(f"[archive] {now.strftime('%H:%M:%S')} 收盘退出, 归档{n}条 -> {out}")


# ── 模块5: 报告组装+复盘接口 ─────────────────────────────────
def build_md(date, dc, dq, sh, arc):
    L = [f"## ⚙️ 执行质量 — {date} (Task#81)\n"]
    t = dc.get('timing', {})
    L.append(f"**决策链**: cron {t.get('cron_at', '?')} → 决策落盘 "
             f"{t.get('decided_at', '?')} (耗时{t.get('duration_sec', '?')}s)")
    qs = dc.get('quote_sources', {})
    if 'error' not in qs:
        L.append(f"- 行情源: 腾讯{qs.get('tencent_hits')}只 + "
                 f"Sina{qs.get('sina_hits')}只, 最终{qs.get('final')}, "
                 f"Sina整轮失败{qs.get('sina_all_fail_rounds')}次"
                 + (f", 5轮未获取: {qs['missed_after_5_rounds']}"
                    if qs.get('missed_after_5_rounds') else ""))
    for b in dc.get('buys', []):
        slip = (f"{b['slippage_pct']:+.3f}%" if b['slippage_pct'] is not None
                else 'NA(开盘价未取到)')
        L.append(f"- 滑点[{b['slot']}] {b['name']}: 决策{b['decision_price']} "
                 f"vs 开盘{b['day_open']} → **{slip}**")
    if not dc.get('buys'):
        L.append("- 当日无买入, 无滑点样本")
    p = dq.get('polls', {})
    L.append(f"\n**守护进程**: 轮询{p.get('count')}次, 间隔p50={p.get('interval_p50')}s "
             f"p90={p.get('interval_p90')}s max={p.get('interval_max')}s, "
             f"缺口(>{GAP_THRESHOLD}s)={p.get('gaps_over_threshold')}, "
             f"行情命中率{dq.get('quote_hit_rate')}%")
    ta = dq.get('trigger_to_action_sec')
    L.append(f"- 触线→动作时延: {ta if isinstance(ta, str) else str(ta) + 's'}")
    if sh.get('compares') is not None:
        L.append(f"\n**影子比对**: 事件{sh['events']}条, 比对{sh['compares']}组, "
                 f"一致{sh['matches']}组, 分歧{len(sh['mismatches'])}组")
        for d in sh['mismatches']:
            L.append(f"- ⚠️ {d['code']}: core={d['core_action']} vs "
                     f"legacy={d['legacy_action']} ({d['attribution']})")
        if sh.get('core_only_triggers'):
            L.append(f"- core单边触发: {sh['core_only_triggers']}")
        if sh.get('legacy_only_triggers'):
            L.append(f"- legacy单边触发: {sh['legacy_only_triggers']}")
    else:
        L.append(f"\n**影子比对**: {sh.get('note')}")
    L.append(f"\n**快照归档**: {arc}")
    return '\n'.join(L) + '\n'


def report(date):
    dc = decision_chain(date)
    dq = daemon_quality(date)
    sh = shadow_summary(date)
    arc_f = f"{SNAP_DIR}/snapshots_{date.replace('-', '')}.jsonl.gz"
    if os.path.exists(arc_f):
        n = sum(1 for _ in gzip.open(arc_f, 'rt', encoding='utf-8'))
        arc = f"{n}条10秒快照 → {os.path.relpath(arc_f, BASE)}"
    else:
        arc = '当日无归档(archive守护未运行)'
    md = build_md(date, dc, dq, sh, arc)
    out = {'schema_version': 1, 'date': date,
           'generated_at': datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
           'decision_chain': dc, 'daemon_quality': dq,
           'shadow_summary': sh, 'snapshot_archive': arc,
           'md_section': md}
    dst = f"{LOG_DIR}/execution_quality_{date.replace('-', '')}.json"
    tmp = dst + '.tmp'
    json.dump(out, open(tmp, 'w'), ensure_ascii=False, indent=1)
    os.replace(tmp, dst)
    print(md)
    print(f"[execution_quality] 已生成: {dst}")
    return dst


def main():
    ap = argparse.ArgumentParser(description='盘中执行质量监测(Task#81)')
    ap.add_argument('mode', choices=['report', 'archive'])
    ap.add_argument('--date', default=datetime.now().strftime('%Y-%m-%d'))
    a = ap.parse_args()
    if a.mode == 'archive':
        archive_loop()
    else:
        report(a.date)


if __name__ == '__main__':
    main()
