#!/usr/bin/env python3
"""
多指标评分系统 - 参数网格搜索优化 V2
按年加载数据避免OOM, 所有combo共享同年数据。
"""
import sys
import os

if not sys.stdout.isatty():
    import io
    sys.stdout = io.TextIOWrapper(open(sys.stdout.fileno(), 'wb', 0), write_through=True)
    sys.stderr = io.TextIOWrapper(open(sys.stderr.fileno(), 'wb', 0), write_through=True)

import sqlite3
import json
import math
import time
import random
import copy
import gc
from collections import defaultdict
from datetime import datetime

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECT_ROOT = os.path.dirname(SCRIPT_DIR)
sys.path.insert(0, PROJECT_ROOT)

from engine.indicators import INDICATOR_REGISTRY
from engine.scoring_engine import ScoringEngine
from engine.buy_strategies import BuyStrategyManager, ScoreBuyStrategy, VShapeBuyStrategy
from engine.sell_strategies import SellStrategyManager, FixedTPSL, TimeLimitExit

DB_PATH = os.path.join(PROJECT_ROOT, 'data', 'stocks.db')
RESULTS_OUTPUT = os.path.join(SCRIPT_DIR, 'optimize_v2_results.json')
HIST_LB = 20

NEEDED_COLS = (
    'date,code,code_name,preclose,open,open_rate,high,high_rate,low,low_rate,'
    'close,close_rate,volume,turn,isST,'
    'hour1_open,hour1_high,hour1_low,hour1_close,'
    'hour2_open,hour2_high,hour2_low,hour2_close,'
    'hour3_open,hour3_high,hour3_low,hour3_close,'
    'hour4_open,hour4_high,hour4_low,hour4_close'
)
COL_LIST = [c.strip() for c in NEEDED_COLS.split(',')]


def _sf(val, d=0.0):
    if val is None: return d
    try:
        f = float(val)
        return d if (math.isnan(f) or math.isinf(f)) else f
    except (ValueError, TypeError):
        return d


def _gs(code):
    if not code: return False
    c = code.replace('sz.','').replace('sh.','')
    return c.startswith('300') or c.startswith('301') or c.startswith('688')


def _lur(code): return 0.2 if _gs(code) else 0.1
def _ldr(code): return 0.2 if _gs(code) else 0.1


def _limit_up_buy(row):
    code = row['code']
    pc = _sf(row['preclose'])
    if pc <= 0: return True
    lu = round(pc * (1 + _lur(code)), 2)
    o, h, c = _sf(row['open']), _sf(row['high']), _sf(row['close'])
    if o > 0 and h > 0 and c > 0:
        if abs(o-h)<0.01 and abs(o-c)<0.01 and o >= lu*0.998:
            return True
    return False


def _buy_ok(bp, pc, code):
    if bp <= 0 or pc <= 0: return False
    lu = round(pc * (1 + _lur(code)), 2)
    return bp < lu * 0.998


def _sell_blocked(sp, pc, code):
    if sp <= 0 or pc <= 0: return True
    ld = round(pc * (1 - _ldr(code)), 2)
    return sp <= ld * 1.002


def _oneword_ld(row):
    code = row['code']
    pc = _sf(row['preclose'])
    if pc <= 0: return True
    ld = round(pc * (1 - _ldr(code)), 2)
    o,h,lo,c = _sf(row['open']),_sf(row['high']),_sf(row['low']),_sf(row['close'])
    if o>0 and h>0 and lo>0 and c>0:
        if abs(o-h)<0.01 and abs(o-lo)<0.01 and abs(o-c)<0.01 and o<=ld*1.002:
            return True
    return False


def load_chunk(conn, start, end):
    sql = f"SELECT {NEEDED_COLS} FROM stock_kline WHERE date>=? AND date<=? ORDER BY code,date"
    cur = conn.execute(sql, (start, end))
    dbc = defaultdict(list)
    n = 0
    for t in cur.fetchall():
        row = dict(zip(COL_LIST, t))
        dbc[row['code']].append(row)
        n += 1
    return dbc, n


def build_idx(dbc):
    idx = {}
    for code, rows in dbc.items():
        m = {}
        for i, r in enumerate(rows):
            m[r['date']] = i
        idx[code] = m
    return idx


