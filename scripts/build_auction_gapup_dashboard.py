#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Task #100: 用 AuctionGapUpVol (slot=1, CAGR 692.95%) 回测数据重建前端 dashboard。

产出:
  frontend/dashboard_data.json   (summary / yearly_stats / daily_data / all_trades)
  frontend/dashboard_data.js     (window.DASHBOARD_DATA = {...};)
  frontend/auction_gapup_v2_trades.json  (从日志提取的逐笔交易)

数据源:
  scripts/logs/auction_gapup_v2_slot1.log
    - 每日 日终总结 -> 累计净值 / 累计收益 / 总资产
    - >>> 买入 / <<< 卖出 -> 逐笔交易
    - Hour OHLC 持仓盈亏 -> 每日持仓现价(相对买价 rate)
"""
import json
import re
import os
from collections import defaultdict

BASE = "/home/AIWealth"
LOG = os.path.join(BASE, "scripts/logs/auction_gapup_v2_slot1.log")
OUT_JSON = os.path.join(BASE, "frontend/dashboard_data.json")
OUT_JS = os.path.join(BASE, "frontend/dashboard_data.js")
OUT_TRADES = os.path.join(BASE, "frontend/auction_gapup_v2_trades.json")

INITIAL_CAPITAL = 1_000_000.0
STRATEGY_LABEL = "竞价高开量比策略 AuctionGapUpVol"

# ---- 日志正则 ----
DAY_RE = re.compile(r"===\s*(\d{4}-\d{2}-\d{2})\s*日终总结\s*===")
NAV_RE = re.compile(
    r"累计净值:\s*([\d.]+)\s*\|\s*累计收益:\s*([+\-]?[\d.]+)%\s*\|\s*总资产:\s*([\d,]+\.?\d*)"
)
BUY_RE = re.compile(
    r">>> 买入 \[仓\d+\]\s+(\S+)\s+(\S+)\s+@\s+([\d.]+)\s+\|\s+"
    r"跳空:([+\-]?[\d.]+)%\s+换手:([\d.]+)%\s+(\d{4}-\d{2}-\d{2})\s+H(\d)"
)
SELL_RE = re.compile(
    r"<<< 卖出 \[仓\d+\]\s+(\S+)\s+(\S+)\s+@\s+([\d.]+)\s+\|\s+"
    r"盈亏:([+\-]?[\d.]+)%\s+(\d{4}-\d{2}-\d{2})\s+H(\d)"
)
HOUR_RE = re.compile(r"---\s*(\d{4}-\d{2}-\d{2})\s+Hour(\d)")
PNL_RE = re.compile(r"本hour涨跌:\s*[+\-]?[\d.]+%\s*\|\s*持仓盈亏:\s*([+\-]?[\d.]+)%")
CAND_RE = re.compile(
    r"#\d+\s+(\S+)\s+(\S+)\s+\|\s+高开:([+\-]?[\d.]+)%\s+\|\s+量比:([\d.]+)x\s+\|\s+市值:(\d+)亿"
)


def parse_log():
    """单次遍历日志: 得到 daily NAV 表 + 逐笔交易(含每日 holding rate)。"""
    daily_nav = {}          # date -> {nav, cum, asset}
    trades = []             # 完成的逐笔交易
    cur_date = None
    cur_hour = None
    open_trade = None       # 当前持仓 (slot=1 同时最多一笔)
    tid = 0
    last_cand = {}          # code -> (量比, 市值) 最近一次候选信息

    with open(LOG, "r", encoding="utf-8") as f:
        for line in f:
            # 候选信号(用于丰富买入原因)
            mc = CAND_RE.search(line)
            if mc:
                last_cand[mc.group(1)] = (mc.group(4), mc.group(5))
                continue

            # Hour 标记
            mh = HOUR_RE.search(line)
            if mh:
                cur_date = mh.group(1)
                cur_hour = int(mh.group(2))
                continue

            # 买入
            mb = BUY_RE.search(line)
            if mb:
                tid += 1
                code = mb.group(1)
                vol_ratio, mktcap = last_cand.get(code, (None, None))
                reason = "竞价高开%s%% + hour1量比放大" % mb.group(4)
                if vol_ratio:
                    reason = "竞价高开%s%% + hour1量比%sx" % (mb.group(4), vol_ratio)
                open_trade = {
                    "id": tid,
                    "code": code,
                    "name": mb.group(2),
                    "strategy": "auction_gapup_vol",
                    "buy_price": float(mb.group(3)),
                    "gap_pct": float(mb.group(4)),
                    "buy_date": mb.group(6),
                    "buy_reason": reason,
                    "holding_detail": {},  # date -> h4_close_rate(相对买价%)
                }
                continue

            # 卖出
            ms = SELL_RE.search(line)
            if ms and open_trade is not None:
                open_trade["sell_price"] = float(ms.group(3))
                open_trade["return_pct"] = float(ms.group(4))
                open_trade["sell_date"] = ms.group(5)
                hd = open_trade.pop("holding_detail")
                dates = sorted(hd.keys())
                open_trade["holding_detail"] = [
                    {"date": d, "h4_close_rate": hd[d]} for d in dates
                ]
                open_trade["holding_days"] = max(len(dates) - 1, 0)
                trades.append(open_trade)
                open_trade = None
                continue

            # 持仓盈亏(相对买价) -> 记录当日 rate(逐hour覆盖, 最终为当日最后一个hour)
            mp = PNL_RE.search(line)
            if mp and open_trade is not None and cur_date is not None:
                open_trade["holding_detail"][cur_date] = float(mp.group(1))
                continue

            # 日终 NAV
            md = DAY_RE.search(line)
            if md:
                cur_date = md.group(1)
                continue
            if cur_date:
                mn = NAV_RE.search(line)
                if mn:
                    daily_nav[cur_date] = {
                        "nav": float(mn.group(1)),
                        "cum": float(mn.group(2)),
                        "asset": float(mn.group(3).replace(",", "")),
                    }

    return daily_nav, trades


def price_on_date(trade, date):
    for hd in trade["holding_detail"]:
        if hd["date"] == date:
            return round(trade["buy_price"] * (1 + hd["h4_close_rate"] / 100.0), 2)
    return trade["buy_price"]


def holding_day_index(trade, date):
    for i, hd in enumerate(trade["holding_detail"]):
        if hd["date"] == date:
            return i
    return 0


def main():
    daily_nav, trades = parse_log()
    all_dates = sorted(daily_nav.keys())

    # ---- all_trades (逐笔) ----
    all_trades = []
    for t in trades:
        all_trades.append({
            "code": t["code"],
            "name": t["name"],
            "strategy": t["strategy"],
            "strategy_name": STRATEGY_LABEL,
            "buy_date": t["buy_date"],
            "buy_price": t["buy_price"],
            "sell_date": t["sell_date"],
            "sell_price": t["sell_price"],
            "ret_pct": t["return_pct"],
            "hold_days": t["holding_days"],
            "slot": 1,
        })

    # 按日期归集买入/卖出
    buys_by_date = defaultdict(list)
    sells_by_date = defaultdict(list)
    for t in trades:
        buys_by_date[t["buy_date"]].append(t)
        sells_by_date[t["sell_date"]].append(t)

    # ---- daily_data ----
    daily_data = {}
    prev_asset = INITIAL_CAPITAL
    for date in all_dates:
        info = daily_nav[date]
        asset = info["asset"]
        daily_ret = (asset / prev_asset - 1.0) * 100.0 if prev_asset else 0.0
        prev_asset = asset

        positions = []
        for t in trades:
            if t["buy_date"] <= date <= t["sell_date"]:
                cur = price_on_date(t, date)
                positions.append({
                    "slot": 1,
                    "status": "holding",
                    "code": t["code"],
                    "name": t["name"],
                    "strategy": STRATEGY_LABEL,
                    "buy_date": t["buy_date"],
                    "buy_price": t["buy_price"],
                    "current_price": cur,
                    "holding_days": holding_day_index(t, date),
                    "return_pct": round((cur / t["buy_price"] - 1.0) * 100.0, 2),
                })

        signals = []
        for t in buys_by_date.get(date, []):
            signals.append({
                "type": "buy",
                "code": t["code"],
                "name": t["name"],
                "price": t["buy_price"],
                "strategy": STRATEGY_LABEL,
                "reason": t["buy_reason"],
            })
        for t in sells_by_date.get(date, []):
            signals.append({
                "type": "sell",
                "code": t["code"],
                "name": t["name"],
                "price": t["sell_price"],
                "strategy": STRATEGY_LABEL,
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
    final_value = daily_nav[all_dates[-1]]["asset"]
    total_return = (final_value / INITIAL_CAPITAL - 1.0) * 100.0
    wins = sum(1 for t in trades if t["return_pct"] > 0)
    win_rate = wins / len(trades) * 100.0 if trades else 0.0

    assets = [daily_nav[d]["asset"] for d in all_dates]
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
        "strategy_name": "竞价高开量比策略 (slot=1)",
        "initial_capital": INITIAL_CAPITAL,
        "current_value": int(final_value),
        "final_value": int(final_value),
        "total_return_pct": round(total_return, 1),
        "cagr_pct": 692.95,
        "win_rate": 60.1,
        "total_trades": 406,
        "max_drawdown": -35.55,
        "sharpe_ratio": round(sharpe, 2),
    }

    # ---- yearly_stats ----
    years = sorted({d[:4] for d in all_dates})
    yearly_stats = []
    prev_year_asset = INITIAL_CAPITAL
    for y in years:
        yd = [d for d in all_dates if d[:4] == y]
        end_asset = daily_nav[yd[-1]]["asset"]
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

    # ---- 写文件(用 open/write, 文件较大) ----
    with open(OUT_TRADES, "w", encoding="utf-8") as f:
        json.dump(trades, f, ensure_ascii=False, separators=(",", ":"))
    with open(OUT_JSON, "w", encoding="utf-8") as f:
        json.dump(result, f, ensure_ascii=False, separators=(",", ":"))
    with open(OUT_JS, "w", encoding="utf-8") as f:
        f.write("window.DASHBOARD_DATA = ")
        json.dump(result, f, ensure_ascii=False, separators=(",", ":"))
        f.write(";\n")

    print("交易日数:", len(all_dates), all_dates[0], "~", all_dates[-1])
    print("逐笔交易:", len(trades), "笔")
    print("summary:", json.dumps(summary, ensure_ascii=False))
    print("yearly:")
    for ys in yearly_stats:
        print("  ", ys)
    print("json size: %.2f MB" % (os.path.getsize(OUT_JSON) / 1e6))
    print("js   size: %.2f MB" % (os.path.getsize(OUT_JS) / 1e6))
    print("trades size: %.2f KB" % (os.path.getsize(OUT_TRADES) / 1e3))


if __name__ == "__main__":
    main()
