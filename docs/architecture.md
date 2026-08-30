# 架构与设计决策

## 1. 目标

本项目的目标不是封装现有 agent，而是清楚展示一个编程智能体最关键的运行机制：

1. 管理任务与上下文；
2. 向模型声明本地工具；
3. 解析模型返回的 tool calls；
4. 校验参数并在本地执行；
5. 将结果返回模型；
6. 在完成、失败或达到限制时停止。

## 2. 模块关系

```text
cli.py
  ├── config.py                 .env、环境变量与运行限制
  ├── models.py                 DeepSeek Chat Completions 客户端
  ├── agent.py                  核心循环与终止条件
  │     ├── conversation.py     历史与上下文预算
  │     └── prompts.py          Agent 行为约束
  └── tools/
        ├── registry.py         工具注册、JSON 解析和调度
        ├── filesystem.py       版本锁、Diff、审批和原子文件编辑
        ├── search.py           rg 文件发现与结构化代码搜索
        ├── external.py         Qwen 联网搜索及图片/PDF 理解（审批后传输）
        ├── planning.py         复杂任务结构化 Plan 与状态更新
        └── shell.py            受限本地命令工具

webapp.py                            本地 HTTP API、会话/任务管理与持久化
  └── web/
        ├── index.html          可视化工作台结构
        ├── styles.css          响应式界面样式
        └── app.js              多轮聊天、图片、Plan、审批和按轮 Trace 渲染
```

模型只负责决定“调用哪个工具、使用什么参数、何时给出最终答案”。工具真正的执行、参数校验、访问范围和错误处理全部由本地代码负责。

Web 界面不在浏览器中执行 Agent，而是通过本地 API 建立会话并创建后台任务。API Key 只存在 Python 进程的环境变量中，配置接口只返回“是否已配置”，不返回凭据内容。

## 3. Web 任务流程

```text
浏览器建立会话
       ↓ POST /api/sessions
锁定工作区、模式和步数限制
       ↓ POST /api/sessions/{id}/messages
将新用户消息追加到 Conversation
       ↓
后台线程运行 Agent 循环 → 事件写入内存与 Trace
       ↑                              ↓
编辑/联网/图片/PDF 工具请求   GET /api/runs/{id} 短轮询
       ↓                              ↓
等待用户同意/拒绝       聊天、Plan、时间线、审批卡片和结果
       ↓ POST /api/runs/{run}/approvals/{approval}
批准后执行修改或外部传输；拒绝则返回结构化错误
```

DeepSeek 只接收文本上下文并负责主 Agent 决策。`web_search`、`analyze_image` 和 `analyze_pdf` 使用通义千问兼容接口；查询文本、图片或 PDF 页面在用户批准前不会离开本机。图片附件先存入会话工作区的 `.agent-images/`，PDF 存入 `.agent-files/`，这些目录不进入 Git，也不会出现在普通文件列表中。PDF 在本地通过 Poppler 校验页数并逐页渲染为 JPEG，批准后才把有序页面交给 Qwen。

复杂任务可由模型调用 `update_plan` 建立最多 20 个步骤，每次只允许一个 `in_progress`。计划状态与 Trace 一起持久化；网页会用 `✓` 展示完成步骤。聊天消息保存各自的 `run_id`，因此点击历史消息时可以载入该轮持久化 Trace，而不是只显示最后一次运行。

会话消息和完整模型上下文会以权限 `0600` 的 JSON 文件原子写入 `.coding-agent/sessions/`，因此服务重启后可恢复。每次运行的模型请求快照、公开决策摘要、工具参数/结果、耗时、错误码、Token 和最终测试结果写入 `.coding-agent/traces/`。这些目录被 Git 忽略，也被 Agent 文件工具拒绝。默认仅绑定 `127.0.0.1`，写操作接口要求自定义客户端请求头，利用浏览器同源策略阻止普通跨站表单触发。

## 4. 一次工具轮次

```text
Agent                              Model
  |                                  |
  | messages + JSON tool schemas     |
  |--------------------------------->|
  |                                  |
  | assistant message + tool_calls   |
  |<---------------------------------|
  |                                  |
  | validate JSON arguments          |
  | execute local tool               |
  | append tool_call_id + result      |
  |                                  |
  | updated messages                 |
  |--------------------------------->|
```

一个模型响应可以包含多个 tool calls。Agent 会逐个执行，并使用各自的 `tool_call_id` 回传结果。

## 5. 上下文与记忆管理

