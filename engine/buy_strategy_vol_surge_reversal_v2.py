"""缩量下跌后首次放量反弹策略 V2 - BuyModule实现 (Task #93)

研究结论(来自 scripts/strategy_vol_surge_reversal.py):
  信号日T: 前5日累计跌幅>=10% + 前5日缩量递减 + 今日放量>=5日均量2倍 + 收阳
  合规买入: T+1 hour1 open买入(信号日=T，买入日=T+1)
  卖出: 持仓2交易日后 hour4卖出(即T+3 hour4)
  裸持最优(无止盈止损)
  排除: ST、次新(<60交易日)、北交所、一字涨停
  历史表现: 胜率55.2%, 每笔+1.56%, 年均170信号

引擎适配:
  引擎主循环中 date = 买入日(T+1), prev_data = 昨日全市场(即信号日T)。
  get_candidates用prev_data确认T日信号，并查DB取T日前10日历史，
  从而完全符合T+0/T+1合规(买入日只用信号日及更早数据判定)。
"""
from typing import List
import sqlite3
import pandas as pd
from .buy_module import BuyModule


# ==================== 策略参数(与研究脚本一致) ====================
DECLINE_THRESHOLD = -10.0     # 前5日累计跌幅阈值(%)
VOL_SURGE_RATIO = 2.0         # 信号日放量倍数(相对前5日均量)
VOL_SHRINK_RATIO = 0.9        # 缩量判定: 5日均量 < 0.9*10日均量
DECLINE_STEPS_MIN = 3         # 前5日成交量递减步数下限(0~4)
MIN_LISTING_DAYS = 60         # 次新股过滤: 上市交易日数下限
# ==================================================================


def _get_limit_ratio(code: str) -> float:
    if code.startswith('sz.300') or code.startswith('sz.301'):
        return 0.20
    elif code.startswith('sh.688'):
        return 0.20
    elif code.startswith('bj.'):
        return 0.30
    return 0.10


def _calc_limit_up(preclose: float, code: str) -> float:
    return round(preclose * (1 + _get_limit_ratio(code)), 2)


def _is_yizi_limit_up(open_p, high, low, close, preclose, code) -> bool:
    """一字涨停判定: 四价相等且>=涨停价"""
    if any(v is None for v in [open_p, high, low, close, preclose]):
        return False
    if preclose <= 0:
        return False
    if open_p == high == low == close and close >= _calc_limit_up(preclose, code):
        return True
    return False


