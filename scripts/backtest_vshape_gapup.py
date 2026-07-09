#!/usr/bin/env python3
"""V字形态高开买入策略 - 2021-2026年完整回测
策略: T-3~T-1累计跌幅≤-5%且T日高开2-5%，hour1开盘买入
卖出: 3%止盈 / -2%止损 / 3天强平
"""
import sys, sqlite3, math, time
from collections import defaultdict
from datetime import datetime
sys.path.insert(0, '/home/AIWealth/scripts')
from compliance_utils import (
    safe_float, is_one_word_board, is_limit_up, is_limit_down,
    is_st, get_limit_threshold, generate_compliance_report
)

# ============ 配置参数 ============
DB_PATH = '/home/AIWealth/data/stocks.db'
START_DATE = '2021-01-01'
END_DATE = '2026-06-30'
INITIAL_CAPITAL = 1_000_000.0
N_SLOTS = 3
# V字形态参数
V_LOOKBACK_DAYS = 3          # 回看天数
V_CUM_DROP = -5.0            # 累计跌幅阈值(%)
V_MIN_DOWN_DAYS = 2          # 至少N天下跌
GAP_UP_MIN = 2.0             # 高开下限(%)
GAP_UP_MAX = 5.0             # 高开上限(%)
# 卖出参数
TP_PCT = 3.0                 # 止盈(%)
SL_PCT = -2.0                # 止损(%)
MAX_HOLD_DAYS = 3            # 最大持有交易日
BUY_HOUR = 1                 # 买入hour
# ==================================

HOUR_FIELDS = {
    1: ('hour1_open', 'hour1_high', 'hour1_low', 'hour1_close'),
    2: ('hour2_open', 'hour2_high', 'hour2_low', 'hour2_close'),
    3: ('hour3_open', 'hour3_high', 'hour3_low', 'hour3_close'),
    4: ('hour4_open', 'hour4_high', 'hour4_low', 'hour4_close'),
}


def load_trading_dates(conn):
    """加载所有交易日"""
    cur = conn.cursor()
    cur.execute("SELECT DISTINCT date FROM stock_kline WHERE date>=? AND date<=? ORDER BY date",
                (START_DATE, END_DATE))
    return [r[0] for r in cur.fetchall()]


def get_prev_close_rates(conn, prev_dates):
    """批量查询前N日创业板/科创板所有股票的close_rate
    返回: {code: [(date, close_rate), ...]}
    """
    if not prev_dates:
        return {}
    ph = ','.join('?' * len(prev_dates))
    cur = conn.cursor()
    cur.execute(f"""SELECT code, date, close_rate FROM stock_kline
                   WHERE date IN ({ph})
                   AND (code LIKE 'sz.300%' OR code LIKE 'sh.688%')
                   AND isST=0""", prev_dates)
    result = defaultdict(list)
    for row in cur:
        cr = safe_float(row[2], None)
        if cr is not None:
            result[row[0]].append((row[1], cr))
    return result


def get_day_candidates(conn, date):
    """获取当日所有创业板/科创板股票日线数据"""
    cur = conn.cursor()
    cur.execute("""SELECT code, code_name, preclose, open, close, open_rate, close_rate,
                          high, low, isST,
                          hour1_open, hour1_high, hour1_low, hour1_close,
                          hour2_open, hour2_high, hour2_low, hour2_close,
                          hour3_open, hour3_high, hour3_low, hour3_close,
                          hour4_open, hour4_high, hour4_low, hour4_close
                   FROM stock_kline
                   WHERE date=? AND (code LIKE 'sz.300%' OR code LIKE 'sh.688%')""", (date,))
    rows = []
    for r in cur:
        rows.append({
            'code': r[0], 'code_name': r[1] or '', 'preclose': r[2],
            'open': r[3], 'close': r[4], 'open_rate': r[5], 'close_rate': r[6],
            'high': r[7], 'low': r[8], 'isST': r[9],
            'hour1_open': r[10], 'hour1_high': r[11], 'hour1_low': r[12], 'hour1_close': r[13],
            'hour2_open': r[14], 'hour2_high': r[15], 'hour2_low': r[16], 'hour2_close': r[17],
            'hour3_open': r[18], 'hour3_high': r[19], 'hour3_low': r[20], 'hour3_close': r[21],
            'hour4_open': r[22], 'hour4_high': r[23], 'hour4_low': r[24], 'hour4_close': r[25],
        })
    return rows


