import os
from urllib.parse import parse_qsl, unquote, urlsplit

from relayforge.adapters.vault import EnvelopeSecretVault

SECRET_KEY = os.environ["DJANGO_SECRET_KEY"]
DEBUG = False
ROOT_URLCONF = "relayforge.web.urls"
ALLOWED_HOSTS = os.environ.get("RF_ALLOWED_HOSTS", "127.0.0.1").split(",")
ALLOWED_ORIGINS = frozenset(filter(None, os.environ.get("RF_ALLOWED_ORIGINS", "").split(",")))
ALLOW_INSECURE_LOCAL_COOKIE = os.environ.get("RF_ALLOW_INSECURE_LOCAL_COOKIE") == "true"
# Enable only when the ingress strips and replaces X-Forwarded-Proto.
if os.environ.get("RF_TRUST_PROXY_SSL_HEADER") == "true":
    SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")
USE_TZ = True
DATA_UPLOAD_MAX_MEMORY_SIZE = 768 * 1024
WSGI_APPLICATION = "relayforge.web.wsgi.application"
LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "filters": {
        "scrub": {
            "()": "relayforge.adapters.net_guard.SecretLogFilter",
            "extra_secrets": (
                SECRET_KEY, os.environ["RF_MASTER_KEK_B64"],
                os.environ.get("RF_OPERATOR_BEARER_TOKEN", ""),
            ),
        },
    },
    "formatters": {"json": {"()": "relayforge.adapters.net_guard.JsonLogFormatter"}},
    "handlers": {
        "safe_console": {
            "class": "logging.StreamHandler",
            "filters": ["scrub"],
            "formatter": "json",
        },
    },
    "loggers": {
        name: {"handlers": ["safe_console"], "level": "INFO", "propagate": False}
        for name in ("django", "relayforge", "httpx", "httpcore")
    },
}
MIDDLEWARE = [
    "relayforge.web.middleware.SecurityMiddleware",
    "relayforge.web.middleware.TenantRLSMiddleware",
]
VAULT = EnvelopeSecretVault.from_environment()


def database(variable, role):
    url = urlsplit(os.environ.get(variable, f"postgresql://{role}@127.0.0.1:5432/relayforge"))
    if url.scheme not in {"postgresql", "postgres"} or unquote(url.username or "") != role:
        raise ValueError(f"{variable} must connect as {role}")
    query = dict(parse_qsl(url.query))
    unsupported = query.keys() - {"sslmode", "sslrootcert", "connect_timeout"}
    if unsupported:
        raise ValueError(f"Unsupported connection options in {variable}")
    return {
        "ENGINE": "django.db.backends.postgresql",
        "NAME": unquote(url.path.removeprefix("/")),
        "USER": role,
        "PASSWORD": unquote(url.password or ""),
        "HOST": url.hostname,
        "PORT": url.port,
        "CONN_MAX_AGE": 0,
        "DISABLE_SERVER_SIDE_CURSORS": True,
        "ATOMIC_REQUESTS": False,
        "OPTIONS": {**query, "prepare_threshold": None},
    }


DATABASES = {
    "default": database("DATABASE_WEB_URL", "relayforge_web"),
    "operator": database("DATABASE_OPERATOR_URL", "relayforge_operator"),
}