class VolSurgeReversalV2Strategy(BuyModule):
    """缩量下跌后首次放量反弹策略 V2"""

    strategy_name = 'vol_surge_reversal'

    def __init__(self, buy_hour: int = 1, target_hold_days: int = 2,
                 db_path: str = '/home/AIWealth/data/stocks.db'):
        self.buy_hour = buy_hour                    # T+1的hour1买入
        self.target_hold_days = target_hold_days    # 持仓2交易日后hour4卖出
        self.db_path = db_path
        self._priority = 5

    def _load_prev_history(self, conn, signal_day: str):
        """批量加载信号日之前10个交易日(T-10..T-1)的 close/volume。
        返回 (hist, days) : hist[code] = {date: (close, volume)}; days = 升序日期列表
        """
        days = [r[0] for r in conn.execute(
            "SELECT DISTINCT date FROM stock_kline WHERE date < ? ORDER BY date DESC LIMIT 10",
            (signal_day,)).fetchall()]
        if len(days) < 10:
            return {}, []
        days.reverse()  # 升序: T-10 .. T-1
        placeholders = ','.join(['?'] * len(days))
        rows = conn.execute(
            f"SELECT code, date, close, volume FROM stock_kline WHERE date IN ({placeholders})",
            days).fetchall()
        hist = {}
        for code, date, close, volume in rows:
            hist.setdefault(code, {})[date] = (close, volume)
        return hist, days

    def get_candidates(self, date: str, day_data: pd.DataFrame,
                       prev_data: pd.DataFrame, blacklist) -> List[dict]:
        """用prev_data(昨日=信号日T)判断信号，DB查前10日历史确认缩量下跌+放量反弹。"""
        if prev_data is None or prev_data.empty:
            return []

        # 信号日 = 昨日
        signal_day = str(prev_data['date'].iloc[0])

        conn = sqlite3.connect(self.db_path)
        try:
            hist, prev_days = self._load_prev_history(conn, signal_day)
            if not prev_days:
                return []

            candidates = []
            # 遍历信号日全市场，逐股确认信号
            for row in prev_data.itertuples(index=False):
                r = row._asdict()
                code = r.get('code')
                if not code:
                    continue
                # --- 排除北交所 ---
                if code.startswith('bj.'):
                    continue
                # --- 排除ST ---
                code_name = r.get('code_name') or ''
                if int(r.get('isST', 0) or 0) == 1:
                    continue
                if 'ST' in str(code_name).upper():
                    continue

                t_open = r.get('open')
                t_high = r.get('high')
                t_low = r.get('low')
                t_close = r.get('close')
                t_preclose = r.get('preclose')
                t_volume = r.get('volume')
                if t_open is None or t_close is None or t_volume is None:
                    continue
                if pd.isna(t_open) or pd.isna(t_close) or pd.isna(t_volume) or t_volume <= 0:
                    continue

                # --- 条件4: 信号日收阳 ---
                if not (t_close > t_open):
                    continue

                # --- 排除信号日一字涨停 ---
                if _is_yizi_limit_up(t_open, t_high, t_low, t_close, t_preclose, code):
                    continue

                # --- 取前10日历史(T-10..T-1) ---
                code_hist = hist.get(code, {})
                if len(code_hist) < 10:
                    continue
                ordered = [code_hist[d] for d in prev_days if d in code_hist]
                if len(ordered) < 10:
                    continue
                closes = [c for c, v in ordered]
                vols = [v for c, v in ordered]
                if any(c is None or c <= 0 for c in closes):
                    continue
                if any(v is None or v <= 0 for v in vols):
                    continue

                # --- 条件1: 前5日累计跌幅 (close[T-5]->close[T-1]) ---
                close_t5 = closes[5]
                close_t1 = closes[9]
                decline_pct = (close_t1 - close_t5) / close_t5 * 100
                if decline_pct > DECLINE_THRESHOLD:
                    continue

                # --- 前5日/前10日均量 ---
                vol_prev5 = vols[5:10]
                avg_vol5 = sum(vol_prev5) / len(vol_prev5)
                avg_vol10 = sum(vols) / len(vols)

                # --- 条件2: 缩量趋势 ---
                dec_steps = sum(1 for i in range(1, len(vol_prev5))
                                if vol_prev5[i] < vol_prev5[i - 1])
                shrink_ok = (dec_steps >= DECLINE_STEPS_MIN) or \
                            (avg_vol5 < VOL_SHRINK_RATIO * avg_vol10)
                if not shrink_ok:
                    continue

                # --- 条件3: 信号日放量 >= 前5日均量 * 2 ---
                surge_ratio = t_volume / avg_vol5 if avg_vol5 > 0 else 0
                if surge_ratio < VOL_SURGE_RATIO:
                    continue

                candidates.append({
                    'code': code,
                    'code_name': str(code_name),
                    'decline_pct': float(decline_pct),
                    'surge_ratio': float(surge_ratio),
                    'dec_steps': int(dec_steps),
                    '_strategy': self.strategy_name,
                    '_priority': self._priority,
                    '_target_hold_days': self.target_hold_days,
                })

            if not candidates:
                return []

            # --- 排除次新股(仅对少量最终候选做per-code查询) ---
            filtered = []
            for c in candidates:
                cnt = conn.execute(
                    "SELECT COUNT(*) FROM stock_kline WHERE code = ? AND date <= ?",
                    (c['code'], signal_day)).fetchone()[0]
                if cnt >= MIN_LISTING_DAYS:
                    filtered.append(c)

            # 放量越强越优先
            filtered.sort(key=lambda x: -x['surge_ratio'])
            return filtered
        finally:
            conn.close()

    def describe_candidate(self, cand: dict) -> str:
        code = cand.get('code', '')
        name = cand.get('code_name', '') or ''
        return (f"{code} {name} | 前5日跌幅:{cand.get('decline_pct', 0):+.1f}% | "
                f"放量:{cand.get('surge_ratio', 0):.1f}倍 | 缩量步数:{cand.get('dec_steps', 0)}/4")

    def should_buy(self, candidates, date, hour, hour_data, portfolio, day_data=None):
        """T+1 hour1 用hour1_open买入。"""
        if hour != self.buy_hour:
            return None
        my_cands = [c for c in candidates if c.get('_strategy') == self.strategy_name]
        if not my_cands:
            return None
        held = portfolio.held_codes() if portfolio else set()

        for cand in my_cands:
            code = cand['code']
            if code in held:
                continue
            h = hour_data.get(code) if hour_data else None
            if not h:
                continue
            price = h.get('open')
            if not price or price <= 0:
                continue
            # 一字板(买入时段O=H=L=C)无法成交，跳过
            h_high = h.get('high')
            h_low = h.get('low')
            h_close = h.get('close')
            if h_high == h_low == price == h_close and price > 0:
                continue
            return {
                'code': code,
                'code_name': cand['code_name'],
                'price': float(price),
                'strategy_name': self.strategy_name,
                'target_hold_days': self.target_hold_days,
                'buy_reason': (f"缩量下跌{cand['decline_pct']:.1f}%后放量"
                               f"{cand['surge_ratio']:.1f}倍反弹收阳"),
            }
        return None
