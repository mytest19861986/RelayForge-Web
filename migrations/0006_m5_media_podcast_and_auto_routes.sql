ALTER TABLE app.podcast_scripts ADD COLUMN segment_cache_json jsonb NOT NULL DEFAULT '{}';
ALTER TABLE app.media_jobs ADD COLUMN cancel_requested boolean NOT NULL DEFAULT false;
ALTER TABLE app.media_jobs ADD COLUMN remote_operation_id text;
ALTER TABLE app.media_jobs ADD COLUMN request_sha256 text;
CREATE INDEX media_jobs_pending ON app.media_jobs (created_at, id) WHERE status = 'PENDING';
CREATE INDEX media_asset_lookup ON app.media_assets (tenant_id, workspace_id, sha256_hex)
    WHERE NOT is_deleted;

GRANT SELECT (id, tenant_id, workspace_id, job_kind, input_spec_json, reservation_id,
              status, created_at, cancel_requested), UPDATE (status)
    ON app.media_jobs TO relayforge_dispatcher_owner;
CREATE POLICY dispatcher_media_select ON app.media_jobs FOR SELECT TO relayforge_dispatcher_owner
    USING (status IN ('PENDING', 'RUNNING'));
CREATE POLICY dispatcher_media_update ON app.media_jobs FOR UPDATE TO relayforge_dispatcher_owner
    USING (status = 'PENDING' AND NOT cancel_requested)
    WITH CHECK (status = 'RUNNING' AND NOT cancel_requested);

CREATE FUNCTION app.fn_worker_claim_media_job()
RETURNS TABLE (job_id uuid, tenant_id uuid, workspace_id uuid, job_kind text,
               input_spec_json jsonb, reservation_id uuid)
LANGUAGE sql SECURITY DEFINER SET search_path = pg_catalog, pg_temp AS $$
    WITH selected AS (
        SELECT j.id FROM app.media_jobs AS j
        JOIN app.tenants AS t ON t.id = j.tenant_id AND t.status = 'ACTIVE'
        WHERE j.status = 'PENDING' AND NOT j.cancel_requested
        ORDER BY j.created_at, j.id LIMIT 1 FOR UPDATE OF j SKIP LOCKED
    )
    UPDATE app.media_jobs AS j SET status = 'RUNNING'
    FROM selected AS s WHERE j.id = s.id
    RETURNING j.id, j.tenant_id, j.workspace_id, j.job_kind, j.input_spec_json, j.reservation_id;
$$;
ALTER FUNCTION app.fn_worker_claim_media_job() OWNER TO relayforge_dispatcher_owner;
REVOKE ALL ON FUNCTION app.fn_worker_claim_media_job() FROM PUBLIC;
GRANT EXECUTE ON FUNCTION app.fn_worker_claim_media_job() TO relayforge_worker;

ALTER TABLE app.ai_model_cache ADD COLUMN supported_resolutions_json jsonb NOT NULL DEFAULT '["1K"]';
