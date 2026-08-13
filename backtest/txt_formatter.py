"""回测TXT明细标准格式生成器。

输出格式严格对标 data/realtime/bigyang_lowopen_202604.txt 参考文件:
- 每笔交易头部: --- {code} {name} ({关键参数}) --- [盈亏: +X.XX%]
- 每天一行: 日期 + 标记 + H1-H4 OHLC rate% + 换手率
- rate基准: 每天独立的preclose (非买入价)
- 时间范围: 信号日前10日 + 持仓 + 卖出后10日
"""
import os
import sqlite3
from typing import List, Dict, Optional

DB_PATH = '/home/AIWealth/data/stocks.db'
LOG_DIR = '/home/AIWealth/logs/backtest'

# 策略中文简述映射(Task#32): 组合明细逐笔标注归属策略, 用户无需查代码即可分辨。
# 新策略缺映射时降级只显示英文名。
STRATEGY_LABELS = {
    'limitup_early_seal': '涨停早封',
    'amplitude_reversal': '振幅反转',
    'gem_star_late_seal': '创科晚封',
    'big_yang_low_open_v2': '大阳低开',
    'two_board_pullback_dip_h1c': '双板回调低吸',
}


def _strategy_tag(trade) -> str:
    """`[英文名·中文简述] ` 标注; 无 strategy_name 时返回空串(兼容旧调用方)。"""
    sname = getattr(trade, 'strategy_name', '') or ''
    if not sname:
        return ''
    zh = STRATEGY_LABELS.get(sname)
    return f'[{sname}·{zh}] ' if zh else f'[{sname}] '


def _safe_float(v) -> float:
    if v is None:
        return 0.0
    try:
        f = float(v)
        return 0.0 if f != f else f
    except (TypeError, ValueError):
        return 0.0


def _fmt_rate_val(price: float, preclose: float) -> str:
    """价格转rate字符串, 右对齐7字符"""
    if preclose <= 0 or price <= 0:
        return '       '
    rate = (price - preclose) / preclose * 100
    s = f'{rate:+.2f}%'
    return f'{s:>7}'


