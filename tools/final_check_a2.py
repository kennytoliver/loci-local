# -*- coding: utf-8 -*-
"""核对 A 收尾：精确判定测试残留 + messages 增长的来源"""
import sys

sys.path.insert(0, r"D:\Loci")
import loci  # noqa: E402

conn = loci.db()

print("=== 1. agent='panel' 的全部记忆（这 5 条到底是什么）===")
for r in conn.execute("SELECT id, content, created_at, session_id FROM memories "
                      "WHERE agent='panel' ORDER BY id"):
    print(f"  #{r['id']} @{r['created_at']} session_id={r['session_id']}")
    print(f"      {r['content'][:88].replace(chr(10), ' / ')}")

print()
print("=== 2. 精确判定：本轮/历轮探测的哨兵是否残留 ===")
MARKERS = [
    "CSRF 测试（不应写入）",                       # 旧探针固定文案（开发侧已清）
]
PREFIXES = ["CSRF 探针哨兵_应被清理_", "__ID语义探测__", "__面板确认__",
            "__抖动探测__", "__最终确认__", "面板空类型校验测试", "面板非法类型校验测试",
            "面板越界重要度测试", "探测哨兵", "并发写入测试", "类型枚举越界测试"]
bad = []
for m in MARKERS:
    rows = conn.execute("SELECT id, deleted FROM memories WHERE content=?", (m,)).fetchall()
    if rows:
        bad += [(r["id"], m, r["deleted"]) for r in rows]
for p in PREFIXES:
    rows = conn.execute("SELECT id, deleted, content FROM memories WHERE content LIKE ?",
                        (p + "%",)).fetchall()
    if rows:
        bad += [(r["id"], r["content"][:40], r["deleted"]) for r in rows]
print(f"  残留条数 = {len(bad)}")
for b in bad:
    print("   ", b)
print("  " + ("✅ 零残留" if not bad else "❌ 有残留"))

print()
print("=== 3. messages 增长的来源（对比 sessions.msg_count 与行数）===")
tot = conn.execute("SELECT COALESCE(SUM(msg_count),0) n FROM sessions").fetchone()["n"]
real = conn.execute("SELECT COUNT(*) c FROM messages").fetchone()["c"]
print(f"  sessions.msg_count 合计 = {tot} | messages 实际行数 = {real} | {'自洽' if tot == real else '❌ 不一致'}")
print("  最近创建/更新的会话:")
for r in conn.execute("SELECT id, title, msg_count, created_at, source_path FROM sessions "
                      "ORDER BY id DESC LIMIT 3"):
    print(f"    #{r['id']} {r['msg_count']}轮 @{r['created_at']} src={(r['source_path'] or '')[-46:]}")
print()
print("  scan_files 台账最近 3 次扫描（增量扫描会补录新增消息）:")
for r in conn.execute("SELECT path, sessions, scanned_at FROM scan_files "
                      "ORDER BY scanned_at DESC LIMIT 3"):
    print(f"    @{r['scanned_at']} sessions={r['sessions']} {(r['path'] or '')[-50:]}")

print()
print("=== 4. 有效记忆构成（104 = ?）===")
print("  按 agent:", dict(conn.execute(
    "SELECT agent, COUNT(*) c FROM memories WHERE deleted=0 AND superseded_by=0 GROUP BY agent")))
conn.close()
