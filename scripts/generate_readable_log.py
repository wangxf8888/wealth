#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
多指标评分量化系统 - 仓位视图回测日志生成器
从 backtest_scoring_trades.json / backtest_daily_candidates.json / backtest_scoring_nav.json
生成人类可读的逐日仓位视图交易日志。

用法: python generate_readable_log.py
输出: backtest_readable_log.txt

格式: 每天展示5个仓位的状态变化 + 收益分层展示(单仓/对总资产贡献/当日汇总)
"""

import json
import os
from collections import defaultdict

# ============ 配置 ============
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
TRADES_FILE = os.path.join(SCRIPT_DIR, 'backtest_scoring_trades.json')
CANDIDATES_FILE = os.path.join(SCRIPT_DIR, 'backtest_daily_candidates.json')
NAV_FILE = os.path.join(SCRIPT_DIR, 'backtest_scoring_nav.json')
OUTPUT_FILE = os.path.join(SCRIPT_DIR, 'backtest_readable_log.txt')

# 只输出2025年1月作为示例
MONTH_FILTER = '2025-01'

INITIAL_CAPITAL = 1_000_000
N_SLOTS = 5
TP_PCT = 10.0
SL_PCT = -3.0
MAX_HOLD = 5


def fmt_amount(amount):
    """智能格式化金额：万/亿"""
    abs_amt = abs(amount)
    if abs_amt >= 1e12:
        return f"{amount/1e12:.2f}万亿"
    elif abs_amt >= 1e8:
        return f"{amount/1e8:.2f}亿"
    elif abs_amt >= 1e4:
        return f"{amount/1e4:.1f}万"
    else:
        return f"{amount:,.0f}元"


def fmt_price(price):
    if price >= 100:
        return f"{price:.2f}"
    else:
        return f"{price:.3f}"


def parse_sell_reason_short(sell_reason):
    """解析卖出原因为简短描述"""
    if 'TP' in sell_reason:
        # 提取触发小时和百分比
        hour_part = ""
        if 'hour' in sell_reason.lower():
            pass
        return f"止盈({sell_reason.split(':')[0] if ':' in sell_reason else sell_reason})"
    elif 'SL' in sell_reason:
        return f"止损({sell_reason.split(':')[0] if ':' in sell_reason else sell_reason})"
    elif 'MAX_HOLD' in sell_reason or 'time_limit' in sell_reason:
        return "到期卖出(持满5天)"
    else:
        return sell_reason


def parse_sell_reason_detail(sell_reason, sell_hour):
    """解析卖出原因为详细描述"""
    hour_str = sell_hour if sell_hour else ""
    if 'TP' in sell_reason:
        # 提取实际涨幅
        pct_part = ""
        if '(' in sell_reason and '/' in sell_reason:
            inner = sell_reason.split('(')[1].rstrip(')')
            pct_part = inner.split('/')[0]
        return f"{hour_str}触发止盈{pct_part}"
    elif 'SL' in sell_reason:
        pct_part = ""
        if '(' in sell_reason and '/' in sell_reason:
            inner = sell_reason.split('(')[1].rstrip(')')
            pct_part = inner.split('/')[0]
        return f"{hour_str}触发止损{pct_part}"
    elif 'MAX_HOLD' in sell_reason or 'time_limit' in sell_reason:
        return f"{hour_str}持满5天强制卖出"
    else:
        return f"{hour_str}{sell_reason}"


def main():
    print('加载数据...')
    with open(TRADES_FILE) as f:
        trades = json.load(f)
    with open(CANDIDATES_FILE) as f:
        candidates_list = json.load(f)
    with open(NAV_FILE) as f:
        nav_list = json.load(f)

    # 建立索引
    cand_by_date = {c['date']: c for c in candidates_list}
    nav_by_date = {n['date']: n for n in nav_list}

    buys_by_date = defaultdict(list)
    sells_by_date = defaultdict(list)
    for t in trades:
        buys_by_date[t['buy_date']].append(t)
        sells_by_date[t['sell_date']].append(t)

    # 获取所有交易日期
    all_dates = sorted(set(
        list(nav_by_date.keys()) +
        list(cand_by_date.keys()) +
        list(buys_by_date.keys()) +
        list(sells_by_date.keys())
    ))

    # 确定输出范围
    if MONTH_FILTER:
        output_dates = [d for d in all_dates if d.startswith(MONTH_FILTER)]
    else:
        output_dates = all_dates

    print(f'生成日志: {len(output_dates)}个交易日 (过滤={MONTH_FILTER or "全部"})')

    # ====== 模拟仓位状态 ======
    # 为了正确显示仓位视图，需要从头开始模拟到目标月份
    # slots[i] = None 或 {code, buy_price, buy_date, buy_hour, shares, hold_days,
    #                      current_price, star_count, score, strategy_name}
    slots = [None] * N_SLOTS

    def find_empty_slot():
        for i in range(N_SLOTS):
            if slots[i] is None:
                return i
        return -1

    def find_slot_by_code(code):
        for i in range(N_SLOTS):
            if slots[i] is not None and slots[i]['code'] == code:
                return i
        return -1

    # 先模拟到目标月份之前，建立仓位状态
    first_output = output_dates[0] if output_dates else None
    pre_dates = [d for d in all_dates if first_output and d < first_output]

    for date in pre_dates:
        # 处理卖出(先卖后买)
        for t in sells_by_date.get(date, []):
            idx = find_slot_by_code(t['code'])
            if idx >= 0:
                slots[idx] = None

        # 处理买入
        for t in buys_by_date.get(date, []):
            idx = find_empty_slot()
            if idx >= 0:
                slots[idx] = {
                    'code': t['code'],
                    'buy_price': t['buy_price'],
                    'buy_date': t['buy_date'],
                    'buy_hour': t.get('buy_hour', ''),
                    'shares': t['shares'],
                    'hold_days': t['hold_days'],
                    'star_count': t.get('star_count', 0),
                    'score': t.get('score_at_buy', 0),
                    'strategy_name': t.get('strategy_name', ''),
                    'current_price': t['buy_price'],
                }

    # 计算持仓天数(从pre_dates最后到output开始)
    # 简化：用trade的hold_days数据来还原

    # ====== 开始生成日志 ======
    lines = []

    # 头部
    lines.append('=' * 90)
    lines.append('  多指标评分量化系统 - 仓位视图回测日志')
    lines.append('=' * 90)
    lines.append(f'回测区间: 2021-01-01 ~ 2026-06-30 | 初始资金: 100万元')
    lines.append(f'持仓模式: {N_SLOTS}仓位并发 | 每仓=总资产/{N_SLOTS}')
    lines.append(f'卖出规则: 止盈+{TP_PCT:.0f}% | 止损{SL_PCT:.0f}% | 最大持仓{MAX_HOLD}天')
    lines.append(f'买入规则: >=3星且至少1个trigger亮, 按(星数,评分)降序选股')
    lines.append('=' * 90)
    lines.append(f'[注] 本日志仅输出 {MONTH_FILTER} 数据作为示例')
    lines.append(f'[注] 单仓收益率=(卖价-买价)/买价; 对总资产贡献=单仓盈亏/当日总资产')
    lines.append('')

    # 获取起始NAV（前一天的NAV）
    prev_nav = None
    if pre_dates:
        last_pre = pre_dates[-1]
        if last_pre in nav_by_date:
            prev_nav = nav_by_date[last_pre]['nav']

    day_counter = 0

    for date in output_dates:
        day_counter += 1
        day_buys = buys_by_date.get(date, [])
        day_sells = sells_by_date.get(date, [])
        cand_info = cand_by_date.get(date)
        nav_info = nav_by_date.get(date)

        if not nav_info and not day_buys and not day_sells and not cand_info:
            if prev_nav and nav_info:
                prev_nav = nav_info['nav']
            continue

        current_nav = nav_info['nav'] if nav_info else prev_nav
        if current_nav is None:
            current_nav = INITIAL_CAPITAL

        # 计算当日盈亏
        if prev_nav and prev_nav > 0:
            daily_pnl = current_nav - prev_nav
            daily_pnl_pct = (current_nav / prev_nav - 1) * 100
        else:
            daily_pnl = 0
            daily_pnl_pct = 0

        cumulative_pct = (current_nav / INITIAL_CAPITAL - 1) * 100

        # 每仓分配额
        slot_size = current_nav / N_SLOTS

        # === 日标题 ===
        daily_pnl_sign = '+' if daily_pnl >= 0 else ''
        daily_pnl_pct_sign = '+' if daily_pnl_pct >= 0 else ''
        lines.append(f'=== {date} (第{day_counter}个交易日) | '
                     f'总资产: {fmt_amount(current_nav)} | '
                     f'当日盈亏: {daily_pnl_sign}{fmt_amount(daily_pnl)}'
                     f'({daily_pnl_pct_sign}{daily_pnl_pct:.2f}%) | '
                     f'累计: {cumulative_pct:+.1f}% ===')
        lines.append('')

        # 处理当日卖出 (先处理卖出以更新仓位状态)
        sold_slots = {}  # slot_idx -> trade_info
        for t in day_sells:
            idx = find_slot_by_code(t['code'])
            if idx >= 0:
                sold_slots[idx] = t
                slots[idx] = None

        # 处理当日买入
        bought_slots = {}  # slot_idx -> trade_info
        for t in day_buys:
            idx = find_empty_slot()
            if idx >= 0:
                slots[idx] = {
                    'code': t['code'],
                    'buy_price': t['buy_price'],
                    'buy_date': t['buy_date'],
                    'buy_hour': t.get('buy_hour', ''),
                    'shares': t['shares'],
                    'hold_days': 0,
                    'star_count': t.get('star_count', 0),
                    'score': t.get('score_at_buy', 0),
                    'strategy_name': t.get('strategy_name', ''),
                    'current_price': t['buy_price'],
                }
                bought_slots[idx] = t

        # === 仓位表格 ===
        lines.append(' 仓位 | 状态     | 股票代码    | 操作/说明                        '
                     '| 买入价   | 当前价  | 仓位盈亏  | 对总资产')
        lines.append('------+----------+------------+----------------------------------'
                     '+----------+---------+-----------+---------')

        for i in range(N_SLOTS):
            slot_num = i + 1
            if i in sold_slots and i in bought_slots:
                # 同一天卖出又买入了新股（先卖后买）
                # 显示新买入状态
                t = bought_slots[i]
                status = "新买入"
                code = t['code']
                desc = f"{t.get('star_count',0)}星,{t.get('buy_hour','')}"
                buy_p = fmt_price(t['buy_price'])
                cur_p = fmt_price(t['buy_price'])
                pnl_str = "0.0%"
                contrib_str = "0.0%"
            elif i in sold_slots:
                # 已卖出，现在是空仓
                t = sold_slots[i]
                status = "卖出->空"
                code = t['code']
                reason_short = parse_sell_reason_short(t.get('sell_reason', ''))
                desc = reason_short[:30]
                buy_p = fmt_price(t['buy_price'])
                cur_p = fmt_price(t['sell_price'])
                pnl_val = t['pnl_pct']
                pnl_str = f"{pnl_val:+.1f}%"
                # 对总资产贡献 = 单仓盈亏金额 / 总资产
                contrib = t['pnl_amount'] / current_nav * 100 if current_nav > 0 else 0
                contrib_str = f"{contrib:+.2f}%"
            elif i in bought_slots:
                # 新买入
                t = bought_slots[i]
                status = "新买入"
                code = t['code']
                desc = f"{t.get('star_count',0)}星,{t.get('buy_hour','hour1')}"
                buy_p = fmt_price(t['buy_price'])
                cur_p = buy_p
                pnl_str = "0.0%"
                contrib_str = "0.0%"
            elif slots[i] is not None:
                # 持有中
                pos = slots[i]
                status = "持有中"
                code = pos['code']
                # 计算持有天数
                hold_d = pos.get('hold_days', 0) + 1
                desc = f"第{hold_d}天持有"
                buy_p = fmt_price(pos['buy_price'])
                cur_p = fmt_price(pos.get('current_price', pos['buy_price']))
                cur_price_val = pos.get('current_price', pos['buy_price'])
                pnl_val = (cur_price_val / pos['buy_price'] - 1) * 100 if pos['buy_price'] > 0 else 0
                pnl_str = f"{pnl_val:+.1f}%"
                pnl_amount_est = pos['shares'] * (cur_price_val - pos['buy_price'])
                contrib = pnl_amount_est / current_nav * 100 if current_nav > 0 else 0
                contrib_str = f"{contrib:+.2f}%"
            else:
                # 空仓
                status = "空"
                code = "-"
                desc = "-"
                buy_p = "-"
                cur_p = "-"
                pnl_str = "-"
                contrib_str = "-"

            lines.append(f'  {slot_num}   | {status:<8s} | {code:<10s} | {desc:<32s}'
                         f'| {buy_p:<8s} | {cur_p:<7s} | {pnl_str:<9s} | {contrib_str}')

        lines.append('')

        # === 当日卖出明细 ===
        if day_sells:
            lines.append(f'  当日卖出明细({len(day_sells)}笔):')
            for t in day_sells:
                # 找到原来的slot
                orig_slot = -1
                for si, st in sold_slots.items():
                    if st is t:
                        orig_slot = si + 1
                        break
                code = t['code']
                hold_days = t['hold_days']
                pnl_pct = t['pnl_pct']
                pnl_amount = t['pnl_amount']
                sell_hour = t.get('sell_hour', '')
                reason_detail = parse_sell_reason_detail(t.get('sell_reason', ''), sell_hour)
                contrib = pnl_amount / current_nav * 100 if current_nav > 0 else 0

                pnl_sign = '+' if pnl_pct >= 0 else ''
                contrib_sign = '+' if contrib >= 0 else ''

                lines.append(f'    仓位{orig_slot}: {code} 持仓{hold_days}天, {reason_detail}, '
                             f'买入{fmt_price(t["buy_price"])}->卖出{fmt_price(t["sell_price"])}, '
                             f'单仓{pnl_sign}{pnl_pct:.2f}%({pnl_sign}{fmt_amount(pnl_amount)}), '
                             f'对总资产贡献{contrib_sign}{contrib:.2f}%')
            lines.append('')

        # === 当日买入明细 ===
        if day_buys:
            lines.append(f'  当日买入明细({len(day_buys)}笔):')
            for t in day_buys:
                orig_slot = -1
                for si, st in bought_slots.items():
                    if st is t:
                        orig_slot = si + 1
                        break
                code = t['code']
                buy_price = t['buy_price']
                shares = t['shares']
                amount = buy_price * shares
                star = t.get('star_count', 0)
                score = t.get('score_at_buy', 0)
                hour = t.get('buy_hour', 'hour1')
                tp_price = buy_price * (1 + TP_PCT / 100)
                sl_price = buy_price * (1 + SL_PCT / 100)

                lines.append(f'    仓位{orig_slot}: {code} {star}星(score={score:.1f}), '
                             f'{hour}以{fmt_price(buy_price)}买入, '
                             f'{shares}股, 分配资金{fmt_amount(amount)}')
                lines.append(f'           止盈目标{fmt_price(tp_price)}(+{TP_PCT:.0f}%) | '
                             f'止损目标{fmt_price(sl_price)}({SL_PCT:.0f}%) | '
                             f'最长持{MAX_HOLD}天')
            lines.append('')

        # === 当日候选股 ===
        if cand_info and cand_info.get('candidates'):
            candidates = cand_info['candidates']
            selected = set(cand_info.get('selected', []))
            skipped = set(cand_info.get('skipped_full', []))
            available = cand_info.get('available_slots', N_SLOTS)

            lines.append(f'  当日候选股(共{len(candidates)}只, 可用仓位{available}, 选中{len(selected)}只):')
            for c in candidates:
                code = c['code']
                star = c.get('star_count', 0)
                score = c.get('score', 0)
                strategy = c.get('strategy', '')
                price = c.get('buy_price', 0)

                if code in selected:
                    mark = "< 选中买入"
                elif code in skipped:
                    mark = "< 仓位满跳过"
                else:
                    mark = "< 未选中"

                lines.append(f'    {"V" if code in selected else "X"} {code} '
                             f'{star}星 score={score:.1f} '
                             f'参考价{fmt_price(price)} {mark}')
            lines.append('')

        # 分隔线
        lines.append('-' * 90)
        lines.append('')

        # 更新prev_nav
        if nav_info:
            prev_nav = nav_info['nav']

        # 更新持有天数(简化：每过一天+1)
        for i in range(N_SLOTS):
            if slots[i] is not None and i not in bought_slots:
                slots[i]['hold_days'] = slots[i].get('hold_days', 0) + 1

    # === 写入文件 ===
    output_text = '\n'.join(lines)
    with open(OUTPUT_FILE, 'w', encoding='utf-8') as f:
        f.write(output_text)

    print(f'日志已生成: {OUTPUT_FILE}')
    print(f'总行数: {len(lines)}')
    print(f'涵盖交易日: {len(output_dates)}天')


if __name__ == '__main__':
    main()
#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
多指标评分量化系统 - 可读性回测交易日志生成器
从 backtest_scoring_trades.json / backtest_daily_candidates.json / backtest_scoring_nav.json
生成人类可读的逐日交易日志。

用法: python generate_readable_log.py
输出: backtest_readable_log.txt
"""

