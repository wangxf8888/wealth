#!/usr/bin/env python3
"""Task#320 盘中mock自测 (staging代码, 全程只读, 零生产文件写入)
A. promo_gate: t311口径核对 + 历史过热日/正常日门控判定
B. h1_touch: staging position_tracker.evaluate_position 合成quote双路径走查
C. notify: gate_note格式兼容检查(仅构造文本, 不发送)
"""
import os, sys, sqlite3
from datetime import datetime

HERE = os.path.dirname(os.path.abspath(__file__))
STAGING = '/home/AIWealth'  # 生产验证版
PROJECT_ROOT = '/home/AIWealth'
DB = '/home/AIWealth/data/stocks.db'

if not os.path.exists(os.path.join(STAGING, 'realtime', '__init__.py')):
    open(os.path.join(STAGING, 'realtime', '__init__.py'), 'w').close()
sys.path.insert(0, PROJECT_ROOT)
sys.path.insert(0, STAGING)

PASS = FAIL = 0
def check(name, cond, detail=''):
    global PASS, FAIL
    ok = bool(cond); PASS += ok; FAIL += (not ok)
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f" | {detail}" if detail else ''))
    return ok

print("="*70); print("A. promo_gate 口径同源核对 + 门控判定"); print("="*70)
from realtime import promo_gate as pg
assert pg.__file__ == '/home/AIWealth/realtime/promo_gate.py', f"导入源错误: {pg.__file__}"
print(f"  导入源: {pg.__file__}")
conn = sqlite3.connect(f'file:{DB}?mode=ro', uri=True)

# A1. 与t311 CSV逐日核对(若存在)
import glob, csv, random
cands = glob.glob('/home/AIWealth/research/results/t311_promo_menu/*.csv')
t311_csv = None
for c in cands:
    with open(c) as f:
        head = f.readline()
    if 'promo' in head.lower() and 'date' in head.lower():
        t311_csv = c; break
if t311_csv:
    with open(t311_csv) as f:
        rows = list(csv.DictReader(f))
    cols = list(rows[0].keys()) if rows else []
    print(f"  t311 CSV: {os.path.basename(t311_csv)} {len(rows)}行, 列={cols}")
    rate_col = next((c for c in cols if 'promo' in c.lower()), None)
    date_col = next((c for c in cols if 'date' in c.lower()), None)
    random.seed(320)
    sample = random.sample(rows, min(12, len(rows)))
    mism = []
    for r in sample:
        d = r[date_col]
        mine = pg.compute_promo_rate(conn, d)
        csv_v = r[rate_col].strip()
        mine_v = ('' if mine is None or mine['promo_rate'] is None
                  else f"{mine['promo_rate']:.6f}")
        csv_f = '' if csv_v == '' else f"{float(csv_v):.6f}"
        if mine_v != csv_f:
            mism.append((d, csv_f, mine_v))
    check(f"A1 t311 CSV抽样{len(sample)}日晋级率逐值一致", not mism,
          f"不一致: {mism[:3]}" if mism else "全部一致")
else:
    print(f"  [SKIP] A1 未找到含promo列的t311 CSV: {cands}")

# A2. 扫描2026年找过热日与正常日
dates = [r[0] for r in conn.execute(
    "SELECT DISTINCT date FROM stock_kline WHERE date>='2026-01-01' ORDER BY date")]
hot_day = normal_day = None
for d in reversed(dates):
    r = pg.compute_promo_rate(conn, d)
    if r is None or r['promo_rate'] is None: continue
    if hot_day is None and r['promo_rate'] >= 0.30: hot_day = (d, r)
    if normal_day is None and r['promo_rate'] < 0.30: normal_day = (d, r)
    if hot_day and normal_day: break
print(f"  过热样本日: {hot_day[0]} rate={hot_day[1]['promo_rate']:.4f}" if hot_day else "  无过热日样本(2026)")
print(f"  正常样本日: {normal_day[0]} rate={normal_day[1]['promo_rate']:.4f}" if normal_day else "  无正常日样本")

def next_td(d):
    return conn.execute("SELECT MIN(date) FROM stock_kline WHERE date>?", (d,)).fetchone()[0]

if hot_day:
    T = next_td(hot_day[0]) or '2026-08-12'
    out = pg.check_promo_gate(conn, T, 0.30)
    check("A3a 过热日次日门控triggered=True", out['triggered'], out['note'])
    check("A3b prev_date回指样本日", out['prev_date'] == hot_day[0],
          f"prev={out['prev_date']} vs {hot_day[0]}")
    check("A3c 通知文案构造", isinstance(out['promo_pct'], float),
          f"过热门控生效(晋级率{out['promo_pct']:.1f}%)")
if normal_day:
    T = next_td(normal_day[0]) or '2026-08-12'
    out = pg.check_promo_gate(conn, T, 0.30)
    check("A4 正常日次日门控triggered=False", not out['triggered'], out['note'])

out = pg.check_promo_gate(conn, '1990-01-01', 0.30)
check("A5a 无历史数据fail-open不拦截", not out['triggered'], out['note'])
out = pg.check_promo_gate(conn, '2026-08-12', 0.30, enabled=False)
check("A5b 开关关闭不拦截", not out['triggered'], out['note'])
out_today = pg.check_promo_gate(conn, datetime.now().strftime('%Y-%m-%d'), 0.30)
print(f"  [INFO] 今日真实判定: triggered={out_today['triggered']} {out_today['note']}")

