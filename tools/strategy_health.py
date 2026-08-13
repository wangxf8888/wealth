#!/usr/bin/env python3
"""[Task#73] 策略健康度监控 - 盘后cron(19:00)运行.

功能:
  1. 每策略滚动窗口(WINDOW_DAYS=30个交易日)实盘表现(positions.json closed_trades
     按策略分组: 笔均收益/胜率/触发频率) vs 回测solo档案同月历史分布基准带(均值±1.5σ)
  2. 三维偏离检测: 笔均超带 / 胜率超带(二项SE) / 触发频率异常
     (连续FREQ_CONSEC_N日该策略候选有但从不成交 = 可能上游失效)
  3. 心理仪表三数字: 连续亏损笔数 / 当前回撤深度与作战等级(drawdown_guard) /
     近30日实盘vs回测期望偏离度(σ单位)
  4. 每晚快照NAV并驱动回撤作战手册评估(调tools/drawdown_guard.snapshot_and_evaluate)

冷启动: 策略窗口内样本 < COLD_START_MIN(20)笔 → 只展示不告警(性能两维);
        触发频率异常不依赖成交样本(恰是冷启动期发现上游失效的手段), 保持告警。

输出(与Robin Task#72晚间复盘报告的接口, schema_version=1):
  logs/realtime/strategy_health.json
  偏离告警追加 logs/realtime/scheduler_alerts.log ([STRATEGY_HEALTH]标签)

只读: positions.json / candidates_*.json / solo档案 / stocks.db;
只写: strategy_health.json / drawdown_state.json / 告警日志。不触碰交易主链。
"""
import glob
import json
import os
import sqlite3
import sys
from datetime import datetime

BASE = '/home/AIWealth'
sys.path.insert(0, BASE)
from tools import drawdown_guard   # noqa: E402

POSITIONS_FILE = f'{BASE}/data/realtime/positions.json'
CAND_GLOB = f'{BASE}/data/realtime/candidates_*.json'
SOLO_DIR = f'{BASE}/logs/backtest/solo'
DB = f'{BASE}/data/stocks.db'
OUT_FILE = f'{BASE}/logs/realtime/strategy_health.json'
ALERT_LOG = f'{BASE}/logs/realtime/scheduler_alerts.log'

# ── 常量(可配) ────────────────────────────────────────────────
WINDOW_DAYS = 30        # 滚动窗口: 30个交易日
BAND_SIGMA = 1.5        # 基准带宽: 均值±1.5σ
COLD_START_MIN = 20     # 样本<20笔只展示不告警
FREQ_CONSEC_N = 5       # 连续N个交易日候选有但从不成交 → 上游失效嫌疑


def _alert(msg):
    line = f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] " \
           f"[STRATEGY_HEALTH] {msg}"
    print(line)
    try:
        with open(ALERT_LOG, 'a') as f:
            f.write(line + '\n')
    except Exception as e:                        # 告警降级不影响主流程
        print(f"[strategy_health] 写告警日志失败(降级继续): {e}")


def trading_days(n):
    """最近n个交易日(升序), 以index_kline sh.000001为日历。"""
    con = sqlite3.connect(DB)
    rows = con.execute(
        "SELECT DISTINCT date FROM index_kline WHERE code='sh.000001' "
        "ORDER BY date DESC LIMIT ?", (n,)).fetchall()
    con.close()
    return sorted(r[0] for r in rows)


def mean_std(xs):
    n = len(xs)
    if n == 0:
        return None, None
    m = sum(xs) / n
    if n == 1:
        return m, 0.0
    var = sum((x - m) ** 2 for x in xs) / (n - 1)
    return m, var ** 0.5


def load_live_trades():
    pos = json.load(open(POSITIONS_FILE))
    closed = pos.get('closed_trades', [])
    holding = [p for p in pos.get('positions', [])
               if p.get('status') == 'holding']
    return closed, holding


def load_candidates_by_day(days):
    """{trade_date: {strategy_name: n_candidates}} 仅窗口内交易日。"""
    out = {}
    for f in glob.glob(CAND_GLOB):
        if f.endswith('.json'):
            try:
                d = json.load(open(f))
            except Exception:
                continue
            td = d.get('trade_date')
            if td not in days:
                continue
            row = {}
            for k, v in d.get('strategies', {}).items():
                if not isinstance(v, dict):
                    continue
                # v2: {S1:{strategy_name,...}}; v1旧格式: 策略名直接作键
                name = v.get('strategy_name', k)
                row[name] = v.get('total_count',
                                  len(v.get('candidates', []) or []))
            out[td] = row
    return out


