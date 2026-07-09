#!/usr/bin/env python3
"""
缩量阴线后放量反转策略研究 (rule2 流程) - Task #73

策略思路:
  连续 3-5 日缩量下跌(抛压衰竭) -> 突然放量高开(新资金进入) = 底部反转信号。

信号定义:
  方案1(严格3日):
    T-3,T-2,T-1 连续 3 日收阴(close<open)
    且 volume 逐日递减: v[T-1] < v[T-2] < v[T-3]
    且 每日成交量 < 该日 5 日均量 * 0.7
  方案2(宽松5日):
    过去 5 日中 >=3 日收阴
    近 5 日总成交量 < 此前 5 日总成交量 * 0.6
    期间累计跌幅 >= 5%
  放量高开(今日):
    open > 昨收 * (1+gap%)   [gap 测试 1%/2%/3%]
    (可选)hour1_volume > 昨日 hour1_volume * 1.5

板块: 创业板(sz.300/301) + 科创板(sh.688)
市值: <50亿 / 50-200亿
排除: ST、涨停开盘(无法买入)
买入: today hour1_open (对比 hour2_open)

用法:
  python3 research_shrink_reversal_r2.py 2024-09
  python3 research_shrink_reversal_r2.py all
"""
import sys
import sqlite3
from statistics import mean

# ============ 配置区 ============
DB_PATH = "/home/AIWealth/data/stocks.db"
LOG_PATH = "/home/AIWealth/scripts/logs/shrink_reversal_r2.log"

GAP_LEVELS = [1.0, 2.0, 3.0]        # 高开阈值(%) 测试档
HOLD_DAYS = [1, 2, 3, 5]            # 持仓期 T+n
BUY_TIMINGS = ["h1", "h2"]         # 买入时机: hour1_open / hour2_open
TP_TOUCH = 5.0                      # 止盈触及率统计阈值(%)
CONTEXT_BEFORE = 5                  # 明细打印: 前 N 日
CONTEXT_AFTER = 5                   # 明细打印: 后 N 日
MAX_DETAIL = 20                     # 单月模式最多打印明细候选数(每方案)
MIN_SAMPLE = 20                     # 单月模式选最优配置的最小样本数
MIN_SAMPLE_ROBUST = 200            # all模式选稳健最优配置的最小样本数(防小样本过拟合)
# ================================

# 全局日志句柄
_LOG_FH = None


def log(msg=""):
    print(msg)
    if _LOG_FH:
        _LOG_FH.write(msg + "\n")


def get_limit_up_price(code, preclose):
    if code.startswith("sz.300") or code.startswith("sz.301"):
        return round(preclose * 1.2, 2)
    elif code.startswith("sh.688"):
        return round(preclose * 1.2, 2)
    elif code.startswith("bj."):
        return round(preclose * 1.3, 2)
    else:
        return round(preclose * 1.1, 2)


def get_cap_group(cap_yi):
    if cap_yi is None:
        return "未知"
    elif cap_yi < 50:
        return "50亿以下"
    elif cap_yi < 200:
        return "50-200亿"
    else:
        return "200亿以上"


def load_board_data(cursor):
    """加载创业板+科创板压缩时序数据(仅信号检测所需字段)"""
    cursor.execute("""
        SELECT date, code, code_name, open, close, volume, high, low,
               preclose, turn, amount, isST, hour1_open, hour2_open, hour1_volume
        FROM stock_kline
        WHERE code LIKE 'sz.300%' OR code LIKE 'sz.301%' OR code LIKE 'sh.688%'
        ORDER BY code, date
    """)
    data = {}
    for row in cursor.fetchall():
        (d, code, name, o, c, v, hi, lo, pc, turn, amt, st, h1o, h2o, h1v) = row
        if code not in data:
            data[code] = {
                'name': name, 'dates': [], 'o': [], 'c': [], 'v': [],
                'hi': [], 'lo': [], 'pc': [], 'turn': [], 'amt': [],
                'st': [], 'h1o': [], 'h2o': [], 'h1v': [],
            }
        s = data[code]
        s['dates'].append(d)
        s['o'].append(o if o is not None else 0)
        s['c'].append(c if c is not None else 0)
        s['v'].append(v if v is not None else 0)
        s['hi'].append(hi if hi is not None else 0)
        s['lo'].append(lo if lo is not None else 0)
        s['pc'].append(pc if pc is not None else 0)
        s['turn'].append(turn if turn is not None else 0)
        s['amt'].append(amt if amt is not None else 0)
        s['st'].append(st if st is not None else 0)
        s['h1o'].append(h1o if h1o is not None else 0)
        s['h2o'].append(h2o if h2o is not None else 0)
        s['h1v'].append(h1v if h1v is not None else 0)
    return data


