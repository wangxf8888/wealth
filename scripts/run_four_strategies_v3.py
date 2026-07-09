#!/usr/bin/env python3
"""四策略V3引擎回测 + 交易明细导出"""
import sys, json, sqlite3
sys.path.insert(0, '/home/AIWealth')

from engine.backtest_engine import BacktestEngineV3
from engine.buy_strategy_four_v2 import FourStrategyBuyer
from engine.sell_module import PerSlotSellStrategy
from engine.blacklist import Blacklist

DB_PATH = '/home/AIWealth/data/stocks.db'


def export_trades_json(portfolio, db_path, output_path):
    """导出交易明细，含持仓期OHLC rate"""
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    trades_out = []

    # 只处理卖出记录(含完整买卖信息)
    sell_trades = [t for t in portfolio.all_trades if t.get('type') == 'sell']

    for i, t in enumerate(sell_trades):
        buy_price = t.get('buy_price', 0)
        if buy_price <= 0:
            continue

        # 查询持仓期间每天的hour级数据
        holding_detail = []
        if t.get('buy_date') and t.get('sell_date'):
            rows = conn.execute(
                """SELECT date, hour1_open, hour1_high, hour1_low, hour1_close,
                          hour2_open, hour2_high, hour2_low, hour2_close,
                          hour3_open, hour3_high, hour3_low, hour3_close,
                          hour4_open, hour4_high, hour4_low, hour4_close
                   FROM stock_kline
                   WHERE code=? AND date>=? AND date<=?
                   ORDER BY date""",
                (t['code'], t['buy_date'], t['sell_date'])
            ).fetchall()

            for row in rows:
                day_detail = {'date': row['date']}
                for h in range(1, 5):
                    for field in ['open', 'high', 'low', 'close']:
                        val = row[f'hour{h}_{field}']
                        if val and val > 0:
                            rate = round((val - buy_price) / buy_price * 100, 2)
                        else:
                            rate = None
                        day_detail[f'h{h}_{field}_rate'] = rate
                holding_detail.append(day_detail)

        sell_price = t.get('sell_price', 0)
        ret_pct = round((sell_price - buy_price) / buy_price * 100, 2) if buy_price > 0 else 0

        trades_out.append({
            'id': i + 1,
            'code': t.get('code', ''),
            'name': t.get('code_name', ''),
            'strategy': t.get('strategy_name', ''),
            'buy_date': t.get('buy_date', ''),
            'sell_date': t.get('sell_date', ''),
            'buy_price': buy_price,
            'sell_price': sell_price,
            'return_pct': ret_pct,
            'buy_reason': t.get('buy_reason', ''),
            'holding_days': t.get('hold_days', 0),
            'holding_detail': holding_detail,
        })

    conn.close()
    with open(output_path, 'w', encoding='utf-8') as f:
        json.dump(trades_out, f, ensure_ascii=False, indent=2)
    print(f"交易明细已导出: {output_path} ({len(trades_out)}笔)")


def main():
    buy_module = FourStrategyBuyer()
    sell_module = PerSlotSellStrategy(sell_hour=4)
    blacklist = Blacklist()

    engine = BacktestEngineV3(
        buy_module=buy_module,
        sell_module=sell_module,
        blacklist=blacklist,
        db_path=DB_PATH,
        start_date='2021-01-01',
        end_date='2026-06-30',
        initial_capital=1_000_000,
        n_slots=5,
        index_ma_filter=20,
        regime_index_code='sz.399001',
        output_path='/home/AIWealth/scripts/logs/four_v3_backtest.log'
    )

    engine.run()

    # 导出交易明细
    export_trades_json(engine.portfolio, DB_PATH,
                       '/home/AIWealth/frontend/four_v3_trades.json')

    # 打印汇总
    nav = engine.portfolio.get_nav()
    total_trades = len([t for t in engine.portfolio.all_trades if t.get('type') == 'sell'])
    wins = sum(1 for t in engine.portfolio.all_trades
               if t.get('type') == 'sell' and t.get('sell_price', 0) > t.get('buy_price', 0))
    win_rate = wins / total_trades * 100 if total_trades > 0 else 0
    print(f"\n{'='*60}")
    print(f"四策略V3回测完成")
    print(f"最终净值: ¥{nav:,.0f}")
    print(f"总收益: {(nav/1_000_000-1)*100:.1f}%")
    print(f"交易笔数: {total_trades}")
    print(f"胜率: {win_rate:.1f}%")
    print(f"{'='*60}")


if __name__ == '__main__':
    main()
