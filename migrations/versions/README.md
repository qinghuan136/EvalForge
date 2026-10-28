# 迁移版本

数据库基础设施阶段暂不创建业务表，因此当前没有迁移版本。
后续加入 ORM 模型后，在可连接的开发数据库上生成迁移并人工检查：

```powershell
.\.venv\Scripts\python.exe -m alembic revision --autogenerate -m "create dataset tables"
.\.venv\Scripts\python.exe -m alembic upgrade head
```

不要直接复制执行 Phoenix 的整套迁移。
