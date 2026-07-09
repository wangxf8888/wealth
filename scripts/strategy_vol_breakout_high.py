#!/usr/bin/env python3
"""
Task #94: 放量突破20日新高策略 - rule2 研究脚本

策略思路:
  - 今日收盘突破近20日最高收盘价 (close > max(close[T-20..T-1]))
  - 成交量放量: volume >= 5日均量(T-5..T-1) × 2
  - 创业板 (sz.30 开头)
  - 流通市值 50-300亿: mcap = amount / (turn/100) / 1e8
  - 排除ST、排除当日涨停 (close >= round(preclose*1.20, 2))
  经典趋势启动信号。

rule2 合规:
  - 突破信号需 T日close 与 volume 确认, 属 T日盘后信号
  - 合规买点为 T+1 hour1 (信号确认后次日买)
  - 另对比 T日hour4 买入 (激进/信号未完全确认, 仅供研究参照)
  - 不使用未来数据: 卖出/持有均基于买入后的hour路径

用法:
  python strategy_vol_breakout_high.py 2026-04            # 单月研究(Step1-4)
  python strategy_vol_breakout_high.py 2021-01 2026-06    # 区间全周期(Step5)

============================ 研究结论 (2026-07) ============================
基准(2026-04单月): 合规T+1 h1买持有3日 收益+1.36%/胜率51.8%; 最优TP15%/SL-7%
  月化估算+7.1%/胜率50.2% -> 未达标。
分层分析发现单月alpha子集(小市值50-100亿+温和放量2-5x+换手5-20%+突破<6%+涨幅<6%),
  单月表现亮眼(持有3日胜率67.5%/月化估算+26.6%)。
但全周期(2021-2026, n=1569)验证该精选组合: 胜率46.3%/月化估算+4.5% -> 未达标。
  分年度: 仅2021勉强55%, 2022亏损(39%), 2023-2026均42-49%, 无稳定alpha。
结论: 单月表现系过拟合/样本偏差, 放量突破20日新高(含精选优化)无稳定alpha,
  判定策略无效, 不推进引擎回测与全周期扩展。
  (稳健性验证脚本: vol_breakout_combo_verify.py; 分层分析: vol_breakout_layered.py)
===========================================================================
"""
import sys
import sqlite3
from collections import defaultdict

# ========== 配置区 ==========
DB_PATH = '/home/AIWealth/data/stocks.db'
BREAKOUT_LOOKBACK = 20        # 突破: 近N日最高收盘价
VOL_MA_DAYS = 5               # 放量: N日均量
VOL_MULTIPLE = 2.0           # 放量倍数
MCAP_MIN = 50.0              # 流通市值下限(亿)
MCAP_MAX = 300.0             # 流通市值上限(亿)
MAX_DETAIL_PER_DAY = 3       # 每天最多打印明细的候选股数
HOLD_DAYS = [1, 2, 3]        # 持有天数(卖在T+N收盘)
TP_GRID = [0.05, 0.07, 0.10, 0.15]   # 止盈网格
SL_GRID = [-0.03, -0.05, -0.07]      # 止损网格
# ============================


def get_limit_ratio(code):
    if code.startswith('sz.30') or code.startswith('sh.688'):
        return 0.20
    elif code.startswith('bj.'):
        return 0.30
    return 0.10


def is_limit_up(close, preclose, code):
    """涨停判定: round(close/preclose, 2) >= 涨停阈值"""
    if preclose is None or preclose <= 0 or close is None:
        return False
    return round(close / preclose, 2) >= (1 + get_limit_ratio(code))


def get_trading_days(cur, month_str):
    cur.execute("SELECT DISTINCT date FROM stock_kline WHERE date LIKE ? ORDER BY date",
                (month_str + '%',))
    return [r[0] for r in cur.fetchall()]


def get_days_in_range(cur, start_month, end_month):
    """获取[start_month, end_month]区间内所有交易日"""
    cur.execute("""
        SELECT DISTINCT date FROM stock_kline
        WHERE date >= ? AND date <= ? ORDER BY date
    """, (start_month + '-01', end_month + '-31'))
    return [r[0] for r in cur.fetchall()]


