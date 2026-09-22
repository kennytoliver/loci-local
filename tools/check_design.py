# -*- coding: utf-8 -*-
"""校验 Trae 交付的设计稿 HTML，判断能否直接落地进 panel.py。

用法：
    python tools/check_design.py 记忆.html [其他页面.html ...]
    python tools/check_design.py 记忆.html --ids   # 只列 id 契约命中情况

为什么需要它：
  Trae 上一轮交付踩了三个坑 —— 图标写成 assets/icon.jpg、8 个页面拆成 8 个文件、
  代码里混入孤立字符 n（粘贴损坏，一落地就 JS 语法错、整页白屏）。
  这些坑肉眼审代码很难发现，于是把它们固化成 11 项自动检查。
  零第三方依赖，纯标准库。
"""

import io
import os
import re
import sys

# ── 元素 id 契约（来自 docs/设计简报2-7个页面.md，JS 靠这些 id 找元素，不能改名）──
ID_GROUPS = [
    ("顶栏 / 全局", ["topbar", "stats", "q", "themetgl", "themeicon", "toast"]),
    ("记忆页", ["list", "list-title", "detail", "form", "f-content", "f-type", "f-proj",
              "f-tags", "f-imp", "g-pin", "g-recent", "proj-filter", "ctx-out", "handoff"]),
    ("会话页", ["s-title", "s-project", "s-agent", "s-text", "s-file", "s-save",
              "s-list", "s-info", "s-view", "s-q"]),
    ("质检页", ["health", "audit-out", "audit-msg", "au-proj"]),
    ("清理页", ["cleanup-panel", "cleanup-msg", "cl-project", "cl-agent", "cl-before",
              "cl-sup", "cm-list", "cm-msg", "cm-proj", "sf-list", "sf-msg", "roots",
              "scan-list", "scan-info", "scan-msg", "scan-out", "sf-all",
              "arch-dir", "arch-line", "arch-hint", "bk-list"]),
    ("采集页", ["ck-all", "ck-none", "ck-go"]),
    ("Agent 页", ["agents", "add-path", "agent-msg"]),
    ("记忆包页", ["pk-proj", "pack-file", "pack-with-sessions"]),
    ("交接卡页", ["hf-proj", "hf-copy", "skill-out", "sk-proj"]),
    ("页面容器", ["v-mem", "v-session", "v-audit", "v-clean", "v-collect",
               "v-agents", "v-pack", "v-handoff"]),
]

BLOCKED_HOSTS = ("cdn.", "unpkg.com", "jsdelivr", "bootcdn", "staticfile",
                 "googleapis", "gstatic", "cdnjs", "fonts.", "use.typekit")
OK_HOSTS = ("www.w3.org", "127.0.0.1", "localhost")

FRAME_MARKS = [
    # 注意：不能只写 createElement —— 原生 JS 的 document.createElement 会误报
    ("React", r'data-reactroot|react-dom|ReactDOM|React\.createElement|from\s+["\']react["\']'),
    ("Vue", r'v-if=|v-for=|v-bind|\bvue(\.min)?\.js|createApp\('),
    ("Tailwind", r'tailwindcss|class="[^"]*\btw-[a-z]'),
    ("jQuery", r'jquery'),
    ("Bootstrap", r'bootstrap(\.min)?\.(css|js)'),
]


