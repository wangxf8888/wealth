#!/usr/bin/env python3
"""冲高7%后第二波策略 - 2021-2026年完整回测
策略: 昨日冲高7%回落，今日hour1开盘买入
卖出: 5%止盈 / -3%止损 / 5天强平
"""
import sys, sqlite3, math, time
from collections import defaultdict
from datetime import datetime, timedelta
sys.path.insert(0, '/home/AIWealth/scripts')
from compliance_utils import (
    safe_float, is_limit_up, is_limit_down, is_one_word_board,
    is_st, get_limit_threshold, generate_compliance_report
)

# ============ 配置参数 ============
DB_PATH = '/home/AIWealth/data/stocks.db'
START_DATE = '2021-01-01'
END_DATE = '2026-06-30'
INITIAL_CAPITAL = 1_000_000.0
N_SLOTS = 3
# 策略参数
SURGE_THRESHOLD = 7.0        # 冲高阈值(%)
FALLBACK_RATIO = 0.7         # 回落系数
TP_PCT = 5.0                 # 止盈(%)
SL_PCT = -3.0                # 止损(%)
MAX_HOLD_DAYS = 5            # 最大持有交易日
BUY_HOUR = 1                 # 买入hour
# ==================================


def load_trading_days(conn):
    """加载回测区间内所有交易日"""
    cur = conn.cursor()
    # 多取前几天用于 prev_day 查询
    lo = (datetime.strptime(START_DATE, "%Y-%m-%d") - timedelta(days=30)).strftime("%Y-%m-%d")
    cur.execute(
        "SELECT DISTINCT date FROM stock_kline WHERE date>=? AND date<=? ORDER BY date",
        (lo, END_DATE)
    )
    all_dates = [r[0] for r in cur.fetchall()]
    date_idx = {d: i for i, d in enumerate(all_dates)}
    bt_dates = [d for d in all_dates if START_DATE <= d <= END_DATE]
    return all_dates, date_idx, bt_dates


def get_prev_day_candidates(conn, prev_date):
    """获取前一日满足冲高回落条件的候选股
    条件: high_rate>=7, close_rate < high_rate*0.7, 非ST, 非北交所, 非涨停封板, 非一字板
    """
    cur = conn.cursor()
    cur.execute("""
        SELECT code, code_name, high_rate, close_rate, preclose, close,
               high, low, open, isST
        FROM stock_kline
        WHERE date=?
          AND high_rate >= ?
          AND code NOT LIKE 'bj.%%'
          AND isST = 0
          AND code_name NOT LIKE '%%ST%%'
          AND preclose > 0
    """, (prev_date, SURGE_THRESHOLD))

    candidates = []
    for row in cur:
        code, name, high_rate, close_rate, preclose, close_p = row[0], row[1], row[2], row[3], row[4], row[5]
        high_p, low_p, open_p, is_st_flag = row[6], row[7], row[8], row[9]

        high_rate = safe_float(high_rate)
        close_rate = safe_float(close_rate)
        preclose = safe_float(preclose)
        close_p = safe_float(close_p)
        high_p = safe_float(high_p)
        low_p = safe_float(low_p)
        open_p = safe_float(open_p)

        if high_rate <= 0 or preclose <= 0:
            continue

        # 冲高回落条件
        if close_rate >= high_rate * FALLBACK_RATIO:
            continue

        # 排除涨停封板（close达到涨停价）
        if close_p > 0 and is_limit_up(code, close_p, preclose):
            continue

        # 排除一字板（全天OHLC相同）
        if is_one_word_board(open_p, high_p, low_p, close_p):
            continue

        candidates.append({
            'code': code,
            'name': name or '',
            'high_rate': high_rate,
        })

    # 按冲高幅度降序排列
    candidates.sort(key=lambda x: -x['high_rate'])
    return candidates


