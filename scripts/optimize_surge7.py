#!/usr/bin/env python3
"""冲高后第二波策略 - 大规模参数网格搜索 v2
核心优化: 预计算每日候选股列表 + 极简回测内循环
"""
import sys, sqlite3, math, time, itertools, gc
from collections import defaultdict
from datetime import datetime, timedelta

sys.path.insert(0, '/home/AIWealth/scripts')
from compliance_utils import safe_float, is_limit_up, is_limit_down, is_one_word_board, is_st

DB_PATH = '/home/AIWealth/data/stocks.db'
INITIAL_CAPITAL = 1_000_000.0

# ============ 参数网格 ============
PARAM_GRID = {
    'surge_threshold': [5.0, 7.0, 9.0, 11.0],
    'fallback_ratio': [0.5, 0.6, 0.7, 0.8],
    'tp_pct': [3.0, 5.0, 8.0, 10.0, 15.0, 999.0],
    'sl_pct': [-2.0, -3.0, -5.0, -7.0, -10.0, -999.0],
    'max_hold_days': [2, 3, 5, 7, 10, 15, 20],
    'buy_hour': [1, 2, 3],
    'n_slots': [1, 2, 3],
    'wait_days': [0, 1, 2, 3],
    'min_turn': [0, 3.0, 5.0, 8.0],
    'market_filter': [False, True],
}

def count_combos():
    total = 1
    for v in PARAM_GRID.values():
        total *= len(v)
    return total

def sf(v):
    if v is None: return 0.0
    try:
        f = float(v)
        return 0.0 if (math.isnan(f) or math.isinf(f)) else f
    except: return 0.0


