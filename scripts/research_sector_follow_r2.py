#!/usr/bin/env python3
"""
板块龙头跟风补涨策略 - 候选股研究脚本 (rule2 阶段一)

策略思路:
  某板块(创业板/科创板)当日出现 >=3 只涨停 -> 该板块"热门日", 资金聚集。
  同板块、同日涨幅 3~8%(接近涨停但未封板)的"准龙头/跟风股", 次日往往有补涨溢出。
  次日(T+1)若高开 >= 1%, 则在 T+1 hour1_open 买入, 观察补涨表现。

T+1 合规:
  - T日涨停数、T日补涨候选(3~8%)在 T日收盘后完全确定。
  - T+1高开在 9:25 竞价确定, 买入价 = T+1 open (= hour1_open)。
  - 全程不使用未来数据。

数据库无行业/板块字段, 采用"交易所板块"近似:
  - 创业板 GEM : sz.300xxx / sz.301xxx (涨跌停 ±20%)
  - 科创板 STAR: sh.688xxx           (涨跌停 ±20%)

用法:
  python3 research_sector_follow_r2.py 2024-09   # 单月(打印明细)
  python3 research_sector_follow_r2.py all        # 全周期(仅汇总+逐年)
"""
import sys
import sqlite3
import os

# ============ 配置区 ============
DB_PATH = "/home/AIWealth/data/stocks.db"
LOG_PATH = "/home/AIWealth/scripts/logs/sector_follow_r2.log"

HOT_LIMIT_COUNT = 3        # 板块热门日: 当日涨停股数 >= 该值
FOLLOW_RATE_MIN = 3.0      # 补涨候选涨幅下限(%)
FOLLOW_RATE_MAX = 8.0      # 补涨候选涨幅上限(%, 需 < 涨停避免封板)
GAP_UP_THRESHOLD = 1.0     # T+1 高开阈值(%)
MAX_MCAP = 100.0           # 流通市值上限(亿元)
CONTEXT_DAYS = 5           # 前后查看天数

# 止盈止损扫描配置(用于汇总分析)
TP_LEVELS = [3.0, 5.0, 8.0, 10.0]   # 止盈档位(%)
SL_LEVELS = [-3.0, -5.0, -8.0]      # 止损档位(%)
HOLD_DAYS = 3                        # 最大持有天数(用于止盈止损模拟)
# ================================

_OUT = None


def w(s=""):
    """写入日志文件"""
    _OUT.write(s + "\n")


def progress(s):
    """终端进度(stderr)"""
    sys.stderr.write(s + "\n")
    sys.stderr.flush()


# ---------------- 基础工具 ----------------

def get_board(code):
    """返回板块标识, 仅关注创业板/科创板; 其他返回 None"""
    if code.startswith("sz.300") or code.startswith("sz.301"):
        return "GEM"   # 创业板
    if code.startswith("sh.688"):
        return "STAR"  # 科创板
    return None


BOARD_NAME = {"GEM": "创业板", "STAR": "科创板"}


def get_limit_ratio(code):
    """板块涨跌停比例"""
    if code.startswith("sz.300") or code.startswith("sz.301"):
        return 0.20
    if code.startswith("sh.688"):
        return 0.20
    if code.startswith("bj."):
        return 0.30
    return 0.10


def get_limit_up_price(code, preclose):
    return round(preclose * (1 + get_limit_ratio(code)), 2)


def is_limit_up(row):
    """涨停判定: close >= round(preclose*(1+ratio),2)"""
    preclose = row["preclose"]
    close = row["close"]
    if preclose is None or close is None or preclose <= 0:
        return False
    return close >= get_limit_up_price(row["code"], preclose)


def is_st(row):
    if row["isST"] == 1:
        return True
    name = row["code_name"] or ""
    if "ST" in name.upper():
        return True
    return False


def calc_mcap(amount, turn):
    """流通市值(亿元) = amount * 100 / turn / 1e8"""
    if amount is None or turn is None or turn <= 0:
        return None
    return amount * 100.0 / turn / 1e8


def get_trading_days(cursor, month_str):
    cursor.execute(
        "SELECT DISTINCT date FROM stock_kline WHERE date LIKE ? ORDER BY date",
        (month_str + "%",),
    )
    return [r[0] for r in cursor.fetchall()]


def get_all_trading_days(cursor):
    cursor.execute("SELECT DISTINCT date FROM stock_kline ORDER BY date")
    return [r[0] for r in cursor.fetchall()]


# ---------------- 核心逻辑 ----------------

