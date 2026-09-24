# -*- coding: utf-8 -*-
"""闸门：技能库的探测与传递（会写盘，但只写白名单目录，自带清理）。

要防的回归：① 把 skill 来源/目标写成写死清单（跟会话来源同一个病）；
            ② 传 skill 时越界（源不在 skill 目录里 / 目标不在白名单里）；
            ③ 传递变成"搬运"（把源删了）—— 传递必须只是复制；
            ④ 撞名静默覆盖，把人家原有 skill 干掉。

测试策略：源用本机真实的 skill（只读），目标用**工作区项目级目录** + 一个
绝对不会跟真东西撞的名字（`__gate_test_skill__`），测完在 finally 里删干净。
本机没有工作区时自动跳过写盘类断言。

跑法：
  python tools/verify_skills.py
"""
import filecmp
import hashlib
import io
import os
import shutil
import sys

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
import loci as h  # noqa: E402

FAIL, PASS = [], []
GATE_NAME = "__gate_test_skill__"


def ok(cond, label, extra=""):
    (PASS if cond else FAIL).append(label)
    print(("  ✓ " if cond else "  ✗ ") + label + (("  " + extra) if extra else ""))


def md5(p):
    with open(p, "rb") as f:
        return hashlib.md5(f.read()).hexdigest()


def tree_files(root):
    out = []
    for dp, _, fs in os.walk(root):
        for f in fs:
            fp = os.path.join(dp, f)
            out.append((os.path.relpath(fp, root), fp))
    return sorted(out)


