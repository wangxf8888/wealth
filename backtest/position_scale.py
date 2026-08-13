"""逐日仓位系数 - 冰点减仓overlay (Task#44, 默认不启用=零侵入)。

规则(Lee Task#41方向2, 引擎级落地):
  交易日t的买入系数 = w  若 D-1(严格早于t的最后一个stock_kline交易日)
                          非ST涨停家数 < ICE阈值
                   = 1.0 其余(含首日无D-1数据)
涨停判定 = trading_rules.is_at_limit_up(code, close, preclose), isST=0过滤
—— 与 scripts/research_emotion_cycle_indicator.py (Lee的emotion_cycle_daily.csv
生成口径)逐日核对一致(scripts/t44_caliber_check.py, 1558/1588天精确相等,
30个差异日全部为CSV陈旧: CSV生成于07-23, DB于07-25被Task#31回补重写)。

只影响买入金额(NAV/n_slots × 系数), 不影响卖出/已有持仓。
用法: PositionScaler(db_path, 'ice35:0.3') → get(date)

修复日豁免(2026-07-31批准, RESEARCH_REPAIR_GATE.md §4):
  spec加后缀 ':repair_exempt' (如 'ice35:0.3:repair_exempt') → 冰点日若同时
  是修复日则系数恢复1.0。修复日=W1(昨跌<=-3%)&W3(昨收20日低位30%)池
  ×竞价浅高开[2,3)×isST=0×上市>=25日×D-1额>=3000万×开盘可买, 当日池>=50只,
  9:25可见口径无未来数据; 运行时计算与 t85g 预注册30日清单逐日精确相等
  (scripts/stage_ab_test.py B2-1)。向后兼容: 无后缀行为与现行完全一致。
  置信度警示: 增量由仅15个豁免日/20笔研究样本驱动, 低统计置信度。
"""
import re
import sqlite3
import time
from datetime import datetime, timedelta

import trading_rules

# =============================================================================
# 修复日定义参数 (RESEARCH_REPAIR_GATE.md §1 预注册主口径, 禁止漂移;
# 实盘侧 realtime/morning_decision 同源import, 回测/实盘单一来源)
# =============================================================================
REPAIR_GATE = 50          # 池规模阈值(主口径; 邻域40/60已验稳健)
REPAIR_OPEN_LO = 2.0      # 竞价浅高开下界(含)
REPAIR_OPEN_HI = 3.0      # 上界(不含; [2,4)参考2023有踩雷, 守[2,3))
REPAIR_MIN_HIST = 25      # 上市>=25交易日(含当日)
REPAIR_MIN_AMT = 30000000  # D-1成交额>=3000万
REPAIR_W1_DROP = -3.0     # W1: 昨日close_rate<=-3%
REPAIR_W3_POS = 0.3       # W3: 昨收位于20日区间低位30%
# 置信度警示(审批包随附, 实盘侧写入decision json留痕)
REPAIR_CONFIDENCE_NOTE = ('增量由仅15个豁免日/20笔研究样本驱动, 低统计置信度; '
                          '历史零踩雷不代表未来零踩雷(RESEARCH_REPAIR_GATE §3)')


def repair_open_buyable(code: str, open_p: float, preclose: float) -> bool:
    """开盘可买(非一字涨跌停开盘)。

    [Task#299] 涨跌停价统一trading_rules.limit_prices(Decimal ROUND_HALF_UP
    交易所口径), 替换t85g研究同款float round自算; 尤其消除
    round(preclose*(2-ratio),2)代数变形double坑(2-1.1=0.8999...,
    Colm#289勘误同型, t292审计P0) — 跌停价改直乘。修复池isST=0已在
    SQL过滤, 此处按非ST口径(创科±20%/北交±30%/主板±10%, 含sh.689对齐)。
    """
    lim_up, lim_dn = trading_rules.limit_prices(code, preclose)
    return lim_dn + 0.01 < open_p < lim_up - 0.01


