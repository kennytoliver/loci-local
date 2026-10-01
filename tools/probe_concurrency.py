# -*- coding: utf-8 -*-
"""并发写压力测试：模拟多个 Agent 同时通过 MCP 写同一个库（项目的核心场景）

每个子进程都是独立的 MCP 服务端进程（stdio），同时向同一 loci.db 写入。
检查：① 是否有 "database is locked" / 异常；② 写入条数是否等于预期（有无丢失）。
"""
import concurrent.futures
import json
import os
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SERVER = os.path.join(HERE, "loci.py")
DB = tempfile.NamedTemporaryFile(prefix="conc_loci_", suffix=".db", delete=False).name
PROCS = 6
PER_PROC = 8
TOTAL = PROCS * PER_PROC


def worker(idx):
    env = dict(os.environ, LOCI_DB=DB, PYTHONIOENCODING="utf-8", LOCI_AGENT=f"agent{idx}")
    p = subprocess.Popen([sys.executable, "-X", "utf8", SERVER], stdin=subprocess.PIPE,
                         stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env)
    p.stdin.write((json.dumps({"jsonrpc": "2.0", "id": 1, "method": "initialize",
                               "params": {"protocolVersion": "2024-11-05", "capabilities": {},
                                          "clientInfo": {"name": f"a{idx}", "version": "1"}}},
                              ensure_ascii=False) + "\n").encode())
    p.stdin.write((json.dumps({"jsonrpc": "2.0", "method": "notifications/initialized"}) + "\n").encode())
    p.stdin.flush()
    p.stdout.readline()
    ok = 0
    errs = []
    for i in range(PER_PROC):
        req = {"jsonrpc": "2.0", "id": 10 + i, "method": "tools/call",
               "params": {"name": "memory_save",
                          "arguments": {"content": f"并发写入测试 agent{idx} 第{i}条 关键字_并发_{idx}_{i}",
                                        "project": "并发"}}}
        p.stdin.write((json.dumps(req, ensure_ascii=False) + "\n").encode("utf-8"))
        p.stdin.flush()
        line = p.stdout.readline().decode("utf-8").strip()
        try:
            r = json.loads(line)
            res = r.get("result", {})
            if res.get("isError"):
                errs.append(res["content"][0]["text"])
            elif "已保存" in res["content"][0]["text"]:
                ok += 1
        except Exception as e:
            errs.append(f"解析失败: {e} :: {line[:120]}")
    p.stdin.close()
    try:
        p.wait(timeout=15)
    except subprocess.TimeoutExpired:
        p.terminate()
    se = p.stderr.read().decode("utf-8", "replace").strip()
    if se:
        errs.append("stderr: " + se[:300])
    return idx, ok, errs


print(f"并发：{PROCS} 个独立 MCP 进程 × {PER_PROC} 条 = 预期 {TOTAL} 条")
with concurrent.futures.ThreadPoolExecutor(max_workers=PROCS) as ex:
    results = list(ex.map(worker, range(PROCS)))

ok_total = 0
all_errs = []
for idx, ok, errs in results:
    ok_total += ok
    all_errs += [(idx, e) for e in errs]
    print(f"  agent{idx}: 成功 {ok}/{PER_PROC}" + (f" | 错误 {len(errs)} 条" if errs else ""))

print(f"\n写入成功合计: {ok_total}/{TOTAL} -> {'PASS' if ok_total == TOTAL else 'FAIL（有丢失）'}")

# 用引擎统计实际落库条数
env = dict(os.environ, LOCI_DB=DB, PYTHONIOENCODING="utf-8")
out = subprocess.run([sys.executable, "-X", "utf8", "-c",
                      "import sys;sys.path.insert(0,r'%s');import loci,json;"
                      "print(json.dumps(loci.stats(),ensure_ascii=False))" % HERE],
                     capture_output=True, env=env)
try:
    st = json.loads(out.stdout.decode("utf-8").strip().splitlines()[-1])
    print(f"数据库实际落库: total={st['total']} -> "
          f"{'PASS' if st['total'] == TOTAL else 'FAIL（应为 %d）' % TOTAL}")
except Exception as e:
    print("统计失败:", e, out.stdout[:200], out.stderr[:300])

if all_errs:
    print("\n错误明细（去重）:")
    for e in sorted({x[1] for x in all_errs})[:10]:
        print("  -", e)

for suf in ("", "-journal", "-wal", "-shm"):
    try:
        os.remove(DB + suf)
    except OSError:
        pass
