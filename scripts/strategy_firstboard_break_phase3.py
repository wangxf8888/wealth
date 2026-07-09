#!/usr/bin/env python3
"""首板断板低吸策略 Phase-3a: 全年验证

规则(已确定):
  - 候选股: 昨日首板涨停(round(close/preclose,2)>=1.10/1.20) + 涨停日turn>15%
           + 前5日无涨停 + 今日open_rate∈[-5%,+5%] + 非ST + 非一字板 + 非北交所
  - 买入: T日 hour2_open
  - 卖出: T+1日逐hour检查open, 止损-4% / 止盈+8% / hour4_open强平
  - 仓位: N=3, 初始100万

严格合规:
  - 买/卖价仅取 hourX_open
  - T+1合规: 买入当日不卖
  - 涨停判定: round(close/preclose, 2) 严格规则
  - 不使用未来数据

用法:
  python3 scripts/strategy_firstboard_break_phase3.py [year, default=2025]
"""
import sqlite3
import sys
import math
from collections import defaultdict

# ============== 配置区 ==============
DB_PATH = '/home/AIWealth/data/stocks.db'
TARGET_YEAR = int(sys.argv[1]) if len(sys.argv) > 1 else 2025

# 筛选阈值
OPEN_RATE_MIN = -5.0   # T日开盘涨幅下限
OPEN_RATE_MAX = 5.0    # T日开盘涨幅上限
PREV_TURN_MIN = 15.0   # 涨停日(T-1)换手率下限
FIRSTBOARD_LOOKBACK = 5 # 首板回溯天数

# 止盈止损
STOP_LOSS_PCT = -4.0   # 止损线 %
TAKE_PROFIT_PCT = 8.0  # 止盈线 %

# 仓位模拟
N_SLOTS = 3            # 最大同时持仓数
INIT_CAPITAL = 1000000.0  # 初始资金 100万
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
    """一字板判定: open==high且high==low"""
    o = sv(row.get('open'))
    h = sv(row.get('high'))
    l = sv(row.get('low'))
    if None in (o, h, l):
        return False
    return o == h and h == l


