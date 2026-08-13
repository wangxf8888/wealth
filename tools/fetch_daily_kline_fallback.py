#!/usr/bin/env python3
"""fetch_daily_kline_fallback.py - 日K降级备用源(腾讯fqkline) [Task #31]

背景(2026-07-24事故): BaoStock账号被封禁(login 10001011 黑名单, 诱因为
3.2req/s持续大批量拉取), 主脚本fetch_daily_kline.py当晚起不可用。
本脚本用腾讯 web.ifzq.gtimg.cn/appstock/app/fqkline/get 拉当日日K兜底:
- 字段校对证据(2026-07-24实测2只股 vs BaoStock已入库行, 逐字段精确匹配):
  day数组 = [date, open, close, high, low, volume(手)]  ← 注意顺序O,C,H,L
  volume: 手×100=股;  preclose = 前一根bar的close
  amount = qt快照[35]第三段(元);  turn = qt快照[38](%);  名称 = qt[1]
- 东财push2his本机网络层不可达(HTTPS/HTTP均被连接重置, 2026-07-24实测),
  push2delay可通但data:null, 故弃用东财改用腾讯(项目既有成熟源)
- hour1-4列全部留NULL(引擎读到NULL自动跳过该股当日, 可接受;
  BaoStock恢复后由主脚本回补: DELETE当日行再重跑fetch_daily_kline.py)
- 仅支持"当日盘后"模式: qt快照的amount/turn只在快照日=目标日时有效

用法:
  写库:   python3 tools/fetch_daily_kline_fallback.py 2026-07-24
  校验:   python3 tools/fetch_daily_kline_fallback.py 2026-07-24 --verify [--limit N]
          (verify模式不写库, 与stock_kline已有行逐字段对照, 用于演练/审计)
"""
import argparse
import json
import logging
import math
import sqlite3
import sys
import time
import urllib.request
from datetime import datetime

DB_PATH = '/home/AIWealth/data/stocks.db'
LOG_FILE = '/home/AIWealth/logs/fetch_daily_kline.log'
ALERT_FILE = '/home/AIWealth/logs/realtime/scheduler_alerts.log'

# [Task #31 限速纪律] 2026-07-24 BaoStock因3.2req/s被封禁的教训:
# 对任何外部行情源保持礼貌限速, 腾讯源≥0.15s/请求; 连续错误立即熔断禁止重试轰炸
REQUEST_INTERVAL = 0.15     # 秒/请求
MAX_CONSECUTIVE_ERRORS = 10  # 连续错误熔断阈值
RETRIES = 2                  # 单股重试次数(温和)

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] [降级模式] %(message)s',
    handlers=[logging.FileHandler(LOG_FILE, encoding='utf-8'),
              logging.StreamHandler()])
logger = logging.getLogger(__name__)


def _write_alert(msg):
    try:
        with open(ALERT_FILE, 'a') as f:
            f.write(f"{datetime.now().strftime('%Y-%m-%d %H:%M:%S')} "
                    f"[DATA_INTEGRITY] {msg}\n")
    except Exception:
        pass


def safe_float(v, default=None):
    try:
        x = float(v)
        return default if (math.isnan(x) or math.isinf(x)) else x
    except (ValueError, TypeError):
        return default


def calc_rate(price, preclose):
    if price is None or preclose is None or preclose == 0:
        return None
    return round((price - preclose) / preclose * 100, 2)


