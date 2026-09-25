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

        Also bridges the resolved region into the environment, as the backstop for
        clients built too deep in `idp_common` to be handed one explicitly. The
        constructors that a CLI command reaches directly take `region=` (see
        `ConfigurationManager`), but some are several frames down with no region in
        scope — `bedrock.model_utils._load_model_limits_from_dynamodb`, on
        `config-upload`'s own validation path, builds a `ConfigurationManager` with
        no caller able to pass one. Without this bridge that read lands in the
        ambient region, fails, and is swallowed by a total `except`, so the upload
        validates against on-disk default limits instead of the stack's and can
        reject a config that is legitimately above a default cap.

        `AWS_DEFAULT_REGION` (not `AWS_REGION`) because it is the variable boto3
        consults for a client built with no explicit region, which is exactly the
        case being covered. `_core/publish.py` uses the same bridge for the same
        reason. Only set when a region was actually requested — writing it
        unconditionally would pin the process to `None`.

        Returns the configuration table's physical ID.
        """
        import os

        region = self._client._region
        if region:
            os.environ["AWS_DEFAULT_REGION"] = region

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

    @staticmethod
    def _validation_unavailable(cause: ImportError) -> ConfigValidationResult:
        """The verdict for an installation that cannot run the checks at all.

        `valid` is False, because nothing about the configuration was established
        and a caller that gates on `valid` must keep refusing: `upload(validate=True)`
        is such a caller, and reporting True here would store an unchecked
        configuration. `validation_available` is what separates this from a
        configuration that was checked and found wrong — the distinction a caller
        needs and cannot get from `valid`, and one it should not have to read out of
        an error message.
        """
        return ConfigValidationResult(
            valid=False,
            validation_available=False,
            errors=[
                # "Validation" is load-bearing in this sentence: `idp-cli
                # config-upload` keys its `--no-validate` hint on the word, and that
                # hint is the useful one here — a component only the checks need can
                # be missing while everything the upload itself needs is present.
                f"Validation unavailable in this installation: {cause}. This "
                "environment is missing part of idp_common, which performs the "
                "checks."
            ],
        )

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
            and unknown_fields populated when extra keys are found. An installation
            that cannot run the checks at all is reported the same way — as a result
            with `validation_available` False and the missing component named in
            `errors` — rather than as an exception out of a method whose contract is
            to return a verdict.
        """
        from pathlib import Path

        import yaml

        # `idp_common` does the checking, and a method whose contract is to return a
        # verdict has to answer when it is not importable rather than raise out of a
        # `from` statement. `idp_common` is an unconditional requirement of this
        # package, so an install that resolved cannot be missing it — what this
        # covers is an environment assembled by other means, which is the ordinary
        # case for the Lambda packages here (a handler tree plus a pruned copy of the
        # library, sized against the deployment limit) and for anything vendoring a
        # subset. `examples/lambda_function.py` calls this method from a handler and
        # serialises the result.
        #
        # Both frames below can raise, and which one does depends on WHICH part is
        # missing rather than on how much: the import fails for anything
        # `idp_common.config`'s own `__init__` pulls in — `models` included, since it
        # imports it — while the modules the validation stack alone imports, and
        # imports lazily (`bedrock.prompt_cache`, `config.migrations`,
        # `config.hook_reachability`, `schema.multi_instance`), surface out of
        # `validate_config` on the second. Both are caught by ImportError's TYPE
        # rather than by module name or message text: the spellings differ per
        # module and per cause, and a rule written against any of them misses the
        # rest.
        try:
            from idp_common.config.merge_utils import load_yaml_file, validate_config
        except ImportError as e:
            return ConfigOperation._validation_unavailable(e)

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

        try:
            result = validate_config(user_config, pattern=pattern)
        except ImportError as e:
            return ConfigOperation._validation_unavailable(e)

        # Keys the configuration models will not read, at every depth, as found by
        # validate_config. Computing `set(config) - set(IDPConfig.model_fields)` here
        # instead — which this did — reports every key IDPConfig does not declare, so
        # it named two the loader honours: `description`, which update_configuration
        # pops and stores, and `rule_classes`, which is renamed to `policy_classes`.
        # It also saw only the top level, where a typo is least likely. The warnings
        # already carry these findings in prose, with the path and, where there is
        # one, the field the key was meant to be.
        errors = list(result.get("errors", []))
        warnings = list(result.get("warnings", []))
        ignored = result.get("ignored_keys", [])
        deprecated_fields = sorted(
            f["path"] for f in ignored if f["kind"] == "deprecated"
        )
        unknown_fields = sorted(f["path"] for f in ignored if f["kind"] == "unknown")

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

            manager = ConfigurationManager(region=self._client._region)
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

            manager = ConfigurationManager(region=self._client._region)
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

            reader = ConfigurationReader(
                table_name=config_table, region=self._client._region
            )
            # `as_dict=True` is the declared way to ask for the dict form. The
            # implementation also takes an undeclared `as_model`, but it only has
            # an effect when True, so `as_model=False` asked for nothing.
            config_data = reader.get_configuration(
                "Config", version=config_version, as_dict=True
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
                # The gate holds either way — an unchecked configuration is not
                # stored — but the two refusals ask for different things from the
                # operator: one is a file to fix, the other is a component to
                # install. Saying "validation failed" for the second sends them
                # looking through a configuration that may be perfectly good.
                reason = (
                    "Validation failed"
                    if result.validation_available
                    else "Validation unavailable"
                )
                return ConfigUploadResult(
                    success=False,
                    error=f"{reason}: {'; '.join(result.errors)}",
                )

        self._configure_config_env(name)

        try:
            from idp_common.config.configuration_manager import ConfigurationManager

            # `region=` is not optional decoration. _configure_config_env above
            # resolved the ConfigurationTable NAME from CloudFormation in
            # self._client._region, and a DynamoDB table name is not
            # region-qualified — so a manager built without the region reads and
            # writes that name in whatever region the ambient credentials resolve
            # to. On a multi-region account that is a successful write to another
            # stack's configuration table, reported as success. Every
            # ConfigurationManager in this module is constructed the same way for
            # the same reason.
            manager = ConfigurationManager(region=self._client._region)

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

            manager = ConfigurationManager(region=self._client._region)
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

            manager = ConfigurationManager(region=self._client._region)
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

        # Bound here, before the try, rather than beside the other BDA locals inside
        # it: the handler of last resort below has to be able to name it, and a local
        # first assigned inside the try is unbound for every exception raised before
        # that point — a `NameError` from inside an exception handler replaces the error
        # the caller actually needs to see.
        bda_service = None
        bda_orphaned_arns: list = []
        bda_classes_synced = 0
        bda_classes_failed = 0

        try:
            os.environ["STACK_NAME"] = name
            from idp_common.config.configuration_manager import ConfigurationManager

            manager = ConfigurationManager(region=self._client._region)

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
            # `bda_classes_synced`, `bda_classes_failed`, `bda_orphaned_arns` and
            # `bda_service` are bound before the try above. The orphan list holds
            # blueprints the sync took out of the project but could not delete: they
            # belong to no class, so they are absent from the per-class status list and
            # do not count as a class failure, and they are reported so that a caller
            # told the activation succeeded — or that it failed — also learns that
            # cleanup is outstanding.

            if use_bda:
                logger.info(
                    "Configuration '%s' uses BDA — performing blueprint sync before activation",
                    config_version,
                )
                try:
                    from idp_common.bda.bda_blueprint_service import BdaBlueprintService

                    bda_project_arn = manager.get_bda_project_arn(config_version)
                    bda_service = BdaBlueprintService(
                        dataAutomationProjectArn=bda_project_arn,
                        region=self._client._region,
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
                    bda_orphaned_arns = list(bda_service.orphaned_blueprint_arns)
                    if bda_orphaned_arns:
                        logger.error(
                            "BDA sync left %d orphaned blueprint(s) in the account; "
                            "run the orphaned-blueprint cleanup to remove them: %s",
                            len(bda_orphaned_arns),
                            bda_orphaned_arns,
                        )

                    if bda_classes_synced == 0 and bda_classes_failed > 0:
                        # Total failure — abort activation. The orphan list is carried
                        # here too: the deletes run whatever happened to the classes,
                        # so this is the outcome most likely to have left one, and it
                        # is also the one where nothing else in the result mentions it.
                        return ConfigActivateResult(
                            success=False,
                            activated_version=config_version,
                            bda_synced=False,
                            bda_classes_synced=0,
                            bda_classes_failed=bda_classes_failed,
                            bda_orphaned_blueprint_arns=bda_orphaned_arns,
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
                    # Read the orphans off the service rather than trusting the local:
                    # the deletes happen before the last two steps of a sync, both of
                    # which can raise, so a sync that never returned may still have
                    # left one — and in that case the local is untouched.
                    if bda_service is not None:
                        bda_orphaned_arns = list(bda_service.orphaned_blueprint_arns)
                    return ConfigActivateResult(
                        success=False,
                        activated_version=config_version,
                        bda_synced=False,
                        bda_classes_synced=bda_classes_synced,
                        bda_classes_failed=bda_classes_failed,
                        bda_orphaned_blueprint_arns=bda_orphaned_arns,
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
                bda_orphaned_blueprint_arns=bda_orphaned_arns,
            )

        except IDPResourceNotFoundError:
            raise
        except IDPProcessingError:
            raise
        except Exception as e:
            # `manager.activate_version()` is outside the inner BDA handler, so a
            # throttle or a denial on that write lands here — after a sync that may
            # already have left a blueprint orphaned. Read the orphans off the service
            # for the same reason the inner handler does, and carry the class counts
            # too: a result that reports 0 synced and 0 failed after a sync that ran is
            # a third wrong answer.
            if bda_service is not None:
                bda_orphaned_arns = list(bda_service.orphaned_blueprint_arns)
            return ConfigActivateResult(
                success=False,
                activated_version=config_version,
                bda_classes_synced=bda_classes_synced,
                bda_classes_failed=bda_classes_failed,
                bda_orphaned_blueprint_arns=bda_orphaned_arns,
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

            manager = ConfigurationManager(region=self._client._region)
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

        ``'cleanup_orphaned'`` is not a sync: it deletes every blueprint carrying the
        stack's name prefix that no class in the named profile accounts for. That is an
        **account-wide** scan rather than a project-scoped one, which is what makes it
        the only way to remove a blueprint a replace-mode sync disassociated but could
        not delete — such a blueprint is invisible to every project-scoped read. It
        reports its outcome in ``cleanup_deleted_count`` and ``cleanup_failed_count``
        rather than in the class counts, because it processes no classes and reporting
        a blueprint as a synced class is a wrong answer rather than an imprecise one.

        Args:
            direction: Sync direction — ``'bidirectional'`` (default),
                ``'bda_to_idp'``, ``'idp_to_bda'``, or ``'cleanup_orphaned'``.
            mode: Sync mode — ``'replace'`` (default, full alignment) or
                ``'merge'`` (additive, don't delete). Not read for
                ``'cleanup_orphaned'``, which deletes by definition.
            config_version: Configuration profile to sync (default: active version).
                For ``'cleanup_orphaned'`` this is the profile whose classes decide
                which blueprints are orphaned, so naming the wrong one deletes live
                blueprints.
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

        bda_service = None

        try:
            os.environ["STACK_NAME"] = name
            from idp_common.bda.bda_blueprint_service import BdaBlueprintService
            from idp_common.config.configuration_manager import ConfigurationManager

            manager = ConfigurationManager(region=self._client._region)

            # Resolve config version if not provided
            if not config_version:
                for v in manager.list_config_versions():
                    if v.get("isActive"):
                        config_version = v.get("versionName")
                        break

            # Get or create BDA project ARN
            bda_project_arn = manager.get_bda_project_arn(config_version)
            bda_service = BdaBlueprintService(
                dataAutomationProjectArn=bda_project_arn,
                region=self._client._region,
            )

            if not bda_project_arn:
                bda_project_arn = bda_service.get_or_create_project_for_version(
                    config_version
                )
                bda_service.dataAutomationProjectArn = bda_project_arn

            # Orphaned-blueprint cleanup is not a sync and shares none of the steps
            # below: it processes no classes, writes no project blueprint list from the
            # configuration, and its outcome is a count of deletions. It is placed
            # after the project-ARN resolution above for the same reason the resolver
            # places it there (sync_bda_idp_resolver/index.py): the cleanup
            # disassociates before deleting, which needs a project to disassociate
            # from.
            if direction == "cleanup_orphaned":
                cleanup = bda_service.cleanup_orphaned_blueprints(
                    version=config_version
                )
                deleted = int(cleanup.get("deleted_count", 0))
                failed = int(cleanup.get("failed_count", 0))
                succeeded = bool(cleanup.get("success", False)) and failed == 0
                if not succeeded:
                    logger.error(
                        "Orphaned blueprint cleanup did not complete: %d deleted, "
                        "%d failed. %s",
                        deleted,
                        failed,
                        cleanup.get("message", ""),
                    )
                return ConfigSyncBdaResult(
                    success=succeeded,
                    direction=direction,
                    mode=mode,
                    cleanup_deleted_count=deleted,
                    cleanup_failed_count=failed,
                    # The blueprints the cleanup could not delete are still orphaned
                    # after it ran, so they are reported for the same reason a sync
                    # reports them: nothing project-scoped will ever show them again.
                    orphaned_blueprint_arns=list(bda_service.orphaned_blueprint_arns),
                    error=(
                        cleanup.get("message")
                        or f"{failed} orphaned blueprint(s) could not be deleted"
                    )
                    if not succeeded
                    else None,
                )

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
            # `class` is read by subscript rather than through a chain of
            # defaults ending in a literal. Every entry the sync appends carries
            # it, on all seven paths that build one, and the two aggregators
            # inside the service subscript that same key to assemble them — so a
            # default here could only ever fire on a future rename, and a default
            # is precisely what let this field report a placeholder for every
            # class indefinitely: a list of plausible-looking names reads as an
            # answer, so nothing ever went looking. A rename is now a sync that
            # reports failure and names the key it could not read, which no
            # caller can mistake for the name of a document class.
            #
            # Re-raised as a `RuntimeError` rather than re-raising the `KeyError`:
            # the handler below stringifies whatever comes out into `error`, which
            # the CLI prints, and `str()` of a `KeyError` is the message wrapped in
            # quotes. Nothing reads the type — this method converts every exception
            # into a result object — so the message is the whole payload.
            try:
                processed_names = [item["class"] for item in sync_result]
            except KeyError as missing_key:
                raise RuntimeError(
                    f"A BDA sync status entry carries no {missing_key} key, so "
                    "the classes the sync processed cannot be named. Those "
                    "entries are produced by "
                    "BdaBlueprintService.create_blueprints_from_custom_configuration; "
                    "a rename of that key has to be made here too."
                ) from missing_key

            classes_synced = len(sync_succeeded)
            classes_failed = len(sync_failed)
            # Blueprints removed from the project that could not then be deleted. Not
            # a class failure — they belong to no class and every class may have
            # synced — so they are reported alongside the result rather than folded
            # into `classes_failed`, which would misreport a class as unsynced.
            orphaned_arns = list(bda_service.orphaned_blueprint_arns)
            if orphaned_arns:
                logger.error(
                    "BDA sync left %d orphaned blueprint(s) in the account; run the "
                    "orphaned-blueprint cleanup to remove them: %s",
                    len(orphaned_arns),
                    orphaned_arns,
                )

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
                orphaned_blueprint_arns=orphaned_arns,
                error=f"{classes_failed} class(es) failed to sync"
                if classes_failed > 0
                else None,
            )

        except Exception as e:
            logger.error(f"BDA sync failed: {e}")
            # A sync that raised may still have deleted — or failed to delete —
            # blueprints: the deletes happen before the last two steps, both of which
            # can raise. So the orphans are read here as well, off the service, since
            # a raise means the normal return path did not run.
            failed_orphans = (
                list(bda_service.orphaned_blueprint_arns)
                if bda_service is not None
                else []
            )
            if failed_orphans:
                logger.error(
                    "The failed BDA sync left %d orphaned blueprint(s) in the "
                    "account; run the orphaned-blueprint cleanup to remove them: %s",
                    len(failed_orphans),
                    failed_orphans,
                )
            return ConfigSyncBdaResult(
                success=False,
                direction=direction,
                mode=mode,
                orphaned_blueprint_arns=failed_orphans,
                error=str(e),
            )
