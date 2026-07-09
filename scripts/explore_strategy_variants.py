#!/usr/bin/env python3
"""策略变种探索 - 8种变种方案独立回测
变种1A: 冲高等回调-低开收阳信号
变种1B: 冲高等回调-突破前收信号
变种1C: 冲高等回调-连阳企稳信号
变种2:  V字+强势市场过滤
变种3:  双信号叠加(冲高回落+V字高开)
变种4:  冲高当日尾盘买入
变种5:  只做炸板(昨日触涨停未封)
变种6:  N字形态(涨->回调->高开)
"""
import sys, sqlite3, math, time
from collections import defaultdict
from datetime import datetime, timedelta
sys.path.insert(0, '/home/AIWealth/scripts')
from compliance_utils import (
    safe_float, is_limit_up, is_limit_down, is_one_word_board, is_st, get_limit_threshold
)

DB_PATH = '/home/AIWealth/data/stocks.db'
START_DATE = '2021-01-01'
END_DATE = '2026-06-30'
CAP = 1_000_000.0
N_SLOTS = 3

def sf(v):
    if v is None: return 0.0
    try:
        f = float(v)
        return 0.0 if (math.isnan(f) or math.isinf(f)) else f
    except: return 0.0

def load_trading_days(conn):
    lo = (datetime.strptime(START_DATE, "%Y-%m-%d") - timedelta(days=60)).strftime("%Y-%m-%d")
    cur = conn.cursor()
    cur.execute("SELECT DISTINCT date FROM stock_kline WHERE date>=? AND date<=? ORDER BY date", (lo, END_DATE))
    ad = [r[0] for r in cur.fetchall()]
    di = {d: i for i, d in enumerate(ad)}
    bt = [d for d in ad if START_DATE <= d <= END_DATE]
    return ad, di, bt

def load_index_data(conn):
    cur = conn.cursor()
    cur.execute("SELECT date, close_rate FROM index_kline WHERE code='sh.000001' AND date>=? AND date<=? ORDER BY date",
                (START_DATE, END_DATE))
    return {r[0]: sf(r[1]) for r in cur.fetchall()}

def get_day_row(conn, code, date):
    cur = conn.cursor()
    cur.execute("""SELECT preclose, open, high, low, close, code_name, isST, open_rate, close_rate, high_rate,
                          hour1_open, hour1_high, hour1_low, hour1_close,
                          hour2_open, hour2_high, hour2_low, hour2_close,
                          hour3_open, hour3_high, hour3_low, hour3_close,
                          hour4_open, hour4_high, hour4_low, hour4_close
                   FROM stock_kline WHERE code=? AND date=?""", (code, date))
    row = cur.fetchone()
    if not row: return None
    return {'preclose': row[0], 'open': row[1], 'high': row[2], 'low': row[3], 'close': row[4],
            'code_name': row[5], 'isST': row[6], 'open_rate': row[7], 'close_rate': row[8], 'high_rate': row[9],
            'h1o': row[10], 'h1h': row[11], 'h1l': row[12], 'h1c': row[13],
            'h2o': row[14], 'h2h': row[15], 'h2l': row[16], 'h2c': row[17],
            'h3o': row[18], 'h3h': row[19], 'h3l': row[20], 'h3c': row[21],
            'h4o': row[22], 'h4h': row[23], 'h4l': row[24], 'h4c': row[25]}

def can_buy(code, row, hour=1):
    if not row: return False, 0
    if is_st(row.get('code_name', '') or '', row.get('isST', 0)): return False, 0
    pc = sf(row.get('preclose'))
    if pc <= 0: return False, 0
    ho = sf(row.get(f'h{hour}o'))
    hh = sf(row.get(f'h{hour}h'))
    hl = sf(row.get(f'h{hour}l'))
    hc = sf(row.get(f'h{hour}c'))
    if ho <= 0: return False, 0
    if is_one_word_board(ho, hh, hl, hc): return False, 0
    if is_limit_up(code, ho, pc): return False, 0
    return True, ho

def can_sell(code, row, hour):
    if not row: return False, 0
    pc = sf(row.get('preclose'))
    ho = sf(row.get(f'h{hour}o'))
    hh = sf(row.get(f'h{hour}h'))
    hl = sf(row.get(f'h{hour}l'))
    hc = sf(row.get(f'h{hour}c'))
    if hc <= 0: return False, 0
    if is_one_word_board(ho, hh, hl, hc) and is_limit_down(code, hc, pc):
        return False, 0
    return True, hc

