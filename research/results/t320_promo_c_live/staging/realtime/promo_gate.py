#!/usr/bin/env python3
"""晋级率过热门控 (Task#320, 用户批准方案C 2026-08-12)
=====================================================
昨日涨停晋级率 ≥ 阈值(30%) 的"过热日"暂停开新仓(买入端门控)。

口径铁律: 与回测侧完全同源 —— LU涨停集SQL逐字复制自
research/results/t311_promo_menu/t311_indicators.py (其承自t250_qa.py,
已QA 6/6 PASS的纯SQL链路; 涨停价round口径与trading_rules.limit_prices
的Decimal ROUND_HALF_UP经-0.001容差对齐, t161已验证):
  promo_rate(D) = D-1首板(D-1涨停∩D-2未涨停)中 D 再涨停占比

时序合规(零未来函数): 交易日T 9:25决策用 D=昨日(prev trade date),
所需stock_kline数据截至昨日, 全部来自昨日18:30日更, 与21:30候选生成
同一数据面。

fail-open铁律(与冰点overlay同哲学, 宁不拦截不拒单):
数据缺失/陈旧(>10自然日)/SQL异常 → 不拦截 + note留痕, 绝不炸决策主链。
回滚 = realtime/config.py PROMO_GATE_ENABLED=False 一行。
"""
import sqlite3
from datetime import datetime

# 研究口径涨停集SQL —— t311_indicators.py逐字复制(ST三重剔除+剔bj)
LU = ("""SELECT code FROM stock_kline WHERE date=? AND preclose>0 AND close>0
 AND isST=0 AND upper(code_name) NOT LIKE '%ST%' AND code_name NOT LIKE '%退%'
 AND code NOT LIKE 'bj.%'
 AND close >= round(preclose*(CASE WHEN code LIKE 'sz.30%' OR code LIKE 'sh.688%'
     OR code LIKE 'sh.689%' THEN 1.20 ELSE 1.10 END),2)-0.001""")


def _lu_set(conn, d: str) -> set:
    return {r[0] for r in conn.execute(LU, (d,))}


def compute_promo_rate(conn, D: str) -> dict:
    """promo_rate(D): D-1首板股在D再涨停的占比(t311口径)。

    返回 {'date','d1','d2','fb_count','promoted','promo_rate'};
    promo_rate=None 表示D-1无首板(t311 CSV中为空串, 门控按不触发处理)。
    历史交易日不足(D前不足2个交易日)返回None。
    """
    rows = conn.execute(
        "SELECT DISTINCT date FROM stock_kline WHERE date<? "
        "ORDER BY date DESC LIMIT 2", (D,)).fetchall()
    if len(rows) < 2:
        return None
    d1, d2 = rows[0][0], rows[1][0]
    fb = _lu_set(conn, d1) - _lu_set(conn, d2)
    if not fb:
        return {'date': D, 'd1': d1, 'd2': d2, 'fb_count': 0,
                'promoted': 0, 'promo_rate': None}
    promoted = len(fb & _lu_set(conn, D))
    return {'date': D, 'd1': d1, 'd2': d2, 'fb_count': len(fb),
            'promoted': promoted, 'promo_rate': promoted / len(fb)}


def check_promo_gate(conn, trade_date: str, threshold: float,
                     enabled: bool = True) -> dict:
    """交易日T 9:25决策前门控判定: promo_rate(昨日) ≥ threshold → 过热。

    返回(decision json全留痕):
      {'enabled','triggered','threshold','prev_date','d1','d2',
       'fb_count','promoted','promo_rate','promo_pct','note'}
    triggered=True 仅在计算成功且promo_rate≥threshold时; 其余一律False。
    """
    out = {'enabled': bool(enabled), 'triggered': False,
           'threshold': threshold, 'prev_date': None, 'd1': None, 'd2': None,
           'fb_count': None, 'promoted': None, 'promo_rate': None,
           'promo_pct': None, 'note': ''}
    if not enabled:
        out['note'] = '开关关闭(PROMO_GATE_ENABLED=False)'
        return out
    try:
        row = conn.execute("SELECT MAX(date) FROM stock_kline WHERE date<?",
                           (trade_date,)).fetchone()
        prev_date = row[0] if row else None
        if not prev_date:
            out['note'] = 'fail-open: 昨日日K缺失, 不拦截'
            return out
        days_gap = (datetime.strptime(trade_date, '%Y-%m-%d')
                    - datetime.strptime(prev_date, '%Y-%m-%d')).days
        if days_gap > 10:
            out['note'] = (f'fail-open: 日K陈旧(最新{prev_date}, '
                           f'距今{days_gap}天), 不拦截')
            return out
        r = compute_promo_rate(conn, prev_date)
        out['prev_date'] = prev_date
        if r is None:
            out['note'] = 'fail-open: 昨日前历史交易日不足, 不拦截'
            return out
        out.update({'d1': r['d1'], 'd2': r['d2'],
                    'fb_count': r['fb_count'], 'promoted': r['promoted'],
                    'promo_rate': (round(r['promo_rate'], 6)
                                   if r['promo_rate'] is not None else None)})
        if r['promo_rate'] is None:
            out['note'] = f'{prev_date}前一日无首板, 晋级率不适用, 不拦截'
            return out
        out['promo_pct'] = round(r['promo_rate'] * 100, 2)
        if r['promo_rate'] >= threshold:
            out['triggered'] = True
            out['note'] = (f"过热: 昨日({prev_date})晋级率"
                           f"{out['promo_pct']:.2f}%≥{threshold * 100:.0f}%"
                           f"({r['d1']}首板{r['fb_count']}只→"
                           f"{r['promoted']}只再板)")
        else:
            out['note'] = (f"正常: 昨日({prev_date})晋级率"
                           f"{out['promo_pct']:.2f}%<{threshold * 100:.0f}%")
    except (sqlite3.Error, ValueError, TypeError) as e:
        out['note'] = f'fail-open: 计算异常({e!r}), 不拦截'
    return out


if __name__ == '__main__':
    # 手工核验: python3 realtime/promo_gate.py [trade_date]
    import sys
    _td = sys.argv[1] if len(sys.argv) > 1 else \
        datetime.now().strftime('%Y-%m-%d')
    _conn = sqlite3.connect('file:/home/AIWealth/data/stocks.db?mode=ro',
                            uri=True)
    print(check_promo_gate(_conn, _td, 0.30))
    _conn.close()
