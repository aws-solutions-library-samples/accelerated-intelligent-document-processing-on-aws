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

import datetime
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
PRESET_GLOB = "config_library/"
KB_EMBED_TEMPLATE = "nested/bedrockkb/template.yaml"

# A Bedrock model or inference-profile id as this repo writes them: an OPTIONAL
# region/geo prefix, then `provider.model`. Anchored, so prose and ARNs are not
# mistaken for ids.
#
# The region prefix must stay optional. A bare `provider.model` id is not exotic —
# it is the ONLY form that works in GovCloud (see the comment block above
# `KnowledgeBaseModelId` in template.yaml), and the tree ships several
# deliberately: `amazon.nova-pro-v1:0` and `amazon.nova-lite-v1:0` as GovCloud
# ids, five `openai.gpt-5.x`, `qwen.qwen3-vl-235b-a22b`,
# `nvidia.nemotron-nano-12b-v2`, `google.gemma-3-27b-it`, and three embedding
# models. Requiring the prefix made 13 of 88 selectable ids invisible to every
# invariant in this file, including the entire GovCloud surface — so a bare
# end-of-life id appended to any enum passed green. A sibling gate
# (`test_pricing_lookup.py`) already handles the bare form.
# The provider segment must START WITH A LETTER and the model segment must CONTAIN
# one. Without both, `provider.model` also matches a bare decimal: `0.7` (a
# `top_p` default) was picked up as a model id the moment the region prefix became
# optional, and `v1.0`-style version strings would be too.
MODEL_ID = re.compile(
    r"^(?:(?:us|eu|apac|global|us-gov)\.)?"       # optional region / geo prefix
    r"[a-z][a-z0-9-]+\."                          # provider, e.g. amazon / openai
    r"(?=[a-z0-9.:_-]*[a-z])[a-z0-9][a-z0-9.:_-]*$"  # model, must contain a letter
)

# Values that appear in a `model` enum but are not Bedrock model ids. `LambdaHook`
# selects a customer Lambda instead of a model, so it has no price and no limits.
NON_MODEL_CHOICES = {"LambdaHook"}

# ---------------------------------------------------------------------------
# End-of-life models. NOT derivable from this tree and CI has no network, so this
# is the one hand-maintained list here. Each entry carries the EOL date and the
# command that establishes it; re-run it to re-verify. An EOL model is completely
# inaccessible in every region (AWS's model-lifecycle policy), which is why it
# must not be selectable at all — unlike a LEGACY model, which existing users can
# still invoke and which this gate deliberately does NOT flag.
#
# `eol` is a real date so a future check can act on it; `verify` is the exact
# command. All three were confirmed on 2026-09-20 against the live API.
# ---------------------------------------------------------------------------
EOL_MODELS = {
    "us.amazon.nova-premier-v1:0": {
        "was_offered": True,
        "eol": "2026-09-14",
        "verify": (
            "aws bedrock get-foundation-model --region us-east-1 "
            "--model-identifier amazon.nova-premier-v1:0"
        ),
        "note": "date from the Bedrock model card for Nova Premier",
    },
    "us.anthropic.claude-3-5-sonnet-20240620-v1:0": {
        "was_offered": True,
        "eol": "2026-09-20",
        "verify": (
            "aws bedrock get-foundation-model --region us-east-1 "
            "--model-identifier anthropic.claude-3-5-sonnet-20240620-v1:0"
        ),
        "note": (
            "past EOL, so AWS no longer publishes a model card; the date recorded "
            "is when this was confirmed, not necessarily the EOL date itself"
        ),
    },
    # Never selectable here, and recorded so it cannot be re-added. `docs/
    # configuration.md` cites it as an end-of-life example, which this keeps honest.
    "us.anthropic.claude-3-5-haiku-20241022-v1:0": {
        "was_offered": False,
        "eol": "2026-09-20",
        "verify": (
            "aws bedrock get-foundation-model --region us-east-1 "
            "--model-identifier anthropic.claude-3-5-haiku-20241022-v1:0"
        ),
        "note": "past EOL; confirmation date, not the EOL date",
    },
}

