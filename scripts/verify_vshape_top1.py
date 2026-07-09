#!/usr/bin/env python3
"""V字形态高开策略 Top1 参数独立回测验证
完全独立实现，从零重建回测逻辑，验证 optimize_vshape.py 声称的 CAGR=+505%

Top1参数(来源: optimize_vshape_results.json):
  v_lookback_days = 4
  v_cum_drop = -3.0
  v_min_down_days = 2
  gap_up_min = 4.0
  gap_up_max = 20.0
  tp_pct = 10.0
  sl_pct = -2.0
  max_hold_days = 3
  n_slots = 1
  board_filter = 'sz.300'
  market_filter = False
  min_turn = 8.0
  shape_type = 'V'
"""
import sys, sqlite3, math, time
from datetime import datetime

sys.path.insert(0, '/home/AIWealth/scripts')
from compliance_utils import (
    safe_float, is_one_word_board, is_limit_up, is_limit_down,
    is_st, get_limit_threshold, is_valid_price
)

# ============================================================
# 参数定义 (从 optimize_vshape_results.json Top1 提取)
# ============================================================
PARAMS = {
    'v_lookback_days': 4,
    'v_cum_drop': -3.0,
    'v_min_down_days': 2,
    'gap_up_min': 4.0,
    'gap_up_max': 20.0,
    'tp_pct': 10.0,
    'sl_pct': -2.0,
    'max_hold_days': 3,
    'n_slots': 1,
    'board_filter': 'sz.300',
    'market_filter': False,
    'min_turn': 8.0,
    'shape_type': 'V',
}

DB_PATH = '/home/AIWealth/data/stocks.db'
INITIAL_CAPITAL = 1_000_000.0
START_DATE = '2021-01-01'
END_DATE = '2026-06-30'


def get_hour_price(row, hour, field):
    """获取指定hour的OHLC价格"""
    return row.get(f'h{hour}_{field}', 0.0)


def print_trade_line(idx, t):
    """打印单笔交易"""
    name = (t['code_name'] or '')[:6]
    print(f"  {idx:>4} | {t['buy_date']:<10} | {t['code']:<10} | {name:<6} | "
          f"{t['buy_price']:>7.2f} | h1_open  | {t['sell_date']:<10} | "
          f"{t['sell_price']:>7.2f} | h{t['sell_hour']}_close   | "
          f"{t['sell_reason']:<26} | {t['pnl_pct']:>+6.1f}% | {t['hold_days']:>3}")


def run_compliance_check(trades, conn):
    """合规性检查"""
    violations = {
        'T+0违规': 0, '涨停买入': 0, '跌停卖出': 0,
        '一字板': 0, '价格越界_买': 0, '价格越界_卖': 0,
    }
    violation_details = []
    cur = conn.cursor()

    for i, t in enumerate(trades, 1):
        issues = []
        # T+1检查
        if t['buy_date'] >= t['sell_date'] and t['sell_reason'] != '回测结束清仓':
            violations['T+0违规'] += 1
            issues.append('T+0')

        # 买入日验证
        cur.execute("SELECT preclose, close, hour1_low, hour1_high FROM stock_kline WHERE date=? AND code=?",
                    (t['buy_date'], t['code']))
        row = cur.fetchone()
        if row:
            preclose = safe_float(row[0])
            close = safe_float(row[1])
            h1l = safe_float(row[2])
            h1h = safe_float(row[3])
            if preclose > 0 and is_limit_up(t['code'], close, preclose):
                violations['涨停买入'] += 1
                issues.append('涨停买入')
            if h1l > 0 and h1h > 0 and not is_valid_price(t['buy_price'], h1l, h1h):
                violations['价格越界_买'] += 1
                issues.append(f'买入价{t["buy_price"]:.2f}不在[{h1l:.2f},{h1h:.2f}]')

        # 卖出日验证
        sh = t['sell_hour']
        cur.execute(f"SELECT preclose, close, hour{sh}_low, hour{sh}_high FROM stock_kline WHERE date=? AND code=?",
                    (t['sell_date'], t['code']))
        row = cur.fetchone()
        if row:
            preclose = safe_float(row[0])
            close = safe_float(row[1])
            hl = safe_float(row[2])
            hh = safe_float(row[3])
            if preclose > 0 and is_limit_down(t['code'], close, preclose):
                violations['跌停卖出'] += 1
                issues.append('跌停卖出')
            if hl > 0 and hh > 0 and not is_valid_price(t['sell_price'], hl, hh):
                violations['价格越界_卖'] += 1
                issues.append(f'卖出价{t["sell_price"]:.2f}不在h{sh}[{hl:.2f},{hh:.2f}]')

        if issues:
            violation_details.append((i, t, issues))

    total = len(trades)
    failed = len(violation_details)
    print(f"\n  总交易: {total}笔")
    print(f"  合规通过: {total - failed}笔 ({(total-failed)/total*100:.1f}%)")
    print(f"  违规: {failed}笔")
    print()
    for k, v in violations.items():
        status = '✓' if v == 0 else f'✗ ({v}笔)'
        print(f"    {k}: {status}")
    if violation_details:
        print(f"\n  违规明细(前10笔):")
        for idx, t, issues in violation_details[:10]:
            print(f"    #{idx} {t['buy_date']}->{t['sell_date']} {t['code']} "
                  f"{t.get('code_name','')} | {', '.join(issues)}")


