#!/usr/bin/env python3
"""fetch_minute_sina.py - 新浪5分钟K线增量回补器 (Task#199)

背景: BaoStock账号黑名单(10001011)致minute.db 2026-07-24后断供, 冲板影子探测器
量能基线(近5日5min量能均值)失去数据源。本工具用新浪 quotes.sina.cn 分钟接口
做近期增量回补(深度~1500bar≈32交易日, Jake#195实测), 保鲜minute.db。

数据源实测定型(#195 EVAL_REPORT + 本任务2026-08-06复测):
- URL: quotes.sina.cn/cn/api/json_v2.php/CN_MarketDataService.getKLineData
       ?symbol=sh600156&scale=5&ma=no&datalen=N   (N<=1500, 2000+被拦截)
- 字段: day='YYYY-MM-DD HH:MM:SS'(bar右端点, 0935..1130/1305..1500,
  与minute.db time约定完全一致), open/high/low/close, volume(股, 与
  minute.db同单位无需换算), amount(忽略)
- 每只股1个请求返回全部近N根bar, 不支持指定历史日期范围

用法:
  小池回补(影子候选池+持仓+当日候选, 本次执行口径):
      python3 tools/fetch_minute_sina.py --pool shadow
  指定股票池: python3 tools/fetch_minute_sina.py --codes sh.600000,sz.000001
  全量(触板宇宙=主板sh.60/sz.00非ST, 另行调度, 勿在盘中跑):
      python3 tools/fetch_minute_sina.py --all
  预扫不拉取: 追加 --dry-run
  边界对齐抽检: 追加 --verify-overlap 3 (用响应内已完整日与库内BaoStock数据
      逐bar比对, 零额外请求)

限速纪律(BaoStock封禁教训延续, 禁止调低):
- 单只请求间隔 >= 1s + 0~0.5s随机抖动
- 连续5次请求异常 → 熔断终止(禁止重试轰炸)
- 断点续传: data/sina_minute_fetch_progress.json, 同参数重跑跳过已完成code

幂等性: PK(code,date,time) + INSERT OR REPLACE; 已有>=48bar的股·日整日跳过
(不触碰既有BaoStock完整数据), 重复运行不产生重复行。
红线: 只写minute.db与progress json; 不碰strategies/backtest/realtime。
"""
import argparse
import glob
import json
import logging
import os
import random
import sqlite3
import sys
import time
import urllib.request
from datetime import datetime

MINUTE_DB = '/home/AIWealth/data/minute.db'
STOCKS_DB = '/home/AIWealth/data/stocks.db'
POSITIONS_FILE = '/home/AIWealth/data/realtime/positions.json'
CANDIDATES_DIR = '/home/AIWealth/data/realtime'
SHADOW_DIR = '/home/AIWealth/research/results/shadow_surge'
PROGRESS_FILE = '/home/AIWealth/data/sina_minute_fetch_progress.json'
LOG_FILE = '/home/AIWealth/logs/fetch_minute_sina.log'
ALERT_FILE = '/home/AIWealth/logs/realtime/scheduler_alerts.log'

BARS_PER_DAY = 48
DATALEN_MAX = 1500            # 新浪硬上限(2000+返回redirect脚本, #195实测)
MIN_INTERVAL = 1.0            # 单只请求最小间隔(秒), 红线禁止调低
JITTER_MAX = 0.5              # 随机抖动上限(秒)
MAX_CONSECUTIVE_ERRORS = 5    # 连续异常熔断阈值
RETRIES = 2

_SINA_URL = ('https://quotes.sina.cn/cn/api/json_v2.php/'
             'CN_MarketDataService.getKLineData'
             '?symbol={sym}&scale=5&ma=no&datalen={n}')

# 合法bar右端点全集(0935..1130, 1305..1500), 异常时间标签行拒收
EXPECTED_TIMES = frozenset(
    [f'{h:02d}{m:02d}' for h in (9, 10, 11) for m in range(0, 60, 5)
     if '0935' <= f'{h:02d}{m:02d}' <= '1130']
    + [f'{h:02d}{m:02d}' for h in (13, 14) for m in range(0, 60, 5)
       if f'{h:02d}{m:02d}' >= '1305']
    + ['1500'])

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(message)s',
    handlers=[logging.FileHandler(LOG_FILE, encoding='utf-8'),
              logging.StreamHandler()])
logger = logging.getLogger(__name__)

SCHEMA = """
CREATE TABLE IF NOT EXISTS minute_kline (
    code    TEXT NOT NULL,
    date    TEXT NOT NULL,          -- 'YYYY-MM-DD'
    time    TEXT NOT NULL,          -- 'HHMM' bar结束时刻(0935..1500)
    open    REAL, high REAL, low REAL, close REAL,
    volume  INTEGER,                -- 单位: 股
    PRIMARY KEY (code, date, time)
) WITHOUT ROWID;
"""


