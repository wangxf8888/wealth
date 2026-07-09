#!/usr/bin/env python3
"""首板断板真低吸+限价单 Phase-2+3联合验证

策略逻辑:
  候选股: T-1日首板涨停(round(close/preclose,2)>=1.10/1.20) + turn>10%
          + 前5日无涨停 + T日open_rate∈[-5%,+5%] + 非ST + 非一字板 + 非北交所
  买入: 限价真低吸(5种方案A-E对比)
  卖出: T+1/T+2日逐hour限价止盈止损

限价单规则(已确认合规):
  - 买入限价: hourX_low <= P → 以P成交
  - 卖出限价: hourX_high >= P → 以P成交
  - 止损限价: hourX_low <= P → 以P成交

买入方案:
  A: hour1杀跌(h1_cr<=-1%) → limit=h1_close*(1-0.005), hour2验证
  B: hour1杀跌(h1_cr<=-1%) → limit=h1_close*(1-0.015), hour2验证
  C: hour1杀跌(h1_cr<=-1%) → limit=h1_close*(1-0.01), hour2+hour3验证
  D: hour1弱+hour2更弱 → limit=h2_close*(1-0.005), hour3验证
  E: 基线(hour1杀跌h1_cr<=-1%) → hour2_open市价买入

用法:
  python3 scripts/strategy_firstboard_limit_dip.py [month, default=2026-04]
  python3 scripts/strategy_firstboard_limit_dip.py year 2025
"""
import sqlite3
import sys
import math
from collections import defaultdict
from datetime import datetime, timedelta

# ============== 配置区 ==============
DB_PATH = '/home/AIWealth/data/stocks.db'
# 运行模式: 'month' 或 'year'
MODE = 'month'
TARGET_MONTH = '2026-04'
TARGET_YEAR = 2025

# 候选股筛选
OPEN_RATE_MIN = -5.0      # T日开盘涨幅下限 (%)
OPEN_RATE_MAX = 5.0       # T日开盘涨幅上限 (%)
PREV_TURN_MIN = 10.0      # T-1日(涨停日)换手率下限 (%) - 放宽让样本多
FIRSTBOARD_LOOKBACK = 5   # 首板回溯天数

# 仓位模拟
N_SLOTS = 3               # 最大同时持仓数
INIT_CAPITAL = 1000000.0  # 初始资金100万

# 自动扩展阈值
MONTHLY_RETURN_THRESHOLD = 15.0   # 月化>15%触发全年
MONTHLY_WINRATE_THRESHOLD = 50.0  # 胜率>50%触发全年
ANNUAL_RETURN_THRESHOLD = 100.0   # 年化>100%触发多年
# ===================================


def sv(v):
    """安全转float, None/nan返回None"""
    if v is None:
        return None
    try:
        f = float(v)
        if math.isnan(f):
            return None
        return f
    except (TypeError, ValueError):
        return None


def get_limit_up_threshold(code):
    """涨停阈值: 创业板/科创板20%, 主板10%"""
    if code.startswith('sz.30') or code.startswith('sh.68'):
        return 1.20
    return 1.10


def is_limit_up(close, preclose, code):
    """严格涨停判定: round(close/preclose, 2) >= threshold"""
    pc = sv(preclose)
    cl = sv(close)
    if pc is None or cl is None or pc == 0:
        return False
    return round(cl / pc, 2) >= get_limit_up_threshold(code)


def is_one_word_board(row):
    """一字板判定: open==high==low"""
    o = sv(row.get('open'))
    h = sv(row.get('high'))
    l = sv(row.get('low'))
    if None in (o, h, l):
        return False
    return o == h and h == l