def check_method1(s, i):
    """严格3日缩量下跌。需要 i>=7"""
    if i < 7:
        return False
    o, c, v = s['o'], s['c'], s['v']
    # 连续3日收阴
    for d in (i - 3, i - 2, i - 1):
        if not (c[d] < o[d] and o[d] > 0):
            return False
    # 成交量逐日递减
    if not (v[i - 1] < v[i - 2] < v[i - 3]):
        return False
    # 每日成交量 < 该日5日均量*0.7
    for d in (i - 3, i - 2, i - 1):
        ma5 = mean(v[d - 4:d + 1])
        if ma5 <= 0 or v[d] >= ma5 * 0.7:
            return False
    return True


def check_method2(s, i):
    """宽松5日缩量下跌。需要 i>=10"""
    if i < 10:
        return False
    o, c, v = s['o'], s['c'], s['v']
    # 过去5日中>=3日收阴
    yin = sum(1 for d in range(i - 5, i) if c[d] < o[d] and o[d] > 0)
    if yin < 3:
        return False
    # 近5日总量 < 此前5日总量*0.6
    vol5 = sum(v[i - 5:i])
    prev5 = sum(v[i - 10:i - 5])
    if prev5 <= 0 or vol5 >= prev5 * 0.6:
        return False
    # 期间累计跌幅>=5% (T-1收盘 vs T-6收盘)
    base = c[i - 6]
    if base <= 0:
        return False
    cum = (c[i - 1] - base) / base * 100
    if cum > -5.0:
        return False
    return True


def scan_candidates(data, target_dates):
    """扫描所有候选股。target_dates=None 表示全部日期"""
    cands = []
    for code, s in data.items():
        dates = s['dates']
        n = len(dates)
        name = s['name'] or ''
        board = "科创板" if code.startswith("sh.688") else "创业板"
        for i in range(n):
            d = dates[i]
            if target_dates is not None and d not in target_dates:
                continue
            m1 = check_method1(s, i)
            m2 = check_method2(s, i)
            if not (m1 or m2):
                continue
            open_t = s['o'][i]
            pc = s['c'][i - 1] if i >= 1 else 0
            if pc <= 0 or open_t <= 0:
                continue
            # 要求高开
            if open_t <= pc:
                continue
            # 排除ST
            if s['st'][i] == 1 or 'ST' in name.upper():
                continue
            # 排除涨停开盘
            if open_t >= get_limit_up_price(code, pc):
                continue
            gap_pct = (open_t - pc) / pc * 100
            buy_h1 = s['h1o'][i]
            buy_h2 = s['h2o'][i]
            # 市值(流通市值 = 成交额/换手率*100, 亿)
            turn, amt = s['turn'][i], s['amt'][i]
            cap = amt / turn * 100 / 1e8 if (turn and turn > 0 and amt > 0) else None
            # 未来数据 T+1..T+5
            fut = []
            for j in range(1, CONTEXT_AFTER + 1):
                if i + j < n:
                    fut.append({'high': s['hi'][i + j], 'low': s['lo'][i + j],
                                'close': s['c'][i + j]})
            # hour1量能放大
            h1v_prev = s['h1v'][i - 1] if i >= 1 else 0
            vol_surge = s['h1v'][i] > h1v_prev * 1.5 if h1v_prev > 0 else False
            cands.append({
                'date': d, 'code': code, 'name': name, 'board': board,
                'year': d[:4], 'idx': i, 'm1': m1, 'm2': m2,
                'pc': pc, 'open': open_t, 'gap_pct': gap_pct,
                'buy_h1': buy_h1, 'buy_h2': buy_h2,
                'cap': cap, 'cap_group': get_cap_group(cap),
                'today_close': s['c'][i], 'fut': fut, 'vol_surge': vol_surge,
            })
    return cands


def get_hour_detail(cursor, code, dates_list):
    if not dates_list:
        return []
    placeholders = ','.join(['?'] * len(dates_list))
    cursor.execute(f"""
        SELECT date, open, high, low, close, preclose,
               hour1_open, hour1_high, hour1_low, hour1_close,
               hour2_open, hour2_high, hour2_low, hour2_close,
               hour3_open, hour3_high, hour3_low, hour3_close,
               hour4_open, hour4_high, hour4_low, hour4_close
        FROM stock_kline
        WHERE code = ? AND date IN ({placeholders})
        ORDER BY date
    """, [code] + dates_list)
    cols = [c[0] for c in cursor.description]
    return [dict(zip(cols, r)) for r in cursor.fetchall()]


