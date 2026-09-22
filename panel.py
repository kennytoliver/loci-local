#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Hippocampus Panel — macOS Vibrancy 风本地网页面板（零依赖单文件）
=================================================
- 复用 hippocampus.py 引擎（同一目录 import），数据直连同一个 hippocampus.db
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
import hippocampus as hippo

# ---------- Agent 注册表：检测 + 一键接入的唯一数据源 ----------
def _custom_path():
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), "agents.json")

def _load_store():
    """agents.json 统一读写：{"agents":[...], "scan_roots":[...]}
    兼容旧格式（纯 list）。"""
    p = _custom_path()
    d = {"agents": [], "scan_roots": []}
    if not os.path.exists(p):
        return d
    try:
        data = json.loads(_file_text(p) or "{}")
    except Exception:
        return d
    if isinstance(data, list):
        d["agents"] = data
    elif isinstance(data, dict):
        d["agents"] = data.get("agents") or []
        d["scan_roots"] = data.get("scan_roots") or []
    return d

def _save_store(d):
    try:
        with open(_custom_path(), "w", encoding="utf-8") as f:
            json.dump(d, f, ensure_ascii=False, indent=2)
        return True
    except Exception:
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
    try:
        import subprocess as _sp
        out = _sp.run(["py", "-0p"], capture_output=True, text=True, timeout=8).stdout or ""
        for line in out.splitlines():
            m = re.search(r"([A-Za-z]:\\[^\s]+\.exe)", line)
            if m:
                cands.append(m.group(1))
    except Exception:
        pass
    cands += [os.path.join(local, "Programs", "Python", "Python313", "python.exe"),
              os.path.join(local, "Programs", "Python", "Python312", "python.exe"),
              os.path.join(home, "anaconda3", "python.exe"),
              os.path.join(home, "miniconda3", "python.exe")]
    seen = set()
    for c in cands:
        if not c or c in seen:
            continue
        seen.add(c)
        if not os.path.exists(c):
            continue
        try:
            r = subprocess.run([c, "-c", "import tkinter"],
                               capture_output=True, timeout=25)
            if r.returncode == 0:
                return c
        except Exception:
            continue
    return None

def pick_folder():
    """弹系统文件夹选择框：自动挑选一个带 tkinter 的解释器（子进程运行，避免与 HTTP 线程冲突）"""
    py = _tk_python()
    if not py:
        return {"error": "本机没找到带图形界面的 Python，无法弹出选择框；请直接把路径粘贴到输入框"}
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
        pass
    return False

# ---------- 本机 Agent 体检 ----------
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
        if cfg_hit:
            try:
                registered = '"hippocampus"' in _file_text(cfg_hit)
            except Exception:
                pass
        out.append({"name": a["name"], "state": state,
                    "installed": state == "installed",
                    "writable": a["write"],
                    "source": a.get("source", "builtin"),
                    "hippocampus_registered": registered,
                    "config": cfg_hit or a["configs"][0]})
    out.sort(key=lambda x: x["name"].lower())   # 按名称 A-Z 排序
    return out

# ---------- 记忆包导出/导入 ----------
def export_backup_file(prefix="hippocampus-backup"):
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
                    continue
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
        for p in paths:
            try:
                shutil.move(p, os.path.join(d, os.path.basename(p)))
            except Exception:
                pass
        return {"ok": True, "recycled": len(paths), "note": "非 Windows：已移入 .trash 目录"}
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

def cleanup_sourcefiles(paths, delete_memory_ids=None):
    """源文件清理：备份（逐个校验）→ 移入回收站（分批≤10）→ 可选同时删除对应记忆"""
    paths = [p for p in (paths or []) if p and os.path.isfile(p)]
    if not paths:
        return {"error": "没有选中任何有效文件"}
    stamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
    backup_dir, ready, failed_backup = _backup_files(paths, stamp)
    if not ready:
        return {"error": "备份失败，已中止删除（未动任何文件）",
                "backup_dir": backup_dir, "failed_backup": failed_backup}
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
                    pass
        out.append({"dir": d, "files": n, "size": size, "time": name})
    return out

PACK_FORMAT = "hippocampus-pack"

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
    if not isinstance(pack, dict) or pack.get("format") != PACK_FORMAT \
            or not isinstance(pack.get("memories"), list):
        return {"error": "不是有效的 Hippocampus 记忆包"}
    conn = hippo.db()
    existing = {r["content"] for r in conn.execute("SELECT content FROM memories")}
    conn.close()
    imported = skipped = 0
    for m in pack["memories"]:
        c = (m.get("content") or "").strip()
        if not c or c in existing:
            skipped += 1
            continue
        hippo.save_memory(c, m.get("mtype", "fact"), m.get("importance", 2),
                           m.get("tags", ""), m.get("project", ""), "pack-import")
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
                    continue
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
                continue
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
            it["in_db"] = False
    return out

def collect(paths):
    """把勾选的文件入库（路径白名单校验 + 内容去重）"""
    roots = [os.path.realpath(r) for r in _collect_roots()]
    conn = hippo.db()
    existing = {r["content"] for r in conn.execute("SELECT content FROM memories")}
    conn.close()
    imported, skipped, errors = 0, 0, []
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
            hippo.save_memory(text, "context", 2, f"采集,{kind}", proj, "collector", rp)
            existing.add(text)
            imported += 1
        except Exception as e:
            errors.append(f"{p}: {e}")
    return {"imported": imported, "skipped": skipped, "errors": errors}

# ⚠️ PAGE 必须是 raw string（r"""）。若去掉 r，Python 会把 JS 里的 \n 提前转义成
# 真实换行，导致发到浏览器的脚本出现跨行字符串 → 浏览器丢弃整段 JS → 所有按钮失效。
# 改动本块后请运行：python test_panel.py
# ---------- 一键接入：直接读写各 Agent 的 MCP 配置（写入前必做备份） ----------
def _script_paths():
    here = os.path.dirname(os.path.abspath(__file__))
    return os.path.join(here, "hippocampus.py"), os.path.join(here, "hippocampus.db")

def _server_entry():
    """生成写进各家配置的 MCP server 条目（路径动态取自当前运行环境，保证可移植）"""
    script, db = _script_paths()
    return {"command": sys.executable,
            "args": ["-X", "utf8", script],
            "env": {"HIPPOCAMPUS_DB": db}}

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
    """把 hippocampus 写入指定 Agent 的 MCP 配置（备份 + 合并不覆盖）"""
    path, shape, err = _resolve_target(name, config)
    if err:
        return err
    try:
        data, _ = _load_config(path)
        servers = _servers_get(data, shape, create=True)
    except ValueError as e:
        return {"error": str(e), "path": path}
    bak = _backup(path)
    servers["hippocampus"] = _server_entry()
    try:
        _write_config(path, data)
    except Exception as e:
        return {"error": f"写入失败：{e}", "path": path, "backup": bak}
    return {"ok": True, "path": path, "backup": bak, "created": bak is None, "shape": shape}

def unregister_agent(name, config=None):
    """移除指定 Agent 配置里的 hippocampus 条目（备份 + 仅删自己那一项）"""
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
    if not servers or "hippocampus" not in servers:
        return {"error": "该配置里没有 hippocampus 条目", "path": path}
    bak = _backup(path)
    servers.pop("hippocampus", None)
    try:
        _write_config(path, data)
    except Exception as e:
        return {"error": f"写入失败：{e}", "path": path, "backup": bak}
    return {"ok": True, "path": path, "backup": bak}