class TxtFormatter:
    """标准TXT明细生成器"""

    def __init__(self, db_path: str = DB_PATH):
        self._conn = sqlite3.connect(db_path)
        self._conn.row_factory = sqlite3.Row
        self._trading_dates = self._load_trading_dates()
        self._date_index = {d: i for i, d in enumerate(self._trading_dates)}
        self._name_map = self._load_code_names()

    def _load_trading_dates(self) -> list:
        cur = self._conn.execute(
            "SELECT DISTINCT date FROM stock_kline ORDER BY date")
        return [r[0] for r in cur.fetchall()]

    def _load_code_names(self) -> dict:
        cur = self._conn.execute(
            "SELECT code, code_name FROM stock_kline GROUP BY code")
        return {r[0]: r[1] for r in cur.fetchall() if r[1]}

    def _prev_trading_date(self, date: str) -> Optional[str]:
        idx = self._date_index.get(date)
        if idx is None or idx == 0:
            return None
        return self._trading_dates[idx - 1]

    def _get_date_range(self, center_start: str, center_end: str,
                        before: int = 10, after: int = 10) -> list:
        """获取 center_start 前 before 天 到 center_end 后 after 天的交易日列表"""
        idx_start = self._date_index.get(center_start)
        idx_end = self._date_index.get(center_end)
        if idx_start is None or idx_end is None:
            return []
        real_start = max(0, idx_start - before)
        real_end = min(len(self._trading_dates) - 1, idx_end + after)
        return self._trading_dates[real_start:real_end + 1]

    def _query_stock_days(self, code: str, dates: list) -> Dict[str, dict]:
        """批量查询一只股票多天的hourly数据, 返回 {date: row_dict}"""
        if not dates:
            return {}
        # 用日期范围查询比IN更高效
        date_min = min(dates)
        date_max = max(dates)
        sql = """SELECT date, preclose, turn,
                   hour1_open, hour1_high, hour1_low, hour1_close,
                   hour2_open, hour2_high, hour2_low, hour2_close,
                   hour3_open, hour3_high, hour3_low, hour3_close,
                   hour4_open, hour4_high, hour4_low, hour4_close,
                   open_rate, close_rate
                  FROM stock_kline
                  WHERE code = ? AND date >= ? AND date <= ?
                  ORDER BY date"""
        cur = self._conn.execute(sql, (code, date_min, date_max))
        result = {}
        date_set = set(dates)
        for row in cur.fetchall():
            d = dict(row)
            if d['date'] in date_set:
                result[d['date']] = d
        return result

    def generate(self, strategy_name: str, summary: dict, trades: list,
                 output_dir: str = LOG_DIR) -> str:
        """生成标准格式TXT交易明细。

        Args:
            strategy_name: 策略名称
            summary: 回测概要dict
            trades: List[TradeRecord] 交易记录列表
            output_dir: 输出目录

        Returns:
            输出文件路径
        """
        os.makedirs(output_dir, exist_ok=True)
        output_path = os.path.join(output_dir, f'{strategy_name}_detail.txt')

        n = len(trades)
        wins = sum(1 for t in trades if t.profit_pct > 0)
        win_rate = (wins / n * 100) if n else 0.0
        avg_profit = (sum(t.profit_pct for t in trades) / n) if n else 0.0

        lines = []
        # === 文件头部 ===
        lines.append("=" * 90)
        lines.append(f"策略: {strategy_name}")
        lines.append(f"回测区间: {summary.get('start_date', '?')} ~ "
                     f"{summary.get('end_date', '?')}")
        lines.append(f"CAGR: {summary.get('cagr_pct', 0):+.2f}%  |  "
                     f"总收益: {summary.get('total_return_pct', 0):+.2f}%  |  "
                     f"最大回撤: {summary.get('max_drawdown_pct', 0):.2f}%")
        lines.append(f"交易笔数: {n}  |  胜率: {win_rate:.1f}%  |  "
                     f"平均收益: {avg_profit:+.2f}%")
        lines.append("=" * 90)
        lines.append("")

        # === 逐笔交易明细 ===
        for i, t in enumerate(trades, 1):
            trade_lines = self._format_one_trade(i, t)
            lines.extend(trade_lines)

        with open(output_path, 'w', encoding='utf-8') as f:
            f.write('\n'.join(lines) + '\n')

        print(f"TXT标准明细已生成: {output_path} ({n}笔)")
        return output_path

    def _format_one_trade(self, idx: int, trade) -> list:
        """格式化单笔交易的完整小时级明细"""
        code = trade.code
        buy_date = trade.buy_date
        sell_date = trade.sell_date
        name = self._name_map.get(code, '')

        # 信号日 = 买入日前一交易日
        signal_date = self._prev_trading_date(buy_date)
        if not signal_date:
            signal_date = buy_date

        # 确定日期范围: signal_date前10日 ~ sell_date后10日
        date_range = self._get_date_range(signal_date, sell_date,
                                          before=10, after=10)
        if not date_range:
            return [f"--- #{idx:03d} {code} {name} "
                    f"{_strategy_tag(trade)}--- [数据缺失]", ""]

        # 批量查询该股在日期范围内的hourly数据
        day_data = self._query_stock_days(code, date_range)

        # 构建头部信息: 昨涨=信号日涨幅, 今开=买入日开盘涨幅
        signal_row = day_data.get(signal_date, {})
        buy_row = day_data.get(buy_date, {})
        signal_close_rate = _safe_float(signal_row.get('close_rate'))
        buy_open_rate = _safe_float(buy_row.get('open_rate'))
        header_params = f"昨涨:{signal_close_rate:+.2f}% 今开:{buy_open_rate:+.2f}%"
        pnl_str = f"{trade.profit_pct:+.2f}%"

        lines = []
        lines.append(f"--- {code} {name} {_strategy_tag(trade)}"
                     f"({header_params}) --- [盈亏: {pnl_str}]")

        # 表头
        lines.append(
            "  日期               |"
            "    H1_O    H1_H    H1_L    H1_C |"
            "    H2_O    H2_H    H2_L    H2_C |"
            "    H3_O    H3_H    H3_L    H3_C |"
            "    H4_O    H4_H    H4_L    H4_C |"
            "  Turn")

        # 标记集合
        markers = {}
        if signal_date and signal_date != buy_date:
            markers[signal_date] = '★信'
        markers[buy_date] = '★买'
        markers[sell_date] = '★卖'

        # 逐日格式化
        for d in date_range:
            row = day_data.get(d)
            if not row:
                continue

            preclose = _safe_float(row.get('preclose'))
            turn = _safe_float(row.get('turn'))

            # 日期+标记
            marker = markers.get(d, '')
            if marker:
                date_str = f"  {d} {marker}"
            else:
                date_str = f"  {d}     "

            # H1-H4 OHLC rates
            hourly_parts = []
            for h in range(1, 5):
                h_open = _safe_float(row.get(f'hour{h}_open'))
                h_high = _safe_float(row.get(f'hour{h}_high'))
                h_low = _safe_float(row.get(f'hour{h}_low'))
                h_close = _safe_float(row.get(f'hour{h}_close'))

                o_str = _fmt_rate_val(h_open, preclose)
                h_str = _fmt_rate_val(h_high, preclose)
                l_str = _fmt_rate_val(h_low, preclose)
                c_str = _fmt_rate_val(h_close, preclose)
                hourly_parts.append(f" {o_str} {h_str} {l_str} {c_str}")

            # 换手率
            turn_str = f"{turn:5.1f}%"

            line = (f"{date_str} |{hourly_parts[0]} |"
                    f"{hourly_parts[1]} |{hourly_parts[2]} |"
                    f"{hourly_parts[3]} | {turn_str}")
            lines.append(line)

        lines.append("")  # 交易间空行
        return lines

    def close(self):
        if self._conn:
            self._conn.close()
            self._conn = None