# Models this repo offers that AWS has moved to LEGACY: deprecated with a known
# EOL date, but still invocable by existing users, so they correctly stay
# selectable. Recorded because `docs/configuration.md` uses one as its worked
# example of that state, and an example that silently becomes end-of-life teaches
# the wrong thing. `review_by` is the EOL date read from
# `list-foundation-models`' `modelLifecycle.endOfLifeTime`; the test below fails
# once it is reached, which is the prompt to move the model to EOL_MODELS, pull it
# from the selectable surfaces and pick a new example.
LEGACY_EXAMPLES = {
    "us.anthropic.claude-sonnet-4-20250514-v1:0": {
        "review_by": "2026-10-14",
        "note": "endOfLifeTime 2026-10-14T08:00:00Z; cited in docs/configuration.md",
    },
    "us.anthropic.claude-opus-4-1-20250805-v1:0": {
        "review_by": "2027-01-08",
        "note": (
            "endOfLifeTime 2027-01-08T08:00:00Z; still carries quota codes in "
            "template.yaml, which is correct while it remains invocable"
        ),
    },
}

# ---------------------------------------------------------------------------
# Limits-coverage exemptions. Each one's PREMISE is asserted by a test below, so
# an exemption cannot quietly rest on a reason that has stopped being true.
# ---------------------------------------------------------------------------
# Premise: an id matching no limits pattern is degraded, not broken —
# `idp_common.bedrock.sizing` falls back to a documented conservative window.
# Every model here is selectable and Active; what is missing is a decided
# context-window number, and overstating one makes auto-sizing shard too little
# and fail at the Bedrock API, which is worse than the conservative fallback.
#
# Llama 4: AWS's own sources disagree — the model cards give 1M (Maverick) and 10M
# (Scout) while the Bedrock launch announcement gives 1M and 3.5M.
#
# Qwen3-VL, Nemotron Nano and Gemma 3 are `agents.chat_companion.model_id`
# choices carried over from earlier releases with no limits entry ever added. They
# were invisible to this gate until the bare-id fix above, which is the reason
# they are listed rather than fixed here: each needs its own looked-up number.
LIMITS_EXEMPT = {
    "us.meta.llama4-maverick-17b-instruct-v1:0",
    "us.meta.llama4-scout-17b-instruct-v1:0",
    "qwen.qwen3-vl-235b-a22b",
    "nvidia.nemotron-nano-12b-v2",
    "google.gemma-3-27b-it",
}

# Embedding models, selectable ONLY as the Bedrock Knowledge Base's embedding
# model (`pEmbedModel` in nested/bedrockkb/template.yaml). They are exempt from
# both the pricing and the limits invariants, and the premise for each is asserted
# below: an embedding model is not a text-generation model, so it has no
# max-output-tokens in the sense the limits file means, and it is not a pipeline
# stage this solution meters, so it has no `bedrock/<id>` pricing row. Asserted
# structurally — if one of these ever appears in a ConfigSchema `model` field, it
# IS a pipeline stage and the exemption fails.
EMBEDDING_MODELS = {
    "amazon.titan-embed-text-v2:0",
    "cohere.embed-english-v3",
    "cohere.embed-multilingual-v3",
}

# Premise (asserted below): not a text-generation model, so it appears in no
# `model` picklist. `us.cohere.embed-v4:0` is the Knowledge Base embedding model
# named as a code default rather than a template enum value.
NON_SELECTABLE_DEFAULTS = {
    "us.cohere.embed-v4:0": "Cohere embeddings model, not a chat model",
}


_REGION_PREFIX = re.compile(r"^(?:us|eu|apac|global|us-gov)\.")


