#!/usr/bin/env python3
"""从carve候选blob中精确切出可AST解析的完整策略源文件.
起点=4096对齐边界或blob头; 终点=文本自然结束(尾部空白/NUL截断).
验收: ast.parse成功 + 含目标class + 大小接近原文件(13903/13506B)."""
import ast, glob, hashlib, os

OUT = '/home/AIWealth/research/results/t327_cleanup_final/carve'
TARGETS = {
    'firstboard_low_open_dip': ('FirstboardLowOpenDipStrategy', 13903),
    'two_board_pullback_dip': ('TwoBoardPullbackDipStrategy', 13506),
}
good = {}
for name, (cls, size) in TARGETS.items():
    for path in glob.glob(f'{OUT}/{name}_*.py'):
        raw = open(path, 'rb').read()
        starts = [0] + [i for i in range(4096 - (13903 % 4096), len(raw), 4096)]
        starts = sorted(set(s for s in range(0, len(raw), 512)))  # 密集尝试512步进
        for s in starts:
            chunk = raw[s:s + size + 64]
            # 尾部截到最后一个换行, 逐步回退直到parse成功
            for endpad in range(0, 65):
                blob = chunk[:size + 64 - endpad]
                nl = blob.rfind(b'\n')
                if nl < size - 4096:
                    break
                blob = blob[:nl + 1]
                try:
                    text = blob.decode('utf-8')
                    tree = ast.parse(text)
                except Exception:
                    continue
                classes = [n.name for n in ast.walk(tree) if isinstance(n, ast.ClassDef)]
                funcs = [n.name for n in ast.walk(tree) if isinstance(n, ast.FunctionDef)]
                if cls in classes and len(blob) >= size - 200:
                    h = hashlib.md5(blob).hexdigest()[:10]
                    if (name, h) not in good:
                        good[(name, h)] = (len(blob), classes, sorted(set(funcs)), text)
                        print(f'[GOOD] {name} md5={h} bytes={len(blob)} classes={classes}')
                break
for (name, h), (n, c, fns, text) in good.items():
    p = f'{OUT}/GOOD_{name}_{h}.py'
    open(p, 'w').write(text)
    print(f'{p} funcs={fns}')
print('[DONE]', len(good))