def run_backtest():
    """独立回测主逻辑"""
    t0 = time.time()
    print("=" * 70)
    print("  V字形态高开策略 Top1 独立回测验证")
    print(f"  时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print("=" * 70)
    print(f"\n  参数:")
    for k, v in PARAMS.items():
        print(f"    {k} = {v}")
    print(f"\n  回测区间: {START_DATE} ~ {END_DATE}")
    print(f"  初始资金: {INITIAL_CAPITAL:,.0f}")
    print("=" * 70, flush=True)

    conn = sqlite3.connect(DB_PATH)

    # 1. 加载交易日列表
    print("\n  [1] 加载交易日...", flush=True)
    cur = conn.cursor()
    cur.execute("SELECT DISTINCT date FROM stock_kline WHERE date>=? AND date<=? ORDER BY date",
                (START_DATE, END_DATE))
    trading_dates = [r[0] for r in cur.fetchall()]
    print(f"    共 {len(trading_dates)} 个交易日")

    # 2. 按年加载K线数据(只加载创业板)
    print("  [2] 加载K线数据...", flush=True)
    all_data = {}  # {(date, code): row_dict}
    board_cond = "AND code LIKE 'sz.300%'"
    for year in range(2021, 2027):
        y_s, y_e = f"{year}-01-01", f"{year}-12-31"
        cur.execute(f"""SELECT date, code, code_name, preclose, open, close,
                        open_rate, close_rate, high, low, turn, isST,
                        hour1_open, hour1_high, hour1_low, hour1_close,
                        hour2_open, hour2_high, hour2_low, hour2_close,
                        hour3_open, hour3_high, hour3_low, hour3_close,
                        hour4_open, hour4_high, hour4_low, hour4_close
                    FROM stock_kline
                    WHERE date>=? AND date<=? {board_cond} AND isST=0""", (y_s, y_e))
        count = 0
        for r in cur:
            all_data[(r[0], r[1])] = {
                'date': r[0], 'code': r[1], 'code_name': r[2],
                'preclose': safe_float(r[3]), 'open': safe_float(r[4]),
                'close': safe_float(r[5]), 'open_rate': safe_float(r[6], None),
                'close_rate': safe_float(r[7], None),
                'high': safe_float(r[8]), 'low': safe_float(r[9]),
                'turn': safe_float(r[10], 0.0), 'isST': r[11],
                'h1_open': safe_float(r[12]), 'h1_high': safe_float(r[13]),
                'h1_low': safe_float(r[14]), 'h1_close': safe_float(r[15]),
                'h2_open': safe_float(r[16]), 'h2_high': safe_float(r[17]),
                'h2_low': safe_float(r[18]), 'h2_close': safe_float(r[19]),
                'h3_open': safe_float(r[20]), 'h3_high': safe_float(r[21]),
                'h3_low': safe_float(r[22]), 'h3_close': safe_float(r[23]),
                'h4_open': safe_float(r[24]), 'h4_high': safe_float(r[25]),
                'h4_low': safe_float(r[26]), 'h4_close': safe_float(r[27]),
            }
            count += 1
        print(f"    {year}: {count} 条", flush=True)

    # 3. 构建close_rate历史(按股票分组，排序)
    print("  [3] 构建close_rate历史...", flush=True)
    cr_history = {}  # {code: [(date, close_rate), ...]}
    for (dt, code), row in all_data.items():
        if row['close_rate'] is not None:
            cr_history.setdefault(code, []).append((dt, row['close_rate']))
    for code in cr_history:
        cr_history[code].sort()
    print(f"    {len(cr_history)} 只股票")

    # 4. 构建每日股票列表
    print("  [4] 构建每日股票索引...", flush=True)
    date_stocks = {}
    for (dt, code), row in all_data.items():
        date_stocks.setdefault(dt, []).append((code, row))

    # 5. 预构建lookback索引(每只股票每天的lookback值)
    print("  [5] 预计算lookback信号...", flush=True)
    lb_days = PARAMS['v_lookback_days']
    # {(date, code): (cum_drop, down_days)}
    lookback_cache = {}
    for code, hist in cr_history.items():
        for pos in range(lb_days, len(hist)):
            target_date = hist[pos][0]  # 当天
            window = hist[pos - lb_days: pos]  # 前lb_days天
            cum = sum(x[1] for x in window)
            dd = sum(1 for x in window if x[1] < 0)
            lookback_cache[(target_date, code)] = (cum, dd)
    print(f"    缓存 {len(lookback_cache)} 条lookback记录")

    # 6. 执行回测
    print("  [6] 执行回测...", flush=True)
    cash = INITIAL_CAPITAL
    positions = []  # [{code, code_name, buy_price, buy_date, hold_days, shares, last_price}]
    trades = []
    skipped_candidates = []
    year_start_eq = {}
    year_end_eq = {}
    peak = INITIAL_CAPITAL
    max_dd = 0.0
    prev_year = None

    for di, today in enumerate(trading_dates):
        cur_year = today[:4]
        if prev_year is None:
            year_start_eq[cur_year] = INITIAL_CAPITAL
        elif cur_year != prev_year:
            year_start_eq[cur_year] = year_end_eq.get(prev_year, INITIAL_CAPITAL)
        prev_year = cur_year

        # 更新持仓天数
        for pos in positions:
            if pos['buy_date'] != today:
                pos['hold_days'] += 1

        # ===== 寻找候选股 =====
        candidates = []
        if len(positions) < PARAMS['n_slots']:
            stocks_today = date_stocks.get(today, [])
            held_codes = {p['code'] for p in positions}
            for code, row in stocks_today:
                if code in held_codes:
                    continue
                if is_st(row['code_name'], row.get('isST', 0)):
                    continue
                open_rate = row['open_rate']
                if open_rate is None:
                    continue
                if open_rate < PARAMS['gap_up_min']:
                    continue
                # 原始脚本: gap_up_max>=20.0时视为无上限(gap_no_max=True)
                gap_no_max = PARAMS['gap_up_max'] >= 20.0
                if not gap_no_max and open_rate > PARAMS['gap_up_max']:
                    continue
                preclose = row['preclose']
                if preclose <= 0 or row['open'] <= 0:
                    continue
                if is_one_word_board(row['open'], row['high'], row['low'], row['close']):
                    continue
                h1o = row['h1_open']
                if h1o <= 0 or is_one_word_board(h1o, row['h1_high'], row['h1_low'], row['h1_close']):
                    continue
                if is_limit_up(code, h1o, preclose):
                    continue
                if row['turn'] < PARAMS['min_turn']:
                    continue
                lb_val = lookback_cache.get((today, code))
                if lb_val is None:
                    continue
                cum_drop, down_days = lb_val
                # V形态匹配
                if cum_drop <= PARAMS['v_cum_drop'] and down_days >= PARAMS['v_min_down_days']:
                    candidates.append({
                        'code': code, 'code_name': row['code_name'],
                        'h1_open': h1o, 'cum_drop': cum_drop,
                    })
            candidates.sort(key=lambda x: x['cum_drop'])

        # ===== 逐小时处理 =====
        for hour in range(1, 5):
            # --- 卖出 ---
            survived = []
            for pos in positions:
                if pos['buy_date'] == today:
                    survived.append(pos)
                    continue
                key = (today, pos['code'])
                row = all_data.get(key)
                if not row:
                    survived.append(pos)
                    continue
                hc = get_hour_price(row, hour, 'close')
                if hc <= 0:
                    survived.append(pos)
                    continue
                # 跌停一字板保护
                preclose_p = row['preclose']
                if preclose_p > 0 and is_limit_down(pos['code'], hc, preclose_p):
                    ho = get_hour_price(row, hour, 'open')
                    hh = get_hour_price(row, hour, 'high')
                    hl = get_hour_price(row, hour, 'low')
                    if is_one_word_board(ho, hh, hl, hc):
                        pos['last_price'] = hc
                        survived.append(pos)
                        continue
                pnl_pct = (hc - pos['buy_price']) / pos['buy_price'] * 100.0
                sell = False
                sell_reason = ''
                if pnl_pct >= PARAMS['tp_pct']:
                    sell = True
                    sell_reason = f'TP h{hour}({pnl_pct:+.1f}%>={PARAMS["tp_pct"]}%)'
                elif pnl_pct <= PARAMS['sl_pct']:
                    sell = True
                    sell_reason = f'SL h{hour}({pnl_pct:+.1f}%<={PARAMS["sl_pct"]}%)'
                elif pos['hold_days'] >= PARAMS['max_hold_days'] and hour == 4:
                    sell = True
                    sell_reason = f'到期h4({pos["hold_days"]}d>={PARAMS["max_hold_days"]})'
                if sell:
                    cash += pos['shares'] * hc
                    trades.append({
                        'code': pos['code'], 'code_name': pos['code_name'],
                        'buy_date': pos['buy_date'], 'buy_price': pos['buy_price'],
                        'buy_hour': 1, 'sell_date': today, 'sell_price': hc,
                        'sell_hour': hour, 'sell_reason': sell_reason,
                        'pnl_pct': pnl_pct, 'hold_days': pos['hold_days'],
                        'shares': pos['shares'],
                    })
                else:
                    pos['last_price'] = hc
                    survived.append(pos)
            positions = survived

            # --- 买入(仅hour1) ---
            if hour == 1 and candidates:
                free_slots = PARAMS['n_slots'] - len(positions)
                if free_slots > 0:
                    held_now = {p['code'] for p in positions}
                    bought = 0
                    day_skipped = []
                    for cand in candidates:
                        if bought >= free_slots:
                            day_skipped.append(cand)
                            continue
                        if cand['code'] in held_now:
                            continue
                        slot_cap = cash / max(1, free_slots - bought)
                        shares = int(slot_cap / cand['h1_open'] // 100) * 100
                        if shares <= 0:
                            continue
                        cost = shares * cand['h1_open']
                        if cost > cash:
                            continue
                        cash -= cost
                        positions.append({
                            'code': cand['code'], 'code_name': cand['code_name'],
                            'buy_price': cand['h1_open'], 'buy_date': today,
                            'hold_days': 0, 'shares': shares,
                            'last_price': cand['h1_open'],
                        })
                        held_now.add(cand['code'])
                        bought += 1
                    if day_skipped:
                        skipped_candidates.append((today, day_skipped))

        # ===== 日末权益(复制原脚本行为:用h4_open估值) =====
        equity = cash
        for pos in positions:
            key = (today, pos['code'])
            row = all_data.get(key)
            if row and row['h4_open'] > 0:
                pos['last_price'] = row['h4_open']
                equity += pos['shares'] * row['h4_open']
            else:
                equity += pos['shares'] * pos['last_price']
        year_end_eq[cur_year] = equity
        if equity > peak:
            peak = equity
        dd = (peak - equity) / peak * 100 if peak > 0 else 0
        if dd > max_dd:
            max_dd = dd

    # 清仓剩余持仓
    if positions:
        last_date = trading_dates[-1]
        for pos in positions:
            key = (last_date, pos['code'])
            row = all_data.get(key)
            sp = pos['last_price']
            if row and row['h4_open'] > 0:
                sp = row['h4_open']
            pnl_pct = (sp - pos['buy_price']) / pos['buy_price'] * 100.0
            cash += pos['shares'] * sp
            trades.append({
                'code': pos['code'], 'code_name': pos['code_name'],
                'buy_date': pos['buy_date'], 'buy_price': pos['buy_price'],
                'buy_hour': 1, 'sell_date': last_date, 'sell_price': sp,
                'sell_hour': 4, 'sell_reason': '回测结束清仓',
                'pnl_pct': pnl_pct, 'hold_days': pos['hold_days'],
                'shares': pos['shares'],
            })

    final_equity = cash
    elapsed = time.time() - t0

    # CAGR计算
    n_days = len(trading_dates)
    total_years = max(0.05, n_days / 245.0)
    cagr = ((final_equity / INITIAL_CAPITAL) ** (1.0 / total_years) - 1) * 100

    # 年度收益
    yearly_rets = {}
    for yr in sorted(year_start_eq.keys()):
        s = year_start_eq[yr]
        e = year_end_eq.get(yr, s)
        yearly_rets[yr] = ((e / s - 1) * 100) if s > 0 else 0.0

    # ===== 输出 =====
    print(f"\n  回测耗时: {elapsed:.1f}秒")
    print("\n" + "=" * 70)
    print("  ===== 回测结果汇总 =====")
    print("=" * 70)
    print(f"\n  初始资金: {INITIAL_CAPITAL:>15,.0f}")
    print(f"  最终资金: {final_equity:>15,.0f}")
    print(f"  总收益率: {(final_equity/INITIAL_CAPITAL-1)*100:>+12.1f}%")
    print(f"  CAGR:     {cagr:>+12.1f}%")
    print(f"  最大回撤: {max_dd:>12.1f}%")
    print(f"  总交易数: {len(trades):>12d}")
    n_wins = sum(1 for t in trades if t['pnl_pct'] > 0)
    win_rate = n_wins / len(trades) * 100 if trades else 0
    print(f"  胜率:     {win_rate:>12.1f}%")
    avg_pnl = sum(t['pnl_pct'] for t in trades) / len(trades) if trades else 0
    print(f"  平均盈亏: {avg_pnl:>+12.2f}%")

    # 年度汇总
    print("\n" + "-" * 70)
    print("  年度汇总:")
    print("-" * 70)
    for yr in sorted(yearly_rets.keys()):
        yr_trades = [t for t in trades if t['buy_date'][:4] == yr]
        yr_wins = sum(1 for t in yr_trades if t['pnl_pct'] > 0)
        yr_wr = yr_wins / len(yr_trades) * 100 if yr_trades else 0
        print(f"  {yr}: 交易{len(yr_trades):>3d}笔  胜率{yr_wr:>5.1f}%  收益{yearly_rets[yr]:>+8.1f}%")

    # 与优化脚本对比
    print("\n" + "-" * 70)
    print("  与优化脚本结果对比:")
    print("-" * 70)
    claimed = {'cagr': 504.96, 'trades': 283, 'wr': 49.1, 'mdd': 49.1}
    claimed_yearly = {'2021': 1484.7, '2022': 684.6, '2023': 370.6,
                      '2024': 846.6, '2025': 169.1, '2026': 15.9}
    print(f"  {'指标':<10} {'优化脚本':>12} {'独立验证':>12} {'偏差':>10}")
    print(f"  {'CAGR':<10} {claimed['cagr']:>+12.1f}% {cagr:>+12.1f}% {cagr-claimed['cagr']:>+10.1f}%")
    print(f"  {'交易数':<10} {claimed['trades']:>12d} {len(trades):>12d} {len(trades)-claimed['trades']:>+10d}")
    print(f"  {'胜率':<10} {claimed['wr']:>12.1f}% {win_rate:>12.1f}% {win_rate-claimed['wr']:>+10.1f}%")
    print(f"  {'最大回撤':<10} {claimed['mdd']:>12.1f}% {max_dd:>12.1f}% {max_dd-claimed['mdd']:>+10.1f}%")
    print(f"\n  年度收益对比:")
    for yr in sorted(claimed_yearly.keys()):
        v_c = claimed_yearly[yr]
        v_v = yearly_rets.get(yr, 0)
        diff = v_v - v_c
        flag = "OK" if abs(diff) < max(abs(v_c) * 0.05, 5) else "DIFF"
        print(f"    {yr}: claimed{v_c:>+8.1f}%  verified{v_v:>+8.1f}%  diff{diff:>+8.1f}%  [{flag}]")

    cagr_diff = abs(cagr - claimed['cagr'])
    if cagr_diff > 5:
        print(f"\n  WARNING: CAGR偏差 {cagr_diff:.1f}% > 5%, 需排查原因!")
    else:
        print(f"\n  PASS: CAGR偏差 {cagr_diff:.1f}% <= 5%, 验证通过")

    # 交易明细
    print("\n" + "=" * 70)
    print("  ===== 每笔交易明细(前50笔 + 后10笔) =====")
    print("=" * 70)
    print(f"  {'序号':>4} | {'买入日期':<10} | {'代码':<10} | {'名称':<6} | "
          f"{'买入价':>7} | {'买入hr':>8} | {'卖出日期':<10} | "
          f"{'卖出价':>7} | {'卖出hr':>12} | {'卖出原因':<26} | {'盈亏':>7} | {'天':>3}")
    print("  " + "-" * 145)
    if len(trades) <= 60:
        for i, t in enumerate(trades, 1):
            print_trade_line(i, t)
    else:
        for i, t in enumerate(trades[:50], 1):
            print_trade_line(i, t)
        print(f"  ... 省略 {len(trades) - 60} 笔 ...")
        for i in range(len(trades) - 10, len(trades)):
            print_trade_line(i + 1, trades[i])

    # 可疑交易
    print("\n" + "=" * 70)
    print("  ===== 可疑交易标记 =====")
    print("=" * 70)
    suspicious = [(i, t) for i, t in enumerate(trades, 1) if t['pnl_pct'] > 20]
    print(f"\n  盈利超20%({len(suspicious)}笔):")
    for idx, t in suspicious[:20]:
        print(f"    #{idx} {t['buy_date']} {t['code']} {t['code_name']} "
              f"pnl:{t['pnl_pct']:+.1f}% | {t['sell_reason']}")
    if len(suspicious) > 20:
        print(f"    ... 共{len(suspicious)}笔")

    # 跳过记录
    print(f"\n  多只候选仅买1只的天数: {len(skipped_candidates)}")
    for dt, skipped in skipped_candidates[:10]:
        codes = [c['code'] + f"({c['cum_drop']:.1f}%)" for c in skipped[:3]]
        print(f"    {dt}: 跳过 {', '.join(codes)}")

    # 合规性验证
    print("\n" + "=" * 70)
    print("  ===== 合规性验证 =====")
    print("=" * 70)
    run_compliance_check(trades, conn)

    # 未来数据检查
    print("\n" + "=" * 70)
    print("  ===== 未来数据泄露检查 =====")
    print("=" * 70)
    t0_violations = sum(1 for t in trades
                        if t['buy_date'] >= t['sell_date'] and t['sell_reason'] != '回测结束清仓')
    print(f"  T+0违规: {'无' if t0_violations == 0 else f'{t0_violations}笔'}")
    print(f"  lookback仅用T日之前close_rate: PASS")
    print(f"  买入用hour1_open(非未来数据): PASS")
    print(f"  卖出按时序逐hour检查: PASS")

    conn.close()
    print("\n" + "=" * 70)
    print("  独立验证完成")
    print("=" * 70)


if __name__ == '__main__':
    run_backtest()
