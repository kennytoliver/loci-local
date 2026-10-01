# -*- coding: utf-8 -*-
"""专项：面板 /api/save 返回的 id 与 /api/list 里的 id 是否同一个东西？

背景：上一轮我观察到"面板 save 写 #158、delete #159"，怀疑过 id 语义不一致。
这一轮复现出同样的形状：save 返回 {"id":1}/{"id":2}，但按内容查列表为空、
清理时只删掉了一个。必须查清面板的 list 返回的是什么字段。
"""
import json
import sys
import urllib.error
import urllib.request

BASE = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8799"


def get(path):
    try:
        with urllib.request.urlopen(BASE + path, timeout=25) as x:
            return x.status, x.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8", "replace")
    except Exception as e:
        return -1, f"{type(e).__name__}: {e}"


def post(path, payload):
    d = json.dumps(payload, ensure_ascii=False).encode()
    r = urllib.request.Request(BASE + path, data=d, method="POST")
    r.add_header("Content-Type", "application/json")
    r.add_header("Origin", BASE)
    try:
        with urllib.request.urlopen(r, timeout=20) as x:
            return x.status, x.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8", "replace")
    except Exception as e:
        return -1, f"{type(e).__name__}: {e}"


MARK = "__ID语义探测__"
print(f"BASE={BASE}")
print()
print("--- 1. 面板 /api/stats ---")
print("   ", get("/api/stats")[1][:220])

print()
print("--- 2. 写入一条，看返回的 id ---")
code, body = post("/api/save", {"content": MARK + "A", "mtype": "fact"})
print(f"    POST /api/save -> HTTP {code} {body}")
ret_id = None
try:
    ret_id = json.loads(body).get("id")
except Exception:
    pass

print()
print("--- 3. GET /api/list?limit=-1 的原始内容（前 3 条的全部字段）---")
code, body = get("/api/list?limit=-1")
try:
    rows = json.loads(body)
    print(f"    共 {len(rows)} 条")
    for r in rows[:3]:
        print("    ", json.dumps(r, ensure_ascii=False)[:300])
    hit = [r for r in rows if str(r.get("content", "")).startswith(MARK)]
    print(f"    按内容匹配到 {len(hit)} 条: {[(r.get('id'), r.get('content')) for r in hit]}")
    print(f"    ⚠ 面板 save 返回的 id = {ret_id!r}")
    ids = [r.get("id") for r in rows[:5]]
    print(f"    列表前 5 条的 id 字段 = {ids}")
    if hit and ret_id is not None:
        print(f"    → save 返回 {ret_id}，列表里同内容的 id = {hit[0].get('id')}  "
              f"{'一致' if ret_id == hit[0].get('id') else '❌ 不一致'}")
except Exception as e:
    print("    解析失败:", e, body[:200])

print()
print("--- 4. 直接读这个库（绕过面板），看真实 id ---")
import os  # noqa: E402
sys.path.insert(0, r"D:\Loci")
db_env = os.environ.get("LOCI_DB", "")
print("    面板进程的 LOCI_DB（本脚本环境里看不到，仅供参考）:", db_env or "(未设置)")

print()
print("--- 5. 清理：按精确内容删 ---")
for i in [r.get("id") for r in (json.loads(get("/api/list?limit=-1")[1]) or [])
          if str(r.get("content", "")).startswith(MARK)]:
    print(f"    删除 #{i} -> {post('/api/delete', {'id': i})[1][:60]}")
print("    残留:", len([r for r in json.loads(get("/api/list?limit=-1")[1])
                        if str(r.get("content", "")).startswith(MARK)]))
