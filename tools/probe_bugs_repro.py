# -*- coding: utf-8 -*-
"""缺陷复现：① 无相关度阈值导致负例误召回 ② memory_save 不校验 type/importance"""
import json
import os
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SERVER = os.path.join(HERE, "loci.py")
_tmp = tempfile.NamedTemporaryFile(prefix="bug_loci_", suffix=".db", delete=False)
_tmp.close()
env = dict(os.environ, LOCI_DB=_tmp.name, PYTHONIOENCODING="utf-8", LOCI_AGENT="bugprobe")
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
    return recv()["result"]["content"][0]["text"]


send({"jsonrpc": "2.0", "id": 1, "method": "initialize",
      "params": {"protocolVersion": "2024-11-05", "capabilities": {}, "clientInfo": {"name": "p", "version": "1"}}})
recv()
send({"jsonrpc": "2.0", "method": "notifications/initialized"})

# 一个和后续查询毫无关系的语料库
seeds = [
    "生图工作台三栏布局：左侧prompt列表、中间生成图、右侧通过判断按钮",
    "缓存策略：写操作先落库再失效，禁止双写造成不一致",
    "训练数据集扩到30条，覆盖异常处理与回归验证场景",
    "漫剧剧本缺口是当前主要卡点，先写剧本再分镜",
]
for s in seeds:
    call("memory_save", {"content": s, "project": "语料"})

print("### 实验一：无关查询是否被误召回（无阈值）")
probes = ["量子引力波探测器", "如何用Rust写一个操作系统内核", "明天北京天气怎么样",
          "红烧肉的家常做法", "美联储加息对汇率的影响"]
hit_total = 0
for q in probes:
    txt = call("memory_search", {"query": q, "limit": 3})
    lines = [l for l in txt.split("\n") if l.startswith("[")]
    hit_total += len(lines)
    print(f"  查询「{q}」 -> 返回 {len(lines)} 条")
    for l in lines:
        print("      " + l[:78])
print(f"  合计：5 个完全无关查询共返回 {hit_total} 条「记忆」（期望 0 条）")

print()
print("### 实验二：同一批无关查询，若加 0.05 相关度门槛")
print("  （项目自带 test_mcp.py 的负例断言用的就是这个 0.05 门槛）")
kept = 0
for q in probes:
    _id[0] += 1
    txt = call("memory_search", {"query": q, "limit": 3})
    for l in [x for x in txt.split("\n") if x.startswith("[")]:
        if float(l.split("]")[0].strip("[")) >= 0.05:
            kept += 1
print(f"  过门槛条数：{kept}（仍是噪声，说明 0.05 也不足以拦住 bigram 误命中）")

print()
print("### 实验三：memory_save 的参数校验缺口")
print("  type 非法值 ->", call("memory_save", {"content": "类型枚举越界测试", "type": "不是合法类型"}))
print("  importance=99 ->", call("memory_save", {"content": "重要度越界测试", "importance": 99}))
print("  importance=-5 ->", call("memory_save", {"content": "负重要度测试", "importance": -5}))
print("  非整数 importance='高' ->", call("memory_save", {"content": "字符串重要度测试", "importance": "高"}))
st = json.loads(call("memory_stats", {}))
print("  memory_stats ->", json.dumps(st, ensure_ascii=False))
print("  ⚠ 检查 by_type 是否出现枚举外的类型：",
      [k for k in st["by_type"] if k not in ("fact", "preference", "context", "decision", "error", "skill", "summary")])
print("  ⚠ 检查 importance 实际落库值：", end=" ")
print(json.loads(call("memory_stats", {}))["total"], "条")

print()
print("### 实验四：memory_search / session_recall 缺 query 的静默行为")
print("  memory_search 无 query ->", repr(call("memory_search", {})))
print("  session_recall 无 query ->", repr(call("session_recall", {})))
print("  对比：memory_save 无 content ->", repr(call("memory_save", {})))

print()
print("### 实验五：无法解析的纯文本被当一条垃圾会话入库")
print("  session_save ->", call("session_save", {"transcript": "```\n根本不是对话的纯文本\n```"}))

proc.stdin.close()
try:
    proc.wait(timeout=10)
except subprocess.TimeoutExpired:
    proc.terminate()
err = proc.stderr.read().decode("utf-8", "replace").strip()
print()
print("服务端 stderr:", err[:600] if err else "（空）")
for suf in ("", "-journal", "-wal", "-shm"):
    try:
        os.remove(_tmp.name + suf)
    except OSError:
        pass