def get_day_data(conn, date, code):
    """获取某股票某日完整K线数据"""
    cur = conn.cursor()
    cur.execute("""
        SELECT preclose, open, high, low, close, code_name, isST,
               hour1_open, hour1_high, hour1_low, hour1_close,
               hour2_open, hour2_high, hour2_low, hour2_close,
               hour3_open, hour3_high, hour3_low, hour3_close,
               hour4_open, hour4_high, hour4_low, hour4_close
        FROM stock_kline WHERE date=? AND code=?
    """, (date, code))
    row = cur.fetchone()
    if not row:
        return None
    return {
        'preclose': row[0], 'open': row[1], 'high': row[2], 'low': row[3],
        'close': row[4], 'code_name': row[5], 'isST': row[6],
        'hour1_open': row[7], 'hour1_high': row[8], 'hour1_low': row[9], 'hour1_close': row[10],
        'hour2_open': row[11], 'hour2_high': row[12], 'hour2_low': row[13], 'hour2_close': row[14],
        'hour3_open': row[15], 'hour3_high': row[16], 'hour3_low': row[17], 'hour3_close': row[18],
        'hour4_open': row[19], 'hour4_high': row[20], 'hour4_low': row[21], 'hour4_close': row[22],
    }


def get_hour_price(day_data, hour, field):
    """获取指定hour的指定字段价格"""
    key = f'hour{hour}_{field}'
    return safe_float(day_data.get(key))


def check_limit_down_block(code, day_data, hour):
    """检查当hour是否为跌停一字板，如果是则无法卖出"""
    h_open = get_hour_price(day_data, hour, 'open')
    h_high = get_hour_price(day_data, hour, 'high')
    h_low = get_hour_price(day_data, hour, 'low')
    h_close = get_hour_price(day_data, hour, 'close')
    preclose = safe_float(day_data.get('preclose'))

    if not all([h_open, h_high, h_low, h_close, preclose]):
        return False

    # 跌停一字板：一字板 + 跌停价
    if is_one_word_board(h_open, h_high, h_low, h_close):
        if is_limit_down(code, h_close, preclose):
            return True
    return False


