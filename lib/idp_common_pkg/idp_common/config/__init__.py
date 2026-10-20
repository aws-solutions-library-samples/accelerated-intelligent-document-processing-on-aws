# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

import boto3
import os
from typing import Dict, Any, Optional, Union, overload, Literal
from botocore.exceptions import ClientError
import logging
from .configuration_manager import ConfigurationManager
from .models import (
    IDPConfig,
    ConfigurationRecord,
    ExtractionConfig,
    ClassificationConfig,
    ConfidenceConfig,
    GeometryConfig,
    HITLConfig,
    SchemaConfig,
    SummarizationConfig,
    ChatConfig,
    OCRConfig,
    AgenticConfig,
    ImageConfig,
    ModelConfigLimitsConfig,
    PricingConfig,
)
from .constants import (
    CONFIG_TYPE_SCHEMA,
    CONFIG_TYPE_DEFAULT,
    CONFIG_TYPE_CUSTOM,
    CONFIG_TYPE_DEFAULT_PRICING,
    CONFIG_TYPE_CUSTOM_PRICING,
    VALID_CONFIG_TYPES,
)

logger = logging.getLogger(__name__)


class ConfigurationReader:
    def __init__(self, table_name=None, region=None):
        """
        Initialize the configuration reader using the table name from environment variable or parameter

        Args:
            table_name: Optional override for configuration table name
            region: Optional AWS region for the underlying clients. ``None``
                   leaves it to boto3, which is correct in Lambda. An
                   out-of-region caller (the CLI with an explicit ``--region``)
                   must pass it — see ConfigurationManager's region docstring.
        """
        # Use ConfigurationManager for all operations (with built-in migration)
        self.manager = ConfigurationManager(table_name, region=region)
        logger.info(f"Initialized ConfigurationReader with ConfigurationManager")

    # `as_dict` carries its default here, matching the implementation below and the
    # docstring's "If True (default)". Without it neither overload accepted a call
    # that omitted `as_dict`, so the documented default form resolved to no overload
    # at all — the same defect as `get_merged_configuration` further down, which had
    # callers and was therefore the one that showed up as an error.
    @overload
    def get_configuration(
        self,
        config_type: str,
        *,
        as_dict: Literal[True] = True,
        version: Optional[str] = None,
    ) -> Optional[Dict[str, Any]]: ...

    @overload
    def get_configuration(
        self, config_type: str, *, as_dict: Literal[False], version: Optional[str] = None
    ) -> Optional[Union[IDPConfig, SchemaConfig, PricingConfig]]: ...

    def get_configuration(
        self, config_type: str, *, as_dict: bool = True, as_model: bool = False, version: Optional[str] = None
    ) -> Optional[
        Union[
            Dict[str, Any],
            IDPConfig,
            SchemaConfig,
            PricingConfig,
            ModelConfigLimitsConfig,
        ]
    ]:
        """
        Retrieve a configuration item from DynamoDB with automatic migration

        Args:
            config_type: The configuration type to retrieve ('Default' or 'Custom')
            as_dict: If True (default), return raw dictionary for backward compatibility
            as_model: If True, return model object (overrides as_dict)
            version: Optional version to retrieve

        Returns:
            Configuration dictionary if found (auto-migrated if needed), None otherwise
        """
        # Handle parameter precedence: as_model overrides as_dict
        if as_model:
            as_dict = False
        
        # ConfigurationManager now returns IDPConfig by default
        idp_config = self.manager.get_configuration(config_type, version=version)

        if idp_config is None:
            return None

        # Convert to dict if requested (for backward compatibility)
        if as_dict:
            config_dict = idp_config.model_dump(mode="python")
            # Add Configuration key back for backward compatibility
            config_dict["Configuration"] = config_type
            return config_dict

        return idp_config


    # `simple_merge(default, custom)` was here: a `deep_update` of a deepcopied
    # default with no migration of `custom`. It had no callers, and it is deleted
    # rather than kept because config format 0.8 turned it from unused into a
    # trap.
    #
    # ⚠️ The hazard is specific. A configuration stored by an earlier release
    # carries `classificationMethod` and NO `granularity` key at all, and the
    # 0.8 migration's "an explicit granularity wins" rule is `if not
    # classification.get("granularity")`. The shipped default is `page`. So on
    # any path that merges such a delta onto the defaults BEFORE migrating, that
    # `page` arrives as the delta's own value, shadows `classificationMethod:
    # textbasedHolisticClassification`, and silently runs page classification on
    # a configuration asking for packet.
    #
    # Every live path migrates first, deliberately:
    # `merge_utils.merge_config_with_defaults` migrates the delta at the top (its
    # docstring says "IMPORTANT — migrate BEFORE merge" about exactly this),
    # `ConfigurationManager.get_raw_configuration` and
    # `get_merged_configuration` both migrate before their `deep_update`, and
    # `update_configuration`'s handler goes through `merge_config_with_defaults`.
    # Leaving a merge-first helper lying around is how a future caller reopens
    # it; use `merge_config_with_defaults`, which takes `migrate=`.


    @overload
    def get_merged_configuration(
        self, *, as_model: Literal[True], version: Optional[str] = None, revision: Optional[int] = None
    ) -> IDPConfig: ...

    # `as_model` carries its default here, matching the implementation below and
    # the `get_config` overloads further down this file. Without it, neither
    # overload accepts a call that omits `as_model`, so every caller relying on
    # the documented dict default resolved to no overload at all.
    @overload
    def get_merged_configuration(
        self, *, as_model: Literal[False] = False, version: Optional[str] = None, revision: Optional[int] = None
    ) -> Dict[str, Any]: ...

    def get_merged_configuration(
        self, *, as_model: bool = False, version: Optional[str] = None, revision: Optional[int] = None
    ) -> Union[IDPConfig, Dict[str, Any]]:
        """
        Get the full configuration for a version, ready for runtime processing.

        Each version stores a complete, self-contained configuration.
        Legacy sparse delta configs are auto-detected and migrated on first read.

        This is THE method to use for all runtime document processing.

        Args:
            as_model: If True, return IDPConfig Pydantic model. If False (default), return dict.
            version: Optional Configuration Profile to load. If None, uses the active one.
            revision: Optional revision of that profile to load instead of its current
                configuration, for a document or test run pinned to a revision.

        Returns:
            Full configuration as IDPConfig or dictionary
        """
        try:
            # Use ConfigurationManager.get_merged_configuration which handles:
            # - An explicitly pinned revision: read that revision's body
            # - Full config format (new): direct read
            # - Legacy sparse format: merge with default + auto-migrate
            config = self.manager.get_merged_configuration(version=version, revision=revision)

            if config is None:
                raise ValueError(f"Configuration not found for version: {version}")

            if as_model:
                return config

            # Return as dict
            return config.model_dump(mode="python")

        except Exception as e:
            logger.error(f"Error getting merged configuration: {str(e)}")
            raise