def print_candidate_detail(cursor, cand, data):
    s = data[cand['code']]
    dates = s['dates']
    i = cand['idx']
    n = len(dates)
    lo_idx = max(0, i - CONTEXT_BEFORE)
    hi_idx = min(n - 1, i + CONTEXT_AFTER)
    window = dates[lo_idx:hi_idx + 1]
    detail = get_hour_detail(cursor, cand['code'], window)
    tags = []
    if cand['m1']:
        tags.append("方案1")
    if cand['m2']:
        tags.append("方案2")
    log(f"\n  {'='*100}")
    cap_str = f"{cand['cap']:.1f}亿" if cand['cap'] else "N/A"
    log(f"  {cand['code']} {cand['name']} [{cand['board']}] {cand['date']} "
        f"| {'+'.join(tags)} | 高开{cand['gap_pct']:+.2f}% | 市值{cap_str}({cand['cap_group']}) "
        f"| h1量能放大={cand['vol_surge']}")
    log(f"  {'日期':<12}{'标记':<6}{'日O':>8}{'日H':>8}{'日L':>8}{'日C':>8}"
        f"{'h1_O':>8}{'h1_C':>8}{'h2_O':>8}{'h2_C':>8}{'h3_C':>8}{'h4_C':>8}")
    for row in detail:
        mark = ""
        if row['date'] == cand['date']:
            mark = ">>今"
        elif row['date'] < cand['date']:
            mark = "前"
        else:
            mark = "后"

        def f(x):
            return f"{x:>8.2f}" if x is not None else f"{'--':>8}"
        log(f"  {row['date']:<12}{mark:<6}{f(row['open'])}{f(row['high'])}{f(row['low'])}"
            f"{f(row['close'])}{f(row['hour1_open'])}{f(row['hour1_close'])}"
            f"{f(row['hour2_open'])}{f(row['hour2_close'])}{f(row['hour3_close'])}"
            f"{f(row['hour4_close'])}")


def calc_config_stats(cands, method, gap, buy, hold):
    """按配置计算统计。返回 dict 或 None"""
    rets = []
    wins = 0
    tp_touch = 0
    max_highs = []
    min_lows = []
    for c in cands:
        if method == 1 and not c['m1']:
            continue
        if method == 2 and not c['m2']:
            continue
        if c['gap_pct'] < gap:
            continue
        bp = c['buy_h1'] if buy == "h1" else c['buy_h2']
        if not bp or bp <= 0:
            continue
        if len(c['fut']) < hold:
            continue
        fut_n = c['fut'][:hold]
        sell = fut_n[-1]['close']
        if not sell or sell <= 0:
            continue
        ret = (sell - bp) / bp * 100
        rets.append(ret)
        if ret > 0:
            wins += 1
        highs = [x['high'] for x in fut_n if x['high'] and x['high'] > 0]
        lows = [x['low'] for x in fut_n if x['low'] and x['low'] > 0]
        if highs:
            mh = (max(highs) - bp) / bp * 100
            max_highs.append(mh)
            if mh >= TP_TOUCH:
                tp_touch += 1
        if lows:
            min_lows.append((min(lows) - bp) / bp * 100)
    if not rets:
        return None
    nn = len(rets)
    return {
        'method': method, 'gap': gap, 'buy': buy, 'hold': hold,
        'count': nn,
        'avg_ret': mean(rets),
        'win_rate': wins / nn * 100,
        'tp_rate': tp_touch / nn * 100,
        'avg_maxhigh': mean(max_highs) if max_highs else 0,
        'max_drawdown': min(min_lows) if min_lows else 0,
        'avg_drawdown': mean(min_lows) if min_lows else 0,
    }


def print_config_table(cands):
    log("\n" + "=" * 110)
    log("配置对比表 (方案 × 高开档 × 买入时机 × 持仓期)")
    log("  avg_ret=每笔均收益(卖=T+n收盘) | win=胜率 | tp5=触及+5%比率 | maxH=均最高涨 | maxDD=最大回撤")
    log("=" * 110)
    log(f"{'方案':<5}{'gap':>5}{'买':>5}{'持仓':>5}{'样本':>7}{'均收益':>9}{'胜率':>8}"
        f"{'tp5':>8}{'均最高':>9}{'最大回撤':>10}")
    log("-" * 110)
    all_stats = []
    for method in (1, 2):
        for gap in GAP_LEVELS:
            for buy in BUY_TIMINGS:
                for hold in HOLD_DAYS:
                    st = calc_config_stats(cands, method, gap, buy, hold)
                    if st:
                        all_stats.append(st)
                        log(f"{('方案'+str(method)):<5}{gap:>4.0f}%{buy:>5}"
                            f"{('T+'+str(hold)):>5}{st['count']:>7}"
                            f"{st['avg_ret']:>+8.2f}%{st['win_rate']:>7.1f}%"
                            f"{st['tp_rate']:>7.1f}%{st['avg_maxhigh']:>+8.2f}%"
                            f"{st['max_drawdown']:>+9.2f}%")
    return all_stats


