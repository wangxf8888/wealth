#!/usr/bin/env python3
"""
盘中分钟级实时监控守护进程 (Task#22)
=====================================
交易时段 9:25-15:00 常驻, 每10秒轮询一次腾讯批量行情(全部code一次请求;
用户定案: 只有持仓股+候选股, 流量小, 10秒频率没问题)。
接口异常退避: 连续失败3次 → 30秒间隔+告警, 恢复后自动回10秒。

架构 (daemon 与 cron 协作):

  cron `25 9 * * 1-5` 拉起本进程 (内部flock防重复启动, 自判交易日, 15:00自然退出)
  ┌────────────────────────────────────────────────────────────────┐
  │ intraday_monitor.py  (10s轮询主循环, INTERVAL_SECONDS可调)     │
  │  1. 心跳: logs/realtime/intraday_monitor_heartbeat (每轮touch) │
  │  2. 持仓股: evaluate_position() ← Task#20区间触线判定共享函数  │
  │     触线 → close_position_locked(owner='daemon') 幂等锁平仓    │
  │     错过的触发 → process_missed_alert() 告警+企微(每日幂等)    │
  │  3. 候选股: 现价/相对昨收涨幅快照 → intraday_snapshot.json     │
  │     进入策略买入区间 → 信息性日志(不自动买入,                  │
  │     买入决策仍归 9:25 morning_decision)                        │
  └────────────────────────────────────────────────────────────────┘
  cron position_tracker.py auto-close (9:31 + 每30分钟, Task#20加密保持不动):
    daemon心跳 < 60s → cron自动跳过(防双写, 卖出检查由daemon负责)
    daemon心跳超时/缺失 → 写scheduler_alerts.log告警 + cron接管兜底

  幂等防线:
    ① 心跳跳过(主防线, 存活期间cron不碰positions.json)
    ② selling_locked持仓标记(二道锁, 先到者锁定, 他方跳过)
    daemon正常退出(15:00)时删除心跳文件, 让15:01收盘cron终检接管。

启动: cron拉起(见上) 或手工 python3 realtime/intraday_monitor.py
      测试: python3 realtime/intraday_monitor.py --once (单轮后退出, 不限时段)
"""
import sys
import os
import json
import fcntl
import time
import argparse
import importlib
from datetime import datetime

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT_ROOT)

from realtime.config import ACTIVE_STRATEGIES, OUTPUT_DIR, LOG_DIR
import realtime.position_tracker as pt
from execution_core import LiveBarAggregator, exit_engine_from_position

LOCK_FILE = os.path.join(LOG_DIR, 'intraday_monitor.lock')
SNAPSHOT_FILE = os.path.join(OUTPUT_DIR, 'intraday_snapshot.json')
# Task#27 统一双模执行框架 feature开关:
#   False(默认) = 旧路径 evaluate_position 日级区间判定(Task#20/22已充分验证,
#                 明晨首跑用此路径 —— 实盘稳定优先于架构演进)
#   True        = 新路径 execution_core.ExitEngine bar级挂单语义
#                 (首轮仍跑一次日级兜底捕捉启动前触发, 之后10s tick聚合
#                 进行中5min bar喂共享ExitEngine, 与回测同一份决策代码)
# 环境变量 USE_EXECUTION_CORE 三态(Task#45影子切换准备):
#   '0'(默认)  = off    旧路径执行
#   '1'        = on     新路径执行(正式切换态)
#   'shadow'   = shadow 影子模式: 新旧路径并行计算, 只执行旧路径,
#                新路径(共享ExitEngine bar级)纯观察, 触发事件+比对结果写
#                logs/realtime/shadow_exit_YYYYMMDD.jsonl。
#                周一实盘跑一天影子比对, 零差异后周二置'1'正式切换。
_MODE_RAW = os.environ.get('USE_EXECUTION_CORE', '0').strip().lower()
EXEC_CORE_MODE = {'0': 'off', '1': 'on',
                  'shadow': 'shadow'}.get(_MODE_RAW, 'off')