class DataEngine:
    """数据引擎：预加载+预计算候选"""

    def __init__(self):
        self.all_dates = []
        self.date_idx = {}
        self.bt_dates = []
        self.market_data = {}
        # 每日每股数据: {date_idx: {code: (preclose, h1o,h1h,h1l,h1c, h2o..h4c, open_rate, turn)}}
        # 使用tuple而非dict进一步省内存加速
        self.day_data = {}  # {date_idx: {code: tuple}}
        # 预计算的信号候选: {date_idx: [(code, high_rate, close_rate, turn)]}
        self.signal_cands = {}

    def load(self, start_date, end_date):
        conn = sqlite3.connect(DB_PATH)
        t0 = time.time()
        lo = (datetime.strptime(start_date, "%Y-%m-%d") - timedelta(days=60)).strftime("%Y-%m-%d")
        cur = conn.cursor()
        cur.execute(
            "SELECT DISTINCT date FROM stock_kline WHERE date>=? AND date<=? ORDER BY date",
            (lo, end_date))
        self.all_dates = [r[0] for r in cur.fetchall()]
        self.date_idx = {d: i for i, d in enumerate(self.all_dates)}
        self.bt_dates = [d for d in self.all_dates if start_date <= d <= end_date]
        print(f"  交易日: {len(self.all_dates)} | 回测区间: {len(self.bt_dates)}天")
        sys.stdout.flush()

        # 加载数据 - tuple格式: 
        # (preclose, h1o,h1h,h1l,h1c, h2o,h2h,h2l,h2c, h3o,h3h,h3l,h3c, h4o,h4h,h4l,h4c, open_rate, turn, high_rate, close_rate, high, low, open, close, isST, code_name)
        # index:  0     1  2  3  4    5  6  7  8    9 10 11 12   13 14 15 16   17        18   19        20         21  22  23   24    25     26
        fields = (
            'date, code, preclose, '
            'hour1_open, hour1_high, hour1_low, hour1_close, '
            'hour2_open, hour2_high, hour2_low, hour2_close, '
            'hour3_open, hour3_high, hour3_low, hour3_close, '
            'hour4_open, hour4_high, hour4_low, hour4_close, '
            'open_rate, turn, high_rate, close_rate, high, low, open, close, isST, code_name'
        )
        total_rows = 0
        years_needed = sorted(set(d[:4] for d in self.all_dates))
        for year in years_needed:
            y_start = f"{year}-01-01"
            y_end = f"{year}-12-31"
            actual_start = max(lo, y_start)
            actual_end = min(end_date, y_end)
            if actual_start > actual_end: continue
            cur.execute(f"SELECT {fields} FROM stock_kline WHERE date>=? AND date<=?",
                        (actual_start, actual_end))
            count = 0
            for row in cur:
                d, code = row[0], row[1]
                di = self.date_idx.get(d)
                if di is None: continue
                # tuple: (preclose, h1o..h4c, open_rate, turn, high_rate, close_rate, high, low, open, close, isST, code_name)
                t = (row[2], row[3],row[4],row[5],row[6], row[7],row[8],row[9],row[10],
                     row[11],row[12],row[13],row[14], row[15],row[16],row[17],row[18],
                     row[19], row[20], row[21], row[22], row[23], row[24], row[25], row[26], row[27], row[28])
                if di not in self.day_data:
                    self.day_data[di] = {}
                self.day_data[di][code] = t
                count += 1
            total_rows += count
            print(f"    年份{year}: {count:,}行")
            sys.stdout.flush()

        # 预计算信号候选 (surge >= 5.0的最低阈值, 不封板, 非ST, 非北交所)
        print("  预计算信号候选...")
        sys.stdout.flush()
        for di, stocks in self.day_data.items():
            cands = []
            for code, t in stocks.items():
                if code.startswith('bj.'): continue
                isST = t[25]
                code_name = t[26] or ''
                if isST == 1 or 'ST' in code_name.upper(): continue
                high_rate = sf(t[19])
                if high_rate < 5.0: continue  # 最低阈值
                close_rate = sf(t[20])
                preclose = sf(t[0])
                close_p = sf(t[24])
                high_p = sf(t[21])
                low_p = sf(t[22])
                open_p = sf(t[23])
                if preclose <= 0: continue
                # 排除涨停封板
                if close_p > 0 and is_limit_up(code, close_p, preclose): continue
                # 排除一字板
                if is_one_word_board(open_p, high_p, low_p, close_p): continue
                turn = sf(t[18])
                cands.append((code, high_rate, close_rate, turn))
            if cands:
                # 按high_rate降序排
                cands.sort(key=lambda x: -x[1])
                self.signal_cands[di] = cands

        # 大盘数据
        cur.execute(
            "SELECT date, close_rate FROM index_kline WHERE code='sh.000001' AND date>=? AND date<=?",
            (lo, end_date))
        for row in cur:
            self.market_data[row[0]] = sf(row[1])
        conn.close()
        elapsed = time.time() - t0
        print(f"  数据加载完成: {total_rows:,}行 | 信号日: {len(self.signal_cands)}天 | 耗时: {elapsed:.1f}s")
        sys.stdout.flush()


