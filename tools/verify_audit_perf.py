"""校验 audit_memories 的预计算优化与原始逐对实现完全等价，并量化加速比。

背景：audit_memories() 里是 O(n^2) 配对比较。原始实现对每个配对都重新
tokenize 两个字符串（similarity + containment 各一次），n=40 时要跑 3120 次
tokenize。优化后每条记忆只 tokenize 一次，jac/con 共用同一次集合交集。

优化必须"逐位等价"，否则质检结果会悄悄变化（重复组少识别、矛盾漏报）。
本脚本用原始 similarity()/containment() 重算参考结果，跟优化后的
quality_scan() 结果比对：重复组、疑似同义、可能矛盾三项必须完全一致。

零第三方依赖：python tools/verify_audit_perf.py
"""
import sys, time
sys.path.insert(0, r"C:/Users/user/Hippocampus")
import hippocampus as hippo


def reference_scan(project=None, dup_th=0.66, contain_th=0.82, conflict_lo=0.28,
                   stale_days=90):
    """原始逐对实现（用 similarity/containment 函数，不做预计算）"""
    rows = [dict(r) for r in hippo.list_memories(project=project, limit=10000)]
    n = len(rows)
    parent = list(range(n))

    def find(x):
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(x, y):
        rx, ry = find(x), find(y)
        if rx != ry:
            parent[ry] = rx

    conflicts, suspects = [], []
    for i in range(n):
        for j in range(i + 1, n):
            jac = hippo.similarity(rows[i]["content"], rows[j]["content"])
            con = hippo.containment(rows[i]["content"], rows[j]["content"])
            if jac >= 0.50 or con >= 0.70:
                union(i, j)
            else:
                same_proj = (rows[i]["project"] or "") == (rows[j]["project"] or "")
                a_chg = any(w in rows[i]["content"] for w in hippo.CHANGE_WORDS)
                b_chg = any(w in rows[j]["content"] for w in hippo.CHANGE_WORDS)
                if same_proj and a_chg != b_chg and jac >= 0.25:
                    conflicts.append({"older": rows[i], "newer": rows[j],
                                      "similarity": round(max(jac, con * 0.9), 3)})
                elif (same_proj and rows[i]["mtype"] == rows[j]["mtype"] and jac >= 0.25):
                    suspects.append({"a": rows[i], "b": rows[j], "similarity": round(jac, 3)})

    groups = {}
    for i in range(n):
        groups.setdefault(find(i), []).append(rows[i])
    dup = [g for g in groups.values() if len(g) > 1]
    return {"n": n, "dups": sorted(sorted(x["id"] for x in g) for g in dup),
            "suspects": sorted(tuple(sorted((c["a"]["id"], c["b"]["id"]))) for c in suspects),
            "conflicts": sorted(tuple(sorted((c["older"]["id"], c["newer"]["id"]))) for c in conflicts)}


def fast_scan(project=None):
    q = hippo.quality_scan(project)
    return {"n": q["scanned"],
            "dups": sorted(sorted(x["id"] for x in g) for g in q["duplicates"]),
            "suspects": sorted(tuple(sorted((c["a"]["id"], c["b"]["id"]))) for c in q["suspects"]),
            "conflicts": sorted(tuple(sorted((c["older"]["id"], c["newer"]["id"]))) for c in q["conflicts"])}


def main():
    print("=" * 64)
    print("等价性校验：预计算实现 vs 原始逐对实现")
    print("=" * 64)

    t0 = time.perf_counter()
    ref = reference_scan()
    t_ref = time.perf_counter() - t0

    t0 = time.perf_counter()
    fast = fast_scan()
    t_fast = time.perf_counter() - t0

    ok = True
    for k in ["n", "dups", "suspects", "conflicts"]:
        same = ref[k] == fast[k]
        ok = ok and same
        print(f"  {'✓' if same else '✗'} {k:10s} 参考={ref[k]!r}")
        if not same:
            print(f"               优化={fast[k]!r}")

    print()
    print(f"  记忆条数      : {ref['n']}")
    print(f"  原始逐对耗时  : {t_ref:.3f}s")
    print(f"  预计算耗时    : {t_fast:.3f}s")
    if t_fast > 0:
        print(f"  加速比        : {t_ref / t_fast:.1f}x")
    n = ref["n"]
    print(f"  tokenize 次数 : {4 * n * (n - 1) // 2} → {n}")

    print()
    if ok:
        print("结论: 完全等价 ✅ 可以放心使用")
        return 0
    print("结论: 结果不一致 ❌ 优化改动了语义，必须回退")
    return 1


if __name__ == "__main__":
    sys.exit(main())
