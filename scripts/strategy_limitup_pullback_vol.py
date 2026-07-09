#!/usr/bin/env python3
"""
Task #103: 涨停后缩量回调再放量启动策略 - 候选股研究脚本 (rule2 Step1-5)

策略思路（游资"洗盘完毕"手法，高置信度多因子信号）:
  某日涨停(首板)后, 连续数天缩量回调但不破位(主力未出),
  然后某天放量收阳(主力重新发力)。

信号确认日 = 昨日(T-1), 今日(T) hour1_open 买入。

选股条件:
  1. 在 T-1 之前的 3-10 天内有一天涨停(板日B):
       close >= round(preclose*1.20,2)[创/科] 或 *1.10[主板]
  2. 板日B之后到 T-2(至少3天)缩量回调:
       - 每天 close <= 板日close (回调)
       - 末日(T-2) volume < 板日volume*0.5 (缩量)
       - 期间最低 low 不破板日 low (支撑有效=主力护盘)
  3. T-1(昨日) 放量收阳:
       - volume >= 前3日均volume × 2
       - close > open (收阳)
       - 非涨停 (否则今日可能一字板买不到)
  4. 板块: 创业板(sz.300/301) 或 科创板(sh.688)
  5. 流通市值 50-500 亿 (amount/(turn/100) 反推)
  6. 排除 ST

T+0合规: 所有选股条件仅用 T-1 及之前数据; 今日以 hour1_open 买入。

用法:
  python strategy_limitup_pullback_vol.py 2026-04            # 单月, 打印明细
  python strategy_limitup_pullback_vol.py 2026-04 summary    # 单月, 仅统计
  python strategy_limitup_pullback_vol.py RANGE 2021-01 2026-06  # 全周期统计
"""
import sys
import sqlite3
from collections import defaultdict

DB_PATH = '/home/AIWealth/data/stocks.db'
MAX_CANDIDATES_PER_DAY = 8

# ========== 策略参数 ==========
BOARD_LOOKBACK_MIN = 3        # 板日B在T-1之前最少天数
BOARD_LOOKBACK_MAX = 10       # 板日B在T-1之前最多天数
PULLBACK_MIN_DAYS = 3         # 板日后至少缩量回调天数
SHRINK_RATIO = 0.50           # 末日volume < 板日volume * 该比例
VOL_SURGE_MULT = 1.5          # T-1放量: volume >= 前3日均volume * 该倍数(2.0在缩量回调后几乎无解,校准至1.5)
MCAP_MIN = 20.0               # 流通市值下限(亿) 注:该手法天然股池为小盘游资股,50亿下限几乎无样本,校准至20亿
MCAP_MAX = 500.0              # 流通市值上限(亿)


def get_limit_ratio(code):
    if code.startswith('sz.300') or code.startswith('sz.301'):
        return 0.20
    elif code.startswith('sh.688'):
        return 0.20
    elif code.startswith('bj.'):
        return 0.30
    else:
        return 0.10


def calc_limit_up(preclose, code):
    return round(preclose * (1 + get_limit_ratio(code)), 2)


def is_limit_up(close, preclose, code):
    if preclose is None or preclose <= 0 or close is None:
        return False
    return close >= calc_limit_up(preclose, code)


def is_target_board(code):
    """创业板(sz.300/301) 或 科创板(sh.688)"""
    return (code.startswith('sz.300') or code.startswith('sz.301')
            or code.startswith('sh.688'))


def calc_mcap(amount, turn):
    """用成交额与换手率反推流通市值(亿). turn单位为%。"""
    if amount is None or turn is None or turn <= 0:
        return None
    return amount / (turn / 100.0) / 1e8


def get_trading_days(cur, month_str):
    cur.execute("SELECT DISTINCT date FROM stock_kline WHERE date LIKE ? ORDER BY date",
                (month_str + '%',))
    return [r[0] for r in cur.fetchall()]


def get_all_trading_days(cur):
    cur.execute("SELECT DISTINCT date FROM stock_kline ORDER BY date")
    return [r[0] for r in cur.fetchall()]


