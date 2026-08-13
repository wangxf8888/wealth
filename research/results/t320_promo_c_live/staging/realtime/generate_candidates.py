#!/usr/bin/env python3
"""
晚间候选股生成 (每个交易日17:00触发)
========================================
策略无关框架 - 动态加载config.py中所有active策略，调用标准接口生成候选股。

新增策略 = 修改config.py + 放策略文件到strategies/ → 无需改此文件。

用法:
  python realtime/generate_candidates.py --date 2026-07-18
  python realtime/generate_candidates.py   (自动使用DB最新交易日)
"""
import sys
import os
import json
import time
import importlib
import argparse
import sqlite3
from datetime import datetime, timedelta

# 确保项目根目录在path中
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT_ROOT)

from realtime.config import ACTIVE_STRATEGIES, DATA_DB, OUTPUT_DIR, LOG_DIR, CANDIDATE_TOP_N
from realtime.data_feed import RealtimeDataFeed
from trading_rules import limit_prices

# [Task#285] 策略冻结名单(用户批准A案2026-08-11): 防御式导入,
# 配置缺失/异常降级为空名单(不标注), 不炸候选生成主链
try:
    from realtime.config import STRATEGY_FROZEN
except Exception as _e:
    print(f"[ERROR][冻结标注] STRATEGY_FROZEN配置读取异常({_e!r}), "
          f"降级为不标注继续")
    STRATEGY_FROZEN = []

# [Task#320] 晋级率门控标注配置: 防御式导入, 缺失/异常降级为不标注
# (候选照常生成, 此处仅加顶层标注字段; 9:25权威判定在morning_decision)
try:
    from realtime.config import PROMO_GATE_ENABLED, PROMO_GATE_THRESHOLD
except Exception as _e:
    print(f"[ERROR][晋级率标注] 配置读取异常({_e!r}), 降级为不标注继续")
    PROMO_GATE_ENABLED, PROMO_GATE_THRESHOLD = False, 0.30


# =============================================================================
# 策略动态加载
# =============================================================================

def load_strategy(cfg: dict):
    """动态import策略类并实例化。"""
    module = importlib.import_module(cfg['module'])
    cls = getattr(module, cfg['class'])
    return cls()


# =============================================================================
# 工具函数
# =============================================================================

def _alert(msg: str):
    """写scheduler_alerts.log(与daemon/cron告警同一人工巡检入口)。"""
    try:
        from realtime.position_tracker import SCHEDULER_ALERT_LOG
        with open(SCHEDULER_ALERT_LOG, 'a', encoding='utf-8') as f:
            f.write(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] {msg}\n")
    except OSError:
        pass
    # [Task#256 G2] P0级条目(🚨等特征)落文件同时实时推企微, 非P0静默跳过
    try:
        from realtime.notify import push_alert
        push_alert(msg)
    except Exception:
        pass


def check_hour_data_supply(conn, signal_date: str):
    """hour线供给体检(P0-2根因二防复发): 信号日hour1_close全NULL
    → 创科晚封(S3)晚封判定静默全灭, 必须显式告警而非静默零候选。
    (BaoStock封禁断供2026-07-27起曾静默持续4个信号日未被发现)
    """
    try:
        n = conn.execute(
            "SELECT COUNT(*) FROM stock_kline WHERE date=? "
            "AND hour1_close IS NOT NULL AND hour1_close > 0",
            (signal_date,)).fetchone()[0]
    except sqlite3.Error:
        return
    if n == 0:
        msg = (f"🚨 晚间候选生成: 信号日{signal_date} hour1_close全NULL"
               f"(hour线断供), 创科晚封(S3)晚封判定失效候选必为0, "
               f"需回补hour线(参Task#77 BaoStock恢复流水线)")
        print(f"  [WARN] {msg}")
        _alert(msg)
    else:
        print(f"  [hour供给体检] {signal_date} hour1_close有效{n}行 ✓")


# [Task#207] 数据完整性门: 8/6事故——22:00 cron触发时日K仅入库~60%
# (fetch_daily_kline因限速18:30启动约22:50才完成), 候选基于残缺数据生成。
# 门槛: 最新交易日日K入库<COMPLETENESS_MIN_ROWS → sleep重试, 仍不足带WARNING继续
# (宁可有候选也不能空窗, 但告警必须显眼进scheduler_alerts人工巡检)
COMPLETENESS_MIN_ROWS = 5000     # 全市场约5200只, 低于此值视为入库不完整
COMPLETENESS_RETRY_SLEEP = 300   # 每轮重试等待秒数
COMPLETENESS_MAX_RETRIES = 6     # 最多重试轮数(共30分钟)

