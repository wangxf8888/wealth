#!/usr/bin/env python3
"""
Task#253 公告监控预警系统 (方案C, 用户批准 2026-08-10)
========================================================
背景: 宁夏建材事故——周末《延期履行避免同业竞争承诺》公告→周一跌停。
任何重组名单圈不住表外公告黑天鹅, 唯一防法=公告流监控(t252 EVAL_REPORT结论)。

扫描对象:
  持仓股 data/realtime/positions.json (status=holding)
  候选股 data/realtime/candidates_YYYYMMDD.json (当日或次日)

数据源: 巨潮fulltextSearch (t252实测可达, 按6位代码检索近LOOKBACK_DAYS日公告)

输出:
  1. data/realtime/ann_alerts.json (原子写, {date:[{code,name,title,matched_kw,url,ts,role}]})
  2. 命中候选回写候选json的ann_alert字段 (前端★渲染/notify★注数据源)
  3. 企微报警 (同代码同公告当日幂等不重复报; --dry-run落盘预览不真发)
  4. 承诺到期日历并入 (commitment_calendar.json到期前3日提醒, 缺失安全跳过)

cron三时点:
  30 7  * * 1-5  --scope both        (持仓+今日候选双扫, 9:25决策前)
  0  18 * * 1-5  --scope positions   (盘后持仓扫)
  35 23 * * 1-5  --scope candidates  (明日候选, 接23:30候选生成后)

兜底铁律: 本脚本任何失败只影响预警产出, 绝不触碰交易主链;
下游(morning_decision/generate_candidates)读ann_alerts.json缺失/损坏
一律降级为空=不排除任何候选。

用法:
  python3 tools/announcement_monitor.py --scope both
  python3 tools/announcement_monitor.py --scope positions
  python3 tools/announcement_monitor.py --scope candidates          # 最新候选文件
  python3 tools/announcement_monitor.py --scope both --dry-run      # 回放/验收: 不真发企微
  python3 tools/announcement_monitor.py --scope both --date 2026-08-10  # 覆盖"今天"(回放)
"""
import argparse
import glob
import json
import os
import re
import sys
import time
import urllib.parse
import urllib.request
from datetime import datetime, timedelta

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT_ROOT)

# =============================================================================
# 配置区
# =============================================================================

# 标题命中关键词表 (Task#253规格, 顶部可配置)
ANN_KEYWORDS = ['重组', '重大资产', '承诺', '延期', '终止', '问询', '立案',
                '减持计划', '业绩修正']

LOOKBACK_DAYS = 3          # 近3日公告窗口
REQUEST_MIN_INTERVAL = 1.0  # 请求限速: >=1s/只
REQUEST_RETRIES = 3        # 单只重试次数
REQUEST_TIMEOUT = 15       # 单请求超时秒
DAILY_BUDGET = 90          # 单次运行请求预算上限(克制)
PAGE_SIZE = 30             # 单股近3日公告不会超过30条

DATA_DIR = os.path.join(PROJECT_ROOT, 'data', 'realtime')
LOG_DIR = os.path.join(PROJECT_ROOT, 'logs', 'realtime')
POSITIONS_FILE = os.path.join(DATA_DIR, 'positions.json')
ALERTS_FILE = os.path.join(DATA_DIR, 'ann_alerts.json')
CALENDAR_FILE = os.path.join(DATA_DIR, 'commitment_calendar.json')
CALENDAR_WARN_DAYS = 3     # 承诺到期前N日并入预警

CNINFO_URL = 'http://www.cninfo.com.cn/new/fulltextSearch/full'
CNINFO_HEADERS = {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) '
                                'AppleWebKit/537.36'}
PDF_BASE = 'http://static.cninfo.com.cn/'

_request_count = 0


def log(msg: str):
    print(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] {msg}", flush=True)


def _alert_log(msg: str):
    """写scheduler_alerts.log(与daemon/cron告警同一人工巡检入口)。"""
    try:
        from realtime.position_tracker import SCHEDULER_ALERT_LOG
        with open(SCHEDULER_ALERT_LOG, 'a', encoding='utf-8') as f:
            f.write(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] {msg}\n")
    except Exception:
        pass


# =============================================================================
# 巨潮公告检索
# =============================================================================

