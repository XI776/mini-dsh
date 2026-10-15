# Mini-dsh 评测协议

## 输入与隔离

`eval-batch` 读取计划中的任务、策略、重复次数和基础 seed。每次调用 `evaluate` 都从任务 `baseline` 复制新工作区；公开测试在基线中，留出测试断言保存在工作区外的 case manifest，只在 Agent 终态之后写入临时目录并只读挂载为 `/holdout`。启动前还检查留出测试文件的完整内容未直接出现在基线文件中；这只能识别原样泄露，无法识别改写或片段泄露。任务 manifest 和基线内容有 SHA-256 快照。留出测试对模型不可见，但实验操作者和仓库读者可以审查；这里的“隐藏”指运行时对 Agent 隐藏，不是对评测作者保密。

公开开发测试用于 Agent 自查，留出测试仅用于 Agent 结束后的判定；人工审查用例应单独记录人工判断和理由，不计入自动 `success`。当前任务集没有人工审查评分，也不把公开测试通过冒充留出测试通过。

六种策略使用同一任务、模型配置、工具权限、预算、镜像 ID 及测试命令。CLI 在实验开始前将任务中的镜像标签解析为本地 `sha256:` ID，并在 Manifest 中同时记录来源标签和实际 ID；之后按 ID 运行。直接调用 Python API 而未传 `image_resolver` 时会明确标为 `unresolved`。本地镜像 ID 不能替代可跨机器拉取的仓库摘要。每个重复编号内策略顺序轮换，以降低固定顺序影响；任务顺序仍固定。`base_seed + repetition` 传给外部模型 bridge，但 DeepSeek 适配器目前不传 seed。价格快照仅用于估算成本。
`benchmarks/Dockerfile` 已将基础镜像固定到仓库摘要，CI 与 README 的构建命令关闭 provenance 元数据。本地重复构建得到相同镜像 ID；跨机器架构、Docker 实现或构建配置仍可能改变本地 ID。旧批次保存的 `sha256:88132...` 镜像已不在本机；其 Trace 可校验，不能仅凭现有标签重放。新的 `pinned-base-20261015` 批次使用 `sha256:f94cc...`，已另存镜像 TAR 及 SHA-256 校验文件。

## 产物

- `manifest.json`：实验开始/完成状态、开始/结束 UTC 时间、预期运行数、任务输入、基线哈希、评测版本、模型配置摘要、Context 配置、价格快照与策略顺序。
- `runs.jsonl`：每次运行一条结果，含 case/strategy/repetition/seed、相对 Trace 路径、Agent 终态、测试结果和指标。
- `summary.json`：逐策略聚合值。
- `.runs/<run_id>/trace.jsonl`：事件、Context View、模型/工具调用与评测测试结果；大输出产物另存 `artifacts/`。
- 实验目录外的可选 SHA-256 锚点：Manifest、结果和各运行目录文件的独立哈希清单。

Manifest 中不记录 API Key。模型配置中的外部命令只存 argv 哈希与环境变量名，不存环境变量值。Trace 对已配置 API Key 做精确值脱敏，其他任务敏感内容需由操作者控制访问权限。

## 指标定义与限制

| 指标 | 计算与来源 |
| --- | --- |
| Task Success Rate | `evaluation_result.success` 的均值；成功需公开与存在的留出测试都退出码为 0。 |
| Input / Output / Total Tokens | 模型适配器返回的 Usage 求和；脚本模型 Usage 只是固定测试数据。新版实验中有未知 Usage 时，策略均值为 `null`，并记录 `known_usage_runs`。 |
| Context Tokens | 每个 `context_view.estimated_tokens` 之和，按消息字符数近似计算，不是提供方计费 Token。 |
| 工具调用 / 失败 | 终态账本中的 `tool_calls`；`tool_result.status != ok` 的数量。 |
| Agent Steps | 终态账本中的 `steps`。 |
| Wall-clock Latency | Trace 单调时钟毫秒；`agent_wall_ms` 到 Agent 终态，`evaluation_wall_ms` 包含测试。 |
| Recorded Cost | 仅累计供应商明确报告或外部 bridge 明确标注为 `reported` 的费用；`recorded_cost_usd_total` 是已报告部分，缺失调用通过 `unreported_cost_calls` 表示。DeepSeek 目前不返回账单费用。 |
| Estimated Cost | 使用版本化价格快照估算；只有全部调用都可估算且 Usage 完整时才有整次运行值，否则 `null`。价格表计算结果不会写入 Recorded Cost。 |
| Uncached / Billable Tokens | 在供应商提供缓存命中和未命中拆分时计算未缓存输入；总计费 Token 在 Usage 完整时取输入加输出。供应商特殊计费规则仍需单独建模。 |
| Cache Hit Rate | 仅提供方明确返回命中和未命中 Token 拆分时，命中输入 Token / 总输入 Token；否则 `null`。 |