def _base_model(model_id: str) -> str:
    """Strip any region/geo prefix, leaving the foundation-model id.

    End-of-life is a property of the FOUNDATION MODEL, not of an inference
    profile: when `amazon.nova-premier-v1:0` was withdrawn, `us.`, `eu.` and
    `global.` profiles routing to it all died with it, and the bare id is the form
    GovCloud uses. Comparing EOL by exact string therefore misses every variant
    except the one spelled in EOL_MODELS — a bare `amazon.nova-premier-v1:0`
    appended to an enum was caught only incidentally, by the pricing invariant,
    and would have passed entirely had someone added a pricing row with it.
    """
    return _REGION_PREFIX.sub("", model_id)


EOL_BASE_MODELS = {_base_model(m): m for m in EOL_MODELS}


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


def _walk_model_values(node: Any, path: str, acc: dict[str, set[str]]) -> None:
    """Collect every model-shaped string value under a `model`-ish key."""
    if isinstance(node, dict):
        for key, value in node.items():
            if isinstance(value, str) and MODEL_ID.match(value):
                acc.setdefault(value, set()).add(f"{path}.{key}")
            else:
                _walk_model_values(value, f"{path}.{key}", acc)
    elif isinstance(node, list):
        for i, item in enumerate(node):
            _walk_model_values(item, f"{path}[{i}]", acc)


@pytest.fixture(scope="module")
def code_defaults() -> dict[str, set[str]]:
    """Every model id an EFFECTIVE default resolves to, by instantiating IDPConfig.

    Instantiated, not pattern-matched. A regex over ``default="…"`` sees only the
    simple case and misses ``default_factory=lambda: X(model="…")`` — and for
    ``IDPConfig`` the ``default_factory`` is what GOVERNS, overriding the nested
    class's own ``default=``. So the regex covered the overridden default and
    missed the effective one: reverting only the ``default_factory`` to an
    end-of-life model left this file green while
    ``IDPConfig().summarization.model`` returned it, which is what a stack runs.

    Walking the instantiated tree removes that whole class of blind spot: whatever
    Pydantic actually resolves is what gets checked, regardless of how it was
    declared.
    """
    models = pytest.importorskip("idp_common.config.models")
    acc: dict[str, set[str]] = {}
    _walk_model_values(
        models.IDPConfig().model_dump(mode="python"), "IDPConfig()", acc
    )
    return {k: v for k, v in acc.items() if k not in NON_MODEL_CHOICES}


@pytest.fixture(scope="module")
def preset_models() -> dict[str, set[str]]:
    """Model ids named by the shipped configuration presets under config_library/.

    A seventh surface, and one a customer reaches directly:
    ``idp-cli deploy --custom-config config_library/unified/<preset>/config.yaml``
    installs exactly this configuration. A preset can therefore point a documented
    feature at a dead model with no enum, no UI list and no code default involved —
    which is how five `ocr-benchmark` presets came to name an end-of-life model on
    ``criteria_validation.model``, a field with no ConfigSchema entry at all and so
    reachable ONLY through a preset.
    """
    acc: dict[str, set[str]] = {}
    for rel in _tracked_files():
        if not rel.startswith(PRESET_GLOB) or not rel.endswith((".yaml", ".yml")):
            continue
        # pricing.yaml and model_config_limits.yaml are the lookup tables, not
        # presets; they legitimately name retired models and are checked elsewhere.
        if Path(rel).name in ("pricing.yaml", "model_config_limits.yaml"):
            continue
        try:
            doc = yaml.safe_load((REPO_ROOT / rel).read_text(encoding="utf-8"))
        except yaml.YAMLError:
            continue
        _walk_model_values(doc, rel, acc)
    return {k: v for k, v in acc.items() if k not in NON_MODEL_CHOICES}


