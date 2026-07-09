#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Task #85: 用四策略V3回测数据重建前端 dashboard 数据。

产出:
  frontend/dashboard_data.json   (summary / yearly_stats / daily_data / all_trades)
  frontend/dashboard_data.js     (window.DASHBOARD_DATA = {...};)

数据源:
  scripts/logs/four_v3_backtest.log  -> 每日 NAV / 总资产 / 累计收益
  frontend/four_v3_trades.json       -> 635 笔逐笔交易(含 holding_detail 小时级rate)
"""
import json
import re
import os
from collections import defaultdict

BASE = "/home/AIWealth"
LOG = os.path.join(BASE, "scripts/logs/four_v3_backtest.log")
TRADES = os.path.join(BASE, "frontend/four_v3_trades.json")
OUT_JSON = os.path.join(BASE, "frontend/dashboard_data.json")
OUT_JS = os.path.join(BASE, "frontend/dashboard_data.js")

INITIAL_CAPITAL = 1_000_000.0
N_SLOTS = 5

STRATEGY_NAME = {
    "bigdrop_gapup": "大阴高开",
    "shrink_reversal": "缩量反转",
    "limitdown_rebound": "跌停反弹",
    "dragon_pullback": "龙回头",
}


def parse_log_nav(path):
    """解析日志中每个交易日的日终 NAV/总资产/累计收益。"""
    day_re = re.compile(r"===\s*(\d{4}-\d{2}-\d{2})\s*日终总结\s*===")
    nav_re = re.compile(
        r"累计净值:\s*([\d.]+)\s*\|\s*累计收益:\s*([+\-]?[\d.]+)%\s*\|\s*总资产:\s*([\d,]+\.?\d*)"
    )
    daily = {}
    cur_date = None
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            m = day_re.search(line)
            if m:
                cur_date = m.group(1)
                continue
            if cur_date:
                mn = nav_re.search(line)
                if mn:
                    nav = float(mn.group(1))
                    cum = float(mn.group(2))
                    asset = float(mn.group(3).replace(",", ""))
                    daily[cur_date] = {"nav": nav, "cum": cum, "asset": asset}
                    cur_date = None
    return daily


def assign_slots(trades):
    """按买入时间贪心分配 5 个固定槽位, 返回 id->slot。"""
    order = sorted(trades, key=lambda t: (t["buy_date"], t["id"]))
    slot_free_after = [None] * N_SLOTS  # 每个槽位当前占用者的 sell_date
    id_slot = {}
    for t in order:
        placed = False
        for s in range(N_SLOTS):
            if slot_free_after[s] is None or slot_free_after[s] < t["buy_date"]:
                id_slot[t["id"]] = s + 1
                slot_free_after[s] = t["sell_date"]
                placed = True
                break
        if not placed:
            # 理论上 5 仓位不应溢出, 兜底放到占用最早释放的槽
            s = min(range(N_SLOTS), key=lambda i: slot_free_after[i])
            id_slot[t["id"]] = s + 1
            slot_free_after[s] = t["sell_date"]
    return id_slot


def price_on_date(trade, date):
    """根据 holding_detail 的 h4_close_rate(相对买价%) 计算某日收盘现价。"""
    for hd in trade.get("holding_detail", []):
        if hd["date"] == date:
            rate = hd.get("h4_close_rate", 0.0)
            return round(trade["buy_price"] * (1 + rate / 100.0), 2)
    return trade["buy_price"]


def holding_day_index(trade, date):
    for i, hd in enumerate(trade.get("holding_detail", [])):
        if hd["date"] == date:
            return i
    return 0


def main():
    nav_map = parse_log_nav(LOG)
    trades = json.load(open(TRADES, "r", encoding="utf-8"))
    id_slot = assign_slots(trades)

    all_dates = sorted(nav_map.keys())

    # ---- all_trades ----
    all_trades = []
    for t in trades:
        sname = STRATEGY_NAME.get(t["strategy"], t["strategy"])
        all_trades.append({
            "code": t["code"],
            "name": t["name"],
            "strategy": t["strategy"],
            "strategy_name": sname,
            "buy_date": t["buy_date"],
            "buy_price": t["buy_price"],
            "sell_date": t["sell_date"],
            "sell_price": t["sell_price"],
            "ret_pct": t["return_pct"],
            "hold_days": t["holding_days"],
            "slot": id_slot[t["id"]],
        })

    # 预建索引: 按日期归集买入/卖出/持有
    buys_by_date = defaultdict(list)
    sells_by_date = defaultdict(list)
    for t in trades:
        buys_by_date[t["buy_date"]].append(t)
        sells_by_date[t["sell_date"]].append(t)

    # ---- daily_data ----
    daily_data = {}
    prev_asset = INITIAL_CAPITAL
    for date in all_dates:
        info = nav_map[date]
        asset = info["asset"]
        daily_ret = (asset / prev_asset - 1.0) * 100.0 if prev_asset else 0.0
        prev_asset = asset

        # 持仓: buy_date <= date <= sell_date
        positions = []
        for t in trades:
            if t["buy_date"] <= date <= t["sell_date"]:
                cur = price_on_date(t, date)
                positions.append({
                    "slot": id_slot[t["id"]],
                    "status": "holding",
                    "code": t["code"],
                    "name": t["name"],
                    "strategy": STRATEGY_NAME.get(t["strategy"], t["strategy"]),
                    "buy_date": t["buy_date"],
                    "buy_price": t["buy_price"],
                    "current_price": cur,
                    "holding_days": holding_day_index(t, date),
                    "return_pct": round((cur / t["buy_price"] - 1.0) * 100.0, 2),
                })
        positions.sort(key=lambda p: p["slot"])

        # 信号: 当日买入 + 当日卖出
        signals = []
        for t in buys_by_date.get(date, []):
            signals.append({
                "type": "buy",
                "code": t["code"],
                "name": t["name"],
                "price": t["buy_price"],
                "strategy": STRATEGY_NAME.get(t["strategy"], t["strategy"]),
                "reason": t.get("buy_reason", ""),
            })
        for t in sells_by_date.get(date, []):
            signals.append({
                "type": "sell",
                "code": t["code"],
                "name": t["name"],
                "price": t["sell_price"],
                "strategy": STRATEGY_NAME.get(t["strategy"], t["strategy"]),
                "reason": "持有%d日 · 收益%+.2f%%" % (t["holding_days"], t["return_pct"]),
            })

        n_buy = len(buys_by_date.get(date, []))
        n_sell = len(sells_by_date.get(date, []))
        commentary = (
            "当日买入%d笔、卖出%d笔；组合净值 ¥%s，当日%+.2f%%，累计%+.1f%%。"
            % (n_buy, n_sell, "{:,}".format(int(asset)), daily_ret, info["cum"])
        )

        daily_data[date] = {
            "portfolio_value": int(asset),
            "daily_return_pct": round(daily_ret, 2),
            "cumulative_return_pct": round(info["cum"], 2),
            "benchmark_return_pct": 0.0,
            "positions": positions,
            "signals": signals,
            "commentary": commentary,
        }

    # ---- summary ----
    final_value = nav_map[all_dates[-1]]["asset"]
    total_return = (final_value / INITIAL_CAPITAL - 1.0) * 100.0
    n_years = (
        (int(all_dates[-1][:4]) + int(all_dates[-1][5:7]) / 12.0)
        - (int(all_dates[0][:4]) + int(all_dates[0][5:7]) / 12.0)
    )
    n_years = max(n_years, 0.01)
    cagr = ((final_value / INITIAL_CAPITAL) ** (1.0 / n_years) - 1.0) * 100.0
    wins = sum(1 for t in trades if t["return_pct"] > 0)
    win_rate = wins / len(trades) * 100.0

    # 最大回撤 + Sharpe (基于日总资产序列)
    assets = [nav_map[d]["asset"] for d in all_dates]
    peak = assets[0]
    max_dd = 0.0
    for a in assets:
        peak = max(peak, a)
        dd = (a / peak - 1.0) * 100.0
        max_dd = min(max_dd, dd)
    rets = [assets[i] / assets[i - 1] - 1.0 for i in range(1, len(assets))]
    if rets:
        mean = sum(rets) / len(rets)
        var = sum((r - mean) ** 2 for r in rets) / len(rets)
        std = var ** 0.5
        sharpe = (mean / std * (252 ** 0.5)) if std > 0 else 0.0
    else:
        sharpe = 0.0

    summary = {
        "strategy_name": "四策略V3组合 大阴高开+缩量反转+跌停反弹+龙回头 (5仓位)",
        "initial_capital": INITIAL_CAPITAL,
        "current_value": int(final_value),
        "final_value": int(final_value),
        "total_return_pct": round(total_return, 1),
        "cagr_pct": round(cagr, 2),
        "total_trades": len(trades),
        "win_rate": round(win_rate, 1),
        "max_drawdown": round(max_dd, 1),
        "sharpe_ratio": round(sharpe, 2),
    }

    # ---- yearly_stats ----
    years = sorted({d[:4] for d in all_dates})
    yearly_stats = []
    prev_year_asset = INITIAL_CAPITAL
    for y in years:
        yd = [d for d in all_dates if d[:4] == y]
        end_asset = nav_map[yd[-1]]["asset"]
        ret = (end_asset / prev_year_asset - 1.0) * 100.0
        prev_year_asset = end_asset
        yt = [t for t in trades if t["sell_date"][:4] == y]
        yw = sum(1 for t in yt if t["return_pct"] > 0)
        yearly_stats.append({
            "year": y,
            "return_pct": round(ret, 1),
            "end_value": int(end_asset),
            "trades": len(yt),
            "win_rate": round(yw / len(yt) * 100.0, 1) if yt else 0.0,
        })

    result = {
        "summary": summary,
        "yearly_stats": yearly_stats,
        "daily_data": daily_data,
        "all_trades": all_trades,
    }

    with open(OUT_JSON, "w", encoding="utf-8") as f:
        json.dump(result, f, ensure_ascii=False, separators=(",", ":"))
    with open(OUT_JS, "w", encoding="utf-8") as f:
        f.write("window.DASHBOARD_DATA = ")
        json.dump(result, f, ensure_ascii=False, separators=(",", ":"))
        f.write(";\n")

    print("交易日数:", len(all_dates), all_dates[0], "~", all_dates[-1])
    print("summary:", json.dumps(summary, ensure_ascii=False))
    print("yearly:")
    for ys in yearly_stats:
        print("  ", ys)
    print("json size: %.2f MB" % (os.path.getsize(OUT_JSON) / 1e6))
    print("js   size: %.2f MB" % (os.path.getsize(OUT_JS) / 1e6))


if __name__ == "__main__":
    main()