def get_all_trading_days(cur):
    cur.execute("SELECT DISTINCT date FROM stock_kline ORDER BY date")
    return [r[0] for r in cur.fetchall()]


def find_candidates(cur, today, all_days, all_days_idx):
    """找出满足放量突破20日新高的候选股"""
    today_idx = all_days_idx[today]
    if today_idx < BREAKOUT_LOOKBACK + 1:
        return []

    # 突破需 T-20..T-1 的close; 放量需 T-5..T-1 的volume
    lookback_days = all_days[today_idx - BREAKOUT_LOOKBACK:today_idx]  # 不含today, 共20日

    # today 全市场数据
    cur.execute("""
        SELECT code, code_name, open, high, low, close, preclose, isST, turn, volume, amount,
               hour1_open, hour1_high, hour1_low, hour1_close,
               hour2_open, hour2_high, hour2_low, hour2_close,
               hour3_open, hour3_high, hour3_low, hour3_close,
               hour4_open, hour4_high, hour4_low, hour4_close
        FROM stock_kline WHERE date = ?
    """, (today,))
    cols = [d[0] for d in cur.description]
    today_rows = {}
    for r in cur.fetchall():
        today_rows[r[0]] = dict(zip(cols, r))

    # lookback区间: close 与 volume
    lb_ph = ','.join(['?'] * len(lookback_days))
    cur.execute(f"""
        SELECT code, date, close, volume FROM stock_kline
        WHERE date IN ({lb_ph}) ORDER BY code, date
    """, lookback_days)
    hist = defaultdict(list)
    for r in cur.fetchall():
        hist[r[0]].append((r[1], r[2], r[3]))  # date, close, volume

    candidates = []
    for code, row in today_rows.items():
        # 板块: 创业板 sz.30
        if not code.startswith('sz.30'):
            continue

        # ST 排除
        if row['isST'] == 1:
            continue
        if row['code_name'] and 'ST' in row['code_name'].upper():
            continue

        close = row['close']
        preclose = row['preclose']
        volume = row['volume']
        turn = row['turn']
        amount = row['amount']
        if close is None or preclose is None or preclose <= 0:
            continue
        if volume is None or turn is None or turn <= 0 or amount is None:
            continue

        # 排除当日涨停
        if is_limit_up(close, preclose, code):
            continue

        # 历史数据充足性
        h = hist.get(code, [])
        h = [x for x in h if x[1] is not None and x[2] is not None]
        if len(h) < BREAKOUT_LOOKBACK:
            continue

        # 突破近20日最高收盘价
        prev_closes = [x[1] for x in h]
        max20 = max(prev_closes)
        if close <= max20:
            continue

        # 放量: 近5日均量 × 2
        recent5_vol = [x[2] for x in h[-VOL_MA_DAYS:]]
        if len(recent5_vol) < VOL_MA_DAYS:
            continue
        vol_ma5 = sum(recent5_vol) / len(recent5_vol)
        if vol_ma5 <= 0 or volume < vol_ma5 * VOL_MULTIPLE:
            continue

        # 流通市值 = amount / (turn/100) / 1e8
        mcap = amount / (turn / 100.0) / 1e8
        if mcap < MCAP_MIN or mcap > MCAP_MAX:
            continue

        candidates.append({
            'code': code,
            'code_name': row['code_name'] or 'N/A',
            'today': today,
            'close': close,
            'preclose': preclose,
            'max20': max20,
            'breakout_pct': (close - max20) / max20 * 100,
            'vol_ratio': volume / vol_ma5,
            'mcap': mcap,
            'turn': turn,
            'day_change': (close - preclose) / preclose * 100,
            'h4_open': row['hour4_open'],
            'h4_close': row['hour4_close'],
        })

    candidates.sort(key=lambda x: x['vol_ratio'], reverse=True)
    return candidates