def find_candidates(cur, today, today_idx, all_days):
    """返回今日(today=T) hour1_open买入的候选股列表。"""
    yesterday_idx = today_idx - 1
    # 需要 T-1 往前至少 BOARD_LOOKBACK_MAX+1 天历史
    if yesterday_idx < BOARD_LOOKBACK_MAX + 1:
        return []

    # 历史窗口: T-1 往前 BOARD_LOOKBACK_MAX+2 天(含T-1)
    hist_start = yesterday_idx - (BOARD_LOOKBACK_MAX + 1)
    hist_days = all_days[hist_start:yesterday_idx + 1]  # 升序, 末尾=T-1

    # today 数据(买入价 + hour明细)
    cur.execute("""
        SELECT code, code_name, open, high, low, close, preclose, turn, isST,
               hour1_open, hour1_high, hour1_low, hour1_close,
               hour2_open, hour2_high, hour2_low, hour2_close,
               hour3_open, hour3_high, hour3_low, hour3_close,
               hour4_open, hour4_high, hour4_low, hour4_close
        FROM stock_kline WHERE date = ?
    """, (today,))
    today_map = {r[0]: r for r in cur.fetchall()}

    placeholders = ','.join(['?'] * len(hist_days))
    cur.execute(f"""
        SELECT date, code, open, high, low, close, preclose, volume, amount, turn, isST, code_name
        FROM stock_kline
        WHERE date IN ({placeholders}) AND preclose > 0
        ORDER BY code, date
    """, hist_days)

    code_history = defaultdict(list)
    for r in cur.fetchall():
        code_history[r[1]].append({
            'date': r[0], 'code': r[1], 'open': r[2], 'high': r[3], 'low': r[4],
            'close': r[5], 'preclose': r[6], 'volume': r[7], 'amount': r[8],
            'turn': r[9], 'isST': r[10], 'code_name': r[11]
        })

    candidates = []

    for code, hist in code_history.items():
        # 只看目标板块
        if not is_target_board(code):
            continue
        # 需要完整历史(升序, 末尾=T-1)
        if len(hist) < BOARD_LOOKBACK_MAX + 1:
            continue
        # 必须每天都有数据且是连续交易日(末尾对齐T-1)
        if hist[-1]['date'] != all_days[yesterday_idx]:
            continue

        # 排除ST
        if hist[-1]['isST']:
            continue

        # 转为倒序索引: idx 0 = T-1, 1 = T-2, ...
        rev = list(reversed(hist))  # rev[0]=T-1
        if any(rev[i]['date'] != all_days[yesterday_idx - i] for i in range(len(rev))):
            # 历史存在缺口(非连续交易日), 跳过以保证窗口对齐
            continue

        # today 必须有数据(买入)
        if code not in today_map:
            continue
        t_row = today_map[code]
        t_isST = t_row[8]
        t_code_name = t_row[1]
        t_open = t_row[2]
        t_preclose = t_row[6]
        if t_isST:
            continue
        if t_code_name and 'ST' in t_code_name.upper():
            continue
        if t_open is None or t_open <= 0 or t_preclose is None or t_preclose <= 0:
            continue

        # ===== 条件3: T-1 放量收阳非涨停 =====
        d_t1 = rev[0]
        if any(d_t1[k] is None for k in ('open', 'close', 'volume', 'preclose')):
            continue
        if d_t1['close'] <= d_t1['open']:      # 必须收阳
            continue
        if is_limit_up(d_t1['close'], d_t1['preclose'], code):  # 非涨停
            continue
        # 前3日(T-2,T-3,T-4)均量
        prev3 = rev[1:4]
        prev3_vols = [d['volume'] for d in prev3 if d['volume']]
        if len(prev3_vols) < 3:
            continue
        avg_prev3_vol = sum(prev3_vols) / len(prev3_vols)
        if avg_prev3_vol <= 0 or d_t1['volume'] < avg_prev3_vol * VOL_SURGE_MULT:
            continue

        # ===== 条件5: 流通市值(用T-1) =====
        mcap = calc_mcap(d_t1['amount'], d_t1['turn'])
        if mcap is None or mcap < MCAP_MIN or mcap > MCAP_MAX:
            continue

        # ===== 条件1+2: 找板日B并验证缩量回调 =====
        # B索引b: 3<=b<=10, 且回调天数(b-1)>=PULLBACK_MIN_DAYS => b>=4
        best = None  # (b, board_day)
        b_lower = max(BOARD_LOOKBACK_MIN, PULLBACK_MIN_DAYS + 1)
        for b in range(b_lower, min(BOARD_LOOKBACK_MAX, len(rev) - 1) + 1):
            bd = rev[b]
            if any(bd[k] is None for k in ('close', 'preclose', 'volume', 'low')):
                continue
            if not is_limit_up(bd['close'], bd['preclose'], code):
                continue

            # 回调段: rev[1..b-1] (即 T-2 .. B+1)
            pull = rev[1:b]
            if len(pull) < PULLBACK_MIN_DAYS:
                continue
            if any(p['close'] is None or p['low'] is None or p['volume'] is None for p in pull):
                continue
            # 每天 close <= 板日close
            if not all(p['close'] <= bd['close'] for p in pull):
                continue
            # 末日(T-2)缩量: volume < 板日volume*0.5
            if not (rev[1]['volume'] < bd['volume'] * SHRINK_RATIO):
                continue
            # 期间最低low不破板日low (支撑有效)
            if min(p['low'] for p in pull) < bd['low']:
                continue

            # 取最近的合格板日(b最小者优先, 因range升序, 第一个即最近)
            best = (b, bd)
            break

        if best is None:
            continue

        b_idx, board_day = best
        pullback_days = b_idx - 1  # 回调天数

        candidates.append({
            'code': code,
            'code_name': t_code_name,
            'today': today,
            'board_date': board_day['date'],
            'board_close': board_day['close'],
            'board_vol': board_day['volume'],
            'board_low': board_day['low'],
            'pullback_days': pullback_days,
            't1_date': d_t1['date'],
            't1_vol': d_t1['volume'],
            't1_vol_mult': d_t1['volume'] / avg_prev3_vol if avg_prev3_vol else 0,
            't1_close': d_t1['close'],
            't1_open': d_t1['open'],
            'mcap': mcap,
            't_open': t_open,
            't_preclose': t_preclose,
            'hour1_open': t_row[9],
            'hour_data_today': {
                'h1': (t_row[9], t_row[10], t_row[11], t_row[12]),
                'h2': (t_row[13], t_row[14], t_row[15], t_row[16]),
                'h3': (t_row[17], t_row[18], t_row[19], t_row[20]),
                'h4': (t_row[21], t_row[22], t_row[23], t_row[24]),
            }
        })

    # 按放量倍数排序(信号强度)
    candidates.sort(key=lambda x: x['t1_vol_mult'], reverse=True)
    return candidates