def run_backtest(conn, all_dates, date_idx, bt_dates):
    """主回测循环"""
    cash = INITIAL_CAPITAL
    positions = []  # [{code, name, buy_price, buy_date, hold_days}]
    trades = []     # 完成的交易记录
    equity_curve = []  # [(date, equity)]

    total_days = len(bt_dates)
    t0 = time.time()

    for day_i, today in enumerate(bt_dates):
        # 进度显示
        if day_i % 200 == 0 and day_i > 0:
            elapsed = time.time() - t0
            speed = day_i / elapsed
            eta = (total_days - day_i) / speed if speed > 0 else 0
            print(f"  进度: {day_i}/{total_days} ({day_i/total_days*100:.1f}%) "
                  f"| 交易={len(trades)} | 持仓={len(positions)} | ETA={eta:.0f}s")

        # Step 1: 获取前一日候选（用于今日买入决策）
        ti = date_idx.get(today)
        prev_date = all_dates[ti - 1] if ti and ti > 0 else None
        candidates = get_prev_day_candidates(conn, prev_date) if prev_date else []

        # Step 2: 小时级循环
        for hour in [1, 2, 3, 4]:
            # === 2a. 检查卖出 ===
            survived = []
            for pos in positions:
                # T+1: 买入当日不卖出
                if pos['buy_date'] == today:
                    survived.append(pos)
                    continue

                day_data = pos.get('_day_cache')
                if day_data is None:
                    day_data = get_day_data(conn, today, pos['code'])
                    pos['_day_cache'] = day_data

                if day_data is None:
                    survived.append(pos)
                    continue

                # 跌停一字板检查：无法卖出
                if check_limit_down_block(pos['code'], day_data, hour):
                    survived.append(pos)
                    continue

                h_close = get_hour_price(day_data, hour, 'close')
                if not h_close:
                    survived.append(pos)
                    continue

                pnl_pct = (h_close / pos['buy_price'] - 1) * 100
                sell_reason = None

                # 止盈
                if pnl_pct >= TP_PCT:
                    sell_reason = '止盈'
                # 止损
                elif pnl_pct <= SL_PCT:
                    sell_reason = '止损'
                # 强平
                elif pos['hold_days'] >= MAX_HOLD_DAYS and hour == 4:
                    sell_reason = '强平'

                if sell_reason:
                    proceeds = pos['shares'] * h_close
                    cash += proceeds
                    trades.append({
                        'code': pos['code'],
                        'code_name': pos['name'],
                        'buy_date': pos['buy_date'],
                        'buy_price': pos['buy_price'],
                        'buy_hour': BUY_HOUR,
                        'sell_date': today,
                        'sell_price': h_close,
                        'sell_hour': hour,
                        'pnl_pct': pnl_pct,
                        'hold_days': pos['hold_days'],
                        'reason': sell_reason,
                        'shares': pos['shares'],
                    })
                else:
                    survived.append(pos)

            positions = survived

            # === 2b. 检查买入 (只在 BUY_HOUR) ===
            if hour == BUY_HOUR and len(positions) < N_SLOTS and candidates:
                held_codes = {p['code'] for p in positions}
                for cand in candidates:
                    if len(positions) >= N_SLOTS:
                        break
                    if cand['code'] in held_codes:
                        continue

                    day_data = get_day_data(conn, today, cand['code'])
                    if day_data is None:
                        continue

                    # 排除ST
                    if is_st(day_data.get('code_name', ''), day_data.get('isST', 0)):
                        continue

                    # 获取hour1数据
                    h1_open = safe_float(day_data.get('hour1_open'))
                    h1_high = safe_float(day_data.get('hour1_high'))
                    h1_low = safe_float(day_data.get('hour1_low'))
                    h1_close = safe_float(day_data.get('hour1_close'))

                    if not h1_open or h1_open <= 0:
                        continue

                    # 排除一字板
                    if is_one_word_board(h1_open, h1_high, h1_low, h1_close):
                        continue

                    # 排除涨停开盘
                    preclose = safe_float(day_data.get('preclose'))
                    if preclose > 0 and is_limit_up(cand['code'], h1_open, preclose):
                        continue

                    # 买入
                    buy_price = h1_open
                    free_slots = N_SLOTS - len(positions)
                    alloc = cash / free_slots if free_slots > 0 else cash
                    shares = int(alloc / buy_price // 100) * 100
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
                        '_day_cache': day_data,
                    })
                    held_codes.add(cand['code'])

        # Step 3: 日终 - 更新hold_days, 清除day_cache
        for pos in positions:
            if pos['buy_date'] != today:
                pos['hold_days'] += 1
            pos['_day_cache'] = None  # 释放内存

        # 计算日终权益
        equity = cash
        for pos in positions:
            day_data = get_day_data(conn, today, pos['code'])
            if day_data:
                h4c = safe_float(day_data.get('hour4_close'))
                cls = safe_float(day_data.get('close'))
                price = h4c if h4c > 0 else (cls if cls > 0 else pos['buy_price'])
            else:
                price = pos['buy_price']
            equity += pos['shares'] * price
        equity_curve.append((today, equity))

    # 回测结束，强制清仓
    if positions:
        last_day = bt_dates[-1]
        for pos in positions:
            day_data = get_day_data(conn, last_day, pos['code'])
            if day_data:
                h4c = safe_float(day_data.get('hour4_close'))
                cls = safe_float(day_data.get('close'))
                sell_price = h4c if h4c > 0 else (cls if cls > 0 else pos['buy_price'])
            else:
                sell_price = pos['buy_price']
            pnl_pct = (sell_price / pos['buy_price'] - 1) * 100
            proceeds = pos['shares'] * sell_price
            cash += proceeds
            trades.append({
                'code': pos['code'],
                'code_name': pos['name'],
                'buy_date': pos['buy_date'],
                'buy_price': pos['buy_price'],
                'buy_hour': BUY_HOUR,
                'sell_date': last_day,
                'sell_price': sell_price,
                'sell_hour': 4,
                'pnl_pct': pnl_pct,
                'hold_days': pos['hold_days'],
                'reason': '期末清仓',
                'shares': pos['shares'],
            })
        positions = []

    return trades, equity_curve


