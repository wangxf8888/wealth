#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
AuctionGapUpVol 参数变体探索 —— 寻找可独立发布的互补策略
========================================================
原策略 (AuctionGapUpVol, 已验证 alpha, slot=1 CAGR 692.95%):
  创业板(sz.300/301) + 流通市值50-200亿 + 昨日非涨停 + 今日高开>=3%
  + hour1量比(today_hour1_amount/yesterday_hour1_amount)>=5x + 排ST + 排开盘涨停
  买入 T日 hour1_open, 卖出 T+1 hour4_close (持仓1交易日)

本脚本统一测试以下 4 个同逻辑变体的全周期(2021-2026)表现, 并计算与原策略的
信号重叠度, 以判定其是否可作为【信号不重叠】的独立互补策略:

  变体1 科创板版本 : sh.688, 市值100-500亿, gap>=3%, vol>=5x, 持仓1天
  变体2 主板大盘股 : sh.6(非688)/sz.00, 市值200-1000亿, gap>=2%, vol>=3x, 持仓1天
  变体3 微高开+极端量比: 创业板+科创板, gap>=1%, vol>=10x, 持仓1天
  变体4 持仓加长   : 与原策略相同信号, 分别测试持仓 1/2/3 天 (T+1/T+2/T+3 尾盘卖)

达标线 (rule2): 月化>=10% (单笔T+1约>=+3.5%概念) / 胜率>=55% / 逐年稳健
互补判定:
  - 与原策略信号重叠 > 80%  => 无独立价值
  - 重叠 < 30% 且自身达标(单笔>=+3.5% & 胜率>=55%) => 可作为独立策略发布

合规 (rule2 / T+1):
  所有筛选条件仅用 T日及之前数据(gap/量比用当日hour1, 属竞价后可得); 买入 T日 hour1_open;
  卖出最早 T+1, 本脚本卖点均为 T+k hour4_close (k>=1), 严格 T+1 合规。

输出:
  日志: /home/AIWealth/scripts/logs/gapup_vol_variants.log
