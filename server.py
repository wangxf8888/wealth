#!/usr/bin/env python3
"""
AIWealth 实盘信号 Web 服务
- 静态文件服务: /home/AIWealth/frontend/
- API 接口: 候选股/决策/持仓/历史交易/策略汇总
- 监听 0.0.0.0:80
"""
import os
import json
import glob
import re
import sqlite3
import urllib.request
from datetime import datetime
from flask import Flask, jsonify, send_from_directory, request

app = Flask(__name__, static_folder='/home/AIWealth/frontend', static_url_path='')

DATA_DIR = '/home/AIWealth/data/realtime'
FRONTEND_DIR = '/home/AIWealth/frontend'


# ===== 静态文件服务 =====

@app.route('/')
def index():
    return send_from_directory(FRONTEND_DIR, 'signal.html')


@app.route('/backtest')
def backtest():
    return send_from_directory(FRONTEND_DIR, 'dashboard.html')


@app.route('/v9')
def v9_dashboard():
    return send_from_directory(FRONTEND_DIR, 'index.html')


# ===== API 接口 =====

@app.route('/api/candidates/latest')
def api_candidates_latest():
    """获取最新候选股"""
    files = sorted(glob.glob(os.path.join(DATA_DIR, 'candidates_*.json')))
    if not files:
        return jsonify({'error': '暂无候选股数据', 'data': None})
    with open(files[-1], 'r', encoding='utf-8') as f:
        data = json.load(f)
    return jsonify(data)


@app.route('/api/candidates/history')
def api_candidates_history():
    """获取历史候选股列表(最近N天)"""
    n = request.args.get('days', 10, type=int)
    files = sorted(glob.glob(os.path.join(DATA_DIR, 'candidates_*.json')))[-n:]
    result = []
    for fp in reversed(files):
        fname = os.path.basename(fp)
        date_str = fname.replace('candidates_', '').replace('.json', '')
        try:
            with open(fp, 'r', encoding='utf-8') as f:
                data = json.load(f)
            total = sum(
                len(s.get('candidates', []))
                for s in data.get('strategies', {}).values()
            )
            result.append({
                'date': date_str,
                'trade_date': data.get('trade_date', date_str),
                'signal_date': data.get('signal_date', ''),
                'total_candidates': total,
                'file': fname
            })
        except Exception:
            pass
    return jsonify(result)


@app.route('/api/candidates/<date>')
def api_candidates_date(date):
    """获取指定日期候选股"""
    fp = os.path.join(DATA_DIR, f'candidates_{date}.json')
    if not os.path.exists(fp):
        return jsonify({'error': f'未找到 {date} 的候选股数据'}), 404
    with open(fp, 'r', encoding='utf-8') as f:
        data = json.load(f)
    return jsonify(data)


@app.route('/api/decision/latest')
def api_decision_latest():
    """获取最新买入决策"""
    files = sorted(glob.glob(os.path.join(DATA_DIR, 'decision_*.json')))
    if not files:
        return jsonify({'error': '暂无决策数据', 'data': None})
    with open(files[-1], 'r', encoding='utf-8') as f:
        data = json.load(f)
    return jsonify(data)


@app.route('/api/decision/<date>')
def api_decision_date(date):
    """获取指定日期决策"""
    fp = os.path.join(DATA_DIR, f'decision_{date}.json')
    if not os.path.exists(fp):
        return jsonify({'error': f'未找到 {date} 的决策数据'}), 404
    with open(fp, 'r', encoding='utf-8') as f:
        data = json.load(f)
    return jsonify(data)


@app.route('/api/decision/history')
def api_decision_history():
    """获取历史决策列表"""
    n = request.args.get('days', 10, type=int)
    files = sorted(glob.glob(os.path.join(DATA_DIR, 'decision_*.json')))[-n:]
    result = []
    for fp in reversed(files):
        fname = os.path.basename(fp)
        date_str = fname.replace('decision_', '').replace('.json', '')
        try:
            with open(fp, 'r', encoding='utf-8') as f:
                data = json.load(f)
            total_recs = 0
            for s in data.get('strategies', {}).values():
                total_recs += len(s.get('recommendations', []))
            result.append({
                'date': date_str,
                'trade_date': data.get('trade_date', date_str),
                'decided_at': data.get('decided_at', ''),
                'market_filter': data.get('market_filter', ''),
                'total_recommendations': total_recs,
            })
        except Exception:
            pass
    return jsonify(result)


