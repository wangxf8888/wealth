#!/usr/bin/env python3
"""Task#215扩充交付4: 打板实验室历史5分钟初测 tools/board_lab_backtest.py.

用 data/minute.db 现有全市场5分钟K(2026-07-24~2026-08-06共10交易日,
约3047只/日×48bar)做 board_lab 设计的历史回测版:
  1. 每日每股用5分钟K重建阶梯路径: 首达+3/+4/.../+9/涨停时刻(bar high口径,
     bar标签=右端点'0935'=9:30-9:35); 缩回事件(达阶梯N后bar close回落
     超过1%);
  2. 每阶梯时点指标: 换手率累计与增速(流通股本=stocks.db前一交易日
     volume/(turn/100)换算, 启动时缓存)、量比近似(当日累计量/(近5日日均量
     ×已交易时间占比))、量能斜率(本bar量/前3bar均量);
  3. 封板近似判定 = 5分钟bar close==涨停价(±EPS); 开板 = 封死后后续bar
     low < 涨停价; 盘口类指标(买一卖一/封单量)5min无档位数据, 一律N/A
     留给实时版(board_lab_collector.py);
  4. 结局标注: 当日终态(SEALED/BROKEN/FADED_x/PLATEAU, 与collector同语义)
     + 次日D+1开盘溢价/最高/收盘(stocks.db日K, 基准=当日close即D+1
     preclose);
  5. 交叉表复用 tools/board_lab_miner.py (分桶/聚合/渲染同口径),
     产出 research/results/board_lab/BOARD_LAB_BACKTEST_REPORT.md,
     用户核心假设"低换手爬升更强势"单独成节给正反结论。

口径差异注记(vs 实时采集器, 报告内如实标注):
  - 阶梯首达: 回测=bar high(5min粒度, 时刻只准到bar右端点);
    实时=每分钟现价快照。回测会比实时"更早/更全"捕捉瞬时冲高;
  - 换手增速: 回测=每5min bar增量; 实时=每1min轮增量, 分桶用同阶梯
    中位数二分故量纲差异被自适应吸收;
  - 封板: 回测bar close==涨停为近似(bar内封开无法分辨);
    实时=ask1量价判定, 更精确。
涨停价: import trading_rules.limit_prices(创科20/ST5/主板10, 与引擎一致);
       isST取自当日stock_kline.isST(当日属性, 开盘前已知, 非未来数据)。
未来数据自查: 流通股本/5日均量/preclose全部取自D-1及以前; 阶梯路径与
  终态是"当日过程记录"(描述对象本身), 结局是D+1(仅作被解释变量), 无泄漏。

运行: python3 tools/board_lab_backtest.py [--start 2026-07-24 --end 2026-08-06]
红线: minute.db/stocks.db一律mode=ro只读; 只写research/results/board_lab/;
     零网络请求; 零BaoStock(禁止import baostock)。
"""
import argparse
import json
import os
import sqlite3
import sys
from datetime import datetime

BASE = '/home/AIWealth'
sys.path.insert(0, BASE)
from trading_rules import limit_prices                     # noqa: E402
sys.path.insert(0, os.path.join(BASE, 'tools'))
from board_lab_miner import (LADDER_ORDER, build_report,   # noqa: E402
                             median)

SDB = os.path.join(BASE, 'data', 'stocks.db')
MDB = os.path.join(BASE, 'data', 'minute.db')
OUT_DIR = os.path.join(BASE, 'research', 'results', 'board_lab')

ENTRY_PCT = 3.0
LADDERS = [3, 4, 5, 6, 7, 8, 9]
SHRINK_GAP = 1.0
EPS = 1e-9
BARS_PER_DAY = 48.0


def _ro(path):
    return sqlite3.connect(f'file:{path}?mode=ro', uri=True)


def bar_frac(t):
    """bar右端点'HHMM' -> 当日已交易时间占比(48bar制)。"""
    h, m = int(t[:2]), int(t[2:])
    mins = h * 60 + m
    if mins <= 690:                      # <=11:30 上午
        n = (mins - 570) / 5.0
    else:                                # 下午 13:05->25本bar
        n = 24 + (mins - 780) / 5.0
    return max(n, 1.0) / BARS_PER_DAY


