#!/usr/bin/env python3
"""
企业微信应用消息通知模块
============================
配置项（需用户填入）：
  CORP_ID = '企业ID'
  CORP_SECRET = '应用Secret'
  AGENT_ID = 应用AgentID
"""
import json
import os
import urllib.request
import urllib.parse
import re
import time
from datetime import datetime

# ========== 配置区 ==========
# 通道一: 企业微信应用消息(需CORP_ID/CORP_SECRET/AGENT_ID, 三项全填才生效)
CORP_ID = ''        # 企业微信企业ID
CORP_SECRET = ''    # 应用Secret
AGENT_ID = 0        # 应用AgentID
# 通道二: 企业微信群机器人Webhook(只需一个key, 配置环境变量QYWX_WEBHOOK_KEY)
#   群机器人添加方法: 企业微信群 → 右上角... → 群机器人 → 添加机器人 → 复制Webhook地址
#   地址格式: https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=xxx-xxx-xxx
#   只需将key填入环境变量: export QYWX_WEBHOOK_KEY='xxx-xxx-xxx'
QYWX_ENV_FILE = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                             '.qywx_env.sh')


def load_qywx_webhook_key() -> str:
    """获取群机器人Webhook KEY: 优先环境变量QYWX_WEBHOOK_KEY。

    Task#123修复: cron等非交互环境不加载.bashrc, 环境变量为空时回退解析
    .qywx_env.sh 中的 export QYWX_WEBHOOK_KEY='...' 行(与
    test_notify_format.py 的兜底逻辑一致, 提成模块级函数),
    使morning_decision/position_tracker等所有调用方在cron下也能发通知。
    """
    key = os.environ.get('QYWX_WEBHOOK_KEY', '')
    if key:
        return key
    try:
        with open(QYWX_ENV_FILE) as f:
            for line in f:
                line = line.strip()
                if line.startswith('export QYWX_WEBHOOK_KEY='):
                    return line.split('=', 1)[1].strip("'\"")
    except OSError:
        pass
    return ''


QYWX_WEBHOOK_KEY = load_qywx_webhook_key()
# ============================

POSITIONS_FILE = '/home/AIWealth/data/realtime/positions.json'

# [Task#256] 通知链G1自检: 发送失败时写scheduler_alerts.log(人工巡检入口)
# + 失败重试1次; NOTIFY_FAIL条目不属P0推送范围(防递归循环)
SCHEDULER_ALERT_LOG_PATH = '/home/AIWealth/logs/realtime/scheduler_alerts.log'
NOTIFY_RETRY_WAIT_SEC = 3   # 失败后重试前等待秒数


def _log_notify_failure(channel: str, detail: str, content: str):
    """[Task#256 G1] 通知发送最终失败(含重试)时落档scheduler_alerts.log。

    只记录真实尝试失败(未配置通道的跳过不记); 写入失败静默降级,
    绝不反向影响通知调用方主流程。"""
    try:
        head = content.replace('\n', ' ')[:60]
        with open(SCHEDULER_ALERT_LOG_PATH, 'a', encoding='utf-8') as f:
            f.write(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] "
                    f"[NOTIFY_FAIL] 通道={channel} 重试1次仍失败: {detail} "
                    f"| 消息头: {head}\n")
    except OSError:
        pass


# P0级告警特征(命中任一即实时推企微): 与全库既有告警文案约定一致
P0_ALERT_MARKERS = ('🚨', '[ERROR]', '⛔', '瘫痪')


def push_alert(msg: str) -> bool:
    """[Task#256 G2] scheduler_alerts的P0/ERROR级条目实时企微推送。

    仅当msg命中P0特征时真发(中性运维文案); 非P0条目静默返回False。
    自身失败只打印不再写scheduler_alerts(防递归); 供各告警写入点
    一行接入: push_alert(msg)。"""
    if not any(m in msg for m in P0_ALERT_MARKERS):
        return False
    try:
        text = ('【运维告警·P0】\n'
                f'{msg[:900]}\n'
                f"时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n"
                '(scheduler_alerts.log同步落档, 请及时处置)')
        return send_qywx(text)
    except Exception as e:
        print(f'[push_alert] P0推送异常(已有文件落档兜底): {e}')
        return False


_token_cache = {'token': '', 'expire': 0}


def get_access_token():
    """获取access_token（带缓存，提前5分钟过期）"""
    now = time.time()
    if _token_cache['token'] and now < _token_cache['expire']:
        return _token_cache['token']

    url = (f'https://qyapi.weixin.qq.com/cgi-bin/gettoken'
           f'?corpid={CORP_ID}&corpsecret={CORP_SECRET}')
    try:
        resp = urllib.request.urlopen(url, timeout=10)
        data = json.loads(resp.read())
        if data.get('errcode') == 0:
            _token_cache['token'] = data['access_token']
            _token_cache['expire'] = now + data.get('expires_in', 7200) - 300
            return _token_cache['token']
        else:
            print(f"[notify] 获取token失败: {data}")
            return None
    except Exception as e:
        print(f"[notify] 获取token异常: {e}")
        return None


