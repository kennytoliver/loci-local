# -*- coding: utf-8 -*-
"""POST 接口健壮性探测：只打不会真正改数据的路径（错误输入应被拦住）。

⚠️ 例外：末尾的 CSRF 一段**会真的尝试写一条哨兵记忆**（那是它的验证目的）。
   所以：
     · 请对着**测试面板**跑，别打正在用的生产面板；跑完它会自己按精确内容删掉哨兵；
     · 若面板连的是真实库，那条哨兵会短暂出现在真库里（脚本会打印清理结果）。
   用法：python tools/probe_panel_post.py http://127.0.0.1:8799
"""
import json
import sys
import urllib.error
import urllib.request
import uuid

BASE = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8799"


def post(path, payload, raw=None):
    data = (raw if raw is not None else json.dumps(payload, ensure_ascii=False)).encode("utf-8")
    req = urllib.request.Request(BASE + path, data=data, method="POST")
    req.add_header("Content-Type", "application/json")
    req.add_header("Origin", BASE)
    try:
        with urllib.request.urlopen(req, timeout=25) as r:
            return r.status, r.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8", "replace")
    except Exception as e:
        return -1, f"{type(e).__name__}: {e}"


# 这些全部应当被「干净地拒绝」（返回 4xx/JSON error），而不是断开连接
CASES = [
    ("/api/save 空内容", "/api/save", {"content": "   "}),
    ("/api/save 缺 content", "/api/save", {}),
    ("/api/delete 缺 id", "/api/delete", {}),
    ("/api/delete id 非数字", "/api/delete", {"id": "abc"}),
    ("/api/pin 缺 id", "/api/pin", {}),
    ("/api/pin id 非数字", "/api/pin", {"id": "xyz"}),
    ("/api/retire 缺 id", "/api/retire", {}),
    ("/api/touch 缺 id", "/api/touch", {}),
    ("/api/supersede 缺字段", "/api/supersede", {}),
    ("/api/split 缺 id", "/api/split", {}),
    ("/api/merge 空 ids", "/api/merge", {"ids": []}),
    ("/api/session/delete 缺 sid", "/api/session/delete", {}),
    ("/api/session/save 空", "/api/session/save", {"text": "   "}),
    ("/api/collect paths 非数组", "/api/collect", {"paths": "notalist"}),
    ("坏 JSON body", "/api/save", None),
]

print("=" * 74)
print("POST 健壮性（期望：全部被干净拒绝，不应出现 -1 连接被掐断）")
broken = []
for name, path, payload in CASES:
    if name == "坏 JSON body":
        code, body = post(path, None, raw="{ 这不是 JSON")
    else:
        code, body = post(path, payload)
    status = "OK" if code not in (-1,) else "❌连接断"
    print(f"[{status}] {name} -> HTTP {code} :: {body[:90].replace(chr(10),' ')}")
    if code == -1:
        broken.append(name)

print("=" * 74)
# CSRF：无 Origin / 外部 Origin 的写请求应被拒
#
# ⚠️ 2026-10-01 修：这一段以前**只负责写、不负责清**，正文是一个固定字符串
#    「CSRF 测试（不应写入）」。如果面板当时连的是真实库（常见的 8787，或本脚本默认
#    的 8799 但面板没设 LOCI_DB），"无 Origin"那一次会被放行并**真的落库** ——
#    于是每次跑这个探针都在真库里堆一条垃圾。实测真库因此堆了 3 条（#160/#161/#162，
#    其中 #162 是 2026-10-01 19:42 写的）。
#    现在：① 用带随机后缀的哨兵，避免与历史垃圾混淆；② 跑完按内容精确查 id 再删干净；
#    ③ 清理失败要吵出来，不静默。
SENTINEL = "CSRF 探针哨兵_应被清理_" + uuid.uuid4().hex[:8]


def _find_ids_by_content(text):
    """按精确内容查 id —— 不用模糊匹配，避免误删别的记忆。"""
    try:
        with urllib.request.urlopen(BASE + "/api/list?limit=-1", timeout=20) as r:
            rows = json.loads(r.read().decode("utf-8", "replace"))
        return [x["id"] for x in rows if x.get("content") == text]
    except Exception as e:
        print(f"  （查哨兵失败: {type(e).__name__}: {e}）")
        return []


csrf_results = []
for label, origin in (("外部 Origin", "http://evil.example.com"), ("无 Origin", None)):
    data = json.dumps({"content": SENTINEL}, ensure_ascii=False).encode()
    req = urllib.request.Request(BASE + "/api/save", data=data, method="POST")
    req.add_header("Content-Type", "application/json")
    if origin:
        req.add_header("Origin", origin)
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            print(f"CSRF {label} 写 -> HTTP {r.status} {'⚠ 未拦截（已写入，稍后清理）' if r.status == 200 else ''}")
            csrf_results.append((label, r.status))
    except urllib.error.HTTPError as e:
        print(f"CSRF {label} 写 -> HTTP {e.code} {'PASS 已拦截' if e.code in (403, 400) else ''}")
        csrf_results.append((label, e.code))
    except Exception as e:
        print(f"CSRF {label} 写 -> {type(e).__name__}: {e}")
        csrf_results.append((label, -1))

# ---- 清理：把这次探针写进去的哨兵删掉 ----
left = _find_ids_by_content(SENTINEL)
if left:
    print(f"\n[清理] 哨兵已落库（id={left}）—— 逐个删除")
    for i in left:
        try:
            d = json.dumps({"id": i}).encode()
            rq = urllib.request.Request(BASE + "/api/delete", data=d, method="POST")
            rq.add_header("Content-Type", "application/json")
            with urllib.request.urlopen(rq, timeout=15) as r:
                body = r.read().decode("utf-8", "replace")
            print(f"   删除 #{i} -> HTTP {r.status} :: {body[:60]}")
        except Exception as e:
            print(f"   删除 #{i} 失败: {type(e).__name__}: {e}")
    still = _find_ids_by_content(SENTINEL)
    if still:
        print(f"   ❌ 仍有残留 {still} —— 面板的 /api/delete 是软删除，"
              f"这些行仍在库里（deleted=1）。请到面板手动清理，或直接操作 SQLite。")
    else:
        print("   ✅ 哨兵已清理（软删除生效）")
else:
    print("\n[清理] 无需清理（探针没有写进任何内容）")

print()
print("断连项：", broken if broken else "无")
