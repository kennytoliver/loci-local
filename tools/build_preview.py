#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""把 README.md 渲染成 HTML 预览（零依赖，纯标准库）。

存在的意义：上架前要"看到效果"，但手写一份 HTML 预览必然和真实 README 不一致。
这个脚本保证**预览永远等于真实 README** —— 只有一份内容，永远不会漂移。

用法：
    python tools/build_preview.py            # 生成 docs/预览.html
    python tools/build_preview.py --open     # 生成并打开浏览器
"""
import io
import os
import re
import sys
import html

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SRC = os.path.join(ROOT, "README.md")
OUT = os.path.join(ROOT, "docs", "预览.html")

CSS = """
:root{--line:#d0d7de;--ink:#1f2328;--sub:#59636e;--acc:#0969da;--card:#f6f8fa}
*{margin:0;padding:0;box-sizing:border-box}
body{background:#fff;color:var(--ink);font-size:14px;line-height:1.65;padding:32px 20px 80px;
  font-family:-apple-system,"Segoe UI","PingFang SC","Microsoft YaHei",sans-serif}
.wrap{max-width:860px;margin:0 auto}
.tip{background:#fff8c5;border:1px solid #d4a72c40;border-radius:6px;padding:9px 13px;
  font-size:12.5px;color:#4d2d00;margin-bottom:22px}
h1{font-size:25px;font-weight:600;margin:26px 0 8px;padding-bottom:6px;border-bottom:1px solid #eaeef2}
h2{font-size:19px;font-weight:600;margin:26px 0 10px;padding-bottom:5px;border-bottom:1px solid #eaeef2}
h3{font-size:16px;font-weight:600;margin:20px 0 8px}
h4{font-size:14px;font-weight:600;margin:16px 0 6px}
p{margin:0 0 10px}
ul,ol{padding-left:24px;margin-bottom:10px}
li{margin-bottom:3px}
code{background:#eff1f3;border-radius:6px;padding:2px 5px;font-size:12.5px;
  font-family:ui-monospace,Consolas,monospace}
pre{background:var(--card);border-radius:6px;padding:12px 14px;margin:10px 0;overflow-x:auto;
  font-size:12.5px;line-height:1.6;font-family:ui-monospace,Consolas,monospace}
pre code{background:none;padding:0}
table{border-collapse:collapse;width:100%;margin:12px 0;font-size:13px}
th,td{border:1px solid var(--line);padding:6px 10px;text-align:left;vertical-align:top}
th{background:var(--card);font-weight:600}
blockquote{border-left:3px solid var(--line);padding-left:12px;color:var(--sub);margin:10px 0}
blockquote p{margin-bottom:6px}
a{color:var(--acc);text-decoration:none}
a:hover{text-decoration:underline}
hr{border:none;border-top:1px solid var(--line);margin:22px 0}
img{max-width:100%}
.foot{color:var(--sub);font-size:12px;margin-top:30px;text-align:center;
  border-top:1px solid var(--line);padding-top:12px}
"""


def inline(t: str) -> str:
    t = html.escape(t, quote=False)
    t = re.sub(r"`([^`]+)`", r"<code>\1</code>", t)
    t = re.sub(r"\*\*([^*]+)\*\*", r"<strong>\1</strong>", t)
    t = re.sub(r"(?<!\*)\*([^*]+)\*(?!\*)", r"<em>\1</em>", t)
    t = re.sub(r"\[([^\]]+)\]\(([^)]+)\)", r'<a href="\2">\1</a>', t)
    t = re.sub(r"<(https?://[^>]+)>", r'<a href="\1">\1</a>', t)
    return t


def render(md: str) -> str:
    out, i = [], 0
    lines = md.split("\n")
    while i < len(lines):
        ln = lines[i]

        # 代码块
        if ln.startswith("```"):
            i += 1
            buf = []
            while i < len(lines) and not lines[i].startswith("```"):
                buf.append(html.escape(lines[i]))
                i += 1
            i += 1
            out.append("<pre><code>%s</code></pre>" % "\n".join(buf))
            continue

        # 表格
        if ln.startswith("|") and i + 1 < len(lines) and re.match(r"^\|[\s:|-]+\|$", lines[i + 1]):
            head = [c.strip() for c in ln.strip("|").split("|")]
            i += 2
            rows = []
            while i < len(lines) and lines[i].startswith("|"):
                rows.append([c.strip() for c in lines[i].strip("|").split("|")])
                i += 1
            t = ["<table><thead><tr>"] + ["<th>%s</th>" % inline(c) for c in head] + ["</tr></thead><tbody>"]
            for r in rows:
                t.append("<tr>" + "".join("<td>%s</td>" % inline(c) for c in r) + "</tr>")
            t.append("</tbody></table>")
            out.append("".join(t))
            continue

        # 标题
        m = re.match(r"^(#{1,6})\s+(.*)$", ln)
        if m:
            lv = len(m.group(1))
            out.append("<h%d>%s</h%d>" % (lv, inline(m.group(2)), lv))
            i += 1
            continue

        # 分隔线
        if re.match(r"^(-{3,}|\*{3,})$", ln.strip()):
            out.append("<hr>")
            i += 1
            continue

        # 引用
        if ln.startswith(">"):
            buf = []
            while i < len(lines) and lines[i].startswith(">"):
                buf.append(inline(lines[i].lstrip(">").strip()))
                i += 1
            out.append("<blockquote>%s</blockquote>" % "".join("<p>%s</p>" % b for b in buf if b))
            continue

        # 列表
        if re.match(r"^\s*[-*+]\s+", ln) or re.match(r"^\s*\d+\.\s+", ln):
            ordered = bool(re.match(r"^\s*\d+\.\s+", ln))
            tag = "ol" if ordered else "ul"
            items = []
            while i < len(lines) and (re.match(r"^\s*[-*+]\s+", lines[i]) or re.match(r"^\s*\d+\.\s+", lines[i])):
                items.append(re.sub(r"^\s*(?:[-*+]|\d+\.)\s+", "", lines[i]))
                i += 1
            out.append("<%s>%s</%s>" % (tag, "".join("<li>%s</li>" % inline(x) for x in items), tag))
            continue

        # 内联 HTML 块（GitHub 支持 <p align="center">…</p>；直接原样透传，不要转义）
        if ln.lstrip().startswith("<") and ln.rstrip().endswith(">"):
            # 预览位于 docs/，相对资源路径要回退一层
            out.append(ln.strip().replace('src="assets/', 'src="../assets/'))
            i += 1
            continue

        # 图片（单独成行）
        m = re.match(r'^<img\s+src="([^"]+)"[^>]*>$', ln.strip())
        if m:
            out.append('<p><img src="%s" alt=""></p>' % m.group(1))
            i += 1
            continue

        if not ln.strip():
            i += 1
            continue

        out.append("<p>%s</p>" % inline(ln))
        i += 1

    return "\n".join(out)


def main():
    if not os.path.isfile(SRC):
        print("找不到 %s" % SRC)
        return 1
    md = io.open(SRC, encoding="utf-8").read()
    body = render(md)
    page = """<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="utf-8"><title>README 预览 · Loci</title>
<style>%s</style></head><body><div class="wrap">
<div class="tip">本页由 <b>tools/build_preview.py</b> 从 <b>README.md</b> 自动生成，是真实文件渲染效果，
不是另写一份内容 —— 因此永远不会和仓库里的 README 不一致。改 README 后重新跑一次即可。</div>
%s
<div class="foot">Loci · I never forget.</div>
</div></body></html>""" % (CSS, body)
    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    io.open(OUT, "w", encoding="utf-8").write(page)
    print("已生成：%s（%d 字节）" % (OUT, len(page.encode("utf-8"))))
    if "--open" in sys.argv:
        import webbrowser
        webbrowser.open("file:///" + OUT.replace("\\", "/"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
