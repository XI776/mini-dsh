# Mini-dsh：可插拔 Coding Agent Harness

Mini-dsh 是 Python 3.11 的轻量级 Coding Agent Runtime。它把模型、上下文策略、工具、权限和会话事件拆开，支持自主多轮工具调用，并用独立测试评估代码任务。当前是单 Agent 原型；脚本模型用于确定性验证，不能代表真实模型能力。

## 架构

```mermaid
flowchart LR
  CLI[CLI] --> Loop[Agent Loop / 四类预算]
  Loop --> Context[Context Strategy / 六种策略]
  Context --> Model[Script / Command / DeepSeek]
  Model --> Loop
  Loop --> Runtime[Tool Runtime / Schema / Permission / Timeout]
  Runtime --> Tools[文件 / Patch / Shell / Git / Docker / MCP]
  Runtime --> Loop
  Loop --> Session[JSONL Trace / Context View / Artifacts]
  Session --> Resume[Checkpoint / Resume]
  Resume --> Loop
  Session --> Eval[独立公开与留出测试]
  Eval --> Experiment[实验快照 / 汇总 / 校验]
```

Agent Loop 的终态为 `finished`、`failed`、`cancelled`、`budget_exhausted`。Context 从原始事件生成每次模型调用实际看到的 View；原始事件保持独立。Tool Runtime 统一参数校验、权限、超时、结果和输出截断。评测器每次复制新的基线工作区，Agent 结束后才挂载留出测试。Agent 自报完成不算任务成功。

## 安装与快速开始

```bash
uv sync --locked --dev
uv run --locked pytest
uv run --locked agent-harness run --task '读取 README 并概括项目' --script examples/readme_run.json --workspace .
```

项目使用 `uv.lock` 固定本地与 CI 的 Python 依赖。默认 Trace 在 `.runs/<run_id>/`，该目录被 Git 忽略。`uv run pytest` 仅收集仓库的 `tests/`，不会误收集任务基线中的公开测试。

真实模型使用 DeepSeek。在 `.env` 中设置 `DEEPSEEK_API_KEY`（参照 `.env.example`），或设置同名环境变量；环境变量优先。不要把密钥放进命令行、任务文件或 Git。价格随模型与时段变化，费用估算必须显式提供价格快照。无需额外 API；确定性任务只需要 Docker。

```bash
uv run --locked agent-harness run --task '读取 README 并概括项目' --deepseek --workspace .
uv run --locked agent-harness run --task '读取 README' --model-argv '["python","examples/bridge_model.py"]' --workspace .
```

`--deepseek-model` 指定模型；`--deepseek-pricing` 接收包含 `cache_hit`、`cache_miss`、`output` 的 JSON 美元/百万 Token 价格文件。未提供价格时费用是未知值。`--model-argv` 启动可信模型桥接进程，`--model-env` 指定透传环境变量名。脚本模型给出的 Token 用量是测试数据，不是提供方计费数据。

## 工具与 Context

文件读写、Patch、Git、Shell、测试、构建与 MCP 统一经过 Tool Runtime。普通 `run` 的写入需 `--allow-writes`，Shell 还需 `--allow-command`；不传 `--docker-image` 时 Shell/测试/构建在宿主机运行。`eval` 默认用 Docker 执行测试命令。MCP stdio 配置示例在 `examples/mcp_servers.json`，MCP server 是宿主机可信进程。

普通 `run` 在启动和每次工具结果后保存工作区哈希检查点。进程中断后，可用相同模型、Context、工具、权限和预算参数执行 `resume --run-id <run_id> --script ... --workspace ...`；DeepSeek 可把 `--script` 换为 `--deepseek`。只有上次事件是完整检查点、工作区未变化且模型适配器可恢复时才继续。已完成的工具调用不会重跑；未确认结果的工具请求会记录 `unknown` 并以 `failed` 结束。外部模型桥接器当前不支持自动恢复。详见 [恢复机制](docs/resume.md)。

六种策略由 `--context` 选择：`full_history`、`sliding_window`、`summary`、`repository`、`retrieval_summary`、`python_symbols`。摘要是确定性摘录，仓库检索按词项匹配，Python 符号检索基于 AST；这三者目前都不是独立 LLM 检索或语义搜索服务。

## 评测与实验

先准备 `coding-agent-sandbox:1` 镜像；下面的 Dockerfile 使用 Python 3.11，首次构建可能需要从镜像仓库下载基础镜像。Docker daemon 和当前用户对 socket 的权限必须可用。

```bash
docker build -t coding-agent-sandbox:1 -f benchmarks/Dockerfile .
uv run --locked agent-harness eval --manifest examples/eval_case.json --script examples/eval_fix.json
uv run --locked agent-harness eval-batch --plan benchmarks/plan.json --script-map benchmarks/scripts/map.json --output .runs/experiments/local-$(date +%s)
uv run --locked agent-harness experiment-verify .runs/experiments/<实验目录>
uv run --locked agent-harness trace-verify .runs/<run_id>/trace.jsonl
```

`benchmarks/` 包含六类小任务、公开开发测试、运行后才挂载的留出测试和固定脚本动作。`--script-map` 用于验证隔离、指标和完整性，结果不能用于比较策略能力。真实消融把 `--script-map ...` 换成 `--deepseek`，并设置 `--deepseek-pricing`、`--model-label`、固定任务集和足够重复次数；每次用新的输出目录。当前 DeepSeek 适配器不传 seed，重复运行也不保证确定性。详见 [评测协议](docs/evaluation.md)。

实验保存 `manifest.json`、`runs.jsonl`、`summary.json` 和各次 Trace。Manifest 记录任务及基线哈希、模型配置摘要、Context 配置、权限和预算来源、价格快照、评测版本、运行状态与时间。完整性校验会重建指标，并检测缺失测试、Trace/记录不一致、输入源变化及不完整运行。它只能验证可见产物的一致性，不能防止能同时重写全部产物的攻击者。旧版实验仍可校验，但可能没有输入快照。

离线费用重算不调用模型：

```bash
uv run --locked agent-harness reprice --experiment .runs/experiments/<实验目录> --pricing <价格快照.json> --pricing-model <模型名> --pricing-source <价格来源>
```

## 已有证据与限制

当前仓库的 `.runs/experiments/context-example-1d1b71fa` 含 1 个任务 × 6 种策略 × 1 次的真实 DeepSeek 运行，六次公开与留出测试均通过，六份 Trace 通过校验，输入快照缺失且运行时未计价。这不足以支持策略优劣或总体成功率结论。`benchmarks/` 的确定性脚本实验只证明评测链路工作。历史描述中的“24 次对照”“1,010 次离线执行”“10 次真实模型回归”及成本优化比例，没有可核验原始证据，不能作为本仓库结果。

Docker 容器采用无网络、只读根文件系统、去能力、内存和进程限制，但共享宿主机内核，不能抵御所有恶意代码。宿主机 `run`、模型桥接和 MCP 进程只适用于可信配置。Trace 可能包含任务内容与工具输出；当前仅对配置的 API Key 做精确值脱敏。文件工具单次读写上限为 1 MB。恢复只支持经过工作区哈希检查点的安全边界，不支持对结果不确定的工具调用自动重试。进一步边界见 [安全说明](docs/security.md)。

下一步优先扩大真实模型任务与重复次数、对恢复机制增加更强的工作区快照和故障注入、收紧宿主机与 MCP 的隔离；在此之前不对 Context 策略做性能排名。
