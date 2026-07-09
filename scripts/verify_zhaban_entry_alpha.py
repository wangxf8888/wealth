#!/usr/bin/env python3
"""
Task #22: ZhaBan限价买入入场信号 纯统计alpha验证

目的：在不假设任何出场策略的前提下，验证 ZhaBan 限价买入信号本身
      在 T+1/T+2/T+3 各关键价格点上是否存在正期望（alpha）。

入场信号：
- T-1: 涨停过(intraday hit limit-up)但收盘未封住 (炸板)
       turn > 15% AND close_rate > 5%
- T:   open_rate < 5%(非一字高开)
       hour2_low <= hour1_close*0.99 (限价单能成交)
- 排除: ST、bj.、一字板
- 买入价 P = hour1_close * 0.99

输出：按年份及全样本统计，并按 turn / close_rate 分组对比。
"""

import sqlite3
import statistics
from collections import defaultdict
from datetime import datetime

DB_PATH = "/home/AIWealth/data/stocks.db"


# ---------- 工具函数 ----------

def get_limit_up_ratio(code: str) -> float:
    """主板10%，创业板/科创板20%。bj 排除掉。"""
    if code.startswith("sz.30") or code.startswith("sh.68"):
        return 1.20
    return 1.10


def is_zhaban(row, ratio: float) -> bool:
    """T-1 炸板：盘中触及涨停 但 收盘未封住。"""
    pre = row["preclose"]
    if pre is None or pre <= 0:
        return False
    high = row["high"]
    close = row["close"]
    if high is None or close is None:
        return False
    high_ratio = round(high / pre, 2)
    close_ratio = round(close / pre, 2)
    return high_ratio >= ratio and close_ratio < ratio


def is_one_word_board(row) -> bool:
    o, h, l, c = row["open"], row["high"], row["low"], row["close"]
    if None in (o, h, l, c):
        return False
    return o == h == l == c


def excluded_by_code_or_name(code: str, name: str) -> bool:
    if code.startswith("bj."):
        return True
    if name and "ST" in name.upper():
        return True
    return False


def pct(values, threshold, op):
    if not values:
        return 0.0
    if op == ">":
        n = sum(1 for v in values if v > threshold)
    elif op == "<":
        n = sum(1 for v in values if v < threshold)
    else:
        raise ValueError(op)
    return n / len(values) * 100.0


def quantile(values, q):
    """简易分位（线性插值）。"""
    if not values:
        return float("nan")
    s = sorted(values)
    if len(s) == 1:
        return s[0]
    pos = (len(s) - 1) * q
    lo = int(pos)
    hi = min(lo + 1, len(s) - 1)
    frac = pos - lo
    return s[lo] * (1 - frac) + s[hi] * frac


def summarize(values):
    """返回 dict: N/mean/median/P25/P75/win%/big_win%/big_loss%."""
    if not values:
        return None
    return {
        "N": len(values),
        "mean": sum(values) / len(values) * 100,        # 百分比
        "median": statistics.median(values) * 100,
        "P25": quantile(values, 0.25) * 100,
        "P75": quantile(values, 0.75) * 100,
        "win%": pct(values, 0.0, ">"),
        ">3%": pct(values, 0.03, ">"),
        "<-3%": pct(values, -0.03, "<"),
    }


def fmt(stat):
    if stat is None:
        return "无样本"
    return (f"N={stat['N']:>5} mean={stat['mean']:+6.2f}% med={stat['median']:+6.2f}% "
            f"P25={stat['P25']:+6.2f}% P75={stat['P75']:+6.2f}% "
            f"win={stat['win%']:5.1f}% >3%={stat['>3%']:5.1f}% <-3%={stat['<-3%']:5.1f}%")


# ---------- 数据加载 ----------

def load_trading_dates(conn):
    cur = conn.execute("SELECT DISTINCT date FROM stock_kline ORDER BY date")
    return [r[0] for r in cur.fetchall()]


def fetch_day(conn, date):
    """返回 dict: code -> Row。"""
    cur = conn.execute(
        "SELECT * FROM stock_kline WHERE date=?", (date,)
    )
    out = {}
    for r in cur.fetchall():
        out[r["code"]] = r
    return out


# ---------- 信号识别 ----------

