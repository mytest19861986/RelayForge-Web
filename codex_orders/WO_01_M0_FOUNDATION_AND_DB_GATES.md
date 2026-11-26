# Codex Work Order `WO-01` (Phase `M0`): Greenfield Foundation, Anti-Bloat Hygiene & PostgreSQL 16 Gate Suite

- **Project:** RelayForge Web SaaS (`v2.0.0`)
- **Target GitHub Repository:** `https://github.com/mytest19861986/RelayForge-Web.git`
- **Target Branch:** `main` (initial baseline commit if repository is empty, then feature branch `feat/m0-foundation-and-gates` merged or pushed cleanly — push both `main` with baseline docs + `feat/m0-foundation-and-gates` or push directly to `main` if initializing from empty, see Section 7)
- **Authoritative Architecture Contracts (DO NOT MODIFY):**
  - `RelayForge_Architecture/contracts/database_schema.sql` (51 PostgreSQL 16 tables)
  - `RelayForge_Architecture/contracts/g0_spike_smoke_test.sql` (`v1.3.2` behavioral & security smoke suite)
  - `RelayForge_Architecture/05_MASTER_ARCHITECTURE.md`
  - `RelayForge_Architecture/07_DATABASE_DESIGN.md`
  - `PROJECT_MASTER_ROADMAP.md`

---

## 1. Mission & Strict Scope Boundary

Implement **Phase `M0` (`WO-01` + `WO-02`)** of `PROJECT_MASTER_ROADMAP.md`:
1. Bootstrap the ultra-lean Greenfield Python 3.12 repository structure (`src/relayforge/{core,adapters,workers,web}`, `scripts/gates/`, `tests/{unit,integration}`, `deploy/`, `.github/workflows/`).
2. Enforce **Zero-Bloat Hygiene** (`.gitignore`, `.dockerignore`, minimal `pyproject.toml`, zero custom heavy Docker builds in `M0`).
3. Implement static architectural & AST security gates (`ruff`, `import-linter`, and custom AST gate for `OC-06` banning raw/aliased `transaction.on_commit` and banning framework imports in `relayforge.core`).
4. Implement the idempotent PostgreSQL 16 schema bootstrap/migrator (`relayforge.adapters.db_migrator`) that applies `RelayForge_Architecture/contracts/database_schema.sql` and verifies checksum integrity in `app.schema_migrations`.
5. Implement the non-vacuous integration & mutation gate suite verifying **51/51 `FORCE RLS`**, **0 non-superuser `BYPASSRLS` (`OC-10`)**, full execution of `g0_spike_smoke_test.sql` (`v1.3.2`), and **4/4 SQL mutant kills** (including `OC-01` `OPERATOR_PLAN` protection `RF007`).

> **OUT OF SCOPE (FORBIDDEN IN THIS WORK ORDER):**
> - Do **NOT** implement business endpoints, authentication views, messenger adapters, S3 uploaders, or UI files yet (those belong to `M1..M6`).
> - Do **NOT** modify any file inside `RelayForge_Architecture/` or `PROJECT_MASTER_ROADMAP.md`.
> - Do **NOT** add unused dependencies (`celery`, `redis`, `minio`, `sqlalchemy`, `telethon`, `pyrogram`, `boto3` are strictly forbidden in `M0`).

---

## 2. Hard Zero-Bloat & Host-Safety Rules (MANDATORY)

1. **Docker Hygiene:**
   - Do **NOT** build any custom Docker image in `M0`. Run Python tests locally (or in a lightweight local `.venv` that is git-ignored) against a single ephemeral `postgres:16-alpine` container defined in `deploy/docker-compose.verify.yml`.
   - Use isolated Compose project name `-p rf_m0_verify`.
   - **NEVER** touch, stop, or prune unrelated containers, volumes, or images on the host machine (e.g., never run `docker system prune` and never touch `hamicard_*` or any non-`rf_m0_verify` container).
   - Always tear down the verification container and its anonymous/named volume immediately after tests pass:
     ```bash
     docker compose -p rf_m0_verify -f deploy/docker-compose.verify.yml down -v --remove-orphans
     ```