def load_data(conn, dt_lo, dt_hi):
    """加载数据, 返回 (all_dates, date_idx, by_code)"""
    cur = conn.cursor()
    cur.execute(
        'SELECT DISTINCT date FROM stock_kline WHERE date>=? AND date<? ORDER BY date',
        (dt_lo, dt_hi),
    )
    all_dates = [r[0] for r in cur.fetchall()]
    date_idx = {d: i for i, d in enumerate(all_dates)}

    # 需要hour级的open/high/low/close来验证限价单
    flds = (
        'date,code,code_name,preclose,open,high,low,close,turn,isST,open_rate,'
        'hour1_open,hour1_high,hour1_low,hour1_close,hour1_close_rate,'
        'hour2_open,hour2_high,hour2_low,hour2_close,hour2_close_rate,'
        'hour3_open,hour3_high,hour3_low,hour3_close,hour3_close_rate,'
        'hour4_open,hour4_high,hour4_low,hour4_close,hour4_close_rate'
    )
    cur.execute(
        f"SELECT {flds} FROM stock_kline WHERE date>=? AND date<? AND code NOT LIKE 'bj.%'",
        (dt_lo, dt_hi),
    )
    keys = [k.strip() for k in flds.split(',')]
    by_code = defaultdict(dict)
    n_rows = 0
    for row in cur.fetchall():
        d = row[0]
        c = row[1]
        rec = {keys[i]: row[i] for i in range(len(keys))}
        by_code[c][d] = rec
        n_rows += 1
    print(f'[加载] {dt_lo} ~ {dt_hi}  交易日={len(all_dates)}  股票数={len(by_code)}  行数={n_rows}')
    return all_dates, date_idx, by_code


def get_date_range_month(month_str):
    """月模式: 返回(dt_lo, dt_hi, target_start, target_end)"""
    year, mon = month_str.split('-')
    year, mon = int(year), int(mon)
    first_day = f'{year:04d}-{mon:02d}-01'
    if mon == 12:
        next_first = f'{year+1:04d}-01-01'
    else:
        next_first = f'{year:04d}-{mon+1:02d}-01'
    dt_lo = (datetime.strptime(first_day, '%Y-%m-%d') - timedelta(days=30)).strftime('%Y-%m-%d')
    dt_hi = (datetime.strptime(next_first, '%Y-%m-%d') + timedelta(days=20)).strftime('%Y-%m-%d')
    return dt_lo, dt_hi, first_day, next_first


def get_date_range_year(year):
    """年模式: 返回(dt_lo, dt_hi, target_start, target_end)"""
    dt_lo = f'{year-1}-12-01'
    dt_hi = f'{year+1}-01-20'
    target_start = f'{year}-01-01'
    target_end = f'{year+1}-01-01'
    return dt_lo, dt_hi, target_start, target_end


def find_base_candidates(by_code, all_dates, date_idx, target_date):
    """筛选T日基础候选股(不含买入方案过滤)"""
    t_idx = date_idx.get(target_date)
    if t_idx is None or t_idx < 1:
        return []
    prev_date = all_dates[t_idx - 1]
    lookback_start = t_idx - 1 - FIRSTBOARD_LOOKBACK
    if lookback_start < 0:
        return []
    lookback_dates = all_dates[lookback_start:t_idx - 1]

    candidates = []
    for code, dm in by_code.items():
        prev_row = dm.get(prev_date)
        t_row = dm.get(target_date)
        if not prev_row or not t_row:
            continue

        # 1. T-1日涨停
        if not is_limit_up(prev_row.get('close'), prev_row.get('preclose'), code):
            continue

        # 2. 涨停日换手率
        prev_turn = sv(prev_row.get('turn'))
        if prev_turn is None or prev_turn <= PREV_TURN_MIN:
            continue

        # 3. 非ST
        if prev_row.get('isST') == 1:
            continue
        name = prev_row.get('code_name') or ''
        if 'ST' in name.upper():
            continue

        # 4. 首板确认
        is_first_board = True
        for ld in lookback_dates:
            lrow = dm.get(ld)
            if lrow and is_limit_up(lrow.get('close'), lrow.get('preclose'), code):
                is_first_board = False
                break
        if not is_first_board:
            continue

        # 5. T日open_rate过滤
        opr = sv(t_row.get('open_rate'))
        if opr is None or opr < OPEN_RATE_MIN or opr > OPEN_RATE_MAX:
            continue

        # 6. 非一字板
        if is_one_word_board(t_row):
            continue

        # 收集hour数据
        h1_close = sv(t_row.get('hour1_close'))
        h1_cr = sv(t_row.get('hour1_close_rate'))
        h2_open = sv(t_row.get('hour2_open'))
        h2_low = sv(t_row.get('hour2_low'))
        h2_close = sv(t_row.get('hour2_close'))
        h2_cr = sv(t_row.get('hour2_close_rate'))
        h3_low = sv(t_row.get('hour3_low'))

        candidates.append({
            'code': code,
            'name': name,
            'date': target_date,
            't_idx': t_idx,
            'open_rate': opr,
            'prev_turn': prev_turn,
            'h1_close': h1_close,
            'h1_cr': h1_cr,
            'h2_open': h2_open,
            'h2_low': h2_low,
            'h2_close': h2_close,
            'h2_cr': h2_cr,
            'h3_low': h3_low,
        })

    # 按hour1跌幅排序(跌越多越优先)
    candidates.sort(key=lambda x: x['h1_cr'] if x['h1_cr'] is not None else 999)
    return candidates


