# -*- coding: utf-8 -*-
"""面板 HTTP 接口烟测：先测合法参数，再用畸形参数探健壮性。"""
import json
import sys
import urllib.error
import urllib.parse
import urllib.request

BASE = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8799"


def get(pathqs, origin=None):
    url = BASE + pathqs
    req = urllib.request.Request(url)
    if origin:
        req.add_header("Origin", origin)
    try:
        with urllib.request.urlopen(req, timeout=25) as r:
            body = r.read().decode("utf-8", "replace")
            return r.status, body
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8", "replace")
    except Exception as e:
        return -1, f"{type(e).__name__}: {e}"


CASES = [
    # (说明, 路径, 是否期望成功)
    ("/api/stats", "/api/stats", True),
    ("/api/list 默认", "/api/list", True),
    ("/api/list 带项目过滤", "/api/list?project=Loci&limit=5", True),
    ("/api/search 中文", "/api/search?q=" + urllib.parse.quote("缓存"), True),
    ("/api/handoff", "/api/handoff?project=" + urllib.parse.quote("Loci"), True),
    ("/api/context", "/api/context", True),
    ("/api/audit", "/api/audit", True),
    ("/api/health", "/api/health", True),
    ("/api/audit/report", "/api/audit/report", True),
    ("/api/skill/preview", "/api/skill/preview?project=Loci", True),
    ("/api/session/list", "/api/session/list?limit=5", True),
    ("/api/session/get 存在", "/api/session/get?sid=1", True),
    ("/api/session/search", "/api/session/search?q=" + urllib.parse.quote("记忆"), True),
    ("/api/archive", "/api/archive", True),
    ("/api/backups", "/api/backups", True),
    ("/api/ping", "/api/ping", True),
    ("/api/scan-roots", "/api/scan-roots", True),
    ("/api/orphans", "/api/orphans", True),
    ("/api/cleanup/preview", "/api/cleanup/preview?limit=5", True),
    ("/api/conv-sources", "/api/conv-sources", True),
    ("/api/sourcefiles", "/api/sourcefiles", True),
    # ---- 畸形参数 ----
    ("/api/list?limit=abc  (非数字 limit)", "/api/list?limit=abc", False),
    ("/api/search?limit=abc", "/api/search?q=x&limit=abc", False),
    ("/api/session/list?limit=abc", "/api/session/list?limit=abc", False),
    ("/api/session/get?sid=abc", "/api/session/get?sid=abc", False),
    ("/api/session/get?sid=99999999", "/api/session/get?sid=99999999", True),
    ("/api/cleanup/preview?limit=abc", "/api/cleanup/preview?limit=abc", False),
    ("/api/extract?sid=abc", "/api/extract?sid=abc", False),
    ("/api/list?limit=-1", "/api/list?limit=-1", None),
    ("/api/session/search?limit=abc", "/api/session/search?q=x&limit=abc", False),
]

print("=" * 74)
bad = []
for name, path, expect_ok in CASES:
    code, body = get(path)
    mark = "OK " if code == 200 else f"HTTP {code}"
    print(f"[{mark}] {name}   ({len(body)} bytes)")
    if expect_ok and code != 200:
        bad.append((name, code, body[:200]))
    if expect_ok is False and code == 200:
        print(f"        未拦住畸形参数，返回: {body[:110]}")
    if code not in (200,):
        print(f"        响应片段: {body[:180].replace(chr(10), ' ')}")

print("=" * 74)
# CSRF 守卫：伪造外部 Origin 访问 /api/ 应被拒
code, body = get("/api/stats", origin="http://evil.example.com")
print(f"CSRF 守卫（外部 Origin 访问 /api/stats）-> HTTP {code}  {'PASS 已拦截' if code != 200 else '⚠ 未拦截'}")
# 无 Origin 应放行（面板自身导航）
code, body = get("/api/stats")
print(f"无 Origin 访问 /api/stats -> HTTP {code}  {'PASS' if code == 200 else '⚠ 被误拦'}")
# 路径穿越尝试
for p in ("/../loci.py", "/assets/../loci.py", "/icon.png", "/../panel.py"):
    code, body = get(p)
    print(f"路径探测 {p} -> HTTP {code} ({len(body)} bytes)")

if bad:
    print("\n❌ 合法请求却失败：")
    for n, c, b in bad:
        print(f"  - {n}: HTTP {c} {b[:120]}")
else:
    print("\n所有合法请求均 200")
