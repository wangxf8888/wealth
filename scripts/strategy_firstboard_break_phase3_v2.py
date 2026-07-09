#!/usr/bin/env python3
"""首板断板增强版 Phase-3a: 5参数组合全年验证

增强条件:
  - hour1_close_rate >= 0%（开盘第一小时收涨企稳）
  - 大盘情绪过滤: 用 T-1日 上证指数 red_ratio (>45% 等)
  - 买入: T日 hour2_open
  - 卖出: T+1 hour1~hour4 逐hour检查 open，命中止损/止盈/h4强平

参数对比:
  V1: h1_cr>=0% + red>45% + 止损-3% + 止盈+6%
  V2: h1_cr>=0% + 无red过滤 + 止损-3% + 止盈+6%
  V3: h1_cr>=0% + red>45% + 止损-4% + 止盈+8%
  V4: h1_cr>=1% + red>45% + 止损-3% + 止盈+6%
  V5: h1_cr>=0% + red>50% + 止损-3% + 止盈+5%

合规:
  - 涨停: round(close/preclose, 2) 严格判定
  - 大盘 red_ratio: 用 T-1 日(盘后已确定), 非 T 日
  - 买/卖价: 仅 hourX_open
  - T+1 卖出: 买入当日不卖

用法:
  python3 scripts/strategy_firstboard_break_phase3_v2.py [year, default=2025]
"""
import sqlite3
import sys
import math
from collections import defaultdict

# ============== 配置区 ==============
DB_PATH = '/home/AIWealth/data/stocks.db'
TARGET_YEAR = int(sys.argv[1]) if len(sys.argv) > 1 else 2025

OPEN_RATE_MIN = -5.0
OPEN_RATE_MAX = 5.0
PREV_TURN_MIN = 15.0
FIRSTBOARD_LOOKBACK = 5

N_SLOTS = 3
INIT_CAPITAL = 1000000.0

# 参数组合 (V1-V5)
PARAM_VERSIONS = [
    {'name': 'V1', 'h1_min': 0.0, 'red_min': 45.0, 'sl': -3.0, 'tp': 6.0},
    {'name': 'V2', 'h1_min': 0.0, 'red_min': None, 'sl': -3.0, 'tp': 6.0},
    {'name': 'V3', 'h1_min': 0.0, 'red_min': 45.0, 'sl': -4.0, 'tp': 8.0},
    {'name': 'V4', 'h1_min': 1.0, 'red_min': 45.0, 'sl': -3.0, 'tp': 6.0},
    {'name': 'V5', 'h1_min': 0.0, 'red_min': 50.0, 'sl': -3.0, 'tp': 5.0},
]
# ===================================


def sv(v):
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
    if code.startswith('sz.30') or code.startswith('sh.68'):
        return 1.20
    return 1.10


def is_limit_up(close, preclose, code):
    pc = sv(preclose)
    cl = sv(close)
    if pc is None or cl is None or pc == 0:
        return False
    return round(cl / pc, 2) >= get_limit_up_threshold(code)


def is_one_word_board(row):
    o = sv(row.get('open'))
    h = sv(row.get('high'))
    l = sv(row.get('low'))
    if None in (o, h, l):
        return False
    return o == h and h == l


def load_year_data(conn, year):
    cur = conn.cursor()
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

    # 加载上证指数 red_ratio
    cur.execute(
        "SELECT date, red_ratio FROM index_kline WHERE code='sh.000001' AND date>=? AND date<? ORDER BY date",
        (dt_lo, dt_hi),
    )
    red_ratio_map = {}
    for d, rr in cur.fetchall():
        red_ratio_map[d] = sv(rr)

    print(f'[加载] {dt_lo} ~ {dt_hi}  交易日={len(all_dates)}  目标日={len(target_dates)}  '
          f'股票数={len(by_code)}  行数={n_rows}  指数日={len(red_ratio_map)}')
    return all_dates, date_idx, target_dates, by_code, red_ratio_map


