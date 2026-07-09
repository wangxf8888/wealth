#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Task#105 诊断: 解释研究(888/+6.90%/67.6%) 与 slot=1回测 的差距根因。

三个层次对比 (全部买=D hour1_open, 卖=D+5 daily close, 无止盈损):
  A. 全部信号(研究口径)               -> 应复现 888/+6.90%/67.6%
  B. 每信号日只取1只(按最大跌幅优先)    -> 模拟V2当前排序
  C. 每信号日只取1只(按跌幅最小优先)    -> 反向排序对照
  D. 串行单仓(持仓期间不再开新仓)       -> 最接近slot=1真实容量
分别再给出逐年拆解, 定位胜率塌陷来源。
"""
import sqlite3

DB = "/home/AIWealth/data/stocks.db"
DROP_TH = -0.12
GAP_TH = 0.02
MKT_CAP_MAX = 50e8
LIMIT_UP_RATE = 1.20
COST = 0.001  # 双边各0.1%


def is_gem(code):
    return code.startswith("sz.300") or code.startswith("sz.301")


def load_data(cur):
    cols = ("date,code,code_name,preclose,open,high,low,close,amount,turn,isST,"
            "hour1_open,hour1_close,hour4_close")
    cur.execute(
        "SELECT %s FROM stock_kline WHERE code LIKE 'sz.300%%' OR code LIKE 'sz.301%%' "
        "ORDER BY code, date" % cols)
    names = [d[0] for d in cur.description]
    data = {}
    for row in cur.fetchall():
        r = dict(zip(names, row))
        data.setdefault(r["code"], []).append(r)
    return data


def find_signals(data):
    sigs = []
    for code, rows in data.items():
        n = len(rows)
        for i in range(1, n - 5):
            y = rows[i - 1]
            d = rows[i]
            if d["date"] < "2021-01-01":
                continue
            if not y["preclose"] or y["preclose"] <= 0:
                continue
            y_drop = (y["close"] - y["preclose"]) / y["preclose"]
            if y_drop > DROP_TH:
                continue
            if not y["close"] or y["close"] <= 0:
                continue
            gap = (d["open"] - y["close"]) / y["close"]
            if gap < GAP_TH:
                continue
            if d["isST"]:
                continue
            if not d["preclose"] or d["preclose"] <= 0:
                continue
            if d["open"] >= round(d["preclose"] * LIMIT_UP_RATE, 2):
                continue
            if not d["turn"] or d["turn"] <= 0:
                continue
            mkt = d["amount"] / (d["turn"] / 100.0)
            if mkt >= MKT_CAP_MAX:
                continue
            buy = d["hour1_open"]
            sell = rows[i + 5]["close"]  # D+5 daily close
            if not buy or buy <= 0 or not sell or sell <= 0:
                continue
            ret = (sell - buy) / buy
            sigs.append({
                "code": code, "date": d["date"], "y_drop": y_drop,
                "buy": buy, "sell": sell, "ret": ret,
                "sell_date": rows[i + 5]["date"],
            })
    sigs.sort(key=lambda s: (s["date"], s["code"]))
    return sigs


def stat(rets):
    if not rets:
        return (0, 0.0, 0.0)
    n = len(rets)
    wr = sum(1 for r in rets if r > 0) / n * 100
    avg = sum(rets) / n * 100
    return (n, avg, wr)


def by_year(sigs):
    yrs = {}
    for s in sigs:
        yrs.setdefault(s["date"][:4], []).append(s["ret"])
    return {y: stat(v) for y, v in sorted(yrs.items())}


def compound(sel):
    """串行复利 (含双边成本), 返回 (最终净值倍数, CAGR%)"""
    nav = 1.0
    for s in sel:
        gross = s["sell"] / s["buy"]
        nav *= gross * (1 - COST) * (1 - COST)
    if not sel:
        return 1.0, 0.0
    days = (5.5)  # 2021-01 ~ 2026-06 ~ 5.5年
    cagr = (nav ** (1 / days) - 1) * 100
    return nav, cagr


def pick_one_per_day(sigs, biggest_drop_first=True):
    days = {}
    for s in sigs:
        days.setdefault(s["date"], []).append(s)
    sel = []
    for d in sorted(days):
        cands = days[d]
        cands.sort(key=lambda x: x["y_drop"], reverse=not biggest_drop_first)
        sel.append(cands[0])
    return sel


def serial_one_slot(sigs, biggest_drop_first=True):
    """串行单仓: 持仓5交易日, 期间不开新仓。用交易日索引近似(信号日+5)。"""
    # 用日期序列近似持仓占用: 买入date后, 直到 sell_date 之前不再买
    days = {}
    for s in sigs:
        days.setdefault(s["date"], []).append(s)
    ordered_days = sorted(days)
    sel = []
    blocked_until = ""  # sell_date, 该日(含)之前不可买
    for d in ordered_days:
        if d <= blocked_until:
            continue
        cands = days[d]
        cands.sort(key=lambda x: x["y_drop"], reverse=not biggest_drop_first)
        chosen = cands[0]
        sel.append(chosen)
        blocked_until = chosen["sell_date"]
    return sel


def main():
    conn = sqlite3.connect(DB)
    cur = conn.cursor()
    data = load_data(cur)
    sigs = find_signals(data)
    conn.close()

    print("=" * 64)
    print("Task#105 跌停反弹策略 研究 vs 回测 差距诊断")
    print("买=D hour1_open, 卖=D+5 daily close, 无止盈损, 创业板<50亿")
    print("=" * 64)

    # A. 全部信号(研究口径)
    all_rets = [s["ret"] for s in sigs]
    n, avg, wr = stat(all_rets)
    print("\n[A] 全部信号(研究口径, 无成本):")
    print("    %d笔  均%+.2f%%  胜率%.1f%%" % (n, avg, wr))
    print("    逐年:", {y: "%d笔/%+.1f%%/%.0f%%" % v for y, v in by_year(sigs).items()})

    # B. 每日取最大跌幅1只 (=V2当前排序)
    selB = pick_one_per_day(sigs, biggest_drop_first=True)
    n, avg, wr = stat([s["ret"] for s in selB])
    print("\n[B] 每信号日取【最大跌幅】1只 (=V2当前 sort by prev_close_rate 升序):")
    print("    %d笔  均%+.2f%%  胜率%.1f%%" % (n, avg, wr))
    print("    逐年:", {y: "%d笔/%+.1f%%/%.0f%%" % v for y, v in by_year(selB).items()})

    # C. 每日取最小跌幅1只
    selC = pick_one_per_day(sigs, biggest_drop_first=False)
    n, avg, wr = stat([s["ret"] for s in selC])
    print("\n[C] 每信号日取【最小跌幅】1只 (反向排序对照):")
    print("    %d笔  均%+.2f%%  胜率%.1f%%" % (n, avg, wr))
    print("    逐年:", {y: "%d笔/%+.1f%%/%.0f%%" % v for y, v in by_year(selC).items()})

    # D. 串行单仓 (最接近slot=1真实容量)
    for tag, bdf in [("最大跌幅优先", True), ("最小跌幅优先", False)]:
        selD = serial_one_slot(sigs, biggest_drop_first=bdf)
        n, avg, wr = stat([s["ret"] for s in selD])
        nav, cagr = compound(selD)
        print("\n[D-%s] 串行单仓(持仓5日不重叠, 含双边成本%.1f%%):" % (tag, COST * 100))
        print("    %d笔  均%+.2f%%  胜率%.1f%%  终值%.3fx  CAGR%+.1f%%" % (n, avg, wr, nav, cagr))
        print("    逐年:", {y: "%d笔/%+.1f%%/%.0f%%" % v for y, v in by_year(selD).items()})


if __name__ == "__main__":
    main()
