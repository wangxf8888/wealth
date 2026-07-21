"""交易明细上下文增强 - 为每笔交易附加完整hour级OHLC上下文。

覆盖范围:
  buy_date前5个交易日 + buy_date到sell_date之间所有交易日 + sell_date后5个交易日

输出字段: daily_hours (列表), 每天含 date, label, h1~h4 的 OHLC rate
所有rate字段为相对当日preclose的涨跌幅百分比:
  open_rate = (open - preclose) / preclose * 100
  close_rate = (close - preclose) / preclose * 100
  high_rate = (high - preclose) / preclose * 100
  low_rate = (low - preclose) / preclose * 100
"""
import sqlite3
from typing import List, Dict, Optional


def enrich_trades_with_context(trades: List[dict], db_path: str) -> List[dict]:
    """为交易记录列表批量添加daily_hours字段。

    Args:
        trades: 交易记录字典列表(已由dataclass转dict)
        db_path: 数据库路径

    Returns:
        增强后的交易记录列表(原地修改并返回)
    """
    if not trades:
        return trades

    conn = sqlite3.connect(db_path)

    # 预加载所有交易日列表
    trading_dates = _load_trading_dates(conn)
    date_index = {d: i for i, d in enumerate(trading_dates)}

    for trade in trades:
        try:
            daily_hours = _build_daily_hours(
                conn, trade, trading_dates, date_index)
            trade['daily_hours'] = daily_hours
        except Exception:
            trade['daily_hours'] = None

    conn.close()
    return trades


def _load_trading_dates(conn: sqlite3.Connection) -> List[str]:
    """加载全部交易日列表。"""
    cur = conn.execute(
        "SELECT DISTINCT date FROM stock_kline ORDER BY date")
    return [r[0] for r in cur.fetchall()]


def _build_daily_hours(
    conn: sqlite3.Connection,
    trade: dict,
    trading_dates: List[str],
    date_index: Dict[str, int]
) -> Optional[list]:
    """构建单笔交易的完整daily_hours列表。"""
    buy_date = trade.get('buy_date', '')
    sell_date = trade.get('sell_date', '')
    code = trade.get('code', '')
    if not buy_date or not sell_date or not code:
        return None

    buy_idx = date_index.get(buy_date)
    sell_idx = date_index.get(sell_date)
    if buy_idx is None or sell_idx is None:
        return None

    # 范围: buy_idx-5 到 sell_idx+5
    start_idx = max(0, buy_idx - 5)
    end_idx = min(len(trading_dates) - 1, sell_idx + 5)

    # 收集所有需要的日期
    target_dates = trading_dates[start_idx:end_idx + 1]
    if not target_dates:
        return None

    # 批量查询该股票这些日期的数据
    rows_by_date = _query_stock_days(conn, code, target_dates)

    # 判断涨停日（D-1通常是涨停日，但要验证）
    limitup_dates = set()
    for d, row in rows_by_date.items():
        if _check_limitup_from_row(row, code):
            limitup_dates.add(d)

    # 组装daily_hours列表
    daily_hours = []
    for date in target_dates:
        row = rows_by_date.get(date)
        if row is None:
            continue

        idx = date_index[date]
        offset = idx - buy_idx
        label = _make_label(offset, date, buy_date, sell_date, limitup_dates)

        day_entry = {
            'date': date,
            'label': label,
        }

        # 添加h1~h4
        for h in range(1, 5):
            h_data = _extract_hour(row, h)
            day_entry[f'h{h}'] = h_data

        daily_hours.append(day_entry)

    return daily_hours if daily_hours else None


def _make_label(offset: int, date: str, buy_date: str,
                sell_date: str, limitup_dates: set) -> str:
    """生成日期标签，含特殊注释。"""
    if offset < 0:
        label = f'D{offset}'
    elif offset == 0:
        label = 'D0'
    else:
        label = f'D+{offset}'

    # 添加特殊注释
    annotations = []
    if date in limitup_dates:
        annotations.append('涨停日')
    if date == buy_date:
        annotations.append('买入日')
    if date == sell_date:
        annotations.append('卖出日')

    if annotations:
        label += f'({",".join(annotations)})'

    return label


def _query_stock_days(
    conn: sqlite3.Connection,
    code: str,
    dates: List[str]
) -> Dict[str, dict]:
    """批量查询某股票多个日期的完整行数据。"""
    if not dates:
        return {}
    placeholders = ','.join('?' * len(dates))
    sql = (f"SELECT * FROM stock_kline "
           f"WHERE code = ? AND date IN ({placeholders}) "
           f"ORDER BY date")
    params = [code] + dates
    cur = conn.execute(sql, params)
    columns = [desc[0] for desc in cur.description]

    result = {}
    for row in cur.fetchall():
        row_dict = dict(zip(columns, row))
        result[row_dict['date']] = row_dict
    return result


def _extract_hour(row: dict, hour: int) -> dict:
    """提取某小时的rate数据。"""
    prefix = f'hour{hour}_'
    open_rate = row.get(f'{prefix}open_rate')
    close_rate = row.get(f'{prefix}close_rate')
    high_rate = row.get(f'{prefix}high_rate')
    low_rate = row.get(f'{prefix}low_rate')

    return {
        'open_rate': _round2(open_rate),
        'close_rate': _round2(close_rate),
        'high_rate': _round2(high_rate),
        'low_rate': _round2(low_rate),
    }


def _check_limitup_from_row(row: dict, code: str) -> bool:
    """从行数据判断是否涨停。"""
    close = _safe_float(row.get('close'))
    preclose = _safe_float(row.get('preclose'))
    if preclose <= 0 or close <= 0:
        return False
    return _check_limitup(code, close, preclose)


def _check_limitup(code: str, close: float, preclose: float) -> bool:
    """判断是否涨停：基于round(close/preclose, 2)价格比值。

    主板(sh.6/sz.0): 10%涨停 -> ratio >= 1.10
    创业板(sz.3)/科创板(sh.688): 20%涨停 -> ratio >= 1.20
    北交所(bj.): 30%涨停 -> ratio >= 1.30
    """
    ratio = round(close / preclose, 2)

    code_upper = code.upper()
    if code_upper.startswith('SZ.3') or code_upper.startswith('SH.688'):
        return ratio >= 1.20
    elif code_upper.startswith('BJ.'):
        return ratio >= 1.30
    else:
        return ratio >= 1.10


def _safe_float(v) -> float:
    """安全转float。"""
    if v is None:
        return 0.0
    try:
        f = float(v)
        return 0.0 if f != f else f  # NaN -> 0
    except (TypeError, ValueError):
        return 0.0


def _round2(v) -> float:
    """安全取两位小数。"""
    f = _safe_float(v)
    return round(f, 2)
