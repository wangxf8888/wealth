#!/usr/bin/env python3
"""Task#215 打板实验室(board_lab)采集器 — 从+3%起全程多指标采集.

设计纲领(用户原话): "从3开始跟踪, 有的缩回去了, 有的继续上攻到45679,
一步一步的过程对应的指标比如换手率的变化。有的股换手率比较低就能带来
几个点的增幅, 是不是这种比较强势? 你要找出很多指标结合的规律点。"

职责(只读行情+只写data/board_process与research区, 不发任何通知,
不碰realtime/生产链, 与shadow_surge_detector/shadow_tracker进程独立):
  1. 每分钟一轮腾讯qt批量快照扫全A(全市场代码=stocks.db最新交易日codes,
     约5200只sh/sz, 北交所无本地覆盖不在库故天然不含);
  2. 观察池准入: 当日涨幅(现价/昨收-1)首次>=+3.0%入池, 入池后每轮跟踪
     直至收盘(池外只算涨幅不落盘); 非活跃降频: pct回落<+2.0%后每5轮
     落盘1次(内存仍每轮更新, 阶梯/触板事件检测不降频);
  3. 每轮记录: px/pct/high/low/累计成交额/换手率累计/换手增速/量比近似/
     买一卖一价量/触板/封单量/开板次数;
  4. 阶梯事件: 首达+3/+4/.../+9/涨停时刻+全指标快照; 缩回事件(从阶梯N
     回落超过1%记shrink);
  5. 15:00收工终态标注: SEALED(收盘封死)/BROKEN(触板未封收)/
     FADED_x(最高到阶梯x后缩回)/PLATEAU(守住最高阶梯附近收盘);
  6. --backfill-outcome: 次日回填D+1开盘溢价/最高/收盘(读events, 查
     stocks.db次日日K), 供miner关联结局。

qt字段表(Task#195实测+Task#213在产交叉验证, tools/shadow_tracker.py):
  [1]名称 [3]现价 [4]昨收 [5]今开 [6]累计成交量(单位分板! 见下) [9]买一价 [10]买一量(手)
  [19]卖一价 [20]卖一量(手) [30]交易所时间戳 [33]最高 [34]最低
  [37]成交额(万) [38]换手率(%,若有) [44]流通市值(亿) [47]涨停价
  注: Task#215任务书写"[9]买一量/[19]卖一量"系价格位笔误, 量在[10]/[20]。
  qt[6]单位分板(Task#218/#219实测, amount/price对照法):
    主板sh60/sz00=手(茅台ratio=99.9) 创业板sz30=手(宁德ratio=99.7)
    北交所bj=手(万达轴承920002 ratio=99.5, 采集器universe无bj股仅备查)
    科创板sh688=股(中芯国际ratio=0.98) sh689 CDR=股(九号公司ratio=1.00)
    ——科创板全段sh68x不得再乘100!

换手率口径(三级fallback, 每行带turnover_src可审计):
  qt38: qt[38]直读; qt44: 累计量/(qt[44]*1e8/现价)推算(流通市值/现价=
  流通股本, 实时精确); db: 累计量/流通股本_db, 流通股本_db=stocks.db
  近10日内最近一天 volume/(turn/100) 换算并启动时缓存。
量比近似: 当日累计量 / (近5日日均量 * 已交易时间占比), 日均量启动时
  从stocks.db一次性算好缓存。

限速: 轮内请求间隔>=0.3s, 400只/请求约13请求/轮, 每轮60s;
数据源铁律: 全程只用腾讯qt快照, 零BaoStock(本文件禁止import baostock)。

落盘: data/board_process/proc_YYYYMMDD.jsonl (逐轮)
      data/board_process/events_YYYYMMDD.jsonl (阶梯/缩回/触板/封板/终态)
      data/board_process/observe_pool_summary_YYYYMMDD.json (实时刷新)
      data/board_process/outcomes_YYYYMMDD.json (--backfill-outcome产物)
磁盘: 预估约36MB/日, 单日>100MB置警告位(summary.disk_warn)并降级全池降频。

运行: nohup python3 -u tools/board_lab_collector.py >> logs/board_lab_collector.log 2>&1 &
     单轮实测: python3 tools/board_lab_collector.py --once
     结局回填: python3 tools/board_lab_collector.py --backfill-outcome
cron: 28 9 * * 1-5 (盘前就位, 内部自管时段+pid锁防重复)
     40 18 * * 1-5 --backfill-outcome (等18:30日K入库? 不: 回填读的是
     昨日events+今日已入库日K, 18:40时今日日K尚未入库(18:30启动更新),
     故回填目标=所有"次日日K已在库"的历史events, 幂等扫描补齐)。

Task#234判定修复(Kirk#232在8/7实测发现, 当日82只真封死中32只被误标BROKEN):
  1. 涨停价一律自算(calc_limit不再信qt[47]): 板块规则sh68/sz30→x1.2,
     ST/含退→x1.05, 其余→x1.1(bj不在universe), +EPS后round=交易所半进位
     口径(8/7实测主板preclose=3.75交易所涨停=4.13, 银行家舍入误得4.12);
     上市前5交易日无涨跌幅→limit_px=0不参与触板/封死判定(8/7 C嘉立创
     曾被误报touch_limit); at_limit改用相等判定|px-limit|<=1分钱
     (px超出自算价带=价带假设不成立, 如复牌无限制ST, 用>=会误报);
  2. 尾盘集合竞价期(14:57-15:00)qt盘口为竞价虚拟盘口(实测ask1_vol==
     bid1_vol镜像伪影), 不可用于封死判定→该窗口封死状态冻结沿用14:56值,
     盘口原始字段保留但行内加ob_auction=true标注竞价期语义;
  3. 15:00后(收盘补扫)封死判定=收盘快照价==自算涨停价, 不再依赖盘口;
     finalize终态SEALED同口径(final事件带final_basis/limit_px可审计)。
"""
import argparse
import glob
import json
import os
import re
import sqlite3
import sys
import time
import urllib.request
from datetime import datetime

