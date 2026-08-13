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
import time
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


# ===== 回测solo档案只读服务（Dashboard数据源，白名单防路径穿越）=====

SOLO_DIR = '/home/AIWealth/logs/backtest/solo'
SOLO_WHITELIST = {
    'firstboard_low_open_dip_v2',
    'amplitude_reversal',
    'gem_star_late_seal',
    'big_yang_low_open_v2',
    'two_board_pullback_dip_h1c',
}


@app.route('/data/solo/<name>_trades.json')
def data_solo_trades(name):
    """只读serve在产策略solo回测档案；name严格限定白名单，杜绝路径穿越"""
    if name not in SOLO_WHITELIST:
        return jsonify({'error': f'策略 {name} 不在白名单内'}), 404
    return send_from_directory(SOLO_DIR, f'{name}_trades.json', mimetype='application/json')


# ===== API 接口 =====

# [Task#209] 候选latest 10s短缓存(配合前端10s统一刷新, 减磁盘IO)
_cand_cache = {'ts': 0, 'payload': None}
_CAND_CACHE_TTL = 10           # 秒


@app.route('/api/candidates/latest')
def api_candidates_latest():
    """获取最新候选股 (10s进程内短缓存)"""
    now = time.time()
    if _cand_cache['payload'] is not None and now - _cand_cache['ts'] < _CAND_CACHE_TTL:
        return jsonify(_cand_cache['payload'])
    files = sorted(glob.glob(os.path.join(DATA_DIR, 'candidates_*.json')))
    if not files:
        return jsonify({'error': '暂无候选股数据', 'data': None})
    with open(files[-1], 'r', encoding='utf-8') as f:
        data = json.load(f)
    _cand_cache['payload'] = data
    _cand_cache['ts'] = now
    return jsonify(data)


# ===== 候选自助排除 (Task#209) =====
# user_excluded.json 结构: {"YYYY-MM-DD": ["sh.xxxxxx", ...], "_log": [{code,action,date,ts}]}
# 日期键数组语义与消费方(generate_candidates/morning_decision的load_user_excluded)完全兼容,
# 两消费方均仅data.get(trade_date)读日期键, _log/_comment等非日期键仅留痕不参与过滤。
USER_EXCLUDED_FILE = os.path.join(DATA_DIR, 'user_excluded.json')
_CODE_RE = re.compile(r'^(sh|sz|bj)\.\d{6}$')
_DATE_RE = re.compile(r'^\d{4}-\d{2}-\d{2}$')


def _load_user_excluded_raw():
    try:
        with open(USER_EXCLUDED_FILE, 'r', encoding='utf-8') as f:
            data = json.load(f)
        if isinstance(data, dict):
            return data
    except Exception:
        pass
    return {}


def _save_user_excluded_raw(data):
    """原子写(tmp+rename), 与候选json落盘同款防半成品"""
    tmp = USER_EXCLUDED_FILE + '.tmp'
    with open(tmp, 'w', encoding='utf-8') as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    os.replace(tmp, USER_EXCLUDED_FILE)


def _exclude_op(action):
    body = request.get_json(silent=True) or {}
    code = str(body.get('code', '')).strip()
    date_str = str(body.get('date', '')).strip() or datetime.now().strftime('%Y-%m-%d')
    if not _CODE_RE.match(code):
        return jsonify({'ok': False, 'error': f'非法代码: {code}'}), 400
    if not _DATE_RE.match(date_str):
        return jsonify({'ok': False, 'error': f'非法日期: {date_str}'}), 400
    data = _load_user_excluded_raw()
    codes = data.get(date_str)
    if not isinstance(codes, list):
        codes = []
    if action == 'exclude':
        if code not in codes:
            codes.append(code)
    else:
        codes = [c for c in codes if c != code]
    data[date_str] = codes
    log = data.get('_log')
    if not isinstance(log, list):
        log = []
    log.append({'code': code, 'action': action, 'date': date_str,
                'ts': datetime.now().strftime('%Y-%m-%d %H:%M:%S')})
    data['_log'] = log[-500:]    # 留痕上限, 防无限膨胀
    _save_user_excluded_raw(data)
    return jsonify({'ok': True, 'date': date_str, 'excluded': codes})


@app.route('/api/exclude_candidate', methods=['POST'])
def api_exclude_candidate():
    """[Task#209] 用户排除候选(仅未买入候选有效): 日期键数组追加+_log留痕"""
    return _exclude_op('exclude')


@app.route('/api/restore_candidate', methods=['POST'])
def api_restore_candidate():
    """[Task#209] 撤销排除"""
    return _exclude_op('restore')


