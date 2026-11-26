import hashlib
import json
import os
import subprocess
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path

import psycopg
import pytest
from psycopg import errors, sql
from psycopg.conninfo import make_conninfo

from relayforge.adapters.db_migrator import (
    CONTRACTS_DIR,
    VERSION,
    MigrationChecksumMismatchError,
    apply_baseline_schema,
)

DEFAULT_DSN = "postgresql://postgres:postgres@127.0.0.1:55432/postgres"
ROLES = {
    "relayforge_migrator",
    "relayforge_operator",
    "relayforge_web",
    "relayforge_worker",
    "relayforge_dispatcher_owner",
    "relayforge_ledger_owner",
}
PASS_NOTICE = "ALL G0 v1.3.2 NON-VACUOUS BEHAVIORAL & SECURITY ASSERTIONS PASSED!"
MUTANTS = [
    (
        "M1_BUDGET_CAP",
        "IF (v_used + p_amount_micros) > v_pol.max_cost_micros THEN",
        "Expected RF001 against server-derived ROUTE cap=250!",
    ),
    (
        "M2_LEASE_EPOCH_FENCE",
        "IF v_supplied_epoch IS NULL OR v_supplied_epoch <> OLD.lease_epoch THEN",
        "Direct worker UPDATE with empty app.lease_epoch was NOT rejected!",
    ),
    (
        "M3_LATE_SETTLE_CAP",
        "IF (v_other_used + p_settled_micros) > v_alloc.max_cost_micros THEN",
        "Expected RF004 on late COMMITTED settle breaching cap after expiry!",
    ),
    (
        "M4_OC01_OPERATOR_PLAN_GUARD",
        "RAISE EXCEPTION 'TENANT_WEB_CANNOT_MUTATE_OPERATOR_PLAN_QUOTA' USING ERRCODE = 'RF007';",
        "relayforge_web was able to raise OPERATOR_PLAN max_cost_micros!",
    ),
]


@pytest.fixture(scope="module")
def admin_dsn():
    dsn = os.environ.get("TEST_DATABASE_ADMIN_URL", DEFAULT_DSN)
    if not dsn and not os.environ.get("CI"):
        pytest.skip("TEST_DATABASE_ADMIN_URL explicitly empty for unit-only mode")
    assert dsn, "CI requires a PostgreSQL test database"
    return dsn


@contextmanager
def disposable_database(admin_dsn, name):
    with psycopg.connect(admin_dsn, autocommit=True) as admin:
        assert admin.info.server_version // 10000 == 16, "PostgreSQL 16 required"
        drop = sql.SQL("DROP DATABASE IF EXISTS {}").format(sql.Identifier(name))
        admin.execute(drop)
        admin.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(name)))
        try:
            yield make_conninfo(admin_dsn, dbname=name)
        finally:
            admin.execute(drop)


