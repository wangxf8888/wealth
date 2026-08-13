"""晋级率过热门控 overlay — 生产默认化 (Task#319, 用户2026-08-12批准方案C)。

规格冻结链(一字不改, 禁止任何再优化):
  #244 G0(promo通过) → #249 G1(终格P70_w0.3, ice>gate>boost语义裁决)
  → #250 引擎终测(GateScaler注入, G1日历131天逐位) → #311 新锚重测格A/格C联测
  → #319 本文件生产默认化。
语义: 昨日首板晋级率promo_rate与全市场尾盘均值tail_mean_h4 双双落入各自
  expanding P70分位(warmup 120, 仅<=D历史无未来) → D+1过热日, 新开仓×0.3;
  优先级 ice > gate > boost(gate日不被drawdown-boost覆盖, 引擎native分支:
  run_unified仅buy_scale>=1.0才覆盖为w)。
指标口径 = t311_indicators.py 纯SQL(承t250_qa已验证链路, 与t249 CSV逐字段一致):
  promo_rate(D)   = D-1首板(D-1涨停∩D-2未涨停)中D再涨停占比, 6位小数
  tail_mean_h4(D) = 全市场hour4均值涨幅(同宇宙过滤), 4位小数
  历史起点2021-01-01冻结(t311 CSV同口径); 精度经字符串格式化对齐原CSV逐位。
门控日历 = t311_runner.expanding_trig逐行一致(D+1作用日)。
kill线(t249转正包): 滚动6月分化>0停用 / 转负连续3月复活。
红线: sqlite全程ro只读。
用法: run_unified CLI默认 --promo-gate p70:0.3, 传 off/none 关闭
  (solo纯策略口径必须显式关闭; API默认None=向后兼容零侵入)。
"""
import re
import sqlite3
import time

# 研究口径涨停集SQL(与t249 qa_audit/t250_qa/t311_indicators逐字一致:
# ST三重剔除+剔bj)
LU = ("""SELECT code FROM stock_kline WHERE date=? AND preclose>0 AND close>0
 AND isST=0 AND upper(code_name) NOT LIKE '%ST%' AND code_name NOT LIKE '%退%'
 AND code NOT LIKE 'bj.%'
 AND close >= round(preclose*(CASE WHEN code LIKE 'sz.30%' OR code LIKE 'sh.688%'
     OR code LIKE 'sh.689%' THEN 1.20 ELSE 1.10 END),2)-0.001""")

HIST_START = '2021-01-01'   # 指标/expanding历史起点(t244→t311冻结, 禁漂移)
WARMUP = 120                # expanding暖启动样本数(t249/t250/t311冻结)


def compute_indicator_rows(db_path: str):
    """日频指标行 [{date, promo_rate, tail_mean_h4}, ...] (值为格式化字符串,
    与t311_indicators.py产出CSV精度逐位一致: promo 6位/tail 4位/缺失='')。"""
    conn = sqlite3.connect(f'file:{db_path}?mode=ro', uri=True)
    dates = [r[0] for r in conn.execute(
        "SELECT DISTINCT date FROM stock_kline WHERE date>=? ORDER BY date",
        (HIST_START,))]
    lu_cache = {}

    def lu(d):
        if d not in lu_cache:
            lu_cache[d] = {r[0] for r in conn.execute(LU, (d,))}
        return lu_cache[d]

    rows = []
    for D in dates:
        d1, d2 = [r[0] for r in conn.execute(
            "SELECT DISTINCT date FROM stock_kline WHERE date<? "
            "ORDER BY date DESC LIMIT 2", (D,))]
        fb = lu(d1) - lu(d2)
        promo = f"{len(fb & lu(D)) / len(fb):.6f}" if fb else ''
        tm = conn.execute(
            "SELECT AVG((hour4_close/hour4_open-1)*100) FROM stock_kline "
            "WHERE date=? AND hour3_open>0 AND hour4_open>0 AND isST=0 "
            " AND code_name NOT LIKE '%ST%' AND code_name NOT LIKE '%退%' "
            " AND code NOT LIKE 'bj.%'", (D,)).fetchone()[0]
        rows.append({'date': D, 'promo_rate': promo,
                     'tail_mean_h4': f"{tm:.4f}" if tm is not None else ''})
    conn.close()
    return rows


