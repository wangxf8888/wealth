"""回测 CLI 入口。

用法:
  python -m backtest.run --strategy dragon_pullback --slots 1 \
      --start 2021-01-01 --end 2026-06-30

策略解析：动态导入 strategies.<strategy> 模块，取其中首个 Strategy 子类。
"""
import argparse
import importlib
import inspect
import json
import os
from dataclasses import asdict

from strategies.base import Strategy
from backtest.data_feed import BacktestDataFeed
from backtest.engine import BacktestEngine
from backtest.context_enrichment import enrich_trades_with_context

DB_PATH = '/home/AIWealth/data/stocks.db'
LOG_DIR = '/home/AIWealth/logs/backtest'


def load_strategy(name: str) -> Strategy:
    """从 strategies.<name> 动态加载 Strategy 子类并实例化。"""
    module = importlib.import_module(f'strategies.{name}')
    for _, obj in inspect.getmembers(module, inspect.isclass):
        if issubclass(obj, Strategy) and obj is not Strategy \
                and obj.__module__ == module.__name__:
            return obj()
    raise ValueError(f"在 strategies.{name} 中未找到 Strategy 子类")


def save_trades(strategy_name: str, slots: int, summary: dict, trades: list,
                db_path: str = DB_PATH):
    os.makedirs(LOG_DIR, exist_ok=True)
    path = os.path.join(LOG_DIR, f'{strategy_name}_s{slots}_trades.json')
    trade_dicts = [asdict(t) for t in trades]

    # 增强：为每笔交易添加前后5日hour级OHLC上下文
    print(f"正在为 {len(trade_dicts)} 笔交易生成上下文...")
    enrich_trades_with_context(trade_dicts, db_path)

    payload = {
        'summary': summary,
        'trades': trade_dicts,
    }
    with open(path, 'w', encoding='utf-8') as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)
    print(f"交易明细已保存: {path} ({len(trades)} 笔)")


def main():
    parser = argparse.ArgumentParser(description='小时级回测引擎')
    parser.add_argument('--strategy', required=True, help='策略模块名(strategies/<name>.py)')
    parser.add_argument('--slots', type=int, default=1, help='仓位数')
    parser.add_argument('--start', required=True, help='起始日期 YYYY-MM-DD')
    parser.add_argument('--end', required=True, help='结束日期 YYYY-MM-DD')
    parser.add_argument('--capital', type=float, default=1_000_000, help='初始资金')
    parser.add_argument('--db', default=DB_PATH, help='数据库路径')
    args = parser.parse_args()

    strategy = load_strategy(args.strategy)
    print(f"策略: {getattr(strategy, 'name', args.strategy)} | 仓位: {args.slots}")

    data_feed = BacktestDataFeed(args.db)
    engine = BacktestEngine(strategy, data_feed,
                            n_slots=args.slots,
                            initial_capital=args.capital)
    summary = engine.run(args.start, args.end)
    engine.print_summary(summary)

    save_trades(args.strategy, args.slots, summary,
                engine.portfolio.get_all_trades(), db_path=args.db)
    data_feed.close()


if __name__ == '__main__':
    main()