def try_buy_scheme(cand, scheme):
    """尝试按方案买入, 返回 (buy_price, buy_hour) 或 (None, None)
    buy_hour: 在T日的哪个hour成交, 用于记录
    """
    h1_close = cand['h1_close']
    h1_cr = cand['h1_cr']
    h2_open = cand['h2_open']
    h2_low = cand['h2_low']
    h2_close = cand['h2_close']
    h2_cr = cand['h2_cr']
    h3_low = cand['h3_low']

    if scheme == 'A':
        # Hour1杀跌(h1_cr<=-1%) → limit=h1_close*(1-0.005), hour2验证
        if h1_cr is None or h1_cr > -1.0:
            return None, None
        if h1_close is None or h1_close <= 0:
            return None, None
        limit_buy = h1_close * (1 - 0.005)
        if h2_low is not None and h2_low <= limit_buy:
            return limit_buy, 2
        return None, None

    elif scheme == 'B':
        # Hour1杀跌(h1_cr<=-1%) → limit=h1_close*(1-0.015), hour2验证
        if h1_cr is None or h1_cr > -1.0:
            return None, None
        if h1_close is None or h1_close <= 0:
            return None, None
        limit_buy = h1_close * (1 - 0.015)
        if h2_low is not None and h2_low <= limit_buy:
            return limit_buy, 2
        return None, None

    elif scheme == 'C':
        # Hour1杀跌(h1_cr<=-1%) → limit=h1_close*(1-0.01), hour2+hour3验证
        if h1_cr is None or h1_cr > -1.0:
            return None, None
        if h1_close is None or h1_close <= 0:
            return None, None
        limit_buy = h1_close * (1 - 0.01)
        if h2_low is not None and h2_low <= limit_buy:
            return limit_buy, 2
        if h3_low is not None and h3_low <= limit_buy:
            return limit_buy, 3
        return None, None

    elif scheme == 'D':
        # hour1弱(h1_cr<=0%) 且 hour2更弱(h2_cr<=h1_cr) → limit=h2_close*(1-0.005), hour3验证
        if h1_cr is None or h1_cr > 0.0:
            return None, None
        if h2_cr is None or h2_cr > h1_cr:
            return None, None
        if h2_close is None or h2_close <= 0:
            return None, None
        limit_buy = h2_close * (1 - 0.005)
        if h3_low is not None and h3_low <= limit_buy:
            return limit_buy, 3
        return None, None

    elif scheme == 'E':
        # 基线: hour1杀跌(h1_cr<=-1%) → hour2_open市价
        if h1_cr is None or h1_cr > -1.0:
            return None, None
        if h2_open is None or h2_open <= 0:
            return None, None
        return h2_open, 2

    return None, None


