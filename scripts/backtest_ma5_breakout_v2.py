#!/usr/bin/env python3
"""MA5突破大盘股(>700亿) V2 - 多方案止损止盈对比回测

修复点:
  1. 止损/止盈基于日收盘价判断，不用日内hour级数据
  2. 测试无止损方案（让到期自然退出）
  3. 加入缩量整理条件（方案F系列）

方案列表:
  A: 无止损/无止盈, 持1天, T+1 hour1_open 卖出
  B: 无止损/无止盈, 持1天, T+1 day_close 卖出
  C: 止损-5% / 止盈+8%, 最大2天, 日收盘价判断
  D: 止损-7% / 止盈+10%, 最大3天, 日收盘价判断
  E: 无止损 / 止盈+5%, 最大2天, 到期按close卖

  F_A ~ F_E: 在A-E基础上加入缩量整理+微幅突破过滤

用法: python3 scripts/backtest_ma5_breakout_v2.py
"""
import sys, os, json, math, time, sqlite3, statistics
from collections import defaultdict, deque
from datetime import datetime

# ============== 配置区 ==============
DB_PATH = '/home/AIWealth/data/stocks.db'
START_DATE = '2021-01-01'
END_DATE = '2026-06-30'
INIT_CAPITAL = 1_000_000.0
N_SLOTS = 5
COST_RATE = 0.001
MIN_MARKET_CAP = 700   # 亿
MIN_TURNOVER = 1.0     # %
MIN_RECENT_DROP = 5.0  # 近10日回撤最少%
LOG_DIR = '/home/AIWealth/scripts/logs'
SUMMARY_LOG = f'{LOG_DIR}/ma5_breakout_v2_summary.log'
BEST_TRADES_JSON = f'{LOG_DIR}/ma5_breakout_v2_best_trades.json'
POSITION_LOG = f'{LOG_DIR}/ma5_breakout_v2_positions.log'
# ===================================

# 方案定义: (name, sl_pct, tp_pct, max_hold_days, sell_mode, use_shrink_filter)
# sell_mode: 'h1_open' | 'day_close' | 'daily_check'
PLANS = [
    ('A',   None, None,  1, 'h1_open',    False),
    ('B',   None, None,  1, 'day_close',  False),
    ('C',   5.0,  8.0,   2, 'daily_check', False),
    ('D',   7.0,  10.0,  3, 'daily_check', False),
    ('E',   None, 5.0,   2, 'daily_check', False),
    ('F_A', None, None,  1, 'h1_open',    True),
    ('F_B', None, None,  1, 'day_close',  True),
    ('F_C', 5.0,  8.0,   2, 'daily_check', True),
    ('F_D', 7.0,  10.0,  3, 'daily_check', True),
    ('F_E', None, 5.0,   2, 'daily_check', True),
]


def sv(v):
    if v is None:
        return 0.0
    try:
        f = float(v)
        return 0.0 if (math.isnan(f) or math.isinf(f)) else f
    except (TypeError, ValueError):
        return 0.0


def get_limit_ratio(code):
    c = code.replace('sz.', '').replace('sh.', '').replace('bj.', '')
    if c.startswith('300') or c.startswith('301') or c.startswith('688'):
        return 0.2
    if code.startswith('bj.'):
        return 0.3
    return 0.1


def calc_limit_up(preclose, code):
    return round(preclose * (1 + get_limit_ratio(code)), 2)


def calc_limit_down(preclose, code):
    return round(preclose * (1 - get_limit_ratio(code)), 2)


