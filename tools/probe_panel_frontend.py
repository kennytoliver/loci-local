# -*- coding: utf-8 -*-
"""前端自检：提取面板 HTML 里的 onclick/onchange 引用，核对 JS 函数是否都有定义。

比 test_panel.py 更宽：不依赖它内置的白名单，直接把所有调用名抓出来对定义。
"""
import os
import re
import sys

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
src = open(os.path.join(HERE, "panel.py"), encoding="utf-8").read()

# PAGE 是 r"""...""" 的 raw string，取第一段最大的 HTML
m = re.search(r'^PAGE\s*=\s*r"""(.*?)^"""', src, re.S | re.M)
if not m:
    print("未能定位 PAGE 字符串")
    sys.exit(1)
page = m.group(1)
print(f"PAGE 长度: {len(page)} 字符")

# 提取 <script> 内容
scripts = re.findall(r"<script[^>]*>(.*?)</script>", page, re.S)
js = "\n".join(scripts)
print(f"内联 JS: {len(scripts)} 段 / {len(js)} 字符")

defined = set()
for pat in (r"function\s+([A-Za-z_$][\w$]*)\s*\(",
            r"(?:const|let|var)\s+([A-Za-z_$][\w$]*)\s*=\s*(?:async\s*)?(?:function|\()",
            r"window\.([A-Za-z_$][\w$]*)\s*=",
            r"([A-Za-z_$][\w$]*)\s*[:=]\s*(?:async\s*)?function"):
    defined |= set(re.findall(pat, js))

# onclick="fn(...)" / onclick='fn(...)' ；也含 onchange/oninput/onkeyup
handlers = re.findall(r'on(?:click|change|input|keyup|keydown|submit|blur|focus)\s*=\s*"([^"]+)"', page)
handlers += re.findall(r"on(?:click|change|input|keyup|keydown|submit|blur|focus)\s*=\s*'([^']+)'", page)
print(f"内联事件处理器: {len(handlers)} 个")

called = set()
for h in handlers:
    for name in re.findall(r"([A-Za-z_$][\w$]*)\s*\(", h):
        called.add(name)

BUILTINS = {"if", "for", "while", "return", "typeof", "catch", "switch", "function",
            "alert", "confirm", "prompt", "parseInt", "parseFloat", "String", "Number",
            "Boolean", "Array", "Object", "JSON", "Math", "Date", "encodeURIComponent",
            "decodeURIComponent", "setTimeout", "console", "event", "this", "isNaN",
            "fetch", "require", "Promise", "RegExp", "Error", "Set", "Map"}

undefined = sorted(n for n in called if n not in defined and n not in BUILTINS)
print()
if undefined:
    print("❌ 内联事件里调用了但未找到定义的函数:")
    for n in undefined:
        # 找出它出现的上下文
        for h in handlers:
            if re.search(r"\b" + re.escape(n) + r"\s*\(", h):
                print(f"   - {n}()   例: {h[:90]}")
                break
else:
    print("✅ 所有内联事件调用的函数都有定义")

# 检查 addr 属性里引用的函数
addr = re.findall(r'addEventListener\("(\w+)",\s*([A-Za-z_$][\w$]*)\)', js)
missing_addr = sorted({f for _, f in addr if f not in defined and f not in BUILTINS})
print()
print(f"addEventListener 绑定: {len(addr)} 处",
      "| 未定义: " + ", ".join(missing_addr) if missing_addr else "| 全部有定义")

# 检查 fetch 的 URL 与后端路由
urls = sorted(set(re.findall(r'["\'`](/api/[A-Za-z0-9_\-/]+)', js)))
print()
print(f"JS 里引用的 /api/ 端点: {len(urls)} 个")
routes = set(re.findall(r'u\.path == "([^"]+)"', src)) | set(re.findall(r'u\.path in \(([^)]+)\)', src))
flat = set()
for r in routes:
    for part in re.findall(r'"([^"]+)"', r):
        flat.add(part)
    if r.startswith("/"):
        flat.add(r)
missing = [u for u in urls if u not in flat]
if missing:
    print("❌ JS 引用但后端 do_GET/do_POST 里没有对应分支:")
    for u in missing:
        print("   -", u)
else:
    print("✅ 所有被引用的 /api/ 端点在后端都有分支")
