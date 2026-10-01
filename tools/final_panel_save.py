# -*- coding: utf-8 -*-
"""项 2 / 3 重跑：面板 /api/save 的校验与正常路径

修正上一版的判据错误：
  · save 返回的 id 要用 /api/list 按 id 回查核对（而不是只按 content 过滤）
  · 每步打印原始输出，并把"面板返回 id ↔ 列表实际 id"的一致性单独判定
"""
import json
import sys
import urllib.error
import urllib.request

BASE = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8799"
R = []
MARK = "__面板确认__"


def check(n, ok, raw):
    R.append((n, bool(ok), str(raw)[:180]))


def get(path):
    try:
        with urllib.request.urlopen(BASE + path, timeout=25) as x:
            return x.status, x.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8", "replace")
    except Exception as e:
        return -1, f"{type(e).__name__}: {e}"


def post(path, payload):
    d = json.dumps(payload, ensure_ascii=False).encode()
    r = urllib.request.Request(BASE + path, data=d, method="POST")
    r.add_header("Content-Type", "application/json")
    r.add_header("Origin", BASE)
    try:
        with urllib.request.urlopen(r, timeout=20) as x:
            return x.status, x.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8", "replace")
    except Exception as e:
        return -1, f"{type(e).__name__}: {e}"


def list_all():
    st, body = get("/api/list?limit=-1")
    try:
        return json.loads(body)
    except Exception:
        return []


print(f"BASE={BASE}")
print(f"起始 stats: {get('/api/stats')[1][:160]}")
print()

print("### 项 2：非法 mtype / importance=99 应被拒（ok:false + error）")
NEG = [
    ("mtype='乱写类型'", {"content": MARK + "非法类型", "mtype": "乱写类型"}),
    ("mtype=''", {"content": MARK + "空类型", "mtype": ""}),
    ("mtype='fact', importance=99", {"content": MARK + "越界", "mtype": "fact", "importance": 99}),
    ("mtype='fact', importance=-5", {"content": MARK + "负值", "mtype": "fact", "importance": -5}),
    ("mtype='fact', importance='高'", {"content": MARK + "非整数", "mtype": "fact", "importance": "高"}),
]
for label, payload in NEG:
    code, body = post("/api/save", payload)
    try:
        j = json.loads(body)
        good = code == 200 and j.get("ok") is False and bool(j.get("error"))
    except Exception:
        good = False
    print(f"  {'PASS' if good else 'FAIL'}  {label:<32} HTTP {code} :: {body[:96]}")
    check(f"项2 {label} 被拒", good, f"HTTP {code} :: {body}")

# 非法请求不应留下任何记录
leftovers = [r for r in list_all() if str(r.get("content", "")).startswith(MARK)]
print(f"  非法请求后的残留记录数: {len(leftovers)}")
check("项2 非法请求均未落库", not leftovers,
      json.dumps([(r.get("id"), r.get("content")) for r in leftovers], ensure_ascii=False))

print()
print("### 项 3：正常路径（下拉选类型保存）应正常入库")
POS = [
    ("mtype='decision', importance=3", {"content": MARK + "正常决策", "mtype": "decision", "importance": 3}, "decision", 3),
    ("mtype='fact'（下拉默认）", {"content": MARK + "正常事实", "mtype": "fact", "importance": 2}, "fact", 2),
    ("mtype='error', importance=4", {"content": MARK + "正常踩坑", "mtype": "error", "importance": 4}, "error", 4),
]
want = {}
for label, payload, exp_type, exp_imp in POS:
    code, body = post("/api/save", payload)
    ret_id = None
    try:
        j = json.loads(body)
        ret_id = j.get("id")
        good = code == 200 and isinstance(ret_id, int)
    except Exception:
        good = False
    print(f"  {'PASS' if good else 'FAIL'}  {label:<34} HTTP {code} :: {body[:60]}  (返回 id={ret_id})")
    check(f"项3 {label} 成功", good, f"HTTP {code} :: {body}")
    if ret_id is not None:
        want[ret_id] = (exp_type, exp_imp)

# 回查：面板返回的 id 是否真能在列表里找到，且类型/重要度正确
rows = list_all()
by_id = {r.get("id"): r for r in rows}
print()
print("  回查（save 返回的 id -> list 里的实际记录）:")
for i, (exp_type, exp_imp) in want.items():
    r = by_id.get(i)
    if r is None:
        print(f"    #{i} ❌ 列表里找不到这个 id（面板返回的 id 与列表不一致！）")
        check(f"项3 #{i} 能被列表回查", False, f"列表 id 集合={sorted(str(k) for k in by_id)}")
    else:
        ok = r.get("mtype") == exp_type and r.get("importance") == exp_imp
        print(f"    #{i} {'PASS' if ok else 'FAIL'} mtype={r.get('mtype')!r}(期望 {exp_type!r}) "
              f"importance={r.get('importance')}(期望 {exp_imp}) content={r.get('content')!r}")
        check(f"项3 #{i} 类型/重要度与提交一致", ok,
              f"实际 mtype={r.get('mtype')!r} imp={r.get('importance')}")

print()
print("### 清理（按精确内容，逐个删）")
rows = list_all()
mine = [r.get("id") for r in rows if str(r.get("content", "")).startswith(MARK)]
for i in mine:
    c, b = post("/api/delete", {"id": i})
    print(f"  删除 #{i} -> HTTP {c} {b[:50]}")
after = [r for r in list_all() if str(r.get("content", "")).startswith(MARK)]
print(f"  清理后残留: {len(after)}")
check("项3 测试记录已清理", not after, json.dumps(after, ensure_ascii=False)[:100])

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
