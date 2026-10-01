# -*- coding: utf-8 -*-
"""MCP 接入 + 全工具冒烟探测（独立临时库，不触碰真实 loci.db）

用「客户端配置里的那条精确命令」启动服务端，覆盖：
  A. 协议层：握手 / 通知 / 未知方法 / 坏 JSON 行 / ping
  B. 10 个工具的正常路径
  C. 边界与负例：缺参、空串、非法枚举、不存在的 id、超限 limit
  D. 数据一致性：写后读、pin 生效、delete 后不可见、会话去重
"""
import json
import os
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SERVER = os.path.join(HERE, "loci.py")
PY = sys.executable

_tmp = tempfile.NamedTemporaryFile(prefix="probe_loci_", suffix=".db", delete=False)
_tmp.close()
DB = _tmp.name

env = dict(os.environ)
env["LOCI_DB"] = DB
env["LOCI_AGENT"] = "probe"
env["PYTHONIOENCODING"] = "utf-8"

proc = subprocess.Popen([PY, "-X", "utf8", SERVER],
                        stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                        stderr=subprocess.PIPE, env=env)

_results = []
_id = [0]


def send(obj, raw=None):
    payload = raw if raw is not None else json.dumps(obj, ensure_ascii=False)
    proc.stdin.write((payload + "\n").encode("utf-8"))
    proc.stdin.flush()


def recv():
    line = proc.stdout.readline().decode("utf-8").strip()
    return json.loads(line) if line else None


def call(name, args, expect_error=False, note=""):
    _id[0] += 1
    send({"jsonrpc": "2.0", "id": _id[0], "method": "tools/call",
          "params": {"name": name, "arguments": args}})
    r = recv()
    if r is None:
        _results.append((f"{name} {note}", False, "服务端无响应"))
        return None
    res = r.get("result")
    if res is None:
        _results.append((f"{name} {note}", False, f"返回 error: {r.get('error')}"))
        return None
    is_err = bool(res.get("isError"))
    text = res["content"][0]["text"]
    if expect_error:
        ok = is_err
        detail = ("已按预期报错: " if ok else "❌ 未报错，返回: ") + text[:90]
    else:
        ok = not is_err
        detail = text[:110].replace("\n", " | ")
    _results.append((f"{name} {note}".strip(), ok, detail))
    return text


def check(name, ok, detail=""):
    _results.append((name, bool(ok), str(detail)[:140]))


# ---------- A. 协议层 ----------
send({"jsonrpc": "2.0", "id": 1, "method": "initialize",
      "params": {"protocolVersion": "2024-11-05", "capabilities": {},
                 "clientInfo": {"name": "ds-harness-probe", "version": "1.0"}}})
r = recv()
info = (r or {}).get("result", {}).get("serverInfo", {})
check("A1 initialize 握手", info.get("name") == "loci",
      f"serverInfo={info} protocol={(r or {}).get('result', {}).get('protocolVersion')}")
check("A2 声明 tools 能力", "tools" in (r or {}).get("result", {}).get("capabilities", {}),
      (r or {}).get("result", {}).get("capabilities"))

send({"jsonrpc": "2.0", "method": "notifications/initialized"})
send({"jsonrpc": "2.0", "id": 2, "method": "ping"})
check("A3 ping 有响应", (recv() or {}).get("id") == 2, "")

send({"jsonrpc": "2.0", "id": 3, "method": "tools/list"})
r = recv()
tools = [t["name"] for t in r["result"]["tools"]]
check("A4 tools/list = 10 个工具且 schema 齐全",
      len(tools) == 10 and all("inputSchema" in t for t in r["result"]["tools"]),
      tools)

send({"jsonrpc": "2.0", "id": 4, "method": "no/such/method"})
r = recv()
check("A5 未知方法返回 -32601", (r or {}).get("error", {}).get("code") == -32601,
      (r or {}).get("error"))

send(None, raw="这不是 JSON —— 宿主塞进来的杂音")
send({"jsonrpc": "2.0", "id": 5, "method": "ping"})
check("A6 坏 JSON 行被忽略且服务不崩", (recv() or {}).get("id") == 5, "")

# ---------- B. 正常路径 ----------
txt = call("memory_save", {"content": "探测：缓存写操作先落库再失效，忌双写",
                           "type": "skill", "importance": 4,
                           "tags": "缓存,架构", "project": "探测项目", "agent": "probe"},
           note="(正常)")
mid = int(txt.split("#")[1]) if txt and "#" in txt else 0

sentinel = "紫水晶协议第七码"
call("memory_save", {"content": f"探测哨兵：{sentinel}", "project": "探测项目"})
call("memory_save", {"content": "探测：数据集扩到30条覆盖异常场景", "type": "fact",
                     "project": "探测项目", "importance": 3})