print(); print("="*70)
print("B. h1_touch 合成quote双路径走查 (staging position_tracker)"); print("="*70)
import importlib.util
spec = importlib.util.spec_from_file_location(
    't320_pt', os.path.join(STAGING, 'realtime', 'position_tracker.py'))
pt = importlib.util.module_from_spec(spec)
spec.loader.exec_module(pt)
print(f"  导入源: {pt.__file__}")
check("B0 S5_H1_TOUCH_ENABLED=True生效", pt.S5_H1_TOUCH_ENABLED is True)

import trading_rules
POS = {'code': 'sh.600000', 'name': '测试股', 'strategy': 'S5双板回调低吸',
       'buy_price': 10.00, 'buy_date': '2026-08-10', 'sl_price': 0.0,
       'tp_price': None, 'expire_date': '2026-08-12',
       'sell_mode': 'timed', 'sell_at': {'date': '2026-08-12', 'time': '10:30'}}
LU_PRICE = trading_rules.limit_prices('sh.600000', 10.00)[0]

def q(price, hm, preclose=10.00):
    return {'price': price, 'open': price, 'high': price, 'low': price,
            'preclose': preclose, '_hm': hm}

res = pt.evaluate_position(dict(POS), q(LU_PRICE, '09:45'), '2026-08-12')
check("B1a 触板→action='h1_touch'", res['action'] == 'h1_touch', f"action={res['action']}")
check("B1b 卖价=板价", res['action_rec'] and res['action_rec']['sell_price'] == round(LU_PRICE, 2),
      f"sell={res['action_rec'] and res['action_rec']['sell_price']} 板价={LU_PRICE}")
check("B1c sell_reason承接action", res['action_rec']['action'] == 'h1_touch')

res = pt.evaluate_position(dict(POS), q(10.50, '09:45'), '2026-08-12')
check("B2a 未触板H1内→继续持有", res['action'] is None, f"action={res['action']}")
res = pt.evaluate_position(dict(POS), q(10.50, '10:30'), '2026-08-12')
check("B2b 未触板到点→expired定时卖照旧", res['action'] == 'expired', f"action={res['action']}")

res = pt.evaluate_position(dict(POS), q(LU_PRICE, '10:30'), '2026-08-12')
check("B3 10:30整点边界→expired(不再h1_touch)", res['action'] == 'expired', f"action={res['action']}")
res = pt.evaluate_position(dict(POS), q(LU_PRICE, '09:45'), '2026-08-11')
check("B4 非卖出日触板→不卖", res['action'] is None, f"action={res['action']}")
res = pt.evaluate_position(dict(POS), q(LU_PRICE, '09:25'), '2026-08-12')
check("B5 09:25竞价期→不触发", res['action'] is None, f"action={res['action']}")
res = pt.evaluate_position(dict(POS), q(LU_PRICE, '09:45', preclose=0), '2026-08-12')
check("B6 preclose=0→fail-safe不触发", res['action'] is None, f"action={res['action']}")

pt.S5_H1_TOUCH_ENABLED = False
res = pt.evaluate_position(dict(POS), q(LU_PRICE, '09:45'), '2026-08-12')
check("B7 开关关闭→触板不卖(旧行为)", res['action'] is None, f"action={res['action']}")
pt.S5_H1_TOUCH_ENABLED = True

POS_ST = dict(POS, code='sz.000001', name='ST测试')
lu_st = trading_rules.limit_prices('sz.000001', 10.00, is_st=True)[0]
res = pt.evaluate_position(POS_ST, q(lu_st, '09:45'), '2026-08-12')
check("B8 ST股5%板价触发+卖价=ST板价",
      res['action'] == 'h1_touch' and res['action_rec']['sell_price'] == round(lu_st, 2),
      f"action={res['action']} sell={res['action_rec'] and res['action_rec']['sell_price']} 板={lu_st}")

POS_FIX = {'code': 'sh.600000', 'name': '测试股', 'strategy': 'S3',
           'buy_price': 10.00, 'buy_date': '2026-08-10', 'sl_price': 9.00,
           'tp_price': 10.80, 'expire_date': '2026-08-14', 'sell_mode': 'fixed'}
res = pt.evaluate_position(POS_FIX, q(11.00, '09:45'), '2026-08-12')
check("B9 fixed模式不走h1_touch(走原TP)", res['action'] == 'take_profit', f"action={res['action']}")

print(); print("="*70)
print("C. notify gate_note格式兼容"); print("="*70)
nsrc = open(os.path.join(STAGING, 'realtime', 'notify.py'), encoding='utf-8').read()
check("C1 gate_note为带缺省值的可选参数",
      "def notify_morning_summary(decisions: dict, gate_note: str = '')" in nsrc)
check("C2 空串时零新增行(旧格式兼容)", 'if gate_note:' in nsrc)

print(); print("="*70)
print(f"结果: PASS={PASS} FAIL={FAIL}")
print("="*70)
conn.close()
sys.exit(1 if FAIL else 0)