def load_day(cursor, date):
    """加载某交易日全市场(仅创业板/科创板)行情, 返回 dict list"""
    cursor.execute(
        """
        SELECT date, code, code_name, preclose, open, high, low, close, close_rate,
               volume, amount, turn, isST,
               hour1_open, hour1_high, hour1_low, hour1_close,
               hour2_open, hour2_high, hour2_low, hour2_close,
               hour3_open, hour3_high, hour3_low, hour3_close,
               hour4_open, hour4_high, hour4_low, hour4_close
        FROM stock_kline WHERE date = ?
        """,
        (date,),
    )
    cols = [d[0] for d in cursor.description]
    rows = []
    for r in cursor.fetchall():
        d = dict(zip(cols, r))
        if get_board(d["code"]) is None:
            continue
        rows.append(d)
    return rows


def find_hot_boards(day_rows):
    """统计每个板块涨停数, 返回 {board: limit_up_count} 中 >=阈值 的部分"""
    counts = {}
    for d in day_rows:
        if is_limit_up(d):
            b = get_board(d["code"])
            counts[b] = counts.get(b, 0) + 1
    return {b: c for b, c in counts.items() if c >= HOT_LIMIT_COUNT}, counts


def find_follow_candidates(day_rows, hot_boards):
    """补涨候选: 热门板块内, 当日涨 3~8%, 市值<100亿, 非ST, 非涨停"""
    cands = []
    for d in day_rows:
        b = get_board(d["code"])
        if b not in hot_boards:
            continue
        if is_st(d):
            continue
        if is_limit_up(d):
            continue
        cr = d["close_rate"]
        if cr is None or not (FOLLOW_RATE_MIN <= cr <= FOLLOW_RATE_MAX):
            continue
        mcap = calc_mcap(d["amount"], d["turn"])
        if mcap is not None and mcap > MAX_MCAP:
            continue
        d["_board"] = b
        d["_mcap"] = mcap
        cands.append(d)
    return cands


def get_row(cursor, code, date):
    cursor.execute(
        """
        SELECT date, code, code_name, preclose, open, high, low, close, close_rate,
               volume, amount, turn, isST,
               hour1_open, hour1_high, hour1_low, hour1_close,
               hour2_open, hour2_high, hour2_low, hour2_close,
               hour3_open, hour3_high, hour3_low, hour3_close,
               hour4_open, hour4_high, hour4_low, hour4_close
        FROM stock_kline WHERE code = ? AND date = ?
        """,
        (code, date),
    )
    cols = [d[0] for d in cursor.description]
    r = cursor.fetchone()
    return dict(zip(cols, r)) if r else None


def get_context_hours(cursor, code, all_days, center_idx, n=5):
    start_idx = max(0, center_idx - n)
    end_idx = min(len(all_days) - 1, center_idx + n)
    days_range = all_days[start_idx:end_idx + 1]
    if not days_range:
        return []
    placeholders = ",".join(["?"] * len(days_range))
    cursor.execute(
        f"""
        SELECT date, open, high, low, close, preclose, close_rate, turn,
               hour1_open, hour1_high, hour1_low, hour1_close,
               hour2_open, hour2_high, hour2_low, hour2_close,
               hour3_open, hour3_high, hour3_low, hour3_close,
               hour4_open, hour4_high, hour4_low, hour4_close
        FROM stock_kline WHERE code = ? AND date IN ({placeholders})
        ORDER BY date
        """,
        [code] + days_range,
    )
    cols = [d[0] for d in cursor.description]
    return [dict(zip(cols, row)) for row in cursor.fetchall()]


