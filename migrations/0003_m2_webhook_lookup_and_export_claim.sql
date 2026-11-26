ALTER TABLE app.sources ADD COLUMN webhook_token_sha256 text UNIQUE;
ALTER TABLE app.sources ADD COLUMN webhook_platform text
    CHECK (webhook_platform IN ('telegram', 'bale'));
ALTER TABLE app.export_import_jobs ADD COLUMN s3_object_etag text;
ALTER TABLE app.destinations ADD COLUMN is_archived boolean NOT NULL DEFAULT false;

GRANT SELECT (id, tenant_id, workspace_id, canonical_channel_id, secret_id, source_kind,
              webhook_platform, webhook_token_sha256, is_active)
    ON app.sources TO relayforge_auth_owner;
CREATE POLICY auth_webhook_sources ON app.sources FOR SELECT TO relayforge_auth_owner
    USING (is_active AND source_kind = 'WEBHOOK_INBOUND' AND webhook_token_sha256 IS NOT NULL);

CREATE FUNCTION app.fn_resolve_inbound_webhook_source(p_platform text, p_token_hash text)
RETURNS TABLE (tenant_id uuid, workspace_id uuid, source_id uuid, canonical_channel_id text,
               secret_id uuid)
LANGUAGE sql STABLE SECURITY DEFINER SET search_path = pg_catalog, pg_temp AS $$
    SELECT s.tenant_id, s.workspace_id, s.id, s.canonical_channel_id, s.secret_id
    FROM app.sources s
    JOIN app.tenants t ON t.id = s.tenant_id AND t.status = 'ACTIVE'
    WHERE s.source_kind = 'WEBHOOK_INBOUND' AND s.is_active
      AND s.webhook_platform = p_platform AND s.webhook_token_sha256 = p_token_hash;
$$;
ALTER FUNCTION app.fn_resolve_inbound_webhook_source(text, text) OWNER TO relayforge_auth_owner;
REVOKE ALL ON FUNCTION app.fn_resolve_inbound_webhook_source(text, text) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION app.fn_resolve_inbound_webhook_source(text, text) TO relayforge_web;

GRANT SELECT (id, tenant_id, workspace_id, source_id, s3_object_key, file_size_bytes, job_mode,
              status, s3_object_etag, raw_deleted_at, created_at, completed_at), UPDATE (status)
    ON app.export_import_jobs TO relayforge_dispatcher_owner;
CREATE POLICY dispatcher_export_ready ON app.export_import_jobs
    FOR SELECT TO relayforge_dispatcher_owner
    USING (status = 'PARSING' OR (raw_deleted_at IS NULL AND (
        status IN ('COMPLETED', 'FAILED', 'EXPIRED_CLEANED')
        OR (status = 'UPLOADING' AND created_at <= clock_timestamp() - interval '24 hours')
    )));
-- UPDATE policy is needed for SELECT ... FOR UPDATE, even though claim only locks rows.
CREATE POLICY dispatcher_export_claim ON app.export_import_jobs
    FOR UPDATE TO relayforge_dispatcher_owner
    USING (status = 'PARSING' OR (raw_deleted_at IS NULL AND (
        status IN ('COMPLETED', 'FAILED', 'EXPIRED_CLEANED')
        OR (status = 'UPLOADING' AND created_at <= clock_timestamp() - interval '24 hours')
    )))
    WITH CHECK (false);

CREATE FUNCTION app.fn_worker_claim_export_job()
RETURNS TABLE (job_id uuid, tenant_id uuid, workspace_id uuid, source_id uuid, s3_object_key text,
               file_size_bytes bigint, job_mode text, s3_object_etag text)
LANGUAGE sql SECURITY DEFINER SET search_path = pg_catalog, pg_temp AS $$
    SELECT j.id, j.tenant_id, j.workspace_id, j.source_id, j.s3_object_key, j.file_size_bytes,
           j.job_mode, j.s3_object_etag
    FROM app.export_import_jobs j
    WHERE j.status = 'PARSING'
    ORDER BY j.created_at, j.id LIMIT 1 FOR UPDATE SKIP LOCKED;
$$;
ALTER FUNCTION app.fn_worker_claim_export_job() OWNER TO relayforge_dispatcher_owner;
REVOKE ALL ON FUNCTION app.fn_worker_claim_export_job() FROM PUBLIC;
GRANT EXECUTE ON FUNCTION app.fn_worker_claim_export_job() TO relayforge_worker;

CREATE FUNCTION app.fn_worker_claim_export_cleanup()
RETURNS TABLE (job_id uuid, tenant_id uuid, s3_object_key text)
LANGUAGE sql SECURITY DEFINER SET search_path = pg_catalog, pg_temp AS $$
    SELECT j.id, j.tenant_id, j.s3_object_key
    FROM app.export_import_jobs j
    WHERE j.raw_deleted_at IS NULL
      AND j.s3_object_key = 'tenants/' || j.tenant_id || '/exports/' || j.id || '.json'
      AND (
        j.status IN ('COMPLETED', 'FAILED', 'EXPIRED_CLEANED')
        OR (j.status = 'UPLOADING' AND j.created_at <= clock_timestamp() - interval '24 hours')
    )
    ORDER BY j.created_at, j.id LIMIT 1 FOR UPDATE SKIP LOCKED;
$$;
ALTER FUNCTION app.fn_worker_claim_export_cleanup() OWNER TO relayforge_dispatcher_owner;
REVOKE ALL ON FUNCTION app.fn_worker_claim_export_cleanup() FROM PUBLIC;
GRANT EXECUTE ON FUNCTION app.fn_worker_claim_export_cleanup() TO relayforge_worker;
