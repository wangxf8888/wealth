#!/usr/bin/env python3
"""
企业微信通知格式测试脚本
========================
用模拟数据发送4条测试消息到Webhook，确认消息格式效果及发送成功（errcode=0）。

测试消息：
1. 买入信号（带日期、20%仓位、持仓栏目、免责声明）
2. 卖出信号-亏损（带日期、盈亏、持仓栏目、免责声明）
3. 卖出信号-盈利（验证红色着色）
4. 盘后日报
5. 打板信号

用法: python3 realtime/test_notify_format.py
"""
import sys
import os
from datetime import datetime

# 确保能 import realtime.notify
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# source 环境变量（如果当前shell没有QYWX_WEBHOOK_KEY）
if not os.environ.get('QYWX_WEBHOOK_KEY'):
    env_file = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                            '.qywx_env.sh')
    if os.path.exists(env_file):
        with open(env_file) as f:
            for line in f:
                line = line.strip()
                if line.startswith('export QYWX_WEBHOOK_KEY='):
                    key = line.split('=', 1)[1].strip("'\"")
                    os.environ['QYWX_WEBHOOK_KEY'] = key
                    break

from realtime.notify import (notify_buy, notify_sell, notify_surge,
                             notify_daily_summary, QYWX_WEBHOOK_KEY)


def main():
    print("=" * 60)
    print("企业微信通知格式测试")
    print("=" * 60)
    print(f"QYWX_WEBHOOK_KEY: {'已配置' if QYWX_WEBHOOK_KEY else '未配置!!'}")
    if not QYWX_WEBHOOK_KEY:
        print("[ERROR] 请先 source /home/AIWealth/.qywx_env.sh")
        sys.exit(1)
    print()

    test_date = '2026-08-05'

    # ━━━ 1. 买入信号测试 ━━━
    print("[1/5] 发送买入信号测试...")
    ok_buy = notify_buy(
        date_str=test_date,
        time_str='09:30:00',
        stock_name='财富趋势',
        stock_code='688318',
        price=76.24,
        stop_profit_price=83.86,
        stop_loss_price=73.19,
    )
    print(f"  结果: {'成功' if ok_buy else '失败'}")
    print()

    # ━━━ 2. 卖出信号测试(亏损, 绿色) ━━━
    print("[2/5] 发送卖出信号测试(亏损)...")
    ok_sell = notify_sell(
        date_str=test_date,
        time_str='14:30:00',
        stock_name='唐源电气',
        stock_code='300859',
        sell_price=18.25,
        buy_price=20.00,
        hold_days=3,
    )
    print(f"  结果: {'成功' if ok_sell else '失败'}")
    print()

    # ━━━ 3. 卖出信号测试(盈利, 红色) ━━━
    print("[3/5] 发送卖出信号测试(盈利)...")
    ok_sell_win = notify_sell(
        date_str=test_date,
        time_str='10:05:00',
        stock_name='金龙羽',
        stock_code='002882',
        sell_price=16.50,
        buy_price=15.00,
        hold_days=2,
    )
    print(f"  结果: {'成功' if ok_sell_win else '失败'}")
    print()

    # ━━━ 4. 盘后日报测试 ━━━
    print("[4/5] 发送盘后日报测试...")
    trades_log = [
        {'action': 'buy', 'name': '财富趋势', 'code': '688318', 'price': 76.24},
        {'action': 'sell', 'name': '唐源电气', 'code': '300859', 'price': 18.25, 'pnl_pct': -8.75},
    ]
    today_pnl_pct = 0.82
    ok_summary = notify_daily_summary(test_date, trades_log, today_pnl_pct)
    print(f"  结果: {'成功' if ok_summary else '失败'}")
    print()

    # ━━━ 5. 打板信号测试 ━━━
    print("[5/5] 发送打板信号测试...")
    ok_surge = notify_surge(
        date_str=test_date,
        time_str='10:15:00',
        stock_name='中际旭创',
        stock_code='300308',
        current_price=85.50,
        surge_pct=9.98,
        vol_ratio=3.5,
    )
    print(f"  结果: {'成功' if ok_surge else '失败'}")
    print()

    # ━━━ 汇总 ━━━
    print("=" * 60)
    results = {
        '买入信号': ok_buy,
        '卖出信号(亏损)': ok_sell,
        '卖出信号(盈利)': ok_sell_win,
        '盘后日报': ok_summary,
        '打板信号': ok_surge,
    }
    all_ok = all(results.values())
    for name, ok in results.items():
        status = ' PASS' if ok else ' FAIL'
        print(f"  {name}: {status}")
    print("=" * 60)
    if all_ok:
        print("全部测试通过!")
    else:
        print("部分测试失败，请检查日志")
    sys.exit(0 if all_ok else 1)


if __name__ == '__main__':
    main()
