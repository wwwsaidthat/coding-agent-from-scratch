Coding Agent from Scratch（LoopCoder）项目说明

一、Git 仓库地址
https://github.com/wwwsaidthat/coding-agent-from-scratch

二、如何运行
环境需要 Python 3.10 或更高版本。项目的 Python 运行时只使用标准库；代码搜索需要 ripgrep，PDF 理解需要 Poppler。在项目根目录执行：

cp .env.example .env

在 .env 中填写自己的 DEEPSEEK_API_KEY。如需联网搜索、图片或 PDF 理解，再填写 QWEN_API_KEY 和对应服务地址。请勿提交包含真实密钥的 .env。

启动网页版：
python3 main.py --web

然后访问 http://127.0.0.1:8765。也可执行无需 API Key 的离线演示：
python3 main.py --demo --workspace /tmp/coding-agent-demo "运行离线工具演示"

运行测试：
python3 -m unittest discover -s tests -v

三、特色功能
本项目不使用 Agent 框架或 Agent SDK，从零实现“模型—工具—结果—模型”自主循环，特色如下：

1. 对话历史与上下文管理：以完整 Exchange 保存用户消息、模型工具调用、工具结果和最终回答，确保调用与结果不被拆散。系统维护目标、约束、完成项、修改文件、测试和下一步等结构化记忆，并按 70%/80%/90% 水位依次压缩历史大结果、旧 Exchange 和早期工具轮次。完整结果按 SHA-256 归档后可分页回查。Web 端支持多会话切换、工作区绑定和重启恢复。

2. 工具的定义与本地执行：所有工具使用名称、描述和 JSON Schema 声明参数，由注册表统一发现、校验与调度。工具覆盖文件列举与分段读取、ripgrep 文件发现与代码检索、精确替换、原子多文件编辑、受限命令执行和 Plan 更新。编辑强制先读后改，用大小、修改时间和哈希检测冲突，并在用户批准红删绿增 Diff 后才原子写入。联网搜索及图片、PDF 理解由可选的通义千问模型执行，数据外传前也需审批。

3. 模型输出的解析：主模型通过 DeepSeek 兼容的 Chat Completions 接口返回最终文本或 tool calls。解析器检查响应结构，提取调用编号、工具名和 JSON 参数，并将本地 Observation 按原编号回填历史。网页界面逐轮展示 Thought 决策摘要、Action 参数和 Observation 结果，但不暴露模型隐藏推理。

4. 循环终止条件：Agent 每轮重新组装 Prompt；模型返回工具调用时继续执行，返回非空最终文本时正常结束。系统还会在达到最大模型步数、收到用户停止请求、模型响应既无文本也无工具调用，或出现无法恢复的模型异常时终止，避免无限循环。

5. 错误处理：工具以统一 ToolResult 返回成功数据或错误码，路径越界、敏感文件、参数非法、文件版本冲突、用户拒绝和禁止命令均会转换为可解释结果交还模型处理。连续三次相同工具调用会被拦截；DeepSeek 的限流、服务端错误、连接失败和超时会有限重试。Trace 持久化记录请求、工具输入输出、耗时、错误码、Token 和测试结果，便于复现与评估。测试同时覆盖上述主要正常与失败路径。

四、其它说明
项目默认只监听本机地址，用于本地代码任务，不建议直接暴露到公网。会话、Trace、附件和工具结果保存在本地并被 Git 忽略。当前安全策略属于应用层防护，不等同于容器或操作系统级强隔离，因此不应将不可信项目的工作区指向敏感目录。完整架构、API、工具清单、安全边界和已知限制请查看 README.md 与 docs/architecture.md。