class BState:
    """回测状态"""
    __slots__ = ['params','cash','positions','trades','nav_hist','peak',
                 't1_sigs','scoring','buy_mgr','sell_mgr']

    def __init__(self, params):
        self.params = params
        self.cash = 1_000_000.0
        self.positions = []
        self.trades = []
        self.nav_hist = []
        self.peak = 1_000_000.0
        self.t1_sigs = {}
        # build engines
        p = params
        ind_cfgs = {}
        for name, cfg in p['indicators'].items():
            if name not in INDICATOR_REGISTRY: continue
            e = {'weight': cfg.get('weight',1.0), 'star_condition': cfg.get('star_condition','>0.5')}
            for k,v in cfg.items():
                if k not in ('weight','star_condition'): e[k]=v
            ind_cfgs[name] = e
        self.scoring = ScoringEngine(ind_cfgs, p['min_stars'], 'star')
        self.buy_mgr = BuyStrategyManager([
            ScoreBuyStrategy(min_stars=p['min_stars']), VShapeBuyStrategy()])
        self.sell_mgr = SellStrategyManager([
            FixedTPSL(tp_pct=p['tp_pct'], sl_pct=p['sl_pct']),
            TimeLimitExit(max_hold_days=p['max_hold_days'], exit_hour=4)])

    @property
    def equity(self):
        return self.cash + sum(p['shares']*p.get('cp',p['bp']) for p in self.positions)

    def run_dates(self, dates, dbc, cidx):
        p = self.params
        ns = p['n_slots']
        mh = p['max_hold_days']
        for date in dates:
            self._sells(date, dbc, cidx, mh)
            self._buys(date, dbc, cidx, ns)
            self._upd(date, dbc, cidx)

    def _hd(self, row, h):
        return {'open':_sf(row.get(f'hour{h}_open')),
                'high':_sf(row.get(f'hour{h}_high')),
                'low':_sf(row.get(f'hour{h}_low')),
                'close':_sf(row.get(f'hour{h}_close'))}

    def _sells(self, date, dbc, cidx, mh):
        for pos in list(self.positions):
            code = pos['code']
            if pos['hd'] == 0: continue
            if code not in cidx or date not in cidx[code]: continue
            row = dbc[code][cidx[code][date]]
            pc = _sf(row['preclose'])
            if _oneword_ld(row):
                if pos['hd'] >= mh*2:
                    cp = _sf(row['close'])
                    if cp > 0: self._sell(pos, cp, date)
                continue
            sold = False
            for h in range(1,5):
                hd = self._hd(row, h)
                if hd['open'] <= 0: continue
                dd = {'volume':_sf(row.get('volume')),'close_rate':_sf(row.get('close_rate')),'prev_volume':0}
                iv = cidx[code][date]
                if iv > 0: dd['prev_volume'] = _sf(dbc[code][iv-1].get('volume'))
                ok, sp, _ = self.sell_mgr.check_position(pos, h, hd, dd)
                if ok and sp > 0:
                    if _sell_blocked(sp, pc, code): continue
                    self._sell(pos, sp, date); sold=True; break
            if not sold:
                if all(_sf(row.get(f'hour{hh}_open'))<=0 for hh in range(1,5)):
                    do = _sf(row.get('open'))
                    if do > 0:
                        fb = {'open':do,'high':_sf(row['high']),'low':_sf(row['low']),'close':_sf(row['close'])}
                        dd = {'volume':_sf(row.get('volume')),'close_rate':_sf(row.get('close_rate')),'prev_volume':0}
                        iv = cidx[code][date]
                        if iv>0: dd['prev_volume']=_sf(dbc[code][iv-1].get('volume'))
                        ok,sp,_ = self.sell_mgr.check_position(pos,4,fb,dd)
                        if ok and sp>0 and not _sell_blocked(sp,pc,code):
                            self._sell(pos,sp,date); sold=True
            if not sold and pos['hd']>=mh*2:
                cp=_sf(row['close'])
                if cp>0: self._sell(pos,cp,date); sold=True
            if not sold:
                for h in range(1,5):
                    hd=self._hd(row,h)
                    if hd['high']>pos['mx']: pos['mx']=hd['high']
                dh=_sf(row['high'])
                if dh>pos['mx']: pos['mx']=dh

    def _sell(self, pos, price, date):
        pnl = (price/pos['bp']-1)*100
        self.trades.append({'pnl_pct':round(pnl,2),'sell_date':date})
        self.cash += pos['shares']*price
        self.positions.remove(pos)

    def _quick_trigger_possible(self, row, hist):
        """快速预判是否有任何trigger可能亮 - 避免全量scoring"""
        if not hist: return False
        yest = hist[-1]
        code = row.get('code','')
        # Trigger1: big_drop_gap_up - yest close_rate < -4 AND today open_rate >= 2
        yest_cr = _sf(yest.get('close_rate'))
        today_or = _sf(row.get('open_rate'))
        if yest_cr <= -4.0 and 2.0 <= today_or <= 20.0:
            return True
        # Trigger2: limit_down_reversal - yest was ~limit down AND today close>open
        yc = _sf(yest.get('close')); ypc = _sf(yest.get('preclose'))
        if yc > 0 and ypc > 0:
            ratio = yc / ypc
            thresh = 0.805 if _gs(code) else 0.905
            if ratio <= thresh:
                tc = _sf(row.get('close')); to = _sf(row.get('open'))
                if tc > 0 and to > 0 and tc > to:
                    return True
        # Trigger3: limit_up_next - yest was limit up
        if yc > 0 and ypc > 0:
            ratio2 = round(yc / ypc, 2)
            lu_thresh = 1.20 if _gs(code) else 1.10
            if ratio2 >= lu_thresh:
                return True
        # VShape: GEM + cum_drop + gap_up
        if _gs(code) and today_or >= 4.0 and _sf(row.get('turn')) >= 8.0:
            if len(hist) >= 4:
                cum = sum(_sf(h.get('close_rate')) for h in hist[-4:])
                if cum <= -3.0:
                    return True
        return False

    def _buys(self, date, dbc, cidx, ns):
        if len(self.positions) >= ns:
            self.t1_sigs.clear(); return
        sigs = []
        held_codes = set(p['code'] for p in self.positions)
        for code, sig in list(self.t1_sigs.items()):
            if code in held_codes: continue
            if code in cidx and date in cidx[code]:
                row = dbc[code][cidx[code][date]]
                bp = _sf(row.get('hour1_open'))
                pc = _sf(row['preclose'])
                if bp>0 and pc>0 and _buy_ok(bp,pc,code) and not _limit_up_buy(row):
                    sig['buy_price']=bp; sig['code']=code; sigs.append(sig)
        self.t1_sigs.clear()
        sig_codes = set(s.get('code') for s in sigs)
        for code, rows in dbc.items():
            if code in held_codes: continue
            if code in sig_codes: continue
            if code not in cidx or date not in cidx[code]: continue
            iv = cidx[code][date]
            row = rows[iv]
            if row.get('isST')==1 or row.get('isST')=='1': continue
            nm = row.get('code_name','') or ''
            if 'ST' in nm.upper(): continue
            if _sf(row.get('turn'))<1.0: continue
            if _limit_up_buy(row): continue
            # FAST PRE-FILTER: skip if no trigger can fire
            hs = max(0,iv-HIST_LB)
            hist = rows[hs:iv]
            if not self._quick_trigger_possible(row, hist):
                continue
            try: sr = self.scoring.score_stock(row, hist)
            except: continue
            try: signals = self.buy_mgr.scan_signals(row, hist, sr)
            except: continue
            for sig in signals:
                if not sig.get('signal'): continue
                if sig.get('is_t1_signal'):
                    self.t1_sigs[code]={'code':code,'score':sr.get('total_score',0),
                        'star_count':sr.get('star_count',0),'strategy_name':sig.get('strategy_name','')}
                    continue
                bp=_sf(sig.get('buy_price')); pc=_sf(row['preclose'])
                if bp>0 and pc>0 and _buy_ok(bp,pc,code):
                    sigs.append({'code':code,'buy_price':bp,
                        'score':sr.get('total_score',0),'star_count':sr.get('star_count',0)})
        if not sigs: return
        sigs.sort(key=lambda s:(s.get('star_count',0),s.get('score',0)),reverse=True)
        eq=self.equity; ss=eq/ns
        for sig in sigs:
            if len(self.positions)>=ns: break
            code=sig['code']
            if code in held_codes: continue
            price=sig['buy_price']
            shares=int(ss/price/100)*100
            if shares<100: continue
            cost=shares*price
            if cost>self.cash:
                shares=int(self.cash/price/100)*100
                if shares<100: continue
                cost=shares*price
            self.cash-=cost
            self.positions.append({'code':code,'bp':price,'shares':shares,
                'buy_date':date,'hd':0,'mx':price,'cp':price})
            held_codes.add(code)

    def _upd(self, date, dbc, cidx):
        for pos in self.positions:
            pos['hd']+=1
            code=pos['code']
            if code in cidx and date in cidx[code]:
                c=_sf(dbc[code][cidx[code][date]]['close'])
                if c>0:
                    pos['cp']=c
                    if c>pos['mx']: pos['mx']=c
        nav=self.equity
        if nav>self.peak: self.peak=nav
        dd=(nav/self.peak-1)*100 if self.peak>0 else 0
        self.nav_hist.append({'date':date,'nav':nav,'dd':dd})

    def metrics(self):
        if not self.nav_hist or len(self.trades)<5: return None
        ini=1_000_000.0
        fn=self.nav_hist[-1]['nav']
        try:
            days=(datetime.strptime(self.nav_hist[-1]['date'],'%Y-%m-%d')-
                  datetime.strptime(self.nav_hist[0]['date'],'%Y-%m-%d')).days
        except: days=365
        yrs=max(days/365.25,0.01)
        cagr=((fn/ini)**(1/yrs)-1)*100
        mdd=min(n['dd'] for n in self.nav_hist)
        calmar=cagr/abs(mdd) if mdd!=0 else 0
        dr=[]
        for i in range(1,len(self.nav_hist)):
            p=self.nav_hist[i-1]['nav']
            if p>0: dr.append(self.nav_hist[i]['nav']/p-1)
        if dr:
            avg=sum(dr)/len(dr)
            std=(sum((r-avg)**2 for r in dr)/len(dr))**0.5
            sharpe=(avg/std*252**0.5) if std>0 else 0
        else: sharpe=0
        nt=len(self.trades)
        wins=sum(1 for t in self.trades if t['pnl_pct']>0)
        wr=wins/nt*100 if nt>0 else 0
        yearly={}
        ynm=defaultdict(list)
        for n in self.nav_hist: ynm[n['date'][:4]].append(n)
        ytm=defaultdict(list)
        for t in self.trades: ytm[t['sell_date'][:4]].append(t)
        for yr in sorted(ynm.keys()):
            ns2=ynm[yr]
            ret=(ns2[-1]['nav']/ns2[0]['nav']-1)*100 if ns2[0]['nav']>0 else 0
            yt=ytm.get(yr,[])
            yw=sum(1 for t in yt if t['pnl_pct']>0)
            yearly[yr]={'return':round(ret,1),'trades':len(yt),'win_rate':round(yw/len(yt)*100 if yt else 0,1)}
        return {'cagr':round(cagr,2),'mdd':round(mdd,2),'calmar':round(calmar,2),
                'sharpe':round(sharpe,2),'trades':nt,'win_rate':round(wr,1),
                'total_return':round((fn/ini-1)*100,1),'yearly':yearly}


