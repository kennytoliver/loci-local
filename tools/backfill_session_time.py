#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
把已归档会话的 started_at / ended_at / 每条消息时间，从来源库里回填。

为什么需要：2026-09-21 发现 `save_session` 从**消息**层读 `at`，但没有任何调用方
往消息里塞过 at，于是全表 10 个会话的 started_at / ended_at 都是空串，
UI 只能退回 created_at（导入那一刻的 now()）—— 结果"9 月 20 日的 10 段对话"
全显示成同一个扫描时刻。修好管线后，**已经入库的老数据**还需要回填一次。

回填来源：会话表里留着 source_path = "agent-scan://<zcode session id>"，
拿这个 id 回 ZCode 库查 session.time_created / time_updated。

安全约定：
  · 默认只预览（--dry-run 行为），要写必须显式 --apply
  · 消息时间只在**条数完全对得上**时才回填；对不上就只回填会话级时间，
    绝不按序号硬塞（错位的时间比没有时间更糟）
  · 回填前自动备份 hippocampus.db

用法：
  python tools/backfill_session_time.py            # 预览
  python tools/backfill_session_time.py --apply    # 真的写
"""
import argparse
import json
import os
import shutil
import sqlite3
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

import hippocampus as hippo  # noqa: E402

ZC_DB = os.path.join(os.path.expanduser("~"), ".zcode", "cli", "db", "db.sqlite")


def zcode_index():
    """从 ZCode 库读出 {session_id: {"start":…, "end":…, "msg_times":[…]}}。
    库可能被 ZCode 占用 → 复制副本再读，绝不动原库。"""
    if not os.path.isfile(ZC_DB):
        return {}
    work = os.path.join(tempfile.gettempdir(), "hippocampus-zcode-backfill.sqlite")
    try:
        shutil.copy2(ZC_DB, work)
    except Exception as e:
        print("！复制 ZCode 库失败：%s" % e)
        return {}
    idx = {}
    try:
        c = sqlite3.connect(work)
        c.row_factory = sqlite3.Row
        for s in c.execute("SELECT id, time_created, time_updated FROM session"):
            sid = s["id"]
            times = []
            for m in c.execute(
                    "SELECT id, data, time_created FROM message WHERE session_id=? ORDER BY sequence", (sid,)):
                try:
                    md = json.loads(m["data"] or "{}")
                except Exception:
                    continue
                if str(md.get("role") or "").lower() not in ("user", "assistant"):
                    continue
                # 必须和 scan_zcode_db 用同一套过滤：有正文才算一轮
                has_text = False
                for p in c.execute("SELECT data FROM part WHERE message_id=? ORDER BY sequence",
                                   (m["id"],)):
                    pd = _safe_json(p["data"])
                    if pd.get("type") == "text" and (pd.get("text") or "").strip():
                        has_text = True
                        break
                if has_text:
                    times.append(hippo.norm_time(m["time_created"]))
            idx[sid] = {"start": hippo.norm_time(s["time_created"]),
                        "end": hippo.norm_time(s["time_updated"]),
                        "msg_times": times}
        c.close()
    finally:
        try:
            os.remove(work)
        except Exception:
            pass
    return idx


def _safe_json(s):
    try:
        return json.loads(s or "{}")
    except Exception:
        return {}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true", help="真的写入（不加则只预览）")
    a = ap.parse_args()

    idx = zcode_index()
    print("ZCode 库里找到 %d 个会话可作来源\n" % len(idx))

    conn = hippo.db()
    rows = list(conn.execute(
        "SELECT id,title,agent,source_path,started_at,ended_at,created_at,msg_count "
        "FROM sessions WHERE (started_at IS NULL OR started_at='') ORDER BY id"))

    if not rows:
        print("没有需要回填的会话（started_at 都已有值）。")
        conn.close()
        return 0

    plan = []
    for r in rows:
        sp = r["source_path"] or ""
        if not sp.startswith("agent-scan://"):
            plan.append((r, None, "来源不是扫描导入，无法回填"))
            continue
        sid = sp[len("agent-scan://"):]
        z = idx.get(sid)
        if not z or not z.get("start"):
            plan.append((r, None, "ZCode 库里已找不到该会话"))
            continue
        plan.append((r, z, ""))

    print("%-4s %-30s %-19s %-19s %s" % ("id", "标题", "started_at", "ended_at", "说明"))
    print("-" * 100)
    ok = 0
    for r, z, why in plan:
        if z:
            ok += 1
            print("%-4d %-30s %-19s %-19s 可回填（消息时间 %d 条 vs 库里 %d 条%s）"
                  % (r["id"], (r["title"] or "")[:28], z["start"], z["end"] or "-",
                     len(z["msg_times"]), r["msg_count"],
                     "  ✓对齐" if len(z["msg_times"]) == r["msg_count"] else "  ✗不对齐，只回填会话级"))
        else:
            print("%-4d %-30s %-19s %-19s %s" % (r["id"], (r["title"] or "")[:28], "(空)", "(空)", why))
    print("-" * 100)
    print("可回填 %d 个，跳过 %d 个" % (ok, len(plan) - ok))

    if not a.apply:
        conn.close()
        print("\n这是预览。确认无误后加 --apply 真正写入。")
        return 0

    # 备份后写
    bak = os.path.join(ROOT, "backup-before-timebackfill-%s"
                       % hippo.datetime.datetime.now().strftime("%Y%m%d-%H%M%S"))
    os.makedirs(bak, exist_ok=True)
    shutil.copy2(hippo.DB_PATH, os.path.join(bak, "hippocampus.db"))
    print("\n已备份到 %s" % bak)

    n_sess = n_msg = 0
    for r, z, why in plan:
        if not z:
            continue
        conn.execute("UPDATE sessions SET started_at=?, ended_at=? WHERE id=?",
                     (z["start"], z["end"] or z["start"], r["id"]))
        n_sess += 1
        if len(z["msg_times"]) == r["msg_count"]:
            for i, t in enumerate(z["msg_times"], 1):
                if t:
                    conn.execute("UPDATE messages SET created_at=? WHERE session_id=? AND turn=?",
                                 (t, r["id"], i))
                    n_msg += 1
    conn.commit()
    conn.close()
    print("回填完成：会话 %d 个，消息 %d 条" % (n_sess, n_msg))
    return 0


if __name__ == "__main__":
    sys.exit(main())
