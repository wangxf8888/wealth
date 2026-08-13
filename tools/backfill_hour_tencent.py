#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""stock_kline hour1~4列回补 —— 腾讯mkline(m60)降级源

背景(2026-07-31, P0-2根因二): BaoStock封禁, 7/27起日K走腾讯降级源
hour列全NULL → S3创科晚封的晚封判定(hour1_close)静默失效, 连续4个
信号日零候选。Task#77的BaoStock恢复流水线遥遥无期, 用户裁决今天就
用腾讯60分钟K解, 不等解禁。

口径(与tools/fetch_daily_kline.py正常路径对齐):
  - bar结束时间映射: 1030→hour1, 1130→hour2, 1400→hour3, 1500→hour4
  - rate = (x/preclose-1)*100, preclose取DB当日行(与日K同源自洽)
  - volume: 腾讯手→×100股(BaoStock同单位); amount无可靠口径留NULL
  - 只UPDATE hour1_close IS NULL的既有行, 不碰正常行/不插新行, 可重复跑
  - sanity: 腾讯当日hour4_close须≈DB日close(容差1.5%), 不合=复权/坏数据, 跳过

范围(最小集, 够S3晚封判定即可, 非全市场):
  每个降级日 = 创科涨停股(S3宇宙: sz.30/sh.688且close>=涨停-0.01)
             + 候选池股(candidates_*.json该信号日各槽候选)

