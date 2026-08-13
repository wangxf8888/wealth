#!/usr/bin/env python3
"""
fetch_daily_kline.py - 从BaoStock获取日K+60minK线数据写入stock_kline表
优化版: 每只股票批量获取全日期范围数据(大幅减少API调用)

用法:
  单日模式: python3 fetch_daily_kline.py 2026-06-30
  回补模式: python3 fetch_daily_kline.py 2026-05-23 2026-06-30
  可选参数: --no-retry     关闭收尾重试轮([Task#222]起默认启用: 空返回类+错误类
            (含database is locked)合并清单收尾同纪律重试一轮;
            --retry-empty旧旗标保留兼容, 现为no-op)

退出码语义([Task#211]固化):
  0  正常(尾部自检通过, 才打"全部完成")
  1  参数错误/BaoStock登录失败(单日模式登录失败时透传降级脚本退出码)
  2  尾部自检不过(空返回率>5% 或 当日入库<任务清单×95%) — 数据疑似残缺
  3  10001011黑名单熔断([Task#194])
  4  达单次处理量上限优雅收工([Task#201]渐进)
  5  BaoStock统一日预算耗尽优雅停止([Task#259], 断点续传次日续)
"""
import sys
import os
import json
import time
import random
import signal
import sqlite3
import math
import logging
from datetime import datetime, timedelta

import baostock as bs

