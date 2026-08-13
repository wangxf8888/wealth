#!/usr/bin/env python3
"""Task#183 打板影子信号滚动记账 —— 每日盘后维护SHADOW_LEDGER.md.

职责(只读影子产物+腾讯行情, 零生产触碰):
  1. 读取 research/results/shadow_surge/shadow_signals_*.json (仅live模式);
  2. 出场复算: 出场规则=D+1 0940 bar开盘价定时卖(early_surge_chase_0940
     引擎语义, minute.db右端点约定'0940'=09:35-09:40)。minute.db断供期间
     出场价取腾讯m5(ifzq.gtimg.cn mkline, bar标签同右端点约定)D+1 0940
     bar的open; 状态持久化到 shadow_exits.json(幂等, 已了结不重复请求);
  3. 生成 SHADOW_LEDGER.md: 严格口径统计(胜率/均笔/等权累计) + 回测G5
     基线(胜率53.02%/均笔+0.83%)对照 + 降级拦截统计;
  4. 降级样本(vol_baseline=skipped, 8/6的26笔)单独追踪并标注
     "降级样本不计入严格口径统计", 出场照常复算仅作参考;
  5. Task#213盘中追踪衔接: 若存在当日shadow_tracking_summary则并入
     "盘中追踪战报"段(封板率/炸板率/路径要点); 滚动表加列
     盘中封板率/终态封板率; 另附可生产性滚动区块(累计信号/封板
     成功率/模拟收益均笔/与G5基线对照/距实盘化差距结论)。

口径说明(如实):
  - 严格口径样本 = 有量能基线的信号(vol_ratio非空);
  - 引擎语义笔(Task#229口径切换, 2026-08-08起) = 每日最早确认bar的非火箭
    winner(slot=1, 每日最多1笔), 与t228回放A口径/G5基线530笔(530天×1笔)对齐;
    旧per-bar口径(每bar winner非火箭)降为参考——t228回放实证bar2/bar3追加
    winner为负贡献(87笔-0.19%), per-bar记账会污染30笔裁决样本;
  - 累计测试战绩(准绳口径) = t228回放31日trades_engine_slot1.json(准绳,
    2026-06-26~08-07) + 2026-08-07之后live slot=1笔拼接(重叠窗口以回放为准);
  - 等权累计收益 = 逐笔收益率算术和(每笔等权1单位, 与回测solo口径一致);
  - 跌停顺延语义(D+1 hour1封死跌停→顺延卖)未自动化, 如遇0940 bar
    high==low且价格≈跌停价, 该笔标记need_review人工复核。

Task#232扩展(2026-08-08): 新增"回封首板测试池"独立区块 —— 读取
reseal_signals_*.json(tools/seal_reseal_detector.py每日15:10产出),
D+1开盘买(开盘涨停不买)/TP+5%/SL-8%(D+2起日级, TP优先, 跳空按开盘)/
D+3收盘兜底复算, 状态持久化reseal_exits.json, 净收益扣双边0.1%;
与冲板池分开统计(各自笔数/胜率/笔均/累计), 考核线: 累计30笔且胜率≥60%
且笔均>0.5%再提G3引擎。当日日K未入库(15:20跑)用qt收盘快照兜底。
Task#240扩展(2026-08-09): 回封池区块拆两行统计——"V1全体"与"C08子集"
(C08=V1中c08_eligible==true即单次回封reopen_count==1的切片, t236矩阵终审
首选升级格), 笔数/胜率/笔均/累计各自独立计算, 考核线两口径独立同标准;
同一信号池两口径并行记账, V1筛选与出场规则本体零改动。
Task#257裁决(2026-08-10): 回封族全周期分段终审否决(死路#62), 转正预期
撤销——回封池降为"已否决-仅静默记账", cron记账链保留仅落盘不推送,
作为数据积累以备行情结构变化后重评; 台账区块与考核线语义已同步中和。
Task#274扩展(2026-08-10): 冲板池逐笔明细表新增climb_*观察字段列(null显示
"-"); 累计测试战绩区新增按climb_half两行分桶统计(样本量+WR+笔均,
标注"观察字段·不参与口径"), t266残值观察积累1-2月实盘样本后再判。
Task#285扩展(2026-08-11): 新增"S3冻结假想池"独立区块 —— 创科晚封
(gem_star_late_seal)冻结期假想跟踪(用户批准A案, t283 DECISION_PACK §2.4)。
数据源=每日9:25 decision json的frozen_recommendations(只读); 纸面记账:
D0开盘买 → S3现行出场(TP+12%/SL-20%/D+2 hour4收盘到期, 小时级,
双触发保守取SL, 与gem_star_late_seal.should_sell同语义); stocks.db小时K
无状态幂等重算(当日日K未入库时该笔保持OPEN次日自然补记), 零新增状态文件。
复活判据: 滚动3月月化回正且笔均>+0.5%(复活需G3+用户批准)。
Task#276裁决(2026-08-11): 冲板池全量无偏复验否决(死路#66终版)——t233全量
数据tag=final复跑: 全周期月化5.96%<10%, 2021/-54%与2024/-21%两失效年,
构成校正后笔均-0.46%转负(触板层100%覆盖实锤剪刀差), G5的+275.75%大部分为
触板条件化覆盖幸存者偏置。转正预期撤销, 冲板池降为"已否决-仅静默记账",
cron记账链保留仅落盘不推送(shadow_surge_detector SEND_BUY_NOTIFY=False),
台账区块与考核线语义已同步中和; 数据积累以备行情结构变化后重评。

运行: python3 tools/shadow_ledger.py [--no-fetch] [--no-notify]
cron: 每交易日15:20盘后(见crontab Task#183条目; 回封池记账并入本运行)。
红线: 只写 research/results/shadow_surge/{shadow_exits.json,
reseal_exits.json, SHADOW_LEDGER.md}; 限速: 请求间隔>=1s; Task#229: 仅发带
⚠️测试标注的D+1模拟卖出结果通知(新了结slot=1笔, 幂等不重发), 零生产触碰不变。
"""
import argparse
import glob
import json
import os
import sqlite3
import sys
import time
import urllib.request
from datetime import datetime, timedelta

SS_DIR = '/home/AIWealth/research/results/shadow_surge'
EXITS_PATH = os.path.join(SS_DIR, 'shadow_exits.json')
LEDGER_PATH = os.path.join(SS_DIR, 'SHADOW_LEDGER.md')
M5_URL = 'https://ifzq.gtimg.cn/appstock/app/kline/mkline?param={sym},m5,,{n}'
QT_URL = 'https://qt.gtimg.cn/q={syms}'
RATE_GAP = 1.0            # 请求最小间隔秒(限速铁律)
MAX_REQ = 80              # 单次运行请求硬上限
G5_BASELINE = {'trades': 530, 'win_rate': 53.02, 'avg_ret': 0.83,
               'span': '2024-01-01~2026-07-31'}
# Task#229 准绳口径: t228回放31日slot=1逐笔明细(用户裁决测试信号模式的考核基准)
REPLAY_TRADES = ('/home/AIWealth/research/results/t228_surge_replay/'
                 'trades_engine_slot1.json')
REPLAY_END = '2026-08-07'          # 回放准绳覆盖终点(重叠窗口以回放为准)
CALIBER_SWITCH_DATE = '2026-08-08'  # 引擎语义笔per-bar→slot=1口径切换日
# ---- Task#232 回封首板测试池(独立记账, 与冲板池分开) ----
SDB_PATH = '/home/AIWealth/data/stocks.db'
RESEAL_EXITS_PATH = os.path.join(SS_DIR, 'reseal_exits.json')
RESEAL_TP, RESEAL_SL = 5.0, -8.0   # V1: TP+5%/SL-8%(D+2起日级)
RESEAL_FEE = 0.2                   # 双边0.1%费用(净收益口径, 与t230回测一致)
RESEAL_BASELINE = ('31日回放(2026-06-26~08-07): slot=1月化+32.6%/胜率75%; '
                   '笔级n=89 净+1.08%/胜率68.5%')
# Task#257(2026-08-10): 全周期终审否决, 原"30笔→提G3"转正预期已撤销
RESEAL_EXAM = ('原线: 累计30笔 且 胜率≥60% 且 笔均>0.5%(转正预期已撤销, '
               'Task#257终审否决, 纸面记账仅作数据积累)')
RESEAL_C08_NOTE = ('C08子集=V1∩单次回封(reopen_count==1, Task#240并行记账'
                   '口径); t236矩阵基准: 有偏样本slot=1月化+12.7%且分年全正')
