# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""The model surfaces agree with each other, and no dead model is reachable.

A "selectable model" is spread over five places that nothing compared:

* the ``AllowedValues`` of a CloudFormation model parameter,
* the ``enum`` of every ``model`` / ``model_id`` field in the ConfigSchema that
  drives the configuration UI's picklists,
* ``config_library/pricing.yaml``, which is how a cost report resolves a rate,
* ``config_library/model_config_limits.yaml``, which is how auto-sizing learns
  the model's context window,
* the UI's own hardcoded per-class override dropdown, whose comment already says
  "keep in sync with the model enum in patterns/unified/template.yaml" — a
  written invariant with nothing enforcing it.

and a sixth that is not selectable at all but decides what most deployments
actually run: the ``default=`` of every model field in ``idp_common``'s config
models. A config that omits the field gets that default, and no preset under
``config_library/`` sets ``summarization.model`` or either ``rule_validation``
model, so those defaults were the live values.

Three failure modes follow, all of which had shipped:

1. **A model selectable but unpriced** — cost reporting resolves nothing and
   silently reports zero for it.
2. **A model selectable but unmatched by any limits pattern** — auto-sizing falls
   back to a conservative window instead of the model's real one.
3. **A dead model still reachable** — Nova Premier (EOL 2026-09-14) was in 17
   enum positions, the UI dropdown, both quota-code maps and the summarization
   default; Claude 3.5 Sonnet 20240620 (also EOL) was the default for both
   rule-validation models. Either fails at inference with no useful signal.

Everything here is DERIVED from the files. The expected model set is never
restated: the templates are discovered by content over ``git ls-files``, the enums
are read out of them, and pricing and limits are read out of their YAML. The one
irreducibly hand-maintained fact is ``EOL_MODELS`` — whether a model is dead is
not knowable from this tree, and CI is offline, so it cannot be derived. Each
entry records the date and the command that establishes it.

