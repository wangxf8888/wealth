#!/usr/bin/env python3
"""策略调研#1: ZhaBan + 固定持有 纯alpha验证"""
import sqlite3, math, time
from collections import defaultdict
from datetime import datetime, timedelta

DB = "/home/AIWealth/data/stocks.db"
CAP = 1_000_000.0
NS = 3
SLIP = 0.003

def sv(v):
    if v is None: return None
    try:
        f = float(v)
        if math.isnan(f) or f == 0: return None
        return f
    except: return None

def gem(c): return c.startswith("sz.30") or c.startswith("sh.688")

def vh(row, h):
    for k in ('open','close','high','low'):
        if sv(row.get(f'hour{h}_{k}')) is None: return False
    return True

def load(conn, st, ed):
    cur = conn.cursor()
    lo = (datetime.strptime(st,"%Y-%m-%d")-timedelta(days=180)).strftime("%Y-%m-%d")
    hi = (datetime.strptime(ed,"%Y-%m-%d")+timedelta(days=10)).strftime("%Y-%m-%d")
    cur.execute("SELECT DISTINCT date FROM stock_kline WHERE date>=? AND date<=? ORDER BY date",(lo,hi))
    ad = [r[0] for r in cur.fetchall()]
    di = {d:i for i,d in enumerate(ad)}
    bt = [d for d in ad if st<=d<=ed]
    flds = "date,code,code_name,preclose,open,high,low,close,amount,turn,open_rate,"+\
           "hour1_open,hour1_high,hour1_low,hour1_close,"+\
           "hour2_open,hour2_high,hour2_low,hour2_close,"+\
           "hour3_open,hour3_high,hour3_low,hour3_close,"+\
           "hour4_open,hour4_high,hour4_low,hour4_close"
    cur.execute(f"SELECT {flds} FROM stock_kline WHERE date>=? AND date<=? AND code NOT LIKE 'bj.%' AND preclose>0",(lo,hi))
    ks = flds.split(",")
    bc = defaultdict(dict)
    for row in cur.fetchall():
        d,c = row[0],row[1]
        r = {ks[i]:row[i] for i in range(len(ks))}
        bc[c][d] = r
    return ad, di, bt, bc

def cands(bc, sd, di, tgt):
    bi = di.get(tgt)
    if bi is None or bi<1: return []
    prev = sd[bi-1]
    out = []
    for code, dm in bc.items():
        pr = dm.get(prev); tr = dm.get(tgt)
        if not pr or not tr: continue
        pc = sv(pr.get('preclose')); hi = sv(pr.get('high')); cl = sv(pr.get('close'))
        if not all([pc,hi,cl]): continue
        if hi < pc*1.098: continue
        if cl >= hi*0.99: continue
        am = sv(pr.get('amount'))
        if not am or am<1e8: continue
        ad = sv(tr.get('amount')); td = sv(tr.get('turn'))
        if not ad or not td or td<=0: continue
        mv = ad/td*100.0
        if mv<30e8 or mv>100e8: continue
        opr = sv(tr.get('open_rate'))
        if opr is None or opr<-3.0 or opr>5.0: continue
        if gem(code) and opr>=19.5: continue
        if not gem(code) and opr>=9.5: continue
        if not vh(tr,1): continue
        pc_t = sv(tr.get('preclose'))
        h1c = sv(tr.get('hour1_close')); h1h = sv(tr.get('hour1_high'))
        if pc_t and h1c and h1h:
            lim = pc_t*(1.198 if gem(code) else 1.098)
            if h1c>=lim-0.01 and abs(h1h-h1c)<0.01: continue
        out.append({'code':code,'name':pr.get('code_name',''),'amount':am})
    out.sort(key=lambda x:-x['amount'])
    return out

