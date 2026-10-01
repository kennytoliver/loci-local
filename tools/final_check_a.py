# -*- coding: utf-8 -*-
"""核对 A：真库状态（#160/#161/#162/#163 是否已清、有效记忆是否 104、#144 是否完好）"""
import sys

sys.path.insert(0, r"D:\Loci")
import loci  # noqa: E402

conn = loci.db()
print("=== 核对 A：真库状态 ===")
total = conn.execute("SELECT COUNT(*) c FROM memories").fetchone()["c"]
deleted = conn.execute("SELECT COUNT(*) c FROM memories WHERE deleted=1").fetchone()["c"]
sup = conn.execute("SELECT COUNT(*) c FROM memories WHERE superseded_by!=0").fetchone()["c"]
valid = conn.execute("SELECT COUNT(*) c FROM memories WHERE deleted=0 AND superseded_by=0").fetchone()["c"]
print(f"  memories 总行数        = {total}")
print(f"  deleted=1              = {deleted}")
print(f"  superseded_by!=0       = {sup}")
print(f"  ★ 有效记忆             = {valid}   （期望 104）")
print()

print("  --- #160/#161/#162/#163 是否还在 ---")
rows = conn.execute("SELECT id, deleted, agent, content FROM memories WHERE id IN (160,161,162,163)").fetchall()
if not rows:
    print("    ✅ 4 条全部不存在（已清）")
else:
    for r in rows:
        print(f"    ❌ #{r['id']} 仍在: deleted={r['deleted']} agent={r['agent']} {r['content'][:40]}")
print()

print("  --- agent='panel' 的测试残留 ---")
prows = conn.execute("SELECT id, content, created_at FROM memories WHERE agent='panel' "
                     "AND (content LIKE '%CSRF%' OR content LIKE '%哨兵%' OR content LIKE '%确认%')").fetchall()
print(f"    条数 = {len(prows)}")
for r in prows:
    print(f"    #{r['id']} {r['content'][:50]} @{r['created_at']}")
print()

print("  --- #144（我上次误删、已恢复的那条真实日志）---")
r = conn.execute("SELECT id, deleted, superseded_by, agent, project, length(content) n, "
                 "substr(content,1,50) c, created_at FROM memories WHERE id=144").fetchone()
if r is None:
    print("    ❌ #144 不见了")
else:
    print(f"    #144 deleted={r['deleted']} superseded_by={r['superseded_by']} agent={r['agent']}")
    print(f"    长度={r['n']} 字符  时间={r['created_at']}")
    print(f"    开头: {r['c']!r}")
    print(f"    {'✅ 完好' if r['deleted'] == 0 else '❌ 被标记删除'}")
print()

print("  --- 按精确内容再查一次那 3 条垃圾 ---")
for c in ("CSRF 测试（不应写入）",):
    n = conn.execute("SELECT COUNT(*) c FROM memories WHERE content=?", (c,)).fetchone()["c"]
    print(f"    content 精确等于 {c!r} 的行数 = {n}")
print()

print("  --- 完整性 ---")
print("    PRAGMA quick_check =", conn.execute("PRAGMA quick_check").fetchone()[0])
print("    sessions =", conn.execute("SELECT COUNT(*) c FROM sessions").fetchone()["c"],
      "| messages =", conn.execute("SELECT COUNT(*) c FROM messages").fetchone()["c"])
conn.close()