def run_engine(conn, all_dates, date_idx, bt_dates, signal_func, tp_pct=10.0, sl_pct=-5.0, max_hold=10):
    cash = CAP
    positions = []
    trades = []
    eq_curve = []
    total = len(bt_dates)

    for day_i, today in enumerate(bt_dates):
        if day_i % 200 == 0:
            print(f"    进度: {day_i}/{total} ({day_i/total*100:.0f}%)", flush=True)

        # === 卖出 ===
        surv = []
        for pos in positions:
            if pos['bd'] == today:
                surv.append(pos); continue
            row = get_day_row(conn, pos['code'], today)
            sold = False
            for hour in [1, 2, 3, 4]:
                ok, hc = can_sell(pos['code'], row, hour)
                if not ok: continue
                pnl = (hc / pos['bp'] - 1) * 100
                reason = None
                if pnl >= tp_pct: reason = 'TP'
                elif pnl <= sl_pct: reason = 'SL'
                elif pos['hold_days'] >= max_hold and hour == 4: reason = 'EXP'
                if reason:
                    cash += pos['sh'] * hc
                    trades.append({'code': pos['code'], 'bd': pos['bd'], 'sd': today,
                                   'bp': pos['bp'], 'sp': hc, 'ret': pnl, 'reason': reason,
                                   'hold': pos['hold_days']})
                    sold = True; break
            if not sold:
                surv.append(pos)
        positions = surv

        # === 买入 ===
        if len(positions) < N_SLOTS:
            ti = date_idx.get(today)
            signals = signal_func(conn, ti, all_dates, date_idx, today, positions)
            held = {p['code'] for p in positions}
            for code, bh in signals:
                if len(positions) >= N_SLOTS: break
                if code in held: continue
                row = get_day_row(conn, code, today)
                ok, bp = can_buy(code, row, bh)
                if not ok: continue
                free = N_SLOTS - len(positions)
                alloc = cash / max(1, free)
                sh = int(alloc / bp // 100) * 100
                if sh <= 0: continue
                cost = sh * bp
                if cost > cash: continue
                cash -= cost
                positions.append({'code': code, 'bp': bp, 'bd': today, 'sh': sh, 'hold_days': 0})
                held.add(code)

        # === 日终 ===
        for pos in positions:
            if pos['bd'] != today:
                pos['hold_days'] += 1
        eqv = cash
        for pos in positions:
            row = get_day_row(conn, pos['code'], today)
            if row:
                h4c = sf(row.get('h4c'))
                c = sf(row.get('close'))
                pr = h4c if h4c > 0 else (c if c > 0 else pos['bp'])
            else:
                pr = pos['bp']
            eqv += pos['sh'] * pr
        eq_curve.append((today, eqv))

    # 末日强平
    if positions and bt_dates:
        last = bt_dates[-1]
        for pos in positions:
            row = get_day_row(conn, pos['code'], last)
            sp = pos['bp']
            if row:
                h4c = sf(row.get('h4c'))
                c = sf(row.get('close'))
                sp = h4c if h4c > 0 else (c if c > 0 else pos['bp'])
            pnl = (sp / pos['bp'] - 1) * 100
            cash += pos['sh'] * sp
            trades.append({'code': pos['code'], 'bd': pos['bd'], 'sd': last,
                           'bp': pos['bp'], 'sp': sp, 'ret': pnl, 'reason': 'END',
                           'hold': pos['hold_days']})
    return trades, eq_curve

def compute_stats(trades, eq_curve):
    n = len(trades)
    if n == 0: return {'n': 0, 'cagr': 0, 'wr': 0, 'mdd': 0, 'yearly': {}}
    fe = eq_curve[-1][1] if eq_curve else CAP
    if len(eq_curve) >= 2:
        d0 = datetime.strptime(eq_curve[0][0], '%Y-%m-%d')
        d1 = datetime.strptime(eq_curve[-1][0], '%Y-%m-%d')
        yrs = max(0.1, (d1 - d0).days / 365.25)
    else:
        yrs = 1.0
    cagr = ((fe / CAP) ** (1 / yrs) - 1) * 100 if fe > 0 else -100
    wins = sum(1 for t in trades if t['ret'] > 0)
    wr = wins / n * 100
    pk = eq_curve[0][1] if eq_curve else CAP
    mdd = 0
    for _, v in eq_curve:
        if v > pk: pk = v
        dd = (pk - v) / pk * 100 if pk > 0 else 0
        if dd > mdd: mdd = dd
    yr_eq = {}
    for d, v in eq_curve: yr_eq[d[:4]] = v
    yearly = {}
    prev_eq = CAP
    for y in sorted(yr_eq.keys()):
        ee = yr_eq[y]
        yr_ret = (ee / prev_eq - 1) * 100 if prev_eq > 0 else 0
        yearly[y] = yr_ret
        prev_eq = ee
    return {'n': n, 'cagr': cagr, 'wr': wr, 'mdd': mdd, 'yearly': yearly, 'final': fe}

# ============ 变种信号函数 ============
def get_surge_candidates(conn, date):
    cur = conn.cursor()
    cur.execute("""SELECT code, code_name, high_rate, close_rate, preclose, close, high, open
                   FROM stock_kline WHERE date=? AND high_rate>=7
                   AND (code LIKE 'sz.300%%' OR code LIKE 'sh.688%%')
                   AND isST=0 AND preclose>0""", (date,))
    result = []
    for r in cur:
        code, name, hr, cr, pc, cls, hi, op = r[0], r[1], sf(r[2]), sf(r[3]), sf(r[4]), sf(r[5]), sf(r[6]), sf(r[7])
        if hr <= 0 or pc <= 0: continue
        if cr >= hr * 0.7: continue
        if cls > 0 and is_limit_up(code, cls, pc): continue
        if is_one_word_board(op, hi, hi, cls): continue
        result.append({'code': code, 'name': name or '', 'high_rate': hr, 'close': cls})
    return result

# --- 变种1A ---
def signal_1a(conn, ti, all_dates, date_idx, today, positions):
    signals = []
    if ti is None or ti < 7: return signals
    yesterday = all_dates[ti - 1]
    for offset in range(2, 7):
        si = ti - offset
        if si < 0: continue
        sd = all_dates[si]
        if sd < '2020-01-01': continue
        surge_cands = get_surge_candidates(conn, sd)
        for cand in surge_cands:
            code = cand['code']
            yrow = get_day_row(conn, code, yesterday)
            if not yrow: continue
            opr = sf(yrow.get('open_rate'))
            op = sf(yrow.get('open'))
            cls = sf(yrow.get('close'))
            if opr >= 0: continue
            if cls <= op or op <= 0: continue
            signals.append((code, 1))
            if len(signals) >= N_SLOTS * 3: return signals[:N_SLOTS]
    return signals[:N_SLOTS]

# --- 变种1B ---
def signal_1b(conn, ti, all_dates, date_idx, today, positions):
    signals = []
    if ti is None or ti < 3: return signals
    yesterday = all_dates[ti - 1]
    for offset in range(2, 7):
        si = ti - offset
        if si < 0: continue
        sd = all_dates[si]
        surge_cands = get_surge_candidates(conn, sd)
        for cand in surge_cands:
            code = cand['code']
            trow = get_day_row(conn, code, today)
            if not trow: continue
            h1c = sf(trow.get('h1c'))
            yrow = get_day_row(conn, code, yesterday)
            if not yrow: continue
            y_close = sf(yrow.get('close'))
            if h1c <= 0 or y_close <= 0: continue
            if h1c > y_close:
                signals.append((code, 2))
            if len(signals) >= N_SLOTS * 3: return signals[:N_SLOTS]
    return signals[:N_SLOTS]

# --- 变种1C ---
def signal_1c(conn, ti, all_dates, date_idx, today, positions):
    signals = []
    if ti is None or ti < 4: return signals
    d_m1 = all_dates[ti - 1]
    d_m2 = all_dates[ti - 2]
    for offset in range(3, 8):
        si = ti - offset
        if si < 0: continue
        sd = all_dates[si]
        surge_cands = get_surge_candidates(conn, sd)
        for cand in surge_cands:
            code = cand['code']
            r1 = get_day_row(conn, code, d_m2)
            r2 = get_day_row(conn, code, d_m1)
            if not r1 or not r2: continue
            cr1 = sf(r1.get('close_rate'))
            cr2 = sf(r2.get('close_rate'))
            if cr1 > 0 and cr2 > 0:
                signals.append((code, 1))
            if len(signals) >= N_SLOTS * 3: return signals[:N_SLOTS]
    return signals[:N_SLOTS]

# --- 变种2 ---
INDEX_DATA = {}
def signal_v2(conn, ti, all_dates, date_idx, today, positions):
    global INDEX_DATA
    signals = []
    if ti is None or ti < 5: return signals
    cum = 0
    for off in range(1, 6):
        d = all_dates[ti - off] if ti - off >= 0 else None
        if d and d in INDEX_DATA: cum += INDEX_DATA[d]
    if cum <= 0: return signals
    prev_dates = [all_dates[ti - j] for j in range(1, 4) if ti - j >= 0]
    if len(prev_dates) < 3: return signals
    cur = conn.cursor()
    cur.execute("""SELECT code, code_name, preclose, open_rate, hour1_open, hour1_high, hour1_low, hour1_close
                   FROM stock_kline WHERE date=?
                   AND (code LIKE 'sz.300%%' OR code LIKE 'sh.688%%')
                   AND open_rate>=2.0 AND open_rate<=5.0 AND isST=0 AND preclose>0""", (today,))
    today_stocks = [(r[0], r[1], sf(r[2]), sf(r[3]), sf(r[4]), sf(r[5]), sf(r[6]), sf(r[7])) for r in cur]
    ph = ','.join('?' * len(prev_dates))
    cur.execute(f"SELECT code, date, close_rate FROM stock_kline WHERE date IN ({ph}) AND (code LIKE 'sz.300%%' OR code LIKE 'sh.688%%')", prev_dates)
    cr_map = defaultdict(list)
    for r in cur:
        cr_map[r[0]].append(sf(r[2]))
    for code, name, pc, opr, h1o, h1h, h1l, h1c in today_stocks:
        crs = cr_map.get(code, [])
        if len(crs) < 3: continue
        cum_drop = sum(crs[-3:])
        down_days = sum(1 for c in crs[-3:] if c < 0)
        if cum_drop > -5.0 or down_days < 2: continue
        if h1o <= 0: continue
        if is_one_word_board(h1o, h1h, h1l, h1c): continue
        if is_limit_up(code, h1o, pc): continue
        signals.append((code, 1))
        if len(signals) >= N_SLOTS * 2: break
    return signals[:N_SLOTS]

# --- 变种3 ---
def signal_v3(conn, ti, all_dates, date_idx, today, positions):
    signals = []
    if ti is None or ti < 5: return signals
    cur = conn.cursor()
    cur.execute("""SELECT code, preclose, hour1_open, hour1_high, hour1_low, hour1_close
                   FROM stock_kline WHERE date=?
                   AND (code LIKE 'sz.300%%' OR code LIKE 'sh.688%%')
                   AND open_rate>=2.0 AND open_rate<=5.0 AND isST=0 AND preclose>0""", (today,))
    today_gapup = set()
    for r in cur:
        code, pc, h1o, h1h, h1l, h1c = r[0], sf(r[1]), sf(r[2]), sf(r[3]), sf(r[4]), sf(r[5])
        if h1o <= 0: continue
        if is_one_word_board(h1o, h1h, h1l, h1c): continue
        if is_limit_up(code, h1o, pc): continue
        today_gapup.add(code)
    if not today_gapup: return signals
    for offset in range(3, 8):
        idx = ti - offset
        if idx < 0: continue
        d = all_dates[idx]
        surge_cands = get_surge_candidates(conn, d)
        for cand in surge_cands:
            if cand['code'] in today_gapup:
                signals.append((cand['code'], 1))
            if len(signals) >= N_SLOTS * 2: return signals[:N_SLOTS]
    return signals[:N_SLOTS]

# --- 变种4 ---
def signal_v4(conn, ti, all_dates, date_idx, today, positions):
    signals = []
    cur = conn.cursor()
    cur.execute("""SELECT code, code_name, high_rate, close_rate, preclose, close, high,
                          hour3_open, hour3_high, hour3_low, hour3_close
                   FROM stock_kline WHERE date=? AND high_rate>=7
                   AND (code LIKE 'sz.300%%' OR code LIKE 'sh.688%%')
                   AND isST=0 AND preclose>0""", (today,))
    cands = []
    for r in cur:
        code, name = r[0], r[1]
        hr, cr, pc, cls, hi = sf(r[2]), sf(r[3]), sf(r[4]), sf(r[5]), sf(r[6])
        h3o, h3h, h3l, h3c = sf(r[7]), sf(r[8]), sf(r[9]), sf(r[10])
        if hr <= 0 or pc <= 0: continue
        if cr >= hr * 0.5: continue
        if cls > 0 and is_limit_up(code, cls, pc): continue
        if h3o <= 0: continue
        if is_one_word_board(h3o, h3h, h3l, h3c): continue
        if is_limit_up(code, h3o, pc): continue
        if h3c > 0 and hi > 0 and h3c >= hi * 0.95: continue
        cands.append((code, hr))
    cands.sort(key=lambda x: -x[1])
    for code, _ in cands[:N_SLOTS * 2]:
        signals.append((code, 3))
    return signals[:N_SLOTS]

# --- 变种5 ---
def signal_v5(conn, ti, all_dates, date_idx, today, positions):
    signals = []
    if ti is None or ti < 1: return signals
    yesterday = all_dates[ti - 1]
    cur = conn.cursor()
    cur.execute("""SELECT code, code_name, preclose, high, close, amount
                   FROM stock_kline WHERE date=?
                   AND (code LIKE 'sz.300%%' OR code LIKE 'sh.688%%')
                   AND isST=0 AND preclose>0 AND amount>=5e7""", (yesterday,))
    yest_cands = []
    for r in cur:
        code, name, pc, hi, cls, amt = r[0], r[1], sf(r[2]), sf(r[3]), sf(r[4]), sf(r[5])
        if pc <= 0 or hi <= 0: continue
        is_gem = code.startswith('sz.30') or code.startswith('sh.688')
        limit_ratio = 1.198 if is_gem else 1.098
        if hi < pc * limit_ratio: continue
        limit_price = round(pc * limit_ratio, 2)
        if cls >= limit_price - 0.01: continue
        yest_cands.append({'code': code, 'name': name, 'amount': amt})
    yest_cands.sort(key=lambda x: -x['amount'])
    for cand in yest_cands[:N_SLOTS * 3]:
        code = cand['code']
        trow = get_day_row(conn, code, today)
        if not trow: continue
        pc = sf(trow.get('preclose'))
        h1o = sf(trow.get('h1o'))
        h1h = sf(trow.get('h1h'))
        h1l = sf(trow.get('h1l'))
        h1c = sf(trow.get('h1c'))
        if h1o <= 0 or pc <= 0: continue
        if is_one_word_board(h1o, h1h, h1l, h1c): continue
        if is_limit_up(code, h1o, pc): continue
        opr = (h1o / pc - 1) * 100
        if opr > 5: continue
        signals.append((code, 1))
        if len(signals) >= N_SLOTS * 2: break
    return signals[:N_SLOTS]

# --- 变种6 ---
def signal_v6(conn, ti, all_dates, date_idx, today, positions):
    signals = []
    if ti is None or ti < 6: return signals
    cur = conn.cursor()
    cur.execute("""SELECT code, code_name, preclose, open_rate,
                          hour1_open, hour1_high, hour1_low, hour1_close
                   FROM stock_kline WHERE date=?
                   AND (code LIKE 'sz.300%%' OR code LIKE 'sh.688%%')
                   AND open_rate>=2.0 AND isST=0 AND preclose>0""", (today,))
    today_stocks = []
    for r in cur:
        code, name, pc, opr = r[0], r[1], sf(r[2]), sf(r[3])
        h1o, h1h, h1l, h1c = sf(r[4]), sf(r[5]), sf(r[6]), sf(r[7])
        if h1o <= 0 or pc <= 0: continue
        if is_one_word_board(h1o, h1h, h1l, h1c): continue
        if is_limit_up(code, h1o, pc): continue
        today_stocks.append((code, name, opr))
    if not today_stocks: return signals
    dates_up = [all_dates[ti - j] for j in range(3, 6) if ti - j >= 0]
    dates_dn = [all_dates[ti - j] for j in range(1, 3) if ti - j >= 0]
    if len(dates_up) < 2 or len(dates_dn) < 2: return signals
    all_check = dates_up + dates_dn
    ph = ','.join('?' * len(all_check))
    cur2 = conn.cursor()
    cur2.execute(f"SELECT code, date, close_rate FROM stock_kline WHERE date IN ({ph}) AND (code LIKE 'sz.300%%' OR code LIKE 'sh.688%%')", all_check)
    cr_map = defaultdict(dict)
    for r in cur2:
        cr_map[r[0]][r[1]] = sf(r[2])
    for code, name, opr in today_stocks:
        crs = cr_map.get(code, {})
        up_sum = sum(crs.get(d, 0) for d in dates_up)
        dn_sum = sum(crs.get(d, 0) for d in dates_dn)
        if up_sum >= 8.0 and dn_sum <= -3.0:
            signals.append((code, 1))
        if len(signals) >= N_SLOTS * 2: break
    return signals[:N_SLOTS]

# ============ 主程序 ============
def main():
    global INDEX_DATA
    t0 = time.time()
    print("=" * 60)
    print("===== 策略变种探索报告 =====")
    print(f"数据: {START_DATE} ~ {END_DATE}")
    print("=" * 60)

    conn = sqlite3.connect(DB_PATH)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA cache_size=-200000")

    print("\n[加载] 交易日...", flush=True)
    all_dates, date_idx, bt_dates = load_trading_days(conn)
    print(f"  交易日: {len(bt_dates)}天 ({bt_dates[0]} ~ {bt_dates[-1]})")

    print("[加载] 大盘指数数据...", flush=True)
    INDEX_DATA = load_index_data(conn)
    print(f"  大盘数据: {len(INDEX_DATA)}天")

    variants = [
        ('变种1A(冲高等回调-低开收阳)', signal_1a, 10.0, -5.0, 10),
        ('变种1B(冲高等回调-突破前收)', signal_1b, 10.0, -5.0, 10),
        ('变种1C(冲高等回调-连阳企稳)', signal_1c, 10.0, -5.0, 10),
        ('变种2(V字+强势市场)', signal_v2, 3.0, -2.0, 3),
        ('变种3(双信号叠加)', signal_v3, 8.0, -4.0, 8),
        ('变种4(冲高当日尾盘买)', signal_v4, 10.0, -5.0, 10),
        ('变种5(只做炸板)', signal_v5, 10.0, -5.0, 8),
        ('变种6(N字形态)', signal_v6, 8.0, -4.0, 5),
    ]

    results = []
    for i, (name, sig_func, tp, sl, mh) in enumerate(variants):
        t1 = time.time()
        print(f"\n{'_'*60}")
        print(f"[{i+1}/{len(variants)}] 回测: {name}  TP={tp}% SL={sl}% MaxHold={mh}天")
        print(f"{'_'*60}", flush=True)

        trades, eq_curve = run_engine(conn, all_dates, date_idx, bt_dates, sig_func,
                                       tp_pct=tp, sl_pct=sl, max_hold=mh)
        stats = compute_stats(trades, eq_curve)
        elapsed = time.time() - t1

        if stats['n'] < 50:
            print(f"  WARNING: 样本不足: 仅{stats['n']}笔交易 (<50), 跳过")
            results.append((name, stats, 'INSUFFICIENT'))
        else:
            yr_str = ' '.join(f"{y}:{v:+.0f}%" for y, v in sorted(stats['yearly'].items()))
            print(f"  {name}: CAGR={stats['cagr']:+.1f}% WR={stats['wr']:.1f}% MDD=-{stats['mdd']:.1f}% N={stats['n']}")
            print(f"  {yr_str}")
            results.append((name, stats, 'OK'))
        print(f"  耗时: {elapsed:.1f}s")

    # ===== 排行榜 =====
    print(f"\n{'='*60}")
    print("===== 排行榜 (按CAGR排序) =====")
    print(f"{'='*60}")
    valid = [(name, s, tag) for name, s, tag in results if tag == 'OK']
    valid.sort(key=lambda x: -x[1]['cagr'])
    for rank, (name, s, _) in enumerate(valid, 1):
        yr_str = ' '.join(f"{y}:{v:+.0f}%" for y, v in sorted(s['yearly'].items()))
        print(f"#{rank}: {name}")
        print(f"     CAGR={s['cagr']:+.1f}% WR={s['wr']:.1f}% MDD=-{s['mdd']:.1f}% N={s['n']}")
        print(f"     {yr_str}")

    insufficient = [(name, s, tag) for name, s, tag in results if tag != 'OK']
    if insufficient:
        print(f"\n--- 样本不足 (N<50) ---")
        for name, s, _ in insufficient:
            print(f"  {name}: N={s['n']}")

    conn.close()
    print(f"\n{'='*60}")
    print(f"总耗时: {time.time()-t0:.1f}s")
    print(f"完成时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")


if __name__ == '__main__':
    main()
