#!/usr/bin/env python3
"""[Task#73] 回撤作战手册代码化 - 纯函数模块 (最早架构分析的P1欠账).

规则(阈值全部常量可配):
  组合NAV距历史高点(HWM)回撤:
    < DD_L1        -> L0 正常, scale=1.0
    >= DD_L1 (10%) -> L1 减半, 新开仓资金×0.5
    >= DD_L2 (20%) -> L2 白名单, 仅允许达标最稳策略(WHITELIST, 当前S3/S4)开仓, scale=0.5
    >= DD_L3 (30%) -> L3 熔断, 停止一切新开仓 + 显著告警 + 企微, 需人工确认解除:
                      python3 tools/drawdown_guard.py ack
  熔断粘滞: L3一旦触发, 即使回撤回落也保持halt, 直到人工ack(防止NAV在30%线上下抖动反复开关)。
  L1/L2随当前回撤深度自然滑动解除。

与冰点overlay同链路叠乘(morning_decision接线, 等Bill Task#68收工后追加):
    final_scale = ice_scale × drawdown_scale, 下限 MIN_SCALE_FLOOR=0.1
    (0.5×0.3=0.15 > 0.1 仍有意义: 1M×0.15/5=3万/slot; floor防更极端组合下仓位小到无交易意义)

状态落盘: data/realtime/drawdown_state.json
  {nav_history: [[date, nav], ...], hwm, current_nav, drawdown_pct, level, scale,
   whitelist_only, halt, halt_since, halt_ack_required, updated_at}
NAV历史来源: positions.json无逐日历史 -> 由 strategy_health.py 每晚19:00盘后
  快照盯市NAV追加(按日期幂等); 种子=实盘启动日初始资金。
口径分账(P0账本修正2026-07-30): positions.json=现金/成本口径账本(预算用),
  本模块nav_history=盯市口径(现金+持仓×最新收盘价, 风险用), 两者不得混写。

本模块只读positions.json/只写drawdown_state.json, 不触碰交易主链任何文件。
"""
import json
import os
import sys
from datetime import datetime

BASE = '/home/AIWealth'
STATE_FILE = f'{BASE}/data/realtime/drawdown_state.json'
POSITIONS_FILE = f'{BASE}/data/realtime/positions.json'
ALERT_LOG = f'{BASE}/logs/realtime/scheduler_alerts.log'

# ── 阈值常量(可配) ────────────────────────────────────────────
DD_L1 = 10.0          # 回撤≥10%: 新开仓×0.5
DD_L2 = 20.0          # 回撤≥20%: 仅白名单策略开仓
DD_L3 = 30.0          # 回撤≥30%: 熔断停止新开仓, 人工确认解除
SCALE_L1 = 0.5
SCALE_L2 = 0.5
MIN_SCALE_FLOOR = 0.1  # 与冰点叠乘后的下限(0.5×0.3=0.15>floor; 防仓位小到无意义)
WHITELIST = ['gem_star_late_seal', 'big_yang_low_open_v2']   # S3/S4 达标最稳
SEED_DATE = '2026-07-22'      # 实盘启动日
SEED_NAV = 1000000.0          # 初始资金

LEVEL_DESC = {0: 'L0正常', 1: 'L1减半(回撤≥10%)',
              2: 'L2白名单(回撤≥20%)', 3: 'L3熔断(回撤≥30%)'}


def evaluate(nav_history, halt_sticky=False):
    """纯函数: NAV历史 -> 回撤等级与买入系数。

    nav_history: [[date_str, nav], ...] 按日期升序(内部会排序去重)
    halt_sticky: 上一状态是否处于未解除的熔断(L3粘滞)
    返回 dict(hwm, current_nav, drawdown_pct, level, scale, whitelist_only, halt)
    """
    if not nav_history:
        return {'hwm': None, 'current_nav': None, 'drawdown_pct': 0.0,
                'level': 0, 'scale': 1.0, 'whitelist_only': False,
                'halt': False}
    seq = sorted({d: float(v) for d, v in nav_history}.items())
    navs = [v for _, v in seq]
    hwm = max(navs)
    cur = navs[-1]
    dd = (hwm - cur) / hwm * 100 if hwm > 0 else 0.0
    if dd >= DD_L3 or halt_sticky:
        level, scale, wl, halt = 3, 0.0, True, True
    elif dd >= DD_L2:
        level, scale, wl, halt = 2, SCALE_L2, True, False
    elif dd >= DD_L1:
        level, scale, wl, halt = 1, SCALE_L1, False, False
    else:
        level, scale, wl, halt = 0, 1.0, False, False
    return {'hwm': round(hwm, 2), 'current_nav': round(cur, 2),
            'drawdown_pct': round(dd, 2), 'level': level, 'scale': scale,
            'whitelist_only': wl, 'halt': halt}


