#!/usr/bin/env python3
"""Task#218 板块/概念涨停分布fetcher — 各板块今日涨停家数/占比/板块涨幅排行.

路径选择实测依据(2026-08-07本机复验, 详见research/results/t218_api_onboard/):
  - 首选push2 clist板块接口: 两轮实测均被RST(14:05探针/14:15复测,
    Gene t216同现象), 本机不可用 → 弃;
  - 备选定型: 腾讯qt全市场快照(在产同款, board_lab_collector口径)
    + stocks.db既有成分映射(stock_industry 129行业/stock_concept 489概念,
    akshare来源2026-07-25/29更新)自算板块聚合
    + 东财push2ex getTopicZTPool(实测可达99ms/18KB)交叉校验总数并富化
    连板数lbc/首封时间fbt/东财行业hybk。
  - ZTPool不可作唯一源: 仅含涨停股无板块全成分, 算不出lu_ratio分母与
    板块涨幅; 且含北交所而本地宇宙零BSE覆盖, 口径需qt侧对齐。

涨停判定(与sector_emotion/QA体系同口径): 现价>=涨停价-0.001, 涨停价优先
  qt[47]直读, 缺失时trading_rules自算(qt[4]昨收+名称ST判定)。盘中采样语义
  ="此刻封板", 收盘后(15:00+)采样=收盘涨停(权威口径)。ST股单列不计主榜。

输出: data/sector_limitup/sector_YYYYMMDD_HHMM.json
  {meta, industry:[全部行业按涨停数排序], concept_top:[涨停数>0概念Top30],
   st_limitup:[], ztpool_crosscheck:{}}

限速: 腾讯~16请求/轮(400只/批, 间隔0.3s在产同级), 东财1请求/轮(ZTPool)。
时段守卫: 交易日9:30-15:05(午休11:31-12:59跳过); 非交易日经指数时戳自检退出。
数据源铁律: 零BaoStock(本文件禁止import baostock)。
cron: */30 9-15 * * 1-5 (内部守卫兜底, 详见部署diff)。
"""
import argparse
import json
import os
import re
import sqlite3
import sys
import time
import urllib.request
from datetime import datetime

sys.path.insert(0, '/home/AIWealth')
import trading_rules

# ---------------- 配置区 ----------------
BASE = '/home/AIWealth'
SDB = os.path.join(BASE, 'data', 'stocks.db')
OUT_DIR = os.path.join(BASE, 'data', 'sector_limitup')
QT_URL = 'https://qt.gtimg.cn/q='
ZTPOOL_URL = ('http://push2ex.eastmoney.com/getTopicZTPool'
              '?ut=7eea3edcaed734bea9cbfc24409ed989&dpt=wz.ztzt'
              '&Pageindex=0&pagesize=500&sort=fbt%3Aasc&date={date}')
QT_BATCH = 400             # 单请求代码数(board_lab在产同级)
REQ_GAP = 0.3              # 腾讯轮内请求最小间隔秒(在产红线)
EM_REQ_GAP = 2.1           # 东财请求间隔(<0.5QPS)
CONCEPT_TOP_N = 30         # 概念榜Top N(489概念全量无意义)
# 泛义/交易属性概念黑名单(首采实测发现其霸榜无题材信息量, 按前缀匹配)
CONCEPT_BLACKLIST = ('融资融券', '沪股通', '深股通', '标准普尔', '富时罗素',
                     'MSCI', '证金持股', '转融券', '昨日涨停', '昨日连板',
                     '昨日高振幅', '昨日触板', '机构重仓', '基金重仓',
                     '预盈预增', '预亏预减', 'QFII重仓', '社保重仓',
                     '共同富裕示范区', '国产降会师概念')
UA = ('Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36')

_QT_LINE = re.compile(r'v_([a-z]{2}\d{6})="([^"]*)"')
_last_req = [0.0]
_req_count = {'qt': 0, 'em': 0}


def log(msg):
    print(f'[{datetime.now().strftime("%H:%M:%S")}] {msg}', flush=True)


def _get(url, gap, referer, kind):
    wait = gap - (time.time() - _last_req[0])
    if wait > 0:
        time.sleep(wait)
    req = urllib.request.Request(url, headers={'User-Agent': UA,
                                               'Referer': referer})
    raw = urllib.request.urlopen(req, timeout=10).read()
    _last_req[0] = time.time()
    _req_count[kind] += 1
    return raw


