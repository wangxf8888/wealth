#!/usr/bin/env python3
"""verify_daily_kline.py - 日K入库质量抽验(只读) [Task #125正式化]

源自t125_quality_check_tmp.py(Diana, 2026-08-05降级补数事故),
参数化日期后转正: BaoStock封禁期间腾讯降级入库日必跑一次。
① 行数统计+板块分布(对照前一交易日) ② 抽样10只字段一致性+preclose连续性
③ 封板股 high=涨停价精确性(主板10% 创科20% 北交30% ST5%, 四舍五入到分)

用法: python3 tools/verify_daily_kline.py 2026-08-05
      (省略日期=库中最新交易日)
退出码: 0=全部PASS; 1=存在FAIL(供daily_update.sh降级分支调用后告警)
"""
import random
import sqlite3
import sys

DB = 'file:/home/AIWealth/data/stocks.db?mode=ro'


def main():
    conn = sqlite3.connect(DB, uri=True)
    conn.row_factory = sqlite3.Row
    if len(sys.argv) > 1:
        target = sys.argv[1]
    else:
        target = conn.execute(
            "SELECT MAX(date) FROM stock_kline").fetchone()[0]
    prev = conn.execute(
        "SELECT MAX(date) FROM stock_kline WHERE date < ?",
        (target,)).fetchone()[0]
    if not prev:
        print(f"[ERROR] {target} 无前一交易日可对照")
        return 1
    fails = []

    # ① 行数统计
    n_t = conn.execute("SELECT COUNT(*) FROM stock_kline WHERE date=?",
                       (target,)).fetchone()[0]
    n_p = conn.execute("SELECT COUNT(*) FROM stock_kline WHERE date=?",
                       (prev,)).fetchone()[0]
    row_ok = n_t >= min(5100, int(n_p * 0.98))
    print(f"[行数] {target}: {n_t}行 (对照 {prev}: {n_p}行) "
          f"{'PASS' if row_ok else 'FAIL(显著偏少须查原因)'}")
    if not row_ok:
        fails.append('行数偏少')
    for pat, label in (('sh.60%', '沪主板'), ('sz.00%', '深主板'),
                       ('sz.30%', '创业板'), ('sh.68%', '科创板'),
                       ('bj.%', '北交所')):
        c = conn.execute(
            "SELECT COUNT(*) FROM stock_kline WHERE date=? AND code LIKE ?",
            (target, pat)).fetchone()[0]
        print(f"  {label}: {c}只")

    # ② 抽样10只: 强制含大盘股+各板块
    random.seed(125)
    fixed = ['sh.600519', 'sh.601398', 'sz.000001', 'sz.300750', 'sh.688981']
    pool = [r[0] for r in conn.execute(
        "SELECT code FROM stock_kline WHERE date=?", (target,))]
    sample = [c for c in fixed if c in pool]
    sample += random.sample(
        [c for c in pool if c not in sample], max(0, 10 - len(sample)))
    print(f"\n[抽样10只] close/rate一致性 + preclose连续性({prev}close)")
    all_ok = True
    for code in sample:
        r = conn.execute(
            "SELECT * FROM stock_kline WHERE date=? AND code=?",
            (target, code)).fetchone()
        p = conn.execute(
            "SELECT close FROM stock_kline WHERE date=? AND code=?",
            (prev, code)).fetchone()
        calc = round((r['close'] - r['preclose']) / r['preclose'] * 100, 2)
        rate_ok = abs(calc - (r['close_rate'] or 0)) <= 0.01
        prev_close = p[0] if p else None
        # 除权除息日preclose!=前日close属正常, 标记后人工看
        cont = '=' if (prev_close and
                       abs(prev_close - r['preclose']) < 0.005) else '除权?'
        ok = rate_ok and r['open'] and \
            r['high'] >= max(r['open'], r['close']) and \
            r['low'] <= min(r['open'], r['close']) and (r['volume'] or 0) > 0
        all_ok &= ok
        print(f"  {code:<10} {(r['code_name'] or '')[:6]:<8} "
              f"close={r['close']:>8.2f} rate={r['close_rate']:>6.2f} "
              f"复算={calc:>6.2f} preclose={r['preclose']:>8.2f} "
              f"{cont} {'OK' if ok else 'BAD'}")
    print(f"[抽样] 判定: {'PASS' if all_ok else 'FAIL'}")
    if not all_ok:
        fails.append('抽样字段异常')

    # amount/turn覆盖 + isST漂移
    na = conn.execute(
        "SELECT COUNT(*) FROM stock_kline WHERE date=? "
        "AND (amount IS NULL OR turn IS NULL)", (target,)).fetchone()[0]
    print(f"\n[字段覆盖] amount/turn为NULL: {na}只 ({na/max(n_t,1)*100:.1f}%)")
    st_chg = conn.execute(
        "SELECT COUNT(*) FROM stock_kline a JOIN stock_kline b "
        "ON a.code=b.code AND a.date=? AND b.date=? "
        "WHERE a.isST != b.isST", (target, prev)).fetchone()[0]
    print(f"[isST] 与{prev}不一致: {st_chg}只 (应≈0)")

    # ③ 封板股涨停价精确性: 主板+创科各验若干
    def limit_price(code, name, preclose):
        if code.startswith(('sz.30', 'sh.68')):
            pct = 0.20
        elif code.startswith('bj.'):
            pct = 0.30
        elif 'ST' in (name or '').upper():
            pct = 0.05
        else:
            pct = 0.10
        return round(preclose * (1 + pct) + 1e-9, 2)

    print("\n[涨停验证] 封板样本(close=理论涨停价):")
    rows = conn.execute(
        "SELECT code, code_name, preclose, high, close FROM stock_kline "
        "WHERE date=? AND close_rate >= 4.9 ORDER BY close_rate DESC "
        "LIMIT 200", (target,)).fetchall()
    checked, ok_cnt, main_seen, gem_seen = 0, 0, 0, 0
    for r in rows:
        lp = limit_price(r['code'], r['code_name'], r['preclose'])
        if abs(r['close'] - lp) >= 0.005:
            continue                        # 非封板股
        is_gem = r['code'].startswith(('sz.30', 'sh.68'))
        if is_gem:
            if gem_seen >= 2:
                continue
            gem_seen += 1
        else:
            if main_seen >= 2:
                continue
            main_seen += 1
        checked += 1
        hi_ok = abs(r['high'] - lp) < 0.005
        ok_cnt += hi_ok
        print(f"  {r['code']} {r['code_name']} preclose={r['preclose']} "
              f"理论涨停={lp} high={r['high']} {'OK' if hi_ok else 'BAD'}")
        if checked >= 4:
            break
    lim_ok = checked >= 2 and ok_cnt == checked
    print(f"[涨停] 验证{checked}只(主板{main_seen}/创科{gem_seen}), "
          f"通过{ok_cnt}只: {'PASS' if lim_ok else 'FAIL/样本不足'}")
    if not lim_ok:
        fails.append('涨停价异常或样本不足')

    conn.close()
    print(f"\n[总判定] {'PASS' if not fails else 'FAIL: ' + '; '.join(fails)}")
    return 0 if not fails else 1


if __name__ == '__main__':
    sys.exit(main())
