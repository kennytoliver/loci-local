# -*- coding: utf-8 -*-
"""品牌改名工具：Hippocampus / 海马体 → Loci / 忆宫

当前覆盖**第 1 步（对外文档层）**：README.md / README_EN.md / USAGE.md / LICENSE / tools/publish.py。
后续步骤（面板界面文案、代码标识符）继续往 PLAN 里加条目即可，机制不变。

为什么要写脚本而不是满仓库 sed：
  仓库里 213 处 "hippocampus" 里，**大部分是代码标识符**（hippocampus.py / hippocampus.db /
  HIPPOCAMPUS_DB / HIPPOCAMPUS_AGENT / claude mcp add hippocampus / hippocampus_registered），
  这些**一个都不能改**（见 install_agents.py 的 RULES_BEGIN 与 4 份 Agent 的 MCP 配置）。
  全局替换会把项目当场改坏。所以这里只做**逐条显式替换**，且每条都要求"必须命中预期次数"，
  漏一条就报错退出 —— 不允许静默 no-op。

用法：python tools/_rename_step1.py [--apply]
"""
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# (文件, 旧串, 新串, 期望命中次数)
PLAN = [
    # ── README.md ──
    ("README.md", 'alt="Hippocampus"', 'alt="Loci"', 1),
    ("README.md", "<b>I never forget.</b>", "<b>a memory palace your agents share.</b>", 1),
    ("README.md", "# Hippocampus（海马体）— 个人跨 Agent 记忆中枢",
                  "# Loci（忆宫）— 个人跨 Agent 记忆中枢", 1),
    ("README.md", "### Hippocampus 的做法：", "### Loci 的做法：", 1),
    ("README.md", "Hippocampus 把这件事做成本地的一个 SQLite 文件",
                  "Loci 把这件事做成本地的一个 SQLite 文件", 1),
    ("README.md", "哪些已接入 Hippocampus", "哪些已接入 Loci", 1),
    ("README.md", "git clone https://github.com/<你的名字>/hippocampus.git\ncd hippocampus",
                  "git clone https://github.com/kennytoliver/loci-local.git\ncd loci-local", 1),
    ("README.md", "MIT © Hippocampus contributors.", "MIT © Loci contributors.", 1),

    # ── README_EN.md ──
    ("README_EN.md", 'alt="Hippocampus"', 'alt="Loci"', 1),
    ("README_EN.md", "<b>I never forget.</b>", "<b>a memory palace your agents share.</b>", 1),
    ("README_EN.md", "# Hippocampus\n", "# Loci\n", 1),
    ("README_EN.md", "Hippocampus fixes that:", "Loci fixes that:", 1),
    # ASCII 架构图：Hippocampus(11 字符) → Loci(4)，补 7 个空格保持竖线对齐
    ("README_EN.md", "┼──►  Hippocampus  ──►", "┼──►  Loci         ──►", 1),
    ("README_EN.md", "### What Hippocampus does:", "### What Loci does:", 1),
    ("README_EN.md", "Hippocampus keeps it in a single local SQLite file",
                    "Loci keeps it in a single local SQLite file", 1),
    ("README_EN.md", "git clone https://github.com/<you>/hippocampus.git\ncd hippocampus",
                    "git clone https://github.com/kennytoliver/loci-local.git\ncd loci-local", 1),
    ("README_EN.md", "Hippocampus reads the conversation stores",
                    "Loci reads the conversation stores", 1),
    ("README_EN.md", "Hippocampus writes a plain-language usage convention",
                    "Loci writes a plain-language usage convention", 1),
    ("README_EN.md", "Hippocampus is an independent implementation",
                    "Loci is an independent implementation", 1),

    # ── USAGE.md ──
    ("USAGE.md", "# Hippocampus 操作手册", "# Loci（忆宫）操作手册", 1),
    ("USAGE.md", "给**另一台电脑上的 Hippocampus**", "给**另一台电脑上的 Loci**", 1),
    ("USAGE.md", "云笔记 / 云盘 | Hippocampus |", "云笔记 / 云盘 | Loci |", 1),

    # ── LICENSE ──
    ("LICENSE", "Copyright (c) 2026 Hippocampus contributors",
                "Copyright (c) 2026 Loci contributors", 1),

    # ── tools/publish.py ──
    ("tools/publish.py", 'REPO = "hippocampus-mcp"', 'REPO = "loci-local"', 1),
]

# 这些串**绝对不能出现在改动后**的文档里（踩过就知道：全局替换的典型事故）
FORBIDDEN = ["Loci.py", "Loci.db", "LOCI_DB", "LOCI_AGENT", "mcp add loci"]

apply_ = "--apply" in sys.argv
cache, ends, bad, total = {}, {}, [], 0
for rel, old, new, want in PLAN:
    if rel not in cache:
        p = os.path.join(ROOT, rel)
        raw = open(p, "rb").read()
        # ⚠️ 必须按字节读、按字节写，并保留原换行符：
        #    本机 core.autocrlf=true，工作区是 CRLF、索引里是 LF。用文本模式写会把 CRLF 变 LF，
        #    LICENSE 那种"索引里本来就是 CRLF"的文件会整篇变成差异（实测 42 行全红）——
        #    真改动只有 1 行，却看起来像重写了整个文件。
        crlf = raw.count(b"\r\n") > 0
        ends[rel] = crlf
        txt = raw.decode("utf-8")
        if crlf:
            txt = txt.replace("\r\n", "\n")
        cache[rel] = txt
    n = cache[rel].count(old)
    if n == 0 and cache[rel].count(new) >= want:
        # 幂等：这条之前已经改过了。**不算失败** —— 允许重复跑，
        # 否则改到第 3 步时重跑一次，前面已完成的条目会全部报错，看不出哪条是真漏了。
        print("  · %-16s 已应用，跳过：%s" % (rel, new[:44]))
        continue
    if n != want:
        bad.append("%s：期望命中 %d 次，实际 %d 次 → %r" % (rel, want, n, old[:50]))
        continue
    cache[rel] = cache[rel].replace(old, new)
    total += n
    print("  ✓ %-16s %s" % (rel, old[:52].replace("\n", "⏎")))

if bad:
    print("\n❌ 有 %d 条没按预期命中，**没有写入任何文件**：" % len(bad))
    for b in bad:
        print("   " + b)
    sys.exit(1)

for rel, txt in cache.items():
    for f in FORBIDDEN:
        if f in txt:
            print("❌ %s 出现禁止串 %r —— 中止" % (rel, f))
            sys.exit(1)

print("\n合计替换 %d 处 / %d 个文件" % (total, len(cache)))
if apply_:
    for rel, txt in cache.items():
        if ends[rel]:
            txt = txt.replace("\n", "\r\n")
        with open(os.path.join(ROOT, rel), "wb") as fh:
            fh.write(txt.encode("utf-8"))
    print("✅ 已写入（换行符按原样保留：%s）"
          % ", ".join("%s=%s" % (k, "CRLF" if v else "LF") for k, v in ends.items()))
else:
    print("（预演模式。加 --apply 才真正写盘）")