def send_text(content: str) -> bool:
    """发送文本消息到企业微信应用。

    [Task#256 G1] 失败重试1次; 最终失败写scheduler_alerts([NOTIFY_FAIL])。
    未配置通道仍直接跳过不计失败(行为与旧版一致)。"""
    if not CORP_ID or not CORP_SECRET:
        print("[notify] 未配置企业微信，跳过通知")
        return False

    last_detail = ''
    for attempt in (1, 2):
        token = get_access_token()
        if not token:
            last_detail = '获取access_token失败'
        else:
            url = (f'https://qyapi.weixin.qq.com/cgi-bin/message/send'
                   f'?access_token={token}')
            payload = json.dumps({
                "touser": "@all",
                "msgtype": "text",
                "agentid": AGENT_ID,
                "text": {"content": content}
            }).encode('utf-8')
            try:
                req = urllib.request.Request(url, data=payload,
                                             headers={'Content-Type': 'application/json'})
                resp = urllib.request.urlopen(req, timeout=10)
                data = json.loads(resp.read())
                if data.get('errcode') == 0:
                    print(f"[notify] 消息发送成功")
                    return True
                last_detail = f'errcode非0: {data}'
                print(f"[notify] 发送失败: {data}")
            except Exception as e:
                last_detail = f'异常: {e}'
                print(f"[notify] 发送异常: {e}")
        if attempt == 1:
            print(f"[notify] {NOTIFY_RETRY_WAIT_SEC}秒后重试1次...")
            time.sleep(NOTIFY_RETRY_WAIT_SEC)
    _log_notify_failure('app_message', last_detail, content)
    return False


# =============================================================================
# 通道二: 企业微信群机器人Webhook
# =============================================================================

def send_qywx(content: str, msgtype: str = 'text') -> bool:
    """通过企业微信群机器人Webhook发送消息。

    需配置环境变量 QYWX_WEBHOOK_KEY (群机器人Webhook地址的key部分)。
    未配置时打印日志并返回False, 不影响其他通知通道。
    msgtype: 'text' 或 'markdown'。
    [Task#256 G1] 失败重试1次; 最终失败写scheduler_alerts([NOTIFY_FAIL])。
    """
    if not QYWX_WEBHOOK_KEY:
        print("[notify_qywx] 未配置QYWX_WEBHOOK_KEY, 跳过群机器人通知")
        return False
    url = (f'https://qyapi.weixin.qq.com/cgi-bin/webhook/send'
           f'?key={QYWX_WEBHOOK_KEY}')
    payload = json.dumps({
        "msgtype": msgtype,
        msgtype: {"content": content}
    }).encode('utf-8')
    last_detail = ''
    for attempt in (1, 2):
        try:
            req = urllib.request.Request(url, data=payload,
                                         headers={'Content-Type': 'application/json'})
            resp = urllib.request.urlopen(req, timeout=10)
            data = json.loads(resp.read())
            if data.get('errcode') == 0:
                print(f"[notify_qywx] 群机器人消息发送成功")
                return True
            last_detail = f'errcode非0: {data}'
            print(f"[notify_qywx] 发送失败: {data}")
        except Exception as e:
            last_detail = f'异常: {e}'
            print(f"[notify_qywx] 发送异常: {e}")
        if attempt == 1:
            print(f"[notify_qywx] {NOTIFY_RETRY_WAIT_SEC}秒后重试1次...")
            time.sleep(NOTIFY_RETRY_WAIT_SEC)
    _log_notify_failure('qywx_webhook', last_detail, content)
    return False


DISCLAIMER = ('⚠️ 免责声明：以上信息由量化系统自动生成，'
              '仅供学习研究参考，不构成任何投资建议。据此操作风险自担。')
SEPARATOR = '━━━━━━━━━━━━━'

# 固定仓位比例
FIXED_POSITION_PCT = 20

# [Task#209] 策略中文正名(与server.py的STRATEGY_CN同源镜像, 用户可见处统一口径)
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


def _board_label(height) -> str:
    """[Task#209] 连板高度→展示标签: 1=首板/2=2板/>=3=3板+; 0或缺失返回空串"""
    try:
        h = int(height or 0)
    except (TypeError, ValueError):
        h = 0
    if h <= 0:
        return ''
    if h == 1:
        return '首板'
    if h == 2:
        return '2板'
    return '3板+'


def _relative_day(date_str: str) -> str:
    """[Task#209] 日期→相对说法: 今天/明天/M月D日(跨周末时)"""
    if not date_str:
        return ''
    try:
        d = datetime.strptime(date_str, '%Y-%m-%d')
    except ValueError:
        return date_str
    delta = (d.date() - datetime.now().date()).days
    if delta == 0:
        return '今天'
    if delta == 1:
        return '明天'
    return f'{d.month}/{d.day}'


