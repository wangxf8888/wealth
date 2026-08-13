                   hour1_open, hour1_high, hour1_low, hour1_close,\n                   hour2_open, hour2_high, hour2_low, hour2_close,\n                   hour3_open, hour3_high, hour3_low, hour3_close,\n                   hour4_open, hour4_high, hour4_low, hour4_close,\n                   open_rate, close_rate\n                  FROM stock_kline\n                  WHERE code = ? AND date \u003e= ? AND date \u003c= ?\n                  ORDER BY date\"\"\"\n        cur = self._conn.execute(sql, (code, date_min, date_max))\n        result = {}\n        date_set = set(dates)\n        for row in cur.fetchall():\n            d = dict(row)\n            if d['date'] in date_set:\n                result[d['date']] = d\n        return result\n\n    def generate(self, strategy_name: str, summary: dict, trades: list,\n                 output_dir: str = LOG_DIR) -\u003e str:\n        \"\"\"生成标准格式TXT交易明细。\n\n        Args:\n            strategy_name: 策略名称\n            summary: 回测概要dict\n            trades: List[TradeRecord] 交易记录列表\n            output_dir: 输出目录\n\n        Returns:\n            输出文件路径\n        \"\"\"\n        os.makedirs(output_dir, exist_ok=True)\n        output_path = os.path.join(output_dir, f'{strategy_name}_detail.txt')\n\n        n = len(trades)\n        wins = sum(1 for t in trades if t.profit_pct \u003e 0)\n        win_rate = (wins / n * 100) if n else 0.0\n        avg_profit = (sum(t.profit_pct for t in trades) / n) if n else 0.0\n\n        lines = []\n        # === 文件头部 ===\n        lines.append(\"=\" * 90)\n        lines.append(f\"策略: {strategy_name}\")\n        lines.append(f\"回测区间: {summary.get('start_date', '?')} ~ \"\n                     f\"{summary.get('end_date', '?')}\")\n        lines.append(f\"CAGR: {summary.get('cagr_pct', 0):+.2f}%  |  \"\n                     f\"总收益: {summary.get('total_return_pct', 0):+.2f}%  |  \"\n                     f\"最大回撤: {summary.get('max_drawdown_pct', 0):.2f}%\")\n        lines.append(f\"交易笔数: {n}  |  胜率: {win_rate:.1f}%  |  \"\n                     f\"平均收益: {avg_profit:+.2f}%\")\n        lines.append(\"=\" * 90)\n        lines.append(\"\")\n\n        # === 逐笔交易明细 ===\n        for i, t in enumerate(trades, 1):\n            trade_lines = self._format_one_trade(i, t)\n            lines.extend(trade_lines)\n\n        with open(output_path, 'w', encoding='utf-8') as f:\n            f.write('\\n'.join(lines) + '\\n')\n\n        print(f\"TXT标准明细已生成: {output_path} ({n}笔)\")\n        return output_path\n\n    def _format_one_trade(self, idx: int, trade) -\u003e list:\n        \"\"\"格式化单笔交易的完整小时级明细\"\"\"\n        code = trade.code\n        buy_date = trade.buy_date\n        sell_date = trade.sell_date\n        name = self._name_map.get(code, '')\n\n        # 信号日 = 买入日前一交易日\n        signal_date = self._prev_trading_date(buy_date)\n        if not signal_date:\n            signal_date = buy_date\n\n        # 确定日期范围: signal_date前10日 ~ sell_date后10日\n        date_range = self._get_date_range(signal_date, sell_date,\n                                          before=10, after=10)\n        if not date_range:\n            return [f\"--- #{idx:03d} {code} {name} \"\n                    f\"{_strategy_tag(trade)}--- [数据缺失]\", \"\"]\n\n        # 批量查询该股在日期范围内的hourly数据\n        day_data = self._query_stock_days(code, date_range)\n\n        # 构建头部信息: 昨涨=信号日涨幅, 今开=买入日开盘涨幅\n        signal_row = day_data.get(signal_date, {})\n        buy_row = day_data.get(buy_date, {})\n        signal_close_rate = _safe_float(signal_row.get('close_rate'))\n        buy_open_rate = _safe_float(buy_row.get('open_rate'))\n        header_params = f\"昨涨:{signal_close_rate:+.2f}% 今开:{buy_open_rate:+.2f}%\"\n        pnl_str = f\"{trade.profit_pct:+.2f}%\"\n\n        lines = []\n        lines.append(f\"--- {code} {name} {_strategy_tag(trade)}\"\n                     f\"({header_params}) --- [盈亏: {pnl_str}]\")\n\n        # 表头\n        lines.append(\n            \"  日期               |\"\n            \"    H1_O    H1_H    H1_L    H1_C |\"\n            \"    H2_O    H2_H    H2_L    H2_C |\"\n            \"    H3_O    H3_H    H3_L    H3_C |\"\n            \"    H4_O    H4_H    H4_L    H4_C |\"\n            \"  Turn\")\n\n        # 标记集合\n        markers = {}\n        if signal_date and signal_date != buy_date:\n            markers[signal_date] = '★信'\n        markers[buy_date] = '★买'\n        markers[sell_date] = '★卖'\n\n        # 逐日格式化\n        for d in date_range:\n            row = day_data.get(d)\n            if not row:\n                continue\n\n            preclose = _safe_float(row.get('preclose'))\n            turn = _safe_float(row.get('turn'))\n\n            # 日期+标记\n            marker = markers.get(d, '')\n            if marker:\n                date_str = f\"  {d} {marker}\"\n            else:\n                date_str = f\"  {d}     \"\n\n            # H1-H4 OHLC rates\n            hourly_parts = []\n            for h in range(1, 5):\n                h_open = _safe_float(row.get(f'hour{h}_open'))\n                h_high = _safe_float(row.get(f'hour{h}_high'))\n                h_low = _safe_float(row.get(f'hour{h}_low'))\n                h_close = _safe_float(row.get(f'hour{h}_close'))\n\n                o_str = _fmt_rate_val(h_open, preclose)\n                h_str = _fmt_rate_val(h_high, preclose)\n                l_str = _fmt_rate_val(h_low, preclose)\n                c_str = _fmt_rate_val(h_close, preclose)\n                hourly_parts.append(f\" {o_str} {h_str} {l_str} {c_str}\")\n\n            # 换手率\n            turn_str = f\"{turn:5.1f}%\"\n\n            line = (f\"{date_str} |{hourly_parts[0]} |\"\n                    f\"{hourly_parts[1]} |{hourly_parts[2]} |\"\n                    f\"{hourly_parts[3]} | {turn_str}\")\n            lines.append(line)\n\n        lines.append(\"\")  # 交易间空行\n        return lines\n\n    def close(self):\n        if self._conn:\n            self._conn.close()\n            self._conn = None\n","lineDetails":[{"lines":["16-34","179-180","194-195"],"type":"added","brand":"qoder","product":"IDE","scenario":"agent","sessionId":"3970bf21-7ca1-42f7-bc00-5566524088e4","userQueryBusinessId":"7443cdbf-7a38-4af2-b23f-53ce5c6bf833"},{"lines":["160","174-175"],"type":"deleted","brand":"qoder","product":"IDE","scenario":"agent","sessionId":"3970bf21-7ca1-42f7-bc00-5566524088e4","userQueryBusinessId":"7443cdbf-7a38-4af2-b23f-53ce5c6bf833"}],"gitRoot":"/home/AIWealth"}
{"filePath":"/home/AIWealth/scripts/task32_rerender_detail.py","aiAddedLines":["1-39"],"aiDeletedLines":[],"aiModifiedContent":"\"\"\"Task#32: 从已有 trades JSON 离线重render TXT明细(不重跑回测/不碰BaoStock/不写前端)。\n\n用法: python3 scripts/task32_rerender_detail.py \u003ctrades.json\u003e [输出策略名]\n  \u003ctrades.json\u003e: run_unified 产物(含 summary + trades 字段), 如\n                 logs/backtest/unified_5slot_trades.json\n  [输出策略名]:  默认 unified_5slot → 输出 logs/backtest/unified_5slot_detail.txt\n\"\"\"\nimport json\nimport sys\nfrom types import SimpleNamespace\n\nsys.path.insert(0, '/home/AIWealth')\n\nfrom backtest.txt_formatter import TxtFormatter, DB_PATH, LOG_DIR  # noqa: E402\n\n\ndef main():\n    if len(sys.argv) \u003c 2:\n        print(__doc__)\n        sys.exit(1)\n    src = sys.argv[1]\n    out_name = sys.argv[2] if len(sys.argv) \u003e 2 else 'unified_5slot'\n\n    with open(src, encoding='utf-8') as f:\n        payload = json.load(f)\n    summary = payload['summary']\n    trades = [SimpleNamespace(**t) for t in payload['trades']]\n    print(f\"载入 {src}: {len(trades)}笔 | 策略={payload.get('strategies')}\")\n\n    fmt = TxtFormatter(DB_PATH)\n    try:\n        path = fmt.generate(out_name, summary, trades, LOG_DIR)\n    finally:\n        fmt.close()\n    print(f\"重render完成: {path}\")\n\n\nif __name__ == '__main__':\n    main()\n","lineDetails":[{"lines":["1-39"],"type":"added","brand":"qoder","product":"IDE","scenario":"agent","sessionId":"3970bf21-7ca1-42f7-bc00-5566524088e4","userQueryBusinessId":"7443cdbf-7a38-4af2-b23f-53ce5c6bf833"}],"gitRoot":"/home/AIWealth"}
{"filePath":"/home/AIWealth/tools/fetch_daily_kline_fallback.py","aiAddedLines":["1-288"],"aiDeletedLines":[],"aiModifiedContent":"#!/usr/bin/env python3\n\"\"\"fetch_daily_kline_fallback.py - 日K降级备用源(腾讯fqkline) [Task #31]\n\n背景(2026-07-24事故): BaoStock账号被封禁(login 10001011 黑名单, 诱因为\n3.2req/s持续大批量拉取), 主脚本fetch_daily_kline.py当晚起不可用。\n本脚本用腾讯 web.ifzq.gtimg.cn/appstock/app/fqkline/get 拉当日日K兜底:\n- 字段校对证据(2026-07-24实测2只股 vs BaoStock已入库行, 逐字段精确匹配):\n  day数组 = [date, open, close, high, low, volume(手)]  ← 注意顺序O,C,H,L\n  volume: 手×100=股;  preclose = 前一根bar的close\n  amount = qt快照[35]第三段(元);  turn = qt快照[38](%);  名称 = qt[1]\n- 东财push2his本机网络层不可达(HTTPS/HTTP均被连接重置, 2026-07-24实测),\n  push2delay可通但data:null, 故弃用东财改用腾讯(项目既有成熟源)\n- hour1-4列全部留NULL(引擎读到NULL自动跳过该股当日, 可接受;\n  BaoStock恢复后由主脚本回补: DELETE当日行再重跑fetch_daily_kline.py)\n- 仅支持\"当日盘后\"模式: qt快照的amount/turn只在快照日=目标日时有效\n\n用法:\n  写库:   python3 tools/fetch_daily_kline_fallback.py 2026-07-24\n  校验:   python3 tools/fetch_daily_kline_fallback.py 2026-07-24 --verify [--limit N]\n          (verify模式不写库, 与stock_kline已有行逐字段对照, 用于演练/审计)\n\"\"\"\nimport argparse\nimport json\nimport logging\nimport math\nimport sqlite3\nimport sys\nimport time\nimport urllib.request\nfrom datetime import datetime\n\nDB_PATH = '/home/AIWealth/data/stocks.db'\nLOG_FILE = '/home/AIWealth/logs/fetch_daily_kline.log'\nALERT_FILE = '/home/AIWealth/logs/realtime/scheduler_alerts.log'\n\n# [Task #31 限速纪律] 2026-07-24 BaoStock因3.2req/s被封禁的教训:\n# 对任何外部行情源保持礼貌限速, 腾讯源≥0.15s/请求; 连续错误立即熔断禁止重试轰炸\nREQUEST_INTERVAL = 0.15     # 秒/请求\nMAX_CONSECUTIVE_ERRORS = 10  # 连续错误熔断阈值\nRETRIES = 2                  # 单股重试次数(温和)\n\nlogging.basicConfig(\n    level=logging.INFO,\n    format='%(asctime)s [%(levelname)s] [降级模式] %(message)s',\n    handlers=[logging.FileHandler(LOG_FILE, encoding='utf-8'),\n              logging.StreamHandler()])\nlogger = logging.getLogger(__name__)\n\n\ndef _write_alert(msg):\n    try:\n        with open(ALERT_FILE, 'a') as f:\n            f.write(f\"{datetime.now().strftime('%Y-%m-%d %H:%M:%S')} \"\n                    f\"[DATA_INTEGRITY] {msg}\\n\")\n    except Exception:\n        pass\n\n\ndef safe_float(v, default=None):\n    try:\n        x = float(v)\n        return default if (math.isnan(x) or math.isinf(x)) else x\n    except (ValueError, TypeError):\n        return default\n\n\ndef calc_rate(price, preclose):\n    if price is None or preclose is None or preclose == 0:\n        return None\n    return round((price - preclose) / preclose * 100, 2)\n\n\ndef fetch_tencent_daily(code, target_date):\n    \"\"\"拉单股日K, 返回record dict或None(当日无数据/停牌)。失败抛异常。\n    腾讯fqkline单请求同时返回day数组与qt实时快照(额/换手/名称)。\"\"\"\n    qt_code = code.replace('sh.', 'sh').replace('sz.', 'sz')\n    url = (f'https://web.ifzq.gtimg.cn/appstock/app/fqkline/get'\n           f'?param={qt_code},day,,,5,')\n    req = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0'})\n    with urllib.request.urlopen(req, timeout=10) as resp:\n        d = json.loads(resp.read().decode('utf-8', errors='replace'))\n    node = d.get('data', {}).get(qt_code, {})\n    days = node.get('day') or []\n    if not days or days[-1][0] != target_date:\n        return None                       # 当日停牌/未上市/无数据\n    # day数组字段序: [date, open, close, high, low, volume(手)] (实测校对)\n    bar = days[-1]\n    open_p, close_p = safe_float(bar[1]), safe_float(bar[2])\n    high_p, low_p = safe_float(bar[3]), safe_float(bar[4])\n    vol_hand = safe_float(bar[5])\n    # volume单位(2026-07-24全量5197只verify实测): 主板/创业板为\"手\"(×100=股,\n    # 与BaoStock精确匹配); 科创板sh.68x(688及689 CDR)为\"股\"(直接用, 否则100倍偏差)\n    if vol_hand is None:\n        volume = None\n    elif code.startswith('sh.68'):\n        volume = int(vol_hand)\n    else:\n        volume = int(vol_hand * 100)\n\n    qt = (node.get('qt') or {}).get(qt_code) or []\n    name, amount, turn, preclose = '', None, None, None\n    if len(qt) \u003e 38:\n        name = qt[1]\n        # qt[30]快照时间戳YYYYMMDDHHMMSS: 仅快照日=目标日时qt字段可信\n        snap_ts = str(qt[30]) if len(qt) \u003e 30 else ''\n        if snap_ts[:8] == target_date.replace('-', ''):\n            # preclose必须用qt[4](交易所口径昨收=除权除息调整后), 不能用day\n            # 前一根close(原始价): 实测sh.600004 2026-07-24除息日 7.68 vs 7.96\n            preclose = safe_float(qt[4])\n            seg = str(qt[35]).split('/')  # \"现价/量(手)/额(元)\"\n            if len(seg) == 3:\n                amount = safe_float(seg[2])\n            turn = safe_float(qt[38])\n    if preclose is None and len(days) \u003e= 2:\n        preclose = safe_float(days[-2][2])  # 兜底: 前根close(非除权日等价)\n    if preclose is None or preclose == 0:\n        return None\n\n    record = {'date': target_date, 'code': code, 'code_name': name,\n              'preclose': preclose,\n              'open': open_p, 'open_rate': calc_rate(open_p, preclose),\n              'high': high_p, 'high_rate': calc_rate(high_p, preclose),\n              'low': low_p, 'low_rate': calc_rate(low_p, preclose),\n              'close': close_p, 'close_rate': calc_rate(close_p, preclose),\n              'volume': volume, 'amount': amount, 'turn': turn,\n              'isST': 1 if 'ST' in name.upper() else 0}\n    # hour1-4全列NULL: 引擎读到NULL跳过该股当日, BaoStock恢复后重补\n    for h in ('hour1', 'hour2', 'hour3', 'hour4'):\n        for suf in ('open', 'open_rate', 'high', 'high_rate', 'low',\n                    'low_rate', 'close', 'close_rate', 'volume', 'amount'):\n            record[f'{h}_{suf}'] = None\n    return record\n\n\nINSERT_SQL = \"\"\"INSERT OR IGNORE INTO stock_kline (\n    date, code, code_name, preclose,\n    open, open_rate, high, high_rate, low, low_rate, close, close_rate,\n    volume, amount, turn,\n    hour1_open, hour1_open_rate, hour1_high, hour1_high_rate,\n    hour1_low, hour1_low_rate, hour1_close, hour1_close_rate,\n    hour2_open, hour2_open_rate, hour2_high, hour2_high_rate,\n    hour2_low, hour2_low_rate, hour2_close, hour2_close_rate,\n    hour3_open, hour3_open_rate, hour3_high, hour3_high_rate,\n    hour3_low, hour3_low_rate, hour3_close, hour3_close_rate,\n    hour4_open, hour4_open_rate, hour4_high, hour4_high_rate,\n    hour4_low, hour4_low_rate, hour4_close, hour4_close_rate,\n    hour1_volume, hour1_amount, hour2_volume, hour2_amount,\n    hour3_volume, hour3_amount, hour4_volume, hour4_amount,\n    isST\n) VALUES (\n    :date, :code, :code_name, :preclose,\n    :open, :open_rate, :high, :high_rate, :low, :low_rate, :close, :close_rate,\n    :volume, :amount, :turn,\n    :hour1_open, :hour1_open_rate, :hour1_high, :hour1_high_rate,\n    :hour1_low, :hour1_low_rate, :hour1_close, :hour1_close_rate,\n    :hour2_open, :hour2_open_rate, :hour2_high, :hour2_high_rate,\n    :hour2_low, :hour2_low_rate, :hour2_close, :hour2_close_rate,\n    :hour3_open, :hour3_open_rate, :hour3_high, :hour3_high_rate,\n    :hour3_low, :hour3_low_rate, :hour3_close, :hour3_close_rate,\n    :hour4_open, :hour4_open_rate, :hour4_high, :hour4_high_rate,\n    :hour4_low, :hour4_low_rate, :hour4_close, :hour4_close_rate,\n    :hour1_volume, :hour1_amount, :hour2_volume, :hour2_amount,\n    :hour3_volume, :hour3_amount, :hour4_volume, :hour4_amount,\n    :isST\n)\"\"\"\n\n\ndef get_stock_list(conn):\n    \"\"\"BaoStock不可用 → 股票列表取自stock_kline最近一个交易日的全部code。\"\"\"\n    last_date = conn.execute(\n        \"SELECT MAX(date) FROM stock_kline\").fetchone()[0]\n    rows = conn.execute(\n        \"SELECT code, code_name FROM stock_kline WHERE date=?\",\n        (last_date,)).fetchall()\n    logger.info(\"股票列表来自stock_kline %s: %d只\", last_date, len(rows))\n    return rows\n\n\ndef main():\n    ap = argparse.ArgumentParser()\n    ap.add_argument('date', help='目标日期 YYYY-MM-DD (仅支持当日盘后)')\n    ap.add_argument('--verify', action='store_true',\n                    help='校验模式: 不写库, 与stock_kline已有行对照')\n    ap.add_argument('--limit', type=int, default=0,\n                    help='verify模式抽样股数(0=全部)')\n    args = ap.parse_args()\n    target = args.date\n\n    logger.warning(\"=== [降级模式] BaoStock不可用, 启用腾讯fqkline备用源 \"\n                   \"(hour1-4列留NULL, 恢复后需重补) ===\")\n    conn = sqlite3.connect(DB_PATH)\n    conn.execute(\"PRAGMA journal_mode=WAL\")\n    stocks = get_stock_list(conn)\n    if args.limit:\n        stocks = stocks[:args.limit]\n\n    inserted, skipped, verified_ok, verified_diff = 0, 0, 0, 0\n    failures, consecutive_err = [], 0\n    diffs = []\n    t0 = time.time()\n    for i, (code, _) in enumerate(stocks):\n        rec, err = None, None\n        for attempt in range(RETRIES):\n            try:\n                rec = fetch_tencent_daily(code, target)\n                err = None\n                break\n            except Exception as exc:\n                err = str(exc)\n                time.sleep(1 + attempt)\n        time.sleep(REQUEST_INTERVAL)   # [Task #31] 礼貌限速≥0.15s\n\n        if err:\n            failures.append((code, err))\n            consecutive_err += 1\n            if consecutive_err \u003e= MAX_CONSECUTIVE_ERRORS:\n                logger.error(\"连续%d次错误, 熔断终止(禁止重试轰炸)!\",\n                             consecutive_err)\n                _write_alert(f\"[DAILY_FALLBACK] 腾讯源连续{consecutive_err}次\"\n                             f\"错误熔断, 已完成{i}/{len(stocks)}\")\n                break\n            continue\n        consecutive_err = 0\n        if rec is None:\n            skipped += 1               # 当日停牌/无数据\n            continue\n\n        if args.verify:\n            row = conn.execute(\n                \"SELECT preclose,open,high,low,close,volume FROM stock_kline \"\n                \"WHERE code=? AND date=?\", (code, target)).fetchone()\n            if row is None:\n                verified_diff += 1\n                diffs.append((code, '库中无行'))\n            else:\n                bad = [f\"{f}:{a}vs{b}\" for f, a, b in zip(\n                    ('preclose', 'open', 'high', 'low', 'close'),\n                    (rec['preclose'], rec['open'], rec['high'],\n                     rec['low'], rec['close']), row[:5])\n                    if a is not None and b is not None\n                    and abs(a - b) / b \u003e 0.001]\n                # volume容忍0.1%(腾讯手数舍入)\n                if rec['volume'] and row[5] and \\\n                        abs(rec['volume'] - row[5]) / row[5] \u003e 0.001:\n                    bad.append(f\"volume:{rec['volume']}vs{row[5]}\")\n                if bad:\n                    verified_diff += 1\n                    diffs.append((code, ';'.join(bad)))\n                else:\n                    verified_ok += 1\n        else:\n            conn.execute(INSERT_SQL, rec)\n            inserted += 1\n            if inserted % 200 == 0:\n                conn.commit()\n\n        if (i + 1) % 500 == 0:\n            logger.info(\"进度 %d/%d, 入库%d 跳过%d 失败%d, %.1f req/s\",\n                        i + 1, len(stocks), inse"""首板次日低开低吸策略 (FB-A) - Task#9 引擎精测。

形态(来自 Task#7 全维度网格复审, 研究口径见 data/realtime/task7_refine.txt):
  昨日(D0)首板涨停(连板数==1, 非一字) → 今日(D1)竞价低开 -4% <= open_rate < -2%
  → 昨日红盘占比 red_ratio >= 60(强势日错杀) → H1_open 买入
  → 退出 tp+6% / sl-10%, 最长5个监控日(D6收盘兜底)
排序: D0换手升序(低换手=筹码未松动优先) — task7_top1_probe:
  top1 +1.50%/日 2021-2026 六年全正; 注意换手降序方向则失效(-0.31%)。

研究口径(池子级 tp6sl-10, 毛收益): n=1574 胜率58.4% 均值+0.73% 5正1平
引擎精测(2021-01~2026-07-15, slot=1, 含成本): CAGR +72.53% | MDD 44.81%
  | 219笔 胜率65.75% +1.62%/笔 | 唯一负年 2023 -7.9% → 观察池第一顺位
Task#12 干净数据+全区间重跑(至2026-07-23): CAGR +64.85%, 2026 +16.9→-7.8
  (数据修复+区间延长), 2021-2025 分年不变; 卖点前移两变体(d2c/h1)引擎
  精测均劣于本对照(+56.8%/+61.3%), FB-A **保持现行 tpsl_d6 口径不动**——
  粗测代理曾示 d2c 更优(+86%), 引擎证伪: 变体笔数增加(226→332/447)但
  均收益稀释(+1.47→+0.92/+0.74%/笔), slot=1 路径依赖下 2021/2024/2026
  转弱, MDD 还升(44.8→45.0/55.7)。只信引擎数据。
red_ratio 权威源=index_kline(sh.000001) red_ratio 列, 2026-07-23 前已补齐
(Task#10 Robin 修复); 缺失日显式跳过不买入作为防御, 不引入未来数据。

Task#12 卖点前移(exit_mode 参数化, 买入逻辑不动):
  tpsl_d6   现行对照: tp6/sl-10 挂单监控 D2..D6, D6收盘兜底
  tpsl_h1   挂单仅监控 D2 H1, 未触发 H1收盘强平 (D2起slot当小时可复用)
  d2h1_open / d2h1_close / trail_h1  同 B2-A 语义
依据: Lee时段结构(H1唯一系统性正时段) x Task#9归因(hold长占slot致信号捕获减半)。
"""
import sqlite3

import numpy as np

from strategies.base import Strategy, Signal, SellSignal

_DB = '/home/AIWealth/data/stocks.db'


def _load_red_map() -> dict:
    """{date: red_ratio(float)}; 缺失日不在map中, 调用方显式跳过。"""
    out = {}
    try:
        conn = sqlite3.connect(_DB)
        for d, r in conn.execute(
                "SELECT date, red_ratio FROM index_kline "
                "WHERE code='sh.000001'"):
            if r is not None:
                out[d] = float(r)
        conn.close()
    except sqlite3.Error:
        pass
    return out


class FirstboardLowOpenDipStrategy(Strategy):
    """FB-A: 昨日首板 → 低开-4~-2 → 红盘>=60 → D0低换手优先 → H1低吸。"""

    name = "firstboard_low_open_dip"
    max_hold_hours = 20          # 监控D2..D6, D6收盘兜底(同研究口径5日窗口)
    sell_day_no_buy = True
    buy_hour = 1

    # === 核心参数(Task#7 网格最优) ===
    open_rate_min = -4.0         # 竞价低开下限
    open_rate_max = -2.0         # 竞价低开上限(不含)
    red_ratio_min = 60.0         # 昨日红盘占比下限(D0收盘后已知)
    min_history_bars = 20        # 上市未满20根K线不买(对齐研究口径 nth>=20)

    # 卖出参数显式声明(fixed模式)
    take_profit_pct = 0.06       # 止盈 +6%
    stop_loss_pct = -0.10        # 止损 -10%

    # === Task#50 挖潜维度: D0封板时间过滤(默认None=不过滤, 零侵入) ===
    # 'early'=H1/H2首次触板(早板筹码强), 'late'=H3/H4触板; 无hour数据按不过关处理
    seal_filter = None

    # === Task#12 卖点前移 ===
    # tpsl_d6(现行对照) | tpsl_d2c(到期提前到D2收盘) | tpsl_h1 | d2h1_open
    # | d2h1_close | trail_h1
    exit_mode = 'tpsl_d6'
    trail_pp = 2.0               # trail_h1 回撤触发(pp), 基于D2 H1_open
    _H1_MODES = ('tpsl_h1', 'd2h1_open', 'd2h1_close', 'trail_h1')

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

        d0 = data_feed._prev_trading_date(date)   # 昨日(首板日)
        if not d0:
            return []
        dm1 = data_feed._prev_trading_date(d0)    # 前日(须未涨停)
        if not dm1:
            return []

        # 情绪过滤: 昨日红盘占比 >= 60; 缺失日显式跳过(不引入未来数据)
        red = self._red_ratio(d0)
        if red is None or red < self.red_ratio_min:
            return []

        f_today = data_feed._load_day(date)
        f_d0 = data_feed._load_day(d0)
        f_dm1 = data_feed._load_day(dm1)
        if any(f is None or f.empty for f in (f_today, f_d0, f_dm1)):
            return []

        codes = f_today.index
        is20 = codes.str.startswith('sz.30') | codes.str.startswith('sh.688')
        ratio = np.where(is20, 0.20, 0.10)

        def limit_flags(frame):
            sub = frame.reindex(codes)
            pre = sub['preclose'].to_numpy(dtype=float, na_value=0.0)
            clo = sub['close'].to_numpy(dtype=float, na_value=0.0)
            opn = sub['open'].to_numpy(dtype=float, na_value=0.0)
            lp = np.round(pre * (1 + ratio), 2)
            valid = (pre > 0) & (clo > 0)
            is_lim = valid & (clo >= lp - 0.001)
            is_yizi = is_lim & (opn > 0) & (opn >= lp - 0.001)
            return valid, is_lim, is_yizi

        v0, lim0, yizi0 = limit_flags(f_d0)
        v1, lim1, _ = limit_flags(f_dm1)

        # 昨日首板(非一字) + 前日未涨停(=恰好第1板)
        pattern = v0 & v1 & lim0 & ~yizi0 & ~lim1

        # 今日竞价窗口 + 基础池过滤
        orate = f_today['open_rate'].to_numpy(dtype=float, na_value=np.nan)
        opn = f_today['open'].to_numpy(dtype=float, na_value=0.0)
        win = (orate >= self.open_rate_min) & (orate < self.open_rate_max)
        # Task#68: signal模式(晚间候选生成)次日开盘未知, permissive开盘价(-20%)
        # 落在带状窗口[-4,-2)之外会误杀全部候选 → 跳过窗口过滤只筛形态;
        # 9:25 live模式(真实开盘价注入)与回测(无_mode属性)窗口照常生效
        # (同 Task#36 B2-A 已验证模板)
        if getattr(data_feed, '_mode', None) == 'signal':
            win = np.ones(len(codes), dtype=bool)
        not_bj = ~codes.str.startswith('bj.')
        st = f_today['isST'].fillna(0).astype(int).to_numpy() > 0
        if 'code_name' in f_today.columns:
            st = st | f_today['code_name'].fillna('').astype(str) \
                .str.upper().str.contains('ST').to_numpy()

        mask = pattern & win & (opn > 0) & not_bj & ~st
        if not mask.any():
            return []

        # Task#50: D0封板时间过滤(首个 hourN_high >= D0涨停价 的N)
        if self.seal_filter in ('early', 'late'):
            sub0 = f_d0.reindex(codes)
            pre0 = sub0['preclose'].to_numpy(dtype=float, na_value=0.0)
            lp0 = np.round(pre0 * (1 + ratio), 2)
            seal_h = np.zeros(len(codes), dtype=np.int8)
            for hh in (4, 3, 2, 1):          # 前面小时后写覆盖 → 首次触板
                col = f'hour{hh}_high'
                if col not in sub0.columns:
                    continue
                hi0 = sub0[col].to_numpy(dtype=float, na_value=0.0)
                seal_h = np.where((hi0 > 0) & (hi0 >= lp0 - 0.001),
                                  hh, seal_h)
            if self.seal_filter == 'early':
                mask &= (seal_h >= 1) & (seal_h <= 2)
            else:
                mask &= seal_h >= 3
            if not mask.any():
                return []

        # 排序: D0换手升序(低换手优先, 买入时刻可得)
        d0_turn = f_d0.reindex(codes)['turn'] \
            .to_numpy(dtype=float, na_value=np.inf)
        idx = np.where(mask)[0]
        idx = idx[np.argsort(d0_turn[idx], kind='stable')]

        final = []
        for i in idx:
            code = codes[i]
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
        limit_up, _ = data_feed.get_limit_prices(code, date)
        if limit_up > 0 and price >= limit_up - 0.001:
            return None
        return Signal(code=code, price=float(price), strategy_name=self.name,
                      target_hold_hours=self.max_hold_hours)

    def should_sell(self, position, date, hour, data_feed):
        # T+1: 买入当日不卖(框架另有兜底)
        if position.buy_date >= date:
            return None
        if self.exit_mode in self._H1_MODES:
            return self._sell_forward(position, date, hour, data_feed)
        buy_price = position.buy_price
        if buy_price <= 0:
            return None

        tp_target = buy_price * (1 + self.take_profit_pct)
        sl_target = buy_price * (1 + self.stop_loss_pct)

        bar_open = data_feed.get_hour_open(position.code, date, hour)
        bar_high = data_feed.get_hour_high(position.code, date, hour)
        bar_low = data_feed.get_hour_low(position.code, date, hour)
        if not bar_open or bar_open <= 0:
            return None

        # 跳空穿越按开盘成交(先判SL, 保守)
        open_pnl = (bar_open - buy_price) / buy_price
        if open_pnl <= self.stop_loss_pct:
            return SellSignal(reason='stop_loss', price=float(bar_open))
        if open_pnl >= self.take_profit_pct:
            return SellSignal(reason='take_profit', price=float(bar_open))

        # 盘中触发: 同小时TP/SL双触发保守取SL(小时内OHLC顺序未知)
        sl_hit = bar_low and bar_low > 0 and bar_low <= sl_target
        tp_hit = bar_high and bar_high > 0 and bar_high >= tp_target
        if sl_hit:
            return SellSignal(reason='stop_loss', price=float(sl_target))
        if tp_hit:
            return SellSignal(reason='take_profit', price=float(tp_target))

        # 到期兜底: tpsl_d6=D6收盘(hours_held>=20); tpsl_d2c=D2收盘(买入次日即到期)
        expired = (position.hours_held >= self.max_hold_hours
                   if self.exit_mode == 'tpsl_d6' else True)
        if expired and hour == 4:
            price = data_feed.get_hour_close(position.code, date, 4)
            if not price or price <= 0:
                price = bar_open
            return SellSignal(reason='expired', price=float(price))
        return None

    def _sell_forward(self, position, date, hour, data_feed):
        """Task#12 前移变体: D2 H1内了结, 卖出当小时slot即可复用。
        跌停封死/缺数据顺延 → 之后任意小时尽快开盘价卖出(deferred)。"""
        code = position.code
        if hour != 1:
            price = data_feed.get_hour_open(code, date, hour)
            if price and price > 0:
                return SellSignal(reason='deferred', price=float(price))
            return None
        o = data_feed.get_hour_open(code, date, 1)
        c = data_feed.get_hour_close(code, date, 1)
        lo = data_feed.get_hour_low(code, date, 1)
        hi = data_feed.get_hour_high(code, date, 1)
        if not o or o <= 0:
            return None

        if self.exit_mode == 'd2h1_open':
            return SellSignal(reason='expired', price=float(o))

        if self.exit_mode == 'd2h1_close':
            price = c if c and c > 0 else o
            return SellSignal(reason='expired', price=float(price))

        if self.exit_mode == 'trail_h1':
            # 保守挂单语义: 触发价只基于H1_open, 不做bar内peak乐观假设
            trig = o * (1 - self.trail_pp / 100)
            if lo and lo > 0 and lo <= trig:
                return SellSignal(reason='stop_loss', price=float(trig))
            price = c if c and c > 0 else o
            return SellSignal(reason='expired', price=float(price))

        if self.exit_mode == 'tpsl_h1':
            bp = position.buy_price
            tp_p = bp * (1 + self.take_profit_pct)
            sl_p = bp * (1 + self.stop_loss_pct)
            if o <= sl_p:
                return SellSignal(reason='stop_loss', price=float(o))
            if o >= tp_p:
                return SellSignal(reason='take_profit', price=float(o))
            if lo and lo > 0 and lo <= sl_p:      # 双触发保守取SL
                return SellSignal(reason='stop_loss', price=float(sl_p))
            if hi and hi > 0 and hi >= tp_p:
                return SellSignal(reason='take_profit', price=float(tp_p))
            price = c if c and c > 0 else o       # 未触发: H1收盘强平
            return SellSignal(reason='expired', price=float(price))
        return None

    def get_buy_price(self, code, date, hour, data_feed):
        return data_feed.get_hour_open(code, date, hour)