def gen_samples(n=100):
    random.seed(42)
    out=[]
    for _ in range(n):
        out.append({'min_stars':random.choice([2,3,4,5,6]),
            'drop_thresh':random.choice([-4,-5,-6,-7]),
            'gap_min':random.choice([2,3,4,5]),
            'cum_lookback':random.choice([5,8,10,15]),
            'cum_thresh':random.choice([-5,-8,-10,-15]),
            'vol_ratio':random.choice([2.0,2.5,3.0,3.5]),
            'amp_thresh':random.choice([3,5,7,10])})
    return out


def mkp(s, tp=10.0, sl=-3.0, hold=5, slots=5):
    return {'min_stars':s['min_stars'],'tp_pct':tp,'sl_pct':sl,
            'max_hold_days':hold,'n_slots':slots,
            'indicators':{
                'big_drop_gap_up':{'weight':1.0,'star_condition':'>0.5',
                    'drop_threshold':float(s['drop_thresh']),'gap_min':float(s['gap_min']),'gap_max':20.0},
                'limit_down_reversal':{'weight':1.0,'star_condition':'>0.5'},
                'limit_up_next':{'weight':1.0,'star_condition':'>0.5'},
                'cum_drop_first_positive':{'weight':1.0,'star_condition':'>0.5',
                    'lookback':int(s['cum_lookback']),'cum_drop_threshold':float(s['cum_thresh'])},
                'volume_breakout':{'weight':1.0,'star_condition':'>0.5','lookback':5,'ratio':float(s['vol_ratio'])},
                'amplitude_positive':{'weight':1.0,'star_condition':'>0.5','amp_threshold':float(s['amp_thresh'])},
                'rsi_oversold_bounce':{'weight':1.0,'star_condition':'>0.5','period':14,'oversold':30.0},
            }}