USE_EXECUTION_CORE = (EXEC_CORE_MODE == 'on')
# 轮询间隔(用户定案10秒: 只有持仓股+候选股, 批量接口一次请求拉全部code, 流量小)
INTERVAL_SECONDS = 10
# 接口异常退避: 连续失败3次 → 改30秒间隔并告警, 恢复后回10秒
# (避免接口故障时10秒频率放大冲击)
BACKOFF_AFTER_FAILS = 3
BACKOFF_INTERVAL = 30
# 持续故障升级告警(Task#38走查补强): 退避后每连败60轮(30s*60≈30分钟)
# 追加scheduler_alerts+企微 —— 故障期间持仓卖出检查实质停摆(cron兜底
# 与daemon同为腾讯源, 同源故障时也拿不到行情), 必须让人持续感知
ESCALATE_EVERY_FAILS = 60
SESSION_START = '09:25'
SESSION_END = '15:00'


def log(msg: str):
    print(f"[{datetime.now().strftime('%H:%M:%S')}] {msg}", flush=True)


def touch_heartbeat():
    """心跳: 供cron判断daemon存活(<180s跳过), 供scheduler超时告警。"""
    with open(pt.HEARTBEAT_FILE, 'w') as f:
        f.write(datetime.now().strftime('%Y-%m-%d %H:%M:%S'))


def remove_heartbeat():
    """正常退出时删心跳: 15:01收盘cron终检不被'新鲜心跳'误跳过。"""
    try:
        os.remove(pt.HEARTBEAT_FILE)
    except OSError:
        pass


# =============================================================================
# 候选股: 加载 + 买入区间(从策略类声明属性动态读取, 不硬编码)
# =============================================================================

def load_buy_ranges() -> dict:
    """{slot_id: {'desc': 展示串, 'check': fn(live_rate)->bool}}。

    与morning_decision口径一致的信息性代理: 仅用策略类声明的开盘涨幅窗口
    (S4 open_rate_threshold / S5 open_rate_min+max), 无声明的slot不判。
    精筛(涨停拦截/流动性等)仍归morning_decision, 此处只做提示不买入。
    """
    ranges = {}
    for cfg in ACTIVE_STRATEGIES:
        if not cfg.get('enabled'):
            continue
        try:
            cls = getattr(importlib.import_module(cfg['module']), cfg['class'])
        except Exception:
            continue
        rmin = getattr(cls, 'open_rate_min', None)
        rmax = getattr(cls, 'open_rate_max', None)
        thr = getattr(cls, 'open_rate_threshold', None)
        if rmin is not None and rmax is not None:
            ranges[cfg['slot_id']] = {
                'desc': f"[{rmin:+.1f}%,{rmax:+.1f}%)",
                'check': (lambda r, lo=rmin, hi=rmax: lo <= r < hi),
            }
        elif thr is not None:
            ranges[cfg['slot_id']] = {
                'desc': f"<={thr:+.1f}%",
                'check': (lambda r, t=thr: r <= t),
            }
    return ranges


def load_candidates(today: str) -> list:
    """当日candidates_*.json → [{'slot','strategy','code','name','signal_close'}]。"""
    path = os.path.join(OUTPUT_DIR, f"candidates_{today.replace('-', '')}.json")
    if not os.path.exists(path):
        return []
    with open(path, 'r', encoding='utf-8') as f:
        data = json.load(f)
    out, seen = [], set()
    for slot, blk in sorted(data.get('strategies', {}).items()):
        for c in blk.get('candidates', []):
            key = (slot, c['code'])
            if key in seen:
                continue
            seen.add(key)
            out.append({'slot': slot,
                        'strategy': blk.get('strategy_name', ''),
                        'code': c['code'],
                        'name': c.get('name', ''),
                        'signal_close': c.get('signal_close', 0)})
    return out


# =============================================================================
# 轮询
# =============================================================================

# =============================================================================
# Task#45 影子模式: 新路径纯观察(不执行不落盘), 事件与比对写jsonl
# =============================================================================

# 影子运行态: {code: {'agg','engine','boot_date','core','legacy','compared'}}
_SHADOW_STATE = {}


def _shadow_log_path(today: str) -> str:
    return os.path.join(LOG_DIR, f"shadow_exit_{today.replace('-', '')}.jsonl")


def _shadow_emit(today: str, rec: dict):
    """影子事件落盘(追加jsonl)。失败仅告警, 绝不影响旧路径执行。"""
    rec['ts'] = datetime.now().strftime('%H:%M:%S')
    try:
        with open(_shadow_log_path(today), 'a') as f:
            f.write(json.dumps(rec, ensure_ascii=False) + '\n')
    except OSError as e:
        log(f"  [WARN] 影子日志写入失败: {e}")


