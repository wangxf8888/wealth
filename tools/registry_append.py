#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""统一注册表写入器（Task#281 治理基建，根治多agent并发写README丢行）。

背景：research/ideas/README.md 由多个agent并发"整文件读-改-写"回填，已实证造成
≥5行丢失（死路#59/#66/#67行、登记表#67/#68行）——典型竞态：A读→B读→A写→B写覆盖A。
本工具以 fcntl 排他锁（锁文件 data/.registry.lock）串行化全部写入，锁内重读最新
文件→按节定位插入→原子写tmp+rename→自动grep回读新行，打印 PASS/FAIL 并以退出码
0/1 返回。**回填/补录一律经本工具（或等效flock+回读机制），直接编辑注册表视为违规。**

支持三种插入模式（--mode）：
  deadend  死路否决清单表行（README「## 二、死路否决清单」，按编号升序定位插入）
  idea     Idea登记表行    （README「## 三、Idea 登记表」，按编号升序定位插入）
  note     详注区条目      （README「> **NO-GO/搁置结论存档：**」区，追加到末条之后）

用法示例（后续任务书直接引用）：
  # 1) 回填死路清单一行（编号从行内自动解析；同号已存在则FAIL防双写）
  python3 tools/registry_append.py --mode deadend \
      --row '| 69 | 方向名（口径） | 否决结论摘要…（2026-08-12 Task#282，t282_xxx） |'

  # 2) 登记新idea一行
  python3 tools/registry_append.py --mode idea \
      --row '| 105 | 方向 | 中文名 | 核心假设… | G1 | 进行中 | 研究席Xxx | 2026-08-12 |'

  # 3) 详注区追加NO-GO存档条目（同号允许重复如"#3机构分支/#3游资分支"，整行重复才FAIL）
  python3 tools/registry_append.py --mode note \
      --row '> - #105 xxx G1(2026-08-12 Task#282 FAIL归档，回填死路#69)：…'

  # 4) 成功判据：stdout 末行 "PASS ..."，退出码0；任何异常/回读失败=FAIL退出码1。
  #    归档SOP硬检查：结题汇报必须附本工具PASS输出作为"已回填并回读"证据。

可选参数：--file 目标文件(默认research/ideas/README.md) --lock 锁文件 --wait 抢锁
超时秒(默认60) --force 允许同号插入（仅撞号治理等特殊场景，慎用）。

