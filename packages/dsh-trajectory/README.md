# dsh-trajectory

把 Harbor 下载的 DeepSeek Harness 原生日志转换为 ATIF，入库确认后清理本地文件。
支持 Linux、macOS 和 Python 3.11 以上版本。

## 安装与试用

在项目目录运行：

```bash
python3 -m pip install .
dsh-trajectory trajectory convert \
  --input examples/native \
  --output /tmp/demo.bundle.json.zst \
  --trial-id demo --attempt-id 1 --harness-version contract-fixture
dsh-trajectory bundle validate --input /tmp/demo.bundle.json.zst
dsh-trajectory bundle ingest --input /tmp/demo.bundle.json.zst --store /tmp/demo-center.sqlite
dsh-trajectory store search --store /tmp/demo-center.sqlite --query hello
dsh-trajectory bundle export --input /tmp/demo.bundle.json.zst --output /tmp/demo-atif
```

示例改写自上游的 `oneTurnLog()` 合约样本，不是生产任务记录。
`bundle export` 输出独立的标准 ATIF JSON，供 Harbor 或其他 ATIF 工具读取。
压缩 bundle 是本包的传输容器，包含 manifest 和多个 ATIF trajectory，不是 ATIF 标准文档本身。

## 在本地完成转换与清理

把某次任务下载的 sessions 单独放进 staging 目录，停止写入后执行：

```bash
dsh-trajectory trajectory process \
  --input /srv/staging/trial-001/native \
  --output /srv/staging/trial-001/bundle.json.zst \
  --trial-id trial-001 --attempt-id 1 --harness-version YOUR_DSH_VERSION \
  --store /srv/trajectory-center/center.sqlite \
  --cleanup all
```

`--store` 指向中心机器本地文件系统上的 SQLite 数据库。
宿主机与中心分开部署时，使用下一节的 `--endpoint`。
不要把数据库放进 staging 目录或 NFS 共享卷。

| `--cleanup` | 中心确认后执行的操作 |
|---|---|
| `none` | 默认值，保留源日志和 bundle |
| `sources` | 删除实际转换的源日志 |
| `all` | 删除实际转换的源日志和 staged bundle |

清理逐个核对源文件 SHA-256，只处理发现并转换的 canonical session 文件。
不会递归删除输入目录、配置、credentials、锁文件或未转换的历史 generation。
选定同一 session 的最高 generation；旧版本需要先通过匹配的 Harness 迁移为 v4。
不同 session 可以用 `--workers 4` 并行转换。

输出目录保留小型 `.state.json` 回执和 `.lock` 文件，前者用于恢复清理与避免重复入库。
`.state.json` 含本机源文件路径及 hash，不含原始消息正文，也不会上传中心。
失败时原命令可以重跑；输出路径、trial、attempt、harness 版本、输入路径、partial 选项和中心地址必须一致。
同一 trial/attempt 的不同内容会返回冲突，新的执行请使用新的 attempt ID。

`--dry-run` 不写文件、不入库、不删除，也不证明输入可以转换。
它返回操作范围；完整校验使用 `trajectory convert` 和 `bundle validate`。

## 独立部署中心

中心服务用 SQLite 保存压缩 bundle，并在同一个事务中建立 `runs`、`trajectories` 和 FTS5 `steps` 索引。
事务使用 `synchronous=FULL`，提交之后才返回带内容 hash 和传输 hash 的回执。
重复入库返回成功，同一 trial/attempt 的内容冲突拒绝覆盖。

中心机器设置一个随机令牌后启动服务：

```bash
export DSH_TRAJECTORY_TOKEN='REPLACE_WITH_A_RANDOM_SECRET'
dsh-trajectory store serve --store /srv/trajectory-center/center.sqlite
```

默认监听 `127.0.0.1:8765`，通过 HTTPS 反向代理对外提供服务。
这个串行参考服务适合验证流程和单机中心，不承诺大规模并发吞吐。
多实例、高可用或大规模列式统计需要替换 `Sink` 或中心服务；本版没有实现 S3、PostgreSQL、ClickHouse 后端。

Harbor 宿主机设置相同令牌后执行：

```bash
export DSH_TRAJECTORY_TOKEN='REPLACE_WITH_THE_SAME_SECRET'
dsh-trajectory trajectory process \
  --input /srv/staging/trial-001/native \
  --output /srv/staging/trial-001/bundle.json.zst \
  --trial-id trial-001 --attempt-id 1 --harness-version YOUR_DSH_VERSION \
  --endpoint https://trajectory.example.com \
  --cleanup all
dsh-trajectory store search --endpoint https://trajectory.example.com --query 'timeout OR error'
```

私有 CA 使用 `--ca-file /path/to/ca.pem`。
`--allow-http` 显式允许明文 HTTP，限本机联调或受控代理网络使用。
客户端拒绝重定向，避免将认证令牌转发给其他地址。
请求超时可以用 `--timeout` 修改。

服务接口：

