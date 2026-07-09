#!/usr/bin/env python3
"""
Task #97: 长下影线(大长腿)次日延续策略 - 研究脚本 (rule2流程)

策略思路:
  今日下影线很长(盘中大幅下杀后被强力拉回), 说明有大资金低位承接,
  这类"大长腿"K线次日可能延续反弹。

T日候选定义:
  - 下影线: (close - low) / close >= 0.05
  - 收盘温和: close_rate(=(close-preclose)/preclose) 在 [-2%, +3%]
  - 板块: 创业板(sz.300/sz.301) 或 科创板(sh.688)  [振幅空间大]
  - 排除跌停: low > round(preclose*0.80, 2)  (创/科±20%)
  - 排除ST
  - 市值筛选: 50-500亿  (mcap = amount / (turn/100) / 1e8)

交易(T+1合规: 用T日收盘后确定的信号, T+1 hour1开盘买入):
  - 买入价 = T+1 hour1_open
  - 卖出候选: 持有到 T+1尾盘(hour4_close) / T+2收盘 / T+3收盘

用法:
  python strategy_long_lower_shadow.py 2026-04          # 单月: 打印明细+统计
  python strategy_long_lower_shadow.py --stats 2026-01 2026-06   # 区间: 仅统计
  python strategy_long_lower_shadow.py --full            # 全周期 2021-2026 分年统计
"""
import sys
import sqlite3

# ============ 配置区 ============
DB_PATH = "/home/AIWealth/data/stocks.db"
SHADOW_MIN = 0.05          # 下影线最小占比 (close-low)/close
CLOSE_RATE_LOW = -2.0      # 收盘涨幅下限(%)
CLOSE_RATE_HIGH = 3.0      # 收盘涨幅上限(%)
MCAP_MIN = 50.0            # 流通市值下限(亿)
MCAP_MAX = 500.0           # 流通市值上限(亿)
CONTEXT_DAYS = 5           # 明细前后查看天数
SHADOW_BUCKETS = [0.05, 0.07, 0.10]   # 下影线分档阈值
# ================================


def is_target_board(code):
    """创业板 或 科创板"""
    return code.startswith("sz.300") or code.startswith("sz.301") or code.startswith("sh.688")


def get_limit_up_price(code, preclose):
    """涨停价 (创/科 +20%)"""
    return round(preclose * 1.2, 2)


def get_limit_down_price(code, preclose):
    """跌停价 (创/科 -20%)"""
    return round(preclose * 0.80, 2)


def calc_mcap(amount, turn):
    """流通市值(亿) = amount / (turn/100) / 1e8"""
    if not amount or not turn or turn <= 0:
        return None
    return amount / (turn / 100.0) / 1e8


def is_st(row):
    if row['isST'] == 1:
        return True
    name = row['code_name'] or ''
    return 'ST' in name.upper()


def get_all_trading_days(cursor):
    cursor.execute("SELECT DISTINCT date FROM stock_kline ORDER BY date")
    return [r[0] for r in cursor.fetchall()]


def get_trading_days(cursor, month_str):
    cursor.execute("""
        SELECT DISTINCT date FROM stock_kline
        WHERE date LIKE ? ORDER BY date
    """, (month_str + '%',))
    return [r[0] for r in cursor.fetchall()]


