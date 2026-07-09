#!/usr/bin/env python3
"""
Task #29: Signal D 随机选股消除逆向选择 + N=3 高频回测

假说: Signal D "全天跌+尾盘加速跳水" 统计显示 +0.90% mean, 55.5% win rate。
之前回测 "选跌最深的" 引入逆向选择(adverse selection)使 alpha 缩水。
本任务实验: RANDOM / MIDDLE / SHALLOWEST / DEEPEST 四种选股方式
            × 4 种过滤(无 / turn>3% / close>5 / 同时) = 16 组合
            
信号定义:
  T 日:
    hour4_close / hour3_close - 1 <= -0.03  (尾盘跳水 3%+)
    hour3_close < hour1_open                (全天跌趋势)
    close_rate ∈ [-5%, 0%)                  (当日跌但非跌停)
    排除 ST(code_name 含 ST 或 isST=1)、北交所(code 以 bj. 开头)

交易:
  T+1 hour1_open 买入, T+2 hour1_open 卖出
  T+2 停牌则向后顺延至首个可成交日的 hour1_open 卖出

回测:
  初始资金 100 万, N=3, 每只占当前净值的 1/N, 持有 1 天
  candidates 不足 3 只时, 有几只买几只(其余仓位空仓)

数据库: /home/AIWealth/data/stocks.db, 表 stock_kline
注: close_rate 字段单位为百分点 (-3.0 表示 -3%)
"""

import sqlite3
import random
import statistics
from collections import defaultdict
from datetime import datetime

DB_PATH = "/home/AIWealth/data/stocks.db"

NEEDED_COLS = [
    "code", "date", "code_name", "preclose", "open", "close", "close_rate",
    "high", "low", "turn", "isST",
    "hour1_open", "hour1_close",
    "hour3_open", "hour3_close",
    "hour4_open", "hour4_close", "hour4_low",
]


# ============== 工具 ==============

def is_excluded(row):
    code = row["code"]
    if code.startswith("bj."):
        return True
    name = (row.get("code_name") or "")
    if "ST" in name.upper():
        return True
    if row.get("isST"):
        return True
    return False


def load_dates(conn, start_date="2019-12-01", end_date="2026-01-01"):
    cur = conn.execute(
        "SELECT DISTINCT date FROM stock_kline WHERE date>=? AND date<? ORDER BY date",
        (start_date, end_date),
    )
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


# ============== 候选扫描 (符合最严信号定义,但保留 T+1/T+2 价格便于复用) ==============

def scan_candidates(conn, dates):
    """
    扫描所有 T 日满足 Signal D 严格定义的候选,并附带 T+1 hour1_open 买入价
    与 T+2(或顺延)hour1_open 卖出价。
    返回: dict[T_date] -> list of candidate dict
    """
    n = len(dates)
    cache = {}            # date -> day data
    by_T_date = defaultdict(list)

    def get_day(d):
        if d not in cache:
            cache[d] = fetch_day(conn, d)
        return cache[d]

    for i, d in enumerate(dates):
        if i + 2 >= n:
            break

        T_data = get_day(d)
        T1 = dates[i + 1]
        T1_data = get_day(T1)
        T2 = dates[i + 2]
        T2_data = get_day(T2)

        for code, row in T_data.items():
            h1o_T = row["hour1_open"]
            h3c = row["hour3_close"]
            h4c = row["hour4_close"]
            cr = row["close_rate"]
            if h1o_T is None or h3c is None or h4c is None or cr is None:
                continue
            if h3c <= 0 or h1o_T <= 0:
                continue
            # 信号 1: 尾盘跳水 3%+
            tail_dive = h4c / h3c - 1
            if tail_dive > -0.03:
                continue
            # 信号 2: 全天跌趋势
            if h3c >= h1o_T:
                continue
            # 信号 3: close_rate ∈ [-5%, 0%)  注: close_rate 单位是百分点
            if cr >= 0.0 or cr < -5.0:
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

            # T+2 卖出价(停牌顺延)
            t2 = T2_data.get(code)
            sell = None
            sell_date = None
            if t2 is not None and t2.get("hour1_open") is not None and t2["hour1_open"] > 0:
                sell = t2["hour1_open"]
                sell_date = T2
            else:
                for j in range(i + 3, min(i + 33, n)):
                    ed = dates[j]
                    rj = get_day(ed).get(code)
                    if rj is not None and rj.get("hour1_open") is not None and rj["hour1_open"] > 0:
                        sell = rj["hour1_open"]
                        sell_date = ed
                        break
            if sell is None:
                continue

            by_T_date[d].append({
                "T_date": d,
                "code": code,
                "code_name": row.get("code_name"),
                "tail_dive": tail_dive,
                "close_rate": cr,
                "turn": row.get("turn") or 0.0,
                "close": row.get("close") or 0.0,
                "buy_date": T1,
                "sell_date": sell_date,
                "buy": buy,
                "sell": sell,
                "ret": sell / buy - 1,
            })

        # 滚动释放老缓存
        if i - 2 >= 0:
            cache.pop(dates[i - 2], None)

    return by_T_date