# ---------------- 配置区 ----------------
BASE = '/home/AIWealth'
SDB = os.path.join(BASE, 'data', 'stocks.db')
OUT_DIR = os.path.join(BASE, 'data', 'board_process')

ENTRY_PCT = 3.0            # 观察池准入涨幅%
LADDERS = [3, 4, 5, 6, 7, 8, 9]   # 阶梯(+涨停单独作'limit'级)
SHRINK_GAP = 1.0           # 从阶梯N回落超过该幅度记shrink
INACTIVE_PCT = 2.0         # 回落至该涨幅以下=非活跃
INACTIVE_EVERY = 5         # 非活跃股每N轮落盘1次
ROUND_SECONDS = 60.0       # 每轮间隔
REQ_GAP = 0.3              # 轮内请求最小间隔秒(限速红线)
QT_BATCH = 400             # 单请求代码数(#195实测500 OK留边距)
QT_URL = 'https://qt.gtimg.cn/q='
SESSIONS = [('09:30:00', '11:30:00'), ('13:00:00', '15:00:00')]
EOD = '15:00:00'
AUCTION_START = '14:57:00'  # 尾盘集合竞价起点(qt盘口自此不可信, Task#234)
DISK_WARN_MB = 100         # 单日落盘超过即告警+全池降频
EPS = 1e-9
PRICE_EPS = 0.001          # 价格相等判定容差(1分钱内, 与trading_rules一致)

_QT_LINE = re.compile(r'v_([a-z]{2}\d{6})="([^"]*)"')
_last_req = [0.0]
_req_count = [0]


def log(msg):
    print(f'[{datetime.now().strftime("%H:%M:%S")}] {msg}', flush=True)


def _ro(path):
    return sqlite3.connect(f'file:{path}?mode=ro', uri=True)


def _get(url):
    wait = REQ_GAP - (time.time() - _last_req[0])
    if wait > 0:
        time.sleep(wait)
    req = urllib.request.Request(
        url, headers={'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; '
                                    'x64) AppleWebKit/537.36',
                      'Referer': 'https://gu.qq.com/'})
    raw = urllib.request.urlopen(req, timeout=10).read()
    _last_req[0] = time.time()
    _req_count[0] += 1
    return raw


