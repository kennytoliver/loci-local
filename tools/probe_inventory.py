# -*- coding: utf-8 -*-
"""一次性探针：数一数本机各 Agent 到底有多少可清算的东西。
只为做决策用，不进产品代码。"""
import os, json, glob

H = os.path.expanduser("~")


def cnt_dir(p, pattern="**/*"):
    if not os.path.isdir(p):
        return None
    n = 0
    for f in glob.glob(os.path.join(p, pattern), recursive=True):
        if os.path.isfile(f):
            n += 1
    return n


def has(p):
    return "有" if os.path.exists(p) else "—"


def jkeys(p):
    try:
        d = json.load(open(p, encoding="utf-8"))
        return d if isinstance(d, dict) else {}
    except Exception:
        return None


rows = []


def add(name, root, cfg=None, skills=None, plugins=None, mcp=None):
    rows.append((name, has(root), cfg, skills, plugins, mcp))


# WorkBuddy
wb = os.path.join(H, ".workbuddy")
mj = jkeys(os.path.join(wb, "mcp.json")) or {}
add("WorkBuddy", wb,
    cfg=", ".join(sorted(os.path.basename(x) for x in glob.glob(os.path.join(wb, "*.json")))),
    skills=cnt_dir(os.path.join(wb, "skills"), "*/SKILL.md"),
    plugins=cnt_dir(os.path.join(wb, "plugins"), "*/SKILL.md"),
    mcp="mcpServers=%d" % len(mj.get("mcpServers", {})))

# ZCode
zc = os.path.join(H, ".zcode")
zcj = jkeys(os.path.join(zc, "cli", "mcp.json")) or jkeys(os.path.join(zc, "mcp.json")) or {}
zc_cfg = sorted(set(os.path.basename(x) for x in glob.glob(os.path.join(zc, "**", "*.json"), recursive=True)))
add("ZCode", zc,
    cfg=", ".join(zc_cfg)[:110],
    skills=cnt_dir(os.path.join(zc, "cli", "skills"), "*/SKILL.md"),
    plugins=cnt_dir(os.path.join(zc, "plugin-workspace"), "**/*"),
    mcp=str(list(zcj.keys()))[:60])

# CodeBuddy
cb = os.path.join(H, ".codebuddy")
cb_cfg = sorted(set(os.path.basename(x) for x in glob.glob(os.path.join(cb, "**", "*.json"), recursive=True)))
add("CodeBuddy", cb,
    cfg=(", ".join(cb_cfg)[:110] or "无"),
    skills=cnt_dir(os.path.join(cb, "skills"), "*/SKILL.md") or "—",
    plugins=cnt_dir(os.path.join(cb, "plugins"), "**/*") or "—",
    mcp="—")

# Trae
tr = os.path.join(H, ".trae-cn")
trm = jkeys(os.path.join(tr, "mcp.json")) or {}
trp = jkeys(os.path.join(tr, "installed-plugins.json"))
add("Trae", tr,
    cfg="mcp.json/plugin-config.json/installed-plugins.json/argv.json",
    skills=cnt_dir(os.path.join(tr, "builtin_skills"), "*/SKILL.md"),
    plugins=(len(trp) if isinstance(trp, (list, dict)) else "?"),
    mcp=str(len(trm.get("mcpServers", trm))))

# Copilot
cp = os.path.join(H, ".copilot")
cfgp = os.path.join(cp, "config.json")
raw = open(cfgp, encoding="utf-8").read() if os.path.exists(cfgp) else ""
add("Copilot", cp,
    cfg="config.json(JSONC %d行, 注释:%s)" % (raw.count("\n") + 1, "有" if "//" in raw else "无"),
    skills="—", plugins="—", mcp="—")

# 缺席的
for name, d in [("Claude Code", ".claude"), ("Codex", ".codex"), ("Cursor", ".cursor"),
                ("Kilo Code", ".kilocode"), ("OpenCode", ".config/opencode"),
                ("Goose", ".config/goose"), ("Amp", ".config/amp"),
                ("Windsurf", ".codeium/windsurf"), ("Antigravity", ".gemini/antigravity"),
                ("Kiro", ".kiro"), ("Hermes", ".hermes"), ("OpenClaw", ".openclaw"),
                ("MiniMax", ".minimax")]:
    p = os.path.join(H, d)
    rows.append((name, has(p), "—", "—", "—", "—" if not os.path.exists(p) else "有"))

print("%-12s %-4s %-44s %-6s %-6s %s" % ("产品", "目录", "配置文件", "技能", "插件", "MCP"))
print("-" * 100)
for r in rows:
    print("%-12s %-4s %-44s %-6s %-6s %s" % (r[0], r[1], str(r[2])[:44], str(r[3]), str(r[4]), r[5]))
