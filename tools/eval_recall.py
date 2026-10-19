# -*- coding: utf-8 -*-
"""召回质量评测：改了检索之后，记忆是更好找了还是更难找了？

这是「可复跑的回归套件」，不是质量断言 —— 语料只有 22 条、用例 26 个，
样本小，所以它的用途是**防止改坏**（回归护栏），不是证明"找得准"。

⚠️ 两个诚实声明（写在这里，也写进了 recall_eval.json 的 _meta）：
   · 语料是**合成的**，不含任何真实个人记忆；
   · 用例**刻意包含当前答不对的类型** —— 只报好数字的评测没有意义。

用法：
    python tools/eval_recall.py            # 报告（人看）
    python tools/eval_recall.py --gate     # 当闸门跑：低于基线就退出码 1
    python tools/eval_recall.py --json     # 机器可读

⚠️ 安全：**全程使用临时数据库**，绝不打开用户真实的 loci.db。
   导入 loci 之前先设好 LOCI_DB —— 因为 loci 在导入时就确定数据库路径。

首测基线（2026-10-11，语料 22 条 / 用例 26 个）：
    Recall@1 80.8% ｜ Recall@3 88.5% ｜ Recall@5 88.5% ｜ MRR 0.846

已知弱项（**刻意留在用例里，不隐藏**）：
    · cn-paraphrase 「换个说法问同一件事」—— 67% @1。bigram 对字面敏感、对语义不敏感，
      所以"为什么有时候推不上去"找不到"push 卡住先试直连"。
    · cross-lang   中文问、英文记忆 —— 0%。分词各自成体系，中间没有语义桥。
      这是「零 LLM 依赖」的直接代价：不做嵌入就没有跨语言对齐。
    这两类**不是 bug，是当前架构的边界**。要提升它们，代价是引入向量/嵌入，
    也就是放弃"零第三方依赖"。所以先如实记录，不擅自改。
"""


import os
import sys
import json
import math
import shutil
import tempfile
import pathlib

ROOT = pathlib.Path(__file__).resolve().parent.parent
FIXTURE = pathlib.Path(__file__).resolve().parent / "recall_eval.json"

# ── 安全：先在临时目录建库，再导入 loci ────────────────────────────────
_TMP = tempfile.mkdtemp(prefix="loci-recall-eval-")
os.environ["LOCI_DB"] = os.path.join(_TMP, "eval.db")
sys.path.insert(0, str(ROOT))
import loci  # noqa: E402  （必须在设好 LOCI_DB 之后导入）

KS = (1, 3, 5)

# 基线：低于它就说明"改坏了"。首次实测填入（2026-10-11），之后只许涨不许跌。
# 这些数字**不是目标**，是护栏 —— 它们记录了当前这套「bigram 分词 + TF-IDF」的真实水平，
# 其中 cross-lang / paraphrase 是已知弱项（见文末"已知弱项"），刻意不从基线里拿掉。
BASELINE = {
    "recall@1": 21 / 26,
    "recall@3": 23 / 26,
    "recall@5": 23 / 26,
    "mrr": 0.846153846154,
}
# 分类下限：弱项也定下限 —— 允许它现在低，不许它继续掉
BASELINE_BY_KIND = {
    "cn-exact": 1.0,
    "cn-paraphrase": 5 / 6,
    "cross-lang-cn2en": 0.0,
    "cross-lang-mix": 0.0,
    "en-exact": 1.0,
    "multi-hit": 1.0,
}


def load():
    with open(FIXTURE, encoding="utf-8") as f:
        return json.load(f)


def build(data):
    """把语料灌进临时库，返回 语料 id → 数据库行 id 的映射。

    ⚠️ skip_check=False：这里测的是**检索**，不是写入过滤。
       排除清单（should_drop）会拦掉看起来像工具输出残骸的内容，
       那是另一条链路的事，不该让评测因为写入被拦而误报。
    """
    idmap = {}
    for item in data["corpus"]:
        mid = loci.save_memory(
            item["content"],
            mtype=item.get("mtype", "fact"),
            importance=item.get("importance", 2),
            tags=item.get("tags", ""),
            project=item.get("project", ""),
            agent="recall-eval",
            skip_check=False,
            allow_dup=True,
        )
        idmap[item["id"]] = mid
    return idmap