# ---------------- 启动时静态缓存(stocks.db一次性加载) ----------------
def load_static():
    """返回 (codes排序list, {code:{name,preclose_db,float_shares,avg5_vol,
    no_limit}})。no_limit=上市前5交易日无涨跌幅(Task#234)。"""
    sc = _ro(SDB)
    latest = sc.execute('SELECT MAX(date) FROM stock_kline').fetchone()[0]
    dates10 = [r[0] for r in sc.execute(
        'SELECT DISTINCT date FROM stock_kline WHERE date<=? '
        'ORDER BY date DESC LIMIT 10', (latest,))]
    dates5 = dates10[:5]
    info = {}
    for code, name, close in sc.execute(
            'SELECT code, code_name, close FROM stock_kline WHERE date=?',
            (latest,)):
        info[code] = {'name': name or '', 'preclose_db': close or 0.0,
                      'float_shares': None, 'avg5_vol': None,
                      'no_limit': False}
    # 流通股本: 近10日内最近一天 volume/(turn/100) (turn缺失日跳过)
    ph = ','.join('?' * len(dates10))
    for code, date, vol, turn in sc.execute(
            f'SELECT code, date, volume, turn FROM stock_kline '
            f'WHERE date IN ({ph}) AND volume>0 AND turn>0 '
            f'ORDER BY code, date DESC', dates10):
        d = info.get(code)
        if d is not None and d['float_shares'] is None:
            d['float_shares'] = vol / (turn / 100.0)
    # 近5日日均成交量(股)
    ph5 = ','.join('?' * len(dates5))
    for code, avg in sc.execute(
            f'SELECT code, AVG(volume) FROM stock_kline '
            f'WHERE date IN ({ph5}) AND volume>0 GROUP BY code', dates5):
        if code in info:
            info[code]['avg5_vol'] = avg
    # 上市前5交易日无涨跌幅(Task#234): 首交易日落在库内最近4个交易日内
    # → 今日至多是其第5个交易日, 涨停价不可判定(limit_px=0)
    cut = dates10[3] if len(dates10) > 3 else dates10[-1]
    n_nl = 0
    for code, fd in sc.execute(
            'SELECT code, MIN(date) FROM stock_kline GROUP BY code'):
        d = info.get(code)
        if d is not None and fd and fd >= cut:
            d['no_limit'] = True
            n_nl += 1
    sc.close()
    codes = sorted(info)
    n_fs = sum(1 for d in info.values() if d['float_shares'])
    n_av = sum(1 for d in info.values() if d['avg5_vol'])
    log(f'[静态] 库最新日={latest} 全A={len(codes)}只 '
        f'流通股本覆盖={n_fs} 5日均量覆盖={n_av} 新股无涨跌幅={n_nl}')
    return codes, info, latest


# ---------------- qt解析 ----------------
def parse_qt(raw_gbk, local_hms):
    """qt原文 -> {code: 快照dict}; 坏行跳过不抛。"""
    out = {}
    for m in _QT_LINE.finditer(raw_gbk):
        sym, data = m.group(1), m.group(2)
        p = data.split('~')
        if len(p) < 40:
            continue
        # qt[6]量纲分板(Task#219): 科创板68x(含689 CDR)为股, 其余为手
        vol_mult = 1 if sym.startswith('sh68') else 100

        def f(i):
            try:
                return float(p[i]) if i < len(p) and p[i] else 0.0
            except ValueError:
                return 0.0
        px = f(3)
        preclose = f(4)
        if px <= 0 or preclose <= 0:
            continue
        raw_ts = p[30] if len(p) > 30 else ''
        if len(raw_ts) >= 14 and raw_ts[:14].isdigit():
            hms = f'{raw_ts[8:10]}:{raw_ts[10:12]}:{raw_ts[12:14]}'
        else:
            hms = local_hms
        out[f'{sym[:2]}.{sym[2:]}'] = {
            'name': p[1], 'px': px, 'preclose': preclose, 'open': f(5),
            'cum_shares': f(6) * vol_mult,       # ->股(688原生股,其余手x100)
            'bid1_px': f(9), 'bid1_vol': f(10) * 100,
            'ask1_px': f(19), 'ask1_vol': f(20) * 100,
            'ts': hms, 'high': f(33), 'low': f(34),
            'amount_wan': f(37), 'turn_qt': f(38),
            'float_mv_yi': f(44), 'limit_qt': f(47),
        }
    return out


