# -*- coding: utf-8 -*-
"""Loci 面板回归测试（纯 Python，无需浏览器）

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
    tmp = os.path.join(tempfile.gettempdir(), "loci_check.js")
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
#    ⚠️ 2026-10-11 更新。原来这行列的是**九页时代**的 id（记忆页 / 记忆包页 / 质检页
#    各自的元素），那几页在 上一版六页改版后已经不在导航里，于是这条断言一直红 ——
#    红了好几天没人发现（CI 也是红的）。这里改成按**当前六个页面**列出各自真正依赖的元素。
#    加元素时请连同它所属的页面一起写，别只堆一个清单。
need_ids = [
    # 框架
    # ⚠️ 2026-10-16：原来这里有 "page-title" —— 它挂在已删的记忆页 #v-mem 里
    #    （页码用 `.phead .ttl h2`，不是 #page-title）。随记忆页一起清掉。
    "navInd", "themetgl", "toast",
    # ① 会话（含从列表点进去的独立详情页）
    "v-session", "s-list", "s-q", "s-chips", "s-kpi",
    "v-sdetail", "sd-title", "sd-thread", "sd-meta",
    # ② 采集（含一键全选：表头框 + 会变字的按钮 + 选中条）
    "v-collect", "scan-list", "scan-info", "ck-all", "ck-allbtn", "ck-pickbar", "ck-sel-n",
    # ③ 交接卡
    "v-handoff", "hf-list", "hf-proj", "hf-q", "hf-tags", "hf-fmt", "hf-pickbar",
    # ④ 库（备份 / 导出 / 恢复 / 销毁）
    # ⚠️ 2026-10-16：原来还有 "cleanup-panel" / "cl-project" / "cl-agent" / "cl-before" /
    #    "cl-msg" —— 这 5 个都在已删的记忆页 #v-mem 里，随记忆页一起清掉。
    #    库页现在用 .card / .tbl / .sfr 这套（见 #v-clean 的 DOM）。
    "v-clean", "sf-list", "db-txt", "db-go", "bk-list",
    # ⑤ Agent
    "v-agents", "ag-list", "ex-path",
    # ⑥ 技能 / MCP
    "v-skill", "sk-list", "sk-out", "mcp-list",
]
missing = [i for i in need_ids if f'id="{i}"' not in P.PAGE]
check("页面关键元素齐全", not missing, f"缺失: {missing}")

# 4. onclick 调用的函数都已定义
onclicks = set(re.findall(r'onclick="([a-zA-Z_$][\w$]*)\(', P.PAGE))
defined = set(re.findall(r"function\s+([a-zA-Z_$][\w$]*)\s*\(", script))
undefined = sorted(onclicks - defined)
check("onclick 函数均已定义", not undefined, f"未定义: {undefined}")

# 5. 视图切换列表与 section 一致
#    ⚠️ 2026-10-11 改。原来断言 `navs == sections`，但 上一版改版后
#    mem / audit / pack 三个旧页面**导航里已经没有入口**（section 还留在 DOM 里）。
#    把断言删掉让它变绿，那不叫修好 —— 改成断言**真正的不变式**：
#      · 每个导航项都必须有对应 section（导航指向不存在的视图 = 真 bug，
#        这一条我 10-06 就踩过：误删 v-audit 的 section，切页就抛异常）
#      · 每个 section 要么是导航项，要么在下面这份**写出来的**例外清单里（不藏）
SECTION_NOT_IN_NAV = {
    "sdetail",               # 会话详情页：从会话列表点进去，按设计就没有导航项
    "mem", "audit", "pack",  # ⚠️ 遗留死页面：导航入口已删，section 还在 DOM 里没清
                             #    （清理排在后续版本。列在这里是为了不装作它不存在）
}
navs = set(re.findall(r'data-v="([a-z]+)"', P.PAGE))
sections = set(re.findall(r'id="v-([a-z]+)"', P.PAGE))
nav_without_section = sorted(navs - sections)
unregistered = sorted(sections - navs - SECTION_NOT_IN_NAV)
check("导航与视图区块一致", not nav_without_section and not unregistered,
      f"导航指向了不存在的视图={nav_without_section}；未登记的 section={unregistered}")

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
           "http", "urllib", "loci", "panel",
           # 2026-10-15 补一个：importlib 是标准库，之前白名单漏了它。
           # test_mcp.py 要用它动态导入 loci，测「面板按需启动」那几个开关。
           # ⚠️ 只补真正需要的 —— 一次塞一堆会把这个闸门削钝（它守的是"零第三方"）。
           "importlib")
third = []
for fn in ("loci.py", "panel.py", "install_agents.py", "test_mcp.py", "test_panel.py"):
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

# 9. 亮色主题的「加载时强制」没被删掉（2026-10-15 加）
#    ⚠️ 这条是为一次**真实事故**加的：
#       2026-10-13 清死代码时，我按「全仓只出现 1 次 = 没人引用 = 死代码」删掉了 initTheme
#       —— 但它是**立即执行函数**：
#           (function initTheme(){ applyTheme("light",false); })()
#       它的"调用"就是它自己，**数引用次数这种办法天生看不见它**。
#       删掉之后 `data-theme` 再没人设置 → 亮色覆盖整层失效 → body 走暗色 --bg、
#       内容走亮色 --lz-* → **顶部一条黑带 + 下面亮色**。
#       （用户 10-15 一眼看出我给的截图不对，才挖出来。）
#    这里不需要识别 IIFE —— 直接钉住"它干了什么"：applyTheme 必须至少有一次调用。
#    ⚠️ 第 1 版写错了：直接 `re.findall(r"applyTheme\s*\(", script)` 数出现次数 ——
#       反向测试（把调用注释掉）照样通过，因为**注释里也含有 `applyTheme(`**。
#       这就是本项目反复踩的「恒真断言」。所以先剥掉注释再数。
_no_comment = re.sub(r"/\*.*?\*/", "", script, flags=re.S)
_no_comment = re.sub(r"(?m)^\s*//[^\n]*", "", _no_comment)
_ap = re.findall(r"\bapplyTheme\s*\(", _no_comment)
check("亮色主题在加载时被强制应用（applyTheme 有调用点）",
      len(_ap) >= 2,
      f"applyTheme( 出现 {len(_ap)} 次（定义 1 次 + 加载时调用至少 1 次；注释不算）")

# ── 「打开面板就静默扫描本机会话」不许回来（2026-10-19 按用户拍板改）──────────
# 事故：一个空的 demo 库被面板一打开就静默灌进了本机的真实对话，
#       差点被拍进公开截图。它和「一打开 harness 面板就弹出来」是同一类错：
#       **自动替用户做了有后果的事**。现在改成先弹窗问一句。
# 判据一：load 处理器里不许直接调 runAutoScan，必须走 maybeAskScan。
_i = _no_comment.find('addEventListener("load"')
_j = _no_comment.find("\n});", _i) if _i >= 0 else -1
_load_seg = _no_comment[_i:_j + 4] if (_i >= 0 and _j > _i) else ""
check("打开面板不再静默扫描：load 里只调 maybeAskScan，不直接调 runAutoScan",
      ("maybeAskScan()" in _load_seg) and ("runAutoScan(" not in _load_seg),
      f"load 段长度 {len(_load_seg)}（为 0 表示找不到，按失败算）")

# 判据二：maybeAskScan 里 runAutoScan 必须被"用户选了扫描"这个条件包着
_k = _no_comment.find("async function maybeAskScan")
_e = _no_comment.find("\n}\n", _k) if _k >= 0 else -1
_ask_seg = _no_comment[_k:_e + 3] if (_k >= 0 and _e > _k) else ""
check("扫描必须由弹窗结果触发（if(pick!==\"cancel\") 之后才调 runAutoScan）",
      ('if(pick!=="cancel") runAutoScan(' in _ask_seg) and ("askDialog(" in _ask_seg),
      f"maybeAskScan 段长度 {len(_ask_seg)}")

print("=" * 56)
print("总体:", "全部通过" if not fails else f"有 {len(fails)} 项失败: {fails}")
sys.exit(1 if fails else 0)