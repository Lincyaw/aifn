# SRT、JSON Schema 和 ATIF

`DeepSeekEngine` 默认启用 SRT，从调用入口的 `Workspace.directory` 自动生成任务目录授权，
不需要重复配置沙箱目录。`sandbox=None` 也表示使用默认策略，不会关闭隔离。
默认控制状态位于 `dsh_home/aifn-control`，原生日志位于 `dsh_home/trajectories`。
可用 `sandbox=SrtPolicy(...)` 增加文件和网络授权或定制控制状态目录。
推荐搭配 `AiFunction(validation="json-schema")`；保留原有 Python 校验模式作为兼容选项。

## 目录与信任边界

```text
/srv/tasks/job-123/                  # 业务工作目录：只有任务文件
/var/lib/aifn/mail/tasks/.../         # 本次任务的 instruction/input/schema/output
/var/lib/aifn/control/<session>/     # 私有策略、启动 patch、就绪凭据
/var/lib/aifn/control/<session>/scratch/ # 仅这个子目录作为 agent 临时目录
/opt/aifn/dsh/                      # DSH 安装、profiles、配置、私有提交凭据
/var/lib/aifn/trajectories/          # 原生 DSH 日志，单独配置
/var/lib/aifn/spool/                # 待入库 ATIF、重试状态和持久化回执
/tmp/asrt-*/                       # 短路径私有网络转接目录，进程结束后清理
```

业务工作目录不能包含控制目录、DSH 安装目录、trajectory 或 spool。任务交换目录也必须
与业务工作目录分开。仅本次任务的交换目录会授予读权限，其 `output/` 子目录可写。
`Workspace.access` 决定业务工作目录是否可写；`FULL_ACCESS` 也不能突破 SRT 的目录授权。

DSH 控制进程在 SRT 外：它读取配置、访问模型接口、执行 Node 校验并记录 trajectory。
`ctx.fs` 的所有实际文件操作通过 RPC 交给 SRT 内的常驻文件进程；Bash 每次执行也通过
SRT。两种工具看到相同的文件和网络策略。原生 sandbox/fs/bash provider 被替换或禁用，
没有再套一层原生 DSH 沙箱。可执行工具限制为 read/write/edit/str_replace_editor/bash
及三个结果工具；其他工具即使被 profile 注册也会被 guard 拒绝。

Agent 可读：显式授权的任务数据、运行二进制/库代码、自己的 scratch，以及私有
`/proc`、`/dev`；SRT 的代理传输 socket 单独开放给内部 relay。没有授权的宿主文件内容被隐藏，`/sys` 也不默认开放。配置和轨迹
不挂进 agent 的命名空间。Shell 的环境先清空，再设置 PATH/HOME/TMPDIR；不会继承
控制进程的模型 API key。`runtime_read` 是必需依赖的只读授权，不能指向含私有配置
或轨迹的目录。默认仅开放系统二进制与库目录（已存在的 `/usr/bin`、`/usr/lib`、`/usr/lib64`），
额外加上 Node、Bash、bubblewrap、socat、ripgrep 可执行文件和已安装 runtime 代码；
非系统安装位置仅授权可执行文件本身。可用 `runtime_read` 按实际依赖替换默认运行依赖授权。

## 安装与运行

Linux x64/arm64，Node.js >=22.19，安装 `bubblewrap`、`socat`、`ripgrep` 和 Bash。
例如 Debian/Ubuntu：

```bash
sudo apt-get install bubblewrap socat ripgrep
uv sync --all-extras
uv run python -m aifn install --dsh-home /opt/aifn/dsh --reinstall
```

完整声明见 [examples/isolated.py](../examples/isolated.py)。核心配置：

```python
function = AiFunction(
    name="count", request=Request, returns=Answer, model=Model(name="deepseek-chat"),
    workspace=Workspace(directory=Path("/srv/tasks/job-123")),
    validation="json-schema",
)
engine = DeepSeekEngine(
    dsh_home=Path("/opt/aifn/dsh"),
    sandbox=SrtPolicy(
        state_root=Path("/var/lib/aifn/control"),
        read=(Path("/srv/reference"),),
        write=(Path("/srv/tasks/job-123/artifacts"),),
        allow_hosts=("example.org:443",),
    ),
    trajectory_directory=Path("/var/lib/aifn/trajectories"),
    archive=TrajectoryArchive(staging=Path("/var/lib/aifn/spool"), sink=central_store),
    env={"DEEPSEEK_API_KEY": os.environ["DEEPSEEK_API_KEY"]},
)
```

额外 read/write/runtime_read 授权使用已存在的绝对真实路径；不接受符号链接作为授权根。
`allow_hosts=()` 禁止 agent 网络访问；规则必须是精确 `host:port`，支持 IPv4 和
`[IPv6]:port`，不接受通配符或 URL 路径。白名单作用于 agent 工具的网络操作；
DSH 控制进程访问模型服务属于控制平面。JSON Schema 模式不启动本机校验 HTTP 端口。

每次运行先执行真实 Linux `doctor`（未授权文件、只读配置、实际 TMPDIR、可写 scratch、宿主
loopback 隔离和代理拒绝检查），再启动 providers。Python 在发送 prompt 前还会检查
本次启动的就绪 nonce；失败会拒绝发送 prompt。CLI：

