#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
龙抬头第二波 - 入场点对比探针
对比 A股7+连板炸板回调后的两种入场:
  高开追入(已验证亏损) vs 低吸(缩量企稳后首个放量阳线, 次日T+1 hour1_open买入)
复用 stocks.db, 结论写日志.
"""
import sqlite3
from collections import defaultdict

DB = '/home/AIWealth/data/stocks.db'
LOG = '/home/AIWealth/scripts/logs/dragon_second_wave_r2.log'
BOARD_MIN = 7
MAX_TRACK = 30
DEEP = 20.0        # 回调至少 20%
FUTURE = 10
CONFIGS = [
    ("TP8_SL5_H3", 8, -5, 3), ("TP10_SL6_H3", 10, -6, 3),
    ("TP15_SL8_H5", 15, -8, 5), ("TP20_SL10_H5", 20, -10, 5),
    ("TP30_SL12_H8", 30, -12, 8), ("TP50_SL15_H10", 50, -15, 10),
    ("HOLD_H2", None, None, 2), ("HOLD_H3", None, None, 3),
    ("HOLD_H5", None, None, 5),
]


def ratio(code):
    if code.startswith(('sz.300','sz.301')) or code.startswith('sh.688'):
        return 0.20
    if code.startswith('bj.'):
        return 0.30
    return 0.10


def limit_up(c, p, code):
    return c is not None and p and p > 0 and c >= round(p*(1+ratio(code)),2)-0.001


def is_st(nm):
    return bool(nm) and 'ST' in nm.upper()


conn = sqlite3.connect(DB)
cur = conn.cursor()
cur.execute("SELECT DISTINCT date FROM stock_kline ORDER BY date")
all_days = [r[0] for r in cur.fetchall()]

cur.execute("""SELECT code,code_name,date,open,high,low,close,preclose,volume,
               hour1_open FROM stock_kline ORDER BY code,date""")
daily = defaultdict(list)
for r in cur.fetchall():
    daily[r[0]].append({'name':r[1],'date':r[2],'open':r[3],'high':r[4],'low':r[5],
                         'close':r[6],'preclose':r[7],'volume':r[8],'h1o':r[9]})

# 找 7+ 连板事件
events = []
for code, rows in daily.items():
    n=len(rows); i=0
    while i<n:
        if limit_up(rows[i]['close'],rows[i]['preclose'],code) and not is_st(rows[i]['name']):
            j=i
            while j+1<n and not is_st(rows[j+1]['name']) and limit_up(rows[j+1]['close'],rows[j+1]['preclose'],code):
                j+=1
            if j-i+1>=BOARD_MIN and '2021-01-01'<=rows[j]['date']<='2026-12-31':
                events.append((code,j,rows[j]['close'],rows[j]['date'],j-i+1,rows[j]['name']))
            i=j+1
        else:
            i+=1


def hour_path(code, buy_date):
    try: idx=all_days.index(buy_date)
    except ValueError: return []
    fdays=all_days[idx+1:idx+1+FUTURE]
    if not fdays: return []
    ph=','.join(['?']*len(fdays))
    cur.execute(f"""SELECT date,hour1_high,hour1_low,hour1_close,hour2_high,hour2_low,hour2_close,
        hour3_high,hour3_low,hour3_close,hour4_high,hour4_low,hour4_close
        FROM stock_kline WHERE code=? AND date IN ({ph}) ORDER BY date""",[code]+fdays)
    path=[]
    for row in cur.fetchall():
        for hh,hl,hc in [(row[1],row[2],row[3]),(row[4],row[5],row[6]),(row[7],row[8],row[9]),(row[10],row[11],row[12])]:
            if hh is None or hl is None or hc is None: continue
            path.append((row[0],hh,hl,hc))
    return path


def sim(bp, path, tp, sl, hold):
    if not path or not bp or bp<=0: return None
    dseq=[]
    for d,_,_,_ in path:
        if d not in dseq: dseq.append(d)
    allow=set(dseq[:hold])
    tpp=bp*(1+tp/100) if tp is not None else None
    slp=bp*(1+sl/100) if sl is not None else None
    lc=None
    for d,hh,hl,hc in path:
        if d not in allow: continue
        lc=hc
        if slp is not None and hl<=slp: return sl
        if tpp is not None and hh>=tpp: return tp
    return None if lc is None else (lc-bp)/bp*100

# 低吸信号: 回调>=DEEP后, 出现"缩量企稳(近3日均量<前5均量)后首个放量阳线(vol>vol_ma5*1.2且close>open且涨>2%)"
# 该阳线为确认日D(用D收盘确认), 次日D+1 hour1_open 买入(T+1合规)
records=[]
for code,end_idx,peak,end_date,bc,name in events:
    rows=daily[code]; n=len(rows)
    low_since=peak
    fired=False
    for k in range(end_idx+3, min(end_idx+1+MAX_TRACK, n-1)):
        d=rows[k]
        pc=rows[k-1]['close']
        if pc is None or pc<=0 or pc>=peak: continue
        low_since=min(low_since, d['close'] if d['close'] else low_since)
        pullback=(peak-pc)/peak*100
        if pullback<DEEP: continue
        # 放量阳线确认
        if d['close'] is None or d['open'] is None or d['open']<=0: continue
        chg=(d['close']-pc)/pc*100
        if not (d['close']>d['open'] and chg>2): continue
        vol5=[rows[x]['volume'] for x in range(k-5,k) if rows[x]['volume'] is not None]
        if len(vol5)<3 or d['volume'] is None: continue
        vma=sum(vol5)/len(vol5)
        if vma<=0 or d['volume']<vma*1.2: continue
        # 次日买入
        if k+1>=n: break
        buy_row=rows[k+1]
        bp=buy_row['h1o'] if (buy_row['h1o'] and buy_row['h1o']>0) else buy_row['open']
        if not bp or bp<=0: break
        path=hour_path(code, buy_row['date'])
        sims={}
        for nm,tp,sl,h in CONFIGS:
            r=sim(bp,path,tp,sl,h)
            if r is not None: sims[nm]=r
        mu=None
        if path:
            hs=[p[1] for p in path]
            if hs: mu=(max(hs)-bp)/bp*100
        records.append({'yr':buy_row['date'][:4],'sims':sims,'pullback':pullback,'max_up':mu})
        fired=True
        break  # 每事件只取首个低吸信号

fh=open(LOG,'a',encoding='utf-8')
def L(t=""):
    print(t); fh.write(t+"\n")

L("\n"+"="*80)
L(f"【入场点对比: 低吸变体】7+连板回调>={DEEP}%后 缩量企稳->首个放量阳线 次日T+1 hour1_open买入")
L("="*80)
L(f"低吸信号样本: {len(records)}")
if records:
    mus=[r['max_up'] for r in records if r['max_up'] is not None]
    if mus:
        for tp in [8,10,15,20,30,50]:
            hit=sum(1 for x in mus if x>=tp)
            L(f"  +{tp:>2}% 触及率: {hit/len(mus)*100:>5.1f}% ({hit}/{len(mus)})")
    L(f"\n  {'配置':<14}|{'样本':>5}|{'均收益':>8}|{'胜率':>7}")
    L(f"  {'-'*14}+{'-'*5}+{'-'*8}+{'-'*7}")
    stats={}
    for nm,tp,sl,h in CONFIGS:
        rs=[r['sims'][nm] for r in records if nm in r['sims']]
        if rs:
            w=sum(1 for x in rs if x>0); m=len(rs); a=sum(rs)/m
            stats[nm]=(a,w/m*100,h,m)
            L(f"  {nm:<14}|{m:>5}|{a:>+7.2f}%|{w/m*100:>6.1f}%")
    if stats:
        best=max(stats,key=lambda k:stats[k][0])
        a,wr,h,m=stats[best]
        L(f"\n  >> 低吸最优: {best} 均收益{a:+.2f}% 胜率{wr:.1f}% 估月化{a*20/h:+.1f}% 样本{m}")
        # 逐年
        by=defaultdict(list)
        for r in records:
            if best in r['sims']: by[r['yr']].append(r['sims'][best])
        L(f"  逐年({best}): "+' '.join(f"{y}:{sum(v)/len(v):+.1f}%/{sum(1 for x in v if x>0)/len(v)*100:.0f}%(n{len(v)})" for y,v in sorted(by.items())))
fh.close(); conn.close()
