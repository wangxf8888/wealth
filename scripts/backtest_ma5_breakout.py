#!/usr/bin/env python3
"""MA5突破大盘股(>700亿)策略 - 完整多仓位回测

策略逻辑（T+0合规）：
  - 候选条件：流通市值>700亿 + yesterday_close < MA5 + today_open > MA5
  - 买入：Hour1 open价格
  - 卖出：止盈+5% | 止损-3% | 持仓3天hour4强平 | 跌停不卖
  - 仓位：5个slot等权分配

用法: python3 scripts/backtest_ma5_breakout.py
"""
import sys
import os
import json
import math
import time
import sqlite3
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
MIN_TURNOVER = 1.0     # % (研究验证用的是1.0)
MIN_RECENT_DROP = 5.0  # 近10日回撤最少% (关键过滤条件)
TP_PCT = 5.0           # 止盈%
SL_PCT = 3.0           # 止损%
MAX_HOLD_DAYS = 3      # 最大持仓天数

LOG_DIR = '/home/AIWealth/scripts/logs'
TRADES_JSON = f'{LOG_DIR}/ma5_breakout_backtest_trades.json'
POSITION_LOG = f'{LOG_DIR}/ma5_breakout_backtest_positions.log'
SUMMARY_LOG = f'{LOG_DIR}/ma5_breakout_backtest_summary.log'
# ===================================


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


def load_day_rows(conn, date):
    """加载一天的数据，返回 {code: dict}"""
    cur = conn.cursor()
    cur.execute("""
        SELECT code, code_name, preclose, open, high, low, close,
               turn, amount, isST,
               hour1_open, hour1_high, hour1_low, hour1_close,
               hour2_open, hour2_high, hour2_low, hour2_close,
               hour3_open, hour3_high, hour3_low, hour3_close,
               hour4_open, hour4_high, hour4_low, hour4_close
        FROM stock_kline WHERE date = ?
    """, (date,))
    result = {}
    for r in cur.fetchall():
        result[r[0]] = {
            'code_name': r[1], 'preclose': r[2],
            'open': r[3], 'high': r[4], 'low': r[5], 'close': r[6],
            'turn': r[7], 'amount': r[8], 'isST': r[9],
            'h1_o': r[10], 'h1_h': r[11], 'h1_l': r[12], 'h1_c': r[13],
            'h2_o': r[14], 'h2_h': r[15], 'h2_l': r[16], 'h2_c': r[17],
            'h3_o': r[18], 'h3_h': r[19], 'h3_l': r[20], 'h3_c': r[21],
            'h4_o': r[22], 'h4_h': r[23], 'h4_l': r[24], 'h4_c': r[25],
        }
    return result