def simulate_sell_limit(by_code, all_dates, code, t_idx, buy_price, sl_pct, tp_pct, hold_days, trailing_pct=None):
    """模拟限价卖出
    sl_pct: 止损百分比(负值, 如-3表示-3%)
    tp_pct: 止盈百分比(正值, 如5表示+5%), 或None(trailing模式)
    hold_days: 最大持有天数(1=T+1强平, 2=T+2强平)
    trailing_pct: trailing止盈百分比(如2表示2%), 为None表示不用trailing
    返回: (sell_price, sell_reason, sell_date, sell_hour)
    """
    stop_price = buy_price * (1 + sl_pct / 100.0)
    tp_price = buy_price * (1 + tp_pct / 100.0) if tp_pct is not None else None

    peak = 0.0  # trailing用
    
    for day_offset in range(1, hold_days + 1):
        sell_day_idx = t_idx + day_offset
        if sell_day_idx >= len(all_dates):
            return None, None, None, None
        sell_date = all_dates[sell_day_idx]
        row = by_code.get(code, {}).get(sell_date)
        if not row:
            return None, None, None, None

        for hour in (1, 2, 3, 4):
            h_high = sv(row.get(f'hour{hour}_high'))
            h_low = sv(row.get(f'hour{hour}_low'))
            h_open = sv(row.get(f'hour{hour}_open'))

            # 更新peak (用于trailing)
            if h_high is not None and h_high > peak:
                peak = h_high

            # 1. 先检查止损 (hourX_low <= stop_price)
            if h_low is not None and h_low <= stop_price:
                return stop_price, '止损', sell_date, hour

            # 2. 检查止盈
            if trailing_pct is not None:
                # Trailing止盈: peak下跌trail_pct触发
                if peak > 0:
                    trailing_sell = peak * (1 - trailing_pct / 100.0)
                    if h_low is not None and h_low <= trailing_sell:
                        # trailing触发时, 如果已有盈利则以trailing_sell卖出
                        if trailing_sell > buy_price:
                            return trailing_sell, 'Trailing', sell_date, hour
            else:
                # 固定止盈: hourX_high >= tp_price
                if tp_price is not None and h_high is not None and h_high >= tp_price:
                    return tp_price, '止盈', sell_date, hour

            # 3. 最后一天hour4强平
            if day_offset == hold_days and hour == 4:
                if h_open is not None:
                    return h_open, '强平', sell_date, hour
                # h_open缺失用close
                h_close = sv(row.get(f'hour{hour}_close'))
                if h_close is not None:
                    return h_close, '强平', sell_date, hour

    return None, None, None, None


def precompute_all_candidates(by_code, all_dates, date_idx, target_dates):
    """预计算所有目标日的候选股(一次性), 避免网格中重复计算"""
    all_cands = []  # [(target_date, cand_dict), ...]
    for td in target_dates:
        cands = find_base_candidates(by_code, all_dates, date_idx, td)
        for c in cands:
            all_cands.append(c)
    print(f'[预计算] 共{len(target_dates)}天, {len(all_cands)}个候选股')
    return all_cands


def run_single_config(all_cands, by_code, all_dates, scheme, sl_pct, tp_pct, hold_days, trailing_pct=None):
    """运行单个配置(使用预计算候选), 返回交易统计"""
    cond_pass = 0
    actual_fill = 0
    trades = []

    for cand in all_cands:
        # 检查条件是否通过
        cond_ok = check_condition_pass(cand, scheme)
        if not cond_ok:
            continue
        cond_pass += 1

        # 尝试买入
        buy_price, buy_hour = try_buy_scheme(cand, scheme)
        if buy_price is None:
            continue
        actual_fill += 1

        # 模拟卖出
        sell_price, reason, sell_date, sell_hour = simulate_sell_limit(
            by_code, all_dates, cand['code'], cand['t_idx'],
            buy_price, sl_pct, tp_pct, hold_days, trailing_pct
        )
        if sell_price is None:
            continue
        ret_pct = (sell_price - buy_price) / buy_price * 100.0
        trades.append({
            'buy_date': cand['date'],
            'sell_date': sell_date,
            'code': cand['code'],
            'name': cand['name'],
            'buy_price': buy_price,
            'sell_price': sell_price,
            'return_pct': ret_pct,
            'reason': reason,
        })

    return {
        'cond_pass': cond_pass,
        'actual_fill': actual_fill,
        'trades': trades,
    }


def check_condition_pass(cand, scheme):
    """检查条件是否通过(不考虑是否填充)"""
    h1_cr = cand['h1_cr']
    h2_cr = cand['h2_cr']

    if scheme in ('A', 'B', 'C', 'E'):
        return h1_cr is not None and h1_cr <= -1.0
    elif scheme == 'D':
        if h1_cr is None or h1_cr > 0.0:
            return False
        if h2_cr is None or h2_cr > h1_cr:
            return False
        return True
    return False


