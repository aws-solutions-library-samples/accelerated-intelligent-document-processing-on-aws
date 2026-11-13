"""Which CloudFormation properties a resource cannot change in place.

``schemas/create_only.json`` is the ``createOnlyProperties`` list of each
resource type the templates use, read from the CloudFormation registry schemas
``cfn-lint`` ships (``scripts/refresh_create_only.py`` regenerates it). Changing
one of these properties replaces the resource, under CloudFormation and, because
``awscc`` is generated from the same registry, under ``awscc`` too. A
``hashicorp/aws`` mapping may force a replacement on more properties than the
registry lists; each mapper declares those in ``FORCE_NEW`` (``aws_mappers``).

Paths are CloudFormation property paths with ``/`` separators and no
``/properties/`` prefix. A ``*`` segment (an array element in the registry's
notation) is dropped, because the translator's walk does not index lists.
"""

from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path

CREATE_ONLY_FILE = Path(__file__).resolve().parents[1] / "schemas" / "create_only.json"


@lru_cache(maxsize=1)
def _load() -> dict[str, list[tuple[str, ...]]]:
    if not CREATE_ONLY_FILE.is_file():
        return {}
    data = json.loads(CREATE_ONLY_FILE.read_text())
    return {
        t: [tuple(s for s in p.split("/") if s and s != "*") for p in paths]
        for t, paths in data["types"].items()
    }


def known(cfn_type: str) -> bool:
    return cfn_type in _load()


def paths(cfn_type: str, extra: tuple[str, ...] = ()) -> list[tuple[str, ...]]:
    """Create-only paths of a type, plus ``extra`` (a mapper's force-new list)."""
    out = list(_load().get(cfn_type, []))
    out.extend(tuple(p.split("/")) for p in extra)
    return out


def is_create_only(path: tuple[str, ...], create_only: list[tuple[str, ...]]) -> bool:
    """True when a value at ``path`` sets a create-only property: the path is at
    or below one, or the value is a whole object containing one."""
    for c in create_only:
        n = min(len(c), len(path))
        if path[:n] == c[:n]:
            return True
    return False
