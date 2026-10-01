# -*- coding: utf-8 -*-
"""项 4：空库面板的 /api/audit/report + health_score（开发侧声称修好的 KeyError）

自起一个**独立空库**面板（端口 8797），先只调只读端点（不写任何东西），
确保测的是真正的空库路径；跑完自动关掉。
"""
import json
import os
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PORT = 8797
BASE = "http://127.0.0.1:%d" % PORT
DB = tempfile.NamedTemporaryFile(prefix="empty_lib_", suffix=".db", delete=False).name
R = []


def check(n, ok, raw):
    R.append((n, bool(ok), str(raw)[:300]))


def get(path, timeout=30):
    try:
        with urllib.request.urlopen(BASE + path, timeout=timeout) as x:
            return x.status, x.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8", "replace")
    except Exception as e:
        return -1, f"{type(e).__name__}: {e}"


env = dict(os.environ, LOCI_DB=DB, PYTHONIOENCODING="utf-8")
print(f"空库 = {DB}")
proc = subprocess.Popen([sys.executable, "-X", "utf8", os.path.join(HERE, "panel.py"),
                         "--port", str(PORT), "--idle-exit", "60"],
                        stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env)
ready = False
for _ in range(40):
    time.sleep(0.5)
    st, _b = get("/api/ping", timeout=3)
    if st == 200:
        ready = True
        break
check("空库面板能起来", ready, f"ping HTTP {st}")
if not ready:
    proc.terminate()
    print("面板没起来，无法继续")
    sys.exit(1)

print()
print("=== 确认这确实是空库 ===")
st, body = get("/api/stats")
print(f"  /api/stats -> HTTP {st} :: {body[:200]}")
try:
    total = json.loads(body).get("total")
except Exception:
    total = None
check("面板连的是空库（total=0）", total == 0, f"total={total}")

print()
print("=== 项 4-a：/api/health（health_score 空库提前返回）===")
st, body = get("/api/health")
print(f"  HTTP {st} :: {body[:300]}")
try:
    h = json.loads(body)
    check("空库 /api/health 返回 200 且含 grade_text", st == 200 and "grade_text" in h,
          f"HTTP {st} :: {body}")
    print(f"  grade_text = {h.get('grade_text')!r}  grade={h.get('grade')!r}  score={h.get('score')!r}")
except Exception as e:
    check("空库 /api/health 返回合法 JSON", False, f"{e}: {body[:200]}")

print()
print("=== 项 4-b：/api/audit/report（以前 KeyError → 400 / 断连）===")
st, body = get("/api/audit/report")
print(f"  HTTP {st}  body 长度={len(body)}")
print(f"  body 前 400 字符:\n{body[:400]}")
ok = st == 200
md = ""
try:
    j = json.loads(body)
    md = j.get("markdown") or ""
    ok = ok and bool(md)
except Exception as e:
    ok = False
    print("  解析失败:", e)
check("空库 /api/audit/report -> 200 + markdown", ok, f"HTTP {st} len={len(body)} :: {body[:200]}")
print(f"  markdown 长度 = {len(md)}")
if md:
    print("  markdown 内容:")
    for line in md.splitlines()[:14]:
        print("    |", line)

print()
print("=== 项 4-c：库层直调（绕过面板）===")
code = (
    "import sys;sys.path.insert(0,r'%s')\n"
    "import loci\n"
    "h = loci.health_score()\n"
    "print('  health_score() =', h)\n"
    "print('  含 grade_text:', 'grade_text' in h)\n"
    "rep = loci.audit_report()\n"
    "print('  audit_report() 长度 =', len(rep))\n"
    "print('  audit_report() 前 120 字符 =', rep[:120].replace(chr(10),' / '))\n"
) % HERE.replace("\\", "\\\\")
p = subprocess.run([sys.executable, "-X", "utf8", "-c", code],
                   env=dict(os.environ, LOCI_DB=DB, PYTHONIOENCODING="utf-8"),
                   capture_output=True)
out = (p.stdout + p.stderr).decode("utf-8", "replace")
print(out.rstrip())
check("库层 health_score/audit_report 空库不抛异常", p.returncode == 0, out)

proc.terminate()
try:
    proc.wait(timeout=10)
except subprocess.TimeoutExpired:
    proc.kill()
for s in ("", "-journal", "-wal", "-shm"):
    try:
        os.remove(DB + s)
    except OSError:
        pass

print()
print("=" * 74)
fails = [n for n, ok, _ in R if not ok]
for n, ok, raw in R:
    print(f"[{'PASS' if ok else 'FAIL'}] {n}")
    if not ok:
        print(f"       {raw}")
print("=" * 74)
print(f"合计 {len(R)} 项，FAIL {len(fails)} 项" + (f" -> {fails}" if fails else ""))
sys.exit(1 if fails else 0)