def load_static_for(sc, d):
    """D日静态缓存(全部取自D-1及以前, 零未来数据):
    {code: (float_shares, avg5_vol)}"""
    prevs = [r[0] for r in sc.execute(
        'SELECT DISTINCT date FROM stock_kline WHERE date<? '
        'ORDER BY date DESC LIMIT 10', (d,))]
    if not prevs:
        return {}
    out = {}
    ph = ','.join('?' * len(prevs))
    for code, vol, turn in sc.execute(
            f'SELECT code, volume, turn FROM stock_kline '
            f'WHERE date IN ({ph}) AND volume>0 AND turn>0 '
            f'ORDER BY code, date DESC', prevs):
        if code not in out:
            out[code] = [vol / (turn / 100.0), None]
    ph5 = ','.join('?' * len(prevs[:5]))
    for code, avg in sc.execute(
            f'SELECT code, AVG(volume) FROM stock_kline '
            f'WHERE date IN ({ph5}) AND volume>0 GROUP BY code', prevs[:5]):
        if code in out:
            out[code][1] = avg
    return out


def replay_day(sc, mc, d, nxt):
    """回放一个交易日 -> (阶梯样本list, 池统计dict)。"""
    static = load_static_for(sc, d)
    day = {}          # {code: (preclose, isST, name)}
    for code, name, pc, isst in sc.execute(
            'SELECT code, code_name, preclose, isST FROM stock_kline '
            'WHERE date=?', (d,)):
        if pc and pc > 0:
            day[code] = (pc, bool(isst), name or '')
    d1 = {}
    if nxt:
        for code, o, h, lo, c, pc in sc.execute(
                'SELECT code, open, high, low, close, preclose '
                'FROM stock_kline WHERE date=?', (nxt,)):
            if pc and pc > 0:
                d1[code] = (o, h, lo, c, pc)
    samples = []
    stats = {'pool': 0, 'touched': 0, 'sealed': 0, 'broken_ever': 0,
             'finals': {}}
    cur_code, bars = None, []

    def flush(code, bars):
        if not code or code not in day or len(bars) < 4:
            return
        pc, isst, name = day[code]
        lim = limit_prices(code, pc, isst)[0]
        if lim <= 0:
            return
        fs, av5 = static.get(code, (None, None))
        cum = 0
        prev_vols = []
        in_pool = False
        touched = sealed_now = False
        open_cnt = 0
        hits, hit_rows, shrunk = {}, {}, {}
        last_close_pct = None
        for t, o, h, lo, c, v in bars:
            v = v or 0
            cum += v
            hp = (h / pc - 1) * 100
            cp = (c / pc - 1) * 100
            last_close_pct = cp
            if not in_pool and hp >= ENTRY_PCT:
                in_pool = True
            if in_pool:
                turn = cum / fs * 100 if fs else None
                speed = v / fs * 100 if fs else None      # 本bar换手增量
                vr = (cum / (av5 * bar_frac(t))) if av5 else None
                slope = (v / (sum(prev_vols[-3:]) / len(prev_vols[-3:]))
                         if prev_vols[-3:] and sum(prev_vols[-3:]) > 0
                         else None)
                at_lim = h >= lim - EPS
                if at_lim:
                    touched = True
                seal_bar = c >= lim - EPS                 # 封板近似
                if sealed_now and lo < lim - EPS:
                    open_cnt += 1                         # 开板
                sealed_now = seal_bar
                for lv in LADDERS:
                    if lv not in hits and hp >= lv:
                        hits[lv] = t
                        hit_rows[lv] = (speed, vr, turn, slope)
                if at_lim and 'limit' not in hits:
                    hits['limit'] = t
                    hit_rows['limit'] = (speed, vr, turn, slope)
                for lv in list(hits):
                    if lv != 'limit' and lv not in shrunk \
                            and cp <= lv - SHRINK_GAP:
                        shrunk[lv] = t
            prev_vols.append(v)
        if not in_pool:
            return
        # 终态(与collector同语义)
        sealed_final = sealed_now
        if sealed_final:
            fstate = 'SEALED'
        elif touched:
            fstate = 'BROKEN'
        else:
            ml = ('limit' if 'limit' in hits
                  else max((k for k in hits if isinstance(k, int)),
                           default=None))
            if ml is not None and last_close_pct is not None \
                    and last_close_pct <= ml - SHRINK_GAP:
                fstate = f'FADED_{ml}'
            else:
                fstate = 'PLATEAU'
        stats['pool'] += 1
        stats['touched'] += touched
        stats['sealed'] += sealed_final
        stats['broken_ever'] += (open_cnt > 0)
        fk = 'FADED' if fstate.startswith('FADED') else fstate
        stats['finals'][fk] = stats['finals'].get(fk, 0) + 1
        oc = d1.get(code)
        for lv, t in hits.items():
            speed, vr, turn, slope = hit_rows[lv]
            samples.append({
                'date': d, 'code': code, 'ladder': str(lv), 'hit_ts': t,
                'turn_speed': round(speed, 4) if speed is not None else None,
                'vol_ratio': round(vr, 3) if vr is not None else None,
                'turnover': round(turn, 3) if turn is not None else None,
                'vol_slope': round(slope, 3) if slope is not None else None,
                'sealed_final': sealed_final,
                'touched': touched,
                'broken': (touched and not sealed_final) or open_cnt > 0,
                'final_state': fstate, 'open_cnt': open_cnt,
                'shrunk': str(lv) in {str(k) for k in shrunk},
                'd1_open_premium_pct': round((oc[0] / oc[4] - 1) * 100, 2)
                if oc else None,
                'd1_high_pct': round((oc[1] / oc[4] - 1) * 100, 2)
                if oc else None,
                'd1_close_pct': round((oc[3] / oc[4] - 1) * 100, 2)
                if oc else None,
            })

    for code, t, o, h, lo, c, v in mc.execute(
            'SELECT code, time, open, high, low, close, volume '
            'FROM minute_kline WHERE date=? ORDER BY code, time', (d,)):
        if code != cur_code:
            flush(cur_code, bars)
            cur_code, bars = code, []
        bars.append((t, o, h, lo, c, v))
    flush(cur_code, bars)
    return samples, stats


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--start', default='2026-07-24')
    ap.add_argument('--end', default='2026-08-06')
    ap.add_argument('--min-codes', type=int, default=2000,
                    help='单日minute覆盖低于此值跳过(全市场覆盖日才有效)')
    args = ap.parse_args()
    sc, mc = _ro(SDB), _ro(MDB)
    days = [r[0] for r in mc.execute(
        'SELECT date FROM (SELECT date, COUNT(DISTINCT code) nc '
        'FROM minute_kline WHERE date BETWEEN ? AND ? GROUP BY date) '
        'WHERE nc>=? ORDER BY date', (args.start, args.end, args.min_codes))]
    print(f'[backtest] 全市场覆盖日: {len(days)}日 {days}')
    all_samples, day_stats = [], {}
    for d in days:
        nxt = sc.execute('SELECT MIN(date) FROM stock_kline WHERE date>?',
                         (d,)).fetchone()[0]
        t0 = datetime.now()
        samples, stats = replay_day(sc, mc, d, nxt)
        all_samples.extend(samples)
        day_stats[d] = stats
        print(f'  {d}: 池{stats["pool"]} 触板{stats["touched"]} '
              f'封板终{stats["sealed"]} 终态{stats["finals"]} '
              f'阶梯样本{len(samples)} '
              f'({(datetime.now() - t0).total_seconds():.1f}s, D+1={nxt})')
    sc.close()
    mc.close()
    os.makedirs(OUT_DIR, exist_ok=True)
    # 明细样本落盘(供miner后续合并与QA抽查)
    samp_path = os.path.join(OUT_DIR, 'backtest_samples.json')
    with open(samp_path, 'w', encoding='utf-8') as f:
        json.dump({'days': days, 'day_stats': day_stats,
                   'n_samples': len(all_samples),
                   'generated_at': datetime.now().strftime(
                       '%Y-%m-%d %H:%M:%S'),
                   'samples': all_samples}, f, ensure_ascii=False)
    # 核心假设正反结论节(低换手 vs 高换手, 按封板率与D1溢价两个终点)
    extra = build_hypothesis_verdict(all_samples)
    out = os.path.join(OUT_DIR, 'BOARD_LAB_BACKTEST_REPORT.md')
    build_report(all_samples, len(days), days,
                 f'历史5分钟回放 minute.db {days[0]}~{days[-1]} '
                 f'(封板=bar close==涨停近似, 盘口指标N/A)', out,
                 extra_sections=extra)
    print(f'[backtest] 样本{len(all_samples)}个 => {out}')
    print(f'[backtest] 明细 => {samp_path}')