| 方法与路径 | 内容 |
|---|---|
| `POST /v1/bundles` | 请求体为 Zstandard bundle，响应为持久化回执 |
| `GET /v1/search?q=hello&limit=20` | 检索模型、消息、推理、工具调用与观察文本 |
| `GET /v1/bundles?trial_id=...&attempt_id=...` | 下载中心保存的压缩 bundle |

所有接口要求 `Authorization: Bearer ...`。
数据库事务包含原始传输包和索引，因此成功回执后不依赖宿主机 staging 文件恢复数据。
`steps` 使用 SQLite FTS5 `unicode61` 分词；它不是中文分词或语义检索服务。
输入、bundle 解压后内容和 HTTP 请求的默认大小上限为 256 MiB，可通过 `--max-bytes` 调整。
转换内存与单次任务日志及输出体积相关；这是任务结束后的批处理转换器。

## 接入 Harbor

包提供 `collect_and_ingest`，只依赖 Harbor environment 的 `download_dir` 接口，不强制安装 Harbor。
在 agent 停止写入后、销毁 environment 前调用。
如果 Harbor 仍要消费本地 `trajectory.json` 来展示或补充用量，应先导出 ATIF 并完成该消费，再清理。
本包不会自动注册 trial hook、修改 Harbor 安装或替换现有 agent 适配器。

```python
import os
from pathlib import Path

from dsh_trajectory.harbor import collect_and_ingest
from dsh_trajectory.transport import HTTPStore

receipt = await collect_and_ingest(
    environment,
    remote_sessions="/logs/agent/dsh-home/sessions",
    staging_dir=Path("/srv/staging"),
    sink=HTTPStore(
        "https://trajectory.example.com",
        token=os.environ["DSH_TRAJECTORY_TOKEN"],
    ),
    trial_id=trial_id,
    attempt_id=attempt_id,
    harness_version=installed_dsh_version,
)
```

`remote_sessions` 必须与 sandbox 中实际配置的 `DSH_HOME/sessions` 对应。
这个助手回收专用 session 目录；若 Harbor 已下载它，直接调用 `pipeline.process`，避免重复下载。
宿主机清理后，sandbox 中的日志仍由 sandbox 生命周期负责。
验证器产生的 reward 和任务元数据目前由调用者另行关联，本包不猜测 trial 的最终结果。

## 数据保真与失败处理

支持 Harness session format v4，输出 ATIF-v1.8。
输入 schema 与词汇基于 `UPSTREAM.json` 中记录的 commit。
ATIF JSON Schema 从 Harbor 的 Pydantic 模型生成，另加连续 step ID、工具引用、agent 专属字段等语义校验。

保留消息文本、推理、工具参数和结果、错误标记、模型信息、token 用量、父会话关系、继承上下文标记，以及 surface replacement 的源坐标。
Harness 的 uncached input、cache read、cache write 三项相加得到 ATIF prompt tokens。
继承上下文的 metrics 留在对应 step，但不重复计入当前 trajectory 的 final metrics。
失败模型 attempt 的原始 attempt 数据保存在 system step 的 `extra.dsh.attempt`。
非法工具参数保留在 `tool_calls[].extra.dsh.raw_arguments`，并标记 `invalid_arguments`。
多个 session 各自导出独立 trajectory，以 `extra.dsh.parent_trajectory_id` 连接父会话，避免重复嵌入整份子会话。

ATIF 是分析投影，不提供 Harness 原生恢复能力。
成功 assistant 的原始分片时间序列不复制；provider replay state、工具 UI meta 和非消息运维事件不作为完整原生日志归档。
非消息事件按类型记录数量，实际被省略的类型可在 `extra.dsh.omitted_event_counts` 查看。
surface replacement 保留为时间线上的后续记录，不折叠为最终模型上下文；训练数据加工必须解释这些坐标。

图片、文件和未知内容块目前会拒绝转换，源文件保留。
未知 required event、未来或旧版格式、重复 JSON key、seq 缺口、截断 JSONL、损坏或不完整的 Zstandard frame 都会拒绝转换。
`--allow-incomplete` 只允许已完整落盘、但 turn/step/工具调用尚未闭合的日志，产物明确标记为 `incomplete`。
它不会跳过物理损坏，也不会伪造工具执行结果。
格式拒绝、转换失败、中心不可达、认证失败或回执不匹配时，都不会触发源文件清理。
源文件在中心确认后发生变化时，保留变化后的文件并报错。

## CLI 约定

所有成功结果以 JSON 写到 stdout，错误以 JSON 写到 stderr，不读取交互输入。
参数帮助通过 `--help` 逐级查看。

| 退出码 | 含义 |
|---|---|
| `0` | 成功，包括同内容重复入库 |
| `2` | 参数错误 |
| `3` | 文件或记录不存在 |
| `4` | 权限或认证失败 |
| `5` | 标识冲突或源数据变化 |
| `6` | 中断 |
| `10` | 格式或语义校验失败 |
| `11` | I/O、中心服务或回执失败 |

`retryable` 表示是否适合使用相同标识、输入和 staging 路径重试。
部署前应拿实际安装版本产生的日志跑一次完整任务；本次验证环境没有连接真实 Harbor sandbox。