# ---- Task#285 S3冻结假想池(独立记账, 数据源=decision json只读) ----
DECISION_DIR = '/home/AIWealth/data/realtime'
S3F_STRATEGY = 'gem_star_late_seal'
S3F_TP, S3F_SL = 12.0, -20.0       # S3现行出场参数(与策略声明一致)
S3F_FEE = 0.2                      # 双边0.1%费用(净收益口径, 与回封池一致)
S3F_REVIVE = ('冻结期假想跟踪·复活判据=滚动3月月化回正且笔均>+0.5%'
              '（复活需G3+用户批准, 不自动复活）')
_last_req = [0.0]
_req_count = [0]


def _get(url):
    if _req_count[0] >= MAX_REQ:
        raise RuntimeError(f'请求数达硬上限{MAX_REQ}, 本次运行截断')
    wait = RATE_GAP - (time.time() - _last_req[0])
    if wait > 0:
        time.sleep(wait)
    req = urllib.request.Request(
        url, headers={'User-Agent': 'Mozilla/5.0',
                      'Referer': 'https://gu.qq.com/'})
    raw = urllib.request.urlopen(req, timeout=15).read()
    _last_req[0] = time.time()
    _req_count[0] += 1
    return raw


def _sym(code):
    return code.replace('.', '')          # 'sh.600156' -> 'sh600156'


def load_signals():
    """全部live信号, 按日期升序; 附分类标记。"""
    sigs = []
    for fp in sorted(glob.glob(os.path.join(SS_DIR, 'shadow_signals_*.json'))):
        try:
            data = json.load(open(fp))
        except (OSError, ValueError):
            continue
        meta = data.get('meta', {})
        if meta.get('mode') != 'live':
            continue                       # dry-run自测不入台账
        d = meta.get('date', '')
        nb = (meta.get('skip_counts') or {}).get('no_vol_baseline', 0)
        for s in data.get('signals', []):
            sigs.append({
                'sig_id': f"{d}_{s['code']}",
                'date': d, 'code': s['code'], 'name': s.get('name', ''),
                'buy_px': s['buy_px'], 'confirm_bar': s['confirm_bar'],
                'vol_ratio': s.get('vol_ratio'),
                'degraded': s.get('vol_baseline') == 'skipped',
                'winner': bool(s.get('winner')),
                'rocket': bool(s.get('rocket')),
                'nb_skips': nb,
                # Task#274观察字段(旧信号无字段→None, 台账显示"-")
                'climb_secs': s.get('climb_secs'),
                'climb_turn_pct': s.get('climb_turn_pct'),
                'climb_half': s.get('climb_half'),
            })
    return sigs


def pick_slot1(day_sigs):
    """引擎slot=1口径当日唯一笔: 最早确认bar的非火箭winner(t228回放A口径)。"""
    cands = [s for s in day_sigs
             if s['winner'] and not s['rocket'] and not s['degraded']]
    if not cands:
        return None
    return min(cands, key=lambda s: s['confirm_bar'])   # '0935'<'0940'<'0945'


def slot1_trades(strict_sigs):
    """strict信号→逐日slot=1笔列表(日期升序)。"""
    by_day = {}
    for s in strict_sigs:
        by_day.setdefault(s['date'], []).append(s)
    out = [w for w in (pick_slot1(v) for _, v in sorted(by_day.items())) if w]
    return out


def load_replay_trades():
    """t228回放准绳逐笔(已了结)。文件缺失返回[](降级为纯live口径, 如实标注)。"""
    try:
        trades = json.load(open(REPLAY_TRADES))
        return [t for t in trades if t.get('status') == 'closed']
    except (OSError, ValueError):
        return []


def load_daily_intercepts():
    """{date: no_vol_baseline拦截数} (live日)。"""
    out = {}
    for fp in sorted(glob.glob(os.path.join(SS_DIR, 'shadow_signals_*.json'))):
        try:
            data = json.load(open(fp))
        except (OSError, ValueError):
            continue
        meta = data.get('meta', {})
        if meta.get('mode') == 'live':
            out[meta.get('date', '')] = \
                (meta.get('skip_counts') or {}).get('no_vol_baseline', 0)
    return out


def load_tracking_summaries():
    """{date: 盘中追踪summary} (Task#213 shadow_tracker产物, 全部日)。"""
    out = {}
    for fp in sorted(glob.glob(
            os.path.join(SS_DIR, 'shadow_tracking_summary_*.json'))):
        try:
            s = json.load(open(fp))
        except (OSError, ValueError):
            continue
        if s.get('date'):
            out[s['date']] = s
    return out


def fetch_exit_px(code, sig_date, m5_cache):
    """D+1 0940 bar开盘价: 腾讯m5中首个>sig_date且含0940 bar的日期。
    返回 (exit_date, exit_px, need_review) 或 None(未到期/取不到)。"""
    sym = _sym(code)
    if sym not in m5_cache:
        try:
            data = json.loads(_get(M5_URL.format(sym=sym, n=400)))
            m5_cache[sym] = (data.get('data', {}).get(sym, {}) or {}) \
                .get('m5') or []
        except Exception as e:
            print(f'  [WARN] {code} m5获取失败: {e}')
            m5_cache[sym] = []
    days = {}
    for b in m5_cache[sym]:
        ts = str(b[0])
        if len(ts) == 12 and ts[8:12] == '0940':
            days[f'{ts[0:4]}-{ts[4:6]}-{ts[6:8]}'] = b
    for d in sorted(days):
        if d > sig_date:
            b = days[d]
            o, h, low = float(b[1]), float(b[3]), float(b[4])
            need_review = bool(h == low)   # 一字bar疑似封死, 顺延语义人工复核
            return d, o, need_review
    return None


def load_reseal_signals():
    """Task#232 回封首板live信号(status=ok日), 升序。"""
    out = []
    for fp in sorted(glob.glob(os.path.join(SS_DIR, 'reseal_signals_*.json'))):
        try:
            data = json.load(open(fp))
        except (OSError, ValueError):
            continue
        meta = data.get('meta', {})
        if meta.get('mode') != 'live' or meta.get('status') != 'ok':
            continue                   # 回放产物/安全退出日不入台账
        d = meta.get('date', '')
        for s in data.get('signals', []):
            rc = s.get('reopen_count', s.get('reopen_cnt'))
            out.append({'sig_id': f"{d}_{s['code']}", 'date': d,
                        'code': s['code'], 'name': s.get('name', ''),
                        'first_seal_bar': s.get('first_seal_bar'),
                        'reopen_cnt': rc,
                        # 旧信号(t240前)无c08_eligible字段: 由reopen数回推
                        'c08_eligible': bool(s.get('c08_eligible', rc == 1))})
    return out


def _qt_batch(codes):
    """qt快照批量(盘后=全日OHLC+收盘): {code: {open,high,low,px,date}}。"""
    out = {}
    if not codes:
        return out
    try:
        raw = _get(QT_URL.format(syms=','.join(_sym(c) for c in codes))) \
            .decode('gbk', errors='replace')
    except Exception as e:
        print(f'  [WARN] reseal qt快照失败: {e}')
        return out
    by_sym = {_sym(c): c for c in codes}
    for line in raw.split(';'):
        if '="' not in line:
            continue
        sym = line.split('=')[0].strip().lstrip('v_')
        f = line.split('"')[1].split('~')
        code = by_sym.get(sym)
        if not code or len(f) < 35:
            continue
        try:
            out[code] = {'px': float(f[3]), 'open': float(f[5]),
                         'date': str(f[30])[:8],
                         'high': float(f[33]), 'low': float(f[34])}
        except (ValueError, IndexError):
            continue
    return out


