# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""A model Bedrock has retired must not be selectable anywhere.

``us.anthropic.claude-3-5-haiku-20241022-v1:0`` reached end of life; invoking it
returns ``ResourceNotFoundException: This model version has reached the end of
its life`` (verified live in us-west-2 on 2026-08-29). It was still listed in
the CloudFormation enums, so a user could pick it in the configuration editor
and get a runtime failure two stages into a document instead of a validation
error at save time (GitHub #708).

Removal is a picklist change, not a validation change. Every model field in
``IDPConfig`` is a plain ``str`` — the ConfigSchema ``enum`` drives the UI
dropdown only, nothing revalidates a stored config against it. So a config that
still names a retired model keeps LOADING exactly as before (it just keeps
failing at invoke time, as it already did); the accompanying
:func:`test_a_retired_model_id_still_loads_in_a_stored_config` pins that, since
it is the whole reason hard removal is safe here and matches the precedent set
when the Sonnet 4/4.5 ``:1m`` variants and the older Claude picklist entries were
retired ("Existing configurations using older versions still work").

The sweep is scoped to the surfaces that OFFER or PRICE a model. Test fixtures
and notebooks may legitimately use a retired ID as an opaque sample string.
"""

from __future__ import annotations

import pathlib
import re

import pytest

#: Bedrock model IDs that must no longer be offered, DERIVED from the single
#: registry in shipped code rather than kept as a second copy here.
#:
#: There were two registries until this was folded: this list and the one in
#: ``scripts/tests/test_model_surface_consistency.py``. Neither referenced the
#: other, and they encoded CONTRADICTORY policies — see the note on
#: ``config_library/pricing.yaml`` below. A newly retired model was added to one
#: and not the other, which is exactly how two gates drift into disagreeing about
#: the same fact.
_registry = pytest.importorskip("idp_common.config.retired_models")
RETIRED_MODEL_IDS: dict[str, str] = {
    model_id: (
        f"End of life on {facts['eol']} — Converse returns "
        f"ResourceNotFoundException 'This model version has reached the end of "
        f"its life' (GitHub #708). Verify: {facts['verify']}"
    )
    for model_id, facts in _registry.RETIRED_MODELS.items()
}

#: Files that must not mention a retired ID: the two templates whose enums feed
#: every model picklist, the Converse client's cachePoint allowlist, and the
#: deploy-time US->EU model swap table.
#:
#: ``config_library/pricing.yaml`` and ``config_library/model_config_limits.yaml``
#: are deliberately NOT here, and used to be. They are consulted
#: **retrospectively**, for whatever model a deployed stack's stored configuration
#: names — which is a superset of what is newly selectable:
#:
#: * a cost report over documents processed while the model was still selectable
#:   resolves its rate from ``pricing.yaml`` by model id, so deleting the row
#:   re-prices historical runs at zero;
#: * dropping the limits pattern makes ``get_model_max_output_tokens`` raise
#:   "Unsupported model ID … run discover_model_limits.py", replacing Bedrock's
#:   accurate end-of-life error with a misleading one.
#:
#: Removing them costs nothing here, because the reason they were listed —
#: ``validate_config`` deriving its valid-model set from ``pricing.yaml``, so the
#: absence of a row is what made validation fail — no longer holds:
#: ``validate_config`` now rejects a retired model **on its own merits**, via
#: ``retired_models.retirement_of``. That is strictly better than the old coupling,
#: which depended on a side effect of an unrelated file.
#:
#: ``scripts/tests/test_model_surface_consistency.py`` owns those two files in BOTH
#: directions: it asserts an offered model is priced and limit-matched, and that a
#: retired one that was once offered KEEPS its pricing row.
#: ``src/lambda/update_configuration/index.py`` is likewise not in the text sweep,
#: because its ``MODEL_MAPPINGS`` table is directional and only one direction is a
#: defect. A retired model as a mapping **key** is legitimate and load-bearing: the
#: table's purpose is to rewrite a stored configuration onto a model that works in
#: an EU region, so the row for a dead US model is precisely what rescues a stack
#: that still names it. A retired model as a mapping **value** would write a dead
#: model into a working configuration. ``test_no_retired_model_is_a_mapping_target``
#: below checks that direction specifically, by parsing the table rather than
#: grepping the file.
_OFFERING_SURFACES = (
    "template.yaml",
    "patterns/unified/template.yaml",
    "lib/idp_common_pkg/idp_common/bedrock/client.py",
)

#: UI sources that hardcode model lists (in addition to the CFN-driven schema).
_UI_SURFACES = (
    "src/ui/src/constants/schemaConstants.ts",
    "src/ui/src/components/json-schema-builder/SchemaInspector.tsx",
)


def _repo_root() -> pathlib.Path:
    return pathlib.Path(__file__).resolve().parents[3]


def _surfaces() -> list[pathlib.Path]:
    root = _repo_root()
    return [
        root / rel
        for rel in (*_OFFERING_SURFACES, *_UI_SURFACES)
        if (root / rel).is_file()
    ]


def test_the_surfaces_exist():
    """Guard the guard: a rename must not silently empty this sweep."""
    present = {p.name for p in _surfaces()}
    for required in ("template.yaml", "client.py"):
        assert required in present, (required, sorted(present))
    # pricing.yaml / model_config_limits.yaml are intentionally absent — see the
    # note on _OFFERING_SURFACES. Asserted, so their removal was a decision and a
    # future re-add is a decision too.
    assert "pricing.yaml" not in present, (
        "pricing.yaml is back in this sweep, which would demand deleting the "
        "pricing row of a retired model and silently re-price historical cost "
        "reports at zero"
    )
    assert "model_config_limits.yaml" not in present
    # The templates are where the enums live; without them this proves nothing.
    root = _repo_root()
    assert (root / "patterns/unified/template.yaml").is_file()


@pytest.mark.parametrize("model_id", sorted(RETIRED_MODEL_IDS))
def test_retired_model_is_not_offered(model_id: str):
    reason = RETIRED_MODEL_IDS[model_id]
    hits: list[str] = []
    for path in _surfaces():
        for lineno, line in enumerate(
            path.read_text(encoding="utf-8").splitlines(), start=1
        ):
            if model_id in line:
                rel = path.relative_to(_repo_root())
                hits.append(f"{rel}:{lineno}: {line.strip()}")
    assert not hits, (
        f"{model_id} is retired ({reason}) but is still offered/priced here:\n  "
        + "\n  ".join(hits)
        + "\n\nRemove it from the enum / pricing entry / model list. Work "
        ".claude/skills/add-model.md in reverse — removing a model touches the "
        "same files as adding one."
    )


@pytest.mark.parametrize("model_id", sorted(RETIRED_MODEL_IDS))
def test_the_cachepoint_allowlist_drops_it(model_id: str):
    """Named explicitly: a stale entry here would advertise caching for a dead ID."""
    client = pytest.importorskip("idp_common.bedrock.client")
    assert model_id not in client.CACHEPOINT_SUPPORTED_MODELS
    assert client.CACHEPOINT_SUPPORTED_MODELS, "the allowlist must not be empty"


@pytest.mark.parametrize("model_id", sorted(RETIRED_MODEL_IDS))
def test_a_retired_model_id_still_loads_in_a_stored_config(model_id: str):
    """Removing the picklist entry must NOT break a config that still names it.

    This is the justification for hard removal over an accepted-but-hidden
    deprecation shim: there is nothing to keep accepting. If a model field ever
    becomes a ``Literal``/``Enum``, this test fails and whoever made that change
    has to deal with stored configs deliberately.
    """
    models = pytest.importorskip("idp_common.config.models")
    for section, kwargs in (
        (models.ExtractionConfig, {"model": model_id}),
        (models.ClassificationConfig, {"model": model_id}),
        (models.SummarizationConfig, {"model": model_id}),
    ):
        assert section(**kwargs).model == model_id


@pytest.mark.parametrize("model_id", sorted(RETIRED_MODEL_IDS))
def test_config_validate_now_reports_it_as_an_invalid_model(model_id: str):
    """Dropping the pricing entry turns the runtime failure into a pre-flight one.

    ``validate_config`` (``idp-cli config-validate`` / ``client.config.validate()``)
    checks model IDs against ``config_library/pricing.yaml``, so removing the
    retired model's pricing block makes a config that pins it fail validation
    instead of failing at the first ``Converse`` call — which is what #708 asked
    for. This path is the *only* consumer of ``validate_config``: neither the
    stack-update custom resource nor the configuration save calls it, so the
    stricter answer cannot wedge a deployment.
    """
    merge_utils = pytest.importorskip("idp_common.config.merge_utils")
    # Control: the same config shape with a current model must pass, otherwise
    # the rejection below proves nothing about the model ID.
    ok = merge_utils.validate_config(
        {"extraction": {"model": "us.anthropic.claude-haiku-4-5-20251001-v1:0"}}
    )
    assert ok["valid"] is True, ok["errors"]

    result = merge_utils.validate_config({"extraction": {"model": model_id}})
    assert result["valid"] is False, result
    assert any("invalid model ID" in err for err in result["errors"]), result["errors"]


def test_model_limits_still_cover_the_retired_family():
    """The generic `claude-3` limits pattern must survive the removal.

    ``model_config_limits.yaml`` has no per-ID entry for the retired model — it
    matched the shared ``claude-3`` regex, which other still-offered Claude 3.x
    IDs (3 Haiku, 3.5 Sonnet, 3.7 Sonnet) also rely on. Deleting that pattern
    while removing the retired ID would silently break them.
    """
    yaml = pytest.importorskip("yaml")
    path = _repo_root() / "config_library/model_config_limits.yaml"
    doc = yaml.safe_load(path.read_text(encoding="utf-8"))
    patterns = [entry["pattern"] for entry in doc["model_limits"]]
    assert any(
        re.search(p, "us.anthropic.claude-3-7-sonnet-20250219-v1:0") for p in patterns
    ), "no model_config_limits pattern matches the still-offered Claude 3.x IDs"


@pytest.mark.parametrize("model_id", sorted(RETIRED_MODEL_IDS))
def test_no_retired_model_is_a_mapping_target(model_id: str):
    """The US->EU swap table may name a retired model as a SOURCE, never a TARGET.

    ``MODEL_MAPPINGS`` in ``src/lambda/update_configuration/index.py`` rewrites a
    stored configuration's model when the stack is deployed in an EU region. Its
    keys are "models a stored config might name", which legitimately includes dead
    ones — the row for ``us.amazon.nova-premier-v1:0`` is what moves such a config
    onto a working model instead of leaving it broken. Its values are "models we
    will write", where a dead model would be a defect.

    Parsed with ``ast`` rather than grepped, because a text sweep cannot tell the
    two directions apart — which is why this file used to forbid both and, once the
    two retired-model registries were folded, contradicted the deliberate retention
    of that row.
    """
    import ast

    path = _repo_root() / "src/lambda/update_configuration/index.py"
    assert path.is_file(), f"{path} moved; update this test"
    tree = ast.parse(path.read_text(encoding="utf-8"))

    targets: list[str] = []
    found_table = False
    for node in ast.walk(tree):
        if not isinstance(node, ast.Assign):
            continue
        if not any(
            isinstance(t, ast.Name) and t.id == "MODEL_MAPPINGS" for t in node.targets
        ):
            continue
        assert isinstance(node.value, ast.Dict), "MODEL_MAPPINGS is no longer a literal dict"
        found_table = True
        for value in node.value.values:
            if isinstance(value, ast.Constant) and isinstance(value.value, str):
                targets.append(value.value)

    assert found_table, "MODEL_MAPPINGS not found; this test proves nothing"
    assert targets, "MODEL_MAPPINGS has no string targets; this test proves nothing"

    base = _registry.base_model_id(model_id)
    offending = [t for t in targets if _registry.base_model_id(t) == base]
    assert not offending, (
        f"MODEL_MAPPINGS maps some model ONTO {model_id}, which is retired — the "
        f"swap would write a dead model into a working configuration: {offending}"
    )


def test_a_retired_model_may_remain_a_mapping_source():
    """The premise behind the direction split, asserted so it is not read as an
    oversight: at least one retired model IS still a key, on purpose."""
    import ast

    path = _repo_root() / "src/lambda/update_configuration/index.py"
    tree = ast.parse(path.read_text(encoding="utf-8"))
    keys: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign) and any(
            isinstance(t, ast.Name) and t.id == "MODEL_MAPPINGS" for t in node.targets
        ):
            for key in node.value.keys:
                if isinstance(key, ast.Constant) and isinstance(key.value, str):
                    keys.append(key.value)
    retired_keys = [k for k in keys if k in RETIRED_MODEL_IDS]
    assert retired_keys, (
        "no retired model is a MODEL_MAPPINGS source any more. That may be correct, "
        "but it means a stored configuration naming a dead US model is no longer "
        "rewritten onto a working one when deployed in an EU region — decide it "
        "rather than letting it lapse, and update the reasoning above."
    )