@app.route('/api/excluded_candidates')
def api_excluded_candidates():
    """[Task#209] 查询某日已排除候选列表, 默认今天"""
    date_str = request.args.get('date') or datetime.now().strftime('%Y-%m-%d')
    if not _DATE_RE.match(date_str):
        return jsonify({'error': 'bad date'}), 400
    data = _load_user_excluded_raw()
    codes = data.get(date_str)
    return jsonify({'date': date_str,
                    'excluded': codes if isinstance(codes, list) else []})


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
    """获取当前持仓，包含实盘统计（胜率含持仓浮盈）"""
    fp = os.path.join(DATA_DIR, 'positions.json')
    if not os.path.exists(fp):
        return jsonify({'positions': [], 'error': '暂无持仓数据'})
    with open(fp, 'r', encoding='utf-8') as f:
        data = json.load(f)

    # 计算实盘统计（含持仓浮盈/浮亏）
    LIVE_START_DATE = '2026-07-22'
    closed_trades = [t for t in data.get('closed_trades', []) if t.get('buy_date', '') >= LIVE_START_DATE]
    positions = data.get('positions', [])

    # 获取持仓实时行情计算浮盈
    # [Task#285追加] 当日卖出条目并入同一次qt批量请求(卖飞判断, 历史卖出不查)
    today_str = datetime.now().strftime('%Y-%m-%d')
    today_sold = [t for t in data.get('closed_trades', [])
                  if t.get('sell_date') == today_str and t.get('code')]
    holding_codes = [p['code'] for p in positions if p.get('code')]
    query_codes = holding_codes + [t['code'] for t in today_sold
                                   if t['code'] not in holding_codes]
    realtime_prices = {}
    realtime_pcts = {}   # [Task#301追加] code -> 当日涨跌幅(现价vs昨收, qt源parts[32])
    if query_codes:
        try:
            sina_codes = [c.replace('.', '') for c in query_codes]
            url = f"http://qt.gtimg.cn/q={','.join(sina_codes)}"
            req = urllib.request.Request(url)
            req.add_header('User-Agent', 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36')
            resp = urllib.request.urlopen(req, timeout=5)
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
                if len(parts) >= 4:
                    try:
                        price = float(parts[3])
                        if price > 0:
                            # [Task#301追加] 当日涨跌幅(现价vs昨收): qt源parts[32]直出,
                            # 缺失时用昨收parts[4]自算——与/api/realtime_quotes的change_pct同口径
                            pct_today = None
                            try:
                                if len(parts) > 32 and parts[32]:
                                    pct_today = float(parts[32])
                                elif len(parts) > 4 and parts[4] and float(parts[4]) > 0:
                                    pct_today = round((price / float(parts[4]) - 1) * 100, 2)
                            except (ValueError, IndexError):
                                pct_today = None
                            # 转回原始代码格式
                            for orig_code in query_codes:
                                if orig_code.replace('.', '') == sina_sym:
                                    realtime_prices[orig_code] = price
                                    if pct_today is not None:
                                        realtime_pcts[orig_code] = pct_today
                                    break
                    except (ValueError, IndexError):
                        pass
        except Exception as e:
            print(f"[api_positions] 获取实时行情失败: {e}")

    # [Task#285追加] 当日卖出条目注入实时价与较卖价差(原地改dict, 随data返回;
    # 行情缺失不注字段, 前端显示"--", 不影响既有统计)
    # [Task#301追加] 同步注入pct_today(当日涨跌幅=现价vs昨收), 前端三段式展示
    for t in today_sold:
        px = realtime_prices.get(t['code'])
        sell_px = t.get('sell_price') or 0
        if px and sell_px > 0:
            t['realtime_px'] = px
            t['vs_sell_pct'] = round((px / sell_px - 1) * 100, 2)
            pct = realtime_pcts.get(t['code'])
            if pct is not None:
                t['pct_today'] = pct

    # 统计胜率：closed_trades盈亏 + 持仓浮盈/浮亏
    wins = sum(1 for t in closed_trades if t.get('pnl_pct', 0) > 0)
    holding_wins = 0
    holding_pnl_details = []
    for p in positions:
        code = p.get('code', '')
        buy_price = p.get('buy_price', 0)
        current_price = realtime_prices.get(code, 0)
        if current_price > 0 and buy_price > 0:
            pnl_pct = (current_price - buy_price) / buy_price * 100
            holding_pnl_details.append({'code': code, 'pnl_pct': round(pnl_pct, 2), 'current_price': current_price})
            if pnl_pct >= 0:  # 盈利或平 = 赢
                holding_wins += 1
        else:
            holding_pnl_details.append({'code': code, 'pnl_pct': None, 'current_price': current_price})

    total_count = len(closed_trades) + len(positions)
    total_wins = wins + holding_wins
    win_rate = round(total_wins / total_count * 100, 1) if total_count > 0 else 0

    # 计算当前总资产(NAV)
    account = data.get('account', {})
    initial_capital = account.get('initial_capital', 1000000)
    # 基于实时价格重算NAV
    realized_pnl = sum(
        (t.get('buy_amount', initial_capital / 5) * t.get('pnl_pct', 0) / 100)
        for t in closed_trades
    )
    unrealized_pnl = 0
    for p in positions:
        code = p.get('code', '')
        buy_price = p.get('buy_price', 0)
        buy_amount = p.get('buy_amount', initial_capital / 5)
        current_price = realtime_prices.get(code, buy_price)
        if buy_price > 0:
            unrealized_pnl += buy_amount * (current_price - buy_price) / buy_price

    current_nav = initial_capital + realized_pnl + unrealized_pnl

    # 注入统计到返回数据
    data['live_stats'] = {
        'live_start_date': LIVE_START_DATE,
        'total_trades': total_count,
        'closed_trades_count': len(closed_trades),
        'holding_count': len(positions),
        'wins': total_wins,
        'win_rate': win_rate,
        'realized_pnl': round(realized_pnl, 2),
        'unrealized_pnl': round(unrealized_pnl, 2),
        'current_nav': round(current_nav, 2),
        'return_pct': round((current_nav / initial_capital - 1) * 100, 2),
        'holding_pnl_details': holding_pnl_details,
    }
    return jsonify(data)


@app.route('/api/live/equity')
def api_live_equity():
    """实盘资金曲线 - 盯市口径，7-21为基准日（起始点），7-22开始实际交易。

    [2026-08-03 equityfix] 历史点(今天以前)改用 drawdown_state.json 的 nav_history
    (每晚19:00盘后盯市快照: 现金+持仓×最新收盘价), 修复原"卖出日已实现盈亏"构点
    导致的时序失真(浮盈不进历史点: 7/31大涨日曲线不动、8/3回落日曲线反涨)。
    当日最后一点保留实时逻辑: initial + Σrealized + Σunrealized = 现金+持仓市值,
    与19:00快照同口径。nav_history缺失交易日不补造假点, response.history_gaps列出。
    """
    BASELINE_DATE = '2026-07-21'  # 基准日：实盘前一天，净值=初始资金
    LIVE_START_DATE = '2026-07-22'  # 实际交易开始日
    pos_file = os.path.join(DATA_DIR, 'positions.json')
    initial_capital = 1000000

    if not os.path.exists(pos_file):
        return jsonify({'equity_curve': [{'date': BASELINE_DATE, 'equity': initial_capital, 'return_pct': 0}], 'initial': initial_capital})

    with open(pos_file, 'r', encoding='utf-8') as f:
        pos_data = json.load(f)

    account = pos_data.get('account', {})
    closed_trades = [t for t in pos_data.get('closed_trades', []) if t.get('buy_date', '') >= LIVE_START_DATE]
    positions = pos_data.get('positions', [])
    today_str = datetime.now().strftime('%Y-%m-%d')

    equity_points = []
    # 基准日起始点：实盘前一天 = 初始资金
    equity_points.append({'date': BASELINE_DATE, 'equity': initial_capital, 'return_pct': 0})

    # 历史点(今天以前): drawdown_state.nav_history 盯市快照(每晚19:00落盘)
    snap_dates = set()
    dd_file = os.path.join(DATA_DIR, 'drawdown_state.json')
    if os.path.exists(dd_file):
        try:
            with open(dd_file, 'r', encoding='utf-8') as f:
                dd_state = json.load(f)
            for d, nav in sorted(dd_state.get('nav_history', [])):
                if d < LIVE_START_DATE or d >= today_str:
                    continue
                snap_dates.add(d)
                equity_points.append({
                    'date': d,
                    'equity': round(float(nav), 2),
                    'return_pct': round((float(nav) / initial_capital - 1) * 100, 2),
                })
        except Exception as e:
            print(f"[live_equity] 读取盯市快照失败: {e}")

    # 缺口容错: 对照stock_kline交易日历(7/22以来, 不含今天), 缺失日不补造假点
    history_gaps = []
    try:
        conn = sqlite3.connect('file:/home/AIWealth/data/stocks.db?mode=ro', uri=True)
        rows = conn.execute(
            "SELECT DISTINCT date FROM stock_kline WHERE date>=? AND date<? ORDER BY date",
            (LIVE_START_DATE, today_str)).fetchall()
        conn.close()
        history_gaps = [r[0] for r in rows if r[0] not in snap_dates]
    except Exception as e:
        print(f"[live_equity] 交易日历查询失败: {e}")

    # 当日实时点的realized基数: 全量closed_trades累计(与历史盯市点口径连续)
    cumulative_nav = initial_capital
    for t in closed_trades:
        if not t.get('sell_date'):
            continue
        buy_amount = t.get('buy_amount', initial_capital / 5)
        cumulative_nav += buy_amount * (t.get('pnl_pct', 0) / 100.0)

    # 最新点：加上持仓浮盈
    # 获取实时价格计算浮盈
    holding_codes = [p['code'] for p in positions if p.get('code')]
    unrealized_pnl = 0
    if holding_codes:
        try:
            sina_codes = [c.replace('.', '') for c in holding_codes]
            url = f"http://qt.gtimg.cn/q={','.join(sina_codes)}"
            req = urllib.request.Request(url)
            req.add_header('User-Agent', 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36')
            resp = urllib.request.urlopen(req, timeout=5)
            content = resp.read().decode('gbk')
            realtime_prices = {}
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
                if len(parts) >= 4:
                    try:
                        price = float(parts[3])
                        if price > 0:
                            for orig_code in holding_codes:
                                if orig_code.replace('.', '') == sina_sym:
                                    realtime_prices[orig_code] = price
                                    break
                    except (ValueError, IndexError):
                        pass

            for p in positions:
                code = p.get('code', '')
                buy_price = p.get('buy_price', 0)
                buy_amount = p.get('buy_amount', initial_capital / 5)
                current_price = realtime_prices.get(code, buy_price)
                if buy_price > 0:
                    unrealized_pnl += buy_amount * (current_price - buy_price) / buy_price
        except Exception as e:
            print(f"[live_equity] 获取实时行情失败: {e}")

    current_nav = cumulative_nav + unrealized_pnl
    if today_str >= LIVE_START_DATE:
        return_pct = round((current_nav / initial_capital - 1) * 100, 2)
        # 如果今天已有equity点（起始点或closed_trade点），更新它；否则新增
        if equity_points[-1]['date'] == today_str:
            equity_points[-1]['equity'] = round(current_nav, 2)
            equity_points[-1]['return_pct'] = return_pct
        else:
            equity_points.append({'date': today_str, 'equity': round(current_nav, 2), 'return_pct': return_pct})

    return jsonify({
        'equity_curve': equity_points,
        'initial': initial_capital,
        'current_nav': round(current_nav, 2),
        'return_pct': round((current_nav / initial_capital - 1) * 100, 2),
        'holding_count': len(positions),
        'closed_count': len(closed_trades),
        'source': 'mark_to_market',
        'history_gaps': history_gaps,
    })


# 策略中文正名(汇报/前端展示统一口径; 技术代号仅限代码内部)
STRATEGY_CN = {
    'firstboard_low_open_dip_v2': '首板低吸',
    'amplitude_reversal': '巨振反转',
    'gem_star_late_seal': '创科晚封',
    'big_yang_low_open_v2': '大阳低吸',
    'two_board_pullback_dip_h1c': '双板回调低吸',
    'early_surge_chase_0940': '早盘冲板追击',
    'limitup_early_seal': '涨停早封',            # 历史(旧S1)
    'gem_star_limitup_low_open': '创科涨停低开',  # 历史(旧S5)
}


@app.route('/api/live/events')
def api_live_events():
    """[Task#84] 当日交易事件流(滚动信息栏数据源), 按时间升序。

    返回 [{time:'HH:MM:SS', type:'buy'|'sell'|'shadow', code, name, price,
           strategy_cn, pnl_pct(仅卖出), note}]。
    数据源与时间口径:
    - 买入: positions/closed_trades 中 buy_date=当日者;
      时间=decision_当日.json 的 decided_at, 缺省'09:30'(实际成交时间)。
    - 卖出: closed_trades 中 sell_date=当日者; 时间降级序:
      ①selling_locked.ts 精确时间戳(盘中守护卖出加锁时刻)
      ②sell_mode=timed 的 sell_at.time 设定时刻 ③'15:00'收盘档兜底。
    - 影子信号: research/results/shadow_surge/shadow_signals_当日.json
      (测试中策略, 不动真金); 文件不存在静默跳过。
    支持 ?date=YYYY-MM-DD 调试/验收回看, 默认今天。
    """
    date_str = request.args.get('date') or datetime.now().strftime('%Y-%m-%d')
    if not re.match(r'^\d{4}-\d{2}-\d{2}$', date_str):
        return jsonify({'error': 'bad date'}), 400
    ymd = date_str.replace('-', '')
    events = []

    # 决策时刻(当日买入事件的时间戳来源, 秒级精度)
    buy_time = '09:30:00'
    dec_file = os.path.join(DATA_DIR, f'decision_{ymd}.json')
    if os.path.exists(dec_file):
        try:
            with open(dec_file, 'r', encoding='utf-8') as f:
                decided_at = json.load(f).get('decided_at') or ''
            # 提取 HH:MM:SS 秒级时间戳
            m = re.search(r'(\d{2}:\d{2}:\d{2})', decided_at)
            if not m:
                m = re.search(r'(\d{2}:\d{2})', decided_at)
            if m:
                raw = m.group(1)
                if len(raw) == 5:          # HH:MM → 补 :00
                    raw += ':00'
                # 09:25:xx 决策时刻 → 09:30:00 实际成交时刻
                if raw.startswith('09:25'):
                    buy_time = '09:30:00'
                else:
                    buy_time = raw
        except Exception as e:
            print(f"[live_events] decision读取失败: {e}")

    pos_file = os.path.join(DATA_DIR, 'positions.json')
    if os.path.exists(pos_file):
        try:
            with open(pos_file, 'r', encoding='utf-8') as f:
                pos_data = json.load(f)
            positions = pos_data.get('positions', [])
            closed = pos_data.get('closed_trades', [])

            # 买入事件: 当日新开仓(在持仓中) + 当日买入且已平仓(防御性合并)
            seen_buys = set()
            for p in list(positions) + list(closed):
                if p.get('buy_date') != date_str:
                    continue
                key = (p.get('code'), p.get('buy_price'))
                if key in seen_buys:
                    continue
                seen_buys.add(key)
                strat = p.get('strategy') or ''
                # 计算持股天数: expire_date - buy_date
                hold_days = ''
                try:
                    bd = datetime.strptime(p.get('buy_date', ''), '%Y-%m-%d')
                    ed = datetime.strptime(p.get('expire_date', ''), '%Y-%m-%d')
                    hold_days = str((ed - bd).days)
                except Exception:
                    pass
                events.append({
                    'time': buy_time, 'type': 'buy',
                    'code': p.get('code'), 'name': p.get('name') or '',
                    'price': p.get('buy_price'),
                    'strategy_cn': STRATEGY_CN.get(strat, strat),
                    'slot_id': p.get('slot_id') or '',
                    'hold_days': hold_days,
                    'note': '',
                })

            # 卖出事件: closed_trades 中 sell_date=当日
            for t in closed:
                if t.get('sell_date') != date_str:
                    continue
                lock_ts = (t.get('selling_locked') or {}).get('ts') or ''
                # 优先提取 HH:MM:SS 秒级精度
                m = re.search(r'(\d{2}:\d{2}:\d{2})', lock_ts)
                if not m:
                    m = re.search(r'(\d{2}:\d{2})', lock_ts.split(' ')[-1])
                if m:
                    sell_time = m.group(1)
                    if len(sell_time) == 5:   # HH:MM → 补 :00
                        sell_time += ':00'
                elif t.get('sell_mode') == 'timed' and \
                        (t.get('sell_at') or {}).get('time'):
                    sell_time = t['sell_at']['time']
                    if len(sell_time) == 5:   # HH:MM → 补 :00
                        sell_time += ':00'
                else:
                    sell_time = '15:00:00'   # hour档兜底(无精确时间戳)
                # 09:25:xx 集合竞价期间 → 09:30:00 实际开盘成交时刻
                if sell_time.startswith('09:25'):
                    sell_time = '09:30:00'
                strat = t.get('strategy') or ''
                reason_cn = {'take_profit': '止盈', 'stop_loss': '止损',
                             'trailing_stop': '止盈回落', 'timed': '定时卖出',
                             'expired': '到期卖出'}.get(
                                 t.get('sell_reason'), t.get('sell_reason') or '')
                events.append({
                    'time': sell_time, 'type': 'sell',
                    'code': t.get('code'), 'name': t.get('name') or '',
                    'price': t.get('sell_price'),
                    'strategy_cn': STRATEGY_CN.get(strat, strat),
                    'pnl_pct': t.get('pnl_pct'),
                    'slot_id': t.get('slot_id') or '',
                    'note': reason_cn,
                })
        except Exception as e:
            print(f"[live_events] positions读取失败: {e}")

    # 影子信号(测试中, 不动真金): 文件不存在静默跳过
    shadow_file = ('/home/AIWealth/research/results/shadow_surge/'
                   f'shadow_signals_{ymd}.json')
    if os.path.exists(shadow_file):
        try:
            with open(shadow_file, 'r', encoding='utf-8') as f:
                shadow = json.load(f)
            for s in shadow.get('signals', []):
                raw_ts = s.get('confirm_ts') or s.get('buy_ts') or ''
                # 提取秒级时间: 支持 HH:MM:SS 或 HH:MM
                tm = re.search(r'(\d{2}:\d{2}:\d{2})', raw_ts)
                if not tm:
                    tm = re.search(r'(\d{2}:\d{2})', raw_ts)
                shadow_time = tm.group(1) if tm else '09:35:00'
                if len(shadow_time) == 5:
                    shadow_time += ':00'
                events.append({
                    'time': shadow_time,
                    'type': 'shadow',
                    'code': s.get('code'), 'name': s.get('name') or '',
                    'price': s.get('buy_px') or s.get('confirm_px'),
                    'strategy_cn': STRATEGY_CN['early_surge_chase_0940'],
                    'note': '冲板信号(测试中)',
                })
        except Exception as e:
            print(f"[live_events] shadow读取失败: {e}")

    events.sort(key=lambda e: e['time'])
    return jsonify({'date': date_str, 'events': events, 'count': len(events)})


# ===== Task#229 冲板测试信号专区聚合接口 (⚠测试信号, 非实盘, 零生产触碰) =====

SHADOW_DIR = '/home/AIWealth/research/results/shadow_surge'
SURGE_REPLAY_TRADES = ('/home/AIWealth/research/results/t228_surge_replay/'
                       'trades_engine_slot1.json')
SURGE_REPLAY_END = '2026-08-07'    # 回放准绳终点, 重叠窗口以回放为准
_ts_cache = {'ts': 0, 'payload': None}
_TS_CACHE_TTL = 10                 # 秒, 与前端10s统一刷新对齐

_SURGE_CLIMB_CN = {'0935': '开盘直线冲板', '0940': '早盘爬升冲板',
                   '0945': '震荡拉升冲板'}
_SURGE_STATE_CN = {'SIGNALED': '信号中', 'TOUCHED': '触板', 'SEALED': '封板中',
                   'BROKEN': '炸板', 'RE_SEALED': '回封', 'SEALED_EOD': '封板至收盘',
                   'FADED': '回落', 'NEVER_TOUCHED': '未触板'}


def _surge_pick_slot1(sig_list):
    """引擎slot=1口径当日唯一笔: 最早确认bar的非火箭winner(t228回放A口径)"""
    cands = [s for s in sig_list if s.get('winner') and not s.get('rocket')
             and s.get('vol_baseline') != 'skipped']
    if not cands:
        return None
    return min(cands, key=lambda s: s.get('confirm_bar') or '9999')


def _surge_live_days():
    """{date: signals} 全部live信号日(升序)"""
    out = {}
    for fp in sorted(glob.glob(os.path.join(SHADOW_DIR, 'shadow_signals_*.json'))):
        try:
            with open(fp, 'r', encoding='utf-8') as f:
                data = json.load(f)
        except (OSError, ValueError):
            continue
        meta = data.get('meta', {})
        if meta.get('mode') == 'live' and meta.get('date'):
            out[meta['date']] = data.get('signals', [])
    return out


def _reseal_section(today):
    """Task#232 回封首板测试信号子区 (第二测试池, 与冲板池同API不同数组):
    今日信号(reseal_signals_*.json live) + 回封池台账累计(reseal_exits.json)。
    数据源全为影子产物JSON, 零实盘触碰。Task#240: 信号/台账均附
    c08_eligible标记(C08升级格=V1∩单次回封reopen_count==1, 并行记账口径),
    ledger附c08子集独立统计。"""
    fp = os.path.join(SHADOW_DIR,
                      f"reseal_signals_{today.replace('-', '')}.json")
    status, today_out = 'no_file', []
    if os.path.exists(fp):
        try:
            with open(fp, 'r', encoding='utf-8') as f:
                data = json.load(f)
            meta = data.get('meta', {})
            if meta.get('mode') == 'live':
                status = meta.get('status', '?')
                for s in data.get('signals', []):
                    fs = s.get('first_seal_bar') or ''
                    rc = s.get('reopen_count', s.get('reopen_cnt'))
                    today_out.append({
                        'code': s['code'], 'name': s.get('name', ''),
                        'first_seal_bar': fs,
                        'first_seal_cn': f'{fs[:2]}:{fs[2:]}' if len(fs) == 4
                        else '—',
                        'first_seal_ts': s.get('first_seal_ts'),
                        'reopen_cnt': s.get('reopen_cnt'),
                        'reopen_count': rc,
                        'c08_eligible': bool(s.get('c08_eligible',
                                                   rc == 1)),
                        'reseal_ts': s.get('reseal_ts'),
                        'notify': bool(s.get('notify')),
                        'plan': s.get('plan'),
                    })
        except (OSError, ValueError):
            status = 'parse_error'
    ledger, recent = None, []
    try:
        with open(os.path.join(SHADOW_DIR, 'reseal_exits.json'), 'r',
                  encoding='utf-8') as f:
            state = json.load(f)
        rows = sorted(state.values(),
                      key=lambda r: (r.get('date', ''), r.get('code', '')))
        closed = [r for r in rows if r.get('status') == 'closed']
        rets = [r['ret_net_pct'] for r in closed]
        # Task#240 C08子集切片(同一信号池, 各指标独立计算)
        rows_c08 = [r for r in rows if r.get('c08_eligible')]
        rets_c08 = [r['ret_net_pct'] for r in rows_c08
                    if r.get('status') == 'closed']
        ledger = {
            'n_signals': len(rows), 'n_closed': len(closed),
            'n_open': sum(1 for r in rows if r.get('status') == 'open'),
            'n_pending': sum(1 for r in rows
                             if r.get('status') == 'pending'),
            'n_skipped': sum(1 for r in rows
                             if r.get('status') == 'skipped'),
            'win_rate_pct': round(sum(1 for r in rets if r > 0)
                                  / len(rets) * 100, 2) if rets else None,
            'avg_net_pct': round(sum(rets) / len(rets), 4) if rets else None,
            'cum_net_pct': round(sum(rets), 4) if rets else None,
            'c08': {
                'n_signals': len(rows_c08), 'n_closed': len(rets_c08),
                'win_rate_pct': round(sum(1 for r in rets_c08 if r > 0)
                                      / len(rets_c08) * 100, 2)
                if rets_c08 else None,
                'avg_net_pct': round(sum(rets_c08) / len(rets_c08), 4)
                if rets_c08 else None,
                'cum_net_pct': round(sum(rets_c08), 4)
                if rets_c08 else None,
            },
        }
        for r in rows[-8:]:            # 最近8笔(含持仓/跳过)供前端展示
            recent.append({
                'date': r.get('date'), 'code': r.get('code'),
                'name': r.get('name'), 'status': r.get('status'),
                'reason': r.get('reason'),
                'buy_date': r.get('buy_date'), 'buy_px': r.get('buy_px'),
                'exit_date': r.get('exit_date'),
                'exit_px': r.get('exit_px'),
                'exit_reason': r.get('exit_reason'),
                'ret_net_pct': r.get('ret_net_pct'),
                'need_review': bool(r.get('need_review')),
                'c08_eligible': bool(r.get('c08_eligible')),
            })
    except (OSError, ValueError):
        pass
    return {
        'strategy_cn': '回封首板(seal_reseal_v1)',
        'rule_cn': 'T3(10:05-11:30)首封∩开板≥1次重封∩首板∩收盘封死; '
                   'D+1开盘买 止盈+5%/止损-8% 第3日收盘兜底(模拟)',
        'status': status,
        'today_signals': today_out,
        'ledger': ledger,
        'recent_trades': recent,
        'baseline': '31日回放: 笔级n=89 净+1.08%/胜率68.5%; '
                    'slot=1月化+32.6%/胜率75%',
        'exam': {'criteria': '累计30笔 且 胜率≥60% 且 笔均>0.5% '
                             '→ 提G3引擎验证',
                 'criteria_c08': 'C08子集(单次回封)独立同标准: '
                                 '累计30笔 且 胜率≥60% 且 笔均>0.5%'},
        'disclaimer': '⚠️测试信号·不代表实盘买入·实盘资金无操作',
    }


@app.route('/api/test_signals')
def api_test_signals():
    """Task#229 冲板测试信号专区聚合 (10s短缓存):
    今日信号(含盘中实时状态) + 昨日D+1模拟卖出结果 + 累计测试战绩(准绳口径)。
    数据源全为影子产物JSON, 与实盘资金链零关联。"""
    now = time.time()
    if _ts_cache['payload'] is not None and now - _ts_cache['ts'] < _TS_CACHE_TTL:
        return jsonify(_ts_cache['payload'])
    today = datetime.now().strftime('%Y-%m-%d')
    days = _surge_live_days()
    exits = {}
    try:
        with open(os.path.join(SHADOW_DIR, 'shadow_exits.json'), 'r',
                  encoding='utf-8') as f:
            exits = json.load(f)
    except (OSError, ValueError):
        pass

    # ---- 今日信号 + 盘中实时状态(shadow_tracker tracking summary) ----
    track_map = {}
    tfp = os.path.join(SHADOW_DIR,
                       f"shadow_tracking_summary_{today.replace('-', '')}.json")
    if os.path.exists(tfp):
        try:
            with open(tfp, 'r', encoding='utf-8') as f:
                tsum = json.load(f)
            for tsig in tsum.get('signals', []):
                track_map[tsig['code']] = tsig
        except (OSError, ValueError):
            pass
    today_sigs = days.get(today, [])
    slot1 = _surge_pick_slot1(today_sigs)
    today_out = []
    for s in today_sigs:
        vr = s.get('vol_ratio')
        cts = (s.get('confirm_ts') or '')[:5]
        reason = (f"{cts}触板" + (f"+量比{vr:.1f}倍" if vr else '')
                  + '·' + _SURGE_CLIMB_CN.get(s.get('confirm_bar'), '冲板'))
        tk = track_map.get(s['code']) or {}
        st = tk.get('final_state') or tk.get('state')
        today_out.append({
            'code': s['code'], 'name': s.get('name', ''),
            'confirm_ts': s.get('confirm_ts'), 'confirm_px': s.get('confirm_px'),
            'buy_px': s.get('buy_px'), 'vol_ratio': vr,
            'reason': reason,
            'slot1': bool(slot1 and s['code'] == slot1['code']),
            'rocket': bool(s.get('rocket')),
            'state': st, 'state_cn': _SURGE_STATE_CN.get(st, st or '—'),
            'float_ret_pct': tk.get('float_ret_pct'),
        })

    # ---- 昨日信号的D+1模拟卖出结果(slot=1笔) ----
    prev_dates = [d for d in sorted(days) if d < today]
    prev_out = None
    if prev_dates:
        pd = prev_dates[-1]
        w = _surge_pick_slot1(days[pd])
        if w:
            ex = exits.get(f"{pd}_{w['code']}")
            prev_out = {
                'date': pd, 'code': w['code'], 'name': w.get('name', ''),
                'buy_px': w.get('buy_px'),
                'exit_date': ex.get('exit_date') if ex else None,
                'exit_px': ex.get('exit_px') if ex else None,
                'ret_pct': ex.get('ret_pct') if ex else None,
                'need_review': bool(ex and ex.get('need_review')),
                'status': 'closed' if ex else 'pending',
            }
        else:
            prev_out = {'date': pd, 'code': None, 'status': 'no_signal'}

    # ---- 累计测试战绩(准绳口径: t228回放30笔 + REPLAY_END后live slot=1) ----
    rets = []
    n_replay = 0
    try:
        with open(SURGE_REPLAY_TRADES, 'r', encoding='utf-8') as f:
            for t in json.load(f):
                if t.get('status') == 'closed':
                    rets.append(t['profit_pct'])
                    n_replay += 1
    except (OSError, ValueError):
        pass
    n_live = 0
    for d in sorted(days):
        if d <= SURGE_REPLAY_END:
            continue                       # 重叠窗口以回放为准
        w = _surge_pick_slot1(days[d])
        ex = exits.get(f"{d}_{w['code']}") if w else None
        if ex:
            rets.append(ex['ret_pct'])
            n_live += 1
    cum = None
    if rets:
        n = len(rets)
        cum = {'n': n,
               'win_rate_pct': round(sum(1 for r in rets if r > 0) / n * 100, 2),
               'avg_ret_pct': round(sum(rets) / n, 4),
               'cum_ret_pct': round(sum(rets), 4),
               'n_replay': n_replay, 'n_live': n_live,
               'basis': f't228回放准绳{n_replay}笔(2026-06-26~{SURGE_REPLAY_END})'
                        f'+之后live slot=1已了结{n_live}笔'}

    payload = {
        'date': today,
        'disclaimer': '⚠️测试信号·不代表实盘买入·实盘资金无操作',
        'strategy_cn': STRATEGY_CN.get('early_surge_chase_0940', '早盘冲板追击'),
        'today_signals': today_out,
        'prev_result': prev_out,
        'cumulative': cum,
        'exam': {'window': '20交易日', 'criteria': '胜率≥48% 且 笔均>0'},
        'reseal': _reseal_section(today),   # Task#232 回封首板第二测试池
    }
    _ts_cache['payload'] = payload
    _ts_cache['ts'] = now
    return jsonify(payload)


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

            # [2026-08-03 equityfix] 实盘段改用 drawdown_state.nav_history 盯市快照
            # (原realized-by-sell-date口径与/api/live/equity同病: 浮盈不进历史点)。
            # 连续性拼接: 实盘点NAV = 回测终点NAV + (快照盯市NAV - 实盘初始资金)。
            live_initial_capital = account.get('initial_capital', initial_capital)
            dd_file = os.path.join(DATA_DIR, 'drawdown_state.json')
            if os.path.exists(dd_file):
                live_start_nav = backtest_end_nav
                with open(dd_file, 'r', encoding='utf-8') as f:
                    dd_state = json.load(f)
                for d, snap_nav in sorted(dd_state.get('nav_history', [])):
                    # 跳过回测已覆盖的日期
                    if backtest_end_date and d <= backtest_end_date:
                        continue
                    live_nav = live_start_nav + (float(snap_nav) - live_initial_capital)
                    return_pct = round((live_nav / initial_capital - 1) * 100, 2)
                    nav_history.append({
                        'date': d,
                        'nav': round(live_nav, 2),
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


# ===== 指数行情栏 (A股5指数+韩日+大A涨跌家数) =====
# 三个上游各自独立try, 单源失败只缺对应块(前端降级显示--), 不碰交易链路
_index_cache = {'ts': 0, 'payload': None}
_INDEX_CACHE_TTL = 10          # 秒 (Task#209: 30→10, 配合前端10s统一刷新)
_UPSTREAM_TIMEOUT = 1.5        # 秒/源


def _http_get(url, timeout=_UPSTREAM_TIMEOUT, encoding='utf-8'):
    req = urllib.request.Request(url)
    req.add_header('User-Agent', 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36')
    resp = urllib.request.urlopen(req, timeout=timeout)
    return resp.read().decode(encoding)


@app.route('/api/index_quotes')
def api_index_quotes():
    """指数行情: 上证/深成/创业/科创50/北证50(腾讯) + 日经225/KOSPI(东财push2) + 涨跌家数(东财ZDFenBu); 30s进程内缓存"""
    now = time.time()
    if _index_cache['payload'] and now - _index_cache['ts'] < _INDEX_CACHE_TTL:
        return jsonify(_index_cache['payload'])

    indices = []

    # 源1: 腾讯A股指数批量 (与/api/realtime_quotes同族, parts[3]现价/parts[4]昨收/parts[32]涨跌幅)
    # Task#184: parts[37]=成交额(万元), sh000001=沪市总额/sz399001=深市总额, 两者和=两市成交额(不含北交所)
    A_CODES = [('sh000001', '上证指数'), ('sz399001', '深证成指'),
               ('sz399006', '创业板指'), ('sh000688', '科创50'), ('bj899050', '北证50')]
    rt_amount_yi = {}    # {code: 成交额亿元}
    try:
        content = _http_get('http://qt.gtimg.cn/q=' + ','.join(c for c, _ in A_CODES), encoding='gbk')
        got = {}
        for line in content.strip().split('\n'):
            m = re.match(r'v_(\w+)="(.*)"', line.strip())
            if not m or not m.group(2):
                continue
            parts = m.group(2).split('~')
            if len(parts) < 35:
                continue
            try:
                got[m.group(1)] = {'price': float(parts[3]), 'change_pct': float(parts[32])}
            except (ValueError, IndexError):
                continue
            try:
                amt = float(parts[37])   # 万元
                if amt > 0:
                    rt_amount_yi[m.group(1)] = amt / 1e4
            except (ValueError, IndexError):
                pass
        for code, cname in A_CODES:
            q = got.get(code)
            indices.append({'code': code, 'name': cname, 'region': 'cn',
                            'price': q['price'] if q else None,
                            'change_pct': q['change_pct'] if q else None})
    except Exception:
        indices.extend({'code': c, 'name': n, 'region': 'cn', 'price': None, 'change_pct': None}
                       for c, n in A_CODES)

    # 源2: 东财push2delay国际指数 (f2/f3整型÷100; f18昨收, 自洽校验f18+f4=f2已实测)
    # 历史备注: 2026-07-31曾因跨源矛盾(TradingView/Yahoo显示下跌)降级昨收口径, 后用户确认反弹属实回切; 备选源(新浪/网易/百度/腾讯两轮穷举/Yahoo)均不可达, 前端标注"东财源"不背书精度
    # 回切开关: 用户已确认韩日行情属实(其终端为准, 2026-07-31裁决)→True实时口径(f2/f3), 前端±8%改⚡异动角标明示不拦截
    INTL_REALTIME = True
    INTL = [('100.KS11', '韩国KOSPI', 'kr'), ('100.N225', '日经225', 'jp')]
    intl_ok = False
    for _attempt in range(2):
        try:
            content = _http_get('http://push2delay.eastmoney.com/api/qt/ulist.np/get?secids='
                                + ','.join(s for s, _, _ in INTL) + '&fields=f2,f3,f12,f18,f124')
            diff = (json.loads(content).get('data') or {}).get('diff') or []
            by_code = {d.get('f12'): d for d in diff}
            if not by_code:
                continue
            for secid, cname, region in INTL:
                d = by_code.get(secid.split('.')[1])
                if INTL_REALTIME:
                    ok = d and isinstance(d.get('f2'), int) and isinstance(d.get('f3'), int)
                    indices.append({'code': secid, 'name': cname, 'region': region,
                                    'price': round(d['f2'] / 100.0, 2) if ok else None,
                                    'change_pct': round(d['f3'] / 100.0, 2) if ok else None,
                                    'quote_ts': d.get('f124') if d else None})
                else:
                    ok = d and isinstance(d.get('f18'), int) and d.get('f18') > 0
                    indices.append({'code': secid, 'name': cname, 'region': region,
                                    'price': round(d['f18'] / 100.0, 2) if ok else None,
                                    'change_pct': None,
                                    'caliber': 'preclose' if ok else None,
                                    'quote_ts': d.get('f124') if d else None})
            intl_ok = True
            break
        except Exception:
            continue
    if not intl_ok:
        indices.extend({'code': s, 'name': n, 'region': r, 'price': None, 'change_pct': None}
                       for s, n, r in INTL)

    # 源3: 大A涨跌家数 (东财涨跌分布: 正桶和=涨, 负桶和=跌, 0桶=平)
    breadth = None
    try:
        content = _http_get('http://push2ex.eastmoney.com/getTopicZDFenBu'
                            '?ut=7eea3edcaed734bea9cbfc24409ed989&dpt=wz.ztzt')
        fenbu = (json.loads(content).get('data') or {}).get('fenbu') or []
        up = down = flat = 0
        for bucket in fenbu:
            for k, v in bucket.items():
                ki = int(k)
                if ki > 0:
                    up += v
                elif ki < 0:
                    down += v
                else:
                    flat += v
        if up + down + flat > 0:
            breadth = {'up': up, 'down': down, 'flat': flat}
    except Exception:
        pass

    # 源4(Task#184/#212): 大A两市成交额+环比. 盘中=腾讯实时累计vs昨日同期(新浪指数分时), 盘后=全天vs全天(stocks.db日K)
    turnover = _build_turnover(rt_amount_yi)

    payload = {'indices': indices, 'breadth': breadth, 'turnover': turnover,
               'updated_at': datetime.now().strftime('%Y-%m-%d %H:%M:%S')}
    _index_cache['payload'] = payload
    _index_cache['ts'] = now
    return jsonify(payload)


# ===== 大A两市成交额 (Task#184; Task#212盘中环比改"vs昨日同期") =====
# 昨日基准=stocks.db stock_kline当日全A(沪+深)amount合计, 单位元(已实测: 2026-08-05合计26179亿≈公开口径2.6万亿)
_turnover_db_cache = {}      # {date_str: total_yi} 日K合计按日缓存, 每日至多查库一次
# Task#212: 昨日两市5min累计成交额序列(新浪指数分时, 每日拉一次进程内缓存; 腾讯ifzq mkline指数无成交额字段已实测排除)
# sh000001=沪市全额, sz399106深证综指=深市全额(sz399001成分股口径实测偏小38%不可用); amount单位元, 昨日全天合计与stocks.db实测偏差0.2%
_prev_minute_cache = {'date': None, 'cum': None, 'total_yi': None, 'fail_ts': 0}
_SINA_IDX_5MIN = ('https://quotes.sina.cn/cn/api/json_v2.php/CN_MarketDataService.getKLineData'
                  '?symbol=%s&scale=5&ma=no&datalen=110')


def _fetch_prev_minute_cum(prev_date):
    """昨日两市(沪sh000001+深综sz399106)5分钟累计成交额序列: [(bar_end'HH:MM', cum_yi)]共48根; 失败缓存10分钟不重试"""
    c = _prev_minute_cache
    if c['date'] == prev_date and c['cum']:
        return c['cum']
    if time.time() - c['fail_ts'] < 600:
        return None
    try:
        per_bar = {}
        for sym in ('sh000001', 'sz399106'):
            bars = json.loads(_http_get(_SINA_IDX_5MIN % sym))
            n = 0
            for b in bars:
                if b['day'][:10] == prev_date:
                    hhmm = b['day'][11:16]
                    per_bar[hhmm] = per_bar.get(hhmm, 0.0) + float(b['amount']) / 1e8
                    n += 1
            if n != 48:                  # 昨日序列不完整(源异常/非交易日)→整体放弃, 降级全天口径
                raise ValueError('%s bars=%d' % (sym, n))
        cum, acc = [], 0.0
        for hhmm in sorted(per_bar):
            acc += per_bar[hhmm]
            cum.append((hhmm, acc))
        c.update(date=prev_date, cum=cum, total_yi=acc, fail_ts=0)
        return cum
    except Exception:
        c['fail_ts'] = time.time()
        return None


def _prev_sametime_yi(prev_date, now_dt):
    """昨日截至"当前时刻同期"的两市累计成交额(亿). 午休钉在11:30值; 当前5min bar内线性插值; 不可用返回None"""
    cum = _fetch_prev_minute_cum(prev_date)
    if not cum:
        return None
    hm = now_dt.hour * 60 + now_dt.minute + now_dt.second / 60.0
    if hm <= 9 * 60 + 30:
        return None
    elapsed = min(hm, 11 * 60 + 30) - (9 * 60 + 30)      # 上午段已走分钟数
    if hm >= 13 * 60:
        elapsed += min(hm, 15 * 60) - 13 * 60            # 下午段(11:30-13:00午休不计, 自然钉在11:30值)
    n_full = int(elapsed // 5)                           # 已走完的5min bar数
    frac = (elapsed - n_full * 5) / 5.0
    if n_full >= len(cum):
        return round(cum[-1][1], 1)
    base = cum[n_full - 1][1] if n_full > 0 else 0.0
    return round(base + (cum[n_full][1] - base) * frac, 1)


def _db_turnover_yi(where_sql, args=()):
    """stock_kline成交额合计(亿元), 返回(date, total_yi)或None"""
    conn = sqlite3.connect('/home/AIWealth/data/stocks.db')
    try:
        row = conn.execute(
            "SELECT date, SUM(amount)/1e8 FROM stock_kline WHERE date=("
            "SELECT MAX(date) FROM stock_kline WHERE " + where_sql + ")", args).fetchone()
        if row and row[0] and row[1]:
            return row[0], round(row[1], 1)
        return None
    finally:
        conn.close()


def _build_turnover(rt_amount_yi):
    """两市成交额today/prev/环比. basis口径: intraday_pace(盘中vs昨日同期)/full_day(全天vs全天)/fallback_daily(盘中降级vs昨日全天)"""
    now_dt = datetime.now()
    today_str = now_dt.strftime('%Y-%m-%d')
    try:
        if 'sh000001' in rt_amount_yi and 'sz399001' in rt_amount_yi:
            today_yi = round(rt_amount_yi['sh000001'] + rt_amount_yi['sz399001'], 1)
            today_date, source = today_str, 'tencent_rt'
        else:
            r = _db_turnover_yi("date<=?", (today_str,))
            if not r:
                return None
            today_date, today_yi = r
            source = 'db_daily'
        prev_date = prev_yi = None
        for d, v in _turnover_db_cache.items():
            if d < today_date and (prev_date is None or d > prev_date):
                prev_date, prev_yi = d, v
        if prev_date is None:
            r = _db_turnover_yi("date<?", (today_date,))
            if r:
                prev_date, prev_yi = r
                _turnover_db_cache[prev_date] = prev_yi
        # Task#212: 盘中(交易日9:35~15:00)环比改vs昨日同期; 任一环节失败降级现行vs昨日全天(fallback_daily)
        # 周六日腾讯qt返回上个交易日收盘快照, 非盘中累计→weekday守卫排除(节假日靠bars=48校验无法覆盖, 风险已评估接受)
        basis = 'full_day'
        if (source == 'tencent_rt' and now_dt.weekday() < 5
                and '09:35' <= now_dt.strftime('%H:%M') < '15:00'):
            basis = 'fallback_daily'
            if prev_date:
                st = _prev_sametime_yi(prev_date, now_dt)
                # 分时序列全天合计须与日K基准吻合(±10%), 防单位/口径漂移后才采纳同期基准
                if (st and prev_yi and _prev_minute_cache['total_yi']
                        and abs(_prev_minute_cache['total_yi'] - prev_yi) / prev_yi <= 0.10):
                    prev_yi, basis = st, 'intraday_pace'
        ratio_pct = round((today_yi - prev_yi) / prev_yi * 100, 1) if prev_yi else None
        return {'today_yi': today_yi, 'today_date': today_date, 'source': source, 'basis': basis,
                'prev_yi': prev_yi, 'prev_date': prev_date, 'ratio_pct': ratio_pct}
    except Exception:
        return None


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
                strat = t.get('strategy_name', '')
                all_trades.append({
                    'code': code,
                    'name': name,
                    'strategy': strat,
                    'strategy_cn': STRATEGY_CN.get(strat, strat),
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
                strat = t.get('strategy', '')
                all_trades.append({
                    'code': t.get('code', ''),
                    'name': t.get('name', ''),
                    'strategy': strat,
                    'strategy_cn': STRATEGY_CN.get(strat, strat),
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


@app.route('/api/backtest/summary')
def api_backtest_summary():
    """回测数据摘要（CAGR/MDD/胜率/交易数/年度收益）"""
    backtest_file = '/home/AIWealth/frontend/data/combined_5slot_new_trades.json'
    if not os.path.exists(backtest_file):
        return jsonify({'error': '暂无回测数据'})
    try:
        with open(backtest_file, 'r', encoding='utf-8') as f:
            bt_data = json.load(f)
        summary = bt_data.get('combined_summary', {})
        return jsonify({
            'start_date': summary.get('start_date', ''),
            'end_date': summary.get('end_date', ''),
            'initial_capital': summary.get('initial_capital', 1000000),
            'final_nav': summary.get('final_nav', 0),
            'total_return_pct': summary.get('total_return_pct', 0),
            'cagr_pct': summary.get('cagr_pct', 0),
            'max_drawdown_pct': summary.get('max_drawdown_pct', 0),
            'n_trades': summary.get('n_trades', 0),
            'win_rate_pct': summary.get('win_rate_pct', 0),
            'avg_profit_pct': summary.get('avg_profit_pct', 0),
            'avg_hold_hours': summary.get('avg_hold_hours', 0),
            'per_strategy': summary.get('per_strategy', {}),
            'yearly_returns': summary.get('yearly_returns', {}),
        })
    except Exception as e:
        return jsonify({'error': f'回测数据加载失败: {e}'})


@app.route('/api/trades/history')
def api_trades_history():
    """历史交易记录（回测+实盘），支持分页和筛选"""
    page = request.args.get('page', 1, type=int)
    page_size = request.args.get('page_size', 10, type=int)
    strategy = request.args.get('strategy', '')  # 策略筛选
    source = request.args.get('source', '')  # 来源筛选: live / backtest

    all_trades = _load_all_trades()

    # 来源筛选
    if source:
        filtered = [t for t in all_trades if t.get('source') == source]
    else:
        filtered = all_trades

    # 策略筛选
    if strategy:
        filtered = [t for t in filtered if t['strategy'] == strategy or t['strategy_tag'] == strategy]

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
    response.headers['Access-Control-Allow-Methods'] = 'GET, POST, OPTIONS'
    response.headers['Access-Control-Allow-Headers'] = 'Content-Type'
    return response


if __name__ == '__main__':
    print(f'[{datetime.now()}] AIWealth Web Server starting on http://0.0.0.0:80')
    print(f'  Frontend: {FRONTEND_DIR}')
    print(f'  Data: {DATA_DIR}')
    print(f'  APIs: /api/candidates/latest, /api/decision/latest, /api/positions, /api/strategies')
    app.run(host='0.0.0.0', port=80, debug=False)
