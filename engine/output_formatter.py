"""结构化输出格式化器"""
import sys


# Hour时段映射
HOUR_TIME_MAP = {
    1: '9:30-10:00',
    2: '10:00-11:00',
    3: '13:00-14:00',
    4: '14:00-15:00',
}


class OutputFormatter:
    """回测结果结构化输出"""

    def __init__(self, output_path: str = None):
        self.output_path = output_path
        self.f = None
        if output_path:
            self.f = open(output_path, 'w', encoding='utf-8')
        self.initial_capital = 0
        self._line_count = 0
        self._progress_interval = 50  # 每50个交易日在终端打印一次进度

    def set_initial_capital(self, capital: float):
        self.initial_capital = capital

    def _write(self, text: str):
        """写入输出文件"""
        if self.f:
            self.f.write(text + '\n')
            self.f.flush()

    def print_hour_summary(self, date: str, hour: int, portfolio, hour_data: dict,
                           just_bought_ids: set = None):
        """
        输出每小时摘要
        hour_data: {code: {open, close, high, low, volume, amount, close_rate, open_rate, high_rate, low_rate, preclose}}
        just_bought_ids: 本hour刚执行买入的slot_id集合
        """
        if just_bought_ids is None:
            just_bought_ids = set()

        time_range = HOUR_TIME_MAP.get(hour, '')
        lines = [f"--- {date} Hour{hour} ({time_range}) ---"]

        hour_pnl_total = 0.0
        occupied_count = 0

        for slot in portfolio.slots:
            slot_label = f"[仓{slot.slot_id + 1}]"
            if slot.is_empty:
                lines.append(f"{slot_label} 空仓 (可用资金: {portfolio.cash / max(1, portfolio.n_slots - len(portfolio.occupied_slots())):,.0f})")
            else:
                occupied_count += 1
                buy_date_short = slot.buy_date[5:] if slot.buy_date else '??'

                # 问题3：区分"买入"和"持有"
                if slot.slot_id in just_bought_ids:
                    action_label = "★买入"
                else:
                    action_label = "持有"

                header = f"{slot_label} [{action_label}] {slot.code} {slot.code_name} | 买入:{slot.buy_price:.2f}({buy_date_short} H{slot.buy_hour})"

                stock_h = hour_data.get(slot.code, {})
                h_open = stock_h.get('open', 0)
                h_close = stock_h.get('close', 0)
                h_high = stock_h.get('high', 0)
                h_low = stock_h.get('low', 0)
                # rate字段
                h_open_rate = stock_h.get('open_rate', 0)
                h_close_rate = stock_h.get('close_rate', 0)
                h_high_rate = stock_h.get('high_rate', 0)
                h_low_rate = stock_h.get('low_rate', 0)

                if h_close and h_close > 0:
                    # 本hour收益(相对买入价)
                    hold_pnl = (h_close / slot.buy_price - 1) * 100

                    # 问题4：显示rate而非绝对价格
                    ohlc_line = (f"      Hour{hour} OHLC_rate: "
                                 f"O={h_open_rate:+.2f}% C={h_close_rate:+.2f}% "
                                 f"H={h_high_rate:+.2f}% L={h_low_rate:+.2f}%")
                    pnl_line = f"      本hour涨跌: {h_close_rate:+.2f}% | 持仓盈亏: {hold_pnl:+.2f}%"
                    lines.append(header)
                    lines.append(ohlc_line)
                    lines.append(pnl_line)
                    hour_pnl_total += hold_pnl
                else:
                    lines.append(header)
                    lines.append(f"      Hour{hour}: 无数据")

        if occupied_count > 0:
            avg_pnl = hour_pnl_total / portfolio.n_slots
            lines.append(f"整体持仓盈亏: {avg_pnl:+.2f}% ({occupied_count}/{portfolio.n_slots}仓位)")

        # 写入文件
        for line in lines:
            self._write(line)
        self._write("")

    def print_candidates(self, signal_date: str, candidates: list, buy_module,
                          selected_code: str = None, market_ok: bool = True,
                          market_threshold: float = None, market_rate: float = None,
                          buy_hour: int = 1):
        """打印某信号日的候选股列表（仅当buy_module实现describe_candidate时调用）

        格式：
        === {signal_date} 炸板信号候选 (共X只) [大盘过滤-不交易] ===
          #1 sh.xxx 名称 | close/high=... | 换手:... | 振幅:...
          ...
          → 选中 #1（T+1 买入）  或  → 大盘H1<X%, 不买入
        """
        if not candidates:
            return
        if not hasattr(buy_module, 'describe_candidate'):
            return

        n = len(candidates)
        if not market_ok:
            header = f"=== {signal_date} 炸板信号候选 (共{n}只) [大盘过滤-不交易] ==="
        else:
            header = f"=== {signal_date} 炸板信号候选 (共{n}只) ==="
        self._write(header)

        selected_idx = None
        for idx, cand in enumerate(candidates, 1):
            desc = buy_module.describe_candidate(cand)
            self._write(f"  #{idx} {desc}")
            if selected_code is not None and cand.get('code') == selected_code and selected_idx is None:
                selected_idx = idx

        if not market_ok:
            rate_str = f"{market_rate:+.2f}%" if market_rate is not None else "无数据"
            thr_str = f"{market_threshold:+.1f}%" if market_threshold is not None else ""
            self._write(f"  → 大盘H{buy_hour}={rate_str} (阈值{thr_str}), 不买入")
        elif selected_idx is not None:
            self._write(f"  → 选中 #{selected_idx}（T+1 买入）")
        else:
            self._write(f"  → 无可买入候选（已持仓或被过滤）")
        self._write("")

    def print_trade(self, trade_type: str, slot, signal_or_price, date: str, hour: int):
        """打印单笔交易"""
        slot_label = f"[仓{slot.slot_id + 1}]"
        if trade_type == 'buy':
            self._write(f">>> 买入 {slot_label} {signal_or_price['code']} {signal_or_price['code_name']} "
                        f"@ {signal_or_price['price']:.2f} | 跳空:{signal_or_price.get('open_rate', 0):.1f}% "
                        f"换手:{signal_or_price.get('turn', 0):.2f}% {date} H{hour}")
        elif trade_type == 'sell':
            if isinstance(signal_or_price, dict):
                pnl = signal_or_price.get('pnl_pct', 0)
                self._write(f"<<< 卖出 {slot_label} {signal_or_price['code']} {signal_or_price['code_name']} "
                            f"@ {signal_or_price['sell_price']:.2f} | 盈亏:{pnl:+.2f}% {date} H{hour}")

    def print_daily_summary(self, date: str, portfolio, trades_today: list, day_index: int = 0):
        """
        输出日终总结
        """
        self._write(f"{'=' * 50}")
        self._write(f"=== {date} 日终总结 ===")

        # 获取当前价格用于计算NAV
        current_prices = {}
        for slot in portfolio.slots:
            if slot.is_occupied:
                current_prices[slot.code] = slot.buy_price  # 使用买入价作为兜底

        nav = portfolio.get_nav(current_prices)
        cum_return = (nav / portfolio.initial_capital - 1) * 100
        nav_ratio = nav / portfolio.initial_capital

        for slot in portfolio.slots:
            slot_label = f"[仓{slot.slot_id + 1}]"
            if slot.is_empty:
                self._write(f"{slot_label} 空仓")
            else:
                hold_days = 1  # 简化
                pnl = 0.0
                self._write(f"{slot_label} {slot.code} {slot.code_name} | 买入:{slot.buy_price:.2f}")

        buy_count = sum(1 for t in trades_today if t[0] == 'buy')
        sell_count = sum(1 for t in trades_today if t[0] == 'sell')
        self._write(f"今日操作: 买入{buy_count}笔 卖出{sell_count}笔")
        self._write(f"累计净值: {nav_ratio:.4f} | 累计收益: {cum_return:+.2f}% | 总资产: {nav:,.2f}")
        self._write(f"{'=' * 50}")
        self._write("")

        # 记录NAV
        portfolio.record_nav(date, nav)

        # 终端进度输出（每N天打印一次）
        self._line_count += 1
        if self._line_count % self._progress_interval == 0:
            print(f"  [{self._line_count}天] {date} | 净值:{nav_ratio:.4f} | 收益:{cum_return:+.2f}% | 交易:{buy_count}买{sell_count}卖")

    def print_final_summary(self, portfolio):
        """最终统计：总收益、年化、胜率、最大回撤等"""
        all_trades = portfolio.all_trades
        sell_trades = [t for t in all_trades if t['type'] == 'sell']

        nav_history = portfolio.nav_history
        if not nav_history:
            self._write("无交易记录")
            return

        final_nav = nav_history[-1]['nav']
        total_return = (final_nav / portfolio.initial_capital - 1) * 100

        # 年化收益
        start_date = nav_history[0]['date']
        end_date = nav_history[-1]['date']
        from datetime import datetime
        d_start = datetime.strptime(start_date, '%Y-%m-%d')
        d_end = datetime.strptime(end_date, '%Y-%m-%d')
        years = (d_end - d_start).days / 365.25
        if years > 0:
            annualized = ((final_nav / portfolio.initial_capital) ** (1 / years) - 1) * 100
        else:
            annualized = 0

        # 胜率
        win_trades = [t for t in sell_trades if t['pnl_pct'] > 0]
        lose_trades = [t for t in sell_trades if t['pnl_pct'] <= 0]
        total_sell = len(sell_trades)
        win_rate = len(win_trades) / total_sell * 100 if total_sell > 0 else 0

        # 平均盈亏
        avg_win = sum(t['pnl_pct'] for t in win_trades) / len(win_trades) if win_trades else 0
        avg_lose = sum(t['pnl_pct'] for t in lose_trades) / len(lose_trades) if lose_trades else 0

        # 最大回撤
        max_drawdown = 0
        peak_nav = portfolio.initial_capital
        for record in nav_history:
            if record['nav'] > peak_nav:
                peak_nav = record['nav']
            dd = (peak_nav - record['nav']) / peak_nav * 100
            if dd > max_drawdown:
                max_drawdown = dd

        # 盈亏比
        profit_loss_ratio = abs(avg_win / avg_lose) if avg_lose != 0 else float('inf')

        self._write("")
        self._write("=" * 60)
        self._write("========== 最终回测统计 ==========")
        self._write("=" * 60)
        self._write(f"回测区间: {start_date} ~ {end_date} ({len(nav_history)}个交易日)")
        self._write(f"初始资金: {portfolio.initial_capital:,.2f}")
        self._write(f"最终资产: {final_nav:,.2f}")
        self._write(f"总收益率: {total_return:+.2f}%")
        self._write(f"年化收益: {annualized:+.2f}%")
        self._write(f"最大回撤: {max_drawdown:.2f}%")
        self._write(f"")
        self._write(f"总交易笔数: {total_sell}笔 (买卖各计1笔)")
        self._write(f"盈利笔数: {len(win_trades)}笔")
        self._write(f"亏损笔数: {len(lose_trades)}笔")
        self._write(f"胜率: {win_rate:.1f}%")
        self._write(f"平均盈利: {avg_win:+.2f}%")
        self._write(f"平均亏损: {avg_lose:+.2f}%")
        self._write(f"盈亏比: {profit_loss_ratio:.2f}")
        self._write("=" * 60)

        # 同时打印到终端
        print("\n" + "=" * 60)
        print("========== 最终回测统计 ==========")
        print("=" * 60)
        print(f"回测区间: {start_date} ~ {end_date} ({len(nav_history)}个交易日)")
        print(f"初始资金: {portfolio.initial_capital:,.2f}")
        print(f"最终资产: {final_nav:,.2f}")
        print(f"总收益率: {total_return:+.2f}%")
        print(f"年化收益: {annualized:+.2f}%")
        print(f"最大回撤: {max_drawdown:.2f}%")
        print(f"总交易笔数: {total_sell}笔")
        print(f"胜率: {win_rate:.1f}%")
        print(f"平均盈利: {avg_win:+.2f}% | 平均亏损: {avg_lose:+.2f}%")
        print(f"盈亏比: {profit_loss_ratio:.2f}")
        print("=" * 60)

    def close(self):
        """关闭输出文件"""
        if self.f:
            self.f.close()
            self.f = None