def main():
    cleanup = []

    print("① 来源探测：每个位置都要跟磁盘对上")
    srcs = h.skill_sources()
    ok(len(srcs) >= 4, "探测到 %d 个 skill 位置" % len(srcs))
    consistent = True
    for s in srcs:
        if s["exists"] != os.path.isdir(s["root"]):
            consistent = False
            print("      ✗ %s %s exists=%s 但磁盘 %s" % (s["agent"], s["scope"], s["exists"], s["root"]))
        # parent_exists 的锚点：用户级看 Agent 的数据目录，项目级看工作区本身
        anchor = s.get("anchor") or os.path.dirname(s["root"])
        if s["parent_exists"] != os.path.isdir(anchor):
            consistent = False
            print("      ✗ %s %s parent_exists 与锚点 %s 不符" % (s["agent"], s["scope"], anchor))
    ok(consistent, "exists / parent_exists 都与磁盘事实一致")

    print("\n①b 文案要说人话：不能把装了的产品说成没装")
    # 曾经的真实 bug：项目级条目的"父目录"是工作区、不是产品安装目录，
    # 一律判"本机没装这个 Agent"，于是 WorkBuddy 的工作区条目也报"没装 WorkBuddy"。
    bad_why = []
    for s in srcs:
        if s["exists"]:
            continue
        if "没装" in (s["why"] or "") or "没有装" in (s["why"] or ""):
            # 说"没装"时，该 Agent 的数据目录必须真的不存在
            probe = {"WorkBuddy": os.path.join(os.path.expanduser("~"), ".workbuddy"),
                     "Claude Code": os.path.join(os.path.expanduser("~"), ".claude"),
                     "Codex": os.path.join(os.path.expanduser("~"), ".codex")}.get(s["agent"])
            if probe and os.path.isdir(probe):
                bad_why.append((s["agent"], s["scope"], s["why"]))
        if not s["why"]:
            bad_why.append((s["agent"], s["scope"], "缺席但没说原因"))
    ok(not bad_why, "缺席的条目都给了正确原因", str(bad_why[:3]))
    for s in srcs:
        if not s["exists"]:
            print("      · %-12s %-6s %s" % (s["agent"], s["scope"], s["why"]))

    print("\n② 清单：每个 skill 都要有 SKILL.md、有名字、路径在来源里")
    skills = h.list_local_skills()
    ok(len(skills) >= 1, "探到 %d 个 skill" % len(skills))
    roots = [os.path.normcase(os.path.abspath(s["root"])) for s in srcs]
    bad = []
    for s in skills:
        if not os.path.isfile(os.path.join(s["path"], "SKILL.md")):
            bad.append((s["name"], "没有 SKILL.md"))
        if not s["name"]:
            bad.append((s["dir"], "名字为空"))
        if not any(os.path.normcase(os.path.abspath(s["path"])).startswith(r) for r in roots):
            bad.append((s["name"], "路径不在任何来源下"))
        for k in ("desc", "agent", "scope", "files", "size", "mtime", "agent_created"):
            if k not in s:
                bad.append((s["name"], "缺字段 " + k))
    ok(not bad, "%d 个 skill 全部合格" % len(skills), str(bad[:3]))
    named = [s for s in skills if s["desc"]]
    ok(len(named) >= 1, "%d/%d 个 skill 解析出了描述" % (len(named), len(skills)))
    print("      示例：" + "、".join(s["name"] for s in skills[:5]))

    print("\n③ 目标清单：ready 的判据是「那个 Agent 装过」")
    tgts = h.skill_copy_targets()
    ok(len(tgts) >= 1, "%d 个可传目标" % len(tgts))
    # 项目级目标的"装过没"看工作区在不在；用户级目标看 Agent 的配置目录在不在。
    tbad = []
    for t in tgts:
        anchor = t.get("workspace") or os.path.dirname(t["root"])
        if t["ready"] != os.path.isdir(anchor):
            tbad.append(t["root"])
    ok(not tbad, "ready 与容器目录存在性一致", str(tbad[:2]))

    print("\n④ 安全边界：这些必须被拒绝")
    real = next((s for s in skills if s["files"] <= 10), None)
    if not real:
        print("  - 跳过（没有小体积 skill 可做样本）")
        real = skills[0] if skills else None
    ws = h.workspaces()
    if not real or not ws:
        print("  - 跳过写盘类断言（缺样本或没有工作区）")
        print("\n通过 %d 项 · 失败 %d 项" % (len(PASS), len(FAIL)))
        return 1 if FAIL else 0
    ws_skills = os.path.join(ws[0], ".workbuddy", "skills")

    for label, kw in (
            ("源不在 skill 目录里 → 拒绝", {"src_path": r"C:\Windows", "target_root": ws_skills}),
            ("目标不在白名单里 → 拒绝", {"src_path": real["path"], "target_root": r"C:\Windows\Temp\xx"}),
            ("源不存在 → 拒绝", {"src_path": os.path.join(os.path.dirname(real["path"]), "不存在的skill"),
                             "target_root": ws_skills}),
            ("源==目标 → 拒绝", {"src_path": real["path"], "target_root": os.path.dirname(real["path"])}),
            ("名字含穿越字符 → 拒绝", {"src_path": real["path"], "target_root": ws_skills,
                                "as_name": ".."})):
        r = h.plan_skill_copy(**kw)
        ok(bool(r.get("error")), label, r.get("error", "居然通过了！"))

    print("\n⑤ 预览不写盘")
    plan = h.plan_skill_copy(real["path"], ws_skills, as_name=GATE_NAME)
    ok(plan.get("ok"), "预览成功：%d 个文件 / %d 字节" % (plan.get("files", 0), plan.get("size", 0)))
    ok(plan["dst"] == os.path.join(ws_skills, GATE_NAME), "目标路径正确")
    ok(not plan["dst_exists"], "预览阶段目标不存在（没写盘）")
    ok(not os.path.exists(plan["dst"]), "确认磁盘上确实没有")

    print("\n⑥ 真复制：文件数与内容都要一致")
    try:
        r = h.copy_skill_to(real["path"], ws_skills, as_name=GATE_NAME)
        cleanup.append(r.get("dst"))
        ok(r.get("ok"), "复制成功 → %s" % r.get("dst"))
        src_files = tree_files(real["path"])
        dst_files = tree_files(r["dst"])
        ok(len(dst_files) == len(src_files),
           "文件数一致（%d）" % len(dst_files), "源 %d / 目标 %d" % (len(src_files), len(dst_files)))
        same_name = [a[0] for a in src_files] == [b[0] for b in dst_files]
        ok(same_name, "相对路径集合一致")
        diff = [a[0] for a, b in zip(src_files, dst_files) if md5(a[1]) != md5(b[1])]
        ok(not diff, "内容逐字节一致（比对 md5）", str(diff[:3]))
        ok(os.path.isdir(real["path"]), "源目录仍在（传递是复制，不是搬运）")

        print("\n⑦ 撞名保护")
        again = h.copy_skill_to(real["path"], ws_skills, as_name=GATE_NAME)
        ok(bool(again.get("error")), "默认不覆盖，报错拦住", again.get("error", "")[:60])
        ok(again.get("error") and "已存在" in again["error"], "错误信息说清了原因")

        print("\n⑧ 明确覆盖：旧内容要留备份")
        marker = os.path.join(r["dst"], "GATE_MARKER.txt")
        io.open(marker, "w", encoding="utf-8").write("这个文件标记了「旧版本」")
        ow = h.copy_skill_to(real["path"], ws_skills, as_name=GATE_NAME, overwrite=True)
        cleanup.append(ow.get("backup"))
        ok(ow.get("ok") and ow.get("overwrote"), "覆盖成功")
        ok(ow.get("backup") and os.path.isdir(ow["backup"]), "旧版本改名留档：%s" % os.path.basename(ow.get("backup", "")))
        ok(os.path.isfile(os.path.join(ow["backup"], "GATE_MARKER.txt")), "备份里能找到旧文件")
        ok(not os.path.exists(marker), "新版本里没有旧文件（确实换新了）")

        print("\n⑨ 面板重新探测能看见传过去的 skill")
        found = [s for s in h.list_local_skills()
                 if os.path.normcase(s["path"]) == os.path.normcase(ow["dst"])]
        ok(bool(found), "新位置被探测到（scope=%s）" % (found[0]["scope"] if found else "-"))
    finally:
        for d in cleanup:
            if not d or GATE_NAME not in d:
                continue
            if os.path.isdir(d):
                try:
                    shutil.rmtree(d)
                    print("      （已清理 %s）" % os.path.basename(d))
                except Exception as e:
                    print("      （清理失败 %s：%s）" % (d, e))
        if os.path.isdir(ws_skills) and not os.listdir(ws_skills):
            try:
                os.rmdir(ws_skills)
            except Exception:
                pass

    print("\n⑩ 读取边界")
    out = h.local_skill_detail(r"C:\Windows")
    ok(bool(out.get("error")), "拒绝读取 skill 目录外的路径", out.get("error", ""))
    d = h.local_skill_detail(real["path"])
    ok(d.get("ok") and len(d.get("text", "")) > 0, "正常 skill 能读到正文（%d 字符）" % d.get("size", 0))

    print("\n" + "=" * 52)
    print("通过 %d 项 · 失败 %d 项" % (len(PASS), len(FAIL)))
    if FAIL:
        print("失败项：")
        for f in FAIL:
            print("  - " + f)
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
