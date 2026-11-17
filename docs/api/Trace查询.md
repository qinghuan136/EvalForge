# Trace 查询

使用浏览器登录会话，查询当前用户的 Trace。Agent 上报仍使用独立的 `POST /v1/traces`。查询响应不缓存。

| 接口 | 请求 | 响应 |
| --- | --- | --- |
| `GET /api/v1/traces` | `page`、`pagesize`（默认 20，最多 100）；可选 `trace_id` 精确筛选 | `{items: TraceSummary[], page, pagesize, total}`，按开始时间倒序 |
| `GET /api/v1/traces/{trace_id}` | 可选 `after_span_id`、`limit`（默认 250，最多 500） | `TraceSummary` 加 `spans: SpanSummary[]`、`next_span_id`。下一批把 `next_span_id` 用作 `after_span_id`；空值表示没有更多 |
| `GET /api/v1/traces/{trace_id}/spans/{span_id}` | 路径中的 Trace、Span ID | `SpanSummary` 加原始属性、事件、链接和其他明细 |

`TraceSummary`：`trace_id`、`title`、`service_name`、`root_span_id`、`status_code` 和起止纳秒时间。名称取根 Span；根未上报时用最早 Span 的名字，`root_span_id` 和 `status_code` 为空。服务名取用于标题的 Span 上报的 `service.name`，可空。

`SpanSummary`：`span_id`、`parent_span_id`、`name`、`kind`、`status_code` 和起止纳秒时间。时间字段为十进制字符串。详情额外返回 `trace_id`、`status_message`、`trace_state`、`flags`、`attributes`、`events`、`links`、`resource`、`instrumentation_scope` 和三个 `dropped_*_count`。

未登录返回 401；不存在或不属于当前用户的单条记录返回 404。列表按 ID 筛选没有命中时返回空列表。参数格式或范围非法返回 422。

Job 详情中的 `trace_id` 用于进入按 ID 筛选的调用链列表。如果结果先于 OTLP 到达，可以稍后刷新列表。