`summary.json` 给出各策略样本数、平均值、总 Token 和测试耗时的最小/最大值，以及每任务最少重复次数。少于 3 次重复时标注 `insufficient_repetitions: true`。即使更多重复，也需要按任务配对、报告波动并检查模型和缓存的时间效应，不能凭汇总均值宣布因果优势。脚本模型固定动作，不会按 Context 改变决策，因此不适合比较策略成功率。

新版模型 Usage 保留提供方原始字段 `raw_usage` 与费用来源 `cost_source`（`reported`、`estimated`、`synthetic`）。脚本模型的成本值默认标为 `synthetic`，不进入实报或价格快照估算。旧 Trace 没有这些字段，继续按旧 schema 回读；其历史 `recorded_cost` 字段可能混合估算和实报，不应直接用于成本结论。新版 manifest 使用 `cost_accounting_version: 2`，校验器按版本重建费用字段。

DeepSeek 适配器对 408、429、5xx 与传输错误做有界重试（默认最多 3 次尝试、指数退避、单次请求不超过本步剩余超时），尝试次数记录在 `model_response` / `model_error` 的 `attempts` 字段。失败的尝试不返回 Usage，因此不进入本地 Token 与费用统计；但供应商是否对已受理后中断的请求计费无法从响应观察，本地统计始终是"成功响应之和"，不是账单。传输层重试也无法区分"请求未送达"与"响应丢失后重发"，后者可能被供应商计费两次。价格快照估算同样只覆盖已记录的成功响应。

## 验证

`uv run agent-harness experiment-report <实验目录> [--baseline full_history]` 按**任务内配对**比较各策略：先取每个任务内各策略的中位数，再给出相对基线的差值中位数、以及有多少个任务的符号与之一致，并同时报告未知 Usage、缓存拆分缺失、预算耗尽与模型重试次数。`summary.json` 的均值会被单个未知 Usage 调用拉成 `null`，且看不出差异来自哪个任务；配对报告用于回答"差异是否在每个任务上同向"，不回答"为什么"。

运行 `uv run agent-harness experiment-verify <实验目录>`。校验器核对运行矩阵、策略轮换、Trace 与记录、测试结论、摘要聚合、模型/Context 快照、价格快照哈希和仍可访问的输入源哈希。缺少测试、Trace 或完整终态会报错；实验未完成会报 `incomplete_experiment`。输入源不可访问时仍可检查保存的快照与 Trace，但不能再次确认源文件未变化。旧版实验 schema 1 支持回读，可能没有输入快照。

完整实验可运行 `uv run agent-harness experiment-seal <实验目录> <外部锚点.json>`，再以 `uv run agent-harness experiment-verify <实验目录> --anchor <外部锚点.json>` 检查文件字节变化。锚点应在实验完成后独立保存；同一操作者若能改写锚点和产物，哈希校验仍无法证明真实性。被 Git 忽略的原始 Trace 也必须另行保存供复核。

`uv run agent-harness experiment-export <实验目录> <锚点.json> <归档.zip>` 先做完整校验，再把锚点、实验 Manifest、结果、Trace、任务 Manifest 与基线源码封入 ZIP；`uv run agent-harness experiment-bundle-verify <归档.zip> --trusted-anchor <独立保存的锚点.json>` 在临时目录复核文件哈希、Trace、实验矩阵和任务输入快照。验包不读取原机器的任务源，`inputs_checked` 给出已核对的任务数。旧版归档没有任务输入，仍可验包但 `inputs_checked` 为 0。归档可用于独立核对输入，重新执行还需相同容器镜像、模型服务及环境。没有单独保存的可信锚点时，ZIP 内部只能做一致性检查。任务 Manifest 可能含留出测试断言，ZIP 只能由评测方保存，不应暴露给 Agent；Trace 也可能包含模型输入和工具输出。