def sell_rule_plain(pos: dict) -> str:
    """[Task#209] 持仓出场规则大白话(纯文案渲染, 触发语义零改动)。

    trailing: "最高涨到X元，回落到Y元就卖（涨了跟着抬）"
              (触发价=peak - buy_price×trailing_pp/100, 与引擎公式一致)
    fixed:    "涨到X元卖出 / 跌到Y元止损"
    timed:    "明天10:30卖出"
    到期:      "最晚M/D收盘前卖出"
    """
    parts = []
    mode = pos.get('sell_mode') or ''
    buy_price = pos.get('buy_price') or 0
    tp = pos.get('tp_price')
    sl = pos.get('sl_price')
    if mode == 'trailing':
        peak = pos.get('peak_price') or buy_price
        pp = pos.get('trailing_pp') or 0
        trigger = peak - buy_price * pp / 100.0
        parts.append(f'最高涨到{peak:.2f}元，回落到{trigger:.2f}元就卖（涨了跟着抬）')
        if sl:
            parts.append(f'跌到{sl:.2f}元止损')
    elif mode == 'timed':
        sa = pos.get('sell_at') or {}
        day = _relative_day(sa.get('date', ''))
        hm = (sa.get('time', '') or '')[:5]
        parts.append(f'{day}{hm}卖出')
    else:  # fixed
        seg = []
        if tp:
            seg.append(f'涨到{tp:.2f}元卖出')
        if sl:
            seg.append(f'跌到{sl:.2f}元止损')
        if seg:
            parts.append(' / '.join(seg))
    exp = pos.get('expire_date') or ''
    if exp and len(exp) == 10:
        parts.append(f'最晚{int(exp[5:7])}/{int(exp[8:10])}收盘前卖出')
    return '；'.join(parts)


# =============================================================================
# 持仓信息获取
# =============================================================================

def _code_to_sina(code: str) -> str:
    """sh.600000 → sh600000"""
    return code.replace('.', '')


def _sina_to_code(sina_code: str) -> str:
    """sh600000 → sh.600000"""
    return sina_code[:2] + '.' + sina_code[2:]


def _fetch_realtime_prices(codes: list) -> dict:
    """通过腾讯实时行情获取当前价, 返回 {code: price}。
    失败或无数据返回空dict。
    """
    if not codes:
        return {}
    tencent_codes = [_code_to_sina(c) for c in codes]
    results = {}
    batch_size = 50
    for i in range(0, len(tencent_codes), batch_size):
        batch = tencent_codes[i:i + batch_size]
        url = f"http://qt.gtimg.cn/q={','.join(batch)}"
        try:
            req = urllib.request.Request(url)
            req.add_header('User-Agent',
                           'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36')
            resp = urllib.request.urlopen(req, timeout=10)
            content = resp.read().decode('gbk')
            for line in content.strip().split('\n'):
                line = line.strip()
                if not line:
                    continue
                m = re.match(r'v_(\w+)="(.*)"', line)
                if not m:
                    continue
                tencent_sym = m.group(1)
                data_str = m.group(2)
                if not data_str:
                    continue
                parts = data_str.split('~')
                if len(parts) < 4:
                    continue
                code = _sina_to_code(tencent_sym)
                try:
                    current = float(parts[3]) if parts[3] else 0
                except (ValueError, IndexError):
                    current = 0
                if current > 0:
                    results[code] = current
        except Exception as e:
            print(f"[notify] 腾讯行情请求失败(非致命): {e}")
    return results