Task#36扩围(2026-08-03, 根因: 候选股只映射signal_date, 买入日trade_date
的hour行从不回补 → 实盘持仓/决策股买入日hour缺失, 回测-实盘对照断链):
  ① 候选池股同时并入 signal_date 与 trade_date 两个降级日
  ② positions.json 持仓股: 并入 buy_date 起的全部降级日(持有窗TP/SL复现)
  ③ decision_*.json 全部涉及股(meet_condition): 并入该 trade_date
  m60一次返回40根bar(≈10个交易日), 请求数=去重code数, 扩围主要增加
  "日期映射"而非新code, 实测增量请求极小(详见Task#36报告)。

限速铁律: >=0.5s/req(Robin降级链路纪律), 连续5次错误熔断。

用法:
  python3 tools/backfill_hour_tencent.py                # 自动扫描降级日
  python3 tools/backfill_hour_tencent.py 2026-07-30     # 只补指定日
  python3 tools/backfill_hour_tencent.py --dry-run      # 只打印目标清单不发请求
"""
import glob
import json
import os
import sqlite3
import sys
import time
import urllib.request
from datetime import datetime

DB = '/home/AIWealth/data/stocks.db'
CAND_DIR = '/home/AIWealth/data/realtime'
POSITIONS_JSON = '/home/AIWealth/data/realtime/positions.json'   # Task#36 ②
ALERT_LOG = '/home/AIWealth/data/realtime/scheduler_alerts.log'
URL = 'https://ifzq.gtimg.cn/appstock/app/kline/mkline?param={sym},m60,,{n}'
DEGRADE_SINCE = '2026-07-25'
TIME_MAP = {'1030': 'hour1', '1130': 'hour2', '1400': 'hour3', '1500': 'hour4'}
RATE_LIMIT = 0.5
MAX_CONSECUTIVE_ERR = 5


def _alert(msg):
    try:
        with open(ALERT_LOG, 'a', encoding='utf-8') as f:
            f.write(f"{datetime.now().strftime('%Y-%m-%d %H:%M:%S')} "
                    f"[DATA_INTEGRITY] {msg}\n")
    except OSError:
        pass


def find_degraded_dates(conn):
    """hour1_close NULL占比>50%的日期(降级日)。"""
    rows = conn.execute(
        "SELECT date, COUNT(*), SUM(hour1_close IS NULL) FROM stock_kline "
        "WHERE date >= ? GROUP BY date ORDER BY date", (DEGRADE_SINCE,))
    return [d for d, n, nul in rows if n and (nul or 0) / n > 0.5]


def build_universe(conn, dates):
    """最小集: {date: set(codes)} = 创科涨停股 + 候选池股 + 持仓/决策股(T36)。"""
    uni = {}
    for d in dates:
        codes = set(r[0] for r in conn.execute(
            "SELECT code FROM stock_kline WHERE date=? "
            "AND (code LIKE 'sz.30%' OR code LIKE 'sh.688%') "
            "AND preclose > 0 AND close >= ROUND(preclose*1.20, 2) - 0.01 "
            "AND hour1_close IS NULL", (d,)))
        uni[d] = codes
    # 候选池: 同时映射signal_date与trade_date(T36根因修复——原先只映射
    # signal_date, 导致买入日行永不回补, 实盘对照断链)
    for fp in glob.glob(os.path.join(CAND_DIR, 'candidates_*.json')):
        try:
            data = json.load(open(fp))
        except (OSError, ValueError):
            continue
        target_days = [d for d in (data.get('signal_date'),
                                   data.get('trade_date')) if d in uni]
        if not target_days:
            continue
        for slot in (data.get('strategies') or {}).values():
            for c in slot.get('candidates') or []:
                if c.get('code'):
                    for d in target_days:
                        uni[d].add(c['code'])
    # decision涉及股(meet_condition全部, T36③): 并入该trade_date
    for fp in glob.glob(os.path.join(CAND_DIR, 'decision_*.json')):
        try:
            data = json.load(open(fp))
        except (OSError, ValueError):
            continue
        td = data.get('trade_date')
        if td not in uni:
            continue
        for slot in (data.get('strategies') or {}).values():
            for c in slot.get('meet_condition') or []:
                if c.get('code'):
                    uni[td].add(c['code'])
    # 当前持仓股(T36②): buy_date起的全部降级日(持有窗TP/SL复现需要)
    try:
        pos = json.load(open(POSITIONS_JSON))
        for p in pos.get('positions') or []:
            code, bd = p.get('code'), p.get('buy_date')
            if not code or not bd:
                continue
            for d in dates:
                if d >= bd:
                    uni[d].add(code)
    except (OSError, ValueError):
        pass
    return uni


def fetch_m60(sym, n_bars=40):
    """取腾讯m60, 返回 {date: {hour: {open,high,low,close,volume}}}。"""
    req = urllib.request.Request(
        URL.format(sym=sym, n=n_bars),
        headers={'User-Agent': 'Mozilla/5.0', 'Referer': 'https://gu.qq.com/'})
    data = json.loads(urllib.request.urlopen(req, timeout=15).read())
    bars = (data.get('data', {}).get(sym, {}) or {}).get('m60') or []
    out = {}
    for b in bars:
        ts = str(b[0])  # YYYYMMDDHHMM
        if len(ts) != 12:
            continue
        hour_key = TIME_MAP.get(ts[8:12])
        if not hour_key:
            continue
        d = f'{ts[0:4]}-{ts[4:6]}-{ts[6:8]}'
        out.setdefault(d, {})[hour_key] = {
            'open': float(b[1]), 'close': float(b[2]),
            'high': float(b[3]), 'low': float(b[4]),
            'volume': int(float(b[5]) * 100),  # 手→股
        }
    return out


def update_row(conn, code, date, hours):
    """UPDATE当日行hour列(仅hour1_close仍NULL的行), 返回是否更新。"""
    row = conn.execute(
        "SELECT preclose, close FROM stock_kline "
        "WHERE date=? AND code=? AND hour1_close IS NULL",
        (date, code)).fetchone()
    if not row or not row[0]:
        return False, 'no_row_or_filled'
    preclose, day_close = row
    # sanity: 腾讯hour4_close ≈ DB日close(防复权口径错位污染)
    h4c = (hours.get('hour4') or {}).get('close')
    if not h4c or not day_close or abs(h4c - day_close) / day_close > 0.015:
        return False, f'sanity_fail(h4c={h4c} vs close={day_close})'
    sets, vals = [], []
    rate = lambda x: round((x / preclose - 1) * 100, 4)
    for h in ('hour1', 'hour2', 'hour3', 'hour4'):
        hd = hours.get(h)
        if not hd:
            return False, f'missing_{h}'
        for f in ('open', 'high', 'low', 'close'):
            sets += [f'{h}_{f}=?', f'{h}_{f}_rate=?']
            vals += [hd[f], rate(hd[f])]
        sets.append(f'{h}_volume=?')
        vals.append(hd['volume'])
    conn.execute(f"UPDATE stock_kline SET {','.join(sets)} "
                 "WHERE date=? AND code=?", vals + [date, code])
    return True, ''


def main():
    args = [a for a in sys.argv[1:] if a != '--dry-run']
    dry_run = '--dry-run' in sys.argv[1:]
    conn = sqlite3.connect(DB)
    dates = sorted(args) or find_degraded_dates(conn)
    if not dates:
        print('无降级日(hour列已齐), 无需回补')
        return 0
    uni = build_universe(conn, dates)
    # T36: 剔除目标日hour已齐/无行的(code,date), 防请求量随降级期累积膨胀
    for d in dates:
        pending = set(r[0] for r in conn.execute(
            "SELECT code FROM stock_kline WHERE date=? "
            "AND hour1_close IS NULL", (d,)))
        uni[d] &= pending
    all_codes = sorted(set().union(*uni.values()))
    print(f'降级日: {dates}')
    print(f'最小集宇宙: {len(all_codes)}只 '
          f'({ {d: len(cs) for d, cs in uni.items()} })')
    if dry_run:
        # T36: 只打印目标(code, 日期)清单, 不发任何请求不写DB
        for code in all_codes:
            ds = [d for d in dates if code in uni[d]]
            print(f'  [dry-run] {code} -> {ds}')
        print(f'[dry-run] 预计请求数={len(all_codes)} '
              f'(限速{RATE_LIMIT}s/req, 约{len(all_codes)*RATE_LIMIT:.0f}s)')
        conn.close()
        return 0

    filled, skipped, consecutive_err = {d: 0 for d in dates}, [], 0
    for i, code in enumerate(all_codes, 1):
        sym = code.replace('sz.', 'sz').replace('sh.', 'sh')
        try:
            m60 = fetch_m60(sym)
            consecutive_err = 0
        except Exception as e:
            consecutive_err += 1
            skipped.append(f'{code}(fetch:{type(e).__name__})')
            if consecutive_err >= MAX_CONSECUTIVE_ERR:
                _alert(f'[HOUR_BACKFILL] 腾讯mkline连续{consecutive_err}次'
                       f'错误, 熔断退出(已补{sum(filled.values())}行)')
                print('!!! 连续错误熔断')
                break
            time.sleep(RATE_LIMIT)
            continue
        for d in dates:
            if code not in uni[d]:
                continue
            if d not in m60:
                skipped.append(f'{code}@{d}(腾讯无bar)')
                continue
            ok, why = update_row(conn, code, d, m60[d])
            if ok:
                filled[d] += 1
            elif why != 'no_row_or_filled':
                skipped.append(f'{code}@{d}({why})')
        conn.commit()
        if i % 10 == 0:
            print(f'  进度 {i}/{len(all_codes)}')
        time.sleep(RATE_LIMIT)

    conn.commit()
    total = sum(filled.values())
    print(f'回补完成: {total}行 明细={filled}')
    if skipped:
        print(f'跳过{len(skipped)}项: {skipped[:20]}')
    _alert(f'[HOUR_BACKFILL] 腾讯m60最小集回补: {total}行 {filled}, '
           f'跳过{len(skipped)}项 (S3晚封判定供给恢复, 全市场仍待Task#77)')
    conn.close()
    return 0


if __name__ == '__main__':
    sys.exit(main())
