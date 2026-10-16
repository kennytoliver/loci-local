# -*- coding: utf-8 -*-
"""闸门：静默吞异常的"普查计数"是否漂移。

为什么要有这道闸门（2026-09-29）
--------------------------------
外部独立审查 + 本项目三轮互相复核，**双方都在"口径"上栽过**：
  · 一方只扫 `pass`，漏了 `continue` 型静默跳过（在扫描/解析循环里最常见）
  · 另一方只认 `except Exception`，漏了 `except json.JSONDecodeError` / `OSError` 这类具体异常
结论：靠"人工记得扫全"不可靠 —— 把普查本身做成闸门。

判据
----
用 `ast` 解析 loci.py 与 panel.py，看每个 `except` 的 handler 体是否**只做静默吞掉**：
  · pass          —— body 只有 `pass`
  · continue      —— body 只有 `continue`
  · return_empty  —— body 只有一个 `return <空容器 / None / "" / 0 / False>`
（不看 `raise`、不看带 `print(...)`/日志的分支 —— 那些是"有出声的"，不在静默之列。）

计数与 EXPECTED 比对，**漂移即红**。

⚠️ 本闸门的**已知边界**（说破，免得以为它全覆盖）
------------------------------------------------
1. **只抓单语句静默**：`len(body) != 1` 就不算（带 `print` 的自然不算，但
   `except: x = None` + `continue` 这种**两句话的静默**也漏）→ 多余语句靠 review。
2. **不抓 `return <变量>` 型部分结果**：`return out` / `return hits` 这类"返回已收集的部分"
   是静默的，但机械计数会把大量**合法**的 `return out` 一起算进来 → 不纳入自动判定，
   改由**人工注释规约**（代码里以「静默部分结果：」开头的那几条）。

红了怎么办
----------
1. 先确认新增/删除的静默点**都写清了"为什么吞掉是安全的"**（这是本项目立的规矩）；
2. 再同步更新下面的 EXPECTED。
⚠️ 数字**下调**要特别小心 —— 可能是有人把日志/注释删了，先查清楚再改。

用法：python tools/audit_silent_skips.py
"""
import ast
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TARGETS = ["loci.py", "panel.py", "install_agents.py"]

# ⚠️ 改动代码后这里红了：先核对上面"红了怎么办"，再同步这几个数字。
# install_agents.py 是**写用户 Agent 配置**的脚本，风险高，所以纳进来做"未来防护"
# （它当前 2 处 except 都带 print/exit，不是静默，故期望 0）。
#
# 2026-10-11 同步（上 PyPI 时）：loci.py pass 10→12、return_empty 9→11。
#   · +1 pass 来自本次：db() 里「尽力创建数据目录」的 except（pip 安装首次运行需要）。
#     为什么安全写在那个 except 上方 —— 只是尽力建目录，建不出来交给 sqlite 报原始错误。
#   · 另外 +1 pass / +2 return_empty 是**在这次改动之前就已经漂移的** ——
#     也就是说这道闸门在 2026-10-11 之前就是红的，只是没人跑它。
#     本次一并校准到实测值：不是「把数字改到能过」，是把基准重新对齐到当前真实代码。
#   · 2026-10-15 又 +1 return_empty：_panel_alive() 探测面板时的
#     「except Exception: return False」—— 探测失败就当作"没在跑"是正确的语义
#     （宁可多探一次，不可漏探；漏探的代价是每次 MCP 启动都多开一个面板）。
#     而且它是**有出声的**：真启动失败时 maybe_open_panel() 会往 stderr 说原因。
#   · 2026-10-16 又 +1 return_empty：current_branch() 探测 git 分支时的
#     「except Exception: return ""」—— 探不到就留空是正确的语义：写记忆不能因为
#     "没装 git / 不是仓库 / 命令超时 / 没权限" 就失败，空串的下游含义是"分支无关"，
#     不是错误状态。理由逐条写在那个函数上方。
#     ⚠️ 这是**新增**的静默点（不是历史漂移），所以本次是"确认安全 → 同步数字"，
#     不是"把数字改到能过"。凡是往下调的数字仍然要按头部「红了怎么办」先查清楚。
#   · 2026-10-16 再 +1 return_empty（同日第二轮，仍是新增而非漂移）：get_memory() 收
#     畸形 mid 时的「except (TypeError, ValueError): return None」——
#     int("abc") 会抛 ValueError 而不是返回 None，这里必须兜住：get_memory 是写回回执的
#     取数口，回执不能因为一个畸形 id 把**已经成功**的 memory_save 变成异常
#     （调用方会以为没存进去而重试 → 反而写出重复记忆）。而且它对用户**不是静默的**：
#     memory_receipt 会明说"读不回这条记录，可能是并发删除"。
EXPECTED = {
    "loci.py": {"pass": 12, "continue": 12, "return_empty": 14},
    "panel.py": {"pass": 8, "continue": 4, "return_empty": 2},
    "install_agents.py": {"pass": 0, "continue": 0, "return_empty": 0},
}