def get_portfolio_summary(exclude_codes=None) -> str:
    """读取当前持仓信息, 返回格式化的markdown字符串。

    从 positions.json 读取持仓数据, 尝试获取实时行情计算浮盈。
    如果无法获取实时数据, 用买入价作为现价(fallback)。
    exclude_codes: 卖出通知专用(Task#185)——本笔及同批已卖标的代码集合。
        落账save_positions在通知之后, 磁盘快照此刻仍含已卖标的,
        持仓栏展示时显式剔除; 剔除后若无剩余持仓, 如实显示（无持仓）,
        不回落模拟数据。累计总收益的NAV计算仍按磁盘快照全量持仓
        (被剔除标的按实时价计值≈卖出所得, 与修复前口径一致)。
    返回格式:
    【当前持仓】
    1. 金龙羽（002882） 买入¥15.00 现价¥16.50 +10.00%
    ...
    【累计总收益】+12.35%
    """
    exclude = set(exclude_codes or [])
    # 模拟数据(测试/fallback用)
    mock_holdings = [
        {'code': 'sz.002882', 'name': '金龙羽', 'buy_price': 15.00, 'current_price': 16.50},
        {'code': 'sh.688318', 'name': '财富趋势', 'buy_price': 76.24, 'current_price': 78.00},
    ]
    mock_total_pnl_pct = 12.35

    try:
        if not os.path.exists(POSITIONS_FILE):
            # 文件不存在, 用模拟数据
            return _format_portfolio_md(mock_holdings, mock_total_pnl_pct)

        with open(POSITIONS_FILE, 'r', encoding='utf-8') as f:
            data = json.load(f)

        account = data.get('account', {})
        positions = data.get('positions', [])
        closed_trades = data.get('closed_trades', [])

        holdings = [p for p in positions if p.get('status') == 'holding']
        if not holdings and not closed_trades:
            # 无持仓无历史, 用模拟数据
            return _format_portfolio_md(mock_holdings, mock_total_pnl_pct)

        # 尝试获取实时行情
        codes = [p['code'] for p in holdings]
        realtime_prices = _fetch_realtime_prices(codes)

        # 构建持仓列表(Task#185: 剔除本笔及同批已卖标的, 展示卖出后状态)
        holding_list = []
        for p in holdings:
            if p.get('code') in exclude:
                continue
            buy_price = p.get('buy_price', 0)
            # 实时价获取不到时, 用买入价作为fallback(不算浮盈)
            current_price = realtime_prices.get(p['code'], buy_price)
            if current_price <= 0:
                current_price = buy_price
            holding_list.append({
                'code': p.get('code', ''),
                'name': p.get('name', ''),
                'buy_price': buy_price,
                'current_price': current_price,
                # [Task#209] 出场规则大白话(纯展示, 不碰触发逻辑)
                'sell_rule': sell_rule_plain(p),
            })

        # 如果没有持仓, 用模拟数据(有剔除时不回落mock: 卖完即如实展示空持仓)
        if not holding_list and not exclude:
            return _format_portfolio_md(mock_holdings, mock_total_pnl_pct)

        # 累计总收益率 = (total_nav - initial_capital) / initial_capital * 100
        initial_capital = account.get('initial_capital', 1000000)
        total_nav = account.get('total_nav', initial_capital)
        realized_pnl = account.get('realized_pnl', 0)

        # 如果有实时行情, total_nav需重算(现金+持仓市值)
        if realtime_prices:
            holding_value = sum(
                realtime_prices.get(p['code'], p.get('buy_price', 0)) *
                (p.get('buy_amount', 0) / p.get('buy_price', 1))
                for p in holdings
                if p.get('buy_price', 0) > 0
            )
            cash = account.get('cash', 0)
            total_nav = cash + holding_value

        total_pnl_pct = ((total_nav - initial_capital) / initial_capital * 100
                         if initial_capital > 0 else 0.0)

        return _format_portfolio_md(holding_list, total_pnl_pct)

    except Exception as e:
        print(f"[notify] 获取持仓信息失败({e}), 使用模拟数据")
        return _format_portfolio_md(mock_holdings, mock_total_pnl_pct)


def _format_portfolio_md(holding_list: list, total_pnl_pct: float) -> str:
    """格式化持仓信息为markdown字符串。空列表显示（无持仓）。"""
    lines = ['【当前持仓】']
    if not holding_list:
        lines.append('（无持仓）')
    for i, h in enumerate(holding_list, 1):
        buy_p = h['buy_price']
        cur_p = h['current_price']
        pnl_pct = ((cur_p / buy_p - 1) * 100) if buy_p > 0 else 0.0
        if pnl_pct > 0:
            lines.append(
                f"{i}. {h['name']}（{h['code']}） "
                f"买入¥{buy_p:.2f} 现价¥{cur_p:.2f} "
                f"<font color=\"#FF0000\">{pnl_pct:+.2f}%</font>"
            )
        elif pnl_pct < 0:
            lines.append(
                f"{i}. {h['name']}（{h['code']}） "
                f"买入¥{buy_p:.2f} 现价¥{cur_p:.2f} "
                f"<font color=\"#00BB00\">{pnl_pct:+.2f}%</font>"
            )
        else:
            lines.append(
                f"{i}. {h['name']}（{h['code']}） "
                f"买入¥{buy_p:.2f} 现价¥{cur_p:.2f} "
                f"{pnl_pct:+.2f}%"
            )
        # [Task#209] 出场规则大白话行(mock/无规则持仓无此键, 不展示)
        rule = h.get('sell_rule')
        if rule:
            lines.append(f"   └ {rule}")
    if total_pnl_pct > 0:
        lines.append(f'【累计总收益】<font color="#FF0000">{total_pnl_pct:+.2f}%</font>')
    elif total_pnl_pct < 0:
        lines.append(f'【累计总收益】<font color="#00BB00">{total_pnl_pct:+.2f}%</font>')
    else:
        lines.append(f'【累计总收益】{total_pnl_pct:+.2f}%')
    return '\n'.join(lines)


# =============================================================================
# 买入信号通知
# =============================================================================