def compute_stats(trades, equity_curve, initial_cap):
    """计算整体统计"""
    n = len(trades)
    if n == 0:
        return {}

    wins = sum(1 for t in trades if t['pnl_pct'] > 0)
    losses = sum(1 for t in trades if t['pnl_pct'] <= 0)
    win_rate = wins / n * 100

    avg_win = sum(t['pnl_pct'] for t in trades if t['pnl_pct'] > 0) / max(1, wins)
    avg_loss = sum(t['pnl_pct'] for t in trades if t['pnl_pct'] <= 0) / max(1, losses)
    profit_loss_ratio = abs(avg_win / avg_loss) if avg_loss != 0 else 999

    final_equity = equity_curve[-1][1] if equity_curve else initial_cap
    total_return = (final_equity / initial_cap - 1) * 100

    # CAGR
    if len(equity_curve) >= 2:
        d0 = datetime.strptime(equity_curve[0][0], "%Y-%m-%d")
        d1 = datetime.strptime(equity_curve[-1][0], "%Y-%m-%d")
        years = max(0.1, (d1 - d0).days / 365.25)
    else:
        years = 1
    cagr = ((final_equity / initial_cap) ** (1 / years) - 1) * 100 if final_equity > 0 else -100

    # 最大回撤
    peak = equity_curve[0][1] if equity_curve else initial_cap
    max_dd = 0
    for _, eq in equity_curve:
        if eq > peak:
            peak = eq
        dd = (peak - eq) / peak * 100 if peak > 0 else 0
        if dd > max_dd:
            max_dd = dd

    return {
        'total_trades': n,
        'wins': wins,
        'losses': losses,
        'win_rate': win_rate,
        'avg_win': avg_win,
        'avg_loss': avg_loss,
        'profit_loss_ratio': profit_loss_ratio,
        'total_return': total_return,
        'cagr': cagr,
        'max_drawdown': max_dd,
        'final_equity': final_equity,
        'years': years,
    }


def compute_yearly_stats(trades, equity_curve, initial_cap):
    """按年度计算统计"""
    by_year = defaultdict(list)
    for t in trades:
        by_year[t['sell_date'][:4]].append(t)

    # 获取每年末权益
    eq_by_year = {}
    for d, eq in equity_curve:
        eq_by_year[d[:4]] = eq

    results = []
    prev_eq = initial_cap
    for year in sorted(by_year.keys()):
        year_trades = by_year[year]
        n = len(year_trades)
        wins = sum(1 for t in year_trades if t['pnl_pct'] > 0)
        win_rate = wins / n * 100 if n > 0 else 0

        year_end_eq = eq_by_year.get(year, prev_eq)
        year_return = (year_end_eq / prev_eq - 1) * 100 if prev_eq > 0 else 0

        avg_win = sum(t['pnl_pct'] for t in year_trades if t['pnl_pct'] > 0) / max(1, wins)
        losses = sum(1 for t in year_trades if t['pnl_pct'] <= 0)
        avg_loss = sum(t['pnl_pct'] for t in year_trades if t['pnl_pct'] <= 0) / max(1, losses)
        plr = abs(avg_win / avg_loss) if avg_loss != 0 else 999

        results.append({
            'year': year,
            'trades': n,
            'win_rate': win_rate,
            'return': year_return,
            'profit_loss_ratio': plr,
        })
        prev_eq = year_end_eq

    return results


def print_trade_details(trades, max_print=50):
    """打印交易明细"""
    print(f"\n{'='*60}")
    print(f"交易明细 (共{len(trades)}笔, 显示前{min(max_print, len(trades))}笔)")
    print(f"{'='*60}")
    for t in trades[:max_print]:
        sign = '+' if t['pnl_pct'] >= 0 else ''
        print(f"[TRADE] 买入:{t['buy_date']} {t['code']} {t['code_name']} "
              f"@{t['buy_price']:.2f} | "
              f"卖出:{t['sell_date']} @{t['sell_price']:.2f} h{t['sell_hour']} | "
              f"收益:{sign}{t['pnl_pct']:.1f}% | "
              f"持有:{t['hold_days']}天 | 原因:{t['reason']}")


