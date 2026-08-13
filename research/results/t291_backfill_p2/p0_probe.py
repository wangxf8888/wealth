#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""[Task#291] P0实测: BaoStock指数5分钟K可得性探测(仅2次请求)。

复用t233_runner的t270 socket守护+fetch_window(import即生效monkey-patch),
统一账本acquire('batch')埋点。探测项:
1. sh.000001 近期一周 → 指数是否支持frequency=5, 字段结构, bar数/日
2. sh.000001 2021年初一周 → 起始年份可得性
"""
import json
import subprocess
import sys

sys.path.insert(0, '/home/AIWealth')
sys.path.insert(0, '/home/AIWealth/tools')
sys.path.insert(0, '/home/AIWealth/research/results/t233_touchboard_backfill')

import t233_runner as R          # noqa: E402  t270守护patch随import生效
import baostock as bs            # noqa: E402


def concurrent_check():
    out = subprocess.run(
        ['bash', '-c',
         "ps aux | grep -E 'fetch_daily|fetch_minute_kline|t233_runner|t291'"
         " | grep -v grep | grep -v p0_probe || true"],
        capture_output=True, text=True).stdout.strip()
    return out


def main():
    busy = concurrent_check()
    if busy:
        print(f"⛔ 零并发红线: 有fetch/runner在跑, 退出:\n{busy}")
        return 1
    if not R.ban_recovered():
        print("⛔ ban_status非recovered, 不发起请求")
        return 1
    if not R.budget_acquire_ok(2):
        print("⛔ 统一日预算(batch)不足2次, 退出")
        return 1
    lg = bs.login()
    if lg.error_code != '0':
        print(f"⛔ login失败: {lg.error_code} {lg.error_msg}")
        return 1
    result = {}
    for tag, (s, e) in {'recent': ('2026-08-03', '2026-08-07'),
                        'y2021': ('2021-01-04', '2021-01-08')}.items():
        rows, pages, err = R.fetch_window('sh.000001', s, e)
        days = {}
        for r in rows:
            days.setdefault(r[1], []).append(r)
        result[tag] = {
            'window': [s, e], 'err': err, 'pages': pages,
            'n_rows': len(rows), 'n_days': len(days),
            'bars_per_day': {d: len(v) for d, v in sorted(days.items())},
            'first_row': rows[0] if rows else None,
            'last_row': rows[-1] if rows else None,
        }
        print(f"[{tag}] {s}~{e}: err={err} rows={len(rows)} "
              f"days={len(days)} bars/day="
              f"{sorted(set(len(v) for v in days.values()))}")
        if rows:
            print(f"  first={rows[0]}  last={rows[-1]}")
    bs.logout()
    with open('/home/AIWealth/research/results/t291_backfill_p2/'
              'p0_probe_result.json', 'w', encoding='utf-8') as f:
        json.dump(result, f, ensure_ascii=False, indent=1)
    return 0


if __name__ == '__main__':
    sys.exit(main())
