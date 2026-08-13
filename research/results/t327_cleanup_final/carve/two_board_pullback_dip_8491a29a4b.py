FtgZqiFPLCo6V//KyR9DIzgIWc9Nv7BOiGRXPIWlhIqSzVCyRHXkVIOqtMktIoPg47v3Sl+76u8QOaN+YdL+nFZ0TBoiswxJy3hxf8SoKgiEo6lbiqLScU/OzRTa/vWrTOj0u/97+aOQJVqO4BpHcWyhwAp/GmmmhaAjQ8WK5OVlDknp/CkVfTOlBm8R+rVk+1MLCHX8woDITUny0+JO+XCHGvtQb39hi/B5S3Nfbl/+2a1qxVOuQbPDBXADgrcusLJox2lsgDAAZvHVwLDpumvBO4hGV3ykFXYX7rJbYwK1qWBuyNHI4cvMdhkyd40P6rg+vm9mRbnyyufjcYAXcwrt/afjs/XHRrUyroyVgasYiUgwwFJkle8f/tXIxl6siqMokCdq3RmzBBcNiVi9rHAU5y6rYQJzbBEBrjfYtCUUbpH8E+NldoTWJ0bwGr/NmauooBGhy2/uLFJ/zZ/jzZ7whZcVYC2/6zd0SxdbW2jqWr8NLd6ck6H1c2g39TvQ6MVl8NITfdOqmeDBi9kMZI1GawrzsOIbQ5+qjmxHlrLrvIOVo3RMMSZvuxmwx4194iYEgrazb6PSPwbMpd1SPDrWtOdQQh/ClIbmFmGr2v2mDmUEjts/m8vaT3Yp5TJa3Nb5dsLlL6lqI+FMxIuBecNkCsVcd2EbhwRCMh7cN73fXI9i+B97Ld4MWJ2aWvL3wyqafJQUKu2F56+LLtuA2QuYDXNhvpM09BTyDrhyFUOEQ93CU/0MK2DHYTUS57NYQsqRvffunareGO2HEvi3JY427s5quf73GNOJqsBe2uXm+ZNEjrQ8j1tAWvI9Qwyc8Q/I9y0/ek8YStLgETPmBBmXZVxzIEhO2CL8ST4nX7ze+ig+x7gj2zaQj1LK0PnGI38dVpHur6Ki3kHNJY3SDL0fYtqGFZJJa1boADxaR4BSqrFkJXsAPK/4YkI6z1hqzXE0Gesg609Je4F0wzn29aZNUt+ER3olS95fCbIsD9c3sr2BHO3XmzJfD8O/hYb1iF2fgJpCPP5jHKUQDEvcaFWXFzKPF5ITPsv+sIlaChvt0Zk+3w3pC2BbDiPGNOCcw6p7DNmJIRmp/IGbJdwdZ9dVTGDbKyOW5RVlGQGICHkjyHC7+u2tZhBr2R1DzUcifqtFpZPsYikNBW4q9Gui12Tpw6rc8WzUaZQO+mcoVEBj/NfBYUAgOxM/seA1FVA6QXYZcg7yOPK1TXum7xYHSH+pmNeXh2JqW7J5cmLfdNOEJ0gChNRLqYhCJ57wl6L/Fkg3kK02Ussm8cSvIpZdWe0VRgvrvgNMDqhJpqJmgNwiztXYTrlT7adBiVKJ87u3Y/3OBQivs+/U4DzbxOcK3uqmjB3Yf61W51E3cO/Kn6uqxrafUJNCR8R9fCN6w3baV7VZ4sKCpg65DDUf2iCH2HtFOKuBPh/qZm0n5YzAnhZnPCOwKNMKmg6wJU/HyfuF/B5o4iad2TWNskflp1dUVM0XvH7n3hX22wpWnyeSqeMIdau0g3bZXe4uiDFwNyswVrAmDSUb+ijCQgJgvmmNY5zx66riCtF4VkUafYZRcvykcDA=={"sessionId":"fb79d14c-e66b-498a-9820-1bbb81da6767","requestId":"bc00a0cf-3191-49d5-9a27-59173bb9f0a1","projectPath":"/home/AIWealth","toolCallId":"toolu_bdrk_01D2J34Lmm5E5LcWjqx8bkvo","toolCallStatus":"FINISHED","parameters":{"command":"cd /home/AIWealth \u0026\u0026 python3 scripts/strategy_bigdrop_reversal.py --mode sweep --start 2021-01-01 --end 2026-06-30 --top 40 \u003e logs/backtest/sweep_full.out 2\u003e\u00261; echo \"EXIT=$?\"; wc -l logs/backtest/sweep_full.out","is_background":false,"timeout":180000},"results":[{"terminalId":"node-fallback:toolu_bdrk_01D2J34Lmm5E5LcWjqx8bkvo","content":"$ cd /home/AIWealth \u0026\u0026 python3 scripts/strategy_bigdrop_reversal.py --mode sweep --start 2021-01-01 --end 2026-06-30 --top 40 \u003e logs/backtest/sweep_full.out 2\u003e\u00261; echo \"EXIT=$?\"; wc -l logs/backtest/sweep_full.out\nEXIT=0\n96 logs/backtest/sweep_full"""2板回调低吸策略 (B2-A) - Task#9 引擎精测。

