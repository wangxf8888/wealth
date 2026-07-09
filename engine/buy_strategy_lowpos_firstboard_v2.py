"""低位首板(底部突破)次日策略 V2 - BuyModule实现 (Task #106)

策略逻辑(全部在 T-1 收盘后即可确定, T+0 合规):
  - 仅创业板(sz.30/sz.301) / 科创板(sh.688)
  - T-1(昨日)首板涨停: round(close/preclose,2) >= 涨停比(创业/科创 1.20)
  - T-1 之前 firstboard_clean_days(默认5) 个交易日内无涨停(确认"首板"非连板)
  - 排除 T-1 一字涨停(次日大概率一字继续, 买不到)
  - 突破前价格(T-2 close = T-1 preclose)在近 lookback_days(默认60)日底部
        position = (t2_close - min60) / (max60 - min60) <= position_threshold(默认0.05)
        用突破前价格而非 T-1 close(已被涨停拉高)才能表达"低位盘整后突然涨停突破"
  - 排除 ST
  - T 日开盘非一字涨停(否则买不到)

买入(T日): hour1 open 买入
卖出: 持仓 target_hold_days(默认3) 交易日后 hour4 卖出(T+3 hour4), 裸持无止盈止损

研究基准(strategy_lowpos_firstboard.py 全周期):
  405信号, 57.5%胜率, 单笔+2.75%, 73.6信号/年
"""
from typing import List, Optional
import sqlite3
import pandas as pd
from .buy_module import BuyModule


def _get_limit_ratio(code: str) -> float:
    """涨跌幅比例"""
    if code.startswith('sz.300') or code.startswith('sz.301'):
        return 0.20  # 创业板
    if code.startswith('sh.688'):
        return 0.20  # 科创板
    if code.startswith('bj.') or code.startswith('bj'):
        return 0.30  # 北交所
    return 0.10      # 主板


def _is_target_board(code: str) -> bool:
    """仅创业板或科创板"""
    return code.startswith('sz.300') or code.startswith('sz.301') or code.startswith('sh.688')


def _is_limit_up(close, preclose, code) -> bool:
    """涨停严格判定: round(close/preclose,2) >= 1+ratio"""
    if preclose is None or preclose <= 0 or close is None:
        return False
    thr = round(1 + _get_limit_ratio(code), 2)
    return round(close / preclose, 2) >= thr


def _is_yizi_limit_up(open_p, preclose, code) -> bool:
    """一字涨停(开盘即涨停价): round(open/preclose,2) >= 1+ratio"""
    if preclose is None or preclose <= 0 or open_p is None:
        return False
    thr = round(1 + _get_limit_ratio(code), 2)
    return round(open_p / preclose, 2) >= thr


