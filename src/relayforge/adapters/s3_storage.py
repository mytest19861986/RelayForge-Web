"""Minimal path-style SigV4 client for a dedicated export bucket."""

import base64
import hashlib
import hmac
import json
import os
import re
from datetime import UTC, datetime, timedelta
from urllib.parse import quote, urlencode, urlsplit
from uuid import UUID

import httpx

MAX_EXPORT_BYTES = 272_629_760
EXPORT_TTL = 900
ALGORITHM = "AWS4-HMAC-SHA256"
LIFECYCLE = (
    b'<LifecycleConfiguration xmlns="http://s3.amazonaws.com/doc/2006-03-01/">'
    b'<Rule><ID>relayforge-exports-24h</ID><Filter><Prefix>tenants/</Prefix></Filter>'
    b'<Status>Enabled</Status><Expiration><Days>1</Days></Expiration></Rule>'
    b'</LifecycleConfiguration>'
)


class StorageError(RuntimeError):
    """An object-store operation failed without revealing credentials."""


def export_key(tenant_id, job_id):
    return f"tenants/{UUID(str(tenant_id))}/exports/{UUID(str(job_id))}.json"


class S3Storage:
    def __init__(self, endpoint, public_upload_url, bucket, access_key, secret_key,
                 region="us-east-1"):
        for origin, public in ((endpoint, False), (public_upload_url, True)):
            url = urlsplit(origin)
            if (
                not url.hostname or url.scheme not in {"http", "https"} or url.username
                or url.password or url.path not in {"", "/"} or url.query or url.fragment
                or (public and url.scheme == "http"
                    and url.hostname not in {"127.0.0.1", "::1", "localhost"})
            ):
                raise ValueError("Use S3 origins and HTTPS for public uploads")
        if not re.fullmatch(r"[a-z0-9][a-z0-9.-]{1,61}[a-z0-9]", bucket):
            raise ValueError("Invalid export bucket")
        self.endpoint, self.public = endpoint.rstrip("/"), public_upload_url.rstrip("/")
        self.bucket, self.access, self._secret, self.region = bucket, access_key, secret_key, region

    @classmethod
    def from_environment(cls):
        endpoint = os.environ["RF_S3_ENDPOINT_URL"]
        return cls(endpoint, os.environ.get("RF_S3_PUBLIC_UPLOAD_URL", endpoint),
                   os.environ["RF_S3_BUCKET"], os.environ["RF_S3_ACCESS_KEY"],
                   os.environ["RF_S3_SECRET_KEY"], os.environ.get("RF_S3_REGION", "us-east-1"))

    def _scope(self, date):
        return f"{date[:8]}/{self.region}/s3/aws4_request"

    def _sign(self, date, value):
        key = ("AWS4" + self._secret).encode()
        for part in self._scope(date).split("/"):
            key = hmac.digest(key, part.encode(), "sha256")
        return hmac.new(key, value.encode(), hashlib.sha256).hexdigest()

    def generate_export_presigned_post(self, tenant_id, export_job_id, max_bytes=MAX_EXPORT_BYTES,
                                       expires_in_sec=EXPORT_TTL, *, issued_at=None):
        if not 1 <= max_bytes <= MAX_EXPORT_BYTES or not 1 <= expires_in_sec <= EXPORT_TTL:
            raise ValueError("Invalid export upload limits")
        now = issued_at or datetime.now(UTC)
        date = now.strftime("%Y%m%dT%H%M%SZ")
        key = export_key(tenant_id, export_job_id)
        fields = {"bucket": self.bucket, "key": key, "x-amz-algorithm": ALGORITHM,
                  "x-amz-credential": f"{self.access}/{self._scope(date)}", "x-amz-date": date}
        conditions = [{name: value} for name, value in fields.items()]
        conditions += [["starts-with", "$key", key.rsplit("/", 1)[0] + "/"],
                       ["content-length-range", 1, max_bytes]]
        policy = json.dumps({
            "expiration": (now + timedelta(seconds=expires_in_sec)).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "conditions": conditions,
        }, separators=(",", ":")).encode()
        encoded = base64.b64encode(policy).decode()
        fields.update(policy=encoded, **{"x-amz-signature": self._sign(date, encoded)})
        return {"upload_url": f"{self.public}/{self.bucket}", "form_fields": fields,
                "max_bytes": max_bytes, "expires_in_sec": expires_in_sec}

    def _request(self, method, key="", *, query=(), body=b"", headers=None):
        if key and any(part in {"", ".", ".."} for part in key.split("/")):
            raise ValueError("Unsafe object key")
        path = f"/{self.bucket}" + ("/" + quote(key, safe="/-_.~") if key else "")
        params = urlencode(sorted(query), quote_via=quote, safe="-_.~")
        date = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
        digest = hashlib.sha256(body).hexdigest()
        signing = {"host": urlsplit(self.endpoint).netloc, "x-amz-date": date,
                   "x-amz-content-sha256": digest, **(headers or {})}
        canonical_headers = "".join(f"{name}:{signing[name]}\n" for name in sorted(signing))
        signed = ";".join(sorted(signing))
        canonical = "\n".join((method, path, params, canonical_headers, signed, digest))
        data = "\n".join((ALGORITHM, date, self._scope(date),
                          hashlib.sha256(canonical.encode()).hexdigest()))
        signing["authorization"] = (
            f"{ALGORITHM} Credential={self.access}/{self._scope(date)}, "
            f"SignedHeaders={signed}, Signature={self._sign(date, data)}"
        )
        signing["accept-encoding"] = "identity"
        return self.endpoint + path + ("?" + params if params else ""), signing

    def _call(
        self, method, key="", *, query=(), body=b"", allow_missing=False, signed_headers=None,
    ):
        url, headers = self._request(method, key, query=query, body=body, headers=signed_headers)
        response = None
        try:
            with httpx.Client(trust_env=False) as client, client.stream(
                method, url, headers=headers, content=body,
            ) as result:
                if result.is_success or (allow_missing and result.status_code == 404):
                    response = result.headers if result.is_success else None
                else:
                    raise StorageError(f"S3_{method}_{result.status_code}")
        except httpx.HTTPError:
            raise StorageError(f"S3_{method}_TRANSPORT") from None
        return response

    def ensure_exports_lifecycle_24h(self):
        checksum = base64.b64encode(hashlib.md5(LIFECYCLE, usedforsecurity=False).digest()).decode()
        self._call("PUT", query=(("lifecycle", ""),), body=LIFECYCLE,
                   signed_headers={"content-md5": checksum, "content-type": "application/xml"})

    def put_object(self, key, body):
        self._call("PUT", key, body=body)

    def head_object(self, key):
        headers = self._call("HEAD", key, allow_missing=True)
        if headers is None:
            return None
        try:
            return int(headers["content-length"]), headers.get("etag")
        except (KeyError, ValueError):
            raise StorageError("S3_INVALID_CONTENT_LENGTH") from None

    def stream_object_chunks(self, key, chunk_size=65536, *, etag=None):
        conditional = {"if-match": etag} if etag else None
        url, headers = self._request("GET", key, headers=conditional)
        try:
            with httpx.Client(trust_env=False) as client, client.stream(
                "GET", url, headers=headers,
            ) as response:
                if response.status_code != 200:
                    raise StorageError(f"S3_GET_{response.status_code}")
                yield from response.iter_raw(chunk_size)
        except httpx.HTTPError:
            raise StorageError("S3_GET_TRANSPORT") from None

    def delete_object(self, key):
        self._call("DELETE", key, allow_missing=True)