def run_backtest(params, engine, bt_dates):
    """极速回测 - 使用预计算候选"""
    surge_th = params['surge_threshold']
    fallback_r = params['fallback_ratio']
    tp = params['tp_pct']
    sl = params['sl_pct']
    max_hold = params['max_hold_days']
    buy_hour = params['buy_hour']
    n_slots = params['n_slots']
    wait_days = params['wait_days']
    min_turn = params['min_turn']
    mkt_filter = params['market_filter']

    # hour数据在tuple中的offset: h1=1..4, h2=5..8, h3=9..12, h4=13..16
    bh_off = (buy_hour - 1) * 4 + 1  # buy hour open offset

    cash = INITIAL_CAPITAL
    positions = []  # [(code, buy_price, buy_date_idx, shares)]
    wins = 0; losses = 0; total_pnl = 0.0; trade_count = 0
    peak_equity = INITIAL_CAPITAL; max_dd = 0.0

    date_idx = engine.date_idx
    all_dates = engine.all_dates
    day_data = engine.day_data
    signal_cands = engine.signal_cands
    market_data = engine.market_data

    tp_active = tp < 900
    sl_active = sl > -900

    for today in bt_dates:
        today_idx = date_idx.get(today, -1)
        today_stocks = day_data.get(today_idx)
        if not today_stocks:
            continue

        # ====== 卖出 ======
        if positions:
            survived = []
            for pos_code, pos_bp, pos_bd_idx, pos_shares in positions:
                if pos_bd_idx == today_idx:
                    survived.append((pos_code, pos_bp, pos_bd_idx, pos_shares))
                    continue
                hold_d = today_idx - pos_bd_idx
                t = today_stocks.get(pos_code)
                if not t:
                    survived.append((pos_code, pos_bp, pos_bd_idx, pos_shares))
                    continue
                preclose = sf(t[0])
                sold = False; sell_price = 0.0
                for h in range(4):
                    off = h * 4 + 1
                    hc = sf(t[off+3])
                    if hc <= 0: continue
                    ho = sf(t[off]); hh = sf(t[off+1]); hl = sf(t[off+2])
                    if ho > 0 and hh > 0 and hl > 0 and (hh - hl) < 0.01:
                        if preclose > 0 and is_limit_down(pos_code, hc, preclose):
                            continue
                    pnl_pct = (hc / pos_bp - 1.0) * 100.0
                    if tp_active and pnl_pct >= tp:
                        sold = True; sell_price = hc; break
                    elif sl_active and pnl_pct <= sl:
                        sold = True; sell_price = hc; break
                    elif h == 3 and hold_d >= max_hold:
                        sold = True; sell_price = hc; break
                if sold:
                    pnl = (sell_price / pos_bp - 1.0) * 100.0
                    cash += pos_shares * sell_price
                    trade_count += 1; total_pnl += pnl
                    if pnl > 0: wins += 1
                    else: losses += 1
                else:
                    survived.append((pos_code, pos_bp, pos_bd_idx, pos_shares))
            positions = survived

        # ====== 买入 ======
        if len(positions) < n_slots:
            if mkt_filter:
                mkt_rate = market_data.get(today, 0.0)
                if mkt_rate < -1.0:
                    goto_equity = True
                else:
                    goto_equity = False
            else:
                goto_equity = False

            if not goto_equity:
                signal_offset = 1 + wait_days
                sig_di = today_idx - signal_offset
                if sig_di >= 0:
                    cands = signal_cands.get(sig_di)
                    if cands:
                        held_codes = {p[0] for p in positions} if positions else set()
                        # 如果wait_days>0, 需要检查today数据中open_rate<=0
                        check_callback = wait_days > 0
                        for code, hr, cr, turn in cands:
                            if len(positions) >= n_slots: break
                            if code in held_codes: continue
                            if hr < surge_th: continue
                            if cr >= hr * fallback_r: continue
                            if min_turn > 0 and turn < min_turn: continue

                            buy_t = today_stocks.get(code)
                            if not buy_t: continue
                            # ST再验
                            if buy_t[25] == 1: continue
                            cn2 = buy_t[26] or ''
                            if 'ST' in cn2.upper(): continue

                            if check_callback:
                                opr = sf(buy_t[17])  # open_rate
                                if opr > 0: continue

                            # 买入hour数据
                            h_o = sf(buy_t[bh_off])
                            h_h = sf(buy_t[bh_off+1])
                            h_l = sf(buy_t[bh_off+2])
                            h_c = sf(buy_t[bh_off+3])
                            if h_o <= 0: continue
                            if is_one_word_board(h_o, h_h, h_l, h_c): continue
                            preclose_b = sf(buy_t[0])
                            if preclose_b > 0 and is_limit_up(code, h_o, preclose_b): continue

                            free_slots = n_slots - len(positions)
                            alloc = cash / free_slots if free_slots > 0 else cash
                            shares = int(alloc / h_o // 100) * 100
                            if shares <= 0: continue
                            cost = shares * h_o
                            if cost > cash: continue
                            cash -= cost
                            positions.append((code, h_o, today_idx, shares))
                            held_codes.add(code)

        # 日终权益
        equity = cash
        for pc, pbp, _, psh in positions:
            t2 = today_stocks.get(pc)
            if t2:
                p = sf(t2[16]) or sf(t2[24]) or pbp  # h4c or close
            else:
                p = pbp
            equity += psh * p
        if equity > peak_equity: peak_equity = equity
        dd = (peak_equity - equity) / peak_equity * 100.0 if peak_equity > 0 else 0
        if dd > max_dd: max_dd = dd

    # 强制清仓
    final_equity = cash
    if positions and bt_dates:
        last_idx = date_idx.get(bt_dates[-1], -1)
        last_stocks = day_data.get(last_idx, {})
        for pos_code, pos_bp, _, pos_shares in positions:
            t2 = last_stocks.get(pos_code)
            sp = (sf(t2[16]) or sf(t2[24]) or pos_bp) if t2 else pos_bp
            pnl = (sp / pos_bp - 1.0) * 100.0
            final_equity += pos_shares * sp
            trade_count += 1; total_pnl += pnl
            if pnl > 0: wins += 1
            else: losses += 1

    total_return = (final_equity / INITIAL_CAPITAL - 1.0) * 100.0
    if len(bt_dates) >= 2:
        d0 = datetime.strptime(bt_dates[0], "%Y-%m-%d")
        d1 = datetime.strptime(bt_dates[-1], "%Y-%m-%d")
        years = max(0.1, (d1 - d0).days / 365.25)
    else:
        years = 1.0
    cagr = ((final_equity / INITIAL_CAPITAL) ** (1.0 / years) - 1.0) * 100.0 if final_equity > 0 else -100.0
    win_rate = wins / trade_count * 100.0 if trade_count > 0 else 0.0
    avg_pnl = total_pnl / trade_count if trade_count > 0 else 0.0
    return (total_return, cagr, win_rate, max_dd, trade_count, avg_pnl)


def main():
    total_combos = count_combos()
    print("=" * 70)
    print("冲高后第二波策略 - 大规模参数网格搜索 v2")
    print("=" * 70)
    print(f"参数空间: {total_combos:,} 组合")
    print(f"阶段1: 训练集 2022-2024 快速筛选 -> Top 50")
    print(f"阶段2: 验证集 2021-2026 全量验证 -> Top 10")
    print(f"启动时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print("=" * 70)
    sys.stdout.flush()

    # ===== 阶段1 =====
    print("\n[阶段1] 加载训练集数据 (2022-2024)...")
    sys.stdout.flush()
    engine = DataEngine()
    engine.load('2022-01-01', '2024-12-31')
    train_dates = engine.bt_dates

    print(f"\n[阶段1] 开始网格搜索 ({total_combos:,} 组合)...")
    sys.stdout.flush()

    keys = list(PARAM_GRID.keys())
    values = [PARAM_GRID[k] for k in keys]
    results_train = []
    t0 = time.time()
    done = 0

    for combo in itertools.product(*values):
        params = dict(zip(keys, combo))
        done += 1
        if done % 5000 == 0:
            elapsed = time.time() - t0
            speed = done / elapsed
            eta = (total_combos - done) / speed if speed > 0 else 0
            best_cagr = max((r[1] for r in results_train), default=-999)
            print(f"  进度: {done:,}/{total_combos:,} ({done/total_combos*100:.1f}%) "
                  f"| {speed:.0f}组合/s | ETA: {eta/3600:.1f}h "
                  f"| best CAGR: {best_cagr:+.1f}% | 有效: {len(results_train)}")
            sys.stdout.flush()

        ret, cagr, wr, mdd, tc, avg = run_backtest(params, engine, train_dates)
        if tc >= 10:
            results_train.append((ret, cagr, wr, mdd, tc, avg, params))

    elapsed_train = time.time() - t0
    print(f"\n[阶段1] 完成! 耗时: {elapsed_train:.1f}s ({elapsed_train/3600:.2f}h)")
    print(f"  有效组合: {len(results_train):,}")
    sys.stdout.flush()

    results_train.sort(key=lambda x: -x[1])
    top50 = results_train[:50]

    print(f"\n  训练集Top 10:")
    for rank, (ret, cagr, wr, mdd, tc, avg, p) in enumerate(top50[:10], 1):
        tp_s = f"{p['tp_pct']}%" if p['tp_pct'] < 900 else "NoTP"
        sl_s = f"{p['sl_pct']}%" if p['sl_pct'] > -900 else "NoSL"
        print(f"  #{rank:2d}: CAGR={cagr:+.1f}% WR={wr:.1f}% MDD={mdd:.1f}% T={tc} "
              f"| surge={p['surge_threshold']} fb={p['fallback_ratio']} tp={tp_s} sl={sl_s} "
              f"hold={p['max_hold_days']}d bh={p['buy_hour']} slots={p['n_slots']} "
              f"wait={p['wait_days']} turn>{p['min_turn']} mkt={p['market_filter']}")
    sys.stdout.flush()

    # 释放
    del engine; gc.collect()

    # ===== 阶段2 =====
    print(f"\n{'='*70}")
    print("[阶段2] 加载全量数据 (2021-2026)...")
    sys.stdout.flush()
    engine_full = DataEngine()
    engine_full.load('2021-01-01', '2026-06-30')
    full_dates = engine_full.bt_dates

    print(f"\n[阶段2] 验证Top 50 (全量6年)...")
    sys.stdout.flush()
    results_full = []
    t1 = time.time()
    for i, (_, _, _, _, _, _, params) in enumerate(top50):
        ret, cagr, wr, mdd, tc, avg = run_backtest(params, engine_full, full_dates)
        results_full.append((ret, cagr, wr, mdd, tc, avg, params))
        if (i + 1) % 10 == 0:
            print(f"  验证: {i+1}/50")
            sys.stdout.flush()

    elapsed_full = time.time() - t1
    print(f"\n[阶段2] 完成! 耗时: {elapsed_full:.1f}s")

    results_full.sort(key=lambda x: -x[1])
    top10 = results_full[:10]

    # ===== 最终输出 =====
    print(f"\n{'='*70}")
    print(f"===== 冲高后第二波策略 参数优化最终结果 =====")
    print(f"{'='*70}")
    print(f"搜索空间: {total_combos:,} 组合")
    print(f"训练集: 2022-2024 | 验证集: 2021-2026")
    print(f"总耗时: {time.time()-t0:.1f}s ({(time.time()-t0)/3600:.2f}h)")
    print(f"\nTop 10 参数组合（按6年CAGR排序）:")
    print(f"{'~'*70}")
    for rank, (ret, cagr, wr, mdd, tc, avg, p) in enumerate(top10, 1):
        calmar = cagr / mdd if mdd > 1 else 0
        tp_s = f"{p['tp_pct']}%" if p['tp_pct'] < 900 else "不止盈"
        sl_s = f"{p['sl_pct']}%" if p['sl_pct'] > -900 else "不止损"
        print(f"#{rank:2d}: CAGR={cagr:+.1f}% WR={wr:.1f}% MDD=-{mdd:.1f}% "
              f"Trades={tc} AvgPnl={avg:+.2f}% Calmar={calmar:.2f}")
        print(f"    参数: surge={p['surge_threshold']}% fallback={p['fallback_ratio']} "
              f"tp={tp_s} sl={sl_s} hold={p['max_hold_days']}days "
              f"buy_h={p['buy_hour']} slots={p['n_slots']} "
              f"wait={p['wait_days']} turn>{p['min_turn']} market_filter={p['market_filter']}")

    print(f"\n{'~'*70}")
    print("参数维度分析（Top 10中各参数值出现频率）:")
    for key in PARAM_GRID.keys():
        freq = defaultdict(int)
        for _, _, _, _, _, _, p in top10:
            freq[p[key]] += 1
        sorted_freq = sorted(freq.items(), key=lambda x: -x[1])
        vals = [f"{v}({c})" for v, c in sorted_freq]
        print(f"  {key}: {', '.join(vals)}")

    print(f"\n完成时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"总耗时: {time.time()-t0:.1f}s ({(time.time()-t0)/3600:.2f}h)")
    sys.stdout.flush()


if __name__ == '__main__':
    main()
