8J\n    2024-08-29            0.63  -0.64   2.01     20.26/20.38     20.39/20.60     20.60/20.88     20.89/20.80   \n    2024-08-30            1.29   3.75   4.42     21.58/21.89     21.85/22.15     22.16/21.82     21.82/21.72   \n    2024-09-02  TODAY     0.93   1.89  -3.64     22.13/21.40     21.41/21.39     21.42/21.16     21.16/20.93   \n    2024-09-03            0.66   0.10   0.43     20.95/21.19     21.28/21.03     21.03/21.04     21.04/21.02   \n    2024-09-04            0.53  -1.47  -0.24     20.71/20.75     20.79/20.80     20.80/20.85     20.85/20.97   \n    2024-09-05            0.46  -1.76  -1.72     20.60/20.79     20.79/20.65     20.65/20.58     20.57/20.61   \n    2024-09-06            0.40   1.31  -2.67     20.88/20.39     20.38/20.18     20.18/20.16     20.16/20.06   \n    2024-09-09            0.43   0.30  -1.45     20.12/19.96     19.96/19.91     19.93/19.77     19.78/19.77   \n\n  sh.688588 凌志软件  突增=5.2x  yd_turn=2.29%  基数=0.439%  高开=2.57%\n    sh.688588 凌志软件  (前5+当日+后5 hour级OHLC)\n    日期          标记       turn%    开幅%    收幅%             h1 o/c          h2 o/c          h3 o/c          h4 o/c\n    2024-08-26            0.45   0.50   1.83      6.10/6.09       6.09/6.12       6.12/6.10       6.10/6.13    \n    2024-08-27            0.46   0.00  -2.28      6.13/6.08       6.07/6.05       6.05/6.02       6.02/5.99    \n    2024-08-28            0.37  -0.17   0.83      5.98/6.00       6.00/6.06       6.05/6.09       6.09/6.04    \n    2024-08-29            0.44   0.17   2.32      6.05/6.11       6.11/6.13       6.13/6.16       6.16/6.18    \n===VERDICT===\n最终对比与达标判定\n============================================================================================\n变体    时机           信号     最优持有      均收益%     胜率%      正年份        判定\n--------------------------------------------------------------------------------------------\nA     today        16        -         -       -        -      样本不足\nA     T+1          16        -         -       -        -      样本不足\nB     today        69      T+4     +9.99    65.2      1/1        达标\nB     T+1          69      T+3     +4.90    47.8      1/1       未达标\nC     today        98      T+4     +7.97    67.3      1/1        达标\nC     T+1          98      T+3     +7.52    64.3      1/1        达标\nD     today         2        -         -       -        -      样本不足\nD     T+1           2        -         -       -        -      样本不足\n--------------------------------------------------------------------------------------------\n\n★ 推荐配置: 变体B + today h1_open + 持有T+4 -\u003e 均收益+9.99% 胜率65.2% 样本69\n  判定: 达标 (胜率\u003e=55% 且均收益\u003e0 且跨年稳定)\n\n(注: 单月样本有限, 判定仅供参考, 以 all 全周期为准)\n\n研究完成。\n","exitCode":0,"cwd":"/home/AIWealth","additionalInformation":"Executed by Node fallback because terminal shell integration was unavailable.","exec_mode":""},"content":"Command completed. \nCommand output:\n```\n$ cd /home/AIWealth \u0026\u0026 timeout 300 python3 scripts/research_turnover_spike_r2.py 2024-09 \u003e /tmp/t77_sep.txt 2\u003e\u00261; echo \"EXIT=$?\"; echo \"===HEAD===\"; head -30 /tmp/t77_sep.txt; echo \"===VERDICT===\"; sed -n '/最终对比/,$p'"""2板回调低吸策略 (B2-A) - Task#9 引擎精测。

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

卖出参数声明: hold1 策略无盘中止盈止损, 声明极宽 TP/SL 仅为满足框架读取要求,
实际退出 = 买入次日 hour4 收盘(expired); 跌停封死顺延后尽快卖出(deferred)。
"""
import csv

import numpy as np

from strategies.base import Strategy, Signal, SellSignal

_EMO_CSV = '/home/AIWealth/data/realtime/emotion_cycle_daily.csv'


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
    """{date: red_ratio(float) 或 None(当日缺失)}。缺失日调用方显式跳过。"""
    out = {}
    try:
        with open(_EMO_CSV) as f:
            for r in csv.DictReader(f):
                v = (r.get('red_ratio') or '').strip()
                out[r['date']] = float(v) if v else None
    except OSError:
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