def _shadow_observe(pos: dict, q: dict, today: str, legacy_action):
    """影子模式单仓观察: 与poll_positions_core的bar级路径同源计算。

    tick → LiveBarAggregator → 共享ExitEngine.on_bar, 但:
      - 不产生actions/不平仓/不写positions(执行权完全在旧路径);
      - 无日级兜底(兜底语义由旧路径每tick区间判定天然覆盖);
      - 新路径首次触发记core_trigger, 旧路径首次触发记legacy_trigger,
        双方都有结论时记compare(action是否一致) —— 周一收盘后
        scripts/shadow_compare.py 汇总差异。
    """
    code = pos['code']
    st = _SHADOW_STATE.get(code)
    if st is None or st['boot_date'] != today:
        try:
            st = {'agg': LiveBarAggregator(code),
                  'engine': exit_engine_from_position(pos),
                  'boot_date': today, 'core': None, 'legacy': None,
                  'compared': False}
        except Exception as e:
            # 构建失败(如timed缺字段): 记日志后该仓当日不再观察
            log(f"  [WARN] 影子引擎构建失败 {code}: {e}")
            st = {'agg': None, 'engine': None, 'boot_date': today,
                  'core': None, 'legacy': None, 'compared': False}
        _SHADOW_STATE[code] = st

    if st['engine'] is not None and st['core'] is None:
        ts = q.get('_ts') or datetime.now()
        try:
            for bar in st['agg'].on_tick(ts, q['price'],
                                         q.get('preclose', 0)):
                dec = st['engine'].on_bar(bar)
                if dec:
                    st['core'] = {
                        'action': dec.action,
                        'sell_price': dec.sell_price,
                        'trigger_ref': dec.trigger_ref,
                        'bar': f"{dec.sell_date} {dec.sell_time}",
                    }
                    _shadow_emit(today, {'event': 'core_trigger',
                                         'code': code, **st['core']})
                    break
        except Exception as e:
            log(f"  [WARN] 影子路径计算异常 {code}: {e}")

    if legacy_action and st['legacy'] is None:
        st['legacy'] = {'action': legacy_action, 'price': q['price']}
        _shadow_emit(today, {'event': 'legacy_trigger', 'code': code,
                             'action': legacy_action, 'price': q['price']})

    if st['core'] and st['legacy'] and not st['compared']:
        st['compared'] = True
        match = st['core']['action'] == st['legacy']['action']
        _shadow_emit(today, {'event': 'compare', 'code': code,
                             'match': match, 'core': st['core'],
                             'legacy': st['legacy']})
        log(f"  [影子比对] {code}: core={st['core']['action']} "
            f"legacy={st['legacy']['action']} match={match}")


# G2确认持仓运行态(2026-07-31批准 APPROVAL_G2_MINUTE, staged库移植):
# {code: {'agg': LiveBarAggregator, 'engine': ExitEngine, 'boot_date': ...}}
_G2_STATE = {}


