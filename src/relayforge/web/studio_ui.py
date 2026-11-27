import os
from pathlib import Path
from urllib.parse import urlsplit

from django.http import FileResponse, Http404
from django.views.decorators.http import require_safe

ASSETS = Path(__file__).parent / "static" / "relayforge_ui"
FILES = {
    "index.html": "text/html",
    "studio.css": "text/css",
    "studio.js": "text/javascript",
    "views.js": "text/javascript",
    "components.js": "text/javascript",
    "operator.js": "text/javascript",
}


@require_safe
def shell(request, view=None):
    response = asset(request, "index.html")
    endpoint = os.environ.get("RF_S3_PUBLIC_UPLOAD_URL") or os.environ.get("RF_S3_ENDPOINT_URL")
    origin = ""
    if endpoint:
        parsed = urlsplit(endpoint)
        if (
            parsed.scheme == "https"
            and parsed.hostname
            and not parsed.username
            and not parsed.password
        ):
            origin = f" https://{parsed.netloc}"
    response["Content-Security-Policy"] = (
        "default-src 'self'; frame-ancestors 'none'; object-src 'none'; "
        f"base-uri 'none'; img-src 'self' data: blob:; connect-src 'self'{origin}"
    )
    return response


@require_safe
def asset(request, filename):
    if filename not in FILES:
        raise Http404
    return FileResponse(
        (ASSETS / filename).open("rb"), content_type=FILES[filename] + "; charset=utf-8"
    )
