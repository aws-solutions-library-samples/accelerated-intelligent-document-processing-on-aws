# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""idp_feature_sdk — Publisher for IDP Accelerator feature packages."""

from .manifest import FeatureManifest, ManifestError, load_manifest
from .publisher import FeaturePublisher, PublishResult

__all__ = [
    "FeatureManifest",
    "FeaturePublisher",
    "ManifestError",
    "PublishResult",
    "load_manifest",
]
__version__ = "0.6.7"