def _poll_g2_position(pos: dict, q: dict, today: str) -> dict:
    """G2确认持仓(持仓带confirm_bars>0, 现仅大阳低吸)的执行权分路。

    APPROVAL_G2_MINUTE.md §④保守映射(用户已批准):
      10s tick → LiveBarAggregator 5min bar → 共享ExitEngine confirm状态机:
      - 硬止损: 进行中bar每tick评估, 触线即决策(=现行10秒即卖, 只紧不松);
      - 10:00前trailing触线: 不即刻卖 → 连续confirm_bars根final bar收盘
        破线确认 → 次bar首个tick按开盘市价卖(ExitDecision.confirmed=True);
      - 10:00后trailing: 确认窗口外, 触线即卖(与legacy语义一致)。
    每日首轮先跑日级兜底evaluate_position(捕捉daemon启动前区间触发+missed
    告警+peak_sod维护), 兜底命中→退化触线即卖(保守, 同cron兜底方向)。
    peak照旧持久化到positions.json → daemon死亡时cron按旧语义无缝接管。
    回滚=策略confirm_bars=0→新持仓无字段→分支恒不入。
    返回与evaluate_position同构res dict → poll_positions循环零改动消费。
    """
    code = pos['code']
    st = _G2_STATE.get(code)
    if st is None or st.get('boot_date') != today:
        # 每日首轮: 日级兜底(启动前触发/missed/T+0展示/peak_sod维护)
        res = pt.evaluate_position(pos, q, today)
        if res['action_rec'] is not None or res['skip'] is not None:
            # 兜底已触发(启动前区间击穿→触线即卖, 保守) 或 T+0禁卖:
            # 不建bar引擎(触发仓即将平仓; T+0仓当日无卖出评估)
            return res
        try:
            st = {'agg': LiveBarAggregator(code),
                  'engine': exit_engine_from_position(pos),
                  'boot_date': today}
        except Exception as e:
            log(f"  [WARN] G2引擎构建失败 {code}: {e} → 本日退化legacy路径")
            st = {'agg': None, 'engine': None, 'boot_date': today}
        _G2_STATE[code] = st
        return res
    if st['engine'] is None:
        return pt.evaluate_position(pos, q, today)   # 构建失败: 当日退化旧语义

    res = {'skip': None, 'action': None, 'action_rec': None,
           'blocked': False, 'missed': None, 'state_changed': False}
    # peak持久化(与core路径同语义: 含当日high, cron兜底接管时peak连续)
    if today > pos.get('buy_date', today):
        new_peak = max(pos.get('peak_price', pos['buy_price']),
                       q.get('high', 0) or 0, q['price'])
        if new_peak > pos.get('peak_price', 0):
            pos['peak_price'] = round(new_peak, 3)
            res['state_changed'] = True

    ts = q.get('_ts') or datetime.now()
    dec = None
    for bar in st['agg'].on_tick(ts, q['price'], q.get('preclose', 0)):
        dec = st['engine'].on_bar(bar)
        if dec:
            break
    if dec:
        label = {'stop_loss': '止损', 'trailing_stop': '移动止盈',
                 'take_profit': '止盈', 'expired': '到期'}[dec.action]
        log(f"  ★ [G2·ExitEngine] {label}: {code} {pos.get('name', '')} "
            f"bar{dec.sell_date} {dec.sell_time} "
            f"触发线{dec.trigger_ref:.2f} 语义卖价{dec.sell_price:.2f} "
            f"盈亏{dec.pnl_pct:+.1f}%"
            f"{' (G2收盘确认后次bar开盘卖)' if dec.confirmed else ''}")
        res['action'] = dec.action
        res['action_rec'] = {
            'code': code, 'name': pos.get('name', ''),
            'strategy': pos.get('strategy', ''),
            'action': dec.action, 'current_price': q['price'],
            'sell_price': dec.sell_price, 'pnl_pct': dec.pnl_pct,
        }
        _G2_STATE.pop(code, None)
    return res


def poll_positions(quotes: dict, today: str) -> list:
    """持仓股检查: 共享Task#20区间触线判定, 触线即刻带锁平仓+通知。

    返回持仓快照行(供intraday_snapshot.json展示)。
    USE_EXECUTION_CORE=True时走统一双模执行框架(Task#27, 共享ExitEngine)。
    G2确认持仓(confirm_bars>0, 2026-07-31批准)在legacy路径内分路进
    _poll_g2_position(共享ExitEngine confirm状态机), 其余持仓路径不变。
    """
    if USE_EXECUTION_CORE:
        return poll_positions_core(quotes, today)
    data = pt.load_positions()
    holdings = [p for p in data['positions'] if p['status'] == 'holding']
    rows = []
    if not holdings:
        if EXEC_CORE_MODE == 'shadow':
            _SHADOW_STATE.clear()
        _G2_STATE.clear()
        return rows

    actions = []
    state_changed = False
    for pos in holdings:
        code = pos['code']
        if code not in quotes:
            log(f"  [WARN] 持仓{code} {pos.get('name','')}未获取到行情, 本轮跳过")
            continue
        q = quotes[code]
        if int(pos.get('confirm_bars') or 0) > 0:
            # G2确认持仓(现仅大阳低吸): 执行权分路进共享ExitEngine;
            # shadow观察不做——执行权已在ExitEngine(2026-07-31批准)
            res = _poll_g2_position(pos, q, today)
        else:
            res = pt.evaluate_position(pos, q, today)
            if EXEC_CORE_MODE == 'shadow':
                # 影子观察: 新路径并行计算(纯观察), 执行权仍在下方旧路径
                _shadow_observe(pos, q, today, res['action'])
        if res['state_changed']:
            state_changed = True
        if res['missed']:
            if pt.process_missed_alert(pos, res['missed'], q, today,
                                       persist=True):
                state_changed = True
        if res.get('blocked'):
            # Task#286: 跌停封死顺延通知(当日去重), 不记成交, 开板即卖
            if pt.process_blocked_alert(pos, q, today, persist=True):
                state_changed = True
        if res['action_rec']:
            actions.append(res['action_rec'])
        rows.append({
            'code': code, 'name': pos.get('name', ''),
            'slot_id': pos.get('slot_id', ''),
            'strategy': pos.get('strategy', ''),
            'buy_price': pos.get('buy_price'),
            'price': q['price'],
            'change_pct': (round((q['price'] / q['preclose'] - 1) * 100, 2)
                           if q.get('preclose') else None),
            'pnl_pct': round((q['price'] / pos['buy_price'] - 1) * 100, 2),
            'triggered': res['action'],
        })

    # 影子/G2运行态清理(已平仓/消失的持仓)
    live = {p['code'] for p in holdings}
    if EXEC_CORE_MODE == 'shadow':
        for gone in [c for c in _SHADOW_STATE if c not in live]:
            _SHADOW_STATE.pop(gone, None)
    for gone in [c for c in _G2_STATE if c not in live]:
        _G2_STATE.pop(gone, None)

    # peak/告警标记落盘(daemon存活期间cron跳过, 写冲突窗口极小)
    if state_changed:
        pt.save_positions(data)

    if actions:
        # 触线即刻平仓: 重新加载最新持仓, selling_locked先到者锁定防与cron双写
        data2 = pt.load_positions()
        account = data2['account']
        closed = 0
        # Task#185: 同批卖出代码集→卖出通知持仓栏剔除用(仅影响通知渲染)
        batch = [x['code'] for x in actions]
        for a in actions:
            if pt.close_position_locked(data2, a, today, owner='daemon',
                                        batch_codes=batch):
                closed += 1
        if closed:
            data2['positions'] = [p for p in data2['positions']
                                  if p.get('status') != 'closed']
            account['realized_pnl'] = round(account['realized_pnl'], 2)
            account['cash'] = round(account['cash'], 2)
            pt.save_positions(data2)
            log(f"  ★ [daemon平仓] {closed}笔 | 现金¥{account['cash']:,.0f} "
                f"| 累计PnL¥{account['realized_pnl']:+,.0f}")
    return rows