import json
import os
from collections import defaultdict

# ============ 配置 ============
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
TRADES_FILE = os.path.join(SCRIPT_DIR, 'backtest_scoring_trades.json')
CANDIDATES_FILE = os.path.join(SCRIPT_DIR, 'backtest_daily_candidates.json')
NAV_FILE = os.path.join(SCRIPT_DIR, 'backtest_scoring_nav.json')
OUTPUT_FILE = os.path.join(SCRIPT_DIR, 'backtest_readable_log.txt')

# 只输出2025年数据作为示例（全量3215笔太大）
YEAR_FILTER = '2025'  # 设为None则输出全部

INITIAL_CAPITAL = 1_000_000
N_SLOTS = 5
TP_PCT = 10.0
SL_PCT = -3.0
MAX_HOLD = 5
SLIPPAGE_BUY = 0.002
SLIPPAGE_SELL = 0.002


def star_display(star_count):
    full = int(star_count)
    empty = 7 - full
    return '★' * full + '☆' * empty


def format_price(price):
    return f'{price:.2f}'


def parse_sell_reason(sell_reason):
    if 'TP' in sell_reason:
        return '触发止盈'
    elif 'SL' in sell_reason:
        return '触发止损'
    elif 'MAX_HOLD' in sell_reason or 'time_limit' in sell_reason:
        return '达到最大持仓天数'
    else:
        return sell_reason