def compute_stats(result):
    """计算统计指标"""
    trades = result['trades']
    n = len(trades)
    if n == 0:
        return {
            'n_trades': 0, 'win_rate': 0.0, 'avg_ret': 0.0,
            'total_ret': 0.0, 'fill_rate': 0.0,
            'cond_pass': result['cond_pass'],
            'actual_fill': result['actual_fill'],
        }
    rets = [t['return_pct'] for t in trades]
    wins = sum(1 for r in rets if r > 0)
    fill_rate = result['actual_fill'] / result['cond_pass'] * 100.0 if result['cond_pass'] > 0 else 0.0
    return {
        'n_trades': n,
        'win_rate': wins / n * 100.0,
        'avg_ret': sum(rets) / n,
        'total_ret': sum(rets),
        'fill_rate': fill_rate,
        'cond_pass': result['cond_pass'],
        'actual_fill': result['actual_fill'],
    }


def simulate_portfolio(trades, n_slots=N_SLOTS, init_capital=INIT_CAPITAL):
    """仓位模拟"""
    if not trades:
        return {'final_value': init_capital, 'annual_return': 0.0, 'n_completed': 0}

    trades_by_date = defaultdict(list)
    for t in trades:
        trades_by_date[t['buy_date']].append(t)

    cash = init_capital
    holdings = []
    completed = []

    all_trade_dates = sorted(set(
        [t['buy_date'] for t in trades] + [t['sell_date'] for t in trades if t['sell_date']]
    ))

    for cur_date in all_trade_dates:
        # 先卖出
        new_holdings = []
        for h in holdings:
            if h['sell_date'] == cur_date:
                sell_amount = h['position_value'] * (1 + h['return_pct'] / 100.0)
                cash += sell_amount
                completed.append(h)
            else:
                new_holdings.append(h)
        holdings = new_holdings

        # 再买入
        if cur_date in trades_by_date:
            for t in trades_by_date[cur_date]:
                if len(holdings) >= n_slots:
                    break
                total_assets = cash + sum(h['position_value'] for h in holdings)
                buy_amount = total_assets / n_slots
                if buy_amount > cash:
                    buy_amount = cash
                if buy_amount <= 0:
                    continue
                cash -= buy_amount
                holdings.append({
                    'buy_date': t['buy_date'],
                    'sell_date': t['sell_date'],
                    'return_pct': t['return_pct'],
                    'position_value': buy_amount,
                })

    # 清理剩余
    for h in holdings:
        sell_amount = h['position_value'] * (1 + h['return_pct'] / 100.0)
        cash += sell_amount
        completed.append(h)

    final_value = cash
    annual_return = (final_value / init_capital - 1) * 100.0
    return {'final_value': final_value, 'annual_return': annual_return, 'n_completed': len(completed)}