def print_candidate(cand, next_row, gap_pct, context_rows, buy_day):
    """打印候选股明细(围绕买入日 T+1)"""
    code = cand["code"]
    name = cand["code_name"] or ""
    board = BOARD_NAME.get(cand["_board"], cand["_board"])
    buy_price = next_row["open"]
    mcap = cand.get("_mcap")
    mcap_str = f"{mcap:.1f}亿" if mcap is not None else "N/A"

    w(f"\n--- {code} ({name}) [{board}] ---")
    w(f"  T日({cand['date']}): 涨幅={cand['close_rate']:+.2f}%  "
      f"close={cand['close']:.2f}  turn={cand['turn']:.2f}%  市值={mcap_str}")
    w(f"  T+1({buy_day}): 高开={gap_pct:+.2f}%  open={buy_price:.2f}  "
      f"(preclose={next_row['preclose']:.2f})")
    w(f"  买入价(T+1 hour1_open): {buy_price:.2f}")
    w()
    w(f"  买入日前{CONTEXT_DAYS}日+后{CONTEXT_DAYS}日 hour级明细:")
    w(f"  {'日期':<12}| {'hour':<5}| {'open':<8}| {'high':<8}| "
      f"{'low':<8}| {'close':<8}| vs买入价")
    w(f"  {'-'*12}+{'-'*6}+{'-'*9}+{'-'*9}+{'-'*9}+{'-'*9}+{'-'*10}")
    for row in context_rows:
        d = row["date"]
        is_buy = (d == buy_day)
        marker = " ← 买入日" if is_buy else ""
        for h in range(1, 5):
            ho = row.get(f"hour{h}_open")
            hh = row.get(f"hour{h}_high")
            hl = row.get(f"hour{h}_low")
            hc = row.get(f"hour{h}_close")
            if ho is None or ho == 0:
                continue
            vs_buy = (hc - buy_price) / buy_price * 100 if buy_price > 0 else 0
            tag = marker if h == 1 and is_buy else ""
            w(f"  {d:<12}| h{h:<4}| {ho:<8.2f}| {hh:<8.2f}| {hl:<8.2f}| "
              f"{hc:<8.2f}| {vs_buy:+.2f}%{tag}")

    # 关键指标
    buy_day_close = next_row["close"]
    buy_day_high = next_row["high"]
    w()
    w(f"  关键指标(买入价={buy_price:.2f}):")
    w(f"    买入日最高: {buy_day_high:.2f} "
      f"({(buy_day_high - buy_price) / buy_price * 100:+.2f}%)")
    w(f"    买入日收盘: {buy_day_close:.2f} "
      f"({(buy_day_close - buy_price) / buy_price * 100:+.2f}%)")
    future_rows = [r for r in context_rows if r["date"] > buy_day]
    if future_rows:
        highs = [r["high"] for r in future_rows if r["high"] and r["high"] > 0]
        lows = [r["low"] for r in future_rows if r["low"] and r["low"] > 0]
        if highs:
            w(f"    后续{len(future_rows)}日最高: {max(highs):.2f} "
              f"({(max(highs) - buy_price) / buy_price * 100:+.2f}%)")
        if lows:
            w(f"    后续{len(future_rows)}日最低: {min(lows):.2f} "
              f"({(min(lows) - buy_price) / buy_price * 100:+.2f}%)")


def simulate_tp_sl(buy_price, day_rows, tp, sl, hold_days):
    """
    基于买入日起 hold_days 天的 hour 级路径模拟止盈止损。
    day_rows: 买入日及之后的 dict list(含 hourN_high/low/close), 已按日期升序。
    返回 (exit_ret_pct, exit_reason)
    保守假设: 同一 hour 内若同时触及止盈和止损, 按止损优先(先看low)。
    """
    tp_price = buy_price * (1 + tp / 100.0)
    sl_price = buy_price * (1 + sl / 100.0)
    for di, row in enumerate(day_rows[:hold_days]):
        for h in range(1, 5):
            hl = row.get(f"hour{h}_low")
            hh = row.get(f"hour{h}_high")
            hc = row.get(f"hour{h}_close")
            if hl is None or hl == 0:
                continue
            # 止损优先(保守)
            if hl <= sl_price:
                return sl, f"止损@D{di+1}h{h}"
            if hh is not None and hh >= tp_price:
                return tp, f"止盈@D{di+1}h{h}"
        # 收盘持有
    # 到期按最后一日收盘价退出
    last_close = None
    for row in reversed(day_rows[:hold_days]):
        for h in range(4, 0, -1):
            hc = row.get(f"hour{h}_close")
            if hc and hc > 0:
                last_close = hc
                break
        if last_close:
            break
    if last_close:
        return (last_close - buy_price) / buy_price * 100, f"到期{hold_days}日"
    return 0.0, "无数据"


# ---------------- 主流程 ----------------