# ---------------------------------------------------------------------------
# The fixtures must actually find things, or every assertion below is vacuous.
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_discovery_is_not_vacuous(
    selectable, priced, limit_patterns, ui_dropdown, code_defaults, preset_models
):
    assert len(_tracked_templates()) >= 25, "template discovery collapsed"
    assert len(selectable) >= 80, f"only {len(selectable)} selectable models found"
    assert len(priced) >= 50, f"only {len(priced)} priced models found"
    assert len(limit_patterns) >= 10
    assert len(ui_dropdown) >= 20
    assert len(code_defaults) >= 5
    assert len(preset_models) >= 5, f"only {len(preset_models)} preset models found"


@pytest.mark.unit
def test_bare_provider_ids_are_recognised():
    """The prefix-optional shape of MODEL_ID, pinned by example.

    Requiring a region prefix silently excluded 13 of 88 selectable ids — the whole
    GovCloud surface among them — so every invariant in this file skipped them and a
    bare end-of-life id appended to any enum passed green.
    """
    for bare in (
        "amazon.nova-pro-v1:0",
        "amazon.nova-lite-v1:0",
        "openai.gpt-5.6-sol",
        "qwen.qwen3-vl-235b-a22b",
        "nvidia.nemotron-nano-12b-v2",
        "google.gemma-3-27b-it",
        "amazon.titan-embed-text-v2:0",
        "anthropic.claude-3-5-sonnet-20240620-v1:0",
    ):
        assert MODEL_ID.match(bare), f"{bare} is a real id this gate must see"
    for prefixed in ("us.amazon.nova-pro-v1:0", "us-gov.anthropic.claude-sonnet-4-6"):
        assert MODEL_ID.match(prefixed), prefixed
    # Still anchored: prose, ARNs and bare words are not ids.
    for not_an_id in (
        "LambdaHook",
        "arn:aws:bedrock:us-east-1::foundation-model/amazon.nova-pro-v1:0",
        "a model id",
        "enabled",
        "us-east-1",
    ):
        assert not MODEL_ID.match(not_an_id), f"{not_an_id} must not match"


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
    missing = {
        m: sorted(paths)[:2]
        for m, paths in selectable.items()
        if m not in priced and m not in EMBEDDING_MODELS
    }
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
        and m not in EMBEDDING_MODELS
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
            # Guarded == a `try:` within the 6 lines above AND an `except` within
            # the 12 below. Deliberately a plain text window, not an indentation
            # analysis: it is a cheap tripwire for "somebody added a bare call",
            # and it can both miss an unguarded call whose try/except is further
            # away and pass one whose `try:` guards something else. A real
            # implementation would walk the AST; the value here is catching the
            # common shape, so the looseness is accepted rather than hidden.
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
        model: {
            **EOL_MODELS[EOL_BASE_MODELS[_base_model(model)]],
            "paths": sorted(paths)[:3],
        }
        for model, paths in selectable.items()
        if _base_model(model) in EOL_BASE_MODELS
    }
    assert not offenders, (
        "these models are end-of-life and completely inaccessible in every "
        "region, but can still be chosen — a customer who picks one gets a "
        "deployment that fails at inference with no useful signal: "
        f"{json.dumps(offenders, indent=2, sort_keys=True)}"
    )


@pytest.mark.unit
def test_no_eol_model_in_the_ui_dropdown(ui_dropdown):
    offenders = sorted(m for m in ui_dropdown if _base_model(m) in EOL_BASE_MODELS)
    assert not offenders, (
        f"{UI_CONSTANTS} still offers end-of-life model(s) {offenders}. The UI "
        "list is hardcoded, so removing a model from the template enum does not "
        "remove it here."
    )