@overload
def get_config(
    *,
    table_name: Optional[str] = None,
    as_model: Literal[True],
    version: Optional[str] = None,
    revision: Optional[int] = None,
) -> IDPConfig:
    """
    Get configuration as Pydantic model.

    Use config.to_dict() to convert to mutable dict with extra fields:
        config = get_config(as_model=True)
        config_dict = config.to_dict(sagemaker_endpoint_name=endpoint)
    """
    ...


@overload
def get_config(
    *,
    table_name: Optional[str] = None,
    as_model: Literal[False] = False,
    version: Optional[str] = None,
    revision: Optional[int] = None,
) -> Dict[str, Any]:
    """Get configuration as mutable dictionary."""
    ...


def get_config(
    *,
    table_name: Optional[str] = None,
    as_model: bool = False,
    version: Optional[str] = None,
    revision: Optional[int] = None,
    region: Optional[str] = None,
) -> Union[IDPConfig, Dict[str, Any]]:
    """
    Get the merged configuration using the environment variable for table name.

    Args:
        table_name: Optional override for configuration table name
        region: Optional AWS region for the underlying clients. None defers to
            boto3, which is correct in Lambda; an out-of-region caller must pass it.
        as_model: If True, return IDPConfig Pydantic model. If False (default), return dict.
        version: Optional Configuration Profile to load. If None, uses the active one.
        revision: Optional revision of that profile. Pass document.config_revision so a
            document keeps processing under the configuration it was queued with, even
            if someone saves the profile mid-flight.
    Returns:
        Merged configuration as IDPConfig (with .to_dict() helper) or mutable dictionary.

    Examples:
        # Get as dict for direct manipulation
        config = get_config(as_model=False)
        config["extra_field"] = "value"

        # Get as model, convert to dict with extras
        config = get_config(as_model=True)
        config_dict = config.to_dict(sagemaker_endpoint_name=endpoint)
    """
    reader = ConfigurationReader(table_name, region=region)
    return reader.get_merged_configuration(as_model=as_model, version=version, revision=revision)
