"""Bounded, uncompressed tenant backup envelope with independently sealed entries."""

import base64
import hashlib
import json
import os
import re
from datetime import UTC, datetime
from uuid import UUID

from argon2.low_level import Type, hash_secret_raw
from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

MAGIC = b"RFBAK2\0"
MAX_BYTES = 512 * 1024
ENTRY_PATH = re.compile(
    r"(logical/tables/[a-z0-9_]+\.jsonl|secrets/tenant_vault_rekeyed\.aead|"
    r"media/[a-f0-9]{2}/[a-f0-9]{64}\.[a-z0-9]{1,8}|config/[a-z0-9_.-]+\.json)"
)


def encode(value):
    return base64.b64encode(value).decode("ascii")


def decode(value):
    return base64.b64decode(value, validate=True)


def packed(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()


def cipher(password, salt):
    return AESGCM(hash_secret_raw(password.encode(), salt, 3, 65536, 1, 32, Type.ID))


def sha(value):
    return hashlib.sha256(value).hexdigest()


def valid_path(path):
    return isinstance(path, str) and ".." not in path and ENTRY_PATH.fullmatch(path)


def seal(password, tenant, slug, entries):
    salt = os.urandom(16)
    key = cipher(password, salt)
    manifest = {
        "format": "RELAYFORGE_SAAS_ENCRYPTED_BACKUP",
        "manifest_version": "2.0",
        "created_at": datetime.now(UTC).isoformat(),
        "app_schema_version": "0007",
        "tenant_id": str(tenant),
        "source_workspace_slug": slug,
        "backup_scope": "TENANT_SETTINGS_EXPORT",
        "kdf_params": {
            "algorithm": "ARGON2ID",
            "memory_kib": 65536,
            "iterations": 3,
            "parallelism": 1,
            "salt_b64": encode(salt),
        },
        "cipher_suite": "AES-256-GCM",
        "safe_mode_enforced_on_restore": True,
        "pitr_retention_disclosure_days": 14,
        "includes_reencrypted_secrets": "secrets/tenant_vault_rekeyed.aead" in entries,
        "limits": {
            "total_uncompressed_bytes": sum(map(len, entries.values())),
            "entry_count": len(entries),
            "max_compression_ratio": 1,
        },
        "entries": [],
    }
    payload = {}
    for path, data in sorted(entries.items()):
        if not valid_path(path):
            raise ValueError("INVALID_BACKUP_PATH")
        nonce = os.urandom(12)
        ciphertext = key.encrypt(nonce, data, path.encode())
        payload[path] = encode(ciphertext)
        manifest["entries"].append(
            {
                "logical_path": path,
                "byte_size": len(data),
                "plaintext_sha256": sha(data),
                "ciphertext_sha256": sha(ciphertext),
                "aead_nonce_b64": encode(nonce),
            }
        )
    header = MAGIC + salt
    nonce = os.urandom(12)
    bundle = header + nonce + key.encrypt(nonce, packed([manifest, payload]), header)
    if len(bundle) > MAX_BYTES:
        raise ValueError("BACKUP_TOO_LARGE")
    return manifest, bundle


def open_bundle(password, bundle):
    try:
        if len(bundle) > MAX_BYTES or not bundle.startswith(MAGIC):
            raise ValueError
        header_size = len(MAGIC) + 16
        header = bundle[:header_size]
        salt = header[len(MAGIC) :]
        key = cipher(password, salt)
        outer_nonce = bundle[header_size : header_size + 12]
        manifest, payload = json.loads(key.decrypt(outer_nonce, bundle[header_size + 12 :], header))
        entries = manifest["entries"]
        if not 1 <= len(entries) <= 128 or len(entries) != len(payload):
            raise ValueError
        result = {}
        nonces = {outer_nonce}
        for entry in entries:
            path = entry["logical_path"]
            if not valid_path(path) or path in result:
                raise ValueError
            encrypted = decode(payload[path])
            nonce = decode(entry["aead_nonce_b64"])
            if len(nonce) != 12 or nonce in nonces:
                raise ValueError
            nonces.add(nonce)
            data = key.decrypt(nonce, encrypted, path.encode())
            if entry != {
                "logical_path": path,
                "byte_size": len(data),
                "plaintext_sha256": sha(data),
                "ciphertext_sha256": sha(encrypted),
                "aead_nonce_b64": encode(nonce),
            }:
                raise ValueError
            result[path] = data
        validate_manifest(manifest, salt, result)
        return manifest, result
    except (ValueError, KeyError, AttributeError, TypeError, InvalidTag, OverflowError):
        raise ValueError("INVALID_BACKUP") from None


def validate_manifest(manifest, salt, result):
    created = datetime.fromisoformat(manifest["created_at"])
    slug = manifest["source_workspace_slug"]
    if created.utcoffset() is None or len(slug) > 64 or not isinstance(slug, str):
        raise ValueError
    UUID(manifest["tenant_id"])
    if manifest != {
        "format": "RELAYFORGE_SAAS_ENCRYPTED_BACKUP",
        "manifest_version": "2.0",
        "created_at": manifest["created_at"],
        "app_schema_version": "0007",
        "tenant_id": manifest["tenant_id"],
        "source_workspace_slug": slug,
        "backup_scope": "TENANT_SETTINGS_EXPORT",
        "kdf_params": {
            "algorithm": "ARGON2ID",
            "memory_kib": 65536,
            "iterations": 3,
            "parallelism": 1,
            "salt_b64": encode(salt),
        },
        "cipher_suite": "AES-256-GCM",
        "safe_mode_enforced_on_restore": True,
        "pitr_retention_disclosure_days": 14,
        "includes_reencrypted_secrets": "secrets/tenant_vault_rekeyed.aead" in result,
        "limits": {
            "total_uncompressed_bytes": sum(map(len, result.values())),
            "entry_count": len(result),
            "max_compression_ratio": 1,
        },
        "entries": manifest["entries"],
    }:
        raise ValueError