def calc_limit(code, name, preclose, no_limit=False):
    """涨停价自算(Task#234, 板规与trading_rules.py同源, 不再信qt[47]):
    sh68/sz30→x1.2; ST/含退→x1.05; 其余→x1.1(bj不在universe)。
    +EPS后round=交易所半进位口径(8/7实测preclose=3.75主板涨停=4.13,
    Python银行家舍入round(4.125,2)误得4.12)。
    no_limit(上市前5交易日无涨跌幅)→返回0表示不可判定。"""
    if no_limit or preclose <= 0:
        return 0.0
    body = code.split('.')[1]
    if body.startswith(('30', '68')):
        rate = 0.2
    elif 'ST' in str(name or '').upper() or '退' in str(name or ''):
        rate = 0.05
    else:
        rate = 0.1
    return round(preclose * (1 + rate) + EPS, 2)


def traded_minutes(hms):
    """当前时刻已交易分钟数(9:30-11:30/13:00-15:00, 竞价不计)。"""
    m = 0.0
    if hms > '09:30:00':
        m += _mins('09:30:00', min(hms, '11:30:00'))
    if hms > '13:00:00':
        m += _mins('13:00:00', min(hms, '15:00:00'))
    return max(m, 1.0)


def _mins(a, b):
    ah, am, asec = int(a[:2]), int(a[3:5]), int(a[6:8])
    bh, bm, bsec = int(b[:2]), int(b[3:5]), int(b[6:8])
    return max(0, (bh * 60 + bm + bsec / 60.0) - (ah * 60 + am + asec / 60.0))


def judge_limit_state(px, limit_px, ts, prev_sealed, ask1_px, ask1_vol):
    """(at_limit, sealed_now)三时段判定(Task#234核心, 独立函数便于单测):
    - at_limit: 相等判定|px-limit|<=1分钱(limit_px=0即无涨跌幅→恒False);
    - ts<14:57 连续竞价: 封死=at_limit且卖一空(盘口可信);
    - 14:57<=ts<15:00 尾盘集合竞价: qt盘口为竞价虚拟盘口(ask1_vol==bid1_vol
      镜像伪影, 8/7实测32只真封死被误判炸板)→封死状态冻结沿用14:56值;
    - ts>=15:00 收盘后(补扫): 封死=收盘价==自算涨停价, 不依赖盘口。"""
    at_limit = limit_px > 0 and abs(px - limit_px) <= PRICE_EPS
    if ts >= EOD:
        sealed_now = at_limit
    elif ts >= AUCTION_START:
        sealed_now = prev_sealed and at_limit
    else:
        sealed_now = at_limit and (ask1_px <= 0 or ask1_vol <= 0)
    return at_limit, sealed_now


# ---------------- 单股状态 ----------------
class StockState:
    def __init__(self, code, name, preclose, limit_px, static):
        self.code = code
        self.name = name
        self.preclose = preclose
        self.limit_px = limit_px
        self.float_shares = static.get('float_shares')
        self.avg5_vol = static.get('avg5_vol')
        self.entry_ts = None
        self.rounds = 0            # 入池后经历轮数
        self.last_write_round = -999
        self.prev_turn = None      # 上轮换手率累计(增速用)
        self.ladder_hits = {}      # {level(int或'limit'): ts}
        self.shrunk = {}           # {level: ts}
        self.max_pct = -99.0
        self.touched = False
        self.touch_ts = None
        self.sealed = False        # 当前是否封死
        self.seal_ts = None
        self.open_cnt = 0          # 开板(炸板)次数累计
        self.last_row = None       # 最近一轮完整指标行(终态引用)
        self.final_state = None

    def max_ladder(self):
        if 'limit' in self.ladder_hits:
            return 'limit'
        lv = [k for k in self.ladder_hits if isinstance(k, int)]
        return max(lv) if lv else None


