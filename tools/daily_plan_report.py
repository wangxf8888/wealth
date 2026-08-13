#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""[Task#72] 早间计划报告生成器 —— 每日双报告体系之"事前计划"。

生成 reports/YYYYMMDD_plan.md, 四大区块:
  1. 今日操作计划: 5槽位候选名单 + 预计买入金额(NAV/5×冰点scale, 昨日涨停家数查库预告)
  2. 三分支预案(核心): 逐候选open_rate网格扫描live复筛路径 → 触发窗口决策表
     ("低开-4%~0才买 / 高开放弃 / 涨停开盘拒单")
  3. 持仓卖出计划: 每仓触发线位(trailing/SL/TP/定时点) + 10秒守护值守说明
  4. 风险提示: 数据源状态/成本红线/当日特殊事件

一致性设计(回测验证≈100%的根基):
- 触发窗口不是"复述策略文档", 而是把候选open按网格注入
  RealtimeDataFeed.set_live_mode → strategy.get_candidates 复筛 →
  execution_core.evaluate_open_entry 守卫 —— 与 morning_decision.morning_evaluate
  同一条判定链路, 构造性一致。
- 策略类按 candidates json 内记录的 module/class 加载(而非当前config),
  历史日重放用"当时可见"的策略版本, 免疫策略切换漂移。
- 冰点scale直接 import morning_decision.compute_position_scale(同一份实现)。

回测验证(上线前必过):
  --backtest-date D : 用D日DB内真实open跑同款判定, 逐slot对比实际decision json
  --verify N        : 近N个有decision的交易日逐日验证一致率

数据纪律: 只读candidates/positions/decision json与stocks.db, 不写交易链路
任何文件; 生成失败只写scheduler_alerts告警。

用法:
  python3 tools/daily_plan_report.py                    # 今晨(cron 8:45)
  python3 tools/daily_plan_report.py --date 2026-07-30  # 指定交易日
  python3 tools/daily_plan_report.py --backtest-date 2026-07-24
  python3 tools/daily_plan_report.py --verify 10