def main():
    t_start = time.time()
    os.makedirs(LOG_DIR, exist_ok=True)

    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()

    # 获取所有交易日
    cur.execute("SELECT DISTINCT date FROM stock_kline ORDER BY date")
    all_days = [r[0] for r in cur.fetchall()]
    all_days_idx = {d: i for i, d in enumerate(all_days)}

    # 找到回测起始日在all_days中的索引
    start_idx = None
    end_idx = None
    for i, d in enumerate(all_days):
        if start_idx is None and d >= START_DATE:
            start_idx = i
        if d <= END_DATE:
            end_idx = i

    if start_idx is None or end_idx is None:
        print("日期范围错误")
        return

    trading_dates = all_days[start_idx:end_idx+1]
    total_days = len(trading_dates)
    print(f"交易日总数: {total_days} ({trading_dates[0]} ~ {trading_dates[-1]})")
    print(f"初始资金: {INIT_CAPITAL:,.0f} | 仓位数: {N_SLOTS}")
    print(f"参数: 市值>{MIN_MARKET_CAP}亿, TP+{TP_PCT}%, SL-{SL_PCT}%, 最大{MAX_HOLD_DAYS}天")
    print(f"{'='*70}")

    # 滑动窗口：保留最近11天的数据（需要10天计算近期回撤 + 6天计算MA5）
    close_window = deque(maxlen=11)  # [(date, {code: close}, {code: (turn,amount)}, {code: (high,low)}), ...]

    # 预加载start之前11天的数据
    warmup_start = max(0, start_idx - 11)
    print("预加载热身数据...")
    for wi in range(warmup_start, start_idx):
        d = all_days[wi]
        cur.execute("SELECT code, close, turn, amount, high, low FROM stock_kline WHERE date = ? AND close IS NOT NULL", (d,))
        close_map = {}
        extra_map = {}
        hl_map = {}  # {code: (high, low)}
        for r in cur.fetchall():
            if r[1]:
                close_map[r[0]] = float(r[1])
            if r[2] and r[3]:
                extra_map[r[0]] = (float(r[2]), float(r[3]))
            if r[4] and r[5]:
                hl_map[r[0]] = (float(r[4]), float(r[5]))
        close_window.append((d, close_map, extra_map, hl_map))
    print(f"热身完成: {len(close_window)}天数据")

    # 仓位状态
    cash = INIT_CAPITAL
    slots = [{'id': i, 'code': None, 'name': '', 'buy_price': 0,
              'buy_date': None, 'shares': 0, 'hold_days': 0, 'preclose': 0}
             for i in range(N_SLOTS)]
    all_trades = []
    nav_history = []
    pos_log = []

    def get_nav(prices=None):
        t = cash
        for s in slots:
            if s['code']:
                p = s['buy_price']
                if prices and s['code'] in prices:
                    p = prices[s['code']]
                t += s['shares'] * p
        return t

    trade_count = 0
    prev_close_map = close_window[-1][1] if close_window else {}
    prev_extra_map = close_window[-1][2] if close_window else {}

    for day_i, date in enumerate(trading_dates):
        # 加载今日全量数据
        day_dict = load_day_rows(conn, date)
        if not day_dict:
            # 保存close到窗口
            close_window.append((date, {}))
            prev_close_map = {}
            continue

        # 更新持仓hold_days和preclose
        for s in slots:
            if s['code'] and s['buy_date'] and s['buy_date'] < date:
                s['hold_days'] += 1
            if s['code'] and s['code'] in day_dict:
                pc = sv(day_dict[s['code']].get('preclose'))
                if pc > 0:
                    s['preclose'] = pc

        # --- 候选股筛选 (已验证的MA5突破逻辑) ---
        # 条件: T-2 close < MA5_T-2, yesterday close > MA5_yesterday, today open > MA5_yesterday
        # 额外: 近10日回撤>5%, 换手率>1%
        candidates = []
        if len(close_window) >= 6:
            for code, t_row in day_dict.items():
                # 基本过滤
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

                # 市值估算（用昨日amount/turn，T+0合规）
                yd_extra = prev_extra_map.get(code)
                if yd_extra is None:
                    continue
                yd_turn, yd_amount = yd_extra
                if yd_turn < MIN_TURNOVER or yd_amount <= 0:
                    continue
                market_cap = yd_amount * 100 / yd_turn / 1e8
                if market_cap < MIN_MARKET_CAP:
                    continue

                # 收集该股在窗口中的close序列和high/low
                code_closes = []
                code_highs = []
                code_lows = []
                for _, cm, _, hlm in close_window:
                    c = cm.get(code)
                    code_closes.append(c if (c is not None and c > 0) else None)
                    hl = hlm.get(code)
                    if hl:
                        code_highs.append(hl[0])
                        code_lows.append(hl[1])
                    elif c and c > 0:
                        code_highs.append(c)
                        code_lows.append(c)

                # 需要至少最后6个有效close
                if len(code_closes) < 6:
                    continue

                # MA5_yesterday = mean(close[-5:])
                last5 = [c for c in code_closes[-5:] if c is not None]
                if len(last5) < 5:
                    continue
                ma5_yd = sum(last5) / 5.0

                # MA5_T-2 = mean(close[-6:-1])
                prev5 = [c for c in code_closes[-6:-1] if c is not None]
                if len(prev5) < 5:
                    continue
                ma5_t2 = sum(prev5) / 5.0

                # T-2 close
                t2_close = code_closes[-2]
                if t2_close is None:
                    continue

                # yesterday close
                yd_close = code_closes[-1]
                if yd_close is None or yd_close <= 0:
                    continue

                # 核心条件1: T-2 close < MA5_T-2
                if t2_close >= ma5_t2:
                    continue
                # 核心条件2: yesterday close > MA5_yesterday
                if yd_close <= ma5_yd:
                    continue
                # 核心条件3: today_open > MA5_yesterday
                if t_open <= ma5_yd:
                    continue

                # 近10日回撤检查（从窗口的high/low计算）
                # 取最近10天的high和low
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

                # 昨日涨停排除（避免追高）
                if yd_close and t_preclose > 0:
                    yd_gain = (yd_close / t_preclose - 1) if t_preclose else 0
                    # 不排除昨日涨停，因为Ma5突破本身就是走强信号

                # 一字板排除(日级)
                t_high = sv(t_row.get('high'))
                t_low = sv(t_row.get('low'))
                t_close = sv(t_row.get('close'))
                if t_open > 0 and abs(t_open-t_high) < 0.001 and abs(t_open-t_low) < 0.001 and abs(t_open-t_close) < 0.001:
                    continue

                candidates.append({
                    'code': code, 'name': cn,
                    'cap': market_cap, 'preclose': t_preclose,
                })

        # 按市值降序
        candidates.sort(key=lambda x: -x['cap'])

        daily_sells = []
        daily_buys = []

        for hour in [1, 2, 3, 4]:
            pfx = f'h{hour}_'

            # --- 卖出检查 ---
            for s in slots:
                if not s['code'] or not s['buy_date'] or s['buy_date'] >= date:
                    continue
                row = day_dict.get(s['code'])
                if row is None:
                    continue
                h_o = sv(row.get(pfx+'o'))
                h_h = sv(row.get(pfx+'h'))
                h_l = sv(row.get(pfx+'l'))
                h_c = sv(row.get(pfx+'c'))
                if h_o <= 0 or h_c <= 0:
                    continue

                # 跌停一字板不卖
                preclose = s['preclose']
                if preclose > 0:
                    ld = calc_limit_down(preclose, s['code'])
                    if (abs(h_o-h_c) < 0.001 and abs(h_h-h_l) < 0.001 and h_c <= ld):
                        continue

                bp = s['buy_price']
                tp_p = bp * (1 + TP_PCT / 100)
                sl_p = bp * (1 - SL_PCT / 100)
                sell_price = 0
                reason = ''

                # 止损优先
                if h_l <= sl_p:
                    sell_price = h_o if h_o <= sl_p else sl_p
                    reason = f'止损({(sell_price/bp-1)*100:+.1f}%)'
                elif h_h >= tp_p:
                    sell_price = h_o if h_o >= tp_p else tp_p
                    reason = f'止盈({(sell_price/bp-1)*100:+.1f}%)'
                elif s['hold_days'] >= MAX_HOLD_DAYS and hour == 4:
                    sell_price = h_c
                    reason = f'到期({s["hold_days"]}天,{(h_c/bp-1)*100:+.1f}%)'

                if sell_price > 0:
                    proceeds = s['shares'] * sell_price * (1 - COST_RATE)
                    pnl_pct = (sell_price / bp - 1) * 100
                    nav_now = get_nav()
                    contrib = (sell_price - bp) * s['shares'] / nav_now * 100 if nav_now > 0 else 0

                    trade = {
                        'type': 'sell', 'slot_id': s['id'],
                        'code': s['code'], 'code_name': s['name'],
                        'buy_price': round(bp, 3), 'sell_price': round(sell_price, 3),
                        'shares': s['shares'], 'pnl_pct': round(pnl_pct, 3),
                        'contribution_pct': round(contrib, 3),
                        'buy_date': s['buy_date'], 'sell_date': date,
                        'sell_hour': hour, 'hold_days': s['hold_days'],
                        'reason': reason,
                    }
                    all_trades.append(trade)
                    daily_sells.append(trade)
                    cash += proceeds
                    s['code'] = None
                    s['name'] = ''
                    s['buy_price'] = 0
                    s['buy_date'] = None
                    s['shares'] = 0
                    s['hold_days'] = 0
                    trade_count += 1

            # --- 买入 (hour1) ---
            if hour == 1 and candidates:
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
                        if abs(h_o-h_c) < 0.001 and abs(h_h-h_l) < 0.001 and h_o > 0:
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
                        trade = {
                            'type': 'buy', 'slot_id': s['id'],
                            'code': code, 'code_name': cand['name'],
                            'price': round(h_o, 3), 'shares': shares,
                            'date': date, 'market_cap': round(cand['cap'], 0),
                        }
                        all_trades.append(trade)
                        daily_buys.append(trade)
                        trade_count += 1
                        break

        # 日终NAV
        end_prices = {}
        for s in slots:
            if s['code']:
                row = day_dict.get(s['code'])
                if row:
                    p = sv(row.get('h4_c'))
                    if p > 0:
                        end_prices[s['code']] = p
        nav = get_nav(end_prices)
        nav_history.append({'date': date, 'nav': nav})

        # 仓位日志（每10天记录一次详细，其余简略）
        ret_pct = (nav / INIT_CAPITAL - 1) * 100
        if daily_buys or daily_sells or day_i % 10 == 0 or day_i == total_days-1:
            log_line = f"\n{date} | NAV:{nav:,.0f}({ret_pct:+.2f}%) | Cash:{cash:,.0f}\n"
            for s in slots:
                sold = [t for t in daily_sells if t['slot_id'] == s['id']]
                bought = [t for t in daily_buys if t['slot_id'] == s['id']]
                if s['code'] is None:
                    if sold:
                        t = sold[0]
                        log_line += (f"  S{s['id']}: 卖 {t['code']} {t['code_name']} "
                                     f"{t['buy_price']:.2f}→{t['sell_price']:.2f} "
                                     f"{t['pnl_pct']:+.2f}% [{t['reason']}]\n")
                    else:
                        log_line += f"  S{s['id']}: 空\n"
                else:
                    cp = end_prices.get(s['code'], s['buy_price'])
                    ur = (cp/s['buy_price']-1)*100 if s['buy_price'] > 0 else 0
                    tag = '[新]' if bought else f'[D{s["hold_days"]}]'
                    log_line += (f"  S{s['id']}: {tag} {s['code']} {s['name']} "
                                 f"{s['buy_price']:.2f}→{cp:.2f}({ur:+.2f}%)\n")
            pos_log.append(log_line)

        # 更新close窗口
        today_close_map = {}
        today_extra_map = {}
        today_hl_map = {}
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
        close_window.append((date, today_close_map, today_extra_map, today_hl_map))
        prev_close_map = today_close_map
        prev_extra_map = today_extra_map

        # 进度
        if (day_i+1) % 200 == 0 or day_i == total_days-1:
            print(f"  [{day_i+1}/{total_days}] {date} | NAV:{nav:,.0f} | "
                  f"{ret_pct:+.2f}% | 笔数:{trade_count}")

    conn.close()
    elapsed = time.time() - t_start
    print(f"\n回测耗时: {elapsed:.1f}秒 ({elapsed/60:.1f}分钟)")

    # ========== 输出 ==========
    sell_trades = [t for t in all_trades if t['type'] == 'sell']

    with open(TRADES_JSON, 'w', encoding='utf-8') as f:
        json.dump(sell_trades, f, ensure_ascii=False, indent=2)
    print(f"交易明细: {TRADES_JSON} ({len(sell_trades)}笔)")

    with open(POSITION_LOG, 'w', encoding='utf-8') as f:
        f.write(f"MA5突破大盘股(>700亿) 仓位日志\n{'='*60}\n")
        f.write(f"区间:{START_DATE}~{END_DATE} | TP+{TP_PCT}% SL-{SL_PCT}% | "
                f"最大{MAX_HOLD_DAYS}天 | {N_SLOTS}仓\n")
        for line in pos_log:
            f.write(line)
    print(f"仓位日志: {POSITION_LOG}")

    output_summary(sell_trades, nav_history)


