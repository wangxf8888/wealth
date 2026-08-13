#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
回测vs实盘逐笔对照 (Task#5)
====================================================================
逻辑:
  读最近一次rolling产出的trades(生产口径=冰点overlay块) + data/realtime/下的
  decision_*.json(9:25决策留痕) + positions.json(账本), 对最近5个交易日:
    ① 买入信号一致性 (引擎当日买什么 vs 实盘9:25买什么, 按code+date配对)
    ② 买入价偏差 (引擎hour1_open成交价 vs 实盘9:25快照价)
    ③ 卖出时点/价格/原因一致性 (按code+buy_date配对已平仓单)
    ④ 日级NAV走势偏差 (实盘侧用"初始资金+累计已实现盈亏"近似, 引擎侧daily_nav)
  每笔差异归因到分类之一:
    data_source_diff / timing_diff / overlay_diff / limit_or_t1_diff /
    param_version_diff / unexplained
  结构性可解释差(实盘V-C动态槽/冰点减仓/回撤三档/9:25快照价 vs 回测hour1_open)
  分开统计; 仅对 unexplained 或 单笔价差>0.5% 追加 scheduler_alerts 告警。

输出: logs/rolling/bt_live_divergence_YYYYMMDD.json + .md
实盘启动日 2026-07-22, 之前无实盘数据 → 优雅降级为"无可对照日"。

Task#47增强(2026-08-03, 修复Task#36发现的两个对照口径盲区):
  A. 未平仓对照层: 引擎trades仅含已平仓单, 窗口末实盘持仓永远无法配对 →
     对每个实盘holding两级探测: (a)引擎已平仓trades同code同buy_date配对;
     (b)否则信号级复核(直调策略get_candidates), 标记
     open_position_signal_match/mismatch, 不再一律落"数据源差"。
  B. 信号级对照层: 对窗口内实盘每笔买入直调对应策略get_candidates(buy_date)
     核对是否在候选列表及排位(signal_check函数, Task#36 revalidate方法固化)。
  C. 归因精确化: hour覆盖归因按"该股该日hour是否有值"逐股判定(hour_available),
     替代按日全市场<50%粗归因(7/21-7/24全市场覆盖实为99.9%, 粗口径曾误归因)。
  D. 三层重合率: 成交级(已平仓trades严格code+buy_date配对)/持仓级/信号级,
     md新增小节, 并追加 logs/rolling/coherence_trend.csv 逐日趋势。

红线: 对 data/realtime/ 全部只读; 只写 logs/rolling/ 与 scheduler_alerts.log(追加)。

CLI:
  python3 tools/backtest_vs_live_compare.py                      # 自动取最新rolling json
  python3 tools/backtest_vs_live_compare.py --rolling-json <path> [--no-alert]
====================================================================
"""
import argparse
import glob
import json
import os
from datetime import datetime

# ================= 配置区 =================
BASE_DIR = '/home/AIWealth'
ROLLING_LOG_DIR = os.path.join(BASE_DIR, 'logs', 'rolling')
REALTIME_DIR = os.path.join(BASE_DIR, 'data', 'realtime')   # 只读!
POSITIONS_PATH = os.path.join(REALTIME_DIR, 'positions.json')
ALERT_LOG = os.path.join(BASE_DIR, 'logs', 'realtime', 'scheduler_alerts.log')

LIVE_START_DATE = '2026-07-22'     # 实盘启动日, 之前无实盘数据
PRICE_DIFF_ALERT_PCT = 0.5         # 单笔价差告警阈值(%)
HOUR_COVERAGE_LOW_PCT = 50         # (保留)全市场日覆盖参考; 归因已改逐股精确判定
DB_PATH = os.path.join(BASE_DIR, 'data', 'stocks.db')       # 只读: 逐股hour判定
COHERENCE_CSV = os.path.join(ROLLING_LOG_DIR, 'coherence_trend.csv')  # T47三层趋势

# 归因分类 (报告固定枚举)
CATEGORIES = [
    'data_source_diff',    # 数据源差(如BaoStock被ban日hour列缺失/腾讯降级)
    'timing_diff',         # 时点差(9:25快照价 vs hour1_open; 引擎trades仅含已平仓)
    'overlay_diff',        # overlay差(冰点减仓/回撤三档/修复日/V-C动态槽)
    'limit_or_t1_diff',    # 涨跌停或T+1拦截差
    'param_version_diff',  # 参数版本差(策略参数迁移期)
    'unexplained',         # 不可解释 → 告警
]
EXPLAINABLE = set(CATEGORIES) - {'unexplained'}
# ================= 配置区结束 =================


def now_str():
    return datetime.now().strftime('%Y-%m-%d %H:%M:%S')


def append_alert(msg: str):
    line = f"[{now_str()}] [BT_LIVE_DIVERGENCE] ⚠️ {msg}\n"
    try:
        with open(ALERT_LOG, 'a', encoding='utf-8') as f:
            f.write(line)
        print(f"[告警] {line.strip()}")
    except Exception as e:
        print(f"[警告] 写告警日志失败: {e}")


def load_json(path):
    try:
        with open(path, 'r', encoding='utf-8') as f:
            return json.load(f)
    except Exception:
        return None


def find_latest_rolling():
    """取logs/rolling/下最新rolling_*.json (mtime倒序)。"""
    cands = glob.glob(os.path.join(ROLLING_LOG_DIR, 'rolling_*.json'))
    if not cands:
        return None
    return max(cands, key=os.path.getmtime)


def pct_diff(a, b):
    """相对偏差(%), 以b(实盘)为基准。"""
    if not a or not b:
        return None
    return round((a - b) / b * 100, 3)


# ================= Task#47: 信号级复核与逐股hour判定 =================
_HOUR_CONN = None          # 只读sqlite连接(懒加载)
_HOUR_CACHE = {}           # (code,date) -> bool
_STRATEGY_CACHE = {}       # 模块名 -> 策略实例
_DATA_FEED = None          # BacktestDataFeed单例(懒加载, 仅信号级复核用)


def hour_available(code, date):
    """该股该日hour列是否有值(逐股精确判定, 替代按日全市场<50%粗归因)。"""
    global _HOUR_CONN
    key = (code, date)
    if key in _HOUR_CACHE:
        return _HOUR_CACHE[key]
    try:
        if _HOUR_CONN is None:
            import sqlite3
            _HOUR_CONN = sqlite3.connect(f'file:{DB_PATH}?mode=ro', uri=True)
        row = _HOUR_CONN.execute(
            "SELECT hour1_close IS NOT NULL FROM stock_kline "
            "WHERE code=? AND date=?", (code, date)).fetchone()
        ok = bool(row and row[0])
    except Exception:
        ok = True   # DB不可读时不做hour归因(宁可走信号级复核)
    _HOUR_CACHE[key] = ok
    return ok


def _get_strategy(module_name):
    """按模块名动态发现strategies/下唯一Strategy子类并实例化(缓存)。"""
    if module_name in _STRATEGY_CACHE:
        return _STRATEGY_CACHE[module_name]
    import importlib
    import inspect
    import sys
    if BASE_DIR not in sys.path:
        sys.path.insert(0, BASE_DIR)
    from strategies.base import Strategy
    mod = importlib.import_module(f'strategies.{module_name}')
    classes = [c for _, c in inspect.getmembers(mod, inspect.isclass)
               if issubclass(c, Strategy) and c is not Strategy
               and c.__module__ == mod.__name__]
    inst = classes[0]() if classes else None
    _STRATEGY_CACHE[module_name] = inst
    return inst


def signal_check(code, buy_date, strategy_name):
    """信号级复核(Task#36方法固化): 直调策略get_candidates(buy_date),
    返回 {'checked','in_candidates','rank','n_candidates','error'}。
    只读引擎数据层, 不跑回测。"""
    global _DATA_FEED
    out = {'checked': False, 'in_candidates': False, 'rank': None,
           'n_candidates': None, 'error': None}
    if not strategy_name:
        out['error'] = 'no_strategy_name'
        return out
    try:
        import sys
        if BASE_DIR not in sys.path:
            sys.path.insert(0, BASE_DIR)
        if _DATA_FEED is None:
            from backtest.data_feed import BacktestDataFeed
            _DATA_FEED = BacktestDataFeed(DB_PATH)
        strat = _get_strategy(strategy_name)
        if strat is None:
            out['error'] = f'strategy_not_found:{strategy_name}'
            return out
        cands = strat.get_candidates(buy_date, _DATA_FEED)
        codes = []
        for c in (cands or []):
            if isinstance(c, str):
                codes.append(c)
            elif isinstance(c, dict):
                codes.append(c.get('code'))
            else:
                codes.append(getattr(c, 'code', None))
        out['checked'] = True
        out['n_candidates'] = len(codes)
        if code in codes:
            out['in_candidates'] = True
            out['rank'] = codes.index(code) + 1
    except Exception as e:
        out['error'] = f'{type(e).__name__}: {e}'
    return out


def compare_open_positions(rolling, positions, live_days):
    """T47-A 未平仓对照层: 实盘每个holding两级探测——
    (a) 引擎已平仓trades同code同buy_date → matched_closed_trade(引擎已先离场);
    (b) 否则信号级复核 → open_position_signal_match / mismatch。"""
    ice = rolling.get('ice_overlay') or {}
    eng = {(t['code'], t['buy_date']): t for t in ice.get('recent_trades', [])}
    probes = []
    for p in positions.get('positions', []):
        if p.get('status') != 'holding':
            continue
        code, bdate = p.get('code'), p.get('buy_date')
        probe = {'code': code, 'buy_date': bdate,
                 'strategy': p.get('strategy'),
                 'in_window': bdate in live_days,
                 'hour_available': hour_available(code, bdate)}
        et = eng.get((code, bdate))
        if et is not None:
            probe['result'] = 'matched_closed_trade'
            probe['engine_sell'] = {'date': et['sell_date'],
                                    'reason': et['reason']}
        else:
            sc = signal_check(code, bdate, p.get('strategy'))
            probe['signal_check'] = sc
            probe['result'] = ('open_position_signal_match'
                               if sc['in_candidates']
                               else 'open_position_signal_mismatch')
        probes.append(probe)
    return probes


def three_layer_rates(live_buys, eng_buys, open_probes, signal_results):
    """T47-D 三层重合率: 成交级(严格code+buy_date配对) / 持仓级 / 信号级。"""
    n_buys = len(live_buys)
    n_trade = sum(1 for k in live_buys if k in eng_buys)
    n_pos = len(open_probes)
    n_pos_ok = sum(1 for pr in open_probes
                   if pr['result'] in ('matched_closed_trade',
                                       'open_position_signal_match'))
    n_sig = len(signal_results)
    n_sig_ok = sum(1 for r in signal_results
                   if r['signal_check'].get('in_candidates'))

    def _rate(ok, total):
        return round(ok / total * 100, 1) if total else None
    return {
        'trade_level': {'matched': n_trade, 'total': n_buys,
                        'rate_pct': _rate(n_trade, n_buys)},
        'position_level': {'matched': n_pos_ok, 'total': n_pos,
                           'rate_pct': _rate(n_pos_ok, n_pos)},
        'signal_level': {'matched': n_sig_ok, 'total': n_sig,
                         'rate_pct': _rate(n_sig_ok, n_sig)},
    }


def append_trend_csv(report_date, rates):
    """追加三层重合率到coherence_trend.csv(同日重跑覆盖旧行, 防重复污染趋势)。"""
    header = 'date,trade_level_pct,position_level_pct,signal_level_pct'

    def _v(layer):
        r = rates[layer]['rate_pct']
        return '' if r is None else f'{r}'
    newline = (f"{report_date},{_v('trade_level')},"
               f"{_v('position_level')},{_v('signal_level')}")
    rows = []
    if os.path.exists(COHERENCE_CSV):
        with open(COHERENCE_CSV, 'r', encoding='utf-8') as f:
            rows = [ln.rstrip('\n') for ln in f if ln.strip()]
    if not rows or rows[0] != header:
        rows = [header] + [r for r in rows if r != header]
    rows = [r for r in rows if not r.startswith(report_date + ',')]
    rows.append(newline)
    with open(COHERENCE_CSV, 'w', encoding='utf-8') as f:
        f.write('\n'.join(rows) + '\n')
    return newline
# ================= Task#47 新增结束 =================


def classify_buy_missing_live(date, code, decision, coverage):
    """引擎当日有买入, 实盘没买 → 归因。"""
    if decision is None:
        return ('data_source_diff', f'{date} 无decision留痕文件')
    ps = decision.get('position_scale', {})
    if ps.get('halt'):
        return ('overlay_diff', '实盘当日halt(回撤熔断/修复日), 引擎无此overlay')
    if ps.get('final_scale', 1.0) < 1.0:
        return ('overlay_diff',
                f"实盘final_scale={ps.get('final_scale')}(冰点/回撤减仓), 可能缩量到未成交")
    # T47-C: 逐股hour精确判定(替代按日全市场<50%粗归因)
    if not hour_available(code, date):
        return ('data_source_diff',
                f'{code}该日hour列缺失, 实盘候选生成时无法复现引擎信号')
    # 该code在decision中是否被列为候选但未推荐(V-C槽/排序截断)
    return (None, None)


def match_and_compare(rolling, live_days, decisions, positions):
    """核心对照: 返回(diffs, buy_pairs, sell_pairs, stats,
    live_buys, eng_buys, signal_results)。"""
    ice = rolling.get('ice_overlay') or {}
    engine_trades = ice.get('recent_trades', [])
    coverage = rolling.get('hour_coverage', {})

    # 实盘买入: holdings + closed, buy_date在窗口内
    live_buys = {}
    for p in (positions.get('positions', []) + positions.get('closed_trades', [])):
        if p.get('buy_date') in live_days:
            live_buys[(p['code'], p['buy_date'])] = p
    # 实盘卖出(已平仓)
    live_sells = {}
    for p in positions.get('closed_trades', []):
        if p.get('sell_date') in live_days or p.get('buy_date') in live_days:
            live_sells[(p['code'], p['buy_date'])] = p

    # 引擎买入/卖出(注意: 引擎trades仅含已平仓单, 期末仍持仓的买入不在其中)
    eng_buys = {}
    eng_sells = {}
    for t in engine_trades:
        if t['buy_date'] in live_days:
            eng_buys[(t['code'], t['buy_date'])] = t
        if t['sell_date'] in live_days:
            eng_sells[(t['code'], t['buy_date'])] = t

    diffs = []          # 每笔差异: {type, date, code, detail, category, reason}
    buy_pairs = []      # 配对成功的买入
    sell_pairs = []     # 配对成功的卖出

    def add_diff(dtype, date, code, detail, category, reason):
        diffs.append({'type': dtype, 'date': date, 'code': code,
                      'detail': detail, 'category': category, 'reason': reason})

    # ① 买入信号一致性 + ② 买入价偏差 (+ T47-B 信号级复核)
    signal_results = []      # 窗口内实盘每笔买入的信号级复核结果
    for key, lt in live_buys.items():
        code, date = key
        sc = signal_check(code, date, lt.get('strategy'))
        signal_results.append({'code': code, 'buy_date': date,
                               'strategy': lt.get('strategy'),
                               'status': lt.get('status'),
                               'signal_check': sc})
        et = eng_buys.get(key)
        if et is None:
            # T47: 归因精确化——先逐股hour判定, 再看信号级复核结果,
            # 不再按日全市场覆盖率一律落"数据源差"
            if not hour_available(code, date):
                cat, reason = 'data_source_diff', \
                    f'{code}该日hour列缺失, 引擎无法复现该信号(逐股精确判定)'
            elif sc['in_candidates']:
                cat, reason = 'timing_diff', \
                    (f"信号级复现✅(候选排位{sc['rank']}/{sc['n_candidates']}), "
                     '引擎trades仅含已平仓单: 期末未平仓不可见或该单被slot时序错开')
            elif sc['checked']:
                cat, reason = 'param_version_diff', \
                    (f"信号级不复现(引擎候选{sc['n_candidates']}只无此票), "
                     '候选生成与引擎信号面差异(需复核参数版本/数据面)')
            else:
                cat, reason = 'unexplained', \
                    f"信号级复核失败({sc['error']}), 无法归因"
            add_diff('buy_signal', date, code,
                     f"实盘买入@{lt.get('buy_price')} ({lt.get('strategy')}), "
                     f"引擎已平仓trades无此单",
                     cat, reason)
            continue
        pd = pct_diff(et['buy_price'], lt.get('buy_price'))
        buy_pairs.append({'code': code, 'date': date,
                          'engine_price': et['buy_price'],
                          'live_price': lt.get('buy_price'),
                          'price_diff_pct': pd,
                          'strategy': lt.get('strategy')})
        if pd is not None and abs(pd) > 1e-9:
            add_diff('buy_price', date, code,
                     f"引擎{et['buy_price']} vs 实盘{lt.get('buy_price')} "
                     f"偏差{pd:+.3f}%",
                     'timing_diff',
                     '结构性: 引擎hour1_open成交 vs 实盘9:25快照/竞价价')

    for key, et in eng_buys.items():
        code, date = key
        if key in live_buys:
            continue
        decision = decisions.get(date)
        cat, reason = classify_buy_missing_live(date, code, decision, coverage)
        if cat is None:
            # 查decision: 引擎买的票是否在实盘决策候选里
            in_dec, recommended = _search_decision(decision, code)
            if in_dec and not recommended:
                cat, reason = 'overlay_diff', \
                    '实盘决策中为候选但未被推荐(V-C动态槽/每策略Top1截断)'
            elif not in_dec:
                cat, reason = 'data_source_diff', \
                    '实盘前晚候选池无此票(候选生成与引擎信号数据面不一致)'
            else:
                cat, reason = 'limit_or_t1_diff', \
                    '实盘已推荐但未成交, 疑似涨跌停/一字/资金占用拦截(需人工复核)'
                if cat not in EXPLAINABLE:
                    cat, reason = 'unexplained', '已推荐未成交且无拦截痕迹'
        add_diff('buy_signal', date, code,
                 f"引擎买入@{et['buy_price']} ({et['strategy_name']}), 实盘未买",
                 cat, reason)

    # ③ 卖出时点/价格/原因一致性
    for key, lt in live_sells.items():
        code, bdate = key
        et = eng_sells.get(key) or eng_buys.get(key)
        if et is None:
            continue  # 买入侧未配对的已在①记录
        same_day = (et['sell_date'] == lt.get('sell_date'))
        pd = pct_diff(et['sell_price'], lt.get('sell_price'))
        pair = {'code': code, 'buy_date': bdate,
                'engine_sell': {'date': et['sell_date'], 'hour': et['sell_hour'],
                                'price': et['sell_price'], 'reason': et['reason']},
                'live_sell': {'date': lt.get('sell_date'),
                              'price': lt.get('sell_price'),
                              'reason': lt.get('sell_reason')},
                'same_day': same_day, 'price_diff_pct': pd}
        sell_pairs.append(pair)
        if not same_day:
            add_diff('sell_timing', lt.get('sell_date'), code,
                     f"引擎卖于{et['sell_date']}H{et['sell_hour']} vs "
                     f"实盘{lt.get('sell_date')}", 'timing_diff',
                     '触线判定粒度差: 引擎hour/5min bar vs 实盘10秒轮询')
        if pd is not None and abs(pd) > 1e-9:
            reason_same = _reason_equiv(et['reason'], lt.get('sell_reason'))
            add_diff('sell_price', lt.get('sell_date'), code,
                     f"引擎{et['sell_price']}({et['reason']}) vs "
                     f"实盘{lt.get('sell_price')}({lt.get('sell_reason')}) "
                     f"偏差{pd:+.3f}%",
                     'timing_diff' if reason_same else 'unexplained',
                     '同因不同粒度成交价差' if reason_same
                     else '卖出原因不一致且价差存在, 无法归因')

    # 引擎已平仓但实盘同单仍持仓/已平仓不同步
    for key, et in eng_sells.items():
        if key not in live_sells and key in live_buys:
            lt = live_buys[key]
            if lt.get('status') == 'holding':
                add_diff('sell_timing', et['sell_date'], key[0],
                         f"引擎已于{et['sell_date']}平仓({et['reason']}), 实盘仍持仓",
                         'timing_diff',
                         '触线粒度/确认窗口差异导致引擎先离场(需持续跟踪)')

    stats = _stats(diffs)
    return (diffs, buy_pairs, sell_pairs, stats,
            live_buys, eng_buys, signal_results)


def _search_decision(decision, code):
    """在decision留痕中查code: 返回(是否出现在meet_condition, 是否被推荐)。"""
    if not decision:
        return (False, False)
    in_dec = recommended = False
    for s in decision.get('strategies', {}).values():
        for c in s.get('meet_condition', []):
            if c.get('code') == code:
                in_dec = True
        for c in s.get('recommendations', []):
            if c.get('code') == code:
                recommended = True
    return (in_dec, recommended)


def _reason_equiv(eng_reason, live_reason):
    """引擎/实盘卖出原因等价判断(命名不同但语义同)。"""
    if not eng_reason or not live_reason:
        return False
    e, l = str(eng_reason).lower(), str(live_reason).lower()
    groups = [
        {'tp', 'take_profit', '止盈'},
        {'sl', 'stop_loss', 'hard_sl', '止损'},
        {'trailing', 'trailing_stop'},
        {'expired', 'expire', 'timeout', 'timed', 'max_hold', '到期'},
    ]
    for g in groups:
        if any(k in e for k in g) and any(k in l for k in g):
            return True
    return e == l


def _stats(diffs):
    by_cat = {c: 0 for c in CATEGORIES}
    for d in diffs:
        by_cat[d['category']] = by_cat.get(d['category'], 0) + 1
    return {
        'total_diffs': len(diffs),
        'explainable': sum(v for k, v in by_cat.items() if k != 'unexplained'),
        'unexplained': by_cat.get('unexplained', 0),
        'by_category': by_cat,
    }


def compare_nav(rolling, positions, live_days):
    """④ 日级NAV走势偏差。实盘无历史逐日NAV留痕, 用
    '初始资金+截至当日累计已实现盈亏' 近似(不含浮盈), 明确标注口径。"""
    ice = rolling.get('ice_overlay') or {}
    eng_nav = ice.get('recent_daily_nav', {})
    init = positions.get('account', {}).get('initial_capital', 1_000_000)
    closed = positions.get('closed_trades', [])

    rows = []
    prev_e = prev_l = None
    for d in sorted(live_days):
        realized = sum(t.get('pnl_amount', 0) for t in closed
                       if t.get('sell_date') and t['sell_date'] <= d)
        live_nav = init + realized
        e_nav = eng_nav.get(d)
        e_chg = (round((e_nav / prev_e - 1) * 100, 3)
                 if e_nav and prev_e else None)
        l_chg = (round((live_nav / prev_l - 1) * 100, 3)
                 if prev_l else None)
        rows.append({'date': d, 'engine_nav': e_nav,
                     'live_nav_realized_approx': round(live_nav, 2),
                     'engine_chg_pct': e_chg, 'live_chg_pct': l_chg,
                     'chg_gap_pp': (round(e_chg - l_chg, 3)
                                    if e_chg is not None and l_chg is not None
                                    else None)})
        if e_nav:
            prev_e = e_nav
        prev_l = live_nav
    note = ('实盘侧为"初始资金+累计已实现盈亏"近似口径(不含持仓浮盈), '
            '账本total_nav仅有当前时点值; 引擎侧为冰点overlay口径daily_nav。'
            '两侧涨跌方向可比, 绝对幅度不可硬比。')
    return {'note': note,
            'live_total_nav_now': positions.get('account', {}).get('total_nav'),
            'daily': rows}


def build_md(payload):
    p = payload
    lines = [f"# 回测vs实盘对照报告 {p['report_date']}", '',
             f"- 生成时间: {p['generated_at']}",
             f"- rolling来源: {p['rolling_source']}"
             f" (窗口 {p['rolling_period']}, 口径=冰点overlay)",
             f"- 对照交易日({len(p['compare_days'])}): "
             + (', '.join(p['compare_days']) or '无'),
             f"- 实盘启动日: {LIVE_START_DATE}", '']
    if not p['compare_days']:
        lines += ['> 窗口内无实盘数据可对照(早于实盘启动日或无decision/账本记录), '
                  '本次跳过逐笔对照。', '']
        return '\n'.join(lines)

    s = p['stats']
    tl = p.get('three_layer') or {}
    if tl:
        lines += ['## 三层重合率 (T47: 成交级/持仓级/信号级)', '',
                  '| 层级 | 口径 | 配对 | 重合率 |', '|---|---|---|---|']
        layer_desc = [
            ('trade_level', '成交级', '引擎已平仓trades严格code+buy_date配对'),
            ('position_level', '持仓级', '实盘holding两级探测(已平仓配对∪信号级复现)'),
            ('signal_level', '信号级', '直调get_candidates核对候选命中(全部实盘买入)'),
        ]
        for key, name, desc in layer_desc:
            r = tl.get(key, {})
            rate = r.get('rate_pct')
            lines.append(f"| {name} | {desc} | {r.get('matched')}/{r.get('total')} "
                         f"| {rate if rate is not None else 'N/A'}% |")
        lines.append('')
        probes = p.get('open_position_probes') or []
        if probes:
            lines += ['### 未平仓对照明细', '',
                      '| 代码 | 买日 | 策略 | 探测结果 | 信号级(排位/候选数) |',
                      '|---|---|---|---|---|']
            for pr in probes:
                sc = pr.get('signal_check') or {}
                sig = (f"{sc.get('rank')}/{sc.get('n_candidates')}"
                       if sc.get('in_candidates') else
                       (sc.get('error') or ('不在候选' if sc else '—')))
                lines.append(f"| {pr['code']} | {pr['buy_date']} "
                             f"| {pr.get('strategy')} | {pr['result']} | {sig} |")
            lines.append('')
        sigs = p.get('signal_results') or []
        if sigs:
            lines += ['### 信号级对照明细', '',
                      '| 代码 | 买日 | 策略 | 在候选 | 排位 | 候选数 |',
                      '|---|---|---|---|---|---|']
            for r in sigs:
                sc = r['signal_check']
                lines.append(f"| {r['code']} | {r['buy_date']} | {r['strategy']} "
                             f"| {'✅' if sc.get('in_candidates') else '❌'} "
                             f"| {sc.get('rank') or '-'} "
                             f"| {sc.get('n_candidates') if sc.get('n_candidates') is not None else sc.get('error')} |")
            lines.append('')
    lines += ['## 差异归因统计', '',
              f"- 差异总数: {s['total_diffs']} "
              f"(可解释 {s['explainable']} / 不可解释 {s['unexplained']})",
              '', '| 归因分类 | 笔数 | 说明 |', '|---|---|---|']
    cat_desc = {
        'data_source_diff': '数据源差(BaoStock被ban/腾讯降级hour缺失)',
        'timing_diff': '时点差(9:25快照 vs hour1_open; 触线粒度; 已平仓可见性)',
        'overlay_diff': '实盘overlay差(冰点·回撤·修复日·V-C槽)',
        'limit_or_t1_diff': '涨跌停或T+1拦截差',
        'param_version_diff': '参数版本差',
        'unexplained': '**不可解释 → 已告警**',
    }
    for c in CATEGORIES:
        lines.append(f"| {c} | {s['by_category'].get(c, 0)} | {cat_desc[c]} |")
    lines.append('')

    lines += [f"## ①② 买入配对 ({len(p['buy_pairs'])}对)", '']
    if p['buy_pairs']:
        lines += ['| 日期 | 代码 | 策略 | 引擎价 | 实盘价 | 偏差% |',
                  '|---|---|---|---|---|---|']
        for b in p['buy_pairs']:
            lines.append(f"| {b['date']} | {b['code']} | {b.get('strategy', '')} "
                         f"| {b['engine_price']} | {b['live_price']} "
                         f"| {b['price_diff_pct'] if b['price_diff_pct'] is not None else 'N/A'} |")
    else:
        lines.append('(无配对成功买入)')
    lines.append('')

    lines += [f"## ③ 卖出配对 ({len(p['sell_pairs'])}对)", '']
    if p['sell_pairs']:
        lines += ['| 代码 | 买日 | 引擎卖出 | 实盘卖出 | 同日 | 价差% |',
                  '|---|---|---|---|---|---|']
        for sp in p['sell_pairs']:
            e, l = sp['engine_sell'], sp['live_sell']
            lines.append(f"| {sp['code']} | {sp['buy_date']} "
                         f"| {e['date']}H{e['hour']} {e['price']}({e['reason']}) "
                         f"| {l['date']} {l['price']}({l['reason']}) "
                         f"| {'✅' if sp['same_day'] else '❌'} "
                         f"| {sp['price_diff_pct'] if sp['price_diff_pct'] is not None else 'N/A'} |")
    else:
        lines.append('(无配对成功卖出)')
    lines.append('')

    lines += ['## 差异明细', '']
    if p['diffs']:
        lines += ['| 类型 | 日期 | 代码 | 明细 | 归因 | 说明 |',
                  '|---|---|---|---|---|---|']
        for d in p['diffs']:
            lines.append(f"| {d['type']} | {d['date']} | {d['code']} "
                         f"| {d['detail']} | {d['category']} | {d['reason']} |")
    else:
        lines.append('(无差异, 完全一致)')
    lines.append('')

    nav = p['nav_compare']
    lines += ['## ④ 日级NAV走势对照', '', f"> {nav['note']}", '',
              '| 日期 | 引擎NAV | 实盘NAV(已实现近似) | 引擎日涨% | 实盘日涨% | 差(pp) |',
              '|---|---|---|---|---|---|']
    for r in nav['daily']:
        lines.append(f"| {r['date']} | {r['engine_nav'] or 'N/A'} "
                     f"| {r['live_nav_realized_approx']} "
                     f"| {r['engine_chg_pct'] if r['engine_chg_pct'] is not None else 'N/A'} "
                     f"| {r['live_chg_pct'] if r['live_chg_pct'] is not None else 'N/A'} "
                     f"| {r['chg_gap_pp'] if r['chg_gap_pp'] is not None else 'N/A'} |")
    lines += ['', f"- 实盘账本当前total_nav: {nav['live_total_nav_now']}", '']
    return '\n'.join(lines)


def main():
    parser = argparse.ArgumentParser(description='回测vs实盘逐笔对照(Task#5)')
    parser.add_argument('--rolling-json', default=None,
                        help='指定rolling产出json(默认取logs/rolling/最新)')
    parser.add_argument('--no-alert', action='store_true', default=False,
                        help='调试用: 不写scheduler_alerts.log')
    args = parser.parse_args()

    rolling_path = args.rolling_json or find_latest_rolling()
    if not rolling_path or not os.path.exists(rolling_path):
        print('[Compare] 未找到rolling产出json, 请先跑 daily_rolling_backtest.py')
        raise SystemExit(1)
    rolling = load_json(rolling_path)
    if not rolling:
        print(f'[Compare] rolling json解析失败: {rolling_path}')
        raise SystemExit(1)

    positions = load_json(POSITIONS_PATH) or {}
    # 对照窗口 = rolling近5交易日 ∩ [实盘启动日, +∞)
    all_days = (rolling.get('ice_overlay') or {}).get('recent_trading_days', [])
    live_days = [d for d in all_days if d >= LIVE_START_DATE]
    decisions = {}
    for d in live_days:
        decisions[d] = load_json(os.path.join(
            REALTIME_DIR, f"decision_{d.replace('-', '')}.json"))

    if live_days:
        (diffs, buy_pairs, sell_pairs, stats,
         live_buys, eng_buys, signal_results) = match_and_compare(
            rolling, live_days, decisions, positions)
        nav_compare = compare_nav(rolling, positions, live_days)
        # T47-A 未平仓对照层 + T47-D 三层重合率
        open_probes = compare_open_positions(rolling, positions, live_days)
        three_layer = three_layer_rates(live_buys, eng_buys,
                                        open_probes, signal_results)
    else:
        print(f'[Compare] 窗口{all_days}早于实盘启动日{LIVE_START_DATE}, 无可对照数据')
        diffs, buy_pairs, sell_pairs = [], [], []
        stats = _stats([])
        nav_compare = {'note': '窗口内无实盘数据', 'live_total_nav_now': None,
                       'daily': []}
        open_probes, signal_results, three_layer = [], [], None

    end_date = rolling.get('end_date', datetime.now().strftime('%Y-%m-%d'))
    ymd = end_date.replace('-', '')
    payload = {
        'generated_at': now_str(),
        'report_date': end_date,
        'rolling_source': rolling_path,
        'rolling_period': f"{rolling.get('start_date')} ~ {end_date}",
        'live_start_date': LIVE_START_DATE,
        'compare_days': live_days,
        'stats': stats,
        'three_layer': three_layer,
        'open_position_probes': open_probes,
        'signal_results': signal_results,
        'buy_pairs': buy_pairs,
        'sell_pairs': sell_pairs,
        'diffs': diffs,
        'nav_compare': nav_compare,
    }

    os.makedirs(ROLLING_LOG_DIR, exist_ok=True)
    tag = rolling.get('tag') or ''
    sfx = f"_{tag}" if tag else ''
    json_path = os.path.join(ROLLING_LOG_DIR, f'bt_live_divergence_{ymd}{sfx}.json')
    md_path = os.path.join(ROLLING_LOG_DIR, f'bt_live_divergence_{ymd}{sfx}.md')
    with open(json_path, 'w', encoding='utf-8') as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)
    with open(md_path, 'w', encoding='utf-8') as f:
        f.write(build_md(payload))
    print(f'[Compare] JSON: {json_path}\n[Compare] MD:   {md_path}')

    # T47-D: 三层重合率趋势追加(同日重跑覆盖旧行)
    if three_layer:
        line = append_trend_csv(end_date, three_layer)
        print(f'[Compare] 三层重合率: '
              f"成交级{three_layer['trade_level']['matched']}/"
              f"{three_layer['trade_level']['total']} "
              f"持仓级{three_layer['position_level']['matched']}/"
              f"{three_layer['position_level']['total']} "
              f"信号级{three_layer['signal_level']['matched']}/"
              f"{three_layer['signal_level']['total']} "
              f'| csv行: {line}')

    # 告警: 仅不可解释差异 或 单笔价差>0.5%
    if not args.no_alert:
        for d in diffs:
            if d['category'] == 'unexplained':
                append_alert(f"不可解释差异 {d['date']} {d['code']}: {d['detail']}")
        for pair in buy_pairs + [
                {'date': sp['live_sell']['date'], 'code': sp['code'],
                 'price_diff_pct': sp['price_diff_pct'],
                 'engine_price': sp['engine_sell']['price'],
                 'live_price': sp['live_sell']['price']}
                for sp in sell_pairs]:
            pd = pair.get('price_diff_pct')
            if pd is not None and abs(pd) > PRICE_DIFF_ALERT_PCT:
                append_alert(f"单笔价差{pd:+.3f}%>{PRICE_DIFF_ALERT_PCT}% "
                             f"{pair.get('date')} {pair['code']} "
                             f"引擎{pair['engine_price']} vs 实盘{pair['live_price']}")

    print(f"[Compare] 完成: 差异{stats['total_diffs']}笔 "
          f"(可解释{stats['explainable']}/不可解释{stats['unexplained']})")


if __name__ == '__main__':
    main()
