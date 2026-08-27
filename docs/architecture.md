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
        ├── filesystem.py       工作区文件工具
        └── shell.py            受限本地命令工具

webapp.py                            本地 HTTP API、任务线程与状态存储
  └── web/
        ├── index.html          可视化工作台结构
        ├── styles.css          响应式界面样式
        └── app.js              任务提交、轮询、停止和轨迹渲染
```

模型只负责决定“调用哪个工具、使用什么参数、何时给出最终答案”。工具真正的执行、参数校验、访问范围和错误处理全部由本地代码负责。

Web 界面不在浏览器中执行 Agent，而是通过本地 API 创建后台任务。API Key 只存在 Python 进程的环境变量中，配置接口只返回“是否已配置”，不返回凭据内容。

## 3. Web 任务流程

```text
浏览器提交 Prompt
       ↓ POST /api/runs
后端校验工作区、步数和 API 配置
       ↓
后台线程运行 Agent 循环 → 事件写入内存状态
       ↑                              ↓
用户可请求停止          GET /api/runs/{id} 短轮询
                                      ↓
                             时间线、统计和最终回答
```

任务状态只保存在本地进程内存中，服务重启后会清空。默认仅绑定 `127.0.0.1`，写操作接口要求自定义客户端请求头，利用浏览器同源策略阻止普通跨站表单触发。

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

## 5. 上下文管理

`Conversation` 将一次 assistant tool call 和对应的全部 tool results 保存为一个完整轮次块。超过本地字符预算时，从最旧的完整轮次开始丢弃，而不是删除单条消息，从而避免出现没有对应结果的 tool call。

始终保留：

- 系统提示词；
- 原始用户任务；
- 至少一个最近工具轮次。

文件工具和命令工具还会限制单次返回大小，防止日志或大文件快速占满上下文。

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

## 8. 为什么不使用 Shell 字符串

`run_command` 接收参数数组并以 `shell=False` 执行。例如：

```json
{"argv": ["python3", "-m", "unittest", "-v"]}
```

这样不会解释 `|`、`>`、`&&`、命令替换等 Shell 语法，降低模型输出被意外解释为复杂命令的风险，也让每个参数的含义更清楚。

## 9. 可继续扩展的方向

- 使用统一 diff/patch 格式编辑代码；
- 用户审批高风险命令；
- 基于容器的强隔离；
- Token 级上下文计数与模型摘要；
- 流式输出和 TUI；
- 任务评测集、成功率和成本统计；
- 多模型适配层。
