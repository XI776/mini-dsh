# Trace 差分

```bash
uv run --locked agent-harness trace-diff --report .runs/<left>/trace.jsonl .runs/<right>/trace.jsonl
```

报告分别给出首次 Context 输入差异、模型行动差异、工具请求差异、工具结果差异、终态差异、测试差异、Usage 差异及预算/终态差异。`first_observable_behavior_difference` 排除 Context 输入本身，因此输入先变化而模型行动相同时，两个字段会给出不同结论。

每项证据包含左右 Trace 路径、事件序号及 Agent 步数。`config_difference` 先比较任务、预算、模型、工具目录和评测设置；配置不同的运行不适合直接解释为 Context 策略导致的变化。原有不带 `--report` 的命令仍返回单个首次差异以兼容旧用法。Trace 差分定位相关事件，不能仅凭事件先后证明因果关系。