def find_candidates(cursor, today):
    """找出T日满足下影线条件的候选股"""
    query = """
        SELECT date, code, code_name, preclose, open, high, low, close,
               close_rate, volume, amount, turn, isST,
               hour1_open, hour1_high, hour1_low, hour1_close,
               hour2_open, hour2_high, hour2_low, hour2_close,
               hour3_open, hour3_high, hour3_low, hour3_close,
               hour4_open, hour4_high, hour4_low, hour4_close
        FROM stock_kline
        WHERE date = ? AND preclose > 0 AND close > 0
    """
    cursor.execute(query, (today,))
    columns = [desc[0] for desc in cursor.description]
    results = []
    for row in cursor.fetchall():
        d = dict(zip(columns, row))
        code = d['code']
        if not is_target_board(code):
            continue
        if is_st(d):
            continue
        # 下影线
        shadow = (d['close'] - d['low']) / d['close']
        if shadow < SHADOW_MIN:
            continue
        # 收盘温和 (自行计算, 单位%)
        cr = (d['close'] - d['preclose']) / d['preclose'] * 100
        if cr < CLOSE_RATE_LOW or cr > CLOSE_RATE_HIGH:
            continue
        # 排除跌停
        if d['low'] <= get_limit_down_price(code, d['preclose']):
            continue
        # 市值筛选
        mcap = calc_mcap(d['amount'], d['turn'])
        if mcap is None or mcap < MCAP_MIN or mcap > MCAP_MAX:
            continue
        d['shadow'] = shadow
        d['cr'] = cr
        d['mcap'] = mcap
        results.append(d)
    return results


def get_row(cursor, code, date):
    cursor.execute("""
        SELECT date, code, preclose, open, high, low, close, close_rate,
               hour1_open, hour1_high, hour1_low, hour1_close,
               hour2_open, hour2_high, hour2_low, hour2_close,
               hour3_open, hour3_high, hour3_low, hour3_close,
               hour4_open, hour4_high, hour4_low, hour4_close
        FROM stock_kline WHERE code = ? AND date = ?
    """, (code, date))
    r = cursor.fetchone()
    if not r:
        return None
    cols = [desc[0] for desc in cursor.description]
    return dict(zip(cols, r))


def eval_signal(cursor, cand, all_days, today_idx):
    """
    评估单个T日信号的T+1买入/多卖点收益。
    返回dict: shadow, buyable, ret_T1, ret_T2, ret_T3 (百分比, 不可用则None)
    合规: T+1 hour1_open买入(基于T日收盘信号); 涨停一字无法买入则buyable=False
    """
    code = cand['code']
    res = {'code': code, 'date': cand['date'], 'shadow': cand['shadow'],
           'cr': cand['cr'], 'mcap': cand['mcap'],
           'buyable': False, 'ret_T1': None, 'ret_T2': None, 'ret_T3': None}

    if today_idx + 1 >= len(all_days):
        return res
    d1 = all_days[today_idx + 1]
    r1 = get_row(cursor, code, d1)
    if not r1 or not r1['hour1_open'] or r1['hour1_open'] <= 0:
        return res
    buy = r1['hour1_open']
    # 涨停无法买入: T+1竞价一字/开盘即涨停
    limit_up_1 = get_limit_up_price(code, r1['preclose'])
    if buy >= limit_up_1:
        return res
    res['buyable'] = True
    res['buy_price'] = buy

    # 卖点1: T+1尾盘 hour4_close (若无则用当日close)
    exit1 = r1['hour4_close'] if r1['hour4_close'] and r1['hour4_close'] > 0 else r1['close']
    if exit1 and exit1 > 0:
        res['ret_T1'] = (exit1 - buy) / buy * 100

    # 卖点2: T+2 收盘
    if today_idx + 2 < len(all_days):
        r2 = get_row(cursor, code, all_days[today_idx + 2])
        if r2 and r2['close'] and r2['close'] > 0:
            res['ret_T2'] = (r2['close'] - buy) / buy * 100

    # 卖点3: T+3 收盘
    if today_idx + 3 < len(all_days):
        r3 = get_row(cursor, code, all_days[today_idx + 3])
        if r3 and r3['close'] and r3['close'] > 0:
            res['ret_T3'] = (r3['close'] - buy) / buy * 100

    return res


