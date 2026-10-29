# 迁移版本

17 张核心表的 ORM 已注册到 Base.metadata，当前尚无正式迁移版本，也没有执行建表。
数据库实例就绪后，在可连接的开发数据库上生成初始迁移并人工检查：

```powershell
.\.venv\Scripts\python.exe -m alembic revision --autogenerate -m "create dataset tables"
.\.venv\Scripts\python.exe -m alembic upgrade head
```

不要直接复制执行 Phoenix 的整套迁移。
外部模块外键目前按约定暂缓，生成的迁移不应包含 users、spans、Prompt 或沙箱等占位表。
