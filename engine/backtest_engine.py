"""回测引擎V3 - 主循环引擎"""
import sqlite3
import time
import pandas as pd
from .portfolio import Portfolio
from .output_formatter import OutputFormatter


class BacktestEngineV3:
    """模块化N仓位回测引擎"""

    def __init__(self, buy_module, sell_module, blacklist,
                 db_path: str, start_date: str, end_date: str,
                 initial_capital: float = 1_000_000,
                 n_slots: int = 3,
                 output_path: str = None,
                 market_filter_threshold: float = None,
                 index_ma_filter: int = None,
                 regime_index_code: str = 'sh.000001'):
        self.buy_module = buy_module
        self.sell_module = sell_module
        self.blacklist = blacklist
        self.db_path = db_path
        self.start_date = start_date
        self.end_date = end_date
        self.initial_capital = initial_capital
        self.market_filter_threshold = market_filter_threshold
        self.index_ma_filter = index_ma_filter          # 指数MA择时窗口(如20)，None=不启用
        self.regime_index_code = regime_index_code      # 择时基准指数

        self.portfolio = Portfolio(initial_capital, n_slots=n_slots)
        self.output = OutputFormatter(output_path)
        self.output.set_initial_capital(initial_capital)
        self._market_cache = {}  # {date: hour1_close_rate}
        self._regime_cache = {}  # {date: bool} 指数MA择时，True=可买入

    def _load_market_data(self):
        """批量加载上证指数hour1_close_rate到缓存（避免逐日查询拖慢速度）"""
        conn = sqlite3.connect(self.db_path)
        cursor = conn.execute(
            "SELECT date, hour1_close_rate FROM index_kline "
            "WHERE code='sh.000001' AND date BETWEEN ? AND ?",
            (self.start_date, self.end_date)
        )
        for row in cursor.fetchall():
            if row[1] is not None:
                self._market_cache[row[0]] = float(row[1])
        conn.close()
        print(f"大盘数据已加载: {len(self._market_cache)}个交易日")

    def _load_regime_data(self):
        """预计算指数MA择时信号(前向安全)：
        某交易日D是否可买入 = 指数(D-1日收盘) > 指数(D-1日的MA{window})。
        仅使用D日开盘前已知的历史数据(D-1及之前)，无未来函数。"""
        w = int(self.index_ma_filter)
        conn = sqlite3.connect(self.db_path)
        rows = conn.execute(
            "SELECT date, close FROM index_kline WHERE code=? ORDER BY date",
            (self.regime_index_code,)
        ).fetchall()
        conn.close()
        dates = [r[0] for r in rows]
        closes = [float(r[1]) for r in rows]
        ok_days = 0
        for i in range(len(dates)):
            if i < w + 1:
                self._regime_cache[dates[i]] = True  # 历史不足，默认放行
                continue
            prev_close = closes[i - 1]
            prev_ma = sum(closes[i - 1 - w:i - 1]) / w
            ok = prev_close > prev_ma
            self._regime_cache[dates[i]] = ok
            if ok:
                ok_days += 1
        print(f"指数MA{w}择时已加载: {self.regime_index_code}, "
              f"可买入日占比 {ok_days}/{len(dates)}")

    def run(self):
        """执行回测主循环"""
        t_start = time.time()
        trading_dates = self._get_trading_dates()
        total_days = len(trading_dates)
        print(f"交易日总数: {total_days} ({self.start_date} ~ {self.end_date})")

        # 加载大盘数据缓存（仅在设置了market_filter_threshold时使用）
        if self.market_filter_threshold is not None:
            self._load_market_data()
            print(f"大盘过滤阈值: hour1_close_rate >= {self.market_filter_threshold}%")

        # 加载指数MA择时信号
        if self.index_ma_filter is not None:
            self._load_regime_data()

        prev_day_data = None  # 上一个交易日的数据
        prev_trading_date = None  # 上一交易日的日期（用于候选日志标题）
        # 是否启用候选股日志（仅当buy_module实现了describe_candidate时）
        self._log_candidates = hasattr(self.buy_module, 'describe_candidate')
        buy_hour_attr = int(getattr(self.buy_module, 'buy_hour', 1) or 1)

        for i, date in enumerate(trading_dates):
            # 加载今日全市场数据
            day_data = self._load_day_data(date)
            if day_data is None or day_data.empty:
                continue

            # 更新持仓的hold_trading_days（今日是新的一天，昨日及之前买入的+1）
            for slot in self.portfolio.occupied_slots():
                if slot.buy_date and slot.buy_date < date:
                    slot.hold_trading_days += 1

            # 调用buy_module的on_day_start钩子（如果有）
            if hasattr(self.buy_module, 'on_day_start'):
                self.buy_module.on_day_start(date, day_data)

            # 获取候选股票（开盘时即可确定）
            candidates = self.buy_module.get_candidates(date, day_data, prev_day_data, self.blacklist)

            trades_today = []

            # 追踪本日刚买入的slot（用于区分"买入"和"持有"显示）
            just_bought_slots = set()

            for hour in [1, 2, 3, 4]:
                hour_data = self._extract_hour_data(day_data, hour)

                # 1. 卖出检查 (T+1合规: buy_date < date)
                for slot in self.portfolio.occupied_slots():
                    if slot.buy_date and slot.buy_date < date:
                        if self.sell_module.should_sell(slot, date, hour, hour_data):
                            sell_price = self._get_hour_close(hour_data, slot.code, hour)
                            if sell_price and sell_price > 0:
                                trade_record = self.portfolio.execute_sell(slot, sell_price, date, hour)
                                trades_today.append(('sell', slot, trade_record))
                                self.output.print_trade('sell', slot, trade_record, date, hour)
                                just_bought_slots.discard(slot.slot_id)

                # 1.5 在buy_hour且买入模块支持描述时，打印候选股列表
                if (self._log_candidates and hour == buy_hour_attr and candidates):
                    market_ok = True
                    market_rate = None
                    if self.market_filter_threshold is not None:
                        market_rate = self._market_cache.get(date)
                        if market_rate is None or market_rate < self.market_filter_threshold:
                            market_ok = False
                    selected_code = None
                    if market_ok and self.portfolio.empty_slots():
                        try:
                            preview = self.buy_module.should_buy(
                                candidates, date, hour, hour_data,
                                self.portfolio, day_data)
                            if preview:
                                selected_code = preview.get('code')
                        except Exception:
                            selected_code = None
                    sig_date = prev_trading_date if prev_trading_date else date
                    self.output.print_candidates(
                        sig_date, candidates, self.buy_module,
                        selected_code=selected_code,
                        market_ok=market_ok,
                        market_threshold=self.market_filter_threshold,
                        market_rate=market_rate,
                        buy_hour=buy_hour_attr,
                    )

                # 2. 买入检查
                hour_bought_ids = set()
                # 大盘环境过滤：如果设置了阈值且当日大盘不满足条件，跳过买入
                market_ok = True
                if self.market_filter_threshold is not None:
                    mkt_rate = self._market_cache.get(date)
                    if mkt_rate is None or mkt_rate < self.market_filter_threshold:
                        market_ok = False
                # 指数MA择时过滤：非上升趋势不买入
                if self.index_ma_filter is not None and not self._regime_cache.get(date, True):
                    market_ok = False

                for slot in self.portfolio.empty_slots():
                    if not market_ok:
                        break
                    signal = self.buy_module.should_buy(candidates, date, hour, hour_data, self.portfolio, day_data)
                    if signal:
                        self.portfolio.execute_buy(slot, signal['code'], signal['code_name'],
                                                   signal['price'], date, hour)
                        # 把策略元信息写入slot(供PerSlotSellStrategy和交易明细导出使用)
                        slot.strategy_name = signal.get('strategy_name', '')
                        slot.target_hold_days = signal.get('target_hold_days', 1)
                        slot.buy_reason = signal.get('buy_reason', '')
                        trades_today.append(('buy', slot, signal))
                        self.output.print_trade('buy', slot, signal, date, hour)
                        # 从candidates中移除已买入的股票
                        candidates = [c for c in candidates if c['code'] != signal['code']]
                        just_bought_slots.add(slot.slot_id)
                        hour_bought_ids.add(slot.slot_id)

                # 3. Hour摘要 - 传入刚买入的slot信息用于区分显示
                self._update_slot_prices(hour_data)
                self.output.print_hour_summary(date, hour, self.portfolio, hour_data,
                                               just_bought_ids=hour_bought_ids)

            # 日终摘要
            self.output.print_daily_summary(date, self.portfolio, trades_today, i)

            # 保存今日数据作为下一交易日的prev_data
            prev_day_data = day_data
            prev_trading_date = date

        # 最终统计
        self.output.print_final_summary(self.portfolio)
        self.output.close()

        elapsed = time.time() - t_start
        print(f"\n回测耗时: {elapsed:.1f}秒 ({elapsed / 60:.1f}分钟)")

    def _get_trading_dates(self) -> list:
        """从数据库获取交易日列表"""
        conn = sqlite3.connect(self.db_path)
        cursor = conn.execute(
            "SELECT DISTINCT date FROM stock_kline WHERE date BETWEEN ? AND ? ORDER BY date",
            (self.start_date, self.end_date)
        )
        dates = [r[0] for r in cursor.fetchall()]
        conn.close()
        return dates

    def _load_day_data(self, date: str) -> pd.DataFrame:
        """加载某日全市场数据（逐日查询，避免OOM）"""
        conn = sqlite3.connect(self.db_path)
        df = pd.read_sql(
            "SELECT * FROM stock_kline WHERE date = ?", conn, params=(date,))
        conn.close()
        return df

    def _extract_hour_data(self, day_data: pd.DataFrame, hour: int) -> dict:
        """
        从日数据中提取特定hour的OHLC等（向量化实现）
        返回: {code: {open, close, high, low, volume, amount, close_rate, open_rate, high_rate, low_rate, preclose}}
        """
        prefix = f'hour{hour}_'
        result = {}

        # 需要的字段映射
        field_keys = ['open', 'close', 'high', 'low', 'volume', 'amount',
                      'close_rate', 'open_rate', 'high_rate', 'low_rate']
        db_cols = [f'{prefix}{k}' for k in field_keys]

        # 检查哪些列存在
        existing = [c for c in db_cols if c in day_data.columns]
        if not existing:
            return result

        # 过滤有效close的行（向量化）
        close_col = f'{prefix}close'
        if close_col not in day_data.columns:
            return result

        mask = day_data[close_col].notna() & (day_data[close_col] > 0)
        valid_df = day_data.loc[mask]
        if valid_df.empty:
            return result

        # 提取数据（向量化）
        codes = valid_df['code'].values
        data_arrays = {}
        for db_col, key in zip(db_cols, field_keys):
            if db_col in valid_df.columns:
                arr = valid_df[db_col].fillna(0).values
                data_arrays[key] = arr

        # 额外提取preclose用于rate计算
        preclose_arr = None
        if 'preclose' in valid_df.columns:
            preclose_arr = valid_df['preclose'].fillna(0).values

        for i, code in enumerate(codes):
            hour_info = {key: float(arr[i]) for key, arr in data_arrays.items()}
            if preclose_arr is not None:
                hour_info['preclose'] = float(preclose_arr[i])
            result[code] = hour_info

        return result

    def _get_hour_close(self, hour_data: dict, code: str, hour: int) -> float:
        """获取特定股票在特定hour的close价"""
        stock_h = hour_data.get(code)
        if stock_h:
            return stock_h.get('close', 0)
        return 0

    def _update_slot_prices(self, hour_data: dict):
        """用最新hour数据更新持仓市值（用于NAV计算）"""
        # NAV在日终摘要中计算，这里不需要额外操作
        pass
