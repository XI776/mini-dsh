# 目标能力与证据矩阵（2026-10-15）

| 能力 | 实现 | 自动测试 | 运行证据 | 状态 |
| --- | --- | --- | --- | --- |
| Agent Loop、四类预算和终态 | `agent.py`、`contracts.py` | `test_harness.py`、`test_resume.py` | JSONL Trace | 已实现且验证；阻塞中的模型请求不能即时取消 |
| Protocol / DI、事件输出 | `contracts.py`、`agent.py` | `test_harness.py::test_protocol_injection_accepts_memory_session_runtime_and_event_sink` | 内存会话测试 | 已实现且验证 |
| 插件依赖、顺序与回滚 | `plugins.py` | `test_harness.py` 插件测试 | 启动和回滚测试 | 已实现且验证；通用 PluginGroup 尚未接入 CLI 配置 |
| 六种 Context 策略与索引刷新 | `context.py` | `test_harness.py` | 六策略脚本实验、旧 DeepSeek 实验 | 工程能力已验证；策略收益验证不足 |
| 实际模型输入的 Context View | `agent.py`、`deepseek.py`、`trace_verify.py` | `test_deepseek.py::test_trace_context_view_matches_sent_messages` | 新版 View 内容哈希 | 已实现且验证；旧 Trace 不含新版哈希 |
| Tool Runtime、文件路径与超时 | `runtime.py`、`process.py`、`docker_runtime.py` | `test_harness.py`、`test_v2.py` | 工具结果及产物哈希 | 已实现且测试验证；宿主机命令不是强隔离 |
| MCP 与不确定副作用恢复 | `mcp_stdio.py`、`agent.py` | `test_v3.py`、`test_resume.py` | `tool_state` 事件 | 已实现且测试验证；远端状态不能自动确认 |
| Resume | `resume.py`、`session.py`、`agent.py` | `test_resume.py` | 完整检查点测试 | 已实现且验证；仅普通 `run` 的安全检查点 |
| Trace Diff | `trace.py` | `test_trace.py` | `trace-diff --report` | 已实现且验证；比较可观察事件，不证明因果关系 |
| 公开/留出测试、输入快照和固定镜像 ID | `eval.py`、`docker_runtime.py`、`experiment.py`、`experiment_verify.py` | `test_v2.py`、`test_experiment_verify.py` | `pinned-base-20261015` 36/36 | 已实现且在 Docker 中重新验证；基础镜像摘要固定，镜像 TAR 本地导出 |
| 实验外部哈希锚点 | `evidence.py` | `test_experiment_verify.py` | `docs/evidence/` 与 `.runs/` 本地复核 | 已实现；锚点及原始 Trace 需分别可信保存 |
| 可搬运实验归档 | `bundle.py` | `test_experiment_verify.py` | 新版确定性 ZIP 60 文件、6 个任务输入；真实模型 ZIP 47 文件、2 个任务输入，均通过独立锚点验包 | 已实现并本地验证；归档尚未上传，重新运行仍需镜像与模型服务 |
| 真实模型 Context 消融 | `experiment.py` | 旧产物与新版锚点可校验 | `real-context-20261015-b` 36 次，27 次任务成功 | 已运行两任务三次重复；11 次 HTTP 503，不能据此排名策略 |
| 缓存与费用归因 | `deepseek.py`、`reprice.py`、`experiment.py` | `test_deepseek.py`、`test_reprice.py`、`test_experiment_verify.py` | 历史价格快照离线重算和新版估算守卫 | 估算链路已验证；没有供应商实报费用；未知 Usage 预留额度 |

审计时发现并在本轮修复：DeepSeek 消息角色转换发生在 View 记录之后；插件启动失败未清理失败插件自身；价格快照估算和实报费用混用；未知 Token 用量进入均值；工具输出落盘没有单次配额。历史 Trace 仍按其原格式回读，不会被改写。