"""
import argparse
import importlib
import json
import os
import sqlite3
import sys
from datetime import datetime

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT_ROOT)

import trading_rules
from execution_core import evaluate_open_entry
from realtime.config import (DATA_DB, OUTPUT_DIR, MAX_SLOT_PER_STRATEGY,
                             strategy_display_name)
from realtime.data_feed import RealtimeDataFeed
from realtime.morning_decision import (compute_final_scale,
                                       fetch_test_open_prices,
                                       get_sell_params)
from realtime.position_tracker import load_positions, get_buy_amount

RT_DIR = OUTPUT_DIR
REPORT_DIR = '/home/AIWealth/reports'
BAN_STATUS = '/home/AIWealth/data/baostock_ban_status.json'
ALERT_LOG = '/home/AIWealth/logs/realtime/scheduler_alerts.log'

# open_rate网格: 主板±11覆盖, 创/科/北候选到±20.5(涨停上界之外含一格)
GRID = [round(x * 0.5, 1) for x in range(-41, 42)]


def _load_json(path):
    if not os.path.exists(path):
        return None
    with open(path, encoding='utf-8') as f:
        return json.load(f)


def load_strategy_from_slotdata(slot_data):
    """按candidates json内记录的module/class加载(历史日=当时可见版本)。"""
    module = importlib.import_module(slot_data['module'])
    return getattr(module, slot_data['strategy_class'])()


# =====================================================================
# 三分支预案: open_rate网格扫描(与morning_evaluate同链路)
# =====================================================================

def scan_trigger_windows(trade_date, strategies_data, feed=None):
    """对每slot每候选扫open_rate网格, 返回:
    {slot_id: {code: {'windows': [(lo,hi),...], 'limit_up_rate': float}}}
    windows=会被买入判定命中的open_rate闭区间列表(网格粒度0.5pp)。
    """
    own = feed is None
    if own:
        feed = RealtimeDataFeed(DATA_DB)
    result = {}
    for slot_id, slot_data in strategies_data.items():
        cands = slot_data.get('candidates', [])
        if not cands:
            result[slot_id] = {}
            continue
        try:
            strategy = load_strategy_from_slotdata(slot_data)
        except Exception as e:
            result[slot_id] = {'_error': f'策略加载失败: {e}'}
            continue
        sc_map = {c['code']: c.get('signal_close', 0) for c in cands
                  if c.get('signal_close', 0) > 0}
        name_map = {c['code']: c.get('name', '') for c in cands}
        hits = {code: [] for code in sc_map}
        for r in GRID:
            opens = {code: round(sc * (1 + r / 100), 2)
                     for code, sc in sc_map.items()}
            feed.set_live_mode(trade_date, opens)
            try:
                filtered = set(strategy.get_candidates(trade_date, feed))
            except Exception:
                filtered = set()
            for code, sc in sc_map.items():
                if code not in filtered:
                    continue
                # 入场守卫同款(涨停开盘拒单; 除权无法预知, 报告尾注说明)
                _, reject = evaluate_open_entry(
                    code=code, strategy=strategy.name, slot_id=slot_id,
                    date=trade_date, open_price=opens[code],
                    exchange_preclose=sc, prev_close=sc,
                    signal_ref_close=sc,
                    is_st=trading_rules.is_st_name(name_map.get(code, '')),
                    limit_basis=sc)
                if reject is None:
                    hits[code].append(r)
        slot_res = {}
        for code, rs in hits.items():
            # 连续网格点合并成区间
            windows = []
            for r in rs:
                if windows and abs(r - windows[-1][1] - 0.5) < 1e-6:
                    windows[-1][1] = r
                else:
                    windows.append([r, r])
            lu, _ = trading_rules.limit_prices(
                code, sc_map[code],
                trading_rules.is_st_name(name_map.get(code, '')))
            lu_rate = (lu / sc_map[code] - 1) * 100 if sc_map[code] else None
            slot_res[code] = {'windows': [tuple(w) for w in windows],
                              'limit_up_rate': lu_rate}
        result[slot_id] = slot_res
    if own:
        feed.close()
    return result


def _branch_labels(windows):
    """把触发窗口翻译成 高开/平开/低开 三分支人话。"""
    if not windows:
        return "任何开盘都不触发(仅入围候选池)"
    covers = lambda lo, hi: any(w[0] <= hi and w[1] >= lo for w in windows)
    branches = []
    branches.append("低开" + ("✅" if covers(-20.5, -0.5) else "✖"))
    branches.append("平开" + ("✅" if covers(0.0, 0.0) else "✖"))
    branches.append("高开" + ("✅" if covers(0.5, 20.5) else "✖"))
    win_txt = ' ∪ '.join(
        (f"[{w[0]:+.1f}%, {w[1]:+.1f}%]" if w[0] != w[1]
         else f"{{{w[0]:+.1f}%}}") for w in windows)
    # 注意: 本函数产物嵌入Markdown表格单元格, 禁用'|'字符
    return f"{' '.join(branches)} — 窗口 {win_txt}"


# =====================================================================
# 计划报告生成
# =====================================================================

def generate_plan(trade_date, quotes=None, tag=''):
    """生成计划报告md。quotes非None=回测重放模式(不影响文件命名tag区分)。"""
    cand_file = os.path.join(RT_DIR,
                             f"candidates_{trade_date.replace('-', '')}.json")
    cand = _load_json(cand_file)
    conn = sqlite3.connect(DATA_DB)
    parts = [f"# 🌅 每日操作计划 — {trade_date}",
             f"\n> 生成时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}"
             f" | 判定链路与9:25 morning_decision构造性一致 | Task#72\n"]

    if cand is None:
        parts.append(f"⚠️ 候选文件不存在({cand_file}), 昨晚22:00候选生成未执行。"
                     f"今日9:25决策也将因此无法执行——**需人工立即补跑** "
                     f"`python3 realtime/generate_candidates.py`\n")
        strategies_data = {}
    else:
        strategies_data = cand.get('strategies', {})

    # --- 仓位系数预告: 冰点×回撤叠乘(Task#73), 与9:25实际计算同一份函数 ---
    ps = compute_final_scale(conn, trade_date)
    pos_data = load_positions()
    account = pos_data.get('account', {})
    positions = pos_data.get('positions', [])
    buy_amt = get_buy_amount(pos_data, scale=ps['final_scale'])
    n_cands = sum(len(s.get('candidates', [])) for s in strategies_data.values())

    parts.append("## 一、今日操作计划总览\n")
    parts.append(f"- 总净值 ¥{account.get('total_nav', 0):,.0f} | "
                 f"现金 ¥{account.get('cash', 0):,.0f} | "
                 f"在手持仓 {len(positions)}个slot")
    ice_txt = (f"❄️ **冰点减仓生效**: 昨日({ps['prev_date']})非ST涨停"
               f"{ps['limitup_cnt']}家<35 → 新仓×{ps['ice_scale']}"
               if ps['ice_scale'] != 1.0 else
               f"昨日({ps['prev_date']})非ST涨停{ps['limitup_cnt']}家≥35, 满仓系数1.0")
    parts.append(f"- 冰点overlay预告: {ice_txt}")
    # 回撤作战手册预告(Task#73): L0静默一行, L1+显著提示
    if ps['halt']:
        parts.append(f"- 🚨 **回撤熔断L3生效中**: 明日停止一切新开仓 "
                     f"(解除: `python3 tools/drawdown_guard.py ack`)")
    elif ps['drawdown_scale'] != 1.0:
        wl_txt = (f", 仅白名单可开仓: {ps['whitelist']}"
                  if ps['whitelist'] else "")
        parts.append(f"- ⚠️ 回撤作战手册L{ps['drawdown_level']}生效: "
                     f"回撤系数×{ps['drawdown_scale']}{wl_txt}")
    else:
        parts.append("- 回撤作战手册: L0正常(回撤系数1.0)")
    parts.append(f"- **预计每slot买入金额: ¥{buy_amt:,.0f}** "
                 f"(NAV/5×{ps['final_scale']}, 冰点{ps['ice_scale']}×"
                 f"回撤{ps['drawdown_scale']})")
    parts.append(f"- 候选总数: {n_cands}只 / {len(strategies_data)}个slot | "
                 f"每slot最多买入{MAX_SLOT_PER_STRATEGY}只(按策略排序取top1)\n")

    # --- 三分支预案决策表 ---
    parts.append("## 二、三分支预案(高开/平开/低开决策表)\n")
    if strategies_data:
        parts.append("> 窗口含义: 明晨9:25开盘价落入触发窗口即买入(网格粒度0.5pp, "
                     "与实际决策同一条复筛+守卫链路); 开盘≥涨停价一律拒单。\n")
        feed = RealtimeDataFeed(DATA_DB)
        scan = scan_trigger_windows(trade_date, strategies_data, feed)
        feed.close()
        for slot_id in sorted(strategies_data):
            s = strategies_data[slot_id]
            cands = s.get('candidates', [])
            strat_name = s.get('strategy_name', '?')
            # 策略名中文主导(用户指令2026-07-30)
            parts.append(f"### {slot_id} · {strategy_display_name(strat_name)} "
                         f"({len(cands)}只候选)\n")
            if not cands:
                parts.append("今日无候选, 本slot空仓待机。\n")
                continue
            slot_scan = scan.get(slot_id, {})
            if '_error' in slot_scan:
                parts.append(f"⚠️ {slot_scan['_error']}\n")
                continue
            try:
                sp = get_sell_params(load_strategy_from_slotdata(s))
            except Exception as e:
                sp = None
                parts.append(f"⚠️ 卖出参数读取失败: {e}\n")
            parts.append("| 候选 | 昨收 | 开盘触发窗口(三分支) | 买入后卖出规则 |")
            parts.append("|---|---|---|---|")
            for c in cands[:12]:
                code, nm = c['code'], c.get('name', '')
                info = slot_scan.get(code, {})
                branch = _branch_labels(info.get('windows', []))
                if sp is None:
                    sell_txt = '?'
                elif sp['sell_mode'] == 'timed':
                    sell_txt = f"次日{sp.get('sell_time','10:30')}定时卖(无盘中TP/SL)"
                elif sp['sell_mode'] == 'trailing':
                    sell_txt = (f"高点回落{sp['trailing_pp']}pp止盈 / "
                                f"SL{sp['sl_pct']*100:+.0f}%")
                else:
                    sell_txt = (f"TP{sp['tp_pct']*100:+.0f}% / "
                                f"SL{sp['sl_pct']*100:+.0f}%")
                parts.append(f"| {code} {nm} | {c.get('signal_close','?')} "
                             f"| {branch} | {sell_txt} |")
            if len(cands) > 12:
                parts.append(f"\n(候选共{len(cands)}只, 表内展示前12; "
                             f"排序靠前者同窗口命中时优先成交)")
            parts.append("")
    else:
        parts.append("无候选数据, 今日预计全slot空仓待机。\n")

    # --- 持仓卖出计划 ---
    parts.append("## 三、持仓卖出计划\n")
    if positions:
        for p in positions:
            mode = p.get('sell_mode', 'fixed')
            if mode == 'timed':
                sa = p.get('sell_at') or {}
                trig = f"定时卖出 {sa.get('date','?')} {sa.get('time','10:30')}"
            elif mode == 'trailing':
                peak = p.get('peak_price') or p.get('buy_price')
                line = round(peak * (1 - p.get('trailing_pp', 2.0) / 100), 2)
                trig = (f"trailing线 {line}(峰值{peak}回落"
                        f"{p.get('trailing_pp')}pp) | 硬SL {p.get('sl_price')}")
            else:
                trig = f"TP {p.get('tp_price')} / SL {p.get('sl_price')}"
            trig += f" | 到期兜底 {p.get('expire_date','?')}"
            parts.append(f"- **{p['code']} {p.get('name','')}** "
                         f"[{p.get('slot_id')}:{strategy_display_name(p.get('strategy'))}] "
                         f"成本{p.get('buy_price')} → {trig}")
        parts.append("\n> 值守: 9:25拉起10秒级intraday_monitor守护进程盘中盯线"
                     "(Task#22), daemon失联时position_tracker半小时cron兜底。")
    else:
        parts.append("当前空仓, 无卖出计划。")
    parts.append("")

    # --- 风险提示 ---
    parts.append("## 四、风险提示\n")
    ban = _load_json(BAN_STATUS) or {}
    if not ban.get('recovered', True):
        parts.append(f"- 🔴 **BaoStock封禁中**(banned_at {ban.get('banned_at')}): "
                     f"日K走腾讯降级源(hour列缺失), 分钟线停更; "
                     f"9:25行情走腾讯+Sina双源, 不受影响")
    else:
        parts.append("- 数据源正常(BaoStock可用)")
    parts.append("- 成本红线: 回测口径含费用假设, 实盘滑点超预期时暂停加仓复核")
    parts.append("- 除权除息: 若候选明日除权, 9:25会被evaluate_open_entry自动拦截"
                 "(本表窗口未预知除权, 以现场拦截为准)")
    # 昨日未闭环告警提示
    if os.path.exists(ALERT_LOG):
        with open(ALERT_LOG, encoding='utf-8') as f:
            tail = f.readlines()[-3:]
        if tail:
            parts.append("- 近期scheduler告警(确认已闭环):")
            for ln in tail:
                parts.append(f"  - `{ln.strip()}`")
    conn.close()

    os.makedirs(REPORT_DIR, exist_ok=True)
    out = os.path.join(REPORT_DIR,
                       f"{trade_date.replace('-', '')}_plan{tag}.md")
    tmp = out + '.tmp'
    with open(tmp, 'w', encoding='utf-8') as f:
        f.write('\n'.join(parts))
    os.replace(tmp, out)
    _update_index()
    print(f"[plan] 已生成: {out}")
    return out


def _update_index():
    files = sorted((f for f in os.listdir(REPORT_DIR)
                    if f.endswith('.md') and not f.startswith('.')),
                   reverse=True)
    with open(os.path.join(REPORT_DIR, 'index.json'), 'w',
              encoding='utf-8') as f:
        json.dump({'files': files,
                   'updated_at': datetime.now().strftime('%Y-%m-%d %H:%M:%S')},
                  f, ensure_ascii=False, indent=1)


# =====================================================================
# 回测验证: 计划触发窗口 vs 实际decision一致率
# =====================================================================

def backtest_verify_one(date, verbose=True):
    """对历史日D: 用D日DB真实open判定'计划会触发谁', 对比实际decision json。

    返回 (n_compared, n_match, skipped_reason)。
    比较层级=逐slot逐候选的 触发/不触发 二值判定(meet_condition语义),
    这正是计划报告触发窗口的谓词, 一致率应≈100%。

    可解释豁免(2026-07-29核查后加入, 三类均为历史数据问题而非报告逻辑漂移):
    A. 旧格式candidates(framework<2.0, slot无module字段) → 无法按当时版本
       加载策略, 全日跳过。(实例: 2026-07-14)
    B. 策略文件版本漂移: diff笔发生时, 若该slot策略.py的mtime晚于候选文件
       generated_at(候选生成后策略被原地改参, 如Task#40 07-25给S1加
       min_prev_turn=3), 该笔归入豁免单列——历史行为已不可用当前代码复现。
       (实例: 2026-07-16 sh.605255 prev_turn=0.89, 旧版无下限时通过)
       注: 豁免仅在diff出现时启用, 无diff的日子照常全量比对, 不掩盖真漂移。
    C. decision补跑污染: decision全slot的meet_condition为空, 但positions
       closed_trades显示当日确有买入(decision文件被事后补跑覆盖, 内容与真实
       成交矛盾) → 全日跳过。(实例: 2026-07-22 decided_at=10:59:14,
       meet全空但sz.000786/sh.688549当日实际买入——报告预测反而与成交一致)
    """
    dstr = date.replace('-', '')
    cand = _load_json(os.path.join(RT_DIR, f'candidates_{dstr}.json'))
    dec = _load_json(os.path.join(RT_DIR, f'decision_{dstr}.json'))
    if cand is None or dec is None:
        return 0, 0, 'candidates/decision文件缺失'
    if dec.get('status') == 'aborted_quote_anomaly':
        return 0, 0, '当日行情源熔断中止(无可比决策)'
    if dec.get('market_filter'):
        return 0, 0, '当日被旧版大盘熔断拦截(规则已变更, 无逐票决策可比)'

    strategies_data = cand.get('strategies', {})
    # 豁免A: 旧格式candidates(无module字段)
    if any('module' not in s for s in strategies_data.values()):
        return 0, 0, '旧格式candidates(framework<2.0无module字段), 无法按当时版本重放'
    # 豁免C: decision补跑污染(meet全空但当日实际有成交)
    dec_met_any = any(s.get('meet_condition')
                      for s in dec.get('strategies', {}).values()
                      if isinstance(s, dict))
    if not dec_met_any:
        pos = load_positions()
        traded = [t for t in pos.get('closed_trades', [])
                  if t.get('buy_date') == date]
        traded += [p for p in pos.get('positions', [])
                   if p.get('buy_date') == date]
        if traded:
            return 0, 0, ('decision补跑污染(meet全空但当日实际买入'
                          f"{[t['code'] for t in traded]}), 文件与成交矛盾")

    cand_gen_at = cand.get('generated_at', '')
    all_codes = list({c['code'] for s in strategies_data.values()
                      for c in s.get('candidates', [])})
    conn = sqlite3.connect(DATA_DB)
    quotes = fetch_test_open_prices(conn, all_codes, date)
    conn.close()

    feed = RealtimeDataFeed(DATA_DB)
    n_cmp, n_match, diffs, exempts = 0, 0, [], []
    for slot_id, slot_data in strategies_data.items():
        cands = slot_data.get('candidates', [])
        if not cands:
            continue
        dslot = dec.get('strategies', {}).get(slot_id)
        if dslot is None or dslot.get('error'):
            continue
        actual_met = {m['code'] for m in dslot.get('meet_condition', [])}
        try:
            strategy = load_strategy_from_slotdata(slot_data)
        except Exception as e:
            diffs.append(f"{slot_id}: 策略加载失败 {e}")
            continue
        # 用真实open跑同款复筛+守卫(=计划触发窗口谓词在真实r处取值)
        opens = {c['code']: quotes[c['code']]['open']
                 for c in cands if c['code'] in quotes}
        feed.set_live_mode(date, opens)
        try:
            filtered = set(strategy.get_candidates(date, feed))
        except Exception as e:
            diffs.append(f"{slot_id}: get_candidates异常 {e}")
            continue
        for c in cands:
            code = c['code']
            if code not in quotes:      # 当日无行(停牌), 决策侧也拿不到→跳过
                continue
            sc = c.get('signal_close', 0)
            open_p = quotes[code]['open']
            predicted = False
            if code in filtered and sc > 0:
                _, reject = evaluate_open_entry(
                    code=code, strategy=strategy.name, slot_id=slot_id,
                    date=date, open_price=open_p,
                    exchange_preclose=quotes[code].get('preclose', sc),
                    prev_close=sc, signal_ref_close=sc,
                    is_st=trading_rules.is_st_name(c.get('name', '')),
                    limit_basis=sc)
                predicted = reject is None
            actual = code in actual_met
            if predicted != actual and _strategy_modified_after(
                    slot_data.get('module'), cand_gen_at):
                # 豁免B: 候选生成后策略被改参, 该笔不可复现(不计入可比样本)
                exempts.append(f"{slot_id} {code}: 计划={predicted} "
                               f"实际={actual} — 策略{slot_data['module']}"
                               f"在候选生成({cand_gen_at})后被改参, 版本漂移豁免")
                continue
            n_cmp += 1
            if predicted == actual:
                n_match += 1
            else:
                diffs.append(f"{slot_id} {code}: 计划={predicted} "
                             f"实际={actual} open={open_p} sc={sc}")
    feed.close()
    if verbose:
        for d in diffs:
            print(f"    [DIFF] {d}")
        for e in exempts:
            print(f"    [豁免B] {e}")
    return n_cmp, n_match, None


def _strategy_modified_after(module_name, generated_at):
    """策略.py文件mtime是否晚于候选文件生成时刻(版本漂移判据)。"""
    if not module_name or not generated_at:
        return False
    path = os.path.join(PROJECT_ROOT, module_name.replace('.', '/') + '.py')
    if not os.path.exists(path):
        return False
    try:
        gen_ts = datetime.strptime(generated_at,
                                   '%Y-%m-%d %H:%M:%S').timestamp()
    except ValueError:
        return False
    return os.path.getmtime(path) > gen_ts


def verify_recent(n_days):
    """近n个有decision的交易日逐日验证。"""
    decs = sorted(f for f in os.listdir(RT_DIR)
                  if f.startswith('decision_') and f.endswith('.json'))
    dates = [f"{f[9:13]}-{f[13:15]}-{f[15:17]}" for f in decs][-n_days:]
    total_cmp, total_match = 0, 0
    print(f"{'日期':<12} {'可比笔数':>6} {'一致':>4} {'一致率':>8}  备注")
    for d in dates:
        n_cmp, n_match, skip = backtest_verify_one(d)
        if skip:
            print(f"{d:<12} {'-':>6} {'-':>4} {'-':>8}  跳过: {skip}")
            continue
        rate = n_match / n_cmp * 100 if n_cmp else 100.0
        flag = '' if n_match == n_cmp else '  ← 漂移!'
        print(f"{d:<12} {n_cmp:>6} {n_match:>4} {rate:>7.1f}%{flag}")
        total_cmp += n_cmp
        total_match += n_match
    overall = total_match / total_cmp * 100 if total_cmp else 100.0
    print(f"\n总计: {total_match}/{total_cmp} 一致率 {overall:.1f}% "
          f"({'✅ 无漂移' if total_match == total_cmp else '🚨 存在漂移, 必须修复'})")
    return total_cmp, total_match


# =====================================================================
# main
# =====================================================================

def main():
    ap = argparse.ArgumentParser(description='早间计划报告(Task#72)')
    ap.add_argument('--date', default=None, help='交易日, 默认今日')
    ap.add_argument('--backtest-date', default=None,
                    help='历史日重放生成计划报告(_bt后缀)并对照实际decision')
    ap.add_argument('--verify', type=int, default=None,
                    help='近N个交易日一致率验证(不生成报告)')
    args = ap.parse_args()

    if args.verify:
        _, _ = verify_recent(args.verify)
        return 0
    if args.backtest_date:
        d = args.backtest_date
        generate_plan(d, tag='_bt')
        print(f"\n[plan] 对照 {d} 实际decision:")
        n_cmp, n_match, skip = backtest_verify_one(d)
        if skip:
            print(f"  跳过: {skip}")
        else:
            print(f"  一致率: {n_match}/{n_cmp}")
        return 0

    date = args.date or datetime.now().strftime('%Y-%m-%d')
    # 周末不生成(与交易cron同口径, 节假日由候选文件缺失兜底提示)
    if datetime.strptime(date, '%Y-%m-%d').weekday() >= 5:
        print(f"[plan] {date} 为周末, 跳过")
        return 0
    try:
        generate_plan(date)
        return 0
    except Exception as e:
        msg = f"计划报告生成失败 {date}: {type(e).__name__}: {e}"
        print(f"[plan] {msg}")
        try:
            with open(ALERT_LOG, 'a', encoding='utf-8') as f:
                f.write(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] "
                        f"⚠️ [REPORT] {msg}\n")
        except OSError:
            pass
        return 1


if __name__ == '__main__':
    sys.exit(main())
