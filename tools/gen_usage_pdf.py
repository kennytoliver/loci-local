# -*- coding: utf-8 -*-
"""生成《Loci 使用指南 · 功能流程版》PDF。
纯文字、按功能讲操作流程，无截图，便于对照录视频。
依赖：reportlab（已装）。中文字体用系统本地字体（黑体/宋体）。
"""
import os
from reportlab.lib.pagesizes import A4
from reportlab.lib.units import mm
from reportlab.lib import colors
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.lib.enums import TA_CENTER, TA_LEFT
from reportlab.platypus import (SimpleDocTemplate, Paragraph, Spacer, Table,
                                TableStyle, ListFlowable, ListItem, PageBreak,
                                HRFlowable)
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont

FONT_HEI = "C:/Windows/Fonts/simhei.ttf"
FONT_SONG = "C:/Windows/Fonts/simsun.ttc"
pdfmetrics.registerFont(TTFont("Hei", FONT_HEI))
pdfmetrics.registerFont(TTFont("Song", FONT_SONG, subfontIndex=0))

NAVY = colors.HexColor("#1f3a5f")
ACCENT = colors.HexColor("#2b6cb0")
GREY = colors.HexColor("#555555")
LIGHT = colors.HexColor("#eef3f8")

ss = getSampleStyleSheet()
def mk(name, **kw):
    base = dict(fontName="Song", fontSize=10.5, leading=17, textColor=colors.black)
    base.update(kw)
    return ParagraphStyle(name, **base)

S_TITLE = mk("t", fontName="Hei", fontSize=26, leading=32, alignment=TA_CENTER, textColor=NAVY)
S_SUB = mk("sub", fontName="Hei", fontSize=13, leading=20, alignment=TA_CENTER, textColor=GREY)
S_SLOGAN = mk("slog", fontName="Hei", fontSize=14, leading=22, alignment=TA_CENTER, textColor=ACCENT)
S_H1 = mk("h1", fontName="Hei", fontSize=16, leading=24, textColor=NAVY, spaceBefore=10, spaceAfter=6)
S_H2 = mk("h2", fontName="Hei", fontSize=12.5, leading=20, textColor=ACCENT, spaceBefore=8, spaceAfter=3)
S_BODY = mk("body", spaceAfter=4)
S_STEP = mk("step", spaceAfter=2)
S_TIP = mk("tip", fontName="Song", fontSize=9.5, leading=14, textColor=GREY, leftIndent=8)
S_CELL = mk("cell", fontSize=9.5, leading=14)
S_CELLH = mk("cellh", fontName="Hei", fontSize=9.5, leading=14, textColor=colors.white)
S_NOTE = mk("note", fontName="Song", fontSize=9, leading=13, textColor=GREY, alignment=TA_LEFT)

def P(t, s=S_BODY):
    return Paragraph(t, s)

def steps(items, s=S_STEP):
    return ListFlowable(
        [ListItem(P(i, s), leftIndent=14, value=n) for n, i in enumerate(items, 1)],
        bulletType="1", bulletFontName="Hei", bulletFontSize=10.5, leftIndent=16)

def bullets(items, s=S_BODY):
    return ListFlowable(
        [ListItem(P(i, s), leftIndent=12) for i in items],
        bulletType="bullet", bulletFontName="Hei", bulletColor=ACCENT, leftIndent=14)

def table(data, col_widths, header=True):
    rows = []
    for r in data:
        rows.append([P(c, S_CELLH if (header and i == 0) else S_CELL) for c in r] if False else
                    [P(c, S_CELL) for c in r])
    t = Table(rows, colWidths=col_widths, repeatRows=1 if header else 0)
    style = [
        ("GRID", (0, 0), (-1, -1), 0.5, colors.HexColor("#bcc7d2")),
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, LIGHT]),
        ("TOPPADDING", (0, 0), (-1, -1), 4),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
        ("LEFTPADDING", (0, 0), (-1, -1), 6),
        ("RIGHTPADDING", (0, 0), (-1, -1), 6),
    ]
    if header:
        style += [("BACKGROUND", (0, 0), (-1, 0), NAVY)]
    t.setStyle(TableStyle(style))
    return t

