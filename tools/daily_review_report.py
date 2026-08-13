#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""[Task#72] 晚间复盘报告生成器 —— 每日双报告体系之"事后复盘"。

生成 reports/YYYYMMDD_review.md, 六大区块:
  1. 大盘形势: 上证(index_kline权威)+深成/创业板(腾讯fqkline展示级) 当日+5日
  2. 情绪温度: 涨停家数/炸板率/最高连板/红盘占比 5日走势(t70_ladder_lib引擎级同源口径)
  3. 板块轮转: stock_industry×当日涨幅 TOP10/BOTTOM5 + 连续强势 + 新启动 + 板内涨停
  4. 连板梯队: 首板/2板/3板+名单与晋级率(t70口径)
  5. 账户复盘: decision json vs positions/closed_trades vs 结果, 自动检出
     冰点/熔断/踏空/卖出执行事件 + 持仓浮盈浮亏
  6. 心得教训: 规则事件自动叙述 + 明日机会点(候选预览, 依赖22:00候选已生成)

架构决策(Task#72任务书授权): cron定22:10单段生成(22:00候选生成后),
不搞19:30+22:00两段拼接 —— 单一产物单一时点, 降低一致性风险。

数据纪律: 全部取自当日盘后可见数据(stock_kline/index_kline当日行+各json),
无未来数据; BaoStock封禁期hour列缺失等如实标注不报错; 任一区块失败
只降级该区块并标注, 不阻断整报告(报告链路独立于交易链路)。

用法:
  python3 tools/daily_review_report.py                 # 今日(需当日日K已入库)
  python3 tools/daily_review_report.py --date 2026-07-28   # 历史日重放
"""
import argparse
import json
import os
import sqlite3
import sys
import urllib.request
from datetime import datetime

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT_ROOT)
sys.path.insert(0, os.path.join(PROJECT_ROOT, 'scripts'))

# 策略名中文主导(用户指令2026-07-30), 导入失败时降级原名显示
try:
    from realtime.config import strategy_display_name
except Exception:
    def strategy_display_name(name):
        return name

DB = '/home/AIWealth/data/stocks.db'
RT_DIR = '/home/AIWealth/data/realtime'
REPORT_DIR = '/home/AIWealth/reports'
BAN_STATUS = '/home/AIWealth/data/baostock_ban_status.json'
ALERT_LOG = '/home/AIWealth/logs/realtime/scheduler_alerts.log'

N_TREND_DAYS = 5      # 走势展示天数
LADDER_WINDOW = 30    # 梯队/情绪计算加载窗(交易日, 保证streak高度<=窗长)


# =====================================================================
# 公共工具
# =====================================================================

def trading_dates_until(conn, date, n):
    """<=date 的最近n个交易日(升序)。"""
    rows = conn.execute(
        "SELECT DISTINCT date FROM stock_kline WHERE date<=? "
        "ORDER BY date DESC LIMIT ?", (date, n)).fetchall()
    return sorted(r[0] for r in rows)


def _load_json(path):
    if not os.path.exists(path):
        return None
    with open(path, encoding='utf-8') as f:
        return json.load(f)


def _fmt_pct(v, digits=2):
    return f"{v:+.{digits}f}%" if v is not None else "N/A"


def section(fn):
    """区块级降级装饰器: 单区块异常不阻断整报告。"""
    def wrapper(*a, **kw):
        try:
            return fn(*a, **kw)
        except Exception as e:
            return f"\n⚠️ 本区块生成失败(已降级): {type(e).__name__}: {e}\n"
    return wrapper


# =====================================================================
# 1. 大盘形势
# =====================================================================

def _fetch_tencent_index(symbol, n=8):
    """腾讯fqkline取指数近n日 [(date, close, chg_pct)]。展示级, 失败返回None。"""
    url = (f'https://web.ifzq.gtimg.cn/appstock/app/fqkline/get'
           f'?param={symbol},day,,,{n},qfq')
    req = urllib.request.Request(url, headers={
        'User-Agent': 'Mozilla/5.0', 'Referer': 'https://gu.qq.com/'})
    data = json.loads(urllib.request.urlopen(req, timeout=10).read())
    node = data['data'][symbol]
    klines = node.get('qfqday') or node.get('day') or []
    out, prev = [], None
    for k in klines:
        c = float(k[2])
        chg = (c / prev - 1) * 100 if prev else None
        out.append((k[0], c, chg))
        prev = c
    return out


@section
def sec_market(conn, date):
    lines = ["## 一、大盘形势\n"]
    # 上证: index_kline权威源(含今晨Task事件后已根治的降级回补链路)
    rows = conn.execute(
        "SELECT date, close, close_rate, red_ratio FROM index_kline "
        "WHERE code='sh.000001' AND date<=? ORDER BY date DESC LIMIT ?",
        (date, N_TREND_DAYS)).fetchall()
    rows = sorted(rows)
    if not rows or rows[-1][0] != date:
        lines.append(f"⚠️ index_kline缺{date}行(daily_update未跑完?), 上证仅展示既有数据\n")
    lines.append("**上证综指**(index_kline权威源):\n")
    lines.append("| 日期 | 收盘 | 涨跌幅 | 红盘占比 |")
    lines.append("|---|---|---|---|")
    for d, c, cr, rr in rows:
        mark = " ←今日" if d == date else ""
        lines.append(f"| {d}{mark} | {c:.2f} | {_fmt_pct(cr)} | "
                     f"{rr if rr is not None else 'N/A'}% |")
    # 深成指/创业板指: 库内无维护(sz.399001已废弃), 腾讯展示级补充
    lines.append("\n**深成指/创业板指**(腾讯源展示级, 库内不落地):\n")
    for sym, name in (('sz399001', '深成指'), ('sz399006', '创业板指')):
        try:
            k = _fetch_tencent_index(sym)
            k = [x for x in k if x[0] <= date][-N_TREND_DAYS:]
            seq = ' | '.join(f"{d[5:]} {_fmt_pct(chg)}" for d, _, chg in k)
            today = next((x for x in k if x[0] == date), None)
            head = (f"{name} {today[1]:.2f} ({_fmt_pct(today[2])})"
                    if today else f"{name} 当日数据未取到")
            lines.append(f"- **{head}** | 近5日: {seq}")
        except Exception as e:
            lines.append(f"- {name}: 腾讯源获取失败({type(e).__name__}), 降级跳过")
    return '\n'.join(lines) + '\n'


# =====================================================================
# 2. 情绪温度 + 4. 连板梯队 (共享t70标注)
# =====================================================================

def load_ladder_df(conn, date):
    """t70_ladder_lib引擎级同源口径加载标注(涨停/炸板/连板streak)。"""
    import t70_ladder_lib as t70
    dates = trading_dates_until(conn, date, LADDER_WINDOW)
    df = t70.load_annotated(dates[0], date)
    # t70 COLS不含close_rate, 从preclose派生(与stock_kline库内rate同义)
    df['close_rate'] = ((df['close'] / df['preclose'] - 1) * 100).where(
        df['preclose'] > 0)
    return df, dates


@section
def sec_emotion(conn, date, df, dates):
    lines = ["## 二、市场情绪温度(引擎级同源口径: t70_ladder_lib)\n"]
    lines.append("| 日期 | 涨停(非ST) | 炸板率 | 最高连板 | 红盘占比 | 冰点(<35) |")
    lines.append("|---|---|---|---|---|---|")
    rr_map = dict(conn.execute(
        "SELECT date, red_ratio FROM index_kline WHERE code='sh.000001' "
        "AND date>=? AND date<=?", (dates[-N_TREND_DAYS], date)))
    for d in dates[-N_TREND_DAYS:]:
        day = df[df['date'] == d]
        lu = day[day['is_lu'] & ~day['st']]
        n_lu = len(lu)
        # 炸板: 盘中触及涨停价但收盘未封(非ST)
        touched = day[~day['st'] & day['high'].notna()
                      & (day['high'] >= day['lp'] - 0.001)]
        n_touch = len(touched)
        zb_rate = (1 - n_lu / n_touch) * 100 if n_touch else 0.0
        max_h = int(lu['streak'].max()) if n_lu else 0
        rr = rr_map.get(d)
        ice = '❄️触发' if n_lu < 35 else '-'
        mark = ' ←今日' if d == date else ''
        lines.append(f"| {d}{mark} | {n_lu} | {zb_rate:.1f}% | {max_h}板 | "
                     f"{rr if rr is not None else 'N/A'}% | {ice} |")
    # 温度一句话
    day = df[df['date'] == date]
    lu = day[day['is_lu'] & ~day['st']]
    n_lu = len(lu)
    mood = ('冰点(涨停<35, 明日新仓×0.3)' if n_lu < 35 else
            '偏冷' if n_lu < 60 else '中性' if n_lu < 100 else '偏热')
    lines.append(f"\n**今日温度判定: {mood}** (涨停{n_lu}家)\n")
    return '\n'.join(lines) + '\n'


@section
def sec_ladder(conn, date, df, dates):
    lines = ["## 三、连板梯队与晋级率\n"]
    idx = dates.index(date)
    prev_d = dates[idx - 1] if idx >= 1 else None
    day = df[(df['date'] == date) & df['is_lu'] & ~df['st']]
    prev = (df[(df['date'] == prev_d) & df['is_lu'] & ~df['st']]
            if prev_d else day.iloc[0:0])

    def names(sub, cap=None):
        rows = sub.sort_values('turn', ascending=False)
        items = [f"{r.code_name}({r.code.split('.')[1]})"
                 for r in rows.itertuples()]
        if cap and len(items) > cap:
            return '、'.join(items[:cap]) + f" 等{len(items)}只"
        return '、'.join(items) if items else '无'

    for k, label, cap in ((1, '首板', 10), (2, '2板', None), (3, '3板+', None)):
        sub = day[day['streak'] == k] if k < 3 else day[day['streak'] >= 3]
        lines.append(f"- **{label}: {len(sub)}只** — {names(sub, cap)}")
    # 晋级率: 昨日k板 → 今日k+1板
    lines.append("\n**晋级率**(昨日→今日):\n")
    today_by_code = dict(zip(day['code'], day['streak']))
    for k in (1, 2, 3):
        base = prev[prev['streak'] == k]
        if not len(base):
            lines.append(f"- {k}板→{k+1}板: 昨日无{k}板, N/A")
            continue
        promoted = sum(1 for c in base['code']
                       if today_by_code.get(c, 0) == k + 1)
        lines.append(f"- {k}板→{k+1}板: {promoted}/{len(base)} "
                     f"= {promoted/len(base)*100:.1f}%")
    return '\n'.join(lines) + '\n'


# =====================================================================
# 3. 板块轮转
# =====================================================================

@section
def sec_sector(conn, date, df, dates):
    import pandas as pd
    lines = ["## 四、板块轮转(申万行业×当日全A涨幅)\n"]
    ind = pd.read_sql(
        "SELECT code, industry FROM stock_industry "
        "WHERE industry IS NOT NULL AND industry != ''", conn)
    win_dates = dates[-6:]  # 今日+前5日(连续强势/新启动用)
    sub = df[df['date'].isin(win_dates) & df['close_rate'].notna()].merge(
        ind, on='code')
    if not len(sub[sub['date'] == date]):
        return "## 四、板块轮转\n⚠️ 当日无行业匹配数据\n"
    # 每日板块中位涨幅与排名
    g = (sub.groupby(['date', 'industry'])
         .agg(chg=('close_rate', 'median'), n=('code', 'count'),
              lu=('is_lu', 'sum')).reset_index())
    g = g[g['n'] >= 5]  # 过滤样本过小板块
    g['rank'] = g.groupby('date')['chg'].rank(ascending=False)
    today = g[g['date'] == date].sort_values('chg', ascending=False)
    n_ind = today['industry'].nunique()

    lines.append(f"**涨幅榜TOP10**(中位涨幅, 板块数={n_ind}):\n")
    lines.append("| 板块 | 中位涨幅 | 板内涨停 | 家数 |")
    lines.append("|---|---|---|---|")
    for r in today.head(10).itertuples():
        lines.append(f"| {r.industry} | {_fmt_pct(r.chg)} | {int(r.lu)} | {int(r.n)} |")
    lines.append("\n**跌幅榜BOTTOM5**:\n")
    for r in today.tail(5).sort_values('chg').itertuples():
        lines.append(f"- {r.industry} {_fmt_pct(r.chg)} (涨停{int(r.lu)}/{int(r.n)}家)")

    # 连续强势: 连续N日排名前10
    strong = []
    for indus in today.head(10)['industry']:
        n_cons = 0
        for d in reversed(win_dates):
            rk = g[(g['date'] == d) & (g['industry'] == indus)]['rank']
            if len(rk) and rk.iloc[0] <= 10:
                n_cons += 1
            else:
                break
        if n_cons >= 2:
            strong.append(f"{indus}(连续{n_cons}日)")
    lines.append(f"\n- **连续强势板块**(≥2日居前10): "
                 f"{'、'.join(strong) if strong else '无'}")

    # 今日新启动: 昨日排名后50%今日进前10
    if len(win_dates) >= 2:
        prev_d = win_dates[-2]
        prev_rank = g[g['date'] == prev_d].set_index('industry')['rank']
        half = prev_rank.max() / 2 if len(prev_rank) else 0
        started = [i for i in today.head(10)['industry']
                   if prev_rank.get(i, 0) > half]
        lines.append(f"- **今日新启动板块**(昨日后50%→今日前10): "
                     f"{'、'.join(started) if started else '无'}")
    return '\n'.join(lines) + '\n'


# =====================================================================
# 5. 账户复盘
# =====================================================================

@section
def sec_account(conn, date):
    lines = ["## 五、账户复盘(决策 vs 成交 vs 结果)\n"]
    dec = _load_json(os.path.join(RT_DIR, f'decision_{date.replace("-", "")}.json'))
    pos_data = _load_json(os.path.join(RT_DIR, 'positions.json')) or {}
    positions = pos_data.get('positions', [])
    closed = pos_data.get('closed_trades', [])
    account = pos_data.get('account', {})
    events = []

    # --- 当日决策 ---
    if dec is None:
        lines.append(f"⚠️ 无 decision_{date} 文件(当日9:25决策未跑或非交易日)\n")
        events.append("当日无决策文件——若为交易日则9:25链路未执行, 需排查")
        recs = []
    elif dec.get('status') == 'aborted_quote_anomaly':
        lines.append("🚨 **当日决策被行情源异常熔断中止**(status=aborted_quote_anomaly)\n")
        events.append("行情源异常熔断触发, 全日未开新仓(Task#38防线, 属保护性事件)")
        recs = []
    elif dec.get('market_filter'):
        lines.append(f"🛑 **当日被大盘过滤拦截**: {dec['market_filter']}\n")
        events.append("大盘熔断触发全日不开新仓(旧规则, Task#68后已改冰点减仓)")
        recs = []
    else:
        ps = dec.get('position_scale') or {}
        if ps.get('scale', 1.0) != 1.0:
            events.append(f"❄️ 冰点减仓生效: 昨日涨停{ps.get('limitup_cnt')}家"
                          f"<35 → 新仓金额×{ps['scale']}")
        recs = []
        lines.append("| Slot | 策略 | 候选 | 命中 | 建议执行 |")
        lines.append("|---|---|---|---|---|")
        for sid in sorted(dec.get('strategies', {})):
            s = dec['strategies'][sid]
            rlist = s.get('recommendations', [])
            for r in rlist:
                r['_slot'] = sid
            recs.extend(rlist)
            rtxt = '、'.join(f"{r['code']}{r.get('name','')}"
                             f"@{r.get('buy_price')}" for r in rlist) or '-'
            lines.append(f"| {sid} | {strategy_display_name(s.get('strategy','?'))} | "
                         f"{s.get('total_candidates',0)} | "
                         f"{len(s.get('meet_condition',[]))} | {rtxt} |")

    # --- 成交对照(建议 vs 实际持仓/平仓) ---
    bought = {p['code'] for p in positions if p.get('buy_date') == date}
    bought |= {t['code'] for t in closed if t.get('buy_date') == date}
    for r in recs:
        if r['code'] not in bought:
            events.append(f"⚠️ 踏空检出: {r['_slot']} 建议 {r['code']} "
                          f"{r.get('name','')} 未见成交(排查confirm_buy日志)")
    if recs and all(r['code'] in bought for r in recs):
        lines.append(f"\n✅ 决策→成交一致: {len(recs)}笔建议全部成交")
    if not recs and bought:
        events.append(f"⚠️ 无建议但出现当日买入{bought}(异常, 需排查)")

    # --- 当日卖出 ---
    sold = [t for t in closed if t.get('sell_date') == date]
    if sold:
        lines.append("\n**今日平仓**:\n")
        for t in sold:
            lines.append(f"- {t['code']} {t.get('name','')} "
                         f"[{t.get('slot_id')}:{strategy_display_name(t.get('strategy'))}] "
                         f"{t.get('buy_date')}买{t.get('buy_price')} → "
                         f"卖{t.get('sell_price')} ({t.get('sell_reason')}) "
                         f"盈亏 {_fmt_pct(t.get('pnl_pct'))} "
                         f"¥{t.get('pnl_amount', 0):+,.0f}")
    else:
        lines.append("\n今日无平仓。")

    # --- 在手持仓浮盈浮亏(当日收盘价盯市) ---
    if positions:
        lines.append("\n**在手持仓浮盈浮亏**(当日收盘盯市):\n")
        for p in positions:
            row = conn.execute(
                "SELECT close FROM stock_kline WHERE code=? AND date=?",
                (p['code'], date)).fetchone()
            if row and row[0] and p.get('buy_price'):
                fl = (row[0] / p['buy_price'] - 1) * 100
                lines.append(f"- {p['code']} {p.get('name','')} "
                             f"[{p.get('slot_id')}] 成本{p['buy_price']} "
                             f"现价{row[0]} 浮动 {_fmt_pct(fl)}")
            else:
                lines.append(f"- {p['code']} {p.get('name','')} 当日收盘价缺失")
    else:
        lines.append("\n当前空仓。")

    lines.append(f"\n**账户**: 总净值 ¥{account.get('total_nav', 0):,.0f} | "
                 f"现金 ¥{account.get('cash', 0):,.0f} | "
                 f"累计已实现盈亏 ¥{account.get('realized_pnl', 0):+,.0f}")
    return '\n'.join(lines) + '\n', events


# =====================================================================
# 6. 心得教训 + 机会点
# =====================================================================

@section
def sec_execution(date):
    """[Task#81接口] 执行质量段: 读execution_quality_YYYYMMDD.json的
    md_section(schema_version=1, Bill 15:10 cron生成), 缺失静默跳过。"""
    eq = _load_json(f"/home/AIWealth/logs/realtime/"
                    f"execution_quality_{date.replace('-', '')}.json")
    if not eq or eq.get('schema_version') != 1:
        return ''
    return '\n' + eq.get('md_section', '')


def _next_trading_hint(date):
    """candidates json里 trade_date>date 的最近一份(明日机会点)。"""
    best = None
    for fn in os.listdir(RT_DIR):
        if fn.startswith('candidates_') and fn.endswith('.json'):
            d = fn[11:19]
            dd = f"{d[:4]}-{d[4:6]}-{d[6:]}"
            if dd > date and (best is None or dd < best[0]):
                best = (dd, os.path.join(RT_DIR, fn))
    return best


@section
def sec_lessons(conn, date, events):
    lines = ["## 六、心得教训与明日机会点\n"]
    # 规则事件自动叙述
    lines.append("**今日规则事件**:\n")
    if events:
        for e in events:
            lines.append(f"- {e}")
    else:
        lines.append("- 无特殊规则事件, 系统按既定纪律运行")
    # 心理仪表三数字(Task#73 strategy_health.json schema_version=1, Felix接口约定)
    health = _load_json('/home/AIWealth/logs/realtime/strategy_health.json') or {}
    psy = health.get('psychology') or {}
    if psy and health.get('schema_version') == 1:
        dd = psy.get('drawdown') or {}
        sigma = psy.get('deviation_30d_sigma')
        lines.append("\n**心理仪表(策略健康度监控)**:\n")
        lines.append(f"- 连败: {psy.get('consecutive_losses', '?')}笔 | "
                     f"回撤: {dd.get('drawdown_pct', '?')}% "
                     f"[{dd.get('level_desc', '?')}] 仓位系数×{dd.get('scale', '?')}"
                     + (" ⛔已停机" if dd.get('halt') else "")
                     + (f" | 30日实盘偏离 {sigma:+.2f}σ"
                        if isinstance(sigma, (int, float)) else
                        " | 30日偏离: 窗口无成交"))
    # 当日scheduler告警回放(夜间告警无人响应教训的制度化: 每晚复盘必读)
    if os.path.exists(ALERT_LOG):
        with open(ALERT_LOG, encoding='utf-8') as f:
            todays = [ln.strip() for ln in f
                      if date in ln.split(']')[0].lstrip('[')]
        if todays:
            lines.append(f"\n**今日scheduler告警({len(todays)}条, 必须逐条闭环)**:\n")
            for ln in todays[-8:]:
                lines.append(f"- `{ln}`")
    # 数据降级如实标注
    ban = _load_json(BAN_STATUS) or {}
    if not ban.get('recovered', True):
        n_null = conn.execute(
            "SELECT COUNT(*) FROM stock_kline WHERE date=? "
            "AND hour1_close IS NULL", (date,)).fetchone()[0]
        lines.append(f"\n**数据降级标注**: BaoStock封禁中"
                     f"(banned_at {ban.get('banned_at')}), 当日日K为腾讯降级源, "
                     f"hour列缺失{n_null}行, 分钟线未更新; 恢复后需重补。")
    # 明日机会点: 候选预览(22:00生成后)
    nxt = _next_trading_hint(date)
    if nxt:
        cand = _load_json(nxt[1]) or {}
        lines.append(f"\n**明日机会点**({nxt[0]} 候选预览, 详见早间计划报告):\n")
        for sid in sorted(cand.get('strategies', {})):
            s = cand['strategies'][sid]
            cs = s.get('candidates', [])
            top = '、'.join(f"{c['code'].split('.')[1]}{c.get('name','')}"
                            for c in cs[:3])
            lines.append(f"- {sid} {strategy_display_name(s.get('strategy_name','?'))}: "
                         f"{len(cs)}只候选" + (f" (前3: {top})" if cs else ""))
    else:
        lines.append("\n**明日机会点**: 候选文件尚未生成(22:00 cron后可用), "
                     "本段降级——明晨8:45计划报告将完整给出。")
    return '\n'.join(lines) + '\n'


# =====================================================================
# main
# =====================================================================

def generate(date):
    conn = sqlite3.connect(DB)
    # 交易日校验
    row = conn.execute("SELECT COUNT(*) FROM stock_kline WHERE date=?",
                       (date,)).fetchone()
    if not row[0]:
        print(f"[review] {date} 无日K数据(非交易日或daily_update未完成), 不生成")
        conn.close()
        return None

    parts = [f"# 📋 每日复盘报告 — {date}",
             f"\n> 生成时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')} | "
             f"数据口径: 当日盘后可见 | Task#72双报告体系\n"]
    parts.append(sec_market(conn, date))
    try:
        df, dates = load_ladder_df(conn, date)
    except Exception as e:
        df, dates = None, None
        parts.append(f"⚠️ 梯队标注加载失败, 情绪/梯队/板块区块降级: {e}\n")
    if df is not None:
        parts.append(sec_emotion(conn, date, df, dates))
        parts.append(sec_ladder(conn, date, df, dates))
        parts.append(sec_sector(conn, date, df, dates))
    acct = sec_account(conn, date)
    if isinstance(acct, tuple):
        acct_md, events = acct
    else:                      # 区块降级时返回str
        acct_md, events = acct, []
    parts.append(acct_md)
    parts.append(sec_execution(date))          # Task#81执行质量段(缺失跳过)
    parts.append(sec_lessons(conn, date, events))
    conn.close()

    os.makedirs(REPORT_DIR, exist_ok=True)
    out = os.path.join(REPORT_DIR, f"{date.replace('-', '')}_review.md")
    tmp = out + '.tmp'
    with open(tmp, 'w', encoding='utf-8') as f:
        f.write('\n'.join(parts))
    os.replace(tmp, out)
    _update_index()
    print(f"[review] 已生成: {out}")
    return out


def _update_index():
    """维护 reports/index.json 供前端列表(新→旧)。"""
    files = sorted((f for f in os.listdir(REPORT_DIR)
                    if f.endswith(('.md',)) and not f.startswith('.')),
                   reverse=True)
    with open(os.path.join(REPORT_DIR, 'index.json'), 'w',
              encoding='utf-8') as f:
        json.dump({'files': files,
                   'updated_at': datetime.now().strftime('%Y-%m-%d %H:%M:%S')},
                  f, ensure_ascii=False, indent=1)


def main():
    ap = argparse.ArgumentParser(description='晚间复盘报告(Task#72)')
    ap.add_argument('--date', default=None, help='历史日重放, 默认今日')
    args = ap.parse_args()
    date = args.date or datetime.now().strftime('%Y-%m-%d')
    try:
        generate(date)
        return 0
    except Exception as e:
        # 报告链路独立: 失败只告警不抛出非零影响调用方语义之外的东西
        msg = f"复盘报告生成失败 {date}: {type(e).__name__}: {e}"
        print(f"[review] {msg}")
        try:
            with open(ALERT_LOG, 'a', encoding='utf-8') as f:
                f.write(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] "
                        f"⚠️ [REPORT] {msg}\n")
        except OSError:
            pass
        return 1


if __name__ == '__main__':
    sys.exit(main())
