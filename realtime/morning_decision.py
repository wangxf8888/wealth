#!/usr/bin/env python3
"""
早盘决策 (每个交易日9:25触发)
==================================
策略无关框架 - 读取晚间候选，获取实时开盘价，调用各策略标准接口决定买入。

**动态再平衡模式**:
- 买入金额 = total_nav / 5 (与回测引擎 portfolio.py 一致)
- total_nav = 现金 + 所有持仓市值
- 不是让每个slot独立运行

新增策略 = 修改config.py + 放策略文件到strategies/ → 无需改此文件。

用法:
  实盘:  python realtime/morning_decision.py
  测试:  python realtime/morning_decision.py --test-date 2026-07-18
"""
import sys
import os
import json
import importlib
import argparse
import sqlite3
import urllib.request
import re
from datetime import datetime, timedelta

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT_ROOT)

from realtime.config import (ACTIVE_STRATEGIES, DATA_DB, OUTPUT_DIR, LOG_DIR,
                             TOTAL_CAPITAL, MAX_SLOT_PER_STRATEGY,
                             MARKET_FILTER_THRESHOLD)
from realtime.data_feed import RealtimeDataFeed
from realtime.position_tracker import load_positions, get_buy_amount


# =============================================================================
# 策略动态加载
# =============================================================================

def load_strategy(cfg: dict):
    """动态import策略类并实例化。"""
    module = importlib.import_module(cfg['module'])
    cls = getattr(module, cfg['class'])
    return cls()


def get_strategy_config_by_slot(slot_id: str) -> dict:
    """通过slot_id查找策略配置。"""
    for cfg in ACTIVE_STRATEGIES:
        if cfg['slot_id'] == slot_id and cfg['enabled']:
            return cfg
    return None


def get_strategy_config_by_name(name: str) -> dict:
    """通过strategy_name查找策略配置。"""
    for cfg in ACTIVE_STRATEGIES:
        if not cfg['enabled']:
            continue
        try:
            mod = importlib.import_module(cfg['module'])
            cls = getattr(mod, cfg['class'])
            instance = cls()
            if instance.name == name:
                return cfg
        except Exception:
            pass
    return None


# =============================================================================
# 实时开盘价获取
# =============================================================================

def code_to_sina(code: str) -> str:
    """sh.600000 → sh600000"""
    return code.replace('.', '')


def sina_to_code(sina_code: str) -> str:
    """sh600000 → sh.600000"""
    return sina_code[:2] + '.' + sina_code[2:]


def _fetch_from_tencent(codes: list) -> dict:
    """通过腾讯实时行情接口获取开盘价。"""
    results = {}
    tencent_codes = [code_to_sina(c) for c in codes]
    batch_size = 50

    for i in range(0, len(tencent_codes), batch_size):
        batch = tencent_codes[i:i + batch_size]
        url = f"http://qt.gtimg.cn/q={','.join(batch)}"
        try:
            req = urllib.request.Request(url)
            req.add_header('User-Agent',
                           'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36')
            resp = urllib.request.urlopen(req, timeout=10)
            content = resp.read().decode('gbk')
            for line in content.strip().split('\n'):
                line = line.strip()
                if not line:
                    continue
                m = re.match(r'v_(\w+)="(.*)"', line)
                if not m:
                    continue
                tencent_sym = m.group(1)
                data_str = m.group(2)
                if not data_str:
                    continue
                parts = data_str.split('~')
                if len(parts) < 6:
                    continue
                code = sina_to_code(tencent_sym)
                try:
                    open_price = float(parts[5]) if parts[5] else 0
                except (ValueError, IndexError):
                    open_price = 0
                if open_price > 0:
                    results[code] = open_price
        except Exception as e:
            print(f"  [WARN] 腾讯接口请求失败: {e}")
    return results


