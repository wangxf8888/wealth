#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Task #70  跌停反弹(创业板<50亿) + 大盘环境过滤 稳健性验证

背景:
  Task#67 rule2 已确认最优配置: h1_open买入 / 无止盈 / 无止损 / 持有到T+5
  全周期888笔, 均+6.90%, 胜率67.6%
  弱年问题: 2021 -1.57%(46.7%), 2022 +0.24%(45.2%)
  本脚本: 加大盘(sh.000001)环境过滤, 验证弱年能否转正/不亏。

策略条件(同Task#67):
  板块 sz.300/301, 流通市值<50亿
  yesterday(D-1): (close-preclose)/preclose <= -0.12
  today(D):       (open-yesterday_close)/yesterday_close >= 0.02
  非ST, 非涨停开盘
  买入: D hour1_open   卖出: T+5 close  (无止盈无止损)

大盘过滤(用yesterday的指数数据, T+0合规):
  近N日累计涨幅 = (idx_close[D-1] / idx_close[D-1-N] - 1) * 100
  方案A: 近5日 > -1%
  方案B: 近5日 > 0%
  方案C: 近10日 > -2%
  方案D: 近5日 > -1% 且 近20日 > -5%
  无过滤: baseline

用法:
  python3 research_limitdown_filtered.py        # 全量(2021-2026)
  python3 research_limitdown_filtered.py all

日志: /home/AIWealth/scripts/logs/limitdown_filtered.log  (Python open/write)
"""
import sqlite3
import sys
import os
import bisect

DB = "/home/AIWealth/data/stocks.db"
LOG = "/home/AIWealth/scripts/logs/limitdown_filtered.log"

# ---------------- 条件参数 ----------------
DROP_TH = -0.12       # 前日大跌阈值
GAP_TH = 0.02         # 今日高开阈值
MKT_CAP_MAX = 50e8    # 流通市值上限
LIMIT_UP_RATE = 1.20  # 创业板涨停20% (非涨停开盘判定)
HOLD = 5              # 持有到 T+5

YEARS = ["2021", "2022", "2023", "2024", "2025", "2026"]

# 大盘过滤方案定义: (名称, 判定函数(idx5, idx10, idx20))
SCHEMES = [
    ("方案A: 近5日>-1%",              lambda f5, f10, f20: f5 is not None and f5 > -1.0),
    ("方案B: 近5日>0%",               lambda f5, f10, f20: f5 is not None and f5 > 0.0),
    ("方案C: 近10日>-2%",             lambda f5, f10, f20: f10 is not None and f10 > -2.0),
    ("方案D: 近5日>-1% 且 近20日>-5%", lambda f5, f10, f20: (f5 is not None and f5 > -1.0
                                                            and f20 is not None and f20 > -5.0)),
]


# ==================== 指数数据 ====================
def load_index(cur):
    """加载 sh.000001 每日 close, 返回 (dates升序列表, close列表)。"""
    cur.execute("SELECT date, close FROM index_kline WHERE code='sh.000001' "
                "AND close IS NOT NULL ORDER BY date")
    dates, closes = [], []
    for d, c in cur.fetchall():
        dates.append(d)
        closes.append(float(c))
    return dates, closes


def idx_cum_return(idx_dates, idx_closes, signal_date, n):
    """
    近n日累计涨幅(%), 用yesterday(<signal_date)的指数数据, T+0合规。
    = (idx_close[p] / idx_close[p-n] - 1) * 100
    其中 p = 最后一个 < signal_date 的指数交易日下标。
    数据不足返回 None。
    """
    p = bisect.bisect_left(idx_dates, signal_date) - 1  # 最后一个 < signal_date
    if p < 0 or p - n < 0:
        return None
    base = idx_closes[p - n]
    if base <= 0:
        return None
    return (idx_closes[p] / base - 1) * 100.0


# ==================== 个股数据 ====================
def is_gem(code):
    return code.startswith("sz.300") or code.startswith("sz.301")


def load_data(cur):
    """加载创业板全部日线(含hour级), 按code分组按date排序。"""
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


def find_signals(data, idx_dates, idx_closes):
    """
    遍历找信号(2021-01-01起), 每个信号计算:
      ret  = (T+5 close - buy) / buy   (buy=D hour1_open)
      idx5/idx10/idx20 = 近N日大盘累计涨幅(用yesterday指数, T+0合规)
    """
    signals = []
    for code, rows in data.items():
        n = len(rows)
        for i in range(1, n - HOLD):  # 需要 D-1 及 D+1..D+5 完整前向窗口
            y = rows[i - 1]   # yesterday D-1
            d = rows[i]       # today  D  (信号日)
            if d["date"] < "2021-01-01":
                continue
            # 前日大跌
            if not y["preclose"] or y["preclose"] <= 0:
                continue
            y_drop = (y["close"] - y["preclose"]) / y["preclose"]
            if y_drop > DROP_TH:
                continue
            # 今日高开
            if not y["close"] or y["close"] <= 0:
                continue
            gap = (d["open"] - y["close"]) / y["close"]
            if gap < GAP_TH:
                continue
            # 非ST
            if d["isST"]:
                continue
            # 非涨停开盘
            if not d["preclose"] or d["preclose"] <= 0:
                continue
            if d["open"] >= round(d["preclose"] * LIMIT_UP_RATE, 2):
                continue
            # 流通市值
            if not d["turn"] or d["turn"] <= 0:
                continue
            mkt_cap = d["amount"] / (d["turn"] / 100.0)
            if mkt_cap >= MKT_CAP_MAX:
                continue
            # 买入价 & 收益 (T+5 close)
            buy = d["hour1_open"]
            if not buy or buy <= 0:
                continue
            sell = rows[i + HOLD]["close"]
            if not sell or sell <= 0:
                continue
            ret = (sell - buy) / buy
            # 大盘过滤指标
            f5 = idx_cum_return(idx_dates, idx_closes, d["date"], 5)
            f10 = idx_cum_return(idx_dates, idx_closes, d["date"], 10)
            f20 = idx_cum_return(idx_dates, idx_closes, d["date"], 20)
            signals.append({
                "code": code, "name": d["code_name"], "date": d["date"],
                "year": d["date"][:4], "y_drop": y_drop, "gap": gap,
                "mkt_cap": mkt_cap, "ret": ret,
                "idx5": f5, "idx10": f10, "idx20": f20,
            })
    signals.sort(key=lambda s: (s["date"], s["code"]))
    return signals


# ==================== 统计 ====================
def _mean(lst):
    return sum(lst) / len(lst) if lst else 0.0


def _winrate(lst):
    if not lst:
        return 0.0
    return sum(1 for x in lst if x > 0) / len(lst)


def stat_block(sigs):
    """返回 (笔数, 均收益%, 胜率%) 及逐年 dict{year:(n,mean%,wr%)}。"""
    rets = [s["ret"] for s in sigs]
    overall = (len(rets), _mean(rets) * 100, _winrate(rets) * 100)
    yearly = {}
    for yr in YEARS:
        yr_rets = [s["ret"] for s in sigs if s["year"] == yr]
        yearly[yr] = (len(yr_rets), _mean(yr_rets) * 100, _winrate(yr_rets) * 100)
    return overall, yearly


def worst_year(yearly):
    """返回收益最低且有信号的年份 (yr, mean%)。"""
    cand = [(yr, v[1]) for yr, v in yearly.items() if v[0] > 0]
    if not cand:
        return (None, 0.0)
    return min(cand, key=lambda x: x[1])


def all_positive(yearly):
    """所有有信号的年份是否均正收益。"""
    ys = [v for v in yearly.values() if v[0] > 0]
    return all(v[1] > 0 for v in ys) if ys else False


# ==================== 主流程 ====================
def main():
    arg = sys.argv[1] if len(sys.argv) > 1 else "all"
    if arg != "all":
        print("提示: 本脚本一次性跑全量对比, 参数固定为 all")

    os.makedirs(os.path.dirname(LOG), exist_ok=True)
    f = open(LOG, "w", encoding="utf-8")

    def w(text):
        f.write(text)

    conn = sqlite3.connect(DB)
    cur = conn.cursor()

    print("加载指数 sh.000001 ...")
    idx_dates, idx_closes = load_index(cur)
    print("指数交易日: %d (%s ~ %s)" % (len(idx_dates), idx_dates[0], idx_dates[-1]))

    print("加载创业板个股数据...")
    data = load_data(cur)
    print("创业板股票数: %d" % len(data))

    print("扫描信号...")
    signals = find_signals(data, idx_dates, idx_closes)
    print("全周期信号数: %d" % len(signals))
    conn.close()

    # ---------------- 写日志头 ----------------
    w("跌停反弹(创业板<50亿) - 大盘环境过滤验证\n")
    w("=" * 59 + "\n")
    w("策略: 前日跌>=12%%, 今日高开>=2%%, 流通市值<50亿, 非ST, 非涨停开盘\n")
    w("买入: D hour1_open   卖出: T+5 close   (无止盈无止损)\n")
    w("大盘过滤: sh.000001 近N日累计涨幅(用yesterday指数, T+0合规)\n")
    w("=" * 59 + "\n")

    # ---------------- Baseline ----------------
    base_overall, base_yearly = stat_block(signals)
    w("\n=== Baseline(无过滤) ===\n")
    w("全周期: %d笔, 均%+.2f%%, 胜率%.1f%%\n" % base_overall)
    w("逐年:\n")
    for yr in YEARS:
        n, m, wr = base_yearly[yr]
        w("  %s: %d笔, %+.2f%%, %.1f%%\n" % (yr, n, m, wr))

    # ---------------- 各方案 ----------------
    scheme_results = []  # (name, overall, yearly)
    for name, cond in SCHEMES:
        passed = [s for s in signals if cond(s["idx5"], s["idx10"], s["idx20"])]
        filtered = [s for s in signals if not cond(s["idx5"], s["idx10"], s["idx20"])]
        overall, yearly = stat_block(passed)
        scheme_results.append((name, overall, yearly))

        w("\n=== %s ===\n" % name)
        w("全周期: %d笔, 均%+.2f%%, 胜率%.1f%%\n" % overall)
        w("逐年:\n")
        for yr in YEARS:
            n, m, wr = yearly[yr]
            w("  %s: %d笔, %+.2f%%, %.1f%%\n" % (yr, n, m, wr))
        fn = len(filtered)
        ftot = len(signals)
        fpct = (fn / ftot * 100) if ftot else 0.0
        fret = _mean([s["ret"] for s in filtered]) * 100
        w("过滤掉的笔数: %d (占%.1f%%)\n" % (fn, fpct))
        w("过滤掉的平均收益: %+.2f%% (被过滤信号本身的收益)\n" % fret)

    # ---------------- 最终对比 ----------------
    w("\n=== 最终对比 ===\n")
    w("%-18s | %-5s | %-7s | %-6s | %-16s | %s\n" %
      ("方案", "笔数", "均收益", "胜率", "最差年", "全年正?"))
    bw = worst_year(base_yearly)
    w("%-18s | %-5d | %+6.2f%% | %5.1f%% | %-16s | %s\n" % (
        "无过滤", base_overall[0], base_overall[1], base_overall[2],
        "%s(%+.2f%%)" % (bw[0], bw[1]), "YES" if all_positive(base_yearly) else "NO"))
    for name, overall, yearly in scheme_results:
        wy = worst_year(yearly)
        wy_str = "%s(%+.2f%%)" % (wy[0], wy[1]) if wy[0] else "-"
        w("%-18s | %-5d | %+6.2f%% | %5.1f%% | %-16s | %s\n" % (
            name.split(":")[0], overall[0], overall[1], overall[2],
            wy_str, "YES" if all_positive(yearly) else "NO"))

    # ---------------- 结论 ----------------
    # 优选逻辑: 优先全年正收益, 其次弱年(最差年)收益最高, 再次全周期均收益
    def score(item):
        name, overall, yearly = item
        ap = 1 if all_positive(yearly) else 0
        wy = worst_year(yearly)[1]
        return (ap, wy, overall[1])
    best = max(scheme_results, key=score)
    b_name, b_overall, b_yearly = best
    b_worst = worst_year(b_yearly)
    b_allpos = all_positive(b_yearly)

    # 月化估算(单仓串行近似)
    months = set(s["date"][:7] for s in signals if
                 [x for x in SCHEMES if x[0] == b_name][0][1](s["idx5"], s["idx10"], s["idx20"]))
    nmonth = max(1, len(months))
    sig_per_month = b_overall[0] / nmonth
    monthly = (b_overall[1] / 100.0) * sig_per_month * 100

    w("\n=== 结论 ===\n")
    w("最优方案: %s\n" % b_name)
    w("原因: 优先满足'所有年份正收益', 其次最差年收益最高, 再看全周期均收益。\n")
    w("  全周期: %d笔, 均%+.2f%%, 胜率%.1f%%\n" % b_overall)
    w("  最差年: %s(%+.2f%%)\n" % (b_worst[0], b_worst[1]))
    w("过滤后是否所有年份正收益: %s\n" % ("YES" if b_allpos else "NO"))
    w("  月均信号: %.1f笔 (共%d个月), 预期月化(单仓串行近似): %+.2f%%\n" % (
        sig_per_month, nmonth, monthly))
    达标 = "YES" if (monthly >= 10.0 and b_overall[2] >= 55.0) else "NO"
    w("是否达标(月化10%%+/胜率55%%+): %s\n" % 达标)

    f.close()

    # 控制台回显
    with open(LOG, "r", encoding="utf-8") as fr:
        lines = fr.readlines()
    print("".join(lines))
    print("\n[日志已写入] %s (%d行)" % (LOG, len(lines)))


if __name__ == "__main__":
    main()
