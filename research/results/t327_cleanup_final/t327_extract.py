#!/usr/bin/env python3
"""按carve_scan.log记录的命中offset, 读取邻域并切出完整python源文件候选.
文件头锚: '# -*- coding' 或 docstring/注释起始; 文件尾: 下一个NUL或非文本区.
输出所有候选到carve/, 按md5去重."""
import re, hashlib, os

LOG = '/home/AIWealth/research/results/t327_cleanup_final/carve_scan.log'
OUT = '/home/AIWealth/research/results/t327_cleanup_final/carve'
DEV = '/dev/vda3'
os.makedirs(OUT, exist_ok=True)

hits = {}
for line in open(LOG):
    m = re.match(r'\[HIT\] (\S+) @ (\d+)', line)
    if m:
        hits.setdefault(m.group(1), []).append(int(m.group(2)))

seen = {}
with open(DEV, 'rb') as f:
    for name, offs in hits.items():
        for ho in offs:
            start = max(0, (ho // 4096) * 4096 - 5 * 4096)  # 类定义前最多~20KB
            f.seek(start)
            region = f.read(10 * 4096)
            # 在region内定位文件头: 常见头 '#!/usr/bin' / '# -*-' / '"""'
            anchor = ho - start
            # 从锚点向前找最近的NUL边界, 再从边界后找文本起点
            nul = region.rfind(b'\x00', 0, anchor)
            head = nul + 1 if nul >= 0 else 0
            # 向后找文件尾: 锚点后第一个NUL
            nul2 = region.find(b'\x00', anchor)
            tail = nul2 if nul2 >= 0 else len(region)
            blob = region[head:tail]
            if len(blob) < 2000:
                continue
            try:
                text = blob.decode('utf-8')
            except UnicodeDecodeError:
                continue
            md5 = hashlib.md5(blob).hexdigest()[:10]
            key = (name, md5)
            if key in seen:
                continue
            seen[key] = 1
            p = f'{OUT}/{name}_{md5}.py'
            open(p, 'w').write(text)
            print(f'[CAND] {p} bytes={len(blob)} head_ok={"coding" in text[:200] or text[:2] in ("#!","# ",chr(34)*2)}')
print('[DONE] candidates:', len(seen))