def find_all_candidates(by_code, all_dates, date_idx, target_date):
    """返回所有满足"基础筛选"的候选股, 含 hour1_close_rate 字段, 后续按版本过滤"""
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
        if code.startswith('bj.'):
            continue
        prev_row = dm.get(prev_date)
        t_row = dm.get(target_date)
        if not prev_row or not t_row:
            continue

        if not is_limit_up(prev_row.get('close'), prev_row.get('preclose'), code):
            continue

        prev_turn = sv(prev_row.get('turn'))
        if prev_turn is None or prev_turn <= PREV_TURN_MIN:
            continue

        if prev_row.get('isST') == 1:
            continue
        name = prev_row.get('code_name') or ''
        if 'ST' in name.upper():
            continue

        is_first_board = True
        for ld in lookback_dates:
            lrow = dm.get(ld)
            if lrow and is_limit_up(lrow.get('close'), lrow.get('preclose'), code):
                is_first_board = False
                break
        if not is_first_board:
            continue

        opr = sv(t_row.get('open_rate'))
        if opr is None or opr < OPEN_RATE_MIN or opr > OPEN_RATE_MAX:
            continue
        if is_one_word_board(t_row):
            continue

        buy_price = sv(t_row.get('hour2_open'))
        if buy_price is None or buy_price <= 0:
            continue

        h1_cr = sv(t_row.get('hour1_close_rate'))
        if h1_cr is None:
            continue

        candidates.append({
            'code': code,
            'name': name,
            'date': target_date,
            'prev_date': prev_date,
            't_idx': t_idx,
            'buy_price': buy_price,
            'open_rate': opr,
            'prev_turn': prev_turn,
            'hour1_close_rate': h1_cr,
        })

    candidates.sort(key=lambda x: x['prev_turn'], reverse=True)
    return candidates


def simulate_sell(by_code, all_dates, code, t_idx, buy_price, sl_pct, tp_pct):
    sl_price = buy_price * (1 + sl_pct / 100.0)
    tp_price = buy_price * (1 + tp_pct / 100.0)

    t1_idx = t_idx + 1
    if t1_idx >= len(all_dates):
        return None, None, None, None
    t1_date = all_dates[t1_idx]
    t1_row = by_code.get(code, {}).get(t1_date)
    if not t1_row:
        return None, None, None, None

    for hour in (1, 2, 3, 4):
        p = sv(t1_row.get(f'hour{hour}_open'))
        if p is None:
            continue
        if p <= sl_price:
            return p, '止损', t1_date, hour
        if p >= tp_price:
            return p, '止盈', t1_date, hour
        if hour == 4:
            return p, '强平', t1_date, hour

    return None, None, None, None


def run_version(version, base_candidates_by_date, by_code, all_dates, red_ratio_map):
    """对单个参数版本跑回测, 返回 trades 列表 + monthly_stats"""
    h1_min = version['h1_min']
    red_min = version['red_min']
    sl = version['sl']
    tp = version['tp']

    all_trades = []
    monthly_stats = defaultdict(lambda: {'candidates': 0, 'trades': 0, 'returns': []})

    for td, base_cands in base_candidates_by_date.items():
        month_key = td[:7]

        # 大盘情绪过滤: 用 T-1 日 red_ratio
        if red_min is not None and base_cands:
            prev_d = base_cands[0]['prev_date']
            rr = red_ratio_map.get(prev_d)
            if rr is None or rr <= red_min:
                continue  # 当天禁止交易

        # 按版本过滤 hour1_close_rate
        cands = [c for c in base_cands if c['hour1_close_rate'] >= h1_min]
        monthly_stats[month_key]['candidates'] += len(cands)

        for cand in cands:
            sell_price, reason, sell_date, sell_hour = simulate_sell(
                by_code, all_dates, cand['code'], cand['t_idx'], cand['buy_price'], sl, tp
            )
            if sell_price is None:
                continue
            ret_pct = (sell_price - cand['buy_price']) / cand['buy_price'] * 100.0
            all_trades.append({
                'buy_date': td,
                'sell_date': sell_date,
                'code': cand['code'],
                'name': cand['name'],
                'buy_price': cand['buy_price'],
                'sell_price': sell_price,
                'return_pct': ret_pct,
                'reason': reason,
                'prev_turn': cand['prev_turn'],
                'h1_cr': cand['hour1_close_rate'],
                'sell_hour': sell_hour,
            })
            monthly_stats[month_key]['trades'] += 1
            monthly_stats[month_key]['returns'].append(ret_pct)

    return all_trades, monthly_stats


def summarize_trades(all_trades, monthly_stats):
    n = len(all_trades)
    if n == 0:
        return {
            'n': 0, 'win_rate': 0.0, 'avg': 0.0, 'compound': 0.0,
            'max_month_loss': 0.0, 'pl_ratio': 0.0,
        }
    rets = [t['return_pct'] for t in all_trades]
    wins = sum(1 for r in rets if r > 0)
    win_rate = wins / n * 100.0
    avg = sum(rets) / n

    # 年化复利模拟 (每笔 1/N 仓位)
    cumulative_nv = 1.0
    months_sorted = sorted(monthly_stats.keys())
    for m in months_sorted:
        for r in monthly_stats[m]['returns']:
            cumulative_nv *= (1 + r / 100.0 / N_SLOTS)
    compound = (cumulative_nv - 1) * 100.0

    # 最大月亏损
    max_month_loss = 0.0
    for m in months_sorted:
        rs = monthly_stats[m]['returns']
        if rs:
            ms = sum(rs)
            if ms < max_month_loss:
                max_month_loss = ms

    # 盈亏比
    win_rs = [r for r in rets if r > 0]
    loss_rs = [r for r in rets if r <= 0]
    avg_win = sum(win_rs) / len(win_rs) if win_rs else 0.0
    avg_loss = sum(loss_rs) / len(loss_rs) if loss_rs else 0.0
    pl_ratio = avg_win / abs(avg_loss) if avg_loss != 0 else float('inf')

    return {
        'n': n, 'win_rate': win_rate, 'avg': avg,
        'compound': compound, 'max_month_loss': max_month_loss,
        'pl_ratio': pl_ratio, 'cumulative_nv': cumulative_nv,
    }


