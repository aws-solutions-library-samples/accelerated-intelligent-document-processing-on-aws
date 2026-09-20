#!/usr/bin/env python3
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Ask Bedrock whether any model this repository OFFERS has been retired.

The completeness ratchet the retired-model registry otherwise lacks. The offline
gates enforce that a model in ``idp_common.config.retired_models.RETIRED_MODELS``
is not selectable; nothing offline can notice a model AWS retired that was never
added to the registry in the first place. That is not hypothetical — it had already
happened **six** times when this script was written: Nova Premier, Claude 3.5 Sonnet
20240620, Claude 3.5 Sonnet 20241022, Claude 3.7 Sonnet, Claude 3 Haiku and Claude
Opus 4 were all offered and all already dead. Three of them were live
``MODEL_MAPPINGS`` targets, so an EU deployment was being rewritten onto a dead
model, and two were the default for a stage nobody had to choose.

Deliberately shaped like ``scripts/sdlc/check_branch_protection.py``:

* **opt-in and non-blocking.** It needs network and credentials, so it must not be
  a required CI gate — a branch would red-line for a condition nobody can fix
  offline. Run it periodically, or before a release.
* **exits 0 when it cannot answer.** No credentials, no network, or no
  ``bedrock:GetFoundationModel`` permission all produce an explanation and a clean
  exit, so putting it in a pipeline cannot break one. ``--fail-on-skip`` turns that
  into an error for a caller that wants a definite answer.
* **read-only.** One ``GetFoundationModel`` call per distinct foundation model.

The authoritative signal is ``GetFoundationModel`` answering
``ResourceNotFoundException`` whose message contains "reached the end of its life".
Two things it deliberately does NOT treat as evidence:

* ``ResourceNotFoundException: Model not found`` — that is "not offered in this
  region", which is a different fact. Claude 3.5 Sonnet 20241022 answers exactly
  that in eu-west-1 while answering end-of-life in us-east-1.
* an inference profile's ``status``. ``get-inference-profile
  us.amazon.nova-premier-v1:0`` still reports ``ACTIVE`` for a model that is
  definitively dead: the profile record lags the foundation model, which is the
  trap that let a retired model stay selectable in the first place.

Usage::

    make check-retired-models                 # human-readable report
    python3 scripts/sdlc/check_retired_models.py --json
    python3 scripts/sdlc/check_retired_models.py --region us-west-2 --fail-on-skip
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[2]

# Same shape as the offline gate's. Kept here rather than imported from a test
# module: this is a script, and tests are not importable API.
MODEL_ID = re.compile(
    r"^(?:(?:us|eu|apac|global|us-gov)\.)?"
    r"[a-z][a-z0-9-]+\."
    r"(?=[a-z0-9.:_-]*[a-z])[a-z0-9][a-z0-9.:_-]*$"
)
_REGION_PREFIX = re.compile(r"^(?:us|eu|apac|global|us-gov)\.")

#: Trailing suffixes that select a service tier or a context window rather than a
#: different model. ``GetFoundationModel`` does not accept them — it answers
#: ``ValidationException: The provided model identifier is invalid`` — so they must
#: be stripped or every ``:flex`` / ``:priority`` / ``:1m`` id reports as
#: unresolvable noise and buries the one line that matters. Order matters: the
#: longest first, and note a real id already contains a colon (``v1:0``), so only
#: these exact endings are removed.
_TIER_SUFFIXES = (":priority", ":flex", ":1m")

#: Model families served by the ``bedrock-mantle`` Responses API rather than
#: ``bedrock-runtime``. ``bedrock:GetFoundationModel`` does not know them, so their
#: lifecycle cannot be read this way and reporting them as unresolvable would be
#: misleading. Named explicitly so the gap is visible rather than silent.
_NON_BEDROCK_RUNTIME_PREFIXES = ("openai.",)

EOL_MARKER = "reached the end of its life"


