# 工程基线审查（2026-10-15）

当前源码集中在 `src/agent_harness/`：`agent.py` 负责预算化循环，`context.py` 提供六种策略，`runtime.py`、`docker_runtime.py` 与 `mcp_stdio.py` 提供工具执行，`session.py` 保存 JSONL 事件，`eval.py`、`experiment.py`、`experiment_verify.py`、`reprice.py` 组成评测链。原有 `tests/` 覆盖 Agent 终态、工具、DeepSeek、Context、MCP、Trace 和评测校验。

初次审查时 `uv run pytest` 因缺少 pytest 开发依赖、并误收集 `examples/eval_baseline/tests` 而失败；新增 dev 依赖及 pytest 范围后是 41 passed。上一阶段补充评测、文件限制与恢复测试后是 61 passed、0 failed（另有 8 个 unittest subtests）。这些是历史基线，不代表最新测试数量。旧真实模型实验仅一任务六策略各一次，无法支持历史运行规模或优化数字。

发现的配置差异：README 曾同时含目标设计和当前实现，容易把规划误读为现状；已改为运行说明及实际限制。原实验 manifest 不记录完成状态、价格/Context 配置，校验器也不检测源输入变化；本轮升级为 schema 2 并继续支持 schema 1。`Docker` 镜像仍用可变标签，正式对照应使用镜像摘要。普通宿主机工具和 MCP 不构成强隔离。Resume 只支持完整工作区检查点后的继续执行；新版检查点带有格式版本，旧版中断 Trace 不可自动恢复。请勿把单次实验当成性能证据。

本次恢复开发时，当前 WSL 环境执行 `docker info` 返回 `docker: command not found`，因此没有重新运行 Docker 评测；已有确定性实验 `local-fixtures-3` 的 36 条 Trace 和旧 DeepSeek 实验的 6 条 Trace 均再次通过 `experiment-verify`。CI 工作流完成本地 YAML 解析、锁定依赖安装和测试命令验证；GitHub 托管环境尚未实际运行该工作流。

2026-10-15 新一轮工程审计以 61 passed、8 subtests 为起点，补齐 Context View 实际输入一致性、插件回滚、费用来源、Trace Diff 报告和输出存储配额后，`uv run --locked pytest -q` 为 **73 passed、0 failed，另有 8 subtests**。本地 `main` 已有首个项目提交；本轮改动尚未提交或推送。旧 36 次确定性实验及 6 次真实模型实验仍可校验。本轮另完成两次本地脚本 `run` 与 Trace 校验，未重新执行 Docker 批量实验。

本轮 Docker Desktop 已能在授权的沙箱外连接。镜像 `coding-agent-sandbox:1` 解析为本地 ID `sha256:88132ce34ed51563ce37e05d3653efd84f606478633c59c69742d6cbae4dcce1` 后，重新执行了六任务 × 六策略的 36 次脚本模型评测；36/36 次公开及留出测试通过，实验校验 36 条 Trace 通过，独立锚点校验 39 个文件通过。最新全量 pytest 为 **80 passed、0 failed，另有 8 subtests**；`git diff --check` 与包编译通过。旧 DeepSeek 六次运行另建了哈希锚点，但旧输入快照仍缺失。GitHub Actions 仍未在托管环境实际触发。

随后执行的两任务 × 六策略 × 三次真实 DeepSeek Flash 实验共 36 次，27 次通过公开及留出测试；36 条 Trace、输入快照和独立锚点校验通过。11 次调用遇到 HTTP 503 并缺失完整 Usage，影响成功率和费用均值；详见 [实验报告](experiment-report.md)。首次尝试的四条记录因旧守卫中止，仍保留在独立未完成目录，不计入完整实验。

在 DeepSeek 服务不稳定期间，停止调用付费 API；新增实验 ZIP 导出和独立锚点验包，两份完整实验各 40 个归档文件在本地通过搬运验证。CI 工作流加入 Docker 镜像构建、确定性全量评测、实验校验、封存、导出和验包；本地已执行相同评测链路，但新工作流还未在 GitHub 托管环境运行。

此轮最终 `uv run --locked pytest -q` 为 **82 passed、0 failed，另有 8 subtests**。按 CI 新增命令另跑 `ci-fixtures`：36/36 次脚本模型运行通过公开及留出测试，36 条 Trace、39 个锚定文件、40 个 ZIP 文件通过校验；`git diff --check` 和包编译也通过。

随后发现 `Dockerfile` 曾使用可变基础镜像标签；重新构建后旧的本地实验镜像 ID 不再存在。已将基础镜像固定到仓库摘要，并在构建命令中关闭 provenance 元数据；两次本地构建得到相同清单 ID `sha256:f94cc1837dfaef77a81157135c8b0abb2848182519e1a46012ce1b56f5bd06c4`。新 `pinned-base-20261015` 脚本模型批次 36/36 次测试通过，Trace、锚点、ZIP 均通过校验；镜像 TAR 与 SHA-256 校验文件也已本地生成。旧批次仍可做证据校验，但旧镜像缺失，不能仅从当前标签重放。

随后把新版实验 ZIP 扩展为任务 Manifest 与基线源码一并归档，并在验包时按实验快照重新计算输入哈希。`pinned-base-with-inputs-20261015.zip` 校验了 60 个文件和 6 个任务输入；`real-context-with-inputs-20261015.zip` 校验了 47 个文件和 2 个任务输入，均使用单独保存的可信锚点。旧版 ZIP 仍能校验原有证据，但 `inputs_checked` 为 0。新归档包含留出测试定义，只能由评测方保存。

本次归档变更后，`UV_CACHE_DIR=/tmp/mini-dsh-uv-cache uv run --locked pytest -q` 为 **84 passed、0 failed，另有 8 subtests**；`git diff --check`、包编译和 CI YAML 本地解析通过。用 `/tmp` 中的 CI 锚点和已有确定性评测目录另做归档，60 个文件及 6 个任务输入通过验包。GitHub 托管 CI 仍未实际触发，DeepSeek 服务不稳定期间没有新增模型调用。
