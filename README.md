# Coding Agent from Scratch

一个从零实现的轻量级本地编程智能体。模型负责决策，程序自行维护对话历史、解析 tool calls、执行本地文件与命令工具，并循环工作直到完成任务。

本项目**不使用任何 agent 框架或 Agent SDK**，也不依赖 API 服务端托管的代码执行或文件工具。

## 当前能力

- DeepSeek `deepseek-v4-pro` Chat Completions 接入；
- 通义千问 `qwen3.6-flash` 图片理解与联网搜索（发送前必须由用户确认）；
- 模型 - 工具 - 结果 - 模型的自主执行循环；
- `rg` 驱动的专业文件发现与代码检索，结构化返回文件、行号、列号和截断状态；
- 强制先读后改、文件版本哈希冲突检测、原子写入和原子 `multi_edit`；
- 每次修改先展示统一 Diff，红色删除、绿色新增，由用户明确同意后才写入；
- 不经过系统 Shell 的本地命令执行；
- 路径越界防护、敏感文件拦截、命令白名单和超时；
- API 重试、参数校验、异常结果回传、重复调用检测和最大步数限制；
- 无需 API key 的离线演示模式；
- 可视化 Web 工作台，可输入 Prompt、设置工作区并逐轮查看 Thought / Action / Observation；
- 本地前后端 API，支持任务状态、步数/工具统计、停止请求和最终结果；
- 类 Codex 的多轮会话侧边栏：可继续追问、切换历史会话，并在服务重启后恢复；
- 会话可在侧边栏中永久删除，同时清理对应消息、上下文、Trace 和上传图片；
- 会话与工作区一一绑定，切换会话时同时切换上下文和可访问目录；
- 分层上下文：固定规则、结构化任务检查点、压缩摘要、当前 Todo 与近期完整工具轨迹；
- 真实 Token 用量校准：首次保守估算，随后根据 API `prompt_tokens` 自动校准并预留回答空间；
- 超长工具输出只向模型提供带 SHA-256 的首尾预览，完整原文保存在本地 Trace；
- 持久化 Trace：模型请求、完整工具参数/结果、耗时、错误码、Token 用量和最后一次测试结果；
- 复杂任务自动建立 Plan，逐步显示待处理、进行中和已完成打勾状态；
- 点击任意一轮聊天消息即可查看该轮对应的计划、指标和执行轨迹；
- 仅使用 Python 标准库，运行时零第三方依赖。

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
- `src/coding_agent/conversation.py`：历史与上下文管理；
- `src/coding_agent/tools/`：工具定义、校验和本地执行。
- `src/coding_agent/webapp.py`：本地 HTTP API、任务管理与静态资源服务；
- `src/coding_agent/web/`：可视化前端页面。

更详细的设计说明见 [`docs/architecture.md`](docs/architecture.md)。

## 环境要求

- Python 3.10 或更高版本；
- 使用真实模型时需要 DeepSeek API key；
- `ripgrep`（命令 `rg`），用于 `find_files` 和 `search_code`；
- macOS、Linux 或 Windows 均可，具体可执行命令取决于本机环境。

## 先运行离线演示

离线演示不访问网络，也不需要 API key。它会在指定目录中依次列举文件、创建文件并重新读取：

```bash
mkdir -p /tmp/coding-agent-demo
python3 main.py --demo --workspace /tmp/coding-agent-demo "运行离线工具演示"
```

预期结果中会显示 5 个模型步骤、4 次工具调用，并生成：

```text
/tmp/coding-agent-demo/agent_demo.txt
```

## 配置 DeepSeek API

不要把真实凭据写入代码、`.env.example`、README、提交历史或演示视频。API 使用 API key，不使用 GitHub 密码或普通账号密码。

在当前终端设置：

```bash
export DEEPSEEK_API_KEY="你的 API key"
export DEEPSEEK_BASE_URL="https://api.deepseek.com"
export DEEPSEEK_MODEL="deepseek-v4-pro"
```

如果你使用的是 OpenAI 兼容的第三方 DeepSeek 网关，请将 `DEEPSEEK_BASE_URL` 和 `DEEPSEEK_MODEL` 改成服务商提供的值。

可选配置：

| 环境变量 | 默认值 | 说明 |
| --- | --- | --- |
| `DEEPSEEK_THINKING` | `enabled` | `enabled` 或 `disabled` |
| `DEEPSEEK_REASONING_EFFORT` | `high` | `low`、`high` 或 `max` |
| `CODING_AGENT_MAX_STEPS` | `20` | 单次任务最多模型轮数 |
| `CODING_AGENT_API_TIMEOUT` | `90` | API 请求超时秒数 |
| `CODING_AGENT_COMMAND_TIMEOUT` | `30` | 本地命令默认超时秒数 |
| `CODING_AGENT_CONTEXT_TOKENS` | `64000` | 上下文窗口预算，程序会另行预留回答空间与安全余量 |
| `CODING_AGENT_ALLOWED_COMMANDS` | 空 | 额外允许的命令，以英文逗号分隔 |

### 使用 `.env`

项目启动时会自动加载根目录下的 `.env`，且不会覆盖终端中已设置的同名变量。`.env` 已被 Git 忽略，不要提交真实 API Key。

```dotenv
DEEPSEEK_API_KEY=你的_API_key
DEEPSEEK_BASE_URL=https://api.deepseek.com
DEEPSEEK_MODEL=deepseek-v4-pro
```

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
7. 查看模型步骤、工具调用、耗时、Token 与最终回答，或停止运行中的任务。
8. 添加 PNG/JPEG/WebP/GIF 图片，由 Qwen 理解图片内容；联网或上传图片前会弹出确认卡片。
9. 复杂任务会显示逐步 Plan；点击历史消息可切换到该轮的计划和 Trace。

