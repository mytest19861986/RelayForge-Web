CREATE POLICY operator_tenant_directory ON app.tenants
    FOR SELECT TO relayforge_operator USING (true);
CREATE POLICY operator_plan_directory ON app.budget_policies
    FOR SELECT TO relayforge_operator USING (policy_origin = 'OPERATOR_PLAN');
GRANT SELECT ON app.tenants, app.budget_policies TO relayforge_operator;
