#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Hippocampus 安装器（命令行版）
============================
给不用网页面板的人用：一条命令把自己接入本机所有 AI Agent。

  python install_agents.py --list              # 看看本机装了哪些 Agent
  python install_agents.py --all               # 全部接入（自动备份 + 只合并不覆盖）
  python install_agents.py --install TraeWork  # 只接入指定 Agent
  python install_agents.py --uninstall         # 全部移除接入
  python install_agents.py --verify            # 真实 MCP 握手验证
  python install_agents.py --rules             # 写入「记忆使用约定」（~/.agents/AGENTS.md 等）
  python install_agents.py --rules --rules-cwd # 同时写入当前项目的 AGENTS.md
  python install_agents.py --uninstall-rules   # 移除该约定

复用 panel.py 里的 Agent 注册表，保证面板与命令行行为完全一致。
"""
import argparse
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

try:
    import panel as P
except Exception as e:  # pragma: no cover
    print(f"无法加载 panel.py：{e}")
    sys.exit(1)

RULES_BEGIN = "<!-- hippocampus:begin -->"
RULES_END = "<!-- hippocampus:end -->"
# 改名前留下的旧约定块。写入前必须先剔除 —— 否则新标记匹配不上旧标记时，
# _write_rules() 会走"追加"分支，结果是同一个文件里并存两份约定：
# 旧的（没有 session_save 那条）在前、新的在后，Agent 读到重复且互相矛盾的指令。
# 品牌演进：MemHub → HippoHub → Hippocampus（.gitignore 里的 memhub.db / hippohub.db 是同一段历史的痕迹）
LEGACY_RULES = (
    ("<!-- hippohub:begin -->", "<!-- hippohub:end -->"),
    ("<!-- memhub:begin -->", "<!-- memhub:end -->"),
)


def _strip_legacy(text):
    """剔除历史品牌名留下的约定块（含其前后多余空行），返回清理后的文本"""
    for b, e in LEGACY_RULES:
        while b in text and e in text:
            head, rest = text.split(b, 1)
            if e in rest:
                text = head.rstrip() + "\n" + rest.split(e, 1)[1].lstrip("\n")
            else:
                break
    return text


RULES_BODY = """{begin}
## Hippocampus 共享记忆（本机跨 Agent 记忆中枢）

本项目/本机已接入 Hippocampus MCP 服务。它有**两层**：会话层存原话（给人看），记忆层存结论（给模型用）。请遵守以下约定：

1. **对话开始时**：先调用 `memory_context`（可带 project 参数）拿到常驻记忆与近期重点，再开始工作。
2. **出现新的决策 / 踩坑 / 用户偏好**：调用 `memory_save` 写入（类型选 decision / error / preference），不要只在对话里说。
3. **需要历史背景**：用 `memory_search` 搜记忆、`session_recall` 搜历史对话原文；不要凭空猜测之前谈过什么。
4. **对话告一段落时**：调用 `session_save` 归档本次对话原文（会话层）。触发时机：用户说「存一下 / 归档 / 记录这次」、完成一个阶段性任务、或对话已超过 10 轮。
   - `transcript` 必须**逐轮带说话人前缀**，每轮开头一行，例如：
     我：用户的原话
     AI：你的回复
   - 前缀只用 `我：`（=用户）和 `AI：`（=你自己）；也可用 `用户：` / `助手：`。
     **不要用具体人名**（如 `建勋：`、`COLE：`）—— 解析器认不出，整段会退化成一条没有轮次的原始记录，面板时间线就显示不出来。
   - `title` 起一个日后能认出来的名字，`project` 填当前项目。同一段对话重复归档会被指纹自动拦截，不会写重。