供脚本复用的等效API：locked_edit(target, transform, lock_path)——同一把锁内
读→transform(text)->text→原子写，非追加类治理操作（如行序重排/节头注记）走此入口。
"""

import argparse
import fcntl
import os
import re
import sys
import tempfile
import time

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEFAULT_TARGET = os.path.join(REPO_ROOT, "research", "ideas", "README.md")
DEFAULT_LOCK = os.path.join(REPO_ROOT, "data", ".registry.lock")

# 模式定义：节锚点 + 行样式 + 编号提取 + 是否按编号排序插入
MODES = {
    "deadend": {
        "anchor": re.compile(r"^## 二、死路否决清单"),
        "row_re": re.compile(r"^\| (\d+) \|"),
        "sorted_insert": True,
        "dup_num_fail": True,
    },
    "idea": {
        "anchor": re.compile(r"^## 三、Idea 登记表"),
        "row_re": re.compile(r"^\| (\d+) \|"),
        "sorted_insert": True,
        "dup_num_fail": True,
    },
    "note": {
        "anchor": re.compile(r"^> \*\*NO-GO/搁置结论存档：\*\*"),
        "row_re": re.compile(r"^> - #(\d+)"),
        "sorted_insert": False,   # 详注区历史为时序追加，尾部追加
        "dup_num_fail": False,    # 同号合法（如 #3机构分支/#3游资分支），仅整行重复FAIL
    },
}
SECTION_END = re.compile(r"^(## |---\s*$|> 登记规则)")


class RegistryLock:
    """fcntl排他锁：flock(LOCK_EX)，带超时重试。锁存续期内其他写入器全部阻塞。"""

    def __init__(self, lock_path=DEFAULT_LOCK, wait=60.0):
        self.lock_path = lock_path
        self.wait = wait
        self.fd = None

    def __enter__(self):
        os.makedirs(os.path.dirname(self.lock_path), exist_ok=True)
        self.fd = os.open(self.lock_path, os.O_CREAT | os.O_RDWR, 0o644)
        deadline = time.time() + self.wait
        while True:
            try:
                fcntl.flock(self.fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.time() >= deadline:
                    os.close(self.fd)
                    self.fd = None
                    raise TimeoutError(
                        f"抢锁超时{self.wait}s：{self.lock_path} 正被其他写入器持有，勿硬写，稍后重试")
                time.sleep(0.2)
        # 留痕：谁持有过锁（诊断用，非协议一部分）
        try:
            os.ftruncate(self.fd, 0)
            os.write(self.fd, f"pid={os.getpid()} t={time.strftime('%F %T')}\n".encode())
        except OSError:
            pass
        return self

    def __exit__(self, *exc):
        if self.fd is not None:
            fcntl.flock(self.fd, fcntl.LOCK_UN)
            os.close(self.fd)
            self.fd = None
        return False


def _atomic_write(target, text):
    """同目录tmp写入+fsync+os.replace，保证任一时刻文件完整。"""
    d = os.path.dirname(os.path.abspath(target))
    fd, tmp = tempfile.mkstemp(prefix=".registry_tmp_", dir=d)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, target)
    except BaseException:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise


def locked_edit(target, transform, lock_path=DEFAULT_LOCK, wait=60.0):
    """等效API：锁内 读最新文件→transform(text)->new_text→原子写。返回新文本。

    transform 返回 None 表示放弃写入（no-op）。调用方自行做回读断言。"""
    with RegistryLock(lock_path, wait):
        with open(target, encoding="utf-8") as f:
            text = f.read()
        new_text = transform(text)
        if new_text is None or new_text == text:
            return text
        _atomic_write(target, new_text)
        return new_text


def _find_insert_pos(lines, mode_cfg, new_num, row):
    """返回插入下标（lines索引）。定位失败/重复冲突抛ValueError。"""
    anchor_idx = None
    for i, ln in enumerate(lines):
        if mode_cfg["anchor"].match(ln):
            anchor_idx = i
            break
    if anchor_idx is None:
        raise ValueError("未找到节锚点，目标文件结构疑似变更，拒绝盲写")

    row_idx = []   # [(lineno, num)]
    end = len(lines)
    started = False
    for i in range(anchor_idx + 1, len(lines)):
        m = mode_cfg["row_re"].match(lines[i])
        if m:
            started = True
            row_idx.append((i, int(m.group(1))))
            continue
        # 行区已开始后，遇节终止符即止；表内的表头/分隔/空行继续扫
        if started and SECTION_END.match(lines[i]):
            end = i
            break
        if not started and SECTION_END.match(lines[i]) and i > anchor_idx + 1:
            end = i
            break
    if not row_idx:
        raise ValueError("节内未找到任何既有行，拒绝盲写（防锚错节）")

    # 重复检测
    for i, num in row_idx:
        if lines[i].strip() == row.strip():
            raise ValueError(f"整行重复：第{i+1}行已存在完全相同内容，拒绝双写")
    if mode_cfg["dup_num_fail"] and any(num == new_num for _, num in row_idx):
        dup_at = [i + 1 for i, num in row_idx if num == new_num]
        raise ValueError(f"编号#{new_num}已存在（行{dup_at}），拒绝双写；如撞号治理需覆盖请人工裁决后用--force")

    if mode_cfg["sorted_insert"]:
        # 插到首个编号>new_num的行之前；否则插到最后一行之后
        for i, num in row_idx:
            if num > new_num:
                return i
    return row_idx[-1][0] + 1


def append_row(mode, row, target=DEFAULT_TARGET, lock_path=DEFAULT_LOCK,
               wait=60.0, force=False):
    """核心流程：锁→重读→定位插入→原子写→grep回读。返回(ok, message)。"""
    cfg = dict(MODES[mode])
    if force:
        cfg["dup_num_fail"] = False
    row = row.rstrip("\n")
    m = cfg["row_re"].match(row)
    if not m:
        return False, f"--row 不符合{mode}模式行样式 {cfg['row_re'].pattern!r}"
    new_num = int(m.group(1))

    try:
        with RegistryLock(lock_path, wait):
            with open(target, encoding="utf-8") as f:
                lines = f.read().splitlines()
            pos = _find_insert_pos(lines, cfg, new_num, row)
            lines.insert(pos, row)
            _atomic_write(target, "\n".join(lines) + "\n")
    except (TimeoutError, ValueError, OSError) as e:
        return False, str(e)

    # grep回读硬检查：锁外重读磁盘上的最终文件，逐字节核对新行确实持久化
    with open(target, encoding="utf-8") as f:
        final = f.read().splitlines()
    hits = [i + 1 for i, ln in enumerate(final) if ln == row]
    if len(hits) == 1:
        return True, f"回读命中 {os.path.relpath(target, REPO_ROOT)}:{hits[0]} （#{new_num}，{mode}）"
    return False, f"回读异常：新行命中{len(hits)}次（期望1），行号={hits}，请人工核查"


def main(argv=None):
    ap = argparse.ArgumentParser(description="统一注册表写入器（flock+原子写+grep回读）")
    ap.add_argument("--mode", required=True, choices=sorted(MODES))
    ap.add_argument("--row", required=True, help="要插入的完整一行（含表格竖线/引用前缀）")
    ap.add_argument("--file", default=DEFAULT_TARGET)
    ap.add_argument("--lock", default=DEFAULT_LOCK)
    ap.add_argument("--wait", type=float, default=60.0)
    ap.add_argument("--force", action="store_true", help="允许同号插入（慎用）")
    args = ap.parse_args(argv)

    ok, msg = append_row(args.mode, args.row, target=args.file,
                         lock_path=args.lock, wait=args.wait, force=args.force)
    print(("PASS " if ok else "FAIL ") + msg)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
