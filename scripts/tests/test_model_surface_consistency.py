# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""The model surfaces agree with each other, and no dead model is reachable.

Which model a deployment can run is decided across SEVEN places that nothing
compared:

1. the ``AllowedValues`` of a CloudFormation model parameter — and its
   ``Default:``, which is a model a customer receives without choosing anything,
   and which four templates set on a parameter carrying no ``AllowedValues`` at
   all;
2. the ``enum`` of every ``model`` / ``model_id`` field in the ConfigSchema that
   drives the configuration UI's picklists;
3. ``config_library/pricing.yaml``, which is how a cost report resolves a rate;
4. ``config_library/model_config_limits.yaml``, which is how auto-sizing learns
   the model's context window;
5. the UI's own hardcoded per-class override dropdown, whose comment already says
   "keep in sync with the model enum in patterns/unified/template.yaml" — a
   written invariant with nothing enforcing it;
6. the resolved model defaults of ``idp_common``'s ``IDPConfig``, which decide
   what most deployments actually run: no shipped preset sets
   ``summarization.model`` or either ``rule_validation`` model, so the default IS
   the live value, and nobody chose it;
7. the shipped configuration presets under ``config_library/``, which
   ``idp-cli deploy --custom-config`` installs verbatim. This surface can point a
   documented feature at a dead model with no enum, no UI entry and no code
   default involved — ``criteria_validation.model`` has no ConfigSchema entry at
   all, so a preset is the only way to reach it.

Three failure modes follow, all of which had shipped:

1. **A model selectable but unpriced** — cost reporting resolves nothing and
   silently reports zero for it.
2. **A model selectable but unmatched by any limits pattern** — auto-sizing falls
   back to a conservative window instead of the model's real one.
3. **A dead model still reachable** — Nova Premier (EOL 2026-09-14) was in 17
   enum positions, the UI dropdown, both quota-code maps and the summarization
   default; Claude 3.5 Sonnet 20240620 (also EOL) was the default for both
   rule-validation models and named by five shipped presets. Either fails at
   inference with no useful signal.

Everything here is DERIVED from the files. The expected model set is never
restated: templates are discovered by content over ``git ls-files``, enums are
read out of them, pricing and limits out of their YAML, presets out of
``config_library/``, and defaults from the config models two ways at once —
because neither way alone is enough. Instantiating ``IDPConfig`` and walking the
resolved tree is needed since ``default_factory`` overrides a nested class's own
``default=``, so a regex over ``default=`` polices the overridden value and misses
the effective one. But the instance cannot reach a field behind
``Optional[X] = None``, which is where four model defaults live — including the two
this change found an end-of-life model on. So the declared field defaults of every
BaseModel are collected as well, and the two results unioned.

The one irreducibly hand-maintained fact is which models are dead, and it lives in
**shipped code** (``idp_common.config.retired_models``) rather than here, so that
``validate_config`` and the #708 gate read the same registry instead of keeping
their own copies — which had already produced two lists encoding contradictory
policies. ``LEGACY_EXAMPLES`` below gives the deprecated-but-still-usable models an
expiry date so a hard-coded lifecycle fact cannot rot silently.

Reading only ``git ls-files`` matters: walking the filesystem picks up
``.aws-sam`` build output and other worktrees, producing findings CI cannot
reproduce. ``code_defaults`` asserts the same thing about the ``idp_common`` it
imports, because that package is an editable install: without ``PYTHONPATH``
pointing at this checkout it would read defaults from one tree and file surfaces
from another.

**Two known gaps, latent today.** ``_walk_model_values`` recurses into a list but
does not inspect a plain string *inside* one, so a model id in a list of strings
(rather than as a mapping value) is not collected. ``_walk_enums`` reads parsed
YAML, so an ``enum`` embedded inside a JSON **string** is invisible to it. No
tracked file has either shape, and both are recorded rather than fixed so that a
future change introducing one is a known risk rather than a surprise.
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
    r"^(?:(?:us|eu|apac|global|us-gov)\.)?"  # optional region / geo prefix
    r"[a-z][a-z0-9-]+\."  # provider, e.g. amazon / openai
    r"(?=[a-z0-9.:_-]*[a-z])[a-z0-9][a-z0-9.:_-]*$"  # model, must contain a letter
)

# Values that appear in a `model` enum but are not Bedrock model ids. `LambdaHook`
# selects a customer Lambda instead of a model, so it has no price and no limits.
NON_MODEL_CHOICES = {"LambdaHook"}