# Task#27 新路径运行态: {code: {'agg': LiveBarAggregator, 'engine': ExitEngine,
#                               'boot_date': 已做过日级兜底判定的日期}}
_CORE_STATE = {}


def poll_positions_core(quotes: dict, today: str) -> list:
    """Task#27统一双模执行框架实盘路径: 10s tick聚合进行中5min bar喂共享ExitEngine。

    与旧路径行为等价保障:
      1. 每持仓每日首轮先跑一次日级兜底 evaluate_position (捕捉daemon启动前/
         重启空窗期的区间触发与missed告警, 语义与Task#20一致);
      2. 之后每轮tick → LiveBarAggregator → ExitEngine.on_bar (bar级挂单语义,
         与回测MinuteDbBarStream走同一份决策代码);
      3. 平仓仍走 close_position_locked(owner='daemon') 幂等锁, peak仍持久化
         到positions.json(daemon死亡时cron按旧语义无缝接管)。
    """
    data = pt.load_positions()
    holdings = [p for p in data['positions'] if p['status'] == 'holding']
    rows = []
    if not holdings:
        _CORE_STATE.clear()
        return rows

    actions = []
    state_changed = False
    live_codes = set()
    for pos in holdings:
        code = pos['code']
        live_codes.add(code)
        if code not in quotes:
            log(f"  [WARN] 持仓{code} {pos.get('name','')}未获取到行情, 本轮跳过")
            continue
        q = quotes[code]
        st = _CORE_STATE.get(code)
        if st is None or st['boot_date'] != today:
            # 每日首轮: 日级兜底判定(捕捉启动前触发+missed告警+peak_sod维护)
            res = pt.evaluate_position(pos, q, today)
            if res['state_changed']:
                state_changed = True
            if res['missed']:
                if pt.process_missed_alert(pos, res['missed'], q, today,
                                           persist=True):
                    state_changed = True
            if res.get('blocked'):
                # Task#286: 跌停封死顺延通知(当日去重), 不记成交
                if pt.process_blocked_alert(pos, q, today, persist=True):
                    state_changed = True
            if res['action_rec']:
                actions.append(res['action_rec'])
                rows.append(_pos_row(pos, q, res['action']))
                continue
            st = {'agg': LiveBarAggregator(code),
                  'engine': exit_engine_from_position(pos),
                  'boot_date': today}
            _CORE_STATE[code] = st
            rows.append(_pos_row(pos, q, None))
            continue

        # bar级路径: tick → 进行中5min bar → 共享ExitEngine
        ts = q.get('_ts') or datetime.now()
        dec = None
        for bar in st['agg'].on_tick(ts, q['price'], q.get('preclose', 0)):
            dec = st['engine'].on_bar(bar)
            if dec:
                break
        # peak持久化(与旧路径同语义: 含当日high, cron兜底接管时peak连续)
        if today > pos.get('buy_date', today):
            new_peak = max(pos.get('peak_price', pos['buy_price']),
                           q.get('high', 0) or 0, q['price'])
            if new_peak > pos.get('peak_price', 0):
                pos['peak_price'] = round(new_peak, 3)
                state_changed = True
        if dec:
            label = {'stop_loss': '止损', 'trailing_stop': '移动止盈',
                     'take_profit': '止盈', 'expired': '到期'}[dec.action]
            log(f"  ★ [ExitEngine] {label}: {code} {pos.get('name','')} "
                f"bar{dec.sell_date} {dec.sell_time} "
                f"触发线{dec.trigger_ref:.2f} 语义卖价{dec.sell_price:.2f} "
                f"盈亏{dec.pnl_pct:+.1f}%")
            actions.append({
                'code': code, 'name': pos.get('name', ''),
                'strategy': pos.get('strategy', ''),
                'action': dec.action, 'current_price': q['price'],
                'sell_price': dec.sell_price, 'pnl_pct': dec.pnl_pct,
            })
        rows.append(_pos_row(pos, q, dec.action if dec else None))

    # 已卖出/消失的持仓清理运行态
    for gone in [c for c in _CORE_STATE if c not in live_codes]:
        _CORE_STATE.pop(gone, None)

    if state_changed:
        pt.save_positions(data)

    if actions:
        data2 = pt.load_positions()
        account = data2['account']
        closed = 0
        # Task#185: 同批卖出代码集→卖出通知持仓栏剔除用(仅影响通知渲染)
        batch = [x['code'] for x in actions]
        for a in actions:
            if pt.close_position_locked(data2, a, today, owner='daemon',
                                        batch_codes=batch):
                closed += 1
                _CORE_STATE.pop(a['code'], None)
        if closed:
            data2['positions'] = [p for p in data2['positions']
                                  if p.get('status') != 'closed']
            account['realized_pnl'] = round(account['realized_pnl'], 2)
            account['cash'] = round(account['cash'], 2)
            pt.save_positions(data2)
            log(f"  ★ [daemon平仓] {closed}笔 | 现金¥{account['cash']:,.0f} "
                f"| 累计PnL¥{account['realized_pnl']:+,.0f}")
    return rows