def run_backtest(plan_name, sl_pct, tp_pct, max_hold_days, sell_mode, use_shrink,
                 trading_dates, all_days, all_days_idx, conn, warmup_data):
    """运行单个方案回测, 返回 (sell_trades, nav_history, pos_log_lines)"""
    cur = conn.cursor()

    # 滑动窗口：保留最近11天的数据
    close_window = deque(maxlen=11)
    for item in warmup_data:
        close_window.append(item)

    cash = INIT_CAPITAL
    slots = [{'id': i, 'code': None, 'name': '', 'buy_price': 0,
              'buy_date': None, 'shares': 0, 'hold_days': 0, 'preclose': 0}
             for i in range(N_SLOTS)]
    sell_trades = []
    nav_history = []
    pos_log = []
    total_days = len(trading_dates)

    prev_extra_map = close_window[-1][2] if close_window else {}

    def get_nav(prices=None):
        t = cash
        for s in slots:
            if s['code']:
                p = s['buy_price']
                if prices and s['code'] in prices:
                    p = prices[s['code']]
                t += s['shares'] * p
        return t

    for day_i, date in enumerate(trading_dates):
        # 加载今日数据
        cur.execute("""
            SELECT code, code_name, preclose, open, high, low, close,
                   turn, amount, isST, volume,
                   hour1_open, hour1_high, hour1_low, hour1_close
            FROM stock_kline WHERE date = ?
        """, (date,))
        day_dict = {}
        for r in cur.fetchall():
            day_dict[r[0]] = {
                'code_name': r[1], 'preclose': r[2],
                'open': r[3], 'high': r[4], 'low': r[5], 'close': r[6],
                'turn': r[7], 'amount': r[8], 'isST': r[9], 'volume': r[10],
                'h1_o': r[11], 'h1_h': r[12], 'h1_l': r[13], 'h1_c': r[14],
            }
        if not day_dict:
            close_window.append((date, {}, {}, {}, {}))
            prev_extra_map = {}
            continue

        # 更新持仓
        for s in slots:
            if s['code'] and s['buy_date'] and s['buy_date'] < date:
                s['hold_days'] += 1
            if s['code'] and s['code'] in day_dict:
                pc = sv(day_dict[s['code']].get('preclose'))
                if pc > 0:
                    s['preclose'] = pc

        # --- 卖出逻辑 (基于日收盘价) ---
        daily_sells = []
        for s in slots:
            if not s['code'] or not s['buy_date'] or s['buy_date'] >= date:
                continue
            row = day_dict.get(s['code'])
            if row is None:
                continue
            bp = s['buy_price']
            sell_price = 0
            reason = ''

            # 跌停一字板不卖
            preclose = s['preclose']
            if preclose > 0:
                ld = calc_limit_down(preclose, s['code'])
                t_o = sv(row.get('open'))
                t_c = sv(row.get('close'))
                t_h = sv(row.get('high'))
                t_l = sv(row.get('low'))
                if (t_o > 0 and abs(t_o - t_c) < 0.001 and
                    abs(t_h - t_l) < 0.001 and t_c <= ld):
                    continue

            if sell_mode == 'h1_open':
                # T+1 hour1 open卖出
                if s['hold_days'] >= max_hold_days:
                    h1o = sv(row.get('h1_o'))
                    if h1o > 0:
                        sell_price = h1o
                        reason = f'T+1开盘({(h1o/bp-1)*100:+.1f}%)'

            elif sell_mode == 'day_close':
                # T+1 day close卖出
                if s['hold_days'] >= max_hold_days:
                    dc = sv(row.get('close'))
                    if dc > 0:
                        sell_price = dc
                        reason = f'T+1收盘({(dc/bp-1)*100:+.1f}%)'

            elif sell_mode == 'daily_check':
                # 基于日收盘价判断止损止盈
                dc = sv(row.get('close'))
                if dc <= 0:
                    continue
                pnl = (dc / bp - 1) * 100

                if sl_pct is not None and pnl <= -sl_pct:
                    sell_price = dc
                    reason = f'止损({pnl:+.1f}%)'
                elif tp_pct is not None and pnl >= tp_pct:
                    sell_price = dc
                    reason = f'止盈({pnl:+.1f}%)'
                elif s['hold_days'] >= max_hold_days:
                    sell_price = dc
                    reason = f'到期({s["hold_days"]}天,{pnl:+.1f}%)'

            if sell_price > 0:
                proceeds = s['shares'] * sell_price * (1 - COST_RATE)
                pnl_pct = (sell_price / bp - 1) * 100
                nav_now = get_nav()
                contrib = (sell_price - bp) * s['shares'] / nav_now * 100 if nav_now > 0 else 0
                trade = {
                    'slot_id': s['id'], 'code': s['code'], 'code_name': s['name'],
                    'buy_price': round(bp, 3), 'sell_price': round(sell_price, 3),
                    'shares': s['shares'], 'pnl_pct': round(pnl_pct, 3),
                    'contribution_pct': round(contrib, 3),
                    'buy_date': s['buy_date'], 'sell_date': date,
                    'hold_days': s['hold_days'], 'reason': reason,
                }
                sell_trades.append(trade)
                daily_sells.append(trade)
                cash += proceeds
                s['code'] = None
                s['name'] = ''
                s['buy_price'] = 0
                s['buy_date'] = None
                s['shares'] = 0
                s['hold_days'] = 0

        # --- 候选股筛选 ---
        candidates = []
        if len(close_window) >= 6:
            for code, t_row in day_dict.items():
                if t_row.get('isST'):
                    continue
                cn = t_row.get('code_name') or ''
                if 'ST' in cn.upper():
                    continue
                if code.startswith('bj.'):
                    continue
                t_open = sv(t_row.get('open'))
                t_preclose = sv(t_row.get('preclose'))
                if t_open <= 0 or t_preclose <= 0:
                    continue

                # 市值估算
                yd_extra = prev_extra_map.get(code)
                if yd_extra is None:
                    continue
                yd_turn, yd_amount = yd_extra[0], yd_extra[1]
                if yd_turn < MIN_TURNOVER or yd_amount <= 0:
                    continue
                market_cap = yd_amount * 100 / yd_turn / 1e8
                if market_cap < MIN_MARKET_CAP:
                    continue

                # close序列
                code_closes = []
                code_highs = []
                code_lows = []
                code_vols = []
                for item in close_window:
                    cm = item[1]
                    c = cm.get(code)
                    code_closes.append(c if (c is not None and c > 0) else None)
                    hlm = item[3]
                    hl = hlm.get(code)
                    if hl:
                        code_highs.append(hl[0])
                        code_lows.append(hl[1])
                    elif c and c > 0:
                        code_highs.append(c)
                        code_lows.append(c)
                    vm = item[4] if len(item) > 4 else {}
                    code_vols.append(vm.get(code, 0))

                if len(code_closes) < 6:
                    continue

                # MA5_yesterday
                last5 = [c for c in code_closes[-5:] if c is not None]
                if len(last5) < 5:
                    continue
                ma5_yd = sum(last5) / 5.0

                # MA5_T-2
                prev5 = [c for c in code_closes[-6:-1] if c is not None]
                if len(prev5) < 5:
                    continue
                ma5_t2 = sum(prev5) / 5.0

                t2_close = code_closes[-2]
                if t2_close is None:
                    continue
                yd_close = code_closes[-1]
                if yd_close is None or yd_close <= 0:
                    continue

                # 核心MA5突破条件
                if t2_close >= ma5_t2:
                    continue
                if yd_close <= ma5_yd:
                    continue
                if t_open <= ma5_yd:
                    continue

                # 近10日回撤
                recent_h = code_highs[-10:] if len(code_highs) >= 10 else code_highs
                recent_l = code_lows[-10:] if len(code_lows) >= 10 else code_lows
                if not recent_h or not recent_l:
                    continue
                max_high = max(recent_h)
                min_low = min(recent_l)
                if max_high <= 0:
                    continue
                recent_drop = (max_high - min_low) / max_high * 100
                if recent_drop < MIN_RECENT_DROP:
                    continue

                # 涨停开盘排除
                lu = calc_limit_up(t_preclose, code)
                if t_open >= lu:
                    continue

                # 一字板排除
                t_high = sv(t_row.get('high'))
                t_low = sv(t_row.get('low'))
                t_close = sv(t_row.get('close'))
                if (t_open > 0 and abs(t_open - t_high) < 0.001 and
                    abs(t_open - t_low) < 0.001 and abs(t_open - t_close) < 0.001):
                    continue

                # 缩量整理过滤
                if use_shrink:
                    # 前3日volume逐日递减
                    if len(code_vols) < 3:
                        continue
                    v3 = code_vols[-3]
                    v2 = code_vols[-2]
                    v1 = code_vols[-1]
                    if not (v3 > 0 and v2 > 0 and v1 > 0):
                        continue
                    if not (v3 > v2 > v1):
                        continue
                    # 微幅突破: open高于MA5_yesterday幅度在0-1%
                    gap_pct = (t_open / ma5_yd - 1) * 100
                    if gap_pct < 0 or gap_pct > 1.0:
                        continue

                candidates.append({
                    'code': code, 'name': cn,
                    'cap': market_cap, 'preclose': t_preclose,
                })

        candidates.sort(key=lambda x: -x['cap'])

        # --- 买入 (hour1 open) ---
        daily_buys = []
        if candidates:
            held = {s['code'] for s in slots if s['code']}
            ci = 0
            for s in slots:
                if s['code'] is not None:
                    continue
                while ci < len(candidates):
                    cand = candidates[ci]
                    ci += 1
                    code = cand['code']
                    if code in held:
                        continue
                    row = day_dict.get(code)
                    if row is None:
                        continue
                    h_o = sv(row.get('h1_o'))
                    h_h = sv(row.get('h1_h'))
                    h_l = sv(row.get('h1_l'))
                    h_c = sv(row.get('h1_c'))
                    if h_o <= 0:
                        continue
                    # 一字板
                    if abs(h_o - h_c) < 0.001 and abs(h_h - h_l) < 0.001 and h_o > 0:
                        continue
                    # 涨停开盘
                    pc = cand['preclose']
                    if pc > 0 and h_o >= calc_limit_up(pc, code):
                        continue
                    # 买入
                    nav = get_nav()
                    target = nav / N_SLOTS
                    buy_amt = min(target, cash)
                    shares = int(buy_amt / (h_o * (1 + COST_RATE)) / 100) * 100
                    if shares <= 0:
                        continue
                    cost = shares * h_o * (1 + COST_RATE)
                    s['code'] = code
                    s['name'] = cand['name']
                    s['buy_price'] = h_o
                    s['buy_date'] = date
                    s['shares'] = shares
                    s['hold_days'] = 0
                    s['preclose'] = pc
                    cash -= cost
                    held.add(code)
                    daily_buys.append({'slot_id': s['id'], 'code': code, 'name': cand['name'], 'price': h_o})
                    break

        # 日终NAV
        end_prices = {}
        for s in slots:
            if s['code']:
                row = day_dict.get(s['code'])
                if row:
                    p = sv(row.get('close'))
                    if p > 0:
                        end_prices[s['code']] = p
        nav = get_nav(end_prices)
        nav_history.append({'date': date, 'nav': nav})

        # 仓位日志
        if daily_buys or daily_sells or day_i % 20 == 0 or day_i == total_days - 1:
            ret_pct = (nav / INIT_CAPITAL - 1) * 100
            log_line = f"\n{date} | NAV:{nav:,.0f}({ret_pct:+.2f}%) | Cash:{cash:,.0f}\n"
            for s in slots:
                sold = [t for t in daily_sells if t['slot_id'] == s['id']]
                bought = [t for t in daily_buys if t['slot_id'] == s['id']]
                if s['code'] is None:
                    if sold:
                        t = sold[0]
                        log_line += (f"  S{s['id']}: 卖 {t['code']} {t['code_name']} "
                                     f"{t['buy_price']:.2f}->{t['sell_price']:.2f} "
                                     f"单仓{t['pnl_pct']:+.2f}% 贡献{t['contribution_pct']:+.2f}% "
                                     f"[{t['reason']}]\n")
                    else:
                        log_line += f"  S{s['id']}: 空仓\n"
                else:
                    cp = end_prices.get(s['code'], s['buy_price'])
                    ur = (cp / s['buy_price'] - 1) * 100 if s['buy_price'] > 0 else 0
                    tag = '[新入]' if bought else f'[持D{s["hold_days"]}]'
                    log_line += (f"  S{s['id']}: {tag} {s['code']} {s['name']} "
                                 f"{s['buy_price']:.2f}->{cp:.2f} 浮盈{ur:+.2f}%\n")
            pos_log.append(log_line)

        # 更新close窗口
        today_close_map = {}
        today_extra_map = {}
        today_hl_map = {}
        today_vol_map = {}
        for code, row in day_dict.items():
            c = row.get('close')
            if c is not None:
                today_close_map[code] = float(c)
            t = row.get('turn')
            a = row.get('amount')
            if t and a:
                today_extra_map[code] = (float(t), float(a))
            h = row.get('high')
            l = row.get('low')
            if h and l:
                today_hl_map[code] = (float(h), float(l))
            v = row.get('volume')
            if v:
                today_vol_map[code] = float(v)
        close_window.append((date, today_close_map, today_extra_map, today_hl_map, today_vol_map))
        prev_extra_map = today_extra_map

    return sell_trades, nav_history, pos_log


