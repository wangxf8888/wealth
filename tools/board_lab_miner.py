#!/usr/bin/env python3
"""Task#215 打板实验室(board_lab)挖掘管线 — 阶梯×指标交叉表规律挖掘.

设计纲领(用户原话): "你要找出很多指标结合的规律点" —— 本管线把采集器
(board_lab_collector.py)与历史回测器(board_lab_backtest.py)产出的
"阶梯事件+结局"样本, 汇总成标准交叉表:
  阶梯到达时刻分桶 × 换手增速分桶 × 量比分桶
    -> 封板率 / 炸板率 / 次日开盘溢价均值 / 样本数
并固化用户核心假设为标准报表之一:
  "各阶梯低换手爬升(换手增速<同阶梯中位) vs 高换手爬升 的封板率与次日溢价对比"

输入(live模式): data/board_process/events_*.jsonl + outcomes_*.json (近N日)
输出: research/results/board_lab/BOARD_LAB_REPORT_YYYYMMDD.md
统计效力: 累计>=10交易日方出正式规律报告; 不足时报告头部注明
  "样本不足, 无统计效力, 仅验证管线连通"。

本模块同时作为库被 board_lab_backtest.py import复用(分桶/交叉表/渲染),
两条数据链(实时qt采集/历史5min回放)共用同一套分析口径。

运行: python3 tools/board_lab_miner.py [--days N] [--date YYYYMMDD]
红线: 只读data/board_process与stocks.db, 只写research/results/board_lab/;
     零网络请求, 零BaoStock。
"""
import argparse
import glob
import json
import os
from datetime import datetime

BASE = '/home/AIWealth'
PROC_DIR = os.path.join(BASE, 'data', 'board_process')
OUT_DIR = os.path.join(BASE, 'research', 'results', 'board_lab')

LADDER_ORDER = ['3', '4', '5', '6', '7', '8', '9', 'limit']
MIN_DAYS_FORMAL = 10       # 正式规律报告最低交易日数


# ---------------- 分桶(两条数据链共用口径) ----------------
def bucket_time(hms):
    """阶梯到达时刻分桶。hms='HH:MM:SS'或'HHMM'。"""
    t = hms.replace(':', '')[:4]
    if t < '1000':
        return 'T1_0930-1000'
    if t < '1100':
        return 'T2_1000-1100'
    if t <= '1130':
        return 'T3_1100-1130'
    if t < '1400':
        return 'T4_1300-1400'
    return 'T5_1400-1500'


def bucket_turn_speed(v, med):
    """换手增速分桶: 同阶梯中位数二分(核心假设口径)。"""
    if v is None or med is None:
        return None
    return 'LOW_turn(<med)' if v < med else 'HIGH_turn(>=med)'


def bucket_vol_ratio(v):
    if v is None:
        return None
    if v < 1:
        return 'VR<1'
    if v < 2:
        return 'VR1-2'
    if v < 4:
        return 'VR2-4'
    return 'VR>=4'


