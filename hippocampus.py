#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Hippocampus（海马体）— 个人跨 Agent 记忆中枢（零依赖单文件）
================================================
- 存储: SQLite 单文件, 零运维
- 检索: 中文 bigram + 英文 token 混合 TF-IDF (专为中文优化)
- 接入: MCP stdio (JSON-RPC 2.0) — 任何支持 MCP 的 Agent 都能接
- 同时提供 CLI 模式, 方便 bat 快捷方式和手动使用

用法:
  python hippocampus.py                 # MCP server 模式 (stdio)
  python hippocampus.py --cli           # 交互式 CLI
  python hippocampus.py --save "内容" --type decision --imp 4 --tags a,b --proj 项目名
  python hippocampus.py --search "查询"
"""
import sys, os, io, json, math, re, sqlite3, argparse, datetime, hashlib

# 环境变量：优先新名，兼容旧名（老配置里可能还写着 HIPPOHUB_DB）
_ENV_DB = os.environ.get("HIPPOCAMPUS_DB") or os.environ.get("HIPPOHUB_DB")
# 来源标记：各 Agent 的 MCP 配置里设置 HIPPOCAMPUS_AGENT，写入记忆时自动带上，
# 避免调用方自己猜导致来源失真（曾出现 Trae 写入却被记成 WorkBuddy）
DEFAULT_AGENT = os.environ.get("HIPPOCAMPUS_AGENT") or ""
DB_PATH = _ENV_DB or os.path.join(os.path.dirname(os.path.abspath(__file__)), "hippocampus.db")
CJK = re.compile(r"[一-鿿　-〿＀-￯]")
WORD = re.compile(r"[a-zA-Z0-9_+\-.#]{2,}")
TYPES = ("fact", "preference", "context", "decision", "error", "skill", "summary")

# ---------- 分词：CJK 走 bigram，ASCII 走单词 ----------
def tokenize(text):
    text = text.lower()
    toks = []
    buf = []
    for ch in text:
        if CJK.match(ch):
            buf.append(ch)
        else:
            if len(buf) == 1:
                toks.append(buf[0])
            else:
                for i in range(len(buf) - 1):
                    toks.append(buf[i] + buf[i + 1])
            buf = []
    if len(buf) == 1:
        toks.append(buf[0])
    else:
        for i in range(len(buf) - 1):
            toks.append(buf[i] + buf[i + 1])
    toks.extend(WORD.findall(text))
    return toks

# ---------- 存储层 ----------
SCHEMA = """
CREATE TABLE IF NOT EXISTS memories (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  content TEXT NOT NULL,
  mtype TEXT NOT NULL DEFAULT 'fact',
  importance INTEGER NOT NULL DEFAULT 2,
  tags TEXT DEFAULT '',
  project TEXT DEFAULT '',
  agent TEXT DEFAULT '',
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  deleted INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_mem_project ON memories(project);
CREATE INDEX IF NOT EXISTS idx_mem_type ON memories(mtype);

-- 会话层：保存「对话原貌」本体（记忆层只存提炼后的结论，会话层存过程与原文）
CREATE TABLE IF NOT EXISTS sessions (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  title TEXT DEFAULT '',
  project TEXT DEFAULT '',
  agent TEXT DEFAULT '',
  source_path TEXT DEFAULT '',
  started_at TEXT DEFAULT '',
  ended_at TEXT DEFAULT '',
  msg_count INTEGER NOT NULL DEFAULT 0,
  summary TEXT DEFAULT '',
  fingerprint TEXT DEFAULT '',
  created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_sess_project ON sessions(project);
CREATE UNIQUE INDEX IF NOT EXISTS idx_sess_fp ON sessions(fingerprint);

CREATE TABLE IF NOT EXISTS messages (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  session_id INTEGER NOT NULL,
  turn INTEGER NOT NULL DEFAULT 0,
  role TEXT DEFAULT '',
  content TEXT DEFAULT '',
  created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_msg_session ON messages(session_id);
"""

def db():
    # timeout：多 Agent 同时写时等待锁而不是立刻报 database is locked
    conn = sqlite3.connect(DB_PATH, timeout=10)
    conn.row_factory = sqlite3.Row
    try:
        # WAL：读不阻塞写、写不阻塞读，多进程（多个 Agent）并发场景更稳
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=10000")
    except Exception:
        pass
    conn.executescript(SCHEMA)
    # 轻量迁移：老库自动补列，不会丢数据
    for tbl, col, decl in (("memories", "pinned", "INTEGER NOT NULL DEFAULT 0"),
                           ("memories", "superseded_by", "INTEGER NOT NULL DEFAULT 0"),
                           ("memories", "source_path", "TEXT DEFAULT ''")):
        cols = [r[1] for r in conn.execute('PRAGMA table_info("%s")' % tbl)]
        if col not in cols:
            conn.execute('ALTER TABLE "%s" ADD COLUMN %s %s' % (tbl, col, decl))
    conn.commit()
    return conn

def now():
    return datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")

def save_memory(content, mtype="fact", importance=2, tags="", project="", agent="", source_path=""):
    if mtype not in TYPES:
        mtype = "fact"
    importance = max(1, min(4, int(importance)))
    conn = db()
    t = now()
    cur = conn.execute(
        "INSERT INTO memories (content, mtype, importance, tags, project, agent, source_path, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?)",
        (content, mtype, importance, tags, project, agent, source_path, t, t))
    conn.commit()
    mid = cur.lastrowid
    conn.close()
    return mid

def search_memory(query, limit=5, mtype=None, project=None):
    conn = db()
    sql = "SELECT * FROM memories WHERE deleted=0 AND superseded_by=0"
    args = []
    if mtype:
        sql += " AND mtype=?"; args.append(mtype)
    if project:
        sql += " AND project=?"; args.append(project)
    rows = conn.execute(sql, args).fetchall()
    conn.close()
    if not rows:
        return []
    q_toks = tokenize(query)
    if not q_toks:
        return []
    # 文档集 idf
    docs = [(r, tokenize(r["content"])) for r in rows]
    df = {}
    for _, toks in docs:
        for t in set(toks):
            df[t] = df.get(t, 0) + 1
    n = len(docs)
    idf = {t: math.log((n + 1) / (c + 0.5)) + 1 for t, c in df.items()}
    results = []
    today = datetime.date.today()
    for r, toks in docs:
        if not toks:
            continue
        tf = {}
        for t in toks:
            tf[t] = tf.get(t, 0) + 1
        score = 0.0
        for t in q_toks:
            if t in tf:
                score += idf.get(t, 1.0) * (tf[t] / len(toks))
        if score <= 0:
            continue
        score = score / math.sqrt(len(toks))
        score *= 1.0 + 0.15 * (r["importance"] - 1)
        # 零依赖加权：常驻 / 项目命中 / 标签命中（不加外部模型）
        if r["pinned"]:
            score *= 1.35
        if project and r["project"] == project:
            score *= 1.25
        qset = set(q_toks)
        if r["project"] and any(t in qset for t in tokenize(r["project"])):
            score *= 1.30
        if r["tags"] and any(t in qset for t in tokenize(r["tags"])):
            score *= 1.20
        try:
            days = (today - datetime.datetime.strptime(r["created_at"][:10], "%Y-%m-%d").date()).days
            score *= 1.0 + 0.02 * max(0, 30 - days)
        except Exception:
            pass
        results.append((score, r))
    results.sort(key=lambda x: x[0], reverse=True)
    return results[:limit]

def list_memories(project=None, mtype=None, limit=20, include_superseded=False):
    conn = db()
    sql = "SELECT * FROM memories WHERE deleted=0"
    if not include_superseded:
        sql += " AND superseded_by=0"
    args = []
    if project:
        sql += " AND project=?"; args.append(project)
    if mtype:
        sql += " AND mtype=?"; args.append(mtype)
    sql += " ORDER BY id DESC LIMIT ?"
    args.append(limit)
    rows = conn.execute(sql, args).fetchall()
    conn.close()
    return rows

def delete_memory(mid):
    conn = db()
    conn.execute("UPDATE memories SET deleted=1, updated_at=? WHERE id=?", (now(), mid))
    conn.commit()
    conn.close()

def stats():
    conn = db()
    A = "deleted=0 AND superseded_by=0"
    total = conn.execute("SELECT COUNT(*) c FROM memories WHERE " + A).fetchone()["c"]
    pinned = conn.execute("SELECT COUNT(*) c FROM memories WHERE " + A + " AND pinned=1").fetchone()["c"]
    superseded = conn.execute("SELECT COUNT(*) c FROM memories WHERE deleted=0 AND superseded_by!=0").fetchone()["c"]
    by_type = {r["mtype"]: r["c"] for r in conn.execute("SELECT mtype, COUNT(*) c FROM memories WHERE " + A + " GROUP BY mtype")}
    by_proj = {r["project"]: r["c"] for r in conn.execute("SELECT project, COUNT(*) c FROM memories WHERE " + A + " AND project!='' GROUP BY project")}
    by_agent = {r["agent"]: r["c"] for r in conn.execute("SELECT agent, COUNT(*) c FROM memories WHERE " + A + " AND agent!='' GROUP BY agent")}
    sess = conn.execute("SELECT COUNT(*) c, COALESCE(SUM(msg_count),0) n FROM sessions").fetchone()
    conn.close()
    return {"total": total, "pinned": pinned, "superseded": superseded,
            "by_type": by_type, "by_project": by_proj, "by_agent": by_agent,
            "sessions": sess["c"], "messages": sess["n"]}

# ---------- 常驻记忆 / 上下文包 ----------
def set_pinned(mid, pinned=1):
    conn = db()
    conn.execute("UPDATE memories SET pinned=?, updated_at=? WHERE id=?",
                 (1 if pinned else 0, now(), mid))
    conn.commit()
    conn.close()

def list_pinned(project=None):
    conn = db()
    sql = "SELECT * FROM memories WHERE deleted=0 AND superseded_by=0 AND pinned=1"
    args = []
    if project:
        sql += " AND project=?"; args.append(project)
    sql += " ORDER BY importance DESC, id DESC"
    rows = conn.execute(sql, args).fetchall()
    conn.close()
    return rows

def context_pack(project=None, limit=8):
    """常驻上下文包：Agent 开聊时调用，一次拿到「始终适用」的记忆 + 近期重点"""
    pinned = [dict(r) for r in list_pinned(project)]
    conn = db()
    sql = "SELECT * FROM memories WHERE deleted=0 AND superseded_by=0"
    args = []
    if project:
        sql += " AND project=?"; args.append(project)
    sql += " ORDER BY importance DESC, id DESC LIMIT ?"
    args.append(limit)
    recent = [dict(r) for r in conn.execute(sql, args).fetchall()]
    s = stats()
    conn.close()
    seen, merged = set(), []
    for r in pinned + recent:
        if r["id"] in seen:
            continue
        seen.add(r["id"]); merged.append(r)
    label = {"decision": "决策", "preference": "偏好", "error": "坑", "skill": "经验",
             "fact": "事实", "context": "背景", "summary": "摘要"}
    head = "# Hippocampus 常驻上下文" + (("（项目：" + project + "）") if project else "")
    lines = [head, "> 有效记忆 %d 条｜常驻 %d 条｜本机共享，跨 Agent 通用"
             % (s["total"], s.get("pinned", 0)), ""]
    if pinned:
        lines.append("## 常驻记忆（始终适用，优先级最高）")
        for r in pinned:
            lines.append("- [%s] %s" % (label.get(r["mtype"], r["mtype"]), r["content"]))
        lines.append("")
    others = [r for r in merged if not r["pinned"]]
    if others:
        lines.append("## 近期重点记忆")
        for r in others:
            lines.append("- [%s|%s] %s" % (label.get(r["mtype"], r["mtype"]),
                                           "★" * r["importance"], r["content"]))
        lines.append("")
    lines.append("需要更多细节：memory_search（记忆）/ session_recall（对话原文）。")
    return "\n".join(lines)

# ---------- 记忆质检：重复 / 矛盾 / 过期 ----------
CHANGE_WORDS = ("改成", "改为", "换成", "不再", "不用", "停止", "取消", "废弃", "作废",
                "调整", "更新", "取代", "替代", "新方案", "现在用", "已经改")

def similarity(a, b):
    """bigram 集合 Jaccard 相似度（零依赖，无需向量模型）"""
    sa, sb = set(tokenize(a)), set(tokenize(b))
    if not sa or not sb:
        return 0.0
    return len(sa & sb) / len(sa | sb)

def containment(a, b):
    """包含度：短的那条有多少比例被长的覆盖（对付「同义+补充说明」型重复）"""
    sa, sb = set(tokenize(a)), set(tokenize(b))
    if not sa or not sb:
        return 0.0
    return len(sa & sb) / min(len(sa), len(sb))

def audit_memories(project=None, dup_th=0.66, contain_th=0.82, conflict_lo=0.28, stale_days=90):
    """记忆质检七查：重复 / 疑似同义 / 可能矛盾 / 长期未更新 / 过短 / 过粗粒度 / 元数据缺失"""
    rows = [dict(r) for r in list_memories(project=project, limit=10000)]
    n = len(rows)
    parent = list(range(n))

    def find(x):
        while parent[x] != x:
            parent[x] = parent[parent[x]]; x = parent[x]
        return x

    def union(x, y):
        rx, ry = find(x), find(y)
        if rx != ry:
            parent[ry] = rx

    conflicts = []
    suspects = []
    for i in range(n):
        for j in range(i + 1, n):
            jac = similarity(rows[i]["content"], rows[j]["content"])
            con = containment(rows[i]["content"], rows[j]["content"])
            if jac >= 0.50 or con >= 0.70:
                union(i, j)
            else:
                same_proj = (rows[i]["project"] or "") == (rows[j]["project"] or "")
                a_chg = any(w in rows[i]["content"] for w in CHANGE_WORDS)
                b_chg = any(w in rows[j]["content"] for w in CHANGE_WORDS)
                if same_proj and a_chg != b_chg and jac >= 0.25:
                    conflicts.append({"older": rows[i] if rows[i]["id"] < rows[j]["id"] else rows[j],
                                      "newer": rows[j] if rows[i]["id"] < rows[j]["id"] else rows[i],
                                      "similarity": round(max(jac, con * 0.9), 3)})
                elif (same_proj and rows[i]["mtype"] == rows[j]["mtype"] and jac >= 0.25):
                    suspects.append({"a": rows[i], "b": rows[j], "similarity": round(jac, 3)})
    conflicts.sort(key=lambda x: -x["similarity"])
    suspects.sort(key=lambda x: -x["similarity"])
    groups = {}
    for i in range(n):
        groups.setdefault(find(i), []).append(rows[i])
    duplicates = [g for g in groups.values() if len(g) > 1]
    duplicates.sort(key=len, reverse=True)
    conflicts.sort(key=lambda x: -x["similarity"])
    today = datetime.date.today()
    stale = []
    for r in rows:
        if r["pinned"]:
            continue
        try:
            days = (today - datetime.datetime.strptime(r["updated_at"][:10], "%Y-%m-%d").date()).days
        except Exception:
            continue
        if days >= stale_days:
            r["days"] = days
            stale.append(r)
    stale.sort(key=lambda x: -x["days"])
    return {"scanned": n, "duplicates": duplicates, "conflicts": conflicts, "suspects": suspects,
            "stale": stale, "stale_days": stale_days}

def supersede_memory(old_id, new_id):
    """用新记忆取代旧记忆：旧记忆标记作废但保留（学 Zep，不删除历史）"""
    conn = db()
    conn.execute("UPDATE memories SET superseded_by=?, updated_at=? WHERE id=?",
                 (int(new_id), now(), int(old_id)))
    conn.commit()
    conn.close()
    return {"ok": True}

def retire_memory(mid):
    """作废一条记忆（无后续版本）；记录保留但不参与检索"""
    conn = db()
    conn.execute("UPDATE memories SET superseded_by=?, updated_at=? WHERE id=?",
                 (-int(mid), now(), int(mid)))
    conn.commit()
    conn.close()
    return {"ok": True}

def touch_memory(mid):
    """续期：刷新最后确认时间，让过期检查重新计时"""
    conn = db()
    conn.execute("UPDATE memories SET updated_at=? WHERE id=?", (now(), int(mid)))
    conn.commit()
    conn.close()
    return {"ok": True}

# ---------- 陈旧检测与批量清理（源头已消失 / 我不要的记忆） ----------
_WS_PAT = re.compile(r"^\d{4}-\d{2}-\d{2}-\d{2}-\d{2}-\d{2}$")

def find_orphans(workspace_root=None):
    """找出「源已消失」的残留：采集/导入时源目录或源文件已被删除，但副本还留在记忆库里"""
    home = os.path.expanduser("~")
    root = workspace_root or os.path.join(home, "WorkBuddy")
    conn = db()
    projects = []
    for r in conn.execute("SELECT DISTINCT project FROM memories WHERE deleted=0 AND project!=''"):
        p = r["project"]
        if not _WS_PAT.match(p):
            continue
        cands = [os.path.join(root, p), os.path.join(home, p)]
        if not any(os.path.isdir(c) for c in cands):
            projects.append(p)
    memories = []
    if projects:
        q = ",".join("?" * len(projects))
        memories = [dict(x) for x in conn.execute(
            "SELECT * FROM memories WHERE deleted=0 AND project IN (%s)" % q, projects)]
    paths = []
    for r in conn.execute("SELECT DISTINCT source_path FROM sessions WHERE source_path!=''"):
        sp = r["source_path"]
        if (os.sep in sp or "/" in sp) and not os.path.exists(sp):
            paths.append(sp)
    sessions = []
    if paths:
        q = ",".join("?" * len(paths))
        sessions = [dict(x) for x in conn.execute(
            "SELECT * FROM sessions WHERE source_path IN (%s)" % q, paths)]
    conn.close()
    return {"workspace_root": root, "projects": projects, "memories": memories,
            "source_paths": paths, "sessions": sessions}

def bulk_delete(project=None, agent=None, before=None, only_superseded=False):
    """批量【硬删除】记忆（调用方必须先备份）。返回删除明细"""
    conn = db()
    where, args = [], []
    if only_superseded:
        where.append("superseded_by!=0")
    else:
        where.append("deleted=0")
    if project:
        where.append("project=?"); args.append(project)
    if agent:
        where.append("agent=?"); args.append(agent)
    if before:
        where.append("created_at<?"); args.append(str(before)[:10] + " 00:00:00")
    sql = "SELECT * FROM memories WHERE " + " AND ".join(where)
    rows = [dict(r) for r in conn.execute(sql, args)]
    if rows:
        ids = [r["id"] for r in rows]
        conn.execute("DELETE FROM memories WHERE id IN (%s)" % ",".join("?" * len(ids)), ids)
        conn.commit()
    conn.close()
    return {"deleted": len(rows), "items": rows}

def bulk_delete_ids(ids):
    """按 id 列表硬删除记忆（逐个勾选删除用）"""
    ids = [int(i) for i in (ids or [])]
    if not ids:
        return {"deleted": 0, "items": []}
    conn = db()
    q = ",".join("?" * len(ids))
    rows = [dict(r) for r in conn.execute("SELECT * FROM memories WHERE id IN (%s)" % q, ids)]
    conn.execute("DELETE FROM memories WHERE id IN (%s)" % q, ids)
    conn.commit()
    conn.close()
    return {"deleted": len(rows), "items": rows}

def purge_superseded():
    """清空所有已作废记忆（硬删除）"""
    return bulk_delete(only_superseded=True)

def bulk_delete_projects(projects):
    """按项目名列表批量硬删除记忆（孤儿清理用）"""
    if not projects:
        return {"deleted": 0, "items": []}
    conn = db()
    q = ",".join("?" * len(projects))
    rows = [dict(r) for r in conn.execute(
        "SELECT * FROM memories WHERE deleted=0 AND project IN (%s)" % q, projects)]
    if rows:
        ids = [r["id"] for r in rows]
        conn.execute("DELETE FROM memories WHERE id IN (%s)" % ",".join("?" * len(ids)), ids)
        conn.commit()
    conn.close()
    return {"deleted": len(rows), "items": rows}

def delete_sessions_bulk(sids):
    """批量硬删除会话（含其消息）"""
    if not sids:
        return {"deleted": 0}
    conn = db()
    q = ",".join("?" * len(sids))
    conn.execute("DELETE FROM messages WHERE session_id IN (%s)" % q, sids)
    conn.execute("DELETE FROM sessions WHERE id IN (%s)" % q, sids)
    conn.commit()
    conn.close()
    return {"deleted": len(sids)}

def merge_memories(ids, content=None):
    """合并重复记忆：保留最新一条（可指定合并后的内容），其余标记作废"""
    ids = [int(i) for i in ids]
    if len(ids) < 2:
        return {"error": "至少需要两条才能合并"}
    rows = [dict(r) for r in list_memories(limit=10000) if r["id"] in ids]
    if not rows:
        return {"error": "记忆不存在"}
    keep = max(rows, key=lambda r: r["id"])
    if content is None or not str(content).strip():
        content = max((r["content"] for r in rows), key=len)
    any_pinned = 1 if any(r["pinned"] for r in rows) else 0
    max_imp = max(r["importance"] for r in rows)
    conn = db()
    conn.execute("UPDATE memories SET content=?, pinned=?, importance=?, updated_at=? WHERE id=?",
                 (str(content).strip(), any_pinned, max_imp, now(), keep["id"]))
    for r in rows:
        if r["id"] != keep["id"]:
            conn.execute("UPDATE memories SET superseded_by=?, updated_at=? WHERE id=?",
                         (keep["id"], now(), r["id"]))
    conn.commit()
    conn.close()
    return {"ok": True, "keep": keep["id"], "merged": len(rows) - 1}

# ---------- 质检扩展：健康度评分 / 粒度检查 / 拆分 / 报告 ----------
COARSE_LEN = 1500   # 超过这个字数视为「粒度太粗」（整篇日志塞成一条）
TINY_LEN = 20       # 低于这个字数视为「过短」

def quality_scan(project=None):
    """七查：重复 / 疑似同义 / 矛盾 / 过期 / 过短 / 过粗粒度 / 元数据缺失"""
    a = audit_memories(project=project)
    conn = db()
    sql = "SELECT * FROM memories WHERE deleted=0 AND superseded_by=0"
    args = []
    if project:
        sql += " AND project=?"; args.append(project)
    rows = [dict(r) for r in conn.execute(sql, args)]
    conn.close()
    tiny = [r for r in rows if len((r["content"] or "").strip()) < TINY_LEN]
    coarse = [r for r in rows if len((r["content"] or "")) > COARSE_LEN]
    no_project = [r for r in rows if not (r["project"] or "").strip()]
    no_tags = [r for r in rows if not (r["tags"] or "").strip()]
    a.update({"scanned": len(rows), "tiny": tiny, "coarse": coarse,
              "no_project": no_project, "no_tags": no_tags})
    return a

def health_score(project=None):
    """记忆库健康度 0-100（四维加权：重复 25 + 过期 25 + 元数据 25 + 粒度 25）"""
    q = quality_scan(project)
    rows = q["scanned"]
    if rows == 0:
        return {"score": 100, "grade": "A", "counts": {}, "penalties": {},
                "scanned": 0, "note": "库里还没有记忆"}
    dup_extra = sum(len(g) - 1 for g in q["duplicates"]) + len(q.get("suspects") or [])
    stale = len(q["stale"])
    meta_missing = len({r["id"] for r in q["no_project"]} | {r["id"] for r in q["no_tags"]})
    grain_bad = len({r["id"] for r in q["coarse"]} | {r["id"] for r in q["tiny"]})
    penalties = {
        "重复 / 疑似同义": round(25.0 * min(1.0, dup_extra / rows), 1),
        "长期未更新": round(25.0 * min(1.0, stale / rows), 1),
        "元数据缺失": round(25.0 * min(1.0, meta_missing / rows), 1),
        "粒度不合理": round(25.0 * min(1.0, grain_bad / rows), 1),
    }
    score = max(0, round(100 - sum(penalties.values())))
    grade = "A" if score >= 90 else ("B" if score >= 80 else ("C" if score >= 70 else "D"))
    return {"score": score, "grade": grade, "scanned": rows,
            "counts": {"重复组": len(q["duplicates"]), "疑似同义": len(q.get("suspects") or []),
                       "可能矛盾": len(q["conflicts"]), "长期未更新": stale,
                       "过短": len(q["tiny"]), "过粗粒度": len(q["coarse"]),
                       "缺项目": len(q["no_project"]), "缺标签": len(q["no_tags"])},
            "penalties": penalties,
            "grade_text": {"A": "优秀", "B": "良好", "C": "一般", "D": "需要治理"}[grade]}

def _split_chunks(text, max_len=600):
    """把长文本切成片段：优先按空行/小标题分段，再按句号/换行兜底，最后硬切"""
    import re as _re
    blocks = [b.strip() for b in _re.split(r"\n\s*\n", text) if b.strip()]
    chunks, cur = [], ""
    for b in blocks:
        if len(b) > max_len:
            # 大块再按行/句拆
            pieces = _re.split(r"(?<=\n)|(?<=。)|\.\s", b)
            for p in pieces:
                p = p.strip()
                if not p:
                    continue
                if len(p) > max_len:
                    for i in range(0, len(p), max_len):
                        seg = p[i:i + max_len].strip()
                        if seg:
                            chunks.append(seg)
                    continue
                if len(cur) + len(p) > max_len and cur:
                    chunks.append(cur.strip()); cur = p
                else:
                    cur = (cur + " " + p).strip()
        else:
            if len(cur) + len(b) > max_len and cur:
                chunks.append(cur.strip()); cur = b
            else:
                cur = (cur + "\n\n" + b).strip()
    if cur.strip():
        chunks.append(cur.strip())
    return [c for c in chunks if len(c) >= 10]

def split_memory(mid, max_len=600, keep_original=True):
    """把超长记忆拆成多条子记忆（继承项目/类型/来源），原文默认保留但标记作废"""
    conn = db()
    r = conn.execute("SELECT * FROM memories WHERE id=?", (int(mid),)).fetchone()
    conn.close()
    if not r:
        return {"error": "记忆不存在"}
    r = dict(r)
    parts = _split_chunks(r["content"], max_len)
    if len(parts) < 2:
        return {"error": "这条内容切不出多个片段（可能段落太少），可先手动整理原文"}
    ids = []
    for i, p in enumerate(parts, 1):
        title = p.strip().splitlines()[0][:24] if p.strip() else ""
        ids.append(save_memory(p, r["mtype"], r["importance"], r["tags"],
                               r["project"], r["agent"] or "split",
                               r.get("source_path") or ""))
    if keep_original:
        conn = db()
        conn.execute("UPDATE memories SET superseded_by=?, updated_at=? WHERE id=?",
                     (ids[0], now(), r["id"]))
        conn.commit()
        conn.close()
    else:
        conn = db()
        conn.execute("DELETE FROM memories WHERE id=?", (r["id"],))
        conn.commit()
        conn.close()
    return {"ok": True, "parts": len(parts), "created": ids,
            "original": r["id"], "original_kept": bool(keep_original),
            "first_title": parts[0][:40]}

def audit_report(project=None):
    """生成质检报告（Markdown），可交付/存档"""
    q = quality_scan(project)
    h = health_score(project)
    L = []
    L.append("# Hippocampus 记忆质检报告")
    L.append("")
    L.append("> 生成时间：%s ｜ 范围：%s ｜ 扫描：%d 条" %
             (now(), project or "全局", q["scanned"]))
    L.append("")
    L.append("## 一、健康度")
    L.append("")
    L.append("**%d / 100（%s）**" % (h["score"], h["grade_text"]))
    L.append("")
    L.append("| 扣分维度 | 扣分 |")
    L.append("|---|---|")
    for k, v in (h.get("penalties") or {}).items():
        L.append("| %s | -%.1f |" % (k, v))
    L.append("")
    L.append("## 二、各维度明细")
    L.append("")
    L.append("| 检查项 | 数量 | 处理建议 |")
    L.append("|---|---|---|")
    tips = {"重复组": "合并保留一条", "疑似同义": "人工判断是否同一件事",
            "可能矛盾": "以新代旧", "长期未更新": "续期或作废",
            "过短": "补充信息或删除", "过粗粒度": "拆分（整篇日志塞成了一条）",
            "缺项目": "补项目名，便于按项目检索", "缺标签": "补标签，提升检索命中"}
    for k, v in (h.get("counts") or {}).items():
        L.append("| %s | %d | %s |" % (k, v, tips.get(k, "")))
    L.append("")
    if q["duplicates"]:
        L.append("### 重复组")
        for i, g in enumerate(q["duplicates"], 1):
            L.append("**第 %d 组**" % i)
            for x in g:
                L.append("- `#%d` %s" % (x["id"], x["content"][:80]))
        L.append("")
    if q.get("coarse"):
        L.append("### 过粗粒度（>%d 字）" % COARSE_LEN)
        for x in q["coarse"]:
            L.append("- `#%d` %d 字 ｜ %s" % (x["id"], len(x["content"]), x["content"][:60]))
        L.append("")
    if q.get("tiny"):
        L.append("### 过短（<%d 字）" % TINY_LEN)
        for x in q["tiny"]:
            L.append("- `#%d` %s" % (x["id"], x["content"][:60]))
        L.append("")
    if q["conflicts"]:
        L.append("### 可能矛盾")
        for c in q["conflicts"]:
            L.append("- 旧 `#%d` %s" % (c["older"]["id"], c["older"]["content"][:60]))
            L.append("  新 `#%d` %s" % (c["newer"]["id"], c["newer"]["content"][:60]))
        L.append("")
    if q["stale"]:
        L.append("### 长期未更新（≥%d 天）" % q["stale_days"])
        for x in q["stale"]:
            L.append("- `#%d` %d 天 ｜ %s" % (x["id"], x["days"], x["content"][:60]))
        L.append("")
    L.append("---")
    L.append("")
    L.append("*本报告由 Hippocampus 质检模块生成，处理动作可在面板「质检」页执行。*")
    return "\n".join(L)

def scan_zcode_db(db_path=None, include_subagent=True, max_sessions=60):
    """读 ZCode（智谱 GLM）的 SQLite 会话库，返回可导入的会话列表。
    库文件可能正被 ZCode 占用 → 复制副本再读，绝不改动原库。"""
    import shutil as _sh, tempfile as _tmp, sqlite3 as _sq
    src = db_path or os.path.join(os.path.expanduser("~"), ".zcode", "cli", "db", "db.sqlite")
    if not os.path.isfile(src):
        return []
    work = os.path.join(_tmp.gettempdir(), "hippocampus-zcode-ro.sqlite")
    try:
        _sh.copy2(src, work)
    except Exception:
        return []
    out = []
    try:
        conn = _sq.connect(work)
        conn.row_factory = _sq.Row
        rows = list(conn.execute(
            "SELECT id, title, directory, time_created FROM session ORDER BY time_updated DESC LIMIT ?",
            (max_sessions,)))
        for sess in rows:
            sid = sess["id"]
            if (not include_subagent) and "subagent" in str(sid).lower():
                continue
            msgs = []
            for m in conn.execute(
                    "SELECT id, data FROM message WHERE session_id=? ORDER BY sequence", (sid,)):
                try:
                    md = json.loads(m["data"] or "{}")
                except Exception:
                    continue
                role = str(md.get("role") or "").lower()
                if role not in ("user", "assistant"):
                    continue
                texts = []
                for p in conn.execute(
                        "SELECT data FROM part WHERE message_id=? ORDER BY sequence", (m["id"],)):
                    try:
                        pd = json.loads(p["data"] or "{}")
                    except Exception:
                        continue
                    if pd.get("type") == "text" and pd.get("text"):
                        texts.append(str(pd["text"]).strip())
                body = "\n".join(t for t in texts if t).strip()
                if body:
                    msgs.append({"role": role, "content": body})
            if msgs:
                out.append({"agent": "ZCode", "session": sid,
                            "title": sess["title"] or "", "dir": sess["directory"] or "",
                            "at": sess["time_created"] or "", "messages": msgs})
        conn.close()
    except Exception:
        return out
    finally:
        try:
            os.remove(work)
        except Exception:
            pass
    return out

def _scan_generic_jsonl(agent, root, subdirs=()):
    """扫描 Claude Code / Codex 的 jsonl 会话文件"""
    home = os.path.expanduser("~")
    base = os.path.join(home, root)
    out = []
    if not os.path.isdir(base):
        return out
    for dirpath, _, files in os.walk(base):
        for fn in files:
            if not fn.endswith(".jsonl"):
                continue
            p = os.path.join(dirpath, fn)
            try:
                text = io.open(p, encoding="utf-8", errors="replace").read()
            except Exception:
                continue
            msgs = parse_transcript(text)
            real = [m for m in msgs if m.get("role") in ("user", "assistant")]
            if len(real) >= 2:
                out.append({"agent": agent, "session": fn[:-6], "title": "", "dir": dirpath,
                            "at": "", "messages": real})
    return out

def auto_scan_agents(include_subagent=True, import_new=True, extract=False,
                     max_candidates=30, zcode_db=None):
    """自动扫描本机各 Agent 的对话存储 → 去重归档 → 可选抽候选记忆"""
    found = scan_zcode_db(zcode_db, include_subagent)
    found += _scan_generic_jsonl("Claude Code", os.path.join(".claude", "projects"))
    found += _scan_generic_jsonl("Codex", os.path.join(".codex", "sessions"))
    result = {"scanned": len(found), "imported": 0, "skipped": 0,
              "sessions": [], "candidates": []}
    new_ids = []
    for item in found:
        if not import_new:
            continue
        title = item["title"] or (item["agent"] + "：" + item["messages"][0]["content"][:22])
        sid, created = save_session(title, item.get("dir") or "", item["agent"],
                                    item["messages"], source_path="agent-scan://" + item["session"])
        if created:
            result["imported"] += 1
            result["sessions"].append({"id": sid, "title": title, "agent": item["agent"],
                                       "count": len(item["messages"])})
            new_ids.append(sid)
        else:
            result["skipped"] += 1
    if extract and new_ids:
        for sid in new_ids[:8]:
            for c in extract_candidates(session_id=sid, limit=12):
                result["candidates"].append(c)
                if len(result["candidates"]) >= max_candidates:
                    break
            if len(result["candidates"]) >= max_candidates:
                break
    return result

# ---------- 会话 -> 记忆抽取 ----------
_EXTRACT_PATTERNS = (
    ("decision", re.compile(r"(决定|确定|就按|采用|方案是|最终|定为|统一为|不改了|拍板)")),
    ("error", re.compile(r"(踩坑|坑了|不行|失败|报错|错误|注意|别用|不要用|避免|禁止|不能用|白干)")),
    ("preference", re.compile(r"(我习惯|我偏好|我要|以后都|一律|必须|优先|别给我|记住)")),
    ("fact", re.compile(r"(\d+\s*(条|个|家|吨|万|元|天|小时|%|倍)|已(经)?(完成|扩|改|加|删|上线|接入))")),
)

def extract_candidates(session_id=None, project=None, limit=40):
    """从已归档会话里按规则抽取候选记忆（零依赖，不用 LLM）"""
    conn = db()
    base = ("SELECT m.*, s.project sproject, s.agent sagent, s.title stitle "
            "FROM messages m JOIN sessions s ON s.id=m.session_id")
    if session_id:
        rows = conn.execute(base + " WHERE m.session_id=? ORDER BY m.turn", (int(session_id),)).fetchall()
    else:
        args = []
        sql = base
        if project:
            sql += " WHERE s.project=?"; args.append(project)
        sql += " ORDER BY m.session_id DESC, m.turn LIMIT 3000"
        rows = conn.execute(sql, args).fetchall()
    conn.close()
    existing = [dict(r) for r in list_memories(limit=10000)]
    out = []
    for m in rows:
        for raw in re.split(r"[\n。！？!?；;]", m["content"]):
            s = raw.strip(" \u3000-*#>\t")
            if len(s) < 12 or len(s) > 120:
                continue
            mtype = None
            for t, pat in _EXTRACT_PATTERNS:
                if pat.search(s):
                    mtype = t
                    break
            if not mtype:
                continue
            if any(similarity(s, e["content"]) > 0.7 for e in existing):
                continue
            out.append({"text": s, "mtype": mtype,
                        "project": m["sproject"] or "", "agent": m["sagent"] or "",
                        "session_id": m["session_id"], "turn": m["turn"],
                        "role": m["role"], "session_title": m["stitle"],
                        "from_user": m["role"] == "user"})
    out.sort(key=lambda x: (not x["from_user"],))
    return out[:limit]

# ---------- 记忆 -> 技能（SKILL.md）----------
def skill_markdown(project, limit=200):
    rows = [dict(r) for r in list_memories(project=project, limit=limit)]
    if not rows:
        return ""
    groups = {}
    for r in rows:
        groups.setdefault(r["mtype"], []).append(r)
    name = "hippocampus-" + re.sub(r"[^\w\u4e00-\u9fff-]", "_", project)
    lines = ["---", "name: " + name,
             "description: 由 Hippocampus 记忆库生成的「%s」经验手册（%d 条）" % (project, len(rows)),
             "---", "", "# %s 经验手册" % project, "",
             "> 自动生成，跨 Agent 共享。使用前请核对时效性与项目当前状态。", ""]
    for t, title in (("decision", "已确认的决策"), ("preference", "硬性要求与偏好"),
                     ("error", "踩过的坑（务必避免）"), ("skill", "可复用经验"),
                     ("fact", "事实与状态"), ("context", "背景")):
        if t in groups:
            lines.append("## " + title)
            for r in groups[t]:
                lines.append("- " + r["content"])
            lines.append("")
    return "\n".join(lines)

def export_skill(project, out_dir=None):
    """把项目记忆导出成 SKILL.md，可直接放进各 Agent 的 skills 目录"""
    md = skill_markdown(project)
    if not md:
        return {"error": "该项目没有可导出的记忆"}
    base = out_dir or os.path.join(os.path.expanduser("~"), ".workbuddy", "skills")
    name = "hippocampus-" + re.sub(r"[^\w\u4e00-\u9fff-]", "_", project)
    d = os.path.join(base, name)
    os.makedirs(d, exist_ok=True)
    p = os.path.join(d, "SKILL.md")
    with open(p, "w", encoding="utf-8") as f:
        f.write(md)
    return {"ok": True, "path": p, "name": name, "count": md.count("\n- ")}

def handoff(project, limit=50):
    """项目交接卡：按类型分组输出 markdown，可直接贴给下一个 Agent"""
    rows = list_memories(project=project, limit=limit)
    rows = list(reversed(rows))
    if not rows:
        return f"# 项目交接卡: {project}\n\n（记忆库中没有该项目的记录）"
    groups = {}
    for r in rows:
        groups.setdefault(r["mtype"], []).append(r)
    label = {"decision": "已确认的决策", "preference": "偏好与硬性要求", "fact": "事实与状态",
             "error": "踩过的坑", "skill": "可复用经验", "context": "背景上下文", "summary": "摘要"}
    lines = [f"# 项目交接卡: {project}",
             f"> 生成时间 {now()} ｜ 共 {len(rows)} 条记忆 ｜ 来源: Hippocampus 本地记忆库", ""]
    for t in ("decision", "preference", "error", "fact", "skill", "context", "summary"):
        if t not in groups:
            continue
        lines.append(f"## {label.get(t, t)}")
        for r in groups[t]:
            star = "★" * r["importance"]
            lines.append(f"- [{star}] {r['content']}")
        lines.append("")
    lines.append("---")
    lines.append("使用说明: 以上内容来自跨 Agent 共享记忆库，请在此基础上继续工作，不要推翻已确认的决策，避免重复踩坑。")
    return "\n".join(lines)

# ---------- 对话解析：把各平台落盘的会话还原成 [{role, content}] ----------
_LABEL_RE = re.compile(
    r"^\s*(?:#{1,6}\s*|\*\*|\-\s|>\s)*"
    r"(我|用户|user|human|你|问|ai|assistant|助手|claude|gpt|chatgpt|"
    r"workbuddy|trae|zcode|copilot|gemini|codex|答)"
    r"\s*(?:\*\*)?\s*(?::|：)?\s*(.*)$", re.I)
_USER_LABELS = {"我", "用户", "user", "human", "你", "问"}

def _msg_text(content):
    """从各种 content 结构里抽出纯文本"""
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for c in content:
            if isinstance(c, str):
                parts.append(c)
            elif isinstance(c, dict):
                if c.get("text"):
                    parts.append(str(c["text"]))
                elif c.get("content"):
                    parts.append(str(c["content"]))
        return "\n".join(p for p in parts if p)
    if isinstance(content, dict):
        return str(content.get("text") or content.get("content") or "")
    return str(content)

def _record_to_msg(obj):
    """把一条落盘记录转成 {role, content}，不是消息则返回 None"""
    if not isinstance(obj, dict):
        return None
    role = obj.get("role") or obj.get("author") or obj.get("speaker") or ""
    inner = obj.get("message") if isinstance(obj.get("message"), dict) else None
    if inner:
        role = role or inner.get("role", "")
    content = obj.get("content")
    if content is None and inner:
        content = inner.get("content")
    if content is None and obj.get("text"):
        content = obj.get("text")
    text = _msg_text(content).strip()
    if not text:
        return None
    # 过滤非对话记录（摘要、工具调用等），但保留有正文的
    typ = str(obj.get("type") or obj.get("event") or "").lower()
    if not role and typ in ("summary", "system", "meta", "tool", "tool_result"):
        return None
    role = str(role).lower()
    if role not in ("user", "assistant", "system", "tool"):
        role = "assistant" if inner else (role or "unknown")
    return {"role": role, "content": text}

def _parse_structured(text):
    """尝试 JSONL（每行一条）或 JSON 数组/对象"""
    lines = [l for l in text.splitlines() if l.strip()]
    if lines and all(l.lstrip().startswith(("{", "[")) for l in lines):
        msgs = []
        for l in lines:
            try:
                o = json.loads(l)
            except Exception:
                return []
            m = _record_to_msg(o)
            if m:
                msgs.append(m)
        if msgs:
            return msgs
    try:
        o = json.loads(text)
    except Exception:
        return []
    arr = o if isinstance(o, list) else (o.get("messages") or o.get("conversation") or o.get("turns") or [])
    if not isinstance(arr, list):
        return []
    msgs = [m for m in (_record_to_msg(x) for x in arr) if m]
    return msgs

def _parse_plain(text):
    """按「我:」「AI:」等说话人前缀切分纯文本聊天记录"""
    msgs = []
    cur_role, cur_lines = None, []
    found_label = False

    def flush():
        if cur_role and cur_lines:
            body = "\n".join(cur_lines).strip()
            if body:
                msgs.append({"role": cur_role, "content": body})

    for raw in text.splitlines():
        line = raw.rstrip()
        m = _LABEL_RE.match(line)
        if m and (line.strip() != m.group(1)):
            lab = m.group(1).lower()
            role = "user" if lab in _USER_LABELS else "assistant"
            rest = m.group(2).strip()
            # 只有标签没有正文时：切换角色，正文在后续行
            if rest or cur_role != role:
                flush()
                cur_role, cur_lines = role, ([rest] if rest else [])
                found_label = True
                continue
        if cur_role:
            cur_lines.append(line)
    flush()
    if not found_label:
        return []
    return msgs

def parse_transcript(text):
    """对外入口：返回 [{role, content}]；识别不出说话人时整段作为 raw 一条"""
    text = (text or "").strip()
    if not text:
        return []
    msgs = _parse_structured(text)
    if not msgs:
        msgs = _parse_plain(text)
    if msgs:
        return msgs
    return [{"role": "raw", "content": text}]

# ---------- 会话存储 ----------
def save_session(title="", project="", agent="", messages=None, source_path="", summary="", allow_dup=False):
    """保存一次会话（含原文）。同一内容重复导入会被指纹拦截，返回 (sid, created)"""
    msgs = messages or []
    if not msgs:
        return None, False
    fp = hashlib.md5(("\n".join(f"{m.get('role','')}:{m.get('content','')}" for m in msgs)).encode("utf-8")).hexdigest()
    conn = db()
    if not allow_dup:
        row = conn.execute("SELECT id FROM sessions WHERE fingerprint=?", (fp,)).fetchone()
        if row:
            conn.close()
            return row["id"], False
    t = now()
    n = len(msgs)
    if not title:
        first = next((m["content"] for m in msgs if m.get("role") == "user"), msgs[0].get("content", ""))
        title = (first[:24] + "…") if len(first) > 24 else (first or "未命名会话")
    cur = conn.execute(
        "INSERT INTO sessions (title,project,agent,source_path,started_at,ended_at,msg_count,summary,fingerprint,created_at)"
        " VALUES (?,?,?,?,?,?,?,?,?,?)",
        (title, project, agent, source_path, msgs[0].get("at", ""), msgs[-1].get("at", ""),
         n, summary, fp, t))
    sid = cur.lastrowid
    conn.executemany(
        "INSERT INTO messages (session_id,turn,role,content,created_at) VALUES (?,?,?,?,?)",
        [(sid, i, m.get("role", "unknown"), m.get("content", ""), t) for i, m in enumerate(msgs, 1)])
    conn.commit()
    conn.close()
    return sid, True

def list_sessions(limit=50, project=None):
    conn = db()
    sql = "SELECT * FROM sessions"
    args = []
    if project:
        sql += " WHERE project=?"; args.append(project)
    sql += " ORDER BY id DESC LIMIT ?"
    args.append(limit)
    rows = conn.execute(sql, args).fetchall()
    conn.close()
    return rows

def get_session(sid):
    conn = db()
    s = conn.execute("SELECT * FROM sessions WHERE id=?", (sid,)).fetchone()
    msgs = conn.execute("SELECT * FROM messages WHERE session_id=? ORDER BY turn", (sid,)).fetchall()
    conn.close()
    return s, msgs

def delete_session(sid):
    conn = db()
    conn.execute("DELETE FROM messages WHERE session_id=?", (sid,))
    conn.execute("DELETE FROM sessions WHERE id=?", (sid,))
    conn.commit()
    conn.close()

def search_messages(query, limit=10, project=None):
    """在会话原文里检索，返回 [(score, message_row, session_row)]"""
    conn = db()
    sql = ("SELECT m.*, s.title stitle, s.project sproject, s.agent sagent, s.created_at screated "
           "FROM messages m JOIN sessions s ON s.id=m.session_id")
    args = []
    if project:
        sql += " WHERE s.project=?"; args.append(project)
    rows = conn.execute(sql, args).fetchall()
    conn.close()
    if not rows:
        return []
    q_toks = tokenize(query)
    if not q_toks:
        return []
    docs = [(r, tokenize(r["content"])) for r in rows]
    df = {}
    for _, toks in docs:
        for t in set(toks):
            df[t] = df.get(t, 0) + 1
    n = len(docs)
    idf = {t: math.log((n + 1) / (c + 0.5)) + 1 for t, c in df.items()}
    out = []
    for r, toks in docs:
        if not toks:
            continue
        tf = {}
        for t in toks:
            tf[t] = tf.get(t, 0) + 1
        score = 0.0
        for t in q_toks:
            if t in tf:
                score += idf.get(t, 1.0) * (tf[t] / len(toks))
        if score <= 0:
            continue
        out.append((score / math.sqrt(len(toks)), r))
    out.sort(key=lambda x: x[0], reverse=True)
    return out[:limit]

def session_stats():
    conn = db()
    s = conn.execute("SELECT COUNT(*) c, COALESCE(SUM(msg_count),0) n FROM sessions").fetchone()
    conn.close()
    return {"sessions": s["c"], "messages": s["n"]}

# ---------- MCP 工具定义 ----------
TOOLS = [
    {"name": "memory_save", "description": "保存一条记忆到本地共享记忆库（跨 Agent 可见）",
     "inputSchema": {"type": "object", "properties": {
         "content": {"type": "string", "description": "记忆内容（中文友好）"},
         "type": {"type": "string", "enum": list(TYPES), "description": "记忆类型"},
         "importance": {"type": "integer", "description": "重要度1-4"},
         "tags": {"type": "string", "description": "逗号分隔标签"},
         "project": {"type": "string", "description": "所属项目名"},
         "agent": {"type": "string", "description": "写入方 Agent 名"}},
        "required": ["content"]}},
    {"name": "memory_search", "description": "语义检索本地记忆库（中文 bigram + 英文混合检索）",
     "inputSchema": {"type": "object", "properties": {
         "query": {"type": "string", "description": "查询（支持中文）"},
         "limit": {"type": "integer", "description": "返回条数，默认5"},
         "type": {"type": "string", "description": "按类型过滤"},
         "project": {"type": "string", "description": "按项目过滤"}},
        "required": ["query"]}},
    {"name": "memory_list", "description": "列出最近的记忆",
     "inputSchema": {"type": "object", "properties": {
         "project": {"type": "string"}, "type": {"type": "string"},
         "limit": {"type": "integer", "description": "默认20"}}}},
    {"name": "memory_delete", "description": "删除一条记忆（软删除）",
     "inputSchema": {"type": "object", "properties": {
         "id": {"type": "integer", "description": "记忆ID"}}, "required": ["id"]}},
    {"name": "memory_stats", "description": "记忆库统计信息（总数/按类型/按项目/按来源Agent）",
     "inputSchema": {"type": "object", "properties": {}}},
    {"name": "memory_handoff", "description": "生成项目交接卡（markdown），切换到另一个 Agent 时注入上下文用",
     "inputSchema": {"type": "object", "properties": {
         "project": {"type": "string", "description": "项目名"}},
        "required": ["project"]}},
    {"name": "session_save", "description": "归档一次对话原文（会话层）。把当前对话保存进本地库，其他 Agent 之后可检索复现",
     "inputSchema": {"type": "object", "properties": {
         "transcript": {"type": "string", "description": "整段对话文本（或 JSONL/JSON 数组）"},
         "title": {"type": "string", "description": "会话标题（留空按首句自动生成）"},
         "project": {"type": "string", "description": "所属项目"},
         "agent": {"type": "string", "description": "来源 Agent 名"},
         "summary": {"type": "string", "description": "一句话摘要（可选）"}},
        "required": ["transcript"]}},
    {"name": "session_recall", "description": "检索历史对话原文（会话层），返回当时的原话片段——用于在新 Agent 里复现旧对话上下文",
     "inputSchema": {"type": "object", "properties": {
         "query": {"type": "string", "description": "查询（支持中文）"},
         "limit": {"type": "integer", "description": "返回条数，默认5"},
         "project": {"type": "string", "description": "按项目过滤"}},
        "required": ["query"]}},
    {"name": "memory_context", "description": "获取常驻上下文包（建议每次对话开始时调用）：常驻记忆 + 近期重点记忆，一次拿齐跨 Agent 共享的上下文",
     "inputSchema": {"type": "object", "properties": {
         "project": {"type": "string", "description": "项目名（可选，留空返回全局）"},
         "limit": {"type": "integer", "description": "近期重点条数，默认8"}}}},
    {"name": "memory_pin", "description": "把一条记忆设为常驻（始终随 memory_context 返回）或取消常驻",
     "inputSchema": {"type": "object", "properties": {
         "id": {"type": "integer", "description": "记忆ID"},
         "pinned": {"type": "integer", "description": "1=常驻，0=取消，默认1"}},
        "required": ["id"]}},
]

def call_tool(name, args):
    if name == "memory_save":
        mid = save_memory(args.get("content", ""), args.get("type", "fact"),
                          args.get("importance", 2), args.get("tags", ""),
                          args.get("project", ""),
                          args.get("agent") or DEFAULT_AGENT)
        return f"已保存记忆 #{mid}"
    if name == "memory_search":
        rs = search_memory(args.get("query", ""), args.get("limit", 5),
                           args.get("type"), args.get("project"))
        if not rs:
            return "没有找到相关记忆"
        out = []
        for s, r in rs:
            meta = []
            if r["project"]: meta.append("项目:" + r["project"])
            if r["mtype"]: meta.append(r["mtype"])
            if r["agent"]: meta.append("来自:" + r["agent"])
            out.append(f"[{s:.3f}] (#{r['id']} {'|'.join(meta)}) {r['content']}")
        return "\n".join(out)
    if name == "memory_list":
        rows = list_memories(args.get("project"), args.get("type"), args.get("limit", 20))
        if not rows:
            return "记忆库为空"
        return "\n".join(f"#{r['id']} [{r['mtype']}|★{r['importance']}] {r['content'][:80]}" for r in rows)
    if name == "memory_delete":
        delete_memory(int(args["id"]))
        return f"已删除记忆 #{args['id']}"
    if name == "memory_stats":
        s = stats()
        return json.dumps(s, ensure_ascii=False, indent=2)
    if name == "memory_handoff":
        return handoff(args.get("project", ""))
    if name == "session_save":
        msgs = parse_transcript(args.get("transcript", ""))
        if not msgs:
            return "没有解析出任何对话内容"
        sid, created = save_session(args.get("title", ""), args.get("project", ""),
                                    args.get("agent", ""), msgs,
                                    summary=args.get("summary", ""),
                                    allow_dup=bool(args.get("allow_dup")))
        if sid and not created:
            return f"这段对话已归档过（会话 #{sid}），未重复写入"
        return f"已归档会话 #{sid}｜{len(msgs)} 轮｜标题: {args.get('title') or '自动'}"
    if name == "session_recall":
        rs = search_messages(args.get("query", ""), args.get("limit", 5), args.get("project"))
        if not rs:
            return "历史对话里没有找到相关片段"
        out = []
        for s, r in rs:
            body = r["content"].replace("\n", " ")
            if len(body) > 200:
                body = body[:200] + "…"
            who = "我" if r["role"] == "user" else ("AI" if r["role"] == "assistant" else r["role"])
            out.append(f"[{s:.3f}] (#{r['session_id']} {r['stitle']}｜第{r['turn']}轮｜{who}) {body}")
        return "\n".join(out)
    if name == "memory_context":
        return context_pack(args.get("project") or None, args.get("limit", 8))
    if name == "memory_pin":
        set_pinned(int(args["id"]), int(args.get("pinned", 1)))
        return ("已设为常驻" if int(args.get("pinned", 1)) else "已取消常驻") + f"：记忆 #{args['id']}"
    return f"未知工具: {name}"

# ---------- 归档快照（长期备份）----------
# 设计原则（应要求）：① 默认不启用、不占系统盘；② 只复制不移动不删除；
# ③ 用 SQLite 在线备份 API，WAL 下也不会拷到半截
ARCHIVE_CFG = os.path.join(os.path.dirname(os.path.abspath(__file__)), "archive.json")


def load_archive_cfg():
    cfg = {"dir": "", "keep": 30, "auto": True}
    try:
        if os.path.isfile(ARCHIVE_CFG):
            d = json.loads(io.open(ARCHIVE_CFG, encoding="utf-8").read() or "{}")
            if isinstance(d, dict):
                cfg.update(d)
    except Exception:
        pass
    return cfg


def save_archive_cfg(cfg):
    try:
        io.open(ARCHIVE_CFG, "w", encoding="utf-8").write(
            json.dumps(cfg, ensure_ascii=False, indent=2))
        return True
    except Exception:
        return False


def list_snapshots(d=""):
    d = (d or load_archive_cfg().get("dir") or "").strip()
    out = []
    snap = os.path.join(d, "snapshots") if d else ""
    if snap and os.path.isdir(snap):
        for fn in sorted(os.listdir(snap)):
            if fn.endswith(".db"):
                p = os.path.join(snap, fn)
                try:
                    st = os.stat(p)
                except Exception:
                    continue
                out.append({"name": fn, "path": p, "size": st.st_size, "ts": st.st_mtime,
                            "mtime": datetime.datetime.fromtimestamp(st.st_mtime).strftime("%Y-%m-%d %H:%M")})
    out.sort(key=lambda x: x["ts"], reverse=True)   # 按修改时间倒序，最新在最前
    return out


def do_archive_snapshot(force=False):
    """做一份快照。不移动、不删改任何记忆，只是复制当前库。"""
    cfg = load_archive_cfg()
    d = (cfg.get("dir") or "").strip()
    if not d:
        return {"error": "尚未设置归档目录。为避免占用系统盘，默认不自动备份 —— "
                         "请先在面板「清理 → 备份与归档」里选择存放位置。"}
    try:
        snap = os.path.join(d, "snapshots")
        os.makedirs(snap, exist_ok=True)
    except Exception as e:
        return {"error": "归档目录不可用：%s" % e}
    today = datetime.date.today().strftime("%Y%m%d")
    target = os.path.join(snap, "hippocampus-%s.db" % today)
    if os.path.exists(target) and not force:
        return {"ok": True, "skipped": True, "path": target, "msg": "今天已有快照，跳过"}
    try:
        src = sqlite3.connect(DB_PATH, timeout=10)
        dst = sqlite3.connect(target, timeout=10)
        with dst:
            src.backup(dst)
        dst.close()
        src.close()
    except Exception as e:
        return {"error": "备份失败：%s" % e}
    keep = int(cfg.get("keep") or 30)
    removed = []
    # 永不删除刚生成的这份；其余按修改时间从旧到新清理
    snaps = [x for x in list_snapshots(d) if os.path.abspath(x["path"]) != os.path.abspath(target)]
    for it in snaps[max(0, keep - 1):]:
        try:
            os.remove(it["path"])
            removed.append(it["name"])
        except Exception:
            pass
    _sz = os.path.getsize(target) if os.path.exists(target) else 0
    return {"ok": True, "path": target, "size": _sz,
            "count": len(list_snapshots(d)), "removed": removed,
            "note": "快照是复制出来的副本，原有记忆未被移动或删除"}


# ---------- MCP stdio 协议 (JSON-RPC 2.0, 换行分隔) ----------
def mcp_server():
    stdin = io.TextIOWrapper(sys.stdin.buffer, encoding="utf-8")
    stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", line_buffering=True)

    def reply(rid, result=None, error=None):
        msg = {"jsonrpc": "2.0", "id": rid}
        if error is not None:
            msg["error"] = error
        else:
            msg["result"] = result
        stdout.write(json.dumps(msg, ensure_ascii=False) + "\n")
        stdout.flush()

    for line in stdin:
        line = line.strip()
        if not line:
            continue
        try:
            req = json.loads(line)
        except json.JSONDecodeError:
            continue
        method = req.get("method", "")
        rid = req.get("id")
        if method == "initialize":
            ver = req.get("params", {}).get("protocolVersion", "2024-11-05")
            reply(rid, {
                "protocolVersion": ver,
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "hippocampus", "version": "0.1.0"}})
        elif method == "notifications/initialized":
            pass
        elif method == "tools/list":
            reply(rid, {"tools": TOOLS})
        elif method == "tools/call":
            p = req.get("params", {})
            try:
                text = call_tool(p.get("name", ""), p.get("arguments", {}) or {})
                reply(rid, {"content": [{"type": "text", "text": text}], "isError": False})
            except Exception as e:
                reply(rid, {"content": [{"type": "text", "text": f"执行出错: {e}"}], "isError": True})
        elif method == "ping":
            reply(rid, {})
        elif rid is not None:
            reply(rid, error={"code": -32601, "message": f"method not found: {method}"})

# ---------- CLI ----------
def cli():
    print("Hippocampus CLI（输入 help 查看命令, quit 退出）")
    while True:
        try:
            line = input("hippocampus> ").strip()
        except (EOFError, KeyboardInterrupt):
            break
        if not line or line == "quit":
            break
        parts = line.split(" ", 1)
        cmd, rest = parts[0], (parts[1] if len(parts) > 1 else "")
        if cmd == "help":
            print("save <内容> | search <查询> | list | stats | handoff <项目> | sessions | recall <查询> | context [项目] | pin <id> | unpin <id> | del <id> | quit")
        elif cmd == "sessions":
            rows = list_sessions(20)
            if not rows:
                print("还没有归档的会话")
            for r in rows:
                print(f"#{r['id']} [{r['agent']}|{r['project']}] {r['title']}  ({r['msg_count']}轮, {r['created_at']})")
        elif cmd == "recall":
            print(call_tool("session_recall", {"query": rest}))
        elif cmd == "context":
            print(call_tool("memory_context", {"project": rest}))
        elif cmd == "pin":
            print(call_tool("memory_pin", {"id": int(rest), "pinned": 1}))
        elif cmd == "unpin":
            print(call_tool("memory_pin", {"id": int(rest), "pinned": 0}))
        elif cmd == "save":
            print(call_tool("memory_save", {"content": rest, "agent": "cli"}))
        elif cmd == "search":
            print(call_tool("memory_search", {"query": rest}))
        elif cmd == "list":
            print(call_tool("memory_list", {}))
        elif cmd == "stats":
            print(call_tool("memory_stats", {}))
        elif cmd == "handoff":
            print(call_tool("memory_handoff", {"project": rest}))
        elif cmd == "del":
            print(call_tool("memory_delete", {"id": int(rest)}))
        else:
            print("未知命令，输入 help")

def main():
    ap = argparse.ArgumentParser(description="Hippocampus 个人跨 Agent 记忆中枢")
    ap.add_argument("--cli", action="store_true")
    ap.add_argument("--save")
    ap.add_argument("--search")
    ap.add_argument("--type", default="fact")
    ap.add_argument("--imp", type=int, default=2)
    ap.add_argument("--tags", default="")
    ap.add_argument("--proj", default="")
    ap.add_argument("--agent", default="cli")
    ap.add_argument("--stats", action="store_true")
    a = ap.parse_args()
    if a.save:
        print(call_tool("memory_save", {"content": a.save, "type": a.type, "importance": a.imp,
                                        "tags": a.tags, "project": a.proj, "agent": a.agent}))
    elif a.search:
        print(call_tool("memory_search", {"query": a.search}))
    elif a.stats:
        print(call_tool("memory_stats", {}))
    elif a.cli:
        cli()
    else:
        mcp_server()

if __name__ == "__main__":
    main()
