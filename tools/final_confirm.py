# -*- coding: utf-8 -*-
"""最终确认轮：6 项复测 + 2 项核对

- 项 1 / 5 走 MCP 协议（独立临时库）
- 项 2 / 3 走面板 HTTP（对着测试端口）
- 项 4 走面板 HTTP（空库场景，需要另起一个空库面板）
- 项 6 由 driver 单独跑 verify_panel_api
每项都打印**实际原始输出**，不做加工。
"""
import json
import os
import subprocess
import sys
import tempfile
import urllib.error
import urllib.request

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SERVER = os.path.join(HERE, "loci.py")
PANEL = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8799"
R = []


def check(n, ok, raw):
    R.append((n, bool(ok), raw))


# ================= 项 1：memory_save 的 type 校验（MCP 协议） =================
print("=" * 78)
print("项 1：memory_save type 校验（独立临时库，MCP 协议）")
print("=" * 78)
DB = tempfile.NamedTemporaryFile(prefix="final_", suffix=".db", delete=False).name
env = dict(os.environ, LOCI_DB=DB, PYTHONIOENCODING="utf-8", LOCI_AGENT="final")
proc = subprocess.Popen([sys.executable, "-X", "utf8", SERVER], stdin=subprocess.PIPE,
                        stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env)
_id = [0]


def send(o):
    proc.stdin.write((json.dumps(o, ensure_ascii=False) + "\n").encode("utf-8"))
    proc.stdin.flush()


def recv():
    line = proc.stdout.readline().decode("utf-8").strip()
    return json.loads(line) if line else None


def call(name, args):
    _id[0] += 1
    send({"jsonrpc": "2.0", "id": _id[0], "method": "tools/call",
          "params": {"name": name, "arguments": args}})
    r = recv()
    res = (r or {}).get("result", {})
    return bool(res.get("isError")), res.get("content", [{}])[0].get("text", "")


send({"jsonrpc": "2.0", "id": 1, "method": "initialize",
      "params": {"protocolVersion": "2024-11-05", "capabilities": {},
                 "clientInfo": {"name": "final", "version": "1"}}})
recv()
send({"jsonrpc": "2.0", "method": "notifications/initialized"})

CASES = [
    ('type=""', {"content": "空类型", "type": ""}, True),
    ('type="   "', {"content": "空白类型", "type": "   "}, True),
    ('type=null', {"content": "null 类型", "type": None}, True),
    ('type="不是合法类型"', {"content": "非法类型", "type": "不是合法类型"}, True),
    ('不传 type（应默认 fact）', {"content": "不传类型"}, False),
    ('type="decision"', {"content": "合法决策", "type": "decision"}, False),
    ('type="fact"', {"content": "合法事实", "type": "fact"}, False),
    ('importance=99', {"content": "越界重要度", "importance": 99}, True),
    ('importance=0', {"content": "零重要度", "importance": 0}, True),
    ('importance="高"', {"content": "字符串重要度", "importance": "高"}, True),
    ('importance=1', {"content": "边界1", "importance": 1}, False),
    ('importance=4', {"content": "边界4", "importance": 4}, False),
]
for label, args, expect_err in CASES:
    is_err, txt = call("memory_save", args)
    ok = (is_err is expect_err)
    print(f"  {'PASS' if ok else 'FAIL'}  {label:<26} isError={is_err}  {txt[:76]}")
    check(f"项1 {label}", ok, f"isError={is_err} :: {txt}")

# 核对：不传 type 那条入库类型确实是 fact；空类型那条没进库
_, st = call("memory_stats", {})
by_type = json.loads(st)["by_type"]
print(f"  入库类型分布: {by_type}")
ok = by_type == {"fact": 6, "decision": 1}
check("项1 合法写入的类型分布正确（fact×6 + decision×1）", ok, json.dumps(by_type, ensure_ascii=False))
proc.stdin.close()
proc.wait(timeout=15)
for s in ("", "-journal", "-wal", "-shm"):
    try:
        os.remove(DB + s)
    except OSError:
        pass

# ================= 项 5：delete_sessions_bulk =================
print()
print("=" * 78)
print("项 5：delete_sessions_bulk 传不存在的 id（库层直调）")
print("=" * 78)
DB2 = tempfile.NamedTemporaryFile(prefix="final2_", suffix=".db", delete=False).name
env2 = dict(os.environ, LOCI_DB=DB2, PYTHONIOENCODING="utf-8")
code = (
    "import sys;sys.path.insert(0,r'%s')\n"
    "import loci\n"
    "print('  delete_sessions_bulk([99999998, 99999999]) ->', loci.delete_sessions_bulk([99999998, 99999999]))\n"
    "print('  delete_sessions_bulk([]) ->', loci.delete_sessions_bulk([]))\n"
    "sid,_ = loci.save_session('边界测试会话', 'P', 'final', [{'role':'user','content':'我: 你好'},{'role':'assistant','content':'AI: 你好'}])\n"
    "print('  真实会话 id =', sid)\n"
    "print('  删已存在的 ->', loci.delete_sessions_bulk([sid]))\n"
    "print('  再删一次（已不存在）->', loci.delete_sessions_bulk([sid]))\n"
    "print('  混合 [存在 + 不存在] -> 先建再测')\n"
    "sid2,_ = loci.save_session('第二个会话', 'P', 'final', [{'role':'user','content':'我: 二'},{'role':'assistant','content':'AI: 二'}])\n"
    "print('    建 sid2 =', sid2, '| 删 [sid2, 99999999] ->', loci.delete_sessions_bulk([sid2, 99999999]))\n"
) % HERE.replace("\\", "\\\\")
p = subprocess.run([sys.executable, "-X", "utf8", "-c", code], env=env2, capture_output=True)
out = (p.stdout + p.stderr).decode("utf-8", "replace")
print(out.rstrip())
check("项5 不存在的 id -> deleted 0", "{'deleted': 0}" in out, out)
check("项5 混合删 -> 只计真实删除数 1", "'deleted': 1" in out, out)
for s in ("", "-journal", "-wal", "-shm"):
    try:
        os.remove(DB2 + s)
    except OSError:
        pass

