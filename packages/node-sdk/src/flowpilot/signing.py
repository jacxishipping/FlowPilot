"""Ed25519 cryptographic package signing, verification, and revocation policy (Section 8)."""

from __future__ import annotations

import base64
import hashlib
import json

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
    Ed25519PublicKey,
)

from flowpilot.manifest import NodePackageManifest


class PackageTrustError(RuntimeError):
    """Raised when a third-party node package fails signature, digest, or revocation checks."""


def generate_signing_keypair() -> tuple[str, str]:
    """Generate a base64-encoded Ed25519 (private_key_b64, public_key_b64) pair."""
    priv = Ed25519PrivateKey.generate()
    pub = priv.public_key()
    priv_raw = priv.private_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PrivateFormat.Raw,
        encryption_algorithm=serialization.NoEncryption(),
    )
    pub_raw = pub.public_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PublicFormat.Raw,
    )
    return (
        base64.b64encode(priv_raw).decode("ascii"),
        base64.b64encode(pub_raw).decode("ascii"),
    )


def compute_manifest_digest(manifest: NodePackageManifest) -> str:
    """Compute canonical SHA-256 digest of a NodePackageManifest excluding digest/signature."""
    data = manifest.model_dump(mode="json")
    data.pop("sha256_digest", None)
    data.pop("signature", None)
    canonical = json.dumps(data, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def sign_manifest(manifest: NodePackageManifest, private_key_b64: str) -> NodePackageManifest:
    """Attach canonical SHA-256 digest and Ed25519 signature to a NodePackageManifest."""
    digest = compute_manifest_digest(manifest)
    priv = Ed25519PrivateKey.from_private_bytes(base64.b64decode(private_key_b64))
    sig = priv.sign(digest.encode("ascii"))
    return manifest.model_copy(
        update={
            "sha256_digest": digest,
            "signature": base64.b64encode(sig).decode("ascii"),
        }
    )


def verify_manifest_signature(manifest: NodePackageManifest, public_key_b64: str) -> bool:
    """Verify that a manifest's digest and Ed25519 signature match the trusted public key."""
    if not manifest.sha256_digest or not manifest.signature:
        return False
    expected_digest = compute_manifest_digest(manifest)
    if manifest.sha256_digest != expected_digest:
        return False
    pub = Ed25519PublicKey.from_public_bytes(base64.b64decode(public_key_b64))
    try:
        pub.verify(
            base64.b64decode(manifest.signature),
            expected_digest.encode("ascii"),
        )
        return True
    except (InvalidSignature, ValueError):
        return False


class PackageTrustPolicy:
    """Enforces publisher key verification and package revocation lists before worker loading."""

    def __init__(
        self,
        *,
        trusted_public_keys: list[str],
        revoked_packages: set[str] | None = None,
        allow_unsigned_first_party: bool = True,
    ) -> None:
        self._trusted_keys = list(trusted_public_keys)
        self._revoked: set[str] = set(revoked_packages or ())
        self._allow_unsigned_first_party = allow_unsigned_first_party

    def revoke(self, package_name: str, package_version: str | None = None) -> None:
        key = f"{package_name}@{package_version}" if package_version else package_name
        self._revoked.add(key)

    def assert_trusted(self, manifest: NodePackageManifest) -> None:
        exact_key = f"{manifest.package_name}@{manifest.package_version}"
        if manifest.package_name in self._revoked or exact_key in self._revoked:
            raise PackageTrustError(
                f"Package '{exact_key}' has been revoked and cannot be loaded."
            )

        if manifest.first_party and self._allow_unsigned_first_party and not manifest.signature:
            return

        if not any(verify_manifest_signature(manifest, key) for key in self._trusted_keys):
            raise PackageTrustError(
                f"Package '{exact_key}' failed Ed25519 signature verification."
            )
