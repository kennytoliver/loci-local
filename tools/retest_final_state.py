# -*- coding: utf-8 -*-
"""最终核对：真库状态 + CSRF 测试残留的精确清单"""
import sys

sys.path.insert(0, r"D:\Loci")
import loci  # noqa: E402

conn = loci.db()
print("=== 真库当前状态 ===")
print("  memories 总行数         =", conn.execute("SELECT COUNT(*) c FROM memories").fetchone()["c"])
print("    deleted=1（软删除）    =", conn.execute("SELECT COUNT(*) c FROM memories WHERE deleted=1").fetchone()["c"])
print("    superseded_by!=0     =", conn.execute("SELECT COUNT(*) c FROM memories WHERE superseded_by!=0").fetchone()["c"])
print("    有效                  =", conn.execute("SELECT COUNT(*) c FROM memories WHERE deleted=0 AND superseded_by=0").fetchone()["c"])
print("  sessions =", conn.execute("SELECT COUNT(*) c FROM sessions").fetchone()["c"],
      "| messages =", conn.execute("SELECT COUNT(*) c FROM messages").fetchone()["c"])

print()
print("=== 含 'CSRF' 的记忆（全部，含软删除）===")
for r in conn.execute("SELECT id,deleted,agent,content,created_at FROM memories "
                      "WHERE content LIKE '%CSRF%' ORDER BY id"):
    print("  #%-4s deleted=%s agent=%-6s %-46s %s"
          % (r["id"], r["deleted"], r["agent"], r["content"][:46], r["created_at"]))

print()
print("=== 含 '应被清理'（我这次探针的哨兵）===")
n = conn.execute("SELECT COUNT(*) c FROM memories WHERE content LIKE '%应被清理%'").fetchone()["c"]
print("  条数 =", n)

print()
print("=== 我名下的探测数据（agent in panel 且疑似测试）===")
rows = conn.execute("SELECT id,content,created_at FROM memories WHERE agent='panel' "
                    "AND (content LIKE '%CSRF%' OR content LIKE '%哨兵%') ORDER BY id").fetchall()
print("  条数 =", len(rows))
for r in rows:
    print("   #%s %s @%s" % (r["id"], r["content"][:40], r["created_at"]))
conn.close()