# ================= 项 2 / 3：面板 /api/save =================
print()
print("=" * 78)
print(f"项 2 / 3：面板 /api/save 校验与正常路径  BASE={PANEL}")
print("=" * 78)


def post(path, payload):
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    r = urllib.request.Request(PANEL + path, data=data, method="POST")
    r.add_header("Content-Type", "application/json")
    r.add_header("Origin", PANEL)
    try:
        with urllib.request.urlopen(r, timeout=20) as x:
            return x.status, x.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8", "replace")
    except Exception as e:
        return -1, f"{type(e).__name__}: {e}"


def get(path):
    try:
        with urllib.request.urlopen(PANEL + path, timeout=25) as x:
            return x.status, x.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8", "replace")
    except Exception as e:
        return -1, f"{type(e).__name__}: {e}"


PANEL_CASES = [
    ("项2 非法 mtype", {"content": "__最终确认__非法类型", "mtype": "乱写类型"}, True),
    ("项2 空 mtype", {"content": "__最终确认__空类型", "mtype": ""}, True),
    ("项2 importance=99", {"content": "__最终确认__越界", "mtype": "fact", "importance": 99}, True),
    ("项2 importance=-5", {"content": "__最终确认__负值", "mtype": "fact", "importance": -5}, True),
    ("项3 正常：下拉选 decision", {"content": "__最终确认__正常路径", "mtype": "decision", "importance": 3}, False),
    ("项3 正常：默认 fact", {"content": "__最终确认__默认类型", "mtype": "fact", "importance": 2}, False),
]
created_ids = []
for label, payload, expect_err in PANEL_CASES:
    code, body = post("/api/save", payload)
    if expect_err:
        try:
            j = json.loads(body)
            got_err = (j.get("ok") is False) and bool(j.get("error"))
        except Exception:
            got_err = False
        ok = code == 200 and got_err
    else:
        try:
            j = json.loads(body)
            got_err = "id" in j
            if got_err and isinstance(j.get("id"), int):
                created_ids.append(j["id"])
        except Exception:
            got_err = False
        ok = code == 200 and got_err
    print(f"  {'PASS' if ok else 'FAIL'}  {label:<26} HTTP {code} :: {body[:88]}")
    check(label, ok, f"HTTP {code} :: {body}")

# 核对：正常路径那条的 mtype 真是 decision
code, body = get("/api/list?limit=-1")
try:
    rows = json.loads(body)
    saved = [r for r in rows if r.get("content") == "__最终确认__正常路径"]
    mtype_ok = bool(saved) and saved[0].get("mtype") == "decision"
    print(f"  入库核对: {[(r['id'], r['mtype'], r['importance']) for r in saved]}")
    check("项3 正常路径入库为 decision/★3", mtype_ok, json.dumps([(r["id"], r["mtype"], r["importance"]) for r in saved], ensure_ascii=False))
    # 非法那几条不应入库
    bad = [r for r in rows if r.get("content", "").startswith("__最终确认__") and r.get("content") != "__最终确认__正常路径" and r.get("content") != "__最终确认__默认类型"]
    print(f"  非法请求的残留: {[(r['id'], r['content']) for r in bad]}")
    check("项2 非法请求均未入库", not bad, json.dumps([(r["id"], r["content"]) for r in bad], ensure_ascii=False))
except Exception as e:
    check("项3 入库核对", False, f"{e}: {body[:200]}")

# 清理我这轮写进面板的合法记录（按精确内容）
print("  --- 清理本轮写入的面板测试记录 ---")
code, body = get("/api/list?limit=-1")
try:
    rows = json.loads(body)
    mine = [r["id"] for r in rows if str(r.get("content", "")).startswith("__最终确认__")]
    for i in mine:
        c, b = post("/api/delete", {"id": i})
        print(f"    删除 #{i} -> HTTP {c} {b[:50]}")
    check("本轮面板测试记录已清理", True, f"清理了 {mine}")
except Exception as e:
    check("清理面板测试记录", False, str(e))

# ================= 汇总 =================
print()
print("=" * 78)
fails = [n for n, ok, _ in R if not ok]
for n, ok, raw in R:
    print(f"[{'PASS' if ok else 'FAIL'}] {n}")
    if not ok:
        print(f"       {str(raw)[:200]}")
print("=" * 78)
print(f"合计 {len(R)} 项，FAIL {len(fails)} 项" + (f" -> {fails}" if fails else ""))
sys.exit(1 if fails else 0)