def notify_buy(date_str: str, time_str: str, stock_name: str, stock_code: str,
               price: float, stop_profit_price: float = None,
               stop_loss_price: float = None, note: str = '') -> bool:
    """发送买入信号通知（企业微信双通道）。

    Args:
        date_str: 日期，如 "2026-08-05"
        time_str: 信号时间，如 "09:30:00"
        stock_name: 股票名称
        stock_code: 股票代码
        price: 买入价
        stop_profit_price: 止盈价（可选，trailing/timed模式可为None）
        stop_loss_price: 止损价（可选，timed模式可为None）
        note: 标注文本(可选, Task#205)。如"回撤加仓1.5x"、
            "弱市仓·到期早盘离场"；空串=不展示(向后兼容零侵入)。
    Returns:
        bool: 是否至少有一个通道发送成功
    """
    datetime_str = f"{date_str} {time_str}"
    tp_pct = None
    sl_pct = None
    if stop_profit_price is not None and price > 0:
        tp_pct = (stop_profit_price / price - 1) * 100
    if stop_loss_price is not None and price > 0:
        sl_pct = (stop_loss_price / price - 1) * 100

    portfolio_summary = get_portfolio_summary()

    # 纯文本格式（应用消息通道）
    lines = [
        '📥 买入信号',
        SEPARATOR,
        f'时间：{datetime_str}',
        f'股票：{stock_name}（{stock_code}）',
        f'买入价：¥{price:.2f}',
        f'仓位：{FIXED_POSITION_PCT}%',
    ]
    if tp_pct is not None:
        lines.append(f'止盈价：¥{stop_profit_price:.2f}（{tp_pct:+.2f}%）')
    if sl_pct is not None:
        lines.append(f'止损价：¥{stop_loss_price:.2f}（{sl_pct:+.2f}%）')
    if note:
        lines.append(f'标注：{note}')
    lines.append(SEPARATOR)
    lines.append(portfolio_summary)
    lines.append(SEPARATOR)
    lines.append(DISCLAIMER)
    text_msg = '\n'.join(lines)

    # Markdown格式（Webhook用，加粗标题+着色止盈止损）
    md_lines = [
        '**📥 买入信号**',
        SEPARATOR,
        f'时间：{datetime_str}',
        f'股票：{stock_name}（{stock_code}）',
        f'买入价：**¥{price:.2f}**',
        f'仓位：{FIXED_POSITION_PCT}%',
    ]
    if tp_pct is not None:
        md_lines.append(f'止盈价：<font color="#FF0000">¥{stop_profit_price:.2f}（{tp_pct:+.2f}%）</font>')
    if sl_pct is not None:
        md_lines.append(f'止损价：<font color="#00BB00">¥{stop_loss_price:.2f}（{sl_pct:+.2f}%）</font>')
    if note:
        md_lines.append(f'标注：**{note}**')
    md_lines.append(SEPARATOR)
    md_lines.append(portfolio_summary)
    md_lines.append(SEPARATOR)
    md_lines.append(DISCLAIMER)
    md_msg = '\n'.join(md_lines)

    ok1 = send_text(text_msg)
    ok2 = send_qywx(md_msg, msgtype='markdown')
    return ok1 or ok2


# =============================================================================
# 卖出信号通知
# =============================================================================

def notify_sell(date_str: str, time_str: str, stock_name: str, stock_code: str,
                sell_price: float, buy_price: float, hold_days: int,
                sold_codes: list = None) -> bool:
    """发送卖出信号通知（企业微信双通道）。

    Args:
        date_str: 日期，如 "2026-08-05"
        time_str: 信号时间，如 "09:30:00"
        stock_name: 股票名称
        stock_code: 股票代码
        sell_price: 卖出价
        buy_price: 买入价
        hold_days: 持仓天数
        sold_codes: 同批卖出标的代码列表(可选, Task#185)。持仓栏展示
            卖出后状态: 本笔(stock_code)及同批卖出标的全部剔除。
    Returns:
        bool: 是否至少有一个通道发送成功
    """
    datetime_str = f"{date_str} {time_str}"
    pnl_pct = ((sell_price / buy_price - 1) * 100) if buy_price > 0 else 0.0
    # Task#185: 卖出通知持仓栏须为卖出后状态——剔除本笔及同批卖出标的
    _exclude = set(sold_codes or [])
    _exclude.add(stock_code)
    portfolio_summary = get_portfolio_summary(exclude_codes=_exclude)

    # 纯文本格式
    lines = [
        '📤 卖出信号',
        SEPARATOR,
        f'时间：{datetime_str}',
        f'股票：{stock_name}（{stock_code}）',
        f'卖出价：¥{sell_price:.2f}',
        f'买入价：¥{buy_price:.2f}',
        f'持仓天数：{hold_days}天',
        f'盈亏：{pnl_pct:+.2f}%',
        SEPARATOR,
        portfolio_summary,
        SEPARATOR,
        DISCLAIMER,
    ]
    text_msg = '\n'.join(lines)

    # Markdown格式（盈亏着色）
    if pnl_pct > 0:
        pnl_tag = f'<font color="#FF0000">{pnl_pct:+.2f}%</font>'
    elif pnl_pct < 0:
        pnl_tag = f'<font color="#00BB00">{pnl_pct:+.2f}%</font>'
    else:
        pnl_tag = f'{pnl_pct:+.2f}%'
    md_lines = [
        '**📤 卖出信号**',
        SEPARATOR,
        f'时间：{datetime_str}',
        f'股票：{stock_name}（{stock_code}）',
        f'卖出价：**¥{sell_price:.2f}**',
        f'买入价：¥{buy_price:.2f}',
        f'持仓天数：{hold_days}天',
        f'盈亏：{pnl_tag}',
        SEPARATOR,
        portfolio_summary,
        SEPARATOR,
        DISCLAIMER,
    ]
    md_msg = '\n'.join(md_lines)

    ok1 = send_text(text_msg)
    ok2 = send_qywx(md_msg, msgtype='markdown')
    return ok1 or ok2


