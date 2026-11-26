#!/bin/bash
set -euo pipefail

scratch=$(mktemp -d)
container=""
volume="relayforge-smoke-${GITHUB_RUN_ID:-$$}-${GITHUB_RUN_ATTEMPT:-1}"
cleanup() {
    if [[ -n "$container" ]]; then docker rm -f "$container" >/dev/null; fi
    docker volume rm "$volume" >/dev/null 2>&1 || true
    rm -f deploy/.env.prod
    rm -rf "$scratch"
}
if [[ -e deploy/.env.prod ]]; then
    echo "Refusing to overwrite deploy/.env.prod" >&2
    exit 1
fi
trap cleanup EXIT
printf 'RF_PUBLIC_HOST=relayforge.example\n' > "$scratch/env"
cp "$scratch/env" deploy/.env.prod
docker compose --project-directory deploy --env-file "$scratch/env" \
    -f deploy/docker-compose.prod.yml config -q

docker build -f deploy/Dockerfile.app -t relayforge-app:ci .
docker build -f deploy/Dockerfile.media-worker -t relayforge-media:ci .
docker run --rm --network none --read-only --cap-drop ALL \
    -e DJANGO_SECRET_KEY=synthetic-docker-smoke-key \
    -e RF_MASTER_KEK_B64=YWFhYWFhYWFhYWFhYWFhYWFhYWFhYWFhYWFhYWFhYWE= \
    relayforge-app:ci sh -ec 'test "$(id -u)" = 10000; python -c "import relayforge.web.wsgi"'

docker volume create "$volume" >/dev/null
docker run --rm --network none --read-only --entrypoint sh --user 0:0 \
    --volume "$volume:/smoke" relayforge-media:ci \
    -ec 'mkdir /smoke/staging /smoke/run; chown 10001:10001 /smoke/staging /smoke/run'
container=$(docker run -d --network none --read-only --cap-drop ALL \
    --security-opt no-new-privileges --pids-limit 64 --tmpfs /tmp \
    --volume "$volume:/smoke" -e RF_MEDIA_SOCKET_PATH=/smoke/run/worker.sock \
    -e RF_MEDIA_STAGING_ROOT=/smoke/staging relayforge-media:ci)
test "$(docker inspect -f '{{.HostConfig.NetworkMode}}' "$container")" = none
docker exec -i "$container" python3 - <<'PY'
import base64
import json
import os
import socket
import time
import uuid
from pathlib import Path

assert (os.getuid(), os.getgid()) == (10001, 10001)
address = Path(os.environ["RF_MEDIA_SOCKET_PATH"])
for _ in range(100):
    if address.is_socket():
        break
    time.sleep(.05)
assert address.is_socket()

def rpc(request):
    with socket.socket(socket.AF_UNIX) as channel:
        channel.settimeout(20)
        channel.connect(str(address))
        channel.sendall(json.dumps(request).encode() + b"\n")
        with channel.makefile("rb") as stream:
            return json.loads(stream.readline())

assert rpc({"command": "ping"})["status"] == "READY"
job_id = str(uuid.uuid4())
staging = Path(os.environ["RF_MEDIA_STAGING_ROOT"]) / job_id
staging.mkdir()
result = rpc({
    "command": "render_infographic", "job_id": job_id,
    "spec": {"title": "آزمون استودیو", "rows": [["مقدار", "۲۵۰"]]},
})
assert result["status"] == "SUCCEEDED", result
assert base64.b64decode(result["content_base64"]).startswith(b"\x89PNG\r\n\x1a\n")
assert not staging.exists()
PY
mkdir -p tmp
printf '%s\n' '{"compose_config":true,"images_built":2,"app_uid":10000,"media_uid":10001,"media_network":"none","media_png":true}' > tmp/m7-docker-smoke-evidence.json
