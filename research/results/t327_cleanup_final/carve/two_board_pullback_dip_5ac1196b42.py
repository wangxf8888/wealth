 ===')\n    top_all = all_df.head(20).copy()\n    top_all['收益均值'] = top_all['收益均值'].apply(lambda x: f'{x*100:.2f}%')\n    top_all['胜率'] = top_all['胜率'].apply(lambda x: f'{x*100:.1f}%')\n    top_all['alpha分'] = top_all['alpha分'].apply(lambda x: f'{x:.3f}')\n    print_table(top_all.reset_index(drop=True), '全量Top20（按综合alpha分排序）')\n    \n    # 结论总结\n    log('\\n' + '='*70)\n    log('  研究结论总结')\n    log('='*70)\n    log('\\n【板块特征】')\n    log('1. 创业板涨停次日溢价显著优于主板（创业板正收益, 主板负收益）')\n    log('2. 大跌后反弹: 创业板小市值表现最强, 科创板大市值表现也不错')\n    log('3. 高开冲高回落: 主板高开普遍回落, 创业板/科创板大市值相对抗跌')\n    log('4. 低开反弹: 科创板反弹概率和幅度最高, 尤其是大市值科创板')\n    log('\\n【市值特征】')\n    log('1. 大跌反弹信号: 小市值(\u003c30亿)收益最高但波动大')\n    log('2. 高开次日收益: 中小市值优于大市值')\n    log('3. 低开反弹: 科创板大市值异常强势')\n    log('\\n【最强信号组合】')\n    if len(strict) \u003e 0:\n        best = strict.iloc[0]\n        log(f'  {best[\"信号\"]} | {best[\"板块\"]} | {best[\"市值段\"]} | '\n            f'收益{best[\"收益均值\"]*100:.2f}% | 胜率{best[\"胜率\"]*100:.1f}% | '\n            f'样本{best[\"样本数\"]} | 年覆盖{best[\"年份覆盖\"]}')\n    elif len(relaxed) \u003e 0:\n        best = relaxed.iloc[0]\n        log(f'  {best[\"信号\"]} | {best[\"板块\"]} | {best[\"市值段\"]} | '\n            f'收益{best[\"收益均值\"]*100:.2f}% | 胜率{best[\"胜率\"]*100:.1f}% | '\n            f'样本{best[\"样本数\"]} | 年覆盖{best[\"年份覆盖\"]}')\n\n\ndef _year_coverage(sub):\n    \"\"\"统计多少个年份有正收益\"\"\"\n    yearly = sub.groupby(sub['date'].str[:4])['ret'].mean()\n    return (yearly \u003e 0).sum()\n\n\n# ============ 主流程 ============\ndef main():\n    log(f'{\"=\"*70}')\n    log(f'  板块×市值维度K线形态收益统计研究')\n    log(f'  运行时间: {datetime.now().strftime(\"%Y-%m-%d %H:%M:%S\")}')\n    log(f'  数据范围: {DATE_START} ~ {DATE_END}')\n    log(f'{\"=\"*70}')\n\n    conn = sqlite3.connect(DB_PATH)\n\n    try:\n        # 加载数据\n        df = load_data(conn)\n\n        # 添加次日数据\n        log('[预处理] 计算次日数据...')\n        df = add_next_day(df)\n        log(f'[预处理] 完成')\n\n        # 研究1-5\n        research_1_limitup_next_day(df)\n        research_2_bigdrop_next_day(df)\n        research_3_gapup(df)\n        research_4_gapdown(df)\n        research_5_gem_vs_main(df)\n\n        # 发现报告\n        generate_discovery_report(df)\n\n    finally:\n        conn.close()\n\n    # 写日志\n    flush_log()\n    log(f'\\n[完成] 日志已写入: {LOG_PATH}')\n\n\nif __name__ == '__main__':\n    main()\n","diffInfo":{"add":109,"delete":103,"sourceMd5":"080e6f1b0ca746e3343ba17fcc2b4899","targetMd5":"06528c7b44ef5d178ee6104fa1bb7683","addCounts":47,"delCounts":41,"modCounts":62,"addChars":3273,"delChars":274},"lastDiffInfo":{"add":109,"delete":103,"sourceMd5":"080e6f1b0ca746e3343ba17fcc2b4899","targetMd5":"06528c7b44ef5d178ee6104fa1bb7683","addCounts":47,"delCounts":41,"modCounts":62,"addChars":3273,"delChars":274},"mode":"MODIFIED"},"fileStatus":"APPLIED","errorMsg":"","errorCode":0,"message":"","messageCode":"","LengthLimit":false,"AppendMode":false,"FinalContent":""},"content":"edit file by SearchReplace success, file path: /home/AIWealth/scripts/research_sector_mcap.py\nline changes: +109 added, -103 removed\nalready to check the file, but find no code syntax errors\n","toolCallName":"search_replace","extraParameters":{"tool_call_meta":{"ai-coding/parent-tool-"""2板回调低吸策略 (B2-A) - Task#9 引擎精测。

形态(来自 Task#7 全维度网格复审, 研究口径见 data/realtime/task7_refine.txt):
  恰2板(最近涨停日连板数==2, 第2板非一字) → 断板回调1~2天(期间不再涨停)
  → 买入日竞价低开 -4% <= open_rate < 0 → H1_open 买入 → hold1 次日收盘卖出
排序: 更低开优先(open_rate升序) — task7_top1_probe: top1 +0.91%/日, 2021-2026六年全正

研究口径(池子级 hold1, 毛收益): n=5388 胜率48.4% 均值+0.67% 六年全正
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