def _fetch_from_sina(codes: list) -> dict:
    """通过Sina实时行情接口获取开盘价(备用)。"""
    results = {}
    sina_codes = [code_to_sina(c) for c in codes]
    batch_size = 50

    for i in range(0, len(sina_codes), batch_size):
        batch = sina_codes[i:i + batch_size]
        url = f"http://hq.sinajs.cn/list={','.join(batch)}"
        try:
            req = urllib.request.Request(url)
            req.add_header('Referer', 'http://finance.sina.com.cn')
            req.add_header('User-Agent',
                           'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36')
            resp = urllib.request.urlopen(req, timeout=10)
            content = resp.read().decode('gbk')
            for line in content.strip().split('\n'):
                m = re.match(r'var hq_str_(\w+)="(.*)";', line)
                if not m:
                    continue
                sina_sym = m.group(1)
                fields = m.group(2).split(',')
                if len(fields) < 4:
                    continue
                code = sina_to_code(sina_sym)
                open_price = float(fields[1]) if fields[1] else 0
                if open_price > 0:
                    results[code] = open_price
        except Exception as e:
            print(f"  [WARN] Sina接口请求失败: {e}")
    return results


def fetch_realtime_open_prices(codes: list) -> dict:
    """获取实时开盘价 (腾讯优先，Sina备用)。"""
    if not codes:
        return {}
    print(f"  尝试腾讯接口...")
    results = _fetch_from_tencent(codes)
    if results:
        print(f"  腾讯接口成功获取 {len(results)} 只")
        return results
    print(f"  腾讯接口未获取数据，回退Sina接口...")
    results = _fetch_from_sina(codes)
    if results:
        print(f"  Sina接口成功获取 {len(results)} 只")
    return results


def fetch_test_open_prices(conn, codes: list, test_date: str) -> dict:
    """从数据库获取指定日期的开盘价(测试模式)。"""
    if not codes:
        return {}
    placeholders = ','.join(['?' for _ in codes])
    cur = conn.execute(f"""
        SELECT code, open FROM stock_kline
        WHERE date = ? AND code IN ({placeholders})
    """, [test_date] + codes)
    results = {}
    for row in cur.fetchall():
        code, open_p = row
        if open_p and open_p > 0:
            results[code] = open_p
    return results


# =============================================================================
# 大盘过滤
# =============================================================================

def check_market_filter(conn, trade_date: str) -> bool:
    """前日上证指数大跌则今日不开新仓。返回True=应跳过。

    防护措施:
    - 如果index_kline数据陈旧(距trade_date超过10个自然日)，视为数据缺失，不过滤
    - 如果stock_kline中有上证数据(preclose/close)，作为备用数据源
    """
    # 主路径: 从index_kline获取
    prev_date, close_rate = None, None
    cur = conn.execute("""
        SELECT date, close_rate FROM index_kline
        WHERE code='sh.000001' AND date < ?
        ORDER BY date DESC LIMIT 1
    """, (trade_date,))
    row = cur.fetchone()
    if row:
        prev_date, close_rate = row

    # 备用路径: 从stock_kline获取上证(如果有)
    if prev_date is None or close_rate is None:
        cur2 = conn.execute("""
            SELECT date, close, preclose FROM stock_kline
            WHERE code='sh.000001' AND date < ?
            ORDER BY date DESC LIMIT 1
        """, (trade_date,))
        row2 = cur2.fetchone()
        if row2 and row2[1] and row2[2] and row2[2] > 0:
            prev_date = row2[0]
            close_rate = (row2[1] / row2[2] - 1) * 100

    # 无数据 -> 不过滤
    if prev_date is None or close_rate is None:
        print(f"  [大盘过滤] 未找到上证指数数据，跳过过滤")
        return False

    # 新鲜度校验: 数据距trade_date超过10个自然日视为陈旧
    try:
        trade_dt = datetime.strptime(trade_date, '%Y-%m-%d')
        prev_dt = datetime.strptime(prev_date, '%Y-%m-%d')
        days_gap = (trade_dt - prev_dt).days
        if days_gap > 10:
            print(f"  [大盘过滤] 指数数据陈旧(最新{prev_date}，距今{days_gap}天)，跳过过滤")
            return False
    except ValueError:
        print(f"  [大盘过滤] 日期解析异常({prev_date})，跳过过滤")
        return False

    triggered = close_rate < MARKET_FILTER_THRESHOLD
    if triggered:
        print(f"  [大盘过滤] {prev_date} 上证跌{close_rate:.2f}% > 阈值{abs(MARKET_FILTER_THRESHOLD):.1f}%")
        print(f"  ★★★ 大盘暴跌过滤，今日不开新仓 ★★★")
    else:
        print(f"  [大盘过滤] {prev_date} 上证{close_rate:+.2f}%，正常开仓")
    return triggered


