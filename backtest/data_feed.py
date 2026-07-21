"""回测数据源 - 从 stocks.db 读数据，严格按时序裁剪，杜绝未来函数。

合规核心：get_market_snapshot(date, hour) 在第 N 小时只返回：
  - 每只股票前一交易日的完整日K数据 (prev_*)
  - 今日竞价已知数据 open / open_rate / preclose (9:25 竞价结束即确定)
  - hour1 .. hour(N-1) 的完整 OHLC (已经走完的小时)
  绝不返回 hourN 及之后的任何 high/low/close/volume/amount。

买卖成交价用 get_hour_open(code, date, hour) = hourN_open（该 hour 开始即确定）。
"""
import sqlite3
import pandas as pd

# hour 级字段后缀（不含 open，open 单独处理）
_HOUR_FIELDS = ['open', 'open_rate', 'high', 'high_rate', 'low', 'low_rate',
                'close', 'close_rate', 'volume', 'amount']
# 日级 / 前日字段
_DAY_FIELDS = ['open', 'open_rate', 'high', 'low', 'close', 'close_rate',
               'volume', 'amount', 'turn', 'preclose']


class BacktestDataFeed:
    def __init__(self, db_path: str):
        self.db_path = db_path
        self._conn = sqlite3.connect(db_path)
        self._trading_dates = self._load_all_trading_dates()
        self._date_index = {d: i for i, d in enumerate(self._trading_dates)}
        # 缓存：最多保留最近若干天的整日 DataFrame，避免重复查库
        self._day_cache = {}          # {date: DataFrame(index=code)}
        self._cache_order = []        # LRU 顺序
        self._max_cache_days = 30     # 增大缓存避免频繁查库
        # 大盘指数日K缓存 {date: close_rate}
        self._index_close_rate = self._load_index_close_rates()
        # IPO 过滤缓存 {code: set(first_n_dates)}
        self._ipo_cache = {}
        # 预加载所有股票的首次上市日期(第1条K线日期)，用于快速判断
        self._first_date_map = self._load_first_dates()
        # 每只股票前5个交易日的日期缓存（按需加载）
        self._ipo_dates_loaded = False

    # ------------------------------------------------------------------
    # 大盘指数
    # ------------------------------------------------------------------
    def _load_index_close_rates(self) -> dict:
        """一次性加载上证指数(sh.000001)所有日期的close_rate，避免回测中反复查库。"""
        cur = self._conn.execute(
            "SELECT date, close_rate FROM index_kline WHERE code='sh.000001'")
        return {row[0]: row[1] for row in cur.fetchall() if row[1] is not None}

    def is_market_down(self, date: str, threshold: float = -1.0) -> bool:
        """判断前一交易日大盘是否大跌（避免未来函数）。

        Args:
            date: 当前交易日（买入日）
            threshold: 跌幅阈值(%)，默认 -1.0 表示前日跌>1%时返回True

        Returns:
            True 表示前一交易日上证指数跌幅超过阈值，应跳过买入
        """
        prev_date = self._prev_trading_date(date)
        if prev_date is None:
            return False
        close_rate = self._index_close_rate.get(prev_date)
        if close_rate is None:
            return False
        return close_rate < threshold

    # ------------------------------------------------------------------
    # 交易日
    # ------------------------------------------------------------------
    def _load_all_trading_dates(self) -> list:
        cur = self._conn.execute(
            "SELECT DISTINCT date FROM stock_kline ORDER BY date")
        return [r[0] for r in cur.fetchall()]

    def get_trading_dates(self, start_date: str, end_date: str) -> list:
        return [d for d in self._trading_dates if start_date <= d <= end_date]

    def _prev_trading_date(self, date: str):
        idx = self._date_index.get(date)
        if idx is None or idx == 0:
            return None
        return self._trading_dates[idx - 1]

    # ------------------------------------------------------------------
    # 整日数据加载（带 LRU 缓存）
    # ------------------------------------------------------------------
    def _load_day(self, date: str) -> pd.DataFrame:
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

    # ------------------------------------------------------------------
    # 全市场快照（合规裁剪）
    # ------------------------------------------------------------------
    def get_market_snapshot(self, date: str, hour: int) -> dict:
        """返回 {code: {字段dict}}，只含第 hour 小时开始时已知的信息。"""
        today = self._load_day(date)
        if today is None or today.empty:
            return {}
        prev_date = self._prev_trading_date(date)
        prev = self._load_day(prev_date) if prev_date else None
        # 用 to_dict('index') 一次性转成纯 dict，远快于逐行 iterrows()
        prev_records = (prev.to_dict('index')
                        if prev is not None and not prev.empty else {})
        today_records = today.to_dict('index')

        # 预计算已走完小时的列名（严格 < hour）
        hour_cols = [f'hour{h}_{k}'
                     for h in range(1, hour) for k in _HOUR_FIELDS]

        result = {}
        for code, row in today_records.items():
            info = {
                'code': code,
                'code_name': row.get('code_name', '') or '',
                'isST': int(row.get('isST', 0) or 0),
                # 今日竞价已知
                'open': _f(row.get('open')),
                'open_rate': _f(row.get('open_rate')),
                'preclose': _f(row.get('preclose')),
            }
            # 前一交易日完整日K
            prow = prev_records.get(code)
            if prow is not None:
                for k in _DAY_FIELDS:
                    info[f'prev_{k}'] = _f(prow.get(k))

            # hour1 .. hour(N-1) 已走完的小时
            for col in hour_cols:
                if col in row:
                    info[col] = _f(row.get(col))
            result[code] = info
        return result

    # ------------------------------------------------------------------
    # 单股历史日K
    # ------------------------------------------------------------------
    def get_stock_history(self, code: str, date: str, lookback_days: int) -> list:
        """返回 code 在 date 之前 lookback_days 个交易日的日K（不含 date）。"""
        cur = self._conn.execute(
            "SELECT date, open, high, low, close, volume, amount, turn, "
            "close_rate, preclose FROM stock_kline "
            "WHERE code = ? AND date < ? ORDER BY date DESC LIMIT ?",
            (code, date, lookback_days))
        rows = cur.fetchall()
        cols = ['date', 'open', 'high', 'low', 'close', 'volume', 'amount',
                'turn', 'close_rate', 'preclose']
        out = [dict(zip(cols, r)) for r in rows]
        out.reverse()  # 按时间升序
        return out

    # ------------------------------------------------------------------
    # 成交价 / 前日数据
    # ------------------------------------------------------------------
    def get_hour_open(self, code: str, date: str, hour: int) -> float:
        """获取某股某天某小时的开盘价（成交价，hour 开始即确定）。"""
        today = self._load_day(date)
        if today is None or today.empty or code not in today.index:
            return 0.0
        row = today.loc[code]
        val = row.get(f'hour{hour}_open')
        return _f(val)

    def get_hour_close(self, code: str, date: str, hour: int) -> float:
        """获取某股某天某小时的收盘价（hour 结束时确定，用于 MOC 订单模拟）。"""
        today = self._load_day(date)
        if today is None or today.empty or code not in today.index:
            return 0.0
        row = today.loc[code]
        val = row.get(f'hour{hour}_close')
        return _f(val)

    def get_hour_high(self, code: str, date: str, hour: int) -> float:
        """获取某股某天某小时的最高价（用于止盈触发检测）。"""
        today = self._load_day(date)
        if today is None or today.empty or code not in today.index:
            return 0.0
        row = today.loc[code]
        val = row.get(f'hour{hour}_high')
        return _f(val)

    def get_hour_low(self, code: str, date: str, hour: int) -> float:
        """获取某股某天某小时的最低价（用于止损触发检测）。"""
        today = self._load_day(date)
        if today is None or today.empty or code not in today.index:
            return 0.0
        row = today.loc[code]
        val = row.get(f'hour{hour}_low')
        return _f(val)

    def get_day_high(self, code: str, date: str) -> float:
        """获取某股某天的日线最高价（含集合竞价，覆盖全天）。"""
        today = self._load_day(date)
        if today is None or today.empty or code not in today.index:
            return 0.0
        row = today.loc[code]
        return _f(row.get('high'))

    def get_day_low(self, code: str, date: str) -> float:
        """获取某股某天的日线最低价（含集合竞价，覆盖全天）。"""
        today = self._load_day(date)
        if today is None or today.empty or code not in today.index:
            return 0.0
        row = today.loc[code]
        return _f(row.get('low'))

    def get_day_close(self, code: str, date: str) -> float:
        """获取某股某天的日线收盘价。"""
        today = self._load_day(date)
        if today is None or today.empty or code not in today.index:
            return 0.0
        row = today.loc[code]
        return _f(row.get('close'))

    def get_day_preclose(self, code: str, date: str) -> float:
        """获取某股某天的前收盘价（用于涨跌幅计算）。"""
        today = self._load_day(date)
        if today is None or today.empty or code not in today.index:
            return 0.0
        row = today.loc[code]
        return _f(row.get('preclose'))

    def get_prev_day(self, code: str, date: str) -> dict:
        """获取某股前一交易日的完整日K数据。"""
        prev_date = self._prev_trading_date(date)
        if prev_date is None:
            return {}
        prev = self._load_day(prev_date)
        if prev is None or prev.empty or code not in prev.index:
            return {}
        row = prev.loc[code]
        return {k: _f(row.get(k)) for k in _DAY_FIELDS}

    # ------------------------------------------------------------------
    # IPO 过滤：上市前5个交易日排除
    # ------------------------------------------------------------------
    def _load_first_dates(self) -> dict:
        """一次性加载所有股票的首条K线日期，用于快速过滤。"""
        cur = self._conn.execute(
            "SELECT code, MIN(date) FROM stock_kline GROUP BY code")
        return {row[0]: row[1] for row in cur.fetchall()}

    def is_ipo_period(self, code: str, date: str, n: int = 5) -> bool:
        """检查 date 是否在 code 上市后前 n 个交易日内。

        优化策略：先用首日日期快速排除（如果 date 比首日晚超过30天，
        几乎不可能在前5个交易日内）；否则查具体的前n天列表。
        """
        first_date = self._first_date_map.get(code)
        if first_date is None:
            return True  # 无数据，保守排除
        # 快速排除：如果 date < first_date，不应该发生，保守排除
        if date < first_date:
            return True
        # 快速排除：如果 date 比 first_date 晚超过20天(日历日)，不可能在前5个交易日内
        if date > first_date and len(date) == 10 and len(first_date) == 10:
            # 简单字符串比较：YYYY-MM-DD 格式，差异超过1个月肯定安全
            y1, m1, d1 = int(first_date[:4]), int(first_date[5:7]), int(first_date[8:10])
            y2, m2, d2 = int(date[:4]), int(date[5:7]), int(date[8:10])
            diff_days = (y2 - y1) * 365 + (m2 - m1) * 30 + (d2 - d1)
            if diff_days > 20:
                return False
        # 精确检查：加载前n天
        if code not in self._ipo_cache:
            cur = self._conn.execute(
                "SELECT date FROM stock_kline WHERE code = ? ORDER BY date LIMIT ?",
                (code, n))
            rows = cur.fetchall()
            self._ipo_cache[code] = set(r[0] for r in rows)
        first_dates = self._ipo_cache[code]
        if len(first_dates) < n:
            return True
        return date in first_dates

    def close(self):
        if self._conn:
            self._conn.close()
            self._conn = None


def _f(v) -> float:
    """安全转 float，None/NaN -> 0.0（字符串字段除外，由调用方处理）。"""
    if v is None:
        return 0.0
    try:
        f = float(v)
        if f != f:  # NaN
            return 0.0
        return f
    except (TypeError, ValueError):
        return 0.0
