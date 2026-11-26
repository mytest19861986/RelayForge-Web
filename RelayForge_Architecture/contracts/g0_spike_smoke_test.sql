-- ============================================================================
-- RelayForge G0 Spike — Minimal Schema v1.3.2 + Non-Vacuous Behavioral Suite
-- WARNING: DISPOSABLE LAB SCRIPT ONLY (Drops 'app' schema & lab roles; uses
--          static dummy passwords). NEVER run against non-ephemeral databases.
-- Run:
--   psql -v ON_ERROR_STOP=1 -U postgres -d postgres -f g0_spike_smoke_test.sql
-- ============================================================================

DROP SCHEMA IF EXISTS app CASCADE;
DROP ROLE IF EXISTS relayforge_migrator;
DROP ROLE IF EXISTS relayforge_operator;
DROP ROLE IF EXISTS relayforge_web;
DROP ROLE IF EXISTS relayforge_worker;
DROP ROLE IF EXISTS relayforge_dispatcher_owner;
DROP ROLE IF EXISTS relayforge_ledger_owner;

-- ============================================================================
-- 1. Roles & Dedicated Schema 'app' (Zero BYPASSRLS across all non-superuser app roles)
-- ============================================================================
CREATE ROLE relayforge_migrator LOGIN PASSWORD 'spike_mig_only' NOSUPERUSER NOCREATEDB NOCREATEROLE NOBYPASSRLS;
CREATE ROLE relayforge_operator LOGIN PASSWORD 'spike_ops_only' NOSUPERUSER NOCREATEDB NOCREATEROLE NOBYPASSRLS;
CREATE ROLE relayforge_web LOGIN PASSWORD 'spike_web_only' NOSUPERUSER NOCREATEDB NOCREATEROLE NOBYPASSRLS;
CREATE ROLE relayforge_worker LOGIN PASSWORD 'spike_wrk_only' NOSUPERUSER NOCREATEDB NOCREATEROLE NOBYPASSRLS;
CREATE ROLE relayforge_dispatcher_owner NOLOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOBYPASSRLS;
CREATE ROLE relayforge_ledger_owner NOLOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOBYPASSRLS;

CREATE SCHEMA app AUTHORIZATION relayforge_migrator;
REVOKE ALL ON SCHEMA app FROM PUBLIC;
GRANT USAGE ON SCHEMA app TO relayforge_operator, relayforge_web, relayforge_worker, relayforge_dispatcher_owner, relayforge_ledger_owner;

-- ============================================================================
-- 2. Tables & Indexes Owned by relayforge_migrator (9 FORCE-RLS Tables)
-- ============================================================================
SET ROLE relayforge_migrator;

CREATE TABLE app.tenants (
    id UUID PRIMARY KEY,
    slug TEXT NOT NULL UNIQUE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT pg_catalog.clock_timestamp()
);

CREATE TABLE app.workspaces (
    id UUID PRIMARY KEY,
    tenant_id UUID NOT NULL REFERENCES app.tenants(id) ON DELETE RESTRICT,
    slug TEXT NOT NULL,
    UNIQUE (tenant_id, id),
    UNIQUE (tenant_id, slug)
);

CREATE TABLE app.routes (
    id UUID PRIMARY KEY,
    tenant_id UUID NOT NULL,
    workspace_id UUID NOT NULL,
    slug TEXT NOT NULL,
    UNIQUE (tenant_id, id),
    UNIQUE (tenant_id, workspace_id, id),
    UNIQUE (tenant_id, workspace_id, slug),
    FOREIGN KEY (tenant_id, workspace_id) REFERENCES app.workspaces(tenant_id, id) ON DELETE RESTRICT
);

CREATE TABLE app.tenant_dispatch_fairness (
    tenant_id UUID PRIMARY KEY REFERENCES app.tenants(id) ON DELETE CASCADE,
    last_served_at TIMESTAMPTZ NOT NULL
);

CREATE TABLE app.delivery_tasks (
    id UUID PRIMARY KEY,
    tenant_id UUID NOT NULL,
    workspace_id UUID NOT NULL,
    route_id UUID,
    idempotency_key TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN (
        'PENDING', 'SCHEDULED', 'READY', 'RESERVED', 'DISPATCHING_IN_FLIGHT',
        'SENT', 'RETRY_WAIT', 'FAILED', 'UNCERTAIN', 'SKIPPED', 'CANCELLED'
    )),
    scheduled_for TIMESTAMPTZ NOT NULL,
    next_retry_at TIMESTAMPTZ,
    runnable_at TIMESTAMPTZ GENERATED ALWAYS AS (COALESCE(next_retry_at, scheduled_for)) STORED,
    lease_owner TEXT,
    lease_expires_at TIMESTAMPTZ,
    lease_epoch BIGINT NOT NULL DEFAULT 0 CHECK (lease_epoch >= 0),
    attempts INT NOT NULL DEFAULT 0 CHECK (attempts >= 0),
    max_attempts INT NOT NULL DEFAULT 4 CHECK (max_attempts BETWEEN 1 AND 100),
    payload_json JSONB NOT NULL DEFAULT '{}'::jsonb,
    remote_message_id TEXT,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT pg_catalog.clock_timestamp(),
    CHECK (status <> 'RETRY_WAIT' OR next_retry_at IS NOT NULL),
    UNIQUE (tenant_id, id),
    UNIQUE (tenant_id, workspace_id, idempotency_key),
    FOREIGN KEY (tenant_id, workspace_id) REFERENCES app.workspaces(tenant_id, id) ON DELETE RESTRICT,
    FOREIGN KEY (tenant_id, workspace_id, route_id) REFERENCES app.routes(tenant_id, workspace_id, id) ON DELETE RESTRICT
);

CREATE TABLE app.reconciliation_records (
    id UUID PRIMARY KEY,
    tenant_id UUID NOT NULL,
    delivery_task_id UUID NOT NULL,
    task_lease_epoch BIGINT NOT NULL,
    resolution_action TEXT NOT NULL CHECK (resolution_action IN (
        'CONFIRM_SENT', 'CONFIRM_NOT_SENT_REQUEUE', 'DISCARD_DO_NOT_SEND'
    )),
    acknowledge_duplicate_risk BOOLEAN NOT NULL DEFAULT false CHECK (
        resolution_action <> 'CONFIRM_NOT_SENT_REQUEUE' OR acknowledge_duplicate_risk = true
    ),
    decided_by TEXT NOT NULL,
    reason TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT pg_catalog.clock_timestamp(),
    UNIQUE (delivery_task_id, task_lease_epoch),
    FOREIGN KEY (tenant_id, delivery_task_id) REFERENCES app.delivery_tasks(tenant_id, id) ON DELETE RESTRICT
);

-- Server-Derived Scoped Budget Policies (v1.3.2: Separates immutable OPERATOR_PLAN ceiling from TENANT_BUDGET sub-caps!)
CREATE TABLE app.budget_policies (
    id UUID PRIMARY KEY,
    tenant_id UUID NOT NULL REFERENCES app.tenants(id) ON DELETE RESTRICT,
    policy_origin TEXT NOT NULL CHECK (policy_origin IN ('OPERATOR_PLAN', 'TENANT_BUDGET')),
    scope_type TEXT NOT NULL CHECK (scope_type IN ('TENANT', 'WORKSPACE', 'ROUTE')),
    scope_ref_id UUID NOT NULL,
    period_kind TEXT NOT NULL CHECK (period_kind IN ('DAILY', 'MONTHLY', 'TOTAL')),
    max_cost_micros BIGINT NOT NULL CHECK (max_cost_micros >= 0),
    is_active BOOLEAN NOT NULL DEFAULT true,
    CHECK (scope_type <> 'TENANT' OR scope_ref_id = tenant_id),
    CHECK (policy_origin <> 'OPERATOR_PLAN' OR scope_type = 'TENANT'),
    UNIQUE (tenant_id, id),
    UNIQUE (tenant_id, policy_origin, scope_type, scope_ref_id, period_kind)
);

CREATE TABLE app.budget_reservations (
    id UUID PRIMARY KEY,
    tenant_id UUID NOT NULL,
    workspace_id UUID NOT NULL,
    route_id UUID,
    idempotency_key TEXT NOT NULL,
    state TEXT NOT NULL CHECK (state IN ('RESERVED', 'COMMITTED', 'RELEASED')),
    reserved_micros BIGINT NOT NULL CHECK (reserved_micros >= 0),
    settled_micros BIGINT CHECK (settled_micros IS NULL OR settled_micros >= 0),
    expires_at TIMESTAMPTZ NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT pg_catalog.clock_timestamp(),
    UNIQUE (tenant_id, id),
    UNIQUE (tenant_id, idempotency_key),
    FOREIGN KEY (tenant_id, workspace_id) REFERENCES app.workspaces(tenant_id, id) ON DELETE RESTRICT,
    FOREIGN KEY (tenant_id, workspace_id, route_id) REFERENCES app.routes(tenant_id, workspace_id, id) ON DELETE RESTRICT
);

CREATE TABLE app.budget_reservation_allocations (
    reservation_id UUID NOT NULL,
    policy_id UUID NOT NULL,
    tenant_id UUID NOT NULL,
    period_bucket TEXT NOT NULL,
    PRIMARY KEY (reservation_id, policy_id),
    FOREIGN KEY (tenant_id, reservation_id) REFERENCES app.budget_reservations(tenant_id, id) ON DELETE RESTRICT,
    FOREIGN KEY (tenant_id, policy_id) REFERENCES app.budget_policies(tenant_id, id) ON DELETE RESTRICT
);

-- Index supporting O(batch_size) per-tenant index-order SKIP LOCKED scan without full backlog sort (Fixes M10)
CREATE INDEX idx_delivery_fair_claim
    ON app.delivery_tasks (tenant_id, runnable_at ASC)
    WHERE status IN ('READY', 'SCHEDULED', 'RETRY_WAIT');

CREATE INDEX idx_delivery_reap_expired
    ON app.delivery_tasks (lease_expires_at ASC)
    WHERE status IN ('RESERVED', 'DISPATCHING_IN_FLIGHT');

CREATE INDEX idx_budget_policies_active_scope
    ON app.budget_policies (tenant_id, scope_type, scope_ref_id)
    WHERE is_active = true;

CREATE INDEX idx_budget_alloc_period
    ON app.budget_reservation_allocations (policy_id, period_bucket);

-- ============================================================================
-- 3. Strict Privilege Separation (Fixes S3 & S4: No DELETE; Encapsulated Ledger)
-- ============================================================================
REVOKE ALL ON ALL TABLES IN SCHEMA app FROM PUBLIC;

GRANT SELECT ON app.tenants TO relayforge_web, relayforge_worker;
GRANT SELECT (id) ON app.tenants TO relayforge_dispatcher_owner;

GRANT SELECT, INSERT, UPDATE, DELETE ON app.workspaces, app.routes TO relayforge_web;
GRANT SELECT ON app.workspaces, app.routes TO relayforge_worker, relayforge_ledger_owner;