story = []

# ---------- 封面 ----------
story.append(Spacer(1, 40 * mm))
story.append(P("Loci 使用指南", S_TITLE))
story.append(P("跨 Agent 本地记忆中枢 · 功能流程版", S_SUB))
story.append(Spacer(1, 8 * mm))
story.append(P("I never forget.", S_SLOGAN))
story.append(Spacer(1, 6 * mm))
story.append(P("—— 会话存原话，记忆存结论。Agent 换来换去，结论跟着你走。", S_NOTE))
story.append(Spacer(1, 10 * mm))
story.append(HRFlowable(width="60%", color=ACCENT, thickness=1))
story.append(Spacer(1, 4 * mm))
story.append(P("用途：熟悉每个功能的操作流程，便于录制讲解视频。本文为纯文字流程说明，不含截图。", S_NOTE))
story.append(PageBreak())

# ---------- 0. 如何启动 ----------
story.append(P("零、如何启动面板", S_H1))
story.append(P("在录视频或日常使用前，先把面板跑起来：", S_BODY))
story.append(steps([
    "确认本机已安装项目要求的 Python 版本，并 clone / 解压好 Loci 项目目录。",
    "Windows 用户：直接双击项目根目录的 <b>loci-panel.bat</b> 即可启动；",
    "或命令行启动：在项目目录下执行 <b>python panel.py --port 8787</b>（常驻可加 <b>--idle-exit 0</b>）。",
    "打开浏览器，访问 <b>http://127.0.0.1:8787</b>，即可看到左侧 8 个功能入口的面板。",
    "关闭面板：直接关掉浏览器即可；常驻进程可在任务管理器结束，或等其 idle 自动退出。",
]))
story.append(P("数据就一个本地文件（loci.db），全部在你自己电脑上，不上云。", S_TIP))
story.append(PageBreak())

# ---------- 1. 核心心智模型 ----------
story.append(P("一、先建立核心心智模型（贯穿全篇）", S_H1))
story.append(P("Loci 把信息分两层，这是理解后面所有功能的前提：", S_BODY))
story.append(table([
    ["类型", "存什么", "作用"],
    ["会话层（原话）", "你和 AI 的完整对话原文", "想找回“当时 AI 到底说了什么”时，用原文检索"],
    ["记忆层（结论）", "你沉淀下来的结论：决策 / 偏好 / 事实 / 经验 / 踩坑 / 背景 / 摘要", "Agent 开局直接读取，省 token、立即可用"],
], [28 * mm, 62 * mm, 80 * mm]))
story.append(Spacer(1, 4 * mm))
story.append(P("用一个贯穿全篇的例子：你让 AI 帮你做个人博客网站。聊了几天、换了三个 Agent 之后，忆宫里会沉淀出：", S_BODY))
story.append(table([
    ["记忆类型", "内容（真实示例）", "作用"],
    ["决策", "技术选型：静态博客，不上数据库", "我为什么这样做"],
    ["偏好", "代码注释一律用中文", "我喜欢的方式"],
    ["事实", "域名 example.com，年费 80 元，2027-09 到期", "客观信息"],
    ["经验", "部署流程：先本地构建 → 再上传 dist 目录", "怎么做的方法"],
    ["踩坑", "GitHub Pages 改 DNS 后要等 10 分钟才生效", "交过学费的教训"],
    ["背景", "整段工作日志（通常由“采集”自动入库）", "完整上下文备查"],
    ["摘要", "长文档的压缩版", "省地方"],
], [24 * mm, 86 * mm, 60 * mm]))
story.append(Spacer(1, 3 * mm))
story.append(P("为什么要分两层？Agent 开局需要的是“结论”——省 token、直接可用；几十万字聊天记录塞给它没意义。但你想找“当时 AI 到底说了什么”时，用会话层的原文检索。", S_TIP))
story.append(PageBreak())

# ---------- 2. 八大功能操作流程 ----------
story.append(P("二、八大功能 · 操作流程（重点）", S_H1))
story.append(P("左侧 8 个入口，每个解决一类问题。下面按“是什么 → 怎么操作 → 新手注意”逐个讲。", S_BODY))