@pytest.mark.unit
def test_no_eol_model_is_a_code_default(code_defaults):
    offenders = {
        m: sorted(code_defaults[m])
        for m in sorted(code_defaults)
        if _base_model(m) in EOL_BASE_MODELS
    }
    assert not offenders, (
        f"{CONFIG_MODELS} resolves a default model field to end-of-life model(s) "
        f"{json.dumps(offenders, indent=2, sort_keys=True)}. A default is worse "
        "than a picklist entry: a stored config that omits the field gets it "
        "without anyone choosing it. The paths are into the instantiated "
        "IDPConfig(), so they name the field a stack actually reads."
    )


@pytest.mark.unit
def test_eol_models_keep_their_pricing_entry(priced):
    """Removal from the selectable set must NOT remove the price.

    Pricing lookup is retrospective: a cost report over documents processed while
    the model was selectable resolves its rate by model id, so deleting the entry
    would silently re-price historical runs at zero. This is the one surface where
    an EOL model must stay.
    """
    missing = sorted(
        m for m, facts in EOL_MODELS.items() if facts["was_offered"] and m not in priced
    )
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
        dead = sorted(k for k in keys if _base_model(k) in EOL_BASE_MODELS)
        assert not dead, f"{name} has quota codes for end-of-life model(s): {dead}"
    assert found_any, "no BEDROCK_MODEL_*_QUOTA_CODES map found in template.yaml"


# ---------------------------------------------------------------------------
# 7. Shipped configuration presets — a surface a customer installs directly
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_no_eol_model_in_a_shipped_preset(preset_models):
    """`idp-cli deploy --custom-config <preset>` installs these verbatim.

    This surface has no enum and no UI list behind it, so nothing else in this file
    would ever have looked at it. Five `ocr-benchmark` presets named an end-of-life
    model on `criteria_validation.model` — a field with no ConfigSchema entry, so
    reachable only through a preset, backing a documented feature
    (docs/criteria-validation.md).
    """
    offenders = {
        m: sorted(preset_models[m])
        for m in sorted(preset_models)
        if _base_model(m) in EOL_BASE_MODELS
    }
    assert not offenders, (
        "these shipped presets name an end-of-life model, so deploying with "
        "--custom-config installs a configuration whose stage fails on every "
        f"call: {json.dumps(offenders, indent=2, sort_keys=True)}"
    )


@pytest.mark.unit
def test_preset_discovery_reaches_the_nested_config_files():
    """Not vacuous: the preset walk must reach the deep `criteria_validation.model`
    key, several levels down in a ~900-line config, or it proves nothing."""
    rel = "config_library/unified/ocr-benchmark/config.yaml"
    assert (REPO_ROOT / rel).is_file(), f"{rel} moved; update this test"
    doc = yaml.safe_load((REPO_ROOT / rel).read_text(encoding="utf-8"))
    acc: dict[str, set[str]] = {}
    _walk_model_values(doc, rel, acc)
    paths = {p for ps in acc.values() for p in ps}
    assert any(p.endswith("criteria_validation.model") for p in paths), (
        "the preset walk no longer reaches criteria_validation.model, the key "
        "whose end-of-life model this surface was added to catch"
    )


# ---------------------------------------------------------------------------
# 8. Embedding-model exemptions, premise asserted structurally
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_embedding_models_are_only_selectable_as_the_kb_embed_model(selectable):
    """The premise behind exempting them from pricing AND limits.

    They are infrastructure parameters of the Bedrock Knowledge Base, not pipeline
    stages this solution meters or size-plans. If one ever appears in a ConfigSchema
    `model` / `model_id` field it IS a pipeline stage, and both exemptions become
    unsound — so this asserts the structural fact rather than restating the reason.
    """
    for model in sorted(EMBEDDING_MODELS):
        assert model in selectable, (
            f"{model} is exempted as a KB embedding model but is no longer "
            "selectable anywhere — remove it from EMBEDDING_MODELS"
        )
        paths = sorted(selectable[model])
        offending = [p for p in paths if not p.startswith(KB_EMBED_TEMPLATE)]
        assert not offending, (
            f"{model} is exempted from the pricing and limits invariants on the "
            "premise that it is only the Knowledge Base's embedding model, but it "
            f"is now offered at {offending}. If that is a pipeline stage it needs "
            "real pricing and limits entries and must come off EMBEDDING_MODELS."
        )


