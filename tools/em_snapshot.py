#!/usr/bin/env python3
"""Task#218 东财push2个股实时快照热备模块 — 腾讯qt主源的备源能力封装.

定位: 独立模块, 不侵入生产。realtime/data_feed.py的主备切换骨架
     (DUAL_SOURCE_ENABLED, 默认关闭)在需要时调用本模块。

端点(Task#216 Gene实测 + Task#218本机复验 2026-08-07):
  1. /api/qt/ulist.np/get  批量快照(在产同款端点结构; fltt=2 → 价格字段
     直接返回元两位小数, 免÷100换算)
  2. /api/qt/stock/get     单股全字段(含五档价量; 批量端点无买卖档量,
     需档量时用本接口点查)
  域级failover(t218实测): push2.eastmoney.com主域对本机IP存在短时连接级
  限流(初期可通/连续请求后RST, Gene t216同现象), push2delay.eastmoney.com
  同结构可通且A股字段为实时——14:19:58请求f124行情时戳=14:19:58, 现价与
  腾讯qt逐位一致(600519/000001/300750两轮对照), "delay"仅限国际指数。
  故HOSTS按序尝试: push2→push2delay, 单批内切换。

字段映射(ulist.np, fltt=2实测验证):
  f2现价 f3涨跌幅% f5成交量(手) f6成交额(元) f12代码 f13市场(0深/1沪)
  f14名称 f15最高 f16最低 f17今开 f18昨收 f31买一价 f32卖一价
  注: ulist无买一/卖一档量字段(f34/f35是内外盘, f20是总市值, 与stock/get
  同名字段语义不同, 本机实测确认), 档量走fetch_eastmoney_depth点查。
字段映射(stock/get, fltt=2实测验证):
  f43现价 f44最高 f45最低 f46今开 f47量(手) f48额(元) f57代码 f58名称
  f60昨收 f19/f20买一价/量(手) f39/f40卖一价/量(手)

输出与腾讯qt解析同构(morning_decision._fetch_from_tencent超集):
  {code: {'name','price','preclose','open','high','low','volume','amount',
          'pct','bid1','bid1_vol','ask1','ask1_vol','limit_up','limit_down',
          'src'}}  volume单位手, amount单位元; 涨停价由preclose+trading_rules
  自算(含ST/创科/北交所差异), 与项目内自算交叉校验口径一致。

限速: 默认请求间隔2.1s(<0.5QPS, Task#218红线口径); 生产激活热备时可经
     req_gap参数调至0.5~1.0s(腾讯qt在产QPS≤1同级)。
数据源铁律: 零BaoStock(本文件禁止import baostock)。
"""
import json
import time
import urllib.request

import trading_rules

# ---------------- 配置区 ----------------
HOSTS = ['push2.eastmoney.com', 'push2delay.eastmoney.com']  # 按序failover
ULIST_PATH = ('/api/qt/ulist.np/get'
              '?secids={secids}&fields=f2,f3,f5,f6,f12,f13,f14,f15,f16,f17,'
              'f18,f31,f32&fltt=2&invt=2')
STOCK_GET_PATH = ('/api/qt/stock/get'
                  '?secid={secid}&invt=2&fltt=2'
                  '&ut=fa5fd1943c7b386f172d6893dbfba10b')
BATCH_SIZE = 50        # 单请求代码数(在产腾讯qt同级50, push2实测300+OK留边距)
DEFAULT_REQ_GAP = 2.1  # 请求最小间隔秒(<0.5QPS红线; 生产热备可调低)
TIMEOUT = 10
UA = ('Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 '
      '(KHTML, like Gecko) Chrome/120.0 Safari/537.36')
REFERER = 'https://quote.eastmoney.com/'

_last_req = [0.0]
_req_count = [0]


# ---------------- 代码格式转换 ----------------
def code_to_secid(code: str) -> str:
    """sh.600519→1.600519 / sz.000001→0.000001 / bj.920008→0.920008
    (东财secid: 沪=1, 深/北交所=0; 兼容sh600519无点格式)"""
    c = code.replace('.', '')
    mkt, num = c[:2].lower(), c[2:]
    return ('1.' if mkt == 'sh' else '0.') + num


def secid_diff_to_code(item: dict) -> str:
    """ulist响应diff项(f12代码+f13市场)→项目内码 sh.600519"""
    prefix = 'sh' if item.get('f13') == 1 else ('bj' if str(
        item.get('f12', '')).startswith(('92', '43', '83', '87')) else 'sz')
    return f"{prefix}.{item.get('f12')}"