def fetch_announcements(code6: str, sdate: str, edate: str,
                        extra_kw: str = '') -> list:
    """按6位代码查[sdate,edate]公告, 返回原始announcement列表。

    限速>=1s/只+重试; 预算耗尽/全部重试失败返回None(区别于空列表[]),
    调用方对None只告警不排除。extra_kw为附加检索词(承诺日历用)。
    """
    global _request_count
    if _request_count >= DAILY_BUDGET:
        log(f"  [预算] 请求数已达{DAILY_BUDGET}上限, 跳过{code6}")
        return None
    params = {
        'searchkey': (code6 + ' ' + extra_kw).strip(),
        'sdate': sdate, 'edate': edate,
        'isfulltext': 'false', 'sortName': 'pubdate', 'sortType': 'desc',
        'pageNum': 1, 'pageSize': PAGE_SIZE, 'type': '',
    }
    url = CNINFO_URL + '?' + urllib.parse.urlencode(params)
    for attempt in range(REQUEST_RETRIES):
        try:
            _request_count += 1
            time.sleep(REQUEST_MIN_INTERVAL)
            req = urllib.request.Request(url, headers=CNINFO_HEADERS)
            with urllib.request.urlopen(req, timeout=REQUEST_TIMEOUT) as resp:
                data = json.loads(resp.read().decode('utf-8'))
            anns = data.get('announcements') or []
            # searchkey按代码全文检索可能混入他股公告, 硬过滤secCode
            return [a for a in anns if a.get('secCode') == code6]
        except Exception as e:
            log(f"  [WARN] 巨潮查询{code6}第{attempt + 1}次失败: {e}")
            time.sleep(2 + attempt * 2)
    return None


def match_keywords(title: str) -> list:
    """标题命中关键词表, 返回命中词列表。"""
    return [kw for kw in ANN_KEYWORDS if kw in title]


def clean_title(raw: str) -> str:
    """去掉巨潮返回的<em>高亮标签与"股票名："前缀冗余。"""
    t = re.sub(r'</?em>', '', raw or '')
    # "宁夏建材：宁夏建材关于..." → 保留冒号后正文
    if '：' in t:
        head, _, rest = t.partition('：')
        if rest:
            t = rest
    return t.strip()


def scan_one(code: str, name: str, role: str, sdate: str, edate: str):
    """扫描单只股票, 返回(命中列表, 接口是否成功)。

    code为内部格式sh.600449, 巨潮用6位数字。
    """
    code6 = code.split('.')[-1]
    anns = fetch_announcements(code6, sdate, edate)
    if anns is None:
        return [], False
    hits = []
    for a in anns:
        title = clean_title(a.get('announcementTitle', ''))
        kws = match_keywords(title)
        if not kws:
            continue
        ann_id = str(a.get('announcementId', ''))
        adj = a.get('adjunctUrl', '')
        ann_date = adj.split('/')[1] if '/' in adj else ''
        hits.append({
            'code': code, 'name': name, 'title': title,
            'matched_kw': kws, 'ann_id': ann_id,
            'ann_date': ann_date,
            'url': PDF_BASE + adj if adj else '',
            'ts': datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
            'role': role,
        })
    return hits, True


# =============================================================================
# 扫描对象加载
# =============================================================================

def load_holdings() -> list:
    """持仓股列表[(code,name)], 缺失/损坏返回空并告警。"""
    try:
        with open(POSITIONS_FILE, encoding='utf-8') as f:
            data = json.load(f)
        return [(p['code'], p.get('name', ''))
                for p in data.get('positions', [])
                if p.get('status') == 'holding']
    except Exception as e:
        log(f"[WARN] positions.json读取失败({e}), 持仓扫描跳过")
        _alert_log(f"⚠️ 公告监控: positions.json读取失败({e}), 持仓扫描跳过")
        return []


def find_candidates_file(today: str) -> str:
    """定位候选文件: 优先trade_date>=today的最新candidates_*.json。

    7:30场景命中今日候选; 23:35场景命中刚生成的明日候选。
    """
    files = sorted(glob.glob(os.path.join(DATA_DIR, 'candidates_*.json')))
    for fp in reversed(files):
        try:
            with open(fp, encoding='utf-8') as f:
                data = json.load(f)
            if (data.get('trade_date') or '') >= today:
                return fp
        except Exception:
            continue
    return ''


