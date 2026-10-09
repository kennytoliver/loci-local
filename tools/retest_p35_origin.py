# -*- coding: utf-8 -*-
"""用 git 里的原始版本核实 P3-5 误报指控（不碰工作区）

只读 `git show :loci.py`（已暂存版本）与 HEAD 版本，检查当时 search_messages
是否真的存在"时间加权逻辑"。同时确认我当初测出的假成功在那个版本确实存在。
"""
import os
import re
import subprocess

os.chdir(r"D:\Loci")


def git_show(rev):
    out = subprocess.run(["git", "show", rev], capture_output=True)
    return out.stdout.decode("utf-8", "replace")


def func_body(src, name):
    m = re.search(r"^def %s\(.*?(?=^def |\Z)" % re.escape(name), src, re.S | re.M)
    return m.group(0) if m else ""


print("=" * 76)
print("一、P3-5 争议：search_messages 在原版本里有没有时间加权？")
print("=" * 76)
for rev in ("HEAD:loci.py", ":loci.py"):
    src = git_show(rev)
    if not src:
        print(f"[{rev}] 取不到内容")
        continue
    body = func_body(src, "search_messages")
    has_time = bool(re.search(r"created_at\[:10\]|updated_at\[:10\]|strptime|days", body))
    print(f"[{rev}]")
    print(f"  search_messages 函数长度: {len(body)} 字符")
    print(f"  含时间加权特征(created_at[:10] / strptime / days): {has_time}")
    # 列出该函数里所有乘法加权行，看是否有 0.02 * max(0, 30 - days) 这类时效项
    weights = [l.strip() for l in body.splitlines()
               if "*=" in l and ("days" in l or "1.0 +" in l)]
    print(f"  加权行: {weights if weights else '（无）'}")
    # 对照：search_memory 里的时效项
    sm = func_body(src, "search_memory")
    sm_time = [l.strip() for l in sm.splitlines() if "days" in l]
    print(f"  对照 search_memory 里的时效行: {sm_time}")
    print()

print("=" * 76)
print("二、我当初测出的假成功，在 HEAD 版本里确实存在吗？（确认我原始报告的依据）")
print("=" * 76)
head = git_show("HEAD:loci.py")
for fn in ("delete_memory", "set_pinned"):
    b = func_body(head, fn)
    print(f"[HEAD] {fn}:")
    print(f"  是否返回 rowcount: {'rowcount' in b}")
    upd = [l.strip() for l in b.splitlines() if "UPDATE" in l]
    print(f"  UPDATE 语句: {upd}")
    print()

print("=" * 76)
print("三、开发侧新加的校验：type 为空串时走哪条分支？")
print("=" * 76)
cur = open(r"D:\Loci\loci.py", encoding="utf-8").read()
m = re.search(r'if name == "memory_save":(.*?)(?=\n    if name ==)', cur, re.S)
if m:
    for line in m.group(1).splitlines()[:22]:
        print("   ", line)