@app.route('/api/positions')
def api_positions():
    """获取当前持仓"""
    fp = os.path.join(DATA_DIR, 'positions.json')
    if not os.path.exists(fp):
        return jsonify({'positions': [], 'error': '暂无持仓数据'})
    with open(fp, 'r', encoding='utf-8') as f:
        data = json.load(f)
    return jsonify(data)


@app.route('/api/nav_history')
def api_nav_history():
    """返回2021至今完整净值曲线: 回测daily_nav + 实盘交易记录拼接。

    数据来源:
    - 回测部分: /home/AIWealth/frontend/data/combined_5slot_new_trades.json 的 daily_nav 字段
    - 实盘部分: positions.json 的 closed_trades / positions 计算

    注: 回测引擎采用动态再平衡模式(买入金额=总净值/5), 实盘同样采用此模式。
    """
    initial_capital = 1000000
    nav_history = []

    # ========== Part 1: 回测 daily_nav (2021-01 ~ 2026-07-01) ==========
    backtest_file = '/home/AIWealth/frontend/data/combined_5slot_new_trades.json'
    backtest_end_nav = initial_capital
    backtest_end_date = None

    if os.path.exists(backtest_file):
        try:
            with open(backtest_file, 'r', encoding='utf-8') as f:
                bt_data = json.load(f)
            daily_nav = bt_data.get('daily_nav', {})
            if daily_nav:
                sorted_dates = sorted(daily_nav.keys())
                for d in sorted_dates:
                    nav_val = daily_nav[d]
                    return_pct = round((nav_val / initial_capital - 1) * 100, 2)
                    nav_history.append({
                        'date': d,
                        'nav': round(nav_val, 2),
                        'return_pct': return_pct
                    })
                backtest_end_nav = daily_nav[sorted_dates[-1]]
                backtest_end_date = sorted_dates[-1]
        except Exception as e:
            print(f"[nav_history] 回测数据加载失败: {e}")

    # ========== Part 2: 实盘交易 (2026-07-13 ~ 今天) ==========
    pos_file = os.path.join(DATA_DIR, 'positions.json')
    total_live_trades = 0

    if os.path.exists(pos_file):
        try:
            with open(pos_file, 'r', encoding='utf-8') as f:
                pos_data = json.load(f)

            # 优先使用account字段
            account = pos_data.get('account', {})
            positions = pos_data.get('positions', [])
            closed_trades = pos_data.get('closed_trades', [])

            # 兼容旧格式: positions列表中的closed记录
            if not closed_trades:
                closed_trades = [p for p in positions if p.get('status') == 'closed']

            total_live_trades = len(closed_trades)

            if closed_trades:
                # 实盘起点NAV = 回测终点NAV (连续性)
                live_start_nav = backtest_end_nav
                live_initial_capital = account.get('initial_capital', initial_capital)

                # 按卖出日期汇总盈亏 (实盘用动态再平衡: 每slot金额 = 当时总NAV/5)
                # 简化计算: 用 pnl_pct * (live_initial_capital/5) 近似
                daily_pnl = {}
                for t in closed_trades:
                    sell_date = t.get('sell_date', '')
                    if not sell_date:
                        continue
                    # 如果有buy_amount字段则用精确金额, 否则按初始资金/5估算
                    buy_amount = t.get('buy_amount', live_initial_capital / 5)
                    pnl_amount = buy_amount * (t.get('pnl_pct', 0) / 100.0)
                    daily_pnl.setdefault(sell_date, 0)
                    daily_pnl[sell_date] += pnl_amount

                # 生成实盘NAV序列 (在回测曲线之后)
                cumulative_live_nav = live_start_nav
                for d in sorted(daily_pnl.keys()):
                    # 跳过回测已覆盖的日期
                    if backtest_end_date and d <= backtest_end_date:
                        continue
                    cumulative_live_nav += daily_pnl[d]
                    return_pct = round((cumulative_live_nav / initial_capital - 1) * 100, 2)
                    nav_history.append({
                        'date': d,
                        'nav': round(cumulative_live_nav, 2),
                        'return_pct': return_pct,
                        'source': 'live'
                    })
        except Exception as e:
            print(f"[nav_history] 实盘数据加载失败: {e}")

    # 总交易笔数
    total_trades = total_live_trades
    if os.path.exists(backtest_file):
        try:
            with open(backtest_file, 'r', encoding='utf-8') as f:
                bt_data = json.load(f)
            total_trades += len(bt_data.get('trades', []))
        except Exception:
            pass

    return jsonify({
        'nav_history': nav_history,
        'initial_capital': initial_capital,
        'total_trades': total_trades,
        'total_return_pct': nav_history[-1]['return_pct'] if nav_history else 0,
        'data_range': {
            'backtest_start': nav_history[0]['date'] if nav_history else None,
            'backtest_end': backtest_end_date,
            'live_trades': total_live_trades,
        },
        'note': '回测+实盘均采用动态再平衡模式(每slot买入金额=总净值/5)'
    })


