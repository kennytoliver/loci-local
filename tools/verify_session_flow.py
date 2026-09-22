"""P0 验收：Agent 按「记忆使用约定」归档会话，会话层能真正被写入并切分。

背景：会话层一直是空的（10 个会话里 9 个来自 ZCode，其他 Agent 一个都没写）。
根因是 ~/.agents/AGENTS.md 的约定里漏了 `session_save`，Agent 根本不知道要归档。
补上约定后，必须证明这条约定**真的可执行** —— 即 Agent 用约定里给的格式写 transcript，
能被 parse_transcript 切成多轮（而不是退化成一整条 raw），会话页才显示得出时间线。

本脚本通过真实 MCP 协议（stdin/stdout JSON-RPC）调用，使用独立临时数据库，
不污染真实 hippocampus.db。

零第三方依赖：python tools/verify_session_flow.py
"""
import json
import os
import subprocess
import sys
import tempfile

HERE = r"C:/Users/user/Hippocampus"
_tmp = tempfile.NamedTemporaryFile(prefix="hc_flow_", suffix=".db", delete=False)
_tmp.close()
env = dict(os.environ)
env["HIPPOCAMPUS_DB"] = _tmp.name

proc = subprocess.Popen([sys.executable, "-X", "utf8", os.path.join(HERE, "hippocampus.py")],
                        stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                        stderr=subprocess.PIPE, env=env)
_id = [0]


def call(method, params=None):
    """发一个带 id 的请求并等它对应的响应"""
    _id[0] += 1
    msg = {"jsonrpc": "2.0", "id": _id[0], "method": method}
    if params is not None:
        msg["params"] = params
    proc.stdin.write((json.dumps(msg, ensure_ascii=False) + "\n").encode("utf-8"))
    proc.stdin.flush()
    while True:
        line = proc.stdout.readline().decode("utf-8").strip()
        if not line:
            return None
        r = json.loads(line)
        if r.get("id") == _id[0]:
            return r


def notify(method, params=None):
    """发通知（不带 id）—— 服务端不会回响应，等它就死锁"""
    msg = {"jsonrpc": "2.0", "method": method}
    if params is not None:
        msg["params"] = params
    proc.stdin.write((json.dumps(msg, ensure_ascii=False) + "\n").encode("utf-8"))
    proc.stdin.flush()


def tool(name, **kw):
    r = call("tools/call", {"name": name, "arguments": kw})
    return r["result"]["content"][0]["text"]


results = []


def check(name, cond, detail=""):
    results.append((name, bool(cond)))
    print(f"  {'PASS' if cond else 'FAIL'}  {name}" + (f"   {detail}" if detail else ""))


call("initialize", {"protocolVersion": "2024-11-05", "capabilities": {},
                    "clientInfo": {"name": "flow", "version": "1"}})
notify("notifications/initialized")

print("=" * 66)
print("P0 验收：会话层写入链路")
print("=" * 66)

# ---------- ① 约定里给的格式，能否切成多轮 ----------
print("\n① 按新约定格式归档（我： / AI： 逐轮）")
good = """我：这个面板的会话页为什么一直是空的？
AI：因为约定文件里没有让 Agent 调用 session_save，所以只有 ZCode 在写会话。
我：那怎么修？
AI：给 RULES_BODY 补一条归档约定，再跑 install_agents.py --rules 覆盖到 AGENTS.md。
我：还有其他坑吗？
AI：有。旧的 hippohub 标记匹配不上新标记，会追加出两份重复约定，得一起清理。"""
out = tool("session_save", transcript=good, title="P0 会话层诊断",
           project="Hippocampus", agent="flow-test")
check("归档成功且回报轮数", "已归档会话" in out, out)

# ---------- ② 落库后真的切成多轮（关键）----------
print("\n② 落库后按轮次切分（决定会话页时间线能否显示）")
probe = subprocess.run(
    [sys.executable, "-X", "utf8", "-c",
     "import sys;sys.path.insert(0,r'%s');import hippocampus as h;"
     "s,ms=h.get_session(1);print(len(ms));"
     "print('|'.join(m['role'] for m in ms))" % HERE],
    capture_output=True, text=True, env=env)
lines = (probe.stdout or "").strip().splitlines()
n_msgs = int(lines[0]) if lines and lines[0].isdigit() else -1
roles = lines[1] if len(lines) > 1 else ""
check("消息条数 ≥4（不是 1 条 raw）", n_msgs >= 4, f"实际 {n_msgs} 条")
check("角色交替 user/assistant（时间线可渲染）",
      roles == "user|assistant|user|assistant|user|assistant", roles)

# ---------- ③ 反例：自造人名会退化成 raw ----------
print("\n③ 反例（约定里警告过的那种写法）：用具体人名当前缀")
bad = """建勋：这个怎么修？
COLE：补一条约定就行。"""
tool("session_save", transcript=bad, title="反例-自造人名",
     project="Hippocampus", agent="flow-test")
probe = subprocess.run(
    [sys.executable, "-X", "utf8", "-c",
     "import sys;sys.path.insert(0,r'%s');import hippocampus as h;"
     "s,ms=h.get_session(2);print(len(ms));"
     "print('|'.join(m['role'] for m in ms))" % HERE],
    capture_output=True, text=True, env=env)
lines = (probe.stdout or "").strip().splitlines()
n_bad = int(lines[0]) if lines and lines[0].isdigit() else -1
roles_bad = lines[1] if len(lines) > 1 else ""
check("自造人名 → 退化成 1 条 raw（所以约定必须禁止）", n_bad == 1, f"{n_bad} 条 / 角色={roles_bad}")

# ---------- ④ 指纹去重 ----------
print("\n④ 同一段对话重复归档")
out2 = tool("session_save", transcript=good, title="P0 会话层诊断",
            project="Hippocampus", agent="flow-test")
check("重复归档被指纹拦截，不写重", "已归档过" in out2, out2)

# ---------- ⑤ 归档后能被检索到 ----------
print("\n⑤ 归档后能被 session_recall 检索（跨 Agent 复现原话）")
rec = tool("session_recall", query="会话页为什么是空的", limit=3)
check("能搜到刚归档的原话", "空的" in rec, rec[:70].replace("\n", " "))

# ---------- ⑥ 会话出现在列表里 ----------
print("\n⑥ 会话出现在会话列表（面板第一落点）")
lst = tool("memory_list", limit=1)  # 冒烟：确认服务没挂
probe = subprocess.run(
    [sys.executable, "-X", "utf8", "-c",
     "import sys;sys.path.insert(0,r'%s');import hippocampus as h;"
     "rs=h.list_sessions();print(len(rs));"
     "print(rs[0]['title'],'|mem',rs[0].get('mem_count'))" % HERE],
    capture_output=True, text=True, env=env)
lines = (probe.stdout or "").strip().splitlines()
n_sess = int(lines[0]) if lines and lines[0].isdigit() else -1
check("会话列表能列出归档的会话", n_sess >= 2, f"{n_sess} 个会话；{lines[1] if len(lines)>1 else ''}")

proc.terminate()
try:
    os.unlink(_tmp.name)
except OSError:
    pass

print()
ok = all(c for _, c in results)
print(f"合计 {len(results)} 项，失败 {sum(1 for _, c in results if not c)}")
print("结论: " + ("P0 验收通过 ✅ 约定生效，Agent 归档的会话能切成多轮"
                 if ok else "有失败项 ❌"))
sys.exit(0 if ok else 1)