story.append(P("2.1 记忆（结论层）", S_H2))
story.append(P("一句话：记忆库，存放你沉淀下来的结论。查“我定了什么技术栈”、把重要记忆设为常驻，都在这。", S_BODY))
story.append(steps([
    "点左侧「记忆」入口，进入记忆库列表。",
    "列表按类型分组、按重要度（1–4 星）排序，扫一眼就能找到关键结论。",
    "点任意一条记忆，查看详情：内容、来源（来自哪个 Agent / 采集 / 导入）、重要度、创建时间。",
    "设为常驻：点该记忆的「设为常驻」，它会立刻进入页面顶部“常驻上下文”，以后每次开聊 AI 自动带上看。",
    "新建记忆：点「新建」→ 选类型（见第三章）→ 填内容 → 设重要度星标 → 保存。",
    "搜索：用顶部搜索框输入关键词（中文搜索已专门优化），快速定位某条记忆。",
]))
story.append(P("新手注意：常驻记忆 = 固定置顶、每次必带；别把所有记忆都设常驻，会稀释重点。", S_TIP))

story.append(P("2.2 会话（原话层）", S_H2))
story.append(P("一句话：对话原文存档。回看当时完整对话、搜“DNS”找出 AI 原话，都在这。", S_BODY))
story.append(steps([
    "点左侧「会话」入口。",
    "先选来源（如 ZCode / WorkBuddy / Trae 等本机 Agent）。",
    "选中某段会话，查看完整对话原文（你说了什么、AI 回了什么）。",
    "在搜索框输入关键词，定位到某句原话；需要引用时直接复制。",
]))
story.append(P("新手注意：会话层是“原话”，记忆层是“结论”。找原话用会话，找结论用记忆。", S_TIP))

story.append(P("2.3 质检（记忆体检）", S_H2))
story.append(P("一句话：给记忆库做体检。健康分掉了？看有没有重复、过期、超长的记忆，一键拆分超长日志。", S_BODY))
story.append(steps([
    "点左侧「质检」入口，查看当前记忆库健康分。",
    "系统会列出问题项：重复记忆、过期记忆、超长日志等。",
    "对重复项：合并或删除冗余的那条。",
    "对超长日志：用“一键拆分”把它拆成多条结构化的小记忆。",
    "处理完，观察右上角健康分实时变化，确认改善。",
]))
story.append(P("新手注意：健康分是给记忆库“瘦身”的指标，定期跑一次，别让重复/过期记忆堆积。", S_TIP))

story.append(P("2.4 清理（删除 + 备份）", S_H2))
story.append(P("一句话：删除不要的内容，并做备份与归档。删记忆/源文件先进回收站，不立刻销毁。", S_BODY))
story.append(steps([
    "点左侧「清理」入口。",
    "勾选要删除的记忆或源文件，执行删除（先进回收站，可恢复）。",
    "在“备份与归档”区域，对重要数据进行备份，避免误删后找不回。",
    "需要彻底清空时，再从回收站二次确认删除。",
]))
story.append(P("新手注意：删除走回收站，不是物理销毁，给误操作留了后悔药；备份习惯要养成。", S_TIP))

story.append(P("2.5 采集（挖本机历史）", S_H2))
story.append(P("一句话：从你过去在 ZCode 等工具里的对话中，把有价值的内容挖出来入库。", S_BODY))
story.append(steps([
    "点左侧「采集」入口。",
    "选择要扫描的本机 Agent（如 ZCode 等）。",
    "运行扫描，工具会把历史对话整理成候选记忆。",
    "逐条挑选“有用”的，确认入库；没价值的跳过。",
]))
story.append(P("新手注意：采集是“补历史账”——你之前没接忆宫时的对话，现在一次性补进来。", S_TIP))

