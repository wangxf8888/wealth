#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""index_kline 指数缺行回补 —— 腾讯fqkline降级源

背景(2026-07-29事件): BaoStock封禁期间 daily_update.sh 的指数更新块
(只走BaoStock且不检查login错误码)静默写0行, 降级源只补了stock_kline,
导致 index_kline 缺 7/27、7/28 → 9:25 morning_decision 大盘过滤断数据。
本工具是根治方案的一半: 被 daily_update.sh 在指数块后检测到缺行时调用。
[Task#295] 泛化为多指数: sz.399001(深证成指)恢复维护(推翻Task#10废弃裁定),
与 sh.000001 同链路检测+回补; 新增 --all 全量扫描模式(回补长缺口用)。

行为:
  1. 找缺行日: stock_kline有数据(>=1000行,排除半途中断日)但
     index_kline 目标指数无行的日期(默认扫最近10个此类日期; --all不设限)
  2. 每指数单次HTTP取腾讯fqkline日K区间(限速礼貌: 一次请求覆盖全部缺口)
  3. preclose链: 用DB前一交易日close起链, 按日期升序逐日补
  4. 口径: 腾讯字段序[date,open,close,high,low,volume(手)], volume×100入库;
     amount/hour1~4无源留NULL; red_ratio留NULL(由update_red_ratio.py补齐,
     仅sh.000001)

用法:
  python3 tools/backfill_index_kline_tencent.py          # 自动检测并回补缺行
  python3 tools/backfill_index_kline_tencent.py --all    # 全量扫描(不限10日)
  python3 tools/backfill_index_kline_tencent.py 2026-07-28  # 只补指定日期
退出码: 0=无缺口或全部补齐; 1=仍有缺口未补(供调用方告警)
"""
import json
import sqlite3
import sys
import urllib.request

DB = '/home/AIWealth/data/stocks.db'
# (库内code, 腾讯symbol, 默认名称)
CODES = [('sh.000001', 'sh000001', '上证综指'),
         ('sz.399001', 'sz399001', '深证成指')]
URL = ('https://web.ifzq.gtimg.cn/appstock/app/fqkline/get'
       '?param={sym},day,{start},{end},{count},qfq')


def find_missing_dates(conn, code, scan_all=False):
    """stock_kline有当日数据但index_kline缺行的日期(升序)。"""
    limit = '' if scan_all else ' LIMIT 10'
    rows = conn.execute(
        "SELECT s.date FROM (SELECT date, COUNT(*) n FROM stock_kline "
        "GROUP BY date HAVING n >= 1000) s "
        "LEFT JOIN index_kline i ON i.date = s.date AND i.code = ? "
        "WHERE i.date IS NULL ORDER BY s.date DESC" + limit, (code,))
    return sorted(r[0] for r in rows)


def fetch_tencent(sym, start, end, count):
    """取fqkline日K, 返回 {date: (open, close, high, low, volume_lots)}。"""
    req = urllib.request.Request(
        URL.format(sym=sym, start=start, end=end, count=count),
        headers={'User-Agent': 'Mozilla/5.0', 'Referer': 'https://gu.qq.com/'})
    data = json.loads(urllib.request.urlopen(req, timeout=15).read())
    node = data['data'][sym]
    klines = node.get('qfqday') or node.get('day') or []
    return {k[0]: tuple(float(x) for x in k[1:6]) for k in klines}


def backfill_code(conn, code, sym, default_name, cli_dates, scan_all):
    """单指数缺行回补, 返回 (filled列表, failed列表)。"""
    if cli_dates:
        missing = sorted(cli_dates)
    else:
        missing = find_missing_dates(conn, code, scan_all)
    if not missing:
        print(f'{code} 无缺行, 无需回补')
        return [], []

    print(f'{code} 待回补{len(missing)}日: {missing[0]} ~ {missing[-1]}')
    kmap = fetch_tencent(sym, missing[0], missing[-1],
                         max(60, len(missing) + 10))

    filled, failed = [], []
    for date in missing:
        if date not in kmap:
            failed.append(f'{code} {date}(腾讯源无此日)')
            continue
        prev = conn.execute(
            "SELECT close, code_name FROM index_kline "
            "WHERE code=? AND date<? ORDER BY date DESC LIMIT 1",
            (code, date)).fetchone()
        if not prev or not prev[0]:
            failed.append(f'{code} {date}(前日close缺失, preclose链断)')
            continue
        preclose, code_name = prev[0], prev[1] or default_name
        o, c, h, l, vol_lots = kmap[date]
        rate = lambda x: round((x / preclose - 1) * 100, 4)
        row = dict(date=date, code=code, code_name=code_name,
                   open=o, high=h, low=l, close=c, preclose=preclose,
                   volume=vol_lots * 100, amount=None,
                   open_rate=rate(o), high_rate=rate(h),
                   low_rate=rate(l), close_rate=rate(c))
        conn.execute(
            'INSERT OR REPLACE INTO index_kline ({}) VALUES ({})'.format(
                ','.join(row), ','.join('?' * len(row))), list(row.values()))
        conn.commit()
        filled.append(date)
        print(f'回补 {code} {date}: close={c} preclose={preclose} '
              f'close_rate={row["close_rate"]:+.4f}%')
    return filled, failed


def main():
    args = [a for a in sys.argv[1:] if a != '--all']
    scan_all = '--all' in sys.argv[1:]
    conn = sqlite3.connect(DB)

    all_filled, all_failed = [], []
    for code, sym, default_name in CODES:
        filled, failed = backfill_code(conn, code, sym, default_name,
                                       args, scan_all)
        all_filled.extend(filled)
        all_failed.extend(failed)

    conn.close()
    if all_failed:
        print(f'!!! 未能回补: {all_failed} (请人工处理)')
        return 1
    print(f'回补完成: {len(all_filled)}行 '
          f'(red_ratio由update_red_ratio.py补齐, 仅sh.000001)')
    return 0


if __name__ == '__main__':
    sys.exit(main())
