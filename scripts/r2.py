#!/usr/bin/env python3
"""调研#2: ZhaBan+固定持有 + SQL流式"""
import sqlite3, math, time
from collections import defaultdict
from datetime import datetime, timedelta

DB = "/home/AIWealth/data/stocks.db"
CAP = 1_000_000.0
NS = 3
SLIP = 0.003

def load_dates(conn, st, ed):
    lo = (datetime.strptime(st,"%Y-%m-%d")-timedelta(days=180)).strftime("%Y-%m-%d")
    hi = (datetime.strptime(ed,"%Y-%m-%d")+timedelta(days=10)).strftime("%Y-%m-%d")
    cur = conn.cursor()
    cur.execute("SELECT DISTINCT date FROM stock_kline WHERE date>=? AND date<=? ORDER BY date",(lo,hi))
    ad = [r[0] for r in cur.fetchall()]
    di = {d:i for i,d in enumerate(ad)}
    bt = [d for d in ad if st<=d<=ed]
    return ad, di, bt

def get_hour(conn, code, dates):
    if not dates: return {}
    ph = ','.join('?'*len(dates))
    cur = conn.cursor()
    cur.execute(f"""
        SELECT date, preclose, hour1_open, hour1_high, hour1_low, hour1_close,
               hour2_open, hour2_high, hour2_low, hour2_close,
               hour3_open, hour3_high, hour3_low, hour3_close,
               hour4_open, hour4_high, hour4_low, hour4_close, close
        FROM stock_kline WHERE code=? AND date IN ({ph})
    """, [code]+list(dates))
    res = {}
    for row in cur:
        d = row[0]
        res[d] = {
            'preclose':row[1],
            'h1o':row[2],'h1h':row[3],'h1l':row[4],'h1c':row[5],
            'h2o':row[6],'h2h':row[7],'h2l':row[8],'h2c':row[9],
            'h3o':row[10],'h3h':row[11],'h3l':row[12],'h3c':row[13],
            'h4o':row[14],'h4h':row[15],'h4l':row[16],'h4c':row[17],
            'close':row[18]
        }
    return res

def get_cands(conn, tgt, prev):
    cur = conn.cursor()
    cur.execute("""
        SELECT p.code, p.code_name, p.amount,
               t.amount, t.turn, t.open_rate,
               t.preclose, t.hour1_open, t.hour1_high, t.hour1_low, t.hour1_close
        FROM stock_kline p
        JOIN stock_kline t ON p.code=t.code AND t.date=?
        WHERE p.date=? AND p.preclose>0
          AND p.high >= p.preclose*1.098
          AND p.close < p.high*0.99
          AND p.amount >= 1e8
          AND p.code NOT LIKE 'bj.%'
    """, (tgt, prev))
    cands = []
    for row in cur:
        code, name, p_amt = row[0], row[1], row[2]
        t_amt, t_turn, t_opr = row[3], row[4], row[5]
        t_pc, t_h1o, t_h1h, t_h1l, t_h1c = row[6], row[7], row[8], row[9], row[10]
        if not t_amt or not t_turn or t_turn<=0: continue
        mv = t_amt/t_turn*100
        if mv<30e8 or mv>100e8: continue
        if t_opr is None or t_opr<-3 or t_opr>5: continue
        is_gem = code.startswith("sz.30") or code.startswith("sh.688")
        if is_gem and t_opr>=19.5: continue
        if not is_gem and t_opr>=9.5: continue
        if not all([t_h1o, t_h1h, t_h1l, t_h1c]) or t_h1o<=0: continue
        if t_pc and t_h1c and t_h1h:
            lim = t_pc*(1.198 if is_gem else 1.098)
            if t_h1c>=lim-0.01 and abs(t_h1h-t_h1c)<0.01: continue
        cands.append({'code':code, 'name':name or '', 'amount':p_amt})
    cands.sort(key=lambda x:-x['amount'])
    return cands

def sf(v):
    if v is None: return None
    try: f=float(v)
    except: return None
    if math.isnan(f) or f==0: return None
    return f

