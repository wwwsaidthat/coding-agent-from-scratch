# Bugfix demo

这是一个故意保留缺陷的小型项目，用于演示 coding agent 的端到端工作过程。

## 任务

`slugify` 应将普通文本转换为适合 URL 的 slug，但当前实现不能正确处理首尾空白、连续分隔符、下划线和标点。

可以给 Agent 以下任务：

```text
阅读这个项目，修复 slugify 函数，使所有测试通过。完成后运行全部测试并总结修改。
```

在项目根目录运行：

```bash
python3 main.py --workspace examples/bugfix_demo \
  "修复 slugify 函数，使所有测试通过；完成后运行全部测试并总结修改"
```

初始测试命令：

```bash
python3 -m unittest discover -s tests -v
```

此目录中的失败测试是演示素材，不属于主项目测试失败。