USER_EXCLUDED_FILE = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), '..',
    'data', 'realtime', 'user_excluded.json')

# [Task#253] 公告预警文件(tools/announcement_monitor.py维护, 本处只读)
ANN_ALERTS_FILE = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), '..',
    'data', 'realtime', 'ann_alerts.json')


def wait_data_completeness(signal_date: str) -> bool:
    """[Task#207] 候选生成前校验信号日日K入库完整性。

    Returns:
        True=数据完整; False=重试耗尽仍不足(调用方带WARNING继续生成)
    """
    for attempt in range(COMPLETENESS_MAX_RETRIES + 1):
        try:
            conn = sqlite3.connect(DATA_DB)
            n = conn.execute(
                "SELECT COUNT(*) FROM stock_kline WHERE date=?",
                (signal_date,)).fetchone()[0]
            conn.close()
        except sqlite3.Error as e:
            print(f"  [WARN] 完整性门查询失败: {e}")
            return False
        if n >= COMPLETENESS_MIN_ROWS:
            print(f"  [完整性门] {signal_date} 日K入库{n}行 >= "
                  f"{COMPLETENESS_MIN_ROWS} ✓")
            return True
        if attempt < COMPLETENESS_MAX_RETRIES:
            msg = (f"⚠️ 完整性门: {signal_date} 日K仅入库{n}行 < "
                   f"{COMPLETENESS_MIN_ROWS}, 等待{COMPLETENESS_RETRY_SLEEP}s后"
                   f"重试({attempt + 1}/{COMPLETENESS_MAX_RETRIES})")
            print(f"  [WARN] {msg}")
            _alert(msg)
            time.sleep(COMPLETENESS_RETRY_SLEEP)
        else:
            msg = (f"🚨🚨🚨 完整性门: {signal_date} 日K入库{n}行 < "
                   f"{COMPLETENESS_MIN_ROWS}, 重试{COMPLETENESS_MAX_RETRIES}轮"
                   f"耗尽仍不足! 带WARNING标记继续生成候选(可能不完整), "
                   f"需人工立即核查fetch_daily_kline并重跑候选生成")
            print(f"  [WARNING] {msg}")
            _alert(msg)
    return False


def load_user_excluded(trade_date: str) -> list:
    """[Task#207] 读取用户手动排除名单(按目标交易日)。

    名单文件缺失/损坏时安全跳过过滤, 绝不炸主流程。
    """
    try:
        with open(USER_EXCLUDED_FILE, 'r', encoding='utf-8') as f:
            data = json.load(f)
        codes = data.get(trade_date, [])
        if isinstance(codes, list):
            return [c for c in codes if isinstance(c, str) and c]
        return []
    except Exception as e:
        print(f"  [用户排除] 名单文件读取跳过({e})")
        return []


def apply_user_exclusions(results: dict, trade_date: str):
    """[Task#207] 候选生成完成后、写文件前应用用户排除名单。"""
    excluded = load_user_excluded(trade_date)
    if not excluded:
        return
    excluded_set = set(excluded)
    for slot_id, slot_data in results.items():
        kept, removed = [], []
        for c in slot_data.get('candidates', []):
            if c.get('code') in excluded_set:
                removed.append(c)
            else:
                kept.append(c)
        if removed:
            slot_data['candidates'] = kept
            slot_data['user_excluded'] = [
                {'code': c['code'], 'name': c.get('name', '')}
                for c in removed]
            for c in removed:
                print(f"  [用户排除] {c['code']} {c.get('name', '')} "
                      f"(slot={slot_id}, 交易日{trade_date}名单命中)")