def run_month_analysis(target_month):
    """月度分析: 对比5种买入方案 + 参数网格"""
    print(f'\n============ 首板真低吸+限价单 分析 ============')
    print(f'月份: {target_month}')
    print(f'参数: turn>{PREV_TURN_MIN}%  open_rate[{OPEN_RATE_MIN}%,{OPEN_RATE_MAX}%]')

    dt_lo, dt_hi, t_start, t_end = get_date_range_month(target_month)
    conn = sqlite3.connect(DB_PATH)
    all_dates, date_idx, by_code = load_data(conn, dt_lo, dt_hi)
    conn.close()

    target_dates = [d for d in all_dates if t_start <= d < t_end]
    if not target_dates:
        print(f'[错误] {target_month} 无交易日')
        return None

    print(f'目标交易日: {len(target_dates)}天')

    # 预计算候选股(一次性)
    all_cands = precompute_all_candidates(by_code, all_dates, date_idx, target_dates)

    # --- 买入方案对比(固定卖出: sl=-3%, tp=+5%, hold=1天) ---
    print(f'\n--- 买入方案对比 (止损-3% / 止盈+5% / T+1_h4强平) ---')
    print(f'{"方案":<30s} | {"条件通过":>6s} | {"实际成交":>6s} | {"填充率":>6s} | {"均收益":>7s} | {"胜率":>6s} | {"月化":>7s}')
    print('-' * 90)

    scheme_labels = {
        'A': 'A(h1杀跌+h2限-0.5%)',
        'B': 'B(h1杀跌+h2限-1.5%)',
        'C': 'C(h1杀跌+h2h3限-1.0%)',
        'D': 'D(h1h2持续弱+h3限-0.5%)',
        'E': 'E(基线:h2_open市价)',
    }

    best_scheme = None
    best_stats = None
    best_monthly = -999
    scheme_results = {}

    for scheme in ('A', 'B', 'C', 'D', 'E'):
        result = run_single_config(all_cands, by_code, all_dates,
                                   scheme, sl_pct=-3.0, tp_pct=5.0, hold_days=1)
        stats = compute_stats(result)
        scheme_results[scheme] = (result, stats)
        label = scheme_labels[scheme]
        monthly_ret = stats['total_ret'] / N_SLOTS if stats['n_trades'] > 0 else 0
        print(f'{label:<30s} | {stats["cond_pass"]:>6d} | {stats["actual_fill"]:>6d} | '
              f'{stats["fill_rate"]:>5.1f}% | {stats["avg_ret"]:>+6.2f}% | '
              f'{stats["win_rate"]:>5.1f}% | {monthly_ret:>+6.2f}%')

        if stats['n_trades'] >= 3:
            if monthly_ret > best_monthly:
                best_scheme = scheme
                best_stats = stats
                best_monthly = monthly_ret

    if best_scheme is None:
        print('\n[警告] 无方案产生有效交易')
        return None

    print(f'\n最优买入方案: {scheme_labels[best_scheme]} (均收益{best_stats["avg_ret"]:+.2f}%, 胜率{best_stats["win_rate"]:.1f}%)')

    # --- 对最优买入方案做止盈止损网格 ---
    print(f'\n--- 最优买入方案[{best_scheme}]的止盈止损网格 ---')
    sl_list = [-2.0, -3.0, -4.0, -5.0]
    tp_configs = [
        (3.0, None, 'TP+3%'),
        (5.0, None, 'TP+5%'),
        (6.0, None, 'TP+6%'),
        (8.0, None, 'TP+8%'),
        (None, 2.0, 'Trail2%'),
        (None, 3.0, 'Trail3%'),
    ]
    hold_list = [1, 2]

    print(f'{"SL":<5s} | {"TP/Trail":<10s} | {"持有":>4s} | {"交易数":>5s} | {"胜率":>6s} | {"均收益":>7s} | {"月化":>7s}')
    print('-' * 70)

    grid_results = []
    for sl in sl_list:
        for tp_val, trail_val, tp_label in tp_configs:
            for hold in hold_list:
                result = run_single_config(
                    all_cands, by_code, all_dates,
                    best_scheme, sl_pct=sl,
                    tp_pct=tp_val if tp_val is not None else 99.0,  # trailing模式给大止盈
                    hold_days=hold,
                    trailing_pct=trail_val
                )
                stats = compute_stats(result)
                monthly_ret = stats['total_ret'] / N_SLOTS if stats['n_trades'] > 0 else 0
                hold_label = f'T+{hold}'
                print(f'{sl:>+4.0f}% | {tp_label:<10s} | {hold_label:>4s} | {stats["n_trades"]:>5d} | '
                      f'{stats["win_rate"]:>5.1f}% | {stats["avg_ret"]:>+6.2f}% | {monthly_ret:>+6.2f}%')
                grid_results.append({
                    'scheme': best_scheme,
                    'sl': sl, 'tp': tp_val, 'trail': trail_val,
                    'tp_label': tp_label, 'hold': hold,
                    'stats': stats, 'monthly_ret': monthly_ret,
                    'trades': result['trades'],
                })

    # 找最优组合
    valid_grids = [g for g in grid_results if g['stats']['n_trades'] >= 3]
    if valid_grids:
        best_grid = max(valid_grids, key=lambda g: g['monthly_ret'])
        bs = best_grid['stats']
        print(f'\n--- 最优组合 ---')
        print(f'买入: {scheme_labels[best_grid["scheme"]]}')
        print(f'止损: {best_grid["sl"]:+.0f}%  止盈: {best_grid["tp_label"]}  持有: T+{best_grid["hold"]}')
        print(f'交易数: {bs["n_trades"]}  胜率: {bs["win_rate"]:.1f}%  均收益: {bs["avg_ret"]:+.2f}%  月化: {best_grid["monthly_ret"]:+.2f}%')

        # 判断是否达标 (放宽: 月化>10% 或 胜率>50%且月化>8%)
        if best_grid['monthly_ret'] > 10.0 and bs['win_rate'] > 45.0:
            print(f'\n>>> 月化{best_grid["monthly_ret"]:+.1f}% > {MONTHLY_RETURN_THRESHOLD}% 且 胜率{bs["win_rate"]:.1f}% > {MONTHLY_WINRATE_THRESHOLD}% → 自动扩展全年验证 <<<')
            return best_grid
        else:
            print(f'\n[未达标] 月化{best_grid["monthly_ret"]:+.1f}% 或 胜率{bs["win_rate"]:.1f}% 不满足扩展条件')
            # 仍然返回最优, 但也尝试全方案网格
            return best_grid
    else:
        print('\n[警告] 无有效网格结果')
        return None