def fetch_tencent_daily(code, target_date):
    """拉单股日K, 返回record dict或None(当日无数据/停牌)。失败抛异常。
    腾讯fqkline单请求同时返回day数组与qt实时快照(额/换手/名称)。"""
    qt_code = code.replace('sh.', 'sh').replace('sz.', 'sz')
    url = (f'https://web.ifzq.gtimg.cn/appstock/app/fqkline/get'
           f'?param={qt_code},day,,,5,')
    req = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0'})
    with urllib.request.urlopen(req, timeout=10) as resp:
        d = json.loads(resp.read().decode('utf-8', errors='replace'))
    node = d.get('data', {}).get(qt_code, {})
    days = node.get('day') or []
    if not days or days[-1][0] != target_date:
        return None                       # 当日停牌/未上市/无数据
    # day数组字段序: [date, open, close, high, low, volume(手)] (实测校对)
    bar = days[-1]
    open_p, close_p = safe_float(bar[1]), safe_float(bar[2])
    high_p, low_p = safe_float(bar[3]), safe_float(bar[4])
    vol_hand = safe_float(bar[5])
    # volume单位(2026-07-24全量5197只verify实测): 主板/创业板为"手"(×100=股,
    # 与BaoStock精确匹配); 科创板sh.68x(688及689 CDR)为"股"(直接用, 否则100倍偏差)
    if vol_hand is None:
        volume = None
    elif code.startswith('sh.68'):
        volume = int(vol_hand)
    else:
        volume = int(vol_hand * 100)

    qt = (node.get('qt') or {}).get(qt_code) or []
    name, amount, turn, preclose = '', None, None, None
    if len(qt) > 38:
        name = qt[1]
        # qt[30]快照时间戳YYYYMMDDHHMMSS: 仅快照日=目标日时qt字段可信
        snap_ts = str(qt[30]) if len(qt) > 30 else ''
        if snap_ts[:8] == target_date.replace('-', ''):
            # preclose必须用qt[4](交易所口径昨收=除权除息调整后), 不能用day
            # 前一根close(原始价): 实测sh.600004 2026-07-24除息日 7.68 vs 7.96
            preclose = safe_float(qt[4])
            seg = str(qt[35]).split('/')  # "现价/量(手)/额(元)"
            if len(seg) == 3:
                amount = safe_float(seg[2])
            turn = safe_float(qt[38])
    if preclose is None and len(days) >= 2:
        preclose = safe_float(days[-2][2])  # 兜底: 前根close(非除权日等价)
    if preclose is None or preclose == 0:
        return None

    record = {'date': target_date, 'code': code, 'code_name': name,
              'preclose': preclose,
              'open': open_p, 'open_rate': calc_rate(open_p, preclose),
              'high': high_p, 'high_rate': calc_rate(high_p, preclose),
              'low': low_p, 'low_rate': calc_rate(low_p, preclose),
              'close': close_p, 'close_rate': calc_rate(close_p, preclose),
              'volume': volume, 'amount': amount, 'turn': turn,
              'isST': 1 if 'ST' in name.upper() else 0}
    # hour1-4全列NULL: 引擎读到NULL跳过该股当日, BaoStock恢复后重补
    for h in ('hour1', 'hour2', 'hour3', 'hour4'):
        for suf in ('open', 'open_rate', 'high', 'high_rate', 'low',
                    'low_rate', 'close', 'close_rate', 'volume', 'amount'):
            record[f'{h}_{suf}'] = None
    return record


INSERT_SQL = """INSERT OR IGNORE INTO stock_kline (
    date, code, code_name, preclose,
    open, open_rate, high, high_rate, low, low_rate, close, close_rate,
    volume, amount, turn,
    hour1_open, hour1_open_rate, hour1_high, hour1_high_rate,
    hour1_low, hour1_low_rate, hour1_close, hour1_close_rate,
    hour2_open, hour2_open_rate, hour2_high, hour2_high_rate,
    hour2_low, hour2_low_rate, hour2_close, hour2_close_rate,
    hour3_open, hour3_open_rate, hour3_high, hour3_high_rate,
    hour3_low, hour3_low_rate, hour3_close, hour3_close_rate,
    hour4_open, hour4_open_rate, hour4_high, hour4_high_rate,
    hour4_low, hour4_low_rate, hour4_close, hour4_close_rate,
    hour1_volume, hour1_amount, hour2_volume, hour2_amount,
    hour3_volume, hour3_amount, hour4_volume, hour4_amount,
    isST
) VALUES (
    :date, :code, :code_name, :preclose,
    :open, :open_rate, :high, :high_rate, :low, :low_rate, :close, :close_rate,
    :volume, :amount, :turn,
    :hour1_open, :hour1_open_rate, :hour1_high, :hour1_high_rate,
    :hour1_low, :hour1_low_rate, :hour1_close, :hour1_close_rate,
    :hour2_open, :hour2_open_rate, :hour2_high, :hour2_high_rate,
    :hour2_low, :hour2_low_rate, :hour2_close, :hour2_close_rate,
    :hour3_open, :hour3_open_rate, :hour3_high, :hour3_high_rate,
    :hour3_low, :hour3_low_rate, :hour3_close, :hour3_close_rate,
    :hour4_open, :hour4_open_rate, :hour4_high, :hour4_high_rate,
    :hour4_low, :hour4_low_rate, :hour4_close, :hour4_close_rate,
    :hour1_volume, :hour1_amount, :hour2_volume, :hour2_amount,
    :hour3_volume, :hour3_amount, :hour4_volume, :hour4_amount,
    :isST
)"""