# ---------------- 静态数据(stocks.db) ----------------
def load_static():
    """返回 (codes, {code: name}, {code: [行业]}, {code: [(概念码,概念名)]})"""
    sc = sqlite3.connect(f'file:{SDB}?mode=ro', uri=True)
    latest = sc.execute('SELECT MAX(date) FROM stock_kline').fetchone()[0]
    names = dict(sc.execute(
        'SELECT code, code_name FROM stock_kline WHERE date=?', (latest,)))
    industry = {}
    for code, ind in sc.execute('SELECT code, industry FROM stock_industry'):
        if ind:
            industry.setdefault(code, []).append(ind)
    concept = {}
    for code, ccode, cname in sc.execute(
            'SELECT code, concept_code, concept_name FROM stock_concept'):
        concept.setdefault(code, []).append((ccode, cname))
    sc.close()
    codes = sorted(c for c in names if c[:2] in ('sh', 'sz'))
    return codes, names, industry, concept


# ---------------- 腾讯qt全市场快照 ----------------
def fetch_market(codes):
    """返回 {code: {'name','price','preclose','pct','limit_up','is_lu','is_st'}}
    qt字段位(board_lab在产口径): [1]名称 [3]现价 [4]昨收 [47]涨停价"""
    out = {}
    for i in range(0, len(codes), QT_BATCH):
        batch = [c.replace('.', '') for c in codes[i:i + QT_BATCH]]
        try:
            raw = _get(QT_URL + ','.join(batch), REQ_GAP,
                       'https://gu.qq.com/', 'qt')
        except Exception as e:
            log(f'[WARN] qt批次{i // QT_BATCH}失败: {e!r}')
            continue
        for sym, s in _QT_LINE.findall(raw.decode('gbk', 'replace')):
            p = s.split('~')
            if len(p) < 48:
                continue
            try:
                price, preclose = float(p[3]), float(p[4])
            except ValueError:
                continue
            if price <= 0 or preclose <= 0:
                continue   # 停牌/无行情
            code = sym[:2] + '.' + sym[2:]
            name = p[1]
            is_st = trading_rules.is_st_name(name)
            try:
                lu = float(p[47])
            except (ValueError, IndexError):
                lu = 0.0
            if lu <= 0:
                lu = trading_rules.limit_prices(code, preclose, is_st)[0]
            out[code] = {
                'name': name, 'price': price, 'preclose': preclose,
                'pct': round((price / preclose - 1) * 100, 2),
                'limit_up': lu,
                'is_lu': lu > 0 and price >= lu - 0.001,
                'is_st': is_st,
            }
    return out


# ---------------- 东财ZTPool交叉校验 ----------------
def fetch_ztpool(date_str):
    """返回 {6位码: {'name','lbc'连板数,'fbt'首封时间,'hybk'东财行业}}; 失败{}"""
    try:
        raw = _get(ZTPOOL_URL.format(date=date_str), EM_REQ_GAP,
                   'https://quote.eastmoney.com/', 'em')
        pool = (json.loads(raw.decode('utf-8')).get('data') or {}).get(
            'pool') or []
        return {x['c']: {'name': x.get('n', ''), 'lbc': x.get('lbc'),
                         'fbt': x.get('fbt'), 'hybk': x.get('hybk', ''),
                         'm': x.get('m')} for x in pool}
    except Exception as e:
        log(f'[WARN] 东财ZTPool失败(降级为无交叉校验): {e!r}')
        return {}


# ---------------- 聚合 ----------------
def aggregate(market, mapping, zt, min_members=8):
    """按板块聚合。mapping: {code: [板块名]}。返回按涨停数/板块涨幅排序列表。
    min_members: 成分不足N只的板块不产行(与sector_emotion同口径)。"""
    sectors = {}
    for code, info in market.items():
        if info['is_st']:
            continue   # ST单列不入板块榜
        for sec in mapping.get(code, []):
            s = sectors.setdefault(sec, {'n': 0, 'pct_sum': 0.0, 'lu': []})
            s['n'] += 1
            s['pct_sum'] += info['pct']
            if info['is_lu']:
                z = zt.get(code[3:], {})
                s['lu'].append({'code': code, 'name': info['name'],
                                'pct': info['pct'],
                                'streak': z.get('lbc'),
                                'first_seal': z.get('fbt')})
    rows = []
    for sec, s in sectors.items():
        if s['n'] < min_members:
            continue
        s['lu'].sort(key=lambda x: -(x['streak'] or 0))
        rows.append({
            'sector': sec,
            'n_members': s['n'],
            'n_limit_up': len(s['lu']),
            'lu_ratio_pct': round(len(s['lu']) / s['n'] * 100, 2),
            'sector_pct': round(s['pct_sum'] / s['n'], 2),   # 等权板块涨幅
            'limit_up_stocks': s['lu'],
        })
    rows.sort(key=lambda r: (-r['n_limit_up'], -r['sector_pct']))
    return rows


