"""Cloud KMS providers (AWS KMS, GCP KMS, HashiCorp Vault) and Enterprise Tenant CMK Router."""

from __future__ import annotations

import base64
import hashlib
import os
from typing import Any
from uuid import UUID

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from flowpilot_credentials.envelope import (
    CredentialEncryptionError,
    EncryptedEnvelope,
    EnvelopeCipher,
    KeyManagementProvider,
)


class AwsKmsProvider:
    """AWS KMS KeyManagementProvider delegating Encrypt/Decrypt to a boto3/stub client."""

    def __init__(self, *, key_arn: str, kms_client: Any) -> None:
        self._key_arn = key_arn
        self._client = kms_client

    @property
    def active_key_id(self) -> str:
        return f"aws-kms:{self._key_arn}"

    def wrap_dek(self, dek: bytes, *, aad: bytes) -> tuple[str, bytes, bytes]:
        resp = self._client.encrypt(
            KeyId=self._key_arn,
            Plaintext=dek,
            EncryptionContext={"aad": aad.decode("utf-8")},
        )
        ciphertext = bytes(resp["CiphertextBlob"])
        return self.active_key_id, ciphertext, b"aws-kms-v1"

    def unwrap_dek(
        self,
        *,
        key_id: str,
        wrapped_dek: bytes,
        nonce: bytes,
        aad: bytes,
    ) -> bytes:
        resp = self._client.decrypt(
            CiphertextBlob=wrapped_dek,
            EncryptionContext={"aad": aad.decode("utf-8")},
        )
        return bytes(resp["Plaintext"])


class GcpKmsProvider:
    """Google Cloud KMS KeyManagementProvider delegating encrypt/decrypt to a KMS client."""

    def __init__(self, *, crypto_key_name: str, kms_client: Any) -> None:
        self._key_name = crypto_key_name
        self._client = kms_client

    @property
    def active_key_id(self) -> str:
        return f"gcp-kms:{self._key_name}"

    def wrap_dek(self, dek: bytes, *, aad: bytes) -> tuple[str, bytes, bytes]:
        resp = self._client.encrypt(
            request={
                "name": self._key_name,
                "plaintext": dek,
                "additional_authenticated_data": aad,
            }
        )
        return self.active_key_id, bytes(resp.ciphertext), b"gcp-kms-v1"

    def unwrap_dek(
        self,
        *,
        key_id: str,
        wrapped_dek: bytes,
        nonce: bytes,
        aad: bytes,
    ) -> bytes:
        resp = self._client.decrypt(
            request={
                "name": self._key_name,
                "ciphertext": wrapped_dek,
                "additional_authenticated_data": aad,
            }
        )
        return bytes(resp.plaintext)


class AzureKeyVaultKeyProvider:
    """Azure Key Vault CryptographyClient provider (wrap_key / unwrap_key)."""

    def __init__(
        self,
        *,
        vault_url: str,
        key_name: str,
        crypto_client: Any,
        algorithm: str = "RSA-OAEP-256",
    ) -> None:
        self._vault_url = vault_url.rstrip("/")
        self._key_name = key_name
        self._client = crypto_client
        self._algorithm = algorithm

    @property
    def active_key_id(self) -> str:
        return f"azure-kv:{self._vault_url}/keys/{self._key_name}"

    def wrap_dek(self, dek: bytes, *, aad: bytes) -> tuple[str, bytes, bytes]:
        resp = self._client.wrap_key(self._algorithm, dek)
        return self.active_key_id, bytes(resp.encrypted_key), b"azure-kv-v1"

    def unwrap_dek(
        self,
        *,
        key_id: str,
        wrapped_dek: bytes,
        nonce: bytes,
        aad: bytes,
    ) -> bytes:
        resp = self._client.unwrap_key(self._algorithm, wrapped_dek)
        return bytes(resp.key)


class VaultTransitKmsProvider:
    """HashiCorp Vault Transit secrets engine provider for self-hosted & enterprise KEKs."""

    def __init__(self, *, key_name: str, vault_client: Any) -> None:
        self._key_name = key_name
        self._client = vault_client

    @property
    def active_key_id(self) -> str:
        return f"vault-transit:{self._key_name}"

    def wrap_dek(self, dek: bytes, *, aad: bytes) -> tuple[str, bytes, bytes]:
        resp = self._client.encrypt_data(
            name=self._key_name,
            plaintext=base64.b64encode(dek).decode("ascii"),
            context=base64.b64encode(aad).decode("ascii"),
        )
        ct_str = str(resp["data"]["ciphertext"])
        return self.active_key_id, ct_str.encode("utf-8"), b"vault-v1"

    def unwrap_dek(
        self,
        *,
        key_id: str,
        wrapped_dek: bytes,
        nonce: bytes,
        aad: bytes,
    ) -> bytes:
        resp = self._client.decrypt_data(
            name=self._key_name,
            ciphertext=wrapped_dek.decode("utf-8"),
            context=base64.b64encode(aad).decode("ascii"),
        )
        return base64.b64decode(resp["data"]["plaintext"])


class CustomerManagedKeyProvider:
    """Enterprise tenant-specific CMK provider backed by customer-supplied key material."""

    def __init__(self, *, organization_id: UUID, key_alias: str, raw_key_material: bytes) -> None:
        self._org_id = organization_id
        self._key_id = f"cmk:{organization_id}:{key_alias}"
        self._kek = hashlib.sha256(raw_key_material).digest()

    @property
    def active_key_id(self) -> str:
        return self._key_id

    def wrap_dek(self, dek: bytes, *, aad: bytes) -> tuple[str, bytes, bytes]:
        nonce = os.urandom(12)
        return self._key_id, AESGCM(self._kek).encrypt(nonce, dek, aad), nonce

    def unwrap_dek(
        self,
        *,
        key_id: str,
        wrapped_dek: bytes,
        nonce: bytes,
        aad: bytes,
    ) -> bytes:
        if key_id != self._key_id:
            raise CredentialEncryptionError(f"CMK key_id mismatch: '{key_id}'.")
        return AESGCM(self._kek).decrypt(nonce, wrapped_dek, aad)


class TenantCmkRouter:
    """Routes enterprise organizations to their Customer-Managed Key or default SaaS KMS."""

    def __init__(self, default_kms: KeyManagementProvider) -> None:
        self._default_cipher = EnvelopeCipher(default_kms)
        self._tenant_ciphers: dict[UUID, EnvelopeCipher] = {}

    def register_tenant_kms(self, organization_id: UUID, kms: KeyManagementProvider) -> None:
        self._tenant_ciphers[organization_id] = EnvelopeCipher(kms)

    def cipher_for(self, organization_id: UUID) -> EnvelopeCipher:
        return self._tenant_ciphers.get(organization_id, self._default_cipher)

    def encrypt_secrets(
        self,
        secrets: dict[str, str],
        *,
        organization_id: UUID,
        credential_id: UUID,
    ) -> EncryptedEnvelope:
        return self.cipher_for(organization_id).encrypt_secrets(
            secrets,
            organization_id=organization_id,
            credential_id=credential_id,
        )

    def decrypt_secrets(
        self,
        envelope: EncryptedEnvelope,
        *,
        organization_id: UUID,
        credential_id: UUID,
    ) -> dict[str, str]:
        return self.cipher_for(organization_id).decrypt_secrets(
            envelope,
            organization_id=organization_id,
            credential_id=credential_id,
        )
