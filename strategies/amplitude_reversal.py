"""振幅强度选股策略 - 大上影冲高回落→次日低开反弹买入。

策略逻辑(方向2 - 已验证有效):
  信号: 昨日振幅强度2(上影-下影) >= 4% (冲高大幅回落, 上影线远长于下影线)
  条件: 今日开盘低开(open_rate < 0%) - T+1卖压释放后的恐慌性低开
  买入: 今日hour1 open (9:30开盘价)
  选股: amp2最大的top_n只中选turn最低的1只(低换手=筹码集中=弹性大)
  止盈: +5% (HIGH触发精确成交)
  止损: -25% (LOW触发精确成交) - 实质安全网(回测六年零触发, 亏损均由到期退出)
  到期: max_hold_hours=8 (hours_held>=8 且 hour==4 → D+2 hour4 close卖出)
  弱市到期提前(expam35, Task#204 2026-08-06用户批准转正):
    弱市日(D-1非ST涨停家数<35)买入的仓位, 到期出场提前至D+2早盘open
    (hours_held>=8后首个hour以open离场, gap先判TP/SL) — t165/t173 QA口径,
    弱市日隔夜持有到收盘期望为负, 早盘流动性窗口离场。

核心逻辑:
  昨日冲高回落(大上影线)→ 被套资金T+1才能卖 → 次日恐慌低开 → 卖压过度 → 反弹

回测结果(2021-01-01 ~ 2026-07-01, slot=1, 引擎含手续费, expam35口径):
  CAGR: +107.24% | 笔数: 503 | 胜率: 54.27% | MDD: 56.42% | final_nav 52,931,624.07
  分年: 2021:+88.83, 2022:+117.30, 2023:+4.27, 2024:+161.68, 2025:+318.90, 2026:+13.52
  (净化基线97.52→107.24, 64笔出场diff: 60笔expired时点迁移+4笔TP截胡,
   买入路径503笔零变化; 证据research/results/t173_s2_promote_qa/QA_REPORT.md)
"""
from typing import Optional

import trading_rules
from strategies.base import Strategy, Signal, SellSignal