def get_hour_data(conn, code, date):
    """获取某只股票某日的小时数据"""
    cur = conn.cursor()
    cur.execute("""SELECT preclose, close,
                          hour1_open, hour1_high, hour1_low, hour1_close,
                          hour2_open, hour2_high, hour2_low, hour2_close,
                          hour3_open, hour3_high, hour3_low, hour3_close,
                          hour4_open, hour4_high, hour4_low, hour4_close
                   FROM stock_kline WHERE code=? AND date=?""", (code, date))
    row = cur.fetchone()
    if not row:
        return None
    return {
        'preclose': row[0], 'close': row[1],
        'hour1_open': row[2], 'hour1_high': row[3], 'hour1_low': row[4], 'hour1_close': row[5],
        'hour2_open': row[6], 'hour2_high': row[7], 'hour2_low': row[8], 'hour2_close': row[9],
        'hour3_open': row[10], 'hour3_high': row[11], 'hour3_low': row[12], 'hour3_close': row[13],
        'hour4_open': row[14], 'hour4_high': row[15], 'hour4_low': row[16], 'hour4_close': row[17],
    }


def check_limit_down_oneword(code, hdata, hour):
    """检查某hour是否为跌停一字板（无法卖出）"""
    o_f, h_f, l_f, c_f = HOUR_FIELDS[hour]
    ho = safe_float(hdata.get(o_f))
    hh = safe_float(hdata.get(h_f))
    hl = safe_float(hdata.get(l_f))
    hc = safe_float(hdata.get(c_f))
    preclose = safe_float(hdata.get('preclose'))
    if not ho or not preclose:
        return True  # 数据缺失视为不可交易
    # 跌停判定
    if not is_limit_down(code, hc, preclose):
        return False
    # 一字板判定
    if is_one_word_board(ho, hh, hl, hc):
        return True
    return False


