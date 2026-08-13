#!/usr/bin/env python3
"""
Task#253 承诺到期日历 v1 构建器
================================
从巨潮历史公告提取当前持仓股的"承诺类"公告, 生成
data/realtime/commitment_calendar.json; announcement_monitor.py 7:30扫描
读取该文件, 到期前3日对持仓股并入预警。

v1口径(如实说明边界):
- 仅标题级提取(不解析PDF正文), 每持仓1请求, 限速1s
- 到期日推断仅覆盖"延期履行…承诺"类: 宁夏建材铁证(2024-08-10→2026-08-10
  精确2年周期, t252 EVAL_REPORT), expiry=ann_date+2年, basis标注estimated;
  其他承诺类公告只登记不推断到期日(拒绝编造数据)
- 建议每次持仓变更后重跑; cron不排(v1手动/随7:30扫描按需)

用法: python3 tools/build_commitment_calendar.py
"""
import json
import os
import sys
from datetime import datetime, timedelta

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT_ROOT)

import importlib.util
_spec = importlib.util.spec_from_file_location(
    'announcement_monitor',
    os.path.join(PROJECT_ROOT, 'tools', 'announcement_monitor.py'))
am = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(am)

CALENDAR_FILE = os.path.join(PROJECT_ROOT, 'data', 'realtime',
                             'commitment_calendar.json')
LOOKBACK_YEARS = 3        # 回看3年承诺类公告
CYCLE_DAYS_2Y = 730       # 延期履行类2年周期(宁夏建材同款前科)


def main():
    today = datetime.now().strftime('%Y-%m-%d')
    sdate = (datetime.now() - timedelta(days=365 * LOOKBACK_YEARS)
             ).strftime('%Y-%m-%d')
    holdings = am.load_holdings()
    if not holdings:
        print('持仓为空, 不生成日历')
        return 0
    items = []
    for code, name in holdings:
        code6 = code.split('.')[-1]
        # "代码+承诺"组合检索(巨潮侧过滤, 免受30条分页截断; secCode硬过滤兼容)
        anns = am.fetch_announcements(code6, sdate, today, extra_kw='承诺')
        if anns is None:
            print(f'[WARN] {code} {name} 巨潮查询失败, 本股跳过')
            continue
        n_hit = 0
        for a in anns:
            title = am.clean_title(a.get('announcementTitle', ''))
            if '承诺' not in title:
                continue
            adj = a.get('adjunctUrl', '')
            ann_date = adj.split('/')[1] if '/' in adj else ''
            item = {'code': code, 'name': name, 'title': title,
                    'ann_date': ann_date,
                    'url': am.PDF_BASE + adj if adj else '',
                    'expiry_date': '', 'basis': ''}
            # 仅"延期履行…承诺"类推断2年周期到期日(宁夏建材铁证同款)
            if '延期履行' in title and ann_date:
                exp = (datetime.strptime(ann_date, '%Y-%m-%d')
                       + timedelta(days=CYCLE_DAYS_2Y)).strftime('%Y-%m-%d')
                item['expiry_date'] = exp
                item['basis'] = ('estimated_2y_cycle: 宁夏建材2024-08-10→'
                                 '2026-08-10同款延期公告精确2年周期(t252)')
            items.append(item)
            n_hit += 1
        print(f'{code} {name}: 承诺类公告{n_hit}条')
    # 窗口由sdate/edate限定, 巨潮侧已按"承诺"检索词过滤, 标题再二次确认
    out = {
        'generated_at': datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
        'lookback_years': LOOKBACK_YEARS,
        'holdings_covered': [c for c, _ in holdings],
        'items': items,
        '_note': ('v1标题级提取; expiry_date仅延期履行类按2年周期估算, '
                  '空值=未推断(拒绝编造); 持仓变更后建议重跑'),
    }
    tmp = CALENDAR_FILE + '.tmp'
    with open(tmp, 'w', encoding='utf-8') as f:
        json.dump(out, f, ensure_ascii=False, indent=2)
    os.replace(tmp, CALENDAR_FILE)
    print(f'[OK] {CALENDAR_FILE} items={len(items)}')
    return 0


if __name__ == '__main__':
    sys.exit(main())
