#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""本机内容四端点的端到端闸门（只读）。

为什么单写一个：
  `verify_panel_api.py` 覆盖了 21 个老端点 + 一批写操作，但上一轮新加的
  **`/api/content`、`/api/content/detail`、`/api/plugin/versions`、
  `/api/backup/detail` 在这套闸门里一条都没有** —— 也就是说"本机内容"这页
  后端一旦挂了，全绿的表也看不出来。这个脚本补的就是这四条。

它重点验三件事：
  1. **五类清单能出数据**（配置 / MCP / 插件 / 历史版本 / 来源），且字段齐全
  2. **列表 → 详情闭环**：列表里出现的每一项，详情接口都必须点得开
     （这才是"数据流完整跑通"，而不是"接口返回 200"）
  3. **脱敏与越权**：真 API Key 一个字节都不许出现在任何响应里；
     拿详情接口去读白名单外的文件（含 ../ 穿越）必须被拒

用法：
  python tools/verify_content_api.py                 # 默认 http://127.0.0.1:8787
  python tools/verify_content_api.py http://127.0.0.1:8787
"""
import io
import json
import os
import re
import sys
import urllib.error
import urllib.parse
import urllib.request

BASE = (sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8787").rstrip("/")
results = []


def get(path, timeout=60):
    try:
        with urllib.request.urlopen(BASE + path, timeout=timeout) as r:
            return r.status, r.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()
    except Exception as e:
        return None, repr(e).encode()


def jget(path):
    """取一个端点并解析 JSON；返回 (ok, data_or_None, raw_text)"""
    st, body = get(path)
    txt = body.decode("utf-8", "replace") if body else ""
    if st != 200:
        return False, None, txt
    try:
        return True, json.loads(txt), txt
    except Exception:
        return False, None, txt


def check(name, cond, detail=""):
    results.append((name, bool(cond)))
    print("  %s  %s%s" % ("PASS" if cond else "FAIL", name, ("   " + detail) if detail else ""))


def q(**kw):
    return "?" + urllib.parse.urlencode(kw)


print("=" * 70)
print("本机内容端点闸门（只读）  BASE=%s" % BASE)
print("=" * 70)

# ---------- 0. 服务在不在 ----------
st, body = get("/")
if st != 200:
    print("\n服务没起来 —— 先跑：python panel.py")
    sys.exit(1)

# ---------- 1. /api/content 五类清单 ----------
print("\n【一、五类清单】")
ok, content, raw = jget("/api/content")
check("/api/content 可访问且是 JSON", ok and isinstance(content, dict), "HTTP/解析 %s" % ok)
if not (ok and isinstance(content, dict)):
    print("\n拿不到 /api/content，后续跳过。")
    sys.exit(1)

for key, label in (("configs", "配置文件"), ("mcps", "MCP 服务"),
                   ("plugins", "插件"), ("backups", "历史版本/备份"),
                   ("sources", "来源声明")):
    v = content.get(key)
    check("五类清单含 %s（%s）" % (key, label),
          isinstance(v, list), "类型 %s / %d 条" % (type(v).__name__, len(v) if isinstance(v, list) else -1))

# 本机应该真的有内容可清算（不是空壳）。0 条说明采集器断了 —— 这是真信号。
check("配置文件非空（采集器真的在读本机）", len(content.get("configs") or []) > 0,
      "%d 条" % len(content.get("configs") or []))
check("插件非空", len(content.get("plugins") or []) > 0,
      "%d 条" % len(content.get("plugins") or []))
check("历史版本非空（75 个插件旧版本 + 配置备份）",
      len(content.get("backups") or []) > 0,
      "%d 条" % len(content.get("backups") or []))

# ---------- 2. 字段齐全 ----------
print("\n【二、字段齐全（前端渲染直接依赖这些键）】")
plugs = content.get("plugins") or []
if plugs:
    P = plugs[0]
    need_p = ["kind", "id", "name", "marketplace", "agent", "version",
              "enabled", "path", "dir", "version_count", "old_count", "note", "source_file"]
    miss = [k for k in need_p if k not in P]
    check("插件条目字段齐全", not miss, ("缺 %s" % miss) if miss else "%d 个字段" % len(need_p))
    check("插件 kind 标记正确", P.get("kind") == "plugin", str(P.get("kind")))
    # dir 是"打开所在文件夹"按钮的抓手；有 installPath 的必须能推出 dir
    has_path = [p for p in plugs if p.get("path")]
    check("有安装路径的插件都能推出 dir（打开文件夹按钮的抓手）",
          all(p.get("dir") for p in has_path),
          "%d/%d 有 dir" % (sum(1 for p in has_path if p.get("dir")), len(has_path)))
    check("old_count 与 version_count 自洽（旧版本数 ≤ 总版本数）",
          all(int(p.get("old_count", 0)) <= int(p.get("version_count", 0)) for p in plugs))

cfgs = content.get("configs") or []
if cfgs:
    C = cfgs[0]
    need_c = ["kind", "name", "agent", "path", "keys", "exists", "secret", "size", "fmt"]
    miss = [k for k in need_c if k not in C]
    check("配置文件条目字段齐全", not miss, ("缺 %s" % miss) if miss else "%d 个字段" % len(need_c))
    check("配置文件 kind 标记正确", C.get("kind") == "config", str(C.get("kind")))

bks = content.get("backups") or []
if bks:
    B = bks[0]
    need_b = ["path", "name", "size", "dir", "mtime", "of", "agent", "kind"]
    miss = [k for k in need_b if k not in B]
    check("备份条目字段齐全", not miss, ("缺 %s" % miss) if miss else "%d 个字段" % len(need_b))
    check("备份按时间倒序", all(
        bks[i]["mtime"] >= bks[i + 1]["mtime"] for i in range(min(len(bks), 6) - 1)))

# ---------- 3. 列表 → 详情 闭环 ----------
print("\n【三、列表 → 详情闭环（每一项都必须点得开）】")

# 3a. 插件 → 版本列表：抽所有「有旧版本」的插件全验，没有就抽前 3 个
rich = [p for p in plugs if p.get("old_count", 0) > 0] or plugs[:3]
bad = []
for p in rich[:8]:
    ok2, ver, _ = jget("/api/plugin/versions" + q(agent=p["agent"], path=p["path"] or ""))
    if not ok2 or not isinstance(ver, dict):
        bad.append((p["name"], "不可访问"))
        continue
    if p.get("path"):
        if not ver.get("ok"):
            bad.append((p["name"], "ok!=True"))
        elif len(ver.get("versions") or []) != p.get("version_count"):
            bad.append((p["name"], "版本数对不上 %s vs %s"
                        % (len(ver.get("versions") or []), p.get("version_count"))))
        else:
            cur = [v for v in ver["versions"] if v.get("is_current")]
            if len(cur) != 1:
                bad.append((p["name"], "is_current 标记 %d 个" % len(cur)))
            elif cur[0]["version"] != ver.get("current"):
                bad.append((p["name"], "current 字段与标记不一致"))
check("插件列表 → 版本详情 全部点得开且自洽", not bad,
      ("%d 个有问题：%s" % (len(bad), bad[:3])) if bad else "验了 %d 个" % len(rich[:8]))

# 3b. 备份 → 备份详情：抽 3 个全验
bad_b = []
for b in bks[:3]:
    ok2, det, _ = jget("/api/backup/detail" + q(path=b["path"]))
    if not ok2 or not isinstance(det, dict):
        bad_b.append((b["name"], "不可访问"))
    elif det.get("error"):
        bad_b.append((b["name"], det["error"][:40]))
    elif not det.get("is_backup"):
        bad_b.append((b["name"], "没标 is_backup"))
check("备份列表 → 备份详情 全部点得开", not bad_b,
      ("%s" % bad_b[:3]) if bad_b else "验了 %d 个" % min(len(bks), 3))

# 3c. 配置文件 → 配置详情：抽 3 个全验
bad_c = []
for c in cfgs[:3]:
    ok2, det, _ = jget("/api/content/detail" + q(path=c["path"]))
    if not ok2 or not isinstance(det, dict):
        bad_c.append((c["name"], "不可访问"))
    elif det.get("error"):
        bad_c.append((c["name"], det["error"][:40]))
    elif det.get("secret") and det.get("text"):
        bad_c.append((c["name"], "敏感文件却回了正文"))
check("配置列表 → 配置详情 全部点得开", not bad_c,
      ("%s" % bad_c[:3]) if bad_c else "验了 %d 个" % min(len(cfgs), 3))

# ---------- 4. 脱敏：真 Key 一个字节都不许出现 ----------
print("\n【四、脱敏（真 API Key 不得出现在任何响应里）】")
# 从本机真配置里取真 Key（只在内存里比对，绝不打印出来）
secrets = []
for cand in ("~/.workbuddy/models.json", "~/.workbuddy/mcp.json",
             "~/.workbuddy/settings.json", "~/.zcode/cli/config.json"):
    p = os.path.expanduser(cand)
    if not os.path.isfile(p):
        continue
    try:
        with io.open(p, encoding="utf-8", errors="replace") as f:
            blob = f.read()
    except Exception:
        continue
    for m in re.finditer(r'"(?:apiKey|api_key|key|token|secret|password)"\s*:\s*"([^"]{8,})"',
                         blob, re.I):
        v = m.group(1)
        # 掩码形态的不算真值（**、xxxx、<set> 之类）
        if re.search(r"[\*•]|\bx{4,}\b|^\<.*\>$|已设置|redacted|your-", v, re.I):
            continue
        secrets.append(v)
check("取到本机真凭据样本（用于泄露比对）", len(secrets) > 0,
      "%d 条真值样本" % len(secrets))

if secrets:
    leaks = []
    # 全量响应里逐个比对：出现在哪个端点
    probes = [("/api/content", raw), ("/api/skills", None)]
    for c in cfgs[:6]:
        ok2, _, t = jget("/api/content/detail" + q(path=c["path"]))
        probes.append(("/api/content/detail?path=%s" % c["name"], t))
    for b in bks[:6]:
        ok2, _, t = jget("/api/backup/detail" + q(path=b["path"]))
        probes.append(("/api/backup/detail?path=%s" % b["name"], t))
    for name, t in probes:
        if not t:
            continue
        for s in secrets:
            if s in t:
                leaks.append(name)
                break
    check("任何响应里都不含真凭据明文", not leaks,
          ("泄露于 %s" % leaks) if leaks else "比对了 %d 个端点 × %d 条真值" % (len(probes), len(secrets)))
    # 备份尤其要盯：models.json.bak-* 里是明文 Key，不能因为"是备份"就开后门
    bak_leak = []
    for b in bks:
        if "models.json" not in b["name"]:
            continue
        ok2, _, t = jget("/api/backup/detail" + q(path=b["path"]))
        if t and any(s in t for s in secrets):
            bak_leak.append(b["name"])
    check("配置备份（models.json.bak-*）同样走脱敏，没开'看原文'后门",
          not bak_leak, ("泄露于 %s" % bak_leak) if bak_leak else "已验 %d 个 Key 备份"
          % len([b for b in bks if "models.json" in b["name"]]))

# ---------- 5. 越权与边界 ----------
print("\n【五、越权与边界（读不到就要拒，不能崩也不能静默回原文）】")
WINDOWS = "C:\\Windows\\win.ini"
ok2, det, _ = jget("/api/content/detail" + q(path=WINDOWS))
check("配置详情：白名单外的文件被拒", ok2 and isinstance(det, dict) and det.get("error"),
      str((det or {}).get("error"))[:48] if isinstance(det, dict) else "非 JSON")

ok2, det, _ = jget("/api/backup/detail" + q(path=WINDOWS))
check("备份详情：非备份文件被拒（不能当任意文件读取器）",
      ok2 and isinstance(det, dict) and det.get("error"),
      str((det or {}).get("error"))[:48] if isinstance(det, dict) else "非 JSON")

# 拿一个真的配置路径，用 ../ 绕过
if cfgs:
    base = os.path.dirname(cfgs[0]["path"])
    trav = os.path.join(base, "..", "..", "Windows", "win.ini")
    ok2, det, _ = jget("/api/content/detail" + q(path=trav))
    check("配置详情：../ 穿越被拒", ok2 and isinstance(det, dict) and det.get("error"),
          str((det or {}).get("error"))[:48] if isinstance(det, dict) else "非 JSON")

    ok2, det, _ = jget("/api/plugin/versions" + q(agent="WorkBuddy", path=WINDOWS))
    check("插件版本：白名单外的路径回空（不扫任意目录）",
          ok2 and isinstance(det, dict) and not (det.get("versions") or []),
          "versions=%s" % (len((det or {}).get("versions") or []) if isinstance(det, dict) else "?"))

for path, desc in (("/api/content/detail", "缺 path 参数"),
                   ("/api/plugin/versions", "缺参数"),
                   ("/api/backup/detail", "缺 path 参数")):
    ok2, det, _ = jget(path)
    check("%s → 不崩（返回 JSON）" % desc, ok2 and isinstance(det, dict))

ok2, det, _ = jget("/api/backup/detail" + q(path="C:\\definitely-not-here-9f3a.json"))
check("备份详情：不存在的路径优雅报错",
      ok2 and isinstance(det, dict) and (det.get("error") or det.get("ok") is False))

# ---------- 6. 与前端对账（同一份数据的另一种视图） ----------
print("\n【六、与 /api/skills 的边界（两页数据不许串）】")
ok2, skills, _ = jget("/api/skills")
if isinstance(skills, dict):
    check("/api/skills 仍只回技能三件套（没被内容采集器污染）",
          all(k in skills for k in ("skills", "sources", "targets")),
          "键：%s" % sorted(skills.keys()))
    sk = skills.get("skills") or []
    check("技能清单非空", len(sk) > 0, "%d 个技能" % len(sk))

# ---------- 汇总 ----------
passed = sum(1 for _, okv in results if okv)
failed = [n for n, okv in results if not okv]
print("\n" + "=" * 70)
print("  通过 %d 项，失败 %d 项" % (passed, len(failed)))
if failed:
    print("  红：")
    for n in failed:
        print("    · %s" % n)
print("=" * 70)
sys.exit(1 if failed else 0)