def process_reseal(no_fetch, now, today):
    """Task#232 回封池复算(幂等): D+1开盘买(开盘涨停/停牌不买) →
    D+2起日级TP+5%/SL-8%(TP优先, 跳空按开盘) → D+3收盘兜底。
    日K以stocks.db为主; 当日15:20未入库用qt收盘快照兜底(仅未了结笔,
    已了结不回改)。状态持久化reseal_exits.json。返回state字典。"""
    sigs = load_reseal_signals()
    state = {}
    if os.path.exists(RESEAL_EXITS_PATH):
        try:
            state = json.load(open(RESEAL_EXITS_PATH))
        except (OSError, ValueError):
            state = {}
    if not sigs:
        return state
    if '/home/AIWealth' not in sys.path:
        sys.path.insert(0, '/home/AIWealth')
    from trading_rules import limit_prices
    sdb = sqlite3.connect(f'file:{SDB_PATH}?mode=ro', uri=True)
    db_days = [r[0] for r in sdb.execute(
        "SELECT DISTINCT date FROM stock_kline WHERE date>=? ORDER BY date",
        (min(s['date'] for s in sigs),))]
    # 当日15:20日K未入库: 工作日且晚于库内最新日 → qt快照兜底为当日行情
    extra_today = (today not in db_days
                   and (not db_days or today > db_days[-1])
                   and datetime.now().weekday() < 5)
    all_days = db_days + ([today] if extra_today else [])
    qts = {}
    if extra_today and not no_fetch:
        need = sorted({s['code'] for s in sigs
                       if state.get(s['sig_id'], {}).get('status')
                       not in ('closed', 'skipped')})
        qts = _qt_batch(need)

    def ohlc(code, day):
        row = sdb.execute(
            "SELECT open, high, low, close FROM stock_kline WHERE code=? "
            "AND date=? AND volume>0", (code, day)).fetchone()
        if row and row[0]:
            return {'o': row[0], 'h': row[1], 'l': row[2], 'c': row[3],
                    'src': 'db'}
        q = qts.get(code)
        if day == today and q and q['date'] == today.replace('-', '') \
                and q['open'] > 0:
            return {'o': q['open'], 'h': q['high'], 'l': q['low'],
                    'c': q['px'], 'src': 'qt_eod'}
        return None

    for s in sigs:
        st = state.setdefault(s['sig_id'], {
            'status': 'pending', 'date': s['date'], 'code': s['code'],
            'name': s['name'], 'first_seal_bar': s.get('first_seal_bar'),
            'reopen_cnt': s.get('reopen_cnt'),
            'c08_eligible': s.get('c08_eligible')})
        if 'c08_eligible' not in st:       # t240前遗留state补切片标记
            st['c08_eligible'] = bool(s.get('c08_eligible'))
        if st['status'] in ('closed', 'skipped'):
            continue
        days = [d for d in all_days if d > s['date']][:3]   # D+1,D+2,D+3
        if not days:
            continue                   # D+1未到, 保持pending
        if st['status'] == 'pending':  # ---- D+1开盘买 ----
            d1 = days[0]
            bar = ohlc(s['code'], d1)
            if bar is None:
                if d1 in db_days:      # 该交易日无行=停牌, 买不进
                    st.update(status='skipped', reason='d1_suspended',
                              updated_at=now)
                continue
            pc = sdb.execute(
                "SELECT close FROM stock_kline WHERE code=? AND date=?",
                (s['code'], s['date'])).fetchone()
            lu, _ = limit_prices(s['code'], pc[0] if pc and pc[0] else 0,
                                 'ST' in (s['name'] or ''))
            if lu > 0 and bar['o'] >= lu - 0.001:
                st.update(status='skipped', reason='open_at_limit',
                          skip_px=bar['o'], updated_at=now)   # V1开盘涨停不买
                continue
            st.update(status='open', buy_date=d1, buy_px=bar['o'],
                      buy_src=bar['src'],
                      tp_px=round(bar['o'] * (1 + RESEAL_TP / 100), 4),
                      sl_px=round(bar['o'] * (1 + RESEAL_SL / 100), 4),
                      updated_at=now)
        # ---- D+2起日级出场(TP优先, 跳空按开盘), D+3收盘兜底 ----
        h3_day = days[2] if len(days) >= 3 else None
        for d in [x for x in days[1:] if x <= all_days[-1]]:
            bar = ohlc(s['code'], d)
            if bar is None:
                if d in db_days and d == h3_day:
                    st['need_review'] = True   # H3日停牌: 顺延待人工复核
                continue
            px = reason = None
            if bar['o'] >= st['tp_px']:
                px, reason = bar['o'], 'TP_gap'
            elif bar['o'] <= st['sl_px']:
                px, reason = bar['o'], 'SL_gap'
            elif bar['h'] >= st['tp_px']:
                px, reason = st['tp_px'], 'TP'
            elif bar['l'] <= st['sl_px']:
                px, reason = st['sl_px'], 'SL'
            elif d == h3_day:
                px, reason = bar['c'], 'H3'
            if px:
                gross = (px / st['buy_px'] - 1) * 100
                st.update(status='closed', exit_date=d,
                          exit_px=round(px, 4), exit_reason=reason,
                          exit_src=bar['src'], ret_pct=round(gross, 4),
                          ret_net_pct=round(gross - RESEAL_FEE, 4),
                          updated_at=now)
                print(f"  [reseal-exit] {s['sig_id']} {s['name']} "
                      f"buy={st['buy_px']}@{st['buy_date']} -> {d} "
                      f"{reason}@{px} 净{st['ret_net_pct']:+.2f}%")
                break
    sdb.close()
    tmp = RESEAL_EXITS_PATH + '.tmp'
    with open(tmp, 'w', encoding='utf-8') as f:
        json.dump(state, f, ensure_ascii=False, indent=1)
    os.replace(tmp, RESEAL_EXITS_PATH)     # 原子写(t51评审放行条件沿袭)
    return state


def reseal_block(state):
    """回封首板测试池 台账区块行(Task#240: V1全体/C08子集两口径切片统计)。"""
    rows = sorted(state.values(), key=lambda r: (r['date'], r['code']))
    closed = [r for r in rows if r.get('status') == 'closed']
    skipped = [r for r in rows if r.get('status') == 'skipped']
    openp = [r for r in rows if r.get('status') == 'open']
    pend = [r for r in rows if r.get('status') == 'pending']
    n, wr, avg, cum = compute_stats(
        [{'ret_pct': r['ret_net_pct']} for r in closed])
    # C08子集切片(同一信号池, c08_eligible==true; 各指标独立计算)
    rows_c08 = [r for r in rows if r.get('c08_eligible')]
    closed_c08 = [r for r in rows_c08 if r.get('status') == 'closed']
    n8, wr8, avg8, cum8 = compute_stats(
        [{'ret_pct': r['ret_net_pct']} for r in closed_c08])
    L = []
    L.append('## 回封首板测试池 (Task#232, 独立区块, 与冲板池分开统计)'
             ' — **已否决-仅静默记账·已下线转回测线（2026-08-11用户裁决）**')
    L.append('')
    L.append('- 📁 归档态(Task#293, 2026-08-11用户裁决"测试信号别放实盘"): '
             '前端实盘信号页测试卡片已移除, 实盘链路已下线, '
             '后续评估转回测线; 本区块仅作数据归档。')
    L.append('- ❌ 状态: 策略已全周期终审否决(Task#257, 2026-08-10, 死路#62'
             '——最优月化+2.76%远低于10%线, 31日窗口+32.6%系窗口幸运); '
             '通知已停灌, 本区块仅静默纸面记账积累数据, 详见 research/'
             'results/t257_reseal_final/FINAL_REPORT.md。')
    L.append('- 策略: 回封首板(seal_reseal_v1) — T3(10:05-11:30)首封'
             '∩开板≥1次重封∩首板∩收盘封死; D+1开盘买(开盘涨停不买), '
             'TP+5%/SL-8%(D+2起日级, TP优先, 跳空按开盘), D+3收盘兜底。'
             '净收益已扣双边0.1%费用。**测试信号·零实盘触碰**。')
    L.append('- 信号源: tools/seal_reseal_detector.py(每日15:10, '
             'board_lab events实采); 记账: 本脚本15:20复算, 当日行情'
             'qt收盘快照兜底(仅未了结笔, 已了结不回改)。')
    L.append(f'- 两口径并行记账(Task#240): {RESEAL_C08_NOTE}。')
    L.append('')
    L.append('| 口径 | 累计信号 | 持仓中 | 待买入 | 未成交(涨停/停牌) | '
             '已了结 | 胜率 | 笔均(净) | 等权累计(净) |')
    L.append('|---|---|---|---|---|---|---|---|---|')
    L.append(f'| V1全体 | {len(rows)} | {len(openp)} | {len(pend)} | '
             f'{len(skipped)} | {n} | {fmt(wr, "%", False)} | {fmt(avg)} | '
             f'{fmt(cum)} |')
    L.append(f'| C08子集(单次回封) | {len(rows_c08)} | '
             f'{sum(1 for r in rows_c08 if r.get("status") == "open")} | '
             f'{sum(1 for r in rows_c08 if r.get("status") == "pending")} | '
             f'{sum(1 for r in rows_c08 if r.get("status") == "skipped")} | '
             f'{n8} | {fmt(wr8, "%", False)} | {fmt(avg8)} | {fmt(cum8)} |')
    L.append('')
    L.append(f'- 回放基准: {RESEAL_BASELINE}')
    exam_hit = (n >= 30 and wr is not None and wr >= 60
                and avg is not None and avg > 0.5)
    exam_hit8 = (n8 >= 30 and wr8 is not None and wr8 >= 60
                 and avg8 is not None and avg8 > 0.5)
    L.append(f'- 考核线(V1线): {RESEAL_EXAM} — 当前已了结n={n}, '
             + ('原线数字已达(不再触发G3, 已否决)' if exam_hit
                else '静默积累中(不触发转正)'))
    L.append(f'- 考核线(C08线, 独立同标准, 转正预期同样已撤销): 原线累计30笔 且 '
             f'胜率≥60% 且 笔均>0.5% — 当前已了结n={n8}, '
             + ('原线数字已达(不再触发G3, 已否决)' if exam_hit8
                else '静默积累中(不触发转正)'))
    if rows:
        L.append('')
        L.append('| 信号日 | 代码 | 名称 | 首封bar | C08 | 状态 | D+1买入 | '
                 '出场 | 原因 | 净收益 |')
        L.append('|---|---|---|---|---|---|---|---|---|---|')
        for r in rows:
            stt = r.get('status', '?') + \
                (f"/{r['reason']}" if r.get('reason') else '') + \
                (' ⚠need_review' if r.get('need_review') else '')
            buy = (f"{r.get('buy_date')} @{r.get('buy_px')}"
                   if r.get('buy_px') else '—')
            ex = (f"{r.get('exit_date')} @{r.get('exit_px')}"
                  if r.get('exit_px') else '—')
            L.append(f"| {r['date']} | {r['code']} | {r['name']} | "
                     f"{r.get('first_seal_bar', '—')} | "
                     f"{'是' if r.get('c08_eligible') else '否'} | {stt} | "
                     f"{buy} | {ex} | {r.get('exit_reason') or '—'} | "
                     f"{fmt(r.get('ret_net_pct'))} |")
    L.append('')
    return L