story.append(P("2.6 Agent（接入管理）", S_H2))
story.append(P("一句话：把忆宫接进各个 AI 工具，并验证它们是否可用。", S_BODY))
story.append(steps([
    "点左侧「Agent」入口。",
    "看已支持的 AI 工具列表，一键执行“接入”把忆宫挂到对应工具上。",
    "用内置验证，确认 10 个工具是否都可用（状态灯绿/红）。",
    "有接不上的，按提示排查（通常是路径或配置文件问题）。",
]))
story.append(P("新手注意：接入一次，之后在该工具里聊，记忆就自动沉淀，不用手动搬运。", S_TIP))

story.append(P("2.7 记忆包（导出 / 导入）", S_H2))
story.append(P("一句话：换电脑时，把全部记忆打包带走（一个 .json 文件）。", S_BODY))
story.append(steps([
    "点左侧「记忆包」入口。",
    "导出：选项目 → 点导出 → 浏览器“另存为”选 U 盘，得到一个 .json。",
    "导入（新电脑）：装 Python → clone 项目 → 开面板 → 记忆包页「选择文件」→ 选那个 json。",
    "系统自动按内容去重，已存在的不重复导入（来源会标成 pack-import）。",
]))
story.append(P("新手注意：记忆包只含“结论（记忆）”，不含会话层原话；原话要换电脑后重跑“采集”。", S_TIP))

story.append(P("2.8 交接卡（给下一个 Agent 的说明书）", S_H2))
story.append(P("一句话：新开对话时，把项目卡片发给 AI，省得重新解释一遍背景。", S_BODY))
story.append(steps([
    "点左侧「交接卡」入口。",
    "选择一个项目，点「生成」。",
    "系统按类型（决策 / 偏好 / 踩坑 / 事实 / 经验 / 背景 / 摘要）分组生成一段 Markdown 卡片。",
    "复制整段文字，粘贴到新开的 AI 对话里，它立刻“懂了”你之前定了什么。",
]))
story.append(P("新手注意：交接卡是“粘贴即用”，对方不需要装任何东西，最适合开新对话时用。", S_TIP))
story.append(PageBreak())

# ---------- 3. 记忆 7 种类型 + 新建流程 ----------
story.append(P("三、记忆的 7 种类型 · 与新建一条记忆的操作流程", S_H1))
story.append(table([
    ["面板显示", "英文标识", "什么时候用"],
    ["事实 fact", "fact", "客观信息：数字、日期、名称"],
    ["决策 decision", "decision", "做过的选择 + 理由"],
    ["偏好 preference", "preference", "你的习惯和要求"],
    ["经验 skill", "skill", "做某事的方法 / 流程"],
    ["踩坑 error", "error", "教训（最值钱，少走弯路）"],
    ["背景 context", "context", "长篇说明（多为采集自动生成）"],
    ["摘要 summary", "summary", "长内容的压缩"],
], [40 * mm, 40 * mm, 90 * mm]))
story.append(Spacer(1, 3 * mm))
story.append(P("新建一条记忆的标准操作流程：", S_BODY))
story.append(steps([
    "进入「记忆」页，点「新建」。",
    "在类型下拉框里选对类型（搞不清就选“背景”或“事实”）。",
    "填写内容；如果是决策/踩坑，把“理由/教训”也写进去，价值更高。",
    "设重要度星标（1–4 星）；重要的设 3–4 星，可顺手“设为常驻”。",
    "保存。之后在记忆库列表和搜索里都能找到它。",
]))
story.append(P("新手注意：踩坑类最值钱，遇到教训务必记一条；不要什么都记成“背景”，分类越准越好检索。", S_TIP))
story.append(PageBreak())