# ---------------- HTTP(限速+域failover内建) ----------------
def _get(path: str, req_gap: float) -> bytes:
    """按HOSTS顺序尝试, 全部失败抛最后一个异常。"""
    last_err = None
    for host in HOSTS:
        wait = req_gap - (time.time() - _last_req[0])
        if wait > 0:
            time.sleep(wait)
        req = urllib.request.Request(
            'http://' + host + path,
            headers={'User-Agent': UA, 'Referer': REFERER})
        try:
            raw = urllib.request.urlopen(req, timeout=TIMEOUT).read()
            _last_req[0] = time.time()
            _req_count[0] += 1
            return raw
        except Exception as e:
            _last_req[0] = time.time()
            _req_count[0] += 1
            last_err = e
    raise last_err


def _f(v, default=0.0):
    """东财字段值容错: '-'/None/非数→default"""
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


# ---------------- 核心接口 ----------------
def fetch_eastmoney_snapshot(codes: list, req_gap: float = DEFAULT_REQ_GAP,
                             batch_size: int = BATCH_SIZE) -> dict:
    """批量实时快照(ulist.np)。codes=项目内码列表(sh.600519格式)。

    返回 {code: dict} 与腾讯qt解析同构; 批量端点无档量,
    bid1_vol/ask1_vol恒为None(需档量用fetch_eastmoney_depth点查)。
    单批失败不影响其余批次(与qt fetcher同容错语义)。
    """
    results = {}
    for i in range(0, len(codes), batch_size):
        batch = codes[i:i + batch_size]
        secids = ','.join(code_to_secid(c) for c in batch)
        try:
            raw = _get(ULIST_PATH.format(secids=secids), req_gap)
            diff = (json.loads(raw.decode('utf-8')).get('data') or {}).get(
                'diff') or []
            for item in diff:
                code = secid_diff_to_code(item)
                preclose = _f(item.get('f18'))
                name = item.get('f14', '') or ''
                lu, ld = trading_rules.limit_prices(
                    code, preclose, trading_rules.is_st_name(name))
                results[code] = {
                    'name': name,
                    'price': _f(item.get('f2')),
                    'preclose': preclose,
                    'open': _f(item.get('f17')),
                    'high': _f(item.get('f15')),
                    'low': _f(item.get('f16')),
                    'volume': _f(item.get('f5')),      # 手
                    'amount': _f(item.get('f6')),      # 元
                    'pct': _f(item.get('f3')),
                    'bid1': _f(item.get('f31')),
                    'bid1_vol': None,                  # ulist无档量
                    'ask1': _f(item.get('f32')),
                    'ask1_vol': None,
                    'limit_up': lu,
                    'limit_down': ld,
                    'src': 'em',
                }
        except Exception as e:
            print(f'  [WARN] 东财ulist批次失败({batch[0]}..): {e!r}')
    return results


def fetch_eastmoney_depth(code: str, req_gap: float = DEFAULT_REQ_GAP) -> dict:
    """单股全字段点查(stock/get), 补齐买一卖一档量(封死判定等场景)。

    返回同构dict(bid1_vol/ask1_vol单位手); 失败返回{}。
    """
    try:
        raw = _get(STOCK_GET_PATH.format(secid=code_to_secid(code)), req_gap)
        d = json.loads(raw.decode('utf-8')).get('data') or {}
        if not d:
            return {}
        preclose = _f(d.get('f60'))
        name = d.get('f58', '') or ''
        lu, ld = trading_rules.limit_prices(
            code, preclose, trading_rules.is_st_name(name))
        return {
            'name': name,
            'price': _f(d.get('f43')),
            'preclose': preclose,
            'open': _f(d.get('f46')),
            'high': _f(d.get('f44')),
            'low': _f(d.get('f45')),
            'volume': _f(d.get('f47')),
            'amount': _f(d.get('f48')),
            'pct': round((_f(d.get('f43')) / preclose - 1) * 100, 2)
                   if preclose > 0 else 0.0,
            'bid1': _f(d.get('f19')),
            'bid1_vol': _f(d.get('f20')),
            'ask1': _f(d.get('f39')),
            'ask1_vol': _f(d.get('f40')),
            'limit_up': lu,
            'limit_down': ld,
            'src': 'em',
        }
    except Exception as e:
        print(f'  [WARN] 东财stock/get失败({code}): {e!r}')
        return {}


def request_count() -> int:
    """本进程累计东财请求数(预算审计用)。"""
    return _req_count[0]


if __name__ == '__main__':
    # 冒烟: 2只样本(2次请求, 计入当日预算)
    snap = fetch_eastmoney_snapshot(['sh.600519', 'sz.000001'])
    for k, v in snap.items():
        print(k, json.dumps(v, ensure_ascii=False))
    print('depth:', json.dumps(fetch_eastmoney_depth('sh.600519'),
                               ensure_ascii=False))
    print('req_count =', request_count())