@pytest.fixture(scope="module")
def catalog(admin_dsn):
    with disposable_database(admin_dsn, "rf_test_schema_m0") as dsn:
        assert apply_baseline_schema(dsn) == "APPLIED"
        assert apply_baseline_schema(dsn) == "ALREADY_APPLIED"
        with pytest.raises(MigrationChecksumMismatchError):
            apply_baseline_schema(dsn, expected_checksum="0" * 64)

        with psycopg.connect(dsn) as conn:
            tables = conn.execute("""
                SELECT relname, relpersistence, relrowsecurity AND relforcerowsecurity
                FROM pg_catalog.pg_class
                JOIN pg_catalog.pg_namespace n ON n.oid = relnamespace
                WHERE n.nspname = 'app' AND relkind = 'r'
            """).fetchall()
            assert len(tables) == 51
            assert sum(persistence == "p" for _, persistence, _ in tables) == 50
            assert {name for name, persistence, _ in tables if persistence == "u"} == {
                "rate_limit_counters"
            }
            force_rls_count = sum(enforced for _, _, enforced in tables)
            assert force_rls_count == 51
            policies = conn.execute("""
                SELECT DISTINCT tablename FROM pg_catalog.pg_policies
                WHERE schemaname = 'app' AND 'relayforge_migrator' = ANY(roles)
            """).fetchall()
            assert {row[0] for row in policies} == {name for name, _, _ in tables}
            bypass_count = conn.execute("""
                SELECT count(*) FROM pg_catalog.pg_roles
                WHERE NOT rolsuper AND rolname NOT LIKE 'pg_%' AND rolbypassrls
            """).fetchone()[0]
            assert bypass_count == 0
            safe_roles = conn.execute("""
                SELECT rolname FROM pg_catalog.pg_roles WHERE NOT rolsuper AND NOT rolbypassrls
            """).fetchall()
            assert ROLES <= {row[0] for row in safe_roles}
            result = {
                "pg_server_version": conn.execute("SHOW server_version").fetchone()[0],
                "table_count": len(tables),
                "force_rls_count": force_rls_count,
                "non_super_bypassrls_count": bypass_count,
            }

        with psycopg.connect(dsn) as conn:
            conn.execute("SET LOCAL ROLE relayforge_migrator")
            conn.execute("SET LOCAL app.migration_mode = 'on'")
            conn.execute(
                "UPDATE app.schema_migrations SET checksum_sha256 = %s WHERE version = %s",
                ("0" * 64, VERSION),
            )
        with pytest.raises(MigrationChecksumMismatchError):
            apply_baseline_schema(dsn)
        return result


def test_schema_catalog_and_idempotency(catalog):
    assert catalog["force_rls_count"] == catalog["table_count"] == 51


def execute_smoke(dsn, source):
    notices = []
    with psycopg.connect(dsn, autocommit=True, prepare_threshold=None) as conn:
        conn.add_notice_handler(lambda diag: notices.append(diag.message_primary))
        # The lab SQL manipulates cluster-wide roles. Never commit its DDL.
        with conn.transaction(force_rollback=True):
            conn.execute("SET LOCAL plpgsql.check_asserts = on")
            conn.execute(source, prepare=False)
    return notices


def test_smoke_contract_and_mutants(admin_dsn, catalog):
    smoke = (CONTRACTS_DIR / "g0_spike_smoke_test.sql").read_text(encoding="utf-8")
    killed = []
    with disposable_database(admin_dsn, "rf_test_smoke_m0") as dsn:
        assert PASS_NOTICE in execute_smoke(dsn, smoke)
        print(PASS_NOTICE)
        for mutant, guard, assertion in MUTANTS:
            assert smoke.count(guard) == 1, f"{mutant}: expected exactly one mutation site"
            replacement = "IF false THEN" if guard.startswith("IF ") else "NULL;"
            mutated = smoke.replace(guard, replacement, 1)
            try:
                execute_smoke(dsn, mutated)
            except errors.AssertFailure as exc:
                assert exc.diag.message_primary == assertion, f"{mutant}: unexpected assertion"
            else:
                pytest.fail(f"MUTANT_SURVIVED: {mutant}")
            killed.append(mutant)
            print(f"{mutant}: killed by {assertion}")

    root = CONTRACTS_DIR.parents[1]
    schema = (CONTRACTS_DIR / "database_schema.sql").read_text(encoding="utf-8")
    evidence = {
        "git_sha": subprocess.check_output(
            ["git", "rev-parse", "HEAD"], cwd=root, text=True
        ).strip(),
        "database_schema_lf_sha256": hashlib.sha256(schema.encode("utf-8")).hexdigest(),
        "smoke_sql_lf_sha256": hashlib.sha256(smoke.encode("utf-8")).hexdigest(),
        "timestamp_utc": datetime.now(UTC).isoformat(),
        **catalog,
        "mutants_killed": killed,
    }
    summary = json.dumps(evidence, sort_keys=True)
    print(summary)
    if evidence_out := os.environ.get("RF_EVIDENCE_OUT"):
        Path(evidence_out).write_text(summary + "\n", encoding="utf-8")
