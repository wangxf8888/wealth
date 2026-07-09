#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Task #24: 扫描日内反转和量价突破信号 V1 纯alpha测试
测试 5 种入场信号 F/G/H/I/J，跨 2020-2025 年统计 mean / win_rate。
所有 *_rate 字段在数据库中以百分数存储（0.45 表示 0.45%）。
"""
import os
import sqlite3
import statistics
from collections import defaultdict

DB = '/home/AIWealth/data/stocks.db'
YEARS = ['2020', '2021', '2022', '2023', '2024', '2025']


def get_board(code):
    if code.startswith('sh.60') or code.startswith('sz.00'):
        return 'main'
    if code.startswith('sz.30'):
        return 'cy'
    if code.startswith('sh.68'):
        return 'kc'
    if code.startswith('bj.'):
        return 'bj'
    return 'other'


def limit_up_ratio(code):
    return 1.20 if get_board(code) in ('cy', 'kc') else 1.10


def limit_down_ratio(code):
    return 0.80 if get_board(code) in ('cy', 'kc') else 0.90


def is_limit_up(close, preclose, code):
    if not preclose or preclose <= 0 or close is None:
        return False
    return round(close / preclose, 2) >= limit_up_ratio(code)


def is_limit_down(close, preclose, code):
    if not preclose or preclose <= 0 or close is None:
        return False
    return round(close / preclose, 2) <= limit_down_ratio(code)


def safe_ret(buy, sell):
    if buy is None or sell is None or buy <= 0:
        return None
    return (sell - buy) / buy


def fetch_st_codes(conn):
    cur = conn.cursor()
    cur.execute("SELECT DISTINCT code FROM stock_kline WHERE code_name LIKE '%ST%'")
    return set(r[0] for r in cur.fetchall())


COLS = (
    'date,code,code_name,preclose,open,open_rate,high,low,close,close_rate,'
    'volume,turn,'
    'hour1_open,hour1_high,hour1_low,hour1_close,hour1_close_rate,'
    'hour2_open,hour2_close,hour3_open,hour3_close,hour4_open,hour4_close,'
    'hour1_volume'
)
COL_LIST = COLS.split(',')


def row_to_dict(row):
    return dict(zip(COL_LIST, row))


def iter_by_code(conn, exclude_codes):
    """流式扫描全表，按 code 分组 yield。"""
    cur = conn.cursor()
    cur.execute(f"SELECT {COLS} FROM stock_kline ORDER BY code, date")
    cur_code = None
    buf = []
    for r in cur:
        d = row_to_dict(r)
        code = d['code']
        if code != cur_code:
            if cur_code is not None and cur_code not in exclude_codes \
               and not cur_code.startswith('bj.'):
                yield cur_code, buf
            cur_code = code
            buf = []
        buf.append(d)
    if cur_code is not None and cur_code not in exclude_codes \
       and not cur_code.startswith('bj.'):
        yield cur_code, buf


def year_of(date_str):
    return date_str[:4]


# ---- Signal evaluators ----
def eval_signal_F(rows, results):
    """Hour1 暴跌 4%+ 后 Hour2 反转。买 T.hour2_open，卖 T+1.hour1_open。"""
    n = len(rows)
    for i in range(n - 1):
        t = rows[i]
        nx = rows[i + 1]
        h1cr = t['hour1_close_rate']
        if h1cr is None or h1cr > -4.0:
            continue
        if t['hour1_low'] is None or t['hour1_close'] is None or t['hour1_low'] <= 0:
            continue
        if t['hour1_close'] <= t['hour1_low'] * 1.005:
            continue
        buy = t['hour2_open']
        sell = nx['hour1_open']
        r = safe_ret(buy, sell)
        if r is None:
            continue
        y = year_of(t['date'])
        if y in YEARS:
            results[y].append(r)


def eval_signal_G(rows, results):
    """T-1 放量3倍且涨>3%，T hour1 回调>=1%。买 T.hour2_open，卖 T+1.hour1_open。"""
    n = len(rows)
    for i in range(6, n - 1):
        # T-1 = rows[i-1]; volume avg of T-6..T-2 = rows[i-6..i-2]
        prev = rows[i - 1]
        if prev['close_rate'] is None or prev['close_rate'] <= 3.0:
            continue
        vols = [rows[j]['volume'] for j in range(i - 6, i - 1)
                if rows[j]['volume'] is not None]
        if len(vols) < 5:
            continue
        avg5 = sum(vols) / 5.0
        if avg5 <= 0 or prev['volume'] is None:
            continue
        if prev['volume'] < avg5 * 3:
            continue
        t = rows[i]
        h1cr = t['hour1_close_rate']
        if h1cr is None or h1cr > -1.0:
            continue
        nx = rows[i + 1]
        buy = t['hour2_open']
        sell = nx['hour1_open']
        r = safe_ret(buy, sell)
        if r is None:
            continue
        y = year_of(t['date'])
        if y in YEARS:
            results[y].append(r)


def eval_signal_H(rows, results):
    """跌停开板。T 触跌停未封住，反弹>2%。买 T+1.hour1_open，卖 T+2.hour1_open。"""
    n = len(rows)
    for i in range(n - 2):
        t = rows[i]
        code = t['code']
        preclose = t['preclose']
        if not preclose or preclose <= 0:
            continue
        ratio = limit_down_ratio(code)  # 0.90 主板 / 0.80 创科
        ld_price = round(preclose * ratio, 2)
        if t['low'] is None or t['low'] > ld_price:
            continue
        # 收盘未跌停
        cls_ratio = round(t['close'] / preclose, 2)
        if cls_ratio <= ratio:
            continue
        # 反弹幅度: close/preclose - 1 > (ratio-1) + 0.02
        rebound_threshold = (ratio - 1) + 0.02
        if (t['close'] / preclose - 1) <= rebound_threshold:
            continue
        # 排除 ST / bj （fetch_codes 已过滤，但保险起见）
        if get_board(code) == 'bj':
            continue
        if t['code_name'] and 'ST' in t['code_name'].upper():
            continue
        buy = rows[i + 1]['hour1_open']
        sell = rows[i + 2]['hour1_open']
        r = safe_ret(buy, sell)
        if r is None:
            continue
        y = year_of(t['date'])
        if y in YEARS:
            results[y].append(r)


def eval_signal_I(rows, results):
    """高开放量日内交易。买 T.hour1_open(=open)，卖 T.hour4_close(=close)。"""
    n = len(rows)
    for i in range(1, n):
        t = rows[i]
        prev = rows[i - 1]
        if t['open_rate'] is None or t['open_rate'] < 3.0:
            continue
        # T-1 非涨停
        if is_limit_up(prev['close'], prev['preclose'], prev['code']):
            continue
        # hour1 量能放大 2 倍
        t_h1v = t['hour1_volume']
        p_h1v = prev['hour1_volume']
        if t_h1v is not None and p_h1v is not None and p_h1v > 0:
            if t_h1v < p_h1v * 2:
                continue
        else:
            # 字段缺失则用 volume/4 近似
            if t['volume'] is None or prev['volume'] is None or prev['volume'] <= 0:
                continue
            if (t['volume'] / 4.0) < (prev['volume'] / 4.0) * 2:
                continue
        buy = t['hour1_open']
        sell = t['hour4_close']
        r = safe_ret(buy, sell)
        if r is None:
            continue
        y = year_of(t['date'])
        if y in YEARS:
            results[y].append(r)


def eval_signal_J(rows, results):
    """连续3日缩量后放量启动。买 T+1.hour1_open，卖 T+2.hour1_open。"""
    n = len(rows)
    for i in range(3, n - 2):
        t = rows[i]
        v0 = t['volume']
        v1 = rows[i - 1]['volume']
        v2 = rows[i - 2]['volume']
        v3 = rows[i - 3]['volume']
        if None in (v0, v1, v2, v3) or v1 <= 0:
            continue
        # 连续 3 日递减: vol(T-1) < vol(T-2) < vol(T-3)
        if not (v1 < v2 < v3):
            continue
        # T 突然放量
        if v0 < v1 * 2:
            continue
        # T 上涨 > 2%
        if t['close_rate'] is None or t['close_rate'] <= 2.0:
            continue
        buy = rows[i + 1]['hour1_open']
        sell = rows[i + 2]['hour1_open']
        r = safe_ret(buy, sell)
        if r is None:
            continue
        y = year_of(t['date'])
        if y in YEARS:
            results[y].append(r)


SIGNALS = [
    ('F', 'Hour1暴跌后Hour2反转', eval_signal_F),
    ('G', '放量突破后次日回调接', eval_signal_G),
    ('H', '跌停开板反弹', eval_signal_H),
    ('I', '高开放量日内动量', eval_signal_I),
    ('J', '缩量后放量启动', eval_signal_J),
]


def summarize(results):
    """results: dict[year] -> list of returns (decimal, e.g. 0.012 = 1.2%)"""
    lines = []
    all_rets = []
    for y in YEARS:
        rs = results.get(y, [])
        all_rets.extend(rs)
        if rs:
            mean = sum(rs) / len(rs)
            wr = sum(1 for r in rs if r > 0) / len(rs)
            lines.append(f"{y}: N={len(rs):5d}, mean={mean*100:+.2f}%, win_rate={wr*100:.1f}%")
        else:
            lines.append(f"{y}: N=    0, mean=  0.00%, win_rate= 0.0%")
    if all_rets:
        m = sum(all_rets) / len(all_rets)
        wr = sum(1 for r in all_rets if r > 0) / len(all_rets)
        lines.append(f"TOTAL: N={len(all_rets):5d}, mean={m*100:+.2f}%, win_rate={wr*100:.1f}%")
        verdict = '有' if m > 0 else '无'
        lines.append(f"结论: {verdict} alpha (mean={m*100:+.2f}%)")
    else:
        lines.append("TOTAL: N=0")
        lines.append("结论: 无样本")
    return '\n'.join(lines)


def main():
    conn = sqlite3.connect(DB)
    print(f"[INFO] 数据库: {DB}")
    st_codes = fetch_st_codes(conn)
    print(f"[INFO] ST 黑名单数: {len(st_codes)}")

    # results: signal_key -> dict[year] -> list of returns
    results = {key: defaultdict(list) for key, _, _ in SIGNALS}

    processed = 0
    for code, rows in iter_by_code(conn, st_codes):
        processed += 1
        if len(rows) < 10:
            continue
        for key, _, fn in SIGNALS:
            fn(rows, results[key])
        if processed % 500 == 0:
            print(f"[PROGRESS] processed {processed} stocks", flush=True)

    conn.close()
    print(f"[INFO] 处理完成，总股票数: {processed}")

    print()
    for key, name, _ in SIGNALS:
        print(f"=== Signal {key}: {name} ===")
        print(summarize(results[key]))
        print()


if __name__ == '__main__':
    main()
