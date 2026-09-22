"""面板可用性全扫描：把每个「只读」接口都打一遍，确认整块面板真能用。

背景：闸门（test_panel / smoke_panel / verify_link_*）验证的是**页面行为**，
但没人验证过**后端接口本身**是否条条通。面板有 49 个路由，其中任何一条挂掉，
对应的按钮点了就是没反应 —— 而这在页面冒烟里未必覆盖得到（比如"导出质检报告"
「孤儿扫描」「记忆包导出」都不在冒烟范围内）。

本脚本只打**只读 / 无副作用**接口，不碰任何写操作（save/delete/merge/collect/run/
import/shutdown 一律跳过），可以安全地对着正在用的面板跑。

用法：
    # 先起面板（建议常驻，否则闲置 300 秒会自动退出）
    python panel.py --idle-exit 0
    # 再跑扫描
    python tools/verify_panel_api.py

零第三方依赖。
"""
import json
import sys
import urllib.error
import urllib.request

BASE = "http://127.0.0.1:8787"

# (路径, 说明, 期望在响应里出现的关键字/键 —— 空表示只要 200+JSON 合法)
READONLY = [
    # ---- 基础 ----
    ("/api/ping", "存活探测", None),
    ("/api/stats", "记忆统计", None),
    ("/api/context", "常驻上下文包", None),
    ("/api/list", "记忆列表", None),
    ("/api/health", "健康度评分", "score"),
    ("/api/audit", "质检七查", "duplicates"),
    ("/api/audit/report", "质检报告（导出用）", "markdown"),
    # ---- 检索 ----
    ("/api/search?q=%E9%9D%A2%E6%9D%BF", "记忆检索『面板』", None),
    ("/api/orphans", "孤儿记忆扫描", None),
    # ---- 会话 ----
    ("/api/session/list", "会话列表", None),
    ("/api/session/get?sid=12", "会话详情（时间线数据源）", "messages"),
    ("/api/session/search?q=%E8%B4%A8%E6%A3%80", "原话检索『质检』", None),
    ("/api/extract?sid=12", "抽取候选", None),
    # ---- Agent ----
    ("/api/agents", "Agent 列表", None),
    ("/api/scan", "采集源扫描", None),
    # ---- 清理 / 打包 / 技能 / 交接 ----
    ("/api/cleanup/preview", "清理预览", None),
    ("/api/pack/export", "记忆包导出", None),
    ("/api/skill/preview", "技能导出预览", None),
    ("/api/handoff", "交接卡生成", None),
    ("/api/backups", "备份列表", None),
    ("/api/sourcefiles", "来源文件", None),
]

# POST 但**无副作用**的接口（只解析/只计算，不落库）—— 可以安全调用
# /api/session/parse 正好顺带验证「约定里的 transcript 格式」在前端链路上也能被切分
POST_READONLY = [
    ("/api/session/parse", {"text": "我：这个面板能用吗？\nAI：能。\n我：那就好。\nAI：嗯。"},
     "会话解析预览（只读）", "messages"),
]

# 明确跳过的写操作接口 —— 列出来是为了让"没扫"这件事透明，不是漏扫
SKIPPED_WRITES = [
    "/api/save", "/api/delete", "/api/pin", "/api/retire", "/api/touch",
    "/api/merge", "/api/supersede", "/api/split", "/api/collect",
    "/api/autoscan", "/api/cleanup/run", "/api/sourcefiles/clean",
    "/api/session/save", "/api/session/delete",
    "/api/pack/import", "/api/archive/config", "/api/archive/snapshot",
    "/api/agent/register", "/api/agent/unregister", "/api/agent/add",
    "/api/agent/forget", "/api/shutdown",
]

# 页面容器 id（8 页）—— 必须在首页 HTML 里（注意是 v-agents 复数）
PAGES = ["v-mem", "v-session", "v-handoff", "v-pack", "v-clean",
         "v-agents", "v-audit", "v-collect"]

# 静态资源（icon-blue.png 是发布前必须核实存在的那一个）
STATIC = ["/icon.png", "/icon-blue.png"]

results = []


def get(path, timeout=90):
    req = urllib.request.Request(BASE + path)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, r.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()
    except Exception as e:
        return None, repr(e).encode()


