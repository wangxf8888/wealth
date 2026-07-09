#!/usr/bin/env python3
"""
自动更新 PROJECT_STATUS.md 的时间戳，并检测最新完成的验证任务。
每10分钟由cron调度运行。
"""

import os
import re
import glob
from datetime import datetime

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
STATUS_FILE = os.path.join(PROJECT_ROOT, "PROJECT_STATUS.md")
LOGS_DIR = os.path.join(PROJECT_ROOT, "scripts", "logs")


def update_timestamp(content: str) -> str:
    """更新'最后更新'时间戳为当前时间"""
    now = datetime.now().strftime("%Y-%m-%d %H:%M")
    pattern = r"(>\s*最后更新:\s*)\d{4}-\d{2}-\d{2}\s+\d{2}:\d{2}"
    return re.sub(pattern, rf"\g<1>{now}", content)


def detect_completed_tasks() -> list:
    """扫描logs目录，检测最近10分钟内新生成/修改的日志文件"""
    if not os.path.isdir(LOGS_DIR):
        return []

    now = datetime.now().timestamp()
    ten_minutes_ago = now - 600
    recent_logs = []

    for log_file in glob.glob(os.path.join(LOGS_DIR, "*.log")):
        mtime = os.path.getmtime(log_file)
        if mtime >= ten_minutes_ago:
            basename = os.path.basename(log_file)
            recent_logs.append(basename)

    return recent_logs


def check_log_for_completion(log_path: str) -> str | None:
    """检查日志文件末尾是否包含完成标志"""
    try:
        with open(log_path, "r", encoding="utf-8") as f:
            # 只读最后2KB
            f.seek(0, 2)
            size = f.tell()
            f.seek(max(0, size - 2048))
            tail = f.read()

        # 检测常见完成标志
        completion_markers = [
            "全年验证完成",
            "验证完成",
            "DONE",
            "完成",
            "Summary:",
            "Total trades:",
        ]
        for marker in completion_markers:
            if marker in tail:
                return marker
    except Exception:
        pass
    return None


def main():
    if not os.path.exists(STATUS_FILE):
        print(f"[ERROR] {STATUS_FILE} not found")
        return

    with open(STATUS_FILE, "r", encoding="utf-8") as f:
        content = f.read()

    # 1. 更新时间戳
    updated_content = update_timestamp(content)

    # 2. 检测新完成的任务（仅打印信息，不自动修改看板内容避免误判）
    recent_logs = detect_completed_tasks()
    if recent_logs:
        print(f"[INFO] 最近10分钟有活动的日志文件:")
        for log_name in recent_logs:
            log_path = os.path.join(LOGS_DIR, log_name)
            completion = check_log_for_completion(log_path)
            status = f"✅ {completion}" if completion else "🔄 运行中"
            print(f"  - {log_name}: {status}")

    # 3. 写回文件
    if updated_content != content:
        with open(STATUS_FILE, "w", encoding="utf-8") as f:
            f.write(updated_content)
        print(f"[OK] 时间戳已更新: {datetime.now().strftime('%Y-%m-%d %H:%M')}")
    else:
        print("[INFO] 时间戳无变化，跳过写入")


if __name__ == "__main__":
    main()
