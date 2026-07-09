"""放量突破买入策略 (VolBreakout) - P0优化版

P0优化内容 (2026-05-21):
    1. 候选排序改为 vol_ratio ASC (选4-5x温和放量，避开>10x投机品种)
    2. vol_ratio 过滤到 3~10 区间 (去掉极端值)
    3. 首板限定: D-1涨停但D-2未涨停 (排除连板股)
    4. 跳空过滤: D日 hour1_open 相对 D-1 close 高开幅度 < gap_up_max(默认5%)
    5. 大盘择时复合过滤器 (Jay研究最优):
       - red_ratio_prev >= 45% (昨日红盘比例)
       - 指数在MA10之上 (index_ma10_above)
       - 5日累计涨幅 > -2% (cum5d > -2)
       - 今日大盘hour1跌幅 < 1% (hour1_index_close_rate > -1%)

策略逻辑:
    信号识别基于昨日(D-1)的真实可观测数据, 今日(D)以 hour1_open 价格执行买入,
    次日(D+1) hour1_close 卖出, 持有期内任一 low <= buy_price*(1-stop_loss_pct)
    触发硬止损 (止损由 sell_module 处理).

核心条件 (基于 D-1 日数据, 无未来数据):
    - turn(D-1) > avg(turn[D-6..D-2]) * vol_mul        # 放量
    - close(D-1) > max(close[D-31..D-2])               # 创30日新高
    - close_rate(D-1) >= min_close_rate                # 涨幅阈值
    - amount(D-1) > min_amount                         # 流动性
    - D-1 涨停且 D-2 未涨停 (首板限定)
    - vol_ratio 在 vol_ratio_min ~ vol_ratio_max 之间
    - 排除 ST/北交所/isST=1/一字板

大盘过滤 (复合条件, 在 should_buy 中执行):
    - red_ratio(D-1) >= red_ratio_min                  # 普涨情绪
    - close(D-1) > MA10(D-1)                           # 均线之上
    - sum(close_rate[D-5..D-1]) > cum5d_min            # 大盘5日累计
    - hour1_close_rate(D) > hour1_min                  # 当日大盘hour1情绪

执行:
    - get_candidates: 基于 prev_data + history (D-2及之前) 筛选
    - should_buy: 在 buy_hour=1 用 hour1_open 价格买入, 跳空+大盘复合过滤
    - 持仓与止损由 StopLossSellStrategy 配合 (sell_hour=1, stop_loss_pct=0.05)
"""
from collections import deque
from typing import List, Optional
import sqlite3
import pandas as pd
from .buy_module import BuyModule


