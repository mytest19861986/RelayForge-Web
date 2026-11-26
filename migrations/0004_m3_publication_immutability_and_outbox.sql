CREATE FUNCTION app.trg_fn_protect_immutable_publications()
RETURNS trigger LANGUAGE plpgsql SET search_path = pg_catalog, pg_temp AS $$
BEGIN
    IF CURRENT_USER = 'relayforge_migrator' AND current_setting('app.migration_mode', true) = 'on' THEN
        IF TG_OP = 'DELETE' THEN RETURN OLD; ELSE RETURN NEW; END IF;
    END IF;
    RAISE EXCEPTION 'PUBLICATION_VERSIONS_ARE_IMMUTABLE' USING ERRCODE = 'RF023';
END;
$$;

CREATE TRIGGER trg_protect_immutable_publication_versions
BEFORE UPDATE OR DELETE ON app.publication_versions
FOR EACH ROW EXECUTE FUNCTION app.trg_fn_protect_immutable_publications();

CREATE TRIGGER trg_protect_route_policy_revisions
BEFORE UPDATE OR DELETE ON app.route_policy_revisions
FOR EACH ROW EXECUTE FUNCTION app.trg_fn_protect_immutable_publications();

GRANT INSERT ON app.publication_versions TO relayforge_worker;
GRANT SELECT (status) ON app.tenants TO relayforge_dispatcher_owner;
GRANT SELECT (id, tenant_id, emergency_stop) ON app.workspaces TO relayforge_dispatcher_owner;
GRANT SELECT (id, tenant_id, workspace_id, is_enabled, is_archived) ON app.destinations
    TO relayforge_dispatcher_owner;
GRANT SELECT (workspace_id, destination_id) ON app.delivery_tasks TO relayforge_dispatcher_owner;
CREATE POLICY dispatcher_workspace_enabled ON app.workspaces FOR SELECT
    TO relayforge_dispatcher_owner USING (NOT emergency_stop);
CREATE POLICY dispatcher_destination_enabled ON app.destinations FOR SELECT
    TO relayforge_dispatcher_owner USING (is_enabled AND NOT is_archived);

ALTER TABLE app.drafts ADD COLUMN route_policy_revision_no int;
ALTER TABLE app.draft_revisions ADD COLUMN media_manifest_json jsonb NOT NULL DEFAULT '[]';

CREATE INDEX m3_policy_dedup_idx ON app.content_fingerprints (tenant_id, route_id, created_at);
CREATE INDEX m3_poll_idx ON app.domain_outbox (tenant_id, workspace_id, created_at, id);

CREATE FUNCTION app.trg_fn_record_reaped_uncertain()
RETURNS trigger LANGUAGE plpgsql SECURITY DEFINER
SET search_path = pg_catalog, pg_temp SET app.tenant_id = '' AS $$
DECLARE
    v_correlation text := COALESCE(OLD.payload_json->>'correlation_id', OLD.id::text);
BEGIN
    PERFORM set_config('app.tenant_id', OLD.tenant_id::text, true);
    INSERT INTO app.delivery_attempts (id, tenant_id, delivery_task_id, attempt_no,
        lease_epoch, outcome, error_safe, started_at, finished_at)
    VALUES (gen_random_uuid(), OLD.tenant_id, OLD.id, OLD.attempts, OLD.lease_epoch,
        'TIMEOUT_UNCERTAIN', 'LEASE_EXPIRED_IN_FLIGHT', OLD.updated_at, clock_timestamp())
    ON CONFLICT DO NOTHING;
    PERFORM pg_advisory_xact_lock(hashtextextended(OLD.workspace_id::text, 4));
    INSERT INTO app.domain_outbox (id, tenant_id, workspace_id, event_type, correlation_id,
        payload_json)
    VALUES (gen_random_uuid(), OLD.tenant_id, OLD.workspace_id, 'delivery.marked_uncertain',
        v_correlation, jsonb_build_object('delivery_task_id', OLD.id, 'lease_epoch', NEW.lease_epoch));
    RETURN NEW;
END;
$$;
ALTER FUNCTION app.trg_fn_record_reaped_uncertain() OWNER TO relayforge_ledger_owner;
REVOKE ALL ON FUNCTION app.trg_fn_record_reaped_uncertain() FROM PUBLIC;
GRANT INSERT ON app.delivery_attempts, app.domain_outbox TO relayforge_ledger_owner;

CREATE TRIGGER trg_record_reaped_uncertain
AFTER UPDATE ON app.delivery_tasks
FOR EACH ROW WHEN (
    OLD.status = 'DISPATCHING_IN_FLIGHT' AND NEW.status = 'UNCERTAIN'
    AND OLD.lease_epoch < NEW.lease_epoch
)
EXECUTE FUNCTION app.trg_fn_record_reaped_uncertain();

-- Keep the existing water-filling algorithm and exclude paused targets at claim time.
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
            WHERE t.status = 'ACTIVE' AND NOT (t.id = ANY(v_exhausted))
              AND EXISTS (
                  SELECT 1 FROM app.delivery_tasks dt
                  WHERE dt.tenant_id = t.id
                    AND dt.status IN ('READY', 'SCHEDULED', 'RETRY_WAIT')
                    AND dt.runnable_at <= v_now
                    AND dt.attempts < dt.max_attempts
                    AND EXISTS (
                        SELECT 1 FROM app.workspaces w JOIN app.destinations d
                          ON d.workspace_id = w.id AND d.tenant_id = w.tenant_id
                        WHERE w.id = dt.workspace_id AND w.tenant_id = dt.tenant_id
                          AND d.id = dt.destination_id
                          AND NOT w.emergency_stop AND d.is_enabled AND NOT d.is_archived
                    )
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
                    AND EXISTS (
                        SELECT 1 FROM app.workspaces w JOIN app.destinations d
                          ON d.workspace_id = w.id AND d.tenant_id = w.tenant_id
                        WHERE w.id = dt.workspace_id AND w.tenant_id = dt.tenant_id
                          AND d.id = dt.destination_id
                          AND NOT w.emergency_stop AND d.is_enabled AND NOT d.is_archived
                    )
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
