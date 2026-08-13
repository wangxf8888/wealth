"""Task#319 阶段2: 门控日历快速验证(官方转正跑前的快速失败保护)。

验证项:
  V1. backtest/promo_gate.compute_indicator_rows vs t311 indicators_daily.csv
      逐位diff(重叠区间, 即CSV覆盖的全部日期)。
  V2. compute_gate_days(P70) ∩ 引擎交易日(2021-01-01~2026-07-01)
      = G1终格131天, 分年 {2021:4, 2022:34, 2023:15, 2024:43, 2025:28, 2026:7}
      逐位一致(t250/t311断言口径)。
任何不一致 → 退出码1, 停手上报。sqlite ro。
"""
import csv
import sys
from collections import Counter

sys.path.insert(0, '/home/AIWealth')
from backtest.promo_gate import compute_indicator_rows, compute_gate_days  # noqa: E402

DB = '/home/AIWealth/data/stocks.db'
CSV = '/home/AIWealth/research/results/t311_promo_menu/indicators_daily.csv'

# ---------- V1: 指标逐位diff ----------
with open(CSV) as f:
    csv_rows = {r['date']: r for r in csv.DictReader(f)}
mem_rows = {r['date']: r for r in compute_indicator_rows(DB)}

missing = sorted(set(csv_rows) - set(mem_rows))
if missing:
    print(f"FAIL V1: 内存重算缺失CSV日期 {len(missing)}个, 首例 {missing[:3]}")
    sys.exit(1)

diffs = []
for d, cr in csv_rows.items():
    mr = mem_rows[d]
    for col in ('promo_rate', 'tail_mean_h4'):
        if (cr[col] or '') != (mr[col] or ''):
            diffs.append((d, col, cr[col], mr[col]))
if diffs:
    print(f"FAIL V1: 指标逐位diff {len(diffs)}处, 首例 {diffs[:5]}")
    sys.exit(1)
print(f"PASS V1: 指标行 {len(csv_rows)}日 x 2列 与t311 CSV逐位一致 "
      f"(内存重算总日数{len(mem_rows)}, CSV外新增日期不影响窗口内expanding)")

# ---------- V2: G1终格日历断言 ----------
gate_days = compute_gate_days(DB, 0.70)
from backtest.run_unified import DB_PATH  # noqa: E402
assert DB_PATH == DB, f"DB_PATH不一致: {DB_PATH}"
from backtest.data_feed import BacktestDataFeed  # noqa: E402
feed = BacktestDataFeed(DB)
eng_dates = set(feed.get_trading_dates('2021-01-01', '2026-07-01'))
eff = sorted(gate_days & eng_dates)
by_year = Counter(d[:4] for d in eff)

EXPECT_TOTAL = 131
EXPECT_YEAR = {'2021': 4, '2022': 34, '2023': 15, '2024': 43,
               '2025': 28, '2026': 7}
if len(eff) != EXPECT_TOTAL or dict(by_year) != EXPECT_YEAR:
    print(f"FAIL V2: 窗口内门控日 {len(eff)}天 分年{dict(by_year)} "
          f"!= G1终格 {EXPECT_TOTAL}天 {EXPECT_YEAR}")
    sys.exit(1)
print(f"PASS V2: 门控日历∩引擎交易日 = {len(eff)}天, "
      f"分年 {dict(sorted(by_year.items()))} 逐位=G1终格")
print("ALL PASS — 可启动官方转正跑")