def annotate_ann_alerts(results: dict, trade_date: str):
    """[Task#253] 候选生成后按公告预警标注ann_alert字段(生成层第一层)。

    23:35扫描若已有该trade_date结果则标注; 文件缺失/损坏/无结果不阻塞,
    绝不炸主流程(排除动作在morning_decision第二层, 本处只标注展示)。
    """
    try:
        with open(ANN_ALERTS_FILE, 'r', encoding='utf-8') as f:
            alerts = json.load(f)
        day = alerts.get(trade_date) or []
        if not isinstance(day, list) or not day:
            return
        by_code = {}
        for h in day:
            if isinstance(h, dict) and h.get('code'):
                by_code.setdefault(h['code'], h)
        for slot_id, slot_data in results.items():
            for c in slot_data.get('candidates', []):
                h = by_code.get(c.get('code'))
                if h:
                    c['ann_alert'] = {'title': h.get('title', ''),
                                      'matched_kw': h.get('matched_kw', []),
                                      'url': h.get('url', '')}
                    print(f"  [公告预警标注] {c['code']} {c.get('name', '')} "
                          f"《{h.get('title', '')}》(slot={slot_id}, 暂不参与买入)")
    except Exception as e:
        print(f"  [公告预警标注] 读取跳过({e})")


def annotate_earnings_block(results: dict, trade_date: str):
    """[Task#297] 候选生成后按预约披露日历标注earnings_block字段(生成层第一层)。

    用户直接指令: 持股期间会触发业绩公告的不买, 避免碰雷。
    口径: 预约披露日∈(D0, D0+策略最长持有天数+1天缓冲] → 标注。
    本处只标注保留展示, 排除买入动作在morning_decision第三层过滤;
    日历缺失/损坏/过期(>3天)→fail-open不标注+告警, 绝不炸主流程。
    """
    from realtime.earnings_calendar import (load_earnings_calendar,
                                            check_earnings_window)
    by_code, err = load_earnings_calendar()
    if err:
        msg = (f"⚠️ 财报拦截标注: {err}, fail-open不标注任何候选"
               f"(交易日{trade_date}), 需核查tools/fetch_earnings_calendar.py")
        print(f"  [WARN] {msg}")
        _alert(msg)
        return
    for slot_id, slot_data in results.items():
        hold_hours = slot_data.get('max_hold_hours', 8)
        for c in slot_data.get('candidates', []):
            hit = check_earnings_window(c.get('code', ''), trade_date,
                                        hold_hours, by_code)
            if hit:
                c['earnings_block'] = True
                c['earnings_appoint_date'] = hit
                print(f"  [财报期拦截标注] {c['code']} {c.get('name', '')} "
                      f"预约披露{hit}在持有窗内(slot={slot_id}, 不参与买入)")


def annotate_frozen(results: dict, trade_date: str):
    """[Task#285] 候选生成后对冻结策略slot加frozen顶层标注(只标注不剔除)。

    S3候选照常生成写文件(假想跟踪数据源); 消费方(morning_decision/
    notify/前端)均.get读取, 无frozen字段时行为不变。参照annotate_ann_alerts
    "只标注不排除"模式, 买入跳过动作在morning_decision冻结分支。
    """
    for slot_id, slot_data in results.items():
        if slot_data.get('strategy_name') in STRATEGY_FROZEN:
            slot_data['frozen'] = True
            print(f"  [冻结标注] {slot_id} {slot_data.get('strategy_name')} "
                  f"候选{len(slot_data.get('candidates', []))}只照常生成, "
                  f"仅假想跟踪不买入(交易日{trade_date})")


def get_next_trade_date(conn, after_date: str) -> str:
    """获取指定日期之后的下一个交易日。"""
    cur = conn.execute(
        "SELECT DISTINCT date FROM stock_kline WHERE date > ? ORDER BY date ASC LIMIT 1",
        (after_date,))
    row = cur.fetchone()
    if row:
        return row[0]
    # DB没有下一日数据(实盘场景), 推算跳过周末
    dt = datetime.strptime(after_date, '%Y-%m-%d')
    for i in range(1, 8):
        next_dt = dt + timedelta(days=i)
        if next_dt.weekday() < 5:
            return next_dt.strftime('%Y-%m-%d')
    return ''


def get_latest_trade_date(conn) -> str:
    """获取DB中最新交易日。"""
    cur = conn.execute("SELECT DISTINCT date FROM stock_kline ORDER BY date DESC LIMIT 1")
    row = cur.fetchone()
    return row[0] if row else ''


# =============================================================================
# 主流程
# =============================================================================

