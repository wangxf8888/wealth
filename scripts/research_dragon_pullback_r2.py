#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
龙回头(创业板 200-700亿) 止盈止损配置研究 - rule2 流程
===================================================================
策略思路:
  创业板 200~700 亿中盘股, 近期(T-6~T-10)创20日新高后回调, 今日跳空高开>=5%,
  即"龙回头二次拉升"逻辑.

选股条件:
  - 板块: sz.300xxx / sz.301xxx (创业板)
  - 流通市值: 200亿 ~ 700亿, 用 amount/(turn/100) 估算 (用昨日数据, 避免未来数据)
  - 龙回头模式:
      * 过去 6-10 日内(T-6~T-10)某日创了20日新高: 该日 high >= max(前20日 highs)
      * 回调: yesterday close < 新高日 close * 0.95  或  最近3日中 >=2 日收阴(close<open)
      * 今日: (open - yesterday_close)/yesterday_close >= 5%
  - 非ST, 非涨停开盘 (open < round(preclose*1.20, 2))  # 创业板20%

T+1 合规:
  - 买入日 T 当日不卖(T+1), 止盈止损从 T+1 起用 hour 级 OHLC 监控.
  - 买入价采用 T 日 hour1_open (9:00-10:00 开盘, 竞价后已知).

用法:
  python3 research_dragon_pullback_r2.py 2025-04   # 单月
  python3 research_dragon_pullback_r2.py 2025       # 单年
  python3 research_dragon_pullback_r2.py all        # 全量 2021-2026

输出:
  日志文件: /home/AIWealth/scripts/logs/dragon_pullback_r2.log
  (脚本用 Python open/write 写日志, 大文件不用 IDE 工具)