# =============================================================================
# 核心决策逻辑
# =============================================================================

def morning_evaluate(trade_date: str, open_prices: dict, candidates_data: dict) -> dict:
    """对所有策略slots执行买入决策。

    Args:
        trade_date: 当日交易日
        open_prices: {code: open_price} 实时/测试开盘价
        candidates_data: 晚间生成的候选股JSON中的strategies字段

    Returns:
        {slot_id: {strategy_name, recommendation, meet_condition, ...}}
    """
    # 创建带实时开盘价的data_feed
    data_feed = RealtimeDataFeed(DATA_DB)
    data_feed.set_live_mode(trade_date, open_prices)

    decisions = {}

    for slot_id, slot_data in candidates_data.items():
        strategy_name = slot_data.get('strategy_name', '')
        strategy_class = slot_data.get('strategy_class', '')
        candidates = slot_data.get('candidates', [])

        if not candidates:
            decisions[slot_id] = {
                'strategy': strategy_name,
                'total_candidates': 0,
                'with_open_price': 0,
                'meet_condition': [],
                'recommendations': [],
            }
            continue

        # 加载策略实例
        cfg = get_strategy_config_by_slot(slot_id)
        if not cfg:
            decisions[slot_id] = {
                'strategy': strategy_name,
                'total_candidates': len(candidates),
                'with_open_price': 0,
                'meet_condition': [],
                'recommendations': [],
                'error': f'slot {slot_id} 未配置',
            }
            continue

        try:
            strategy = load_strategy(cfg)
        except Exception as e:
            decisions[slot_id] = {
                'strategy': strategy_name,
                'total_candidates': len(candidates),
                'error': str(e),
                'meet_condition': [],
                'recommendations': [],
            }
            continue

        # 使用策略的get_candidates在live模式下重新筛选(精确过滤)
        try:
            filtered_codes = strategy.get_candidates(trade_date, data_feed)
        except Exception as e:
            print(f"  [WARN] {strategy.name}.get_candidates(live) 异常: {e}")
            filtered_codes = []

        # 收集满足条件的候选
        candidate_codes_set = set(c['code'] for c in candidates)
        meet_condition = []
        with_open_price = 0

        for c in candidates:
            code = c['code']
            if code not in open_prices:
                continue
            with_open_price += 1

            if code not in filtered_codes:
                continue

            open_p = open_prices[code]
            signal_close = c.get('signal_close', 0)
            preclose = c.get('signal_close', open_p)  # trade_date preclose ≈ signal_date close
            open_rate = (open_p / preclose - 1) * 100 if preclose > 0 else 0

            tp_pct = getattr(strategy, 'take_profit_pct', 0.08)
            sl_pct = getattr(strategy, 'stop_loss_pct', -0.15)
            tp_price = round(open_p * (1 + tp_pct), 2)
            sl_price = round(open_p * (1 + sl_pct), 2)

            meet_condition.append({
                'code': code,
                'name': c.get('name', ''),
                'open': round(open_p, 2),
                'signal_close': signal_close,
                'open_rate_pct': round(open_rate, 2),
                'turn': c.get('turn', 0),
                'buy_price': round(open_p, 2),
                'tp_price': tp_price,
                'sl_price': sl_price,
            })

        # 排序取top (保持原策略排序 - filtered_codes已排好序)
        code_rank = {c: i for i, c in enumerate(filtered_codes)}
        meet_condition.sort(key=lambda x: code_rank.get(x['code'], 9999))
        recommendations = meet_condition[:MAX_SLOT_PER_STRATEGY]

        decisions[slot_id] = {
            'strategy': strategy_name,
            'total_candidates': len(candidates),
            'with_open_price': with_open_price,
            'meet_condition': meet_condition,
            'recommendations': recommendations,
        }

    data_feed.close()
    return decisions