# ---------------------------------------------------------------------------
# End-of-life models come from SHIPPED CODE, not from a copy kept here.
#
# `idp_common.config.retired_models` is the single registry. It has to live in
# product code because `validate_config` needs the same answer — `idp-cli
# config-validate` rejects a configuration that pins a dead model — and because
# this repository previously had TWO registries with contradictory policies: the
# #708 gate (`scripts/sdlc/tests/test_retired_models_not_offered.py`) required a
# retired model to be ABSENT from pricing.yaml, since that absence is what made
# validation fail, while this gate requires the pricing row to be RETAINED so a
# historical cost report still resolves its rate. Both goals are legitimate and
# neither can be met by the presence or absence of a pricing row, so validation
# now rejects on retirement itself and the row stays. See that module's docstring.
#
# Whether a model is dead is not knowable from this tree and CI is offline, so the
# registry is hand-maintained — but it is hand-maintained in ONE place, with each
# entry carrying its date and the exact command that establishes it.
# ---------------------------------------------------------------------------
_retired = pytest.importorskip("idp_common.config.retired_models")
EOL_MODELS = _retired.RETIRED_MODELS
_base_model = _retired.base_model_id

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


#: base model id -> listed id, so any regional variant of a dead model matches.
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
    """Collect every model-shaped string a template OFFERS.

    Two shapes, not one:

    * an ``enum`` / ``AllowedValues`` list — a constrained choice;
    * a ``Default:`` on a CloudFormation ``Parameter`` — which a customer
      **receives without choosing anything**. A parameter with no
      ``AllowedValues`` is completely unconstrained, so a dead model there is
      exactly the class this file's docstring calls "worse than a picklist entry",
      and four tracked templates ship a model id in that shape today
      (``GeneratorModelId``, ``BedrockModelId``, two ``TargetModelId``).
    """
    if isinstance(node, dict):
        for key, value in node.items():
            if key in ("enum", "AllowedValues") and isinstance(value, list):
                for item in value:
                    if isinstance(item, str) and MODEL_ID.match(item):
                        acc.setdefault(item, set()).add(f"{path}.{key}")
            # A `Default:` is only a model offering when it sits on a Parameter;
            # `Default` appears elsewhere in a ConfigSchema as a field default,
            # which the code-defaults surface covers instead.
            if (
                key == "Default"
                and isinstance(value, str)
                and MODEL_ID.match(value)
                and ".Parameters." in f"{path}.{key}"
            ):
                acc.setdefault(value, set()).add(f"{path}.{key}")
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


def _declared_field_defaults(models_module: Any, acc: dict[str, set[str]]) -> None:
    """Every model-shaped field default declared by any BaseModel in models.py.

    The second of two passes, and it exists because the first one alone was a
    coverage REGRESSION. Walking the instantiated ``IDPConfig()`` sees only fields
    present in the resolved instance, and ``RuleValidationConfig`` declares four
    sub-configs as ``Optional[X] = None`` — so
    ``IDPConfig().rule_validation.fact_extraction`` is ``None`` and the walk never
    descends. The four fields it could not reach were
    ``FactExtractionConfig.model``, ``RuleValidationOrchestratorConfig.model``,
    ``Z3RuleTranslatorConfig.model`` and ``Z3ValueExtractionConfig.model`` — the
    first two being exactly where this change found its second end-of-life model.
    The default is live, not theoretical::

        IDPConfig(rule_validation={"enabled": True, "fact_extraction": {}}) \\
            .rule_validation.fact_extraction.model

    The regex this replaced did catch those, because ``re.findall`` scans module
    text irrespective of reachability. So the rewrite gained ``default_factory``
    coverage and lost reachability-independent coverage, with the new blind spot
    in a different place from the old one. Both passes are kept and unioned:
    reachability-independent from the class declarations, factory-aware from the
    instance.
    """
    import inspect as _inspect

    import pydantic

    for name, obj in vars(models_module).items():
        if not (
            _inspect.isclass(obj)
            and issubclass(obj, pydantic.BaseModel)
            and obj is not pydantic.BaseModel
        ):
            continue
        # Only classes DEFINED here, not ones imported into the namespace.
        if getattr(obj, "__module__", None) != models_module.__name__:
            continue
        for field_name, field in obj.model_fields.items():
            # A pydantic v2 default_factory may take a validated-data argument, so
            # calling it zero-arg can raise. A factory we cannot evaluate simply
            # yields nothing here — the instantiated-tree pass covers the reachable
            # ones, and this pass exists for the declared `default=` values behind
            # Optional sub-configs.
            produced = None
            if field.default_factory is not None:
                try:
                    produced = field.default_factory()  # pyright: ignore[reportCallIssue]
                except TypeError:
                    produced = None
            for value in (field.default, produced):
                if isinstance(value, str) and MODEL_ID.match(value):
                    acc.setdefault(value, set()).add(f"{name}.{field_name}")
                elif isinstance(value, pydantic.BaseModel):
                    # A factory returning a sub-model, e.g.
                    # `default_factory=lambda: SummarizationConfig(model=…)`.
                    _walk_model_values(
                        value.model_dump(mode="python"), f"{name}.{field_name}", acc
                    )


