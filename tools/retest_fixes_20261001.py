# -*- coding: utf-8 -*-
"""独立复测：开发侧《缺陷修复交付报告-20261001》声称的修复是否真的成立。

不复用开发侧的测试脚本，用独立临时库（LOCI_DB 指向临时文件）直接走 MCP 协议。
"""
import json
import os
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SERVER = os.path.join(HERE, "loci.py")
DB = tempfile.NamedTemporaryFile(prefix="retest_", suffix=".db", delete=False).name
env = dict(os.environ, LOCI_DB=DB, PYTHONIOENCODING="utf-8", LOCI_AGENT="retest")
proc = subprocess.Popen([sys.executable, "-X", "utf8", SERVER], stdin=subprocess.PIPE,
                        stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env)
_id = [0]
R = []


def send(o):
    proc.stdin.write((json.dumps(o, ensure_ascii=False) + "\n").encode("utf-8"))
    proc.stdin.flush()


def recv():
    line = proc.stdout.readline().decode("utf-8").strip()
    return json.loads(line) if line else None


def call(name, args):
    """返回 (isError, text)"""
    _id[0] += 1
    send({"jsonrpc": "2.0", "id": _id[0], "method": "tools/call",
          "params": {"name": name, "arguments": args}})
    r = recv()
    if r is None:
        return None, "<无响应>"
    res = r.get("result", {})
    return bool(res.get("isError")), res.get("content", [{}])[0].get("text", "")


def check(name, ok, detail=""):
    R.append((name, bool(ok), str(detail)[:150]))


send({"jsonrpc": "2.0", "id": 1, "method": "initialize",
      "params": {"protocolVersion": "2024-11-05", "capabilities": {},
                 "clientInfo": {"name": "retest", "version": "1"}}})
recv()
send({"jsonrpc": "2.0", "method": "notifications/initialized"})


def count_memories():
    _, t = call("memory_stats", {})
    try:
        return json.loads(t)["total"]
    except Exception:
        return -1


def count_sessions():
    _, t = call("memory_stats", {})
    try:
        return json.loads(t)["sessions"]
    except Exception:
        return -1


# ---------- 复测项 1：不存在 id 的 5 个操作必须失败 ----------
print("### 复测项 1：不存在 id 上的 delete / pin（MCP 层应 isError=True）")
before = count_memories()
for tool, args in (("memory_delete", {"id": 999999}), ("memory_pin", {"id": 999999})):
    is_err, txt = call(tool, args)
    check(f"{tool} 对不存在 id 报错", is_err is True, f"isError={is_err} :: {txt}")

print("### 复测项 1b：retire / touch / supersede（库层应 ok=false）")
sys.path.insert(0, HERE)
import loci  # noqa: E402
os.environ["LOCI_DB"] = DB
for fn, args in (("retire_memory", (999999,)), ("touch_memory", (999999,)),
                 ("supersede_memory", (999999, 1))):
    try:
        r = getattr(loci, fn)(*args)
        if isinstance(r, dict):
            check(f"loci.{fn} 对不存在 id 返回 ok=false", r.get("ok") is False, json.dumps(r, ensure_ascii=False))
        else:
            check(f"loci.{fn} 对不存在 id 返回 0", r == 0, f"返回 {r!r}")
    except Exception as e:
        check(f"loci.{fn} 对不存在 id 报错", True, f"抛异常: {type(e).__name__}: {e}")

# ---------- 复测项 2：memory_save 非法参数必须报错且不落库 ----------
print("### 复测项 2：memory_save 非法 type / importance（应报错且库里不新增）")
base = count_memories()
for label, args in (('type="不是合法类型"', {"content": "非法类型复测", "type": "不是合法类型"}),
                    ('type=""', {"content": "空类型复测", "type": ""}),
                    ('importance=99', {"content": "越界重要度复测", "importance": 99}),
                    ('importance=-5', {"content": "负重要度复测", "importance": -5}),
                    ('importance="高"', {"content": "非整数重要度复测", "importance": "高"})):
    is_err, txt = call("memory_save", args)
    check(f"memory_save {label} 报错", is_err is True, f"isError={is_err} :: {txt}")
    # 合法路径不应被误伤

