# 工程基线审查（2026-10-15）

当前源码集中在 `src/agent_harness/`：`agent.py` 负责预算化循环，`context.py` 提供六种策略，`runtime.py`、`docker_runtime.py` 与 `mcp_stdio.py` 提供工具执行，`session.py` 保存 JSONL 事件，`eval.py`、`experiment.py`、`experiment_verify.py`、`reprice.py` 组成评测链。原有 `tests/` 覆盖 Agent 终态、工具、DeepSeek、Context、MCP、Trace 和评测校验。

审查开始时 `uv run pytest` 因缺少 pytest 开发依赖、并误收集 `examples/eval_baseline/tests` 而失败；新增 dev 依赖及 pytest 范围后是 41 passed。本轮补充评测、文件限制与恢复测试后，最终执行 `uv run --locked pytest -q` 得到 61 passed、0 failed（另有 8 个 unittest subtests）。项目所有源码与文档当前均未被 Git 跟踪，工作区没有首个提交；`.env`、`.venv`、`.runs` 被忽略。旧真实模型实验仅一任务六策略各一次，无法支持历史运行规模或优化数字。

发现的配置差异：README 曾同时含目标设计和当前实现，容易把规划误读为现状；已改为运行说明及实际限制。原实验 manifest 不记录完成状态、价格/Context 配置，校验器也不检测源输入变化；本轮升级为 schema 2 并继续支持 schema 1。`Docker` 镜像仍用可变标签，正式对照应使用镜像摘要。普通宿主机工具和 MCP 不构成强隔离。Resume 只支持完整工作区检查点后的继续执行；新版检查点带有格式版本，旧版中断 Trace 不可自动恢复。请勿把单次实验当成性能证据。

本次恢复开发时，当前 WSL 环境执行 `docker info` 返回 `docker: command not found`，因此没有重新运行 Docker 评测；已有确定性实验 `local-fixtures-3` 的 36 条 Trace 和旧 DeepSeek 实验的 6 条 Trace 均再次通过 `experiment-verify`。CI 工作流完成本地 YAML 解析、锁定依赖安装和测试命令验证；GitHub 托管环境尚未实际运行该工作流。
