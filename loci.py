#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Loci（忆宫）— 个人跨 Agent 记忆中枢（零依赖单文件）
================================================
- 存储: SQLite 单文件, 零运维
- 检索: 中文 bigram + 英文 token 混合 TF-IDF (专为中文优化)
- 接入: MCP stdio (JSON-RPC 2.0) — 任何支持 MCP 的 Agent 都能接
- 同时提供 CLI 模式, 方便 bat 快捷方式和手动使用

用法:
  python loci.py                 # MCP server 模式 (stdio)
  python loci.py --cli           # 交互式 CLI
  python loci.py --save "内容" --type decision --imp 4 --tags a,b --proj 项目名
  python loci.py --search "查询"
"""
import sys, os, io, json, math, re, sqlite3, argparse, datetime, hashlib, collections

# ⚠️ 全项目**唯一**的版本号来源（2026-09-25 统一）。
#    以前是 3 处字面量各写各的：MCP serverInfo「0.2.0」、面板页脚「v0.2.0」、
#    面板自检 clientInfo「0.2」—— 发到 v0.3 之后这三处还都停在 0.2，
#    用户在界面上看到的版本号是错的。现在只在这里定义：
#      · 本文件 MCP 握手用它
#      · panel.py 页脚用它（serve 时替换 __APP_VERSION__ 占位符）
#      · panel.py 自检 clientInfo 用它
#    发版时**只改这一行**。
APP_VERSION = "0.4.1"

# 环境变量：优先新名 LOCI_*，**同时兼容全部历史名**。
# 老配置（4 个 Agent 的 MCP 配置里）可能还写着 HIPPOCAMPUS_DB / HIPPOHUB_DB，
# 少认一个，那个 Agent 就会连到另一个（空的）库上 —— 表现是"记忆突然全没了"。
_ENV_DB = (os.environ.get("LOCI_DB") or os.environ.get("HIPPOCAMPUS_DB")
           or os.environ.get("HIPPOHUB_DB"))
# 来源标记：各 Agent 的 MCP 配置里设置 LOCI_AGENT，写入记忆时自动带上，
# 避免调用方自己猜导致来源失真（曾出现 Trae 写入却被记成 WorkBuddy）
DEFAULT_AGENT = os.environ.get("LOCI_AGENT") or os.environ.get("HIPPOCAMPUS_AGENT") or ""
# 数据库默认位置：新名 loci.db；但**如果新文件还不存在、旧的 hippocampus.db 在**，
# 就先接着用旧的 —— 改名当天万一复制失败或被漏掉，也不至于让用户看到"空记忆库"。
_DIR = os.path.dirname(os.path.abspath(__file__))
_NEW_DB, _OLD_DB = os.path.join(_DIR, "loci.db"), os.path.join(_DIR, "hippocampus.db")
DB_PATH = _ENV_DB or (_OLD_DB if (not os.path.exists(_NEW_DB) and os.path.exists(_OLD_DB)) else _NEW_DB)
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
  deleted INTEGER NOT NULL DEFAULT 0,
  -- 打通会话层：这条记忆是从哪段会话的第几轮产出的（0 = 无来源）
  session_id INTEGER NOT NULL DEFAULT 0,
  turn INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_mem_project ON memories(project);
CREATE INDEX IF NOT EXISTS idx_mem_type ON memories(mtype);
-- 注意：session_id 的索引**不能**写在这里。老库没有这一列时，下面的轻量迁移
-- 才补列，而 executescript 是一次跑完的 —— 先建索引会直接抛错，整个 db() 挂掉。
-- 它的创建语句在 db() 里、补列之后。踩过的坑，别再挪回来。

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
    except Exception as e:
        # 容错：进不去 WAL 也能用（退回默认 journal 模式），但不能无声 ——
        # 多 Agent 并发写时会退化成偶发 "database is locked"，用户只会觉得"偶尔卡"。
        print("[loci] 无法开启 WAL/busy_timeout(%s)；并发写入下可能偶发 database is locked" % e,
              file=sys.stderr)
    conn.executescript(SCHEMA)
    # 轻量迁移：老库自动补列，不会丢数据
    for tbl, col, decl in (("memories", "pinned", "INTEGER NOT NULL DEFAULT 0"),
                           ("memories", "superseded_by", "INTEGER NOT NULL DEFAULT 0"),
                           ("memories", "source_path", "TEXT DEFAULT ''"),
                           # 打通会话层用：记忆记住自己的出处（0 = 无来源）
                           ("memories", "session_id", "INTEGER NOT NULL DEFAULT 0"),
                           ("memories", "turn", "INTEGER NOT NULL DEFAULT 0")):
        cols = [r[1] for r in conn.execute('PRAGMA table_info("%s")' % tbl)]
        if col not in cols:
            conn.execute('ALTER TABLE "%s" ADD COLUMN %s %s' % (tbl, col, decl))
    # 必须在补列之后建（见 SCHEMA 里的说明）
    conn.execute("CREATE INDEX IF NOT EXISTS idx_mem_session ON memories(session_id)")
    # 同源唯一（2026-09-22）：source_path 非空时不许重复 —— 同源 = 同一段会话。
    #   配合 save_session 的同源幂等，双保险：即使代码路径有漏，DB 层也拦住重复。
    #   用 try 包住：老库可能已堆了历史重复，建索引会直接失败；那种情况先跳过，
    #   等合并脚本清干净后，随下次启动自动建上。
    try:
        conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_sess_src "
                     "ON sessions(source_path) WHERE source_path <> ''")
    except Exception:
        pass      # 容错：老库可能已堆历史重复，建唯一索引会失败；见上两行注释的"先跳过"策略
    # 增量扫描台账：记下每个来源文件的 size+mtime。文件没变就不重复解析 ——
    # 本机 WorkBuddy 的会话 jsonl 有 47MB，全量解析要 38 秒，而它大部分时候没变。
    conn.execute("""CREATE TABLE IF NOT EXISTS scan_files(
        path TEXT PRIMARY KEY, size INTEGER NOT NULL DEFAULT 0, mtime REAL NOT NULL DEFAULT 0,
        sessions INTEGER NOT NULL DEFAULT 0, scanned_at TEXT DEFAULT '')""")
    conn.commit()
    return conn

def now():
    return datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def norm_time(v):
    """把各种来源的时间戳统一成 "YYYY-MM-DD HH:MM:SS"；认不出来就返回 ""（不猜）。

    为什么需要它：不同 Agent 的时间格式各不相同 ——
      · ZCode：epoch **毫秒**整数（1789911190551），存在 session.time_created / message.time_created
      · 有的写 ISO 串（2026-09-20T21:33:10Z / 带时区偏移）
      · 有的直接就是 "YYYY-MM-DD HH:MM:SS"
    以前这几个值一路透传到前端当字符串用，结果是"会话时间"显示成导入时间
    （2026-09-21 发现：全表 10 个会话 started_at 都是空，UI 只能退回 created_at = 扫描时间）。

    判据：>= 1e11 当毫秒、>= 1e9 当秒（1e11 ms ≈ 1973 年，1e9 s ≈ 2001 年，
    两边都远早于本项目任何真实数据，不会误判）。负数 / 0 / 无法解析 → ""。
    """
    if v is None:
        return ""
    if isinstance(v, datetime.datetime):
        return v.strftime("%Y-%m-%d %H:%M:%S")
    s = str(v).strip()
    if not s:
        return ""
    # 纯数字 → epoch（毫秒或秒）
    if re.fullmatch(r"\d+(\.\d+)?", s):
        try:
            n = float(s)
        except ValueError:
            return ""
        if n <= 0:
            return ""
        if n >= 1e11:       # 毫秒
            n /= 1000.0
        elif n < 1e9:       # 太小，不像是真实时间（避免把 0.5 之类当时间）
            return ""
        try:
            return datetime.datetime.fromtimestamp(n).strftime("%Y-%m-%d %H:%M:%S")
        except (ValueError, OSError, OverflowError):
            return ""
    # ISO 串：统一成 "YYYY-MM-DD HH:MM:SS"
    m = re.match(r"(\d{4})-(\d{2})-(\d{2})[ T](\d{2}):(\d{2}):(\d{2})", s)
    if m:
        return "%s-%s-%s %s:%s:%s" % m.groups()
    # 只给到日期
    m = re.match(r"(\d{4})-(\d{2})-(\d{2})$", s)
    if m:
        return "%s-%s-%s 00:00:00" % m.groups()
    return ""

def save_memory(content, mtype="fact", importance=2, tags="", project="", agent="", source_path="",
                session_id=0, turn=0):
    """写一条记忆。

    session_id / turn：这条记忆是从哪段会话的第几轮产出的。
      0 = 无来源 —— Agent 直接手写的（走 MCP）、采集器扫来的、迁移前的老数据。
      有来源的记忆能在面板里溯源回原话；没来源的只在检索视图里出现。
    """
    if mtype not in TYPES:
        mtype = "fact"
    importance = max(1, min(4, int(importance)))
    try:
        session_id = int(session_id or 0)
    except Exception:
        session_id = 0
    try:
        turn = int(turn or 0)
    except Exception:
        turn = 0
    conn = db()
    t = now()
    cur = conn.execute(
        "INSERT INTO memories (content, mtype, importance, tags, project, agent, source_path, session_id, turn, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        (content, mtype, importance, tags, project, agent, source_path, session_id, turn, t, t))
    conn.commit()
    mid = cur.lastrowid
    conn.close()
    return mid

def memories_of_session(session_id):
    """某段会话产出的记忆（按轮次排）—— 会话详情右栏用。"""
    conn = db()
    rows = conn.execute(
        "SELECT * FROM memories WHERE session_id=? AND deleted=0 ORDER BY turn, id",
        (int(session_id),)).fetchall()
    conn.close()
    return rows

def with_session_title(rows):
    """给记忆行补一个 session_title：它出处会话的**标题**。

    面板上要给人类看「这段结论出自哪次对话」，标题能读，`#12` 读不了 ——
    行号只配待在 tooltip 和详情里。无来源的（session_id=0）留空字符串。
    """
    out = [dict(r) for r in rows]
    ids = sorted({r["session_id"] for r in out if r.get("session_id")})
    if not ids:
        return out
    conn = db()
    m = {r["id"]: r["title"] for r in conn.execute(
        "SELECT id, title FROM sessions WHERE id IN (%s)" % ",".join("?" * len(ids)), ids)}
    conn.close()
    for r in out:
        r["session_title"] = m.get(r.get("session_id") or 0) or ""
    return out

# ---------- 检索预计算：bigram 分词结果的进程内缓存 ----------
# ⚠️ 2026-09-28 修（外部审查 P0）：search_memory / search_messages 原来**每次查询都
# 把整张表重新分词**（`[(r, tokenize(r["content"])) for r in rows]`）。实测：
#   104 条记忆 → 370~463 ms/次  ｜  3157 条消息 → 2.4 s/次
# 分词结果只跟文本有关，所以按**文本本身**做键，缓存「词频表 + 词数」——
# 内容改了文本就变，键自然失效，不会读到旧结果。
#
# ⚠️ 2026-09-28 修（外部审查 P1·内存上界）：原来按**条目数**限 8000 且到顶 clear() 全清。
#   两个问题：
#     ① 条目数不是内存的正确度量 —— 库里有 5048 字的长记忆，一条切出 ~5000 个 bigram，
#        单条缓存 ≈0.9MB（词频 dict + bigram 字符串）；8000 条这种 = 7GB 级，会 OOM。
#     ② clear() 全清 → 下次查询全 miss 重新分词，出现周期性卡顿。
#   改成按**总字符数**限量 + OrderedDict 逐条 LRU 淘汰：内存上界可算（≈ 字符总量 × 0.3），
#   淘汰也平滑（只丢最久没用的，不会一次性回冷）。
_TOKS_CACHE = collections.OrderedDict()
_TOKS_CHARS = 0
_TOKS_CHARS_CAP = 1000000   # 缓存的原始文本总量上限（≈150~300MB 级）


def _toks_tf(text):
    """返回 (词频 dict, 词数)。命中缓存则直接复用 —— 省掉逐字符 bigram 切分。"""
    global _TOKS_CHARS
    text = text or ""
    t = _TOKS_CACHE.get(text)
    if t is not None:
        _TOKS_CACHE.move_to_end(text)        # LRU：命中的挪到最新
        return t
    toks = tokenize(text)
    tf = {}
    for x in toks:
        tf[x] = tf.get(x, 0) + 1
    val = (tf, len(toks))
    _TOKS_CACHE[text] = val
    _TOKS_CHARS += len(text)
    # 超上限就淘汰最久未用的，直到回到上限内。`len > 1` 是边界保护：单条就超过上限的
    # 超长文本会独占缓存（退化成"只缓存这一条"）——行为正确，能走到这里的文本本就远超单条容量。
    while _TOKS_CHARS > _TOKS_CHARS_CAP and len(_TOKS_CACHE) > 1:
        k, _v = _TOKS_CACHE.popitem(last=False)
        _TOKS_CHARS -= len(k)
    return val
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
    # 文档集 idf / tf：都走 _toks_tf 的缓存（原来这里每次把 104 条记忆重新分词）
    docs = [(r, _toks_tf(r["content"])) for r in rows]
    df = {}
    for _, (tf, _tn) in docs:
        for t in tf:
            df[t] = df.get(t, 0) + 1
    n = len(docs)
    idf = {t: math.log((n + 1) / (c + 0.5)) + 1 for t, c in df.items()}
    results = []
    today = datetime.date.today()
    qset = set(q_toks)
    for r, (tf, tn) in docs:
        if not tn:
            continue
        score = 0.0
        for t in q_toks:
            c = tf.get(t)
            if c:
                score += idf.get(t, 1.0) * (c / tn)
        if score <= 0:
            continue
        score = score / math.sqrt(tn)
        score *= 1.0 + 0.15 * (r["importance"] - 1)
        # 零依赖加权：常驻 / 项目命中 / 标签命中（不加外部模型）
        if r["pinned"]:
            score *= 1.35
        if project and r["project"] == project:
            score *= 1.25
        if r["project"] and any(t in qset for t in _toks_tf(r["project"])[0]):
            score *= 1.30
        if r["tags"] and any(t in qset for t in _toks_tf(r["tags"])[0]):
            score *= 1.20
        try:
            days = (today - datetime.datetime.strptime(r["created_at"][:10], "%Y-%m-%d").date()).days
            score *= 1.0 + 0.02 * max(0, 30 - days)
        except Exception:
            pass      # 容错：created_at 格式异常就不加时效分，不影响命中集合与排序正确性
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
    head = "# Loci 常驻上下文" + (("（项目：" + project + "）") if project else "")
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

    # 每条记忆的 bigram 集合只算一次。原来在每个配对上重新 tokenize 两个字符串，
    # n 条要算 4*C(n,2) 次 tokenize（n=40 时是 3120 次）；预计算后降到 n 次。
    # jac 与 con 共用同一次集合交集，结果与 similarity()/containment() 逐位等价 ——
    # 由 tools/verify_audit_perf.py 对真实库比对校验。
    toks = [set(tokenize(r["content"] or "")) for r in rows]
    tsize = [len(t) for t in toks]
    projs = [(r["project"] or "") for r in rows]
    mtype = [r["mtype"] for r in rows]
    chg = [any(w in (r["content"] or "") for w in CHANGE_WORDS) for r in rows]

    conflicts = []
    suspects = []
    for i in range(n):
        sa, la = toks[i], tsize[i]
        if not la:
            continue
        for j in range(i + 1, n):
            sb, lb = toks[j], tsize[j]
            if not lb:
                continue
            inter = len(sa & sb)
            if inter:
                jac = inter / (la + lb - inter)
                con = inter / (la if la < lb else lb)
            else:
                jac = con = 0.0
            if jac >= 0.50 or con >= 0.70:
                union(i, j)
            else:
                same_proj = projs[i] == projs[j]
                if same_proj and chg[i] != chg[j] and jac >= 0.25:
                    conflicts.append({"older": rows[i] if rows[i]["id"] < rows[j]["id"] else rows[j],
                                      "newer": rows[j] if rows[i]["id"] < rows[j]["id"] else rows[i],
                                      "similarity": round(max(jac, con * 0.9), 3)})
                elif (same_proj and mtype[i] == mtype[j] and jac >= 0.25):
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
            continue      # 容错：updated_at 格式异常 → 本就算不出时效，不参与"长期未更新"判定
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
    L.append("# Loci 记忆质检报告")
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
    L.append("*本报告由 Loci 质检模块生成，处理动作可在面板「质检」页执行。*")
    return "\n".join(L)

def scan_zcode_db(db_path=None, include_subagent=True, max_sessions=60,
                  known=None, seen=None, force=False, stats=None):
    """读 ZCode（智谱 GLM）的 SQLite 会话库，返回可导入的会话列表。
    库文件可能正被 ZCode 占用 → 复制副本再读，绝不改动原库。"""
    import shutil as _sh, tempfile as _tmp, sqlite3 as _sq
    src = db_path or os.path.join(os.path.expanduser("~"), ".zcode", "cli", "db", "db.sqlite")
    if not os.path.isfile(src):
        return []
    sig, key = _file_sig(src), _norm_key(src)
    if seen is not None and sig is not None:
        seen[key] = sig
    st = stats if stats is not None else {}
    st["files"] = st.get("files", 0) + 1
    if not force and sig is not None and (known or {}).get(key) == sig:
        st["unchanged"] = st.get("unchanged", 0) + 1
        return []
    work = os.path.join(_tmp.gettempdir(), "loci-zcode-ro.sqlite")
    try:
        _sh.copy2(src, work)
    except Exception:
        return []
    out = []
    try:
        conn = _sq.connect(work)
        conn.row_factory = _sq.Row
        rows = list(conn.execute(
            "SELECT id, title, directory, time_created, time_updated FROM session ORDER BY time_updated DESC LIMIT ?",
            (max_sessions,)))
        for sess in rows:
            sid = sess["id"]
            if (not include_subagent) and "subagent" in str(sid).lower():
                continue
            msgs = []
            for m in conn.execute(
                    "SELECT id, data, time_created FROM message WHERE session_id=? ORDER BY sequence", (sid,)):
                try:
                    md = json.loads(m["data"] or "{}")
                except Exception:
                    continue      # 容错：单条 message 的 data 坏了就跳过它，不拖垮整次会话扫描
                role = str(md.get("role") or "").lower()
                if role not in ("user", "assistant"):
                    continue
                texts = []
                for p in conn.execute(
                        "SELECT data FROM part WHERE message_id=? ORDER BY sequence", (m["id"],)):
                    try:
                        pd = json.loads(p["data"] or "{}")
                    except Exception:
                        continue      # 容错：单个 part 的 data 坏了就跳过，同上（坏行不拖垮整次质检）
                    if pd.get("type") == "text" and pd.get("text"):
                        texts.append(str(pd["text"]).strip())
                body = "\n".join(t for t in texts if t).strip()
                if body:
                    # 每条消息带自己的时间（ZCode 存的是 epoch 毫秒）
                    msgs.append({"role": role, "content": body,
                                 "at": norm_time(m["time_created"])})
            if msgs:
                # 会话级时间也带出来：started_at 取 time_created、ended_at 取 time_updated。
                # 消息时间缺失时，这两个还能兜住会话本身的先后顺序。
                out.append({"agent": "ZCode", "session": sid,
                            "title": sess["title"] or "", "dir": sess["directory"] or "",
                            "at": norm_time(sess["time_created"]),
                            "end": norm_time(sess["time_updated"]),
                            "messages": msgs})
        conn.close()
    except Exception:
        return out      # 静默部分结果：中途失败返回已解析的会话（宁可少不可断）——见上方逐行 continue 的容错
    finally:
        try:
            os.remove(work)
        except Exception:
            pass      # 容错：临时文件删不掉不影响扫描结果（同名会被下次覆盖）
    st["sessions"] = st.get("sessions", 0) + len(out)
    st.setdefault("file_sessions", {})[key] = len(out)
    return out

# 落盘记录里这些 type 不是"对话正文"：推理块、工具调用、快照、标题行……
# 2026-09-21 统计 WorkBuddy 的 9389 行：message 1377 / reasoning 1649 /
# function_call 2618 / function_call_result 2609 / file-history-snapshot 1120。
# 不过滤的话，会话里 85% 是工具噪音，"我问了什么、AI 答了什么"会被埋掉。
_NON_DIALOG_TYPES = {
    "reasoning", "thinking", "function_call", "function_call_result", "tool_call",
    "tool_result", "tool", "meta", "system", "snapshot", "file-history-snapshot",
    "file-history", "ai-title", "summary", "resend-fork-notice", "compact",
}

def _norm_key(p):
    """路径归一化成台账主键（Windows 下大小写不敏感）"""
    return os.path.normcase(os.path.abspath(p))

def _file_sig(p):
    """文件的 (size, mtime) 签名；读不到返回 None"""
    try:
        st = os.stat(p)
        return (int(st.st_size), round(float(st.st_mtime), 3))
    except Exception:
        return None

def _scan_ledger(conn=None):
    """读增量台账 {路径: (size, mtime)}"""
    try:
        c = conn or db()
        return {r["path"]: (r["size"], r["mtime"])
                for r in c.execute("SELECT path,size,mtime FROM scan_files")}
    except Exception:
        return {}       # 静默部分结果：台账读不出 → 当作"没有台账"，下次全量重扫（功能正确，只是慢）

def _scan_ledger_write(seen, counts=None, conn=None):
    """把本次见到的文件签名写回台账"""
    if not seen:
        return
    counts = counts or {}
    try:
        c = conn or db()
        c.executemany(
            "INSERT OR REPLACE INTO scan_files(path,size,mtime,sessions,scanned_at) VALUES(?,?,?,?,?)",
            [(k, v[0], v[1], counts.get(k, 0), now()) for k, v in seen.items()])
        c.commit()
    except Exception as e:
        # 容错：台账写失败不影响本次扫描结果，但要说出来 ——
        # 下次会对这些文件全量重扫（本机会话 jsonl 有 47MB，全量=几十秒）。
        print("[loci] 写入增量扫描台账失败(%s)；下次将对这些文件全量重扫" % e, file=sys.stderr)

def _scan_generic_jsonl(agent, root, subdirs=(), known=None, seen=None, force=False, stats=None):
    """扫一个 agent 的 jsonl 会话目录（Claude Code / Codex / WorkBuddy 通用）

    比早期版本多做了三件事（都是为了 WorkBuddy，但对所有来源都有好处）：
      ① 只收对话记录 —— 按 type 过滤掉工具调用/推理块，别把噪音当聊天
      ② 顺手捡标题 —— WorkBuddy 的 `ai-title` 行、Claude Code 的 `summary` 行，
         有就用它当会话标题。否则标题只能截首条消息，而首条常是系统注入块
      ③ 带上时间 —— 每行自己的 timestamp 一路带到会话级，
         以前这里恒为空，导进来的会话没有发生时间

    增量：`known` 是 {文件路径: (size, mtime)} 台账（来自 scan_files 表）。
    签名一致的文件直接跳过解析，`seen` 用于把本次见到的签名回写。
    """
    home = os.path.expanduser("~")
    _root = os.path.expanduser(root) if str(root).startswith("~") else root
    base = _root if os.path.isabs(_root) else os.path.join(home, _root)
    out = []
    known = known or {}
    st = stats if stats is not None else {}
    if not os.path.isdir(base):
        return out
    for dirpath, _, files in os.walk(base):
        for fn in files:
            if not fn.endswith(".jsonl"):
                continue
            p = os.path.join(dirpath, fn)
            sig = _file_sig(p)
            if sig is None:
                continue
            key = _norm_key(p)
            if seen is not None:
                seen[key] = sig
            st["files"] = st.get("files", 0) + 1
            if not force and known.get(key) == sig:
                st["unchanged"] = st.get("unchanged", 0) + 1
                continue
            try:
                text = io.open(p, encoding="utf-8", errors="replace").read()
            except Exception:
                continue      # 容错：文件被锁 / 无权限读不了就跳过这个文件（扫描本就允许部分失败）
            st["parsed"] = st.get("parsed", 0) + 1
            title, msgs = "", []
            for line in text.splitlines():
                line = line.strip()
                if not line:
                    continue
                try:
                    o = json.loads(line)
                except Exception:
                    continue      # 容错：脏 JSONL 行跳过（写入中断/日志被截断是常态）
                if not isinstance(o, dict):
                    continue
                typ = str(o.get("type") or "").lower()
                if typ == "ai-title":                       # WorkBuddy
                    title = title or str(o.get("aiTitle") or o.get("title") or "").strip()
                    continue
                if typ == "summary":                        # Claude Code
                    title = title or str(o.get("summary") or "").strip()
                    continue
                if typ in _NON_DIALOG_TYPES:
                    continue
                m = _record_to_msg(o)
                if m:
                    msgs.append(m)
            if len(msgs) >= 2:
                at = next((m["at"] for m in msgs if m.get("at")), "")
                en = next((m["at"] for m in reversed(msgs) if m.get("at")), "")
                out.append({"agent": agent, "session": fn[:-6], "title": title, "dir": dirpath,
                            "at": at, "end": en, "messages": msgs})
                st.setdefault("file_sessions", {})[key] = 1
            else:
                st.setdefault("file_sessions", {}).setdefault(key, 0)
    st["sessions"] = st.get("sessions", 0) + len(out)
    return out

# ---------- 本机对话来源：探测，而不是写死 ----------
# 2026-09-21 改。之前 auto_scan_agents() 里写死三路：
#     scan_zcode_db(...) + _scan_generic_jsonl("Claude Code", ...) + _scan_generic_jsonl("Codex", ...)
# 后果：本机**主力**来源 WorkBuddy（~/.workbuddy/projects，8 会话 / 1377 条消息）
# 一个字都没被扫到；而根本没装的 Claude Code / Codex 却挂在名单上，
# 面板文案还照着写"正在扫描本机对话（ZCode / Claude Code / Codex）"。
# 换台电脑这名单就是错的 —— 名单得从磁盘上问出来，不能靠作者当时装了啥。
SCAN_SOURCES = (
    {"agent": "WorkBuddy", "kind": "jsonl",
     "root": os.path.join("~", ".workbuddy", "projects"),
     "note": "WorkBuddy / CodeBuddy 桌面端会话"},
    {"agent": "ZCode", "kind": "zcode_sqlite",
     "root": os.path.join("~", ".zcode", "cli", "db", "db.sqlite"),
     "note": "智谱 ZCode 会话库（SQLite）"},
    {"agent": "Claude Code", "kind": "jsonl",
     "root": os.path.join("~", ".claude", "projects"),
     "note": "Claude Code 会话"},
    {"agent": "Codex", "kind": "jsonl",
     "root": os.path.join("~", ".codex", "sessions"),
     "note": "OpenAI Codex 会话"},
)

def detect_conversation_sources():
    """问磁盘：本机到底哪些 Agent 存了对话？

    返回每个登记源的状态（found / empty / absent）。**没数据的也返回**，
    并说明为什么没扫 —— 面板要能解释"名单为什么是这几个"，
    而不是甩一份作者写死的产品列表出来。
    """
    out = []
    for s in SCAN_SOURCES:
        root = os.path.expanduser(s["root"])
        info = {"agent": s["agent"], "kind": s["kind"], "root": root,
                "note": s["note"], "state": "absent", "files": 0, "size": 0}
        if s["kind"] == "jsonl":
            if os.path.isdir(root):
                files = []
                for dp, _, fs in os.walk(root):
                    files += [os.path.join(dp, f) for f in fs if f.endswith(".jsonl")]
                info["files"] = len(files)
                for f in files:
                    try:
                        info["size"] += os.path.getsize(f)
                    except Exception:
                        pass      # 容错：单个文件取不到大小就跳过它，目录大小是展示用统计
                info["state"] = "found" if files else "empty"
        elif s["kind"] == "zcode_sqlite":
            if os.path.isfile(root):
                info["files"] = 1
                try:
                    info["size"] = os.path.getsize(root)
                except Exception:
                    pass      # 容错：同上 —— 取不到大小不影响"已找到"这个判定
                info["state"] = "found"
        out.append(info)
    return out

def suggest_conversation_sources(max_depth=3):
    """找"疑似存了对话、但没登记"的目录 —— 本机装了别的 Agent 时能自己冒出来。

    只在 home 的一级点目录里找 .jsonl，且抽头部判断有没有 "role" 字段
    （对话记录的特征）。**只建议，不自动扫** —— 万一是日志或数据文件，
    乱导进去比漏导更烦人。
    """
    home = os.path.expanduser("~")
    known = {os.path.normcase(os.path.expanduser(s["root"])) for s in SCAN_SOURCES}
    hits = []
    try:
        tops = [os.path.join(home, d) for d in os.listdir(home)
                if d.startswith(".") and os.path.isdir(os.path.join(home, d))]
    except Exception:
        return hits     # 静默部分结果：列不出 home 下的隐藏目录就返回已收集的（仅用于"发现新来源"探测）
    for top in tops:
        if any(os.path.normcase(top).startswith(k) or k.startswith(os.path.normcase(top))
               for k in known):
            continue
        n, sample = 0, ""
        for dp, dirs, fs in os.walk(top):
            if dp[len(top):].count(os.sep) >= max_depth:
                dirs[:] = []
                continue
            for f in fs:
                if not f.endswith(".jsonl"):
                    continue
                p = os.path.join(dp, f)
                try:
                    head = io.open(p, encoding="utf-8", errors="replace").read(2048)
                except Exception:
                    continue      # 容错：探测性读取失败（被锁/无权限）就跳过这个文件
                if '"role"' in head:
                    n += 1
                    if not sample:
                        sample = dp
            if n > 50:
                break
        if n:
            hits.append({"dir": sample or top, "root": top, "files": n})
    return hits

def auto_scan_agents(include_subagent=True, import_new=True, extract=False,
                     max_candidates=30, zcode_db=None, force=False):
    """自动扫描本机各 Agent 的对话存储 → 去重归档 → 可选抽候选记忆

    扫哪些来源由 detect_conversation_sources() 现场探测决定（见那里的注释）。
    返回值里带上 `sources` / `used`，面板据此显示"这次到底扫了谁、为什么"。

    `force=True` 忽略增量台账，强制重新解析所有文件（改了解析规则后用）。
    """
    sources = detect_conversation_sources()
    seen, counts = {}, {}
    try:
        conn = db()
        known = _scan_ledger(conn)
    except Exception:
        conn, known = None, {}
    found = []
    used = []
    for s in sources:
        if s["state"] != "found":
            continue
        stats = {}
        if s["kind"] == "zcode_sqlite":
            got = scan_zcode_db(zcode_db or s["root"], include_subagent, known=known,
                                seen=seen, force=force, stats=stats)
        else:
            got = _scan_generic_jsonl(s["agent"], s["root"], known=known,
                                      seen=seen, force=force, stats=stats)
        found += got
        counts.update(stats.get("file_sessions", {}))
        used.append({"agent": s["agent"], "files": stats.get("files", 0),
                     "parsed": stats.get("parsed", 0),
                     "unchanged": stats.get("unchanged", 0),
                     "sessions": len(got)})
    result = {"scanned": len(found), "imported": 0, "skipped": 0,
              "sessions": [], "candidates": [], "force": bool(force),
              "sources": sources, "used": used,
              "files_unchanged": sum(u["unchanged"] for u in used)}
    new_ids = []
    for item in found:
        if not import_new:
            continue
        title = item["title"] or (item["agent"] + "：" + item["messages"][0]["content"][:22])
        sid, created = save_session(title, item.get("dir") or "", item["agent"],
                                    item["messages"], source_path="agent-scan://" + item["session"],
                                    started_at=item.get("at", ""), ended_at=item.get("end", ""))
        if created:
            result["imported"] += 1
            result["sessions"].append({"id": sid, "title": title, "agent": item["agent"],
                                       "count": len(item["messages"])})
            new_ids.append(sid)
        else:
            result["skipped"] += 1
    _scan_ledger_write(seen, counts, conn)
    try:
        if conn is not None:
            conn.close()
    except Exception:
        pass      # 容错：连接可能已被上游关闭，close 再失败也无需处理
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

def extract_candidates(session_id=None, project=None, limit=40, max_msgs=800):
    """从已归档会话里按规则抽取候选记忆（零依赖，不用 LLM）

    `max_msgs` 限制单次扫描的消息条数 —— 本机主力会话动辄 700+ 条，
    全量扫一遍的收益远小于代价（候选还要人工勾选）。
    """
    conn = db()
    base = ("SELECT m.*, s.project sproject, s.agent sagent, s.title stitle "
            "FROM messages m JOIN sessions s ON s.id=m.session_id")
    if session_id:
        rows = conn.execute(base + " WHERE m.session_id=? ORDER BY m.turn LIMIT ?",
                            (int(session_id), int(max_msgs))).fetchall()
    else:
        args = []
        sql = base
        if project:
            sql += " WHERE s.project=?"; args.append(project)
        sql += " ORDER BY m.session_id DESC, m.turn LIMIT 3000"
        rows = conn.execute(sql, args).fetchall()
    conn.close()
    existing = [dict(r) for r in list_memories(limit=10000)]
    # 去重要拿候选片段跟 existing 逐条比 Jaccard（similarity 里是纯 Python 逐字符分词）。
    # 以前每次比较都把片段和记忆**重新分词**——片段被分词几千遍、记忆被分词上万遍，
    # 遇到刚导入的大会话（上千条消息 × 每条切几十段）直接把面板扫描拖到 2 分钟以上。
    # 现在两边各分词一次；再用 Jaccard 的长度上界剪枝（下面那行 continue）。
    ex_toks = [set(tokenize(e["content"] or "")) for e in existing]
    ex_len = [len(t) for t in ex_toks]
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
            st = set(tokenize(s))
            ls = len(st)
            dup = False
            for et, le in zip(ex_toks, ex_len):
                if not ls or not le:
                    continue
                # Jaccard(A,B) ≤ min(|A|,|B|) / max(|A|,|B|) —— 这是个硬上界。
                # 长度比都到不了 0.7 的一对，交集算了也过不了 0.7，直接跳过。
                # 纯剪枝，不改结果（tools/verify_extract_perf.py 对真实库比对校验）。
                if (ls if ls < le else le) / (ls if ls > le else le) <= 0.7:
                    continue
                if len(st & et) / len(st | et) > 0.7:
                    dup = True
                    break
            if dup:
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
    name = "loci-" + re.sub(r"[^\w\u4e00-\u9fff-]", "_", project)
    lines = ["---", "name: " + name,
             "description: 由 Loci 记忆库生成的「%s」经验手册（%d 条）" % (project, len(rows)),
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
    name = "loci-" + re.sub(r"[^\w\u4e00-\u9fff-]", "_", project)
    d = os.path.join(base, name)
    os.makedirs(d, exist_ok=True)
    p = os.path.join(d, "SKILL.md")
    with open(p, "w", encoding="utf-8") as f:
        f.write(md)
    return {"ok": True, "path": p, "name": name, "count": md.count("\n- ")}

# ---------- 技能库：本机已有的 skill，跨 Agent 传递 ----------
# 解决的问题：记忆传下去了，能力没传。换台机器/换个 Agent，skill 得手动翻目录找。
# 这里只做两件事：① 把本机所有 skill 探出来（跨 Agent、跨作用域）；
# ② 把一个 skill 复制到另一个 Agent 的 skills 目录。**不删源** —— "移植"是复制。

_WORKBUDDY_SKILLS = os.path.join("~", ".workbuddy", "skills")

def _fm_parse(text):
    """解析 SKILL.md 头部的 --- 块。只认 name/description/agent_created 这类简单键值，
    折行续写（YAML 的 >- 风格）拼成一行 —— 不引 yaml 依赖。"""
    if not text.startswith("---"):
        return {}
    end = text.find("\n---", 3)
    if end < 0:
        return {}
    out, key = {}, None
    for line in text[3:end].splitlines():
        if not line.strip():
            continue
        if line[:1].isspace() and key:          # 续行
            out[key] = (out[key] + " " + line.strip()).strip()
            continue
        m = re.match(r"^([A-Za-z_][\w-]*)\s*:\s*(.*)$", line)
        if not m:
            continue
        key, val = m.group(1), m.group(2).strip()
        if val in (">-", ">", "|", "|-"):       # 块标量：正文在后面的缩进行
            val = ""
        out[key] = val.strip("\"'")
    return out

def workspaces(limit=12):
    """本机的 WorkBuddy 工作区（项目级 skill 藏在各工作区里）。

    工作区列表从会话 jsonl 的 cwd 字段读 —— 只读文件头，不解析整个会话。
    目录名（c-Users-<用户名>-WorkBuddy-2026-09-21-13-44-31）里的 `-`
    跟真实路径的 `-` 有歧义，反推不出来，所以必须读 cwd。
    """
    base = os.path.join(os.path.expanduser("~"), ".workbuddy", "projects")
    out, seen = [], set()
    if not os.path.isdir(base):
        return out
    for d in sorted(os.listdir(base), reverse=True)[:limit]:
        p = os.path.join(base, d)
        if not os.path.isdir(p):
            continue
        for f in sorted(os.listdir(p)):
            if not f.endswith(".jsonl"):
                continue
            try:
                head = io.open(os.path.join(p, f), encoding="utf-8", errors="replace").read(16384)
            except Exception:
                continue      # 容错：同上的探测性读取 —— 读不到就跳过这个文件
            m = re.search(r'"cwd"\s*:\s*"((?:[^"\\]|\\.)*)"', head)
            if not m:
                continue
            cwd = m.group(1).replace("\\\\", "\\")
            if cwd and os.path.isdir(cwd) and os.path.normcase(cwd) not in seen:
                seen.add(os.path.normcase(cwd))
                out.append(cwd)
            break
    return out

def skill_sources():
    """本机所有 skill 存放位置。存在的报 exists，不存在的也返回（说明为什么没扫）

    `why` 直接给**人话**，由后端算 —— 前端各写一套判断必然会写歪：
    第一版把"父目录不存在"一律说成"本机没装这个 Agent"，
    于是 WorkBuddy 的项目级条目（父目录是工作区，不是产品安装目录）
    也报"没装 WorkBuddy"，明显是错的。
    """
    home = os.path.expanduser("~")
    roots = [
        {"agent": "WorkBuddy", "scope": "用户级", "root": os.path.join(home, ".workbuddy", "skills")},
        {"agent": "ZCode", "scope": "用户级", "root": os.path.join(home, ".zcode", "cli", "skills")},
        {"agent": "CodeBuddy", "scope": "用户级", "root": os.path.join(home, ".codebuddy", "skills")},
        {"agent": "Trae", "scope": "内置", "root": os.path.join(home, ".trae-cn", "builtin_skills")},
        {"agent": "Claude Code", "scope": "用户级", "root": os.path.join(home, ".claude", "skills")},
        {"agent": "Codex", "scope": "用户级", "root": os.path.join(home, ".codex", "skills")},
    ]
    for ws in workspaces():
        roots.append({"agent": "WorkBuddy", "scope": "项目级",
                      "root": os.path.join(ws, ".workbuddy", "skills"), "workspace": ws})
    for r in roots:
        r["exists"] = os.path.isdir(r["root"])
        ws = r.get("workspace")
        if ws:
            # 项目级：容器是**工作区本身**。<ws>/.workbuddy 不存在不等于工作区没了 ——
            # 第一版就是判了 <ws>/.workbuddy，于是把存在的工作区说成"已不在"。
            r["parent_exists"] = os.path.isdir(ws)
            r["anchor"] = ws
        else:
            r["parent_exists"] = os.path.isdir(os.path.dirname(r["root"]))
            r["anchor"] = os.path.dirname(r["root"])
        if r["exists"]:
            r["why"] = ""
        elif ws:
            r["why"] = ("这个工作区还没建 skills 目录（往这儿传技能会自动建）"
                        if r["parent_exists"] else "工作区目录已不在（临时目录被清过）")
        else:
            # 说人话：报**产品名**，不要报父目录名 ——
            # 报 os.path.basename(anchor) 会输出 "cli 已在本机"（ZCode）、
            # ".codebuddy 已在本机"（CodeBuddy），用户看不懂这是谁。
            r["why"] = ("%s 已在本机，只是还没建 skills 目录（传技能过去会自动建）"
                        % r["agent"]
                        if r["parent_exists"] else "本机没有装这个 Agent")
    return roots

def list_local_skills():
    """列出本机全部 skill（目录型：含 SKILL.md）

    只读，不动任何文件。名字优先取 frontmatter 的 name。
    """
    out = []
    for r in skill_sources():
        root = r["root"]
        if not r["exists"]:
            continue
        try:
            names = sorted(os.listdir(root))
        except Exception:
            continue      # 容错：skills 根目录读不了（无权限/已卸载）就跳过这个来源
        for name in names:
            p = os.path.join(root, name)
            # 只跳过隐藏目录。**不按名字前缀过滤** —— Trae 的 `_shared` 是内部目录，
            # 但它本来就没有 SKILL.md，靠下面那条判据就挡住了；
            # 反过来，名字带下划线的真 skill 不该被名字误伤。
            if name.startswith(".") or not os.path.isdir(p):
                continue
            md = os.path.join(p, "SKILL.md")
            if not os.path.isfile(md):
                continue
            try:
                text = io.open(md, encoding="utf-8", errors="replace").read()
            except Exception:
                continue      # 容错：单个 SKILL.md 读不了就跳过该 skill，不影响其他 skill
            fm = _fm_parse(text)
            files, size, latest = 0, 0, 0.0
            for dp, _, fs in os.walk(p):
                for f in fs:
                    files += 1
                    try:
                        size += os.path.getsize(os.path.join(dp, f))
                        latest = max(latest, os.path.getmtime(os.path.join(dp, f)))
                    except Exception:
                        pass      # 容错：skills 目录里读不到的文件跳过，大小/时间是展示用
            desc = re.sub(r"\s+", " ", (fm.get("description") or "")).strip()
            out.append({
                "name": fm.get("name") or name,
                "dir": name,
                "desc": desc,
                "agent": r["agent"],
                "scope": r["scope"],
                "workspace": r.get("workspace", ""),
                "path": p,
                "md": md,
                "files": files,
                "size": size,
                "lines": text.count("\n") + 1,
                "mtime": (datetime.datetime.fromtimestamp(latest).strftime("%Y-%m-%d %H:%M")
                          if latest else ""),
                "agent_created": str(fm.get("agent_created", "")).lower() in ("true", "yes", "1"),
            })
    out.sort(key=lambda x: (x["agent"], x["scope"], x["name"]))
    return out

# ── 本机内容清单：配置文件 / MCP ──────────────────────────────────────────
# 表里**只填本机实测过的路径**。这条规矩是被用户骂出来的：
# 上一版凭印象列了 22 个产品的路径，用户一句"什么 Roo/Factory/Goose/Amp，我没听过"
# 就全废了 —— 本机 13 个候选产品一个都没装，写进去的全是无法验证的死代码。
# 现在：加产品 = 加一行，但加之前先用 tools/probe_inventory.py 验一遍真假。
#
# 四种 MCP schema 靠表里**声明**，不靠嗅探（嗅探会把别的同名键吃进来）：
#   mcpServers        WorkBuddy / Trae（也是最常见的约定）
#   mcp.servers       ZCode —— 双层嵌套，路径写 "mcp.servers"
#   mcp               Kilo / OpenCode 系（本机没装，先按约定留着见下方注释）
#   servers           VS Code 系（同上）

CONTENT_SOURCES = (
    {"agent": "WorkBuddy",
     "configs": ["~/.workbuddy/mcp.json", "~/.workbuddy/settings.json",
                 "~/.workbuddy/models.json"],
     "mcp": [("~/.workbuddy/mcp.json", "mcpServers")]},
    {"agent": "ZCode",
     "configs": ["~/.zcode/cli/config.json", "~/.zcode/v2/config.json",
                 "~/.zcode/v2/provider_config.json", "~/.zcode/v2/setting.json",
                 "~/.zcode/v2/credentials.json"],
     "mcp": [("~/.zcode/cli/config.json", "mcp.servers")]},
    {"agent": "Trae",
     "configs": ["~/.trae-cn/mcp.json", "~/.trae-cn/plugin-config.json",
                 "~/.trae-cn/installed-plugins.json", "~/.trae-cn/argv.json"],
     "mcp": [("~/.trae-cn/mcp.json", "mcpServers")]},
    {"agent": "Copilot",
     "configs": ["~/.copilot/config.json"],
     "mcp": []},
    # CodeBuddy 本机只有 diagnostics/logs，一个配置文件都没有 ——
    # 照样留一行空表，好让面板能如实说"这个产品没东西可清算"，
    # 而不是像没扫过一样悄悄消失。
    {"agent": "CodeBuddy",
     "configs": [], "mcp": []},
    # ~/.agents/AGENTS.md 是**跨 Agent 共享的规则文件**（本机 5 个 Agent 都读它），
    # 不属于任何单一产品，所以 agent 名就写"共享规则"。
    # 它是 Markdown 不是 JSON —— 按扩展名走文本模式，不做 JSON 脱敏
    # （规则文件是用户自己写的，本来就该能看全文）。
    {"agent": "共享规则",
     "configs": ["~/.agents/AGENTS.md"],
     "mcp": []},
)

# 非 JSON 的配置文件按扩展名走文本模式（不解析、不脱敏）
_TEXT_EXTS = (".md", ".txt", ".yaml", ".yml", ".toml", ".ini", ".conf")


def _content_fmt(path):
    # 备份文件名形如 `models.json.bak-20260921-211853` / `AGENTS.md.bak-...`
    # —— 取原扩展名才能判对，否则 `.bak-*` 把 `.md`/`.json` 接走，会误判成 JSON
    # 然后在 backup_detail 里走错分支（文本备份被当 JSON 解析失败 → is_backup 漏标）。
    low = str(path).lower()
    for _ in range(2):
        i = low.rfind(".bak")
        if i > 0 and (i + 4 == len(low) or low[i + 4] == "-"):
            low = low[:i]
            continue
        break
    return "text" if low.endswith(_TEXT_EXTS) else "json"

# 值一律不读的键名（大小写不敏感，命中即隐去）
_SECRET_KEY_HINTS = ("token", "secret", "password", "passwd", "apikey", "api_key",
                     "credential", "authorization", "jwt", "cookie", "private_key")

# 整份文件都不读值的文件名特征
_SECRET_FILE_HINTS = ("credential", "secret", "token", ".key", ".pem")

# 键名里"整段是标识符"的形状：UUID、纯 hex 串、或 60 位以上的超长 blob。
# 阈值为什么是 60 而不是 18/40：ZCode 的 setting.json 里有一堆 40 多位的
# **正常驼峰键名**（desktopChromiumHardwareAccelerationEnabled 等 4 个），
# 按长度一刀切会把它们也压成 `…` —— 而键名是这类敏感文件里唯一的信息。
_KEYNAME_BLOB = re.compile(
    r"^(?:[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}"
    r"|[0-9a-fA-F]{32,}"
    r"|[A-Za-z0-9+/=_\-]{60,})$")


def _is_secret_file(path):
    """这个文件整份都不该读值。**按文件名判，宁可误判成敏感。**"""
    b = os.path.basename(path).lower()
    return b.startswith(".env") or any(h in b for h in _SECRET_FILE_HINTS)


def _mask_keyname(k):
    """键名里**像标识符的整段**压成一撮，其余原样保留。

    报键名是为了让用户认得出"这是什么"（oauth:zai:access_token），
    不是为了把账号标识甩出来（…:account:a683fe91-7b35-…:api-key）。

    判据只认"整段是 UUID / 超长 blob"，**不按长度一刀切** ——
    第一版写成"≥18 字符就压"，结果 `enable-crash-reporter`、
    `modelProviderFamilySelectedKeys` 这些正常键名也被压成 `…`，
    等于把键名这条唯一的信息也弄丢了。
    """
    parts = []
    for seg in str(k).split(":"):
        parts.append("…" if _KEYNAME_BLOB.match(seg) else seg)
    return ":".join(parts)


def _mask_json(obj, depth=0):
    """把 JSON 里像密钥的值换成占位符，其余原样保留。

    两条规则，缺一不可：
      ① 键名命中敏感词（API_KEY / TOKEN / SECRET…）→ 值隐去；
      ② 键名就是 env / environment → **只留键名**，值全隐去 ——
         MCP 的 env 块经常塞 API Key，而键名本身（FOO_BASE_URL）也可能是变量，
         分不清就一律隐去值。
    """
    if depth > 10:
        return "…"
    if isinstance(obj, dict):
        out = {}
        for k, v in obj.items():
            kl = str(k).lower()
            if isinstance(v, str) and any(h in kl for h in _SECRET_KEY_HINTS):
                out[k] = "••••••（已隐去）"
            elif kl in ("env", "environment") and isinstance(v, dict):
                out[k] = dict((ik, "••••••（已隐去）") for ik in v)
            else:
                out[k] = _mask_json(v, depth + 1)
        return out
    if isinstance(obj, list):
        return [_mask_json(x, depth + 1) for x in obj]
    return obj


def _jsonc_to_json(text):
    """JSONC（带 // 注释、/* */ 块注释、尾逗号）→ 能解析的 JSON。

    被咬过一次：~/.copilot/config.json 带 `//` 注释，裸 json.load 直接抛异常，
    上一版探针读出来是 None，看着像"这文件是空的"。
    **逐字符扫，不用正则**：正则替换会把 "https://x" 里的 // 也当注释切掉。
    """
    out = []
    i, n = 0, len(text)
    in_str, esc = False, False
    while i < n:
        c = text[i]
        if in_str:
            out.append(c)
            if esc:
                esc = False
            elif c == "\\":
                esc = True
            elif c == '"':
                in_str = False
            i += 1
            continue
        if c == '"':
            in_str, i = True, i + 1
            out.append('"')
            continue
        if c == "/" and i + 1 < n and text[i + 1] == "/":
            while i < n and text[i] not in "\r\n":
                i += 1
            continue
        if c == "/" and i + 1 < n and text[i + 1] == "*":
            end = text.find("*/", i + 2)
            i = n if end < 0 else end + 2
            continue
        if c == ",":
            j = i + 1
            while j < n and text[j] in " \t\r\n":
                j += 1
            if j < n and text[j] in "}]":
                i += 1          # 尾逗号：丢掉
                continue
        out.append(c)
        i += 1
    return "".join(out)


def _read_json_file(path):
    """读 JSON，失败就按 JSONC 再试一次。返回 (data, note)；note 说明走了哪条路。"""
    try:
        raw = io.open(path, encoding="utf-8", errors="replace").read()
    except Exception as e:
        return None, "读取失败：%s" % e
    try:
        return json.loads(raw), ""
    except Exception:
        pass      # 容错：这步是"先按纯 JSON 试"，失败就落到下面按行 JSONL 解析（多格式尝试）
    try:
        return json.loads(_jsonc_to_json(raw)), "含注释（JSONC），已容错解析"
    except Exception as e:
        return None, "解析失败：%s" % e


def _dig(obj, keypath):
    """按 "a.b.c" 取嵌套值，中途不是 dict 就返回 None。"""
    cur = obj
    for k in str(keypath or "").split("."):
        if not isinstance(cur, dict) or k not in cur:
            return None
        cur = cur[k]
    return cur


def _content_paths():
    """表里声明过的全部文件绝对路径（读详情时的白名单）。"""
    out = []
    for s in CONTENT_SOURCES:
        for x in list(s["configs"]) + [p for p, _ in s["mcp"]]:
            out.append(os.path.abspath(os.path.expanduser(x)))
    return out


def _read_text_file(path):
    try:
        return io.open(path, encoding="utf-8", errors="replace").read(), ""
    except Exception as e:
        return None, "读取失败：%s" % e


def list_configs():
    """本机各 Agent 的配置文件清单（只读；敏感文件只报存在与键名）"""
    out = []
    for src in CONTENT_SOURCES:
        for rel in src["configs"]:
            path = os.path.expanduser(rel)
            if not os.path.isfile(path):
                continue
            secret = _is_secret_file(path)
            fmt = _content_fmt(path)
            try:
                st = os.stat(path)
                size, mt = st.st_size, st.st_mtime
            except Exception:
                size, mt = 0, 0
            item = {
                "kind": "config",
                "name": os.path.basename(path),
                # rel 用来区分同名文件：ZCode 有 cli/config.json 和 v2/config.json 两个
                # config.json，只报 basename 的话列表里两行长得一模一样。
                "rel": rel,
                "agent": src["agent"],
                "path": path,
                "exists": True,
                "dir": os.path.dirname(path),
                "size": size,
                "mtime": (datetime.datetime.fromtimestamp(mt).strftime("%Y-%m-%d %H:%M")
                          if mt else ""),
                "secret": secret,
                "note": "",
                "keys": [],
                "fmt": fmt,
                "backups": len(_backup_paths_for(path)),
            }
            if fmt == "text":
                text, note = _read_text_file(path)
                item["note"] = note
                item["lines"] = (text.count("\n") + 1) if text else 0
                out.append(item)
                continue
            data, note = _read_json_file(path)
            item["note"] = note
            if secret:
                # 敏感文件：连"看起来没问题"的值也不读出来，只报键名。
                if isinstance(data, dict):
                    item["keys"] = [_mask_keyname(k) for k in list(data.keys())]
                item["note"] = "敏感文件：只报存在与键名，值一律不读"
            elif isinstance(data, dict):
                item["keys"] = [_mask_keyname(k) for k in list(data.keys())]
            out.append(item)
    out.sort(key=lambda x: (x["agent"], x["name"]))
    return out


def list_mcp_servers():
    """本机各 Agent 挂了哪些 MCP（schema 由来源表声明，不嗅探）

    env 块只回**键名**，值一律不回 —— 里面经常就是 API Key。
    """
    out = []
    for src in CONTENT_SOURCES:
        for rel, keypath in src["mcp"]:
            path = os.path.expanduser(rel)
            if not os.path.isfile(path):
                continue
            data, note = _read_json_file(path)
            if not isinstance(data, dict):
                continue
            servers = _dig(data, keypath)
            if not isinstance(servers, dict):
                continue
            for name, spec in servers.items():
                spec = spec if isinstance(spec, dict) else {}
                if spec.get("command"):
                    transport = "stdio"
                elif spec.get("url"):
                    transport = "http"
                else:
                    transport = "未知"
                args = spec.get("args") or []
                env = spec.get("env") or {}
                out.append({
                    "kind": "mcp",
                    "name": name,
                    "rel": rel,
                    "agent": src["agent"],
                    "path": path,
                    "dir": os.path.dirname(path),
                    "keypath": keypath,
                    "transport": transport,
                    "command": str(spec.get("command") or ""),
                    "args": [str(a) for a in (args if isinstance(args, list) else [args])],
                    "url": str(spec.get("url") or ""),
                    "env_keys": sorted(str(k) for k in env) if isinstance(env, dict) else [],
                    "disabled": bool(spec.get("disabled")),
                    "note": note,
                })
    out.sort(key=lambda x: (x["agent"], x["name"]))
    return out


def content_sources():
    """给前端报"为什么这个产品没有内容" —— 每个产品一行，含空表。"""
    rows = []
    for src in CONTENT_SOURCES:
        cfgs = [os.path.expanduser(x) for x in src["configs"]]
        found = [p for p in cfgs if os.path.isfile(p)]
        if not cfgs:
            why = "表里没给这个产品声明配置文件路径（本机实测：它只有日志目录）"
        elif not found:
            why = "声明的 %d 个路径在本机都不存在" % len(cfgs)
        else:
            why = ""
        rows.append({"agent": src["agent"], "declared": len(cfgs),
                     "found": len(found), "why": why})
    return rows


def content_detail(path, max_chars=20000):
    """读一个配置文件的（已脱敏）正文。**只认表里声明过的路径。**"""
    p = os.path.abspath(path or "")
    allowed = set(os.path.normcase(x) for x in _content_paths())
    if os.path.normcase(p) not in allowed:
        return {"error": "不在已知的配置文件清单里，拒绝读取"}
    if not os.path.isfile(p):
        return {"error": "文件不存在：%s" % p}
    try:
        size = os.path.getsize(p)
    except Exception:
        size = 0
    backups = _backup_paths_for(p)
    # 非 JSON（AGENTS.md 这类规则文件）：直接回原文 —— 是用户自己写的规则，
    # 本来就该能看全文，不做 JSON 脱敏（也解析不了）。
    if _content_fmt(p) == "text":
        text, note = _read_text_file(p)
        if text is None:
            return {"ok": True, "path": p, "secret": False, "size": size, "note": note,
                    "keys": [], "text": "", "backups": backups,
                    "why": "读不出来：%s" % note}
        return {"ok": True, "path": p, "secret": False, "size": size, "note": note,
                "keys": [], "fmt": "text", "backups": backups,
                "text": text[:max_chars], "truncated": len(text) > max_chars, "why": ""}
    data, note = _read_json_file(p)
    if _is_secret_file(p):
        keys = [_mask_keyname(k) for k in data.keys()] if isinstance(data, dict) else []
        return {"ok": True, "path": p, "secret": True, "size": size, "note": note,
                "keys": keys, "text": "", "backups": backups,
                "why": "敏感文件（%s）：只报存在与键名，值一律不读" % os.path.basename(p)}
    if data is None:
        # 解析失败也不回原文 —— 回原文等于绕过脱敏
        return {"ok": True, "path": p, "secret": False, "size": size, "note": note,
                "keys": [], "text": "", "backups": backups,
                "why": "这个文件读不出来（%s），且不回原文：原文未经脱敏" % (note or "格式未知")}
    text = json.dumps(_mask_json(data), ensure_ascii=False, indent=2)
    return {"ok": True, "path": p, "secret": False, "size": size, "note": note,
            "keys": [_mask_keyname(k) for k in data.keys()] if isinstance(data, dict) else [],
            "text": text[:max_chars], "truncated": len(text) > max_chars, "why": "",
            "backups": backups}


# ── 插件 / 本地历史版本 ──────────────────────────────────────────────────
# "历史版本"这块竞品是放在**云端**的（它的"炉子"），用户明确不要云端产品理念。
# 本机实测发现：我们要的东西**本来就躺在本地磁盘上**，一个字节都不用上云 ——
#   · 插件旧版本：~/.workbuddy/plugins/cache/<市场>/<插件>/<版本>/ 共 123 个版本目录，
#     已装 48 个 → **75 个旧版本**还在磁盘上（5.5.3 / 5.5.4 / 5.5.6 三代同堂）
#   · 配置备份：<配置文件名>.bak-<时间戳>，本机 5 个（AGENTS.md / mcp.json /
#     models.json ×2 / cli-config.json）
# 所以这里做的是「就地清算」，不是「云端归档」。
#
# ⚠ 本文件里插件路径一律**以已装清单为准**，不靠扫目录猜：
#    从 installPath 反推插件目录再列版本。扫目录会把 `<市场>/plugins/` 这种
#    容器目录当成插件名（实测扫出来过一个 `cb_teams_marketplace/plugins`）。

PLUGIN_SOURCES = (
    # WorkBuddy：值形如 [{"scope","installPath","version","installedAt","lastUpdated"}]
    {"agent": "WorkBuddy",
     "file": "~/.workbuddy/plugins/installed_plugins.json",
     "keypath": "plugins", "style": "installed_v2", "sep": "@"},
    # ZCode：值就是 bool（启用/停用），没有版本、没有路径
    {"agent": "ZCode",
     "file": "~/.zcode/cli/config.json",
     "keypath": "plugins.enabledPlugins", "style": "enabled_map", "sep": "@"},
    # Trae：值是 {"user_enabled":bool,"app_origin_enabled":bool}，分隔符是**冒号**
    # （`trae-remote-official:browser`，registry 在前）—— 跟另两家相反
    {"agent": "Trae",
     "file": "~/.trae-cn/plugin-config.json",
     "keypath": "plugins", "style": "config_map", "sep": ":"},
)

# ⚠ Trae 的 installed-plugins.json **不是**已装清单，是**市场目录**
#   （{"runtime","generated_at","marketplaces":[{"marketplace","page","plugins"}]}）。
#   名字叫 installed 但装的是市场快照 —— 照名字取会把它当插件列表用。
#   已装的看 plugin-config.json。这条注释是踩过一次留下的（见设计规范 6.9）。


def _plugin_split(pid, sep="@"):
    """`name@marketplace` / `registry:name` → (name, marketplace)

    两种分隔符方向相反，所以分隔符由来源表声明，不猜。
    """
    s = str(pid)
    if sep and sep in s:
        a, b = s.split(sep, 1)
        return (b, a) if sep == ":" else (a, b)
    return s, ""


def _plugin_versions(install_path):
    """从 installPath 反推插件目录，列出磁盘上所有版本。

    只认 ~/.workbuddy/plugins/ 底下的路径（白名单），别的一律不碰。
    """
    root = os.path.join(os.path.expanduser("~"), ".workbuddy", "plugins")
    p = os.path.abspath(install_path or "")
    if not p or not os.path.normcase(p).startswith(os.path.normcase(os.path.abspath(root))):
        return []
    plug_dir = os.path.dirname(p)          # .../cache/<市场>/<插件>
    cur_ver = os.path.basename(p)
    if not os.path.isdir(plug_dir):
        return []
    out = []
    try:
        names = sorted(os.listdir(plug_dir))
    except Exception:
        return []
    for v in names:
        vp = os.path.join(plug_dir, v)
        # 版本目录名以数字开头；顺手挡掉 .bak / 隐藏目录
        if not os.path.isdir(vp) or not re.match(r"^\d", v) or v.startswith("."):
            continue
        try:
            mt = os.path.getmtime(vp)
        except Exception:
            mt = 0
        has_skill = os.path.isfile(os.path.join(vp, "SKILL.md"))
        try:
            entries = len(os.listdir(vp))
        except Exception:
            entries = 0
        out.append({
            "version": v,
            "path": vp,
            "mtime": (datetime.datetime.fromtimestamp(mt).strftime("%Y-%m-%d %H:%M")
                      if mt else ""),
            "mtime_ts": mt,
            "is_current": v == cur_ver,
            "has_skill": has_skill,
            "entries": entries,
        })
    # 时间倒序：最近的排最前（同批安装的 mtime 一样，再用版本号兜底）
    out.sort(key=lambda x: (-x["mtime_ts"], x["version"]), reverse=False)
    out.sort(key=lambda x: x["mtime_ts"], reverse=True)
    return out


def list_plugins():
    """本机各 Agent 装了哪些插件（只读）

    三种 schema 各写一个分支，不强行抽象成一套 —— 它们的字段本来就不同，
    硬统一只会把某家的信息丢掉（ZCode 就没有版本和路径）。
    """
    out = []
    for src in PLUGIN_SOURCES:
        path = os.path.expanduser(src["file"])
        if not os.path.isfile(path):
            continue
        data, note = _read_json_file(path)
        if not isinstance(data, dict):
            continue
        node = _dig(data, src["keypath"])
        if not isinstance(node, dict):
            continue
        for pid, spec in node.items():
            name, market = _plugin_split(pid, src["sep"])
            item = {
                "kind": "plugin",
                "id": pid,
                "name": name,
                "marketplace": market,
                "agent": src["agent"],
                "version": "",
                "enabled": True,
                "installed_at": "",
                "updated_at": "",
                "path": "",
                "dir": "",
                "version_count": 0,
                "old_count": 0,
                "note": note,
                "source_file": path,
            }
            if src["style"] == "installed_v2":
                it = spec[0] if isinstance(spec, list) and spec else (
                    spec if isinstance(spec, dict) else {})
                item["path"] = str(it.get("installPath") or "")
                # dir 是给前端"打开所在文件夹"用的（openCurDir 读的是 .dir）。
                # 插件的 path 是**版本目录**（...\<插件>\5.5.6），要打开的是它上一层。
                item["dir"] = os.path.dirname(item["path"]) if item["path"] else ""
                item["version"] = str(it.get("version") or "")
                item["installed_at"] = str(it.get("installedAt") or "")[:19].replace("T", " ")
                item["updated_at"] = str(it.get("lastUpdated") or "")[:19].replace("T", " ")
                vers = _plugin_versions(item["path"])
                item["version_count"] = len(vers)
                item["old_count"] = sum(1 for v in vers if not v["is_current"])
            elif src["style"] == "enabled_map":
                item["enabled"] = bool(spec)
                item["note"] = "这个产品只记了启用开关，不记版本与安装路径"
            elif src["style"] == "config_map":
                if isinstance(spec, dict):
                    item["enabled"] = bool(spec.get("user_enabled", True))
                    item["note"] = ("用户已启用" if spec.get("user_enabled")
                                    else "用户已停用")
                if market:
                    item["marketplace"] = market
                item["note"] = "这个产品只记了启用开关，不记版本与安装路径"
            out.append(item)
    # 排序：**本地留的旧版本多的排前面**。
    # 不按 agent 字母序 —— 那样 ZCode/Trae 这 7 个"只记了启用开关"的会霸占最前，
    # 把真正有旧版本历史可看的插件挤出屏幕（它们才是这一页的重点）。
    # 同分再按 agent / name，保证顺序稳定。
    out.sort(key=lambda x: (-(x["old_count"]), x["agent"], x["name"]))
    return out


def plugin_versions(agent, plugin_path):
    """某个插件在本机磁盘上留了几个版本（详情页用）"""
    vers = _plugin_versions(plugin_path)
    return {
        "ok": True,
        "versions": vers,
        "current": next((v["version"] for v in vers if v["is_current"]), ""),
        "old": [v for v in vers if not v["is_current"]],
        "why": ("" if vers else
                "本机没有留下这个插件的版本目录（可能是别的产品装的，或已清理）"),
    }


# ── 配置文件的历史版本（.bak-<时间戳>） ─────────────────────────────────
# 只认 CONTENT_SOURCES 里声明过的文件的备份 —— 不扫全盘。
# 实测全盘扫会捞出一堆噪音（Internet Explorer\brndlog.bak、
# AppData\Local\Temp\panel.bak.py），那是系统临时文件，不是"历史版本"。

def _backup_paths_for(path):
    """这个文件的本地备份（同目录、同名前缀 + .bak/.backup）。"""
    d = os.path.dirname(path)
    b = os.path.basename(path)
    out = []
    if not os.path.isdir(d):
        return out
    try:
        names = os.listdir(d)
    except Exception:
        return out      # 静默部分结果：目录读不了 → 当作"没有备份"（展示用，不影响主流程）
    for f in names:
        low = f.lower()
        if not (low.startswith(b.lower() + ".bak") or low.startswith(b.lower() + ".backup")):
            continue
        fp = os.path.join(d, f)
        if not os.path.isfile(fp):
            continue
        try:
            st = os.stat(fp)
        except Exception:
            continue      # 容错：文件在遍历途中消失（竞态）→ 跳过，即"不算孤儿"
        out.append({"path": fp, "name": f, "size": st.st_size, "mtime_ts": st.st_mtime,
                    "dir": d,
                    "mtime": datetime.datetime.fromtimestamp(st.st_mtime).strftime("%Y-%m-%d %H:%M:%S"),
                    "of": b})
    out.sort(key=lambda x: x["mtime_ts"], reverse=True)
    return out


def content_backups(cfgs=None):
    """全部配置文件的备份，按时间倒序（列表用）

    允许传入已经读好的 cfgs —— `/api/content` 同时要 configs 和 backups，
    不传的话 list_configs() 会被读两遍（每个文件都重新解析一次）。
    """
    out = []
    for c in (cfgs if cfgs is not None else list_configs()):
        for bk in _backup_paths_for(c["path"]):
            bk["agent"] = c["agent"]
            bk["kind"] = "backup"
            out.append(bk)
    out.sort(key=lambda x: x["mtime_ts"], reverse=True)
    return out


def backup_detail(path, max_chars=20000):
    """读一个配置备份的（已脱敏）正文。

    ⚠ 备份**必须走同一套脱敏**，不能因为是备份就跳过 —— 本机的
    `models.json.bak-*` 里就是**明文 API Key**，这条要是漏了，
    整个脱敏设计就形同虚设（相当于给了个"看原文"的后门）。
    """
    p = os.path.abspath(path or "")
    allowed = set()
    for bk in content_backups():
        allowed.add(os.path.normcase(os.path.abspath(bk["path"])))
    if os.path.normcase(p) not in allowed:
        return {"error": "这个文件不是已知配置文件的备份，拒绝读取"}
    if not os.path.isfile(p):
        return {"error": "文件不存在：%s" % p}
    try:
        size = os.path.getsize(p)
    except Exception:
        size = 0
    # 备份也可能是 Markdown（AGENTS.md.bak-*）
    if _content_fmt(p) == "text":
        text, note = _read_text_file(p)
        return {"ok": True, "path": p, "secret": False, "size": size, "note": note,
                "keys": [], "fmt": "text", "is_backup": True,
                "text": (text or "")[:max_chars],
                "truncated": bool(text) and len(text) > max_chars, "why": ""}
    data, note = _read_json_file(p)
    if data is None:
        return {"ok": True, "path": p, "secret": False, "size": size, "note": note,
                "keys": [], "text": "",
                "why": "这个备份读不出来（%s），且不回原文：原文未经脱敏" % (note or "格式未知"),
                "is_backup": True}
    text = json.dumps(_mask_json(data), ensure_ascii=False, indent=2)
    return {"ok": True, "path": p, "secret": False, "size": size, "note": note,
            "keys": [_mask_keyname(k) for k in data.keys()] if isinstance(data, dict) else [],
            "text": text[:max_chars], "truncated": len(text) > max_chars, "why": "",
            "is_backup": True}


def local_skill_detail(path, max_chars=20000):
    """读一个 skill 的 SKILL.md 正文（面板上预览用）"""
    p = os.path.abspath(path or "")
    known = [os.path.abspath(s["root"]) for s in skill_sources()]
    inside = any(os.path.normcase(p).startswith(os.path.normcase(k)) for k in known)
    md = os.path.join(p, "SKILL.md") if os.path.isdir(p) else p
    if not os.path.isfile(md):
        return {"error": "找不到 SKILL.md"}
    if not inside:
        return {"error": "不在已知的 skill 目录里，拒绝读取"}
    try:
        text = io.open(md, encoding="utf-8", errors="replace").read()
    except Exception as e:
        return {"error": "读取失败：%s" % e}
    return {"ok": True, "path": md, "text": text[:max_chars],
            "truncated": len(text) > max_chars, "size": len(text)}

def skill_copy_targets():
    """能把这个 skill 传到哪 —— 只列本机真实存在的容器目录

    判据是**父目录存在**（说明那个 Agent 装过），而不是"这个目录当前有没有内容"。
    目标目录不存在会自动建（传 skill 本来就该能建目录）。
    """
    home = os.path.expanduser("~")
    out = []
    for agent, scope, root in (
            ("WorkBuddy", "用户级", os.path.join(home, ".workbuddy", "skills")),
            ("ZCode", "用户级", os.path.join(home, ".zcode", "cli", "skills")),
            ("CodeBuddy", "用户级", os.path.join(home, ".codebuddy", "skills")),
            ("Trae", "用户级", os.path.join(home, ".trae-cn", "skills")),
            ("Claude Code", "用户级", os.path.join(home, ".claude", "skills")),
            ("Codex", "用户级", os.path.join(home, ".codex", "skills"))):
        parent = os.path.dirname(root)
        out.append({"agent": agent, "scope": scope, "root": root,
                    "ready": os.path.isdir(parent),
                    "why": "" if os.path.isdir(parent) else "本机没装这个 Agent（父目录 %s 不存在）" % parent})
    for ws in workspaces():
        root = os.path.join(ws, ".workbuddy", "skills")
        out.append({"agent": "WorkBuddy", "scope": "项目级", "root": root,
                    "ready": os.path.isdir(ws), "workspace": ws, "why": ""})
    return out

def plan_skill_copy(src_path, target_root, as_name=""):
    """预览迁移：从哪到哪、几个文件、会不会撞名。**不写盘。**"""
    src = os.path.abspath(src_path or "")
    tgt_root = os.path.abspath(target_root or "")
    known_src = [os.path.abspath(s["root"]) for s in skill_sources()]
    if not any(os.path.normcase(src).startswith(os.path.normcase(k)) for k in known_src):
        return {"error": "源不在已知 skill 目录里"}
    allowed = {os.path.normcase(os.path.abspath(t["root"])) for t in skill_copy_targets()}
    if os.path.normcase(tgt_root) not in allowed:
        return {"error": "目标目录不在白名单里"}
    if not os.path.isdir(src):
        return {"error": "源 skill 目录不存在"}
    # 名字只允许字母数字下划线中日文点和横杠 —— 挡住 ..\..\ 这类穿越
    name = re.sub(r"[^\w\u4e00-\u9fff.-]", "_", (as_name or "").strip())
    if not name:
        name = os.path.basename(src.rstrip("\\/"))
    if name in (".", "..") or name.startswith("."):
        return {"error": "目标名字不合法"}
    dst = os.path.join(tgt_root, name)
    if os.path.normcase(src) == os.path.normcase(dst):
        return {"error": "源和目标是同一个目录"}
    files, size = 0, 0
    for dp, _, fs in os.walk(src):
        for f in fs:
            files += 1
            try:
                size += os.path.getsize(os.path.join(dp, f))
            except Exception:
                pass      # 容错：统计复制体积时单个文件失败跳过，不影响复制本身
    return {"ok": True, "src": src, "dst": dst, "name": name,
            "files": files, "size": size,
            "dst_exists": os.path.exists(dst),
            "target_root": tgt_root}

def copy_skill_to(src_path, target_root, overwrite=False, as_name=""):
    """执行迁移（复制，不删源）。

    撞名默认不覆盖 —— 让人先看清楚。要覆盖时也不直接删旧目录，
    而是先改名成 `.bak-<时间戳>` 留在原地，出问题能倒回去。
    """
    plan = plan_skill_copy(src_path, target_root, as_name=as_name)
    if plan.get("error"):
        return plan
    if plan["dst_exists"] and not overwrite:
        return {"error": "目标已存在同名 skill（%s）。确认要覆盖再说。" % plan["dst"], "plan": plan}
    import shutil as _sh
    backup = ""
    try:
        os.makedirs(plan["target_root"], exist_ok=True)
        if plan["dst_exists"]:
            backup = plan["dst"] + ".bak-" + datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
            os.rename(plan["dst"], backup)
        _sh.copytree(plan["src"], plan["dst"])
    except Exception as e:
        # 复制失败：把备份改回来，别让人丢了原来那份
        note = ""
        if backup and not os.path.exists(plan["dst"]):
            try:
                os.rename(backup, plan["dst"])
            except Exception:
                # ⚠️ 2026-09-28 修：回滚也失败时，原目录其实还躺在 `<dst>.bak-<时间戳>` 下，
                # 但原来只回一句"复制失败" —— 用户以为配置丢了。现在把真实路径写进错误信息。
                note = ("；且回滚失败，你的原目录现在位于 %s（没有丢，请手动改名回来）" % backup)
        return {"error": "复制失败：%s%s" % (e, note), "plan": plan}
    return {"ok": True, "dst": plan["dst"], "files": plan["files"],
            "size": plan["size"], "overwrote": bool(plan["dst_exists"]),
            "backup": backup}


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
             f"> 生成时间 {now()} ｜ 共 {len(rows)} 条记忆 ｜ 来源: Loci 本地记忆库", ""]
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

# 宿主会往首条 user 消息前面插一段环境注入块（`<system-reminder data-role="user-context">`
# 里塞 OS 版本、当前时间、连接器状态等）。它是给模型看的，不是人说的话 ——
# 直接入库会让会话标题变成 "<system-reminder data-role=..." 这种鬼东西，
# 也会把"人到底说了什么"淹掉。2026-09-21 接 WorkBuddy 来源时发现。
_INJECT_RE = re.compile(r"<system-reminder\b[^>]*>[\s\S]*?</system-reminder>", re.I)
_INJECT_OPEN_RE = re.compile(r"<system-reminder\b[^>]*>[\s\S]*$", re.I)
_INJECT_TAG_RE = re.compile(r"</?system-reminder\b[^>]*>", re.I)
# WorkBuddy 把用户输入包在 <user_query> 里（单纯是个信封，剥掉不损失内容）
_WRAP_TAG_RE = re.compile(r"</?user_query\b[^>]*>", re.I)

def _strip_injected(text):
    """剥掉宿主注入块和信封标签，只留人真正打的字。剥完为空则返回空串（调用方丢弃该条）"""
    if not text:
        return text
    t = text
    if "system-reminder" in t.lower():
        t = _INJECT_RE.sub("", t)
        t = _INJECT_OPEN_RE.sub("", t)      # 未闭合的尾巴
        t = _INJECT_TAG_RE.sub("", t)
    if "user_query" in t.lower():
        t = _WRAP_TAG_RE.sub("", t)
    return t.strip()

def _record_to_msg(obj):
    """把一条落盘记录转成 {role, content[, at]}，不是消息则返回 None

    `at` = 这条记录自己的时间（能认出来才带）。以前不带时间，
    所以除了 ZCode 以外的来源导进来全是空时间。
    """
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
    text = _strip_injected(_msg_text(content).strip())
    if not text:
        return None
    # 过滤非对话记录（摘要、工具调用等），但保留有正文的
    typ = str(obj.get("type") or obj.get("event") or "").lower()
    if not role and typ in ("summary", "system", "meta", "tool", "tool_result"):
        return None
    role = str(role).lower()
    if role not in ("user", "assistant", "system", "tool"):
        role = "assistant" if inner else (role or "unknown")
    out = {"role": role, "content": text}
    at = norm_time(obj.get("timestamp") or obj.get("time") or obj.get("at")
                   or obj.get("created_at") or obj.get("createdAt"))
    if at:
        out["at"] = at
    return out

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
def save_session(title="", project="", agent="", messages=None, source_path="", summary="", allow_dup=False,
                 started_at="", ended_at=""):
    """保存一次会话（含原文）。同一内容重复导入会被指纹拦截，返回 (sid, created)

    时间从哪来（2026-09-21 修）：
      以前只从**消息**里读 at（msgs[0].get("at")），但没有任何调用方往消息里塞过 at，
      于是 started_at / ended_at 永远是空串 —— 全表 0/10 有值。UI 拿不到会话时间，
      只能退回 created_at（= 导入那一下的 now()），所以"9 月 20 日的 10 段对话"
      全都显示成同一个扫描时刻。
      现在两层都收：显式传进来的 started_at/ended_at 优先；没传就退回消息自带的 at。
      传进来的值一律过 norm_time()，epoch 毫秒 / ISO 串都能收。
    """
    msgs = messages or []
    if not msgs:
        return None, False
    fp = hashlib.md5(("\n".join(f"{m.get('role','')}:{m.get('content','')}" for m in msgs)).encode("utf-8")).hexdigest()
    t = now()
    n = len(msgs)
    if not title:
        first = next((m["content"] for m in msgs if m.get("role") == "user"), msgs[0].get("content", ""))
        title = (first[:24] + "…") if len(first) > 24 else (first or "未命名会话")
    # 会话时间：显式参数 > 首/末条消息自带的 at > 空（不编造）
    # 消息自带时间两个键都认：扫描器写 "at"，导出包写 "created_at"（见 panel.py 的 pack 导出）
    def _mat(seq):
        if not seq or not isinstance(seq[0], dict):
            return ""
        return seq[0].get("at") or seq[0].get("created_at") or ""
    st = norm_time(started_at) or norm_time(_mat(msgs[:1]))
    en = norm_time(ended_at) or norm_time(_mat(msgs[-1:]))
    conn = db()
    # ---- 同源幂等（2026-09-22 修）----
    # 症状：一段 WorkBuddy 对话被反复采集，同一个 source_path 堆了 62 条 sessions
    #   （msg_count 692→767 一路递增），messages 表被灌到 4 万条；会话列表里
    #   同一段对话重复几十行，用户直接问"为什么不能合并"。
    # 根因：旧去重只按"整段消息的 md5"，而每次扫描对话都更长 → 指纹次次不同 → 次次新建。
    # 现在：source_path 非空 = 同一段会话的又一次快照 —— 原地更新保留的那条
    #   （只有内容变多才重写正文），并清掉同源的其余快照（记忆出处迁移到保留的那条），
    #   保证"一个来源恒为一条会话"。保留原 id，所以记忆里的"出处"链接不断。
    if source_path:
        dup = conn.execute(
            "SELECT id, msg_count FROM sessions WHERE source_path=? "
            "ORDER BY msg_count DESC, id DESC", (source_path,)).fetchall()
        if dup:
            keep = dup[0]["id"]
            if len(dup) > 1:
                dead = [r["id"] for r in dup[1:]]
                conn.executemany("UPDATE memories SET session_id=? WHERE session_id=?",
                                 [(keep, d) for d in dead])
                conn.executemany("DELETE FROM messages WHERE session_id=?", [(d,) for d in dead])
                conn.executemany("DELETE FROM sessions WHERE id=?", [(d,) for d in dead])
            if n > (dup[0]["msg_count"] or 0):
                conn.execute("DELETE FROM messages WHERE session_id=?", (keep,))
                conn.executemany(
                    "INSERT INTO messages (session_id,turn,role,content,created_at) VALUES (?,?,?,?,?)",
                    [(keep, i, m.get("role", "unknown"), m.get("content", ""),
                      norm_time(m.get("at") or m.get("created_at") or "") or t)
                     for i, m in enumerate(msgs, 1)])
                try:
                    conn.execute(
                        "UPDATE sessions SET title=?,project=?,agent=?,msg_count=?,summary=?,fingerprint=?,"
                        "started_at=COALESCE(NULLIF(?,''),started_at),"
                        "ended_at=COALESCE(NULLIF(?,''),ended_at) WHERE id=?",
                        (title, project, agent, n, summary, fp, st, en, keep))
                except sqlite3.IntegrityError:
                    # 新指纹撞了另一条会话（内容完全一致）：保留旧指纹，别让保存整个失败
                    conn.execute(
                        "UPDATE sessions SET title=?,project=?,agent=?,msg_count=?,summary=?,"
                        "started_at=COALESCE(NULLIF(?,''),started_at),"
                        "ended_at=COALESCE(NULLIF(?,''),ended_at) WHERE id=?",
                        (title, project, agent, n, summary, st, en, keep))
            conn.commit()
            conn.close()
            return keep, False
    if not allow_dup:
        row = conn.execute("SELECT id FROM sessions WHERE fingerprint=?", (fp,)).fetchone()
        if row:
            conn.close()
            return row["id"], False
    cur = conn.execute(
        "INSERT INTO sessions (title,project,agent,source_path,started_at,ended_at,msg_count,summary,fingerprint,created_at)"
        " VALUES (?,?,?,?,?,?,?,?,?,?)",
        (title, project, agent, source_path, st, en, n, summary, fp, t))
    sid = cur.lastrowid
    # 每条消息也尽量用它自己的时间；没有就退回导入时刻
    conn.executemany(
        "INSERT INTO messages (session_id,turn,role,content,created_at) VALUES (?,?,?,?,?)",
        [(sid, i, m.get("role", "unknown"), m.get("content", ""),
          norm_time(m.get("at") or m.get("created_at") or "") or t) for i, m in enumerate(msgs, 1)])
    conn.commit()
    conn.close()
    return sid, True

def list_sessions(limit=50, project=None):
    conn = db()
    # mem_n：这段会话产出了几条记忆。列表里直接给出来，"哪段对话有产出"一眼可见
    # ——这是会话层和记忆层之间的那条线，别删。（走 idx_mem_session，成本可忽略）
    sql = ("SELECT s.*, (SELECT COUNT(*) FROM memories m "
           "WHERE m.session_id=s.id AND m.deleted=0) mem_n FROM sessions s")
    args = []
    if project:
        sql += " WHERE s.project=?"; args.append(project)
    sql += " ORDER BY s.id DESC LIMIT ?"
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
    # 同上：不再对 3000+ 条消息逐条重新分词（实测 2.4s/次 → 见提交说明）
    docs = [(r, _toks_tf(r["content"])) for r in rows]
    df = {}
    for _, (tf, _tn) in docs:
        for t in tf:
            df[t] = df.get(t, 0) + 1
    n = len(docs)
    idf = {t: math.log((n + 1) / (c + 0.5)) + 1 for t, c in df.items()}
    out = []
    for r, (tf, tn) in docs:
        if not tn:
            continue
        score = 0.0
        for t in q_toks:
            c = tf.get(t)
            if c:
                score += idf.get(t, 1.0) * (c / tn)
        if score <= 0:
            continue
        out.append((score / math.sqrt(tn), r))
    out.sort(key=lambda x: x[0], reverse=True)
    return out[:limit]
# ---------- MCP 工具定义 ----------
TOOLS = [
    {"name": "memory_save", "description": "保存一条记忆到本地共享记忆库（跨 Agent 可见）",
     "inputSchema": {"type": "object", "properties": {
         "content": {"type": "string", "description": "记忆内容（中文友好）"},
         "type": {"type": "string", "enum": list(TYPES), "description": "记忆类型"},
         "importance": {"type": "integer", "description": "重要度1-4"},
         "tags": {"type": "string", "description": "逗号分隔标签"},
         "project": {"type": "string", "description": "所属项目名"},
         "agent": {"type": "string", "description": "写入方 Agent 名"},
         "session_id": {"type": "integer", "description": "可选：这条结论出自哪段已归档会话（面板「会话」页的编号）。填了就能在面板里溯源回原话"},
         "turn": {"type": "integer", "description": "可选：出自该会话的第几轮"}},
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

class ToolError(Exception):
    """工具层「可预期的失败」：参数不合法、目标不存在等。

    为什么要单独一个类：MCP 侧要把这类失败标成 isError=True ——
    否则调用方（Agent）会把「记忆内容不能为空」当成一条正常结果读下去，
    继续按「保存成功」往下走。
    ⚠️ 走查实测（2026-09-25）：缺 content 时老代码会把空串当内容静默存进库，
    连写一条空记忆进去污染检索结果 —— schema 里声明 required 是不够的。
    """
    pass


def call_tool(name, args):
    if name == "memory_save":
        # content 必填：schema 声明之外，自己再兜一道（见 ToolError 注释）
        content = (args.get("content") or "").strip()
        if not content:
            raise ToolError("记忆内容不能为空（content 必填）")
        mid = save_memory(content, args.get("type", "fact"),
                          args.get("importance", 2), args.get("tags", ""),
                          args.get("project", ""),
                          args.get("agent") or DEFAULT_AGENT,
                          session_id=args.get("session_id", 0),
                          turn=args.get("turn", 0))
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
        if args.get("id") in (None, ""):
            raise ToolError("缺少参数 id（要删除的记忆编号）")
        delete_memory(int(args["id"]))
        return f"已删除记忆 #{args['id']}"
    if name == "memory_stats":
        s = stats()
        return json.dumps(s, ensure_ascii=False, indent=2)
    if name == "memory_handoff":
        proj = (args.get("project") or "").strip()
        if not proj:
            raise ToolError("缺少参数 project（要生成交接卡的项目名）")
        return handoff(proj)
    if name == "session_save":
        transcript = args.get("transcript") or ""
        if not transcript.strip():
            raise ToolError("会话内容不能为空（transcript 必填）")
        msgs = parse_transcript(transcript)
        if not msgs:
            raise ToolError("没有从这段文本里解析出对话内容"
                            "（支持「我: / AI:」聊天文本、JSONL、JSON 数组）")
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
        if args.get("id") in (None, ""):
            raise ToolError("缺少参数 id（要设为常驻的记忆编号）")
        set_pinned(int(args["id"]), int(args.get("pinned", 1)))
        return ("已设为常驻" if int(args.get("pinned", 1)) else "已取消常驻") + f"：记忆 #{args['id']}"
    raise ToolError(
        f"未知工具: {name}（可用：" + " / ".join(sorted(t["name"] for t in TOOLS)) + "）")

# ---------- 归档备份（长期备份）----------
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
    except Exception as e:
        # 容错：配置读不出来就用默认值继续，但要说出来 —— 否则用户设的归档参数被静默忽略
        print("[loci] 读取归档配置失败(%s)；本次使用默认配置" % e, file=sys.stderr)
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
                    continue      # 容错：快照文件 stat 失败就跳过（展示用统计，不影响其他备份）
                out.append({"name": fn, "path": p, "size": st.st_size, "ts": st.st_mtime,
                            "mtime": datetime.datetime.fromtimestamp(st.st_mtime).strftime("%Y-%m-%d %H:%M")})
    out.sort(key=lambda x: x["ts"], reverse=True)   # 按修改时间倒序，最新在最前
    return out


def do_archive_snapshot(force=False):
    """做一份数据库备份。不移动、不删改任何记忆，只是复制当前库。"""
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
    target = os.path.join(snap, "loci-%s.db" % today)
    if os.path.exists(target) and not force:
        return {"ok": True, "skipped": True, "path": target, "msg": "今天已有备份，跳过"}
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
            pass      # 容错：旧备份删不掉就留着，不阻塞新备份生成
    _sz = os.path.getsize(target) if os.path.exists(target) else 0
    return {"ok": True, "path": target, "size": _sz,
            "count": len(list_snapshots(d)), "removed": removed,
            "note": "备份是复制出来的副本，原有记忆未被移动或删除"}


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
            continue      # 协议容错：非 JSON 行（宿主往 stdin 塞了别的东西）忽略，继续读下一行
        method = req.get("method", "")
        rid = req.get("id")
        if method == "initialize":
            ver = req.get("params", {}).get("protocolVersion", "2024-11-05")
            reply(rid, {
                "protocolVersion": ver,
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "loci", "version": APP_VERSION}})
        elif method == "notifications/initialized":
            pass      # 协议要求：通知(notification)不需要响应，静默即可
        elif method == "tools/list":
            reply(rid, {"tools": TOOLS})
        elif method == "tools/call":
            p = req.get("params", {})
            try:
                text = call_tool(p.get("name", ""), p.get("arguments", {}) or {})
                reply(rid, {"content": [{"type": "text", "text": text}], "isError": False})
            except ToolError as e:
                # 参数不合法 / 工具名不存在这类「可预期的失败」：直接给原因，别加噪声
                reply(rid, {"content": [{"type": "text", "text": str(e)}], "isError": True})
            except Exception as e:
                reply(rid, {"content": [{"type": "text", "text": f"执行出错: {e}"}], "isError": True})
        elif method == "ping":
            reply(rid, {})
        elif rid is not None:
            reply(rid, error={"code": -32601, "message": f"method not found: {method}"})

# ---------- CLI ----------
def cli_call(name, args=None):
    """CLI 专用：把「可预期的失败」变成一行提示。

    交互式会话不该因为一次参数写错就整体退出 —— MCP 侧由协议层接住异常，
    CLI 侧没有那层壳，得自己接。args 传可调用对象时会在 try 内部求值，
    这样 int(rest) 这类转换错误也能被接住，而不是在调用前就炸掉。
    """
    try:
        a = args() if callable(args) else (args or {})
        return call_tool(name, a)
    except ToolError as e:
        return "错误: " + str(e)
    except (ValueError, KeyError, TypeError) as e:
        return "错误: 参数不对（" + str(e) + "）"


def cli():
    print("Loci CLI（输入 help 查看命令, quit 退出）")
    while True:
        try:
            line = input("loci> ").strip()
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
            print(cli_call("session_recall", {"query": rest}))
        elif cmd == "context":
            print(cli_call("memory_context", {"project": rest}))
        elif cmd == "pin":
            print(cli_call("memory_pin", lambda: {"id": int(rest), "pinned": 1}))
        elif cmd == "unpin":
            print(cli_call("memory_pin", lambda: {"id": int(rest), "pinned": 0}))
        elif cmd == "save":
            print(cli_call("memory_save", {"content": rest, "agent": "cli"}))
        elif cmd == "search":
            print(cli_call("memory_search", {"query": rest}))
        elif cmd == "list":
            print(cli_call("memory_list", {}))
        elif cmd == "stats":
            print(cli_call("memory_stats", {}))
        elif cmd == "handoff":
            print(cli_call("memory_handoff", {"project": rest}))
        elif cmd == "del":
            print(cli_call("memory_delete", lambda: {"id": int(rest)}))
        else:
            print("未知命令，输入 help")

def main():
    ap = argparse.ArgumentParser(description="Loci 个人跨 Agent 记忆中枢")
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
        print(cli_call("memory_save", {"content": a.save, "type": a.type, "importance": a.imp,
                                        "tags": a.tags, "project": a.proj, "agent": a.agent}))
    elif a.search:
        print(cli_call("memory_search", {"query": a.search}))
    elif a.stats:
        print(cli_call("memory_stats", {}))
    elif a.cli:
        cli()
    else:
        mcp_server()

if __name__ == "__main__":
    main()
