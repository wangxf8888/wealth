#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
龙抬头 (7+连板 -> N字回调 -> 第二波拉升) 策略研究 - rule2 流程
策略思路: 连续涨停打高度 -> 炸板回调洗盘 -> 第二波拉升.
第二波信号(严格合规,只用 D-1 及之前 + D 当日 hour1_open):
  S4_simple : 回调 >=15% + 今日高开 >=3%
  S1_shrink : 回调 >=15% + 缩量企稳(近3日均量<前5日均量*0.5) + 高开 >=3%
  S2_deep   : 回调 >=25% + 今日高开 >=3%
T+1 合规: 买入价=信号日 hour1_open, 买入日不卖, 止盈止损从 T+1 起用 hour 级监控.
用法: stat|all|YYYY|YYYY-MM [board_min]
"""
import sys
import sqlite3
from collections import defaultdict

DB_PATH = '/home/AIWealth/data/stocks.db'
LOG_PATH = '/home/AIWealth/scripts/logs/dragon_second_wave_r2.log'

DEFAULT_BOARD_MIN = 7
MAX_TRACK_DAYS = 30
PULLBACK_SHALLOW = 15.0
PULLBACK_DEEP = 25.0
GAP_UP_MIN = 3.0
SHRINK_RATIO = 0.5
FUTURE_DAYS = 10
MAX_DETAIL_PER_EVENT = 3
DETAIL_BUDGET = 150

CONFIGS = [
    ("TP8_SL5_H3",   8.0,  -5.0, 3),
    ("TP10_SL8_H3", 10.0,  -8.0, 3),
    ("TP15_SL8_H5", 15.0,  -8.0, 5),
    ("TP15_SL10_H5",15.0, -10.0, 5),
    ("TP20_SL10_H5",20.0, -10.0, 5),
    ("TP20_SL12_H8",20.0, -12.0, 8),
    ("TP30_SL12_H8",30.0, -12.0, 8),
    ("TP30_SL15_H10",30.0,-15.0, 10),
    ("TP50_SL15_H10",50.0,-15.0, 10),
    ("HOLD_H2_close", None, None, 2),
    ("HOLD_H3_close", None, None, 3),
    ("HOLD_H5_close", None, None, 5),
    ("HOLD_H10_close",None, None, 10),
]

SIGNAL_TYPES = ['S4_simple', 'S1_shrink', 'S2_deep']


def log_line(fh, text=""):
    print(text)
    fh.write(text + "\n")


def get_limit_ratio(code):
    if code.startswith('sz.300') or code.startswith('sz.301'):
        return 0.20
    if code.startswith('sh.688'):
        return 0.20
    if code.startswith('bj.'):
        return 0.30
    return 0.10


def is_limit_up(close, preclose, code):
    if close is None or preclose is None or preclose <= 0:
        return False
    limit_price = round(preclose * (1 + get_limit_ratio(code)), 2)
    return close >= limit_price - 0.001


def is_st_name(name):
    return bool(name) and 'ST' in name.upper()


def get_all_trading_days(cur):
    cur.execute("SELECT DISTINCT date FROM stock_kline ORDER BY date")
    return [r[0] for r in cur.fetchall()]


def load_daily(cur):
    print("加载日线数据 ...")
    cur.execute("""
        SELECT code, code_name, date, open, high, low, close, preclose,
               volume, turn, isST
        FROM stock_kline
        ORDER BY code, date
    """)
    data = defaultdict(list)
    for r in cur.fetchall():
        data[r[0]].append({
            'name': r[1], 'date': r[2], 'open': r[3], 'high': r[4],
            'low': r[5], 'close': r[6], 'preclose': r[7],
            'volume': r[8], 'turn': r[9], 'isST': r[10],
        })
    print(f"加载完成: {len(data)} 只股票")
    return data


def find_board_events(daily, board_min):
    events = []
    for code, rows in daily.items():
        n = len(rows)
        i = 0
        while i < n:
            row = rows[i]
            if is_limit_up(row['close'], row['preclose'], code) and not row['isST'] \
                    and not is_st_name(row['name']):
                j = i
                while j + 1 < n:
                    nxt = rows[j + 1]
                    if nxt['isST'] or is_st_name(nxt['name']):
                        break
                    if is_limit_up(nxt['close'], nxt['preclose'], code):
                        j += 1
                    else:
                        break
                board_count = j - i + 1
                if board_count >= board_min:
                    events.append({
                        'code': code, 'name': rows[j]['name'],
                        'start_date': rows[i]['date'], 'end_date': rows[j]['date'],
                        'board_count': board_count, 'peak_close': rows[j]['close'],
                        'end_idx': j,
                    })
                i = j + 1
            else:
                i += 1
    return events


def scan_second_wave_signals(daily, event):
    code = event['code']
    rows = daily[code]
    end_idx = event['end_idx']
    peak = event['peak_close']
    if peak is None or peak <= 0:
        return {}
    n = len(rows)
    signals = {}
    found_types = set()
    for k in range(end_idx + 2, min(end_idx + 1 + MAX_TRACK_DAYS, n)):
        if len(found_types) == len(SIGNAL_TYPES):
            break
        d = rows[k]
        d_prev = rows[k - 1]
        prev_close = d_prev['close']
        if prev_close is None or prev_close <= 0:
            continue
        if prev_close >= peak:
            continue
        pullback = (peak - prev_close) / peak * 100.0
        d_open = d['open']
        if d_open is None or d_open <= 0:
            continue
        gap = (d_open - prev_close) / prev_close * 100.0
        if gap < GAP_UP_MIN:
            continue
        shrink_ok = False
        if k - 6 >= 0:
            vol3 = [rows[x]['volume'] for x in range(k - 3, k)
                    if rows[x]['volume'] is not None]
            vol5 = [rows[x]['volume'] for x in range(k - 6, k - 1)
                    if rows[x]['volume'] is not None]
            if len(vol3) == 3 and len(vol5) >= 3:
                ma3 = sum(vol3) / len(vol3)
                ma5 = sum(vol5) / len(vol5)
                if ma5 > 0 and ma3 < ma5 * SHRINK_RATIO:
                    shrink_ok = True
        base = {
            'code': code, 'name': event['name'], 'signal_date': d['date'],
            'end_date': event['end_date'], 'board_count': event['board_count'],
            'peak_close': peak, 'prev_close': prev_close,
            'pullback_pct': pullback, 'gap': gap, 'shrink_ok': shrink_ok,
            'signal_idx': k, 'days_after_board': k - end_idx,
        }
        if 'S4_simple' not in found_types and pullback >= PULLBACK_SHALLOW:
            s = dict(base); s['type'] = 'S4_simple'
            signals['S4_simple'] = s; found_types.add('S4_simple')
        if 'S1_shrink' not in found_types and pullback >= PULLBACK_SHALLOW and shrink_ok:
            s = dict(base); s['type'] = 'S1_shrink'
            signals['S1_shrink'] = s; found_types.add('S1_shrink')
        if 'S2_deep' not in found_types and pullback >= PULLBACK_DEEP:
            s = dict(base); s['type'] = 'S2_deep'
            signals['S2_deep'] = s; found_types.add('S2_deep')
    return signals


def get_hour_window(cur, code, days_list):
    if not days_list:
        return []
    placeholders = ','.join(['?'] * len(days_list))
    cur.execute(f"""
        SELECT date, open, high, low, close, turn,
               hour1_open, hour1_high, hour1_low, hour1_close,
               hour2_open, hour2_high, hour2_low, hour2_close,
               hour3_open, hour3_high, hour3_low, hour3_close,
               hour4_open, hour4_high, hour4_low, hour4_close
        FROM stock_kline WHERE code = ? AND date IN ({placeholders})
        ORDER BY date
    """, [code] + days_list)
    return cur.fetchall()


def get_signal_day_hour1_open(cur, code, date):
    cur.execute("SELECT hour1_open, open FROM stock_kline WHERE code=? AND date=?",
                (code, date))
    r = cur.fetchone()
    if not r:
        return None
    if r[0] and r[0] > 0:
        return r[0]
    return r[1] if (r[1] and r[1] > 0) else None


def format_hour_table(fh, hour_rows, buy_price, buy_date):
    log_line(fh, f"  {'日期':<11}|{'hr':<4}|{'open':>8}|{'high':>8}|{'low':>8}|{'close':>8}| vsBuy   | turn")
    log_line(fh, f"  {'-'*11}+{'-'*4}+{'-'*8}+{'-'*8}+{'-'*8}+{'-'*8}+{'-'*9}+{'-'*7}")
    for row in hour_rows:
        date = row[0]
        day_turn = row[5]
        mark = " *BUY" if date == buy_date else ""
        hours = [
            ('h1', row[6], row[7], row[8], row[9]),
            ('h2', row[10], row[11], row[12], row[13]),
            ('h3', row[14], row[15], row[16], row[17]),
            ('h4', row[18], row[19], row[20], row[21]),
        ]
        for hn, ho, hh, hl, hc in hours:
            if ho is None or hc is None:
                continue
            vs = (hc - buy_price) / buy_price * 100 if buy_price else 0
            ts = f"{day_turn:.2f}" if day_turn else "-"
            log_line(fh, f"  {date:<11}|{hn:<4}|{ho:>8.2f}|{hh:>8.2f}|{hl:>8.2f}|{hc:>8.2f}|{vs:>+7.2f}%|{ts:>6}{mark}")


def build_future_hour_path(cur, code, signal_date, all_days):
    try:
        idx = all_days.index(signal_date)
    except ValueError:
        return []
    future_days = all_days[idx + 1: idx + 1 + FUTURE_DAYS]
    if not future_days:
        return []
    rows = get_hour_window(cur, code, future_days)
    path = []
    for row in rows:
        date = row[0]
        hours = [
            ('h1', row[7], row[8], row[9]),
            ('h2', row[11], row[12], row[13]),
            ('h3', row[15], row[16], row[17]),
            ('h4', row[19], row[20], row[21]),
        ]
        for hn, hh, hl, hc in hours:
            if hh is None or hl is None or hc is None:
                continue
            path.append((date, hn, hh, hl, hc))
    return path


def simulate_config(buy_price, path, tp, sl, hold_days):
    if not path or buy_price is None or buy_price <= 0:
        return None
    day_seq = []
    for d, _, _, _, _ in path:
        if d not in day_seq:
            day_seq.append(d)
    allowed_days = set(day_seq[:hold_days])
    tp_price = buy_price * (1 + tp / 100.0) if tp is not None else None
    sl_price = buy_price * (1 + sl / 100.0) if sl is not None else None
    last_close = None
    for d, hn, hh, hl, hc in path:
        if d not in allowed_days:
            continue
        last_close = hc
        if sl_price is not None and hl <= sl_price:
            return (sl, 'SL')
        if tp_price is not None and hh >= tp_price:
            return (tp, 'TP')
    if last_close is None:
        return None
    return ((last_close - buy_price) / buy_price * 100.0, 'CLOSE')


def report_events(fh, events, board_min):
    log_line(fh, "\n" + "=" * 80)
    log_line(fh, f"【连板事件统计】阈值 >= {board_min} 连板 (非ST)")
    log_line(fh, "=" * 80)
    by_year = defaultdict(list)
    for e in events:
        by_year[e['end_date'][:4]].append(e)
    log_line(fh, f"  {'年份':<6}|{'事件数':>6}|  代表股票 (板数)")
    log_line(fh, f"  {'-'*6}+{'-'*6}+{'-'*50}")
    for yr in sorted(by_year):
        evs = sorted(by_year[yr], key=lambda x: x['board_count'], reverse=True)
        samples = ', '.join(f"{e['name'] or e['code']}({e['board_count']})"
                            for e in evs[:6])
        log_line(fh, f"  {yr:<6}|{len(evs):>6}|  {samples}")
    log_line(fh, f"  {'-'*6}+{'-'*6}+{'-'*50}")
    log_line(fh, f"  合计: {len(events)} 个连板事件")
    dist = defaultdict(int)
    for e in events:
        b = e['board_count']
        key = f"{b}板" if b < 10 else "10板+"
        dist[key] += 1
    log_line(fh, "  板数分布: " + ' '.join(f"{k}={v}" for k, v in sorted(dist.items())))


def analyze(fh, records):
    n = len(records)
    log_line(fh)
    log_line(fh, "=" * 80)
    log_line(fh, f"汇总分析  (第二波信号样本 {n})")
    log_line(fh, "=" * 80)
    if n == 0:
        log_line(fh, "无第二波信号样本.")
        return
    log_line(fh, "\n【0】各信号类型触发数")
    by_type = defaultdict(list)
    for r in records:
        by_type[r['type']].append(r)
    for st in SIGNAL_TYPES:
        log_line(fh, f"  {st:<12}: {len(by_type[st])} 个")

    log_line(fh, "\n【1】回调幅度分布 (信号触发时相对连板高点)")
    pbs = [r['pullback_pct'] for r in records]
    ranges = [(0, 15, '<15%'), (15, 20, '15~20%'), (20, 25, '20~25%'),
              (25, 30, '25~30%'), (30, 40, '30~40%'), (40, 999, '>=40%')]
    for lo, hi, lbl in ranges:
        cnt = sum(1 for x in pbs if lo <= x < hi)
        if cnt:
            log_line(fh, f"  {lbl:<8}: {cnt/len(pbs)*100:>5.1f}%  ({cnt}/{len(pbs)})")
    log_line(fh, f"  平均回调 {sum(pbs)/len(pbs):.1f}%  中位 {sorted(pbs)[len(pbs)//2]:.1f}%")

    log_line(fh, "\n【2】止盈触及率 (买入=信号日 hour1_open, 观察 T+1~T+%d)" % FUTURE_DAYS)
    for tp in [8, 10, 15, 20, 30, 50]:
        hit = sum(1 for r in records if r['max_up'] is not None and r['max_up'] >= tp)
        valid = sum(1 for r in records if r['max_up'] is not None)
        if valid:
            log_line(fh, f"  +{tp:>2}% : {hit/valid*100:>5.1f}%  ({hit}/{valid})")

    log_line(fh, "\n【3】最大回撤分布 (T+1~T+%d hour低点 vs 买入价)" % FUTURE_DAYS)
    downs = [r['max_down'] for r in records if r['max_down'] is not None]
    if downs:
        ds = sorted(downs)
        db = [(-5, '0~-5%'), (-8, '-5~-8%'), (-10, '-8~-10%'),
              (-15, '-10~-15%'), (-100, '<-15%')]
        prev = 0
        for thr, lbl in db:
            cnt = sum(1 for x in downs if thr < x <= prev)
            log_line(fh, f"  {lbl:<9}: {cnt/len(downs)*100:>5.1f}%  ({cnt}/{len(downs)})")
            prev = thr
        log_line(fh, f"  中位最大回撤 {ds[len(ds)//2]:+.2f}%  平均 {sum(downs)/len(downs):+.2f}%")

    log_line(fh, "\n【4】止盈止损配置对比 (全体信号, 买入=信号日 hour1_open, T+1起监控)")
    log_line(fh, f"  {'配置':<16}|{'样本':>5}|{'均收益':>8}|{'胜率':>7}|{'TP占':>6}|{'SL占':>6}|{'平仓占':>7}")
    log_line(fh, f"  {'-'*16}+{'-'*5}+{'-'*8}+{'-'*7}+{'-'*6}+{'-'*6}+{'-'*7}")
    config_stats = {}
    for name, tp, sl, hold in CONFIGS:
        rets, reasons = [], defaultdict(int)
        for r in records:
            res = r['sims'].get(name)
            if res is None:
                continue
            rets.append(res[0]); reasons[res[1]] += 1
        if rets:
            win = sum(1 for x in rets if x > 0); m = len(rets); avg = sum(rets) / m
            config_stats[name] = (avg, win / m * 100, m)
            log_line(fh, f"  {name:<16}|{m:>5}|{avg:>+7.2f}%|{win/m*100:>6.1f}%|"
                         f"{reasons['TP']/m*100:>5.0f}%|{reasons['SL']/m*100:>5.0f}%|{reasons['CLOSE']/m*100:>6.0f}%")

    log_line(fh, "\n【5】各信号类型下最优配置收益")
    for st in SIGNAL_TYPES:
        recs = by_type[st]
        if not recs:
            continue
        best = None
        for name, tp, sl, hold in CONFIGS:
            rets = [r['sims'][name][0] for r in recs if r['sims'].get(name)]
            if not rets:
                continue
            avg = sum(rets) / len(rets)
            win = sum(1 for x in rets if x > 0) / len(rets) * 100
            if best is None or avg > best[1]:
                best = (name, avg, win, len(rets))
        if best:
            log_line(fh, f"  {st:<12}: 最优 {best[0]:<14} 均收益 {best[1]:+.2f}%  "
                         f"胜率 {best[2]:.1f}%  样本 {best[3]}")

    if config_stats:
        best_name = max(config_stats, key=lambda k: config_stats[k][0])
        log_line(fh, f"\n【6】逐年稳定性 (全体最优配置: {best_name})")
        by_year = defaultdict(list)
        for r in records:
            res = r['sims'].get(best_name)
            if res is None:
                continue
            by_year[r['signal_date'][:4]].append(res[0])
        log_line(fh, f"  {'年份':<6}|{'样本':>5}|{'均收益':>8}|{'胜率':>7}")
        log_line(fh, f"  {'-'*6}+{'-'*5}+{'-'*8}+{'-'*7}")
        for yr in sorted(by_year):
            ys = by_year[yr]
            win = sum(1 for x in ys if x > 0)
            log_line(fh, f"  {yr:<6}|{len(ys):>5}|{sum(ys)/len(ys):>+7.2f}%|{win/len(ys)*100:>6.1f}%")

    log_line(fh, "\n【7】最终结论")
    if config_stats:
        ranked = sorted(config_stats.items(), key=lambda kv: kv[1][0], reverse=True)
        hold_map = dict((c[0], c[3]) for c in CONFIGS)
        log_line(fh, "  按均收益排序 Top6 配置 (估月化 = 均收益 * 20/持仓天数):")
        for name, (avg, wr, m) in ranked[:6]:
            hold = hold_map[name]
            monthly = avg * (20.0 / hold)
            flag = "达标" if (monthly >= 10.0 and wr >= 55.0) else ""
            log_line(fh, f"    {name:<16} 均收益{avg:>+6.2f}%  胜率{wr:>5.1f}%  "
                         f"估月化{monthly:>+6.1f}%  样本{m:>4}  {flag}")
        best_name, (best_avg, best_wr, best_m) = ranked[0]
        best_hold = hold_map[best_name]
        best_monthly = best_avg * (20.0 / best_hold)
        log_line(fh, f"\n  >> 推荐配置: {best_name}")
        log_line(fh, f"     单笔均收益 {best_avg:+.2f}%, 胜率 {best_wr:.1f}%, 估月化 {best_monthly:+.1f}%, 样本 {best_m}")
        log_line(fh, f"     是否达标(月化10%+/胜率55%+): {'是' if (best_monthly>=10 and best_wr>=55) else '否'}")
        log_line(fh, f"     注: 龙抬头频率低, 若单笔期望足够大, 可用于组合中的高弹性仓位.")


def main():
    if len(sys.argv) < 2:
        print("用法: python3 research_dragon_second_wave_r2.py stat|all|YYYY|YYYY-MM [board_min]")
        sys.exit(1)
    arg = sys.argv[1]
    board_min = DEFAULT_BOARD_MIN
    if len(sys.argv) >= 3:
        try:
            board_min = int(sys.argv[2])
        except ValueError:
            pass
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    all_days = get_all_trading_days(cur)
    daily = load_daily(cur)
    print(f"检测 >= {board_min} 连板事件 ...")
    events = find_board_events(daily, board_min)
    events = [e for e in events if '2021-01-01' <= e['end_date'] <= '2026-12-31']
    fh = open(LOG_PATH, 'w', encoding='utf-8')
    log_line(fh, "=" * 80)
    log_line(fh, f"龙抬头 (>={board_min}连板 -> N字回调 -> 第二波) 策略研究 - rule2 流程")
    log_line(fh, f"参数: 连板>={board_min} | 追踪{MAX_TRACK_DAYS}日 | 浅回调>={PULLBACK_SHALLOW}% "
                 f"深回调>={PULLBACK_DEEP}% | 高开>={GAP_UP_MIN}% | 观察T+1~T+{FUTURE_DAYS}")
    log_line(fh, f"数据库交易日 {len(all_days)} | 股票 {len(daily)} 只")
    log_line(fh, "=" * 80)
    report_events(fh, events, board_min)
    if arg == 'stat':
        log_line(fh, "\n(stat 模式: 仅统计连板事件数量)")
        log_line(fh, "\n研究完成. 日志: " + LOG_PATH)
        fh.close(); conn.close()
        return
    if arg == 'all':
        title = "全量 2021-2026"; scope_events = events
    elif len(arg) == 4:
        title = f"{arg} 全年"
        scope_events = [e for e in events if e['end_date'][:4] == arg]
    elif len(arg) == 7 and arg[4] == '-':
        title = f"{arg} 单月"
        scope_events = [e for e in events if e['end_date'][:7] == arg]
    else:
        print(f"参数错误: {arg}")
        fh.close(); conn.close(); sys.exit(1)
    log_line(fh, f"\n研究范围: {title}  (范围内连板事件 {len(scope_events)} 个)")
    records = []
    detail_budget = DETAIL_BUDGET
    total_signals = 0
    for e in scope_events:
        signals = scan_second_wave_signals(daily, e)
        if not signals:
            continue
        printed = 0
        for st in SIGNAL_TYPES:
            sig = signals.get(st)
            if sig is None:
                continue
            total_signals += 1
            code = sig['code']
            buy_price = get_signal_day_hour1_open(cur, code, sig['signal_date'])
            if buy_price is None or buy_price <= 0:
                continue
            if printed < MAX_DETAIL_PER_EVENT and detail_budget > 0:
                printed += 1; detail_budget -= 1
                log_line(fh, f"\n--- {code} ({sig['name'] or 'N/A'}) [{sig['type']}] ---")
                log_line(fh, f"  连板结束 {sig['end_date']} ({sig['board_count']}板) 高点close={sig['peak_close']:.2f}"
                             f" -> 信号日 {sig['signal_date']} (板后第{sig['days_after_board']}日)")
                log_line(fh, f"  昨收 {sig['prev_close']:.2f} 回调 -{sig['pullback_pct']:.1f}% | "
                             f"今日高开 +{sig['gap']:.1f}% | 缩量企稳={'是' if sig['shrink_ok'] else '否'} | "
                             f"买入价(h1open) {buy_price:.2f}")
                try:
                    sidx = all_days.index(sig['signal_date'])
                    prev5 = max(0, sidx - 5)
                    nxt = min(len(all_days), sidx + FUTURE_DAYS + 1)
                    window_days = all_days[prev5:nxt]
                    hour_rows = get_hour_window(cur, code, window_days)
                    if hour_rows:
                        format_hour_table(fh, hour_rows, buy_price, sig['signal_date'])
                except ValueError:
                    pass
            path = build_future_hour_path(cur, code, sig['signal_date'], all_days)
            sims = {}
            for name, tp, sl, hold in CONFIGS:
                res = simulate_config(buy_price, path, tp, sl, hold)
                if res is not None:
                    sims[name] = res
            max_up = max_down = None
            if path:
                highs = [p[2] for p in path]; lows = [p[3] for p in path]
                if highs:
                    max_up = (max(highs) - buy_price) / buy_price * 100
                if lows:
                    max_down = (min(lows) - buy_price) / buy_price * 100
            records.append({
                'type': sig['type'], 'signal_date': sig['signal_date'],
                'code': code, 'buy_price': buy_price, 'pullback_pct': sig['pullback_pct'],
                'sims': sims, 'max_up': max_up, 'max_down': max_down,
            })
    log_line(fh, f"\n\n第二波信号总数: {total_signals}  (进入模拟 {len(records)})")
    analyze(fh, records)
    log_line(fh, "\n研究完成. 日志: " + LOG_PATH)
    fh.close(); conn.close()


if __name__ == '__main__':
    main()
