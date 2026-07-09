#!/usr/bin/env python3
"""离线组合模拟器：从four_v3_trades.json复现NAV，快速测试仓位数/策略删减/优先级配置。
注意：只能"减少"交易(删策略/减仓位)，无法模拟被挤出的新候选，故对"减法"配置为有效下界估计。"""
import json, re, math
from collections import defaultdict

TRADES = '/home/AIWealth/frontend/four_v3_trades.json'


def load_trades():
    d = json.load(open(TRADES))
    out = []
    for t in d:
        m = re.search(r'优先级(\d+)', t.get('buy_reason', ''))
        prio = int(m.group(1)) if m else 9
        out.append({
            'code': t['code'],
            'strategy': t['strategy'],
            'buy_date': t['buy_date'],
            'sell_date': t['sell_date'],
            'ret': t['return_pct'] / 100.0,
            'prio': prio,
        })
    return out


def simulate(trades, n_slots, allowed_strategies=None, prio_override=None,
             init=1_000_000, cost=0.001):
    """按时间顺序回放，共享资金池，每笔=NAV/n_slots(不超过现金)。
    返回 (总收益率%, CAGR%, 最大回撤%, 成交笔数, 胜率%)"""
    ts = [t for t in trades if (allowed_strategies is None or t['strategy'] in allowed_strategies)]
    # 优先级覆盖
    if prio_override:
        for t in ts:
            t = t
    # 事件：按日期排序，买入事件与卖出事件
    # 收集所有交易日
    all_dates = sorted(set([t['buy_date'] for t in ts] + [t['sell_date'] for t in ts]))
    # 按买入日分组
    buys_by_date = defaultdict(list)
    for t in ts:
        buys_by_date[t['buy_date']].append(t)
    # 排序：优先级(小优先) -> 若override用strategy序
    def sort_key(t):
        p = prio_override.get(t['strategy'], t['prio']) if prio_override else t['prio']
        return (p, -t['ret'])  # 同优先级下这里无法预知，用ret占位(仅影响谁先占slot)
    sells_by_date = defaultdict(list)
    for t in ts:
        sells_by_date[t['sell_date']].append(t)

    cash = init
    nav = init
    slots = []  # list of dicts: {shares_value_at_buy, ret, sell_date, invested}
    # 用持仓字典：key=id
    holdings = []  # {invested, ret, sell_date}
    peak = init
    max_dd = 0.0
    nav_series = []
    wins = 0
    done = 0

    for dt in all_dates:
        # 先卖出（当日到期）
        still = []
        for h in holdings:
            if h['sell_date'] == dt:
                proceeds = h['invested'] * (1 + h['ret']) * (1 - cost)
                cash += proceeds
                if h['ret'] > 0:
                    wins += 1
                done += 1
            else:
                still.append(h)
        holdings = still
        # 再买入
        todays = sorted(buys_by_date.get(dt, []), key=sort_key)
        for t in todays:
            if len(holdings) >= n_slots:
                break
            # 当前NAV = cash + 持仓市值(按买入价估，简化为invested)
            cur_nav = cash + sum(h['invested'] for h in holdings)
            target = cur_nav / n_slots
            invest = min(target, cash)
            if invest < 1000:
                continue
            invest_net = invest * (1 - cost)  # 买入成本
            cash -= invest
            holdings.append({'invested': invest_net, 'ret': t['ret'], 'sell_date': t['sell_date']})
        # 记录NAV
        nav = cash + sum(h['invested'] * (1 + h['ret']) for h in holdings)
        # 近似：用最终ret估市值(高估波动，DD仅供参考)
        peak = max(peak, nav)
        dd = (peak - nav) / peak
        max_dd = max(max_dd, dd)
        nav_series.append(nav)

    final = cash + sum(h['invested'] * (1 + h['ret']) for h in holdings)
    total_ret = (final / init - 1) * 100
    years = 5.5
    cagr = ((final / init) ** (1 / years) - 1) * 100
    wr = wins / done * 100 if done else 0
    return total_ret, cagr, max_dd * 100, done, wr


def main():
    trades = load_trades()
    ALL = {'dragon_pullback', 'shrink_reversal', 'limitdown_rebound', 'bigdrop_gapup'}

    print(f"{'配置':40s} {'总收益%':>9s} {'CAGR%':>7s} {'笔数':>5s} {'胜率%':>6s}")
    print('-' * 75)

    def run(label, n, strat=None, prio=None):
        tr, cg, dd, n2, wr = simulate(trades, n, strat, prio)
        print(f"{label:40s} {tr:9.1f} {cg:7.2f} {n2:5d} {wr:6.1f}")

    run('基准 5仓 全策略', 5, ALL)
    run('4仓 全策略', 4, ALL)
    run('3仓 全策略', 3, ALL)
    run('2仓 全策略', 2, ALL)
    print('-' * 75)
    run('5仓 去bigdrop', 5, ALL - {'bigdrop_gapup'})
    run('3仓 去bigdrop', 3, ALL - {'bigdrop_gapup'})
    run('3仓 去bigdrop+去limitdown', 3, {'dragon_pullback', 'shrink_reversal'})
    run('2仓 dragon+shrink', 2, {'dragon_pullback', 'shrink_reversal'})
    print('-' * 75)
    # 优先级重排：dragon优先
    prio_new = {'dragon_pullback': 1, 'shrink_reversal': 2, 'limitdown_rebound': 3, 'bigdrop_gapup': 4}
    run('5仓 dragon优先重排', 5, ALL, prio_new)
    run('3仓 dragon优先重排', 3, ALL, prio_new)
    run('3仓 dragon优先+去bigdrop', 3, ALL - {'bigdrop_gapup'}, prio_new)


if __name__ == '__main__':
    main()
