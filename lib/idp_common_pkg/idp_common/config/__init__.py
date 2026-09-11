# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

import boto3
import os
from typing import Dict, Any, Optional, Union, overload, Literal
from botocore.exceptions import ClientError
import logging
from copy import deepcopy
from .configuration_manager import ConfigurationManager
from .merge_utils import deep_update
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
    def __init__(self, table_name=None):
        """
        Initialize the configuration reader using the table name from environment variable or parameter

        Args:
            table_name: Optional override for configuration table name
        """
        # Use ConfigurationManager for all operations (with built-in migration)
        self.manager = ConfigurationManager(table_name)
        logger.info(f"Initialized ConfigurationReader with ConfigurationManager")

    @overload
    def get_configuration(
        self, config_type: str, *, as_dict: Literal[True], version: Optional[str] = None
    ) -> Optional[Dict[str, Any]]: ...

    @overload
    def get_configuration(
        self, config_type: str, *, as_dict: Literal[False], version: Optional[str] = None
    ) -> Optional[Union[IDPConfig, SchemaConfig, PricingConfig]]: ...

    def get_configuration(
        self, config_type: str, *, as_dict: bool = True, as_model: bool = False, version: Optional[str] = None
    ) -> Optional[Union[Dict[str, Any], IDPConfig, SchemaConfig, PricingConfig]]:
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


    def simple_merge(
        self, default: Dict[str, Any], custom: Dict[str, Any]
    ) -> Dict[str, Any]:
        """
        Deep merge with custom values overriding defaults.

        Custom configuration should only contain fields that differ from default,
        not a complete configuration tree. Nested dicts are merged recursively.

        Args:
            default: The default configuration dictionary
            custom: The custom overrides dictionary

        Returns:
            Merged configuration dictionary (default updated with custom)
        """
        from copy import deepcopy

        merged = deepcopy(default)
        return deep_update(merged, custom)
    
    @overload
    def get_merged_configuration(
        self, *, as_model: Literal[True], version: Optional[str] = None, revision: Optional[int] = None
    ) -> IDPConfig: ...

    @overload
    def get_merged_configuration(
        self, *, as_model: Literal[False], version: Optional[str] = None, revision: Optional[int] = None
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
) -> Union[IDPConfig, Dict[str, Any]]:
    """
    Get the merged configuration using the environment variable for table name.

    Args:
        table_name: Optional override for configuration table name
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
    reader = ConfigurationReader(table_name)
    return reader.get_merged_configuration(as_model=as_model, version=version, revision=revision)
