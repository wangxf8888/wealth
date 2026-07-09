#!/usr/bin/env python3
"""
Task #26: Signal D (尾盘跳水反弹) 扩展+过滤+完整 N=3/N=1 回测

Signal 基础：
  T 日 hour4_close / hour3_close - 1 <= -0.03 (尾盘跳水 3%)
  T 日 close_rate > -5% (非全天暴跌)
  T+1 hour1_open 买入, T+2 hour1_open 卖出
  排除 ST、北交所、停牌

Phase 1: 单维度变体扫描 (跳水深度/前3小时走势/换手/价格/尾盘企稳/全天跌幅)
Phase 2: 网格搜索最优过滤组合
Phase 3: 完整 N=3 回测 (2020-2025)
Phase 4: 仓位集中 N=1 回测

数据库: /home/AIWealth/data/stocks.db, 表 stock_kline
说明: 数据库 close_rate 字段单位为百分点 (-0.96 表示 -0.96%)
"""

import sqlite3
import statistics
from collections import defaultdict
from datetime import datetime
from itertools import product

DB_PATH = "/home/AIWealth/data/stocks.db"

NEEDED_COLS = [
    "code", "date", "code_name", "preclose", "open", "close", "close_rate",
    "high", "low", "turn", "isST",
    "hour1_open", "hour1_close",
    "hour3_close", "hour3_open",
    "hour4_open", "hour4_close", "hour4_low", "hour4_high",
]


# ============== 工具函数 ==============

def is_excluded(row):
    code = row["code"]
    if code.startswith("bj."):
        return True
    name = row.get("code_name") or ""
    if "ST" in name.upper():
        return True
    if row.get("isST"):
        return True
    return False


def fmt_stat(values, name=""):
    if not values:
        return f"{name:<28} N=    0  无样本"
    n = len(values)
    mean_pct = sum(values) / n * 100
    win = sum(1 for v in values if v > 0) / n * 100
    median_pct = statistics.median(values) * 100
    return (f"{name:<28} N={n:>5}  mean={mean_pct:+6.2f}%  "
            f"win={win:5.1f}%  med={median_pct:+6.2f}%")


def stat_dict(values):
    if not values:
        return {"N": 0, "mean": 0.0, "win": 0.0, "median": 0.0}
    n = len(values)
    return {
        "N": n,
        "mean": sum(values) / n * 100,
        "win": sum(1 for v in values if v > 0) / n * 100,
        "median": statistics.median(values) * 100,
    }


# ============== 数据加载 ==============

def load_dates(conn):
    cur = conn.execute("SELECT DISTINCT date FROM stock_kline ORDER BY date")
    return [r[0] for r in cur.fetchall()]


def fetch_day(conn, d):
    cols = ",".join(NEEDED_COLS)
    cur = conn.execute(f"SELECT {cols} FROM stock_kline WHERE date=?", (d,))
    out = {}
    description = [c[0] for c in cur.description]
    for r in cur.fetchall():
        rd = dict(zip(description, r))
        out[rd["code"]] = rd
    return out


# ============== 信号扫描 (一次扫全部, 后续过滤在内存中) ==============

