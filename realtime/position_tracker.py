#!/usr/bin/env python3
"""
持仓跟踪与卖出监控 (盘中每小时/收盘后触发)
=============================================
策略无关框架 - 对所有持仓调用对应策略的should_sell接口。

**动态再平衡模式**:
- 每次卖出后更新 account.total_nav
- 下次买入金额 = total_nav / 5 (不是独立slot资金)
- 与回测引擎 portfolio.py 的逻辑完全一致 (buy_amount = get_nav() / n_slots)

功能:
1. 维护持仓JSON (/home/AIWealth/data/realtime/positions.json)
2. 盘中每小时检查止盈/止损/到期
3. 确认买入(pending → holding)
4. 确认卖出(holding → closed)
5. 列出持仓/历史
`
命令:
  python realtime/position_tracker.py check [--test-date YYYY-MM-DD] [--hour N]
  python realtime/position_tracker.py confirm-buy
  python realtime/position_tracker.py confirm-sell CODE PRICE
  python realtime/position_tracker.py add CODE NAME STRATEGY SLOT_ID BUY_PRICE TP SL EXPIRE
  python realtime/position_tracker.py list
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

from realtime.config import ACTIVE_STRATEGIES, DATA_DB, OUTPUT_DIR

POSITIONS_FILE = os.path.join(OUTPUT_DIR, 'positions.json')
PENDING_FILE = os.path.join(OUTPUT_DIR, 'pending_buys.json')


# =============================================================================
# 策略加载
# =============================================================================

def load_strategy_by_name(strategy_name: str):
    """通过策略name查找配置并加载。"""
    for cfg in ACTIVE_STRATEGIES:
        if not cfg['enabled']:
            continue
        try:
            mod = importlib.import_module(cfg['module'])
            cls = getattr(mod, cfg['class'])
            instance = cls()
            if instance.name == strategy_name:
                return instance
        except Exception:
            continue
    return None


def load_strategy_by_slot(slot_id: str):
    """通过slot_id加载策略。"""
    for cfg in ACTIVE_STRATEGIES:
        if cfg['slot_id'] == slot_id and cfg['enabled']:
            try:
                mod = importlib.import_module(cfg['module'])
                cls = getattr(mod, cfg['class'])
                return cls()
            except Exception:
                return None
    return None


# =============================================================================
# 持仓文件操作
# =============================================================================

# 默认账户结构
DEFAULT_ACCOUNT = {
    'initial_capital': 1000000,
    'cash': 1000000,
    'realized_pnl': 0.0,
    'total_nav': 1000000,
    'note': '实盘采用动态再平衡模式: 每slot买入金额 = total_nav / 5'
}


def load_positions() -> dict:
    """加载持仓文件。兼容新旧格式。

    新格式: {account: {...}, positions: [...], closed_trades: [...]}
    旧格式: {positions: [...]}  (closed记录混在positions里)
    """
    if not os.path.exists(POSITIONS_FILE):
        return {
            'account': DEFAULT_ACCOUNT.copy(),
            'positions': [],
            'closed_trades': []
        }
    with open(POSITIONS_FILE, 'r', encoding='utf-8') as f:
        data = json.load(f)

    # 确保有account字段
    if 'account' not in data:
        data['account'] = DEFAULT_ACCOUNT.copy()
    # 确保有closed_trades字段
    if 'closed_trades' not in data:
        data['closed_trades'] = []
    return data


def save_positions(data: dict):
    """保存持仓文件。"""
    os.makedirs(os.path.dirname(POSITIONS_FILE), exist_ok=True)
    # 更新total_nav
    _update_account_nav(data)
    with open(POSITIONS_FILE, 'w', encoding='utf-8') as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def _update_account_nav(data: dict):
    """更新 account.total_nav = cash + 持仓市值(以买入价估算)。"""
    account = data.get('account', DEFAULT_ACCOUNT.copy())
    positions = data.get('positions', [])
    holdings = [p for p in positions if p.get('status') == 'holding']

    # 持仓市值: 用buy_price * buy_amount/buy_price = buy_amount (无实时价时)
    holding_value = sum(p.get('buy_amount', account['initial_capital'] / 5) for p in holdings)
    account['total_nav'] = account['cash'] + holding_value
    data['account'] = account


def get_buy_amount(data: dict) -> float:
    """动态再平衡: 每个slot的买入金额 = total_nav / 5。

    与回测引擎 portfolio.py 的逻辑一致:
      target_amount = self.get_nav() / self.n_slots
    """
    account = data.get('account', DEFAULT_ACCOUNT.copy())
    positions = data.get('positions', [])
    holdings = [p for p in positions if p.get('status') == 'holding']

    # total_nav = cash + 持仓市值
    holding_value = sum(p.get('buy_amount', account['initial_capital'] / 5) for p in holdings)
    total_nav = account['cash'] + holding_value

    buy_amount = total_nav / 5
    # 不能超过可用现金
    buy_amount = min(buy_amount, account['cash'])
    return buy_amount


def load_pending() -> list:
    """加载待确认买入。"""
    if not os.path.exists(PENDING_FILE):
        return []
    with open(PENDING_FILE, 'r', encoding='utf-8') as f:
        return json.load(f)


def save_pending(data: list):
    """保存待确认买入。"""
    os.makedirs(os.path.dirname(PENDING_FILE), exist_ok=True)
    with open(PENDING_FILE, 'w', encoding='utf-8') as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


# =============================================================================
# 实时行情
# =============================================================================

def code_to_sina(code: str) -> str:
    return code.replace('.', '')


def sina_to_code(sina_code: str) -> str:
    return sina_code[:2] + '.' + sina_code[2:]


def fetch_realtime_prices(codes: list) -> dict:
    """通过腾讯/Sina实时行情获取当前价格。"""
    if not codes:
        return {}
    tencent_codes = [code_to_sina(c) for c in codes]
    results = {}
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
                if len(parts) < 4:
                    continue
                code = sina_to_code(tencent_sym)
                try:
                    # parts[3]: 当前价(current price)
                    current = float(parts[3]) if parts[3] else 0
                except (ValueError, IndexError):
                    current = 0
                if current > 0:
                    results[code] = current
        except Exception as e:
            print(f"  [WARN] 腾讯接口请求失败: {e}")

    return results


def fetch_test_prices(codes: list, test_date: str, hour: int = None) -> dict:
    """从数据库获取测试价格。hour指定则取hour_close, 否则取日close。"""
    if not codes:
        return {}
    conn = sqlite3.connect(DATA_DB)
    placeholders = ','.join(['?' for _ in codes])
    if hour and hour in (1, 2, 3, 4):
        col = f'hour{hour}_close'
        cur = conn.execute(f"""
            SELECT code, {col} FROM stock_kline
            WHERE date = ? AND code IN ({placeholders})
        """, [test_date] + codes)
    else:
        cur = conn.execute(f"""
            SELECT code, close FROM stock_kline
            WHERE date = ? AND code IN ({placeholders})
        """, [test_date] + codes)
    results = {}
    for row in cur.fetchall():
        code, price = row
        if price and price > 0:
            results[code] = price
    conn.close()
    return results


# =============================================================================
# 核心: 检查持仓
# =============================================================================

def check_positions(test_date: str = None, hour: int = None, auto_close: bool = False):
    """检查所有持仓, 输出止盈/止损/到期提醒。auto_close=True时自动平仓。"""
    data = load_positions()
    holdings = [p for p in data['positions'] if p['status'] == 'holding']

    if not holdings:
        print("\n[持仓跟踪] 当前无持仓")
        return

    today = test_date or datetime.now().strftime('%Y-%m-%d')
    current_hour = hour or _guess_current_hour()
    print(f"\n{'=' * 60}")
    print(f"  持仓跟踪 ({today} Hour={current_hour}) {'[AUTO-CLOSE]' if auto_close else ''}")
    print(f"{'=' * 60}")

    # 获取当前价格
    codes = [p['code'] for p in holdings]
    if test_date:
        prices = fetch_test_prices(codes, test_date, hour)
    else:
        prices = fetch_realtime_prices(codes)

    actions = []
    for pos in holdings:
        code = pos['code']
        name = pos['name']
        buy_price = pos['buy_price']
        tp_price = pos['tp_price']
        sl_price = pos['sl_price']
        expire_date = pos['expire_date']
        strategy_name = pos.get('strategy', '')

        if code not in prices:
            # 对于过期仓位，尝试用DB回溯查找卖出价
            if today >= expire_date and auto_close:
                sell_price = _find_historical_sell_price(pos, today)
                if sell_price:
                    prices[code] = sell_price
                else:
                    print(f"\n  {code} {name} [{strategy_name}]: 无行情且无法回溯")
                    continue
            else:
                print(f"\n  {code} {name} [{strategy_name}]: 未获取到行情")
                continue

        current_price = prices[code]
        pnl_pct = (current_price / buy_price - 1) * 100

        # 判断动作
        action = None
        if current_price >= tp_price:
            action = 'take_profit'
            print(f"\n  ★ 止盈: {code} {name} [{strategy_name}] "
                  f"当前{current_price:.2f} >= TP{tp_price:.2f} 盈利+{pnl_pct:.1f}%")
        elif current_price <= sl_price:
            action = 'stop_loss'
            print(f"\n  ★ 止损: {code} {name} [{strategy_name}] "
                  f"当前{current_price:.2f} <= SL{sl_price:.2f} 亏损{pnl_pct:.1f}%")
        elif today >= expire_date:
            action = 'expired'
            print(f"\n  ★ 到期: {code} {name} [{strategy_name}] "
                  f"到期{expire_date} 浮盈{pnl_pct:+.1f}%")
        else:
            print(f"\n  持仓: {code} {name} [{strategy_name}] "
                  f"浮盈{pnl_pct:+.1f}% | 当前{current_price:.2f} "
                  f"TP:{tp_price:.2f} SL:{sl_price:.2f} 到期:{expire_date}")

        if action:
            actions.append({
                'code': code,
                'name': name,
                'strategy': strategy_name,
                'action': action,
                'current_price': current_price,
                'pnl_pct': round(pnl_pct, 2),
            })

    if actions:
        print(f"\n  --- 需要操作: {len(actions)} 笔 ---")
        for a in actions:
            print(f"  [{a['action']}] {a['code']} {a['name']} → 卖出@{a['current_price']:.2f}")

        if auto_close:
            # 自动平仓 (动态再平衡: 资金回到现金池)
            account = data['account']
            closed_count = 0
            for a in actions:
                for pos in data['positions']:
                    if pos['code'] == a['code'] and pos['status'] == 'holding':
                        pos['status'] = 'closed'
                        pos['sell_date'] = today
                        pos['sell_price'] = a['current_price']
                        pos['sell_reason'] = a['action']
                        pos['pnl_pct'] = a['pnl_pct']

                        # 计算盈亏金额并更新account
                        buy_amount = pos.get('buy_amount', account['initial_capital'] / 5)
                        pnl_amount = buy_amount * (a['pnl_pct'] / 100.0)
                        pos['pnl_amount'] = round(pnl_amount, 2)

                        proceeds = buy_amount + pnl_amount
                        account['cash'] += proceeds
                        account['realized_pnl'] += pnl_amount

                        # 移到closed_trades
                        closed_record = pos.copy()
                        del closed_record['status']
                        data['closed_trades'].append(closed_record)

                        closed_count += 1
                        break

            # 清理positions中已closed的
            data['positions'] = [p for p in data['positions'] if p.get('status') != 'closed']
            account['realized_pnl'] = round(account['realized_pnl'], 2)
            account['cash'] = round(account['cash'], 2)
            save_positions(data)
            print(f"\n  [AUTO-CLOSE] 已自动平仓 {closed_count} 笔")
            print(f"  现金: ¥{account['cash']:,.0f} | 累计PnL: ¥{account['realized_pnl']:+,.0f}")
        else:
            print(f"  确认卖出: python realtime/position_tracker.py confirm-sell CODE PRICE")

    print(f"\n{'=' * 60}")


def _find_historical_sell_price(pos: dict, today: str) -> float:
    """用DB数据回溯查找持仓应在哪天以什么价格卖出。
    规则: T+1后逐日检查H1-H4价格, 先看TP/SL, 最后按expire日H4_close强平。
    """
    code = pos['code']
    buy_date = pos['buy_date']
    tp_price = pos['tp_price']
    sl_price = pos['sl_price']
    expire_date = pos['expire_date']

    conn = sqlite3.connect(DATA_DB)
    # 获取买入日之后的所有交易日数据
    cur = conn.execute("""
        SELECT date, open, high, low, close,
               hour1_open, hour1_close, hour2_open, hour2_close,
               hour3_open, hour3_close, hour4_open, hour4_close
        FROM stock_kline WHERE code = ? AND date > ? AND date <= ?
        ORDER BY date
    """, (code, buy_date, today))

    for row in cur.fetchall():
        date = row[0]
        day_open, day_high, day_low, day_close = row[1:5]
        hours = [(row[5], row[6]), (row[7], row[8]), (row[9], row[10]), (row[11], row[12])]

        # 检查是否开盘就触发SL(跳空低开)
        if day_open and day_open <= sl_price:
            conn.close()
            return day_open

        # 检查是否开盘就触发TP(跳空高开)
        if day_open and day_open >= tp_price:
            conn.close()
            return day_open

        # 逐小时检查
        for h_open, h_close in hours:
            if h_close and h_close >= tp_price:
                conn.close()
                return tp_price  # 止盈价卖出
            if h_close and h_close <= sl_price:
                conn.close()
                return sl_price  # 止损价卖出

        # 到期日强制卖出 - H4收盘
        if date >= expire_date:
            sell_p = hours[3][1] or day_close  # hour4_close or day_close
            conn.close()
            return sell_p

    conn.close()
    return None


def _guess_current_hour() -> int:
    """根据当前时间推测trading hour。"""
    now = datetime.now()
    h = now.hour
    m = now.minute
    if h < 10:
        return 1
    elif h < 11 or (h == 11 and m <= 30):
        return 2
    elif h < 14:
        return 3
    else:
        return 4


# =============================================================================
# 命令: confirm-buy
# =============================================================================

def confirm_buy():
    """确认买入pending记录，转入正式持仓。使用动态再平衡分配资金。"""
    pending = load_pending()
    if not pending:
        print("[position_tracker] 无待确认的买入记录")
        return

    data = load_positions()
    account = data['account']

    print(f"\n确认买入以下股票 (动态再平衡模式):")
    print(f"  当前总净值: ¥{account['total_nav']:,.0f} | 可用现金: ¥{account['cash']:,.0f}")

    for p in pending:
        # 动态再平衡: 每slot金额 = total_nav / 5
        buy_amount = get_buy_amount(data)
        slot_id = p.get('slot_id', '?')
        strategy = p.get('strategy', '?')

        if buy_amount < 10000:
            print(f"  [{slot_id}:{strategy}] {p['code']} {p['name']} - 跳过(资金不足)")
            continue

        print(f"  [{slot_id}:{strategy}] {p['code']} {p['name']} "
              f"买入@{p['buy_price']:.2f} 金额¥{buy_amount:,.0f} "
              f"TP={p['tp_price']:.2f} SL={p['sl_price']:.2f}")

        # 扣减现金
        account['cash'] -= buy_amount

        data['positions'].append({
            'code': p['code'],
            'name': p['name'],
            'strategy': strategy,
            'slot_id': slot_id,
            'buy_date': p['buy_date'],
            'buy_price': p['buy_price'],
            'buy_amount': round(buy_amount, 2),
            'tp_price': p['tp_price'],
            'sl_price': p['sl_price'],
            'expire_date': p['expire_date'],
            'status': 'holding',
        })

    save_positions(data)
    save_pending([])
    print(f"\n[OK] 已确认 {len(pending)} 笔买入，持仓已更新")
    print(f"  更新后现金: ¥{account['cash']:,.0f} | 总净值: ¥{account['total_nav']:,.0f}")


# =============================================================================
# 命令: confirm-sell
# =============================================================================

def confirm_sell(code: str, sell_price: float):
    """确认卖出。卖出后资金回到现金池，更新realized_pnl。"""
    data = load_positions()
    account = data['account']
    found = False

    for pos in data['positions']:
        if pos['code'] == code and pos['status'] == 'holding':
            pos['status'] = 'closed'
            pos['sell_date'] = datetime.now().strftime('%Y-%m-%d')
            pos['sell_price'] = sell_price
            pos['pnl_pct'] = round((sell_price / pos['buy_price'] - 1) * 100, 2)

            # 计算实际盈亏金额
            buy_amount = pos.get('buy_amount', account['initial_capital'] / 5)
            pnl_amount = buy_amount * (pos['pnl_pct'] / 100.0)
            pos['pnl_amount'] = round(pnl_amount, 2)

            # 更新account: 卖出资金回到现金池
            proceeds = buy_amount + pnl_amount
            account['cash'] += proceeds
            account['realized_pnl'] += pnl_amount
            account['realized_pnl'] = round(account['realized_pnl'], 2)
            account['cash'] = round(account['cash'], 2)

            # 移到closed_trades
            closed_record = pos.copy()
            del closed_record['status']
            data['closed_trades'].append(closed_record)

            pnl = pos['pnl_pct']
            print(f"[OK] 已卖出: {code} {pos['name']} [{pos.get('strategy','')}] "
                  f"买入{pos['buy_price']:.2f} → 卖出{sell_price:.2f} 收益{pnl:+.2f}%")
            print(f"  盈亏金额: ¥{pnl_amount:+,.0f} | "
                  f"累计已实现PnL: ¥{account['realized_pnl']:+,.0f}")
            print(f"  下次每slot买入: ¥{(account['cash'] + sum(p.get('buy_amount', 0) for p in data['positions'] if p.get('status')=='holding')) / 5:,.0f}")
            found = True
            break

    if not found:
        print(f"[ERROR] 未找到 {code} 的持仓记录")
        return

    # 从positions中移除已closed的
    data['positions'] = [p for p in data['positions'] if p.get('status') != 'closed']
    save_positions(data)


# =============================================================================
# 命令: add
# =============================================================================

def add_position(code: str, name: str, strategy: str, slot_id: str,
                 buy_price: float, tp_price: float, sl_price: float, expire_date: str):
    """直接添加持仓 (使用动态再平衡分配资金)。"""
    data = load_positions()
    account = data['account']
    buy_amount = get_buy_amount(data)

    # 扣减现金
    account['cash'] -= buy_amount
    account['cash'] = round(account['cash'], 2)

    data['positions'].append({
        'code': code,
        'name': name,
        'strategy': strategy,
        'slot_id': slot_id,
        'buy_date': datetime.now().strftime('%Y-%m-%d'),
        'buy_price': buy_price,
        'buy_amount': round(buy_amount, 2),
        'tp_price': tp_price,
        'sl_price': sl_price,
        'expire_date': expire_date,
        'status': 'holding',
    })
    save_positions(data)
    print(f"[OK] 已添加: {code} {name} [{slot_id}:{strategy}] "
          f"@{buy_price:.2f} 金额¥{buy_amount:,.0f} TP={tp_price:.2f} SL={sl_price:.2f}")
    print(f"  剩余现金: ¥{account['cash']:,.0f}")


# =============================================================================
# 命令: list
# =============================================================================

def list_positions():
    """列出所有持仓。"""
    data = load_positions()
    account = data['account']
    holdings = [p for p in data['positions'] if p.get('status') == 'holding']
    closed = data.get('closed_trades', [])

    print(f"\n{'=' * 60}")
    print(f"  持仓列表 (动态再平衡模式)")
    print(f"{'=' * 60}")
    print(f"  账户: 初始¥{account['initial_capital']:,.0f} | "
          f"现金¥{account['cash']:,.0f} | "
          f"已实现PnL¥{account['realized_pnl']:+,.0f} | "
          f"总净值¥{account['total_nav']:,.0f}")
    print(f"  下次买入: ¥{get_buy_amount(data):,.0f}/slot")

    if holdings:
        print(f"\n  --- 当前持仓 ({len(holdings)}只) ---")
        for p in holdings:
            slot = p.get('slot_id', '?')
            amt = p.get('buy_amount', '?')
            print(f"  [{slot}] {p['code']} {p['name']} | 策略:{p['strategy']} "
                  f"买入:{p['buy_price']:.2f} @{p['buy_date']} 金额:¥{amt}"
                  f" | TP:{p['tp_price']:.2f} SL:{p['sl_price']:.2f} 到期:{p['expire_date']}")
    else:
        print(f"\n  当前无持仓")

    if closed:
        print(f"\n  --- 已平仓 (最近10笔) ---")
        for p in closed[-10:]:
            pnl = p.get('pnl_pct', '?')
            pnl_amt = p.get('pnl_amount', '?')
            print(f"  [{p.get('slot_id','?')}] {p['code']} {p['name']} | "
                  f"买{p['buy_price']:.2f}→卖{p.get('sell_price', '?')} "
                  f"收益:{pnl}% (¥{pnl_amt})")

    print(f"\n{'=' * 60}")


# =============================================================================
# Main
# =============================================================================

def main():
    if len(sys.argv) < 2:
        cmd = 'check'
    else:
        cmd = sys.argv[1]

    if cmd in ('check', 'auto-close'):
        test_date = None
        hour = None
        auto_close = (cmd == 'auto-close')
        for i, arg in enumerate(sys.argv[2:], 2):
            if arg == '--test-date' and i < len(sys.argv) - 1:
                test_date = sys.argv[i + 1]
            elif arg.startswith('--test-date='):
                test_date = arg.split('=', 1)[1]
            elif arg == '--hour' and i < len(sys.argv) - 1:
                hour = int(sys.argv[i + 1])
            elif arg.startswith('--hour='):
                hour = int(arg.split('=', 1)[1])
            elif arg == '--auto-close':
                auto_close = True
        check_positions(test_date, hour, auto_close)

    elif cmd == 'confirm-buy':
        confirm_buy()

    elif cmd == 'confirm-sell':
        if len(sys.argv) < 4:
            print("用法: python realtime/position_tracker.py confirm-sell CODE PRICE")
            return 1
        code = sys.argv[2]
        price = float(sys.argv[3])
        confirm_sell(code, price)

    elif cmd == 'add':
        if len(sys.argv) < 10:
            print("用法: python realtime/position_tracker.py add "
                  "CODE NAME STRATEGY SLOT_ID BUY_PRICE TP SL EXPIRE_DATE")
            return 1
        add_position(
            code=sys.argv[2],
            name=sys.argv[3],
            strategy=sys.argv[4],
            slot_id=sys.argv[5],
            buy_price=float(sys.argv[6]),
            tp_price=float(sys.argv[7]),
            sl_price=float(sys.argv[8]),
            expire_date=sys.argv[9],
        )

    elif cmd == 'list':
        list_positions()

    else:
        print(f"未知命令: {cmd}")
        print("可用命令: check, confirm-buy, confirm-sell, add, list")
        return 1

    return 0


if __name__ == '__main__':
    sys.exit(main())
