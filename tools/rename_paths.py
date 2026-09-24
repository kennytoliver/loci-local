# -*- coding: utf-8 -*-
"""第 2 步：本地路径/标识符改名 —— hippocampus.* → loci.*（含环境变量、MCP 服务名、包格式名）

为什么又是"逐条显式替换"而不是 sed：
  仓库里 `hippocampus` 出现在 4 类完全不同的语境，处理方式各不相同：
    ① 文件名/路径   hippocampus.py / hippocampus.db / hippocampus.bat    → 全部改
    ② 环境变量      HIPPOCAMPUS_DB / HIPPOCAMPUS_AGENT                   → 改，但**代码里保留读旧名**
    ③ MCP 服务名    "hippocampus"（= 工具前缀 mcp__hippocampus__*）       → 改
    ④ 历史痕迹      .gitignore / CHANGELOG 里的旧条目、install_agents 的 LEGACY_RULES
                    → **故意保留**（删了就认不出老配置/老标记）
  一条无脑全局替换会把 ④ 一起抹掉，于是老配置和老 AGENTS.md 标记全部失配。

用法：python tools/rename_paths.py [--apply]
幂等：已应用的条目自动跳过，可重复跑。
"""
import os
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# 顺序敏感：更长的、更具体的必须排在更短的前面
RULES = [
    # ── ① 文件名 / 路径 ──
    ("hippocampus-panel.bat", "loci-panel.bat"),
    ("hippocampus.bat", "loci.bat"),
    ("hippocampus.db.bak-", "loci.db.bak-"),
    ("hippocampus.db", "loci.db"),
    ("hippocampus.py", "loci.py"),
    ("hippocampus-backup-", "loci-backup-"),
    ("hippocampus-zcode-ro", "loci-zcode-ro"),
    # ── ② 环境变量 ──
    ("HIPPOCAMPUS_DB", "LOCI_DB"),
    ("HIPPOCAMPUS_AGENT", "LOCI_AGENT"),
    # ── ③ 服务名 / 模块名 / 包格式 ──
    ("import hippocampus as hippo", "import loci as hippo"),
    ("import hippocampus as h", "import loci as h"),
    ("import hippocampus", "import loci"),
    ("hippocampus-panel", "loci-panel"),
    ("hippocampus-pack", "loci-pack"),
    ('servers.pop("hippocampus"', 'servers.pop("loci"'),
    ('name == "hippocampus"', 'name == "loci"'),
    ('"name": "hippocampus"', '"name": "loci"'),
    ("claude mcp add hippocampus", "claude mcp add loci"),
    ("没有 hippocampus 条目", "没有 loci 条目"),
    ("<!-- hippocampus:begin -->", "<!-- loci:begin -->"),
    ("<!-- hippocampus:end -->", "<!-- loci:end -->"),
    # ── ④ 代码标识符 / 其它环境变量 / 临时文件名 ──
    ("hippocampus_registered", "loci_registered"),
    ("HIPPOCAMPUS_LOG_CONN", "LOCI_LOG_CONN"),
    ("HIPPOCAMPUS_TEST_SKIP_DB", "LOCI_TEST_SKIP_DB"),
    ("hippocampus-test", "loci-test"),
    ("hippocampus_test_", "loci_test_"),
    ("hippocampus_check", "loci_check"),
    ("hippocampus-scan-gate", "loci-scan-gate"),
    ("hippocampus-zcode-backfill", "loci-zcode-backfill"),
    ("hippocampus.APP_VERSION", "loci.APP_VERSION"),
    ("hippocampus.CONTENT_SOURCES", "loci.CONTENT_SOURCES"),
    ("hippocampus-theme", "loci-theme"),
    # ── ⑤ 品牌词（最后做，避免吃掉上面那些具体规则）──
    ("Hippocampus", "Loci"),
    ("海马体", "忆宫"),
    # ── ⑥ 剩下这些是"散装"的旧服务名（预演扫描扫出来的，逐条加）──
    ('servers["hippocampus"]', 'servers["loci"]'),
    ('"hippocampus" not in servers', '"loci" not in servers'),
    ("hippocampus-backup", "loci-backup"),
    ("hippocampus-质检报告-", "loci-质检报告-"),
    ('== "hippocampus"', '== "loci"'),
    ('"hippocampus", "panel"', '"loci", "panel"'),
    ("没有 hippocampus 这个工具", "没有 loci 这个工具"),
    ("新的 hippocampus 块", "新的 loci 块"),
    ("把 hippocampus 写入指定 Agent 的 MCP 配置", "把 loci 写入指定 Agent 的 MCP 配置"),
    ("移除指定 Agent 配置里的 hippocampus 条目", "移除指定 Agent 配置里的 loci 条目"),
    # loci.py 内部生成物 / CLI 提示符（这些是"用户会看到的字符串"，也要换）
    ('"hippocampus-" +', '"loci-" +'),
    ('"hippocampus-%s.db"', '"loci-%s.db"'),
    ('input("hippocampus> ")', 'input("loci> ")'),
]