def combine_scale(ice_scale, dd_scale):
    """纯函数: 冰点×回撤叠乘, 带下限。dd_scale=0(熔断)时直接0不套floor。"""
    if dd_scale <= 0:
        return 0.0
    return max(ice_scale * dd_scale, MIN_SCALE_FLOOR)


# ── 状态文件维护 ──────────────────────────────────────────────
def load_state():
    if os.path.exists(STATE_FILE):
        return json.load(open(STATE_FILE))
    return {'nav_history': [[SEED_DATE, SEED_NAV]],
            'halt': False, 'halt_since': None}


def _tencent_closes(codes):
    """腾讯行情兜底取收盘价(盘后parts[3]=当日收盘)。失败返回空dict不抛异常。"""
    import re
    import urllib.request
    prices = {}
    if not codes:
        return prices
    try:
        url = 'http://qt.gtimg.cn/q=' + ','.join(c.replace('.', '') for c in codes)
        req = urllib.request.Request(url)
        req.add_header('User-Agent', 'Mozilla/5.0')
        content = urllib.request.urlopen(req, timeout=5).read().decode('gbk')
        for line in content.strip().split('\n'):
            m = re.match(r'v_(\w+)="(.*)"', line.strip())
            if not m or not m.group(2):
                continue
            parts = m.group(2).split('~')
            if len(parts) >= 4:
                try:
                    px = float(parts[3])
                except ValueError:
                    continue
                if px > 0:
                    for c in codes:
                        if c.replace('.', '') == m.group(1):
                            prices[c] = px
                            break
    except Exception as e:
        print(f"[drawdown_guard] 腾讯行情兜底失败(继续降级): {e}")
    return prices


def mark_to_market_nav(pos_data, date_str=None):
    """盯市NAV = cash + Σ(buy_amount/buy_price × 当日收盘价)。

    与positions.json的total_nav(成本口径)分账: 回撤风控必须看盯市浮动,
    否则浮亏不触发作战等级。
    [Task#251修复] 原实现取"库内最新收盘"(ORDER BY date DESC LIMIT 1):
    19:00快照与18:30日K入库(BaoStock降级分批, 常跑到20点后)竞态,
    8/5~8/7连续3日用昨收快照, 8/7曲线少记¥29,817(11.88% vs 真实14.86%)。
    修正取价优先级: ①stock_kline当日(date=快照日)收盘
    ②腾讯实时兜底(盘后=当日收盘) ③库内最新收盘(≤快照日, 记stale告警)
    ④成本价退化。非交易日走②③自然等于最近收盘, NAV持平无失真。
    """
    import sqlite3
    if date_str is None:
        date_str = datetime.now().strftime('%Y-%m-%d')
    acct = pos_data.get('account', {})
    nav = acct.get('cash', SEED_NAV)
    holdings = [p for p in pos_data.get('positions', [])
                if p.get('status') == 'holding']
    if not holdings:
        return nav
    try:
        conn = sqlite3.connect(f'file:{BASE}/data/stocks.db?mode=ro', uri=True)
        # ①当日收盘
        day_close = {}
        for p in holdings:
            row = conn.execute(
                "SELECT close FROM stock_kline WHERE code=? AND date=?",
                (p['code'], date_str)).fetchone()
            if row and row[0]:
                day_close[p['code']] = row[0]
        # ②当日缺行的腿走腾讯兜底(18:30入库竞态期的常态路径)
        missing = [p['code'] for p in holdings if p['code'] not in day_close]
        tencent = _tencent_closes(missing)
        for p in holdings:
            code, bp = p['code'], p.get('buy_price')
            px = day_close.get(code) or tencent.get(code)
            if px is None:
                # ③库内最新收盘(stale, 显式告警不再静默)
                row = conn.execute(
                    "SELECT close, date FROM stock_kline WHERE code=? "
                    "AND date<=? ORDER BY date DESC LIMIT 1",
                    (code, date_str)).fetchone()
                if row and row[0]:
                    px = row[0]
                    _alert(f"⚠️ 盯市快照{date_str} {code}当日收盘缺失且腾讯兜底失败, "
                           f"退化用{row[1]}收盘{row[0]}(stale)")
            if px and bp:
                nav += p['buy_amount'] / bp * px
            else:
                nav += p.get('buy_amount', 0)     # ④无价退化成本价
        conn.close()
    except Exception as e:
        print(f"[drawdown_guard] 盯市计算降级为成本口径: {e}")
        nav = acct.get('cash', SEED_NAV) + sum(p.get('buy_amount', 0)
                                               for p in holdings)
    return round(nav, 2)


def save_state(state):
    tmp = STATE_FILE + '.tmp'
    json.dump(state, open(tmp, 'w'), ensure_ascii=False, indent=1)
    os.replace(tmp, STATE_FILE)


def _alert(msg):
    line = f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] " \
           f"[DRAWDOWN_GUARD] {msg}"
    print(line)
    try:
        with open(ALERT_LOG, 'a') as f:
            f.write(line + '\n')
    except Exception as e:                        # 告警降级, 不影响主流程
        print(f"[drawdown_guard] 写告警日志失败(降级继续): {e}")


