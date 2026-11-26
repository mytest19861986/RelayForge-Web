"""Apply the ordered PostgreSQL contracts from a repository checkout."""

import argparse
import hashlib
import os
from pathlib import Path

import psycopg

VERSION = "0001_initial_web_saas_v2"
CONTRACTS_DIR = Path(__file__).resolve().parents[3] / "RelayForge_Architecture/contracts"


class MigrationChecksumMismatchError(RuntimeError):
    """The migration ledger and expected baseline checksum disagree."""


M1_VERSION = "0002_m1_auth_and_shredding_functions"
M2_VERSION = "0003_m2_webhook_lookup_and_export_claim"
MIGRATIONS = (
    (VERSION, CONTRACTS_DIR / "database_schema.sql"),
    (M1_VERSION, CONTRACTS_DIR.parents[1] / "migrations" / f"{M1_VERSION}.sql"),
    (M2_VERSION, CONTRACTS_DIR.parents[1] / "migrations" / f"{M2_VERSION}.sql"),
)


def apply_baseline_schema(dsn: str, *, expected_checksum: str | None = None) -> str:
    applied = False
    with psycopg.connect(dsn, autocommit=True, prepare_threshold=None) as conn, conn.transaction():
        if conn.info.server_version // 10000 != 16:
            raise RuntimeError("PostgreSQL 16 is required")

        conn.execute("SELECT pg_catalog.pg_advisory_xact_lock(1380338992)")
        ledger_exists = conn.execute(
            "SELECT pg_catalog.to_regclass('app.schema_migrations') IS NOT NULL"
        ).fetchone()[0]
        conn.execute("SET LOCAL app.migration_mode = 'on'")
        for version, path in MIGRATIONS:
            source = path.read_text(encoding="utf-8")
            checksum = hashlib.sha256(source.encode("utf-8")).hexdigest()
            if version == VERSION and expected_checksum not in {None, checksum}:
                raise MigrationChecksumMismatchError("Expected checksum differs from baseline SQL")

            if ledger_exists:
                conn.execute("SET LOCAL ROLE relayforge_migrator")
                row = conn.execute(
                    "SELECT checksum_sha256 FROM app.schema_migrations WHERE version = %s",
                    (version,),
                ).fetchone()
                conn.execute("RESET ROLE")
                if row:
                    if row[0] != checksum:
                        raise MigrationChecksumMismatchError(f"Different ledger entry: {version}")
                    continue
                if version == VERSION:
                    raise MigrationChecksumMismatchError(f"Missing ledger entry: {version}")

            conn.execute(source, prepare=False)
            conn.execute("SET LOCAL ROLE relayforge_migrator")
            conn.execute(
                "INSERT INTO app.schema_migrations (version, checksum_sha256) VALUES (%s, %s)",
                (version, checksum),
            )
            conn.execute("RESET ROLE")
            applied = ledger_exists = True
    return "APPLIED" if applied else "ALREADY_APPLIED"


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dsn", default=os.environ.get("DATABASE_ADMIN_URL"))
    args = parser.parse_args()
    if not args.dsn:
        parser.error("set DATABASE_ADMIN_URL or pass --dsn")
    print(apply_baseline_schema(args.dsn))


if __name__ == "__main__":
    main()
