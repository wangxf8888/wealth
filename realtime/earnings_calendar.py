#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""预约披露日买入拦截 — 日历读取与持有窗判定 (Task#297, 用户直接指令:
"候选股如果触发买入，持股期间如果会触发业绩公告的，这种不要买入，避免碰雷")。

数据源: data/earnings_calendar.csv (tools/fetch_earnings_calendar.py每日20:45
盘后刷新[Task#303自22:40前移], 东财RPT_PUBLIC_BS_APPOIN预约披露时间表)。

判定口径: 候选股最新预约披露日 ∈ (D0买入日, D0+策略最长持有天数+1天缓冲]
→ earnings_block=true。持有天数=ceil(策略max_hold_hours/4)(4小时=1交易日),
窗口按工作日(周一~五)外推——节假日外推误差由+1天缓冲吸收。

fail-open铁律(与load_ann_alerts/load_user_excluded同安全降级风格):
日历文件缺失/损坏/数据过期(fetched_at距今>3天) → 一律不拦截任何候选,
只留告警日志——数据断供绝不误杀全部候选。
"""
import csv
import math
import os
from datetime import datetime, timedelta

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CALENDAR_CSV = os.path.join(PROJECT_ROOT, 'data', 'earnings_calendar.csv')

# 数据新鲜度门槛(天): fetched_at距今超过此值视为断供过期 → fail-open
STALE_DAYS = 3

# 持有窗额外缓冲(交易日): 覆盖披露日前夜盘后发布/节假日外推误差
BUFFER_DAYS = 1


def load_earnings_calendar(path: str = CALENDAR_CSV):
    """读取预约披露日历。

    Returns:
        (by_code, err): by_code={code: [appoint_date,...]}(仅未披露is_publish!=1
        的预约, 已实际披露的雷已爆无需拦截); err=''表示可用,
        非空=fail-open原因(缺失/损坏/过期), 此时by_code={}。
    """
    try:
        if not os.path.exists(path):
            return {}, f'日历文件缺失({path})'
        by_code = {}
        fetched_at = ''
        with open(path, newline='', encoding='utf-8') as f:
            for row in csv.DictReader(f):
                fetched_at = row.get('fetched_at') or fetched_at
                if (row.get('is_publish') or '0').strip() == '1':
                    continue        # 已实际披露: 不再构成持有期未爆雷
                code = (row.get('code') or '').strip()
                ap = (row.get('appoint_date') or '').strip()
                if code and ap:
                    by_code.setdefault(code, []).append(ap)
        if not by_code:
            return {}, '日历文件为空/无未披露预约行'
        if not fetched_at:
            return {}, '日历缺fetched_at字段无法判新鲜度'
        age = datetime.now() - datetime.strptime(fetched_at,
                                                 '%Y-%m-%d %H:%M:%S')
        if age.days > STALE_DAYS:
            return {}, (f'日历数据过期(fetched_at={fetched_at}, '
                        f'距今{age.days}天>{STALE_DAYS})')
        return by_code, ''
    except Exception as e:
        return {}, f'日历读取异常({e!r})'


def max_hold_days_of(max_hold_hours) -> int:
    """策略max_hold_hours(4小时=1交易日)→最长持有交易日数, 异常兜底2天。"""
    try:
        h = int(max_hold_hours)
        if h <= 0:
            return 2
        return math.ceil(h / 4)
    except (TypeError, ValueError):
        return 2


def _window_end(trade_date: str, n_days: int) -> str:
    """自trade_date起向后数n_days个工作日(周一~五)的日期。

    节假日未剔除(项目无未来交易日历), 误差由BUFFER_DAYS缓冲吸收。
    """
    dt = datetime.strptime(trade_date, '%Y-%m-%d')
    added = 0
    while added < n_days:
        dt += timedelta(days=1)
        if dt.weekday() < 5:
            added += 1
    return dt.strftime('%Y-%m-%d')


def check_earnings_window(code: str, trade_date: str, max_hold_hours,
                          by_code: dict) -> str:
    """判定code的预约披露日是否落在(D0, D0+持有天数+缓冲]持有窗内。

    Returns:
        命中的预约披露日'YYYY-MM-DD'; 未命中/无预约返回''。
    """
    dates = by_code.get(code)
    if not dates:
        return ''
    hold_days = max_hold_days_of(max_hold_hours)
    end = _window_end(trade_date, hold_days + BUFFER_DAYS)
    hits = [d for d in dates if trade_date < d <= end]
    return min(hits) if hits else ''