class VolBreakoutBuyStrategy(BuyModule):
    """放量突破P0优化版 - vol_ratio排序 + 首板限定 + 跳空过滤 + 大盘复合择时"""

    def __init__(self,
                 vol_mul: float = 4.0,
                 high_period: int = 30,
                 min_close_rate: float = 9.0,
                 min_amount: float = 100_000_000,
                 buy_hour: int = 1,
                 stop_loss_pct: float = 0.05,
                 # === P0最优: 大盘过滤参数 ===
                 use_market_filter: bool = True,
                 red_ratio_min: float = 50.0,       # P0最优: 50
                 cum5d_min: float = 0.0,             # P0最优: 0
                 hour1_index_min: float = -1.0,      # P0最优: h1>-1%
                 use_ma10_filter: bool = False,      # P0最优: 不用MA10
                 # === P0最优: 跳空过滤参数 ===
                 gap_up_max: float = 3.0,            # P0最优: <3% (关键改进)
                 # === P0最优: vol_ratio区间 ===
                 vol_ratio_min: float = 3.0,         # vol_ratio下限
                 vol_ratio_max: float = 10.0,        # vol_ratio上限
                 # === P0: 首板限定 ===
                 first_board_only: bool = False,     # P0最优: 不限首板
                 # === 通用 ===
                 db_path: str = None,
                 preload_start_date: str = None,
                 # 兼容旧参数
                 min_rate: float = None,
                 min_confirm_rate: float = None):
        # 兼容老的 min_rate 参数
        if min_rate is not None:
            min_close_rate = float(min_rate)

        self.vol_mul = vol_mul
        self.high_period = high_period
        self.min_close_rate = min_close_rate
        self.min_amount = min_amount
        self.buy_hour = buy_hour
        self.stop_loss_pct = stop_loss_pct
        self.use_market_filter = use_market_filter
        self.red_ratio_min = red_ratio_min
        self.cum5d_min = cum5d_min
        self.hour1_index_min = hour1_index_min
        self.use_ma10_filter = use_ma10_filter
        self.gap_up_max = gap_up_max
        self.vol_ratio_min = vol_ratio_min
        self.vol_ratio_max = vol_ratio_max
        self.first_board_only = first_board_only
        self.db_path = db_path

        # 维护每只股票最近 N+2 日的 (close, turn, preclose) 历史:
        # 检查 D-1 信号时, 需要 D-31..D-2 (30日新高) 与 D-6..D-2 (5日均换手).
        # 首板限定需要 D-2 的 close/preclose.
        # on_day_start(D) 之后, history 末尾 = D, 倒数第二 = D-1.
        # 因此 maxlen = high_period + 2.
        self._maxlen = high_period + 2
        self.history = {}  # {code: deque(maxlen=N+2)}

        # 大盘指标缓存 {date: {red_ratio, cum5d, close, ma10, hour1_close_rate}}
        self.market_cache = {}
        if self.use_market_filter and self.db_path:
            self._load_market_data()

        # 预加载历史数据 (避免引擎前 N 日空跑无信号)
        if self.db_path and preload_start_date:
            self._preload_history(preload_start_date)

    # ------------------------------------------------------------------
    # 大盘数据加载 (P0增强: 加载close/MA10/hour1_close_rate)
    # ------------------------------------------------------------------
    def _load_market_data(self):
        """从数据库加载上证指数 red_ratio, 5日累计close_rate, close, MA10, hour1_close_rate"""
        try:
            conn = sqlite3.connect(self.db_path)
            df = pd.read_sql(
                "SELECT date, close, close_rate, red_ratio, hour1_close_rate "
                "FROM index_kline "
                "WHERE code='sh.000001' ORDER BY date", conn)
            conn.close()
        except Exception as e:
            print(f"[VolBreakout] 加载大盘数据失败: {e}")
            return

        if df.empty:
            return
        df["close"] = pd.to_numeric(df["close"], errors="coerce").fillna(0.0)
        df["close_rate"] = pd.to_numeric(df["close_rate"], errors="coerce").fillna(0.0)
        df["red_ratio"] = pd.to_numeric(df["red_ratio"], errors="coerce")
        df["hour1_close_rate"] = pd.to_numeric(df["hour1_close_rate"], errors="coerce")

        # cum5d 表示 D 行的 D-4..D 共5日累计涨幅
        df["cum5d"] = df["close_rate"].rolling(5).sum()
        # MA10
        df["ma10"] = df["close"].rolling(10, min_periods=10).mean()

        for _, row in df.iterrows():
            d = row["date"]
            self.market_cache[d] = {
                "red_ratio": row["red_ratio"] if pd.notna(row["red_ratio"]) else None,
                "cum5d": row["cum5d"] if pd.notna(row["cum5d"]) else None,
                "close": row["close"] if pd.notna(row["close"]) else None,
                "ma10": row["ma10"] if pd.notna(row["ma10"]) else None,
                "hour1_close_rate": row["hour1_close_rate"] if pd.notna(row["hour1_close_rate"]) else None,
            }
        print(f"[VolBreakout] 大盘指标缓存: {len(self.market_cache)}天 (含close/MA10/hour1)")

    def _preload_history(self, start_date: str):
        """在引擎 start_date 前加载 N+2+5=37 自然日 (~35 交易日) 的历史数据,
        用于第一天即可产生信号 (避免前 ~35 日空跑).
        P0增强: 同时加载 preclose 用于首板限定涨停判定."""
        try:
            conn = sqlite3.connect(self.db_path)
            # 留 70 自然日缓冲, 保证至少 32 个交易日
            df = pd.read_sql(
                "SELECT date, code, close, turn, preclose FROM stock_kline "
                "WHERE date < ? AND date >= date(?, '-70 day') "
                "AND code NOT LIKE 'bj.%' "
                "ORDER BY date, code",
                conn, params=(start_date, start_date))
            conn.close()
        except Exception as e:
            print(f"[VolBreakout] 历史预加载失败: {e}")
            return
        if df.empty:
            return
        df["close"] = pd.to_numeric(df["close"], errors="coerce").fillna(0.0)
        df["turn"] = pd.to_numeric(df["turn"], errors="coerce").fillna(0.0)
        df["preclose"] = pd.to_numeric(df["preclose"], errors="coerce").fillna(0.0)
        # 按日期分组按顺序 append, 保持 deque 时序
        for d, sub in df.groupby("date"):
            for code, c, t, pc in zip(sub["code"].values, sub["close"].values,
                                       sub["turn"].values, sub["preclose"].values):
                if code not in self.history:
                    self.history[code] = deque(maxlen=self._maxlen)
                self.history[code].append({
                    "close": float(c),
                    "turn": float(t),
                    "preclose": float(pc),
                })
        print(f"[VolBreakout] 预加载历史: {len(self.history)}只股票, "
              f"日期窗口 {df['date'].min()}~{df['date'].max()}")

    # ------------------------------------------------------------------
    # 引擎钩子
    # ------------------------------------------------------------------
    def on_day_start(self, date: str, day_data: pd.DataFrame):
        """每日开始时将当天数据加入 history 末尾 (P0: 含preclose)"""
        if day_data is None or day_data.empty:
            return
        codes = day_data["code"].values
        closes = pd.to_numeric(day_data["close"], errors="coerce").fillna(0).values
        turns = pd.to_numeric(day_data.get("turn", pd.Series(dtype=float)),
                              errors="coerce").fillna(0).values
        precloses = pd.to_numeric(day_data.get("preclose", pd.Series(dtype=float)),
                                  errors="coerce").fillna(0).values
        for i in range(len(codes)):
            code = codes[i]
            if code not in self.history:
                self.history[code] = deque(maxlen=self._maxlen)
            self.history[code].append({
                "close": float(closes[i]),
                "turn": float(turns[i]),
                "preclose": float(precloses[i]),
            })

    # ------------------------------------------------------------------
    # 涨停判定辅助
    # ------------------------------------------------------------------
    @staticmethod
    def _is_limit_up(close: float, preclose: float, code: str = "") -> bool:
        """判断是否涨停: round(close/preclose, 2) >= 阈值
        主板(sh.6/sz.0): 10%, 创业板(sz.3)/科创板(sh.68): 20%"""
        if preclose <= 0 or close <= 0:
            return False
        ratio = round(close / preclose, 2)
        code_lower = code.lower() if code else ""
        if code_lower.startswith("sz.3") or code_lower.startswith("sh.68"):
            return ratio >= 1.20
        return ratio >= 1.10

    # ------------------------------------------------------------------
    # 候选股筛选 (信号在 D-1, prev_data) [P0优化]
    # ------------------------------------------------------------------
    def get_candidates(self, date: str, day_data: pd.DataFrame,
                       prev_data: pd.DataFrame, blacklist) -> List[dict]:
        """
        基于昨日 (D-1, prev_data) 数据筛选放量突破候选:
            1. close_rate(D-1) >= min_close_rate
            2. amount(D-1) > min_amount
            3. turn(D-1) > avg_turn(D-6..D-2) * vol_mul
            4. close(D-1) > max_close(D-31..D-2)
            5. 排除 ST/北交所/isST/一字板
            P0新增:
            6. vol_ratio 在 [vol_ratio_min, vol_ratio_max] 区间
            7. 首板限定: D-1涨停 且 D-2未涨停
            8. 排序: vol_ratio ASC (温和放量优先)
        注意: 大盘过滤已移至 should_buy (需要hour1数据)
        """
        if prev_data is None or prev_data.empty:
            return []

        # P0: 大盘基础过滤仍保留 red_ratio + cum5d (这些在get_candidates时可判断)
        if self.use_market_filter:
            d_prev = prev_data["date"].iloc[0]
            mkt = self.market_cache.get(d_prev)
            if mkt is None:
                return []
            rr = mkt.get("red_ratio")
            cum5 = mkt.get("cum5d")
            if rr is None or rr < self.red_ratio_min:
                return []
            if cum5 is None or cum5 < self.cum5d_min:
                return []
            # P0: MA10过滤 (D-1 close > MA10)
            if self.use_ma10_filter:
                mkt_close = mkt.get("close")
                mkt_ma10 = mkt.get("ma10")
                if mkt_close is not None and mkt_ma10 is not None:
                    if mkt_close <= mkt_ma10:
                        return []

        df = prev_data.copy()

        # ST 过滤
        if "isST" in df.columns:
            df = df[df["isST"].fillna(0).astype(int) != 1]
        if "code_name" in df.columns:
            st_mask = df["code_name"].astype(str).str.upper().str.contains("ST", na=False)
            df = df[~st_mask]
        # 北交所
        df = df[~df["code"].astype(str).str.lower().str.startswith("bj.", na=False)]
        if df.empty:
            return []

        # 数值化
        df["close_rate"] = pd.to_numeric(df["close_rate"], errors="coerce").fillna(0)
        df["amount"] = pd.to_numeric(df["amount"], errors="coerce").fillna(0)
        df["turn"] = pd.to_numeric(df["turn"], errors="coerce").fillna(0)
        df["close"] = pd.to_numeric(df["close"], errors="coerce").fillna(0)
        df["open"] = pd.to_numeric(df["open"], errors="coerce").fillna(0)
        df["high"] = pd.to_numeric(df["high"], errors="coerce").fillna(0)
        df["low"] = pd.to_numeric(df["low"], errors="coerce").fillna(0)
        df["preclose"] = pd.to_numeric(df["preclose"], errors="coerce").fillna(0)

        # 一字板 (D-1) 排除: O==H==L==C 且 close_rate>5
        oneword = ((df["open"] == df["high"]) & (df["high"] == df["low"]) &
                   (df["low"] == df["close"]) & (df["close_rate"] > 5))
        df = df[~oneword]

        # 基础过滤
        df = df[(df["close_rate"] >= self.min_close_rate) &
                (df["amount"] > self.min_amount) &
                (df["turn"] > 0) &
                (df["close"] > 0)]
        if df.empty:
            return []

        # 逐股校验历史 (放量+创新高+首板限定)
        # on_day_start(D) 之后, history 末尾=D, 倒数第二=D-1
        candidates = []
        for _, row in df.iterrows():
            code = row["code"]
            hist = self.history.get(code)
            if not hist or len(hist) < self.high_period + 2:
                continue

            hist_list = list(hist)
            n = len(hist_list)

            # D-1 真实值
            d_minus_1 = hist_list[n - 2]
            d1_close = d_minus_1["close"]
            d1_turn = d_minus_1["turn"]
            d1_preclose = d_minus_1.get("preclose", 0)

            # 5日均换手 (D-6..D-2 → 索引 n-7..n-3)
            turn_window = [hist_list[j]["turn"] for j in range(n - 7, n - 2)]
            if len(turn_window) < 5:
                continue
            avg_turn_5 = sum(turn_window) / 5.0
            if avg_turn_5 <= 0:
                continue
            if d1_turn <= avg_turn_5 * self.vol_mul:
                continue

            vol_ratio = d1_turn / avg_turn_5

            # P0: vol_ratio 区间过滤
            if vol_ratio < self.vol_ratio_min or vol_ratio > self.vol_ratio_max:
                continue

            # 30日最高 close (D-31..D-2 → 索引 n-32..n-3, 共30个)
            close_window = [hist_list[j]["close"] for j in range(n - 32, n - 2)]
            if len(close_window) < self.high_period:
                continue
            max_close_n = max(close_window)
            if d1_close <= max_close_n:
                continue

            # P0: 首板限定 - D-1必须涨停, 且D-2未涨停
            if self.first_board_only:
                # D-1 涨停判定
                if not self._is_limit_up(d1_close, d1_preclose, code):
                    continue
                # D-2 未涨停判定
                d_minus_2 = hist_list[n - 3]
                d2_close = d_minus_2["close"]
                d2_preclose = d_minus_2.get("preclose", 0)
                if self._is_limit_up(d2_close, d2_preclose, code):
                    continue  # D-2也涨停 = 连板, 排除

            candidates.append({
                "code": code,
                "code_name": row.get("code_name", ""),
                "close_rate": float(row["close_rate"]),
                "turn": float(row["turn"]),
                "avg_turn_5": avg_turn_5,
                "vol_ratio": vol_ratio,
                "prev_close": d1_close,
                "max_close_n": max_close_n,
                "amount": float(row["amount"]),
                "breakout_pct": (d1_close / max_close_n - 1) * 100 if max_close_n > 0 else 0,
            })

        # P0: 按 vol_ratio ASC 排序 (温和放量优先, 避开投机品种)
        candidates.sort(key=lambda x: x["vol_ratio"])
        return candidates

    def describe_candidate(self, cand: dict) -> str:
        """简要描述用于日志"""
        code = cand.get("code", "")
        name = cand.get("code_name", "") or ""
        return (f"{code} {name} | 涨幅:{cand.get('close_rate', 0):+.2f}% | "
                f"放量:{cand.get('vol_ratio', 0):.1f}x | "
                f"突破:{cand.get('breakout_pct', 0):+.2f}%")

    # ------------------------------------------------------------------
    # 买入决策 (在 buy_hour 用 hour_open 买入) [P0优化: 跳空+大盘复合]
    # ------------------------------------------------------------------
    def should_buy(self, candidates: List[dict], date: str, hour: int,
                   hour_data: dict, portfolio, day_data: pd.DataFrame = None) -> Optional[dict]:
        """
        在 buy_hour (默认 hour1) 以 hour1_open 买入:
            - 排除一字板/已持仓/isST
            - 用 hour1_open 作为成交价 (信号在昨日, 今日开盘即可下单)
            P0新增:
            - 跳空过滤: hour1_open / prev_close - 1 < gap_up_max
            - 大盘hour1情绪过滤: hour1_index_close_rate > hour1_index_min
        """
        if hour != self.buy_hour:
            return None
        if not candidates:
            return None

        # P0: 大盘hour1情绪过滤 (当天hour1数据, 在hour1时已可观测)
        if self.use_market_filter and self.hour1_index_min is not None:
            mkt = self.market_cache.get(date)
            if mkt:
                h1_rate = mkt.get("hour1_close_rate")
                if h1_rate is not None and h1_rate < self.hour1_index_min:
                    return None  # 大盘hour1跌幅过大, 不买

        held_codes = portfolio.held_codes()

        # 预处理 day_data 用于 isST/一字板二次校验
        day_lookup = {}
        if day_data is not None and not day_data.empty:
            day_lookup = day_data.set_index("code").to_dict("index")

        for cand in candidates:
            code = cand["code"]
            if code in held_codes:
                continue

            stock_hour = hour_data.get(code)
            if stock_hour is None:
                continue

            h_open = stock_hour.get("open", 0)
            h_close = stock_hour.get("close", 0)
            h_high = stock_hour.get("high", 0)
            h_low = stock_hour.get("low", 0)

            if not h_open or h_open <= 0:
                continue

            # 一字板封死: hour O=H=L=C 且 open_rate ≥ 9.5% (涨停一字)
            if h_open == h_close == h_high == h_low:
                h_open_rate = stock_hour.get("open_rate", 0)
                if h_open_rate is not None and h_open_rate > 9.5:
                    continue  # 涨停一字, 无法买入

            # P0: 跳空过滤 - hour1_open 相对 D-1 close 的涨幅
            prev_close = cand.get("prev_close", 0)
            if prev_close and prev_close > 0:
                gap_up_rate = (h_open / prev_close - 1) * 100
                if gap_up_rate >= self.gap_up_max:
                    continue  # 高开幅度过大, 跳过

            # isST / 日级一字 二次确认 (今日)
            day_row = day_lookup.get(code)
            if day_row:
                if int(day_row.get("isST", 0) or 0) == 1:
                    continue

            return {
                "code": code,
                "code_name": cand["code_name"],
                "price": float(h_open),  # ★ 关键: 用 hour1_open 价格成交
                "vol_ratio": cand.get("vol_ratio"),
                "close_rate": cand.get("close_rate"),
                "breakout_pct": cand.get("breakout_pct"),
            }

        return None