# =============================================================================
# 每日早盘决策汇总通知 (9:25决策完成后)
# =============================================================================

def notify_morning_summary(decisions: dict, gate_note: str = ''):
    """每日9:25决策完成后的汇总通知(早盘决策汇总)。

    decisions: {slot_id: {strategy, total_candidates, meet_condition, recommendations, ...}}
    gate_note: [Task#320]晋级率过热门控注明(空串=未触发, 格式与旧版完全兼容),
      如"过热门控生效(晋级率35.0%)"。
    """
    lines = ['📊 【每日决策汇总】']
    if gate_note:
        lines.append(f"🛑 {gate_note}, 今日不开新仓")
    buy_count = 0
    for slot_id in sorted(decisions.keys()):
        info = decisions[slot_id]
        recs = info.get('recommendations', [])
        meet = info.get('meet_condition', [])
        cands = info.get('total_candidates', 0)
        meet_count = len(meet) if isinstance(meet, list) else meet
        # [Task#285] 冻结策略行渲染(无frozen字段的旧决策行为不变)
        if info.get('frozen'):
            n_frozen = len(info.get('frozen_recommendations') or [])
            lines.append(f"  {slot_id}: 候选{cands}只 → 满足{meet_count}只 → "
                         f"🧊已冻结(假想{n_frozen}只)")
            continue
        lines.append(f"  {slot_id}: 候选{cands}只 → 满足{meet_count}只 → 买入{len(recs)}只")
        buy_count += len(recs)

    lines.append(f"\n今日买入: {buy_count}笔")
    lines.append(f"⏰ {time.strftime('%Y-%m-%d %H:%M:%S')}")
    msg = '\n'.join(lines)
    send_text(msg)
    send_qywx(msg)


# =============================================================================
# 盘后日报通知 (每日收盘后)
# =============================================================================

