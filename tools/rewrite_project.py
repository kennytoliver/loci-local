#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
project 字段历史数据重写（2026-10-05）

**为什么需要这个**：norm_project() 只在**写入前**归一，库里的历史数据还是
2026-09-05 之前的老格式（整条 Windows 路径 / 纯时间戳 / 子代理目录三种混用）。
Supermemory 和 Mem0 官方文档都明写「改配置不回溯已有数据」—— 所以必须单独
跑一次全量重写，否则 project 筛选永远漏。

**它做什么**：把 memories.project 和 sessions.project 按同一套规则重写。

**安全**：
  · 默认 **dry-run**，只打印不写
  · 真正写入必须显式加 --apply
  · 动手前自动备份一份 .db

用法：
  python tools/rewrite_project.py              # 预演，看会改成什么
  python tools/rewrite_project.py --apply      # 真写（自动备份）
  python tools/rewrite_project.py --apply --yes # 不再问
"""
import os
import shutil
import sqlite3
import sys
import datetime

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

import importlib.util
_spec = importlib.util.spec_from_file_location("loci_mod", os.path.join(ROOT, "loci.py"))
_loci = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_loci)
norm_project = _loci.norm_project

DB = os.environ.get("LOCI_DB") or os.path.join(ROOT, "loci.db")
UNCAT = "未分类"


def rewrite_table(conn, table):
    """重写一张表。返回 (变更条数, 变更明细列表)。"""
    rows = conn.execute("SELECT id, project FROM %s" % table).fetchall()
    changes = []
    for r in rows:
        old = r["project"] or ""
        new = norm_project(old) or (UNCAT if old.strip() else "")
        if new != old:
            changes.append((r["id"], old, new))
    return changes


def main():
    apply = "--apply" in sys.argv
    assume_yes = "--yes" in sys.argv

    if not os.path.exists(DB):
        print("找不到数据库：%s" % DB)
        return 1

    conn = sqlite3.connect(DB)
    conn.row_factory = sqlite3.Row

    print("=" * 66)
    print("project 字段重写  |  %s" % DB)
    print("模式：%s" % ("**真正写入**" if apply else "预演（dry-run，不改数据）"))
    print("=" * 66)

    plan = {}
    for table in ("memories", "sessions"):
        ch = rewrite_table(conn, table)
        plan[table] = ch
        print("\n【%s】需要改 %d 条" % (table, len(ch)))
        # 按「旧值 → 新值」汇总，直观看到合并效果
        agg = {}
        for mid, old, new in ch:
            agg[(old, new)] = agg.get((old, new), 0) + 1
        for (old, new), n in sorted(agg.items(), key=lambda x: -x[1]):
            print("  %4d 条  %-46s → %s" % (n, (old or "(空)")[:46], new))

    total = sum(len(v) for v in plan.values())
    print("\n" + "-" * 66)
    print("合计需要改 %d 条" % total)

    if not apply:
        print("\n这是预演。加 --apply 才会真正写入。")
        conn.close()
        return 0

    if not total:
        print("\n没有需要改的，跳过。")
        conn.close()
        return 0

    if not assume_yes:
        ans = input("\n确认写入？输入 yes 继续：").strip().lower()
        if ans != "yes":
            print("已取消，未改动任何数据。")
            conn.close()
            return 0

    # 备份（只备份一次，两张表共用）
    stamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
    bak = DB + ".bak-project-" + stamp
    conn.commit()
    conn.close()
    shutil.copy2(DB, bak)
    print("\n已备份：%s" % bak)

    conn = sqlite3.connect(DB)
    for table, ch in plan.items():
        if not ch:
            continue
        conn.executemany(
            "UPDATE %s SET project=? WHERE id=?" % table,
            [(new, mid) for mid, old, new in ch])
    conn.commit()

    # 复查
    # ⚠️ 这一段重新 connect 了，必须重设 row_factory —— 否则 fetchone() 返回的是
    # tuple，row["c"] 会抛 TypeError（2026-10-05 实测踩过，写入成功但脚本报错）。
    conn = sqlite3.connect(DB)
    conn.row_factory = sqlite3.Row
    print("\n写入后复查：")
    for table in ("memories", "sessions"):
        n = conn.execute(
            "SELECT COUNT(DISTINCT project) c FROM %s WHERE project!=''" % table).fetchone()["c"]
        longn = conn.execute(
            "SELECT COUNT(*) c FROM %s WHERE project LIKE '%\\%' OR project LIKE '%/%'" % table
        ).fetchone()["c"]
        print("  %s：%d 个不同 project | 仍含路径分隔符：%d 条"
              % (table, n, longn))
    conn.close()
    print("\n完成。回滚：把 %s 覆盖回去即可。" % bak)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