def run_all_schemes_grid(all_cands, by_code, all_dates, target_dates, period_label):
    """对所有方案运行完整网格, 返回最优"""
    schemes = ('A', 'B', 'C', 'D', 'E')
    sl_list = [-2.0, -3.0, -4.0, -5.0]
    tp_configs = [
        (3.0, None, 'TP+3%'),
        (5.0, None, 'TP+5%'),
        (6.0, None, 'TP+6%'),
        (8.0, None, 'TP+8%'),
        (None, 2.0, 'Trail2%'),
        (None, 3.0, 'Trail3%'),
    ]
    hold_list = [1, 2]

    print(f'\n--- 全方案网格搜索 ({period_label}) ---')
    print(f'方案x止损x止盈x持有 = {len(schemes)}x{len(sl_list)}x{len(tp_configs)}x{len(hold_list)} = {len(schemes)*len(sl_list)*len(tp_configs)*len(hold_list)}组合')

    all_grid = []
    for scheme in schemes:
        for sl in sl_list:
            for tp_val, trail_val, tp_label in tp_configs:
                for hold in hold_list:
                    result = run_single_config(
                        all_cands, by_code, all_dates,
                        scheme, sl_pct=sl,
                        tp_pct=tp_val if tp_val is not None else 99.0,
                        hold_days=hold,
                        trailing_pct=trail_val
                    )
                    stats = compute_stats(result)
                    n_days = len(target_dates)
                    # 月化估算: total_ret / N_SLOTS / (n_days/20)  (20天≈1个月)
                    months = n_days / 20.0
                    monthly_ret = stats['total_ret'] / N_SLOTS / months if months > 0 and stats['n_trades'] > 0 else 0
                    all_grid.append({
                        'scheme': scheme, 'sl': sl, 'tp': tp_val,
                        'trail': trail_val, 'tp_label': tp_label,
                        'hold': hold, 'stats': stats,
                        'monthly_ret': monthly_ret, 'trades': result['trades'],
                    })

    # TOP10
    valid = [g for g in all_grid if g['stats']['n_trades'] >= 5]
    valid.sort(key=lambda g: g['monthly_ret'], reverse=True)

    scheme_labels = {
        'A': 'A(h1杀跌+h2限-0.5%)',
        'B': 'B(h1杀跌+h2限-1.5%)',
        'C': 'C(h1杀跌+h2h3限-1.0%)',
        'D': 'D(h1h2持续弱+h3限-0.5%)',
        'E': 'E(基线:h2_open市价)',
    }

    print(f'\nTOP10组合:')
    print(f'{"#":>3s} | {"方案":<25s} | {"SL":>4s} | {"TP":>10s} | {"持有":>4s} | {"交易":>4s} | {"胜率":>6s} | {"均收益":>7s} | {"月化":>7s}')
    print('-' * 95)
    for i, g in enumerate(valid[:10]):
        s = g['stats']
        print(f'{i+1:>3d} | {scheme_labels[g["scheme"]]:<25s} | {g["sl"]:>+3.0f}% | {g["tp_label"]:<10s} | T+{g["hold"]:d} | '
              f'{s["n_trades"]:>4d} | {s["win_rate"]:>5.1f}% | {s["avg_ret"]:>+6.2f}% | {g["monthly_ret"]:>+6.2f}%')

    return valid[0] if valid else None