"""
import sys
import sqlite3
from collections import defaultdict

DB_PATH = '/home/AIWealth/data/stocks.db'
LOG_PATH = '/home/AIWealth/scripts/logs/dragon_pullback_r2.log'

# ========== 策略参数 ==========
NEW_HIGH_LOOKBACK = 20        # 新高判定回望天数
NH_WINDOW_MIN = 6             # 新高日距今最小天数 (T-6)
NH_WINDOW_MAX = 10            # 新高日距今最大天数 (T-10)
PULLBACK_RATIO = 0.95         # 回调判定: yesterday close < 新高日close * 0.95
GAP_UP_MIN = 5.0             # 今日跳空高开最小幅度 (%)
MKT_CAP_MIN = 200e8           # 流通市值下限
MKT_CAP_MAX = 700e8           # 流通市值上限
MAX_DETAIL_PER_DAY = 6        # 每日最多打印明细的候选股数
FUTURE_DAYS = 5               # 后续观察天数

# 止盈止损配置矩阵: (名称, 止盈%, 止损%, 持仓天数)
CONFIGS = [
    ("TP3_SL5_H2",   3.0,  -5.0, 2),
    ("TP3_SL8_H3",   3.0,  -8.0, 3),
    ("TP5_SL5_H2",   5.0,  -5.0, 2),
    ("TP5_SL8_H3",   5.0,  -8.0, 3),
    ("TP5_SL10_H5",  5.0, -10.0, 5),
    ("TP8_SL5_H3",   8.0,  -5.0, 3),
    ("TP8_SL8_H5",   8.0,  -8.0, 5),
    ("TP10_SL8_H5", 10.0,  -8.0, 5),
    ("TP10_SL10_H5",10.0, -10.0, 5),
    ("TP15_SL10_H5",15.0, -10.0, 5),
    ("HOLD_H1_close", None, None, 1),   # T+1 收盘无条件卖
    ("HOLD_H2_close", None, None, 2),   # 持2日收盘卖
    ("HOLD_H3_close", None, None, 3),   # 持3日收盘卖
    ("HOLD_H5_close", None, None, 5),   # 持5日收盘卖
]


def log_line(fh, text=""):
    """同时输出到控制台与日志文件"""
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


def calc_limit_up(preclose, code):
    return round(preclose * (1 + get_limit_ratio(code)), 2)


def get_trading_days_like(cur, prefix):
    cur.execute("SELECT DISTINCT date FROM stock_kline WHERE date LIKE ? ORDER BY date",
                (prefix + '%',))
    return [r[0] for r in cur.fetchall()]


def get_all_trading_days(cur):
    cur.execute("SELECT DISTINCT date FROM stock_kline ORDER BY date")
    return [r[0] for r in cur.fetchall()]


def find_candidates(cur, today, today_idx, all_days):
    """找出满足龙回头条件的候选股"""
    yesterday_idx = today_idx - 1
    # 需要历史: today 往前 NH_WINDOW_MAX + NEW_HIGH_LOOKBACK 天
    need_back = NH_WINDOW_MAX + NEW_HIGH_LOOKBACK + 2
    if yesterday_idx < need_back:
        return []

    hist_start_idx = today_idx - need_back
    hist_days = all_days[hist_start_idx:today_idx]  # 不含 today
    yesterday = all_days[yesterday_idx]

    # today 数据 (含 hour)
    cur.execute("""
        SELECT code, code_name, open, high, low, close, preclose, turn, isST,
               hour1_open, hour1_high, hour1_low, hour1_close,
               hour2_open, hour2_high, hour2_low, hour2_close,
               hour3_open, hour3_high, hour3_low, hour3_close,
               hour4_open, hour4_high, hour4_low, hour4_close
        FROM stock_kline
        WHERE date = ? AND (code LIKE 'sz.300%' OR code LIKE 'sz.301%')
    """, (today,))
    today_map = {r[0]: r for r in cur.fetchall()}
    if not today_map:
        return []

    # 历史数据
    placeholders = ','.join(['?'] * len(hist_days))
    cur.execute(f"""
        SELECT date, code, open, high, low, close, preclose, turn, amount, isST
        FROM stock_kline
        WHERE date IN ({placeholders})
          AND (code LIKE 'sz.300%' OR code LIKE 'sz.301%')
        ORDER BY code, date
    """, hist_days)

    code_hist = defaultdict(dict)  # code -> {date: row_dict}
    for r in cur.fetchall():
        code_hist[r[1]][r[0]] = {
            'date': r[0], 'open': r[2], 'high': r[3], 'low': r[4],
            'close': r[5], 'preclose': r[6], 'turn': r[7],
            'amount': r[8], 'isST': r[9]
        }

    candidates = []
    for code, dmap in code_hist.items():
        if code not in today_map:
            continue
        if yesterday not in dmap:
            continue

        t = today_map[code]
        t_name = t[1]
        t_open, t_high, t_low, t_close, t_preclose, t_turn, t_isST = t[2], t[3], t[4], t[5], t[6], t[7], t[8]

        # 非ST
        if t_isST:
            continue
        if t_name and 'ST' in t_name.upper():
            continue
        if t_preclose is None or t_preclose <= 0 or t_open is None or t_open <= 0:
            continue

        y = dmap[yesterday]
        y_close = y['close']
        if y_close is None or y_close <= 0:
            continue

        # ===== 今日跳空高开 >= 5% =====
        gap = (t_open - y_close) / y_close * 100
        if gap < GAP_UP_MIN:
            continue

        # ===== 非涨停开盘 =====
        limit_up_price = calc_limit_up(t_preclose, code)
        if t_open >= limit_up_price:
            continue

        # ===== 流通市值 (用昨日 amount/turn) =====
        y_amount, y_turn = y['amount'], y['turn']
        if y_amount is None or y_turn is None or y_turn <= 0:
            continue
        mkt_cap = y_amount / (y_turn / 100.0)
        if mkt_cap < MKT_CAP_MIN or mkt_cap > MKT_CAP_MAX:
            continue

        # ===== 龙回头: 过去6-10日内某日创20日新高 =====
        new_high_day = None
        new_high_close = None
        for offset in range(NH_WINDOW_MAX, NH_WINDOW_MIN - 1, -1):  # 10 -> 6
            d_idx = today_idx - offset
            d = all_days[d_idx]
            if d not in dmap:
                continue
            d_high = dmap[d]['high']
            if d_high is None:
                continue
            prior20 = all_days[d_idx - NEW_HIGH_LOOKBACK:d_idx]
            prior_highs = [dmap[x]['high'] for x in prior20 if x in dmap and dmap[x]['high'] is not None]
            if len(prior_highs) < 15:
                continue
            if d_high >= max(prior_highs):
                new_high_day = d
                new_high_close = dmap[d]['close']
                break  # 取最近(offset从大到小, 命中即最靠前; 反向优先最近)
        # 反向: 优先最近新高日 (offset小), 重新扫一遍取 offset 最小者
        for offset in range(NH_WINDOW_MIN, NH_WINDOW_MAX + 1):  # 6 -> 10
            d_idx = today_idx - offset
            d = all_days[d_idx]
            if d not in dmap:
                continue
            d_high = dmap[d]['high']
            if d_high is None:
                continue
            prior20 = all_days[d_idx - NEW_HIGH_LOOKBACK:d_idx]
            prior_highs = [dmap[x]['high'] for x in prior20 if x in dmap and dmap[x]['high'] is not None]
            if len(prior_highs) < 15:
                continue
            if d_high >= max(prior_highs):
                new_high_day = d
                new_high_close = dmap[d]['close']
                break
        if new_high_day is None or new_high_close is None or new_high_close <= 0:
            continue

        # ===== 回调判定 =====
        cond_a = y_close < new_high_close * PULLBACK_RATIO
        # 最近3日收阴数
        recent3_idx = [yesterday_idx - 2, yesterday_idx - 1, yesterday_idx]
        neg_count = 0
        valid3 = 0
        for ridx in recent3_idx:
            if ridx < 0:
                continue
            rd = all_days[ridx]
            if rd not in dmap:
                continue
            rc, ro = dmap[rd]['close'], dmap[rd]['open']
            if rc is None or ro is None:
                continue
            valid3 += 1
            if rc < ro:
                neg_count += 1
        cond_b = (valid3 >= 2 and neg_count >= 2)
        if not (cond_a or cond_b):
            continue

        pullback_pct = (new_high_close - y_close) / new_high_close * 100
        candidates.append({
            'code': code, 'code_name': t_name, 'today': today,
            'new_high_day': new_high_day, 'new_high_close': new_high_close,
            'yesterday_close': y_close, 'pullback_pct': pullback_pct,
            'cond_a': cond_a, 'cond_b': cond_b, 'neg_count': neg_count,
            'gap': gap, 'mkt_cap': mkt_cap,
            't_open': t_open, 't_high': t_high, 't_low': t_low,
            't_close': t_close, 't_preclose': t_preclose, 't_turn': t_turn,
            'hour_today': {
                'h1': (t[9], t[10], t[11], t[12]),
                'h2': (t[13], t[14], t[15], t[16]),
                'h3': (t[17], t[18], t[19], t[20]),
                'h4': (t[21], t[22], t[23], t[24]),
            }
        })

    candidates.sort(key=lambda x: x['gap'], reverse=True)
    return candidates


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


def build_future_hour_path(cur, code, today, all_days):
    """构建 T+1 起的 hour 级路径 (open, high, low, close), 用于止盈止损模拟"""
    try:
        idx = all_days.index(today)
    except ValueError:
        return []
    future_days = all_days[idx + 1: idx + 1 + FUTURE_DAYS]
    if not future_days:
        return []
    rows = get_hour_window(cur, code, future_days)
    path = []  # 每个元素 (date, hour_name, high, low, close)
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
    """
    模拟单个止盈止损配置的收益.
    path: T+1 起的 hour 级 [(date, hour, high, low, close)]
    tp/sl: 百分比阈值 (sl 为负). None 表示纯持有到期收盘.
    hold_days: 持仓交易日数 (从 T+1 算).
    返回 (ret_pct, exit_reason) 或 None(无数据).
    """
    if not path or buy_price is None or buy_price <= 0:
        return None
    # 按持仓天数截取路径涉及的交易日
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
        # 同一 hour 内 TP/SL 同时触及: 保守取止损优先
        if sl_price is not None and hl <= sl_price:
            return (sl, 'SL')
        if tp_price is not None and hh >= tp_price:
            return (tp, 'TP')
    if last_close is None:
        return None
    return ((last_close - buy_price) / buy_price * 100.0, 'CLOSE')


def analyze(fh, records):
    """对全部候选记录做汇总分析"""
    n = len(records)
    log_line(fh)
    log_line(fh, "=" * 80)
    log_line(fh, f"汇总分析  (总样本 {n})")
    log_line(fh, "=" * 80)
    if n == 0:
        log_line(fh, "无候选样本.")
        return

    # ---- 1. 买入时机 ----
    log_line(fh, "\n【1】买入时机对比 (h1_open / h1_close / h2_open 作为买入价, 看 T+1收盘收益)")
    for buy_key, label in [('h1_open', 'H1开盘买入'), ('h1_close', 'H1收盘买入'), ('h2_open', 'H2开盘买入')]:
        rets = [r['timing'][buy_key] for r in records if r['timing'].get(buy_key) is not None]
        if rets:
            win = sum(1 for x in rets if x > 0)
            log_line(fh, f"  {label:<10}: 样本{len(rets):>4}  均值T+1收盘{sum(rets)/len(rets):>+6.2f}%  胜率{win/len(rets)*100:>5.1f}%")
        else:
            log_line(fh, f"  {label:<10}: 无数据")

    # ---- 2. 止盈触及率 ----
    log_line(fh, "\n【2】止盈触及率 (买入价=h1_open, 观察 T+1~T+%d hour高点)" % FUTURE_DAYS)
    for tp in [3, 5, 8, 10, 15]:
        hit = sum(1 for r in records if r['max_up'] is not None and r['max_up'] >= tp)
        valid = sum(1 for r in records if r['max_up'] is not None)
        if valid:
            log_line(fh, f"  +{tp:>2}% : {hit/valid*100:>5.1f}%  ({hit}/{valid})")

    # ---- 3. 最大回撤分布 ----
    log_line(fh, "\n【3】最大回撤分布 (T+1~T+%d hour低点 vs 买入价)" % FUTURE_DAYS)
    downs = [r['max_down'] for r in records if r['max_down'] is not None]
    if downs:
        downs_sorted = sorted(downs)
        buckets = [(-3, '0~-3%'), (-5, '-3~-5%'), (-8, '-5~-8%'), (-10, '-8~-10%'), (-100, '<-10%')]
        prev = 0
        for thr, lbl in buckets:
            cnt = sum(1 for x in downs if thr < x <= prev)
            log_line(fh, f"  {lbl:<8}: {cnt/len(downs)*100:>5.1f}%  ({cnt}/{len(downs)})")
            prev = thr
        mid = downs_sorted[len(downs_sorted)//2]
        log_line(fh, f"  中位最大回撤: {mid:+.2f}%  平均: {sum(downs)/len(downs):+.2f}%")

    # ---- 4. 配置对比 ----
    log_line(fh, "\n【4】止盈止损配置对比 (买入价=h1_open, T+1起监控)")
    log_line(fh, f"  {'配置':<15}|{'样本':>5}|{'均收益':>8}|{'胜率':>7}|{'TP占':>6}|{'SL占':>6}|{'平仓占':>7}")
    log_line(fh, f"  {'-'*15}+{'-'*5}+{'-'*8}+{'-'*7}+{'-'*6}+{'-'*6}+{'-'*7}")
    config_stats = {}
    for name, tp, sl, hold in CONFIGS:
        rets = []
        reasons = defaultdict(int)
        for r in records:
            res = r['sims'].get(name)
            if res is None:
                continue
            rets.append(res[0])
            reasons[res[1]] += 1
        if rets:
            win = sum(1 for x in rets if x > 0)
            m = len(rets)
            avg = sum(rets) / m
            config_stats[name] = (avg, win / m * 100, m)
            log_line(fh, f"  {name:<15}|{m:>5}|{avg:>+7.2f}%|{win/m*100:>6.1f}%|"
                         f"{reasons['TP']/m*100:>5.0f}%|{reasons['SL']/m*100:>5.0f}%|{reasons['CLOSE']/m*100:>6.0f}%")

    # ---- 5. 逐年稳定性 (对最优配置) ----
    if config_stats:
        best_name = max(config_stats, key=lambda k: config_stats[k][0])
        log_line(fh, f"\n【5】逐年稳定性 (最优配置: {best_name})")
        by_year = defaultdict(list)
        for r in records:
            res = r['sims'].get(best_name)
            if res is None:
                continue
            by_year[r['today'][:4]].append(res[0])
        log_line(fh, f"  {'年份':<6}|{'样本':>5}|{'均收益':>8}|{'胜率':>7}")
        log_line(fh, f"  {'-'*6}+{'-'*5}+{'-'*8}+{'-'*7}")
        for yr in sorted(by_year):
            ys = by_year[yr]
            win = sum(1 for x in ys if x > 0)
            log_line(fh, f"  {yr:<6}|{len(ys):>5}|{sum(ys)/len(ys):>+7.2f}%|{win/len(ys)*100:>6.1f}%")

    # ---- 6. 最终结论 ----
    log_line(fh, "\n【6】最终结论")
    if config_stats:
        ranked = sorted(config_stats.items(), key=lambda kv: kv[1][0], reverse=True)
        log_line(fh, "  按均收益排序 Top5 配置:")
        for name, (avg, wr, m) in ranked[:5]:
            # 估算月化: 单笔均收益 * 月内可开仓次数(假设持仓天数, 20交易日/月)
            hold = dict((c[0], c[3]) for c in CONFIGS)[name]
            monthly = avg * (20.0 / hold)
            flag = "达标" if (monthly >= 10.0 and wr >= 55.0) else ""
            log_line(fh, f"    {name:<15} 均收益{avg:>+6.2f}%  胜率{wr:>5.1f}%  "
                         f"估月化{monthly:>+6.1f}%  {flag}")
        best_name, (best_avg, best_wr, best_m) = ranked[0]
        best_hold = dict((c[0], c[3]) for c in CONFIGS)[best_name]
        best_monthly = best_avg * (20.0 / best_hold)
        log_line(fh, f"\n  >> 推荐配置: {best_name}")
        log_line(fh, f"     单笔均收益 {best_avg:+.2f}%, 胜率 {best_wr:.1f}%, 估月化 {best_monthly:+.1f}%")
        target = (best_monthly >= 10.0 and best_wr >= 55.0)
        log_line(fh, f"     是否达标(月化10%+/胜率55%+): {'是 ✓' if target else '否 ✗'}")


def main():
    if len(sys.argv) < 2:
        print("用法: python3 research_dragon_pullback_r2.py 2025-04 | 2025 | all")
        sys.exit(1)

    arg = sys.argv[1]
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    all_days = get_all_trading_days(cur)

    if arg == 'all':
        target_days = [d for d in all_days if '2021-01-01' <= d <= '2026-12-31']
        title = "全量 2021-2026"
    elif len(arg) == 4:  # 年
        target_days = get_trading_days_like(cur, arg)
        title = f"{arg} 全年"
    elif len(arg) == 7 and arg[4] == '-':  # 月
        target_days = get_trading_days_like(cur, arg)
        title = f"{arg} 单月"
    else:
        print(f"参数错误: {arg}")
        sys.exit(1)

    if not target_days:
        print(f"未找到 {arg} 的交易日数据")
        sys.exit(1)

    fh = open(LOG_PATH, 'w', encoding='utf-8')
    log_line(fh, "=" * 80)
    log_line(fh, f"龙回头(创业板 200-700亿) 止盈止损研究 - {title}")
    log_line(fh, f"参数: 新高回望{NEW_HIGH_LOOKBACK}日 | 新高窗口T-{NH_WINDOW_MIN}~T-{NH_WINDOW_MAX} | "
                 f"回调<{PULLBACK_RATIO} | 跳空>={GAP_UP_MIN}% | 市值{MKT_CAP_MIN/1e8:.0f}~{MKT_CAP_MAX/1e8:.0f}亿")
    log_line(fh, f"交易日数: {len(target_days)}  (数据库共 {len(all_days)} 交易日)")
    log_line(fh, "=" * 80)

    records = []
    total = 0
    detail_budget = 200  # 明细打印总预算 (避免日志过大)

    for today in target_days:
        try:
            today_idx = all_days.index(today)
        except ValueError:
            continue
        cands = find_candidates(cur, today, today_idx, all_days)
        if not cands:
            continue
        total += len(cands)
        log_line(fh, f"\n{'='*60}")
        log_line(fh, f"===== {today}  候选 {len(cands)} 只 =====")

        for i, c in enumerate(cands):
            h1 = c['hour_today']['h1']
            h2 = c['hour_today']['h2']
            h1_open, h1_close = h1[0], h1[3]
            h2_open = h2[0]
            buy_price = h1_open if (h1_open and h1_open > 0) else c['t_open']

            # 打印明细 (前 MAX_DETAIL_PER_DAY 只, 且总预算内)
            if i < MAX_DETAIL_PER_DAY and detail_budget > 0:
                detail_budget -= 1
                log_line(fh, f"\n--- {c['code']} ({c['code_name'] or 'N/A'}) ---")
                log_line(fh, f"  新高日 {c['new_high_day']} close={c['new_high_close']:.2f} -> "
                             f"昨收 {c['yesterday_close']:.2f} (回调 -{c['pullback_pct']:.1f}%) "
                             f"| 回调判据: {'价<95%' if c['cond_a'] else ''}{' 3日≥2阴' if c['cond_b'] else ''}")
                log_line(fh, f"  今日 open={c['t_open']:.2f} preclose={c['t_preclose']:.2f} "
                             f"跳空 +{c['gap']:.2f}% | 流通市值 {c['mkt_cap']/1e8:.0f}亿 | 买入价(h1open) {buy_price:.2f}")
                prev5 = max(0, today_idx - 5)
                nxt5 = min(len(all_days), today_idx + FUTURE_DAYS + 1)
                window_days = all_days[prev5:nxt5]
                hour_rows = get_hour_window(cur, c['code'], window_days)
                if hour_rows:
                    format_hour_table(fh, hour_rows, buy_price, today)

            # 构建未来路径 & 模拟各配置
            path = build_future_hour_path(cur, c['code'], today, all_days)
            sims = {}
            for name, tp, sl, hold in CONFIGS:
                res = simulate_config(buy_price, path, tp, sl, hold)
                if res is not None:
                    sims[name] = res

            # 买入时机: 用 T+1 收盘收益衡量
            timing = {}
            # T+1 收盘
            t1_close = None
            if path:
                first_day = path[0][0]
                closes = [p[4] for p in path if p[0] == first_day]
                if closes:
                    t1_close = closes[-1]
            for key, bp in [('h1_open', h1_open), ('h1_close', h1_close), ('h2_open', h2_open)]:
                if bp and bp > 0 and t1_close:
                    timing[key] = (t1_close - bp) / bp * 100

            # 最大涨幅/回撤 (基于 h1_open 买入价)
            max_up = None
            max_down = None
            if path and buy_price and buy_price > 0:
                highs = [p[2] for p in path]
                lows = [p[3] for p in path]
                if highs:
                    max_up = (max(highs) - buy_price) / buy_price * 100
                if lows:
                    max_down = (min(lows) - buy_price) / buy_price * 100

            records.append({
                'today': today, 'code': c['code'], 'buy_price': buy_price,
                'sims': sims, 'timing': timing, 'max_up': max_up, 'max_down': max_down,
            })

    log_line(fh, f"\n\n总候选样本: {total}")
    analyze(fh, records)
    log_line(fh, "\n研究完成. 日志: " + LOG_PATH)
    fh.close()
    conn.close()


if __name__ == '__main__':
    main()