# 研究口径SQL(scripts/t85g_repair_gate.py Q 去pandas+参数化, 主口径[2,3)):
# 当日行锚定, LAG取昨日特征, 窗口=截至昨日20日区间, nhist=上市深度(含当日)。
_REPAIR_Q_BACKTEST = """
WITH b AS (
  SELECT date, code, preclose, open, open_rate,
         LAG(close_rate)  OVER w AS p_cr,
         LAG(close)       OVER w AS p_close,
         LAG(amount)      OVER w AS p_amt,
         MIN(low) OVER (PARTITION BY code ORDER BY date
                        ROWS BETWEEN 20 PRECEDING AND 1 PRECEDING) AS low20,
         MAX(high) OVER (PARTITION BY code ORDER BY date
                        ROWS BETWEEN 20 PRECEDING AND 1 PRECEDING) AS high20,
         COUNT(*) OVER (PARTITION BY code ORDER BY date
                        ROWS BETWEEN 24 PRECEDING AND CURRENT ROW) AS nhist,
         isST
  FROM stock_kline
  WHERE date >= :warmup
  WINDOW w AS (PARTITION BY code ORDER BY date)
)
SELECT date, code, preclose, open
FROM b
WHERE date >= :start AND date <= :end
  AND isST = 0 AND nhist >= :min_hist
  AND open_rate >= :open_lo AND open_rate < :open_hi
  AND preclose > 0 AND open > 0 AND p_close > 0 AND p_amt >= :min_amt
  AND p_cr <= :w1_drop
  AND high20 > 0 AND p_close <= low20 + (high20 - low20) * :w3_pos
"""


def compute_repair_days(conn, start_date: str, end_date: str,
                        gate: int = REPAIR_GATE) -> set:
    """区间内修复日全集(运行时计算, 不依赖预注册清单)。

    与scripts/t85g_repair_gate.py研究实现逐位对齐(预热窗>=25交易日即与
    研究固定'2020-11-01'预热等价, 此处取120自然日)。2021-01-01~2026-07-23
    区间实测与t85g预注册30日清单逐日精确相等。
    """
    warmup = (datetime.strptime(start_date, '%Y-%m-%d')
              - timedelta(days=120)).strftime('%Y-%m-%d')
    counts = {}
    for date, code, preclose, open_p in conn.execute(_REPAIR_Q_BACKTEST, {
            'warmup': warmup, 'start': start_date, 'end': end_date,
            'min_hist': REPAIR_MIN_HIST, 'open_lo': REPAIR_OPEN_LO,
            'open_hi': REPAIR_OPEN_HI, 'min_amt': REPAIR_MIN_AMT,
            'w1_drop': REPAIR_W1_DROP, 'w3_pos': REPAIR_W3_POS}):
        if repair_open_buyable(code, open_p, preclose):
            counts[date] = counts.get(date, 0) + 1
    return {d for d, n in counts.items() if n >= gate}


def compute_weak_market_days(db_path: str, threshold: int = 35) -> set:
    """弱市日全集: 交易日t满足 D-1(前一交易日)非ST涨停家数 < threshold。

    S2巨振反转expam35弱市日历(Task#204转正落地, 用户2026-08-06批准):
    运行时从stocks.db实时计算, 不依赖静态json; 涨停计数口径与
    PositionScaler._build逐位同源(t173 QA A口径全期1352天0 diff实锤)。
    """
    t0 = time.time()
    conn = sqlite3.connect(f'file:{db_path}?mode=ro', uri=True)
    dates = [r[0] for r in conn.execute(
        "SELECT DISTINCT date FROM stock_kline ORDER BY date")]
    weak, prev_cnt = set(), None
    for i, d in enumerate(dates):
        cnt = 0
        for code, close, preclose in conn.execute(
                "SELECT code, close, preclose FROM stock_kline "
                "WHERE date=? AND isST=0", (d,)):
            if trading_rules.is_at_limit_up(code, close, preclose):
                cnt += 1
        if i > 0 and prev_cnt < threshold:
            weak.add(d)
        prev_cnt = cnt
    conn.close()
    print(f"[weak-days] 弱市日(D-1非ST涨停家数<{threshold}): {len(weak)}天"
          f"/共{len(dates)}交易日 耗时{time.time() - t0:.1f}s")
    return weak


