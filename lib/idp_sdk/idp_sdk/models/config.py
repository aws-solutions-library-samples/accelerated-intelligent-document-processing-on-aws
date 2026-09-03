# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Configuration-related models."""

from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field


class ConfigCreateResult(BaseModel):
    """Result of config template creation."""

    yaml_content: str = Field(description="Generated YAML configuration content")
    output_path: Optional[str] = Field(
        default=None, description="Path where config was written"
    )


class ConfigValidationResult(BaseModel):
    """Result of configuration validation."""

    valid: bool = Field(description="Whether configuration is valid")
    errors: List[str] = Field(default_factory=list, description="Validation errors")
    warnings: List[str] = Field(default_factory=list, description="Validation warnings")
    deprecated_fields: List[str] = Field(
        default_factory=list,
        description="Deprecated fields found in the configuration file",
    )
    unknown_fields: List[str] = Field(
        default_factory=list,
        description="Unknown fields found in the configuration file (not in IDPConfig schema)",
    )
    merged_config: Optional[Dict[str, Any]] = Field(
        default=None, description="Merged configuration (if show_merged=True)"
    )


class ConfigDownloadResult(BaseModel):
    """Result of config download."""

    config: Dict[str, Any] = Field(description="Configuration dictionary")
    yaml_content: str = Field(description="Configuration as YAML string")
    output_path: Optional[str] = Field(
        default=None, description="Path where config was written"
    )
    revision: Optional[int] = Field(
        default=None,
        description=(
            "Revision the configuration was read from, when a specific revision "
            "was requested. None means the profile's current configuration."
        ),
    )


class ConfigUploadResult(BaseModel):
    """Result of config upload."""

    success: bool = Field(description="Whether upload succeeded")
    version: Optional[str] = Field(
        default=None, description="Configuration version that was uploaded"
    )
    version_created: bool = Field(
        default=False,
        description="True if a new version was created; False if an existing version was updated",
    )
    revision: Optional[int] = Field(
        default=None,
        description=(
            "Revision number this upload produced — the value to pass as "
            "config_revision to pin processing to exactly what was just uploaded. "
            "None when the stack has no revision history (an older deployment). "
            "A save that changed nothing records no new revision, so this is then "
            "the revision already current."
        ),
    )
    error: Optional[str] = Field(default=None, description="Error message if failed")


class ConfigActivateResult(BaseModel):
    """Result of a configuration version activation."""

    success: bool = Field(description="Whether activation succeeded")
    activated_version: str = Field(description="The configuration version activated")
    bda_synced: bool = Field(
        default=False,
        description="Whether BDA blueprint sync was performed",
    )
    bda_classes_synced: int = Field(
        default=0,
        description="Number of BDA classes successfully synced",
    )
    bda_classes_failed: int = Field(
        default=0,
        description="Number of BDA classes that failed to sync",
    )
    error: Optional[str] = Field(default=None, description="Error message if failed")


class ConfigVersionInfo(BaseModel):
    """Information about a single configuration version."""

    version_name: str = Field(description="Configuration version name/identifier")
    is_active: bool = Field(
        default=False, description="Whether this is the currently active version"
    )
    created_at: Optional[str] = Field(
        default=None, description="ISO timestamp when version was created"
    )
    updated_at: Optional[str] = Field(
        default=None, description="ISO timestamp when version was last updated"
    )
    description: Optional[str] = Field(
        default=None, description="Optional description for this version"
    )
    managed: bool = Field(
        default=False,
        description=(
            "True when the profile is owned by a stack — a built-in preset from "
            "config_library, or one an extension installed. The Web UI refuses to "
            "delete a managed profile because the owning stack would recreate it; "
            "`idp-cli config-delete` can still remove one, which is how an "
            "extension's profiles are cleaned up after the extension is gone."
        ),
    )
    latest_revision: Optional[int] = Field(
        default=None,
        description=(
            "Highest revision number ever cut for this profile. None when the "
            "profile has no history (untouched since the stack was upgraded)."
        ),
    )
    published_revision: Optional[int] = Field(
        default=None,
        description=(
            "Revision the profile's current configuration reflects — the one a "
            "new document is pinned to. Usually equal to latest_revision; it "
            "differs while a restore is in flight."
        ),
    )


class ConfigListResult(BaseModel):
    """Result of listing configuration versions."""

    versions: List[ConfigVersionInfo] = Field(
        description="List of configuration versions"
    )
    count: int = Field(description="Total number of versions returned")


class ConfigRevisionInfo(BaseModel):
    """One immutable revision of a Configuration Profile."""

    revision: int = Field(description="Revision number (r7 is revision=7)")
    created_at: Optional[str] = Field(
        default=None, description="ISO timestamp when the revision was cut"
    )
    created_by: Optional[str] = Field(
        default=None,
        description="Email of the user whose save cut this, or 'system'",
    )
    label: Optional[str] = Field(
        default=None,
        description="User-applied label. A labeled revision is exempt from retention pruning.",
    )
    notes: Optional[str] = Field(
        default=None, description="Note recorded with the revision"
    )
    size_bytes: Optional[int] = Field(
        default=None, description="Uncompressed size of the recorded configuration"
    )
    published: bool = Field(
        default=False,
        description="True for the revision the profile's current configuration reflects",
    )
    pinned: bool = Field(
        default=False,
        description=(
            "True when a test run scored against this revision, which exempts it "
            "from retention pruning so the comparison stays reproducible."
        ),
    )
    class_fingerprint: Optional[str] = Field(
        default=None,
        description=(
            "Hash of the document classes and their schemas. Two revisions with "
            "the same fingerprint extract the same fields, so their accuracy "
            "numbers are directly comparable."
        ),
    )


class ConfigRevisionListResult(BaseModel):
    """Revision history of one Configuration Profile, newest first."""

    profile: str = Field(description="Configuration profile these revisions belong to")
    revisions: List[ConfigRevisionInfo] = Field(
        default_factory=list, description="Retained revisions, newest first"
    )
    count: int = Field(default=0, description="Number of retained revisions")


class ConfigDeleteResult(BaseModel):
    """Result of deleting a configuration version."""

    success: bool = Field(description="Whether deletion succeeded")
    deleted_version: str = Field(
        description="The configuration version that was deleted"
    )
    error: Optional[str] = Field(default=None, description="Error message if failed")


class ConfigSyncBdaResult(BaseModel):
    """Result of BDA blueprint synchronization."""

    success: bool = Field(description="Whether sync succeeded")
    direction: str = Field(
        description="Sync direction: 'bidirectional', 'bda_to_idp', or 'idp_to_bda'"
    )
    mode: str = Field(
        default="replace",
        description="Sync mode: 'replace' or 'merge'",
    )
    classes_synced: int = Field(
        default=0, description="Number of classes successfully synced"
    )
    classes_failed: int = Field(
        default=0, description="Number of classes that failed to sync"
    )
    processed_classes: List[str] = Field(
        default_factory=list, description="Names of processed classes"
    )
    error: Optional[str] = Field(default=None, description="Error message if failed")
