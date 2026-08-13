"""2板回调低吸策略 (B2-A) - Task#9 引擎精测。

形态(来自 Task#7 全维度网格复审, 研究口径见 data/realtime/task7_refine.txt):
  恰2板(最近涨停日连板数==2, 第2板非一字) → 断板回调1~2天(期间不再涨停)
  → 买入日竞价低开 -4% <= open_rate < 0 → H1_open 买入 → hold1 次日收盘卖出
排序: 更低开优先(open_rate升序) — task7_top1_probe: top1 +0.91%/日, 2021-2026六年全正

研究口径(池子级 hold1, 毛收益): n=5388 胜率48.4% 均值+0.67% 六年全正
引擎精测(2021-01~2026-07-23, slot=1, 含成本): CAGR +56.21% | MDD 73.10%
  | 641笔 胜率49.45% +0.65%/笔 | 分年 21:+70 22:-33 23:+3 24:+46 25:+194 26:+121
  → 观察池(50-100%档); 详见 data/realtime/task9_engine_report.txt
变体 B2-B(two_board_pullback_dip_b): 加"昨日红盘占比 red_ratio < 40"过滤,
  red_ratio 取 D0(昨日)值(D0收盘后即确定, 无未来数据); 缺失日显式跳过不买入。
red_ratio 权威源=index_kline(sh.000001), 已补齐到 2026-07-23(Task#10 Robin修复)。

Task#12 卖点前移(exit_mode 参数化, 买入逻辑不动):
  hold1_close 现行对照: D2 hour4 收盘卖
  d2h1_open / d2h1_close / trail_h1: D2 H1内了结 → slot当小时可复用
依据: Lee时段结构(H1唯一系统性正时段) x Task#9归因(hold1占slot隔日才空)。

卖出参数声明: hold1 策略无盘中止盈止损, 声明极宽 TP/SL 仅为满足框架读取要求,
实际退出 = 买入次日 hour4 收盘(expired); 跌停封死顺延后尽快卖出(deferred)。
"""
import sqlite3

import numpy as np

from strategies.base import Strategy, Signal, SellSignal

_DB = '/home/AIWealth/data/stocks.db'


def _limit_flags(frame, codes):
    """返回 (valid, is_limit, is_yizi, close) 对齐 codes 的numpy数组。

    涨停判定与研究口径一致: close >= round(preclose*(1+ratio),2) - 0.001
    一字板: open >= 涨停价 - 0.001。ST股已在候选层排除, 此处用非ST比例。
    """
    sub = frame.reindex(codes)
    pre = sub['preclose'].to_numpy(dtype=float, na_value=0.0)
    clo = sub['close'].to_numpy(dtype=float, na_value=0.0)
    opn = sub['open'].to_numpy(dtype=float, na_value=0.0)
    is20 = codes.str.startswith('sz.30') | codes.str.startswith('sh.688')
    ratio = np.where(is20, 0.20, 0.10)
    lp = np.round(pre * (1 + ratio), 2)
    valid = (pre > 0) & (clo > 0)
    is_lim = valid & (clo >= lp - 0.001)
    is_yizi = is_lim & (opn > 0) & (opn >= lp - 0.001)
    return valid, is_lim, is_yizi, clo


def _load_red_map() -> dict:
    """{date: red_ratio(float)}; 缺失日不在map中, 调用方显式跳过。"""
    out = {}
    try:
        conn = sqlite3.connect(_DB)
        for d, r in conn.execute(
                "SELECT date, red_ratio FROM index_kline "
                "WHERE code='sh.000001'"):
            if r is not None:
                out[d] = float(r)
        conn.close()
    except sqlite3.Error:
        pass
    return out