# ---------------- 采集主体 ----------------
class Collector:
    def __init__(self, date_str, test_mode=False):
        self.date = date_str
        ymd = date_str.replace('-', '')
        os.makedirs(OUT_DIR, exist_ok=True)
        pfx = 'test_' if test_mode else ''
        self.proc_path = os.path.join(OUT_DIR, f'{pfx}proc_{ymd}.jsonl')
        self.ev_path = os.path.join(OUT_DIR, f'{pfx}events_{ymd}.jsonl')
        self.sum_path = os.path.join(
            OUT_DIR, f'{pfx}observe_pool_summary_{ymd}.json')
        self.codes, self.static, self.db_latest = load_static()
        self.chunks = [self.codes[i:i + QT_BATCH]
                       for i in range(0, len(self.codes), QT_BATCH)]
        self.pool = {}             # {code: StockState}
        self.round_no = 0
        self.bytes_written = 0
        self.disk_warn = False
        self.started_at = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        self.req_fail = 0
        log(f'[启动] board_lab采集器 date={date_str} 全A={len(self.codes)}只 '
            f'{len(self.chunks)}请求/轮 间隔>={REQ_GAP}s')

    # ---- 一轮扫描 ----
    def scan_round(self):
        self.round_no += 1
        local = datetime.now().strftime('%H:%M:%S')
        quotes = {}
        nok = 0
        for chunk in self.chunks:
            url = QT_URL + ','.join(c.replace('.', '') for c in chunk)
            try:
                raw = _get(url).decode('gbk', errors='replace')
                quotes.update(parse_qt(raw, local))
                nok += 1
            except Exception as e:
                self.req_fail += 1
                log(f'  [WARN] 请求失败({len(chunk)}只): {e}')
        self.process(quotes, local)
        return nok, len(quotes)

    def process(self, quotes, local_hms):
        rows, events = [], []
        for code, q in quotes.items():
            pct = (q['px'] / q['preclose'] - 1) * 100
            st = self.pool.get(code)
            if st is None:
                if pct < ENTRY_PCT:
                    continue
                stat = self.static.get(code, {})
                st = StockState(code, q['name'] or stat.get('name', ''),
                                q['preclose'],
                                calc_limit(code, q['name'], q['preclose'],
                                           stat.get('no_limit', False)), stat)
                st.entry_ts = q['ts']
                self.pool[code] = st
                events.append(self._event('pool_entry', st, q, pct, None))
            st.rounds += 1
            # 触板/封死判定先行(row需反映本轮实时状态, 不降频);
            # Task#234三时段判定见judge_limit_state
            at_limit, sealed_now = judge_limit_state(
                q['px'], st.limit_px, q['ts'], st.sealed,
                q['ask1_px'], q['ask1_vol'])
            in_auction = AUCTION_START <= q['ts'] < EOD
            row = self._metrics(st, q, pct, at_limit, sealed_now, in_auction)
            st.last_row = row
            if at_limit and not st.touched:
                st.touched = True
                st.touch_ts = q['ts']
                events.append(self._event('touch_limit', st, q, pct, row))
            if sealed_now and not st.sealed:
                st.sealed = True
                st.seal_ts = q['ts']
                events.append(self._event(
                    're_seal' if st.open_cnt else 'seal', st, q, pct, row))
            elif not sealed_now and st.sealed:
                st.sealed = False
                st.open_cnt += 1
                events.append(self._event('break_seal', st, q, pct, row))
            # 阶梯首达(现价口径快照语义; 盘中瞬时高点由high字段留痕)
            for lv in LADDERS:
                if lv not in st.ladder_hits and pct >= lv:
                    st.ladder_hits[lv] = q['ts']
                    events.append(self._event(f'ladder_{lv}', st, q, pct,
                                              row, ladder=lv))
            if at_limit and 'limit' not in st.ladder_hits:
                st.ladder_hits['limit'] = q['ts']
                events.append(self._event('ladder_limit', st, q, pct, row,
                                          ladder='limit'))
            # 缩回: 已达阶梯N后回落超过SHRINK_GAP
            for lv, ts0 in list(st.ladder_hits.items()):
                if lv == 'limit':
                    continue
                if lv not in st.shrunk and pct <= lv - SHRINK_GAP:
                    st.shrunk[lv] = q['ts']
                    events.append(self._event('shrink', st, q, pct, row,
                                              ladder=lv))
            st.max_pct = max(st.max_pct, pct)
            # 落盘降频: 非活跃(pct<+2%)每INACTIVE_EVERY轮写1次;
            # 磁盘告警后全池降频
            every = 1
            if pct < INACTIVE_PCT or self.disk_warn:
                every = INACTIVE_EVERY
            if st.rounds - st.last_write_round >= every:
                st.last_write_round = st.rounds
                rows.append(row)
        self._flush(rows, events)

    def _metrics(self, st, q, pct, at_limit, sealed_now, in_auction=False):
        """一轮完整指标行。"""
        # 换手率三级fallback
        if q['turn_qt'] > 0:
            turn, src = q['turn_qt'], 'qt38'
        elif q['float_mv_yi'] > 0:
            turn = q['cum_shares'] / (q['float_mv_yi'] * 1e8 / q['px']) * 100
            src = 'qt44'
        elif st.float_shares:
            turn, src = q['cum_shares'] / st.float_shares * 100, 'db'
        else:
            turn, src = None, 'none'
        speed = (round(turn - st.prev_turn, 4)
                 if turn is not None and st.prev_turn is not None else None)
        if turn is not None:
            st.prev_turn = turn
        vr = None
        if st.avg5_vol:
            frac = traded_minutes(q['ts']) / 240.0
            vr = q['cum_shares'] / (st.avg5_vol * max(frac, 1 / 240))
        row = {
            'ts': q['ts'], 'round': self.round_no, 'code': st.code,
            'name': st.name, 'px': q['px'],
            'pct': round(pct, 2),
            'high': q['high'], 'low': q['low'],
            'high_pct': round((q['high'] / st.preclose - 1) * 100, 2)
            if q['high'] > 0 else None,
            'amount_wan': q['amount_wan'],
            'turnover_pct': round(turn, 4) if turn is not None else None,
            'turnover_src': src,
            'turn_speed': speed,
            'vol_ratio': round(vr, 3) if vr is not None else None,
            'bid1_px': q['bid1_px'], 'bid1_vol': q['bid1_vol'],
            'ask1_px': q['ask1_px'], 'ask1_vol': q['ask1_vol'],
            'at_limit': bool(at_limit),
            'sealed': bool(sealed_now),
            'seal_vol': q['bid1_vol'] if sealed_now else None,
            'open_cnt': st.open_cnt,
            'ladder': st.max_ladder(),
        }
        if in_auction:
            # Task#234: 尾盘竞价期bid1/ask1/seal_vol为竞价虚拟盘口值,
            # 不代表连续竞价封单, 消费方须按此语义使用
            row['ob_auction'] = True
        return row

    def _event(self, etype, st, q, pct, row, ladder=None):
        return {'type': etype, 'ts': q['ts'], 'code': st.code,
                'name': st.name, 'ladder': ladder,
                'pct': round(pct, 2), 'round': self.round_no,
                'snapshot': row}

    def _flush(self, rows, events):
        for path, items in ((self.proc_path, rows), (self.ev_path, events)):
            if not items:
                continue
            with open(path, 'a', encoding='utf-8') as f:
                for r in items:
                    line = json.dumps(r, ensure_ascii=False) + '\n'
                    f.write(line)
                    self.bytes_written += len(line.encode('utf-8'))
        if (not self.disk_warn
                and self.bytes_written > DISK_WARN_MB * 1024 * 1024):
            self.disk_warn = True
            log(f'[ALERT] 单日落盘超{DISK_WARN_MB}MB, 全池降频至'
                f'每{INACTIVE_EVERY}轮1写')
        self.write_summary(finished=False)

    def write_summary(self, finished):
        dist = {}
        for st in self.pool.values():
            k = str(st.max_ladder())
            dist[k] = dist.get(k, 0) + 1
        finals = {}
        for st in self.pool.values():
            if st.final_state:
                k = ('FADED' if st.final_state.startswith('FADED')
                     else st.final_state)
                finals[k] = finals.get(k, 0) + 1
        summary = {
            'date': self.date, 'started_at': self.started_at,
            'updated_at': datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
            'round_no': self.round_no, 'finished': finished,
            'universe': len(self.codes), 'db_latest': self.db_latest,
            'pool_size': len(self.pool),
            'ladder_distribution': dist,
            'n_touched': sum(1 for s in self.pool.values() if s.touched),
            'n_sealed_now': sum(1 for s in self.pool.values() if s.sealed),
            'n_broken_ever': sum(1 for s in self.pool.values()
                                 if s.open_cnt > 0),
            'final_states': finals or None,
            'req_total': _req_count[0], 'req_fail': self.req_fail,
            'req_gap_s': REQ_GAP,
            'bytes_written': self.bytes_written,
            'disk_warn': self.disk_warn,
        }
        tmp = self.sum_path + '.tmp'
        with open(tmp, 'w', encoding='utf-8') as f:
            json.dump(summary, f, ensure_ascii=False, indent=1)
        os.replace(tmp, self.sum_path)
        return summary

    # ---- 收工终态 ----
    def finalize(self):
        events = []
        ts = datetime.now().strftime('%H:%M:%S')
        for st in self.pool.values():
            row = st.last_row or {}
            pct = row.get('pct', st.max_pct)
            px = row.get('px') or 0.0
            # Task#234: 终态SEALED=收盘快照价==自算涨停价(相等判定),
            # 不依赖尾盘竞价期盘口ask量(8/7曾32只真封死被误标BROKEN)
            sealed_eod = (st.limit_px > 0
                          and abs(px - st.limit_px) <= PRICE_EPS)
            if sealed_eod:
                st.final_state = 'SEALED'
            elif st.touched:
                st.final_state = 'BROKEN'
            else:
                ml = st.max_ladder()
                if ml is None:
                    st.final_state = 'FADED_3' if pct < ENTRY_PCT - SHRINK_GAP \
                        else 'PLATEAU'
                elif pct is not None and pct <= (
                        ml if isinstance(ml, int) else 10) - SHRINK_GAP:
                    st.final_state = f'FADED_{ml}'
                else:
                    st.final_state = 'PLATEAU'
            events.append({'type': 'final', 'ts': ts, 'code': st.code,
                           'name': st.name, 'final_state': st.final_state,
                           'final_basis': 'close_px==selfcalc_limit',
                           'limit_px': st.limit_px,
                           'max_pct': round(st.max_pct, 2),
                           'ladder': st.max_ladder(),
                           'touch_ts': st.touch_ts, 'seal_ts': st.seal_ts,
                           'open_cnt': st.open_cnt,
                           'entry_ts': st.entry_ts,
                           'ladder_hits': {str(k): v for k, v
                                           in st.ladder_hits.items()},
                           'shrunk': {str(k): v for k, v
                                      in st.shrunk.items()},
                           'round': self.round_no, 'snapshot': st.last_row})
        self._flush([], events)
        s = self.write_summary(finished=True)
        log(f'[收工] 池={s["pool_size"]} 阶梯分布={s["ladder_distribution"]} '
            f'触板={s["n_touched"]} 终态={s["final_states"]} '
            f'请求={s["req_total"]}(失败{s["req_fail"]}) '
            f'落盘={s["bytes_written"] / 1048576:.1f}MB')