def _pos_row(pos: dict, q: dict, triggered) -> dict:
    """持仓快照行(与旧路径poll_positions输出同构)。"""
    return {
        'code': pos['code'], 'name': pos.get('name', ''),
        'slot_id': pos.get('slot_id', ''),
        'strategy': pos.get('strategy', ''),
        'buy_price': pos.get('buy_price'),
        'price': q['price'],
        'change_pct': (round((q['price'] / q['preclose'] - 1) * 100, 2)
                       if q.get('preclose') else None),
        'pnl_pct': round((q['price'] / pos['buy_price'] - 1) * 100, 2),
        'triggered': triggered,
    }


def poll_candidates(candidates: list, quotes: dict, buy_ranges: dict,
                    in_range_logged: set) -> list:
    """候选股跟踪: 现价/相对昨收涨幅; 进入买入区间→信息日志(不买入)。"""
    rows = []
    for c in candidates:
        q = quotes.get(c['code'])
        if not q:
            rows.append({**c, 'price': None})
            continue
        sig_close = c.get('signal_close') or 0
        live_rate = (round((q['price'] / sig_close - 1) * 100, 2)
                     if sig_close > 0 else None)
        rng = buy_ranges.get(c['slot'])
        in_range = bool(rng and live_rate is not None
                        and rng['check'](live_rate))
        rows.append({
            'slot': c['slot'], 'strategy': c['strategy'],
            'code': c['code'], 'name': c['name'],
            'signal_close': sig_close,
            'price': q['price'],
            'preclose': q.get('preclose'),
            'change_pct': (round((q['price'] / q['preclose'] - 1) * 100, 2)
                           if q.get('preclose') else None),
            'open': q.get('open'), 'high': q.get('high'), 'low': q.get('low'),
            'live_rate': live_rate,
            'buy_range': rng['desc'] if rng else None,
            'in_buy_range': in_range,
        })
        if in_range and (c['slot'], c['code']) not in in_range_logged:
            in_range_logged.add((c['slot'], c['code']))
            log(f"  ◆ [买入区间-信息] {c['slot']} {c['code']} {c['name']} "
                f"现价{q['price']:.2f} 较信号收盘{live_rate:+.2f}% "
                f"∈ {rng['desc']} (不自动买入, 买入归9:25 morning_decision)")
    return rows