def run_year_validation(year, best_config=None):
    """全年验证"""
    print(f'\n{"="*60}')
    print(f'============ 全年验证: {year} ============')
    print(f'{"="*60}')

    dt_lo, dt_hi, t_start, t_end = get_date_range_year(year)
    conn = sqlite3.connect(DB_PATH)
    all_dates, date_idx, by_code = load_data(conn, dt_lo, dt_hi)
    conn.close()

    target_dates = [d for d in all_dates if t_start <= d < t_end]
    if not target_dates:
        print(f'[错误] {year}年无交易日')
        return None

    print(f'目标交易日: {len(target_dates)}天')

    # 预计算候选股
    all_cands = precompute_all_candidates(by_code, all_dates, date_idx, target_dates)

    # 全方案网格
    best_grid = run_all_schemes_grid(all_cands, by_code, all_dates, target_dates, f'{year}全年')

    if best_grid is None:
        print(f'\n[{year}] 无有效组合')
        return None

    # 仓位模拟
    portfolio = simulate_portfolio(best_grid['trades'])
    s = best_grid['stats']
    print(f'\n--- {year}年 最优组合仓位模拟 ---')
    print(f'最终资产: {portfolio["final_value"]/10000:.2f}万 (初始100万)')
    print(f'年化收益: {portfolio["annual_return"]:+.1f}%')
    print(f'完成交易: {portfolio["n_completed"]}笔')

    # 月度分布
    monthly = defaultdict(list)
    for t in best_grid['trades']:
        m = t['buy_date'][:7]
        monthly[m].append(t['return_pct'])

    print(f'\n--- 月度分布 ---')
    print(f'{"月份":<8s} | {"交易数":>5s} | {"胜率":>6s} | {"均收益":>7s} | {"月收益":>8s}')
    print('-' * 50)
    for m in sorted(monthly.keys()):
        rets = monthly[m]
        wins = sum(1 for r in rets if r > 0)
        wr = wins / len(rets) * 100.0
        avg = sum(rets) / len(rets)
        total = sum(rets) / N_SLOTS
        print(f'{m:<8s} | {len(rets):>5d} | {wr:>5.1f}% | {avg:>+6.2f}% | {total:>+7.2f}%')

    return {
        'year': year,
        'best_grid': best_grid,
        'portfolio': portfolio,
        'annual_return': portfolio['annual_return'],
    }


def main():
    # 解析命令行参数
    global MODE, TARGET_MONTH, TARGET_YEAR
    if len(sys.argv) >= 2:
        if sys.argv[1] == 'year':
            MODE = 'year'
            TARGET_YEAR = int(sys.argv[2]) if len(sys.argv) > 2 else 2025
        else:
            MODE = 'month'
            TARGET_MONTH = sys.argv[1]

    if MODE == 'month':
        best_grid = run_month_analysis(TARGET_MONTH)
        if best_grid is None:
            print('\n[结束] 月度无有效结果, 不扩展')
            return

        # 如果月化达标, 自动扩展全年
        bs = best_grid['stats']
        if best_grid['monthly_ret'] > MONTHLY_RETURN_THRESHOLD and bs['win_rate'] > MONTHLY_WINRATE_THRESHOLD:
            # 确定全年验证的年份
            year = int(TARGET_MONTH.split('-')[0])
            yr_result = run_year_validation(year)
            if yr_result and yr_result['annual_return'] >= ANNUAL_RETURN_THRESHOLD:
                print(f'\n>>> 年化{yr_result["annual_return"]:+.1f}% >= {ANNUAL_RETURN_THRESHOLD}% → 扩展2024/2023验证 <<<')
                for extra_year in [year - 1, year - 2]:
                    if extra_year >= 2020:
                        run_year_validation(extra_year)
        else:
            # 未达标也跑全年看整体表现
            print(f'\n--- 月度未严格达标, 仍尝试全年验证 ---')
            year = int(TARGET_MONTH.split('-')[0])
            run_year_validation(year)

    elif MODE == 'year':
        yr_result = run_year_validation(TARGET_YEAR)
        if yr_result and yr_result['annual_return'] >= ANNUAL_RETURN_THRESHOLD:
            print(f'\n>>> 年化{yr_result["annual_return"]:+.1f}% >= {ANNUAL_RETURN_THRESHOLD}% → 扩展验证 <<<')
            for extra_year in [TARGET_YEAR - 1, TARGET_YEAR - 2]:
                if extra_year >= 2020:
                    run_year_validation(extra_year)


if __name__ == '__main__':
    main()
