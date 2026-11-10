# Agent 流式输出设计

本文记录拟实施的第一版。用户创建实验时选择流式模式，Runner 逐片读取 Agent 的 SSE 响应，云端实时接收片段并累积预览。最终结果沿用 `jobs.complete` 落库，随后触发评分。

## 执行开关与兼容

- 实验 `execution_config` 保存 `stream_output`，默认 `false`，同一实验的所有 Job 使用保存时选定的模式。
- 云端沿用 `jobs.dispatch`。流式任务携带 `stream_output: true`，仅派给在 `runner.hello` 声明 `http_sse` 的 Runner。普通任务保持原报文。缺少对应能力时，流式任务继续排队。
- Runner 接单时把模式写进 SQLite 的 Attempt，并纳入重复派发校验。升级时将旧记录按 `false` 迁移。普通执行读完整 JSON，流式执行按 SSE 事件读取，遇到不匹配的响应类型时报告错误。请求体继续使用样本的原始 JSON。

## 片段与最终结果

```text
Agent SSE → Runner 累积 → 完整 output → SQLite → jobs.complete → PostgreSQL → Eval
                 └──→ WSS 片段通知 → 云端累积预览 → 后续前端订阅
```

- Agent 协议先约定两类事件。`delta` 提供文本片段，`result` 提供最终 JSON 并结束流。正常结束前断流按失败处理。
- Runner 的 `OutputChunk` 携带片段序号和内容，`OutputAccumulator` 累积本次输出。客户端按批发送 `jobs.output_chunk` 通知，附带 `job_id` 和 `attempt_id`。片段发送允许丢失，Agent 读取和最终结果提交独立推进。
- 云端校验会话和 Attempt 归属，按 `(job_id, attempt_id)` 累积预览，保留从开头收到的内容，并记录最后序号、缺片和截断状态。缓冲设置容量上限，结束后短期保留。未来前端可用快照的序号衔接增量。跨进程切换或 Runner 断线可能丢失部分预览。
- 云端按 `jobs.complete` 提交的完整输出结算，沿用 ACK、重连补传和 Eval。提交后关闭该 Attempt 的预览写入。

## 实施顺序

1. **配置和派发。** 接入实验开关、Runner 能力、SQLite 迁移与兼容派发。
2. **Runner 读取流。** 实现 SSE 解析、片段累积和最终 JSON 提交，覆盖断流、超时和取消。
3. **云端实时预览。** 接入批量片段通知、Attempt 归属校验和进程内累积缓冲。用一个分段响应的 Agent 验证云端能在 `jobs.complete` 前读到片段，最终输出可正常评分。

前端订阅、跨实例预览转发和首片延迟等统计留作后续工作。借鉴 Phoenix 的片段与最终结果分流设计，并复用 EvalForge 的最终结果协议。 [现有 Runner 协议](EvalForge_Runner_JSON-RPC_Protocol_v1.md#25-jobscomplete) · [现有评分链路](server-evaluations.md)
