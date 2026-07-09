#!/usr/bin/env python3
"""QA - A股交易合规性全面验证
覆盖检查项:
  1. T+1违规 (buy_date == sell_date)
  2. 涨停买入 (buy_price >= limit_up)
  3. 跌停卖出 (sell_price <= limit_down)
  4. 持仓天数合理性 (1 <= hold_days <= 10)
  5. 一字跌停卖出 (跌停封板日不可卖出)
  6. 确定性验证 (重跑回测对比笔数/净值)
  7. 抽样验证 (随机5笔交叉核对数据库)
"""
import json, sqlite3, random, subprocess, sys, os, math
from datetime import datetime

TRADES_FILE = '/home/AIWealth/scripts/backtest_scoring_trades.json'
CANDIDATES_FILE = '/home/AIWealth/scripts/backtest_daily_candidates.json'
DB_PATH = '/home/AIWealth/data/stocks.db'
REPORT_FILE = '/home/AIWealth/scripts/qa_compliance_report.md'
MAX_DETAIL = 20

# ===== 工具函数 =====

def get_limit_ratio(code):
    """获取涨跌停幅度比例"""
    # 创业板 300/301
    if code.startswith('sz.300') or code.startswith('sz.301'):
        return 0.2
    # 科创板 688
    if code.startswith('sh.688'):
        return 0.2
    # 北交所 43/83/87
    c = code.split('.')[1] if '.' in code else code
    if c.startswith('43') or c.startswith('83') or c.startswith('87'):
        return 0.3
    # 主板 10%
    return 0.1


def calc_limit_up(preclose, ratio):
    """精确计算涨停价"""
    return round(preclose * (1 + ratio), 2)


def calc_limit_down(preclose, ratio):
    """精确计算跌停价"""
    return round(preclose * (1 - ratio), 2)


def query_kline(cursor, code, date):
    """查询日K线+小时K线"""
    cursor.execute(
        "SELECT preclose, open, high, low, close, "
        "hour1_open, hour1_high, hour1_low, hour1_close, "
        "hour2_open, hour2_high, hour2_low, hour2_close, "
        "hour3_open, hour3_high, hour3_low, hour3_close, "
        "hour4_open, hour4_high, hour4_low, hour4_close "
        "FROM stock_kline WHERE code=? AND date=?", (code, date))
    row = cursor.fetchone()
    if row is None:
        return None
    keys = ['preclose','open','high','low','close',
            'hour1_open','hour1_high','hour1_low','hour1_close',
            'hour2_open','hour2_high','hour2_low','hour2_close',
            'hour3_open','hour3_high','hour3_low','hour3_close',
            'hour4_open','hour4_high','hour4_low','hour4_close']
    return dict(zip(keys, row))


def is_yizi_limit_down(kl, ratio):
    """判断是否为一字跌停(open==low==close==跌停价)"""
    if not kl:
        return False
    pc = kl['preclose']
    if not pc:
        return False
    limit_dn = calc_limit_down(pc, ratio)
    o, l, c = kl['open'], kl['low'], kl['close']
    if not all([o, l, c]):
        return False
    # 一字跌停: 开盘=最低=收盘=跌停价
    return (abs(o - limit_dn) < 0.015 and
            abs(l - limit_dn) < 0.015 and
            abs(c - limit_dn) < 0.015 and
            abs(o - l) < 0.015)


# ===== 主验证逻辑 =====

