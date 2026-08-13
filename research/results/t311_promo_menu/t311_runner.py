#!/usr/bin/env python3
"""Task#311 双转正包新锚联测: 晋级率过热门控重测(格A) + 门控×h1_touch联合(格C)。

背景: 两转正包等批, 但数字口径已脏——
  ①晋级率门控#250终测174.29/20.25/8.61基于t299涨跌停口径修复前旧代码(旧锚171.73),
    结构性失效, 本任务新锚重测(格A);
  ②S5 h1_touch #310已是新锚口径179.77/26.50/6.78, 直接引用不重跑(格B);
  格C=两改动联合跑, 看是否相互干扰。
base=173.10/26.51/6.53/2888 直接复用t307 run_base.json(同码同数据自跑配对锚)。

规格冻结, 零参数再优化:
  gate: GateScaler类逐字复制t250_runner.py(P70_w0.3, promo∩tail_mean_h4 expanding
    P70 warmup120 → D+1新开仓×0.3, ice>gate>boost); 指标CSV由t311_indicators.py
    按t250_qa已验证SQL重建(t244原件被t300清空), 日历131天/分年逐位断言=G1终格。
  h1_touch: S5H1Touch类逐字复制t307_engine_runner.py(仅D2 H1内触板卖板价,
    其余super()生产逐字路径, slot当小时复购保留)。

红线: 零生产改动/sqlite ro(生产data_feed与position_scale已内建mode=ro,
data_feed主连接仅SELECT只读使用)/零BaoStock/nice -19/STATUS.md每10分钟。
用法: nice -n 19 python3 t311_runner.py <tag>   tag∈{gate, combo}
"""
import csv
import json
import os
import sys
import time
from collections import defaultdict
from dataclasses import asdict

sys.path.insert(0, '/home/AIWealth')
sys.path.insert(0, '/home/AIWealth/research')

from strategies.base import SellSignal                        # noqa: E402 只读
from strategies.two_board_pullback_dip_h1c import (           # noqa: E402 只读
    TwoBoardPullbackDipH1cStrategy)
from backtest.data_feed import BacktestDataFeed               # noqa: E402 只读
from backtest.position_scale import PositionScaler            # noqa: E402 只读
from backtest.run_unified import (                            # noqa: E402 只读
    UnifiedBacktestEngine, DB_PATH, load_strategy)
from t174_boost_engine_runner import install_buy_audit        # noqa: E402 Remy版

HERE = os.path.dirname(os.path.abspath(__file__))
CSV = os.path.join(HERE, 'indicators_daily.csv')   # t311重建, 口径=t250_qa SQL
START, END = '2021-01-01', '2026-07-01'
ICE_SPEC = 'ice35:0.3:repair_exempt'
GATE_Q, GATE_W = 0.70, 0.3
WARMUP = 120
NAMES = ['big_yang_low_open_v2', 'firstboard_low_open_dip_v2',
         'gem_star_late_seal', 'amplitude_reversal',
         'two_board_pullback_dip_h1c']   # 与t143/t187/t250/t307 slot顺序逐字一致
INIT_CAP = 1_000_000.0
# G1终格日历(t249/t250冻结): 131天/分年逐位断言, 重建CSV漂移即停跑上报
LEDGER_GATED = {'n_days': 131,
                'days_by_year': {'2021': 4, '2022': 34, '2023': 15,
                                 '2024': 43, '2025': 28, '2026': 7}}


def pctl(sv, q):
    return sv[min(len(sv) - 1, int(q * len(sv)))]


def expanding_trig(col, q):
    """与t250_runner.expanding_trig逐行一致: expanding Pq无未来触发
    (warmup120, 仅<=D历史), 返回D+1作用日集。"""
    rows = [r for r in csv.DictReader(open(CSV)) if r['date'] >= '2021-01-01']
    ds = [r['date'] for r in rows]
    nxt = {ds[i]: ds[i + 1] for i in range(len(ds) - 1)}
    sample = [r for r in rows if r[col] != '' and r['date'] in nxt]
    hist, trig = [], set()
    for r in sample:
        x = float(r[col])
        if len(hist) >= WARMUP and x >= pctl(sorted(hist), q):
            trig.add(nxt[r['date']])
        hist.append(x)
    return trig


def gate_calendar(engine_dates):
    """P70交集门控作用日历(D+1), 断言与G1终格131天/分年逐位一致后放行。"""
    gd = (expanding_trig('promo_rate', GATE_Q)
          & expanding_trig('tail_mean_h4', GATE_Q)) & set(engine_dates)
    by_year = defaultdict(int)
    for d in gd:
        by_year[d[:4]] += 1
    assert len(gd) == LEDGER_GATED['n_days'], \
        f"日历天数{len(gd)} != G1的{LEDGER_GATED['n_days']} — 重建CSV漂移, 停跑上报"
    assert dict(by_year) == LEDGER_GATED['days_by_year'], \
        f"分年{dict(by_year)} != G1的{LEDGER_GATED['days_by_year']} — 停跑上报"
    print(f"[gate] 日历复算与G1终格逐位一致: {len(gd)}天 分年{dict(by_year)}")
    return gd


