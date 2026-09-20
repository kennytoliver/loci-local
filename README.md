<p align="center"><img src="assets/icon.png" width="128" alt="Hippocampus"></p>

<p align="center"><b>I never forget.</b><br>一个本地记忆中枢，让你的所有 AI Agent 共享同一份记忆。</p>

# Hippocampus（海马体）— 个人跨 Agent 记忆中枢

[English](README_EN.md) ｜ 简体中文

零依赖单文件 · 中文友好检索 · 通用 MCP 接入 · 单文件网页面板 · 数据 100% 本地

给你的所有 AI Agent 装一个**共用的大脑皮层**：WorkBuddy / Claude Code / Codex / 任何支持 MCP 的工具，读写同一份本地记忆。Agent 可以换，记忆不能丢。

## 为什么做这个项目

### 痛点：每换一个 harness，你就得从头解释一遍自己

同时用几个 AI 编程产品的人（Claude Code、Codex、ZCode、Kimi、Trae、WorkBuddy…），一定经历过这个循环：

- 早上在 A 里把项目背景、技术选型、踩过的坑讲清楚了；
- 中午想试试 B 家新出的模型，切过去 —— **它一个字都不知道**，得重新讲一遍；
- 晚上为了省额度换回 C，又得再讲一遍。

具体到不同的人，卡点还不一样：

| 你是哪种用户 | 卡在哪 |
|---|---|
| **多 harness 用户** | 决策原因、隐性约定、踩过的坑都留在上一个产品里，一换就失忆 |
| **模型对比测试者** | 想跑同一个任务在不同 harness / 模型下的表现，但上下文搬不过去，只能反复粘贴同一段背景 |
| **白嫖党**（靠各家免费额度过日子） | 哪家有额度用哪家，一天切换三四次 —— 每次都要重建上下文，最耗耐心 |

更麻烦的是：即使同一个产品，**换个会话也可能丢上下文**（取决于它自己的会话管理），而你没有任何办法把它导出来、存起来、带走。

### Hippocampus 的做法：把记忆从 harness 里剥出来

记忆不该属于某个产品，它属于你。Hippocampus 把这件事做成本地的一个 SQLite 文件，通过标准 MCP 暴露给所有 Agent：

- **换 harness 不再重置**：新 Agent 一开口调 `memory_context`，就能拿到你的常驻决策、偏好、趟过的坑；
- **反向也通**：任何 Agent 写下的东西（比如在 ZCode 里说一句"记住…"），其他 Agent 都能搜到 —— 这条链路已实测跑通；
- **不是"另一个云服务"**：没有账号、没有 API Key、没有遥测，数据只有一个文件，躺在你自己的硬盘上。

三个技术上的差异化：

1. **中文检索真的能用**：bigram 分词 + TF-IDF，中文查询命中率 5/5；
2. **标准 MCP 接入**：任何支持 MCP 的 Agent 即插即用，不需要每个产品单独适配；
3. **零依赖纯 Python**：单文件引擎 + 单文件面板，克隆下来就能跑。

版本变更见 [CHANGELOG.md](CHANGELOG.md)。

## 功能

| 模块 | 说明 |
|---|---|
| 10 个 MCP 工具 | `memory_save` / `memory_search` / `memory_list` / `memory_delete` / `memory_stats` / `memory_handoff` / `session_save` / `session_recall` / `memory_context` / `memory_pin` |
| 项目交接卡 | `memory_handoff` 按项目抽取决策/偏好/坑/事实，生成 markdown 注入下一个 Agent |
| 网页面板 | 深色工作台：统计卡片、中文搜索、新增/删除记忆、交接卡生成、记忆包导出导入、本机 Agent 体检 |
| 记忆包 | 导出为 JSON 文件，可在不同电脑/不同人之间交换记忆（导入自动去重） |
| 常驻记忆 | 标记「常驻」的记忆始终随 `memory_context` 返回；Agent 开聊调一次即获得跨 Agent 上下文（学 Letta core memory） |
| 记忆质检 | **七查 + 健康度评分**：重复（合并）/ 疑似同义 / 可能矛盾（以新代旧）/ 长期未更新 / 过短 / 过粗粒度（一键拆分）/ 元数据缺失；0-100 健康分；可导出 Markdown 质检报告（作废记忆保留不删除，学 Zep） |
| 记忆转技能 | 把项目记忆导出成 SKILL.md 写进各 Agent 的 skills 目录（Mem2Skill 思路） |
| 会话→记忆 | 从归档会话按规则抽取候选记忆（决策/坑/偏好/事实），勾选入库，零 LLM 依赖 |
| 会话层 | 归档对话原文（会话库存过程与原话，记忆库存结论），支持粘贴/文件/JSONL 导入、时间线回看、原话检索，重复内容自动去重 |
| 采集中心 | 扫描本机 Agent 历史日志与 skills，人工勾选后入库（去重+白名单校验） |
| Agent 体检 | 面板自动检测本机安装了哪些 Agent、哪些已接入 Hippocampus |
| 一键接入 | 覆盖 15 个产品：WorkBuddy / ZCode / Kimi Code / DeepSeek CLI / Trae / TraeWork / VS Code / Cursor / Windsurf / Gemini CLI / Qoder / CodeBuddy 等（ZCode 使用嵌套 mcp.servers 结构）；写入前自动备份、只合并不覆盖、可移除 |
| CLI | 交互式命令行 + 单条命令两种模式 |

