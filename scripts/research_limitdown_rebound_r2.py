#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Task #67  跌停反弹(创业板<50亿)止盈止损配置研究  (rule2 流程)

策略思路:
  创业板小市值(<50亿)股票, 前日大跌>=12%(接近跌停)后, 今日跳空高开>=2%
  -> 超跌反弹 + 情绪修复. 买入=信号日(D)hour1_open, T+1开始计止盈止损.

用法:
  python3 research_limitdown_rebound_r2.py 2024-09   # 单月
  python3 research_limitdown_rebound_r2.py 2024       # 单年
  python3 research_limitdown_rebound_r2.py all        # 全量 2021-2026

日志: /home/AIWealth/scripts/logs/limitdown_rebound_r2.log  (Python open/write)
"""
import sqlite3
import sys
import os

DB = "/home/AIWealth/data/stocks.db"
LOG = "/home/AIWealth/scripts/logs/limitdown_rebound_r2.log"

# ---------------- 条件参数 ----------------
DROP_TH = -0.12      # 前日大跌阈值
GAP_TH = 0.02        # 今日高开阈值
MKT_CAP_MAX = 50e8   # 流通市值上限
LIMIT_UP_RATE = 1.20 # 创业板涨停20% (非涨停开盘判定)

# ---------------- 止盈/止损配置 (至少12种) ----------------
# (名称, 止盈tp或None, 止损sl(负)或None, 最长持仓天数)
CONFIGS = [
    ("A: +5%止盈/-3%止损/T+2",   0.05,  -0.03, 2),
    ("B: +8%止盈/-5%止损/T+3",   0.08,  -0.05, 3),
    ("C: +10%止盈/-5%止损/T+5",  0.10,  -0.05, 5),
    ("D: +15%止盈/无止损/T+5",   0.15,  None,  5),
    ("E: 无止盈/-8%止损/T+2到期", None,  -0.08, 2),
    ("F: 无止盈/无止损/T+2到期",  None,  None,  2),
    ("G: +3%止盈/-3%止损/T+2",   0.03,  -0.03, 2),
    ("H: +5%止盈/-5%止损/T+3",   0.05,  -0.05, 3),
    ("I: +6%止盈/-4%止损/T+2",   0.06,  -0.04, 2),
    ("J: +8%止盈/-8%止损/T+5",   0.08,  -0.08, 5),
    ("K: +10%止盈/-8%止损/T+5",  0.10,  -0.08, 5),
    ("L: +7%止盈/-5%止损/T+3",   0.07,  -0.05, 3),
    ("M: +5%止盈/无止损/T+5",    0.05,  None,  5),
    ("N: 无止盈/无止损/T+5到期",  None,  None,  5),
    ("O: +6%止盈/-3%止损/T+3",   0.06,  -0.03, 3),
    ("P: +12%止盈/-6%止损/T+5",  0.12,  -0.06, 5),
]

TP_LEVELS = [0.03, 0.05, 0.08, 0.10, 0.15]
DD_LEVELS = [-0.03, -0.05, -0.08, -0.10]


def is_gem(code):
    return code.startswith("sz.300") or code.startswith("sz.301")


def load_data(cur):
    """加载创业板全部日线(含hour级), 按code分组按date排序。"""
    cols = ("date,code,code_name,preclose,open,high,low,close,amount,turn,isST,"
            "hour1_open,hour1_high,hour1_low,hour1_close,"
            "hour2_open,hour2_high,hour2_low,hour2_close,"
            "hour3_open,hour3_high,hour3_low,hour3_close,"
            "hour4_open,hour4_high,hour4_low,hour4_close")
    cur.execute(
        "SELECT %s FROM stock_kline WHERE code LIKE 'sz.300%%' OR code LIKE 'sz.301%%' "
        "ORDER BY code, date" % cols)
    names = [d[0] for d in cur.description]
    data = {}
    for row in cur.fetchall():
        r = dict(zip(names, row))
        data.setdefault(r["code"], []).append(r)
    return data


def hour_bars(day):
    """返回该日4个小时bar的(high,low)列表, None回退到日级。"""
    bars = []
    for h in (1, 2, 3, 4):
        hi = day.get("hour%d_high" % h)
        lo = day.get("hour%d_low" % h)
        if hi is None:
            hi = day["high"]
        if lo is None:
            lo = day["low"]
        bars.append((hi, lo))
    return bars


def find_signals(data, period):
    """遍历找信号。period: (kind, value)  kind in month/year/all"""
    signals = []
    for code, rows in data.items():
        n = len(rows)
        for i in range(1, n - 5):  # 需要 D-1 及 D+1..D+5 完整前向窗口
            y = rows[i - 1]   # yesterday D-1
            d = rows[i]       # today  D  (信号日)
            # 期间过滤
            if not _in_period(d["date"], period):
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
            signals.append({
                "code": code, "name": d["code_name"], "date": d["date"],
                "y_drop": y_drop, "gap": gap, "mkt_cap": mkt_cap,
                "idx": i, "rows": rows,
            })
    signals.sort(key=lambda s: (s["date"], s["code"]))
    return signals


def _in_period(date, period):
    kind, val = period
    if kind == "all":
        # 全量默认从2021起(2020数据保留做前置窗口)
        return date >= "2021-01-01"
    if kind == "year":
        return date[:4] == val
    if kind == "month":
        return date[:7] == val
    return True


def simulate(fwd, buy, tp, sl, max_hold):
    """从T+1(fwd[0])开始逐小时检查, 先触高后触低(乐观假设)。返回收益率。"""
    for dnum in range(max_hold):
        day = fwd[dnum]
        for (hi, lo) in hour_bars(day):
            if tp is not None and hi >= buy * (1 + tp):
                return tp, "tp", dnum + 1
            if sl is not None and lo <= buy * (1 + sl):
                return sl, "sl", dnum + 1
    ret = (fwd[max_hold - 1]["close"] - buy) / buy
    return ret, "exp", max_hold


def analyze(signals):
    """对信号集合做汇总统计, 返回结构化结果 dict。"""
    res = {"n": len(signals), "days": len(set(s["date"] for s in signals))}
    if not signals:
        return res

    # 买入时机对比
    buy_keys = [("h1_open", "hour1_open"), ("h1_close", "hour1_close"),
                ("h2_open", "hour2_open")]
    timing = {k: {"T1": [], "T2": [], "T5": []} for k, _ in buy_keys}
    # 止盈触及 (buy=h1_open)
    tp_hit = {lv: {"hit": 0, "days": []} for lv in TP_LEVELS}
    dd_dist = {lv: 0 for lv in DD_LEVELS}
    max_dd_list = []
    cfg_ret = {c[0]: [] for c in CONFIGS}

    for s in signals:
        rows = s["rows"]
        i = s["idx"]
        d = rows[i]
        fwd = rows[i + 1:i + 6]  # T+1..T+5
        # 买入时机
        for k, col in buy_keys:
            buy = d[col]
            if not buy or buy <= 0:
                continue
            timing[k]["T1"].append((fwd[0]["close"] - buy) / buy)
            timing[k]["T2"].append((fwd[1]["close"] - buy) / buy)
            timing[k]["T5"].append((fwd[4]["close"] - buy) / buy)

        buy = d["hour1_open"]
        if not buy or buy <= 0:
            continue
        # 止盈触及率
        for lv in TP_LEVELS:
            reached = None
            for dnum in range(5):
                for (hi, lo) in hour_bars(fwd[dnum]):
                    if hi >= buy * (1 + lv):
                        reached = dnum + 1
                        break
                if reached:
                    break
            if reached:
                tp_hit[lv]["hit"] += 1
                tp_hit[lv]["days"].append(reached)
        # 最大回撤 (T+1..T+5 全部小时low)
        min_ret = 0.0
        for dnum in range(5):
            for (hi, lo) in hour_bars(fwd[dnum]):
                r = (lo - buy) / buy
                if r < min_ret:
                    min_ret = r
        max_dd_list.append(min_ret)
        for lv in DD_LEVELS:
            if min_ret > lv:
                dd_dist[lv] += 1
        # 配置模拟
        for name, tp, sl, mh in CONFIGS:
            ret, _, _ = simulate(fwd, buy, tp, sl, mh)
            cfg_ret[name].append(ret)

    res["timing"] = timing
    res["tp_hit"] = tp_hit
    res["dd_dist"] = dd_dist
    res["cfg_ret"] = cfg_ret
    res["n_valid"] = len(max_dd_list)
    return res


def _mean(lst):
    return sum(lst) / len(lst) if lst else 0.0


def _winrate(lst):
    if not lst:
        return 0.0
    return sum(1 for x in lst if x > 0) / len(lst)


def write_summary(w, res, title, signals=None):
    w("\n===== 汇总分析 [%s] =====\n" % title)
    w("总信号: %d笔 (%d个信号日)\n" % (res["n"], res["days"]))
    if not res["n"]:
        w("(无信号)\n")
        return None
    nv = res["n_valid"]

    w("\n--- 买入时机对比 ---\n")
    for k in ("h1_open", "h1_close", "h2_open"):
        t = res["timing"][k]
        w("  %s买入: 均收益%+.2f%%(T+1), %+.2f%%(T+2), %+.2f%%(T+5)\n" % (
            k, _mean(t["T1"]) * 100, _mean(t["T2"]) * 100, _mean(t["T5"]) * 100))

    w("\n--- 止盈触及率(买入=h1_open, 从T+1开始计) ---\n")
    for lv in TP_LEVELS:
        h = res["tp_hit"][lv]
        rate = h["hit"] / nv * 100 if nv else 0
        avgd = _mean(h["days"])
        w("  +%d%%: %.1f%%触及, 均%.1f天达到\n" % (int(lv * 100), rate, avgd))

    w("\n--- 最大回撤分布(持仓T+1到T+5) ---\n")
    for lv in DD_LEVELS:
        rate = res["dd_dist"][lv] / nv * 100 if nv else 0
        w("  回撤>%d%%: %.1f%%\n" % (int(lv * 100), rate))

    w("\n--- 配置对比(止盈/止损/最长持仓组合) ---\n")
    cfg_stats = []
    for name, tp, sl, mh in CONFIGS:
        rets = res["cfg_ret"][name]
        m = _mean(rets)
        wr = _winrate(rets)
        cfg_stats.append((name, m, wr))
        w("  %-28s -> 均收益%+.2f%%, 胜率%.1f%%\n" % (name, m * 100, wr * 100))

    # 最优: 均收益最高, 胜率tie-break
    best = max(cfg_stats, key=lambda x: (x[1], x[2]))
    w("\n--- 最优配置(按均收益) ---\n")
    w("  %s -> 均收益%+.2f%%, 胜率%.1f%%\n" % (best[0], best[1] * 100, best[2] * 100))
    return best


def write_yearly(w, data, best_name):
    """逐年稳定性(最优配置)。"""
    tp = sl = mh = None
    for name, t, s, m in CONFIGS:
        if name == best_name:
            tp, sl, mh = t, s, m
            break
    w("\n--- 逐年稳定性(最优配置: %s) ---\n" % best_name)
    all_rets = []
    yearly = {}
    for yr in ["2021", "2022", "2023", "2024", "2025", "2026"]:
        sigs = find_signals(data, ("year", yr))
        rets = []
        for s in sigs:
            rows = s["rows"]
            i = s["idx"]
            buy = rows[i]["hour1_open"]
            if not buy or buy <= 0:
                continue
            fwd = rows[i + 1:i + 6]
            ret, _, _ = simulate(fwd, buy, tp, sl, mh)
            rets.append(ret)
        yearly[yr] = rets
        all_rets.extend(rets)
        w("  %s: %d笔, 均%+.2f%%, 胜率%.1f%%\n" % (
            yr, len(rets), _mean(rets) * 100, _winrate(rets) * 100))
    return all_rets, yearly


def main():
    if len(sys.argv) < 2:
        print("用法: python3 research_limitdown_rebound_r2.py [2024-09|2024|all]")
        sys.exit(1)
    arg = sys.argv[1]
    if arg == "all":
        period = ("all", None)
    elif len(arg) == 7 and "-" in arg:
        period = ("month", arg)
    elif len(arg) == 4:
        period = ("year", arg)
    else:
        print("参数格式错误: %s" % arg)
        sys.exit(1)

    os.makedirs(os.path.dirname(LOG), exist_ok=True)
    f = open(LOG, "w", encoding="utf-8")

    def w(text):
        f.write(text)

    conn = sqlite3.connect(DB)
    cur = conn.cursor()
    w("========================================================\n")
    w("跌停反弹策略研究 (创业板<50亿)  Task#67 rule2\n")
    w("条件: 前日跌>=12%%, 今日高开>=2%%, 流通市值<50亿, 非ST, 非涨停开盘\n")
    w("周期参数: %s\n" % arg)
    w("========================================================\n")

    print("加载创业板数据...")
    data = load_data(cur)
    print("创业板股票数: %d" % len(data))

    signals = find_signals(data, period)
    print("信号数: %d" % len(signals))

    # 逐信号明细: 前5日+当日+后5日 hour级OHLC
    w("\n########## 信号明细 (前5日+当日D+后5日 hour级OHLC) ##########\n")
    for s in signals:
        rows = s["rows"]
        i = s["idx"]
        w("\n[信号] %s %s  D=%s  前日跌%.2f%% 高开%.2f%% 流通市值%.1f亿\n" % (
            s["code"], s["name"], s["date"], s["y_drop"] * 100, s["gap"] * 100,
            s["mkt_cap"] / 1e8))
        lo_i = max(0, i - 5)
        hi_i = min(len(rows) - 1, i + 5)
        for j in range(lo_i, hi_i + 1):
            r = rows[j]
            tag = "D" if j == i else ("D%+d" % (j - i))
            w("  %-4s %s O%.2f H%.2f L%.2f C%.2f | h1 %.2f/%.2f/%.2f/%.2f  "
              "h2 %.2f/%.2f/%.2f/%.2f  h3 %.2f/%.2f/%.2f/%.2f  h4 %.2f/%.2f/%.2f/%.2f\n" % (
                tag, r["date"], r["open"], r["high"], r["low"], r["close"],
                r["hour1_open"] or 0, r["hour1_high"] or 0, r["hour1_low"] or 0, r["hour1_close"] or 0,
                r["hour2_open"] or 0, r["hour2_high"] or 0, r["hour2_low"] or 0, r["hour2_close"] or 0,
                r["hour3_open"] or 0, r["hour3_high"] or 0, r["hour3_low"] or 0, r["hour3_close"] or 0,
                r["hour4_open"] or 0, r["hour4_high"] or 0, r["hour4_low"] or 0, r["hour4_close"] or 0))

    # 汇总
    res = analyze(signals)
    best = write_summary(w, res, arg, signals)

    # 逐年稳定性 + 最终结论 (仅 all/year 有意义, month也给出全量逐年参考)
    if best:
        all_rets, yearly = write_yearly(w, data, best[0])
        w("\n--- 最终结论 ---\n")
        w("  最优配置: %s\n" % best[0])
        m = best[1]
        wr = best[2]
        # 预期月化 (单仓串行近似): 均收益 * 月均信号数
        months = set(s["date"][:7] for s in signals)
        nmonth = max(1, len(months))
        sig_per_month = res["n"] / nmonth
        monthly = m * sig_per_month
        w("  单笔均收益: %+.2f%%, 胜率: %.1f%%\n" % (m * 100, wr * 100))
        w("  月均信号: %.1f笔 (共%d个月)\n" % (sig_per_month, nmonth))
        w("  预期月化(单仓串行近似): %+.2f%%\n" % (monthly * 100))
        达标 = "YES" if (monthly >= 0.10 and wr >= 0.55) else "NO"
        w("  是否达标(月化10%%+/胜率55%%+): %s\n" % 达标)

    f.close()
    conn.close()
    # 控制台回显日志尾部摘要
    with open(LOG, "r", encoding="utf-8") as fr:
        lines = fr.readlines()
    print("".join(lines[-60:]))
    print("\n[日志已写入] %s (%d行)" % (LOG, len(lines)))


if __name__ == "__main__":
    main()