def print_candidate(cursor, cand, all_days, today_idx):
    """打印候选股前5后5日hour级明细 (相对T日close的涨跌幅率)"""
    code = cand['code']
    name = cand['code_name'] or ''
    t_close = cand['close']
    start = max(0, today_idx - CONTEXT_DAYS)
    end = min(len(all_days) - 1, today_idx + CONTEXT_DAYS)
    days_range = all_days[start:end + 1]

    print(f"\n--- {code} ({name}) ---")
    print(f"  T日({cand['date']}): close={t_close:.2f} preclose={cand['preclose']:.2f} "
          f"low={cand['low']:.2f} 下影线={cand['shadow']*100:.1f}% "
          f"close_rate={cand['cr']:+.2f}% 市值={cand['mcap']:.0f}亿")
    print(f"  {'日期':<12}| {'hour':<5}| {'open':<8}| {'high':<8}| {'low':<8}| {'close':<8}| vs T日close")
    print(f"  {'-'*12}+{'-'*6}+{'-'*9}+{'-'*9}+{'-'*9}+{'-'*9}+{'-'*11}")
    for d in days_range:
        r = get_row(cursor, code, d)
        if not r:
            continue
        marker = ""
        if d == cand['date']:
            marker = " ←T日"
        elif days_range.index(d) == (today_idx - start) + 1:
            marker = " ←T+1"
        for h in range(1, 5):
            ho = r.get(f'hour{h}_open'); hh = r.get(f'hour{h}_high')
            hl = r.get(f'hour{h}_low'); hc = r.get(f'hour{h}_close')
            if not ho or ho == 0:
                continue
            vs = (hc - t_close) / t_close * 100 if t_close > 0 else 0
            tag = marker if h == 1 else ""
            print(f"  {d:<12}| h{h:<4}| {ho:<8.2f}| {hh:<8.2f}| {hl:<8.2f}| {hc:<8.2f}| {vs:+.2f}%{tag}")


def collect_signals(cursor, days, all_days, verbose=False):
    """遍历days收集所有信号评估结果"""
    signals = []
    for today in days:
        if today not in all_days:
            continue
        today_idx = all_days.index(today)
        if today_idx <= 0:
            continue
        cands = find_candidates(cursor, today)
        if verbose:
            print(f"\n{'='*10} {today} {'='*10}  候选数: {len(cands)}")
        for cand in cands:
            if verbose:
                print_candidate(cursor, cand, all_days, today_idx)
            sig = eval_signal(cursor, cand, all_days, today_idx)
            signals.append(sig)
    return signals


def _stats(rets):
    """返回 (n, avg, winrate)"""
    vals = [r for r in rets if r is not None]
    if not vals:
        return 0, 0.0, 0.0
    n = len(vals)
    avg = sum(vals) / n
    win = sum(1 for v in vals if v > 0) / n * 100
    return n, avg, win


def monthly_est(avg_ret_pct, hold_days, signals_per_month):
    """
    理想复利月化估算(单槽位连续复用):
      每月可交易轮数 = min(信号/月, 20个交易日 / 持有天数)
      月化 = (1+avg)^轮数 - 1
    """
    rounds = min(signals_per_month, 20.0 / hold_days) if signals_per_month > 0 else 0
    if rounds <= 0:
        return 0.0
    return ((1 + avg_ret_pct / 100.0) ** rounds - 1) * 100


