# OTLP Trace 上报

云端接收 `POST /v1/traces`，支持 OTLP/HTTP 二进制 protobuf（`Content-Type: application/x-protobuf`）和 gzip。Compose 的 HTTPS 网关已将这个路径转发给云端实例。已有数据库先应用 [Trace 迁移](../migrations/versions/README.md)。

## 启用上报

1. 使用创建 Runner 时保存的完整凭证，格式为 `efr.<token_id>.<secret>`。无需配置独立的上报密钥。
2. 在 Agent 的 OTLP exporter 配置 `https://<域名>/v1/traces`，协议选 `http/protobuf`，请求头设为 `Authorization: Bearer <完整 Runner 凭证>`。

## 写入规则

- Trace 归属于 Runner 的所属用户。云端验证凭证未过期或撤销，且 Runner 和账号仍启用。上报不要求 Runner 的 WSS 在线，也不更新连接或心跳状态。Span 自带的归属属性只作为普通属性保存。
- 单次上报最多 4 MiB（压缩前后均检查）及 4096 个 Span。整批先解码和校验，再在一个事务内分批写入。
- 同一用户下，Trace ID 相同的 Span 聚到一起；同一 Trace、同一 Span ID 保留首次写入。父 Span 可以晚到，只有新入库的 Span 才能扩展 Trace 时间范围。
- 提交后返回 HTTP 200 和 protobuf `ExportTraceServiceResponse`。无效数据整批拒绝，超限返回 413，数据库写入失败整批回滚并返回 503，交给上报端重试。
- 保留已解码的 OTLP 原生字段、属性、事件、链接、Resource 和 Scope。无需 AI 专用语义；不记录原始报文或 Runner 凭证。

Runner 执行评测时为每次 Attempt 创建新的 OpenTelemetry 追踪上下文，通过 HTTP 请求头 `traceparent` 传给 Agent，并在 `jobs.complete` 中回传同一 Trace ID。Runner 自己的客户端 Span 暂不导出；Agent 仍需启用入站埋点和 OTLP exporter。上报后可在工作台「调用链」查看，见 [Trace 查询](api/Trace查询.md)。滚动更新时先升级云端，再升级 Runner，以便云端识别新的结果字段。
