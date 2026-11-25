"""FlowPilot envelope-encrypted credential manager."""

from flowpilot_credentials.envelope import (
    CredentialEncryptionError,
    EncryptedEnvelope,
    EnvelopeCipher,
    KeyManagementProvider,
    LocalMasterKeyProvider,
)
from flowpilot_credentials.kms import (
    AwsKmsProvider,
    CustomerManagedKeyProvider,
    GcpKmsProvider,
    TenantCmkRouter,
    VaultTransitKmsProvider,
)

__all__ = [
    "AwsKmsProvider",
    "CredentialEncryptionError",
    "CustomerManagedKeyProvider",
    "EncryptedEnvelope",
    "EnvelopeCipher",
    "GcpKmsProvider",
    "KeyManagementProvider",
    "LocalMasterKeyProvider",
    "TenantCmkRouter",
    "VaultTransitKmsProvider",
]