def report(signals, label, n_months):
    """按下影线分档 × 卖点 输出统计"""
    print(f"\n{'='*20} 统计汇总: {label} {'='*20}")
    total = len(signals)
    buyable = [s for s in signals if s['buyable']]
    print(f"总信号数: {total}  可买入(排除T+1一字涨停): {len(buyable)}")
    if n_months > 0:
        print(f"跨度月数: {n_months:.1f}  月均信号(可买入): {len(buyable)/n_months:.2f}  "
              f"年均信号: {len(buyable)/n_months*12:.1f}")
    if not buyable:
        print("无可买入信号")
        return

    hold_map = {'ret_T1': 1, 'ret_T2': 2, 'ret_T3': 3}
    exit_label = {'ret_T1': 'T+1尾盘卖', 'ret_T2': 'T+2收盘卖', 'ret_T3': 'T+3收盘卖'}
    spm = len(buyable) / n_months if n_months > 0 else 0

    print(f"\n{'下影线档':<10}{'样本':<6}{'卖点':<12}{'平均收益':<10}{'胜率':<8}{'月化估算':<10}")
    print('-' * 62)
    best = None
    for bucket in SHADOW_BUCKETS:
        grp = [s for s in buyable if s['shadow'] >= bucket]
        # 该档月均信号
        bucket_spm = len(grp) / n_months if n_months > 0 else 0
        for key in ['ret_T1', 'ret_T2', 'ret_T3']:
            n, avg, win = _stats([s[key] for s in grp])
            if n == 0:
                continue
            mest = monthly_est(avg, hold_map[key], bucket_spm)
            print(f"≥{bucket*100:.0f}%{'':<6}{n:<6}{exit_label[key]:<12}"
                  f"{avg:+.2f}%{'':<4}{win:.1f}%{'':<3}{mest:+.1f}%")
            cand_best = {'bucket': bucket, 'exit': exit_label[key], 'n': n,
                         'avg': avg, 'win': win, 'mest': mest}
            # 最优: 胜率≥55 & 月化最高
            if win >= 55 and (best is None or mest > best['mest']):
                best = cand_best
    print('-' * 62)
    if best:
        print(f"\n★ 达标最优配置(胜率≥55%): 下影线≥{best['bucket']*100:.0f}% + {best['exit']}")
        print(f"  样本={best['n']} 平均收益={best['avg']:+.2f}% 胜率={best['win']:.1f}% "
              f"月化估算={best['mest']:+.1f}%")
    else:
        print(f"\n✗ 无配置同时满足胜率≥55%")
    return best


def month_span(days):
    """估算跨度月数"""
    if not days:
        return 0
    months = set(d[:7] for d in days)
    return len(months)


def main():
    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit(1)

    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()
    all_days = get_all_trading_days(cursor)

    print("=" * 62)
    print("Task #97: 长下影线(大长腿)次日延续策略研究")
    print(f"条件: 下影线≥{SHADOW_MIN*100:.0f}% close_rate∈[{CLOSE_RATE_LOW},{CLOSE_RATE_HIGH}]% "
          f"创/科板 市值{MCAP_MIN:.0f}-{MCAP_MAX:.0f}亿 非ST 非跌停")
    print("=" * 62)

    mode = sys.argv[1]

    if mode == '--full':
        # 全周期分年 2021-2026
        overall = []
        for year in range(2021, 2027):
            ydays = [d for d in all_days if d.startswith(str(year))]
            if not ydays:
                continue
            sigs = collect_signals(cursor, ydays, all_days, verbose=False)
            overall.extend(sigs)
            report(sigs, f"{year}年", month_span(ydays))
        # 汇总
        all_sig_days = [d for d in all_days if d[:4] in [str(y) for y in range(2021, 2027)]]
        report(overall, "2021-2026全周期", month_span(all_sig_days))

    elif mode == '--stats':
        start, end = sys.argv[2], sys.argv[3]
        rng = [d for d in all_days if start <= d[:7] <= end]
        sigs = collect_signals(cursor, rng, all_days, verbose=False)
        report(sigs, f"{start}~{end}", month_span(rng))

    else:
        # 单月: 打印明细
        month_str = mode
        mdays = get_trading_days(cursor, month_str)
        if not mdays:
            print(f"错误: 未找到 {month_str} 数据")
            conn.close()
            sys.exit(1)
        print(f"该月交易日数: {len(mdays)}")
        sigs = collect_signals(cursor, mdays, all_days, verbose=True)
        report(sigs, month_str, 1.0)

    conn.close()
    print("\n完成。")


if __name__ == "__main__":
    main()