def simulate_portfolio(all_trades):
    trades_by_date = defaultdict(list)
    for t in all_trades:
        trades_by_date[t['buy_date']].append(t)

    cash = INIT_CAPITAL
    holdings = []
    completed = []

    all_trade_dates = sorted(set(
        [t['buy_date'] for t in all_trades] + [t['sell_date'] for t in all_trades]
    ))

    for cur_date in all_trade_dates:
        new_holdings = []
        for h in holdings:
            if h['sell_date'] == cur_date:
                sell_amount = h['position_value'] * (1 + h['return_pct'] / 100.0)
                cash += sell_amount
                completed.append(h)
            else:
                new_holdings.append(h)
        holdings = new_holdings

        if cur_date in trades_by_date:
            for t in trades_by_date[cur_date]:
                if len(holdings) >= N_SLOTS:
                    break
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

    for h in holdings:
        sell_amount = h['position_value'] * (1 + h['return_pct'] / 100.0)
        cash += sell_amount
        completed.append(h)

    final_value = cash
    annual_return = (final_value / INIT_CAPITAL - 1) * 100.0

    return {
        'final_value': final_value,
        'annual_return': annual_return,
        'n_completed': len(completed),
    }


def run_year(year):
    print(f'\n============ 首板断板增强版 Phase-3a 全年验证 ============')
    print(f'年份: {year}')

    conn = sqlite3.connect(DB_PATH)
    all_dates, date_idx, target_dates, by_code, red_ratio_map = load_year_data(conn, year)
    conn.close()

    if not target_dates:
        print(f'[错误] {year}年无交易日数据')
        return None

    # 一次性预筛全年基础候选股 (按日期)
    print('[预筛] 计算每日基础候选股...')
    base_candidates_by_date = {}
    total_base = 0
    for td in target_dates:
        cands = find_all_candidates(by_code, all_dates, date_idx, td)
        base_candidates_by_date[td] = cands
        total_base += len(cands)
    print(f'[预筛] 完成, 全年基础候选股累计 {total_base} 笔')

    # --- 跑 5 个版本 ---
    results = []
    print(f'\n--- 参数组合对比 ---')
    print(f'{"版本":<4s} | {"h1_cr":>7s} | {"red>":>5s} | {"止损":>5s} | {"止盈":>5s} | '
          f'{"交易数":>5s} | {"胜率":>6s} | {"均收益":>8s} | {"年化":>8s} | {"最大月亏":>8s} | {"盈亏比":>6s}')
    print('-' * 105)

    for v in PARAM_VERSIONS:
        trades, mstats = run_version(v, base_candidates_by_date, by_code, all_dates, red_ratio_map)
        s = summarize_trades(trades, mstats)
        port = simulate_portfolio(trades) if trades else {'annual_return': 0.0, 'final_value': INIT_CAPITAL}

        red_str = f'{v["red_min"]:.0f}%' if v['red_min'] is not None else '无'
        pl_str = f'{s["pl_ratio"]:.2f}' if s['pl_ratio'] != float('inf') else 'inf'
        print(f'{v["name"]:<4s} | {v["h1_min"]:>+6.1f}% | {red_str:>5s} | '
              f'{v["sl"]:>+4.1f}% | {v["tp"]:>+4.1f}% | '
              f'{s["n"]:>5d} | {s["win_rate"]:>5.1f}% | {s["avg"]:>+7.2f}% | '
              f'{port["annual_return"]:>+7.1f}% | {s["max_month_loss"]:>+7.1f}% | {pl_str:>6s}')

        results.append({
            'version': v,
            'trades': trades,
            'monthly_stats': mstats,
            'summary': s,
            'portfolio': port,
        })

    # --- 选择最优版本(按 portfolio annual_return) ---
    results.sort(key=lambda r: r['portfolio']['annual_return'], reverse=True)
    best = results[0]
    print(f'\n>>> 最优版本: {best["version"]["name"]}  '
          f'年化={best["portfolio"]["annual_return"]:+.1f}%  '
          f'交易数={best["summary"]["n"]}  胜率={best["summary"]["win_rate"]:.1f}% <<<')

    # --- 最优版本月度明细 ---
    print(f'\n--- 最优版本({best["version"]["name"]}) 月度明细 ---')
    print(f'{"月份":<8s} | {"候选数":>5s} | {"交易数":>5s} | {"胜率":>6s} | {"均收益":>8s} | '
          f'{"月累计":>8s} | {"累计净值":>8s}')
    print('-' * 72)

    cumulative_nv = 1.0
    months_sorted = sorted(best['monthly_stats'].keys())
    for m in months_sorted:
        ms = best['monthly_stats'][m]
        rets = ms['returns']
        n_trades = ms['trades']
        n_cands = ms['candidates']
        if n_trades > 0:
            wins = sum(1 for r in rets if r > 0)
            win_rate = wins / n_trades * 100.0
            avg_ret = sum(rets) / n_trades
            month_total = sum(rets)
            for r in rets:
                cumulative_nv *= (1 + r / 100.0 / N_SLOTS)
        else:
            win_rate = 0.0
            avg_ret = 0.0
            month_total = 0.0
        print(f'{m:<8s} | {n_cands:>5d} | {n_trades:>5d} | {win_rate:>5.1f}% | '
              f'{avg_ret:>+7.2f}% | {month_total:>+7.1f}% | {cumulative_nv:>7.3f}')

    # --- 仓位模拟详细 ---
    port = best['portfolio']
    print(f'\n--- 仓位模拟(N={N_SLOTS}, 初始{INIT_CAPITAL/10000:.0f}万, 最优={best["version"]["name"]}) ---')
    print(f'最终资产: {port["final_value"]/10000:.2f}万')
    print(f'年化收益: {port["annual_return"]:+.1f}%')
    print(f'实际完成交易: {port["n_completed"]}笔')

    # --- 逐笔明细(前30) ---
    print(f'\n--- 逐笔交易明细(最优版本前30笔) ---')
    print(f'{"买入日":>10s} | {"代码":<9s} | {"名称":<8s} | {"h1_cr":>6s} | {"买入价":>7s} | '
          f'{"卖出价":>7s} | {"收益%":>7s} | {"原因"}')
    print('-' * 90)
    for t in best['trades'][:30]:
        print(f'{t["buy_date"]:>10s} | {t["code"]:<9s} | {t["name"][:8]:<8s} | '
              f'{t["h1_cr"]:>+5.2f}% | {t["buy_price"]:>7.2f} | {t["sell_price"]:>7.2f} | '
              f'{t["return_pct"]:>+6.2f}% | {t["reason"]}')

    return {
        'year': year,
        'results': results,
        'best': best,
    }