# ============== 选股 & 过滤 ==============

def apply_filter(cands, mode):
    """mode in A/B/C/D"""
    out = []
    for c in cands:
        if mode in ("B", "D") and c["turn"] <= 3.0:
            continue
        if mode in ("C", "D") and c["close"] <= 5.0:
            continue
        out.append(c)
    return out


def select_picks(cands, method, k, rng):
    """选股: RANDOM / MIDDLE / SHALLOWEST / DEEPEST。k 为最大持仓数"""
    if not cands:
        return []
    if method == "RANDOM":
        # 用稳定排序保证可复现
        pool = sorted(cands, key=lambda x: x["code"])
        rng.shuffle(pool)
        return pool[:k]
    # 按 tail_dive 升序: 跌幅最深(最负)在最前
    sorted_c = sorted(cands, key=lambda x: x["tail_dive"])
    n = len(sorted_c)
    if method == "DEEPEST":
        return sorted_c[:k]
    if method == "SHALLOWEST":
        return sorted_c[-k:][::-1]
    if method == "MIDDLE":
        # 25% - 75% 分位区间
        lo = int(n * 0.25)
        hi = int(n * 0.75)
        if hi <= lo:
            mid = sorted_c[n // 2: n // 2 + 1]
        else:
            mid = sorted_c[lo:hi]
        # 中间区间内按"接近中位数"排序选 k 只
        m_idx = (lo + hi) / 2.0
        mid_sorted = sorted(mid, key=lambda x: abs(sorted_c.index(x) - m_idx))
        return mid_sorted[:k]
    raise ValueError(method)


# ============== N 仓位回测 (按日组合) ==============

def backtest_portfolio(by_T_date, dates_in_range, method, filter_mode, n_pos,
                       initial_cap=1_000_000.0, seed=42):
    """
    简化模型: 每日 T 选出 k 只, 每只投入 cash/n_pos, T+1 买 / T+2 卖, 收益结算。
    多日交易并行(允许同一时刻持有多笔, 因为信号每日触发, 不冲突资金 — 我们以"按笔
    成交时净值的 1/N"来计算每笔投入), 这是 N 仓位等权高频策略的标准近似。
    净值序列按 sell_date 累加收益。
    返回: trades(list), nav_series(dict date->nav), stats
    """
    rng = random.Random(seed)

    # 收集每日交易
    daily_trades = []  # list of (T_date, picks)
    for d in dates_in_range:
        cands = by_T_date.get(d, [])
        if not cands:
            continue
        cands_f = apply_filter(cands, filter_mode)
        if not cands_f:
            continue
        picks = select_picks(cands_f, method, n_pos, rng)
        if picks:
            daily_trades.append((d, picks))

    # 净值滚动: 每个 T 日开仓时,以当时 nav 的 1/n_pos 投入,
    # T+2 卖出日记入 PnL。我们以"按 sell_date 排序"逐笔结算。
    # 为简化资金占用(与原始 mean_ret 对照): 假设可以分散同时持有多笔,
    # nav_at_T 取信号触发当日的 NAV (按已结算交易计算)。

    # 1) 先按 sell_date 排序所有交易, 生成净值序列。
    all_trades = []
    for T_date, picks in daily_trades:
        for p in picks:
            all_trades.append({
                "T_date": T_date,
                "buy_date": p["buy_date"],
                "sell_date": p["sell_date"],
                "code": p["code"],
                "ret": p["ret"],
                "n_pos_at_T": n_pos,
            })

    # 按交易日推进净值: 每日先用"截至昨日已结算的 NAV"作为开仓基准
    nav = initial_cap
    # 我们要在 T 日决定每只投入多少 = nav_at_T / n_pos
    # 然后到 sell_date 才结算; 期间多笔开仓共用 nav_at_T 计算 — 这是高频等权的常用近似

    # 索引 trades by T_date 与 sell_date
    trades_by_T = defaultdict(list)
    for t in all_trades:
        trades_by_T[t["T_date"]].append(t)

    # 生成 dates_in_range 的"交易日序列"按时间推进
    nav_history = {}
    pending_settle = defaultdict(list)  # sell_date -> list trades(已写入 invest)

    for d in dates_in_range:
        # 先结算 sell_date == d 的交易
        if d in pending_settle:
            for t in pending_settle[d]:
                pnl = t["invest"] * t["ret"]
                nav += pnl
            pending_settle.pop(d)

        # 在 T == d 开仓: 用当前 nav 作为基准
        if d in trades_by_T:
            for t in trades_by_T[d]:
                t["invest"] = nav / t["n_pos_at_T"]
                pending_settle[t["sell_date"]].append(t)

        nav_history[d] = nav

    # 收尾结算未结清的(理论上 sell_date 都在 dates_in_range 内,但保险起见)
    for sd in sorted(pending_settle.keys()):
        for t in pending_settle[sd]:
            pnl = t["invest"] * t["ret"]
            nav += pnl
        nav_history[sd] = nav

    # 统计
    rets = [t["ret"] for t in all_trades]
    n = len(rets)
    if n == 0:
        return {
            "trades": 0, "mean_ret": 0.0, "win_rate": 0.0,
            "cagr": 0.0, "max_dd": 0.0, "final_nav": initial_cap,
            "nav_history": nav_history, "all_trades": all_trades,
        }
    mean_ret = sum(rets) / n * 100
    win_rate = sum(1 for r in rets if r > 0) / n * 100

    # CAGR & MaxDD
    sorted_dates = sorted(nav_history.keys())
    if sorted_dates:
        d0 = sorted_dates[0]
        d1 = sorted_dates[-1]
        nav_final = nav_history[d1]
        years = (datetime.strptime(d1, "%Y-%m-%d") - datetime.strptime(d0, "%Y-%m-%d")).days / 365.25
        if years > 0 and nav_final > 0:
            cagr = (nav_final / initial_cap) ** (1.0 / years) - 1.0
        else:
            cagr = 0.0
        # MaxDD
        peak = -1e18
        max_dd = 0.0
        for d in sorted_dates:
            v = nav_history[d]
            if v > peak:
                peak = v
            if peak > 0:
                dd = v / peak - 1.0
                if dd < max_dd:
                    max_dd = dd
    else:
        cagr = 0.0
        max_dd = 0.0
        nav_final = initial_cap

    return {
        "trades": n,
        "mean_ret": mean_ret,
        "win_rate": win_rate,
        "cagr": cagr * 100,
        "max_dd": max_dd * 100,
        "final_nav": nav_final,
        "nav_history": nav_history,
        "all_trades": all_trades,
    }


# ============== 主流程 ==============

def main():
    print("=" * 80)
    print("Task #29: Signal D 随机/中间/最浅/最深 选股 × 过滤变体 N=3 回测")
    print("=" * 80)

    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row

    print("[1/4] 加载交易日列表 ...")
    dates = load_dates(conn, "2019-12-01", "2026-01-01")
    print(f"      共 {len(dates)} 个交易日, {dates[0]} -> {dates[-1]}")

    # 选定回测年份范围
    backtest_dates = [d for d in dates if "2020-01-01" <= d <= "2025-12-31"]
    print(f"      回测窗口 {backtest_dates[0]} -> {backtest_dates[-1]}, {len(backtest_dates)} 天")

    print("[2/4] 扫描候选(Signal D 严格定义) ...")
    by_T_date = scan_candidates(conn, dates)
    total_cand = sum(len(v) for v in by_T_date.values())
    n_signal_days = sum(1 for d in backtest_dates if by_T_date.get(d))
    print(f"      候选总条数={total_cand}, 有信号交易日={n_signal_days}")
    daily_avg = total_cand / max(1, n_signal_days)
    print(f"      日均候选 (有信号日) = {daily_avg:.2f}")

    print("[3/4] 16 组合 N=3 回测 ...")
    methods = ["RANDOM", "MIDDLE", "SHALLOWEST", "DEEPEST"]
    filters = ["A", "B", "C", "D"]
    filter_desc = {"A": "无", "B": "turn>3%", "C": "close>5", "D": "turn>3%&close>5"}

    results = {}
    print()
    print(f"{'选股':<11} {'过滤':<18} {'总交易':>7} {'均收益':>9} {'胜率':>7} "
          f"{'6年CAGR':>9} {'MaxDD':>8}")
    print("-" * 78)
    for m in methods:
        for f in filters:
            res = backtest_portfolio(by_T_date, backtest_dates, m, f, n_pos=3)
            results[(m, f)] = res
            print(f"{m:<11} {filter_desc[f]:<18} {res['trades']:>7d} "
                  f"{res['mean_ret']:>+8.2f}% {res['win_rate']:>6.1f}% "
                  f"{res['cagr']:>+8.2f}% {res['max_dd']:>+7.2f}%")
        print("-" * 78)

    # 找最优 CAGR
    best = max(results.items(), key=lambda kv: kv[1]["cagr"])
    (best_m, best_f), best_res = best
    print()
    print("=" * 80)
    print(f"[4/4] 最优配置: {best_m} + 过滤={filter_desc[best_f]}  "
          f"CAGR={best_res['cagr']:+.2f}%  MaxDD={best_res['max_dd']:+.2f}%")
    print("=" * 80)

    # 逐年收益
    print("\n>>> 逐年收益(基于年末/年初 NAV 比值)")
    nav_history = best_res["nav_history"]
    sorted_dates = sorted(nav_history.keys())
    year_nav = {}
    for d in sorted_dates:
        y = d[:4]
        year_nav[y] = nav_history[d]  # 该年最后一个 NAV
    years_sorted = sorted(year_nav.keys())
    prev = 1_000_000.0
    print(f"  {'年份':<6} {'年末NAV':>15} {'年收益':>10}")
    for y in years_sorted:
        cur = year_nav[y]
        ret = (cur / prev - 1) * 100 if prev > 0 else 0.0
        print(f"  {y:<6} {cur:>15,.0f} {ret:>+9.2f}%")
        prev = cur

    # 日均候选数
    cand_filtered_days = 0
    cand_filtered_total = 0
    for d in backtest_dates:
        cs = by_T_date.get(d, [])
        cs_f = apply_filter(cs, best_f)
        if cs_f:
            cand_filtered_days += 1
            cand_filtered_total += len(cs_f)
    avg = cand_filtered_total / max(1, cand_filtered_days)
    print(f"\n>>> 最优过滤下日均候选数(有信号日): {avg:.2f}  "
          f"(有信号日 {cand_filtered_days} / 总交易日 {len(backtest_dates)})")

    # 若 CAGR > 50%, 测试 N=1 / N=2
    if best_res["cagr"] > 50.0:
        print()
        print("=" * 80)
        print(f"最优 CAGR > 50%, 额外测试 N=1 全仓 / N=2")
        print("=" * 80)
        for n_pos in (1, 2):
            res = backtest_portfolio(by_T_date, backtest_dates, best_m, best_f, n_pos=n_pos)
            print(f"  N={n_pos:<1}: 交易数={res['trades']}  均收益={res['mean_ret']:+.2f}%  "
                  f"胜率={res['win_rate']:.1f}%  CAGR={res['cagr']:+.2f}%  "
                  f"MaxDD={res['max_dd']:+.2f}%  期末NAV={res['final_nav']:,.0f}")
    else:
        print(f"\n最优 CAGR={best_res['cagr']:.2f}% 未达 50% 门槛, 跳过 N=1/N=2 测试。")

    conn.close()
    print("\n回测结束.")


if __name__ == "__main__":
    main()
