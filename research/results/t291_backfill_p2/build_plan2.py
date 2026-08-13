#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""[Task#291] P1全市场普通日remainder查询计划生成(纯本地零网络)。

复用一期(t233 build_final.py)定案打包逻辑:
- 宇宙: stock_kline全市场股·日(2021-01-01~库内最新日, 仅白名单sh./sz.——
  北交所bj.*无BaoStock分钟数据, 另实测库内混有test.*脏数据一并剔除,
  请求必空纯耗预算)
- 减集: minute.db已有>=48bar完整覆盖的(code,date); 不足48bar的残日重补
  (窗口顺带, 不增请求)
- 打包: 同股41交易日窗口贪心(41*48=1968行<=2000单页, rs.next()永不翻页),
  gap日顺带入库白得覆盖
- 排序(任务卡): 2024-2026段(研究价值最高)优先 → 2021-2023殿后。分段独立
  打包(段界不跨窗), 段内按code顺序
- 停牌日(volume=0/NULL)仍留在目标集: 请求返回空由runner按suspended记账,
  与一期语义一致

输出: plan2.json {meta, intervals:[{code,start,end,dates,id}]}
"""
import json
import os
import sqlite3
from collections import defaultdict
from datetime import datetime

STOCKS_DB = '/home/AIWealth/data/stocks.db'
MINUTE_DB = '/home/AIWealth/data/minute.db'
OUT_DIR = os.path.dirname(os.path.abspath(__file__))
PLAN_JSON = os.path.join(OUT_DIR, 'plan2.json')

START = '2021-01-01'
SEG_SPLIT = '2024-01-01'     # 段A: >=此日(优先) / 段B: <此日(殿后)
BARS = 48
WINDOW_TDAYS = 41
ROW_BYTES = 78               # minute.db实测均摊(2.82GB/36.2M行)


def log(m):
    print(f"{datetime.now().strftime('%H:%M:%S')} {m}", flush=True)


def pack(code, idxs, cal, ci):
    """一期同款窗口贪心: 目标日索引升序, 每窗从首个未覆盖目标起铺41交易日。"""
    out = []
    i = 0
    n = len(idxs)
    while i < n:
        s_idx = idxs[i]
        e_idx = min(s_idx + WINDOW_TDAYS - 1, len(cal) - 1)
        dates = []
        while i < n and idxs[i] <= e_idx:
            dates.append(cal[idxs[i]])
            i += 1
        out.append({'code': code, 'start': cal[s_idx],
                    'end': cal[e_idx], 'dates': dates})
    return out


def main():
    sconn = sqlite3.connect(f'file:{STOCKS_DB}?mode=ro', uri=True)
    end = sconn.execute("SELECT MAX(date) FROM stock_kline").fetchone()[0]
    cal = [r[0] for r in sconn.execute(
        "SELECT DISTINCT date FROM stock_kline WHERE date>=? AND date<=? "
        "ORDER BY date", (START, end))]
    ci = {d: i for i, d in enumerate(cal)}
    log(f"交易日历: {START}~{end} 共{len(cal)}日")

    log("minute.db已覆盖集合聚合(>=48bar)...")
    mconn = sqlite3.connect(f'file:{MINUTE_DB}?mode=ro', uri=True)
    covered = set()
    for c, d in mconn.execute(
            "SELECT code, date FROM minute_kline WHERE date>=? "
            "GROUP BY code, date HAVING COUNT(*)>=?", (START, BARS)):
        covered.add((c, d))
    mconn.close()
    log(f"已完整覆盖: {len(covered):,}股·日")

    log("stock_kline全市场目标集扫描...")
    todo = defaultdict(list)       # code -> [day_idx](升序, 按扫描序保证)
    n_all = n_bj = 0
    yr_todo = defaultdict(int)
    for code, d in sconn.execute(
            "SELECT code, date FROM stock_kline WHERE date>=? AND date<=? "
            "ORDER BY code, date", (START, end)):
        n_all += 1
        if not (code.startswith('sh.') or code.startswith('sz.')):
            n_bj += 1        # bj.*/test.*等非sh/sz代码一律剔除
            continue
        if (code, d) in covered:
            continue
        todo[code].append(ci[d])
        yr_todo[d[:4]] += 1
    n_todo = sum(len(v) for v in todo.values())
    log(f"全市场股·日{n_all:,} | 北交所剔除{n_bj:,} | 待补{n_todo:,} "
        f"({len(todo):,}只) | 按年: {dict(sorted(yr_todo.items()))}")

    split_idx = ci[min(d for d in cal if d >= SEG_SPLIT)]
    seg_a, seg_b = [], []          # A=2024-2026优先, B=2021-2023殿后
    for code in sorted(todo):
        idxs = todo[code]
        ia = [i for i in idxs if i >= split_idx]
        ib = [i for i in idxs if i < split_idx]
        if ia:
            seg_a.extend(pack(code, ia, cal, ci))
        if ib:
            seg_b.extend(pack(code, ib, cal, ci))
    intervals = seg_a + seg_b
    for k, iv in enumerate(intervals):
        iv['id'] = k

    span = sum(ci[iv['end']] - ci[iv['start']] + 1 for iv in intervals)
    est_gb = span * BARS * ROW_BYTES / 1024 ** 3
    gb_a = (sum(ci[iv['end']] - ci[iv['start']] + 1 for iv in seg_a)
            * BARS * ROW_BYTES / 1024 ** 3)
    meta = {'built_at': datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
            'range': [START, end], 'window_tdays': WINDOW_TDAYS,
            'universe': 'stock_kline全市场(仅sh./sz., 剔bj.*与test.*)', 'seg_split': SEG_SPLIT,
            'n_all_stockdays': n_all, 'n_bj_excluded': n_bj,
            'n_covered': len(covered), 'n_todo': n_todo,
            'n_codes': len(todo), 'yr_todo': dict(sorted(yr_todo.items())),
            'n_intervals': len(intervals),
            'n_seg_a_2024_2026': len(seg_a), 'n_seg_b_2021_2023': len(seg_b),
            'span_tdays': span, 'est_disk_gb': round(est_gb, 2),
            'est_disk_gb_seg_a': round(gb_a, 2)}
    with open(PLAN_JSON, 'w', encoding='utf-8') as f:
        json.dump({'meta': meta, 'intervals': intervals}, f,
                  ensure_ascii=False)
    log(f"计划落盘{PLAN_JSON}: {json.dumps(meta, ensure_ascii=False)}")
    sconn.close()


if __name__ == '__main__':
    main()