def median(xs):
    xs = sorted(xs)
    n = len(xs)
    if not n:
        return None
    return xs[n // 2] if n % 2 else (xs[n // 2 - 1] + xs[n // 2]) / 2.0


# ---------------- 样本结构 ----------------
# 每个样本 = 一只股一日到达某阶梯的事件:
# {date, code, ladder, hit_ts, turn_speed, vol_ratio, turnover,
#  sealed_final(bool), broken(bool触板未封收或炸过板), touched,
#  d1_open_premium_pct, d1_close_pct}


def crosstab(samples, keys):
    """按keys(样本字段名或callable标签函数列表)分组聚合。"""
    groups = {}
    for s in samples:
        kt = tuple(k(s) if callable(k) else s.get(k) for k in keys)
        if any(v is None for v in kt):
            continue
        groups.setdefault(kt, []).append(s)
    rows = []
    for kt, ss in sorted(groups.items(), key=lambda x: str(x[0])):
        n = len(ss)
        n_seal = sum(1 for s in ss if s['sealed_final'])
        n_touch = sum(1 for s in ss if s['touched'])
        n_break = sum(1 for s in ss if s['broken'])
        prem = [s['d1_open_premium_pct'] for s in ss
                if s.get('d1_open_premium_pct') is not None]
        d1c = [s['d1_close_pct'] for s in ss
               if s.get('d1_close_pct') is not None]
        rows.append({
            'key': kt, 'n': n,
            'seal_rate': round(n_seal / n * 100, 1),
            'touch_rate': round(n_touch / n * 100, 1),
            'break_rate': round(n_break / n_touch * 100, 1)
            if n_touch else None,
            'd1_open_prem_avg': round(sum(prem) / len(prem), 2)
            if prem else None,
            'd1_close_avg': round(sum(d1c) / len(d1c), 2) if d1c else None,
            'n_outcome': len(prem),
        })
    return rows


def render_table(rows, key_names):
    head = ('| ' + ' | '.join(key_names)
            + ' | n | 封板率% | 触板率% | 炸板率%(触板中) | D1开盘溢价% '
              '| D1收盘% | n结局 |')
    sep = '|' + '---|' * (len(key_names) + 7)
    lines = [head, sep]
    for r in rows:
        lines.append(
            '| ' + ' | '.join(str(k) for k in r['key'])
            + f" | {r['n']} | {r['seal_rate']} | {r['touch_rate']} "
              f"| {r['break_rate'] if r['break_rate'] is not None else '-'} "
              f"| {r['d1_open_prem_avg'] if r['d1_open_prem_avg'] is not None else '-'} "
              f"| {r['d1_close_avg'] if r['d1_close_avg'] is not None else '-'} "
              f"| {r['n_outcome']} |")
    return '\n'.join(lines)


def core_hypothesis_table(samples):
    """用户核心假设标准报表: 各阶梯 低换手爬升 vs 高换手爬升."""
    out = []
    for lv in LADDER_ORDER:
        lv_s = [s for s in samples if s['ladder'] == lv
                and s.get('turn_speed') is not None]
        if len(lv_s) < 4:
            continue
        med = median([s['turn_speed'] for s in lv_s])
        for s in lv_s:
            s['_ts_bucket'] = bucket_turn_speed(s['turn_speed'], med)
        rows = crosstab(lv_s, [lambda s: f'+{s["ladder"]}'
                               if s['ladder'] != 'limit' else '涨停',
                               '_ts_bucket'])
        out.extend(rows)
    return out


def build_report(samples, n_days, dates, source_desc, out_path,
                 extra_sections=''):
    formal = n_days >= MIN_DAYS_FORMAL
    ts_all = [s['turn_speed'] for s in samples
              if s.get('turn_speed') is not None]
    lines = [
        f'# 打板实验室规律报告 {"(正式)" if formal else "(样例/无统计效力)"}',
        '',
        f'- 生成时间: {datetime.now().strftime("%Y-%m-%d %H:%M:%S")}',
        f'- 数据源: {source_desc}',
        f'- 覆盖交易日: {n_days}日 ({dates[0]} ~ {dates[-1]})' if dates
        else '- 覆盖交易日: 0',
        f'- 阶梯事件样本: {len(samples)}个 (换手增速可用{len(ts_all)}个)',
        '',
    ]
    if not formal:
        lines += ['> **警示: 累计交易日<10, 本表无统计效力, '
                  '仅验证管线连通与口径正确。**', '']
    # 报表1: 核心假设
    lines += ['## 报表1(用户核心假设): 各阶梯 低换手爬升 vs 高换手爬升',
              '',
              '> 假设: "有的股换手率比较低就能带来几个点的增幅, '
              '是不是这种比较强势?" —— 换手增速取同阶梯中位数二分。',
              '']
    rows = core_hypothesis_table(samples)
    lines.append(render_table(rows, ['阶梯', '换手爬升']))
    # 结论段: 低vs高汇总
    lo = [r for r in rows if 'LOW' in str(r['key'][1])]
    hi = [r for r in rows if 'HIGH' in str(r['key'][1])]

    def _w(rs, f):
        num = sum((r[f] or 0) * r['n'] for r in rs if r[f] is not None)
        den = sum(r['n'] for r in rs if r[f] is not None)
        return round(num / den, 2) if den else None
    if lo and hi:
        lines += ['',
                  f'**汇总(全部阶梯加权)**: 低换手爬升 n={sum(r["n"] for r in lo)} '
                  f'封板率{_w(lo, "seal_rate")}% D1开盘溢价{_w(lo, "d1_open_prem_avg")}% '
                  f'|| 高换手爬升 n={sum(r["n"] for r in hi)} '
                  f'封板率{_w(hi, "seal_rate")}% D1开盘溢价{_w(hi, "d1_open_prem_avg")}%',
                  '']
    # 报表2: 阶梯×时刻
    lines += ['## 报表2: 阶梯 × 到达时刻分桶', '']
    lines.append(render_table(
        crosstab(samples, ['ladder', lambda s: bucket_time(s['hit_ts'])]),
        ['阶梯', '到达时刻']))
    # 报表3: 阶梯×量比
    lines += ['', '## 报表3: 阶梯 × 量比分桶', '']
    lines.append(render_table(
        crosstab(samples, ['ladder',
                           lambda s: bucket_vol_ratio(s.get('vol_ratio'))]),
        ['阶梯', '量比']))
    # 报表4: 三维(时刻×换手增速×量比, 阶梯>=5聚合)
    hi_s = [s for s in samples if s['ladder'] in ('5', '6', '7', '8', '9',
                                                  'limit')
            and s.get('turn_speed') is not None]
    med_all = median([s['turn_speed'] for s in hi_s])
    for s in hi_s:
        s['_ts_bucket'] = bucket_turn_speed(s['turn_speed'], med_all)
    lines += ['', '## 报表4: 三维交叉(阶梯>=+5聚合): 时刻 × 换手增速 × 量比',
              '']
    lines.append(render_table(
        crosstab(hi_s, [lambda s: bucket_time(s['hit_ts']), '_ts_bucket',
                        lambda s: bucket_vol_ratio(s.get('vol_ratio'))]),
        ['到达时刻', '换手爬升', '量比']))
    if extra_sections:
        lines += ['', extra_sections]
    lines += ['', '---',
              '- 口径: 封板率=终态SEALED占比; 炸板率=触板样本中终态非SEALED'
              '或曾开板占比; D1溢价基准=当日收盘(次日preclose)。',
              '- 管线: tools/board_lab_collector.py(实时qt) / '
              'board_lab_backtest.py(历史5min) -> board_lab_miner.py(本表)。']
    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    with open(out_path, 'w', encoding='utf-8') as f:
        f.write('\n'.join(lines) + '\n')
    return out_path


# ---------------- live产物 -> 样本 ----------------
def load_live_samples(days):
    ev_files = sorted(glob.glob(os.path.join(PROC_DIR, 'events_*.jsonl')))
    ev_files = ev_files[-days:]
    samples, dates = [], []
    for fp in ev_files:
        ymd = os.path.basename(fp)[7:15]
        d = f'{ymd[:4]}-{ymd[4:6]}-{ymd[6:]}'
        dates.append(d)
        oc_path = os.path.join(PROC_DIR, f'outcomes_{ymd}.json')
        outcomes = {}
        if os.path.exists(oc_path):
            outcomes = json.load(open(oc_path)).get('outcomes', {})
        finals, ladder_ev = {}, []
        for line in open(fp, encoding='utf-8'):
            try:
                e = json.loads(line)
            except ValueError:
                continue
            if e['type'] == 'final':
                finals[e['code']] = e
            elif e['type'].startswith('ladder_'):
                ladder_ev.append(e)
        for e in ladder_ev:
            code = e['code']
            fin = finals.get(code, {})
            oc = outcomes.get(code, {})
            snap = e.get('snapshot') or {}
            fstate = fin.get('final_state') or ''
            samples.append({
                'date': d, 'code': code,
                'ladder': str(e['ladder']),
                'hit_ts': e['ts'],
                'turn_speed': snap.get('turn_speed'),
                'vol_ratio': snap.get('vol_ratio'),
                'turnover': snap.get('turnover_pct'),
                'sealed_final': fstate == 'SEALED',
                'touched': bool(fin.get('touch_ts')),
                'broken': (bool(fin.get('touch_ts'))
                           and fstate != 'SEALED')
                or (fin.get('open_cnt') or 0) > 0,
                'd1_open_premium_pct': oc.get('d1_open_premium_pct'),
                'd1_close_pct': oc.get('d1_close_pct'),
            })
    return samples, dates


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--days', type=int, default=30)
    ap.add_argument('--date', default=datetime.now().strftime('%Y%m%d'),
                    help='报告文件名日期后缀')
    args = ap.parse_args()
    samples, dates = load_live_samples(args.days)
    if not samples:
        print('[miner] 无可用样本(events_*.jsonl为空或不存在)')
        return
    out = os.path.join(OUT_DIR, f'BOARD_LAB_REPORT_{args.date}.md')
    build_report(samples, len(dates), dates,
                 f'实时qt采集 data/board_process/events_*.jsonl ({len(dates)}日)',
                 out)
    print(f'[miner] 样本{len(samples)}个/{len(dates)}日 => {out}')


if __name__ == '__main__':
    main()