def load_s3_frozen_recs():
    """[Task#285] S3冻结假想池信号源: decision json的frozen_recommendations。

    数据源=morning_decision 9:25产物(经live复筛+入场守卫的"本应买入"单,
    t283 DECISION_PACK §2.4口径), 只读; 单文件缺失/损坏安全跳过。
    产出: [{sig_id, date(=决策日即买入日), code, name, buy_px}] 日期升序。
    """
    out = []
    for fp in sorted(glob.glob(os.path.join(DECISION_DIR,
                                            'decision_*.json'))):
        try:
            data = json.load(open(fp))
        except (OSError, ValueError):
            continue
        d = data.get('trade_date', '')
        for slot in (data.get('strategies') or {}).values():
            if slot.get('strategy') != S3F_STRATEGY:
                continue
            for r in (slot.get('frozen_recommendations') or []):
                code = r.get('code')
                px = r.get('buy_price') or r.get('open')
                if not code or not px or not d:
                    continue
                out.append({'sig_id': f'{d}_{code}', 'date': d,
                            'code': code, 'name': r.get('name', ''),
                            'buy_px': float(px)})
    out.sort(key=lambda t: (t['date'], t['code']))
    return out


def simulate_s3_frozen(trades):
    """[Task#285] S3假想池纸面模拟(无状态幂等重算, stocks.db小时K只读)。

    语义与strategies/gem_star_late_seal.should_sell逐条一致:
    买入=决策日开盘(buy_price, 已过9:25入场守卫); T+1买入日禁卖;
    出场日(买入次一交易日, 停牌/未入库顺延)逐小时:
    ①open越SL/TP以open成交(先SL保守) ②hourly low触SL以SL价 /
    high触TP以TP价(同小时双触发保守取SL) ③hour4收盘expired
    (hours_held>=8, hour4_close缺失退化day close)。净收益扣S3F_FEE。
    原地写status/exit_date/exit_px/exit_reason/ret_net_pct;
    当日行情未入库保持open, 次日重算自动补记(零状态文件)。
    """
    if not trades:
        return
    sdb = sqlite3.connect(f'file:{SDB_PATH}?mode=ro', uri=True)
    db_days = [r[0] for r in sdb.execute(
        "SELECT DISTINCT date FROM stock_kline WHERE date>=? ORDER BY date",
        (min(t['date'] for t in trades),))]
    for t in trades:
        t['status'] = 'open'
        buy = t['buy_px']
        sl_target = buy * (1 + S3F_SL / 100)
        tp_target = buy * (1 + S3F_TP / 100)
        # 出场窗口: 买入次一交易日起, 停牌顺延, 5个交易日仍无行→保持open
        for d in [x for x in db_days if x > t['date']][:5]:
            row = sdb.execute(
                "SELECT hour1_open, hour1_high, hour1_low,"
                " hour2_open, hour2_high, hour2_low,"
                " hour3_open, hour3_high, hour3_low,"
                " hour4_open, hour4_high, hour4_low, hour4_close, close"
                " FROM stock_kline WHERE code=? AND date=? AND volume>0",
                (t['code'], d)).fetchone()
            if not row or not row[0]:
                continue                      # 停牌/未入库: 顺延
            px = reason = None
            for h in range(4):
                o, hi, lo = row[h * 3], row[h * 3 + 1], row[h * 3 + 2]
                if not o or o <= 0:
                    continue                  # 该小时无bar
                open_pnl = (o - buy) / buy * 100
                if open_pnl <= S3F_SL:
                    px, reason = o, 'SL_gap'
                elif open_pnl >= S3F_TP:
                    px, reason = o, 'TP_gap'
                elif lo and lo > 0 and lo <= sl_target:
                    px, reason = sl_target, 'SL'
                elif hi and hi > 0 and hi >= tp_target:
                    px, reason = tp_target, 'TP'
                elif h == 3:                  # hour4到期(hours_held>=8)
                    c = row[12] if row[12] and row[12] > 0 else row[13]
                    px, reason = (c if c and c > 0 else o), 'expired'
                if px:
                    break
            if px:
                gross = (px / buy - 1) * 100
                t.update(status='closed', exit_date=d,
                         exit_px=round(px, 4), exit_reason=reason,
                         ret_net_pct=round(gross - S3F_FEE, 4))
                break
    sdb.close()


def s3_frozen_block(trades, today):
    """[Task#285] S3冻结假想池 台账区块(冻结期假想跟踪+复活判据盘点)。"""
    closed = [t for t in trades if t.get('status') == 'closed']
    openp = [t for t in trades if t.get('status') == 'open']
    n, wr, avg, cum = compute_stats(
        [{'ret_pct': t['ret_net_pct']} for t in closed])
    # 滚动3月月化 = 90自然日窗内已了结笔净收益等权累计 / 3
    win_lo = (datetime.strptime(today, '%Y-%m-%d')
              - timedelta(days=90)).strftime('%Y-%m-%d')
    recent = [t for t in closed if t['exit_date'] >= win_lo]
    m3 = sum(t['ret_net_pct'] for t in recent) / 3 if recent else None
    L = []
    L.append('## S3冻结假想池 (Task#285, 独立区块, 冻结期假想跟踪)')
    L.append('')
    L.append('- 🧊 状态: S3创科晚封(gem_star_late_seal)已冻结新开仓'
             '(用户批准A案 2026-08-11, t283 DECISION_PACK §2); 候选照常'
             '生成+9:25照常复筛, "本应买入"单转frozen_recommendations'
             '仅纸面记账不下单, 槽位交V-C动态竞争; 存量持仓出场链零改动。')
    L.append('- 口径: 信号源=decision json的frozen_recommendations'
             '(经live复筛+入场守卫, 每日最多1笔); 模拟=决策日开盘买'
             '(buy_price), 出场与S3现行规则逐条一致(T+1禁卖, 次日逐小时'
             f'TP{S3F_TP:+.0f}%/SL{S3F_SL:+.0f}%挂单语义, open越线以open'
             '成交且先判SL, 同小时双触发保守取SL, hour4收盘到期); 净收益'
             '扣双边0.1%费用; 无状态幂等重算(stocks.db小时K只读), 当日'
             '行情未入库保持OPEN次日补记。')
    L.append(f'- {S3F_REVIVE}')
    L.append('')
    L.append('| 累计假想笔 | 持仓中 | 已了结 | 胜率 | 笔均(净) | '
             '等权累计(净) | 滚动3月月化 |')
    L.append('|---|---|---|---|---|---|---|')
    L.append(f'| {len(trades)} | {len(openp)} | {n} | '
             f'{fmt(wr, "%", False)} | {fmt(avg)} | {fmt(cum)} | '
             f'{fmt(m3)} |')
    L.append('')
    revive_hit = (m3 is not None and m3 > 0
                  and avg is not None and avg > 0.5)
    L.append(f'- 复活判据盘点: 滚动3月月化={fmt(m3)} / 笔均={fmt(avg)} → '
             + ('✅ 数字已达线, 可提交G3+用户批准(不自动复活)'
                if revive_hit else '未达线, 继续假想跟踪'))
    if trades:
        L.append('')
        L.append('| 决策日(买入日) | 代码 | 名称 | 假想买入 | 出场 | '
                 '原因 | 净收益 |')
        L.append('|---|---|---|---|---|---|---|')
        for t in trades:
            ex = (f"{t.get('exit_date')} @{t.get('exit_px')}"
                  if t.get('exit_px') else 'OPEN')
            L.append(f"| {t['date']} | {t['code']} | {t['name']} | "
                     f"@{t['buy_px']} | {ex} | "
                     f"{t.get('exit_reason') or '—'} | "
                     f"{fmt(t.get('ret_net_pct'))} |")
    else:
        L.append('- 暂无假想笔(冻结生效链: 今晚23:30候选生成→次晨9:25复筛'
                 '产生frozen_recommendations, 2026-08-12起逐日积累)。')
    L.append('')
    return L


