# Mini-dsh 评测协议

## 输入与隔离

`eval-batch` 读取计划中的任务、策略、重复次数和基础 seed。每次调用 `evaluate` 都从任务 `baseline` 复制新工作区；公开测试在基线中，留出测试断言保存在工作区外的 case manifest，只在 Agent 终态之后写入临时目录并只读挂载为 `/holdout`。启动前还检查留出测试文件的完整内容未直接出现在基线文件中；这只能识别原样泄露，无法识别改写或片段泄露。任务 manifest 和基线内容有 SHA-256 快照。留出测试对模型不可见，但实验操作者和仓库读者可以审查；这里的“隐藏”指运行时对 Agent 隐藏，不是对评测作者保密。

公开开发测试用于 Agent 自查，留出测试仅用于 Agent 结束后的判定；人工审查用例应单独记录人工判断和理由，不计入自动 `success`。当前任务集没有人工审查评分，也不把公开测试通过冒充留出测试通过。

六种策略使用同一任务、模型配置、工具权限、预算、镜像标签及测试命令。每个重复编号内策略顺序轮换，以降低固定顺序影响；任务顺序仍固定。`base_seed + repetition` 传给外部模型 bridge，但 DeepSeek 适配器目前不传 seed。价格快照仅用于估算成本。Docker 镜像通过标签指定；镜像内容若发生变化，现有 manifest 不会自动检测，因此正式实验应使用不可变摘要引用。当前脚本任务以 `coding-agent-sandbox:1` 运行。

## 产物

- `manifest.json`：实验开始/完成状态、开始/结束 UTC 时间、预期运行数、任务输入、基线哈希、评测版本、模型配置摘要、Context 配置、价格快照与策略顺序。
- `runs.jsonl`：每次运行一条结果，含 case/strategy/repetition/seed、相对 Trace 路径、Agent 终态、测试结果和指标。
- `summary.json`：逐策略聚合值。
- `.runs/<run_id>/trace.jsonl`：事件、Context View、模型/工具调用与评测测试结果；大输出产物另存 `artifacts/`。

Manifest 中不记录 API Key。模型配置中的外部命令只存 argv 哈希与环境变量名，不存环境变量值。Trace 对已配置 API Key 做精确值脱敏，其他任务敏感内容需由操作者控制访问权限。

## 指标定义与限制

| 指标 | 计算与来源 |
| --- | --- |
| Task Success Rate | `evaluation_result.success` 的均值；成功需公开与存在的留出测试都退出码为 0。 |
| Input / Output / Total Tokens | 模型适配器返回的 Usage 求和；脚本模型 Usage 只是固定测试数据。未知 Usage 通过 `unknown_usage_calls` 标出。 |
| Context Tokens | 每个 `context_view.estimated_tokens` 之和，按消息字符数近似计算，不是提供方计费 Token。 |
| 工具调用 / 失败 | 终态账本中的 `tool_calls`；`tool_result.status != ok` 的数量。 |
| Agent Steps | 终态账本中的 `steps`。 |
| Wall-clock Latency | Trace 单调时钟毫秒；`agent_wall_ms` 到 Agent 终态，`evaluation_wall_ms` 包含测试。 |
| Estimated Cost | 仅所有调用的价格均已知且用量完整时，求价格快照估算值；否则 `null`。`recorded_cost_usd_total` 只是已知部分的下界。 |
| Cache Hit Rate | 仅提供方明确返回命中和未命中 Token 拆分时，命中输入 Token / 总输入 Token；否则 `null`。 |

`summary.json` 给出各策略样本数、平均值、总 Token 和测试耗时的最小/最大值，以及每任务最少重复次数。少于 3 次重复时标注 `insufficient_repetitions: true`。即使更多重复，也需要按任务配对、报告波动并检查模型和缓存的时间效应，不能凭汇总均值宣布因果优势。脚本模型固定动作，不会按 Context 改变决策，因此不适合比较策略成功率。

## 验证

运行 `uv run agent-harness experiment-verify <实验目录>`。校验器核对运行矩阵、策略轮换、Trace 与记录、测试结论、摘要聚合、模型/Context 快照、价格快照哈希和仍可访问的输入源哈希。缺少测试、Trace 或完整终态会报错；实验未完成会报 `incomplete_experiment`。输入源不可访问时仍可检查保存的快照与 Trace，但不能再次确认源文件未变化。旧版实验 schema 1 支持回读，可能没有输入快照。

这是一致性校验，不是防篡改签名；攻击者若能同时修改 Trace、记录和 Manifest，可以重建自洽假证据。正式发表结果应将产物哈希固定在独立的版本库或对象存储中，并记录不可变镜像摘要。

## 当前可核验结果

`.runs/experiments/local-fixtures-3`：六个本地任务 × 六策略 × 每策略每任务一次，共 36 次**脚本模型**运行。36 条 Trace 均通过完整性校验，公开与留出测试全通过。这证明 Docker 评测与记录链路工作；Token 固定为每次 75，仅由脚本给定，不说明 Context 效果。每任务重复次数为 1，且运行费用未知。

`.runs/experiments/context-example-1d1b71fa`：一个任务 × 六策略 × 一次的真实 DeepSeek 实验，六次公开与留出测试均通过，现有校验器验证 6 条 Trace，`input_snapshots: false`，运行费用未知。只能作为接入冒烟测试。没有证据支持历史描述中的 24 次对照、1,010 次离线执行、10 次真实模型回归和优化比例。
