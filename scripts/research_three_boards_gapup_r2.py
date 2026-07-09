#\!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
科创板/创业板/北交所 跳空高开统一策略研究 (rule2 流程, R2)
思路: 三个高波动板块(科创±20%/创业±20%/北交±30%)的跳空高开配合前置条件
      (前日大跌/前日涨停/连续缩量/龙回头/换手率变异)寻找高alpha。
      P1-P5 × 3板块 × 2市值档 × 3高开幅度 = 90种组合, 统计 T+1/T+2/T+5 收益胜率。
买入: today hour1_open。T+0合规: 前置条件仅用昨日及之前信号。
用法: python3 research_three_boards_gapup_r2.py 2024-09 | all
"""
import sys
import os
import sqlite3
from collections import defaultdict

# ================= 配置区 =================
DB_PATH = "/home/AIWealth/data/stocks.db"
LOG_PATH = "/home/AIWealth/scripts/logs/three_boards_gapup_r2.log"

BOARDS = {
    "chuangye": {"cn": "创业板", "prefixes": ("sz.300", "sz.301"), "limit_pct": 0.20,
                 "bigdrop": -5.0, "gap_bands": [(2, 5, "2-5%"), (5, 8, "5-8%"), (8, 12, "8-12%")]},
    "kechuang": {"cn": "科创板", "prefixes": ("sh.688",), "limit_pct": 0.20,
                 "bigdrop": -7.0, "gap_bands": [(2, 5, "2-5%"), (5, 8, "5-8%"), (8, 12, "8-12%")]},
    "beijiao": {"cn": "北交所", "prefixes": ("bj.",), "limit_pct": 0.30,
                "bigdrop": -10.0, "gap_bands": [(3, 8, "3-8%"), (8, 15, "8-15%"), (15, 20, "15-20%")]},
}
BOARD_ORDER = ["chuangye", "kechuang", "beijiao"]

MCAP_BANDS = [("small", "<50亿", 0.0, 5e9), ("mid", "50-200亿", 5e9, 2e10)]
MCAP_ORDER = ["small", "mid"]

P_ORDER = ["P1", "P2", "P3", "P4", "P5"]
P_DESC = {
    "P1": "前日大阴(创跌>=5%/科>=7%/北>=10%)",
    "P2": "前日涨停(首板)",
    "P3": "近3日缩量(vol<prior5*0.7)+今日高开突破",
    "P4": "5日内高点回调>10%后企稳",
    "P5": "前日换手<0.5%+今日突然高开",
}

MIN_TURN = 0.5
MIN_AMOUNT = 2e6
MIN_SAMPLE = 20
TOP_N = 10
MAX_DETAIL_CAND = 6
CONTEXT_DAYS = 5
# ==========================================

_LOG_FH = None


def log(msg=""):
    _LOG_FH.write(str(msg) + "\n")


def progress(msg):
    print(msg, flush=True)


def detect_board(code):
    for b in BOARD_ORDER:
        for p in BOARDS[b]["prefixes"]:
            if code.startswith(p):
                return b
    return None


def is_st(row):
    if row.get("isST") == 1:
        return True
    name = row.get("code_name") or ""
    return "ST" in name.upper()


def get_limit_pct(code, date):
    # 创业板注册制±20%自2020-08-24起, 之前为±10%; 科创板始终±20%; 北交±30%
    if code.startswith("sz.300") or code.startswith("sz.301"):
        return 0.20 if date >= "2020-08-24" else 0.10
    if code.startswith("sh.688"):
        return 0.20
    if code.startswith("bj."):
        return 0.30
    return 0.10


def is_limit_up(row, bconf):
    pc = row.get("preclose")
    c = row.get("close")
    if not pc or pc <= 0 or c is None:
        return False
    return c >= round(pc * (1 + get_limit_pct(row["code"], row["date"])), 2) - 0.001


def is_yizi_or_locked(row, bconf):
    o, h, l, c = row.get("open"), row.get("high"), row.get("low"), row.get("close")
    pc = row.get("preclose")
    if None in (o, h, l, c) or not pc or pc <= 0:
        return True
    limit_price = round(pc * (1 + get_limit_pct(row["code"], row["date"])), 2)
    if o == h == l == c and c >= limit_price - 0.001:
        return True
    if o >= limit_price - 0.001 and o == h:
        return True
    return False


def match_gap_band(bconf, gap):
    for lo, hi, label in bconf["gap_bands"]:
        if lo <= gap < hi:
            return label
    return None


def est_mcap(y):
    turn = y.get("turn")
    amount = y.get("amount")
    if not turn or turn <= 0 or not amount or amount <= 0:
        return None
    return amount * 100.0 / turn


def match_mcap(mcap):
    if mcap is None:
        return None
    for key, _, lo, hi in MCAP_BANDS:
        if lo <= mcap < hi:
            return key
    return None


def liquidity_ok(y, need_high_turn=True):
    turn = y.get("turn")
    amount = y.get("amount")
    if amount is None or amount < MIN_AMOUNT:
        return False
    if turn is None:
        return False
    if need_high_turn and turn < MIN_TURN:
        return False
    return True


def cond_P1(rows, i, bconf):
    y = rows[i - 1]
    if not liquidity_ok(y):
        return False
    if y.get("close_rate") is None:
        return False
    return y["close_rate"] <= bconf["bigdrop"]


def cond_P2(rows, i, bconf):
    if i < 2:
        return False
    y, y2 = rows[i - 1], rows[i - 2]
    if not liquidity_ok(y):
        return False
    if not is_limit_up(y, bconf):
        return False
    if is_limit_up(y2, bconf):
        return False
    return True


def cond_P3(rows, i, bconf):
    if i < 8:
        return False
    y = rows[i - 1]
    if not liquidity_ok(y):
        return False
    recent3 = [rows[i - 3]["volume"], rows[i - 2]["volume"], rows[i - 1]["volume"]]
    prior5 = [rows[j]["volume"] for j in range(i - 8, i - 3)]
    if any(v is None for v in recent3) or any(v is None for v in prior5):
        return False
    prior5_avg = sum(prior5) / 5.0
    if prior5_avg <= 0:
        return False
    return (sum(recent3) / 3.0) < 0.7 * prior5_avg


def cond_P4(rows, i, bconf):
    if i < 5:
        return False
    y = rows[i - 1]
    if not liquidity_ok(y):
        return False
    window = rows[i - 5:i]
    highs = [r["high"] for r in window if r.get("high")]
    if not highs:
        return False
    mx = max(highs)
    if mx <= 0 or y.get("close") is None:
        return False
    if (y["close"] - mx) / mx * 100.0 > -10.0:
        return False
    if y.get("close_rate") is None or y["close_rate"] < -2.0:
        return False
    return True


def cond_P5(rows, i, bconf):
    y = rows[i - 1]
    if not liquidity_ok(y, need_high_turn=False):
        return False
    turn = y.get("turn")
    if turn is None or turn >= MIN_TURN:
        return False
    return True


COND_FUNCS = {"P1": cond_P1, "P2": cond_P2, "P3": cond_P3, "P4": cond_P4, "P5": cond_P5}


def future_return(rows, i, buy, k):
    if i + k >= len(rows):
        return None
    c = rows[i + k].get("close")
    if c is None:
        return None
    return (c - buy) / buy * 100.0


def future_extremes(rows, i, buy):
    end = min(i + 6, len(rows))
    highs = [rows[j]["high"] for j in range(i + 1, end) if rows[j].get("high")]
    lows = [rows[j]["low"] for j in range(i + 1, end) if rows[j].get("low")]
    fmax = (max(highs) - buy) / buy * 100.0 if highs else None
    fmin = (min(lows) - buy) / buy * 100.0 if lows else None
    return fmax, fmin


def load_data(cursor):
    cursor.execute("""
        SELECT date, code, code_name, preclose, open, high, low, close,
               close_rate, volume, amount, turn, isST, hour1_open
        FROM stock_kline
        WHERE code LIKE 'sz.300%' OR code LIKE 'sz.301%'
           OR code LIKE 'sh.688%' OR code LIKE 'bj.%'
        ORDER BY code, date
    """)
    cols = [d[0] for d in cursor.description]
    data = defaultdict(list)
    for row in cursor.fetchall():
        d = dict(zip(cols, row))
        data[d["code"]].append(d)
    return data


def agg_stats(samples):
    n = len(samples)
    out = {"n": n}
    for k in ("t1", "t2", "t5"):
        vals = [s[k] for s in samples if s[k] is not None]
        if vals:
            out[k + "_mean"] = sum(vals) / len(vals)
            out[k + "_win"] = sum(1 for v in vals if v > 0) / len(vals) * 100.0
        else:
            out[k + "_mean"] = None
            out[k + "_win"] = None
    fmax = [s["fmax"] for s in samples if s["fmax"] is not None]
    fmin = [s["fmin"] for s in samples if s["fmin"] is not None]
    out["fmax_avg"] = sum(fmax) / len(fmax) if fmax else None
    out["fmin_avg"] = sum(fmin) / len(fmin) if fmin else None
    return out


def fmt(v, suffix="%"):
    return f"{v:+.2f}{suffix}" if v is not None else "  N/A "


def fmt_win(v):
    return f"{v:.1f}%" if v is not None else " N/A "


def print_hour_context(cursor, code, name, buy, dates, center):
    if not dates:
        return
    ph = ",".join(["?"] * len(dates))
    cursor.execute(f"""
        SELECT date,
               hour1_open,hour1_high,hour1_low,hour1_close,
               hour2_open,hour2_high,hour2_low,hour2_close,
               hour3_open,hour3_high,hour3_low,hour3_close,
               hour4_open,hour4_high,hour4_low,hour4_close
        FROM stock_kline WHERE code=? AND date IN ({ph}) ORDER BY date
    """, [code] + dates)
    cols = [d[0] for d in cursor.description]
    rows = [dict(zip(cols, r)) for r in cursor.fetchall()]
    log(f"      {'日期':<11}|hour| {'open':<8}| {'high':<8}| {'low':<8}| {'close':<8}| vs买入")
    log(f"      {'-'*11}+----+{'-'*9}+{'-'*9}+{'-'*9}+{'-'*9}+--------")
    for r in rows:
        d = r["date"]
        mark = " <=买入日" if d == center else ""
        for h in range(1, 5):
            ho = r.get(f"hour{h}_open")
            if ho is None or ho == 0:
                continue
            hh = r.get(f"hour{h}_high")
            hl = r.get(f"hour{h}_low")
            hc = r.get(f"hour{h}_close")
            vs = (hc - buy) / buy * 100.0 if buy else 0
            tag = mark if h == 1 else ""
            log(f"      {d:<11}|h{h}  | {ho:<8.2f}| {hh:<8.2f}| {hl:<8.2f}| {hc:<8.2f}| {vs:+.2f}%{tag}")


def suggest_tp_sl(st):
    fmax = st["fmax_avg"]
    fmin = st["fmin_avg"]
    if fmax is None or fmin is None:
        return "数据不足"
    tp = round(fmax * 0.55, 1)
    sl = round(fmin * 0.55, 1)
    best_day, best_ret = None, None
    for k, lab in (("t1", "T+1"), ("t2", "T+2"), ("t5", "T+5")):
        v = st.get(k + "_mean")
        if v is not None and (best_ret is None or v > best_ret):
            best_ret, best_day = v, lab
    return (f"止盈≈{tp:+.1f}% 止损≈{sl:+.1f}% "
            f"(5日均最高{fmax:+.1f}%/均最低{fmin:+.1f}%); 最佳持有={best_day}({best_ret:+.2f}%)")


def main():
    if len(sys.argv) < 2:
        print("用法: python3 research_three_boards_gapup_r2.py <月份|all>")
        sys.exit(1)

    arg = sys.argv[1]
    os.makedirs(os.path.dirname(LOG_PATH), exist_ok=True)

    global _LOG_FH
    _LOG_FH = open(LOG_PATH, "w", encoding="utf-8")

    progress("[1/4] 连接数据库并加载三板块数据...")
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    data = load_data(cursor)
    n_codes = len(data)
    progress(f"      载入 {n_codes} 只股票")

    def is_target(d):
        return True if arg == "all" else d.startswith(arg)

    log("=" * 78)
    log("科创板/创业板/北交所 跳空高开统一策略研究 (rule2, R2)")
    log("=" * 78)
    log(f"范围: {arg}    载入股票数: {n_codes}")
    log("买入: today hour1_open   |   收益: T+N close/买入-1")
    log("市值估算: 流通市值≈昨日amount*100/turn  |  样本不足阈值: n<%d" % MIN_SAMPLE)
    log("合规说明: 前置条件仅用昨日及之前信号; P3的'今日放量'不可在hour1_open前")
    log("          获知(未来数据),故以'今日高开突破'替代. T+N为研究前瞻指标.")
    board_counts = defaultdict(int)
    for code in data:
        b = detect_board(code)
        if b:
            board_counts[b] += 1
    log("-" * 78)
    log("板块数据可用性:")
    for b in BOARD_ORDER:
        cnt = board_counts.get(b, 0)
        note = "" if cnt > 0 else "  <= 数据库无该板块数据, 相关组合标记[无数据]"
        log(f"  {BOARDS[b]['cn']:<6} ({'/'.join(BOARDS[b]['prefixes'])}): {cnt} 只{note}")
    log("=" * 78)

    progress("[2/4] 遍历候选股, 统计90种组合...")
    combos = defaultdict(list)
    code_rows = {}
    scanned = 0

    for code, rows in data.items():
        board = detect_board(code)
        if board is None:
            continue
        bconf = BOARDS[board]
        code_rows[code] = rows
        n = len(rows)
        for i in range(n):
            today = rows[i]
            d = today["date"]
            if not is_target(d):
                continue
            if i < 8:
                continue
            if is_st(today):
                continue
            if is_yizi_or_locked(today, bconf):
                continue
            buy = today.get("hour1_open") or today.get("open")
            if not buy or buy <= 0:
                continue
            pc = today.get("preclose")
            if not pc or pc <= 0:
                continue
            gap = (today["open"] - pc) / pc * 100.0
            gap_band = match_gap_band(bconf, gap)
            if gap_band is None:
                continue
            y = rows[i - 1]
            mcap_band = match_mcap(est_mcap(y))
            if mcap_band is None:
                continue
            conds = [P for P in P_ORDER if COND_FUNCS[P](rows, i, bconf)]
            if not conds:
                continue
            t1 = future_return(rows, i, buy, 1)
            t2 = future_return(rows, i, buy, 2)
            t5 = future_return(rows, i, buy, 5)
            fmax, fmin = future_extremes(rows, i, buy)
            sample = {"code": code, "name": today.get("code_name") or "", "date": d,
                      "i": i, "buy": buy, "gap": gap, "t1": t1, "t2": t2, "t5": t5,
                      "fmax": fmax, "fmin": fmin}
            for P in conds:
                combos[(P, board, mcap_band, gap_band)].append(sample)
                scanned += 1

    progress(f"      命中样本(含多P重复计): {scanned}")

    progress("[3/4] 生成90种组合得分矩阵...")
    log("")
    log("#" * 78)
    log("# 一、90种组合得分矩阵  (T+1/T+2/T+5 均收益 | 胜率)")
    log("#" * 78)
    header = (f"{'前置':<4} {'板块':<7} {'市值':<9} {'高开':<7} {'样本':>5} | "
              f"{'T1收益':>8} {'T1胜':>6} | {'T2收益':>8} {'T2胜':>6} | "
              f"{'T5收益':>8} {'T5胜':>6}  标记")
    log(header)
    log("-" * len(header))

    all_stats = {}
    mcap_cn_map = dict((k, cn) for k, cn, _, _ in MCAP_BANDS)
    for P in P_ORDER:
        for board in BOARD_ORDER:
            for mcap in MCAP_ORDER:
                bconf = BOARDS[board]
                for _, _, gap_band in bconf["gap_bands"]:
                    key = (P, board, mcap, gap_band)
                    st = agg_stats(combos.get(key, []))
                    all_stats[key] = st
                    n_ = st["n"]
                    if n_ == 0 and board_counts.get(board, 0) == 0:
                        flag = "[无数据]"
                    elif n_ < MIN_SAMPLE:
                        flag = "[样本不足]"
                    else:
                        flag = ""
                    log(f"{P:<4} {bconf['cn']:<7} {mcap_cn_map[mcap]:<9} {gap_band:<7} {n_:>5} | "
                        f"{fmt(st['t1_mean']):>8} {fmt_win(st['t1_win']):>6} | "
                        f"{fmt(st['t2_mean']):>8} {fmt_win(st['t2_win']):>6} | "
                        f"{fmt(st['t5_mean']):>8} {fmt_win(st['t5_win']):>6}  {flag}")
        log("-" * len(header))

    progress(f"[4/4] 分析Top{TOP_N}组合...")
    eligible = [(k, all_stats[k]) for k in all_stats
                if all_stats[k]["n"] >= MIN_SAMPLE and all_stats[k]["t1_mean"] is not None]
    eligible.sort(key=lambda x: x[1]["t1_mean"], reverse=True)
    top = eligible[:TOP_N]

    log("")
    log("#" * 78)
    log(f"# 二、Top{TOP_N} 组合详细分析 (按 T+1 均收益排序, 仅n>={MIN_SAMPLE})")
    log("#" * 78)
    if not top:
        log("无满足样本量要求的组合。")

    for rank, (key, st) in enumerate(top, 1):
        P, board, mcap, gap_band = key
        log("")
        log(f"【Top{rank}】 {P}({P_DESC[P]}) × {BOARDS[board]['cn']} × "
            f"{mcap_cn_map[mcap]} × 高开{gap_band}")
        log(f"  样本数={st['n']}  "
            f"T+1={fmt(st['t1_mean'])}(胜{fmt_win(st['t1_win'])})  "
            f"T+2={fmt(st['t2_mean'])}(胜{fmt_win(st['t2_win'])})  "
            f"T+5={fmt(st['t5_mean'])}(胜{fmt_win(st['t5_win'])})")
        log(f"  止盈止损建议: {suggest_tp_sl(st)}")
        samples = sorted(combos[key], key=lambda s: s["date"])
        log(f"  候选股hour级明细(前{min(MAX_DETAIL_CAND, len(samples))}个, 共{len(samples)}个):")
        for s in samples[:MAX_DETAIL_CAND]:
            rows = code_rows[s["code"]]
            i = s["i"]
            lo = max(0, i - CONTEXT_DAYS)
            hi = min(len(rows), i + CONTEXT_DAYS + 1)
            dates = [rows[j]["date"] for j in range(lo, hi)]
            log(f"    - {s['code']}({s['name']}) {s['date']} 高开{s['gap']:+.2f}% "
                f"买入={s['buy']:.2f} T+1={fmt(s['t1'])} T+5={fmt(s['t5'])}")
            print_hour_context(cursor, s["code"], s["name"], s["buy"], dates, s["date"])

    log("")
    log("#" * 78)
    log("# 三、与现有'大阴高开'策略(P1×创业板×<50亿)的对比与互补性")
    log("#" * 78)
    log("现有已确认: 创业板<50亿 + 前日跌>=5% + 高开2-8% (=P1×创业板×small×[2-5%,5-8%])")
    for gb in ("2-5%", "5-8%"):
        k = ("P1", "chuangye", "small", gb)
        st = all_stats.get(k, {"n": 0})
        if st.get("n"):
            log(f"  P1×创业板×<50亿×{gb}: n={st['n']} "
                f"T+1={fmt(st['t1_mean'])}(胜{fmt_win(st['t1_win'])}) "
                f"T+5={fmt(st['t5_mean'])}(胜{fmt_win(st['t5_win'])})")
    log("")
    log("互补性: 下列非P1的Top组合在'不同触发日'产生信号, 与现有P1策略正交,")
    log("        可作为组合补充(不同前置条件=不同建仓时机):")
    comp = [(k, s) for k, s in top if k[0] != "P1"]
    if comp:
        for key, st in comp:
            P, board, mcap, gap_band = key
            log(f"  - {P}({P_DESC[P]}) × {BOARDS[board]['cn']} × {mcap_cn_map[mcap]} × {gap_band}: "
                f"T+1={fmt(st['t1_mean'])} 胜{fmt_win(st['t1_win'])} n={st['n']}")
    else:
        log("  (Top组合均为P1类, 未见明显正交的新前置模式)")

    log("")
    log("#" * 78)
    log("# 四、结论")
    log("#" * 78)
    best_per_p = {}
    for key, st in all_stats.items():
        if st["n"] < MIN_SAMPLE or st["t1_mean"] is None:
            continue
        P = key[0]
        if P not in best_per_p or st["t1_mean"] > best_per_p[P][1]["t1_mean"]:
            best_per_p[P] = (key, st)
    log("各前置条件的最佳可用组合(n>=%d):" % MIN_SAMPLE)
    for P in P_ORDER:
        if P in best_per_p:
            key, st = best_per_p[P]
            _, board, mcap, gap_band = key
            log(f"  {P}: {BOARDS[board]['cn']}×{mcap_cn_map[mcap]}×{gap_band} -> "
                f"T+1={fmt(st['t1_mean'])}(胜{fmt_win(st['t1_win'])}) "
                f"T+2={fmt(st['t2_mean'])} T+5={fmt(st['t5_mean'])} n={st['n']}")
        else:
            log(f"  {P}: 无满足样本量的组合")
    log("")
    if top:
        bk, bst = top[0]
        log(f"全局最优(T+1): {bk[0]}×{BOARDS[bk[1]]['cn']}×{mcap_cn_map[bk[2]]}×{bk[3]} "
            f"T+1={fmt(bst['t1_mean'])} 胜{fmt_win(bst['t1_win'])} n={bst['n']}")
    log("注: 北交所无数据; 结论基于创业板+科创板。以上为研究阶段前瞻指标,")
    log("    正式采纳需按rule2第3步对接回测引擎做T+1可执行完整回测确认。")

    conn.close()
    _LOG_FH.close()
    progress(f"完成! 日志已写入: {LOG_PATH}")


if __name__ == "__main__":
    main()