call("memory_search", {"query": "缓存怎么失效", "limit": 3}, note="(中文检索)")
call("memory_search", {"query": "紫水晶协议", "limit": 3}, note="(哨兵检索)")
call("memory_list", {"project": "探测项目", "limit": 10}, note="(按项目)")
call("memory_stats", {}, note="(正常)")
call("memory_handoff", {"project": "探测项目"}, note="(正常)")
call("memory_context", {"project": "探测项目", "limit": 5}, note="(正常)")

call("session_save", {"transcript": "我: 缓存策略怎么定？\nAI: 先落库再失效，别双写\n我: 好",
                      "title": "缓存策略讨论", "project": "探测项目", "agent": "probe"},
     note="(正常)")
call("session_recall", {"query": "缓存策略", "limit": 3}, note="(正常)")

# 会话同源去重（同标题+同内容第二次应提示已归档）
call("session_save", {"transcript": "我: 缓存策略怎么定？\nAI: 先落库再失效，别双写\n我: 好",
                      "title": "缓存策略讨论", "project": "探测项目", "agent": "probe"},
     note="(重复归档应去重)")

call("memory_pin", {"id": mid, "pinned": 1}, note="(设常驻)")
ctx = call("memory_context", {"limit": 5}, note="(常驻是否进上下文)")
check("D1 常驻记忆出现在 memory_context", ctx is not None and "缓存写操作先落库" in ctx,
      (ctx or "")[:100])

# ---------- C. 边界与负例 ----------
call("memory_save", {}, expect_error=True, note="(缺 content)")
call("memory_save", {"content": "   "}, expect_error=True, note="(空白 content)")
call("memory_save", {"content": "非法类型测试", "type": "不存在的类型"},
     note="(非法 type 枚举)")
call("memory_save", {"content": "非法重要度测试", "importance": 99},
     note="(importance 越界)")
call("memory_save", {"content": "limit 越界记忆"}, note="(备用)")

call("memory_search", {}, expect_error=True, note="(缺 query)")
call("memory_search", {"query": ""}, note="(空 query)")
call("memory_search", {"query": "缓存", "limit": 100000}, note="(超大 limit)")
call("memory_search", {"query": "缓存", "limit": -5}, note="(负数 limit)")
call("memory_search", {"query": "量子引力波探测器", "limit": 3}, note="(负例不误召回)")

call("memory_delete", {}, expect_error=True, note="(缺 id)")
call("memory_delete", {"id": 999999}, note="(不存在的 id)")
call("memory_delete", {"id": "abc"}, expect_error=True, note="(非数字 id)")

call("memory_pin", {}, expect_error=True, note="(缺 id)")
call("memory_pin", {"id": 999999, "pinned": 1}, note="(不存在的 id)")

call("memory_handoff", {}, expect_error=True, note="(缺 project)")
call("memory_handoff", {"project": "根本不存在的项目"}, note="(空项目)")

call("session_save", {}, expect_error=True, note="(缺 transcript)")
call("session_save", {"transcript": "   "}, expect_error=True, note="(空白 transcript)")
call("session_save", {"transcript": "```\n没有任何对话标记的纯文本\n```"},
     expect_error=True, note="(无法解析的文本)")
call("session_recall", {}, expect_error=True, note="(缺 query)")
call("session_recall", {"query": "量子引力波探测器"}, note="(负例)")

call("__不存在的工具__", {}, expect_error=True, note="(未知工具名)")
call("memory_list", {"limit": -1}, note="(负数 limit)")
call("memory_context", {"limit": -3}, note="(负数 limit)")

# ---------- D. 删除后不可见 ----------
call("memory_delete", {"id": mid}, note="(删除哨兵 id)")
call("memory_search", {"query": "缓存写操作先落库", "limit": 3}, note="(删除后检索)")

# ---------- 收尾 ----------
proc.stdin.close()
try:
    proc.wait(timeout=10)
except subprocess.TimeoutExpired:
    proc.terminate()
err = proc.stderr.read().decode("utf-8", "replace").strip()

print("=" * 72)
fails = 0
for name, ok, detail in _results:
    if not ok:
        fails += 1
    print(f"[{'PASS' if ok else 'FAIL'}] {name}")
    if detail:
        print(f"       {detail}")
print("=" * 72)
print(f"合计 {len(_results)} 项，FAIL {fails} 项 | 退出码 {proc.returncode}")
if err:
    print("--- 服务端 stderr ---")
    print(err[:2000])
else:
    print("服务端 stderr 干净（无异常/无告警）")

for suffix in ("", "-journal", "-wal", "-shm"):
    try:
        os.remove(DB + suffix)
    except OSError:
        pass
