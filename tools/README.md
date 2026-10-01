# tools/ —— 脚本分层说明

这个目录有 50+ 个脚本，一次改版/排障期间攒下来的。**它们不是同一层东西**，
所以按用途分三类：跑闸门的、平时用得上的、历史一次性脚本。

> 一句话：**改完代码先 `bash tools/run_gates.sh`**，其余按需。

---

## ① 闸门 —— 由 `tools/run_gates.sh` 统一调度（31 项）

```bash
bash tools/run_gates.sh            # 全部（约 7 分钟）
bash tools/run_gates.sh fast       # 跳过多秒级重型项（约 30 秒）
bash tools/run_gates.sh browser    # 只跑浏览器类
bash tools/run_gates.sh csrf       # 只跑名字含该关键字的
```

前置：面板已在 8787 运行（浏览器类闸门要它）。
`python panel.py --port 8787 --idle-exit 0`；失败日志落 `tools/.gate-logs/`。

| 脚本 | 查什么 |
|---|---|
| `check_js_syntax.js` | 内联在 `panel.py` 里的 JS 语法（正则切出 `<script>` 再 `node --check`） |
| `audit_tokens.js` | 面板样式 token / 关键选择器的静态统计 |
| `check_design.py` | **设计契约**：页面容器 id、必须存在的元素 id 清单（改 DOM 必须先看它） |
| `audit_silent_skips.py` | **静默吞异常普查**：统计 `except` 里只做 `pass` / `continue` / `return 空值` 的处数，与期望值比对，漂移即红 |
| `verify_csrf.py` | 跨站请求防护：外人 403 / 自己人放行 / 无来源头放行 |
| `verify_panel_api.py` | 面板 API 全量连通性与返回结构 |
| `verify_shutdown.py` / `verify_conn_drop.py` | 进程退出 / 连接中断行为 |
| `verify_session_*.py` | 会话层：导入流程 / 时间回填 / 同源去重 |
| `verify_scan_sources.py` / `verify_skills.py` / `verify_skill_detail.js` | 采集来源 / 技能清单 / 技能详情 |
| `verify_extract_perf.py` / `verify_audit_perf.py` | 抽取与质检的性能对拍 |
| `verify_rules_migration.py` | `AGENTS.md` 使用约定的迁移（新旧标记不打架） |
| `verify_link_shots.js` / `verify_link_measure.js` | 「会话 ↔ 记忆」关联的截图与尺寸 |
| `verify_slist_rows.js` / `verify_pane_guide.js` / `verify_folds.js` / `verify_sel_feedback.js` / `verify_frames.js` / `verify_ui_polish.js` | UI：行高 / 分栏指引 / 折叠 / 选中反馈 / 卡片框线 / 观感 |
| `measure_layers.js` / `smoke_panel.js` / `verify_content.js` | 布局尺寸 / 全页冒烟 / 内容渲染 |
| `shot_new_pages.js` | 页面截图（人工过目用） |
| `test_mcp.py` / `test_panel.py` | **在仓库根目录**（不在 tools/）：MCP 协议 / 面板单元自检 |

---

## ② 开发与发布工具（不进闸门，按需手动跑）

**发布 / 交付**
- `publish.py` —— 把本地仓库文件发布到 GitHub。本机 git 走代理连不上时改用 GitHub Contents API（已验证可行）
- `gen_usage_pdf.py` —— 生成《Loci 使用指南》PDF（纯文字流程版，便于对照录视频；依赖 reportlab）
- `build_preview.py` —— 把 `README.md` 渲染成 HTML 预览（零依赖纯标准库），避免"手写预览和真 README 不一致"
- `make_icon_blue.py` —— 由原图生成品牌蓝底图标 `assets/icon-blue.png`（原图 `icon.png` 不动）

**数据修复（一次性，跑前请先备份库）**
- `merge_dup_sessions.py` —— 合并同一 `source_path` 的重复会话
- `backfill_session_time.py` —— 从来源库回填已归档会话的 `started_at` / 每条消息时间

