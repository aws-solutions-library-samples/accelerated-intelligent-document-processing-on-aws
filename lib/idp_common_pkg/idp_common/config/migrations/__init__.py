# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Config-shape migrations.

Each migration is a pure ``dict -> dict`` transform that upgrades a stored
configuration from one ``config_format_version`` to the next. Migrations are
idempotent so they can be safely applied on every read (see
``IDPConfig.log_deprecated_fields`` and ``ConfigurationManager``).

Migrations chain in order: a stored v0.5 config is brought to the current format
by applying each step in sequence, so every call site should apply the whole
chain via ``migrate_config`` rather than picking a single step.
"""

from .v05_to_v06 import migrate_v05_to_v06
from .v06_to_v07 import migrate_v06_to_v07

__all__ = ["migrate_config", "migrate_v05_to_v06", "migrate_v06_to_v07"]


def migrate_config(config):
    """Apply every migration in order, bringing ``config`` to the current format.

    Call this rather than an individual step: a v0.5 config needs both hops, and
    a call site that applies only one silently leaves the config a version
    behind. Each step is itself idempotent and a no-op when not needed, so this
    is safe to call on every read.
    """
    return migrate_v06_to_v07(migrate_v05_to_v06(config))
