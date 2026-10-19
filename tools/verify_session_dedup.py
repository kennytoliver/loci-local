# -*- coding: utf-8 -*-
"""闸门：会话同源去重（2026-09-22 用户报的 bug #2）。

用户原话："会话列表里面……753 轮 734 733 这种重复的，为什么不能合并？"
根因：采集反复把同一段对话的"全量快照"重新 save，而旧去重只按整段消息的 md5，
      对话一变长指纹就变 → 同一个 source_path 堆了 62 条，messages 被灌到 4 万条。
修在两处：
  · save_session：source_path 非空时走同源幂等（原地更新，清同源其余快照）
  · db()：建 source_path 的部分唯一索引 idx_sess_src 兜底
本闸门把这两条钉住，防复发。

断言：
  A1 同源连续保存 → sid 稳定，第二次起 created=False
  A2 同源恒为 1 条；消息数以"最全"为准（更短的一次不覆盖）
  A3 真实库 idx_sess_src 唯一索引存在
  A4 真实库无重复来源（0 组）—— 历史脏数据已被 merge_dup_sessions.py 清掉
"""
import importlib
import os
import sqlite3
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

_ok = True


def ck(name, cond, extra=""):
    global _ok
    print(("  PASS " if cond else "  FAIL ") + name + ((" | " + extra) if extra else ""))
    _ok = _ok and bool(cond)


# ---- 隔离库测幂等（绝不碰真实库）----
TMP = os.path.join(tempfile.gettempdir(), "hp_gate_dedup.db")
for _s in ("", "-wal", "-shm"):
    try:
        os.remove(TMP + _s)
    except OSError:
        pass
os.environ["LOCI_DB"] = TMP
import loci as h  # noqa: E402

SRC = "agent-scan://gate-dup"
a = [{"role": "user", "content": "第一轮"}, {"role": "assistant", "content": "回答"}]
sid1, c1 = h.save_session("t1", "", "test", a, source_path=SRC)
sid2, c2 = h.save_session("t2", "", "test", a + [{"role": "user", "content": "第二轮"}], source_path=SRC)
sid3, c3 = h.save_session("t3", "", "test", a[:1], source_path=SRC)
conn = h.db()
n = conn.execute("SELECT COUNT(*) FROM sessions WHERE source_path=?", (SRC,)).fetchone()[0]
m = conn.execute("SELECT COUNT(*) FROM messages WHERE session_id=?", (sid1,)).fetchone()[0]
conn.close()

ck("A1 同源重复保存 sid 稳定且 created=False",
   sid1 == sid2 == sid3 and c1 and not c2 and not c3,
   "sid=%s,%s,%s created=%s,%s,%s" % (sid1, sid2, sid3, c1, c2, c3))
ck("A2 同源恒 1 条、消息不被更短内容截断", n == 1 and m == 3, "条数=%s(1) 消息=%s(3)" % (n, m))

# ---- 真实库检查索引与残留 ----
os.environ.pop("LOCI_DB", None)
importlib.reload(h)
real = h.db()
idx = real.execute("SELECT name FROM sqlite_master WHERE type='index' AND name='idx_sess_src'").fetchone()
ck("A3 真实库 idx_sess_src 唯一索引存在", bool(idx))
dups = real.execute(
    "SELECT COUNT(*) FROM (SELECT source_path FROM sessions "
    "WHERE source_path<>'' GROUP BY source_path HAVING COUNT(*)>1)").fetchone()[0]
ck("A4 真实库无重复来源", dups == 0, "重复组=%s" % dups)
real.close()

print("闸门结果:", "PASS" if _ok else "FAIL")
sys.exit(0 if _ok else 1)
