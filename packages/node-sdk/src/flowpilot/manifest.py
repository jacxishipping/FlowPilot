"""Package manifest schema for first-party and third-party integration packages."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field


class NodePackageManifest(BaseModel):
    """Version, compatibility, permission, and signing metadata for a node package."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    package_name: str = Field(..., min_length=2, max_length=128)
    package_version: str = Field(..., min_length=1, max_length=32)
    sdk_compatibility: str = Field(default=">=0.1.0,<1.0.0")
    first_party: bool = False
    entrypoints: dict[str, str] = Field(
        default_factory=dict,
        description="Mapping of canonical node type -> 'module.path:ClassName'.",
    )
    permissions: tuple[str, ...] = Field(default_factory=tuple)
    credential_types: tuple[str, ...] = Field(default_factory=tuple)
    dependencies: tuple[str, ...] = Field(default_factory=tuple)
    sha256_digest: str | None = None
    signature: str | None = None
