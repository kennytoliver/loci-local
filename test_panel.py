# -*- coding: utf-8 -*-
"""Hippocampus 面板回归测试（纯 Python，无需浏览器）

专门防住一个曾把整个面板打瘫的坑：
  panel.py 里的 PAGE 变量若不是 raw string，Python 会把它内部的 \\n
  提前转义成真实换行，导致发到浏览器的 JS 出现跨行字符串字面量，
  浏览器报 "Invalid or unexpected token" 并丢弃整段 script —— 所有按钮全部失效。

同时校验页面关键元素与接口路由是否齐全。
"""
import io
import re
import sys
import os

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import panel as P  # noqa: E402

fails = []


def check(name, ok, detail=""):
    print(("[PASS] " if ok else "[FAIL] ") + name + (("  " + str(detail)) if detail and not ok else ""))
    if not ok:
        fails.append(name)


# 1. PAGE 必须是 raw string（源码层面检查，防止后人改回去）
src = open(os.path.join(HERE, "panel.py"), encoding="utf-8").read()
m = re.search(r'^PAGE = (\w*)"', src, re.M)
check("PAGE 声明为 raw string（r\"\"\"）", bool(m) and m.group(1).startswith("r"),
      "PAGE 不是 raw string，JS 里的 \\n 会被 Python 提前转义")

# 2. JS 语法必须正确（优先用 node 做真实语法检查；没有 node 时退回引号奇偶启发式）
script = re.search(r"<script>(.*?)</script>", P.PAGE, re.S).group(1)


def _find_node():
    import shutil as _sh
    for cand in ("node",):
        hit = _sh.which(cand)
        if hit:
            return hit
    base = os.path.join(os.path.expanduser("~"), ".workbuddy", "binaries", "node", "versions")
    if os.path.isdir(base):
        for v in sorted(os.listdir(base), reverse=True):
            exe = os.path.join(base, v, "node.exe" if os.name == "nt" else "bin/node")
            if os.path.isfile(exe):
                return exe
    return None


_node = _find_node()
if _node:
    import tempfile
    import subprocess
    tmp = os.path.join(tempfile.gettempdir(), "hippocampus_check.js")
    io.open(tmp, "w", encoding="utf-8").write(script)
    try:
        r = subprocess.run([_node, "--check", tmp], capture_output=True, text=True, timeout=30)
        detail = (r.stderr or r.stdout or "").strip().splitlines()
        check("JS 语法正确（node --check）", r.returncode == 0,
              " / ".join(detail[:2]) if r.returncode else "")
    except Exception as e:
        check("JS 语法正确（node --check）", False, str(e))
    finally:
        try:
            os.remove(tmp)
        except Exception:
            pass
else:
    bad_lines = []
    for i, line in enumerate(script.split("\n"), 1):
        code = re.sub(r"\\.", "x", line)
        code = re.sub(r"`[^`]*`", "``", code)
        if code.count('"') % 2 or code.count("'") % 2:
            bad_lines.append(i)
    check("JS 无跨行字符串字面量（启发式）", not bad_lines, f"可疑行: {bad_lines}")

# 3. 页面关键元素齐全
need_ids = ["stats", "list", "agents", "q", "scan-list", "scan-info", "add-path",
            "s-text", "s-title", "s-list", "s-view", "s-q", "v-session",
            "v-audit", "audit-out", "audit-msg", "ctx-out", "sk-proj", "skill-out",
            "cleanup-panel", "cleanup-msg", "cl-project", "cl-agent", "cl-before", "cl-sup",
            "v-clean", "sf-list", "sf-msg", "cm-list", "cm-msg", "bk-list", "cm-proj",
            "v-mem", "v-collect", "v-agents", "v-pack", "v-handoff",
            "proj-filter", "pk-proj", "hf-proj", "pack-file", "form"]
missing = [i for i in need_ids if f'id="{i}"' not in P.PAGE]
check("页面关键元素齐全", not missing, f"缺失: {missing}")

# 4. onclick 调用的函数都已定义
onclicks = set(re.findall(r'onclick="([a-zA-Z_$][\w$]*)\(', P.PAGE))
defined = set(re.findall(r"function\s+([a-zA-Z_$][\w$]*)\s*\(", script))
undefined = sorted(onclicks - defined)
check("onclick 函数均已定义", not undefined, f"未定义: {undefined}")

# 5. 视图切换列表与 section 一致
navs = set(re.findall(r'data-v="([a-z]+)"', P.PAGE))
sections = set(re.findall(r'id="v-([a-z]+)"', P.PAGE))
check("导航与视图区块一致", navs == sections, f"导航={sorted(navs)} 区块={sorted(sections)}")

# 6. 后端路由齐全
routes = ["/api/stats", "/api/list", "/api/search", "/api/handoff",
          "/api/agents", "/api/scan", "/api/pack/export", "/api/pack/import", "/api/collect",
          "/api/agent/verify", "/api/agent/register", "/api/agent/unregister",
          "/api/agent/register-all", "/api/agent/add", "/api/agent/forget",
          "/api/agent/pick-folder", "/api/session/list", "/api/session/get",
          "/api/session/search", "/api/session/parse", "/api/session/save",
          "/api/session/delete", "/api/pin", "/api/context", "/api/audit",
          "/api/extract", "/api/skill/preview", "/api/skill/export",
          "/api/supersede", "/api/merge", "/api/retire", "/api/touch",
          "/api/orphans", "/api/cleanup/preview", "/api/cleanup/run",
          "/api/cleanup/orphans", "/api/sourcefiles", "/api/sourcefiles/clean",
          "/api/backups"]
handler_src = src[src.index("class Handler"):]
missing_routes = [r for r in routes if f'"{r}"' not in handler_src]
check("后端路由齐全", not missing_routes, f"缺失: {missing_routes}")

# 7. 无第三方依赖（检查全部 .py 文件）
ALLOWED = ("sys", "os", "io", "json", "math", "re", "sqlite3", "argparse", "datetime",
           "hashlib", "threading", "webbrowser", "shutil", "subprocess",
           "http", "urllib", "hippocampus", "panel")
third = []
for fn in ("hippocampus.py", "panel.py", "install_agents.py", "test_mcp.py", "test_panel.py"):
    fp = os.path.join(HERE, fn)
    if not os.path.exists(fp):
        continue
    body = open(fp, encoding="utf-8").read()
    for i in re.findall(r"^(?:import|from)\s+([\w.]+)", body, re.M):
        if i.split(".")[0] not in ALLOWED:
            third.append(f"{fn}:{i}")
check("零第三方依赖（全部文件）", not third, f"第三方: {third}")

# 8. 安装器可用性与必需参数
inst = open(os.path.join(HERE, "install_agents.py"), encoding="utf-8").read()
for flag in ("--list", "--all", "--install", "--uninstall", "--verify", "--rules", "--uninstall-rules"):
    if flag not in inst:
        fails.append("安装器缺少参数 " + flag)
check("安装器命令行参数齐全", all(f in inst for f in
      ("--list", "--all", "--install", "--uninstall", "--verify", "--rules")))

print("=" * 56)
print("总体:", "全部通过" if not fails else f"有 {len(fails)} 项失败: {fails}")
sys.exit(1 if fails else 0)
