"""AES-256-GCM envelope encryption with tenant-bound AAD and pluggable KMS key wrapping."""

from __future__ import annotations

import base64
import hashlib
import json
import os
from typing import Protocol
from uuid import UUID

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from pydantic import BaseModel, ConfigDict, Field


class CredentialEncryptionError(RuntimeError):
    """Raised when envelope encryption, decryption, or tenant AAD verification fails."""


class EncryptedEnvelope(BaseModel):
    """Self-describing encrypted secret blob stored in PostgreSQL."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    algorithm: str = Field(default="AES-256-GCM")
    key_id: str = Field(..., min_length=1, max_length=128)
    encrypted_dek_b64: str
    dek_nonce_b64: str
    ciphertext_b64: str
    data_nonce_b64: str


class KeyManagementProvider(Protocol):
    """KMS abstraction allowing Cloud KMS, HashiCorp Vault, CMK, or local master keys."""

    @property
    def active_key_id(self) -> str: ...

    def wrap_dek(self, dek: bytes, *, aad: bytes) -> tuple[str, bytes, bytes]:
        """Wrap a 32-byte DEK and return (key_id, wrapped_dek, nonce)."""
        ...

    def unwrap_dek(
        self,
        *,
        key_id: str,
        wrapped_dek: bytes,
        nonce: bytes,
        aad: bytes,
    ) -> bytes:
        """Unwrap a DEK using the specified key_id and AAD."""
        ...


class LocalMasterKeyProvider:
    """Envelope KEK provider deriving 256-bit keys from configured master secrets.

    Supports multiple key IDs simultaneously so key rotation does not invalidate
    existing credential envelopes.
    """

    def __init__(
        self,
        *,
        active_key_id: str = "local-v1",
        keys: dict[str, str] | None = None,
        master_secret: str | None = None,
    ) -> None:
        raw_keys: dict[str, str] = {}
        if keys:
            raw_keys.update(keys)
        if master_secret:
            raw_keys[active_key_id] = master_secret
        if not raw_keys:
            env_secret = os.environ.get(
                "FLOWPILOT_MASTER_KEY",
                "flowpilot-dev-only-insecure-master-key-change-me",
            )
            raw_keys[active_key_id] = env_secret
        if active_key_id not in raw_keys:
            raise CredentialEncryptionError(
                f"Active key_id '{active_key_id}' not found in configured keys."
            )
        self._active_key_id = active_key_id
        self._derived_keys: dict[str, bytes] = {
            kid: hashlib.sha256(f"flowpilot-kek:{kid}:{secret}".encode()).digest()
            for kid, secret in raw_keys.items()
        }

    @property
    def active_key_id(self) -> str:
        return self._active_key_id

    def wrap_dek(self, dek: bytes, *, aad: bytes) -> tuple[str, bytes, bytes]:
        kek = self._derived_keys[self._active_key_id]
        nonce = os.urandom(12)
        wrapped = AESGCM(kek).encrypt(nonce, dek, aad)
        return self._active_key_id, wrapped, nonce

    def unwrap_dek(
        self,
        *,
        key_id: str,
        wrapped_dek: bytes,
        nonce: bytes,
        aad: bytes,
    ) -> bytes:
        kek = self._derived_keys.get(key_id)
        if kek is None:
            raise CredentialEncryptionError(f"Unknown KMS key_id '{key_id}'.")
        try:
            return AESGCM(kek).decrypt(nonce, wrapped_dek, aad)
        except InvalidTag as exc:
            raise CredentialEncryptionError(
                "Failed to unwrap credential DEK: authentication tag mismatch."
            ) from exc


class EnvelopeCipher:
    """Encrypts and decrypts tenant-scoped credential secrets using per-record DEKs."""

    def __init__(self, kms: KeyManagementProvider | None = None) -> None:
        self._kms: KeyManagementProvider = kms or LocalMasterKeyProvider()

    @staticmethod
    def _build_aad(organization_id: UUID, credential_id: UUID) -> bytes:
        return f"org:{organization_id}:cred:{credential_id}".encode()

    def encrypt_secrets(
        self,
        secrets: dict[str, str],
        *,
        organization_id: UUID,
        credential_id: UUID,
    ) -> EncryptedEnvelope:
        """Encrypt a secret dictionary under a fresh 256-bit DEK bound to (org_id, cred_id)."""
        aad = self._build_aad(organization_id, credential_id)
        dek = os.urandom(32)
        data_nonce = os.urandom(12)
        plaintext = json.dumps(secrets, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ciphertext = AESGCM(dek).encrypt(data_nonce, plaintext, aad)
        key_id, wrapped_dek, dek_nonce = self._kms.wrap_dek(dek, aad=aad)

        return EncryptedEnvelope(
            key_id=key_id,
            encrypted_dek_b64=base64.b64encode(wrapped_dek).decode("ascii"),
            dek_nonce_b64=base64.b64encode(dek_nonce).decode("ascii"),
            ciphertext_b64=base64.b64encode(ciphertext).decode("ascii"),
            data_nonce_b64=base64.b64encode(data_nonce).decode("ascii"),
        )

    def decrypt_secrets(
        self,
        envelope: EncryptedEnvelope,
        *,
        organization_id: UUID,
        credential_id: UUID,
    ) -> dict[str, str]:
        """Decrypt an EncryptedEnvelope after verifying cryptographic tenant AAD binding."""
        aad = self._build_aad(organization_id, credential_id)
        wrapped_dek = base64.b64decode(envelope.encrypted_dek_b64)
        dek_nonce = base64.b64decode(envelope.dek_nonce_b64)
        ciphertext = base64.b64decode(envelope.ciphertext_b64)
        data_nonce = base64.b64decode(envelope.data_nonce_b64)

        dek = self._kms.unwrap_dek(
            key_id=envelope.key_id,
            wrapped_dek=wrapped_dek,
            nonce=dek_nonce,
            aad=aad,
        )
        try:
            plaintext = AESGCM(dek).decrypt(data_nonce, ciphertext, aad)
        except InvalidTag as exc:
            raise CredentialEncryptionError(
                "Credential ciphertext authentication failed "
                "(possible tenant mismatch or tampering)."
            ) from exc

        data = json.loads(plaintext.decode("utf-8"))
        if not isinstance(data, dict):
            raise CredentialEncryptionError("Decrypted credential payload is not a JSON object.")
        return {str(k): str(v) for k, v in data.items()}

    def rewrap_envelope(
        self,
        envelope: EncryptedEnvelope,
        *,
        organization_id: UUID,
        credential_id: UUID,
    ) -> EncryptedEnvelope:
        """Rotate the KEK on an existing envelope without exposing plaintext."""
        aad = self._build_aad(organization_id, credential_id)
        dek = self._kms.unwrap_dek(
            key_id=envelope.key_id,
            wrapped_dek=base64.b64decode(envelope.encrypted_dek_b64),
            nonce=base64.b64decode(envelope.dek_nonce_b64),
            aad=aad,
        )
        new_key_id, new_wrapped, new_nonce = self._kms.wrap_dek(dek, aad=aad)
        return EncryptedEnvelope(
            algorithm=envelope.algorithm,
            key_id=new_key_id,
            encrypted_dek_b64=base64.b64encode(new_wrapped).decode("ascii"),
            dek_nonce_b64=base64.b64encode(new_nonce).decode("ascii"),
            ciphertext_b64=envelope.ciphertext_b64,
            data_nonce_b64=envelope.data_nonce_b64,
        )
