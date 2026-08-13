#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
每日滚动回测 (Task#5)
====================================================================
逻辑:
  1. 只读查询 stocks.db 最新交易日 latest_date
  2. 跑在产5策略统一资金池组合两次:
     (a) 无overlay          (纯策略口径, 对照基线)
     (b) 生产口径R2+gate = ice35:0.3:repair_exempt + dd-boost 15:1.5
         + 晋级率过热门控 p70:0.3 (优先级 ice > gate > boost)
         (Task#319 用户2026-08-12批准方案C: 门控转正+S5 h1_touch,
          新全量锚 CAGR 182.22 / MDD 20.25 / Calmar 9.00 / 2893笔;
          旧#204锚171.73/26.50/6.48/2889笔已经t299口径修复+#319双改动, 仅存档对照)
  3. 结果落盘:
     - logs/rolling/rolling_YYYYMMDD[_tag].json  (两口径指标+近5交易日交易)
     - logs/rolling/rolling_YYYYMMDD[_tag].md    (人读版, 含与昨日meta的CAGR差值)
     - data/rolling/rolling_meta.json            (写前备份到 rolling_meta.prev.json)
  4. 仅当 start=2021-01-01 全量口径且未 --skip-frontend 时,
     重建 frontend/data/strategies_summary.json (结构保持, display_name中文正名)
  5. 全程>40分钟 → 追加告警到 logs/realtime/scheduler_alerts.log

CLI:
  python3 tools/daily_rolling_backtest.py                  # 全量: 2021-01-01 ~ 最新交易日
  python3 tools/daily_rolling_backtest.py --start 2026-06-01 --end 2026-07-31 \
      --skip-frontend --tag smoke                          # 短窗口冒烟

红线: 本脚本对 data/realtime/ 只读; 只写 logs/rolling/, data/rolling/,
      frontend/data/strategies_summary.json (仅全量口径), scheduler_alerts.log (追加).
====================================================================
"""
import argparse
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import time
from dataclasses import asdict
from datetime import datetime

# ================= 配置区 =================
BASE_DIR = '/home/AIWealth'
DB_PATH = os.path.join(BASE_DIR, 'data', 'stocks.db')
ROLLING_LOG_DIR = os.path.join(BASE_DIR, 'logs', 'rolling')
ROLLING_DATA_DIR = os.path.join(BASE_DIR, 'data', 'rolling')
META_PATH = os.path.join(ROLLING_DATA_DIR, 'rolling_meta.json')
META_PREV_PATH = os.path.join(ROLLING_DATA_DIR, 'rolling_meta.prev.json')
FRONTEND_SUMMARY = os.path.join(BASE_DIR, 'frontend', 'data',
                                'strategies_summary.json')
COMBINED_TRADES_PATH = os.path.join(BASE_DIR, 'frontend', 'data',
                                    'combined_5slot_new_trades.json')
SOLO_DIR = os.path.join(BASE_DIR, 'logs', 'backtest', 'solo')
ALERT_LOG = os.path.join(BASE_DIR, 'logs', 'realtime', 'scheduler_alerts.log')

FULL_START = '2021-01-01'          # 全量口径起点(仅此口径重建前端summary)
# Task#204 生产口径R2(用户2026-08-06批准): ice含修复日豁免 + 回撤加仓boost
ICE_SPEC = 'ice35:0.3:repair_exempt'   # 冰点overlay生产实配
DD_BOOST = '15:1.5'                # drawdown-boost生产实配(X=15%/w=1.5)
# Task#319 晋级率过热门控生产实配(用户2026-08-12批准方案C, 规格冻结链
# #244→#249→#250→#311, 禁止参数再优化; 优先级 ice > gate > boost)
PROMO_GATE = 'p70:0.3'             # 过热日新开仓×0.3
RECENT_DAYS = 5                    # 近N个交易日新增交易
RUNTIME_ALERT_MIN = 40             # 全程超时告警阈值(分钟)
SOLO_STALE_DAYS = 3                # solo档案end_date落后最新交易日超过N天视为过期
                                   # (Task#42: 14→3收紧, 首夜混合口径事故复盘)
PROC_START_TS = time.time()        # 本进程启动时刻(Task#42守卫: solo刷新中判定基准)

# 在产5策略 (回测引擎名) + 中文正名 (汇报/前端一律中文)
# Task#204: slot顺序对齐t143/t187档案口径(买入优先级影响现金约束/半仓日
# 限额分配, 顺序不同会偏离新基线锚182.22, 旧R2锚171.73同理) —— 禁止改序
STRATEGIES = [
    'big_yang_low_open_v2',
    'firstboard_low_open_dip_v2',
    'gem_star_late_seal',
    'amplitude_reversal',
    'two_board_pullback_dip_h1c',
]
CN_NAMES = {
    'firstboard_low_open_dip_v2': '首板低吸',
    'amplitude_reversal': '巨振反转',
    'gem_star_late_seal': '创科晚封',
    'big_yang_low_open_v2': '大阳低吸',
    'two_board_pullback_dip_h1c': '双板回调低吸',
}
# ================= 配置区结束 =================

sys.path.insert(0, BASE_DIR)


def now_str():
    return datetime.now().strftime('%Y-%m-%d %H:%M:%S')


def append_alert(msg: str):
    """追加告警到 scheduler_alerts.log, 与现有行格式一致: [ts] [TAG] ⚠️ msg"""
    line = f"[{now_str()}] [ROLLING] ⚠️ {msg}\n"
    try:
        with open(ALERT_LOG, 'a', encoding='utf-8') as f:
            f.write(line)
        print(f"[告警] {line.strip()}")
    except Exception as e:  # 告警失败不阻断主流程
        print(f"[警告] 写告警日志失败: {e}")


def get_latest_trade_date() -> str:
    """只读查询stocks.db最新交易日。"""
    conn = sqlite3.connect(f'file:{DB_PATH}?mode=ro', uri=True)
    try:
        cur = conn.cursor()
        cur.execute("SELECT MAX(date) FROM stock_kline")
        row = cur.fetchone()
        if not row or not row[0]:
            raise RuntimeError("stock_kline 查不到最新交易日")
        return row[0]
    finally:
        conn.close()


def get_hour_coverage(days: list) -> dict:
    """只读统计指定日期的hour数据覆盖率(BaoStock被ban期间腾讯降级只有日K)。"""
    if not days:
        return {}
    conn = sqlite3.connect(f'file:{DB_PATH}?mode=ro', uri=True)
    try:
        cur = conn.cursor()
        ph = ','.join('?' * len(days))
        cur.execute(
            f"SELECT date, COUNT(*), SUM(hour1_open IS NOT NULL) "
            f"FROM stock_kline WHERE date IN ({ph}) GROUP BY date", days)
        out = {}
        for d, total, has_hour in cur.fetchall():
            pct = round((has_hour or 0) / total * 100, 2) if total else 0.0
            out[d] = {'total': total, 'with_hour': has_hour or 0,
                      'coverage_pct': pct}
        return out
    finally:
        conn.close()


def run_one(position_scale, start, end, keep_full=False, dd_boost=None,
            promo_gate=None):
    """跑一次组合回测, 返回 block_dict。
    keep_full=True时额外携带完整trades/daily_nav(块内'_full'键, 调用方须pop,
    禁止随payload落盘——全量3000+笔会把rolling_YYYYMMDD.json撑爆)。"""
    from backtest.run_unified import run_unified_backtest
    if position_scale and dd_boost and promo_gate:
        label = (f"production_R2({position_scale}+boost{dd_boost}"
                 f"+gate{promo_gate})")
    elif position_scale and dd_boost:
        label = f"production_R2({position_scale}+boost{dd_boost})"
    elif position_scale:
        label = f"ice_overlay({position_scale})"
    else:
        label = "no_overlay"
    print(f"\n{'=' * 60}\n[Rolling] 开始口径: {label} | {start} ~ {end}\n{'=' * 60}")
    t0 = time.time()
    result = run_unified_backtest(STRATEGIES, start, end,
                                  position_scale=position_scale,
                                  dd_boost=dd_boost,
                                  promo_gate=promo_gate)
    elapsed = round(time.time() - t0, 1)
    s = result['summary']

    # 近N个交易日 + 该窗口内的新增交易(买或卖发生在窗口内; 引擎trades仅含已平仓)
    all_days = sorted(result['daily_nav'].keys())
    recent_days = all_days[-RECENT_DAYS:]
    recent_trades = [asdict(t) for t in result['trades']
                     if t.buy_date in recent_days or t.sell_date in recent_days]
    recent_nav = {d: result['daily_nav'][d] for d in all_days[-10:]}

    block = {
        'label': label,
        'position_scale': position_scale,
        'elapsed_sec': elapsed,
        'cagr_pct': s['cagr_pct'],
        'max_drawdown_pct': s['max_drawdown_pct'],
        'win_rate_pct': s['win_rate_pct'],
        'n_trades': s['n_trades'],
        'final_nav': s['final_nav'],
        'avg_profit_pct': s['avg_profit_pct'],
        'yearly_returns': s.get('yearly_returns', {}),
        'per_strategy': s.get('per_strategy', {}),
        'recent_trading_days': recent_days,
        'recent_trades': recent_trades,
        'recent_daily_nav': recent_nav,
        'summary': s,
    }
    if keep_full:
        block['_full'] = {
            'trades': [asdict(t) for t in result['trades']],
            'daily_nav': result['daily_nav'],
        }
    print(f"[Rolling] {label} 完成: CAGR={s['cagr_pct']:+.2f}% "
          f"MDD={s['max_drawdown_pct']:.2f}% 耗时{elapsed}s")
    return block


def load_json(path):
    try:
        with open(path, 'r', encoding='utf-8') as f:
            return json.load(f)
    except Exception:
        return None


def write_json(path, obj):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, 'w', encoding='utf-8') as f:
        json.dump(obj, f, ensure_ascii=False, indent=2)


def write_json_atomic(path, obj):
    """Task#51: 原子写JSON, 防前端/其他进程并发读到半截文件。
    先写 path+'.tmp' → flush+fsync落盘 → os.replace原子替换(同目录同文件系统)。"""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + '.tmp'
    with open(tmp, 'w', encoding='utf-8') as f:
        json.dump(obj, f, ensure_ascii=False, indent=2)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


def write_text_atomic(path, text):
    """Task#51: 原子写文本(md报告), 机制同write_json_atomic。"""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + '.tmp'
    with open(tmp, 'w', encoding='utf-8') as f:
        f.write(text)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


def build_md(payload, prev_meta) -> str:
    """人读版报告, 含与昨日meta的CAGR差值。"""
    a = payload['no_overlay']
    b = payload['ice_overlay']
    lines = []
    lines.append(f"# 每日滚动回测报告 {payload['end_date']}"
                 + (f" ({payload['tag']})" if payload['tag'] else ''))
    lines.append('')
    lines.append(f"- 生成时间: {payload['generated_at']}")
    lines.append(f"- 回测区间: {payload['start_date']} ~ {payload['end_date']}"
                 f" ({'全量口径' if payload['is_full_period'] else '短窗口(CAGR无参考意义)'})")
    lines.append(f"- 策略组合: " + ' / '.join(CN_NAMES[s] for s in STRATEGIES))
    lines.append(f"- 总耗时: {payload['total_elapsed_sec']}s")
    lines.append('')
    lines.append('## 两口径指标')
    lines.append('')
    lines.append('| 口径 | CAGR | MDD | 胜率 | 交易数 | 期末NAV | 耗时 |')
    lines.append('|---|---|---|---|---|---|---|')
    for blk, name in ((a, '无overlay'), (b, f'生产口径R2(ice+boost+gate)')):
        lines.append(f"| {name} | {blk['cagr_pct']:+.2f}% "
                     f"| {blk['max_drawdown_pct']:.2f}% "
                     f"| {blk['win_rate_pct']:.2f}% | {blk['n_trades']} "
                     f"| {blk['final_nav']:,.2f} | {blk['elapsed_sec']}s |")
    lines.append('')

    # 与昨日meta对比
    lines.append('## 与上次滚动对比')
    lines.append('')
    if prev_meta and prev_meta.get('start_date') == payload['start_date']:
        d_no = a['cagr_pct'] - prev_meta.get('cagr_no_overlay', 0)
        d_ice = b['cagr_pct'] - prev_meta.get('cagr_ice', 0)
        lines.append(f"- 上次: end={prev_meta.get('last_end_date')} "
                     f"(updated_at={prev_meta.get('updated_at')})")
        lines.append(f"- ΔCAGR(无overlay): {d_no:+.2f}pp "
                     f"({prev_meta.get('cagr_no_overlay')}% → {a['cagr_pct']}%)")
        lines.append(f"- ΔCAGR(生产口径R2): {d_ice:+.2f}pp "
                     f"({prev_meta.get('cagr_ice')}% → {b['cagr_pct']}%)")
    elif prev_meta:
        lines.append(f"- 上次meta口径不同(start={prev_meta.get('start_date')} "
                     f"vs 本次{payload['start_date']}), 差值不可比, 跳过")
    else:
        lines.append('- 无历史meta, 首次滚动, 差值 N/A')
    lines.append('')

    # 数据质量
    lines.append('## 数据质量(近5交易日hour覆盖率)')
    lines.append('')
    lines.append('| 日期 | 总行数 | 有hour行 | 覆盖率 |')
    lines.append('|---|---|---|---|')
    low_cov = False
    for d, c in sorted(payload['hour_coverage'].items()):
        lines.append(f"| {d} | {c['total']} | {c['with_hour']} "
                     f"| {c['coverage_pct']}% |")
        if c['coverage_pct'] < 50:
            low_cov = True
    if low_cov:
        lines.append('')
        lines.append('> ⚠️ 存在hour覆盖率<50%的交易日(BaoStock被ban期间腾讯降级只补日K), '
                     '引擎近端买卖依赖hour数据, 近端交易/NAV可能失真。')
    lines.append('')

    # 近5交易日交易 (以生产口径R2=ice+boost为主)
    lines.append(f"## 近{RECENT_DAYS}个交易日新增交易 "
                 f"(生产口径R2, 共{len(b['recent_trades'])}笔; "
                 f"无overlay口径{len(a['recent_trades'])}笔)")
    lines.append('')
    if b['recent_trades']:
        lines.append('| 代码 | 策略 | 买日 | 买价 | 卖日 | 卖价 | 收益% | 原因 |')
        lines.append('|---|---|---|---|---|---|---|---|')
        for t in b['recent_trades']:
            cn = CN_NAMES.get(t['strategy_name'], t['strategy_name'])
            lines.append(f"| {t['code']} | {cn} | {t['buy_date']} "
                         f"| {t['buy_price']} | {t['sell_date']} "
                         f"| {t['sell_price']} | {t['profit_pct']:+.2f} "
                         f"| {t['reason']} |")
    else:
        lines.append('(窗口内无已平仓交易; 注意引擎trades不含期末仍持仓单)')
    lines.append('')
    return '\n'.join(lines)


def solo_refresh_in_progress() -> str:
    """Task#42守卫: 检测solo档案是否正在被刷新(首夜混合口径事故复盘)。

    两个信号任一命中即视为刷新进行中, 返回原因字符串(空串=未命中):
      1. weekly_solo_refresh 进程存活 (pgrep -f)
      2. 任一在产solo档案mtime晚于本滚动进程启动时刻(=滚动运行期间被改写)
    """
    try:
        r = subprocess.run(['pgrep', '-f', 'weekly_solo_refresh'],
                           capture_output=True, text=True, timeout=10)
        if r.returncode == 0 and r.stdout.strip():
            return f"weekly_solo_refresh进程在跑(pid={r.stdout.split()[0]})"
    except Exception:
        pass  # pgrep不可用不阻断, 继续mtime判定
    for name in STRATEGIES:
        path = os.path.join(SOLO_DIR, f'{name}_trades.json')
        try:
            if os.path.getmtime(path) > PROC_START_TS:
                return f"{name}_trades.json mtime晚于本滚动启动时刻(刷新中)"
        except OSError:
            continue  # 档案不存在交由rebuild内fallback处理
    return ''


def rebuild_frontend_summary(payload):
    """仅全量口径: 重建 strategies_summary.json (保持现有结构)。
    strategies数组=在产5策略最新solo档案指标; combined_5slot=本次滚动组合结果。
    Task#42守卫: solo刷新进行中 → 跳过strategies重建只更新combined, 防混合口径。"""
    old = load_json(FRONTEND_SUMMARY) or {}
    old_by_name = {s.get('name'): s for s in old.get('strategies', [])}
    end_date = payload['end_date']

    guard_reason = solo_refresh_in_progress()
    if guard_reason:
        append_alert(f"solo刷新进行中, strategies待下轮重建 ({guard_reason})")

    strategies = []
    if guard_reason:
        # 守卫命中: 不读solo档案(可能写入半截), 整体沿用前端现有strategies数组
        strategies = old.get('strategies', [])
    else:
        for name in STRATEGIES:
            solo = load_json(os.path.join(SOLO_DIR, f'{name}_trades.json'))
            entry = {'name': name, 'display_name': CN_NAMES[name]}
            if solo and 'summary' in solo:
                s = solo['summary']
                entry.update({
                    'cagr': s['cagr_pct'], 'trades': s['n_trades'],
                    'win_rate': s['win_rate_pct'], 'mdd': s['max_drawdown_pct'],
                    'avg_return': s['avg_profit_pct'], 'final_nav': s['final_nav'],
                    'verified': True,
                })
                # solo档案过期判定: end_date落后最新交易日超过SOLO_STALE_DAYS天
                try:
                    gap = (datetime.strptime(end_date, '%Y-%m-%d')
                           - datetime.strptime(s['end_date'], '%Y-%m-%d')).days
                except Exception:
                    gap = None
                if gap is None or gap > SOLO_STALE_DAYS:
                    entry['verified_note'] = (
                        f"solo档案截至{s.get('end_date')}, 已过期(>{SOLO_STALE_DAYS}天), "
                        f"沿用现档案值, 待weekly_solo_refresh刷新")
            elif name in old_by_name:
                entry = dict(old_by_name[name])
                entry['display_name'] = CN_NAMES[name]
                entry['verified_note'] = '无solo档案, 沿用前端旧值, 待weekly_solo_refresh生成'
            else:
                entry.update({'cagr': None, 'trades': None, 'win_rate': None,
                              'mdd': None, 'avg_return': None, 'final_nav': None,
                              'verified': False,
                              'verified_note': '无solo档案且无历史值'})
            strategies.append(entry)

    a = payload['no_overlay']
    b = payload['ice_overlay']
    # Task#204: combined主值=生产口径R2; 键名'ice_overlay'/'caliber'保持稳定
    # 防消费方断裂, 内容升级为R2口径; 新增'no_overlay'子键保留纯策略对照
    # Task#319: 内容再升级为含gate的新基线口径(沿用#204模式: 键名不动)
    combined = {
        'name': '5策略统一资金池(等权再平衡)',
        'caliber': (f'生产口径R2({ICE_SPEC}+boost{DD_BOOST}'
                    f'+gate{PROMO_GATE})'),
        'cagr': b['cagr_pct'], 'mdd': b['max_drawdown_pct'],
        'trades': b['n_trades'], 'win_rate': b['win_rate_pct'],
        'avg_return': b['avg_profit_pct'], 'final_nav': b['final_nav'],
        'initial_capital': 1000000,
        'ice_overlay': {
            'position_scale': ICE_SPEC,
            'dd_boost': DD_BOOST,
            'promo_gate': PROMO_GATE,
            'cagr': b['cagr_pct'], 'mdd': b['max_drawdown_pct'],
            'trades': b['n_trades'], 'win_rate': b['win_rate_pct'],
            'final_nav': b['final_nav'],
        },
        'no_overlay': {
            'cagr': a['cagr_pct'], 'mdd': a['max_drawdown_pct'],
            'trades': a['n_trades'], 'win_rate': a['win_rate_pct'],
            'final_nav': a['final_nav'],
        },
    }

    new = dict(old)  # 保留其他顶层键(如combined_3slot历史存档)原样
    new['last_updated'] = datetime.now().strftime('%Y-%m-%d')
    new['backtest_period'] = f"{payload['start_date']} ~ {end_date}"
    new['strategies'] = strategies
    new['combined_5slot'] = combined
    write_json_atomic(FRONTEND_SUMMARY, new)
    if guard_reason:
        print(f"[Rolling] 前端summary部分重建(守卫命中: {guard_reason}): "
              f"combined_5slot已更新, strategies沿用现值待下轮")
    else:
        print(f"[Rolling] 前端summary已重建: {FRONTEND_SUMMARY}")


def refresh_combined_trades(block_full, start, end):
    """Task#46: 全量口径时把组合完整结果落盘替换组合trades档案。
    Task#204起: 档案口径=生产口径R2(ice+boost), 与看板combined主值同源。

    结构与dashboard.js adaptData消费口径严格对齐(顶层5键):
      mode='unified_pool' | combined_summary=引擎summary(13键) |
      strategies=在产5策略名list | daily_nav={date:nav} | trades=[11键/笔]
    首次写入前把现文件(7/25含退役策略旧档案)备份为 .bak_t46, 只备份一次。
    """
    bak = COMBINED_TRADES_PATH + '.bak_t46'
    if os.path.exists(COMBINED_TRADES_PATH) and not os.path.exists(bak):
        shutil.copy2(COMBINED_TRADES_PATH, bak)
        print(f"[Rolling] 组合档案旧版已备份: {bak}")
    payload = {
        'mode': 'unified_pool',
        'combined_summary': block_full['summary'],
        'strategies': list(STRATEGIES),
        'daily_nav': block_full['_full']['daily_nav'],
        'trades': block_full['_full']['trades'],
    }
    write_json_atomic(COMBINED_TRADES_PATH, payload)
    print(f"[Rolling] 组合trades档案已刷新: {COMBINED_TRADES_PATH} "
          f"({len(payload['trades'])}笔, {start}~{end})")


def main():
    parser = argparse.ArgumentParser(description='每日滚动回测(Task#5)')
    parser.add_argument('--start', default=FULL_START,
                        help=f'起始日期(默认{FULL_START}=全量口径)')
    parser.add_argument('--end', default='auto',
                        help='结束日期(默认auto=stocks.db最新交易日)')
    parser.add_argument('--skip-frontend', action='store_true', default=False,
                        help='调试用: 不重建frontend/data/strategies_summary.json')
    parser.add_argument('--tag', default='',
                        help='产物文件名后缀标注(如 smoke), 用于区分调试产物')
    args = parser.parse_args()

    t_start = time.time()
    end = get_latest_trade_date() if args.end == 'auto' else args.end
    start = args.start
    is_full = (start == FULL_START)
    # Task#46: 生产口径三条件(全量起点+未跳过前端+无tag), 调试跑零触碰生产meta/组合档案
    is_production = is_full and not args.skip_frontend and not args.tag
    tag_sfx = f"_{args.tag}" if args.tag else ''
    ymd = end.replace('-', '')

    print(f"[Rolling] 窗口 {start} ~ {end} | 全量口径={is_full} "
          f"| 生产口径={is_production} | tag={args.tag or '-'}")
    os.makedirs(ROLLING_LOG_DIR, exist_ok=True)
    os.makedirs(ROLLING_DATA_DIR, exist_ok=True)

    # 两口径回测: (a)无overlay对照 (b)生产口径=ice+boost+gate(Task#204/#319)
    # 生产口径时(b)携带完整trades/daily_nav供组合档案刷新(Task#46)
    block_a = run_one(None, start, end)
    block_b = run_one(ICE_SPEC, start, end, keep_full=is_production,
                      dd_boost=DD_BOOST, promo_gate=PROMO_GATE)
    block_b_full = block_b.pop('_full', None)   # 全量明细不随payload落盘

    total_elapsed = round(time.time() - t_start, 1)
    payload = {
        'generated_at': now_str(),
        'start_date': start,
        'end_date': end,
        'is_full_period': is_full,
        'tag': args.tag,
        'total_elapsed_sec': total_elapsed,
        'strategies': STRATEGIES,
        'hour_coverage': get_hour_coverage(block_a['recent_trading_days']),
        'no_overlay': block_a,
        'ice_overlay': block_b,
    }

    # 落盘 JSON + MD
    prev_meta = load_json(META_PATH)
    json_path = os.path.join(ROLLING_LOG_DIR, f'rolling_{ymd}{tag_sfx}.json')
    md_path = os.path.join(ROLLING_LOG_DIR, f'rolling_{ymd}{tag_sfx}.md')
    write_json_atomic(json_path, payload)
    write_text_atomic(md_path, build_md(payload, prev_meta))
    print(f"[Rolling] JSON: {json_path}\n[Rolling] MD:   {md_path}")

    # meta写入门禁(Task#46): 仅生产口径才写meta与prev备份, 调试跑零触碰
    # (Task#42事故复盘: 带tag短窗口smoke曾把生产meta覆盖成短窗口值)
    if is_production:
        # meta三备份: 写前把上一份备份到 prev (meta + prev + 当日json 共三份)
        if os.path.exists(META_PATH):
            # Task#51: prev备份也走原子替换(copy到.tmp再replace)
            shutil.copy2(META_PATH, META_PREV_PATH + '.tmp')
            os.replace(META_PREV_PATH + '.tmp', META_PREV_PATH)
            print(f"[Rolling] meta已备份: {META_PREV_PATH}")
        meta = {
            'last_end_date': end,
            'start_date': start,
            'cagr_no_overlay': block_a['cagr_pct'],
            'cagr_ice': block_b['cagr_pct'],
            'nav': block_a['final_nav'],
            'nav_ice': block_b['final_nav'],
            'mdd_no_overlay': block_a['max_drawdown_pct'],
            'mdd_ice': block_b['max_drawdown_pct'],
            'updated_at': now_str(),
        }
        write_json_atomic(META_PATH, meta)
        print(f"[Rolling] meta已更新: {META_PATH}")
    else:
        print(f"[Rolling] 跳过meta写入(非生产口径: is_full={is_full}, "
              f"skip_frontend={args.skip_frontend}, tag={args.tag or '-'})")

    # 前端summary重建: 双保险 —— 必须全量口径 且 未--skip-frontend
    if is_full and not args.skip_frontend:
        rebuild_frontend_summary(payload)
    else:
        print(f"[Rolling] 跳过前端summary重建 "
              f"(is_full={is_full}, skip_frontend={args.skip_frontend})")

    # 组合trades档案刷新(Task#46/#204): 仅生产口径且完整明细在手, R2口径
    if is_production and block_b_full:
        block_b['_full'] = block_b_full  # refresh按block结构消费, 用后即弃
        refresh_combined_trades(block_b, start, end)
        block_b.pop('_full', None)
    else:
        print(f"[Rolling] 跳过组合trades档案刷新(生产口径={is_production})")

    # 运行时长告警
    if total_elapsed > RUNTIME_ALERT_MIN * 60:
        append_alert(f"daily_rolling_backtest 全程{total_elapsed / 60:.1f}分钟 "
                     f"> {RUNTIME_ALERT_MIN}分钟阈值 (窗口 {start}~{end})")

    print(f"\n[Rolling] 全部完成, 总耗时 {total_elapsed}s")


if __name__ == '__main__':
    main()
