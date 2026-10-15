# 真实模型 Trace 失败归因案例（2026-10-15）

原始数据来自 `.runs/experiments/real-context-20261015-180556`。比较时选取**同一任务、同一 Context 策略**的成功与失败运行；`trace-diff --report` 的 `config_difference` 均为 `null`。运行的重复编号不同，DeepSeek 不接收实验 seed；首个可观察分叉只能定位证据，不能单凭时间先后认定某个策略或 Context View 导致失败。下述留出测试仅在 Agent 结束后执行。

## 案例 1：提前结束，未执行所说的工具调用

- 任务：`text-keep-case`，策略：`full_history`。成功运行 `0b7974ca9bab454b911ab53186c5bc97`（重复 0）与失败运行 `b0013b077ad04a21bd60c86bc2e05435`（重复 1）。
- `trace-diff` 报告首个模型行动差异在第 1 步、两侧 `seq=5`；首个 Context View 差异在第 2 步、两侧 `seq=16`。这两个差异不能证明后者造成前者。
- 失败运行的第 3 步 `model_response`（`seq=31`）返回 `finish=true`、`tool_calls=[]`，消息里却写了要读取 `importer.py` 等文件。Agent 因 `model_finished` 结束（`seq=32`），没有文件补丁；公开和留出测试退出码均为 1（`seq=33–35`）。
- 成功运行在第 3 步分别补丁 `util/text.py` 与 `importer.py`，运行后公开和留出测试均通过。可定位的直接问题是**模型把行动描述当成了已执行操作并提前结束**；Agent 的自报完成不能代替测试判定。

复核命令：

```bash
uv run --locked agent-harness trace-diff --report .runs/0b7974ca9bab454b911ab53186c5bc97/trace.jsonl .runs/b0013b077ad04a21bd60c86bc2e05435/trace.jsonl
```

## 案例 2：补丁路径错误后未修复跨文件行为

- 任务：`text-keep-case`，策略：`sliding_window`。成功运行 `5d499330372b49fcb74838a77eb64b46`（重复 1）与失败运行 `fb87d1b3b9a3495997853f423f1d92eb`（重复 2）。
- `trace-diff` 报告首个模型行动差异在第 1 步、两侧 `seq=5`；首个 Context View 差异在第 2 步（成功 `seq=16`，失败 `seq=11`）。
- 失败运行第 8 步向不存在的 `util/importer.py` 请求 `patch_file`（`seq=118`），工具以 `execution_error` 返回文件不存在（`seq=120`）。之后 Agent 继续运行至第 23 步，没有成功给实际的 `importer.py` 补丁；公开测试退出码为 0，留出测试退出码为 1（`seq=348–350`）。
- 成功运行第 5 步补丁 `util/text.py` 和正确的 `importer.py`（`seq=75,79`），两套测试均通过。这个案例显示**工具错误虽被结构化记录并反馈，模型仍可能恢复不充分**；公开测试通过也不能替代留出测试。

复核命令：

```bash
uv run --locked agent-harness trace-diff --report .runs/5d499330372b49fcb74838a77eb64b46/trace.jsonl .runs/fb87d1b3b9a3495997853f423f1d92eb/trace.jsonl
```

归因边界：以上是两个具体运行的可观察失败机制，不是六策略的因果消融结论。若要检验某个 Context 机制，应固定更多环境变量、增加任务和重复，并单独分析执行长度及预算耗尽。