def run_year(conn, states, label, ds, de, ts, te):
    """加载一年数据，跑所有states"""
    t0=time.time()
    print(f"  [{label}] 加载 {ds}~{de} ...", end=' ')
    dbc, n = load_chunk(conn, ds, de)
    cidx = build_idx(dbc)
    dates = sorted(set(r['date'] for rows in dbc.values() for r in rows if ts<=r['date']<=te))
    lt=time.time()-t0
    print(f"{n:,}行 {len(dbc)}股 {len(dates)}日 {lt:.0f}s")
    ns=len(states)
    step=max(1,ns//10)
    for i,st in enumerate(states):
        st.run_dates(dates, dbc, cidx)
        if (i+1)%step==0:
            print(f"    [{label}] {i+1}/{ns} ({time.time()-t0:.0f}s)")
    del dbc, cidx
    gc.collect()
    print(f"  [{label}] done ({time.time()-t0:.0f}s)")


def run_all_years(conn, states, year_chunks):
    for label, ds, de, ts, te in year_chunks:
        run_year(conn, states, label, ds, de, ts, te)


def ptop(results, n=10, title="Top"):
    print(f"\n{'='*85}")
    print(f"  {title}")
    print(f"{'='*85}")
    print(f"{'#':<4}{'CAGR%':>9}{'MDD%':>8}{'Calmar':>8}{'Sharpe':>7}"
          f"{'Trades':>7}{'WR%':>6}{'*':>3}{'TP':>5}{'SL':>5}{'Hd':>4}{'N':>3}")
    print("-"*75)
    for i,r in enumerate(results[:n]):
        p,m=r['params'],r['metrics']
        print(f"{i+1:<4}{m['cagr']:>+9.1f}{m['mdd']:>8.1f}{m['calmar']:>8.2f}"
              f"{m['sharpe']:>7.2f}{m['trades']:>7}{m['win_rate']:>6.1f}"
              f"{p['min_stars']:>3}{p['tp_pct']:>5.0f}{p['sl_pct']:>5.0f}"
              f"{p['max_hold_days']:>4}{p['n_slots']:>3}")


def main():
    T0=time.time()
    print("="*85)
    print("  参数网格搜索优化 V2 (按年加载,避免OOM)")
    print("="*85)
    conn=sqlite3.connect(DB_PATH)
    train_yrs=[
        ('2023','2022-11-20','2023-12-31','2023-01-01','2023-12-31'),
        ('2024','2023-12-01','2024-12-31','2024-01-01','2024-12-31'),
        ('2025','2024-12-01','2025-12-31','2025-01-01','2025-12-31'),
    ]

    # === Phase 1 ===
    print(f"\n{'='*85}")
    print("  Phase1: 指标+min_stars (tp=10,sl=-3,hold=5,N=5)")
    print(f"{'='*85}")
    samples=gen_samples(50)
    p1_params=[mkp(s) for s in samples]
    print(f"  {len(p1_params)} combos")
    states=[BState(p) for p in p1_params]
    run_all_years(conn, states, train_yrs)
    p1_res=[]
    for i,st in enumerate(states):
        m=st.metrics()
        if m and m['trades']>=30 and m['mdd']>-40:
            p1_res.append({'params':p1_params[i],'metrics':m,'sample':samples[i]})
    p1_res.sort(key=lambda r:r['metrics']['calmar'],reverse=True)
    print(f"\n  Phase1: {len(p1_res)}/{len(samples)} valid")
    ptop(p1_res,30,"Phase1 Top30")
    print(f"  Phase1 time: {(time.time()-T0)/60:.1f}min")
    del states; gc.collect()

    if not p1_res:
        print("[ERROR] No valid Phase1 results"); conn.close(); return

    # === Phase 2 ===
    print(f"\n{'='*85}")
    print("  Phase2: 卖出参数 (Top5 base × tp×sl×hold)")
    print(f"{'='*85}")
    tp_l=[5,8,10,12,15]; sl_l=[-2,-3,-5]; hd_l=[3,5,7]
    p2_params=[]; p2_samples=[]
    for base in p1_res[:5]:
        s=base['sample']
        for tp in tp_l:
            for sl in sl_l:
                for hd in hd_l:
                    p2_params.append(mkp(s,tp=tp,sl=sl,hold=hd))
                    p2_samples.append(s)
    print(f"  {len(p2_params)} combos")
    states2=[BState(p) for p in p2_params]
    run_all_years(conn, states2, train_yrs)
    p2_res=[]
    for i,st in enumerate(states2):
        m=st.metrics()
        if m and m['trades']>=20 and m['mdd']>-30:
            p2_res.append({'params':p2_params[i],'metrics':m,'sample':p2_samples[i]})
    p2_res.sort(key=lambda r:r['metrics']['calmar'],reverse=True)
    print(f"\n  Phase2: {len(p2_res)}/{len(p2_params)} valid")
    ptop(p2_res,20,"Phase2 Top20")
    print(f"  Phase2 time: {(time.time()-T0)/60:.1f}min")
    del states2; gc.collect()
    if not p2_res: p2_res=p1_res[:10]

    # === Phase 3 ===
    print(f"\n{'='*85}")
    print("  Phase3: 仓位数")
    print(f"{'='*85}")
    sl_n=[3,5,7,10]
    p3_params=[]; p3_samples=[]
    for base in p2_res[:10]:
        s=base.get('sample',{})
        for n in sl_n:
            p=copy.deepcopy(base['params']); p['n_slots']=n
            p3_params.append(p); p3_samples.append(s)
    print(f"  {len(p3_params)} combos")
    states3=[BState(p) for p in p3_params]
    run_all_years(conn, states3, train_yrs)
    p3_res=[]
    for i,st in enumerate(states3):
        m=st.metrics()
        if m and m['trades']>=15:
            p3_res.append({'params':p3_params[i],'metrics':m})
    p3_res.sort(key=lambda r:r['metrics']['calmar'],reverse=True)
    print(f"\n  Phase3: {len(p3_res)}/{len(p3_params)} valid")
    ptop(p3_res,10,"Phase3 Top10")
    print(f"  Phase3 time: {(time.time()-T0)/60:.1f}min")
    del states3; gc.collect()
    if not p3_res: p3_res=p2_res[:10]

    # === Phase 4: full validation ===
    print(f"\n{'='*85}")
    print("  Phase4: 全量验证 2020-2026")
    print(f"{'='*85}")
    full_yrs=[
        ('2020','2019-11-20','2020-12-31','2020-01-01','2020-12-31'),
        ('2021','2020-12-01','2021-12-31','2021-01-01','2021-12-31'),
        ('2022','2021-12-01','2022-12-31','2022-01-01','2022-12-31'),
        ('2023','2022-12-01','2023-12-31','2023-01-01','2023-12-31'),
        ('2024','2023-12-01','2024-12-31','2024-01-01','2024-12-31'),
        ('2025','2024-12-01','2025-12-31','2025-01-01','2025-12-31'),
        ('2026','2025-12-01','2026-06-30','2026-01-01','2026-06-30'),
    ]
    p4_params=[r['params'] for r in p3_res[:10]]
    p4_train=[r['metrics'] for r in p3_res[:10]]
    print(f"  {len(p4_params)} combos × 7yrs")
    states4=[BState(p) for p in p4_params]
    run_all_years(conn, states4, full_yrs)
    p4_res=[]
    for i,st in enumerate(states4):
        m=st.metrics()
        if m:
            p4_res.append({'params':p4_params[i],'metrics':m,'train_metrics':p4_train[i]})
            print(f"  #{i+1} CAGR={m['cagr']:+.1f}% MDD={m['mdd']:.1f}% "
                  f"Calmar={m['calmar']:.2f} T={m['trades']} WR={m['win_rate']:.1f}%")
    p4_res.sort(key=lambda r:r['metrics']['calmar'],reverse=True)
    ptop(p4_res,10,"Phase4 Full Top10")
    conn.close()

    # Save
    out={'top_results':[]}
    for i,r in enumerate(p4_res[:5]):
        out['top_results'].append({'rank':i+1,'params':r['params'],
            'train':r.get('train_metrics',{}),'full':r['metrics']})
    with open(RESULTS_OUTPUT,'w',encoding='utf-8') as f:
        json.dump(out,f,ensure_ascii=False,indent=2)
    print(f"\n[OUTPUT] {RESULTS_OUTPUT}")
    print(f"\n{'='*85}")
    print(f"  Total: {(time.time()-T0)/60:.1f} min")
    print(f"{'='*85}")


if __name__ == '__main__':
    main()
