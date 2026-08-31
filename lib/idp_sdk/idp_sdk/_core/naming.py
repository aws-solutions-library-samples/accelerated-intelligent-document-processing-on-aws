# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Transitional naming: `config_profile` is the new name for `config_version`.

The product calls the named configuration entity a **Configuration Profile**. The
SDK and CLI were built when it was called a "configuration version", so
`config_version` / `--config-version` are baked into every existing script anyone
has written against them.

Rather than rename and break those, both names are accepted at the public boundary
and normalized here. The internal plumbing keeps ONE name (`config_version`) so
the alias does not multiply through every private helper — a translation layer at
the edge, not a second vocabulary running in parallel.

Deliberately **not** aliased:

- GraphQL fields and stored attributes (`versionName`, `ConfigVersion`,
  `allowedConfigVersions`). Those are load-bearing for RBAC scope checks and for
  data already written; aliasing them means dual reads/writes and a migration, not
  a keyword argument.
- `config_revision`, which was named correctly from the start.

No `DeprecationWarning` is raised yet: warning on `config_version` today would nag
every existing script for a rename that has no deadline. When `config_profile` is
the documented default everywhere and has shipped for a release or two, a warning
is the natural next step.
"""

from __future__ import annotations

from typing import Optional


def resolve_config_profile(
    config_profile: Optional[str] = None,
    config_version: Optional[str] = None,
    required: bool = False,
) -> Optional[str]:
    """
    Collapse the new and old argument names into one value.

    Args:
        config_profile: The new name.
        config_version: The former name, still accepted.
        required: Set by the three operations that cannot run without a profile
            (upload, activate, delete). Their `config_version` parameter used to
            be a required positional, which meant `config_profile=` alone raised
            a bare "missing argument" TypeError naming the OLD parameter — so the
            alias did not actually work there. The parameter is now optional in
            the signature and the requirement is enforced here instead, where the
            error can name both spellings.

    Returns:
        The profile name, or None when neither was supplied and `required` is False.

    Raises:
        ValueError: If both are supplied with different values. Picking one
            silently would run the caller's work against a configuration they did
            not ask for, which is the failure this whole feature exists to prevent.
            Also raised when `required` and neither name was supplied.
    """
    if config_profile is not None and config_version is not None:
        if config_profile != config_version:
            raise ValueError(
                "config_profile and config_version are two names for the same "
                f"argument, but were given different values "
                f"({config_profile!r} and {config_version!r}). Pass only "
                f"config_profile — config_version is the former name."
            )
        resolved = config_profile
    else:
        resolved = config_profile if config_profile is not None else config_version

    if required and resolved is None:
        raise ValueError(
            "config_profile is required (config_version is the former name of "
            "the same argument and is still accepted)."
        )
    return resolved