# =============================================================================
# 输出格式化
# =============================================================================

def print_decision(trade_date: str, signal_date: str, decisions: dict):
    """格式化输出交易建议。"""
    n_strategies = len(decisions)

    # 获取当前账户状态和动态再平衡金额
    pos_data = load_positions()
    account = pos_data.get('account', {})
    buy_amount_per_slot = get_buy_amount(pos_data)

    print(f"\n{'=' * 60}")
    print(f"  {trade_date} {n_strategies}策略交易建议 (信号日: {signal_date})")
    print(f"  动态再平衡: 每slot买入 ¥{buy_amount_per_slot:,.0f} "
          f"(总净值 ¥{account.get('total_nav', TOTAL_CAPITAL):,.0f} / 5)")
    print(f"{'=' * 60}")

    for slot_id in sorted(decisions.keys()):
        res = decisions[slot_id]
        strat_name = res.get('strategy', '?')
        print(f"\n[{slot_id} {strat_name}]")
        print(f"  候选: {res['total_candidates']}只 | "
              f"获取开盘价: {res['with_open_price']}只 | "
              f"满足条件: {len(res['meet_condition'])}只")

        if res.get('error'):
            print(f"  [ERROR] {res['error']}")
            continue

        recs = res.get('recommendations', [])
        if recs:
            for i, rec in enumerate(recs, 1):
                print(f"  ★ 建议{i}: {rec['code']} {rec['name']} "
                      f"@{rec['buy_price']:.2f} (开盘{rec['open_rate_pct']:+.2f}%) "
                      f"turn={rec['turn']}%")
                print(f"       止盈={rec['tp_price']:.2f} | 止损={rec['sl_price']:.2f}")
        else:
            print(f"  ★ 建议: 无 (无满足条件标的)")

        # 备选
        if len(res['meet_condition']) > MAX_SLOT_PER_STRATEGY:
            print(f"  备选:")
            for c in res['meet_condition'][MAX_SLOT_PER_STRATEGY:4]:
                print(f"    - {c['code']} {c['name']} @{c['open']:.2f} "
                      f"({c['open_rate_pct']:+.2f}%) turn={c['turn']}%")

    print(f"\n{'=' * 60}")


# =============================================================================
# Pending Buy 生成
# =============================================================================

def generate_pending_buys(trade_date: str, decisions: dict):
    """生成待确认买入记录(供position_tracker使用)。"""
    pending = []
    buy_date = trade_date
    expire_dt = datetime.strptime(buy_date, '%Y-%m-%d') + timedelta(days=4)
    expire_date = expire_dt.strftime('%Y-%m-%d')

    for slot_id, res in sorted(decisions.items()):
        recs = res.get('recommendations', [])
        if not recs:
            continue
        rec = recs[0]  # 每slot取top1执行
        pending.append({
            'code': rec['code'],
            'name': rec['name'],
            'strategy': res['strategy'],
            'slot_id': slot_id,
            'buy_date': buy_date,
            'buy_price': rec['buy_price'],
            'tp_price': rec['tp_price'],
            'sl_price': rec['sl_price'],
            'expire_date': expire_date,
        })

    if pending:
        pending_file = os.path.join(OUTPUT_DIR, 'pending_buys.json')
        with open(pending_file, 'w', encoding='utf-8') as f:
            json.dump(pending, f, ensure_ascii=False, indent=2)
        print(f"\n[INFO] 已生成 {len(pending)} 笔待确认买入 → {pending_file}")
        for p in pending:
            print(f"  [{p['slot_id']}:{p['strategy']}] {p['code']} {p['name']} "
                  f"@{p['buy_price']:.2f} TP={p['tp_price']:.2f} SL={p['sl_price']:.2f}")