def verify_mcp(timeout=20):
    """真实握手验证：起 hippocampus.py，走 initialize + tools/list，确认 MCP 可用"""
    script, db = _script_paths()
    env = dict(os.environ)
    env["HIPPOCAMPUS_DB"] = db
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
                         "clientInfo": {"name": "hippocampus-panel", "version": "0.2"}}})
        r = recv()
        name = (r or {}).get("result", {}).get("serverInfo", {}).get("name")
        send({"jsonrpc": "2.0", "method": "notifications/initialized"})
        send({"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
        r2 = recv()
        tools = [t["name"] for t in (r2 or {}).get("result", {}).get("tools", [])]
        ok = name == "hippocampus" and len(tools) == 10
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
            pass


# ---------- 闲置自动退出 ----------
# 设计意图：面板是「用完即弃」的管理界面，不该常驻后台。
# 页面每 60 秒发一次心跳；页面关掉后心跳停止，超过 IDLE_EXIT_SEC 秒就自动退出，
# 用户不需要记得关，也不需要额外的开关程序。
_LAST_SEEN = [0.0]      # 最近一次活动（单调时钟）
IDLE_EXIT_SEC = 300     # 默认 5 分钟；设为 0 表示常驻不自动退出


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
            print("[Hippocampus] 面板已闲置 %d 秒，自动退出（记忆数据不受影响）"
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
<title>Hippocampus</title>
<style>
:root{
  /* 页面底从库值 #161616 压到 #0e0e0e：库的 #161616 对卡片 #1d1d1c 只有 1.073:1 面差，
     用户 2026-09-21 反馈「框跟背景底色融在一起，分不开」——框画出来了，但框里框外一个色。
     现在 1.144:1，与亮色的 1.141:1 手感对齐（低亮度区人眼分辨更差，所以暗色不能低于亮色）。 */
  --d0:#0e0e0e; --d1:#1d1d1c; --d2:#2e2e2e; --d3:#383838;
  /* 描边：库在暗色把 --border 定义成与卡片同色（#1d1d1c）、--input 定义成纯黑，
     结果就是"框看不见"（用户 2026-09-21 明确反馈，实测对比度只有 1.00:1）。
     这里按用户要求调亮：常规 1.44:1、控件 1.90:1（暗色要更高，低亮度下人眼分辨更差）。
     控件描边取库里 --sidebar-border 的暗色族（#404040）再提一档，不是凭空编的。 */
  --line:#383838; --line2:#4a4a4a;
  --ink:#eff1f4; --sub:#949494; --faint:#6e6e6e;
  --acc:#8ab4f8; --acc2:#2dccd3; --ok:#34a853; --warn:#fbbc05; --bad:#f1204a;
  /* 数据可视化五色（库的 --chart-1..5，暗色刻意更"电"）——
     规范原话：图表是整个系统里颜色能量最强的地方，其余表面要保持安静 */
  --chart-1:#2dccd3; --chart-2:#f1204a; --chart-3:#edbbe8; --chart-4:#fbeb35; --chart-5:#baf6f0;
  --destructive:#ef4444; --destructive-foreground:#ffffff;
  --tracking-normal:0em;
  --chrome:rgba(22,22,22,.88); --toastbg:rgba(46,46,46,.96); --hover:rgba(255,255,255,.05);
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
  --d0:#eef0f4; --d1:#ffffff; --d2:#f9f9fa; --d3:#eff1f4;
  /* 描边：库给的是 #ebebeb，实测对白底只有 1.19:1 —— 用户说"太不够明显"，
     所以两个主题一起提到"一眼能看清"的量级（对比度写进 audit_tokens.js 的断言）：
     常规描边 1.37:1、控件描边 1.57:1（暗色要更高，低亮度下人眼分辨更差） */
  --line:#dcdcdc; --line2:#cccfd4;
  --ink:#0e1115; --sub:#7f8d9f; --faint:#a0aab5;
  --acc:#4285f4; --acc2:#4285f4;
  /* 图表五色：亮色走"可识别的 Google 多彩"路线 */
  --chart-1:#4285f4; --chart-2:#ea4335; --chart-3:#fbbc05; --chart-4:#0043ad; --chart-5:#34a853;
  --destructive:#ef4444; --destructive-foreground:#ffffff;
  --chrome:rgba(255,255,255,.86); --toastbg:rgba(255,255,255,.98);
  --hover:rgba(14,17,21,.04);
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
  --sidebar-primary:#4285f4; --sidebar-primary-foreground:#fff;
  --sidebar-accent:#dbeafe; --sidebar-accent-foreground:#003e8f;
  --sidebar-border:#e7eaef; --sidebar-ring:#4285f4;}
/* 白底上需要单独调色的地方（黑底成立的配色在白底会失真） */
:root[data-theme="light"] .toast-item{box-shadow:0 10px 30px rgba(14,17,21,.16)}

*{margin:0;padding:0;box-sizing:border-box}
html,body{height:100%}
body{background:var(--bg);color:var(--ink);font-family:var(--sans);
  -webkit-font-smoothing:antialiased;overflow:hidden}
.app{display:flex;height:100vh}

/* ── 侧边栏（规范：专属的淡色外壳 = 工作区框架，不是通用列表）────────────── */
.side{width:240px;flex-shrink:0;display:flex;flex-direction:column;
  background:var(--sidebar);color:var(--sidebar-foreground);
  border-right:1px solid var(--sidebar-border)}
.lights{display:flex;gap:8px;padding:16px 18px 10px}
.lights i{width:12px;height:12px;border-radius:50%;display:block}
.lights .r{background:#ff5f57}.lights .y{background:#febc2e}.lights .g{background:#28c840}
.sbrand{display:flex;align-items:center;gap:10px;padding:14px 16px 16px;border-bottom:1px solid var(--sidebar-border)}
/* 品牌块：底色用规范的 --sidebar-primary（亮 #4285f4 / 暗 #0065fd），
   图标本身也换成品牌蓝底版（tools/make_icon_blue.py 生成），确保「底色是蓝的」看得见 */
.slogo{width:32px;height:32px;flex:0 0 32px;border-radius:var(--radius-lg);object-fit:cover;
  display:block;background:var(--sidebar-primary);box-shadow:var(--shadow-2xs)}
.sbtext{min-width:0}
.sfoot{padding:10px 18px;font-size:11px;color:var(--faint);border-top:1px solid var(--sidebar-border);flex-shrink:0}
.sbrand h1{font-family:var(--sans);font-size:16px;font-weight:600;letter-spacing:.1px}
.sbrand p{font-size:11px;color:var(--sub);margin-top:3px}
nav{flex:1;padding:12px 10px;overflow-y:auto}
.nav{display:flex;align-items:center;gap:10px;padding:8px 12px;
  border-radius:var(--radius-md);font-size:13.5px;color:var(--sub);
  cursor:pointer;text-decoration:none;margin-bottom:2px;
  transition:background var(--duration-fast) var(--ease-out),
             color var(--duration-fast) var(--ease-out)}
.nav:hover{background:var(--sidebar-accent);color:var(--sidebar-accent-foreground)}
.nav.on{background:var(--sidebar-primary);color:var(--sidebar-primary-foreground)}

/* ── 主区 ───────────────────────── */
.main{flex:1;display:flex;flex-direction:column;min-width:0}
.toolbar{display:flex;gap:10px;align-items:center;padding:14px 22px;
  background:var(--chrome);backdrop-filter:blur(20px);
  -webkit-backdrop-filter:blur(20px);
  border-bottom:1px solid var(--line);position:relative;z-index:2}
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
.content{flex:1;overflow-y:auto;padding:20px 20px 48px}
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
.btn.pri{background:var(--acc);border-color:var(--acc);color:#fff}
.btn.pri:hover{background:var(--primary-hover);border-color:var(--primary-hover);color:#fff}
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
.mem.indb{opacity:.42}
/* 复选框用更饱和的蓝，暗色下那条极细的原生框才看得见 */
.ck{width:14px;height:14px;accent-color:var(--sidebar-primary);cursor:pointer;flex-shrink:0}
.mem .meta{display:flex;justify-content:space-between;align-items:center;
  margin-top:10px;padding-top:9px;border-top:1px solid var(--line);
  font-size:11px;color:var(--faint)}
.del{border:none;background:none;color:var(--faint);cursor:pointer;font-size:11px}
.del:hover{color:var(--bad)}

/* ── Agent 体检 ──────────────────── */
/* 卡片网格：自适应列宽（15 款产品，窄窗口自动降列） */
.agents{display:grid;grid-template-columns:repeat(auto-fill,minmax(320px,1fr));gap:12px}
.agent{background:var(--card);border:1px solid var(--line);border-radius:var(--radius-lg);
  padding:var(--space-4) var(--space-5);box-shadow:var(--shadow-2xs)}
.agent .aname{font-size:14px;font-weight:600;display:flex;align-items:center;gap:9px}
.agent .astat{font-size:11px;margin-top:8px}
.dot{width:8px;height:8px;border-radius:50%;display:inline-block;flex-shrink:0}
.dot.on{background:var(--ok)}.dot.off{background:var(--faint)}.dot.warn{background:var(--warn)}
.badge-ok{color:var(--ok)}.badge-warn{color:var(--warn)}.badge-no{color:var(--faint)}
.agent .acts{margin-top:11px;display:flex;gap:8px}
.mini{border:1px solid var(--line2);background:var(--card);color:var(--ink);
  border-radius:var(--radius-md);padding:5px 12px;font-size:12px;cursor:pointer;
  font-family:var(--sans);transition:background var(--duration-fast) var(--ease-out)}
.mini:hover{background:var(--d3);border-color:var(--acc)}
.mini.warn{color:var(--bad)}
.msg{background:var(--d2);border:1px solid var(--line);border-radius:var(--radius-md);
  padding:12px 16px;font-size:12.5px;line-height:1.75;margin-bottom:14px;white-space:pre-wrap}
.msg.ok{border-color:rgba(52,168,83,.45)}
.msg.err{border-color:rgba(255,69,58,.5)}
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
.pagehead .pacts{display:flex;align-items:center;gap:var(--space-2);flex-wrap:wrap}
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
#sk-list .grphead.ghead{border:1px solid var(--line);border-radius:var(--radius-md);
  padding:var(--space-2) var(--space-3);background:var(--d2);margin-top:var(--space-3)}
#sk-list .gbody{border:1px solid var(--line);border-radius:var(--radius-md);
  padding:var(--space-2);margin-top:2px}
/* 分组内容下方的收起按钮（用户要求放在下面，而不是去点标题） */
.grpfoot{display:block;width:100%;margin-top:4px;padding:4px 0;font:inherit;font-size:12px;
  color:var(--acc);background:transparent;border:1px dashed var(--line);
  border-radius:var(--radius-md);cursor:pointer}
.grpfoot:hover{background:var(--hover)}
.row{display:flex;gap:10px;align-items:center;flex-wrap:wrap}
.hint{font-size:12px;color:var(--faint);line-height:1.7;margin-top:10px}
/* 本机对话来源列表 —— 让"这次到底扫了谁、为什么"看得见
   （2026-09-21：以前来源是写死的三个产品名，跟本机实际装了什么无关） */
.srclist{display:flex;flex-direction:column;gap:var(--space-2)}
.srcrow{display:flex;align-items:center;gap:10px;padding:7px 11px;
  border-radius:var(--radius-md);background:var(--d2);font-size:12.5px;color:var(--sub)}
.srcrow.on{background:var(--d3);color:var(--ink)}
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
  .agents{grid-template-columns:1fr}
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
.agent,.stat{animation:cardIn .34s var(--ease) both;animation-delay:calc(var(--i,0)*32ms)}

/* 悬停微交互 */
.mem,.agent,.stat{transition:transform .18s var(--ease),border-color .18s var(--ease),background .18s var(--ease)}
.mem:hover,.agent:hover{transform:translateY(-2px);border-color:var(--line2)}
.btn,.mini,.del,.nav{transition:transform .12s var(--ease),background .16s var(--ease),color .16s var(--ease)}
.btn:active,.mini:active,.del:active{transform:scale(.965)}
/* 键盘焦点可见环：亮色蓝 / 暗色白（规范 --ring） */
.btn:focus-visible,.nav:focus-visible,.mini:focus-visible,.del:focus-visible{
  outline:2px solid var(--ring);outline-offset:2px}
.nav:active{transform:scale(.985)}





/* 搜索框聚焦发光（有细节但不刺眼） */
.toolbar input:focus,.textin:focus{box-shadow:0 0 0 3px var(--ring-soft)}

/* 顶部细进度条（长任务时出现） */
#topbar{position:fixed;left:0;top:0;height:2px;width:0;z-index:99;
  background:linear-gradient(90deg,#0a84ff,#30d158);transition:width .3s var(--ease),opacity .3s}
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
.toast-item.err{border-color:rgba(255,69,58,.55)}

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
.mem:hover{border-color:var(--ring);box-shadow:none}


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

/* ── 健康度仪表（套规范 Chart 组件：环形占比 + 五色分类条 + 计数 chips）──
   规范原话「图表是整个系统里颜色能量最强的地方，其余表面要保持安静」，
   所以这里是全页唯一允许用满 --chart-* 的地方。
   环形用 conic-gradient + ::after 掏空中间（和库 ui_kit 的 .donut 同一手法），纯 CSS 零依赖。 */
.health{display:grid;grid-template-columns:auto minmax(0,1fr);gap:var(--space-5) var(--space-8);
  align-items:center;background:var(--card);border:1px solid var(--line);
  border-radius:var(--radius-lg);padding:var(--space-5) var(--space-6);margin-bottom:var(--space-5)}
.health .hgauge{display:flex;flex-direction:column;align-items:center;gap:var(--space-3)}
.health .hring{width:132px;height:132px;border-radius:50%;position:relative;
  transition:background .5s var(--ease)}
.health .hring::after{content:"";position:absolute;inset:16px;border-radius:50%;background:var(--card)}
.health .hval{position:absolute;inset:0;display:flex;align-items:center;justify-content:center;
  gap:2px;z-index:1}
.health .hval b{font-size:34px;font-weight:500;line-height:1;font-variant-numeric:tabular-nums}
.health .hval i{font-size:12px;color:var(--sub);font-style:normal;margin-top:11px}
.health .hgrade{font-size:12px;font-weight:600;padding:3px 10px;border-radius:999px;
  box-shadow:inset 0 0 0 1px currentColor}
.health .hgrade.a,.health .hgrade.b{color:var(--ok)}
.health .hgrade.c{color:var(--warn)}
.health .hgrade.d{color:var(--bad)}
.health .hbars{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:var(--space-3) var(--space-6)}
.health .hrow{display:flex;align-items:center;gap:var(--space-3);font-size:12px;color:var(--sub)}
.health .hrow .lab{width:80px;flex-shrink:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.health .hbar{flex:1;height:6px;border-radius:999px;background:var(--muted);overflow:hidden}
.health .hbar i{display:block;height:100%;border-radius:999px;
  transition:width .5s var(--ease)}
.health .hrow .val{width:38px;text-align:right;color:var(--ink);
  font-variant-numeric:tabular-nums;flex-shrink:0}
.health .hchips{grid-column:1/-1;display:flex;flex-wrap:wrap;gap:var(--space-2);
  border-top:1px solid var(--line);padding-top:var(--space-4)}
.health .hchip{font-size:11.5px;color:var(--sub);border:1px solid var(--line);
  border-radius:999px;padding:2px 9px;white-space:nowrap}
.health .hchip.warn{color:var(--warn);border-color:var(--warn)}

/* ── 可勾选文件表（套规范 Table 组件：静音表头 + 细描边行 + 彩色状态）── */
/* 用户反馈：这张表原来没有外框、长内容还会压到邻列叠字。
   加外框 + 每个单元格自己裁 —— `min-width:0` 是关键：grid 子项默认不肯缩，
   会被长字符串（比如完整时间戳）顶出去盖到下一列。 */
.stable{display:flex;flex-direction:column;border:1px solid var(--line);
  border-radius:var(--radius-lg);padding:var(--space-2) 0;background:var(--card)}
.stable .shead-row>span,.stable .srow>span{min-width:0;overflow:hidden;
  text-overflow:ellipsis;white-space:nowrap}
.stable .shead-row,.stable .srow{
  display:grid;grid-template-columns:26px minmax(0,1fr) 58px 116px 92px 74px;
  gap:var(--space-3);align-items:center}
.stable .shead-row{padding:0 var(--space-4) var(--space-2);font-size:11px;font-weight:600;
  letter-spacing:.06em;text-transform:uppercase;color:var(--sub)}
.stable .srow{padding:var(--space-3) var(--space-4);border-radius:var(--radius-md);
  cursor:pointer;background:transparent;border:0;width:100%;text-align:left;
  transition:background var(--duration-fast) var(--ease-out)}
.stable .srow:hover{background:var(--hover)}
.stable .srow.indb{opacity:.5}
.stable .srow .spath{font-size:13px;color:var(--sub);overflow:hidden;
  text-overflow:ellipsis;white-space:nowrap}
.stable .srow:hover .spath{color:var(--ink)}
.stable .srow .cell{font-size:12px;color:var(--sub);overflow:hidden;
  text-overflow:ellipsis;white-space:nowrap}

/* 退出服务按钮：平时低调，悬停变警示色 */
.quitbtn{color:var(--sub)}
.quitbtn:hover{color:#ff453a;border-color:rgba(255,69,58,.5)}

/* 关闭后的提示页 */
.bye{display:grid;place-items:center;height:100vh;text-align:center;gap:14px;
  font-family:-apple-system,"PingFang SC","Microsoft YaHei",sans-serif}
.bye img{width:96px;opacity:.9}
.bye h2{font-size:20px;font-weight:600;margin:0}
.bye p{color:var(--sub);font-size:13.5px;margin:0;line-height:1.9}
.bye code{background:var(--d3);border-radius:6px;padding:2px 7px;font-size:12.5px}

/* 主题切换按钮 */
.themetgl{width:34px;height:34px;flex-shrink:0;display:grid;place-items:center;
  background:transparent;border:1px solid var(--line);border-radius:9px;
  color:var(--sub);cursor:pointer;transition:all .18s var(--ease)}
.themetgl:hover{color:var(--acc);border-color:var(--acc);transform:rotate(-18deg)}
.ml-auto{margin-left:auto}

/* 主从双栏（列表 + 详情） */
.split{display:grid;grid-template-columns:clamp(360px,33%,460px) minmax(0,1fr);gap:20px;align-items:start}
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
  min-height:0;overflow:auto;
  animation:cardIn .3s var(--ease) both
}
/* 详情卡头／体 —— 对齐 Trae 稿 .detail-header / .detail-body（均 20px 内边距，头下一条分隔线） */
.split-side .dhead{padding:var(--space-5);border-bottom:1px solid var(--line)}
.split-side .dmain{padding:var(--space-5)}
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
[data-secfold] .t::before,[data-secfold] h3::before{content:"▼";display:inline-block;
  font-size:9px;color:var(--sub);margin-right:6px;vertical-align:middle;
  transition:transform .15s ease}
[data-secfold].sec-collapsed .t::before,
[data-secfold].sec-collapsed h3::before{content:"▶"}
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
.split-main .mem.lrow .mdot{width:8px;height:8px;border-radius:50%;
  flex:0 0 8px;margin-top:6px}
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
.split-main .macts{margin-left:auto;display:flex;gap:8px;flex-shrink:0;
  opacity:0;transition:opacity var(--duration-fast) var(--ease-out)}
.split-main .mem.lrow:hover .macts,.split-main .mem.lrow.sel .macts{opacity:1}
.split-main .mem.lrow.sel{background:var(--sel-bg);box-shadow:inset 3px 0 0 var(--sel-accent)}
.split-main .mem.lrow.sel:hover{background:var(--sel-bg)}
.split-main .mem.lrow.sel .mtitle{color:var(--sel-ink);font-weight:600}
.split-main .mem.lrow.sel .mmeta{color:var(--sel-ink);opacity:.8}
.split-main .mem.lrow.sel .mdot{box-shadow:0 0 0 2px var(--sel-bg)}

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
.foldbody.folded{display:-webkit-box;-webkit-line-clamp:6;-webkit-box-orient:vertical;
  overflow:hidden}
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
</style>
</head>
<body>
<div id="topbar"></div>
<div id="toast"></div>
<div class="app">
  <aside class="side">
    <div class="sbrand">
      <img class="slogo" src="/icon-blue.png" alt="">
      <div class="sbtext">
        <h1>Hippocampus</h1>
        <p>本地 AI 记忆管理器</p>
      </div>
    </div>
    <nav>
      <a class="nav on" data-v="session"><svg class="ico" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.9" stroke-linecap="round" stroke-linejoin="round"><path d="M20 12a8 8 0 1 1-3.2-6.4"/><path d="M4 20l1.6-4.2"/><circle cx="9" cy="12" r="1"/><circle cx="13" cy="12" r="1"/><circle cx="17" cy="12" r="1"/></svg><span>会话</span></a>
      <a class="nav" data-v="mem"><svg class="ico" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.9" stroke-linecap="round" stroke-linejoin="round"><ellipse cx="12" cy="5.5" rx="7.5" ry="3"/><path d="M4.5 5.5v6c0 1.7 3.4 3 7.5 3s7.5-1.3 7.5-3v-6"/><path d="M4.5 11.5v6c0 1.7 3.4 3 7.5 3s7.5-1.3 7.5-3v-6"/></svg><span>记忆</span></a>
      <a class="nav" data-v="audit"><svg class="ico" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.9" stroke-linecap="round" stroke-linejoin="round"><path d="M20 6L9.5 16.5 4 11"/><path d="M20 13v5a2 2 0 0 1-2 2H6a2 2 0 0 1-2-2v-5"/></svg><span>质检</span></a>
      <a class="nav" data-v="clean"><svg class="ico" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.9" stroke-linecap="round" stroke-linejoin="round"><path d="M4 7h16"/><path d="M9 7V5.5A1.5 1.5 0 0 1 10.5 4h3A1.5 1.5 0 0 1 15 5.5V7"/><path d="M6 7l1 12a2 2 0 0 0 2 2h6a2 2 0 0 0 2-2l1-12"/><path d="M10 11v6M14 11v6"/></svg><span>清理</span></a>
      <a class="nav" data-v="collect"><svg class="ico" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.9" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="8"/><path d="M12 4v8l5.5 3.5"/></svg><span>采集</span></a>
      <a class="nav" data-v="agents"><svg class="ico" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.9" stroke-linecap="round" stroke-linejoin="round"><rect x="4" y="7" width="16" height="12" rx="3"/><path d="M12 3v4"/><circle cx="9" cy="13" r="1.2"/><circle cx="15" cy="13" r="1.2"/></svg><span>Agent</span></a>
      <a class="nav" data-v="skill"><svg class="ico" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.9" stroke-linecap="round" stroke-linejoin="round"><path d="M12 3l2.4 5.3 5.6.7-4.2 3.9 1.1 5.6L12 15.8 7.1 18.5l1.1-5.6L4 9l5.6-.7z"/></svg><span>本机内容</span></a>
      <a class="nav" data-v="pack"><svg class="ico" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.9" stroke-linecap="round" stroke-linejoin="round"><path d="M3.5 8.5L12 4l8.5 4.5v7L12 20l-8.5-4.5z"/><path d="M3.5 8.5L12 13l8.5-4.5M12 13v7"/></svg><span>记忆包</span></a>
      <a class="nav" data-v="handoff"><svg class="ico" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.9" stroke-linecap="round" stroke-linejoin="round"><rect x="3.5" y="6" width="17" height="12" rx="3"/><path d="M8 11h8M8 14h5"/></svg><span>交接卡</span></a>
    </nav>
    <div class="sfoot">v0.2.0 · 本地运行</div>
  </aside>

  <main class="main">
    <div class="toolbar">
      <div class="stats" id="stats"></div>
      <input id="q" placeholder="搜索记忆，支持中文 — 如：上次定的部署方案"
             onkeydown="if(event.key==='Enter')doSearch()">
      <button class="btn quitbtn" onclick="shutdownPanel()" title="关闭面板服务（记忆数据不受影响）">⏻ 退出服务</button>
      <button class="themetgl" id="themetgl" onclick="toggleTheme()" title="切换白天 / 黑夜模式">
        <svg id="themeicon" viewBox="0 0 24 24" width="17" height="17" fill="none"
             stroke="currentColor" stroke-width="1.9" stroke-linecap="round"></svg>
      </button>
    </div>

    <div class="content">
      <!-- 记忆 -->
      <section id="v-mem" hidden>
        <!-- 页面标题区：Trae 稿在数值条下方写「记忆」，右侧放动作按钮 -->
        <div class="pagehead">
          <h2 class="ptitle" id="page-title">记忆</h2>
          <div class="pacts">
            <select id="proj-filter" class="proj-sel" onchange="loadList()">
              <option value="">全部项目</option>
            </select>
            <button class="btn" onclick="showContext()">常驻上下文</button>
            <button class="btn" onclick="toggleCleanup()">批量清理</button>
            <button class="btn" onclick="backToList()">全部</button>
            <button class="btn pri" onclick="goAdd()">＋ 记一条</button>
          </div>
        </div>
        <p class="lead">这里是<strong>给模型检索用的记忆碎片</strong> —— 短、独立、能命中关键词，
        不是给人读的叙事。想看「当时到底聊了什么」，去
        <span class="leadlink" onclick="show('session')">会话</span>页看原文时间线；
        每条记忆的「出处」都能跳回产出它的那一轮。</p>
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
          <p class="hint">删除是<strong>硬删除</strong>（不进回收站），但执行前会自动导出全量备份 JSON 到程序目录，可随时用「记忆包导入」恢复。时间条件＝删除该日期之前的记忆。</p>
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
          <aside class="split-side" id="detail"></aside>
        </div>
      </section>

      <!-- 会话层 -->
      <section id="v-session">
        <div class="pagehead">
          <h2 class="ptitle">会话</h2>
          <div class="pacts">
            <button class="btn" onclick="goAdd()" title="手写一条记忆（不用从对话里抽）">＋ 记一条</button>
            <button class="btn" onclick="runAutoScan()">扫描本机对话</button>
          </div>
        </div>
        <p class="lead">归档对话原文。记忆库存结论，会话库存过程与原话 —— 新 Agent 可检索复现当时的对话。</p>
        <!-- 扫描结果（瞬时区：扫描前这里是空的，不占位） -->
        <div class="msg" id="scan-msg" style="display:none"></div>
        <div id="scan-out"></div>

        <!-- 骨架 C：归档表单 -->
        <div class="panel">
          <div class="listhead">
            <span class="t">归档新会话</span>
            <span class="hint" style="margin:0">支持「我: / AI:」聊天文本、JSONL、JSON 数组</span>
          </div>
          <div class="row">
            <input class="textin" id="s-title" placeholder="会话标题（留空自动取首句）">
          </div>
          <div class="row" style="margin-top:8px">
            <input class="textin" id="s-project" placeholder="项目名（可选）">
            <input class="textin" id="s-agent" placeholder="来源 Agent（如 WorkBuddy / TraeWork）">
          </div>
          <textarea id="s-text" style="margin-top:8px"
            placeholder="把对话粘贴到这里：支持「我: / AI:」这类聊天文本，也支持 JSONL / JSON 数组（各 Agent 落盘的会话文件内容可直接贴）"></textarea>
          <div class="row" style="margin-top:12px">
            <button class="btn" onclick="sessionParse()">解析预览</button>
            <button class="btn" onclick="document.getElementById('s-file').click()">选文件导入</button>
            <input type="file" id="s-file" accept=".txt,.md,.json,.jsonl" style="display:none" onchange="sessionFile(this)">
            <button class="btn pri" id="s-save" disabled onclick="sessionSave()">确认归档</button>
          </div>
          <p class="hint" id="s-info">先点「解析预览」确认说话人识别无误，再点「确认归档」。内容重复的会话会自动跳过。</p>
        </div>

        <!-- 骨架 A：左会话列表 + 右原文时间线 -->
        <div class="listhead" style="margin-top:20px">
          <span class="t">已归档会话</span>
          <span class="fgroup">
            <input class="textin" id="s-q" placeholder="在原话里检索…（回车或点检索）"
                   onkeydown="if(event.key==='Enter')sessionSearch()"
                   oninput="if(!this.value.trim())loadSessions()">
            <button class="btn" onclick="sessionSearch()" title="在原话里检索（也可直接回车）">检索</button>
          </span>
        </div>
        <div class="split">
          <div class="split-main">
            <div class="shead"><span class="t">会话列表</span></div>
            <div class="sbody"><div id="s-list"></div></div>
          </div>
          <aside class="split-side">
            <div class="dhead">
              <div class="dhtop">
                <h3 id="s-view-t">原文时间线</h3>
                <div class="dacts" id="s-view-acts"></div>
              </div>
              <div class="dtop">
                <span class="dmi" id="s-view-h">点左侧任意会话查看原文；「抽取记忆」会在这里逐轮给出候选记忆</span>
              </div>
            </div>
            <div class="dmain"><div class="handoff-out" id="s-view"></div></div>
          </aside>
        </div>
      </section>

      <!-- 质检 -->
      <section id="v-audit" hidden>
        <div class="pagehead">
          <h2 class="ptitle">记忆质检</h2>
          <div class="pacts">
            <select id="au-proj" class="proj-sel" style="max-width:200px"><option value="">全部项目</option></select>
            <button class="btn" onclick="exportReport()">下载质检报告</button>
            <button class="btn pri" onclick="runAudit()">开始质检</button>
          </div>
        </div>
        <p class="lead">七查：重复（合并）· 疑似同义（人工判断）· 可能矛盾（以新代旧）· 长期未更新（续期/作废）· 过短 · 过粗粒度（拆分）· 元数据缺失。作废的记忆保留在库里但不参与检索。</p>
        <div class="health" id="health"></div>
        <div class="msg" id="audit-msg" style="display:none"></div>
        <div id="audit-out"></div>
      </section>

      <!-- 清理 -->
      <section id="v-clean" hidden>
        <div class="pagehead">
          <h2 class="ptitle">清理</h2>
          <div class="pacts">
            <button class="btn pri" onclick="doSnapshot()">立即备份</button>
          </div>
        </div>
        <p class="lead">删记忆、删源文件。文件走「备份 + 系统回收站」双保险，记忆删除前也会自动导出备份。</p>

        <!-- ① 先把它设好，再动下面的删除工具 -->
        <div class="panel">
          <div class="listhead">
            <span class="t">① 备份与归档</span>
            <span>
              <button class="btn" onclick="pickArchiveDir()">选择存放位置…</button>
              <button class="btn" onclick="openArchiveDir()">打开归档目录</button>
            </span>
          </div>
          <p class="lead" id="arch-line" style="margin-bottom:8px">加载中…</p>
          <p class="hint" id="arch-hint"></p>
          <div class="row">
            <input type="text" class="textin" id="arch-dir" placeholder="归档目录（可放在 D 盘 / 移动硬盘；留空 = 不启用）">
            <button class="btn" onclick="saveArchiveDir()">保存</button>
          </div>
          <p class="hint">快照是<b>复制</b>当前记忆库，<b>不会移动或删除任何记忆</b>。为避免占用系统盘，默认不启用，
            选择位置后才开始自动备份（每次打开面板检查一次，每天最多一份，默认保留 30 份）。</p>
        </div>

        <div class="panel">
          <div class="listhead">
            <span class="t">② 记忆的源文件</span>
            <span>
              <button class="btn" onclick="loadSourceFiles()">重新扫描</button>
              <button class="btn" onclick="cleanSourceFiles()">备份并移入回收站</button>
            </span>
          </div>
          <p class="hint" style="margin-top:0">这些是当初采集记忆的源文件（WorkBuddy 工作区日志）。删掉文件＝删掉对应记忆的来源依据。
            每批最多处理 10 个，完成后会在下方给出备份目录。</p>
          <div id="sf-list"></div>
          <div class="msg" id="sf-msg" style="display:none"></div>
        </div>

        <div class="panel">
          <div class="listhead">
            <span class="t">③ 记忆逐条勾选</span>
            <span>
              <select id="cm-proj" class="proj-sel" style="max-width:180px"><option value="">全部项目</option></select>
              <button class="btn" onclick="loadMemPick()">列出</button>
              <button class="btn" onclick="deletePicked()">删除选中记忆</button>
            </span>
          </div>
          <p class="hint" style="margin-top:0">勾选后点「删除选中记忆」＝硬删除（删前自动全量备份，可用「记忆包」导入还原）。</p>
          <div id="cm-list"></div>
          <div class="msg" id="cm-msg" style="display:none"></div>
        </div>

        <div class="panel">
          <div class="listhead"><span class="t">④ 备份记录</span></div>
          <div id="bk-list"></div>
        </div>
      </section>

      <!-- 采集中心 -->
      <section id="v-collect" hidden>
        <div class="pagehead">
          <h2 class="ptitle">采集中心</h2>
          <div class="pacts">
            <button class="btn pri" onclick="doScan()">扫描本机</button>
          </div>
        </div>
        <p class="lead">扫描本机 Agent 的历史日志与 skills，勾选后入库。已入库的条目自动置灰，不会重复写入。</p>
        <div class="panel">
          <div class="listhead">
            <span class="t">扫描结果</span>
            <span>
              <button class="btn" id="ck-all" style="display:none" onclick="checkAll(true)">全选</button>
              <button class="btn" id="ck-none" style="display:none" onclick="checkAll(false)">清空</button>
              <button class="btn pri" id="ck-go" style="display:none" onclick="doCollect()">入库选中</button>
            </span>
          </div>
          <p class="hint" style="margin-top:0" id="scan-info">还没扫描。扫描范围：WorkBuddy 各工作区 memory 日志、~/.workbuddy/skills。</p>
          <div id="scan-list"></div>
        </div>
      </section>

      <!-- Agent 体检 -->
      <section id="v-agents" hidden>
        <div class="pagehead">
          <h2 class="ptitle">Agent</h2>
          <div class="pacts">
            <button class="btn" onclick="verifyMcp()">验证 MCP 服务</button>
            <button class="btn pri" onclick="registerAll()">全部接入</button>
          </div>
        </div>
        <p class="lead">把 Hippocampus 接入本机所有 AI Agent：自动识别已安装的产品，一键写入 MCP 配置。写入前自动备份，只增不改其他条目。</p>

        <div class="panel">
          <div class="listhead">
            <span class="t">装在其他位置的 Agent？</span>
            <span class="hint" style="margin:0">粘 mcp.json 路径或它所在目录</span>
          </div>
          <div class="row">
            <input type="text" class="textin" id="add-path" placeholder="mcp.json 路径，或 Agent 所在目录">
            <button class="btn" onclick="pickFolder()">浏览…</button>
            <button class="btn" onclick="addAgent()">添加</button>
            <button class="btn" onclick="addScanRoot()">记为扫描目录</button>
          </div>
          <p class="hint">「添加」＝这一次认出来；「记为扫描目录」＝记住这个目录，以后每次自动扫描都会带上它（适合自己放的 Agent 集合目录）。</p>
          <div id="roots" class="row" style="flex-wrap:wrap;margin-top:10px"></div>
        </div>

        <div class="msg" id="agent-msg" style="display:none"></div>
        <div class="listhead" style="margin-top:20px">
          <span class="t">本机 Agent</span>
          <span class="hint" style="margin:0">接入后需重启对应的 Agent 才会生效</span>
        </div>
        <div class="agents" id="agents"></div>
      </section>

      <!-- 记忆包 -->
      <!-- 本机内容：技能 / 配置文件 / MCP 三类清算（原先只有技能） -->
      <section id="v-skill" hidden>
        <div class="pagehead">
          <h2 class="ptitle">本机内容</h2>
          <div class="pacts">
            <button class="btn" onclick="loadSkills()">重新探测</button>
          </div>
        </div>
        <p class="lead">记忆传结论，技能传能力。这里清算本机各 Agent 的三类内容：
          <b>技能</b>（能互相传的那类）、<b>配置文件</b>、<b>MCP</b>。
          全部只读；配置文件里的密钥一律不读值。传递＝复制，源目录不会被删。</p>

        <div class="panel">
          <div class="listhead">
            <span class="t">本机来源探测</span>
            <span class="hint" style="margin:0">结果随机器变，不是写死的名单</span>
          </div>
          <div id="sk-src" class="srclist"><span class="hint">正在探测…</span></div>
        </div>

        <div class="split">
          <div class="split-main">
            <div class="shead">
              <span class="t">清算结果</span>
              <span class="hint" id="sk-count" style="margin:0"></span>
            </div>
            <div class="sbody"><div id="sk-list"><div class="empty">正在探测…</div></div></div>
          </div>
          <aside class="split-side" id="sk-detail">
            <div class="dmain"><div class="empty">左边点一条，看内容；技能还能传到别的 Agent</div></div>
          </aside>
        </div>
      </section>

      <section id="v-pack" hidden>
        <div class="pagehead">
          <h2 class="ptitle">记忆包</h2>
          <div class="pacts">
            <button class="btn pri" onclick="doExport()">导出记忆包</button>
          </div>
        </div>
        <p class="lead">把记忆导出为 JSON 文件，跨电脑备份或发给他人导入。导入时按内容自动去重。</p>

        <div class="panel">
          <div class="listhead">
            <span class="t">① 导出范围</span>
            <span class="hint" style="margin:0">格式 hippocampus-pack v1：内容 / 类型 / 重要度 / 项目 / 时间</span>
          </div>
          <div class="row">
            <select id="pk-proj" class="proj-sel"><option value="">全部项目</option></select>
          </div>
          <div class="row" style="gap:8px;align-items:center;margin-top:12px">
            <input type="checkbox" id="pack-with-sessions" style="width:auto;margin:0">
            <span>同时导出「会话层（对话原文）」</span>
          </div>
          <p class="hint">默认只导出记忆（结论）。勾上这一项会把归档的原始对话一起打包 ——
            换电脑后原话也能恢复。导入时记忆与会话<b>两边都会自动去重</b>，重复导入不会产生第二份。</p>
        </div>

        <div class="panel">
          <div class="listhead"><span class="t">② 导入</span></div>
          <div class="row">
            <button class="btn" onclick="document.getElementById('pack-file').click()">选择文件并导入</button>
            <input type="file" id="pack-file" accept=".json" style="display:none" onchange="doImport(this)">
          </div>
          <p class="hint">只接受 Hippocampus 导出的记忆包；与现有记忆内容重复的条目会自动跳过。</p>
        </div>

        <div class="panel">
          <div class="listhead">
            <span class="t">③ 技能包</span>
            <span class="hint" style="margin:0">把经验类记忆导出成 SKILL.md</span>
          </div>
          <div class="row">
            <select id="sk-proj" class="proj-sel" style="max-width:220px"><option value="">选择项目</option></select>
            <button class="btn" onclick="skillPreview()">预览技能包</button>
            <button class="btn" onclick="skillExport()">写入 skills 目录</button>
          </div>
          <p class="hint">写进 ~/.workbuddy/skills，各 Agent 可直接读取。</p>
        </div>
        <div class="handoff-out" id="skill-out"></div>
      </section>

      <!-- 交接卡 -->
      <section id="v-handoff" hidden>
        <div class="pagehead">
          <h2 class="ptitle">项目交接卡</h2>
          <div class="pacts">
            <button class="btn pri" onclick="doHandoff()">生成交接卡</button>
            <button class="btn txt" id="hf-copy" style="display:none" onclick="copyHandoff()">复制</button>
          </div>
        </div>
        <p class="lead">按项目抽取决策 / 偏好 / 坑 / 事实，生成 markdown 卡片，切换 Agent 时贴给它即可无损续接。</p>
        <div class="panel">
          <div class="listhead">
            <span class="t">选择项目</span>
            <span class="hint" style="margin:0">留「全部项目」＝ 库里所有记忆都带上</span>
          </div>
          <div class="row">
            <select id="hf-proj" class="proj-sel"><option value="">全部项目</option></select>
          </div>
        </div>
        <div class="handoff-out" id="handoff"></div>
      </section>
    </div>
  </main>
</div>

<script>
const TL={fact:"事实",decision:"决策",preference:"偏好",skill:"经验",error:"踩坑",context:"背景",summary:"摘要"};
const TC={fact:"#0a84ff",decision:"#ffd60a",preference:"#bf5af2",skill:"#30d158",error:"#ff453a",context:"#8e8e93",summary:"#8e8e93"};
let searching=false;

/* 视图切换 */
function show(v){
  document.querySelectorAll(".nav").forEach(n=>n.classList.toggle("on",n.dataset.v===v));
  ["mem","session","audit","clean","collect","agents","skill","pack","handoff"].forEach(x=>document.getElementById("v-"+x).hidden=(x!==v));
  if(v==="session") loadSessions();
  if(v==="audit") runAudit();
  if(v==="clean"){loadSourceFiles();loadArchive();}
  if(v==="skill") loadSkills();
  try{ if(location.hash!=="#"+v) history.replaceState(null,"","#"+v); }catch(e){}
  // ⚠️ 这里不能同步调 secApply()：脚本顶部就会跑一次初始化 show()，而 SEC_FOLD
  //    定义在脚本末尾 —— 同步调用会撞上"还没定义"。延到当前 tick 之后再调。
  setTimeout(secApply,0);
  setTimeout(secApply,150);   // 右栏/列表是异步渲染的，渲染完再补一次
}
document.querySelectorAll(".nav").forEach(n=>n.onclick=()=>show(n.dataset.v));
var _h=(location.hash||"").replace("#","");
if(["mem","session","audit","clean","collect","agents","skill","pack","handoff"].indexOf(_h)>=0){ show(_h); }
// 没带锚点时落在「会话」：面板是给人看的，先看「发生过什么对话」，
// 再看从对话里提炼出的记忆碎片（碎片是给模型检索用的，不该当第一眼）。
else{ show("session"); }
function goAdd(){show("mem");document.getElementById("form").classList.add("open");
  document.getElementById("f-content").focus()}
function backToList(){document.getElementById("q").value="";show("mem");loadList()}

async function api(path,opt){const r=await fetch(path,opt);return r.json()}

async function loadStats(){
  const s=await api("/api/stats");
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
}

function esc(t){const d=document.createElement("div");d.textContent=t;return d.innerHTML}
/* esc() 走 textContent→innerHTML，不会转义双引号；写进属性值要再转一次 */
function escAttr(t){return esc(t).replace(/"/g,"&quot;")}

/* -- 主从详情面板 -- */
var LASTROWS=[], SELID=null;
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
    el.innerHTML='<div class="dhead"><h3 class="dempty-h">记忆详情</h3></div>'+
      '<div class="dmain"><div class="dempty">点左侧任意一条记忆，这里会显示完整内容、标签与操作按钮。</div></div>';
    return;
  }
  var full=String(r.content||"");
  var short=full.length>46;
  var title=short?(full.slice(0,46)+"…"):full;
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
    try{localStorage.setItem("hippocampus-theme",t)}catch(e){}
    toast(t==="light"?"已切换到白天模式":"已切换到黑夜模式","ok");
  }
}
function toggleTheme(){
  var cur=document.documentElement.getAttribute("data-theme")==="light"?"dark":"light";
  applyTheme(cur,true);
}
(function initTheme(){
  var t=null,q=(location.search||"").match(/theme=(light|dark)/);
  if(q){ t=q[1]; applyTheme(t,false); return; }   /* URL 指定优先（便于分享/截图） */
  try{t=localStorage.getItem("hippocampus-theme")}catch(e){}
  if(!t){
    t=(window.matchMedia&&window.matchMedia("(prefers-color-scheme: light)").matches)?"light":"dark";
  }
  applyTheme(t,false);
})();

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
function countUp(el,to){
  var from=parseInt(el.textContent,10)||0;
  if(from===to){el.textContent=to;return}
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
function cardHtml(r,score,idx){
  var t=String(r.content||"").replace(/\s+/g," ").trim();
  var tags=tagList(r), proj=projName(r.project);
  var tip="#"+r.id+" · P"+r.importance+" · "+agentName(r.agent)+" · "+r.created_at
    +(proj?(" · 项目 "+proj):"")+(tags.length?(" · 标签 "+tags.join("/")):"");
  return `<div class="mem lrow" id="memcard-${r.id}" style="--i:${idx||0}" title="${escAttr(tip)}"
      onclick="selectMem(${r.id})">
    <span class="mdot" style="background:${TC[r.mtype]||"#8e8e93"}"></span>
    <div class="mbody">
      <div class="mtitle">${esc(t)}</div>
      <div class="mmeta">
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
  await api("/api/pin",{method:"POST",headers:{"Content-Type":"application/json"},
    body:JSON.stringify({id:id,pinned:pinned})});
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
      return '<div class="mem"><div class="top">'+
        '<input type="checkbox" class="sfck" data-i="'+i+'">'+
        '<span class="ttag"><i style="background:#ff9f0a"></i>'+esc(f.kind)+'</span>'+
        '<span class="proj">'+esc(f.workspace)+'</span>'+
        '<span class="accent">关联 '+f.memories+' 条'+(f.exact?"（精确）":"（按工作区估算）")+'</span>'+
        '<span class="score">'+fmtSize(f.size)+' · '+esc(f.mtime)+'</span></div>'+
        '<div class="content" style="font-size:12px;color:var(--sub);word-break:break-all">'+esc(f.file_name||f.path)+'</div>'+
        (f.memory_items&&f.memory_items.length?('<div class="meta"><span>'+f.memory_items.map(function(m){return "#"+m.id+" "+esc(m.content.slice(0,26));}).join("　")+'</span></div>'):"")+
      '</div>';
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
  if(!confirm("⚠️ 将要处理 "+picked.length+" 个文件（共 "+fmtSize(total)+"）：\n\n"+list+
      "\n\n操作方式：先复制备份到 trash-backup/，再移入系统回收站（可恢复）。\n确定继续？"))return;
  sfmsg("正在备份并移入回收站（每批最多 10 个）…");progress(true);
  var r=await api("/api/sourcefiles/clean",{method:"POST",
    headers:{"Content-Type":"application/json"},
    body:JSON.stringify({paths:picked.map(function(f){return f.path})})});
  if(r.error){sfmsg("失败："+r.error,"err");return}
  sfmsg("已处理 "+r.files+" 个文件（分 "+r.batches.length+" 批）\n备份目录："+r.backup_dir+
        (r.failed&&r.failed.length?("\n失败："+r.failed.join("、")):"\n失败：无")+
        "\n可在系统回收站或备份目录找回。","ok");
  progress(false);toast("已处理 "+r.files+" 个文件（进回收站+备份）","ok");
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
      return '<div class="mem"><div class="top">'+
        '<input type="checkbox" class="cmck" data-i="'+i+'">'+
        '<span class="ttag"><i style="background:'+(TC[m.mtype]||"#8e8e93")+'"></i>'+(TL[m.mtype]||m.mtype)+'</span>'+
        (m.project?('<span class="proj">'+esc(m.project)+'</span>'):"")+
        '<span class="score">#'+m.id+' · '+esc(m.agent||"")+' · '+m.created_at.slice(0,10)+'</span></div>'+
        '<div class="content">'+esc(m.content)+'</div></div>';
    }).join("")) : '<div class="empty">没有记忆</div>';
  cmmsg("共 "+r.count+" 条可选","ok");
}
function toggleAllCM(v){document.querySelectorAll(".cmck").forEach(function(c){c.checked=v})}
async function deletePicked(){
  var ids=[];
  document.querySelectorAll(".cmck:checked").forEach(function(c){ids.push(MPICK[parseInt(c.dataset.i,10)].id)});
  if(!ids.length){cmmsg("先勾选要删除的记忆","err");return}
  if(!confirm("⚠️ 将硬删除 "+ids.length+" 条记忆（删前会自动导出全量备份）：\n\n"+
      ids.map(function(i){return "  #"+i}).join("\n")+"\n\n确定继续？"))return;
  cmmsg("正在备份并删除…");progress(true);
  var r=await api("/api/cleanup/run",{method:"POST",headers:{"Content-Type":"application/json"},
    body:JSON.stringify({ids:ids})});
  cmmsg(r.error?("失败："+r.error):("已删除 "+r.deleted+" 条\n备份："+r.backup),r.error?"err":"ok");
  progress(false);
  toast(r.error?("删除失败："+r.error):("已删除 "+r.deleted+" 条记忆"), r.error?"err":"ok");
  loadMemPick();loadStats();
}
async function loadBackups(){
  var rows=await api("/api/backups");
  document.getElementById("bk-list").innerHTML = rows.length ? rows.map(function(b){
    return '<div class="mem"><div class="top"><span class="ttag"><i style="background:#5DCAA5"></i>备份</span>'+
      '<span class="proj">'+b.files+' 个文件</span>'+
      '<span class="accent">'+fmtSize(b.size)+'</span><span class="score">'+esc(b.time)+'</span></div>'+
      '<div class="content" style="font-size:12px;color:var(--sub);word-break:break-all">'+esc(b.dir)+'</div></div>';
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
async function cleanupRun(){
  var r0=await api("/api/cleanup/preview"+_clQuery());
  if(!r0.count){cmsg("当前条件没有命中任何记忆","err");return}
  if(!confirm("将【硬删除】"+r0.count+" 条记忆，删前会自动导出备份。确定执行？"))return;
  cmsg("正在备份并删除…");
  var body={};
  var proj=document.getElementById("cl-project").value;
  var ag=document.getElementById("cl-agent").value;
  var bf=document.getElementById("cl-before").value;
  if(proj)body.project=proj;
  if(ag)body.agent=ag;
  if(bf)body.before=bf;
  if(document.getElementById("cl-sup").checked)body.only_superseded=true;
  var r=await api("/api/cleanup/run",{method:"POST",headers:{"Content-Type":"application/json"},
    body:JSON.stringify(body)});
  if(r.error){cmsg("删除失败："+r.error,"err");return}
  cmsg("已删除 "+r.deleted+" 条记忆\n备份文件："+r.backup+"\n（需要恢复就用「记忆包」页导入这个文件）","ok");
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
  el.textContent += "\n\n点「清理陈旧记忆」可删除这些孤儿（会自动备份）。";
}
async function purgeOrphans(){
  if(!confirm("删除所有「源已消失」的记忆与会话？（会自动导出备份）"))return;
  cmsg("正在备份并清理…");
  var r=await api("/api/cleanup/orphans",{method:"POST",headers:{"Content-Type":"application/json"},body:"{}"});
  if(r.error){cmsg("清理失败："+r.error,"err");return}
  cmsg("已清理：记忆 "+r.memories+" 条、会话 "+r.sessions+" 个\n消失的源："+(r.projects.join("、")||"—")+
       "\n备份文件："+r.backup,"ok");
  document.getElementById("ctx-out").textContent="";
  document.getElementById("ctx-out").style.display="none";
  refresh();
}

function gheadHtml(key, label, n){
  return '<div class="ghead' + (key === 'recent' ? ' g-recent' : '') + '" id="gh-' + key +
    '" data-k="' + key + '" onclick="toggleGroup(this.dataset.k)">' +
    '<span class="cv">▼</span><span>' + label + '</span>' +
    '<span class="cnt">' + n + '</span></div>';
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
  if(head) head.classList.toggle('collapsed', nowHidden);
  var foot = document.getElementById('gf-' + key);   // 内容下方那个按钮也跟着翻字
  if(foot) foot.textContent = nowHidden ? '展开 ▾' : '收起 ▴';
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
  await api("/api/delete",{method:"POST",headers:{"Content-Type":"application/json"},
    body:JSON.stringify({id})});
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
  el.innerHTML='<div class="foldbody">'+esc(r.markdown)+'</div>';
  foldAll();
  document.getElementById("hf-copy").style.display="inline";
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
  const nIn=rows.filter(r=>r.in_db).length;
  document.getElementById("scan-info").textContent=
    rows.length?`共发现 ${rows.length} 个候选（${nIn} 个已入库置灰）。勾选后点「入库选中」。`
               :"没有发现可采集的文件。";
  ["ck-all","ck-none","ck-go"].forEach(id=>{
    document.getElementById(id).style.display=rows.length?"inline-block":"none"});
  // 表格化（规范 Table 组件：静音表头 + 细描边行 + 彩色状态）。整行是 label，点哪都能勾。
  el.innerHTML = rows.length ? (
    '<div class="stable">'+
      '<div class="shead-row"><span></span><span>内容预览</span><span>类型</span>'+
      '<span>来源</span><span>日期</span><span>状态</span></div>'+
      rows.map(it=>`
        <label class="srow${it.in_db?" indb":""}" title="${escAttr(it.path)}">
          <span><input type="checkbox" class="ck" data-p="${escAttr(it.path)}" ${it.in_db?"disabled":""} ${it.in_db?"":"checked"}></span>
          <span class="spath">${esc(it.preview)}</span>
          <span class="cell">${esc(it.kind)}</span>
          <span class="cell">${esc(it.source)}</span>
          <span class="cell">${esc(it.date)}</span>
          <span>${it.in_db?'<span class="bdg state">已入库</span>':'<span class="bdg pin">待入库</span>'}</span>
        </label>`).join("")+
    '</div>'
  ) : '<div class="empty">没有发现可采集的文件</div>';
}
function checkAll(v){
  document.querySelectorAll(".ck:not([disabled])").forEach(c=>c.checked=v)}
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
/* 本机对话来源：只**探测并缓存**，不再往页面上摆卡片。
   来源名单仍然从磁盘问（不写死），但展示位置只剩「扫描」那句提示文案 ——
   用户明确说过不需要在会话页多一个模块，归档表单才是第一眼要看的东西。 */
async function loadConvSources(){
  try{var r=await api("/api/conv-sources");SRCS=r.sources||[];}catch(e){SRCS=[];}
}
async function runAutoScan(silent){
  if(!silent){
    var names=SRCS.filter(function(s){return s.state==="found"}).map(function(s){return s.agent});
    scanmsg(names.length?("正在扫描本机对话（"+names.join(" / ")+"）…"):"正在探测本机对话来源…");
    progress(true);
  }
  var r=await api("/api/autoscan?subagent=1&extract=1");
  progress(false);
  SCANCAND=r.candidates||[];
  var used=(r.used||[]).map(function(u){return u.agent+" "+u.sessions+" 个"});
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
          '<span class="ttag"><i style="background:'+(c.from_user?"#0a84ff":"#30d158")+'"></i>'+(c.from_user?"我":"AI")+'</span>'+
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
  loadConvSources().then(function(){ runAutoScan(true); });
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
function agentColor(a){return a==="WorkBuddy"?"#0a84ff":(a==="Trae"?"#ea4335":
  (a==="ZCode"?"#30d158":(a==="Copilot"?"#bf5af2":"#ff9f0a")))}
function agentTag(a){
  return '<span class="ttag"><i style="background:'+agentColor(a)+'"></i>'+esc(a)+'</span>';
}
function grp(title,n,note){
  return '<div class="grphead"><span class="gt">'+esc(title)+'</span>'+
    '<span class="gn">'+n+' 个'+(note?' ｜ '+esc(note):'')+'</span></div>';
}
/* 把左栏的每个 .grphead 段落变成**可折叠的独立分组**（用户要的：
   "每一类单独一个框，能整块收起来"）。做法是后处理而不是改渲染代码 ——
   段数和顺序会随探测结果变，直接改拼接字符串容易漏一处。
   包好之后直接复用记忆页那套 toggleGroup：段头点一下，.gbody 整块 display:none。 */
function wrapGroups(root){
  if(!root)return;
  var heads=Array.prototype.slice.call(root.querySelectorAll(".grphead"));
  heads.forEach(function(h,i){
    if(h.dataset.wrapped)return;
    h.dataset.wrapped="1";
    var key="skg"+i;
    h.classList.add("ghead");
    h.id="gh-"+key;
    h.onclick=function(){toggleGroup(key)};
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
    foot.textContent="收起 ▴";
    foot.onclick=function(ev){ev.stopPropagation();toggleGroup(key)};
    body.parentNode.insertBefore(foot,body.nextSibling);
  });
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
  var r,c;
  try{
    var both=await Promise.all([api("/api/skills"),api("/api/content")]);
    r=both[0]||{};c=both[1]||{};
  }catch(e){box.innerHTML='<div class="empty">探测失败</div>';return}
  SKILLS=r.skills||[];SKTGT=r.targets||[];SKSRC=r.sources||[];
  CFGS=c.configs||[];MCPS=c.mcps||[];CSRC=c.sources||[];
  PLUGINS=c.plugins||[];BACKUPS=c.backups||[];
  /* 本机磁盘上留着的插件旧版本总数。这个数**不是装饰** ——
     它是"就地清算"的度量：装了 55 个插件，磁盘上却有 130 个版本目录。 */
  var OLDVER=PLUGINS.reduce(function(n,p){return n+(p.old_count||0)},0);
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
  wrapGroups(box);
}
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
  el.innerHTML='<div class="dmain"><div class="empty">读取中…</div></div>';
  var r=await api("/api/skill/detail?path="+encodeURIComponent(SKCUR.path));
  if(r.error){el.innerHTML='<div class="dmain"><div class="empty">'+esc(r.error)+'</div></div>';return}
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
  foldAll();secApply();
}
/* 配置文件详情：正文一定是**后端脱敏过**的那份。
   面板这边不做任何还原，也拿不到未脱敏的值 —— 后端对敏感文件直接回空 text。 */
async function pickCfg(i){
  CCUR=CFGS[i];
  var el=document.getElementById("sk-detail");
  el.innerHTML='<div class="dmain"><div class="empty">读取中…</div></div>';
  var r=await api("/api/content/detail?path="+encodeURIComponent(CCUR.path));
  if(r.error){el.innerHTML='<div class="dmain"><div class="empty">'+esc(r.error)+'</div></div>';return}
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
  el.innerHTML='<div class="dmain"><div class="empty">读取本地版本…</div></div>';
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
  el.innerHTML='<div class="dmain"><div class="empty">读取中…</div></div>';
  var r=await api("/api/backup/detail?path="+encodeURIComponent(b.path));
  if(r.error){el.innerHTML='<div class="dmain"><div class="empty">'+esc(r.error)+'</div></div>';return}
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
function roleColor(r){return r==="user"?"#0a84ff":(r==="assistant"?"#30d158":(r==="raw"?"#8e8e93":"#bf5af2"))}
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
function sessionFile(inp){
  var f=inp.files[0];if(!f)return;
  var fr=new FileReader();
  fr.onload=function(){document.getElementById("s-text").value=fr.result;
    document.getElementById("s-info").textContent="已读入文件 "+f.name+"，点「解析预览」看识别结果";
    sessionParse()};
  fr.readAsText(f,"utf-8");
  inp.value="";
}
async function sessionSave(){
  if(!SPARSED||!SPARSED.length){alert("先点「解析预览」");return}
  var r=await api("/api/session/save",{method:"POST",
    headers:{"Content-Type":"application/json"},
    body:JSON.stringify({messages:SPARSED,
      title:document.getElementById("s-title").value.trim(),
      project:document.getElementById("s-project").value.trim(),
      agent:document.getElementById("s-agent").value.trim()})});
  document.getElementById("s-info").textContent = (r.ok?("已归档：会话 #"+r.id+"，"+
    (r.created?"新增":"内容已存在，跳过")+"（"+r.count+" 轮）"):("归档失败："+ (r.error||"未知错误")));
  if(r.ok)toast(r.created?("会话已归档 #"+r.id):("会话已存在，跳过"),"ok");
  SPARSED=null;document.getElementById("s-save").disabled=true;
  document.getElementById("s-text").value="";
  loadSessions();loadStats();
}
async function loadSessions(){
  // 行结构与记忆列表同一套：标题一行 + meta 一行（时间 · 来源 · 轮次/编号），操作 hover 才出现。
  // ⚠️ 必须带 lrow 类 —— 紧凑行样式挂在 .mem.lrow 上，漏了它就会掉回卡片样式
  //    （412px 的窄列放不下卡片，标题会被挤成竖排。踩过）。
  var rows=await api("/api/session/list?limit=50");
  document.getElementById("s-list").innerHTML = rows.length ? rows.map(function(s){
    var mn=s.mem_n||0;
    return '<div class="mem lrow" onclick="openSession('+s.id+')" title="'
        +escAttr((s.source_path?s.source_path+' · ':'')+'点开看原文时间线')+'">'+
      '<div class="mbody">'+
        '<div class="mtitle">'+esc(s.title)+'</div>'+
        '<div class="mmeta">'+
          '<span class="mtime">'+esc(String(s.started_at||s.created_at||"").slice(0,10))+'</span>'+
          '<span class="bdg">'+esc(s.agent||"未知来源")+'</span>'+
          (mn?('<span class="bdg src">产出 '+mn+' 条记忆</span>'):'<span class="mtags">暂无产出</span>')+
          '<span class="mtags">'+s.msg_count+' 轮 · #'+s.id+'</span>'+
          '<span class="macts">'+
            '<button class="del" onclick="event.stopPropagation();extractSession('+s.id+')">抽记忆</button>'+
            '<button class="del" onclick="event.stopPropagation();openSession('+s.id+')">查看原文</button>'+
            '<button class="del" onclick="event.stopPropagation();delSession('+s.id+')">删除</button>'+
          '</span>'+
        '</div>'+
      '</div></div>';
  }).join("") : '<div class="empty">还没有归档的会话</div>';
}
async function extractSession(sid){
  var box=document.getElementById("s-view");
  box.style.display="block";
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
  document.getElementById("s-view-h").textContent=
    "从会话 #"+sid+" 抽出 "+rows.length+" 条候选（你=优先）。勾掉不要的，再点「入库选中」。";
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
        '<span class="ttag"><i style="background:'+(c.from_user?"#0a84ff":"#30d158")+'"></i>'+(c.from_user?"我":"AI")+'</span>'+
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
  btn.textContent="展开全文 ▾";
  btn.onclick=function(ev){
    ev.stopPropagation();                       // 卡片本身可点，别让点按钮触发选中
    var nowFolded=el.classList.toggle("folded");
    btn.textContent=nowFolded?"展开全文 ▾":"收起 ▴";
  };
  el.insertAdjacentElement("afterend",btn);
}
function foldAll(){document.querySelectorAll(FOLD_SEL).forEach(foldOne)}

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
["detail","sk-detail","s-list","list"].forEach(function(id){
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
    +'<i style="background:'+(TC[m.mtype]||"#8e8e93")+'"></i><b>#'+m.id+'</b>'
    +'<span class="tchip-t">'+esc(t)+'</span></span>';
}

function renderSessionView(r){
  var el=document.getElementById("s-view");
  var msgs=r.messages||[], mems=r.memories||[], s=r.session||{};
  document.getElementById("s-view-t").textContent=s.title||"原文时间线";
  document.getElementById("s-view-h").textContent=
    "#"+s.id+" · "+msgs.length+" 轮 · 产出 "+mems.length+" 条记忆"
    +(s.agent?(" · 来自 "+s.agent):"")
    +(s.started_at?(" · "+String(s.started_at).slice(0,10)):(s.created_at?(" · "+String(s.created_at).slice(0,10)):""));
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
  foldAll();
  secApply();
}

function clearSessionView(){
  LAST_SID=null;LAST_SES=null;
  document.getElementById("s-view-t").textContent="原文时间线";
  document.getElementById("s-view-h").textContent=
    "点左侧任意会话查看原文；「抽取记忆」会在这里逐轮给出候选记忆";
  document.getElementById("s-view-acts").innerHTML="";
  var el=document.getElementById("s-view");
  el.innerHTML="";el.style.display="none";
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
  await api("/api/session/delete",{method:"POST",
    headers:{"Content-Type":"application/json"},body:JSON.stringify({sid:sid})});
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
  if(!confirm("关闭面板服务？\n\n记忆数据不受影响（都存在 hippocampus.db 里）。\n下次要用，双击桌面的 Hippocampus 图标即可重新打开。")) return;
  try{ await api("/api/shutdown",{method:"POST",headers:{"Content-Type":"application/json"},body:"{}"}); }catch(e){}
  document.documentElement.innerHTML=
    '<body class="bye">'+
      '<img src="/icon.png" alt="">'+
      '<h2>面板服务已关闭</h2>'+
      '<p>记忆数据完好，保存在 <code>hippocampus.db</code> 里。<br>'+
      '你的 AI Agent 照常能读写记忆 —— 它们不依赖这个面板。<br>'+
      '下次要管理，双击桌面的 <b>Hippocampus</b> 图标即可。</p>'+
    '</body>';
}

async function renderHealth(){
  var proj=document.getElementById("au-proj").value;
  var h=await api("/api/health"+(proj?("?project="+encodeURIComponent(proj)):""));
  // 四维扣分条：按维度顺序取 --chart-1..4（规范的图表五色就是给分类对比用的）
  var bars=Object.keys(h.penalties||{}).map(function(k,i){
    var v=h.penalties[k], pct=Math.round(v/25*100);
    return '<div class="hrow"><span class="lab">'+k+'</span>'+
      '<span class="hbar"><i style="width:'+pct+'%;background:var(--chart-'+((i%5)+1)+')"></i></span>'+
      '<span class="val">-'+v.toFixed(1)+'</span></div>';
  }).join("");
  var chips=Object.keys(h.counts||{}).map(function(k){
    var v=h.counts[k];
    return '<span class="hchip'+(v>0?" warn":"")+'">'+k+" "+v+'</span>';
  }).join("");
  var g=String(h.grade||"A").toLowerCase();
  document.getElementById("health").innerHTML=
    '<div class="hgauge">'+
      '<div class="hring" style="background:conic-gradient(var(--chart-1) 0 '+h.score+
        '%,var(--muted) '+h.score+'% 100%)">'+
        '<div class="hval"><b>'+h.score+'</b><i>/100</i></div>'+
      '</div>'+
      '<span class="hgrade '+g+'">'+h.grade+" · "+(h.grade_text||"")+'</span>'+
    '</div>'+
    '<div class="hbars">'+bars+'</div>'+
    '<div class="hchips">'+(chips||'<span class="hchip">库里还没有记忆，没有可检查的项</span>')+'</div>';
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
  downloadMd(md, "hippocampus-质检报告-" + new Date().toISOString().slice(0,10) + ".md");
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
  var h="";
  h+='<div class="listhead"><span class="t">重复记忆 · '+r.duplicates.length+' 组</span></div>';
  h+= r.duplicates.length ? r.duplicates.map(function(g,i){
      var ids=g.map(function(x){return x.id}).join(",");
      return '<div class="mem"><div class="top"><span class="ttag"><i style="background:#ffd60a"></i>重复组 '+(i+1)+'</span>'+
        '<span class="proj">'+g.length+' 条同义</span></div><div class="content">'+
        g.map(function(x){return memLine(x,"")}).join("")+'</div>'+
        '<div class="meta"><span>合并后保留最新一条，其余标记作废</span>'+
        '<span><button class="del" onclick="mergeGroup(\''+ids+'\')">合并</button></span></div></div>';
    }).join("") : '<div class="empty">没有发现重复</div>';
  h+='<div class="listhead"><span class="t">疑似同义 · '+(r.suspects||[]).length+' 对</span></div>';
  h+= (r.suspects||[]).length ? r.suspects.map(function(c){
      return '<div class="mem"><div class="top"><span class="ttag"><i style="background:#bf5af2"></i>疑似同义</span>'+
        '<span class="score">相似度 '+c.similarity+'</span></div><div class="content">'+
        memLine(c.a,"")+memLine(c.b,"")+'</div>'+
        '<div class="meta"><span>措辞不同但可能是同一件事，请人工判断</span>'+
        '<span><button class="del" onclick="mergeGroup(\''+c.a.id+','+c.b.id+'\')">合并</button></span></div></div>';
    }).join("") : '<div class="empty">没有疑似同义项</div>';
  h+='<div class="listhead"><span class="t">可能矛盾 · '+r.conflicts.length+' 对</span></div>';
  h+= r.conflicts.length ? r.conflicts.map(function(c){
      return '<div class="mem"><div class="top"><span class="ttag"><i style="background:#ff453a"></i>新旧冲突</span>'+
        '<span class="score">相似度 '+c.similarity+'</span></div><div class="content">'+
        memLine(c.older,' ── 旧版说法')+
        memLine(c.newer,' ── 新版说法')+'</div>'+
        '<div class="meta"><span>若新版说法已取代旧版，可作废旧版</span>'+
        '<span><button class="del" onclick="supersedePair('+c.older.id+','+c.newer.id+')">以新代旧</button> '+
        '<button class="del" onclick="retireOne('+c.older.id+')">作废旧的</button></span></div></div>';
    }).join("") : '<div class="empty">没有发现矛盾</div>';
  h+='<div class="listhead"><span class="t">过粗粒度 · '+(r.coarse||[]).length+' 条（整篇日志塞成一条，建议拆分）</span></div>';
  h+= (r.coarse||[]).length ? r.coarse.map(function(x){
      return '<div class="mem"><div class="top"><span class="ttag"><i style="background:#ff9f0a"></i>'+x.content.length+' 字</span>'+
        '<span class="proj">'+esc(x.project||"—")+'</span><span class="score">#'+x.id+'</span></div>'+
        '<div class="content">'+esc(x.content.slice(0,200))+'…</div>'+
        '<div class="meta"><span>拆成多条后检索更精准，原文会保留</span>'+
        '<span><button class="del" onclick="splitMem('+x.id+')">拆分成多条</button></span></div></div>';
    }).join("") : '<div class="empty">没有过粗的记忆</div>';
  h+='<div class="listhead"><span class="t">过短 · '+(r.tiny||[]).length+' 条（<20 字）</span></div>';
  h+= (r.tiny||[]).length ? r.tiny.map(function(x){
      return '<div class="mem"><div class="top"><span class="ttag"><i style="background:#8e8e93"></i>过短</span><span class="score">#'+x.id+'</span></div>'+
        '<div class="content">'+esc(x.content)+'</div>'+
        '<div class="meta"><span>信息量不足，建议补充或删除</span>'+
        '<span><button class="del" onclick="retireOne('+x.id+')">作废</button></span></div></div>';
    }).join("") : '<div class="empty">没有过短的记忆</div>';
  h+='<div class="listhead"><span class="t">元数据缺失 · 缺项目 '+(r.no_project||[]).length+' / 缺标签 '+(r.no_tags||[]).length+'</span></div>';
  h+= ((r.no_project||[]).length+(r.no_tags||[]).length) ?
      '<div class="mem"><div class="content">'+
      (r.no_project||[]).slice(0,8).map(function(x){return "#"+x.id+" 缺项目："+esc(x.content.slice(0,36))}).join("<br>")+
      ((r.no_tags||[]).slice(0,6).map(function(x){return "#"+x.id+" 缺标签："+esc(x.content.slice(0,32))}).join("<br>"))+
      '</div><div class="meta"><span>补上项目/标签能显著提升按项目检索的命中率</span></div></div>'
      : '<div class="empty">元数据齐全</div>';
  h+='<div class="listhead"><span class="t">长期未更新 · '+r.stale.length+' 条（≥'+r.stale_days+' 天）</span></div>';
  h+= r.stale.length ? r.stale.map(function(x){
      return '<div class="mem"><div class="top"><span class="ttag"><i style="background:#8e8e93"></i>'+x.days+' 天未确认</span>'+
        (x.project?('<span class="proj">'+esc(x.project)+'</span>'):"")+'</div><div class="content">'+
        memLine(x,"")+'</div><div class="meta"><span>仍然有效就续期，不再适用就作废</span>'+
        '<span><button class="del" onclick="touchOne('+x.id+')">续期</button> '+
        '<button class="del" onclick="retireOne('+x.id+')">作废</button></span></div></div>';
    }).join("") : '<div class="empty">没有过期项</div>';
  document.getElementById("audit-out").innerHTML=h;
  renderHealth();
  amsg("质检完成：扫描 "+r.scanned+" 条 · 重复 "+r.duplicates.length+" 组 · 疑似同义 "+
       (r.suspects||[]).length+" 对 · 矛盾 "+r.conflicts.length+" 对 · 过期 "+r.stale.length+" 条",
       (r.conflicts.length||r.duplicates.length)?"err":"ok");
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
async function supersedePair(oldId,newId){
  if(!confirm("把 #"+oldId+" 标记为被 #"+newId+" 取代？（旧记录保留，不再参与检索）"))return;
  await api("/api/supersede",{method:"POST",headers:{"Content-Type":"application/json"},
    body:JSON.stringify({old_id:oldId,new_id:newId})});
  amsg("已作废 #"+oldId+"（由 #"+newId+" 取代）","ok");
  runAudit();loadStats();
}
async function retireOne(id){
  if(!confirm("作废记忆 #"+id+"？（保留记录，不再参与检索）"))return;
  await api("/api/retire",{method:"POST",headers:{"Content-Type":"application/json"},
    body:JSON.stringify({id:id})});
  amsg("已作废 #"+id,"ok");
  runAudit();loadStats();
}
async function touchOne(id){
  await api("/api/touch",{method:"POST",headers:{"Content-Type":"application/json"},
    body:JSON.stringify({id:id})});
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
  if(!confirm("从 " + a.name + " 的配置里移除 Hippocampus？（会先备份原文件）"))return;
  var r=await api("/api/agent/unregister",{method:"POST",
    headers:{"Content-Type":"application/json"},
    body:JSON.stringify({name:a.name,config:a.config})});
  if(r.error){msg("移除失败：" + r.error,"err")}
  else{msg("已移除 " + name + " 的 Hippocampus 条目\n配置文件：" + r.path +
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
  var el=document.getElementById("arch-line"); if(!el)return;
  var cfg=r.cfg||{}, snaps=r.snapshots||[];
  if(!cfg.dir){
    el.innerHTML='<b style="color:#ff9f0a">⚠ 归档未启用</b> —— 为避免占用系统盘，默认不自动备份。请先选择存放位置。';
    document.getElementById("arch-hint").textContent="建议选空间大的盘（如 D 盘或移动硬盘）；本机记忆库当前 " +
      (r.db||"") ;
  }else{
    el.innerHTML='✅ 存档位置：<code>'+esc(cfg.dir)+'</code> ｜ 已有 <b>'+snaps.length+'</b> 份快照，共 '+
      (r.total/1048576).toFixed(2)+' MB ｜ 保留最近 '+cfg.keep+' 份'+
      (cfg.auto?' ｜ 自动备份：开':' ｜ 自动备份：关');
    document.getElementById("arch-hint").textContent = snaps.length ?
      ('最新一份：'+snaps[0].mtime+'（'+snaps[0].name+'）') : '还没有快照，点「立即备份」生成第一份。';
  }
  document.getElementById("arch-dir").value = cfg.dir||"";
}
async function pickArchiveDir(){
  var r=await api("/api/archive/pick");
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
  toast("已生成快照","ok");
  msg("快照完成："+r.path+"\n大小 "+(r.size/1048576).toFixed(2)+" MB ｜ 现有 "+r.count+" 份"+
      (r.removed&&r.removed.length?("\n已按保留份数清理："+r.removed.join("、")):"")+
      "\n\n"+r.note,"ok");
  loadArchive();
}

async function pickFolder(){
  msg("正在弹出系统文件夹选择框…（若无反应，请直接在输入框粘贴路径）");
  var r=await api("/api/agent/pick-folder");
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

async function loadAgents(){
  const rows=await api("/api/agents");
  AGENTS=rows;
  loadScanRoots();
  document.getElementById("agents").innerHTML=rows.map((a,i)=>{
    let dot,txt,cls,acts="",tag="";
    if(a.state==="residue"){dot="off";txt="已卸载 · 有残留配置";cls="badge-no"}
    else if(!a.installed){dot="off";txt="未安装";cls="badge-no"}
    else if(a.hippocampus_registered){dot="on";txt="已接入 Hippocampus";cls="badge-ok"}
    else if(!a.writable){dot="warn";txt="已安装 · 暂不支持自动写入（可手动配）";cls="badge-warn"}
    else{dot="warn";txt="已安装 · 未接入";cls="badge-warn"}
    if(a.source==="discovered") tag='<span class="badge-new">自动发现</span>';
    if(a.source==="manual") tag='<span class="badge-new">手动添加</span>';
    var btns=[];
    if(a.installed && a.writable){
      btns.push(a.hippocampus_registered
        ? `<button class="mini warn" onclick="unregisterAgentAt(${i})">移除接入</button>`
        : `<button class="mini" onclick="registerAgentAt(${i})">一键接入</button>`);
    }
    if(a.source==="manual" || a.source==="discovered"){
      btns.push(`<button class="mini" onclick="forgetAgentAt(${i})">移出列表</button>`);
    }
    if(btns.length) acts='<div class="acts">'+btns.join("")+'</div>';
    return `<div class="agent">
      <div class="aname"><span class="dot ${dot}"></span>${esc(a.name)}${tag}</div>
      <div class="astat ${cls}">${txt}</div>
      <div class="astat" style="color:var(--faint);word-break:break-all">${esc(a.config)}</div>
      ${acts}
    </div>`;
  }).join("");
}

function doExport(){
  const proj=document.getElementById("pk-proj").value;
  const _wsEl=document.getElementById("pack-with-sessions");
  const _ws=(_wsEl&&_wsEl.checked)?"1":"0";
  const url="/api/pack/export?include_sessions="+_ws+(proj?("&project="+encodeURIComponent(proj)):"");
  const a=document.createElement("a");a.href=url;a.click();
}

async function doImport(inp){
  const f=inp.files[0];if(!f)return;
  try{
    const pack=JSON.parse(await f.text());
    const r=await api("/api/pack/import",{method:"POST",
      headers:{"Content-Type":"application/json"},body:JSON.stringify(pack)});
    if(r.error){alert(r.error)}
    else{alert(`导入完成：新增 ${r.imported} 条，跳过重复 ${r.skipped} 条`);refresh()}
  }catch(e){alert("文件不是有效的 JSON 记忆包")}
  inp.value="";
}

document.getElementById("f-imp").addEventListener("click",function(){
  let v=+this.dataset.v%4+1;this.dataset.v=v;
  this.innerHTML="<b>"+"★".repeat(v)+"</b>"+"★".repeat(4-v);
});

function refresh(){loadStats();loadAgents();searching?doSearch():loadList()}
refresh();
</script>
</body>
</html>
"""


SELFTEST = """<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="utf-8"><title>Hippocampus 诊断页</title>
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
    def _send(self, code, body, ctype="application/json; charset=utf-8"):
        data = body.encode("utf-8") if isinstance(body, str) else body
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _json(self, obj):
        self._send(200, json.dumps(obj, ensure_ascii=False))

    def do_GET(self):
        _touch()
        u = urlparse(self.path)
        q = parse_qs(u.query)
        if u.path in ("/", "/index.html"):
            self._send(200, PAGE, "text/html; charset=utf-8")
        elif u.path == "/selftest":
            self._send(200, SELFTEST, "text/html; charset=utf-8")
        elif u.path == "/api/stats":
            self._json(hippo.stats())
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
        elif u.path == "/api/open-folder":
            # 走 q 而不是 body —— do_GET 里没有 body 这个名字（老代码在这儿 NameError）
            self._json(open_folder(q.get("path", [""])[0]))
        elif u.path == "/api/archive/pick":
            self._json(pick_folder())
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
                self.send_response(200)
                self.send_header("Content-Type", "image/png")
                self.send_header("Content-Length", str(len(_d)))
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                self.wfile.write(_d)
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
        elif u.path == "/api/autoscan":
            r = hippo.auto_scan_agents(
                include_subagent=q.get("subagent", ["1"])[0] != "0",
                extract=q.get("extract", ["1"])[0] != "0")
            self._json(r)
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
            # 配置文件 + MCP。路径只填本机实测过的（见 hippocampus.CONTENT_SOURCES）；
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
        elif u.path == "/api/agent/pick-folder":
            self._json(pick_folder())
        elif u.path == "/api/pack/export":
            proj = q.get("project", [None])[0] or None
            _ws = (q.get("include_sessions", ["0"])[0] or "0") not in ("0", "", "false")
            pack = export_pack(proj, include_sessions=_ws)
            body = json.dumps(pack, ensure_ascii=False, indent=2).encode("utf-8")
            fname = "hippocampus-pack.json"
            if proj:
                fname = f"hippocampus-pack-{quote(proj)}.json"
            self.send_response(200)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Disposition",
                             f"attachment; filename*=UTF-8''{quote(fname)}")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        else:
            self._send(404, '{"error":"not found"}')

    def do_POST(self):
        _touch()
        u = urlparse(self.path)
        length = int(self.headers.get("Content-Length", 0))
        try:
            body = json.loads(self.rfile.read(length).decode("utf-8") or "{}")
        except Exception:
            return self._send(400, '{"error":"bad json"}')
        if u.path == "/api/save":
            if not body.get("content", "").strip():
                return self._send(400, '{"error":"empty content"}')
            mid = hippo.save_memory(
                body["content"], body.get("mtype", "fact"),
                body.get("importance", 2), body.get("tags", ""),
                body.get("project", ""), "panel",
                session_id=body.get("session_id", 0),
                turn=body.get("turn", 0))
            self._json({"id": mid})
        elif u.path == "/api/delete":
            hippo.delete_memory(int(body["id"]))
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
            hippo.delete_session(int(body.get("sid", 0)))
            self._json({"ok": True})
        elif u.path == "/api/pin":
            hippo.set_pinned(int(body["id"]), int(body.get("pinned", 1)))
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
            self._json(cleanup_sourcefiles(body.get("paths", []), body.get("memory_ids", [])))
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
            if body.get("ids"):
                bak = export_backup_file("hippocampus-backup-before-cleanup")
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
            bak = export_backup_file("hippocampus-backup-before-cleanup")
            r = hippo.bulk_delete(project, agent, before, only_sup)
            r["backup"] = bak
            r["items"] = r["items"][:20]
            self._json(r)
        elif u.path == "/api/cleanup/orphans":
            o = hippo.find_orphans()
            ids = [m["id"] for m in o["memories"]]
            sessions = [s["id"] for s in o["sessions"]]
            bak = export_backup_file("hippocampus-backup-before-orphan-purge")
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
                if a["installed"] and not a["hippocampus_registered"] and a.get("writable"):
                    r = register_agent(a["name"])
                    r["name"] = a["name"]
                    results.append(r)
            self._json({"results": results,
                        "note": "" if results else "没有需要接入的 Agent（未安装或不支持自动写入）"})
        elif u.path == "/api/open-folder":
            self._json(open_folder(body.get("path", "")))
        else:
            self._send(404, '{"error":"not found"}')

    def log_message(self, *a):
        pass


def main():
    ap = argparse.ArgumentParser(description="Hippocampus 管理面板")
    ap.add_argument("--port", type=int, default=8787)
    ap.add_argument("--open", action="store_true")
    ap.add_argument("--idle-exit", type=int, default=300, metavar="秒",
                    help="闲置多少秒后自动退出，默认 300（0 = 常驻不退出）")
    a = ap.parse_args()
    global IDLE_EXIT_SEC
    IDLE_EXIT_SEC = max(0, a.idle_exit)
    url = f"http://127.0.0.1:{a.port}"
    srv = ThreadingHTTPServer(("127.0.0.1", a.port), Handler)
    if IDLE_EXIT_SEC > 0:
        print("Hippocampus 管理面板已启动: %s" % url)
        print("  · 关掉浏览器页面后，%d 分钟内无访问会自动退出（不占后台）"
              % (IDLE_EXIT_SEC // 60 or 1))
        print("  · 想让它一直开着：--idle-exit 0")
    else:
        print("Hippocampus 管理面板已启动: %s（常驻模式）" % url)
    print("  · Ctrl+C 立即停止")
    _start_watchdog()
    # 打开面板时检查一次归档（未配置目录时会直接跳过，不做任何事）
    try:
        if hippo.load_archive_cfg().get("auto"):
            _r = hippo.do_archive_snapshot()
            if _r.get("ok") and not _r.get("skipped"):
                print("  · 已生成今日快照：%s" % _r.get("path"))
    except Exception:
        pass
    if a.open:
        threading.Timer(0.8, lambda: webbrowser.open(url)).start()
    srv.serve_forever()


if __name__ == "__main__":
    main()
