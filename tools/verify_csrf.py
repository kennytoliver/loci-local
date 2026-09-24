# -*- coding: utf-8 -*-
"""闸门：面板的跨站请求防护（CSRF）是否还在。

为什么要有这道闸门
------------------
2026-09-24 实测（真 Chrome，从一个别的源 8800 打开页面）：那个页面能**静默写库** ——
攻击前 118 条 → 攻击后 119 条，多出一条 agent=panel 的伪造记忆。
原代码 do_POST 只看路径就执行，不看请求从哪来；do_GET 里 /api/autoscan 等
带副作用的接口也一样。原有 27 道闸门一条都没覆盖"来源"这个维度。

修法：Handler._csrf_guard() —— Origin/Referer 指向本面板自己才放行。
这道闸门就是钉住它，防止以后被无意改回去。

判据分三组
----------
A. 外人（带外来源头）→ 必须 403，且**一个动作都不执行**
B. 自己人（面板页面的同源请求）→ 必须放行，别把正常功能修坏了
C. 无来源头（MCP 直连 / tools 脚本 / curl）→ 必须放行
   —— 浏览器发 POST 一定带 Origin，所以"没带"就等于"不是浏览器发起的跨站请求"

用法：python tools/verify_csrf.py [http://127.0.0.1:8787]
"""
import json
import sys
import urllib.error
import urllib.request

BASE = (sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8787").rstrip("/")
EVIL = "http://evil.example"
OTHER_LOOPBACK = "http://127.0.0.1:8800"   # 本机另一个端口上的别的服务
results = []


def req(method, path, headers=None, body=None, timeout=20):
    data = json.dumps(body).encode("utf-8") if body is not None else None
    r = urllib.request.Request(BASE + path, data=data, method=method)
    if data is not None:
        r.add_header("Content-Type", "application/json")
    for k, v in (headers or {}).items():
        r.add_header(k, v)
    try:
        with urllib.request.urlopen(r, timeout=timeout) as resp:
            return resp.status, resp.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()
    except Exception as e:                       # 连接层失败也要算"没被拦住的漏"
        return None, repr(e).encode()


def check(name, cond, detail=""):
    results.append((name, bool(cond)))
    print("  %s  %s%s" % ("PASS" if cond else "FAIL", name,
                          ("   " + detail) if detail else ""))


print("== A. 外人必须被拦（403，且不执行动作）==")
for label, hdrs in [
    ("Origin=别的域名", {"Origin": EVIL}),
    ("Origin=本机另一个端口(8800)", {"Origin": OTHER_LOOPBACK}),
    ("Origin=null(沙箱/file://)", {"Origin": "null"}),
]:
    code, body = req("POST", "/api/save", hdrs, {"content": "CSRF-GATE-SHOULD-NOT-WRITE"})
    check("POST /api/save  %s → 403" % label, code == 403,
          "实际 %s %s" % (code, body[:70].decode("utf-8", "replace")))

code, body = req("POST", "/api/delete", {"Referer": EVIL + "/x.html"}, {"id": 1})
check("POST /api/delete 仅带外来 Referer → 403", code == 403, "实际 %s" % code)

code, body = req("GET", "/api/list?limit=1", {"Origin": EVIL})
check("GET /api/list 外来 Origin → 403（/api/ 带副作用，一律校验）", code == 403,
      "实际 %s" % code)

code, body = req("POST", "/api/shutdown", {"Origin": EVIL}, {})
check("POST /api/shutdown 外来 Origin → 403（最该拦住的一个）", code == 403,
      "实际 %s" % code)

print("== B. 自己人必须放行（别把正常功能修坏）==")
code, body = req("GET", "/api/list?limit=1", {"Origin": BASE})
check("GET /api/list  Origin=面板自己 → 200", code == 200, "实际 %s" % code)

code, body = req("GET", "/api/stats", {"Origin": BASE, "Referer": BASE + "/"})
check("GET /api/stats Origin+Referer=面板自己 → 200", code == 200, "实际 %s" % code)

code, body = req("GET", "/", {"Origin": EVIL})
check("GET /  外来 Origin → 200（顶层导航豁免：从聊天软件点链接要能进面板）",
      code == 200, "实际 %s" % code)

print("== C. 无来源头必须放行（MCP 直连 / tools 脚本 / curl）==")
code, body = req("GET", "/api/stats")
check("GET /api/stats 不带任何来源头 → 200", code == 200, "实际 %s" % code)

code, body = req("POST", "/api/session/parse", None,
                 {"text": "我：闸门在吗？\nAI：在。\n我：好。\nAI：嗯。"})
check("POST /api/session/parse 不带来源头 → 200（只读接口，不落库）", code == 200,
      "实际 %s" % code)
if code == 200:
    check("  └ 返回体真的是解析结果（不是被挡成空壳）",
          "messages" in json.loads(body.decode("utf-8")), body[:70].decode("utf-8", "replace"))

ok = sum(1 for _, p in results if p)
bad = [n for n, p in results if not p]
print("\n── CSRF 闸门：%d/%d 通过" % (ok, len(results)))
if bad:
    print("   红：%s" % "; ".join(bad))
sys.exit(1 if bad else 0)
