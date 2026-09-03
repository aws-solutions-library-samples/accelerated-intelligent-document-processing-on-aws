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

#: Bedrock model IDs that must no longer be offered, with the reason.
RETIRED_MODEL_IDS: dict[str, str] = {
    "us.anthropic.claude-3-5-haiku-20241022-v1:0": (
        "End of life — Converse returns ResourceNotFoundException "
        "'This model version has reached the end of its life' (GitHub #708)."
    ),
}

#: Files that must not mention a retired ID: the two templates whose enums feed
#: every model picklist, the pricing table, the Converse client's cachePoint
#: allowlist, and the deploy-time US->EU model swap table.
_OFFERING_SURFACES = (
    "template.yaml",
    "patterns/unified/template.yaml",
    "config_library/pricing.yaml",
    "config_library/model_config_limits.yaml",
    "lib/idp_common_pkg/idp_common/bedrock/client.py",
    "src/lambda/update_configuration/index.py",
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
    for required in ("template.yaml", "pricing.yaml", "client.py", "index.py"):
        assert required in present, (required, sorted(present))
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