def load_candidates(cand_file: str) -> tuple:
    """候选股去重列表[(code,name)]与trade_date。"""
    try:
        with open(cand_file, encoding='utf-8') as f:
            data = json.load(f)
    except Exception as e:
        log(f"[WARN] 候选文件{cand_file}读取失败({e}), 候选扫描跳过")
        return [], ''
    seen, out = set(), []
    for slot_data in (data.get('strategies') or {}).values():
        for c in slot_data.get('candidates') or []:
            code = c.get('code', '')
            if code and code not in seen:
                seen.add(code)
                out.append((code, c.get('name', '')))
    return out, data.get('trade_date', '')


# =============================================================================
# ann_alerts.json 读写 (原子写, 幂等)
# =============================================================================

def load_alerts() -> dict:
    try:
        with open(ALERTS_FILE, encoding='utf-8') as f:
            data = json.load(f)
        if isinstance(data, dict):
            return data
    except Exception:
        pass
    return {}


def save_alerts(data: dict):
    tmp = ALERTS_FILE + '.tmp'
    with open(tmp, 'w', encoding='utf-8') as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    os.replace(tmp, ALERTS_FILE)


def merge_alerts(alerts: dict, date_key: str, hits: list) -> list:
    """把命中并入alerts[date_key], 按code+ann_id去重, 返回本次新增条目。"""
    day = alerts.setdefault(date_key, [])
    existing = {(h.get('code'), h.get('ann_id')) for h in day}
    new_items = []
    for h in hits:
        key = (h['code'], h['ann_id'])
        if key in existing:
            # 已存在: role升级(候选→持仓+候选不覆盖, 但补充role并集)
            for old in day:
                if (old.get('code'), old.get('ann_id')) == key \
                        and h['role'] not in old.get('role', ''):
                    old['role'] = old.get('role', '') + '/' + h['role']
            continue
        existing.add(key)
        day.append(h)
        new_items.append(h)
    return new_items


# =============================================================================
# 候选json回写 ann_alert 字段 (前端★/notify★数据源)
# =============================================================================

def writeback_candidates(cand_file: str, day_alerts: list):
    """把命中标注回写候选json的候选条目ann_alert字段(原子写)。

    只增改ann_alert字段, 不动其余结构; 失败只告警不阻断。
    """
    if not cand_file or not day_alerts:
        return
    try:
        with open(cand_file, encoding='utf-8') as f:
            data = json.load(f)
        by_code = {}
        for h in day_alerts:
            by_code.setdefault(h['code'], h)
        n = 0
        for slot_data in (data.get('strategies') or {}).values():
            for c in slot_data.get('candidates') or []:
                h = by_code.get(c.get('code'))
                if h and not c.get('ann_alert'):
                    c['ann_alert'] = {'title': h['title'],
                                      'matched_kw': h['matched_kw'],
                                      'url': h.get('url', '')}
                    n += 1
        # 顶层向后兼容映射同步(strategy_name → candidates为同一list引用,
        # json.load后是两份对象, 需单独标注)
        for key, val in data.items():
            if isinstance(val, list):
                for c in val:
                    if isinstance(c, dict):
                        h = by_code.get(c.get('code'))
                        if h and not c.get('ann_alert'):
                            c['ann_alert'] = {'title': h['title'],
                                              'matched_kw': h['matched_kw'],
                                              'url': h.get('url', '')}
        if n:
            tmp = cand_file + '.tmp'
            with open(tmp, 'w', encoding='utf-8') as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
            os.replace(tmp, cand_file)
            log(f"  [回写] {os.path.basename(cand_file)} 标注ann_alert {n}处")
    except Exception as e:
        log(f"[WARN] 候选json回写ann_alert失败({e}), 不阻断")


# =============================================================================
# 企微报警 (幂等: _reported键记code|ann_id, 当日不重复报)
# =============================================================================