def calc_stats(sell_trades, nav_history):
    """计算统计指标"""
    if not nav_history:
        return {}
    final_nav = nav_history[-1]['nav']
    total_return = (final_nav / INIT_CAPITAL - 1) * 100
    first_d = datetime.strptime(nav_history[0]['date'], '%Y-%m-%d')
    last_d = datetime.strptime(nav_history[-1]['date'], '%Y-%m-%d')
    years = (last_d - first_d).days / 365.25
    cagr = ((final_nav / INIT_CAPITAL) ** (1 / years) - 1) * 100 if years > 0 else 0

    peak = INIT_CAPITAL
    max_dd = 0
    for item in nav_history:
        n = item['nav']
        if n > peak:
            peak = n
        dd = (peak - n) / peak * 100
        if dd > max_dd:
            max_dd = dd

    n_trades = len(sell_trades)
    if n_trades > 0:
        wins = sum(1 for t in sell_trades if t['pnl_pct'] > 0)
        win_rate = wins / n_trades * 100
        avg_pnl = sum(t['pnl_pct'] for t in sell_trades) / n_trades
    else:
        wins = 0
        win_rate = avg_pnl = 0

    # 逐年
    yearly = defaultdict(list)
    for t in sell_trades:
        yearly[t['sell_date'][:4]].append(t['pnl_pct'])

    # 按原因
    reason_stats = defaultdict(list)
    for t in sell_trades:
        r = t.get('reason', '').split('(')[0]
        reason_stats[r].append(t['pnl_pct'])

    # Sharpe
    daily_rets = []
    for i in range(1, len(nav_history)):
        pn = nav_history[i - 1]['nav']
        cn = nav_history[i]['nav']
        if pn > 0:
            daily_rets.append(cn / pn - 1)
    if len(daily_rets) > 1:
        avg_dr = statistics.mean(daily_rets)
        std_dr = statistics.stdev(daily_rets)
        sharpe = avg_dr / std_dr * (252 ** 0.5) if std_dr > 0 else 0
    else:
        sharpe = 0

    return {
        'final_nav': final_nav, 'total_return': total_return,
        'cagr': cagr, 'max_dd': max_dd, 'sharpe': sharpe,
        'n_trades': n_trades, 'wins': wins, 'win_rate': win_rate,
        'avg_pnl': avg_pnl, 'yearly': yearly, 'reason_stats': reason_stats,
    }