def run(month_arg):
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()

    all_days = get_all_trading_days(cursor)
    is_all = (month_arg == "all")
    print_detail = not is_all

    if is_all:
        target_days = all_days
    else:
        target_days = get_trading_days(cursor, month_arg)
        if not target_days:
            w(f"错误: 未找到 {month_arg} 的交易日数据")
            conn.close()
            return

    w("=" * 70)
    w("板块龙头跟风补涨策略 - 候选股研究 (rule2 阶段一)")
    w(f"参数: 热门日涨停数>={HOT_LIMIT_COUNT}  补涨涨幅[{FOLLOW_RATE_MIN},"
      f"{FOLLOW_RATE_MAX}]%  T+1高开>={GAP_UP_THRESHOLD}%  市值<{MAX_MCAP}亿")
    w(f"范围: {month_arg}  交易日数: {len(target_days)}")
    w("=" * 70)

    all_records = []       # 全部买入记录(统计)
    hot_day_count = 0      # 出现热门板块的交易日数
    scanned_days = 0

    for today in target_days:
        idx = all_days.index(today) if today in all_days else -1
        if idx < 0 or idx + 1 >= len(all_days):
            continue  # 需要 T+1
        scanned_days += 1
        next_day = all_days[idx + 1]

        day_rows = load_day(cursor, today)
        if not day_rows:
            continue
        hot_boards, all_counts = find_hot_boards(day_rows)
        if not hot_boards:
            continue
        hot_day_count += 1

        cands = find_follow_candidates(day_rows, hot_boards)
        if not cands:
            continue

        if print_detail:
            hot_desc = ", ".join(
                f"{BOARD_NAME[b]}={hot_boards[b]}只" for b in hot_boards)
            w(f"\n{'=' * 12} {today} {'=' * 12}")
            w(f"热门板块: {hot_desc}  补涨候选: {len(cands)}只")

        for cand in cands:
            next_row = get_row(cursor, cand["code"], next_day)
            if not next_row or next_row["preclose"] is None or next_row["preclose"] <= 0:
                continue
            gap_pct = (next_row["open"] - next_row["preclose"]) / next_row["preclose"] * 100

            # 统计需要区分: 是否高开达标
            signal = gap_pct >= GAP_UP_THRESHOLD
            buy_price = next_row["open"]
            if buy_price is None or buy_price <= 0:
                continue

            next_idx = all_days.index(next_day)
            context_rows = get_context_hours(cursor, cand["code"], all_days,
                                             next_idx, CONTEXT_DAYS)

            if signal and print_detail:
                print_candidate(cand, next_row, gap_pct, context_rows, next_day)

            # 收集统计(只统计高开达标的信号)
            if not signal:
                continue

            buy_day_ret = (next_row["close"] - buy_price) / buy_price * 100
            buy_day_max = (next_row["high"] - buy_price) / buy_price * 100
            future_rows = [r for r in context_rows if r["date"] >= next_day]
            fmax = fmin = None
            fut_after = [r for r in future_rows if r["date"] > next_day]
            if fut_after:
                highs = [r["high"] for r in fut_after if r["high"] and r["high"] > 0]
                lows = [r["low"] for r in fut_after if r["low"] and r["low"] > 0]
                if highs:
                    fmax = (max(highs) - buy_price) / buy_price * 100
                if lows:
                    fmin = (min(lows) - buy_price) / buy_price * 100

            # 止盈止损模拟(基于买入日起路径)
            hold_path = [r for r in future_rows]  # 含买入日
            tp_sl_results = {}
            for tp in TP_LEVELS:
                for sl in SL_LEVELS:
                    ret, _ = simulate_tp_sl(buy_price, hold_path, tp, sl, HOLD_DAYS)
                    tp_sl_results[(tp, sl)] = ret

            all_records.append({
                "code": cand["code"],
                "t_date": today,
                "buy_date": next_day,
                "year": next_day[:4],
                "board": cand["_board"],
                "gap": gap_pct,
                "buy_day_ret": buy_day_ret,
                "buy_day_max": buy_day_max,
                "buy_day_positive": 1 if next_row["close"] > buy_price else 0,
                "fmax": fmax,
                "fmin": fmin,
                "tp_sl": tp_sl_results,
            })

    # ---------------- 汇总 ----------------
    w(f"\n\n{'=' * 20} 汇总分析 {'=' * 20}")
    w(f"扫描交易日: {scanned_days}  出现热门板块的日数: {hot_day_count} "
      f"({hot_day_count / scanned_days * 100:.1f}%)" if scanned_days else "无数据")
    w(f"高开达标买入信号总数: {len(all_records)}")
    if hot_day_count:
        w(f"平均每个热门日补涨买入信号: {len(all_records) / hot_day_count:.2f} 个")

    if not all_records:
        w("\n无有效买入信号, 建议放宽条件(降低涨停阈值到2 或 放宽补涨到[2,8]%)。")
        conn.close()
        return

    n = len(all_records)
    avg_ret = sum(r["buy_day_ret"] for r in all_records) / n
    avg_max = sum(r["buy_day_max"] for r in all_records) / n
    pos = sum(r["buy_day_positive"] for r in all_records)
    pos_ratio = pos / n * 100
    fmax_list = [r["fmax"] for r in all_records if r["fmax"] is not None]
    fmin_list = [r["fmin"] for r in all_records if r["fmin"] is not None]
    avg_fmax = sum(fmax_list) / len(fmax_list) if fmax_list else 0
    avg_fmin = sum(fmin_list) / len(fmin_list) if fmin_list else 0

    w(f"\n【买入日表现】(买入价=T+1 hour1_open, 已过滤高开>={GAP_UP_THRESHOLD}%)")
    w(f"  样本数: {n}")
    w(f"  买入日收益(close/buy-1)均值: {avg_ret:+.2f}%")
    w(f"  买入日最大涨幅(high/buy-1)均值: {avg_max:+.2f}%")
    w(f"  买入日收阳(胜率): {pos_ratio:.1f}%")
    w(f"  后续{CONTEXT_DAYS}日最大涨幅均值: {avg_fmax:+.2f}%")
    w(f"  后续{CONTEXT_DAYS}日最大跌幅均值: {avg_fmin:+.2f}%")

    # 止盈止损扫描
    w(f"\n【止盈止损配置扫描】(买入日起最多持有{HOLD_DAYS}日, 止损优先)")
    w(f"  {'止盈%':>6} {'止损%':>6} {'平均收益%':>10} {'胜率%':>8}")
    best = None
    for tp in TP_LEVELS:
        for sl in SL_LEVELS:
            rets = [r["tp_sl"][(tp, sl)] for r in all_records]
            avg = sum(rets) / len(rets)
            win = sum(1 for x in rets if x > 0) / len(rets) * 100
            w(f"  {tp:>6.1f} {sl:>6.1f} {avg:>10.2f} {win:>8.1f}")
            if best is None or avg > best[2]:
                best = (tp, sl, avg, win)
    if best:
        w(f"  → 最优组合: 止盈{best[0]:.1f}% 止损{best[1]:.1f}% "
          f"平均收益{best[2]:+.2f}% 胜率{best[3]:.1f}%")

    # 逐年稳定性
    w(f"\n【逐年稳定性】")
    w(f"  {'年份':>6} {'信号数':>7} {'买入日收益%':>11} {'胜率%':>8} "
      f"{'后5日最大%':>11} {'后5日最小%':>11}")
    years = sorted(set(r["year"] for r in all_records))
    for y in years:
        yr = [r for r in all_records if r["year"] == y]
        y_ret = sum(x["buy_day_ret"] for x in yr) / len(yr)
        y_pos = sum(x["buy_day_positive"] for x in yr) / len(yr) * 100
        y_fmax = [x["fmax"] for x in yr if x["fmax"] is not None]
        y_fmin = [x["fmin"] for x in yr if x["fmin"] is not None]
        yfmax = sum(y_fmax) / len(y_fmax) if y_fmax else 0
        yfmin = sum(y_fmin) / len(y_fmin) if y_fmin else 0
        w(f"  {y:>6} {len(yr):>7} {y_ret:>11.2f} {y_pos:>8.1f} "
          f"{yfmax:>11.2f} {yfmin:>11.2f}")

    # 按板块
    w(f"\n【分板块统计】")
    for b in ["GEM", "STAR"]:
        br = [r for r in all_records if r["board"] == b]
        if not br:
            continue
        b_ret = sum(x["buy_day_ret"] for x in br) / len(br)
        b_pos = sum(x["buy_day_positive"] for x in br) / len(br) * 100
        w(f"  {BOARD_NAME[b]}: 信号{len(br)}  买入日收益{b_ret:+.2f}%  胜率{b_pos:.1f}%")

    conn.close()
    w(f"\n完成。")


def main():
    global _OUT
    if len(sys.argv) < 2:
        print("用法: python3 research_sector_follow_r2.py <月份|all>")
        print("示例: python3 research_sector_follow_r2.py 2024-09")
        sys.exit(1)

    month_arg = sys.argv[1]
    os.makedirs(os.path.dirname(LOG_PATH), exist_ok=True)
    _OUT = open(LOG_PATH, "w", encoding="utf-8")
    progress(f"开始研究: {month_arg}  日志 -> {LOG_PATH}")
    try:
        run(month_arg)
    finally:
        _OUT.close()
    progress("完成。")


if __name__ == "__main__":
    main()
