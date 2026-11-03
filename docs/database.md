# PostgreSQL 基础设施

已具备连接能力和 21 张表的 ORM 定义（含停用保留的旧配置）；身份与凭证设计见 [鉴权数据结构](auth-schema.md)。当前不启动 PostgreSQL、不执行实际建表，不改变现有 V0 实验执行流程。

## 已提供的组件

| 层次 | 实现 |
| --- | --- |
| 项目环境 | 项目根目录 `.venv`，Python 3.12 |
| ORM / Engine / Session | SQLAlchemy 异步接口 |
| PostgreSQL 驱动 | asyncpg |
| 连接配置 | 环境变量或 `.env`，环境变量优先 |
| 事务 | `Database.transaction()`，成功提交、异常回滚、退出关闭会话 |
| 生命周期 | 共享 Engine；`async with Database(...)` 退出释放连接池 |
| 迁移 | Alembic 异步环境、模板、版本目录 |

参考 Phoenix 的 `db/engines.py`、`server/app.py` 中 `_db()` 及 Alembic 机制。
保留连接检查、连接池存活探测与回收，不引入读副本、云认证或批量入库组件。

## 安装与验证环境

所有命令从 EvalForge 根目录执行，PowerShell 示例：

```powershell
.\.venv\Scripts\python.exe -m pip check
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
```

另一台机器可先创建 Python 3.11+ 虚拟环境，再安装：

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.lock
.\.venv\Scripts\python.exe -m pip install --no-deps -e .
```

`pyproject.toml` 声明直接依赖范围，`requirements.lock` 记录本次安装的依赖版本。

## 没有数据库实例时

下面只验证示例配置，不尝试连接，也不会创建 `.env`：

```powershell
.\.venv\Scripts\python.exe -m evalforge.db check-config --env-file .env.example
.\.venv\Scripts\python.exe -m alembic heads
```

当前没有业务迁移，`alembic heads` 无版本输出是正常情况。

## 有实例后

1. 将 `.env.example` 复制为 `.env`，填入真实主机、端口、用户、密码和库名；`.env` 已被 Git 忽略。
2. 目标数据库应已由 PostgreSQL 管理员创建；连接工具不负责创建数据库。
3. 执行检查：

```powershell
.\.venv\Scripts\python.exe -m evalforge.db check-config
.\.venv\Scripts\python.exe -m evalforge.db check
```

标准地址格式：`postgresql+asyncpg://user:password@host:5432/database`。
也接受 `postgresql://`、`postgres://` 并自动选用 asyncpg。URL 密码里的 `@`、`%` 等字符需要 URL 编码；不要在日志中打印原始连接串。

## 代码用法

```python
import asyncio
from sqlalchemy import text
from evalforge.db import Database, DatabaseSettings

async def main():
    async with Database(DatabaseSettings.from_env()) as db:
        async with db.transaction() as session:
            value = await session.scalar(text("SELECT 1"))
            print(value)

asyncio.run(main())
```

Engine 和连接池应在同一个事件循环内使用并关闭，不要跨多个 asyncio.run 复用。
连接超时为 10 秒，SQL 命令超时为 30 秒；Task / Evaluator 执行应放在数据库事务外。
这里只准备异步数据库访问，现有同步 Task 不需要因此改成 async。

## ORM 与后续建表

18 张表的 ORM 已实现，统一从 `evalforge/db/models.py` 导入；现有 `evalforge/models.py` 继续保留 dataclass 契约。
模型分组、继承和暂缓的外部外键见 [ORM 说明](orm.md)。Alembic 已能加载完整 metadata，在有连接的开发数据库上生成迁移：

```powershell
.\.venv\Scripts\python.exe -m alembic revision --autogenerate -m "create core tables"
# 检查生成的脚本后执行
.\.venv\Scripts\python.exe -m alembic upgrade head
```

目前没有自动启动迁移，也没有把 Phoenix 全套业务表或迁移搬入。
实例未配置时，只能验证配置、驱动加载、会话管理及离线迁移；真实连接和数据库事务读写待实例就绪后验证。
测试设置 `EVALFORGE_TEST_DATABASE_URL` 后会启用只读的 PostgreSQL 连接集成测试。

## 数据库基础设施阶段验证（ORM 实现之前）

- Python 3.12.14；SQLAlchemy 2.1.6、asyncpg 0.32.0、Alembic 1.20.1、python-dotenv 1.2.4。
- `pip check` 通过，无依赖冲突。
- 33 项测试：32 项通过，1 项 PostgreSQL 集成测试因未配置实例而跳过。
- 示例配置检查通过；迁移模板可生成脚本并输出 PostgreSQL 离线升级 SQL。
- 未启动数据库，未执行真实连接、服务端提交回滚或业务表迁移。
