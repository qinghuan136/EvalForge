import asyncio
import io
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from alembic import command
from alembic.config import Config
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from evalforge.db import Base, Database, DatabaseSettings

ROOT = Path(__file__).resolve().parents[1]
TEST_URL = "postgresql+asyncpg://test-user:test-password@localhost:5432/evalforge_test"


class ConfigurationTests(unittest.TestCase):
    def settings(self, url=TEST_URL):
        with patch.dict(os.environ, {"EVALFORGE_DATABASE_URL": url}, clear=True):
            return DatabaseSettings.from_env(ROOT / "does-not-exist.env")

    def test_postgres_urls_are_normalized_to_asyncpg(self):
        for prefix in ("postgres://", "postgresql://", "postgresql+asyncpg://"):
            with self.subTest(prefix=prefix):
                settings = self.settings(prefix + "user:password@localhost/database")
                self.assertEqual(settings.url.drivername, "postgresql+asyncpg")

    def test_environment_overrides_env_file_without_mutating_environment(self):
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / ".env"
            source.write_text(
                "EVALFORGE_DATABASE_URL=postgresql://file-user:password@localhost/from_file\n"
                "EVALFORGE_DB_ECHO=true\n", encoding="utf-8",
            )
            with patch.dict(os.environ, {"EVALFORGE_DATABASE_URL": TEST_URL}, clear=True):
                settings = DatabaseSettings.from_env(source)
                self.assertEqual(settings.url.database, "evalforge_test")
                self.assertTrue(settings.echo)
                self.assertNotIn("EVALFORGE_DB_ECHO", os.environ)

    def test_password_is_not_exposed_by_repr_or_display(self):
        settings = self.settings()
        self.assertNotIn("test-password", settings.safe_url)
        self.assertNotIn("test-password", repr(settings))
        self.assertIn("***", settings.safe_url)

    def test_percent_encoded_password_survives_url_parsing(self):
        settings = self.settings("postgresql://user:p%40ss%25word@localhost/db")
        self.assertEqual(settings.url.password, "p@ss%word")

    def test_missing_url_has_actionable_error(self):
        with patch.dict(os.environ, {}, clear=True), self.assertRaisesRegex(ValueError, "EVALFORGE_DATABASE_URL"):
            DatabaseSettings.from_env(ROOT / "does-not-exist.env")

    def test_invalid_configuration_is_rejected(self):
        for url in ("invalid-secret-url", "sqlite:///local.db", "postgresql+psycopg://u:p@localhost/db",
                    "postgresql://u:p@localhost", "postgresql://u:p@localhost:70000/db"):
            with self.subTest(url=url), self.assertRaises(ValueError):
                self.settings(url)
        with patch.dict(os.environ, {"EVALFORGE_DATABASE_URL": TEST_URL, "EVALFORGE_DB_ECHO": "maybe"}, clear=True):
            with self.assertRaisesRegex(ValueError, "EVALFORGE_DB_ECHO"):
                DatabaseSettings.from_env(ROOT / "does-not-exist.env")

    def test_config_cli_checks_example_without_connecting(self):
        environment = {k: v for k, v in os.environ.items() if not k.startswith("EVALFORGE_")}
        completed = subprocess.run(
            [sys.executable, "-X", "utf8", "-m", "evalforge.db", "check-config", "--env-file", ".env.example"],
            cwd=ROOT, env=environment, capture_output=True, text=True, encoding="utf-8", check=True,
        )
        self.assertIn("No database connection was attempted", completed.stdout)
        self.assertNotIn("change-me", completed.stdout)


class SessionTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        with patch.dict(os.environ, {"EVALFORGE_DATABASE_URL": TEST_URL}, clear=True):
            self.db = Database(DatabaseSettings.from_env(ROOT / "does-not-exist.env"))

    async def asyncTearDown(self):
        await self.db.close()

    async def test_engine_uses_postgresql_asyncpg_without_opening_connections(self):
        self.assertEqual(self.db.engine.dialect.name, "postgresql")
        self.assertEqual(self.db.engine.dialect.driver, "asyncpg")
        self.assertEqual(self.db.engine.pool.checkedout(), 0)

    async def test_transaction_context_creates_a_fresh_session_per_operation(self):
        async with self.db.transaction() as first:
            self.assertIsInstance(first, AsyncSession)
            transaction = first.get_transaction()
            self.assertTrue(transaction.is_active)
        self.assertFalse(transaction.is_active)
        async with self.db.transaction() as second:
            self.assertIsNot(first, second)

    async def test_exception_leaves_no_active_session_transaction(self):
        with self.assertRaisesRegex(RuntimeError, "example failure"):
            async with self.db.transaction() as session:
                transaction = session.get_transaction()
                raise RuntimeError("example failure")
        self.assertFalse(transaction.is_active)


class MigrationTests(unittest.TestCase):
    def test_template_and_offline_upgrade_without_database(self):
        # 在临时副本生成空迁移，验证 Alembic 模板和 PostgreSQL 离线 SQL，不触碰真实数据库。
        with tempfile.TemporaryDirectory() as tmp:
            location = Path(tmp) / "migrations"
            shutil.copytree(ROOT / "migrations", location)
            output = io.StringIO()
            config = Config(stdout=output, output_buffer=output)
            config.set_main_option("script_location", str(location))
            command.revision(config, message="infrastructure test", rev_id="test_v1")
            with patch.dict(os.environ, {"EVALFORGE_DATABASE_URL": TEST_URL}):
                command.upgrade(config, "head", sql=True)
            self.assertIn("CREATE TABLE alembic_version", output.getvalue())
            self.assertIn("test_v1", output.getvalue())


@unittest.skipUnless(os.getenv("EVALFORGE_TEST_DATABASE_URL"), "No PostgreSQL test instance configured")
class PostgreSQLIntegrationTests(unittest.IsolatedAsyncioTestCase):
    async def test_real_connection_and_session_query(self):
        with patch.dict(os.environ, {"EVALFORGE_DATABASE_URL": os.environ["EVALFORGE_TEST_DATABASE_URL"]}):
            settings = DatabaseSettings.from_env(ROOT / "does-not-exist.env")
        async with Database(settings) as db:
            self.assertTrue(await db.check_connection())
            async with db.transaction() as session:
                self.assertEqual(await session.scalar(text("SELECT 1")), 1)


if __name__ == "__main__":
    unittest.main()
