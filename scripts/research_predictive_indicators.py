#!/usr/bin/env python3
"""
研究脚本：识别A股"阶段性拉升"的可靠前兆信号
数据源: /home/AIWealth/data/stocks.db (stock_kline表)
方法: 对16+候选信号统计条件概率，与基线对比
"""
import sqlite3
import time
from collections import defaultdict

DB_PATH = '/home/AIWealth/data/stocks.db'
YEAR_START = 2023
YEAR_END = 2025
RALLY_THRESHOLDS = [5, 8, 10]
FUTURE_DAYS = [1, 2, 3, 5]
MIN_SAMPLES = 500
MIN_LIFT = 2.0

def log(msg):
    print(msg, flush=True)

def main():
    start_time = time.time()
    log(f"{'='*80}")
    log(f"A股预测性指标研究 - 分析年份: {YEAR_START}-{YEAR_END}")
    log(f"{'='*80}")

    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()

    # 获取非ST股票代码
    cur.execute("""
        SELECT DISTINCT code FROM stock_kline
        WHERE date >= ? AND date <= ? AND isST = 0
    """, (f'{YEAR_START}-01-01', f'{YEAR_END}-12-31'))
    codes = [r[0] for r in cur.fetchall()]
    log(f"股票数量: {len(codes)}")

    global_signals = defaultdict(list)
    global_baseline = []
    processed = 0

    for idx, code in enumerate(codes):
        if (idx + 1) % 500 == 0:
            elapsed = time.time() - start_time
            log(f"  进度: {idx+1}/{len(codes)} ({elapsed:.1f}s)")

        cur.execute("""
            SELECT date, preclose, open, open_rate, high, low, close, close_rate,
                   volume, turn, hour3_close, hour4_close, isST
            FROM stock_kline WHERE code = ? AND date >= ? AND date <= ?
            ORDER BY date ASC
        """, (code, f'{YEAR_START-1}-11-01', f'{YEAR_END}-12-31'))
        rows = cur.fetchall()
        if len(rows) < 30:
            continue

        n = len(rows)
        dates = [r[0] for r in rows]
        preclose_a = [r[1] for r in rows]
        opens_a = [r[2] for r in rows]
        open_rate_a = [r[3] for r in rows]
        highs_a = [r[4] for r in rows]
        lows_a = [r[5] for r in rows]
        closes_a = [r[6] for r in rows]
        close_rate_a = [r[7] for r in rows]
        volumes_a = [r[8] for r in rows]
        turns_a = [r[9] for r in rows]
        h3c = [r[10] for r in rows]
        h4c = [r[11] for r in rows]
        ist = [r[12] for r in rows]

        # 确定分析起点
        start_i = 0
        for i in range(n):
            if dates[i] >= f'{YEAR_START}-01-01':
                start_i = i
                break
        start_i = max(start_i, 30)

        # MA5, MA10
        ma5 = [None]*n
        ma10 = [None]*n
        for i in range(4, n):
            vals = [closes_a[j] for j in range(i-4,i+1) if closes_a[j] is not None]
            if len(vals)==5: ma5[i] = sum(vals)/5
        for i in range(9, n):
            vals = [closes_a[j] for j in range(i-9,i+1) if closes_a[j] is not None]
            if len(vals)==10: ma10[i] = sum(vals)/10

        # RSI14
        rsi14 = [None]*n
        if n >= 15:
            ag = al = 0
            for i in range(1, 15):
                if closes_a[i] and closes_a[i-1]:
                    d = closes_a[i] - closes_a[i-1]
                    if d > 0: ag += d
                    else: al -= d
            ag /= 14; al /= 14
            rsi14[14] = 100.0 if al==0 else 100-100/(1+ag/al)
            for i in range(15, n):
                if closes_a[i] and closes_a[i-1]:
                    d = closes_a[i]-closes_a[i-1]
                    g = d if d>0 else 0
                    l = -d if d<0 else 0
                    ag = (ag*13+g)/14; al = (al*13+l)/14
                    rsi14[i] = 100.0 if al==0 else 100-100/(1+ag/al)

        max_fd = max(FUTURE_DAYS)
        for i in range(start_i, n - max_fd):
            if ist[i]: continue
            if not closes_a[i] or closes_a[i] <= 0: continue

            # 未来收益
            mx = 0; fmg = {}; fhr = {}; ok = True
            for d in range(1, max_fd+1):
                if i+d >= n: ok=False; break
                if highs_a[i+d] is not None:
                    mx = max(mx, highs_a[i+d])
                if d in FUTURE_DAYS:
                    fmg[d] = (mx/closes_a[i]-1)*100
                    fhr[d] = ((closes_a[i+d]/closes_a[i]-1)*100) if (closes_a[i+d] and closes_a[i+d]>0) else None
            if not ok or not fmg: continue

            outcome = (fmg, fhr)
            global_baseline.append(outcome)

            # === 信号检测 ===
            # 1. 换手率突增
            if turns_a[i] and turns_a[i]>0:
                pt = [turns_a[j] for j in range(max(0,i-5),i) if turns_a[j] and turns_a[j]>0]
                if len(pt)>=3:
                    at = sum(pt)/len(pt)
                    if at>0:
                        r = turns_a[i]/at
                        if r>=3.0: global_signals['换手率突增3x'].append(outcome)
                        if r>=2.5: global_signals['换手率突增2.5x'].append(outcome)
                        if r>=2.0: global_signals['换手率突增2x'].append(outcome)

            # 2. 成交量突增
            if volumes_a[i] and volumes_a[i]>0:
                pv = [volumes_a[j] for j in range(max(0,i-5),i) if volumes_a[j] and volumes_a[j]>0]
                if len(pv)>=3:
                    av = sum(pv)/len(pv)
                    if av>0:
                        r = volumes_a[i]/av
                        if r>=3.0: global_signals['成交量突增3x'].append(outcome)
                        if r>=2.5: global_signals['成交量突增2.5x'].append(outcome)
                        if r>=2.0: global_signals['成交量突增2x'].append(outcome)

            # 3. 缩量企稳后放量
            if i>=5 and turns_a[i] and turns_a[i]>0:
                sc = 0
                for j in range(i-4,i):
                    if j>=1 and turns_a[j] and turns_a[j-1] and turns_a[j]<=turns_a[j-1]:
                        if close_rate_a[j] is not None and close_rate_a[j]>-1.0: sc+=1
                if sc>=3:
                    pt2 = [turns_a[j] for j in range(i-4,i) if turns_a[j] and turns_a[j]>0]
                    if pt2:
                        apt = sum(pt2)/len(pt2)
                        if apt>0 and turns_a[i]/apt>=1.5:
                            global_signals['缩量企稳后放量'].append(outcome)

            # 4. 量价背离
            if i>=20 and lows_a[i] is not None and volumes_a[i]:
                rl = [lows_a[j] for j in range(i-20,i) if lows_a[j] is not None]
                if rl and lows_a[i]<=min(rl):
                    pv5 = [volumes_a[j] for j in range(i-5,i) if volumes_a[j] and volumes_a[j]>0]
                    if pv5 and volumes_a[i]<sum(pv5)/len(pv5)*0.7:
                        global_signals['量价背离底部缩量'].append(outcome)

            # 5. 站上MA5
            if ma5[i] and i>=1 and ma5[i-1] and closes_a[i-1] is not None:
                if closes_a[i-1]<ma5[i-1] and closes_a[i]>=ma5[i]:
                    global_signals['站上MA5'].append(outcome)

            # 6. MA5金叉MA10
            if ma5[i] and ma10[i] and i>=1 and ma5[i-1] and ma10[i-1]:
                if ma5[i-1]<ma10[i-1] and ma5[i]>=ma10[i]:
                    global_signals['MA5金叉MA10'].append(outcome)

            # 7. 突破新高
            if i>=20 and closes_a[i]:
                rh = [closes_a[j] for j in range(i-20,i) if closes_a[j] is not None]
                if rh and closes_a[i]>max(rh):
                    global_signals['突破20日新高'].append(outcome)
            if i>=30 and closes_a[i]:
                rh = [closes_a[j] for j in range(i-30,i) if closes_a[j] is not None]
                if rh and closes_a[i]>max(rh):
                    global_signals['突破30日新高'].append(outcome)

            # 8. 连续下跌后首阳
            if close_rate_a[i] is not None and close_rate_a[i]>0:
                cum=0
                for j in range(i-1, max(i-11,-1), -1):
                    if j<0: break
                    if close_rate_a[j] is not None and close_rate_a[j]<0: cum+=close_rate_a[j]
                    else: break
                if cum<=-5: global_signals['累跌5%后首阳'].append(outcome)
                if cum<=-8: global_signals['累跌8%后首阳'].append(outcome)
                if cum<=-10: global_signals['累跌10%后首阳'].append(outcome)

            # 9. 大阴后高开
            if i>=1 and close_rate_a[i-1] is not None and close_rate_a[i-1]<=-5:
                if open_rate_a[i] is not None and open_rate_a[i]>=2:
                    global_signals['大阴后高开2%'].append(outcome)
                if open_rate_a[i] is not None and open_rate_a[i]>=3:
                    global_signals['大阴后高开3%'].append(outcome)

            # 10. RSI超卖反转
            if rsi14[i] is not None and i>=1 and rsi14[i-1] is not None:
                if rsi14[i-1]<30 and rsi14[i]>=30:
                    global_signals['RSI超卖反转'].append(outcome)

            # 11. 缩量下跌后放量上涨
            if close_rate_a[i] is not None and close_rate_a[i]>0 and volumes_a[i]:
                sd=0
                for j in range(i-1, max(i-6,-1), -1):
                    if j<1: break
                    if (close_rate_a[j] is not None and close_rate_a[j]<0 and
                        volumes_a[j] and volumes_a[j-1] and volumes_a[j-1]>0 and
                        volumes_a[j]<volumes_a[j-1]): sd+=1
                    else: break
                if sd>=3 and volumes_a[i-1] and volumes_a[i-1]>0:
                    if volumes_a[i]>volumes_a[i-1]*1.3:
                        global_signals['缩量下跌后放量上涨'].append(outcome)

            # 12. 振幅扩大收阳
            if (closes_a[i] and opens_a[i] and highs_a[i] and lows_a[i]
                and closes_a[i]>opens_a[i] and closes_a[i]>0):
                amp = (highs_a[i]-lows_a[i])/closes_a[i]*100
                pa=[]
                for j in range(max(0,i-5),i):
                    if highs_a[j] and lows_a[j] and closes_a[j] and closes_a[j]>0:
                        pa.append((highs_a[j]-lows_a[j])/closes_a[j]*100)
                if len(pa)>=3:
                    aa = sum(pa)/len(pa)
                    if aa>0 and amp>aa*2:
                        global_signals['振幅扩大收阳'].append(outcome)

            # 13. 尾盘拉升
            if h3c[i] and h4c[i] and h3c[i]>0:
                tp = (h4c[i]/h3c[i]-1)*100
                if tp>=2.0: global_signals['尾盘拉升2%'].append(outcome)
                if tp>=1.5: global_signals['尾盘拉升1.5%'].append(outcome)

            # 14. 涨停次日
            if i>=1 and closes_a[i-1] and preclose_a[i-1] and preclose_a[i-1]>0:
                ratio = round(closes_a[i-1]/preclose_a[i-1], 2)
                if ratio>=1.10: global_signals['涨停次日'].append(outcome)

            # 15. 炸板次日
            if (i>=1 and highs_a[i-1] and preclose_a[i-1] and preclose_a[i-1]>0
                and closes_a[i-1]):
                lp = preclose_a[i-1]*1.10
                if highs_a[i-1]>=lp*0.998 and closes_a[i-1]<lp*0.99:
                    global_signals['炸板次日'].append(outcome)

            # 16. 跌停反包
            if (i>=1 and close_rate_a[i-1] is not None and close_rate_a[i-1]<=-9.5
                and close_rate_a[i] is not None and close_rate_a[i]>0):
                global_signals['跌停反包'].append(outcome)

        processed += 1

    conn.close()
    elapsed = time.time() - start_time
    log(f"\n处理完成: {processed}只股票, 耗时{elapsed:.1f}s")
    log(f"基线样本数: {len(global_baseline)}")
    log(f"信号种类: {len(global_signals)}")

    # === 基线统计 ===
    log(f"\n{'='*80}")
    log(f"基线概率")
    log(f"{'='*80}")
    total_b = len(global_baseline)
    bp = {}; br = {}
    for d in FUTURE_DAYS:
        for thr in RALLY_THRESHOLDS:
            c = sum(1 for (fm,fh) in global_baseline if d in fm and fm[d]>=thr)
            p = c/total_b if total_b>0 else 0
            bp[(d,thr)] = p
            log(f"  P(未来{d}天最高涨>={thr}%) = {p*100:.2f}% ({c}/{total_b})")
        rs = [fh[d] for (fm,fh) in global_baseline if d in fh and fh[d] is not None]
        br[d] = sum(rs)/len(rs) if rs else 0
    log(f"  基线均收益: 1d={br.get(1,0):.3f}%, 3d={br.get(3,0):.3f}%, 5d={br.get(5,0):.3f}%")

    # === 信号统计 ===
    log(f"\n{'='*80}")
    log(f"各信号详细统计")
    log(f"{'='*80}")

    summary = []
    for sn, ocs in sorted(global_signals.items()):
        samp = len(ocs)
        if samp < 100: continue
        log(f"\n--- {sn} (n={samp}) ---")
        for d in FUTURE_DAYS:
            for thr in RALLY_THRESHOLDS:
                c = sum(1 for (fm,fh) in ocs if d in fm and fm[d]>=thr)
                p = c/samp
                b = bp.get((d,thr),0)
                li = p/b if b>0 else 0
                log(f"  P({d}天涨{thr}%)={p*100:.2f}% base={b*100:.2f}% lift={li:.2f}x")
        for d in FUTURE_DAYS:
            rs = [fh[d] for (fm,fh) in ocs if d in fh and fh[d] is not None]
            ar = sum(rs)/len(rs) if rs else 0
            log(f"  持有{d}天均收益={ar:.3f}% (base={br.get(d,0):.3f}%)")

        # 关键指标
        p35 = sum(1 for (fm,fh) in ocs if 3 in fm and fm[3]>=5)/samp
        l35 = p35/bp.get((3,5),0.001)
        p58 = sum(1 for (fm,fh) in ocs if 5 in fm and fm[5]>=8)/samp
        l58 = p58/bp.get((5,8),0.001)
        r5 = [fh[5] for (fm,fh) in ocs if 5 in fh and fh[5] is not None]
        ar5 = sum(r5)/len(r5) if r5 else 0
        summary.append((sn, samp, p35, l35, p58, l58, ar5))

    # === 排名 ===
    log(f"\n{'='*80}")
    log(f"最终排名（按3天涨5%提升倍数降序）")
    log(f"{'='*80}")
    summary.sort(key=lambda x: x[3], reverse=True)

    log(f"\n{'信号':<20} {'样本':>7} {'P(3d5%)':>8} {'倍数':>6} {'P(5d8%)':>8} {'倍数':>6} {'5d收益':>8}")
    log("-"*70)

    qualified = []
    for (nm,sa,p35,l35,p58,l58,ar5) in summary:
        mk = "★" if sa>=MIN_SAMPLES and (l35>=MIN_LIFT or l58>=MIN_LIFT) else " "
        if sa>=MIN_SAMPLES and (l35>=MIN_LIFT or l58>=MIN_LIFT):
            qualified.append((nm,sa,p35,l35,p58,l58,ar5))
        log(f"{mk}{nm:<19} {sa:>7} {p35*100:>7.2f}% {l35:>5.2f}x {p58*100:>7.2f}% {l58:>5.2f}x {ar5:>7.3f}%")

    log(f"\n{'='*80}")
    log(f"★ 合格指标（提升>=2x, 样本>=500）: {len(qualified)}个")
    log(f"{'='*80}")
    for i,(nm,sa,p35,l35,p58,l58,ar5) in enumerate(qualified,1):
        log(f"\n  {i}. {nm}")
        log(f"     样本量: {sa}")
        log(f"     P(3天涨5%): {p35*100:.2f}% (基线{bp.get((3,5),0)*100:.2f}%, 提升{l35:.2f}x)")
        log(f"     P(5天涨8%): {p58*100:.2f}% (基线{bp.get((5,8),0)*100:.2f}%, 提升{l58:.2f}x)")
        log(f"     持有5天均收益: {ar5:.3f}% (基线{br.get(5,0):.3f}%)")

    log(f"\n总耗时: {time.time()-start_time:.1f}s")

if __name__ == '__main__':
    main()