Reading only ``git ls-files`` matters: walking the filesystem picks up
``.aws-sam`` build output and other worktrees, producing findings CI cannot
reproduce.
"""

from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path
from typing import Any

import pytest

yaml = pytest.importorskip("yaml")

REPO_ROOT = Path(__file__).resolve().parents[2]

PRICING = "config_library/pricing.yaml"
LIMITS = "config_library/model_config_limits.yaml"
UI_CONSTANTS = "src/ui/src/constants/schemaConstants.ts"
CONFIG_MODELS = "lib/idp_common_pkg/idp_common/config/models.py"

# A Bedrock inference-profile / model id as this repo writes them: a region or
# geo prefix, then the provider and model. Anchored, so prose and ARNs are not
# mistaken for ids.
MODEL_ID = re.compile(r"^(?:us|eu|apac|global|us-gov)\.[a-z0-9][a-z0-9.:_-]*$")

# Values that appear in a `model` enum but are not Bedrock model ids. `LambdaHook`
# selects a customer Lambda instead of a model, so it has no price and no limits.
NON_MODEL_CHOICES = {"LambdaHook"}

# ---------------------------------------------------------------------------
# End-of-life models. NOT derivable from this tree and CI has no network, so this
# is the one hand-maintained list here. Each entry names the evidence; re-run the
# command to re-verify. An EOL model is completely inaccessible in every region
# (AWS's model-lifecycle policy), which is why it must not be selectable at all —
# unlike a LEGACY model, which existing users can still invoke and which this
# gate deliberately does NOT flag.
# ---------------------------------------------------------------------------
EOL_MODELS = {
    "us.amazon.nova-premier-v1:0": (
        "EOL 2026-09-14 per the Bedrock model card for Nova Premier. Verify: "
        "aws bedrock get-foundation-model --region us-east-1 "
        "--model-identifier amazon.nova-premier-v1:0  -> ResourceNotFoundException "
        "'This model version has reached the end of its life'"
    ),
    "us.anthropic.claude-3-5-sonnet-20240620-v1:0": (
        "EOL as of 2026-09-20; past its EOL date, so AWS no longer publishes a "
        "model card. Verify: aws bedrock get-foundation-model --region us-east-1 "
        "--model-identifier anthropic.claude-3-5-sonnet-20240620-v1:0  -> "
        "ResourceNotFoundException 'This model version has reached the end of its life'"
    ),
}

# ---------------------------------------------------------------------------
# Limits-coverage exemptions. Each one's PREMISE is asserted by a test below, so
# an exemption cannot quietly rest on a reason that has stopped being true.
# ---------------------------------------------------------------------------
# Premise: an id matching no limits pattern is not broken — idp_common.bedrock
# .sizing falls back to a documented conservative window. These two are selectable
# and Active, and their AWS model cards give 1M (Maverick) and 10M (Scout) token
# context windows while the Bedrock launch announcement gives 1M and 3.5M. Adding
# a pattern means choosing between two AWS-published numbers, and overstating the
# input window makes auto-sizing shard too little and fail at the API, so the
# conservative fallback is deliberately kept until the number is settled.
LIMITS_EXEMPT = {
    "us.meta.llama4-maverick-17b-instruct-v1:0",
    "us.meta.llama4-scout-17b-instruct-v1:0",
}

# Premise (asserted below): these are not text-generation models, so they appear
# in no `model` picklist and need no limits entry. An embeddings model has no
# max-output-tokens in the sense the limits file means.
NON_SELECTABLE_DEFAULTS = {
    "us.cohere.embed-v4:0": "Titan/Cohere-style embeddings model, not a chat model",
}


# ---------------------------------------------------------------------------
# Loading
# ---------------------------------------------------------------------------


class _CfnLoader(yaml.SafeLoader):
    """SafeLoader that tolerates CloudFormation short-form tags."""


def _tag_to_python(loader, tag_suffix, node):
    if isinstance(node, yaml.ScalarNode):
        return {f"Fn::{tag_suffix}": loader.construct_scalar(node)}
    if isinstance(node, yaml.SequenceNode):
        return {f"Fn::{tag_suffix}": loader.construct_sequence(node, deep=True)}
    return {f"Fn::{tag_suffix}": loader.construct_mapping(node, deep=True)}


_CfnLoader.add_multi_constructor("!", _tag_to_python)


def _tracked_files() -> list[str]:
    out = subprocess.run(
        ["git", "ls-files"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    return [line for line in out.splitlines() if line]


def _tracked_templates() -> list[str]:
    """Templates discovered by CONTENT, matching `make cfn-lint`'s convention, so
    a new template carrying a model enum cannot be added without being covered."""
    found = []
    for rel in _tracked_files():
        if not rel.endswith((".yaml", ".yml")):
            continue
        try:
            text = (REPO_ROOT / rel).read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        if "AWSTemplateFormatVersion" in text:
            found.append(rel)
    return found


def _walk_enums(node: Any, path: str, acc: dict[str, set[str]]) -> None:
    """Collect every model-shaped string in an `enum` or `AllowedValues` list."""
    if isinstance(node, dict):
        for key, value in node.items():
            if key in ("enum", "AllowedValues") and isinstance(value, list):
                for item in value:
                    if isinstance(item, str) and MODEL_ID.match(item):
                        acc.setdefault(item, set()).add(f"{path}.{key}")
            _walk_enums(value, f"{path}.{key}", acc)
    elif isinstance(node, list):
        for item in node:
            _walk_enums(item, path, acc)


@pytest.fixture(scope="module")
def selectable() -> dict[str, set[str]]:
    """model id -> the enum paths that offer it, over every tracked template."""
    acc: dict[str, set[str]] = {}
    for rel in _tracked_templates():
        doc = yaml.load(
            (REPO_ROOT / rel).read_text(encoding="utf-8"), Loader=_CfnLoader
        )
        _walk_enums(doc, rel, acc)
    return acc


@pytest.fixture(scope="module")
def priced() -> set[str]:
    doc = yaml.safe_load((REPO_ROOT / PRICING).read_text(encoding="utf-8"))
    return {
        entry["name"][len("bedrock/") :]
        for entry in doc["pricing"]
        if isinstance(entry, dict)
        and isinstance(entry.get("name"), str)
        and entry["name"].startswith("bedrock/")
    }


@pytest.fixture(scope="module")
def limit_patterns() -> list[str]:
    doc = yaml.safe_load((REPO_ROOT / LIMITS).read_text(encoding="utf-8"))
    return [entry["pattern"] for entry in doc["model_limits"]]


@pytest.fixture(scope="module")
def ui_dropdown() -> set[str]:
    text = (REPO_ROOT / UI_CONSTANTS).read_text(encoding="utf-8")
    values = set(re.findall(r"value:\s*'([^']*)'", text)) | set(
        re.findall(r'value:\s*"([^"]*)"', text)
    )
    return {v for v in values if MODEL_ID.match(v)}


@pytest.fixture(scope="module")
def code_defaults() -> set[str]:
    text = (REPO_ROOT / CONFIG_MODELS).read_text(encoding="utf-8")
    return {
        v
        for v in re.findall(r'default="([^"]+)"', text)
        if MODEL_ID.match(v) and v not in NON_MODEL_CHOICES
    }


# ---------------------------------------------------------------------------
# The fixtures must actually find things, or every assertion below is vacuous.
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_discovery_is_not_vacuous(
    selectable, priced, limit_patterns, ui_dropdown, code_defaults
):
    assert len(_tracked_templates()) >= 25, "template discovery collapsed"
    assert len(selectable) >= 50, f"only {len(selectable)} selectable models found"
    assert len(priced) >= 50, f"only {len(priced)} priced models found"
    assert len(limit_patterns) >= 10
    assert len(ui_dropdown) >= 20
    assert len(code_defaults) >= 5


@pytest.mark.unit
def test_template_discovery_reads_only_tracked_files():
    """Untracked build output and sibling worktrees produce findings CI cannot
    reproduce, which is why discovery goes through `git ls-files`."""
    for rel in _tracked_templates():
        assert not rel.startswith(".aws-sam"), rel
        assert ".aws-sam/" not in rel, rel
        rc = subprocess.run(
            ["git", "ls-files", "--error-unmatch", rel],
            cwd=REPO_ROOT,
            capture_output=True,
        ).returncode
        assert rc == 0, f"{rel} is not tracked by git"


# ---------------------------------------------------------------------------
# 1. Selectable implies priced
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_every_selectable_model_has_a_price(selectable, priced):
    missing = {m: sorted(paths)[:2] for m, paths in selectable.items() if m not in priced}
    assert not missing, (
        "these models can be selected but have no bedrock/<id> entry in "
        f"{PRICING}, so every cost report that includes them under-reports "
        f"silently: {json.dumps(missing, indent=2, sort_keys=True)}"
    )


# ---------------------------------------------------------------------------
# 2. Selectable implies a limits pattern (with premise-checked exemptions)
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_every_selectable_model_matches_a_limits_pattern(selectable, limit_patterns):
    unmatched = sorted(
        m
        for m in selectable
        if m not in LIMITS_EXEMPT
        and not any(re.search(p, m, re.IGNORECASE) for p in limit_patterns)
    )
    assert not unmatched, (
        f"these models are selectable but match no pattern in {LIMITS}, so "
        "auto-sizing uses a fallback window rather than the model's real one. "
        "Add a pattern with the number from the model card, or add to "
        f"LIMITS_EXEMPT with a premise this file asserts: {unmatched}"
    )


@pytest.mark.unit
def test_limits_exemptions_are_still_selectable_and_still_unmatched(
    selectable, limit_patterns
):
    """The exemption's own premise, part one: the entry is real and still needed.

    A stale exemption is worse than none — it names a model the check would now
    pass anyway, so a reader trusts a carve-out that is doing nothing.
    """
    for model in sorted(LIMITS_EXEMPT):
        assert model in selectable, (
            f"{model} is exempted from limits coverage but is no longer "
            "selectable — remove it from LIMITS_EXEMPT"
        )
        assert not any(
            re.search(p, model, re.IGNORECASE) for p in limit_patterns
        ), (
            f"{model} now matches a limits pattern, so the exemption is stale — "
            "remove it from LIMITS_EXEMPT"
        )


@pytest.mark.unit
def test_unmatched_model_is_degraded_not_broken():
    """The exemption's own premise, part two: the consequence really is a
    documented conservative fallback, not a crash.

    This is the whole reason the Llama 4 entries are tolerable, and it is NOT
    obvious: the resolver itself, ``get_model_max_output_tokens``, *raises*
    ``ValueError`` for an unmatched model. The premise rests entirely on every
    caller catching that — so this asserts the behaviour for the exempt ids
    directly, rather than inspecting one fallback constant and assuming the rest.
    """
    sizing = pytest.importorskip("idp_common.bedrock.sizing")
    model_utils = pytest.importorskip("idp_common.bedrock.model_utils")

    for model in sorted(LIMITS_EXEMPT):
        # The mechanism: the resolver really does reject these.
        with pytest.raises(ValueError):
            model_utils.get_model_max_output_tokens(model)
        # The consequence: sizing still yields a usable positive window, and
        # honestly reports that it did not resolve it.
        max_in, max_out, resolved = sizing._resolve_limits(model)
        assert max_in > 0 and max_out > 0, (
            f"sizing yields a non-positive window for exempt model {model}, so "
            "the LIMITS_EXEMPT premise ('degraded, not broken') is false"
        )
        assert resolved is False, (
            f"sizing reports {model} as resolved, which contradicts it matching "
            "no limits pattern — one of the two is wrong"
        )


@pytest.mark.unit
def test_every_limits_resolver_caller_handles_the_unknown_model_raise():
    """The exemption's own premise, part three: no caller is left unguarded.

    ``get_model_max_output_tokens`` raises for an unmatched model, so a call site
    that does not catch turns "no limits entry" into a hard failure and makes
    LIMITS_EXEMPT unsound. Call sites are discovered from the tracked sources
    rather than listed, so a new one is covered as soon as it is written.
    """
    unguarded = []
    for rel in _tracked_files():
        if not rel.endswith(".py") or "/tests/" in rel or rel.startswith("scripts/"):
            continue
        text = (REPO_ROOT / rel).read_text(encoding="utf-8", errors="ignore")
        if "get_model_max_output_tokens(" not in text:
            continue
        lines = text.splitlines()
        for i, line in enumerate(lines):
            stripped = line.strip()
            if "get_model_max_output_tokens(" not in line or "def " in line:
                continue
            # Not real call sites: the import, docstring doctest examples and
            # comments. `>>>` lines in particular are documentation of the happy
            # path and are never executed.
            if "import" in line or stripped.startswith((">>>", "...", "#")):
                continue
            # A guarded call sits inside a try: whose except is within a few
            # lines below; look for the nearest preceding `try:` at a shallower
            # or equal indent and an `except` after the call.
            window_before = "\n".join(lines[max(0, i - 6) : i])
            window_after = "\n".join(lines[i : i + 12])
            if "try:" in window_before and "except" in window_after:
                continue
            unguarded.append(f"{rel}:{i + 1}: {line.strip()}")
    assert not unguarded, (
        "these call sites invoke get_model_max_output_tokens without catching "
        "the ValueError it raises for a model absent from "
        f"{LIMITS}, so an unmatched model would hard-fail rather than degrade "
        f"(which is what LIMITS_EXEMPT assumes): {unguarded}"
    )


# ---------------------------------------------------------------------------
# 3. No end-of-life model is reachable
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_no_eol_model_is_selectable(selectable):
    offenders = {
        model: {"reason": reason, "paths": sorted(selectable[model])[:3]}
        for model, reason in EOL_MODELS.items()
        if model in selectable
    }
    assert not offenders, (
        "these models are end-of-life and completely inaccessible in every "
        "region, but can still be chosen — a customer who picks one gets a "
        "deployment that fails at inference with no useful signal: "
        f"{json.dumps(offenders, indent=2, sort_keys=True)}"
    )


@pytest.mark.unit
def test_no_eol_model_in_the_ui_dropdown(ui_dropdown):
    offenders = sorted(set(ui_dropdown) & set(EOL_MODELS))
    assert not offenders, (
        f"{UI_CONSTANTS} still offers end-of-life model(s) {offenders}. The UI "
        "list is hardcoded, so removing a model from the template enum does not "
        "remove it here."
    )


@pytest.mark.unit
def test_no_eol_model_is_a_code_default(code_defaults):
    offenders = sorted(set(code_defaults) & set(EOL_MODELS))
    assert not offenders, (
        f"{CONFIG_MODELS} defaults a model field to end-of-life model(s) "
        f"{offenders}. A default is worse than a picklist entry: a stored config "
        "that omits the field gets it without anyone choosing it."
    )


@pytest.mark.unit
def test_eol_models_keep_their_pricing_entry(priced):
    """Removal from the selectable set must NOT remove the price.

    Pricing lookup is retrospective: a cost report over documents processed while
    the model was selectable resolves its rate by model id, so deleting the entry
    would silently re-price historical runs at zero. This is the one surface where
    an EOL model must stay.
    """
    missing = sorted(m for m in EOL_MODELS if m not in priced)
    assert not missing, (
        f"end-of-life model(s) {missing} have no entry in {PRICING}, so cost "
        "reports covering documents processed while they were selectable now "
        "resolve no rate and under-report. Keep the entry; only the selectable "
        "surfaces get cleaned."
    )


# ---------------------------------------------------------------------------
# 4. The UI dropdown does not outrun the templates
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_ui_dropdown_is_a_subset_of_the_template_enums(ui_dropdown, selectable):
    """`schemaConstants.ts` says "keep in sync with the model enum in
    patterns/unified/template.yaml". The dropdown is deliberately a curated
    SUBSET (it omits the EU/global/`:priority` variants), so equality is not the
    invariant — but a value it offers that no enum accepts is, and that is exactly
    what a removal leaves behind.
    """
    extra = sorted(set(ui_dropdown) - set(selectable))
    assert not extra, (
        f"{UI_CONSTANTS} offers model(s) no template enum accepts: {extra}. "
        "Either add them to the enums or drop them from the dropdown."
    )


# ---------------------------------------------------------------------------
# 5. Code defaults are live and, where they are picklist models, selectable
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_every_code_default_is_selectable_or_premise_checked(
    code_defaults, selectable
):
    unexplained = sorted(
        m
        for m in code_defaults
        if m not in selectable and m not in NON_SELECTABLE_DEFAULTS
    )
    assert not unexplained, (
        f"{CONFIG_MODELS} defaults a model field to {unexplained}, which no "
        "template enum offers. The UI cannot represent the value, and a config "
        "omitting the field runs a model nobody could have chosen. Point the "
        "default at a selectable model, or record why it is not selectable in "
        "NON_SELECTABLE_DEFAULTS."
    )


@pytest.mark.unit
def test_non_selectable_default_exemptions_are_still_needed(
    code_defaults, selectable
):
    """The exemption's own premise: still a default, and still not selectable."""
    for model, why in NON_SELECTABLE_DEFAULTS.items():
        assert model in code_defaults, (
            f"{model} is recorded in NON_SELECTABLE_DEFAULTS ({why}) but is no "
            f"longer a default in {CONFIG_MODELS} — remove the entry"
        )
        assert model not in selectable, (
            f"{model} is now selectable, so the NON_SELECTABLE_DEFAULTS entry is "
            "stale — remove it"
        )