class LowPosFirstBoardV2Strategy(BuyModule):
    """低位首板(底部突破)次日策略 V2"""

    strategy_name = 'lowpos_firstboard'
    buy_hour = 1

    def __init__(self, buy_hour: int = 1, target_hold_days: int = 3,
                 position_threshold: float = 0.05,
                 lookback_days: int = 60, firstboard_clean_days: int = 5,
                 db_path: str = '/home/AIWealth/data/stocks.db'):
        self.buy_hour = buy_hour
        self.target_hold_days = target_hold_days
        self.position_threshold = position_threshold  # 底部区间阈值(默认0.05)
        self.lookback_days = lookback_days            # 底部位置回看交易日数
        self.firstboard_clean_days = firstboard_clean_days  # 首板确认回看日数
        self.db_path = db_path

    # ---------- DB 辅助 ----------
    def _load_day(self, cur, date: str) -> dict:
        """加载某交易日全市场基础数据, 返回 {code: {...}}"""
        cur.execute(
            "SELECT code, code_name, preclose, open, high, low, close, isST "
            "FROM stock_kline WHERE date = ?", (date,))
        cols = ['code', 'code_name', 'preclose', 'open', 'high', 'low', 'close', 'isST']
        return {r[0]: dict(zip(cols, r)) for r in cur.fetchall()}

    def _get_close_series(self, cur, code: str, end_date: str, n: int) -> list:
        """获取 code 在 end_date(含) 及之前 n 个交易日的 close 序列"""
        cur.execute(
            "SELECT close FROM stock_kline "
            "WHERE code = ? AND date <= ? AND close IS NOT NULL "
            "ORDER BY date DESC LIMIT ?", (code, end_date, n))
        return [r[0] for r in cur.fetchall()]

    # ---------- 候选筛选 ----------
    def get_candidates(self, date: str, day_data: pd.DataFrame,
                       prev_data: pd.DataFrame, blacklist) -> List[dict]:
        """筛选 date(=T日, 首板次日) 的候选股。
        prev_data 为 T-1 数据(此处直接查库以获取完整历史)。"""
        if day_data is None or day_data.empty:
            return []

        conn = sqlite3.connect(self.db_path)
        cur = conn.cursor()
        try:
            # 取 date 之前的交易日(T-1, T-2, ..., 用于首板确认与位置基准)
            cur.execute(
                "SELECT DISTINCT date FROM stock_kline WHERE date < ? "
                "ORDER BY date DESC LIMIT ?", (date, self.firstboard_clean_days + 2))
            prior = [r[0] for r in cur.fetchall()]
            if len(prior) < self.firstboard_clean_days + 1:
                return []
            yesterday = prior[0]                          # T-1 首板日
            daybefore = prior[1]                          # T-2 突破前收盘参照
            clean_start = prior[self.firstboard_clean_days]  # 首板判定回看起点(T-6)

            yd_map = self._load_day(cur, yesterday)
            # T 日行情从 day_data 提取(引擎已加载)
            today_lookup = day_data.set_index('code').to_dict('index')

            candidates = []
            for code, yd in yd_map.items():
                if not _is_target_board(code):
                    continue
                if yd.get('isST'):
                    continue
                name = yd.get('code_name') or ''
                if name and 'ST' in name.upper():
                    continue
                yd_close, yd_pre, yd_open = yd.get('close'), yd.get('preclose'), yd.get('open')
                if yd_pre is None or yd_pre <= 0 or yd_close is None:
                    continue

                # 1. T-1 首板涨停
                if not _is_limit_up(yd_close, yd_pre, code):
                    continue

                # 2. T-1 之前 clean_days 日内无涨停(区间 (clean_start, yesterday) 开区间)
                cur.execute(
                    "SELECT close, preclose FROM stock_kline "
                    "WHERE code = ? AND date > ? AND date < ? AND preclose > 0",
                    (code, clean_start, yesterday))
                if any(_is_limit_up(c, p, code) for c, p in cur.fetchall()):
                    continue  # 连板/近期有涨停, 非首板

                # 3. 排除 T-1 一字涨停
                if _is_yizi_limit_up(yd_open, yd_pre, code):
                    continue

                # 4. 底部位置: 用突破前价格(T-2 close = T-1 preclose)在近60日中的相对位置
                closes = self._get_close_series(cur, code, daybefore, self.lookback_days)
                if len(closes) < 40:  # 数据不足(次新股等)跳过
                    continue
                min60, max60 = min(closes), max(closes)
                if max60 <= min60:
                    continue
                position = (yd_pre - min60) / (max60 - min60)
                if position > self.position_threshold:
                    continue

                # 5. T 日必须有数据且可买入(开盘非涨停)
                t = today_lookup.get(code)
                if t is None:
                    continue
                t_open = t.get('open')
                t_pre = t.get('preclose')
                if t_pre is None or t_pre <= 0 or t_open is None or t_open <= 0:
                    continue
                if int(t.get('isST', 0) or 0) == 1:
                    continue
                if _is_yizi_limit_up(t_open, t_pre, code):
                    continue

                open_rate = (t_open - t_pre) / t_pre * 100
                candidates.append({
                    'code': code,
                    'code_name': name,
                    'position': position,
                    'open_rate': open_rate,
                    'yd_pct': (yd_close - yd_pre) / yd_pre * 100,
                    '_strategy': self.strategy_name,
                    '_priority': 4,
                    '_target_hold_days': self.target_hold_days,
                })

            candidates.sort(key=lambda x: x['position'])  # 越低越优先
            return candidates
        finally:
            conn.close()

    def describe_candidate(self, cand: dict) -> str:
        """简要描述用于日志"""
        code = cand.get('code', '')
        name = cand.get('code_name', '') or ''
        return (f"{code} {name} | 底部位置:{cand.get('position', 0) * 100:.1f}% | "
                f"高开:{cand.get('open_rate', 0):+.2f}% | 低位首板")

    # ---------- 买入决策 ----------
    def should_buy(self, candidates: List[dict], date: str, hour: int,
                   hour_data: dict, portfolio, day_data: pd.DataFrame = None) -> Optional[dict]:
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
            price = None
            if hour_data and code in hour_data:
                h = hour_data[code]
                # hour_data(hour=1)的'open'即为 hour1_open(=T日开盘价)
                price = h.get('hour1_open') or h.get('open')
            if price and price > 0:
                return {
                    'code': code,
                    'code_name': cand['code_name'],
                    'price': float(price),
                    'strategy_name': self.strategy_name,
                    'target_hold_days': self.target_hold_days,
                    'buy_reason': (f"低位首板 底部{cand['position'] * 100:.1f}% "
                                   f"高开{cand['open_rate']:+.1f}%"),
                }
        return None
