# 上游来源

`src/dsh_trajectory/atif.schema.json` 从 Harbor 的 `harbor.models.trajectories.Trajectory.model_json_schema()` 生成。
模型来自 `UPSTREAM.json` 所记录的 Harbor commit，原始模型采用 Apache-2.0；完整许可位于 `licenses/Harbor.txt`。
生成使用 Pydantic 2.14.2，输出中的字段说明来自上游模型。

`src/dsh_trajectory/known_events.txt` 取自 DeepSeek Harness 的 generated known event catalog。
`examples/native/session.v4.jsonl` 改写自同仓库 `packages/session/session-persistence/tests/contract.ts` 的 `oneTurnLog()`，并添加 canonical v4 header。
它是公开合约样本，不是生产运行数据。
这两项采用上游 MIT 许可，完整许可位于 `licenses/DeepSeek-Harness.txt`。

源代码链接、固定 commit 和生成文件 hash 位于 `UPSTREAM.json`。