# ---------------------------------------------------------------------------
# 6. A removed model leaves nothing behind in the quota-code maps
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_quota_code_maps_hold_no_eol_model():
    """The parent template's BEDROCK_MODEL_*_QUOTA_CODES are JSON blobs keyed by
    model id, used to look up a model's TPM/RPM Service Quotas code when
    calculating capacity.

    This checks only for END-OF-LIFE keys, not for merely non-selectable ones.
    Like ``pricing.yaml``, these maps are legitimately a SUPERSET of the currently
    selectable set: they are consulted for whatever model a *deployed stack's*
    stored configuration names, which includes models that were selectable in an
    earlier release and are now LEGACY — still invocable by existing users, so
    still needing a quota lookup. ``us.anthropic.claude-opus-4-1-20250805-v1:0``
    is exactly that case and is correct to keep. An EOL model is different: it
    cannot be invoked in any region, so no capacity calculation for it can ever
    run, and a key for it reads as support for a model that is gone.
    """
    text = (REPO_ROOT / "template.yaml").read_text(encoding="utf-8")
    found_any = False
    for name, blob in re.findall(
        r"(BEDROCK_MODEL_(?:RPM_)?QUOTA_CODES):\s*'(\{.*?\})'\s*$",
        text,
        re.MULTILINE,
    ):
        found_any = True
        keys = {k for k in json.loads(blob) if MODEL_ID.match(k)}
        dead = sorted(keys & set(EOL_MODELS))
        assert not dead, f"{name} has quota codes for end-of-life model(s): {dead}"
    assert found_any, "no BEDROCK_MODEL_*_QUOTA_CODES map found in template.yaml"
