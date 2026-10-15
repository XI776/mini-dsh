# Context 策略与模型输入

| 策略 | 保留或生成的内容 |
| --- | --- |
| `full_history` | 任务与可用的历史模型/工具消息 |
| `sliding_window` | 任务与最近若干条历史消息 |
| `summary` | 任务、早期事件的确定性摘录、最近消息 |
| `repository` | 任务、词项检索结果、最近消息 |
| `retrieval_summary` | 摘录、词项检索结果、最近消息 |
| `python_symbols` | AST 符号命中、最近消息 |

所有策略实现相同的 `ContextStrategy.build(events, task, token_budget)` 接口。`summary` 是确定性摘录，不是 LLM 语义压缩。`ContextView.metadata.selection` 记录按本地 Token 预算保留、丢弃及截断的事件 ID；窗口策略另记被窗口排除的范围，摘要策略另记摘录范围。任务消息始终置于 View 开头；预算不足时 Agent 给出预算耗尽终态。

仓库索引按文件信息扫描，并在工具结果可能修改工作区或外部文件信息变化时重建；不会递归跟随符号链接目录。索引快照 ID 与刷新原因写入 View 元数据。词项检索与 Python AST 索引并非语义搜索，文件系统竞态和未扫描的隐藏文件仍是边界。

Session Event 是追加的执行事实，Context View 是某次模型请求的输入。新版 View 保存消息、关联事件 ID、策略、配置来源、估计 Token 数和消息 SHA-256。DeepSeek 的工具消息角色转换在记录 View 前完成；测试直接比较了 HTTP 请求消息与 Trace 中的 View。Trace 里的本地 Context Token 数不是供应商计费 Token。