@pytest.fixture(scope="module")
def code_defaults() -> dict[str, set[str]]:
    """Every model id a default resolves to, from BOTH directions.

    1. The **instantiated** ``IDPConfig()`` tree, which is what a stack actually
       reads. A regex over ``default="…"`` misses
       ``default_factory=lambda: X(model="…")``, and for ``IDPConfig`` the factory
       GOVERNS, overriding the nested class's own ``default=``. Reverting only the
       factory to an end-of-life model left a regex-based check green while
       ``IDPConfig().summarization.model`` returned it.
    2. Every **declared** field default on every BaseModel in the module, which
       does not depend on a field being reachable from a default instance. See
       ``_declared_field_defaults`` — four model fields live behind
       ``Optional[X] = None`` and are invisible to (1).

    Neither pass alone is sufficient and their blind spots are in different places,
    so both run and the results are unioned.
    """
    models = pytest.importorskip("idp_common.config.models")
    # Same provenance guard as the SDK gate: idp_common is an editable install, so
    # without PYTHONPATH pointing at this checkout the defaults could be read from
    # a DIFFERENT tree than the file surfaces above — green for a fix that is not
    # in the code under test.
    module_path = Path(models.__file__).resolve()
    assert module_path.is_relative_to(REPO_ROOT), (
        f"idp_common.config.models resolved to {module_path}, outside the checkout "
        f"under test ({REPO_ROOT}). Set PYTHONPATH to this checkout's "
        "lib/idp_common_pkg — otherwise this fixture and the file-based fixtures "
        "above describe two different trees."
    )

    acc: dict[str, set[str]] = {}
    _walk_model_values(models.IDPConfig().model_dump(mode="python"), "IDPConfig()", acc)
    _declared_field_defaults(models, acc)
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
    # Still anchored: prose, ARNs and bare words are not ids. `0.7` and `v1.0` are
    # the two shapes the prefix-optional widening actually created — a `top_p`
    # default and a version string, both of which matched until the provider was
    # required to start with a letter and the model segment to contain one.
    for not_an_id in (
        "LambdaHook",
        "arn:aws:bedrock:us-east-1::foundation-model/amazon.nova-pro-v1:0",
        "a model id",
        "enabled",
        "us-east-1",
        "0.7",
        "1.0",
        "0.95",
        "2.5",
        "v1.0",
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
        assert not any(re.search(p, model, re.IGNORECASE) for p in limit_patterns), (
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
        "without anyone choosing it. An `IDPConfig().…` path names the field a "
        "default instance already resolves; a `Class.field` path names a declared "
        "default behind an Optional sub-config, which a config supplying that "
        "sub-config resolves."
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
def test_every_code_default_is_selectable_or_premise_checked(code_defaults, selectable):
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
def test_non_selectable_default_exemptions_are_still_needed(code_defaults, selectable):
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


@pytest.mark.unit
def test_every_model_bearing_config_class_is_covered(code_defaults):
    """Coverage of the defaults walk, asserted rather than assumed.

    Fifteen classes in ``models.py`` declare a model-shaped field default. The
    instantiated-tree pass alone reaches eleven: ``RuleValidationConfig`` declares
    four sub-configs as ``Optional[X] = None``, so a default instance has ``None``
    there and the walk cannot descend. Those four —
    ``FactExtractionConfig.model``, ``RuleValidationOrchestratorConfig.model``,
    ``Z3RuleTranslatorConfig.model`` and ``Z3ValueExtractionConfig.model`` — are
    where this change found its second end-of-life model, so a check that cannot
    see them is no check at all.

    This asserts the union covers every one, by comparing against the classes
    discovered from the module rather than against a list written here.
    """
    import inspect as _inspect

    import pydantic

    models = pytest.importorskip("idp_common.config.models")

    expected: set[str] = set()
    for name, obj in vars(models).items():
        if not (
            _inspect.isclass(obj)
            and issubclass(obj, pydantic.BaseModel)
            and obj is not pydantic.BaseModel
            and getattr(obj, "__module__", None) == models.__name__
        ):
            continue
        for field_name, field in obj.model_fields.items():
            if isinstance(field.default, str) and MODEL_ID.match(field.default):
                expected.add(f"{name}.{field_name}")

    assert len(expected) >= 15, (
        f"only {len(expected)} model-bearing field defaults discovered; the class "
        "walk has stopped finding them and this test is vacuous"
    )

    covered = {p for paths in code_defaults.values() for p in paths}
    missing = sorted(expected - covered)
    assert not missing, (
        "these declared model defaults are not covered by the defaults walk, so an "
        f"end-of-life model placed on one would be invisible: {missing}"
    )

    # The specific four the instantiated pass cannot reach, named so a future
    # simplification back to one pass fails loudly instead of quietly regressing.
    for behind_optional in (
        "FactExtractionConfig.model",
        "RuleValidationOrchestratorConfig.model",
        "Z3RuleTranslatorConfig.model",
        "Z3ValueExtractionConfig.model",
    ):
        assert behind_optional in covered, (
            f"{behind_optional} sits behind an Optional[...] = None sub-config, so "
            "the instantiated-tree pass cannot see it. The declared-defaults pass "
            "is what covers it — do not drop one of the two passes."
        )


@pytest.mark.unit
def test_the_optional_subconfigs_really_are_unreachable_from_a_default_instance():
    """The premise behind keeping two passes.

    If these ever stop defaulting to ``None``, the instantiated pass reaches them
    and the second pass becomes belt-and-braces rather than load-bearing — worth
    knowing, and worth failing on so the comment above stops being wrong.
    """
    models = pytest.importorskip("idp_common.config.models")
    rv = models.IDPConfig().rule_validation
    for field in (
        "fact_extraction",
        "rule_validation_orchestrator",
        "z3_rule_translator",
        "z3_value_extraction",
    ):
        assert getattr(rv, field) is None, (
            f"rule_validation.{field} is no longer None by default, so the "
            "instantiated-tree pass now reaches it. Update the reasoning in "
            "_declared_field_defaults rather than leaving it stale."
        )
    # …and the default they hide is genuinely live once the sub-config is supplied.
    supplied = models.IDPConfig(
        rule_validation={"enabled": True, "fact_extraction": {}}
    ).rule_validation.fact_extraction
    assert supplied is not None and MODEL_ID.match(supplied.model), supplied


@pytest.mark.unit
def test_parameter_defaults_are_actually_collected(selectable):
    """Not vacuous: a CloudFormation `Parameter` `Default:` is a real surface here.

    An unconstrained parameter default is a model a customer receives without
    choosing anything, and six tracked parameters carry one. If this ever collects
    nothing, an end-of-life model placed on one becomes invisible.
    """
    default_paths = sorted(
        {p for paths in selectable.values() for p in paths if p.endswith(".Default")}
    )
    assert len(default_paths) >= 5, (
        f"only {len(default_paths)} parameter-default paths collected: {default_paths}"
    )
    # A Default is only an offering on a Parameter; the ConfigSchema's field-level
    # `Default`s are the code-defaults surface's business, not this one.
    for path in default_paths:
        assert ".Parameters." in path, path


@pytest.mark.unit
def test_the_retired_model_registry_is_shared_with_the_other_eol_gate():
    """One registry, read by both gates, so they cannot drift.

    ``scripts/sdlc/tests/test_retired_models_not_offered.py`` (the #708 gate) kept
    its own ``RETIRED_MODEL_IDS`` and covers two surfaces this file does not — the
    Converse cachePoint allowlist and the UI's SchemaInspector list. Two hand-kept
    lists of the same fact had already diverged: a newly retired model was in one
    and not the other, and neither referenced the other. Both now derive from
    ``idp_common.config.retired_models``, and this asserts the #708 gate really does
    rather than having been re-hardcoded.
    """
    other = (
        REPO_ROOT / "scripts/sdlc/tests/test_retired_models_not_offered.py"
    ).read_text(encoding="utf-8")
    assert "idp_common.config.retired_models" in other, (
        "the #708 gate no longer derives its retired-model list from the shared "
        "registry, so the two gates can disagree about which models are dead"
    )
    assert "_registry.RETIRED_MODELS.items()" in other, (
        "the #708 gate's list is no longer built from the shared registry's entries"
    )
    # And the registry the product code enforces is the one this file uses.
    validate_src = (
        REPO_ROOT / "lib/idp_common_pkg/idp_common/config/merge_utils.py"
    ).read_text(encoding="utf-8")
    assert "retirement_of" in validate_src, (
        "validate_config no longer consults the retired-model registry, so "
        "`idp-cli config-validate` would accept a configuration pinning a dead "
        "model — which is what #708 asked it not to do"
    )