@app.route('/api/strategies')
def api_strategies():
    """获取策略矩阵汇总"""
    fp = os.path.join(FRONTEND_DIR, 'data', 'strategies_summary.json')
    if not os.path.exists(fp):
        return jsonify({'error': '暂无策略汇总数据'})
    with open(fp, 'r', encoding='utf-8') as f:
        data = json.load(f)
    return jsonify(data)


@app.route('/api/status')
def api_status():
    """系统状态"""
    # 最新文件时间
    cand_files = sorted(glob.glob(os.path.join(DATA_DIR, 'candidates_*.json')))
    dec_files = sorted(glob.glob(os.path.join(DATA_DIR, 'decision_*.json')))
    pos_file = os.path.join(DATA_DIR, 'positions.json')

    # 计算最后交易日期和空窗期原因
    last_trade_date = None
    idle_info = []
    if os.path.exists(pos_file):
        try:
            with open(pos_file, 'r', encoding='utf-8') as f:
                pos_data = json.load(f)
            closed_trades = pos_data.get('closed_trades', [])
            if closed_trades:
                sell_dates = [t.get('sell_date', '') for t in closed_trades if t.get('sell_date')]
                if sell_dates:
                    last_trade_date = max(sell_dates)
        except Exception:
            pass

    # 从决策文件中提取空窗期原因
    if last_trade_date and dec_files:
        for fp in dec_files:
            fname = os.path.basename(fp)
            date_str = fname.replace('decision_', '').replace('.json', '')
            # 格式化日期
            try:
                d_formatted = f"{date_str[:4]}-{date_str[4:6]}-{date_str[6:8]}"
            except Exception:
                continue
            if d_formatted <= last_trade_date:
                continue
            try:
                with open(fp, 'r', encoding='utf-8') as f:
                    dec_data = json.load(f)
                mf = dec_data.get('market_filter', '')
                if mf:
                    idle_info.append({'date': d_formatted, 'reason': '大盘过滤: ' + mf})
                else:
                    # 检查是否有推荐但未执行
                    total_recs = sum(
                        len(s.get('recommendations', []))
                        for s in dec_data.get('strategies', {}).values()
                    )
                    if total_recs == 0:
                        idle_info.append({'date': d_formatted, 'reason': '无满足条件的候选股'})
                    else:
                        idle_info.append({'date': d_formatted, 'reason': f'有{total_recs}个信号未执行(槽位已满或回溯生成)'})
            except Exception:
                idle_info.append({'date': d_formatted, 'reason': '决策文件读取异常'})

    status = {
        'server_time': datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
        'latest_candidates': os.path.basename(cand_files[-1]) if cand_files else None,
        'latest_decision': os.path.basename(dec_files[-1]) if dec_files else None,
        'positions_updated': datetime.fromtimestamp(
            os.path.getmtime(pos_file)
        ).strftime('%Y-%m-%d %H:%M:%S') if os.path.exists(pos_file) else None,
        'total_candidate_files': len(cand_files),
        'total_decision_files': len(dec_files),
        'last_trade_date': last_trade_date,
        'idle_days': len(idle_info),
        'idle_info': idle_info,
    }
    return jsonify(status)