-- Neither web nor worker has DELETE on delivery_tasks! Trigger guards INSERT & UPDATE.
GRANT SELECT, INSERT, UPDATE ON app.delivery_tasks TO relayforge_web, relayforge_worker;
GRANT SELECT, INSERT ON app.reconciliation_records TO relayforge_web;

-- Dispatcher owner gets minimal column-level access ONLY:
GRANT SELECT (id, tenant_id, status, scheduled_for, next_retry_at, runnable_at, lease_expires_at, lease_epoch, attempts, max_attempts),
      UPDATE (status, next_retry_at, lease_owner, lease_expires_at, lease_epoch, attempts, updated_at)
      ON app.delivery_tasks TO relayforge_dispatcher_owner;
GRANT SELECT, INSERT, UPDATE ON app.tenant_dispatch_fairness TO relayforge_dispatcher_owner;

-- Budget encapsulation (Fixes S4 & v1.3.2 OC-01):
--   1. OPERATOR_PLAN ceilings are writable ONLY by relayforge_operator / relayforge_migrator; relayforge_web can only mutate TENANT_BUDGET!
--   2. Worker CANNOT mutate budget_policies (read-only).
--   3. Neither web nor worker can directly INSERT/UPDATE/DELETE budget_reservations or allocations!
GRANT SELECT, INSERT, UPDATE, DELETE ON app.budget_policies TO relayforge_operator, relayforge_web;
GRANT SELECT ON app.budget_policies TO relayforge_worker;
GRANT SELECT, UPDATE ON app.budget_policies TO relayforge_ledger_owner;

GRANT SELECT ON app.budget_reservations, app.budget_reservation_allocations TO relayforge_web, relayforge_worker;
GRANT SELECT, INSERT, UPDATE ON app.budget_reservations TO relayforge_ledger_owner;
GRANT SELECT, INSERT ON app.budget_reservation_allocations TO relayforge_ledger_owner;

-- ============================================================================
-- 4. Enable & Force RLS + Complete Policies (Fixes S1: UNCERTAIN/FAILED in SELECT)
-- ============================================================================
ALTER TABLE app.tenants ENABLE ROW LEVEL SECURITY;
ALTER TABLE app.tenants FORCE ROW LEVEL SECURITY;
ALTER TABLE app.workspaces ENABLE ROW LEVEL SECURITY;
ALTER TABLE app.workspaces FORCE ROW LEVEL SECURITY;
ALTER TABLE app.routes ENABLE ROW LEVEL SECURITY;
ALTER TABLE app.routes FORCE ROW LEVEL SECURITY;
ALTER TABLE app.tenant_dispatch_fairness ENABLE ROW LEVEL SECURITY;
ALTER TABLE app.tenant_dispatch_fairness FORCE ROW LEVEL SECURITY;
ALTER TABLE app.delivery_tasks ENABLE ROW LEVEL SECURITY;
ALTER TABLE app.delivery_tasks FORCE ROW LEVEL SECURITY;
ALTER TABLE app.reconciliation_records ENABLE ROW LEVEL SECURITY;
ALTER TABLE app.reconciliation_records FORCE ROW LEVEL SECURITY;
ALTER TABLE app.budget_policies ENABLE ROW LEVEL SECURITY;
ALTER TABLE app.budget_policies FORCE ROW LEVEL SECURITY;
ALTER TABLE app.budget_reservations ENABLE ROW LEVEL SECURITY;
ALTER TABLE app.budget_reservations FORCE ROW LEVEL SECURITY;
ALTER TABLE app.budget_reservation_allocations ENABLE ROW LEVEL SECURITY;
ALTER TABLE app.budget_reservation_allocations FORCE ROW LEVEL SECURITY;

-- Tenant Isolation Policies (Applies to web, worker, AND ledger_owner!)
CREATE POLICY tenants_tenant_rls ON app.tenants
    FOR SELECT TO relayforge_web, relayforge_worker
    USING (id = NULLIF(pg_catalog.current_setting('app.tenant_id', true), '')::uuid);

CREATE POLICY workspaces_tenant_rls ON app.workspaces
    FOR ALL TO relayforge_web, relayforge_worker, relayforge_ledger_owner
    USING (tenant_id = NULLIF(pg_catalog.current_setting('app.tenant_id', true), '')::uuid)
    WITH CHECK (tenant_id = NULLIF(pg_catalog.current_setting('app.tenant_id', true), '')::uuid);

CREATE POLICY routes_tenant_rls ON app.routes
    FOR ALL TO relayforge_web, relayforge_worker, relayforge_ledger_owner
    USING (tenant_id = NULLIF(pg_catalog.current_setting('app.tenant_id', true), '')::uuid)
    WITH CHECK (tenant_id = NULLIF(pg_catalog.current_setting('app.tenant_id', true), '')::uuid);

CREATE POLICY delivery_tasks_tenant_rls ON app.delivery_tasks
    FOR ALL TO relayforge_web, relayforge_worker
    USING (tenant_id = NULLIF(pg_catalog.current_setting('app.tenant_id', true), '')::uuid)
    WITH CHECK (tenant_id = NULLIF(pg_catalog.current_setting('app.tenant_id', true), '')::uuid);

CREATE POLICY reconciliation_tenant_rls ON app.reconciliation_records
    FOR ALL TO relayforge_web
    USING (tenant_id = NULLIF(pg_catalog.current_setting('app.tenant_id', true), '')::uuid)
    WITH CHECK (tenant_id = NULLIF(pg_catalog.current_setting('app.tenant_id', true), '')::uuid);

CREATE POLICY budget_policies_tenant_rls ON app.budget_policies
    FOR ALL TO relayforge_operator, relayforge_web, relayforge_worker, relayforge_ledger_owner
    USING (tenant_id = NULLIF(pg_catalog.current_setting('app.tenant_id', true), '')::uuid)
    WITH CHECK (tenant_id = NULLIF(pg_catalog.current_setting('app.tenant_id', true), '')::uuid);

CREATE POLICY budget_reservations_tenant_rls ON app.budget_reservations
    FOR ALL TO relayforge_web, relayforge_worker, relayforge_ledger_owner
    USING (tenant_id = NULLIF(pg_catalog.current_setting('app.tenant_id', true), '')::uuid)
    WITH CHECK (tenant_id = NULLIF(pg_catalog.current_setting('app.tenant_id', true), '')::uuid);

CREATE POLICY budget_allocations_tenant_rls ON app.budget_reservation_allocations
    FOR ALL TO relayforge_web, relayforge_worker, relayforge_ledger_owner
    USING (tenant_id = NULLIF(pg_catalog.current_setting('app.tenant_id', true), '')::uuid)
    WITH CHECK (tenant_id = NULLIF(pg_catalog.current_setting('app.tenant_id', true), '')::uuid);

-- Migrator Policies on ALL 9 FORCE-RLS Tables
CREATE POLICY tenants_migrator_rls ON app.tenants FOR ALL TO relayforge_migrator
    USING (pg_catalog.current_setting('app.migration_mode', true) = 'on')
    WITH CHECK (pg_catalog.current_setting('app.migration_mode', true) = 'on');
CREATE POLICY workspaces_migrator_rls ON app.workspaces FOR ALL TO relayforge_migrator
    USING (pg_catalog.current_setting('app.migration_mode', true) = 'on')
    WITH CHECK (pg_catalog.current_setting('app.migration_mode', true) = 'on');
CREATE POLICY routes_migrator_rls ON app.routes FOR ALL TO relayforge_migrator
    USING (pg_catalog.current_setting('app.migration_mode', true) = 'on')
    WITH CHECK (pg_catalog.current_setting('app.migration_mode', true) = 'on');
CREATE POLICY fairness_migrator_rls ON app.tenant_dispatch_fairness FOR ALL TO relayforge_migrator
    USING (pg_catalog.current_setting('app.migration_mode', true) = 'on')
    WITH CHECK (pg_catalog.current_setting('app.migration_mode', true) = 'on');
CREATE POLICY delivery_tasks_migrator_rls ON app.delivery_tasks FOR ALL TO relayforge_migrator
    USING (pg_catalog.current_setting('app.migration_mode', true) = 'on')
    WITH CHECK (pg_catalog.current_setting('app.migration_mode', true) = 'on');
CREATE POLICY reconciliation_migrator_rls ON app.reconciliation_records FOR ALL TO relayforge_migrator
    USING (pg_catalog.current_setting('app.migration_mode', true) = 'on')
    WITH CHECK (pg_catalog.current_setting('app.migration_mode', true) = 'on');
CREATE POLICY budget_policies_migrator_rls ON app.budget_policies FOR ALL TO relayforge_migrator
    USING (pg_catalog.current_setting('app.migration_mode', true) = 'on')
    WITH CHECK (pg_catalog.current_setting('app.migration_mode', true) = 'on');
CREATE POLICY budget_reservations_migrator_rls ON app.budget_reservations FOR ALL TO relayforge_migrator
    USING (pg_catalog.current_setting('app.migration_mode', true) = 'on')
    WITH CHECK (pg_catalog.current_setting('app.migration_mode', true) = 'on');
CREATE POLICY budget_allocations_migrator_rls ON app.budget_reservation_allocations FOR ALL TO relayforge_migrator
    USING (pg_catalog.current_setting('app.migration_mode', true) = 'on')
    WITH CHECK (pg_catalog.current_setting('app.migration_mode', true) = 'on');

-- Dispatcher Policies (Fixes S1: UNCERTAIN & FAILED included in SELECT USING for UPDATE...RETURNING!)
CREATE POLICY dispatcher_tenants_select_rls ON app.tenants
    FOR SELECT TO relayforge_dispatcher_owner USING (true);

CREATE POLICY dispatcher_fairness_rls ON app.tenant_dispatch_fairness
    FOR ALL TO relayforge_dispatcher_owner USING (true) WITH CHECK (true);

CREATE POLICY dispatcher_select_rls ON app.delivery_tasks
    FOR SELECT TO relayforge_dispatcher_owner
    USING (status IN ('SCHEDULED', 'READY', 'RETRY_WAIT', 'RESERVED', 'DISPATCHING_IN_FLIGHT', 'UNCERTAIN', 'FAILED'));

CREATE POLICY dispatcher_update_rls ON app.delivery_tasks
    FOR UPDATE TO relayforge_dispatcher_owner
    USING (status IN ('SCHEDULED', 'READY', 'RETRY_WAIT', 'RESERVED', 'DISPATCHING_IN_FLIGHT'))
    WITH CHECK (status IN ('READY', 'RESERVED', 'FAILED', 'UNCERTAIN'));

