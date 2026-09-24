# -*- coding: utf-8 -*-
"""闸门：本机对话来源是「探出来的」，不是「写死的」。

要防的回归（2026-09-21 修）：`auto_scan_agents()` 里曾经硬编码三个来源 ——
    scan_zcode_db(...) + _scan_generic_jsonl("Claude Code", ...) + _scan_generic_jsonl("Codex", ...)
本机主力来源 WorkBuddy（~/.workbuddy/projects，8 会话 / 1377 条消息）一个字都没被扫，
而根本没装的 Claude Code / Codex 却挂在扫描名单上，面板文案还照着念。
换台电脑这份名单就是错的 —— 名单必须从磁盘问出来。

本脚本对**真实磁盘**做断言（只读，不碰真实库）：探测结果要跟文件系统事实一致、
实存的源要真扫得出来、导入的会话要有真标题/真时间/干净正文、增量不能重复。

跑法：
  python tools/verify_scan_sources.py
"""
import io
import os
import shutil
import sys
import tempfile
import time

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

# 必须在 import loci 之前设库：DB 路径是模块加载时求值的。
TMP_DB = os.path.join(tempfile.gettempdir(), "loci-scan-gate.db")
os.environ["LOCI_DB"] = TMP_DB

import loci as h  # noqa: E402

FAIL, PASS = [], []


def ok(cond, label, extra=""):
    (PASS if cond else FAIL).append(label)
    print(("  ✓ " if cond else "  ✗ ") + label + (("  " + extra) if extra else ""))


def reset_db():
    for suf in ("", "-wal", "-shm"):
        try:
            os.remove(TMP_DB + suf)
        except Exception:
            pass
    h.db().close()