def send_alerts(alerts: dict, date_key: str, new_items: list,
                dry_run: bool, preview_path: str = ''):
    reported = alerts.setdefault('_reported', {}).setdefault(date_key, [])
    to_send = [h for h in new_items
               if f"{h['code']}|{h['ann_id']}" not in reported]
    if not to_send:
        return
    pos_items = [h for h in to_send if '持仓' in h['role']]
    cand_items = [h for h in to_send if '持仓' not in h['role']]
    msgs = []
    for title_role, items in (('持仓股', pos_items), ('候选股', cand_items)):
        if not items:
            continue
        lines = [f'⚠️公告预警·{title_role}', '━━━━━━━━━━━━━']
        for h in items:
            lines.append(f"{h['name']}（{h['code']}）")
            lines.append(f"《{h['title']}》")
            lines.append(f"命中: {'/'.join(h['matched_kw'])}"
                         f" | 公告日: {h.get('ann_date', '')}")
            if title_role == '候选股':
                lines.append('→ 候选股将自动排除买入(保留展示, 星号标注)')
            lines.append('')
        lines.append('详情见看板候选区/持仓区 ★ 标注')
        msgs.append('\n'.join(lines))
    if dry_run:
        preview = '\n\n========\n\n'.join(msgs)
        if preview_path:
            with open(preview_path, 'a', encoding='utf-8') as f:
                f.write(f"--- preview {datetime.now()} ---\n{preview}\n")
        log(f"  [dry-run] 报警预览已留档({len(to_send)}条), 未真实外发")
        # dry-run不消耗幂等额度: 不记_reported, 真实运行仍会首报
        return
    try:
        from realtime.notify import send_text, send_qywx
        for m in msgs:
            send_text(m)
            send_qywx(m, msgtype='text')
    except Exception as e:
        log(f"[WARN] 企微报警发送失败({e}), 预警已落盘不丢失")
        _alert_log(f"⚠️ 公告监控: 企微报警发送失败({e})")
    for h in to_send:
        reported.append(f"{h['code']}|{h['ann_id']}")
    # 留痕上限防膨胀
    alerts['_reported'][date_key] = reported[-300:]


# =============================================================================
# 承诺到期日历并入 (v1: 到期前N日对持仓股提示)
# =============================================================================

def check_commitment_calendar(today: str, holding_codes: set,
                              dry_run: bool, preview_path: str):
    """commitment_calendar.json缺失/损坏安全跳过。"""
    try:
        with open(CALENDAR_FILE, encoding='utf-8') as f:
            cal = json.load(f)
    except Exception:
        return
    try:
        today_dt = datetime.strptime(today, '%Y-%m-%d')
        due = []
        for item in cal.get('items', []):
            if item.get('code') not in holding_codes:
                continue
            exp = item.get('expiry_date', '')
            if not exp:
                continue
            days = (datetime.strptime(exp, '%Y-%m-%d') - today_dt).days
            if 0 <= days <= CALENDAR_WARN_DAYS:
                due.append((item, days))
        if not due:
            return
        lines = ['⚠️公告预警·承诺到期日历', '━━━━━━━━━━━━━']
        for item, days in due:
            lines.append(f"{item.get('name', '')}（{item['code']}）"
                         f"承诺到期日{item['expiry_date']}(还有{days}天)")
            lines.append(f"《{item.get('title', '')}》")
            lines.append('→ 到期窗内警惕延期/变更类公告(宁夏建材2年周期同款)')
        msg = '\n'.join(lines)
        if dry_run:
            if preview_path:
                with open(preview_path, 'a', encoding='utf-8') as f:
                    f.write(f"--- calendar preview {datetime.now()} ---\n{msg}\n")
            log(f"  [dry-run] 承诺日历提醒预览留档({len(due)}条)")
        else:
            # [Task#270] 补send_qywx: send_text是通道一(CORP_ID未配置永远跳过),
            # 群机器人通道(含.qywx_env.sh兜底)才是实际可达通道
            from realtime.notify import send_text, send_qywx
            send_text(msg)
            send_qywx(msg, msgtype='text')
        log(f"  [承诺日历] 到期窗提醒{len(due)}条")
    except Exception as e:
        log(f"[WARN] 承诺日历检查异常({e}), 跳过")


# =============================================================================
# 主流程
# =============================================================================