def main():
    t_start = time.time()
    os.makedirs(LOG_DIR, exist_ok=True)

    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()

    # 获取交易日
    cur.execute("SELECT DISTINCT date FROM stock_kline ORDER BY date")
    all_days = [r[0] for r in cur.fetchall()]
    all_days_idx = {d: i for i, d in enumerate(all_days)}

    start_idx = end_idx = None
    for i, d in enumerate(all_days):
        if start_idx is None and d >= START_DATE:
            start_idx = i
        if d <= END_DATE:
            end_idx = i
    if start_idx is None or end_idx is None:
        print("日期范围错误")
        return

    trading_dates = all_days[start_idx:end_idx + 1]
    print(f"交易日: {len(trading_dates)} ({trading_dates[0]} ~ {trading_dates[-1]})")
    print(f"初始资金: {INIT_CAPITAL:,.0f} | 仓位数: {N_SLOTS}")
    print(f"{'=' * 70}")

    # 预加载warmup数据（start前11天）
    warmup_data = []
    warmup_start = max(0, start_idx - 11)
    for wi in range(warmup_start, start_idx):
        d = all_days[wi]
        cur.execute("""SELECT code, close, turn, amount, high, low, volume
                       FROM stock_kline WHERE date = ? AND close IS NOT NULL""", (d,))
        close_map = {}
        extra_map = {}
        hl_map = {}
        vol_map = {}
        for r in cur.fetchall():
            if r[1]:
                close_map[r[0]] = float(r[1])
            if r[2] and r[3]:
                extra_map[r[0]] = (float(r[2]), float(r[3]))
            if r[4] and r[5]:
                hl_map[r[0]] = (float(r[4]), float(r[5]))
            if r[6]:
                vol_map[r[0]] = float(r[6])
        warmup_data.append((d, close_map, extra_map, hl_map, vol_map))
    print(f"预热数据: {len(warmup_data)}天")

    # 运行所有方案
    results = {}
    best_plan = None
    best_cagr = -999

    for plan_name, sl_pct, tp_pct, max_hold, sell_mode, use_shrink in PLANS:
        print(f"\n--- 方案 {plan_name}: SL={sl_pct}, TP={tp_pct}, "
              f"持{max_hold}天, {sell_mode}, 缩量={use_shrink} ---")

        trades, nav_hist, pos_log = run_backtest(
            plan_name, sl_pct, tp_pct, max_hold, sell_mode, use_shrink,
            trading_dates, all_days, all_days_idx, conn, warmup_data
        )
        stats = calc_stats(trades, nav_hist)
        stats['trades'] = trades
        stats['nav_history'] = nav_hist
        stats['pos_log'] = pos_log
        results[plan_name] = stats

        cagr = stats.get('cagr', 0)
        print(f"  总收益:{stats['total_return']:+.1f}% | CAGR:{cagr:+.2f}% | "
              f"MaxDD:{stats['max_dd']:.1f}% | 笔数:{stats['n_trades']} | "
              f"胜率:{stats['win_rate']:.1f}% | 均收益:{stats['avg_pnl']:+.3f}%")

        if cagr > best_cagr:
            best_cagr = cagr
            best_plan = plan_name

    conn.close()
    elapsed = time.time() - t_start
    print(f"\n{'=' * 70}")
    print(f"回测总耗时: {elapsed:.1f}秒 ({elapsed / 60:.1f}分钟)")
    print(f"最优方案: {best_plan} (CAGR={best_cagr:+.2f}%)")

    # === 输出汇总 ===
    lines = []
    lines.append(f"{'=' * 80}")
    lines.append(f"MA5突破大盘股(>700亿) V2 - 多方案对比汇总")
    lines.append(f"{'=' * 80}")
    lines.append(f"区间: {START_DATE} ~ {END_DATE} | 初始资金: {INIT_CAPITAL:,.0f} | 仓位: {N_SLOTS}")
    lines.append(f"公共条件: 市值>{MIN_MARKET_CAP}亿, 换手率>{MIN_TURNOVER}%, "
                 f"近10日回撤>{MIN_RECENT_DROP}%")
    lines.append(f"修复: 止损止盈基于日收盘价判断(非日内hour级)")
    lines.append(f"")
    lines.append(f"{'方案':<6}{'止损':<7}{'止盈':<7}{'持仓天':<7}{'卖出方式':<14}"
                 f"{'缩量':<5}{'总收益%':<10}{'CAGR%':<9}{'MaxDD%':<9}"
                 f"{'笔数':<7}{'胜率%':<8}{'均收益%':<10}{'Sharpe':<8}")
    lines.append('-' * 110)

    for plan_name, sl_pct, tp_pct, max_hold, sell_mode, use_shrink in PLANS:
        s = results[plan_name]
        sl_str = f"-{sl_pct}%" if sl_pct else "无"
        tp_str = f"+{tp_pct}%" if tp_pct else "无"
        shrink_str = "是" if use_shrink else "否"
        lines.append(
            f"{plan_name:<6}{sl_str:<7}{tp_str:<7}{max_hold:<7}{sell_mode:<14}"
            f"{shrink_str:<5}{s['total_return']:+8.1f}  {s['cagr']:+7.2f}  "
            f"{s['max_dd']:7.1f}  {s['n_trades']:<7}{s['win_rate']:6.1f}  "
            f"{s['avg_pnl']:+8.3f}  {s['sharpe']:6.3f}")

    lines.append(f"\n{'=' * 80}")
    lines.append(f"最优方案: {best_plan} (CAGR={best_cagr:+.2f}%)")
    lines.append(f"{'=' * 80}")

    # 最优方案逐年明细
    best_stats = results[best_plan]
    lines.append(f"\n--- 最优方案 {best_plan} 逐年表现 ---")
    lines.append(f"{'年':<6}{'笔数':<8}{'胜率%':<10}{'均收益%':<12}{'累计%':<10}")
    for yr in sorted(best_stats['yearly'].keys()):
        rets = best_stats['yearly'][yr]
        wr = sum(1 for r in rets if r > 0) / len(rets) * 100 if rets else 0
        avg = sum(rets) / len(rets) if rets else 0
        tot = sum(rets)
        lines.append(f"{yr:<6}{len(rets):<8}{wr:.1f}%{'':>4}{avg:+.3f}%{'':>4}{tot:+.1f}%")

    lines.append(f"\n--- 最优方案 {best_plan} 按卖出原因 ---")
    for reason in sorted(best_stats['reason_stats'].keys()):
        rets = best_stats['reason_stats'][reason]
        avg = sum(rets) / len(rets) if rets else 0
        wr = sum(1 for r in rets if r > 0) / len(rets) * 100 if rets else 0
        lines.append(f"  {reason:<14} {len(rets):>5}笔  均{avg:+.3f}%  胜率{wr:.1f}%")

    lines.append(f"\n{'=' * 80}")

    summary_text = '\n'.join(lines)
    with open(SUMMARY_LOG, 'w', encoding='utf-8') as f:
        f.write(summary_text)
    print(f"\n汇总文件: {SUMMARY_LOG}")
    print(summary_text)

    # 最优方案交易明细
    best_trades = best_stats['trades']
    with open(BEST_TRADES_JSON, 'w', encoding='utf-8') as f:
        json.dump(best_trades, f, ensure_ascii=False, indent=2)
    print(f"最优交易明细: {BEST_TRADES_JSON} ({len(best_trades)}笔)")

    # 最优方案仓位日志
    with open(POSITION_LOG, 'w', encoding='utf-8') as f:
        plan_cfg = [p for p in PLANS if p[0] == best_plan][0]
        f.write(f"MA5突破V2 仓位日志 - 方案{best_plan}\n{'=' * 60}\n")
        f.write(f"参数: SL={plan_cfg[1]}, TP={plan_cfg[2]}, "
                f"持{plan_cfg[3]}天, {plan_cfg[4]}, 缩量={plan_cfg[5]}\n")
        f.write(f"区间: {START_DATE}~{END_DATE} | {N_SLOTS}仓\n{'=' * 60}\n")
        for line in best_stats['pos_log']:
            f.write(line)
    print(f"仓位日志: {POSITION_LOG}")


if __name__ == '__main__':
    main()