is_err, txt = call("memory_save", {"content": "合法写入对照", "type": "decision", "importance": 4})
check("memory_save 合法参数仍成功", is_err is False, f"isError={is_err} :: {txt}")
after = count_memories()
check("非法参数未落库（只多了那 1 条合法对照）", after == base + 1,
      f"base={base} after={after}（差 {after-base}，应为 1）")

# 边界：importance=1 和 4 应合法
for v in (1, 4):
    is_err, txt = call("memory_save", {"content": f"边界重要度{v}", "importance": v})
    check(f"memory_save importance={v} 合法", is_err is False, f"isError={is_err}")

# ---------- 复测项 3：session_save 纯文本必须被拒 ----------
print("### 复测项 3：session_save 非对话纯文本（应报错且会话数不变）")
s_before = count_sessions()
for label, tr in (("代码围栏纯文本", "```\n根本不是对话的纯文本\n```"),
                  ("单行说明文字", "这是一段没有任何角色标记的说明文字"),
                  ("空行分隔文档", "第一段\n\n第二段\n\n第三段")):
    is_err, txt = call("session_save", {"transcript": tr})
    check(f"session_save 拒绝「{label}」", is_err is True, f"isError={is_err} :: {txt}")
s_after = count_sessions()
check("被拒文本未产生会话", s_after == s_before, f"before={s_before} after={s_after}")

# 合法对话仍应能归档
is_err, txt = call("session_save", {"transcript": "我: 缓存怎么定？\nAI: 先落库再失效", "title": "复测"})
check("session_save 合法对话仍成功", is_err is False, f"isError={is_err} :: {txt}")

# ---------- 复测项 4：memory_search / session_recall 缺 query ----------
print("### 复测项 4：缺 query 应报错；空串仍按'查了没有'处理")
for tool in ("memory_search", "session_recall"):
    is_err, txt = call(tool, {})
    check(f"{tool} 缺 query 报错", is_err is True, f"isError={is_err} :: {txt}")
    is_err2, txt2 = call(tool, {"query": ""})
    check(f"{tool} 空串 query 不报错（刻意设计）", is_err2 is False, f"isError={is_err2} :: {txt2}")
    is_err3, txt3 = call(tool, {"query": "缓存"})
    check(f"{tool} 正常 query 仍可用", is_err3 is False, f"isError={is_err3} :: {txt3[:60]}")

# ---------- 复测项 5：_clamp_limit 边界 ----------
print("### 复测项 5：limit 收口（-1 / 0 / 超大 不应返回全表或报错）")
sys.path.insert(0, HERE)
cl = getattr(loci, "_clamp_limit", None)
check("_clamp_limit 存在", cl is not None, f"{cl}")
if cl:
    for v, exp_lo in ((-1, 1), (0, 1), (99999, 1000), ("abc", 1), (None, 1)):
        try:
            got = cl(v, 20)
            check(f"_clamp_limit({v!r}, 20) 在 [1,1000] 内", isinstance(got, int) and 1 <= got <= 1000,
                  f"返回 {got}")
        except Exception as e:
            check(f"_clamp_limit({v!r}, 20) 不抛异常", False, f"{type(e).__name__}: {e}")

is_err, txt = call("memory_list", {"limit": -1})
check("memory_list limit=-1 不返回全表/不报错", is_err is False, f"返回 {len(txt.splitlines())} 行")

# ---------- 收尾 ----------
proc.stdin.close()
try:
    proc.wait(timeout=10)
except subprocess.TimeoutExpired:
    proc.terminate()
err = proc.stderr.read().decode("utf-8", "replace").strip()

print()
print("=" * 76)
fails = [(n, d) for n, ok, d in R if not ok]
for n, ok, d in R:
    print(f"[{'PASS' if ok else 'FAIL'}] {n}")
    if not ok and d:
        print(f"       {d}")
print("=" * 76)
print(f"合计 {len(R)} 项，FAIL {len(fails)} 项")
print("服务端 stderr:", (err[:400] if err else "（空）"))

for suf in ("", "-journal", "-wal", "-shm"):
    try:
        os.remove(DB + suf)
    except OSError:
        pass