def run(bc, sd, di, bt, hold):
    cash = CAP; pos = []; trades = []; eq = []
    for today in bt:
        if today!=bt[-1] and len(pos)<NS and cash>0:
            held = {p['code'] for p in pos}
            for c in cands(bc,sd,di,today):
                if len(pos)>=NS: break
                if c['code'] in held: continue
                row = bc.get(c['code'],{}).get(today)
                if not row: continue
                h1o = sv(row.get('hour1_open'))
                if not h1o: continue
                bp = h1o*(1+SLIP)
                fr = NS-len(pos); sm = cash/max(1,fr)
                if sm<bp*100: continue
                sh = int(sm/bp//100)*100
                if sh<=0: continue; cost = sh*bp
                if cost>cash: continue
                cash-=cost
                pos.append({'code':c['code'],'bp':bp,'bd':today,'sh':sh})
                held.add(c['code'])
        surv = []
        for p in pos:
            if p['bd']==today:
                surv.append(p); continue
            bi_ = di.get(p['bd']); ti = di.get(today)
            off = ti-bi_ if bi_ is not None and ti is not None else -1
            if off<=0:
                surv.append(p); continue
            if off>=hold:
                row = bc.get(p['code'],{}).get(today)
                sp = sv(row.get('hour4_close')) or sv(row.get('close')) or p['bp'] if row else p['bp']
                asp = sp*(1-SLIP)
                ret = (asp/p['bp']-1)*100
                proc = p['sh']*asp; cash+=proc
                trades.append({'bd':p['bd'],'sd':today,'code':p['code'],
                              'ret':ret,'pnl':proc-p['sh']*p['bp']})
            else:
                surv.append(p)
        pos = surv
        eqv = cash
        for p in pos:
            row = bc.get(p['code'],{}).get(today)
            pr = sv(row.get('hour4_close')) or sv(row.get('close')) or p['bp'] if row else p['bp']
            eqv+=p['sh']*pr
        eq.append((today,eqv))
    if bt and pos:
        last = bt[-1]
        for p in pos:
            row = bc.get(p['code'],{}).get(last)
            sp = sv(row.get('hour4_close')) or sv(row.get('close')) or p['bp'] if row else p['bp']
            asp = sp*(1-SLIP)
            ret = (asp/p['bp']-1)*100
            proc = p['sh']*asp; cash+=proc
            trades.append({'bd':p['bd'],'sd':last,'code':p['code'],
                          'ret':ret,'pnl':proc-p['sh']*p['bp']})
    return trades, eq

def stat(trades, eq, cap):
    fe = eq[-1][1] if eq else cap
    n = len(trades)
    if eq and len(eq)>1:
        yrs = max(0.05,(datetime.strptime(eq[-1][0],"%Y-%m-%d")-datetime.strptime(eq[0][0],"%Y-%m-%d")).days/365.25)
    else: yrs=1
    cagr = ((fe/cap)**(1/yrs)-1)*100 if fe>0 else -100
    wins = sum(1 for t in trades if t['ret']>0)
    wr = wins/n*100 if n else 0
    ar = sum(t['ret'] for t in trades)/n if n else 0
    pk = eq[0][1]; mdd=0
    for _,v in eq:
        if v>pk: pk=v
        dd = (pk-v)/pk*100 if pk>0 else 0
        if dd>mdd: mdd=dd
    cal = cagr/mdd if mdd>1e-6 else 0
    return n,wr,ar,cagr,mdd,cal,fe

def byyr(trades, eq, cap):
    by_y = defaultdict(list)
    for t in trades: by_y[t['sd'][:4]].append(t)
    eq_y = {}
    for d,v in eq:
        y = d[:4]; eq_y[y]=v
    pe = cap; out = []
    for y in sorted(by_y):
        ts = by_y[y]; n=len(ts); w=sum(1 for t in ts if t['ret']>0)
        ee = eq_y.get(y,pe)
        ret = (ee/pe-1)*100 if pe>0 else 0
        yr_eq = [(d,v) for d,v in eq if d[:4]==y]
        pk2 = yr_eq[0][1] if yr_eq else pe; ym=0
        for _,v in yr_eq:
            if v>pk2: pk2=v
            dd = (pk2-v)/pk2*100 if pk2>0 else 0
            if dd>ym: ym=dd
        out.append((y,n,w/n*100 if n else 0,ret,ym,ee))
        pe = ee
    return out

st,ed = '2020-01-01','2025-12-31'
print("="*60)
print("调研#1: ZhaBan+固定持有 纯alpha验证")
print("="*60)
conn = sqlite3.connect(DB)
t0 = time.time()
ad, di, bt, bc = load(conn, st, ed)
conn.close()
print(f"加载: {time.time()-t0:.1f}s | {len(ad)}天 {len(bc)}股 {len(bt)}回测日\n")
for hold in (1,2,3):
    t1 = time.time()
    trades, eq = run(bc, ad, di, bt, hold)
    n,wr,ar,cagr,mdd,cal,fe = stat(trades, eq, CAP)
    yr = byyr(trades, eq, CAP)
    print(f"[持{hold}天] CAGR={cagr:+7.1f}% MDD={mdd:5.1f}% WR={wr:5.1f}% "
          f"T={n:4d} Avg={ar:+6.2f}% Calmar={cal:+6.2f} 终值={fe:,.0f}")
    print(f"        分年: ",end="")
    for y,n_,wr_,ret_,mdd_,ee_ in yr:
        print(f"{y}:{ret_:+5.0f}%(WR{wr_:.0f}%/{n_}) ",end="")
    print(f"| {time.time()-t1:.1f}s")
print(f"\n总耗时: {time.time()-t0:.1f}s")