## 快速开始

**环境要求**：Python 3.9+，零第三方依赖。

```bash
git clone https://github.com/<你的名字>/hippocampus.git
cd hippocampus
```

**方式一：网页面板（推荐）**

```bash
python panel.py --open        # 自动打开浏览器
# 或
python panel.py --port 8787   # 手动访问 http://127.0.0.1:8787
```

> **面板不常驻**：页面开着就算在用；关掉浏览器页面后 5 分钟无访问会自动退出。
> 想让它一直开着：`python panel.py --idle-exit 0`。（记忆本体不受影响 —— Agent 照常读写）

**方式二：接入 Agent（MCP）**

最省事：打开面板 → **Agent 体检** → 对已安装的 Agent 点「**一键接入**」。面板会自动写入对应的 MCP 配置（先备份、只合并不覆盖），并提供「移除接入」与「验证 MCP 服务」。

手动配置（面板未覆盖的客户端）：

Claude Code：
```bash
claude mcp add hippocampus -- python -X utf8 /path/to/hippocampus.py
```

其他 MCP 客户端：command 指向你的 Python，args 为 `["-X", "utf8", "/path/to/hippocampus.py"]`，环境变量 `HIPPOCAMPUS_DB` 可指定数据库位置（默认 hippocampus.py 同目录）。

**方式二·补充：命令行安装器（不用面板）**

```bash
python install_agents.py --list        # 扫描本机装了哪些 Agent
python install_agents.py --all         # 一键接入全部（写前自动备份）
python install_agents.py --verify      # 真实 MCP 握手验证
python install_agents.py --rules       # 写入「记忆使用约定」，让 Agent 开局先调 memory_context
```

**方式三：命令行**

```bash
python hippocampus.py --cli                                # 交互模式
python hippocampus.py --save "写操作先落库再失效" --type skill --proj 架构
python hippocampus.py --search "缓存怎么失效"
python hippocampus.py --stats
```

## 测试

```bash
python test_mcp.py     # 引擎端到端测试：握手/工具列表/写入/中文检索/交接卡/统计/负例
python test_panel.py   # 面板回归测试：JS 语法守卫/元素完整性/路由齐全/零依赖
```

## 文件说明

| 文件 | 作用 |
|---|---|
| `hippocampus.py` | 核心引擎 + MCP Server + CLI（约 1,400 行，零依赖） |
| `panel.py` | 单文件网页面板（零依赖，复用 hippocampus.py） |
| `test_mcp.py` | 引擎端到端测试 |
| `test_panel.py` | 面板回归测试（JS 语法守卫） |
| `hippocampus.db` | SQLite 数据库（首次运行自动创建；含个人数据，已在 .gitignore 中排除） |

## 隐私

所有数据保存在本地 SQLite 文件中，不联网、不上云、无遥测。面板只监听 `127.0.0.1`。

**请知悉**：这是本机单人使用的工具。数据库是一个普通文件，因此**同机任何能读到它的进程都能拿到全部记忆内容** —— 「跨 Agent 可见」和「无访问隔离」是同一件事的两面。若你机器上有多个用户或不信任的进程，请自行用文件权限保护该文件。

## 贡献

欢迎提 Issue 和 PR —— 唯一硬性要求：**不能引入第三方依赖**（零依赖是这个项目的立身之本）。详见 [CONTRIBUTING.md](CONTRIBUTING.md)。

## 致谢

感谢 [MemForge](https://github.com/gitstq/MemForge)（MIT）—— 它先证明了「本地优先 + 单文件记忆库」这条路走得通，给了我不少启发。

顺着这条路，我想再往前推一步：**把中文检索做扎实，用 MCP 接入更多 AI 工具，让记忆能在不同 harness 之间延续下去。**

## License

MIT © Hippocampus contributors.