# ---- Task#285追加B 盘后候选复盘(复盘参考·不影响任何口径) ----
CR_STRAT_CN = {'S1': '首板低吸', 'S2': '巨振反转', 'S3': '创科晚封',
               'S4': '大阳低吸', 'S5': '双板回调低吸'}


def candidates_review_block(no_fetch, today):
    """候选复盘区块(Task#285追加B): 各策略当日已买入笔表现 vs 未入选候选
    平均/最优表现 + 错过最优Top3(含未入选原因归类)。只读重算零状态,
    当日涨幅口径统一=qt收盘价/信号日收盘(昨收)-1, 与任何在产口径无关。"""
    L = []
    L.append('## 候选复盘 (Task#285追加B) — **复盘参考·不影响任何口径**')
    L.append('')
    compact = today.replace('-', '')
    cand_path = os.path.join(DECISION_DIR, f'candidates_{compact}.json')
    if not os.path.exists(cand_path):
        L.append(f'- 当日候选文件缺失({cand_path}), 本日无复盘。')
        L.append('')
        return L
    cand_doc = json.load(open(cand_path))
    if cand_doc.get('trade_date') != today:
        L.append(f"- 候选文件trade_date={cand_doc.get('trade_date')}与今日"
                 f'不符(非交易日/未生成), 本日无复盘。')
        L.append('')
        return L

    # 决策文件(可缺失): meet/rec集合用于未入选原因归类
    dec_doc = {}
    dec_path = os.path.join(DECISION_DIR, f'decision_{compact}.json')
    if os.path.exists(dec_path):
        try:
            d = json.load(open(dec_path))
            if d.get('trade_date') == today:
                dec_doc = d
        except (OSError, ValueError):
            pass

    # 当日实买集合(持仓+当日已了结), 另存买价供参考列
    bought = {}
    try:
        pos_doc = json.load(open(os.path.join(DECISION_DIR, 'positions.json')))
        for p in (pos_doc.get('positions') or []):
            if p.get('buy_date') == today and p.get('code'):
                bought[p['code']] = p.get('buy_price')
        for t in (pos_doc.get('closed_trades') or []):
            if t.get('buy_date') == today and t.get('code'):
                bought[t['code']] = t.get('buy_price')
    except (OSError, ValueError):
        pass

    # 用户手动排除名单(按交易日)
    excluded = set()
    try:
        ex_doc = json.load(open(os.path.join(DECISION_DIR,
                                             'user_excluded.json')))
        excluded = set(ex_doc.get(today) or [])
    except (OSError, ValueError):
        pass

    # 冻结名单(防御导入, 失败降级为空)
    frozen_names = []
    try:
        if '/home/AIWealth' not in sys.path:
            sys.path.insert(0, '/home/AIWealth')
        from realtime.config import STRATEGY_FROZEN
        frozen_names = list(STRATEGY_FROZEN)
    except Exception:
        frozen_names = []

    strategies = cand_doc.get('strategies') or {}
    all_codes = sorted({c['code'] for v in strategies.values()
                        for c in (v.get('candidates') or []) if c.get('code')})
    quotes = {}
    if no_fetch:
        L.append('- ⚠ no-fetch模式: 未拉取当日行情, 仅渲染结构(涨幅列缺省)。')
    else:
        for i in range(0, len(all_codes), 50):     # qt批量分桗50防URL过长
            quotes.update(_qt_batch(all_codes[i:i + 50]))

    def day_ret(c):
        """当日涨幅%: qt收盘/候选信号日收盘(=昨收)-1; 无行情/停牌返None。"""
        q = quotes.get(c.get('code'))
        base = c.get('signal_close')
        if not q or not base or q.get('px', 0) <= 0:
            return None
        return round((q['px'] / base - 1) * 100, 2)

    def miss_reason(skey, sname, code, cand):
        """未入选原因归类(优先级: 冻结>用户排除>公告预警>槽位/资金>排序>复筛)。"""
        sv = strategies.get(skey) or {}
        dv = (dec_doc.get('strategies') or {}).get(skey) or {}
        if sv.get('frozen') or sname in frozen_names:
            return '策略冻结(假想跟踪)'
        if code in excluded:
            return '用户手动排除'
        if cand.get('ann_alert'):
            return '公告风险预警自动排除'
        rec_codes = {r.get('code') for r in (dv.get('recommendations') or [])}
        meet_codes = {m.get('code') for m in (dv.get('meet_condition') or [])}
        if code in rec_codes:
            return '入选推荐但未成交(槽位/资金/执行)'
        if code in meet_codes:
            return 'live复筛通过·排序/槽位未入选'
        if dv:
            return '9:25复筛未过(开盘条件)'
        return '无决策文件(未参与决策)'

    # 各策略: 已买笔 vs 未入选候选平均/最优
    L.append('- 口径: 当日涨幅=qt收盘价/信号日收盘(昨收)-1, 已买笔与未入选'
             '候选同口径可比; 括号内另注已买笔相对买入价浮动供参考。')
    L.append('')
    L.append('| 策略 | 候选数 | 已买入笔(当日涨幅) | 未入选平均 | 未入选最优 |')
    L.append('|---|---|---|---|---|')
    missed_all = []
    for skey in ('S1', 'S2', 'S3', 'S4', 'S5'):
        sv = strategies.get(skey) or {}
        cands = sv.get('candidates') or []
        dv = (dec_doc.get('strategies') or {}).get(skey) or {}
        sname = dv.get('strategy') or ''
        b_parts, b_rets = [], []
        miss_rets = []
        best = None
        for c in cands:
            code = c.get('code')
            r = day_ret(c)
            if code in bought:
                buy_px = bought.get(code)
                q = quotes.get(code)
                vs_buy = (round((q['px'] / buy_px - 1) * 100, 2)
                          if q and buy_px else None)
                b_parts.append(
                    f"{c.get('name')}{fmt(r)}"
                    + (f'(较买价{fmt(vs_buy)})' if vs_buy is not None else ''))
                if r is not None:
                    b_rets.append(r)
            else:
                if r is not None:
                    miss_rets.append(r)
                    if best is None or r > best[0]:
                        best = (r, c)
                missed_all.append((r, skey, sname, c,
                                   b_rets))  # b_rets后续引用同列表引用会跟随更新
        avg_miss = (round(sum(miss_rets) / len(miss_rets), 2)
                    if miss_rets else None)
        best_txt = (f"{best[1].get('name')}{fmt(best[0])}" if best else '—')
        L.append(f'| {skey} {CR_STRAT_CN[skey]} | {len(cands)} | '
                 f"{'; '.join(b_parts) or '无'} | {fmt(avg_miss)} | "
                 f'{best_txt} |')

    # 错过最优Top3(全策略合并, 按当日涨幅降序, 未入选且有行情的候选)
    ranked = sorted([m for m in missed_all if m[0] is not None],
                    key=lambda m: m[0], reverse=True)[:3]
    L.append('')
    if ranked:
        L.append('### 错过最优Top3 (未入选候选按当日涨幅降序)')
        L.append('')
        L.append('| # | 策略 | 股票 | 未入选原因 | 当日涨幅 | 同策略实买涨幅 |')
        L.append('|---|---|---|---|---|---|')
        for i, (r, skey, sname, c, b_rets) in enumerate(ranked, 1):
            reason = miss_reason(skey, sname, c.get('code'), c)
            b_txt = ('; '.join(fmt(x) for x in b_rets) if b_rets
                     else '未买入')
            L.append(f'| {i} | {skey} {CR_STRAT_CN[skey]} | '
                     f"{c.get('code')} {c.get('name')} | {reason} | "
                     f'{fmt(r)} | {b_txt} |')
    else:
        L.append('- 本日无可比未入选候选(无行情/无候选)。')
    L.append('')
    return L


