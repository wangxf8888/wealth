#!/usr/bin/env python3
"""Task#327事故恢复: 从/dev/vda3裸设备雕刻误删的两个策略基类源码.
原理: ext4小文件数据块刚释放未覆盖, 按唯一类名锚点定位, 取锚点所在
块对齐邻域, 再按python源码特征切出文件头尾. 只读设备零写入风险."""
import sys, os

DEV = '/dev/vda3'
CHUNK = 64 * 1024 * 1024
OVERLAP = 64 * 1024
MARKERS = {
    b'class FirstboardLowOpenDipStrategy': 'firstboard_low_open_dip',
    b'class TwoBoardPullbackDipStrategy': 'two_board_pullback_dip',
}
OUT = '/home/AIWealth/research/results/t327_cleanup_final/carve'
os.makedirs(OUT, exist_ok=True)

hits = {}
with open(DEV, 'rb') as f:
    off = 0
    while True:
        f.seek(off)
        buf = f.read(CHUNK + OVERLAP)
        if not buf:
            break
        for m, name in MARKERS.items():
            p = 0
            while True:
                i = buf.find(m, p)
                if i < 0 or i >= CHUNK:
                    break
                abs_off = off + i
                hits.setdefault(name, []).append(abs_off)
                print(f'[HIT] {name} @ {abs_off}', flush=True)
                p = i + 1
        off += CHUNK
        if off % (4 * 1024**3) == 0:
            print(f'[SCAN] {off/1024**3:.0f}GB', flush=True)

for name, offs in hits.items():
    with open(DEV, 'rb') as f:
        for k, ho in enumerate(offs):
            start = max(0, (ho // 4096) * 4096 - 8 * 4096)
            f.seek(start)
            region = f.read(24 * 4096)
            with open(f'{OUT}/{name}_hit{k}.bin', 'wb') as w:
                w.write(region)
            print(f'[DUMP] {name}_hit{k}.bin region@{start}', flush=True)
print('[DONE]', {k: len(v) for k, v in hits.items()}, flush=True)