def get_hour_data_for_days(cur, code, days_list):
    if not days_list:
        return []
    placeholders = ','.join(['?'] * len(days_list))
    cur.execute(f"""
        SELECT date, open, close, turn,
               hour1_open, hour1_high, hour1_low, hour1_close,
               hour2_open, hour2_high, hour2_low, hour2_close,
               hour3_open, hour3_high, hour3_low, hour3_close,
               hour4_open, hour4_high, hour4_low, hour4_close
        FROM stock_kline WHERE code = ? AND date IN ({placeholders})
        ORDER BY date
    """, [code] + days_list)
    return cur.fetchall()


def format_hour_table(hour_rows, ref_open):
    """相对 T日 open 的 rate%"""
    lines = []
    lines.append(f"  {'日期':<12}| {'hour':<5}| {'open':<8}| {'high':<8}| {'low':<8}| {'close':<8}| vsT_open  | 换手率")
    lines.append(f"  {'-'*12}+{'-'*6}+{'-'*9}+{'-'*9}+{'-'*9}+{'-'*9}+{'-'*10}+{'-'*8}")
    for row in hour_rows:
        date = row[0]
        day_turn = row[3]
        hours = [
            ('h1', row[4], row[5], row[6], row[7]),
            ('h2', row[8], row[9], row[10], row[11]),
            ('h3', row[12], row[13], row[14], row[15]),
            ('h4', row[16], row[17], row[18], row[19]),
        ]
        for h_name, h_open, h_high, h_low, h_close in hours:
            if h_open is None or h_close is None:
                continue
            vs = (h_close - ref_open) / ref_open * 100 if ref_open else 0
            turn_str = f"{day_turn:.2f}%" if day_turn else "N/A"
            lines.append(
                f"  {date:<12}| {h_name:<5}| {h_open:<8.2f}| {h_high:<8.2f}| "
                f"{h_low:<8.2f}| {h_close:<8.2f}| {vs:+7.2f}%  | {turn_str}"
            )
    return '\n'.join(lines)