def generate_all_candidates(signal_date: str, trade_date: str) -> dict:
    """对所有active策略生成候选股。

    Args:
        signal_date: 信号日(当日,数据已完整)
        trade_date: 候选股适用的交易日(次日)

    Returns:
        {slot_id: {strategy_name, candidates, buy_hour, tp_pct, sl_pct, ...}}
    """
    data_feed = RealtimeDataFeed(DATA_DB)
    data_feed.set_signal_mode(signal_date, trade_date)

    results = {}
    for cfg in ACTIVE_STRATEGIES:
        if not cfg['enabled']:
            continue

        slot_id = cfg['slot_id']
        try:
            strategy = load_strategy(cfg)
        except Exception as e:
            print(f"  [WARN] 加载策略 {cfg['class']} 失败: {e}")
            results[slot_id] = {
                'strategy_name': cfg['class'],
                'error': str(e),
                'candidates': [],
            }
            continue

        # 调用策略标准接口
        try:
            candidate_codes = strategy.get_candidates(trade_date, data_feed)
        except Exception as e:
            print(f"  [WARN] {strategy.name}.get_candidates 异常: {e}")
            candidate_codes = []
            # 静默失败告警(P0-2教训): 异常候选退化为空不能只留stdout,
            # 必须进scheduler_alerts人工巡检入口
            _alert(f"🚨 晚间候选生成: {slot_id} {strategy.name}"
                   f".get_candidates异常({e}), 候选退化为空, 需人工排查")

        # 收集候选股详情
        # Task#68: 策略可声明 candidate_top_n 覆盖全局截断 —— FB-A类策略
        # signal模式无法预筛次日开盘窗口, 形态池大且排序与次日开盘无关,
        # 截断会丢失9:25复筛宇宙(7/15实例: 引擎买入标的排第69位被截)
        top_n = getattr(strategy, 'candidate_top_n', None) or CANDIDATE_TOP_N
        candidates_detail = []
        if candidate_codes:
            # 从signal_date数据获取详细信息
            conn = sqlite3.connect(DATA_DB)
            for code in candidate_codes[:top_n]:
                detail = _get_candidate_detail(conn, code, signal_date, strategy)
                if detail:
                    candidates_detail.append(detail)
            conn.close()

        results[slot_id] = {
            'strategy_name': strategy.name,
            'strategy_class': cfg['class'],
            'module': cfg['module'],
            'description': cfg.get('description', ''),
            'buy_hour': getattr(strategy, 'buy_hour', 1),
            'tp_pct': getattr(strategy, 'take_profit_pct', 0.08),
            'sl_pct': getattr(strategy, 'stop_loss_pct', -0.15),
            'max_hold_hours': getattr(strategy, 'max_hold_hours', 8),
            'candidates': candidates_detail,
            'total_count': len(candidate_codes),
        }

        print(f"  {slot_id} ({strategy.name}): {len(candidate_codes)} 只候选")

    data_feed.close()
    return results


def _get_candidate_detail(conn, code: str, signal_date: str, strategy) -> dict:
    """从DB获取候选股的详细信息。"""
    cur = conn.execute("""
        SELECT code, code_name, open, high, low, close, preclose, turn, amount,
               close_rate, hour1_close
        FROM stock_kline WHERE date = ? AND code = ?
    """, (signal_date, code))
    row = cur.fetchone()
    if not row:
        return None

    code, name, open_p, high, low, close_p, preclose, turn, amount, close_rate, h1_close = row
    if not close_p or close_p <= 0:
        return None

    # 通用信息
    detail = {
        'code': code,
        'name': name or '',
        'signal_close': round(close_p, 2),
        'signal_preclose': round(preclose, 2) if preclose else 0,
        'signal_open': round(open_p, 2) if open_p else 0,
        'signal_high': round(high, 2) if high else 0,
        'signal_low': round(low, 2) if low else 0,
        'close_rate': round(close_rate, 2) if close_rate else 0,
        'turn': round(turn, 2) if turn else 0,
        'amount_yi': round((amount or 0) / 1e8, 2),
    }

    # 策略特定信息
    tp_pct = getattr(strategy, 'take_profit_pct', 0.08)
    sl_pct = getattr(strategy, 'stop_loss_pct', -0.15)
    detail['tp_pct'] = round(tp_pct * 100, 1)
    detail['sl_pct'] = round(sl_pct * 100, 1)

    # [Task#209] 连板高度标注(信号日为终点的连续涨停天数, 2板+高风险)
    detail['board_height'] = calc_board_height(conn, code, signal_date)

    return detail


# [Task#209] t208_s3_board_height口径: close∈[涨停价±0.011] 且volume>0
# (带上下限排除IPO前5日误判; volume=0停牌冻结行排除)
# [Task#299] 涨停价改trading_rules.limit_prices统一口径(原round自算)
_LIMIT_TOL = 0.011