def scan_all_signals(conn, dates, start_year, end_year):
    """
    扫描所有满足"基础最宽松条件"的候选, 一次性计算 T+1 买入/T+2 卖出收益。
    最宽松版本：
       - tail_dive <= -0.02
       - close_rate > -8 (即 close_rate%) — 涵盖所有 close_rate 变体
       - 排除 ST/北交所/停牌
    再在内存里做精细过滤。
    """
    signals = []
    cache = {}  # date -> {code -> row}
    n = len(dates)

    for i, d in enumerate(dates):
        if i + 2 >= n:
            break
        year = int(d[:4])
        if year < start_year or year > end_year:
            continue

        # 加载 T, T+1, T+2 三天数据
        for off in (0, 1, 2):
            ed = dates[i + off]
            if ed not in cache:
                cache[ed] = fetch_day(conn, ed)

        T_data = cache[dates[i]]
        T1_data = cache[dates[i + 1]]
        T2_data = cache[dates[i + 2]]

        for code, row in T_data.items():
            h3c = row["hour3_close"]
            h4c = row["hour4_close"]
            cr = row["close_rate"]
            if h3c is None or h4c is None or cr is None:
                continue
            if h3c <= 0:
                continue
            tail_dive = h4c / h3c - 1
            if tail_dive > -0.02:
                continue
            if cr <= -8.0:  # 最宽口径
                continue
            if is_excluded(row):
                continue

            # T+1 买入价
            t1 = T1_data.get(code)
            if t1 is None:
                continue
            buy = t1.get("hour1_open")
            if buy is None or buy <= 0:
                continue

            # T+2 卖出价；如果 T+2 停牌则向后查找首个可成交日
            t2 = T2_data.get(code)
            sell = None
            sell_date = None
            if t2 is not None and t2.get("hour1_open") is not None and t2["hour1_open"] > 0:
                sell = t2["hour1_open"]
                sell_date = dates[i + 2]
            else:
                for j in range(i + 3, min(i + 33, n)):
                    ed = dates[j]
                    if ed not in cache:
                        cache[ed] = fetch_day(conn, ed)
                    rj = cache[ed].get(code)
                    if rj is not None and rj.get("hour1_open") is not None and rj["hour1_open"] > 0:
                        sell = rj["hour1_open"]
                        sell_date = ed
                        break
            if sell is None:
                continue

            # T 日 hour1_open（前3小时走势判断要用到）
            h1o_T = row.get("hour1_open")
            h4_low = row.get("hour4_low")

            ret = sell / buy - 1
            signals.append({
                "T_date": d,
                "year": year,
                "code": code,
                "code_name": row.get("code_name"),
                "tail_dive": tail_dive,
                "close_rate": cr,
                "turn": row.get("turn") or 0.0,
                "close": row.get("close") or 0.0,
                "h1o_T": h1o_T,
                "h3c_T": h3c,
                "h4c_T": h4c,
                "h4_low_T": h4_low,
                "buy_date": dates[i + 1],
                "sell_date": sell_date,
                "buy": buy,
                "sell": sell,
                "ret": ret,
            })

        # 释放 i-1 缓存
        if i - 1 >= 0:
            cache.pop(dates[i - 1], None)

    return signals


# ============== Phase 1: 单维度变体扫描 ==============

def variant_check(sig, variant):
    """根据 variant 字典检查一个信号是否通过过滤. variant 包含若干键: tail_dive_range, close_rate_range, turn_min, close_min, stabilize, prev3h_trend"""
    td = sig["tail_dive"]
    cr = sig["close_rate"]
    turn = sig["turn"]
    close_p = sig["close"]
    h4c = sig["h4c_T"]
    h4_low = sig["h4_low_T"]
    h1o_T = sig["h1o_T"]
    h3c = sig["h3c_T"]

    # tail_dive 范围
    rng = variant.get("tail_dive_range")
    if rng is not None:
        lo, hi = rng  # 单位是分数 (-0.05, -0.04)
        # td 必须在 (lo, hi]: 例如 (-0.04, -0.03] 表示 -3%~-4%
        if not (lo < td <= hi):
            return False

    # close_rate 范围 (单位 %)
    crr = variant.get("close_rate_range")
    if crr is not None:
        lo, hi = crr
        if not (lo < cr <= hi):
            return False

    if variant.get("turn_min") is not None and turn < variant["turn_min"]:
        return False

    if variant.get("close_min") is not None and close_p < variant["close_min"]:
        return False

    stab = variant.get("stabilize")
    if stab == "above_low":
        if h4_low is None or h4c is None:
            return False
        if h4c <= h4_low:
            return False
    elif stab == "rebound_0p5":
        if h4_low is None or h4c is None or h4_low <= 0:
            return False
        if h4c / h4_low - 1 < 0.005:
            return False

    trend = variant.get("prev3h_trend")
    if trend == "up":
        if h1o_T is None or h3c is None:
            return False
        if h3c <= h1o_T:
            return False
    elif trend == "down":
        if h1o_T is None or h3c is None:
            return False
        if h3c >= h1o_T:
            return False

    return True