def notify_daily_summary(date_str: str, trades_log: list, today_pnl_pct: float,
                         breakdown: dict = None, note: str = ''):
    """盘后日报：今日收益(盯市) + 分解 + 今日交易 + 当前持仓 + 累计总收益。

    Args:
        date_str: 日期，如 "2026-08-05"
        trades_log: 今日交易列表, 每项为 dict:
            {'action': 'buy'|'sell', 'name': str, 'code': str,
             'price': float, 'pnl_pct': float(仅sell有, 买入价基准全周期盈亏)}
        today_pnl_pct: 今日收益百分比(盯市口径: 今收净值环比昨收净值)
        breakdown: 可选分解 {'realized_pct': 卖出贡献%, 'float_pct': 持仓浮动贡献%}
            (两部分均以昨日净值为分母, 相加=today_pnl_pct)
        note: 可选标题后缀, 如"修正版"

    格式:
        📊 今日收益通知（修正版）
        ━━━━━━━━━━━━━
        日期：2026-08-05
        今日收益：+3.20%（盘后盯市）
        └ 已实现（卖出）贡献 +0.50% / 持仓浮动贡献 +2.70%（含新买浮盈）
        今日交易：
          买入：财富趋势（688318）¥76.24
          卖出：唐源电气（300789）¥18.25（-8.75%）  ←笔明细为买入价基准全周期
        ━━━━━━━━━━━━━
        【当前持仓】...
        【累计总收益】+12.35%
        ━━━━━━━━━━━━━
        ⚠️ 免责声明：...
    """
    title = '📊 今日收益通知' + (f'（{note}）' if note else '')
    if today_pnl_pct > 0:
        pnl_color_str = f'<font color="#FF0000">{today_pnl_pct:+.2f}%</font>'
    elif today_pnl_pct < 0:
        pnl_color_str = f'<font color="#00BB00">{today_pnl_pct:+.2f}%</font>'
    else:
        pnl_color_str = f'{today_pnl_pct:+.2f}%'

    # 盯市分解小节(卖出贡献基准为昨收而非买入价, 与笔明细的全周期盈亏分离)
    breakdown_line = ''
    if breakdown:
        realized = breakdown.get('realized_pct', 0)
        floating = breakdown.get('float_pct', 0)
        breakdown_line = (f'└ 已实现（卖出）贡献 {realized:+.2f}% / '
                          f'持仓浮动贡献 {floating:+.2f}%（含新买浮盈）')

    # 交易记录
    trade_lines = []
    for t in trades_log:
        action = t.get('action', '')
        name = t.get('name', '')
        code = t.get('code', '')
        price = t.get('price', 0)
        if action == 'buy':
            trade_lines.append(f"  买入：{name}（{code}）¥{price:.2f}")
        elif action == 'sell':
            pnl = t.get('pnl_pct', 0)
            if pnl > 0:
                pnl_c = f'<font color="#FF0000">{pnl:+.2f}%</font>'
            elif pnl < 0:
                pnl_c = f'<font color="#00BB00">{pnl:+.2f}%</font>'
            else:
                pnl_c = f'{pnl:+.2f}%'
            trade_lines.append(
                f"  卖出：{name}（{code}）¥{price:.2f}"
                f"（{pnl_c}）"
            )
    if not trade_lines:
        trade_lines.append('  （今日无交易）')

    portfolio_summary = get_portfolio_summary()
    bb_line = _baostock_usage_line()   # [Task#259] BaoStock当日API用量一行

    # Markdown格式(头部先行盯市今日收益+分解, 再逐笔明细)
    md_lines = [
        f'**{title}**',
        SEPARATOR,
        f'日期：{date_str}',
        f'今日收益：{pnl_color_str}（盘后盯市）',
    ]
    if breakdown_line:
        md_lines.append(breakdown_line)
    md_lines.append('今日交易：')
    md_lines.extend(trade_lines)
    md_lines.append(SEPARATOR)
    md_lines.append(portfolio_summary)
    if bb_line:
        md_lines.append(bb_line)
    md_lines.append(SEPARATOR)
    md_lines.append(DISCLAIMER)
    md_msg = '\n'.join(md_lines)

    # 纯文本格式(去掉markdown标签)
    text_trade_lines = []
    for t in trades_log:
        action = t.get('action', '')
        name = t.get('name', '')
        code = t.get('code', '')
        price = t.get('price', 0)
        if action == 'buy':
            text_trade_lines.append(f"  买入：{name}（{code}）¥{price:.2f}")
        elif action == 'sell':
            pnl = t.get('pnl_pct', 0)
            text_trade_lines.append(f"  卖出：{name}（{code}）¥{price:.2f}（{pnl:+.2f}%）")
    if not text_trade_lines:
        text_trade_lines.append('  （今日无交易）')

    # 持仓信息的纯文本版本
    text_portfolio = _format_portfolio_text(portfolio_summary)

    text_lines = [
        title,
        SEPARATOR,
        f'日期：{date_str}',
        f'今日收益：{today_pnl_pct:+.2f}%（盘后盯市）',
    ]
    if breakdown_line:
        text_lines.append(breakdown_line)
    text_lines.append('今日交易：')
    text_lines.extend(text_trade_lines)
    text_lines.append(SEPARATOR)
    text_lines.append(text_portfolio)
    if bb_line:
        text_lines.append(bb_line)
    text_lines.append(SEPARATOR)
    text_lines.append(DISCLAIMER)
    text_msg = '\n'.join(text_lines)

    ok1 = send_text(text_msg)
    ok2 = send_qywx(md_msg, msgtype='markdown')
    return ok1 or ok2


def _format_portfolio_text(md_str: str) -> str:
    """将持仓信息的markdown字符串转为纯文本(去掉<font>标签)。"""
    import re as _re
    return _re.sub(r'<font color="[^"]*">(.*?)</font>', r'\1', md_str)


def _baostock_usage_line() -> str:
    """[Task#259] 盘后日报的BaoStock当日API用量一行(读统一预算账本,
    读失败/非今日静默返回空不阻断日报)。"""
    import json as _json
    from datetime import date as _date
    try:
        with open('/home/AIWealth/data/baostock_daily_budget.json',
                  encoding='utf-8') as f:
            d = _json.load(f)
        if d.get('date') != _date.today().isoformat():
            return ''
        used = d.get('used', 0)
        hard = d.get('hard_limit', 40000)
        return f'BaoStock当日API用量：{used}/{hard}（{used / hard * 100:.1f}%）'
    except Exception:
        return ''


# =============================================================================
# 晚间候选股通知 (Task#209)
# =============================================================================

