#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""每日预约披露日历刷新 (Task#297 业绩披露日买入拦截 数据层)。

数据源: 东财datacenter RPT_PUBLIC_BS_APPOIN (data.eastmoney.com/bbsj/yysj
预约披露时间表, 2026-08-11 probe_source.py实测: 当季5543行/206ms/翻页正常,
与在产lhb_backfill/t252同域同限速0.6s/请求)。

拉取范围: 当前报告期 + 下一报告期(未发布时东财返回9201空, 属正常静默跳过)。
产物: data/earnings_calendar.csv
  字段: code(sh./sz./bj.项目口径), appoint_date(最新预约披露日),
        report_date(报告期), is_publish(1=已实际披露), actual_publish_date,
        fetched_at(数据更新时点, 消费方按此判新鲜度>3天fail-open)
写入: 原子写tmp+rename(与generate_candidates.save_candidates_json同风格)。

cron: 每交易日22:40盘后刷新(23:30候选生成消费)。
失败处理: 首页拉取失败/零行 → 保留旧CSV不覆盖 + scheduler_alerts告警
(消费方按fetched_at过期fail-open不拦截, 铁律: 数据断供不误杀候选)。
"""
import csv
import os
import sys
import time
from datetime import datetime, date

import requests

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT_CSV = os.path.join(PROJECT_ROOT, 'data', 'earnings_calendar.csv')
ALERT_LOG = os.path.join(PROJECT_ROOT, 'logs', 'realtime',
                         'scheduler_alerts.log')

URL = 'https://datacenter-web.eastmoney.com/api/data/v1/get'
HEADERS = {
    'User-Agent': ('Mozilla/5.0 (Windows NT 10.0; Win64; x64) '
                   'AppleWebKit/537.36'),
    'Referer': 'https://data.eastmoney.com/bbsj/yysj/',
}
REPORT_NAME = 'RPT_PUBLIC_BS_APPOIN'
PAGE_SIZE = 500
SLEEP_SEC = 0.6          # 与lhb_backfill/t252在产节奏一致
CSV_FIELDS = ['code', 'appoint_date', 'report_date', 'is_publish',
              'actual_publish_date', 'fetched_at']


def _alert(msg: str):
    """写scheduler_alerts.log人工巡检入口(失败静默, 不炸主流程)。"""
    try:
        os.makedirs(os.path.dirname(ALERT_LOG), exist_ok=True)
        with open(ALERT_LOG, 'a', encoding='utf-8') as f:
            f.write(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] "
                    f"{msg}\n")
    except OSError:
        pass


def relevant_report_dates(today: date) -> list:
    """当前报告期+下一报告期(按披露截止日: Q1/年报4-30, 半年报8-31, Q3 10-31)。

    "当前"=最早一个披露截止日>=today的报告期, "下一"=其后一期。
    年报季(1~4月)自然覆盖 上年年报(12-31)+一季报(03-31) 双期并行。
    """
    # (报告期月-日, 披露截止(月, 日, 跨年偏移))
    periods = [('03-31', (4, 30, 0)), ('06-30', (8, 31, 0)),
               ('09-30', (10, 31, 0)), ('12-31', (4, 30, 1))]
    seq = []
    for year in (today.year - 1, today.year, today.year + 1):
        for md, (dm, dd, yoff) in periods:
            rpt = f'{year}-{md}'
            deadline = date(year + yoff, dm, dd)
            seq.append((deadline, rpt))
    seq.sort()
    upcoming = [rpt for deadline, rpt in seq if deadline >= today]
    return upcoming[:2]


def to_project_code(secucode: str) -> str:
    """'002107.SZ' → 'sz.002107' (项目sh./sz./bj.统一口径)。"""
    if not secucode or '.' not in secucode:
        return ''
    num, mkt = secucode.rsplit('.', 1)
    mkt = mkt.lower()
    if mkt not in ('sh', 'sz', 'bj'):
        return ''
    return f'{mkt}.{num}'


def fetch_page(report_date: str, page: int, retry: int = 3):
    """单页拉取, 返回result dict; 东财9201空数据返回{'count':0,...}; 失败None。"""
    params = {'reportName': REPORT_NAME, 'columns': 'ALL',
              'pageNumber': page, 'pageSize': PAGE_SIZE, 'source': 'WEB',
              'filter': f"(REPORT_DATE='{report_date}')",
              'sortColumns': 'SECURITY_CODE', 'sortTypes': '1'}
    for i in range(retry):
        try:
            j = requests.get(URL, params=params, headers=HEADERS,
                             timeout=15).json()
            if j.get('success'):
                return j['result']
            if j.get('code') == 9201:      # 该报告期预约尚未发布(正常)
                return {'count': 0, 'pages': 0, 'data': []}
            print(f'  [WARN] {report_date} p{page} api fail: '
                  f'{j.get("message")}', flush=True)
        except Exception as e:
            print(f'  [WARN] {report_date} p{page} try{i} err: {e}',
                  flush=True)
        time.sleep(2 + i * 3)
    return None


def fetch_report_period(report_date: str) -> list:
    """整报告期全量拉取。返回行列表; 首页失败返回None(区别于空)。"""
    r1 = fetch_page(report_date, 1)
    if r1 is None:
        return None
    rows, seen = [], set()

    def collect(data):
        for row in data:
            code = to_project_code(row.get('SECUCODE') or '')
            appoint = (row.get('APPOINT_PUBLISH_DATE')
                       or row.get('FIRST_APPOINT_DATE') or '')[:10]
            if not code or not appoint:
                continue
            key = (code, report_date)
            if key in seen:
                continue
            seen.add(key)
            rows.append({
                'code': code,
                'appoint_date': appoint,
                'report_date': report_date,
                'is_publish': row.get('IS_PUBLISH') or '0',
                'actual_publish_date':
                    (row.get('ACTUAL_PUBLISH_DATE') or '')[:10],
            })

    collect(r1['data'])
    pages = r1.get('pages', 0)
    for pg in range(2, pages + 1):
        time.sleep(SLEEP_SEC)
        r = fetch_page(report_date, pg)
        if r is None:
            print(f'  [WARN] {report_date} p{pg} 重试耗尽, 该期数据可能不完整',
                  flush=True)
            return None
        collect(r['data'])
    print(f'  [{report_date}] 拉取{len(rows)}行 (pages={pages})', flush=True)
    return rows


def main():
    today = date.today()
    report_dates = relevant_report_dates(today)
    print(f'[fetch_earnings_calendar] {today} 目标报告期: {report_dates}',
          flush=True)

    all_rows, any_fail = [], False
    for i, rpt in enumerate(report_dates):
        if i > 0:
            time.sleep(SLEEP_SEC)
        rows = fetch_report_period(rpt)
        if rows is None:
            any_fail = True
            continue
        all_rows.extend(rows)

    if not all_rows:
        msg = ('🚨 预约披露日历刷新: 全部报告期拉取失败/零行, 保留旧CSV; '
               '消费方将按fetched_at过期(>3天)fail-open不拦截, 需人工排查'
               ' (tools/fetch_earnings_calendar.py)')
        print(f'[FAIL] {msg}', flush=True)
        _alert(msg)
        return 1

    fetched_at = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    for r in all_rows:
        r['fetched_at'] = fetched_at

    tmp = OUT_CSV + '.tmp'
    with open(tmp, 'w', newline='', encoding='utf-8') as f:
        w = csv.DictWriter(f, fieldnames=CSV_FIELDS)
        w.writeheader()
        w.writerows(all_rows)
    os.replace(tmp, OUT_CSV)
    print(f'[OK] {OUT_CSV} 写入{len(all_rows)}行 '
          f'(报告期{report_dates}, fetched_at={fetched_at})', flush=True)

    if any_fail:
        msg = ('⚠️ 预约披露日历刷新: 部分报告期拉取失败(已写入成功部分), '
               '详见logs/realtime/fetch_earnings_calendar.log')
        _alert(msg)
    return 0


if __name__ == '__main__':
    sys.exit(main())
