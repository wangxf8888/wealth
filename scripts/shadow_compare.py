#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Task#45 影子比对汇总: 读shadow_exit_YYYYMMDD.jsonl, 给出切换裁决建议。

用法: python3 scripts/shadow_compare.py [--date 2026-07-27]
      (默认今天; 周一收盘后跑, 零差异→周二 USE_EXECUTION_CORE=1 正式切换)

裁决口径:
  MATCH      = compare事件match=True (双路径同action) → 计入零差异
  DIVERGENCE = compare事件match=False, 或仅单侧触发(core-only/legacy-only)
  说明: core与legacy卖价天然有语义差(bar级挂单语义价 vs 现价), 价差仅展示
        不判differ; action与"是否卖"才是切换安全性的判据。
  已知可解释差异类别(出现时人工归因, 不自动放行):
    - legacy-only且发生在daemon启动首轮: 日级兜底捕捉启动前触发,
      core路径bar流尚未建立(影子无日级兜底) → 属预期, 归因后可放行;
    - core-only且发生在14:55后: legacy现价未触但bar级挂单语义在尾盘
      bar内触及 → 需人工复核bar数据。
"""
import sys
import os
import json
import argparse
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from realtime.config import LOG_DIR


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--date', default=datetime.now().strftime('%Y-%m-%d'))
    args = parser.parse_args()
    path = os.path.join(LOG_DIR,
                        f"shadow_exit_{args.date.replace('-', '')}.jsonl")
    print(f"影子比对汇总 {args.date} | {path}")
    if not os.path.exists(path):
        print("[INFO] 无影子日志 → 当日无任何触发事件(双路径都未卖) = 零差异")
        print("裁决建议: PASS (可切换, 但样本为空, 建议结合当日是否有持仓判断)")
        return 0

    events = [json.loads(line) for line in open(path, encoding='utf-8')]
    by_code = {}
    for e in events:
        by_code.setdefault(e['code'], []).append(e)

    divergences = []
    matches = []
    for code, evs in sorted(by_code.items()):
        core = next((e for e in evs if e['event'] == 'core_trigger'), None)
        legacy = next((e for e in evs if e['event'] == 'legacy_trigger'), None)
        comp = next((e for e in evs if e['event'] == 'compare'), None)
        if comp:
            line = (f"  {code}: core={comp['core']['action']}"
                    f"@{comp['core']['sell_price']}"
                    f"(bar {comp['core']['bar']}) | "
                    f"legacy={comp['legacy']['action']}"
                    f"@{comp['legacy']['price']} | "
                    f"match={comp['match']}")
            (matches if comp['match'] else divergences).append(line)
        elif core and not legacy:
            divergences.append(
                f"  {code}: CORE-ONLY {core['action']}@{core['sell_price']}"
                f" (bar {core['bar']}, ts {core['ts']}) legacy未触发")
        elif legacy and not core:
            divergences.append(
                f"  {code}: LEGACY-ONLY {legacy['action']}"
                f"@{legacy['price']} (ts {legacy['ts']}) core未触发"
                f" —— 若为启动首轮日级兜底属预期(见文档差异类别)")

    print(f"\n事件: {len(events)}条 | 涉及持仓: {len(by_code)}只")
    print(f"一致(MATCH): {len(matches)}")
    for line in matches:
        print(line)
    print(f"差异(DIVERGENCE): {len(divergences)}")
    for line in divergences:
        print(line)

    print()
    if divergences:
        print("裁决建议: HOLD —— 存在差异, 逐条归因后再定; "
              "未归因前周二不切换(维持USE_EXECUTION_CORE=0或继续shadow)")
        return 1
    print("裁决建议: PASS —— 零差异, 周二可置 USE_EXECUTION_CORE=1 正式切换")
    return 0


if __name__ == '__main__':
    sys.exit(main())