# 这些文件**故意保留**旧名字（历史痕迹 / 兼容代码 / 迁移工具自身）
KEEP_OLD = {".gitignore", "CHANGELOG.md", "tools/rename_brand.py", "tools/rename_paths.py"}
# 这几个文件里允许残留旧串（要有对应注释解释为什么）
ALLOW_OLD = KEEP_OLD | {"install_agents.py", "loci.py", "tools/probe_session_search.js"}

SKIP_DIRS = (".git", "docs", "backup-", "trash-backup", "__pycache__")


def tracked():
    r = subprocess.run(["git", "ls-files"], capture_output=True, text=True, cwd=ROOT)
    return [x.strip() for x in (r.stdout or "").splitlines() if x.strip()]


def wanted(rel):
    # ⚠️ 这里踩过一次坑：原来写 `rel.startswith(d)` 配 SKIP_DIRS 里的 ".git"，
    #    结果把 **.github/workflows/ci.yml 也一起跳过了**（".git" 是 ".github" 的前缀）
    #    → CI 里那条 `py_compile hippocampus.py` 没被改到。必须按目录边界匹配。
    for d in SKIP_DIRS:
        if rel == d or rel.startswith(d.rstrip("/") + "/"):
            return False
    return rel.endswith((".py", ".md", ".js", ".yml", ".yaml", ".json", ".bat", ".sh", ".txt")) \
        or os.path.basename(rel) in (".gitignore", "LICENSE")


def main():
    apply_ = "--apply" in sys.argv
    files = [f for f in tracked() if wanted(f)]
    changed, total, ends = {}, 0, {}
    for rel in files:
        p = os.path.join(ROOT, rel)
        raw = open(p, "rb").read()
        crlf = raw.count(b"\r\n") > 0          # ⚠️ 必须保换行符，否则 diff 全是噪声
        txt = raw.decode("utf-8")
        if crlf:
            txt = txt.replace("\r\n", "\n")
        orig = txt
        for old, new in RULES:
            if old in txt:
                n = txt.count(old)
                txt = txt.replace(old, new)
                total += n
                print("  %-26s %-26s x%d" % (rel, old, n))
        if txt != orig:
            changed[rel] = txt
            ends[rel] = crlf

    print("\n受影响文件 %d 个，替换 %d 处" % (len(changed), total))

    print("\n=== 扫描残留（应当只剩「故意保留」的那几处）===")
    bad = []
    for rel, txt in list(changed.items()) + [(f, open(os.path.join(ROOT, f), encoding="utf-8").read())
                                             for f in files if f not in changed]:
        if rel in ALLOW_OLD:
            continue
        for kw in ("hippocampus", "HIPPOCAMPUS", "海马体"):
            if kw in txt:
                for i, ln in enumerate(txt.splitlines(), 1):
                    if kw in ln:
                        bad.append("%s:%d  %s" % (rel, i, ln.strip()[:96]))
    if bad:
        print("⚠️ 以下位置仍有旧名，需要人工确认是「该改没改」还是「故意保留」：")
        for b in bad[:40]:
            print("   " + b)
        print("   共 %d 处" % len(bad))
    else:
        print("  干净 ✅")

    if not apply_:
        print("\n（预演模式。加 --apply 才真正写盘）")
        return 0
    for rel, txt in changed.items():
        if ends[rel]:
            txt = txt.replace("\n", "\r\n")
        with open(os.path.join(ROOT, rel), "wb") as fh:
            fh.write(txt.encode("utf-8"))
    print("\n✅ 已写入 %d 个文件（换行符按原样保留）" % len(changed))
    return 0


if __name__ == "__main__":
    sys.exit(main())