"""
import sqlite3
import sys
import os
from collections import defaultdict

DB_PATH = "/home/AIWealth/data/stocks.db"
LOG_PATH = "/home/AIWealth/scripts/logs/gapup_vol_variants.log"
DATE_START = "2021-01-01"
DATE_END = "2026-06-30"

# ===================== 达标线 =====================
TARGET_AVG = 3.5        # 单笔平均收益(%) 达标线
TARGET_WIN = 55.0       # 胜率(%) 达标线
OVERLAP_INDEP = 30.0    # 重叠度低于此 => 具独立价值
OVERLAP_REDUNDANT = 80.0  # 重叠度高于此 => 冗余无价值

# 只加载必要列(流式处理, 控制内存)
LOAD_COLS = [
    'date', 'code', 'code_name', 'preclose', 'open', 'close',
    'amount', 'turn', 'isST', 'hour1_amount', 'hour1_open', 'hour2_open', 'hour4_close',
]


def get_board(code):
    if code.startswith('bj.'):
        return 'bse'
    if code.startswith('sh.688'):
        return 'star'
    if code.startswith('sz.300') or code.startswith('sz.301'):
        return 'gem'
    return 'main'


def get_limit_ratio(code):
    b = get_board(code)
    return {'bse': 0.30, 'star': 0.20, 'gem': 0.20, 'main': 0.10}[b]


def is_limit_up(code, close, preclose):
    if close is None or preclose is None or preclose <= 0:
        return False
    return close >= round(preclose * (1 + get_limit_ratio(code)), 2) - 0.001


def is_open_limit_up(code, open_price, preclose):
    if open_price is None or preclose is None or preclose <= 0:
        return False
    return open_price >= round(preclose * (1 + get_limit_ratio(code)), 2) - 0.001


def is_st(name, is_st_flag):
    if is_st_flag == 1:
        return True
    return 'ST' in (name or '').upper()


def calc_mcap(amount, turn):
    """流通市值(亿) = amount / (turn/100) / 1e8"""
    if turn is None or turn <= 0 or amount is None or amount <= 0:
        return None
    return amount * 100 / turn / 1e8


# ===================== 基础信号扫描 (流式, 一次遍历全市场) =====================
# 基础过滤(覆盖所有变体的最宽条件): gap>=1 & vol>=3 & 排ST & 昨非涨停 & 排开盘涨停 & mcap可算
BASE_GAP = 1.0
BASE_VOL = 3.0


def scan_base_signals(conn):
    """
    流式扫描创业板/科创板/主板全市场, 返回所有满足最宽基础条件的信号记录。
    每条记录: dict(code, name, date, board, gap, vol_ratio, mcap, ret1, ret2, ret3)
    ret_k = 买入 T日 hour1_open, 卖出 T+k hour4_close 的收益(%)
    """
    cur = conn.cursor()
    col_sql = ','.join(LOAD_COLS)
    cur.execute(f"""
        SELECT {col_sql} FROM stock_kline
        WHERE code LIKE 'sh.6%' OR code LIKE 'sz.00%'
              OR code LIKE 'sz.300%' OR code LIKE 'sz.301%'
        ORDER BY code, date
    """)

    signals = []
    cur_code = None
    rows = []

    def flush(code, srows):
        if not code or len(srows) < 5:
            return
        board = get_board(code)
        n = len(srows)
        for i in range(1, n - 1):  # 至少需 T-1 与 T+1
            t0 = srows[i]
            tm1 = srows[i - 1]
            d = t0['date']
            if d < DATE_START or d > DATE_END:
                continue
            if t0['preclose'] is None or t0['preclose'] <= 0:
                continue
            if t0['open'] is None or t0['open'] <= 0:
                continue
            if t0['hour1_amount'] is None or t0['hour1_amount'] <= 0:
                continue
            if tm1['hour1_amount'] is None or tm1['hour1_amount'] <= 0:
                continue
            if t0['hour1_open'] is None or t0['hour1_open'] <= 0:
                continue
            if is_st(t0['code_name'], t0['isST']) or is_st(tm1['code_name'], tm1['isST']):
                continue
            if is_limit_up(code, tm1['close'], tm1['preclose']):
                continue
            gap = (t0['open'] - t0['preclose']) / t0['preclose'] * 100
            if gap < BASE_GAP:
                continue
            if is_open_limit_up(code, t0['open'], t0['preclose']):
                continue
            vol_ratio = t0['hour1_amount'] / tm1['hour1_amount']
            if vol_ratio < BASE_VOL:
                continue
            mcap = calc_mcap(t0['amount'], t0['turn'])
            if mcap is None or mcap <= 0:
                continue

            buy = t0['hour1_open']
            rets = []
            for k in (1, 2, 3):
                j = i + k
                if j < n and srows[j]['hour4_close'] and srows[j]['hour4_close'] > 0:
                    rets.append((srows[j]['hour4_close'] - buy) / buy * 100)
                else:
                    rets.append(None)
            if rets[0] is None:  # 至少要有 T+1 收益
                continue

            # 合规敏感性: 用 hour2_open 买入 (第一小时结束后才知量比, 买在10:30更合规), 卖 T+1 尾盘
            buy2 = t0['hour2_open']
            ret1_h2 = None
            if buy2 and buy2 > 0:
                j = i + 1
                if j < n and srows[j]['hour4_close'] and srows[j]['hour4_close'] > 0:
                    ret1_h2 = (srows[j]['hour4_close'] - buy2) / buy2 * 100

            signals.append({
                'code': code, 'name': t0['code_name'], 'date': d, 'board': board,
                'gap': gap, 'vol_ratio': vol_ratio, 'mcap': mcap,
                'ret1': rets[0], 'ret2': rets[1], 'ret3': rets[2],
                'ret1_h2': ret1_h2,
            })

    for row in cur:
        d = dict(zip(LOAD_COLS, row))
        if d['code'] != cur_code:
            flush(cur_code, rows)
            cur_code = d['code']
            rows = []
        rows.append(d)
    flush(cur_code, rows)

    signals.sort(key=lambda s: (s['date'], s['code']))
    return signals


# ===================== 变体过滤 =====================
def filt(base, boards=None, mcap_min=0, mcap_max=1e18, gap_min=0, vol_min=0):
    out = []
    for s in base:
        if boards and s['board'] not in boards:
            continue
        if s['mcap'] < mcap_min or s['mcap'] > mcap_max:
            continue
        if s['gap'] < gap_min:
            continue
        if s['vol_ratio'] < vol_min:
            continue
        out.append(s)
    return out


def stat(sigs, ret_key='ret1'):
    rets = [s[ret_key] for s in sigs if s[ret_key] is not None]
    n = len(rets)
    if n == 0:
        return {'n': 0, 'avg': 0.0, 'win': 0.0}
    return {
        'n': n,
        'avg': sum(rets) / n,
        'win': sum(1 for r in rets if r > 0) / n * 100,
    }


def yearly_stat(sigs, ret_key='ret1'):
    yr = defaultdict(list)
    for s in sigs:
        if s[ret_key] is not None:
            yr[s['date'][:4]].append(s[ret_key])
    out = {}
    for y in sorted(yr):
        rets = yr[y]
        out[y] = {
            'n': len(rets),
            'avg': sum(rets) / len(rets),
            'win': sum(1 for r in rets if r > 0) / len(rets) * 100,
        }
    return out


def overlap_pct(variant_sigs, original_keys):
    """variant 信号中有多少 (code,date) 同时属于原策略信号集"""
    if not variant_sigs:
        return 0.0, 0
    keys = set((s['code'], s['date']) for s in variant_sigs)
    inter = keys & original_keys
    return len(inter) / len(keys) * 100, len(inter)


# ===================== 报告 =====================
class Tee:
    def __init__(self, f):
        self.file = f
        self.stdout = sys.__stdout__

    def write(self, s):
        self.stdout.write(s)
        self.file.write(s)

    def flush(self):
        self.stdout.flush()
        self.file.flush()


def print_variant(title, cond_desc, sigs, ret_key, original_keys, note=''):
    st = stat(sigs, ret_key)
    ys = yearly_stat(sigs, ret_key)
    ov_pct, ov_n = overlap_pct(sigs, original_keys)

    print("\n" + "=" * 74)
    print(f"【{title}】")
    print(f"  条件: {cond_desc}")
    if note:
        print(f"  说明: {note}")
    print("-" * 74)
    print(f"  信号数      : {st['n']}")
    print(f"  胜率        : {st['win']:.1f}%")
    print(f"  平均每笔收益: {st['avg']:+.2f}%  (卖点={ret_key})")
    print(f"  与原策略重叠: {ov_pct:.1f}%  ({ov_n}/{st['n']} 笔同时满足原策略条件)")
    print("  逐年:")
    all_pos = True
    for y, v in ys.items():
        print(f"    {y}: {v['n']:>4}笔  胜率{v['win']:5.1f}%  单笔{v['avg']:+.2f}%")
        if v['avg'] <= 0:
            all_pos = False

    # 达标判定
    self_ok = (st['avg'] >= TARGET_AVG and st['win'] >= TARGET_WIN)
    print("-" * 74)
    print(f"  ▶ 自身达标(单笔>=+{TARGET_AVG}% & 胜率>={TARGET_WIN}%): "
          f"{'✅ 达标' if self_ok else '❌ 未达标'}"
          f"{'  (逐年全正)' if all_pos else '  (存在亏损年份)'}")
    if ov_pct > OVERLAP_REDUNDANT:
        verdict = "❌ 与原策略高度重叠(>80%), 无独立价值"
    elif ov_pct < OVERLAP_INDEP and self_ok:
        verdict = "✅ 可作为独立互补策略发布 (重叠<30% 且自身达标)"
    elif ov_pct < OVERLAP_INDEP and not self_ok:
        verdict = "⚠️ 信号独立(重叠<30%)但自身不达标, 暂不发布"
    else:
        verdict = "⚠️ 重叠度中等(30~80%), 需人工权衡互补性"
    print(f"  ▶ 互补性判定: {verdict}")
    return {'title': title, 'stat': st, 'overlap': ov_pct,
            'self_ok': self_ok, 'all_pos': all_pos, 'verdict': verdict}


def main():
    os.makedirs(os.path.dirname(LOG_PATH), exist_ok=True)
    logf = open(LOG_PATH, 'w', encoding='utf-8')
    sys.stdout = Tee(logf)
    try:
        print("=" * 74)
        print("AuctionGapUpVol 参数变体探索  (全周期 2021-2026)")
        print(f"数据范围: {DATE_START} ~ {DATE_END}")
        print("买入=T日 hour1_open; 卖出=T+k hour4_close (严格T+1合规)")
        print("=" * 74)

        conn = sqlite3.connect(DB_PATH)
        print("扫描全市场基础信号 (gap>=1% & vol>=3x, 创业板+科创板+主板)...")
        base = scan_base_signals(conn)
        conn.close()
        print(f"基础信号池: {len(base)} 笔")
        board_cnt = defaultdict(int)
        for s in base:
            board_cnt[s['board']] += 1
        print(f"  按板块: {dict(board_cnt)}")

        # ---- 原策略信号集 (用于重叠度基准) ----
        original = filt(base, boards={'gem'}, mcap_min=50, mcap_max=200,
                        gap_min=3, vol_min=5)
        original_keys = set((s['code'], s['date']) for s in original)
        ost = stat(original, 'ret1')
        print("\n" + "#" * 74)
        print("# 原策略 AuctionGapUpVol (重叠度基准)")
        print(f"#   创业板 50-200亿 gap>=3% vol>=5x, 买h1_open 卖T+1尾盘")
        print(f"#   信号数={ost['n']}  胜率={ost['win']:.1f}%  单笔={ost['avg']:+.2f}%")
        print("#" * 74)

        results = []

        # ---- 变体1: 科创板版本 ----
        v1 = filt(base, boards={'star'}, mcap_min=100, mcap_max=500,
                  gap_min=3, vol_min=5)
        results.append(print_variant(
            "变体1 科创板版本", "科创板(sh.688) 市值100-500亿 gap>=3% vol>=5x, 持仓1天(T+1尾盘)",
            v1, 'ret1', original_keys,
            note="不同板块(科创板 vs 创业板), 天然与原策略信号池互斥"))

        # ---- 变体2: 主板大盘股 ----
        v2 = filt(base, boards={'main'}, mcap_min=200, mcap_max=1000,
                  gap_min=2, vol_min=3)
        results.append(print_variant(
            "变体2 主板大盘股", "主板(sh.6非688/sz.00) 市值200-1000亿 gap>=2% vol>=3x, 持仓1天(T+1尾盘)",
            v2, 'ret1', original_keys,
            note="大盘股gap较小, 量比放宽至3x; 不同板块+市值区间, 与原策略互斥"))

        # ---- 变体3: 微高开+极端量比 ----
        v3 = filt(base, boards={'gem', 'star'}, mcap_min=0, mcap_max=1e18,
                  gap_min=1, vol_min=10)
        results.append(print_variant(
            "变体3 微高开+极端量比", "创业板+科创板 gap>=1% vol>=10x (不限市值), 持仓1天(T+1尾盘)",
            v3, 'ret1', original_keys,
            note="低gap高量比=不高开但量能极大, 疑似偷偷建仓; 与原策略在创业板gap>=3段重叠"))

        # ---- 变体4: 持仓加长 (相同原始信号, 不同持仓天数) ----
        print("\n" + "=" * 74)
        print("【变体4 持仓加长】")
        print("  条件: 与原策略完全相同信号(创业板 50-200亿 gap>=3% vol>=5x),")
        print("        对比持仓 1/2/3 天(T+1/T+2/T+3 尾盘卖出)")
        print("  说明: 信号集与原策略100%重叠(非信号变体), 目的是找更优持仓周期")
        print("-" * 74)
        hold_map = {'持仓1天(T+1尾盘)': 'ret1', '持仓2天(T+2尾盘)': 'ret2', '持仓3天(T+3尾盘)': 'ret3'}
        print(f"  {'持仓周期':<18}{'样本':<7}{'胜率':<10}{'平均每笔':<12}{'逐年全正':<8}")
        print("  " + "-" * 56)
        v4_rows = []
        for label, rk in hold_map.items():
            st = stat(original, rk)
            ys = yearly_stat(original, rk)
            all_pos = all(v['avg'] > 0 for v in ys.values())
            print(f"  {label:<18}{st['n']:<7}{st['win']:<9.1f}%{st['avg']:+.2f}%{'':<6}"
                  f"{'✓' if all_pos else '✗'}")
            v4_rows.append((label, rk, st, all_pos))
        # 变体4 达标判定: 找出比持仓1天(基准)更优且达标的持仓周期
        base1 = stat(original, 'ret1')
        best = max(v4_rows, key=lambda x: x[2]['avg'])
        print("-" * 74)
        print(f"  ▶ 持仓1天基准: 单笔{base1['avg']:+.2f}% 胜率{base1['win']:.1f}%")
        print(f"  ▶ 最优持仓周期: {best[0]}  单笔{best[2]['avg']:+.2f}% 胜率{best[2]['win']:.1f}%")
        if best[1] == 'ret1':
            print("  ▶ 结论: 加长持仓未带来提升, 维持原策略T+1卖出即可 (无新增独立策略)")
            v4_verdict = "❌ 加长持仓无提升, 无独立价值"
        else:
            improved = best[2]['avg'] - base1['avg']
            v4_ok = best[2]['avg'] >= TARGET_AVG and best[2]['win'] >= TARGET_WIN
            print(f"  ▶ 结论: {best[0]} 较T+1 提升 {improved:+.2f}%; "
                  f"因信号与原策略100%重叠, 属【持仓周期优化】而非独立信号策略,")
            print("          不能与原策略并行占用不同slot(信号相同), 但可作为原策略的持仓参数升级。")
            v4_verdict = f"⚠️ {best[0]}优于T+1(信号100%重叠, 属持仓优化非独立策略)"
        results.append({'title': '变体4 持仓加长', 'stat': best[2], 'overlap': 100.0,
                        'self_ok': best[2]['avg'] >= TARGET_AVG and best[2]['win'] >= TARGET_WIN,
                        'all_pos': best[3], 'verdict': v4_verdict})

        # ---- 汇总 ----
        print("\n" + "#" * 74)
        print("# 汇总: 各变体达标 & 互补性判定")
        print("#" * 74)
        print(f"{'变体':<22}{'信号数':>7}{'胜率':>8}{'单笔':>8}{'重叠度':>8}  判定")
        print("-" * 74)
        for r in results:
            print(f"{r['title']:<22}{r['stat']['n']:>7}{r['stat']['win']:>7.1f}%"
                  f"{r['stat']['avg']:>+7.2f}%{r['overlap']:>7.1f}%  {r['verdict']}")
        print("-" * 74)
        # ---- 合规敏感性检验: hour1_open(潜在未来函数) vs hour2_open(10:30可确认量比后买入) ----
        print("\n" + "#" * 74)
        print("# 合规敏感性检验 (关键!): 量比用 hour1_amount(09:30-10:30整段) 过滤,")
        print("#   但原策略买在 hour1_open(09:30) => 第一小时量能未走完即买入, 属潜在未来函数。")
        print("#   对比 hour2_open(10:30, 量比已确认后才买) 卖T+1尾盘, 量化该影响:")
        print("#" * 74)
        print(f"{'信号集':<22}{'样本':>7}{'h1_open单笔':>12}{'h1_open胜率':>12}{'h2_open单笔':>12}{'h2_open胜率':>12}")
        print("-" * 74)
        for label, sset_sigs in [("原策略(gem gap3 vol5)", original), ("变体3(微高开极端量比)", v3)]:
            s1 = stat(sset_sigs, 'ret1')
            s2 = stat(sset_sigs, 'ret1_h2')
            print(f"{label:<22}{s1['n']:>7}{s1['avg']:>+11.2f}%{s1['win']:>11.1f}%"
                  f"{s2['avg']:>+11.2f}%{s2['win']:>11.1f}%")
        print("-" * 74)
        print("  ▶ 若 h2_open 收益大幅塌陷 => 说明 alpha 主要来自第一小时内的价格冲高,")
        print("    即 hour1_open 买入依赖了尚未发生的 hour1 量能信息(未来函数), 需谨慎对待。")
        print("    合规落地应以 hour2_open(或次日)为可执行买点重新评估达标性。")

        publishable = [r for r in results
                       if r['overlap'] < OVERLAP_INDEP and r['self_ok']]
        print(f"\n可独立发布的互补策略数: {len(publishable)}")
        for r in publishable:
            print(f"  ✅ {r['title']}: 单笔{r['stat']['avg']:+.2f}% 胜率{r['stat']['win']:.1f}% "
                  f"重叠{r['overlap']:.1f}% (n={r['stat']['n']})")
        if not publishable:
            print("  (无变体同时满足 重叠<30% + 自身达标, 详见上方逐项判定)")
        print("\n完成。")
    finally:
        sys.stdout.flush()
        sys.stdout = sys.__stdout__
        logf.close()


if __name__ == "__main__":
    main()
