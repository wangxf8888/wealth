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
import importlib
import argparse
import sqlite3
from datetime import datetime, timedelta

# 确保项目根目录在path中
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT_ROOT)

from realtime.config import ACTIVE_STRATEGIES, DATA_DB, OUTPUT_DIR, LOG_DIR, CANDIDATE_TOP_N
from realtime.data_feed import RealtimeDataFeed


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

        # 收集候选股详情
        candidates_detail = []
        if candidate_codes:
            # 从signal_date数据获取详细信息
            conn = sqlite3.connect(DATA_DB)
            for code in candidate_codes[:CANDIDATE_TOP_N]:
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

    return detail


# =============================================================================
# 输出
# =============================================================================

def save_candidates_json(results: dict, signal_date: str, trade_date: str):
    """保存候选股JSON文件。"""
    os.makedirs(OUTPUT_DIR, exist_ok=True)

    output = {
        'signal_date': signal_date,
        'trade_date': trade_date,
        'generated_at': datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
        'framework_version': '2.0',  # 标识为新框架生成
        'strategies': results,
    }

    # 向后兼容: 顶层保留strategy_name → candidates映射
    for slot_id, slot_data in results.items():
        strat_name = slot_data.get('strategy_name', '')
        if strat_name and slot_data.get('candidates'):
            output[strat_name] = slot_data['candidates']

    out_file = os.path.join(OUTPUT_DIR, f'candidates_{trade_date.replace("-", "")}.json')
    with open(out_file, 'w', encoding='utf-8') as f:
        json.dump(output, f, ensure_ascii=False, indent=2)

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

    # 生成候选
    results = generate_all_candidates(signal_date, trade_date)

    # 保存JSON
    out_file = save_candidates_json(results, signal_date, trade_date)
    print(f"\n[OK] 候选股已写入: {out_file}")

    # 打印摘要
    print_summary(results)

    return 0


if __name__ == '__main__':
    sys.exit(main())