真实 DeepSeek 运行可使用 `benchmarks/real-plan.json` 和 `benchmarks/pricing/deepseek-flash-peak-20261015.json`。后者是 2026-10-15 查询 [DeepSeek 官方价格表](https://api-docs.deepseek.com/quick_start/pricing/) 得到的 Flash 峰时费率，单位美元/百万 Token；它是估算快照，不代表账单。`--deepseek-max-output-tokens 4096 --max-estimated-cost-usd 1.0` 给出单次输出上限和批次本地费用估算守卫；真实输入 Token 可能超过本地预算估计，供应商亦可调整价格，因此该值不是实际扣费上限。

这是一致性校验，不是防篡改签名；攻击者若能同时修改 Trace、记录和 Manifest，可以重建自洽假证据。正式发表结果应将产物哈希固定在独立的版本库或对象存储中，并记录不可变镜像摘要。

## 当前可核验结果

`.runs/experiments/local-fixtures-3`：六个本地任务 × 六策略 × 每策略每任务一次，共 36 次**脚本模型**运行。36 条 Trace 均通过完整性校验，公开与留出测试全通过。这证明 Docker 评测与记录链路工作；Token 固定为每次 75，仅由脚本给定，不说明 Context 效果。每任务重复次数为 1，且运行费用未知。

`.runs/experiments/local-fixtures-pinned-20261015`：在固定本地镜像 ID 后重跑相同 36 次脚本模型评测，36/36 次公开及留出测试通过，36 条 Trace 和 39 个锚定文件均通过校验。独立锚点在 `docs/evidence/local-fixtures-pinned-20261015.json`；原始 `.runs/` 文件未纳入 Git，仅本地可复核。
该实验和新真实模型实验已分别导出到本地 `.exports/`，各自 40 个归档文件通过携带独立锚点的验包；归档目录被 Git 忽略，尚未上传到外部存储。

`.runs/experiments/pinned-base-20261015`：固定基础镜像摘要及构建参数后重新执行 36 次脚本模型评测，36/36 次公开及留出测试通过；36 条 Trace、39 个锚定文件、40 个 ZIP 文件校验通过。对应镜像 TAR 的 45,613,056 字节和 SHA-256 记录在 `docs/evidence/pinned-base-image.sha256`。产物和镜像仍仅在本地，复现还需保存当前仓库版本。

`.runs/experiments/context-example-1d1b71fa`：一个任务 × 六策略 × 一次的真实 DeepSeek 实验，六次公开与留出测试均通过，现有校验器验证 6 条 Trace，`input_snapshots: false`，运行费用未知。只能作为接入冒烟测试。没有证据支持历史描述中的 24 次对照、1,010 次离线执行、10 次真实模型回归和优化比例。
该旧实验现有独立文件哈希锚点 `docs/evidence/context-example-1d1b71fa.json`，但旧 schema 缺失的输入快照不能靠事后哈希补回。

`.runs/experiments/real-context-20261015-b`：两任务 × 六策略 × 每任务每策略三次，共 36 次真实 DeepSeek Flash 运行；36 条 Trace、输入快照及 39 个锚定文件校验通过，27 次公开及留出测试成功。11 次出现 DeepSeek HTTP 503 并缺失完整 Usage，故本批次只作为故障归因与评测链路证据，不用于 Context 效果排名。完整数据及费用口径见 [实验报告](experiment-report.md)。

`.runs/experiments/real-context-20261015-180556`：六任务 × 六策略 × 每任务每策略三次，共 108 次真实 DeepSeek Flash 运行；70 次公开与留出测试同时成功，108 条 Trace 和输入快照均通过校验，35 次触及 Agent 预算。独立锚点 `docs/evidence/real-context-20261015-180556.json` 覆盖 137 个文件；本地 ZIP 的 176 个文件和 6 个任务输入快照通过验包。全部 108 次都有提供方 Usage 和缓存拆分，费用仍只有价格快照估算，没有供应商账单值。任务少、重复有限、执行步数差异大，具体数字及局限见 [实验报告](experiment-report.md)，两个失败路径见 [Trace 案例](trace-cases-20261015.md)。

固定历史价格表的离线重算见 [实验报告](experiment-report.md)。旧 Trace 缺少原始缓存拆分，重算器会标记为未验证；新 Trace 若明确缺少缓存拆分则不给出完整费用估算。重算值不是供应商实报费用。
