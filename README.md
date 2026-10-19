<p align="center"><img src="assets/icon.png" width="128" alt="Loci"></p>

<p align="center"><b>a memory palace your agents share.</b><br>你所有 AI Agent 共用的一份本地记忆。</p>

# Loci（忆宫）

[![license: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)
[![dependencies: 0](https://img.shields.io/badge/dependencies-0-brightgreen.svg)](pyproject.toml)
[![python: 3.9+](https://img.shields.io/badge/python-3.9%2B-3776AB.svg)](pyproject.toml)
[![MCP tools: 11](https://img.shields.io/badge/MCP%20tools-11-4338ca.svg)](loci.py)

[English](README_EN.md) ｜ 简体中文

零依赖 · 中文检索 · 标准 MCP · 单文件网页面板 · 数据只在本机

## 为什么做这个项目

早上在 A 里把项目背景、技术选型、踩过的坑讲清楚了；中午想试试 B 家新出的模型，切过去 —— **它一个字都不知道**，得重新讲一遍；晚上为了省额度换回 C，又得再讲一遍。

| 你是哪种用户 | 卡在哪 |
|---|---|
| **多 Agent / 多 harness 用户** | 决策原因、隐性约定、踩过的坑都留在上一个产品里，一换就失忆 |
| **模型对比测试者** | 想跑同一个任务在不同 harness / 模型下的表现，但上下文搬不过去，只能反复粘贴同一段背景 |
| **白嫖党**（靠各家免费额度过日子） | 哪家有额度用哪家，一天切换三四次 —— 每次都要重建上下文，最耗耐心 |

Loci 把记忆从 harness 里剥出来，收进一个本地 SQLite 文件，用标准 MCP 给所有 Agent 共用：

- **换 Agent 不失忆** —— 新会话调一次 `memory_context`，定过的方案、踩过的坑、说过的偏好，开局就知道；15 个产品可以一键接入
- **存结论，不堆聊天记录** —— 原文进会话库（你自己回看），决策 / 坑 / 偏好进记忆库
- **不花钱、不联网** —— 检索是本地 bigram + TF-IDF 算的：不调模型、不花 token、不发请求
- **中文真的能用** —— 中文提问、中文回答，靠本地分词命中，不是靠模型猜
- **零依赖，能读完** —— 单文件 Python + 一个 SQLite，无第三方包，不会被投毒也不会偷偷升级；约 4000 行
- **写进去的东西不会悄悄变** —— 每次写入都返回**回执**（写了什么 / 归到哪 / 出自哪段会话 / 怎么撤）；碰到重复**如实说「没有重复写入」**

<p align="center"><img src="assets/panel.png" alt="Loci 面板：会话页" width="100%"></p>

## 六个页面

### 会话 —— 每个 Agent 跟你说过什么

按 Agent 列出全部对话（标题、摘要、来源、时间），点进去是独立详情页：原话逐轮，以及从这段对话里抽出来的记忆。
右栏是「记忆来源」和「等你处理」（待质检 / 待采集 / 已排除）。本机已有的对话，扫一下就能收进来。

### 采集 —— 把散落的历史收进来

扫描本机各 Agent 的历史日志与 skills 目录，列出待入库条目，勾选后入库。
入库前做去重（含"同一天日志越写越长"那种前缀重合）和路径白名单校验；扫描根目录可增删。

### 交接卡 —— 把一个项目交给下一个 Agent

选一个项目，生成一张 markdown 交接卡：已确认的决策 / 偏好约定 / 踩过的坑 / 事实，连同所属 Git 分支一起标出。
复制走，粘给任何 Agent。

### 库 —— 记忆本体，以及它的运维

搜索、浏览全部记忆；七查质检（重复 / 疑似同义 / 可能矛盾 / 长期未更新 / 过短 / 过粗粒度 / 元数据缺失）+ 0-100 健康分；
备份、导出记忆包、恢复、销毁（销毁前强制确认，只动库、不动源文件）。

### Agent —— 谁装了、谁接上了

列出本机装了哪些 Agent、哪些已接入 Loci、各自的数据目录能不能读。
已装的点「一键接入」写入 MCP 配置（写前备份、只合并不覆盖），可移除、可验证握手。

### 技能 · MCP —— 跨 Agent 复用

本机技能清单与分布（哪个技能被几个 Agent 装着），可跨 Agent 复制；
MCP 配置查看与复制，省得每个产品手动填一遍。

## 快速开始

环境：Python 3.9+，无第三方依赖。

```bash
git clone https://github.com/kennytoliver/loci-local.git
cd loci-local
python panel.py --open
```

想双击启动：自己建个快捷方式，指向 `python panel.py --open` 就行。

面板起来后 → **Agent** 页 → 对已装的 Agent 点「一键接入」。

不想开面板，命令行装：

```bash
python install_agents.py --list     # 看本机装了什么
python install_agents.py --all      # 全部接入（写前自动备份）
python install_agents.py --verify   # 真实 MCP 握手验证
```

手动接入任何 MCP 客户端：

```json
{"command": "python", "args": ["-X", "utf8", "/path/to/loci.py"]}
```

> Windows 优先。macOS / Linux 能跑，回收站删除等少数功能降级。

页面怎么用、卡住了怎么办 —— 见 **[USAGE.md](USAGE.md)**。

## 数据在你手里

一个 SQLite 文件，无账号、无 API Key、无遥测、无云端副本。面板只监听 `127.0.0.1`，Agent 走 stdio、不经过端口。

```bash
grep -nE "^\s*(import|from) " loci.py            # 全是标准库，没有第三方
grep -nE "urlopen|requests\.|socket\." loci.py   # 引擎不联网
wc -l loci.py                                     # 约 4000 行，读得完
```

同机任何能读到这个文件的进程都能拿到全部记忆 —— 请用文件权限保护它。
外部独立审查的四轮记录见 [AUDIT-LOG.md](AUDIT-LOG.md)。

## 文件

| | |
|---|---|
| `loci.py` | 引擎 + MCP 服务 + CLI |
| `panel.py` | 单文件网页面板 |
| `install_agents.py` | 命令行接入器 |
| `loci.db` | 数据库（首次运行自动创建，已在 .gitignore 中排除） |

## 贡献

欢迎 Issue 和 PR。唯一硬性要求：**不引入第三方依赖** —— 零依赖是这个项目的立身之本。详见 [CONTRIBUTING.md](CONTRIBUTING.md)。

## 致谢

感谢 [MemForge](https://github.com/gitstq/MemForge)（MIT）先证明了「本地优先 + 单文件记忆库」这条路走得通。

## License

MIT © Loci contributors.
