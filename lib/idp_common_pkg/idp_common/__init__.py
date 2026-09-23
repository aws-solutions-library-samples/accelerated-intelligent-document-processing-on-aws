# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

# Use true lazy loading for all submodules
import importlib
from typing import TYPE_CHECKING

__version__ = "0.1.0"

#: Submodules reachable as `idp_common.<name>` without importing them up front.
#:
#: Lazy loading is the reason a Lambda installing `idp_common[core]` does not pull in
#: the `[all]` dependency set: none of these is imported until something asks for it,
#: so `import idp_common` costs the standard library and nothing else.
_LAZY_SUBMODULES = frozenset(
    {
        "bedrock",
        "s3",
        "dynamodb",
        "docs_service",
        "metrics",
        "image",
        "utils",
        "config",
        "ocr",
        "classification",
        "extraction",
        "evaluation",
        "assessment",
        "models",
        "reporting",
        "agents",
        "delete_documents",
    }
)

# Type hints are only evaluated during type checking, not at runtime
if TYPE_CHECKING:
    from .config import get_config as get_config
    from .config.models import IDPConfig as IDPConfig
    from .models import Document as Document
    from .models import Page as Page
    from .models import Section as Section
    from .models import Status as Status


def __getattr__(name):
    """Lazy load submodules only when accessed.

    Deferred to ``importlib``, which means to ``sys.modules``, rather than to a
    package-private dict. A second cache alongside ``sys.modules`` can hold a
    different object for the same name, and ``unittest.mock.patch`` resolves its target
    through ``sys.modules`` — so a patch applied to one copy is invisible to code
    holding the other (#1159).

    ``importlib.import_module`` also imports strictly less than the
    ``__import__(..., fromlist=["*"])`` form it replaces, which asks the import machinery
    to resolve every name in the target's ``__all__`` as a submodule. Nothing here
    relied on that, and it is the opposite of what a lazy loader is for.
    """
    if name in _LAZY_SUBMODULES:
        return importlib.import_module(f"idp_common.{name}")

    # Special handling for directly exposed functions
    if name == "get_config":
        config = __getattr__("config")
        return config.get_config

    # Special handling for directly exposed classes
    if name == "IDPConfig":
        config = __getattr__("config")
        return config.models.IDPConfig

    if name in ["Document", "Page", "Section", "Status"]:
        models = __getattr__("models")
        return getattr(models, name)

    raise AttributeError(f"module 'idp_common' has no attribute '{name}'")


__all__ = [
    "bedrock",
    "s3",
    "dynamodb",
    "docs_service",
    "metrics",
    "image",
    "utils",
    "config",
    "ocr",
    "classification",
    "extraction",
    "evaluation",
    "assessment",
    "models",
    "reporting",
    "agents",
    "delete_documents",
    "get_config",
    "IDPConfig",
    "Document",
    "Page",
    "Section",
    "Status",
]