def phase1_variants(signals):
    """对每个测试条件打印 N, mean, win"""
    print("\n" + "=" * 100)
    print("  Phase 1: 单维度变体扫描 (基础: tail_dive<=-3% & close_rate>-5%)")
    print("=" * 100)

    # 基础信号（原始定义）
    base = [s for s in signals
            if s["tail_dive"] <= -0.03 and s["close_rate"] > -5.0]
    base_rets = [s["ret"] for s in base]
    print("\n--- 基础信号 (原始定义) ---")
    print(fmt_stat(base_rets, "[基础] td<=-3% & cr>-5%"))

    # A. 跳水深度分段
    print("\n--- A. 跳水深度分段 (close_rate > -5%) ---")
    seg_a = [
        ("td: -3% < td <= -2%  (放宽)", -0.03, -0.02),
        ("td: -4% < td <= -3%  (原始)", -0.04, -0.03),
        ("td: -5% < td <= -4%        ", -0.05, -0.04),
        ("td: td <= -5%        (极端)", -1.00, -0.05),
    ]
    for label, lo, hi in seg_a:
        sub = [s["ret"] for s in signals
               if lo < s["tail_dive"] <= hi and s["close_rate"] > -5.0]
        print(fmt_stat(sub, label))

    # B. 当日前3小时走势 (在原始基础信号上)
    print("\n--- B. T 日前3小时走势 (在 td<=-3% & cr>-5% 之上) ---")
    up = [s["ret"] for s in base if s["h1o_T"] and s["h3c_T"] and s["h3c_T"] > s["h1o_T"]]
    down = [s["ret"] for s in base if s["h1o_T"] and s["h3c_T"] and s["h3c_T"] < s["h1o_T"]]
    print(fmt_stat(up, "h3c > h1o (前3h涨,尾盘跳)"))
    print(fmt_stat(down, "h3c < h1o (全天跌,尾盘加速)"))

    # C. 换手率
    print("\n--- C. 换手率过滤 (在原始基础信号上) ---")
    for tm in (3, 5, 8):
        sub = [s["ret"] for s in base if s["turn"] >= tm]
        print(fmt_stat(sub, f"turn >= {tm}%"))

    # D. 价格
    print("\n--- D. 价格过滤 (在原始基础信号上) ---")
    for cm in (5, 10):
        sub = [s["ret"] for s in base if s["close"] >= cm]
        print(fmt_stat(sub, f"close >= {cm}元"))

    # E. 尾盘企稳
    print("\n--- E. 尾盘企稳 (在原始基础信号上) ---")
    e1 = [s["ret"] for s in base
          if s["h4_low_T"] is not None and s["h4c_T"] is not None and s["h4c_T"] > s["h4_low_T"]]
    print(fmt_stat(e1, "h4_close > h4_low"))
    e2 = [s["ret"] for s in base
          if s["h4_low_T"] and s["h4c_T"] and s["h4_low_T"] > 0
          and s["h4c_T"] / s["h4_low_T"] - 1 >= 0.005]
    print(fmt_stat(e2, "h4_close/h4_low-1 >= 0.5%"))

    # F. 全天跌幅
    print("\n--- F. 全天跌幅放宽/收紧 (td<=-3%) ---")
    for label, lo, hi in [
        ("close_rate > -3% (加严)", -3.0, 100.0),
        ("close_rate > -5% (原始)", -5.0, 100.0),
        ("close_rate > -8% (放宽)", -8.0, 100.0),
    ]:
        sub = [s["ret"] for s in signals
               if s["tail_dive"] <= -0.03 and lo < s["close_rate"] <= hi]
        print(fmt_stat(sub, label))

    return base


# ============== Phase 2: 网格搜索最优过滤组合 ==============