2. **Git Hygiene:**
   - `.gitignore` and `.dockerignore` **MUST** exclude:
     `.venv/`, `venv/`, `__pycache__/`, `*.py[cod]`, `.pytest_cache/`, `.ruff_cache/`, `.mypy_cache/`, `.import_linter_cache/`, `.coverage`, `coverage.xml`, `htmlcov/`, `node_modules/`, `dist/`, `build/`, `*.egg-info/`, `*.zip`, `*.tar*`, `*.rfbak`, `.env`, `.env.*`, `g0-spike/`, `tmp/`, `staging/`, `artifacts/`.
   - Zero binary files, zero `.venv` files, and zero cache files may be staged in Git. Total tracked repository size (including `RelayForge_Architecture/`) must remain **< 2 MB**.

---

## 3. Exact Deliverables & Technical Requirements

### 3.1 Package Layout & Dependencies (`pyproject.toml`)
Create `pyproject.toml` (PEP 621, `requires-python = ">=3.12"`, `src/` layout):
- Runtime dependencies (keep strictly minimal for `M0`):
  - `psycopg[binary]>=3.2,<4.0`
  - `django>=5.2,<5.3`
  - `django-ninja>=1.3,<2.0`
  - `pydantic>=2.9,<3.0`
- Optional `[project.optional-dependencies] dev`:
  - `pytest>=8.3,<9.0`
  - `ruff>=0.8,<1.0`
  - `import-linter>=2.1,<3.0`

Create clean package modules:
- `src/relayforge/__init__.py` (`__version__ = "2.0.0a0"`)
- `src/relayforge/core/__init__.py`
- `src/relayforge/core/contracts.py` — pure Python (`typing.Protocol`, `dataclasses`, `uuid`, `datetime` only) defining foundational port protocols (`TenantContext`, `ClockPort`, `UnitOfWorkPort`) with **zero** framework/driver imports.
- `src/relayforge/adapters/__init__.py`
- `src/relayforge/adapters/db_migrator.py` (see Section 3.3)
- `src/relayforge/workers/__init__.py`
- `src/relayforge/web/__init__.py`
- `src/relayforge/web/db_tx.py` (see Section 3.2)

### 3.2 Tenant-Safe `on_tenant_commit` & Static/AST Gates (`SPK-06` + `OC-06`)
1. **`src/relayforge/web/db_tx.py`:**
   - Implement `on_tenant_commit(tenant_id: uuid.UUID, callback: Callable[[uuid.UUID], None], *, using: str | None = None) -> None`:
     - Validates `isinstance(tenant_id, uuid.UUID)` (fail-closed `ValueError` on empty/non-UUID).
     - Captures `bound_tid = uuid.UUID(str(tenant_id))` at registration time and registers via `django.db.transaction.on_commit(lambda: callback(bound_tid), using=using)` (annotate that exact line with `# noqa: TID251  # rf-gate: allow-raw-on-commit`).
2. **`ruff.toml`:**
   - Configure `target-version = "py312"`, `line-length = 100`, `src = ["src", "tests", "scripts"]`.
   - Enable lint rules `["E", "F", "I", "B", "UP", "TID251"]`.
   - Under `[lint.flake8-tidy-imports.banned-api]`, ban `"django.db.transaction.on_commit"` with message `"Raw transaction.on_commit loses SET LOCAL app.tenant_id; use relayforge.web.db_tx.on_tenant_commit (OC-06)."`.