def get_hour_rows(cur, code, days_list):
    if not days_list:
        return []
    ph = ','.join(['?'] * len(days_list))
    cur.execute(f"""
        SELECT date, close, preclose, turn,
               hour1_open, hour1_high, hour1_low, hour1_close,
               hour2_open, hour2_high, hour2_low, hour2_close,
               hour3_open, hour3_high, hour3_low, hour3_close,
               hour4_open, hour4_high, hour4_low, hour4_close
        FROM stock_kline WHERE code = ? AND date IN ({ph}) ORDER BY date
    """, [code] + days_list)
    return cur.fetchall()


def print_hour_table(hour_rows, t_date, t_close):
    """打印hour级明细, rate相对T日close"""
    lines = []
    lines.append(f"  {'日期':<12}| {'hr':<4}| {'open':<8}| {'high':<8}| {'low':<8}| {'close':<8}| vsT收盘")
    lines.append(f"  {'-'*12}+{'-'*5}+{'-'*9}+{'-'*9}+{'-'*9}+{'-'*9}+{'-'*9}")
    for row in hour_rows:
        date = row[0]
        hours = [
            ('h1', row[4], row[5], row[6], row[7]),
            ('h2', row[8], row[9], row[10], row[11]),
            ('h3', row[12], row[13], row[14], row[15]),
            ('h4', row[16], row[17], row[18], row[19]),
        ]
        for hn, ho, hh, hl, hc in hours:
            if ho is None or hc is None:
                continue
            vs_t = (hc - t_close) / t_close * 100 if t_close else 0
            marker = " ← T日" if date == t_date and hn == 'h4' else ""
            lines.append(f"  {date:<12}| {hn:<4}| {ho:<8.2f}| {hh:<8.2f}| "
                         f"{hl:<8.2f}| {hc:<8.2f}| {vs_t:+.2f}%{marker}")
    return '\n'.join(lines)


def get_future_path(cur, code, all_days, t_idx, max_days):
    """获取T+1..T+max_days的hour级路径, 用于模拟TP/SL"""
    fdays = all_days[t_idx + 1:t_idx + 1 + max_days]
    if not fdays:
        return []
    rows = get_hour_rows(cur, code, fdays)
    path = []  # [(date, hour_name, open, high, low, close)]
    row_map = {r[0]: r for r in rows}
    for d in fdays:
        if d not in row_map:
            continue
        r = row_map[d]
        for hn, oi in [('h1', 4), ('h2', 8), ('h3', 12), ('h4', 16)]:
            ho, hh, hl, hc = r[oi], r[oi + 1], r[oi + 2], r[oi + 3]
            if ho is None or hc is None:
                continue
            path.append((d, hn, ho, hh, hl, hc))
    return path


def simulate_tp_sl(buy_price, path, tp, sl, max_days):
    """在hour路径上模拟止盈止损. 返回(exit_pct, exit_reason)
    path: [(date, hour, o, h, l, c)] 按时间序; 已限定max_days内
    """
    if buy_price is None or buy_price <= 0 or not path:
        return None, None
    tp_price = buy_price * (1 + tp)
    sl_price = buy_price * (1 + sl)
    for (d, hn, ho, hh, hl, hc) in path:
        # 先判止损(保守: low先触及)
        if hl is not None and hl <= sl_price:
            return (sl_price - buy_price) / buy_price * 100, f'止损@{d}{hn}'
        if hh is not None and hh >= tp_price:
            return (tp_price - buy_price) / buy_price * 100, f'止盈@{d}{hn}'
    # 未触发: 末日收盘卖出
    last = path[-1]
    return (last[5] - buy_price) / buy_price * 100, f'到期@{last[0]}{last[1]}'


