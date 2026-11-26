DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_catalog.pg_roles WHERE rolname = 'relayforge_auth_owner') THEN
        CREATE ROLE relayforge_auth_owner NOLOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOBYPASSRLS;
    END IF;
END;
$$;

GRANT USAGE ON SCHEMA app TO relayforge_auth_owner;
GRANT SELECT (id, email, password_hash_argon2id, is_active) ON app.users TO relayforge_auth_owner;
GRANT SELECT (id, slug, status) ON app.tenants TO relayforge_auth_owner;
GRANT SELECT (tenant_id, user_id, role, is_active) ON app.tenant_memberships TO relayforge_auth_owner;
GRANT SELECT (id, tenant_id, slug, is_archived, created_at) ON app.workspaces TO relayforge_auth_owner;
GRANT SELECT (id, tenant_id, user_id, token_hash_sha256, revoked_at, expires_at, csrf_token_hash_sha256)
    ON app.web_sessions TO relayforge_auth_owner;
CREATE POLICY auth_users ON app.users FOR SELECT TO relayforge_auth_owner USING (true);
CREATE POLICY auth_tenants ON app.tenants FOR SELECT TO relayforge_auth_owner USING (true);
CREATE POLICY auth_memberships ON app.tenant_memberships FOR SELECT TO relayforge_auth_owner USING (true);
CREATE POLICY auth_workspaces ON app.workspaces FOR SELECT TO relayforge_auth_owner USING (true);
CREATE POLICY auth_sessions ON app.web_sessions FOR SELECT TO relayforge_auth_owner USING (true);

CREATE FUNCTION app.fn_auth_lookup_login_candidate(p_email text, p_tenant_slug text DEFAULT NULL)
RETURNS TABLE (user_id uuid, password_hash_argon2id text, tenant_id uuid, tenant_slug text,
               role text, default_workspace_id uuid)
LANGUAGE plpgsql STABLE SECURITY DEFINER SET search_path = pg_catalog, pg_temp AS $$
BEGIN
    RETURN QUERY
    SELECT u.id, u.password_hash_argon2id, t.id, t.slug, tm.role,
           (SELECT w.id FROM app.workspaces w WHERE w.tenant_id = t.id AND NOT w.is_archived
            ORDER BY (w.slug = 'default') DESC, w.created_at, w.id LIMIT 1)
    FROM app.users u
    JOIN app.tenant_memberships tm ON tm.user_id = u.id AND tm.is_active
    JOIN app.tenants t ON t.id = tm.tenant_id AND t.status = 'ACTIVE'
    WHERE u.email = lower(trim(p_email)) AND u.is_active
      AND (p_tenant_slug IS NULL OR t.slug = p_tenant_slug)
    LIMIT 2;
    IF p_tenant_slug IS NULL AND (SELECT count(*) FROM (
        SELECT 1 FROM app.users u
        JOIN app.tenant_memberships tm ON tm.user_id = u.id AND tm.is_active
        JOIN app.tenants t ON t.id = tm.tenant_id AND t.status = 'ACTIVE'
        WHERE u.email = lower(trim(p_email)) AND u.is_active LIMIT 2
    ) AS matches) > 1 THEN
        RAISE EXCEPTION 'AMBIGUOUS_TENANT_SLUG_REQUIRED' USING ERRCODE = 'RF022';
    END IF;
END;
$$;
ALTER FUNCTION app.fn_auth_lookup_login_candidate(text, text) OWNER TO relayforge_auth_owner;
REVOKE ALL ON FUNCTION app.fn_auth_lookup_login_candidate(text, text) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION app.fn_auth_lookup_login_candidate(text, text) TO relayforge_web;

CREATE FUNCTION app.fn_auth_resolve_session(p_token_hash_sha256 text)
RETURNS TABLE (session_id uuid, tenant_id uuid, user_id uuid, role text, csrf_token_hash_sha256 text)
LANGUAGE sql SECURITY DEFINER SET search_path = pg_catalog, pg_temp AS $$
    SELECT ws.id, ws.tenant_id, ws.user_id, tm.role, ws.csrf_token_hash_sha256
    FROM app.web_sessions ws
    JOIN app.tenants t ON t.id = ws.tenant_id AND t.status = 'ACTIVE'
    JOIN app.users u ON u.id = ws.user_id AND u.is_active
    JOIN app.tenant_memberships tm ON tm.tenant_id = ws.tenant_id
        AND tm.user_id = ws.user_id AND tm.is_active
    WHERE ws.token_hash_sha256 = p_token_hash_sha256
      AND ws.revoked_at IS NULL AND ws.expires_at > clock_timestamp();