-- ============================================================================
-- 5. Complete DML Guard Trigger: BEFORE INSERT OR UPDATE OR DELETE (Fixes S3, M5, M6, M7, M8)
-- ============================================================================
CREATE OR REPLACE FUNCTION app.trg_fn_enforce_delivery_state_machine()
RETURNS TRIGGER
LANGUAGE plpgsql
SET search_path = pg_catalog, pg_temp
AS $$
DECLARE
    v_caller TEXT := CURRENT_USER;
    v_is_super BOOLEAN;
    v_supplied_epoch BIGINT;
    v_now TIMESTAMPTZ := pg_catalog.clock_timestamp();
BEGIN
    -- 1. Controlled Migration Bypass
    IF v_caller = 'relayforge_migrator' AND pg_catalog.current_setting('app.migration_mode', true) = 'on' THEN
        IF TG_OP = 'DELETE' THEN RETURN OLD; ELSE RETURN NEW; END IF;
    END IF;

    -- 2. Audited Break-Glass Superuser Escape Hatch (Fixes Minor #2)
    SELECT r.rolsuper INTO v_is_super FROM pg_catalog.pg_roles r WHERE r.rolname = v_caller;
    IF COALESCE(v_is_super, false) AND COALESCE(pg_catalog.current_setting('app.break_glass_reason', true), '') <> '' THEN
        IF TG_OP = 'DELETE' THEN RETURN OLD; ELSE RETURN NEW; END IF;
    END IF;

    -- 3. Block all direct DELETEs (Fixes S3)
    IF TG_OP = 'DELETE' THEN
        RAISE EXCEPTION 'DELIVERY_TASK_DELETE_FORBIDDEN' USING ERRCODE = 'RF010';
    END IF;

    -- 4. Guard INSERT invariants (Fixes S3 & M7: No forged SENT/RESERVED/epoch on insert; initial max_attempts <= 10)
    IF TG_OP = 'INSERT' THEN
        IF v_caller NOT IN ('relayforge_web', 'relayforge_worker') THEN
            RAISE EXCEPTION 'UNAUTHORIZED_INSERT_ROLE: %', v_caller USING ERRCODE = 'RF020';
        END IF;
        IF NEW.status NOT IN ('PENDING', 'SCHEDULED', 'READY')
           OR NEW.lease_epoch <> 0
           OR NEW.attempts <> 0
           OR NEW.max_attempts > 10
           OR NEW.next_retry_at IS NOT NULL
           OR NEW.lease_owner IS NOT NULL
           OR NEW.lease_expires_at IS NOT NULL
           OR NEW.remote_message_id IS NOT NULL THEN
            RAISE EXCEPTION 'ILLEGAL_INITIAL_DELIVERY_TASK_STATE' USING ERRCODE = 'RF010';
        END IF;
        RETURN NEW;
    END IF;

    -- 5. Guard UPDATE identity immutability
    IF NEW.id <> OLD.id
       OR NEW.tenant_id <> OLD.tenant_id
       OR NEW.workspace_id <> OLD.workspace_id
       OR NEW.route_id IS DISTINCT FROM OLD.route_id
       OR NEW.idempotency_key <> OLD.idempotency_key THEN
        RAISE EXCEPTION 'IMMUTABLE_IDENTITY_COLUMNS' USING ERRCODE = 'RF010';
    END IF;

    -- 6. Dispatcher Owner (Strict verification of due time, expiry, attempts+1, and epoch+1 — Fixes M6)
    IF v_caller = 'relayforge_dispatcher_owner' THEN
        IF NEW.lease_epoch <> OLD.lease_epoch + 1 THEN
            RAISE EXCEPTION 'DISPATCHER_MUST_INCREMENT_EPOCH' USING ERRCODE = 'RF011';
        END IF;

        -- Path A: Claiming due runnable task -> RESERVED
        IF OLD.status IN ('SCHEDULED', 'READY', 'RETRY_WAIT') AND NEW.status = 'RESERVED' THEN
            IF COALESCE(OLD.next_retry_at, OLD.scheduled_for) > v_now
               OR OLD.attempts >= OLD.max_attempts
               OR NEW.attempts <> OLD.attempts + 1
               OR NEW.next_retry_at IS NOT NULL
               OR NEW.lease_owner IS NULL
               OR NEW.lease_expires_at IS NULL THEN
                RAISE EXCEPTION 'DISPATCHER_CLAIM_INVARIANT_VIOLATION' USING ERRCODE = 'RF012';
            END IF;
            RETURN NEW;
        END IF;

        -- Path B: Reaping expired RESERVED or DISPATCHING_IN_FLIGHT lease
        IF ((OLD.status = 'RESERVED' AND NEW.status IN ('READY', 'FAILED'))
            OR (OLD.status = 'DISPATCHING_IN_FLIGHT' AND NEW.status = 'UNCERTAIN')) THEN
            IF OLD.lease_expires_at IS NULL OR OLD.lease_expires_at >= v_now
               OR NEW.attempts <> OLD.attempts
               OR (OLD.status = 'RESERVED' AND NEW.status = 'READY' AND OLD.attempts >= OLD.max_attempts)
               OR (OLD.status = 'RESERVED' AND NEW.status = 'FAILED' AND OLD.attempts < OLD.max_attempts)
               OR NEW.lease_owner IS NOT NULL
               OR NEW.lease_expires_at IS NOT NULL THEN
                RAISE EXCEPTION 'DISPATCHER_REAP_INVARIANT_VIOLATION' USING ERRCODE = 'RF012';
            END IF;
            RETURN NEW;
        END IF;

        RAISE EXCEPTION 'ILLEGAL_DISPATCHER_TRANSITION: % -> %', OLD.status, NEW.status USING ERRCODE = 'RF012';
    END IF;

    -- 7. Worker Role (Fenced transitions + Lease Heartbeat renewal — Fixes M5)
    IF v_caller = 'relayforge_worker' THEN
        IF NEW.lease_epoch <> OLD.lease_epoch OR NEW.attempts <> OLD.attempts OR NEW.max_attempts <> OLD.max_attempts THEN
            RAISE EXCEPTION 'WORKER_CANNOT_MUTATE_EPOCH_OR_ATTEMPTS' USING ERRCODE = 'RF013';
        END IF;

        IF OLD.status = 'PENDING' AND NEW.status IN ('SCHEDULED', 'READY', 'SKIPPED')
           AND NEW.lease_owner IS NULL AND NEW.lease_expires_at IS NULL
           AND NEW.remote_message_id IS NULL THEN
            RETURN NEW;
        END IF;

        IF OLD.status IN ('RESERVED', 'DISPATCHING_IN_FLIGHT') THEN
            v_supplied_epoch := NULLIF(pg_catalog.current_setting('app.lease_epoch', true), '')::bigint;
            IF v_supplied_epoch IS NULL OR v_supplied_epoch <> OLD.lease_epoch THEN
                RAISE EXCEPTION 'MISSING_OR_STALE_FENCING_EPOCH' USING ERRCODE = 'RF014';
            END IF;
            IF OLD.lease_expires_at IS NULL OR OLD.lease_expires_at < v_now THEN
                RAISE EXCEPTION 'WORKER_LEASE_EXPIRED' USING ERRCODE = 'RF015';
            END IF;

            -- Heartbeat lease renewal (same status, extended lease_expires_at — Fixes M5)
            IF NEW.status = OLD.status AND NEW.lease_owner = OLD.lease_owner
               AND NEW.lease_expires_at IS NOT NULL AND NEW.lease_expires_at > v_now THEN
                RETURN NEW;
            END IF;

            IF NEW.status = 'RETRY_WAIT' THEN
                IF OLD.attempts >= OLD.max_attempts THEN
                    RAISE EXCEPTION 'MAX_ATTEMPTS_EXCEEDED_MUST_FAIL' USING ERRCODE = 'RF016';
                END IF;
                IF NEW.next_retry_at IS NULL THEN
                    RAISE EXCEPTION 'RETRY_WAIT_REQUIRES_NEXT_RETRY_AT' USING ERRCODE = 'RF017';
                END IF;
            END IF;

            IF (OLD.status = 'RESERVED' AND NEW.status IN ('DISPATCHING_IN_FLIGHT', 'RETRY_WAIT', 'FAILED'))
               OR (OLD.status = 'DISPATCHING_IN_FLIGHT' AND NEW.status IN ('SENT', 'RETRY_WAIT', 'FAILED', 'UNCERTAIN')) THEN
                RETURN NEW;
            END IF;
        END IF;

        RAISE EXCEPTION 'ILLEGAL_WORKER_TRANSITION: % -> %', OLD.status, NEW.status USING ERRCODE = 'RF017';
    END IF;

    -- 8. Web Role (Non-status edits on waiting tasks, PENDING promotion, Cancel, and Audited UNCERTAIN exit — Fixes M5, M6, M7)
    IF v_caller = 'relayforge_web' THEN
        -- Allow non-status metadata/schedule edits on unreserved waiting tasks (Fixes M5)
        IF OLD.status IN ('PENDING', 'SCHEDULED', 'READY') AND NEW.status = OLD.status
           AND NEW.lease_epoch = OLD.lease_epoch AND NEW.attempts = OLD.attempts
           AND NEW.max_attempts <= 10
           AND NEW.lease_owner IS NULL AND NEW.lease_expires_at IS NULL
           AND NEW.remote_message_id IS NULL THEN
            RETURN NEW;
        END IF;

        -- Allow PENDING -> SCHEDULED/READY/SKIPPED (Fixes M6)
        IF OLD.status = 'PENDING' AND NEW.status IN ('SCHEDULED', 'READY', 'SKIPPED')
           AND NEW.lease_epoch = OLD.lease_epoch AND NEW.attempts = OLD.attempts
           AND NEW.max_attempts <= 10
           AND NEW.lease_owner IS NULL AND NEW.lease_expires_at IS NULL
           AND NEW.remote_message_id IS NULL THEN
            RETURN NEW;
        END IF;

        -- Allow cancellation of waiting tasks
        IF OLD.status IN ('PENDING', 'SCHEDULED', 'READY', 'RETRY_WAIT') AND NEW.status = 'CANCELLED'
           AND NEW.lease_epoch = OLD.lease_epoch + 1 AND NEW.attempts = OLD.attempts
           AND NEW.max_attempts = OLD.max_attempts
           AND NEW.remote_message_id IS NOT DISTINCT FROM OLD.remote_message_id THEN
            RETURN NEW;
        END IF;

        -- Audited reconciliation exit from UNCERTAIN (Fixes M7: allows max_attempts = attempts + 1 on requeue)
        IF OLD.status = 'UNCERTAIN' AND NEW.status IN ('SENT', 'READY', 'CANCELLED')
           AND NEW.lease_epoch = OLD.lease_epoch + 1 AND NEW.attempts = OLD.attempts
           AND NEW.max_attempts = (
               CASE WHEN NEW.status = 'READY' AND OLD.attempts >= OLD.max_attempts
                    THEN OLD.attempts + 1
                    ELSE OLD.max_attempts
               END
           ) THEN
            IF NOT EXISTS (
                SELECT 1 FROM app.reconciliation_records rr
                WHERE rr.delivery_task_id = OLD.id
                  AND rr.task_lease_epoch = OLD.lease_epoch
                  AND (
                      (rr.resolution_action = 'CONFIRM_SENT' AND NEW.status = 'SENT')
                      OR (rr.resolution_action = 'CONFIRM_NOT_SENT_REQUEUE' AND NEW.status = 'READY' AND rr.acknowledge_duplicate_risk = true)
                      OR (rr.resolution_action = 'DISCARD_DO_NOT_SEND' AND NEW.status = 'CANCELLED')
                  )
            ) THEN
                RAISE EXCEPTION 'UNCERTAIN_EXIT_REQUIRES_MATCHING_RECONCILIATION_RECORD' USING ERRCODE = 'RF018';
            END IF;
            RETURN NEW;
        END IF;

        RAISE EXCEPTION 'ILLEGAL_WEB_TRANSITION: % -> %', OLD.status, NEW.status USING ERRCODE = 'RF019';
    END IF;

    RAISE EXCEPTION 'UNAUTHORIZED_ROLE_FOR_DELIVERY_UPDATE: %', v_caller USING ERRCODE = 'RF020';
END;
$$;

CREATE TRIGGER trg_enforce_delivery_state_machine
BEFORE INSERT OR UPDATE OR DELETE ON app.delivery_tasks
FOR EACH ROW EXECUTE FUNCTION app.trg_fn_enforce_delivery_state_machine();

-- Budget Policy Origin Guard Trigger (v1.3.2 OC-01: Blocks relayforge_web from mutating OPERATOR_PLAN ceilings!)
CREATE OR REPLACE FUNCTION app.trg_fn_enforce_budget_policy_origin()
RETURNS TRIGGER
LANGUAGE plpgsql
SET search_path = pg_catalog, pg_temp
AS $$
DECLARE
    v_caller TEXT := CURRENT_USER;
    v_is_super BOOLEAN;
BEGIN
    IF v_caller = 'relayforge_migrator' AND pg_catalog.current_setting('app.migration_mode', true) = 'on' THEN
        IF TG_OP = 'DELETE' THEN RETURN OLD; ELSE RETURN NEW; END IF;
    END IF;

    SELECT r.rolsuper INTO v_is_super FROM pg_catalog.pg_roles r WHERE r.rolname = v_caller;
    IF COALESCE(v_is_super, false) AND COALESCE(pg_catalog.current_setting('app.break_glass_reason', true), '') <> '' THEN
        IF TG_OP = 'DELETE' THEN RETURN OLD; ELSE RETURN NEW; END IF;
    END IF;

    -- Lock acquisition via FOR UPDATE inside fn_budget_reserve / fn_budget_settle does not fire BEFORE UPDATE triggers,
    -- only actual DML writes fire this trigger!
    IF v_caller = 'relayforge_operator' THEN
        IF (TG_OP IN ('UPDATE', 'DELETE') AND OLD.policy_origin <> 'OPERATOR_PLAN')
           OR (TG_OP IN ('INSERT', 'UPDATE') AND NEW.policy_origin <> 'OPERATOR_PLAN') THEN
            RAISE EXCEPTION 'OPERATOR_ROLE_ONLY_MANAGES_OPERATOR_PLAN' USING ERRCODE = 'RF007';
        END IF;
        IF TG_OP = 'DELETE' THEN RETURN OLD; ELSE RETURN NEW; END IF;
    END IF;

    IF v_caller = 'relayforge_web' THEN
        IF (TG_OP IN ('UPDATE', 'DELETE') AND OLD.policy_origin = 'OPERATOR_PLAN')
           OR (TG_OP IN ('INSERT', 'UPDATE') AND NEW.policy_origin = 'OPERATOR_PLAN') THEN
            RAISE EXCEPTION 'TENANT_WEB_CANNOT_MUTATE_OPERATOR_PLAN_QUOTA' USING ERRCODE = 'RF007';
        END IF;
        IF TG_OP = 'DELETE' THEN RETURN OLD; ELSE RETURN NEW; END IF;
    END IF;

    RAISE EXCEPTION 'UNAUTHORIZED_BUDGET_POLICY_DML_ROLE: %', v_caller USING ERRCODE = 'RF007';
END;
$$;

CREATE TRIGGER trg_enforce_budget_policy_origin
BEFORE INSERT OR UPDATE OR DELETE ON app.budget_policies
FOR EACH ROW EXECUTE FUNCTION app.trg_fn_enforce_budget_policy_origin();

RESET ROLE;

-- ============================================================================
-- 6. Functions: Water-Filling Fair Claim, Reaper, Heartbeat, Transition, Reconcile & Budget
-- ============================================================================

-- Water-Filling Per-Tenant Index-Order SKIP LOCKED Claim (Fixes M10 + preserves 100+100 concurrency!)
--   * Never scans full tenant backlog: walks idx_delivery_fair_claim (tenant_id, runnable_at ASC) up to LIMIT v_share.
--   * Never over-locks unclaimed rows: every row locked by FOR UPDATE SKIP LOCKED is claimed in the same statement.
--   * Concurrent sessions on 200 tasks with batch_size=100 claim 100 + 100 with zero overlap.
--   * Updates tenant_dispatch_fairness in deterministic tenant_id ASC order (zero cross-tenant deadlock).
CREATE OR REPLACE FUNCTION app.fn_worker_claim_next_jobs(
    p_worker_id TEXT,
    p_batch_size INT,
    p_lease_ttl_sec INT
)
RETURNS TABLE (claimed_task_id UUID, claimed_tenant_id UUID, claimed_epoch BIGINT)
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $$
DECLARE
    v_limit INT;
    v_rem INT;
    v_ttl INTERVAL;
    v_now TIMESTAMPTZ := pg_catalog.clock_timestamp();
    v_tenants UUID[];
    v_next_tenants UUID[];
    v_exhausted UUID[] := ARRAY[]::UUID[];
    v_touched_tenants UUID[] := ARRAY[]::UUID[];
    v_tid UUID;
    v_idx INT;
    v_rem_tenants INT;
    v_share INT;
    v_got INT;
BEGIN
    IF p_worker_id IS NULL OR p_batch_size IS NULL OR p_lease_ttl_sec IS NULL THEN
        RAISE EXCEPTION 'NULL_ARGUMENT_FORBIDDEN' USING ERRCODE = '22004';
    END IF;

    v_limit := LEAST(GREATEST(p_batch_size, 1), 100);
    v_rem := v_limit;
    v_ttl := LEAST(GREATEST(p_lease_ttl_sec, 15), 600) * INTERVAL '1 second';

    WHILE v_rem > 0 LOOP
        SELECT ARRAY(
            SELECT t.id
            FROM app.tenants t
            LEFT JOIN app.tenant_dispatch_fairness f ON f.tenant_id = t.id
            WHERE NOT (t.id = ANY(v_exhausted))
              AND EXISTS (
                  SELECT 1 FROM app.delivery_tasks dt
                  WHERE dt.tenant_id = t.id
                    AND dt.status IN ('READY', 'SCHEDULED', 'RETRY_WAIT')
                    AND dt.runnable_at <= v_now
                    AND dt.attempts < dt.max_attempts
              )
            ORDER BY COALESCE(f.last_served_at, '-infinity'::timestamptz) ASC, t.id ASC
            LIMIT v_rem
        ) INTO v_tenants;

        IF COALESCE(pg_catalog.cardinality(v_tenants), 0) = 0 THEN
            EXIT;
        END IF;

        WHILE v_rem > 0 AND COALESCE(pg_catalog.cardinality(v_tenants), 0) > 0 LOOP
            v_next_tenants := ARRAY[]::UUID[];
            v_rem_tenants := pg_catalog.cardinality(v_tenants);

            FOR v_idx IN 1..v_rem_tenants LOOP
                EXIT WHEN v_rem <= 0;
                v_tid := v_tenants[v_idx];
                v_share := GREATEST((v_rem + (v_rem_tenants - v_idx)) / (v_rem_tenants - v_idx + 1), 1);

                RETURN QUERY
                WITH locked AS (
                    SELECT dt.id
                    FROM app.delivery_tasks dt
                    WHERE dt.tenant_id = v_tid
                      AND dt.status IN ('READY', 'SCHEDULED', 'RETRY_WAIT')
                      AND dt.runnable_at <= v_now
                      AND dt.attempts < dt.max_attempts
                    ORDER BY dt.runnable_at ASC
                    LIMIT v_share
                    FOR UPDATE OF dt SKIP LOCKED
                ),
                upd AS (
                    UPDATE app.delivery_tasks t
                    SET status = 'RESERVED',
                        next_retry_at = NULL,
                        lease_owner = p_worker_id,
                        lease_expires_at = v_now + v_ttl,
                        lease_epoch = t.lease_epoch + 1,
                        attempts = t.attempts + 1,
                        updated_at = v_now
                    FROM locked l
                    WHERE t.id = l.id
                    RETURNING t.id, t.tenant_id, t.lease_epoch
                )
                SELECT u.id, u.tenant_id, u.lease_epoch FROM upd u;

                GET DIAGNOSTICS v_got = ROW_COUNT;

                IF v_got > 0 THEN
                    v_rem := v_rem - v_got;
                    v_touched_tenants := pg_catalog.array_append(v_touched_tenants, v_tid);
                END IF;

                IF v_got = v_share AND v_rem > 0 THEN
                    v_next_tenants := pg_catalog.array_append(v_next_tenants, v_tid);
                ELSE
                    v_exhausted := pg_catalog.array_append(v_exhausted, v_tid);
                END IF;
            END LOOP;

            v_tenants := v_next_tenants;
        END LOOP;
    END LOOP;

    IF COALESCE(pg_catalog.cardinality(v_touched_tenants), 0) > 0 THEN
        INSERT INTO app.tenant_dispatch_fairness (tenant_id, last_served_at)
        SELECT d.tid, v_now
        FROM (
            SELECT DISTINCT u.tid
            FROM pg_catalog.unnest(v_touched_tenants) AS u(tid)
            ORDER BY u.tid ASC
        ) d
        ON CONFLICT (tenant_id) DO UPDATE
            SET last_served_at = EXCLUDED.last_served_at
            WHERE app.tenant_dispatch_fairness.last_served_at < v_now - INTERVAL '100 milliseconds';
    END IF;
END;
$$;

ALTER FUNCTION app.fn_worker_claim_next_jobs(TEXT, INT, INT) OWNER TO relayforge_dispatcher_owner;
REVOKE ALL ON FUNCTION app.fn_worker_claim_next_jobs(TEXT, INT, INT) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION app.fn_worker_claim_next_jobs(TEXT, INT, INT) TO relayforge_worker;

CREATE OR REPLACE FUNCTION app.fn_reap_expired_delivery_leases(p_batch_size INT)
RETURNS INT
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $$
DECLARE
    v_limit INT;
    v_now TIMESTAMPTZ := pg_catalog.clock_timestamp();
    v_count INT;
BEGIN
    IF p_batch_size IS NULL THEN
        RAISE EXCEPTION 'NULL_ARGUMENT_FORBIDDEN' USING ERRCODE = '22004';
    END IF;
    v_limit := LEAST(GREATEST(p_batch_size, 1), 200);

    WITH expired AS (
        SELECT id
        FROM app.delivery_tasks
        WHERE status IN ('RESERVED', 'DISPATCHING_IN_FLIGHT')
          AND lease_expires_at < v_now
        ORDER BY lease_expires_at ASC
        LIMIT v_limit
        FOR UPDATE SKIP LOCKED
    ),
    updated AS (
        UPDATE app.delivery_tasks t
        SET status = CASE
                WHEN t.status = 'DISPATCHING_IN_FLIGHT' THEN 'UNCERTAIN'
                WHEN t.attempts >= t.max_attempts THEN 'FAILED'
                ELSE 'READY'
            END,
            lease_owner = NULL,
            lease_expires_at = NULL,
            lease_epoch = t.lease_epoch + 1,
            updated_at = v_now
        FROM expired e
        WHERE t.id = e.id
        RETURNING t.id
    )
    SELECT count(*)::int INTO v_count FROM updated;
    RETURN v_count;
END;
$$;

ALTER FUNCTION app.fn_reap_expired_delivery_leases(INT) OWNER TO relayforge_dispatcher_owner;
REVOKE ALL ON FUNCTION app.fn_reap_expired_delivery_leases(INT) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION app.fn_reap_expired_delivery_leases(INT) TO relayforge_worker;

-- Worker Heartbeat Lease Renewal (Fixes M5: prevents false UNCERTAIN on large media uploads)
CREATE OR REPLACE FUNCTION app.fn_worker_heartbeat_lease(
    p_task_id UUID,
    p_expected_epoch BIGINT,
    p_extend_sec INT
)
RETURNS TIMESTAMPTZ
LANGUAGE plpgsql
SECURITY INVOKER
SET search_path = pg_catalog, pg_temp
AS $$
DECLARE
    v_new_exp TIMESTAMPTZ;
BEGIN
    IF p_task_id IS NULL OR p_expected_epoch IS NULL OR p_extend_sec IS NULL THEN
        RAISE EXCEPTION 'NULL_ARGUMENT_FORBIDDEN' USING ERRCODE = '22004';
    END IF;

    PERFORM pg_catalog.set_config('app.lease_epoch', p_expected_epoch::text, true);

    UPDATE app.delivery_tasks
    SET lease_expires_at = pg_catalog.clock_timestamp() + (LEAST(GREATEST(p_extend_sec, 15), 600) * INTERVAL '1 second'),
        updated_at = pg_catalog.clock_timestamp()
    WHERE id = p_task_id
      AND lease_epoch = p_expected_epoch
      AND status IN ('RESERVED', 'DISPATCHING_IN_FLIGHT')
    RETURNING lease_expires_at INTO v_new_exp;

    IF v_new_exp IS NULL THEN
        RAISE EXCEPTION 'HEARTBEAT_FENCING_OR_TASK_NOT_ACTIVE' USING ERRCODE = 'RF014';
    END IF;
    RETURN v_new_exp;
END;
$$;

ALTER FUNCTION app.fn_worker_heartbeat_lease(UUID, BIGINT, INT) OWNER TO relayforge_migrator;
REVOKE ALL ON FUNCTION app.fn_worker_heartbeat_lease(UUID, BIGINT, INT) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION app.fn_worker_heartbeat_lease(UUID, BIGINT, INT) TO relayforge_worker;

CREATE OR REPLACE FUNCTION app.fn_worker_transition_task(
    p_task_id UUID,
    p_expected_epoch BIGINT,
    p_new_status TEXT,
    p_remote_message_id TEXT DEFAULT NULL,
    p_next_retry_at TIMESTAMPTZ DEFAULT NULL
)
RETURNS VOID
LANGUAGE plpgsql
SECURITY INVOKER
SET search_path = pg_catalog, pg_temp
AS $$
DECLARE
    v_rows INT;
BEGIN
    IF p_task_id IS NULL OR p_expected_epoch IS NULL OR p_new_status IS NULL THEN
        RAISE EXCEPTION 'NULL_ARGUMENT_FORBIDDEN' USING ERRCODE = '22004';
    END IF;

    PERFORM pg_catalog.set_config('app.lease_epoch', p_expected_epoch::text, true);

    UPDATE app.delivery_tasks
    SET status = p_new_status,
        remote_message_id = COALESCE(p_remote_message_id, remote_message_id),
        next_retry_at = CASE WHEN p_new_status = 'RETRY_WAIT' THEN p_next_retry_at ELSE NULL END,
        lease_owner = CASE WHEN p_new_status IN ('SENT', 'RETRY_WAIT', 'FAILED', 'UNCERTAIN') THEN NULL ELSE lease_owner END,
        lease_expires_at = CASE WHEN p_new_status IN ('SENT', 'RETRY_WAIT', 'FAILED', 'UNCERTAIN') THEN NULL ELSE lease_expires_at END,
        updated_at = pg_catalog.clock_timestamp()
    WHERE id = p_task_id AND lease_epoch = p_expected_epoch;

    GET DIAGNOSTICS v_rows = ROW_COUNT;
    IF v_rows <> 1 THEN
        RAISE EXCEPTION 'FENCING_OR_TASK_NOT_FOUND' USING ERRCODE = 'RF014';
    END IF;
END;
$$;

ALTER FUNCTION app.fn_worker_transition_task(UUID, BIGINT, TEXT, TEXT, TIMESTAMPTZ) OWNER TO relayforge_migrator;
REVOKE ALL ON FUNCTION app.fn_worker_transition_task(UUID, BIGINT, TEXT, TEXT, TIMESTAMPTZ) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION app.fn_worker_transition_task(UUID, BIGINT, TEXT, TEXT, TIMESTAMPTZ) TO relayforge_worker;

CREATE OR REPLACE FUNCTION app.fn_web_reconcile_uncertain_task(
    p_reconciliation_id UUID,
    p_task_id UUID,
    p_expected_epoch BIGINT,
    p_action TEXT,
    p_ack_duplicate BOOLEAN,
    p_decided_by TEXT,
    p_reason TEXT
)
RETURNS VOID
LANGUAGE plpgsql
SECURITY INVOKER
SET search_path = pg_catalog, pg_temp
AS $$
DECLARE
    v_tenant_id UUID := NULLIF(pg_catalog.current_setting('app.tenant_id', true), '')::uuid;
    v_target_status TEXT;
    v_rows INT;
BEGIN
    IF p_reconciliation_id IS NULL OR p_task_id IS NULL OR p_expected_epoch IS NULL
       OR p_action IS NULL OR p_ack_duplicate IS NULL OR p_decided_by IS NULL OR p_reason IS NULL THEN
        RAISE EXCEPTION 'NULL_ARGUMENT_FORBIDDEN' USING ERRCODE = '22004';
    END IF;
    IF v_tenant_id IS NULL THEN
        RAISE EXCEPTION 'MISSING_TENANT_CONTEXT' USING ERRCODE = 'RF003';
    END IF;

    v_target_status := CASE p_action
        WHEN 'CONFIRM_SENT' THEN 'SENT'
        WHEN 'CONFIRM_NOT_SENT_REQUEUE' THEN 'READY'
        WHEN 'DISCARD_DO_NOT_SEND' THEN 'CANCELLED'
        ELSE NULL
    END;
    IF v_target_status IS NULL THEN
        RAISE EXCEPTION 'INVALID_RECONCILIATION_ACTION' USING ERRCODE = '22023';
    END IF;

    INSERT INTO app.reconciliation_records (
        id, tenant_id, delivery_task_id, task_lease_epoch,
        resolution_action, acknowledge_duplicate_risk, decided_by, reason
    ) VALUES (
        p_reconciliation_id, v_tenant_id, p_task_id, p_expected_epoch,
        p_action, p_ack_duplicate, p_decided_by, p_reason
    );

    UPDATE app.delivery_tasks
    SET status = v_target_status,
        max_attempts = CASE WHEN v_target_status = 'READY' AND attempts >= max_attempts THEN attempts + 1 ELSE max_attempts END,
        lease_epoch = lease_epoch + 1,
        updated_at = pg_catalog.clock_timestamp()
    WHERE id = p_task_id AND status = 'UNCERTAIN' AND lease_epoch = p_expected_epoch;

    GET DIAGNOSTICS v_rows = ROW_COUNT;
    IF v_rows <> 1 THEN
        RAISE EXCEPTION 'UNCERTAIN_TASK_OR_EPOCH_MISMATCH' USING ERRCODE = 'RF018';
    END IF;
END;
$$;

ALTER FUNCTION app.fn_web_reconcile_uncertain_task(UUID, UUID, BIGINT, TEXT, BOOLEAN, TEXT, TEXT) OWNER TO relayforge_migrator;
REVOKE ALL ON FUNCTION app.fn_web_reconcile_uncertain_task(UUID, UUID, BIGINT, TEXT, BOOLEAN, TEXT, TEXT) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION app.fn_web_reconcile_uncertain_task(UUID, UUID, BIGINT, TEXT, BOOLEAN, TEXT, TEXT) TO relayforge_web;

-- Server-Derived Scoped Atomic Budget Reserve (Fixes S4, M8, M9; Fail-Closed on missing policy)
-- Owned by relayforge_ledger_owner (NOBYPASSRLS + subject to app.tenant_id FORCE RLS!)
CREATE OR REPLACE FUNCTION app.fn_budget_reserve(
    p_reservation_id UUID,
    p_workspace_id UUID,
    p_route_id UUID,
    p_idempotency_key TEXT,
    p_amount_micros BIGINT,
    p_ttl_sec INT
)
RETURNS UUID
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $$
DECLARE
    v_tenant_id UUID;
    v_existing RECORD;
    v_pol RECORD;
    v_locked_plan_policies INT := 0;
    v_bucket TEXT;
    v_used BIGINT;
    v_now TIMESTAMPTZ := pg_catalog.clock_timestamp();
BEGIN
    IF p_reservation_id IS NULL OR p_workspace_id IS NULL OR p_idempotency_key IS NULL
       OR p_amount_micros IS NULL OR p_ttl_sec IS NULL THEN
        RAISE EXCEPTION 'NULL_ARGUMENT_FORBIDDEN' USING ERRCODE = '22004';
    END IF;

    IF p_amount_micros < 0 THEN
        RAISE EXCEPTION 'NEGATIVE_AMOUNT_FORBIDDEN' USING ERRCODE = '22023';
    END IF;

    v_tenant_id := NULLIF(pg_catalog.current_setting('app.tenant_id', true), '')::uuid;
    IF v_tenant_id IS NULL THEN
        RAISE EXCEPTION 'MISSING_TENANT_CONTEXT' USING ERRCODE = 'RF003';
    END IF;

    -- Verify workspace (and optional route) exist within current tenant RLS boundary
    IF NOT EXISTS (SELECT 1 FROM app.workspaces w WHERE w.id = p_workspace_id) THEN
        RAISE EXCEPTION 'INVALID_OR_UNAUTHORIZED_WORKSPACE' USING ERRCODE = 'RF002';
    END IF;

    IF p_route_id IS NOT NULL AND NOT EXISTS (
        SELECT 1 FROM app.routes r WHERE r.id = p_route_id AND r.workspace_id = p_workspace_id
    ) THEN
        RAISE EXCEPTION 'INVALID_OR_UNAUTHORIZED_ROUTE' USING ERRCODE = 'RF002';
    END IF;

    -- 1. Server derives & locks ALL active policies (mandatory OPERATOR_PLAN + optional TENANT_BUDGET sub-caps) in deterministic ID order!
    FOR v_pol IN
        SELECT bp.id, bp.policy_origin
        FROM app.budget_policies bp
        WHERE bp.is_active = true
          AND (
              (bp.scope_type = 'TENANT' AND bp.scope_ref_id = v_tenant_id)
              OR (bp.scope_type = 'WORKSPACE' AND bp.scope_ref_id = p_workspace_id)
              OR (p_route_id IS NOT NULL AND bp.scope_type = 'ROUTE' AND bp.scope_ref_id = p_route_id)
          )
        ORDER BY bp.id ASC
        FOR UPDATE
    LOOP
        IF v_pol.policy_origin = 'OPERATOR_PLAN' THEN
            v_locked_plan_policies := v_locked_plan_policies + 1;
        END IF;
    END LOOP;

    -- Fail-Closed (v1.3.2 OC-01): Every tenant MUST have at least one active OPERATOR_PLAN quota; TENANT_BUDGET alone is NOT sufficient!
    IF v_locked_plan_policies = 0 THEN
        RAISE EXCEPTION 'NO_ACTIVE_OPERATOR_PLAN_QUOTA_FOR_TENANT' USING ERRCODE = 'RF006';
    END IF;

    -- 2. Idempotent replay check with strict parameter verification (Fixes M9a)
    SELECT br.id, br.workspace_id, br.route_id, br.reserved_micros
    INTO v_existing
    FROM app.budget_reservations br
    WHERE br.tenant_id = v_tenant_id AND br.idempotency_key = p_idempotency_key;

    IF v_existing.id IS NOT NULL THEN
        IF v_existing.workspace_id <> p_workspace_id
           OR v_existing.route_id IS DISTINCT FROM p_route_id
           OR v_existing.reserved_micros <> p_amount_micros THEN
            RAISE EXCEPTION 'IDEMPOTENCY_PARAMETER_MISMATCH' USING ERRCODE = 'RF005';
        END IF;
        RETURN v_existing.id;
    END IF;

    -- 3. Evaluate ceiling on EVERY derived active policy within its UTC period_bucket
    FOR v_pol IN
        SELECT bp.id, bp.period_kind, bp.max_cost_micros
        FROM app.budget_policies bp
        WHERE bp.is_active = true
          AND (
              (bp.scope_type = 'TENANT' AND bp.scope_ref_id = v_tenant_id)
              OR (bp.scope_type = 'WORKSPACE' AND bp.scope_ref_id = p_workspace_id)
              OR (p_route_id IS NOT NULL AND bp.scope_type = 'ROUTE' AND bp.scope_ref_id = p_route_id)
          )
        ORDER BY bp.id ASC
    LOOP
        v_bucket := CASE v_pol.period_kind
            WHEN 'DAILY' THEN pg_catalog.to_char(v_now AT TIME ZONE 'UTC', 'YYYY-MM-DD')
            WHEN 'MONTHLY' THEN pg_catalog.to_char(v_now AT TIME ZONE 'UTC', 'YYYY-MM')
            ELSE 'TOTAL'
        END;

        SELECT COALESCE(SUM(
            CASE
                WHEN br.state = 'COMMITTED' THEN COALESCE(br.settled_micros, br.reserved_micros)
                WHEN br.state = 'RESERVED' AND br.expires_at > v_now THEN br.reserved_micros
                ELSE 0
            END
        ), 0) INTO v_used
        FROM app.budget_reservation_allocations bra
        JOIN app.budget_reservations br
          ON br.tenant_id = bra.tenant_id AND br.id = bra.reservation_id
        WHERE bra.policy_id = v_pol.id AND bra.period_bucket = v_bucket;

        IF (v_used + p_amount_micros) > v_pol.max_cost_micros THEN
            RAISE EXCEPTION 'BUDGET_EXCEEDED on policy % bucket % (used=%, req=%, max=%)',
                v_pol.id, v_bucket, v_used, p_amount_micros, v_pol.max_cost_micros
                USING ERRCODE = 'RF001';
        END IF;
    END LOOP;

    -- 4. Insert reservation + allocation row for EVERY derived active policy
    INSERT INTO app.budget_reservations (
        id, tenant_id, workspace_id, route_id, idempotency_key,
        state, reserved_micros, expires_at, created_at
    ) VALUES (
        p_reservation_id, v_tenant_id, p_workspace_id, p_route_id, p_idempotency_key,
        'RESERVED', p_amount_micros,
        v_now + (LEAST(GREATEST(p_ttl_sec, 10), 3600) * INTERVAL '1 second'),
        v_now
    );

    INSERT INTO app.budget_reservation_allocations (reservation_id, policy_id, tenant_id, period_bucket)
    SELECT p_reservation_id,
           bp.id,
           v_tenant_id,
           CASE bp.period_kind
               WHEN 'DAILY' THEN pg_catalog.to_char(v_now AT TIME ZONE 'UTC', 'YYYY-MM-DD')
               WHEN 'MONTHLY' THEN pg_catalog.to_char(v_now AT TIME ZONE 'UTC', 'YYYY-MM')
               ELSE 'TOTAL'
           END
    FROM app.budget_policies bp
    WHERE bp.is_active = true
      AND (
          (bp.scope_type = 'TENANT' AND bp.scope_ref_id = v_tenant_id)
          OR (bp.scope_type = 'WORKSPACE' AND bp.scope_ref_id = p_workspace_id)
          OR (p_route_id IS NOT NULL AND bp.scope_type = 'ROUTE' AND bp.scope_ref_id = p_route_id)
      );

    RETURN p_reservation_id;
END;
$$;

ALTER FUNCTION app.fn_budget_reserve(UUID, UUID, UUID, TEXT, BIGINT, INT) OWNER TO relayforge_ledger_owner;
REVOKE ALL ON FUNCTION app.fn_budget_reserve(UUID, UUID, UUID, TEXT, BIGINT, INT) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION app.fn_budget_reserve(UUID, UUID, UUID, TEXT, BIGINT, INT) TO relayforge_web, relayforge_worker;

-- Budget Settlement with Expired/Overage Ceiling Re-Check (Fixes S4b & M9b)
CREATE OR REPLACE FUNCTION app.fn_budget_settle(
    p_reservation_id UUID,
    p_final_state TEXT,
    p_settled_micros BIGINT DEFAULT NULL
)
RETURNS VOID
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $$
DECLARE
    v_tenant_id UUID := NULLIF(pg_catalog.current_setting('app.tenant_id', true), '')::uuid;
    v_res RECORD;
    v_alloc RECORD;
    v_other_used BIGINT;
    v_now TIMESTAMPTZ := pg_catalog.clock_timestamp();
BEGIN
    IF p_reservation_id IS NULL OR p_final_state IS NULL THEN
        RAISE EXCEPTION 'NULL_ARGUMENT_FORBIDDEN' USING ERRCODE = '22004';
    END IF;
    IF v_tenant_id IS NULL THEN
        RAISE EXCEPTION 'MISSING_TENANT_CONTEXT' USING ERRCODE = 'RF003';
    END IF;
    IF p_final_state NOT IN ('COMMITTED', 'RELEASED') THEN
        RAISE EXCEPTION 'INVALID_BUDGET_SETTLE_STATE' USING ERRCODE = '22023';
    END IF;
    IF p_final_state = 'COMMITTED' AND (p_settled_micros IS NULL OR p_settled_micros < 0) THEN
        RAISE EXCEPTION 'COMMITTED_REQUIRES_NON_NEGATIVE_SETTLED_MICROS' USING ERRCODE = '22023';
    END IF;

    -- Lock policies linked to this reservation in deterministic order
    FOR v_alloc IN
        SELECT bp.id
        FROM app.budget_reservation_allocations bra
        JOIN app.budget_policies bp ON bp.tenant_id = bra.tenant_id AND bp.id = bra.policy_id
        WHERE bra.reservation_id = p_reservation_id
        ORDER BY bp.id ASC
        FOR UPDATE OF bp
    LOOP
        NULL;
    END LOOP;

    SELECT br.id, br.state, br.reserved_micros, br.expires_at
    INTO v_res
    FROM app.budget_reservations br
    WHERE br.id = p_reservation_id
    FOR UPDATE;

    IF v_res.id IS NULL OR v_res.state <> 'RESERVED' THEN
        RAISE EXCEPTION 'RESERVATION_NOT_ACTIVE_OR_NOT_FOUND' USING ERRCODE = 'RF004';
    END IF;

    -- Fixes M9b: If hold already expired OR settled > reserved, verify ceiling is not breached!
    IF p_final_state = 'COMMITTED' AND (v_res.expires_at <= v_now OR p_settled_micros > v_res.reserved_micros) THEN
        FOR v_alloc IN
            SELECT bp.id AS policy_id, bp.max_cost_micros, bra.period_bucket
            FROM app.budget_reservation_allocations bra
            JOIN app.budget_policies bp ON bp.tenant_id = bra.tenant_id AND bp.id = bra.policy_id
            WHERE bra.reservation_id = p_reservation_id
            ORDER BY bp.id ASC
        LOOP
            SELECT COALESCE(SUM(
                CASE
                    WHEN br.state = 'COMMITTED' THEN COALESCE(br.settled_micros, br.reserved_micros)
                    WHEN br.state = 'RESERVED' AND br.expires_at > v_now THEN br.reserved_micros
                    ELSE 0
                END
            ), 0) INTO v_other_used
            FROM app.budget_reservation_allocations bra2
            JOIN app.budget_reservations br ON br.tenant_id = bra2.tenant_id AND br.id = bra2.reservation_id
            WHERE bra2.policy_id = v_alloc.policy_id
              AND bra2.period_bucket = v_alloc.period_bucket
              AND br.id <> p_reservation_id;

            IF (v_other_used + p_settled_micros) > v_alloc.max_cost_micros THEN
                RAISE EXCEPTION 'LATE_OR_OVERAGE_SETTLEMENT_EXCEEDS_CAP on policy %', v_alloc.policy_id
                    USING ERRCODE = 'RF004';
            END IF;
        END LOOP;
    END IF;

    UPDATE app.budget_reservations
    SET state = p_final_state,
        settled_micros = CASE WHEN p_final_state = 'COMMITTED' THEN p_settled_micros ELSE 0 END
    WHERE id = p_reservation_id;
END;
$$;

ALTER FUNCTION app.fn_budget_settle(UUID, TEXT, BIGINT) OWNER TO relayforge_ledger_owner;
REVOKE ALL ON FUNCTION app.fn_budget_settle(UUID, TEXT, BIGINT) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION app.fn_budget_settle(UUID, TEXT, BIGINT) TO relayforge_web, relayforge_worker;

-- ============================================================================
-- 7. Non-Vacuous Behavioral Verification Suite (Mutation-Proof via v_caught flags)
-- ============================================================================
DO $$
DECLARE
    v_t1 UUID := '11111111-1111-1111-1111-111111111111';
    v_t2 UUID := '22222222-2222-2222-2222-222222222222';
    v_ws1 UUID := 'aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa';
    v_ws2 UUID := 'aaaaaaaa-aaaa-aaaa-aaaa-bbbbbbbbbbbb';
    v_rt1 UUID := '99999999-9999-9999-9999-999999999901';
    v_rt2 UUID := '99999999-9999-9999-9999-999999999902';
    v_pol_tenant UUID := 'bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb';
    v_pol_ws UUID := 'cccccccc-cccc-cccc-cccc-cccccccccccc';
    v_pol_route UUID := 'cccccccc-cccc-cccc-cccc-dddddddddddd';
    v_task1 UUID := 'dddddddd-dddd-dddd-dddd-dddddddddd01';
    v_task_t2 UUID := 'dddddddd-dddd-dddd-dddd-dddddddddd09';
    v_task_reap_unc UUID := 'dddddddd-dddd-dddd-dddd-dddddddddd02';
    v_task_reap_fail UUID := 'dddddddd-dddd-dddd-dddd-dddddddddd03';
    v_task_reap_ready UUID := 'dddddddd-dddd-dddd-dddd-dddddddddd04';
    v_yesterday_bucket TEXT := to_char((clock_timestamp() - interval '1 day') AT TIME ZONE 'UTC', 'YYYY-MM-DD');
    v_claim RECORD;
    v_rows INT;
    v_caught BOOLEAN;
    v_replay_id UUID;
    v_hb_exp TIMESTAMPTZ;
BEGIN
    -- [A] Migrator Seeding, Multi-Table Backfill & Yesterday DAILY Rollover Fixture
    EXECUTE 'SET LOCAL ROLE relayforge_migrator';
    PERFORM set_config('app.migration_mode', 'on', true);

    INSERT INTO app.tenants (id, slug) VALUES (v_t1, 'tenant-a'), (v_t2, 'tenant-b');
    INSERT INTO app.workspaces (id, tenant_id, slug) VALUES (v_ws1, v_t1, 'ws-a'), (v_ws2, v_t2, 'ws-b');
    INSERT INTO app.routes (id, tenant_id, workspace_id, slug) VALUES (v_rt1, v_t1, v_ws1, 'rt-a1'), (v_rt2, v_t2, v_ws2, 'rt-b1');

    UPDATE app.workspaces SET slug = 'ws-a-migrated' WHERE id = v_ws1;
    GET DIAGNOSTICS v_rows = ROW_COUNT;
    ASSERT v_rows = 1, 'Migrator backfill failed on workspaces';

    INSERT INTO app.budget_policies (id, tenant_id, policy_origin, scope_type, scope_ref_id, period_kind, max_cost_micros)
    VALUES (v_pol_tenant, v_t1, 'OPERATOR_PLAN', 'TENANT', v_t1, 'TOTAL', 1000),
           (v_pol_ws, v_t1, 'TENANT_BUDGET', 'WORKSPACE', v_ws1, 'DAILY', 400),
           (v_pol_route, v_t1, 'TENANT_BUDGET', 'ROUTE', v_rt1, 'DAILY', 250);

    -- Seed a COMMITTED reservation of 400 in YESTERDAY's DAILY bucket on ws-a (counts toward TOTAL=1000, NOT today's DAILY=400!)
    INSERT INTO app.budget_reservations (id, tenant_id, workspace_id, route_id, idempotency_key, state, reserved_micros, settled_micros, expires_at)
    VALUES ('00000000-0000-0000-0000-0000000000aa', v_t1, v_ws1, NULL, 'idem-yesterday', 'COMMITTED', 400, 400, clock_timestamp() - interval '23 hours');
    INSERT INTO app.budget_reservation_allocations (reservation_id, policy_id, tenant_id, period_bucket)
    VALUES ('00000000-0000-0000-0000-0000000000aa', v_pol_ws, v_t1, v_yesterday_bucket),
           ('00000000-0000-0000-0000-0000000000aa', v_pol_tenant, v_t1, 'TOTAL');

    PERFORM set_config('app.migration_mode', '', true);
    RESET ROLE;

    -- [B] Cross-Tenant RLS Isolation (Empty Context, Cross-Read, Cross-Insert) & Web Task Lifecycle
    EXECUTE 'SET LOCAL ROLE relayforge_web';
    PERFORM set_config('app.tenant_id', '', true);
    ASSERT (SELECT count(*) FROM app.workspaces) = 0, 'Empty tenant_id context leaked rows!';

    PERFORM set_config('app.tenant_id', v_t2::text, true);
    ASSERT (SELECT count(*) FROM app.workspaces WHERE id = v_ws1) = 0, 'Cross-tenant leak: Tenant 2 saw Tenant 1 workspace!';

    v_caught := false;
    BEGIN
        INSERT INTO app.workspaces (id, tenant_id, slug) VALUES ('aaaaaaaa-aaaa-aaaa-aaaa-cccccccccccc', v_t1, 'cross-spoof');
    EXCEPTION WHEN insufficient_privilege THEN
        v_caught := true;
    END;
    ASSERT v_caught, 'Cross-tenant INSERT was NOT blocked by WITH CHECK RLS!';

    INSERT INTO app.delivery_tasks (id, tenant_id, workspace_id, route_id, idempotency_key, status, scheduled_for, max_attempts)
    VALUES (v_task_t2, v_t2, v_ws2, v_rt2, 'idem-t2-1', 'READY', clock_timestamp() - interval '1 second', 4);

    PERFORM set_config('app.tenant_id', v_t1::text, true);
    INSERT INTO app.delivery_tasks (id, tenant_id, workspace_id, route_id, idempotency_key, status, scheduled_for, max_attempts)
    VALUES (v_task1, v_t1, v_ws1, v_rt1, 'idem-1', 'PENDING', clock_timestamp() - interval '2 seconds', 10);

    -- Web non-status edit on PENDING + promotion PENDING -> SCHEDULED (Fixes M5 & M6)
    UPDATE app.delivery_tasks SET payload_json = '{"edited":true}'::jsonb WHERE id = v_task1;
    UPDATE app.delivery_tasks SET status = 'SCHEDULED' WHERE id = v_task1;
    ASSERT (SELECT status FROM app.delivery_tasks WHERE id = v_task1) = 'SCHEDULED', 'Web PENDING->SCHEDULED failed';

    -- [C] Operator Plan Immutability (OC-01), Period-Bucket Rollover, Multi-Scope Caps, Idempotency & Fail-Closed
    -- 0. Tenant 2 creates a generous TENANT_BUDGET (5000), yet STILL fails closed with RF006 because it has NO OPERATOR_PLAN!
    PERFORM set_config('app.tenant_id', v_t2::text, true);
    INSERT INTO app.budget_policies (id, tenant_id, policy_origin, scope_type, scope_ref_id, period_kind, max_cost_micros)
    VALUES ('cccccccc-2222-2222-2222-222222222222', v_t2, 'TENANT_BUDGET', 'WORKSPACE', v_ws2, 'DAILY', 5000);

    v_caught := false;
    BEGIN
        PERFORM app.fn_budget_reserve('00000000-0000-0000-0000-0000000000f0', v_ws2, v_rt2, 'idem-nop', 10, 60);
    EXCEPTION WHEN SQLSTATE 'RF006' THEN
        v_caught := true;
    END;
    ASSERT v_caught, 'Expected RF006 fail-closed when tenant has TENANT_BUDGET but 0 active OPERATOR_PLAN quotas!';

    PERFORM set_config('app.tenant_id', v_t1::text, true);

    -- 1. Verify relayforge_web CANNOT raise, delete, or forge OPERATOR_PLAN ceiling (Fixes High #1 / OC-01 -> RF007!)
    v_caught := false;
    BEGIN
        UPDATE app.budget_policies SET max_cost_micros = 99999999 WHERE id = v_pol_tenant;
    EXCEPTION WHEN SQLSTATE 'RF007' THEN
        v_caught := true;
    END;
    ASSERT v_caught, 'relayforge_web was able to raise OPERATOR_PLAN max_cost_micros!';

    v_caught := false;
    BEGIN
        DELETE FROM app.budget_policies WHERE id = v_pol_tenant;
    EXCEPTION WHEN SQLSTATE 'RF007' THEN
        v_caught := true;
    END;
    ASSERT v_caught, 'relayforge_web was able to DELETE OPERATOR_PLAN policy!';

    v_caught := false;
    BEGIN
        INSERT INTO app.budget_policies (id, tenant_id, policy_origin, scope_type, scope_ref_id, period_kind, max_cost_micros)
        VALUES ('bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbb99', v_t1, 'OPERATOR_PLAN', 'TENANT', v_t1, 'DAILY', 99999999);
    EXCEPTION WHEN SQLSTATE 'RF007' THEN
        v_caught := true;
    END;
    ASSERT v_caught, 'relayforge_web was able to INSERT forged OPERATOR_PLAN policy!';

    -- 2. Cross-tenant / invalid route_id MUST raise RF002
    v_caught := false;
    BEGIN
        PERFORM app.fn_budget_reserve('00000000-0000-0000-0000-0000000000f1', v_ws1, v_rt2, 'idem-bad-rt', 10, 60);
    EXCEPTION WHEN SQLSTATE 'RF002' THEN
        v_caught := true;
    END;
    ASSERT v_caught, 'Expected RF002 on cross-tenant/invalid route_id!';

    -- 3. Server-derived ROUTE cap (250) rejects 260 even though WORKSPACE (400 today) & TENANT (600 left) allow it!
    v_caught := false;
    BEGIN
        PERFORM app.fn_budget_reserve('00000000-0000-0000-0000-0000000000f2', v_ws1, v_rt1, 'idem-rt-cap', 260, 60);
    EXCEPTION WHEN SQLSTATE 'RF001' THEN
        v_caught := true;
    END;
    ASSERT v_caught, 'Expected RF001 against server-derived ROUTE cap=250!';

    -- 4. Today's 300 workspace reserve SUCCEEDS despite yesterday's 400 COMMITTED in DAILY bucket (proves period_bucket rollover!)
    PERFORM app.fn_budget_reserve('00000000-0000-0000-0000-000000000001', v_ws1, NULL, 'idem-b1', 300, 10);

    -- 5. Idempotent replay with identical params returns existing ID; different amount raises RF005
    v_replay_id := app.fn_budget_reserve('00000000-0000-0000-0000-000000000099', v_ws1, NULL, 'idem-b1', 300, 10);
    ASSERT v_replay_id = '00000000-0000-0000-0000-000000000001'::uuid, 'Idempotent replay did not return original ID';

    v_caught := false;
    BEGIN
        PERFORM app.fn_budget_reserve('00000000-0000-0000-0000-000000000098', v_ws1, NULL, 'idem-b1', 100, 10);
    EXCEPTION WHEN SQLSTATE 'RF005' THEN
        v_caught := true;
    END;
    ASSERT v_caught, 'Expected RF005 on mismatched idempotency replay was NOT raised!';

    -- 6. Second reserve of 300 today MUST fail with RF001 against server-derived 400 workspace DAILY cap (Non-vacuous — Fixes S2!)
    v_caught := false;
    BEGIN
        PERFORM app.fn_budget_reserve('00000000-0000-0000-0000-000000000002', v_ws1, NULL, 'idem-b2', 300, 60);
    EXCEPTION WHEN SQLSTATE 'RF001' THEN
        v_caught := true;
    END;
    ASSERT v_caught, 'Expected RF001 on 300+300 > 400 workspace cap was NOT raised!';

    -- 7. NULL argument MUST raise 22004
    v_caught := false;
    BEGIN
        PERFORM app.fn_budget_reserve(NULL, v_ws1, NULL, 'idem-null', 10, 60);
    EXCEPTION WHEN SQLSTATE '22004' THEN
        v_caught := true;
    END;
    ASSERT v_caught, 'Expected 22004 on NULL argument was NOT raised!';

    -- [D] Worker Privilege Lockdown Tests (Fixes S3 & S4: No forged INSERT, no DELETE, no budget tampering)
    RESET ROLE;
    EXECUTE 'SET LOCAL ROLE relayforge_worker';
    PERFORM set_config('app.tenant_id', v_t1::text, true);

    v_caught := false;
    BEGIN
        UPDATE app.budget_policies SET max_cost_micros = 999999 WHERE id = v_pol_ws;
    EXCEPTION WHEN insufficient_privilege THEN
        v_caught := true;
    END;
    ASSERT v_caught, 'Worker was able to UPDATE budget_policies!';

    v_caught := false;
    BEGIN
        UPDATE app.budget_reservations SET state = 'RELEASED' WHERE id = '00000000-0000-0000-0000-000000000001';
    EXCEPTION WHEN insufficient_privilege THEN
        v_caught := true;
    END;
    ASSERT v_caught, 'Worker was able to directly UPDATE budget_reservations!';

    v_caught := false;
    BEGIN
        INSERT INTO app.delivery_tasks (id, tenant_id, workspace_id, idempotency_key, status, scheduled_for, lease_epoch)
        VALUES ('dddddddd-dddd-dddd-dddd-dddddddddd99', v_t1, v_ws1, 'forged', 'SENT', clock_timestamp(), 99);
    EXCEPTION WHEN SQLSTATE 'RF010' THEN
        v_caught := true;
    END;
    ASSERT v_caught, 'Worker was able to INSERT forged SENT/epoch=99 task!';

    v_caught := false;
    BEGIN
        DELETE FROM app.delivery_tasks WHERE id = v_task1;
    EXCEPTION WHEN insufficient_privilege OR SQLSTATE 'RF010' THEN
        v_caught := true;
    END;
    ASSERT v_caught, 'Worker was able to DELETE delivery_tasks!';

    -- [E] Multi-Tenant Fair Claim, Heartbeat, Stale/Missing Epoch Fencing & Reaper (All 3 Paths: UNCERTAIN, FAILED, READY)
    SELECT count(*) INTO v_rows FROM app.fn_worker_claim_next_jobs('w1', 10, 60);
    ASSERT v_rows = 2, 'Fair claim across Tenant 1 & Tenant 2 did not claim both runnable tasks!';

    -- Heartbeat lease renewal succeeds on Tenant 1's claimed task (epoch=1)
    v_hb_exp := app.fn_worker_heartbeat_lease(v_task1, 1, 120);
    ASSERT v_hb_exp > clock_timestamp() + interval '100 seconds', 'Heartbeat did not extend lease_expires_at';

    -- Missing epoch on direct UPDATE MUST fail with RF014
    PERFORM set_config('app.lease_epoch', '', true);
    v_caught := false;
    BEGIN
        UPDATE app.delivery_tasks SET status = 'DISPATCHING_IN_FLIGHT' WHERE id = v_task1;
    EXCEPTION WHEN SQLSTATE 'RF014' THEN
        v_caught := true;
    END;
    ASSERT v_caught, 'Direct worker UPDATE with empty app.lease_epoch was NOT rejected!';

    -- Stale epoch transition MUST fail with RF014
    v_caught := false;
    BEGIN
        PERFORM app.fn_worker_transition_task(v_task1, 999, 'DISPATCHING_IN_FLIGHT', NULL, NULL);
    EXCEPTION WHEN SQLSTATE 'RF014' THEN
        v_caught := true;
    END;
    ASSERT v_caught, 'Stale fencing epoch was NOT rejected!';

    -- Seed 3 expired tasks via migrator to test ALL 3 Reaper paths: -> UNCERTAIN, -> FAILED, -> READY (Fixes S1!)
    RESET ROLE;
    EXECUTE 'SET LOCAL ROLE relayforge_migrator';
    PERFORM set_config('app.migration_mode', 'on', true);

    INSERT INTO app.delivery_tasks (
        id, tenant_id, workspace_id, idempotency_key, status, scheduled_for,
        lease_owner, lease_expires_at, lease_epoch, attempts, max_attempts
    ) VALUES
    (v_task_reap_unc, v_t1, v_ws1, 'reap-unc', 'DISPATCHING_IN_FLIGHT', clock_timestamp() - interval '60 seconds',
     'dead-w1', clock_timestamp() - interval '5 seconds', 1, 10, 10),
    (v_task_reap_fail, v_t1, v_ws1, 'reap-fail', 'RESERVED', clock_timestamp() - interval '60 seconds',
     'dead-w2', clock_timestamp() - interval '5 seconds', 1, 4, 4),
    (v_task_reap_ready, v_t1, v_ws1, 'reap-ready', 'RESERVED', clock_timestamp() - interval '60 seconds',
     'dead-w3', clock_timestamp() - interval '5 seconds', 1, 1, 4);

    -- Expire reservation 1 to test M9b late-settle over-cap rejection
    UPDATE app.budget_reservations SET expires_at = clock_timestamp() - interval '1 second'
    WHERE id = '00000000-0000-0000-0000-000000000001';
    PERFORM set_config('app.migration_mode', '', true);

    RESET ROLE;
    EXECUTE 'SET LOCAL ROLE relayforge_worker';
    PERFORM set_config('app.tenant_id', v_t1::text, true);

    v_rows := app.fn_reap_expired_delivery_leases(50);
    ASSERT v_rows = 3, 'Reaper did not reap expected 3 expired tasks';
    ASSERT (SELECT status FROM app.delivery_tasks WHERE id = v_task_reap_unc) = 'UNCERTAIN', 'Reaper failed IN_FLIGHT -> UNCERTAIN';
    ASSERT (SELECT status FROM app.delivery_tasks WHERE id = v_task_reap_fail) = 'FAILED', 'Reaper failed max-attempts RESERVED -> FAILED';
    ASSERT (SELECT status FROM app.delivery_tasks WHERE id = v_task_reap_ready) = 'READY', 'Reaper failed retryable RESERVED -> READY';

    -- Test M9b: Consume 300 in new active hold, then attempt late settle of 300 on expired hold 1 (300+300 > 400 -> RF004!)
    PERFORM app.fn_budget_reserve('00000000-0000-0000-0000-000000000003', v_ws1, NULL, 'idem-b3', 300, 60);
    v_caught := false;
    BEGIN
        PERFORM app.fn_budget_settle('00000000-0000-0000-0000-000000000001', 'COMMITTED', 300);
    EXCEPTION WHEN SQLSTATE 'RF004' THEN
        v_caught := true;
    END;
    ASSERT v_caught, 'Expected RF004 on late COMMITTED settle breaching cap after expiry!';

    -- [F] Audited UNCERTAIN Requeue at attempts=10 -> Re-Claimable & Superuser Break-Glass Check (Fixes M7 & Minor #2)
    RESET ROLE;
    EXECUTE 'SET LOCAL ROLE relayforge_web';
    PERFORM set_config('app.tenant_id', v_t1::text, true);

    PERFORM app.fn_web_reconcile_uncertain_task(
        'eeeeeeee-eeee-eeee-eeee-eeeeeeeeeeee', v_task_reap_unc, 2,
        'CONFIRM_NOT_SENT_REQUEUE', true, 'owner-1', 'Verified missing in destination; approved manual requeue'
    );
    ASSERT (SELECT status FROM app.delivery_tasks WHERE id = v_task_reap_unc) = 'READY', 'Requeue from UNCERTAIN at attempts=10 failed';
    ASSERT (SELECT max_attempts FROM app.delivery_tasks WHERE id = v_task_reap_unc) = 11, 'max_attempts did not increment to 11 on requeue';

    -- Verify worker can now re-claim both reap_ready AND the requeued reap_unc task!
    RESET ROLE;
    EXECUTE 'SET LOCAL ROLE relayforge_worker';
    SELECT count(*) INTO v_rows FROM app.fn_worker_claim_next_jobs('w2', 10, 60);
    ASSERT v_rows = 2, 'Worker failed to re-claim requeued UNCERTAIN task (attempts=10, max_attempts=11) + reap_ready!';

    -- Verify superuser break-glass guard: blocked without reason (RF020), allowed with reason
    RESET ROLE;
    PERFORM set_config('app.break_glass_reason', '', true);
    v_caught := false;
    BEGIN
        UPDATE app.delivery_tasks SET payload_json = '{"bg":1}'::jsonb WHERE id = v_task1;
    EXCEPTION WHEN SQLSTATE 'RF020' THEN
        v_caught := true;
    END;
    ASSERT v_caught, 'Superuser UPDATE without app.break_glass_reason was NOT blocked!';

    PERFORM set_config('app.break_glass_reason', 'INC-2026-SMOKE-VERIFY', true);
    UPDATE app.delivery_tasks SET payload_json = '{"bg":2}'::jsonb WHERE id = v_task1;
    PERFORM set_config('app.break_glass_reason', '', true);

    RAISE NOTICE 'ALL G0 v1.3.2 NON-VACUOUS BEHAVIORAL & SECURITY ASSERTIONS PASSED!';
END;
$$;