def solo_baseline(strategy, months):
    """solo档案同月(月份of-year∈months)历史分布 → 均值/σ/胜率/n。"""
    path = f'{SOLO_DIR}/{strategy}_trades.json'
    if not os.path.exists(path):
        return None
    trades = json.load(open(path)).get('trades', [])
    xs = [t['profit_pct'] for t in trades
          if int(t['buy_date'][5:7]) in months]
    if not xs:
        return None
    m, s = mean_std(xs)
    wr = 100.0 * sum(1 for x in xs if x > 0) / len(xs)
    return {'n_hist': len(xs), 'mean': round(m, 3), 'std': round(s, 3),
            'band': [round(m - BAND_SIGMA * s, 2),
                     round(m + BAND_SIGMA * s, 2)],
            'wr_hist': round(wr, 2),
            'months': sorted(months), 'source': f'solo/{strategy}_trades.json'}


def analyze_strategy(name, slot, closed, holding, cand_by_day, days):
    """单策略健康度: 实盘窗口表现 + 基准带 + 三维偏离。"""
    w_start, w_end = days[0], days[-1]
    trades = [t for t in closed if t.get('strategy') == name
              and w_start <= t.get('sell_date', '') <= w_end]
    n = len(trades)
    avg = wr = None
    if n:
        avg = round(sum(t['pnl_pct'] for t in trades) / n, 3)
        wr = round(100.0 * sum(1 for t in trades if t['pnl_pct'] > 0) / n, 2)

    # 触发频率: 候选日/成交日/连续候选无成交
    buy_dates = {t['buy_date'] for t in closed if t.get('strategy') == name}
    buy_dates |= {p['buy_date'] for p in holding if p.get('strategy') == name}
    cand_days = [d for d in days if cand_by_day.get(d, {}).get(name, 0) > 0]
    consec_no_buy = 0
    for d in reversed(days):                      # 从最近往回数
        n_cand = cand_by_day.get(d, {}).get(name)
        if n_cand is None:                        # 该日无档案(策略未上线等)
            continue
        if n_cand == 0:
            break                                 # 无候选日中断连击
        if d in buy_dates:
            break
        consec_no_buy += 1

    months = {int(d[5:7]) for d in days}
    base = solo_baseline(name, months)

    cold = n < COLD_START_MIN
    dev = {'avg_out_of_band': False, 'wr_out_of_band': False,
           'freq_anomaly': consec_no_buy >= FREQ_CONSEC_N}
    alerts = []
    if base and n:
        if not (base['band'][0] <= avg <= base['band'][1]):
            dev['avg_out_of_band'] = True
            alerts.append(f"{name}: 窗口笔均{avg:+.2f}% 超出基准带"
                          f"{base['band']} (同月历史均值{base['mean']:+.2f}"
                          f"±{BAND_SIGMA}σ, n_hist={base['n_hist']})")
        p = base['wr_hist'] / 100.0
        se = (p * (1 - p) / n) ** 0.5 * 100 if 0 < p < 1 else 0.0
        lo, hi = base['wr_hist'] - BAND_SIGMA * se, \
            base['wr_hist'] + BAND_SIGMA * se
        if not (lo <= wr <= hi):
            dev['wr_out_of_band'] = True
            alerts.append(f"{name}: 窗口胜率{wr:.1f}% 超出二项带"
                          f"[{lo:.1f},{hi:.1f}] (历史{base['wr_hist']:.1f}%, "
                          f"n={n})")
    if dev['freq_anomaly']:
        alerts.append(f"{name}: 连续{consec_no_buy}个交易日有候选但从未成交 "
                      f"(≥{FREQ_CONSEC_N}日阈值) — 可能上游失效(数据源/"
                      f"过滤器/决策链), 请查morning_decision日志")

    # 冷启动: 性能两维只展示不告警; 频率异常保持告警(不依赖成交样本)
    fire = alerts if not cold else \
        [a for a in alerts if '从未成交' in a]
    for a in fire:
        _alert("⚠️ " + a)

    return {
        'slot': slot, 'n_trades': n, 'avg_pnl_pct': avg, 'win_rate_pct': wr,
        'trigger': {'candidate_days': len(cand_days),
                    'buy_days': len([d for d in days if d in buy_dates]),
                    'consec_cand_no_buy': consec_no_buy},
        'baseline': base,
        'deviation': dev,
        'cold_start': cold,
        'alerts_fired': fire,
        'alerts_suppressed_cold_start': [a for a in alerts if a not in fire],
    }