def check(name, cond, detail=""):
    results.append((name, bool(cond)))
    print(f"  {'PASS' if cond else 'FAIL'}  {name}" + (f"   {detail}" if detail else ""))


print("=" * 70)
print("面板可用性全扫描（只读，跳过所有写操作）")
print("=" * 70)

# ---------- 0. 服务在不在 ----------
print("\n【服务】")
st, body = get("/")
alive = st == 200 and len(body) > 50000
check("首页可访问且内容完整", alive, f"HTTP {st}, {len(body)} 字节")
check("首页含全部 8 个页面容器", all(f'id="{p}"'.encode() in body for p in PAGES),
      f"{sum(1 for p in PAGES if f'id=\"{p}\"'.encode() in body)}/8")
check("首页引用品牌图标 /icon-blue.png", b"/icon-blue.png" in body)

if not alive:
    print("\n服务没起来 —— 先跑：python panel.py --idle-exit 0")
    sys.exit(1)

for p in STATIC:
    st, body = get(p)
    check(f"静态资源 {p}", st == 200 and len(body) > 100, f"HTTP {st}, {len(body)} 字节")

# ---------- 1. 各接口 ----------
print("\n【接口】")
for path, desc, must in READONLY:
    st, body = get(path)
    ok = st == 200
    detail = f"HTTP {st}"
    if ok:
        try:
            data = json.loads(body.decode("utf-8"))
            if isinstance(data, dict) and data.get("error"):
                ok = False
                detail = f"返回 error: {data['error']}"
            elif must and must not in json.dumps(data, ensure_ascii=False):
                ok = False
                detail = f"响应缺少关键字段 {must!r}"
            else:
                n = len(data) if isinstance(data, (list, dict)) else "?"
                detail = f"HTTP 200, {len(body)} 字节, 顶层 {n} 项"
        except Exception as e:
            ok = False
            detail = f"不是合法 JSON: {e}"
    else:
        detail = f"HTTP {st} {body[:60]!r}"
    check(f"{path:38s} {desc}", ok, detail)

# ---------- 2. POST 只读接口 ----------
print("\n【POST 接口（无副作用）】")
for path, payload, desc, must in POST_READONLY:
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(BASE + path, data=data, method="POST")
    req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            st, body = r.status, r.read()
    except urllib.error.HTTPError as e:
        st, body = e.code, e.read()
    except Exception as e:
        st, body = None, repr(e).encode()
    ok = st == 200
    detail = f"HTTP {st}"
    if ok:
        try:
            j = json.loads(body.decode("utf-8"))
            msgs = j.get(must) or []
            # 顺带验证：约定里的 transcript 格式在服务端链路上也能切成多轮
            ok = len(msgs) == 4 and [m["role"] for m in msgs] == ["user", "assistant", "user", "assistant"]
            detail = f"HTTP 200, 切出 {len(msgs)} 轮 → 约定格式有效 ✅" if ok else \
                     f"HTTP 200 但切分异常: {len(msgs)} 条"
        except Exception as e:
            ok = False
            detail = f"不是合法 JSON: {e}"
    check(f"{path:38s} {desc}", ok, detail)

# ---------- 3. 边界：不存在的会话 ----------
print("\n【边界处理（应优雅报错，不是 500 / 崩）】")
st, body = get("/api/session/get?sid=999999")
ok = st == 200 and b"error" in body
check("/api/session/get 不存在的会话 → 优雅报错", ok, f"HTTP {st} {body[:50]!r}")

st, body = get("/api/session/get")
ok = st in (200, 400)
check("/api/session/get 缺参数 → 不崩", ok, f"HTTP {st}")

st, body = get("/api/definitely-not-a-route")
ok = st == 404
check("未知路由 → 404 而不是 500", ok, f"HTTP {st}")

print("\n【未扫描（写操作，故意跳过）】")
print("  " + " · ".join(x for x in SKIPPED_WRITES if "session/parse" not in x))

print()
ok_all = all(c for _, c in results)
print(f"合计 {len(results)} 项，失败 {sum(1 for _, c in results if not c)}")
print("结论: " + ("面板整体可用 ✅" if ok_all else "有接口不可用 ❌（见上面 FAIL 行）"))
sys.exit(0 if ok_all else 1)
