# -*- coding: utf-8 -*-
"""闸门：候选记忆抽取的提速**没有改变结果**。

背景（2026-09-21）：接上 WorkBuddy 会话来源后，本机主力会话动辄 700+ 条消息，
`extract_candidates()` 直接跑到 2 分钟以上，把面板的静默扫描卡死。

根因跟之前质检页那次是同一个形状 —— O(n²) 里藏着重复计算：
    if any(similarity(s, e["content"]) > 0.7 for e in existing):
`similarity()` 内部是 `set(tokenize(a)) & set(tokenize(b))`，而 `tokenize()` 是
**纯 Python 逐字符循环**。这句每比一条 existing 就把片段 `s` 重新分词一遍，
记忆侧也是每轮重算 —— 片段被分词几千遍、记忆被分词上万遍。

改法两步，都不改语义：
  ① 两边各分词一次，预计算 token 集合
  ② Jaccard 的硬上界剪枝：J(A,B) ≤ min(|A|,|B|)/max(|A|,|B|)，
     长度比 ≤ 0.7 的一对，交集算了也过不了阈值

这个脚本拿真实库的数据，把两种判定逐条对拍 —— 只要有一条不一致就红。

跑法：
  python tools/verify_extract_perf.py [库路径]
"""
import io
import os
import random
import re
import sys
import time

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# 必须在 import loci **之前**设置：DB 路径是模块加载时求值的，
# 放在 import 之后设等于没设（第一版就是这么错的 —— 闸门悄悄读了真实库还以为在测目标库）。
if len(sys.argv) > 1:
    os.environ["LOCI_DB"] = sys.argv[1]

import loci as h  # noqa: E402

FAIL = []
PASS = []


def ok(cond, label, extra=""):
    (PASS if cond else FAIL).append(label)
    print(("  ✓ " if cond else "  ✗ ") + label + (("  " + extra) if extra else ""))


# ---------- 旧判定：原样复刻优化前的写法 ----------
def old_is_dup(frag, existing):
    return any(h.similarity(frag, e["content"]) > 0.7 for e in existing)


# ---------- 新判定：与 extract_candidates() 现在的写法一致 ----------
def new_is_dup(frag, ex_toks, ex_len):
    st = set(h.tokenize(frag))
    ls = len(st)
    for et, le in zip(ex_toks, ex_len):
        if not ls or not le:
            continue
        if (ls if ls < le else le) / (ls if ls > le else le) <= 0.7:
            continue
        if len(st & et) / len(st | et) > 0.7:
            return True
    return False


def main():
    print("库：%s" % h.DB_PATH)
    conn = h.db()
    rows = [dict(r) for r in conn.execute(
        "SELECT content FROM messages ORDER BY id DESC LIMIT 1200")]
    conn.close()
    if not rows:
        print("库里没有消息，先跑一次扫描再来")
        return 1
    existing = [dict(r) for r in h.list_memories(limit=10000)]
    print("语料：%d 条消息 / %d 条已有记忆\n" % (len(rows), len(existing)))

    # 切片段（跟 extract_candidates 同一套切法）
    frags = []
    for m in rows:
        for raw in re.split(r"[\n。！？!?；;]", m["content"] or ""):
            s = raw.strip(" \u3000-*#>\t")
            if 12 <= len(s) <= 120:
                frags.append(s)
    print("可判定片段：%d 条" % len(frags))
    random.seed(20260921)
    sample = random.sample(frags, min(80, len(frags)))

    print("\n① 判定等价性（旧写法 vs 新写法，逐条对拍）")
    ex_toks = [set(h.tokenize(e["content"] or "")) for e in existing]
    ex_len = [len(t) for t in ex_toks]
    mismatch = []
    for s in sample:
        a = old_is_dup(s, existing)
        b = new_is_dup(s, ex_toks, ex_len)
        if a != b:
            mismatch.append((s, a, b))
    ok(not mismatch, "新旧判定 %d 条全部一致" % len(sample),
       ("不一致 %d 条，例：%r" % (len(mismatch), mismatch[0])) if mismatch else "")
    dup_rate = sum(1 for s in sample if new_is_dup(s, ex_toks, ex_len))
    print("     （样本里判为重 %d 条，占 %.1f%%）" % (dup_rate, 100.0 * dup_rate / len(sample)))

    print("\n② 上界剪枝确实在起作用（不能一条都没剪掉）")
    # 直接统计：有多少 (片段, 记忆) 对满足长度比 ≤ 0.7
    pairs_total = len(sample) * len(existing)
    pairs_kept = 0
    for s in sample:
        ls = len(set(h.tokenize(s)))
        for le in ex_len:
            if ls and le and (ls if ls < le else le) / (ls if ls > le else le) > 0.7:
                pairs_kept += 1
    ok(pairs_kept < pairs_total,
       "剪枝命中：%d/%d 对被跳过（留下 %.1f%%）"
       % (pairs_total - pairs_kept, pairs_total, 100.0 * pairs_kept / max(1, pairs_total)))

    print("\n③ 提速幅度（同一批片段，两种判定各跑一遍）")
    t = time.time()
    for s in sample[:25]:
        old_is_dup(s, existing)
    t_old = time.time() - t
    t = time.time()
    for s in sample[:25]:
        new_is_dup(s, ex_toks, ex_len)
    t_new = time.time() - t
    speed = (t_old / t_new) if t_new > 0 else float("inf")
    ok(t_new < t_old, "新写法更快：%.2fs → %.2fs（%.1fx）" % (t_old, t_new, speed))

    print("\n④ 真实接口跑得动（对最大会话抽一次候选）")
    conn = h.db()
    row = conn.execute("SELECT id, msg_count FROM sessions ORDER BY msg_count DESC LIMIT 1").fetchone()
    conn.close()
    if row:
        t = time.time()
        cands = h.extract_candidates(session_id=row["id"], limit=12)
        dt = time.time() - t
        ok(dt < 8.0, "最大会话 #%d（%d 条消息）抽取耗时 %.2fs < 8s，得 %d 条候选"
           % (row["id"], row["msg_count"], dt, len(cands)))
    else:
        print("  - 跳过（库里没有会话）")

    print("\n" + "=" * 52)
    print("通过 %d 项 · 失败 %d 项" % (len(PASS), len(FAIL)))
    if FAIL:
        print("失败项：" + "；".join(FAIL))
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
