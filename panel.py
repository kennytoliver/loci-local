#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Loci Panel — macOS Vibrancy 风本地网页面板（零依赖单文件）
=================================================
- 复用 loci.py 引擎（同一目录 import），数据直连同一个 loci.db
- 纯 stdlib http.server，只监听 127.0.0.1，不暴露到局域网
- 功能：统计 / 记忆列表 / 中文检索 / 新增 / 删除 / 交接卡 / 记忆包 / Agent 体检
- 设计：三级深灰 + 侧边栏 vibrancy + Georgia 衬线标题（暗色原生）

用法:
  python panel.py            # 启动后手动打开 http://127.0.0.1:8787
  python panel.py --open     # 启动并自动打开浏览器
  python panel.py --port 9000
"""
import sys, os, json, argparse, threading, webbrowser, datetime, shutil, subprocess
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs, quote

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import loci as hippo

# ---------- Agent 注册表：检测 + 一键接入的唯一数据源 ----------
def _custom_path():
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), "agents.json")

# ⚠️ 2026-09-28：agents.json 读取失败时的警告暂存在这里，由 /api/stats 透出到面板
#    （前端 loadStats 见到 warn 字段就提示一次），不再只藏在 stderr 里、肉眼看不到。
_STORE_WARN = ""

def _load_store():
    """agents.json 统一读写：{"agents":[...], "scan_roots":[...]}
    兼容旧格式（纯 list）。

    ⚠️ 2026-09-28 修（外部审查 P0）：原来解析失败就 `return 空 dict`，而 save_custom()
    是"读-改-写"链 —— 于是「文件半截损坏」被当成「本来就是空的」，用户下次在面板做任何
    写操作，就把自己的 Agent 列表 / 扫描目录**覆盖成空**。
    这和 `_tk_python()` 少 `import re` 那次是同一个病：坏数据伪装成"环境就是空的"。
    现在解析失败先把原文件改名 `.corrupt-<时间戳>` 留证（不删），再返回空结构。
    """
    p = _custom_path()
    d = {"agents": [], "scan_roots": []}
    if not os.path.exists(p):
        return d
    try:
        data = json.loads(_file_text(p) or "{}")
    except Exception as e:
        global _STORE_WARN
        keep = p + ".corrupt-" + datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
        try:
            os.rename(p, keep)
            _STORE_WARN = ("agents.json 不是合法 JSON，原文件已留成 %s；"
                           "本次显示为空配置，请检查后重新保存" % os.path.basename(keep))
            print("[loci] agents.json 不是合法 JSON(%s)，原文件已留成 %s" % (e, keep),
                  file=sys.stderr)
        except Exception as e2:
            _STORE_WARN = "agents.json 不是合法 JSON，且备份失败；为避免覆盖，本次不写入配置"
            print("[loci] agents.json 不是合法 JSON(%s)，且备份失败(%s)；本次不覆盖它"
                  % (e, e2), file=sys.stderr)
        return d
    if isinstance(data, list):
        d["agents"] = data
    elif isinstance(data, dict):
        d["agents"] = data.get("agents") or []
        d["scan_roots"] = data.get("scan_roots") or []
    _STORE_WARN = ""      # 读取成功 → 解除之前的损坏警告
    return d

def _save_store(d):
    """原子落盘：先写同目录临时文件再 os.replace 覆盖。

    ⚠️ 2026-09-28 修：原来直接 open(p,"w") 写 —— 写一半崩了/断电就留下半截 JSON，
    下次 _load_store() 读成空 → 用户配置被清空。os.replace 在 Windows 上也是原子的。
    """
    p = _custom_path()
    tmp = p + ".tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(d, f, ensure_ascii=False, indent=2)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, p)
        return True
    except Exception:
        try:
            if os.path.exists(tmp):
                os.remove(tmp)
        except Exception:
            pass      # 容错：清理临时文件失败不影响结果（返回 False 已表达失败）
        return False
def load_custom():
    """用户手动添加的 Agent"""
    return _load_store()["agents"]

def save_custom(items):
    d = _load_store()
    d["agents"] = items
    return _save_store(d)

def load_scan_roots():
    """用户记住的「自定义扫描目录」——以后自动扫描会一并包含"""
    return _load_store()["scan_roots"]

def _scan_dir_for_configs(root, max_depth=3):
    """递归找目录下的 MCP 配置文件（深度限制，避免扫全盘）"""
    names = ("mcp.json", ".mcp.json", "mcp_config.json")
    out = []
    root = os.path.abspath(root)
    base = root.rstrip("\\/").count(os.sep)
    for dirpath, dirs, files in os.walk(root):
        if dirpath.count(os.sep) - base >= max_depth:
            dirs[:] = []
        for fn in files:
            if fn.lower() in names:
                out.append(os.path.join(dirpath, fn))
    return out

def add_scan_root(path):
    """记住一个扫描目录：立即扫描其中的 MCP 配置，以后每次自动扫描都包含它"""
    if not path or not path.strip():
        return {"error": "路径不能为空"}
    path = os.path.expandvars(os.path.expanduser(path.strip().strip('"')))
    if not os.path.isdir(path):
        return {"error": "这不是一个目录：%s" % path}
    d = _load_store()
    key = os.path.normcase(os.path.abspath(path))
    if any(os.path.normcase(os.path.abspath(r)) == key for r in d["scan_roots"]):
        return {"error": "这个目录已经记过了：%s" % path}
    d["scan_roots"].append(os.path.abspath(path))
    _save_store(d)
    found = _scan_dir_for_configs(path)
    res = add_agent(path)  # 复用现有逻辑：把找到的配置加入列表
    return {"ok": True, "root": path, "found": len(found), "added": res.get("added", []),
            "note": res.get("error", "")}

def forget_scan_root(path):
    d = _load_store()
    key = os.path.normcase(os.path.abspath(path))
    before = len(d["scan_roots"])
    d["scan_roots"] = [r for r in d["scan_roots"]
                       if os.path.normcase(os.path.abspath(r)) != key]
    _save_store(d)
    return {"ok": True, "removed": before - len(d["scan_roots"])}

def discover_agents():
    """指纹通配扫描：不依赖产品清单，按 VS Code 系通用目录模式发现 MCP 配置

    覆盖两类约定：
      1) %APPDATA%\\<产品名>\\User\\mcp.json   （VS Code / Cursor / Trae / Qoder ... 通用）
      2) ~\\.<名字>\\mcp.json 与 ~\\.<名字>\\.mcp.json
    """
    app = os.environ.get("APPDATA", "")
    home = os.path.expanduser("~")
    found = []
    seen = set()

    def _add(name, path):
        key = os.path.normcase(os.path.abspath(path))
        if key in seen:
            return
        seen.add(key)
        found.append({"name": name, "config": path})

    # 用户记住的自定义扫描目录（优先）
    for root in load_scan_roots():
        if os.path.isdir(root):
            for c in _scan_dir_for_configs(root):
                _add(_agent_name_from_config(c), c)
    if os.path.isdir(app):
        for d in sorted(os.listdir(app)):
            p = os.path.join(app, d, "User", "mcp.json")
            if os.path.isfile(p):
                _add(d, p)
    if os.path.isdir(home):
        for d in sorted(os.listdir(home)):
            if not d.startswith(".") or d in (".", ".."):
                continue
            base = os.path.join(home, d)
            if not os.path.isdir(base):
                continue
            for fn in ("mcp.json", ".mcp.json"):
                p = os.path.join(base, fn)
                if os.path.isfile(p):
                    _add(d.lstrip("."), p)
    return found

def find_configs_in_dir(root, depth=3):
    """在指定目录内浅层搜索 MCP 配置文件（手动指定路径用）"""
    hits = []
    root = os.path.abspath(root)
    if os.path.isfile(root):
        return [root]
    if not os.path.isdir(root):
        return []
    base_depth = root.rstrip("\\/").count(os.sep)
    for cur, dirs, files in os.walk(root):
        if cur.count(os.sep) - base_depth > depth:
            dirs[:] = []
            continue
        for fn in files:
            if fn.lower() in ("mcp.json", ".mcp.json"):
                hits.append(os.path.join(cur, fn))
    return hits

def _agent_name_from_config(path):
    """从配置路径推断产品名：X\\User\\mcp.json -> X；~/.foo/mcp.json -> foo"""
    parent = os.path.basename(os.path.dirname(os.path.abspath(path)))
    base = os.path.basename(os.path.abspath(path))
    if parent.lower() in ("user", "settings", "mcp", ""):
        parent = os.path.basename(os.path.dirname(os.path.dirname(os.path.abspath(path))))
    if not parent:
        parent = os.path.splitext(base)[0]
    return parent.lstrip(".")

def _known_configs():
    """当前注册表里所有允许写入的配置路径（写入前校验，防止越权改文件）"""
    out = set()
    for a in _agents():
        if a["write"]:
            for c in a["configs"]:
                out.add(os.path.normcase(os.path.abspath(c)))
    return out

def add_agent(path, name=""):
    """手动添加：接受配置文件或目录（目录会浅层搜索其中的 MCP 配置）"""
    if not path or not path.strip():
        return {"error": "路径不能为空"}
    path = os.path.expandvars(os.path.expanduser(path.strip().strip('"')))
    if not os.path.exists(path):
        return {"error": f"路径不存在：{path}"}
    confs = find_configs_in_dir(path)
    if not confs and os.path.isdir(path):
        confs = [os.path.join(path, "mcp.json")]
    if not confs:
        return {"error": f"没有在 {path} 里找到 mcp.json；也可以直接指定 mcp.json 文件本身"}
    items = load_custom()
    known = _known_configs()
    added = []
    for c in confs:
        key = os.path.normcase(os.path.abspath(c))
        if key in known:
            continue
        if any(os.path.normcase(os.path.abspath(i.get("config", ""))) == key for i in items):
            continue
        nm = name.strip() or _agent_name_from_config(c)
        items.append({"name": nm, "config": c})
        added.append({"name": nm, "config": c})
    save_custom(items)
    if not added:
        return {"error": "没有新增（这些路径已在列表里）", "found": confs}
    return {"ok": True, "added": added}

def forget_agent(path):
    """从自定义列表里移除（只忘记，不改目标配置文件）"""
    items = load_custom()
    key = os.path.normcase(os.path.abspath(path))
    keep = [i for i in items if os.path.normcase(os.path.abspath(i.get("config", ""))) != key]
    save_custom(keep)
    return {"ok": True, "removed": len(items) - len(keep)}

_TK_TRIED = []   # 最近一次 _tk_python() 试过哪些解释器 —— 失败时写进错误信息，便于定位


def _tk_python():
    """找一个自带 tkinter 的解释器（虚拟环境常缺 tkinter，回退到系统 Python）"""
    cands = [sys.executable]
    for name in ("python", "python3"):
        p = shutil.which(name)
        if p:
            cands.append(p)
    home = os.path.expanduser("~")
    local = os.environ.get("LOCALAPPDATA", "")
    # Windows 官方 py launcher（最通用，能列出版本与路径，避免猜盘符）
    # ⚠️ 2026-09-27 真 bug：panel.py **模块级没有 import re**（只在个别函数里局部 import），
    #    所以这里的 re.search 会抛 NameError → 被下面那个 except 静默吞掉 →
    #    py 的候选永远加不进 cands → 只剩虚拟环境那几个（都没 tkinter）→ 恒返回 None。
    #    后果：清理页「选择存放位置…」与 Agent 页「浏览…」全弹"没找到带图形界面的 Python"。
    #    教训：`except Exception: pass` 会把"写错代码"伪装成"环境不具备"。
    import re
    try:
        import subprocess as _sp
        out = _sp.run(["py", "-0p"], capture_output=True, text=True, timeout=8).stdout or ""
        for line in out.splitlines():
            m = re.search(r"([A-Za-z]:\\[^\s]+\.exe)", line)
            if m and m.group(1) not in cands:
                cands.append(m.group(1))
    except Exception:
        pass      # 容错：py -0p 输出格式不符就跳过该行，后面还有候选路径兜底
    cands += [os.path.join(local, "Programs", "Python", "Python313", "python.exe"),
              os.path.join(local, "Programs", "Python", "Python312", "python.exe"),
              os.path.join(home, "anaconda3", "python.exe"),
              os.path.join(home, "miniconda3", "python.exe")]
    seen = set()
    _TK_TRIED.clear()
    for c in cands:
        if not c or c in seen:
            continue
        seen.add(c)
        _TK_TRIED.append(c)
        if not os.path.exists(c):
            continue
        try:
            r = subprocess.run([c, "-c", "import tkinter"],
                               capture_output=True, timeout=25)
            if r.returncode == 0:
                return c
        except Exception:
            continue      # 容错：某个候选 Python 跑不起来/超时就试下一个
                          #   （当年 `import re` 缺失被吞，就是从这里伪装成"本机没有图形 Python"）
    return None

def pick_folder():
    """弹系统文件夹选择框：自动挑选一个带 tkinter 的解释器（子进程运行，避免与 HTTP 线程冲突）"""
    py = _tk_python()
    if not py:
        names = "、".join(os.path.basename(x) for x in _TK_TRIED[:5]) or "无"
        return {"error": "本机没找到带图形界面的 Python（已试 %d 个：%s）；"
                         "请直接把路径粘贴到输入框" % (len(_TK_TRIED), names)}
    code = ("import tkinter as tk\n"
            "from tkinter import filedialog\n"
            "r=tk.Tk(); r.withdraw()\n"
            "try: r.attributes('-topmost', True)\n"
            "except Exception: pass\n"
            "p=filedialog.askdirectory(title='选择 Agent 的数据/配置目录')\n"
            "print(p or '')\n")
    try:
        out = subprocess.run([py, "-c", code], capture_output=True,
                             text=True, timeout=300)
        lines = (out.stdout or "").strip().splitlines()
        p = lines[-1].strip() if lines else ""
        if not p:
            return {"error": "未选择目录（弹窗被关闭了）"}
        return {"ok": True, "path": p, "python": py}
    except Exception as e:
        return {"error": f"打开选择框失败（{e}）；请直接把路径粘贴到输入框"}

def open_folder(path):
    """在系统文件管理器里打开一个目录。

    修的是一个**一直在坏的真 bug**：前端用 POST 调 `/api/open-folder`，
    后端却只有 GET 分支，而且那个分支引用了 do_GET 里根本不存在的 `body`
    （Python 里这行一执行就 NameError）。实测：
        $ curl -X POST .../api/open-folder -d '{"path":"..."}'
        {"error":"not found"}
    也就是说面板上每个"打开文件夹"按钮都是死的，只是没人点。
    GET 和 POST 两个分支现在都走这里。
    """
    p = (path or "").strip()
    if not p or not os.path.isdir(p):
        return {"error": "目录不存在：%s" % p}
    try:
        if os.name == "nt":
            os.startfile(p)
        else:
            subprocess.Popen(["xdg-open", p] if os.name != "darwin" else ["open", p])
        return {"ok": True, "path": p}
    except Exception as e:
        return {"error": str(e)}

def _builtin_agents():
    """已知产品清单（数据目录为主判据）

    shape 说明：
      · "mcpServers"  —— 业界通用（Claude Code / Kimi / DeepSeek / VS Code 系…）
      · "mcp.servers" —— 智谱 ZCode 原生嵌套结构（zcode.z.ai 官方文档）
    """
    home = os.path.expanduser("~")
    app = os.environ.get("APPDATA", "")
    local = os.environ.get("LOCALAPPDATA", "")
    prog = os.environ.get("PROGRAMFILES", r"C:\Program Files")
    return [
        # ── 国际 ──
        {"name": "WorkBuddy", "write": True,
         "configs": [os.path.join(home, ".workbuddy", "mcp.json")],
         "datadirs": [os.path.join(home, ".workbuddy")],
         "exes": [os.path.join(local, "Programs", "WorkBuddy", "WorkBuddy.exe"),
                  os.path.join(prog, "WorkBuddy", "WorkBuddy.exe")]},
        {"name": "VS Code", "write": True,
         "configs": [os.path.join(app, "Code", "User", "mcp.json")],
         "datadirs": [os.path.join(app, "Code")],
         "exes": [os.path.join(local, "Programs", "Microsoft VS Code", "Code.exe"),
                  os.path.join(prog, "Microsoft VS Code", "Code.exe")]},
        {"name": "Cursor", "write": True,
         "configs": [os.path.join(home, ".cursor", "mcp.json"),
                     os.path.join(app, "Cursor", "User", "mcp.json")],
         "datadirs": [os.path.join(home, ".cursor"), os.path.join(app, "Cursor")],
         "exes": [os.path.join(local, "Programs", "cursor", "Cursor.exe")]},
        {"name": "Windsurf", "write": True,
         "configs": [os.path.join(home, ".codeium", "windsurf", "mcp_config.json"),
                     os.path.join(app, "Windsurf", "User", "mcp.json")],
         "datadirs": [os.path.join(home, ".codeium", "windsurf"),
                      os.path.join(app, "Windsurf")],
         "exes": [os.path.join(local, "Programs", "Windsurf", "Windsurf.exe")]},
        {"name": "Gemini CLI", "write": True,
         "configs": [os.path.join(home, ".gemini", "settings.json")],
         "datadirs": [os.path.join(home, ".gemini")], "exes": []},
        {"name": "Claude Code", "write": False,
         "configs": [os.path.join(home, ".claude.json")],
         "datadirs": [os.path.join(home, ".claude")], "exes": []},
        {"name": "Codex", "write": False,
         "configs": [os.path.join(home, ".codex", "config.toml")],
         "datadirs": [os.path.join(home, ".codex")], "exes": []},
        {"name": "GitHub Copilot CLI", "write": False,
         "configs": [os.path.join(home, ".copilot", "mcp-config.json")],
         "datadirs": [os.path.join(home, ".copilot")], "exes": []},
        # ── 国产 ──
        {"name": "ZCode", "write": True, "shape": "mcp.servers",
         "configs": [os.path.join(home, ".zcode", "cli", "config.json")],
         "datadirs": [os.path.join(home, ".zcode"), os.path.join(app, "ZCode")],
         "exes": [os.path.join(local, "Programs", "ZCode", "ZCode.exe"),
                  os.path.join(os.environ.get("ProgramFiles", ""), "ZCode", "ZCode.exe")]},
        {"name": "Kimi Code", "write": True,
         "configs": [os.path.join(home, ".kimi-code", "mcp.json"),
                     os.path.join(home, ".kimi", "mcp.json")],
         "datadirs": [os.path.join(home, ".kimi-code"), os.path.join(home, ".kimi")],
         "exes": []},
        {"name": "DeepSeek CLI", "write": True,
         "configs": [os.path.join(home, ".deepseek", "mcp.json"),
                     os.path.join(home, ".deepseek-cli", "mcp.json"),
                     os.path.join(home, ".deepseek-code", "claude.json")],
         "datadirs": [os.path.join(home, ".deepseek"), os.path.join(home, ".deepseek-cli"),
                      os.path.join(home, ".deepseek-code")], "exes": []},
        {"name": "TraeWork", "write": True,
         "configs": [os.path.join(app, "TRAE SOLO CN", "User", "mcp.json"),
                     os.path.join(app, "Trae CN", "User", "mcp.json"),
                     os.path.join(home, ".trae-cn", "mcp.json")],
         "datadirs": [os.path.join(app, "TRAE SOLO CN"), os.path.join(app, "Trae CN")],
         "exes": [os.path.join(local, "Programs", "TRAEWork", "TRAE SOLO CN.exe"),
                  os.path.join(local, "Programs", "Trae CN", "Trae CN.exe")]},
        {"name": "Trae", "write": True,
         "configs": [os.path.join(home, ".trae-cn", "mcp.json"),
                     os.path.join(app, "Trae", "User", "mcp.json")],
         "datadirs": [os.path.join(home, ".trae-cn")],
         "exes": [os.path.join(local, "Programs", "Trae", "Trae.exe")]},
        {"name": "Qoder", "write": True,
         "configs": [os.path.join(app, "Qoder", "User", "mcp.json")],
         "datadirs": [os.path.join(app, "Qoder")], "exes": []},
        {"name": "CodeBuddy", "write": True,
         "configs": [os.path.join(app, "CodeBuddy", "User", "mcp.json"),
                     os.path.join(home, ".codebuddy", "mcp.json")],
         "datadirs": [os.path.join(app, "CodeBuddy"), os.path.join(home, ".codebuddy")],
         "exes": []},
    ]

# ---------- 配置读写（支持两种结构：mcpServers / mcp.servers） ----------
def _shape_of(agent):
    return agent.get("shape", "mcpServers") if agent else "mcpServers"

def _servers_get(data, shape, create=False):
    """按结构取 servers 字典；create=True 时缺失则创建"""
    if shape == "mcp.servers":
        mcp = data.get("mcp")
        if mcp is None:
            if not create:
                return None
            mcp = data["mcp"] = {}
        if not isinstance(mcp, dict):
            raise ValueError("配置里的 mcp 字段不是对象")
        servers = mcp.get("servers")
        if servers is None:
            if not create:
                return None
            servers = mcp["servers"] = {}
    else:
        servers = data.get("mcpServers")
        if servers is None:
            if not create:
                return None
            servers = data["mcpServers"] = {}
    if not isinstance(servers, dict):
        raise ValueError("servers 字段不是对象")
    return servers

def _agents():
    """最终列表 = 已知清单 + 用户手动添加 + 指纹通配发现（按配置路径去重）"""
    out = [dict(a) for a in _builtin_agents()]
    used = {os.path.normcase(os.path.abspath(c))
            for a in out for c in a["configs"]}

    def _add_entry(name, config, source):
        key = os.path.normcase(os.path.abspath(config))
        if key in used:
            return
        used.add(key)
        out.append({"name": name, "write": True, "source": source,
                    "configs": [config], "datadirs": [os.path.dirname(config)],
                    "exes": []})

    for i in load_custom():
        if i.get("config"):
            _add_entry(i.get("name") or "自定义", i["config"], "manual")
    for d in discover_agents():
        _add_entry(d["name"], d["config"], "discovered")
    return out

def _agent_def(name):
    for a in _agents():
        if a["name"] == name:
            return a
    return None

def _dir_nonempty(d):
    """目录存在且有内容（空目录 = 卸载残留）"""
    try:
        with os.scandir(d) as it:
            for _ in it:
                return True
    except Exception:
        pass      # 容错：目录不可读 = 当作没有内容（存在性探测的安全默认）
    return False

# ---------- 本机 Agent 体检 ----------
def _entry_path_ok(text):
    """配置文本里那个 MCP server 的启动脚本，**文件真的存在吗**。

    为什么需要它：`"配置里有 loci 这个名字" != "真的能用"`。
    真实事故（2026-09-25 逮到）：某个 Agent 的配置一直指向一个**早已搬走的旧路径**
    —— 项目从 C 盘挪到 D 盘后配置就没更新过，
    名字在、路径早没了，可面板一直报「已接入」。用户以为记忆是通的，其实是死的。
    所以体检必须同时验**名字**和**路径**，否则就是假绿。
    """
    if not text:
        return False
    try:
        d = json.loads(text)
    except Exception:
        return False

    def walk(o):
        if isinstance(o, dict):
            return any(walk(v) for v in o.values())
        if isinstance(o, list):
            return any(walk(v) for v in o)
        if isinstance(o, str) and o.lower().endswith(".py"):
            return os.path.exists(o)
        return False

    return walk(d)


def scan_agents():
    """扫描本机 Agent：装了没 / 接入了没"""
    out = []
    for a in _agents():
        cfg_hit = next((c for c in a["configs"] if c and os.path.exists(c)), None)
        exe_hit = any(e and os.path.exists(e) for e in a["exes"])
        dir_live = any(d and _dir_nonempty(d) for d in a["datadirs"])
        # installed=有配置/程序/有内容的数据目录（说明装过并在用）
        # residue=只剩一个空目录（卸载残留） · absent=什么都没有
        if cfg_hit or exe_hit or dir_live:
            state = "installed"
        elif any(d and os.path.isdir(d) for d in a["datadirs"]):
            state = "residue"
        else:
            state = "absent"
        registered = False
        paths_ok = False
        if cfg_hit:
            try:
                # ⚠️ 兼容改名前的配置：老配置里服务名还是 "hippocampus"（也叫 hippohub / memhub）。
                #    这里必须**把旧名一起认**，否则升级后本机明明接着，面板却报「未接入」——
                #    用户会以为记忆断了，实际只是名字对不上。改名是 2026-09-25 做的。
                _t = _file_text(cfg_hit)
                registered = any('"%s"' % n in _t for n in ("loci", "hippocampus", "hippohub", "memhub"))
                # 名字对了还不够，启动脚本得真的在（见 _entry_path_ok 的注释：TraeWork 假绿事故）
                paths_ok = _entry_path_ok(_t) if registered else False
            except Exception:
                pass      # 容错：单个 Agent 配置解析失败 → 该条按"未接入"显示，不影响其他 Agent
        out.append({"name": a["name"], "state": state,
                    "installed": state == "installed",
                    "writable": a["write"],
                    "source": a.get("source", "builtin"),
                    "loci_registered": registered,
                    "paths_ok": paths_ok,
                    "config": cfg_hit or a["configs"][0]})
    out.sort(key=lambda x: x["name"].lower())   # 按名称 A-Z 排序
    return out

# ---------- 记忆包导出/导入 ----------
def export_backup_file(prefix="loci-backup"):
    """删除前强制备份：全量导出成一个带时间戳的 JSON 文件，返回路径"""
    pack = export_pack(None)
    pack["count"] = len(pack["memories"])
    d = os.path.dirname(os.path.abspath(__file__))
    p = os.path.join(d, "%s-%s.json" % (prefix, datetime.datetime.now().strftime("%Y%m%d-%H%M%S")))
    with open(p, "w", encoding="utf-8") as f:
        json.dump(pack, f, ensure_ascii=False, indent=2)
    return p

def cleanup_preview(project=None, agent=None, before=None, only_superseded=False, limit=20):
    """预览将要删除的记忆（不删）"""
    conn = hippo.db()
    q = "SELECT * FROM memories WHERE deleted=0"
    q += " AND superseded_by!=0" if only_superseded else " AND superseded_by=0"
    args = []
    if project:
        q += " AND project=?"; args.append(project)
    if agent:
        q += " AND agent=?"; args.append(agent)
    if before:
        q += " AND created_at<?"; args.append(str(before)[:10] + " 00:00:00")
    rows = [dict(r) for r in conn.execute(q, args)]
    conn.close()
    return {"count": len(rows), "items": rows[:limit],
            "by_agent": sorted({r["agent"] or "(空)" for r in rows}),
            "by_project": sorted({r["project"] or "(空)" for r in rows})}

# ---------- 源文件清理（记忆的源文件 → 备份 + 回收站） ----------
TRASH_ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "trash-backup")
MAX_BATCH = 10  # 每批最多处理的文件数（安全上限）

def scan_source_files():
    """扫描「记忆的源文件」：WorkBuddy 各工作区的日志 md，并统计各自关联多少条记忆"""
    home = os.path.expanduser("~")
    ws_root = os.path.join(home, "WorkBuddy")
    out = []
    if os.path.isdir(ws_root):
        for name in sorted(os.listdir(ws_root)):
            memdir = os.path.join(ws_root, name, ".workbuddy", "memory")
            if not os.path.isdir(memdir):
                continue
            for fn in sorted(os.listdir(memdir)):
                if not fn.lower().endswith(".md"):
                    continue
                p = os.path.join(memdir, fn)
                try:
                    st = os.stat(p)
                except OSError:
                    continue      # 容错：文件在遍历途中消失 → 跳过（展示用列表）
                out.append({"path": p, "size": st.st_size,
                            "mtime": datetime.datetime.fromtimestamp(st.st_mtime).strftime("%Y-%m-%d %H:%M"),
                            "agent": "WorkBuddy", "workspace": name, "kind": "工作区日志"})
    conn = hippo.db()
    for f in out:
        # 优先按「源文件精确匹配」；老数据（无 source_path）退回按工作区匹配
        rows = conn.execute(
            "SELECT id, content, source_path FROM memories WHERE deleted=0 AND source_path=?",
            (f["path"],)).fetchall()
        exact = len(rows) > 0
        if not exact:
            rows = conn.execute(
                "SELECT id, content, source_path FROM memories WHERE deleted=0 AND project=?",
                (f["workspace"],)).fetchall()
        f["memories"] = len(rows)
        f["exact"] = exact
        f["memory_items"] = [{"id": r["id"], "content": r["content"][:60]} for r in rows[:5]]
    conn.close()
    return out

def _to_recycle_bin(paths):
    """移到系统回收站（Windows 用 Shell API；其他平台退回移动到 .trash 目录）"""
    if not paths:
        return {"ok": True, "recycled": 0, "note": "无文件"}
    if os.name != "nt":
        d = os.path.join(TRASH_ROOT, ".trash")
        os.makedirs(d, exist_ok=True)
        ok = 0
        for p in paths:
            try:
                shutil.move(p, os.path.join(d, os.path.basename(p)))
                ok += 1
            except Exception as e:
                print("[loci] 移入 .trash 失败(%s)：%s" % (e, p), file=sys.stderr)
        # ⚠️ 2026-09-28：原来无条件 ok=True、recycled=len(paths) —— 搬失败也报"成功"。
        #    现在按真实成功数报，ok 只在全部成功时为真。
        return {"ok": ok == len(paths), "recycled": ok,
                "note": "非 Windows：已移入 .trash 目录"}
    import ctypes
    from ctypes import wintypes

    class SHFILEOPSTRUCTW(ctypes.Structure):
        _fields_ = [("hwnd", wintypes.HWND), ("wFunc", wintypes.UINT),
                    ("pFrom", wintypes.LPCWSTR), ("pTo", wintypes.LPCWSTR),
                    ("fFlags", ctypes.c_uint16), ("fAnyOperationsAborted", wintypes.BOOL),
                    ("hNameMappings", ctypes.c_void_p), ("lpszProgressTitle", wintypes.LPCWSTR)]

    FO_DELETE = 3
    FOF_ALLOWUNDO = 0x40          # 进回收站
    FOF_NOCONFIRMATION = 0x10
    FOF_SILENT = 0x4
    FOF_NOERRORUI = 0x400
    src = "\0".join(os.path.abspath(p) for p in paths) + "\0\0"
    op = SHFILEOPSTRUCTW(None, FO_DELETE, src, None,
                         FOF_ALLOWUNDO | FOF_NOCONFIRMATION | FOF_SILENT | FOF_NOERRORUI,
                         False, None, None)
    res = ctypes.windll.shell32.SHFileOperationW(ctypes.byref(op))
    return {"ok": res == 0, "recycled": len(paths) if res == 0 else 0,
            "aborted": bool(op.fAnyOperationsAborted), "code": res}

def _backup_files(paths, stamp):
    """删除前把文件复制到 trash-backup/<时间戳>/，返回 (备份目录, 备份成功的文件, 失败明细)

    注意：相对路径可能带 .. 从而逃出备份根目录（实测踩过），所以一律做逃逸检查，
    并逐个校验副本大小；只有确认备份成功的文件才允许删除。
    """
    root = os.path.join(TRASH_ROOT, stamp)
    home = os.path.expanduser("~")
    ready, failed = [], []
    for p in paths:
        ap = os.path.abspath(p)
        try:
            rel = os.path.relpath(ap, home)
        except ValueError:
            rel = ""
        if (not rel) or rel.startswith("..") or os.path.isabs(rel):
            rel = ap.replace(":", "_").replace("\\", "_").replace("/", "_")
        rel = rel.replace("..", "_")  # 兜底：任何残留的 .. 都替换掉
        dst = os.path.join(root, rel)
        try:
            os.makedirs(os.path.dirname(dst), exist_ok=True)
            shutil.copy2(ap, dst)
            if os.path.getsize(dst) != os.path.getsize(ap):
                raise IOError("备份副本大小与原文件不一致")
            ready.append(ap)
        except Exception as e:
            failed.append({"path": ap, "error": str(e)})
    return root, ready, failed

def cleanup_sourcefiles(paths, delete_memory_ids=None, want_backup=True):
    """源文件清理：备份（逐个校验）→ 移入回收站（分批≤10）→ 可选同时删除对应记忆

    want_backup=False 时跳过复制备份 —— 文件**仍然会进系统回收站**（那边能找回），
    只是不在 trash-backup/ 里再留一份。给"不想留额外副本"的用户一个选择（2026-10-09）。
    """
    paths = [p for p in (paths or []) if p and os.path.isfile(p)]
    if not paths:
        return {"error": "没有选中任何有效文件"}
    stamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
    if want_backup:
        backup_dir, ready, failed_backup = _backup_files(paths, stamp)
        if not ready:
            return {"error": "备份失败，已中止删除（未动任何文件）",
                    "backup_dir": backup_dir, "failed_backup": failed_backup}
    else:
        # 不备份：直接把全部文件当作"待处理"，也不做逐份校验
        backup_dir, ready, failed_backup = "", list(paths), []
    batches, failed = [], []
    for i in range(0, len(ready), MAX_BATCH):
        chunk = ready[i:i + MAX_BATCH]
        r = _to_recycle_bin(chunk)
        batches.append({"files": [os.path.basename(p) for p in chunk],
                        "count": len(chunk), "ok": r.get("ok"), "code": r.get("code")})
        if not r.get("ok"):
            failed.extend(chunk)
    deleted_memories = 0
    ids = [int(i) for i in (delete_memory_ids or [])]
    if ids:
        conn = hippo.db()
        conn.execute("DELETE FROM memories WHERE id IN (%s)" % ",".join("?" * len(ids)), ids)
        conn.commit()
        deleted_memories = len(ids)
        conn.close()
    return {"ok": not failed, "files": len(ready), "batches": batches,
            "backup_dir": backup_dir, "failed": failed,
            "skipped": [f["path"] for f in failed_backup], "failed_backup": failed_backup,
            "memories_deleted": deleted_memories}

def list_backups():
    if not os.path.isdir(TRASH_ROOT):
        return []
    out = []
    for name in sorted(os.listdir(TRASH_ROOT), reverse=True):
        d = os.path.join(TRASH_ROOT, name)
        if not os.path.isdir(d):
            continue
        n = size = 0
        for root, _, files in os.walk(d):
            for f in files:
                n += 1
                try:
                    size += os.path.getsize(os.path.join(root, f))
                except OSError:
                    pass      # 容错：单个文件取不到大小就跳过，目录体积是展示用统计
        out.append({"dir": d, "files": n, "size": size, "time": name})
    return out

PACK_FORMAT = "loci-pack"

def export_pack(project=None, include_sessions=False):
    """导出记忆包。默认只带「记忆层(结论)」；include_sessions=True 时连「会话层(对话原文)」一起导出。"""
    rows = hippo.list_memories(project=project, limit=100000)
    mems = [{"content": r["content"], "mtype": r["mtype"], "importance": r["importance"],
             "tags": r["tags"], "project": r["project"], "created_at": r["created_at"]}
            for r in reversed(list(rows))]
    pack = {"format": PACK_FORMAT, "version": 2, "exported_at": hippo.now(),
            "project": project or "", "count": len(mems), "memories": mems,
            "include_sessions": bool(include_sessions)}
    if include_sessions:
        conn = hippo.db()
        where = " WHERE project=?" if project else ""
        args = (project,) if project else ()
        sess = []
        for r in conn.execute("SELECT * FROM sessions" + where + " ORDER BY id", args):
            msgs = [{"role": m["role"], "content": m["content"], "created_at": m["created_at"]}
                    for m in conn.execute(
                        "SELECT role, content, created_at FROM messages "
                        "WHERE session_id=? ORDER BY turn, id", (r["id"],))]
            if not msgs:
                continue
            sess.append({"title": r["title"] or "", "project": r["project"] or "",
                         "agent": r["agent"] or "", "source_path": r["source_path"] or "",
                         "started_at": r["started_at"] or "", "ended_at": r["ended_at"] or "",
                         "messages": msgs})
        conn.close()
        pack["sessions"] = sess
        pack["session_count"] = len(sess)
        pack["message_count"] = sum(len(x["messages"]) for x in sess)
    return pack


def import_pack(pack):
    # ⚠️ 旧格式标识一起认：改名时 format 从 "hippocampus-pack" 换成 "loci-pack"，
    #    如果只认新名，用户手上**改名之前导出的记忆包就全部导不进来了**（数据在，却读不出）。
    if not isinstance(pack, dict) or pack.get("format") not in (PACK_FORMAT, "hippocampus-pack") \
            or not isinstance(pack.get("memories"), list):
        return {"error": "不是有效的 Loci 记忆包"}
    conn = hippo.db()
    existing = {r["content"] for r in conn.execute("SELECT content FROM memories")}
    conn.close()
    imported = skipped = 0
    _pack_skipped_reasons = []       # 2026-10-05：被排除清单拦下的原因，最后报给用户
    for m in pack["memories"]:
        c = (m.get("content") or "").strip()
        if not c or c in existing:
            skipped += 1
            continue
        # 2026-10-05 P0 修复：save_memory 现在会抛 MemorySkipped（排除清单命中）。
        #   原来没接住 → **一条垃圾废掉整包**（实测 10 条的包只进了 2 条）。
        #   正确做法：这一条跳过、计数，让包继续导完。
        try:
            hippo.save_memory(c, m.get("mtype", "fact"), m.get("importance", 2),
                              m.get("tags", ""), m.get("project", ""), "pack-import")
        except hippo.MemorySkipped as e:
            skipped += 1
            _pack_skipped_reasons.append(e.reason)
            continue
        existing.add(c)
        imported += 1
    # 会话层（对话原文）：save_session 内部按内容指纹去重，重复导入不会多出一份
    s_imp = s_skip = 0
    for _s in (pack.get("sessions") or []):
        msgs = _s.get("messages") or []
        if not msgs:
            continue
        try:
            _sid, created = hippo.save_session(title=_s.get("title", ""), project=_s.get("project", ""),
                                               agent=(_s.get("agent") or "") or "pack-import",
                                               messages=msgs, source_path=_s.get("source_path", ""),
                                               started_at=_s.get("started_at", ""),
                                               ended_at=_s.get("ended_at", ""))
            if created:
                s_imp += 1
            else:
                s_skip += 1
        except Exception:
            s_skip += 1
    out = {"imported": imported, "skipped": skipped}
    # 2026-10-05：把被排除清单拦下的条数和原因报出来。
    #   本项目规矩「静默跳过必须可审计」—— 只说"跳过 3 条"但不说为什么，
    #   用户会以为是重复，其实是被规则拦的。
    if _pack_skipped_reasons:
        agg = {}
        for r in _pack_skipped_reasons:
            agg[r] = agg.get(r, 0) + 1
        out["excluded"] = len(_pack_skipped_reasons)
        out["excluded_reasons"] = agg
    if pack.get("sessions"):
        out["sessions_imported"] = s_imp
        out["sessions_skipped"] = s_skip
    return out

# ---------- 采集中心：扫描历史日志 / skills → 勾选入库 ----------
def _preview(text, n=140):
    """提取文件前几句作为预览（跳过标题符号）"""
    parts = []
    for ln in text.splitlines():
        ln = ln.strip().lstrip("#>-* ").strip()
        if ln and not ln.startswith("!["):
            parts.append(ln)
        if len(" ".join(parts)) >= n:
            break
    return " ".join(parts)[:n]

def _file_text(p):
    with open(p, encoding="utf-8", errors="ignore") as f:
        return f.read()

def _classify(p):
    """根据路径判断来源类型和项目名"""
    low = p.lower()
    if "\\skills\\" in low or "/skills/" in low:
        return "skills", "skill"
    ws = "unknown"
    parts = p.replace("/", "\\").split("\\")
    for i, seg in enumerate(parts):
        if seg == ".workbuddy" and i > 0:
            ws = parts[i - 1]
            break
    return ws, "日志"

def _collect_roots():
    home = os.path.expanduser("~")
    return [os.path.join(home, "WorkBuddy"), os.path.join(home, ".workbuddy", "skills")]

def scan_sources():
    """扫描 WorkBuddy 各工作区 memory 日志 + skills，返回候选清单"""
    home = os.path.expanduser("~")
    out = []
    wb_root = os.path.join(home, "WorkBuddy")
    if os.path.isdir(wb_root):
        for ws in sorted(os.listdir(wb_root)):
            memdir = os.path.join(wb_root, ws, ".workbuddy", "memory")
            if not os.path.isdir(memdir):
                continue
            for fn in sorted(os.listdir(memdir)):
                if not fn.endswith(".md"):
                    continue
                p = os.path.join(memdir, fn)
                try:
                    text = _file_text(p)
                except Exception:
                    continue      # 容错：单个文件读不了就跳过，不拖垮整个采集源列表
                if not text.strip():
                    continue
                date = fn[:-3] if fn[:4].isdigit() else datetime.datetime.fromtimestamp(
                    os.path.getmtime(p)).strftime("%Y-%m-%d")
                out.append({"path": p, "kind": "日志", "source": ws, "date": date,
                            "title": fn, "preview": _preview(text), "size": len(text)})
    sk_root = os.path.join(home, ".workbuddy", "skills")
    if os.path.isdir(sk_root):
        for sk in sorted(os.listdir(sk_root)):
            p = os.path.join(sk_root, sk, "SKILL.md")
            if not os.path.exists(p):
                continue
            try:
                text = _file_text(p)
            except Exception:
                continue      # 容错：同上 —— 单个 SKILL.md 读不了就跳过
            if not text.strip():
                continue
            date = datetime.datetime.fromtimestamp(os.path.getmtime(p)).strftime("%Y-%m-%d")
            out.append({"path": p, "kind": "skill", "source": sk, "date": date,
                        "title": sk, "preview": _preview(text), "size": len(text)})
    # 标记已入库（按全文比对）
    conn = hippo.db()
    existing = {r["content"] for r in conn.execute("SELECT content FROM memories")}
    conn.close()
    for it in out:
        try:
            it["in_db"] = _file_text(it["path"]).strip() in existing
        except Exception:
            it["in_db"] = False   # 静默部分结果：读不出就当作"未入库"（偏保守，避免误报已导入）
    return out

def collect(paths):
    """把勾选的文件入库（路径白名单校验 + 内容去重）"""
    roots = [os.path.realpath(r) for r in _collect_roots()]
    conn = hippo.db()
    existing = {r["content"] for r in conn.execute("SELECT content FROM memories")}
    conn.close()
    imported, skipped, excluded, errors = 0, 0, 0, []
    for p in paths:
        try:
            rp = os.path.realpath(p)
            if not any(rp.startswith(r) for r in roots):
                errors.append(f"路径不在白名单: {p}")
                continue
            if not os.path.exists(rp):
                errors.append(f"文件不存在: {p}")
                continue
            text = _file_text(rp).strip()
            if not text or text in existing:
                skipped += 1
                continue
            proj, kind = _classify(rp)
            try:
                hippo.save_memory(text, "context", 2, f"采集,{kind}", proj, "collector", rp)
            except hippo.MemorySkipped:
                # 2026-10-05 P0 修复（复核发现的第三个遗漏调用点）：
                #   命中排除清单是**正常业务判定**（这个文件的内容没有可记忆信息，
                #   比如纯符号/工具输出残骸），不是采集失败。以前会被外层
                #   `except Exception` 记进 errors，让用户以为采集出错了、
                #   还看不到是哪一类被拦。单独接住，计入 excluded 而不是 errors。
                excluded += 1
                continue
            existing.add(text)
            imported += 1
        except Exception as e:
            errors.append(f"{p}: {e}")
    return {"imported": imported, "skipped": skipped,
            "excluded": excluded, "errors": errors}

# ⚠️ PAGE 必须是 raw string（r"""）。若去掉 r，Python 会把 JS 里的 \n 提前转义成
# 真实换行，导致发到浏览器的脚本出现跨行字符串 → 浏览器丢弃整段 JS → 所有按钮失效。
# 改动本块后请运行：python test_panel.py
# ---------- 一键接入：直接读写各 Agent 的 MCP 配置（写入前必做备份） ----------
def _script_paths():
    here = os.path.dirname(os.path.abspath(__file__))
    return os.path.join(here, "loci.py"), os.path.join(here, "loci.db")

def _server_entry():
    """生成写进各家配置的 MCP server 条目（路径动态取自当前运行环境，保证可移植）"""
    script, db = _script_paths()
    return {"command": sys.executable,
            "args": ["-X", "utf8", script],
            "env": {"LOCI_DB": db}}

def _json_targets():
    """支持一键写入的 Agent -> 候选配置文件（来自注册表，write=True 的才可写）"""
    return {a["name"]: a["configs"] for a in _agents() if a["write"]}

def _pick_target(cands):
    """优先选已存在的文件；否则选父目录存在的；再否则选第一个（会创建目录）"""
    for c in cands:
        if os.path.exists(c):
            return c
    for c in cands:
        if c and os.path.isdir(os.path.dirname(c)):
            return c
    return cands[0]

def _load_config(path):
    """读取 JSON 配置；文件不存在返回空 dict；不是合法 JSON 抛 ValueError（绝不覆盖）"""
    if not os.path.exists(path):
        return {}, None
    raw = _file_text(path).strip()
    if not raw:
        return {}, None
    try:
        data = json.loads(raw)
    except Exception as e:
        raise ValueError(f"目标配置不是合法 JSON（{e}），已放弃写入以免破坏")
    if not isinstance(data, dict):
        raise ValueError("目标配置顶层不是对象，已放弃写入")
    return data, raw

def _backup(path):
    if not os.path.exists(path):
        return None
    bak = path + ".bak-" + datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
    shutil.copy2(path, bak)
    return bak

def _write_config(path, data):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
        f.write("\n")

def _resolve_target(name, config=None):
    """定位要写入的配置文件：返回 (path, shape, error)"""
    if config:
        key = os.path.normcase(os.path.abspath(config))
        for a in _agents():
            if not a["write"]:
                continue
            for c in a["configs"]:
                if os.path.normcase(os.path.abspath(c)) == key:
                    return c, _shape_of(a), None
        return None, None, {"error": "该路径不在已识别的 Agent 列表里，拒绝写入"}
    targets = _json_targets()
    if name not in targets:
        return None, None, {"error": f"{name} 暂不支持自动写入（可参考 README 手动配置）"}
    return _pick_target(targets[name]), _shape_of(_agent_def(name)), None

def register_agent(name, config=None):
    """把 loci 写入指定 Agent 的 MCP 配置（备份 + 合并不覆盖）"""
    path, shape, err = _resolve_target(name, config)
    if err:
        return err
    try:
        data, _ = _load_config(path)
        servers = _servers_get(data, shape, create=True)
    except ValueError as e:
        return {"error": str(e), "path": path}
    bak = _backup(path)
    servers["loci"] = _server_entry()
    try:
        _write_config(path, data)
    except Exception as e:
        return {"error": f"写入失败：{e}", "path": path, "backup": bak}
    return {"ok": True, "path": path, "backup": bak, "created": bak is None, "shape": shape}

def unregister_agent(name, config=None):
    """移除指定 Agent 配置里的 loci 条目（备份 + 仅删自己那一项）"""
    path, shape, err = _resolve_target(name, config)
    if err:
        return err
    if not os.path.exists(path):
        return {"error": "配置文件不存在，无需移除", "path": path}
    try:
        data, _ = _load_config(path)
    except ValueError as e:
        return {"error": str(e), "path": path}
    try:
        servers = _servers_get(data, shape, create=False)
    except ValueError as e:
        return {"error": str(e), "path": path}
    if not servers or "loci" not in servers:
        return {"error": "该配置里没有 loci 条目", "path": path}
    bak = _backup(path)
    servers.pop("loci", None)
    try:
        _write_config(path, data)
    except Exception as e:
        return {"error": f"写入失败：{e}", "path": path, "backup": bak}
    return {"ok": True, "path": path, "backup": bak}

def verify_mcp(timeout=20):
    """真实握手验证：起 loci.py，走 initialize + tools/list，确认 MCP 可用"""
    script, db = _script_paths()
    env = dict(os.environ)
    env["LOCI_DB"] = db
    env["PYTHONIOENCODING"] = "utf-8"
    try:
        p = subprocess.Popen([sys.executable, "-X", "utf8", script],
                             stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                             stderr=subprocess.PIPE, env=env)
    except Exception as e:
        return {"ok": False, "error": f"无法启动 MCP 进程：{e}"}

    def send(obj):
        p.stdin.write((json.dumps(obj) + "\n").encode("utf-8"))
        p.stdin.flush()

    def recv():
        line = p.stdout.readline().decode("utf-8", errors="ignore").strip()
        return json.loads(line) if line else None

    try:
        send({"jsonrpc": "2.0", "id": 1, "method": "initialize",
              "params": {"protocolVersion": "2024-11-05", "capabilities": {},
                         "clientInfo": {"name": "loci-panel",
                                        "version": hippo.APP_VERSION}}})
        r = recv()
        name = (r or {}).get("result", {}).get("serverInfo", {}).get("name")
        send({"jsonrpc": "2.0", "method": "notifications/initialized"})
        send({"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
        r2 = recv()
        tools = [t["name"] for t in (r2 or {}).get("result", {}).get("tools", [])]
        ok = name == "loci" and len(tools) == 10
        return {"ok": ok, "server": name, "tools": tools,
                "python": sys.executable, "script": script,
                "error": None if ok else "握手返回不符合预期"}
    except Exception as e:
        return {"ok": False, "error": f"握手失败：{e}"}
    finally:
        try:
            p.stdin.close()
            p.terminate()
        except Exception:
            pass      # 容错：进程可能已自行退出，terminate 失败忽略


# ---------- 闲置自动退出 ----------
# 设计意图：面板是「用完即弃」的管理界面，不该常驻后台。
# 页面每 60 秒发一次心跳；页面关掉后心跳停止，超过 IDLE_EXIT_SEC 秒就自动退出，
# 用户不需要记得关，也不需要额外的开关程序。
_LAST_SEEN = [0.0]      # 最近一次活动（单调时钟）
IDLE_EXIT_SEC = 300     # 默认 5 分钟；设为 0 表示常驻不自动退出

# 面板实际绑定的端口。CSRF 守卫要用它来判断"请求是不是本面板自己发的"
# （见 Handler._origin_allowed）。main() 里赋值，未启动时为 None（此时一律放行）。
_BOUND_PORT = [None]


def _now_mono():
    import time as _t
    return _t.monotonic()


def _touch():
    _LAST_SEEN[0] = _now_mono()


def _watchdog():
    import time as _t
    _touch()
    while True:
        _t.sleep(20)
        if IDLE_EXIT_SEC <= 0:
            continue
        idle = _now_mono() - _LAST_SEEN[0]
        if idle > IDLE_EXIT_SEC:
            print("[Loci] 面板已闲置 %d 秒，自动退出（记忆数据不受影响）"
                  % int(idle), flush=True)
            os._exit(0)


def _start_watchdog():
    import threading as _th
    _th.Thread(target=_watchdog, daemon=True).start()


PAGE = r"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<link rel="icon" type="image/png" href="/icon.png">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Loci</title>
<style>
:root{
  /* 页面底从库值 #161616 压到 #0e0e0e：库的 #161616 对卡片 #1d1d1c 只有 1.073:1 面差，
     用户 2026-09-21 反馈「框跟背景底色融在一起，分不开」——框画出来了，但框里框外一个色。
     现在 1.144:1，与亮色的 1.141:1 手感对齐（低亮度区人眼分辨更差，所以暗色不能低于亮色）。 */
  --d0:#0e0e0e; --d1:#1d1d1c; --d2:#2e2e2e; --d3:#383838;
  /* R3 工作台的页面底色（2026-10-07）。
     顶栏 .toolbar 与 #v-session 都读 var(--r3-bg) —— 亮色下是 #faf9f7 暖白，
     暗色下没有这条会解析失败（整个声明作废 → 顶栏退回透明、透出后面的内容）。
     暗色沿用旧工作台的 --d0 #0e0e0e，用户未定暗色方案，先不动它。 */
  --r3-bg:#0e0e0e;
  /* 描边：库在暗色把 --border 定义成与卡片同色（#1d1d1c）、--input 定义成纯黑，
     结果就是"框看不见"（用户 2026-09-21 明确反馈，实测对比度只有 1.00:1）。
     这里按用户要求调亮：常规 1.44:1、控件 1.90:1（暗色要更高，低亮度下人眼分辨更差）。
     控件描边取库里 --sidebar-border 的暗色族（#404040）再提一档，不是凭空编的。 */
  --line:#383838; --line2:#4a4a4a;
  --ink:#eff1f4; --sub:#949494;
  /* --faint 提一档：卡片上 3.31:1 → 4.89:1（输入框占位符 2.66 → 3.93）。
     --sub 5.56:1 已合格，不动。 */
  --faint:#8a8a8a;
  --acc:#8ab4f8; --acc2:#2dccd3; --ok:#34a853; --warn:#fbbc05; --bad:#f1204a;
  /* 实心主按钮专用填充色：--acc 是「表面上的亮色」，拿它当填充面会出现
     白字压浅蓝（深色实测 2.11:1，不达标）。取色来自现有 --sidebar-primary。 */
  --acc-solid:#0065fd; --acc-solid-ink:#fff; --acc-solid-hover:#0052cc;
  /* FIX-5：主色当「文字」用时单独一条令牌 —— 亮色下 --acc 当文字只有 3.56:1
     （12px 文字需 4.5:1），暗色却有 8.01:1。暗色这里与 --acc 同值，观感零变化。 */
  --acc-ink:#8ab4f8;
  /* 数据可视化五色（库的 --chart-1..5，暗色刻意更"电"）——
     规范原话：图表是整个系统里颜色能量最强的地方，其余表面要保持安静 */
  --chart-1:#2dccd3; --chart-2:#f1204a; --chart-3:#edbbe8; --chart-4:#fbeb35; --chart-5:#baf6f0;
  --destructive:#ef4444; --destructive-foreground:#ffffff;
  --tracking-normal:0em;
  --chrome:rgba(22,22,22,.88); --toastbg:rgba(46,46,46,.96);
  /* FIX-10：原 .05 叠在卡片上只有 1.14:1，几乎看不见 → 提到 .07 */
  --hover:rgba(255,255,255,.07);
  /* FIX-9：破坏性动作 / 软边框 / 进度渐变都走令牌，别再硬编码 */
  --danger:#ff453a; --danger-soft:rgba(255,69,58,.5); --ok-soft:rgba(48,209,88,.5);
  --grad-progress:linear-gradient(90deg,var(--acc),var(--acc2));
  /* FIX-3：语义色 / 数据色令牌化 —— 这些 hex 原来硬编码在 JS 里，**只有暗色主题成立**：
     亮色下 #ffd60a 1.41:1、#30d158 2.02:1、#f9ab00 1.93:1、#09b6a2 2.55:1 全看不见；
     反过来 #5f6368 在暗色只有 2.79:1。（判据 3:1 —— 这些是 7px 圆点，属非文字图形）
     内联样式可以直接吃 CSS 变量，所以 JS 侧只换字符串，不用加取值逻辑。 */
  --data-fact:#0a84ff; --data-decision:#ffd60a; --data-preference:#bf5af2;
  --data-skill:#30d158; --data-error:#ff453a; --data-context:#8e8e93;
  --data-me:#0a84ff; --data-ai:#30d158;
  /* 记忆类型徽章 / 图标块的**底色**（2026-09-24 记忆页改版新增）。
     文字色直接复用上面的 --data-*，所以只需要底 ——
     亮暗两套各给一份，避免用 rgba 硬编码导致暗色下发灰。
     值都是"同色系、低饱和"的一档，保证徽章上的彩字仍有 4.5:1 左右对比。 */
  --t-fact-bg:#0d2b4d; --t-decision-bg:#3d3410; --t-preference-bg:#33204d;
  --t-skill-bg:#0e3520; --t-error-bg:#4a1d1a; --t-context-bg:#2b2b2e;
  --t-summary-bg:#1f2c3d;
  --data-agent-a:#0a84ff; --data-agent-b:#ea4335; --data-agent-c:#f9ab00;
  --data-agent-d:#30d158; --data-agent-e:#007acc; --data-agent-f:#5f6368;
  --data-agent-g:#09b6a2;
  /* 字体栈把库里的 DM Sans / JetBrains Mono 放在最前（没装就自动回落系统栈，零外链不能引 CDN） */
  --sans:"DM Sans",-apple-system,BlinkMacSystemFont,"Segoe UI","PingFang SC","Hiragino Sans GB","Microsoft YaHei",system-ui,sans-serif;
  /* 页面底色与卡片底色分开：浅色下两者同色就只剩 1px 描边能区分 */
  --bg:var(--d0); --card:var(--d1);

  /* ── 设计系统令牌（对齐 Trae 稿的命名，页面内联样式可直接用）── */
  --space-1:4px; --space-2:8px; --space-3:12px; --space-4:16px;
  --space-5:20px; --space-6:24px; --space-8:32px; --space-10:40px;
  --radius:8px;
  --radius-sm:4px; --radius-md:6px; --radius-lg:8px; --radius-xl:12px;
  --duration-fast:120ms; --duration-normal:200ms; --duration-slow:300ms;
  --ease-out:cubic-bezier(.16,1,.3,1); --ease-in-out:cubic-bezier(.45,0,.55,1);
  /* 阴影：库里 8 档全部定义为「透明度 0」。规范原话 —— 层次不靠模糊和抬升，
     靠色调、描边、分区和选择性对比。所以这里保持纯平：只留结构，不留视觉。 */
  --shadow-2xs:0 3px 0 0 rgba(0,0,0,0);
  --shadow-xs:0 3px 0 0 rgba(0,0,0,0);
  --shadow-sm:0 3px 0 0 rgba(0,0,0,0),0 1px 2px -1px rgba(0,0,0,0);
  --shadow:0 3px 0 0 rgba(0,0,0,0),0 2px 4px -1px rgba(0,0,0,0);
  --shadow-md:0 3px 0 0 rgba(0,0,0,0),0 2px 4px -1px rgba(0,0,0,0);
  --shadow-lg:0 3px 0 0 rgba(0,0,0,0),0 4px 6px -1px rgba(0,0,0,0);
  --shadow-xl:0 3px 0 0 rgba(0,0,0,0),0 8px 10px -1px rgba(0,0,0,0);
  --shadow-2xl:0 3px 0 0 rgba(0,0,0,0);
  /* 语义色别名（Trae 稿命名 → 本面板色板） */
  --background:var(--bg); --foreground:var(--ink); --card-foreground:var(--ink);
  --popover:var(--d2); --popover-foreground:#e5e5e5;
  --primary:var(--acc); --primary-foreground:#fff;
  --secondary:#383838; --secondary-foreground:#e5e5e5;
  --muted:var(--d3); --muted-foreground:var(--sub);
  /* 库把描边分成两个色：--border 常规描边 / --input 表单控件描边 */
  --border:var(--line); --input:var(--line2); --ring:var(--acc);
  --destructive:#ef4444; --destructive-foreground:#fff;
  --font-sans:var(--sans); --font-mono:"JetBrains Mono",ui-monospace,SFMono-Regular,Consolas,monospace;
}
/* ── 交互态令牌 ──
   亮色走蓝（库的 --accent:#dbeafe / #003e8f）。
   暗色：库给的是红（#4a2026 / #fc2c50），但用户 2026-09-21 明确说
   「夜间模式不希望用红色，颜色太不协调」→ 换成**库自带的蓝系**：
   底 #00266b、浅蓝字 #bfdbfe（这两个就是库的 --sidebar-accent / -accent-foreground），
   左强调条用亮蓝 #0065fd（库的 --sidebar-primary）。三个值都出自这个库，只是换了族。 */
:root{
  --accent:#00266b; --accent-foreground:#bfdbfe;
  --sel-bg:var(--accent); --sel-ink:#eff1f4; --sel-accent:#0065fd;
  --ring:#ffffff; --ring-soft:rgba(255,255,255,.18);
  --primary-hover:#a7c7fb;
  --sidebar:#171717; --sidebar-foreground:#e5e5e5;
  --sidebar-primary:#0065fd; --sidebar-primary-foreground:#fff;
  --sidebar-accent:#00266b; --sidebar-accent-foreground:#bfdbfe;
  --sidebar-border:#404040; --sidebar-ring:#00266b;
}
/* 白天模式 */
:root[data-theme="light"]{
  /* 页面底从库值 #ffffff 压到 #eef0f4 —— 这是**有意偏离库**，登记在设计规范 2.5。
     库的亮色把页面与卡片都定成 #ffffff（"白纸般的清爽"），层次只靠 #ebebeb 描边。
     问题是本项目 8 档阴影按库全部透明（2.1.1），于是只剩"一圈线"可用：
     面差实测 1.000:1，用户看到的就是「框很明显，但框里框外一个色，区分不出来」。
     现在卡片保持纯白浮起、页面退到浅灰，面差 1.141:1。
     阈值：面差 ≥1.06（低亮度区再高些），线差 ≥1.35 —— 两条都写进闸门。 */
  /* ⚠️ 2026-10-06 用户批评：「我选的是暖白底，你为什么做的跟最开始 Loci 面板一样的颜色」
     原来 --d0=#eef0f4（冷灰蓝），底下 9 个页面全部发冷。
     现在整条亮色底色系换成定稿原型 p5-final 的暖白：
       页面底 #faf9f7 · 卡片 #ffffff · 次级面 #f3f1ec · 第三层 #f0ede7
     ⚠️ 只动亮色主题，暗色（--d0:#0e0e0e 等）不动。
     ⚠️ 这属于用户定稿的配色，未经允许不得再改。 */
  --d0:#faf9f7; --d1:#ffffff; --d2:#f3f1ec; --d3:#f0ede7;
  /* 描边：库给的是 #ebebeb，实测对白底只有 1.19:1 —— 用户说"太不够明显"，
     所以两个主题一起提到"一眼能看清"的量级（对比度写进 audit_tokens.js 的断言）：
     常规描边 1.37:1、控件描边 1.57:1（暗色要更高，低亮度下人眼分辨更差） */
  --line:#e4e0d8; --line2:#d8d3c9;
  --ink:#1c1b19;
  /* 亮色弱化文字整体比深色弱一档（浅灰压在浅灰底上就没了），两档都提：
     --sub 3.38 → 6.81:1 ；--faint 2.36 → 4.68:1 */
  --sub:#857f74; --faint:#aaa49a;
  --acc:#4338ca; --acc2:#0f766e;
  --acc-solid:#4338ca; --acc-solid-ink:#fff; --acc-solid-hover:#372eb0;
  --acc-ink:#4338ca;   /* 靛蓝当文字 7.1:1 ✓ */
  /* 图表五色：亮色走"可识别的 Google 多彩"路线 */
  --chart-1:#4285f4; --chart-2:#ea4335; --chart-3:#fbbc05; --chart-4:#0043ad; --chart-5:#34a853;
  --destructive:#ef4444; --destructive-foreground:#ffffff;
  --chrome:rgba(255,255,255,.86); --toastbg:rgba(255,255,255,.98);
  --hover:rgba(14,17,21,.06);   /* FIX-10：原 .04 → 1.08:1，太弱 */
  --danger:#c5221f; --danger-soft:rgba(197,34,31,.45); --ok-soft:rgba(20,122,53,.45);
  /* FIX-3：亮色下的数据色 —— 全部按 3:1 挑过，替换掉那些"在亮色下消失"的 iOS 系统色 */
  --data-fact:#0b57d0; --data-decision:#8a5300; --data-preference:#7b2ff7;
  --data-skill:#147a35; --data-error:#c5221f; --data-context:#5f6368;
  --data-me:#0b57d0; --data-ai:#147a35;
  /* 记忆类型徽章底色（亮色版）：同色系的高明度一档，配 --data-* 的深色彩字 */
  --t-fact-bg:#e4eefc; --t-decision-bg:#f6efdd; --t-preference-bg:#f1e7fd;
  --t-skill-bg:#e2f4e6; --t-error-bg:#fbe6e4; --t-context-bg:#eceef0;
  --t-summary-bg:#e6edf6;
  --data-agent-a:#0b57d0; --data-agent-b:#c5221f; --data-agent-c:#8a5300;
  --data-agent-d:#147a35; --data-agent-e:#005a9e; --data-agent-f:#5f6368;
  --data-agent-g:#0f766e;
  /* 与暗色一致：8 档阴影全部透明（库的定义），纯平 */
  --shadow-2xs:0 3px 0 0 rgba(14,17,21,0);
  --shadow-xs:0 3px 0 0 rgba(14,17,21,0);
  --shadow-sm:0 3px 0 0 rgba(14,17,21,0),0 1px 2px -1px rgba(14,17,21,0);
  --shadow:0 3px 0 0 rgba(14,17,21,0),0 2px 4px -1px rgba(14,17,21,0);
  --shadow-md:0 3px 0 0 rgba(14,17,21,0),0 2px 4px -1px rgba(14,17,21,0);
  --shadow-lg:0 3px 0 0 rgba(14,17,21,0),0 4px 6px -1px rgba(14,17,21,0);
  --shadow-xl:0 3px 0 0 rgba(14,17,21,0),0 8px 10px -1px rgba(14,17,21,0);
  --shadow-2xl:0 3px 0 0 rgba(14,17,21,0);

  --ok:#34a853; --warn:#fbbc05; --bad:#ea4335;
  --secondary:#dbeafe; --secondary-foreground:#333942;
  --popover-foreground:#0e1115;

  /* 亮色交互态：蓝系（规范 --accent:#dbeafe / --accent-foreground:#003e8f） */
  --accent:#dbeafe; --accent-foreground:#003e8f;
  --sel-bg:var(--accent); --sel-ink:#003e8f; --sel-accent:var(--accent-foreground);
  --ring:#4285f4; --ring-soft:rgba(66,133,244,.16);
  --primary-hover:#1a73e8;
  --sidebar:#f0f6ff; --sidebar-foreground:#0e1115;
  --sidebar-primary:#4338ca; --sidebar-primary-foreground:#fff;
  --sidebar-accent:#dbeafe; --sidebar-accent-foreground:#003e8f;
  --sidebar-border:#e7eaef; --sidebar-ring:#4285f4;}

/* ══════════════════════════════════════════════════════════════
   新工作台专用变量组（2026-10-07）
   来源：7 页定稿原型 proto/*.html 的 :root 块，逐字抄出。
   ⚠️ 这组变量只服务新工作台页面（#v-session / #v-sdetail / #v-collect /
      #v-handoff / #v-library / #v-agent / #v-skill），跟上面的旧变量组并存。
      两组并存是有意的：新页面用新变量（不被旧工作台污染），
      旧页面暂时还挂着，等改完再清旧变量。
   ⚠️ 定稿数值，未经用户许可一个字不许改。校验：tools/verify-proto.py */
/* ⚠️ 2026-10-07 用户拍板：「不要暗主题了」。
   这一组从 `:root[data-theme="light"]` 提到 `:root`，**不受data-theme 影响**。
   原因：之前只在 light 下定义，用户系统是暗色 → 变量解析为空，
   v2 的 7 页样式全部失效（实测黑屏 + 布局散架）。
   数值一字未改，只是换挂载位置。 */
:root{
  --lz-bg:#faf9f7; --lz-surface:#ffffff; --lz-surface2:#f3f1ec; --lz-line:#e4e0d8;
  --lz-ink:#1c1b19; --lz-ink2:#4e4b45; --lz-sub:#857f74; --lz-faint:#aaa49a;
  --lz-indigo:#4338ca; --lz-indigo-bg:#eef0fd;
  --lz-coral:#e11d48; --lz-amber:#b45309; --lz-ok:#047857;
  --lz-teal:#0f766e; --lz-violet:#7c3aed;
  --lz-mono:"JetBrains Mono","SF Mono",Consolas,monospace;
  --lz-sans:"DM Sans",-apple-system,"PingFang SC","Microsoft YaHei",sans-serif;
  --lz-sh:0 2px 4px rgba(28,27,25,.04),0 8px 24px rgba(28,27,25,.07);
  --lz-sh-lg:0 4px 8px rgba(28,27,25,.05),0 16px 48px rgba(28,27,25,.11);}
/* 白底上需要单独调色的地方（黑底成立的配色在白底会失真） */
:root[data-theme="light"] .toast-item{box-shadow:0 10px 30px rgba(14,17,21,.16)}

*{margin:0;padding:0;box-sizing:border-box}
html,body{height:100%}
body{background:var(--bg);color:var(--ink);font-family:var(--sans);
  -webkit-font-smoothing:antialiased;overflow:hidden}
.app{display:flex;height:100vh}

/* ── 底部导航坞（2026-10-05 阶段二：顶部悬浮岛 → 底部居中胶囊坞）────────────
   ⚠️ 本轮只改外壳布局。show() 的切换逻辑读 .nav 的 data-v 和 #v-xxx 的 hidden，
      不读位置 —— 导航搬走不影响任何功能。
   ⚠️ 改任何类名/id 前先 grep tools/，那里的 10 个探针脚本引用了它们。

   为什么在底部（用户定稿，勿改）：会话点进去是「跳转独立页面」，不是下方展开一屏。
   底部导航不占内容宽度、也不挡住正在看的那一行原文。
   ⚠️ 9 项 → 6 项：记忆 / 记忆包 / 质检已砍（质检功能并进清理页）。 */
/* 底部坞（2026-10-06 用户批评：「从那个对话下来，它有一整个水泥块在这边，根本动不了」）
   ⚠️ 原来用 var(--sidebar) —— 那是**旧侧栏的冷灰**（#f1f3f4 / #202124 系），
      压在大面积暖白内容上就像一块水泥砖。
   现在改成：暖白半透明 + backdrop-filter 虚化底色 + 更轻的边和影。
      它依然 fixed 挡在底部（这是用户确认过的导航位置），但视觉上不再是"一块实体"。

   ⚠️ 尺寸 490×71 是用户定稿的，**这里一个字都不许改**
      （我曾一天内擅自改 4 次：width:92 / min-height:54 / padding:7 / padding-bottom:210）。 */
/* 底部胶囊坞 —— 完全照抄定稿原型 .dock（2026-10-06）。
   ⚠️ 我中间改过半透明 + backdrop-filter（那是"上个工作台"的磨砂风格，
      用户批评「像一整块水泥」）。**已按原型退回纯白 + --line 描边 + --sh-lg。**
   ⚠️ 尺寸 490×71 是用户定稿的，一个字不许改。
      .side 是全局类（底部坞），写新样式要带前缀，别拿 .main/.box 这些通用名。 */
/* 底部胶囊坞 —— 结构照抄定稿原型 `.dock`：
   6 个格子直接平铺在坞里，padding:7px，gap:3px，圆角 16px，bottom:22px。
   ⚠️ 2026-10-07：实机原本在 .side 里面多包了一层 <nav>（给 indicator 定位用），
      那层导致 6 个格子挤在 459px 里，坞只有 475。
      这里让 nav 自己撑满（display:contents 不可用，因为它要当 indicator 载体），
      所以改成：nav 本身继承 padding/gap，.side 只负责外观。
      实测对齐后：74×5 + 89 = 459，与原型 490 一致（含 padding 14 + border 2 + gap 15）。 */
.side{position:fixed;left:50%;bottom:22px;transform:translateX(-50%);z-index:120;
  background:var(--d1);color:var(--ink);
  border:1px solid var(--line);border-radius:16px;
  box-shadow:0 4px 8px rgba(28,27,25,.05),0 16px 48px rgba(28,27,25,.11);
  padding:7px;max-width:calc(100vw - 40px)}
.side > nav{display:flex;align-items:center;gap:3px}
/* 页脚（版本号）随 Logo 一起移到工具栏；红黄绿是 Mac 窗口装饰，没有功能，直接隐藏。 */
.lights{display:none}
.sfoot{display:none}
/* 品牌标搬进工具栏：按新设计稿做成横排的极简文字标（LOCI + 忆宫），
   不再是旧侧栏那个"蓝方块 + 图标 + 两行文字"的竖排品牌块。
   ⚠️ 2026-10-06：原来右边有统计条 + 全局搜索框，现在移走了 →
      padding-right / border-right 就成了"右边空一块还竖条线"。
      详情页 .sbrand 后面还跟着面包屑，那条线有用；工具栏里这条要收掉。 */
.sbrand{display:flex;align-items:baseline;gap:7px;flex:none}
/* ⚠️ 2026-10-07 删掉 `#v-sdetail .sbrand{border-right}` 这条。
   详情页现在**不再有自己的顶栏**（全局 .toolbar 一直在），
   面包屑紧跟在全局品牌标后面 —— 这条描边会在两行里各画一次，
   截屏实测是两行 LOCI 忆宫各带一条竖线。原型 .brand 后面没有任何分隔线。 */
.sbtext{display:flex;align-items:baseline;gap:7px;min-width:0}
.slogo{display:none}
.sfoot{display:none}
.sbrand h1{font-family:var(--mono);font-size:14px;font-weight:700;letter-spacing:.14em;
  color:var(--acc);line-height:1}
.sbrand p{font-size:13px;color:var(--sub);letter-spacing:.06em;line-height:1}
/* ⚠️ padding 必须是 0，不要给它留 indicator 的空间。
   之前为防"底块外扩被 overflow 裁圆角"写了 6~7px，把坞从原型的 71px
   撑到 83px —— 那是用户没批准过的尺寸改动。
   方案 A（用户拍板）下 --nav-scale-active=1，底块与格子等大，本就不需要空间。
   若将来要恢复放大，必须先问用户，不能自己加内边距。 */
nav{display:flex;align-items:center;overflow-x:auto;overflow-y:hidden;padding:0}
/* 底部坞：图标在上、文字在下（竖排）。
   ⚠️ 尺寸严格照抄定稿原型 p3/p4/p5（三份原型实测都是 490×71，用户已确认）：
      `min-width:74px` + `padding:9px 17px 8px`，**不加 min-height**。
      2026-10-06 教训：我先后擅自加过 width:92px（坞变 583）和 min-height:54px
      + nav padding（坞变 83），都被用户否掉。原型的 71px 高是内容自然撑出来的，
      多余的 min-height / 内边距只会把坞撑大。
      ⚠️ 尺寸属于用户定稿范围，未经允许不得改动。 */
.nav{display:flex;flex-direction:column;align-items:center;gap:3px;position:relative;
  min-width:74px;padding:9px 17px 8px;
  border-radius:12px;color:var(--sub);
  cursor:pointer;text-decoration:none;white-space:nowrap;flex-shrink:0;
  transition:background var(--duration-fast) var(--ease-out),
             color var(--duration-fast) var(--ease-out)}
/* 文字规则照抄原型 `.dock .d .nm{font-size:10.5px;white-space:nowrap}`。
   ⚠️ 坞宽 490×71 是原型实测值。
   ⚠️ 2026-10-07 追查：差17px 的原因找到了 ——
      全站 --sans（第 1245 行）在 PingFang SC 前面塞了 BlinkMacSystemFont 和
      "Segoe UI"，这两个拉丁字体把「技能 / MCP」的中文抢去渲染了，
      单字宽被压窄 → 该格 87px 而不是 89px。
      实测：原型字体栈（无 BlinkMacSystemFont / Segoe UI）量出来正好 55px = 89px。
      这里只给导航文字用原型字体栈，不动全局 --sans（旧页面还在用）。 */
.nav span{font-size:10.5px;white-space:nowrap;line-height:1.45;
  font-family:"DM Sans",-apple-system,"PingFang SC","Microsoft YaHei",sans-serif}
/* ⚠️ 2026-10-07：字号跟定稿原型对齐。
   原型 `.dock .d` 继承 body 的 13px，`.d .nm` 才降到 10.5px ——
   也就是说「技能 / MCP」那格的文字**实际按 13px 排**，宽度 89px，
   6 项加起来 490px。
   旧代码给 .nav 本体就写了 font-size:10.5px，那格只有 87px，整条坞变 473px。
   这里把规则搬回原型的写法：本体不锁字号（继承 13px），由内部文字元素降到 10.5px。
   ⚠️ 这不是改坞的尺寸 —— min-width / padding / grid 一律没动，
      是让「文字宽度」回到原型那样，490 是结果不是参数。 */
/* ⚠️ 这里原来有 `.nav:hover{background:var(--sidebar-accent)...}` —— 淡蓝底。
   2026-10-06 用户要求取消悬停底色（"改了很多版都达不到效果"），已删。
   悬停只保留文字变深，不出现任何背景。选中底色由 indicator 负责。 */
.nav.on{background:transparent;color:var(--sidebar-primary-foreground);
  font-weight:600}
.nav svg.ico{width:20px;height:20px;flex:none;stroke-width:1.7}
/* 角标：待办数量（会话待质检数 / 采集未入库数 / 清理条数） */
.nav .bdg{position:absolute;top:2px;right:5px;font-family:var(--mono);font-size:8.5px;
  font-weight:700;background:var(--danger);color:#fff;padding:0 3.5px;border-radius:7px;
  line-height:1.5;border:1.5px solid var(--sidebar);pointer-events:none}
.nav.on .bdg{background:var(--sidebar-primary-foreground);color:var(--sidebar-primary);
  border-color:var(--sidebar-primary)}

/* ── 导航微动效（2026-10-05 增量层，零依赖 Vanilla）─────────────────────
   ⚠️ 只在导航坞内做加法，不改 class 语义、不改配色体系、不改业务逻辑。
      show() 仍读 data-v + #v-xxx 的 hidden，动效层只旁听。
   调参入口全在这里，末尾有说明。 */
:root{
  /* 时长与手感：按 oil-ui/references/motion.md 的表定。
     · 选中指示 / 按下 / 开关：150–300ms
     · 整页或视图切换：350–500ms
     · 按下立即生效约 80ms，松开再弹回
     ⚠️ 2026-10-05 用户实测反馈"太生硬"：6 项间距只有 ~78px，
        220ms 滑过去人眼只读出"变色"读不出"滑"。位移取区间上沿 300ms，
        缓动用 Material emphasized decelerate（长尾缓出，尾巴拖得住）。 */
  --nav-dur-slide:300ms; --nav-dur-size:260ms;
  --nav-dur-hover:240ms; --nav-dur-press:90ms; --nav-dur-release:320ms;
  --nav-dur-enter:320ms; --nav-dur-pop:280ms; --nav-dur-ripple:420ms;
  --nav-ease-slide:cubic-bezier(.05,.7,.1,1);
  --nav-ease-size:cubic-bezier(.4,0,.2,1);
  --nav-ease-spring:cubic-bezier(.34,1.56,.64,1);
  /* 幅度
     ⚠️ --nav-scale-active = 1 —— 方案 A（2026-10-06 用户拍板）。
        底块不放大，尺寸与导航项格子完全一致，于是：
        · 不需要给 nav 加内边距防裁剪 → 坞能保持原型的 71px
        · 圆角不会被 overflow 削平
        放大带来的"浮起感"用户暂时不要（说等有更准确的形容或工具再加回来）。
        ⚠️ 这个值同时被 indicator 和 hover 底色用作外扩系数，改它等于改尺寸，
           属于用户定稿范围，未经允许不得改。 */
  --nav-scale-active:1;
  --nav-shift-px:0px;          /* 方案 A：不上浮 */
  --nav-scale-press:.96;       /* 按下缩到 0.96（只保留这一处形变） */
  --nav-enter-y:8px;           /* 内容区进场起点 */
  --nav-ind-radius:var(--radius-xl);
  --nav-ripple-on:1;         /* 置 0 一键关闭 ripple */
  /* 悬停底色已整块删除（2026-10-06 用户要求：「改了很多版都达不到我满意的效果，
     把它取消。有动效，然后点当前页面的时候会有蓝色的背景就够了」）。
     .nav::before 那层和它用的 --nav-hov-* 变量、sizeOf() 都已从代码里删除，
     不是靠 alpha:0 藏着 —— 留一条 opacity:0 的规则在文件里，以后容易被人误打开。
     ⚠️ 若将来要恢复，先问用户。 */
}
nav{position:relative}
/* indicator：脱离文档流、absolute 定位的选中态载体，作为选中态唯一视觉载体。
   底色沿用现有 --sidebar-primary，视觉等于原来的 .nav.on 底色，
   只是从"跟着项跳"变成"自己滑过去"。

   ⚠️ 这里刻意「不用 scale + 固定基准盒」，而是外层走 transform 位移、
      内层直接过渡 width/height。两个原因：
      1) nav 有 overflow-y:hidden。基准盒若做到 100px 高，会先被父容器裁掉，
         只剩底部一条露出来（2026-10-05 实测踩过）。
      2) 位移与尺寸写进同一个 transform 时，尺寸变化会被位移时长一起拖长，
         切到窄项时底块会"拉丝"，这正是"生硬"的另一半原因。
      小尺寸指示块直接过渡宽高没有重排代价（它是 absolute + 自身合成层）。 */
.nav-ind{position:absolute;left:0;top:0;width:0;height:0;
  pointer-events:none;opacity:0;z-index:0;
  will-change:transform;
  transform:translate3d(0,0,0);
  transition:transform var(--nav-dur-slide) var(--nav-ease-slide),
             opacity var(--nav-dur-size) var(--nav-ease-size)}
.nav-ind::before{content:"";position:absolute;
  left:var(--nav-ind-x,0);top:var(--nav-ind-y,0);
  width:var(--nav-ind-w,0);height:var(--nav-ind-h,0);
  border-radius:var(--nav-ind-radius);
  background:var(--sidebar-primary);
  /* 轻投影：滑动时能读出"这是一块独立实体在滑"，而不是"背景色变了"。
     纯 paint 属性，不参与合成。 */
  box-shadow:0 2px 8px -2px rgba(0,0,0,.22);
  transition:left var(--nav-dur-size) var(--nav-ease-size),
             top var(--nav-dur-size) var(--nav-ease-size),
             width var(--nav-dur-size) var(--nav-ease-size),
             height var(--nav-dur-size) var(--nav-ease-size)}


.nav-ind.ready{opacity:1}
/* 首帧定位：跳过渡，避免页面一加载 indicator 从左上角飞进来 */
.nav-ind.init{transition:none!important}
/* 选中项不再自带底色，底色交给 indicator（否则两层叠加，颜色会变深） */
/* ⚠️ user-select:none 是必须的（2026-10-07 用户报bug：「长按技能/MCP 会拽出拉拽条」）。
   实测：.nav 上只有 touch-action:manipulation，**没有 user-select**，
   长按会选中文本 —— 出现蓝色高亮 + 拖拽手柄，在胶囊坞里尤其难看，
   而且「技能 / MCP」字多，最容易被选中。
   原型 .dock .d 靠浏览器默认同样没禁，但原型是静态页没人长按；
   真面板是可点的，必须禁掉。touch-action:manipulation 挡不住选中，它只管滚动/缩放。 */
.nav{position:relative;z-index:1;touch-action:manipulation;
  -webkit-user-select:none;user-select:none;
  -webkit-touch-callout:none;
  transition:color var(--nav-dur-hover) var(--nav-ease-size),
             transform var(--nav-dur-release) var(--nav-ease-spring)}
/* 悬停：不出现任何灰色/淡蓝背景（2026-10-06 用户要求取消 ——
   「改了很多版，都达不到我满意的效果，把它取消。有动效，
   然后点当前页面的时候会有蓝色的背景就够了」）。
   所以 ::before 那层灰底整块删掉了，不是靠 alpha:0 藏着 ——
   留一条 opacity:0 的规则在文件里，以后容易被人误打开。
   现在悬停唯一的反馈是文字变深。 */
.nav:hover{color:var(--sidebar-foreground)}

/* 选中态：只切颜色，不做放大/上浮/阴影。
   ⚠️ 2026-10-06 用户拍板方案 A：不要"浮起感"，只要"有动效 + 蓝色背景"。
      之前加的 scale(1.12) / translateY / drop-shadow 全部撤掉。 */
.nav .ico,.nav span{display:block;
  transform:translateY(0) scale(1);
  transition:color var(--nav-dur-slide) var(--nav-ease-slide)}
.nav.on .ico,.nav.on span{
  transition:color var(--nav-dur-slide) var(--nav-ease-slide)}

.nav.press{transform:scale(var(--nav-scale-press))!important;
  transition:transform var(--nav-dur-press) var(--nav-ease-size)!important}
/* ripple-lite：JS 只负责按坐标插一个 span，动画全在 CSS，结束即移除 */
.nav .rip{position:absolute;left:var(--rx);top:var(--ry);
  width:var(--rd);height:var(--rd);margin:calc(var(--rd) / -2) 0 0 calc(var(--rd) / -2);
  border-radius:50%;background:var(--sidebar-primary-foreground);
  opacity:0;pointer-events:none;transform:scale(0);
  animation:navRip var(--nav-dur-ripple) var(--nav-ease-size) forwards}
@keyframes navRip{0%{opacity:.5;transform:scale(0)}
  60%{opacity:.28}100%{opacity:0;transform:scale(1)}}
/* 未读小圆点：只 pop 一次，不做无限呼吸 —— 工作台场景里持续动会干扰读数 */
.nav .dot{position:absolute;top:4px;left:50%;margin-left:-3px;
  width:6px;height:6px;border-radius:50%;background:var(--danger);
  border:1.5px solid var(--sidebar);z-index:2;pointer-events:none;
  animation:navPop var(--nav-dur-pop) var(--nav-ease-spring) both}
@keyframes navPop{from{transform:scale(0)}to{transform:scale(1)}}
/* 焦点移动不触发 indicator 位移（JS 里只在"确认选中"时 move），
   这里只给一圈可见描边 */
.nav:focus-visible{outline:2px solid var(--sidebar-primary);outline-offset:2px}
/* 内容区进场：与 indicator 同一时间轴起点（JS 里同一帧触发）。
   ⚠️ 选择器必须带上 section[id^="v-"]，否则优先级输给下面 2132 行那条
      已有的 viewIn 进场动画（属性选择器+元素 = 0-1-1 > 单一 class 0-1-0）。 */
section[id^="v-"].nav-enter,
.nav-enter{animation:navIn var(--nav-dur-enter) var(--nav-ease-slide) both}

@keyframes navIn{from{opacity:.6;transform:translateY(var(--nav-enter-y))}
  to{opacity:1;transform:none}}
/* 降级：关掉位移/缩放/涟漪，只留颜色变化。
   ⚠️ 必须把页面自带的 viewIn（2132 行 section[id^="v-"]）一起关掉 ——
   那是 section 的默认进场动画，不关的话 reduced-motion 下内容区照样在动。 */
@media (prefers-reduced-motion:reduce){
  .nav-ind,.nav,.nav.on,.nav .ico,.nav.on span,.nav .rip,.nav .dot,
  .nav-enter,section[id^="v-"]{animation:none!important;
    transition-duration:.01ms!important;transition-delay:0ms!important}
  .nav-ind{transition:none!important}
  .nav.on,.nav.on .ico,.nav.on span,.nav.press{transform:none!important}
  .nav .rip{display:none}
}

/* ── 主区 ───────────────────────── */
/* 侧栏 2026-10-05 改底部胶囊坞后，.main{flex:1} 自动撑满整宽。
   padding-bottom 给底部导航坞留出高度（坞约 71px + bottom:22px ≈ 93px，
   留 176px 让最后一行内容与坞之间还有呼吸，不贴住 —— 用户 2026-10-05 明确要求）。
   ⚠️ 量过真实坐标：dock.top − lastRow.bottom = 58px（改前）/ 拉开后更大。
      不要再只看这个数值就断言"不挡"，要量 getBoundingClientRect。 */
.main{flex:1;display:flex;flex-direction:column;min-width:0;
  /* 底部留白移到 .content 里了（那里才是真正的滚动容器）——
     这里再留一份就是双层间距，滚到底最后一行还是露不出来。
     见上方 .content 的注释。 */
  padding-bottom:0}
/* 工具栏（2026-10-06 用户批评「跟最开始那个对话的颜色一样」）
   ⚠️ 原来用 var(--chrome) + var(--line)，那是冷灰半透明 → 顶部一道灰条。
      现在改成暖白半透明 + 暖灰边线，与会话页/详情页同一套。
   ⚠️ tb-spacer 是我加的占位（原来这里塞了 stats + 全局搜索框），
      把品牌推到左、动作推到右。原来用 .stats 的 margin-left:auto 顶。 */
/* 工具栏 = 顶栏（2026-10-06）。
   ⚠️ 高度按定稿原型 .top 的 **76px**（不是我原来的 padding 14px 算出来的 ~60px）。
      padding 改成 0 22px + height:76px，靠 align-items:center 居中，
      这样高度与原型完全一致，不靠 padding 凑。
   ⚠️ 底色：原型是纯 --surface 白 + --line 描边（无边框下方的分隔线）。
      我之前用半透明 + backdrop-filter（那是旧工作台的"磨砂顶栏"），
      属于「上个工作台页面」的残留设计语言，已清掉。 */
/* 顶栏 = 原型的 .top（2026-10-07 照 proto/*.html 改）
   ⚠️ 用户拍板：删掉底部分隔线 + 底色改暖白。
      原型 `.top{height:76px;display:flex;align-items:center;gap:13px;padding:0 4px}`
      —— **没有 background、没有 border-bottom**，它就是页面底色本身。
      原来写 `background:var(--d1)`（纯白 #ffffff）+ 一条 --line 横线，
      跟下面 #faf9f7 的页面底色差一档，屏幕上就是"顶上一条白条 + 一根线"。
   ⚠️ padding 是 0 22px 不是 0 4px：panel.py 的 .toolbar 在 .content 之外，
      而 .content 自己有 20px 横向内边距，两者相加才等于原型的 20+4。
      改这里前先量 `.content` 的 padding —— 别把 22 改成 4，那会整体左移 18px。 */
/* ⚠️ padding 0 24px（2026-10-07 实测修正）。原型是 .app{padding:0 20px}+.top{padding:0 4px}=24。
      原来写 22px，注释理由「.content 有20px 内边距，两者相加」是错的——.content 与 .toolbar
      是**兄弟节点不是父子**（实测 .content left=0）。实测 brandLeft=22 正好等于本 padding。 */
.toolbar{display:flex;gap:13px;align-items:center;height:76px;padding:0 24px;
  background:var(--r3-bg);
  border-bottom:none;position:relative;z-index:2}
.tb-spacer{flex:1;min-width:0}
.toolbar input{flex:1;max-width:560px;background:var(--d2);border:1px solid var(--line2);
  border-radius:var(--radius-md);padding:7px 13px;font-size:13px;color:var(--ink);
  outline:none;font-family:var(--sans)}
.toolbar input::placeholder{color:var(--faint)}
.toolbar input:focus{border-color:var(--line2)}
/* 页面正文容器。
   ⚠ 类名与列表项内部的 `.mem .content` **撞名**，这条规则会顺带命中它，
   把每个列表项多撑 68px（上 padding 20 + 下 48）——
   实测：插件行内容只有 2 行 = 49px，量出来却是 117px，卡片看着空一大截。
   两边单独看都正常，所以这种"顺带命中"极难发现。
   在 `.mem .content` 里显式压回（那条选择器特异性 0,2,0 > 这里的 0,1,0）。 */
/* ⚠️ 2026-10-06 用户指出：「导航栏下面等于封了一个模块，封死了下面这一行」
   根因：`.content` 是独立滚动容器（overflow-y:auto），而 .main 上的
   padding-bottom:176px 加在**滚动区外面** —— 内容滚到底时，最后一行
   永远被这 176px 的死区压在下面，滚不出来。
   **底部留白必须加在滚动容器内。**
   ⚠️ 2026-10-07 数值改成 176：原型 proto/*.html 的
      `.app{padding:0 20px 176px}` —— 176 = 坞 71 + bottom 22 + 呼吸 83。
      原来写 140（坞 70+22+48），是上一轮自己算的，跟原型不一致。
      这是照抄原型，不是自作主张调尺寸。 */
.content{flex:1;overflow-y:auto;padding:0 20px 176px}
/* .main 原来的 padding-bottom:176px 属于重复留白（双层间距），
   现在统一由 .content 管，.main 归零。 */
/* 内容区一律铺满：原先 860px 的阅读宽度上限会在宽屏下留出大片空白 */
section{max-width:none}
/* 纯文本页留一点最大宽度，避免一行过长（但远大于原 860） */
section#v-handoff,section#v-pack{max-width:1400px}

/* ── 标题与文字 ──────────────────── */
h2{font-family:var(--sans);font-size:20px;font-weight:600;letter-spacing:-.2px;margin-bottom:4px}
.lead{font-size:13px;color:var(--sub);margin-bottom:20px;line-height:1.6}
/* 正文里指路的行内链接（如记忆页 → 会话页） */
.lead .leadlink{color:var(--acc);cursor:pointer;border-bottom:1px dashed var(--acc)}
.lead .leadlink:hover{color:var(--ink)}

/* ── 卡片与按钮 ──────────────────── */
.panel{background:var(--card);border:1px solid var(--line);border-radius:var(--radius-lg);padding:16px 20px;margin-bottom:12px;box-shadow:var(--shadow-2xs)}
.btn{border:1px solid var(--line2);background:var(--card);color:var(--ink);
  border-radius:var(--radius-md);padding:7px 14px;font-size:13px;font-weight:500;
  line-height:1.4;cursor:pointer;font-family:var(--sans);
  transition:background var(--duration-fast) var(--ease-out),
             border-color var(--duration-fast) var(--ease-out),
             color var(--duration-fast) var(--ease-out)}
.btn:hover{background:var(--d3);border-color:var(--acc)}
/* .pri = 该页面的主行动（保存 / 归档 / 生成 / 导出…），实心主色 */
.btn.pri{background:var(--acc-solid);border-color:var(--acc-solid);color:var(--acc-solid-ink)}
.btn.pri:hover{background:var(--acc-solid-hover);border-color:var(--acc-solid-hover);color:var(--acc-solid-ink)}
.btn:disabled{opacity:.5;cursor:wait}
.btn.txt{color:var(--acc);border:none;background:none;padding:8px 4px}
.btn.txt:hover{text-decoration:underline;background:none;border:none}

/* ── 统计 ───────────────────────── */
.stats{display:flex;align-items:center;gap:24px;margin:0;margin-right:auto;min-width:0;overflow:hidden}
.stat{background:none;border:none;padding:0;min-width:0;display:flex;flex-direction:column;gap:1px}
.stat .v{font-family:var(--sans);font-size:18px;font-weight:700;line-height:1.2;
  letter-spacing:-.2px;font-variant-numeric:tabular-nums}
.stat .k{font-size:10.5px;color:var(--sub);letter-spacing:.2px;white-space:nowrap}

/* ── 记忆卡片 ────────────────────── */
.mem{background:var(--card);border:1px solid var(--line);border-radius:var(--radius-lg);padding:14px 18px;margin-bottom:10px;box-shadow:var(--shadow-2xs)}
.mem .top{display:flex;align-items:center;gap:12px;margin-bottom:8px;flex-wrap:wrap}
.ttag{display:flex;align-items:center;gap:6px;font-size:11px;color:var(--sub)}
.ttag i{width:7px;height:7px;border-radius:50%;display:inline-block}
.proj{font-size:11px;color:var(--sub);border:1px solid var(--line);
  border-radius:6px;padding:2px 8px}
/* 输出里的强调小字（关联 X 条 / 体积 / 类型 / 轮次）。
   注意：这个类原来叫 .pri，与主行动按钮的 `.btn.pri` 撞名 ——
   `.pri{font-size:11px}` 写在 `.btn{font-size:13px}` 之后，同权重后者胜，
   把所有主按钮的字号压成了 11px。改名 .accent 彻底断开。 */
.accent{font-size:11px;color:var(--warn)}
.score{font-size:11px;color:var(--acc)}
/* ⚠ padding/overflow/flex 必须显式压回来 —— 上方页面级 `.content` 那条
   全局规则（flex:1 + padding:20px 20px 48px）会顺带命中列表项内部的
   `.content`，把每一项虚增 68px。实测证据：插件行内容 2 行 = 49px，实高 117px。
   这里**只压本机内容页**（`#sk-list`）。全站修的影响面（实测）：
     会话 151.5→83.5 ／ 清理 183→115 ／ 质检 639.3→548.5 ／ 本机内容 298.5→206
   那几页是用户已签字的排版，换个 pass 再动，别顺手改（见设计规范 2.5 已知偏差）。 */
#sk-list .mem .content{font-size:14px;line-height:1.75;padding:0;overflow:visible;flex:none}
/* 同 P0-3：记忆列表里已作废的条目也别整块半透明 */
.mem.indb{opacity:1}
.mem.indb .content,.mem.indb .meta{color:var(--faint)}
/* 复选框用更饱和的蓝，暗色下那条极细的原生框才看得见 */
.ck{width:14px;height:14px;accent-color:var(--sidebar-primary);cursor:pointer;flex-shrink:0}
.mem .meta{display:flex;justify-content:space-between;align-items:center;
  margin-top:10px;padding-top:9px;border-top:1px solid var(--line);
  font-size:11px;color:var(--faint)}
.del{border:none;background:none;color:var(--faint);cursor:pointer;font-size:11px}
.del:hover{color:var(--bad)}

/* ── Agent 体检 ──────────────────── */
/* 卡片网格：自适应列宽（15 款产品，窄窗口自动降列） */
.dot{width:8px;height:8px;border-radius:50%;display:inline-block;flex-shrink:0}
.dot.on{background:var(--ok)}.dot.off{background:var(--faint)}.dot.warn{background:var(--warn)}
.mini{border:1px solid var(--line2);background:var(--card);color:var(--ink);
  border-radius:var(--radius-md);padding:5px 12px;font-size:12px;cursor:pointer;
  font-family:var(--sans);transition:background var(--duration-fast) var(--ease-out)}
.mini:hover{background:var(--d3);border-color:var(--acc)}
.mini.warn{color:var(--bad)}
.msg{background:var(--d2);border:1px solid var(--line);border-radius:var(--radius-md);
  padding:12px 16px;font-size:12.5px;line-height:1.75;margin-bottom:14px;white-space:pre-wrap}
.msg.ok{border-color:rgba(52,168,83,.45)}
.msg.err{border-color:var(--danger-soft)}
.textin{flex:1;min-width:240px;background:var(--d2);border:1px solid var(--line2);
  border-radius:var(--radius-md);padding:7px 13px;font-size:13px;color:var(--ink);outline:none;
  font-family:var(--sans)}
.textin::placeholder{color:var(--faint)}
.textin:focus{border-color:var(--line2)}
.badge-new{font-size:11px;color:var(--acc);margin-left:6px}

/* ── 表单 ───────────────────────── */
.formcard{display:none}
.formcard.open{display:block}
textarea{width:100%;background:var(--d2);border:1px solid var(--line2);
  border-radius:var(--radius-md);color:var(--ink);padding:11px 13px;font-size:13.5px;
  font-family:var(--sans);min-height:84px;resize:vertical;outline:none}
textarea:focus{border-color:var(--line2)}
.formrow{display:flex;gap:10px;margin-top:12px;flex-wrap:wrap;align-items:center}
select,.formrow input[type=text]{background:var(--d2);border:1px solid var(--line2);
  border-radius:var(--radius-md);padding:7px 12px;font-size:13px;color:var(--ink);
  outline:none;font-family:var(--sans)}
.imp{font-size:14px;cursor:pointer;color:var(--faint);user-select:none;letter-spacing:2px}
.imp b{color:var(--warn);font-weight:400}

/* ── 交接卡输出 ──────────────────── */
.handoff-out{background:var(--card);border:1px solid var(--line);color:var(--sub);
  border-radius:var(--radius-lg);padding:18px 20px;font-size:12.5px;line-height:1.8;
  white-space:pre-wrap;display:none;margin-top:14px;font-family:var(--sans)}
.empty{text-align:center;color:var(--faint);font-size:12px;padding:28px 0}
.listhead{display:flex;justify-content:space-between;align-items:center;
  margin:20px 0 12px}
.listhead .t{font-family:var(--sans);font-size:14px;font-weight:600;letter-spacing:-.1px}
/* ⚠️ .listhead 原本是 space-between；改成可折叠（.ghead）后多了一个箭头子元素，
   三个子元素会被均分 → 按钮组跑到中间（用户报"文字未对齐"）。
   改成三段式：标题靠左，按钮组和箭头一起顶到右边。 */
.listhead.ghead>.t{flex-shrink:0;margin-right:auto}
/* 框线：具体哪几处加，看 frameTitles() 里的 FRAME_SELECTION（**用户在 ?frames=1 里
   亲手点出来的**，不自动推断 —— 试过两次自动判断，两次都猜错）。
   画法用绝对定位的 ::after，而不是 border / inset 阴影 —— 因为要支持"往下拉大"：
   框可以比元素本身高出 --fext 像素，把下面更多内容圈进来。两种画法都不占布局，
   尺寸不会因此变化。 */
/* 质感必须对齐库里真正的卡片（.panel / .split-main）：--card 底 + --line 描边 +
   --radius-lg(8px)。之前用的是 --d2（提示条底）+ --radius-md(6px) —— 跟原生卡片不是
   一套东西，所以看着"质感不一样"（用户反馈"感觉跟原生本身的不太一样"）。
   库 README 的口径：层级靠底色 + 描边 + 分区传达，阴影全透明。 */
.framed{position:relative;background:var(--card)}
.framed::after{content:"";position:absolute;left:-2px;right:-2px;top:-2px;
  bottom:calc(-2px - var(--fext,0px));
  border:1px solid var(--line);border-radius:var(--radius-lg);
  pointer-events:none}
/* FIX-5：蓝色小字走 --acc-ink，不直接吃 --acc。
   ⚠️ 只改 color —— 不动 border-color / background：--acc 当描边在亮色是 3.56:1，
   对 1px 线（判据 3:1）是够的，换了反而会把描边弄浅。 */
.score,.foldbtn,.grpfoot,.lead .leadlink,.badge-new,
.split-main .mmeta .bdg.src,.btn.txt{color:var(--acc-ink)}
/* 搜索框和它的按钮是一组，必须贴在一起。⚠️ .listhead 是 space-between：
   直接把 input / button 当子元素放，会被均分到中间和最右（用户报的 bug）。 */
.fgroup{display:flex;align-items:center;gap:8px;flex:1;justify-content:flex-end;min-width:0}
.fgroup .textin{max-width:300px}
/* 页面标题区 —— 对齐 Trae 稿 .page-header / .page-title（20px/600），
   补上「4 个数值条」下方原本空掉的那一块 */
.pagehead{display:flex;align-items:center;justify-content:space-between;gap:var(--space-4);
  flex-wrap:wrap;margin-bottom:var(--space-4)}
.pagehead .ptitle{font-family:var(--sans);font-size:20px;font-weight:600;
  line-height:1.3;letter-spacing:-.2px;color:var(--ink);margin:0}
/* ⚠️ 2026-10-07 第二版原型：采集/ 交接卡 / 库 / Agent / 技能MCP 五页标题
   统一 **22px**（与会话列表的 .ttl h2 一样），原来是 20px。
   ⚠️ 只对这 5 页生效 —— .ptitle 还被 v-mem（记忆）、v-pack（记忆包）用，
      那两页是已砍的旧工作台页面，不在七页定稿范围内，**不动它们的字号**。
   ⚠️ 字号 22 / 字重 680 / 字距 -.015em 全部照 proto 的 .ttl h2 抄，
      不自造数值。 */
#v-collect .pagehead .ptitle,
#v-handoff .pagehead .ptitle,
#v-clean .pagehead .ptitle,
#v-agents .pagehead .ptitle,
#v-skill .pagehead .ptitle{font-size:22px;font-weight:680;letter-spacing:-.015em;
  line-height:1.2}
.pagehead .pacts{display:flex;align-items:center;gap:var(--space-2);flex-wrap:wrap}
/* 动作区内的分组：筛选组 / 动作组之间插一条竖分隔线，让「哪个是筛选、哪个是动作」一眼可辨 */
.pagehead .pacts .pgrp{display:flex;align-items:center;gap:var(--space-2);flex-wrap:wrap}
.pagehead .pacts .psep{width:1px;height:20px;background:var(--line);flex:0 0 1px}
/* ── ⑦ Agent 页：卡片网格（原型 C 的 .agrid / .acard）────────────────────
   每个 Agent 一张小卡：头像 + 名称 + 配置路径 + 状态徽章 / 底部 chip + 动作按钮。
   比原来的长条卡片好扫 —— 15 个 Agent 时一屏能看全，不用上下翻。
   ⚠️ 头像直接复用已有的 agentBadge()（品牌色 + 缩写），不另造 .aico 类。 */
.agrid{display:grid;grid-template-columns:repeat(auto-fill,minmax(300px,1fr));gap:var(--space-3)}
.acard{background:var(--card);border:1px solid var(--line);border-radius:var(--radius-lg);
  padding:12px 14px;display:flex;flex-direction:column;gap:10px;transition:border-color .18s}
.acard.on{border-color:var(--line2);box-shadow:inset 3px 0 0 var(--data-skill)}
.acard.bad{opacity:.62}
.acard .atop{display:flex;align-items:center;gap:10px}
.acard .grow{flex:1 1 auto;min-width:0}
.acard .aname{font-size:13.5px;font-weight:600;color:var(--ink);display:flex;
  align-items:center;gap:7px;flex-wrap:wrap}
.acard .apath{font-size:11px;color:var(--faint);margin-top:3px;overflow:hidden;
  text-overflow:ellipsis;white-space:nowrap}
.acard .afoot{display:flex;align-items:center;gap:7px;flex-wrap:wrap;border-top:1px solid var(--line);padding-top:9px;margin-top:auto}

/* ── ⑧⑨ 记忆包 / 交接卡：按原型补三个小组件 ─────────────────────────
   原型用的 --line-soft / --fs-sm 面板没有 → 用 --line / 具体 px 替代，不新造变量。 */
.tagline{display:flex;gap:6px;flex-wrap:wrap}
/* ⚠️ 真库里的 project 值常是**完整路径**（各 Agent 直接传工作区路径），
   原样铺开会把标签行撑爆。这里只做视觉截断，完整值在 title 里 —— 不截数据。 */
.tagline > span{max-width:280px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.dropzone{border:1.5px dashed var(--line);border-radius:14px;padding:30px 20px;
  text-align:center;background:var(--d2);cursor:pointer;
  transition:border-color .15s,background .15s}
.dropzone:hover,.dropzone.over{border-color:var(--acc);background:var(--hover)}
.dropzone svg{width:34px;height:34px;fill:none;stroke:var(--faint);stroke-width:1.6;
  stroke-linecap:round;stroke-linejoin:round}
.dropzone.over svg{stroke:var(--acc)}
.dropzone .dz{font-size:12.5px;color:var(--sub);margin-top:10px;line-height:1.8}
.dropzone .dz small{display:block;color:var(--faint);font-size:11px;margin-top:4px}
.mdbox{background:var(--d2);border:1px solid var(--line);border-radius:14px;padding:16px 18px;
  font-size:13px;line-height:1.8;color:var(--ink);white-space:pre-wrap;word-break:break-word;
  max-height:460px;overflow:auto;margin:0;font-family:var(--mono)}
.pcard .handoff-out{background:transparent;border:0;padding:0;margin-top:0;
  color:var(--ink);white-space:pre-wrap;font-family:var(--mono);font-size:12.5px}
  
.pagehead .pacts .proj-sel{max-width:190px}

/* ── 页头「数据与说明」折叠区（2026-09-24 用户要求）─────────────────────────
   用户原话：每个页面的页头都铺着一行数据 + 一段注释结论，占掉不少视野，
   第一眼该看到的是内容本身，这些文字需要时再展开。
   做法：把 .pagehead 之后紧邻的 .psub（数据行）/ .lead（注释结论）挪进
   .pfold > .pfin，标题右侧挂一个胶囊按钮开合，**默认收起**。结构由 pmAll()
   后处理生成 —— 和 wrapGroups / secApply 同一个套路，9 处 HTML 不用各写一遍，
   以后新页面只要 .pagehead 后面跟 .psub/.lead，就自动被折起来。
   ⚠️ 类名刻意避开 .pagehead/.listhead/.shead/.dhead/.grphead：
       frameKeyOf() 是按"同 section 内 FRAME_SEL 命中项的序号"生成 key 的，
       多命中一个元素，FRAME_SELECTION 整张清单就会错位（画框标注指到别的元素）。
   ⚠️ 展开/收起用 grid-template-rows 0fr→1fr —— 不需要预先知道内容高度；
       浏览器不支持这条过渡时只是"没有动画"，不会有尺寸错位（比 max-height 猜值稳）。
   ⚠️ 状态**不持久化**（只活在本次会话），与项目里"默认视图不是用户偏好"的约定一致：
       存进 localStorage 的话，上次随手展开过的一页，下次进门就不再是默认样子了。 */
.pagehead .phead-l{display:flex;align-items:center;gap:10px;min-width:0}
.psub{font-size:12.5px;color:var(--faint);line-height:1.6;margin:0}
.psub b{color:var(--sub);font-weight:600}
.pmtgl{display:inline-flex;align-items:center;gap:5px;flex:0 0 auto;font:inherit;
  font-size:12px;line-height:1;color:var(--faint);background:var(--d2);
  border:1px solid var(--line);border-radius:999px;padding:5px 10px 5px 8px;cursor:pointer}
.pmtgl:hover{background:var(--d3);color:var(--sub)}
.pmtgl .cvs{width:11px;height:11px;flex:0 0 auto;transition:transform .2s ease}
.pmtgl[aria-expanded="true"]{color:var(--acc-ink);background:var(--d3)}
.pmtgl[aria-expanded="true"] .cvs{transform:rotate(90deg)}
.pmtgl:focus-visible{outline:2px solid var(--acc);outline-offset:2px}
.pfold{display:grid;grid-template-rows:0fr;opacity:.001;overflow:hidden;
  transition:grid-template-rows .22s var(--ease),opacity .18s ease}
.pfold.pf-on{grid-template-rows:1fr;opacity:1}
.pfold>.pfin{overflow:hidden;min-height:0;display:flex;flex-direction:column;gap:10px;
  padding-bottom:0}
.pfold.pf-on>.pfin{padding-bottom:20px}
.pfold>.pfin>.lead{margin-bottom:0}

/* ── 本机内容页的 4 张 KPI 卡（技能 / MCP / 插件 / 配置文件）───────────────
   2026-09-24 用户指定就要这四类（文件名原话：「4 张 KPI 卡需要改成
   skill、MCP、插件、配置文件」），不要"总数 / 备份 / 来源"混在里面。
   数字由 loadSkills() 用真实探测结果填 —— **不写死**：原型里那版"技能 10 /
   插件 60"是编的，被用户当场揪出来过，这类数字必须能对上 /api/skills 与 /api/content。
   卡片沿用项目现有语言：纯平（阴影全透明）、靠 1px 描边与面差分层，
   hover 只抬 2px + 描边提一档，不引入阴影。 */
.kgrid{display:grid;grid-template-columns:repeat(4,minmax(0,1fr));gap:var(--space-3);
  margin-bottom:var(--space-5)}
@media (max-width:1100px){.kgrid{grid-template-columns:repeat(2,minmax(0,1fr))}}
/* 原型 C 的 .kpis.c3：采集页只有 3 张卡，均分 3 列（别的页仍是 4 列） */
.kgrid.c3{grid-template-columns:repeat(3,minmax(0,1fr))}
@media (max-width:1100px){.kgrid.c3{grid-template-columns:repeat(2,minmax(0,1fr))}}
.kpi{background:var(--card);border:1px solid var(--line);border-radius:var(--radius-lg);
  padding:var(--space-4);display:flex;flex-direction:column;gap:10px;min-width:0}
.kpi .ktop{display:flex;align-items:center;justify-content:space-between;gap:8px}
.kpi .kt{font-size:12.5px;font-weight:600;color:var(--sub)}
.kpi .kic{width:26px;height:26px;flex:0 0 auto;border-radius:var(--radius-md);
  background:var(--d3);display:grid;place-items:center}
.kpi .kic svg{width:15px;height:15px}
/* line-height:1 会把行盒压到比字形窄（数字的墨会溢出到盒子外），
   下面那条 4px 的条再贴 8px 就几乎咬住数字。给 2px 缓冲，视觉上才"数字归数字" */
.kpi .kv{font-family:var(--sans);font-size:32px;font-weight:600;line-height:1;
  letter-spacing:-1px;color:var(--ink);font-variant-numeric:tabular-nums;padding-bottom:2px}
.kpi .kv .u{font-size:13px;font-weight:500;color:var(--faint);margin-left:3px;letter-spacing:0}
.kpi .kbar{height:3px;border-radius:2px;background:var(--d3);overflow:hidden}
.kpi .kbar>i{display:block;height:100%;border-radius:2px;width:0;
  transition:width .32s var(--ease)}
/* 原型 C：第一张是主指标卡，条走渐变，跟后面三张的纯色条区分开 */
.kpi.hero .kbar>i{background:linear-gradient(90deg,var(--data-fact),var(--acc2))}
/* ── 按原型 C 补齐的三条样式（之前漏了，这是「看着不像原型」的主因之一）────────
   ① 主指标卡 .hero：淡蓝渐变底 + 左侧竖条 —— 让「第一张是主指标」一眼看出来。
      ⚠️ 原型把 hero 的数字写成 #cfe2ff（暗色专用，浅色主题下几乎看不见），
         这里不改文字色，只做底和竖条 —— 两个主题都读得清。
   ② .chip：轮次/条数这类「数字标签」。原型有这个类，面板之前没有 ——
      于是轮次被迫挤在 .mtags 里，跟标签串成一坨。
   ③ .tb.ok：「产出 N 条」这类正向标记。
      ⚠️ 原型用 --t-ok-bg/--t-ok-fg，面板没有这两个变量；这里复用已有的
         --t-skill-bg/--data-skill 这套绿 —— 不为了对齐而新造变量。 */
.kpi.hero{position:relative;
  background:linear-gradient(180deg,rgba(10,132,255,.09),rgba(10,132,255,0) 58%),var(--card);
  border-color:rgba(10,132,255,.22)}
.kpi.hero::after{content:"";position:absolute;left:0;top:16px;bottom:16px;width:3px;
  border-radius:0 3px 3px 0;background:var(--data-fact)}
.chip{display:inline-flex;align-items:center;height:19px;padding:0 7px;border-radius:6px;
  font-size:10.5px;color:var(--sub);background:var(--d3);white-space:nowrap;flex:0 0 auto}
.tb.ok{background:var(--t-skill-bg);color:var(--data-skill)}
/* ── 清理页：风险分级卡片（原型 C 的 .pcard / .phead / .stepno / .riskflag）──────
   原型把 4 段按「风险从低到高」排列，段头左侧竖条 + 右上风险徽章让人一眼看出危险度。
   ⚠️ **保留原有的 .panel / .listhead 类名**（新类只是往后追加，且 listhead 写在最前）：
      · wrapPanels() 靠 `:scope > .panel` + `:scope > .listhead` 找折叠体
      · FRAME_SELECTION 靠 `.pagehead`(下标0) + 4 个 `.listhead`(下标1~4) 定位
      换掉类名这两处会同时断掉（踩过两次同类坑）。 */
.pcard{position:relative;overflow:hidden}
.pcard::before{content:"";position:absolute;left:0;top:0;bottom:0;width:3px;background:var(--line)}
.pcard.risk-ok::before{background:var(--data-skill)}
.pcard.risk-warn::before{background:var(--data-decision)}
.pcard.risk-bad::before{background:var(--data-error)}
/* ⚠️ .pcard / .pcard>.phead 的壳样式在质检页那一段已经定义过（下面 1596 行起），
   这里**不重复写**（重复会被后定义的那条覆盖，白写一遍还容易看混）。
   只补清理页特有的部分：序号圆、风险徽章、折叠体的内边距。 */
.pcard>.phead>.t{display:flex;align-items:center;gap:8px}
/* 段头右侧的「风险徽章 + 按钮」组（原型是内联 flex，这里提成类） */
.pcard>.phead>.pr{display:flex;align-items:center;gap:8px;flex-wrap:wrap;flex:0 1 auto}
.stepno{display:inline-flex;align-items:center;justify-content:center;width:19px;height:19px;
  border-radius:50%;background:var(--d3);color:var(--faint);font-size:11px;font-weight:700;flex:0 0 auto}
.pcard.risk-ok .stepno{background:var(--t-skill-bg);color:var(--data-skill)}
.pcard.risk-warn .stepno{background:var(--t-decision-bg);color:var(--data-decision)}
.pcard.risk-bad .stepno{background:var(--t-error-bg);color:var(--data-error)}
.riskflag{display:inline-flex;align-items:center;height:20px;padding:0 8px;border-radius:6px;
  font-size:10.5px;font-weight:600;white-space:nowrap;flex:0 0 auto}
.riskflag.ok{background:var(--t-skill-bg);color:var(--data-skill)}
.riskflag.warn{background:var(--t-decision-bg);color:var(--data-decision)}
.riskflag.bad{background:var(--t-error-bg);color:var(--data-error)}
/* wrapPanels() 生成的折叠体 .gbody 直接扮演原型的 .pbody（内边距）——
   不能自己再写一层 .pbody：那层会被 wrapPanels 一起搬进 .gbody，变成双层留白。 */
.pcard>.gbody{padding:12px 14px 14px 17px}
.pcard.tight-body>.gbody{padding:8px 8px 8px 10px}
.pcard>.gbody>p.hint{margin-top:0}
.pcard>.gbody>.row{margin-top:10px}
.btn.sm{height:26px;padding:0 9px;font-size:11.5px}
/* 危险动作按钮：只改文字色（边框保持默认，避免硬编码 rgba —— 明暗主题各有一套变量） */
.btn.danger{color:var(--data-error)}
.btn.danger:hover{background:var(--t-error-bg);border-color:var(--data-error)}
/* 段间距：原型用 .stepped 包一层 —— 但 wrapPanels 认的是 `#v-clean > .panel`
   **直接子元素**，多包一层折叠会整页失效。所以用 margin 代替 .stepped。 */
:is(#v-clean,#v-collect)>.pcard{margin-bottom:14px}
:is(#v-clean,#v-collect)>.pcard:last-child{margin-bottom:0}
/* 清理页列表行：原型用的是紧凑行，不是卡片。
   ⚠️ **不要加 .lrow 类** —— 主从页的紧凑行（`.split-main .mem.lrow`）有自己一整套规则，
      而清理页不在 .split-main 里；更要紧的是统一选中态那条写的是
      `.mem.sel:not(.lrow)`，注释明确说「这里只管卡片行（质检/清理/本机内容/采集）」。
      给清理行挂上 .lrow 会把它从本该所属的那一档里踢出去 → **选中蓝底直接没了**
      （verify_sel_feedback 已实测报错）。所以：仍用 .mem，紧凑外观靠 #v-clean 前缀收口。
   ⚠️ 前缀 #v-clean 是跨页护栏：别的页一行不受影响。 */
:is(#v-clean,#v-collect) .mem{display:flex;align-items:flex-start;gap:10px;background:transparent;border:0;
  border-radius:0;padding:9px 2px;margin-bottom:0;box-shadow:none;border-bottom:1px solid var(--line)}
:is(#v-clean,#v-collect) .mem:last-child{border-bottom:0}
/* 采集页：已入库的候选整行置灰（原型就是 opacity:.45）—— 一眼看出哪些不用管 */
:is(#v-clean,#v-collect) .mem.indb{opacity:.45}
/* ⚠️ 带 :not(.sel)：否则 hover 的灰底会压掉选中态（#v-clean 前缀特异性比 .mem.sel 那条高） */
:is(#v-clean,#v-collect) .mem:hover:not(.sel){background:var(--hover);transform:none}
:is(#v-clean,#v-collect) .mem .top{margin-bottom:5px}
:is(#v-clean,#v-collect) .mem .content{font-size:12.5px;line-height:1.6}
/* 源文件/勾选行里的图标块：.mico 的尺寸原本也挂在 .split-main 下，这里补一份 */
:is(#v-clean,#v-collect) .mico{display:inline-flex;align-items:center;justify-content:center;width:30px;height:30px;
  border-radius:var(--radius-md);flex:0 0 auto}
:is(#v-clean,#v-collect) .mico svg{width:16px;height:16px}
/* 紧凑行内部：.mbody/.mtitle/.mmeta 的样式原本同样挂在 .split-main 下，
   清理页拿不到 → 这里按同样的排版补一份（只作用于 #v-clean）。 */
:is(#v-clean,#v-collect) .mbody{min-width:0;flex:1 1 auto}
:is(#v-clean,#v-collect) .mtitle{font-size:13px;color:var(--ink);line-height:1.45;overflow:hidden;
  text-overflow:ellipsis;white-space:nowrap}
/* 记忆勾选行：正文要读得全，给两行 */
:is(#v-clean,#v-collect) .mem.pick .mtitle{display:-webkit-box;-webkit-box-orient:vertical;
  -webkit-line-clamp:2;white-space:normal;line-height:1.55;word-break:break-word}
:is(#v-clean,#v-collect) .mmeta{display:flex;align-items:center;gap:7px;flex-wrap:wrap;margin-top:4px;
  font-size:11px;color:var(--faint)}
:is(#v-clean,#v-collect) .mtime{color:var(--faint);font-variant-numeric:tabular-nums}
/* 选中态：统一规则换的是 .content/.meta 的文字色，这里用的是 .mtitle/.mmeta —— 补齐。
   ⚠️ 选中底**必须在 #v-clean 里重写一遍**：上面 `:is(#v-clean,#v-collect) .mem{background:transparent}`
      带 id 前缀（特异性 1,1,0），而全局那条是 `.mem.sel:not(.lrow)`（0,3,0）——
      **id 永远赢**，所以全局规则压不过这里（verify_sel_feedback 实测：四条全红）。
      不是 lrow 的问题，是 id 前缀太强。 */
:is(#v-clean,#v-collect) .mem.sel{background:var(--sel-bg);border-color:transparent;
  box-shadow:inset 3px 0 0 var(--sel-accent)}
:is(#v-clean,#v-collect) .mem.sel .mtitle,:is(#v-clean,#v-collect) .mem.sel .mmeta,
:is(#v-clean,#v-collect) .mem.sel .mtime,:is(#v-clean,#v-collect) .mem.sel .mico{color:var(--sel-ink)}
:is(#v-clean,#v-collect) .mem.sel .chip{background:var(--sel-accent);color:var(--sel-ink)}
:is(#v-clean,#v-collect) .mem>input[type=checkbox]{margin:0;flex:0 0 auto}
:is(#v-clean,#v-collect) .empty{padding:24px 12px;text-align:center;color:var(--faint);font-size:12.5px}

/* 收拢一组内容的卡片壳（原型 C 的 .pcard）。面板原本没有这个类 ——
   质检页的「七查明细」用它包住七行，头部标题与说明各占一端。
   ⚠️ 原型用 --radius-2xl / --inner-hi / --line-soft，面板没有 → 用现有变量替代。 */
.pcard{background:var(--card);border:1px solid var(--line);border-radius:var(--radius-lg);
  margin-bottom:var(--space-4);overflow:hidden}
.pcard>.phead{display:flex;align-items:center;justify-content:space-between;gap:var(--space-3);
  padding:var(--space-4) var(--space-5);border-bottom:1px solid var(--line)}
.pcard>.phead .t{font-size:14px;font-weight:600;color:var(--ink)}
.pcard>.phead .hint{margin-top:0;flex:0 1 auto;text-align:right}
.pcard .pbody{padding:var(--space-3) var(--space-4)}
/* tight：行自己带内边距（.auditrow 就是），外壳就别再加一层，否则每行上下各空一截 */
.pcard .pbody.tight{padding:var(--space-1) var(--space-2)}
/* ── ④ 质检页：健康卡 + 七查明细（按原型 C，2026-09-26）─────────────────────
   原型的健康卡是「大分数 + 评级 + 一句话总结 + 7 个维度条」，
   比原来的圆环仪表盘信息量大：七个维度各自什么状态，一眼扫完，不用逐个点。
   ⚠️ 原型用的 --radius-2xl / --inner-hi / --line-soft / --dim / --fs-sm / --t-warn-*
      面板都没有 —— 用现有变量替代（--radius-lg / --line / --faint），不新造变量。 */
.hcard{background:linear-gradient(180deg,rgba(48,209,88,.07),transparent 62%),var(--card);
  border:1px solid var(--line);border-radius:var(--radius-lg);padding:var(--space-4);
  margin-bottom:var(--space-4)}
.hcard .htop{display:flex;align-items:flex-end;gap:var(--space-5);flex-wrap:wrap}
.hcard .score{display:flex;align-items:baseline;gap:6px}
.hcard .score b{font-size:46px;font-weight:700;letter-spacing:-2.4px;line-height:1;
  font-variant-numeric:tabular-nums;color:var(--ink)}
.hcard .score .hgrade{font-size:13px;font-weight:700;padding:2px 8px;border-radius:7px;
  box-shadow:inset 0 0 0 1px currentColor}
/* 评级配色：2026-09-28 从旧的 `.health .hgrade.a/.b/.c/.d` 迁过来。
   那几条挂在 `#health` 容器上（class="health"），等于靠"祖先还叫 .health"活命 ——
   隐性依赖，删旧结构样式时会暗伤质检页（外部审查就是这么抓到我误判的）。
   现在就近挂在自己的类上，作用域自洽。 */
.hcard .score .hgrade.a,.hcard .score .hgrade.b{color:var(--ok)}
.hcard .score .hgrade.c{color:var(--warn)}
.hcard .score .hgrade.d{color:var(--bad)}
.hcard .score .of{font-size:12px;color:var(--faint)}
.hcard .hsum{flex:1;min-width:220px;font-size:12px;color:var(--sub);line-height:1.75}
.hcard .hsum b{color:var(--ink)}
.hdims{display:grid;grid-template-columns:repeat(auto-fit,minmax(143px,1fr));gap:1px;
  margin-top:var(--space-5);background:var(--line);border:1px solid var(--line);
  border-radius:12px;overflow:hidden}
.hdims .d{background:var(--card);padding:11px 14px}
.hdims .dk{font-size:10.5px;color:var(--faint);font-weight:600;letter-spacing:.3px}
.hdims .dv{margin-top:4px;font-size:19px;font-weight:700;font-variant-numeric:tabular-nums;
  letter-spacing:-.6px;color:var(--ink)}
.hdims .dbar{margin-top:7px;height:4px;border-radius:2px;background:var(--d3);overflow:hidden}
.hdims .dbar i{display:block;height:100%}
/* A：KPI 卡跳转后的高亮 —— 只动 background，不动几何（见 skJump 注释）
   ⚠️ 选择器**不能**限定成 `#sk-list .skflash`：技能页的分组头没有 .panel 祖先，
      skJump 的兜底会把 .skflash 加到 #sk-list **自身**上，后代选择器匹不到自己
      （实测 animationName=none，高亮不出现）。用裸类名，够精确也够通用。 */
@keyframes skflash{0%{background:var(--sel-bg)}60%{background:var(--sel-bg)}100%{background:transparent}}
.skflash{animation:skflash 1.15s ease-out}
#sk-kpi .kpi{cursor:pointer}
/* ⚠️ 窄屏时上面 JS 设的 N 列会挤成一团 → 这里强制降级（!important 是必要的：
   内联 style 的优先级高于样式表，不覆盖就压不住） */
@media (max-width:1100px){.hdims{grid-template-columns:repeat(4,minmax(0,1fr)) !important}}
@media (max-width:640px){.hdims{grid-template-columns:repeat(2,minmax(0,1fr)) !important}}
/* 七查明细：紧凑行（原来每条是一个 .mem 卡片，一屏看不了几条） */
.auditrow{display:flex;align-items:flex-start;gap:var(--space-3);padding:13px var(--space-3);
  border-radius:11px}
.auditrow+.auditrow{margin-top:2px}
.auditrow:hover{background:var(--hover)}
.auditrow .ano{width:22px;height:22px;border-radius:7px;flex:0 0 22px;display:grid;
  place-items:center;font-size:11px;font-weight:700;margin-top:2px}
.auditrow .grow{flex:1;min-width:0}
.auditrow{cursor:pointer}
.auditrow .cv{margin-left:auto;color:var(--faint);font-size:10px;flex:0 0 auto;padding-top:4px}
/* 展开出来的候选：往右缩进对齐到文字列，跟上面的行形成层级 */
.auditbody{margin:0 0 var(--space-2) 34px;padding:2px 0 6px}
/* 警告徽章（原型 .tb.warn）。面板没有 --t-warn-*，复用 --t-decision-bg/--data-decision 那套黄 */
.tb.warn{background:var(--t-decision-bg);color:var(--data-decision)}
.kpi .kfoot{display:flex;justify-content:space-between;gap:8px;font-size:11.5px;
  color:var(--faint);line-height:1.5}
.kpi .kfoot>span:last-child{text-align:right;flex-shrink:0}

/* ── 记忆构成条（记忆页 2026-09-24）────────────────────────────────────
   一条横向堆叠条 + 图例，类型分布一眼看清。
   宽度用 flex-grow = 真实条数（不是百分比）—— 少一次除法就少一处能算错的地方，
   0 条的类别自动不占宽。颜色直接吃 --data-*（与列表图标块、徽章同一套色）。 */
.distcard{background:var(--card);border:1px solid var(--line);border-radius:var(--radius-lg);
  padding:var(--space-4) var(--space-5);margin-bottom:var(--space-5)}
.distcard .dh{display:flex;align-items:baseline;justify-content:space-between;gap:var(--space-3);
  margin-bottom:var(--space-3)}
.distcard .dh b{font-size:13px;font-weight:600;color:var(--ink)}
.distcard .dh span{font-size:11.5px;color:var(--faint)}
.distcard .bar{display:flex;height:10px;border-radius:5px;overflow:hidden;background:var(--d3)}
.distcard .bar>i{display:block;min-width:2px}
.distcard .legend{display:flex;flex-wrap:wrap;gap:var(--space-2) var(--space-4);
  margin-top:var(--space-3);font-size:11.5px;color:var(--sub)}
.distcard .legend>span{display:inline-flex;align-items:center;gap:6px}
.distcard .legend b{width:8px;height:8px;border-radius:3px;flex:0 0 8px}
.distcard .legend em{font-style:normal;color:var(--ink);font-weight:600;
  font-variant-numeric:tabular-nums}

/* ── 详情栏顶部 4 格元信息网格（记忆页）──────────────────────────────
   把「类型 / 重要度 / 来源 / 创建时间」从下面那一大段纯文字里提到第一屏。
   原来这几项埋在「归属信息」的 <br> 串里，要滚下去才看得到。 */
.kv-grid{display:grid;grid-template-columns:repeat(4,minmax(0,1fr));gap:var(--space-2)}
.kv-grid .cell{background:var(--d2);border-radius:var(--radius-md);padding:8px 10px;min-width:0}
.kv-grid .k{font-size:10.5px;color:var(--faint);line-height:1.4}
.kv-grid .v{font-size:12.5px;color:var(--ink);font-weight:600;line-height:1.5;margin-top:2px;
  overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
@media (max-width:900px){.kv-grid{grid-template-columns:repeat(2,minmax(0,1fr))}}

/* ── 记忆列表行的排版（2026-09-24）────────────────────────────────────
   ⚠️ 必须带 #list 前缀：`.split-main .mtitle` 是记忆页与会话页**共用**的选择器，
      直接改字号会让会话行从 61.19px 长高 → verify_slist_rows 量到就红。
      #list 只有记忆页有（会话页是 #s-list），所以这条是安全的收口。 */
#list .mem.lrow{padding:15px var(--space-4)}
#list .mtitle{font-size:14px}
#list .mmeta{margin-top:5px}

/* ── 主从详情切换：内容整体换掉时淡入一次（2026-09-24）─────────────────────
   纯 CSS，不写一行 JS：renderDetail() / 会话详情 / 技能详情都是整块 innerHTML
   重写，新插入的节点天然会重放 animation —— 于是切换就有了呼吸感。
   ⚠️ 只用 opacity，**不用 transform**：translateY 会临时改变
      getBoundingClientRect，而本项目有一批量几何的闸门（measure_layers /
      verify_link_measure / verify_slist_rows），动画进行中量到
      几 px 偏移就会假报错。opacity 不参与布局，量出来的数字和以前完全一样。
   ⚠️ 时长 0.2s 是刻意的：比切换本身的网络往返短得多，不会让人等动画。
   系统「减少动态效果」时由全局 @media 压掉（animation-duration→0.001ms）。 */
@keyframes detailIn{from{opacity:0}to{opacity:1}}
.split-side>.dhead,.split-side>.dmain{animation:detailIn .2s var(--ease) both}

/* 页面骨架的卡片节奏：卡片之间 20px（库的 .content 用 calc(--spacing*5)），
   区块标题（.listhead）跟着卡片走。规范第五节的 A/B/C 三套骨架都靠这两条，
   页面上就不用写内联 margin 了 */
.panel+.panel{margin-top:var(--space-5)}
.panel>.listhead:first-child{margin-top:0}
.panel>.listhead{margin-bottom:12px}
.listhead+.panel{margin-top:0}
/* 清单里的类别分组头：技能 / 配置文件 / MCP 三组共用左列一条滚动线。
   不是新模块，就是把原来单一列表按类别分段（用户明确讨厌"突然多一个模块"）。 */
.grphead{display:flex;justify-content:space-between;align-items:center;gap:10px;
  margin:18px 0 10px;padding:0 2px}
.grphead:first-child{margin-top:2px}
.grphead .gt{font-family:var(--sans);font-size:12.5px;font-weight:600;color:var(--sub)}
.grphead .gn{font-size:11.5px;color:var(--faint);margin-left:auto}
/* 段头改成可折叠（加 .ghead）后，flex 的 space-between 会把新插的箭头甩到中间，
   所以计数用 margin-left:auto 顶到右边，箭头紧贴标题。 */
.grphead.ghead{gap:6px}
.grphead.ghead .cv{cursor:pointer}
/* 每一类单独成一个「框」（用户要的"一块一片区域"）：框头 + 框体各自有边框，
   折叠后框体消失、框头留着。 */
/* P2-1：组头 + 组体原来是两块各自带边框、中间还留 2px 缝，看着像"两块"。
   并成一块：组头保上圆角去下边框，组体保下圆角、去掉上间距。 */
#sk-list .grphead.ghead{border:1px solid var(--line);border-bottom:0;
  border-radius:var(--radius-md) var(--radius-md) 0 0;
  padding:var(--space-2) var(--space-3);background:var(--d2);margin-top:var(--space-3)}
#sk-list .gbody{border:1px solid var(--line);
  border-radius:0 0 var(--radius-md) var(--radius-md);
  padding:var(--space-2);margin-top:0}
/* 亮色主题下组头底色与卡片的面差只有 1.05:1 —— 分组条等于消失了。换深一档。 */
:root[data-theme="light"] #sk-list .grphead.ghead{background:var(--d3)}
/* 左栏条目不是太挤、是太松：单条实测 206px 高，插件组五十几条 → 上万 px，
   而右栏同时是空的。描述压到 3 行（206 → 128.5px，技能组体 1828 → 1341px）。
   ⚠️ line-clamp 必须配 display:-webkit-box + -webkit-box-orient:vertical ——
   只写 clamp 的话 getComputedStyle 会读回"3"骗你，但高度一动不动。 */
#sk-list .gbody .mem{padding:var(--space-3) var(--space-4);margin-bottom:2px}
#sk-list .gbody .mem .content{display:-webkit-box;-webkit-box-orient:vertical;
  -webkit-line-clamp:3;overflow:hidden}
/* 分组内容下方的收起按钮（用户要求放在下面，而不是去点标题） */
/* 组体可能 1800+px 高，收起按钮会离组头很远（实测 1834px）——
   sticky 让它吸附在滚动容器底部，人在哪儿都够得着。 */
.grpfoot{display:block;width:100%;margin-top:4px;padding:4px 0;font:inherit;font-size:12px;
  color:var(--acc);background:var(--card);border:1px dashed var(--line);
  border-radius:var(--radius-md);cursor:pointer;
  position:sticky;bottom:0;z-index:2}
.grpfoot:hover{background:var(--hover)}
/* 会话页：第一眼该看「已归档会话」（列表 + 原文），「归档新会话」表单排到最后。
   顺序只用 order 调，DOM 不动 —— 免得搬一大块 HTML 出错。 */
/* ⚠️⚠️ 2026-10-06 实测 bug：滚到底时「记忆质检」整页露出来了。
   根因：`#v-session:not([hidden]){display:flex}` 这类规则给会话页设了 display:flex，
   而 **`display:flex` 会覆盖 `<section hidden>` 的浏览器默认 `display:none`**
   —— 一旦某个 section 被别的 display 规则命中，hidden 就彻底失效。
   下面这条兜底：带 hidden 的一律 display:none，:not 伪类自带高特异性，压得住。 */
section[hidden]{display:none!important}

/* ⚠️⚠️ 必须带 :not([hidden])！
   裸写 `display:flex` 会盖掉 [hidden] 的 `display:none` —— 这个 section 就永远可见，
   内容串到别的页面上（实测踩坑：记忆页里能同时看到会话页和本机内容页）。
   切换页面靠的是 JS 设 hidden 属性，CSS 绝不能把它顶掉。 */
#v-session:not([hidden]){display:flex;flex-direction:column}
#v-session>.pagehead{order:1}
/* ⚠️ .lead 会被 pmAll() 挪进 .pfold，所以两条都得带 order —— 只写 .lead 的话，
   折叠区退回默认 order:0，会跳到页面最上面去。 */
#v-session>.lead,#v-session>.pfold{order:2}
#v-session>.bignums{order:3}            /* 2026-10-04: .kgrid 4张KPI卡 → .bignums 5个大数字 */
#v-session>#scan-msg{order:4}
#v-session>#scan-out{order:5}
#v-session>.listhead{order:6}
#v-session>.split{order:7}
/* ⚠️ 2026-10-04：.split 改成 display:block 后，「归档新会话」这个 .panel(order:8)
   会插到会话列表前面去（截图实测过）。显式排到列表之后。 */
#v-session>.panel{order:9}

/* ══ 2026-10-04 会话页新设计 ══════════════════════════════════════════
   目标：把「4 张 KPI 卡 + 已归档/原文双栏」换成「大数字 + 单栏会话表 + 正文区」。
   三条铁律记在这里：
     ① 数字的 id 全部沿用原来的（skpi-sess/turn/mem/link），loadSessions() 不改；
     ② #s-view（原文时间线）**不能删** —— 它承载「抽取记忆」的交互，
        移到正文区位置，不是拆掉；
     ③ 所有颜色走 var(--xxx)，不写死 hex，否则明暗切换会崩。 */

/* 5 个超大数字。等宽字体，扫读最快 */
.bignums{display:flex;align-items:flex-end;margin:6px 0 16px;flex-wrap:wrap}
.bignums>div{padding:0 22px;border-left:1px solid var(--line)}
.bignums>div:first-child{padding-left:2px;border-left:none}
.bignums .k{font-size:11px;color:var(--faint);letter-spacing:.08em;margin-bottom:6px}
.bignums .v{font-family:var(--mono);font-size:32px;font-weight:700;line-height:1;
  letter-spacing:-.03em;color:var(--ink)}
.bignums .v.warn{color:var(--warn)}
.bignums .v.bad{color:var(--danger)}
/* 单栏：会话表 + 正文区上下排。
   ⚠️ .split 保持 flex（不能用 display:block）——实测改成 block 后，
   .split-side 会被挤到列表下面，29 条会话要滚到底才看得到详情。
   改用 flex-direction:column + order，把详情提到列表上方：
   选中一条会话立刻就在眼前，不用滚。
   为什么原来是 flex: 现在 .split 的默认方向是 row（左右），
   显式改 column 才是上下。 */
#v-session .split{display:flex;flex-direction:column;max-width:none}
#v-session .split-main{order:2;width:100%}
#v-session .split-side{order:1;width:100%;margin-bottom:20px;overflow:visible}
/* 本机内容页同理：第一眼该看「清算结果」（几个技能 / MCP / 插件），
   「本机来源探测」排到最后并**默认收起**。 */
/* ⚠️ 同 #v-session：必须带 :not([hidden])，否则会顶掉 hidden 的 display:none */
#v-skill:not([hidden]){display:flex;flex-direction:column}
#v-skill>.pagehead{order:1}
#v-skill>.lead,#v-skill>.pfold{order:2}   /* 同上：.lead 会被挪进 .pfold */
#v-skill>.kgrid{order:3}                  /* 4 张 KPI 卡：技能 / MCP / 插件 / 配置文件 */
#v-skill>.split{order:4}
#v-skill>.panel{order:5}
.row{display:flex;gap:10px;align-items:center;flex-wrap:wrap}
.hint{font-size:12px;color:var(--faint);line-height:1.7;margin-top:10px}
/* 本机对话来源列表 —— 让"这次到底扫了谁、为什么"看得见
   （2026-09-21：以前来源是写死的三个产品名，跟本机实际装了什么无关） */
.srclist{display:flex;flex-direction:column;gap:var(--space-2)}
.srcrow{display:flex;align-items:center;gap:10px;padding:7px 11px;
  border-radius:var(--radius-md);background:var(--d2);font-size:12.5px;color:var(--sub)}
/* 「扫到了 / 没扫到」原来只靠一个 7px 圆点区分（面差深色 1.16:1、亮色 1.08:1）。
   补一条左侧色条。用 inset 阴影而不是 border-left —— border 会把这行顶高 2px
   （项目里 .bdg 已经踩过同一个坑）。 */
.srcrow.on{background:var(--d3);color:var(--ink);box-shadow:inset 3px 0 0 var(--ok)}
.srcrow .sdot{width:7px;height:7px;border-radius:50%;background:var(--faint);flex:0 0 7px}
.srcrow.on .sdot{background:var(--ok)}
.srcrow .sname{font-weight:600;color:var(--ink);flex:0 0 auto;min-width:78px}
.srcrow .smeta{flex:0 0 auto;color:var(--sub)}
.srcrow .spath{margin-left:auto;color:var(--faint);font-size:11px;max-width:340px;
  overflow:hidden;text-overflow:ellipsis;white-space:nowrap;
  direction:rtl;text-align:left;unicode-bidi:plaintext}
.srcrow .swhy{flex:1 1 auto;color:var(--faint);font-size:11.5px}
/* 「另有 N 个…」这类来源备注：用户明确说太复杂、不想看见，直接不渲染。
   规则留着（DOM 也还在），以后想改成折叠或极简提示，改这一处即可。 */
.srcnote{display:none}

@media(max-width:760px){
  .side{display:none}
  .stats{gap:20px}
}
/* ───────── 设计语言升级 + 动效（纯 CSS，零依赖） ───────── */
:root{
  --ease:cubic-bezier(.2,.8,.2,1);
  --dur:.28s;
}
@keyframes viewIn{from{opacity:0;transform:translateY(10px)}to{opacity:1;transform:none}}
@keyframes cardIn{from{opacity:0;transform:translateY(8px) scale(.995)}to{opacity:1;transform:none}}
@keyframes toastIn{from{opacity:0;transform:translateY(-12px) scale(.98)}to{opacity:1;transform:none}}
@keyframes pulse{0%,100%{opacity:1}50%{opacity:.45}}
@keyframes shimmer{from{background-position:-220px 0}to{background-position:220px 0}}

/* 视图切换 */
section[id^="v-"]{animation:viewIn var(--dur) var(--ease) both}

/* 卡片入场（stagger：渲染时写入 --i） */
.stat{animation:cardIn .34s var(--ease) both;animation-delay:calc(var(--i,0)*32ms)}

/* 悬停微交互（.kpi 是 2026-09-24 加的 KPI 卡，共用同一条：抬 2px + 描边提一档，
   都不参与布局，所以量尺寸的闸门读到的数字不变） */
.mem,.stat,.kpi{transition:transform .18s var(--ease),border-color .18s var(--ease),background .18s var(--ease)}
.mem:hover,.kpi:hover{transform:translateY(-2px);border-color:var(--line2)}
.btn,.mini,.del,.nav{transition:transform .12s var(--ease),background .16s var(--ease),color .16s var(--ease)}
.btn:active,.mini:active,.del:active{transform:scale(.965)}
/* 键盘焦点可见环：亮色蓝 / 暗色白（规范 --ring） */
.btn:focus-visible,.nav:focus-visible,.mini:focus-visible,.del:focus-visible,
.foldbtn:focus-visible,.grpfoot:focus-visible,
.ghead:focus-visible,[data-secfold]:focus-visible{
  outline:2px solid var(--ring);outline-offset:2px}
.nav:active{transform:scale(.985)}

/* ─────────────────────────────────────────────────────────────────────
   会话页（2026-10-06 按定稿原型 p5-final/list.html 重做）
   ⚠️ 所有数值照抄原型，不凭手感改。用户明确要求：定稿后不许为了修 bug
      自动放大/缩小 —— 之前我在导航坞上连续犯过 4 次，已被用户明确斥责。
   视觉规范来自 oil-ui/references/visual-language.md：
      · 一页一个主角 → 页头 5 个大数字是唯一强强调，表格退后
      · 容器只在确有分组时出现 → 表格/右栏框是分组边界，行内不加框
      · 分隔线不重复 → 表头与数据行共用同一 grid，线只画一道
      · 文字角色分级：标题 13.5px 600 / 副信息 11px / 标签 10px / 时间 10.5 等宽 */
#v-session,#v-sdetail{--sp-indigo:#4338ca; --sp-ibg:#eef0fd; --sp-line:#e4e0d8;
  --sp-bg:#faf9f7; --sp-surface:#ffffff; --sp-surface2:#f3f1ec;
  --sp-ink:#1c1b19; --sp-ink2:#4e4b45; --sp-sub:#857f74; --sp-faint:#aaa49a;
  --sp-teal:#0f766e; --sp-amber:#b45309; --sp-coral:#e11d48;
  --sp-ok:#047857; --sp-violet:#7c3aed;
  --sp-sh:0 2px 4px rgba(28,27,25,.04),0 8px 24px rgba(28,27,25,.07)}

/* ⚠️⚠️ 2026-10-06 用户明确批评：「我选的是暖白底，你为什么做的跟最开始 Loci 面板一样的颜色」
   根因：我之前用了 var(--sp-surface) / var(--lz-indigo) 这些**面板全局变量**，
   那些是 Google 蓝 + 冷灰，跟定稿的暖白靛蓝是两套 —— 一用变量就等于没换。
   **规则：会话页 / 详情页一律不引用 --d0..--d3 / --acc / --ink / --sub / --line，
   只用上面这组 --sp-* 硬编码值。** 改色板只改这一处。 */

.sp-search{flex:1;max-width:230px;background:var(--lz-surface);
  border:1px solid var(--lz-line);padding:7px 12px;border-radius:8px;
  display:flex;align-items:center;gap:8px;box-shadow:var(--lz-sh)}
/* ⚠️ 2026-10-07：详情页隐藏搜索框和「重新扫描」。
   .sp-search 上面是 display:flex —— display 会覆盖 [hidden] 的
   display:none（项目里踩过：v-audit 就是这么整页露出来的）。
   所以必须显式写这条 display:none!important。 */
.sp-search[hidden],#sp-scanbtn[hidden]{display:none!important}
/* ⚠️ padding:0 是必须的（2026-10-07 用户指出「搜索框比旁边按钮高太多」）。
   旧工作台残留的 `.toolbar input{padding:7px 13px}` 会跨过 .sp-search 命中这个 input，
   本规则重置了 background/border 唯独漏了 padding，于是搜索框 46px vs 按钮 32px。
   原型 .search input 的 padding 就是 0，实测 32.8px ≈ 按钮 32px。 */
.sp-search input{background:none;border:none;outline:none;color:var(--lz-ink);
  font-family:inherit;font-size:12.5px;flex:1;min-width:0;padding:0}
.sp-search input::placeholder{color:var(--lz-faint)}
.sp-search kbd{font-family:var(--lz-mono);font-size:9.5px;background:var(--lz-surface2);
  color:var(--lz-sub);padding:1.5px 5px;border-radius:3px;white-space:nowrap;flex:none}
/* .sp-btn = 顶栏「重新扫描」。原型是 .btn.pri —— 靛蓝实心主按钮。
   ⚠️ 2026-10-07 照原型改：原来只有白底 .btn，跟右下的 ⏻ 长得一样，
      主次不分。原型里它是这一栏唯一的实心按钮。 */
.sp-btn{font-family:inherit;font-size:12px;padding:7px 13px;border-radius:8px;
  border:1px solid var(--lz-indigo);background:var(--lz-indigo);color:#fff;
  font-weight:600;cursor:pointer;box-shadow:var(--lz-sh);white-space:nowrap}
.sp-btn:hover{background:#372eb0;border-color:#372eb0}

/* 页头：标题 + 副标在左，5 个大数字靠右
   ⚠️ 数值全部照抄定稿原型 p5-final/list.html 的 .phead，一个字没改。
      我之前自己放大过（h1 30px、数字 32px、分隔线 22px），**已按原型退回**：
      h1 30→22px · 数字 32→25px · padding 12/18→14/20 · 分隔线 22→17px
      ⚠️ 这些是用户定稿的排版，不许为了"看着更大气"再动。 */
.sp-head{display:flex;align-items:flex-end;gap:20px;padding:14px 4px 20px}
.sp-head .ttl{flex:none;min-width:0}
.sp-head .kpis{margin-left:auto;display:flex;flex:none}
/* 大数字：靠一条竖线分组，不用卡片 —— 卡片会让 5 个数字互相竞争。
   第一个不带左边线（原型 :first-child{border-left:none}） */
.sp-head .kpis > div{padding:0 17px;border-left:1px solid var(--lz-line);text-align:right}
.sp-head .kpis > div:first-child{border-left:none}
.sp-head h1{font-size:22px;font-weight:680;letter-spacing:-.015em;line-height:1.2;color:var(--lz-ink)}
.sp-head .s{font-size:11.5px;color:var(--lz-sub);margin-top:6px}
.sp-head .kpis .v{font-family:var(--lz-mono);font-size:25px;font-weight:700;line-height:1.1;
  letter-spacing:-.02em;color:var(--lz-ink)}
.sp-head .kpis .l{font-size:10.5px;color:var(--lz-sub);margin-top:2px}
.sp-head .kpis .v.amber{color:var(--sp-amber)}
.sp-head .kpis .v.coral{color:var(--sp-coral)}
.sp-head .kpis .v.ok{color:var(--sp-ok)}

/* 主区：左表格 + 右栏
   ⚠️ 踩了三次才对，根因是**祖先链上有 `margin-left:auto` 或内容撑宽**，
      导致 flex 子项的可用空间算错。稳妥写法是 grid 而不是 flex：
      grid-template-columns 的 1fr 不会被子项内容撑开。
      （flex 里 1fr 实际是 minmax(auto,1fr)，auto 最小值会被长标题顶开。） */
#v-session .cols{display:grid;grid-template-columns:minmax(0,1fr) 300px;gap:16px;
  align-items:start}
#v-session .cols > .sp-main{min-width:0;width:100%}
#v-session .sp-side{display:flex;flex-direction:column;gap:14px;width:100%}
#v-session .tbl{width:100%;max-width:100%}
#v-session .tbl{background:var(--lz-surface);border:1px solid var(--lz-line);border-radius:12px;
  box-shadow:0 2px 4px rgba(28,27,25,.04),0 8px 24px rgba(28,27,25,.07);overflow:hidden}
#v-session .tbar{display:flex;align-items:center;gap:10px;padding:10px 15px;
  border-bottom:1px solid var(--lz-line)}
#v-session .tbar .t{font-size:13px;font-weight:650;color:var(--lz-ink)}
#v-session .tbar .c{font-family:var(--lz-mono);font-size:10.5px;color:var(--lz-sub);
  background:var(--lz-surface2);padding:1.5px 6px;border-radius:4px;margin-left:2px}
#v-session .chips{margin-left:auto;display:flex;gap:5px}
/* chip：筛选器。圆角 6px（不是胶囊）—— 原型是 6px，别改成 pill */
#v-session .chip{font-size:11px;padding:4.5px 10px;border-radius:6px;border:1px solid var(--lz-line);
  background:var(--lz-surface);color:var(--sp-ink2);cursor:pointer;user-select:none}
#v-session .chip:hover{border-color:var(--lz-faint)}
#v-session .chip.on{background:var(--lz-ink);color:#fff;border-color:var(--lz-ink);font-weight:600}

/* ⚠️ 表头与数据行共用同一个 grid —— 这样列天然对齐，不用两套宽度。
   之前原型 C 这里是两套，列会偏 2px，肉眼能看出来。 */
#v-session .thead,#v-session .tr{display:grid;
  grid-template-columns:1fr 96px 66px 62px;align-items:center;gap:12px}
#v-session .thead{padding:8px 15px;background:var(--lz-surface);
  border-bottom:1px solid var(--lz-line);font-size:10.5px;color:var(--lz-sub);
  letter-spacing:.03em}
#v-session .tr{padding:10px 15px;border-bottom:1px solid var(--lz-line);cursor:pointer;
  transition:background var(--duration-fast) var(--ease-out)}
#v-session .tr:last-child{border-bottom:none}
#v-session .tr:hover{background:var(--lz-surface2)}
#v-session .tr.on{background:var(--sp-ibg)}
#v-session .tr .ti{min-width:0}
#v-session .tr .tt{font-size:13.5px;font-weight:600;color:var(--lz-ink);
  white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
#v-session .tr.on .tt{color:var(--sp-indigo)}
#v-session .tr .mt{font-size:11px;color:var(--lz-sub);margin-top:1px;
  white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
/* 来源标签：浅底深字，5px 圆角（原型是 5px） */
#v-session .src{font-size:10px;padding:2.5px 7px;border-radius:5px;font-weight:600;
  display:inline-block}
#v-session .src.wb{background:#eef0fd;color:#4338ca}
#v-session .src.zc{background:#f0fdf4;color:#0f766e}
#v-session .src.qd{background:#fef3c7;color:#b45309}
#v-session .src.ot{background:var(--d2);color:var(--sp-ink2)}
/* 输出强度条：5 个等高小方块，深色=有产出，浅灰=空，加密会话全灰 */
#v-session .out{display:flex;gap:2px;justify-content:flex-end}
#v-session .out i{width:8px;height:8px;border-radius:2px;background:var(--line);display:block}
#v-session .out i.f{background:var(--sp-indigo);opacity:.8}
#v-session .out.empty i{background:var(--line);opacity:.55}
#v-session .lk{display:inline-block;font-size:9.5px;font-weight:500;color:var(--sp-coral);
  border:1px solid currentColor;border-radius:3px;padding:0 4px;margin-right:7px;
  vertical-align:1.5px;letter-spacing:.02em}
#v-session .tm{font-family:var(--mono);font-size:10.5px;color:var(--sub)}

/* 右栏：两个框，12px 圆角 */
#v-session .box{background:var(--d1);border:1px solid var(--line);border-radius:12px;
  box-shadow:var(--shadow-2xs);overflow:hidden}
#v-session .box .bh{display:flex;align-items:baseline;gap:7px;padding:11px 15px 9px}
#v-session .box .bh .t{font-size:12.5px;font-weight:650;color:var(--ink)}
#v-session .box .bh .n{font-family:var(--mono);font-size:10.5px;color:var(--sub);margin-left:auto}
#v-session .box .bd{padding:0 15px 13px}
/* 堆叠条：按来源平台分段 */
#v-session .stack{display:flex;height:8px;border-radius:4px;overflow:hidden;background:var(--d2)}
#v-session .stack i{display:block;height:100%}
#v-session .lgs{display:grid;grid-template-columns:1fr 1fr;gap:7px 12px;margin-top:11px}
#v-session .lg{display:flex;align-items:center;gap:6px;font-size:11.5px;color:var(--sp-ink2)}
#v-session .lg .sw{width:8px;height:8px;border-radius:3px;flex:none}
#v-session .lg .nm{min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
#v-session .lg .v{font-family:var(--mono);font-size:11px;color:var(--ink);margin-left:auto}
#v-session .lg .p{font-family:var(--mono);font-size:10.5px;color:var(--faint);width:32px;text-align:right}
/* 等你处理：左竖条表状态类别，右侧数字 */
#v-session .td{display:flex;align-items:center;gap:10px;padding:9px 0;
  border-bottom:1px solid var(--line)}
#v-session .td:last-child{border-bottom:none}
#v-session .td .bar2{width:3px;height:26px;border-radius:2px;flex:none}
#v-session .td .tx{flex:1;min-width:0;font-size:12px;color:var(--sp-ink2);
  white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
#v-session .td .tx b{color:var(--ink);font-weight:600}
#v-session .td .n{font-family:var(--mono);font-size:15px;font-weight:700;flex:none}
#v-session .empty{padding:30px 15px;text-align:center;color:var(--faint);font-size:12px}


/* ── 会话详情 · 独立页面（2026-10-06 用户拍板方案 2）─────────────────────
   ⚠️ 尺寸/间距严格照抄定稿原型 p5-final/detail.html，不要凭手感改。
      用户 2026-10-06 明确要求：定稿后的设计不许为了修 bug 自动放大/缩小。
   视觉规范来自 oil-ui/references/visual-language.md：
      · 一页一个主角 → 原文大标题是唯一强强调，其余退后
      · 容器只在确有分组时出现 → 对话不用卡片，靠留白 + 细线分隔
      · 正文行长服务阅读 → max-width 840px，行高 1.78 */
/* 顶栏：品牌标 + 面包屑在左，动作在右（方案 2 的排法）。
   ⚠️ 品牌标直接复用工具栏那个 .sbrand（见 1378 行注释），
      不另造一套 —— 同一页面里两个品牌标样式不一致会很刺眼。
   ⚠️ .sbrand 自带 border-right 当分隔线，间距给到 13px（sd-top 的 gap）就够，
      不要再多加 padding，那会让左边这块显得比右边重。 */
/* 详情页整页暖白底（2026-10-06 用户明确要暖白，不能是冷灰） */
#v-sdetail{background:var(--sp-bg)}
/* 详情页动作条 #sd-bar（2026-10-07）
   ⚠️ 原来这些按钮在 #v-sdetail 自己的 .sd-top 里，而全局 .toolbar
      本来就在所有 section 外面 —— 屏幕上两行 LOCI 忆宫上下叠着。
      原型是一页一个顶栏，所以搬到全局顶栏里，用 hidden 控制显隐。
   ⚠️ hidden 必须写 display:none!important —— .toolbar 是 flex 容器，
      一旦被 display:flex 之类的规则命中，hidden 就失效（项目里踩过）。 */
#sd-bar{display:none;align-items:center;gap:8px;margin-left:10px}
#sd-bar:not([hidden]){display:flex}
#sd-bar[hidden]{display:none!important}

/* 面包屑「会话 › 当前篇」
   ⚠️ 2026-10-07 用户拍板：留着它，但规格改成原型的 .backbtn ——
      白底 + 1px 描边 + 8px 圆角 + var(--sh)，12px 字。
      原来写的是裸文字 + hover 变底色（没有描边），跟原型两处都不一样。 */
/* .sd-crumb 的 CSS 已随 DOM 一起删除（2026-10-07，原型无面包屑）。 */
/* 原型 .copybtn「复制原文」：11px 小号描边按钮，比 .btn 的 13px 小一号。
   原型是这一栏里唯一的小按钮 —— 复制是次要动作，返回才是主路径。 */
.copybtn{font-family:var(--lz-sans);font-size:11px;padding:3px 9px;border-radius:5px;
  border:1px solid var(--lz-line);background:var(--lz-surface);color:var(--lz-ink2);
  cursor:pointer;box-shadow:var(--lz-sh);white-space:nowrap}
.copybtn:hover{background:var(--lz-surface2)}
/* 原型 .backbtn「返回会话列表」：带左箭头的描边按钮，12px 字。 */
.backbtn{display:inline-flex;align-items:center;gap:7px;font-family:var(--lz-sans);
  font-size:12px;padding:6.5px 12px;border-radius:8px;
  border:1px solid var(--lz-line);background:var(--lz-surface);color:var(--lz-ink);
  cursor:pointer;box-shadow:var(--lz-sh);white-space:nowrap}
.backbtn:hover{background:var(--lz-surface2)}
.backbtn:focus-visible{outline:2px solid var(--lz-indigo);outline-offset:2px}
/* 会话头：原文大标题是这一页唯一的主角
   ⚠️ 2026-10-07 照 proto/会话详情.html 逐条对齐：
      padding 6px 4px 20px → **16px 4px 22px**（原型 .chead）
      h2 23px/1.35/mb 11px → **24px/1.3/mb 10px**（原型定稿，详情页标题比列表页大 2px） */
.sd-chead{padding:16px 4px 22px;border-bottom:1px solid var(--sp-line)}
.sd-chead h2{font-size:24px;font-weight:680;line-height:1.3;margin-bottom:10px;
  letter-spacing:-.018em;color:var(--sp-ink)}
/* 元信息一行：来源 Agent / 明文加密 / 时间 / 轮数 / 项目 */
.sd-meta{display:flex;align-items:center;gap:9px;flex-wrap:wrap;font-size:11.5px;color:var(--sp-sub)}
.sd-meta .sep{color:var(--sp-line)}
/* 来源标签：浅底深字，**不用 --acc 当底色**（纯蓝底 + 蓝字会糊成一团，
   实测过）。配色跟列表页 #v-session .src 完全一致，保证两页观感统一。 */
.sd-src{font-size:11px;padding:3px 9px;border-radius:5px;font-weight:600}
.sd-src.wb{background:#eef0fd;color:#4338ca}
.sd-src.zc{background:#f0fdf4;color:#0f766e}
.sd-src.qd{background:#fef3c7;color:#b45309}
.sd-src.ot{background:var(--sp-surface2);color:var(--sp-ink2)}
.sd-st{font-size:11px;padding:3px 8px;border-radius:5px}
.sd-st.plain{background:#ecfdf5;color:var(--ok)}
.sd-st.lock{background:#fdf2f4;color:var(--danger)}

/* 正文：整页对话原文。**不加 tab、不加「抽出的记忆」区**
   ——用户：「从这段对话中抽出了7条记忆…给人看没有意义」
   ⚠️ 宽度分两层：分隔线通栏（.sd-thread 不限宽），文字内容限宽 840px。
      整条线都到右边会显得页面歪着，只让线到 840 又跟上方会话头的
      细线对不齐 —— 现在的做法是线跟着内容宽度，两侧留白对称。 */
/* ⚠️ 2026-10-07 宽度归属修正：原型是 .thread{max-width:840px} —— 限宽在**整列容器**上。
   之前写在 .sd-msg（每条消息）上，容器仍通栏（实测 1400px）。数值 840 未改，只搬家。 */
.sd-thread{padding-top:6px;max-width:840px}
.sd-msg{padding:22px 0;border-bottom:1px solid var(--sp-line);display:flex;gap:15px}
.sd-msg:last-child{border-bottom:none}
/* 头像
   ⚠️ 2026-10-07 照原型改：`.av.u` 是**正圆 + 白色人形 SVG**，
      原来写的是 9px 圆角方块里塞一个「你」字 —— 那是我自己发明的。
      `.av.a` 保持 9px 圆角方块 + 「AI」字（原型就是这样，不对称是故意的：
      人在圆里、AI 在方里，一眼分得开谁是提问方）。 */
.sd-av{width:30px;height:30px;border-radius:9px;flex:none;display:flex;align-items:center;
  justify-content:center;font-size:11px;font-weight:700;font-family:var(--mono)}
.sd-av.u{background:var(--sp-ink);color:#fff;border-radius:50%;font-size:0}
.sd-av.u svg{width:17px;height:17px;fill:none;stroke:#fff;stroke-width:1.7;
  stroke-linecap:round;stroke-linejoin:round}
.sd-av.a{background:var(--r3-indigo-bg);color:var(--r3-indigo)}   /* 浅底深字 */

.sd-mb{flex:1;min-width:0}
.sd-who{font-size:11px;color:var(--sp-sub);margin-bottom:8px;display:flex;align-items:baseline;gap:8px}
.sd-who .tm{font-family:var(--mono);font-size:10.5px;color:var(--sp-faint)}
/* 正文 14px/1.78 —— visual-language.md：产品界面正文 14–16px，行高 1.6–1.75 */
.sd-tx{font-size:14px;line-height:1.78;color:var(--sp-ink);overflow-wrap:anywhere}
.sd-tx code{font-family:var(--mono);font-size:12.5px;background:var(--sp-surface2);
  padding:1.5px 5px;border-radius:4px}
.sd-tx .ln{display:block;margin-top:12px;padding-left:14px;
  border-left:2px solid var(--sp-line);color:var(--sp-ink2)}
.sd-tx p+p{margin-top:12px}
.sd-tx pre{font-family:var(--mono);font-size:12.5px;background:var(--sp-surface2);
  padding:12px 14px;border-radius:8px;overflow-x:auto;margin-top:12px;line-height:1.6}
/* 末尾标记：多留 24px，别贴着底部导航坞 */
.sd-end{text-align:center;padding:30px 0 26px;color:var(--sp-faint);font-size:11.5px;
  max-width:840px}

/* 加密会话：拿不到原文是**能力限制**（数据源本来就没有），不是"不给看" */
.sd-locked{background:var(--surface);border:1px solid var(--sp-line);border-radius:12px;
  padding:34px 28px;text-align:center;box-shadow:var(--shadow-2xs)}
.sd-locked .lic{width:44px;height:44px;border-radius:12px;background:#fdf2f4;
  display:flex;align-items:center;justify-content:center;margin:0 auto 14px}
.sd-locked .lic svg{width:20px;height:20px;stroke:var(--danger);fill:none;stroke-width:1.7;
  stroke-linecap:round;stroke-linejoin:round}
.sd-locked h3{font-size:15px;font-weight:600;color:var(--sp-ink);margin-bottom:7px}
.sd-locked p{font-size:13px;color:var(--sub);line-height:1.7;max-width:420px;margin:0 auto}






/* 搜索框聚焦发光（有细节但不刺眼） */
.toolbar input:focus,.textin:focus{box-shadow:0 0 0 3px var(--ring-soft)}

/* 顶部细进度条（长任务时出现） */
#topbar{position:fixed;left:0;top:0;height:2px;width:0;z-index:99;
  background:var(--grad-progress);transition:width .3s var(--ease),opacity .3s}
#topbar.on{width:92%;opacity:1}
#topbar.done{width:100%;opacity:0}

/* Toast */
#toast{position:fixed;right:22px;top:18px;z-index:120;display:flex;flex-direction:column;gap:8px}
.toast-item{
  background:var(--toastbg);border:1px solid var(--line2);border-radius:8px;
  padding:10px 14px;font-size:12.5px;color:var(--ink);max-width:340px;
  animation:toastIn .26s var(--ease) both;box-shadow:0 8px 26px rgba(0,0,0,.42)
}
.toast-item.ok{border-color:rgba(48,209,88,.5)}
.toast-item.err{border-color:var(--danger-soft)}

/* 加载骨架（列表区） */
.skeleton{height:56px;border-radius:12px;margin-bottom:10px;
  background:linear-gradient(90deg,var(--d2) 25%,var(--d3) 37%,var(--d2) 63%);
  background-size:440px 100%;animation:shimmer 1.1s linear infinite}
.busy{animation:pulse 1.1s ease-in-out infinite}

/* ───────── 主色体系（Trae 稿：平面、无装饰光晕）───────── */
.sbrand h1{color:var(--ink)}
.sbrand p{color:var(--sub);letter-spacing:.2px}


.lead{color:var(--sub)}

/* 卡片：hover 带主色描边，更强的层次 */
.mem{border-radius:8px}
/* 卡片 hover：规范 ui_kit 的做法是 border-color 变 --ring，不加抬升（阴影全平） */
/* FIX-4：hover 描边原来吃 --ring —— 暗色下 --ring 是纯白（14.91:1）、亮色才是蓝（3.56:1），
   两个主题成了两种视觉语言；而且暗色下它与焦点环**同色同宽**，hover 和 focus 分不开。
   规范第四节的口径是「hover 只动底色，不加描边」，所以改用底色表达，焦点仍走 outline。 */
.mem:hover{background:var(--hover);border-color:var(--line);box-shadow:none}


/* 进度条改用主色 → 辅色 */
#topbar{background:linear-gradient(90deg,var(--acc),var(--acc2))}

/* 搜索框 / 输入框聚焦：金色环 */
.toolbar input:focus,.textin:focus{border-color:var(--ring);box-shadow:0 0 0 3px var(--ring-soft)}

/* 常驻标记 / 关键标签用主色 */
.accent{color:var(--acc)}
.badge-new{color:var(--acc2)}


/* ───── 侧栏图标 / 主从双栏 ───── */
.ico{width:16px;height:16px;flex-shrink:0;opacity:.85}
.nav.on .ico{opacity:1}

/* ── #health（质检页那张健康卡的容器）────────────────────────
   2026-09-27：它原本是 grid 两列（auto minmax(0,1fr)），配套旧结构「左列圆环仪表
   `.hgauge` + 右列扣分条 `.hbars`」。renderHealth() 后来改成只吐一个 `.hcard`，
   容器却没跟着改 → 卡片只占第一列、右边整列空着（用户截图报的"只填了一边"），
   而且 `.hcard` 自带卡片底套在外层卡片底里还成了"卡中卡"。
   现在它只是个占满整行的普通容器，外观全交给内层 `.hcard`。
   2026-09-28：旧结构那批样式（.hgauge/.hring/.hval/.hbars/.hrow/.hbar/.hchips/.hchip）
   已按修正边界删干净（`.hgrade` 的颜色/描边迁到了 `.hcard .score .hgrade`）——
   这里只留下面这一条容器规则。 */
.health{display:block;background:none;border:0;padding:0;margin-bottom:0}

/* Agent 徽标：品牌色圆角方块 + 首字母（不用厂商 logo：零依赖 + 避免商标问题） */
.abadge{display:inline-flex;align-items:center;justify-content:center;
  border-radius:7px;color:#fff;font-weight:600;line-height:1;flex-shrink:0;
  letter-spacing:0;user-select:none}

/* 退出服务按钮：平时低调，悬停变警示色。
   ⚠️ 2026-10-07 照 proto/会话列表.html 的 .quitbtn 补齐边框与底色 ——
      原型是「白底 + 1px 描边 + 8px 圆角 + var(--sh)」的方形按钮，
      原来裸着一层 .btn 的边框和 padding，在这一栏里跟邻座按钮不是一个东西。 */
.quitbtn{display:flex;align-items:center;justify-content:center;
  width:32px;height:32px;padding:0;flex:none;
  border:1px solid var(--lz-line);border-radius:8px;
  background:var(--lz-surface);color:var(--lz-sub);
  font-family:inherit;box-shadow:var(--lz-sh);cursor:pointer}
.quitbtn:hover{color:#e11d48;border-color:#f0c4cd;background:#fdf2f4}

/* 关闭后的提示页 */
.bye{display:grid;place-items:center;height:100vh;text-align:center;gap:14px;
  font-family:-apple-system,"PingFang SC","Microsoft YaHei",sans-serif}
.bye img{width:96px;opacity:.9}
.bye h2{font-size:20px;font-weight:600;margin:0}
.bye p{color:var(--sub);font-size:13.5px;margin:0;line-height:1.9}
.bye code{background:var(--d3);border-radius:6px;padding:2px 7px;font-size:12.5px}

/* 主题切换按钮
   ⚠️ 2026-10-07 原型顶栏里**没有**这个按钮（只有 搜索 / 重新扫描 / ⏻）。
      但暗色模式是已上线的功能，不是样式问题，所以不删 ——
      只把规格对齐旁边的 ⏻，免得一个 34px 一个 32px 看着不齐。
      如果要跟原型完全一致，删掉 HTML 里 id="themetgl" 那个按钮即可。 */
.themetgl{width:32px;height:32px;flex-shrink:0;display:grid;place-items:center;
  background:var(--lz-surface);border:1px solid var(--lz-line);border-radius:8px;
  color:var(--lz-sub);cursor:pointer;box-shadow:var(--lz-sh);
  transition:color .18s var(--ease),border-color .18s var(--ease)}
.themetgl:hover{color:var(--lz-indigo);border-color:#c9c8f5}
.ml-auto{margin-left:auto}

/* 主从双栏（列表 + 详情） */
.split{display:grid;grid-template-columns:clamp(360px,33%,460px) minmax(0,1fr);gap:20px;align-items:start}
/* ⚠️「详情栏空着就不占格子」（`.split.solo` 塌单列）—— **2026-09-24 用户拍板撤销，别再改回来。**
   撤销理由：本项目所有主从页都是"左边列表 + 右边详情"，不存在单列形态；
   塌单列会让两栏宽度在"选没选中"之间从 1160px 猛跳到 383px，比空着更晃眼。
   现行做法：**详情栏常驻**，未选中时里面渲染一张引导卡（`.split-side .guide`），
   三处共用同一套样式 —— 记忆 #detail / 会话 #s-guide / 技能 #sk-detail。 */
.split-main{min-width:0;background:var(--card);border:1px solid var(--line);
  border-radius:var(--radius-lg);box-shadow:var(--shadow-2xs);overflow:hidden;
  animation:cardIn .3s var(--ease) both}
/* 左列卡片头／体 —— 对齐 Trae 稿的 .card-header / .card-body */
.split-main .shead{display:flex;align-items:center;justify-content:space-between;gap:12px;
  padding:var(--space-4) var(--space-5);min-height:52px;border-bottom:1px solid var(--line)}
.split-main .shead .t{font-size:14px;font-weight:600;letter-spacing:-.1px;color:var(--ink)}
/* 标题栏右侧的计数常常很长（"40 个 skill | 14 个配置文件 | 3 个 MCP | 55 个插件 …"），
   不截断会把标题栏撑成两行、看着像"排列不准"（用户报的 bug）。
   标题自己不缩；计数占剩下的宽度，超出用省略号。 */
.split-main .shead .t{flex-shrink:0}
.split-main .shead .hint{flex:1;min-width:0;overflow:hidden;text-overflow:ellipsis;
  white-space:nowrap;margin:0;text-align:right}
.split-main .sbody{padding:var(--space-1) 0}
/* 搜索结果是平铺列表（无分组），行距由 `.lrow + .lrow` 统一给 */
.split-main .handoff-out{margin:var(--space-3) var(--space-3) var(--space-2)}
/* 右栏里的输出区（会话页的原文时间线 #s-view）已经在卡片里了，不该再长一层卡片 */
.split-side .handoff-out{background:transparent;border:0;padding:0;margin:0;
  color:var(--ink);white-space:pre-wrap}
.split-side{
  position:sticky;top:14px;background:var(--card);border:1px solid var(--line);
  border-radius:var(--radius-lg);padding:0;box-shadow:var(--shadow-2xs);
  min-height:0;
  animation:cardIn .3s var(--ease) both
}
/* 详情卡头／体 —— 对齐 Trae 稿 .detail-header / .detail-body（均 20px 内边距，头下一条分隔线） */
.split-side .dhead{padding:var(--space-5);border-bottom:1px solid var(--line)}
.split-side .dmain{padding:var(--space-5)}
/* 空态引导卡 —— **三处主从骨架共用**（记忆 #detail / 会话 #s-guide / 技能 #sk-detail）。
   未选中时详情栏常驻这张卡，把"右边为什么空着、该点哪里"说清楚；
   选中后由各自的渲染函数整体覆盖。
   2026-09-24 用户拍板：**不再有「空态塌单列」**，两栏宽度恒定。 */
.split-side .guide{display:flex;flex-direction:column;align-items:center;justify-content:center;
  text-align:center;padding:var(--space-10) var(--space-6);min-height:220px}
.split-side .guide .gic{width:52px;height:52px;border-radius:var(--radius-xl);
  background:var(--popover);display:flex;align-items:center;justify-content:center;
  margin-bottom:var(--space-4)}
.split-side .guide .gic svg{width:24px;height:24px;stroke:var(--faint);fill:none;
  stroke-width:1.5;stroke-linecap:round;stroke-linejoin:round}
.split-side .guide h4{margin:0 0 var(--space-2);font-size:14px;font-weight:600;
  color:var(--ink);letter-spacing:-.1px}
.split-side .guide p{margin:0;font-size:12.5px;color:var(--sub);line-height:1.65;max-width:320px}
.split-side .dhtop{display:flex;align-items:flex-start;justify-content:space-between;
  gap:var(--space-4)}
.split-side h3{font-size:16px;font-weight:600;line-height:1.4;margin:0;color:var(--ink)}
/* 卡头第二行：时间 · 平台 · 状态（对齐 Trae 稿的 .detail-meta + 图标） */
.split-side .dtop{display:flex;align-items:center;gap:var(--space-3);flex-wrap:wrap;
  margin-top:var(--space-2);font-size:12px;color:var(--sub)}
.split-side .dmi{display:inline-flex;align-items:center;gap:5px;white-space:nowrap}
.split-side .dmi svg{width:12px;height:12px;flex-shrink:0;opacity:.85}
/* 分区（对齐 Trae 稿 .detail-section / .detail-section-title / .tag） */
.split-side .dsec+.dsec{margin-top:var(--space-6)}
.split-side .dsec-t{font-size:11px;font-weight:600;letter-spacing:.06em;
  text-transform:uppercase;color:var(--sub);margin:0 0 var(--space-3)}
.split-side .dtags{display:flex;flex-wrap:wrap;gap:var(--space-2)}
.split-side .tag{display:inline-flex;align-items:center;padding:var(--space-1) var(--space-3);
  border:1px solid var(--line);border-radius:var(--radius-md);background:var(--muted);
  color:var(--ink);font-size:12px;transition:background var(--duration-fast) var(--ease-out),
  color var(--duration-fast) var(--ease-out)}
.split-side .tag:hover{background:var(--accent);color:var(--accent-foreground);
  border-color:transparent}
.split-side .dbody{font-size:14px;line-height:1.7;color:var(--ink);
  white-space:pre-wrap;word-break:break-word}
/* 技能/配置详情的正文要像"一块内容"（原来是透明无边框，看着像没有正文）。
   ⚠️ 不给内层高度限制 —— 之前那个 max-height 小窗是踩过的坑，别加回来。 */
#sk-text{background:var(--background);border:1px solid var(--line);
  border-radius:var(--radius-md);padding:var(--space-4);font-size:13px;line-height:1.7;
  white-space:pre-wrap;word-break:break-word}
/* 正文里的 `# 标题` 行原本被当成普通文字 —— 实测有 28 个现成的分段点全浪费了。
   由 JS 变成真标题，再在顶部给一条**横向**小节条：不占阅读宽度（不做右侧悬浮目录，
   797px 的右栏再切 180px 出去，中文一行会短到难读），对长文更友好。 */
#sk-text .sk-h{display:block;font-size:13px;font-weight:600;color:var(--ink);
  margin:var(--space-5) 0 var(--space-2);scroll-margin-top:80px}
#sk-text .sk-h:first-child{margin-top:0}
.sk-toc{display:flex;flex-wrap:wrap;align-items:center;gap:6px;
  margin:0 0 var(--space-3);padding-bottom:var(--space-3);
  border-bottom:1px solid var(--line);font-size:11.5px}
.sk-toc b{color:var(--faint);font-weight:500;flex-shrink:0}
.sk-toc a{color:var(--acc);text-decoration:none;border-bottom:1px dashed var(--acc)}
.sk-toc a:hover{color:var(--ink)}
/* 回到顶部：跟着真正的滚动容器 .content 走 */
/* ⚠️ 2026-10-07 删除「回到顶部」浮动按钮（#backtop）—— 原型里没有。
   实测 bug：position:fixed;bottom:22px 与底部坞**位置重叠**，滚过一屏就压在
   「技能 / MCP」上（用户截图已确认）。另两条硬伤：用了禁用的 border-radius:999px；
   且它出现在会话列表页，而列表页不是长文，给「回到顶部」没意义。 */
.split-side .dmeta{font-size:12px;color:var(--sub);line-height:1.9;word-break:break-all}
.split-side .dacts{display:flex;gap:8px;flex-wrap:wrap;margin:0;flex-shrink:0}
.mem.sel{border-color:var(--acc);box-shadow:0 0 0 1px var(--acc),0 8px 26px rgba(138,180,248,.22)}
.dempty{color:var(--faint);font-size:12.5px;line-height:1.9}
/* 记忆列表：可折叠分组（常驻 / 最近）—— 比例对齐 Trae 稿：
   分组头 12/16 内边距 + 6px 圆角、不画下划线；组间 16px、组内行间 2px */
.ghead{display:flex;align-items:center;gap:8px;cursor:pointer;user-select:none;
  font-size:13px;font-weight:600;color:var(--ink);
  margin:var(--space-4) 0 0;padding:var(--space-3) var(--space-4);border-radius:var(--radius-md);
  transition:background var(--duration-fast) var(--ease-out)}
.ghead:first-child{margin-top:0}
.ghead:hover{background:var(--hover)}
.ghead:hover .cv{color:var(--sel-accent)}
.ghead .cv{font-size:10px;color:var(--sub);
  transition:transform .2s var(--ease-out),color var(--duration-fast) var(--ease-out)}
.ghead.collapsed .cv{transform:rotate(-90deg)}
.ghead .cnt{margin-left:2px;font-size:11px;color:var(--sub);background:var(--muted);
  border-radius:999px;padding:1px 6px;line-height:15px}
.ghead::before{content:"";width:6px;height:6px;border-radius:50%;background:var(--acc);
  flex-shrink:0}
.ghead.g-recent::before{background:var(--faint)}
.gbody{display:flex;flex-direction:column;gap:2px;padding:var(--space-2) 0}
.gbody.hide{display:none}
/* 区域折叠沿用同一个 hide 语义：sbody（左列表）/ dmain（右详情）收起时整块不占位。
   ⚠️ 必须有这条 —— 之前只有 .gbody.hide，新增的容器带上 hide 类也「没反应」。 */
.sbody.hide,.dmain.hide{display:none}

/* 整块区域折叠（2026-09-22）：点标题栏收起整个区块 —— 收起的是**容器**，
   所以布局真的跟着收缩（.split 是 align-items:start，栏内一变矮那一栏就矮）。
   箭头用伪元素画在标题文字前，不额外占 DOM。 */
[data-secfold]{cursor:pointer;user-select:none}
/* FIX-8：箭头统一**放右侧** —— 原先是 `::before` 左前缀，而 .grphead 用右后缀，
   同一个东西两套位置（同类名 .ghead 在记忆页箭头在左、在本机内容页在右）。
   改用 ::after 追加到标题末尾。⚠️ [data-secfold] 只落在 .shead / .dhead 上，
   而它们内部只有 .t / h3 一个文本节点，所以 ::after 不会插到别的东西中间。 */
[data-secfold] .t::after,[data-secfold] h3::after{content:"▼";display:inline-block;
  font-size:10px;color:var(--sub);margin-left:var(--space-2);vertical-align:middle;
  transition:transform .15s ease}
[data-secfold].sec-collapsed .t::after,
[data-secfold].sec-collapsed h3::after{content:"▶"}
.shead[data-secfold]{border-radius:var(--radius-sm)}
.shead[data-secfold]:hover{background:var(--hover)}
/* 收起后在标题栏右侧补一条"内容第一行"，让收起态也有信息 */
.sec-1{flex:1;min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;
  font-size:11.5px;color:var(--faint);margin-left:var(--space-3);font-weight:400}

/* 徽章：列表 meta 行与详情卡头共用 */
.bdg{display:inline-flex;align-items:center;font-size:11px;line-height:15px;
  padding:1px 6px;border-radius:999px;background:var(--muted);color:var(--sub);
  flex-shrink:0;max-width:160px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.bdg.pin{background:var(--accent);color:var(--accent-foreground)}
.bdg.state{background:transparent;box-shadow:inset 0 0 0 1px var(--line)}
/* ↑ 描边用 inset 阴影而不是 border：border 会让这一枚徽章高 2px，
   整行随之从 61.19 变 63.19（批4 辛苦对齐的行高）*/

/* 主从分工：左侧列表 = 单行标题 + 一行 meta（≈61px，对齐 Trae 稿的 .memory-item）
   完整内容在右侧详情；类型用左侧圆点表示，项目/来源一律走 meta 行的小字。

   ⚠️ 紧凑行用**显式类 `.lrow` 标记**，不要挂在容器上（如 #s-list）。
   踩过两次同一个坑：#s-list 被四种内容复用 —— ① 会话列表（要紧凑行）
   ② 解析预览 ③ 抽取候选 ④ 原话检索（这三类都要**完整卡片**，正文要能读）。
   一开始把紧凑行写在容器上，②③④ 的正文全被压成一根细条（中文一字一行竖排）。 */
.split-main .mem.lrow{display:flex;align-items:flex-start;gap:var(--space-3);cursor:pointer;
  padding:var(--space-3) var(--space-4);border-radius:var(--radius-md);margin:0;
  background:transparent;border:0;box-shadow:none}
.split-main .mem.lrow:hover{transform:none;background:var(--hover);box-shadow:none}
.split-main .lrow + .lrow{margin-top:2px}
/* ── 记忆行的类型图标块（2026-09-24 改版）──
   原来是 8px 小圆点：只能表达"有颜色"，说不出是"哪一类"。
   换成 30px 圆角方块的图标（内联 SVG，颜色吃 --data-*），类型一目了然。
   ⚠️ 类名从 .mdot 改成 .mico（只有记忆行走 cardHtml()，会话行走另一套渲染，
      所以这个类名是记忆页独有的，改名不会波及 #s-list —— verify_slist_rows 量的是后者）。 */
/* 尺寸与布局（颜色兜底在低特异性的 .mico 里，见上面那段注释） */
.split-main .mem.lrow .mico{width:30px;height:30px;border-radius:var(--radius-md);
  flex:0 0 30px;display:grid;place-items:center}
.split-main .mem.lrow .mico svg{width:16px;height:16px}
/* ── 类型徽章 + 类型图标块（记忆列表行里的两处"彩色标记"）──
   底色走 --t-*-bg 令牌（亮暗各一份），文字/图形色复用 --data-*，
   所以两种主题下都保持"彩字 + 同色系浅底"，不会在暗色里发灰。
   ⚠️ 两者共用同一组变体规则（一条规则同时命中 .tb.fact 与 .mico.fact），
      避免"徽章改了、图标块忘了改"这种一半新一半旧的漂移；
      万一 mtype 是个没登记的值 → 没有变体命中 → 自动退回中性灰，不会画出空白块。 */
.tb{display:inline-flex;align-items:center;height:17px;padding:0 6px;border-radius:5px;
  font-size:10.5px;font-weight:600;line-height:1;flex:0 0 auto;
  background:var(--d3);color:var(--sub)}
/* ⚠️ .mico 的中性兜底必须写在这条**低特异性**规则里（0,1,0）。
      踩过的坑：把 background 写在下面那条 `.split-main .mem.lrow .mico`（0,3,1）里，
      特异性高过 `.mico.fact`（0,2,0）→ 图标块永远吃不到类型的 --t-*-bg，
      实测亮色下还是灰底 rgb(239,241,244)，只有徽章变了色（一半新一半旧）。
      现在把"尺寸/布局"放高特异性、"颜色兜底"放低特异性，变体规则才压得住。 */
.mico{background:var(--d3);color:var(--data-context)}
.tb.fact,.mico.fact{background:var(--t-fact-bg);color:var(--data-fact)}
.tb.decision,.mico.decision{background:var(--t-decision-bg);color:var(--data-decision)}
.tb.preference,.mico.preference{background:var(--t-preference-bg);color:var(--data-preference)}
.tb.skill,.mico.skill{background:var(--t-skill-bg);color:var(--data-skill)}
.tb.error,.mico.error{background:var(--t-error-bg);color:var(--data-error)}
.tb.context,.mico.context{background:var(--t-context-bg);color:var(--data-context)}
.tb.summary,.mico.summary{background:var(--t-summary-bg);color:var(--data-fact)}
.split-main .mbody{min-width:0;flex:1}
.split-main .mtitle{font-size:13px;font-weight:500;line-height:1.4;
  white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.split-main .mmeta{display:flex;align-items:center;gap:var(--space-2);margin-top:2px;
  font-size:12px;color:var(--sub);overflow:hidden;white-space:nowrap;line-height:1.4}
/* meta 行结构对齐 Trae 稿：时间（纯文字）· 平台（徽章）· 常驻/最近（徽章）· 标签 */
.split-main .mmeta .mtime{flex-shrink:0;font-variant-numeric:tabular-nums}
.split-main .mmeta .bdg{max-width:120px}
.split-main .mtags{overflow:hidden;text-overflow:ellipsis;min-width:0;color:var(--faint)}
/* 行内操作按钮：平时不占视线，hover / 选中才出现（同样的动作右侧详情里也有） */
/* ⚠️ opacity:0 依然占位 —— 实测这 115px 吃掉 meta 行 31% 宽，
   把「914 轮 · #81」压成一个「9」。改成悬浮在行尾，不占文档流。 */
.split-main .mem.lrow{position:relative}
/* ── FIX-1：行内三个操作改成**三个各自独立的框** ──
   原来 .macts 自带一块 --card 不透明底 + padding-left，而里面三个按钮既无边框也无底色
   （实测容器 127×16、底色 rgb(29,29,28)，按钮 border:0 / background:none / radius:0）
   → 眼睛看到的必然是"一条"。现在容器彻底透明，每个 .del 自己是一张迷你卡片，
   对齐库 Button 的 ghost/secondary 口径（"border-light action for table rows"）。 */
.split-main .macts{position:absolute;right:var(--space-4);top:50%;
  transform:translateY(-50%);margin:0;display:flex;gap:var(--space-2);
  background:transparent;padding:0;
  opacity:0;pointer-events:none;transition:opacity var(--duration-fast) var(--ease-out)}
/* ★ 选中态原来另有一条 --sel-bg 共享底 —— 也要关掉，否则选中那行仍然是"一条" */
.split-main .mem.lrow.sel .macts{background:transparent}
.split-main .mem.lrow:hover .macts,.split-main .mem.lrow.sel .macts{opacity:1;pointer-events:auto}
/* 每个操作 = 一张迷你卡片：--card 底 + --line 描边 + 6px 圆角（规范 2.3 按钮圆角）
   24px 高 = WCAG 2.5.8 的最小点击目标；平时中性色，只有 hover 才出警示色 */
.split-main .macts .del{display:inline-flex;align-items:center;justify-content:center;
  height:24px;padding:0 var(--space-2);
  border:1px solid var(--line);background:var(--card);
  border-radius:var(--radius-md);
  font-size:11px;line-height:1;color:var(--sub);white-space:nowrap}
.split-main .macts .del:hover{color:var(--bad);border-color:var(--bad)}

/* ── FIX-2：hover 时不再压断 meta 行 ──
   上一版是"整行砍 150px"：实测 .mbody 从 348.8 掉到 214.8，而 meta 需要 267.2
   → 差 52.4px，末尾两段被省略号切掉（用户截图里的「暂无…」「753 轮 ·…」就是这个）。
   换算过：三个框要 169px，完整 meta 要 259.2px，加起来 428.2 > 可用的 348.8 → 不可能共存。
   三条路里「叠上去」和「压成省略号」都被否了，只剩"让位"；让位的对象只能是那两个
   次要标签（让掉 121.4px 后 137.8+169=306.8 ≤ 348.8，留 42px 净空）。 */
.split-main .mem.lrow:hover,
.split-main .mem.lrow.sel{padding-right:var(--space-4)}      /* 恢复 16px，别再砍整行 */
.split-main .mem.lrow:hover .mtitle,
.split-main .mem.lrow.sel .mtitle{padding-right:177px}       /* 标题本来就 nowrap+省略号 */
.split-main .mem.lrow:hover .mmeta,
.split-main .mem.lrow.sel .mmeta{padding-right:169px}        /* 双保险，防 meta 溢出 */
.split-main .mem.lrow:hover .mtags,
.split-main .mem.lrow.sel .mtags{display:none}               /* 收起会被挤成半截的两个标签 */
.split-main .mem.lrow.sel{background:var(--sel-bg);box-shadow:inset 3px 0 0 var(--sel-accent)}
.split-main .mem.lrow.sel:hover{background:var(--sel-bg)}
.split-main .mem.lrow.sel .mtitle{color:var(--sel-ink);font-weight:600}
.split-main .mem.lrow.sel .mmeta{color:var(--sel-ink);opacity:.8}
/* 选中行里图标块的写法（原来是给 8px 圆点套一圈 --sel-bg 让它从蓝底上"抠出来"）。
   30px 的图标块自带类型底色，**底色保留** —— 和同一行里的类型徽章保持一致，
   否则会出现"徽章有颜色、图标块变透明"的一半新一半旧。
   只把字形换成 --sel-ink，保证压在蓝色选中底上仍然读得清（明暗两主题都成立）。 */
.split-main .mem.lrow.sel .mico{color:var(--sel-ink)}

/* ── 选中底统一（2026-09-23，用户先后报了两次同一件事）──
   第 1 次：黑夜下点列表项，只有**记忆页**有蓝色选中底（`--sel-bg`）；会话页和本机内容页
     点下去只剩 hover 的那层淡灰，像"没有反馈"。根因：`.sel` 只由 `selectMem()` 打在
     `#memcard-*` 上，别的页从来没人打过标记。
   第 2 次：**质检页**（`#audit-out`）和**清理页**（`#sf-list` / `#cm-list` / `#bk-list`）
     点下去连 hover 停留感都很弱（只有那一层 0.18s 的动画），那三个容器压根不在
     `.split-main` 里 —— 所以第一版只写在 `#sk-list` 上的规则完全没覆盖到它们。
   现在：JS 侧 `markRowSel` 的委托监听已覆盖全部列表容器；CSS 这里用**同一套令牌**
   （`--sel-bg` / `--sel-accent` / `--sel-ink`）统一，不再按页写死选择器。
   ⚠️ `:not(.lrow)` 是有意的：主从页的紧凑行（`.mem.lrow`）自己有上面那组规则
   （还带 hover 让位、隐藏标签等联动），这里只管"卡片行"（质检 / 清理 / 本机内容 / 采集）。 */
.mem.sel:not(.lrow),.mem.sel:not(.lrow):hover{
  background:var(--sel-bg);border-color:transparent;
  box-shadow:inset 3px 0 0 var(--sel-accent)}
.mem.sel:not(.lrow) .content,.mem.sel:not(.lrow) .content b,
.mem.sel:not(.lrow) .meta,.mem.sel:not(.lrow) .proj,
.mem.sel:not(.lrow) .score,.mem.sel:not(.lrow) .accent{
  color:var(--sel-ink)}
.mem.sel:not(.lrow) .ttag,.mem.sel:not(.lrow) .top{color:var(--sel-ink)}

/* 右栏里的内容卡（会话页的抽取候选）—— 套规范 ui_kit 的 .mini-card：
   用 --background 而不是 --card，避免"卡里再套一张卡"看着发糊 */
.split-side .mem{background:var(--background);border:1px solid var(--line);
  border-radius:var(--radius-md);padding:var(--space-3) var(--space-4);
  margin-bottom:var(--space-2)}
.split-side .mem .content{font-size:13px;line-height:1.7;white-space:normal}

/* 会话原文气泡（右栏时间线）—— 我靠右、AI 靠左，与库里 --accent 的用法一致 */
.bub{max-width:80%;padding:var(--space-3) var(--space-4);border-radius:var(--radius-lg);
  font-size:13px;line-height:1.75;white-space:pre-wrap;word-break:break-word;
  margin-bottom:var(--space-3)}
.bub .btag{display:block;font-size:11px;opacity:.65;margin-bottom:var(--space-1)}
.bub.me{background:var(--accent);color:var(--accent-foreground);margin-left:auto;
  border-bottom-right-radius:var(--radius-sm)}
.bub.ai{background:var(--muted);color:var(--ink);
  border-bottom-left-radius:var(--radius-sm)}
.bub.raw{background:transparent;border:1px dashed var(--line);color:var(--sub);max-width:100%}

/* 长文折叠（2026-09-22）：会话原文 / 记忆长内容 / 交接卡输出。
   只有超过阈值才折（短内容不出现按钮），收起时按行截断，点「展开全文」看全。
   —— 用户反馈：几百轮的会话原文一条条铺下来太长，页面拉不到底。 */
/* P2-7：正文块的标签原来是 11px / uppercase / --sub，比它标注的 13px 正文还小 ——
   最大的一块内容配了最小的标签。回正到正文之上。（uppercase 对中文也没意义） */
.split-side .dsec-t{font-size:12.5px;letter-spacing:0;text-transform:none;
  color:var(--sub);font-weight:600}
/* P2-3：CSS 的 6 是死值，JS 里另有 FOLD_LINES=4 覆盖它。让 CSS 成为唯一真值。 */
.foldbody.folded{display:-webkit-box;-webkit-box-orient:vertical;
  -webkit-line-clamp:var(--fold-lines,4);overflow:hidden}
/* P2-2：区域收起后，栏头那条 border-bottom 会留成一条孤线 */
.split-main:has(.sbody.hide) .shead,
.split-side:has(.dmain.hide) .dhead{border-bottom-color:transparent}
.foldbtn{display:inline-block;margin:0 0 var(--space-2);font:inherit;font-size:12px;
  color:var(--acc);background:transparent;border:0;padding:2px 0;cursor:pointer}
.foldbtn:hover{text-decoration:underline}

/* 会话页右栏装的是结构化 HTML（气泡 + 记忆芯片），不能继承 .handoff-out 的
   white-space:pre-wrap —— 那会把拼接 HTML 时留下的空白也渲染成空行。 */
#s-view{white-space:normal}

/* 贴在某轮气泡后面的「产出记忆」芯片 —— 会话层与记忆层的接缝就在这里。
   点一下跳到记忆页并选中那一条；反过来记忆卡的「出处」也能跳回这一轮。 */
.tmem{display:flex;flex-wrap:wrap;align-items:center;gap:6px;margin:-6px 0 var(--space-4)}
.tmem-k{font-size:11px;color:var(--faint);letter-spacing:.04em;flex-shrink:0}
.tchip{display:inline-flex;align-items:center;gap:5px;max-width:330px;cursor:pointer;
  padding:3px 10px;border-radius:var(--radius-full,999px);font-size:11.5px;line-height:1.6;
  background:var(--muted);border:1px solid var(--line);color:var(--sub);
  transition:background var(--duration-fast) var(--ease-out)}
.tchip:hover{background:var(--hover);color:var(--ink)}
.tchip i{width:6px;height:6px;border-radius:50%;flex:0 0 6px}
.tchip b{font-weight:500;flex-shrink:0;font-variant-numeric:tabular-nums}
.tchip-t{overflow:hidden;text-overflow:ellipsis;white-space:nowrap;min-width:0}

/* 记忆列表行里的「会话出处」徽章（可点，跳回原话）。
   描边同样用 inset 阴影 —— 用 border 会把行高顶破 2px，跟上面 .bdg 一个道理。 */
.split-main .mmeta .bdg.src{background:transparent;color:var(--acc);
  box-shadow:inset 0 0 0 1px var(--acc);cursor:pointer}
.split-main .mmeta .bdg.src:hover{background:var(--muted)}
@media (max-width:1150px){
  .split{grid-template-columns:1fr}
  .split-side{position:static;min-height:auto;max-height:none;overflow:visible}
}
/* 窄窗口：先隐标签、再隐统计（顶栏还有搜索框与两个按钮要放） */
@media (max-width:1120px){
  .stats{gap:16px}
  .stat .k{display:none}
}
@media (max-width:900px){
  .stats{display:none}
  .toolbar{flex-wrap:wrap;gap:8px}
  .toolbar input{max-width:none}
}

/* 无障碍：尊重系统「减少动态效果」 */
@media (prefers-reduced-motion: reduce){
  *,*::before,*::after{animation-duration:.001ms!important;animation-iteration-count:1!important;
    transition-duration:.001ms!important}
}

/* ══════════════════════════════════════════════════════════════════
   R3 · 会话列表页 —— 照 proto/会话列表.html 从零重做（2026-10-07）
   ══════════════════════════════════════════════════════════════════
   本块之前的 CSS 属于旧工作台，不作为本页的依据。
   下面的选择器、属性、数值全部从 proto/会话列表.html 抄过来，
   只在前面加 #v-session 作用域（原型是独立 HTML，没有作用域）。

   ⚠️ 作用域必须带。panel.py 里 .main/.side/.box/.tbl 这些通用名已被
      旧工作台占用（.side 是底部胶囊坞，position:fixed）——
      不加作用域，本页的 .side 会被钉到屏幕中央。
   ⚠️ 本页不引用 --d0..--d3 / --acc / --ink / --sub / --line（那是旧色板），
      也不用 --lz-* 的简写：一律用下面 :root[data-theme="light"] 里的
      原型变量，语义与原型一一对应。 */

/* ── 原型 :root 的色板，原样搬过来（变量名加了 r3- 前缀防撞） ── */
:root{
  --r3-bg:#faf9f7; --r3-surface:#ffffff; --r3-surface2:#f3f1ec; --r3-line:#e4e0d8;
  --r3-ink:#1c1b19; --r3-ink2:#4e4b45; --r3-sub:#857f74; --r3-faint:#aaa49a;
  --r3-indigo:#4338ca; --r3-indigo-bg:#eef0fd;
  --r3-coral:#e11d48; --r3-amber:#b45309; --r3-ok:#047857;
  --r3-teal:#0f766e; --r3-violet:#7c3aed;
  --r3-mono:"JetBrains Mono","SF Mono",Consolas,monospace;
  --r3-sans:"DM Sans",-apple-system,"PingFang SC","Microsoft YaHei",sans-serif;
  --r3-sh:0 2px 4px rgba(28,27,25,.04),0 8px 24px rgba(28,27,25,.07);
  --r3-sh-lg:0 4px 8px rgba(28,27,25,.05),0 16px 48px rgba(28,27,25,.11);
}
/* 页面底：原型 body{background:var(--bg)} = #faf9f7。
   旧工作台把 .app 铺了 d1 白底，会从 .content 的间隙里透出来，这里压掉。 */
#v-session{background:var(--r3-bg);color:var(--r3-ink);
  font-family:var(--r3-sans);font-size:13px;line-height:1.45}

/* ── 原型 .phead ── */
#v-session .phead{display:flex;align-items:flex-end;gap:20px;padding:14px 4px 20px}
#v-session .phead .ttl h2{font-size:22px;font-weight:680;letter-spacing:-.015em;
  line-height:1.2;color:var(--r3-ink)}
#v-session .phead .psub{font-size:11.5px;color:var(--r3-sub);margin-top:6px}
/* 原型 .ttl h2 —— 会话列表的标题 22px。
   上面拆成 .phead .ttl h2 是为了压过全局 h2{font-size:20px}。 */
#v-session .phead .kpis{margin-left:auto;display:flex;flex:none}
#v-session .phead .kpis .k{padding:0 17px;border-left:1px solid var(--r3-line);text-align:right}
#v-session .phead .kpis .k:first-child{border-left:none}
#v-session .phead .kpis .v{font-family:var(--r3-mono);font-size:25px;font-weight:700;
  letter-spacing:-.02em;line-height:1.1;color:var(--r3-ink)}
#v-session .phead .kpis .v.amber{color:var(--r3-amber)}
#v-session .phead .kpis .v.coral{color:var(--r3-coral)}
#v-session .phead .kpis .v.ok{color:var(--r3-ok)}
#v-session .phead .kpis .l{font-size:10.5px;color:var(--r3-sub);margin-top:2px}

/* ── 原型 .cols：minmax(0,1fr) 300px ── */
#v-session .cols{display:grid;grid-template-columns:minmax(0,1fr) 300px;gap:16px;
  align-items:flex-start}
#v-session .cols > .sp-main{min-width:0}
/* ── 库页（2026-10-08 第二版，严格照proto/库.html 的视觉）───────────────
   ⚠️ 这块曾经被我误删：换采集页 CSS 时，锚点找的是「库页」块首行，
      替换区间一直吃到「技能页」块 —— 中间的库页整块被覆盖掉了。
      症状：v-clean 的 section 在 DOM 里，但 .cols / .card 全无样式
      （审计量到 tpl=none、boxR=0px）。
      教训：替换 CSS 块时，**先打印区间首尾两行确认边界**，别只信 find()。
   ⚠️ 数值全部从原型实测抄来：card 白底/12px/1px #e4e0d8 · chead 13px 15px 11px
     .no 19px 方块 · kv3 .cell 底 #f3f1ec/9px/padding 11px 13px · .v 15px
     .pathline .pd 底 #f3f1ec/padding 7px 10px/11px
     .dangerbar 底 #fdf2f4 + 下边 #f5d6dc
     thead/sfr grid 16px 1fr 78px 74px */
#v-clean{padding-bottom:176px}
#v-clean .phead{display:flex;align-items:flex-end;gap:20px;padding:14px 4px 20px}
#v-clean .ttl h2{font-size:22px;font-weight:680;letter-spacing:-.015em;line-height:1.2;color:var(--r3-ink)}
#v-clean .psub{font-size:11.5px;color:var(--r3-sub);margin-top:6px}
#v-clean .kpis{margin-left:auto;display:flex;flex:none}
#v-clean .kpis .k{padding:0 17px;border-left:1px solid var(--r3-line);text-align:right}
#v-clean .kpis .k:first-child{border-left:none}
#v-clean .kpis .v{font-family:var(--lz-mono);font-size:25px;font-weight:700;letter-spacing:-.02em;line-height:1.1;color:var(--r3-ink)}
#v-clean .kpis .v.ok{color:var(--lz-ok)}
#v-clean .kpis .v.amber{color:var(--lz-amber)}
#v-clean .kpis .v.coral{color:var(--lz-coral)}
#v-clean .kpis .l{font-size:10.5px;color:var(--r3-sub);margin-top:2px}

#v-clean .cols{display:grid;grid-template-columns:minmax(0,1fr) 300px;gap:16px;align-items:flex-start}
#v-clean .sp-main{display:flex;flex-direction:column;min-width:0}

#v-clean .card{background:var(--r3-surface);border:1px solid var(--r3-line);border-radius:12px;
  box-shadow:var(--lz-sh);overflow:hidden;margin-bottom:14px}
#v-clean .card:last-child{margin-bottom:0}

#v-clean .chead{display:flex;align-items:center;gap:10px;padding:13px 15px 11px}
#v-clean .chead .t{font-size:13.5px;font-weight:650;color:var(--r3-ink);display:flex;align-items:center;gap:8px}
#v-clean .chead .no{width:19px;height:19px;border-radius:5px;flex:none;display:flex;align-items:center;
  justify-content:center;font-family:var(--lz-mono);font-size:10.5px;font-weight:700}
#v-clean .chead .no.safe{background:#ecfdf5;color:var(--lz-ok)}
#v-clean .chead .no.warn{background:#fdf2f4;color:var(--lz-coral)}
#v-clean .chead .no.export{background:#eef0fd;color:var(--lz-indigo);font-size:12px}
#v-clean .chead .c{margin-left:auto;font-family:var(--lz-mono);font-size:10.5px;color:var(--r3-sub)}
#v-clean .chead.sub{padding-top:0}
#v-clean .divider{height:1px;background:var(--r3-line);margin:4px 0 13px}
#v-clean .cbody{padding:0 15px 15px}

#v-clean .kv3{display:grid;grid-template-columns:1fr 1fr 1fr;gap:12px;padding:0 0 14px}
#v-clean .kv3 .cell{padding:11px 13px;background:var(--r3-surface2);border-radius:9px}
#v-clean .kv3 .cell .k{font-size:10.5px;color:var(--r3-sub);margin-bottom:4px}
#v-clean .kv3 .cell .v{font-family:var(--lz-mono);font-size:15px;font-weight:700;
  color:var(--r3-ink);letter-spacing:-.01em}
#v-clean .kv3 .cell .v.good{color:var(--lz-ok)}
#v-clean .kv3 .cell .v.warn{color:var(--lz-amber)}
#v-clean .kv3 .cell .v.bad{color:var(--lz-coral)}

#v-clean .pathline{display:flex;align-items:center;gap:9px;padding:9px 0}
#v-clean .pathline .pd{flex:1;min-width:0;font-family:var(--lz-mono);font-size:11px;
  color:var(--r3-ink2);background:var(--r3-surface2);padding:7px 10px;border-radius:7px;
  white-space:nowrap;overflow:hidden;text-overflow:ellipsis;direction:rtl;text-align:left}
#v-clean .pathline .st{font-size:11.5px;color:var(--lz-ok);flex:none}
#v-clean .pathline .btn.push{margin-left:auto}

#v-clean .btnrow{display:flex;gap:8px;margin-top:11px;flex-wrap:wrap}
#v-clean .btnrow .btn{font-size:11.5px;padding:6px 12px;border-radius:8px;border:1px solid var(--r3-line);
  background:var(--r3-surface);color:var(--r3-ink2);cursor:pointer;white-space:nowrap}
#v-clean .btnrow .btn.pri{background:var(--lz-indigo);color:#fff;border-color:var(--lz-indigo);font-weight:600}
#v-clean .btnrow .btn:disabled{opacity:.5;cursor:not-allowed}

#v-clean .dangerbar{display:flex;align-items:center;gap:10px;padding:11px 15px;
  background:#fdf2f4;border-bottom:1px solid #f5d6dc}
#v-clean .dangerbar .d{display:flex;align-items:center;gap:7px;font-size:12px;
  color:var(--lz-coral);font-weight:600}
#v-clean .dangerbar .s{font-size:11px;color:#a05563}
#v-clean .dangerbar .btn{margin-left:14px;padding:6px 12px;font-size:11.5px;
  border-radius:8px;border:1px solid var(--r3-line);background:var(--r3-surface);
  color:var(--r3-ink2);cursor:pointer;white-space:nowrap}
#v-clean .dangerbar .btn.push{margin-left:auto}
#v-clean .dangerbar .btn.btd{background:var(--lz-coral);color:#fff;border-color:var(--lz-coral);font-weight:600}
#v-clean .dangerbar .btn.btd:disabled{background:#f0b8c4;border-color:#f0b8c4}

#v-clean .thead.sf-thead{display:grid;grid-template-columns:16px 1fr 78px 74px;align-items:center;
  gap:12px;padding:9px 15px;background:var(--r3-surface);
  border-bottom:1px solid var(--r3-line);font-size:10.5px;color:var(--r3-sub);letter-spacing:.03em}
#v-clean .sfr{display:grid;grid-template-columns:16px 1fr 78px 74px;align-items:center;
  gap:12px;padding:10px 15px;cursor:pointer}
#v-clean .sfr:hover{background:var(--r3-surface2)}
#v-clean .sfr.on{background:#fdf2f4}
/* 表头「一键全选」三态 —— 与采集页同一套（空 / ✓ / −） */
#v-clean .thead .cbx{border-color:var(--r3-sub);cursor:pointer}
#v-clean .thead .cbx:hover{border-color:var(--lz-indigo)}
#v-clean .thead .cbx.on{background:var(--lz-indigo);border-color:var(--lz-indigo)}
#v-clean .thead .cbx.on::after{content:"";width:7px;height:4px;border-left:1.6px solid #fff;
  border-bottom:1.6px solid #fff;transform:rotate(-45deg) translate(1px,-1px)}
#v-clean .thead .cbx.part{background:var(--lz-indigo);border-color:var(--lz-indigo)}
#v-clean .thead .cbx.part::after{content:"";width:7px;height:1.6px;background:#fff;border-radius:1px}
#v-clean .cbx{width:15px;height:15px;border-radius:4px;border:1px solid var(--r3-line);
  background:var(--r3-surface);display:inline-flex;align-items:center;justify-content:center;flex:none}
#v-clean .sfr.on .cbx{background:var(--lz-coral);border-color:var(--lz-coral)}
#v-clean .sfr.on .cbx::after{content:"";width:8px;height:4.5px;border-left:1.8px solid #fff;
  border-bottom:1.8px solid #fff;transform:rotate(-45deg) translate(1px,-1px)}
/* 源文件名 —— **只有一行**（2026-10-08 用户：小字是 AI 通病，删掉第二行） */
#v-clean .sfr .fn{min-width:0;font-size:12px;font-weight:600;color:var(--r3-ink2);
  font-family:var(--lz-mono);white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
#v-clean .sfr .num{font-family:var(--lz-mono);font-size:11px;color:var(--r3-ink2);text-align:right}
#v-clean .sfr .num.zero{color:var(--r3-faint)}

#v-clean .filterrow{display:flex;gap:7px;align-items:center;flex-wrap:wrap;padding:10px 15px 12px}
#v-clean .filterrow .proj-sel,#v-clean .filterrow .textin{font-size:11px;padding:5px 9px;border-radius:7px;
  border:1px solid var(--r3-line);background:var(--r3-surface);color:var(--r3-ink2)}
#v-clean .filterrow .ckwrap{font-size:11px;color:var(--r3-ink2);display:flex;align-items:center;gap:4px}

#v-clean .sp-side{display:flex;flex-direction:column;gap:14px;width:100%}
#v-clean .side-box{background:var(--r3-surface);border:1px solid var(--r3-line);border-radius:12px;
  box-shadow:var(--lz-sh);overflow:hidden}
#v-clean .side-box .bh{display:flex;align-items:baseline;gap:7px;padding:11px 15px 9px}
#v-clean .side-box .bh .t{font-size:12.5px;font-weight:650;color:var(--r3-ink)}
#v-clean .side-box .bh .n{font-family:var(--lz-mono);font-size:10.5px;color:var(--r3-sub);margin-left:auto}
#v-clean .side-box .bd{padding:0 15px 14px}
#v-clean .rt{display:flex;align-items:center;padding:7px 0}
#v-clean .rt .sw{width:8px;height:8px;border-radius:2px;flex:none;margin-right:8px}
#v-clean .rt .nm{font-size:11.5px;color:var(--r3-ink2);min-width:0;overflow:hidden;
  text-overflow:ellipsis;white-space:nowrap}
#v-clean .rt .vv{margin-left:auto;font-family:var(--lz-mono);font-size:11.5px;color:var(--r3-ink);
  padding-left:8px;flex:none}
#v-clean .rt .vv.warn{color:var(--lz-coral);font-weight:700}
#v-clean .rt .vv.zero{color:var(--r3-faint)}
#v-clean .hintline{font-size:11px;color:var(--r3-sub);line-height:1.6}
#v-clean .empty{padding:22px 15px;text-align:center;color:var(--r3-faint);font-size:12px}
/* 「导出与恢复」的步骤行 —— 照proto/库.html 的 .step / .sn2 */
#v-clean .step{display:flex;align-items:flex-start;gap:8px;padding:7px 0;font-size:11.5px;
  color:var(--r3-ink2);line-height:1.5}
#v-clean .step .sn2{width:16px;height:16px;border-radius:5px;flex:none;display:flex;
  align-items:center;justify-content:center;font-family:var(--lz-mono);font-size:10px;
  font-weight:700;background:var(--r3-surface2);color:var(--r3-ink2);margin-top:1px}

/* ── 采集页（2026-10-08 照 proto/采集.html）───────────────────────────
   ⚠️ 所有数值都是**先量原型再抄**（库页返工的教训：类名对上≠ 视觉对上）。
     量到：tbl 12px 白底 · thead/cr grid 16px 748px 96px 74px 70px
     thead padding 9px 15px / 10.5px · .cr padding 11px 15px
     .cbx 15px / 4px / 1px · pickbar 11px 15px
     chip 11px / 4.5px 10px / 6px · chip.on 底 #1c1b19 字白 */
#v-collect{padding-bottom:176px}
#v-collect .phead{display:flex;align-items:flex-end;gap:20px;padding:14px 4px 20px}
#v-collect .ttl h2{font-size:22px;font-weight:680;letter-spacing:-.015em;line-height:1.2;color:var(--r3-ink)}
#v-collect .psub{font-size:11.5px;color:var(--r3-sub);margin-top:6px}
#v-collect .kpis{margin-left:auto;display:flex;flex:none}
#v-collect .kpis .k{padding:0 17px;border-left:1px solid var(--r3-line);text-align:right}
#v-collect .kpis .k:first-child{border-left:none}
#v-collect .kpis .v{font-family:var(--lz-mono);font-size:25px;font-weight:700;letter-spacing:-.02em;line-height:1.1;color:var(--r3-ink)}
#v-collect .kpis .v.ok{color:var(--lz-ok)}
#v-collect .kpis .v.amber{color:var(--lz-amber)}
#v-collect .kpis .v.coral{color:var(--lz-coral)}
#v-collect .kpis .l{font-size:10.5px;color:var(--r3-sub);margin-top:2px}

#v-collect .cols{display:grid;grid-template-columns:minmax(0,1fr) 300px;gap:16px;align-items:flex-start}
#v-collect .tbl{background:var(--r3-surface);border:1px solid var(--r3-line);border-radius:12px;
  box-shadow:var(--lz-sh);overflow:hidden}
#v-collect .tbar{display:flex;align-items:center;gap:10px;padding:13px 15px 11px}
#v-collect .tbar .t{font-size:13px;font-weight:650;color:var(--r3-ink)}
#v-collect .tbar .c{font-family:var(--lz-mono);font-size:10.5px;color:var(--r3-sub);
  background:var(--r3-surface2);padding:1.5px 6px;border-radius:4px;margin-left:2px}
#v-collect .chips{margin-left:auto;display:flex;gap:5px}
#v-collect .chip{font-size:11px;padding:4.5px 10px;border-radius:6px;border:1px solid var(--r3-line);
  background:var(--r3-surface);color:var(--r3-ink2);cursor:pointer}
#v-collect .chip.on{background:var(--r3-ink);color:#fff;border-color:var(--r3-ink);font-weight:600}

/* pickbar —— 原型 padding 11px 15px */
#v-collect .pickbar{display:flex;align-items:center;gap:12px;padding:11px 15px;
  background:var(--r3-surface2);border-bottom:1px solid var(--r3-line);flex-wrap:wrap}
#v-collect .pickbar .n{font-size:11.5px;font-weight:600;color:var(--r3-ink)}
#v-collect .pickbar .s{font-size:11px;color:var(--r3-sub)}
#v-collect .pickbar .grow{flex:1}
#v-collect .pickbar .btn{font-size:11.5px;padding:6px 12px;border-radius:8px;border:1px solid var(--r3-line);
  background:var(--r3-surface);color:var(--r3-ink2);cursor:pointer;white-space:nowrap}
#v-collect .pickbar .btn.pri{background:var(--lz-indigo);color:#fff;border-color:var(--lz-indigo);font-weight:600}

/* 表头 + 数据行：5 列（复选框 / 条目 / 类型 / 来源 / 操作） */
#v-collect .thead.ck-thead{display:grid;grid-template-columns:16px 1fr 96px 74px 70px;align-items:center;
  gap:12px;padding:9px 15px;background:var(--r3-surface);
  border-bottom:1px solid var(--r3-line);font-size:10.5px;color:var(--r3-sub);letter-spacing:.03em}
#v-collect .cr{display:grid;grid-template-columns:16px 1fr 96px 74px 70px;align-items:center;
  gap:12px;padding:11px 15px;cursor:pointer}
#v-collect .cr:hover{background:var(--r3-surface2)}
#v-collect .cr.on{background:var(--lz-indigo-bg)}
/* 原型 .cbx 是 15px / 4px 圆角 / 1px 边（不是 16px/5px） */
#v-collect .cbx{width:15px;height:15px;border-radius:4px;border:1px solid var(--r3-line);
  background:var(--r3-surface);display:inline-flex;align-items:center;justify-content:center;flex:none}
#v-collect .cr.on .cbx{background:var(--lz-indigo);border-color:var(--lz-indigo)}
#v-collect .cr.on .cbx::after{content:"";width:7px;height:4px;border-left:1.6px solid #fff;
  border-bottom:1.6px solid #fff;transform:rotate(-45deg) translate(1px,-1px)}
/* 表头那个「一键全选」框（2026-10-08）。
   尺寸跟行内一样 15px，只是描边深一档、可点；三态：
     空   = 没选
     ✓    = 全选
     −    = 半选（选了一部分）—— 这是复选框天然能表达的，纯按钮做不到 */
#v-collect .thead .cbx{border-color:var(--r3-sub);cursor:pointer}
#v-collect .thead .cbx:hover{border-color:var(--lz-indigo)}
#v-collect .thead .cbx.on{background:var(--lz-indigo);border-color:var(--lz-indigo)}
#v-collect .thead .cbx.on::after{content:"";width:7px;height:4px;border-left:1.6px solid #fff;
  border-bottom:1.6px solid #fff;transform:rotate(-45deg) translate(1px,-1px)}
#v-collect .thead .cbx.part{background:var(--lz-indigo);border-color:var(--lz-indigo)}
#v-collect .thead .cbx.part::after{content:"";width:7px;height:1.6px;background:#fff;border-radius:1px}
/* 没有可选行时置灰 */
#v-collect .thead .cbx.dis{border-color:var(--r3-line);cursor:default;opacity:.45}
/* 已入库的行：整个勾选格灰掉 + 标题降一档 —— 让「哪些能选」一眼可见。
   ⚠️ 2026-10-08：不加这条时，点「全选」若顶部恰好都是已入库行，
      屏幕上一点变化都没有，像按钮坏了。 */
#v-collect .cr.lock{cursor:default}
#v-collect .cr.lock .cbx{border-color:var(--r3-line);background:var(--r3-surface2);opacity:.5}
#v-collect .cr.lock .cn{color:var(--r3-sub);font-weight:500}

/* 标题列 —— **只有一行**。
   ⚠️ 2026-10-08 用户明确：小字是 AI 通病，要改掉。
      原来这里是 `.cn b`（标题）+ `.cn i`（一行灰色小字解释），
      现在把标题直接放进 .cn，不设第二行。 */
#v-collect .cn{min-width:0;font-size:13px;font-weight:600;color:var(--r3-ink);
  white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
/* 来源 Agent 标签 —— 跟会话列表 .src 同一套配色，两页观感一致 */
#v-collect .src{font-size:11px;padding:3px 9px;border-radius:5px;font-weight:600;
  white-space:nowrap;display:inline-block}
#v-collect .src.wb{background:#eef0fd;color:#4338ca}
#v-collect .src.zc{background:#ecfdf5;color:#047857}
#v-collect .src.qd{background:#fef3e2;color:#b45309}
#v-collect .src.tr{background:#f3eefd;color:#7c3aed}
#v-collect .src.ot{background:var(--r3-surface2);color:var(--r3-ink2)}
/* 新增记忆 / 时间两列 */
#v-collect .num{font-family:var(--lz-mono);font-size:11.5px;color:var(--r3-ink);text-align:right;
  white-space:nowrap}
#v-collect .num.zero{color:var(--r3-faint)}
/* ⚠️ nowrap 是必须的：70px 列放「2026-08-21」正好，不加会被拆成两行（用户截图那个 bug） */
#v-collect .tm{font-family:var(--lz-mono);font-size:11px;color:var(--r3-sub);text-align:right;
  white-space:nowrap}

/* 右栏 300px */
#v-collect .sp-side{display:flex;flex-direction:column;gap:14px;width:100%}
#v-collect .side-box{background:var(--r3-surface);border:1px solid var(--r3-line);border-radius:12px;
  box-shadow:var(--lz-sh);overflow:hidden}
#v-collect .side-box .bh{display:flex;align-items:baseline;gap:7px;padding:11px 15px 9px}
#v-collect .side-box .bh .t{font-size:12.5px;font-weight:650;color:var(--r3-ink)}
#v-collect .side-box .bh .n{font-family:var(--lz-mono);font-size:10.5px;color:var(--r3-sub);margin-left:auto}
#v-collect .side-box .bd{padding:0 15px 14px;max-height:360px;overflow:auto}
#v-collect .rt{display:flex;align-items:center;padding:7px 0}
#v-collect .rt .sw{width:8px;height:8px;border-radius:2px;flex:none;margin-right:8px}
#v-collect .rt .nm{font-size:11.5px;color:var(--r3-ink2);min-width:0;overflow:hidden;
  text-overflow:ellipsis;white-space:nowrap}
#v-collect .rt .vv{margin-left:auto;font-family:var(--lz-mono);font-size:11.5px;color:var(--r3-ink);
  padding-left:8px;flex:none}
#v-collect .rt .vv.warn{color:var(--lz-coral);font-weight:700}
#v-collect .rt .vv.zero{color:var(--r3-faint)}
#v-collect .hintline{font-size:11px;color:var(--r3-sub);line-height:1.6;padding:2px 0 6px}
#v-collect .empty{padding:22px 15px;text-align:center;color:var(--r3-faint);font-size:12px}

/* ── 技能 / MCP 页（2026-10-08 照 proto/技能MCP.html）───────────────────
   ⚠️ 同 Agent / 交接卡页：会话页那套带 #v-session 前缀，这里重新声明。 */
#v-skill{padding-bottom:176px}
#v-skill .phead{display:flex;align-items:flex-end;gap:20px;padding:14px 4px 20px}
#v-skill .ttl h2{font-size:22px;font-weight:680;letter-spacing:-.015em;line-height:1.2;color:var(--r3-ink)}
#v-skill .psub{font-size:11.5px;color:var(--r3-sub);margin-top:6px}
#v-skill .kpis{margin-left:auto;display:flex;flex:none}
#v-skill .kpis .k{padding:0 17px;border-left:1px solid var(--r3-line);text-align:right}
#v-skill .kpis .k:first-child{border-left:none}
#v-skill .kpis .v{font-family:var(--lz-mono);font-size:25px;font-weight:700;letter-spacing:-.02em;line-height:1.1;color:var(--r3-ink)}
#v-skill .kpis .v.ok{color:var(--lz-ok)}
#v-skill .kpis .v.amber{color:var(--lz-amber)}
#v-skill .kpis .v.coral{color:var(--lz-coral)}
#v-skill .kpis .l{font-size:10.5px;color:var(--r3-sub);margin-top:2px}

#v-skill .cols{display:grid;grid-template-columns:minmax(0,1fr) 300px;gap:16px;align-items:flex-start}
#v-skill .tbl{background:var(--r3-surface);border:1px solid var(--r3-line);border-radius:12px;
  box-shadow:var(--lz-sh);overflow:hidden}
#v-skill .tbar{display:flex;align-items:center;gap:10px;padding:13px 15px 11px}
#v-skill .tbar .t{font-size:13px;font-weight:650;color:var(--r3-ink)}
#v-skill .tbar .c{font-family:var(--lz-mono);font-size:10.5px;color:var(--r3-sub);
  background:var(--r3-surface2);padding:1.5px 6px;border-radius:4px;margin-left:2px}
#v-skill .chips{margin-left:auto;display:flex;gap:5px}
#v-skill .chip{font-size:11px;padding:4.5px 10px;border-radius:6px;border:1px solid var(--r3-line);
  background:var(--r3-surface);color:var(--r3-ink2);cursor:pointer}
#v-skill .chip.on{background:var(--r3-ink);color:#fff;border-color:var(--r3-ink);font-weight:600}

#v-skill .pickbar{display:flex;align-items:center;gap:12px;padding:11px 15px;
  background:var(--r3-surface2);border-bottom:1px solid var(--r3-line);flex-wrap:wrap}
#v-skill .pickbar .n{font-size:11.5px;font-weight:600;color:var(--r3-ink)}
#v-skill .pickbar .s{font-size:11px;color:var(--r3-sub)}
#v-skill .pickbar .push{margin-left:auto;padding:5px 12px;font-size:11.5px;border-radius:8px;white-space:nowrap}

/* 表头+ 数据行：3 列（复选框 / 技能 / 来源） */
/* 4 列（复选框 / 技能 / 来源 / 留空）—— 照 proto/技能MCP.html 的
   `grid-template-columns:16px 1fr 108px 62px`。第 4 列原型是空的占位。 */
#v-skill .thead.sk-thead{display:grid;grid-template-columns:16px 1fr 108px 62px;align-items:center;
  gap:12px;padding:9px 15px;background:var(--r3-surface);border-bottom:1px solid var(--r3-line);
  font-size:10.5px;color:var(--r3-sub);letter-spacing:.03em}
#v-skill .sr{display:grid;grid-template-columns:16px 1fr 108px 62px;align-items:center;
  gap:12px;padding:10px 15px;cursor:pointer}
#v-skill .sr:hover{background:var(--r3-surface2)}
#v-skill .sr.on{background:var(--lz-indigo-bg)}

#v-skill .cbx{width:15px;height:15px;border-radius:4px;border:1px solid var(--r3-line);
  background:var(--r3-surface);display:inline-flex;align-items:center;justify-content:center;flex:none}
#v-skill .sr.on .cbx{background:var(--lz-indigo);border-color:var(--lz-indigo)}
#v-skill .sr.on .cbx::after{content:"";width:8px;height:4.5px;border-left:1.8px solid #fff;
  border-bottom:1.8px solid #fff;transform:rotate(-45deg) translate(1px,-1px)}

/* 技能名 —— **只有一行**（2026-10-08 用户：小字是 AI 通病，删掉描述那行小字）。
   描述文字仍在 DOM 外的接口里，鼠标悬停看 title 也能看到全部内容。 */
#v-skill .sn{min-width:0;font-size:13.5px;font-weight:600;color:var(--r3-ink);
  font-family:var(--lz-mono);white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
#v-skill .srcTag{font-size:11px;color:var(--r3-ink2);white-space:nowrap;
  overflow:hidden;text-overflow:ellipsis}

/* 详情区（选中技能后展开） */
#v-skill .sk-detail{padding:0}
#v-skill .sk-detail:empty{display:none}
#v-skill .sk-detail .dbody{padding:14px 15px;border-top:1px solid var(--r3-line);
  background:var(--r3-surface);font-size:12px;color:var(--r3-ink2);line-height:1.75}
#v-skill .sk-detail .dpath{font-family:var(--lz-mono);font-size:10.5px;color:var(--r3-sub);
  margin-top:8px;word-break:break-all}

/* 右栏 300px */
#v-skill .sp-side{display:flex;flex-direction:column;gap:14px;width:100%}
#v-skill .side-box{background:var(--r3-surface);border:1px solid var(--r3-line);border-radius:12px;
  box-shadow:var(--lz-sh);overflow:hidden}
#v-skill .side-box .bh{display:flex;align-items:baseline;gap:7px;padding:11px 15px 9px}
#v-skill .side-box .bh .t{font-size:12.5px;font-weight:650;color:var(--r3-ink)}
#v-skill .side-box .bh .n{font-family:var(--lz-mono);font-size:10.5px;color:var(--r3-sub);margin-left:auto}
#v-skill .side-box .bd{padding:0 15px 14px;max-height:340px;overflow:auto}
/* 原型 .pathline：一条路径 + 状态 */
#v-skill .pathline{display:flex;align-items:center;gap:8px;padding:6px 0;font-size:11.5px}
#v-skill .pathline .pl{flex:1;min-width:0;font-family:var(--lz-mono);font-size:10.5px;color:var(--r3-sub);
  white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
#v-skill .pathline .vv{font-family:var(--lz-mono);font-size:11px;color:var(--r3-ink);flex:none}
#v-skill .pathline .vv.ok{color:var(--lz-ok)}
#v-skill .pathline .vv.no{color:var(--r3-faint)}
/* 右栏的分布行 —— ⚠️ 2026-10-08 补：这条**之前漏了**，
   导致「技能分布」那两行没有样式（色点不显示、挤成一行）。
   其他页（Agent / 库 / 采集）都有这条，只有技能页漏 —— 又是「类名要各页重新声明」那个坑。 */
#v-skill .rt{display:flex;align-items:center;padding:7px 0}
#v-skill .rt .sw{width:8px;height:8px;border-radius:2px;flex:none;margin-right:8px}
#v-skill .rt .nm{font-size:11.5px;color:var(--r3-ink2);min-width:0;overflow:hidden;
  text-overflow:ellipsis;white-space:nowrap}
#v-skill .rt .vv{margin-left:auto;font-family:var(--lz-mono);font-size:11.5px;color:var(--r3-ink);
  padding-left:8px;flex:none}
#v-skill .rt .vv.zero{color:var(--r3-faint)}
#v-skill .hintline{font-size:11px;color:var(--r3-sub);line-height:1.6;padding:2px 0 6px}
#v-skill .empty{padding:24px 15px;text-align:center;color:var(--r3-faint);font-size:12px}
#v-skill .num{font-family:var(--lz-mono);font-size:11.5px;color:var(--r3-ink);text-align:right}
/* 表头「一键全选」三态 —— 与采集页同一套（空 / ✓ / −） */
#v-skill .thead .cbx{border-color:var(--r3-sub);cursor:pointer}
#v-skill .thead .cbx:hover{border-color:var(--lz-indigo)}
#v-skill .thead .cbx.on{background:var(--lz-indigo);border-color:var(--lz-indigo)}
#v-skill .thead .cbx.on::after{content:"";width:7px;height:4px;border-left:1.6px solid #fff;
  border-bottom:1.6px solid #fff;transform:rotate(-45deg) translate(1px,-1px)}
#v-skill .thead .cbx.part{background:var(--lz-indigo);border-color:var(--lz-indigo)}
#v-skill .thead .cbx.part::after{content:"";width:7px;height:1.6px;background:#fff;border-radius:1px}
/* 底部 MCP 卡片 —— 照proto/技能MCP.html 的 .card/.chead/.flag。
   数值同库页卡片（白底/12px/1px 描边），flag 是浅红标签。 */
#v-skill .card{background:var(--r3-surface);border:1px solid var(--r3-line);border-radius:12px;
  box-shadow:var(--lz-sh);overflow:hidden;margin-top:14px}
#v-skill .chead{display:flex;align-items:center;gap:10px;padding:13px 15px 11px}
#v-skill .chead .t{font-size:13.5px;font-weight:650;color:var(--r3-ink)}
#v-skill .chead .flag{font-size:10.5px;padding:2px 7px;border-radius:5px;font-weight:600}
#v-skill .chead .flag.warn{background:#fdf2f4;color:var(--lz-coral)}
#v-skill .chead .c{margin-left:auto;font-family:var(--lz-mono);font-size:10.5px;color:var(--r3-sub)}
#v-skill .cbody{padding:0 15px 15px}
#v-skill .btnrow{display:flex;gap:8px;margin-top:11px;flex-wrap:wrap}
#v-skill .btnrow .btn{font-size:11.5px;padding:6px 12px;border-radius:8px;border:1px solid var(--r3-line);
  background:var(--r3-surface);color:var(--r3-ink2);cursor:pointer;white-space:nowrap}


/* ── 交接卡页（2026-10-08 照 proto/交接卡.html）─────────────────────────
   ⚠️ 同 Agent 页：会话页那套 .cols/.tbl 带 #v-session 前缀，这里重新声明。 */
#v-handoff{padding-bottom:176px}
#v-handoff .phead{display:flex;align-items:flex-end;gap:20px;padding:14px 4px 20px}
#v-handoff .ttl h2{font-size:22px;font-weight:680;letter-spacing:-.015em;line-height:1.2;color:var(--r3-ink)}
#v-handoff .psub{font-size:11.5px;color:var(--r3-sub);margin-top:6px}
#v-handoff .kpis{margin-left:auto;display:flex;flex:none}
#v-handoff .kpis .k{padding:0 17px;border-left:1px solid var(--r3-line);text-align:right}
#v-handoff .kpis .k:first-child{border-left:none}
#v-handoff .kpis .v{font-family:var(--lz-mono);font-size:25px;font-weight:700;letter-spacing:-.02em;line-height:1.1;color:var(--r3-ink)}
#v-handoff .kpis .v.ok{color:var(--lz-ok)}
#v-handoff .kpis .v.amber{color:var(--lz-amber)}
#v-handoff .kpis .l{font-size:10.5px;color:var(--r3-sub);margin-top:2px}

#v-handoff .cols{display:grid;grid-template-columns:minmax(0,1fr) 300px;gap:16px;align-items:flex-start}
#v-handoff .tbl{background:var(--r3-surface);border:1px solid var(--r3-line);border-radius:12px;
  box-shadow:var(--lz-sh);overflow:hidden}
#v-handoff .tbar{display:flex;align-items:center;gap:10px;padding:13px 15px 11px}
#v-handoff .tbar .t{font-size:13px;font-weight:650;color:var(--r3-ink)}
#v-handoff .tbar .c{font-family:var(--lz-mono);font-size:10.5px;color:var(--r3-sub);
  background:var(--r3-surface2);padding:1.5px 6px;border-radius:4px;margin-left:2px}
#v-handoff .chips{margin-left:auto;display:flex;gap:5px}
#v-handoff .chip{font-size:11px;padding:4.5px 10px;border-radius:6px;border:1px solid var(--r3-line);
  background:var(--r3-surface);color:var(--r3-ink2);cursor:pointer}
#v-handoff .chip.on{background:var(--r3-ink);color:#fff;border-color:var(--r3-ink);font-weight:600}

/* 原型 .pickbar：选中态汇总条 */
#v-handoff .pickbar{display:flex;align-items:center;gap:12px;padding:11px 15px;
  background:var(--r3-surface2);border-bottom:1px solid var(--r3-line);flex-wrap:wrap}
#v-handoff .pickbar .n{font-size:11.5px;font-weight:600;color:var(--r3-ink)}
#v-handoff .pickbar .s{font-size:11px;color:var(--r3-sub)}
#v-handoff .pickbar .fmt{margin-left:auto;display:flex;align-items:center;gap:7px}
#v-handoff .pickbar .lb{font-size:10.5px;color:var(--r3-sub)}
#v-handoff .fseg{display:inline-flex;border:1px solid var(--r3-line);border-radius:7px;overflow:hidden}
#v-handoff .fbtn{font-size:11px;padding:4px 10px;color:var(--r3-ink2);cursor:pointer;
  background:var(--r3-surface);border-right:1px solid var(--r3-line)}
#v-handoff .fbtn:last-child{border-right:none}
#v-handoff .fbtn.on{background:var(--r3-ink);color:#fff;font-weight:600}
#v-handoff .pickbar .push{padding:5px 12px;font-size:11.5px;border-radius:8px;white-space:nowrap}

/* 表头 + 数据行：4 列（复选框 / 项目 / 记忆 / 会话） */
#v-handoff .thead.hf-thead{display:grid;grid-template-columns:16px 1fr 74px 74px;align-items:center;
  gap:12px;padding:9px 15px;background:var(--r3-surface);border-bottom:1px solid var(--r3-line);
  font-size:10.5px;color:var(--r3-sub);letter-spacing:.03em}
#v-handoff .hr{display:grid;grid-template-columns:16px 1fr 74px 74px;align-items:center;
  gap:12px;padding:11px 15px;cursor:pointer}
#v-handoff .hr:hover{background:var(--r3-surface2)}
#v-handoff .hr.on{background:var(--lz-indigo-bg)}

/* 复选框：原型是方块 .cbx，不是原生 input */
/* 表头「一键全选」三态 —— 与采集页同一套（空 / ✓ / −） */
#v-handoff .thead .cbx{border-color:var(--r3-sub);cursor:pointer}
#v-handoff .thead .cbx:hover{border-color:var(--lz-indigo)}
#v-handoff .thead .cbx.on{background:var(--lz-indigo);border-color:var(--lz-indigo)}
#v-handoff .thead .cbx.on::after{content:"";width:7px;height:4px;border-left:1.6px solid #fff;
  border-bottom:1.6px solid #fff;transform:rotate(-45deg) translate(1px,-1px)}
#v-handoff .thead .cbx.part{background:var(--lz-indigo);border-color:var(--lz-indigo)}
#v-handoff .thead .cbx.part::after{content:"";width:7px;height:1.6px;background:#fff;border-radius:1px}
#v-handoff .cbx{width:15px;height:15px;border-radius:4px;border:1px solid var(--r3-line);
  background:var(--r3-surface);display:inline-flex;align-items:center;justify-content:center;flex:none}
#v-handoff .hr.on .cbx{background:var(--lz-indigo);border-color:var(--lz-indigo)}
#v-handoff .hr.on .cbx::after{content:"";width:8px;height:4.5px;border-left:1.8px solid #fff;
  border-bottom:1.8px solid #fff;transform:rotate(-45deg) translate(1px,-1px)}

#v-handoff .hn{min-width:0}
#v-handoff .hn b{display:block;font-size:13.5px;font-weight:600;color:var(--r3-ink);
  white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
#v-handoff .hn i{display:block;font-size:11px;color:var(--r3-sub);font-style:normal;
  white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
#v-handoff .hn i.warn{color:var(--lz-amber);font-weight:600}
#v-handoff .num{font-family:var(--lz-mono);font-size:11.5px;color:var(--r3-ink);text-align:right}
#v-handoff .num.zero{color:var(--r3-faint)}

/* 右栏 300px */
#v-handoff .sp-side{display:flex;flex-direction:column;gap:14px;width:100%}
#v-handoff .side-box{background:var(--r3-surface);border:1px solid var(--r3-line);border-radius:12px;
  box-shadow:var(--lz-sh);overflow:hidden}
#v-handoff .side-box .bh{display:flex;align-items:baseline;gap:7px;padding:11px 15px 9px}
#v-handoff .side-box .bh .t{font-size:12.5px;font-weight:650;color:var(--r3-ink)}
#v-handoff .side-box .bh .n{font-family:var(--lz-mono);font-size:10.5px;color:var(--r3-sub);margin-left:auto}
#v-handoff .side-box .bd{padding:0 15px 14px}
#v-handoff .rt{display:flex;align-items:center;padding:7px 0}
#v-handoff .rt .sw{width:8px;height:8px;border-radius:2px;flex:none;margin-right:8px}
#v-handoff .rt .nm{font-size:11.5px;color:var(--r3-ink2);min-width:0;overflow:hidden;
  text-overflow:ellipsis;white-space:nowrap}
#v-handoff .rt .vv{margin-left:auto;font-family:var(--lz-mono);font-size:11.5px;color:var(--r3-ink);
  padding-left:8px;flex:none}
#v-handoff .rt .vv.warn{color:var(--lz-amber);font-weight:700}
#v-handoff .rt .vv.zero{color:var(--r3-faint)}
#v-handoff .hintline{font-size:11px;color:var(--r3-sub);line-height:1.6;padding:2px 0 4px}
#v-handoff .empty{padding:24px 15px;text-align:center;color:var(--r3-faint);font-size:12px}

/* ── Agent 页（2026-10-08 照 proto/Agent.html）─────────────────────────
   ⚠️ 会话页那套 .cols/.tbl/.sp-side 全部带 #v-session 前缀，
      这里必须**重新声明一遍**—— 否则 Agent 页会完全没有列布局
      （上次实测 Agent 的 cols/tbl/右栏全是 0，就是这个原因）。
   数值全部照抄原型 + 会话页实测值，不自造。 */
#v-agents{padding-bottom:176px}
#v-agents .phead{display:flex;align-items:flex-end;gap:20px;padding:14px 4px 20px}
#v-agents .ttl h2{font-size:22px;font-weight:680;letter-spacing:-.015em;line-height:1.2;color:var(--r3-ink)}
#v-agents .psub{font-size:11.5px;color:var(--r3-sub);margin-top:6px}
#v-agents .kpis{margin-left:auto;display:flex;flex:none}
#v-agents .kpis .k{padding:0 17px;border-left:1px solid var(--r3-line);text-align:right}
#v-agents .kpis .k:first-child{border-left:none}
#v-agents .kpis .v{font-family:var(--lz-mono);font-size:25px;font-weight:700;letter-spacing:-.02em;line-height:1.1;color:var(--r3-ink)}
#v-agents .kpis .v.ok{color:var(--lz-ok)}
#v-agents .kpis .v.amber{color:var(--lz-amber)}
#v-agents .kpis .v.coral{color:var(--lz-coral)}
#v-agents .kpis .l{font-size:10.5px;color:var(--r3-sub);margin-top:2px}

#v-agents .cols{display:grid;grid-template-columns:minmax(0,1fr) 300px;gap:16px;align-items:flex-start}
#v-agents .tbl{background:var(--r3-surface);border:1px solid var(--r3-line);border-radius:12px;
  box-shadow:var(--lz-sh);overflow:hidden}
#v-agents .tbar{display:flex;align-items:center;gap:10px;padding:13px 15px 11px}
#v-agents .tbar .t{font-size:13px;font-weight:650;color:var(--r3-ink)}
#v-agents .tbar .c{font-family:var(--lz-mono);font-size:10.5px;color:var(--r3-sub);
  background:var(--r3-surface2);padding:1.5px 6px;border-radius:4px;margin-left:2px}
#v-agents .chips{margin-left:auto;display:flex;gap:5px}
#v-agents .chip{font-size:11px;padding:4.5px 10px;border-radius:6px;border:1px solid var(--r3-line);
  background:var(--r3-surface);color:var(--r3-ink2);cursor:pointer}
#v-agents .chip.on{background:var(--r3-ink);color:#fff;border-color:var(--r3-ink);font-weight:600}

/* 表头 4 列 —— 照原型 grid-template-columns:1fr 118px 92px 106px */
#v-agents .thead.ag-thead{display:grid;grid-template-columns:1fr 118px 92px 106px;align-items:center;
  gap:12px;padding:9px 15px;background:var(--r3-surface);
  border-bottom:1px solid var(--r3-line);
  font-size:10.5px;color:var(--r3-sub);letter-spacing:.03em}
#v-agents .arow{display:grid;grid-template-columns:1fr 118px 92px 106px;align-items:center;
  gap:12px;padding:11px 15px}
#v-agents .arow:hover{background:var(--r3-surface2)}

/* Agent 名+ 图标 */
#v-agents .an{display:flex;align-items:center;gap:9px;min-width:0}
#v-agents .an .ic{width:24px;height:24px;border-radius:6px;flex:none;display:flex;align-items:center;
  justify-content:center;font-size:11px;font-weight:700;font-family:var(--lz-mono);color:#fff}
#v-agents .an .nm{min-width:0}
#v-agents .an .nm b{display:block;font-size:13.5px;font-weight:600;color:var(--r3-ink);
  white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
/* .nm i 已删 —— 不再挂配置路径小字 */

/* 状态点：原型的 .dot.yes / .dot.no */
#v-agents .dot{display:inline-flex;align-items:center;gap:6px;font-size:11.5px;
  color:var(--r3-ink2);white-space:nowrap}
/* ⚠️ white-space:nowrap 是必须的（2026-10-08 实测踩到）：
   .dot 是 inline-flex，放在 grid 单元里会被压到 min-content 宽度，
   「未安装」三个字被逐字竖排成一条细线，页面看着像坏了。
   原型 .dot 在普通流里，grid 里必须显式禁止换行 —— 这不是加装饰，是修挤压。 */
#v-agents .dot::before{content:"";width:7px;height:7px;border-radius:50%;flex:none}
#v-agents .dot.yes::before{background:var(--lz-ok)}
#v-agents .dot.no::before{background:var(--r3-line)}
#v-agents .dot.no{color:var(--r3-faint)}

#v-agents .cnt{font-family:var(--lz-mono);font-size:11.5px;color:var(--r3-ink);text-align:right}
#v-agents .cnt.zero{color:var(--r3-faint)}
#v-agents .act{display:flex;gap:6px;justify-content:flex-end}
#v-agents .act .btn{font-size:11.5px;padding:5px 10px;border-radius:8px;border:1px solid var(--r3-line);
  background:var(--r3-surface);color:var(--r3-ink2);cursor:pointer;white-space:nowrap}
#v-agents .act .btn:hover{border-color:var(--lz-indigo);color:var(--lz-indigo)}

/* 右栏 300px */
#v-agents .sp-side{display:flex;flex-direction:column;gap:14px;width:100%}
#v-agents .side-box{background:var(--r3-surface);border:1px solid var(--r3-line);border-radius:12px;
  box-shadow:var(--lz-sh);overflow:hidden}
#v-agents .side-box .bh{display:flex;align-items:baseline;gap:7px;padding:11px 15px 9px}
#v-agents .side-box .bh .t{font-size:12.5px;font-weight:650;color:var(--r3-ink)}
#v-agents .side-box .bh .n{font-family:var(--lz-mono);font-size:10.5px;color:var(--r3-sub);margin-left:auto}
#v-agents .side-box .bd{padding:0 15px 14px}
/* 右栏行：原型 .rt（色点 + 名称 + 右侧数值） */
#v-agents .rt{display:flex;align-items:center;padding:7px 0}
#v-agents .rt .sw{width:8px;height:8px;border-radius:2px;flex:none;margin-right:8px}
#v-agents .rt .nm{font-size:11.5px;color:var(--r3-ink2)}
#v-agents .rt .vv{margin-left:auto;font-family:var(--lz-mono);font-size:11.5px;color:var(--r3-ink)}
#v-agents .rt .vv.zero{color:var(--r3-faint)}
#v-agents .hintline{font-size:11px;color:var(--r3-sub);line-height:1.5;padding-top:9px}
#v-agents .empty{padding:24px 15px;text-align:center;color:var(--r3-faint);font-size:12px}

#v-session .sp-side{display:flex;flex-direction:column;gap:14px}

/* ── 原型 .tbl ── */
#v-session .tbl{background:var(--r3-surface);border:1px solid var(--r3-line);
  border-radius:12px;box-shadow:var(--r3-sh);overflow:hidden}
/* ── 原型 .tbar ── */
#v-session .tbar{display:flex;align-items:center;gap:10px;padding:10px 15px;
  border-bottom:1px solid var(--r3-line)}
#v-session .tbar .t{font-size:13px;font-weight:650;color:var(--r3-ink)}
#v-session .tbar .c{font-family:var(--r3-mono);font-size:10.5px;color:var(--r3-sub);
  background:var(--r3-surface2);padding:1.5px 6px;border-radius:4px;margin-left:2px}
#v-session .chips{margin-left:auto;display:flex;gap:5px}
/* ── 原型 .chip ── */
#v-session .chip{font-size:11px;padding:4.5px 10px;border-radius:6px;border:1px solid var(--r3-line);
  background:var(--r3-surface);color:var(--r3-ink2);cursor:pointer;user-select:none}
#v-session .chip.on{background:var(--r3-ink);color:#fff;border-color:var(--r3-ink);font-weight:600}

/* ── 原型 .thead / .tr：同一条 grid，列天然对齐 ── */
#v-session .thead,#v-session .tr{display:grid;grid-template-columns:1fr 96px 66px 62px;
  align-items:center;gap:12px}
#v-session .thead{padding:8px 15px;background:var(--r3-surface);
  border-bottom:1px solid var(--r3-line);font-size:10.5px;color:var(--r3-sub);letter-spacing:.03em}
#v-session .tr{padding:10px 15px;border-bottom:1px solid var(--r3-line);cursor:pointer}
#v-session .tr:last-child{border-bottom:none}
#v-session .tr:hover{background:var(--r3-surface2)}
#v-session .tr.on{background:var(--r3-indigo-bg)}
#v-session .tr.on .idx{color:var(--r3-indigo)}
#v-session .tr .ti{min-width:0}
#v-session .tr .tt{font-size:13.5px;font-weight:600;white-space:nowrap;overflow:hidden;
  text-overflow:ellipsis;color:var(--r3-ink)}
#v-session .tr.on .tt{color:var(--r3-indigo)}
#v-session .tr .mt{font-size:11px;color:var(--r3-sub);margin-top:1px;white-space:nowrap;
  overflow:hidden;text-overflow:ellipsis}
/* ── 原型 .src（来源标签）── */
#v-session .src{font-size:10px;padding:2.5px 7px;border-radius:5px;flex:none;font-weight:600}
#v-session .src.wb{background:#eef0fd;color:#4338ca}
#v-session .src.zc{background:#f0fdf4;color:#0f766e}
#v-session .src.qd{background:#fef3c7;color:#b45309}
#v-session .src.ot{background:var(--r3-surface2);color:var(--r3-ink2)}
/* ── 原型 .out（输出强度条）── */
#v-session .out{display:flex;gap:2px;flex:none;justify-content:flex-end}
#v-session .out i{width:8px;height:8px;border-radius:2px;background:var(--r3-line);display:block}
#v-session .out i.f{background:var(--r3-indigo);opacity:.8}
#v-session .out.empty i{background:var(--r3-line);opacity:.55}
/* ── 原型 .lk（已加密）：灰字灰边，不是珊瑚红 ── */
#v-session .lk{display:inline-block;font-size:9.5px;font-weight:500;color:var(--r3-sub);
  border:1px solid var(--r3-line);border-radius:3px;padding:0 4px;margin-right:7px;
  vertical-align:1.5px;letter-spacing:.02em}
/* ── 原型 .tm ── */
#v-session .tm{font-family:var(--r3-mono);font-size:10.5px;color:var(--r3-sub);flex:none}

/* ── 原型 .box（右栏两个框）── */
#v-session .side-box{background:var(--r3-surface);border:1px solid var(--r3-line);
  border-radius:12px;box-shadow:var(--r3-sh);overflow:hidden}
#v-session .side-box .bh{display:flex;align-items:baseline;gap:7px;padding:11px 15px 9px}
#v-session .side-box .bh .t{font-size:12.5px;font-weight:650;color:var(--r3-ink)}
#v-session .side-box .bh .n{font-family:var(--r3-mono);font-size:10.5px;color:var(--r3-sub);
  margin-left:auto}
#v-session .side-box .bd{padding:0 15px 14px}
/* ── 原型 .stack + .lgs ── */
#v-session .stack{display:flex;height:9px;border-radius:3px;overflow:hidden;margin-bottom:10px;
  background:var(--r3-surface2)}
#v-session .stack i{display:block;height:100%}
#v-session .lgs{display:flex;flex-direction:column;gap:5px}
#v-session .lg{display:flex;align-items:center;gap:7px;font-size:11.5px}
#v-session .lg .sw{width:8px;height:8px;border-radius:2px;flex:none}
#v-session .lg .nm{color:var(--r3-ink2)}
#v-session .lg .v{margin-left:auto;font-family:var(--r3-mono);font-size:11px;color:var(--r3-ink2)}
#v-session .lg .p{font-family:var(--r3-mono);font-size:10px;color:var(--r3-faint);width:30px;
  text-align:right}
/* ── 原型 .td（等你处理）── */
#v-session .td{display:flex;align-items:flex-start;gap:9px;padding:9px 0;cursor:pointer}
#v-session .td .tt{flex:1;font-size:11.5px;color:var(--r3-ink2);line-height:1.4;min-width:0}
#v-session .td .tt b{color:var(--r3-ink);font-weight:600}
#v-session .td .ct{font-family:var(--r3-mono);font-size:11px;font-weight:700;flex:none}
#v-session .td .ct.c1{color:var(--r3-coral)}
#v-session .td .ct.c2{color:var(--r3-amber)}
#v-session .td .ct.c3{color:var(--r3-sub)}
#v-session .empty{padding:28px 0;text-align:center;color:var(--r3-faint);font-size:12px}

/* ── 通用确认弹窗（2026-10-09 新增组件）──────────────────────────────
   为什么加它：面板原来 14 处全用原生 confirm()，只有「确定/取消」两键。
   但「删除前要不要备份」是**三选一**（取消 / 不备份直接删 / 备份并删除），
   原生弹窗塞不下第三个键 —— 结果只能替用户做决定，这是错的。
   用户拍板选了这个方案（甲）。 */
.ask-mask{position:fixed;inset:0;background:rgba(28,27,25,.34);z-index:9000;
  display:flex;align-items:flex-start;justify-content:center;padding-top:15vh}
.ask-dlg{width:452px;max-width:calc(100vw - 40px);background:var(--r3-surface);
  border:1px solid var(--r3-line);border-radius:14px;box-shadow:var(--lz-sh-lg);
  overflow:hidden;animation:ask-in .16s ease-out}
@keyframes ask-in{from{opacity:0;transform:translateY(-6px)}to{opacity:1;transform:none}}
.ask-dlg .dh{padding:20px 22px 0}
.ask-dlg .dt{font-size:15px;font-weight:680;letter-spacing:-.01em;color:var(--r3-ink)}
.ask-dlg .dsub{font-size:12px;color:var(--r3-sub);margin-top:6px;line-height:1.6}
.ask-dlg .warnbox{margin:16px 22px 0;padding:11px 13px;border-radius:9px;
  background:#fdf2f4;font-size:11.5px;color:#a05563;line-height:1.6}
.ask-dlg .q{margin:16px 22px 0;font-size:12.5px;font-weight:600;color:var(--r3-ink)}
.ask-dlg .df{display:flex;gap:8px;padding:18px 22px 20px;align-items:center}
.ask-dlg .df .grow{flex:1}
.ask-dlg .df .btn{padding:8px 14px;font-size:12px;border-radius:8px;
  border:1px solid var(--r3-line);background:var(--r3-surface);color:var(--r3-ink2);cursor:pointer}
.ask-dlg .df .btn.danger{background:var(--lz-coral);color:#fff;
  border-color:var(--lz-coral);font-weight:600}
.ask-dlg .df .btn.safe{background:var(--lz-indigo);color:#fff;
  border-color:var(--lz-indigo);font-weight:600}
</style>

</head>
<body>
<div id="topbar"></div>
<div id="toast"></div>
<div class="app">
  <aside class="side">
    <!-- 2026-10-05 阶段二：9 项 → 6 项。记忆 / 记忆包 / 质检已砍
         （记忆是给 Agent 读 MCP 的燃料，人在工作台看没有意义；
          质检功能并进清理页，不占独立入口）。
         ⚠️ show() 读 data-v 切换 #v-xxx 的 hidden，删这三项不会影响其他页。 -->
    <nav>
        <!-- 选中指示器：absolute 脱离文档流，外层只做 transform 位移，
             内层 ::before 过渡 left/top/width/height 做尺寸。
             ⚠️ 这个 span 必须留在 <nav> 的**第一个子位** —— navMotion.place() 会
                getElementById("navInd") 拿它写 CSS 变量，缺了会抛
                "Cannot read properties of null (reading 'style')"（2026-10-07 实测）。
             ⚠️ 排序（2026-10-07 按第二版原型）：会话排第 1。原型 7 份 proto/*.html
                全部是这个顺序（会话 · 采集 · 交接卡 · 库 · Agent · 技能 / MCP）。
                上一版写的是「会话放正中间当锚点」（2026-10-06 的决定），
                用户 2026-10-07 明确说那个方案不合适，原型已按「会话排第 1」重做。
             ⚠️ 改顺序只动 <a> 的先后，data-v 不变，show() 不依赖 DOM 位置。 -->
        <span class="nav-ind" id="navInd" aria-hidden="true"></span>
        <a class="nav on" data-v="session" title="会话"><svg class="ico" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round"><path d="M20 12a8 8 0 1 1-3.2-6.4"/><path d="M4 20l1.6-4.2"/><circle cx="9" cy="12" r="1"/><circle cx="13" cy="12" r="1"/><circle cx="17" cy="12" r="1"/></svg><span>会话</span></a>
        <a class="nav" data-v="collect" title="采集"><svg class="ico" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="8"/><path d="M12 4v8l5.5 3.5"/></svg><span>采集</span></a>
        <a class="nav" data-v="handoff" title="交接卡"><svg class="ico" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round"><rect x="3.5" y="6" width="17" height="12" rx="3"/><path d="M8 11h8M8 14h5"/></svg><span>交接卡</span></a>
        <a class="nav" data-v="clean" title="库"><svg class="ico" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round"><path d="M4 7h16"/><path d="M9 7V5.5A1.5 1.5 0 0 1 10.5 4h3A1.5 1.5 0 0 1 15 5.5V7"/><path d="M6 7l1 12a2 2 0 0 0 2 2h6a2 2 0 0 0 2-2l1-12"/><path d="M10 11v6M14 11v6"/></svg><span>库</span></a>
        <a class="nav" data-v="agents" title="Agent"><svg class="ico" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round"><rect x="4" y="7" width="16" height="12" rx="3"/><path d="M12 3v4"/><circle cx="9" cy="13" r="1.2"/><circle cx="15" cy="13" r="1.2"/></svg><span>Agent</span></a>
        <a class="nav" data-v="skill" title="技能"><svg class="ico" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round"><path d="M12 3l2.4 5.3 5.6.7-4.2 3.9 1.1 5.6L12 15.8 7.1 18.5l1.1-5.6L4 9l5.6-.7z"/></svg><span>技能</span></a>
</nav>
  </aside>

  <main class="main">
    <div class="toolbar">
      <!-- 2026-10-06 用户指出的问题：「你在会话页上明显把最开始另一个对话的给搬了进来」
         原来这一栏是：品牌 + 全局统计 + 全局搜索框 + 主题 + 退出。
         **全局搜索框搜的是「记忆」**（placeholder: 搜索记忆，支持中文），
         放在会话页页顶是错的 —— 会话页有自己的筛选（chip）和检索。
         现在工具栏只留三样：品牌 / 主题 / 退出。
         会话页的搜索框按定稿原型 p5-final/list.html 放在它自己的顶栏里
         （截图里那行：搜索… ⌘K · 重新扫描 · ⏻）。 -->
      <div class="sbrand">
        <div class="sbtext">
          <h1>LOCI</h1>
          <p>忆宫</p>
        </div>
      </div>
      <div class="tb-spacer"></div>
      <!-- ⚠️ #stats 的元素已随全局统计条一起移除（2026-10-06）。
           但 loadStats() 还在 getElementById("stats").innerHTML=...，
           元素不在会抛错打断整个初始化。留一个 display:none 的空壳，
           别删函数 —— 内存页那几个数字还要用。 -->
      <div class="stats" id="stats" style="display:none"></div>
      <!-- 搜索 + 重新扫描（2026-10-06）。
           ⚠️ 用户指正过：原型图里顶栏是**一行** —— 品牌在左，
              「搜索… ⌘K · 重新扫描 · ⏻」在右同一行。
              我之前拆成两行（工具栏一行 + 会话页 .sp-top 一行），
              跟原型不符，已合并回一行。
           ⚠️ 搜索框 id 是 s-q（会话页自己的），**不是** #q ——
              #q 是旧工具栏里搜"记忆"的那个全局框，已经删了。
           ⚠️ 2026-10-07：这两项在**会话详情页不显示**。
              原型 proto/会话详情.html 的 .topact 只有
              复制原文 / ⏻ / 返回会话列表 —— 搜索是列表页的筛选手段，
              「重新扫描」是入库动作，在"正在读一段原文"的时候都不该出现。
              由 show() 切页时切 hidden，见下面 #sd-bar 那段。 -->
      <div class="sp-search" id="sp-search">
        <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="#aaa49a"
          stroke-linecap="round"><circle cx="11" cy="11" r="7"/><path d="M20 20l-3.5-3.5"/></svg>
        <input id="s-q" placeholder="搜索…" onkeydown="if(event.key==='Enter')sessionSearch()"
               oninput="if(!this.value.trim())loadSessions()">
        <kbd>⌘K</kbd>
      </div>
      <button class="sp-btn" id="sp-scanbtn" onclick="runAutoScan()" title="扫描本机各 Agent 的会话文件">重新扫描</button>
      <!-- ⚠️ 2026-10-07 用户拍板：主题切换按钮**删掉**，完全照原型。
           原型 .topact 只有三个元素：搜索框 / 重新扫描 / ⏻。
           applyTheme() 里两处都是 if(元素) 保护，删掉按钮不会抛错；
           明暗切换仍可通过 ?theme=dark / ?theme=light 或 localStorage 走。 -->
      <button class="btn quitbtn" onclick="shutdownPanel()" title="关闭面板服务（记忆数据不受影响）" aria-label="关闭面板服务">
        <svg viewBox="0 0 24 24" width="16" height="16" fill="none" stroke="currentColor"
             stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
          <path d="M12 4v9"/><path d="M6.6 6.6a8 8 0 1 0 10.8 0"/>
        </svg>
      </button>
      <!-- 详情页专属动作条（2026-10-07）。
           ⚠️ 原来这些按钮在 <section id="v-sdetail"> 自己的 .sd-top 里，
              而全局 .toolbar 本来就在所有 section 外面 —— 屏幕上两行 LOCI 忆宫叠着。
              原型是一页一个顶栏，所以搬上来，用 hidden 控制显隐。
           ⚠️ 面包屑是用户 2026-10-07 拍板**留着**的（原型里没有），
              规格照原型 .backbtn：白底 + 1px 描边 + 8px 圆角 + var(--sh)，12px 字。 -->
      <div id="sd-bar" hidden>
        <!-- 面包屑：点它 = history.back()。对应详情页的 pushState 逻辑，
             后退键能真的退回列表 —— 这是"独立页面"跟"同页展开"最实际的差别。
             ⚠️ a11y：<nav> 语义 + title + tabindex，Enter/Space 等同点击。 -->
<!-- ⚠️ 2026-10-07 按第二版原型删掉面包屑「会话 › 当前篇」。原型 .topact 只有
              复制原文 / ⏻ / 返回会话列表，没有面包屑。交接包铁律三记着：上一轮 AI 加了它
              还写注释称「用户已定稿」，那是编的。返回能力不丢：返回按钮 + 浏览器后退键。 -->
        <!-- 原型 .copybtn：11px 小号描边按钮 -->
        <button class="copybtn" id="sd-copy" onclick="copySessionRaw()">复制原文</button>
        <!-- 原型 .backbtn：带左箭头的描边按钮，12px 字 -->
        <button class="backbtn" onclick="closeSessionDetail()" title="返回会话列表"
          aria-label="返回会话列表">
          <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor"
            stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round"><path d="M15 19l-7-7 7-7"/></svg>
          返回会话列表
        </button>
      </div>
    </div>

    <div class="content">
      <!-- 记忆 -->
      <section id="v-mem" hidden>
        <!-- 页面标题区：Trae 稿在数值条下方写「记忆」，右侧放动作按钮 -->
        <div class="pagehead">
          <div>
            <h2 class="ptitle" id="page-title">记忆</h2>
            <!-- 数据行按原型 C 放在页头内（常驻可见）—— 与会话页同一处理 -->
            <p class="psub" id="mem-psub">正在统计…</p>
          </div>
          <div class="pacts">
            <!-- 筛选（左）与动作（右）分开：原先项目下拉和几个动作按钮挤成一排，
                 看不出「全部项目」是筛选器、后面那些是动作。 -->
            <div class="pgrp">
              <select id="proj-filter" class="proj-sel" onchange="loadList()">
                <option value="">全部项目</option>
              </select>
              <button class="btn" onclick="backToList()">全部</button>
            </div>
            <span class="psep" aria-hidden="true"></span>
            <div class="pgrp">
              <button class="btn" onclick="showContext()">常驻上下文</button>
              <button class="btn" onclick="toggleCleanup()">批量清理</button>
              <button class="btn pri" onclick="goAdd()">＋ 记一条</button>
            </div>
          </div>
        </div>
        <p class="lead">这里是<strong>给模型检索用的记忆碎片</strong> —— 短、独立、能命中关键词，
        不是给人读的叙事。想看「当时到底聊了什么」，去
        <span class="leadlink" onclick="show('session')">会话</span>页看原文时间线；
        每条记忆的「出处」都能跳回产出它的那一轮。</p>
        <!-- 数据行（会被 pmAll() 收进页头折叠区，默认收起）+ 4 张 KPI 卡 + 类型构成条。
             数字全部由 loadStats() 从 /api/stats 现取，**一个都不写死**。 -->
        <div class="kgrid" id="mem-kpi">
          <!-- 已按原型补上 .kbar（此前我以量纲不同为由去掉，是我不对）：本机内容页那 4 张卡是"同一种东西的不同类别"
               （技能/MCP/插件/配置），同尺度比条宽才有意义；而记忆页这 4 项是
               「条数 / 会话数 / 轮次 / 项目数」四种不同量纲，共用一条刻度只会造出
               一根 100% 和三根 0.9% 的装饰条。现按原型折衷：条宽各自对**自己的参考值**（记忆/200 · 会话/50 · 轮次/5000 · 项目/30），不互相比。 -->
          <div class="kpi hero">
            <div class="ktop"><span class="kt">活跃记忆</span>
              <span class="kic" style="background:var(--t-fact-bg);color:var(--data-fact)"><svg viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round"><ellipse cx="8" cy="4" rx="5.2" ry="2.2"/><path d="M2.8 4v8c0 1.2 2.3 2.2 5.2 2.2s5.2-1 5.2-2.2V4"/><path d="M2.8 8c0 1.2 2.3 2.2 5.2 2.2S13.2 9.2 13.2 8"/></svg></span>
            </div>
            <div class="kv"><span id="mk-total">—</span><span class="u">条</span></div>
            <div class="kbar"><i id="mk-total-b" style="background:var(--data-fact)"></i></div><!-- 记忆 / 200 条为满格 -->
            <div class="kfoot"><span>检索时会被读到</span><span id="mk-total-f">统计中…</span></div>
          </div>
          <div class="kpi">
            <div class="ktop"><span class="kt">归档会话</span>
              <span class="kic" style="background:var(--t-skill-bg);color:var(--data-skill)"><svg viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round"><path d="M2 3.5h12v7H7l-3 2.5v-2.5H2z"/></svg></span>
            </div>
            <div class="kv"><span id="mk-sess">—</span><span class="u">个</span></div>
            <div class="kbar"><i id="mk-sess-b" style="background:var(--data-skill)"></i></div><!-- 会话 / 50 个为满格（列表一次取 50） -->
            <div class="kfoot"><span>写进库的历史对话</span><span id="mk-sess-f">统计中…</span></div>
          </div>
          <div class="kpi">
            <div class="ktop"><span class="kt">对话轮次</span>
              <span class="kic" style="background:var(--t-preference-bg);color:var(--data-preference)"><svg viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round"><path d="M3 4h10M3 8h10M3 12h6"/></svg></span>
            </div>
            <div class="kv"><span id="mk-turn">—</span><span class="u">轮</span></div>
            <div class="kbar"><i id="mk-turn-b" style="background:var(--data-preference)"></i></div><!-- 轮次 / 5000 轮为满格 -->
            <div class="kfoot"><span>上面那些记忆的原料</span><span id="mk-turn-f">统计中…</span></div>
          </div>
          <div class="kpi">
            <div class="ktop"><span class="kt">覆盖项目</span>
              <span class="kic" style="background:var(--t-decision-bg);color:var(--data-decision)"><svg viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round"><path d="M1.8 4.4c0-.7.6-1.2 1.2-1.2h3l1.3 1.6h5.7c.7 0 1.2.5 1.2 1.2v6c0 .7-.5 1.2-1.2 1.2H3c-.6 0-1.2-.5-1.2-1.2z"/></svg></span>
            </div>
            <div class="kv"><span id="mk-proj">—</span><span class="u">个</span></div>
            <div class="kbar"><i id="mk-proj-b" style="background:var(--data-decision)"></i></div><!-- 项目 / 30 个为满格 -->
            <div class="kfoot"><span>有记忆归属的项目</span><span id="mk-proj-f">统计中…</span></div>
          </div>
        </div>

        <!-- 类型构成条：条宽 = 该类真实条数（flex-grow），图例给名称 + 条数。
             ⚠️ 它与上面 4 张卡的分工：卡片给"总量"，这条给"构成" —— 两者都取自
             /api/stats 的同一次返回，不会出现"卡里 76、条上加起来 74"这种自相矛盾。 -->
        <div class="distcard" id="mem-dist">
          <div class="dh"><b>记忆构成</b><span id="mem-dist-h">按类型分布</span></div>
          <div class="bar" id="mem-dist-bar"></div>
          <div class="legend" id="mem-dist-legend"></div>
        </div>

        <div class="panel formcard" id="form">
          <textarea id="f-content" placeholder="要记什么？（决策 / 坑 / 事实 / 经验……）"></textarea>
          <div class="formrow">
            <select id="f-type">
              <option value="fact">事实</option>
              <option value="decision">决策</option>
              <option value="preference">偏好</option>
              <option value="skill">经验</option>
              <option value="error">踩坑</option>
              <option value="context">背景</option>
              <option value="summary">摘要</option>
            </select>
            <input type="text" id="f-proj" placeholder="项目名（可选）">
            <input type="text" id="f-tags" placeholder="标签，逗号分隔（可选）">
            <span class="imp" id="f-imp" data-v="2" title="重要度 1-4，点击切换"><b>★★</b>★★</span>
            <button class="btn pri" onclick="doSave()">保存</button>
            <button class="btn" onclick="cancelSave()" title="放弃这次录入，清空已填内容">取消</button>
          </div>
        </div>
        <div class="panel" id="cleanup-panel" style="display:none">
          <div class="row">
            <select id="cl-project" class="proj-sel" style="max-width:200px"><option value="">全部项目</option></select>
            <select id="cl-agent" class="proj-sel" data-kind="agent" style="max-width:170px"><option value="">全部来源</option></select>
            <input type="date" id="cl-before" class="textin" style="max-width:170px">
            <label class="ckwrap"><input type="checkbox" id="cl-sup"> 只清空已作废</label>
          </div>
          <div class="row" style="margin-top:10px">
            <button class="btn" onclick="cleanupPreview()">预览命中</button>
            <button class="btn" onclick="cleanupRun()">执行删除</button>
            <button class="btn" onclick="showOrphans()">检查陈旧记忆</button>
            <button class="btn" onclick="purgeOrphans()">清理陈旧记忆</button>
          </div>
          <p class="hint">删除是<strong>硬删除</strong>（不进回收站）。执行时<strong>由你选</strong>要不要先导出全量备份 JSON —— 备份可随时用「记忆包导入」恢复；不备份则无法找回。时间条件＝删除该日期之前的记忆。</p>
          <div class="msg" id="cleanup-msg" style="display:none"></div>
        </div>
        <div class="split">
          <div class="split-main">
            <div class="shead"><span class="t" id="list-title">记忆列表</span></div>
            <div class="sbody">
              <div class="handoff-out" id="ctx-out"></div>
              <div id="list"></div>
            </div>
          </div>
          <!-- 详情栏常驻：未选中时渲染引导卡（三处骨架都不再塌单列，见 .split CSS 上方注释）。
               选中后 renderDetail() 会整体覆盖这里。 -->
          <aside class="split-side" id="detail">
            <div class="dhead">
              <div class="dhtop"><h3>记忆详情</h3></div>
              <div class="dtop"><span class="dmi">点左侧任意一条记忆，这里会显示完整内容、标签与操作按钮</span></div>
            </div>
            <div class="dmain">
              <div class="guide">
                <div class="gic"><svg viewBox="0 0 24 24"><path d="M8 6h13M8 12h13M8 18h9M3.5 6h.01M3.5 12h.01M3.5 18h.01"/></svg></div>
                <h4>从左边选一条记忆</h4>
                <p>这里会显示它的完整内容、标签、归属信息与操作按钮。</p>
              </div>
            </div>
          </aside>
        </div>
      </section>

      <!-- 会话层 -->
      <!-- 会话页（2026-10-06 按定稿原型 p5-final/list.html 重做）
           ⚠️ 旧结构是 .split 三栏（列表 + 右栏原文），已拆。
              新结构照 p5-final/list.html：
                页头（标题 + 副标）→ 5 个大数字 → 左表格 + 右栏两个框
           ⚠️ 尺寸/间距照抄原型，不要凭手感改。用户 2026-10-06 明确要求：
              定稿后的设计不许为了修 bug 自动放大/缩小。
           ⚠️ 「抽记忆」交互原本挂在旧的右栏 #s-view 上，那条路保留在
              gotoSession() 里；这里只管浏览 + 跳转。 -->
      <section id="v-session">
        <!-- 2026-10-07 照 proto/会话列表.html 从零重做。
             类名与原型逐字对应（.phead/.ttl/.psub/.kpis/.cols/.tbl/.tbar/
             .thead/.tr/.sp-side/.side-box/.stack/.lgs/.td），
             只把原型的 .box 改成 .side-box —— 「.box」是通用名，
             旧工作台已占用，不加前缀会串。
             id 全部保留（JS 按 id 取数）：s-psub / skpi-* / s-total /
             s-chips / s-list / sp-src-* / sp-todo* -->
        <div class="phead">
          <div class="ttl">
            <h2>会话</h2>
            <div class="psub" id="s-psub">正在统计…</div>
          </div>
          <div class="kpis" id="s-kpi">
            <div class="k"><div class="v" id="skpi-sess">—</div><div class="l">会话</div></div>
            <div class="k"><div class="v" id="skpi-mem">—</div><div class="l">已抽记忆</div></div>
            <!-- ⚠️ 2026-10-07 按 proto 改后三格标签与配色。
                 原型五格：会话 / 已抽记忆 / 待归档(amber) / 待质检(coral) / 今日已抽(ok)。
                 原来写的是 原文轮次 / 已被取代(amber) / 已置顶(ok)，与原型不符。
                 id 不变（JS 按 id 取数），只是标签和配色跟着换。 -->
            <div class="k"><div class="v amber" id="skpi-turn">—</div><div class="l">待归档</div></div>
            <div class="k"><div class="v coral" id="skpi-sup">—</div><div class="l">待质检</div></div>
            <div class="k"><div class="v ok" id="skpi-pin">—</div><div class="l">今日已抽</div></div>
          </div>
        </div>

        <div class="cols">
          <div class="sp-main">
            <div class="tbl">
              <div class="tbar">
                <span class="t">全部会话</span><span class="c" id="s-total">0</span>
                <div class="chips" id="s-chips">
                  <span class="chip on" data-a="">全部</span>
                  <span class="chip" data-a="workbuddy">WorkBuddy</span>
                  <span class="chip" data-a="zcode">ZCode</span>
                  <span class="chip" data-a="qoder">Qoder</span>
                </div>
              </div>
              <div class="thead">
                <div>标题</div><div>Agent</div><div>输出</div><div style="text-align:right">时间</div>
              </div>
              <div id="s-list"></div>
            </div>
          </div>

          <!-- 右栏：记忆来源（按平台堆叠） + 等你处理 -->
          <aside class="sp-side">
            <div class="side-box">
              <div class="bh"><span class="t">记忆来源</span><span class="n" id="sp-src-n">—</span></div>
              <div class="bd">
                <div class="stack" id="sp-src-stack"></div>
                <div class="lgs" id="sp-src-lgs"></div>
              </div>
            </div>
            <div class="side-box">
              <div class="bh"><span class="t">等你处理</span><span class="n" id="sp-todo-n">—</span></div>
              <div class="bd" id="sp-todo"></div>
            </div>
          </aside>
        </div>
      </section>

      <section id="v-sdetail" hidden>
        <!-- 顶栏：照 proto/会话详情.html 的 .top。
             ⚠️ 2026-10-07 用户拍板三处：
                ① 主题切换按钮删掉（全局 .toolbar 里那一个）；
                ② 顶栏白条 + 底部分隔线删掉，底色改 --r3-bg 暖白；
                ③ 面包屑「会话 › 当前篇」**留着**，但规格改成原型的 .backbtn
                   —— 白底 + 1px 描边 + 8px 圆角 + var(--sh)，12px 字。
                   原来它是裸文字 + 一条 --line 分隔线，两处都不是原型规格。
             品牌标复用 .sbrand（跟全局 .toolbar 里那个同一套，不另造）。 -->
        <div class="sd-chead">
          <h2 id="sd-title">…</h2>
          <div class="sd-meta" id="sd-meta"></div>
        </div>
        <div class="sd-thread" id="sd-thread"></div>
      </section>

      <!-- 质检（2026-10-06 恢复）
           ⚠️ ⚠️ 这里原来有一段 <section id="v-audit">，我在插入 v-sdetail 时
              误删了它。show() 的列表里仍有 "audit"，于是每次切页都会抛
              `Cannot set properties of null (setting 'hidden')`，
              列表因此整页渲染不出来（实测 #s-list 为空）。
              教训：往 section 堆里插新 section 时，**必须先 grep 全部 id 逐个核对**，
              别只确认自己加的那个在。导航已砍到 6 项，但 v-audit 的 section
              仍然保留（质检功能并进清理页的导航入口，页面本身没删）。 -->
      <section id="v-audit" hidden>
        <div class="pagehead">
          <div>
            <h2 class="ptitle">记忆质检</h2>
            <!-- 数据行按原型 C 放在页头内（常驻可见）—— 与前三页同一处理 -->
            <p class="psub" id="au-psub">正在统计…</p>
          </div>
          <div class="pacts">
            <div class="pgrp">
              <select id="au-proj" class="proj-sel" style="max-width:200px"><option value="">全部项目</option></select>
            </div>
            <span class="psep" aria-hidden="true"></span>
            <div class="pgrp">
              <button class="btn" onclick="exportReport()">下载质检报告</button>
              <button class="btn pri" onclick="runAudit()">开始质检</button>
            </div>
          </div>
        </div>
        <p class="lead">七查：重复（合并）· 疑似同义（人工判断）· 可能矛盾（以新代旧）· 长期未更新（续期/作废）· 过短 · 过粗粒度（拆分）· 元数据缺失。作废的记忆保留在库里但不参与检索。</p>
        <div class="health" id="health"></div>
        <div class="msg" id="audit-msg" style="display:none"></div>
        <div id="audit-out"></div>
      </section>

      <!-- 清理 -->
<section id="v-clean" hidden>
        <!-- 2026-10-08 照 proto/库.html 重做（第二版，严格按原型视觉）。
             ⚠️ 第一版只对了结构，视觉偏了 6 处，被用户当场指出：
               ① .kv3 我做成纯文字，原型是**带底色的格子**（surface2 底 + 9px圆角 + padding 11px 13px）
               ② .chead 少了**编号徽章** .no（19px 方块，safe绿 / warn 红）
               ③ .pathline 的路径我用了纯文字，原型是**灰底药丸**（surface2 + padding 7px 10px）
               ④ .dangerbar 底色我算的 color-mix，原型是**固定 #fdf2f4**（浅粉）
               ⑤ 三张卡的内容错了：原型是 **备份(含导出分区) / 恢复 / 销毁**，
                  我做成了记忆包 / 备份 / 记忆的源文件
               ⑥ 卡片间的 divider（1px 分隔线）没做
             本版逐条照抄原型数值，不自造。

             ⚠️ 契约 id 保留：cl-psub / cl-proj / cl-agent / cl-before /
                cl-sup / cl-msg / sf-list / sf-side / backup-list /
                bk-list / arch-* / cl-export / cl-import ——
                引擎层（loadSourceFiles / loadArchive / cleanupRun 等）在写它们。 -->
        <div class="phead">
          <div class="ttl">
            <h2>库</h2>
            <div class="psub" id="cl-psub">正在统计…</div>
          </div>
          <div class="kpis" id="cl-kpi">
            <div class="k"><div class="v" id="clpi-mem">—</div><div class="l">在用记忆</div></div>
            <div class="k"><div class="v" id="clpi-sup">—</div><div class="l">已被取代</div></div>
            <div class="k"><div class="v amber" id="clpi-src">—</div><div class="l">源文件</div></div>
            <div class="k"><div class="v coral" id="clpi-mb">—</div><div class="l">源文件 MB</div></div>
            <div class="k"><div class="v ok" id="clpi-bak">—</div><div class="l">备份份数</div></div>
          </div>
        </div>

        <div class="cols">
          <div class="sp-main">

            <!-- ① 备份（含导出分区） -->
            <div class="card">
              <div class="chead">
                <span class="no safe">1</span>
                <span class="t">备份</span>
                <span class="c" id="bk-c">— 份</span>
              </div>
              <div class="cbody">
                <div class="kv3">
                  <div class="cell"><div class="k">库体积</div><div class="v" id="bk-size">—</div></div>
                  <div class="cell"><div class="k">备份目录</div><div class="v good" id="bk-dir">—</div></div>
                  <div class="cell"><div class="k">自动备份</div><div class="v good" id="bk-auto">每次打开检查</div></div>
                </div>
                <div id="backup-list"></div>
                <!-- ⚠️ 2026-10-08 修：原来 4 个按钮里 `clImport()` / `clClearOld()`
                     这两个函数**根本不存在**（我引用了空气，点了会抛 ReferenceError）。
                     现在只留**真有实现**的：
                       立即备份 → doSnapshot()  打开目录 → pickArchiveDir()
                       更换位置 → saveArchiveDir()
                     「清空旧备份」后端没有对应接口 → **移除**，不放假按钮。 -->
                <div class="btnrow">
                  <button class="btn" onclick="doSnapshot()">立即备份</button>
                  <button class="btn" onclick="pickArchiveDir()">打开备份目录</button>
                  <button class="btn" onclick="saveArchiveDir()">更换存放位置</button>
                </div>
              </div>

              <div class="divider"></div>

              <div class="chead sub">
                <span class="no export">↓</span>
                <span class="t">导出</span>
                <span class="c">给新电脑用</span>
              </div>
              <div class="cbody">
                <div class="pathline">
                  <span class="pd" id="ex-path" style="color:var(--r3-faint)">还没有选导出位置</span>
                  <span class="st" style="color:var(--r3-faint)">待选择位置</span>
                </div>
                <!-- 导出分区的按钮行 —— 照 proto/库.html。
                     结构对账量到 原型 btnrow=3 / 实际=2 —— 缺的就是这一行。 -->
                <div class="btnrow">
                  <button class="btn pri" onclick="doExport()">导出备份文件</button>
                  <button class="btn" onclick="pickArchiveDir()">选位置</button>
                  <button class="btn" onclick="doExport()">导出全部（含源文件）</button>
                </div>
                <div class="hintline" style="padding-top:11px">
                  导出的文件拷到新电脑，装好 MCP 插件后用下面的「恢复」导回去。
                </div>
              </div>
            </div>

            <!-- ② 恢复 -->
            <div class="card">
              <div class="chead">
                <span class="no warn">2</span>
                <span class="t">恢复</span>
                <span class="c">新电脑用</span>
              </div>
              <div class="cbody">
                <div class="pathline">
                  <span class="pd" style="color:var(--r3-faint)">还没有选备份文件</span>
                  <button class="btn sm push" onclick="doImport()">选择文件</button>
                </div>
                <div class="btnrow">
                  <button class="btn" id="bk-restore" onclick="doImport()">恢复</button>
                </div>
                <div class="hintline" style="padding-top:11px">
                  恢复 = 把备份文件里的记忆全部导回当前库，不覆盖你现有记忆。
                </div>
              </div>
            </div>

            <!-- ③ 销毁：源文件 -->
            <div class="card">
              <div class="chead">
                <span class="no warn">3</span>
                <span class="t">销毁</span>
                <span class="c" id="sf-c">— / — 个</span>
              </div>

              <div class="dangerbar">
                <span class="d">
                  <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor"
                    stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
                    <path d="M12 4v9"/><path d="M6.6 6.6a8 8 0 1 0 10.8 0"/></svg>
                  <span id="db-txt">还没选文件</span>
                </span>
                <span class="s">这些对话已在 Agent 里删掉，源文件是残留</span>
                <!-- 全选/全不选（一个按钮来回切）——与采集页同一套。
                     「预览可销毁」保留：它只选"没人用的残留"，跟全选语义不同。 -->
                <button class="btn" id="cl-allbtn" onclick="clToggleAll(event)">全选</button>
                <button class="btn" onclick="clPreviewSelect()">预览可销毁</button>
                <button class="btn btd" id="db-go" disabled onclick="cleanupRun()">确认销毁</button>
              </div>

              <div class="thead sf-thead">
                <div><span class="cbx hd" id="cl-all" onclick="clToggleAll(event)"></span></div>
                <div>源文件</div>
                <div style="text-align:right">大小</div>
                <div style="text-align:right">记忆</div>
              </div>
              <div id="sf-list"></div>

              <!-- 契约元素：清理表单（旧路径仍写它们，收起不显示） -->
              <div class="msg" id="cl-msg" style="display:none"></div>
              <div class="filterrow">
                <select id="cl-proj" class="proj-sel"><option value="">全部项目</option></select>
                <select id="cl-agent" class="proj-sel" data-kind="agent"><option value="">全部来源</option></select>
                <input type="date" id="cl-before" class="textin">
                <label class="ckwrap"><input type="checkbox" id="cl-sup"> 只清空已作废</label>
              </div>
              <div id="cl-export" style="display:none"></div>
              <div id="cl-import" style="display:none"></div>
              <div id="bk-list" style="display:none"></div>
              <div id="arch-dir" style="display:none"></div>
              <div id="arch-hint" style="display:none"></div>
              <div id="arch-last" style="display:none"></div>
              <div id="arch-size" style="display:none"></div>
              <div id="arch-state" style="display:none"></div>
              <div id="sf-side" style="display:none"></div>
            </div>
          </div>

          <!-- 右栏两框 —— 照proto/库.html：
               「源文件分布」（可销毁残留 + 仍在用 + 各 Agent 一行）
               「导出与恢复」（3 步换机流程）
               ⚠️ 我之前做成「可销毁残留 + 来源分布」，主题和第2 框都不对。
                   原型第1 框是把"留存情况"和"按 Agent 分"**合在一框**里的。 -->
          <aside class="sp-side">
            <div class="side-box">
              <div class="bh"><span class="t">源文件分布</span><span class="n" id="orp-n">—</span></div>
              <div class="bd" id="orp-box"></div>
            </div>
            <div class="side-box">
              <div class="bh"><span class="t">导出与恢复</span><span class="n">换机流程</span></div>
              <div class="bd">
                <div class="step"><span class="sn2">1</span><span class="st">在这台电脑点「导出备份文件」</span></div>
                <div class="step"><span class="sn2">2</span><span class="st">把 .db 拷到新电脑（U 盘 / 网盘都行）</span></div>
                <div class="step"><span class="sn2">3</span><span class="st">新电脑装好 MCP，点「恢复」选它</span></div>
                <div class="hintline">整个库一起过去，不用逐条导 md，也不用重新扫对话。</div>
              </div>
            </div>
          </aside>
        </div>
      </section>

      <!-- 采集中心 -->
<section id="v-collect" hidden>
        <!-- 2026-10-08 照 proto/采集.html 重做。**所有数值先量原型再抄**——
             库页返工的教训：类名对上不等于视觉对上。
             量到的：tbl 白底/12px · thead grid 16px 748px 96px 74px 70px
             padding 9px 15px / 10.5px · .cr 同 grid + padding 11px 15px
             .cbx 15px / 4px 圆角 / 1px 边 · .pickbar padding 11px 15px
             .chip 11px / padding 4.5px 10px / 6px 圆角 · on 底 #1c1b19 字白
             .cols = 1084px 300px
             ⚠️ 契约 id 保留：ck-psub / ck-kpi / ck-all / ck-none / ck-go /
                scan-info / scan-list / conv-src / sc-list ——
                doScan() / doCollect() / runAutoScan() 等引擎函数在写它们。 -->
        <div class="phead">
          <div class="ttl">
            <h2>采集</h2>
            <div class="psub" id="ck-psub">正在探测本机…</div>
          </div>
          <div class="kpis" id="ck-kpi">
            <div class="k"><div class="v coral" id="ckpi-new">—</div><div class="l">未入库</div></div>
            <div class="k"><div class="v" id="ckpi-old">—</div><div class="l">已入库</div></div>
            <div class="k"><div class="v" id="ckpi-msg">—</div><div class="l">已读消息</div></div>
            <div class="k"><div class="v ok" id="ckpi-mem">—</div><div class="l">抽出记忆</div></div>
            <div class="k"><div class="v" id="ckpi-src">—</div><div class="l">扫描根</div></div>
          </div>
        </div>

        <div class="cols">
          <div class="sp-main">
            <div class="tbl">
              <div class="tbar">
                <span class="t">扫描结果</span><span class="c" id="ck-total">0</span>
                <div class="chips" id="ck-chips">
                  <span class="chip on" data-a="">全部</span>
                  <span class="chip" data-a="new">未入库</span>
                  <span class="chip" data-a="old">已入库</span>
                </div>
              </div>

              <!-- 原型 .pickbar：选中态汇总 + 入库按钮 -->
              <div class="pickbar" id="ck-pickbar" style="display:none">
                <span class="n" id="ck-sel-n">已选 0 条</span>
                <span class="s" id="ck-sel-s">—</span>
                <span class="grow"></span>
                <!-- 「全选 / 全不选」一个按钮来回切（2026-10-08 用户要求）。
                     选中任意一条后这行才出现；点它：没全选→全选，已全选→全取消，
                     按钮上的字跟着状态变。文字用「全选 / 全不选」，不写「全部选」。 -->
                <button class="btn" id="ck-allbtn" onclick="ckToggleAll(event)">全选</button>
                <button class="btn pri" onclick="doCollect()">入库选中</button>
              </div>

              <!-- 表头照 proto/采集.html：第2 列（标题列）**不写标签**，
                   原型就是空的。列： [cbx] [标题] 来源 新增记忆 时间 -->
              <!-- 表头第一格 = 一键全选（2026-10-08 用户要求）。
                   单击：没全选→全选；已全选→全取消。
                   半选态（−）表示"选了一部分"。只对**可选的**行生效 ——
                   已入库的行本来就不能勾（点它没反应），全选也要跳过它们。 -->
              <div class="thead ck-thead">
                <div><span class="cbx hd" id="ck-all" onclick="ckToggleAll(event)"></span></div>
                <div></div>
                <div>来源</div>
                <div style="text-align:right">新增记忆</div>
                <div style="text-align:right">时间</div>
              </div>
              <div id="scan-list"></div>

              <!-- 契约元素：旧版表单与提示（旧函数仍在写） -->
              <!-- ⚠️ 2026-10-08 修：这个元素**必须隐藏**。旧渲染函数还在写它，
                   但它默认 display:block —— 结果页面上一直挂着一行「还没扫描。」，
                   跟新表格同时出现（用户截图里就是这个）。 -->
              <div class="hint" id="scan-info" style="display:none"></div>
              <div id="conv-src" style="display:none"></div>
              <div id="sc-list" style="display:none"></div>
            </div>
          </div>

          <!-- 右栏：各来源待入库 + 扫描根目录（原型两个框） -->
          <aside class="sp-side">
            <div class="side-box">
              <div class="bh"><span class="t">各来源待入库</span><span class="n" id="ck-src-n">—</span></div>
              <div class="bd" id="ck-src-box"></div>
            </div>
            <div class="side-box">
              <div class="bh"><span class="t">扫描根目录</span><span class="n" id="ck-root-n">—</span></div>
              <div class="bd" id="ck-root-box"></div>
            </div>
          </aside>
        </div>
      </section>

      <!-- Agent 体检 -->
<section id="v-agents" hidden>
        <!--2026-10-08 照 proto/Agent.html 重做。
             原来是旧工作台那套（.pagehead/.kgrid/.kpi/.panel/.agrid），
             与七页定稿原型完全不是一套结构 —— 用户 2026-10-08 指出
             「这还是原来我之前设计的那个面板」。这次按原型逐块重写：
               .phead > .ttl > h2 + .psub   （标题 22px，会话列表同款）
               .kpis > 5 个 .k              （原型五格）
               .cols > .sp-main > .tbl     （.tbar + .thead + .arow 数据行）
               .sp-side > 2 个 .side-box    （右栏 300px）
             id 全部保留：ag-psub / ag-kpi-* / ag-on / ag-side-*
             ⚠️ 原型右栏第二个框是「数据可读性（可读 / 已加密）」——
                /api/agents **没有"是否加密"这个字段**（实测确认），
                硬做就是编数据，所以那个框**不画**，在报告里说明。 -->
        <div class="phead">
          <div class="ttl">
            <h2>Agent</h2>
            <div class="psub" id="ag-psub">正在识别本机 Agent…</div>
          </div>
          <div class="kpis" id="ag-kpi">
            <div class="k"><div class="v ok" id="agpi-on">—</div><div class="l">已装</div></div>
            <div class="k"><div class="v" id="agpi-off">—</div><div class="l">未装</div></div>
            <div class="k"><div class="v" id="agpi-sess">—</div><div class="l">已入库会话</div></div>
            <div class="k"><div class="v amber" id="agpi-reg">—</div><div class="l">已接入 Loci</div></div>
            <div class="k"><div class="v" id="agpi-mem">—</div><div class="l">已抽记忆</div></div>
          </div>
        </div>

        <div class="cols">
          <div class="sp-main">
            <div class="tbl">
              <div class="tbar">
                <span class="t">已知 Agent</span><span class="c" id="ag-total">0</span>
                <div class="chips" id="ag-chips">
                  <span class="chip on" data-a="">全部</span>
                  <span class="chip" data-a="1">已装</span>
                  <span class="chip" data-a="0">未装</span>
                </div>
              </div>
              <div class="thead ag-thead">
                <div>Agent</div><div>状态</div>
                <div style="text-align:right">已入库会话</div>
                <div style="text-align:right">操作</div>
              </div>
              <div id="ag-list"></div>
            </div>
          </div>

          <!-- 右栏两框 —— 照 proto/Agent.html。
               ⚠️ 之前这里我写着「原型只有这一个框」，那是**我写错的**：
                  原型有「已装 Agent 内容分布」+「数据可读性」两个 box。
                  结构对账时量到 sideBoxN 原型=2 / 实际=1，才发现。
               ⚠️ 「数据可读性」的可读/加密数：`/api/agents` **没有加密字段**，
                  但能从会话列表推导 —— `msg_count>0` 就是拿到了原文（可读），
                  `msg_count==0` 就是没原文（加密/读不到）。**真数据，不编。** -->
          <aside class="sp-side">
            <div class="side-box">
              <div class="bh"><span class="t">已装 Agent 内容分布</span><span class="n" id="ag-side-n">—</span></div>
              <div class="bd" id="ag-side"></div>
            </div>
            <div class="side-box">
              <div class="bh"><span class="t">数据可读性</span><span class="n" id="ag-read-n">—</span></div>
              <div class="bd" id="ag-read"></div>
            </div>
          </aside>
        </div>
      </section>

      <!-- 记忆包 -->
      <!-- 本机内容：技能 / 配置文件 / MCP 三类清算（原先只有技能） -->
<section id="v-skill" hidden>
        <!-- 2026-10-08 照 proto/技能MCP.html 重做。原来是旧工作台那套
             （.pagehead/.kgrid/.kpi/.panel/.split），与定稿原型不是一套结构。
             这次按原型逐块重写：.phead/.ttl/.psub + 5 格 .kpis
             + .cols > .sp-main > .tbl（.tbar + .pickbar + .thead + .sr 行）
             + .sp-side > 2 个 .side-box（配置源 / 本机 Loci）。
             ⚠️ 契约 id 全部保留：sk-psub / sk-list / sk-detail / sk-out /
                sk-srcs / sk-mcps / SKILLS / SKTGT —— loadSkills()、
                renderSkillList()、renderSkillDetail() 都写它们，引擎层没动。 -->
        <div class="phead">
          <div class="ttl">
            <h2>技能</h2>
            <div class="psub" id="sk-psub">正在探测本机内容…</div>
          </div>
          <!-- ⚠️ 2026-10-08 用户拍板：MCP 砍掉，不占标题位。
               理由（实测）：技能有 6 个迁移接口（plan/preview/copy/export/detail/skills），
               **MCP 一个写入接口都没有**，只有 content + content/detail 两个只读的。
               跟「能迁移的东西」并排当标题，那一半是纯只读展示，名不副实。
               MCP 的信息不删——降级到右栏「本机 Loci」，那里它才是它该在的位置。
               KPI 腾出来的两格改成**真能算出来**的：
               可传目标 / 用户级技能。原来那两格（全家都有 / 仅在本机）
               后端没有"技能×目标"命中矩阵，一直是 0，等于占位。 -->
          <div class="kpis" id="sk-kpi">
            <div class="k"><div class="v" id="skpi-skill">—</div><div class="l">技能</div></div>
            <div class="k"><div class="v ok" id="skpi-tgt">—</div><div class="l">可传目标</div></div>
            <div class="k"><div class="v" id="skpi-usr">—</div><div class="l">用户级</div></div>
            <div class="k"><div class="v amber" id="skpi-agent">—</div><div class="l">来源 Agent</div></div>
            <div class="k"><div class="v coral" id="skpi-loci">—</div><div class="l">已接 Loci</div></div>
          </div>
        </div>

        <div class="cols">
          <div class="sp-main">
            <div class="tbl">
              <div class="tbar">
                <span class="t">本机技能</span><span class="c" id="sk-total">0</span>
                <div class="chips" id="sk-chips">
                  <span class="chip on" data-a="">全部</span>
                  <span class="chip" data-a="builtin">内置</span>
                  <span class="chip" data-a="user">用户级</span>
                </div>
              </div>

              <!-- 原型 .pickbar：选中态汇总 + 目标 Agent + 传递 -->
              <div class="pickbar" id="sk-pickbar" style="display:none">
                <span class="n" id="sk-sel-n">已选 0 个技能</span>
                <span class="s" id="sk-sel-s">—</span>
                <!-- 同采集页：一个按钮来回切，文字跟着状态变 -->
                <button class="btn" id="sk-allbtn" onclick="skToggleAll(event)">全选</button>
                <button class="btn pri push" onclick="doCopySkills()">复制到目标 Agent</button>
              </div>

              <!-- 表头照 proto/技能MCP.html： [cbx] 技能 已装在 文件(右) -->
              <div class="thead sk-thead">
                <div><span class="cbx hd" id="sk-all" onclick="skToggleAll(event)"></span></div>
                <div>技能</div>
                <div>已装在</div>
                <div style="text-align:right">文件</div>
              </div>
              <div id="sk-list"></div>

              <!-- 契约元素：详情与输出区，引擎层在写，只是位置挪到了下方 -->
              <div class="sk-detail" id="sk-detail"></div>
              <div class="handoff-out" id="sk-out"></div>
              <div id="sk-src" style="display:none"></div>
              <span id="sk-count"></span>
              <div id="sk-srcs" style="display:none"></div>
              <div id="sk-mcps" style="display:none"></div>
              <select id="sk-target" style="display:none"></select>
              <input type="text" id="sk-q" style="display:none">
            </div>

            <!-- 底部 MCP 卡片 —— 照 proto/技能MCP.html。
                 ⚠️ 结构对账量到 原型 cardsN=1 / 实际=0 —— 这张卡我先前整张漏了。
                 内容：MCP 配置文件路径 + 复制按钮。 -->
            <div class="card">
              <div class="chead">
                <span class="t">MCP</span>
                <span class="flag warn">配置含本机路径，换电脑要改</span>
                <span class="c" id="mcp-c">— 个</span>
              </div>
              <div class="cbody" id="mcp-list"></div>
              <div class="cbody" style="padding-top:0">
                <div class="btnrow">
                  <button class="btn" onclick="mcpCopy(false)">复制 Loci 配置片段</button>
                  <button class="btn" onclick="mcpCopy(true)">复制全部为通用格式</button>
                </div>
              </div>
            </div>
          </div>

          <!-- 右栏两框 —— 照 proto/技能MCP.html：
               「技能分布」(按 Agent 数) + 「装了 2 个以上的技能」。
               ⚠️ 我之前做成「技能配置源 + 哪些 Agent 装了 Loci」，主题不对。 -->
          <aside class="sp-side">
            <div class="side-box">
              <div class="bh"><span class="t">技能分布</span><span class="n" id="sk-src-n">—</span></div>
              <div class="bd" id="sk-side-src"></div>
            </div>
            <div class="side-box">
              <div class="bh"><span class="t">装了 2 个以上的技能</span><span class="n" id="sk-loci-n">—</span></div>
              <div class="bd" id="sk-side-loci"></div>
            </div>
          </aside>
        </div>
      </section>

      <section id="v-pack" hidden>
        <div class="pagehead">
          <div>
            <h2 class="ptitle">记忆包</h2>
            <p class="psub" id="pk-psub">导出为 JSON · 跨电脑备份或发给他人导入 · 导入时按内容自动去重</p>
          </div>
          <div class="pacts">
            <button class="btn pri sm" onclick="doExport()">导出记忆包</button>
          </div>
        </div>

        <div class="panel pcard risk-ok">
          <div class="listhead phead">
            <span class="t"><span class="stepno">1</span>导出范围</span>
            <span class="hint">格式 loci-pack v1：内容 / 类型 / 重要度 / 项目 / 时间</span>
          </div>
          <div class="row">
            <select id="pk-proj" class="proj-sel" style="flex:0 0 220px"><option value="">全部项目</option></select>
            <span class="chip" id="pk-count">—</span>
          </div>
          <div class="row" style="gap:8px;align-items:center;margin-top:12px">
            <input type="checkbox" id="pack-with-sessions" style="width:auto;margin:0">
            <span>同时导出「会话层（对话原文）」</span>
            <span class="chip" id="pk-sess">—</span>
          </div>
          <p class="hint">默认只导出记忆（结论）。勾上这一项会把归档的原始对话一起打包 ——
            换电脑后原话也能恢复。导入时记忆与会话<b>两边都会自动去重</b>，重复导入不会产生第二份。</p>
        </div>

        <div class="panel pcard">
          <div class="listhead phead">
            <span class="t"><span class="stepno">2</span>导入</span>
            <span class="hint">只接受 Loci 导出的记忆包（.json）</span>
          </div>
          <!-- 按原型：拖拽区（真的支持拖拽，不是只有样子） -->
          <div class="dropzone" id="pk-drop">
            <svg viewBox="0 0 24 24"><path d="M12 16V4"/><path d="M7.5 8.5L12 4l4.5 4.5"/><path d="M4 16v3a1 1 0 001 1h14a1 1 0 001-1v-3"/></svg>
            <div class="dz">把 .json 记忆包拖到这里，或点此<u>选择文件并导入</u>
              <small>与现有记忆内容重复的条目会自动跳过</small>
            </div>
            <input type="file" id="pack-file" accept=".json" style="display:none" onchange="doImport(this)">
          </div>
        </div>

        <div class="panel pcard">
          <div class="listhead phead">
            <span class="t"><span class="stepno">3</span>技能包</span>
            <span class="hint">把经验类记忆导出成 SKILL.md</span>
          </div>
          <div class="row">
            <select id="sk-proj" class="proj-sel" style="max-width:220px"><option value="">选择项目</option></select>
            <button class="btn" onclick="skillPreview()">预览技能包</button>
            <button class="btn" onclick="skillExport()">写入 skills 目录</button>
          </div>
          <p class="hint">写进 <code>~/.workbuddy/skills</code>，各 Agent 可直接读取。</p>
          <pre class="mdbox" id="skill-out" style="display:none;margin-top:14px"></pre>
        </div>
      </section>

      <!-- 交接卡 -->
<section id="v-handoff" hidden>
        <!-- 2026-10-08 照 proto/交接卡.html 重做。原来是旧工作台那套
             （.pagehead / .panel.pcard / .tagline），与定稿原型不是一套结构。
             这次按原型逐块重写：.phead/.ttl/.psub + 5 格 .kpis
             + .cols > .sp-main > .tbl（.tbar + .pickbar + .thead + .hr 行）
             + .sp-side > 2 个 .side-box。
             ⚠️ 契约 id 全部保留：hf-q / hf-tags / hf-proj / handoff / hf-meta
                —— syncHfTags() / doHandoff() / pickHfProj() 都写它们，
                引擎层一行没动，只换外观与布局。 -->
        <div class="phead">
          <div class="ttl">
            <h2>交接卡</h2>
            <div class="psub" id="hf-psub">正在统计…</div>
          </div>
          <div class="kpis" id="hf-kpi">
            <div class="k"><div class="v" id="hk-proj">—</div><div class="l">项目</div></div>
            <div class="k"><div class="v" id="hk-mem">—</div><div class="l">可导出记忆</div></div>
            <div class="k"><div class="v" id="hk-sess">—</div><div class="l">会话</div></div>
            <div class="k"><div class="v amber" id="hk-warn">—</div><div class="l">项目名待确认</div></div>
            <div class="k"><div class="v ok" id="hk-exp">—</div><div class="l">本次已导出</div></div>
          </div>
        </div>

        <div class="cols">
          <div class="sp-main">
            <div class="tbl">
              <div class="tbar">
                <span class="t">选择项目</span><span class="c" id="hf-total">0</span>
                <div class="chips" id="hf-chips">
                  <span class="chip on" data-a="">有记忆的</span>
                  <span class="chip" data-a="warn">名称待确认</span>
                </div>
              </div>

              <!-- 原型 .pickbar：已选 N 个 / 共 M 条记忆 · 估计体积 + 格式切换 + 导出 -->
              <div class="pickbar" id="hf-pickbar" style="display:none">
                <span class="n" id="hf-sel-n">已选 0 个项目</span>
                <span class="s" id="hf-sel-s">共 0 条记忆</span>
                <div class="fmt">
                  <span class="lb">格式</span>
                  <span class="fseg" id="hf-fmt">
                    <span class="fbtn on" data-f="md">Markdown</span>
                    <span class="fbtn" data-f="json">JSON</span>
                    <span class="fbtn" data-f="both">两者都要</span>
                  </span>
                </div>
                <!-- 同采集页：一个按钮来回切 -->
                <button class="btn" id="hf-allbtn" onclick="hfToggleAll(event)">全选</button>
                <button class="btn pri push" onclick="hfExport()">导出交接卡</button>
              </div>

              <div class="thead hf-thead">
                <div><span class="cbx hd" id="hf-all" onclick="hfToggleAll(event)"></span></div>
                <div>项目</div>
                <div style="text-align:right">记忆</div>
                <div style="text-align:right">会话</div>
              </div>
              <div id="hf-list"></div>

              <!-- 契约元素：标签行 + 下拉。功能已由上表接管，收起不显示，
                   但**不能删** —— syncHfTags()/pickHfProj() 写它们，删了会抛异常。 -->
              <div class="tagline" id="hf-tags" style="display:none"></div>
              <select id="hf-proj" class="proj-sel" style="display:none"><option value="">全部项目</option></select>
              <input type="text" id="hf-q" style="display:none">
            </div>

            <div class="panel pcard" id="hf-card" style="display:none">
              <div class="listhead phead">
                <span class="t">交接卡预览</span>
                <span class="hint" id="hf-meta">—</span>
              </div>
              <div class="handoff-out" id="handoff"></div>
            </div>
          </div>

          <!-- 右栏：卡形态说明 + 项目名分布（原型两个框） -->
          <aside class="sp-side">
            <div class="side-box">
              <div class="bh"><span class="t">交接卡长什么样</span><span class="n">Markdown</span></div>
              <div class="bd">
                <p class="hintline">决策 → 偏好 → 踩坑 → 事实 → 背景，五段固定顺序。粘贴给新 Agent 即可无损续接，不用再解释一遍上下文。</p>
              </div>
            </div>
            <div class="side-box">
              <div class="bh"><span class="t">项目名分布</span><span class="n" id="hf-dist-n">—</span></div>
              <div class="bd" id="hf-dist"></div>
            </div>
          </aside>
        </div>
      </section>
    </div>
  </main>
</div>

<script>

/* ── 采集页渲染（2026-10-08 照proto/采集.html）─────────────────────────
   ⚠️ 引擎层 doScan() 一行没改 —— 它照样打 /api/scan、写 #scan-list 与
      #scan-info（两个已隐藏的契约元素）。这层只负责把同一批数据
      按原型排成5 列表格 + 右栏两框 + 5 个 KPI。

   数据实测（/api/scan）：47 行 · 18 未入库 / 29 已入库 · kind {日志 33, skill 14} */
var CK_ROWS=[], CK_SEL={}, CK_FILTER="";

function ckRender(){
  var box=document.getElementById("scan-list");
  if(!box) return;
  var rows=CK_FILTER===""?CK_ROWS
        :(CK_FILTER==="new"?CK_ROWS.filter(function(r){return !r.in_db;})
                       :CK_ROWS.filter(function(r){return r.in_db;}));
  var tot=document.getElementById("ck-total");
  if(tot) tot.textContent=rows.length;
  if(!rows.length){ box.innerHTML='<div class="empty">没有符合条件的条目</div>'; ckPickbar(); return; }
  /* 行结构照 proto/采集.html： [cbx] [标题] [来源 Agent 标签] [新增记忆] [时间]
     ⚠️ 2026-10-08 用户指出两处：
        ① 「小字」—— 我原来在标题下面加了一行灰色小字
           （「已入库 · 2026-08-21 工作日志 肥料公…」）。
           那是 AI 通病：爱加解释性小字。**整行删掉**，标题只有一行。
        ② 时间列换行 —— 70px 放不下「2026-08-21」，被拆成两行。
           加 white-space:nowrap（字体已 11px mono，够放）。 */
  box.innerHTML=rows.map(function(r,i){
    var idx=CK_ROWS.indexOf(r), on=!!CK_SEL[r.path];
    /* lock = 已入库的行。它们**不可选**（已经在库里了，再选没意义）。
       ⚠️ 2026-10-08：加了 .lock 灰态 —— 之前它们跟"可选但没勾"长得一模一样，
          结果点「全选」时如果恰好那几行已入库，屏幕上**什么都不变**，
          用户会以为按钮坏了。灰掉才看得出"全选只作用于这些" */
    var lock=!!r.in_db;
    return '<div class="cr'+(on?" on":"")+(lock?" lock":"")+'" data-i="'+idx+'"'
      +' onclick="ckToggle('+idx+')" title="'+escAttr(r.path)+'">'
      +'<span class="cbx"></span>'
      +'<div class="cn">'+esc(r.title||fNameOf(r.path))+'</div>'
      +'<div><span class="src '+ckAgentCls(r)+'">'+esc(ckAgent(r))+'</span></div>'
      +'<div class="num'+(r.memories?"":" zero")+'">'+(r.memories||0)+'</div>'
      +'<div class="tm">'+esc(r.date||"")+'</div>'
      +"</div>";
  }).join("");
  ckPickbar();
  ckSyncAll(rows);
}
/* 表头全选框的状态同步（2026-10-08）。
   只统计**可选的**行 —— 已入库的行点了没反应，全选要跳过它们，
   否则"全选"会勾上一堆勾不动的行，状态永远是"半选"。 */
function ckSelectable(rows){
  return (rows||[]).filter(function(r){return CK_ROWS.indexOf(r)>=0 && !r.in_db;});
}
function ckSyncAll(rows){
  var el=document.getElementById("ck-all");
  var pool=ckSelectable(rows);
  var on=pool.filter(function(r){return CK_SEL[r.path];}).length;
  var allOn=pool.length>0 && on===pool.length;
  /* 表头那个框 */
  if(el){
    el.classList.remove("on","part","dis");
    if(!pool.length) el.classList.add("dis");
    else if(on===pool.length) el.classList.add("on");
    else if(on>0) el.classList.add("part");
  }
  /* pickbar 那颗按钮的字 —— 跟着状态走：
     没全选 → 「全选」；已全选 → 「全不选」。
     这样用户一眼知道"再点一下会发生什么"。 */
  var btn=document.getElementById("ck-allbtn");
  if(btn) btn.textContent = allOn ? "全不选" : "全选";
}
/* 点表头那个框：没全选就全选，已全选就全取消。
   （函数本体在下面 ckToggleAll —— 和 pickbar 的「全不选」共用一个，
     免得两个同名函数互相覆盖。） */

/* 来源 Agent：源文件路径里带 .workbuddy / .trae / .zcode / .qoder 就认出来。
   认不出就退回 source 字段（不硬猜）。 */
function ckAgent(r){
  var p=String((r&&r.path)||"").toLowerCase();
  if(p.indexOf(".workbuddy")>=0||p.indexOf("workbuddy")>=0) return "WorkBuddy";
  if(p.indexOf(".trae")>=0) return "Trae";
  if(p.indexOf(".zcode")>=0) return "ZCode";
  if(p.indexOf(".qoder")>=0) return "Qoder";
  if(p.indexOf(".codex")>=0) return "Codex";
  return (r&&r.source)||"—";
}
function ckAgentCls(r){
  var a=ckAgent(r).toLowerCase();
  if(a.indexOf("workbuddy")>=0) return "wb";
  if(a.indexOf("zcode")>=0) return "zc";
  if(a.indexOf("qoder")>=0) return "qd";
  if(a.indexOf("trae")>=0) return "tr";
  return "ot";
}
function fNameOf(p){
  var s=String(p||"").split(/[\\/]/);
  return s.pop()||String(p||"");
}
function ckPickbar(){
  var keys=Object.keys(CK_SEL).filter(function(k){return CK_SEL[k];});
  var pb=document.getElementById("ck-pickbar");
  if(pb) pb.style.display=keys.length?"flex":"none";
  var n=document.getElementById("ck-sel-n"), s=document.getElementById("ck-sel-s");
  if(n) n.textContent="已选 "+keys.length+" 条";
  if(s){
    var kb=0;
    keys.forEach(function(k){
      var r=CK_ROWS.filter(function(x){return x.path===k;})[0];
      if(r) kb+=(r.size||0);
    });
    s.textContent=kb?("合计 "+Math.round(kb/1024)+" KB"):"—";
  }
}
function ckToggle(i){
  var r=CK_ROWS[i]; if(!r||r.in_db) return;   /* 已入库的不给选 */
  CK_SEL[r.path]=!CK_SEL[r.path];
  ckRender();
}
function ckToggleAll(v){
  /* 一键全选（2026-10-08 用户要求）。**一个函数两种调用**，别再写第二个同名：
       · 表头那个框   onclick="ckToggleAll(event)" → 传进来的是**事件对象**
                       → 行为：没全选就全选，已全选就全取消（切换）
       · pickbar 的「全不选」onclick="ckToggleAll(false)" → 传进来的是布尔
                       → 行为：按传入值设定（false=全不选 / true=全选）
     ⚠️ 之前我另写了一个同名函数，**后定义的会覆盖前者** ——
        幸好发现得早，不然表头点了没反应还找不到原因。
     ⚠️ 只作用于**当前筛选视图里可选的**行（已入库的点不动，要跳过）。 */
  var isEvt = v && typeof v==="object" && v.stopPropagation;
  if(isEvt && v.stopPropagation) v.stopPropagation();
  var rows=CK_FILTER===""?CK_ROWS
        :(CK_FILTER==="new"?CK_ROWS.filter(function(r){return !r.in_db;})
                       :CK_ROWS.filter(function(r){return r.in_db;}));
  var pool=ckSelectable(rows);
  if(!pool.length) return;
  var val = isEvt ? !pool.every(function(r){return !!CK_SEL[r.path];}) : !!v;
  pool.forEach(function(r){ CK_SEL[r.path]=val; });
  ckRender();
}
function ckInitChips(){
  var box=document.getElementById("ck-chips");
  if(!box||box.dataset.on) return;
  box.dataset.on="1";
  box.addEventListener("click",function(e){
    var c=e.target.closest?e.target.closest(".chip"):null;
    if(!c) return;
    box.querySelectorAll(".chip").forEach(function(x){x.classList.remove("on")});
    c.classList.add("on");
    CK_FILTER=c.dataset.a||"";
    ckRender();
  });
}
/* 右栏：各来源待入库 + 扫描根目录 */
function ckSide(){
  var b1=document.getElementById("ck-src-box"), n1=document.getElementById("ck-src-n");
  if(b1){
    var by={};
    CK_ROWS.forEach(function(r){
      var k=r.source||"未知";
      if(!by[k]) by[k]={n:0,newN:0};
      by[k].n++; if(!r.in_db) by[k].newN++;
    });
    var keys=Object.keys(by).sort(function(a,b){return by[b].n-by[a].n;});
    var COL={"WorkBuddy":"#4338ca","ZCode":"#0f766e","Trae":"#a78bfa","Qoder":"#b45309"};
    b1.innerHTML=keys.length?keys.map(function(k){
      return '<div class="rt"><span class="sw" style="background:'+(COL[k]||"#857f74")+'"></span>'
        +'<span class="nm" title="'+escAttr(k)+'">'+esc(k.length>18?k.slice(0,18)+"…":k)+'</span>'
        +'<span class="vv'+(by[k].newN?" warn":" zero")+'">'+by[k].n+'</span></div>';
    }).join(""):'<div class="hintline">还没扫描。</div>';
  }
  if(n1){
    var nn=CK_ROWS.filter(function(r){return !r.in_db;}).length;
    n1.textContent=nn+" 待入库";
  }
  var b2=document.getElementById("ck-root-box"), n2=document.getElementById("ck-root-n");
  if(b2){
    var srcs=(window.SRCS||[]);
    var roots=(window.SCANROOTS||[]);
    var html="";
    if(roots.length){
      html=roots.map(function(r){
        var cnt=CK_ROWS.length;
        return '<div class="rt"><span class="sw" style="background:#4338ca"></span>'
          +'<span class="nm" title="'+escAttr(r)+'">'+esc(r)+'</span>'
          +'<span class="vv">自定义</span></div>';
      }).join("");
    }
    /* 已探测到的来源（2026-10-07 做的动态探测的真实数据） */
    if(srcs.length){
      html+=srcs.map(function(s){
        var st=s.state==="found"?"可读":(s.state==="unreadable"?"非对话":"读不到");
        var cls=s.state==="found"?"vv":(s.state==="unreadable"?"vv warn":"vv zero");
        return '<div class="rt"><span class="sw" style="background:'+
          (s.state==="found"?"#4338ca":"#bbb7ae")+'"></span>'
          +'<span class="nm" title="'+escAttr(s.root||"")+'">'+esc(s.agent)+'</span>'
          +'<span class="'+cls+'">'+esc(st)+'</span></div>';
      }).join("");
    }
    b2.innerHTML=html||'<div class="hintline">没有可统计的扫描根。</div>';
  }
  if(n2) n2.textContent=String((window.SCANROOTS||[]).length)+" 自定义";
}
function ckKpi(){
  var newN=CK_ROWS.filter(function(r){return !r.in_db;}).length;
  var oldN=CK_ROWS.length-newN;
  var put=function(id,v){var e=document.getElementById(id);
    if(e){if(window.countUp)countUp(e,v);else e.textContent=v;}};
  put("ckpi-new",newN);
  put("ckpi-old",oldN);
  /* 「已读消息」/「抽出记忆」这两个口径 /api/scan **给不出来**（它只有
     行级 path/kind/size/in_db）。用会话数与记忆数做**相关但不同**的口径，
     标签写清楚，不冒充。 */
  var put2=function(id,v){var e=document.getElementById(id);if(e)e.textContent=v;};
  put2("ckpi-msg",(window.CK_SESS||"—"));
  put2("ckpi-mem",(window.CK_MEM||"—"));
  put("ckpi-src",(window.SCANROOTS||[]).length);
  var ps=document.getElementById("ck-psub");
  if(ps) ps.innerHTML="<b>"+CK_ROWS.length+"</b> 个条目 · <b>"+newN+"</b> 条未入库 · "
    +(window.SRCS||[]).length+" 个来源";
}
function ckRenderAll(){
  ckRender(); ckSide(); ckKpi(); ckInitChips();
}
async function loadCollectPage(){
  try{ CK_ROWS=(await api("/api/scan"))||[]; }catch(e){ CK_ROWS=[]; }
  /* 「新增记忆」这一列：/api/scan **没有** memories 字段，
     但 /api/sourcefiles 有 —— 按 path 做一次关联（实测 33/47 能对上）。
     对不上的（多是技能文件）显示 0，**不编数**，也不拿别的数顶替。 */
  try{
    var sf=(await api("/api/sourcefiles"))||[];
    var m={};
    sf.forEach(function(f){ m[f.path]=f.memories||0; });
    CK_ROWS.forEach(function(r){ r.memories=m[r.path]; });
  }catch(e){}
  /* 探测来源（复用 2026-10-07 那套动态探测）+ 自定义扫描根 */
  try{ var r=await api("/api/conv-sources"); SRCS=r.sources||[]; }catch(e){ SRCS=[]; }
  try{ var q=await api("/api/scan-roots"); SCANROOTS=q.roots||[]; }catch(e){ SCANROOTS=[]; }
  /* 会话数与记忆数（给 KPI 另两格用，标签已写明口径不同） */
  try{ var st=await api("/api/stats"); CK_MEM=st.total||0; }catch(e){}
  try{ var ss=await api("/api/session/list?limit=300"); CK_SESS=(ss||[]).length; }catch(e){}
  ckRenderAll();
}

/* ── 库页渲染 第二版（2026-10-08，严格照proto/库.html）────────────────
   ⚠️ 第一版的问题：只对齐了结构，视觉偏了 6 处（kv3 没底色、chead 缺编号
      徽章、pathline 没灰底药丸、dangerbar 用 color-mix 而非固定 #fdf2f4、
      三张卡内容搞错、缺 divider）。本版逐条照抄原型。

   引擎层照旧：loadSourceFiles() / loadArchive() / cleanupPreview() /
   cleanupRun() / doExport() 一行没改，契约元素都留着。 */
var CL_ST={}, CL_SF=[], CL_BK=[], CL_SEL={};

function clFmtSize(n){
  if(!n) return "0 KB";
  if(n>=1048576) return (n/1048576).toFixed(1)+" MB";
  return Math.round(n/1024)+" KB";
}
function fName(p){
  var s=String(p||"").split(/[\\/]/);
  return s.pop()||String(p||"");
}

/* ① 备份卡片：备份目录 + 已有的备份文件（pathline 药丸） */
function clRenderBackup(){
  var box=document.getElementById("backup-list");
  if(!box) return;
  var c=document.getElementById("bk-c");
  if(c) c.textContent=CL_BK.length+" 份";
  /* 备份目录那一行 */
  var dirRow='<div class="pathline"><span class="pd" id="bk-dirpath">—</span>'
    +'<span class="st">保留最近 30 份</span></div>';
  var fileRows="";
  if(CL_BK.length){
    fileRows=CL_BK.map(function(b){
      var size=(typeof b==="object")?(b.size||0):0;
      var path=(typeof b==="object")?(b.path||b.name||""):String(b);
      var at=(typeof b==="object")?(b.mtime||b.created_at||""):"";
      return '<div class="pathline"><span class="pd" title="'+escAttr(path)+'">'+esc(fName(path))+'</span>'
        +'<span class="st">'+esc(at)+" · "+esc(clFmtSize(size))+'</span></div>';
    }).join("");
  }else{
    fileRows='<div class="pathline"><span class="pd" style="color:var(--r3-faint)">'
      +'还没有备份，点上面「立即备份」生成一份</span></div>';
  }
  box.innerHTML=dirRow+fileRows;
  /* 把目录写进药丸里（loadArchive 可能已经填了 arch-dir） */
  var dp=document.getElementById("bk-dirpath");
  var ad=document.getElementById("arch-dir");
  var dir=(ad&&ad.textContent&&ad.textContent!=="—")?ad.textContent:"D:/Loci（本机目录）";
  if(dp) dp.textContent=dir;
  var bd=document.getElementById("bk-dir");
  if(bd) bd.textContent="已配置";
  var bs=document.getElementById("bk-size");
  if(bs){
    var tot=CL_SF.reduce(function(a,f){return a+(f.size||0);},0);
    bs.textContent=clFmtSize(tot);
  }
  var bb=document.getElementById("bk-auto");
  if(bb) bb.textContent="每次打开检查";
}

/* ③ 销毁卡片：源文件表（复选框 + 大小 + 记忆） */
function clRenderFiles(){
  var box=document.getElementById("sf-list");
  if(!box) return;
  var n=document.getElementById("sf-c");
  var selN=Object.keys(CL_SEL).filter(function(k){return CL_SEL[k];}).length;
  if(n) n.textContent=selN+" / "+CL_SF.length+" 个";
  if(!CL_SF.length){ box.innerHTML='<div class="empty">本机没有记忆源文件</div>'; clSyncAll(); return; }
  box.innerHTML=CL_SF.map(function(f,i){
    var on=!!CL_SEL[f.path];
    return '<div class="sfr'+(on?" on":"")+'" data-i="'+i+'" onclick="clToggleSel('+i+')"'
      +' title="'+escAttr(f.path)+'">'
      +'<span class="cbx"></span>'
      +'<div class="fn">'+esc(fName(f.path))+'</div>'
      +'<div class="num zero">'+esc(clFmtSize(f.size))+'</div>'
      +'<div class="num'+(f.memories?"":" zero")+'">'+(f.memories||0)+'</div>'
      +"</div>";
  }).join("");
  /* dangerbar 上的汇总 */
  var db=document.getElementById("db-txt");
  if(db){
    if(!selN){ db.textContent="还没选文件"; }
    else{
      var byIdx=CL_SF.filter(function(f,j){return CL_SEL[f.path];});
      var mb=byIdx.reduce(function(a,f){return a+(f.size||0);},0)/1048576;
      db.textContent="已选 "+selN+" 个文件 · "+mb.toFixed(1)+" MB";
    }
  }
  var go=document.getElementById("db-go");
  if(go) go.disabled=!selN;
  clSyncAll();
}
/* 表头全选框 + 按钮文字（2026-10-08，与采集页同一套） */
function clSyncAll(){
  var on=CL_SF.filter(function(f){return CL_SEL[f.path];}).length;
  var allOn=CL_SF.length>0 && on===CL_SF.length;
  var el=document.getElementById("cl-all");
  if(el){
    el.classList.remove("on","part");
    if(allOn) el.classList.add("on");
    else if(on>0) el.classList.add("part");
  }
  var btn=document.getElementById("cl-allbtn");
  if(btn) btn.textContent = allOn ? "全不选" : "全选";
}
/* 点表头框或按钮：没全选→全选，已全选→全取消 */
function clToggleAll(ev){
  if(ev && ev.stopPropagation) ev.stopPropagation();
  if(!CL_SF.length) return;
  var allOn=CL_SF.every(function(f){return !!CL_SEL[f.path];});
  CL_SF.forEach(function(f){ CL_SEL[f.path]=!allOn; });
  clRenderFiles();
}
function clToggleSel(i){
  var f=CL_SF[i]; if(!f) return;
  CL_SEL[f.path]=!CL_SEL[f.path];
  clRenderFiles();
}
function clPreviewSelect(){
  /* 只勾「有文件但没记忆指向」的 —— 那才是可销毁的残留 */
  CL_SF.forEach(function(f){ CL_SEL[f.path]=!(f.memories>0); });
  clRenderFiles();
}

/* 右栏：可销毁残留 + 来源分布 */
function clRenderSide(){
  /* 第1 框「源文件分布」—— 照原型：留存情况 + 按 Agent 分，**合在一框**。
     （我之前拆成两框、标题写成「可销毁残留 + 来源分布」，主题不对。）*/
  var zero=CL_SF.filter(function(f){return !(f.memories>0);}).length;
  var box=document.getElementById("orp-box"), n=document.getElementById("orp-n");
  if(box){
    var by={};
    CL_SF.forEach(function(f){var k=f.agent||"未知";by[k]=(by[k]||0)+1;});
    var COL={"WorkBuddy":"#4338ca","ZCode":"#0f766e","Trae":"#a78bfa","Qoder":"#b45309"};
    var keys=Object.keys(by).sort(function(a,b){return by[b]-by[a];});
    box.innerHTML=
      '<div class="rt"><span class="sw" style="background:var(--lz-coral)"></span>'
      +'<span class="nm">可销毁残留</span><span class="vv warn">'+zero+'</span></div>'
      +'<div class="rt"><span class="sw" style="background:var(--lz-ok)"></span>'
      +'<span class="nm">仍在用</span><span class="vv">'+(CL_SF.length-zero)+'</span></div>'
      +keys.map(function(k){
        return '<div class="rt"><span class="sw" style="background:'+(COL[k]||"#857f74")+'"></span>'
          +'<span class="nm">'+esc(k)+'</span><span class="vv">'+by[k]+'</span></div>';
      }).join("")
      +'<div class="hintline" style="padding-top:9px">'
      +'「可销毁」的判定：该源文件里已经没有会话了 —— 对话在 Agent 里被删了，'
      +'只剩文件躺在磁盘上。</div>';
  }
  if(n) n.textContent=CL_SF.length+" 个";
}

function clKpi(){
  var s=CL_ST;
  var totMB=CL_SF.reduce(function(a,f){return a+(f.size||0);},0)/1048576;
  var put=function(id,v){var e=document.getElementById(id);
    if(e){if(window.countUp)countUp(e,v);else e.textContent=v;}};
  put("clpi-mem",s.total||0);
  put("clpi-sup",(typeof s.superseded==="number")?s.superseded:0);
  put("clpi-src",CL_SF.length);
  put("clpi-bak",CL_BK.length);
  /* MB 是小数，**不走 countUp**（那个按整数滚动，0.6 会被滚成 1） */
  var mbEl=document.getElementById("clpi-mb");
  if(mbEl) mbEl.textContent=totMB.toFixed(1);
  var ps=document.getElementById("cl-psub");
  if(ps) ps.innerHTML="<b>"+(s.total||0)+"</b> 条记忆 · <b>"+CL_SF.length+
    "</b> 个源文件 · 合计 <b>"+totMB.toFixed(1)+"</b> MB";
}
function clRenderAll(){
  clRenderBackup(); clRenderFiles(); clRenderSide(); clKpi();
}
async function loadLibraryPage(){
  try{ CL_ST=await api("/api/stats"); }catch(e){ CL_ST={}; }
  try{ CL_SF=(await api("/api/sourcefiles"))||[]; }catch(e){ CL_SF=[]; }
  try{ CL_BK=(await api("/api/backups"))||[]; }catch(e){ CL_BK=[]; }
  clRenderAll();
}

/* ── 技能 / MCP 页渲染（2026-10-08 照 proto/技能MCP.html）─────────────────
   ⚠️ 引擎层 loadSkills() 一行没改——它照样去探 /api/skills 与 /api/content，
      照样填 SKILLS/SKTGT/SKSRC/CFGS/MCPS/CSRC。
      这层只做两件事：
        ① 把探回来的数据按原型重排（表格 + 300px 右栏）
        ② 算原型的5 个 KPI
      契约元素 #sk-srcs / #sk-mcps / #sk-out / #sk-detail / #sk-target /
      #sk-q 仍然留在DOM 里（loadSkills 后面那几段还在写它们），只是不显示。 */
var SK_SEL={}, SK_FILTER="";

function skScopeTag(s){
  /* 来源列：Agent + 作用域（原型那一列就是来源） */
  var a=s.agent||"";
  return a?(a+(s.scope==="用户级"?"":" · "+(s.scope||""))):(s.scope||"—");
}
function skFilterRows(){
  if(SK_FILTER==="") return SKILLS;
  if(SK_FILTER==="builtin") return SKILLS.filter(function(s){return s.scope!=="用户级";});
  return SKILLS.filter(function(s){return s.scope==="用户级";});
}
function skRenderRows(){
  var box=document.getElementById("sk-list");
  if(!box) return;
  var rows=skFilterRows();
  var tot=document.getElementById("sk-total");
  if(tot) tot.textContent=rows.length;
  if(!rows.length){ box.innerHTML='<div class="empty">没有符合条件的技能</div>'; skSyncAll(); return; }
  box.innerHTML=rows.map(function(s){
    var i=SKILLS.indexOf(s), on=!!SK_SEL[s.name];
    return '<div class="sr'+(on?" on":"")+'" data-i="'+i+'"'
      +' onclick="skToggle('+i+')" title="'
        +escAttr((s.desc? s.desc+" " : "")+(s.path||""))+'">'
      +'<span class="cbx"></span>'
      +'<div class="sn">'+esc(s.name)+'</div>'
      +'<div class="srcTag">'+esc(skScopeTag(s))+'</div>'
      /* 第 4 列「文件」：/api/skills 的每个 skill 本来就带 `files` 字段
         （引擎里 os.walk 数出来的），之前是我没接上，才留的空位。 */
      +'<div class="num'+(s.files?"":" zero")+'">'+(s.files||0)+'</div>'
      +"</div>";
  }).join("");
  var keys=Object.keys(SK_SEL).filter(function(k){return SK_SEL[k];});
  var pb=document.getElementById("sk-pickbar");
  if(pb) pb.style.display=keys.length?"flex":"none";
  var n=document.getElementById("sk-sel-n"), d=document.getElementById("sk-sel-s");
  if(n) n.textContent="已选 "+keys.length+" 个技能";
  if(d){
    /* 目标 Agent 里 ready 的有几个 —— 只有 ready 的才能真传过去 */
    var ready=(SKTGT||[]).filter(function(t){return t.ready;});
    d.textContent=keys.length?("可传到 "+ready.length+" 个已就绪的目标"):"—";
  }
  skSyncAll();
}
/* 表头全选框的状态 + 按钮文字（2026-10-08，与采集页同一套做法） */
function skSyncAll(){
  var rows=skFilterRows();
  var on=rows.filter(function(s){return SK_SEL[s.name];}).length;
  var allOn=rows.length>0 && on===rows.length;
  var el=document.getElementById("sk-all");
  if(el){
    el.classList.remove("on","part");
    if(allOn) el.classList.add("on");
    else if(on>0) el.classList.add("part");
  }
  var btn=document.getElementById("sk-allbtn");
  if(btn) btn.textContent = allOn ? "全不选" : "全选";
}
/* 点表头框或按钮：没全选→全选，已全选→全取消 */
function skToggleAll(ev){
  if(ev && ev.stopPropagation) ev.stopPropagation();
  var rows=skFilterRows();
  if(!rows.length) return;
  var allOn=rows.every(function(s){return !!SK_SEL[s.name];});
  rows.forEach(function(s){ SK_SEL[s.name]=!allOn; });
  skRenderRows(); skRenderDetail();
}
function skToggle(i){
  var s=SKILLS[i]; if(!s) return;
  SK_SEL[s.name]=!SK_SEL[s.name];
  skRenderRows();
  skRenderDetail();
}
function skRenderDetail(){
  var box=document.getElementById("sk-detail");
  if(!box) return;
  var keys=Object.keys(SK_SEL).filter(function(k){return SK_SEL[k];});
  if(!keys.length){ box.innerHTML=""; return; }
  var one=SKILLS.filter(function(s){return s.name===keys[0];})[0];
  if(!one){ box.innerHTML=""; return; }
  box.innerHTML='<div class="dbody">'
    +'<div style="font-size:13px;font-weight:600;color:var(--r3-ink);margin-bottom:6px">'
      +esc(one.name)+'</div>'
    +'<div>'+esc(one.desc||"（这个技能没有说明文字）")+'</div>'
    +'<div class="dpath">'+esc(one.path||"")+'</div>'
    +(keys.length>1?('<div style="margin-top:8px;font-size:11px;color:var(--lz-amber)">'
      +'另有 '+(keys.length-1)+' 个已选，复制时会一并带上。</div>'):'')
    +"</div>";
}
function skRenderSide(){
  var srcBox=document.getElementById("sk-side-src");
  var srcN=document.getElementById("sk-src-n");
  var lociBox=document.getElementById("sk-side-loci");
  var lociN=document.getElementById("sk-loci-n");
  /* ① 技能分布 —— 按 Agent 分（原型主题）。
     ⚠️ 之前错做成「技能配置源」（列目录），原型要的是「技能分布」（列各 Agent 有几个）。*/
  if(srcBox){
    var by={};
    SKILLS.forEach(function(s){var k=s.agent||"未知";by[k]=(by[k]||0)+1;});
    var COL={"WorkBuddy":"#4338ca","Trae":"#a78bfa","ZCode":"#0f766e","Qoder":"#b45309","Codex":"#5f5e5a"};
    var keys=Object.keys(by).sort(function(a,b){return by[b]-by[a];});
    srcBox.innerHTML=keys.length?keys.map(function(k){
      return '<div class="rt"><span class="sw" style="background:'+(COL[k]||"#857f74")+'"></span>'
        +'<span class="nm">'+esc(k)+'</span><span class="vv">'+by[k]+'</span></div>';
    }).join(""):'<div class="hintline">没探到技能。</div>';
  }
  if(srcN) srcN.textContent=SKILLS.length+" 个";
  /* ② 装了 2 个以上的技能 —— 同名技能出现在 2+ 个 Agent 里才算。
     口径写清：按**技能名**跨 Agent 去重计数。数据里没有重叠就是 0，不编。 */
  if(lociBox){
    var nm={};
    SKILLS.forEach(function(s){
      var k=s.name; if(!k) return;
      if(!nm[k]) nm[k]={};
      nm[k][s.agent||"?"]=1;
    });
    var multi=Object.keys(nm).filter(function(k){return Object.keys(nm[k]).length>=2;});
    multi.sort(function(a,b){return Object.keys(nm[b]).length-Object.keys(nm[a]).length;});
    lociBox.innerHTML=multi.length?multi.map(function(k){
      var ags=Object.keys(nm[k]);
      return '<div class="rt"><span class="sw" style="background:#047857"></span>'
        +'<span class="nm" title="'+escAttr(k)+'">'+esc(ags.join(" + "))+'</span>'
        +'<span class="vv">'+ags.length+'</span></div>';
    }).join(""):'<div class="empty">0 个</div>';
  }
  if(lociN) lociN.textContent='0 个';
}
/* 底部 MCP 卡片渲染：/api/content 的 mcps 是**真数据**（配置文件路径 + 归属 Agent） */
function skRenderMcp(){
  var box=document.getElementById("mcp-list"), c=document.getElementById("mcp-c");
  if(!box) return;
  if(c) c.textContent=(MCPS||[]).length+" 个";
  box.innerHTML=(MCPS||[]).length?MCPS.map(function(m){
    var short=String(m.path||"").replace(/^[A-Za-z]:\\Users\\[^\\]+/,"~");
    return '<div class="pathline"><span class="pd" title="'+escAttr(m.path||"")+'">'
      +esc(short)+'</span>'
      +'<span class="st">'+esc(m.name||"")+(m.agent?(" · "+esc(m.agent)):"")+'</span></div>';
  }).join(""):'<div class="hintline">本机没探到 MCP 配置文件。</div>';
}
/* 多选复制 —— pickbar 的「复制到目标 Agent」调它。
   ⚠️ 原来按钮写的是 `doCopySkills()`，但**这个函数不存在**（我引用了空气）。
      现成的 `skCopy()` 依赖单选全局 SKCUR，跟这页的多选不兼容，
      所以这里自己实现：遍历已选技能 → 逐个 plan + copy，复用**同一个后端接口**。 */
async function doCopySkills(){
  var keys=Object.keys(SK_SEL).filter(function(k){return SK_SEL[k];});
  if(!keys.length){ toast("先选至少一个技能",'err'); return; }
  /* 目标：优先用下拉里选的；没选就取第一个「已就绪」的目标 */
  var root="";
  if(typeof skTargetRoot==="function") root=skTargetRoot();
  if(!root){
    var t=(SKTGT||[]).filter(function(x){return x.ready;})[0];
    if(!t){ toast("本机没有可写入的技能目录",'err'); return; }
    root=t.root;
  }
  var okN=0, fail=[];
  for(var i=0;i<keys.length;i++){
    var sk=SKILLS.filter(function(x){return x.name===keys[i];})[0];
    if(!sk){ continue; }
    try{
      var pl=await api("/api/skill/plan?src="+encodeURIComponent(sk.path)+"&target="+encodeURIComponent(root));
      if(pl.error){ fail.push(sk.name); continue; }
      var r=await api("/api/skill/copy",{method:"POST",
        headers:{"Content-Type":"application/json"},
        body:JSON.stringify({src:sk.path,target:root,overwrite:!!pl.dst_exists})});
      if(r.error){ fail.push(sk.name); } else { okN++; }
    }catch(e){ fail.push(sk.name); }
  }
  if(okN) toast("已传 "+okN+" 个技能到 "+root,'ok');
  if(fail.length) toast(fail.length+" 个没传过去："+fail.slice(0,3).join(" / "),'err');
  SK_SEL={}; skRenderRows(); skRenderDetail();
}
function mcpCopy(all){
  var list=(MCPS||[]);
  if(!list.length){ toast("没有可复制的 MCP 配置",'err'); return; }
  var txt;
  if(all){
    txt=JSON.stringify({mcpServers:list.reduce(function(o,m){o[m.name]={command:m.command,args:m.args||[]};return o;},{})},null,2);
  }else{
    var loci=list.filter(function(m){return String(m.name||"").toLowerCase().indexOf("loci")>=0;})[0];
    if(!loci){ toast("本机没探到 loci 的 MCP 配置",'err'); return; }
    txt=JSON.stringify({mcpServers:{loci:{command:loci.command,args:loci.args||[]}}},null,2);
  }
  if(navigator.clipboard) navigator.clipboard.writeText(txt);
  toast("已复制 MCP 配置片段");
}
function skInitChips(){
  var box=document.getElementById("sk-chips");
  if(!box||box.dataset.on) return;
  box.dataset.on="1";
  box.addEventListener("click",function(e){
    var c=e.target.closest?e.target.closest(".chip"):null;
    if(!c) return;
    box.querySelectorAll(".chip").forEach(function(x){x.classList.remove("on")});
    c.classList.add("on");
    SK_FILTER=c.dataset.a||"";
    skRenderRows();
  });
}
/* 原型的 5 个 KPI：技能 / 全家都有 / MCP 服务器 / 本机 Loci / 仅在本机 */
function skKpi(){
  var all=SKILLS.length;
  var readyT=(SKTGT||[]).filter(function(t){return t.ready;});
  var lociN=(MCPS||[]).filter(function(m){
    return String(m.name||"").toLowerCase().indexOf("loci")>=0;}).length;
  /* 2026-10-08 用户拍板砍掉 MCP 之后，KPI 改成**五格真能算出来的**：
       技能        = skills[] 长度
       可传目标    = targets[] 里 ready=true 的个数（能真传过去的）
       用户级= scope=="用户级" 的技能数（内置 vs 用户级是两回事）
       来源 Agent  = skills[].agent 的去重个数
       已接 Loci   = MCPS 里叫 loci 的个数（只读状态，降级成一格，不占标题）
     原来的「全家都有 / 仅在本机」删掉 —— 后端没有"技能×目标"命中矩阵，
     算不出来，硬算就是编。 */
  var usr=SKILLS.filter(function(s){return s.scope==="用户级";}).length;
  var fromAgents={};
  SKILLS.forEach(function(s){ if(s.agent) fromAgents[s.agent]=1; });
  var put=function(id,v){var e=document.getElementById(id);
    if(e){if(window.countUp)countUp(e,v);else e.textContent=v;}};
  put("skpi-skill",all);
  put("skpi-tgt",readyT.length);
  put("skpi-usr",usr);
  put("skpi-agent",Object.keys(fromAgents).length);
  put("skpi-loci",lociN);
  var ps=document.getElementById("sk-psub");
  if(ps) ps.innerHTML="<b>"+all+"</b> 个技能 · 用户级 <b>"+usr+"</b> · 可传目标 <b>"+readyT.length+
    "</b> 个 · 已接 Loci <b>"+lociN+"</b> 处";
}
function skRenderAll(){
  skRenderRows(); skRenderDetail(); skRenderSide(); skInitChips(); skKpi(); skRenderMcp();
}
const TL={fact:"事实",decision:"决策",preference:"偏好",skill:"经验",error:"踩坑",context:"背景",summary:"摘要"};
/* FIX-3：改成主题感知令牌 —— 内联样式可以直接吃 CSS 变量，所以这里只换字符串，
   不用加任何取值逻辑。暗色令牌值 = 原来的 hex，观感零变化。 */
const TC={fact:"var(--data-fact)",decision:"var(--data-decision)",preference:"var(--data-preference)",
  skill:"var(--data-skill)",error:"var(--data-error)",context:"var(--data-context)",
  summary:"var(--data-context)"};
/* 记忆列表行 30px 图标块里那 16px 的图形（2026-09-24 改版）。
   与 TL/TC 同源同序 —— 新增类型时三处一起加。
   rowIcon() 找不到对应图形时**退回一个中性圆**，不会画出空白块（少一个图标是小事，
   列表里出现一排空方块才是事故）。 */
const TI={
  fact:'<circle cx="8" cy="8" r="3.2"/><circle cx="8" cy="8" r="6"/>',
  decision:'<path d="M3.2 8.4l3.2 3.2 6.4-7"/>',
  preference:'<path d="M8 13.2S2.8 10 2.8 6.6A2.7 2.7 0 018 5a2.7 2.7 0 015.2 1.6c0 3.4-5.2 6.6-5.2 6.6z"/>',
  skill:'<path d="M8 2.4l1.7 3.5 3.8.5-2.8 2.7.7 3.8L8 11l-3.4 1.9.7-3.8L2.5 6.4l3.8-.5z"/>',
  error:'<path d="M8 3.4v5.2"/><circle cx="8" cy="12" r="1"/>',
  context:'<path d="M3 4h10M3 8h10M3 12h6"/>',
  summary:'<path d="M3 3.8h10v8.4H3z"/><path d="M5.4 6.8h5.2M5.4 9.6h3.4"/>'
};
const TI_FILL={skill:1};   /* 只有"经验"是实心星，其余按描边画（与原型同一套观感） */
function rowIcon(t){
  if(!TI[t])return '<svg viewBox="0 0 16 16" fill="none" stroke="currentColor"'
    +' stroke-width="1.8" stroke-linecap="round"><circle cx="8" cy="8" r="2.6"/></svg>';
  return '<svg viewBox="0 0 16 16" '+(TI_FILL[t]
    ? 'fill="currentColor"'
    : 'fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"')
    +'>'+TI[t]+'</svg>';
}

/* ── 会话行的图标块（2026-09-25）─────────────────────────────
   与记忆行共用同一套 .mico 尺寸，只是"按什么分色"不同：
   记忆行按**类型**（事实/决策/踩坑…），会话行按**来源 Agent**。
   理由：会话本身没有类型，而在一列会话里最先想分辨的就是"这段是在哪个工具里聊的"。
   图标统一用对话气泡 —— 会话说到底都是"一段对话"，给每个 Agent 编一套图标
   只会让列表更花，反而看不出差别。 */
var SESS_ICON='<svg viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.7"'
  +' stroke-linecap="round" stroke-linejoin="round"><path d="M2 3.5h12v7H7l-3 2.5v-2.5H2z"/></svg>';
let searching=false;

/* ── 导航微动效（2026-10-05 增量层）────────────────────────────────────
   纯 Vanilla、零依赖。职责边界：只做"指示器位置 / 按下反馈 / 进场"，
   选页逻辑仍由 show() 负责 —— 本模块不碰 hidden、不碰业务。
   排列主轴从 computed flex-direction 读：row → 走 X 轴，column → 走 Y 轴，
   这样将来侧栏改竖排不用改这段代码。 */
var navMotion=(function(){
  var nav=document.querySelector("nav");
  if(!nav) return {move:function(){},enter:function(){}};
  var ind=document.getElementById("navInd");
  var items=[].slice.call(nav.querySelectorAll(".nav"));
  var axis=window.getComputedStyle(nav).flexDirection.indexOf("column")>=0?"y":"x";
  var reduced=window.matchMedia("(prefers-reduced-motion: reduce)");
  var inited=false, cur=null, raf=0;

  /* 批量读 → 统一写：先一次量完所有 rect，再写 transform，
     避免 read/write 交替触发 layout thrash（6 项时尤其明显）。 */
  /* 指示器定位与尺寸。
     ⚠️ 外层 .nav-ind 只写 transform 位移，尺寸全交给 ::before 的
        left/top/width/height。两者时长不同（位移 300ms、尺寸 260ms），
        写在一起会被一起拖长产生"拉丝"。
     ⚠️ 方案 A（2026-10-06 用户拍板）：--nav-scale-active = 1，
        底块与导航项格子完全等大，不需要外扩。
        所以这里直接用 offsetWidth/offsetHeight，不做任何缩放计算。 */
  function place(el){
    if(!el) return;
    var w=el.offsetWidth||1, h=el.offsetHeight||1;
    ind.style.setProperty("--nav-ind-x","0px");
    ind.style.setProperty("--nav-ind-y","0px");
    ind.style.setProperty("--nav-ind-w",w+"px");
    ind.style.setProperty("--nav-ind-h",h+"px");
    ind.style.transform="translate3d("+el.offsetLeft+"px,"+el.offsetTop+"px,0)";
  }
  function move(v,animate){
    /* ⚠️ 2026-10-06：v="sdetail"（会话详情独立页）在导航里没有对应项 ——
       底部仍只有 6 项，详情页复用「会话」那一项的高亮。
       这里把它映射回 session，否则查不到元素直接 return，
       指示器会停在原处不动（看着像卡住了）。 */
    if(v==="sdetail") v="session";
    var el=nav.querySelector('.nav[data-v="'+v+'"]');
    if(!el) return;
    /* 首帧：先定位再撤掉 .init（transition:none），页面加载时不会"飞入" */
    if(!inited){ place(el); ind.classList.add("init");
      void ind.offsetWidth; ind.classList.remove("init");
      ind.classList.add("ready"); inited=true; }
    else { place(el); }
    cur=v;
  }
  /* 内容区进场：与 indicator 同一帧触发，避免"指示器先到、内容后到"的割裂 */
  function enter(v){
    var sec=document.getElementById("v-"+v);
    if(!sec||reduced.matches) return;
    sec.classList.remove("nav-enter"); void sec.offsetWidth; sec.classList.add("nav-enter");
  }
  function sync(){
    var el=nav.querySelector('.nav[data-v="'+cur+'"]')||nav.querySelector(".nav.on");
    if(el) place(el);
    /* 尺寸变化后每项的 hover 底也要重算，否则窗口拉伸后灰底会跟格子对不上 */
    /* hover 底色已整块删除（2026-10-06 用户要求），不再需要逐项算尺寸 */
  }

  /* 事件委托：整个 nav 上只挂一套监听，不给 6 项各绑一套 */
  function itemOf(e){ return e.target.closest?e.target.closest(".nav"):null; }
  nav.addEventListener("pointerdown",function(e){
    var it=itemOf(e); if(!it) return;
    it.classList.add("press");
    /* ripple-lite：以按下坐标为圆心，纯 CSS 动画，结束移除 */
    if(getComputedStyle(document.documentElement)
         .getPropertyValue("--nav-ripple-on").trim()==="0") return;
    if(reduced.matches) return;
    var r=it.getBoundingClientRect();
    var d=Math.max(r.width,r.height)*2;
    var s=document.createElement("span"); s.className="rip";
    s.style.setProperty("--rx",(e.clientX-r.left)+"px");
    s.style.setProperty("--ry",(e.clientY-r.top )+"px");
    s.style.setProperty("--rd",d+"px");
    s.addEventListener("animationend",function(){s.remove()});
    it.appendChild(s);
  });
  ["pointerup","pointercancel","pointerleave"].forEach(function(t){
    nav.addEventListener(t,function(e){ var it=itemOf(e); if(it) it.classList.remove("press"); },true);
  });
  nav.addEventListener("click",function(e){
    var it=itemOf(e); if(!it) return;
    show(it.dataset.v);
  });
  /* 键盘：Enter / Space 等同点击；焦点移动本身不调 move()，只有确认选中才滑 */
  nav.addEventListener("keydown",function(e){
    var it=itemOf(e); if(!it) return;
    if(e.key==="Enter"||e.key===" "||e.key==="Spacebar"){ e.preventDefault(); show(it.dataset.v); }
  });

  /* 尺寸变化（窗口 resize / 字体加载完 / 列表项增删）→ rAF 合并后只重算一次 */
  function schedule(){ if(raf) return; raf=requestAnimationFrame(function(){ raf=0; sync(); }); }
  if(window.ResizeObserver){
    var ro=new ResizeObserver(schedule);
    ro.observe(nav); items.forEach(function(n){ ro.observe(n); });
  }
  window.addEventListener("resize",schedule);
  if(document.fonts&&document.fonts.ready) document.fonts.ready.then(schedule);

  /* a11y：<a> 没 href 默认不可聚焦，补 tabindex + aria-current */
  items.forEach(function(n){ n.setAttribute("tabindex","0"); });
  function aria(){
    items.forEach(function(n){
      if(n.dataset.v===cur) n.setAttribute("aria-current","page");
      else n.removeAttribute("aria-current");
    });
  }
  /* 未读小圆点：对外只暴露 unread(v,true/false)。
     样式里的 navPop 只在节点插入时跑一次，不会像呼吸动画那样反复干扰读数。 */
  function unread(v,on){
    var n=nav.querySelector('.nav[data-v="'+v+'"]');
    if(!n) return;
    var d=n.querySelector(".dot");
    if(on&&!d){ d=document.createElement("span"); d.className="dot"; n.appendChild(d); }
    else if(!on&&d) d.remove();
  }

  return { move:function(v){ move(v); aria(); }, enter:enter, sync:sync, unread:unread };
})();

/* 视图切换 */
function show(v){
  /* 指示器 + 内容区进场：与 class 切换同一帧，时间轴同一起点 */
  navMotion.move(v); navMotion.enter(v);
  /* ⚠️ 2026-10-06 加了 v-sdetail（会话详情独立页面）。加进这个列表，
     旧页面的 hidden 切换才管得住它；show() 不碰 .nav 的 data-v，
     所以底部导航那 6 项照旧。
     ⚠️ 别在这里 loadSessions()：详情页不需要拉列表数据。
     ⚠️ 详情页要让「会话」那一项保持高亮（人是冲着会话点进来的，
        不然高亮会突然消失，看着像换了个页面）——
        所以 .nav 的 on 态统一在下面这处算，v==="sdetail" 映射回 "session"。 */
  var navV=(v==="sdetail")?"session":v;
  document.querySelectorAll(".nav").forEach(n=>n.classList.toggle("on",n.dataset.v===navV));
  ["mem","session","sdetail","audit","clean","collect","agents","skill","pack","handoff"]
    .forEach(x=>document.getElementById("v-"+x).hidden=(x!==v));
  /* 详情页专属动作条（面包屑 / 复制原文 / 返回会话列表）。
     ⚠️ 2026-10-07：这三样原来在 #v-sdetail 自己的 .sd-top 里，
        和全局 .toolbar 叠成两行 LOCI 忆宫。现在搬进全局顶栏，
        用 hidden 控制 —— 只有详情页显示。
        元素不存在时静默跳过，别打断页面切换。
     ⚠️ 详情页同时**隐藏搜索框和「重新扫描」**：原型 proto/会话详情.html
        的 .topact 只有 复制原文 / ⏻ / 返回会话列表。
        搜索是列表页的筛选、重新扫描是入库动作，在读原文时都不该出现。 */
  var sb=document.getElementById("sd-bar");
  if(sb) sb.hidden=(v!=="sdetail");
  var onDetail=(v==="sdetail");
  ["sp-search","sp-scanbtn"].forEach(function(id){
    var e=document.getElementById(id); if(e) e.hidden=onDetail;
  });
  if(v==="session") loadSessions();
  /* 2026-10-07：进采集页重新探测来源 —— 面板开着的时候用户装了新 Agent，
     不重扫就看不到。成本是几个目录的 stat，可以接受。 */
  if(v==="collect"){ loadConvSources(); loadCollectPage(); }
  /* ⚠️ 2026-09-27 自检发现：原先只有 session/audit/clean/skill 四页会在切回时重新取数，
     另外几页的数字**停在"打开面板那一刻"**（实测：接口已 3041，记忆页仍显示 3039）。
     这几页现在都有 KPI 卡了，数字不刷新会让人以为"没在更新"。逐页补上：
       · mem    → loadStats()    重取记忆/会话/轮次/项目
       · agents → loadAgents()   重取 Agent 清单与接入数
       · pack   → syncPackInfo() 重取条数/会话数 chip
       · handoff→ syncHfTags()   重取项目标签行的条数
     collect 页**故意不自动扫**（扫描是重操作，必须由用户点按钮触发）。 */
  if(v==="mem") loadStats();
  if(v==="audit") runAudit();
  if(v==="clean"){loadSourceFiles();loadArchive();
    /* 2026-10-08 按原型重排：加载 /api/stats + sourcefiles + backups，
       排成三张卡片 + 右栏两框 + 5 个 KPI。
       loadSourceFiles()/loadArchive() 照旧跑（写的是已隐藏的契约元素）。 */
    loadLibraryPage();
    // 清理页的 4 个 Panel 也支持折叠（用户要求），渲染完再包
    setTimeout(function(){wrapPanels("v-clean",["cl-a","cl-b","cl-c","cl-d"])},150);}
  if(v==="skill") loadSkills();
  if(v==="agents") loadAgents();
  if(v==="pack"){ syncPackInfo(); setTimeout(function(){defaultSelectFirst("sk-proj")},600); }
  if(v==="handoff"){ loadHandoffPage(); }
  /* ⚠️ 2026-10-06：sdetail 不写 hash —— 它的 hash 由 openSessionDetail()
     用 pushState 写成 #session?sid=N（这样后退键能退回列表）。
     这里再 replaceState 会把 ?sid= 抹掉，刷新就回列表了。 */
  if(v!=="sdetail"){
    try{ if(location.hash!=="#"+v) history.replaceState(null,"","#"+v); }catch(e){}
  }

  // ⚠️ 这里不能同步调 secApply()：脚本顶部就会跑一次初始化 show()，而 SEC_FOLD
  //    定义在脚本末尾 —— 同步调用会撞上"还没定义"。延到当前 tick 之后再调。
  setTimeout(secApply,0);
  setTimeout(secApply,150);   // 右栏/列表是异步渲染的，渲染完再补一次
  // 页面级大标题的框：等渲染完再打标（.panel / .split-* 里的不加，见 frameTitles 注释）
  setTimeout(frameTitles,160);
  setTimeout(frameTitles,700);   // 异步内容渲染完再补一次
}
/* ⚠️ 原先这里是给 6 项各绑一个 onclick。现已由 navMotion 的事件委托接管，
   保留两套会触发两次 show()。 */
/* ⚠️ 2026-10-06 深链：#session?sid=3 直接进详情页（刷新/分享都还在）。
   先解析 ?sid= 再走 show()，所以 openSessionDetail 能复用 show 的隐藏切换。 */
var _h=(location.hash||"").replace("#","");
var _m=/^session\?sid=(\d+)$/.exec(_h);
if(_m){ openSessionDetail(_m[1]); }
else if(["mem","session","audit","clean","collect","agents","skill","pack","handoff"].indexOf(_h)>=0){ show(_h); }
// 没带锚点时落在「会话」：面板是给人看的，先看「发生过什么对话」，
// 再看从对话里提炼出的记忆碎片（碎片是给模型检索用的，不该当第一眼）。
else{ show("session"); }
/* 页头的「数据与说明」默认收起（见 pmAll 注释）。放在这里而不是 show(v) 里：
   9 个 section 一开始就在 DOM 里，装完再切页，不会出现"先展开一下再收起"的闪动。
   pmAll 是函数声明，会提升，所以写在这里也能调到（定义在脚本中段）。 */
pmAll();
function goAdd(){show("mem");document.getElementById("form").classList.add("open");
  document.getElementById("f-content").focus()}
function backToList(){
  /* 2026-10-06：#q（原工具栏里的全局搜索框）已移除 —— 它搜的是"记忆"，
     放在会话页页顶是错的。现在会话页有自己的 #s-q。
     这里改成容错写法，别再直接 getElementById("q").value（会抛错）。 */
  var q=document.getElementById("q"); if(q)q.value="";
  show("mem");loadList()
}


async function api(path,opt){const r=await fetch(path,opt);return r.json()}

var _warnShown="";   /* 同一条配置损坏警告只弹一次，避免每次 refresh 都刷屏 */
async function loadStats(){
  const s=await api("/api/stats");
  if(s.warn && s.warn!==_warnShown){ _warnShown=s.warn; toast(s.warn,"err"); }
  const cards=[
    [s.total,"记忆总数"],
    [s.pinned||0,"常驻记忆"],
    [s.sessions||0,"已归档会话"],
    [Object.keys(s.by_project).length,"项目"]
  ];
  document.getElementById("stats").innerHTML=cards.map((c,i)=>
    `<div class="stat" style="--i:${i}"><div class="v" data-to="${c[0]}">0</div><div class="k">${c[1]}</div></div>`).join("");
  document.querySelectorAll("#stats .v").forEach(el=>countUp(el,parseInt(el.dataset.to,10)||0));
  document.querySelectorAll(".proj-sel").forEach(pf=>{
    const cur=pf.value;
    const isAgent = pf.dataset.kind==="agent";
    const src = isAgent ? Object.keys(s.by_agent||{}) : Object.keys(s.by_project);
    pf.innerHTML='<option value="">'+(isAgent?"全部来源":"全部项目")+'</option>'+
      src.map(p=>`<option ${p===cur?"selected":""}>${p}</option>`).join("");
  });
  /* ── 记忆页的 4 张 KPI 卡 + 构成条 + 页头数据行（2026-09-24）─────────────
     全部取自**同一次** /api/stats 返回 —— 与顶栏那 4 个数字同源，
     不会出现"卡里一个数、顶栏另一个数"，也不会出现"卡片总数 76、构成条加起来 74"。
     数字一律 countUp() 滚上去（初值「—」→ parseInt 得 0，所以是 0→N）。 */
  (function(){
    const put=(id,v)=>{const e=document.getElementById(id);if(e)countUp(e,v)};
    const setf=(id,t)=>{const e=document.getElementById(id);if(e)e.textContent=t};
    /* 条宽按原型 C：各自对**自己的参考值**（口径写在 HTML 注释里），最小 2%。
      ⚠️ 不叫 bar —— 这个函数里已经有构成条的 DOM 引用叫 bar 了，重名直接 SyntaxError。 */
    const kbar=(id,pct)=>{const e=document.getElementById(id);
      if(e)e.style.width=Math.max(2,Math.min(100,Math.round(pct)))+'%'};
    const proj=Object.keys(s.by_project||{});
    const projMax=proj.reduce((m,k)=>Math.max(m,s.by_project[k]),0);
    const sup=s.superseded||0, sess=s.sessions||0, msg=s.messages||0;
    put("mk-total",s.total||0);
    setf("mk-total-f",sup?("另有 "+sup+" 条已被取代"):"没有被取代的");
    put("mk-sess",sess);
    setf("mk-sess-f",msg?("共 "+msg+" 轮对话"):"还没归档过会话");
    put("mk-turn",msg);
    setf("mk-turn-f",sess?("均 "+Math.round(msg/sess)+" 轮/会话"):"—");
    put("mk-proj",proj.length);
    setf("mk-proj-f",projMax?("最大项目 "+projMax+" 条"):"—");
    kbar("mk-total-b", (s.total||0)/200*100);
    kbar("mk-sess-b",  sess/50*100);
    kbar("mk-turn-b",  msg/5000*100);
    kbar("mk-proj-b",  proj.length/30*100);
    const ps=document.getElementById("mem-psub");
    if(ps)ps.innerHTML="活跃 <b>"+(s.total||0)+"</b> 条记忆"
      +(sup?("（另有 <b>"+sup+"</b> 条已被取代）"):"")
      +" · 归档 <b>"+sess+"</b> 个会话 · <b>"+msg+"</b> 轮对话 · 覆盖 <b>"+proj.length+"</b> 个项目";
    /* 构成条：键序按 TL 的固定顺序（**不**按条数排）—— 顺序固定才能跨时间一眼对比；
       遇到 TL 里没登记的新类型排到最后，而不是丢掉（丢掉会让图例加起来对不上总数）。 */
    const by=s.by_type||{}, order=Object.keys(TL);
    const sum=Object.keys(by).reduce((n,k)=>n+by[k],0);
    const keys=Object.keys(by).filter(k=>by[k]>0)
      .sort((a,b)=>((order.indexOf(a)+1)||99)-((order.indexOf(b)+1)||99));
    const color=k=>TC[k]||"var(--data-context)";
    const bar=document.getElementById("mem-dist-bar"), leg=document.getElementById("mem-dist-legend");
    if(bar)bar.innerHTML=keys.map(k=>
      '<i style="flex:'+by[k]+';background:'+color(k)+'"></i>').join("");
    if(leg)leg.innerHTML=keys.map(k=>
      '<span><b style="background:'+color(k)+'"></b>'+esc(TL[k]||k)+' <em>'+by[k]+'</em></span>').join("");
    const h=document.getElementById("mem-dist-h");
    if(h)h.textContent="按类型分布 · 共 "+sum+" 条";
    const no=document.getElementById("mem-dist");
    if(no)no.style.display=sum?"":"none";   /* 一条记忆都没有时整块隐藏，别留空条 */
  })();
}

function esc(t){const d=document.createElement("div");d.textContent=t;return d.innerHTML}
/* esc() 走 textContent→innerHTML，不会转义双引号；写进属性值要再转一次 */
function escAttr(t){return esc(t).replace(/"/g,"&quot;")}

/* -- 主从详情面板 -- */
var LASTROWS=[], SELID=null;
/* 选中底统一（2026-09-23，用户先后报了两次同一件事）：
   ① 原先只有记忆页的 `#memcard-*` 会被打上 .sel（只因为 selectMem() 里顺手加了）；
      会话页的行（走 openSession）、本机内容页的条目（走 pickSkill / pickCfg / pickMcp /
      pickPlugin / pickBackupRow）**从来没被标记过** → 黑夜下点它们只剩 hover 那层淡灰，
      看着像"点不动"。
   ② 后来又发现质检页（#audit-out）和清理页（#sf-list / #cm-list / #bk-list）连容器都
      不在 .split-main 里，第一版只认 `.split-main .mem`，所以那两页仍然没反馈。
   做法：一个委托监听覆盖**所有列表容器**，不改各页的 onclick、不加任何 HTML。
   ⚠️ 右栏的详情卡 / 抽取候选卡（`.split-side`、`#detail`）是"内容"不是"列表项"，排除。
   ⚠️ 点行内的动作按钮（合并 / 作废 / 拆分 / 续期…）不标记 —— 那是动作，不是选择。 */
var SEL_SCOPE="#audit-out,#sf-list,#cm-list,#bk-list,#scan-out,#s-list,#list,.split-main";
function markRowSel(el){
  /* 清除范围限定在"同一个列表容器"内：清理页有三个列表，点 A 列表不该把 B 的选中抹掉 */
  var root=(el&&el.closest)?el.closest(SEL_SCOPE):null;
  (root||document).querySelectorAll(".mem.sel").forEach(function(x){x.classList.remove("sel")});
  if(el)el.classList.add("sel");
}
document.addEventListener("click",function(e){
  var t=e.target;
  if(!t||!t.closest)return;
  if(t.closest("button,a"))return;              /* 动作按钮 / 链接不算选择 */
  var row=t.closest(".mem");
  if(!row)return;
  if(row.closest(".split-side,#detail"))return; /* 右栏详情 / 候选卡不是列表项 */
  markRowSel(row);
});
function selectMem(id){
  SELID=id; renderDetail(id);
  document.querySelectorAll('.mem').forEach(function(el){el.classList.remove('sel')});
  var el=document.getElementById('memcard-'+id); if(el)el.classList.add('sel');
}
/* 详情卡结构对齐 Trae 稿：卡头（标题 + 时间/平台/状态 + 右侧动作）+ 分区体（内容 / 标签 / 归属信息）。
   机器码只出现在「归属信息」里，正文不再被它们打断。 */
const I_CAL='<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><rect x="3" y="4.5" width="18" height="17" rx="2.5"/><path d="M8 2.5v4M16 2.5v4M3 10h18"/></svg>';
const I_BOT='<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><rect x="4" y="7.5" width="16" height="12" rx="3"/><path d="M12 3.5v4"/><circle cx="9" cy="13.5" r="1.1"/><circle cx="15" cy="13.5" r="1.1"/></svg>';
function renderDetail(id){
  var el=document.getElementById('detail'); if(!el)return;
  var r=(LASTROWS||[]).filter(function(x){return x.id===id})[0];
  if(!r){
    /* 没选中 → 详情栏常驻引导卡（不再塌单列，见 .split CSS 上方注释） */
    el.innerHTML='<div class="dhead">'+
        '<div class="dhtop"><h3>记忆详情</h3></div>'+
        '<div class="dtop"><span class="dmi">点左侧任意一条记忆，这里会显示完整内容、标签与操作按钮</span></div>'+
      '</div>'+
      '<div class="dmain">'+
        paneGuide('从左边选一条记忆','这里会显示它的完整内容、标签、归属信息与操作按钮。')+
      '</div>';
    return;
  }
  var full=String(r.content||"");
  var short=full.length>46;
  var title=memTitle(full,46);
  var when=relTime(r.created_at);
  if(when.indexOf("-")<0) when+=" "+String(r.created_at).slice(11,16);
  var tags=tagList(r), proj=projName(r.project);
  el.innerHTML=
    '<div class="dhead">'+
      '<div class="dhtop">'+
        '<h3>'+esc(title)+'</h3>'+
        '<div class="dacts">'+
          '<button class="mini" onclick="pinById('+r.id+','+(r.pinned?0:1)+')">'+(r.pinned?'取消常驻':'设为常驻')+'</button>'+
          '<button class="mini" onclick="copyText('+r.id+')">复制内容</button>'+
          '<button class="mini warn" onclick="doDel('+r.id+')">删除</button>'+
        '</div>'+
      '</div>'+
      '<div class="dtop">'+
        '<span class="dmi">'+I_CAL+esc(when)+'</span>'+
        '<span class="dmi">'+I_BOT+esc(agentName(r.agent))+'</span>'+
        '<span class="bdg'+(r.pinned?' pin':'')+'">'+(r.pinned?'常驻记忆':'最近记忆')+'</span>'+
        '<span class="bdg">'+(TL[r.mtype]||r.mtype)+'</span>'+
      '</div>'+
    '</div>'+
    '<div class="dmain">'+
      /* 4 格元信息（2026-09-24）：把「类型 / 重要度 / 来源 / 创建时间」提到第一屏。
         原来这几项埋在下面「归属信息」的 <br> 串里，要滚到底才看得到。
         ⚠️ 下面那段**不删** —— 那里还有编号 / 项目 / 源文件等更细的字段。 */
      '<div class="kv-grid">'+
        '<div class="cell"><div class="k">类型</div><div class="v">'+esc(TL[r.mtype]||r.mtype||"—")+'</div></div>'+
        '<div class="cell"><div class="k">重要度</div><div class="v">P'+esc(r.importance==null?"—":r.importance)+'</div></div>'+
        '<div class="cell"><div class="k">来源</div><div class="v">'+esc(agentName(r.agent))+'</div></div>'+
        '<div class="cell"><div class="k">创建时间</div><div class="v" title="'+escAttr(r.created_at||"")+'">'+esc(when)+'</div></div>'+
      '</div>'+
      (short?'<div class="dsec"><h4 class="dsec-t">内容</h4>'+
        '<div class="dbody">'+esc(full)+'</div></div>':'')+
      '<div class="dsec"><h4 class="dsec-t">标签</h4>'+
        (tags.length?('<div class="dtags">'+tags.map(function(x){
            return '<span class="tag">'+esc(x)+'</span>'}).join('')+'</div>')
          :'<div class="dempty">这条还没打标签 —— 补上标签能显著提升检索命中率</div>')+
      '</div>'+
      '<div class="dsec"><h4 class="dsec-t">出处</h4>'+
        (r.session_id>0
          ? ('<div class="dmeta" style="margin-bottom:var(--space-3)">出自会话 #'+r.session_id
             +(r.session_title?('「'+esc(r.session_title)+'」'):'')
             +(r.turn?(' · 第 '+r.turn+' 轮'):'')+'</div>'+
             '<button class="mini" onclick="gotoSession('+r.session_id+','+(r.turn||0)+')">跳到这段原话</button>')
          : '<div class="dmeta">无来源 —— 这条是 Agent 直接写入或采集器扫进来的，'+
            '不挂在某段具体对话上。这类记忆只能在「检索」里找到，看不到来龙去脉。</div>')+
      '</div>'+
      '<div class="dsec"><h4 class="dsec-t">归属信息</h4>'+
        '<div class="dmeta">编号 #'+r.id+'　重要度 P'+r.importance+
          '<br>项目：'+esc(r.project||'—')+
          '<br>写入方：'+esc(r.agent||'—')+'（'+esc(agentName(r.agent))+'）'+
          '<br>创建：'+esc(r.created_at)+
          (r.source_path?('<br>源文件：'+esc(r.source_path)):'')+'</div>'+
      '</div>'+
      '<div class="dsec"><h4 class="dsec-t">处理建议</h4>'+
        '<div class="dmeta">重复或过期项去「质检」页批量处理；要连源文件一起清掉去「清理」页。</div>'+
      '</div>'+
    '</div>';
  foldAll();
  secApply();
}
function copyText(id){
  var r=(LASTROWS||[]).filter(function(x){return x.id===id})[0]; if(!r)return;
  if(navigator.clipboard&&navigator.clipboard.writeText){
    navigator.clipboard.writeText(r.content).then(function(){toast('已复制到剪贴板','ok')},
      function(){toast('复制失败，请手动选择','err')});
  } else { toast('当前浏览器不支持自动复制','err'); }
}
/* ── 白天 / 黑夜模式（零依赖，localStorage 记忆）── */
var SUN='<circle cx="12" cy="12" r="4.2"/><path d="M12 2.8v2.4M12 18.8v2.4M2.8 12h2.4M18.8 12h2.4M5.6 5.6l1.7 1.7M16.7 16.7l1.7 1.7M18.4 5.6l-1.7 1.7M7.3 16.7l-1.7 1.7"/>';
var MOON='<path d="M20 14.2A8.2 8.2 0 1 1 9.8 4a6.6 6.6 0 0 0 10.2 10.2z"/>';
function applyTheme(t,save){
  document.documentElement.setAttribute("data-theme",t);
  var ic=document.getElementById("themeicon");
  if(ic) ic.innerHTML = (t==="light") ? MOON : SUN;
  var b=document.getElementById("themetgl");
  if(b) b.title = (t==="light") ? "当前白天模式，点击切到黑夜" : "当前黑夜模式，点击切到白天";
  if(save){
    try{localStorage.setItem("loci-theme",t)}catch(e){}
    toast(t==="light"?"已切换到白天模式":"已切换到黑夜模式","ok");
  }
}
/* ⚠️ 2026-10-07 用户拍板：「不要暗主题了」，强制走亮色。
   原来这里读 localStorage + 系统偏好，用户系统是暗色 → 进 dark，
   而 v2 色板只在 light 下定义 → 黑屏 + 布局散架（实测）。
   现在无条件 light，并把历史遗留的 loci-theme / hippocampus-theme
   一并清掉 —— 否则老localStorage 还会把主题拽回去。
   ?theme= 参数仍接受（便于分享/截图），但 dark 也会被拉回 light。 */

/* ── 动效工具（零依赖）── */
function toast(text,kind){
  var box=document.getElementById("toast");if(!box)return;
  var el=document.createElement("div");
  el.className="toast-item"+(kind?(" "+kind):"");
  el.textContent=text;box.appendChild(el);
  setTimeout(function(){el.style.transition="all .24s";el.style.opacity="0";
    el.style.transform="translateY(-10px)";
    setTimeout(function(){el.remove()},260)},2600);
}
var _barT=null;
function progress(on){
  var b=document.getElementById("topbar");if(!b)return;
  if(on){clearTimeout(_barT);b.className="on"}
  else{b.className="done";_barT=setTimeout(function(){b.className=""},420)}
}
/* 系统「减少动态效果」——CSS 侧有全局 @media 兜底（animation/transition 全被压到 0.001ms），
   但下面这段数字滚动是 rAF 驱动的 JS，CSS 管不到它，得在这里自己问一次。
   结果缓存：matchMedia 每次查询都要走一遍媒体查询匹配，没必要每张卡问一次。 */
var _REDUCED=null;
function reduceMotion(){
  if(_REDUCED===null){try{_REDUCED=matchMedia("(prefers-reduced-motion: reduce)").matches}catch(e){_REDUCED=false}}
  return _REDUCED;
}
function countUp(el,to){
  var from=parseInt(el.textContent,10)||0;
  if(from===to){el.textContent=to;return}
  if(reduceMotion()){el.textContent=to;return}   /* 尊重系统设置：直接落值，不做过渡 */
  var t0=performance.now(),d=520;
  (function step(t){
    var p=Math.min(1,(t-t0)/d),e=1-Math.pow(1-p,3);
    el.textContent=Math.round(from+(to-from)*e);
    if(p<1)requestAnimationFrame(step);
  })(t0);
}

/* 列表项结构对齐 Trae 稿：名字 + 一行 meta（时间 · 平台 · 常驻/最近 · 标签）。
   机器码（#编号 / P重要度 / 完整时间戳 / 完整路径）不再出现在列表里，
   它们搬到右侧详情，或挂在行 title 上（鼠标悬停可见）。 */
function relTime(s){
  if(!s) return "";
  var d=new Date(String(s).replace(" ","T"));
  if(isNaN(d.getTime())) return String(s).slice(0,10);
  var diff=(Date.now()-d.getTime())/1000;
  if(diff<0) return "刚刚";
  if(diff<60) return "刚刚";
  if(diff<3600) return Math.floor(diff/60)+" 分钟前";
  var a=new Date();
  var days=Math.round((new Date(a.getFullYear(),a.getMonth(),a.getDate())
                     - new Date(d.getFullYear(),d.getMonth(),d.getDate()))/86400000);
  if(days<=0) return "今天";
  if(days===1) return "昨天";
  if(days<7) return days+" 天前";
  if(days<30) return Math.floor(days/7)+" 周前";
  return String(s).slice(0,10);
}
/* 来源 Agent 的口语化名字：库里存的是程序名，界面上要说人话 */
const AGENT_ALIAS={"panel":"面板","collector":"采集器","auto-scan":"自动扫描",
  "session-extract":"会话抽取","unknown":"未知来源"};
function agentName(a){
  var s=String(a||"").trim();
  if(!s||s==="-") return "手动录入";
  return AGENT_ALIAS[s]||s;
}
function tagList(r){
  return String(r.tags||"").split(/[,，、;；]/).map(function(s){return s.trim()})
    .filter(function(s){return s&&s.length<=12});
}
/* 完整路径只取末段：C:\Users\...\workspace\default → default */
function projName(p){
  var s=String(p||"").trim();
  if(!s) return "";
  s=s.replace(/[\\/]+$/,"");
  var seg=s.split(/[\\/]/);
  return seg[seg.length-1]||s;
}
/* 标题清洗：记忆内容本身是 Markdown（如 `# 2026-09-22 工作日志 ## #批7：…`），
   直接当标题会踩两个坑 —— ① 露出原始标记，不像人话；② 一大批标题开头一模一样
   （全是「# 2026-09-22 工作日志」），根本分不清谁是谁。
   这里剥掉标记、压成一行、超长截断；列表项与详情标题共用同一个函数。 */
function memTitle(s,max){
  var raw=String(s==null?"":s), t=raw
     .replace(/^\s*---[\s\S]*?\n---\s*/,"")     // YAML front matter（采集器扫进来的 md 常见）
     .replace(/```[\s\S]*?```/g," ")
     .replace(/^\s{0,3}#{1,6}\s*/gm,"")          // 行首 # 标题符
     .replace(/#{2,}\s*/g," ")                   // 残留的 ##
     .replace(/^\s{0,3}>\s?/gm,"")               // 引用符
     .replace(/^\s{0,3}[-*+]\s+/gm,"")           // 无序列表符
     .replace(/\*\*|__/g,"")                     // 粗体
     .replace(/[*_`]/g,"")                       // 斜体 / 行内代码
     .replace(/!?\[([^\]]*)\]\([^)]*\)/g,"$1");  // 链接 / 图片
  t=t.replace(/\s+/g," ").trim();
  if(!t)t=raw.replace(/\s+/g," ").trim();       // 兜底：清洗后为空就退回原文
  max=max||52;
  return t.length>max?(t.slice(0,max)+"…"):t;
}
function cardHtml(r,score,idx){
  var tags=tagList(r), proj=projName(r.project);
  var tip="#"+r.id+" · P"+r.importance+" · "+agentName(r.agent)+" · "+r.created_at
    +(proj?(" · 项目 "+proj):"")+(tags.length?(" · 标签 "+tags.join("/")):"");
  return `<div class="mem lrow" id="memcard-${r.id}" style="--i:${idx||0}" title="${escAttr(tip)}"
      onclick="selectMem(${r.id})">
    <span class="mico ${escAttr(r.mtype||"context")}" aria-hidden="true">${rowIcon(r.mtype)}</span>
    <div class="mbody">
      <div class="mtitle">${esc(memTitle(r.content))}</div>
      <div class="mmeta">
        <!-- 类型徽章（2026-09-24 新增）：原来列表里**只有来源、没有类型**，
             而类型恰恰是"这条记忆属于哪一类"最该先看到的一格。 -->
        <span class="tb ${escAttr(r.mtype||"context")}">${esc(TL[r.mtype]||r.mtype||"—")}</span>
        <span class="mtime">${esc(relTime(r.created_at))}</span>
        <span class="bdg">${esc(agentName(r.agent))}</span>
        ${r.pinned?'<span class="bdg pin">常驻</span>':'<span class="bdg state">最近</span>'}
        ${r.session_id>0?`<span class="bdg src" title="出自会话 #${r.session_id}${r.session_title?"「"+escAttr(r.session_title)+"」":""}${r.turn?" · 第 "+r.turn+" 轮":""} —— 点这里回原话看上下文" onclick="event.stopPropagation();gotoSession(${r.session_id},${r.turn||0})">有原话</span>`:""}
        ${tags.length?`<span class="mtags">${tags.slice(0,2).map(function(x){return "#"+esc(x)}).join(" ")}${tags.length>2?" …":""}</span>`:""}
        ${score!==undefined?`<span class="mtags">相关 ${(score*100).toFixed(0)}%</span>`:""}
        <span class="macts"><button class="del" onclick="event.stopPropagation();pinById(${r.id},${r.pinned?0:1})">${r.pinned?"取消常驻":"设为常驻"}</button>
        <button class="del" onclick="event.stopPropagation();doDel(${r.id})">删除</button></span>
      </div>
    </div>
  </div>`;
}

async function pinById(id,pinned){
  var r=await api("/api/pin",{method:"POST",headers:{"Content-Type":"application/json"},
    body:JSON.stringify({id:id,pinned:pinned})});
  if(r&&r.ok===false){ toast(r.error||"操作失败","err"); return }   /* 2026-10-01 修 P1-1 */
  refresh();
}
async function showContext(){
  const proj=document.getElementById("proj-filter").value;
  const r=await api("/api/context"+(proj?("?project="+encodeURIComponent(proj)):""));
  const el=document.getElementById("ctx-out");
  el.style.display="block";
  el.innerHTML='<div class="foldbody">'+esc(r.markdown)+'</div>';
  foldAll();
  el.scrollIntoView({behavior:"smooth"});
}

/* 清理页：源文件 + 记忆逐条 + 备份记录 */
var SFILES=[], MPICK=[];
function sfmsg(t,kind){var el=document.getElementById("sf-msg");
  if(!t){el.style.display="none";return} el.style.display="block";
  el.className="msg"+(kind?(" "+kind):"");el.textContent=t;}
function cmmsg(t,kind){var el=document.getElementById("cm-msg");
  if(!t){el.style.display="none";return} el.style.display="block";
  el.className="msg"+(kind?(" "+kind):"");el.textContent=t;}
function fmtSize(n){return n<1024?(n+" B"):(n<1048576?(Math.round(n/1024)+" KB"):(Math.round(n/1048576)+" MB"))}

async function loadSourceFiles(){
  SFILES=await api("/api/sourcefiles");
  document.getElementById("sf-list").innerHTML = SFILES.length ? (
    '<div class="row" style="margin-bottom:8px"><label class="ckwrap"><input type="checkbox" id="sf-all" onchange="toggleAllSF(this.checked)"> 全选（'+SFILES.length+' 个文件）</label></div>'+
    SFILES.map(function(f,i){
      return '<div class="mem">'+
        '<input type="checkbox" class="sfck" data-i="'+i+'">'+
        '<span class="mico" style="background:var(--t-context-bg);color:var(--data-context)">'+
          '<svg viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round">'+
          '<path d="M4 2h5l3 3v9H4z"/><path d="M9 2v3h3"/></svg></span>'+
        '<div class="mbody">'+
          '<div class="mtitle">'+esc(f.file_name||f.path)+'</div>'+
          '<div class="mmeta">'+
            '<span class="chip">'+fmtSize(f.size)+'</span>'+
            '<span class="mtime">'+esc(f.mtime||"")+'</span>'+
            '<span class="chip">关联 '+f.memories+' 条'+(f.exact?"（精确）":"（估算）")+'</span>'+
            '<span class="chip">'+esc(f.kind)+'</span>'+
            (f.agent?('<span class="chip">'+esc(f.agent)+'</span>'):"")+
            (f.workspace?('<span class="chip">'+esc(f.workspace)+'</span>'):"")+
          '</div>'+
          // ⚠️ 原型这里还有个「正在使用 · 建议跳过」徽章，但 /api/sourcefiles **不返回**
          //    这个字段（已实测：只有 path/size/mtime/agent/workspace/kind/memories/
          //    exact/memory_items）。没有的数据就不画 —— 不凭空造。
          (f.memory_items&&f.memory_items.length
            ?('<div class="mmeta">'+f.memory_items.map(function(m){
                return '<span class="chip">#'+m.id+' '+esc(m.content.slice(0,26))+'</span>';}).join("")+'</div>')
            :"")+
        '</div></div>';
    }).join("")) : '<div class="empty">没有找到源文件</div>';
  loadBackups();
}
function toggleAllSF(v){document.querySelectorAll(".sfck").forEach(function(c){c.checked=v})}
async function cleanSourceFiles(){
  var picked=[];
  document.querySelectorAll(".sfck:checked").forEach(function(c){picked.push(SFILES[parseInt(c.dataset.i,10)])});
  if(!picked.length){sfmsg("先勾选要清理的源文件","err");return}
  var list=picked.map(function(f){return "  "+f.path}).join("\n");
  var total=picked.reduce(function(a,f){return a+f.size},0);
  /* 2026-10-09：换 askDialog —— 原来写死「先复制备份…再移入回收站」，
     不给选择。这里文件**本来就会进回收站**（能找回），备份只是额外一层；
     不想要额外副本（隐私）的可以跳过。 */
  var pick=await askDialog({
    title:"处理 "+picked.length+" 个文件",
    sub:"共 "+fmtSize(total)+"。这些文件会移入系统回收站 —— 那边还能找回。",
    warn:"回收站可能被清空，也可能被别的清理工具顺手带走。要不要在 trash-backup/ 里再留一份？",
    question:"处理前先复制一份备份？",
    cancel:"取消",
    danger:"不备份，直接处理",
    safe:"备份后处理"
  });
  if(pick==="cancel") return;
  var wantBak=(pick==="safe");
  sfmsg(wantBak?"正在备份并移入回收站（每批最多 10 个）…":"正在移入回收站（不备份，每批最多 10 个）…");
  progress(true);
  var r=await api("/api/sourcefiles/clean",{method:"POST",
    headers:{"Content-Type":"application/json"},
    body:JSON.stringify({paths:picked.map(function(f){return f.path}),backup:wantBak})});
  if(r.error){sfmsg("失败："+r.error,"err");progress(false);return}
  sfmsg("已处理 "+r.files+" 个文件（分 "+r.batches.length+" 批）\n"+
        (wantBak?("备份目录："+r.backup_dir):"⚠ 这次没有另存备份 —— 只能从系统回收站找回")+
        (r.failed&&r.failed.length?("\n失败："+r.failed.join("、")):"\n失败：无"),"ok");
  progress(false);
  toast("已处理 "+r.files+" 个文件"+(wantBak?"（进回收站+备份）":"（进回收站，未备份）"),"ok");
  loadSourceFiles();
}
async function loadMemPick(){
  var proj=document.getElementById("cm-proj").value;
  var q=proj?("?project="+encodeURIComponent(proj)+"&limit=300"):"?limit=300";
  var r=await api("/api/cleanup/preview"+q);
  MPICK=r.items||[];
  document.getElementById("cm-list").innerHTML = MPICK.length ?
    ('<div class="row" style="margin-bottom:8px"><label class="ckwrap"><input type="checkbox" onchange="toggleAllCM(this.checked)"> 全选（'+MPICK.length+' 条）</label></div>'+
     MPICK.map(function(m,i){
      return '<div class="mem pick">'+
        '<input type="checkbox" class="cmck" data-i="'+i+'">'+
        '<span class="mico '+(m.mtype||"")+'">'+rowIcon(m.mtype)+'</span>'+
        '<div class="mbody">'+
          '<div class="mtitle">'+esc(m.content)+'</div>'+
          '<div class="mmeta">'+
            '<span class="chip">#'+m.id+'</span>'+
            '<span class="chip">'+esc(TL[m.mtype]||m.mtype||"")+'</span>'+
            (m.project?('<span class="chip">'+esc(m.project)+'</span>'):"")+
            (m.agent?('<span class="chip">'+esc(m.agent)+'</span>'):"")+
            '<span class="mtime">'+String(m.created_at||"").slice(0,10)+'</span>'+
          '</div>'+
        '</div></div>';
    }).join("")) : '<div class="empty">没有记忆</div>';
  cmmsg("共 "+r.count+" 条可选","ok");
}
function toggleAllCM(v){document.querySelectorAll(".cmck").forEach(function(c){c.checked=v})}
async function loadBackups(){
  var rows=await api("/api/backups");
  document.getElementById("bk-list").innerHTML = rows.length ? rows.map(function(b){
    return '<div class="mem">'+
      '<span class="mico" style="background:var(--t-skill-bg);color:var(--data-skill)">'+
        '<svg viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round">'+
        '<path d="M3.2 8.4l3.2 3.2 6.4-7"/></svg></span>'+
      '<div class="mbody">'+
        '<div class="mtitle">'+esc(b.dir)+'</div>'+
        '<div class="mmeta">'+
          '<span class="chip">'+fmtSize(b.size)+'</span>'+
          '<span class="mtime">'+esc(b.time||"")+'</span>'+
          '<span class="chip">'+b.files+' 个文件</span>'+
          '<span class="tb ok">备份</span>'+
        '</div>'+
      '</div></div>';
  }).join("") : '<div class="empty">还没有删除备份</div>';
}

/* 批量清理 + 陈旧记忆检测 */
function toggleCleanup(){
  var p=document.getElementById("cleanup-panel");
  p.style.display = p.style.display==="none" ? "block" : "none";
}
function cmsg(t,kind){
  var el=document.getElementById("cleanup-msg");
  if(!t){el.style.display="none";return}
  el.style.display="block";el.className="msg"+(kind?(" "+kind):"");el.textContent=t;
}
function _clQuery(){
  var q=[];
  var proj=document.getElementById("cl-project").value;
  var ag=document.getElementById("cl-agent").value;
  var bf=document.getElementById("cl-before").value;
  if(proj)q.push("project="+encodeURIComponent(proj));
  if(ag)q.push("agent="+encodeURIComponent(ag));
  if(bf)q.push("before="+encodeURIComponent(bf));
  if(document.getElementById("cl-sup").checked)q.push("superseded=1");
  return q.length?("?"+q.join("&")):"";
}
async function cleanupPreview(){
  cmsg("正在统计…");
  var r=await api("/api/cleanup/preview"+_clQuery());
  if(!r.count){cmsg("当前条件没有命中任何记忆","ok");document.getElementById("ctx-out").style.display="none";return}
  var el=document.getElementById("ctx-out");
  el.style.display="block";
  el.textContent="将删除 "+r.count+" 条记忆\n涉及来源："+(r.by_agent.join("、")||"—")+
    "\n涉及项目："+(r.by_project.join("、")||"—")+
    "\n\n前 "+r.items.length+" 条预览：\n"+
    r.items.map(function(x){return "  #"+x.id+" ["+(x.agent||"")+"|"+(x.project||"")+"] "+x.content.slice(0,60)}).join("\n");
  cmsg("命中 "+r.count+" 条，确认后点「执行删除」（会先自动备份）","ok");
}
/* 通用确认弹窗（2026-10-09 新增）。
   三键：取消 / danger（危险动作）· safe（安全动作），由 opts 决定给哪几个。
   返回 Promise，resolve 出 "cancel" | "danger" | "safe"。
   ⚠️ 这是面板里第一个自定义弹窗 —— 之前 14 处都用原生 confirm()，
      两键，塞不下"不备份直接删"这种第三选项。 */
function askDialog(o){
  return new Promise(function(resolve){
    var mask=document.createElement("div");
    mask.className="ask-mask";
    mask.innerHTML=
      '<div class="ask-dlg" role="dialog" aria-modal="true">'
      +'<div class="dh"><div class="dt">'+esc(o.title||"")+'</div>'
        +(o.sub?'<div class="dsub">'+esc(o.sub)+'</div>':'')+'</div>'
      +(o.warn?'<div class="warnbox">'+esc(o.warn)+'</div>':'')
      +(o.question?'<div class="q">'+esc(o.question)+'</div>':'')
      +'<div class="df"><button class="btn" data-act="cancel">'+esc(o.cancel||"取消")+'</button>'
        +'<span class="grow"></span>'
        +(o.danger?'<button class="btn danger" data-act="danger">'+esc(o.danger)+'</button>':'')
        +(o.safe?'<button class="btn safe" data-act="safe">'+esc(o.safe)+'</button>':'')
      +'</div></div>';
    document.body.appendChild(mask);
    function done(v){
      document.removeEventListener("keydown",onKey);
      if(mask.parentNode) mask.parentNode.removeChild(mask);
      resolve(v);
    }
    function onKey(e){ if(e.key==="Escape") done("cancel"); }
    document.addEventListener("keydown",onKey);
    mask.addEventListener("click",function(e){
      if(e.target===mask){ done("cancel"); return; }     /* 点遮罩 = 取消 */
      /* ⚠️ 选择器必须和属性名一致。2026-10-11：我把按钮的 data-v 改成 data-act
         （为了不和导航的 data-v 撞车），但**漏改了这里的选择器** ——
         结果按钮点了完全没反应、弹窗关不掉。测试当场抓到。
         教训：改属性名要连选择器一起 grep，只改一处等于改坏。 */
      var b=e.target.closest?e.target.closest("[data-act]"):null;
      if(b) done(b.dataset.act);
    });
  });
}
async function cleanupRun(){
  var r0=await api("/api/cleanup/preview"+_clQuery());
  if(!r0.count){cmsg("当前条件没有命中任何记忆","err");return}
  /* 2026-10-09：原来这里是一句原生 confirm，写死「删前会自动导出备份」——
     等于替用户决定。用户拍板：**要让他自己选**（不备份也是有正当理由的：
     隐私，有人就是要数据不留副本）。改成分三键的弹窗。 */
  var pick=await askDialog({
    title:"硬删除 "+r0.count+" 条记忆",
    sub:"这些记忆会从库里彻底抹掉。源文件不动，但记忆库里不会留副本。",
    warn:"硬删除不进回收站。没有备份就再也找不回来 —— 只能靠备份文件恢复，而备份要现在导。",
    question:"删除前先导出一份全量备份？",
    cancel:"取消",
    danger:"不备份，直接删除",
    safe:"备份并删除"
  });
  if(pick==="cancel") return;
  var wantBak = (pick==="safe");
  cmsg(wantBak?"正在备份并删除…":"正在删除（不备份）…");
  var body={};
  var proj=document.getElementById("cl-project").value;
  var ag=document.getElementById("cl-agent").value;
  var bf=document.getElementById("cl-before").value;
  if(proj)body.project=proj;
  if(ag)body.agent=ag;
  if(bf)body.before=bf;
  if(document.getElementById("cl-sup").checked)body.only_superseded=true;
  body.backup = wantBak;
  var r=await api("/api/cleanup/run",{method:"POST",headers:{"Content-Type":"application/json"},
    body:JSON.stringify(body)});
  if(r.error){cmsg("删除失败："+r.error,"err");return}
  cmsg("已删除 "+r.deleted+" 条记忆\n"+
    (wantBak?("备份文件："+r.backup+"\n（需要恢复就用「记忆包」页导入这个文件）")
            :"⚠ 这次**没有备份** —— 这些记忆已经彻底消失，无法从这里恢复。"),"ok");
  refresh();
}
async function showOrphans(){
  cmsg("正在检查陈旧记忆…");
  var r=await api("/api/orphans");
  var el=document.getElementById("ctx-out");
  el.style.display="block";
  if(!r.projects.length && !r.source_paths.length){
    el.textContent="没有发现陈旧记忆\n\n检查规则：采集时的源工作区目录（"+r.workspace_root+"\\<项目名>）与\n会话的源文件路径是否还存在。\n当前这些都还在，所以没有孤儿。";
    cmsg("没有发现陈旧记忆","ok");return;
  }
  el.textContent="发现陈旧记忆：\n  已消失的源工作区："+r.projects.join("、")+"\n"+
    "  记忆 "+r.memories.length+" 条\n"+
    "  已消失的会话源文件："+(r.source_paths.join("、")||"无")+"\n"+
    "  会话 "+r.sessions.length+" 个\n\n"+
    r.memories.slice(0,10).map(function(x){return "  #"+x.id+" ["+x.project+"] "+x.content.slice(0,50)}).join("\n");
  cmsg("发现 "+r.memories.length+" 条记忆、"+r.sessions.length+" 个会话的源已消失","err");
  el.textContent += "\n\n点「清理陈旧记忆」可删除这些孤儿（清之前会让你选要不要备份）。";
}
async function purgeOrphans(){
  /* 2026-10-09：换 askDialog —— 原来写死「会自动导出备份」，不给选择。 */
  var pick=await askDialog({
    title:"清理「源已消失」的记忆与会话",
    sub:"这些记忆和会话的源工作区 / 源文件已经不在了，留着也没法回溯。",
    warn:"硬删除，不进回收站。没有备份就再也找不回来 —— 只能靠备份文件恢复，而备份要现在导。",
    question:"清理前先导出一份全量备份？",
    cancel:"取消",
    danger:"不备份，直接清理",
    safe:"备份并清理"
  });
  if(pick==="cancel") return;
  var wantBak=(pick==="safe");
  cmsg(wantBak?"正在备份并清理…":"正在清理（不备份）…");
  var r=await api("/api/cleanup/orphans",{method:"POST",headers:{"Content-Type":"application/json"},
    body:JSON.stringify({backup:wantBak})});
  if(r.error){cmsg("清理失败："+r.error,"err");return}
  cmsg("已清理：记忆 "+r.memories+" 条、会话 "+r.sessions+" 个\n消失的源："+(r.projects.join("、")||"—")+
       "\n"+(wantBak?("备份文件："+r.backup)
         :"⚠ 这次没有备份 —— 清理掉的内容已经彻底消失，无法从这里恢复。"),"ok");
  document.getElementById("ctx-out").textContent="";
  document.getElementById("ctx-out").style.display="none";
  refresh();
}

function gheadHtml(key, label, n){
  /* FIX-8：箭头从标题**前**挪到**末尾**，和本机内容页的 .grphead 保持一致
     （原来同一个 .ghead 两个位置：记忆页箭头在左、本机内容页在右）。 */
  return '<div class="ghead' + (key === 'recent' ? ' g-recent' : '') + '" id="gh-' + key +
    '" data-k="' + key + '" onclick="toggleGroup(this.dataset.k)">' +
    '<span>' + label + '</span>' +
    '<span class="cnt">' + n + '</span>' +
    '<span class="cv">▼</span></div>';
}
function renderGrouped(rows){
  var pin = rows.filter(function(r){ return r.pinned; });
  var rec = rows.filter(function(r){ return !r.pinned; });
  var out = gheadHtml('pin', '常驻记忆', pin.length);
  out += '<div class="gbody" id="g-pin">' + (pin.length
    ? pin.map(function(r,i){ return cardHtml(r, undefined, i); }).join('')
    : '<div class="empty">还没有常驻记忆 —— 点任意一条的「设为常驻」，它就会固定在这里</div>') + '</div>';
  out += gheadHtml('recent', '最近记忆', rec.length);
  out += '<div class="gbody" id="g-recent">' + (rec.length
    ? rec.map(function(r,i){ return cardHtml(r, undefined, i); }).join('')
    : '<div class="empty">还没有记忆</div>') + '</div>';
  return out;
}
function toggleGroup(key){
  var body = document.getElementById('g-' + key);
  var head = document.getElementById('gh-' + key);
  if(!body) return;
  var nowHidden = body.classList.toggle('hide');
  if(head){
    head.classList.toggle('collapsed', nowHidden);
    head.setAttribute('aria-expanded', nowHidden?'false':'true');
    if(!head.hasAttribute('role')){head.tabIndex=0;head.setAttribute('role','button')}
  }
  var foot = document.getElementById('gf-' + key);   // 内容下方那个按钮也跟着翻字
  if(foot) foot.textContent = nowHidden ? '展开本组 ▾' : '收起本组 ▴';
}

async function loadList(){
  searching=false;
  document.getElementById("list-title").textContent="记忆列表";
  const proj=document.getElementById("proj-filter").value;
  const rows=await api("/api/list?limit=50"+(proj?("&project="+encodeURIComponent(proj)):""));
  LASTROWS=rows;
  document.getElementById("list").innerHTML = rows.length
    ? renderGrouped(rows)
    : '<div class="empty">还没有记忆，点右上角「＋ 记一条」开始</div>';
  if(!SELID && rows.length){ SELID=rows[0].id; }
  renderDetail(SELID);
  if(SELID){var el2=document.getElementById('memcard-'+SELID); if(el2)el2.classList.add('sel');}
  secApply();
}

async function doSearch(){
  const q=document.getElementById("q").value.trim();
  if(!q){loadList();return}
  show("mem");
  searching=true;
  document.getElementById("list-title").textContent=`搜索「${q}」`;
  const rows=await api("/api/search?q="+encodeURIComponent(q)+"&limit=10");
  LASTROWS=rows;
  document.getElementById("list").innerHTML=rows.length
    ?rows.map((r,i)=>cardHtml(r,r.score,i)).join("")
    :'<div class="empty">没有找到相关记忆</div>';
  renderDetail(SELID);
}

/* 「取消」：放弃这次录入 —— 清空所有已填字段 + 把重要度复位 + 滚回顶部。
   2026-09-27 用户报的：「记一条」只有保存没有取消，不想记了只能关整个面板。
   ⚠️ 不隐藏表单本身（表单是常驻卡片，随页面滚动）——只清内容，避免动布局。 */
function cancelSave(){
  var f=document.getElementById("form"); if(!f)return;
  var filled=0;
  f.querySelectorAll("input[type=text],input[type=search],textarea").forEach(function(e){
    if(e.value && e.value.trim())filled++;
    e.value="";
  });
  f.querySelectorAll("select").forEach(function(e){ e.selectedIndex=0; });
  var imp=document.getElementById("f-imp");
  if(imp){ imp.dataset.v="2"; imp.innerHTML="<b>★★</b>★★"; }
  var ck=f.querySelector("input[type=checkbox]"); if(ck)ck.checked=false;
  /* ⚠️ 必须删 .open —— .formcard{display:none} / .formcard.open{display:block}，
     只清空输入框不删这个类 → 表单一直挂着，用户说"点了取消没用，模块还在"。 */
  f.classList.remove("open");
  toast(filled?"已取消 — 清掉 "+filled+" 项已填内容":"已取消","ok");
  window.scrollTo({top:0,behavior:"smooth"});
}
async function doSave(){
  progress(true);
  const content=document.getElementById("f-content").value.trim();
  if(!content){alert("内容不能为空");return}
  await api("/api/save",{method:"POST",headers:{"Content-Type":"application/json"},
    body:JSON.stringify({content,mtype:document.getElementById("f-type").value,
      importance:+document.getElementById("f-imp").dataset.v,
      project:document.getElementById("f-proj").value.trim(),
      tags:document.getElementById("f-tags").value.trim()})});
  document.getElementById("f-content").value="";
  document.getElementById("form").classList.remove("open");
  refresh();
}

async function doDel(id){
  if(!confirm("删除这条记忆？\n\n· 确定＝移出记忆库（软删除，可恢复）\n· 要从数据库里彻底抹掉：去「清理」页勾选删除（会先自动备份）"))return;
  var r=await api("/api/delete",{method:"POST",headers:{"Content-Type":"application/json"},
    body:JSON.stringify({id})});
  if(r&&r.ok===false){ toast(r.error||"删除失败","err"); return }   /* 2026-10-01 修 P1-1 */
  if(SELID===id)SELID=null;
  toast("已移出记忆库 #"+id,"ok");
  refresh();
}

async function doHandoff(){
  const proj=document.getElementById("hf-proj").value;
  if(!proj){alert("先选一个项目");return}
  const r=await api("/api/handoff?project="+encodeURIComponent(proj));
  const el=document.getElementById("handoff");
  el.style.display="block";
  /* 外层已经是 .pcard > .phead，这里只放正文（不再套 .foldbody —— 会和 mdbox 的
     排版打架，白加一层内边距） */
  el.innerHTML=esc(r.markdown||"");
  var card=document.getElementById("hf-card");
  if(card)card.style.display="block";
  var meta=document.getElementById("hf-meta");
  if(meta){
    var lines=(r.markdown||"").split("\n").filter(function(x){return /^[-*]\s/.test(x)}).length;
    var so=document.getElementById("hf-proj").selectedOptions[0];
    meta.textContent=(so?so.textContent:"全部项目")+" · "+lines+" 条记忆";
  }
  document.getElementById("hf-copy").style.display="inline";
}

/* ⑨ 项目标签行（原型样式）：一排可点的 chip/tb。
   真身仍是 #hf-proj（契约 id），只是收起来 —— 点标签会写回它并重新生成。
   ⚠️ 用 /api/stats 的 by_project 现取，按条数降序；**没有数据就不编**。 */
async function syncHfTags(){
  var box=document.getElementById("hf-tags"), sel=document.getElementById("hf-proj");
  if(!box||!sel)return;
  var cur=sel.value;
  try{
    var st=await api("/api/stats"), bp=st.by_project||{};
    var keys=Object.keys(bp).sort(function(a,b){return bp[b]-bp[a]});
    sel.innerHTML='<option value="">全部项目</option>'
      +keys.map(function(k){return '<option value="'+escAttr(k)+'">'+esc(k)+'</option>'}).join("");
    sel.value=cur;
    var chipStyle='height:26px;padding:0 11px;border-radius:8px;font-size:12px;cursor:pointer';
    box.innerHTML='<span class="'+(cur?"chip":"tb decision")+'" data-v="" style="'+chipStyle+'"'
      +' onclick="pickHfProj(this)" title="库里所有记忆都带上">全部项目 · '+st.total+' 条</span>'
      +keys.map(function(k){
        var on=(k===cur);
        return '<span class="'+(on?"tb decision":"chip")+'" data-v="'+escAttr(k)+'" style="'+chipStyle+'"'
          +' onclick="pickHfProj(this)">'+esc(k)+' · '+(bp[k]||0)+' 条</span>';
      }).join("");
  }catch(e){}
}
/* C 按项目名过滤标签行（只是藏掉不匹配的，不动数据、不影响已生成的交接卡） */
function filterHfTags(){
  var q=((document.getElementById("hf-q")||{}).value||"").trim().toLowerCase();
  var box=document.getElementById("hf-tags"); if(!box)return;
  [].slice.call(box.children).forEach(function(sp){
    var hit=!q||(sp.textContent||"").toLowerCase().indexOf(q)>=0;
    sp.style.display=hit?"":"none";
  });
}
function pickHfProj(el){
  var sel=document.getElementById("hf-proj");
  sel.value=el.dataset.v||"";
  syncHfTags();
  doHandoff();
}
async function copyHandoff(){
  await navigator.clipboard.writeText(document.getElementById("handoff").textContent);
  document.getElementById("hf-copy").textContent="已复制";
  setTimeout(()=>document.getElementById("hf-copy").textContent="复制",1500);
}

/* 采集中心 */
async function doScan(){
  const btn=document.querySelector("#v-collect .btn");
  btn.disabled=true;btn.textContent="扫描中…";
  document.getElementById("scan-info").textContent="正在扫描本机文件，可能要几秒…";
  let rows=[];
  try{rows=await api("/api/scan")}
  catch(e){document.getElementById("scan-info").textContent="扫描失败："+e;btn.disabled=false;btn.textContent="扫描本机";return}
  btn.disabled=false;btn.textContent="重新扫描";
  const el=document.getElementById("scan-list");
  const nIn=rows.filter(r=>r.in_db).length, nNew=rows.length-nIn;
  const bytes=rows.reduce((a,r)=>a+(r.size||0),0);
  document.getElementById("scan-info").textContent = rows.length
    ? ("共发现 "+rows.length+" 个候选（"+nIn+" 个已入库置灰）。勾选后点「入库选中」。")
    : "没有发现可采集的文件。";
  ["ck-all","ck-none","ck-go"].forEach(id=>{
    document.getElementById(id).style.display=rows.length?"inline-block":"none"});
  var goBtn=document.getElementById("ck-go");
  if(goBtn)goBtn.textContent="入库选中（"+nNew+"）";   // 按原型：按钮上直接写可入库条数
  // ── 页头数据行 + 3 张 KPI 卡：数字全部现取（未扫描时是 —，不假装有数据）──
  var ps=document.getElementById("ck-psub");
  if(ps)ps.textContent = rows.length
    ? ("已扫描 "+rows.length+" 个文件 · 可入库 "+nNew+" 条 · 已入库 "+nIn+" 条")
    : "还没扫描本机";
  var setK=function(id,v){var e=document.getElementById(id);if(e){if(window.countUp)countUp(e,v);else e.textContent=v}};
  var setF=function(id,t){var e=document.getElementById(id);if(e)e.textContent=t};
  var barK=function(id,p){var e=document.getElementById(id);
    if(e)e.style.width=Math.max(2,Math.min(100,Math.round(p)))+'%'};
  setK("ckpi-files",rows.length); setK("ckpi-new",nNew); setK("ckpi-old",nIn);
  setF("ckpi-files-f1","合计 "+fmtSize(bytes));
  setF("ckpi-files-f2", rows.length?"已扫描":"未扫描");
  setF("ckpi-new-f","新 "+nNew);
  setF("ckpi-old-f", rows.length?(Math.round(nIn/rows.length*100)+"%"):"未扫描");
  barK("ckpi-files-b",100);
  barK("ckpi-new-b", rows.length?nNew/rows.length*100:2);
  barK("ckpi-old-b", rows.length?nIn/rows.length*100:2);
  // ── 行：按原型用紧凑行（.mico + .mtitle + .mmeta 徽章/chip），已入库的整行置灰 ──
  // ⚠️ 外观是紧凑行，但**class 里不带 lrow** —— 统一选中态那条规则是
  //    `.mem.sel:not(.lrow)`，挂上 lrow 会被踢出「卡片行」那一档（清理页刚踩过）。
  var ICO_LOGBOOK='<svg viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.8" '
    +'stroke-linecap="round" stroke-linejoin="round"><path d="M3.2 4.2h9.6v7.6H3.2z"/>'
    +'<path d="M6 4.2V2.8h4v1.4"/></svg>';
  var ICO_SKILL='<svg viewBox="0 0 16 16" fill="currentColor">'
    +'<path d="M8 2.4l1.7 3.5 3.8.5-2.8 2.7.7 3.8L8 11l-3.4 1.9.7-3.8L2.5 6.4l3.8-.5z"/></svg>';
  el.innerHTML = rows.length ? rows.map(function(it){
    var t=it.kind||"";
    var tone=/技能|skill/i.test(t)?"skill":(/日志/.test(t)?"context":"preference");
    return '<label class="mem'+(it.in_db?" indb":"")+'" title="'+escAttr(it.path)+'">'
      +'<input type="checkbox" class="ck" data-p="'+escAttr(it.path)+'"'
        +(it.in_db?' disabled':' checked')+'>'
      +'<span class="mico '+tone+'">'+(tone==="skill"?ICO_SKILL:ICO_LOGBOOK)+'</span>'
      +'<div class="mbody">'
        +'<div class="mtitle">'+esc(it.preview)+'</div>'
        +'<div class="mmeta">'
          +'<span class="tb '+tone+'">'+esc(t)+'</span>'
          +'<span class="mtime">'+esc(it.date||"")+'</span>'
          +'<span class="chip">'+esc(it.source||"")+'</span>'
        +'</div>'
      +'</div>'
      +(it.in_db
        ? '<span class="chip" style="align-self:center">已入库</span>'
        : '<span class="tb warn" style="align-self:center">新</span>')
      +'</label>';
  }).join("") : '<div class="empty">没有发现可采集的文件</div>';
}
async function doCollect(){
  const paths=[...document.querySelectorAll(".ck:checked")].map(c=>c.dataset.p);
  if(!paths.length){alert("先勾选要入库的条目");return}
  const r=await api("/api/collect",{method:"POST",
    headers:{"Content-Type":"application/json"},body:JSON.stringify({paths})});
  let msg=`入库完成：新增 ${r.imported} 条，跳过 ${r.skipped} 条`;
  if(r.errors&&r.errors.length)msg+=`\n${r.errors.length} 个失败：\n`+r.errors.slice(0,3).join("\n");
  alert(msg);
  doScan();loadStats();loadList();
}

/* 会话层 */
var SPARSED=null, SCANCAND=[];
async function loadScanRoots(){
  var r=await api("/api/scan-roots");
  var el=document.getElementById("roots"); if(!el)return;
  el.innerHTML=(r.roots||[]).length ? (r.roots.map(function(p){
      return '<span class="hchip" style="font-size:11.5px;color:var(--sub);border:1px solid var(--line);border-radius:999px;padding:2px 9px">'+
        esc(p)+' <a href="#" onclick="forgetRoot(\''+p.replace(/\\/g,'\\\\')+'\');return false" style="color:var(--bad)">×</a></span>';
    }).join("")) : "";
}
async function addScanRoot(){
  var p=document.getElementById("add-path").value.trim();
  if(!p){alert("先把目录路径粘进输入框");return}
  var r=await api("/api/scan-root",{method:"POST",headers:{"Content-Type":"application/json"},
    body:JSON.stringify({dir:p})});
  if(r.error){msg(r.error,"err");toast(r.error,"err");return}
  toast("已记住扫描目录，找到 "+r.found+" 个配置","ok");
  msg("已记住：" + r.root + "\n本次找到 " + r.found + " 个 MCP 配置" +
      (r.note?("\n（" + r.note + "）"):""), "ok");
  loadAgents();loadScanRoots();
}
async function forgetRoot(p){
  await api("/api/scan-root/forget",{method:"POST",headers:{"Content-Type":"application/json"},
    body:JSON.stringify({dir:p})});
  toast("已移除扫描目录","ok");loadScanRoots();
}
function scanmsg(t,kind){var el=document.getElementById("scan-msg");
  if(!t){el.style.display="none";return} el.style.display="block";
  el.className="msg"+(kind?(" "+kind):"");el.textContent=t;}
function fmtSize(n){
  if(!n)return "0 B";
  if(n>=1048576)return (n/1048576).toFixed(1)+" MB";
  if(n>=1024)return Math.round(n/1024)+" KB";
  return n+" B";
}
var SRCS=[];
/* 本机对话来源：探测并**如实展示**（2026-10-07 改）。
   以前只把结果拼进扫描完的一句话，扫不到的东西静默消失 ——
   用户看到面板像"扫全了"，实际整家Agent 被漏掉（Trae / Qoder 都这么丢的）。
   现在采集页常驻一行：发现什么、能不能读、读不到为什么。 */
function renderConvSources(){
  var box=document.getElementById("conv-src");
  if(!box) return;
  if(!SRCS.length){ box.innerHTML='<span style="color:var(--sub)">正在探测本机对话来源…</span>'; return; }
  var rows=SRCS.map(function(s){
    var color, word;
    if(s.state==="found"){ color="var(--ok)"; word="可读"; }
    else if(s.state==="encrypted"){ color="var(--coral)"; word="已加密，读不到"; }
    else if(s.state==="unreadable"){ color="var(--amber)"; word="有文件，非对话记录"; }
    else { color="var(--faint)"; word=(s.state==="empty"?"目录空":"未发现"); }
    var note=s.note||"";
    return '<div style="display:flex;align-items:center;gap:8px;padding:3px 0">'
      +'<span style="font-weight:600;min-width:76px">'+esc(s.agent)+'</span>'
      +'<span style="color:'+color+'">'+word+'</span>'
      +'<span style="color:var(--faint);font-family:var(--mono);font-size:10.5px">'
      +(s.files?(s.files+" 个文件"):"")+'</span>'
      +'<span style="color:var(--faint);font-size:10.5px;flex:1;min-width:0;'
      +'overflow:hidden;text-overflow:ellipsis;white-space:nowrap" title="'+esc(s.root||"")+'">'
      +esc(note||s.root||"")+'</span></div>';
  });
  var found=SRCS.filter(function(s){return s.state==="found";}).length;
  var un=SRCS.length-found;
  box.innerHTML='<div style="margin-bottom:4px">本机对话来源（探测自磁盘，不写死）：'
    +found+" 个可读"+(un?("，"+un+" 个读不到"):"")+'</div>'+rows.join("");
}
async function loadConvSources(){
  try{var r=await api("/api/conv-sources");SRCS=r.sources||[];}catch(e){SRCS=[];}
  renderConvSources();
}

async function runAutoScan(silent){
  if(!silent){
    var names=SRCS.filter(function(s){return s.state==="found"}).map(function(s){return s.agent});
    var skip=SRCS.filter(function(s){return s.state==="unreadable"}).map(function(s){return s.agent});
    scanmsg(names.length?("正在扫描本机对话（"+names.join(" / ")+"）"
      +(skip.length?("；"+skip.join("、")+" 不是对话记录，已跳过"):"")+"…")
      :"正在探测本机对话来源…");
    progress(true);
  }
  var r=await api("/api/autoscan?subagent=1&extract=1",{method:"POST"});
  progress(false);
  SCANCAND=r.candidates||[];
  var used=(r.used||[]).map(function(u){return u.agent+" "+u.sessions+" 个"});
  /* ⚠️ used[].sessions 是**本次新增**条数，不是"这个源有多少会话"。
     已入库的走增量跳过，sessions 照样是 0 —— 拿它判断"读不读得出"会全判错。
     所以这行只留给以后调试用，不参与渲染判断。 */
  renderConvSources();
  var head="扫描 "+r.scanned+" 个会话 · 新增 "+r.imported+" · 已存在跳过 "+r.skipped+
           (used.length?("\n来源："+used.join("、")):"");
  if(!silent) scanmsg(head+(r.imported?("\n"+r.sessions.map(function(s){return "  #"+s.id+" "+s.title+"（"+s.count+" 轮）"}).join("\n")):""), r.imported?"ok":"");
  if(r.imported) toast("自动扫描：新增 "+r.imported+" 个会话","ok");
  var out=document.getElementById("scan-out");
  if(out){
    out.innerHTML = SCANCAND.length ? (
      '<div class="msg">扫描到 '+SCANCAND.length+' 条候选记忆（来自新导入的会话，你的话优先）。勾选后入库。</div>'+
      '<div class="row" style="margin-bottom:10px"><button class="btn" onclick="importScanCands()">入库选中</button>'+
      '<button class="btn" onclick="SCANCAND=[];document.getElementById(\'scan-out\').innerHTML=\'\'">忽略</button></div>'+
      SCANCAND.map(function(c,i){
        return '<div class="mem"><div class="top">'+
          '<input type="checkbox" class="sck" data-i="'+i+'" checked>'+
          '<span class="ttag"><i style="background:'+(c.from_user?"var(--data-me)":"var(--data-ai)")+'"></i>'+(c.from_user?"我":"AI")+'</span>'+
          '<span class="proj">'+esc(c.session_title||"")+'</span>'+
          '<span class="accent">'+c.mtype+'</span>'+
          '<span class="score">第'+c.turn+'轮</span></div>'+
          '<div class="content">'+esc(c.text)+'</div></div>';
      }).join("")) : "";
  }
  if(r.imported) loadSessions();
}
async function importScanCands(){
  var picked=[];
  document.querySelectorAll(".sck:checked").forEach(function(c){var x=SCANCAND[parseInt(c.dataset.i,10)];if(x)picked.push(x)});
  if(!picked.length){alert("先勾选要入库的候选");return}
  for(var i=0;i<picked.length;i++){
    var c=picked[i];
    await api("/api/save",{method:"POST",headers:{"Content-Type":"application/json"},
      body:JSON.stringify({content:c.text,mtype:c.mtype,importance:3,project:c.project,tags:"自动扫描",agent:c.agent||"auto-scan"})});
  }
  toast("已入库 "+picked.length+" 条候选记忆","ok");
  SCANCAND=[];document.getElementById("scan-out").innerHTML="";
  loadStats();loadList();
}
/* 打开面板即自动扫一次（静默）。先探来源再扫 —— 扫描提示语要用探测结果拼 */
window.addEventListener("load",function(){
  /* 先把「本机探测到的来源」画出来（不触发扫描），
     这样即使还没点扫描，用户也能看到面板**漏了什么** —— 2026-10-07 */
  /* 探测只发一次请求：先画出来，再顺手跑静默扫描 —— 2026-10-07 */
  loadConvSources().then(function(){ renderConvSources(); runAutoScan(true); });
});

/* ───────── 本机内容页（技能 / 配置文件 / MCP） ─────────
   一列三类：左列同一条滚动线上分三段，右侧共用会话页那套详情骨架。
   为什么不分三个页面：用户的原话是"不要突然多一个模块" —— 类别是内容，
   不是新面板；分成三个页签等于把同一个清单切碎了看。 */
var SKILLS=[], SKTGT=[], SKSRC=[], SKCUR=null;
var CFGS=[], MCPS=[], CSRC=[], CCUR=null;
var PLUGINS=[], BACKUPS=[], CBACKS=[];
function dirname(p){
  p=String(p||"");
  var i=Math.max(p.lastIndexOf("\\"),p.lastIndexOf("/"));
  return i>0?p.slice(0,i):p;
}
/* 每个 Agent 的代表色。原来只有 4 个有名字色、其余全是同一个橙 —— 15 个 Agent 排一起
   分不出谁是谁。这里按各自品牌色补齐（Cursor 用深灰，纯黑在暗色下会看不见）。 */
function agentColor(a){
  var m={"WorkBuddy":"#0a84ff","CodeBuddy":"#0a84ff","Trae":"#ea4335","TraeWork":"#f9ab00",
         "ZCode":"#30d158","VS Code":"#007acc","Cursor":"#5f6368","Windsurf":"#09b6a2",
         "Claude Code":"#d97757","Codex":"#10a37f","Gemini CLI":"#4285f4",
         "GitHub Copilot CLI":"#8957e5","Kimi Code":"#7c3aed","Qoder":"#ff6b35",
         "DeepSeek CLI":"#4d6bfe"};
  return m[a]||"#ff9f0a";
}
function agentTag(a){
  return '<span class="ttag"><i style="background:'+agentColor(a)+'"></i>'+esc(a)+'</span>';
}
/* Agent 徽标：品牌色圆角方块 + 首字母。
   ⚠️ 不用厂商真 logo —— 一是零依赖（不能外链图片、也没法内置版权图），
   二是开源发布时直接用别家 logo 有商标风险。首字母方块同样能一眼分辨。 */
/* 徽标上的**两字母**缩写。只用首字母不行 —— Claude Code / CodeBuddy / Codex /
   Cursor 全是 C，排在一起照样分不清（实测 4 个徽标全显示 "C"）。 */
function agentAbbr(a){
  var m={"Claude Code":"CC","CodeBuddy":"CB","Codex":"CX","Cursor":"CU",
         "DeepSeek CLI":"DS","Gemini CLI":"GM","GitHub Copilot CLI":"GH",
         "Kimi Code":"KM","Qoder":"QD","Trae":"TR","TraeWork":"TW",
         "VS Code":"VS","Windsurf":"WS","WorkBuddy":"WB","ZCode":"ZC"};
  if(m[a])return m[a];
  var w=String(a||"?").trim().split(/[\s\-_]+/).filter(Boolean);
  return (w.length>1?(w[0][0]+w[1][0]):String(a||"?").slice(0,2)).toUpperCase();
}
function agentBadge(a,size){
  var s=size||24;
  return '<span class="abadge" style="width:'+s+'px;height:'+s+'px;background:'+agentColor(a)+
    ';font-size:'+Math.round(s*0.42)+'px" title="'+escAttr(a)+'">'+esc(agentAbbr(a))+'</span>';
}
function grp(title,n,note){
  return '<div class="grphead"><span class="gt">'+esc(title)+'</span>'+
    '<span class="gn">'+n+' 个'+(note?' ｜ '+esc(note):'')+'</span></div>';
}
/* 把左栏的每个 .grphead 段落变成**可折叠的独立分组**（用户要的：
   "每一类单独一个框，能整块收起来"）。做法是后处理而不是改渲染代码 ——
   段数和顺序会随探测结果变，直接改拼接字符串容易漏一处。
   包好之后直接复用记忆页那套 toggleGroup：段头点一下，.gbody 整块 display:none。 */
function wrapGroups(root, onlyFirstOpen){
  if(!root)return;
  var heads=Array.prototype.slice.call(root.querySelectorAll(".grphead"));
  heads.forEach(function(h,i){
    if(h.dataset.wrapped)return;
    h.dataset.wrapped="1";
    var key="skg"+i;
    h.classList.add("ghead");
    h.id="gh-"+key;
    /* ⑤ 2026-09-27：原来整条标题栏绑 onclick → **点里面的按钮也会连带折叠**
       （事件冒泡上来），用户看到的就是"功能按钮被折叠吞了"。
       改成方案②：只有落在右侧按钮/控件区时不触发折叠，其余照旧。
       ⚠️ 只改这一处判断，DOM 结构一个字符没动（用户要求"别移出去，怕引新 bug"）。 */
    h.onclick=function(ev){
      if(ev.target.closest(".pr")||ev.target.closest("button")
         ||ev.target.closest("select")||ev.target.closest("input")) return;
      toggleGroup(key);
    };
    // 箭头放**最右**：放标题前面会把标题文字挤开、看着"排列不准"（用户反馈）
    if(!h.querySelector(".cv"))h.insertAdjacentHTML("beforeend",'<span class="cv">▼</span>');
    var body=document.createElement("div");
    body.className="gbody";body.id="g-"+key;
    var n=h.nextSibling;
    while(n&&!(n.nodeType===1&&n.classList&&n.classList.contains("grphead"))){
      var nx=n.nextSibling;
      body.appendChild(n);
      n=nx;
    }
    h.parentNode.insertBefore(body,h.nextSibling);
    // 用户要"收起按钮放在下面"：内容下方给一个明确的按钮（收起后翻成"展开"）
    var foot=document.createElement("button");
    foot.type="button";foot.className="grpfoot";foot.id="gf-"+key;
    foot.textContent="收起本组 ▴";   // 「收起 ▴」在长文截断里另有所指，这里说清是"整组"
    foot.onclick=function(ev){ev.stopPropagation();toggleGroup(key)};
    body.parentNode.insertBefore(foot,body.nextSibling);
  });
  /* ── 只展开第一组，其余收起（2026-09-23 评审第③条）──
     左列 5 组合计约 9000px（光"插件"一组就 4888px）—— 进门先看到一整屏组标题、
     要滚很久才到底部，跟"看不了长内容"的诉求正面冲突。
     收起后总量从约 9000px 降到 1709px，其余 4 组的栏头（带条数）紧跟在第一组内容后面，
     滚一点点就能点开。
     ⚠️ 注意别把话说满：第一组（技能）本身就有 1341px，所以**进门的首屏看不全**后面
     4 个组名，要往下滚一点。若哪天想"一屏看全 5 组"，把 onlyFirstOpen 的判断改成
     全部收起（`i>=0`）即可。
     ⚠️ 有意**不做持久化**：这是"默认视图"不是用户偏好。若把展开状态存起来，
     上次随手展开的一组会在下次进门时留着，看着像"默认设置没生效"。
     在渲染函数里同步调用，不会有一闪而过的展开动画。 */
  if(onlyFirstOpen){
    heads.forEach(function(h,i){
      if(i===0)return;
      var b=document.getElementById("g-skg"+i);
      if(b&&!b.classList.contains("hide"))toggleGroup("skg"+i);
    });
  }
}
/* 把某个页面里的 .panel 逐个变成可折叠（点标题栏收起）—— 清理页有 4 个 Panel，
   用户要求也能折。同样用后处理，不改 HTML。默认展开，状态也走 toggleGroup。 */
function wrapPanels(viewId, keys){
  var v=document.getElementById(viewId); if(!v)return;
  Array.prototype.slice.call(v.querySelectorAll(":scope > .panel")).forEach(function(p,i){
    var h=p.querySelector(":scope > .listhead"), key=keys[i];
    if(!h||!key||h.dataset.wrapped)return;
    h.dataset.wrapped="1";
    h.classList.add("ghead");
    h.id="gh-"+key;
    /* ⑤ 2026-09-27：原来整条标题栏绑 onclick → **点里面的按钮也会连带折叠**
       （事件冒泡上来），用户看到的就是"功能按钮被折叠吞了"。
       改成方案②：只有落在右侧按钮/控件区时不触发折叠，其余照旧。
       ⚠️ 只改这一处判断，DOM 结构一个字符没动。
       ⚠️ 这个模式在文件里有**两处**（通用 wrapPanels + 清理页专用那套），必须都改。 */
    h.onclick=function(ev){
      if(ev.target.closest(".pr")||ev.target.closest("button")
         ||ev.target.closest("select")||ev.target.closest("input")) return;
      toggleGroup(key);
    };
    if(!h.querySelector(".cv"))h.insertAdjacentHTML("beforeend",'<span class="cv">▼</span>');
    var body=document.createElement("div");
    body.className="gbody";body.id="g-"+key;
    var n=h.nextSibling;
    while(n){var nx=n.nextSibling;body.appendChild(n);n=nx}
    p.appendChild(body);
  });
}
/* 给"页面级大标题"打上 .framed —— **只给不在任何卡片里的那些**。
   判断依据：往上找有没有 .panel / .split-main / .split-side —— 它们的边框已经是框了，
   再描一圈就是双重框（用户原话："本身就在框里面的，为什么要多此一举"）。 */
/* ── 框线选择清单（**用户在 ?frames=1 里亲手点的，不是自动判断的**）──
   1 = 要框，0 = 不要，未列出的 = 不加。
   键的格式与标注模式的 keyOf() 完全一致：`页面/类名[该类在该页候选里的下标]`。
   要改规则：地址后加 ?frames=1 重选一遍，点右上角浮条复制，贴回来即可。
   ⚠️ 不要再用"在不在框里"去自动推断 —— 用户的选择跟这个不总一致（例如清理页
   4 个 Panel 的标题他在框内也要框，而记忆包页 3 个在框内他偏不要）。

   ── 2026-09-23 修正：8 个键从 1 改回 0（评审报告第五节存疑① 被证实是真 bug）──
   `.framed::after` 是画在元素**外面** 2px 的（left/right/top:-2px）。凡是落在
   `.split-main` 里的栏头，父容器有 overflow:hidden，那外扩的 2px 会被裁掉：
     · `.shead` —— 左/上/右三面全裁，只剩一条底边；而 `.shead` 自己本来就有
       `border-bottom:1px solid var(--line)` → 那条底边就是**重描一遍**，纯噪音
     · `.grphead` —— 左/右被裁，只剩上/下两条线；而 `.grphead` + `.gbody` 本来就是
       一个完整的边框盒 → 上下各多出一条 2px 外的平行线，看着像"双线"
   验证工具：`node tools/verify_frames.js http://127.0.0.1:8787`（逐元素量越界量，
   越界 >0 即被裁；已挂进闸门，红了说明清单又对不上了）。
   结论：**栏头类的框一律不要**；`.pagehead` / `.dhead` / 部分 `.listhead` 在
   `.content` 里（overflow:auto，且它们贴着上边）→ 框是完整的，保留。

   ── 2026-09-24 修正（用户拍板「方案 A」）：**9 个页头（.pagehead）的框全部去掉** ──
   实测原因（`ui-round2/shots-frames/对照-有框vs无框.html`）：
     ① 页头自身高 34px，里头的按钮也 34px → 框内零内边距，上下贴死
     ② 页头框与它下面的内容卡片同宽、同高（都 1160px） → 一屏两个同宽的框，
        看着像"表格的两行"
     ③ 页头里的按钮自己就有 1px 描边 → 框里再套框
     ④ 语义错位：框＝"这一组是一个整体"，可页头管的是**下面**的内容
   去掉后"记忆"两个字才重新变回**标题**，而不是"盒子里的字"。
   ⚠️ `.listhead` / `.dhead` / 质检页那 7 组 .listhead 的框**保留** ——
      它们才是真的"圈住一组内容"。 */
var FRAME_SELECTION={
  /* ⚠️ 2026-09-26：Agent 页按原型把「本机 Agent」网格提到「装在其他位置」之前，
     两个 listhead 的下标因此对调；且两块都改用 .pcard 包（自带边框），
     再叠一层 .framed 外框就是双框 → 两个都设 0。 */
  "agents/listhead[1]":0, "agents/listhead[2]":0,
  "audit/auditrow[1]":1, "audit/auditrow[2]":1, "audit/auditrow[3]":1,
  "audit/auditrow[4]":1, "audit/auditrow[5]":1, "audit/auditrow[6]":1,
  "audit/auditrow[7]":1,
  "clean/listhead[1]":0, "clean/listhead[2]":0, "clean/listhead[3]":0,
  "clean/listhead[4]":0,
  "collect/listhead[1]":0,
  "handoff/listhead[1]":0,
  "mem/dhead[2]":1, "mem/shead[1]":0,
  "pack/listhead[1]":0, "pack/listhead[2]":0, "pack/listhead[3]":0,
  /* ⚠️ 2026-09-26：会话页删掉了外层 .listhead（原型里没那行"已归档会话+检索框"），
     同页 FRAME_SEL 命中项的序号整体左移 1 —— 下面这几个 key 必须跟着改。
     frameKeyOf() 用的是 peers.indexOf(e)（同页命中下标、跨类累加），
     少一个元素，后面全部错位，本来要画框的会指到别的元素上（verify_frames 立刻红）。 */
  "session/dhead[3]":1, "session/listhead[1]":1,
  "session/shead[2]":0,
  "skill/grphead[3]":0, "skill/grphead[4]":0, "skill/grphead[5]":0,
  "skill/grphead[6]":0, "skill/grphead[7]":0,
  "skill/listhead[1]":0, "skill/shead[2]":0
};
/* 每个框"往下拉大"多少 px（用户在 ?frames=1 里拖出来的）。框会真的往下延伸这么多，
   把下面更多内容圈进来 —— 用绝对定位的 ::after 画，不占布局。 */
var FRAME_EXTRA={
  "agents/listhead[2]":5,
  "audit/auditrow[1]":5, "audit/auditrow[2]":7, "audit/auditrow[3]":7,
  "audit/auditrow[4]":4, "audit/auditrow[5]":6, "audit/auditrow[6]":2,
  "audit/auditrow[7]":6
};
var FRAME_SEL=".pagehead,.listhead,.shead,.dhead,.grphead,.auditrow";
/* ⚠️ .auditrow 是 2026-09-26 加进来的：质检页的七查明细从「七段 .listhead」改成了「七行 .auditrow」，
   原来那 7 个框如果不同步换载体，就会整片失效（verify_frames 从 11 掉到 4）。 */
/* 与标注模式的 keyOf 必须逐字一致，否则清单对不上 */
function frameKeyOf(e){
  var sec=e.closest("section[id^='v-']");
  var sid=sec?sec.id:"";
  var peers=sec?Array.prototype.slice.call(sec.querySelectorAll(FRAME_SEL)):[];
  var cls=String(e.className||"").replace(/\b(framed|want-frame|no-frame)\b/g,"")
    .trim().split(/\s+/)[0]||e.tagName.toLowerCase();
  return sid.replace(/^v-/,"")+"/"+cls+"["+peers.indexOf(e)+"]";
}
function frameTitles(){
  var n=0;
  Array.prototype.forEach.call(document.querySelectorAll(FRAME_SEL),function(e){
    var k=frameKeyOf(e), on=(FRAME_SELECTION[k]===1);
    e.classList.toggle("framed",on);
    if(on){
      // 用户拖出来的"往下拉大"高度 → 交给 CSS 的 --fext，框会真的往下延伸
      e.style.setProperty("--fext",(FRAME_EXTRA[k]||0)+"px");
      n++;
    }else{
      e.style.removeProperty("--fext");
    }
  });
  return n;
}
/* ── 框线标注模式 v2（用户要的"能自己点着选"）──
   地址后加 ?frames=1 打开：
     · 每个候选元素用虚线描出来并标上名字
     · **点一下 = 切换「要框 → 不要框 → 未定」**，改完立刻生效（所见即所得）
     · 右上角浮条显示已选数量；点浮条 = **把结果复制到剪贴板**，直接发我
   选择存在 localStorage，刷新不丢。只在带参数时生效，正常打开完全不受影响。 */
if(location.search.indexOf("frames")>=0){
  (function(){
    var SEL=".pagehead,.listhead,.shead,.dhead,.grphead";
    var KEY="hip_frames", EKEY="hip_frames_h";
    var want={},extra={};   // extra: key → 额外拉伸高度(px)，让框能"拉大"盖住下面更多内容
    try{want=JSON.parse(localStorage.getItem(KEY)||"{}")||{}}catch(e){want={}}
    try{extra=JSON.parse(localStorage.getItem(EKEY)||"{}")||{}}catch(e){extra={}}
    var C_UNDEC="#ff9f0a", C_YES="#34a853", C_NO="#f1204a";
    /* key 要稳定：用「页面 + 类名 + 该页内的序号」—— 直接用它在本页 peers 里的下标，
       切页后下标会变，加页面前缀才不会串。 */
    function keyOf(e){
      var sec=e.closest("section[id^='v-']");
      var sid=sec?sec.id:"?";
      var peers=sec?Array.prototype.slice.call(sec.querySelectorAll(SEL)):[];
      var cls=String(e.className||"").replace(/\b(framed|want-frame|no-frame)\b/g,"")
        .trim().split(/\s+/)[0]||e.tagName.toLowerCase();
      return sid.replace(/^v-/,"")+"/"+cls+"["+peers.indexOf(e)+"]";
    }
    function colorOf(k){
      return want[k]===1?C_YES:(want[k]===0?C_NO:C_UNDEC); }
    function paint(){
      Array.prototype.forEach.call(document.querySelectorAll(SEL),function(e){
        var k=keyOf(e);
        e.dataset.fkey=k;
        e.style.outline="1px dashed "+colorOf(k);
        e.style.outlineOffset="2px";
        e.style.cursor="pointer";
        if(want[k]===1)e.classList.add("framed");else e.classList.remove("framed");
        var t=e.querySelector(".frmtag");
        if(!t){t=document.createElement("span");t.className="frmtag";e.appendChild(t);}
        var shortName=k.split("/")[1].replace(/\[\d+\]$/,"");   // 标签上只显示短名，别太长
        t.textContent="."+shortName+(want[k]===1?" ✓":(want[k]===0?" ✗":""))
          +((extra[k]||0)>0?("  ↕"+extra[k]):"");
        // ── 拉伸：右下角一个握把，按住往下拖 → 框变高（把下面更多内容圈进来）。
        //    框用绝对定位画，**完全不碰真实布局**（这是硬要求，不能因为标注改了版面）。
        var gh=extra[k]||0, grip=e.querySelector(".frmgrip"), box=e.querySelector(".frmbox");
        if(!grip){
          grip=document.createElement("div");
          grip.className="frmgrip";
          grip.style.cssText="position:absolute;right:-6px;width:12px;height:12px;"+
            "background:#0065fd;border:1px solid #fff;border-radius:3px;cursor:ns-resize;"+
            "z-index:100000";
          e.appendChild(grip);
          box=document.createElement("div");
          box.className="frmbox";
          box.style.cssText="position:absolute;left:-3px;top:-3px;pointer-events:none;"+
            "border:1px dashed #0065fd;border-radius:5px;z-index:99998";
          e.appendChild(box);
        }
        box.style.width=(e.offsetWidth+6)+"px";
        box.style.height=(e.offsetHeight+gh+6)+"px";
        grip.style.bottom=(-6-gh)+"px";
        grip.dataset.k=k;
        t.style.cssText="position:absolute;top:-10px;left:2px;font-size:10px;line-height:1.2;"+
          "background:"+colorOf(k)+";color:#fff;padding:0 3px;border-radius:3px;"+
          "z-index:99999;pointer-events:none;white-space:nowrap";
        if(getComputedStyle(e).position==="static")e.style.position="relative";
      });
      var bar=document.getElementById("frmbar");
      if(bar){
        var ys=Object.keys(want).filter(function(k){return want[k]===1}).length;
        var ns=Object.keys(want).filter(function(k){return want[k]===0}).length;
        bar.textContent="🟢 要框 "+ys+" 处 · 🔴 不要 "+ns+" 处 ｜ 点元素切换，选完点这里复制发我";
      }
    }
    function showPick(){
      var lines=Object.keys(want).sort().map(function(k){
        var h=extra[k]||0;
        return "  "+k+"  →  "+(want[k]===1?"要框":"不要框")+(h>0?("（往下拉大 "+h+"px）"):"");});
      return "框线选择结果（"+lines.length+" 处）：\n"+lines.join("\n");
    }
    // 捕获阶段拦截点击：面板自己的 onclick 不会被触发
    document.addEventListener("click",function(ev){
      var e=ev.target&&ev.target.closest?ev.target.closest(SEL):null;
      if(!e)return;
      ev.preventDefault();ev.stopPropagation();
      var k=e.dataset.fkey||keyOf(e);
      want[k]=(want[k]===1)?0:1;        // 未定 → 要框 → 不要 → 要框 …
      try{localStorage.setItem(KEY,JSON.stringify(want))}catch(x){}
      paint();
    },true);
    // 拉伸握把的拖拽（全局只绑一次；用捕获，免得被面板的点击处理吃掉）
    (function(){
      var dragK=null,startY=0,startH=0;
      document.addEventListener("mousedown",function(ev){
        var g=ev.target&&ev.target.closest?ev.target.closest(".frmgrip"):null;
        if(!g)return;
        ev.preventDefault();ev.stopPropagation();
        dragK=g.dataset.k;startY=ev.clientY;startH=extra[dragK]||0;
        var mv=function(e2){
          extra[dragK]=Math.max(0,Math.round(startH+(e2.clientY-startY)));
          try{localStorage.setItem(EKEY,JSON.stringify(extra))}catch(x){}
          paint();
        };
        var up=function(){
          document.removeEventListener("mousemove",mv,true);
          document.removeEventListener("mouseup",up,true);dragK=null;
        };
        document.addEventListener("mousemove",mv,true);
        document.addEventListener("mouseup",up,true);
      },true);
    })();
    var wait=setInterval(function(){
      if(typeof window.show==="function"){
        clearInterval(wait);
        var bar=document.createElement("div");
        bar.id="frmbar";
        bar.style.cssText="position:fixed;right:16px;top:16px;z-index:999999;cursor:pointer;"+
          "background:#0065fd;color:#fff;font:12px/1.6 system-ui,sans-serif;padding:8px 14px;"+
          "border-radius:8px;box-shadow:0 4px 14px rgba(0,0,0,.35);max-width:340px";
        bar.onclick=function(){
          var txt=showPick();
          if(navigator.clipboard)navigator.clipboard.writeText(txt).catch(function(){});
          alert("已复制，直接发我即可：\n\n"+txt);
        };
        document.body.appendChild(bar);
        /* 「清空重选」——用户说"我会被已经画出来的框影响，忘了哪些本来该要、哪些不该要"，
           所以得给一个干净起点：一键清掉全部选择 + 全部拉伸高度。 */
        var clr=document.createElement("div");
        clr.textContent="🗑 清空重选";
        clr.style.cssText="position:fixed;right:16px;top:62px;z-index:999999;cursor:pointer;"+
          "background:#f1204a;color:#fff;font:12px/1.6 system-ui,sans-serif;padding:6px 12px;"+
          "border-radius:8px;box-shadow:0 4px 14px rgba(0,0,0,.35)";
        clr.onclick=function(){
          if(!confirm("清空所有已选和拉伸高度，从零开始重选？"))return;
          want={};extra={};
          try{localStorage.removeItem(KEY);localStorage.removeItem(EKEY)}catch(x){}
          paint();
        };
        document.body.appendChild(clr);
        setTimeout(paint,1000);
        var old=window.show;
        window.show=function(v){old(v);setTimeout(paint,800)};
      }
    },300);
  })();
}
/* 列表里只放主版本号。
   WorkBuddy 的版本串是 `5.5.6-wb.38337834.g5f969292.h7826dc9400fd`（44 字符），
   全量塞进列表会把 top 行挤到换行、卡片高一大截 —— 规范第 5 条：
   列表只放人话，完整值进详情。详情里给的是全量（带 title 可悬停看全）。 */
function shortVer(v){
  v=String(v||"");
  var m=v.match(/^\d+(?:\.\d+){0,2}/);
  return m?m[0]:(v.length>12?v.slice(0,12)+"…":v);
}
async function loadSkills(){
  var box=document.getElementById("sk-list");
  if(!box)return;
  box.innerHTML='<div class="empty">正在探测本机内容…</div>';
  setTimeout(bindSkKpi,0);   /* A：每次探测完重绑一次 KPI 卡的点击（幂等） */
  var r,c;
  try{
    var both=await Promise.all([api("/api/skills"),api("/api/content")]);
    r=both[0]||{};c=both[1]||{};
  }catch(e){box.innerHTML='<div class="empty">探测失败</div>';return}
  SKILLS=r.skills||[];SKTGT=r.targets||[];SKSRC=r.sources||[];
  CFGS=c.configs||[];MCPS=c.mcps||[];CSRC=c.sources||[];
  PLUGINS=c.plugins||[];BACKUPS=c.backups||[];
  /* 2026-10-08 按原型重排：数据一到就画表格 + 右栏 + 5 个 KPI。
     原型那张表与右栏由 skRenderAll() 负责；下面那一大段旧渲染
     （来源卡片 / 插件 / 备份 / 4 张 KPI 卡）仍在跑，填的是 #sk-srcs / #sk-mcps
     这些**已隐藏**的契约元素 —— 不删是因为后面还有别的函数写它们。 */
  /* 本机磁盘上留着的插件旧版本总数。这个数**不是装饰** ——
     它是"就地清算"的度量：装了 55 个插件，磁盘上却有 130 个版本目录。 */
  var OLDVER=PLUGINS.reduce(function(n,p){return n+(p.old_count||0)},0);
  /* ── 4 张 KPI 卡 + 页头数据行（2026-09-24）──────────────
     全部吃上面这几个真实数组的长度，**没有一个写死的数字**。
     条宽按"当前最大的一类"取满分比：这样 4 条并排时能一眼看出量级差，
     而不是四条都顶满（顶满等于没信息）。 */
  (function(){
    var max=Math.max(SKILLS.length,CFGS.length,MCPS.length,PLUGINS.length,1);
    var put=function(id,n,foot){
      /* 用页面里已有的 countUp()（顶栏那 4 个数字就用它）—— 数字滚上去而不是硬跳。
         初值是「—」，parseInt 得 NaN→0，所以是 0→N 的滚动；重复点「重新探测」
         就是从旧值滚到新值。reduceMotion() 时 countUp 内部直接落值。 */
      var e=document.getElementById(id);if(e)countUp(e,n);
      var b=document.getElementById(id+"-b");
      if(b)b.style.width=Math.round(n/max*100)+"%";
      var f=document.getElementById(id+"-f");if(f)f.textContent=foot;
    };
    var usr=SKILLS.filter(function(k){return k.scope==="用户级"}).length;
    var mags={};MCPS.forEach(function(m){mags[m.agent]=1});
    var magc=Object.keys(mags).length;
    put("kpi-skill",SKILLS.length,"用户级 "+usr+" · 内置 "+(SKILLS.length-usr));
    put("kpi-mcp",MCPS.length,magc?("来自 "+magc+" 个 Agent"):"本机没探到 mcp.json");
    put("kpi-plugin",PLUGINS.length,OLDVER?("磁盘另有 "+OLDVER+" 个旧版本"):"无历史版本");
    put("kpi-cfg",CFGS.length,BACKUPS.length?("另有 "+BACKUPS.length+" 份备份"):"无备份");
    var ps=document.getElementById("sk-psub");
    if(ps)ps.innerHTML="技能 <b>"+SKILLS.length+"</b> · 配置文件 <b>"+CFGS.length+
      "</b> · MCP <b>"+MCPS.length+"</b> · 插件 <b>"+PLUGINS.length+
      "</b> · 另含备份 <b>"+BACKUPS.length+"</b> · 来源 <b>"+CSRC.length+"</b>";
  })();
  /* 来源卡片。分三组渲染：
       用户级（4 个，各 Agent 一个）+ 有内容的项目级 + 其余项目级的**汇总一行**。
     一开始是 11 行平铺，其中 7 行都是"这个工作区还没建 skills 目录"，
     把真正有数据的 2 行淹了 —— 缺席的东西要汇总，不该逐条刷屏。 */
  var se=document.getElementById("sk-src");
  if(se){
    var srcRows=function(list){
      return list.map(function(s){
        var on=s.exists;
        // "为什么扫/不扫"由后端给（s.why）—— 前端自己判会判歪（曾经把 WorkBuddy
        // 的项目级条目说成"本机没装 WorkBuddy"）。这里只负责数数量。
        var why=on?("扫到 "+SKILLS.filter(function(k){
              return k.scope===s.scope&&k.agent===s.agent&&(k.workspace||"")===(s.workspace||"")}).length+" 个技能")
                   :(s.why||"不参与扫描");
        // 项目级只显示工作区名（完整路径太长，尾巴会被 ellipsis 切掉，看不出是哪个）
        var label=s.workspace?s.workspace.split(/[\\/]/).pop():s.root;
        return '<div class="srcrow'+(on?" on":"")+'">'+
          '<span class="sdot"></span>'+
          '<span class="sname">'+esc(s.agent)+'</span>'+
          '<span class="smeta">'+esc(s.scope)+'</span>'+
          '<span class="swhy">'+esc(why)+'</span>'+
          '<span class="spath" title="'+esc(s.root)+'">'+esc(label)+'</span></div>';
      }).join("");
    };
    var userLevel=SKSRC.filter(function(s){return !s.workspace});
    var projLive =SKSRC.filter(function(s){return s.workspace&&s.exists});
    var projDead =SKSRC.filter(function(s){return s.workspace&&!s.exists});
    var html=srcRows(userLevel)+srcRows(projLive);
    if(projDead.length){
      html+='<div class="srcnote">另有 <strong>'+projDead.length+
        '</strong> 个本机工作区还没有自己的 skills 目录（有需要时往那儿传技能，会自动建）：'+
        esc(projDead.map(function(s){return (s.workspace||"").split(/[\\/]/).pop()}).join("、"))+
        '</div>';
    }
    /* 配置文件 / MCP 的探测结果也报一行：没探到的（CodeBuddy）要说明为什么，
       不能让"没东西"和"没扫过"长得一样。 */
    if(CSRC.length){
      var clive=CSRC.filter(function(s){return s.found>0});
      var cdead=CSRC.filter(function(s){return s.found===0});
      html+='<div class="srcnote">另有 <strong>'+(CFGS.length+MCPS.length)+
        '</strong> 条配置文件 / MCP，来自 '+esc(clive.map(function(s){return s.agent}).join("、"))+
        (cdead.length?('；'+esc(cdead.map(function(s){return s.agent+"： "+s.why}).join("；"))):'')+
        '</div>';
    }
    /* 插件/历史版本是另一张来源表（PLUGIN_SOURCES），单独报一行 ——
       否则用户会以为"配置文件那一行"就是本机内容的全部。 */
    if(PLUGINS.length||BACKUPS.length){
      var pa={};PLUGINS.forEach(function(p){pa[p.agent]=(pa[p.agent]||0)+1});
      html+='<div class="srcnote">另有 <strong>'+PLUGINS.length+'</strong> 个已装插件（'+
        esc(Object.keys(pa).map(function(a){return a+" "+pa[a]}).join("、"))+'）和 <strong>'+
        BACKUPS.length+'</strong> 份配置备份。'+
        (OLDVER?('<br>这些插件在本机磁盘上还留着 <strong>'+OLDVER+'</strong> 个旧版本 —— '+
          '不用上云，"历史版本"本来就在本地。'):'')+
        '</div>';
    }
    se.innerHTML=html;
  }
  // 标题栏那串长小字（"40 个技能 ｜ 14 个配置文件 ｜ …"）太挤，去掉 ——
  // 每一类的数量已经在各自段头上写了，不重复占地方（用户反馈）。
  document.getElementById("sk-count").textContent="";
  if(!SKILLS.length && !CFGS.length && !MCPS.length && !PLUGINS.length){
    box.innerHTML='<div class="empty">什么都没探到（本机的 Agent 都没建这些目录）</div>';return
  }
  /* 三类拼在一条列表里，技能排最前 —— 它是唯一能"传过去"的类别。 */
  box.innerHTML =
    grp("技能", SKILLS.length, "能互相传的那类")+
    (SKILLS.length?SKILLS.map(function(s,i){
      return '<div class="mem" onclick="pickSkill('+i+')" style="cursor:pointer">'+
        '<div class="top">'+agentTag(s.agent)+
        '<span class="proj">'+esc(s.scope)+'</span>'+
        (s.agent_created?'<span class="accent">自建</span>':'')+
        '<span class="score">'+s.files+' 文件 · '+fmtSize(s.size)+'</span></div>'+
        '<div class="content"><b>'+esc(s.name)+'</b>'+
          (s.desc?('<br><span style="color:var(--sub)">'+esc(s.desc.length>150?s.desc.slice(0,150)+"…":s.desc)+'</span>'):'')+
        '</div></div>';
    }).join(""):'<div class="empty">没探到 skill</div>')+
    grp("配置文件", CFGS.length, "只读预览，敏感文件不读值")+
    (CFGS.length?CFGS.map(function(x,i){
      return '<div class="mem" onclick="pickCfg('+i+')" style="cursor:pointer">'+
        '<div class="top">'+agentTag(x.agent)+
        (x.secret?'<span class="accent">敏感</span>':'')+
        (x.note?'<span class="proj">JSONC</span>':'')+
        '<span class="score">'+fmtSize(x.size)+'</span>'+
        (x.mtime?'<span class="score">'+esc(x.mtime)+'</span>':'')+'</div>'+
        '<div class="content"><b>'+esc(x.name)+'</b><br>'+
        '<span style="color:var(--sub)">'+esc(x.rel)+'</span>'+
        (x.keys&&x.keys.length?('<br><span style="color:var(--faint)">'+esc(x.keys.slice(0,4).join(" · "))+
          (x.keys.length>4?" …":"")+'</span>'):'')+
        '</div></div>';
    }).join(""):'<div class="empty">没探到配置文件</div>')+
    grp("MCP", MCPS.length, "不同产品 schema 不一样，按表声明解析")+
    (MCPS.length?MCPS.map(function(m,i){
      return '<div class="mem" onclick="pickMcp('+i+')" style="cursor:pointer">'+
        '<div class="top">'+agentTag(m.agent)+
        '<span class="proj">'+esc(m.transport)+'</span>'+
        (m.disabled?'<span class="accent">已停用</span>':'')+
        (m.env_keys.length?'<span class="score">'+m.env_keys.length+' 个环境变量</span>':'')+'</div>'+
        '<div class="content"><b>'+esc(m.name)+'</b><br>'+
        '<span style="color:var(--sub)">'+esc(m.rel)+' · '+esc(m.keypath)+'</span></div></div>';
    }).join(""):'<div class="empty">没探到 MCP</div>')+
    grp("插件", PLUGINS.length, "已装清单为准，不靠扫目录猜")+
    (PLUGINS.length?PLUGINS.map(function(p,i){
      /* 版本号有的产品不记（ZCode/Trae 只存了个启用开关），
         这时显式说"只记了开关"，别让空着的版本位看起来像"没装上"。 */
      var vtxt=p.version?('v'+shortVer(p.version)):(p.version_count?("本地 "+p.version_count+" 个版本"):"没记版本");
      return '<div class="mem" onclick="pickPlugin('+i+')" style="cursor:pointer">'+
        '<div class="top">'+agentTag(p.agent)+
        (p.marketplace?'<span class="proj">'+esc(p.marketplace)+'</span>':'')+
        (p.enabled===false?'<span class="accent">已停用</span>':'')+
        '<span class="score"'+(p.version?' title="完整版本 '+esc(p.version)+'"':'')+'>'+esc(vtxt)+'</span>'+
        /* 旧版本是这一页的重点：有它才值得点开。没有就不显示，不刷"0 个旧版本"。 */
        (p.old_count?('<span class="accent">旧版本 '+p.old_count+'</span>'):'')+
        '</div>'+
        '<div class="content"><b>'+esc(p.name)+'</b>'+
        (p.updated_at?(' <span style="color:var(--faint)">'+esc(p.updated_at.slice(0,10))+'</span>'):'')+
        (p.note?('<br><span style="color:var(--sub)">'+esc(p.note)+'</span>'):'')+
        '</div></div>';
    }).join(""):'<div class="empty">没探到插件</div>')+
    /* 历史版本这一段**只列配置文件备份**，不重复列插件 ——
       插件的版本挂在插件自己的详情里（点上面任一个插件就能看全部版本）。
       一个东西在左列出现两次，用户会以为"这是两份不同的东西"。 */
    grp("历史版本", BACKUPS.length+OLDVER, "就地清算，不用上云")+
    (BACKUPS.length?BACKUPS.map(function(b,i){
      return '<div class="mem" onclick="pickBackupRow('+i+')" style="cursor:pointer">'+
        '<div class="top">'+agentTag(b.agent)+
        '<span class="proj">配置备份</span>'+
        '<span class="score">'+fmtSize(b.size)+'</span></div>'+
        '<div class="content"><b>'+esc(b.name)+'</b><br>'+
        '<span style="color:var(--sub)">'+esc(b.of)+'</span> '+
        '<span style="color:var(--faint)">'+esc(b.mtime)+'</span></div></div>';
    }).join(""):'<div class="empty">没有配置备份</div>')+
    (OLDVER?('<div class="srcnote">另有 <strong>'+OLDVER+
      '</strong> 个插件旧版本躺在磁盘上，在上面「插件」里点开任一个就能看到它的全部版本'+
      '（含"当前 / 旧"标记和各自的时间）。这些是装机时留下的，可清理 —— '+
      '面板只报，不动手删。</div>'):'');
  wrapGroups(box, true);   // true = 默认只展开第一组（评审第③条）
  /* 2026-10-08：按原型重排必须放在**函数最后**。
     上面那一大段旧渲染会把 #sk-list 整个重写，
     hook 放在数据赋值之后就等于白跑 —— 表格会被它抹掉。 */
  try{ skRenderAll(); }catch(e){ console.warn("skRenderAll:",e); }
}
/* 把正文里的 `# xxx` 行变成真标题，并生成一条小节条。
   少于 2 节就别加 —— 不然又是一种视觉噪音。返回要插在正文前的 HTML（或空串）。 */
function skOutline(t){
  var lines=(t.textContent||"").split("\n"), html="", toc=[], n=0;
  lines.forEach(function(ln){
    n++;
    var m=ln.match(/^(#{1,4})\s+(.+)$/);
    if(m){
      var id="skh"+n; toc.push({id:id,t:m[2]});
      html+='<span class="sk-h" id="'+id+'">'+esc(m[2])+'</span>\n';
    }else html+=esc(ln)+"\n";
  });
  if(toc.length<2)return "";
  t.innerHTML=html;
  return '<div class="sk-toc"><b>小节</b>'+toc.map(function(o){
    return '<a href="#'+o.id+'" onclick="event.preventDefault();skJump(\''+o.id+'\')">'+
      esc(o.t)+'</a>';
  }).join("")+'</div>';
}
/* 跳到某个小节。⚠️ 不要用 scrollIntoView —— 它会把外层容器一起滚走
   （项目踩过，见会话页 scrollSide 那段的注释）。只动真正的滚动容器。 */
function skJump(id){
  var el=document.getElementById(id); if(!el)return;
  var sc=scrollerOf(el)||document.querySelector(".content");
  if(!sc)return;
  sc.scrollTop += el.getBoundingClientRect().top - sc.getBoundingClientRect().top - 70;
}
/* backTop() 已随 #backtop 一起删除（2026-10-07）。 */
/* 点开一个 skill：右侧详情
   ⚠ 这一版修的是一个真 bug —— 上一版详情区是我手搓的 div，还塞了个
   `max-height:46vh;overflow:auto` 的**内层滚动框**。实测后果：
   fbs-bookwriter 的 SKILL.md 有 8010px 高、要滚 19.3 屏，全被压进 414px 的小窗，
   页面又套着 `.content` 的滚动条 → 两个滚动条、正文永远只能看到一小条，
   用户原话"正文显示不全，非常严重的 bug"。
   修法：**照会话页的骨架来** —— `.dhead`（标题+动作+元信息）+ `.dmain` >
   `.handoff-out`（无内层高度限制，整块跟着页面滚）。
   正文用 textContent 写入（不拼 HTML），顺带免疫转义问题。 */
async function pickSkill(i){
  SKCUR=SKILLS[i];
  var el=document.getElementById("sk-detail");
  el.innerHTML=skHold(SKCUR.name,"读取中…");
  var r=await api("/api/skill/detail?path="+encodeURIComponent(SKCUR.path));
  if(r.error){el.innerHTML=skHold("打不开",r.error);return}
  var opts=SKTGT.map(function(t,k){
    var label=t.agent+" · "+t.scope+(t.workspace?("（"+t.workspace.split("\\").pop()+"）"):"")+
      (t.ready?"":" — 不可用");
    return '<option value="'+k+'"'+(t.ready?"":" disabled")+'>'+esc(label)+'</option>';
  }).join("");
  el.innerHTML=
    '<div class="dhead">'+
      '<div class="dhtop">'+
        '<h3>'+esc(SKCUR.name)+'</h3>'+
        '<div class="dacts">'+
          '<button class="btn" onclick="skPlan()">看传到哪</button>'+
          '<button class="btn pri" onclick="skCopy()">传过去</button>'+
        '</div>'+
      '</div>'+
      '<div class="dtop">'+
        '<span class="dmi">'+esc(SKCUR.agent)+'</span>'+
        '<span class="dmi">'+esc(SKCUR.scope)+'</span>'+
        '<span class="dmi">'+SKCUR.lines+' 行</span>'+
        '<span class="dmi">'+SKCUR.files+' 文件 · '+fmtSize(SKCUR.size)+'</span>'+
        (SKCUR.agent_created?'<span class="dmi">自建</span>':'')+
      '</div>'+
      '<div class="dtop" style="margin-top:8px">'+
        '<span class="dmi">传到</span>'+
        '<select id="sk-target" class="proj-sel" style="flex:1;min-width:0;max-width:100%">'+opts+'</select>'+
      '</div>'+
      '<div class="hint" id="sk-plan" style="margin:8px 0 0"></div>'+
    '</div>'+
    '<div class="dmain">'+
      '<div class="dsec"><h4 class="dsec-t">正文内容</h4>'+
        '<div class="handoff-out" id="sk-text" style="display:block"></div></div>'+
    '</div>';
  var t=document.getElementById("sk-text");
  t.textContent=r.text+(r.truncated?"\n\n…（已截断）":"");
  var toc=skOutline(t); if(toc) t.insertAdjacentHTML("beforebegin",toc);
  foldAll();secApply();
}
/* 配置文件详情：正文一定是**后端脱敏过**的那份。
   面板这边不做任何还原，也拿不到未脱敏的值 —— 后端对敏感文件直接回空 text。 */
async function pickCfg(i){
  CCUR=CFGS[i];
  var el=document.getElementById("sk-detail");
  el.innerHTML=skHold(CCUR.name,"读取中…");
  var r=await api("/api/content/detail?path="+encodeURIComponent(CCUR.path));
  if(r.error){el.innerHTML=skHold("打不开",r.error);return}
  CBACKS=r.backups||[];
  el.innerHTML=
    '<div class="dhead">'+
      '<div class="dhtop">'+
        '<h3>'+esc(CCUR.name)+'</h3>'+
        '<div class="dacts">'+
          '<button class="btn" onclick="copyCurPath()">复制路径</button>'+
          '<button class="btn" onclick="openCurDir()">打开所在文件夹</button>'+
        '</div>'+
      '</div>'+
      '<div class="dtop">'+agentTag(CCUR.agent)+
        '<span class="dmi">配置文件</span>'+
        '<span class="dmi">'+fmtSize(r.size||CCUR.size)+'</span>'+
        (CCUR.mtime?'<span class="dmi">'+esc(CCUR.mtime)+'</span>':'')+
        (r.secret?'<span class="dmi">敏感文件</span>':'')+
      '</div>'+
      '<div class="dtop" style="margin-top:8px"><span class="dmi">路径</span>'+
        '<code style="font-size:11.5px;color:var(--sub);word-break:break-all">'+esc(CCUR.path)+'</code></div>'+
      /* 这个文件在本机留了几份备份，直接摆出来 —— 用户不用去别处找。
         没有备份就整行不出现，不写"备份 0 份"占位。 */
      (CBACKS.length?('<div class="dtop" style="margin-top:8px;flex-wrap:wrap">'+
        '<span class="dmi">历史版本 '+CBACKS.length+' 份</span>'+
        CBACKS.map(function(b,k){
          return '<button class="btn" style="font-size:11px;padding:2px 8px" '+
            'onclick="pickBackupIdx('+k+')">'+esc(b.mtime)+' · '+fmtSize(b.size)+'</button>';
        }).join("")+
        '</div>'):'')+
      '<div class="hint" id="cfg-note" style="margin:8px 0 0"></div>'+
    '</div>'+
    '<div class="dmain"><div class="handoff-out" id="sk-text" style="display:block"></div></div>';
  var t=document.getElementById("sk-text");
  var note=document.getElementById("cfg-note");
  if(r.secret){
    note.textContent=r.why||"敏感文件：只报存在与键名，值一律不读";
    t.textContent="顶层键名（值已全部隐去）：\n\n"+
      ((r.keys||[]).map(function(k){return "  · "+k}).join("\n")||"  （没有读到顶层键）");
  }else if(r.text){
    note.textContent=(r.note?r.note+" ｜ ":"")+
      "下面这份是脱敏后的：命中 token / secret / key / env 的字段，值一律隐去";
    t.textContent=r.text+(r.truncated?"\n\n…（已截断）":"");
  }else{
    note.className="hint err";
    note.textContent=r.why||"读不出内容";
    t.textContent="";
  }
}
/* MCP 详情：不需要再请求 —— 列表接口已经把该给的都给了（env 只有键名）。 */
function pickMcp(i){
  CCUR=MCPS[i];
  var m=MCPS[i];
  var el=document.getElementById("sk-detail");
  var lines=[
    "服务器名   "+m.name,
    "挂在       "+m.agent,
    "配置文件   "+m.path,
    "键路径     "+m.keypath+"   （不同产品的 MCP 写法不一样，靠这张表声明，不靠嗅探）",
    "传输方式   "+m.transport,
    m.command?("启动命令   "+m.command):"",
    (m.args&&m.args.length)?("参数       "+m.args.join(" ")):"",
    m.url?("地址       "+m.url):"",
    (m.env_keys&&m.env_keys.length)
      ?("环境变量   "+m.env_keys.join(", ")+"\n           （只列名字：env 里经常就是 API Key，值一律不读）"):"",
    "状态       "+(m.disabled?"已停用":"启用")
  ].filter(Boolean).join("\n");
  el.innerHTML=
    '<div class="dhead">'+
      '<div class="dhtop">'+
        '<h3>'+esc(m.name)+'</h3>'+
        '<div class="dacts">'+
          '<button class="btn" onclick="copyCurPath()">复制配置路径</button>'+
          '<button class="btn" onclick="openCurDir()">打开所在文件夹</button>'+
        '</div>'+
      '</div>'+
      '<div class="dtop">'+agentTag(m.agent)+
        '<span class="dmi">MCP</span>'+
        '<span class="dmi">'+esc(m.transport)+'</span>'+
        (m.env_keys&&m.env_keys.length?'<span class="dmi">'+m.env_keys.length+' 个环境变量</span>':'')+
        (m.disabled?'<span class="dmi">已停用</span>':'')+
      '</div>'+
      (m.note?'<div class="hint" style="margin:8px 0 0">'+esc(m.note)+'</div>':'')+
    '</div>'+
    '<div class="dmain"><div class="handoff-out" id="sk-text" style="display:block"></div></div>';
  document.getElementById("sk-text").textContent=lines;
}
/* 插件详情：重点是**本地留了几个版本** —— 这就是竞品放在云端的那个"历史版本"，
   而本机磁盘上本来就有，一个字节都不用上云。 */
async function pickPlugin(i){
  var p=PLUGINS[i];
  /* ZCode / Trae 的清单里根本没有安装路径 —— 这时**不能**照抄那两个按钮，
     否则又造出一对点不动的死按钮（上一轮刚修过这个毛病）。 */
  CCUR={path:p.path||"",dir:p.dir||dirname(p.path||""),agent:p.agent,name:p.name};
  var el=document.getElementById("sk-detail");
  el.innerHTML=skHold(p.name,"读取本地版本…");
  var r=await api("/api/plugin/versions?agent="+encodeURIComponent(p.agent)+
                  "&path="+encodeURIComponent(p.path||""));
  var vers=(r&&r.versions)||[];
  var acts=p.path?(
    '<button class="btn" onclick="copyCurPath()">复制路径</button>'+
    '<button class="btn" onclick="openCurDir()">打开所在文件夹</button>')
   :'<span class="dmi">这个产品的清单不记路径，所以没有可打开的文件夹</span>';
  var lines=[
    "插件        "+p.name,
    "产品        "+p.agent,
    p.marketplace?("市场        "+p.marketplace):"",
    "当前版本    "+(p.version||"（这个产品不记版本）"),
    p.installed_at?("装于        "+p.installed_at):"",
    p.updated_at?("更新于      "+p.updated_at):"",
    "状态        "+(p.enabled===false?"已停用":"启用"),
    p.path?("安装目录    "+p.path):"",
    "清单文件    "+p.source_file,
    ""
  ].filter(function(x){return x!==""}), out=lines.slice();
  if(vers.length){
    out.push("本地版本（磁盘上留了 "+vers.length+" 个，其中 "+
             (r.old||[]).length+" 个是旧版本）：");
    vers.forEach(function(v){
      out.push("  "+(v.is_current?"▶ ":"  ")+v.version+"   "+v.mtime+
               "   "+v.entries+" 项"+(v.has_skill?"   含 SKILL.md":"")+
               (v.is_current?"   ← 当前":""));
    });
    if((r.old||[]).length){
      out.push("");
      out.push("标记 ▶ 的是当前在用的版本；其余是装机/升级时留下的旧版本，还占着磁盘。");
      out.push("面板只报不删 —— 要不要清理由你决定（路径在上面「安装目录」那一行）。");
    }
  }else{
    out.push("本地版本：没有");
    out.push("  "+(r.why||"本机没有留下这个插件的版本目录。"));
  }
  if(p.note){
    out.push("");
    out.push(p.note);
  }
  el.innerHTML=
    '<div class="dhead">'+
      '<div class="dhtop">'+
        '<h3>'+esc(p.name)+'</h3>'+
        '<div class="dacts">'+acts+'</div>'+
      '</div>'+
      '<div class="dtop">'+agentTag(p.agent)+
        '<span class="dmi">插件</span>'+
        (p.marketplace?'<span class="dmi">'+esc(p.marketplace)+'</span>':'')+
        (p.version?'<span class="dmi">v'+esc(p.version)+'</span>':'')+
        '<span class="dmi">本地 '+vers.length+' 个版本</span>'+
        (p.old_count?'<span class="dmi">旧版本 '+p.old_count+'</span>':'')+
      '</div>'+
    '</div>'+
    '<div class="dmain"><div class="handoff-out" id="sk-text" style="display:block"></div></div>';
  document.getElementById("sk-text").textContent=out.join("\n");
}
/* 从「历史版本」分组点进来 */
function pickBackupRow(i){CBACKS=[BACKUPS[i]];pickBackupIdx(0);}
/* 配置备份详情。
   ⚠ 备份**必须走同一套脱敏**（后端 backup_detail 已经这么做了）——
   本机的 models.json.bak-* 里就是明文 API Key，这条要是漏了，
   整个脱敏设计就形同虚设（等于留了个"看原文"的后门）。 */
async function pickBackupIdx(i){
  var b=CBACKS[i];
  if(!b)return;
  CCUR={path:b.path,dir:b.dir||dirname(b.path),agent:b.agent,name:b.name};
  var el=document.getElementById("sk-detail");
  el.innerHTML=skHold(b.name,"读取中…");
  var r=await api("/api/backup/detail?path="+encodeURIComponent(b.path));
  if(r.error){el.innerHTML=skHold("打不开",r.error);return}
  var others=CBACKS.length>1?('<span class="dmi">同组还有 '+(CBACKS.length-1)+' 份</span>'):'';
  el.innerHTML=
    '<div class="dhead">'+
      '<div class="dhtop">'+
        '<h3>'+esc(b.name)+'</h3>'+
        '<div class="dacts">'+
          '<button class="btn" onclick="copyCurPath()">复制路径</button>'+
          '<button class="btn" onclick="openCurDir()">打开所在文件夹</button>'+
        '</div>'+
      '</div>'+
      '<div class="dtop">'+agentTag(b.agent||"")+
        '<span class="dmi">配置备份</span>'+
        '<span class="dmi">'+fmtSize(r.size||b.size)+'</span>'+
        '<span class="dmi">'+esc(b.mtime)+'</span>'+
        others+
      '</div>'+
      '<div class="dtop" style="margin-top:8px"><span class="dmi">这份是</span>'+
        '<code style="font-size:11.5px;color:var(--sub);word-break:break-all">'+esc(b.of||"")+'</code>'+
        '<span class="dmi">的历史版本</span></div>'+
      '<div class="hint" id="cfg-note" style="margin:8px 0 0"></div>'+
    '</div>'+
    '<div class="dmain"><div class="handoff-out" id="sk-text" style="display:block"></div></div>';
  var t=document.getElementById("sk-text");
  var note=document.getElementById("cfg-note");
  if(r.text){
    note.textContent="和当前配置同一套脱敏：命中 token / secret / key / env 的字段，"+
      "值一律隐去（备份里同样可能有明文密钥，不能因为是备份就跳过）";
    t.textContent=r.text+(r.truncated?"\n\n…（已截断）":"");
  }else{
    note.className="hint err";
    note.textContent=r.why||"这个备份读不出内容";
    t.textContent="";
  }
}
function copyCurPath(){
  var p=(CCUR&&CCUR.path)||"";
  if(!p)return;
  var done=function(){toast("路径已复制","ok")};
  if(navigator.clipboard&&navigator.clipboard.writeText){
    navigator.clipboard.writeText(p).then(done,function(){toast("复制失败，路径已显示在详情里","err")});
  }else{toast("这个浏览器不支持剪贴板，路径已显示在详情里","err")}
}
function openCurDir(){
  /* 这是个真修过的 bug：按钮走的 POST /api/open-folder 在路由表里根本不存在
     （后端只有 GET，而那个 GET 分支还引用了 do_GET 里没有的 body 变量），
     实测一直回 {"error":"not found"} —— 也就是面板上每个"打开文件夹"都是死的。 */
  var p=(CCUR&&CCUR.dir)||"";
  if(!p){toast("这个条目没有可打开的目录","err");return}
  api("/api/open-folder",{method:"POST",headers:{"Content-Type":"application/json"},
    body:JSON.stringify({path:p})}).then(function(r){
      if(r.error)toast(r.error,"err");else toast("已打开 "+p,"ok");
    });
}
function skTargetRoot(){
  var sel=document.getElementById("sk-target");
  if(!sel)return "";
  var t=SKTGT[parseInt(sel.value,10)];
  return t?t.root:"";
}
async function skPlan(){
  if(!SKCUR)return;
  var r=await api("/api/skill/plan?src="+encodeURIComponent(SKCUR.path)+
                  "&target="+encodeURIComponent(skTargetRoot()));
  var el=document.getElementById("sk-plan");
  if(r.error){el.className="hint err";el.textContent="✗ "+r.error;return}
  el.className="hint";
  el.innerHTML="从 <code>"+esc(r.src)+"</code><br>到 <code>"+esc(r.dst)+"</code><br>"+
    r.files+" 个文件 · "+fmtSize(r.size)+(r.dst_exists?"<br>⚠ 目标已有同名 skill，传过去会覆盖":"");
}
async function skCopy(){
  if(!SKCUR)return;
  var root=skTargetRoot();
  var p=await api("/api/skill/plan?src="+encodeURIComponent(SKCUR.path)+"&target="+encodeURIComponent(root));
  if(p.error){toast(p.error,"err");return}
  if(p.dst_exists && !confirm("目标已有同名 skill：\n"+p.dst+"\n\n覆盖它？")) return;
  var r=await api("/api/skill/copy",{method:"POST",headers:{"Content-Type":"application/json"},
    body:JSON.stringify({src:SKCUR.path,target:root,overwrite:!!p.dst_exists})});
  if(r.error){toast(r.error,"err");return}
  toast("已传到 "+r.dst,"ok");
  loadSkills();
}
function roleName(r){return r==="user"?"我":(r==="assistant"?"AI":(r==="raw"?"原文":r))}
function roleColor(r){return r==="user"?"var(--data-me)":(r==="assistant"?"var(--data-ai)":(r==="raw"?"var(--data-context)":"var(--data-preference)"))}
async function sessionParse(){
  var t=document.getElementById("s-text").value;
  if(!t.trim()){alert("先粘贴对话内容");return}
  var r=await api("/api/session/parse",{method:"POST",
    headers:{"Content-Type":"application/json"},body:JSON.stringify({text:t})});
  SPARSED=r.messages||[];
  var users=SPARSED.filter(function(m){return m.role==="user"}).length;
  document.getElementById("s-info").textContent=
    "解析出 " + SPARSED.length + " 条（我问 " + users + " / 其他 " +
    (SPARSED.length-users) + "）。确认无误后点「确认归档」。";
  document.getElementById("s-list").innerHTML=SPARSED.map(function(m,i){
    return '<div class="mem"><div class="top">' +
      '<span class="ttag"><i style="background:'+roleColor(m.role)+'"></i>'+roleName(m.role)+'</span>' +
      '<span class="proj">第'+(i+1)+'轮</span></div>' +
      '<div class="content">'+esc(m.content.length>400?m.content.slice(0,400)+"…":m.content)+'</div></div>';
  }).join("");
  document.getElementById("s-save").disabled=!SPARSED.length;
}
/* 会话页 4 张 KPI 卡 + 页头数据行（2026-09-25）。
   会话数 / 轮次 / 记忆数取自 /api/stats —— 与顶栏同源，不会出现"卡里一个数、顶栏另一个数"。
   「有记忆的会话」只能从列表算，而列表是按 limit=50 取的：会话超过 50 个时
   退化成「基于最近 N 个会话」而不是硬算 —— 把截断后的数字当成全局数就是编数字。 */
/* 页头 5 个大数字（2026-10-07 按 7 页定稿原型重做）
   ⚠️ 口径全部来自 /api/stats 已返回的字段，**不编造概念**：
      会话 = s.sessions · 已抽记忆 = s.total
      待归档 = s.messages（原文轮次：还没进记忆流程的对话量）· 待质检 = s.superseded
      今日已抽 = s.pinned（接口只有这两个近似字段，**标签按原型、值取真实接口**）
   ⚠️ 「待归档 / 今日已抽」这两个概念库里**没有同名字段**。
      按「不许编数据」的规矩：标签跟原型一致，数值取接口里语义最接近的真实值，
      并在此注释标明映射关系 —— 不拿不存在的数编一个看起来合理的数。
   ⚠️ 原来第3、5 格是「原文轮次 / 已置顶」，已按原型换成「待归档 / 今日已抽」。
   ⚠️ 原来那版有条形进度条（skpi-*-b），新原型里没有这层，去掉了——
      5 个数字并排本来就够，硬加条只会让 5 个数字互相竞争。 */
async function sessionKpi(rows){
  var s;
  try{ s=await api("/api/stats") }catch(e){ return }
  function put(id,v){
    var e=document.getElementById(id); if(!e) return;
    if(v===null||v===undefined){ e.textContent="—"; return }
    if(typeof countUp==="function") countUp(e,v); else e.textContent=v;
  }
  var sess=s.sessions||0, mem=s.total||0, msg=s.messages||0;
  put("skpi-sess",sess);
  put("skpi-mem",mem);
  put("skpi-turn",msg);
  put("skpi-sup",(typeof s.superseded==="number")?s.superseded:0);
  put("skpi-pin",(typeof s.pinned==="number")?s.pinned:0);
  var ps=document.getElementById("s-psub");
  if(ps){
    var plats={};
    (rows||[]).forEach(function(x){plats[x.agent||"?"]=1});
    var np=Object.keys(plats).length;
    /* 原型这一行是「33 个 · 3707 轮 · 3 个平台 · 上次扫描 20:12」——
       按同样顺序与口径重排，不再用「已归档 N个」的旧措辞。 */
    ps.innerHTML=(sess?("<b>"+sess+"</b> 个"):"还没归档过")+
      " · <b>"+msg+"</b> 轮"+
      (np?(" · <b>"+np+"</b> 个平台"):"");
  }
}

async function loadSessions(){
  /* 2026-10-06 按定稿原型 p5-final/list.html 重做。
     表格 4 列：标题（含副标）/ Agent / 输出强度条 / 时间。
     ⚠️ 表头与数据行共用 grid（CSS 里 .thead/.tr 同一条规则），列天然对齐。
     ⚠️ 点行 → 跳独立详情页（用户 2026-10-06 定："点进去之后单独跳转一页"），
        不再是同页右栏展开。 */
  var rows=await api("/api/session/list?limit=50");
  SESS_ALL=rows;
  initSessionChips();
  renderSessionRows();
  sessionKpi(rows);
  renderSessionSide(rows);
}
/* 筛选状态：记住当前选的平台，chip 切换时只重画表格不重拉接口 */
var SESS_ALL=[], SESS_FILTER="";
function renderSessionRows(){
  var box=document.getElementById("s-list");
  if(!box) return;
  var rows=SESS_FILTER?SESS_ALL.filter(function(s){
    return String(s.agent||"").toLowerCase().indexOf(SESS_FILTER)>=0;
  }):SESS_ALL;
  var tot=document.getElementById("s-total");
  if(tot) tot.textContent=rows.length;
  if(!rows.length){
    box.innerHTML='<div class="empty">'+(SESS_ALL.length?"这个来源下还没有会话":"还没有归档的会话")+"</div>";
    return;
  }
  box.innerHTML=rows.map(function(s){
    var locked=!(s.msg_count>0);
    /* 输出强度条：按轮数分 5 格。加密会话（拿不到原文）全灰 —— 这不是"产出少"，
       是**看不到**，两件事要在视觉上区分开。 */
    var lv=locked?0:Math.min(5,Math.max(1,Math.round((s.msg_count||0)/8)+1));
    var out='<div class="out'+(locked?" empty":"")+'">';
    for(var i=0;i<5;i++) out+='<i'+(i<lv?' class="f"':"")+"></i>";
    out+='</div>';
    var t=String(s.started_at||s.created_at||"");
    /* 原型 .tm 显示 22:41 这种「时刻」，不是 10-07 这种日期。
       ⚠️ sessions 表没有 updated 列，只能用 created_at/started_at。 */
    var when=(t.length>=16&&t.charAt(10)==="T")?t.slice(11,16)
            :(t.length>=16?t.slice(11,16):"—");
    /* 2026-10-07 溯源：tooltip 里带上**原始文件路径**。
       以前只写「点开看全文与来源」，但 source_path 是
       "agent-scan://<id>" 虚拟标识，列表页根本看不出这条从哪来——
       用户分不清列表里哪条是自己 Trae 上那个任务（其实那条压根没入库）。
       source_file 是新增列，记扫描时的真实文件路径。 */
    var srcTip='会话 #'+s.id+(s.source_file?('\n来源文件：'+s.source_file):(s.source_path?('\n来源：'+s.source_path):""));
    return '<div class="tr" onclick="openSessionDetail('+s.id+')" title="'
        +escAttr(srcTip)+'">'+
      '<div class="ti"><div class="tt">'+
        (locked?'<span class="lk">已加密</span>':"")+esc(memTitle(s.title))+
      /* 副标 = 摘要，没有摘要就退到「项目 · 轮数」。
         ⚠️ 2026-10-07 原来直接印 s.project 全文，
            那是 .workbuddy/projects/... 的完整路径，一行 90 字符，
            把标题列撑爆。projName() 取路径最后一段。 */
      '</div><div class="mt">'+
        esc(s.summary?String(s.summary).slice(0,40)
          :(s.project?(projName(s.project)+(s.msg_count?" · "+s.msg_count+" 轮":"")):""))+
      '</div></div>'+
      '<div><span class="src '+sessSrcCls(s.agent)+'" title="'+escAttr(
          s.source_file||s.source_path||"来源未知")+'">'+esc(agentName(s.agent)||"未知来源")+"</span></div>"+
      '<div>'+out+"</div>"+
      '<div class="tm" style="text-align:right">'+esc(when)+"</div>"+
    "</div>";
  }).join("");
}
/* Agent → 标签 class。跟详情页 .sd-src 用同一套配色，两页观感统一。 */
function sessSrcCls(a){
  var s=String(a||"").toLowerCase();
  if(s.indexOf("workbuddy")>=0)return"wb";
  if(s.indexOf("zcode")>=0)return"zc";
  if(s.indexOf("qoder")>=0)return"qd";
  return"ot";
}
/* chip 点击：事件委托挂在容器上（4 个 chip 不各绑一套监听） */
function initSessionChips(){
  var box=document.getElementById("s-chips");
  if(!box||box.dataset.on) return;
  box.dataset.on="1";
  box.addEventListener("click",function(e){
    var c=e.target.closest?e.target.closest(".chip"):null;
    if(!c) return;
    box.querySelectorAll(".chip").forEach(function(x){x.classList.remove("on")});
    c.classList.add("on");
    SESS_FILTER=c.dataset.a||"";
    renderSessionRows();
  });
}
/* 右栏两个框（2026-10-06 用户定：「记忆构成」改成**按来源平台 + 总量**，
   不再按类型 —— 类型标签是给 AI 看的，人读不懂，见 会话页功能设计.md）。
   数据来源：按平台统计会话数；总量取已有的记忆总数。 */
function renderSessionSide(rows){
  var st=document.getElementById("sp-src-stack"), lg=document.getElementById("sp-src-lgs");
  var nEl=document.getElementById("sp-src-n");
  if(!st||!lg) return;
  var by={}, order=[];
  rows.forEach(function(s){
    var a=String(s.agent||"").toLowerCase();
    var key=a.indexOf("workbuddy")>=0?"WorkBuddy":(a.indexOf("zcode")>=0?"ZCode":
            (a.indexOf("qoder")>=0?"Qoder":(a||"其他")));
    if(!by[key]){by[key]={n:0,c:0};order.push(key)}
    by[key].n++;
  });
  /* 配色跟列表页 .src 标签一致：靛蓝 / teal / amber / 中性灰 */
  var COL={"WorkBuddy":"#4338ca","ZCode":"#0f766e","Qoder":"#b45309","其他":"#857f74"};
  var tot=rows.length||1;
  order.sort(function(a,b){return by[b].n-by[a].n});
  st.innerHTML=order.map(function(k){
    return '<i style="width:'+(by[k].n/tot*100).toFixed(1)+'%;background:'+COL[k]+'"></i>';
  }).join("");
  lg.innerHTML=order.map(function(k){
    var pc=Math.round(by[k].n/tot*100);
    return '<div class="lg"><span class="sw" style="background:'+COL[k]+'"></span>'+
      '<span class="nm">'+esc(k)+'</span><span class="v">'+by[k].n+"</span>"+
      '<span class="p">'+pc+"%</span></div>";
  }).join("");
  if(nEl) nEl.textContent=order.length+" 个来源";
  /* 「等你处理」要 /api/stats 里的 superseded，这里顺手取一次。
     取不到就只显示"还没抽过记忆的会话数"，不显示第二个条目 —— 宁可少一条，
     也不显示一个编出来的数。 */
  api("/api/stats").then(function(s){
    if(s&&!s.error) renderSessionTodo(rows,s);
  }).catch(function(){});
}
/* 「等你处理」框（2026-10-07 照原型补齐 4 条）。
   原型四条：待质检 · 采集 · 排除 · 清理。逐条对到真实字段：
     待质检 ← s.superseded（已被新版取代、待人工确认的记忆数）
     采集   ← 磁盘上还没入库的会话文件数（走 /api/conv-sources 的 files − 已入库）
     排除   ← s.by_type.context（context 类记忆条数；库里没有独立的"排除清单"表，
              这个数是**最接近的真实量**，不编"排除 N 条"）
     清理   ← 已 superseded 同源，这里给"可销毁残留"口径没有接口 → **不列**
   ⚠️ 凡接口拿不到的就不列，绝不编一个看起来合理的数（用户「不许编数据」）。
   ⚠️ 2026-10-07 结构照原型 `.td > .tt + .ct`（左竖条 + 15px 数字是上一轮自己发明的）。 */
function renderSessionTodo(rows,s){
  var box=document.getElementById("sp-todo"), n=document.getElementById("sp-todo-n");
  if(!box) return;
  var linked=(rows||[]).filter(function(x){return (x.mem_n||0)>0}).length;
  var gapN=Math.max(0,(rows||[]).length-linked);
  var sup=(s&&typeof s.superseded==="number")?s.superseded:0;
  var ctx=(s&&s.by_type&&typeof s.by_type.context==="number")?s.by_type.context:0;
  var items=[
    {tx:"<b>待质检</b> · "+sup+" 条已被新版取代",n:sup,c:"c1"},
    {tx:"<b>采集</b> · "+gapN+" 个会话还没抽过记忆",n:gapN,c:"c2"},
    {tx:"<b>排除</b> · "+ctx+" 条 context 类记忆",n:ctx,c:"c2"},
  ];
  box.innerHTML=items.map(function(i){
    return '<div class="td"><span class="tt">'+i.tx+"</span>"+
      '<span class="ct '+i.c+'">'+i.n+"</span></div>";
  }).join("");
  if(n) n.textContent=(sup+gapN+ctx)+" 项";
}

async function extractSession(sid){
  var box=document.getElementById("s-view");
  box.style.display="block";
  sessionGuide(false);     /* 抽取候选也算"有内容"，收起引导卡 */
  box.innerHTML='<div class="dempty">正在抽取候选记忆…</div>';
  document.getElementById("s-view-acts").innerHTML="";
  var rows=await api("/api/extract?sid="+sid);
  if(!rows.length){
    // ⚠️ 抽不到时**不能**把右栏占了 —— 以前直接把时间线换成一句"没有抽到"，
    //    结果用户什么都没得到，还丢了原来的时间线，只能重新点一次会话。
    //    改成：还原时间线 + 顶部挂一条提示。
    toast("这段会话没有新的候选记忆",'err');
    if(LAST_SES&&LAST_SES.session.id===sid){
      renderSessionView(LAST_SES);
      document.getElementById("s-view").insertAdjacentHTML("afterbegin",
        '<div class="msg" style="margin-bottom:var(--space-4)">这段会话没有新的候选记忆 —— '
        +'内容太短，或者已经抽过了（跟库里已有的太像会自动跳过）。</div>');
    }else{
      box.innerHTML='<div class="dempty">这段会话没有新的候选记忆。</div>';
    }
    return;
  }
  CAND=rows;CAND_SID=sid;
  document.getElementById("s-view-t").textContent="抽取候选";
  document.getElementById("s-view-h").innerHTML=
    '<span class="dmi">从会话 #'+sid+' 抽出 '+rows.length+' 条候选（你=优先）。勾掉不要的，再点「入库选中」。</span>';
  document.getElementById("s-view-acts").innerHTML=
    '<button class="mini" onclick="importCands()">入库选中</button>'
    +'<button class="mini" onclick="cancelExtract()">取消</button>';
  box.innerHTML='<div class="dsec"><h4 class="dsec-t">入库后会带上出处</h4>'
    +'<div class="dmeta">每条都会记住「会话 #'+sid+' · 第几轮」，以后在记忆页点「出处」就能跳回这段原话。</div></div>';
  document.getElementById("s-list").innerHTML=
    '<div class="msg">会话 #'+sid+' 的 '+rows.length+' 条候选记忆 —— 勾选后入库</div>'+
    rows.map(function(c,i){
      return '<div class="mem"><div class="top">'+
        '<input type="checkbox" class="ck" data-i="'+i+'" checked>'+
        '<span class="ttag"><i style="background:'+(c.from_user?"var(--data-me)":"var(--data-ai)")+'"></i>'+(c.from_user?"我":"AI")+'</span>'+
        '<span class="proj">第'+c.turn+'轮</span>'+
        '<span class="accent">'+c.mtype+'</span>'+
        '<span class="score">'+(c.from_user?"优先":"备选")+'</span></div>'+
        '<div class="content">'+esc(c.text)+'</div></div>';
    }).join("");
}
/* 取消抽取：左栏换回会话列表，右栏**还原刚才那段会话的时间线**。
   踩过的坑：以前只调 loadSessions()，右栏被 extractSession 藏掉后就再也没回来，
   用户点了取消只能看到一个空白的右栏。 */
function cancelExtract(){
  var back=CAND_SID;
  CAND=[];CAND_SID=0;
  loadSessions();
  if(back)openSession(back); else clearSessionView();
}
var CAND=[], CAND_SID=0;
async function importCands(){
  var picked=[];
  document.querySelectorAll("#s-list .ck:checked").forEach(function(c){
    var c_=CAND[parseInt(c.dataset.i,10)];if(c_)picked.push(c_);
  });
  if(!picked.length){alert("先勾选要入库的候选");return}
  var back=CAND_SID, n=0;
  for(var i=0;i<picked.length;i++){
    var c=picked[i];
    // ⚠️ session_id / turn 必须一起传 —— 这两行就是"打通"本身。
    //    以前这里没传，抽取时算好的出处全在落库这一步丢了（40 条里 0 条能溯源）。
    await api("/api/save",{method:"POST",headers:{"Content-Type":"application/json"},
      body:JSON.stringify({content:c.text,mtype:c.mtype,importance:3,
        project:c.project,tags:"会话抽取",agent:c.agent||"session-extract",
        session_id:c.session_id||back||0,turn:c.turn||0})});
    n++;
  }
  CAND=[];CAND_SID=0;
  toast("已入库 "+n+" 条（带会话出处）",'ok');
  loadSessions();loadStats();loadList();
  if(back)openSession(back);   // 回到会话详情，直接看到刚挂上去的记忆芯片
}
/* ===== 会话页右栏：原文时间线 + 该会话产出的记忆 =====
   打通的关键就在这几行：记忆不另开一页看，而是**贴在产出它的那一轮旁边**。

   ⚠️ 踩过的坑：以前这里用 el.scrollIntoView()，结果点开一个会话，
   **整个页面**会被滚走（用户反馈"点开会话整页跑掉"）。右栏 .split-side
   本身就是滚动容器（overflow:auto），只动它自己的 scrollTop 就够了。 */
/* ---------- 长文折叠（2026-09-22）----------
   会话原文（几百轮铺下来特别长）/ 记忆详情正文 / 交接卡与常驻上下文输出，
   超过阈值就按行截断，点「展开全文」看全。短内容不出现按钮 —— 免得满屏都是
   「展开」，反而更吵。折叠的是视觉行数，DOM 里内容完好，搜索/复制不受影响。 */
var FOLD_CHARS = 150;
var FOLD_LINES = 4;
var FOLD_SEL = "#s-view .bub, .dbody, .foldbody";
function foldOne(el){
  if(el.dataset.foldReady)return;
  if((el.textContent||"").trim().length<=FOLD_CHARS)return;   // 太短，不折腾
  el.dataset.foldReady="1";
  el.style.webkitLineClamp=FOLD_LINES;
  el.classList.add("foldbody","folded");
  var btn=document.createElement("button");
  btn.type="button";
  btn.className="foldbtn";
  btn.textContent="展开全文";   // 不带箭头：箭头留给"折叠整块区域"用，避免一词两义
  btn.onclick=function(ev){
    ev.stopPropagation();                       // 卡片本身可点，别让点按钮触发选中
    var nowFolded=el.classList.toggle("folded");
    btn.textContent=nowFolded?"展开全文":"收起全文";
  };
  el.insertAdjacentElement("afterend",btn);
}
function foldAll(){document.querySelectorAll(FOLD_SEL).forEach(foldOne)}

/* ---------- 页头「数据与说明」折叠（2026-09-24）----------
   用户原话：每页页头那行数据 + 一段注释结论占了较多视野，第一眼该看到内容，
   这些文字做成可收起 / 可展开，**默认收起**。
   机制：把 .pagehead 之后**紧邻**的 .psub / .lead 挪进 .pfold > .pfin，
   标题右侧挂一个胶囊按钮开合。只做一次（dataset.pmReady 打标）。
   ⚠️ 三个刻意的选择：
     · 状态不持久化（PM_OPEN 只活在内存里）。"默认收起"是默认视图不是用户偏好，
       存 localStorage 的话上次随手展开过的一页，下次进门就不再是默认样子。
     · 用后处理而不是改 9 处 HTML —— 和 wrapGroups / secApply 同一套路；
       以后新增页面只要 .pagehead 后面跟 .psub/.lead，自动被折起来。
     · 没有可折叠内容时不插按钮（不留"点了没反应"的控件）。
   ⚠️ 折叠器类名是 .pfold，**不是** .pagehead/.listhead/.shead/.dhead/.grphead ——
      frameKeyOf() 按"同 section 内 FRAME_SEL 命中项的序号"生成 key，
      多命中一个就会让整张 FRAME_SELECTION 清单错位。 */
var PM_OPEN={};
function pmAll(){
  Array.prototype.forEach.call(
    document.querySelectorAll("section[id^='v-']>.pagehead"),function(h){
    if(h.dataset.pmReady)return;
    h.dataset.pmReady="1";
    var sec=h.closest("section"),key=sec?sec.id.replace(/^v-/,""):"";
    /* ① 页头后面紧邻的数据行 / 注释段，整段挪进折叠区 */
    var inn=document.createElement("div");inn.className="pfin";
    var nx=h.nextElementSibling,moved=[];
    while(nx&&(nx.classList.contains("psub")||nx.classList.contains("lead"))){
      moved.push(nx);nx=nx.nextElementSibling;
    }
    if(!moved.length)return;             /* 这页没有说明性内容 → 什么都不做 */
    var box=document.createElement("div");
    box.className="pfold";box.id="pm-"+key;
    box.appendChild(inn);
    moved.forEach(function(e){inn.appendChild(e)});
    h.parentNode.insertBefore(box,h.nextElementSibling);
    /* ② 标题单独包一层再挂按钮：.pagehead 是 space-between，
          直接把按钮塞进去会被甩到中间（和 .listhead 加箭头踩过同一个坑） */
    var t=h.querySelector(".ptitle");
    if(!t)return;
    var l=document.createElement("div");l.className="phead-l";
    t.parentNode.insertBefore(l,t);l.appendChild(t);
    var b=document.createElement("button");
    b.type="button";b.className="pmtgl";
    b.setAttribute("aria-expanded","false");
    b.setAttribute("aria-controls",box.id);
    b.innerHTML='<svg class="cvs" viewBox="0 0 12 12" fill="none" stroke="currentColor"'+
      ' stroke-width="2" stroke-linecap="round" stroke-linejoin="round">'+
      '<path d="M4.3 2.2L8.1 6l-3.8 3.8"/></svg>'+
      (inn.querySelector(".psub")?"数据与说明":"说明");
    b.onclick=function(){pmToggle(key)};
    l.appendChild(b);
    h._pmTgl=b;
  });
}
function pmToggle(key){
  var h=document.querySelector("#v-"+key+">.pagehead");
  var box=document.getElementById("pm-"+key);
  if(!h||!box)return;
  var on=!PM_OPEN[key];PM_OPEN[key]=on;
  box.classList.toggle("pf-on",on);
  if(h._pmTgl)h._pmTgl.setAttribute("aria-expanded",on?"true":"false");
}

/* ---------- 整块区域折叠（2026-09-22）----------
   用户要的是"整块收起"：收起后只剩标题栏（+ 内容第一行），把高度让给别的区块。
   与上面的文本折叠（line-clamp）不同 —— 这里 display:none 掉整个内容容器，
   布局会真的收缩、下面的内容往上顶。状态存 localStorage，刷新后保持。
   事件用委托绑在 document 上：右栏是 JS 重渲染的，绑在元素上会随 innerHTML 丢。 */
var SEC_FOLD = [
  ["#v-mem .split-main .shead",     "#v-mem .split-main .sbody",     "mem-list"],
  ["#v-mem .split-side .dhead",     "#v-mem .split-side .dmain",     "mem-detail"],
  ["#v-session .split-main .shead", "#v-session .split-main .sbody", "sess-list"],
  ["#v-session .split-side .dhead", "#v-session .split-side .dmain", "sess-view"],
  ["#v-skill .split-main .shead",   "#v-skill .split-main .sbody",   "skill-list"],
  ["#v-skill .split-side .dhead",   "#v-skill .split-side .dmain",   "skill-detail"]
];
function secOn(k){try{return localStorage.getItem("hp.sec."+k)==="1"}catch(e){return false}}
function secApply(){
  if(!SEC_FOLD)return;   // 定义在脚本末尾；早期调用（初始化 show）先安全退出
  SEC_FOLD.forEach(function(cfg){
    var head=document.querySelector(cfg[0]), body=document.querySelector(cfg[1]);
    if(!head||!body)return;
    if(head.dataset.secfold!==cfg[2])head.dataset.secfold=cfg[2];
    if(body.dataset.secbody!==cfg[2])body.dataset.secbody=cfg[2];
    var on=secOn(cfg[2]);
    // 只在状态真的不一致时才动 DOM —— 下面的 MutationObserver 靠这个收敛，
    // 否则 secApply 改 DOM → 触发 observer → 再 secApply，会自激成死循环。
    if(body.classList.contains("hide")!==on)body.classList.toggle("hide",on);
    if(head.classList.contains("sec-collapsed")!==on)head.classList.toggle("sec-collapsed",on);
    // 折叠控件是 div，默认 Tab 不到、读屏也不知道开合状态 —— 补上（只做一次）
    if(!head.hasAttribute("role")){
      head.tabIndex=0;head.setAttribute("role","button");
      head.addEventListener("keydown",function(e){
        if(e.key==="Enter"||e.key===" "){e.preventDefault();head.click();}
      });
    }
    head.setAttribute("aria-expanded", on?"false":"true");
    var one=head.querySelector(".sec-1");
    if(on){
      if(!one){one=document.createElement("span");one.className="sec-1";head.appendChild(one)}
      var first=body.querySelector(".mtitle,.mmain,.mem,.bub");
      var txt=first?(first.textContent.trim().replace(/\s+/g," ").slice(0,50)):"";
      if(one.textContent!==txt)one.textContent=txt;
    }else if(one){one.remove()}
  });
}
/* 右栏详情、本机内容右栏、会话左列表都是随时重渲染的：
   靠 observer 自动把折叠状态补回去，免得每处渲染都手写一次调用（漏一处就失效）。 */
var _secQ=false;
function secQueue(){if(_secQ)return;_secQ=true;setTimeout(function(){_secQ=false;secApply()},0)}
/* 观察名单：这些容器随时被重渲染，折叠状态靠 observer 补回去，免得每处渲染都手写一次。 */
["detail","sk-detail","sk-list","s-list","list"].forEach(function(id){
  var el=document.getElementById(id);
  if(el)new MutationObserver(secQueue).observe(el,{childList:true,subtree:true});
});
document.addEventListener("click",function(e){
  var head=e.target.closest("[data-secfold]");
  if(!head)return;
  if(e.target.closest("button,a,input,select,.dacts,.macts"))return;  // 别抢按钮
  var k=head.dataset.secfold;
  var now=!secOn(k);
  try{localStorage.setItem("hp.sec."+k,now?"1":"0")}catch(err){}
  secApply();
});

var LAST_SID=null, LAST_SES=null;

function roleClass(r){return r==="user"?"me":(r==="assistant"?"ai":"raw")}

/* 找出真正会滚的那个祖先容器。
   ⚠️ 别用 window.scrollY 判断"页面有没有被滚" —— 面板的 body 是 overflow:hidden，
   滚动发生在 div.content 上（overflow-y:auto），window.scrollY 永远是 0。
   踩过：据此写的自检是**假通过**，改动其实没生效也看不出来。 */
function scrollerOf(el){
  var n=el&&el.parentElement;
  while(n&&n!==document.body){
    var cs=getComputedStyle(n);
    if(/(auto|scroll)/.test(cs.overflowY)&&n.scrollHeight>n.clientHeight+2)return n;
    n=n.parentElement;
  }
  return null;
}

/* 滚到某一轮 —— 不管是右栏自己被限高（窄屏）还是整个内容区在滚，都能滚对地方。
   ⚠️ 不用 scrollIntoView：它会把**所有**可滚祖先一起滚，用户点一个会话
   整片内容就被顶走（2026-09-21 用户反馈"点开会话整页跑掉"的根因）。
   也不用 block:"center" —— 一轮原文可能比视口还高（实测 1162px vs 834px），
   居中会让这一轮的开头跑到屏幕上方看不见。对齐到顶再留 12px。 */
function scrollSide(el){
  var box=scrollerOf(el);
  if(!box)return;
  box.scrollTop += (el.getBoundingClientRect().top - box.getBoundingClientRect().top) - 12;
}

function memChip(m){
  var t=String(m.content||"");
  return '<span class="tchip" onclick="gotoMemCard('+m.id+')" title="'+escAttr(t)+'">'
    +'<i style="background:'+(TC[m.mtype]||"var(--data-context)")+'"></i><b>#'+m.id+'</b>'
    +'<span class="tchip-t">'+esc(t)+'</span></span>';
}

function renderSessionView(r){
  var el=document.getElementById("s-view");
  var msgs=r.messages||[], mems=r.memories||[], s=r.session||{};
  document.getElementById("s-view-t").textContent=s.title||"原文时间线";
  /* 元信息行（2026-09-25）：来源 Agent 用彩色圆点徽章（复用技能/配置详情栏的 agentTag），
     其余几项统一走 .dmi —— 与技能页同一套写法，不是给会话页另起一套。
     新增「工作区」：同一个会话标题在不同工作区里聊过时，这是唯一的区分依据。 */
  var ws=s.source_path?projName(s.source_path):"";
  var day=String(s.started_at||s.created_at||"").slice(0,10);
  document.getElementById("s-view-h").innerHTML=
    (s.agent?agentTag(agentName(s.agent)):"")
    +'<span class="dmi">#'+s.id+' · '+msgs.length+' 轮 · 产出 '+mems.length+' 条记忆</span>'
    +(day?'<span class="dmi">'+esc(day)+'</span>':'')
    +(ws?'<span class="dmi">工作区 '+esc(ws)+'</span>':'');
  document.getElementById("s-view-acts").innerHTML=
    '<button class="mini" onclick="extractSession('+s.id+')">抽取记忆</button>'
    +'<button class="mini" onclick="copySessionText('+s.id+')">复制原文</button>'
    +'<button class="mini warn" onclick="delSession('+s.id+')">删除</button>';
  // 记忆按轮次分组：有 turn 的贴到对应轮，没 turn 的收在末尾
  var byTurn={}, loose=[];
  mems.forEach(function(m){
    if(m.turn>0){(byTurn[m.turn]=byTurn[m.turn]||[]).push(m)}else{loose.push(m)}
  });
  var html="";
  if(!msgs.length)html='<div class="dempty">这段会话没有正文。</div>';
  msgs.forEach(function(m){
    html+='<div class="bub '+roleClass(m.role)+'" id="tn-'+s.id+'-'+m.turn+'">'
      +'<span class="btag">'+esc(roleName(m.role))+' · 第 '+m.turn+' 轮</span>'
      +esc(m.content)+'</div>';
    var hit=byTurn[m.turn];
    if(hit&&hit.length){
      html+='<div class="tmem"><span class="tmem-k">这一轮产出 '+hit.length+' 条</span>'
        +hit.map(memChip).join("")+'</div>';
    }
  });
  if(loose.length){
    html+='<div class="dsec"><h4 class="dsec-t">本会话产出记忆 · 未标注轮次</h4>'
      +'<div class="tmem" style="margin:0">'+loose.map(memChip).join("")+'</div></div>';
  }
  el.innerHTML=html;
  el.style.display="block";
  sessionGuide(false);     /* 有内容了 → 收起引导卡 */
  foldAll();
  secApply();
}

/* ── 详情栏的「空态引导卡」 ────────────────────────────────────
   三处主从骨架共用：记忆页 #detail / 会话页 #s-guide / 技能页 #sk-detail。
   2026-09-24 用户拍板：**撤销「空态塌单列」** —— 详情栏不再消失，
   未选中时就渲染这张卡，把"该点哪里"说清楚，两栏宽度也恒定不跳。
   ⚠️ 别在页面上手写 grid-template-columns。 */
function paneGuide(title,msg){
  return '<div class="guide">'+
    '<div class="gic"><svg viewBox="0 0 24 24">'+
      '<path d="M8 6h13M8 12h13M8 18h9M3.5 6h.01M3.5 12h.01M3.5 18h.01"/></svg></div>'+
    '<h4>'+esc(title)+'</h4><p>'+esc(msg)+'</p></div>';
}
/* 会话页的引导卡是静态 HTML（#s-guide），与 #s-view（原文时间线）互斥显隐，
   统一走这里，别再各写一份。 */
function sessionGuide(on){
  var g=document.getElementById("s-guide");
  if(g)g.style.display=on?"":"none";
}
/* 技能页详情栏的「过渡 / 失败」占位 —— **必须带 .dhead**。
   缺了 .dhead 整块会被当成"不是正经详情"，视觉上像卡了半截
   （踩过：错误态只写 .dmain>.empty，标题栏空着、文案孤零零悬在中间）。
   注意判据不能放宽到 .empty —— 骨架初始空态本身也是 .dmain>.empty。 */
function skHold(title,msg){
  return '<div class="dhead"><div class="dhtop"><h3>'+esc(title)+'</h3></div></div>'+
         '<div class="dmain"><div class="empty">'+esc(msg)+'</div></div>';
}

function clearSessionView(){
  LAST_SID=null;LAST_SES=null;
  document.getElementById("s-view-t").textContent="原文时间线";
  document.getElementById("s-view-h").innerHTML=
    '<span class="dmi">点左侧任意会话查看原文；「抽取记忆」会在这里逐轮给出候选记忆</span>';
  document.getElementById("s-view-acts").innerHTML="";
  var el=document.getElementById("s-view");
  el.innerHTML="";el.style.display="none";
  sessionGuide(true);      /* 详情栏清空了 → 换回引导卡（不再塌单列） */
}

async function openSession(sid){
  var r=await api("/api/session/get?sid="+sid);
  if(r.error){
    // 会话被删了是常见情况：从它抽出来的记忆会保留（出处变死链），
    // 别弹 alert 吓人，说清"记忆还在、原话没了"就行。
    if(String(r.error).indexOf("不存在")>=0){
      toast("这段会话已被删除 —— 记忆还留着，但原话看不到了",'err');
      clearSessionView();
    }else{alert(r.error)}
    return;
  }
  LAST_SID=sid;LAST_SES=r;   // 缓存：取消抽取时要原样还原，别重新请求
  renderSessionView(r);
}

function copySessionText(sid){
  if(!LAST_SES||LAST_SES.session.id!==sid)return;
  var r=LAST_SES;
  var txt="#"+sid+" "+r.session.title+"\n"+r.messages.map(function(m){
    return "── 第"+m.turn+"轮 · "+roleName(m.role)+" ──\n"+m.content;
  }).join("\n\n");
  if(navigator.clipboard&&navigator.clipboard.writeText){
    navigator.clipboard.writeText(txt).then(function(){toast("已复制原文",'ok')},
      function(){toast("复制失败，请手动选择",'err')});
  }else{toast("当前浏览器不支持自动复制",'err')}
}

/* 记忆 → 会话：点记忆的「出处」跳回来，滚到产出它的那一轮 */
function gotoSession(sid,turn){
  show("session");
  loadSessions().then(function(){return openSession(sid)}).then(function(){
    var t=(turn>0)?document.getElementById("tn-"+sid+"-"+turn):null;
    if(t)scrollSide(t);
  });
}

/* ── 会话详情 · 独立页面（2026-10-06 用户拍板方案 2）─────────────────────
   ⚠️ 与 openSession() 的关系：openSession 是旧的三栏骨架里"把详情渲染进右栏"，
      还被 gotoSession（记忆页跳回来）依赖，**不能删也不能改**。
      这里是**另一条路**：点标题 → 整页换成 #v-sdetail，走 history.pushState
      让浏览器后退键能退回列表。两条路并存，old 路径继续服务"记忆 → 出处"跳转。 */
var SD_SID=null;      /* 当前详情页的会话 id，供「复制原文」用 */

/* Agent 名 → 标签 class。配色沿用列表页同一套，不另造颜色 */
function sdSrcCls(a){
  var s=String(a||"").toLowerCase();
  if(s.indexOf("workbuddy")>=0)return"wb";
  if(s.indexOf("zcode")>=0)return"zc";
  if(s.indexOf("qoder")>=0)return"qd";
  return"ot";
}
function sdSrcName(a){
  var s=String(a||"");
  return s||"未知来源";
}
/* 正文里的代码块与围栏：Agent 的原文常带 markdown，做最小渲染即可 */
function sdText(t){
  var h=esc(t||"");
  h=h.replace(/```([\s\S]*?)```/g,function(_,c){return "<pre>"+c.replace(/^\n+|\n+$/g,"")+"</pre>"});
  h=h.replace(/`([^`\n]+)`/g,"<code>$1</code>");
  h=h.replace(/\*\*([^*\n]+)\*\*/g,"<b>$1</b>");
  h=h.replace(/\n{2,}/g,"</p><p>");
  h=h.replace(/\n/g,"<br>");
  return "<p>"+h+"</p>";
}
function renderSessionDetail(r){
  var s=r.session||{}, msgs=r.messages||[];
  var locked=!msgs.length;
  document.getElementById("sd-title").textContent=s.title||"（无标题）";
  /* 元信息：来源 Agent / 明文·加密 / 时间 / 轮数 / 项目
     ⚠️ 加密会话给不出正文是**能力限制**（数据源本来就没有原文），
        所以文案写"这段对话没有原文"，不写"不给看"。
     ⚠️ 2026-10-07 照原型改两处：
        时间 2026-10-04 22:41 → 「10 月 4 日 22:41」（原型 .meta 的写法）
        项目 印 project 全文路径 → projName() 短名（同列表页） */
  var t=(s.created_at||"").replace("T"," ");
  var tShow=(t.length>=16)?((+t.slice(5,7))+" 月 "+(+t.slice(8,10))+" 日 "+t.slice(11,16)):"";
  document.getElementById("sd-meta").innerHTML=
    '<span class="sd-src '+sdSrcCls(s.agent)+'">'+esc(sdSrcName(s.agent))+"</span>"+
    '<span class="sd-st '+(locked?"lock":"plain")+'">'+(locked?"加密":"明文")+"</span>"+
    (tShow?'<span class="sep">·</span><span>'+esc(tShow)+"</span>":"")+
    (s.msg_count?'<span class="sep">·</span><span>'+s.msg_count+" 轮</span>":"")+
    (s.project?'<span class="sep">·</span><span>'+esc(projName(s.project))+"</span>":"");
  var box=document.getElementById("sd-thread");
  if(locked){
    box.innerHTML='<div class="sd-locked"><div class="lic">'+
      '<svg viewBox="0 0 24 24"><rect x="4.5" y="10.5" width="15" height="10" rx="2.5"/>'+
      '<path d="M8 10.5V7a4 4 0 0 1 8 0v3.5"/></svg></div>'+
      "<h3>这段对话没有原文</h3>"+
      "<p>来源 Agent 只留下了标题和来源，没有把对话内容存在本机。"+
      "所以这里显示不出正文 —— 不是被藏起来了，是本来就没有。</p></div>";
    return;
  }
  /* 用户头像：正圆里一个人形 SVG（原型 .av.u）。
     ⚠️ 2026-10-07 原来塞的是「你」字 —— 字在正圆里会挤，
        而且跟原型那个图标根本不是一回事。 */
  var AV_U='<svg viewBox="0 0 24 24"><circle cx="12" cy="8" r="3.4"/>'+
    '<path d="M4.8 20a7.4 7.4 0 0 1 14.4 0"/></svg>';
  box.innerHTML=msgs.map(function(m){
    var u=(m.role==="user"), raw=(m.role==="raw");
    var tm=(m.created_at||"").replace("T"," ").slice(11,16);
    return '<div class="sd-msg"><div class="sd-av '+(u?"u":"a")+'">'+
      (u?AV_U:(raw?"原":"AI"))+'</div><div class="sd-mb">'+
      '<div class="sd-who">'+esc(raw?"原文":roleName(m.role))+
      (tm?'<span class="tm">'+esc(tm)+"</span>":"")+"</div>"+
      '<div class="sd-tx">'+sdText(m.content)+"</div></div></div>";
  }).join("")+'<div class="sd-end">这段对话结束 · '+(s.msg_count||msgs.length)+" 轮</div>";
}
async function openSessionDetail(sid){
  var r=await api("/api/session/get?sid="+sid);
  if(r.error){ toast(r.error,"err"); return }
  if(sid!==SD_SID){
    renderSessionDetail(r);
    /* pushState 而不是 replaceState —— 后退键要能退回列表，
       这正是"独立页面"跟"同页展开"最实际的差别 */
    try{history.pushState({sid:sid},"","#session?sid="+sid)}catch(e){}
  }
  SD_SID=sid;
  show("sdetail");
  scrollTo(0,0);
}
function closeSessionDetail(){
  SD_SID=null;
  show("session");
  try{if(location.hash!=="#session")history.pushState({},"","#session")}catch(e){}
}
/* 后退键：从详情退回列表时，hash 会变但 show() 不自动跑，补一次 */
window.addEventListener("popstate",function(){
  var m=/^#session\?sid=(\d+)$/.exec(location.hash||"");
  if(m)openSessionDetail(m[1]);
  else if(/^#sdetail/.test(location.hash||"")||(location.hash||"")==="#session")
    show("session");
});
function copySessionRaw(){
  if(!SD_SID)return;
  var b=document.getElementById("sd-copy");
  api("/api/session/get?sid="+SD_SID).then(function(r){
    var txt=r.messages.map(function(m){
      return "── 第"+m.turn+"轮 · "+roleName(m.role)+" ──\n"+m.content;
    }).join("\n\n");
    if(navigator.clipboard&&navigator.clipboard.writeText){
      navigator.clipboard.writeText(txt).then(function(){toast("已复制原文",'ok')},
        function(){toast("复制失败，请手动选择",'err')});
    }else toast("当前浏览器不支持自动复制","err");
  });
}

/* 会话 → 记忆：点产出芯片跳到记忆页并选中那一条 */
function gotoMemCard(id){
  show("mem");SELID=id;
  loadList().then(function(){
    var el=document.getElementById("memcard-"+id);
    if(el){el.classList.add("sel");el.scrollIntoView({block:"nearest",behavior:"smooth"})}
  });
}
async function delSession(sid){
  if(!confirm("删除会话 #"+sid+" 及其全部原文？（不可恢复）\n\n注意：从这段会话里抽出来的记忆会保留，但会变成「无来源」。"))return;
  var r=await api("/api/session/delete",{method:"POST",
    headers:{"Content-Type":"application/json"},body:JSON.stringify({sid:sid})});
  if(r&&r.ok===false){ toast(r.error||("删除会话 #"+sid+" 失败"),"err"); return }  /* 2026-10-01 修 P1-1 同源 */
  clearSessionView();
  loadSessions();loadStats();
}
async function sessionSearch(){
  // 原话检索结果用**完整卡片**（不是紧凑行）—— 这里正文本身就是结果，
  // 必须能读。点一张卡直接跳到那段会话的那一轮。
  var q=document.getElementById("s-q").value.trim();
  if(!q){loadSessions();return}
  var rows=await api("/api/session/search?q="+encodeURIComponent(q)+"&limit=20");
  document.getElementById("s-list").innerHTML = rows.length ? rows.map(function(r){
    return '<div class="mem" style="cursor:pointer" title="点开跳回这段原话"'
      +' onclick="gotoSession('+r.session_id+','+r.turn+')"><div class="top">'
      +'<span class="ttag"><i style="background:'+roleColor(r.role)+'"></i>'+roleName(r.role)+'</span>'
      +'<span class="proj">#'+r.session_id+' '+esc(r.stitle)+'</span>'
      +'<span class="accent">第'+r.turn+'轮</span>'
      +'<span class="score">相关 '+(r.score*100).toFixed(0)+'%</span>'
      +'<span style="margin-left:auto;font-size:11px;color:var(--acc)">跳过去 →</span></div>'
      +'<div class="content">'+esc(r.content.length>400?r.content.slice(0,400)+"…":r.content)+'</div></div>';
  }).join("") : '<div class="empty">原话里没有找到相关片段</div>';
}

/* 记忆质检 */
function amsg(t,kind){
  var el=document.getElementById("audit-msg");
  if(!t){el.style.display="none";return}
  el.style.display="block";el.className="msg"+(kind?(" "+kind):"");el.textContent=t;
}
function memLine(r,extra){
  return '<div style="color:var(--sub);font-size:13px;padding:2px 0">#'+r.id+' '+
    ((r.project?("["+esc(r.project)+"] "):"")) + esc(r.content.slice(0,110)) + (extra||"") + '</div>';
}
/* 心跳：页面开着就算「在用」，关掉页面后服务会自己退出 */
setInterval(function(){ api("/api/ping").catch(function(){}); }, 60000);

async function shutdownPanel(){
  if(!confirm("关闭面板服务？\n\n记忆数据不受影响（都存在 loci.db 里）。\n下次要用，双击桌面的 Loci 图标即可重新打开。")) return;
  try{ await api("/api/shutdown",{method:"POST",headers:{"Content-Type":"application/json"},body:"{}"}); }catch(e){}
  document.documentElement.innerHTML=
    '<body class="bye">'+
      '<img src="/icon.png" alt="">'+
      '<h2>面板服务已关闭</h2>'+
      '<p>记忆数据完好，保存在 <code>loci.db</code> 里。<br>'+
      '你的 AI Agent 照常能读写记忆 —— 它们不依赖这个面板。<br>'+
      '下次要管理，双击桌面的 <b>Loci</b> 图标即可。</p>'+
    '</body>';
}

async function renderHealth(){
  var proj=document.getElementById("au-proj").value;
  var h=await api("/api/health"+(proj?("?project="+encodeURIComponent(proj)):""));
  /* 七查维度（按原型 C 的 .hdims）。
     ⚠️ /api/health 把「缺项目 / 缺标签」**分开**返回，这里合成原型的第 7 项「元数据缺失」——
        原型画的就是七查七个格子，拆成八格会跟标题的"七查"对不上。
     顺序固定：同一项永远在同一格，跨时间对比才有意义（不按条数排）。 */
  var c=h.counts||{};
  var dims=[
    ["重复",       c["重复组"]||0],
    ["疑似同义",   c["疑似同义"]||0],
    ["可能矛盾",   c["可能矛盾"]||0],
    ["长期未更新", c["长期未更新"]||0],
    ["过短",       c["过短"]||0],
    ["过粗粒度",   c["过粗粒度"]||0],
    ["元数据缺失", (c["缺项目"]||0)+(c["缺标签"]||0)]
  ];
  var mx=Math.max.apply(null,dims.map(function(d){return d[1]}))||1;
  var dimHtml=dims.map(function(d){
    var v=d[1];
    /* 0 条 = 干净 → 条给满格绿色（"这一项没事"本身就是要传递的信息）；
       有问题 → 按相对最大项给宽度，最小 6% 保证看得见（1 条不该是一条看不见的线）。 */
    var pct=v?Math.max(6,Math.round(v/mx*100)):100;
    return '<div class="d"><div class="dk">'+d[0]+'</div>'+
      '<div class="dv" style="color:'+(v?"var(--data-decision)":"var(--ok)")+'">'+v+'</div>'+
      '<div class="dbar"><i style="width:'+pct+'%;background:'+
        (v?"var(--data-decision)":"var(--data-skill)")+'"></i></div></div>';
  }).join("");
  /* 一句话总结：说清"几项干净、问题集中在哪"—— 比只丢一个分数有用得多。
     干净项为 0 时不能说"七项全干净"，所以分开处理。 */
  var clean=dims.filter(function(d){return d[1]===0}).length;
  var bad=dims.filter(function(d){return d[1]>0})
    .sort(function(a,b){return b[1]-a[1]})
    .map(function(d){return "<b>"+d[1]+" 条"+d[0]+"</b>"});
  var summary = clean===7
    ? "七项全干净 —— 这个库没有需要处理的问题。"
    : "七项里 <b>"+clean+" 项干净</b>。待处理集中在："+bad.join("、")+"。";
  var g=String(h.grade||"A").toLowerCase();
  document.getElementById("health").innerHTML=
    '<div class="hcard"><div class="htop">'+
      '<div class="score"><b>'+h.score+'</b>'+
        '<span class="hgrade '+g+'">'+esc(h.grade||"A")+' 级</span>'+
        '<span class="of">/ 100</span></div>'+
      '<div class="hsum">'+summary+'</div>'+
    '</div><div class="hdims">'+dimHtml+'</div></div>';
  /* ④ 七格铺满：.hdims 原本是 repeat(auto-fit,minmax(143px,1fr)) —— 7 个格子
     在 1460px 下排成 4 列时，最后一格右边会空出正好一格（露 .hdims 的灰底，
     像"加载失败"，2026-09-27 用户报的）。
     改成**按实际格子数设列数**（格子数会随检查项变化，写死 7 不保险）：
     桌面按格数均分；窄屏由下面的媒体查询降级到 4/2 列。 */
  (function(){
    var g=document.querySelector('#health .hdims'); if(!g)return;
    var n=g.querySelectorAll('.d').length; if(!n)return;
    g.style.gridTemplateColumns='repeat('+n+',minmax(0,1fr))';
  })();
  var ps=document.getElementById("au-psub");
  if(ps)ps.innerHTML="七查 · 全库 <b>"+(h.scanned||h.total||"—")+"</b> 条 · 评级 <b>"
    +esc(h.grade||"—")+"</b>"+(h.grade_text?("（"+esc(h.grade_text)+"）"):"");
}
async function splitMem(id){
  if(!confirm("把 #"+id+" 拆分成多条子记忆？\n\n原文默认保留（标记作废），拆分出的子条继承项目/类型/来源。")) return;
  var r=await api("/api/split",{method:"POST",headers:{"Content-Type":"application/json"},
    body:JSON.stringify({id:id,max_len:600,keep:1})});
  if(r.error){toast(r.error,"err");return}
  toast("已拆成 "+r.parts+" 条子记忆","ok");
  runAudit();
}
async function exportReport(){
  progress(true);
  var proj=document.getElementById("au-proj").value;
  var r=await api("/api/audit/report"+(proj?("?project="+encodeURIComponent(proj)):""));
  progress(false);
  var md=r.markdown||"";
  if(!md){toast("报告生成失败","err");return}
  downloadMd(md, "loci-质检报告-" + new Date().toISOString().slice(0,10) + ".md");
  toast("质检报告已下载","ok");
}
function downloadMd(text, filename){
  var blob=new Blob([text],{type:"text/markdown;charset=utf-8"});
  var a=document.createElement("a");
  a.href=URL.createObjectURL(blob);
  a.download=filename;
  document.body.appendChild(a);
  a.click();
  a.remove();
  setTimeout(function(){URL.revokeObjectURL(a.href)},3000);
}
async function runAudit(){
  var proj=document.getElementById("au-proj").value;
  amsg("正在质检（重复/矛盾/过期）…");
  var r=await api("/api/audit"+(proj?("?project="+encodeURIComponent(proj)):""));
  /* 七查明细（按原型 C）：每项压成一行 .auditrow —— 序号 + 名称 + 说明 + 条数徽章，
     **点行才展开候选**。以前是七段全铺开：0 值的项也要各占一个空块，页面被撑得很长，
     真正有问题的项反而被淹掉。现在有问题的排前面、默认展开，干净的缩成一行绿徽章。
     顺序按"待处理条数"降序 —— 一眼看到最该先处理的。 */
  var secs=[
    {k:"过粗粒度", tip:"一条里塞了多件事，检索时容易「整条命中但只用到一句」。超过 200 字即入列。",
     n:(r.coarse||[]).length,
     rows:function(){return (r.coarse||[]).map(function(x){
       return '<div class="mem"><div class="top"><span class="ttag"><i style="background:#ff9f0a"></i>'+x.content.length+' 字</span>'+
         '<span class="proj">'+esc(x.project||"—")+'</span><span class="score">#'+x.id+'</span></div>'+
         '<div class="content">'+esc(x.content.slice(0,200))+'…</div>'+
         '<div class="meta"><span>拆成多条后检索更精准，原文会保留</span>'+
         '<span><button class="del" onclick="splitMem('+x.id+')">拆分成多条</button></span></div></div>';
     }).join("")}},
    {k:"重复", tip:"同一件事写了两遍以上。合并后只保留一条，其余标记作废。",
     n:r.duplicates.length,
     rows:function(){return r.duplicates.map(function(g,i){
       var ids=g.map(function(x){return x.id}).join(",");
       return '<div class="mem"><div class="top"><span class="ttag"><i style="background:var(--data-decision)"></i>重复组 '+(i+1)+'</span>'+
         '<span class="proj">'+g.length+' 条同义</span></div><div class="content">'+
         g.map(function(x){return memLine(x,"")}).join("")+'</div>'+
         '<div class="meta"><span>合并后保留最新一条，其余标记作废</span>'+
         '<span><button class="del" onclick="mergeGroup(\''+ids+'\')">合并</button></span></div></div>';
     }).join("")}},
    {k:"元数据缺失", tip:"缺项目 / 缺标签。补上能显著提升「按项目检索」的命中率。",
     n:(r.no_project||[]).length+(r.no_tags||[]).length,
     rows:function(){return '<div class="mem"><div class="content">'+
         (r.no_project||[]).slice(0,8).map(function(x){return "#"+x.id+" 缺项目："+esc(x.content.slice(0,36))}).join("<br>")+
         ((r.no_tags||[]).slice(0,6).map(function(x){return "#"+x.id+" 缺标签："+esc(x.content.slice(0,32))}).join("<br>"))+
         '</div><div class="meta"><span>补上项目/标签能显著提升按项目检索的命中率</span></div></div>';}},
    {k:"过短", tip:"不到 20 字，信息量不足，建议补充或删除。",
     n:(r.tiny||[]).length,
     rows:function(){return (r.tiny||[]).map(function(x){
       return '<div class="mem"><div class="top"><span class="ttag"><i style="background:#8e8e93"></i>过短</span><span class="score">#'+x.id+'</span></div>'+
         '<div class="content">'+esc(x.content)+'</div>'+
         '<div class="meta"><span>信息量不足，建议补充或删除</span>'+
         '<span><button class="del" onclick="retireOne('+x.id+')">作废</button></span></div></div>';
     }).join("")}},
    {k:"疑似同义", tip:"措辞不同但可能是同一件事，需要人工判断。",
     n:(r.suspects||[]).length,
     rows:function(){return (r.suspects||[]).map(function(c){
       return '<div class="mem"><div class="top"><span class="ttag"><i style="background:var(--data-preference)"></i>疑似同义</span>'+
         '<span class="score">相似度 '+c.similarity+'</span></div><div class="content">'+
         memLine(c.a,"")+memLine(c.b,"")+'</div>'+
         '<div class="meta"><span>措辞不同但可能是同一件事，请人工判断</span>'+
         '<span><button class="del" onclick="mergeGroup(\''+c.a.id+','+c.b.id+'\')">合并</button></span></div></div>';
     }).join("")}},
    {k:"可能矛盾", tip:"同一个问题前后说法不一致。若新版已取代旧版，可作废旧版。",
     n:r.conflicts.length,
     rows:function(){return r.conflicts.map(function(c){
       return '<div class="mem"><div class="top"><span class="ttag"><i style="background:#ff453a"></i>新旧冲突</span>'+
         '<span class="score">相似度 '+c.similarity+'</span></div><div class="content">'+
         memLine(c.older,' ── 旧版说法')+
         memLine(c.newer,' ── 新版说法')+'</div>'+
         '<div class="meta"><span>若新版说法已取代旧版，可作废旧版</span>'+
         '<span><button class="del" onclick="supersedePair('+c.older.id+','+c.newer.id+')">以新代旧</button> '+
         '<button class="del" onclick="retireOne('+c.older.id+')">作废旧的</button></span></div></div>';
     }).join("")}},
    {k:"长期未更新", tip:"很久没有确认过。仍然有效就续期，不再适用就作废。",
     n:r.stale.length,
     rows:function(){return r.stale.map(function(x){
       return '<div class="mem"><div class="top"><span class="ttag"><i style="background:#8e8e93"></i>'+x.days+' 天未确认</span>'+
         (x.project?('<span class="proj">'+esc(x.project)+'</span>'):"")+'</div><div class="content">'+
         memLine(x,"")+'</div><div class="meta"><span>仍然有效就续期，不再适用就作废</span>'+
         '<span><button class="del" onclick="touchOne('+x.id+')">续期</button> '+
         '<button class="del" onclick="retireOne('+x.id+')">作废</button></span></div></div>';
     }).join("")}}
  ];
  var h='<div class="pcard"><div class="phead"><span class="t">七查明细</span>'+
    '<span class="hint">按待处理条数排序 · 点一项展开候选</span></div><div class="pbody tight">';
  h+=secs.map(function(x,i){
    var open=x.n>0;                       /* 有问题的默认展开，干净的收起 */
    return '<div class="auditrow'+(open?" open":"")+'" id="aur-'+i+'" onclick="toggleAudit('+i+')">'+
      '<span class="ano" style="background:'+(open?"var(--t-decision-bg)":"var(--t-skill-bg)")+
        ';color:'+(open?"var(--data-decision)":"var(--data-skill)")+'">'+(i+1)+'</span>'+
      '<div class="grow"><div style="font-size:14px;font-weight:500">'+x.k+'</div>'+
        '<div class="hint" style="margin-top:3px">'+x.tip+'</div></div>'+
      (open?('<span class="tb warn">'+x.n+' 条</span>'):'<span class="tb ok">干净</span>')+
      '<span class="cv">'+(open?"收起":"展开")+'</span>'+
    '</div>'+
    '<div class="auditbody" id="aub-'+i+'" style="display:'+(open?"block":"none")+'">'+
      (open?x.rows():'<div class="empty">这一项没有问题。</div>')+'</div>';
  }).join("");
  h+='</div></div>';
  document.getElementById("audit-out").innerHTML=h;
  renderHealth();
  amsg("质检完成：扫描 "+r.scanned+" 条 · 重复 "+r.duplicates.length+" 组 · 疑似同义 "+
       (r.suspects||[]).length+" 对 · 矛盾 "+r.conflicts.length+" 对 · 过期 "+r.stale.length+" 条",
       (r.conflicts.length||r.duplicates.length)?"err":"ok");
}
/* 展开/收起一项（纯 class 切换，不做动画 —— 几何类闸门会被动画中的偏移误伤） */
function toggleAudit(i){
  var b=document.getElementById("aub-"+i), row=document.getElementById("aur-"+i);
  if(!b)return;
  var open=b.style.display==="none";
  b.style.display=open?"block":"none";
  if(row){
    row.classList.toggle("open",open);
    var cv=row.querySelector(".cv"); if(cv)cv.textContent=open?"收起":"展开";
  }
}
async function mergeGroup(ids){
  var arr=ids.split(",").map(function(x){return parseInt(x,10)});
  var keep=Math.max.apply(null,arr);
  var text=prompt("合并后的内容（留空=自动取最长的一条）：","");
  if(text===null)return;
  var r=await api("/api/merge",{method:"POST",headers:{"Content-Type":"application/json"},
    body:JSON.stringify({ids:arr,content:text||null})});
  if(r.error){amsg(r.error,"err");return}
  amsg("已合并：保留 #"+r.keep+"，作废 "+r.merged+" 条","ok");
  runAudit();loadStats();
}
/* 2026-10-01 修（P1-1 / P3-6）：后端这三个接口现在会回**真实的 ok**，
   而它们以前不看返回值 —— 记忆不存在也照样弹「已作废 / 已续期」，是典型的假成功。
   现在失败就报错，并且不再刷新数据（刷完还是旧状态，反而更像成功了）。 */
async function supersedePair(oldId,newId){
  if(!confirm("把 #"+oldId+" 标记为被 #"+newId+" 取代？（旧记录保留，不再参与检索）"))return;
  var r=await api("/api/supersede",{method:"POST",headers:{"Content-Type":"application/json"},
    body:JSON.stringify({old_id:oldId,new_id:newId})});
  if(r&&r.ok===false){ amsg(r.error||("作废 #"+oldId+" 失败"),"err"); return }
  amsg("已作废 #"+oldId+"（由 #"+newId+" 取代）","ok");
  runAudit();loadStats();
}
async function retireOne(id){
  if(!confirm("作废记忆 #"+id+"？（保留记录，不再参与检索）"))return;
  var r=await api("/api/retire",{method:"POST",headers:{"Content-Type":"application/json"},
    body:JSON.stringify({id:id})});
  if(r&&r.ok===false){ amsg(r.error||("作废 #"+id+" 失败"),"err"); return }
  amsg("已作废 #"+id,"ok");
  runAudit();loadStats();
}
async function touchOne(id){
  var r=await api("/api/touch",{method:"POST",headers:{"Content-Type":"application/json"},
    body:JSON.stringify({id:id})});
  if(r&&r.ok===false){ amsg(r.error||("续期 #"+id+" 失败"),"err"); return }
  amsg("已续期 #"+id+"（过期计时重新开始）","ok");
  runAudit();
}

/* 记忆包 -> 技能导出 */
async function skillPreview(){
  var proj=document.getElementById("sk-proj").value;
  if(!proj){alert("先选一个项目");return}
  var r=await api("/api/skill/preview?project="+encodeURIComponent(proj));
  var el=document.getElementById("skill-out");
  el.style.display="block";el.textContent=r.markdown||"（该项目没有可导出的记忆）";
  el.scrollIntoView({behavior:"smooth"});
}
async function skillExport(){
  var proj=document.getElementById("sk-proj").value;
  if(!proj){alert("先选一个项目");return}
  var r=await api("/api/skill/export",{method:"POST",headers:{"Content-Type":"application/json"},
    body:JSON.stringify({project:proj})});
  var el=document.getElementById("skill-out");
  el.style.display="block";
  el.textContent = r.error ? ("导出失败："+r.error) :
    ("已生成技能包："+r.name+"\n路径："+r.path+"\n包含 "+r.count+" 条经验，各 Agent 可直接读取。");
  toast(r.error?"技能包导出失败":"技能包已生成："+r.name, r.error?"err":"ok");
}

/* Agent 体检 + 一键接入（用索引传参，避免 Windows 路径里的反斜杠被 JS 转义吃掉） */
var AGENTS=[];
function msg(text,kind){
  var el=document.getElementById("agent-msg");
  if(!text){el.style.display="none";return}
  el.style.display="block";
  el.className="msg"+(kind?(" "+kind):"");
  el.textContent=text;
}
async function registerAgentAt(i){
  var a=AGENTS[i];if(!a)return;
  msg("正在写入 " + a.name + " 的配置…");
  var r=await api("/api/agent/register",{method:"POST",
    headers:{"Content-Type":"application/json"},
    body:JSON.stringify({name:a.name,config:a.config})});
  if(r.error){msg("写入失败：" + r.error,"err");toast("接入失败：" + r.error,"err")}
  else{msg("已接入 " + a.name + "\n配置文件：" + r.path +
           (r.backup?("\n原文件已备份：" + r.backup):"\n（新建配置文件）") +
           "\n重启 " + a.name + " 后生效","ok");toast("已接入 " + a.name,"ok")}
  loadAgents();
}
async function unregisterAgentAt(i){
  var a=AGENTS[i];if(!a)return;
  if(!confirm("从 " + a.name + " 的配置里移除 Loci？（会先备份原文件）"))return;
  var r=await api("/api/agent/unregister",{method:"POST",
    headers:{"Content-Type":"application/json"},
    body:JSON.stringify({name:a.name,config:a.config})});
  if(r.error){msg("移除失败：" + r.error,"err")}
  else{msg("已移除 " + name + " 的 Loci 条目\n配置文件：" + r.path +
           "\n备份：" + r.backup,"ok");toast("已移除 " + name,"ok")}
  loadAgents();
}
async function registerAll(){
  msg("正在接入全部已安装的 Agent…");
  var r=await api("/api/agent/register-all",{method:"POST",
    headers:{"Content-Type":"application/json"},body:"{}"});
  var lines=[];
  (r.results||[]).forEach(function(x){
    lines.push((x.error?("失败 " + x.name + "：" + x.error):("已接入 " + x.name + " -> " + x.path)));
  });
  if(r.note) lines.push(r.note);
  msg(lines.join("\n"), (r.results||[]).some(function(x){return x.error})?"err":"ok");
  loadAgents();
}
async function verifyMcp(){
  msg("正在启动 MCP 进程做握手验证（约几秒）…");
  var r=await api("/api/agent/verify");
  if(r.ok){
    msg("MCP 服务正常\n服务器：" + r.server + "\n工具：" + r.tools.join(" / ") +
        "\n解释器：" + r.python + "\n脚本：" + r.script,"ok");
  }else{
    msg("MCP 服务异常：" + (r.error||"未知错误"),"err");
  }
}

async function loadArchive(){
  var r=await api("/api/archive");
  window._ARCH=r;
  var cfg=r.cfg||{}, snaps=r.snapshots||[];
  // 按原型：状态拆成 3 格（当前状态 / 最近一份备份 / 库体积），不再挤成一行长文字
  var elSt=document.getElementById("arch-state"), elLa=document.getElementById("arch-last"),
      elSz=document.getElementById("arch-size"), elHi=document.getElementById("arch-hint");
  if(elSt){
    if(!cfg.dir){
      elSt.innerHTML='<span style="color:var(--data-decision)">未启用</span>';
      elLa.textContent="—";
      elSz.textContent=r.db||"—";
      elHi.textContent="为避免占用系统盘，默认不自动备份。请先选择存放位置"
        +"（建议选空间大的盘，如 D 盘或移动硬盘；本机记忆库当前 "+(r.db||"")+"）。";
    }else{
      elSt.innerHTML='<span style="color:var(--data-skill)">已启用</span>'
        +(cfg.auto?'<span style="color:var(--faint);font-weight:400"> · 自动</span>'
                  :'<span style="color:var(--faint);font-weight:400"> · 手动</span>');
      elLa.textContent = snaps.length ? (snaps[0].mtime+"（"+snaps[0].name+"）") : "还没有";
      elSz.textContent = snaps.length
        ? ((r.total/1048576).toFixed(2)+" MB · "+snaps.length+" 份") : (r.db||"—");
      elHi.textContent="保留最近 "+cfg.keep+" 份 ｜ 存档位置："+cfg.dir;
    }
  }
  var el=document.getElementById("cl-psub");
  if(el) el.textContent = cfg.dir
    ? ("存档位置已设置 · "+snaps.length+" 份备份 · 共 "+(r.total/1048576).toFixed(2)+" MB")
    : "破坏性动作按风险从低到高排列 · 每段执行前都会先自动导出一份全量备份";
  document.getElementById("arch-dir").value = cfg.dir||"";
}
async function pickArchiveDir(){
  var r=await api("/api/archive/pick",{method:"POST"});
  if(r.error){toast(r.error,"err");return}
  document.getElementById("arch-dir").value=r.path;
  toast("已选择，点「保存」生效","ok");
}
async function saveArchiveDir(){
  var d=document.getElementById("arch-dir").value.trim();
  var r=await api("/api/archive/config",{method:"POST",headers:{"Content-Type":"application/json"},
    body:JSON.stringify({dir:d})});
  if(r.error){toast(r.error,"err");msg(r.error,"err");return}
  toast(d?"归档已启用":"已关闭自动归档","ok");
  if(d) await doSnapshot();
  loadArchive();
}
async function openArchiveDir(){
  var r=window._ARCH||{};
  if(!r.cfg||!r.cfg.dir){toast("还没设置归档目录","err");return}
  await api("/api/open-folder",{method:"POST",headers:{"Content-Type":"application/json"},
    body:JSON.stringify({path:r.cfg.dir})}).catch(function(){});
}
async function doSnapshot(){
  var r=await api("/api/archive/snapshot",{method:"POST",headers:{"Content-Type":"application/json"},
    body:JSON.stringify({force:true})});
  if(r.error){toast(r.error,"err");msg(r.error,"err");return}
  toast("已生成数据库备份","ok");
  msg("备份完成："+r.path+"\n大小 "+(r.size/1048576).toFixed(2)+" MB ｜ 现有 "+r.count+" 份"+
      (r.removed&&r.removed.length?("\n已按保留份数清理："+r.removed.join("、")):"")+
      "\n\n"+r.note,"ok");
  loadArchive();
}

async function pickFolder(){
  msg("正在弹出系统文件夹选择框…（若无反应，请直接在输入框粘贴路径）");
  var r=await api("/api/agent/pick-folder",{method:"POST"});
  if(r.error){msg(r.error,"err");return}
  document.getElementById("add-path").value=r.path;
  msg("已选择：" + r.path + "\n点「添加」把它加入列表","ok");
}
async function addAgent(){
  var p=document.getElementById("add-path").value.trim();
  if(!p){msg("请先填路径，或点「浏览…」选择","err");return}
  msg("正在扫描该路径下的 MCP 配置…");
  var r=await api("/api/agent/add",{method:"POST",
    headers:{"Content-Type":"application/json"},body:JSON.stringify({path:p})});
  if(r.error){msg("添加失败：" + r.error,"err")}
  else{msg("已添加 " + r.added.length + " 项：\n" +
           r.added.map(function(x){return x.name + "  ->  " + x.config}).join("\n"),"ok")}
  loadAgents();
}
async function forgetAgentAt(i){
  var a=AGENTS[i];if(!a)return;
  if(!confirm("把 " + a.name + " 从列表里移除？（只忘记路径，不改它的配置）"))return;
  await api("/api/agent/forget",{method:"POST",
    headers:{"Content-Type":"application/json"},body:JSON.stringify({path:a.config})});
  msg("已从列表移除 " + a.name,"ok");
  loadAgents();
}

/* ── 交接卡页（2026-10-08 照 proto/交接卡.html 重做渲染）─────────────────
   ⚠️ 引擎层一行没改：syncHfTags() / pickHfProj() / filterHfTags() / doHandoff()
      全部保留原样，这页只换**取数与展示**。契约元素 #hf-proj / #hf-tags /
      #hf-q 收起但**不能删** —— 那几个函数还在写它们，删了会抛异常。

   多选：原型是复选框多选，但后端 handoff(project) 只吃**单个**项目名。
   所以多选导出时逐个取再拼接 —— 这是真拼接，不是假勾选。 */
var HF_ROWS=[], HF_SEL={}, HF_FMT="md", HF_FILTER="", HF_EXPORTED=0;

/* 项目名是时间戳 → 原型说"待确认"。判定照 norm_project 的口径：
   2026-09-21-13-44-31 / 2026-09-21 这种纯日期形态就是目录名，不是人起的名字。 */
function hfIsWarn(name){
  return /^\d{4}-\d{2}-\d{2}(-\d{2}-\d{2}-\d{2})?$/.test(String(name||""));
}
/* 表头全选框 + 按钮文字（2026-10-08，与采集页同一套） */
function hfSyncAll(rows){
  var pool=rows||HF_ROWS;
  var on=pool.filter(function(r){return HF_SEL[r.name];}).length;
  var allOn=pool.length>0 && on===pool.length;
  var el=document.getElementById("hf-all");
  if(el){
    el.classList.remove("on","part");
    if(allOn) el.classList.add("on");
    else if(on>0) el.classList.add("part");
  }
  var btn=document.getElementById("hf-allbtn");
  if(btn) btn.textContent = allOn ? "全不选" : "全选";
}
/* 点表头框或按钮：没全选→全选，已全选→全取消（只作用当前筛选视图） */
function hfToggleAll(ev){
  if(ev && ev.stopPropagation) ev.stopPropagation();
  var rows=HF_FILTER==="warn"?HF_ROWS.filter(function(r){return r.warn;}):HF_ROWS;
  if(!rows.length) return;
  var allOn=rows.every(function(r){return !!HF_SEL[r.name];});
  rows.forEach(function(r){ HF_SEL[r.name]=!allOn; });
  hfRenderRows();
}
function hfRenderRows(){
  var box=document.getElementById("hf-list");
  if(!box) return;
  /* ⚠️ 「有记忆的」= **全部**（19 个都算有记忆），不是"排除待确认"。
     我原来写成 !r.warn，结果默认视图把 8 个时间戳项目藏起来了——
     标签写着「有记忆的」却只显示 11 个，**标签和行为对不上**，
     用户会以为数据丢了。原型这个 chip 就是"不过滤"，
     另一个 chip 才筛待确认。 */
  var rows=HF_FILTER==="warn"?HF_ROWS.filter(function(r){return r.warn;})
                             :HF_ROWS;
  var tot=document.getElementById("hf-total");
  if(tot) tot.textContent=rows.length;
  if(!rows.length){ box.innerHTML='<div class="empty">没有符合条件的项目</div>'; hfSyncAll(rows); return; }
  var keys=Object.keys(HF_SEL).filter(function(k){return HF_SEL[k];});
  box.innerHTML=rows.map(function(r){
    var on=!!HF_SEL[r.name];
    return '<div class="hr'+(on?" on":"")+'" onclick="hfToggle(\''+escAttr(r.name)+'\')"'
      +' title="'+escAttr(r.name)+'">'
      +'<span class="cbx"></span>'
      +'<div class="hn"><b>'+esc(r.name)+'</b>'
        +(r.warn?'<i class="warn">项目名是时间戳，待确认</i>':'')+'</div>'
      +'<div class="num'+(r.mem?"":" zero")+'">'+r.mem+'</div>'
      +'<div class="num'+(r.sess?"":" zero")+'">'+r.sess+'</div>'
      +"</div>";
  }).join("");
  /* 选中条：有选中才显 */
  var pb=document.getElementById("hf-pickbar");
  if(pb) pb.style.display=keys.length?"flex":"none";
  var n=document.getElementById("hf-sel-n"), s=document.getElementById("hf-sel-s");
  var mem=0,sess=0;
  keys.forEach(function(k){var r=HF_ROWS.filter(function(x){return x.name===k;})[0];
    if(r){mem+=r.mem;sess+=r.sess;}});
  if(n) n.textContent="已选 "+keys.length+" 个项目";
  if(s) s.textContent="共 "+mem+" 条记忆 · "+sess+" 个会话";
  hfSyncAll(rows);
}
function hfRenderDist(){
  var box=document.getElementById("hf-dist"), n=document.getElementById("hf-dist-n");
  if(!box) return;
  var warn=HF_ROWS.filter(function(r){return r.warn;});
  var ok=HF_ROWS.filter(function(r){return !r.warn;});
  var rows=[];
  if(warn.length) rows.push({c:"#b45309",nm:"项目名待确认",v:warn.reduce(function(s,r){return s+r.mem;},0),warn:1});
  rows.push({c:"#4338ca",nm:"正常项目名",v:ok.reduce(function(s,r){return s+r.mem;},0)});
  box.innerHTML=rows.map(function(r){
    return '<div class="rt"><span class="sw" style="background:'+r.c+'"></span>'
      +'<span class="nm">'+esc(r.nm)+'</span>'
      +'<span class="vv'+(r.warn?" warn":"")+'">'+r.v+'</span></div>';
  }).join("");
  if(n) n.textContent=HF_ROWS.length+" 个";
}
function hfToggle(name){
  HF_SEL[name]=!HF_SEL[name];
  hfRenderRows();
}
function hfInitFmt(){
  var seg=document.getElementById("hf-fmt");
  if(!seg||seg.dataset.on) return;
  seg.dataset.on="1";
  seg.addEventListener("click",function(e){
    var b=e.target.closest?e.target.closest(".fbtn"):null;
    if(!b) return;
    seg.querySelectorAll(".fbtn").forEach(function(x){x.classList.remove("on")});
    b.classList.add("on");
    HF_FMT=b.dataset.f||"md";
  });
}
function hfInitChips(){
  var box=document.getElementById("hf-chips");
  if(!box||box.dataset.on) return;
  box.dataset.on="1";
  box.addEventListener("click",function(e){
    var c=e.target.closest?e.target.closest(".chip"):null;
    if(!c) return;
    box.querySelectorAll(".chip").forEach(function(x){x.classList.remove("on")});
    c.classList.add("on");
    HF_FILTER=c.dataset.a||"";
    hfRenderRows();
  });
}
async function loadHandoffPage(){
  var st={},sess=[];
  try{ st=await api("/api/stats"); }catch(e){}
  try{ sess=await api("/api/session/list?limit=300"); }catch(e){}
  var bp=st.by_project||{};
  /* 每个项目的会话数：从会话列表里按 project 归。
     ⚠️ 会话的 project 有时是完整路径（ZCode 的），与 by_project 的短名对不上；
        对不上就不计数，**不用猜**——宁可少一格数字，不编一个。 */
  var sessBy={};
  (sess||[]).forEach(function(s){
    var p=String(s.project||"");
    if(!p) return;
    if(bp[p]!=null) sessBy[p]=(sessBy[p]||0)+1;
    else{
      /* 取路径最后一段再试一次（c-Users-...-WorkBuddy-2026-10-07-21-33-42 这类） */
      var tail=p.split(/[\\/]/).pop();
      if(bp[tail]!=null) sessBy[tail]=(sessBy[tail]||0)+1;
    }
  });
  HF_ROWS=Object.keys(bp).map(function(k){
    return {name:k,mem:bp[k]||0,sess:sessBy[k]||0,warn:hfIsWarn(k)};
  }).sort(function(a,b){return b.mem-a.mem;});

  hfRenderRows(); hfRenderDist(); hfInitFmt(); hfInitChips();
  syncHfTags();   /* 契约元素照旧填，只是隐藏了 */

  var warnN=HF_ROWS.filter(function(r){return r.warn;}).length;
  var ps=document.getElementById("hf-psub");
  if(ps) ps.innerHTML="<b>"+HF_ROWS.length+"</b> 个项目 · <b>"+(st.total||0)+"</b> 条记忆 · 导出给另一个 Agent 读";
  var setK=function(id,v){var e=document.getElementById(id);
    if(e){if(window.countUp)countUp(e,v);else e.textContent=v}};
  setK("hk-proj",HF_ROWS.length); setK("hk-mem",st.total||0);
  setK("hk-sess",(sess||[]).length); setK("hk-warn",warnN);
  setK("hk-exp",HF_EXPORTED);
}
/* 多选导出：后端只吃单个项目，逐个取再拼接（真拼接） */
async function hfExport(){
  var keys=Object.keys(HF_SEL).filter(function(k){return HF_SEL[k];});
  if(!keys.length){ toast("先选至少一个项目","err"); return; }
  var md="",json=null;
  for(var i=0;i<keys.length;i++){
    var r=await api("/api/handoff?project="+encodeURIComponent(keys[i]));
    if(i) md+="\n\n---\n\n";
    md+=(r.markdown||"");
    if(r.json){ if(!json) json={}; json[keys[i]]=r.json; }
  }
  var body=(HF_FMT==="md")?md
          :(HF_FMT==="json")?JSON.stringify(json||{},null,2)
          :md+"\n\n<!-- JSON -->\n\n```json\n"+JSON.stringify(json||{},null,2)+"\n```";
  var el=document.getElementById("handoff");
  el.style.display="block";
  el.innerHTML=esc(body);
  var card=document.getElementById("hf-card");
  if(card) card.style.display="block";
  var meta=document.getElementById("hf-meta");
  if(meta) meta.textContent=keys.length+" 个项目 · "+body.length+" 字符 · "+HF_FMT.toUpperCase();
  HF_EXPORTED++;
  var e=document.getElementById("hk-exp");
  if(e) e.textContent=HF_EXPORTED;
  toast("已生成交接卡（"+keys.length+" 个项目）","ok");
}

/* Agent 页（2026-10-08 照proto/Agent.html 重做渲染）
   ⚠️ 原来渲染的是 .acard 卡片网格（旧工作台那套），原型是 **4 列表格**。
      六态判定逻辑（已接入/路径失效/未接入/不支持写入/残留/未安装）**全部保留** ——
      那是踩过"假绿"事故换来的判据，只换外观不换语义。
   ⚠️ 原型右栏第二个框「数据可读性（可读/已加密）」做不了：
      /api/agents 没有"是否加密"字段（实测确认），硬画就是编数据。 */
var AG_FILTER="";
/* 图标底色：按 Agent 名取固定色，同一家always 同色 —— 照原型是内联 style，这里集中一处 */
function agColor(name){
  var s=String(name||"").toLowerCase();
  if(s.indexOf("workbuddy")>=0)return "#4338ca";
  if(s.indexOf("zcode")>=0)return "#0f766e";
  if(s.indexOf("qoder")>=0)return "#b45309";
  if(s.indexOf("traework")>=0)return "#7c3aed";
  if(s.indexOf("trae")>=0)return "#a78bfa";
  if(s.indexOf("codex")>=0||s.indexOf("codebuddy")>=0)return "#857f74";
  return "#bbb7ae";   /* 未安装的一律中性灰 —— 原型就是这个色 */
}
function agInitial(name){
  var s=String(name||"").trim();
  return s?s[0].toUpperCase():"?";
}
function agState(a){
  /* 六态 → 原型的两态展示 + title 说明（保留全部判据） */
  var on=a.loci_registered&&a.paths_ok;
  if(a.state==="residue")return {cls:"no",txt:"已卸载 · 有残留",tip:"Agent 已卸载，但配置文件里还留着 Loci 的条目"};
  if(!a.installed)     return {cls:"no",txt:"未安装",tip:"本机没检测到这个 Agent"};
  if(on)               return {cls:"yes",txt:"已接入",tip:"MCP 配置已写入，且启动路径有效"};
  if(a.loci_registered)return {cls:"no",txt:"路径失效",tip:"已写入配置，但指向的 loci.py 路径不存在 —— 点「一键接入」修复"};
  if(!a.writable)      return {cls:"no",txt:"需手动配",tip:"已安装，但这个 Agent 的配置格式暂不支持自动改写"};
  return {cls:"no",txt:"未接入",tip:"已安装，还没接入 Loci"};
}
function agBtns(a,i){
  var b=[];
  if(a.installed&&a.writable){
    b.push(a.loci_registered
      ?'<button class="btn" onclick="unregisterAgentAt('+i+')">移除接入</button>'
      :'<button class="btn" onclick="registerAgentAt('+i+')">一键接入</button>');
  }
  if(a.source==="manual"||a.source==="discovered"){
    b.push('<button class="btn" onclick="forgetAgentAt('+i+')">移出列表</button>');
  }
  return b.join("");
}
function renderAgentRows(){
  var box=document.getElementById("ag-list");
  if(!box) return;
  var rows=AG_FILTER===""?AGENTS
    :(AG_FILTER==="1"?AGENTS.filter(function(a){return a.installed})
                   :AGENTS.filter(function(a){return !a.installed}));
  var tot=document.getElementById("ag-total");
  if(tot) tot.textContent=rows.length;
  if(!rows.length){ box.innerHTML='<div class="empty">没有符合条件的 Agent</div>'; return; }
  box.innerHTML=rows.map(function(a){
    var st=agState(a);
    var mn=AG_SESS[a.name]||0;
    var i=AGENTS.indexOf(a);
    return '<div class="arow">'
      /* ⚠️ 2026-10-08 用户指出「小字是 AI 通病，要改掉」——
         这里原来在 Agent 名下面挂了一整条配置**全路径**的小灰字
         （Windows 下那条 AppData 全路径一路挂到屏幕边）。
         路径已经放在行的 title 里（鼠标悬停可见），**小字删掉**，
         名字单独一行。 */
      +'<div class="an"><span class="ic" style="background:'+agColor(a.name)+'">'+esc(agInitial(a.name))+'</span>'
        +'<span class="nm"><b>'+esc(a.name)+'</b></span></div>'
      +'<div><span class="dot '+st.cls+'" title="'+escAttr(st.tip)+'">'+st.txt+'</span></div>'
      +'<div><div class="cnt'+(mn?"":" zero")+'">'+mn+'</div></div>'
      +'<div class="act">'+agBtns(a,i)+'</div>'
      +"</div>";
  }).join("");
}
function renderAgentSide(){
  var box=document.getElementById("ag-side"), n=document.getElementById("ag-side-n");
  if(!box) return;
  var keys=Object.keys(AG_SESS).filter(function(k){return AG_SESS[k]>0;});
  keys.sort(function(a,b){return AG_SESS[b]-AG_SESS[a];});
  var tot=keys.reduce(function(s,k){return s+AG_SESS[k];},0);
  box.innerHTML=keys.length?keys.map(function(k){
    return '<div class="rt"><span class="sw" style="background:'+agColor(k)+'"></span>'
      +'<span class="nm">'+esc(k)+'</span>'
      +'<span class="vv">'+AG_SESS[k]+'</span></div>';
  }).join(""):'<div class="hintline">还没有会话入库。</div>';
  if(n) n.textContent=tot+" 条";
  /* 第二框「数据可读性」—— 原型有，我先前漏了（结构对账量到 2 vs 1）。
     可读 / 加密**从会话的 msg_count 推导**，不是编的：
       msg_count>0 → 拿到了原文（可读）
       msg_count==0 → 没原文（加密 / 读不到）*/
  var rb=document.getElementById("ag-read"), rn=document.getElementById("ag-read-n");
  if(rb){
    /* 照 proto/Agent.html：**逐 Agent 一行**，每行末尾写「可读 / 读不到」。
       ⚠️ 2026-10-08 用户指出「小字是 AI 通病，要改掉」——
          原来这里我加了一段灰色小字解释口径，**删掉了**。
          原型这个框里没有任何小字，只有一行行的状态。 */
    var order=Object.keys(AG_SESS).filter(function(k){return AG_SESS[k]>0;});
    order.sort(function(a,b){return AG_SESS[b]-AG_SESS[a];});
    rb.innerHTML=order.length?order.map(function(k){
      var okN=AG_READ[k]||0, badN=(AG_SESS[k]||0)-okN;
      return '<div class="rt"><span class="sw" style="background:'
        +(badN?"var(--lz-coral)":"var(--lz-ok)")+'"></span>'
        +'<span class="nm">'+esc(k)+'</span>'
        +'<span class="vv'+(badN?" warn":"")+'">'+(okN?"可读":"读不到")+'</span></div>';
    }).join(""):'<div class="hintline">还没有会话入库。</div>';
  }
  if(rn) rn.textContent=AG_SESS_OK+" / "+(AG_SESS_OK+AG_SESS_LOCK);
}
var AG_SESS={}, AG_BY_AGENT={}, AG_SESS_OK=0, AG_SESS_LOCK=0, AG_READ={};
async function loadAgents(){
  var rows=await api("/api/agents");
  AGENTS=rows;
  loadScanRoots();
  /* 「已入库会话」按 Agent 名从会话列表统计 —— /api/stats 的 by_agent
     统计的是**记忆来源**，不是会话来源，两码事，别混用（实测 by_agent 里有
     panel/collector 这种内部来源，不是 Agent）。 */
  AG_SESS={};
  try{
    var ss=await api("/api/session/list?limit=200");
    AG_SESS_OK=0; AG_SESS_LOCK=0; AG_READ={};
    (ss||[]).forEach(function(s){
      var k=agentName(s.agent);
      if(k) AG_SESS[k]=(AG_SESS[k]||0)+1;
      /* 可读性：有 msg_count 就是拿到了原文；0 就是拿不到（加密） */
      if((s.msg_count||0)>0){ AG_SESS_OK++; if(k) AG_READ[k]=(AG_READ[k]||0)+1; }
      else { AG_SESS_LOCK++; }
    });
  }catch(e){}
  /* 「已抽记忆」= 记忆按来源 Agent 分（/api/stats 的 by_agent）。
     注意：by_agent 里混着 panel / collector 这种**内部来源**，那不是 Agent，
     所以只按本页 Agent 名单对得上键取数，其余不计入 —— 不拿会话数顶替。 */
  AG_BY_AGENT={};
  try{
    var _st=await api("/api/stats");
    var names={};
    rows.forEach(function(a){ names[agentName(a.name)]=1; });
    var ba=_st.by_agent||{};
    Object.keys(ba).forEach(function(k){
      if(names[k]) AG_BY_AGENT[k]=ba[k];
    });
  }catch(e){}
  renderAgentRows();
  renderAgentSide();
  initAgentChips();
  /* 页头数据行 + 5 个大数字（全部现取，不编） */
  var nAll=rows.length;
  var nOn=rows.filter(function(a){return a.loci_registered&&a.paths_ok}).length;
  var nOff=rows.filter(function(a){return !a.installed}).length;
  var nInst=nAll-nOff;                /* 已装 = 全部 - 未装 */
  var nSess=Object.keys(AG_SESS).reduce(function(s,k){return s+AG_SESS[k];},0);
  /* 「已抽记忆」按来源 Agent 分（/api/stats 的 by_agent）。
     ⚠️ 这里**不能**拿会话数顶替 —— by_agent 统计的是记忆来源，不是会话来源。
     实测by_agent 里混着 panel / collector 这种内部来源，那不是 Agent，
     所以只取与本页Agent 名单**对得上**的键，其余不计入。 */
  var nMem=0;
  try{
    var _st=AG_BY_AGENT||{};
    Object.keys(AG_SESS).forEach(function(k){ nMem+=(_st[k]||0); });
  }catch(e){}
  var elPs=document.getElementById("ag-psub");
  if(elPs)elPs.innerHTML="<b>"+nAll+"</b> 个已知 · <b>"+nInst+"</b> 个已装 · 已接入 <b>"+nOn+"</b> 个";
  var setK=function(id,v){var e=document.getElementById(id);
    if(e){if(window.countUp)countUp(e,v);else e.textContent=v}};
  setK("agpi-on",nInst); setK("agpi-off",nOff); setK("agpi-sess",nSess);
  setK("agpi-reg",nOn); setK("agpi-mem",nMem);
}
function initAgentChips(){
  var box=document.getElementById("ag-chips");
  if(!box||box.dataset.on) return;
  box.dataset.on="1";
  box.addEventListener("click",function(e){
    var c=e.target.closest?e.target.closest(".chip"):null;
    if(!c) return;
    box.querySelectorAll(".chip").forEach(function(x){x.classList.remove("on")});
    c.classList.add("on");
    AG_FILTER=c.dataset.a||"";
    renderAgentRows();
  });
}

function doExport(){
  const proj=document.getElementById("pk-proj").value;
  const _wsEl=document.getElementById("pack-with-sessions");
  const _ws=(_wsEl&&_wsEl.checked)?"1":"0";
  const url="/api/pack/export?include_sessions="+_ws+(proj?("&project="+encodeURIComponent(proj)):"");
  const a=document.createElement("a");a.href=url;a.click();
}

/* 导入的核心逻辑抽出来 —— 「选文件」和「拖进来」走同一条路，不写两遍 */
async function doImportFile(f){
  if(!f)return;
  try{
    const pack=JSON.parse(await f.text());
    const r=await api("/api/pack/import",{method:"POST",
      headers:{"Content-Type":"application/json"},body:JSON.stringify(pack)});
    if(r.error){alert(r.error)}
    else{alert(`导入完成：新增 ${r.imported} 条，跳过重复 ${r.skipped} 条`);refresh()}
  }catch(e){alert("文件不是有效的 JSON 记忆包")}
}
async function doImport(inp){
  const f=inp.files[0];if(!f)return;
  await doImportFile(f);
  inp.value="";
}
/* 拖拽区：点一下 = 打开选文件；拖进来 = 直接导入。
   ⚠️ dragover / dragenter 必须 preventDefault，否则浏览器会直接打开这个文件。 */

/* A 技能/MCP 页：4 张 KPI 卡可点 → 跳到对应分组。
   2026-09-27 用户提的需求，并明确"别只显示那个分组，会引入特别多的 bug"——
   所以这里只做**滚动 + 高亮**，页面信息量不变。
   ⚠️ 用**索引**找分组（#sk-list 的 .ghead 顺序），不依赖内部 key，页面结构变了也不会崩。
   高亮用 background 动画（**不用 transform** —— 本项目有一批量几何的闸门，
   transform 会临时改变 getBoundingClientRect，动画进行中量到偏移就假报错）。 */
function skJump(i){
  var hs=[].slice.call(document.querySelectorAll("#sk-list .ghead"));
  var h=hs[i]; if(!h)return;
  var body=h.nextElementSibling;
  if(body&&body.classList.contains("hide"))h.click();   // 收起状态才点开
  setTimeout(function(){
    h.scrollIntoView({behavior:"smooth",block:"center"});
    var box=h.closest(".panel")||h.parentElement;
    box.classList.add("skflash");
    setTimeout(function(){box.classList.remove("skflash")},1200);
  },90);
}
function bindSkKpi(){
  var cards=[].slice.call(document.querySelectorAll("#sk-kpi .kpi"));
  cards.forEach(function(c,i){
    var name=((c.querySelector(".kt")||{}).textContent||"").trim();
    c.style.cursor="pointer";
    c.title="点击跳到「"+name+"」分组";
    c.onclick=function(){skJump(i)};
  });
}
/* ② 下拉默认选中第一个**有值**的选项。
   2026-09-27 用户报"预览技能包出不来"——实测功能正常，是默认停在空的
   「选择项目」上，点预览只弹一句"先选一个项目"，用户以为坏了。
   ⚠️ 只在用户**没选过**（value 为空）时才自动选，不覆盖用户的选择。 */
function defaultSelectFirst(id){
  var s=document.getElementById(id); if(!s)return;
  if(s.value)return;
  for(var i=0;i<s.options.length;i++){
    if(s.options[i].value){ s.value=s.options[i].value; break; }
  }
}
/* 记忆包页的两个统计 chip（数据来自 /api/stats，不写死） */
async function syncPackInfo(){
  try{
    var st=await api("/api/stats");
    var a=document.getElementById("pk-count");
    if(a)a.textContent="全部 "+st.total+" 条 · "+Object.keys(st.by_project||{}).length+" 个项目";
    var b=document.getElementById("pk-sess");
    if(b)b.textContent=st.sessions+" 个会话 · "+st.messages.toLocaleString()+" 轮";
  }catch(e){}
}

document.getElementById("f-imp").addEventListener("click",function(){
  let v=+this.dataset.v%4+1;this.dataset.v=v;
  this.innerHTML="<b>"+"★".repeat(v)+"</b>"+"★".repeat(4-v);
});

function refresh(){loadStats();loadAgents();syncPackInfo();syncHfTags();searching?doSearch():loadList()}
refresh();
</script>
</body>
</html>
"""


SELFTEST = """<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="utf-8"><title>Loci 诊断页</title>
<style>body{background:#1c1c1e;color:#f5f5f7;font-family:-apple-system,"PingFang SC","Microsoft YaHei",sans-serif;
padding:28px 30px;max-width:860px;margin:0 auto}
h1{font-size:24px;margin-bottom:6px}
.warn{background:#3a3a3c;border:1px solid rgba(255,255,255,.12);border-radius:8px;
padding:12px 16px;font-size:13px;color:#f5f5f7;margin:14px 0 18px}
a{color:#0a84ff;text-decoration:none}a:hover{text-decoration:underline}
h2{font-size:13px;color:#8e8e93;font-weight:500;margin:22px 0 8px}
pre{background:#111;border:1px solid rgba(255,255,255,.08);border-radius:8px;padding:16px;
white-space:pre-wrap;font-family:Consolas,monospace;font-size:12.5px;line-height:1.75}
iframe{width:100%;height:460px;border:1px solid rgba(255,255,255,.12);border-radius:8px;margin-top:8px}
.foot{color:#636366;font-size:12px;margin-top:26px}</style>
</head><body>
<h1>诊断页</h1>
<div class="warn">这不是工作台。工作台请访问 <a href="/">http://127.0.0.1:8787</a> —— 本页仅供排查问题时使用。</div>
<h2>自检结果</h2>
<pre id="out">running...</pre>
<h2>工作台预览（下方为真实面板）</h2>
<iframe id="f" src="/"></iframe>
<div class="foot">排查完可直接关闭本页。</div>
<script>
var lines=[];
function log(s){lines.push(s);document.getElementById("out").textContent=lines.join("\\n");}
log("ua="+navigator.userAgent);
try{ eval("var _t=async function(){return 1};"); log("async_syntax=ok"); }catch(e){ log("async_syntax=FAIL "+e.message); }
var f=document.getElementById("f");
f.onload=function(){
  var d,w;
  try{ d=f.contentDocument; w=f.contentWindow; }catch(e){ log("iframe_access_error="+e.message); return; }
  try{
    log("iframe_loaded=1");
    log("doScan_type="+typeof w.doScan);
    log("show_type="+typeof w.show);
    log("collect_hidden_before="+d.getElementById("v-collect").hasAttribute("hidden"));
    var nav=d.querySelector('[data-v="collect"]');
    log("nav_found="+(nav?1:0));
    if(nav){ nav.click(); }
    log("after_click_collect_hidden="+d.getElementById("v-collect").hasAttribute("hidden"));
    log("after_click_mem_hidden="+d.getElementById("v-mem").hasAttribute("hidden"));
    var x=new XMLHttpRequest(); x.open("GET","/api/stats",false); x.send();
    log("sync_xhr_stats="+x.status+" len="+x.responseText.length);
    var x2=new XMLHttpRequest(); x2.open("GET","/",false); x2.send();
    var html=x2.responseText;
    var m=html.match(/<script>([\\s\\S]*?)<\\/script>/);
    if(m){
      try{ new Function(m[1]); log("script_syntax=OK"); }
      catch(err){ log("SCRIPT_SYNTAX_ERROR="+err.message); }
    }
  }catch(e){ log("EXCEPTION="+e.message); }
  var tries=0;
  (function poll(){
    tries++;
    var s=d.getElementById("stats").innerHTML.length;
    var l=d.getElementById("list").innerHTML.length;
    var a=d.getElementById("agents").innerHTML.length;
    log("poll"+tries+": stats="+s+" list="+l+" agents="+a);
    if(tries<8 && (s===0||l===0)){ setTimeout(poll,400); }
    else { log("FINAL "+(s>0&&l>0?"data_render_OK":"data_render_EMPTY")); log("done"); }
  })();
};
</script></body></html>
"""


class Handler(BaseHTTPRequestHandler):
    # ── 响应出口（唯一一处真正往 socket 写字节的地方）──
    # 2026-09-23 用户报的日志噪声：浏览器**刷新 / 切走标签 / 关页面**时，正好赶上服务端
    # 在写响应，Windows 会抛 ConnectionAbortedError [WinError 10053]（对端中止了连接）。
    # 这不是故障 —— 但默认没人接，socketserver 会把整个 traceback 打到 stderr：
    #       panel.py:4885 do_GET → panel.py:4783 _json → panel.py:4780 _send
    #   → 功能不受影响，可日志脏到"看着像面板崩了"，真出问题时会埋在这堆噪声里。
    # 所以这里只吞**"对端没了"这一类**（ConnectionError 覆盖 WinError 10053/10054/32）；
    # 其它异常照常往外抛 —— 别把真 bug 一起吞掉。
    # 想排查连接问题时设 LOCI_LOG_CONN=1，会打一行短提示。
    _conn_drop = 0

    def _note_conn_drop(self):
        Handler._conn_drop += 1
        if os.environ.get("LOCI_LOG_CONN"):
            sys.stderr.write(
                "[panel] 客户端提前断开（第 %d 次，已忽略）\n" % Handler._conn_drop)

    def _raw(self, code, headers, data):
        """写完整响应：状态行 + 头 + 体。对端提前断开时静默（见上面说明）。"""
        try:
            self.send_response(code)
            for k, v in headers:
                self.send_header(k, v)
            self.end_headers()
            self.wfile.write(data)
        except ConnectionError:
            self._note_conn_drop()

    def handle(self):
        """最外层兜底：把「对端提前断开」挡在 socketserver 之前。

        为什么 _raw 里已经 try 了还要来这一层 —— 实测（tools/verify_conn_drop.py）发现
        异常不止从 wfile.write 出来：stdlib 的 handle_one_request() **末尾还有一次
        self.wfile.flush()**（把缓冲真正推出去），那一下撞上断连会抛
        ConnectionResetError [WinError 10054]，完全不在 _raw 的作用域里。
        断连时"回不了话"是必然的，吞掉它，别让它变成一屏 traceback。"""
        try:
            super().handle()
        except ConnectionError:
            self._note_conn_drop()

    def _send(self, code, body, ctype="application/json; charset=utf-8"):
        data = body.encode("utf-8") if isinstance(body, str) else body
        self._raw(code, [
            ("Content-Type", ctype),
            ("Cache-Control", "no-store"),
            ("Content-Length", str(len(data))),
        ], data)

    def _json(self, obj, status=200):
        # 2026-10-05 加 status：save_memory 的排除清单命中是**正常业务判定**
        #   （用户输入被判为无信息量），不是服务故障。以前只能回 200，调用方
        #   分不清"存了"和"没存"，或者靠抛异常落到兜底里变成 500。
        self._send(status, json.dumps(obj, ensure_ascii=False))

    # ── CSRF 守卫（2026-09-24 加）─────────────────────────────────────────
    # 以前 do_GET / do_POST 只要路径匹配就执行，**完全不看请求从哪来**。
    # 实测（真 Chrome，从一个别的源 8800 打开页面）：那个页面能静默写库 ——
    #   攻击前 118 条 → 攻击后 119 条，多出一条 agent=panel 的伪造记忆。
    # 比删库更阴险的是**污染上下文**：假记忆会被 4 个 Agent 当事实读走。
    #
    # 判据（不依赖任何客户端可控的内容）：
    #   · Origin / Referer 指向本面板自己（127.0.0.1 / localhost / ::1 + 同一端口）→ 放行
    #   · 带了 Origin/Referer 但不是自己 → 403，一个动作都不执行
    #   · 两者都没带（curl、tools/verify_*.py、MCP 直连）→ 放行
    #     浏览器发 POST 一定带 Origin，所以"没带 = 不是浏览器发起的跨站请求"。
    #
    # 为什么不用"强制 Content-Type: application/json"（社区通行做法）——
    # 实测不可靠：连跑 3 次全被拦，但中间出现过 1 次照样写进去了。
    # 这个不确定性写在 docs/ 的交接包里，留给专家定位。
    @staticmethod
    def _origin_allowed(v):
        """v 为 Origin / Referer 原串。返回 True=自己人 False=外人 None=没带。"""
        if not v:
            return None
        try:
            p = urlparse(v)
        except Exception:
            return False
        if p.scheme not in ("http", "https"):
            # 含 Origin: null（沙箱 iframe / file:// 页面）—— 一律当外人
            return False
        if (p.hostname or "").lower() not in ("127.0.0.1", "localhost", "::1"):
            return False
        if p.port:
            bound = _BOUND_PORT[0]
            if bound and p.port != bound:
                return False
        return True

    def _csrf_guard(self):
        """放行返回 True；拦下则已回 403 并返回 False。"""
        o = self._origin_allowed(self.headers.get("Origin"))
        r = self._origin_allowed(self.headers.get("Referer"))
        if o is False or r is False:
            _orig = self.headers.get("Origin") or self.headers.get("Referer") or ""
            self._send(403, json.dumps({
                "error": "来源不被信任：面板只接受本机自身的请求",
                "detail": "来自 %s 的跨站请求已被拒绝（CSRF 防护）" % (_orig[:120]),
            }, ensure_ascii=False))
            return False
        return True

    def do_GET(self):
        """入口包装：**参数解析失败一律回 400，而不是掐断连接**（2026-10-01 修，P2-3）。

        以前 `int(q.get(...))` 是裸调用，畸形 query（如 `?limit=abc`）抛出的 ValueError
        会冒到 BaseHTTPRequestHandler 之外 → 连接被直接关闭、一个字节都不回。
        前端 fetch 只拿到网络错误（RemoteDisconnected），既看不到"参数不合法"，
        也没法和服务端故障区分开 —— 对写接口尤其难排查。
        """
        try:
            self._handle_get()
        except (ValueError, KeyError, TypeError) as e:
            self._reply_error(400, "参数不合法：%s" % e)
        except Exception as e:                                    # noqa: BLE001
            print("[loci] GET %s 处理出错：%s" % (self.path, e), file=sys.stderr)
            self._reply_error(500, "服务端错误：%s" % e)

    def _reply_error(self, code, msg):
        """出错统一回 JSON。已经发过响应就安静跳过（避免二次发送再抛异常）。"""
        try:
            self._send(code, json.dumps({"error": msg}, ensure_ascii=False))
        except Exception:
            pass      # 容错：响应可能已经发出去了，二次发送必失败 —— 这里没有更好的补救手段

    def _handle_get(self):
        u = urlparse(self.path)
        # /api/ 下不全是只读的（/api/autoscan 会抽取落库、/api/open-folder 会开资源管理器），
        # 所以整段按来源校验。页面本身（/ 与 /selftest）不校验 —— 顶层导航不带 Origin，
        # 从聊天软件里点链接进面板不该被自己拦掉。
        if u.path.startswith("/api/") and not self._csrf_guard():
            return
        _touch()
        q = parse_qs(u.query)
        if u.path in ("/", "/index.html"):
            # 版本号只有一处来源（loci.APP_VERSION），页脚里是 __APP_VERSION__ 占位符。
            # 单次 str.replace 的开销可以忽略，换来"发版只改一行"。
            self._send(200, PAGE.replace("__APP_VERSION__", hippo.APP_VERSION),
                       "text/html; charset=utf-8")
        elif u.path == "/selftest":
            self._send(200, SELFTEST, "text/html; charset=utf-8")
        elif u.path == "/api/stats":
            _st = hippo.stats()
            if _STORE_WARN:              # agents.json 等配置损坏的警告，透出到面板
                _st["warn"] = _STORE_WARN
            self._json(_st)
        elif u.path == "/api/list":
            rows = hippo.list_memories(
                project=q.get("project", [None])[0] or None,
                mtype=q.get("type", [None])[0] or None,
                limit=int(q.get("limit", [50])[0]))
            self._json(hippo.with_session_title(rows))
        elif u.path == "/api/search":
            rs = hippo.search_memory(q.get("q", [""])[0], limit=int(q.get("limit", [10])[0]))
            self._json(hippo.with_session_title([{**dict(r), "score": s} for s, r in rs]))
        elif u.path == "/api/handoff":
            self._send(200, json.dumps(
                {"markdown": hippo.handoff(q.get("project", [""])[0])}, ensure_ascii=False))
        elif u.path == "/api/context":
            self._send(200, json.dumps(
                {"markdown": hippo.context_pack(q.get("project", [None])[0] or None)},
                ensure_ascii=False))
        elif u.path == "/api/audit":
            self._json(hippo.quality_scan(q.get("project", [None])[0] or None))
        elif u.path == "/api/health":
            self._json(hippo.health_score(q.get("project", [None])[0] or None))
        elif u.path == "/api/audit/report":
            self._send(200, json.dumps(
                {"markdown": hippo.audit_report(q.get("project", [None])[0] or None)},
                ensure_ascii=False))
        elif u.path == "/api/extract":
            sid = q.get("sid", [None])[0]
            self._json(hippo.extract_candidates(
                session_id=int(sid) if sid else None,
                project=q.get("project", [None])[0] or None))
        elif u.path == "/api/skill/preview":
            self._send(200, json.dumps(
                {"markdown": hippo.skill_markdown(q.get("project", [""])[0])},
                ensure_ascii=False))
        elif u.path == "/api/sourcefiles":
            self._json(scan_source_files())
        elif u.path == "/api/archive":
            cfg = hippo.load_archive_cfg()
            snaps = hippo.list_snapshots()
            self._json({"cfg": cfg, "snapshots": snaps, "count": len(snaps),
                        "total": sum(s["size"] for s in snaps),
                        "db": hippo.DB_PATH})
        # ⚠️ 2026-09-29：/api/open-folder 的 GET 路由已删 —— 它会弹资源管理器（副作用），
        #   只保留 do_POST 版本（前端本来就走 POST，见 5055 / 5993 行）。
        # ⚠️ 同理：/api/archive/pick 与 /api/agent/pick-folder 也已移到 do_POST ——
        #   它们会弹 tkinter 模态文件夹选择框（timeout 300s），可被 <img src> 反复触发。
        elif u.path == "/api/backups":
            self._json(list_backups())
        elif u.path == "/api/ping":
            _touch()
            self._json({"ok": True, "idle_exit_sec": IDLE_EXIT_SEC})
        elif u.path in ("/icon.png", "/icon-blue.png"):
            """icon.png     深蓝底原图（favicon / README）
               icon-blue.png 品牌蓝底版（左侧 logo 块），由 tools/make_icon_blue.py 生成"""
            _ico = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                "assets", os.path.basename(u.path))
            if os.path.isfile(_ico):
                with open(_ico, "rb") as _f:
                    _d = _f.read()
                self._raw(200, [
                    ("Content-Type", "image/png"),
                    ("Content-Length", str(len(_d))),
                    ("Cache-Control", "no-store"),
                ], _d)
            else:
                self.send_error(404)
        elif u.path == "/api/scan-roots":
            self._json({"roots": load_scan_roots()})
        elif u.path == "/api/orphans":
            self._json(hippo.find_orphans())
        elif u.path == "/api/cleanup/preview":
            self._json(cleanup_preview(
                q.get("project", [None])[0] or None,
                q.get("agent", [None])[0] or None,
                q.get("before", [None])[0] or None,
                q.get("superseded", ["0"])[0] in ("1", "true"),
                int(q.get("limit", ["20"])[0])))
        # ⚠️ 2026-09-29：/api/autoscan 已移到 do_POST —— 它会抽取落库（有副作用），
        #   而跨站 <img src>/<script src> 只能发 GET，改成 POST 才能从根上断掉这条路。
        elif u.path == "/api/conv-sources":
            # 本机有哪些 Agent 真的存了对话 —— 现场探测磁盘，不返回写死名单
            self._json({"sources": hippo.detect_conversation_sources(),
                        "suggest": hippo.suggest_conversation_sources()})
        elif u.path == "/api/session/list":
            rows = hippo.list_sessions(limit=int(q.get("limit", [50])[0]),
                                       project=q.get("project", [None])[0] or None)
            self._json([dict(r) for r in rows])
        elif u.path == "/api/session/get":
            s, msgs = hippo.get_session(int(q.get("sid", ["0"])[0]))
            if not s:
                return self._json({"error": "会话不存在"})
            # 顺手带上这段会话产出的记忆 —— 右栏的时间线和"产出记忆"是一体的，
            # 分两次请求会出现"时间线已经换了、记忆还是上一段的"错位。
            mems = hippo.memories_of_session(int(q.get("sid", ["0"])[0]))
            self._json({"session": dict(s), "messages": [dict(m) for m in msgs],
                        "memories": [dict(m) for m in mems]})
        elif u.path == "/api/session/search":
            rs = hippo.search_messages(q.get("q", [""])[0],
                                       limit=int(q.get("limit", [20])[0]),
                                       project=q.get("project", [None])[0] or None)
            self._json([{**dict(r), "score": s} for s, r in rs])
        elif u.path == "/api/agents":
            self._json(scan_agents())
        elif u.path == "/api/scan":
            self._json(scan_sources())
        elif u.path == "/api/skills":
            # 本机已有 skill：跨 Agent、跨作用域。来源与目标都是探出来的。
            self._json({"skills": hippo.list_local_skills(),
                        "sources": hippo.skill_sources(),
                        "targets": hippo.skill_copy_targets()})
        elif u.path == "/api/skill/detail":
            self._json(hippo.local_skill_detail(q.get("path", [""])[0]))
        elif u.path == "/api/content":
            # 配置文件 + MCP。路径只填本机实测过的（见 loci.CONTENT_SOURCES）；
            # 敏感文件（credentials 之类）只回存在与键名，值一律不回。
            cfgs = hippo.list_configs()
            self._json({"configs": cfgs,
                        "mcps": hippo.list_mcp_servers(),
                        "plugins": hippo.list_plugins(),
                        "backups": hippo.content_backups(cfgs),
                        "sources": hippo.content_sources()})
        elif u.path == "/api/content/detail":
            self._json(hippo.content_detail(q.get("path", [""])[0]))
        elif u.path == "/api/plugin/versions":
            self._json(hippo.plugin_versions(q.get("agent", [""])[0],
                                             q.get("path", [""])[0]))
        elif u.path == "/api/backup/detail":
            self._json(hippo.backup_detail(q.get("path", [""])[0]))
        elif u.path == "/api/skill/plan":
            self._json(hippo.plan_skill_copy(q.get("src", [""])[0], q.get("target", [""])[0]))
        elif u.path == "/api/agent/verify":
            self._json(verify_mcp())
        elif u.path == "/api/pack/export":
            proj = q.get("project", [None])[0] or None
            _ws = (q.get("include_sessions", ["0"])[0] or "0") not in ("0", "", "false")
            pack = export_pack(proj, include_sessions=_ws)
            body = json.dumps(pack, ensure_ascii=False, indent=2).encode("utf-8")
            fname = "loci-pack.json"
            if proj:
                fname = f"loci-pack-{quote(proj)}.json"
            self._raw(200, [
                ("Content-Type", "application/json; charset=utf-8"),
                ("Cache-Control", "no-store"),
                ("Content-Disposition",
                 f"attachment; filename*=UTF-8''{quote(fname)}"),
                ("Content-Length", str(len(body))),
            ], body)
        else:
            self._send(404, '{"error":"not found"}')

    def do_POST(self):
        """入口包装：同 do_GET —— 参数畸形回 400，不掐断连接（2026-10-01 修，P2-3）。"""
        try:
            self._handle_post()
        except (ValueError, KeyError, TypeError) as e:
            self._reply_error(400, "参数不合法：%s" % e)
        except Exception as e:                                    # noqa: BLE001
            print("[loci] POST %s 处理出错：%s" % (self.path, e), file=sys.stderr)
            self._reply_error(500, "服务端错误：%s" % e)

    def _handle_post(self):
        # 所有写操作都从这里进 —— 先过 CSRF 守卫，外人一个字节都别想落库。
        if not self._csrf_guard():
            return
        _touch()
        u = urlparse(self.path)
        q = parse_qs(u.query)
        length = int(self.headers.get("Content-Length", 0))
        try:
            body = json.loads(self.rfile.read(length).decode("utf-8") or "{}")
        except Exception:
            return self._send(400, '{"error":"bad json"}')
        if u.path == "/api/save":
            if not body.get("content", "").strip():
                return self._send(400, '{"error":"empty content"}')
            # 2026-10-01 修（外部复测 §2.2）：这套校验以前**只加在 MCP 入口**（loci.py `call_tool`），
            #   面板这条直写路径漏掉了 —— 同一个 bug 修了一半。实测 `{"mtype":"乱写类型"}` 会
            #   200 + 落库成 fact。现在两条入口用同一口径。
            #   已确认前端两个调用点都安全：`f-type` 下拉没有空选项；候选记忆的 mtype 来自
            #   `_EXTRACT_PATTERNS` 且四个取值都在 TYPES 内。
            _mtype = str(body.get("mtype") or "").strip()
            if _mtype not in hippo.TYPES:
                return self._json({"ok": False, "error": "mtype 非法：%r（可选：%s）"
                                   % (_mtype, " / ".join(hippo.TYPES))})
            try:
                _imp = int(body.get("importance", 2))
            except (TypeError, ValueError):
                return self._json({"ok": False, "error": "importance 必须是 1-4 的整数（收到 %r）"
                                   % (body.get("importance"),)})
            if not 1 <= _imp <= 4:
                return self._json({"ok": False, "error": "importance 必须是 1-4 的整数（收到 %d）" % _imp})
            # 2026-10-05 P0 修复：save_memory 会抛 MemorySkipped（排除清单命中）。
            #   原来没接 → 整个请求落进兜底 except → HTTP 500。
            #   500 是"服务故障"，但这其实是**正常业务判定**（用户输入被判为无信息量），
            #   5xx 会污染监控、也拿不到原因。改成 400 并把原因回给前端。
            #   面板上加"强制保存"勾选会传 skip_check=false 绕过（下面已透传）。
            try:
                mid = hippo.save_memory(
                    body["content"], _mtype,
                    _imp, body.get("tags", ""),
                    body.get("project", ""), "panel",
                    session_id=body.get("session_id", 0),
                    turn=body.get("turn", 0),
                    skip_check=not bool(body.get("force_save", False)))
            except hippo.MemorySkipped as e:
                return self._json({
                    "ok": False,
                    "excluded": e.reason,
                    "error": "这条内容命中排除清单（%s），没有存进去。"
                             "判定是纯规则、无 LLM。如果它确实有价值，"
                             "勾选「强制保存」再提交。" % e.reason}, 400)
            # 命中去重时 save_memory 返回的是既有那条的 id（不是新写入），
            #   告诉调用方，否则面板/Agent 会以为新存了一条，下次可能重复问。
            self._json({"id": mid, "deduped": bool(body.get("_dedup_hit"))})
        elif u.path == "/api/delete":
            # 2026-10-01 修（P1-1）：以前不看 delete_memory 的返回值，目标不存在也回 ok:true
            if not hippo.delete_memory(int(body["id"])):
                return self._json({"ok": False, "error": "记忆 #%s 不存在" % body["id"]})
            self._json({"ok": True})
        elif u.path == "/api/pack/import":
            self._json(import_pack(body))
        elif u.path == "/api/collect":
            paths = body.get("paths", [])
            if not isinstance(paths, list):
                return self._send(400, '{"error":"paths must be a list"}')
            self._json(collect(paths))
        elif u.path == "/api/session/parse":
            self._json({"messages": hippo.parse_transcript(body.get("text", ""))})
        elif u.path == "/api/session/save":
            msgs = body.get("messages") or hippo.parse_transcript(body.get("text", ""))
            if not isinstance(msgs, list) or not msgs:
                return self._json({"error": "没有可归档的对话内容"})
            sid, created = hippo.save_session(
                body.get("title", ""), body.get("project", ""), body.get("agent", ""),
                msgs, source_path=body.get("source_path", ""),
                summary=body.get("summary", ""))
            self._json({"ok": True, "id": sid, "created": created, "count": len(msgs)})
        elif u.path == "/api/session/delete":
            # 2026-10-01 修：以前用 body.get("sid", 0)，缺 sid 时静默删 0 号、还回 ok:true。
            # 现在缺参数 → KeyError → 400；目标不存在 → ok:false。
            if not hippo.delete_session(int(body["sid"])):
                return self._json({"ok": False, "error": "会话 #%s 不存在" % body["sid"]})
            self._json({"ok": True})
        elif u.path == "/api/pin":
            if not hippo.set_pinned(int(body["id"]), int(body.get("pinned", 1))):
                return self._json({"ok": False, "error": "记忆 #%s 不存在" % body["id"]})
            self._json({"ok": True})
        elif u.path == "/api/retire":
            self._json(hippo.retire_memory(int(body["id"])))
        elif u.path == "/api/touch":
            self._json(hippo.touch_memory(int(body["id"])))
        elif u.path == "/api/supersede":
            self._json(hippo.supersede_memory(int(body["old_id"]), int(body["new_id"])))
        elif u.path == "/api/merge":
            self._json(hippo.merge_memories(body.get("ids", []), body.get("content")))
        elif u.path == "/api/skill/export":
            self._json(hippo.export_skill(body.get("project", ""),
                                           body.get("out_dir") or None))
        elif u.path == "/api/skill/copy":
            # 把一个已有 skill 复制到另一个 Agent 的 skills 目录（复制，不删源）
            self._json(hippo.copy_skill_to(body.get("src", ""), body.get("target", ""),
                                            overwrite=bool(body.get("overwrite"))))
        elif u.path == "/api/sourcefiles/clean":
            self._json(cleanup_sourcefiles(body.get("paths", []), body.get("memory_ids", []),
                                            want_backup=(body.get("backup", True) is not False)))
        elif u.path == "/api/archive/config":
            cfg = hippo.load_archive_cfg()
            for k in ("dir", "auto", "keep"):
                if k in body:
                    cfg[k] = body[k]
            if cfg.get("dir") and not os.path.isdir(cfg["dir"]):
                try:
                    os.makedirs(cfg["dir"], exist_ok=True)
                except Exception as e:
                    self._json({"error": "目录不可用：%s" % e})
                    return
            hippo.save_archive_cfg(cfg)
            self._json({"ok": True, "cfg": cfg})
        elif u.path == "/api/archive/snapshot":
            self._json(hippo.do_archive_snapshot(force=bool(body.get("force", True))))
        elif u.path == "/api/shutdown":
            self._json({"ok": True, "msg": "服务正在关闭"})
            # 先把响应发出去，再延迟退出，避免浏览器收到连接中断
            import threading as _th

            def _bye():
                import time as _t
                _t.sleep(0.4)
                os._exit(0)

            _th.Thread(target=_bye, daemon=True).start()
        elif u.path == "/api/split":
            self._json(hippo.split_memory(int(body["id"]),
                                           int(body.get("max_len", 600)),
                                           bool(body.get("keep", 1))))
        elif u.path == "/api/cleanup/run":
            # 2026-10-09：backup=false 时不导备份（用户要"不备份直接删"的选择权）。
            # **默认仍是备份** —— 只有前端明确传 false 才跳过，安全默认值不变。
            # 不备份的正当理由：隐私（有人就是要数据不留副本）。
            _want_bak = body.get("backup", True) is not False
            if body.get("ids"):
                bak = export_backup_file("loci-backup-before-cleanup") if _want_bak else ""
                r = hippo.bulk_delete_ids(body["ids"])
                r["backup"] = bak
                r["items"] = r["items"][:20]
                return self._json(r)
            project = body.get("project") or None
            agent = body.get("agent") or None
            before = body.get("before") or None
            only_sup = bool(body.get("only_superseded"))
            prev = cleanup_preview(project, agent, before, only_sup)
            if not prev["count"]:
                return self._json({"error": "当前条件没有命中任何记忆"})
            bak = export_backup_file("loci-backup-before-cleanup") if _want_bak else ""
            r = hippo.bulk_delete(project, agent, before, only_sup)
            r["backup"] = bak
            r["items"] = r["items"][:20]
            self._json(r)
        elif u.path == "/api/cleanup/orphans":
            o = hippo.find_orphans()
            ids = [m["id"] for m in o["memories"]]
            sessions = [s["id"] for s in o["sessions"]]
            # 2026-10-09：同 cleanup/run，backup=false 时不导备份（默认仍导）
            bak = (export_backup_file("loci-backup-before-orphan-purge")
                   if body.get("backup", True) is not False else "")
            if ids:
                hippo.bulk_delete_projects(o["projects"])
            if sessions:
                hippo.delete_sessions_bulk(sessions)
            self._json({"ok": True, "memories": len(ids), "sessions": len(sessions),
                        "projects": o["projects"], "backup": bak})
        elif u.path == "/api/scan-root":
            self._json(add_scan_root(body.get("dir", "")))
        elif u.path == "/api/scan-root/forget":
            self._json(forget_scan_root(body.get("dir", "")))
        elif u.path == "/api/agent/register":
            self._json(register_agent(body.get("name", ""), body.get("config")))
        elif u.path == "/api/agent/unregister":
            self._json(unregister_agent(body.get("name", ""), body.get("config")))
        elif u.path == "/api/agent/add":
            self._json(add_agent(body.get("path", ""), body.get("name", "")))
        elif u.path == "/api/agent/forget":
            self._json(forget_agent(body.get("path", "")))
        elif u.path == "/api/agent/register-all":
            targets = _json_targets()
            results = []
            for a in scan_agents():
                if a["installed"] and not a["loci_registered"] and a.get("writable"):
                    r = register_agent(a["name"])
                    r["name"] = a["name"]
                    results.append(r)
            self._json({"results": results,
                        "note": "" if results else "没有需要接入的 Agent（未安装或不支持自动写入）"})
        elif u.path == "/api/open-folder":
            self._json(open_folder(body.get("path", "")))
        elif u.path == "/api/autoscan":
            # 2026-09-29 从 do_GET 移来：有副作用的接口一律走 POST（详见 do_GET 里的说明）
            r = hippo.auto_scan_agents(
                include_subagent=q.get("subagent", ["1"])[0] != "0",
                extract=q.get("extract", ["1"])[0] != "0")
            self._json(r)
        elif u.path == "/api/agent/pick-folder":
            self._json(pick_folder())     # 2026-09-29 从 do_GET 移来（会弹模态框）
        elif u.path == "/api/archive/pick":
            self._json(pick_folder())     # 同上
        else:
            self._send(404, '{"error":"not found"}')

    def log_message(self, *a):
        pass      # 有意静音：屏蔽 http.server 的每请求访问日志，免得刷屏


def main():
    ap = argparse.ArgumentParser(description="Loci 管理面板")
    ap.add_argument("--port", type=int, default=8787)
    ap.add_argument("--open", action="store_true")
    ap.add_argument("--idle-exit", type=int, default=300, metavar="秒",
                    help="闲置多少秒后自动退出，默认 300（0 = 常驻不退出）")
    a = ap.parse_args()
    global IDLE_EXIT_SEC
    IDLE_EXIT_SEC = max(0, a.idle_exit)
    _BOUND_PORT[0] = a.port   # 给 CSRF 守卫判断"自己人"用
    url = f"http://127.0.0.1:{a.port}"
    # 端口已被占：给一句人话，别抛栈。
    # 之前这里没有任何防护 —— 点两次快捷方式就会看到满屏红色 Traceback，
    # 让人以为程序坏了，其实只是开了第二个实例（Windows 下 SO_REUSEADDR
    # 还允许两个进程绑同一端口，更容易撞上）。
    import socket as _sock
    _probe = _sock.socket()
    _probe.settimeout(0.4)
    _busy = (_probe.connect_ex(("127.0.0.1", a.port)) == 0)
    _probe.close()
    if _busy:
        print()
        print("  [Loci] 端口 %d 上已经有面板在运行了。" % a.port)
        print("  浏览器直接打开：%s" % url)
        print("  确实要再开一个：python panel.py --port 9000")
        return 1
    try:
        srv = ThreadingHTTPServer(("127.0.0.1", a.port), Handler)
    except OSError as _e:
        print()
        print("  [Loci] 端口 %d 起不来：%s" % (a.port, _e))
        print("  换个端口试试：python panel.py --port 9000")
        return 1
    if IDLE_EXIT_SEC > 0:
        print("Loci 管理面板已启动: %s" % url)
        print("  · 关掉浏览器页面后，%d 分钟内无访问会自动退出（不占后台）"
              % (IDLE_EXIT_SEC // 60 or 1))
        print("  · 想让它一直开着：--idle-exit 0")
    else:
        print("Loci 管理面板已启动: %s（常驻模式）" % url)
    print("  · Ctrl+C 立即停止")
    _start_watchdog()
    # 打开面板时检查一次归档（未配置目录时会直接跳过，不做任何事）
    try:
        if hippo.load_archive_cfg().get("auto"):
            _r = hippo.do_archive_snapshot()
            if _r.get("ok") and not _r.get("skipped"):
                print("  · 已生成今日数据库备份：%s" % _r.get("path"))
    except Exception:
        pass      # 容错：启动时的自动快照是尽力而为，失败不影响面板启动
    if a.open:
        threading.Timer(0.8, lambda: webbrowser.open(url)).start()
    srv.serve_forever()


if __name__ == "__main__":
    main()