```bash
node /opt/aifn/dsh/aifn-runtime/policy/src/cli.mjs doctor \
  --policy /var/lib/aifn/control/SESSION/policy.json
```

安装器为固定 SRT 0.0.81 应用两处 Linux 兼容修正：先恢复只读父目录，再恢复显式可写
子目录；将未授权区域的 tmpfs 路径骨架设为只读，阻止创建“影子文件”。补丁前后源码
SHA-256 都固定在 `src/aifn/runtime_patch.py`，未知源码会拒绝启动；原有 denyWrite 与
凭据 mask 的处理顺序保留。代理 socket 使用短私有路径，避免 Linux Unix socket
路径长度限制；payload 会恢复授权 TMPDIR，并把内部代理 localhost 改为数字 loopback，
因此不必向 agent 开放宿主 `/etc/hosts`。

不需要更换 Docker runtime。如果 Harbor 所用容器禁止 user namespace、PID namespace、
mount 或所需 seccomp 操作，自检会失败；此时需要调整容器配置或改在 Linux 宿主运行。
已在 Linux 6.6（TencentOS Server 4，Node 24、Python 3.14）完成真实 SRT 隔离与
端到端验证：未授权路径读写被拒绝、白名单访问成功、未授权端口返回 403、宿主 loopback
无法直连。`DeepSeekEngine` 通过官方 SDK 的 `resume_session()` 恢复会话；
新会话和恢复会话每次启动均根据当前调用的工作目录、权限和任务文件重新生成 SRT 授权。
恢复需要保留原生日志：如果会话后续还会继续，归档配置应使用 `cleanup="none"`。
Shell 输出超过默认 64 KB 捕获上限时只返回尾部；完整输出可重定向到任务内的可写文件。

## 校验一次，再构造模型

1. Node 插件读取本次任务的 JSON Schema，并使用 AJV 校验候选 JSON；Schema 中的静态默认值也在 Node 端填充并校验。
2. `validate_output` 反馈错误。相同文件内容提交时复用校验缓存；内容变更才重新校验。
3. `submit_output` 把 **Node 校验过的规范化 JSON** 写到控制平面的私有提交文件。
   Agent 的 `output/` 下伪造 `submitted.json` 不会被 Python 采用。
4. Python 从私有提交文件接收数据，递归使用 `model_construct()` 创建 Pydantic 对象。
   mailbox 重读与 replay 也走构造路径，不重复运行输出校验器。提交文件绑定
   Schema 的 SHA-256；更换输出模型后不会把旧结果直接构造成另一个类型。

该模式支持 JSON 基础类型、嵌套模型、RootModel、列表/元组、字符串键字典、JSON 标量值的 Enum/Literal
和 optional union。整数限于 JavaScript safe integer 范围，数值必须有限。
`set/frozenset`、`Json[...]`、非 optional union、复杂输入别名、default_factory、model_post_init 及
Python 专用类型会在启动任务前拒绝；这些情况使用默认 `validation="pydantic"`。
JSON Schema 模式不执行 Python 自定义 validators 或隐式类型转换。

## ATIF 与中心存储

`packages/dsh-trajectory` 是仓库内的独立 Python 包，`uv sync --extra trajectory` 会安装。
它将停止写入的 DSH v4 JSONL/Zstd 日志转换为通过 ATIF v1.8 Schema 的压缩 bundle，
支持 SQLite 参考存储和带 token 的 HTTP/HTTPS 中心存储。

`TrajectoryArchive` 在 SDK 会话关闭后执行转换与入库，包括已有完整日志的失败会话。只有回执的 trial/attempt、
内容 hash 和 payload hash 全部匹配且确认 committed，才按 `cleanup` 删除原生日志。
`cleanup="all"` 还删除暂存 bundle，留下小型重试状态和回执。上传失败保留原始数据和
spool，不重跑模型调用；异常中断的会话也保留日志，可用 CLI 手工归档。

`Worker(retain_events=False)` 只在 ATIF 已提交后把完整 mailbox events 替换为
`trajectory/archived` 指针。提交结果与调用状态仍留在 mailbox，成功结果可正常读取和
replay；完整过程分析从中心 ATIF 获取。归档失败时仍保留原 events。

```bash
uv run python -m aifn trajectory process \
  --input /var/lib/aifn/trajectories/PROJECT/SESSION \
  --output /var/lib/aifn/spool/TRIAL/ATTEMPT.atif.json.zst \
  --trial-id TRIAL --attempt-id ATTEMPT --harness-version 0.2.1-rc.5 \
  --endpoint https://trajectories.example.org --cleanup all
```

HTTP token 从 `DSH_TRAJECTORY_TOKEN` 读取。相同 IDs、输入和暂存路径可重试；状态文件
会校验身份与文件 hash。`cleanup="none"` 用于还要继续恢复的原生会话。

Harbor 收集接口保留为 `dsh_trajectory.harbor.collect_and_ingest`：任务停止写入后、销毁
sandbox 前下载指定 trajectory 目录，再执行转换/入库/回执核对/清理。先拷贝再转换
不会减少本次原始数据的传输量；希望减少跨机传输时，需要在 sandbox 内先转换 bundle。
本次集成没有默认启用运行中实时转换。
