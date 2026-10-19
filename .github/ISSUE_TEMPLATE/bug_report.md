---
name: Bug 报告
about: 报告一个可以复现的问题
title: "[Bug] "
labels: bug
---

## 环境

- 操作系统：<!-- Windows 11 / macOS 14 / Ubuntu 22.04 … -->
- Python 版本：<!-- python --version -->
- Loci 版本：<!-- 面板页脚，或 python -c "import loci;print(loci.APP_VERSION)" -->
- 用的哪个 Agent：<!-- WorkBuddy / Claude Code / Cursor / ZCode / Kimi Code / … -->

## 复现步骤

1.
2.
3.

## 期望结果

## 实际结果

## 补充

- 面板上的问题：附浏览器控制台（F12）的报错
- MCP / Agent 侧的问题：附该 Agent 的 MCP 日志
- 方便的话跑一下自检并贴结果：

```bash
python test_mcp.py
python test_panel.py
```

> ⚠️ 贴日志前请**删掉真实路径、项目名、记忆正文**等私人内容。
> 本项目**零依赖、无遥测**，但也请你自己把关不要贴出敏感信息。