def output_summary(sell_trades, nav_history):
    if not nav_history:
        print("无数据")
        return

    final_nav = nav_history[-1]['nav']
    total_return = (final_nav / INIT_CAPITAL - 1) * 100
    first_d = datetime.strptime(nav_history[0]['date'], '%Y-%m-%d')
    last_d = datetime.strptime(nav_history[-1]['date'], '%Y-%m-%d')
    years = (last_d - first_d).days / 365.25
    cagr = ((final_nav / INIT_CAPITAL) ** (1/years) - 1) * 100 if years > 0 else 0

    peak = INIT_CAPITAL
    max_dd = 0
    for item in nav_history:
        n = item['nav']
        if n > peak:
            peak = n
        dd = (peak - n) / peak * 100
        if dd > max_dd:
            max_dd = dd

    daily_rets = []
    for i in range(1, len(nav_history)):
        pn = nav_history[i-1]['nav']
        cn = nav_history[i]['nav']
        if pn > 0:
            daily_rets.append((cn/pn - 1) * 100)

    if daily_rets:
        import statistics
        avg_daily = statistics.mean(daily_rets)
        std_daily = statistics.stdev(daily_rets) if len(daily_rets) > 1 else 1
        sharpe = avg_daily / std_daily * (252**0.5) if std_daily > 0 else 0
    else:
        avg_daily = sharpe = 0

    n_trades = len(sell_trades)
    if n_trades > 0:
        wins = sum(1 for t in sell_trades if t['pnl_pct'] > 0)
        win_rate = wins / n_trades * 100
        avg_pnl = sum(t['pnl_pct'] for t in sell_trades) / n_trades
        wl = [t['pnl_pct'] for t in sell_trades if t['pnl_pct'] > 0]
        ll = [t['pnl_pct'] for t in sell_trades if t['pnl_pct'] <= 0]
        avg_win = sum(wl)/len(wl) if wl else 0
        avg_loss = sum(ll)/len(ll) if ll else 0
    else:
        wins = 0; win_rate = avg_pnl = avg_win = avg_loss = 0

    yearly = defaultdict(list)
    for t in sell_trades:
        yearly[t['sell_date'][:4]].append(t['pnl_pct'])

    reason_stats = defaultdict(list)
    for t in sell_trades:
        r = t.get('reason', '').split('(')[0]
        reason_stats[r].append(t['pnl_pct'])

    lines = []
    lines.append(f"{'='*70}")
    lines.append(f"MA5突破大盘股(>700亿) - 回测汇总")
    lines.append(f"{'='*70}")
    lines.append(f"区间: {START_DATE} ~ {END_DATE}")
    lines.append(f"初始: {INIT_CAPITAL:,.0f} → 最终: {final_nav:,.0f}")
    lines.append(f"")
    lines.append(f"--- 收益 ---")
    lines.append(f"总收益:   {total_return:+.2f}%")
    lines.append(f"年化CAGR: {cagr:+.2f}%")
    lines.append(f"日均收益: {avg_daily:+.4f}%")
    lines.append(f"夏普比率: {sharpe:.3f}")
    lines.append(f"最大回撤: {max_dd:.2f}%")
    lines.append(f"")
    lines.append(f"--- 交易 ---")
    lines.append(f"总笔数:   {n_trades}")
    lines.append(f"胜率:     {win_rate:.1f}% ({wins}/{n_trades})")
    lines.append(f"均收益:   {avg_pnl:+.3f}%")
    lines.append(f"均盈利:   {avg_win:+.3f}%")
    lines.append(f"均亏损:   {avg_loss:+.3f}%")
    if avg_loss != 0:
        lines.append(f"盈亏比:   {abs(avg_win/avg_loss):.2f}")
    lines.append(f"")
    lines.append(f"--- 逐年 ---")
    lines.append(f"{'年':<6}{'笔数':<8}{'胜率':<10}{'均收益':<12}{'累计':<10}")
    for yr in sorted(yearly.keys()):
        rets = yearly[yr]
        wr = sum(1 for r in rets if r > 0) / len(rets) * 100
        avg = sum(rets) / len(rets)
        tot = sum(rets)
        lines.append(f"{yr:<6}{len(rets):<8}{wr:.1f}%{'':>4}{avg:+.3f}%{'':>4}{tot:+.1f}%")
    lines.append(f"")
    lines.append(f"--- 按卖出原因 ---")
    for reason in sorted(reason_stats.keys()):
        rets = reason_stats[reason]
        avg = sum(rets)/len(rets)
        wr = sum(1 for r in rets if r > 0) / len(rets) * 100
        lines.append(f"  {reason:<10} {len(rets):>5}笔  均{avg:+.3f}%  胜率{wr:.1f}%")
    lines.append(f"{'='*70}")

    with open(SUMMARY_LOG, 'w', encoding='utf-8') as f:
        f.write('\n'.join(lines))
    print(f"统计汇总: {SUMMARY_LOG}\n")
    print('\n'.join(lines))


if __name__ == '__main__':
    main()
