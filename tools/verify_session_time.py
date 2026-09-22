#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
会话时间管线断言（在临时库上跑，不动真库）。

为什么需要：2026-09-21 发现一个"静默错很久"的 bug ——
  `save_session` 从**消息**层读 `at`（msgs[0].get("at")），但没有任何调用方
  往消息里塞过 at，于是 started_at / ended_at 永远是空串（全表 0/10）。
  UI 拿不到会话时间只能退回 created_at（导入那一刻），
  结果「09-17 发生的对话」全被显示成 09-20（扫描那天）—— 差了三天。
  更坏的是：**所有闸门都是绿的**，因为没有任何断言检查过这个字段。

所以这里把三个环节都钉住：
  ① norm_time()   —— 各家时间格式归一（epoch 毫秒 / 秒 / ISO / 纯日期 / 垃圾值）
  ② save_session  —— 显式参数优先、其次消息自带 at/created_at、都没有则留空（不编造）
  ③ 端到端       —— 扫描器产物 → save_session → get_session，时间必须一路带到底

用法：python tools/verify_session_time.py
"""
import os
import shutil
import sqlite3
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

TMP = tempfile.mkdtemp(prefix="hippo-stime-")
os.environ["HIPPOCAMPUS_DB"] = os.path.join(TMP, "hippocampus.db")

import hippocampus as hippo  # noqa: E402

RESULTS = []


def rec(name, ok, info=""):
    RESULTS.append((name, ok, info))
    print(("  PASS  " if ok else "  FAIL  ") + name + ("  " + info if info else ""))


def section(t):
    print("\n── " + t)


def main():
    print("临时库：%s" % os.environ["HIPPOCAMPUS_DB"])

    # ── ① norm_time ────────────────────────────────────────────────────────
    section("norm_time 各格式归一")
    cases = [
        (1789911190551, "2026-09-20 21:33:10", "epoch 毫秒（ZCode 的格式）"),
        (1789911190, "2026-09-20 21:33:10", "epoch 秒"),
        ("2026-09-20 21:33:10", "2026-09-20 21:33:10", "已经是目标格式"),
        ("2026-09-20T21:33:10Z", "2026-09-20 21:33:10", "ISO（带 T 和 Z）"),
        ("2026-09-20T21:33:10+08:00", "2026-09-20 21:33:10", "ISO（带时区偏移）"),
        ("2026-09-20", "2026-09-20 00:00:00", "只有日期"),
        ("", "", "空串"),
        (None, "", "None"),
        (0, "", "0（不是时间）"),
        (-1, "", "负数"),
        ("abc", "", "垃圾字符串"),
        ("1.5", "", "小数（不够 epoch 量级）"),
        (1789911190551.0, "2026-09-20 21:33:10", "float 毫秒"),
    ]
    for raw, want, why in cases:
        got = hippo.norm_time(raw)
        rec("norm_time(%r) → %r" % (raw, want), got == want, "实得 %r（%s）" % (got, why) if got != want else why)

    # ── ② save_session 三种时间来源 ────────────────────────────────────────
    section("save_session 的时间来源优先级")
    # (a) 显式传参优先
    sid, created = hippo.save_session(
        title="显式时间", agent="test",
        messages=[{"role": "user", "content": "甲"}, {"role": "assistant", "content": "乙"}],
        started_at=1789911190551, ended_at="2026-09-20 22:00:00")
    s, msgs = hippo.get_session(sid)
    rec("显式 started_at 生效（epoch 毫秒被归一）", s["started_at"] == "2026-09-20 21:33:10",
        "实得 %r" % s["started_at"])
    rec("显式 ended_at 生效", s["ended_at"] == "2026-09-20 22:00:00", "实得 %r" % s["ended_at"])
    rec("没给消息时间时，消息退回导入时刻（不为空）",
        all(m["created_at"] for m in msgs), "第1条=%r" % msgs[0]["created_at"])

    # (b) 消息自带 at（扫描器写的键）
    sid2, _ = hippo.save_session(
        title="消息 at", agent="test",
        messages=[{"role": "user", "content": "丙", "at": "2026-09-17 16:21:56"},
                  {"role": "assistant", "content": "丁", "at": "2026-09-17 16:23:32"}])
    s2, m2 = hippo.get_session(sid2)
    rec("消息 at → started_at 兜底生效", s2["started_at"] == "2026-09-17 16:21:56",
        "实得 %r" % s2["started_at"])
    rec("消息 at → ended_at 取末条", s2["ended_at"] == "2026-09-17 16:23:32",
        "实得 %r" % s2["ended_at"])
    rec("每条消息各用自己的时间（不再全压成同一个）",
        [m["created_at"] for m in m2] == ["2026-09-17 16:21:56", "2026-09-17 16:23:32"],
        str([m["created_at"] for m in m2]))

    # (c) 消息自带 created_at（导出包的键）—— 往返不丢时间
    sid3, _ = hippo.save_session(
        title="消息 created_at", agent="test",
        messages=[{"role": "user", "content": "戊", "created_at": "2026-09-16 10:00:00"},
                  {"role": "assistant", "content": "己", "created_at": "2026-09-16 10:05:00"}])
    s3, _ = hippo.get_session(sid3)
    rec("消息 created_at（导出包键）也能兜底", s3["started_at"] == "2026-09-16 10:00:00",
        "实得 %r" % s3["started_at"])

    # (d) 完全没时间 → 留空，不编造
    sid4, _ = hippo.save_session(title="无时间", agent="test",
                                 messages=[{"role": "user", "content": "庚"}])
    s4, m4 = hippo.get_session(sid4)
    rec("没有任何时间来源时 started_at 留空（不编造）", s4["started_at"] == "",
        "实得 %r" % s4["started_at"])
    rec("但 created_at 一定有值（导入时刻，用于兜底展示）", bool(s4["created_at"]))

    # ── ③ 扫描器 → 落库 端到端 ────────────────────────────────────────────
    section("扫描器产物 → save_session → get_session 端到端")
    item = {"agent": "ZCode", "session": "sess_fake_1", "title": "端到端",
            "dir": "/tmp/x", "at": hippo.norm_time(1789911190551),
            "end": hippo.norm_time(1789911723610),
            "messages": [{"role": "user", "content": "端1", "at": hippo.norm_time(1789911190551)},
                         {"role": "assistant", "content": "端2", "at": hippo.norm_time(1789911220000)}]}
    sid5, ok5 = hippo.save_session(item["title"], item["dir"], item["agent"], item["messages"],
                                   source_path="agent-scan://" + item["session"],
                                   started_at=item["at"], ended_at=item["end"])
    s5, m5 = hippo.get_session(sid5)
    rec("端到端 started_at", s5["started_at"] == "2026-09-20 21:33:10", "实得 %r" % s5["started_at"])
    rec("端到端 ended_at", s5["ended_at"] == "2026-09-20 21:42:03", "实得 %r" % s5["ended_at"])
    rec("端到端：started <= ended", s5["started_at"] <= s5["ended_at"],
        "%s <= %s" % (s5["started_at"], s5["ended_at"]))
    rec("端到端：消息时间不等于导入时间（说明真带下来了）",
        m5[0]["created_at"] == "2026-09-20 21:33:10" and m5[0]["created_at"] != s5["created_at"],
        "msg=%r created=%r" % (m5[0]["created_at"], s5["created_at"]))

    # ── ④ 列表接口把 started_at 带出来（前端才有得显示） ───────────────────
    section("list_sessions 暴露 started_at")
    rows = [dict(r) for r in hippo.list_sessions(limit=50)]
    has_key = all("started_at" in r for r in rows)
    rec("list_sessions 返回 start/ended 字段", has_key)
    filled = [r for r in rows if r.get("started_at")]
    rec("其中至少几条有真实时间", len(filled) >= 3, "%d 条有值" % len(filled))
    # 排序按 id，不按时间 —— 但时间的先后关系必须自洽
    bad = [r["id"] for r in filled if r.get("ended_at") and r["ended_at"] < r["started_at"]]
    rec("没有 ended_at < started_at 的倒挂", not bad, "有问题：%s" % bad if bad else "")

    bad = [n for n, ok, _ in RESULTS if not ok]
    print("\n" + "═" * 52)
    print("通过 %d 项，失败 %d 项" % (len(RESULTS) - len(bad), len(bad)))
    if bad:
        print("红：" + "、".join(bad))
    return 1 if bad else 0


if __name__ == "__main__":
    try:
        code = main()
    finally:
        shutil.rmtree(TMP, ignore_errors=True)
    sys.exit(code)
