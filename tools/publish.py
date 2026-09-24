#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""把本地仓库文件发布到 GitHub。

本机 git 走代理连不上 github.com，所以改用 GitHub Contents API 上传（已验证可行）。

用法：
    # Windows (Git Bash)
    export GITHUB_TOKEN=ghp_xxxx
    python tools/publish.py            # 先看有哪些文件会变（预演）
    python tools/publish.py --yes      # 真正上传

令牌获取：https://github.com/settings/tokens
  只需勾两项：repo + workflow
（含 .github/workflows/ 时必须 workflow scope，否则被拒）

注意：令牌只从环境变量读，**不会写进任何文件**。
"""
import base64
import hashlib
import json
import os
import sys
import urllib.parse
import urllib.request
import urllib.error

OWNER = "kennytoliver"
REPO = "loci-local"
BRANCH = "main"
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
API = "https://api.github.com/repos/%s/%s" % (OWNER, REPO)
# 这些不上传（本地私有文件）
SKIP_DIRS = ("docs", "backup", "__pycache__", ".git", "trash-backup", "archive")
SKIP_FILES = ("RESTORE.md", "archive.json", "agents.json", "filelist.txt",
              "hippocampus.db", "hippohub.db")
SKIP_SUFFIX = (".db", ".db-wal", ".db-shm", ".bat", ".lnk")


def we_want(path):
    rel = os.path.relpath(path, ROOT).replace(os.sep, "/")
    for d in SKIP_DIRS:
        if rel == d or rel.startswith(d + "/"):
            return False
    if os.path.basename(path) in SKIP_FILES:
        return False
    if rel.endswith(SKIP_SUFFIX):
        return False
    return True


def collect():
    """只上传 git 追踪的文件 —— 与仓库内容严格一致，也不会带进中文路径的本地文件"""
    import subprocess
    r = subprocess.run(["git", "ls-files"], capture_output=True, text=True, cwd=ROOT)
    out = [x.strip().replace(os.sep, "/") for x in (r.stdout or "").splitlines() if x.strip()]
    return sorted(x for x in out if we_want(os.path.join(ROOT, x.replace("/", os.sep))))


def main():
    token = os.environ.get("GITHUB_TOKEN", "").strip()
    if not token:
        print("请先设置令牌：")
        print("  export GITHUB_TOKEN=ghp_xxxx   （Windows 用 set 或 Git Bash 的 export）")
        return 1
    apply = "--yes" in sys.argv
    op = urllib.request.build_opener(urllib.request.ProxyHandler({}))

    def api(url, data=None, method=None):
        body = json.dumps(data).encode("utf-8") if data is not None else None
        req = urllib.request.Request(url, data=body,
                                     method=method or ("PUT" if body else "GET"))
        req.add_header("Authorization", "token " + token)
        req.add_header("Accept", "application/vnd.github+json")
        if body:
            req.add_header("Content-Type", "application/json")
        try:
            with op.open(req, timeout=60) as r:
                return json.loads(r.read().decode("utf-8")), 200
        except urllib.error.HTTPError as e:
            try:
                return json.loads(e.read().decode("utf-8", "replace")), e.code
            except Exception:
                return {"message": str(e)}, e.code

    files = collect()
    print("待处理 %d 个文件%s" % (len(files), "" if apply else "（预演，加 --yes 才真正上传）"))
    changed = same = fail = 0
    for rel in files:
        with open(os.path.join(ROOT, rel), "rb") as fh:
            raw = fh.read()
        b64 = base64.b64encode(raw).decode("ascii")
        # 先取远端 sha 判断是否真有变化
        cur, code = api(API + "/contents/" + urllib.parse.quote(rel) + "?ref=" + BRANCH)
        remote_b64 = ""
        sha = None
        if code == 200 and isinstance(cur, dict):
            remote_b64 = cur.get("content", "").replace("\n", "")
            sha = cur.get("sha")
        if sha and remote_b64 == b64:
            same += 1
            print("  = 未变   %s" % rel)
            continue
        if not apply:
            changed += 1
            print("  ~ 将更新 %s" % rel if sha else "  + 将新增 %s" % rel)
            continue
        payload = {"message": "chore: update %s" % rel, "content": b64, "branch": BRANCH}
        if sha:
            payload["sha"] = sha
        r, code = api(API + "/contents/" + urllib.parse.quote(rel), payload)
        if code in (200, 201):
            changed += 1
            print("  ✅ %s %s" % ("已更新" if sha else "已新增", rel))
        else:
            fail += 1
            print("  ❌ %s → HTTP %s %s" % (rel, code, (r or {}).get("message", "")[:90]))
    print()
    print("新增/更新 %d ｜ 未变 %d ｜ 失败 %d" % (changed, same, fail))
    return 0 if fail == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
