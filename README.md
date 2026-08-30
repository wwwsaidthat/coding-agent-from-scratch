# Coding Agent from Scratch

一个从零实现的轻量级本地编程智能体，Web 产品名为 **LoopCoder**。模型负责决策，程序自行维护对话历史、解析 tool calls、执行本地文件与命令工具，并循环工作直到完成任务。当前项目版本为 `0.4.0`。

本项目**不使用任何 agent 框架或 Agent SDK**，也不依赖 API 服务端托管的代码执行或文件工具。

## 当前能力

- DeepSeek 兼容 Chat Completions 接入，默认主模型为 `deepseek-v4-pro`，可通过环境变量替换；
- 可选的通义千问能力，默认 `qwen3.6-flash`，负责图片、PDF 理解与联网搜索；
- 模型 - 工具 - 结果 - 模型的自主执行循环；
- `rg` 驱动的专业文件发现与代码检索，结构化返回文件、行号、列号和截断状态；
- 强制先读后改、文件版本哈希冲突检测、原子写入和原子 `multi_edit`；
- 真实 CLI 与 Web 模式下，每次修改先展示统一 Diff，红色删除、绿色新增，由用户明确同意后才写入；
- 不经过系统 Shell 的本地命令执行；
- 路径越界防护、敏感文件拦截、命令白名单和超时；
- DeepSeek 对 429、5xx、连接错误和超时进行有限重试，并具备参数校验、异常结果回传、重复调用检测和最大步数限制；
- 无需 API key 的离线演示模式；
- 可视化 Web 工作台，可输入 Prompt、设置工作区并逐轮查看公开的 Thought 决策摘要、Action 和 Observation；
- 本地前后端 API，支持任务状态、步数/工具统计、停止请求和最终结果；
- 类 Codex 的多轮会话侧边栏：可继续追问、切换历史会话，并在服务重启后恢复；
- 会话可在侧边栏中永久删除，同时清理对应消息、上下文、Trace 和会话上传附件；
- 会话与工作区一一绑定，切换会话时同时切换上下文和可访问目录；
- 五层上下文：固定规则、结构化任务检查点、近期原文、历史摘要与可回查原始记录；
- 真实 Token 用量校准：首次保守估算，随后根据 API `prompt_tokens` 自动校准并预留回答空间；
- 70% / 82% / 92% 分级压缩，按搜索、命令、网页、文档和 JSON 类型保留最重要信息；
- 在高压力阶段里程碑处使用同一主模型生成结构化语义摘要，不影响主任务失败处理；
- 超过历史压缩阈值的工具原文按 SHA-256 保存在本地，可用 `read_tool_result` 精确分页回查被省略区间；
- 持久化 Trace：模型请求、完整工具参数/结果、耗时、错误码、Token 用量和最后一次测试结果；
- Web 模式下，复杂任务可建立 Plan，逐步显示待处理、进行中和已完成状态；
- 点击任意一轮聊天消息即可查看该轮对应的计划、指标和执行轨迹；
- Python 运行时代码仅使用标准库；代码检索和 PDF 渲染分别依赖本机的 `rg` 与 Poppler 命令。

## 工作原理

```text
用户任务
   ↓
构造系统提示词、历史消息和工具定义
   ↓
调用 DeepSeek Chat Completions API
   ↓
模型返回最终文本？ ── 是 ──→ 输出结果并结束
   │
   否（返回 tool calls）
   ↓
校验参数 → 在本地执行工具 → 将结果写入历史
   └───────────────────────────────↑
```

核心逻辑位于：

- `src/coding_agent/agent.py`：Agent 循环和终止条件；
- `src/coding_agent/models.py`：DeepSeek API 调用与输出解析；
- `src/coding_agent/conversation.py`：多轮历史、结构化记忆和分级上下文管理；
- `src/coding_agent/context_compression.py`：按工具类型压缩模型可见结果；
- `src/coding_agent/tools/`：工具定义、参数校验、本地执行与完整结果归档；
- `src/coding_agent/webapp.py`：本地 HTTP API、任务管理与静态资源服务；
- `src/coding_agent/web/`：可视化前端页面。