class GateScaler:
    """(逐字复制t250_runner.py) ice > gate 优先级scaler(研究注入);
    gate > boost由引擎native分支成立(run_unified仅buy_scale>=1.0才覆盖为w)。"""

    def __init__(self, db_path, ice_spec, gate_days, gate_w, start, end):
        self.inner = PositionScaler(db_path, ice_spec, start, end)
        self.gate_days = frozenset(gate_days)
        self.gate_w = gate_w
        self.engine = None
        self._last_date = None
        self.day_base = {}        # date -> scaler返回值(boost覆盖前)
        self.day_final = {}       # date -> 当日最终生效buy_scale(boost覆盖后)
        self.gate_eff_days = []   # gate实际生效日(非冰点减仓日)
        self.gate_ice_overlap = []  # gate∩冰点减仓日(ice优先, gate让位)

    def attach(self, engine):
        self.engine = engine

    def get(self, date):
        # 引擎在D开盘前调用; 此刻buy_scale仍是D-1最终值(含boost覆盖) → 快照
        if self._last_date is not None and self.engine is not None:
            self.day_final[self._last_date] = self.engine.portfolio.buy_scale
        self._last_date = date
        base = self.inner.get(date)
        if base < 1.0:                       # 冰点减仓日: ice最高优先
            if date in self.gate_days:
                self.gate_ice_overlap.append(date)
            s = base
        elif date in self.gate_days:         # gate日(含冰点豁免日): ×0.3
            self.gate_eff_days.append(date)
            s = self.gate_w
        else:
            s = 1.0
        self.day_base[date] = s
        return s

    def finalize(self):
        if self._last_date is not None and self.engine is not None:
            self.day_final[self._last_date] = self.engine.portfolio.buy_scale

    def is_ice_day(self, date):
        return self.inner.is_ice_day(date)


class S5H1Touch(TwoBoardPullbackDipH1cStrategy):
    """(逐字复制t307_engine_runner.py, Task#310 PASS分支) H1限定touch窄门:
    仅D2(卖出日)H1内触板卖板价兑现; 其余全部super()=生产d2h1_close逐字路径
    (H1收盘卖/deferred兜底), slot当小时复购结构完整保留。
    防未来: 触板=H1内已发生事件, 卖价=板价。"""

    def should_sell(self, position, date, hour, data_feed):
        if position.buy_date < date and hour == 1 \
                and data_feed._prev_trading_date(date) == position.buy_date:
            limit_up, _ = data_feed.get_limit_prices(position.code, date)
            hi = data_feed.get_hour_high(position.code, date, 1)
            if limit_up > 0 and hi and hi >= limit_up - 0.001:
                return SellSignal(reason='touch_board', price=float(limit_up))
        return super().should_sell(position, date, hour, data_feed)


def priority_audit(scaler, dd_w):
    """(逐字复制t250_runner.py) ice>gate>boost全量逐日断言。"""
    n = {'ice_reduce': 0, 'gate_eff': 0, 'ice_exempt': 0, 'boost': 0,
         'plain': 0, 'gate_boost_suppressed': 0}
    violations = []
    for d, final in scaler.day_final.items():
        ice_reduce = scaler.inner.get(d) < 1.0
        is_ice = scaler.is_ice_day(d)
        is_gate = d in scaler.gate_days
        if ice_reduce:                       # ice减仓日: 恒0.3, gate/boost让位
            n['ice_reduce'] += 1
            ok = abs(final - scaler.inner.weight) < 1e-12
        elif is_gate:                        # gate日: 恒0.3, boost绝不覆盖
            n['gate_eff'] += 1
            ok = abs(final - scaler.gate_w) < 1e-12
        elif is_ice:                         # 冰点豁免日: 1.0, boost被ice抑制
            n['ice_exempt'] += 1
            ok = abs(final - 1.0) < 1e-12
        else:                                # 普通日: 1.0 或 boost 1.5
            ok = final in (1.0, dd_w)
            n['boost' if final == dd_w else 'plain'] += 1
        if not ok:
            violations.append((d, final))
    return n, violations


def build_strategies(tag):
    st = {}
    for i, n in enumerate(NAMES):
        if n == 'two_board_pullback_dip_h1c' and tag == 'combo':
            st[i] = S5H1Touch()
            print(f"  Slot {i}: {st[i].name} (h1_touch)")
        else:
            st[i] = load_strategy(n)
            print(f"  Slot {i}: {n}")
    return st


