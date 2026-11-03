# Redis 基础设施

云端根目录 `.venv` 已安装 `redis-py==8.1.0`；直接依赖名为 `redis`，异步接口为 `redis.asyncio`。客户端独立环境不变。

## 文件与职责

| 文件 | 职责 |
| --- | --- |
| `evalforge/redis/config.py` | RedisSettings、环境变量 / .env、地址与参数校验 |
| `evalforge/redis/store.py` | RedisStore、共享异步客户端与连接池、PING 和关闭 |
| `evalforge/redis/__main__.py` | 配置检查与显式连接检查 |
| `tests/test_redis.py` | 配置、凭据脱敏、生命周期、取消、超时与响应丢失测试 |

本层只提供连接能力。凭据缓存、登录会话、Runner 会话注册、业务键前缀与 TTL 策略由后续业务层确定。

## 配置与检查

环境变量优先于指定 `.env`，不修改进程环境，也不自动创建 `.env`。示例见根目录 `.env.example`。

| 环境变量 | 默认值 / 说明 |
| --- | --- |
| `EVALFORGE_REDIS_URL` | 必填；`redis://localhost:6379/0` 或 `rediss://...` |
| `EVALFORGE_REDIS_CONNECT_TIMEOUT` | 5 秒，必须为有限正数 |
| `EVALFORGE_REDIS_SOCKET_TIMEOUT` | 5 秒，读写等待超时，必须为有限正数 |
| `EVALFORGE_REDIS_MAX_CONNECTIONS` | 20，正整数；池满由驱动抛错，不无限等待 |
| `EVALFORGE_REDIS_HEALTH_CHECK_INTERVAL` | 30 秒；0 关闭健康检查，在使用连接时按间隔检查 |

地址格式为 `redis[s]://[user:password@]host[:port][/db]`。省略端口为 6379，省略 DB 为 0；密码中的特殊字符需 URL 编码。拒绝 query/fragment，避免地址参数覆盖 TLS、超时或重试配置。`rediss://` 使用证书和主机名校验。

在 EvalForge 根目录执行：

```powershell
# 不访问网络，不写 Redis
.\.venv\Scripts\python.exe -m evalforge.redis check-config --env-file .env.example

# 实例就绪并填写 .env 后：只执行 PING，不写业务数据
.\.venv\Scripts\python.exe -m evalforge.redis check
```

检查工具不打印连接串和凭据；连接错误只输出错误类别。无需 PostgreSQL 配置即可单独使用 Redis 层。

## 代码用法

```python
import asyncio
from evalforge.redis import RedisSettings, RedisStore

async def main():
    async with RedisStore(RedisSettings.from_env()) as store:
        await store.client.set("evalforge:demo", "hello", ex=60)
        value = await store.client.get("evalforge:demo")
        print(value)

asyncio.run(main())
```

- 导入、构造和进入上下文不联网；首个命令或 check_connection 才连接。
- 同一云端进程和事件循环内共享一个 Store / client；后续可由 FastAPI lifespan 创建并注入，业务操作不要逐次创建客户端。
- `decode_responses=True` 返回字符串；显式采用 RESP2，避免跟随驱动默认协议变化。
- 退出前先结束在途操作，再关闭 Store，释放其内部连接池；上下文外不可继续持有和使用旧 client。
- 原生 Redis 异常向调用者抛出，基础层不把失败当成缓存未命中，也不隐式回源数据库。
- 单条命令默认零自动重试：连接断开或超时不能证明写入未执行。重试、幂等、事务和会话接管由具体业务决定。
- 健康检查是使用连接时的存活检测，不是后台保活任务，也不是业务会话续期。

## 验证边界

测试使用真正的 redis-py 连接本机 RESP 模拟端，验证 AUTH、SELECT、PING、池关闭、取消、超时，以及写入已执行但响应丢失时不自动重放。它不替代真实 Redis / TLS 握手验证。

设置 `EVALFORGE_TEST_REDIS_URL` 后，完整测试会启用额外的真实实例只读 PING；未配置时跳过，不启动 Redis 服务。

2026-11-03：云端全部 71 项测试中 69 项通过，2 项因未配置 PostgreSQL / Redis 实例跳过；新增 Redis 测试 10 项。依赖检查、编译检查和示例配置检查通过。
临时将命令重试从 0 改为 1 后，响应丢失测试检测到重复写入并失败；恢复源码后完整测试通过。代码复查未发现阻塞问题。

官方参考：[异步客户端与资源关闭](https://redis.readthedocs.io/en/stable/examples/asyncio_examples.html)、[连接和重试配置](https://redis.io/docs/latest/develop/clients/redis-py/produsage/)。