def main():
    ap = argparse.ArgumentParser(description='Task#253 公告监控预警')
    ap.add_argument('--scope', choices=['positions', 'candidates', 'both'],
                    default='both')
    ap.add_argument('--date', default=None, help='覆盖"今天"(回放用)')
    ap.add_argument('--dry-run', action='store_true',
                    help='不真发企微, 报警文本落盘预览')
    ap.add_argument('--preview-file', default=os.path.join(
        LOG_DIR, 'ann_alert_preview.txt'))
    ap.add_argument('--cand-file', default=None,
                    help='指定候选文件(回放用, 默认自动定位)')
    args = ap.parse_args()

    today = args.date or datetime.now().strftime('%Y-%m-%d')
    sdate = (datetime.strptime(today, '%Y-%m-%d')
             - timedelta(days=LOOKBACK_DAYS)).strftime('%Y-%m-%d')
    edate = (datetime.strptime(today, '%Y-%m-%d')
             + timedelta(days=1)).strftime('%Y-%m-%d')
    log(f"=== 公告监控启动 scope={args.scope} 窗口[{sdate},{edate}] "
        f"dry_run={args.dry_run} ===")

    targets = []   # [(code, name, role)]
    holding_codes = set()
    if args.scope in ('positions', 'both'):
        for code, name in load_holdings():
            targets.append((code, name, '持仓'))
            holding_codes.add(code)

    cand_file, cand_trade_date = '', ''
    if args.scope in ('candidates', 'both'):
        cand_file = args.cand_file or find_candidates_file(today)
        if cand_file:
            cands, cand_trade_date = load_candidates(cand_file)
            seen = {t[0] for t in targets}
            for code, name in cands:
                if code in seen:
                    # 持仓兼候选: 已扫过, 复用结果(见下方role并集)
                    targets.append((code, name, '候选*'))
                else:
                    targets.append((code, name, '候选'))
            log(f"候选文件: {os.path.basename(cand_file)} "
                f"trade_date={cand_trade_date} 候选{len(cands)}只")
        else:
            log("[WARN] 未找到适用候选文件, 候选扫描跳过")

    if not targets:
        log("扫描对象为空, 退出")
        return 0

    log(f"扫描对象共{len(targets)}只 (预算{DAILY_BUDGET}请求)")
    alerts = load_alerts()
    all_hits, fail_cnt = [], 0
    scanned_cache = {}   # code → hits (持仓兼候选去重复用)
    for code, name, role in targets:
        if code in scanned_cache:
            hits = [dict(h, role=role.rstrip('*')) for h in scanned_cache[code]]
            all_hits.extend(hits)
            continue
        hits, ok = scan_one(code, name, role.rstrip('*'), sdate, edate)
        if not ok:
            fail_cnt += 1
            continue
        scanned_cache[code] = hits
        if hits:
            for h in hits:
                log(f"  [命中] {role} {code} {name} "
                    f"《{h['title']}》 kw={h['matched_kw']}")
            all_hits.extend(hits)
        else:
            log(f"  [清白] {role} {code} {name}")

    # 预警落盘: 候选命中记在候选trade_date键下(9:25决策按trade_date读),
    # 持仓命中记在today键下
    new_items_all = []
    pos_hits = [h for h in all_hits if h['role'] == '持仓']
    cand_hits = [h for h in all_hits if h['role'] == '候选']
    if pos_hits:
        new_items_all += merge_alerts(alerts, today, pos_hits)
    if cand_hits:
        key = cand_trade_date or today
        new_items_all += merge_alerts(alerts, key, cand_hits)
    save_alerts(alerts)
    log(f"ann_alerts.json已更新: 命中{len(all_hits)}条(新增{len(new_items_all)}) "
        f"接口失败{fail_cnt}只 请求数{_request_count}")

    if fail_cnt:
        _alert_log(f"⚠️ 公告监控: {fail_cnt}只股票巨潮查询重试耗尽, "
                   f"本轮对其无预警覆盖(不排除候选), 请关注")

    # 候选json回写ann_alert(前端★数据源)
    if cand_file and cand_trade_date:
        writeback_candidates(cand_file, alerts.get(cand_trade_date, []))

    # 企微报警(幂等)
    if new_items_all:
        # 按落盘日期键分别做幂等记录: 持仓today, 候选trade_date
        pos_new = [h for h in new_items_all if h['role'] == '持仓']
        cand_new = [h for h in new_items_all if h['role'] != '持仓']
        if pos_new:
            send_alerts(alerts, today, pos_new, args.dry_run, args.preview_file)
        if cand_new:
            send_alerts(alerts, cand_trade_date or today, cand_new,
                        args.dry_run, args.preview_file)
        save_alerts(alerts)

    # 承诺到期日历(缺失安全跳过)
    check_commitment_calendar(today, holding_codes, args.dry_run,
                              args.preview_file)

    log("=== 公告监控完成 ===")
    return 0


if __name__ == '__main__':
    try:
        sys.exit(main())
    except Exception as e:
        # 兜底铁律: 本监控崩溃绝不影响主链, 但必须留告警痕迹
        log(f"[FATAL] 公告监控未捕获异常: {e!r}")
        _alert_log(f"🚨 公告监控崩溃({e!r}), 本轮无预警覆盖(不排除候选)")
        sys.exit(1)
