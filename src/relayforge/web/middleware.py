import hashlib
import hmac
import time
from collections import OrderedDict
from threading import Lock
from uuid import UUID, uuid4

from django.conf import settings
from django.core.exceptions import DisallowedHost
from django.db import DatabaseError, transaction

from relayforge.web.db import first, lock_tenant, set_context
from relayforge.web.problems import PROBLEMS, problem

API = "/api/v1"
MUTATING = {"POST", "PUT", "PATCH", "DELETE"}
COOKIE = "__Host-rf_session"
WINDOW = 60
BUCKET = 10
SESSION_LIMIT = 180
LOGIN_LIMIT = 20
_login_buckets = OrderedDict()
_login_lock = Lock()


def sha256(value):
    return hashlib.sha256(value.encode()).hexdigest()


def cookie_name(request):
    if (
        not request.is_secure()
        and settings.ALLOW_INSECURE_LOCAL_COOKIE
        and request.get_host().partition(":")[0] == "127.0.0.1"
        and request.META.get("REMOTE_ADDR") == "127.0.0.1"
    ):
        return "rf_session"
    return COOKIE


def login_throttled(address):
    now = time.monotonic()
    with _login_lock:
        while _login_buckets and next(iter(_login_buckets.values()))[0] <= now:
            _login_buckets.popitem(last=False)
        expires, hits = _login_buckets.get(address, (now + WINDOW, 0))
        if address not in _login_buckets and len(_login_buckets) >= 4096:
            return True
        _login_buckets[address] = expires, hits + 1
        return hits >= LOGIN_LIMIT


def allowed_origin(request):
    origin = request.headers.get("Origin")
    return origin is None or origin in settings.ALLOWED_ORIGINS or (
        origin == f"{request.scheme}://{request.get_host()}"
    )


class SecurityMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        try:
            cid = str(UUID(request.headers.get("X-Correlation-ID", "")))
        except ValueError:
            cid = str(uuid4())
        request.correlation_id = cid
        try:
            request.get_host()
        except DisallowedHost:
            response = problem(request, 400)
        else:
            response = self.get_response(request)
        response["X-Correlation-ID"] = cid
        response["Content-Security-Policy"] = "default-src 'self'; frame-ancestors 'none'"
        response["X-Content-Type-Options"] = "nosniff"
        response["Referrer-Policy"] = "same-origin"
        if request.path.startswith(API + "/"):
            if response.status_code in PROBLEMS and not response["Content-Type"].startswith(
                "application/json"
            ):
                allow = response.get("Allow")
                headers = dict(response.headers)
                response = problem(request, response.status_code)
                for name, value in headers.items():
                    if name.lower() not in {"content-type", "content-length"}:
                        response[name] = value
                if allow:
                    response["Allow"] = allow
            response["Cache-Control"] = "no-store"
        return response


def session_rate_limit(session_id, tenant_id):
    now = int(time.time())
    bucket = now - now % BUCKET
    key = f"session:{session_id}"
    return first("""
        WITH bump AS (
            INSERT INTO app.rate_limit_counters (tenant_id, bucket_key, window_epoch_sec)
            VALUES (%s, %s, %s)
            ON CONFLICT (tenant_id, bucket_key, window_epoch_sec)
            DO UPDATE SET hit_count = app.rate_limit_counters.hit_count + 1
            RETURNING hit_count
        ), expired AS (
            DELETE FROM app.rate_limit_counters
            WHERE tenant_id = %s AND bucket_key = %s AND window_epoch_sec < %s
        )
        SELECT (SELECT hit_count FROM bump) + COALESCE(SUM(hit_count), 0) AS total
        FROM app.rate_limit_counters
        WHERE tenant_id = %s AND bucket_key = %s AND window_epoch_sec >= %s
          AND window_epoch_sec < %s
    """, (tenant_id, key, bucket, tenant_id, key, now - WINDOW,
          tenant_id, key, now - WINDOW, bucket))["total"] > SESSION_LIMIT


class TenantRLSMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        path = request.path_info
        if path == API + "/auth/login" and request.method == "POST":
            if not allowed_origin(request):
                return problem(request, 403)
            if login_throttled(request.META.get("REMOTE_ADDR", "")):
                return problem(request, 429, retry_after=WINDOW)
        if (
            not path.startswith(API + "/")
            or path in {API + "/health", API + "/auth/login"}
            or path.startswith(API + "/operator/")
            or path.startswith(API + "/webhooks/inbound/")
        ):
            return self.get_response(request)

        token = request.COOKIES.get(cookie_name(request), "")
        if not token or len(token) > 128:
            return problem(request, 401)
        try:
            token_hash = sha256(token)
            with transaction.atomic(using="default"):
                session = first("SELECT * FROM app.fn_auth_resolve_session(%s)", (token_hash,))
                if not session:
                    return problem(request, 401)
                set_context(session["tenant_id"], session["user_id"])
                limited = session_rate_limit(session["session_id"], session["tenant_id"])
            if limited:
                return problem(request, 429, retry_after=WINDOW)
            with transaction.atomic(using="default"):
                return self._authenticated(request, token_hash)
        except DatabaseError:
            return problem(request, 503)

    def _authenticated(self, request, token_hash):
        resolve = "SELECT * FROM app.fn_auth_resolve_session(%s)"
        session = first(resolve, (token_hash,))
        if not session:
            return problem(request, 401)

        # Coordinate with shredding and recheck the session after any wait.
        lock_tenant(
            session["tenant_id"],
            exclusive=request.path_info == API + "/tenants/current/delete-account",
        )
        session = first(resolve, (token_hash,))
        if not session:
            return problem(request, 401)
        request.tenant = session
        set_context(session["tenant_id"], session["user_id"])

        if request.method in MUTATING:
            csrf = request.headers.get("X-CSRFToken", "")
            if not csrf or not hmac.compare_digest(
                sha256(csrf), session["csrf_token_hash_sha256"]
            ) or not allowed_origin(request):
                return problem(request, 403)

        with transaction.atomic(using="default"):
            response = self.get_response(request)
            if response.streaming:
                response.close()
                return problem(request, 500)
            if response.status_code >= 400:
                transaction.set_rollback(True, using="default")
            return response