# ============ 配置区 ============
DB_PATH = '/home/AIWealth/data/stocks.db'
PROGRESS_FILE = '/home/AIWealth/data/fetch_progress.json'
LOG_FILE = '/home/AIWealth/logs/fetch_daily_kline.log'
COMMIT_BATCH = 100          # 每100只股票commit一次
QUERY_TIMEOUT = 60          # 单次查询超时秒数
# 股票代码前缀过滤(只取A股主板/创业板/科创板)
STOCK_PREFIXES = ('sh.60', 'sh.68', 'sz.00', 'sz.30')
# 60分钟K线时间映射: BaoStock返回的结束时间 -> hour序号
TIME_MAP = {
    '103000': 'hour1',  # 9:30-10:30
    '113000': 'hour2',  # 10:30-11:30
    '140000': 'hour3',  # 13:00-14:00
    '150000': 'hour4',  # 14:00-15:00
}
# ============ [Task #31] BaoStock限速纪律(2026-07-24封禁事故固化) ============
# 事故: 3.2req/s持续拉取触发BaoStock黑名单(10001011), 全链路瘫痪一晚。
# 铁律: ①请求间隔≥0.5s ②连续错误立即熔断+告警禁止重试轰炸 ③大批量分日执行
# 本脚本每股2个请求(日K+60minK), 间隔在每股循环内落地(≥1.0s/股)
BAOSTOCK_MIN_INTERVAL = 0.5        # 秒/请求
MAX_CONSECUTIVE_ERRORS = 10        # 连续错误熔断阈值
# ============ [Task#194] 2026-08-05二次封禁事故追加纪律 ============
# 事故: 8/5 17:10解禁后立即全量回补5193只(87min高强度)→18:31再封。
# ④分批限速: 每批BATCH_SIZE只, 批间sleep 30s+0-10s随机抖动
# ⑤熔断升级: 任何请求再遇10001011 → 立即停止+ban_status写re_banned事件
BATCH_SIZE = 50                    # 每批股票数
# ============ [Task#201] 2026-08-06 用户要求请求节奏进一步放保守 ============
# (历史: 单只间隔1.5s+0~0.3s, 批间60s+0~15s — t259已按用户裁决放开)
# ============ [Task#259] 2026-08-10 用户裁决放开保守限速 ============
# BaoStock官方规则: 日上限5万次+禁并发, 无QPS限制; 用户按日4万次预算执行
# (统一预算模块baostock_budget拦截), 单请求对齐t233已验证0.5s+0~0.2s;
# 本脚本每股2请求(日K+60minK) → 每股间隔2×0.5s=1.0s+2×0.2s抖动上限
# 批间休眠对齐t233: 60s+0~15s → 15s+0~5s
PER_STOCK_INTERVAL = 1.0           # 秒/股(2请求×0.5s/请求, t259: 原1.5)
STOCK_SLEEP_JITTER = 0.4           # 单只间隔附加随机抖动上限(2×0.2s, t259: 原0.3)
BATCH_SLEEP_BASE = 15              # 批间基础sleep秒数(t259: 原60)
BATCH_SLEEP_JITTER = 5             # 批间随机抖动上限秒数(t259: 原15)
BAN_STATUS_FILE = '/home/AIWealth/data/baostock_ban_status.json'
# ============ [Task#211] 尾部完整性自检(2026-08-06假完整事故根因修复) ============
# 事故: BaoStock对2044只(sz.00后段+创业板全部)返回空数据, 空返回走continue
# 不计错误, 22:04日志打出"全部完成"假象(实际8/6仅入库3150/5194行), 下游22:00
# 候选生成用残缺数据选错股。修复: 空返回单独计数+尾部自检门, 不过则退出码2。
EMPTY_RATE_MAX = 0.05        # 空返回率上限: 超过 → 自检不过
ROW_COMPLETENESS_MIN = 0.95  # 每交易日入库行数下限系数: <任务清单×95% → 自检不过
ALERT_FILE = '/home/AIWealth/logs/realtime/scheduler_alerts.log'
# ============ [Task#222] SQLite锁竞争加固(2026-08-07 263只日K缺口根因修复) ============
# 事故: baostock_recovery长跑实例持stocks.db写锁至19:06, 18:30日K更新前263只
# (sh.600000~600354连续段)撞锁失败(默认timeout=5s即抛database is locked)。
# 修复: timeout=60+busy_timeout=60000(t220补拉脚本已实证此配置对锁竞争零失败),
# 撞锁时排队等待而非抛异常 — 并发写锁问题的主保险。
SQLITE_TIMEOUT = 60              # sqlite3.connect timeout秒数
SQLITE_BUSY_TIMEOUT_MS = 60000   # PRAGMA busy_timeout毫秒
# ============================================================================
# ================================

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(message)s',
    handlers=[
        logging.FileHandler(LOG_FILE, encoding='utf-8'),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger(__name__)

# ============ [Task#259] BaoStock统一日预算埋点(4万次/日用户红线) ============
# 本脚本属日更链priority='p0'(仅受hard_limit=40000拦截); 预算耗尽 →
# 写日志+优雅停止+退出码5。模块自身异常fail-open放行(数据链可用性优先,
# 另有10001011熔断/连续错误熔断充当硬保险)。
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
try:
    import baostock_budget as _bb
except Exception as _e:          # 模块缺失不阻断日更链
    _bb = None

# ============ [Task#273] baostock socket死连接自旋守护(源自Task#270) ============
# 官方库socketutil.send_msg的while True: recv(8192)在对端关闭连接后recv持续
# 返回b''不抛异常 → 无sleep用户态死循环(8/10 t233停摆2.4h/91%CPU实证)。
# monkey-patch守护版(空recv返回None走库原生10002007"网络接收错误"路径 +
# socket 120s超时兜底), 须在bs.login()前生效 — 模块导入期即install。
# 根因与验证详见 research/results/t270_ops_incident/REPORT.md。
# 导入失败fail-open不阻断日更链(与_bb同款容错, 仅失去防自旋守护, 记警告)。
try:
    import baostock_socket_guard as _bsg
    _bsg.install()
except Exception as _e:
    logger.warning("[Task#273] socket守护安装失败(fail-open, 无防自旋保护): %s",
                   _e)
    _bsg = None


class BudgetExhaustedError(Exception):
    """[Task#259] 统一日预算耗尽 → 优雅停止专用异常(退出码5)。"""
    pass


def budget_acquire(n=1, priority='p0'):
    """[Task#259] BaoStock请求前统一预算申请: 额度不足raise
    BudgetExhaustedError; 预算模块自身异常fail-open放行。"""
    if _bb is None:
        return
    try:
        ok = _bb.acquire(n, priority)
    except Exception as e:
        logger.warning("[Task#259] 预算模块异常(fail-open放行): %s", e)
        return
    if not ok:
        raise BudgetExhaustedError(
            f"BaoStock统一日预算耗尽(priority={priority}, n={n})")


class QueryTimeoutError(Exception):
    pass


class BlacklistError(Exception):
    """[Task#194] BaoStock返回10001011黑名单错误 → 立即熔断专用异常。"""
    pass


class DeadConnectionError(Exception):
    """[Task#273] BaoStock socket死连接专用异常: 守护版send_msg防自旋返回
    None后, 库侧回10002007"网络接收错误" — 原socket重试无意义, 须
    logout+re-login重建socket(Task#270已验证login内部重建连接)。"""
    pass


def check_blacklist(rs):
    """[Task#194] 查询结果黑名单检测: 命中则raise BlacklistError。"""
    blob = str(rs.error_code) + str(rs.error_msg)
    if '10001011' in blob or '黑名单' in blob:
        raise BlacklistError(f'{rs.error_code} {rs.error_msg}')


def check_dead_connection(rs):
    """[Task#273] 死连接检测: 命中10002007/网络接收错误则raise
    DeadConnectionError(须在check_blacklist之后调用, 黑名单熔断优先)。"""
    blob = str(rs.error_code) + str(rs.error_msg)
    if '10002007' in blob or '网络接收错误' in blob:
        raise DeadConnectionError(f'{rs.error_code} {rs.error_msg}')


def relogin_rebuild(where):
    """[Task#273] 死连接后logout+re-login重建socket(对齐t233_runner同款守护)。
    返回(ok, emsg); re-login遇10001011由调用方按既有熔断语义处理, 本函数
    不引入新循环。"""
    try:
        bs.logout()
    except Exception:
        pass
    time.sleep(3)
    lg = bs.login()
    if lg.error_code != '0':
        return False, f"{lg.error_code} {lg.error_msg}"
    logger.info("[Task#273] %s: 死连接re-login重建socket成功", where)
    return True, ''


def write_re_banned_event(note):
    """[Task#194] 回补中途再封禁 → ban_status写re_banned事件(append到
    history数组, 字段风格与既有事件一致)+recovered翻回false。
    仅在当前状态为recovered=true时写(状态迁移语义, 避免封禁期每日刷事件)。"""
    ts = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    try:
        with open(BAN_STATUS_FILE, encoding='utf-8') as f:
            ban = json.load(f)
    except Exception:
        ban = {}
    if not ban.get('recovered') and not ban.get('pending_confirm'):
        return   # 本就处于banned态, 不重复追加事件
    ban['recovered'] = False
    ban['pending_confirm'] = False
    ban.setdefault('history', []).append({
        'event': 're_banned', 'detected_at': ts,
        'note': f'{note} (fetch_daily_kline熔断自动写入, Task#194)'})
    try:
        with open(BAN_STATUS_FILE, 'w', encoding='utf-8') as f:
            json.dump(ban, f, ensure_ascii=False, indent=2)
    except Exception as e:
        logger.error("re_banned事件写入失败: %s", e)


def write_alert(msg):
    """[Task#211] 追加scheduler_alerts.log(与daemon/cron告警同一人工巡检入口)。
    行格式沿用本脚本既有内联写法: {ts} [DATA_INTEGRITY] [DAILY_KLINE] {msg}"""
    try:
        with open(ALERT_FILE, 'a', encoding='utf-8') as f:
            f.write(f"{datetime.now().strftime('%Y-%m-%d %H:%M:%S')} "
                    f"[DATA_INTEGRITY] [DAILY_KLINE] {msg}\n")
    except Exception as e:
        logger.error("scheduler_alerts写入失败: %s", e)
    # [Task#256 G2] P0级条目(🚨/⛔等特征)落文件同时实时推企微
    try:
        sys.path.insert(0, '/home/AIWealth')
        from realtime.notify import push_alert
        push_alert(msg)
    except Exception:
        pass


def run_selfcheck(task_total, empty_codes, date_rows, error_map=None):
    """[Task#211] 尾部自检门: 全部循环结束后校验本次任务完整性。
    a) 空返回率>EMPTY_RATE_MAX  b) 任一交易日入库行数<task_total×ROW_COMPLETENESS_MIN
    任一命中 → ERROR日志+scheduler_alerts告警, 返回False(调用方退出码2);
    自检通过才允许打"全部完成"。task_total=本次任务清单长度。
    [Task#222] error_map={code: 错误信息}为收尾重试后仍失败清单(重试后最终
    口径); 报警文案分类: 空返回类(疑数据源未出数) vs 错误类(含locked疑并发
    锁), 样例各3只 — 值守据此快速区分接口问题与本地问题。"""
    error_map = error_map or {}
    if task_total <= 0:
        return True   # 本次无待处理股票(断点续传全跳过), 无从校验
    problems = []
    empty_rate = len(empty_codes) / task_total
    if empty_rate > EMPTY_RATE_MAX:
        problems.append(f"空返回率{empty_rate:.1%}({len(empty_codes)}/"
                        f"{task_total})>阈值{EMPTY_RATE_MAX:.0%}")
    min_rows = task_total * ROW_COMPLETENESS_MIN
    for date in sorted(date_rows):
        if date_rows[date] < min_rows:
            problems.append(f"{date}入库{date_rows[date]}行<任务清单"
                            f"{task_total}×{ROW_COMPLETENESS_MIN:.0%}"
                            f"={min_rows:.0f}行")
    if problems:
        # [Task#222] 分类文案: 空返回(数据源侧) vs 错误含locked(本地并发锁)
        locked = [c for c, m in error_map.items()
                  if 'locked' in str(m).lower()]
        problems.append(
            f"分类: 空返回{len(empty_codes)}只(疑数据源未出数, "
            f"样例:{','.join(empty_codes[:3]) or '无'})"
            f" / 错误{len(error_map)}只(含locked {len(locked)}只, 疑并发锁, "
            f"样例:{','.join(list(error_map)[:3]) or '无'})")
        for p in problems:
            logger.error(f"🚨 [Task#211自检] {p}")
        write_alert(f"🚨 日K拉取尾部自检不过(退出码2, 数据疑似残缺勿直接使用): "
                    f"{'; '.join(problems)}")
        return False
    logger.info(f"[Task#211自检] 通过: 空返回{len(empty_codes)}/{task_total}"
                f"(≤{EMPTY_RATE_MAX:.0%}), 重试后仍错误{len(error_map)}只, "
                f"各交易日入库行数≥{min_rows:.0f}")
    return True


def timeout_handler(signum, frame):
    raise QueryTimeoutError("Query timeout")


def safe_float(val, default=None):
    if val is None or val == '':
        return default
    try:
        v = float(val)
        if math.isnan(v) or math.isinf(v):
            return default
        return v
    except (ValueError, TypeError):
        return default


def safe_int(val, default=None):
    if val is None or val == '':
        return default
    try:
        return int(float(val))
    except (ValueError, TypeError):
        return default


def calc_rate(price, preclose):
    if price is None or preclose is None or preclose == 0:
        return None
    return round((price - preclose) / preclose * 100, 2)


def get_trading_dates(start_date, end_date):
    budget_acquire(1, 'p0')   # [Task#259]
    rs = bs.query_trade_dates(start_date=start_date, end_date=end_date)
    dates = []
    while rs.error_code == '0' and rs.next():
        row = rs.get_row_data()
        if row[1] == '1':
            dates.append(row[0])
    return dates


def get_all_stocks(trading_dates):
    """获取所有交易日的股票并集, 返回 {code: code_name}

    BaoStock的query_all_stock在当天17:00可能返回空(数据未就绪),
    fallback机制: 如果目标日期返回空，自动尝试前1-5个交易日
    """
    stock_map = {}
    # 取首尾+中间日获取完整股票列表
    sample_dates = [trading_dates[0], trading_dates[-1]]
    if len(trading_dates) > 10:
        sample_dates.append(trading_dates[len(trading_dates)//2])

    for date in sample_dates:
        budget_acquire(1, 'p0')   # [Task#259]
        rs = bs.query_all_stock(day=date)
        while rs.error_code == '0' and rs.next():
            row = rs.get_row_data()
            code, trade_status, code_name = row[0], row[1], row[2]
            if trade_status == '1' and code.startswith(STOCK_PREFIXES):
                stock_map[code] = code_name

    # Fallback: 如果返回0只股票(BaoStock数据延迟), 尝试前几个交易日
    if not stock_map:
        logger.warning("当前日期股票列表为空, 尝试使用前几个交易日的列表...")
        from datetime import datetime, timedelta
        base_date = datetime.strptime(trading_dates[0], '%Y-%m-%d')
        for offset in range(1, 6):
            fallback_date = (base_date - timedelta(days=offset)).strftime('%Y-%m-%d')
            budget_acquire(1, 'p0')   # [Task#259]
            rs = bs.query_all_stock(day=fallback_date)
            while rs.error_code == '0' and rs.next():
                row = rs.get_row_data()
                code, trade_status, code_name = row[0], row[1], row[2]
                if trade_status == '1' and code.startswith(STOCK_PREFIXES):
                    stock_map[code] = code_name
            if stock_map:
                logger.info(f"使用 {fallback_date} 的股票列表({len(stock_map)}只)作为fallback")
                break
    return stock_map


def get_daily_kline_range(code, start_date, end_date):
    """批量获取单只股票多日日K线, 返回 {date: row_data}"""
    budget_acquire(1, 'p0')   # [Task#259]
    fields = 'date,code,open,high,low,close,preclose,volume,amount,turn,pctChg,isST'
    rs = bs.query_history_k_data_plus(
        code, fields,
        start_date=start_date, end_date=end_date,
        frequency='d', adjustflag='3'
    )
    result = {}
    if rs.error_code != '0':
        check_blacklist(rs)   # [Task#194] 10001011→raise熔断, 其余错误仍返空
        check_dead_connection(rs)   # [Task#273] 10002007→raise re-login重建
        return result
    while rs.next():
        row = rs.get_row_data()
        result[row[0]] = row
    return result


def get_60min_kline_range(code, start_date, end_date):
    """批量获取单只股票多日60min K线, 返回 {date: {hour: data}}"""
    budget_acquire(1, 'p0')   # [Task#259]
    fields = 'date,time,code,open,high,low,close,volume,amount'
    rs = bs.query_history_k_data_plus(
        code, fields,
        start_date=start_date, end_date=end_date,
        frequency='60', adjustflag='3'
    )
    result = {}
    if rs.error_code != '0':
        check_blacklist(rs)   # [Task#194] 10001011→raise熔断
        check_dead_connection(rs)   # [Task#273] 10002007→raise re-login重建
        return result
    while rs.next():
        row = rs.get_row_data()
        date = row[0]
        time_str = row[1][8:14] if len(row[1]) >= 14 else ''
        hour_key = TIME_MAP.get(time_str)
        if hour_key:
            if date not in result:
                result[date] = {}
            result[date][hour_key] = {
                'open': safe_float(row[3]),
                'high': safe_float(row[4]),
                'low': safe_float(row[5]),
                'close': safe_float(row[6]),
                'volume': safe_int(row[7]),
                'amount': safe_float(row[8]),
            }
    return result


def build_record(code, code_name, daily_row, hour_data):
    preclose = safe_float(daily_row[6])
    open_p = safe_float(daily_row[2])
    high_p = safe_float(daily_row[3])
    low_p = safe_float(daily_row[4])
    close_p = safe_float(daily_row[5])
    volume = safe_int(daily_row[7])
    amount = safe_float(daily_row[8])
    turn = safe_float(daily_row[9])
    isST_val = safe_int(daily_row[11], 0)

    if isST_val is None or isST_val == 0:
        if 'ST' in code_name.upper():
            isST_val = 1
        else:
            isST_val = 0

    record = {
        'date': daily_row[0],
        'code': code,
        'code_name': code_name,
        'preclose': preclose,
        'open': open_p,
        'open_rate': calc_rate(open_p, preclose),
        'high': high_p,
        'high_rate': calc_rate(high_p, preclose),
        'low': low_p,
        'low_rate': calc_rate(low_p, preclose),
        'close': close_p,
        'close_rate': calc_rate(close_p, preclose),
        'volume': volume,
        'amount': amount,
        'turn': turn,
        'isST': isST_val,
    }

    for h in ['hour1', 'hour2', 'hour3', 'hour4']:
        hd = hour_data.get(h, {}) if hour_data else {}
        h_open = hd.get('open')
        h_high = hd.get('high')
        h_low = hd.get('low')
        h_close = hd.get('close')
        record[f'{h}_open'] = h_open
        record[f'{h}_open_rate'] = calc_rate(h_open, preclose)
        record[f'{h}_high'] = h_high
        record[f'{h}_high_rate'] = calc_rate(h_high, preclose)
        record[f'{h}_low'] = h_low
        record[f'{h}_low_rate'] = calc_rate(h_low, preclose)
        record[f'{h}_close'] = h_close
        record[f'{h}_close_rate'] = calc_rate(h_close, preclose)
        record[f'{h}_volume'] = hd.get('volume')
        record[f'{h}_amount'] = hd.get('amount')

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


def retry_failed_round(empty_codes, error_codes, stock_map, start_date,
                       end_date, conn=None, sleep_scale=1.0,
                       fetch_daily=None, fetch_hour=None):
    """[Task#211→#222] 收尾重试轮: 空返回类+错误类(含database is locked
    并发锁)合并清单二次重试一轮(不递归)。限速纪律与主循环完全一致:
    每股PER_STOCK_INTERVAL+抖动, 每批BATCH_SIZE只批间BATCH_SLEEP_BASE+抖动;
    10001011熔断语义同主循环(退出码3)。8/7事故263只locked错误若有此机制,
    19:06锁释放后当晚即可自愈。返回(仍空codes, 仍错误{code: msg}, 本轮插入
    行数)。dry-run经sleep_scale压缩+mock fetch注入。"""
    fetch_daily = fetch_daily or get_daily_kline_range
    fetch_hour = fetch_hour or get_60min_kline_range
    use_alarm = sleep_scale >= 1.0   # dry-run不挂超时alarm
    _empty_set = set(empty_codes)
    retry_codes = list(empty_codes) + [c for c in error_codes
                                       if c not in _empty_set]
    still_empty = []
    still_error = {}
    inserted = 0
    logger.info(f"[Task#222 retry-failed] 对空返回{len(empty_codes)}只+错误"
                f"{len(error_codes)}只合并二次重试一轮(限速纪律不变: "
                f"{PER_STOCK_INTERVAL}s/股+抖动, "
                f"每批{BATCH_SIZE}只批间{BATCH_SLEEP_BASE}s+抖动, 一轮为限不递归)")
    for i, code in enumerate(retry_codes):
        _t_stock = time.time()
        try:
            if use_alarm:
                signal.alarm(QUERY_TIMEOUT)
            daily_data = fetch_daily(code, start_date, end_date)
            hour_data_all = (fetch_hour(code, start_date, end_date)
                             if daily_data else {})
            if use_alarm:
                signal.alarm(0)
            if not daily_data:
                still_empty.append(code)
            else:
                for date, daily_row in daily_data.items():
                    preclose = safe_float(daily_row[6])
                    if preclose is None or preclose == 0:
                        continue
                    record = build_record(code, stock_map.get(code, ''),
                                          daily_row, hour_data_all.get(date, {}))
                    if conn is not None:
                        conn.execute(INSERT_SQL, record)
                    inserted += 1
        except BlacklistError as e:
            # [Task#194] 重试轮再遇10001011 → 熔断语义与主循环完全一致
            if use_alarm:
                signal.alarm(0)
            logger.error(f"⛔ [retry-failed] {code} 命中黑名单错误({e}), "
                         f"再封禁熔断立即停止!")
            write_re_banned_event(f"retry-failed重试轮{code}再遇10001011: {e}")
            write_alert(f"⛔再封禁熔断(retry-failed轮): {code} {e}")
            if conn is not None:
                conn.commit()
                conn.close()
            sys.exit(3)
        except DeadConnectionError as e:
            # [Task#273] 重试轮遇死连接(守护版send_msg防自旋返回10002007):
            # logout+re-login重建socket后继续后续股, 本股计仍错误(一轮为限
            # 不递归的既有纪律不变); re-login再遇10001011 → 熔断语义同主循环
            if use_alarm:
                signal.alarm(0)
            still_error[code] = f'dead_connection: {str(e)[:60]}'
            logger.warning(f"[Task#273][retry-failed] {code} 疑死连接({e}), "
                           f"re-login重建socket")
            ok, emsg = relogin_rebuild('retry-failed轮')
            if not ok:
                if '10001011' in emsg or '黑名单' in emsg:
                    logger.error(f"⛔ [retry-failed] 死连接re-login再遇黑名单"
                                 f"({emsg}), 熔断立即停止!")
                    write_re_banned_event(
                        f"retry-failed轮死连接re-login再遇10001011: {emsg}")
                    write_alert(f"⛔再封禁熔断(retry-failed死连接re-login): "
                                f"{emsg}")
                    if conn is not None:
                        conn.commit()
                        conn.close()
                    sys.exit(3)
                logger.warning(f"[Task#273][retry-failed] re-login失败"
                               f"(非黑名单): {emsg}")
        except BudgetExhaustedError as e:
            # [Task#259] 重试轮预算耗尽 → commit保留已入库行, 优雅停止退出码5
            if use_alarm:
                signal.alarm(0)
            logger.error(f"⛔ [Task#259][retry-failed] {e}, 优雅停止(退出码5)")
            write_alert(f"[Task#259] 日预算耗尽, retry-failed轮优雅停止于{code}")
            if conn is not None:
                conn.commit()
                conn.close()
            sys.exit(5)
        except Exception as e:
            if use_alarm:
                signal.alarm(0)
            still_error[code] = str(e)[:80]
            logger.warning(f"[retry-failed] {code} 异常: {e}")
        # 限速: 单只间隔(与主循环同参数; dry-run按sleep_scale压缩回放)
        _gap = (PER_STOCK_INTERVAL + random.uniform(0, STOCK_SLEEP_JITTER)
                - (time.time() - _t_stock))
        if _gap > 0:
            time.sleep(_gap * sleep_scale)
        # 限速: 批间休眠(与主循环同参数)
        if (i + 1) % BATCH_SIZE == 0 and i + 1 < len(retry_codes):
            _pause = BATCH_SLEEP_BASE + random.uniform(0, BATCH_SLEEP_JITTER)
            logger.info(f"[Task#222 retry-failed分批] 第{(i + 1) // BATCH_SIZE}批"
                        f"({BATCH_SIZE}只)完成, 批间休眠{_pause:.1f}s"
                        + (f"(回放压缩为{_pause * sleep_scale:.2f}s)"
                           if sleep_scale < 1.0 else ""))
            time.sleep(_pause * sleep_scale)
    logger.info(f"[Task#222 retry-failed] 重试轮完成: 恢复"
                f"{len(retry_codes) - len(still_empty) - len(still_error)}只, "
                f"仍空{len(still_empty)}只, 仍错误{len(still_error)}只, "
                f"插入{inserted}行")
    return still_empty, still_error, inserted


def retry_empty_round(empty_codes, stock_map, start_date, end_date, **kwargs):
    """[兼容shim] Task#222起由retry_failed_round取代(错误类并入重试);
    保留旧签名与(仍空codes, 插入行数)返回, 供历史研究脚本(如t220)import不破。"""
    still_empty, _still_error, inserted = retry_failed_round(
        empty_codes, [], stock_map, start_date, end_date, **kwargs)
    return still_empty, inserted


def update_progress(progress):
    try:
        with open(PROGRESS_FILE, 'w') as f:
            json.dump(progress, f, ensure_ascii=False, indent=2)
    except Exception:
        pass


def get_existing_codes(conn, start_date, end_date):
    """获取已有数据的(date,code)集合用于断点续传"""
    cur = conn.execute(
        "SELECT DISTINCT code FROM stock_kline WHERE date >= ? AND date <= ? GROUP BY code HAVING COUNT(*) >= ?",
        (start_date, end_date, 20)  # 如果一只股票已有>=20天数据(约26天的77%),认为已完成
    )
    return set(row[0] for row in cur.fetchall())


def dry_run_main(start_date, end_date, no_retry=False):
    """[Task#194] dry-run: mock批循环, 不登录/不发任何BaoStock请求。
    验证: 分批节奏(50只/批+批间sleep参数) + 10001011熔断写re_banned事件。
    环境变量:
      T194_MOCK_STOCKS=<N>         mock股票数(默认120)
      T194_MOCK_BLACKLIST_AT=<i>   第i只股(1序)命中10001011触发熔断
      T194_MOCK_BAN_STATUS=<path>  熔断事件写入的mock ban_status文件
      T201_MAX_STOCKS=<N>          本次运行处理量上限(达到即优雅收工退出码4)
    [Task#211/#222] 尾部自检/收尾重试轮验证环境变量:
      T211_MOCK_EMPTY_AT=<i,j,..>  第i/j只股(1序)mock为"query成功但0行"空返回
      T211_MOCK_ALERT_FILE=<path>  自检告警重定向到隔离路径(不碰生产alerts)
      T211_MOCK_RETRY_RECOVER=<i,j,..|all>  retry轮哪些空返回股(1序)恢复有数据
      T222_MOCK_ERROR_AT=<i,j,..>  第i/j只股(1序)mock为错误类(database is locked)
      T222_MOCK_RETRY_FIX=<i,j,..|all>      retry轮哪些错误股(1序)恢复有数据
    sleep压缩100倍回放, 日志打印真实参数值供审计."""
    global BAN_STATUS_FILE, ALERT_FILE
    mock_ban = os.environ.get('T194_MOCK_BAN_STATUS')
    if mock_ban:
        BAN_STATUS_FILE = mock_ban
    mock_alert = os.environ.get('T211_MOCK_ALERT_FILE')
    if mock_alert:
        ALERT_FILE = mock_alert
    n = int(os.environ.get('T194_MOCK_STOCKS', '120'))
    black_at = int(os.environ.get('T194_MOCK_BLACKLIST_AT', '0'))
    max_stocks = int(os.environ.get('T201_MAX_STOCKS', '0'))
    empty_at = set(int(x) for x in
                   os.environ.get('T211_MOCK_EMPTY_AT', '').split(',') if x)
    err_at = set(int(x) for x in
                 os.environ.get('T222_MOCK_ERROR_AT', '').split(',') if x)
    empty_codes = []
    error_msgs = {}   # [Task#222] mock错误类清单{code: msg}
    logger.info(f"[dry-run] mock回补 {start_date}~{end_date}: {n}只股, "
                f"限速参数[Task#201]: 每股间隔{PER_STOCK_INTERVAL}s+0-"
                f"{STOCK_SLEEP_JITTER}s抖动(2请求/股), 每批{BATCH_SIZE}只, "
                f"批间sleep {BATCH_SLEEP_BASE}s+0-{BATCH_SLEEP_JITTER}s抖动")
    for i in range(n):
        code = f"sh.60{i:04d}"
        if black_at and i + 1 == black_at:
            e = "10001011 黑名单用户(mock)"
            logger.error(f"⛔ [dry-run] {code} 命中黑名单错误({e}), "
                         f"再封禁熔断立即停止!")
            write_re_banned_event(f"回补中途{code}再遇10001011: {e}")
            logger.error(f"[dry-run] re_banned事件已写入 {BAN_STATUS_FILE}, "
                         f"退出码3(与真实熔断一致)")
            sys.exit(3)
        # [Task#222] mock错误类(如database is locked): 计入错误清单入收尾重试
        if i + 1 in err_at:
            error_msgs[code] = 'database is locked (mock)'
            logger.warning(f"[dry-run][Task#222] {code} mock错误"
                           f"(database is locked, 累计{len(error_msgs)})")
        # [Task#211] mock空返回: query成功但0行 → 计数+收集(与真实路径同语义)
        elif i + 1 in empty_at:
            empty_codes.append(code)
            logger.info(f"[dry-run][Task#211] {code} mock空返回"
                        f"(query成功但0行, 累计{len(empty_codes)})")
        time.sleep((PER_STOCK_INTERVAL
                    + random.uniform(0, STOCK_SLEEP_JITTER)) / 100)  # 100倍压缩
        # [Task#201] 渐进处理: 达本次运行上限优雅收工(与真实路径退出码4一致)
        if max_stocks and i + 1 >= max_stocks and i + 1 < n:
            logger.info(f"[Task#201渐进][dry-run] 本次运行处理量达上限"
                        f"{max_stocks}只, 优雅收工(剩余{n - max_stocks}只"
                        f"由断点续传续跑), 退出码4")
            sys.exit(4)   # main()返回值会被丢弃, 必须直接exit(与熔断exit(3)同款)
        if (i + 1) % BATCH_SIZE == 0 and i + 1 < n:
            _pause = BATCH_SLEEP_BASE + random.uniform(0, BATCH_SLEEP_JITTER)
            logger.info(f"[Task#194分批][dry-run] 第{(i + 1) // BATCH_SIZE}批"
                        f"({BATCH_SIZE}只)完成, 批间休眠{_pause:.1f}s"
                        f"(回放压缩为{_pause / 100:.2f}s)")
            time.sleep(_pause / 100)

    # [Task#211→#222] 收尾重试轮验证: 走真实retry_failed_round循环(mock fetch
    # 注入, conn=None不落库, sleep压缩100倍), 空返回+错误类合并重试+限速语义
    error_map = dict(error_msgs)
    if not no_retry and (empty_codes or error_map):
        recover_env = os.environ.get('T211_MOCK_RETRY_RECOVER', '')
        if recover_env == 'all':
            recover = set(empty_codes)
        else:
            idx = set(int(x) for x in recover_env.split(',') if x)
            recover = set(c for k, c in enumerate(empty_codes)
                          if k + 1 in idx)
        fix_env = os.environ.get('T222_MOCK_RETRY_FIX', '')
        if fix_env == 'all':
            fixed = set(error_map)
        else:
            fidx = set(int(x) for x in fix_env.split(',') if x)
            fixed = set(c for k, c in enumerate(error_map)
                        if k + 1 in fidx)

        def _mock_daily(code, sd, ed):
            if code in recover or code in fixed:
                # 字段序同真实日K: date,code,o,h,l,c,preclose,vol,amt,turn,pctChg,isST
                return {sd: [sd, code, '10.0', '11.0', '9.5', '10.5',
                             '10.0', '1000000', '10500000', '2.5', '5.0', '0']}
            if code in error_map:
                # 错误类未修复 → 重试轮仍抛并发锁异常(与真实路径同语义)
                raise sqlite3.OperationalError('database is locked (mock)')
            return {}

        def _mock_hour(code, sd, ed):
            return {}

        empty_codes, error_map, _ins = retry_failed_round(
            empty_codes, list(error_map), {}, start_date, end_date, conn=None,
            sleep_scale=0.01, fetch_daily=_mock_daily, fetch_hour=_mock_hour)

    # [Task#211] 尾部自检门: 走真实run_selfcheck(mock入库行数=非空非错股数1行/只)
    date_rows = {start_date: n - len(empty_codes) - len(error_map)}
    if not run_selfcheck(n, empty_codes, date_rows, error_map):
        logger.error(f"⛔ [dry-run] 尾部自检不过(空返回{len(empty_codes)}/错误"
                     f"{len(error_map)}/{n}), "
                     f"告警已写入 {ALERT_FILE}, 退出码2(与真实路径一致)")
        sys.exit(2)
    logger.info(f"[dry-run] mock回补全部完成! {n}只股, "
                f"批数{(n + BATCH_SIZE - 1) // BATCH_SIZE}, "
                f"空返回{len(empty_codes)}, 错误{len(error_map)}, "
                f"未触发熔断, 尾部自检通过")
    return 0


def main():
    # [Task#194] --dry-run: mock批循环, 不真实请求BaoStock(验证分批/熔断路径)
    #   配合环境变量 T194_MOCK_BLACKLIST_AT=<序号> 模拟第N只股命中10001011
    # [Task#222] 收尾重试轮默认启用(空返回类+错误类合并, 清单为空时零额外请求);
    #   --no-retry显式关闭; --retry-empty旧旗标保留兼容(现为no-op)
    dry_run = '--dry-run' in sys.argv
    no_retry = '--no-retry' in sys.argv
    argv = [a for a in sys.argv
            if a not in ('--dry-run', '--retry-empty', '--no-retry')]
    if len(argv) < 2:
        print("用法:")
        print("  单日模式: python3 fetch_daily_kline.py 2026-06-30")
        print("  回补模式: python3 fetch_daily_kline.py 2026-05-23 2026-06-30")
        sys.exit(1)

    start_date = argv[1]
    end_date = argv[2] if len(argv) > 2 else start_date

    # 设置signal handler
    signal.signal(signal.SIGALRM, timeout_handler)

    if dry_run:
        return dry_run_main(start_date, end_date, no_retry)

    # 登录BaoStock([Task#259] login也计1次预算)
    logger.info("登录BaoStock...")
    budget_acquire(1, 'p0')
    lg = bs.login()
    if lg.error_code != '0':
        logger.error(f"BaoStock登录失败: {lg.error_msg}")
        # [Task #31] 2026-07-24 BaoStock封禁事故(10001011黑名单): login失败自动
        # 降级到腾讯fqkline备用源(仅单日模式; hour列留NULL, 恢复后需重补当日)
        if start_date == end_date:
            import subprocess
            logger.warning("[降级模式] 自动切换腾讯fqkline备用源: "
                           "fetch_daily_kline_fallback.py %s", start_date)
            r = subprocess.run(
                [sys.executable,
                 os.path.join(os.path.dirname(os.path.abspath(__file__)),
                              'fetch_daily_kline_fallback.py'), start_date])
            sys.exit(r.returncode)
        sys.exit(1)
    logger.info("BaoStock登录成功")

    # 获取交易日列表
    logger.info(f"获取交易日列表: {start_date} ~ {end_date}")
    trading_dates = get_trading_dates(start_date, end_date)
    logger.info(f"共{len(trading_dates)}个交易日")
    if not trading_dates:
        logger.warning("无交易日, 退出")
        bs.logout()
        sys.exit(0)

    # 获取所有股票列表
    logger.info("获取股票列表...")
    stock_map = get_all_stocks(trading_dates)
    total_stocks = len(stock_map)
    logger.info(f"共{total_stocks}只股票")

    # 连接数据库([Task#222] timeout+busy_timeout加固: 撞锁排队等待而非~5s即抛
    # database is locked — 8/7事故263只缺口的主保险)
    conn = sqlite3.connect(DB_PATH, timeout=SQLITE_TIMEOUT)
    conn.execute(f"PRAGMA busy_timeout={SQLITE_BUSY_TIMEOUT_MS}")
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")

    # 断点续传: 跳过已完整获取的股票
    completed_codes = get_existing_codes(conn, start_date, end_date)
    logger.info(f"已完成股票: {len(completed_codes)}只, 待处理: {total_stocks - len(completed_codes)}只")

    # 初始化进度
    progress = {
        'start_date': start_date,
        'end_date': end_date,
        'total_days': len(trading_dates),
        'total_stocks': total_stocks,
        'completed': len(completed_codes),
        'speed': '',
        'errors': [],
        'started_at': datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
    }
    update_progress(progress)

    total_inserted = 0
    errors_count = 0
    empty_codes = []         # [Task#211] query成功但0行的股清单(区分于报错重试类)
    error_msgs = {}          # [Task#222] 报错股清单{code: 错误信息}(含locked, 入收尾重试)
    consecutive_errors = 0   # [Task #31] 连续错误熔断计数
    # [Task#201] 渐进处理: 恢复流水线经环境变量传入本次运行处理量上限(0=不限)
    max_stocks = int(os.environ.get('T201_MAX_STOCKS', '0'))
    if max_stocks:
        logger.info(f"[Task#201渐进] 本次运行处理量上限: {max_stocks}只")
    start_time = time.time()
    stocks_list = [(code, name) for code, name in stock_map.items() if code not in completed_codes]

    for i, (code, code_name) in enumerate(stocks_list):
        _t_stock = time.time()
        try:
            signal.alarm(QUERY_TIMEOUT)

            # 批量获取日K (全日期范围)
            daily_data = get_daily_kline_range(code, start_date, end_date)
            if not daily_data:
                signal.alarm(0)
                consecutive_errors = 0
                # [Task#211] 空返回单独计数+收集清单(8/6假完整事故根因:
                # 此分支原先不计数不告警, 2044只静默漏拉仍打"全部完成")
                empty_codes.append(code)
                # [Task #31]铁律1/[Task#201]: 空结果路径同样保持单只间隔+抖动
                _gap = (PER_STOCK_INTERVAL
                        + random.uniform(0, STOCK_SLEEP_JITTER)
                        - (time.time() - _t_stock))
                if _gap > 0:
                    time.sleep(_gap)
                continue

            # 批量获取60min K (全日期范围)
            hour_data_all = get_60min_kline_range(code, start_date, end_date)

            signal.alarm(0)
            consecutive_errors = 0

            # 对每个有效交易日组装记录并插入
            for date, daily_row in daily_data.items():
                preclose = safe_float(daily_row[6])
                if preclose is None or preclose == 0:
                    continue
                hour_data = hour_data_all.get(date, {})
                record = build_record(code, code_name, daily_row, hour_data)
                conn.execute(INSERT_SQL, record)
                total_inserted += 1

        except QueryTimeoutError:
            signal.alarm(0)
            errors_count += 1
            consecutive_errors += 1
            error_msgs[code] = 'timeout'   # [Task#222] 错误类入收尾重试清单
            logger.warning(f"{code} 查询超时, 跳过")
            if len(progress['errors']) < 100:
                progress['errors'].append(f"{code}|timeout")
        except BlacklistError as e:
            # [Task#194] 熔断: 回补中途任何请求再遇10001011 → 立即停止
            signal.alarm(0)
            logger.error(f"⛔ {code} 命中黑名单错误({e}), 再封禁熔断立即停止! "
                         f"(Task#194: 禁止继续轰炸加重封禁判定)")
            write_re_banned_event(f"回补中途{code}再遇10001011: {e}")
            try:
                with open('/home/AIWealth/logs/realtime/scheduler_alerts.log',
                          'a') as _af:
                    _af.write(f"{datetime.now().strftime('%Y-%m-%d %H:%M:%S')} "
                              f"[DATA_INTEGRITY] [DAILY_KLINE] ⛔再封禁熔断: "
                              f"{code} {e}\n")
            except Exception:
                pass
            conn.commit()
            conn.close()
            sys.exit(3)
        except BudgetExhaustedError:
            # [Task#265] 评审修复(Sven-Critical): 原实现靠宽泛except Exception
            # 捕获后用str(e)[:80]截断串匹配"统一日预算耗尽"识别, 消息截断/格式
            # 变化可致exit=5不触发、日更链误判成功。改为独立异常分支精确捕获,
            # 预算耗尽必然走"日志+scheduler_alerts告警+退出码5"优雅收工路径
            signal.alarm(0)
            logger.error(f"⛔ [Task#259] 日预算耗尽, 优雅收工(已处理{i}只, "
                         f"commit保留进度, 断点续传次日续), 退出码5")
            write_alert(f"[Task#259] BaoStock日预算耗尽, fetch_daily_kline优雅"
                        f"停止于{code}(已处理{i}/{len(stocks_list)}只)")
            conn.commit()
            progress['completed'] = len(completed_codes) + i
            progress['total_inserted'] = total_inserted
            update_progress(progress)
            conn.close()
            bs.logout()
            sys.exit(5)
        except DeadConnectionError as e:
            # [Task#273] socket死连接(守护版send_msg防自旋返回10002007):
            # 原socket重试无意义 → logout+re-login重建连接; 本股计入错误清单
            # 由收尾重试轮补拉(对齐既有重试结构); 同时计入连续错误熔断计数
            # (re-login持续失败则由既有MAX_CONSECUTIVE_ERRORS熔断接管);
            # re-login再遇10001011 → 熔断语义与BlacklistError分支完全一致
            signal.alarm(0)
            errors_count += 1
            consecutive_errors += 1
            error_msgs[code] = f'dead_connection: {str(e)[:60]}'
            logger.warning(f"[Task#273] {code} 疑死连接({e}), "
                           f"re-login重建socket")
            if len(progress['errors']) < 100:
                progress['errors'].append(f"{code}|dead_connection")
            ok, emsg = relogin_rebuild('主循环')
            if not ok:
                if '10001011' in emsg or '黑名单' in emsg:
                    logger.error(f"⛔ 死连接re-login再遇黑名单({emsg}), "
                                 f"再封禁熔断立即停止!")
                    write_re_banned_event(
                        f"死连接re-login再遇10001011: {emsg}")
                    write_alert(f"⛔再封禁熔断(死连接re-login): {emsg}")
                    conn.commit()
                    conn.close()
                    sys.exit(3)
                logger.warning(f"[Task#273] re-login失败(非黑名单): {emsg}, "
                               f"交连续错误熔断计数处理")
        except Exception as e:
            signal.alarm(0)
            errors_count += 1
            consecutive_errors += 1
            error_msgs[code] = str(e)[:80]   # [Task#222] 含locked, 入收尾重试清单
            logger.warning(f"{code} 异常: {e}")
            if len(progress['errors']) < 100:
                progress['errors'].append(f"{code}|{str(e)[:80]}")

        # [Task #31] 铁律2: 连续错误熔断, 禁止重试轰炸(2026-07-24封禁事故)
        if consecutive_errors >= MAX_CONSECUTIVE_ERRORS:
            logger.error(f"连续{consecutive_errors}只股票请求异常, 熔断终止! "
                         f"(疑似封禁/网络故障, 禁止继续轰炸数据源)")
            try:
                with open('/home/AIWealth/logs/realtime/scheduler_alerts.log',
                          'a') as _af:
                    _af.write(f"{datetime.now().strftime('%Y-%m-%d %H:%M:%S')} "
                              f"[DATA_INTEGRITY] [DAILY_KLINE] 连续"
                              f"{consecutive_errors}股异常熔断终止\n")
            except Exception:
                pass
            break

        # [Task #31]铁律1/[Task#201]: 每股2请求(日K+60minK), 间隔1.5s+抖动/股
        _gap = (PER_STOCK_INTERVAL + random.uniform(0, STOCK_SLEEP_JITTER)
                - (time.time() - _t_stock))
        if _gap > 0:
            time.sleep(_gap)

        # [Task#201] 渐进处理: 达本次运行上限 → commit+保存progress游标后
        # 优雅收工退出码4(恢复流水线据此记账写daily_cap_reached事件次日续)
        if max_stocks and i + 1 >= max_stocks and i + 1 < len(stocks_list):
            conn.commit()
            progress['completed'] = len(completed_codes) + i + 1
            progress['total_inserted'] = total_inserted
            progress['errors_count'] = errors_count
            progress['capped_at'] = datetime.now().strftime(
                '%Y-%m-%d %H:%M:%S')
            update_progress(progress)
            logger.info(f"[Task#201渐进] 本次运行处理量达上限{max_stocks}只, "
                        f"优雅收工(剩余{len(stocks_list) - i - 1}只由断点"
                        f"续传机制次日续跑), 退出码4")
            conn.close()
            bs.logout()
            sys.exit(4)

        # [Task#194]铁律4/[Task#201]: 分批限速 — 每批BATCH_SIZE只, 批间60s+抖动
        if (i + 1) % BATCH_SIZE == 0 and i + 1 < len(stocks_list):
            _pause = BATCH_SLEEP_BASE + random.uniform(0, BATCH_SLEEP_JITTER)
            logger.info(f"[Task#194分批] 第{(i + 1) // BATCH_SIZE}批"
                        f"({BATCH_SIZE}只)完成, 批间休眠{_pause:.1f}s")
            time.sleep(_pause)

        # 每COMMIT_BATCH只commit + 更新进度
        if (i + 1) % COMMIT_BATCH == 0:
            conn.commit()
            elapsed = time.time() - start_time
            speed = (i + 1) / elapsed if elapsed > 0 else 0
            progress['completed'] = len(completed_codes) + i + 1
            progress['speed'] = f"{speed:.1f} stocks/s"
            progress['total_inserted'] = total_inserted
            progress['errors_count'] = errors_count
            eta_seconds = (len(stocks_list) - i - 1) / speed if speed > 0 else 0
            progress['eta_minutes'] = round(eta_seconds / 60, 1)
            update_progress(progress)
            logger.info(f"进度 {i+1}/{len(stocks_list)}, 插入{total_inserted}条, "
                       f"错误{errors_count}, 空返回{len(empty_codes)}, "
                       f"速度{speed:.1f}/s, ETA {eta_seconds/60:.0f}min")

    conn.commit()

    # [Task#222] 收尾重试轮(Task#211 retry-empty扩展为retry-failed, 默认启用):
    # 空返回类+错误类(含database is locked并发锁)合并清单同纪律重试一轮;
    # 重试后自检门用重试后的最终口径判定。清单为空时零额外请求;
    # 连续错误熔断break后不重试(禁止对疑似封禁/故障源继续轰炸)
    error_map = dict(error_msgs)
    if not no_retry and (empty_codes or error_map) \
            and consecutive_errors < MAX_CONSECUTIVE_ERRORS:
        empty_codes, error_map, retry_inserted = retry_failed_round(
            empty_codes, list(error_map), stock_map, start_date, end_date,
            conn=conn)
        total_inserted += retry_inserted
        conn.commit()

    # [Task#211] 尾部自检门: 每交易日入库行数取DB实测(与8/6事故核验同口径,
    # 含断点续传已完成股), 预期数=本次任务清单长度len(stocks_list)
    date_rows = {}
    for d in trading_dates:
        date_rows[d] = conn.execute(
            "SELECT COUNT(*) FROM stock_kline WHERE date=?",
            (d,)).fetchone()[0]
    selfcheck_ok = run_selfcheck(len(stocks_list), empty_codes, date_rows,
                                 error_map)

    conn.close()
    bs.logout()

    elapsed = time.time() - start_time
    progress['completed'] = total_stocks
    progress['total_inserted'] = total_inserted
    progress['errors_count'] = errors_count
    progress['empty_returns'] = len(empty_codes)   # [Task#211]
    progress['failed_after_retry'] = len(error_map)   # [Task#222] 重试后仍错误
    progress['finished_at'] = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    progress['elapsed_minutes'] = round(elapsed / 60, 1)
    update_progress(progress)

    # [Task#211] 自检通过才打"全部完成"; 不过则退出码2(数据疑似残缺)
    if not selfcheck_ok:
        logger.error(f"⛔ 拉取结束但尾部自检不过: 处理{len(stocks_list)}只, "
                     f"插入{total_inserted}条, 错误{errors_count}"
                     f"(重试后仍{len(error_map)}), "
                     f"空返回{len(empty_codes)}, 耗时{elapsed/60:.1f}分钟 "
                     f"— 数据疑似残缺, 退出码2")
        sys.exit(2)

    logger.info(f"全部完成! 处理{len(stocks_list)}只股票, 插入{total_inserted}条, "
               f"错误{errors_count}(重试后仍{len(error_map)}), "
               f"空返回{len(empty_codes)}, 耗时{elapsed/60:.1f}分钟")


if __name__ == '__main__':
    try:
        main()
    except BudgetExhaustedError as e:
        # [Task#259] 主循环外(login/交易日/股票列表)预算耗尽的优雅兜底
        logger.error(f"⛔ [Task#259] {e}, 优雅停止(退出码5)")
        write_alert(f"[Task#259] BaoStock日预算耗尽, fetch_daily_kline启动段"
                    f"优雅停止: {e}")
        sys.exit(5)