def analyze_and_print(cur, month_days, all_days, all_days_idx, verbose=True):
    """执行研究并返回统计结果"""
    total_cand = 0
    # 记录每个候选的买点与未来路径数据
    # entryA: T日hour4_open买(激进); entryB: T+1 hour1_open买(合规)
    recA = {h: [] for h in HOLD_DAYS}   # buy T-h4, sell T+h close
    recB = {h: [] for h in HOLD_DAYS}   # buy T+1-h1, sell T+h close
    # TP/SL 模拟(基于合规买点 entryB)
    tpsl_rec = defaultdict(list)  # (tp,sl) -> [ret]

    for today in month_days:
        if today not in all_days_idx:
            continue
        t_idx = all_days_idx[today]
        cands = find_candidates(cur, today, all_days, all_days_idx)
        total_cand += len(cands)

        if verbose:
            print(f"\n{'='*70}")
            print(f"========== {today}  候选股: {len(cands)}只 ==========")

        for rank, c in enumerate(cands):
            code = c['code']
            # 未来日线close序列(T..T+3)
            fdays = all_days[t_idx:t_idx + 1 + max(HOLD_DAYS)]
            drows = get_hour_rows(cur, code, fdays)
            dmap = {r[0]: r for r in drows}

            # entryA买入价: T日hour4_open
            buyA = c['h4_open']
            # entryB买入价: T+1 hour1_open
            t1 = all_days[t_idx + 1] if t_idx + 1 < len(all_days) else None
            buyB = None
            if t1 and t1 in dmap:
                buyB = dmap[t1][4]  # hour1_open

            # 各持有期收益(卖在T+h的close)
            for h in HOLD_DAYS:
                th = all_days[t_idx + h] if t_idx + h < len(all_days) else None
                if th and th in dmap and dmap[th][1] is not None:
                    sell_close = dmap[th][1]
                    if buyA and buyA > 0:
                        recA[h].append(((sell_close - buyA) / buyA * 100, c['mcap'], c['vol_ratio']))
                    if buyB and buyB > 0:
                        recB[h].append(((sell_close - buyB) / buyB * 100, c['mcap'], c['vol_ratio']))

            # TP/SL 模拟(entryB合规买点, 最长持有max(HOLD_DAYS))
            if buyB and buyB > 0:
                path = get_future_path(cur, code, all_days, t_idx, max(HOLD_DAYS))
                for tp in TP_GRID:
                    for sl in SL_GRID:
                        ret, reason = simulate_tp_sl(buyB, path, tp, sl, max(HOLD_DAYS))
                        if ret is not None:
                            tpsl_rec[(tp, sl)].append(ret)

            # 明细打印
            if verbose and rank < MAX_DETAIL_PER_DAY:
                print(f"\n--- {code} ({c['code_name']}) ---")
                print(f"  T日close={c['close']:.2f} 突破20日高{c['max20']:.2f} "
                      f"(+{c['breakout_pct']:.1f}%) | 涨幅{c['day_change']:+.2f}%")
                print(f"  放量={c['vol_ratio']:.2f}x | 市值{c['mcap']:.0f}亿 | 换手{c['turn']:.2f}%")
                p5 = max(0, t_idx - 5)
                p5e = min(len(all_days), t_idx + 6)
                window = all_days[p5:p5e]
                hrows = get_hour_rows(cur, code, window)
                if hrows:
                    print(print_hour_table(hrows, today, c['close']))
                if buyA:
                    print(f"  [激进]T日h4买={buyA:.2f}  [合规]T+1 h1买={buyB if buyB else 0:.2f}")

    return total_cand, recA, recB, tpsl_rec