def phase2_grid_search(signals, dates_in_range):
    print("\n" + "=" * 100)
    print("  Phase 2: 最优过滤组合网格搜索 (要求 mean>+0.5%, 日均>=3, 年稳定>=5/6)")
    print("=" * 100)

    # 网格定义 (以原始 td<=-3% & cr>-5% 为骨架, 再做精细过滤)
    grid_tail = [
        ("td<=-3%", -1.00, -0.03),
        ("td<=-4%", -1.00, -0.04),
        ("-4%<td<=-3%", -0.04, -0.03),
    ]
    grid_cr = [
        ("cr>-5%", -5.0, 100.0),
        ("cr>-3%", -3.0, 100.0),
        ("-5%<cr<=-1%", -5.0, -1.0),
        ("0%<cr", 0.0, 100.0),
    ]
    grid_turn = [("turn>=0", 0), ("turn>=3%", 3), ("turn>=5%", 5)]
    grid_close = [("close>=0", 0), ("close>=5", 5), ("close>=10", 10)]
    grid_stab = [("stab=ANY", None), ("h4c>h4_low", "above_low")]
    grid_trend = [("trend=ANY", None), ("h3c>h1o", "up")]

    n_dates = len(dates_in_range)
    by_year_total = defaultdict(int)
    for d in dates_in_range:
        by_year_total[int(d[:4])] += 1

    candidates = []
    for (lt, td_lo, td_hi), (lc, cr_lo, cr_hi), (lturn, tm), (lclose, cm), (lstab, stab), (ltrend, trend) in product(
            grid_tail, grid_cr, grid_turn, grid_close, grid_stab, grid_trend):
        var = {
            "tail_dive_range": (td_lo, td_hi),
            "close_rate_range": (cr_lo, cr_hi),
            "turn_min": tm,
            "close_min": cm,
            "stabilize": stab,
            "prev3h_trend": trend,
        }
        passed = [s for s in signals if variant_check(s, var)]
        N = len(passed)
        if N < 100:
            continue
        rets = [s["ret"] for s in passed]
        mean = sum(rets) / N * 100
        win = sum(1 for r in rets if r > 0) / N * 100
        per_day = N / max(n_dates, 1)
        # 年稳定性
        by_year = defaultdict(list)
        for s in passed:
            by_year[s["year"]].append(s["ret"])
        years_pos = sum(1 for y, rs in by_year.items() if rs and (sum(rs) / len(rs)) > 0)
        n_years = len(by_year)
        # 过滤
        if mean < 0.5 or per_day < 3 or years_pos < max(1, n_years - 1):
            continue
        candidates.append({
            "label": f"{lt} & {lc} & {lturn} & {lclose} & {lstab} & {ltrend}",
            "var": var, "N": N, "mean": mean, "win": win, "per_day": per_day,
            "years_pos": years_pos, "n_years": n_years,
            "by_year": {y: stat_dict(rs) for y, rs in by_year.items()},
        })

    candidates.sort(key=lambda c: (c["mean"] * (c["N"] ** 0.5)), reverse=True)

    print(f"\n共找到 {len(candidates)} 组合通过门槛(mean>0.5%, 日均>=3, 年正≥总-1)")
    print("\nTop 15 (按 mean*sqrt(N) 排序):")
    print("-" * 100)
    print(f"{'#':>2} {'label':<70} {'N':>6} {'mean%':>7} {'win%':>6} {'/day':>6} {'年正':>5}")
    print("-" * 100)
    for i, c in enumerate(candidates[:15], 1):
        print(f"{i:>2} {c['label']:<70} {c['N']:>6} {c['mean']:>+7.2f} "
              f"{c['win']:>6.1f} {c['per_day']:>6.1f} {c['years_pos']}/{c['n_years']}")

    if not candidates:
        print("\n[警告] 无组合满足门槛,fallback 至原始信号")
        return None

    best = candidates[0]
    print("\n" + "-" * 100)
    print(f"[最优组合] {best['label']}")
    print(f"  N={best['N']}  mean={best['mean']:+.2f}%  win={best['win']:.1f}%  /day={best['per_day']:.2f}  年正={best['years_pos']}/{best['n_years']}")
    print("\n  逐年表现:")
    for y in sorted(best["by_year"].keys()):
        s = best["by_year"][y]
        print(f"    {y}: N={s['N']:>5}  mean={s['mean']:+6.2f}%  win={s['win']:5.1f}%")

    return best


# ============== Phase 3 & 4: 完整回测 ==============

