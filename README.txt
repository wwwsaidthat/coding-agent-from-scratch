Coding Agent from Scratch（LoopCoder）项目说明

一、Git 仓库地址
https://github.com/wwwsaidthat/coding-agent-from-scratch

二、如何运行
环境需要 Python 3.10 或更高版本。代码搜索需要 ripgrep，PDF 理解需要 Poppler。在项目根目录执行：

cp .env.example .env

在 .env 中填写自己的 DEEPSEEK_API_KEY。如需联网搜索、图片或 PDF 理解，再填写 QWEN_API_KEY 和对应服务地址。请勿提交包含真实密钥的 .env。

启动网页版：
python3 main.py --web

然后访问 http://127.0.0.1:8765。也可执行无需 API Key 的离线演示：
python3 main.py --demo --workspace /tmp/coding-agent-demo "运行离线工具演示"

运行测试：
python3 -m unittest discover -s tests -v

三、特色功能
本项目不使用 Agent 框架或 Agent SDK，从零实现“模型—工具—结果—模型”自主循环。主模型负责任务规划与工具调度，本地程序负责文件读写、代码搜索、命令执行和结果回传。系统支持多轮会话、会话与工作区绑定、Plan 进度、执行 Trace、上下文记忆和 70%/80%/90% 分级压缩。文件编辑强制先读后改，通过哈希检测冲突，修改前展示红删绿增 Diff 并等待用户批准。项目还具备路径越界防护、敏感文件拦截、命令白名单、重复调用检测和最大步数限制。可选的通义千问模型用于联网搜索及图片、PDF 理解，数据外传前会请求用户确认。

四、其它说明
项目默认只监听本机地址，用于本地代码任务，不建议直接暴露到公网。会话、Trace、附件和工具结果保存在本地并被 Git 忽略。完整架构、API、工具清单、安全边界和已知限制请查看 README.md 与 docs/architecture.md。
