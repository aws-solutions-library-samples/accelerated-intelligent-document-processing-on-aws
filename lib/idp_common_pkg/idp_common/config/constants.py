# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""
Configuration type constants.

These constants define the valid configuration types used throughout the system.
Use these instead of hardcoded strings to ensure consistency and type safety.
"""

# Configuration Types
CONFIG_TYPE_SCHEMA = "Schema"
CONFIG_TYPE_CONFIG = "Config"
DEFAULT_VERSION = "default"

# Legacy configuration types (for backward compatibility)
CONFIG_TYPE_DEFAULT = "Default"
CONFIG_TYPE_CUSTOM = "Custom"

# Pricing Configuration Types (mirrors Default/Custom pattern)
CONFIG_TYPE_DEFAULT_PRICING = "DefaultPricing"
CONFIG_TYPE_CUSTOM_PRICING = "CustomPricing"

# Model Config Limits Configuration Types (mirrors DefaultPricing/CustomPricing)
CONFIG_TYPE_DEFAULT_MODEL_CONFIG_LIMITS = "DefaultModelConfigLimits"
CONFIG_TYPE_CUSTOM_MODEL_CONFIG_LIMITS = "CustomModelConfigLimits"

# All valid configuration types
VALID_CONFIG_TYPES = [
    CONFIG_TYPE_SCHEMA,
    CONFIG_TYPE_CONFIG,
    CONFIG_TYPE_DEFAULT_PRICING,
    CONFIG_TYPE_CUSTOM_PRICING,
    CONFIG_TYPE_DEFAULT_MODEL_CONFIG_LIMITS,
    CONFIG_TYPE_CUSTOM_MODEL_CONFIG_LIMITS,
    CONFIG_TYPE_DEFAULT,  # Legacy
    CONFIG_TYPE_CUSTOM,   # Legacy
]

# Sentinel record holding a pointer to the active Configuration Profile. Read
# with a single get_item at queue time instead of scanning every profile.
ACTIVE_POINTER_VERSION = "__active"
ACTIVE_POINTER_KEY = f"{CONFIG_TYPE_CONFIG}#{ACTIVE_POINTER_VERSION}"

# Profile names that would collide with a sentinel record and must be refused.
# Without this guard a user could create a profile literally named "__active"
# and overwrite the active-profile pointer.
RESERVED_VERSION_NAMES = frozenset({ACTIVE_POINTER_VERSION})