def run_backtest(signals_filtered, dates, N, capital=1_000_000.0):
    """
    每日 d 早盘:
      1) 卖出: 持仓中 sell_date == d 的, 立即视为 hour1_open 卖出, 释放 slot
      2) 买入: 用 dates[i-1] 作为 T 日的候选信号, 排序后按可用 slot 数量买入

    每个 slot 维护独立现金。slot 的累积收益 = 各次交易 (1+ret) 连乘。
    停牌 sell_date 已在 signals 中处理为复牌首日。

    返回:
      equity_curve: list of (date, total_value)
      trades: list of trade dicts
      slot_busy_until: not needed for output
    """
    # 按 T_date 索引信号
    by_t_date = defaultdict(list)
    for s in signals_filtered:
        by_t_date[s["T_date"]].append(s)
    # 每天按 tail_dive 升序 (跌得最深的优先)
    for k in by_t_date:
        by_t_date[k].sort(key=lambda x: x["tail_dive"])

    slots = [{"cash": capital / N, "busy_until": None,
              "current_code": None, "current_ret": 0.0} for _ in range(N)]

    equity_curve = []
    trades = []

    for i, d in enumerate(dates):
        # 1) 卖出阶段: sell_date == d 的 slot, 应用 ret 并释放
        for s in slots:
            if s["busy_until"] is not None and s["busy_until"] == d:
                s["cash"] = s["cash"] * (1.0 + s["current_ret"])
                s["busy_until"] = None
                s["current_code"] = None
                s["current_ret"] = 0.0

        # 2) 买入阶段: 用昨日 T_date 的候选填充空 slot
        if i >= 1:
            prev_d = dates[i - 1]
            cands = by_t_date.get(prev_d, [])
            free_slots = [s for s in slots if s["busy_until"] is None]
            for slot, cand in zip(free_slots, cands[:len(free_slots)]):
                slot["busy_until"] = cand["sell_date"]
                slot["current_code"] = cand["code"]
                slot["current_ret"] = cand["ret"]
                trades.append({
                    "buy_date": cand["buy_date"], "sell_date": cand["sell_date"],
                    "code": cand["code"], "code_name": cand["code_name"],
                    "buy": cand["buy"], "sell": cand["sell"], "ret": cand["ret"],
                })

        # 3) 净值: 简化为各 slot cash 之和 (持仓中段不做盘中 mark-to-market)
        total = sum(s["cash"] for s in slots)
        equity_curve.append((d, total))

    # 收尾: 仍有未平仓的 slot, 强制结算
    for s in slots:
        if s["busy_until"] is not None:
            s["cash"] = s["cash"] * (1.0 + s["current_ret"])
            s["busy_until"] = None
            s["current_ret"] = 0.0
    if equity_curve:
        last_d = equity_curve[-1][0]
        equity_curve[-1] = (last_d, sum(s["cash"] for s in slots))

    return equity_curve, trades


def report_backtest(name, equity_curve, trades, capital):
    if not equity_curve:
        print(f"[{name}] 无数据")
        return
    final = equity_curve[-1][1]
    total_ret = final / capital - 1

    # 逐年收益
    yearly = defaultdict(list)
    for d, v in equity_curve:
        yearly[d[:4]].append((d, v))

    # 每年起止净值
    year_returns = {}
    prev_end = capital
    for y in sorted(yearly.keys()):
        start_v = prev_end
        end_v = yearly[y][-1][1]
        year_returns[y] = end_v / start_v - 1
        prev_end = end_v

    # CAGR
    n_years = len(year_returns)
    cagr = (final / capital) ** (1.0 / n_years) - 1 if n_years > 0 else 0.0

    # 最大回撤
    peak = equity_curve[0][1]
    max_dd = 0.0
    for _, v in equity_curve:
        if v > peak:
            peak = v
        dd = v / peak - 1
        if dd < max_dd:
            max_dd = dd

    # 交易统计
    n_trades = len(trades)
    if n_trades > 0:
        avg_ret = sum(t["ret"] for t in trades) / n_trades * 100
        win_rate = sum(1 for t in trades if t["ret"] > 0) / n_trades * 100
    else:
        avg_ret = 0.0
        win_rate = 0.0

    print(f"\n{'=' * 100}")
    print(f"  [{name}] 回测结果")
    print("=" * 100)
    print(f"  初始资金: {capital:>15,.0f}")
    print(f"  最终净值: {final:>15,.0f}")
    print(f"  累计收益: {total_ret:>+15.2%}")
    print(f"  CAGR    : {cagr:>+15.2%}")
    print(f"  最大回撤: {max_dd:>+15.2%}")
    print(f"  总交易数: {n_trades}")
    print(f"  平均单笔: {avg_ret:>+15.2f}%")
    print(f"  胜率    : {win_rate:>15.1f}%")
    print()
    print("  逐年收益率:")
    for y in sorted(year_returns.keys()):
        print(f"    {y}: {year_returns[y]:>+8.2%}")