class TwoBoardPullbackDipStrategy(Strategy):
    """B2-A: 恰2板回调1~2天 → 低开-4~0 → H1低吸 → 次日收盘卖出。"""

    name = "two_board_pullback_dip"
    max_hold_hours = 8           # D1 h1 买入 → D2 h4 收盘 = 8小时
    sell_day_no_buy = True
    buy_hour = 1

    # === 核心参数(Task#7 网格最优) ===
    open_rate_min = -4.0         # 竞价低开下限
    open_rate_max = 0.0          # 竞价低开上限(不含)
    red_ratio_max = None         # B2-B变体设为40.0; None=不启用红盘过滤
    min_history_bars = 20        # 上市未满20根K线不买(对齐研究口径 nth>=20)

    # hold1 无盘中TP/SL; 极宽声明仅为满足框架卖出参数读取要求(不会触发)
    take_profit_pct = 9.99       # +999%, 实际不可达
    stop_loss_pct = -0.99        # -99%, 实际不可达

    # === Task#12 卖点前移 ===
    # hold1_close(现行对照) | d2h1_open | d2h1_close | trail_h1
    exit_mode = 'hold1_close'
    trail_pp = 2.0               # trail_h1 回撤触发(pp), 基于D2 H1_open

    def __init__(self):
        self._cand_codes = []
        self._cand_date = None
        self._red_map = None

    def _red_ratio(self, date: str):
        if self._red_map is None:
            self._red_map = _load_red_map()
        return self._red_map.get(date)

    def get_candidates(self, date, data_feed):
        self._cand_codes, self._cand_date = [], date

        # 回看5个交易日: d[0]=D0(昨日) .. d[4]
        chain = []
        d = date
        for _ in range(5):
            d = data_feed._prev_trading_date(d)
            if not d:
                return []
            chain.append(d)

        # B2-B: 昨日红盘占比过滤(D0收盘后已知); 缺失日显式跳过, 不引入未来数据
        if self.red_ratio_max is not None:
            red = self._red_ratio(chain[0])
            if red is None or red >= self.red_ratio_max:
                return []

        f_today = data_feed._load_day(date)
        if f_today is None or f_today.empty:
            return []
        frames = [data_feed._load_day(d) for d in chain]
        if any(f is None or f.empty for f in frames):
            return []

        codes = f_today.index
        flags = [_limit_flags(f, codes) for f in frames]
        v = [x[0] for x in flags]
        L = [x[1] for x in flags]
        Y = [x[2] for x in flags]

        # 恰2板 + 回调1~2天(期间无涨停), 第2板非一字:
        #   case1: D0未板, D-1/D-2连板, D-3未板   (断板后第1天)
        #   case2: D0/D-1未板, D-2/D-3连板, D-4未板 (断板后第2天)
        case1 = v[0] & v[1] & v[2] & v[3] & ~L[0] & L[1] & L[2] & ~L[3] & ~Y[1]
        case2 = (v[0] & v[1] & v[2] & v[3] & v[4]
                 & ~L[0] & ~L[1] & L[2] & L[3] & ~L[4] & ~Y[2])
        pattern = case1 | case2

        # 今日竞价窗口 + 基础池过滤
        orate = f_today['open_rate'].to_numpy(dtype=float, na_value=np.nan)
        opn = f_today['open'].to_numpy(dtype=float, na_value=0.0)
        win = (orate >= self.open_rate_min) & (orate < self.open_rate_max)
        not_bj = ~codes.str.startswith('bj.')
        st = f_today['isST'].fillna(0).astype(int).to_numpy() > 0
        if 'code_name' in f_today.columns:
            st = st | f_today['code_name'].fillna('').astype(str) \
                .str.upper().str.contains('ST').to_numpy()

        mask = pattern & win & (opn > 0) & not_bj & ~st
        if not mask.any():
            return []

        # 排序: 更低开优先(open_rate升序)
        idx = np.where(mask)[0]
        idx = idx[np.argsort(orate[idx], kind='stable')]

        final = []
        for i in idx:
            code = codes[i]
            # 上市历史检查(对齐研究口径 nth>=20), 仅对少量入围者查库
            hist = data_feed.get_stock_history(code, date,
                                               self.min_history_bars)
            if len(hist) < self.min_history_bars:
                continue
            final.append(code)

        self._cand_codes = final
        return final

    def should_buy(self, code, date, hour, data_feed, portfolio):
        if hour != self.buy_hour:
            return None
        if date != self._cand_date or code not in self._cand_codes:
            return None
        price = data_feed.get_hour_open(code, date, hour)
        if not price or price <= 0:
            return None
        # 双保险: 开盘已涨停不可买(引擎另有兜底)
        limit_up, _ = data_feed.get_limit_prices(code, date)
        if limit_up > 0 and price >= limit_up - 0.001:
            return None
        return Signal(code=code, price=float(price), strategy_name=self.name,
                      target_hold_hours=self.max_hold_hours)

    def should_sell(self, position, date, hour, data_feed):
        # T+1: 买入当日不卖(框架另有兜底)
        if position.buy_date >= date:
            return None
        # hold1: 次日(D2) hour4 收盘卖出
        if hour == 4:
            price = data_feed.get_hour_close(position.code, date, 4)
            if not price or price <= 0:
                price = data_feed.get_hour_open(position.code, date, 4)
            if price and price > 0:
                return SellSignal(reason='expired', price=float(price))
            return None
        # 跌停封死顺延/停牌后的兜底: 已超过hold1目标仍持仓 → 尽快卖出
        if position.hours_held >= self.max_hold_hours:
            price = data_feed.get_hour_open(position.code, date, hour)
            if price and price > 0:
                return SellSignal(reason='deferred', price=float(price))
        return None

    def get_buy_price(self, code, date, hour, data_feed):
        return data_feed.get_hour_open(code, date, hour)