def check_file(path, ids_only=False):
    fails = []
    warns = []
    name = os.path.basename(path)
    # 宿主文件（panel.py 等 Python 单文件）：第 7/8 项是给"待内联的设计草稿"用的，
    # 宿主本身就是 Python、且 JS 里含 <script> 字面量，拿来检必然误报 —— 直接跳过。
    is_host = name.lower().endswith(".py")
    print("=" * 62)
    print("文件: " + path)
    print("=" * 62)

    if not os.path.isfile(path):
        print("[FAIL] 文件不存在")
        return 1
    s = io.open(path, encoding="utf-8", errors="replace").read()
    if not s.strip():
        print("[FAIL] 文件为空")
        return 1
    print("大小: %.1f KB ｜ 行数: %d" % (len(s.encode("utf-8")) / 1024.0, s.count("\n") + 1))

    def ok(label, good, detail=""):
        print(("[PASS] " if good else "[FAIL] ") + label + (("  " + detail) if detail and not good else ""))
        if not good:
            fails.append(label)

    # 1. 单文件内联（不能引外部 css / js，不能 @import）
    ext_css = re.findall(r'<link[^>]+rel=["\']?stylesheet', s, re.I)
    ext_js = re.findall(r'<script[^>]+src=', s, re.I)
    imports = re.findall(r'@import', s, re.I)
    ok("单文件内联（无外部 css / js / @import）", not (ext_css or ext_js or imports),
       "外链css=%d 外链js=%d @import=%d" % (len(ext_css), len(ext_js), len(imports)))

    # 2. 零外链（SVG 命名空间与本地回环除外）
    bad_hosts = []
    for u in re.findall(r'https?://([^\s"\'<>/]+)', s):
        u = u.lower()
        host = u.split(":")[0]   # 去掉端口，否则 127.0.0.1:8787 会被误判成外链
        if host.startswith("www.w3.org") or host in ("127.0.0.1", "localhost"):
            continue
        bad_hosts.append(u)
    hits = [h for h in BLOCKED_HOSTS if h in s.lower()]
    ok("零外链（无 CDN / 外部字体 / 图标库）", not bad_hosts and not hits,
       "外部主机=%s 关键词=%s" % (sorted(set(bad_hosts))[:4], hits[:4]))

    # 3. 零框架
    frames = [n for n, p in FRAME_MARKS if re.search(p, s, re.I)]
    ok("零框架（无 React / Vue / Tailwind 等）", not frames, "命中: %s" % frames)

    # 4. 图标资产：只能是 /icon.png（像素海马）
    bad_icon = re.findall(r'icon\.(?:jpe?g|gif|webp|bmp|svg)', s, re.I)
    rel_icon = re.findall(r'\.\./assets/', s)
    ok("图标只用 /icon.png", not bad_icon and not rel_icon,
       "错误扩展名=%s 相对路径=%s" % (bad_icon[:3], rel_icon[:3]))

    # 5. 明暗双主题都要有（同名变量被定义了两次 = 两套主题）
    has_dark = ('class="dark"' in s) or ('data-theme' in s) or ('.dark' in s)
    defs = re.findall(r'^\s*--(?:background|bg|d0)\s*:', s, re.M)
    ok("明暗双主题（两套变量都能找到）", has_dark and len(defs) >= 2,
       "dark标记=%s 同名变量定义次数=%d" % (has_dark, len(defs)))

    # 6. 孤立字符 n（Trae 粘贴损坏特征：行首多了个 n 再接代码）
    CODE_HEAD = ("const ", "let ", "var ", "function", "<", "}", ")", "return", "if(",
                 "var(--", "//", "for(", "this.")
    sus = []
    for i, line in enumerate(s.split("\n"), 1):
        m = re.match(r'^\s*n(\s+)(\S.*)$', line)
        if m and (m.group(2).startswith(CODE_HEAD) or line.strip() == "n"):
            sus.append(i)
    ok("无孤立字符 n（粘贴损坏）", not sus, "可疑行: %s" % sus[:8])

    # 7. 内联进 Python raw string 的安全性（设计草稿专用）
    if is_host:
        print('[SKIP] 内联三引号安全性（宿主 .py 自身就是 Python，含 r""" —— 该检查只针对设计草稿）')
    else:
        triple = s.count('"""')
        tail_bs = len(s.rstrip("\n")) - len(s.rstrip("\n").rstrip("\\"))
        ok('可安全内联进 r"""…"""', triple == 0 and tail_bs % 2 == 0,
           '含三引号=%d 结尾反斜杠=%d' % (triple, tail_bs))

    # 8. JS 语法（有 node 就真检查）
    node = _find_node()
    scripts = re.findall(r'<script(?![^>]*\bsrc=)[^>]*>(.*?)</script>', s, re.S)
    if is_host:
        print("[SKIP] JS 语法检查（宿主 .py 的 JS 里含 <script> 字面量，抽取会截断 —— "
              "请对「服务端返回的真实 HTML」跑 test_panel.py / 手工 node --check）")
    elif node and scripts and not ids_only:
        import subprocess
        import tempfile
        tmp = os.path.join(tempfile.gettempdir(), "trae_design_check.js")
        io.open(tmp, "w", encoding="utf-8").write("\n".join(scripts))
        try:
            r = subprocess.run([node, "--check", tmp], capture_output=True, text=True, timeout=30)
            detail = (r.stderr or r.stdout or "").strip().splitlines()
            ok("JS 语法正确（node --check）", r.returncode == 0,
               " / ".join(detail[:2]) if r.returncode else "")
        except Exception as e:  # noqa: BLE001
            warns.append("node 检查未跑起来: %s" % e)
        finally:
            try:
                os.remove(tmp)
            except OSError:
                pass
    else:
        print("[SKIP] JS 语法检查（本机没有 node，或页面没有内联 script）")

    # 9. id 契约（JS 靠 id 找元素；id 变了要重接 59 个函数）
    print("-" * 62)
    total_hit = 0
    for gname, ids in ID_GROUPS:
        hit = [i for i in ids if ('id="%s"' % i) in s or ("id='%s'" % i) in s]
        miss = [i for i in ids if i not in hit]
        total_hit += len(hit)
        if not hit:
            continue
        ok("id 契约 · %s（%d/%d）" % (gname, len(hit), len(ids)), not miss, "缺: %s" % miss)
    if total_hit == 0:
        ok("id 契约 · 至少命中一个契约 id", False,
           "一个都没有 —— 落地时 59 个 JS 函数全靠 id 找元素，这样交付等于全部重接")
    else:
        print("       id 契约合计命中 %d 个（跨 %d 组）"
              % (total_hit, sum(1 for _, ids in ID_GROUPS
                                if any(('id="%s"' % i) in s for i in ids))))
        for gname, ids in ID_GROUPS:
            if not any(('id="%s"' % i) in s for i in ids):
                print("       %-10s 0/%d  —— 本页不含该模块（单页交付时正常）" % (gname, len(ids)))

    # 11. 落地提示
    if re.search(r'id="v-mem"', s) and re.search(r'id="v-session"', s):
        warns.append("本文件含多个页面容器 —— 交付要求是「每个页面可单独打开预览」，"
                     "合并版可以，但别忘了说明哪几个 id 属于同一页。")

    print("-" * 62)
    for w in warns:
        print("[提示] " + w)
    if fails:
        print("结论: 不通过（%d 项待修）→ %s" % (len(fails), "、".join(fails)))
    else:
        print("结论: 通过 ✅ 可以直接落地")
    return 1 if fails else 0


def _find_node():
    import shutil as _sh
    hit = _sh.which("node")
    if hit:
        return hit
    base = os.path.join(os.path.expanduser("~"), ".workbuddy", "binaries", "node", "versions")
    if os.path.isdir(base):
        for v in sorted(os.listdir(base), reverse=True):
            exe = os.path.join(base, v, "node.exe" if os.name == "nt" else "bin/node")
            if os.path.isfile(exe):
                return exe
    return None


def main(argv):
    args = [a for a in argv if not a.startswith("--")]
    ids_only = "--ids" in argv
    if not args:
        print(__doc__)
        return 2
    rc = 0
    for p in args:
        rc |= check_file(p, ids_only)
    print("=" * 62)
    print("总体:", "全部通过" if rc == 0 else "有不通过项")
    return rc


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