def market_open_guard():
    """时段+交易日守卫: 9:30-15:05(午休跳过); 指数时戳日期!=今日视为非交易日。"""
    hm = datetime.now().strftime('%H%M')
    if not ('0930' <= hm <= '1505') or '1131' <= hm <= '1259':
        return False, f'非采样时段({hm})'
    try:
        raw = _get(QT_URL + 'sh000001', REQ_GAP, 'https://gu.qq.com/', 'qt')
        m = _QT_LINE.search(raw.decode('gbk', 'replace'))
        ts = m.group(2).split('~')[30] if m else ''
        if ts[:8] != datetime.now().strftime('%Y%m%d'):
            return False, f'指数时戳{ts[:8]}!=今日(非交易日/数据未开)'
    except Exception as e:
        return False, f'指数守卫请求失败: {e!r}'
    return True, ''


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--force', action='store_true',
                    help='跳过时段守卫(手动验证用)')
    args = ap.parse_args()

    if not args.force:
        ok, why = market_open_guard()
        if not ok:
            log(f'守卫退出: {why}')
            return 0

    t0 = time.time()
    codes, names, industry, concept = load_static()
    log(f'宇宙{len(codes)}只(sh/sz), 行业映射{len(industry)}, '
        f'概念映射{len(concept)}')

    market = fetch_market(codes)
    lu_all = {c: v for c, v in market.items() if v['is_lu']}
    lu_st = {c: v for c, v in lu_all.items() if v['is_st']}
    log(f'快照{len(market)}只有行情, 涨停{len(lu_all)}(含ST{len(lu_st)})')

    today = datetime.now().strftime('%Y%m%d')
    zt = fetch_ztpool(today)
    # 交叉校验: qt自算涨停(非ST, sh/sz) vs ZTPool(含BJ, 剔BJ后对齐)
    zt_shsz = {c for c, v in zt.items() if not c.startswith(('92', '43',
                                                             '83', '87'))}
    qt_lu = {c[3:] for c, v in lu_all.items() if not v['is_st']}
    crosscheck = {
        'ztpool_total': len(zt),
        'ztpool_shsz': len(zt_shsz),
        'qt_selfcalc_shsz_nonst': len(qt_lu),
        'both': len(qt_lu & zt_shsz),
        'qt_only': sorted(qt_lu - zt_shsz),    # 多为ZTPool未及刷新/炸板瞬间
        'ztpool_only': sorted(zt_shsz - qt_lu),  # 多为盘中炸板(池含曾涨停)
    }

    ind_rows = aggregate(market, industry, zt)
    # 概念映射键含bj.前缀行, 聚合天然只命中market内sh/sz; 黑名单剔泛义概念
    cpt_map = {c: [f'{n}({k})' for k, n in v
                   if not any(n.startswith(b) for b in CONCEPT_BLACKLIST)]
               for c, v in concept.items()}
    cpt_rows = [r for r in aggregate(market, cpt_map, zt)
                if r['n_limit_up'] > 0][:CONCEPT_TOP_N]

    out = {
        'meta': {
            'ts': datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
            'session': ('close' if datetime.now().strftime('%H%M') >= '1500'
                        else 'intraday'),
            'source_path': ('腾讯qt全市场快照+stocks.db成分映射自算; '
                            '东财ZTPool交叉校验+连板/首封富化 '
                            '(clist被RST不可用, 实测依据见t218报告)'),
            'universe': len(codes), 'quoted': len(market),
            'n_limit_up_nonst': len(lu_all) - len(lu_st),
            'n_limit_up_st': len(lu_st),
            'requests': dict(_req_count),
            'elapsed_s': round(time.time() - t0, 1),
        },
        'ztpool_crosscheck': crosscheck,
        'industry': ind_rows,
        'concept_top': cpt_rows,
        'st_limitup': [{'code': c, 'name': v['name'], 'pct': v['pct']}
                       for c, v in sorted(lu_st.items())],
    }
    os.makedirs(OUT_DIR, exist_ok=True)
    path = os.path.join(OUT_DIR,
                        f'sector_{datetime.now().strftime("%Y%m%d_%H%M")}.json')
    with open(path, 'w') as f:
        json.dump(out, f, ensure_ascii=False, indent=1)
    top = [(r['sector'], r['n_limit_up']) for r in ind_rows[:5]]
    log(f'落盘 {path} | 行业Top5涨停: {top} | 请求 {dict(_req_count)} '
        f'| 耗时{out["meta"]["elapsed_s"]}s')
    return 0


if __name__ == '__main__':
    sys.exit(main())