def write_snapshot(today: str, seq: int, pos_rows: list, cand_rows: list):
    """快照原子写(临时文件+replace, 供前端随时读)。"""
    snap = {
        'updated_at': datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
        'trade_date': today,
        'poll_seq': seq,
        'positions': pos_rows,
        'candidates': cand_rows,
    }
    tmp = SNAPSHOT_FILE + '.tmp'
    with open(tmp, 'w', encoding='utf-8') as f:
        json.dump(snap, f, ensure_ascii=False, indent=2)
    os.replace(tmp, SNAPSHOT_FILE)


def poll_once(candidates: list, buy_ranges: dict, in_range_logged: set,
              seq: int, today: str) -> bool:
    """单轮轮询: 一次批量行情 → 持仓触线检查 + 候选快照。返回行情是否获取成功。"""
    hold_codes = [p['code'] for p in pt.load_positions()['positions']
                  if p['status'] == 'holding']
    all_codes = list(dict.fromkeys(hold_codes + [c['code'] for c in candidates]))
    quotes = pt.fetch_realtime_quotes(all_codes)  # 内部batch=50, 一次HTTP拉全部
    if all_codes and not quotes:
        log(f"轮询#{seq} 行情获取失败(0/{len(all_codes)}), 本轮跳过")
        return False

    pos_rows = poll_positions(quotes, today)
    cand_rows = poll_candidates(candidates, quotes, buy_ranges, in_range_logged)
    write_snapshot(today, seq, pos_rows, cand_rows)

    n_up = sum(1 for r in cand_rows if (r.get('change_pct') or 0) > 0)
    n_dn = sum(1 for r in cand_rows if (r.get('change_pct') or 0) < 0)
    n_trig = sum(1 for r in pos_rows if r.get('triggered'))
    n_in = sum(1 for r in cand_rows if r.get('in_buy_range'))
    log(f"轮询#{seq} 行情{len(quotes)}/{len(all_codes)} | "
        f"持仓{len(pos_rows)}(触线{n_trig}) | "
        f"候选{len(cand_rows)}(涨{n_up} 跌{n_dn} 入买区{n_in}) | 快照已写")
    return True


# =============================================================================
# 主循环
# =============================================================================