def ensure_db():
    conn = sqlite3.connect(MINUTE_DB)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute(SCHEMA)
    conn.commit()
    return conn


def _write_alert(msg):
    try:
        os.makedirs(os.path.dirname(ALERT_FILE), exist_ok=True)
        with open(ALERT_FILE, 'a') as f:
            f.write(f"{datetime.now().strftime('%Y-%m-%d %H:%M:%S')} "
                    f"[DATA_INTEGRITY] {msg}\n")
    except Exception:
        pass


# ---------------------------------------------------------------------------
# 股票池构建
# ---------------------------------------------------------------------------
def pool_shadow(with_candidates=False):
    """影子候选池(历史shadow_signals的signals+skips) + 在产持仓/近平仓,
    小池先跑通口径(Task#199, 请求预算克制)。with_candidates=True时追加
    近3个候选文件(约+270只, 全量调度前的中间档, 默认关)。"""
    codes = set()
    for fp in sorted(glob.glob(os.path.join(SHADOW_DIR,
                                            'shadow_signals_*.json'))):
        try:
            d = json.load(open(fp))
        except Exception as exc:
            logger.warning("跳过无法解析的信号文件 %s: %s", fp, exc)
            continue
        for s in d.get('signals', []):
            codes.add(s['code'])
        for s in d.get('skips', []):
            if s.get('code'):
                codes.add(s['code'])
    n_shadow = len(codes)
    try:
        pd_ = json.load(open(POSITIONS_FILE))
        for p in pd_.get('positions', []):
            if p.get('code'):
                codes.add(p['code'])
        for t in pd_.get('closed_trades', [])[-30:]:
            if t.get('code'):
                codes.add(t['code'])
    except Exception as exc:
        logger.warning("positions.json读取失败(不阻断): %s", exc)
    n_pos = len(codes) - n_shadow
    n_before = len(codes)
    if with_candidates:
        for fp in sorted(glob.glob(os.path.join(
                CANDIDATES_DIR, 'candidates_*.json')))[-3:]:
            try:
                d = json.load(open(fp))
                for v in d.values():
                    if isinstance(v, list):
                        for item in v:
                            if isinstance(item, dict) and item.get('code'):
                                codes.add(item['code'])
            except Exception as exc:
                logger.warning("候选文件解析失败 %s: %s", fp, exc)
    logger.info("[池] shadow=%d 持仓相关=+%d 候选文件=+%d → 合计%d只",
                n_shadow, n_pos, len(codes) - n_before, len(codes))
    return sorted(codes)


def pool_all():
    """触板宇宙全量(参考#79/影子探测器白名单定义): 主板sh.60/sz.00非ST。
    注: 不带昨日turn过滤(turn逐日变化, 基线库宁多勿漏)。另行调度用。"""
    sc = sqlite3.connect(f'file:{STOCKS_DB}?mode=ro', uri=True)
    p1 = sc.execute("SELECT MAX(date) FROM stock_kline").fetchone()[0]
    codes = [r[0] for r in sc.execute(
        "SELECT code FROM stock_kline WHERE date=? AND isST=0 "
        "AND (code LIKE 'sh.60%' OR code LIKE 'sz.00%') "
        "AND close>0 ORDER BY code", (p1,))]
    sc.close()
    logger.info("[池] 触板宇宙全量(基准日%s): %d只", p1, len(codes))
    return codes


# ---------------------------------------------------------------------------
# 新浪拉取与解析
# ---------------------------------------------------------------------------
def fetch_sina_5min(code, datalen):
    """单只请求。返回 {date: [(code,date,hhmm,o,h,l,c,vol), ...]}。
    异常抛出; 空响应/非法JSON抛RuntimeError。"""
    sym = code.replace('sh.', 'sh').replace('sz.', 'sz')
    url = _SINA_URL.format(sym=sym, n=datalen)
    req = urllib.request.Request(url)
    req.add_header('Referer', 'https://finance.sina.com.cn')
    req.add_header('User-Agent',
                   'Mozilla/5.0 (Windows NT 10.0; Win64; x64) '
                   'AppleWebKit/537.36')
    with urllib.request.urlopen(req, timeout=10) as resp:
        raw = resp.read().decode('utf-8', errors='replace')
    if not raw or raw[0] not in '[{':
        # datalen超限/被拦截时新浪返回redirect脚本而非JSON
        raise RuntimeError(f'非JSON响应(疑似被拦截): {raw[:80]!r}')
    bars = json.loads(raw)
    if not isinstance(bars, list):
        raise RuntimeError(f'响应结构异常: {type(bars)}')
    by_day, n_badtime = {}, 0
    for b in bars:
        day = b.get('day', '')
        if len(day) < 16:
            n_badtime += 1
            continue
        d, hhmm = day[:10], day[11:13] + day[14:16]
        if hhmm not in EXPECTED_TIMES:      # 非法bar标签拒收(fail-safe)
            n_badtime += 1
            continue
        try:
            by_day.setdefault(d, []).append(
                (code, d, hhmm, float(b['open']), float(b['high']),
                 float(b['low']), float(b['close']),
                 int(float(b['volume']))))
        except (KeyError, ValueError, TypeError):
            n_badtime += 1
    if n_badtime:
        logger.warning("%s: %d根bar时间标签/字段异常被拒收", code, n_badtime)
    return by_day