更详细的设计说明见 [`docs/architecture.md`](docs/architecture.md)。

## 环境要求

- Python 3.10 或更高版本；
- 使用真实模型时需要 DeepSeek API key；
- `ripgrep`（命令 `rg`），用于 `find_files` 和 `search_code`；
- Poppler（命令 `pdfinfo` 和 `pdftoppm`），用于在本地校验并渲染 PDF；
- 项目代码不绑定特定桌面系统；实际可用命令以及 `rg`、Poppler 的安装方式取决于本机环境。

## 安装

项目没有 Python 第三方运行时依赖，可以直接从源码启动。若希望安装 `coding-agent` 命令，可在项目根目录执行：

```bash
python3 -m pip install -e .
coding-agent --help
```

不安装也可以始终使用 `python3 main.py ...`。PDF 功能需要另行安装 Poppler，代码搜索功能需要安装 ripgrep。

## 先运行离线演示

离线演示不访问网络，也不需要 API key。它会在指定目录中依次列举文件、创建文件并重新读取：

离线演示使用固定脚本模型，目的是验证完整工具循环；它会自动执行预设写入，不弹出编辑审批，因此请使用专门的临时目录。

```bash
mkdir -p /tmp/coding-agent-demo
python3 main.py --demo --workspace /tmp/coding-agent-demo "运行离线工具演示"
```

预期结果中会显示 5 个模型步骤、4 次工具调用，并生成：

```text
/tmp/coding-agent-demo/agent_demo.txt
```

## 配置模型 API

不要把真实凭据写入代码、`.env.example`、README、提交历史或演示视频。API 使用 API key，不使用 GitHub 密码或普通账号密码。

在当前终端设置：

```bash
export DEEPSEEK_API_KEY="你的 API key"
export DEEPSEEK_BASE_URL="https://api.deepseek.com"
export DEEPSEEK_MODEL="deepseek-v4-pro"
```

如果你使用的是 OpenAI 兼容的第三方 DeepSeek 网关，请将 `DEEPSEEK_BASE_URL` 和 `DEEPSEEK_MODEL` 改成服务商提供的值。

项目实际读取的全部模型与运行环境变量如下：

| 环境变量 | 默认值 | 说明 |
| --- | --- | --- |
| `DEEPSEEK_API_KEY` | 无 | 真实模式必填；离线演示不需要 |
| `DEEPSEEK_BASE_URL` | `https://api.deepseek.com` | DeepSeek 兼容接口根地址，也可直接以 `/chat/completions` 结尾 |
| `DEEPSEEK_MODEL` | `deepseek-v4-pro` | 主 Agent 使用的模型名 |
| `DEEPSEEK_THINKING` | `enabled` | `enabled` 或 `disabled` |
| `DEEPSEEK_REASONING_EFFORT` | `high` | `low`、`high` 或 `max` |
| `CODING_AGENT_MAX_STEPS` | `20` | 单次任务最多模型轮数 |
| `CODING_AGENT_API_TIMEOUT` | `90` | API 请求超时秒数 |
| `CODING_AGENT_COMMAND_TIMEOUT` | `30` | 本地命令默认超时秒数 |
| `CODING_AGENT_CONTEXT_TOKENS` | `64000` | 上下文窗口预算，程序会另行预留回答空间与安全余量 |
| `CODING_AGENT_ALLOWED_COMMANDS` | 空 | 额外允许的命令，以英文逗号分隔 |
| `QWEN_API_KEY` | 无 | 联网搜索、图片和 PDF 理解需要 |
| `QWEN_BASE_URL` | 带工作空间占位符的百炼地址 | 必须替换占位符后才能调用 Qwen |
| `QWEN_MODEL` | `qwen3.6-flash` | 三个外部能力使用的辅助模型 |