形态(来自 Task#7 全维度网格复审, 研究口径见 data/realtime/task7_refine.txt):
  恰2板(最近涨停日连板数==2, 第2板非一字) → 断板回调1~2天(期间不再涨停)
  → 买入日竞价低开 -4% <= open_rate < 0 → H1_open 买入 → hold1 次日收盘卖出
排序: 更低开优先(open_rate升序) — task7_top1_probe: top1 +0.91%/日, 2021-2026六年全正

研究口径(池子级 hold1, 毛收益): n=5388 胜率48.4% 均值+0.67% 六年全正
引擎精测(2021-01~2026-07-23, slot=1, 含成本): CAGR +56.21% | MDD 73.10%
  | 641笔 胜率49.45% +0.65%/笔 | 分年 21:+70 22:-33 23:+3 24:+46 25:+194 26:+121
  → 观察池(50-100%档); 详见 data/realtime/task9_engine_report.txt
Task#12 干净数据重跑(stocks.db 2026-05-25~06-30缺口修复后): CAGR +61.36%,
  2026 +121→+174, 其余年份不变; 卖点前移变体 two_board_pullback_dip_h1c
  (D2 H1收盘卖+slot当小时复用) **CAGR +107.79% 达标** → 推荐替代本对照版。
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


def _lim_price(row, code):
    """单行涨停价(与_limit_flags同口径); Task#68 F2过滤用。"""
    ratio = 0.20 if code.startswith(('sz.30', 'sh.688')) else 0.10
    pre = float(row.get('preclose') or 0)
    return round(pre * (1 + ratio), 2) if pre > 0 else 0.0


def _is_lim(row, code):
    lp = _lim_price(row, code)
    c = float(row.get('close') or 0)
    return lp > 0 and c > 0 and c >= lp - 0.001


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
    # Task#63/68 F2板质量过滤: 第2板首触涨停hour > 该值则剔除(1=只留H1早板);
    # None=不过滤零侵入。逻辑自 scripts/t63_engine_runner.py T63Variant 转正,
    # 特征全部来自板日(D-1..D-4)K线, D1开盘前可见, 时序合规
    max_seal_hour_b2 = None

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
        # Task#36: signal模式(晚间候选生成)次日开盘未知, permissive开盘价(-20%)
        # 落在带状窗口[-4,0)之外会误杀全部候选 → 跳过窗口过滤只筛形态;
        # 9:25 live模式(真实开盘价注入)与回测(无_mode属性)窗口照常生效
        if getattr(data_feed, '_mode', None) == 'signal':
            win = np.ones(len(codes), dtype=bool)
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

        # Task#63/68 F2: 板质量过滤(第2板必须在max_seal_hour_b2内首触涨停);
        # 与T63Variant完全同序同口径: 在基础候选链(含历史检查)之后叠加
        if self.max_seal_hour_b2 is not None:
            kept = []
            for code in final:
                br = self._board_rows(code, date, data_feed)
                if br is None:
                    continue                  # 板日重推失败: 保守剔除
                _b1, b2 = br
                if self._seal_hour(b2, code) > self.max_seal_hour_b2:
                    continue
                kept.append(code)
            final = kept

        self._cand_codes = final
        return final

    def _board_rows(self, code, date, data_feed):
        """返回 (b1_row, b2_row) 或 None; 板日从回看链重推(与形态判定一致)。"""
        chain = []
        d = date
        for _ in range(4):
            d = data_feed._prev_trading_date(d)
            if not d:
                return None
            chain.append(d)
        frames = [data_feed._load_day(x) for x in chain]
        if any(f is None or f.empty or code not in f.index for f in frames):
            return None
        # case1: 板=chain[1],chain[2]; case2: 板=chain[2],chain[3]
        if _is_lim(frames[1].loc[code], code):
            b2, b1 = frames[1].loc[code], frames[2].loc[code]
        elif _is_lim(frames[2].loc[code], code):
            b2, b1 = frames[2].loc[code], frames[3].loc[code]
        else:
            return None
        return b1, b2

    def _seal_hour(self, row, code):
        """首次触涨停时段(hourN_high>=涨停价的最早N); 无hour数据返回0。"""
        lp = _lim_price(row, code)
        for h in (1, 2, 3, 4):
            v = row.get(f'hour{h}_high')
            if v is not None and np.isfinite(v) and v >= lp - 0.001:
                return h
        return 0

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
        if self.exit_mode != 'hold1_close':
            return self._sell_forward(position, date, hour, data_feed)
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

    def _sell_forward(self, position, date, hour, data_feed):
        """Task#12 前移变体: D2 H1内了结, 卖出当小时slot即可复用。
        跌停封死/缺数据顺延 → 之后任意小时尽快开盘价卖出(deferred)。"""
        code = position.code
        if hour != 1:
            price = data_feed.get_hour_open(code, date, hour)
            if price and price > 0:
                return SellSignal(reason='deferred', price=float(price))
            return None
        o = data_feed.get_hour_open(code, date, 1)
        c = data_feed.get_hour_close(code, date, 1)
        lo = data_feed.get_hour_low(code, date, 1)
        if not o or o <= 0:
            return None
        if self.exit_mode == 'd2h1_open':
            return SellSignal(reason='expired', price=float(o))
        if self.exit_mode == 'd2h1_close':
            price = c if c and c > 0 else o
            return SellSignal(reason='expired', price=float(price))
        if self.exit_mode == 'trail_h1':
            # 保守挂单语义: 触发价只基于H1_open, 不做bar内peak乐观假设
            trig = o * (1 - self.trail_pp / 100)
            if lo and lo > 0 and lo <= trig:
                return SellSignal(reason='stop_loss', price=float(trig))
            price = c if c and c > 0 else o
            return SellSignal(reason='expired', price=float(price))
        return None

    def get_buy_price(self, code, date, hour, data_feed):
        return data_feed.get_hour_open(code, date, hour)
