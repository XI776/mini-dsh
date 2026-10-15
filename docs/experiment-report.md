# 实验与费用证据（2026-10-15）

## 已校验产物

- `.runs/experiments/local-fixtures-3`：六个任务 × 六策略 × 一次，共 **36 次脚本模型运行**；36/36 Trace 与输入快照校验通过，公开及留出测试均通过。脚本动作和 Usage 是固定测试数据，不能用来比较策略成功率或模型费用。
- `.runs/experiments/context-example-1d1b71fa`：一个任务 × 六策略 × 一次，共 **6 次真实 DeepSeek 运行**；6/6 Trace 校验通过，公开及留出测试均通过。旧 schema 1 没有输入快照，每策略样本数仅 1，不能推断一般性能优势。
- `.runs/experiments/context-example-57508ad4`：6 次脚本模型运行，Trace 校验通过；不计入真实模型次数。
- `.runs/experiments/local-fixtures-pinned-20261015`：固定本地镜像 ID 后重跑六任务 × 六策略的 36 次脚本模型运行，36/36 次通过公开与留出测试，36 条 Trace 与 39 个锚定文件校验通过。锚点为 `docs/evidence/local-fixtures-pinned-20261015.json`。
- `.runs/experiments/pinned-base-20261015`：固定基础镜像仓库摘要和构建参数后新跑 36 次脚本模型运行，36/36 次测试通过，36 条 Trace 与 39 个锚定文件通过校验。新版归档 `.exports/pinned-base-with-inputs-20261015.zip` 的 60 个文件与 6 个任务输入快照通过独立锚点验包；旧版 40 文件归档也仍可校验，但不含任务输入。镜像 TAR 及其校验和只保存在本地；见 `docs/evidence/pinned-base-image.sha256`。
- `.runs/experiments/real-context-20261015-b`：两任务 × 六策略 × 每策略每任务三次，共 **36 次真实 DeepSeek Flash 运行**。36 条 Trace、输入快照、费用守卫记录及 39 个锚定文件校验通过。新版归档 `.exports/real-context-with-inputs-20261015.zip` 的 47 个文件与 2 个任务输入快照通过独立锚点验包。锚点为 `docs/evidence/real-context-20261015-b.json`。
- 其他保留的实验目录有汇总或记录不一致，不能计入完整有效实验。

本轮另执行了两次本地脚本模型 `run` 冒烟回归：`9f836624ecd0473f8112c363dd996da5`（full_history）与 `09e31593da68443b8cf2cf37d6a85f62`（sliding_window），两条新 Trace 均通过 `trace-verify`。短任务没有触发滑动窗口裁剪，`trace-diff --report` 未发现输入或行为差异。这两次没有独立测试判定，不计入上述评测成功率。

## 真实模型单任务观察值

下表来自 `context-example-1d1b71fa` 的历史规范化 Usage；费用来自 `reprice-audit-v2-2026-10-15` 对这些 Trace 使用 `examples/deepseek-flash-2026-10-14-offpeak.json` 历史价格快照的**离线估算**，不是供应商实报账单，也不是当前价格。旧 Trace 未保留提供方原始缓存拆分：23 次模型调用都被重算器标记为 `legacy_cache_breakdown_calls`，六次运行的 `cache_breakdown_verified` 均为 false，因此费用值只适合说明重算过程，不能当作已审计的计费结果。

| Context | 次数 | 公开/留出成功 | 总 Token | Cache Read Token | 估算 USD |
| --- | ---: | ---: | ---: | ---: | ---: |
| full_history | 1 | 1/1 | 2300 | 1536 | 0.000259608 |
| sliding_window | 1 | 1/1 | 2223 | 1536 | 0.000213408 |
| summary | 1 | 1/1 | 3984 | 2432 | 0.000440346 |
| repository | 1 | 1/1 | 3188 | 1920 | 0.000308460 |
| retrieval_summary | 1 | 1/1 | 3284 | 2048 | 0.000332394 |
| python_symbols | 1 | 1/1 | 3030 | 1920 | 0.000285210 |

在这一个任务中，`summary` 相对 `full_history` 的 Token 为 **+73.2%**，按该历史快照估算的费用为 **+69.6%**。这些仅是单次观察值，且旧实验缺少输入快照；不能据此排名策略。缓存费率可能使“更少 Token、费用更高”成立，`test_deepseek.py` 有明确标作合成示例的算例；上述真实样本并未出现所预设的“摘要 Token -7.7%、费用 +64%”结果。

## 新真实模型批次：两任务、三次重复

`real-context-20261015-b` 使用 `benchmarks/real-plan.json`、本地镜像 ID `sha256:88132ce34ed51563ce37e05d3653efd84f606478633c59c69742d6cbae4dcce1`、`deepseek-flash`、每次请求最多 4096 输出 Token，以及 `benchmarks/pricing/deepseek-flash-peak-20261015.json` 峰时价格快照。每个任务/策略重复 3 次，策略顺序按重复编号轮换；DeepSeek API 不接收本计划中的 seed，故重复运行并非确定性。价格来自 2026-10-15 查询的 [DeepSeek 官方价格表](https://api-docs.deepseek.com/quick_start/pricing/)。

| 策略 | 成功/运行 | 缺失完整 Usage 的运行 |
| --- | ---: | ---: |
| full_history | 6/6 | 1 |
| sliding_window | 4/6 | 3 |
| summary | 4/6 | 2 |
| repository | 4/6 | 2 |
| retrieval_summary | 5/6 | 2 |
| python_symbols | 4/6 | 1 |

总体 27/36 次公开与留出测试成功。11 次运行出现 `DeepSeek HTTP 503`，造成未知 Usage；其中 8 次失败、3 次后来成功。其余 25 次 Usage 完整的运行有 24 次成功，另 1 次因 Agent 预算耗尽失败。由此，原始成功率受供应商故障明显影响，不能据此比较 Context 策略。两任务、每任务每策略三次样本也不足以支持一般性的性能结论。

25 次 Usage 完整运行中，按快照可估算的费用合计为 **$0.019277004**；未知 Usage 的调用不加入这个值。本地费用守卫把未知运行按预留额度记账，36 次合计 **$0.231744204**，低于设定的 $1.0 本地估算阈值。DeepSeek 未返回账单费用，因此两个数都不是实际扣费。由于 11 次运行缺失 Usage，六策略的完整 Token 与费用均值均为 `null`，不应从部分数据补算策略排名。

首次尝试 `.runs/experiments/real-context-20261015` 在第 4 次调用遇到 HTTP 503 后，由旧费用守卫中止；其 Manifest 为 `running`，保留作中断证据，不计入完整批次。随后改为对未知 Usage 单独预留费用并从新目录重跑，没有覆盖旧产物。

## 未验证数字与环境

没有找到可验证的“1,010 次离线执行”或历史所称“24 次真实 Context 对照”“10 次真实模型回归”的原始证据，因此不把这些历史数字作为本仓库结果。本轮新增的 36 次真实模型运行另有 Manifest、Trace 和锚点支持，但不等同于那些历史宣称。Docker Desktop 在授权的沙箱外可用；本地重新执行了容器评测。复现命令和指标定义见 [评测协议](evaluation.md)。