# ---------------------------------------------------------------------------
# 9. The lifecycle facts this file hard-codes carry an expiry
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_legacy_examples_have_not_reached_their_eol_date():
    """A LEGACY model that this repo cites as "deprecated but still usable" stops
    being that on its EOL date, and nothing in an offline CI can notice.

    So the date AWS published is recorded and asserted to be in the future. When
    this fails it is not a code defect: it is the prompt to re-verify the model
    against the live API, move it into EOL_MODELS, pull it from every selectable
    surface, and pick a new example for docs/configuration.md.
    """
    today = datetime.date.today()
    expired = {
        model: facts
        for model, facts in LEGACY_EXAMPLES.items()
        if datetime.date.fromisoformat(facts["review_by"]) <= today
    }
    assert not expired, (
        "these models were recorded as LEGACY (deprecated but still invocable) and "
        "have now reached the end-of-life date AWS published for them, so the "
        "premise has expired. Re-verify with `aws bedrock get-foundation-model`, "
        "then move each to EOL_MODELS and remove it from the selectable surfaces: "
        f"{json.dumps(expired, indent=2, sort_keys=True)}"
    )


@pytest.mark.unit
def test_eol_entries_are_well_formed():
    """Each EOL fact carries a parseable date and a reproducible command, because
    a hand-maintained list whose provenance rots is the thing this file exists to
    prevent elsewhere."""
    assert EOL_MODELS, "EOL_MODELS is empty; every EOL assertion is vacuous"
    today = datetime.date.today()
    for model, facts in EOL_MODELS.items():
        assert MODEL_ID.match(model), f"{model} is not a model-id shape"
        recorded = datetime.date.fromisoformat(facts["eol"])
        assert recorded <= today, (
            f"{model} is listed as end-of-life on {facts['eol']}, which is in the "
            "future — a model is not EOL until its date passes, and until then it "
            "belongs in LEGACY_EXAMPLES instead"
        )
        assert isinstance(facts["was_offered"], bool), (
            f"{model} must record whether this repo ever OFFERED it: only a model "
            "that was once selectable needs its pricing entry retained"
        )
        assert facts["verify"].startswith("aws bedrock get-foundation-model"), (
            f"{model}'s evidence must be a command a reader can re-run: "
            f"{facts['verify']!r}"
        )


@pytest.mark.unit
def test_eol_matching_is_prefix_insensitive():
    """Every region/geo variant of an end-of-life model is itself end-of-life.

    Pins `_base_model`: EOL is a property of the foundation model, so `us.`, `eu.`,
    `global.` and the bare GovCloud form must all resolve to the same fact. Keying
    the EOL checks on the exact string meant only the one spelling listed in
    EOL_MODELS was caught.
    """
    assert _base_model("us.amazon.nova-premier-v1:0") == "amazon.nova-premier-v1:0"
    assert _base_model("amazon.nova-premier-v1:0") == "amazon.nova-premier-v1:0"
    assert _base_model("global.amazon.nova-premier-v1:0") == "amazon.nova-premier-v1:0"
    assert _base_model("us-gov.anthropic.claude-sonnet-4-6") == (
        "anthropic.claude-sonnet-4-6"
    )
    # A provider whose name happens to start like a region prefix is not stripped.
    assert _base_model("amazon.nova-pro-v1:0") == "amazon.nova-pro-v1:0"
    for variant in (
        "us.amazon.nova-premier-v1:0",
        "amazon.nova-premier-v1:0",
        "eu.amazon.nova-premier-v1:0",
        "global.amazon.nova-premier-v1:0",
    ):
        assert _base_model(variant) in EOL_BASE_MODELS, variant