$$;
ALTER FUNCTION app.fn_auth_resolve_session(text) OWNER TO relayforge_auth_owner;
REVOKE ALL ON FUNCTION app.fn_auth_resolve_session(text) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION app.fn_auth_resolve_session(text) TO relayforge_web;

GRANT INSERT, SELECT, UPDATE ON app.workspaces, app.workspace_settings TO relayforge_operator;
GRANT SELECT (tenant_id, is_revoked), UPDATE (is_revoked) ON app.secrets_vault TO relayforge_operator;
GRANT SELECT (tenant_id, revoked_at), UPDATE (revoked_at) ON app.web_sessions TO relayforge_operator;

CREATE FUNCTION app.fn_owner_crypto_shred_tenant(p_confirmation_slug text, p_correlation_id text)
RETURNS void LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, pg_temp AS $$
DECLARE
    v_tenant_id uuid := NULLIF(current_setting('app.tenant_id', true), '')::uuid;
    v_user_id uuid := NULLIF(current_setting('app.user_id', true), '')::uuid;
    v_now timestamptz;
BEGIN
    IF v_tenant_id IS NULL OR v_user_id IS NULL THEN
        RAISE EXCEPTION 'OWNER_MEMBERSHIP_REQUIRED' USING ERRCODE = '42501';
    END IF;
    PERFORM pg_advisory_xact_lock(hashtextextended(v_tenant_id::text, 1));
    IF NOT EXISTS (
        SELECT 1 FROM app.tenant_memberships tm JOIN app.users u ON u.id = tm.user_id
        WHERE tm.tenant_id = v_tenant_id AND tm.user_id = v_user_id
          AND tm.role = 'OWNER' AND tm.is_active AND u.is_active
    ) THEN
        RAISE EXCEPTION 'OWNER_MEMBERSHIP_REQUIRED' USING ERRCODE = '42501';
    END IF;
    IF NOT EXISTS (
        SELECT 1 FROM app.tenants WHERE id = v_tenant_id AND slug = p_confirmation_slug
            AND status = 'ACTIVE'
    ) THEN
        RAISE EXCEPTION 'CONFIRMATION_SLUG_MISMATCH' USING ERRCODE = '22023';
    END IF;
    v_now := clock_timestamp();
    UPDATE app.tenant_encryption_keys SET key_state = 'SHREDDED', wrapped_dek_ciphertext = NULL,
        shredded_at = v_now WHERE tenant_id = v_tenant_id AND key_state <> 'SHREDDED';
    UPDATE app.secrets_vault SET is_revoked = true WHERE tenant_id = v_tenant_id;
    UPDATE app.web_sessions SET revoked_at = COALESCE(revoked_at, v_now)
        WHERE tenant_id = v_tenant_id;
    UPDATE app.workspaces SET emergency_stop = true WHERE tenant_id = v_tenant_id;
    UPDATE app.tenants SET status = 'PENDING_DELETION', updated_at = v_now WHERE id = v_tenant_id;
    INSERT INTO app.audit_events (id, tenant_id, actor_user_id, actor_role, action_code,
        target_type, target_id, correlation_id, details_safe_json)
    VALUES (gen_random_uuid(), v_tenant_id, v_user_id, 'OWNER', 'TENANT_CRYPTO_SHREDDED',
        'TENANT', v_tenant_id::text, p_correlation_id, '{"pitr_retention_disclosure_days":14}');
END;
$$;
ALTER FUNCTION app.fn_owner_crypto_shred_tenant(text, text) OWNER TO relayforge_operator;
REVOKE ALL ON FUNCTION app.fn_owner_crypto_shred_tenant(text, text) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION app.fn_owner_crypto_shred_tenant(text, text) TO relayforge_web;