3. **`.importlinter`:**
   - Set `root_packages = ["relayforge"]`.
   - Contract 1 (`independence` / `forbidden`): `relayforge.core` is forbidden from importing `relayforge.web`, `relayforge.adapters`, `relayforge.workers`, `django`, `ninja`, `psycopg`.
   - Contract 2 (`forbidden`): `relayforge.workers` is forbidden from importing `relayforge.web` view/middleware modules (`relayforge.web.db_tx` may be moved to a shared infra helper or explicitly scoped if needed, or keep `on_tenant_commit` in `relayforge.web.db_tx` for web layer only).
4. **`scripts/gates/check_ast_gates.py` (Closing `OC-06` Bypass Vector):**
   - Walk all `.py` files under `src/`.
   - **Rule A (`core/` Purity):** Inside `src/relayforge/core/`, reject any `import` / `from ... import` targeting `django`, `ninja`, `psycopg`, `httpx`, `boto3`, `relayforge.web`, `relayforge.adapters`, or `relayforge.workers`, **AND** reject dynamic import calls (`importlib.import_module(...)`, `__import__(...)`).
   - **Rule B (`on_commit` Alias/Attribute Ban — `OC-06`):** Across all of `src/` **except** `src/relayforge/web/db_tx.py`, reject:
     - Any attribute access `.on_commit` (catches `transaction.on_commit`, `tx.on_commit` alias!).
     - Any `from ... import on_commit` (including `as ...`).
     - Any `getattr(..., "on_commit")` call with string constant `"on_commit"`.
   - Exit `0` when clean, exit `1` with `file:line: rule: message` on violation.
5. **`tests/unit/test_static_gates.py`:**
   - Test `on_tenant_commit` rejects `None` / non-UUID strings and passes exact bound `UUID` on commit callback.
   - Test `check_ast_gates.py` passes on `src/` AND **explicitly catches** synthetic temp files containing:
     1) `from django.db import transaction as tx; tx.on_commit(fn)`
     2) `getattr(transaction, "on_commit")(fn)`
     3) `import importlib; importlib.import_module("django.conf")` inside a simulated `core/` path.

### 3.3 Idempotent PostgreSQL 16 Schema Migrator (`src/relayforge/adapters/db_migrator.py`)
Implement a clean, deterministic migration runner using `psycopg` (`prepare_threshold=None`):
- Accepts an admin/bootstrap PostgreSQL DSN (`DATABASE_ADMIN_URL`, used once to execute `RelayForge_Architecture/contracts/database_schema.sql` which idempotently creates the 6 non-superuser roles + `app` schema and switches `SET ROLE relayforge_migrator;` to create all 51 tables, triggers, functions, and `FORCE RLS` policies).
- **Important Idempotency Requirement:**
  - Compute `sha256_hex` of `RelayForge_Architecture/contracts/database_schema.sql` (normalized LF line endings so Windows CRLF vs Linux LF produces the exact same canonical hash, or store normalized LF SHA-256!).
  - Check whether `app.schema_migrations` already exists in the target database:
    - If `app.schema_migrations` does **not** exist: execute `database_schema.sql` inside a single atomic transaction (`conn.transaction()`), then insert row `version = '0001_initial_web_saas_v2'`, `description = 'Baseline 51-table PostgreSQL 16 FORCE RLS schema'`, `checksum_sha256 = <canonical_lf_sha256>`, `execution_ms = <elapsed_ms>` into `app.schema_migrations` (note: check exact column names of `app.schema_migrations` in `RelayForge_Architecture/contracts/database_schema.sql` before writing the `INSERT` — **do NOT guess column names!** Also satisfy any RLS policy on `app.schema_migrations` as `relayforge_migrator`).
    - If `app.schema_migrations` **already** exists and contains `'0001_initial_web_saas_v2'`:
      - If stored checksum matches `<canonical_lf_sha256>`, return `status="ALREADY_APPLIED"` (`exit=0`) without re-running `CREATE TABLE`.
      - If stored checksum **differs** from `<canonical_lf_sha256>`, raise `MigrationChecksumMismatchError` (`exit != 0`).
