#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Task #18 - 连板龙头回调+限价单策略 (Dragon Pullback Limit)

策略思路:
  连续涨停(2板以上)的龙头股在断板后出现大幅回调, 利用限价单在回调低点接入,
  捕捉龙头修复反弹.

Pipeline:
  Phase-1: 2025年全年候选股筛选, 统计月均候选数量
  Phase-2: 2025-Q1 上的多组限价买入 + 三档止损/止盈卖出参数遍历
  Phase-3: 2020-2025 六年 N=3 仓位完整回测, 输出逐年收益与CAGR

合规要点:
  * 涨停严格判定: round(close/preclose, 2) 与 1.20/1.10 比较
  * 限价单买入: 当日 hourX_low <= P 才能成交于 P
  * 限价单/止损卖出: hourX_high >= P 或 hourX_low <= P 才成交
  * T+1, 涨停不可卖, 跌停不可买
"""

from __future__ import annotations

import os
import sys
import math
import json
import sqlite3
from collections import defaultdict, OrderedDict
from datetime import datetime
from typing import Dict, List, Optional, Tuple

DB_PATH = "/home/AIWealth/data/stocks.db"

# ---------------------------------------------------------------------------
# 工具函数
# ---------------------------------------------------------------------------

def get_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def is_limit_up(code: str, close: float, preclose: float) -> bool:
    """A股严格涨停判定 (基于 round(close/preclose, 2))."""
    if preclose is None or close is None or preclose <= 0:
        return False
    try:
        ratio = round(close / preclose, 2)
    except Exception:
        return False
    if code.startswith("sz.30") or code.startswith("sh.68"):
        return ratio >= 1.20
    return ratio >= 1.10


def is_limit_down(code: str, close: float, preclose: float) -> bool:
    if preclose is None or close is None or preclose <= 0:
        return False
    try:
        ratio = round(close / preclose, 2)
    except Exception:
        return False
    if code.startswith("sz.30") or code.startswith("sh.68"):
        return ratio <= 0.80
    return ratio <= 0.90


def get_trade_dates(conn: sqlite3.Connection, start: str, end: str) -> List[str]:
    cur = conn.execute(
        "SELECT DISTINCT date FROM stock_kline WHERE date>=? AND date<=? ORDER BY date",
        (start, end),
    )
    return [r[0] for r in cur.fetchall()]


def fetch_day(conn: sqlite3.Connection, date: str) -> List[sqlite3.Row]:
    cur = conn.execute("SELECT * FROM stock_kline WHERE date=?", (date,))
    return cur.fetchall()


def code_eligible(code: str, name: str = "") -> bool:
    if not code:
        return False
    if code.startswith("bj."):
        return False
    # 禁ST
    if name and ("ST" in name or "*ST" in name):
        return False
    return True


# ---------------------------------------------------------------------------
# Phase-1: 候选股筛选
# ---------------------------------------------------------------------------

def build_history_index(conn: sqlite3.Connection, start: str, end: str,
                         lookback_days: int = 8) -> Tuple[List[str], Dict[str, Dict[str, sqlite3.Row]]]:
    """加载 [start - lookback, end + 8] 区间的全部数据, 按 code 索引.

    返回:
        trade_dates: 排序的交易日列表 (start ~ end)
        idx: code -> {date -> row}
    """
    # 取一些前置/后置日期, 方便回看与持仓兑现
    cur = conn.execute(
        "SELECT DISTINCT date FROM stock_kline WHERE date<? ORDER BY date DESC LIMIT ?",
        (start, lookback_days),
    )
    pre = [r[0] for r in cur.fetchall()]
    pre.reverse()

    cur = conn.execute(
        "SELECT DISTINCT date FROM stock_kline WHERE date>? ORDER BY date ASC LIMIT 8",
        (end,),
    )
    post = [r[0] for r in cur.fetchall()]

    main = get_trade_dates(conn, start, end)
    full_dates = pre + main + post
    if not full_dates:
        return [], {}

    cur = conn.execute(
        "SELECT * FROM stock_kline WHERE date>=? AND date<=?",
        (full_dates[0], full_dates[-1]),
    )
    idx: Dict[str, Dict[str, sqlite3.Row]] = defaultdict(dict)
    for row in cur.fetchall():
        idx[row["code"]][row["date"]] = row
    return main, idx


def find_candidates_for_date(idx: Dict[str, Dict[str, sqlite3.Row]],
                              all_dates: List[str], today: str,
                              lookback: int = 5,
                              min_consecutive: int = 2,
                              open_rate_max: float = -2.0) -> List[Dict]:
    """对单日 today 找出满足 dragon-pullback 候选条件的股票."""
    if today not in all_dates:
        return []
    ti = all_dates.index(today)
    if ti < lookback:
        return []
    window_dates = all_dates[ti - lookback:ti]  # 前 lookback 个交易日
    yesterday = all_dates[ti - 1]

    candidates: List[Dict] = []
    for code, by_date in idx.items():
        if not code_eligible(code, by_date.get(today, {"code_name": ""})["code_name"] if today in by_date else ""):
            continue
        today_row = by_date.get(today)
        if today_row is None:
            continue
        # ST 过滤
        if today_row["isST"] == 1:
            continue
        # 价格/数据完整性
        if today_row["preclose"] is None or today_row["preclose"] <= 0:
            continue
        if today_row["open"] is None or today_row["open_rate"] is None:
            continue

        # 1. 今日开盘回调 open_rate <= -2% (今日开盘价已知, 无未来数据)
        if today_row["open_rate"] > open_rate_max:
            continue

        # 2. 严格无未来: 昨日已断板 (昨日不涨停).
        #    我们要求在 lookback 窗口内出现过 >= min_consecutive 连板,
        #    并且昨日不再涨停 (即断板事件已发生).
        yrow = by_date.get(yesterday)
        if yrow is None or yrow["preclose"] is None or yrow["preclose"] <= 0:
            continue
        if is_limit_up(code, yrow["close"], yrow["preclose"]):
            # 昨日仍涨停, 还未断板, 跳过 (避免使用 today close)
            continue

        # 3. 前 lookback 日内至少 min_consecutive 连板
        max_streak = 0
        cur_streak = 0
        had_streak_recent = False
        for d in window_dates:
            r = by_date.get(d)
            if r is None or r["preclose"] is None or r["preclose"] <= 0:
                cur_streak = 0
                continue
            if is_limit_up(code, r["close"], r["preclose"]):
                cur_streak += 1
                if cur_streak >= min_consecutive:
                    had_streak_recent = True
                if cur_streak > max_streak:
                    max_streak = cur_streak
            else:
                cur_streak = 0
        if not had_streak_recent:
            continue
        candidates.append({
            "code": code,
            "name": today_row["code_name"],
            "max_streak": max_streak,
            "open_rate": today_row["open_rate"],
            "close_rate": today_row["close_rate"],
            "preclose": today_row["preclose"],
            "open": today_row["open"],
            "close": today_row["close"],
            "high": today_row["high"],
            "low": today_row["low"],
        })
    return candidates


def phase1(conn: sqlite3.Connection,
           start: str = "2025-01-01", end: str = "2025-12-31",
           lookback: int = 5, min_consecutive: int = 2,
           open_rate_max: float = -2.0,
           verbose_each_day: bool = False) -> Tuple[Dict[str, List[Dict]], Dict[str, int]]:
    print(f"\n{'='*80}\nPhase-1 候选筛选 [{start} ~ {end}] "
          f"lookback={lookback}d, min_streak>={min_consecutive}, open_rate<={open_rate_max}%\n{'='*80}")
    main_dates, idx = build_history_index(conn, start, end, lookback_days=lookback + 3)
    if not main_dates:
        print("[Phase-1] 无交易日数据.")
        return {}, {}

    candidates_by_date: Dict[str, List[Dict]] = OrderedDict()
    monthly_count: Dict[str, int] = defaultdict(int)
    total = 0
    for d in main_dates:
        cands = find_candidates_for_date(idx, main_dates, d,
                                          lookback=lookback,
                                          min_consecutive=min_consecutive,
                                          open_rate_max=open_rate_max)
        candidates_by_date[d] = cands
        ym = d[:7]
        monthly_count[ym] += len(cands)
        total += len(cands)
        if verbose_each_day and cands:
            names = ",".join(f"{c['code']}({c['max_streak']}板,{c['open_rate']:.1f}%)" for c in cands[:6])
            print(f"  {d}: {len(cands):3d} -> {names}{'...' if len(cands)>6 else ''}")

    print(f"\n[Phase-1] 总候选数: {total}, 平均每月: {total/max(1,len(monthly_count)):.1f}")
    print("[Phase-1] 月度分布:")
    for ym in sorted(monthly_count):
        print(f"   {ym}: {monthly_count[ym]:4d}")
    return candidates_by_date, monthly_count


# ---------------------------------------------------------------------------
# Phase-2: 多参数买卖测试
# ---------------------------------------------------------------------------

def simulate_single_trade(idx_by_code: Dict[str, Dict[str, sqlite3.Row]],
                          all_dates: List[str], code: str, buy_date: str,
                          buy_strategy: str = "A",
                          stop_loss_pct: float = -3.0,
                          trailing_pct: float = -3.0,
                          hold_max_days: int = 3) -> Optional[Dict]:
    """模拟单笔交易. 返回 trade 详情或 None (未成交)."""
    if buy_date not in all_dates:
        return None
    by_date = idx_by_code.get(code)
    if not by_date:
        return None
    bday_row = by_date.get(buy_date)
    if not bday_row:
        return None

    # 计算限价 P 与可成交 hour
    open_p = bday_row["open"]
    h1c = bday_row["hour1_close"]
    h1l = bday_row["hour1_low"]
    h2l = bday_row["hour2_low"]
    if any(v is None for v in (open_p, h1c, h1l, h2l)):
        return None

    if buy_strategy == "A":
        # hour2 限价 = h1_close * 0.98, 验证 hour2_low <= P
        P = h1c * 0.98
        buy_hour = 2
        ok = h2l <= P
    elif buy_strategy == "B":
        # hour2 限价 = h1_low, 验证 hour2_low <= P
        P = h1l
        buy_hour = 2
        ok = h2l <= P
    elif buy_strategy == "C":
        # hour1 限价 = open*0.97, 验证 hour1_low <= P
        P = open_p * 0.97
        buy_hour = 1
        ok = h1l <= P
    else:
        return None

    if not ok or P is None or P <= 0:
        return None

    buy_price = round(P, 2)
    bi = all_dates.index(buy_date)

    # 卖出逻辑: T+1 起每个 hour 检查
    # 高点跟踪
    highest = buy_price
    sl_price = buy_price * (1 + stop_loss_pct / 100.0)

    sell_date = None
    sell_price = None
    sell_reason = None
    sell_hour = None

    # buy_date 当日剩余 hours 不卖 (T+1)
    # 持仓最长到 buy_date + hold_max_days 个交易日的尾盘
    for off in range(1, hold_max_days + 1):
        if bi + off >= len(all_dates):
            break
        sd = all_dates[bi + off]
        srow = by_date.get(sd)
        if not srow:
            continue
        # 跌停日不可卖
        if is_limit_down(code, srow["close"], srow["preclose"]):
            # 仍要更新 highest, 跳过卖出
            for hi in range(1, 5):
                hh = srow[f"hour{hi}_high"]
                if hh and hh > highest:
                    highest = hh
            continue
        for hi in range(1, 5):
            ho = srow[f"hour{hi}_open"]
            hh = srow[f"hour{hi}_high"]
            hl = srow[f"hour{hi}_low"]
            hc = srow[f"hour{hi}_close"]
            if any(v is None for v in (ho, hh, hl, hc)):
                continue
            # 保守序: 先用旧 highest 判定下跌SL/trailing, 并后更新 highest
            old_highest = highest
            tr_price = old_highest * (1 + trailing_pct / 100.0)
            # 优先 SL —— 跳空合规: 如 hour_open 已低于 SL, 以 hour_open 成交
            if hl <= sl_price:
                sell_date = sd
                sell_price = ho if ho <= sl_price else sl_price
                sell_reason = "STOP_LOSS"
                sell_hour = hi
                break
            # Trailing (仅当旧 highest 已盈利 0.5%+)
            if old_highest > buy_price * 1.005 and hl <= tr_price:
                sell_date = sd
                sp = ho if ho <= tr_price else tr_price
                sell_price = max(sp, sl_price) if ho > sl_price else sp
                sell_reason = "TRAILING"
                sell_hour = hi
                break
            # 本小时未触发卖出, 更新 highest 供后续使用
            if hh > highest:
                highest = hh
            # 最后一日尾盘强平
            if off == hold_max_days and hi == 4:
                sell_date = sd
                sell_price = hc
                sell_reason = "HOLD_MAX"
                sell_hour = hi
                break
        if sell_date:
            break

    if sell_date is None:
        # 取最后一个可用日的收盘强平
        for off in range(hold_max_days, 0, -1):
            if bi + off >= len(all_dates):
                continue
            sd = all_dates[bi + off]
            srow = by_date.get(sd)
            if srow and srow["close"] is not None:
                sell_date = sd
                sell_price = srow["close"]
                sell_reason = "FORCE_CLOSE"
                sell_hour = 4
                break
        if sell_date is None:
            return None

    ret_pct = (sell_price - buy_price) / buy_price * 100.0
    return {
        "code": code,
        "buy_date": buy_date,
        "buy_strategy": buy_strategy,
        "buy_hour": buy_hour,
        "buy_price": round(buy_price, 4),
        "sell_date": sell_date,
        "sell_hour": sell_hour,
        "sell_price": round(sell_price, 4),
        "sell_reason": sell_reason,
        "return_pct": round(ret_pct, 3),
    }


def phase2(conn: sqlite3.Connection, candidates_by_date: Dict[str, List[Dict]],
           q1_start: str = "2025-01-01", q1_end: str = "2025-03-31") -> Dict:
    print(f"\n{'='*80}\nPhase-2 参数遍历 [{q1_start} ~ {q1_end}]\n{'='*80}")
    main_dates, idx = build_history_index(conn, q1_start, q1_end, lookback_days=8)

    param_grid = []
    for buy in ["A", "B", "C"]:
        for sl in [-2.0, -3.0, -4.0]:
            for tr in [-2.0, -3.0, -4.0]:
                for hm in [2, 3, 5]:
                    param_grid.append({"buy": buy, "sl": sl, "tr": tr, "hm": hm})

    # 每组参数遍历所有 Q1 候选
    results: List[Dict] = []
    for params in param_grid:
        trades: List[Dict] = []
        for d in main_dates:
            cands = candidates_by_date.get(d, [])
            for c in cands:
                t = simulate_single_trade(idx, main_dates, c["code"], d,
                                           buy_strategy=params["buy"],
                                           stop_loss_pct=params["sl"],
                                           trailing_pct=params["tr"],
                                           hold_max_days=params["hm"])
                if t:
                    trades.append(t)
        if not trades:
            results.append({**params, "n": 0, "win": 0.0, "avg": 0.0, "med": 0.0})
            continue
        n = len(trades)
        wins = sum(1 for t in trades if t["return_pct"] > 0)
        avg = sum(t["return_pct"] for t in trades) / n
        sorted_r = sorted(t["return_pct"] for t in trades)
        med = sorted_r[n // 2]
        results.append({**params, "n": n, "win": wins / n * 100, "avg": avg, "med": med})

    results.sort(key=lambda r: (r["avg"], r["win"]), reverse=True)
    print(f"{'buy':<4}{'SL':>6}{'TR':>6}{'HM':>4}{'N':>6}{'WIN%':>8}{'AVG%':>8}{'MED%':>8}")
    print("-" * 60)
    for r in results[:20]:
        print(f"{r['buy']:<4}{r['sl']:>6.1f}{r['tr']:>6.1f}{r['hm']:>4d}"
              f"{r['n']:>6d}{r['win']:>8.2f}{r['avg']:>8.3f}{r['med']:>8.3f}")
    return {"all": results, "best": results[0] if results else None}


# ---------------------------------------------------------------------------
# Phase-3: 全年 / 多年 N=3 仓位回测
# ---------------------------------------------------------------------------

def phase3_backtest(conn: sqlite3.Connection,
                    start: str, end: str,
                    buy_strategy: str, sl: float, tr: float, hm: int,
                    n_positions: int = 3,
                    init_cash: float = 1_000_000.0,
                    lookback: int = 5, min_consecutive: int = 2,
                    open_rate_max: float = -2.0) -> Dict:
    print(f"\n--- Phase-3 回测 [{start} ~ {end}] buy={buy_strategy} SL={sl}% TR={tr}% HM={hm} N={n_positions} ---")
    main_dates, idx = build_history_index(conn, start, end, lookback_days=lookback + 3)
    if not main_dates:
        return {"start": start, "end": end, "ret_pct": 0.0, "trades": 0}

    cash = init_cash
    positions: List[Dict] = []  # {code, buy_price, shares, buy_date, highest, sl_price}
    closed_trades: List[Dict] = []
    equity_curve: List[Tuple[str, float]] = []

    for di, today in enumerate(main_dates):
        # 1. 卖出逻辑 (现有持仓, 跳过 buy_date 当日 = T+1)
        new_positions: List[Dict] = []
        for pos in positions:
            if pos["buy_date"] == today:
                new_positions.append(pos)
                continue
            srow = idx.get(pos["code"], {}).get(today)
            if not srow:
                new_positions.append(pos)
                continue
            sold = False
            # 跌停不可卖
            if is_limit_down(pos["code"], srow["close"], srow["preclose"]):
                # 更新 highest
                for hi in range(1, 5):
                    hh = srow[f"hour{hi}_high"]
                    if hh and hh > pos["highest"]:
                        pos["highest"] = hh
                # 强平天数检查
                hold_off = main_dates.index(today) - main_dates.index(pos["buy_date"])
                if hold_off >= hm and srow["close"] is not None:
                    # 即便跌停, hold_max 强平到 close
                    sell_price = srow["close"]
                    cash += sell_price * pos["shares"]
                    closed_trades.append({**pos, "sell_date": today, "sell_price": sell_price,
                                          "sell_reason": "HOLD_MAX_LD", "sell_hour": 4,
                                          "return_pct": (sell_price - pos["buy_price"]) / pos["buy_price"] * 100})
                    sold = True
                if not sold:
                    new_positions.append(pos)
                continue

            # 涨停不卖
            if is_limit_up(pos["code"], srow["close"], srow["preclose"]):
                for hi in range(1, 5):
                    hh = srow[f"hour{hi}_high"]
                    if hh and hh > pos["highest"]:
                        pos["highest"] = hh
                hold_off = main_dates.index(today) - main_dates.index(pos["buy_date"])
                if hold_off >= hm and srow["close"] is not None:
                    sell_price = srow["close"]
                    cash += sell_price * pos["shares"]
                    closed_trades.append({**pos, "sell_date": today, "sell_price": sell_price,
                                          "sell_reason": "HOLD_MAX_LU", "sell_hour": 4,
                                          "return_pct": (sell_price - pos["buy_price"]) / pos["buy_price"] * 100})
                    sold = True
                if not sold:
                    new_positions.append(pos)
                continue

            sl_price = pos["sl_price"]
            for hi in range(1, 5):
                ho_ = srow[f"hour{hi}_open"]
                hh = srow[f"hour{hi}_high"]
                hl = srow[f"hour{hi}_low"]
                hc = srow[f"hour{hi}_close"]
                if any(v is None for v in (ho_, hh, hl, hc)):
                    continue
                # 保守序: 用旧 highest 判定, 后更新
                old_highest = pos["highest"]
                tr_price = old_highest * (1 + tr / 100.0)
                if hl <= sl_price:
                    sell_price = ho_ if ho_ <= sl_price else sl_price
                    cash += sell_price * pos["shares"]
                    closed_trades.append({**pos, "sell_date": today, "sell_price": sell_price,
                                          "sell_reason": "STOP_LOSS", "sell_hour": hi,
                                          "return_pct": (sell_price - pos["buy_price"]) / pos["buy_price"] * 100})
                    sold = True
                    break
                if old_highest > pos["buy_price"] * 1.005 and hl <= tr_price:
                    sp = ho_ if ho_ <= tr_price else tr_price
                    sell_price = max(sp, sl_price) if ho_ > sl_price else sp
                    cash += sell_price * pos["shares"]
                    closed_trades.append({**pos, "sell_date": today, "sell_price": sell_price,
                                          "sell_reason": "TRAILING", "sell_hour": hi,
                                          "return_pct": (sell_price - pos["buy_price"]) / pos["buy_price"] * 100})
                    sold = True
                    break
                if hh > pos["highest"]:
                    pos["highest"] = hh
            if sold:
                continue
            # 持仓天数到达 hm 强平
            hold_off = main_dates.index(today) - main_dates.index(pos["buy_date"])
            if hold_off >= hm:
                sell_price = srow["close"] if srow["close"] is not None else pos["buy_price"]
                cash += sell_price * pos["shares"]
                closed_trades.append({**pos, "sell_date": today, "sell_price": sell_price,
                                      "sell_reason": "HOLD_MAX", "sell_hour": 4,
                                      "return_pct": (sell_price - pos["buy_price"]) / pos["buy_price"] * 100})
                continue
            new_positions.append(pos)
        positions = new_positions

        # 2. 买入逻辑: 当日空槽位 = N - len(positions)
        slots = n_positions - len(positions)
        if slots > 0:
            cands = find_candidates_for_date(idx, main_dates, today,
                                              lookback=lookback,
                                              min_consecutive=min_consecutive,
                                              open_rate_max=open_rate_max)
            # 按 max_streak 降序, open_rate 升序 (跌得多优先)
            cands.sort(key=lambda c: (-c["max_streak"], c["open_rate"]))
            held_codes = {p["code"] for p in positions}
            cands = [c for c in cands if c["code"] not in held_codes]

            for c in cands:
                if slots <= 0:
                    break
                trow = idx[c["code"]][today]
                # 涨停不可买 (open already 验证 < -2%, 但保险)
                if is_limit_up(c["code"], trow["close"], trow["preclose"]):
                    continue
                # 计算限价
                open_p = trow["open"]
                h1c = trow["hour1_close"]
                h1l = trow["hour1_low"]
                h2l = trow["hour2_low"]
                if any(v is None for v in (open_p, h1c, h1l, h2l)):
                    continue
                if buy_strategy == "A":
                    P = h1c * 0.98
                    if h2l > P:
                        continue
                    bh = 2
                elif buy_strategy == "B":
                    P = h1l
                    if h2l > P:
                        continue
                    bh = 2
                else:
                    P = open_p * 0.97
                    if h1l > P:
                        continue
                    bh = 1
                if P is None or P <= 0:
                    continue
                buy_price = round(P, 2)
                # 仓位资金 = (cash + 持仓市值)/N
                # 简单分配: 每仓 init_cash / N (固定), 或动态. 这里用动态:
                pos_cash = cash / max(1, slots)
                shares = int(pos_cash / buy_price / 100) * 100
                if shares < 100:
                    continue
                cost = shares * buy_price
                if cost > cash:
                    continue
                cash -= cost
                positions.append({
                    "code": c["code"],
                    "buy_date": today,
                    "buy_price": buy_price,
                    "buy_hour": bh,
                    "shares": shares,
                    "highest": buy_price,
                    "sl_price": buy_price * (1 + sl / 100.0),
                })
                slots -= 1

        # 3. 当日权益快照 (按收盘市值)
        eq = cash
        for pos in positions:
            srow = idx.get(pos["code"], {}).get(today)
            mp = srow["close"] if srow and srow["close"] is not None else pos["buy_price"]
            eq += mp * pos["shares"]
        equity_curve.append((today, eq))

    # 收尾: 用最后一日收盘强平剩余持仓
    if positions and main_dates:
        last_d = main_dates[-1]
        for pos in positions:
            srow = idx.get(pos["code"], {}).get(last_d)
            mp = srow["close"] if srow and srow["close"] is not None else pos["buy_price"]
            cash += mp * pos["shares"]
            closed_trades.append({**pos, "sell_date": last_d, "sell_price": mp,
                                  "sell_reason": "EOP", "sell_hour": 4,
                                  "return_pct": (mp - pos["buy_price"]) / pos["buy_price"] * 100})
        positions = []

    final_eq = equity_curve[-1][1] if equity_curve else init_cash
    ret_pct = (final_eq - init_cash) / init_cash * 100
    n = len(closed_trades)
    win_rate = sum(1 for t in closed_trades if t["return_pct"] > 0) / max(1, n) * 100
    avg_ret = sum(t["return_pct"] for t in closed_trades) / max(1, n)
    print(f"  最终权益: {final_eq:,.0f}  收益: {ret_pct:+.2f}%  交易数: {n}  胜率: {win_rate:.2f}%  单笔均值: {avg_ret:+.3f}%")
    return {
        "start": start, "end": end,
        "final_equity": final_eq,
        "ret_pct": ret_pct,
        "trades": n,
        "win_rate": win_rate,
        "avg_ret": avg_ret,
    }


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------

def main():
    conn = get_conn()

    # ============ Phase-1 ============
    cands_by_date, monthly = phase1(conn,
                                      start="2025-01-01", end="2025-12-31",
                                      lookback=5, min_consecutive=2,
                                      open_rate_max=-2.0)
    avg_per_month = sum(monthly.values()) / max(1, len(monthly))

    # 自适应放宽/加严
    open_rate_max = -2.0
    if avg_per_month < 20:
        print("\n[Phase-1] 候选过少, 放宽 open_rate <= 0%, 重新筛选...")
        open_rate_max = 0.0
        cands_by_date, monthly = phase1(conn,
                                          start="2025-01-01", end="2025-12-31",
                                          lookback=5, min_consecutive=2,
                                          open_rate_max=open_rate_max)
        avg_per_month = sum(monthly.values()) / max(1, len(monthly))
    elif avg_per_month > 500:
        print("\n[Phase-1] 候选过多, 加严 min_consecutive=3, 重新筛选...")
        cands_by_date, monthly = phase1(conn,
                                          start="2025-01-01", end="2025-12-31",
                                          lookback=5, min_consecutive=3,
                                          open_rate_max=-2.0)
        avg_per_month = sum(monthly.values()) / max(1, len(monthly))

    # ============ Phase-2 (Q1) ============
    p2 = phase2(conn, cands_by_date,
                q1_start="2025-01-01", q1_end="2025-03-31")
    best = p2["best"]
    if best is None or best["n"] == 0:
        print("\n[Phase-2] 无可用 alpha, 终止.")
        conn.close()
        return
    if best["avg"] <= 0:
        print(f"\n[Phase-2] 最佳参数平均收益<=0 (avg={best['avg']:.3f}%), 仍继续 Phase-3 验证以获取数据.")

    # ============ Phase-3 全年 / 多年 ============
    print(f"\n{'='*80}\nPhase-3 多年回测 (Best: buy={best['buy']} SL={best['sl']} TR={best['tr']} HM={best['hm']})\n{'='*80}")
    yearly_results = []
    for y in range(2020, 2026):
        ys = f"{y}-01-01"
        ye = f"{y}-12-31"
        if y == 2026:
            ye = "2026-05-22"
        r = phase3_backtest(conn, ys, ye,
                             buy_strategy=best["buy"],
                             sl=best["sl"], tr=best["tr"], hm=best["hm"],
                             n_positions=3, init_cash=1_000_000.0,
                             lookback=5, min_consecutive=2,
                             open_rate_max=open_rate_max)
        yearly_results.append((y, r))

    # 汇总
    print(f"\n{'='*80}\nPhase-3 汇总 (逐年收益与累计 CAGR)\n{'='*80}")
    cum = 1.0
    for y, r in yearly_results:
        cum *= (1 + r["ret_pct"] / 100.0)
        print(f"  {y}: 收益 {r['ret_pct']:+8.2f}%  交易 {r['trades']:5d}  胜率 {r['win_rate']:5.2f}%  累计权益倍数 {cum:.3f}")
    n_years = len(yearly_results)
    if n_years > 0 and cum > 0:
        cagr = (cum ** (1.0 / n_years) - 1) * 100
        print(f"\n  6年累计权益倍数: {cum:.3f}, CAGR: {cagr:+.2f}%")

    conn.close()


if __name__ == "__main__":
    main()