def print_cap_group_analysis(cands):
    log("\n" + "=" * 90)
    log("市值分组分析 (方案1/方案2, 买h1, 持仓T+1, gap>=1%)")
    log("=" * 90)
    log(f"{'方案':<6}{'市值区间':<12}{'样本':>7}{'均收益':>9}{'胜率':>8}{'tp5':>8}{'最大回撤':>10}")
    log("-" * 90)
    for method in (1, 2):
        for grp in ["50亿以下", "50-200亿", "200亿以上"]:
            sub = [c for c in cands
                   if (c['m1'] if method == 1 else c['m2'])
                   and c['cap_group'] == grp and c['gap_pct'] >= 1.0]
            st = calc_config_stats(sub, method, 1.0, "h1", 1)
            if st:
                log(f"{('方案'+str(method)):<6}{grp:<12}{st['count']:>7}"
                    f"{st['avg_ret']:>+8.2f}%{st['win_rate']:>7.1f}%"
                    f"{st['tp_rate']:>7.1f}%{st['max_drawdown']:>+9.2f}%")


def print_yearly_stability(cands, best):
    log("\n" + "=" * 90)
    log(f"逐年稳定性 (最优配置: 方案{best['method']} gap>={best['gap']:.0f}% "
        f"买{best['buy']} 持仓T+{best['hold']})")
    log("=" * 90)
    log(f"{'年份':<8}{'样本':>7}{'均收益':>10}{'胜率':>9}{'tp5':>9}{'最大回撤':>11}")
    log("-" * 90)
    years = sorted(set(c['year'] for c in cands))
    yearly = []
    for y in years:
        sub = [c for c in cands if c['year'] == y]
        st = calc_config_stats(sub, best['method'], best['gap'], best['buy'], best['hold'])
        if st:
            yearly.append(st)
            log(f"{y:<8}{st['count']:>7}{st['avg_ret']:>+9.2f}%"
                f"{st['win_rate']:>8.1f}%{st['tp_rate']:>8.1f}%{st['max_drawdown']:>+10.2f}%")
        else:
            log(f"{y:<8}{'0':>7}{'N/A':>10}")
    return yearly