def run(conn, sd, di, bt, hold):
    cash = CAP; pos = []; trades = []; eq = []
    for today in bt:
        # Phase0: buy
        if today!=bt[-1] and len(pos)<NS and cash>0:
            bi = di.get(today)
            prev = sd[bi-1] if bi and bi>0 else None
            if prev:
                cands = get_cands(conn, today, prev)
                held = {p['code'] for p in pos}
                for c in cands:
                    if len(pos)>=NS: break
                    if c['code'] in held: continue
                    row = get_hour(conn, c['code'], [today]).get(today)
                    if not row: continue
                    h1o = sf(row.get('h1o'))
                    if not h1o: continue
                    bp = h1o*(1+SLIP)
                    fr = NS-len(pos)
                    sm = cash/max(1,fr)
                    if sm<bp*100: continue
                    sh = int(sm/bp//100)*100
                    if sh<=0: continue
                    cost = sh*bp
                    if cost>cash: continue
                    cash -= cost
                    pos.append({'code':c['code'],'bp':bp,'bd':today,'sh':sh})
                    held.add(c['code'])
        # Phase1: sell
        surv = []
        for p in pos:
            if p['bd']==today:
                surv.append(p); continue
            bi_ = di.get(p['bd']); ti = di.get(today)
            off = ti-bi_ if bi_ is not None and ti is not None else -1
            if off<=0:
                surv.append(p); continue
            if off>=hold:
                row = get_hour(conn, p['code'], [today]).get(today)
                sp = sf(row.get('h4c')) or sf(row.get('close')) or p['bp'] if row else p['bp']
                asp = sp*(1-SLIP)
                ret = (asp/p['bp']-1)*100
                proc = p['sh']*asp; cash+=proc
                trades.append({'bd':p['bd'],'sd':today,'code':p['code'],'ret':ret,'pnl':proc-p['sh']*p['bp']})
            else:
                surv.append(p)
        pos = surv
        # mark-to-market
        eqv = cash
        for p in pos:
            row = get_hour(conn, p['code'], [today]).get(today)
            pr = sf(row.get('h4c')) or sf(row.get('close')) or p['bp'] if row else p['bp']
            eqv += p['sh']*pr
        eq.append((today,eqv))
    # force close
    if bt and pos:
        last = bt[-1]
        for p in pos:
            row = get_hour(conn, p['code'], [last]).get(last)
            sp = sf(row.get('h4c')) or sf(row.get('close')) or p['bp'] if row else p['bp']
            asp = sp*(1-SLIP); ret=(asp/p['bp']-1)*100
            proc = p['sh']*asp; cash+=proc
            trades.append({'bd':p['bd'],'sd':last,'code':p['code'],'ret':ret,'pnl':proc-p['sh']*p['bp']})
    return trades, eq

def stat(trades, eq, cap):
    fe = eq[-1][1] if eq else cap; n=len(trades)
    if eq and len(eq)>1:
        yrs = max(0.05,(datetime.strptime(eq[-1][0],"%Y-%m-%d")-datetime.strptime(eq[0][0],"%Y-%m-%d")).days/365.25)
    else: yrs=1
    cagr = ((fe/cap)**(1/yrs)-1)*100 if fe>0 else -100
    wins = sum(1 for t in trades if t['ret']>0)
    wr = wins/n*100 if n else 0
    ar = sum(t['ret'] for t in trades)/n if n else 0
    pk=eq[0][1]; mdd=0
    for _,v in eq:
        if v>pk: pk=v
        dd=(pk-v)/pk*100 if pk>0 else 0
        if dd>mdd: mdd=dd
    cal=cagr/mdd if mdd>1e-6 else 0
    return n,wr,ar,cagr,mdd,cal,fe

def byyr(trades, eq, cap):
    by_y=defaultdict(list)
    for t in trades: by_y[t['sd'][:4]].append(t)
    eq_y={}; pe=cap; out=[]
    for d,v in eq: y=d[:4]; eq_y[y]=v
    for y in sorted(by_y):
        ts=by_y[y]; n=len(ts); w=sum(1 for t in ts if t['ret']>0)
        ee=eq_y.get(y,pe); ret=(ee/pe-1)*100 if pe>0 else 0
        yr_eq=[(d,v) for d,v in eq if d[:4]==y]
        pk2=yr_eq[0][1] if yr_eq else pe; ym=0
        for _,v in yr_eq:
            if v>pk2: pk2=v
            dd=(pk2-v)/pk2*100 if pk2>0 else 0
            if dd>ym: ym=dd
        out.append((y,n,w/n*100 if n else 0,ret,ym,ee))
        pe=ee
    return out

st,ed='2020-01-01','2025-12-31'
print("="*60)
print("调研#2: ZhaBan+固定持有 SQL流式")
print("="*60)
conn = sqlite3.connect(DB)
t0=time.time()
ad,di,bt=load_dates(conn,st,ed)
print(f"交易日: {len(ad)}天 回测: {len(bt)}天 | {time.time()-t0:.1f}s\n")
for hold in (1,2,3):
    t1=time.time()
    trades,eq=run(conn,ad,di,bt,hold)
    n,wr,ar,cagr,mdd,cal,fe=stat(trades,eq,CAP)
    yr=byyr(trades,eq,CAP)
    print(f"[持{hold}天] CAGR={cagr:+7.1f}% MDD={mdd:5.1f}% WR={wr:5.1f}% T={n:4d} Avg={ar:+6.2f}% Calmar={cal:+6.2f} 终值={fe:,.0f}")
    print(f"        分年: ",end="")
    for y,n_,wr_,ret_,mdd_,ee_ in yr:
        print(f"{y}:{ret_:+5.0f}%(WR{wr_:.0f}%/{n_}) ",end="")
    print(f"| {time.time()-t1:.1f}s")
conn.close()
print(f"\n总耗时: {time.time()-t0:.1f}s")