# ---------- 4. 拍视频讲解框架 ----------
story.append(P("四、拍视频讲解框架（60 秒脚本）", S_H1))
story.append(P("建议按下面顺序讲，逻辑最顺：", S_BODY))
story.append(table([
    ["段落", "时长", "讲什么"],
    ["1 抛痛点", "15 秒", "“早上在 A 工具聊清楚的事，中午换 B 它全忘了——每换一个 AI 都要重新解释一遍自己。”"],
    ["2 给方案", "10 秒", "“所以我做了这个：本地记忆库，所有 AI 共享。数据就一个文件，在你自己电脑上。”"],
    ["3 核心演示", "20 秒", "打开面板→展示常驻记忆分组→切到 ZCode 问“我之前的技术栈是什么”→它直接答出；关键一帧：记忆详情来源显示 ZCode、搜索分数远超其他"],
    ["4 收尾", "15 秒", "“零依赖、纯 Python、不开面板也不占后台。Agent 关了它就关了，记忆都在本地。”"],
], [26 * mm, 18 * mm, 126 * mm]))
story.append(Spacer(1, 3 * mm))
story.append(P("避坑提示：别讲“TF-IDF”“bigram”这类词，观众听不懂；就说“中文搜索专门优化过”。那句 slogan 可以念出来：<b>I never forget.</b>", S_TIP))
story.append(Spacer(1, 3 * mm))
story.append(P("视频里可以现场演示的 3 个动作（带步骤）：", S_H2))
story.append(steps([
    "设常驻：点任意记忆 → 设为常驻 → 顶部“常驻上下文”里立刻出现。",
    "健康分：质检页 → 处理一条重复 / 超长记忆 → 分数实时变化。",
    "交接卡：选个项目 → 生成 → 把卡片内容粘给另一个 AI → 它立刻“懂了”。",
]))
story.append(PageBreak())

# ---------- 5. 记忆包 vs 交接卡 ----------
story.append(P("五、记忆包 vs 交接卡（最容易搞混的两个）", S_H1))
story.append(table([
    ["", "记忆包", "交接卡"],
    ["形态", "一个 .json 文件", "一段 Markdown 文字"],
    ["给谁", "给另一台电脑上的忆宫", "给另一个 AI Agent"],
    ["怎么用", "面板里“选择文件”导入", "复制粘贴到对话里"],
    ["场景", "换电脑 / 备份 / 给同事", "开新对话时让 AI 立刻懂"],
    ["需要对方装东西吗", "需要", "不需要（粘贴即用）"],
], [34 * mm, 68 * mm, 68 * mm]))
story.append(Spacer(1, 4 * mm))
story.append(P("记忆包：换电脑的完整流程", S_H2))
story.append(steps([
    "旧电脑：记忆包页 → 选项目 → 导出 → 浏览器“另存为”选 U 盘。",
    "U 盘上得到一个 .json。",
    "新电脑：装 Python → clone 项目 → 开面板。",
    "记忆包页 → 「选择文件」→ 选 U 盘上那个 json。",
    "自动按内容去重，已存在的不重复导入（来源标成 pack-import）。",
    "注意：记忆包只含“结论”，不含原话；换电脑后要原话，在新电脑再跑一次“采集”。",
]))
story.append(P("交接卡：开新对话时省掉重新解释", S_H2))
story.append(steps([
    "生成的 Markdown 按类型分组（决策 / 偏好 / 踩坑 / 事实 / 经验 / 背景 / 摘要）。",
    "直接贴给新开的 AI 对话，它立刻知道你之前定了什么。",
    "拍视频 Tip：交接卡最容易出效果——“粘贴一张卡片，AI 立刻说：我知道了，按你之前定的静态博客方案来。”",
]))
story.append(Spacer(1, 4 * mm))
story.append(P("说明：本文按钮名称以当前面板版本为准；功能逻辑与上方一致。", S_NOTE))

# ---------- 页脚 ----------
def footer(canvas, doc):
    canvas.saveState()
    canvas.setFont("Song", 8)
    canvas.setFillColor(GREY)
    canvas.drawString(20 * mm, 12 * mm, "Loci 使用指南 · 功能流程版")
    canvas.drawRightString(A4[0] - 20 * mm, 12 * mm, "第 %d 页" % doc.page)
    canvas.restoreState()

OUT = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                   "docs", "使用指南-功能流程版.pdf")
doc = SimpleDocTemplate(OUT, pagesize=A4,
                        leftMargin=20 * mm, rightMargin=20 * mm,
                        topMargin=18 * mm, bottomMargin=18 * mm,
                        title="Loci 使用指南 · 功能流程版",
                        author="Loci")
doc.build(story, onFirstPage=footer, onLaterPages=footer)
print("OK ->", OUT)