def load_year_data(conn, year):
    """加载全年数据(含前后缓冲期)"""
    cur = conn.cursor()
    # 需要前一年12月数据(覆盖1月初的前5日判断)和次年1月数据(覆盖12月末T+1卖出)
    dt_lo = f'{year-1}-12-01'
    dt_hi = f'{year+1}-01-15'

    cur.execute(
        'SELECT DISTINCT date FROM stock_kline WHERE date>=? AND date<? ORDER BY date',
        (dt_lo, dt_hi),
    )
    all_dates = [r[0] for r in cur.fetchall()]
    date_idx = {d: i for i, d in enumerate(all_dates)}

    target_start = f'{year}-01-01'
    target_end = f'{year+1}-01-01'
    target_dates = [d for d in all_dates if target_start <= d < target_end]

    flds = (
        'date,code,code_name,preclose,open,high,low,close,turn,isST,open_rate,'
        'hour1_open,hour1_close_rate,'
        'hour2_open,'
        'hour3_open,'
        'hour4_open'
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
    print(f'[加载] {dt_lo} ~ {dt_hi}  交易日={len(all_dates)}  目标日={len(target_dates)}  '
          f'股票数={len(by_code)}  行数={n_rows}')
    return all_dates, date_idx, target_dates, by_code


def find_candidates_for_date(by_code, all_dates, date_idx, target_date):
    """筛选某一交易日T的候选股"""
    t_idx = date_idx.get(target_date)
    if t_idx is None or t_idx < 1:
        return []
    prev_date = all_dates[t_idx - 1]  # T-1日(涨停日)
    lookback_start = t_idx - 1 - FIRSTBOARD_LOOKBACK
    if lookback_start < 0:
        return []
    lookback_dates = all_dates[lookback_start:t_idx - 1]  # T-6~T-2日

    candidates = []
    for code, dm in by_code.items():
        if code.startswith('bj.'):
            continue
        prev_row = dm.get(prev_date)
        t_row = dm.get(target_date)
        if not prev_row or not t_row:
            continue

        # 1. T-1日涨停
        if not is_limit_up(prev_row.get('close'), prev_row.get('preclose'), code):
            continue

        # 2. 涨停日换手率 > 15%
        prev_turn = sv(prev_row.get('turn'))
        if prev_turn is None or prev_turn <= PREV_TURN_MIN:
            continue

        # 3. 非ST
        if prev_row.get('isST') == 1:
            continue
        name = prev_row.get('code_name') or ''
        if 'ST' in name.upper():
            continue

        # 4. 首板确认: 前5日无涨停
        is_first_board = True
        for ld in lookback_dates:
            lrow = dm.get(ld)
            if lrow and is_limit_up(lrow.get('close'), lrow.get('preclose'), code):
                is_first_board = False
                break
        if not is_first_board:
            continue

        # 5. T日过滤: open_rate, 非一字板
        opr = sv(t_row.get('open_rate'))
        if opr is None or opr < OPEN_RATE_MIN or opr > OPEN_RATE_MAX:
            continue
        if is_one_word_board(t_row):
            continue

        # 买入价: T日 hour2_open
        buy_price = sv(t_row.get('hour2_open'))
        if buy_price is None or buy_price <= 0:
            continue

        candidates.append({
            'code': code,
            'name': name,
            'date': target_date,
            't_idx': t_idx,
            'buy_price': buy_price,
            'open_rate': opr,
            'prev_turn': prev_turn,
        })

    # 按涨停日换手率降序排列(高换手优先)
    candidates.sort(key=lambda x: x['prev_turn'], reverse=True)
    return candidates


def simulate_sell(by_code, all_dates, code, t_idx, buy_price):
    """模拟卖出: T+1日逐hour检查open价格
    返回: (sell_price, sell_reason, sell_date, sell_hour)
    """
    sl_price = buy_price * (1 + STOP_LOSS_PCT / 100.0)
    tp_price = buy_price * (1 + TAKE_PROFIT_PCT / 100.0)

    # T+1日
    t1_idx = t_idx + 1
    if t1_idx >= len(all_dates):
        return None, None, None, None
    t1_date = all_dates[t1_idx]
    t1_row = by_code.get(code, {}).get(t1_date)
    if not t1_row:
        return None, None, None, None

    # 逐hour检查: hour1 → hour2 → hour3 → hour4
    for hour in (1, 2, 3, 4):
        p = sv(t1_row.get(f'hour{hour}_open'))
        if p is None:
            continue
        # 先检查止损
        if p <= sl_price:
            return p, '止损', t1_date, hour
        # 再检查止盈
        if p >= tp_price:
            return p, '止盈', t1_date, hour
        # hour4强平
        if hour == 4:
            return p, '强平', t1_date, hour

    # 所有hour数据缺失, 无法卖出
    return None, None, None, None


def run_year_validation(year):
    """执行全年验证"""
    print(f'\n============ 首板断板低吸 Phase-3a 全年验证 ============')
    print(f'年份: {year}')
    print(f'参数: 涨停日turn>{PREV_TURN_MIN}%  open_rate∈[{OPEN_RATE_MIN}%,{OPEN_RATE_MAX}%]  '
          f'止损{STOP_LOSS_PCT}%  止盈+{TAKE_PROFIT_PCT}%  仓位N={N_SLOTS}')

    conn = sqlite3.connect(DB_PATH)
    all_dates, date_idx, target_dates, by_code = load_year_data(conn, year)
    conn.close()

    if not target_dates:
        print(f'[错误] {year}年无交易日数据')
        return None

    # --- 逐日执行策略 ---
    all_trades = []  # 所有交易记录
    monthly_stats = defaultdict(lambda: {'candidates': 0, 'trades': 0, 'returns': []})

    for td in target_dates:
        candidates = find_candidates_for_date(by_code, all_dates, date_idx, td)
        month_key = td[:7]
        monthly_stats[month_key]['candidates'] += len(candidates)

        for cand in candidates:
            sell_price, reason, sell_date, sell_hour = simulate_sell(
                by_code, all_dates, cand['code'], cand['t_idx'], cand['buy_price']
            )
            if sell_price is None:
                continue
            ret_pct = (sell_price - cand['buy_price']) / cand['buy_price'] * 100.0
            trade = {
                'buy_date': td,
                'sell_date': sell_date,
                'code': cand['code'],
                'name': cand['name'],
                'buy_price': cand['buy_price'],
                'sell_price': sell_price,
                'return_pct': ret_pct,
                'reason': reason,
                'prev_turn': cand['prev_turn'],
                'sell_hour': sell_hour,
            }
            all_trades.append(trade)
            monthly_stats[month_key]['trades'] += 1
            monthly_stats[month_key]['returns'].append(ret_pct)

    # --- 月度明细 ---
    print(f'\n--- 月度明细 ---')
    print(f'{"月份":<8s} | {"候选数":>5s} | {"交易数":>5s} | {"胜率":>6s} | {"均收益":>8s} | {"月累计收益":>10s} | {"累计净值":>8s}')
    print('-' * 72)

    cumulative_nv = 1.0
    months_sorted = sorted(monthly_stats.keys())
    for m in months_sorted:
        ms = monthly_stats[m]
        rets = ms['returns']
        n_trades = ms['trades']
        n_cands = ms['candidates']
        if n_trades > 0:
            wins = sum(1 for r in rets if r > 0)
            win_rate = wins / n_trades * 100.0
            avg_ret = sum(rets) / n_trades
            month_total = sum(rets)
            # 月累计对净值的影响(简单加法模拟, 每笔1/N仓位)
            for r in rets:
                cumulative_nv *= (1 + r / 100.0 / N_SLOTS)
        else:
            win_rate = 0.0
            avg_ret = 0.0
            month_total = 0.0
        print(f'{m:<8s} | {n_cands:>5d} | {n_trades:>5d} | {win_rate:>5.1f}% | {avg_ret:>+7.2f}% | '
              f'{month_total:>+9.1f}% | {cumulative_nv:>7.3f}')

    # --- 全年汇总 ---
    total_trades = len(all_trades)
    if total_trades == 0:
        print('\n[错误] 全年无有效交易')
        return None

    all_returns = [t['return_pct'] for t in all_trades]
    total_wins = sum(1 for r in all_returns if r > 0)
    total_win_rate = total_wins / total_trades * 100.0
    avg_return = sum(all_returns) / total_trades
    total_simple = sum(all_returns)

    # 盈亏比
    win_rets = [r for r in all_returns if r > 0]
    loss_rets = [r for r in all_returns if r <= 0]
    avg_win = sum(win_rets) / len(win_rets) if win_rets else 0
    avg_loss = sum(loss_rets) / len(loss_rets) if loss_rets else 0
    pl_ratio = avg_win / abs(avg_loss) if avg_loss != 0 else float('inf')

    # 最大连亏
    max_consec_loss = 0
    cur_loss = 0
    for r in all_returns:
        if r <= 0:
            cur_loss += 1
            max_consec_loss = max(max_consec_loss, cur_loss)
        else:
            cur_loss = 0

    # 最大月亏损
    max_month_loss = 0
    for m in months_sorted:
        rets = monthly_stats[m]['returns']
        if rets:
            month_sum = sum(rets)
            if month_sum < max_month_loss:
                max_month_loss = month_sum

    print(f'\n--- 全年汇总 ---')
    print(f'总交易数: {total_trades}')
    print(f'总胜率: {total_win_rate:.1f}%')
    print(f'平均每笔收益: {avg_return:+.2f}%')
    print(f'全年累计收益(简单): {total_simple:+.1f}%')
    print(f'年化收益(复利,N={N_SLOTS}仓位模拟): {(cumulative_nv - 1) * 100:+.1f}%')
    print(f'最大月亏损: {max_month_loss:+.1f}%')
    print(f'盈亏比: {pl_ratio:.2f}')
    print(f'最大连亏次数: {max_consec_loss}')

    # --- 仓位模拟(N=3, 初始100万) ---
    portfolio_result = simulate_portfolio(all_trades, all_dates, date_idx, by_code)

    # --- 逐笔交易明细(前30笔) ---
    print(f'\n--- 逐笔交易明细(前30笔) ---')
    print(f'{"买入日":>10s} | {"代码":<9s} | {"名称":<6s} | {"买入价":>7s} | {"卖出价":>7s} | {"收益%":>7s} | {"卖出原因"}')
    print('-' * 76)
    for t in all_trades[:30]:
        print(f'{t["buy_date"]:>10s} | {t["code"]:<9s} | {t["name"]:<6s} | '
              f'{t["buy_price"]:>7.2f} | {t["sell_price"]:>7.2f} | '
              f'{t["return_pct"]:>+6.2f}% | {t["reason"]}')

    return {
        'year': year,
        'total_trades': total_trades,
        'win_rate': total_win_rate,
        'avg_return': avg_return,
        'simple_total': total_simple,
        'compound_return': (cumulative_nv - 1) * 100,
        'portfolio_result': portfolio_result,
    }


def simulate_portfolio(all_trades, all_dates, date_idx, by_code):
    """仓位模拟: N=3槽位, 按日期顺序执行"""
    print(f'\n--- 仓位模拟(N={N_SLOTS}, 初始{INIT_CAPITAL/10000:.0f}万) ---')

    # 按买入日期排序
    trades_by_date = defaultdict(list)
    for t in all_trades:
        trades_by_date[t['buy_date']].append(t)

    cash = INIT_CAPITAL
    holdings = []  # [{code, buy_price, buy_date, sell_date, sell_price, return_pct, reason}]
    completed = []
    total_value_history = []

    # 收集所有涉及的交易日并排序
    all_trade_dates = sorted(set(
        [t['buy_date'] for t in all_trades] + [t['sell_date'] for t in all_trades]
    ))

    for cur_date in all_trade_dates:
        # 1. 先处理卖出(释放仓位)
        new_holdings = []
        for h in holdings:
            if h['sell_date'] == cur_date:
                # 卖出: 收回资金
                sell_amount = h['position_value'] * (1 + h['return_pct'] / 100.0)
                cash += sell_amount
                completed.append(h)
            else:
                new_holdings.append(h)
        holdings = new_holdings

        # 2. 处理买入
        if cur_date in trades_by_date:
            for t in trades_by_date[cur_date]:
                if len(holdings) >= N_SLOTS:
                    break  # 满仓跳过
                # 买入金额 = 当前总资产 / N
                total_assets = cash + sum(h['position_value'] for h in holdings)
                buy_amount = total_assets / N_SLOTS
                if buy_amount > cash:
                    buy_amount = cash
                if buy_amount <= 0:
                    continue
                cash -= buy_amount
                holdings.append({
                    'code': t['code'],
                    'name': t['name'],
                    'buy_date': t['buy_date'],
                    'sell_date': t['sell_date'],
                    'buy_price': t['buy_price'],
                    'sell_price': t['sell_price'],
                    'return_pct': t['return_pct'],
                    'reason': t['reason'],
                    'position_value': buy_amount,
                })

    # 处理剩余持仓(理论上不应有, 因为所有trade都有sell_date)
    for h in holdings:
        sell_amount = h['position_value'] * (1 + h['return_pct'] / 100.0)
        cash += sell_amount
        completed.append(h)

    final_value = cash
    annual_return = (final_value / INIT_CAPITAL - 1) * 100.0

    print(f'按日期顺序执行交易，每次买入金额=当前总资产/{N_SLOTS}')
    print(f'最终资产: {final_value/10000:.2f}万')
    print(f'年化收益: {annual_return:+.1f}%')
    print(f'实际完成交易: {len(completed)}笔')

    return {
        'final_value': final_value,
        'annual_return': annual_return,
        'n_completed': len(completed),
    }


def main():
    result_2025 = run_year_validation(TARGET_YEAR)

    if result_2025 is None:
        print('\n[结论] 数据不足或无交易, 无法完成验证')
        return

    # 判断是否达标
    portfolio_annual = result_2025['portfolio_result']['annual_return']
    compound_return = result_2025['compound_return']

    print(f'\n============ 达标判定 ============')
    print(f'仓位模拟年化: {portfolio_annual:+.1f}%')
    print(f'复利估算年化: {compound_return:+.1f}%')

    if portfolio_annual >= 100.0 or compound_return >= 100.0:
        print(f'>>> 达标! 年化>=100%, 执行额外年份对照 <<<')
        # 跑2024和2023
        for extra_year in [2024, 2023]:
            run_year_validation(extra_year)
    else:
        print(f'\n>>> 未达标(年化<100%) <<<')
        print(f'\n--- 分析原因与建议 ---')
        print(f'1. 当前胜率: {result_2025["win_rate"]:.1f}%, 平均收益: {result_2025["avg_return"]:+.2f}%')
        if result_2025['win_rate'] < 55:
            print(f'   - 胜率偏低, 建议收紧筛选条件(如提高涨停日换手率门槛, 缩小open_rate范围)')
        if result_2025['avg_return'] < 1.0:
            print(f'   - 平均收益偏低, 建议调大止盈或优化卖出时点')
        print(f'2. 建议调整方向:')
        print(f'   - 提高涨停日换手率门槛(当前>{PREV_TURN_MIN}%)')
        print(f'   - 缩小open_rate范围(如-3%~+3%)')
        print(f'   - 加入hour1_close_rate过滤(如仅取hour1微涨的)')
        print(f'   - 调整止盈止损比例(当前止损{STOP_LOSS_PCT}%/止盈+{TAKE_PROFIT_PCT}%)')


if __name__ == '__main__':
    main()