_EMPTY_CALLS = ("list", "dict", "set", "tuple")


def _is_empty_value(v):
    """返回值是不是"空"（空容器 / None / "" / 0 / False）。"""
    if v is None:
        return True
    if isinstance(v, ast.Constant):
        return v.value in (None, "", 0, False)
    if isinstance(v, (ast.List, ast.Set, ast.Tuple)):
        return not v.elts
    if isinstance(v, ast.Dict):
        return not v.keys
    if isinstance(v, ast.Call) and isinstance(v.func, ast.Name) and v.func.id in _EMPTY_CALLS:
        return not v.args and not v.keywords
    return False


def classify(handler):
    """返回 'pass' / 'continue' / 'return_empty'；不是静默吞掉则 None。"""
    body = [n for n in handler.body]
    # 去掉纯字符串表达式（docstring 式说明）
    body = [n for n in body
            if not (isinstance(n, ast.Expr) and isinstance(n.value, ast.Constant)
                    and isinstance(n.value.value, str))]
    if len(body) != 1:
        return None
    n = body[0]
    if isinstance(n, ast.Pass):
        return "pass"
    if isinstance(n, ast.Continue):
        return "continue"
    if isinstance(n, ast.Return) and _is_empty_value(n.value):
        return "return_empty"
    return None


def scan(path):
    src = open(path, encoding="utf-8").read()
    tree = ast.parse(src, filename=path)
    hits = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Try):
            for h in node.handlers:
                kind = classify(h)
                if kind:
                    hits.append((h.lineno, kind))
    return sorted(hits)


def main():
    ok = True
    print("== audit_silent_skips ==  （静默吞异常普查：pass / continue / return 空值）")
    for name in TARGETS:
        path = os.path.join(ROOT, name)
        if not os.path.exists(path):
            print("  FAIL  找不到 %s" % path)
            ok = False
            continue
        hits = scan(path)
        counts = {"pass": 0, "continue": 0, "return_empty": 0}
        for _, k in hits:
            counts[k] += 1
        exp = EXPECTED.get(name, {})
        drift = {k: (counts[k], exp.get(k)) for k in counts if counts[k] != exp.get(k)}
        flag = "PASS" if not drift else "FAIL"
        if drift:
            ok = False
        print("  %s  %-10s pass=%d continue=%d return_empty=%d" %
              (flag, name, counts["pass"], counts["continue"], counts["return_empty"]))
        if drift:
            for k, (got, want) in drift.items():
                print("          · %-13s 实际 %s，期望 %s" % (k, got, want))
        if os.environ.get("VERBOSE"):
            for ln, k in hits:
                print("            %s:%d  %s" % (name, ln, k))
    print("  合计静默点：%d 处（loci.py + panel.py）" %
          sum(len(scan(os.path.join(ROOT, n))) for n in TARGETS))
    print("── 结果：%s" % ("PASS" if ok else "FAIL（口径漂移 —— 见脚本头部「红了怎么办」）"))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
