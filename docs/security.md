# 运行安全边界

Mini-dsh 是面向可信开发环境的原型。`WorkspaceFiles` 使用目录 FD 和 `O_NOFOLLOW` 拒绝路径穿越与符号链接；`.env` 和指定 DeepSeek 凭据文件受文件工具保护。Shell 只接受 argv 数组和显式白名单，子进程超时会清理进程组，输出返回有界片段并保存完整产物。权限策略默认禁止写操作；评测任务显式授予工作区写权限。

普通 `run` 未设置 `--docker-image` 时，Shell、测试、构建和模型 bridge 在宿主机运行。内置 Git 工具目前仅允许 `status`、`diff` 和 `log`，不提供 Git 写操作；但 Shell 可间接调用 Git，取决于白名单。MCP server 也是宿主机进程，且可能有外部副作用；必须使用可信配置。文件工具的路径限制不限制这些进程自身的文件访问。

Docker 评测使用无网络、只读容器根目录、去除 Linux capabilities、`no-new-privileges`、内存与进程限制，工作区仍可写，容器仍共享宿主机内核。不能把它视为抵御任意恶意代码的强隔离。Docker daemon 的访问权本身很高，只应授予可信操作者。

API Key 的精确值在 Session Event、Context 和产物中脱敏；其他凭据、PII、任意工具输出及异常信息仍可能进入 Trace。`Trace` 与 `.runs` 默认不提交，但操作者必须保护运行目录。文件工具单次读写上限为 1 MB；模型响应、模型输入和 Trace 仍可能消耗较多内存。恢复机制只从完整工作区检查点继续，并拒绝不确定工具结果、变化的工作区与外部模型桥接器。工作区哈希无法证明外部系统状态，因此 MCP、Shell 和 Git 的外部副作用仍需人工核查。详见 [恢复机制](resume.md)。