# ---------------------------------------------------------------------------
# 断点续传 progress
# ---------------------------------------------------------------------------
def load_progress(params):
    try:
        p = json.load(open(PROGRESS_FILE))
        if p.get('params') == params:
            return p
    except Exception:
        pass
    return {'params': params, 'done': {},
            'started_at': datetime.now().strftime('%Y-%m-%d %H:%M:%S')}


def save_progress(progress):
    try:
        with open(PROGRESS_FILE, 'w') as f:
            json.dump(progress, f, ensure_ascii=False, indent=1)
    except Exception:
        pass


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------
def run(codes, start, end, datalen, dry_run=False, verify_overlap=0):
    conn = ensure_db()
    # 预扫: 各code在区间内既有完整日(>=48bar), 用于整日跳过与幂等
    complete = {}    # {code: set(完整日)}
    for code in codes:
        complete[code] = {d for d, c in conn.execute(
            "SELECT date, COUNT(*) FROM minute_kline "
            "WHERE code=? AND date>=? AND date<=? GROUP BY date",
            (code, start, end)) if c >= BARS_PER_DAY}
    logger.info("回补区间 %s ~ %s, %d只, datalen=%d, "
                "库内既有完整股·日=%d(整日跳过不触碰)",
                start, end, len(codes), datalen,
                sum(len(v) for v in complete.values()))
    if dry_run:
        logger.info("[dry-run] 仅预扫, 不拉取")
        conn.close()
        return 0

    params = {'start': start, 'end': end, 'datalen': datalen,
              'n_codes': len(codes)}
    progress = load_progress(params)
    todo = [c for c in codes if c not in progress['done']]
    if len(todo) < len(codes):
        logger.info("断点续传: %d只已完成, 本次待拉%d只",
                    len(codes) - len(todo), len(todo))

    inserted, day_incomplete, failures = 0, [], []
    overlap_checked, overlap_mismatch = 0, 0
    consecutive_errors = 0
    t0 = time.time()
    for i, code in enumerate(todo):
        by_day, err = {}, None
        for attempt in range(RETRIES + 1):
            _t = time.time()
            try:
                by_day = fetch_sina_5min(code, datalen)
                err = None
            except Exception as exc:
                err = str(exc)
            finally:
                # 限速铁律: >=1s + 抖动(成功失败一律等待)
                gap = MIN_INTERVAL + random.uniform(0, JITTER_MAX) \
                    - (time.time() - _t)
                if gap > 0:
                    time.sleep(gap)
            if err is None:
                break
            time.sleep(1 + attempt)
        if err:
            consecutive_errors += 1
            failures.append((code, err))
            logger.warning("[FETCH_FAIL] %s: %s", code, err)
            if consecutive_errors >= MAX_CONSECUTIVE_ERRORS:
                logger.error("连续%d次请求异常, 熔断终止(禁止轰炸数据源)!",
                             consecutive_errors)
                _write_alert(f"[MINUTE_SINA] 连续{consecutive_errors}次异常"
                             f"熔断: {err}")
                break
            continue
        consecutive_errors = 0

        # 边界对齐抽检: 响应内已完整日 vs 库内既有(BaoStock)数据逐bar比对
        if verify_overlap and overlap_checked < verify_overlap:
            ov_days = sorted(set(by_day) & complete[code])[-2:]
            if ov_days:
                overlap_checked += 1
                for d in ov_days:
                    db_rows = {r[0]: r[1:] for r in conn.execute(
                        "SELECT time, open, high, low, close, volume "
                        "FROM minute_kline WHERE code=? AND date=?",
                        (code, d))}
                    n_diff = 0
                    for r in by_day[d]:
                        old = db_rows.get(r[2])
                        if old is None:
                            n_diff += 1
                            continue
                        if any(abs(a - b) > 0.011 for a, b in
                               zip(old[:4], r[3:7])) \
                                or abs((old[4] or 0) - r[7]) > 100:
                            n_diff += 1
                    if n_diff:
                        overlap_mismatch += 1
                        logger.warning("[OVERLAP] %s %s: %d/%d bar与库内"
                                       "BaoStock数据不一致", code, d,
                                       n_diff, len(by_day[d]))
                    else:
                        logger.info("[OVERLAP] %s %s: %dbar 与库内既有数据"
                                    "逐bar一致 ✓", code, d, len(by_day[d]))

        n_rows, n_days = 0, 0
        for d in sorted(by_day):
            if d < start or d > end or d in complete[code]:
                continue                    # 区间外/已完整日: 不触碰
            rows = by_day[d]
            for r in rows:
                conn.execute("INSERT OR REPLACE INTO minute_kline "
                             "VALUES (?,?,?,?,?,?,?,?)", r)
            n_rows += len(rows)
            n_days += 1
            if len(rows) < BARS_PER_DAY:
                day_incomplete.append((code, d, len(rows)))
        conn.commit()
        inserted += n_rows
        progress['done'][code] = {'days': n_days, 'rows': n_rows}
        if (i + 1) % 10 == 0 or i + 1 == len(todo):
            elapsed = time.time() - t0
            speed = (i + 1) / elapsed if elapsed else 0
            progress.update(
                completed=len(progress['done']), total=len(codes),
                inserted=inserted, failures=len(failures),
                speed=f"{speed:.2f} req/s",
                eta_minutes=round((len(todo) - i - 1) / speed / 60, 1)
                if speed else None)
            save_progress(progress)
            logger.info("进度 %d/%d, 插入%d行, 失败%d, %.2f req/s",
                        i + 1, len(todo), inserted, len(failures), speed)

    progress.update(finished_at=datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
                    inserted=inserted, failures=len(failures))
    save_progress(progress)
    conn.commit()
    conn.close()
    logger.info("=" * 60)
    logger.info("回补完成: 插入%d行 | 不足48bar股·日%d(可能临停/新上市/当日"
                "未收盘) | 失败%d | 边界抽检%d只(不一致%d) | 耗时%.1f分钟",
                inserted, len(day_incomplete), len(failures),
                overlap_checked, overlap_mismatch,
                (time.time() - t0) / 60)
    if day_incomplete:
        logger.info("[INCOMPLETE] 前20条: %s", day_incomplete[:20])
    if failures:
        logger.error("[FETCH_FAIL] 失败清单: %s", failures[:50])
        _write_alert(f"[MINUTE_SINA] 回补失败{len(failures)}只, 详见{LOG_FILE}")
        return 1
    return 0