def notify_candidates(trade_date: str, results: dict, top_n: int = 5) -> bool:
    """晚间候选股通知(含连板高度标注, 2板+高风险警示)。

    Args:
        trade_date: 候选适用交易日
        results: generate_candidates产出 {slot_id: {strategy_name, candidates}}
        top_n: 每策略展示前N只
    全部策略候选为空时不发送。
    """
    md_lines = [f'**📋 明日（{trade_date}）候选股**', SEPARATOR]
    text_lines = [f'📋 明日（{trade_date}）候选股', SEPARATOR]
    total = 0
    for slot_id in sorted(results.keys()):
        slot = results[slot_id] or {}
        cands = slot.get('candidates') or []
        if not cands:
            continue
        strat = slot.get('strategy_name', '')
        cn = STRATEGY_CN.get(strat, strat)
        # [Task#285] 冻结策略标注(无frozen字段的旧候选行为不变)
        frozen_tag = ' 🧊已冻结·仅假想跟踪不买入' if slot.get('frozen') else ''
        md_lines.append(f'【{cn}】{len(cands)}只{frozen_tag}')
        text_lines.append(f'【{cn}】{len(cands)}只{frozen_tag}')
        for c in cands[:top_n]:
            label = _board_label(c.get('board_height'))
            try:
                h = int(c.get('board_height') or 0)
            except (TypeError, ValueError):
                h = 0
            # Task#253: 公告风险预警★注(命中候选自动排除买入, 保留展示)
            ann = c.get('ann_alert') or {}
            ann_star = '★' if ann else ''
            # Task#297: 财报期拦截📊注(预约披露日在持有窗内, 不买避雷)
            eb_date = (c.get('earnings_appoint_date') or ''
                       ) if c.get('earnings_block') else ''
            eb_tag = '📊' if eb_date else ''
            base = (f"  {ann_star}{eb_tag}{c.get('name', '')}（{c.get('code', '')}） "
                    f"收{c.get('signal_close', 0)}")
            if label and h >= 2:
                md_lines.append(base + f' {label} <font color="#FF0000">⚠️高风险</font>')
                text_lines.append(base + f' {label} ⚠️高风险')
            elif label:
                md_lines.append(base + f' {label}')
                text_lines.append(base + f' {label}')
            else:
                md_lines.append(base)
                text_lines.append(base)
            if ann:
                note = (f"    ★公告风险预警：《{ann.get('title', '')}》"
                        f"·暂不买入")
                md_lines.append(f'<font color="#FF0000">{note}</font>')
                text_lines.append(note)
            if eb_date:
                note = (f"    📊财报期：预约披露{eb_date[5:]}在持有窗内"
                        f"·不买入避雷")
                md_lines.append(f'<font color="#FF9900">{note}</font>')
                text_lines.append(note)
        if len(cands) > top_n:
            md_lines.append(f'  …共{len(cands)}只(余略)')
            text_lines.append(f'  …共{len(cands)}只(余略)')
        total += len(cands)
    if total == 0:
        print('[notify] 候选为空, 跳过候选通知')
        return False
    tail = '⚠️ 2板及以上次日止损打穿风险约为首板4倍, 谨慎参与'
    md_lines += [SEPARATOR, tail, SEPARATOR, DISCLAIMER]
    text_lines += [SEPARATOR, tail, SEPARATOR, DISCLAIMER]
    ok1 = send_text('\n'.join(text_lines))
    ok2 = send_qywx('\n'.join(md_lines), msgtype='markdown')
    return ok1 or ok2


# =============================================================================
# 打板信号通知
# =============================================================================

def notify_surge(date_str: str, time_str: str, stock_name: str, stock_code: str,
                 current_price: float, surge_pct: float,
                 vol_ratio: float = None) -> bool:
    """发送打板信号通知（企业微信群机器人Webhook）。

    Args:
        date_str: 日期，如 "2026-08-05"
        time_str: 信号时间，如 "09:30:00"
        stock_name: 股票名称
        stock_code: 股票代码
        current_price: 当前价
        surge_pct: 涨幅百分比
        vol_ratio: 量能比（可选）
    Returns:
        bool: 是否发送成功
    """
    datetime_str = f"{date_str} {time_str}"
    vr_str = f"{vol_ratio:.1f}x" if vol_ratio is not None else "无基线"
    portfolio_summary = get_portfolio_summary()

    # 纯文本格式
    lines = [
        '🔥 打板信号',
        SEPARATOR,
        f'时间：{datetime_str}',
        f'股票：{stock_name}（{stock_code}）',
        f'当前价：¥{current_price:.2f}',
        f'涨幅：{surge_pct:+.2f}%',
        f'量能比：{vr_str}',
        f'仓位：{FIXED_POSITION_PCT}%',
        SEPARATOR,
        portfolio_summary,
        SEPARATOR,
        DISCLAIMER,
    ]
    text_msg = '\n'.join(lines)

    # Markdown格式
    md_lines = [
        '**🔥 打板信号**',
        SEPARATOR,
        f'时间：{datetime_str}',
        f'股票：{stock_name}（{stock_code}）',
        f'当前价：**¥{current_price:.2f}**',
        f'涨幅：<font color="#FF0000">{surge_pct:+.2f}%</font>',
        f'量能比：{vr_str}',
        f'仓位：{FIXED_POSITION_PCT}%',
        SEPARATOR,
        portfolio_summary,
        SEPARATOR,
        DISCLAIMER,
    ]
    md_msg = '\n'.join(md_lines)

    send_text(text_msg)
    return send_qywx(md_msg, msgtype='markdown')


if __name__ == '__main__':
    print("测试企业微信通知...")
    print(f"  应用消息通道: {'已配置' if CORP_ID else '未配置'}")
    print(f"  群机器人Webhook: {'已配置' if QYWX_WEBHOOK_KEY else '未配置'}")
    test_date = datetime.now().strftime('%Y-%m-%d')
    notify_buy(test_date, '09:30:00', '财富趋势', '688318', 76.24,
              stop_profit_price=83.86, stop_loss_price=73.19)
    notify_sell(test_date, '09:30:00', '唐源电气', '300859', 18.25, 20.00, 3)
