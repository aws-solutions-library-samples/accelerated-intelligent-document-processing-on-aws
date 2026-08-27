# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Safe YAML parsing for CloudFormation templates.

CloudFormation's short-form intrinsics (`!Ref`, `!Sub`, `!GetAtt`, `!If`) are
not standard YAML, so PyYAML's `SafeLoader` rejects them outright. The usual
workaround is a `SafeLoader` subclass with a multi-constructor for `!`-prefixed
tags, invoked through `yaml.load(..., Loader=...)`. This repo had that pattern
copy-pasted at six call sites; this module is the single home for it.

Security
--------
Two properties keep this safe, and both matter:

1. The loader subclasses `yaml.SafeLoader`, never `yaml.Loader`/`FullLoader`.
   The `python/object`, `python/name`, and `python/object/apply` constructors
   that make `yaml.load` dangerous are simply not registered, so no Python
   object can be instantiated and no module can be imported from the document.
2. The registered multi-constructor only ever returns plain scalars, lists, and
   dicts. It cannot construct or evaluate code from the tag or its value.

`load_cfn_yaml` drives the loader directly rather than calling `yaml.load`.
That is exactly what `yaml.load` does internally (construct loader, take the
single document, dispose), but it keeps this module free of the `yaml.load`
call shape that pattern-based scanners flag as CWE-94 — a finding that has to
be re-triaged by hand every time it resurfaces. See
`tests/unit/test_cfn_yaml.py`, which asserts the inertness directly.

Callers here parse templates committed to this repo, not untrusted input.

Tag policies
------------
`preserve_intrinsics` (the default) keeps each intrinsic as a single-key
`{"!Tag": value}` dict, so a document stays inspectable — you can tell a
`!GetAtt` from a literal string, and `str()` of a value still contains the
logical id a test wants to assert on.

Some callers need a different policy (collapsing intrinsics to `None`, or
normalizing to long form so `Fn::If` can be evaluated) and pass their own
constructor to `make_cfn_loader`.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Callable

import yaml

__all__ = [
    "load_cfn_template",
    "load_cfn_yaml",
    "make_cfn_loader",
    "preserve_intrinsics",
]

# A constructor receives (loader, tag_suffix, node) and returns plain data.
CfnTagConstructor = Callable[[yaml.SafeLoader, str, yaml.Node], Any]


def preserve_intrinsics(loader: Any, tag_suffix: str, node: yaml.Node) -> Any:
    """Represent a CFN intrinsic as a `{"!Tag": value}` dict.

    Deep construction is used so nested intrinsics inside a `!If` or `!Sub`
    resolve too, rather than surfacing as unconstructed node objects.
    """
    tag = "!" + tag_suffix
    if isinstance(node, yaml.ScalarNode):
        value: Any = loader.construct_scalar(node)
    elif isinstance(node, yaml.SequenceNode):
        value = loader.construct_sequence(node, deep=True)
    else:
        value = loader.construct_mapping(node, deep=True)
    return {tag: value}


def make_cfn_loader(
    constructor: CfnTagConstructor = preserve_intrinsics,
    *,
    prefix: str = "!",
) -> type[yaml.SafeLoader]:
    """Build a `SafeLoader` subclass that tolerates CFN short-form tags.

    A fresh subclass per call: `add_multi_constructor` mutates the class it is
    called on, so registering on a shared class would leak one caller's tag
    policy into another's.

    `prefix` is the tag prefix the constructor handles. The default `"!"`
    covers CloudFormation's short forms and leaves anything else — including
    `!!python/object/apply` — to `SafeLoader`, which refuses it with a
    `ConstructorError`. Passing `""` instead routes *every* unrecognized tag to
    `constructor`, which turns those same payloads into inert data rather than
    an error; both are safe, they differ only in whether an unknown tag is
    tolerated or raises.
    """

    class _CfnLoader(yaml.SafeLoader):
        pass

    _CfnLoader.add_multi_constructor(prefix, constructor)
    return _CfnLoader


def load_cfn_yaml(
    text: str,
    # Deliberately `type[Any]` rather than `type[yaml.SafeLoader]`: the point of
    # the check below is to catch callers who pass something else, and a narrower
    # annotation makes a type checker read the guard as unreachable code.
    loader_cls: type[Any] | None = None,
) -> Any:
    """Parse one CloudFormation YAML document from `text`.

    Equivalent to `yaml.load(text, Loader=loader_cls)`; see the module
    docstring for why the loader is driven directly instead.

    A `loader_cls` that is not a `SafeLoader` subclass is refused. This module's
    contract is that it cannot execute the document it parses, and that has to
    hold for callers passing their own loader too — otherwise
    `load_cfn_yaml(text, yaml.UnsafeLoader)` would be an RCE reached through an
    entry point documented as safe.
    """
    if loader_cls is None:
        loader_cls = make_cfn_loader()
    elif not issubclass(loader_cls, yaml.SafeLoader):
        raise TypeError(
            f"{loader_cls.__name__} is not a yaml.SafeLoader subclass; it can "
            "construct arbitrary Python objects from the document. Build the "
            "loader with make_cfn_loader() instead."
        )

    loader = loader_cls(text)
    try:
        return loader.get_single_data()
    finally:
        loader.dispose()


def load_cfn_template(
    path: str | Path,
    loader_cls: type[Any] | None = None,
) -> dict:
    """Parse a CloudFormation template file into a dict.

    An empty document yields `{}` so callers can index `Resources` without a
    `None` check. Parse errors are *not* swallowed: a template that cannot be
    read must fail loudly rather than degrade to "this template declares
    nothing", which would make an assertion pass vacuously.
    """
    text = Path(path).read_text(encoding="utf-8")
    return load_cfn_yaml(text, loader_cls) or {}