def psychology(closed, dd_state, strat_results):
    """心理仪表三数字(供前端与Robin复盘报告)。"""
    # 1. 连续亏损笔数(全组合, 按卖出时序)
    seq = sorted(closed, key=lambda t: (t.get('sell_date', ''),
                                        t.get('code', '')))
    streak = 0
    for t in reversed(seq):
        if t.get('pnl_pct', 0) <= 0:
            streak += 1
        else:
            break
    # 2. 回撤深度与作战等级
    dd = {'drawdown_pct': dd_state.get('drawdown_pct', 0.0),
          'level': dd_state.get('level', 0),
          'level_desc': drawdown_guard.LEVEL_DESC[dd_state.get('level', 0)],
          'scale': dd_state.get('scale', 1.0),
          'halt': dd_state.get('halt', False)}
    # 3. 近30日实盘vs回测期望偏离度(σ单位, 全策略汇总)
    lv, hist_m, hist_s = [], [], []
    for name, r in strat_results.items():
        if r['n_trades'] and r['baseline']:
            lv += [r['avg_pnl_pct']] * r['n_trades']
            hist_m += [r['baseline']['mean']] * r['n_trades']
            hist_s += [r['baseline']['std']] * r['n_trades']
    z = None
    if lv:
        live_avg = sum(lv) / len(lv)
        exp_avg = sum(hist_m) / len(hist_m)
        pooled_s = sum(hist_s) / len(hist_s)
        z = round((live_avg - exp_avg) / pooled_s, 3) if pooled_s else 0.0
    return {'consecutive_losses': streak,
            'drawdown': dd,
            'deviation_30d_sigma': z,
            'note': 'deviation为窗口内实盘笔均相对solo同月期望的σ偏离; '
                    'null=窗口无成交'}


def main():
    days = trading_days(WINDOW_DAYS)
    if not days:
        _alert("❌ 交易日历为空(stocks.db异常?), 本次跳过")
        return
    closed, holding = load_live_trades()
    cand_by_day = load_candidates_by_day(set(days))

    # 策略宇宙: 最新candidates文件的S1-S5 + 历史closed_trades出现过的策略
    latest = max(glob.glob(CAND_GLOB))
    slots = {v.get('strategy_name', k): k for k, v in
             json.load(open(latest)).get('strategies', {}).items()
             if isinstance(v, dict)}
    for t in closed:
        slots.setdefault(t.get('strategy'), '-')

    # NAV快照 + 回撤作战手册评估(drawdown_guard自行落盘/告警/企微)
    dd_state = drawdown_guard.snapshot_and_evaluate()

    results = {}
    for name, slot in sorted(slots.items(), key=lambda kv: kv[1]):
        if not name:
            continue
        results[name] = analyze_strategy(name, slot, closed, holding,
                                         cand_by_day, days)

    out = {
        'schema_version': 1,
        'generated_at': datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
        'window': {'start': days[0], 'end': days[-1],
                   'trading_days': len(days)},
        'config': {'band_sigma': BAND_SIGMA, 'cold_start_min': COLD_START_MIN,
                   'freq_consec_n': FREQ_CONSEC_N},
        'strategies': results,
        'psychology': psychology(closed, dd_state, results),
        'drawdown_state': {k: dd_state.get(k) for k in
                           ('current_nav', 'hwm', 'drawdown_pct', 'level',
                            'scale', 'whitelist_only', 'halt',
                            'halt_ack_required', 'updated_at')},
    }
    tmp = OUT_FILE + '.tmp'
    json.dump(out, open(tmp, 'w'), ensure_ascii=False, indent=1)
    os.replace(tmp, OUT_FILE)
    n_alerts = sum(len(r['alerts_fired']) for r in results.values())
    print(f"[strategy_health] {out['generated_at']} 窗口{days[0]}~{days[-1]} "
          f"策略{len(results)}个 告警{n_alerts}条 -> {OUT_FILE}")


if __name__ == '__main__':
    try:
        main()
    except Exception as e:                        # 全链路降级: 只告警不抛出
        _alert(f"❌ strategy_health运行异常(不影响交易主链): {e!r}")
        raise SystemExit(0)
