# Coding Agent from Scratch

一个从零实现的轻量级本地编程智能体。模型负责决策，程序自行维护对话历史、解析 tool calls、执行本地文件与命令工具，并循环工作直到完成任务。

本项目**不使用任何 agent 框架或 Agent SDK**，也不依赖 API 服务端托管的代码执行或文件工具。

## 当前能力

- DeepSeek `deepseek-v4-pro` Chat Completions 接入；
- 模型 - 工具 - 结果 - 模型的自主执行循环；
- 完整工具调用历史与上下文预算管理；
- 工作区范围内的文件列举、读取、写入和精确替换；
- 不经过系统 Shell 的本地命令执行；
- 路径越界防护、敏感文件拦截、命令白名单和超时；
- API 重试、参数校验、异常结果回传、重复调用检测和最大步数限制；
- 无需 API key 的离线演示模式；
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

更详细的设计说明见 [`docs/architecture.md`](docs/architecture.md)。

## 环境要求

- Python 3.10 或更高版本；
- 使用真实模型时需要 DeepSeek API key；
- macOS、Linux 或 Windows 均可，具体可执行命令取决于本机环境。

## 先运行离线演示

离线演示不访问网络，也不需要 API key。它会在指定目录中依次列举文件、创建文件并重新读取：

```bash
mkdir -p /tmp/coding-agent-demo
python3 main.py --demo --workspace /tmp/coding-agent-demo "运行离线工具演示"
```

预期结果中会显示 4 个模型步骤、3 次工具调用，并生成：

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
| `CODING_AGENT_ALLOWED_COMMANDS` | 空 | 额外允许的命令，以英文逗号分隔 |

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
```

## 可用工具

| 工具 | 功能 | 关键限制 |
| --- | --- | --- |
| `list_files` | 列举项目结构 | 忽略 `.git`、虚拟环境和敏感文件 |
| `read_file` | 分段读取 UTF-8 文件 | 仅限工作区，单文件最大 1 MB |
| `write_file` | 创建或完整写入文件 | 覆盖已有文件时必须显式确认参数 |
| `replace_in_file` | 精确文本替换 | 匹配数量不符则不修改文件 |
| `run_command` | 执行测试或构建命令 | 不使用 Shell；白名单、超时和输出截断 |

## 运行测试

```bash
python3 -m unittest discover -s tests -v
```

测试覆盖：

- 文件工具读写与精确替换；
- 路径越界和敏感文件拦截；
- 命令白名单、非零退出码和危险 Git 操作；
- DeepSeek tool call 解析；
- Agent 多轮工具循环、终止条件和最大步数；
- 上下文裁剪时保持 tool call/result 配对；
- 无 API key 的 CLI 离线端到端演示。

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

- 当前只实现命令行界面；
- 当前未实现流式输出；
- 上下文压缩采用完整工具轮次裁剪，没有调用额外模型生成摘要；
- 命令安全策略是基础防护，不等同于强隔离；
- 未配置真实 API key 时只能运行离线演示。

## License

[MIT](LICENSE)