def on_5min_boundary(time_end: str, today: str):
    """(预留)Task#45统一调度: 5min级策略的daemon盘中决策挂载点。

    调用时机: 主循环检测到5min bar窗口切换(上一窗口定稿)时, time_end
    为刚定稿bar的结束时刻'HHMM'(0935..1500, 与execution_core.BAR_TIMES/
    tick_scheduler同源)。
    未来5min级策略(decision_interval='5min')接入实盘时在此分发调用
    should_buy/should_sell —— 与回测tick_scheduler.strategy_due同口径:
    hour级策略在此永远无决策点(其盘中决策点=9:25买入(morning_decision)
    + ExitEngine持续卖出监控=hour级挂单的及时执行, 不经此处)。
    当前实盘为hour级体系(用户裁决), 本函数为空实现, 零行为影响。
    """
    pass


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--once', action='store_true',
                    help='单轮后退出(测试用, 不限交易时段)')
    args = ap.parse_args()

    os.makedirs(LOG_DIR, exist_ok=True)
    # flock防重复启动(锁随进程生命周期持有)
    lock_fp = open(LOCK_FILE, 'w')
    try:
        fcntl.flock(lock_fp, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except (BlockingIOError, OSError):
        print("[intraday_monitor] 已有实例在运行(flock占用), 退出")
        return 0
    lock_fp.write(str(os.getpid()))
    lock_fp.flush()

    today = datetime.now().strftime('%Y-%m-%d')
    # 自判交易日: 当日candidates文件存在(22:00 generate_candidates按交易日历生成)
    candidates = load_candidates(today)
    if not candidates and not args.once:
        log(f"非交易日或候选文件缺失(candidates_{today.replace('-','')}.json), 退出")
        return 0
    if datetime.now().weekday() >= 5 and not args.once:
        log("周末, 退出")
        return 0

    buy_ranges = load_buy_ranges()
    _path_desc = {'off': '旧路径evaluate_position(已验证)',
                  'on': 'ExitEngine(统一双模核心)',
                  'shadow': '旧路径执行+ExitEngine影子比对'}[EXEC_CORE_MODE]
    log(f"启动 pid={os.getpid()} | 候选{len(candidates)}只 | "
        f"卖出路径={_path_desc} | "
        f"买入区间声明: " + ', '.join(f"{k}{v['desc']}"
                                     for k, v in sorted(buy_ranges.items())))

    in_range_logged = set()  # 当日已记过"进入买入区间"日志的(slot,code)
    seq = 0
    consec_fails = 0     # 连续失败计数(退避用)
    backoff_alerted = False
    last_bar_key = None  # (预留)5min边界检测: 上一轮所在bar窗口
    try:
        while True:
            hm = datetime.now().strftime('%H:%M')
            if hm >= SESSION_END:
                log(f"{SESSION_END}收盘, 自然退出(共{seq}轮)")
                break
            if hm < SESSION_START:
                log(f"未到{SESSION_START}, 等待中...")
                time.sleep(10)
                continue
            t0 = time.time()
            seq += 1
            ok = False
            try:
                ok = poll_once(candidates, buy_ranges, in_range_logged, seq, today)
            except Exception as e:
                log(f"[ERROR] 轮询#{seq}异常(非致命, 下轮重试): {e}")
            touch_heartbeat()
            # (预留)5min边界分发: bar窗口切换=上一bar定稿 → 5min级策略决策点
            key = LiveBarAggregator.bar_key(datetime.now())
            if key is not None and key != last_bar_key:
                if last_bar_key is not None:
                    on_5min_boundary(last_bar_key[1], today)
                last_bar_key = key
            if args.once:
                log("--once 单轮完成, 退出")
                break

            # 接口异常退避: 连续失败{BACKOFF_AFTER_FAILS}次 → 30秒间隔+告警,
            # 恢复后回{INTERVAL_SECONDS}秒
            if ok:
                if consec_fails >= BACKOFF_AFTER_FAILS:
                    log(f"接口恢复, 轮询间隔回到{INTERVAL_SECONDS}秒")
                    try:
                        ts = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
                        with open(pt.SCHEDULER_ALERT_LOG, 'a',
                                  encoding='utf-8') as f:
                            f.write(f"[{ts}] ✅ 盘中监控行情接口已恢复"
                                    f"(此前连续失败{consec_fails}次)\n")
                    except OSError:
                        pass
                consec_fails = 0
                backoff_alerted = False
                interval = INTERVAL_SECONDS
            else:
                consec_fails += 1
                if consec_fails >= BACKOFF_AFTER_FAILS:
                    interval = BACKOFF_INTERVAL
                    # 首次退避告警 + 持续故障升级(每ESCALATE_EVERY_FAILS轮重发,
                    # Task#38: 长时间故障=持仓卖出检查停摆, 单条告警易被淹没)
                    escalate = (consec_fails - BACKOFF_AFTER_FAILS) \
                        % ESCALATE_EVERY_FAILS == 0
                    if not backoff_alerted or escalate:
                        backoff_alerted = True
                        est_min = consec_fails * BACKOFF_INTERVAL // 60
                        alert = (f"⚠️ 盘中监控行情接口连续失败{consec_fails}次"
                                 f"(约{est_min}分钟), 已退避至{BACKOFF_INTERVAL}s"
                                 f"间隔。持仓卖出检查停摆中(cron兜底同为腾讯源, "
                                 f"同源故障也无行情), 请人工关注持仓")
                        log(alert)
                        try:
                            ts = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
                            with open(pt.SCHEDULER_ALERT_LOG, 'a',
                                      encoding='utf-8') as f:
                                f.write(f"[{ts}] {alert}\n")
                        except OSError:
                            pass
                        # 企微通知(未配置时send_text自动降级为日志)
                        try:
                            from realtime.notify import send_text
                            send_text(f"🚨 [intraday_monitor] {alert}")
                        except Exception:
                            pass
                else:
                    interval = INTERVAL_SECONDS
            time.sleep(max(1, interval - (time.time() - t0)))
    finally:
        remove_heartbeat()
    return 0


if __name__ == '__main__':
    sys.exit(main())
