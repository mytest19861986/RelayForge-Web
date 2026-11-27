import shutil
import subprocess

import psycopg
from django.test import Client
from test_m1_auth_rls_vault_and_shredding import OPERATOR, expect, login, provision, request

from relayforge.web.studio_ui import FILES


def test_rtl_routes_and_static_security(db):
    client = Client()
    for slug in (
        "", "operator", "app/dashboard", "app/sources", "app/destinations", "app/routes",
        "app/content", "app/intelligence", "app/styles", "app/media", "app/ai",
        "app/drafts", "app/delivery", "app/operations",
    ):
        response = client.get("/" + slug)
        assert response.status_code == 200
        html = b"".join(response.streaming_content)
        assert b'<html lang="fa" dir="rtl">' in html
        assert html.count(b"<script") == html.count(b'<script type="module" src=') == 1
        for directive in ("default-src 'self'", "frame-ancestors 'none'", "object-src 'none'",
                          "base-uri 'none'"):
            assert directive in response["Content-Security-Policy"]
    for file in FILES:
        asset = client.get(f"/static/relayforge_ui/{file}")
        assert asset.status_code == 200
        content = b"".join(asset.streaming_content)
        assert b"localStorage" not in content and b"sessionStorage" not in content
        assert b"innerHTML" not in content
        if file.endswith(".js") and (node := shutil.which("node")):
            subprocess.run([node, "--input-type=module", "--check"], input=content, check=True)
    assert client.get("/static/relayforge_ui/unknown.js").status_code == 404


def test_operator_directory_auth_search_and_workspace_lookup(db):
    client = Client()
    path = "/operator/tenants"
    assert request(client, "GET", path).status_code in (401, 403)
    assert request(client, "GET", path, HTTP_AUTHORIZATION="Bearer invalid").status_code in (
        401, 403,
    )
    a = provision("m8-directory-one")
    provision("m8-directory-two")
    update = {"daily_max_cost_micros": 3100000, "monthly_max_cost_micros": 32000000}
    expect(request(client, "PUT", f"{path}/{a}/plan-quotas", update, **OPERATOR), 200)
    found = expect(request(client, "GET", path + "?q=directory-one", **OPERATOR), 200)["items"]
    assert len(found) == 1 and found[0]["id"] == a
    assert all(found[0][key] == value for key, value in update.items())
    page = expect(request(client, "GET", path + "?q=m8-directory&limit=1", **OPERATOR), 200)
    next_page = expect(request(client, "GET", path + "?q=m8-directory&limit=1&cursor="
                               + page["page_info"]["next_cursor"], **OPERATOR), 200)
    assert page["items"][0]["id"] != next_page["items"][0]["id"]
    a_session = login("m8-directory-one", REMOTE_ADDR="192.0.2.151")
    b_session = login("m8-directory-two", REMOTE_ADDR="192.0.2.152")
    from test_m1_auth_rls_vault_and_shredding import authorized

    ws = a_session["default_workspace_id"]
    for route in ("channel-intelligence/analyses", "media/jobs", "podcast-scripts", "backups"):
        endpoint = f"/workspaces/{ws}/{route}"
        assert expect(authorized(a_session, "GET", endpoint), 200)["items"] == []
        expect(authorized(b_session, "GET", endpoint), 404)
    dashboard = expect(authorized(a_session, "GET", f"/workspaces/{ws}/dashboard"), 200)
    assert dashboard["disk_total_bytes"] > 0 and dashboard["pitr_retention_days"] == 14
    with psycopg.connect(db) as conn:
        enabled, forced = conn.execute("""
            SELECT count(*) FILTER(WHERE relrowsecurity),count(*) FILTER(WHERE relforcerowsecurity)
            FROM pg_class WHERE relnamespace='app'::regnamespace AND relkind='r'
        """).fetchone()
        assert enabled == forced == 51
        assert conn.execute(
            "SELECT count(*) FROM pg_roles WHERE rolbypassrls AND NOT rolsuper"
        ).fetchone()[0] == 0
