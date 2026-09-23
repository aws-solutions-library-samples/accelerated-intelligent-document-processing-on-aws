# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""
Synthesis module: bootstrap IDP configurations and synthetic, labeled test
sets from a prompt and/or example documents.

Pieces:
  * ``schema_bridge`` - convert IDP config-class schemas <-> the SEED document
    generator's ``schema.json`` format, preserving leaf field names exactly.
  * ``schema_author`` - author a document-class schema from a natural-language
    prompt (optionally adapting a catalog seed schema).
  * ``catalog`` - a unified index of reusable schema/sample assets across the
    generator's built-ins and IDP's ``config_library``.
  * ``engine`` - runtime-agnostic adapter to the SEED generator. Imported
    lazily because it pulls heavy/optional native dependencies.

The ``engine`` submodule is intentionally NOT imported here so that schema
authoring, catalog lookup and the bridge work without the generator installed.
Import it explicitly: ``from idp_common.synthesis import engine``.

Job status for a bootstrap/synthesis run is **not** reported from this package.
It is owned by the ``idp-data-generator`` extension, which writes each job's
status to its own ``BootstrapTrackingTable`` (``bootstrap-processor/index.py``
and ``agent-source/runtime/handler.py``) and serves it back over the extension's
FeatureApi as ``GET /jobs/{jobId}``, which the Web UI polls. The host has no
status channel of its own to post to.
"""

from idp_common.synthesis.schema_bridge import (
    config_class_to_generator_schema,
    field_names,
    inline_refs,
    leaf_field_paths,
)

__all__ = [
    "config_class_to_generator_schema",
    "leaf_field_paths",
    "field_names",
    "inline_refs",
]