### 使用 `.env`

项目提供了完整的 [`.env.example`](.env.example)。先在项目根目录复制为 `.env`，再填写自己的 API Key 和服务地址：

```bash
cp .env.example .env
```

项目启动时会自动加载根目录下的 `.env`，且不会覆盖终端中已设置的同名变量。`.env` 已被 Git 忽略；只提交不含真实凭据的 `.env.example`，不要提交真实 API Key。

## 启动可视化网页

在项目根目录执行：

```bash
python3 main.py --web
```

然后浏览器打开 [http://127.0.0.1:8765](http://127.0.0.1:8765)。页面中可以：

1. 填写 Agent 允许访问的工作区；
2. 输入编程任务 Prompt；
3. 选择真实 DeepSeek 或离线演示；
4. 在左侧栏切换多个会话，每个会话保留自己的工作区和记忆；
5. 逐轮查看 Thought 决策摘要、Action 工具参数、Observation 工具结果；
6. 修改前检查红删绿增 Diff，并选择“同意并写入”或“拒绝修改”；
7. 查看模型步骤、工具调用、耗时、Token 与最终回答，或请求停止运行中的任务；
8. 添加 PNG/JPEG/WebP/GIF 图片或 PDF，由 Qwen 理解内容；发送给 Qwen 前会弹出确认卡片；
9. 复杂任务会显示逐步 Plan；点击历史消息可切换到该轮的计划和 Trace。

这里展示的 Thought 是模型返回的简短、公开决策摘要，不是隐藏的完整推理过程。

### 配置 Qwen 图片、PDF 理解与 Web 搜索

默认情况下，`deepseek-v4-pro` 负责主 Agent 决策；图片、PDF 理解和联网搜索由通义千问承担。两者的模型名都可配置。Qwen 所需变量已统一放在 [`.env.example`](.env.example) 中；复制后，在本地 `.env` 填写阿里云百炼的 API Key 和工作空间 ID：

```dotenv
QWEN_API_KEY=你的_百炼_API_Key
QWEN_BASE_URL=https://你的工作空间ID.cn-beijing.maas.aliyuncs.com/compatible-mode/v1
QWEN_MODEL=qwen3.6-flash
```

网页只会显示是否已配置，不会返回 API Key。浏览器选中的图片先保存在当前会话工作区的 `.agent-images/`，PDF 保存在 `.agent-files/`；这个本地保存步骤不等于上传给模型。只有 Agent 实际调用 `analyze_image` 或 `analyze_pdf` 且你点击同意后，内容才会发送给 Qwen。PDF 会先通过 Poppler 在本地逐页转成 120 DPI JPEG，再按页序交给 Qwen，因此扫描型 PDF 也可以解读；原 PDF 上限为 20 MB、50 页，渲染后页面总量上限为 50 MB。`web_search` 同样会先展示搜索词并等待确认。

### 多轮对话与上下文记忆

首次发送 Prompt 时，网页会建立一个会话。任务完成后可直接继续输入，例如：

```text
继续修复刚才发现的边界情况，然后重新运行测试。
```

后续轮次会收到同一会话中的用户消息、Agent 最终回答和近期工具轨迹。会话历史保存在本地 `.coding-agent/sessions/`，运行 Trace 保存在 `.coding-agent/traces/`，可回查的完整工具结果保存在 `.coding-agent/tool-results/`。这些目录均被 Git 忽略，同时普通文件工具不允许 Agent 读写。网页中的“任务检查点”可展开查看目标、约束、计划进度、修改文件、测试和失败记录。

每个会话固定工作区、模式和最大步数；需要切换项目时请新建会话。Web 页面允许把单次任务最大模型轮数设为 1～50。若工作区根目录存在 `AGENTS.md` 或 `PROJECT_RULES.md`，新会话会把它们作为常驻项目规则载入，已建立会话不会自动热重载规则文件。

默认上下文窗口为 64,000 Token，其中预留 8,000 Token 给模型回答并保留 3% 安全余量。低于输入预算 70% 时保留原文；70% 起确定性压缩旧工具结果；82% 起压缩旧 exchange，并在完成步骤、代码修改、测试或最终结果等里程碑后使用同一主模型生成固定字段的语义摘要；92% 起只保留固定层、检查点、当前任务和最近两个完整工具轮次。所有裁剪均保持 tool call 与 result 配对。语义摘要调用会产生额外的主模型 Token，但失败不会中断当前任务。

### 本地数据存储与删除

| 数据 | 实际位置 | 生命周期 |
| --- | --- | --- |
| Web 会话消息、`Conversation` 状态、结构化记忆 | 启动 Web 服务时默认工作区下的 `.coding-agent/sessions/` | 服务重启后恢复；删除会话时删除 |
| 每轮 Web 执行 Trace | 同一启动默认工作区下的 `.coding-agent/traces/` | 与聊天消息中的 `run_id` 关联；删除会话时删除对应 Trace |
| 被压缩工具结果原文 | 各会话工作区下的 `.coding-agent/tool-results/` | 按 SHA-256 去重保存；当前不会随删除会话自动清理 |
| 会话图片附件 | 各会话工作区下的 `.agent-images/<session_id>/` | 删除会话时删除 |
| 会话 PDF 附件 | 各会话工作区下的 `.agent-files/<session_id>/` | 删除会话时删除 |

这些目录均被 Git 忽略，普通文件与搜索工具也会拒绝或跳过它们。会话与 Trace JSON、工具结果归档采用原子写入并设置为仅当前用户可读写。由于会话数据库跟随“启动 Web 服务时的默认工作区”，换一个 `--workspace` 启动服务会看到另一套会话列表；会话本身仍可绑定其他绝对工作区。

可选的启动参数：

```text
--workspace PATH   网页默认工作区
--host HOST        绑定地址，默认 127.0.0.1
--port PORT        端口，默认 8765
```

Web 界面默认只监听本机回环地址。Agent 需要访问你指定的本地代码目录和执行本地测试，因此它不是纯静态网站，不建议直接暴露到公网。

### Web API 概览

前端实际使用的主要本地接口如下：

| 方法与路径 | 用途 |
| --- | --- |
| `GET /api/health` | 服务健康状态 |
| `GET /api/config` | 返回非敏感运行配置和模型是否已配置 |
| `GET /api/sessions` | 会话摘要列表，不返回完整上下文 |
| `POST /api/sessions` | 新建并锁定工作区、模式和最大步数 |
| `GET /api/sessions/{id}` | 当前会话消息、上下文统计和结构化记忆 |
| `POST /api/sessions/{id}/messages` | 追加一轮用户消息并启动后台 Agent |
| `POST /api/sessions/{id}/images` / `pdfs` | 保存会话附件到本地工作区 |
| `DELETE /api/sessions/{id}` | 删除未运行的整段会话及其会话级产物 |
| `GET /api/runs/{id}` | 获取状态、Plan、指标、审批和公开 Trace |
| `POST /api/runs/{id}/approvals/{approval_id}` | 同意或拒绝编辑、联网或文件外传 |
| `POST /api/runs/{id}/cancel` | 请求在当前模型调用结束后停止 |

所有写操作都要求前端发送 `X-Agent-Client: web-ui`。这是面向本地单用户界面的同源防护，不是完整的用户认证系统。

## 运行真实任务

```bash
python3 main.py --workspace /path/to/project "分析项目，修复失败的测试，并运行测试验证"
```

也可以不在命令行传任务，启动后再输入：

```bash
python3 main.py --workspace /path/to/project
```

常用参数：

```text
--workspace PATH   限制 Agent 可以访问的工作目录
--max-steps N      覆盖最大模型轮数
--quiet            隐藏中间工具事件
--demo             使用离线脚本模型，不请求 API
--web              启动本地可视化 Web 界面
```

CLI 与 Web 使用同一个 Agent 核心循环，但当前交互能力并不完全相同：

| 能力 | CLI | Web |
| --- | --- | --- |
| 单次 Prompt 与工具循环 | 支持 | 支持 |
| 编辑与外部传输审批 | 终端交互确认；非 TTY 时拒绝 | 页面确认卡片 |
| 多轮会话、切换与重启恢复 | 不支持跨进程保存 | 支持 |
| 结构化 Plan | 当前未注册 | 支持 |
| 图片 / PDF 附件上传 | 需先把文件放进工作区 | 页面可选择并保存到会话目录 |
| 持久化 Web Trace | 不生成 | 支持 |

## 可用工具

| 工具 | 可用模式 | 功能 | 关键限制 |
| --- | --- | --- | --- |
| `find_files` | CLI / Web | 使用 glob 查找文件 | 内部调用 `rg`；最多 500 条，默认 200 条，12 秒超时 |
| `search_code` | CLI / Web | 搜索代码文本或正则 | 默认按字面量搜索；返回文件、行、列、文本和截断状态 |
| `list_files` | CLI / Web | 列举项目结构 | 深度 1～8；最多 500 项；忽略 Git、依赖目录和敏感目录 |
| `read_file` | CLI / Web | 分段读取 UTF-8 文件并记录版本 | 仅限工作区；单文件最大 1 MB |
| `write_file` | CLI / Web | 创建或完整写入文件 | 覆盖已有文件前必须读取；用户确认 Diff 后写入 |
| `replace_in_file` | CLI / Web | 精确文本替换 | 必须先读；匹配数或文件版本不符则不修改 |
| `multi_edit` | CLI / Web | 原子执行多个精确替换 | 1～20 项；所有目标先读、全部校验、一次审批 |
| `run_command` | CLI / Web | 执行测试或构建命令 | `shell=False`；白名单；1～120 秒；输出上限 30,000 字符 |
| `read_tool_result` | CLI / Web | 分页回查被压缩的完整工具结果 | 使用 SHA-256 结果 ID；单次最多返回 12,000 字符 |
| `update_plan` | 仅 Web | 创建并更新复杂任务计划 | 1～20 步；至多一个 `in_progress` |
| `web_search` | 真实 CLI / Web | 通过 Qwen 检索最新网页信息 | 需要 Qwen 配置；查询发送前必须明确同意 |
| `analyze_image` | 真实 CLI / Web | 通过 Qwen 理解工作区图片 | PNG/JPEG/WebP/GIF，最大 10 MB；发送前明确同意 |
| `analyze_pdf` | 真实 CLI / Web | 通过 Qwen 解读工作区 PDF | 依赖 Poppler；最多 20 MB、50 页；发送前明确同意 |

默认命令白名单为 `cargo`、`git`、`go`、`node`、`npm`、`npx`、`pnpm`、`python`、`python3`、`pytest`、`ruby`、`ruff` 和 `uv`。额外白名单只表示允许启动该程序，不会解除路径限制、Git 子命令限制、`python -c` / `node -e` 限制或审批规则。

## 运行测试

```bash
python3 -m unittest discover -s tests -v
```

测试覆盖：

- 文件工具读写与精确替换；
- `rg` 文件发现与结构化代码搜索；
- 先读后改、乐观锁冲突、审批拒绝与原子多文件编辑；
- 路径越界和敏感文件拦截；
- 命令白名单、非零退出码和危险 Git 操作；
- DeepSeek tool call 解析；
- Agent 多轮工具循环、终止条件和最大步数；
- 上下文裁剪时保持 tool call/result 配对；
- 分级上下文、工具类型压缩、里程碑摘要与原始结果分页回查；
- 无 API key 的 CLI 离线端到端演示；
- `.env` 安全加载与环境变量优先级；
- Web 静态页面、配置接口、请求防护和离线端到端任务；
- 两轮对话的上下文复用、本地持久化和服务重启恢复；
- Trace 请求快照、耗时、Token 聚合与最终测试结果。

## 视频演示方案

演示采用“Agent 构建 Agent”的两轮任务：被考核和提交的项目是 **LoopCoder**，它在一个独立工作区中完成 **MiniAgent** 项目；MiniAgent 只是 LoopCoder 的演示任务产物。

### 第一轮：完成 MiniAgent 核心

工作区预先提供项目骨架、功能要求和验收测试。LoopCoder 先阅读项目、运行初始测试，然后实现对话历史、工具注册与本地执行、tool call 解析、Agent 循环、最大步数和错误处理，最后运行全部测试。参考 Prompt：

```text
当前工作区是一个待完善的 MiniAgent 项目。请根据 README 中的要求检查项目，先运行初始测试并制定计划，再自行实现模型—工具—结果循环、read_file、write_file、run_command、对话历史、错误处理和最大步数终止。不使用任何 Agent 框架或 SDK，不要通过修改测试绕过问题。完成后运行全部测试并总结。
```

### 第二轮：根据参考图继续设计前端

在同一会话中上传界面参考图，让 LoopCoder 复用上一轮的项目上下文，分析图片的配色、排版、间距和组件风格，在不破坏现有交互的前提下修改 MiniAgent 前端。参考 Prompt：

```text
继续完善刚才的 MiniAgent 项目。请读取附件中的界面参考图，提取它的配色、排版、卡片、间距和按钮风格，并据此重新设计现有前端。只借鉴视觉语言，不复制图片中的品牌、文字或 Logo；保留现有 DOM ID、接口和交互功能，避免无关的后端改动。完成后运行相关测试并总结设计选择。
```

两轮演示可同时呈现真实编程任务、连续对话记忆、图片理解、本地工具执行、Diff 审批和测试验证。演示视频应保留 Prompt、关键 Action / Observation、修改确认和最终测试结果；等待过程可剪辑或加速。

## 安全边界

本项目提供的是应用层防护，不是操作系统级安全沙箱：

- 文件工具会解析真实路径并拒绝离开工作区；
- `.env`、常见私钥和 Git 凭据文件不可读取；
- 命令必须用参数数组表达，不支持管道、重定向或 Shell 展开；
- `git push`、`git reset`、`git clean` 等操作被拦截；
- Python `-c` 和 Node `-e` 被拦截；
- 子进程只继承必要环境变量，不会继承 DeepSeek API key。

风险动作分为两类：代码编辑以及向 Qwen 发送查询、图片或 PDF 会进入用户审批；命令策略明确禁止的操作则直接返回 `CommandDenied`，不会提供“仍然执行”的确认按钮。`CODING_AGENT_ALLOWED_COMMANDS` 应只添加你信任的本机程序。

但是，被允许执行的项目脚本本身仍可能包含任意代码。不要对不可信项目开放敏感目录，生产场景应进一步使用容器或系统沙箱。

## 已知限制

- Web 界面使用短轮询获取进度，尚未实现 SSE/WebSocket 流式通道；
- 模型 API 请求进行期间无法强制中断，停止请求会在当前请求返回后生效；
- 持久化多轮会话和结构化 Plan 目前属于 Web 模式；CLI 每次进程只执行一个用户任务；
- 首次上下文预算使用本地 Token 估算，后续会用 API 的真实用量校准，但尚未集成模型专用 tokenizer；
- 语义摘要是有损信息，只在高压力里程碑触发；精确细节仍应通过 `read_file`、`read_tool_result` 或 Trace 回查；
- `.coding-agent/tool-results/` 目前没有引用计数或自动垃圾回收，删除会话后内容寻址的归档可能继续保留；
- Web 写接口的自定义请求头用于本地同源防护，不提供多用户登录、权限角色或公网部署认证；
- 命令安全策略是基础防护，不等同于强隔离；
- 未配置真实 API key 时只能运行离线演示。

## License

[MIT](LICENSE)
