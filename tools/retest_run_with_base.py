# -*- coding: utf-8 -*-
"""不改开发侧脚本，把硬编码 8787 的闸门指到测试端口再跑。

做法：先在 __main__ 命名空间放进 BASE，再 runpy 执行目标脚本。
因为脚本顶层的 `BASE = "http://127.0.0.1:8787"` 会覆盖，所以改成
「先读源码、替换那一行、再 exec」—— 保持其余逻辑与文件完全一致。

用法：python tools/retest_run_with_base.py <脚本路径> <BASE>
"""
import io
import os
import re
import sys

script = sys.argv[1]
base = sys.argv[2]

src = io.open(script, encoding="utf-8").read()
new_src, n = re.subn(r'^BASE\s*=\s*"http://127\.0\.0\.1:\d+"', 'BASE = %r' % base,
                     src, count=1, flags=re.M)
if n == 0:
    print(f"⚠ 未在 {script} 里找到硬编码的 BASE 行，原样执行（可能用的是环境变量）")

g = {"__name__": "__main__", "__file__": os.path.abspath(script)}
sys.argv = [script]
sys.path.insert(0, os.path.dirname(os.path.abspath(script)))
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(script))))
exec(compile(new_src, script, "exec"), g)