**排障 / 剖析**
- `profile_audit.py` —— 质检与健康度评分的性能剖析
- `verify_content_api.py` —— 「本机内容」四个端点的端到端只读校验
- `probe_inventory.py` —— 探针：数本机各 Agent 到底有多少可清算的内容（只做决策用）
- `measure.js` / `compare_layers.js` —— 量真实布局尺寸 / 生成"改前改后"同画面对比图
- `diag_skill_detail.js` —— 技能详情显示不全的定位
- `probe_borders.js` / `probe_fold.js` / `probe_layout.js` / `probe_secfold.js` / `probe_session_search.js`
  —— 边框 / 折叠 / 布局 / 区域收起 / 会话搜索的专项探针
- `snapshot_pages.js` —— 8 页 × 明暗双主题 = 16 张基线截图
- `shot_bugshots.js` / `shot_content_plugins.js` / `shot_sel_feedback.js` —— 现场截图

**健壮性 / 安全探测**（2026-09-30 由一次外部独立测试引入，全部用**临时库**，不碰真库）

| 脚本 | 探什么 | 跑法 |
|---|---|---|
| `probe_mcp_connect.py` | MCP 接入 + 10 个工具冒烟（协议层 / 正常路径 / 错误路径，48 项） | `python tools/probe_mcp_connect.py` |
| `probe_bugs_repro.py` | 缺陷复现：检索门槛 / `memory_save` 参数校验 / `session_save` 解析 | `python tools/probe_bugs_repro.py` |
| `probe_panel_api.py` | 面板 GET 接口烟测 + 畸形参数健壮性 | **必须传 URL**：`python tools/probe_panel_api.py http://127.0.0.1:8787` |
| `probe_panel_post.py` | POST 接口健壮性（错误输入应被拦住，不该掐断连接） | 同上，URL 必传 |
| `probe_panel_frontend.py` | 前端自检：抓 HTML 里的 `onclick/onchange` 引用，核对 JS 函数都有定义 | `python tools/probe_panel_frontend.py` |
| `probe_concurrency.py` | 并发写压力：多进程同时经 MCP 写同一库（查 `database is locked` / 丢写） | `python tools/probe_concurrency.py` |
| `probe_csrf.py` | 跨站可构造写请求验证 | ⚠️ **会真写一条哨兵记忆再删**，只能用测试端口：<br>`python panel.py --port 8799 --idle-exit 60` 后 `python tools/probe_csrf.py http://127.0.0.1:8799` |

> ⚠️ **两个面板探针的 `BASE` 默认端口是 8799**，不是 8787。
> 不打 URL 参数时会连 8799 而报"连接被拒绝"，看起来像面板没起来 —— 白查半天。
> 另外本机 HTTP 探测要绕开沙箱代理：`unset http_proxy https_proxy` + `no_proxy=127.0.0.1,localhost`。

---

## ③ 历史脚本（一次性迁移，**别再在现在的仓库上跑**）

这些是品牌/路径迁移时用过的一次性脚本，留在这里是**迁移史的实物证据**，
但它们假设的是"迁移前"的仓库状态 —— 现在跑会改坏文件。

| 脚本 | 曾经做什么 |
|---|---|
| `rename_brand.py` | 对外文档层的品牌名批量替换（MemHub → HippoHub → Hippocampus → **Loci**） |
| `rename_paths.py` | 本地路径与标识符改名（`hippocampus.*` → `loci.*`，含环境变量 / MCP 服务名 / 包格式名） |

> 品牌演进史见 `CHANGELOG.md` 的 `[0.4.0]` 一节。

---

## 约定

1. **新增闸门**请登记进 `tools/run_gates.sh` 的 `GATES` 数组 —— 否则它永远不会被跑。
2. **一次性脚本**用完请挪到本节 ③ 的说明里，或直接删除（别留在 ① ② 里误导人）。
3. 名字以下划线开头（`_chk_*.js` 等）的临时脚本已被 `.gitignore` 忽略，不会进仓库。