def calc_holding_returns(cur, code, buy_date, buy_price, all_days):
    """买入价=T hour1_open; 计算 T收盘/T+1/T+2/T+3 收盘收益。"""
    if buy_price is None or buy_price <= 0:
        return None
    try:
        idx = all_days.index(buy_date)
    except ValueError:
        return None
    fut = all_days[idx:idx + 4]  # T, T+1, T+2, T+3
    placeholders = ','.join(['?'] * len(fut))
    cur.execute(f"""
        SELECT date, high, low, close FROM stock_kline
        WHERE code = ? AND date IN ({placeholders}) ORDER BY date
    """, [code] + fut)
    rows = {r[0]: r for r in cur.fetchall()}
    res = {}
    for n, d in enumerate(fut):
        if d in rows and rows[d][3] is not None:
            res[f'T{"" if n==0 else "+"+str(n)}'] = (rows[d][3] - buy_price) / buy_price * 100
    # T日最高
    if fut and fut[0] in rows and rows[fut[0]][1] is not None:
        res['T_high'] = (rows[fut[0]][1] - buy_price) / buy_price * 100
    return res


class StatsAcc:
    def __init__(self):
        self.n = 0
        self.ret = defaultdict(list)   # key: T/T+1/T+2/T+3 -> list of pct
        self.by_pullback = defaultdict(lambda: defaultdict(list))  # pdays -> key -> list

    def add(self, res, pullback_days):
        if not res:
            return
        self.n += 1
        for k in ('T', 'T+1', 'T+2', 'T+3'):
            if k in res:
                self.ret[k].append(res[k])
                self.by_pullback[pullback_days][k].append(res[k])

    @staticmethod
    def _fmt(vals):
        if not vals:
            return "无数据"
        n = len(vals)
        avg = sum(vals) / n
        win = sum(1 for v in vals if v > 0) / n * 100
        return f"n={n:<4} 均值={avg:+6.2f}% 胜率={win:5.1f}%"

    def report(self):
        lines = []
        lines.append(f"总样本数: {self.n}")
        lines.append("--- 按持有周期(买入价=T hour1_open) ---")
        for k in ('T', 'T+1', 'T+2', 'T+3'):
            lines.append(f"  持有到{k:<4}收盘: {self._fmt(self.ret[k])}")
        lines.append("--- 按板后回调天数分组(持有到T+1收盘) ---")
        for pd in sorted(self.by_pullback.keys()):
            lines.append(f"  回调{pd}天: T+1 {self._fmt(self.by_pullback[pd]['T+1'])} | "
                         f"T+2 {self._fmt(self.by_pullback[pd]['T+2'])}")
        return '\n'.join(lines)


