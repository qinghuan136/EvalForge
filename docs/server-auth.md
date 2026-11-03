# 云端登录鉴权

已实现 `/api/v1/auth` 下的 csrf、register、login、me、logout、logout-all、password 七个接口；请求与响应见 [接口文档](api/登录鉴权与Runner管理.md)。
Runner 管理和云端 WSS 尚未实现，本次不改动客户端或 V0 实验流程。

## 启动

使用项目根目录 `.venv`。依赖为 FastAPI、Uvicorn、Pydantic、argon2-cffi；PostgreSQL 和 Redis 复用现有基础设施。

```powershell
.\.venv\Scripts\python.exe -m pip install -r requirements.lock
.\.venv\Scripts\python.exe -m pip install --no-deps -e .
```

配置 `.env` 中的 EVALFORGE_DATABASE_URL 和 EVALFORGE_REDIS_URL，并准备好数据库表；应用不会自动建表或升级结构。现有迁移环境的使用见 [数据库说明](database.md)。

本机 HTTP 调试时设置以下环境变量；生产必须使用 HTTPS 和 Secure Cookie：

```powershell
$env:EVALFORGE_ALLOWED_ORIGINS = 'http://127.0.0.1:8000'
$env:EVALFORGE_COOKIE_SECURE = 'false'
.\.venv\Scripts\python.exe -m uvicorn evalforge.server.app:create_app --factory --host 127.0.0.1 --port 8000 --no-proxy-headers
```

- 接口说明：`http://127.0.0.1:8000/docs`；OpenAPI：`/openapi.json`。
- HTTP 开发 Cookie 名为 evalforge_session；生产使用 __Host-evalforge_session，附带 Secure / HttpOnly / SameSite=Lax / Path=/。
- 调用写接口前先获取 csrf，并携带 Cookie、X-CSRF-Token 和配置允许的 Origin；Swagger 中可填写 CSRF Header 参数，普通脚本需显式提供来源头。
- 默认允许来源为 https://localhost:8000；多个来源用逗号分隔，不允许通配符或路径。不直接根据 Host / X-Forwarded-For 决定可信来源或客户端 IP，代理信任由部署时显式配置。
- 数据库与 Redis 不可用时认证请求返回 503；不自动退回内存会话。未配置实例时仍可执行下面的离线接口测试。

## 核心链路与文件

```text
routes/auth.py → dependencies.py（来源、CSRF、当前用户）
              → services/auth.py（账号业务与事务）
                  ├─ db.Database → PostgreSQL users
                  ├─ sessions.py → Redis 浏览器会话 / 限流
                  └─ security.py → Argon2id / 随机 Token / SHA-256
```

| 文件 | 职责 |
| --- | --- |
| evalforge/server/app.py | 应用工厂、路由、CORS、资源启动与释放 |
| evalforge/server/config.py | 来源、Cookie、会话有效期配置 |
| evalforge/server/schemas/auth.py | 请求校验和用户响应白名单；禁止额外字段 |
| evalforge/server/routes/auth.py | 七个 HTTP 接口、Cookie 设置与清除 |
| evalforge/server/dependencies.py | 资源获取、CSRF、当前用户校验 |
| evalforge/server/services/auth.py | 注册、登录、退出全部、改密码及事务 |
| evalforge/server/sessions.py | 匿名 / 登录会话、原子轮换、固定窗口限流 |
| evalforge/server/security.py | 原生 Argon2id 库；耗时操作进工作线程，最多两个并行任务 |
| evalforge/server/errors.py | 错误统一为 error.code / message，不回显请求密码或底层异常 |

应用创建的 Database / RedisStore 在 lifespan 中共享并关闭；注入的测试资源由调用方关闭。每次数据库操作仍使用独立 Session。
Argon2id 明确采用 OWASP 基线参数：19 MiB 内存、2 次迭代、并行度 1。库默认的 64 MiB 配置曾在本机回归时出现内存分配失败；当前参数限制内存开销，资源不足返回 503，不误报密码错误。

## 会话规则

- 先创建十分钟匿名会话获取 CSRF；登录时原子消费旧会话，生成新的登录 Token 和 CSRF，防止会话固定。并发消费只有一次成功。
- 浏览器 Token 使用 32 个密码学随机字节；Redis key 为 evalforge:{auth}:session: 加 Token 的 SHA-256 摘要，记录 user_id、auth_version、csrf_token、expires_at，不保存原始登录 Token。
- 登录会话默认 24 小时绝对有效期，TTL 和 expires_at 双重检查；访问 me / csrf 不自动续期。
- 每次认证都从 PostgreSQL 检查用户是否启用及 auth_version；logout-all / 改密码递增版本，旧 Redis 记录即使尚未清理也无法认证，最后由 TTL 回收。
- 登录重新检查账号和密码摘要，并与 logout-all / 改密码使用同一用户行锁；密码验证和生成放在锁外，避免长期占锁。
- 单独退出删除当前 Redis 会话；浏览器退出、会话过期或改密不改变 Runner 凭证。
- Redis 与 PostgreSQL 不使用分布式事务。若 Redis 已轮换而后续数据库提交失败，不发送新 Cookie；客户端重新获取 csrf 后重试登录，未返回的会话最终由 TTL 清理。
- API 响应禁止缓存；错误与日志不输出密码、Token 或底层异常文本。用户名重复为 409；未知账号、错误密码、禁用账号登录统一返回 401。

## 验证与边界

安装测试工具并执行：

```powershell
.\.venv\Scripts\python.exe -m pip install -e '.[test]'
.\.venv\Scripts\python.exe -X utf8 -W error -m unittest discover -s tests -v
```

接口测试使用 HTTPX ASGI、真实 Argon2、异步 SQLite 与 fakeredis 的 Lua 执行，验证正常闭环、CSRF / 来源拦截、会话重放、退出全部、改密、限流、依赖故障及提交失败恢复。
本阶段新增 21 项鉴权测试；最终云端全量 100 项中 98 项通过，2 项因没有真实 PostgreSQL / Redis 实例跳过。临时移除 auth_version 校验后，退出全部会话的重放测试按预期失败，已恢复源码。
PostgreSQL 行锁并发语义和真实 Redis 部署尚未联调；SQLite / fakeredis 不替代这两项验证。服务不使用测试内存数据库或模拟 Redis 作为生产后备。

参考：[FastAPI lifespan](https://fastapi.tiangolo.com/advanced/events/)、[异步测试](https://fastapi.tiangolo.com/advanced/async-tests/)、[Argon2 API](https://argon2-cffi.readthedocs.io/en/stable/api.html)、[OWASP 身份认证](https://cheatsheetseries.owasp.org/cheatsheets/Authentication_Cheat_Sheet.html)、[密码存储参数](https://cheatsheetseries.owasp.org/cheatsheets/Password_Storage_Cheat_Sheet.html)。