def calc_board_height(conn, code: str, signal_date: str) -> int:
    """[Task#209] 连板高度: 以信号日为终点的连续涨停天数。

    ratio按trading_rules.limit_ratio(bj.30%/创科20%/ST5%/主板10%)。
    非涨停信号日返回0; 异常/无数据返回0不阻断候选生成。
    """
    try:
        rows = conn.execute(
            "SELECT close, preclose, volume, isST FROM stock_kline "
            "WHERE code=? AND date<=? ORDER BY date DESC LIMIT 15",
            (code, signal_date)).fetchall()
    except sqlite3.Error:
        return 0
    height = 0
    for close_p, preclose, volume, is_st in rows:
        if not close_p or not preclose or preclose <= 0:
            break
        if not volume or volume <= 0:
            break
        # [Task#299] 涨停价统一trading_rules.limit_prices(Decimal ROUND_HALF_UP
        # 交易所口径, 按板块/ST自动取ratio), 替换round自算旁路(t292审计P0)
        limit_px = limit_prices(code, preclose, is_st=bool(is_st))[0]
        if abs(close_p - limit_px) <= _LIMIT_TOL:
            height += 1
        else:
            break
    return height


# =============================================================================
# 输出
# =============================================================================

def save_candidates_json(results: dict, signal_date: str, trade_date: str,
                         data_complete: bool = True, promo_gate: dict = None):
    """保存候选股JSON文件。[Task#207] 原子写(tmp+rename)防半成品文件。

    promo_gate: [Task#320]晋级率门控顶层标注(仅信息性, 候选照常生成;
    9:25权威判定在morning_decision重算)。None=不写字段(旧格式兼容)。
    """
    os.makedirs(OUTPUT_DIR, exist_ok=True)

    output = {
        'signal_date': signal_date,
        'trade_date': trade_date,
        'generated_at': datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
        'framework_version': '2.0',  # 标识为新框架生成
        'strategies': results,
    }
    if not data_complete:
        # [Task#207] 完整性门未达标仍生成的候选: 文件内显式标记供下游/人工识别
        output['data_completeness_warning'] = (
            f'信号日{signal_date}日K入库不足{COMPLETENESS_MIN_ROWS}行, '
            f'候选可能不完整, 需数据补齐后重跑')
    if promo_gate is not None:
        # [Task#320] 晋级率门控标注(仅信息性字段, 不影响候选内容与下游解析)
        output['promo_gate'] = promo_gate

    # 向后兼容: 顶层保留strategy_name → candidates映射
    for slot_id, slot_data in results.items():
        strat_name = slot_data.get('strategy_name', '')
        if strat_name and slot_data.get('candidates'):
            output[strat_name] = slot_data['candidates']

    out_file = os.path.join(OUTPUT_DIR, f'candidates_{trade_date.replace("-", "")}.json')
    tmp_file = out_file + '.tmp'
    with open(tmp_file, 'w', encoding='utf-8') as f:
        json.dump(output, f, ensure_ascii=False, indent=2)
    os.replace(tmp_file, out_file)

    return out_file


def print_summary(results: dict):
    """打印摘要。"""
    for slot_id, slot_data in results.items():
        strat_name = slot_data.get('strategy_name', '?')
        candidates = slot_data.get('candidates', [])
        if not candidates:
            continue
        print(f"\n  --- {slot_id} ({strat_name}) TOP5 ---")
        for i, c in enumerate(candidates[:5], 1):
            print(f"  {i}. {c['code']} {c['name']} "
                  f"turn={c['turn']}% close={c['signal_close']} "
                  f"rate={c['close_rate']}%")


# =============================================================================
# Main
# =============================================================================

