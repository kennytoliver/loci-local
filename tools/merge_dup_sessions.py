# -*- coding: utf-8 -*-
"""合并同一来源（source_path）的重复会话。

背景（2026-09-22 用户报的 bug）：
  会话列表里同一段 WorkBuddy 对话重复了几十行（msg_count 692→767 递增）。
  根因是采集每次把"整段对话的全量快照"重新 save，而旧去重只按全消息 md5，
  对话一变长指纹就变，于是次次新建。修好 save_session 的同源幂等后，
  历史遗留的重复需要就地清算。

策略：每个重复组保留"消息最全的那条"（msg_count 最大，并列取 id 最大），
  把同组其余会话的**记忆出处迁移**到保留条，再删除其余会话与它们的 messages。
  保留原 id → 记忆里的「出处」链接不断。

用法：
  python tools/merge_dup_sessions.py                 # 只报告（dry-run，不改动）
  python tools/merge_dup_sessions.py --apply          # 真正执行（执行前请自行备份 db）
"""
import os
import sqlite3
import sys

DB = os.environ.get("HIPPOCAMPUS_DB",
                    os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                                 "hippocampus.db"))


def main():
    apply = "--apply" in sys.argv
    c = sqlite3.connect(DB)
    c.row_factory = sqlite3.Row

    s0 = c.execute("SELECT COUNT(*) n FROM sessions").fetchone()["n"]
    m0 = c.execute("SELECT COUNT(*) n FROM messages").fetchone()["n"]
    print(f"db={DB}")
    print(f"处理前：sessions={s0}  messages={m0}  模式={'APPLY(真改)' if apply else 'DRY-RUN(只看)'}")

    groups = c.execute(
        "SELECT source_path, COUNT(*) n FROM sessions "
        "WHERE source_path IS NOT NULL AND source_path<>'' "
        "GROUP BY source_path HAVING n>1 ORDER BY n DESC").fetchall()
    if not groups:
        print("没有重复来源，无需合并。")
        return

    tot_dead = 0
    for g in groups:
        sp = g["source_path"]
        rows = c.execute(
            "SELECT id, msg_count, title, created_at FROM sessions WHERE source_path=? "
            "ORDER BY msg_count DESC, id DESC", (sp,)).fetchall()
        keep = rows[0]
        keep_real = c.execute("SELECT COUNT(*) n FROM messages WHERE session_id=?",
                              (keep["id"],)).fetchone()["n"]
        dead = [r["id"] for r in rows[1:]]
        print(f"\n来源: {sp[:64]}")
        print(f"  该组 {len(rows)} 条，保留 id={keep['id']}（表记 msg_count={keep['msg_count']}，"
              f"实际 messages={keep_real}）")
        print(f"  将删除 {len(dead)} 条: id={dead[:12]}{' …' if len(dead) > 12 else ''}")
        # 记忆出处迁移的量
        qs = ",".join("?" * len(dead))
        moved = c.execute(f"SELECT COUNT(*) n FROM memories WHERE session_id IN ({qs})",
                          dead).fetchone()["n"]
        print(f"  这些会话被 {moved} 条记忆引用 → 迁到 id={keep['id']}")
        if apply:
            c.executemany("UPDATE memories SET session_id=? WHERE session_id=?",
                          [(keep["id"], d) for d in dead])
            c.executemany("DELETE FROM messages WHERE session_id=?", [(d,) for d in dead])
            c.executemany("DELETE FROM sessions WHERE id=?", [(d,) for d in dead])
            c.execute("UPDATE sessions SET msg_count=? WHERE id=?", (keep_real, keep["id"]))
        tot_dead += len(dead)

    if apply:
        c.commit()
        s1 = c.execute("SELECT COUNT(*) n FROM sessions").fetchone()["n"]
        m1 = c.execute("SELECT COUNT(*) n FROM messages").fetchone()["n"]
        print(f"\n合并完成：删除 {tot_dead} 条重复会话")
        print(f"处理后：sessions={s1}  messages={m1}  （省下 {m0 - m1} 条消息）")
        left = c.execute(
            "SELECT COUNT(*) n FROM (SELECT source_path FROM sessions "
            "WHERE source_path<>'' GROUP BY source_path HAVING COUNT(*)>1)").fetchone()["n"]
        print(f"剩余重复来源组数: {left}（应为 0）")
    else:
        print(f"\n[DRY-RUN] 共可删除 {tot_dead} 条重复会话。确认无误后加 --apply 执行。")
    c.close()


if __name__ == "__main__":
    main()