- **Hard Operational Rule:** Never emit `DROP ROLE` anywhere in runtime or migration code (`PgBouncer` OID cache safety).
- Provide CLI `python -m relayforge.adapters.db_migrator --dsn <DSN>`.

### 3.4 Integration, Catalog & 4-Mutant Kill Suite (`tests/integration/test_db_schema_and_smoke_gates.py`)
Using `TEST_DATABASE_ADMIN_URL` (defaulting to `postgresql://postgres:postgres@127.0.0.1:55432/postgres` matching `deploy/docker-compose.verify.yml`, skipping only if explicitly absent in unit-only mode, **never** skipping in CI or verification runs!):

1. **Test 1 — 51-Table Schema Bootstrap, Catalog Invariants & Idempotency:**
   - Create a fresh disposable database `rf_test_schema_m0` (drop if exists before & after test).
   - Run `apply_baseline_schema` on `rf_test_schema_m0`.
   - Query `pg_catalog` and assert **all** of the following non-vacuous invariants:
     - Total tables (`relkind = 'r'`) in schema `app` == **`51`** (`50` logged `relpersistence = 'p'` + `1` unlogged `relpersistence = 'u'` named `rate_limit_counters`).
     - Tables in `app` with `relrowsecurity = true AND relforcerowsecurity = true` == **`51`** (`0` tables missing `ENABLE` or `FORCE RLS`).
     - Distinct tables in `pg_catalog.pg_policies` (`schemaname = 'app'`) with a policy granting access to `relayforge_migrator` == **`51`**.
     - Non-superuser non-system roles with `rolbypassrls = true` (`SELECT count(*) FROM pg_catalog.pg_roles WHERE NOT rolsuper AND rolname NOT LIKE 'pg_%' AND rolbypassrls`) == **`0`** (`OC-10`).
     - All 6 expected application roles exist (`relayforge_migrator`, `relayforge_operator`, `relayforge_web`, `relayforge_worker`, `relayforge_dispatcher_owner`, `relayforge_ledger_owner`) with `rolsuper = false` and `rolbypassrls = false`.
   - Re-run `apply_baseline_schema` a second time -> assert `ALREADY_APPLIED`.
   - Pass a modified expected checksum -> assert `MigrationChecksumMismatchError` is raised.

2. **Test 2 — `g0_spike_smoke_test.sql` (`v1.3.2`) Execution & 4-Mutant Kill Verification (`OC-01`):**
   - Create a fresh disposable database `rf_test_smoke_m0`.
   - Execute `RelayForge_Architecture/contracts/g0_spike_smoke_test.sql` in full -> assert clean completion (`exit/status OK`) and capture the `NOTICE` confirming `ALL G0 v1.3.2 NON-VACUOUS BEHAVIORAL & SECURITY ASSERTIONS PASSED!`.
   - Execute **4 isolated mutation tests** (each on a fresh/reset schema copy of `g0_spike_smoke_test.sql`), asserting that each mutated SQL script **FAILS (`psycopg.Error` raised)** during the embedded assertion block — if any mutant unexpectedly passes, fail the test immediately with `MUTANT_SURVIVED`:
     - **`M1_BUDGET_CAP`:** Replace `IF (v_used + p_estimated_micros) > v_pol.max_cost_micros THEN` with `IF false THEN` inside `fn_budget_reserve`.
     - **`M2_LEASE_EPOCH_FENCE`:** Remove/bypass the `v_row.lease_epoch IS DISTINCT FROM p_lease_epoch` guard (`RF014`) inside `fn_worker_complete_delivery`.
     - **`M3_LATE_SETTLE_CAP`:** Remove/bypass the expired reservation late-settle cap check (`RF004`) inside `fn_budget_settle`.
     - **`M4_OC01_OPERATOR_PLAN_GUARD`:** Replace `RAISE EXCEPTION 'TENANT_WEB_CANNOT_MUTATE_OPERATOR_PLAN_QUOTA'` (or disable the `RF007` branch) inside `trg_fn_enforce_budget_policy_origin`.
   - Emit a structured JSON summary report (`OC-06` schema: `git_sha`, `database_schema_lf_sha256`, `smoke_sql_lf_sha256`, `pg_server_version`, `timestamp_utc`, `table_count`, `force_rls_count`, `non_super_bypassrls_count`, `mutants_killed`) to `os.environ.get("RF_EVIDENCE_OUT")` if set (and print summary to stdout).