def main():
    print('加载数据...')
    with open(TRADES_FILE) as f:
        trades = json.load(f)
    with open(CANDIDATES_FILE) as f:
        candidates_list = json.load(f)
    with open(NAV_FILE) as f:
        nav_list = json.load(f)

    # 建立索引
    cand_by_date = {}
    for c in candidates_list:
        cand_by_date[c['date']] = c

    nav_by_date = {}
    for n in nav_list:
        nav_by_date[n['date']] = n

    buys_by_date = defaultdict(list)
    sells_by_date = defaultdict(list)
    for t in trades:
        buys_by_date[t['buy_date']].append(t)
        sells_by_date[t['sell_date']].append(t)

    all_dates = sorted(set(
        list(nav_by_date.keys()) +
        list(cand_by_date.keys()) +
        list(buys_by_date.keys()) +
        list(sells_by_date.keys())
    ))

    if YEAR_FILTER:
        all_dates = [d for d in all_dates if d.startswith(YEAR_FILTER)]

    print(f'生成日志: {len(all_dates)}个交易日 (年份={YEAR_FILTER or "全部"})')

    # 恢复过滤开始前的持仓状态
    active_holdings = {}
    if YEAR_FILTER:
        filter_start = f'{YEAR_FILTER}-01-01'
        for t in trades:
            if t['buy_date'] < filter_start and t['sell_date'] >= filter_start:
                active_holdings[t['code']] = t

    lines = []

    # 头部配置说明
    lines.append('=' * 55)
    lines.append('  多指标评分量化系统 回测交易日志')
    lines.append('=' * 55)
    lines.append('回测区间: 2021-01-01 ~ 2026-06-30')
    lines.append('初始资金: 100万元')
    lines.append(f'持仓模式: {N_SLOTS}仓位并发持股（非单股持仓）')
    lines.append(f'每仓分配: 总资产 / {N_SLOTS} = 每仓金额')
    lines.append(f'买入规则: >=3星且至少1个trigger亮, 按(星数,评分)降序选股')
    lines.append(f'卖出规则: 止盈+{TP_PCT:.0f}% | 止损{SL_PCT:.0f}% | 最大持仓{MAX_HOLD}天')
    lines.append(f'滑点: 买入+{SLIPPAGE_BUY*100:.1f}%, 卖出-{SLIPPAGE_SELL*100:.1f}%')
    lines.append('=' * 55)
    if YEAR_FILTER:
        lines.append('')
        lines.append(f'[注] 本日志仅输出{YEAR_FILTER}年数据作为示例（全量3215笔交易覆盖2021~2026年）')
    lines.append('')
    lines.append('')

    for date in all_dates:
        day_has_content = False
        day_lines = []

        cand_info = cand_by_date.get(date)
        day_buys = buys_by_date.get(date, [])
        day_sells = sells_by_date.get(date, [])
        nav_info = nav_by_date.get(date)

        if not cand_info and not day_buys and not day_sells:
            continue

        day_lines.append(f'=== {date} ===')

        # 【候选股扫描】
        if cand_info and cand_info.get('candidates'):
            candidates = cand_info['candidates']
            selected = cand_info.get('selected', [])
            available = cand_info.get('available_slots', N_SLOTS)
            skipped = cand_info.get('skipped_full', [])

            day_lines.append(f'【候选股扫描】共{len(candidates)}只通过评分阈值 (可用仓位: {available})')
            for i, c in enumerate(candidates, 1):
                stars = star_display(c.get('star_count', 0))
                score = c.get('score', 0)
                code = c['code']
                strategy = c.get('strategy', '')
                price = c.get('buy_price', 0)
                sel_mark = ' [已选入]' if code in selected else (' [仓位满未选]' if code in skipped else '')
                day_lines.append(f'  {i}. {code} {stars} score={score:.1f} 策略={strategy} 参考价={format_price(price)}{sel_mark}')
            day_lines.append('')
            day_has_content = True

        # 【买入执行】
        if day_buys:
            day_lines.append(f'【买入执行】当日买入{len(day_buys)}只')
            for i, t in enumerate(day_buys, 1):
                code = t['code']
                buy_price = t['buy_price']
                shares = t['shares']
                amount = buy_price * shares
                score = t.get('score_at_buy', 0)
                star = t.get('star_count', 0)
                hour = t.get('buy_hour', '')
                tp_price = buy_price * (1 + TP_PCT / 100)
                sl_price = buy_price * (1 + SL_PCT / 100)

                day_lines.append(f'  买入{i}: {code} 以{hour} open价 {format_price(buy_price)}元买入, '
                                 f'{shares}股, 金额{amount:,.0f}元')
                day_lines.append(f'         评分: {star}星 score={score:.1f} | '
                                 f'止盈{format_price(tp_price)}(+{TP_PCT:.0f}%), '
                                 f'止损{format_price(sl_price)}({SL_PCT:.0f}%), '
                                 f'最长持{MAX_HOLD}天')
                active_holdings[code] = t
            day_lines.append('')
            day_has_content = True

        # 【当日卖出】
        if day_sells:
            day_lines.append(f'【当日卖出】卖出{len(day_sells)}只')
            for t in day_sells:
                code = t['code']
                sell_price = t['sell_price']
                hold_days = t['hold_days']
                pnl_pct = t['pnl_pct']
                pnl_amount = t['pnl_amount']
                sell_hour = t.get('sell_hour', '')
                reason = parse_sell_reason(t.get('sell_reason', ''))
                sell_reason_raw = t.get('sell_reason', '')

                pnl_sign = '+' if pnl_pct >= 0 else ''
                amt_sign = '+' if pnl_amount >= 0 else ''

                day_lines.append(f'  卖出: {code} 持仓{hold_days}天, {sell_hour}{reason}, '
                                 f'卖出价{format_price(sell_price)}')
                day_lines.append(f'        本次盈亏: {pnl_sign}{pnl_pct:.2f}% ({amt_sign}{pnl_amount:,.0f}元)')
                day_lines.append(f'        原因: {sell_reason_raw}')
                if code in active_holdings:
                    del active_holdings[code]
            day_lines.append('')
            day_has_content = True

        # 【持仓状态】
        if nav_info and day_has_content:
            nav_val = nav_info['nav']
            dd = nav_info.get('drawdown', 0)
            total_return = (nav_val / INITIAL_CAPITAL - 1) * 100
            occupied = len(active_holdings)

            day_lines.append(f'【持仓状态】{occupied}/{N_SLOTS}仓位占用, '
                             f'当前总资产: {nav_val:,.0f}元, '
                             f'累计收益: {total_return:+.2f}%, '
                             f'最大回撤: {dd:.2f}%')
            if active_holdings:
                day_lines.append(f'  当前持仓: {", ".join(sorted(active_holdings.keys()))}')
            day_lines.append('')

        if day_has_content:
            lines.extend(day_lines)
            lines.append('---')
            lines.append('')

    output_text = chr(10).join(lines)
    with open(OUTPUT_FILE, 'w', encoding='utf-8') as f:
        f.write(output_text)

    print(f'日志已生成: {OUTPUT_FILE}')
    print(f'总行数: {len(lines)}')
    print(f'涵盖交易日: {len(all_dates)}天')


if __name__ == '__main__':
    main()
