#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Task#78: 龙虎榜(LHB)全量回补器 (东财datacenter-web, 2021-01-01至今).

用法:
  python3 tools/lhb_backfill.py --budget 2500        # 回补模式(断点续跑, 预算耗尽自动收工)
  python3 tools/lhb_backfill.py --incremental        # 盘后增量(仅最新交易日, 3~5请求)

纪律(Task#74调研口径):
  - 限速>=0.6s/请求
  - 连续3失败 -> 退避300s; 退避后再连续3失败 -> 中止本轮(状态已落盘可续跑)
  - 时段守卫: 8:00-15:30 自动暂停(sleep至15:31), 不与盘中争带宽
  - 幂等: lhb_done表记录完成日(买卖两侧行数与API count核对一致才标记), 已完成日跳过
  - 独立库 data/lhb.db, 不碰 stocks.db
数据: lhb_daily(名单) + lhb_seats(席位明细, >500行确定性排序翻页, Task#74已验证)
"""
import argparse
import json
import math
import sqlite3
import sys
import time
import urllib.parse
import urllib.request
from datetime import datetime

ROOT = '/home/AIWealth'
LHB_DB = f'{ROOT}/data/lhb.db'
STOCKS_DB = f'{ROOT}/data/stocks.db'
BACKFILL_START = '2021-01-01'

BASE = "https://datacenter-web.eastmoney.com/api/data/v1/get"
HEADERS = {
    "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36",
    "Referer": "https://data.eastmoney.com/stock/tradedetail.html",
}
MIN_INTERVAL = 0.6
PAGE = 500                 # 服务端pageSize硬顶500
RPT_DAILY = "RPT_DAILYBILLBOARD_DETAILSNEW"
RPT_SEAT = {"B": "RPT_BILLBOARD_DAILYDETAILSBUY", "S": "RPT_BILLBOARD_DAILYDETAILSSELL"}

_last_ts = 0.0
_fail_streak = 0
_backoff_used = False
_req_count = 0
_budget = 2500


def log(msg):
    print(f"[{datetime.now().strftime('%H:%M:%S')}] {msg}", flush=True)


class BudgetExhausted(Exception):
    pass


def market_hours_guard():
    """8:00-15:30 暂停(明晨9:25首跑日绝不与盘中争带宽)。"""
    while True:
        now = datetime.now()
        hm = now.hour * 60 + now.minute
        if 8 * 60 <= hm < 15 * 60 + 30:
            log("[时段守卫] 8:00-15:30盘中时段, 暂停至15:31...")
            time.sleep(min((15 * 60 + 31 - hm) * 60, 1800))
        else:
            return


def fetch(report_name, flt, page):
    """单请求: 限速+预算+失败退避熔断+时段守卫。返回body或None(单次失败)。"""
    global _last_ts, _fail_streak, _backoff_used, _req_count
    if _req_count >= _budget:
        raise BudgetExhausted()
    market_hours_guard()
    if _fail_streak >= 3:
        if _backoff_used:
            raise SystemExit("[熔断] 退避后仍连续3失败, 中止本轮(状态已落盘, 下轮续跑)")
        log("[退避] 连续3失败, 休眠300s...")
        time.sleep(300)
        _backoff_used = True
        _fail_streak = 0
    wait = MIN_INTERVAL - (time.time() - _last_ts)
    if wait > 0:
        time.sleep(wait)
    params = {"reportName": report_name, "columns": "ALL", "filter": flt,
              "pageNumber": page, "pageSize": PAGE,
              "sortColumns": "SECURITY_CODE,OPERATEDEPT_CODE", "sortTypes": "1,1",
              "source": "WEB", "client": "WEB"}
    if report_name == RPT_DAILY:
        params["sortColumns"], params["sortTypes"] = "SECURITY_CODE", "1"
    url = BASE + "?" + urllib.parse.urlencode(params)
    _last_ts = time.time()
    _req_count += 1
    try:
        req = urllib.request.Request(url, headers=HEADERS)
        with urllib.request.urlopen(req, timeout=15) as r:
            body = json.loads(r.read().decode("utf-8"))
        _fail_streak = 0
        return body
    except Exception as e:
        _fail_streak += 1
        log(f"[FAIL#{_fail_streak}] {report_name} p{page} {flt} -> {e!r}")
        return None


def fetch_all(report_name, flt):
    """拉全量(自动翻页)。返回(rows, count); 请求失败返回(None, None)。"""
    b = fetch(report_name, flt, 1)
    if not b or not b.get("result"):
        # success=True但result=None意为该日无数据(count=0)
        if b and b.get("success"):
            return [], 0
        return None, None
    total = b["result"]["count"]
    all_rows = list(b["result"]["data"] or [])
    for p in range(2, math.ceil(total / PAGE) + 1):
        b2 = fetch(report_name, flt, p)
        if not b2 or not b2.get("result"):
            return None, None
        all_rows += b2["result"]["data"] or []
    return all_rows, total


def norm_code(c):
    if c.startswith('6'):
        return 'sh.' + c
    if c[0] in '03':
        return 'sz.' + c
    if c[0] == '1':
        return 'cb.' + c    # 可转债(11x/12x), 榜单含之, 研究侧按前缀剔除
    if c.startswith('90'):
        return 'sh.' + c    # 沪B股(900xxx), 非交易宇宙但前缀如实
    if c.startswith('20'):
        return 'sz.' + c    # 深B股
    return 'bj.' + c        # 4/8/92开头北交所系


def ensure_schema(lc):
    lc.executescript("""
    CREATE TABLE IF NOT EXISTS lhb_daily(
        trade_date TEXT, code TEXT, name TEXT, close REAL, change_rate REAL,
        explanation TEXT, bb_buy REAL, bb_sell REAL, bb_net REAL,
        d1_ret REAL, d5_ret REAL,
        PRIMARY KEY(trade_date, code, explanation));
    CREATE TABLE IF NOT EXISTS lhb_seats(
        trade_date TEXT, code TEXT, side TEXT, seat_name TEXT,
        buy_amt REAL, sell_amt REAL, net_amt REAL, explanation TEXT);
    CREATE INDEX IF NOT EXISTS idx_seats_dc ON lhb_seats(trade_date, code);
    CREATE INDEX IF NOT EXISTS idx_seats_name ON lhb_seats(seat_name);
    CREATE TABLE IF NOT EXISTS lhb_done(
        trade_date TEXT PRIMARY KEY, daily_cnt INT, buy_cnt INT, sell_cnt INT, done_at TEXT);
    """)
    # 一次性引导: Task#74抽样已入库并经t74_lhb_patch.py与API count逐组核对一致的62日
    if lc.execute("SELECT COUNT(*) FROM lhb_done").fetchone()[0] == 0:
        n = 0
        for (d,) in lc.execute("SELECT DISTINCT trade_date FROM lhb_daily"):
            dc = lc.execute("SELECT COUNT(*) FROM lhb_daily WHERE trade_date=?", (d,)).fetchone()[0]
            bc = lc.execute("SELECT COUNT(*) FROM lhb_seats WHERE trade_date=? AND side='B'", (d,)).fetchone()[0]
            sc_ = lc.execute("SELECT COUNT(*) FROM lhb_seats WHERE trade_date=? AND side='S'", (d,)).fetchone()[0]
            if dc and bc and sc_:
                lc.execute("INSERT OR IGNORE INTO lhb_done VALUES(?,?,?,?,?)",
                           (d, dc, bc, sc_, 'bootstrap_t74_sample'))
                n += 1
        lc.commit()
        if n:
            log(f"[引导] Task#74抽样{n}日已标记完成(patch阶段已与API核对)")


def process_day(lc, d):
    """回补单日。成功且账实相符 -> 标记done并返回True。"""
    rows_d, cnt_d = fetch_all(RPT_DAILY, f"(TRADE_DATE='{d}')")
    if rows_d is None:
        return False
    lc.execute("DELETE FROM lhb_daily WHERE trade_date=?", (d,))
    for r in rows_d:
        lc.execute("INSERT OR REPLACE INTO lhb_daily VALUES(?,?,?,?,?,?,?,?,?,?,?)", (
            d, norm_code(r["SECURITY_CODE"]), r.get("SECURITY_NAME_ABBR"),
            r.get("CLOSE_PRICE"), r.get("CHANGE_RATE"), r.get("EXPLANATION"),
            r.get("BILLBOARD_BUY_AMT"), r.get("BILLBOARD_SELL_AMT"),
            r.get("BILLBOARD_NET_AMT"), r.get("D1_CLOSE_ADJCHRATE"),
            r.get("D5_CLOSE_ADJCHRATE")))
    side_cnt = {}
    for side in ("B", "S"):
        rows_s, cnt_s = fetch_all(RPT_SEAT[side], f"(TRADE_DATE='{d}')")
        if rows_s is None:
            lc.rollback()
            return False
        if len(rows_s) != cnt_s:
            log(f"[警告] {d} {side} 翻页后{len(rows_s)}行 != count {cnt_s}, 本日不标记done")
            lc.rollback()
            return False
        lc.execute("DELETE FROM lhb_seats WHERE trade_date=? AND side=?", (d, side))
        for x in rows_s:
            lc.execute("INSERT INTO lhb_seats VALUES(?,?,?,?,?,?,?,?)", (
                d, norm_code(x["SECURITY_CODE"]), side,
                x.get("OPERATEDEPT_NAME"), x.get("BUY"), x.get("SELL"),
                x.get("NET"), x.get("EXPLANATION")))
        side_cnt[side] = cnt_s
    lc.execute("INSERT OR REPLACE INTO lhb_done VALUES(?,?,?,?,?)",
               (d, len(rows_d), side_cnt["B"], side_cnt["S"],
                datetime.now().strftime('%Y-%m-%d %H:%M:%S')))
    lc.commit()
    return True


def get_calendar(start, end=None):
    sc = sqlite3.connect(STOCKS_DB)
    q = ("SELECT DISTINCT date FROM index_kline WHERE code='sh.000001' "
         "AND date>=? ORDER BY date")
    dates = [r[0] for r in sc.execute(q, (start,))]
    sc.close()
    if end:
        dates = [d for d in dates if d <= end]
    return dates


def main():
    global _budget
    ap = argparse.ArgumentParser()
    ap.add_argument('--budget', type=int, default=2500)
    ap.add_argument('--incremental', action='store_true',
                    help='盘后增量: 仅回补最新交易日')
    args = ap.parse_args()
    _budget = args.budget

    lc = sqlite3.connect(LHB_DB)
    ensure_schema(lc)

    if args.incremental:
        cal = get_calendar(BACKFILL_START)
        target = [cal[-1]] if cal else []
        log(f"[增量模式] 目标日: {target}")
    else:
        cal = get_calendar(BACKFILL_START)
        done = {r[0] for r in lc.execute("SELECT trade_date FROM lhb_done")}
        target = [d for d in cal if d not in done]
        log(f"[回补模式] 日历{len(cal)}日, 已完成{len(done)}, 待回补{len(target)}, "
            f"本轮预算{_budget}请求")

    ok = fail = 0
    try:
        for i, d in enumerate(target):
            if process_day(lc, d):
                ok += 1
            else:
                fail += 1
                if not args.incremental:
                    log(f"[跳过] {d} 本轮未完成(可续跑)")
            if ok and ok % 25 == 0:
                log(f"[进度] 完成{ok}/{len(target)}日, 已用请求{_req_count}/{_budget}, "
                    f"当前日{d}")
    except BudgetExhausted:
        log(f"[预算耗尽] 本轮收工: 完成{ok}日, 用满{_req_count}请求")
    except KeyboardInterrupt:
        log("[中断] 状态已落盘, 可续跑")

    total_done = lc.execute("SELECT COUNT(*) FROM lhb_done").fetchone()[0]
    nd = lc.execute("SELECT COUNT(*) FROM lhb_daily").fetchone()[0]
    ns = lc.execute("SELECT COUNT(*) FROM lhb_seats").fetchone()[0]
    log(f"[收工] 本轮完成{ok}日/失败{fail}日, 请求{_req_count}; "
        f"累计done={total_done}日, lhb_daily={nd}行, lhb_seats={ns}行")
    lc.close()
    # 增量模式失败只告警不中断上游(daily_update.sh约定)
    if args.incremental and fail:
        print("[ALERT] lhb增量回补失败, 明日回补模式会自动补齐", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