def main():
    ap = argparse.ArgumentParser(description='新浪5min增量回补minute.db')
    ap.add_argument('--codes', help='逗号分隔, 如 sh.600000,sz.000001')
    ap.add_argument('--pool', choices=['shadow'],
                    help='shadow=影子候选池+持仓相关(小池)')
    ap.add_argument('--with-candidates', action='store_true',
                    help='shadow池追加近3日候选文件(~+270只, 默认关)')
    ap.add_argument('--all', action='store_true',
                    help='触板宇宙全量(主板非ST, 另行调度, 勿盘中跑)')
    ap.add_argument('--start', help='回补起始日(默认=库内最新日期, 该日若'
                                    '不足48bar会被补齐)')
    ap.add_argument('--end', help='回补截止日(默认=今日)')
    ap.add_argument('--datalen', type=int, default=800,
                    help='每只请求bar数(默认800≈16交易日, 上限1500)')
    ap.add_argument('--dry-run', action='store_true')
    ap.add_argument('--verify-overlap', type=int, default=0, metavar='N',
                    help='抽N只用重叠日与库内既有数据逐bar比对(零额外请求)')
    args = ap.parse_args()

    if args.datalen > DATALEN_MAX:
        logger.error("datalen=%d超过新浪上限%d", args.datalen, DATALEN_MAX)
        return 2
    if args.codes:
        codes = sorted({c.strip() for c in args.codes.split(',') if c.strip()})
    elif args.pool == 'shadow':
        codes = pool_shadow(with_candidates=args.with_candidates)
    elif args.all:
        codes = pool_all()
    else:
        ap.print_help()
        return 2
    if not codes:
        logger.error("股票池为空, 无事可做")
        return 2

    end = args.end or datetime.now().strftime('%Y-%m-%d')
    if args.start:
        start = args.start
    else:
        conn = sqlite3.connect(f'file:{MINUTE_DB}?mode=ro', uri=True)
        start = conn.execute(
            "SELECT MAX(date) FROM minute_kline").fetchone()[0] or end
        conn.close()
        logger.info("未指定--start, 取库内最新日期%s为起点(部分日会补齐)",
                    start)
    return run(codes, start, end, args.datalen,
               dry_run=args.dry_run, verify_overlap=args.verify_overlap)


if __name__ == '__main__':
    sys.exit(main())
