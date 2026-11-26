import base64
import binascii
import os
from uuid import UUID

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from relayforge.core.security import SecretKind, SecretRevokedError, TenantKeyShreddedError


class EnvelopeSecretVault:
    def __init__(self, master_kek: bytes):
        if len(master_kek) != 32:
            raise ValueError("Master KEK must be 32 bytes")
        self._kek = AESGCM(master_kek)

    @classmethod
    def from_environment(cls):
        try:
            return cls(base64.b64decode(os.environ.get("RF_MASTER_KEK_B64", ""), validate=True))
        except (ValueError, binascii.Error):
            raise ValueError("RF_MASTER_KEK_B64 must encode exactly 32 bytes") from None

    def generate_wrapped_dek(self, tenant_id: UUID, key_version: int) -> bytes:
        nonce = os.urandom(12)
        aad = f"relayforge:dek:v1:{tenant_id}:{key_version}".encode()
        return nonce + self._kek.encrypt(nonce, os.urandom(32), aad)

    def _key(self, tenant_id, key_version, wrapped_dek, key_state):
        if key_state == "SHREDDED":
            raise TenantKeyShreddedError("Tenant key has been shredded")
        aad = f"relayforge:dek:v1:{tenant_id}:{key_version}".encode()
        return AESGCM(self._kek.decrypt(wrapped_dek[:12], wrapped_dek[12:], aad))

    @staticmethod
    def _secret_aad(tenant_id: UUID, workspace_id: UUID, secret_id: UUID, kind: SecretKind):
        return f"relayforge:secret:v1:{tenant_id}:{workspace_id}:{secret_id}:{kind}".encode()

    def encrypt_secret(
        self, tenant_id, workspace_id, secret_id, kind, raw_secret, *, key_version, wrapped_dek
    ):
        nonce = os.urandom(12)
        key = self._key(tenant_id, key_version, wrapped_dek, "ACTIVE")
        aad = self._secret_aad(tenant_id, workspace_id, secret_id, kind)
        return nonce, key.encrypt(nonce, raw_secret.encode(), aad)

    def decrypt_secret(
        self, tenant_id, workspace_id, secret_id, kind, nonce, ciphertext, *,
        key_version, wrapped_dek, key_state, is_revoked,
    ):
        if key_state == "SHREDDED":
            raise TenantKeyShreddedError("Tenant key has been shredded")
        if is_revoked:
            raise SecretRevokedError("Secret has been revoked")
        key = self._key(tenant_id, key_version, wrapped_dek, key_state)
        aad = self._secret_aad(tenant_id, workspace_id, secret_id, kind)
        return key.decrypt(nonce, ciphertext, aad).decode()
