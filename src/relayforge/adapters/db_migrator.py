"""Apply the immutable M0 database contract from a repository checkout."""

import argparse
import hashlib
import os
from pathlib import Path

import psycopg

VERSION = "0001_initial_web_saas_v2"
CONTRACTS_DIR = Path(__file__).resolve().parents[3] / "RelayForge_Architecture/contracts"


class MigrationChecksumMismatchError(RuntimeError):
    """The migration ledger and expected baseline checksum disagree."""


def apply_baseline_schema(dsn: str, *, expected_checksum: str | None = None) -> str:
    source = (CONTRACTS_DIR / "database_schema.sql").read_text(encoding="utf-8")
    checksum = hashlib.sha256(source.encode("utf-8")).hexdigest()
    expected = checksum if expected_checksum is None else expected_checksum
    if checksum != expected:
        raise MigrationChecksumMismatchError("Expected checksum does not match the SQL contract")

    with psycopg.connect(dsn, autocommit=True, prepare_threshold=None) as conn, conn.transaction():
        if conn.info.server_version // 10000 != 16:
            raise RuntimeError("PostgreSQL 16 is required")

        # Serialize competing bootstraps for this database until the ledger is committed.
        conn.execute("SELECT pg_catalog.pg_advisory_xact_lock(1380338992)")
        ledger_exists = conn.execute(
            "SELECT pg_catalog.to_regclass('app.schema_migrations') IS NOT NULL"
        ).fetchone()[0]

        if not ledger_exists:
            conn.execute(source, prepare=False)

        conn.execute("SET LOCAL ROLE relayforge_migrator")
        conn.execute("SET LOCAL app.migration_mode = 'on'")
        if ledger_exists:
            row = conn.execute(
                "SELECT checksum_sha256 FROM app.schema_migrations WHERE version = %s", (VERSION,)
            ).fetchone()
            if row is None or row[0] != expected:
                raise MigrationChecksumMismatchError(
                    f"Missing or different ledger entry: {VERSION}"
                )
            return "ALREADY_APPLIED"

        conn.execute(
            "INSERT INTO app.schema_migrations (version, checksum_sha256) VALUES (%s, %s)",
            (VERSION, checksum),
        )
        return "APPLIED"


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dsn", default=os.environ.get("DATABASE_ADMIN_URL"))
    args = parser.parse_args()
    if not args.dsn:
        parser.error("set DATABASE_ADMIN_URL or pass --dsn")
    print(apply_baseline_schema(args.dsn))


if __name__ == "__main__":
    main()