class AmplitudeReversalStrategy(Strategy):
    """大上影冲高回落 → 次日低开反弹买入策略。"""
    name = "amplitude_reversal"
    max_hold_hours = 8           # 最多持有8小时(D+1全天)

    # === 核心参数(已通过网格搜索验证最优) ===
    buy_hour = 1                 # hour1 买入
    min_amp2_pct = 4.0           # 昨日振幅强度2最低门槛(%) - 上影线减去下影线
    max_open_rate = 0.0          # 今日必须低开(open_rate < 0)
    take_profit_pct = 0.05       # 止盈 +5% (HIGH触发)
    # [Task#40 2026-07-25 参数v2切换] SL -0.15→-0.25, 依据: Task#37重校准+QA放行报告
    # data/realtime/QA_AUDIT_S1S2_V2_REPORT.md (CAGR 87.63→93.05; -25%六年零触发=纯安全网,
    # 释放v1中9笔被-15%提前止损的仓位由到期机制退出)
    stop_loss_pct = -0.25        # 止损 -25% (LOW触发) - 极端安全网
    top_n = 5                    # 取amp2最大的前N只中turn最低的1只

    # 过滤条件
    min_turn = 1.0               # 最低换手率(%), 过滤僵尸股
    max_turn = 30.0              # 最高换手率(%), 过滤异常
    min_amount = 5000            # 最低成交额(万), 过滤流动性差的

    # [Task#204 2026-08-06 expam35转正] 弱市日历阈值: D-1非ST涨停家数<35
    weak_exit_threshold = 35

    def __init__(self):
        self._cand_codes = []
        self._cand_date = None
        self._weak_days = None   # 弱市日全集(懒加载, 仅回测数据源激活)

    def _ensure_weak_days(self, data_feed):
        """弱市日历懒加载: 仅backtest数据源运行时从stocks.db实时计算
        (与t173 QA A口径/PositionScaler._build逐位同源); 实盘数据源置空集,
        弱市分支inert(实盘AM离场由realtime侧ExitEngine落地, 边界隔离)。"""
        if self._weak_days is not None:
            return
        if type(data_feed).__module__.startswith('backtest'):
            from backtest.position_scale import compute_weak_market_days
            self._weak_days = frozenset(compute_weak_market_days(
                data_feed.db_path, self.weak_exit_threshold))
        else:
            self._weak_days = frozenset()

    @staticmethod
    def _is_st(info) -> bool:
        # ST双保险判定: isST字段 ∨ 名称含ST(大写化) ∨ 名称含'退'(退市整理期, Task#143对齐S3/S4的#136写法)
        if info.get('isST'):
            return True
        name = str(info.get('code_name') or '')
        if 'ST' in name.upper() or '退' in name:
            return True
        return False

    def get_candidates(self, date, data_feed):
        """筛选候选股 - 用prev_day计算大上影信号 + 今日低开过滤。"""
        snapshot = data_feed.get_market_snapshot(date, self.buy_hour)
        if not snapshot:
            self._cand_codes = []
            self._cand_date = date
            return []

        candidates = []

        for code, info in snapshot.items():
            # ST过滤
            if self._is_st(info):
                continue

            # 北交所过滤
            if code.startswith('bj.'):
                continue

            # 今日必须低开
            open_rate = info.get('open_rate') or 0.0
            if open_rate >= self.max_open_rate:
                continue

            # 今日不能涨停开盘
            open_price = info.get('open') or 0.0
            preclose = info.get('preclose') or 0.0
            if open_price <= 0 or preclose <= 0:
                continue
            # [Task#299] 涨停价统一trading_rules.limit_prices(Decimal ROUND_HALF_UP
            # 交易所口径), 替换round自算旁路(t292审计P0)
            if open_price >= trading_rules.limit_prices(code, preclose)[0]:
                continue

            # 获取前日OHLC
            prev_open = info.get('prev_open') or 0.0
            prev_high = info.get('prev_high') or 0.0
            prev_low = info.get('prev_low') or 0.0
            prev_close = info.get('prev_close') or 0.0
            prev_preclose = info.get('prev_preclose') or 0.0
            prev_turn = info.get('prev_turn') or 0.0
            prev_amount = info.get('prev_amount') or 0.0

            # 基本数据校验
            if prev_open <= 0 or prev_high <= 0 or prev_low <= 0:
                continue
            if prev_close <= 0 or prev_preclose <= 0:
                continue

            # 换手率过滤
            if prev_turn < self.min_turn or prev_turn > self.max_turn:
                continue

            # 成交额过滤(万)
            if prev_amount / 10000 < self.min_amount:
                continue

            # 计算振幅强度2 = (上影线 - 下影线) / preclose * 100
            body_top = max(prev_open, prev_close)
            body_bottom = min(prev_open, prev_close)
            upper_shadow = prev_high - body_top        # 上影线长度
            lower_shadow = body_bottom - prev_low      # 下影线长度
            amp2 = (upper_shadow - lower_shadow) / prev_preclose * 100

            # 门槛过滤
            if amp2 < self.min_amp2_pct:
                continue

            candidates.append({
                'code': code,
                'amp2': amp2,
                'turn': prev_turn,
                'open_rate': open_rate,
            })

        if not candidates:
            self._cand_codes = []
            self._cand_date = date
            return []

        # 排序: amp2降序取top_n, 再选turn最低
        candidates.sort(key=lambda x: -x['amp2'])
        top_cands = candidates[:self.top_n]
        top_cands.sort(key=lambda x: x['turn'])
        final = [top_cands[0]['code']]

        self._cand_codes = final
        self._cand_date = date
        return final

    def should_buy(self, code, date, hour, data_feed, portfolio):
        if hour != self.buy_hour:
            return None
        if date != self._cand_date or code not in self._cand_codes:
            return None

        # 防止同日再入场：本策略卖出当天不买新股（与快速模拟一致）
        # [Task#13修复] 原实现读共享组合全局trades[-1], 在多策略组合中被其他策略
        # 的卖出事件误拦截(B2-A每日H1卖出时S2笔数396→53); solo审计基线(507笔)
        # 即own-strategy语义, 修复后组合口径与solo/实盘框架三方一致
        own_trades = [t for t in portfolio.trades if t.strategy_name == self.name]
        if own_trades and own_trades[-1].sell_date == date:
            return None

        price = data_feed.get_hour_open(code, date, hour)
        if not price or price <= 0:
            return None
        return Signal(
            code=code,
            price=float(price),
            strategy_name=self.name,
            target_hold_hours=self.max_hold_hours,
        )

    def should_sell(self, position, date, hour, data_feed):
        # T+1合规：买入当日禁止卖出
        if position.buy_date == date:
            return None

        # [Task#204 expam35] 弱市日买入仓到期(hours_held>=8) → 首个hour以open
        # 离场(D+2早盘), gap先判TP/SL — 语义逐行对齐t173 QAExpAM(503笔diff=0)
        self._ensure_weak_days(data_feed)
        if (position.buy_date in self._weak_days
                and position.hours_held >= self.max_hold_hours):
            o = data_feed.get_hour_open(position.code, date, hour)
            if not o or o <= 0:
                return None
            pnl = (o - position.buy_price) / position.buy_price
            if pnl >= self.take_profit_pct:
                return SellSignal(reason='take_profit', price=float(o))
            if pnl <= self.stop_loss_pct:
                return SellSignal(reason='stop_loss', price=float(o))
            return SellSignal(reason='expired', price=float(o))

        buy_price = position.buy_price
        if buy_price <= 0:
            return None

        # 止盈/止损目标价
        tp_target = buy_price * (1 + self.take_profit_pct)
        sl_target = buy_price * (1 + self.stop_loss_pct)

        # 当前hour的HIGH/LOW/OPEN
        bar_high = data_feed.get_hour_high(position.code, date, hour)
        bar_low = data_feed.get_hour_low(position.code, date, hour)
        bar_open = data_feed.get_hour_open(position.code, date, hour)

        if not bar_open or bar_open <= 0:
            return None

        # 开盘价已触发止盈/止损 → 以open成交（gap跳空场景）
        open_pnl = (bar_open - buy_price) / buy_price
        if open_pnl >= self.take_profit_pct:
            return SellSignal(reason='take_profit', price=float(bar_open))
        if open_pnl <= self.stop_loss_pct:
            return SellSignal(reason='stop_loss', price=float(bar_open))

        # 盘中HIGH/LOW检测（保守：先检查止损）
        sl_hit = bar_low > 0 and bar_low <= sl_target
        tp_hit = bar_high > 0 and bar_high >= tp_target

        if sl_hit and tp_hit:
            return SellSignal(reason='stop_loss', price=float(sl_target))
        if sl_hit:
            return SellSignal(reason='stop_loss', price=float(sl_target))
        if tp_hit:
            return SellSignal(reason='take_profit', price=float(tp_target))

        # 到期: hours_held >= max_hold_hours 且 hour == 4 → close 卖
        if position.hours_held >= self.max_hold_hours and hour == 4:
            close_price = data_feed.get_hour_close(position.code, date, hour)
            sell_price = close_price if close_price and close_price > 0 else bar_open
            return SellSignal(reason='expired', price=float(sell_price))

        return None

    def get_buy_price(self, code, date, hour, data_feed):
        return data_feed.get_hour_open(code, date, hour)