class PositionScaler:
    """预计算 {交易日: 买入系数}。spec格式: 'ice<阈值>:<系数>[:repair_exempt]'。"""

    def __init__(self, db_path: str, spec: str,
                 start_date: str = None, end_date: str = None):
        m = re.fullmatch(r'ice(\d+):(0?\.\d+|1(\.0+)?)(:repair_exempt)?',
                         spec.strip().lower())
        if not m:
            raise ValueError(
                f"--position-scale 格式错误: {spec!r} (期望 "
                f"ice<阈值>:<系数>[:repair_exempt], 如 ice35:0.3 或 "
                f"ice35:0.3:repair_exempt)")
        self.ice_threshold = int(m.group(1))
        self.weight = float(m.group(2))
        self.repair_exempt = bool(m.group(4))
        self.spec = spec
        self._scale = {}           # date -> 系数
        self._limitup = {}         # date -> 当日涨停家数(供诊断)
        self._ice_days = set()     # 冰点日全集(含修复豁免日, 供boost抑制判定)
        self.n_scaled_days = 0
        self.n_exempt_days = 0     # 修复日豁免命中天数(冰点∩修复)
        self._build(db_path, start_date, end_date)

    def _build(self, db_path: str, start_date: str, end_date: str):
        t0 = time.time()
        conn = sqlite3.connect(f'file:{db_path}?mode=ro', uri=True)
        dates = [r[0] for r in conn.execute(
            "SELECT DISTINCT date FROM stock_kline ORDER BY date")]
        if start_date:
            # D-1需要区间首日的前一交易日 → 多取1天
            i = next((k for k, d in enumerate(dates) if d >= start_date), 0)
            dates = dates[max(0, i - 1):]
        if end_date:
            dates = [d for d in dates if d <= end_date]

        for d in dates:
            cnt = 0
            for code, close, preclose in conn.execute(
                    "SELECT code, close, preclose FROM stock_kline "
                    "WHERE date=? AND isST=0", (d,)):
                if trading_rules.is_at_limit_up(code, close, preclose):
                    cnt += 1
            self._limitup[d] = cnt

        # 修复日豁免(2026-07-31批准): 启用时运行时计算区间修复日全集
        repair_days = set()
        if self.repair_exempt and dates:
            repair_days = compute_repair_days(conn, dates[0], dates[-1])
        conn.close()

        # 交易日t → D-1(前一交易日)的涨停家数决定系数
        for i, d in enumerate(dates):
            if i == 0:
                self._scale[d] = 1.0    # 无D-1数据 → 满仓(与Lee回放一致)
                continue
            prev_cnt = self._limitup[dates[i - 1]]
            if prev_cnt < self.ice_threshold:
                # 冰点日语义(t174 ENGINE_REPORT §8.1): 豁免日系数=1.0但仍是冰点日
                self._ice_days.add(d)
                if d in repair_days:
                    # 冰点∩修复 → 豁免恢复满仓(语义=t85g引擎实测口径)
                    self._scale[d] = 1.0
                    self.n_exempt_days += 1
                else:
                    self._scale[d] = self.weight
                    self.n_scaled_days += 1
            else:
                self._scale[d] = 1.0
        print(f"[position-scale] {self.spec}: 预计算{len(dates)}个交易日 "
              f"耗时{time.time() - t0:.1f}s | 触发减仓{self.n_scaled_days}天 "
              f"(D-1涨停家数<{self.ice_threshold} → 系数{self.weight})"
              + (f" | 修复日豁免{self.n_exempt_days}天(恢复满仓)"
                 if self.repair_exempt else ""))

    def get(self, date: str) -> float:
        """交易日买入系数。日期不在表内(理论不应发生)按满仓处理。"""
        return self._scale.get(date, 1.0)

    def is_ice_day(self, date: str) -> bool:
        """冰点日判定(D-1非ST涨停家数<阈值, 含修复豁免日仍属冰点日语义)。

        供drawdown-boost抑制判定(Task#204): 豁免日_scale=1.0不能代替本判定。
        """
        return date in self._ice_days
