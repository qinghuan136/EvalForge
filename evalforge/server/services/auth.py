"""账号业务；PostgreSQL 保存身份，Redis 保存浏览器会话。"""

from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from evalforge.db import Database
from evalforge.db.models import User

from ..errors import APIError
from ..schemas.auth import RegisterRequest
from ..security import Passwords
from ..sessions import BrowserSession, SessionStore


def not_authenticated() -> APIError:
    return APIError(401, "not_authenticated", "请重新登录")


def invalid_credentials() -> APIError:
    return APIError(401, "invalid_credentials", "用户名或密码错误")


class AuthService:
    def __init__(self, database: Database, sessions: SessionStore, passwords: Passwords):
        self.database, self.sessions, self.passwords = database, sessions, passwords

    async def current_user(self, record: BrowserSession | None) -> User:
        if record is None or record.user_id is None:
            raise not_authenticated()
        async with self.database.transaction() as db:
            user = await db.get(User, record.user_id)
            if user is None or not user.enabled or user.auth_version != record.auth_version:
                raise not_authenticated()
            return user

    async def register(self, data: RegisterRequest) -> User:
        encoded = await self.passwords.hash(data.password.get_secret_value())
        try:
            async with self.database.transaction() as db:
                user = User(username=data.username, password_hash=encoded, display_name=data.display_name)
                db.add(user)
                await db.flush()
            return user
        except IntegrityError:
            # 只把已存在的用户名映射成 409，其余约束错误交给基础设施错误处理。
            async with self.database.transaction() as db:
                existing = await db.scalar(select(User.id).where(User.username == data.username))
            if existing is not None:
                raise APIError(409, "username_exists", "用户名已存在") from None
            raise

    async def login(self, username: str, password: str, old_token: str) -> tuple[User, str, BrowserSession]:
        async with self.database.transaction() as db:
            snapshot = await db.scalar(select(User).where(User.username == username))
        matched = await self.passwords.verify(snapshot.password_hash if snapshot else None, password)
        if not matched or snapshot is None or not snapshot.enabled:
            raise invalid_credentials()
        replacement = await self.passwords.hash(password) if self.passwords.needs_rehash(snapshot.password_hash) else None
        async with self.database.transaction() as db:
            user = await db.scalar(select(User).where(User.id == snapshot.id).with_for_update())
            if (user is None or not user.enabled or user.password_hash != snapshot.password_hash
                    or user.auth_version != snapshot.auth_version):
                raise invalid_credentials()
            # 与改密 / logout-all 使用同一用户行锁，避免并发签发过时版本的登录。
            token, record = await self.sessions.rotate(old_token, user.id, user.auth_version)
            user.last_login_at = datetime.now(timezone.utc)
            if replacement:
                user.password_hash = replacement
        return user, token, record

    async def logout_all(self, user: User, record: BrowserSession) -> None:
        async with self.database.transaction() as db:
            current = await db.scalar(select(User).where(User.id == user.id).with_for_update())
            if current is None or not current.enabled or current.auth_version != record.auth_version:
                raise not_authenticated()
            current.auth_version += 1
        # 旧 Redis 会话可保留至 TTL；每次认证核对数据库版本，已全部失效。

    async def change_password(self, user: User, record: BrowserSession, old: str, new: str) -> None:
        if not await self.passwords.verify(user.password_hash, old):
            raise APIError(401, "invalid_password", "当前密码错误")
        encoded = await self.passwords.hash(new)
        async with self.database.transaction() as db:
            current = await db.scalar(select(User).where(User.id == user.id).with_for_update())
            if (current is None or not current.enabled or current.auth_version != record.auth_version
                    or current.password_hash != user.password_hash):
                raise not_authenticated()
            current.password_hash = encoded
            current.auth_version += 1