def summary(day_count, total_cand, recA, recB, tpsl_rec):
    def stat(rec_list):
        if not rec_list:
            return None
        rets = [x[0] for x in rec_list]
        n = len(rets)
        avg = sum(rets) / n
        wins = sum(1 for r in rets if r > 0)
        wr = wins / n * 100
        return n, avg, wr

    print(f"\n\n{'='*70}")
    print(f"研究汇总统计")
    print(f"{'='*70}")
    print(f"交易日数: {day_count} | 总候选数: {total_cand} | 日均: {total_cand/max(1,day_count):.2f}只")

    print(f"\n--- 买点 x 持有期: 平均收益 / 胜率 ---")
    print(f"  {'配置':<28}{'样本':<8}{'平均收益':<12}{'胜率':<10}")
    print(f"  {'-'*58}")
    best = None
    for label, rec in [('激进 T日h4买', recA), ('合规 T+1 h1买', recB)]:
        for h in HOLD_DAYS:
            s = stat(rec[h])
            if not s:
                continue
            n, avg, wr = s
            tag = f"{label} 持有{h}日"
            print(f"  {tag:<28}{n:<8}{avg:+.2f}%{'':<6}{wr:.1f}%")
            if best is None or avg > best[1]:
                best = (tag, avg, wr, n)
    if best:
        print(f"\n  >>> 最优(按平均收益): {best[0]} | 收益{best[1]:+.2f}% | 胜率{best[2]:.1f}% | n={best[3]}")

    print(f"\n--- 止盈止损网格 (合规T+1 h1买, 最长持有{max(HOLD_DAYS)}日) ---")
    print(f"  {'止盈/止损':<16}{'样本':<8}{'平均收益':<12}{'胜率':<10}")
    print(f"  {'-'*46}")
    best_ts = None
    for tp in TP_GRID:
        for sl in SL_GRID:
            rets = tpsl_rec.get((tp, sl), [])
            if not rets:
                continue
            n = len(rets)
            avg = sum(rets) / n
            wr = sum(1 for r in rets if r > 0) / n * 100
            print(f"  TP{tp*100:.0f}%/SL{sl*100:.0f}%{'':<4}{n:<8}{avg:+.2f}%{'':<6}{wr:.1f}%")
            if best_ts is None or avg > best_ts[1]:
                best_ts = ((tp, sl), avg, wr, n)
    if best_ts:
        (tp, sl), avg, wr, n = best_ts
        print(f"\n  >>> 最优TP/SL: TP{tp*100:.0f}%/SL{sl*100:.0f}% | 收益{avg:+.2f}% | 胜率{wr:.1f}% | n={n}")

    # 月化估算: 假设每笔平均持有max(HOLD_DAYS)日, 每月约20交易日
    if best_ts:
        (tp, sl), avg, wr, n = best_ts
        cycles = 20.0 / max(HOLD_DAYS)
        monthly = ((1 + avg / 100) ** cycles - 1) * 100
        print(f"\n  月化估算(单仓连续复利, 每月{cycles:.1f}轮): {monthly:+.1f}%")
        print(f"  目标: 月化10%+ 且 胜率55%+ → "
              f"{'✓ 达标' if monthly >= 10 and wr >= 55 else '✗ 未达标'}")


def main():
    args = sys.argv[1:]
    if not args:
        print("用法: python strategy_vol_breakout_high.py 2026-04 [end_month]")
        sys.exit(1)

    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    all_days = get_all_trading_days(cur)
    all_days_idx = {d: i for i, d in enumerate(all_days)}

    if len(args) == 1:
        start_month = args[0]
        target_days = get_trading_days(cur, start_month)
        mode = f"单月 {start_month}"
        verbose = True
    else:
        start_month, end_month = args[0], args[1]
        target_days = get_days_in_range(cur, start_month, end_month)
        mode = f"区间 {start_month} ~ {end_month}"
        verbose = False  # 全周期不打印明细, 只统计

    print(f"{'='*70}")
    print(f"放量突破20日新高策略 - 研究 [{mode}]")
    print(f"条件: close>近{BREAKOUT_LOOKBACK}日高 | vol>={VOL_MULTIPLE}x{VOL_MA_DAYS}日均量 "
          f"| 创业板 | 市值[{MCAP_MIN:.0f},{MCAP_MAX:.0f}]亿 | 排除ST/涨停")
    print(f"交易日数: {len(target_days)}")
    print(f"{'='*70}")

    if not target_days:
        print("错误: 无交易日数据")
        conn.close()
        sys.exit(1)

    total_cand, recA, recB, tpsl_rec = analyze_and_print(
        cur, target_days, all_days, all_days_idx, verbose=verbose)
    summary(len(target_days), total_cand, recA, recB, tpsl_rec)

    conn.close()
    print(f"\n{'='*70}")
    print("研究完成。")


if __name__ == '__main__':
    main()
