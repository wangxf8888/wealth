#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Task#105 跌停反弹V2 slot=1 回测 runner"""
import sys
sys.path.insert(0, '/home/AIWealth')

from engine.backtest_engine import BacktestEngineV3
from engine.buy_strategy_limitdown_rebound_v2 import LimitDownReboundV2Strategy
from engine.sell_module import PerSlotSellStrategy
from engine.blacklist import Blacklist

engine = BacktestEngineV3(
    buy_module=LimitDownReboundV2Strategy(buy_hour=1, target_hold_days=5),
    sell_module=PerSlotSellStrategy(sell_hour=4),
    blacklist=Blacklist(),
    db_path='/home/AIWealth/data/stocks.db',
    start_date='2021-01-01',
    end_date='2026-06-30',
    initial_capital=1_000_000,
    n_slots=1,
    output_path='/home/AIWealth/scripts/logs/limitdown_rebound_v2_fixed_slot1.log',
)
engine.run()