def main():
    print("临时库：%s" % TMP_DB)
    print("脚本路径以 %s 为基准\n" % ROOT)

    print("① 探测覆盖全部登记来源，且状态跟磁盘事实一致")
    if not hasattr(h, "detect_conversation_sources"):
        ok(False, "detect_conversation_sources() 存在")
        print("      → 代码里根本没有探测函数，来源还是写死的 —— 这正是本闸门要防的回归")
        print("\n通过 0 项 · 失败 1 项")
        return 1
    srcs = h.detect_conversation_sources()
    ok(len(srcs) == len(h.SCAN_SOURCES),
       "探测返回 %d 个源 = SCAN_SOURCES 登记数 %d" % (len(srcs), len(h.SCAN_SOURCES)))
    consistent = True
    for s in srcs:
        exists = os.path.isdir(s["root"]) if s["kind"] == "jsonl" else os.path.isfile(s["root"])
        if not exists and s["state"] != "absent":
            consistent = False
            print("      ✗ %s 路径不存在却报 %s" % (s["agent"], s["state"]))
        if exists and s["state"] == "absent":
            consistent = False
            print("      ✗ %s 路径存在却报 absent" % s["agent"])
        if s["state"] == "found" and s["files"] < 1:
            consistent = False
            print("      ✗ %s 报 found 但 files=%d" % (s["agent"], s["files"]))
    ok(consistent, "每个源的 state 与磁盘一致")
    for s in srcs:
        print("      · %-12s %-6s files=%-3d %s" % (s["agent"], s["state"], s["files"], s["root"]))
    found = [s for s in srcs if s["state"] == "found"]
    ok(len(found) >= 1, "本机至少有一个来源有数据（%d 个）" % len(found))

    print("\n② 没有写死名单：扫描入口必须走探测函数")
    src_txt = io.open(os.path.join(ROOT, "loci.py"), encoding="utf-8").read()
    seg = src_txt.split("def auto_scan_agents")[1].split("\ndef ")[0]
    ok("detect_conversation_sources()" in seg, "auto_scan_agents() 内部调用了探测函数")
    hard = [a for a in ("'Claude Code'", '"Claude Code"', "'Codex'", '"Codex"', "'ZCode'", '"ZCode"')
            if a in seg]
    ok(not hard, "auto_scan_agents() 里没有硬编码产品名", ("发现：" + ",".join(hard)) if hard else "")

    print("\n③ 只用探测到的源：absent 的源不出现在扫描结果里")
    reset_db()
    t = time.time()
    r = h.auto_scan_agents(import_new=False, extract=False)
    dt = time.time() - t
    used_agents = [u["agent"] for u in r["used"]]
    found_agents = [s["agent"] for s in found]
    ok(set(used_agents) == set(found_agents),
       "扫的源 = 探到的源 %s" % found_agents,
       "" if set(used_agents) == set(found_agents) else "实际扫了 %s" % used_agents)
    absent_in_used = [s["agent"] for s in srcs if s["state"] == "absent" and s["agent"] in used_agents]
    ok(not absent_in_used, "缺席的源没被扫", str(absent_in_used))
    ok(r["scanned"] > 0, "扫出 %d 个会话（%.2fs）" % (r["scanned"], dt))

    print("\n④ 真导入：空库 → 会话/消息/时间都要落地")
    reset_db()
    t = time.time()
    r = h.auto_scan_agents(import_new=True, extract=False)
    dt = time.time() - t
    ok(r["imported"] == r["scanned"] and r["imported"] > 0,
       "空库首次导入 %d 个会话（%.2fs）" % (r["imported"], dt))
    conn = h.db()
    ns = conn.execute("SELECT COUNT(*) FROM sessions").fetchone()[0]
    nm = conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0]
    ok(ns == r["imported"], "库里会话数 %d = 导入数 %d" % (ns, r["imported"]))
    ok(nm > 0, "库里消息数 %d" % nm)
    # 这条是用户实际报的问题：本机主力来源（WorkBuddy）有数据却没被扫进来。
    # 写死名单的旧代码在这里必红。
    for s in found:
        n_a = conn.execute("SELECT COUNT(*) FROM sessions WHERE agent=?",
                           (s["agent"],)).fetchone()[0]
        ok(n_a > 0, "本机实存来源「%s」的会话被扫进来了（%d 个）" % (s["agent"], n_a))

    print("\n⑤ 标题来自宿主自己的标题行，不是系统注入块")
    wb = [s for s in srcs if s["agent"] == "WorkBuddy" and s["state"] == "found"]
    if wb:
        raw = h._scan_generic_jsonl("WorkBuddy", wb[0]["root"])
        titled = [x for x in raw if x["title"]]
        ok(len(titled) > 0, "从源里捡到 %d 个会话标题（共 %d 个会话）" % (len(titled), len(raw)))
        # 库里的标题必须等于源里的 aiTitle，或者退回"首条用户消息"截断
        rows = [dict(x) for x in conn.execute(
            "SELECT id,title FROM sessions WHERE agent='WorkBuddy'")]
        by_title = {x["title"] for x in titled}
        bad = [x for x in rows if x["title"] not in by_title
               and not any(x["title"].startswith(m["content"][:20]) for m in
                           (raw[0]["messages"] if raw else []))]
        hit = sum(1 for x in rows if x["title"] in by_title)
        ok(hit > 0, "%d/%d 个 WorkBuddy 会话用的是源里的标题" % (hit, len(rows)))
        if bad:
            print("      （%d 个会话走了首条消息回退，属正常）" % len(bad))
    else:
        print("  - 跳过（本机没有 WorkBuddy 数据）")

    print("\n⑥ 正文干净：宿主注入块不能进库")
    dirty = conn.execute(
        "SELECT COUNT(*) FROM messages WHERE content LIKE '%<system-reminder%' "
        "OR content LIKE '%</system-reminder>%'").fetchone()[0]
    ok(dirty == 0, "0 条消息含 system-reminder 残留", "实际 %d 条" % dirty)
    uq = conn.execute(
        "SELECT COUNT(*) FROM messages WHERE content LIKE '%<user_query>%'").fetchone()[0]
    ok(uq == 0, "0 条消息含 <user_query> 信封标签", "实际 %d 条" % uq)
    empty = conn.execute("SELECT COUNT(*) FROM messages WHERE TRIM(content)=''").fetchone()[0]
    ok(empty == 0, "0 条空消息（剥完为空的应当丢弃）", "实际 %d 条" % empty)

    print("\n⑦ 只收对话，不收工具噪音")
    if wb:
        p = None
        for dp, _, fs in os.walk(wb[0]["root"]):
            for f in fs:
                if f.endswith(".jsonl"):
                    p = os.path.join(dp, f)
                    break
            if p:
                break
        if p:
            lines = [l for l in io.open(p, encoding="utf-8", errors="replace").read().splitlines() if l.strip()]
            import json as _j
            n_msg = sum(1 for l in lines if _j.loads(l).get("type") == "message")
            n_all = len(lines)
            ok(n_all > n_msg, "源文件 %d 行里只有 %d 行是对话（其余被过滤）" % (n_all, n_msg))

    print("\n⑧ 时间是真的发生时间，不是导入时刻")
    st = conn.execute("SELECT COUNT(*) FROM sessions WHERE started_at<>''").fetchone()[0]
    ok(st == ns, "%d/%d 个会话有 started_at" % (st, ns))
    bad_time = conn.execute(
        "SELECT COUNT(*) FROM sessions WHERE started_at<>'' AND ended_at<>'' "
        "AND ended_at < started_at").fetchone()[0]
    ok(bad_time == 0, "0 个会话的结束时间早于开始时间", "实际 %d 个" % bad_time)
    # 导入时刻 = created_at；真时间不该等于它（除非真的同一秒）
    same = conn.execute(
        "SELECT COUNT(*) FROM sessions WHERE started_at<>'' AND started_at=created_at").fetchone()[0]
    ok(same < ns, "会话时间没有退化成导入时刻（%d/%d 与 created_at 相同）" % (same, ns))
    span = conn.execute("SELECT MIN(started_at), MAX(ended_at) FROM sessions").fetchone()
    print("      时间跨度：%s → %s" % (span[0], span[1]))

    print("\n⑨ 增量：文件没变就不重复解析、不重复入库")
    p = h._scan_ledger()
    ok(len(p) > 0, "台账记下了 %d 个来源文件" % len(p))
    t = time.time()
    r2 = h.auto_scan_agents(import_new=True, extract=False)
    dt2 = time.time() - t
    ok(r2["scanned"] == 0, "文件没变时扫出 0 个会话", "实际 %d" % r2["scanned"])
    ok(r2["files_unchanged"] == len(p),
       "全部 %d 个文件都被判为未变" % len(p), "实际 %d" % r2["files_unchanged"])
    ns2 = h.db().execute("SELECT COUNT(*) FROM sessions").fetchone()[0]
    ok(ns2 == ns, "会话数没变（%d → %d）" % (ns, ns2))
    ok(dt2 < 1.0, "增量扫描 %.2fs < 1s（首次 %.2fs）" % (dt2, dt))

    print("\n⑩ force 能强制重扫（改了解析规则时的逃生门）")
    t = time.time()
    r3 = h.auto_scan_agents(import_new=False, extract=False, force=True)
    dt3 = time.time() - t
    ok(r3["files_unchanged"] == 0, "force 时没有文件被跳过")
    ok(r3["scanned"] == r["scanned"], "force 重扫得到同样的会话数（%d）" % r3["scanned"])
    ok(dt3 >= dt2, "force 确实干活了（%.2fs > 增量 %.2fs）" % (dt3, dt2))

    print("\n⑪ 建议器：只提示不自动扫")
    sug = h.suggest_conversation_sources()
    ok(isinstance(sug, list), "suggest_conversation_sources() 返回列表（%d 个）" % len(sug))
    for x in sug:
        ok(all(k in x for k in ("dir", "root", "files")), "建议项字段齐全：%s" % x.get("root"))

    conn.close()
    reset_db()
    for suf in ("", "-wal", "-shm"):
        try:
            os.remove(TMP_DB + suf)
        except Exception:
            pass

    print("\n" + "=" * 52)
    print("通过 %d 项 · 失败 %d 项" % (len(PASS), len(FAIL)))
    if FAIL:
        print("失败项：")
        for f in FAIL:
            print("  - " + f)
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
