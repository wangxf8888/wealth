#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
解析 V9-Final 引擎产物 -> Dashboard data.json + data.js
================================================
输入:
  /home/AIWealth/scripts/priority_v9_final_trades.log    (管道分隔交易明细)
  /home/AIWealth/scripts/priority_v9_final_equity.csv    (日权益曲线)

输出:
  /home/AIWealth/frontend/data.json
  /home/AIWealth/frontend/data.js   (window.DASHBOARD_DATA = {...};)
"""
from __future__ import annotations

import csv
import json
import os
from collections import defaultdict
from datetime import datetime

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TRADES_LOG = os.path.join(ROOT, "scripts", "priority_v9_final_trades.log")
EQUITY_CSV = os.path.join(ROOT, "scripts", "priority_v9_final_equity.csv")
OUT_JSON = os.path.join(ROOT, "frontend", "data.json")
OUT_JS = os.path.join(ROOT, "frontend", "data.js")

INITIAL_CAPITAL = 1_000_000.0
MAX_NAV_POINTS = 1500


# ---------------------------------------------------------------- trades
def parse_trades(path):
    """返回 trades 列表, 已剔除 header"""
    trades = []
    with open(path, "r", encoding="utf-8") as f:
        for i, raw in enumerate(f):
            line = raw.rstrip("\n")
            if not line:
                continue
            if i == 0:  # header
                continue
            parts = line.split("|")
            if len(parts) < 13:
                continue
            try:
                pnl_pct_raw = parts[8].strip().rstrip("%")
                pnl_pct = float(pnl_pct_raw)
                pnl_raw = parts[11].strip().replace(",", "")
                pnl = float(pnl_raw)
                trades.append({
                    "trade_id": i,
                    "buy_date": parts[0],
                    "sell_date": parts[1],
                    "sell_hour": int(parts[2]),
                    "tag": parts[3],
                    "code": parts[4],
                    "name": parts[5],
                    "buy_price": float(parts[6]),
                    "sell_price": float(parts[7]),
                    "pnl_pct": pnl_pct,
                    "hold_days": int(parts[9]),
                    "shares": int(parts[10]),
                    "pnl": pnl,
                    "exit_type": parts[12],
                })
            except (ValueError, IndexError):
                continue
    trades.sort(key=lambda t: (t["sell_date"], t["buy_date"]))
    return trades


# ---------------------------------------------------------------- equity
def parse_equity(path):
    """返回按日期升序的 [{date, equity, nav, cash}, ...]"""
    rows = []
    with open(path, "r", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for r in reader:
            try:
                rows.append({
                    "date": r["date"],
                    "equity": float(r["equity"]),
                    "nav": float(r["nav"]),
                    "cash": float(r["cash"]),
                })
            except (ValueError, KeyError):
                continue
    rows.sort(key=lambda x: x["date"])
    return rows


# ---------------------------------------------------------------- stats
def compute_drawdown(equity_rows):
    """返回 [{date, drawdown}], drawdown 单位百分比, 正数代表回撤"""
    out = []
    peak = 0.0
    for r in equity_rows:
        nav = r["nav"]
        if nav > peak:
            peak = nav
        dd = (peak - nav) / peak * 100.0 if peak > 0 else 0.0
        out.append({"date": r["date"], "drawdown": round(dd, 4)})
    return out


def yearly_monthly_returns(equity_rows):
    """年度/月度收益 (与引擎一致: 上期末为基准, 首期以 nav=1 为基准)"""
    by_y_end = {}
    by_m_end = {}
    for r in equity_rows:
        d = r["date"]
        y, ym = d[:4], d[:7]
        by_y_end[y] = r["nav"]
        by_m_end[ym] = r["nav"]

    yearly = {}
    prev = 1.0
    for y in sorted(by_y_end.keys()):
        end = by_y_end[y]
        if prev > 0:
            yearly[y] = round((end / prev - 1) * 100.0, 2)
        prev = end

    monthly = {}
    prev = 1.0
    for ym in sorted(by_m_end.keys()):
        end = by_m_end[ym]
        if prev > 0:
            monthly[ym] = round((end / prev - 1) * 100.0, 2)
        prev = end
    return yearly, monthly


def build_heatmap(monthly_returns):
    hm = {}
    for ym, v in monthly_returns.items():
        y, m = ym.split("-")
        hm.setdefault(y, {})[int(m)] = v
    return hm


def downsample(rows, key, max_points=MAX_NAV_POINTS):
    if not rows:
        return []
    n = len(rows)
    if n <= max_points:
        out = [{"date": r["date"], key: r[key]} for r in rows]
    else:
        step = max(1, n // max_points)
        out = [{"date": rows[i]["date"], key: rows[i][key]}
               for i in range(0, n, step)]
        if out[-1]["date"] != rows[-1]["date"]:
            out.append({"date": rows[-1]["date"], key: rows[-1][key]})
    return out


# ---------------------------------------------------------------- summary
def tag_stats(trades, tag):
    sub = [t for t in trades if t["tag"] == tag]
    n = len(sub)
    wins = sum(1 for t in sub if t["pnl_pct"] > 0)
    win_rate = (wins / n * 100.0) if n else 0.0
    return n, win_rate


def build_summary(trades, equity_rows, drawdown_rows):
    if not equity_rows:
        return {}
    final_eq = equity_rows[-1]["equity"]
    d0 = datetime.strptime(equity_rows[0]["date"], "%Y-%m-%d")
    d1 = datetime.strptime(equity_rows[-1]["date"], "%Y-%m-%d")
    years = max(0.05, (d1 - d0).days / 365.25)
    cagr = (final_eq / INITIAL_CAPITAL) ** (1.0 / years) - 1.0
    cagr_pct = round(cagr * 100.0, 2)

    n = len(trades)
    wins = sum(1 for t in trades if t["pnl_pct"] > 0)
    win_rate = round(wins / n * 100.0, 2) if n else 0.0

    pos = [t["pnl_pct"] for t in trades if t["pnl_pct"] > 0]
    neg = [-t["pnl_pct"] for t in trades if t["pnl_pct"] < 0]
    if pos and neg:
        plr = round((sum(pos) / len(pos)) / (sum(neg) / len(neg)), 2)
    else:
        plr = 0.0

    max_dd = max((r["drawdown"] for r in drawdown_rows), default=0.0)
    max_dd_date = ""
    for r in drawdown_rows:
        if r["drawdown"] >= max_dd - 1e-9:
            max_dd_date = r["date"]
            break
    calmar = round(cagr_pct / max_dd, 2) if max_dd > 1e-6 else 0.0

    p0_n, p0_win = tag_stats(trades, "P0")
    p1_n, p1_win = tag_stats(trades, "P1")

    return {
        "engine_version": "V9-Final",
        "annualized_return": cagr_pct,
        "win_rate": win_rate,
        "max_drawdown": round(max_dd, 2),
        "max_dd_date": max_dd_date,
        "profit_loss_ratio": plr,
        "calmar": calmar,
        "total_trades": n,
        "winning_trades": wins,
        "losing_trades": n - wins,
        "p0_trades": p0_n,
        "p1_trades": p1_n,
        "p0_win_rate": round(p0_win, 2),
        "p1_win_rate": round(p1_win, 2),
        "backtest_start": equity_rows[0]["date"],
        "backtest_end": equity_rows[-1]["date"],
        "initial_capital": INITIAL_CAPITAL,
        "final_asset": round(final_eq, 2),
    }


# ---------------------------------------------------------------- main
def main():
    print(f"[V9-Final PARSER] trades  : {TRADES_LOG}")
    print(f"[V9-Final PARSER] equity  : {EQUITY_CSV}")

    trades = parse_trades(TRADES_LOG)
    equity_rows = parse_equity(EQUITY_CSV)
    print(f"  trades parsed : {len(trades)}")
    print(f"  equity rows   : {len(equity_rows)}")

    drawdown_rows = compute_drawdown(equity_rows)
    yearly_returns, monthly_returns = yearly_monthly_returns(equity_rows)
    heatmap = build_heatmap(monthly_returns)
    summary = build_summary(trades, equity_rows, drawdown_rows)

    strategy_params = {
        "P0": "7+连板龙头二波 | 回调35-50% | J<20 | T+2卖出",
        "P1": "ZhaBan trail_5d | SL-3% | Trail 5%启动/3%回撤 | 最长5天",
        "slots": "3仓位",
        "slippage": "±0.3% (内置)",
    }

    equity_curve = downsample(equity_rows, "nav")
    drawdown_curve = downsample(drawdown_rows, "drawdown")

    data = {
        "summary": summary,
        "strategy_params": strategy_params,
        "equity_curve": equity_curve,
        "drawdown_curve": drawdown_curve,
        "yearly_returns": yearly_returns,
        "monthly_returns": monthly_returns,
        "heatmap": heatmap,
        "trades": trades,
    }

    with open(OUT_JSON, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    with open(OUT_JS, "w", encoding="utf-8") as f:
        f.write("window.DASHBOARD_DATA = ")
        json.dump(data, f, ensure_ascii=False)
        f.write(";\n")

    print(f"\n✓ V9-Final Dashboard 数据已生成")
    print(f"  CAGR        : {summary.get('annualized_return')}%")
    print(f"  Win Rate    : {summary.get('win_rate')}%")
    print(f"  MaxDD       : {summary.get('max_drawdown')}% @ {summary.get('max_dd_date')}")
    print(f"  Calmar      : {summary.get('calmar')}")
    print(f"  PLR         : {summary.get('profit_loss_ratio')}")
    print(f"  Trades(P0/P1): {summary.get('p0_trades')} / {summary.get('p1_trades')}")
    print(f"  P0 Win/P1 Win: {summary.get('p0_win_rate')}% / {summary.get('p1_win_rate')}%")
    print(f"  Final Asset : {summary.get('final_asset'):,.0f}")
    print(f"  Equity pts  : {len(equity_curve)} (downsampled from {len(equity_rows)})")
    print(f"  -> {OUT_JSON}")
    print(f"  -> {OUT_JS}")


if __name__ == "__main__":
    main()
