# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Configuration operations for IDP SDK."""

import logging
from typing import Optional

from idp_sdk._core.naming import resolve_config_profile
from idp_sdk.exceptions import IDPProcessingError, IDPResourceNotFoundError
from idp_sdk.models import (
    ConfigActivateResult,
    ConfigCreateResult,
    ConfigDeleteResult,
    ConfigDownloadResult,
    ConfigListResult,
    ConfigRevisionInfo,
    ConfigRevisionListResult,
    ConfigSyncBdaResult,
    ConfigUploadResult,
    ConfigValidationResult,
    ConfigVersionInfo,
)

logger = logging.getLogger(__name__)


class ConfigOperation:
    """Configuration management operations."""

    def __init__(self, client):
        self._client = client

    def _lookup_stack_resources(self, stack_name: str, logical_ids) -> dict:
        """Physical IDs for the requested logical IDs, in one pass over the stack."""
        import boto3

        wanted = set(logical_ids)
        found: dict = {}

        # Enhancement 8: use self._client._region consistently
        cfn = boto3.client("cloudformation", region_name=self._client._region)
        paginator = cfn.get_paginator("list_stack_resources")

        for page in paginator.paginate(StackName=stack_name):
            for resource in page.get("StackResourceSummaries", []):
                logical_id = resource.get("LogicalResourceId")
                if logical_id in wanted:
                    found[logical_id] = resource.get("PhysicalResourceId")
            if len(found) == len(wanted):
                break

        return found

    def _get_config_table(self, stack_name: str) -> str:
        """Look up the ConfigurationTable physical resource ID for a stack.

        Returns the physical resource ID.
        Raises IDPResourceNotFoundError if not found.
        """
        config_table = self._lookup_stack_resources(
            stack_name, {"ConfigurationTable"}
        ).get("ConfigurationTable")

        if not config_table:
            raise IDPResourceNotFoundError(
                f"ConfigurationTable not found in stack '{stack_name}'"
            )

        return config_table

    def _configure_config_env(self, stack_name: str) -> str:
        """
        Point `idp_common` at this stack's configuration table AND bucket.

        Both, because revision history lives in two places: the counters and index
        in DynamoDB, the recorded configurations in S3 under the Configuration
        bucket. `ConfigRevisionStore` treats a missing `CONFIGURATION_BUCKET` as
        "history disabled" and silently does nothing — so setting only the table
        (which is all the SDK used to do) meant every CLI/SDK save skipped cutting
        a revision, every history listing came back empty, and deleting a profile
        left its revision bodies orphaned in S3. The Lambdas always had both set,
        which is why this was invisible until the CLI read a real stack.

        Returns the configuration table's physical ID.
        """
        import os

        found = self._lookup_stack_resources(
            stack_name, {"ConfigurationTable", "ConfigurationBucket"}
        )

        config_table = found.get("ConfigurationTable")
        if not config_table:
            raise IDPResourceNotFoundError(
                f"ConfigurationTable not found in stack '{stack_name}'"
            )
        os.environ["CONFIGURATION_TABLE_NAME"] = config_table

        config_bucket = found.get("ConfigurationBucket")
        if config_bucket:
            os.environ["CONFIGURATION_BUCKET"] = config_bucket
        else:
            logger.warning(
                f"Stack '{stack_name}' has no ConfigurationBucket; configuration "
                f"revision history is unavailable for this stack"
            )

        return config_table

    def create(
        self,
        features: str = "min",
        pattern: str = "pattern-2",
        output: Optional[str] = None,
        include_prompts: bool = False,
        include_comments: bool = True,
        **kwargs,
    ) -> ConfigCreateResult:
        """Generate an IDP configuration template.

        Args:
            features: Feature set to include
            pattern: Pattern to use (pattern-1, pattern-2)
            output: Optional output file path
            include_prompts: Include prompt templates
            include_comments: Include explanatory comments
            **kwargs: Additional parameters

        Returns:
            ConfigCreateResult with generated configuration
        """
        from idp_common.config.merge_utils import generate_config_template

        if "," in features:
            feature_list = [f.strip() for f in features.split(",")]
        else:
            feature_list = features

        yaml_content = generate_config_template(
            features=feature_list,
            pattern=pattern,
            include_prompts=include_prompts,
            include_comments=include_comments,
        )

        if output:
            with open(output, "w", encoding="utf-8") as f:
                f.write(yaml_content)

        return ConfigCreateResult(yaml_content=yaml_content, output_path=output)

    def validate(
        self,
        config_file: str,
        pattern: str = "pattern-2",
        show_merged: bool = False,
        strict: bool = False,
        **kwargs,
    ) -> ConfigValidationResult:
        """Validate a configuration file against system defaults.

        Args:
            config_file: Path to configuration file
            pattern: Pattern to validate against
            show_merged: Include merged configuration in result
            strict: If True, report deprecated/unknown fields as errors
                    (the caller decides whether to fail — the SDK only reports them)
            **kwargs: Additional parameters

        Returns:
            ConfigValidationResult with validation status, including deprecated_fields
            and unknown_fields populated when extra keys are found.
        """
        from pathlib import Path

        import yaml

        from idp_common.config.merge_utils import load_yaml_file, validate_config

        try:
            user_config = load_yaml_file(Path(config_file))
        except yaml.YAMLError as e:
            return ConfigValidationResult(
                valid=False, errors=[f"YAML syntax error: {e}"]
            )
        except Exception as e:
            return ConfigValidationResult(
                valid=False, errors=[f"Failed to load file: {e}"]
            )

        result = validate_config(user_config, pattern=pattern)

        # Enhancement 3: detect deprecated and unknown fields
        deprecated_fields: list = []
        unknown_fields: list = []
        errors = list(result.get("errors", []))
        warnings = list(result.get("warnings", []))

        try:
            from idp_common.config.models import IDP_CONFIG_DEPRECATED_FIELDS, IDPConfig

            defined_fields = set(IDPConfig.model_fields.keys())
            user_fields = (
                set(user_config.keys()) if isinstance(user_config, dict) else set()
            )
            extra_fields = user_fields - defined_fields

            deprecated_fields = sorted(extra_fields & IDP_CONFIG_DEPRECATED_FIELDS)
            unknown_fields = sorted(extra_fields - IDP_CONFIG_DEPRECATED_FIELDS)

            # Add informational warnings for deprecated / unknown fields
            for field in deprecated_fields:
                warnings.append(
                    f"Deprecated field '{field}' found — it will be ignored by the pipeline"
                )
            for field in unknown_fields:
                warnings.append(
                    f"Unknown field '{field}' found — it is not part of the IDPConfig schema"
                )

        except ImportError:
            # If idp_common.config.models is not available, skip the check gracefully
            logger.warning(
                "Could not import IDP_CONFIG_DEPRECATED_FIELDS — skipping deprecated field check"
            )

        return ConfigValidationResult(
            valid=result["valid"],
            errors=errors,
            warnings=warnings,
            deprecated_fields=deprecated_fields,
            unknown_fields=unknown_fields,
            merged_config=result.get("merged_config") if show_merged else None,
        )

    def download(
        self,
        stack_name: Optional[str] = None,
        output: Optional[str] = None,
        format: str = "full",
        pattern: Optional[str] = None,
        config_version: Optional[str] = None,
        config_revision: Optional[int] = None,
        *,
        config_profile: Optional[str] = None,
        **kwargs,
    ) -> ConfigDownloadResult:
        """Download configuration from a deployed IDP stack.

        Args:
            stack_name: Optional stack name override
            output: Optional output file path
            format: Format type ('full' or 'minimal')
            pattern: Pattern override
            config_version: Configuration profile to download (default: active version)
            config_revision: Download an exact revision of that profile rather than
                its current configuration — how you retrieve what an earlier run
                actually used, or branch a new iteration from an older one.
            config_profile: Configuration profile (the current name for
                config_version; either may be given, not both with different values).
            **kwargs: Additional parameters

        Returns:
            ConfigDownloadResult with downloaded configuration

        Raises:
            IDPResourceNotFoundError: If the requested revision is not retained.
                Falling back to the profile head would hand back a *different*
                configuration than the one asked for, under the same filename.
        """
        config_version = resolve_config_profile(config_profile, config_version)

        import yaml

        name = self._client._require_stack(stack_name)
        config_table = self._configure_config_env(name)

        # If no version specified, resolve the active version from DynamoDB
        # (all configs are stored as Config#<version>, never as bare "Config")
        if not config_version:
            from idp_common.config.configuration_manager import ConfigurationManager

            manager = ConfigurationManager()
            for v in manager.list_config_versions():
                if v.get("isActive"):
                    config_version = v.get("versionName")
                    logger.info(f"Resolved active config version: {config_version}")
                    break
            if not config_version:
                from idp_common.config.constants import DEFAULT_VERSION

                config_version = DEFAULT_VERSION
                logger.info(
                    f"No active version found, falling back to: {config_version}"
                )

        if config_revision is not None:
            from idp_common.config.configuration_manager import ConfigurationManager

            manager = ConfigurationManager()
            body = manager.get_revision(config_version, int(config_revision))
            if body is None:
                raise IDPResourceNotFoundError(
                    f"Revision r{config_revision} of configuration profile "
                    f"'{config_version}' is not available (deleted, pruned, or the "
                    f"stack has no revision history)"
                )
            # Strip the storage-format marker and the discriminator; neither is
            # part of the configuration a caller edits or re-uploads.
            config_data = {
                k: v
                for k, v in body.items()
                if k not in ("_config_format", "config_type")
            }
        else:
            from idp_common.config import ConfigurationReader

            reader = ConfigurationReader(table_name=config_table)
            config_data = reader.get_configuration(
                "Config", version=config_version, as_model=False
            )

        if format == "minimal":
            from idp_common.config.merge_utils import (
                get_diff_dict,
                load_system_defaults,
            )

            if not pattern:
                classification_method = (
                    config_data.get("classification", {}).get(
                        "classificationMethod", ""
                    )
                    if config_data
                    else ""
                )
                if classification_method == "bda":
                    pattern = "pattern-1"
                else:
                    pattern = "pattern-2"

            defaults = load_system_defaults(pattern)
            config_data = get_diff_dict(defaults, config_data)

        yaml_content = yaml.dump(
            config_data,
            default_flow_style=False,
            sort_keys=False,
            allow_unicode=True,
            width=120,
        )

        if output:
            with open(output, "w", encoding="utf-8") as f:
                f.write(f"# Configuration downloaded from stack: {name}\n")
                f.write(f"# Format: {format}\n")
                if config_revision is not None:
                    # Provenance in the file itself: a downloaded revision and a
                    # downloaded head are otherwise indistinguishable on disk.
                    f.write(
                        f"# Profile: {config_version} (revision r{config_revision})\n"
                    )
                f.write("\n")
                f.write(yaml_content)

        return ConfigDownloadResult(
            config=config_data or {},
            yaml_content=yaml_content,
            output_path=output,
            revision=int(config_revision) if config_revision is not None else None,
        )

    def upload(
        self,
        config_file: str,
        config_version: Optional[str] = None,
        stack_name: Optional[str] = None,
        validate: bool = True,
        pattern: Optional[str] = None,
        description: Optional[str] = None,
        *,
        config_profile: Optional[str] = None,
        created_by: Optional[str] = None,
        revision_notes: Optional[str] = None,
        **kwargs,
    ) -> ConfigUploadResult:
        """Upload a configuration file to a deployed IDP stack.

        Args:
            config_file: Path to configuration file
            config_version: Configuration profile to upload to (e.g., "default", "v1", "v2").
                Use "default" to update the base default configuration.
                If the version doesn't exist, it will be created.
            config_profile: Configuration profile (the current name for
                config_version; either may be given, not both with different values).
            stack_name: Optional stack name override
            validate: Validate before uploading
            pattern: Pattern for validation
            description: Description for the configuration version
            revision_notes: What this upload changed, recorded on the revision and
                shown as "Notes" in the revision history — e.g. "raised topK to 20".
                Distinct from `description`, which sets the PROFILE's description and
                is overwritten by every save; this is per-revision and immutable.
                Without it a profile edited programmatically has a history of
                timestamps with no statement of intent.
            created_by: Recorded as the author of the revision this save cuts, and
                shown as "By" in the revision history. The API path derives it from
                the caller's Cognito identity; an SDK caller has no such identity,
                so it defaults to "system" — set it to something that identifies
                your automation if you want its saves attributable.
            **kwargs: Additional parameters

        Returns:
            ConfigUploadResult with upload status
        """
        config_version = resolve_config_profile(
            config_profile, config_version, required=True
        )
        import json

        import yaml

        name = self._client._require_stack(stack_name)

        try:
            with open(config_file, "r", encoding="utf-8") as f:
                content = f.read()

            if config_file.endswith(".json"):
                user_config = json.loads(content)
            else:
                user_config = yaml.safe_load(content)
        except Exception as e:
            return ConfigUploadResult(
                success=False, error=f"Failed to load config: {e}"
            )

        # Check if config has managed=true and reject it
        if isinstance(user_config, dict) and user_config.get("managed") is True:
            return ConfigUploadResult(
                success=False,
                error="Cannot upload managed configuration via CLI. Managed configs are stack-controlled and overwritten on stack updates. Remove 'managed: true' from your config or set 'managed: false'.",
            )

        # Ensure managed field is explicitly set to false for user-uploaded configs.
        # Force-write this field even if absent to protect against future model defaults
        # changing — if the ConfigurationManager later defaults managed to true,
        # user-uploaded configs would be incorrectly marked as stack-managed and
        # overwritten on stack updates.
        if isinstance(user_config, dict):
            user_config["managed"] = False

        if validate:
            result = self.validate(config_file, pattern=pattern or "pattern-2")
            if not result.valid:
                return ConfigUploadResult(
                    success=False,
                    error=f"Validation failed: {'; '.join(result.errors)}",
                )

        self._configure_config_env(name)

        try:
            from idp_common.config.configuration_manager import ConfigurationManager

            manager = ConfigurationManager()

            # Enhancement 4: check whether the version already exists and set saveAsVersion
            # flag for new versions, matching CLI config_upload behavior.
            version_exists = False
            version_created = False
            if config_version:
                try:
                    existing = manager.get_configuration(
                        "Config", version=config_version
                    )
                    version_exists = existing is not None
                except Exception:
                    version_exists = False

                if not version_exists:
                    # New version — signal ConfigurationManager to create a new version record
                    user_config["saveAsVersion"] = True
                    version_created = True

            config_json = json.dumps(user_config)
            success = manager.handle_update_custom_configuration(
                config_json,
                version=config_version,
                description=description,
                created_by=created_by,
                revision_notes=revision_notes,
            )

            # Report the revision this upload produced. Without it a caller can
            # upload iteration N and then have no way to pin the run to exactly
            # what it just uploaded — it would have to guess a number, or name a
            # whole new profile per iteration (which is what the Auto Optimizer
            # extension does today). Reading the published counter rather than
            # threading a return value through handle_update_custom_configuration
            # also gets the no-op case right: a save that changed nothing cuts no
            # new revision, and the correct answer is the revision already current.
            revision = None
            if success and config_version:
                try:
                    revision = manager.resolve_published_revision(config_version)
                except Exception as e:  # noqa: BLE001
                    logger.warning(
                        f"Uploaded '{config_version}' but could not read its "
                        f"revision number: {e}"
                    )

            return ConfigUploadResult(
                success=success,
                version=config_version,
                version_created=version_created,
                revision=revision,
                error=None if success else "Upload failed",
            )
        except Exception as e:
            return ConfigUploadResult(success=False, error=str(e))

    def list(
        self,
        stack_name: Optional[str] = None,
        **kwargs,
    ) -> ConfigListResult:
        """List all configuration versions in a deployed IDP stack.

        Args:
            stack_name: Optional stack name override
            **kwargs: Additional parameters

        Returns:
            ConfigListResult with typed list of configuration versions
        """

        name = self._client._require_stack(stack_name)
        self._configure_config_env(name)

        try:
            from idp_common.config.configuration_manager import ConfigurationManager

            manager = ConfigurationManager()
            versions_raw = manager.list_config_versions()

            versions = [
                ConfigVersionInfo(
                    version_name=v.get("versionName", v.get("version_name", str(v)))
                    if isinstance(v, dict)
                    else str(v),
                    # `bool(... or False)` rather than `.get(key, False)`: a profile
                    # record written without an IsActive attribute comes back with
                    # the key PRESENT and the value None, so the default never
                    # applied and pydantic rejected None — which took the whole
                    # command down for every profile, not just that one. Found by
                    # running config-list against a live stack.
                    is_active=bool(v.get("isActive") or v.get("is_active") or False)
                    if isinstance(v, dict)
                    else False,
                    created_at=v.get("createdAt", v.get("created_at"))
                    if isinstance(v, dict)
                    else None,
                    updated_at=v.get("updatedAt", v.get("updated_at"))
                    if isinstance(v, dict)
                    else None,
                    description=v.get("description") if isinstance(v, dict) else None,
                    managed=bool(v.get("managed") or False)
                    if isinstance(v, dict)
                    else False,
                    latest_revision=v.get("latestRevision")
                    if isinstance(v, dict)
                    else None,
                    published_revision=v.get("publishedRevision")
                    if isinstance(v, dict)
                    else None,
                )
                for v in (versions_raw or [])
            ]

            return ConfigListResult(versions=versions, count=len(versions))
        except Exception as e:
            raise IDPResourceNotFoundError(f"Failed to list configurations: {e}") from e

    def revisions(
        self,
        config_version: Optional[str] = None,
        stack_name: Optional[str] = None,
        *,
        config_profile: Optional[str] = None,
        **kwargs,
    ) -> ConfigRevisionListResult:
        """Revision history of one Configuration Profile, newest first.

        Every save of a profile cuts an immutable revision. This lists the ones
        still retained (the last 20, plus anything labeled, pinned by a test run,
        or currently in use), so a caller can see its own iterations, fetch an
        earlier one with `download(config_revision=...)`, or pin one for
        processing with `config_revision=` on `batch.process`.

        Args:
            config_version: Configuration profile whose history to list
            config_profile: Configuration profile (the current name for
                config_version; either may be given, not both with different values).
            stack_name: Optional stack name override
            **kwargs: Additional parameters

        Returns:
            ConfigRevisionListResult with the retained revisions, newest first.
            An empty list means no history — an older deployment, or a profile
            untouched since the stack was upgraded.
        """
        config_version = resolve_config_profile(
            config_profile, config_version, required=True
        )

        name = self._client._require_stack(stack_name)
        self._configure_config_env(name)

        try:
            from idp_common.config.configuration_manager import ConfigurationManager

            manager = ConfigurationManager()
            # A disabled store returns [] from every read, which would report
            # "this profile has no history" for a profile that has plenty — the
            # store just cannot see it. Say which of the two it is.
            if not manager.revisions.enabled:
                raise IDPProcessingError(
                    f"Configuration revision history is unavailable for stack "
                    f"'{name}' (no Configuration bucket resolved), so the history "
                    f"of profile '{config_version}' cannot be read. This is not the "
                    f"same as the profile having no revisions."
                )
            entries = manager.list_revisions(config_version)
        except IDPProcessingError:
            raise
        except Exception as e:
            raise IDPResourceNotFoundError(
                f"Failed to list revisions of configuration profile "
                f"'{config_version}': {e}"
            ) from e

        revisions = [
            ConfigRevisionInfo(
                revision=int(entry["revision"]),
                created_at=entry.get("createdAt"),
                created_by=entry.get("createdBy"),
                label=entry.get("label"),
                notes=entry.get("notes"),
                size_bytes=entry.get("sizeBytes"),
                published=bool(entry.get("published", False)),
                pinned=bool(entry.get("pinned", False)),
                class_fingerprint=entry.get("classFingerprint"),
            )
            for entry in (entries or [])
            if entry.get("revision") is not None
        ]
        return ConfigRevisionListResult(
            profile=config_version, revisions=revisions, count=len(revisions)
        )

    def activate(
        self,
        config_version: Optional[str] = None,
        stack_name: Optional[str] = None,
        *,
        config_profile: Optional[str] = None,
        **kwargs,
    ) -> ConfigActivateResult:
        """Activate a configuration version in a deployed IDP stack.

        If the configuration has use_bda=True, performs BDA blueprint sync
        before activation (matches CLI and Web UI behavior).

        Args:
            config_version: Configuration profile to activate
            config_profile: Configuration profile (the current name for
                config_version; either may be given, not both with different values).
            stack_name: Optional stack name override
            **kwargs: Additional parameters

        Returns:
            ConfigActivateResult with typed activation status and BDA sync details
        """
        config_version = resolve_config_profile(
            config_profile, config_version, required=True
        )
        import os

        name = self._client._require_stack(stack_name)
        self._configure_config_env(name)

        try:
            os.environ["STACK_NAME"] = name
            from idp_common.config.configuration_manager import ConfigurationManager

            manager = ConfigurationManager()

            # Check if the version exists
            existing_config = manager.get_configuration(
                "Config", version=config_version
            )
            if not existing_config:
                return ConfigActivateResult(
                    success=False,
                    activated_version=config_version,
                    error=f"Configuration version '{config_version}' does not exist",
                )

            # Enhancement 2: BDA blueprint sync before activation
            use_bda = (
                existing_config.use_bda
                if hasattr(existing_config, "use_bda")
                else False
            )

            bda_synced = False
            bda_classes_synced = 0
            bda_classes_failed = 0

            if use_bda:
                logger.info(
                    "Configuration '%s' uses BDA — performing blueprint sync before activation",
                    config_version,
                )
                try:
                    from idp_common.bda.bda_blueprint_service import BdaBlueprintService

                    bda_project_arn = manager.get_bda_project_arn(config_version)
                    bda_service = BdaBlueprintService(
                        dataAutomationProjectArn=bda_project_arn
                    )

                    if not bda_project_arn:
                        bda_project_arn = bda_service.get_or_create_project_for_version(
                            config_version
                        )
                        bda_service.dataAutomationProjectArn = bda_project_arn

                    sync_result = (
                        bda_service.create_blueprints_from_custom_configuration(
                            sync_direction="idp_to_bda",
                            version=config_version,
                            sync_mode="replace",
                        )
                    )

                    sync_failed = [
                        item for item in sync_result if item.get("status") != "success"
                    ]
                    sync_succeeded = [
                        item for item in sync_result if item.get("status") == "success"
                    ]

                    bda_classes_synced = len(sync_succeeded)
                    bda_classes_failed = len(sync_failed)

                    if bda_classes_synced == 0 and bda_classes_failed > 0:
                        # Total failure — abort activation
                        return ConfigActivateResult(
                            success=False,
                            activated_version=config_version,
                            bda_synced=False,
                            bda_classes_synced=0,
                            bda_classes_failed=bda_classes_failed,
                            error="BDA sync failed for all classes — activation aborted",
                        )
                    elif bda_classes_failed > 0:
                        # Partial failure — continue with partial sync (matching CLI behavior)
                        manager.set_bda_project_arn(
                            config_version, bda_project_arn, "partial"
                        )
                        logger.warning(
                            "BDA sync partially failed: %d succeeded, %d failed — continuing with activation",
                            bda_classes_synced,
                            bda_classes_failed,
                        )
                    else:
                        # Full success
                        manager.set_bda_project_arn(
                            config_version, bda_project_arn, "synced"
                        )

                    bda_synced = True

                except Exception as bda_exc:
                    logger.error("BDA blueprint sync raised an exception: %s", bda_exc)
                    return ConfigActivateResult(
                        success=False,
                        activated_version=config_version,
                        bda_synced=False,
                        bda_classes_synced=bda_classes_synced,
                        bda_classes_failed=bda_classes_failed,
                        error=f"BDA sync error: {bda_exc}",
                    )

            # Activate the version (BDA sync complete, or use_bda is False)
            manager.activate_version(config_version)

            return ConfigActivateResult(
                success=True,
                activated_version=config_version,
                bda_synced=bda_synced,
                bda_classes_synced=bda_classes_synced,
                bda_classes_failed=bda_classes_failed,
            )

        except IDPResourceNotFoundError:
            raise
        except IDPProcessingError:
            raise
        except Exception as e:
            return ConfigActivateResult(
                success=False,
                activated_version=config_version,
                error=str(e),
            )

    def delete(
        self,
        config_version: Optional[str] = None,
        stack_name: Optional[str] = None,
        *,
        config_profile: Optional[str] = None,
        **kwargs,
    ) -> ConfigDeleteResult:
        """Delete a configuration version from a deployed IDP stack.

        Args:
            config_version: Configuration profile to delete
            config_profile: Configuration profile (the current name for
                config_version; either may be given, not both with different values).
            stack_name: Optional stack name override
            **kwargs: Additional parameters

        Returns:
            ConfigDeleteResult with typed deletion status
        """
        config_version = resolve_config_profile(
            config_profile, config_version, required=True
        )

        name = self._client._require_stack(stack_name)
        self._configure_config_env(name)

        try:
            from idp_common.config.configuration_manager import ConfigurationManager

            manager = ConfigurationManager()
            manager.delete_configuration("Config", version=config_version)

            return ConfigDeleteResult(success=True, deleted_version=config_version)
        except IDPResourceNotFoundError:
            raise
        except Exception as e:
            return ConfigDeleteResult(
                success=False, deleted_version=config_version, error=str(e)
            )

    def sync_bda(
        self,
        direction: str = "bidirectional",
        mode: str = "replace",
        config_version: Optional[str] = None,
        stack_name: Optional[str] = None,
        *,
        config_profile: Optional[str] = None,
        **kwargs,
    ) -> ConfigSyncBdaResult:
        """Synchronize document class schemas between IDP configuration and BDA blueprints.

        Performs bidirectional or one-way synchronization between the IDP
        configuration's document classes and BDA (Bedrock Data Automation)
        blueprints.

        Args:
            direction: Sync direction — ``'bidirectional'`` (default),
                ``'bda_to_idp'``, or ``'idp_to_bda'``.
            mode: Sync mode — ``'replace'`` (default, full alignment) or
                ``'merge'`` (additive, don't delete).
            config_version: Configuration profile to sync (default: active version).
            config_profile: Configuration profile (the current name for
                config_version; either may be given, not both with different values).
            stack_name: Optional stack name override.
            **kwargs: Additional parameters.

        Returns:
            ConfigSyncBdaResult with sync status and details.
        """
        config_version = resolve_config_profile(config_profile, config_version)
        import os

        name = self._client._require_stack(stack_name)
        self._configure_config_env(name)

        try:
            os.environ["STACK_NAME"] = name
            from idp_common.bda.bda_blueprint_service import BdaBlueprintService
            from idp_common.config.configuration_manager import ConfigurationManager

            manager = ConfigurationManager()

            # Resolve config version if not provided
            if not config_version:
                for v in manager.list_config_versions():
                    if v.get("isActive"):
                        config_version = v.get("versionName")
                        break

            # Get or create BDA project ARN
            bda_project_arn = manager.get_bda_project_arn(config_version)
            bda_service = BdaBlueprintService(dataAutomationProjectArn=bda_project_arn)

            if not bda_project_arn:
                bda_project_arn = bda_service.get_or_create_project_for_version(
                    config_version
                )
                bda_service.dataAutomationProjectArn = bda_project_arn

            # Perform sync
            sync_result = bda_service.create_blueprints_from_custom_configuration(
                sync_direction=direction,
                version=config_version,
                sync_mode=mode,
            )

            # Process results
            sync_succeeded = [
                item for item in sync_result if item.get("status") == "success"
            ]
            sync_failed = [
                item for item in sync_result if item.get("status") != "success"
            ]
            processed_names = [
                item.get("class_name", item.get("name", "unknown"))
                for item in sync_result
            ]

            classes_synced = len(sync_succeeded)
            classes_failed = len(sync_failed)

            # Update BDA project ARN status
            if classes_synced > 0 and classes_failed == 0:
                manager.set_bda_project_arn(config_version, bda_project_arn, "synced")
            elif classes_synced > 0:
                manager.set_bda_project_arn(config_version, bda_project_arn, "partial")

            return ConfigSyncBdaResult(
                success=classes_failed == 0,
                direction=direction,
                mode=mode,
                classes_synced=classes_synced,
                classes_failed=classes_failed,
                processed_classes=processed_names,
                error=f"{classes_failed} class(es) failed to sync"
                if classes_failed > 0
                else None,
            )

        except Exception as e:
            logger.error(f"BDA sync failed: {e}")
            return ConfigSyncBdaResult(
                success=False,
                direction=direction,
                mode=mode,
                error=str(e),
            )