### 3.5 Ephemeral Verification Compose & GitHub Actions CI
1. **`deploy/docker-compose.verify.yml`:**
   - Single service `postgres` using `postgres:16-alpine`, `tmpfs: /var/lib/postgresql/data` (zero disk bloat, blazingly fast!), bound strictly to `127.0.0.1:55432:5432`, healthcheck via `pg_isready -U postgres`.
2. **`.github/workflows/ci-m0.yml`:**
   - Triggers on `push` and `pull_request`.
   - Uses `ubuntu-24.04`, `postgres:16-alpine` service container on port `55432`, Python `3.12`.
   - Runs in strict order:
     1) `ruff check .`
     2) `lint-imports`
     3) `python scripts/gates/check_ast_gates.py`
     4) `pytest -v tests/unit tests/integration`

---

## 4. Verification Protocol Before Pushing (MANDATORY)

Run every single command below locally and ensure **zero errors and zero leftover containers**:

```bash
# 1. Static, boundary, and AST gates
ruff check .
lint-imports
python scripts/gates/check_ast_gates.py
pytest -v tests/unit

# 2. Spin up ephemeral tmpfs PostgreSQL 16 container (isolated project name)
docker compose -p rf_m0_verify -f deploy/docker-compose.verify.yml up -d --wait

# 3. Run full integration + 51-table catalog + 4-mutant kill suite
TEST_DATABASE_ADMIN_URL="postgresql://postgres:postgres@127.0.0.1:55432/postgres" pytest -v tests/integration

# 4. Immediate mandatory teardown (leave ZERO containers/volumes behind)
docker compose -p rf_m0_verify -f deploy/docker-compose.verify.yml down -v --remove-orphans
```

Also verify repository hygiene before committing:
- `git status --ignored` confirms `.venv/`, caches, and `g0-spike/` are ignored and **not** staged.
- No file inside `RelayForge_Architecture/` or `PROJECT_MASTER_ROADMAP.md` was altered or deleted.

---

## 5. Git & GitHub Push Instructions

The repository `https://github.com/mytest19861986/RelayForge-Web.git` already has `main` initialized with `RelayForge_Architecture/`, `PROJECT_MASTER_ROADMAP.md`, `codex_orders/`, and `.gitignore`.

1. Start from the latest `origin/main` and create/use branch `feat/m0-foundation-and-gates` (or commit on your assigned Codex task branch and open/push PR or branch to `origin`):
   ```bash
   git fetch origin
   git checkout -B feat/m0-foundation-and-gates origin/main
   ```
2. Stage all Phase `M0` deliverables (keeping `.gitignore` strictly enforced; do **not** modify `RelayForge_Architecture/` or `PROJECT_MASTER_ROADMAP.md`).
3. Commit with message:
   `feat(m0): bootstrap greenfield skeleton, AST/import gates, PG16 51-table migrator and 4-mutant CI suite`
4. Push cleanly to GitHub (`https://github.com/mytest19861986/RelayForge-Web.git` — either push `feat/m0-foundation-and-gates` / open PR into `main`, or merge fast-forward into `main` if authorized) and report:
   - Exact pushed branch & commit SHA
   - Output summary of `ruff`, `lint-imports`, `check_ast_gates.py`, and `pytest` (including `51/51 FORCE RLS` and `4/4 mutants killed`)
   - Confirmation that any temporary PostgreSQL verification container was torn down cleanly.