def get_stock_list(conn):
    """BaoStock不可用 → 股票列表取自stock_kline最近一个交易日的全部code。"""
    last_date = conn.execute(
        "SELECT MAX(date) FROM stock_kline").fetchone()[0]
    rows = conn.execute(
        "SELECT code, code_name FROM stock_kline WHERE date=?",
        (last_date,)).fetchall()
    logger.info("股票列表来自stock_kline %s: %d只", last_date, len(rows))
    return rows


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('date', help='目标日期 YYYY-MM-DD (仅支持当日盘后)')
    ap.add_argument('--verify', action='store_true',
                    help='校验模式: 不写库, 与stock_kline已有行对照')
    ap.add_argument('--limit', type=int, default=0,
                    help='verify模式抽样股数(0=全部)')
    args = ap.parse_args()
    target = args.date

    logger.warning("=== [降级模式] BaoStock不可用, 启用腾讯fqkline备用源 "
                   "(hour1-4列留NULL, 恢复后需重补) ===")
    conn = sqlite3.connect(DB_PATH)
    conn.execute("PRAGMA journal_mode=WAL")
    stocks = get_stock_list(conn)
    if args.limit:
        stocks = stocks[:args.limit]

    inserted, skipped, verified_ok, verified_diff = 0, 0, 0, 0
    failures, consecutive_err = [], 0
    diffs = []
    t0 = time.time()
    for i, (code, _) in enumerate(stocks):
        rec, err = None, None
        for attempt in range(RETRIES):
            try:
                rec = fetch_tencent_daily(code, target)
                err = None
                break
            except Exception as exc:
                err = str(exc)
                time.sleep(1 + attempt)
        time.sleep(REQUEST_INTERVAL)   # [Task #31] 礼貌限速≥0.15s

        if err:
            failures.append((code, err))
            consecutive_err += 1
            if consecutive_err >= MAX_CONSECUTIVE_ERRORS:
                logger.error("连续%d次错误, 熔断终止(禁止重试轰炸)!",
                             consecutive_err)
                _write_alert(f"[DAILY_FALLBACK] 腾讯源连续{consecutive_err}次"
                             f"错误熔断, 已完成{i}/{len(stocks)}")
                break
            continue
        consecutive_err = 0
        if rec is None:
            skipped += 1               # 当日停牌/无数据
            continue

        if args.verify:
            row = conn.execute(
                "SELECT preclose,open,high,low,close,volume FROM stock_kline "
                "WHERE code=? AND date=?", (code, target)).fetchone()
            if row is None:
                verified_diff += 1
                diffs.append((code, '库中无行'))
            else:
                bad = [f"{f}:{a}vs{b}" for f, a, b in zip(
                    ('preclose', 'open', 'high', 'low', 'close'),
                    (rec['preclose'], rec['open'], rec['high'],
                     rec['low'], rec['close']), row[:5])
                    if a is not None and b is not None
                    and abs(a - b) / b > 0.001]
                # volume容忍0.1%(腾讯手数舍入)
                if rec['volume'] and row[5] and \
                        abs(rec['volume'] - row[5]) / row[5] > 0.001:
                    bad.append(f"volume:{rec['volume']}vs{row[5]}")
                if bad:
                    verified_diff += 1
                    diffs.append((code, ';'.join(bad)))
                else:
                    verified_ok += 1
        else:
            conn.execute(INSERT_SQL, rec)
            inserted += 1
            if inserted % 200 == 0:
                conn.commit()

        if (i + 1) % 500 == 0:
            logger.info("进度 %d/%d, 入库%d 跳过%d 失败%d, %.1f req/s",
                        i + 1, len(stocks), inserted, skipped,
                        len(failures), (i + 1) / (time.time() - t0))

    conn.commit()
    elapsed = time.time() - t0
    if args.verify:
        logger.info("[verify] 对照完成: 一致%d | 差异%d | 停牌跳过%d | "
                    "失败%d | 耗时%.1f分钟", verified_ok, verified_diff,
                    skipped, len(failures), elapsed / 60)
        if diffs:
            logger.warning("[verify] 差异明细(前20): %s", diffs[:20])
    else:
        cnt = conn.execute(
            "SELECT COUNT(*) FROM stock_kline WHERE date=?",
            (target,)).fetchone()[0]
        logger.info("[降级模式] 入库完成: 新插入%d | 停牌跳过%d | 失败%d | "
                    "%s库中总行数%d | 耗时%.1f分钟",
                    inserted, skipped, len(failures), target, cnt,
                    elapsed / 60)
        _write_alert(f"[DAILY_FALLBACK] {target} 腾讯源降级入库: 新插入"
                     f"{inserted}行, 总行数{cnt}, hour列为NULL待BaoStock恢复后重补")
    conn.close()
    if failures:
        logger.error("失败清单(前30): %s", failures[:30])
        return 1
    return 0


if __name__ == '__main__':
    sys.exit(main())