def main():
    main_result = run_year(TARGET_YEAR)
    if main_result is None:
        print('\n[结论] 数据不足或无交易, 无法完成验证')
        return

    best = main_result['best']
    best_annual = best['portfolio']['annual_return']

    print(f'\n============ 达标判定 ============')
    print(f'最优版本: {best["version"]["name"]}  年化: {best_annual:+.1f}%')

    if best_annual >= 100.0:
        print(f'>>> 达标! 最优年化>=100%, 跑 2024/2023 验证 <<<')
        for extra_year in [2024, 2023]:
            run_year(extra_year)
    else:
        print(f'\n>>> 未达标(年化<100%) <<<')
        print(f'\n--- 分析与建议 ---')
        # 简单分析
        all_versions = main_result['results']
        n_pos = sum(1 for r in all_versions if r['portfolio']['annual_return'] > 0)
        print(f'1. {len(all_versions)} 个版本中 {n_pos} 个为正收益')
        print(f'2. 最优版本 {best["version"]["name"]} 胜率={best["summary"]["win_rate"]:.1f}%, '
              f'均收益={best["summary"]["avg"]:+.2f}%')
        if best['summary']['win_rate'] < 50:
            print(f'   - 胜率仍低, 说明首板断板低吸在 T+1 hour 跳空风险大')
            print(f'   - 建议: 进一步收紧 hour1_close_rate(>=2%), 或叠加涨停日尾盘强势条件')
        if best['summary']['avg'] < 0:
            print(f'   - 均收益为负, 跳空回落难以止损保护')
            print(f'   - 建议: 改为 T 日 hour3 或 hour4 买入(进一步观察日内走势)')
        print(f'3. red_ratio 过滤效果对比:')
        for r in main_result['results']:
            v = r['version']
            print(f'   - {v["name"]} (red>{v["red_min"]}, h1>={v["h1_min"]}%, sl={v["sl"]}, tp={v["tp"]}): '
                  f'年化{r["portfolio"]["annual_return"]:+.1f}%, 胜率{r["summary"]["win_rate"]:.1f}%, '
                  f'交易{r["summary"]["n"]}笔')


if __name__ == '__main__':
    main()