`Conversation` 将一条用户消息、该轮的全部 assistant tool calls / tool results 和最终回答保存为一个完整 exchange。与此同时，`MemoryCheckpoint` 独立维护目标、约束、已完成步骤、关键决策、修改文件、测试、失败尝试、禁止重复项和下一步。原始历史与模型当前可见的工作记忆因此相互独立。

默认窗口为 64,000 Token，预留 8,000 Token 给回答并保留 3% 安全余量。首次调用使用适配中英文的本地估算；收到 API 的 `prompt_tokens` 后，以指数移动方式校准后续估算。校准参数随会话持久化。

上下文按输入预算分级处理，而不是等到完全溢出：低于 70% 保留原文；70%～82% 对旧工具结果做确定性、类型感知的压缩；82%～92% 将旧 exchange 收敛到结构化摘要；92% 以上进入紧急模式，只保留固定层、检查点、当前 exchange 和最近两个完整工具轮次。任何裁剪都不会拆散 tool call 与对应 result。

送给模型的上下文分为以下稳定层：

- 系统安全与工具规则；
- 工作区根目录的 `AGENTS.md` / `PROJECT_RULES.md`；
- 结构化任务检查点；
- 里程碑语义摘要与被压缩的早期对话；
- 最新用户请求形成的当前 Todo；
- 最近完整 exchange 和工具轨迹。

工具结果压缩采用分类策略：代码搜索保留文件、行列与匹配文本；命令输出优先保留失败、异常、堆栈和末尾统计；网页搜索保留答案与来源；图片/PDF 保留分析字段；JSON 保留字段结构；普通文本才使用首尾窗口。压缩元数据记录原始长度、返回长度、SHA-256、策略和 `result_id`。完整原文以权限 `0600` 保存到 `.coding-agent/tool-results/`，模型需要精确信息时可通过 `read_tool_result` 按字符区间回查，而不必重复执行联网或高成本工具。

当上下文压力达到 82% 且任务完成步骤或测试等新里程碑时，Agent 使用同一个 DeepSeek 模型发起一次不带工具的 JSON 摘要请求。摘要固定保存目标、约束、决策、完成动作、修改文件、代码事实、测试、失败尝试、阻塞项和下一步。它是可失败的辅助流程：失败会进入 Trace，但不会让主任务失败；也不会每轮调用，从而控制费用、延迟和事实漂移。

## 6. 终止条件

任务在以下情况结束：

- 模型返回没有 tool calls 的非空最终文本；
- 达到最大模型轮数；
- 模型响应既没有工具调用也没有文本；
- 用户中断进程；
- Web 用户请求停止任务；
- 无法恢复的 API 或配置错误。

此外，连续三次完全相同的工具调用会被阻止，并向模型返回 `RepeatedToolCall` 错误，避免无意义循环。

## 7. 错误处理

所有可恢复的本地错误会转换为结构化结果：

```json
{
  "success": false,
  "error": "Path escapes the workspace",
  "metadata": {"code": "PathDenied"}
}
```

模型可以根据错误调整路径或参数。API 对 429 和 5xx 响应进行有限重试；其他 HTTP 错误直接报告，避免无限重试。

## 8. 安全编辑与乐观锁

`read_file` 在返回内容时记录文件大小、纳秒级修改时间和 SHA-256。编辑已有文件时必须存在这个版本记录；审批等待结束后还会再次核对。文件在任一阶段发生变化都会返回 `Conflict`，要求模型重新读取。

单文件写入使用同目录临时文件替换。`multi_edit` 先验证所有目标、生成全部 Diff、统一审批，再暂存并提交；提交发生 I/O 错误时尝试恢复已经替换的文件。Web 审批卡片只展示公开统一 Diff，不展示模型的隐藏推理。

## 9. Trace 与评测记录

模型请求、语义摘要请求和工具事件使用同一递增序号。浏览器只接收渲染所需的精简事件；本地 Trace 额外保留去除隐藏推理字段后的请求消息、工具 schema 和完整工具结果。测试命令会被识别并保存最后一次退出码与成功状态，可作为本次运行的最终评测结果。

## 10. 为什么不使用 Shell 字符串

`run_command` 接收参数数组并以 `shell=False` 执行。例如：

```json
{"argv": ["python3", "-m", "unittest", "-v"]}
```

这样不会解释 `|`、`>`、`&&`、命令替换等 Shell 语法，降低模型输出被意外解释为复杂命令的风险，也让每个参数的含义更清楚。

## 11. 可继续扩展的方向

- 基于容器的强隔离；
- 可检索的跨会话长期项目记忆；
- 流式输出和 TUI；
- 批量任务评测集、成功率和成本趋势面板；
- 多模型适配层。