def main():
    parser = argparse.ArgumentParser(description='晚间候选股生成(策略无关框架)')
    parser.add_argument('--date', '-d', type=str, default=None,
                        help='信号日期(默认DB最新交易日)')
    # 兼容旧版直接传日期参数
    parser.add_argument('date_positional', nargs='?', default=None,
                        help=argparse.SUPPRESS)
    args = parser.parse_args()

    signal_date = args.date or args.date_positional

    os.makedirs(OUTPUT_DIR, exist_ok=True)
    os.makedirs(LOG_DIR, exist_ok=True)

    # 确定信号日
    conn = sqlite3.connect(DATA_DB)
    if not signal_date:
        signal_date = get_latest_trade_date(conn)
        if not signal_date:
            print("[ERROR] 数据库为空")
            conn.close()
            return 1

    # 确定候选股适用的交易日
    trade_date = get_next_trade_date(conn, signal_date)
    conn.close()

    print(f"[generate_candidates] 信号日: {signal_date}")
    print(f"[generate_candidates] 候选股适用交易日: {trade_date}")
    print(f"[generate_candidates] 活跃策略: {sum(1 for s in ACTIVE_STRATEGIES if s['enabled'])} 个")

    # [Task#207] 数据完整性门: 信号日日K入库不足则等待重试, 耗尽带WARNING继续
    data_complete = wait_data_completeness(signal_date)

    # hour线供给体检(P0-2防复发, 断供只告警不阻断其余策略候选生成)
    conn2 = sqlite3.connect(DATA_DB)
    check_hour_data_supply(conn2, signal_date)
    conn2.close()

    # 生成候选
    results = generate_all_candidates(signal_date, trade_date)

    # [Task#207] 写文件前应用用户手动排除名单(缺失/损坏安全跳过)
    try:
        apply_user_exclusions(results, trade_date)
    except Exception as e:
        print(f"  [WARN] 用户排除应用异常(跳过不阻断): {e}")

    # [Task#253] 公告预警标注(缺失/损坏/无结果安全跳过, 只标注不剔除)
    try:
        annotate_ann_alerts(results, trade_date)
    except Exception as e:
        print(f"  [WARN] 公告预警标注异常(跳过不阻断): {e}")

    # [Task#297] 预约披露日标注(日历缺失/过期fail-open, 只标注不剔除)
    try:
        annotate_earnings_block(results, trade_date)
    except Exception as e:
        print(f"  [WARN] 财报拦截标注异常(跳过不阻断): {e}")

    # [Task#285] 冻结策略slot标注(只标注不剔除, 异常安全跳过)
    try:
        annotate_frozen(results, trade_date)
    except Exception as e:
        print(f"  [WARN] 冻结标注异常(跳过不阻断): {e}")

    # [Task#320] 晋级率门控顶层标注(候选照常生成, 仅加标注字段;
    # promo_rate(信号日)=明日9:25门控的"昨日晋级率", 权威判定在
    # morning_decision重算; 异常安全跳过不标注)
    promo_gate_ann = None
    try:
        from realtime.promo_gate import compute_promo_rate
        _pg_conn = sqlite3.connect(f'file:{DATA_DB}?mode=ro', uri=True)
        _r = compute_promo_rate(_pg_conn, signal_date)
        _pg_conn.close()
        if _r is not None:
            _rate = _r['promo_rate']
            promo_gate_ann = {
                'signal_date_promo_rate': (round(_rate, 6)
                                           if _rate is not None else None),
                'fb_count': _r['fb_count'], 'promoted': _r['promoted'],
                'threshold': PROMO_GATE_THRESHOLD,
                'enabled': PROMO_GATE_ENABLED,
                'would_trigger': bool(PROMO_GATE_ENABLED
                                      and _rate is not None
                                      and _rate >= PROMO_GATE_THRESHOLD),
                'note': '信息性标注; 9:25 morning_decision重算为准',
            }
            if promo_gate_ann['would_trigger']:
                print(f"  [晋级率标注] 信号日{signal_date}晋级率"
                      f"{_rate * 100:.2f}%≥{PROMO_GATE_THRESHOLD * 100:.0f}%"
                      f" → 明日预计过热门控生效(以9:25重算为准)")
            else:
                _rs = (f"{_rate * 100:.2f}%" if _rate is not None
                       else '不适用(前日无首板)')
                print(f"  [晋级率标注] 信号日{signal_date}晋级率{_rs}, 正常")
    except Exception as e:
        print(f"  [WARN] 晋级率标注异常(跳过不阻断): {e}")

    # 保存JSON
    out_file = save_candidates_json(results, signal_date, trade_date,
                                    data_complete=data_complete,
                                    promo_gate=promo_gate_ann)
    print(f"\n[OK] 候选股已写入: {out_file}")

    # [Task#209] 候选通知(含连板高度标注), 失败不阻断主流程
    try:
        from realtime.notify import notify_candidates
        notify_candidates(trade_date, results)
    except Exception as e:
        print(f"  [WARN] 候选通知发送异常(跳过不阻断): {e}")

    # 打印摘要
    print_summary(results)

    return 0


if __name__ == '__main__':
    sys.exit(main())