def print_report(stats, yearly_stats, trades):
    """打印回测报告"""
    print(f"\n{'='*60}")
    print(f"===== 冲高7%后第二波 策略回测报告 =====")
    print(f"{'='*60}")
    print(f"数据范围: {START_DATE} ~ {END_DATE}")
    print(f"参数: TP={TP_PCT}%, SL={SL_PCT}%, MaxHold={MAX_HOLD_DAYS}天, Slots={N_SLOTS}")
    print(f"选股: 昨日冲高>={SURGE_THRESHOLD}%, 回落系数<{FALLBACK_RATIO}")
    print(f"{'─'*60}")
    print(f"总交易: {stats['total_trades']}笔")
    print(f"总收益率: {stats['total_return']:+.1f}%")
    print(f"CAGR: {stats['cagr']:+.1f}%")
    print(f"胜率: {stats['win_rate']:.1f}%")
    print(f"盈亏比: {stats['profit_loss_ratio']:.2f}")
    print(f"最大回撤: -{stats['max_drawdown']:.1f}%")
    print(f"终值: {stats['final_equity']:,.0f}")
    print(f"年数: {stats['years']:.1f}")

    # 年度汇总
    print(f"\n{'─'*60}")
    print(f"{'年度':<6} {'收益率':<10} {'交易':<8} {'胜率':<8} {'盈亏比':<8}")
    print(f"{'─'*60}")
    for ys in yearly_stats:
        print(f"[YEAR] {ys['year']}: 收益率={ys['return']:+.1f}% "
              f"交易={ys['trades']}笔 胜率={ys['win_rate']:.1f}% "
              f"盈亏比={ys['profit_loss_ratio']:.1f}")


def run_compliance_check(trades, conn):
    """运行合规检查"""
    print(f"\n{'='*60}")
    print("合规性验证...")
    print(f"{'='*60}")

    # 构造 compliance_utils 需要的格式
    compliance_trades = []
    for t in trades:
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

    report = generate_compliance_report(compliance_trades, conn)
    return report


def main():
    print("=" * 60)
    print("冲高7%后第二波策略 - 完整回测")
    print(f"时间范围: {START_DATE} ~ {END_DATE}")
    print(f"资金: {INITIAL_CAPITAL:,.0f} | 仓位: {N_SLOTS}仓")
    print(f"止盈: {TP_PCT}% | 止损: {SL_PCT}% | 最大持有: {MAX_HOLD_DAYS}天")
    print("=" * 60)

    conn = sqlite3.connect(DB_PATH)
    t0 = time.time()

    # 加载交易日
    print("\n[1] 加载交易日列表...")
    all_dates, date_idx, bt_dates = load_trading_days(conn)
    print(f"  全部交易日: {len(all_dates)} | 回测区间: {len(bt_dates)}天")
    print(f"  范围: {bt_dates[0]} ~ {bt_dates[-1]}")

    # 执行回测
    print(f"\n[2] 执行回测...")
    trades, equity_curve = run_backtest(conn, all_dates, date_idx, bt_dates)
    elapsed = time.time() - t0
    print(f"  回测完成! 耗时: {elapsed:.1f}s | 总交易: {len(trades)}笔")

    # 统计
    print(f"\n[3] 计算统计...")
    stats = compute_stats(trades, equity_curve, INITIAL_CAPITAL)
    yearly_stats = compute_yearly_stats(trades, equity_curve, INITIAL_CAPITAL)

    # 输出
    print_trade_details(trades, max_print=30)
    print_report(stats, yearly_stats, trades)

    # 合规验证
    print(f"\n[4] 合规验证 (抽样前200笔)...")
    sample_trades = trades[:200] if len(trades) > 200 else trades
    run_compliance_check(sample_trades, conn)

    conn.close()
    print(f"\n总耗时: {time.time() - t0:.1f}s")
    print(f"完成时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")


if __name__ == '__main__':
    main()