# ============== 主流程 ==============

def main():
    t0 = datetime.now()
    print("=" * 100)
    print("  Task #26: Signal D 尾盘跳水反弹 - 扩展+过滤+完整回测")
    print(f"  开始时间: {t0:%Y-%m-%d %H:%M:%S}")
    print("=" * 100)

    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row

    # 加载交易日 (2020-2025)
    all_dates = load_dates(conn)
    dates = [d for d in all_dates if "2020-01-01" <= d <= "2025-12-31"]
    print(f"\n[1] 交易日加载: {len(dates)} 天 ({dates[0]} ~ {dates[-1]})")

    # 扫描信号
    print(f"\n[2] 扫描全部宽口径信号 ...")
    signals = scan_all_signals(conn, all_dates, 2020, 2025)
    # 仅保留落在 2020-2025 区间内的 T_date
    signals = [s for s in signals if "2020-01-01" <= s["T_date"] <= "2025-12-31"]
    print(f"    宽口径候选数(td<=-2% & cr>-8%): {len(signals)}")
    dt = (datetime.now() - t0).total_seconds()
    print(f"    [耗时 {dt:.1f}s]")

    # Phase 1
    base = phase1_variants(signals)

    # Phase 2
    best = phase2_grid_search(signals, dates)

    # 选择最优过滤变体: 优先 best, 否则用基础(td<=-3% & cr>-5%)
    if best is not None:
        final_var = best["var"]
        final_label = best["label"]
        print(f"\n[Phase 3 输入] 使用最优过滤组合: {final_label}")
        chosen = [s for s in signals if variant_check(s, final_var)]
    else:
        print("\n[Phase 3 输入] 使用基础信号 td<=-3% & cr>-5%")
        chosen = base
        final_label = "td<=-3% & cr>-5% (基础)"

    # 同时也跑基础信号的 N=3 作对照
    print("\n" + "#" * 100)
    print(f"# Phase 3: 完整 N=3 回测 (筛选: {final_label})")
    print("#" * 100)
    eq_n3, tr_n3 = run_backtest(chosen, dates, N=3, capital=1_000_000.0)
    report_backtest(f"N=3 / {final_label}", eq_n3, tr_n3, 1_000_000.0)

    # 基础信号 N=3 对照 (若 best 与基础不同)
    if best is not None:
        print("\n" + "-" * 100)
        print("[对照] 基础信号 td<=-3% & cr>-5% N=3:")
        eq_b3, tr_b3 = run_backtest(base, dates, N=3, capital=1_000_000.0)
        report_backtest("N=3 / 基础信号", eq_b3, tr_b3, 1_000_000.0)

    # Phase 4: N=1 集中
    print("\n" + "#" * 100)
    print(f"# Phase 4: 仓位集中 N=1 回测 (筛选: {final_label})")
    print("#" * 100)
    eq_n1, tr_n1 = run_backtest(chosen, dates, N=1, capital=1_000_000.0)
    report_backtest(f"N=1 / {final_label}", eq_n1, tr_n1, 1_000_000.0)

    # 也跑 N=2 作中间档
    print("\n" + "#" * 100)
    print(f"# 附加: N=2 回测 (筛选: {final_label})")
    print("#" * 100)
    eq_n2, tr_n2 = run_backtest(chosen, dates, N=2, capital=1_000_000.0)
    report_backtest(f"N=2 / {final_label}", eq_n2, tr_n2, 1_000_000.0)

    dt = (datetime.now() - t0).total_seconds()
    print(f"\n{'=' * 100}")
    print(f"  全部完成 / 总耗时 {dt:.1f}s")
    print(f"{'=' * 100}")
    conn.close()


if __name__ == "__main__":
    main()
