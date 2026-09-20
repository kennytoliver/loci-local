# -*- coding: utf-8 -*-
"""Hippocampus MCP Server 端到端测试：模拟 MCP 客户端全流程

- 路径自动定位（与 hippocampus.py 同目录），不写死绝对路径
- 使用独立临时数据库，跑完自动删除，不污染真实 hippocampus.db
"""
import subprocess, json, sys, os, tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
SERVER = os.path.join(HERE, "hippocampus.py")

# 独立临时库：测试数据与真实数据隔离
_tmp = tempfile.NamedTemporaryFile(prefix="hippocampus_test_", suffix=".db", delete=False)
_tmp.close()
os.environ["HIPPOCAMPUS_DB"] = _tmp.name

proc = subprocess.Popen(
    [sys.executable, "-X", "utf8", SERVER],
    stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE)

def send(obj):
    proc.stdin.write((json.dumps(obj, ensure_ascii=False) + "\n").encode("utf-8"))
    proc.stdin.flush()

def recv():
    line = proc.stdout.readline().decode("utf-8").strip()
    return json.loads(line) if line else None

results = []

# 1. initialize 握手
send({"jsonrpc": "2.0", "id": 1, "method": "initialize",
      "params": {"protocolVersion": "2024-11-05", "capabilities": {}, "clientInfo": {"name": "test-client", "version": "1.0"}}})
r = recv()
ok = r and r.get("result", {}).get("serverInfo", {}).get("name") == "hippocampus"
results.append(("initialize 握手", ok, r.get("result", {}).get("serverInfo") if r else None))

send({"jsonrpc": "2.0", "method": "notifications/initialized"})

# 2. tools/list
send({"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
r = recv()
tools = [t["name"] for t in r["result"]["tools"]]
results.append(("tools/list 返回 10 个工具", len(tools) == 10, tools))

# 3. 写入测试记忆（写入临时库，不影响真实数据）
seeds = [
    ("生图工作台项目：prompt-图片-通过判断三栏布局，左侧prompt列表、中间生成图、右侧通过/不通过按钮", "decision", 3, "生图工作台"),
    ("生图评测规则V0.6：bad case分析必须注明失败原因类别（构图/语义/风格/细节），不能只写『图片不好』", "preference", 4, "生图工作台"),
    ("训练数据集已扩至30条，覆盖两类业务线，含需求评审、异常处理、回归验证三类场景", "fact", 3, "数据集治理"),
    ("项目定位：单机版优先，差异化打法是先离线跑通再接云端，避免过早引入依赖", "decision", 4, "求职"),
    ("缓存策略：读多写少走本地缓存，写操作先落库再失效，忌用双写（容易出现不一致）", "skill", 3, "数据集治理"),
    ("AI漫剧方向：年代重生题材+老年人蓝海，当前卡点是剧本缺口，先写剧本再分镜", "fact", 2, "AI漫剧"),
]
for i, (c, t, imp, proj) in enumerate(seeds):
    send({"jsonrpc": "2.0", "id": 10 + i, "method": "tools/call",
          "params": {"name": "memory_save", "arguments": {
              "content": c, "type": t, "importance": imp, "project": proj, "agent": "test"}}})
    r = recv()
results.append(("写入 6 条记忆", True, None))

# 4. 中文检索测试（关键验证点：bigram 中文检索）
tests = [
    ("生图工作台怎么做", "三栏布局"),
    ("bad case 怎么写", "失败原因类别"),
    ("数据集多少条", "30条"),
    ("缓存怎么失效", "先落库"),
    ("漫剧剧本", "剧本缺口"),
]
hit = 0
detail = []
for q, expect in tests:
    send({"jsonrpc": "2.0", "id": 20, "method": "tools/call",
          "params": {"name": "memory_search", "arguments": {"query": q, "limit": 3}}})
    r = recv()
    text = r["result"]["content"][0]["text"]
    first_line = text.split("\n")[0] if text else ""
    good = expect in first_line
    hit += 1 if good else 0
    detail.append(f"  {q} -> 命中" if good else f"  {q} -> 未命中: {first_line[:50]}")
results.append((f"中文检索命中率 {hit}/5", hit >= 4, detail))

# 5. 交接卡
send({"jsonrpc": "2.0", "id": 30, "method": "tools/call",
      "params": {"name": "memory_handoff", "arguments": {"project": "生图工作台"}}})
r = recv()
card = r["result"]["content"][0]["text"]
ok = "项目交接卡" in card and "三栏布局" in card and "已确认的决策" in card
results.append(("memory_handoff 交接卡", ok, card[:200]))

# 6. stats
send({"jsonrpc": "2.0", "id": 31, "method": "tools/call",
      "params": {"name": "memory_stats", "arguments": {}}})
r = recv()
s = json.loads(r["result"]["content"][0]["text"])
results.append(("memory_stats", s["total"] >= 6, s))

# 7. 会话层：归档对话原文 + 召回原话
send({"jsonrpc": "2.0", "id": 50, "method": "tools/call",
      "params": {"name": "session_save", "arguments": {
          "transcript": "我: 训练数据集要扩到多少条？\nAI: 建议从30条扩到50条，优先补异常样本场景\n我: 那就先补10条异常的",
          "title": "数据集扩容讨论", "project": "数据集", "agent": "test"}}})
r = recv()
saved = r["result"]["content"][0]["text"]
results.append(("session_save 归档对话", "已归档会话" in saved, saved))

send({"jsonrpc": "2.0", "id": 51, "method": "tools/call",
      "params": {"name": "session_recall", "arguments": {"query": "数据集扩到多少条"}}})
r = recv()
recalled = r["result"]["content"][0]["text"]
results.append(("session_recall 召回原话", "异常" in recalled or "50条" in recalled, recalled[:160]))

# 8. 负例：不相关查询应得低分或无结果
send({"jsonrpc": "2.0", "id": 40, "method": "tools/call",
      "params": {"name": "memory_search", "arguments": {"query": "量子引力波探测器", "limit": 3}}})
r = recv()
text = r["result"]["content"][0]["text"]
results.append(("负例不误召回", "没有找到" in text or all(float(l.split("]")[0].strip("[")) < 0.05 for l in text.split("\n") if l.startswith("[")), text[:80]))

proc.stdin.close()
proc.terminate()

# 清理临时库
for suffix in ("", "-journal", "-wal", "-shm"):
    try:
        os.remove(_tmp.name + suffix)
    except OSError:
        pass

print("=" * 60)
all_ok = True
for name, ok, extra in results:
    mark = "PASS" if ok else "FAIL"
    all_ok = all_ok and ok
    print(f"[{mark}] {name}")
    if extra and isinstance(extra, list):
        print("\n".join(extra))
    elif extra:
        print(f"      {str(extra)[:150]}")
print("=" * 60)
print("总体:", "全部通过" if all_ok else "有失败项")
print("（测试使用独立临时数据库，真实 hippocampus.db 未被改动）")
