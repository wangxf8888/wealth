#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""从 v2 optimized 真实回测结果构建前端 dashboard 数据。"""
import json
import math
from collections import defaultdict
from datetime import date as _date

FE = "/home/AIWealth/frontend"
INITIAL = 1_000_000.0

equity = json.load(open(f"{FE}/combined_v2_equity_optimized.json"))
trades = json.load(open(f"{FE}/combined_v2_trades_optimized.json"))

equity.sort(key=lambda x: x["date"])
dates = [e["date"] for e in equity]
date_idx = {d: i for i, d in enumerate(dates)}
eq_by_date = {e["date"]: e for e in equity}

final_value = equity[-1]["equity"]
total_return_pct = (final_value / INITIAL - 1.0) * 100.0
wins = sum(1 for t in trades if t["profit"] > 0)
total_trades = len(trades)
win_rate = wins / total_trades * 100.0 if total_trades else 0.0

daily_ret = []
peak = equity[0]["equity"]
max_dd = 0.0
prev = None
for e in equity:
    v = e["equity"]
    if prev is not None and prev > 0:
        daily_ret.append(v / prev - 1.0)
    peak = max(peak, v)
    dd = (v - peak) / peak * 100.0
    max_dd = min(max_dd, dd)
    prev = v

mean_r = sum(daily_ret) / len(daily_ret) if daily_ret else 0.0
var = sum((r - mean_r) ** 2 for r in daily_ret) / len(daily_ret) if daily_ret else 0.0
std_r = math.sqrt(var)
sharpe = (mean_r / std_r * math.sqrt(252)) if std_r > 0 else 0.0

_d0 = _date.fromisoformat(dates[0])
_d1 = _date.fromisoformat(dates[-1])
years = (_d1 - _d0).days / 365.25 if _d1 > _d0 else 1.0
cagr = ((final_value / INITIAL) ** (1.0 / years) - 1.0) * 100.0 if years > 0 else 0.0

summary = {
    "strategy_name": "大阴高开+龙回头 优化组合 A_deep+B+D (5仓位)",
    "initial_capital": INITIAL,
    "current_value": round(final_value),
    "final_value": round(final_value),
    "total_return_pct": round(total_return_pct, 1),
    "cagr_pct": round(cagr, 1),
    "total_trades": total_trades,
    "win_rate": round(win_rate, 1),
    "max_drawdown": round(max_dd, 1),
    "sharpe_ratio": round(sharpe, 2),
}

buys_by_date = defaultdict(list)
sells_by_date = defaultdict(list)
for t in trades:
    buys_by_date[t["buy_date"]].append(t)
    sells_by_date[t["sell_date"]].append(t)

def price_on(t, date):
    if date == t["sell_date"]:
        return t["sell_price"], t["ret_pct"], "selling"
    return t["buy_price"], 0.0, "holding"

active = {}
free_slots = [1, 2, 3, 4, 5]
daily_data = {}

for i, d in enumerate(dates):
    e = eq_by_date[d]
    signals = []
    for t in sells_by_date.get(d, []):
        signals.append({
            "type": "sell", "code": t["code"], "name": t["name"],
            "price": t["sell_price"], "strategy": t["strategy_name"],
            "reason": f"{t['strategy_name']} · 持有{t['hold_days']}天 · 收益{t['ret_pct']:+.2f}%",
        })
    for t in buys_by_date.get(d, []):
        signals.append({
            "type": "buy", "code": t["code"], "name": t["name"],
            "price": t["buy_price"], "strategy": t["strategy_name"],
            "reason": f"{t['strategy_name']} · 高开{t['gap']:.2f}% · 前日跌幅{t['drop']:.2f}%",
        })

    for code in list(active.keys()):
        if active[code]["trade"]["sell_date"] < d:
            free_slots.append(active[code]["slot"])
            free_slots.sort()
            del active[code]
    for t in buys_by_date.get(d, []):
        code = t["code"]
        if code not in active and free_slots:
            active[code] = {"trade": t, "slot": free_slots.pop(0)}

    day_positions = []
    for code, info in active.items():
        t = info["trade"]
        if t["buy_date"] <= d <= t["sell_date"]:
            cur_price, ret_pct, status = price_on(t, d)
            hold = date_idx.get(d, 0) - date_idx.get(t["buy_date"], 0)
            day_positions.append({
                "slot": info["slot"], "status": status,
                "code": code, "name": t["name"], "strategy": t["strategy_name"],
                "buy_date": t["buy_date"], "buy_price": t["buy_price"],
                "current_price": cur_price, "holding_days": max(hold, 0),
                "return_pct": ret_pct,
            })
    day_positions.sort(key=lambda p: p["slot"])

    prev_eq = eq_by_date[dates[i - 1]]["equity"] if i > 0 else INITIAL
    daily_ret_pct = (e["equity"] / prev_eq - 1.0) * 100.0 if prev_eq > 0 else 0.0
    cum_ret_pct = (e["equity"] / INITIAL - 1.0) * 100.0

    n_buy = len(buys_by_date.get(d, []))
    n_sell = len(sells_by_date.get(d, []))
    if n_buy or n_sell:
        commentary = (f"当日买入{n_buy}笔、卖出{n_sell}笔；组合净值 ¥{e['equity']:,.0f}，"
                      f"当日{daily_ret_pct:+.2f}%，累计{cum_ret_pct:+.1f}%。")
    else:
        commentary = (f"当日无交易，持仓{len(day_positions)}只；组合净值 ¥{e['equity']:,.0f}，"
                      f"累计{cum_ret_pct:+.1f}%。")

    daily_data[d] = {
        "portfolio_value": round(e["equity"]),
        "daily_return_pct": round(daily_ret_pct, 2),
        "cumulative_return_pct": round(cum_ret_pct, 2),
        "benchmark_return_pct": 0.0,
        "positions": day_positions,
        "signals": signals,
        "commentary": commentary,
    }

year_end = {}
for e in equity:
    year_end[e["date"][:4]] = e["equity"]
years_sorted = sorted(year_end.keys())
yearly_stats = []
prev_end = INITIAL
tcount = defaultdict(int)
twin = defaultdict(int)
for t in trades:
    y = t["sell_date"][:4]
    tcount[y] += 1
    if t["profit"] > 0:
        twin[y] += 1
for y in years_sorted:
    end_v = year_end[y]
    ret = (end_v / prev_end - 1.0) * 100.0 if prev_end > 0 else 0.0
    yearly_stats.append({
        "year": y,
        "return_pct": round(ret, 1),
        "end_value": round(end_v),
        "trades": tcount.get(y, 0),
        "win_rate": round(twin.get(y, 0) / tcount[y] * 100.0, 1) if tcount.get(y) else 0.0,
    })
    prev_end = end_v

out = {
    "summary": summary,
    "yearly_stats": yearly_stats,
    "daily_data": daily_data,
    "all_trades": trades,
}

with open(f"{FE}/dashboard_data.json", "w", encoding="utf-8") as f:
    json.dump(out, f, ensure_ascii=False, separators=(",", ":"))

with open(f"{FE}/dashboard_data.js", "w", encoding="utf-8") as f:
    f.write("window.DASHBOARD_DATA = ")
    json.dump(out, f, ensure_ascii=False, separators=(",", ":"))
    f.write(";\n")

print("OK 交易日:", len(dates), "首日:", dates[0], "末日:", dates[-1])
print("summary:", json.dumps(summary, ensure_ascii=False))
for ys in yearly_stats:
    print("  ", ys)
