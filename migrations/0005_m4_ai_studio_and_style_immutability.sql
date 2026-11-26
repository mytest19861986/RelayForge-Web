CREATE TRIGGER trg_protect_style_revisions
BEFORE UPDATE OR DELETE ON app.style_revisions
FOR EACH ROW EXECUTE FUNCTION app.trg_fn_protect_immutable_publications();

GRANT INSERT, UPDATE ON app.ai_executions TO relayforge_web;
GRANT SELECT ON app.budget_reservations TO relayforge_web;
GRANT SELECT, INSERT, UPDATE, DELETE ON app.ai_model_cache TO relayforge_web;
ALTER TABLE app.feedback_events ADD COLUMN destination_id uuid;
ALTER TABLE app.feedback_events ADD FOREIGN KEY (tenant_id, destination_id)
    REFERENCES app.destinations (tenant_id, id) ON DELETE CASCADE;
ALTER TABLE app.style_profiles ADD COLUMN package_slug text;
ALTER TABLE app.style_profiles ADD COLUMN package_tags jsonb NOT NULL DEFAULT '[]';
ALTER TABLE app.style_revisions ADD COLUMN statistical_evidence_json jsonb NOT NULL DEFAULT '{}';
ALTER TABLE app.generation_sessions ADD COLUMN request_sha256 text;
ALTER TABLE app.generation_sessions ADD COLUMN completed_at timestamptz;
ALTER TABLE app.generation_sessions ADD COLUMN result_json jsonb;

CREATE FUNCTION app.fn_budget_settle_with_accuracy(
    p_reservation_id uuid, p_final_state text, p_settled_micros bigint, p_cost_accuracy text
) RETURNS void LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, pg_temp AS $$
BEGIN
    IF p_cost_accuracy IS NULL OR
       p_cost_accuracy NOT IN ('ACTUAL_PROVIDER', 'ESTIMATED', 'UNKNOWN') THEN
        RAISE EXCEPTION 'INVALID_COST_ACCURACY' USING ERRCODE = '22023';
    END IF;
    PERFORM app.fn_budget_settle(p_reservation_id, p_final_state, p_settled_micros);
    UPDATE app.budget_reservations SET cost_accuracy = p_cost_accuracy
    WHERE id = p_reservation_id;
END;
$$;
ALTER FUNCTION app.fn_budget_settle_with_accuracy(uuid,text,bigint,text)
    OWNER TO relayforge_ledger_owner;
REVOKE ALL ON FUNCTION app.fn_budget_settle_with_accuracy(uuid,text,bigint,text) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION app.fn_budget_settle_with_accuracy(uuid,text,bigint,text)
    TO relayforge_web, relayforge_worker;
