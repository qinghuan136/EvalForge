# Agent HTTP 调用层（第一步）

本次只实现一次 HTTP 调用。JobManager、容量控制、jobs.started、持久化和结果上报不在本层。

## 入口与边界

- [`targets.py`](../../client/evalforge_client/targets.py)：将本地目标配置转换为 AgentTarget，校验 URL 和请求头。
- [`http_executor.py`](../../client/evalforge_client/http_executor.py)：HttpExecutor 管理 HTTPX 连接资源，execute 返回 HttpExecutionResult。
- [`config.py`](../../client/evalforge_client/config.py)：启动配置检查复用同一套目标校验。
- [`test_http_executor.py`](../../client/tests/test_http_executor.py)：单元测试及临时本机 HTTP 服务测试。

```text
本地目标配置 → 固定目标快照
agent_target_id + input_json + timeout_seconds
  → 严格 JSON 编码
  → POST 本地配置的 URL
  → 在总超时内读取完整响应
  → 收集 output / http_status / duration_ms / error
```

客户端不抽取 answer，不改写消息，不添加 Job ID 或参考答案。原样指 JSON 内容保持一致，
不是保留空格、键顺序或原始 HTTP 字节。请求和响应支持对象、数组、字符串、数字、布尔值与 null。

## 使用方式

```python
from evalforge_client.http_executor import HttpExecutor

targets = {
    "order-agent": {
        "url": "http://127.0.0.1:8001/invoke",
        "headers": {"X-Mode": "evaluation"},
        "headers_env": {"Authorization": "ORDER_AGENT_AUTHORIZATION"},
    }
}

# 环境变量 ORDER_AGENT_AUTHORIZATION 的完整值例如 Bearer ...，不自动添加前缀。
async with HttpExecutor(targets) as executor:
    result = await executor.execute(
        "order-agent", {"message": "查询订单1024"}, timeout_seconds=60,
    )
    print(result.http_status, result.duration_ms)
    # result.output 为完整 JSON，result.error 为 None 或 code/message 对象。
```

config.example.json 的仅 url 配置仍然有效。headers 为固定字符串请求头，headers_env 从本地环境变量
解析完整请求头值，缺失立即报错；同名头不允许重复。Content-Type、Accept-Encoding 和 HTTP 连接/长度头由执行器管理。
目标配置在构造时快照；调用参数不能覆盖 URL 和头。配置及结果的 repr 隐藏 URL、请求头和业务输出。

## 判定规则

| 情况 | output | error.code |
| --- | --- | --- |
| 2xx + 合法 UTF-8 JSON | 完整 JSON（包括 null） | 无错误 |
| 非 2xx + 合法 JSON | 完整错误响应 JSON | http_error |
| 非 2xx + 非 JSON | null | http_error |
| 2xx + 非 JSON / 空响应（包括 204） | null | invalid_json |
| 请求或完整响应体读取超时 | null | timeout |
| 连接失败 | null | connection_error |
| 其他 HTTPX 请求错误 | null | request_error |
| 响应体超过限制 | null | response_too_large |
| 响应声明压缩编码 | null | unsupported_encoding |

响应头已收到时保留 http_status；无法获取响应头时为 null。耗时使用单调时钟，单位毫秒。
HTTP 成功不代表业务答案正确：200 返回 success=false 仍是一次成功的 HTTP 调用，由云端评测业务含义。

请求固定 POST、默认 TLS 校验，不使用系统代理，不自动重试，不跟随重定向。
单次调用不接收先提交后轮询、多轮、SSE 或 WebSocket Agent；特殊情况由用户适配器返回最终 JSON。
总等待使用 asyncio.timeout，覆盖 HTTPX 各阶段超时之外的整次请求与读取；默认响应上限 1 MiB，构造参数可调。
请求 Accept-Encoding: identity，压缩响应在读取解压前拒绝。不同调用和目标不自动继承 Set-Cookie，
需要 Cookie 时只能通过本地静态请求头明确配置。CLI 抑制 HTTPX INFO 请求日志，避免输出带敏感 query 的 URL。

非法调用参数和本地配置抛 ValueError，未打开/重复打开抛 RuntimeError；网络问题返回结构化结果。
asyncio.CancelledError 继续向上传播，响应资源会关闭；这不保证远端 Agent 已停止。
JobManager 负责记录取消结果；HTTP 超时由本层返回结构化错误，不能依赖本层实现恰好一次执行。

## 验证与后续

在 client 目录执行：

```powershell
.\.venv\Scripts\python.exe -X utf8 -W error -m unittest discover -s tests -p test_http_executor.py -v
```

本次新增 14 项测试，覆盖 JSON 原样收发及 null、认证头、非 2xx、无效响应、超时、取消、
不重试/不重定向、Cookie 隔离、压缩响应、响应大小限制和实际 loopback HTTP 请求。

2026-11-02：完整客户端 63 项测试在 `-W error` 下通过，依赖检查及可编辑包构建通过。
独立复查指出压缩响应和 Cookie 状态隔离问题，均已处理并补测试。
临时反转非 2xx 判定后，相关测试按预期失败；恢复源码后重新验证。

进程级 JobManager 已持有 HttpExecutor，在取得执行权后调用，结束后交给仓储保存结果。
app 默认创建两者并接入会话，详见 [Job 执行层](Job执行层核心链路.md)；HTTP 层本身不添加客户端 Job 排队能力。

参考：[HTTPX 异步调用](https://www.python-httpx.org/async/)、
[HTTPX 分阶段超时](https://www.python-httpx.org/advanced/timeouts/)。