def find_signals(conn, dates):
    """
    遍历所有 (T-1, T) 相邻交易日对，输出符合入场条件的 (T_date, code, P, T_minus1_row, T_row) 列表。
    """
    signals = []
    prev_date = None
    prev_data = None

    for i, d in enumerate(dates):
        if i == 0:
            prev_date = d
            prev_data = fetch_day(conn, d)
            continue

        t_data = fetch_day(conn, d)
        # 找 T-1 炸板候选
        candidates = []
        for code, prow in prev_data.items():
            if excluded_by_code_or_name(code, prow["code_name"]):
                continue
            ratio = get_limit_up_ratio(code)
            if not is_zhaban(prow, ratio):
                continue
            turn = prow["turn"]
            close_rate = prow["close_rate"]
            if turn is None or close_rate is None:
                continue
            if turn <= 15.0 or close_rate <= 5.0:
                continue
            candidates.append((code, prow))

        # T日检查
        for code, prow in candidates:
            trow = t_data.get(code)
            if trow is None:
                continue  # T日停牌
            if excluded_by_code_or_name(code, trow["code_name"]):
                continue
            if is_one_word_board(trow):
                continue
            o_rate = trow["open_rate"]
            if o_rate is None or o_rate >= 5.0:
                continue
            h1_close = trow["hour1_close"]
            h2_low = trow["hour2_low"]
            if h1_close is None or h2_low is None or h1_close <= 0:
                continue
            P = h1_close * 0.99
            if h2_low > P:
                continue  # 限价单无法成交
            signals.append({
                "T_date": d,
                "code": code,
                "P": P,
                "turn_t1": prow["turn"],
                "close_rate_t1": prow["close_rate"],
            })

        prev_date = d
        prev_data = t_data

    return signals


# ---------- 后续 N 个交易日数据 ----------

def get_future_rows(conn, code, t_date, n=3):
    cur = conn.execute(
        "SELECT * FROM stock_kline WHERE code=? AND date>? ORDER BY date ASC LIMIT ?",
        (code, t_date, n),
    )
    return cur.fetchall()


# ---------- 收益率计算 ----------

def collect_metrics(conn, signals):
    """对每个信号计算各价格点相对 P 的收益率，按年份累积。"""
    # 价格点定义
    # T+1: h1_open, h1_high, h1_low, day_high, day_low, h4_close
    # T+2: h1_open, day_high, day_low, h4_close
    # T+3: h1_open, h4_close
    metric_keys = [
        "T+1_h1_open", "T+1_h1_high", "T+1_h1_low", "T+1_day_high", "T+1_day_low", "T+1_h4_close",
        "T+2_h1_open", "T+2_day_high", "T+2_day_low", "T+2_h4_close",
        "T+3_h1_open", "T+3_h4_close",
    ]

    # 全样本
    all_data = defaultdict(list)
    # 按年份
    by_year = defaultdict(lambda: defaultdict(list))
    # 按turn分组
    by_turn = defaultdict(lambda: defaultdict(list))
    # 按close_rate分组
    by_cr = defaultdict(lambda: defaultdict(list))

    kept = 0
    for sig in signals:
        future = get_future_rows(conn, sig["code"], sig["T_date"], n=3)
        if len(future) < 1:
            continue
        P = sig["P"]
        year = sig["T_date"][:4]

        # turn 分组
        t = sig["turn_t1"]
        if 15 < t <= 20:
            tg = "15-20%"
        elif 20 < t <= 30:
            tg = "20-30%"
        else:
            tg = ">30%"

        # close_rate 分组
        cr = sig["close_rate_t1"]
        if 5 < cr <= 7:
            cg = "5-7%"
        elif 7 < cr <= 9:
            cg = "7-9%"
        else:
            cg = ">9%"

        local = {}

        # T+1
        if len(future) >= 1:
            r = future[0]
            if r["hour1_open"] is not None:
                local["T+1_h1_open"] = r["hour1_open"] / P - 1
            if r["hour1_high"] is not None:
                local["T+1_h1_high"] = r["hour1_high"] / P - 1
            if r["hour1_low"] is not None:
                local["T+1_h1_low"] = r["hour1_low"] / P - 1
            highs = [r["hour1_high"], r["hour2_high"], r["hour3_high"], r["hour4_high"]]
            lows = [r["hour1_low"], r["hour2_low"], r["hour3_low"], r["hour4_low"]]
            highs = [x for x in highs if x is not None]
            lows = [x for x in lows if x is not None]
            if highs:
                local["T+1_day_high"] = max(highs) / P - 1
            if lows:
                local["T+1_day_low"] = min(lows) / P - 1
            if r["hour4_close"] is not None:
                local["T+1_h4_close"] = r["hour4_close"] / P - 1

        # T+2
        if len(future) >= 2:
            r = future[1]
            if r["hour1_open"] is not None:
                local["T+2_h1_open"] = r["hour1_open"] / P - 1
            highs = [r["hour1_high"], r["hour2_high"], r["hour3_high"], r["hour4_high"]]
            lows = [r["hour1_low"], r["hour2_low"], r["hour3_low"], r["hour4_low"]]
            highs = [x for x in highs if x is not None]
            lows = [x for x in lows if x is not None]
            if highs:
                local["T+2_day_high"] = max(highs) / P - 1
            if lows:
                local["T+2_day_low"] = min(lows) / P - 1
            if r["hour4_close"] is not None:
                local["T+2_h4_close"] = r["hour4_close"] / P - 1

        # T+3
        if len(future) >= 3:
            r = future[2]
            if r["hour1_open"] is not None:
                local["T+3_h1_open"] = r["hour1_open"] / P - 1
            if r["hour4_close"] is not None:
                local["T+3_h4_close"] = r["hour4_close"] / P - 1

        if not local:
            continue
        kept += 1
        for k, v in local.items():
            all_data[k].append(v)
            by_year[year][k].append(v)
            by_turn[tg][k].append(v)
            by_cr[cg][k].append(v)

    return metric_keys, all_data, by_year, by_turn, by_cr, kept


