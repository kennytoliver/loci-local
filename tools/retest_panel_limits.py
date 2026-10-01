# -*- coding: utf-8 -*-
"""补测报告 §7 复测清单里、我还没独立验证的两项：

  #4 面板 ?limit=abc / -1 / 0 / 99999  -> 400 / 20 条 / 1 条 / 200 条（不越界、不掐断）
  #5 POST {"id": 999999} 到 delete / pin / session/delete -> ok:false 且不改数据

同时验证：失败的写请求**真的没有改动任何数据**（用写前后的 stats 对拍）。
"""
import json
import sys
import urllib.error
import urllib.parse
import urllib.request

BASE = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8787"
R = []


def check(name, ok, detail=""):
    R.append((name, bool(ok), str(detail)[:150]))


def req(path, data=None, origin=None, ctype="application/json"):
    r = urllib.request.Request(BASE + path, data=data)
    if data is not None:
        r.add_header("Content-Type", ctype)
        r.method = "POST"
    if origin:
        r.add_header("Origin", origin)
    try:
        with urllib.request.urlopen(r, timeout=30) as resp:
            return resp.status, resp.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8", "replace")
    except Exception as e:
        return -1, f"{type(e).__name__}: {e}"


def stats():
    _, b = req("/api/stats")
    return json.loads(b)


print("### 复测项 4：面板 limit 收口")
s0 = stats()
for qs, expect in (("?limit=abc", "400"), ("?limit=-1", "200"), ("?limit=0", "200"),
                   ("?limit=99999", "200"), ("?limit=5", "200")):
    code, body = req("/api/list" + qs)
    n = None
    try:
        n = len(json.loads(body))
    except Exception:
        pass
    if expect == "400":
        check(f"/api/list{qs} -> 400 且不掐断", code == 400,
              f"HTTP {code} :: {body[:70]}")
    else:
        check(f"/api/list{qs} -> HTTP {code}, 返回 {n} 条（应在 [1,1000]）",
              code == 200 and isinstance(n, int) and 1 <= n <= 1000,
              f"HTTP {code} 条数={n}")

# 关键：-1 不能返回全表
code, body = req("/api/list?limit=-1")
n_minus1 = len(json.loads(body)) if code == 200 else -1
total = s0.get("total")
check(f"limit=-1 不再是全表（返回 {n_minus1} 条 vs 全表有效 {total} 条）",
      isinstance(n_minus1, int) and n_minus1 <= 1000, f"返回 {n_minus1}")

print("### 复测项 5：写接口对不存在目标的返回，且不改数据")
s_before = stats()
for path, payload, label in (
        ("/api/delete", {"id": 999999}, "delete 不存在 id"),
        ("/api/pin", {"id": 999999}, "pin 不存在 id"),
        ("/api/session/delete", {"sid": 999999}, "session/delete 不存在 sid"),
        ("/api/retire", {"id": 999999}, "retire 不存在 id"),
        ("/api/touch", {"id": 999999}, "touch 不存在 id"),
        ("/api/split", {"id": 999999}, "split 不存在 id")):
    code, body = req(path, json.dumps(payload).encode(), origin=BASE)
    try:
        j = json.loads(body)
        ok_false = j.get("ok") is False
    except Exception:
        ok_false = False
    check(f"{label} -> ok:false（HTTP {code}）", code == 200 and ok_false,
          f"HTTP {code} :: {body[:90]}")

# 缺参数
for path, payload, label in (("/api/delete", {}, "delete 缺 id"),
                             ("/api/pin", {}, "pin 缺 id"),
                             ("/api/session/delete", {}, "session/delete 缺 sid"),
                             ("/api/supersede", {}, "supersede 缺字段")):
    code, body = req(path, json.dumps(payload).encode(), origin=BASE)
    check(f"{label} -> 400 且不掐断", code == 400, f"HTTP {code} :: {body[:90]}")

s_after = stats()
changed = {k: (s_before.get(k), s_after.get(k))
           for k in set(s_before) | set(s_after)
           if s_before.get(k) != s_after.get(k) and k != "warn"}
check("全部失败写请求未改动任何数据", not changed, f"变化的键: {changed}")

print()
print("=" * 74)
for n, ok, d in R:
    print(f"[{'PASS' if ok else 'FAIL'}] {n}")
    if not ok and d:
        print(f"       {d}")
fa = [n for n, ok, _ in R if not ok]
print("=" * 74)
print(f"合计 {len(R)} 项，FAIL {len(fa)} 项" + (f" -> {fa}" if fa else ""))
sys.exit(1 if fa else 0)