5. **切换项目或交接给别的 Agent**：调用 `memory_handoff` 生成交接卡。
6. 记忆库是**本机共享**的：你在 WorkBuddy / Trae / ZCode / Claude Code / Codex 等任意一端写入的内容，其他 Agent 都能读到。
{end}"""


def _rules_targets(include_cwd=False):
    home = os.path.expanduser("~")
    out = [os.path.join(home, ".agents", "AGENTS.md")]
    if os.path.isdir(os.path.join(home, ".claude")):
        out.append(os.path.join(home, ".claude", "CLAUDE.md"))
    if os.path.isdir(os.path.join(home, ".codex")):
        out.append(os.path.join(home, ".codex", "AGENTS.md"))
    if include_cwd:
        out.append(os.path.join(os.getcwd(), "AGENTS.md"))
    return out


def _write_rules(path):
    block = RULES_BODY.format(begin=RULES_BEGIN, end=RULES_END)
    old = ""
    if os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            old = f.read()
    stripped = _strip_legacy(old)
    if RULES_BEGIN in stripped and RULES_END in stripped:
        head = stripped.split(RULES_BEGIN)[0]
        tail = stripped.split(RULES_END, 1)[1]
        new = head + block + tail
        action = "已更新"
    else:
        new = (stripped.rstrip() + "\n\n" + block + "\n") if stripped.strip() else (block + "\n")
        action = "已写入"
    # 顺带清掉旧品牌块：动作里标注出来，避免"已更新"却留下两份约定这种静默问题
    if stripped != old:
        action += "（并清理了旧品牌名残留）"
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(new)
    return action


def _remove_rules(path):
    if not os.path.exists(path):
        return "不存在"
    with open(path, encoding="utf-8") as f:
        old = f.read()
    had = RULES_BEGIN in old or any(b in old for b, _ in LEGACY_RULES)
    if not had:
        return "无约定内容"
    text = _strip_legacy(old)
    if RULES_BEGIN in text:
        head = text.split(RULES_BEGIN)[0].rstrip()
        tail = text.split(RULES_END, 1)[1].lstrip("\n")
        text = (head + "\n" + tail).strip() + "\n" if (head or tail.strip()) else ""
    with open(path, "w", encoding="utf-8") as f:
        f.write(text)
    return "已移除"


def cmd_list():
    rows = P.scan_agents()
    print("本机 Agent 扫描结果")
    print("-" * 72)
    for a in rows:
        state = {"installed": "已安装", "residue": "残留", "absent": "未安装"}[a["state"]]
        mark = "已接入" if a["hippocampus_registered"] else ("可接入" if (a["installed"] and a["writable"]) else "")
        print(f"  {a['name']:<22} {state:<6} {mark:<6} {a['config']}")
    n = sum(1 for a in rows if a["installed"])
    print("-" * 72)
    print(f"共 {len(rows)} 款产品，已安装 {n} 款")


def cmd_install(names=None):
    rows = [a for a in P.scan_agents() if a["installed"] and a["writable"]]
    if names:
        want = set(names)
        rows = [a for a in rows if a["name"] in want]
    todo = [a for a in rows if not a["hippocampus_registered"]]
    if not todo:
        print("没有需要接入的 Agent（未安装或已接入）")
        return
    for a in todo:
        r = P.register_agent(a["name"], a["config"])
        if r.get("error"):
            print(f"  跳过 {a['name']}：{r['error']}")
        else:
            bak = f"\n      备份: {r['backup']}" if r.get("backup") else "（新建文件）"
            print(f"  已接入 {a['name']} -> {r['path']}{bak}")


def cmd_uninstall(names=None):
    rows = [a for a in P.scan_agents() if a["hippocampus_registered"]]
    if names:
        want = set(names)
        rows = [a for a in rows if a["name"] in want]
    if not rows:
        print("没有已接入的 Agent")
        return
    for a in rows:
        r = P.unregister_agent(a["name"], a["config"])
        print(f"  {a['name']}：{'已移除' if r.get('ok') else r.get('error')}")


def cmd_verify():
    r = P.verify_mcp()
    if r.get("ok"):
        print("MCP 服务正常")
        print(f"  服务器: {r['server']}   工具数: {len(r['tools'])}")
        print(f"  工具: {', '.join(r['tools'])}")
        print(f"  解释器: {r['python']}")
    else:
        print(f"MCP 服务异常: {r.get('error')}")


def cmd_rules(remove=False, include_cwd=False):
    for p in _rules_targets(include_cwd):
        try:
            action = _remove_rules(p) if remove else _write_rules(p)
            print(f"  {action}: {p}")
        except Exception as e:
            print(f"  失败 {p}：{e}")


def main():
    ap = argparse.ArgumentParser(description="Hippocampus 安装器")
    ap.add_argument("--list", action="store_true", help="扫描本机 Agent")
    ap.add_argument("--all", action="store_true", help="接入全部已安装的 Agent")
    ap.add_argument("--install", nargs="*", metavar="NAME", help="接入指定 Agent")
    ap.add_argument("--uninstall", nargs="*", metavar="NAME", help="移除接入")
    ap.add_argument("--verify", action="store_true", help="MCP 握手验证")
    ap.add_argument("--rules", action="store_true", help="写入记忆使用约定（用户级）")
    ap.add_argument("--rules-cwd", action="store_true", help="同时写入当前目录的 AGENTS.md")
    ap.add_argument("--uninstall-rules", action="store_true", help="移除记忆使用约定")
    a = ap.parse_args()
    if a.list:
        cmd_list()
    elif a.all:
        cmd_install()
    elif a.install is not None:
        cmd_install(a.install or None)
    elif a.uninstall is not None:
        cmd_uninstall(a.uninstall or None)
    elif a.verify:
        cmd_verify()
    elif a.rules or a.rules_cwd:
        cmd_rules(False, a.rules_cwd)
    elif a.uninstall_rules:
        cmd_rules(True)
    else:
        ap.print_help()
        print("\n提示：先跑 --list 看本机有哪些 Agent，再跑 --all 一键接入。")


if __name__ == "__main__":
    main()
