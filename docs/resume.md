# 最小可用 Resume

`run` 在启动和每次工具结果处理后写入版本 2 的 `workspace_checkpoint`，包含工作区文件名、类型和内容的 SHA-256 摘要。Trace 根目录被排除，以免 Session 自身写入改变摘要；符号链接本身纳入检查但不会遍历其目标目录。检查覆盖 `.git` 等工作区内容；最多扫描 10,000 个条目、512 MB 文件字节，超过上限时不保证可恢复。旧版检查点使用不同的哈希范围，不支持自动恢复；旧 Trace 仍可用于查看和完整性校验。

`JsonlSession.open_existing` 校验 JSONL 完整行、事件序号和 run ID，并继续追加同一 Trace。`AgentLoop.run(resume=True)` 从原始事件恢复任务、消息历史、步数、模型 Token/费用账本、工具调用数和剩余墙钟预算。模型、Context、工具 Schema、权限与预算必须和原启动配置一致。脚本模型跳过已完成的固定响应；DeepSeek 适配器按无状态请求继续。外部命令模型桥接器未声明可恢复，因其进程可能保存不可见状态。

工具状态通过 `tool_state` 事件区分：模型规划后为 `pending`，请求已记录时为 `running`，有结果后为 `completed` 或 `failed`；进程中断后未配对的工具请求标记 `unknown`。只有最后事件是完整检查点、所有规划工具均有结果、工作区哈希仍匹配时才继续。已完成调用保留结果，不再执行。未知模型响应、未知工具结果、不完整工具批次、工作区变化或配置变化会写 `recovery_blocked` 和 `failed` 终态；未知工具结果额外写入 `tool_result.status=unknown`，不执行重试。

示例：

```bash
uv run agent-harness run --task '修复代码' --script script.json --workspace ./work --allow-writes
uv run agent-harness resume --run-id <run_id> --script script.json --workspace ./work --allow-writes
uv run agent-harness trace-verify .runs/<run_id>/trace.jsonl
```

恢复是保守的本地继续执行，不是任意时间点重放。Trace 保留消息和 Context View；新策略实例根据原始事件和当前工作区重建后续 View。工作区哈希不能检查远端 API、Git remote、数据库或 MCP server 的外部状态；对于结果不确定的外部操作只能失败并人工核查。Shell、Docker 命令和 MCP 工具若返回非 `ok` 结果，恢复同样会阻止继续执行。恢复机制目前只用于普通 `run`，不支持未完成的 `eval` 容器任务。实验目录中的 Trace 可以验证，但不能把它们作为 `run resume` 的输入。