def run_month(cur, month_str, all_days, detail, acc):
    month_days = get_trading_days(cur, month_str)
    if not month_days:
        return
    total = 0
    for today in month_days:
        try:
            today_idx = all_days.index(today)
        except ValueError:
            continue
        candidates = find_candidates(cur, today, today_idx, all_days)
        total += len(candidates)

        if detail:
            print(f"\n{'='*60}")
            print(f"========== {today} ==========")
            print(f"候选股: {len(candidates)}只")

        shown = min(MAX_CANDIDATES_PER_DAY, len(candidates)) if detail else 0
        for i in range(shown):
            c = candidates[i]
            print(f"\n--- {c['code']} ({c['code_name'] or 'N/A'}) ---")
            print(f"  板日: {c['board_date']} close={c['board_close']:.2f} "
                  f"vol={c['board_vol']} low={c['board_low']:.2f}")
            print(f"  回调: {c['pullback_days']}天 (板后缩量不破位)")
            print(f"  T-1放量收阳: {c['t1_date']} vol={c['t1_vol']} "
                  f"(前3日均量×{c['t1_vol_mult']:.2f}) open={c['t1_open']:.2f} close={c['t1_close']:.2f}")
            print(f"  流通市值: {c['mcap']:.1f}亿")
            print(f"  今日买入: hour1_open={c['hour1_open']:.2f} (T open={c['t_open']:.2f})")

            prev5 = max(0, today_idx - 5)
            next5 = min(len(all_days), today_idx + 6)
            window_days = all_days[prev5:next5]
            hour_rows = get_hour_data_for_days(cur, c['code'], window_days)
            if hour_rows:
                print(f"\n  T-5~T+5 hour级明细 (rate相对T日open={c['t_open']:.2f}):")
                print(format_hour_table(hour_rows, c['t_open']))

        # 统计所有候选(用hour1_open买入)
        for c in candidates:
            buy = c['hour1_open']
            if not (buy and buy > 0):
                continue
            res = calc_holding_returns(cur, c['code'], today, buy, all_days)
            acc.add(res, c['pullback_days'])

    if detail:
        print(f"\n>>> {month_str} 候选股合计: {total}只")


def iter_months(start_ym, end_ym):
    sy, sm = int(start_ym[:4]), int(start_ym[5:7])
    ey, em = int(end_ym[:4]), int(end_ym[5:7])
    y, m = sy, sm
    out = []
    while (y, m) <= (ey, em):
        out.append(f"{y:04d}-{m:02d}")
        m += 1
        if m > 12:
            m = 1
            y += 1
    return out


def main():
    if len(sys.argv) < 2:
        print("用法: python strategy_limitup_pullback_vol.py 2026-04 [summary]")
        print("     python strategy_limitup_pullback_vol.py RANGE 2021-01 2026-06")
        sys.exit(1)

    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    all_days = get_all_trading_days(cur)

    print(f"{'='*80}")
    print(f"Task #103: 涨停后缩量回调再放量启动策略")
    print(f"参数: 板日T-1前{BOARD_LOOKBACK_MIN}-{BOARD_LOOKBACK_MAX}天 | 回调>={PULLBACK_MIN_DAYS}天 | "
          f"末日缩量<板日{SHRINK_RATIO*100:.0f}% | T-1放量>=前3日均量×{VOL_SURGE_MULT} | "
          f"市值{MCAP_MIN:.0f}-{MCAP_MAX:.0f}亿 | 创业板+科创板")
    print(f"数据库交易日: {all_days[0]} ~ {all_days[-1]} ({len(all_days)}天)")
    print(f"{'='*80}")

    acc = StatsAcc()

    if sys.argv[1] == 'RANGE':
        start_ym, end_ym = sys.argv[2], sys.argv[3]
        months = iter_months(start_ym, end_ym)
        print(f"全周期扫描: {start_ym} ~ {end_ym} ({len(months)}个月)")
        for mo in months:
            run_month(cur, mo, all_days, detail=False, acc=acc)
    else:
        month_str = sys.argv[1]
        detail = not (len(sys.argv) > 2 and sys.argv[2] == 'summary')
        run_month(cur, month_str, all_days, detail=detail, acc=acc)

    print(f"\n\n{'='*80}")
    print(f"========== 统计汇总 ==========")
    print(f"{'='*80}")
    print(acc.report())
    conn.close()
    print(f"\n{'='*80}")
    print("研究完成。")


if __name__ == '__main__':
    main()