def pctl(sv, q):
    return sv[min(len(sv) - 1, int(q * len(sv)))]


def expanding_trig(rows, col, q):
    """与t250_runner/t311_runner.expanding_trig逐行一致: expanding Pq无未来
    触发(warmup 120, 仅<=D历史), 返回D+1作用日集。"""
    rows = [r for r in rows if r['date'] >= HIST_START]
    ds = [r['date'] for r in rows]
    nxt = {ds[i]: ds[i + 1] for i in range(len(ds) - 1)}
    sample = [r for r in rows if r[col] != '' and r['date'] in nxt]
    hist, trig = [], set()
    for r in sample:
        x = float(r[col])
        if len(hist) >= WARMUP and x >= pctl(sorted(hist), q):
            trig.add(nxt[r['date']])
        hist.append(x)
    return trig


def compute_gate_days(db_path: str, q: float) -> set:
    """P{q}交集门控作用日历(D+1): promo_rate与tail_mean_h4双触发交集。"""
    rows = compute_indicator_rows(db_path)
    return expanding_trig(rows, 'promo_rate', q) \
        & expanding_trig(rows, 'tail_mean_h4', q)


class PromoGateScaler:
    """逐日仓位系数overlay: ice > gate 优先级(get逻辑逐字承t250/t311
    GateScaler); gate > boost由引擎native分支成立(run_unified仅
    buy_scale>=1.0才覆盖为w)。inner=PositionScaler(冰点overlay)或None。

    spec格式: 'p<分位>:<系数>' (生产实配 'p70:0.3' = expanding P70交集
    过热日新开仓×0.3, 冻结参数禁止再优化)。
    """

    def __init__(self, db_path: str, spec: str, inner=None):
        m = re.fullmatch(r'p(\d+):(0?\.\d+|1(\.0+)?)', spec.strip().lower())
        if not m:
            raise ValueError(
                f"--promo-gate 格式错误: {spec!r} (期望 p<分位>:<系数>, "
                f"如生产实配 p70:0.3)")
        self.gate_q = int(m.group(1)) / 100.0
        self.gate_w = float(m.group(2))
        self.spec = spec
        self.inner = inner              # PositionScaler(ice) 或 None
        t0 = time.time()
        self.gate_days = frozenset(compute_gate_days(db_path, self.gate_q))
        self.gate_eff_days = []         # gate实际生效日(非冰点减仓日)
        self.gate_ice_overlap = []      # gate∩冰点减仓日(ice优先, gate让位)
        print(f"[promo-gate] {spec}: 指标重算+expanding日历构建完成 "
              f"耗时{time.time() - t0:.1f}s | 全历史过热作用日"
              f"{len(self.gate_days)}天 (P{int(self.gate_q * 100)}交集, "
              f"warmup{WARMUP}, 起点{HIST_START}, D+1新开仓×{self.gate_w})")

    def get(self, date: str) -> float:
        base = self.inner.get(date) if self.inner is not None else 1.0
        if base < 1.0:                       # 冰点减仓日: ice最高优先
            if date in self.gate_days:
                self.gate_ice_overlap.append(date)
            return base
        if date in self.gate_days:           # gate日(含冰点豁免日): ×w
            self.gate_eff_days.append(date)
            return self.gate_w
        return 1.0

    def is_ice_day(self, date: str) -> bool:
        """冰点日判定透传(供引擎drawdown-boost抑制, 语义与PositionScaler同)。"""
        return self.inner.is_ice_day(date) if self.inner is not None else False