@app.route('/api/realtime_quotes')
def api_realtime_quotes():
    """实时行情代理 - 通过腾讯财经接口获取实时行情"""
    codes = request.args.get('codes', '')
    if not codes:
        return jsonify({'error': '缺少codes参数'})

    code_list = [c.strip() for c in codes.split(',') if c.strip()]
    if not code_list:
        return jsonify({'error': '无有效股票代码'})

    # 转换代码格式: sh.600162 -> sh600162, sz.002623 -> sz002623
    sina_codes = []
    code_map = {}
    for code in code_list:
        sina_code = code.replace('.', '')
        sina_codes.append(sina_code)
        code_map[sina_code] = code

    results = {}

    # 方案1: 尝试腾讯财经接口 (更稳定)
    try:
        url = f"http://qt.gtimg.cn/q={','.join(sina_codes)}"
        req = urllib.request.Request(url)
        req.add_header('User-Agent', 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36')
        resp = urllib.request.urlopen(req, timeout=10)
        content = resp.read().decode('gbk')

        for line in content.strip().split('\n'):
            line = line.strip()
            if not line:
                continue
            m = re.match(r'v_(\w+)="(.*)"', line)
            if not m:
                continue
            sina_sym = m.group(1)
            data_str = m.group(2)
            if not data_str:
                continue
            parts = data_str.split('~')
            if len(parts) < 46:
                continue

            original_code = code_map.get(sina_sym, sina_sym)
            try:
                name = parts[1]
                current_price = float(parts[3]) if parts[3] else 0
                yesterday_close = float(parts[4]) if parts[4] else 0
                today_open = float(parts[5]) if parts[5] else 0
                high = float(parts[33]) if parts[33] else 0
                low = float(parts[34]) if parts[34] else 0
                volume = parts[36] if len(parts) > 36 else ''
                amount = parts[37] if len(parts) > 37 else ''
                change_pct = float(parts[32]) if parts[32] else 0

                if current_price > 0:
                    results[original_code] = {
                        'name': name,
                        'price': current_price,
                        'yesterday_close': yesterday_close,
                        'open': today_open,
                        'high': high,
                        'low': low,
                        'change_pct': change_pct,
                        'volume': volume,
                        'amount': amount,
                    }
            except (ValueError, IndexError):
                continue

    except Exception as e:
        # 方案2: 备选新浪接口
        try:
            url = f"http://hq.sinajs.cn/list={','.join(sina_codes)}"
            req = urllib.request.Request(url)
            req.add_header('User-Agent', 'Mozilla/5.0')
            req.add_header('Referer', 'https://finance.sina.com.cn')
            resp = urllib.request.urlopen(req, timeout=10)
            content = resp.read().decode('gbk')

            for line in content.strip().split('\n'):
                m = re.match(r'var hq_str_(\w+)="(.+)"', line)
                if not m:
                    continue
                sina_sym = m.group(1)
                data_str = m.group(2)
                parts = data_str.split(',')
                if len(parts) < 32:
                    continue

                original_code = code_map.get(sina_sym, sina_sym)
                try:
                    name = parts[0]
                    today_open = float(parts[1]) if parts[1] else 0
                    yesterday_close = float(parts[2]) if parts[2] else 0
                    current_price = float(parts[3]) if parts[3] else 0
                    high = float(parts[4]) if parts[4] else 0
                    low = float(parts[5]) if parts[5] else 0
                    change_pct = round((current_price / yesterday_close - 1) * 100, 2) if yesterday_close > 0 else 0

                    if current_price > 0:
                        results[original_code] = {
                            'name': name,
                            'price': current_price,
                            'yesterday_close': yesterday_close,
                            'open': today_open,
                            'high': high,
                            'low': low,
                            'change_pct': change_pct,
                            'volume': parts[8] if len(parts) > 8 else '',
                            'amount': parts[9] if len(parts) > 9 else '',
                        }
                except (ValueError, IndexError):
                    continue
        except Exception as e2:
            return jsonify({'error': f'行情获取失败: tencent={e}, sina={e2}', 'data': {}})

    return jsonify({'data': results, 'updated_at': datetime.now().strftime('%Y-%m-%d %H:%M:%S')})


# ===== Trades History Cache =====
_trades_cache = {'data': None, 'mtime': 0}


def _load_stock_names():
    """从数据库加载股票代码->名称映射"""
    db_path = '/home/AIWealth/data/stocks.db'
    names = {}
    try:
        conn = sqlite3.connect(db_path)
        cur = conn.cursor()
        cur.execute("SELECT code, code_name FROM stock_list")
        for row in cur.fetchall():
            names[row[0]] = row[1]
        conn.close()
    except Exception:
        pass
    return names


def _load_all_trades():
    """加载所有回测+实盘交易，带缓存"""
    backtest_file = '/home/AIWealth/frontend/data/combined_5slot_new_trades.json'
    pos_file = os.path.join(DATA_DIR, 'positions.json')

    # 检查是否需要刷新缓存
    bt_mtime = os.path.getmtime(backtest_file) if os.path.exists(backtest_file) else 0
    pos_mtime = os.path.getmtime(pos_file) if os.path.exists(pos_file) else 0
    latest_mtime = max(bt_mtime, pos_mtime)

    if _trades_cache['data'] is not None and _trades_cache['mtime'] >= latest_mtime:
        return _trades_cache['data']

    # 加载股票名称
    stock_names = _load_stock_names()

    all_trades = []

    # 加载回测交易
    if os.path.exists(backtest_file):
        try:
            with open(backtest_file, 'r', encoding='utf-8') as f:
                bt_data = json.load(f)
            for t in bt_data.get('trades', []):
                code = t.get('code', '')
                name = stock_names.get(code, code.split('.')[1] if '.' in code else code)
                all_trades.append({
                    'code': code,
                    'name': name,
                    'strategy': t.get('strategy_name', ''),
                    'strategy_tag': t.get('strategy_tag', ''),
                    'buy_date': t.get('buy_date', ''),
                    'sell_date': t.get('sell_date', ''),
                    'buy_price': t.get('buy_price', 0),
                    'sell_price': t.get('sell_price', 0),
                    'pnl_pct': round(t.get('profit_pct', 0), 2),
                    'sell_reason': t.get('reason', ''),
                    'hold_hours': t.get('hold_hours', 0),
                    'source': 'backtest'
                })
        except Exception as e:
            print(f"[trades_history] 回测数据加载失败: {e}")

    # 加载实盘交易
    if os.path.exists(pos_file):
        try:
            with open(pos_file, 'r', encoding='utf-8') as f:
                pos_data = json.load(f)
            for t in pos_data.get('closed_trades', []):
                all_trades.append({
                    'code': t.get('code', ''),
                    'name': t.get('name', ''),
                    'strategy': t.get('strategy', ''),
                    'strategy_tag': t.get('slot_id', ''),
                    'buy_date': t.get('buy_date', ''),
                    'sell_date': t.get('sell_date', ''),
                    'buy_price': t.get('buy_price', 0),
                    'sell_price': t.get('sell_price', 0),
                    'pnl_pct': round(t.get('pnl_pct', 0), 2),
                    'sell_reason': t.get('sell_reason', ''),
                    'hold_hours': 0,
                    'source': 'live'
                })
        except Exception as e:
            print(f"[trades_history] 实盘数据加载失败: {e}")

    # 按卖出日期倒序
    all_trades.sort(key=lambda x: x.get('sell_date', ''), reverse=True)

    _trades_cache['data'] = all_trades
    _trades_cache['mtime'] = latest_mtime
    return all_trades


@app.route('/api/trades/history')
def api_trades_history():
    """历史交易记录（回测+实盘），支持分页和筛选"""
    page = request.args.get('page', 1, type=int)
    page_size = request.args.get('page_size', 10, type=int)
    strategy = request.args.get('strategy', '')  # 策略筛选

    all_trades = _load_all_trades()

    # 策略筛选
    if strategy:
        filtered = [t for t in all_trades if t['strategy'] == strategy or t['strategy_tag'] == strategy]
    else:
        filtered = all_trades

    # 计算汇总
    total = len(filtered)
    wins = sum(1 for t in filtered if t['pnl_pct'] > 0)
    win_rate = round(wins / total * 100, 2) if total > 0 else 0
    avg_return = round(sum(t['pnl_pct'] for t in filtered) / total, 2) if total > 0 else 0

    # 分页
    start = (page - 1) * page_size
    end = start + page_size
    page_trades = filtered[start:end]

    return jsonify({
        'total': total,
        'page': page,
        'page_size': page_size,
        'has_more': end < total,
        'trades': page_trades,
        'summary': {
            'total_trades': total,
            'win_rate': win_rate,
            'avg_return': avg_return,
            'total_profit': round(sum(t['pnl_pct'] for t in filtered if t['pnl_pct'] > 0), 2),
            'total_loss': round(sum(t['pnl_pct'] for t in filtered if t['pnl_pct'] < 0), 2),
        }
    })


# ===== CORS =====
@app.after_request
def add_cors(response):
    response.headers['Access-Control-Allow-Origin'] = '*'
    response.headers['Access-Control-Allow-Methods'] = 'GET, OPTIONS'
    response.headers['Access-Control-Allow-Headers'] = 'Content-Type'
    return response


if __name__ == '__main__':
    print(f'[{datetime.now()}] AIWealth Web Server starting on http://0.0.0.0:80')
    print(f'  Frontend: {FRONTEND_DIR}')
    print(f'  Data: {DATA_DIR}')
    print(f'  APIs: /api/candidates/latest, /api/decision/latest, /api/positions, /api/strategies')
    app.run(host='0.0.0.0', port=80, debug=False)