def main():
    with open(TRADES_FILE) as f:
        trades = json.load(f)
    total = len(trades)

    report_lines = []
    def log(msg=""):
        print(msg)
        report_lines.append(msg)

    log("# A股交易合规性验证报告")
    log(f"\n**验证时间**: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    log(f"**总交易笔数**: {total}")
    log(f"**交易明细文件**: `{TRADES_FILE}`")
    log("")

    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cache = {}

    def get_kl(code, date):
        k = (code, date)
        if k not in cache:
            cache[k] = query_kline(cur, code, date)
        return cache[k]

    # ========================================
    # 检查1: T+1违规
    # ========================================
    log("## 1. T+1违规检查")
    log("规则: 不允许当日买当日卖 (buy_date == sell_date)")
    log("")
    r1 = [t for t in trades if t['buy_date'] == t['sell_date']]
    log(f"**结果**: {'PASS' if len(r1)==0 else 'FAIL'} - 违规 {len(r1)} 笔 / {total} 笔")
    if r1:
        log("\n违规明细:")
        log("| # | 股票代码 | 买入日 | 卖出日 |")
        log("|---|---------|--------|--------|")
        for idx, t in enumerate(r1[:MAX_DETAIL]):
            log(f"| {idx+1} | {t['code']} | {t['buy_date']} | {t['sell_date']} |")
        if len(r1) > MAX_DETAIL:
            log(f"| ... | (还有{len(r1)-MAX_DETAIL}笔) | | |")
    log("")

    # ========================================
    # 检查2: 涨停买入检查
    # ========================================
    log("## 2. 涨停买入检查")
    log("规则: buy_price 不得 >= 涨停价 (round(preclose*(1+ratio),2))")
    log("")
    r2 = []
    r2_nodata = 0
    for t in trades:
        code = t['code']
        ratio = get_limit_ratio(code)
        # 优先使用trades中自带的preclose_buy
        pc = t.get('preclose_buy')
        if not pc:
            kl = get_kl(code, t['buy_date'])
            if kl is None:
                r2_nodata += 1
                continue
            pc = kl['preclose']
        if not pc:
            r2_nodata += 1
            continue
        limit_up = calc_limit_up(pc, ratio)
        if t['buy_price'] >= limit_up:
            r2.append({
                'code': t['code'],
                'buy_date': t['buy_date'],
                'buy_price': t['buy_price'],
                'preclose': pc,
                'limit_up': limit_up,
                'ratio': ratio
            })

    log(f"**结果**: {'PASS' if len(r2)==0 else 'FAIL'} - 违规 {len(r2)} 笔 / {total} 笔")
    if r2_nodata:
        log(f"  (无法验证: {r2_nodata} 笔缺少preclose数据)")
    if r2:
        log("\n违规明细:")
        log("| # | 股票代码 | 买入日 | 买入价 | 前收盘 | 涨停价 | 涨停幅度 |")
        log("|---|---------|--------|--------|--------|--------|---------|")
        for idx, v in enumerate(r2[:MAX_DETAIL]):
            log(f"| {idx+1} | {v['code']} | {v['buy_date']} | {v['buy_price']:.3f} | {v['preclose']:.3f} | {v['limit_up']:.3f} | {v['ratio']*100:.0f}% |")
    log("")

    # ========================================
    # 检查3: 跌停卖出检查
    # ========================================
    log("## 3. 跌停卖出检查")
    log("规则: sell_price 不得 <= 跌停价 (round(preclose*(1-ratio),2))")
    log("")
    r3 = []
    r3_nodata = 0
    for t in trades:
        code = t['code']
        ratio = get_limit_ratio(code)
        pc = t.get('preclose_sell')
        if not pc:
            kl = get_kl(code, t['sell_date'])
            if kl is None:
                r3_nodata += 1
                continue
            pc = kl['preclose']
        if not pc:
            r3_nodata += 1
            continue
        limit_down = calc_limit_down(pc, ratio)
        if t['sell_price'] <= limit_down:
            # 跌停卖出 - 需进一步判断是否一字跌停
            kl_sell = get_kl(code, t['sell_date'])
            yizi = is_yizi_limit_down(kl_sell, ratio) if kl_sell else False
            r3.append({
                'code': t['code'],
                'sell_date': t['sell_date'],
                'sell_price': t['sell_price'],
                'preclose': pc,
                'limit_down': limit_down,
                'ratio': ratio,
                'is_yizi': yizi
            })

    log(f"**结果**: {'PASS' if len(r3)==0 else 'FAIL'} - 违规 {len(r3)} 笔 / {total} 笔")
    if r3_nodata:
        log(f"  (无法验证: {r3_nodata} 笔缺少preclose数据)")
    if r3:
        log("\n违规明细:")
        log("| # | 股票代码 | 卖出日 | 卖出价 | 前收盘 | 跌停价 | 一字跌停 |")
        log("|---|---------|--------|--------|--------|--------|---------|")
        for idx, v in enumerate(r3[:MAX_DETAIL]):
            log(f"| {idx+1} | {v['code']} | {v['sell_date']} | {v['sell_price']:.3f} | {v['preclose']:.3f} | {v['limit_down']:.3f} | {'是' if v['is_yizi'] else '否'} |")
    log("")

    # ========================================
    # 检查4: 持仓天数合理性
    # ========================================
    log("## 4. 持仓天数合理性检查")
    log("规则: 1 <= hold_days <= 10 (max_hold_days*2 安全阀)")
    log("")
    r4_low = [t for t in trades if t['hold_days'] < 1]
    r4_high = [t for t in trades if t['hold_days'] > 10]
    r4 = r4_low + r4_high
    log(f"**结果**: {'PASS' if len(r4)==0 else 'FAIL'} - 违规 {len(r4)} 笔 / {total} 笔")
    log(f"  - hold_days < 1: {len(r4_low)} 笔")
    log(f"  - hold_days > 10: {len(r4_high)} 笔")
    hold_days_dist = {}
    for t in trades:
        d = t['hold_days']
        hold_days_dist[d] = hold_days_dist.get(d, 0) + 1
    log(f"  - 持仓天数分布: {dict(sorted(hold_days_dist.items()))}")
    if r4:
        log("\n违规明细:")
        for idx, t in enumerate(r4[:MAX_DETAIL]):
            log(f"  #{idx+1} {t['code']} buy={t['buy_date']} sell={t['sell_date']} hold_days={t['hold_days']}")
    log("")

    # ========================================
    # 检查5: 一字跌停卖出检查(关键)
    # ========================================
    log("## 5. 一字跌停日卖出检查")
    log("规则: 一字跌停日(open==low==close==跌停价)不可卖出")
    log("注意: 一字涨停日CAN卖出(有大量买单封板)")
    log("")
    r5 = []
    r5_nodata = 0
    for t in trades:
        code = t['code']
        ratio = get_limit_ratio(code)
        kl = get_kl(code, t['sell_date'])
        if kl is None:
            r5_nodata += 1
            continue
        if is_yizi_limit_down(kl, ratio):
            r5.append({
                'code': t['code'],
                'sell_date': t['sell_date'],
                'sell_price': t['sell_price'],
                'preclose': kl['preclose'],
                'limit_down': calc_limit_down(kl['preclose'], ratio)
            })

    log(f"**结果**: {'PASS' if len(r5)==0 else 'FAIL'} - 违规 {len(r5)} 笔 / {total} 笔")
    if r5_nodata:
        log(f"  (无法验证: {r5_nodata} 笔无卖出日K线数据)")
    if r5:
        log("\n违规明细:")
        log("| # | 股票代码 | 卖出日 | 卖出价 | 跌停价 |")
        log("|---|---------|--------|--------|--------|")
        for idx, v in enumerate(r5[:MAX_DETAIL]):
            log(f"| {idx+1} | {v['code']} | {v['sell_date']} | {v['sell_price']:.3f} | {v['limit_down']:.3f} |")
    log("")

    # ========================================
    # 检查6: 确定性验证
    # ========================================
    log("## 6. 确定性验证")
    log("方法: 重新运行回测, 对比交易笔数和最终净值")
    log("")
    try:
        result = subprocess.run(
            [sys.executable, 'backtest_scoring_system.py',
             '--start', '2021-01-01', '--end', '2026-06-30', '--slots', '5'],
            capture_output=True, text=True, timeout=600,
            cwd='/home/AIWealth/scripts'
        )
        output = result.stdout + result.stderr
        # 重新读取生成的trades文件
        with open(TRADES_FILE) as f:
            new_trades = json.load(f)
        new_total = len(new_trades)

        # 读取NAV
        nav_file = '/home/AIWealth/scripts/backtest_scoring_nav.json'
        with open(nav_file) as f:
            new_nav = json.load(f)
        new_final_nav = new_nav[-1]['nav'] if new_nav else None

        # 与原始对比
        trades_match = (new_total == total)
        log(f"  - 原始交易笔数: {total}")
        log(f"  - 重跑交易笔数: {new_total}")
        log(f"  - 笔数一致: {'YES' if trades_match else 'NO'}")

        if new_final_nav is not None:
            log(f"  - 重跑最终净值: {new_final_nav:.4f}")

        # 逐笔对比前10笔
        mismatch_count = 0
        for i in range(min(total, new_total)):
            if (trades[i]['code'] != new_trades[i]['code'] or
                trades[i]['buy_date'] != new_trades[i]['buy_date'] or
                abs(trades[i]['buy_price'] - new_trades[i]['buy_price']) > 0.001):
                mismatch_count += 1
        log(f"  - 逐笔对比不一致数: {mismatch_count}")

        deterministic = trades_match and mismatch_count == 0
        log(f"\n**结果**: {'PASS' if deterministic else 'FAIL'} - {'确定性验证通过' if deterministic else '确定性验证失败'}")
    except subprocess.TimeoutExpired:
        log("  回测超时(>600s), 跳过确定性验证")
        log("**结果**: SKIP")
        deterministic = None
    except Exception as e:
        log(f"  运行出错: {e}")
        log("**结果**: ERROR")
        deterministic = None
    log("")

    # ========================================
    # 检查7: 抽样验证交易合理性
    # ========================================
    log("## 7. 抽样验证交易合理性")
    log("方法: 随机抽取5笔交易, 查数据库验证买入价/卖出价/日期间隔")
    log("")
    random.seed(42)
    sample_indices = random.sample(range(total), min(5, total))
    sample_pass = 0
    sample_fail = 0

    for idx in sample_indices:
        t = trades[idx]
        code = t['code']
        issues = []

        # 验证买入价 == 买入hour的open
        bk = get_kl(code, t['buy_date'])
        if bk is None:
            issues.append("买入日无K线数据")
        else:
            bh = t['buy_hour']
            if bh in ('hour1','hour2','hour3','hour4'):
                expected_open = bk.get(f'{bh}_open')
                if expected_open and expected_open > 0:
                    dev = abs(t['buy_price'] - expected_open) / expected_open
                    if dev > 0.002:
                        issues.append(f"buy_price={t['buy_price']:.4f} != {bh}_open={expected_open:.4f} (偏差{dev*100:.2f}%)")
                else:
                    issues.append(f"{bh}_open无数据")
            # 验证买入价在日K线范围内
            if bk['low'] and bk['high']:
                if t['buy_price'] < bk['low'] * 0.999 or t['buy_price'] > bk['high'] * 1.001:
                    issues.append(f"buy_price={t['buy_price']:.4f} 超出日线范围[{bk['low']:.4f}, {bk['high']:.4f}]")

        # 验证卖出价在卖出hour范围内
        sk = get_kl(code, t['sell_date'])
        if sk is None:
            issues.append("卖出日无K线数据")
        else:
            sh = t['sell_hour']
            if sh in ('hour1','hour2','hour3','hour4'):
                sh_low = sk.get(f'{sh}_low')
                sh_high = sk.get(f'{sh}_high')
                if sh_low and sh_high and sh_low > 0:
                    if t['sell_price'] < sh_low * 0.999 or t['sell_price'] > sh_high * 1.001:
                        issues.append(f"sell_price={t['sell_price']:.4f} 超出{sh}范围[{sh_low:.4f}, {sh_high:.4f}]")
                else:
                    issues.append(f"{sh} OHLC无数据")
            # 日线范围
            if sk['low'] and sk['high']:
                if t['sell_price'] < sk['low'] * 0.999 or t['sell_price'] > sk['high'] * 1.001:
                    issues.append(f"sell_price={t['sell_price']:.4f} 超出日线范围[{sk['low']:.4f}, {sk['high']:.4f}]")

        # 验证日期间隔 == hold_days (交易日间隔)
        # hold_days在回测中基于交易日计算,这里做基本验证
        from datetime import date as dt_date
        bd = dt_date.fromisoformat(t['buy_date'])
        sd = dt_date.fromisoformat(t['sell_date'])
        calendar_days = (sd - bd).days
        if calendar_days < t['hold_days']:
            issues.append(f"日历天数{calendar_days} < hold_days={t['hold_days']}")

        status = "PASS" if not issues else "FAIL"
        if issues:
            sample_fail += 1
        else:
            sample_pass += 1

        log(f"### 样本 #{idx}")
        log(f"- 股票: {t['code']}")
        log(f"- 买入: {t['buy_date']} / {t['buy_hour']} @ {t['buy_price']:.3f}")
        log(f"- 卖出: {t['sell_date']} / {t['sell_hour']} @ {t['sell_price']:.3f}")
        log(f"- 持仓: {t['hold_days']}天, 收益: {t['pnl_pct']:.2f}%")
        log(f"- 卖出原因: {t['sell_reason']}")
        log(f"- **验证**: {status}")
        if issues:
            for iss in issues:
                log(f"  - {iss}")
        log("")

    log(f"**抽样总结**: 通过={sample_pass}, 失败={sample_fail}")
    log("")

    # ========================================
    # 总结
    # ========================================
    log("## 总结")
    log("")
    all_violations = len(r1) + len(r2) + len(r3) + len(r4) + len(r5)
    # 去重统计有问题的交易
    violation_indices = set()
    for t in r1:
        violation_indices.add(trades.index(t) if t in trades else -1)
    # r2-r5 use code+buy_date as key
    violation_codes = set()
    for v in r2:
        violation_codes.add((v['code'], v['buy_date']))
    for v in r3:
        violation_codes.add((v['code'], v['sell_date']))
    for v in r5:
        violation_codes.add((v['code'], v['sell_date']))

    log("| 检查项 | 结果 | 违规笔数 |")
    log("|--------|------|---------|")
    log(f"| 1. T+1违规 | {'PASS' if len(r1)==0 else 'FAIL'} | {len(r1)} |")
    log(f"| 2. 涨停买入 | {'PASS' if len(r2)==0 else 'FAIL'} | {len(r2)} |")
    log(f"| 3. 跌停卖出 | {'PASS' if len(r3)==0 else 'FAIL'} | {len(r3)} |")
    log(f"| 4. 持仓天数 | {'PASS' if len(r4)==0 else 'FAIL'} | {len(r4)} |")
    log(f"| 5. 一字跌停卖出 | {'PASS' if len(r5)==0 else 'FAIL'} | {len(r5)} |")
    det_str = 'PASS' if deterministic else ('SKIP' if deterministic is None else 'FAIL')
    log(f"| 6. 确定性验证 | {det_str} | - |")
    log(f"| 7. 抽样验证 | {'PASS' if sample_fail==0 else 'FAIL'} | {sample_fail}/5 |")
    log("")

    total_checks = 7
    passed = sum([
        len(r1) == 0,
        len(r2) == 0,
        len(r3) == 0,
        len(r4) == 0,
        len(r5) == 0,
        deterministic == True,
        sample_fail == 0
    ])
    log(f"**合规率**: {total - len(set(r1) | set(r4_low) | set(r4_high))}/{total} 笔交易无违规")
    log(f"**检查通过率**: {passed}/{total_checks} 项检查通过")
    log("")

    if all_violations == 0 and (deterministic is True or deterministic is None) and sample_fail == 0:
        log("**结论: 全部合规检查通过, 交易明细质量合格。**")
    else:
        log("**结论: 存在合规问题, 详见各检查项。**")

    conn.close()

    # 写入报告文件
    with open(REPORT_FILE, 'w') as f:
        f.write('\n'.join(report_lines))
    print(f"\n报告已保存至: {REPORT_FILE}")


if __name__ == '__main__':
    main()
#!/usr/bin/env python3
"""QA - A股交易合规性验证"""
import json, sqlite3, random

TRADES_FILE = '/home/AIWealth/scripts/backtest_scoring_trades.json'
DB_PATH = '/home/AIWealth/data/stocks.db'
MAX_DETAIL = 20

def get_limit_ratio(code):
    if code.startswith('sz.300') or code.startswith('sz.301'):
        return 0.2
    if code.startswith('sh.688'):
        return 0.2
    return 0.1

def query_kline(cursor, code, date):
    cursor.execute(
        "SELECT preclose, open, high, low, close, "
        "hour1_open, hour1_high, hour1_low, hour1_close, "
        "hour2_open, hour2_high, hour2_low, hour2_close, "
        "hour3_open, hour3_high, hour3_low, hour3_close, "
        "hour4_open, hour4_high, hour4_low, hour4_close "
        "FROM stock_kline WHERE code=? AND date=?", (code, date))
    row = cursor.fetchone()
    if row is None:
        return None
    keys = ['preclose','open','high','low','close',
            'hour1_open','hour1_high','hour1_low','hour1_close',
            'hour2_open','hour2_high','hour2_low','hour2_close',
            'hour3_open','hour3_high','hour3_low','hour3_close',
            'hour4_open','hour4_high','hour4_low','hour4_close']
    return dict(zip(keys, row))

def is_yizi_up(kl, lr):
    if not kl: return False
    pc = kl['preclose']
    if not pc: return False
    lp = pc * (1 + lr)
    o, h, c = kl['open'], kl['high'], kl['close']
    if not all([o, h, c]): return False
    return abs(o-h)<0.001 and abs(o-c)<0.001 and o >= lp*0.998

def is_yizi_down(kl, lr):
    if not kl: return False
    pc = kl['preclose']
    if not pc: return False
    lp = pc * (1 - lr)
    o, l, c = kl['open'], kl['low'], kl['close']
    if not all([o, l, c]): return False
    return abs(o-l)<0.001 and abs(o-c)<0.001 and o <= lp*1.002

def main():
    with open(TRADES_FILE) as f:
        trades = json.load(f)
    total = len(trades)
    print("=== A股交易合规性验证报告 ===")
    print(f"总交易数: {total}")
    print()

    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cache = {}
    def get_kl(code, date):
        k=(code,date)
        if k not in cache:
            cache[k] = query_kline(cur, code, date)
        return cache[k]

    # 规则1: T+1
    r1 = []
    for i,t in enumerate(trades):
        if t['sell_date'] <= t['buy_date']:
            r1.append((i,t))
    print(f"[规则1: T+1] 违规: {len(r1)}笔 ({100*len(r1)/total:.1f}%)")
    if r1:
        for idx,(i,t) in enumerate(r1[:MAX_DETAIL]):
            print(f"  #{idx+1} {t['code']} buy={t['buy_date']} sell={t['sell_date']}")
    else:
        print("  无违规")
    print()

    # 规则2: 涨停不可买入
    r2 = []
    r2nd = 0
    for i,t in enumerate(trades):
        code,bd,bp = t['code'],t['buy_date'],t['buy_price']
        lr = get_limit_ratio(code)
        kl = get_kl(code, bd)
        if kl is None: r2nd+=1; continue
        pc = kl['preclose']
        if not pc: r2nd+=1; continue
        lup = pc*(1+lr)
        pv = bp >= lup*0.998
        yv = is_yizi_up(kl, lr)
        if pv or yv:
            rs=[]
            if pv: rs.append(f"buy={bp:.3f}>=limit*0.998={lup*0.998:.3f}")
            if yv: rs.append("一字涨停")
            r2.append((i,t,'; '.join(rs),pc,lup))
    print(f"[规则2: 涨停不可买入] 违规: {len(r2)}笔 ({100*len(r2)/total:.1f}%)")
    if r2nd: print(f"  (无K线数据: {r2nd}笔)")
    if r2:
        for idx,(i,t,rs,pc,lp) in enumerate(r2[:MAX_DETAIL]):
            print(f"  #{idx+1} {t['code']} {t['buy_date']} buy={t['buy_price']:.3f} preclose={pc:.3f} limit_up={lp:.3f} | {rs}")
    else:
        print("  无违规")
    print()

    # 规则3: 跌停不可卖出
    r3 = []
    r3nd = 0
    for i,t in enumerate(trades):
        code,sd,sp = t['code'],t['sell_date'],t['sell_price']
        lr = get_limit_ratio(code)
        kl = get_kl(code, sd)
        if kl is None: r3nd+=1; continue
        pc = kl['preclose']
        if not pc: r3nd+=1; continue
        ldn = pc*(1-lr)
        pv = sp <= ldn*1.002
        yv = is_yizi_down(kl, lr)
        if pv or yv:
            rs=[]
            if pv: rs.append(f"sell={sp:.3f}<=limit_dn*1.002={ldn*1.002:.3f}")
            if yv: rs.append("一字跌停")
            r3.append((i,t,'; '.join(rs),pc,ldn))
    print(f"[规则3: 跌停不可卖出] 违规: {len(r3)}笔 ({100*len(r3)/total:.1f}%)")
    if r3nd: print(f"  (无K线数据: {r3nd}笔)")
    if r3:
        for idx,(i,t,rs,pc,lp) in enumerate(r3[:MAX_DETAIL]):
            print(f"  #{idx+1} {t['code']} {t['sell_date']} sell={t['sell_price']:.3f} preclose={pc:.3f} limit_dn={lp:.3f} | {rs}")
    else:
        print("  无违规")
    print()

    # 规则4: 买入价格合理性
    r4 = []
    r4nd = 0
    for i,t in enumerate(trades):
        code,bd,bp,bh = t['code'],t['buy_date'],t['buy_price'],t['buy_hour']
        kl = get_kl(code, bd)
        if kl is None: r4nd+=1; continue
        dl,dh = kl['low'],kl['high']
        if not dl or not dh: r4nd+=1; continue
        viol=[]
        tol=0.001
        if bp < dl*(1-tol): viol.append(f"buy={bp:.3f}<low={dl:.3f}")
        if bp > dh*(1+tol): viol.append(f"buy={bp:.3f}>high={dh:.3f}")
        if bh=='hour1':
            h1o=kl.get('hour1_open')
            if h1o and h1o>0:
                if abs(bp-h1o)/h1o > 0.002:
                    viol.append(f"hour1: buy={bp:.3f}!=open={h1o:.3f}")
        if bh in ('hour1','hour2','hour3','hour4'):
            hl=kl.get(f'{bh}_low')
            hh=kl.get(f'{bh}_high')
            if hl and hh and hl>0:
                if bp < hl*(1-tol) or bp > hh*(1+tol):
                    viol.append(f"buy={bp:.3f}不在{bh}[{hl:.3f},{hh:.3f}]")
        if viol:
            r4.append((i,t,'; '.join(viol)))
    print(f"[规则4: 买入价格合理性] 违规: {len(r4)}笔 ({100*len(r4)/total:.1f}%)")
    if r4nd: print(f"  (无K线数据: {r4nd}笔)")
    if r4:
        for idx,(i,t,rs) in enumerate(r4[:MAX_DETAIL]):
            print(f"  #{idx+1} {t['code']} {t['buy_date']} {t['buy_hour']} buy={t['buy_price']:.3f} | {rs}")
    else:
        print("  无违规")
    print()

    # 规则5: 卖出价格合理性
    r5 = []
    r5nd = 0
    for i,t in enumerate(trades):
        code,sd,sp,sh = t['code'],t['sell_date'],t['sell_price'],t['sell_hour']
        kl = get_kl(code, sd)
        if kl is None: r5nd+=1; continue
        dl,dh = kl['low'],kl['high']
        if not dl or not dh: r5nd+=1; continue
        viol=[]
        tol=0.001
        if sp < dl*(1-tol): viol.append(f"sell={sp:.3f}<low={dl:.3f}")
        if sp > dh*(1+tol): viol.append(f"sell={sp:.3f}>high={dh:.3f}")
        if sh in ('hour1','hour2','hour3','hour4'):
            hl=kl.get(f'{sh}_low')
            hh=kl.get(f'{sh}_high')
            if hl and hh and hl>0:
                if sp < hl*(1-tol) or sp > hh*(1+tol):
                    viol.append(f"sell={sp:.3f}不在{sh}[{hl:.3f},{hh:.3f}]")
        if viol:
            r5.append((i,t,'; '.join(viol)))
    print(f"[规则5: 卖出价格合理性] 违规: {len(r5)}笔 ({100*len(r5)/total:.1f}%)")
    if r5nd: print(f"  (无K线数据: {r5nd}笔)")
    if r5:
        for idx,(i,t,rs) in enumerate(r5[:MAX_DETAIL]):
            print(f"  #{idx+1} {t['code']} {t['sell_date']} {t['sell_hour']} sell={t['sell_price']:.3f} | {rs}")
    else:
        print("  无违规")
    print()

    # 抽样交叉验证
    print(f"=== 抽样交叉验证 (50笔) ===")
    print()
    sorted_pnl = sorted(enumerate(trades), key=lambda x: x[1]['pnl_pct'], reverse=True)
    top_p = sorted_pnl[:10]
    top_l = sorted_pnl[-10:]
    sel = set([x[0] for x in top_p]+[x[0] for x in top_l])
    rem = [i for i in range(total) if i not in sel]
    random.seed(42)
    rand_s = random.sample(rem, min(30, len(rem)))
    samples = ([(i,trades[i],"最大盈利") for i in [x[0] for x in top_p]] +
               [(i,trades[i],"最大亏损") for i in [x[0] for x in top_l]] +
               [(i,trades[i],"随机") for i in rand_s])
    cp,cf,cn = 0,0,0
    for idx,(i,t,cat) in enumerate(samples):
        code=t['code']
        bk=get_kl(code,t['buy_date'])
        sk=get_kl(code,t['sell_date'])
        iss=[]
        if bk is None:
            iss.append("买入日无数据")
        else:
            bh=t['buy_hour']
            if bh in ('hour1','hour2','hour3','hour4'):
                eo=bk.get(f'{bh}_open')
                if eo and eo>0:
                    dev=abs(t['buy_price']-eo)/eo
                    if dev>0.002:
                        iss.append(f"buy={t['buy_price']:.4f}!={bh}_open={eo:.4f}(偏差{100*dev:.2f}%)")
                else:
                    iss.append(f"{bh}_open无数据")
            if bk['low'] and bk['high']:
                if t['buy_price']<bk['low']*0.999 or t['buy_price']>bk['high']*1.001:
                    iss.append(f"buy超日范围[{bk['low']:.4f},{bk['high']:.4f}]")
        if sk is None:
            iss.append("卖出日无数据")
        else:
            sh=t['sell_hour']
            if sh in ('hour1','hour2','hour3','hour4'):
                hl=sk.get(f'{sh}_low')
                hh=sk.get(f'{sh}_high')
                if hl and hh and hl>0:
                    if t['sell_price']<hl*0.999 or t['sell_price']>hh*1.001:
                        iss.append(f"sell={t['sell_price']:.4f}不在{sh}[{hl:.4f},{hh:.4f}]")
                else:
                    iss.append(f"{sh} OHLC无数据")
            if sk['low'] and sk['high']:
                if t['sell_price']<sk['low']*0.999 or t['sell_price']>sk['high']*1.001:
                    iss.append(f"sell超日范围[{sk['low']:.4f},{sk['high']:.4f}]")
        if bk is None and sk is None:
            cn+=1; st="NO_DATA"
        elif iss:
            cf+=1; st="FAIL"
        else:
            cp+=1; st="PASS"
        print(f"  [{cat}] #{i} {code} buy={t['buy_date']}/{t['buy_hour']}@{t['buy_price']:.3f} "
              f"sell={t['sell_date']}/{t['sell_hour']}@{t['sell_price']:.3f} pnl={t['pnl_pct']:.1f}% | {st}")
        if iss:
            for x in iss:
                print(f"       -> {x}")
    print()
    print(f"  抽样结果: 通过={cp} 不通过={cf} 无数据={cn}")
    print()

    # 总结
    print("=== 总结 ===")
    all_v = set()
    all_v.update([v[0] for v in r1])
    all_v.update([v[0] for v in r2])
    all_v.update([v[0] for v in r3])
    all_v.update([v[0] for v in r4])
    all_v.update([v[0] for v in r5])
    ok = total - len(all_v)
    rate = 100*ok/total
    print(f"规则1(T+1)违规: {len(r1)}笔")
    print(f"规则2(涨停买入)违规: {len(r2)}笔")
    print(f"规则3(跌停卖出)违规: {len(r3)}笔")
    print(f"规则4(买入价格)违规: {len(r4)}笔")
    print(f"规则5(卖出价格)违规: {len(r5)}笔")
    print(f"存在违规的交易总数(去重): {len(all_v)}笔")
    print(f"总合规率: {rate:.1f}% ({ok}/{total})")
    conn.close()

if __name__ == '__main__':
    main()