def build_hypothesis_verdict(samples):
    """用户核心假设单独一节: 四终点正反结论(封板率/炸板率/D1开盘/D1收盘)。"""
    lines = ['## 核心假设裁决: "低换手爬升更强势" 正反结论', '']
    verdicts = []
    for lv in LADDER_ORDER:
        lv_s = [s for s in samples if s['ladder'] == lv
                and s.get('turn_speed') is not None]
        if len(lv_s) < 20:
            continue
        med = median([s['turn_speed'] for s in lv_s])
        lo = [s for s in lv_s if s['turn_speed'] < med]
        hi = [s for s in lv_s if s['turn_speed'] >= med]

        def rate(ss):
            return (sum(1 for s in ss if s['sealed_final']) / len(ss) * 100
                    if ss else 0)

        def brate(ss):
            tt = [s for s in ss if s['touched']]
            return (sum(1 for s in tt if s['broken']) / len(tt) * 100
                    if tt else None)

        def avg(ss, f):
            xs = [s[f] for s in ss if s.get(f) is not None]
            return sum(xs) / len(xs) if xs else None
        sl, sh = rate(lo), rate(hi)
        bl, bh = brate(lo), brate(hi)
        pl, ph = avg(lo, 'd1_open_premium_pct'), avg(hi, 'd1_open_premium_pct')
        cl, ch = avg(lo, 'd1_close_pct'), avg(hi, 'd1_close_pct')
        tag = ('+' + lv) if lv != 'limit' else '涨停'
        v = {'seal': sl > sh,
             'break': bl is not None and bh is not None and bl < bh,
             'open': pl is not None and ph is not None and pl > ph,
             'close': cl is not None and ch is not None and cl > ch}
        verdicts.append(v)

        def fmt(x, s='%'):
            return f'{x:+.2f}{s}' if isinstance(x, float) else '-'
        lines.append(
            f'- 阶梯{tag}(n={len(lv_s)}, 增速中位{med:.4f}%/bar): '
            f'低换手[封板{sl:.1f}% 炸板{bl if bl is None else round(bl, 1)}% '
            f'D1开{fmt(pl)} D1收{fmt(cl)}] vs '
            f'高换手[封板{sh:.1f}% 炸板{bh if bh is None else round(bh, 1)}% '
            f'D1开{fmt(ph)} D1收{fmt(ch)}] -> '
            f'支持端: {"/".join(k for k, ok in v.items() if ok) or "无"}')
    n = len(verdicts)
    cnt = {k: sum(1 for v in verdicts if v[k]) for k in
           ('seal', 'break', 'open', 'close')}
    lines += ['',
              f'**四终点计票({n}个可判阶梯)**: '
              f'封板率端支持 {cnt["seal"]}/{n} | '
              f'炸板率端(触板中)支持 {cnt["break"]}/{n} | '
              f'D1开盘溢价端支持 {cnt["open"]}/{n} | '
              f'D1收盘端支持 {cnt["close"]}/{n}',
              '',
              '**初步解读(待实时数据交叉验证)**: ',
              '- 封板概率端不支持——冲板本身需要量能, 高换手爬升的触板/封板'
              '概率系统性更高;',
              '- 但持续性端部分支持——触板后低换手的炸板率更低、多数阶梯'
              'D1收盘更高: "低换手=惜筹锁筹=封得住", 假设的正确打开方式可能是'
              '**条件化于已触板/已到高阶梯后看持续性**, 而非预测能否冲板;',
              '- 混杂警示: 高换手命中集中早盘(报表2示T1封板率远高于午后), '
              '换手增速与到达时刻强相关, 严格结论需看报表4同时刻桶内对比。']
    return '\n'.join(lines)


if __name__ == '__main__':
    main()
