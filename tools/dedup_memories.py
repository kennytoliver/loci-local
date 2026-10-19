#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
清理重复记忆（2026-10-05）

**为什么需要**：用户反馈「同一条记忆在多处反复出现，严重影响观感」。
实测确认：144 条在用记忆里，按「归一化内容指纹」聚合后只有 115 组，
**29 条（20%）是重复**，最严重的一组同一个 skill 说明被存了 6 遍。

**判定规则**（纯算术，不用 LLM）：
  1. 指纹 = 去掉所有空白/标点/Markdown 符号后取前 120 字（识别「同一个东西的
     不同措辞」—— 完全相同的手工比对抓不到，同一天工作日志的 5 个不同长度版本才是大头）
  2. 同一指纹的一组里，**保留最长的一条**（内容最全），其余待删
  3. 长度相同时保留 id 大的（最新入库的）

**安全**：
  · 默认 dry-run，只打印不写
  · --apply 才真写，**真删前自动备份 .db**
  · ⚠️ 真删不可逆（不是 deleted=1 软删除），备份是唯一的回滚手段

用法：
  python tools/dedup_memories.py
  python tools/dedup_memories.py --apply --yes
"""
import os
import re
import sys
import glob
import shutil
import datetime
import sqlite3
import collections

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
DB = os.environ.get("LOCI_DB") or os.path.join(ROOT, "loci.db")

_STRIP = re.compile(r"[\s`*_#>|\-\[\]()`~]+")
_FP_LEN = 120


def fingerprint(text):
    """内容指纹：去空白/标点/Markdown 符号后取前 120 字，小写。"""
    return _STRIP.sub("", text or "").lower()[:_FP_LEN]


def plan(conn):
    rows = conn.execute(
        "SELECT id, mtype, project, content FROM memories "
        "WHERE deleted=0 ORDER BY id").fetchall()
    buckets = collections.defaultdict(list)
    for mid, mt, proj, content in rows:
        buckets[fingerprint(content)].append((mid, mt, proj or "", len(content or "")))

    dups = {k: v for k, v in buckets.items() if len(v) > 1}
    to_delete, groups = [], []
    for k, v in dups.items():
        # 保留：最长优先；等长时取 id 大的（更新）
        keep = max(v, key=lambda x: (x[3], x[0]))
        dels = [x for x in v if x[0] != keep[0]]
        to_delete.extend(dels)
        groups.append((k, keep, sorted(dels, key=lambda x: x[0])))
    groups.sort(key=lambda x: -len(x[2]))
    return rows, to_delete, groups


def main():
    apply = "--apply" in sys.argv
    assume_yes = "--yes" in sys.argv
    if not os.path.exists(DB):
        print("找不到数据库：%s" % DB)
        return 1

    conn = sqlite3.connect(DB)
    conn.row_factory = sqlite3.Row
    rows, to_delete, groups = plan(conn)

    print("=" * 68)
    print("重复记忆清理  |  %s" % DB)
    print("模式：%s" % ("**真删（不可逆，靠备份回滚）**" if apply else "预演（dry-run）"))
    print("=" * 68)
    print("\n在用 %d 条 → 按内容指纹聚合后 %d 组，重复 %d 组 / 冗余 %d 条\n"
          % (len(rows), len(rows) - len(to_delete), len(groups), len(to_delete)))

    for k, keep, dels in groups:
        print("保留 #%-4d %6d字 %-10s (%s)" % (keep[0], keep[3], keep[1], keep[2]))
        for d in dels:
            print("  删 #%-4d %6d字 %-10s (%s)" % (d[0], d[3], d[1], d[2]))
        print("  内容: %s...\n" % k[:56])

    freed = sum(d[3] for d in to_delete)
    print("-" * 68)
    print("合计待删 %d 条，预计释放 %.1f MB（%d 字符）"
          % (len(to_delete), freed / 1024 / 1024, freed))
    print("删除后剩 %d 条" % (len(rows) - len(to_delete)))

    if not apply:
        print("\n这是预演。加 --apply 才会真删。")
        conn.close()
        return 0
    if not to_delete:
        print("\n没有要删的。")
        conn.close()
        return 0
    if not assume_yes:
        ans = input("\n**真删不可逆**。确认？输入 yes 继续：").strip().lower()
        if ans != "yes":
            print("已取消，未改动任何数据。")
            conn.close()
            return 0

    # 备份（老备份自动清，只留最近 5 份，避免越攒越多）
    stamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
    bak = DB + ".bak-dedup-" + stamp
    conn.commit()
    conn.close()
    shutil.copy2(DB, bak)
    for old in sorted(glob.glob(DB + ".bak-dedup-*"))[:-5]:
        try:
            os.remove(old)
        except OSError:
            pass
    print("\n已备份：%s" % bak)

    conn = sqlite3.connect(DB)
    ids = [d[0] for d in to_delete]
    # 2026-10-05 防护（实测踩过的坑）：删条前先解除"别人指向它"的 superseded 关系。
    #   ⚠️ 今天第一次跑时删掉的 29 条里含 #75 / #83，而它们正是另外 4 条
    #      （#42 / #44 / #55 / #82）的 superseded_by 目标 —— 删完就留下 4 条
    #      **悬空指针**（指向不存在的 id），那 4 条有价值的内容从此不显示、不被检索。
    #   正确顺序：先把指向待删条的记录归 0（它们自己成为独立记忆），再删。
    #   若归 0 后产生新重复，交给下一轮 dedup 处理（可回归、无害）。
    freed_refs = conn.execute(
        "UPDATE memories SET superseded_by=0 WHERE superseded_by IN (%s)"
        % ",".join("?" * len(ids)), ids).rowcount
    if freed_refs:
        print("  · 先解除 %d 条指向待删记录的 superseded 关系（防悬空指针）" % freed_refs)
    conn.executemany("DELETE FROM memories WHERE id=?", [(i,) for i in ids])
    conn.commit()

    # 复查（重连后必须重设 row_factory，否则 fetchone() 返回 tuple）
    conn.row_factory = sqlite3.Row
    left = conn.execute("SELECT COUNT(*) c FROM memories WHERE deleted=0").fetchone()["c"]
    still = conn.execute(
        "SELECT COUNT(*) c FROM memories WHERE deleted=0 AND project LIKE '%\\%'").fetchone()["c"]
    # 去重后是否还有指纹重复
    rows2 = conn.execute("SELECT content FROM memories WHERE deleted=0").fetchall()
    fp2 = collections.Counter(fingerprint(r["content"]) for r in rows2)
    rest = sum(n - 1 for n in fp2.values() if n > 1)
    conn.close()

    print("\n删除后：剩 %d 条 | 仍含路径的 %d 条 | 剩余指纹重复 %d 条"
          % (left, still, rest))
    print("回滚：把 %s 覆盖回去即可。" % bak)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
