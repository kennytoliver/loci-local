# -*- coding: utf-8 -*-
"""专项：/api/list 的返回是否稳定？（怀疑有非确定性）

现象：连续 save 得到 #4/#5/#6，但 list 只显示 #6；清理后再 list 又冒出 #5。
本脚本每一步都打印 list 的**完整原始响应**与条数，连续多次读同一端点看是否抖动。
"""
import json
import sys
import time
import urllib.parse
import urllib.request

BASE = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8799"


def get(path, raw=False):
    with urllib.request.urlopen(BASE + path, timeout=25) as x:
        body = x.read().decode("utf-8", "replace")
        return x.status, body


def post(path, payload):
    d = json.dumps(payload, ensure_ascii=False).encode()
    r = urllib.request.Request(BASE + path, data=d, method="POST")
    r.add_header("Content-Type", "application/json")
    r.add_header("Origin", BASE)
    with urllib.request.urlopen(r, timeout=20) as x:
        return x.status, x.read().decode("utf-8", "replace")


print(f"BASE={BASE}")
print()
print("=== A. 同一端点连续读 6 次，看是否稳定 ===")
for k in range(6):
    st, body = get("/api/list?limit=-1")
    try:
        arr = json.loads(body)
        ids = sorted(r.get("id") for r in arr)
        print(f"  第{k+1}次: HTTP {st} 条数={len(arr)} ids={ids}")
    except Exception:
        print(f"  第{k+1}次: HTTP {st} 解析失败 {body[:120]}")
    time.sleep(0.3)

print()
print("=== B. 写一条，立刻读（对比 stats 与 list）===")
st, body = post("/api/save", {"content": "__抖动探测__X", "mtype": "fact"})
print(f"  POST /api/save -> {body}")
new_id = json.loads(body).get("id")
for k in range(4):
    s = get("/api/stats")[1]
    st, lb = get("/api/list?limit=-1")
    try:
        arr = json.loads(lb)
        ids = sorted(r.get("id") for r in arr)
        found = any(r.get("id") == new_id for r in arr)
        print(f"  第{k+1}次: stats.total={json.loads(s).get('total')} list条数={len(arr)} "
              f"ids={ids} 含#{new_id}={found}")
    except Exception as e:
        print(f"  第{k+1}次: 解析失败 {e}")
    time.sleep(0.3)

print()
print("=== C. 用默认 limit（不传参数）与 limit=50 对比 ===")
for q in ("/api/list", "/api/list?limit=50", "/api/list?limit=1000", "/api/list?limit=-1"):
    st, body = get(q)
    try:
        arr = json.loads(body)
        print(f"  {q:<26} -> 条数={len(arr)} ids={sorted(r.get('id') for r in arr)}")
    except Exception:
        print(f"  {q:<26} -> 解析失败 {body[:100]}")

print()
print("=== D. 按 id 直查：/api/list 是否漏掉了某些行 ===")
st, body = get("/api/list?limit=-1")
arr = json.loads(body)
print(f"  list 返回 {len(arr)} 条")
for r in arr:
    print("   ", json.dumps({k: r.get(k) for k in ("id", "content", "mtype", "deleted", "superseded_by")},
                           ensure_ascii=False))