def main():
    global _LOG_FH
    arg = sys.argv[1] if len(sys.argv) > 1 else "2024-09"

    _LOG_FH = open(LOG_PATH, "w", encoding="utf-8")

    log("=" * 110)
    log("缩量阴线后放量反转策略研究 (Task #73, rule2 流程)")
    log(f"模式: {arg} | 板块: 创业板+科创板 | 排除: ST/涨停开盘")
    log(f"gap档: {GAP_LEVELS} | 持仓: {HOLD_DAYS} | 买入: {BUY_TIMINGS}")
    log("=" * 110)

    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()

    log("\n加载创业板+科创板时序数据...")
    data = load_board_data(cursor)
    log(f"  加载股票数: {len(data)}")

    # 目标日期集合
    if arg == "all":
        target_dates = None
        single_month = False
    else:
        cursor.execute("SELECT DISTINCT date FROM stock_kline WHERE date LIKE ? ORDER BY date",
                       (arg + '%',))
        target_dates = set(r[0] for r in cursor.fetchall())
        single_month = True
        log(f"  目标月份 {arg} 交易日: {len(target_dates)}")

    log("\n扫描候选股...")
    cands = scan_candidates(data, target_dates)
    m1_cnt = sum(1 for c in cands if c['m1'])
    m2_cnt = sum(1 for c in cands if c['m2'])
    both = sum(1 for c in cands if c['m1'] and c['m2'])
    log(f"  候选总数: {len(cands)} | 方案1: {m1_cnt} | 方案2: {m2_cnt} | 同时满足: {both}")

    if not cands:
        log("\n无候选股，结束。")
        conn.close()
        _LOG_FH.close()
        return

    # 单月模式: 打印候选明细
    if single_month:
        log("\n" + "=" * 110)
        log("候选股 hour 级明细 (前5日 + 当日 + 后5日)")
        log("=" * 110)
        m1_list = [c for c in cands if c['m1']][:MAX_DETAIL]
        m2_only = [c for c in cands if c['m2'] and not c['m1']][:MAX_DETAIL]
        log(f"\n----- 方案1候选(严格3日缩量) 打印 {len(m1_list)} 只 -----")
        for c in m1_list:
            print_candidate_detail(cursor, c, data)
        log(f"\n----- 方案2独有候选(宽松5日) 打印 {len(m2_only)} 只 -----")
        for c in m2_only:
            print_candidate_detail(cursor, c, data)

    # 配置对比表
    all_stats = print_config_table(cands)

    # 市值分组
    print_cap_group_analysis(cands)

    # 选最优配置: 优先稳健样本阈值(防小样本过拟合), 评分=均收益*胜率
    min_sample = MIN_SAMPLE_ROBUST if arg == "all" else MIN_SAMPLE
    valid = [s for s in all_stats if s['count'] >= min_sample and s['win_rate'] >= 50.0]
    if not valid:
        valid = [s for s in all_stats if s['count'] >= min_sample]
    if not valid:
        valid = [s for s in all_stats if s['count'] >= MIN_SAMPLE]
    if not valid:
        valid = all_stats
    def score(s):
        return s['avg_ret'] * (s['win_rate'] / 100.0)
    best = max(valid, key=score)

    # 逐年稳定性(仅 all 模式有意义, 单月也打印)
    yearly = print_yearly_stability(cands, best)

    # 最终结论
    log("\n" + "=" * 110)
    log("最终结论")
    log("=" * 110)
    log(f"最优配置: 方案{best['method']} | 高开>={best['gap']:.0f}% | 买入={best['buy']}_open "
        f"| 持仓=T+{best['hold']}")
    log(f"  样本数: {best['count']}")
    log(f"  每笔均收益: {best['avg_ret']:+.2f}%")
    log(f"  胜率: {best['win_rate']:.1f}%")
    log(f"  止盈(+{TP_TOUCH:.0f}%)触及率: {best['tp_rate']:.1f}%")
    log(f"  均最高涨幅: {best['avg_maxhigh']:+.2f}%")
    log(f"  最大回撤: {best['max_drawdown']:+.2f}%")

    # 达标判定: rule2 目标 月化10%+ 胜率55%+
    log("\n达标判定 (rule2 目标: 月化>=10%, 胜率>=55%):")
    reach_win = best['win_rate'] >= 55.0
    reach_ret = best['avg_ret'] >= 3.0  # 单笔均收益参考线
    log(f"  胜率 {best['win_rate']:.1f}% {'>= 55% 达标' if reach_win else '< 55% 未达标'}")
    log(f"  单笔均收益 {best['avg_ret']:+.2f}% "
        f"{'>= 3% 有潜力' if reach_ret else '< 3% 偏弱'}")
    valid_years = [y for y in yearly if y['count'] >= 10]
    if valid_years:
        pos_years = sum(1 for y in valid_years if y['avg_ret'] > 0)
        consistency = pos_years / len(valid_years) * 100
        log(f"  逐年一致性: {pos_years}/{len(valid_years)} 个有效年份(样本>=10)正收益 = {consistency:.0f}%")
        reach_stable = consistency >= 60.0
    else:
        reach_stable = False
        log("  逐年一致性: 有效年份(样本>=10)不足, 无法判定稳定性(疑似小样本过拟合)")
    if reach_win and reach_ret and reach_stable:
        verdict = "初步达标且逐年稳定, 建议进入回测引擎对接阶段"
    elif best['win_rate'] >= 55.0 and best['avg_ret'] > 0 and reach_stable:
        verdict = "胜率与稳定性达标但单笔收益偏薄, 需靠止盈/仓位/频率放大收益"
    elif reach_stable and best['avg_ret'] > 0:
        verdict = "逐年稳定小幅正期望但胜率/单笔收益未达标, 仅可作组合分散因子, 单独难达月化10%"
    elif best['win_rate'] >= 50.0 and best['avg_ret'] > 0:
        verdict = "接近达标但逐年不稳定, 优势主要来自特定行情(如924行情), 需进一步优化"
    else:
        verdict = "未达标, 策略优势不明显, 建议调整信号定义或放弃"
    log(f"  综合判定: {verdict}")

    conn.close()
    _LOG_FH.close()
    print(f"\n[日志已写入] {LOG_PATH}")


if __name__ == "__main__":
    main()
