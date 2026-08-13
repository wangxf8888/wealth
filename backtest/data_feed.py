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

import trading_rules
import execution_core

# hour 级字段后缀（不含 open，open 单独处理）
_HOUR_FIELDS = ['open', 'open_rate', 'high', 'high_rate', 'low', 'low_rate',
                'close', 'close_rate', 'volume', 'amount']
# 日级 / 前日字段
_DAY_FIELDS = ['open', 'open_rate', 'high', 'low', 'close', 'close_rate',
               'volume', 'amount', 'turn', 'preclose']

# 分钟级5min bar数据源 (Task#24, 只读)
_MINUTE_DB_PATH = '/home/AIWealth/data/minute.db'
# hour -> 该小时12根5min bar的time区间(bar结束时刻'HHMM', 与minute.db口径一致)
_MINUTE_HOUR_WINDOW = {1: ('0935', '1030'), 2: ('1035', '1130'),
                       3: ('1305', '1400'), 4: ('1405', '1500')}
# bar结束时刻 -> 当日序号0..47 (Task#54 bar流裁剪用, 与execution_core同源)
_BAR_INDEX = {t: i for i, t in enumerate(execution_core.BAR_TIMES)}


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
        # T-1涨跌比+成交额风控预计算缓存（懒加载，仅在调用get_market_health时初始化）
        self._adv_dec_ratio_cache = None
        self._index_amount_cache = None
        # 分钟级5min bar (Task#24): 只读连接懒加载 + (code,date)级LRU缓存 + 缺口计数
        self._minute_conn = None
        self._minute_cache = {}          # {(code,date): {hour: [(time,o,h,l,c)...]}}
        self._minute_cache_order = []    # LRU 顺序
        self._minute_cache_max = 8192    # 约8k股·日, 每项48根bar, 内存可控
        self._minute_map_cache = {}      # {(code,date): {time: bar}} (Task#54)
        self._minute_hit = 0             # 命中(拿到完整12根bar)的hour数
        self._minute_gap = 0             # 缺口(fallback hour级)的hour数
        self._minute_gap_samples = []    # 前20个缺口样本(code,date,hour,n_bars)

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
    # 涨跌停价与可执行性判定（实盘可执行性守卫）
    # ------------------------------------------------------------------
    @staticmethod
    def _limit_ratio(code: str, is_st: bool = False) -> float:
        """按板块返回涨跌幅限制比例（规则源: trading_rules模块）。"""
        return trading_rules.limit_ratio(code, is_st)

    def get_limit_prices(self, code: str, date: str) -> tuple:
        """返回 (涨停价, 跌停价)，基于当日 preclose 与板块规则。

        无数据时返回 (0.0, 0.0)，调用方应视为无法判定（不拦截）。
        """
        today = self._load_day(date)
        if today is None or today.empty or code not in today.index:
            return 0.0, 0.0
        row = today.loc[code]
        preclose = _f(row.get('preclose'))
        is_st = bool(int(row.get('isST', 0) or 0))
        return trading_rules.limit_prices(code, preclose, is_st)

    def is_st_day(self, code: str, date: str) -> bool:
        """当日isST标记(库字段, 与get_limit_prices同源)。缺数据返回False。"""
        today = self._load_day(date)
        if today is None or today.empty or code not in today.index:
            return False
        return bool(int(today.loc[code].get('isST', 0) or 0))

    def is_buy_blocked_limit_up(self, code: str, date: str, hour: int) -> bool:
        """买入可执行性: 当前 hour open 已达涨停价 → 实盘挂单大概率排队买不到。"""
        limit_up, _ = self.get_limit_prices(code, date)
        if limit_up <= 0:
            return False
        price = self.get_hour_open(code, date, hour)
        if price <= 0:
            return False
        return price >= limit_up - 0.001

    def is_sell_blocked_limit_down(self, code: str, date: str, hour: int) -> bool:
        """卖出可执行性: 该 hour 全程封死跌停(high==跌停价) → 卖单排队无法成交。

        仅拦截"整小时封死"的情况；盘中砸到跌停又打开的，视为止损单可成交。
        """
        _, limit_down = self.get_limit_prices(code, date)
        if limit_down <= 0:
            return False
        h_high = self.get_hour_high(code, date, hour)
        if h_high <= 0:
            return False
        return h_high <= limit_down + 0.001

    def is_ex_dividend_day(self, code: str, date: str) -> bool:
        """除权除息日判定(Task#4): 当日交易所preclose != 前一交易日close。

        库为不复权价, 除权除息日preclose被交易所调整为除权参考价 →
        当日open_rate语义失真(小比例分红产生-1%~-3%假"低开"), 引擎买入
        检查据此拦截该日开盘买入。缺数据时返回False(无法判定, 不拦截)。
        规则源: trading_rules.is_ex_dividend_gap(容差0.2%排除厘位舍入)。
        """
        preclose = self.get_day_preclose(code, date)
        prev = self.get_prev_day(code, date)
        return trading_rules.is_ex_dividend_gap(preclose, prev.get('close'))

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

    # ------------------------------------------------------------------
    # T-1 涨跌比 + 成交额MA20 市场健康度风控
    # ------------------------------------------------------------------
    def _load_adv_dec_ratios(self) -> dict:
        """一次性预计算所有交易日的涨跌比(上涨家数/下跌家数)。
        排除停牌股(close=preclose且volume=0)。"""
        cur = self._conn.execute(
            "SELECT date, "
            "SUM(CASE WHEN close > preclose THEN 1 ELSE 0 END) as up_count, "
            "SUM(CASE WHEN close < preclose THEN 1 ELSE 0 END) as down_count "
            "FROM stock_kline WHERE volume > 0 "
            "GROUP BY date")
        result = {}
        for row in cur.fetchall():
            date, up, down = row[0], row[1] or 0, row[2] or 0
            if down > 0:
                result[date] = up / down
            else:
                result[date] = 999.0  # 无下跌股，视为极强
        return result

    def _load_index_amounts(self) -> dict:
        """一次性加载上证指数所有日期的成交额。"""
        cur = self._conn.execute(
            "SELECT date, amount FROM index_kline WHERE code='sh.000001'")
        return {row[0]: row[1] for row in cur.fetchall() if row[1] is not None}

    def get_market_health(self, date: str) -> tuple:
        """计算T-1日的市场健康度（用于决定T日是否允许买入）。

        使用T-1日数据决定T日操作，严格时序合规。

        规则:
        - T-1涨跌比 < 0.3                     → 暂停所有买入 (0.0)
        - T-1涨跌比 0.3~0.5 且 成交额<MA20×80% → 暂停买入 (0.0)
        - T-1涨跌比 0.5~0.8 且 成交额<MA20×80% → 缩减50% (0.5)
        - T-1涨跌比 ≥ 0.8                     → 正常交易 (1.0)

        Returns:
            (buy_ratio, reason): buy_ratio=0.0/0.5/1.0, reason=说明文字
        """
        # 懒加载缓存
        if self._adv_dec_ratio_cache is None:
            self._adv_dec_ratio_cache = self._load_adv_dec_ratios()
        if self._index_amount_cache is None:
            self._index_amount_cache = self._load_index_amounts()
        prev_date = self._prev_trading_date(date)
        if prev_date is None:
            return 1.0, "no_prev_date"

        # 1. T-1日涨跌比
        adv_dec_ratio = self._adv_dec_ratio_cache.get(prev_date)
        if adv_dec_ratio is None:
            return 1.0, "no_adv_dec_data"

        # 2. T-1日成交额 vs MA20基准
        prev_amount = self._index_amount_cache.get(prev_date, 0)

        # 计算MA20：prev_date前20个交易日的成交额均值
        prev_idx = self._date_index.get(prev_date)
        if prev_idx is None:
            return 1.0, "prev_date_not_in_index"

        # 取prev_date之前的20个交易日（不含prev_date本身）
        start_idx = max(0, prev_idx - 20)
        ma20_dates = self._trading_dates[start_idx:prev_idx]
        if len(ma20_dates) < 10:
            # 数据不足，不过滤
            return 1.0, "insufficient_ma20_data"

        amounts = [self._index_amount_cache.get(d, 0) for d in ma20_dates]
        amounts = [a for a in amounts if a > 0]
        if not amounts:
            return 1.0, "no_amount_data"

        ma20 = sum(amounts) / len(amounts)
        is_shrink = prev_amount < ma20 * 0.8

        # 3. 应用规则
        if adv_dec_ratio < 0.3:
            return 0.0, f"adv_dec={adv_dec_ratio:.2f}<0.3"
        elif adv_dec_ratio < 0.5 and is_shrink:
            return 0.0, f"adv_dec={adv_dec_ratio:.2f}<0.5&shrink({prev_amount/1e8:.0f}亿<MA20*0.8={ma20*0.8/1e8:.0f}亿)"
        elif adv_dec_ratio < 0.8 and is_shrink:
            return 0.5, f"adv_dec={adv_dec_ratio:.2f}<0.8&shrink({prev_amount/1e8:.0f}亿<MA20*0.8={ma20*0.8/1e8:.0f}亿)"
        else:
            return 1.0, f"healthy(adv_dec={adv_dec_ratio:.2f})"

    # ------------------------------------------------------------------
    # 分钟级5min bar (Task#24: 卖出触线分钟级精化)
    # ------------------------------------------------------------------
    def _minute_connect(self):
        """minute.db 只读连接懒加载(URI mode=ro, 防误写; 连接全程复用)。"""
        if self._minute_conn is None:
            self._minute_conn = sqlite3.connect(
                f"file:{_MINUTE_DB_PATH}?mode=ro", uri=True)
        return self._minute_conn

    def _load_minute_day(self, code: str, date: str) -> dict:
        """加载某股某日全部5min bar并按hour分组, (code,date)级LRU缓存。"""
        key = (code, date)
        if key in self._minute_cache:
            return self._minute_cache[key]
        cur = self._minute_connect().execute(
            "SELECT time, open, high, low, close FROM minute_kline "
            "WHERE code=? AND date=? ORDER BY time", (code, date))
        by_hour = {1: [], 2: [], 3: [], 4: []}
        for row in cur.fetchall():
            t = row[0]
            for h, (t_lo, t_hi) in _MINUTE_HOUR_WINDOW.items():
                if t_lo <= t <= t_hi:
                    by_hour[h].append(row)
                    break
        self._minute_cache[key] = by_hour
        self._minute_cache_order.append(key)
        if len(self._minute_cache_order) > self._minute_cache_max:
            old = self._minute_cache_order.pop(0)
            self._minute_cache.pop(old, None)
            self._minute_map_cache.pop(old, None)
        return by_hour

    def get_minute_bars(self, code: str, date: str, hour: int):
        """返回某股某日某hour的12根5min bar [(time,o,h,l,c)...], 按time升序。

        缺数据(不足12根)返回 None 并计入缺口统计(调用方fallback hour级路径,
        回测结束打印"分钟数据缺口告警", 防静默偏差)。
        """
        by_hour = self._load_minute_day(code, date)
        bars = by_hour.get(hour) or []
        if len(bars) != 12:
            self._minute_gap += 1
            if len(self._minute_gap_samples) < 20:
                self._minute_gap_samples.append((code, date, hour, len(bars)))
            return None
        self._minute_hit += 1
        return bars

    # ------------------------------------------------------------------
    # Task#54: bar流时刻裁剪(5min级策略数据通道, 未来数据在数据层封死)
    # ------------------------------------------------------------------
    def _minute_day_map(self, code: str, date: str) -> dict:
        """当日全部5min bar的 {time: (time,o,h,l,c)} 映射, 与_minute_cache同评期。"""
        key = (code, date)
        m = self._minute_map_cache.get(key)
        if m is None:
            by_hour = self._load_minute_day(code, date)
            m = {}
            for h in (1, 2, 3, 4):
                for row in by_hour.get(h) or []:
                    m[row[0]] = row
            self._minute_map_cache[key] = m
        return m

    def get_bars_until(self, code: str, date: str, time_end: str):
        """bar流时刻裁剪(Task#54): bar N(结束时刻time_end)开始时点的可见数据。

        返回 (prior_bars, bar_open):
          prior_bars: 当日bar N之前全部已走完的bar [(time,o,h,l,c)...] 升序;
          bar_open:   bar N的开盘价(bar开始时点即确定; 该bar缺失→0.0)。
        bar N自身的high/low/close及之后的bar从数据层封死(未来数据), 与
        hour级"只见hour-1及更早完整数据+当前hour开盘"裁剪同哲学。
        prior覆盖不完整(中途缺bar) → (None, 0.0) 并计入缺口统计。
        """
        idx = _BAR_INDEX.get(time_end)
        if idx is None:
            raise ValueError(f"非法5min bar时刻: {time_end!r} (合法: BAR_TIMES)")
        m = self._minute_day_map(code, date)
        prior = []
        for t in execution_core.BAR_TIMES[:idx]:
            row = m.get(t)
            if row is None:
                self._minute_gap += 1
                if len(self._minute_gap_samples) < 20:
                    self._minute_gap_samples.append(
                        (code, date, t, len(m)))
                return None, 0.0
            prior.append(row)
        self._minute_hit += 1
        cur = m.get(time_end)
        return prior, (_f(cur[1]) if cur is not None else 0.0)

    def get_bar_at(self, code: str, date: str, time_end: str):
        """某股某日单根5min bar (time,o,h,l,c)。缺失→None(调用方降级hour级)。

        ⚠️含该bar的high/low/close(bar走完才确定), 仅限引擎成交价钳制等
        执行模型场景使用; 策略决策数据一律走get_bars_until(时刻裁剪)。
        """
        if time_end not in _BAR_INDEX:
            raise ValueError(f"非法5min bar时刻: {time_end!r} (合法: BAR_TIMES)")
        return self._minute_day_map(code, date).get(time_end)

    def minute_coverage_report(self) -> str:
        """分钟数据命中/缺口统计(回测结束时打印, 命中率<100%即告警)。"""
        total = self._minute_hit + self._minute_gap
        if total == 0:
            return "分钟路径未启用(0次查询)"
        rate = self._minute_hit / total * 100
        msg = (f"分钟bar查询: {total}次 | 命中: {self._minute_hit} | "
               f"缺口fallback: {self._minute_gap} | 命中率: {rate:.2f}%")
        if self._minute_gap:
            samples = ', '.join(f"{c} {d} h{h}({n}根)"
                                for c, d, h, n in self._minute_gap_samples)
            msg += f"\n[分钟数据缺口告警] 样本(前{len(self._minute_gap_samples)}): {samples}"
        return msg

    def close(self):
        if self._conn:
            self._conn.close()
            self._conn = None
        if self._minute_conn:
            self._minute_conn.close()
            self._minute_conn = None


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
