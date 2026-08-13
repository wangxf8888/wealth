"""实盘数据源适配器 - 为策略类提供与BacktestDataFeed兼容的接口。

设计:
- 策略类依赖 data_feed.get_market_snapshot(date, hour) 获取行情快照
- 回测中由BacktestDataFeed从DB提供完整数据
- 实盘中有两种场景需要适配:
  1. 晚间候选生成(signal_mode): 次日open未知 → 用permissive值使候选通过open过滤
  2. 早盘决策(live_mode): 注入实时开盘价 → 策略精确判断买入条件

接口兼容:
- get_market_snapshot(date, hour)
- get_hour_open(code, date, hour)
- get_hour_high/low/close (for position tracking)
- _prev_trading_date(date)
- _load_day(date)
- _date_index
"""
import sqlite3
import pandas as pd
import numpy as np
from datetime import datetime, timedelta

import trading_rules
from realtime.config import PERMISSIVE_OPEN_RATIO


# ==============================================================================
# [Task#218 2026-08-07] 行情源主备切换骨架 —— 腾讯qt主源 + 东财push2热备
# ==============================================================================
# 现状: 腾讯qt是实盘唯一实时主源(morning_decision/_fetch_from_tencent等5处),
#       单点故障无热备。备源能力已封装在 tools/em_snapshot.py(与腾讯解析
#       同构的统一字段输出, 双源对拍单测见 research/results/t218_api_onboard/)。
# 本次只交付能力, 不激活生产切换: DUAL_SOURCE_ENABLED默认False,
# 下面骨架函数生产链零调用, 激活需leader审批后在调用点接线。
#
# 激活时的接线点位(均为各自文件内 _fetch_from_tencent 失败分支):
#   1. realtime/morning_decision.py::_fetch_from_tencent — 9:25开盘价(现fallback新浪)
#   2. realtime/position_tracker.py::_fetch_from_tencent — 持仓监控
#   3. realtime/notify.py::_fetch_from_tencent — 通知行情
#   接线模式(调用示例):
#     from realtime import data_feed as df_mod
#     quotes = _fetch_from_tencent(codes)
#     if not quotes and df_mod.record_tencent_failure():   # 连续N次失败触发
#         from tools.em_snapshot import fetch_eastmoney_snapshot
#         em = fetch_eastmoney_snapshot(codes, req_gap=1.0)  # 生产限速≥腾讯同级
#         quotes = {c: {'open': v['open'], 'preclose': v['preclose']}
#                   for c, v in em.items() if v['open'] > 0}
#     else:
#         df_mod.record_tencent_success()
DUAL_SOURCE_ENABLED = False   # 热备总开关(未经leader审批不得置True)
TENCENT_FAIL_THRESHOLD = 3    # 腾讯连续失败N次后切备源
_tencent_fail_count = [0]     # 进程内连续失败计数(成功即清零)


def record_tencent_failure() -> bool:
    """腾讯源失败一次; 返回是否应切换到东财备源(开关开且达阈值)。"""
    _tencent_fail_count[0] += 1
    return (DUAL_SOURCE_ENABLED
            and _tencent_fail_count[0] >= TENCENT_FAIL_THRESHOLD)


def record_tencent_success():
    """腾讯源成功, 清零连续失败计数(回切主源)。"""
    _tencent_fail_count[0] = 0


# 与BacktestDataFeed一致的字段定义
_DAY_FIELDS = ['open', 'open_rate', 'high', 'low', 'close', 'close_rate',
               'volume', 'amount', 'turn', 'preclose']