# =============================================================================
# Main
# =============================================================================

def main():
    parser = argparse.ArgumentParser(description='早盘决策(策略无关框架)')
    parser.add_argument('--test-date', type=str, default=None,
                        help='测试模式:从DB读取历史开盘价')
    args = parser.parse_args()

    test_date = args.test_date
    os.makedirs(LOG_DIR, exist_ok=True)
    os.makedirs(OUTPUT_DIR, exist_ok=True)

    conn = sqlite3.connect(DATA_DB)

    # 确定交易日
    if test_date:
        trade_date = test_date
        print(f"[morning_decision] 测试模式, 交易日: {trade_date}")
    else:
        trade_date = datetime.now().strftime('%Y-%m-%d')
        print(f"[morning_decision] 实盘模式, 交易日: {trade_date}")

    # 查找候选股文件
    cand_file = os.path.join(OUTPUT_DIR, f'candidates_{trade_date.replace("-", "")}.json')
    if not os.path.exists(cand_file):
        print(f"[ERROR] 候选股文件不存在: {cand_file}")
        print(f"  请先运行 generate_candidates.py 生成候选股")
        conn.close()
        return 1

    # 加载候选股
    with open(cand_file, 'r', encoding='utf-8') as f:
        candidates_json = json.load(f)

    signal_date = candidates_json.get('signal_date', '?')
    strategies_data = candidates_json.get('strategies', {})

    if not strategies_data:
        print("[ERROR] 候选股文件中无strategies数据(可能是旧版格式)")
        conn.close()
        return 1

    # 统计候选
    total_cands = sum(len(s.get('candidates', [])) for s in strategies_data.values())
    print(f"  信号日: {signal_date} | 总候选: {total_cands}只 | "
          f"策略slots: {len(strategies_data)}个")

    # 收集所有需要开盘价的代码
    all_codes = list(set(
        c['code']
        for s in strategies_data.values()
        for c in s.get('candidates', [])
    ))

    # 获取开盘价
    if test_date:
        print(f"  使用DB中 {trade_date} 的开盘价...")
        open_prices = fetch_test_open_prices(conn, all_codes, trade_date)
    else:
        print(f"  获取实时开盘价...")
        open_prices = fetch_realtime_open_prices(all_codes)

    print(f"  获取到 {len(open_prices)}/{len(all_codes)} 只开盘价")

    # 大盘过滤
    market_filtered = check_market_filter(conn, trade_date)
    if market_filtered:
        decision = {
            'trade_date': trade_date,
            'signal_date': signal_date,
            'decided_at': datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
            'market_filter': '触发-前日大盘跌>阈值,今日不开新仓',
            'strategies': {},
        }
        out_file = os.path.join(OUTPUT_DIR, f'decision_{trade_date.replace("-", "")}.json')
        with open(out_file, 'w', encoding='utf-8') as f:
            json.dump(decision, f, ensure_ascii=False, indent=2)
        print(f"\n[OK] 决策已保存(大盘过滤): {out_file}")
        conn.close()
        return 0

    # 执行决策
    decisions = morning_evaluate(trade_date, open_prices, strategies_data)

    # 输出
    print_decision(trade_date, signal_date, decisions)

    # 保存决策JSON
    decision_output = {
        'trade_date': trade_date,
        'signal_date': signal_date,
        'decided_at': datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
        'framework_version': '2.0',
        'strategies': decisions,
    }
    out_file = os.path.join(OUTPUT_DIR, f'decision_{trade_date.replace("-", "")}.json')
    with open(out_file, 'w', encoding='utf-8') as f:
        json.dump(decision_output, f, ensure_ascii=False, indent=2)
    print(f"\n[OK] 决策结果已保存: {out_file}")

    # 生成pending_buy
    generate_pending_buys(trade_date, decisions)

    conn.close()
    return 0


if __name__ == '__main__':
    sys.exit(main())
