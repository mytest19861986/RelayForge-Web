import base64
import os
from urllib.parse import urlsplit, urlunsplit

import django
import psycopg
import pytest
from django.db import connections
from psycopg import sql
from psycopg.conninfo import make_conninfo

from relayforge.adapters import db_migrator
from relayforge.adapters.db_migrator import apply_baseline_schema


@pytest.fixture(scope="module")
def db():
    dsn = os.environ.get(
        "TEST_DATABASE_ADMIN_URL", "postgresql://postgres:postgres@127.0.0.1:55432/postgres"
    )
    if not dsn and not os.environ.get("CI"):
        pytest.skip("TEST_DATABASE_ADMIN_URL explicitly empty for unit-only mode")
    name = "rf_test_auth_m1"
    drop = sql.SQL("DROP DATABASE IF EXISTS {} WITH (FORCE)").format(sql.Identifier(name))
    with psycopg.connect(dsn, autocommit=True) as admin:
        assert admin.info.server_version // 10000 == 16
        admin.execute(drop)
        admin.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(name)))
        try:
            test_dsn = make_conninfo(dsn, dbname=name)
            for migration_count in (1, 2, 3, 4):
                with pytest.MonkeyPatch.context() as env:
                    env.setattr(db_migrator, "MIGRATIONS", db_migrator.MIGRATIONS[:migration_count])
                    assert apply_baseline_schema(test_dsn) == "APPLIED"
            assert apply_baseline_schema(test_dsn) == "APPLIED"
            assert apply_baseline_schema(test_dsn) == "ALREADY_APPLIED"
            base = urlsplit(dsn)
            netloc = base.netloc.rsplit("@", 1)[-1]
            with pytest.MonkeyPatch.context() as env:
                env.setenv("DJANGO_SETTINGS_MODULE", "relayforge.web.settings")
                env.setenv("DJANGO_SECRET_KEY", "synthetic-django-key-for-tests")
                env.setenv("RF_MASTER_KEK_B64", base64.b64encode(b"k" * 32).decode())
                env.setenv("RF_OPERATOR_BEARER_TOKEN", "synthetic-operator-token")
                env.setenv("RF_ALLOWED_HOSTS", "testserver,127.0.0.1")
                for key, user in (
                    ("DATABASE_WEB_URL", "relayforge_web"),
                    ("DATABASE_OPERATOR_URL", "relayforge_operator"),
                ):
                    admin.execute(sql.SQL("ALTER ROLE {} PASSWORD 'm1_test_only'").format(
                        sql.Identifier(user)
                    ))
                    env.setenv(key, urlunsplit((
                        base.scheme, f"{user}:m1_test_only@{netloc}", f"/{name}", "", ""
                    )))
                django.setup()
                try:
                    yield test_dsn
                finally:
                    connections.close_all()
        finally:
            admin.execute(drop)