def run_backtest(conn, trading_dates):
    """主回测循环"""
    date_idx = {d: i for i, d in enumerate(trading_dates)}
    cash = INITIAL_CAPITAL
    positions = []  # [{code, name, buy_price, buy_date, hold_days}]
    trades = []     # 完成的交易记录
    equity_curve = []  # [(date, equity)]

    for i, today in enumerate(trading_dates):
        if i < V_LOOKBACK_DAYS:
            continue

        # ========== 1. 获取前3日close_rate数据 ==========
        prev_dates = trading_dates[i - V_LOOKBACK_DAYS: i]
        prev_cr_map = get_prev_close_rates(conn, prev_dates)

        # ========== 2. 获取当日数据 ==========
        day_stocks = get_day_candidates(conn, today)

        # ========== 3. 筛选候选股 ==========
        candidates = []
        held_codes = {p['code'] for p in positions}

        for stock in day_stocks:
            code = stock['code']
            if code in held_codes:
                continue
            # ST排除
            if is_st(stock['code_name'], stock.get('isST', 0)):
                continue
            # open_rate范围
            opr = safe_float(stock.get('open_rate'), None)
            if opr is None or opr < GAP_UP_MIN or opr > GAP_UP_MAX:
                continue
            # 当日非涨停一字板（日线级）
            preclose = safe_float(stock.get('preclose'))
            day_open = safe_float(stock.get('open'))
            day_high = safe_float(stock.get('high'))
            day_low = safe_float(stock.get('low'))
            day_close = safe_float(stock.get('close'))
            if preclose <= 0 or day_open <= 0:
                continue
            if is_one_word_board(day_open, day_high, day_low, day_close):
                continue
            # 前3日检查
            cr_list = prev_cr_map.get(code, [])
            if len(cr_list) < V_LOOKBACK_DAYS:
                continue
            # 按日期排序确保顺序正确
            cr_list_sorted = sorted(cr_list, key=lambda x: x[0])
            cr_values = [x[1] for x in cr_list_sorted[-V_LOOKBACK_DAYS:]]
            if len(cr_values) < V_LOOKBACK_DAYS:
                continue
            cum_drop = sum(cr_values)
            down_days = sum(1 for c in cr_values if c < 0)
            if cum_drop > V_CUM_DROP:
                continue
            if down_days < V_MIN_DOWN_DAYS:
                continue
            # hour1非一字板、非涨停开盘
            h1o = safe_float(stock.get('hour1_open'))
            h1h = safe_float(stock.get('hour1_high'))
            h1l = safe_float(stock.get('hour1_low'))
            h1c = safe_float(stock.get('hour1_close'))
            if h1o <= 0:
                continue
            if is_one_word_board(h1o, h1h, h1l, h1c):
                continue
            # hour1非涨停开盘
            if preclose > 0 and is_limit_up(code, h1o, preclose):
                continue

            candidates.append({
                'code': code, 'name': stock['code_name'],
                'h1_open': h1o, 'cum_drop': cum_drop,
                'stock_data': stock
            })

        # 按累计跌幅绝对值降序排列（跌得越深优先）
        candidates.sort(key=lambda x: x['cum_drop'])  # cum_drop是负数，越小=跌得越深

        # ========== 4. 更新hold_days (在卖出检查之前) ==========
        for pos in positions:
            if pos['buy_date'] != today:
                pos['hold_days'] += 1

        # ========== 5. 小时级循环 ==========
        for hour in [1, 2, 3, 4]:
            o_f, h_f, l_f, c_f = HOUR_FIELDS[hour]

            # --- 5a. 检查卖出 ---
            survived = []
            for pos in positions:
                # T+1: 买入当日不可卖出
                if pos['buy_date'] == today:
                    survived.append(pos)
                    continue

                hdata = get_hour_data(conn, pos['code'], today)
                if not hdata:
                    survived.append(pos)
                    continue

                hc = safe_float(hdata.get(c_f))
                if hc <= 0:
                    survived.append(pos)
                    continue

                # 跌停一字板保护：无法卖出
                if check_limit_down_oneword(pos['code'], hdata, hour):
                    survived.append(pos)
                    continue

                pnl_pct = (hc - pos['buy_price']) / pos['buy_price'] * 100.0
                sell_reason = None

                # 止盈
                if pnl_pct >= TP_PCT:
                    sell_reason = '止盈'
                # 止损
                elif pnl_pct <= SL_PCT:
                    sell_reason = '止损'
                # 强平（持有>=MAX_HOLD_DAYS天，在hour4卖出）
                elif pos['hold_days'] >= MAX_HOLD_DAYS and hour == 4:
                    sell_reason = '强平'

                if sell_reason:
                    proceeds = pos['shares'] * hc
                    cash += proceeds
                    trades.append({
                        'code': pos['code'],
                        'code_name': pos['name'],
                        'buy_date': pos['buy_date'],
                        'buy_price': pos['buy_price'],
                        'buy_hour': BUY_HOUR,
                        'sell_date': today,
                        'sell_price': hc,
                        'sell_hour': hour,
                        'pnl_pct': pnl_pct,
                        'hold_days': pos['hold_days'],
                        'reason': sell_reason,
                    })
                else:
                    survived.append(pos)

            positions = survived

            # --- 5b. 检查买入 (仅hour1) ---
            if hour == BUY_HOUR:
                free_slots = N_SLOTS - len(positions)
                if free_slots > 0 and candidates:
                    held_now = {p['code'] for p in positions}
                    bought = 0
                    for cand in candidates:
                        if bought >= free_slots:
                            break
                        if cand['code'] in held_now:
                            continue
                        buy_price = cand['h1_open']
                        slot_capital = cash / max(1, free_slots - bought)
                        shares = int(slot_capital / buy_price // 100) * 100
                        if shares <= 0:
                            continue
                        cost = shares * buy_price
                        if cost > cash:
                            continue
                        cash -= cost
                        positions.append({
                            'code': cand['code'],
                            'name': cand['name'],
                            'buy_price': buy_price,
                            'buy_date': today,
                            'hold_days': 0,
                            'shares': shares,
                        })
                        held_now.add(cand['code'])
                        bought += 1

        # ========== 6. 日终: 计算净值 ==========
        # 计算当日净值
        equity = cash
        for pos in positions:
            hdata = get_hour_data(conn, pos['code'], today)
            if hdata:
                h4c = safe_float(hdata.get('hour4_close'))
                if h4c > 0:
                    equity += pos['shares'] * h4c
                else:
                    equity += pos['shares'] * pos['buy_price']
            else:
                equity += pos['shares'] * pos['buy_price']
        equity_curve.append((today, equity))

    # ========== 末日强平 ==========
    if positions and trading_dates:
        last_day = trading_dates[-1]
        for pos in positions:
            hdata = get_hour_data(conn, pos['code'], last_day)
            sp = pos['buy_price']
            if hdata:
                h4c = safe_float(hdata.get('hour4_close'))
                if h4c > 0:
                    sp = h4c
            pnl_pct = (sp - pos['buy_price']) / pos['buy_price'] * 100.0
            proceeds = pos['shares'] * sp
            cash += proceeds
            trades.append({
                'code': pos['code'],
                'code_name': pos['name'],
                'buy_date': pos['buy_date'],
                'buy_price': pos['buy_price'],
                'buy_hour': BUY_HOUR,
                'sell_date': last_day,
                'sell_price': sp,
                'sell_hour': 4,
                'pnl_pct': pnl_pct,
                'hold_days': pos['hold_days'],
                'reason': '末日强平',
            })

    return trades, equity_curve


def compute_stats(trades, equity_curve, capital):
    """计算总体统计"""
    n = len(trades)
    if n == 0:
        return {}
    final_eq = equity_curve[-1][1] if equity_curve else capital
    total_ret = (final_eq / capital - 1) * 100

    # CAGR
    if len(equity_curve) >= 2:
        d0 = datetime.strptime(equity_curve[0][0], '%Y-%m-%d')
        d1 = datetime.strptime(equity_curve[-1][0], '%Y-%m-%d')
        years = max(0.05, (d1 - d0).days / 365.25)
    else:
        years = 1.0
    cagr = ((final_eq / capital) ** (1 / years) - 1) * 100 if final_eq > 0 else -100

    # 胜率 / 盈亏比
    wins = [t for t in trades if t['pnl_pct'] > 0]
    losses = [t for t in trades if t['pnl_pct'] <= 0]
    win_rate = len(wins) / n * 100
    avg_win = sum(t['pnl_pct'] for t in wins) / len(wins) if wins else 0
    avg_loss = abs(sum(t['pnl_pct'] for t in losses) / len(losses)) if losses else 1
    profit_factor = avg_win / avg_loss if avg_loss > 0 else 999

    # 最大回撤
    peak = equity_curve[0][1] if equity_curve else capital
    max_dd = 0
    for _, eq in equity_curve:
        if eq > peak:
            peak = eq
        dd = (peak - eq) / peak * 100 if peak > 0 else 0
        if dd > max_dd:
            max_dd = dd

    return {
        'total_trades': n,
        'total_return': total_ret,
        'cagr': cagr,
        'win_rate': win_rate,
        'profit_factor': profit_factor,
        'max_drawdown': max_dd,
        'avg_pnl': sum(t['pnl_pct'] for t in trades) / n,
        'final_equity': final_eq,
    }


def compute_yearly(trades, equity_curve, capital):
    """按年度统计"""
    by_year = defaultdict(list)
    for t in trades:
        by_year[t['sell_date'][:4]].append(t)

    # 年末净值
    year_end_eq = {}
    for d, eq in equity_curve:
        year_end_eq[d[:4]] = eq

    results = []
    prev_eq = capital
    for year in sorted(by_year.keys()):
        ts = by_year[year]
        n = len(ts)
        wins = sum(1 for t in ts if t['pnl_pct'] > 0)
        wr = wins / n * 100 if n > 0 else 0
        end_eq = year_end_eq.get(year, prev_eq)
        yr_ret = (end_eq / prev_eq - 1) * 100 if prev_eq > 0 else 0

        avg_win = sum(t['pnl_pct'] for t in ts if t['pnl_pct'] > 0)
        avg_loss = abs(sum(t['pnl_pct'] for t in ts if t['pnl_pct'] <= 0))
        pf = (avg_win / avg_loss) if avg_loss > 0 else 999

        results.append((year, n, wr, yr_ret, pf))
        prev_eq = end_eq
    return results


def main():
    t0 = time.time()
    print("=" * 65)
    print("  V字形态高开买入 策略回测")
    print(f"  数据范围: {START_DATE} ~ {END_DATE}")
    print(f"  参数: V_CUM_DROP={V_CUM_DROP}%, GAP_UP=[{GAP_UP_MIN}%,{GAP_UP_MAX}%], "
          f"TP={TP_PCT}%, SL={SL_PCT}%, MaxHold={MAX_HOLD_DAYS}天, Slots={N_SLOTS}")
    print("=" * 65)

    conn = sqlite3.connect(DB_PATH)

    # 加载交易日
    trading_dates = load_trading_dates(conn)
    print(f"\n交易日总数: {len(trading_dates)} | 加载耗时: {time.time()-t0:.1f}s")

    # 执行回测
    print("\n[回测中] ...", flush=True)
    trades, equity_curve = run_backtest(conn, trading_dates)
    print(f"[回测完成] 耗时: {time.time()-t0:.1f}s | 交易笔数: {len(trades)}")

    # ===== 交易明细 =====
    print(f"\n{'='*65}")
    print("  交易明细 (前50笔 + 后20笔)")
    print(f"{'='*65}")
    for t in trades[:50]:
        print(f"[TRADE] 买入:{t['buy_date']} {t['code']} {t['code_name']} @{t['buy_price']:.2f} | "
              f"卖出:{t['sell_date']} @{t['sell_price']:.2f} h{t['sell_hour']} | "
              f"收益:{t['pnl_pct']:+.1f}% | 持有:{t['hold_days']}天 | 原因:{t['reason']}")
    if len(trades) > 70:
        print(f"  ... 省略中间 {len(trades)-70} 笔 ...")
    if len(trades) > 50:
        for t in trades[-20:]:
            print(f"[TRADE] 买入:{t['buy_date']} {t['code']} {t['code_name']} @{t['buy_price']:.2f} | "
                  f"卖出:{t['sell_date']} @{t['sell_price']:.2f} h{t['sell_hour']} | "
                  f"收益:{t['pnl_pct']:+.1f}% | 持有:{t['hold_days']}天 | 原因:{t['reason']}")

    # ===== 年度汇总 =====
    yearly = compute_yearly(trades, equity_curve, INITIAL_CAPITAL)
    print(f"\n{'='*65}")
    print("  年度汇总")
    print(f"{'='*65}")
    for year, n, wr, yr_ret, pf in yearly:
        print(f"[YEAR] {year}: 收益率={yr_ret:+.1f}% 交易={n}笔 胜率={wr:.1f}% 盈亏比={pf:.2f}")

    # ===== 总体统计 =====
    stats = compute_stats(trades, equity_curve, INITIAL_CAPITAL)
    print(f"\n{'='*65}")
    print("  ===== V字形态高开买入 策略回测报告 =====")
    print(f"{'='*65}")
    print(f"  数据范围: {START_DATE} ~ {END_DATE}")
    print(f"  参数: V_CUM_DROP={V_CUM_DROP}%, GAP_UP=[{GAP_UP_MIN}%,{GAP_UP_MAX}%], "
          f"TP={TP_PCT}%, SL={SL_PCT}%, MaxHold={MAX_HOLD_DAYS}天, Slots={N_SLOTS}")
    print(f"  总交易: {stats.get('total_trades', 0)}笔")
    print(f"  总收益率: {stats.get('total_return', 0):+.1f}%")
    print(f"  CAGR: {stats.get('cagr', 0):+.1f}%")
    print(f"  胜率: {stats.get('win_rate', 0):.1f}%")
    print(f"  盈亏比: {stats.get('profit_factor', 0):.2f}")
    print(f"  最大回撤: -{stats.get('max_drawdown', 0):.1f}%")
    print(f"  终值: {stats.get('final_equity', INITIAL_CAPITAL):,.0f}")
    print(f"  平均每笔收益: {stats.get('avg_pnl', 0):+.2f}%")

    # ===== 合规验证 =====
    print("\n[合规验证] 抽样验证前100笔交易...")
    sample = trades[:100] if len(trades) > 100 else trades
    compliance_trades = []
    for t in sample:
        compliance_trades.append({
            'code': t['code'],
            'code_name': t['code_name'],
            'buy_date': t['buy_date'],
            'buy_price': t['buy_price'],
            'buy_hour': t['buy_hour'],
            'sell_date': t['sell_date'],
            'sell_price': t['sell_price'],
            'sell_hour': t['sell_hour'],
            'pnl_pct': t['pnl_pct'],
        })
    generate_compliance_report(compliance_trades, conn)

    conn.close()
    print(f"\n总耗时: {time.time()-t0:.1f}s")


if __name__ == '__main__':
    main()