def _tracked_templates() -> list[Path]:
    """Templates discovered by CONTENT over ``git ls-files``.

    Same discovery as ``make cfn-lint`` and the offline gate, so this script and
    that gate see the same set of offered models. Reading only tracked files keeps
    build output and sibling worktrees out.
    """
    listing = subprocess.run(
        ["git", "ls-files"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    found = []
    for rel in listing.splitlines():
        if not rel.endswith((".yaml", ".yml")):
            continue
        path = REPO_ROOT / rel
        try:
            text = path.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        if "AWSTemplateFormatVersion" in text:
            found.append(path)
    return found


def _offered_model_ids() -> set[str]:
    """Every model id a template offers, from an enum, AllowedValues or Default."""
    import yaml

    class _CfnLoader(yaml.SafeLoader):
        pass

    def _tag(loader, suffix, node):  # tolerate !Ref / !Sub / !GetAtt
        if isinstance(node, yaml.ScalarNode):
            return {f"Fn::{suffix}": loader.construct_scalar(node)}
        if isinstance(node, yaml.SequenceNode):
            return {f"Fn::{suffix}": loader.construct_sequence(node, deep=True)}
        return {f"Fn::{suffix}": loader.construct_mapping(node, deep=True)}

    _CfnLoader.add_multi_constructor("!", _tag)

    ids: set[str] = set()

    def walk(node: Any, path: str) -> None:
        if isinstance(node, dict):
            for key, value in node.items():
                if key in ("enum", "AllowedValues") and isinstance(value, list):
                    ids.update(
                        v for v in value if isinstance(v, str) and MODEL_ID.match(v)
                    )
                if (
                    key == "Default"
                    and isinstance(value, str)
                    and MODEL_ID.match(value)
                    and ".Parameters." in f"{path}.{key}"
                ):
                    ids.add(value)
                walk(value, f"{path}.{key}")
        elif isinstance(node, list):
            for item in node:
                walk(item, path)

    for template in _tracked_templates():
        walk(
            yaml.load(template.read_text(encoding="utf-8"), Loader=_CfnLoader),
            str(template.relative_to(REPO_ROOT)),
        )
    return ids


def _registry() -> dict[str, dict[str, Any]]:
    sys.path.insert(0, str(REPO_ROOT / "lib" / "idp_common_pkg"))
    from idp_common.config.retired_models import RETIRED_MODELS

    return dict(RETIRED_MODELS)


def _base(model_id: str) -> str:
    """The foundation-model id: no region/geo prefix, no service-tier suffix.

    Every regional variant and every tier variant of a model shares one lifecycle,
    so collapsing to this both avoids redundant calls and avoids reporting a tier
    suffix as an unknown model.
    """
    stripped = _REGION_PREFIX.sub("", model_id)
    for suffix in _TIER_SUFFIXES:
        if stripped.endswith(suffix):
            stripped = stripped[: -len(suffix)]
    return stripped


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument(
        "--region",
        default="us-east-1",
        help="Region to query (default us-east-1). Lifecycle is global, but the "
        "model catalogue is per-region, so a model absent from this region reports "
        "as unknown rather than retired.",
    )
    ap.add_argument("--profile", default=None, help="AWS profile to use")
    ap.add_argument("--json", action="store_true", help="Machine-readable output")
    ap.add_argument(
        "--fail-on-skip",
        action="store_true",
        help="Exit non-zero when the answer could not be obtained, instead of 0",
    )
    args = ap.parse_args()

    registry = _registry()
    registry_bases = {_base(m) for m in registry}
    offered = _offered_model_ids()
    # One call per distinct FOUNDATION model: every regional variant of an id
    # resolves to the same lifecycle.
    all_bases = {_base(m) for m in offered} - registry_bases
    not_readable = sorted(
        b for b in all_bases if b.startswith(_NON_BEDROCK_RUNTIME_PREFIXES)
    )
    to_check = sorted(all_bases - set(not_readable))

    try:
        import boto3
        import botocore.exceptions
    except ImportError as exc:
        return _skip(f"boto3 is not installed ({exc})", args)

    try:
        session = (
            boto3.Session(profile_name=args.profile)
            if args.profile
            else boto3.Session()
        )
        client = session.client("bedrock", region_name=args.region)
    except Exception as exc:  # noqa: BLE001 — any setup failure is "cannot answer"
        return _skip(f"could not create a Bedrock client: {exc}", args)

    retired: list[dict[str, str]] = []
    unknown: list[dict[str, str]] = []
    alive: list[str] = []

    for base in to_check:
        try:
            details = client.get_foundation_model(modelIdentifier=base)
            status = (
                details.get("modelDetails", {}).get("modelLifecycle", {}).get("status")
            )
            alive.append(f"{base} ({status})")
        except botocore.exceptions.NoCredentialsError:
            return _skip("no AWS credentials available", args)
        except botocore.exceptions.EndpointConnectionError as exc:
            return _skip(f"no network access to Bedrock: {exc}", args)
        except botocore.exceptions.ClientError as exc:
            code = exc.response.get("Error", {}).get("Code", "")
            message = exc.response.get("Error", {}).get("Message", "")
            if code in ("AccessDeniedException", "UnauthorizedOperation"):
                return _skip(
                    f"these credentials lack bedrock:GetFoundationModel ({message})",
                    args,
                )
            if code == "ResourceNotFoundException" and EOL_MARKER in message:
                retired.append({"model": base, "message": message})
            else:
                # "Model not found" means "not offered in this region", which is a
                # DIFFERENT fact and must not be reported as retirement.
                unknown.append({"model": base, "reason": f"{code}: {message}"})

    result = {
        "region": args.region,
        "offered_models": len(offered),
        "foundation_models_checked": len(to_check),
        "already_in_registry": len(registry),
        "retired_but_still_offered": retired,
        "unknown": unknown,
        "not_readable_via_this_api": not_readable,
        "active": alive,
    }

    if args.json:
        print(json.dumps(result, indent=2, sort_keys=True))
    else:
        print(
            f"Checked {len(to_check)} foundation model(s) offered by this repo and "
            f"not already in the registry, in {args.region}.\n"
            f"({len(offered)} offered ids resolve to those; {len(registry)} already "
            f"registered as retired.)"
        )
        if retired:
            print(
                f"\n❌ {len(retired)} model(s) are RETIRED but still offered. Add each "
                f"to RETIRED_MODELS in\n"
                f"   lib/idp_common_pkg/idp_common/config/retired_models.py, then let "
                f"the offline gates\n   name every surface still offering it:\n"
            )
            for item in retired:
                print(f"     {item['model']}\n       {item['message']}")
        else:
            print("\n✅ No offered model has been retired.")
        if not_readable:
            print(
                f"\nℹ {len(not_readable)} model(s) are served via the bedrock-mantle "
                f"Responses API, whose\n  lifecycle bedrock:GetFoundationModel cannot "
                f"report. Not covered by this check:\n"
            )
            for model in not_readable:
                print(f"     {model}")
        if unknown:
            print(
                f"\n⚠ {len(unknown)} model(s) could not be resolved in {args.region}. "
                f"'Model not found' means\n  the model is not offered in this region, "
                f"which is NOT retirement — re-check in a\n  region that offers it "
                f"before concluding anything:\n"
            )
            for item in unknown:
                print(f"     {item['model']}: {item['reason']}")

    return 1 if retired else 0


def _skip(reason: str, args: argparse.Namespace) -> int:
    """Report that no answer was obtained, and exit 0 unless asked otherwise.

    Exiting 0 is what keeps this safe to run anywhere: it needs network and
    credentials, so a clean exit when it has neither is the difference between an
    opt-in check and one that red-lines every offline branch.
    """
    payload = {"skipped": True, "reason": reason}
    if args.json:
        print(json.dumps(payload, indent=2))
    else:
        print(f"⏭  Skipped: {reason}")
        print(
            "   This check needs network access and read-only "
            "bedrock:GetFoundationModel.\n"
            "   Exiting 0 so it is safe to run offline; pass --fail-on-skip to make "
            "this an error."
        )
    return 1 if args.fail_on_skip else 0


if __name__ == "__main__":
    sys.exit(main())