def rank_of(rows, wanted_dbids):
    """期望的任一条在结果里的排名（1 起）；没命中返回 None。"""
    for i, (_, r) in enumerate(rows, 1):
        if r["id"] in wanted_dbids:
            return i
    return None


def run(data):
    idmap = build(data)
    per_case = []
    for c in data["cases"]:
        want = {idmap[x] for x in c["expect"] if x in idmap}
        rows = loci.search_memory(c["query"], limit=max(KS))
        rk = rank_of(rows, want)
        per_case.append({
            "query": c["query"],
            "kind": c["kind"],
            "expect": c["expect"],
            "rank": rk,
            "top1": (rows[0][1]["content"][:40] if rows else ""),
        })
    return per_case


def summarize(per_case):
    n = len(per_case)
    out = {"cases": n, "by_kind": {}, "misses": []}
    for k in KS:
        out["recall@%d" % k] = sum(1 for c in per_case if c["rank"] and c["rank"] <= k) / n
    out["mrr"] = sum((1.0 / c["rank"]) if c["rank"] else 0.0 for c in per_case) / n

    kinds = {}
    for c in per_case:
        kinds.setdefault(c["kind"], []).append(c)
    for kind, cs in sorted(kinds.items()):
        m = len(cs)
        out["by_kind"][kind] = {
            "cases": m,
            "recall@1": sum(1 for c in cs if c["rank"] == 1) / m,
            "recall@5": sum(1 for c in cs if c["rank"] and c["rank"] <= 5) / m,
            "mrr": sum((1.0 / c["rank"]) if c["rank"] else 0.0 for c in cs) / m,
        }
    out["misses"] = [c for c in per_case if not c["rank"]]
    return out


def report(s):
    print("=" * 68)
    print("召回质量评测  语料 %d 条 · 用例 %d 个" % (len(load()["corpus"]), s["cases"]))
    print("=" * 68)
    print()
    print("总体")
    for k in KS:
        print("   Recall@%d    %5.1f%%   (%d/%d)" % (
            k, s["recall@%d" % k] * 100,
            round(s["recall@%d" % k] * s["cases"]), s["cases"]))
    print("   MRR         %.3f" % s["mrr"])
    print()
    print("按用例类型（弱点会在这里露出来）")
    print("   %-20s %5s %9s %9s %7s" % ("类型", "用例", "Recall@1", "Recall@5", "MRR"))
    for kind, v in s["by_kind"].items():
        print("   %-20s %5d %8.0f%% %8.0f%% %7.3f" % (
            kind, v["cases"], v["recall@1"] * 100, v["recall@5"] * 100, v["mrr"]))
    print()
    if s["misses"]:
        print("完全没命中（%d 个）—— 这些是当前真实存在的弱项：" % len(s["misses"]))
        for c in s["misses"]:
            print("   [%s] %s" % (c["kind"], c["query"]))
            print("        期望命中 %s ｜ 实际召回第一条：%s" % (c["expect"], c["top1"] or "（空）"))
    else:
        print("全部用例至少命中了 top5。")
    print()


def gate(s):
    """回归护栏：低于基线就失败。首次运行时基线是 0，等于只记录不拦。"""
    bad = []
    for key in ("recall@1", "recall@3", "recall@5", "mrr"):
        if key not in BASELINE:
            continue
        got, base = s[key], BASELINE[key]
        if got + 1e-9 < base:
            bad.append("%s 从 %.3f 掉到 %.3f" % (key, base, got))
    for kind, v in s["by_kind"].items():
        if kind in BASELINE_BY_KIND:
            b = BASELINE_BY_KIND[kind]
            if v["recall@5"] + 1e-9 < b:
                bad.append("%s 的 recall@5 从 %.0f%% 掉到 %.0f%%" % (
                    kind, b * 100, v["recall@5"] * 100))
    if bad:
        print("❌ 召回质量回退：")
        for x in bad:
            print("   - " + x)
        return 1
    print("✅ 召回质量不低于基线（%s）" % ", ".join(
        "%s=%.3f" % (k, v) for k, v in BASELINE.items() if v))
    return 0


def main():
    data = load()
    per_case = run(data)
    s = summarize(per_case)
    try:
        if "--json" in sys.argv:
            print(json.dumps(s, ensure_ascii=False, indent=2))
            return 0
        report(s)
        if "--gate" in sys.argv:
            return gate(s)
        return 0
    finally:
        shutil.rmtree(_TMP, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
