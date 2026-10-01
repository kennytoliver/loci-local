# -*- coding: utf-8 -*-
"""CSRF 可行性验证：无 Origin + text/plain 的跨站可构造写请求

⚠️ 重要：本脚本会**真的向面板写入一条哨兵记忆**（因为它要验证写操作是否被执行），
   然后按「精确内容」删除。请只在测试端口上运行，不要打 8787 生产面板：
        python panel.py --port 8799 --idle-exit 60
        python tools/probe_csrf.py http://127.0.0.1:8799

   另注意：面板的 /api/delete 是往**请求里那个 id** 打删除标记。若 id 传错，
   删除的会是别的记忆 —— 所以本脚本删除前先按内容精确查出 id，再逐个删。
"""
import json
import sys
import urllib.error
import urllib.parse
import urllib.request

BASE = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8799"

# 防误伤：默认拒绝把生产端口当靶子
if BASE.endswith(":8787") and "--force" not in sys.argv:
    print("拒绝运行：8787 是你的生产面板端口。")
    print("请先另起一个测试面板：python panel.py --port 8799 --idle-exit 60")
    print("确实要在 8787 上测，再加 --force（会在真实库里写一条哨兵再删掉）。")
    sys.exit(2)

SENTINEL = "CSRF哨兵_DO_NOT_KEEP_7f3a"


def post(path, body_str, headers):
    req = urllib.request.Request(BASE + path, data=body_str.encode("utf-8"), method="POST")
    for k, v in headers.items():
        req.add_header(k, v)
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            return r.status, r.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8", "replace")
    except Exception as e:
        return -1, f"{type(e).__name__}: {e}"


def get(path):
    try:
        with urllib.request.urlopen(BASE + path, timeout=20) as r:
            return r.status, r.read().decode("utf-8", "replace")
    except Exception as e:
        return -1, str(e)


def find_ids_by_content(text):
    """按精确内容查 id —— 不用模糊匹配，避免误删别的记忆。"""
    code, body = get("/api/list?limit=-1")
    try:
        return [r["id"] for r in json.loads(body) if r.get("content") == text]
    except Exception:
        return []


print("实验：模拟跨站页面能发出的「简单请求」（text/plain，无预检）")
print()
payload = json.dumps({"content": SENTINEL, "project": "安全探测"}, ensure_ascii=False)

c1, b1 = post("/api/save", payload, {"Content-Type": "text/plain",
                                     "Origin": "http://evil.example.com"})
print(f"  [外部 Origin + text/plain] HTTP {c1} :: {b1[:80]}")

c2, b2 = post("/api/save", payload, {"Content-Type": "text/plain"})
print(f"  [无 Origin + text/plain]   HTTP {c2} :: {b2[:80]}")

ids = find_ids_by_content(SENTINEL)
print(f"\n  哨兵是否落库: {'❌ 是（无 Origin 的写请求真的写进了库）' if ids else '否（被拦住了）'}")
if ids:
    print(f"  哨兵 id: {ids}")
    for i in ids:
        cc, bb = post("/api/delete", json.dumps({"id": i}), {"Content-Type": "text/plain"})
        print(f"    删除 #{i} -> HTTP {cc} :: {bb[:60]}")
    left = find_ids_by_content(SENTINEL)
    print(f"  清理后仍在库中的哨兵: {left if left else '已清理干净'}")
    if left:
        print("  ⚠️ 清理未完成 —— 请到面板「记忆」页按内容手动删除。")
        print("     （面板的软删除若不生效，说明删除请求打到了别的 id 上。）")