# ---------------- 时段控制 ----------------
def in_session(hms):
    return any(a <= hms < b for a, b in SESSIONS)


def live_loop(col):
    while True:
        now = datetime.now().strftime('%H:%M:%S')
        if now >= EOD:
            break
        if not in_session(now):
            time.sleep(20)
            continue
        t0 = time.time()
        nok, nq = col.scan_round()
        pool_active = sum(1 for s in col.pool.values()
                          if s.last_row and s.last_row['pct'] >= INACTIVE_PCT)
        log(f'轮{col.round_no}: 请求成功{nok}/{len(col.chunks)} 快照{nq} '
            f'池{len(col.pool)}(活跃{pool_active}) '
            f'封板{sum(1 for s in col.pool.values() if s.sealed)}')
        time.sleep(max(0.0, ROUND_SECONDS - (time.time() - t0)))
    # 15:00后补一轮定格收盘值再终态标注
    log('[EOD] 15:00到点, 补扫一轮定格收盘值')
    try:
        col.scan_round()
    except Exception as e:
        log(f'[WARN] 收盘补扫失败: {e}')
    col.finalize()


# ---------------- --once 单轮实测 ----------------
def run_once():
    date_str = datetime.now().strftime('%Y-%m-%d')
    col = Collector(date_str, test_mode=True)
    nok, nq = col.scan_round()
    s = col.write_summary(finished=False)
    log(f'[once] 请求成功{nok}/{len(col.chunks)} 快照解析={nq} '
        f'池={s["pool_size"]} 阶梯分布={s["ladder_distribution"]}')
    # 抽2只人工核对: 换手率主口径 vs db流通股本口径交叉验证
    picked = sorted((s for s in col.pool.values() if s.last_row),
                    key=lambda x: -(x.last_row.get('turnover_pct') or 0))[:2]
    for st in picked:
        r = st.last_row
        fs_db = st.float_shares
        turn_db = None
        if fs_db and r.get('amount_wan') and r.get('px'):
            cum_est = r['amount_wan'] * 1e4 / r['px']   # 均价近似累计股数
            turn_db = round(cum_est / fs_db * 100, 3)
        log(f'  [核对] {st.code} {st.name}: 主口径turn={r["turnover_pct"]}%'
            f'(src={r["turnover_src"]}) db口径近似={turn_db}% '
            f'db流通股本={fs_db and int(fs_db)} '
            f'px={r["px"]} pct={r["pct"]}% vol_ratio={r["vol_ratio"]} '
            f'bid1_vol={r["bid1_vol"]} ask1_vol={r["ask1_vol"]}')
    log(f'[once] 产物: {col.proc_path} / {col.ev_path} / {col.sum_path}')
    log(f'[once] 请求消耗={_req_count[0]} (测试期预算<30)')


