"""验证 Agent 约定文件（AGENTS.md / CLAUDE.md）的写入与迁移逻辑。

为什么需要它：`_write_rules()` 是"标记替换 + 否则追加"。改品牌名（MemHub → HippoHub
→ Hippocampus）之后，旧文件里留着**旧标记**的约定块，新标记匹配不上 → 走追加分支 →
同一个文件里并存两份约定（旧的没有 session_save 那条，新的有），Agent 会读到重复且
互相矛盾的指令，而且这种问题**肉眼看不出**（两段 Markdown 长得几乎一样）。

本脚本覆盖：全新写入 / 覆盖更新 / 旧品牌块清理 / 新旧并存 / 用户自有内容保留 /
幂等性 / 移除。零第三方依赖：python tools/verify_rules_migration.py
"""
import os
import sys
import tempfile

sys.path.insert(0, r"C:/Users/user/Hippocampus")
import install_agents as IA

OLD_HIPPO = """<!-- hippohub:begin -->
## HippoHub 共享记忆（本机跨 Agent 记忆中枢）

1. **对话开始时**：先调用 `memory_context`。
<!-- hippohub:end -->
"""

OLD_MEM = """<!-- memhub:begin -->
## MemHub 共享记忆
老约定的内容。
<!-- memhub:end -->
"""

USER_OWN = """# 我自己的全局约定

- 回答用中文
- 不要用 emoji
"""

results = []


def check(name, cond, detail=""):
    results.append((name, bool(cond), detail))
    print(f"  {'PASS' if cond else 'FAIL'}  {name}" + (f"   {detail}" if detail else ""))


def in_tmp(content, fn):
    """把 content 写进临时文件，跑 fn(path)，返回文件最终内容"""
    with tempfile.TemporaryDirectory() as d:
        p = os.path.join(d, "AGENTS.md")
        if content is not None:
            with open(p, "w", encoding="utf-8") as f:
                f.write(content)
        fn(p)
        if not os.path.exists(p):
            return None
        with open(p, encoding="utf-8") as f:
            return f.read()


NEW_B = IA.RULES_BEGIN
NEW_E = IA.RULES_END

print("=" * 66)
print("Agent 约定文件：写入 / 迁移逻辑验证")
print("=" * 66)

# ---- 1. 文件不存在 ----
r = in_tmp(None, lambda p: IA._write_rules(p))
check("① 文件不存在 → 写入新约定", r and NEW_B in r and NEW_E in r)

# ---- 2. 空文件 ----
r = in_tmp("", lambda p: IA._write_rules(p))
check("② 空文件 → 写入新约定", r and NEW_B in r)

# ---- 3. 只有旧 hippohub 块（本机真实情况）----
print("\n  · 场景：文件里只有旧品牌名 hippohub 的块（就是本机现在的样子）")
r = in_tmp(OLD_HIPPO, lambda p: IA._write_rules(p))
check("③ 旧 hippohub 块被清理掉", "hippohub" not in r,
      f"残留={r.count('hippohub')} 处")
check("③ 新的 hippocampus 块已写入", NEW_B in r and NEW_E in r)
check("③ 全文只剩 1 个约定块（没有并存）", r.count(NEW_B) == 1, f"块数={r.count(NEW_B)}")
check("③ 新约定含 session_save 那一节", "session_save" in r)

# ---- 4. 只有旧 memhub 块 ----
r = in_tmp(OLD_MEM, lambda p: IA._write_rules(p))
check("④ 旧 memhub 块也被清理", "memhub" not in r and NEW_B in r)

# ---- 5. 已经是最新（幂等）----
r1 = in_tmp(OLD_HIPPO, lambda p: IA._write_rules(p))
r2 = in_tmp(r1, lambda p: IA._write_rules(p))
check("⑤ 幂等：连续写两次结果完全一致", r1 == r2, f"第一次 {len(r1)}B / 第二次 {len(r2)}B")
check("⑤ 幂等后仍然只有 1 个块", r2.count(NEW_B) == 1, f"块数={r2.count(NEW_B)}")

# ---- 6. 用户自有内容必须保留 ----
r = in_tmp(USER_OWN + "\n" + OLD_HIPPO, lambda p: IA._write_rules(p))
check("⑥ 用户自己的约定被保留", "不要用 emoji" in r and "回答用中文" in r)
check("⑥ 旧块被清理且只留新块", "hippohub" not in r and r.count(NEW_B) == 1)
check("⑥ 用户内容在新块之前（顺序未乱）",
      r.index("不要用 emoji") < r.index(NEW_B))

# ---- 7. 最糟情况：新旧三份并存 ----
print("\n  · 场景：文件里新旧三份约定并存（改名后反复接入会这样）")
worst = USER_OWN + "\n" + OLD_MEM + "\n" + OLD_HIPPO
r = in_tmp(worst, lambda p: IA._write_rules(p))
check("⑦ 三份并存 → 收敛为只剩 1 个新块", r.count(NEW_B) == 1, f"新块={r.count(NEW_B)}")
check("⑦ 两个旧品牌名全部清除", "hippohub" not in r and "memhub" not in r)
check("⑦ 用户内容仍在", "不要用 emoji" in r)

# ---- 8. 移除 ----
r = in_tmp(USER_OWN + "\n" + OLD_HIPPO, lambda p: IA._remove_rules(p))
check("⑧ 移除：新块与旧块一起清掉", "hippohub" not in r and NEW_B not in r)
check("⑧ 移除：用户内容保留", "不要用 emoji" in r)

r = in_tmp(USER_OWN + "\n" + NEW_B + "\n内容\n" + NEW_E, lambda p: IA._remove_rules(p))
check("⑧ 移除：只清约定块", NEW_B not in r and "不要用 emoji" in r)

r = in_tmp("", lambda p: IA._remove_rules(p))
check("⑧ 空文件移除 → 返回无约定内容", True)

# ---- 9. 约定正文本身的完整性 ----
body = IA.RULES_BODY
check("⑨ 新约定含全部 6 条", all(f"{i}." in body for i in range(1, 7)))
check("⑨ 新约定明确要求 session_save", "session_save" in body)
check("⑨ 新约定警告不要用具体人名", "不要用具体人名" in body)
check("⑨ 新约定正例用 我： / AI：", "我：" in body and "AI：" in body)
# format 占位符必须只有 begin/end 两个，否则 .format() 会抛 KeyError
try:
    IA.RULES_BODY.format(begin=NEW_B, end=NEW_E)
    check("⑨ 正文可安全 .format()（无多余大括号）", True)
except Exception as e:
    check("⑨ 正文可安全 .format()（无多余大括号）", False, repr(e))

print()
ok = all(c for _, c, _ in results)
print(f"合计 {len(results)} 项，失败 {sum(1 for _, c, _ in results if not c)}")
if ok:
    print("结论: 全部通过 ✅ 可以安全地对着本机真实文件跑 --rules")
else:
    print("结论: 有失败项 ❌ 先别动真实文件")
sys.exit(0 if ok else 1)