class RealtimeDataFeed:
    """实盘数据源 - 提供策略接口所需的全部数据访问方法。"""

    def __init__(self, db_path: str):
        self.db_path = db_path
        self._conn = sqlite3.connect(db_path)
        self._trading_dates = self._load_all_trading_dates()
        self._date_index = {d: i for i, d in enumerate(self._trading_dates)}
        self._day_cache = {}
        self._cache_order = []
        self._max_cache_days = 10

        # 模式控制
        self._mode = 'normal'  # 'normal' | 'signal' | 'live'
        self._signal_date = None
        self._trade_date = None
        self._injected_open = {}  # {code: open_price}
        # 大盘指数
        self._index_close_rate = self._load_index_close_rates()

    # ==========================================================================
    # 模式设置
    # ==========================================================================

    def set_signal_mode(self, signal_date: str, trade_date: str):
        """晚间候选生成模式: signal_date为信号日(当日), trade_date为次日交易日。
        get_market_snapshot(trade_date, 1) 返回:
          - prev_* = signal_date完整数据
          - open/open_rate = 极低值(permissive, 使所有open过滤通过)
        """
        self._mode = 'signal'
        self._signal_date = signal_date
        self._trade_date = trade_date
        # 确保trade_date在_date_index中(即使DB没有)
        if trade_date not in self._date_index:
            self._date_index[trade_date] = len(self._trading_dates)
            self._trading_dates.append(trade_date)

    def set_live_mode(self, trade_date: str, open_prices: dict):
        """早盘决策模式: 注入实时开盘价, trade_date为当日。
        get_market_snapshot(trade_date, 1) 返回:
          - prev_* = 上一交易日完整数据(从DB)
          - open/open_rate = 注入的实时价格
        """
        self._mode = 'live'
        self._trade_date = trade_date
        self._injected_open = open_prices or {}
        if trade_date not in self._date_index:
            self._date_index[trade_date] = len(self._trading_dates)
            self._trading_dates.append(trade_date)

    # ==========================================================================
    # 核心接口 - 策略调用
    # ==========================================================================

    def get_market_snapshot(self, date: str, hour: int) -> dict:
        """返回 {code: {字段dict}}, 兼容BacktestDataFeed接口。"""
        if self._mode == 'signal' and date == self._trade_date:
            return self._signal_snapshot()
        elif self._mode == 'live' and date == self._trade_date:
            return self._live_snapshot()
        else:
            return self._db_snapshot(date, hour)

    def get_hour_open(self, code: str, date: str, hour: int) -> float:
        """获取code在date/hour的开盘价。"""
        if date == self._trade_date and self._mode in ('signal', 'live'):
            if hour == 1:
                if self._mode == 'live':
                    return self._injected_open.get(code, 0.0)
                else:
                    # signal mode: 返回permissive价格
                    return self._get_permissive_open(code)
            return 0.0
        # DB mode
        return self._db_hour_field(code, date, hour, 'open')

    def get_hour_high(self, code: str, date: str, hour: int) -> float:
        """获取hour级最高价(盘中监控用)。"""
        if date == self._trade_date and self._mode in ('signal', 'live'):
            return 0.0  # 实盘盘中无法预知high
        return self._db_hour_field(code, date, hour, 'high')

    def get_hour_low(self, code: str, date: str, hour: int) -> float:
        """获取hour级最低价(盘中监控用)。"""
        if date == self._trade_date and self._mode in ('signal', 'live'):
            return 0.0
        return self._db_hour_field(code, date, hour, 'low')

    def get_hour_close(self, code: str, date: str, hour: int) -> float:
        """获取hour级收盘价。"""
        if date == self._trade_date and self._mode in ('signal', 'live'):
            return 0.0
        return self._db_hour_field(code, date, hour, 'close')

    def get_stock_history(self, code: str, date: str, lookback_days: int) -> list:
        """返回 code 在 date 之前 lookback_days 个交易日的日K。"""
        cur = self._conn.execute(
            "SELECT date, open, high, low, close, volume, amount, turn, "
            "close_rate, preclose FROM stock_kline "
            "WHERE code = ? AND date < ? ORDER BY date DESC LIMIT ?",
            (code, date, lookback_days))
        rows = cur.fetchall()
        cols = ['date', 'open', 'high', 'low', 'close', 'volume', 'amount',
                'turn', 'close_rate', 'preclose']
        out = [dict(zip(cols, r)) for r in rows]
        out.reverse()
        return out

    def get_limit_prices(self, code: str, date: str) -> tuple:
        """返回 (涨停价, 跌停价)。语义对齐 BacktestDataFeed.get_limit_prices:
        当日preclose+isST → trading_rules.limit_prices; 无数据返回(0.0, 0.0)不拦截。

        signal/live模式下 date==trade_date 时走 _synthetic_today_df,
        其 preclose=前一交易日close(实盘口径), isST沿用前日标记。(Task#36 B2-A依赖)
        """
        today = self._load_day(date)
        if today is None or today.empty or code not in today.index:
            return 0.0, 0.0
        row = today.loc[code]
        try:
            preclose = float(row.get('preclose') or 0)
        except (TypeError, ValueError):
            preclose = 0.0
        st_val = row.get('isST', 0)
        is_st = bool(int(st_val)) if st_val is not None and not pd.isna(st_val) else False
        return trading_rules.limit_prices(code, preclose, is_st)

    def is_market_down(self, date: str, threshold: float = -1.0) -> bool:
        """前一交易日大盘是否大跌。"""
        prev_date = self._prev_trading_date(date)
        if prev_date is None:
            return False
        close_rate = self._index_close_rate.get(prev_date)
        if close_rate is None:
            return False
        return close_rate < threshold

    # ==========================================================================
    # 策略内部常用方法(兼容BacktestDataFeed私有API)
    # ==========================================================================

    def _prev_trading_date(self, date: str):
        """获取前一交易日。"""
        idx = self._date_index.get(date)
        if idx is None or idx == 0:
            # date不在DB中, 取DB最后一个日期
            if self._trading_dates:
                # 找DB中小于date的最后一个交易日
                for d in reversed(self._trading_dates):
                    if d < date:
                        return d
            return None
        return self._trading_dates[idx - 1]

    def _load_day(self, date: str) -> pd.DataFrame:
        """加载某日全市场数据(含hourly列)。"""
        if date == self._trade_date and self._mode in ('signal', 'live'):
            return self._synthetic_today_df(date)
        if date in self._day_cache:
            return self._day_cache[date]
        df = pd.read_sql(
            "SELECT * FROM stock_kline WHERE date = ?",
            self._conn, params=(date,))
        if not df.empty:
            df = df.set_index('code', drop=False)
        self._day_cache[date] = df
        self._cache_order.append(date)
        if len(self._cache_order) > self._max_cache_days:
            old = self._cache_order.pop(0)
            self._day_cache.pop(old, None)
        return df

    # ==========================================================================
    # 内部实现
    # ==========================================================================

    def _load_all_trading_dates(self) -> list:
        cur = self._conn.execute(
            "SELECT DISTINCT date FROM stock_kline ORDER BY date")
        return [r[0] for r in cur.fetchall()]

    def _load_index_close_rates(self) -> dict:
        cur = self._conn.execute(
            "SELECT date, close_rate FROM index_kline WHERE code='sh.000001'")
        return {row[0]: row[1] for row in cur.fetchall() if row[1] is not None}

    def _signal_snapshot(self) -> dict:
        """晚间候选模式: prev_*来自signal_date, open/open_rate用极低值。"""
        signal_df = self._load_day(self._signal_date)
        if signal_df is None or signal_df.empty:
            return {}

        result = {}
        for code, row in signal_df.to_dict('index').items():
            preclose = row.get('close') or 0  # trade_date的preclose = signal_date的close
            if preclose <= 0:
                continue
            # 价格/涨幅解耦(P0-2修复): open_rate固定-20喂低开类条件全部放行,
            # open价格用PERMISSIVE_OPEN_RATIO(0.80时与旧行为逐位一致;
            # 0.995时温和低开, 不再撞死S3非跌停开/S4 open>昨low下界过滤)
            permissive_open = round(preclose * PERMISSIVE_OPEN_RATIO, 2)
            info = {
                'code': code,
                'code_name': row.get('code_name', '') or '',
                'isST': int(row.get('isST', 0) or 0),
                'open': permissive_open,
                'open_rate': -20.0,
                'preclose': preclose,
            }
            # prev_* = signal_date的实际数据
            for k in _DAY_FIELDS:
                info[f'prev_{k}'] = float(row.get(k) or 0)
            result[code] = info
        return result

    def _live_snapshot(self) -> dict:
        """早盘决策模式: prev_*来自DB最后一个交易日, open来自注入。"""
        prev_date = self._prev_trading_date(self._trade_date)
        if not prev_date:
            return {}
        signal_df = self._load_day(prev_date)
        if signal_df is None or signal_df.empty:
            return {}

        result = {}
        for code, row in signal_df.to_dict('index').items():
            preclose = row.get('close') or 0
            if preclose <= 0:
                continue
            open_price = self._injected_open.get(code, 0.0)
            if open_price <= 0:
                # 未获取到开盘价的股票，设open_rate=0(不满足低开条件)
                open_rate = 0.0
                open_price = preclose
            else:
                open_rate = (open_price / preclose - 1) * 100

            info = {
                'code': code,
                'code_name': row.get('code_name', '') or '',
                'isST': int(row.get('isST', 0) or 0),
                'open': open_price,
                'open_rate': open_rate,
                'preclose': preclose,
            }
            for k in _DAY_FIELDS:
                info[f'prev_{k}'] = float(row.get(k) or 0)
            result[code] = info
        return result

    def _synthetic_today_df(self, date: str) -> pd.DataFrame:
        """为trade_date生成合成DataFrame(策略内部_load_day调用时使用)。"""
        # 获取signal_date数据作为基础
        prev_date = self._prev_trading_date(date)
        if not prev_date:
            return pd.DataFrame()
        signal_df = self._load_day(prev_date)
        if signal_df is None or signal_df.empty:
            return pd.DataFrame()

        # 构建合成DataFrame
        records = []
        for code, row in signal_df.to_dict('index').items():
            preclose = row.get('close') or 0
            if preclose <= 0:
                continue
            if self._mode == 'live':
                open_p = self._injected_open.get(code, preclose)
                open_rate = (open_p / preclose - 1) * 100 if preclose > 0 else 0
            else:
                # signal模式: 价格/涨幅解耦, 与_signal_snapshot同口径
                open_p = round(preclose * PERMISSIVE_OPEN_RATIO, 2)  # permissive
                open_rate = -20.0

            records.append({
                'code': code,
                'code_name': row.get('code_name', '') or '',
                'date': date,
                'open': open_p,
                'open_rate': open_rate,
                'preclose': preclose,
                'isST': row.get('isST', 0),
                'hour1_open': open_p,  # hour1_open ≈ 日open
                'hour1_close': 0,
                'hour1_high': 0,
                'hour1_low': 0,
            })
        if not records:
            return pd.DataFrame()
        df = pd.DataFrame(records)
        df = df.set_index('code', drop=False)
        return df

    def _get_permissive_open(self, code: str) -> float:
        """signal模式下获取permissive开盘价。"""
        signal_df = self._load_day(self._signal_date)
        if signal_df is None or signal_df.empty or code not in signal_df.index:
            return 0.0
        preclose = signal_df.loc[code].get('close') or 0
        return round(preclose * PERMISSIVE_OPEN_RATIO, 2) if preclose > 0 else 0.0

    def _db_snapshot(self, date: str, hour: int) -> dict:
        """正常DB模式(与BacktestDataFeed一致)。"""
        today = self._load_day(date)
        if today is None or today.empty:
            return {}
        prev_date = self._prev_trading_date(date)
        prev = self._load_day(prev_date) if prev_date else None
        prev_records = (prev.to_dict('index')
                        if prev is not None and not prev.empty else {})
        today_records = today.to_dict('index')

        result = {}
        for code, row in today_records.items():
            info = {
                'code': code,
                'code_name': row.get('code_name', '') or '',
                'isST': int(row.get('isST', 0) or 0),
                'open': float(row.get('open') or 0),
                'open_rate': float(row.get('open_rate') or 0),
                'preclose': float(row.get('preclose') or 0),
            }
            prow = prev_records.get(code)
            if prow is not None:
                for k in _DAY_FIELDS:
                    info[f'prev_{k}'] = float(prow.get(k) or 0)
            # 已走完的小时
            for h in range(1, hour):
                for suffix in ['open', 'high', 'low', 'close', 'volume', 'amount']:
                    col = f'hour{h}_{suffix}'
                    if col in row:
                        info[col] = float(row.get(col) or 0)
            result[code] = info
        return result

    def _db_hour_field(self, code: str, date: str, hour: int, field: str) -> float:
        """从DB获取指定hour的字段值。"""
        today = self._load_day(date)
        if today is None or today.empty or code not in today.index:
            return 0.0
        row = today.loc[code]
        val = row.get(f'hour{hour}_{field}')
        try:
            return float(val) if val is not None and not pd.isna(val) else 0.0
        except (TypeError, ValueError):
            return 0.0

    def close(self):
        """关闭数据库连接。"""
        if self._conn:
            self._conn.close()
            self._conn = None