# ---------- 报告输出 ----------

def print_table(title, metric_keys, data_map):
    print(f"\n{'=' * 100}")
    print(f"  {title}")
    print(f"{'=' * 100}")
    for k in metric_keys:
        s = summarize(data_map.get(k, []))
        print(f"  {k:<18} {fmt(s)}")


def main():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row

    print("=" * 100)
    print(" Task #22: ZhaBan限价买入入场信号 纯统计alpha验证")
    print("=" * 100)
    t0 = datetime.now()

    print("\n[1] 加载交易日历...")
    dates = load_trading_dates(conn)
    print(f"    交易日数 = {len(dates)}, 范围 {dates[0]} ~ {dates[-1]}")

    print("\n[2] 扫描入场信号(炸板T-1 + 限价T成交) ...")
    signals = find_signals(conn, dates)
    print(f"    候选信号数 = {len(signals)}")

    print("\n[3] 计算各价格点收益率 ...")
    metric_keys, all_data, by_year, by_turn, by_cr, kept = collect_metrics(conn, signals)
    print(f"    最终有效样本(至少有T+1数据) = {kept}")

    # 全样本
    print_table("【全样本】 入场后各时点 收益率(%)统计", metric_keys, all_data)

    # 按年份
    for y in sorted(by_year.keys()):
        print_table(f"【按年份】 {y}年", metric_keys, by_year[y])

    # 按 turn
    print("\n" + "#" * 100)
    print("# 额外分析 1：按 T-1 turn 分组")
    print("#" * 100)
    for g in ["15-20%", "20-30%", ">30%"]:
        if g in by_turn:
            print_table(f"【turn={g}】", metric_keys, by_turn[g])

    # 按 close_rate
    print("\n" + "#" * 100)
    print("# 额外分析 2：按 T-1 close_rate 分组")
    print("#" * 100)
    for g in ["5-7%", "7-9%", ">9%"]:
        if g in by_cr:
            print_table(f"【close_rate={g}】", metric_keys, by_cr[g])

    # 总结
    print("\n" + "=" * 100)
    print(" 总结：T+1 hour1_open 平均收益（即次日开盘卖出的期望）")
    print("=" * 100)
    s_total = summarize(all_data.get("T+1_h1_open", []))
    print(f"  全样本: {fmt(s_total)}")
    for y in sorted(by_year.keys()):
        s = summarize(by_year[y].get("T+1_h1_open", []))
        print(f"  {y}年  : {fmt(s)}")

    s_close = summarize(all_data.get("T+1_h4_close", []))
    print()
    print(" T+1 hour4_close 平均收益（即次日收盘卖出的期望）:")
    print(f"  全样本: {fmt(s_close)}")
    for y in sorted(by_year.keys()):
        s = summarize(by_year[y].get("T+1_h4_close", []))
        print(f"  {y}年  : {fmt(s)}")

    dt = (datetime.now() - t0).total_seconds()
    print(f"\n[完成] 耗时 {dt:.1f}s")
    conn.close()


if __name__ == "__main__":
    main()
