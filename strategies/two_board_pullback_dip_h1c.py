"""B2-A 卖点前移变体 (Task#12): D2 收盘卖 → D2 H1收盘卖(多吃H1正时段)。

依据: Lee时段结构(H1唯一系统性正时段, H3最差) — 持仓到收盘吃满负时段。
D2 H1 收盘了结 → sell_day_no_buy=False → slot 当小时复用(off=1), 捕获率100%。
粗测代理: 净CAGR~ +94.0% vs 对照 +76.6%, 笔数 647→1270, 2022 -13%→+1.3%。

引擎精测(2021-01~2026-07-23, slot=1, 干净数据, 含成本): **CAGR +107.79%** 达标
  | MDD 79.92% | 1250笔 胜率46.40% +0.56%/笔 | 对照(hold1)同数据 +61.36%
  | 分年 21:+111 22:+113 23:-0.9 24:+0.1 25:+306 26:+206 — 2022 -33%→+113% 修复
  | slot周转假设成立: 笔数 641→1250(1.95x), 94%买入日=卖出日(当日slot复用)
  | 邻域无断崖: 日内alpha逐小时递增边际递减(h1c优势来自slot周转结构而非时点尖点)
  详见 data/realtime/task12_engine_report.txt
"""
from strategies.base import SellSignal
from strategies.two_board_pullback_dip import TwoBoardPullbackDipStrategy


class TwoBoardPullbackDipH1cStrategy(TwoBoardPullbackDipStrategy):
    name = "two_board_pullback_dip_h1c"
    exit_mode = 'd2h1_close'
    max_hold_hours = 5           # D1 h1 买入 → D2 h1 = 5小时
    sell_day_no_buy = False      # 卖出当小时/当日可再买(slot周转核心开关)
    # [Task#63 F2升级, 2026-07-29落地/07-30生效, 用户已批准] 第2板必须
    # H1内首触涨停(晚板毒区剔除): solo +99.23%→+211.27%, 训盲双升,
    # 依据 data/realtime/RESEARCH_B2A_FAILURE_ANATOMY.md,
    # QA放行 data/realtime/QA_AUDIT_B2A_F2_REPORT.md 。回滚=置 None
    max_seal_hour_b2 = 1

    # [Task#319 h1_touch转正, 2026-08-12用户批准方案C] H1限定touch窄门:
    # 仅D2(卖出日)H1内触板(hour1_high>=limit_prices涨停价-0.001)卖板价兑现;
    # 其余全部super()=现行d2h1_close逐字路径(H1收盘卖/deferred兜底),
    # slot当小时复购结构完整保留。防未来: 触板=H1内已发生事件, 卖价=板价。
    # 规格源(一字不改)=t307_engine_runner.S5H1Touch(Task#310 PASS分支,
    # METHOD_REPORT§8); 引擎级锚(t311格C): 组合182.22/20.25/Calmar9.00/2893笔,
    # S5笔数守恒。回滚=删除本 should_sell 覆盖(退回d2h1_close逐字路径)。
    # 实盘侧同步由Task#320落地(realtime卖出走position_tracker自有路径,
    # 本覆盖仅回测引擎调用)。
    def should_sell(self, position, date, hour, data_feed):
        if position.buy_date < date and hour == 1 \
                and data_feed._prev_trading_date(date) == position.buy_date:
            limit_up, _ = data_feed.get_limit_prices(position.code, date)
            hi = data_feed.get_hour_high(position.code, date, 1)
            if limit_up > 0 and hi and hi >= limit_up - 0.001:
                return SellSignal(reason='touch_board', price=float(limit_up))
        return super().should_sell(position, date, hour, data_feed)