# ---------------- --backfill-outcome ----------------
def backfill_outcome():
    """扫描所有无outcomes的events日, 次日日K已入库则回填(幂等)。"""
    sc = _ro(SDB)
    done = 0
    for ev_path in sorted(glob.glob(os.path.join(OUT_DIR, 'events_*.jsonl'))):
        ymd = os.path.basename(ev_path)[7:15]
        d = f'{ymd[:4]}-{ymd[4:6]}-{ymd[6:]}'
        out_path = os.path.join(OUT_DIR, f'outcomes_{ymd}.json')
        if os.path.exists(out_path):
            continue
        nxt = sc.execute('SELECT MIN(date) FROM stock_kline WHERE date>?',
                         (d,)).fetchone()[0]
        if not nxt:
            log(f'[outcome] {d}: 次日日K未入库, 跳过')
            continue
        codes = set()
        finals = {}
        with open(ev_path, encoding='utf-8') as f:
            for line in f:
                try:
                    e = json.loads(line)
                except ValueError:
                    continue
                codes.add(e['code'])
                if e.get('type') == 'final':
                    finals[e['code']] = e
        outcomes = {}
        for code in sorted(codes):
            r0 = sc.execute(
                'SELECT close FROM stock_kline WHERE date=? AND code=?',
                (d, code)).fetchone()
            r1 = sc.execute(
                'SELECT open, high, low, close, preclose FROM stock_kline '
                'WHERE date=? AND code=?', (nxt, code)).fetchone()
            if not r1 or not r1[4]:
                outcomes[code] = {'d1_missing': True}
                continue
            o, h, lo, c, pc = r1
            outcomes[code] = {
                'd_close': r0[0] if r0 else None,
                'd1_date': nxt,
                'd1_open_premium_pct': round((o / pc - 1) * 100, 2),
                'd1_high_pct': round((h / pc - 1) * 100, 2),
                'd1_low_pct': round((lo / pc - 1) * 100, 2),
                'd1_close_pct': round((c / pc - 1) * 100, 2),
                'final_state': (finals.get(code) or {}).get('final_state'),
            }
        with open(out_path, 'w', encoding='utf-8') as f:
            json.dump({'date': d, 'd1_date': nxt,
                       'generated_at': datetime.now().strftime(
                           '%Y-%m-%d %H:%M:%S'),
                       'outcomes': outcomes}, f, ensure_ascii=False, indent=1)
        log(f'[outcome] {d} -> {nxt}: 回填{len(outcomes)}只 => {out_path}')
        done += 1
    sc.close()
    if not done:
        log('[outcome] 无待回填日(全部已回填或次日数据未入库)')


# ---------------- 入口 ----------------
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--once', action='store_true', help='单轮实测(test_前缀落盘)')
    ap.add_argument('--backfill-outcome', action='store_true')
    args = ap.parse_args()
    if args.backfill_outcome:
        backfill_outcome()
        return
    if args.once:
        run_once()
        return
    date_str = datetime.now().strftime('%Y-%m-%d')
    if datetime.now().weekday() >= 5:
        log('[退出] 非交易日(周末)')
        return
    # pid锁防重复(cron 9:28 + 手工nohup并存场景)
    lock = os.path.join(OUT_DIR, f'.collector_lock_{date_str.replace("-", "")}')
    os.makedirs(OUT_DIR, exist_ok=True)
    if os.path.exists(lock):
        try:
            old = int(open(lock).read().strip())
            os.kill(old, 0)
            log(f'[退出] 已有采集进程pid={old}在跑')
            return
        except (ValueError, ProcessLookupError, PermissionError):
            pass
    with open(lock, 'w') as f:
        f.write(str(os.getpid()))
    try:
        col = Collector(date_str)
        live_loop(col)
    finally:
        try:
            os.remove(lock)
        except OSError:
            pass


if __name__ == '__main__':
    main()
    main()