def snapshot_and_evaluate(date_str=None, nav=None):
    """追加当日NAV快照(幂等)并重估等级, 落盘状态。供strategy_health调用。

    date_str/nav 缺省时从positions.json持仓计算盯市NAV(现金+持仓×最新收盘价)。
    返回 evaluate() 结果 + halt管理字段。
    """
    state = load_state()
    if date_str is None:
        date_str = datetime.now().strftime('%Y-%m-%d')
    if nav is None:
        pos = json.load(open(POSITIONS_FILE))
        # 盯市口径(P0修正: 原误用成本口径total_nav; Task#251: 按快照日取价防竞态)
        nav = mark_to_market_nav(pos, date_str)
    hist = {d: v for d, v in state.get('nav_history', [])}
    hist[date_str] = float(nav)                    # 同日重跑=覆盖, 幂等
    state['nav_history'] = sorted(hist.items())

    prev_level = state.get('level', 0)
    res = evaluate(state['nav_history'], halt_sticky=state.get('halt', False))

    # 熔断触发(首次进入L3): 粘滞+显著告警+企微
    if res['halt'] and not state.get('halt', False):
        state['halt'] = True
        state['halt_since'] = date_str
        _alert(f"🚨 L3熔断触发! 回撤{res['drawdown_pct']}% ≥{DD_L3}% "
               f"(NAV {res['current_nav']:,.0f} / HWM {res['hwm']:,.0f}), "
               f"停止一切新开仓; 人工确认解除: "
               f"python3 tools/drawdown_guard.py ack")
        try:
            sys.path.insert(0, BASE)
            from realtime.notify import send_text
            send_text(f"🚨 AIWealth回撤熔断(L3)\n回撤{res['drawdown_pct']}% "
                      f"≥{DD_L3}%\nNAV {res['current_nav']:,.0f} / "
                      f"HWM {res['hwm']:,.0f}\n已停止一切新开仓, 需人工确认解除")
        except Exception as e:
            print(f"[drawdown_guard] 企微通知失败(降级继续): {e}")
    elif res['level'] != prev_level and res['level'] > 0:
        _alert(f"⚠️ 回撤等级变更 {LEVEL_DESC[prev_level]} → "
               f"{LEVEL_DESC[res['level']]}: 回撤{res['drawdown_pct']}%, "
               f"新开仓系数×{res['scale']}"
               + (f", 仅白名单{WHITELIST}" if res['whitelist_only'] else ""))
    elif res['level'] == 0 and prev_level > 0:
        _alert(f"✅ 回撤恢复正常: {LEVEL_DESC[prev_level]} → L0, "
               f"回撤{res['drawdown_pct']}%")

    state.update(res)
    state['halt_ack_required'] = state.get('halt', False)
    state['whitelist'] = WHITELIST
    state['thresholds'] = {'L1': DD_L1, 'L2': DD_L2, 'L3': DD_L3,
                           'scale_L1': SCALE_L1, 'scale_L2': SCALE_L2,
                           'floor': MIN_SCALE_FLOOR}
    state['updated_at'] = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    save_state(state)
    return state


def get_live_scale():
    """供morning_decision接线用(等Bill Task#68收工后追加):
    读最新落盘状态 -> (drawdown_scale, whitelist_only时的白名单或None)。
    状态文件缺失时安全降级为满仓(1.0, None)——不影响交易主链。
    """
    try:
        state = json.load(open(STATE_FILE))
    except Exception:
        return 1.0, None
    if state.get('halt'):
        return 0.0, []
    wl = state.get('whitelist', WHITELIST) if state.get('whitelist_only') \
        else None
    return state.get('scale', 1.0), wl


def main():
    if len(sys.argv) > 1 and sys.argv[1] == 'ack':
        state = load_state()
        if not state.get('halt'):
            print("[drawdown_guard] 当前无熔断状态, 无需解除")
            return
        state['halt'] = False
        state['halt_ack_at'] = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        res = evaluate(state['nav_history'], halt_sticky=False)
        state.update(res)
        state['halt_ack_required'] = state.get('halt', False)
        save_state(state)
        _alert(f"✅ 人工确认解除熔断, 当前回撤{res['drawdown_pct']}% → "
               f"{LEVEL_DESC[res['level']]}, 系数×{res['scale']}")
        return
    st = snapshot_and_evaluate()
    print(f"[drawdown_guard] {st['updated_at']} NAV={st['current_nav']:,.0f} "
          f"HWM={st['hwm']:,.0f} 回撤{st['drawdown_pct']}% "
          f"{LEVEL_DESC[st['level']]} scale={st['scale']}"
          f"{' halt(需人工ack)' if st['halt'] else ''}")


if __name__ == '__main__':
    main()