def run_one(tag, gate_days):
    t0 = time.time()
    feed = BacktestDataFeed(DB_PATH)
    scaler = GateScaler(DB_PATH, ICE_SPEC, gate_days, GATE_W, START, END)
    strategies = build_strategies(tag)
    # 生产口径: native dd-boost 15:1.5(Task#204默认) + position_scaler注入
    engine = UnifiedBacktestEngine(strategies, feed,
                                   initial_capital=INIT_CAP,
                                   position_scaler=scaler,
                                   dd_boost_x=15.0, dd_boost_w=1.5)
    scaler.attach(engine)
    buy_events = install_buy_audit(engine.portfolio)
    summary = engine.run(START, END)
    scaler.finalize()
    engine.print_summary(summary)
    feed.close()

    cagr, mdd = summary['cagr_pct'], summary['max_drawdown_pct']
    summary['calmar'] = round(cagr / mdd, 2) if mdd else None

    # ---- 优先级链全量审计(t250口径) ----
    prio_n, prio_viol = priority_audit(scaler, 1.5)
    print(f"[priority] {json.dumps(prio_n)} 违规={len(prio_viol)}"
          + (f" {prio_viol[:5]}" if prio_viol else ""))

    # ---- 红线审计(t250+t307口径合并) ----
    trades = engine.portfolio.trades
    gate_eff = set(scaler.gate_eff_days)
    s5_reasons = {}
    for t in trades:
        if t.strategy_name == 'two_board_pullback_dip_h1c':
            s5_reasons[t.reason] = s5_reasons.get(t.reason, 0) + 1
    audit = {
        'cash_min': min((e['cash_after'] for e in buy_events),
                        default=INIT_CAP),
        'max_cost_over_nav': max((e['cost'] / e['nav_b']
                                  for e in buy_events if e['nav_b'] > 0),
                                 default=0.0),
        'n_partial': sum(1 for e in buy_events if e['partial']),
        'n_buys': len(buy_events),
        't0_sell_blocked': engine._t0_sell_blocked,
        'price_clamped': engine._price_clamped,
        'cash_ge0_all': all(e['cash_after'] >= -1e-6 for e in buy_events),
        'single_pos_le_cap': all(
            e['cost'] <= e['nav_b'] / 5 * 1.5 + 1e-6 for e in buy_events),
        'gate_day_cap_ok': all(
            e['cost'] <= e['nav_b'] / 5 * GATE_W + 1e-6
            for e in buy_events if e['date'] in gate_eff),
        'n_gate_day_buys': sum(1 for e in buy_events if e['date'] in gate_eff),
        't1_violations': sum(1 for t in trades if t.buy_date == t.sell_date),
        's5_trades': sum(1 for t in trades
                         if t.strategy_name == 'two_board_pullback_dip_h1c'),
        's5_reasons': s5_reasons,
        'priority_chain': prio_n,
        'priority_violations': len(prio_viol),
    }
    out = {'tag': tag, 'gate': f'P70_w{GATE_W}(131d)',
           's5_exit': ('h1_touch' if tag == 'combo' else 'prod(d2h1_close)'),
           'ice_spec': ICE_SPEC, 'dd_boost': '15:1.5(native)',
           'start': START, 'end': END, 'strategies': NAMES,
           'summary': summary, 'audit': audit,
           'overlay_days': {
               'n_gate_eff': len(scaler.gate_eff_days),
               'n_gate_ice_overlap': len(scaler.gate_ice_overlap),
               'n_ice_scaled': scaler.inner.n_scaled_days,
               'n_ice_exempt': scaler.inner.n_exempt_days,
               'n_boost_eff': engine._dd_boost_days,
               'n_boost_wanted': engine._dd_wanted_days,
               'n_boost_ice_suppressed': engine._dd_suppressed_days},
           'elapsed_sec': round(time.time() - t0, 1)}
    with open(os.path.join(HERE, f'run_{tag}.json'), 'w') as f:
        json.dump(out, f, ensure_ascii=False, indent=1)
    # QA工作件(逐笔trades+逐日系数, 审计后即删)
    slim = {'daily_nav': engine.daily_nav,
            'trades': [asdict(t) for t in trades],
            'day_base': scaler.day_base, 'day_final': scaler.day_final,
            'gate_eff_days': scaler.gate_eff_days,
            'gate_ice_overlap': scaler.gate_ice_overlap}
    with open(os.path.join(HERE, f'work_{tag}.json'), 'w') as f:
        json.dump(slim, f, ensure_ascii=False)
    print(f"\n[t311:{tag}] done {out['elapsed_sec']}s | CAGR={cagr} "
          f"MDD={mdd} Calmar={summary['calmar']} "
          f"trades={summary['n_trades']} | audit={json.dumps(audit)}")
    return out


def main():
    tag = sys.argv[1]
    assert tag in ('gate', 'combo'), f"unknown tag {tag}"
    feed = BacktestDataFeed(DB_PATH)
    all_dates = feed.get_trading_dates(START, END)
    feed.close()
    gd = gate_calendar(all_dates)
    return run_one(tag, gd)


if __name__ == '__main__':
    main()