def compute_stats(rows):
    """逐笔收益统计: (n, 胜率%, 均笔%, 等权累计%)。"""
    rets = [r['ret_pct'] for r in rows]
    if not rets:
        return 0, None, None, None
    n = len(rets)
    wr = sum(1 for r in rets if r > 0) / n * 100
    return n, wr, sum(rets) / n, sum(rets)


def fmt(v, suffix='%', signed=True):
    if v is None:
        return '—'
    return f'{v:+.2f}{suffix}' if signed else f'{v:.2f}{suffix}'


def build_exit_notice(sig, ex, cum):
    """Task#229 D+1模拟卖出结果通知(红线: 必须带⚠️测试标注)。
    返回 (text_msg, md_msg)。cum=(n, wr, avg, cum_ret) 准绳口径累计战绩。"""
    if '/home/AIWealth' not in sys.path:
        sys.path.insert(0, '/home/AIWealth')
    from realtime.notify import DISCLAIMER, SEPARATOR
    n, wr, avg, cum_ret = cum
    ret = ex['ret_pct']
    body = [
        f"信号：{sig['name']}（{sig['code']}） {sig['date']}模拟买入"
        f"¥{sig['buy_px']:.2f}",
        f"卖出：{ex['exit_date']} 09:40bar开盘 ¥{ex['exit_px']:.2f}"
        + ("（⚠一字bar疑似封死, 待人工复核）" if ex.get('need_review')
           else ''),
        f'模拟收益：{ret:+.2f}%',
        f'累计测试战绩：{n}笔 胜率{fmt(wr, "%", False)} '
        f'笔均{fmt(avg)} 累计{fmt(cum_ret)}',
        '测试信号·非实盘交易·实盘资金无操作',
    ]
    text_msg = '\n'.join(['⚠️冲板测试信号·D+1模拟卖出（非实盘）', SEPARATOR]
                         + body + [SEPARATOR, DISCLAIMER])
    md_msg = '\n'.join(['**⚠️冲板测试信号·D+1模拟卖出（非实盘）**', SEPARATOR]
                       + body + [SEPARATOR, DISCLAIMER])
    return text_msg, md_msg


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--no-fetch', action='store_true',
                    help='不发任何网络请求(只用已持久化的出场记录重算台账)')
    ap.add_argument('--no-notify', action='store_true',
                    help='不发D+1测试结果通知(手工重算/自测用)')
    args = ap.parse_args()
    now = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    today = datetime.now().strftime('%Y-%m-%d')
    print(f'[shadow_ledger] {now} 开始 (no_fetch={args.no_fetch})')

    sigs = load_signals()
    intercepts = load_daily_intercepts()
    trackings = load_tracking_summaries()   # Task#213盘中追踪产物
    exits = {}
    if os.path.exists(EXITS_PATH):
        try:
            exits = json.load(open(EXITS_PATH))
        except (OSError, ValueError):
            exits = {}

    # ---- 出场复算(幂等: 已了结跳过; 未到期跳过) ----
    new_exit_ids = []          # 本次运行新了结的sig_id(Task#229 D+1通知用)
    m5_cache = {}
    if not args.no_fetch:
        for s in sigs:
            if s['sig_id'] in exits or s['date'] >= today:
                continue                   # 已了结 / D+1未到
            try:
                got = fetch_exit_px(s['code'], s['date'], m5_cache)
            except RuntimeError as e:
                print(f'  [WARN] {e}')
                break
            if got:
                ed, px, review = got
                exits[s['sig_id']] = {
                    'exit_date': ed, 'exit_px': px,
                    'ret_pct': round((px / s['buy_px'] - 1) * 100, 4),
                    'src': 'tencent_m5_0940_open',
                    'need_review': review,
                    'recorded_at': now,
                }
                new_exit_ids.append(s['sig_id'])
                print(f"  [exit] {s['sig_id']} {s['name']} "
                      f"buy={s['buy_px']} -> {ed} 0940o={px} "
                      f"ret={exits[s['sig_id']]['ret_pct']:+.2f}%"
                      f"{' [need_review:一字bar疑似封死]' if review else ''}")
        with open(EXITS_PATH, 'w', encoding='utf-8') as f:
            json.dump(exits, f, ensure_ascii=False, indent=1)

    # ---- 分口径统计 ----
    for s in sigs:
        e = exits.get(s['sig_id'])
        s['ret_pct'] = e['ret_pct'] if e else None
        s['exit'] = e
    strict = [s for s in sigs if not s['degraded']]
    degraded = [s for s in sigs if s['degraded']]
    # Task#229口径切换: 引擎语义笔 = 每日最早确认bar非火箭winner(slot=1)
    st_eng = slot1_trades(strict)
    st_perbar = [s for s in strict if s['winner'] and not s['rocket']]  # 旧口径参考
    closed_eng = [s for s in st_eng if s['ret_pct'] is not None]
    closed_pb = [s for s in st_perbar if s['ret_pct'] is not None]
    closed_all = [s for s in strict if s['ret_pct'] is not None]
    closed_dg = [s for s in degraded if s['ret_pct'] is not None]
    n_e, wr_e, avg_e, cum_e = compute_stats(closed_eng)
    n_pb, wr_pb, avg_pb, cum_pb = compute_stats(closed_pb)
    n_a, wr_a, avg_a, cum_a = compute_stats(closed_all)
    n_d, wr_d, avg_d, cum_d = compute_stats(closed_dg)

    # ---- Task#229 累计测试战绩(准绳口径): t228回放 + REPLAY_END后live slot=1 ----
    replay_closed = load_replay_trades()
    post_live = [s for s in closed_eng if s['date'] > REPLAY_END]
    comb_rows = ([{'ret_pct': t['profit_pct']} for t in replay_closed]
                 + [{'ret_pct': s['ret_pct']} for s in post_live])
    n_c, wr_c, avg_c, cum_c = compute_stats(comb_rows)

    # ---- Task#229 D+1模拟卖出结果通知(仅新了结的slot=1笔, 幂等不重发) ----
    if not args.no_notify:
        slot1_ids = {s['sig_id'] for s in st_eng}
        to_notify = [s for s in st_eng
                     if s['sig_id'] in new_exit_ids and s['sig_id'] in slot1_ids]
        for s in to_notify[-3:]:           # 硬上限3条防积压刷屏(正常=每日1条)
            try:
                if '/home/AIWealth' not in sys.path:
                    sys.path.insert(0, '/home/AIWealth')
                from realtime.notify import send_qywx, send_text
                text_msg, md_msg = build_exit_notice(
                    s, s['exit'], (n_c, wr_c, avg_c, cum_c))
                send_text(text_msg)
                send_qywx(md_msg, msgtype='markdown')
                print(f"  [notify] ⚠️D+1测试卖出结果已推送: {s['sig_id']} "
                      f"{s['ret_pct']:+.2f}%")
            except Exception as e:
                print(f'  [notify] D+1结果通知失败(非致命): {e}')

    # 未了结degraded winner的纸面浮动(qt快照, 一次批量请求)
    floats = {}
    open_dg_w = [s for s in degraded
                 if s['ret_pct'] is None and s['winner']]
    if open_dg_w and not args.no_fetch:
        try:
            raw = _get(QT_URL.format(
                syms=','.join(_sym(s['code']) for s in open_dg_w))) \
                .decode('gbk', errors='replace')
            for line in raw.split(';'):
                if '="' not in line:
                    continue
                sym = line.split('=')[0].strip().lstrip('v_')
                f = line.split('"')[1].split('~')
                if len(f) > 3 and float(f[3] or 0) > 0:
                    floats[sym] = float(f[3])
        except Exception as e:
            print(f'  [WARN] 浮动快照获取失败: {e}')

    # ---- 生成LEDGER ----
    live_days = sorted({s['date'] for s in sigs} | set(intercepts))
    L = []
    L.append('# 打板影子信号滚动台账 (SHADOW_LEDGER)')
    L.append('')
    # Task#293移交(Miller): 下线注记写进生成器模板——本文件每日15:20被'w'模式
    # 整体重写, 直接改.md头部会被冲掉, 故注记必须由这里逐日重新生成
    L.append('> ⚠️ **冲板/回封测试池已于2026-08-11用户裁决下线转回测线，'
             '以下为历史归档+S3假想池在跑**（Task#293实盘链路已下线、'
             '前端测试卡片已移除；本台账保留作数据归档与S3冻结假想跟踪）')
    L.append('')
    L.append(f'- 更新时间: {now} (tools/shadow_ledger.py, Task#183)')
    L.append(f'- 策略: 早盘冲板追击(early_surge_chase_0940)影子探测, '
             f'出场=D+1 0940 bar开盘定时卖(腾讯m5复算) '
             f'— **已否决-仅静默记账**(Task#276, 2026-08-11, 死路#66终版)'
             f'·**已下线转回测线（2026-08-11用户裁决, 前端测试卡片已移除, '
             f'实盘链路已下线, 本台账仅作数据归档）**')
    L.append(f'- live运行日: {", ".join(live_days) or "无"}')
    L.append('')
    L.append('## 严格口径统计 (有量能基线信号; 主统计=引擎语义笔'
             '=每日slot=1唯一笔, 与G5基线530笔(530天×1笔)口径对齐)')
    L.append('')
    L.append('| 口径 | 累计信号 | 已了结 | 胜率 | 均笔收益 | 等权累计收益 |')
    L.append('|---|---|---|---|---|---|')
    L.append(f'| 引擎语义笔(slot=1) | {len(st_eng)} | {n_e} | '
             f'{fmt(wr_e, "%", False)} | {fmt(avg_e)} | {fmt(cum_e)} |')
    L.append(f'| 旧per-bar口径(参考) | {len(st_perbar)} | {n_pb} | '
             f'{fmt(wr_pb, "%", False)} | {fmt(avg_pb)} | {fmt(cum_pb)} |')
    L.append(f'| 全信号(参考) | {len(strict)} | {n_a} | '
             f'{fmt(wr_a, "%", False)} | {fmt(avg_a)} | {fmt(cum_a)} |')
    L.append(f'| **回测G5基线** | {G5_BASELINE["trades"]} '
             f'({G5_BASELINE["span"]}) | 全部 | '
             f'{G5_BASELINE["win_rate"]:.2f}% | '
             f'+{G5_BASELINE["avg_ret"]:.2f}% | — |')
    L.append('')
    L.append(f'> **口径切换({CALIBER_SWITCH_DATE}, Task#229)**: 引擎语义笔'
             '从per-bar口径(每bar winner非火箭)切换为每日最早确认bar的'
             '非火箭winner(slot=1, 每日最多1笔)。原因: Task#228回放实证'
             'bar2/bar3追加winner为负贡献(per-bar 87笔均笔-0.19%), '
             'per-bar记账会污染裁决样本; slot=1与回放A口径/G5基线对齐。')
    L.append(f'> 统计效力: 已了结引擎语义笔 n={n_e}, <20笔前任何对照'
             '仅作快照参考, 无统计意义(t178审计口径)。')
    L.append('')

    # ---- Task#229 累计测试战绩(准绳口径) ----
    L.append('## 累计测试战绩 (Task#229准绳口径) — **已否决-仅静默记账'
             '·已下线转回测线（2026-08-11用户裁决）**')
    L.append('')
    L.append('- ❌ 状态: 策略已全量无偏复验否决(Task#276, 2026-08-11, '
             '死路#66终版——全周期无偏月化5.96%<10%线, 2021/2024两失效年, '
             '构成校正后笔均-0.46%转负); 买入通知已停推, 以下仅为静默记账。')
    L.append('')
    L.append(f'| 总笔数 | 胜率 | 笔均 | 累计模拟收益 |')
    L.append('|---|---|---|---|')
    L.append(f'| {n_c} | {fmt(wr_c, "%", False)} | {fmt(avg_c)} | '
             f'{fmt(cum_c)} |')
    L.append('')
    L.append(f'- 构成: t228回放31日准绳{len(replay_closed)}笔'
             f'(trades_engine_slot1.json, 2026-06-26~{REPLAY_END}) + '
             f'{REPLAY_END}之后live slot=1已了结{len(post_live)}笔; '
             '重叠窗口以回放为准(全市场无偏)。')
    L.append('- 原测试期考核标准已随Task#276终审否决作废(历史参考: 胜率≥48%'
             '且笔均>0, docs/strategies/早盘冲板追击_测试信号说明.md)。')
    L.append('')

    # ---- Task#274 climb_half分桶(观察字段·不参与口径, 纯样本积累) ----
    def _bucket(h):
        return compute_stats([{'ret_pct': s['ret_pct']} for s in closed_all
                              if s.get('climb_half') == h])
    n_lo, wr_lo, avg_lo, _ = _bucket('low')
    n_hi, wr_hi, avg_hi, _ = _bucket('high')
    L.append('### climb_half分桶 (**观察字段·不参与口径**, Task#274)')
    L.append('')
    L.append('| climb_half | 样本量 | WR | 笔均 |')
    L.append('|---|---|---|---|')
    L.append(f'| low(低半区) | {n_lo} | {fmt(wr_lo, "%", False)} | '
             f'{fmt(avg_lo)} |')
    L.append(f'| high(高半区) | {n_hi} | {fmt(wr_hi, "%", False)} | '
             f'{fmt(avg_hi)} |')
    L.append('')
    L.append('- t266残值观察: R1低换手爬升作触板后持续性过滤在无偏窗已触板'
             '子集有+7.4pp WR迹象(n=24太薄); 本分桶仅积累实盘样本(目标'
             '1-2个月), 够量后再判是否引擎验证, **不改冲板口径**。样本='
             '严格口径已了结全信号中climb_half非null笔(观察目的最大化样本); '
             '2026-08-10及之前旧信号无字段不计入。')
    L.append('')

    # ---- Task#274 冲板池逐笔明细(climb_*观察字段列, null显示"-") ----
    def _dash(v, f=None):
        if v is None:
            return '-'
        return f.format(v) if f else str(v)
    if strict:
        L.append('## 冲板池逐笔明细 (严格口径全信号; climb_*=Task#274'
                 '**观察字段·不参与口径**, 无字段旧信号显示"-")')
        L.append('')
        L.append('| 信号日 | 代码 | 名称 | 确认bar | 标记 | 买入价 | '
                 'climb_secs | climb_turn% | climb_half | 出场 | 收益 |')
        L.append('|---|---|---|---|---|---|---|---|---|---|---|')
        for s in strict:
            mark = ('★winner' if s['winner'] else '') + \
                   ('🚀火箭(引擎拒单)' if s['rocket'] else '')
            if s['exit']:
                ex = (f"{s['exit']['exit_date']} @{s['exit']['exit_px']}"
                      + (' ⚠need_review' if s['exit'].get('need_review')
                         else ''))
                ret = fmt(s['ret_pct'])
            else:
                ex, ret = 'OPEN', '—'
            L.append(f"| {s['date']} | {s['code']} | {s['name']} | "
                     f"{s['confirm_bar']} | {mark or '—'} | {s['buy_px']} | "
                     f"{_dash(s.get('climb_secs'))} | "
                     f"{_dash(s.get('climb_turn_pct'), '{:.4f}')} | "
                     f"{_dash(s.get('climb_half'))} | {ex} | {ret} |")
        L.append('')

    # ---- Task#232 回封首板测试池(独立区块, 与冲板池分开) ----
    reseal_state = process_reseal(args.no_fetch, now, today)
    L.extend(reseal_block(reseal_state))

    # ---- Task#285 S3冻结假想池(decision json只读, 异常不炸既有台账) ----
    s3f_trades = []
    try:
        s3f_trades = load_s3_frozen_recs()
        simulate_s3_frozen(s3f_trades)
        L.extend(s3_frozen_block(s3f_trades, today))
    except Exception as e:
        print(f'  [WARN][S3假想池] 区块生成异常(跳过不阻断既有台账): {e!r}')

    # ---- Task#285追加B 候选复盘(复盘参考·不影响任何口径, 异常不炸台账) ----
    try:
        L.extend(candidates_review_block(args.no_fetch, today))
    except Exception as e:
        print(f'  [WARN][候选复盘] 区块生成异常(跳过不阻断既有台账): {e!r}')

    # ---- Task#213 盘中追踪战报(当日) + 滚动表 + 可生产性区块 ----
    ts_today = trackings.get(today)
    if ts_today:
        fin = '终态定格' if ts_today.get('finished') else '盘中实时'
        L.append(f'## 盘中追踪战报 {today} ({fin}, tools/shadow_tracker.py '
                 f'Task#213, 5分钟轮询)')
        L.append('')
        L.append(f'- 信号{ts_today["n_signals"]}笔: '
                 f'触板{ts_today["n_touched"]} / 曾封板{ts_today["n_sealed_ever"]} / '
                 f'炸板{ts_today["n_broken_ever"]} / '
                 f'假信号(全程未触板){ts_today["n_false_signal"]}; '
                 f'盘中封板率{ts_today["seal_rate_intraday_pct"]}%'
                 + (f', 终态封板率{ts_today["seal_rate_final_pct"]}%'
                    if ts_today.get('seal_rate_final_pct') is not None
                    else ''))
        g5 = ts_today.get('sim_g5') or {}
        L.append(f'- G5规则模拟(引擎语义笔{g5.get("n_engine_trades", 0)}笔): '
                 f'当日浮动均笔{fmt(g5.get("avg_float_ret_pct"))} '
                 f'(终局收益以D+1 0940复算为准)')
        L.append('- 路径要点:')
        for sg in ts_today.get('signals', []):
            seg = [f'触板{sg["touch_ts"]}' if sg.get('touched') else '未触板']
            if sg.get('first_seal_ts'):
                seg.append(f'首封{sg["first_seal_ts"]}')
            if sg.get('break_count'):
                seg.append(f'炸板{sg["break_count"]}次')
            seg.append(f'最高溢价{fmt(sg.get("max_premium_pct"))}')
            seg.append(f'最深回撤{fmt(sg.get("max_drawdown_pct"))}')
            L.append(f'  - {sg["name"]}({sg["code"]}) '
                     f'[{sg.get("final_state") or sg["state"]}]: '
                     + ' / '.join(seg))
        L.append('')
    if trackings:
        L.append('## 盘中追踪滚动表 (Task#213, 逐日)')
        L.append('')
        L.append('| 日期 | 信号 | 触板 | 曾封板 | 炸板 | 假信号 | '
                 '盘中封板率 | 终态封板率 |')
        L.append('|---|---|---|---|---|---|---|---|')
        for d in sorted(trackings):
            t = trackings[d]
            fr = t.get('seal_rate_final_pct')
            L.append(
                f'| {d} | {t["n_signals"]} | {t["n_touched"]} | '
                f'{t["n_sealed_ever"]} | {t["n_broken_ever"]} | '
                f'{t["n_false_signal"]} | {t["seal_rate_intraday_pct"]}% | '
                f'{str(fr) + "%" if fr is not None else "盘中"} |')
        L.append('')
        # 可生产性滚动区块: 追踪日累计 + 与G5基线对照 + 差距结论
        cum_n = sum(t['n_signals'] for t in trackings.values())
        cum_seal = sum(t['n_sealed_ever'] for t in trackings.values())
        seal_rate = cum_seal / cum_n * 100 if cum_n else None
        L.append('## 可生产性滚动评估 (Task#213, 持续证据链)')
        L.append('')
        L.append(f'- 盘中追踪覆盖: {len(trackings)}个交易日, '
                 f'累计信号{cum_n}笔, 封板成功率'
                 f'{fmt(seal_rate, "%", False)}')
        L.append(f'- 模拟收益(严格口径引擎语义笔, D+1复算已了结n={n_e}): '
                 f'胜率{fmt(wr_e, "%", False)} 均笔{fmt(avg_e)} vs '
                 f'G5基线胜率{G5_BASELINE["win_rate"]}%/均笔'
                 f'+{G5_BASELINE["avg_ret"]}%')
        n_total_eng = len(st_eng)
        if n_total_eng < 30:
            verdict = (f'样本量不足(引擎语义笔累计{n_total_eng}<30), '
                       '继续影子观察, 不具备实盘化裁决条件')
        elif wr_e is not None and wr_e >= G5_BASELINE['win_rate'] - 5 \
                and avg_e is not None and avg_e > 0:
            verdict = (f'样本{n_total_eng}笔且胜率/均笔不劣于基线-5pp, '
                       '可提交实盘化评审')
        else:
            verdict = (f'样本{n_total_eng}笔但胜率/均笔距基线有差距, '
                       '需归因分析后再裁决')
        L.append(f'- 距实盘化差距结论: {verdict}')
        L.append('')
    L.append('## 降级拦截统计 (STRICT口径: 无量能基线不发信号)')
    L.append('')
    L.append('| 日期 | no_vol_baseline拦截 | 降级放行信号(修复前遗留) |')
    L.append('|---|---|---|')
    dg_by_day = {}
    for s in degraded:
        dg_by_day[s['date']] = dg_by_day.get(s['date'], 0) + 1
    for d in live_days:
        L.append(f'| {d} | {intercepts.get(d, 0)} | {dg_by_day.get(d, 0)} |')
    L.append('')
    if degraded:
        L.append('## 降级样本追踪 (**不计入严格口径统计**, 仅纸面参考)')
        L.append('')
        L.append(f'2026-08-05 22:11改版引入的降级放行缺陷所产信号'
                 f'(Task#183已修复, 详见research/results/t183_shadow_fix/)。'
                 f'共{len(degraded)}笔, 已了结{n_d}笔'
                 + (f'(胜率{fmt(wr_d, "%", False)} 均笔{fmt(avg_d)} '
                    f'等权累计{fmt(cum_d)})' if n_d else '') + '。')
        L.append('')
        L.append('| 信号日 | 代码 | 名称 | 买入价 | 标记 | 出场 | 收益 |')
        L.append('|---|---|---|---|---|---|---|')
        for s in degraded:
            mark = ('★winner' if s['winner'] else '') + \
                   ('🚀火箭(引擎拒单)' if s['rocket'] else '')
            if s['exit']:
                ex = (f"{s['exit']['exit_date']} @{s['exit']['exit_px']}"
                      + (' ⚠need_review' if s['exit'].get('need_review')
                         else ''))
                ret = fmt(s['ret_pct'])
            elif _sym(s['code']) in floats:
                px = floats[_sym(s['code'])]
                ex = f'OPEN(现价{px})'
                ret = fmt((px / s['buy_px'] - 1) * 100) + '(浮动)'
            else:
                ex, ret = 'OPEN', '—'
            L.append(f"| {s['date']} | {s['code']} | {s['name']} | "
                     f"{s['buy_px']} | {mark or '—'} | {ex} | {ret} |")
        L.append('')
    L.append('## 口径备注')
    L.append('')
    L.append('- 等权累计收益=逐笔收益率算术和(每笔等权1单位)。')
    L.append('- 出场价=D+1 minute右端点约定0940 bar(09:35-09:40)开盘价, '
             'minute.db断供期取腾讯m5同标签bar open。')
    L.append('- 跌停顺延(D+1 hour1封死跌停→hour2起顺延卖)未自动化: '
             '0940 bar一字时标need_review人工复核。')
    with open(LEDGER_PATH, 'w', encoding='utf-8') as f:
        f.write('\n'.join(L) + '\n')
    n_rs = len(reseal_state)
    n_rs_c = sum(1 for r in reseal_state.values()
                 if r.get('status') == 'closed')
    n_s3c = sum(1 for t in s3f_trades if t.get('status') == 'closed')
    print(f'[shadow_ledger] 台账已更新: {LEDGER_PATH} '
          f'(严格口径引擎语义: 信号{len(st_eng)}/了结{n_e}; '
          f'降级样本{len(degraded)}/了结{n_d}; '
          f'回封池{n_rs}/了结{n_rs_c}; '
          f'S3假想池{len(s3f_trades)}/了结{n_s3c}; 请求数{_req_count[0]})')


if __name__ == '__main__':
    main()