### 配置 Qwen 图片理解与 Web 搜索

DeepSeek V4 Pro 继续负责主 Agent 决策；图片理解和联网搜索由价格更低的通义千问承担。复制 [`qwen.env.template`](qwen.env.template) 中的变量名到本地 `.env`，再填写阿里云百炼的 API Key 和工作空间 ID：

```dotenv
QWEN_API_KEY=你的_百炼_API_Key
QWEN_BASE_URL=https://你的工作空间ID.cn-beijing.maas.aliyuncs.com/compatible-mode/v1
QWEN_MODEL=qwen3.6-flash
```

网页只会显示是否已配置，不会返回 API Key。添加图片只会先保存到当前会话工作区的 `.agent-images/`；只有 Agent 实际调用 `analyze_image` 且你点击同意后，图片才会发送给阿里云。`web_search` 同样会先展示搜索词并等待确认。

### 多轮对话与上下文记忆

首次发送 Prompt 时，网页会建立一个会话。任务完成后可直接继续输入，例如：

```text
继续修复刚才发现的边界情况，然后重新运行测试。
```

后续轮次会收到同一会话中的用户消息、Agent 最终回答和近期工具轨迹。会话历史保存在本地 `.coding-agent/sessions/`，运行 Trace 保存在 `.coding-agent/traces/`。两者均被 Git 忽略，同时文件工具不允许 Agent 读写。网页中的“任务检查点”可展开查看目标、约束、计划进度、修改文件、测试和失败记录。

每个会话固定工作区、模式和最大步数；需要切换项目时请新建会话。若工作区根目录存在 `AGENTS.md` 或 `PROJECT_RULES.md`，新会话会把它们作为常驻项目规则载入。默认上下文窗口为 64,000 Token，其中预留 8,000 Token 给模型回答并保留 3% 安全余量。旧对话按完整 exchange 压缩；单个长任务则按完整的 tool call + results 块压缩，绝不拆散调用协议。

可选的启动参数：

```text
--workspace PATH   网页默认工作区
--host HOST        绑定地址，默认 127.0.0.1
--port PORT        端口，默认 8765
```

Web 界面默认只监听本机回环地址。Agent 需要访问你指定的本地代码目录和执行本地测试，因此它不是纯静态网站，不建议直接暴露到公网。

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

## 可用工具

| 工具 | 功能 | 关键限制 |
| --- | --- | --- |
| `find_files` | 使用 glob 查找文件 | 内部调用 `rg`，返回路径和截断状态 |
| `search_code` | 搜索代码文本或正则 | 返回文件、行、列、文本和截断状态 |
| `list_files` | 列举项目结构 | 忽略 `.git`、虚拟环境和敏感文件 |
| `read_file` | 分段读取 UTF-8 文件并记录版本 | 仅限工作区，单文件最大 1 MB |
| `write_file` | 创建或完整写入文件 | 覆盖前必须读取；Web 中写入前必须审批 Diff |
| `replace_in_file` | 精确文本替换 | 必须先读；匹配数或版本不符则不修改 |
| `multi_edit` | 原子执行多个精确替换 | 所有目标须先读、全部校验、一次审批 |
| `run_command` | 执行测试或构建命令 | 不使用 Shell；白名单、超时和输出截断 |
| `update_plan` | 创建并更新复杂任务计划 | 至多一个进行中步骤，完成后逐项打勾 |
| `web_search` | 通过 Qwen 检索最新网页信息 | 查询发送前必须由用户明确同意 |
| `analyze_image` | 通过 Qwen 理解工作区图片 | 图片上传前显示路径、大小、哈希并请求同意 |

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
- 无 API key 的 CLI 离线端到端演示。
- `.env` 安全加载与环境变量优先级；
- Web 静态页面、配置接口、请求防护和离线端到端任务。
- 两轮对话的上下文复用、本地持久化和服务重启恢复。
- Trace 请求快照、耗时、Token 聚合与最终测试结果。

## 视频演示用示例任务

`examples/bugfix_demo` 是一个故意带有失败测试的小项目，可以用来演示 Agent 阅读代码、修改文件并运行测试：

```bash
python3 main.py --workspace examples/bugfix_demo \
  "修复 slugify 函数，使所有测试通过；完成后运行全部测试并总结修改"
```

示例说明见 [`examples/bugfix_demo/README.md`](examples/bugfix_demo/README.md)。

## 安全边界

本项目提供的是应用层防护，不是操作系统级安全沙箱：

- 文件工具会解析真实路径并拒绝离开工作区；
- `.env`、常见私钥和 Git 凭据文件不可读取；
- 命令必须用参数数组表达，不支持管道、重定向或 Shell 展开；
- `git push`、`git reset`、`git clean` 等操作被拦截；
- Python `-c` 和 Node `-e` 被拦截；
- 子进程只继承必要环境变量，不会继承 DeepSeek API key。

但是，被允许执行的项目脚本本身仍可能包含任意代码。不要对不可信项目开放敏感目录，生产场景应进一步使用容器或系统沙箱。

## 已知限制

- Web 界面使用短轮询获取进度，尚未实现 SSE/WebSocket 流式通道；
- 模型 API 请求进行期间无法强制中断，停止请求会在当前请求返回后生效；
- 上下文预算使用字符数估算，尚未使用模型 tokenizer 做精确 Token 计数；
- 早期轮次的压缩摘要由本地规则生成，不额外调用模型；项目规则只在建立会话时载入；
- 命令安全策略是基础防护，不等同于强隔离；
- 未配置真实 API key 时只能运行离线演示。

## License

[MIT](LICENSE)
