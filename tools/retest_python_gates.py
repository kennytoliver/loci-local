# -*- coding: utf-8 -*-
"""闸门复测驱动器（替代 run_gates.sh 的 Python 部分；本机没有 bash）

- 每项用独立子进程 + 独立临时 LOCI_DB，互不干扰、不碰真库
- verify_shutdown（会真的关服务）与需要 Node/puppeteer 的项不在此列
"""
import os
import subprocess
import sys
import tempfile
import time

ROOT = r"D:\Loci"
PY = os.environ.get("RETEST_PY", sys.executable)
BASE = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8799"

GATES = [
    ("check_design", [PY, "tools/check_design.py", "panel.py"], False),
    ("audit_silent_skips", [PY, "tools/audit_silent_skips.py"], False),
    ("test_mcp", [PY, "test_mcp.py"], False),
    ("test_panel", [PY, "test_panel.py"], False),
    ("verify_conn_drop", [PY, "tools/verify_conn_drop.py"], False),
    ("verify_session_flow", [PY, "tools/verify_session_flow.py"], False),
    ("verify_session_time", [PY, "tools/verify_session_time.py"], False),
    ("verify_session_dedup", [PY, "tools/verify_session_dedup.py"], False),
    ("verify_scan_sources", [PY, "tools/verify_scan_sources.py"], False),
    ("verify_skills", [PY, "tools/verify_skills.py"], False),
    ("verify_extract_perf", [PY, "tools/verify_extract_perf.py"], False),
    ("verify_rules_migration", [PY, "tools/verify_rules_migration.py"], False),
    ("verify_audit_perf", [PY, "tools/verify_audit_perf.py"], False),
    ("verify_panel_api", [PY, "tools/verify_panel_api.py"], True),
    ("verify_csrf", [PY, "tools/verify_csrf.py", BASE], True),
    ("probe_mcp_connect", [PY, "tools/probe_mcp_connect.py"], False),
    ("probe_bugs_repro", [PY, "tools/probe_bugs_repro.py"], False),
    ("probe_concurrency", [PY, "tools/probe_concurrency.py"], False),
    ("probe_panel_api", [PY, "tools/probe_panel_api.py", BASE], True),
    ("probe_panel_post", [PY, "tools/probe_panel_post.py", BASE], True),
    ("probe_panel_frontend", [PY, "tools/probe_panel_frontend.py"], False),
    ("retest_fixes", [PY, "tools/retest_fixes_20261001.py"], False),
]

results = []
for name, cmd, needs_panel in GATES:
    if needs_panel and not BASE:
        results.append((name, "SKIP", 0, "无面板 URL"))
        continue
    tmp = tempfile.NamedTemporaryFile(prefix="gate_%s_" % name, suffix=".db", delete=False)
    tmp.close()
    env = dict(os.environ, LOCI_DB=tmp.name, PYTHONIOENCODING="utf-8",
               LOCI_AGENT="gate-retest", LOCI_PANEL_BASE=BASE)
    env.pop("LOCI_DB_DISABLE", None)
    t0 = time.time()
    try:
        p = subprocess.run(cmd, cwd=ROOT, env=env, capture_output=True, timeout=420)
        out = (p.stdout + p.stderr).decode("utf-8", "replace")
        code = p.returncode
    except subprocess.TimeoutExpired:
        out, code = "TIMEOUT（>420s）", -9
    dt = time.time() - t0
    status = "PASS" if code == 0 else "FAIL"
    results.append((name, status, dt, out))
    print(f"[{status}] {name:<24} {dt:6.1f}s  exit={code}")
    if code != 0:
        tail = "\n".join(out.strip().splitlines()[-12:])
        print("       " + tail.replace("\n", "\n       "))
    for suf in ("", "-journal", "-wal", "-shm"):
        try:
            os.remove(tmp.name + suf)
        except OSError:
            pass

print()
print("=" * 78)
p = sum(1 for _, s, _, _ in results if s == "PASS")
f = sum(1 for _, s, _, _ in results if s == "FAIL")
k = sum(1 for _, s, _, _ in results if s == "SKIP")
print(f"Python 闸门复测：PASS {p} / FAIL {f} / SKIP {k}  （共 {len(results)} 项）")
if f:
    print("红：", ", ".join(n for n, s, _, _ in results if s == "FAIL"))
os.makedirs(os.path.join(ROOT, "tools", ".gate-logs"), exist_ok=True)
logp = os.path.join(ROOT, "tools", ".gate-logs", "retest-python-gates.txt")
with open(logp, "w", encoding="utf-8") as fh:
    for name, s, dt, out in results:
        fh.write("=" * 70 + f"\n### {name} [{s}] {dt:.1f}s\n" + "=" * 70 + "\n" + out + "\n")
print("完整输出:", logp)
