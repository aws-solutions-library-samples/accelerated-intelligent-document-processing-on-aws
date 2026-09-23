# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Every key in a shipped configuration preset is one `IDPConfig` reads.

`idp-cli deploy --custom-config config_library/<...>/config.yaml` installs a preset
verbatim, and `IDPConfig` takes `extra="ignore"`, so a top-level block whose name no
field matches is **discarded on load**. The file goes on looking like configuration:
a reader adjusting a value in it is editing something with no effect, and any prompt
text inside it is a second copy that drifts from the one the pipeline uses.

That is what five `ocr-benchmark` presets did with a `criteria_validation:` block —
thirteen keys including a model, a semaphore and two prompts, none of them read. The
load does log `IDPConfig: Ignoring unknown fields (not defined in model)` at WARNING,
which is why this was discoverable at all, but that line lands in a Lambda log while
the file stays in the repository looking authoritative.

This gate closes the class from the **authoring** side, at every depth. The loading
side — the log line an operator sees when their own configuration carries such a key
— is `IDPConfig`'s own report, extended below the top level in
[#1134](https://github.com/aws-solutions-library-samples/accelerated-intelligent-document-processing-on-aws/issues/1134).

**The two depths are answered by two different tests here, because the question
differs.** Top level is set arithmetic against `IDPConfig.model_fields` and admits an
exemption for a key another consumer reads (`TOP_LEVEL_KEY_EXEMPT`). Below it, the
answer comes from `models.collect_ignored_config_keys`, the same walk the load
performs — so this gate cannot drift from what the runtime actually drops, and the
nested resolution exists in one place rather than two. Presets are migrated first, as
a load would: a legacy key is relocated rather than dropped, and reporting one would
name a key that works.

Discovery is derived, not listed: every tracked `.yaml`/`.yml` under
`config_library/` that parses to a mapping and looks like a preset. Two lookup
tables that live there and are not presets are excluded by name, and the exclusion
is asserted non-vacuous — if either stops being present, this file fails rather than
silently narrowing.

**The nested question is also asked outside `config_library/`.** A reader does not
start at the preset directory: the notebooks carry configuration files of their own,
and a dead key in one of those is copied into a real deployment by whoever follows the
walkthrough. Five of them carried `extraction.max_tokens` and one set `top_p`/`top_k`
on two models that declare neither. Those documents are found by shape rather than by
directory, and only the nested question is asked of them — see the test for why.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

yaml = pytest.importorskip("yaml")

REPO_ROOT = Path(__file__).resolve().parents[2]
PRESET_DIR = "config_library/"

# Files under config_library/ that are lookup tables rather than deployable presets.
# Neither is installed by --custom-config, and neither is a configuration document:
# pricing.yaml maps a model id to a rate and model_config_limits.yaml maps one to a
# context window. Their top-level keys are model ids and section names of their own,
# so IDPConfig has no opinion about them. Asserted present below.
PRESET_SCAN_EXCLUDED_LOOKUP_TABLES = {
    "pricing.yaml",
    "model_config_limits.yaml",
    "finetuning_models.yaml",
}

# Files under config_library/ that are not IDPConfig documents at all. The extension
# manifests are read by the feature platform and carry their own schema
# (`schemaVersion`, `features`); IDPConfig never sees them.
PRESET_SCAN_EXCLUDED_NON_CONFIGS = {
    "extensions-marketplace.yaml",
    "extensions-oss.yaml",
}

# Top-level keys IDPConfig does NOT model but that ANOTHER consumer reads, so
# "IDPConfig discards it" does not make them dead. One entry per key, each naming its
# reader, and `test_every_read_elsewhere_key_really_is_read` computes that premise
# rather than trusting this comment.
#
# `description`: read by `update_configuration`, which POPS it off the dict before
# IDPConfig ever sees it and embeds it in the DynamoDB sort key the UI renders
# (`Config#{version}#{description}`). That pop is exactly why IDPConfig models no such
# field, and why "IDPConfig discards it" is the wrong question for this key.
#
# Each entry is (path of the PRODUCTION consumer, the source text that constitutes the
# read). Both halves are load-bearing:
#
#   * The path names production, not a test. A test merely asserting the string would
#     keep this entry green after the real reader was deleted.
#   * The marker is the READ, not the bare key name. `description` occurs ten times in
#     that file, mostly as an unrelated function parameter, so a substring check for
#     the word alone would survive deleting the line that actually reads the config
#     key — a premise satisfied by a coincidence is not a premise.
TOP_LEVEL_KEY_EXEMPT = {
    "description": (
        "src/lambda/update_configuration/index.py",
        'pop("description"',
    ),
}


def _tracked_preset_files() -> list[str]:
    out = subprocess.run(
        ["git", "ls-files", "-z", PRESET_DIR],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    return [
        rel
        for rel in out.split("\0")
        if rel.endswith((".yaml", ".yml"))
        and Path(rel).name not in PRESET_SCAN_EXCLUDED_LOOKUP_TABLES
        and Path(rel).name not in PRESET_SCAN_EXCLUDED_NON_CONFIGS
    ]


def _idp_config_top_level_fields() -> set[str]:
    """The top-level names `IDPConfig` accepts, read from the model itself."""
    from idp_common.config.models import IDPConfig

    fields = set(IDPConfig.model_fields)
    # A field may be populated under an alias rather than its attribute name.
    for name, info in IDPConfig.model_fields.items():
        alias = getattr(info, "alias", None)
        if alias:
            fields.add(alias)
        fields.add(name)
    return fields


@pytest.fixture(scope="module")
def presets() -> dict[str, dict]:
    docs: dict[str, dict] = {}
    for rel in _tracked_preset_files():
        try:
            doc = yaml.safe_load((REPO_ROOT / rel).read_text(encoding="utf-8"))
        except yaml.YAMLError:
            continue  # malformed YAML is a different gate's finding
        if isinstance(doc, dict):
            docs[rel] = doc
    return docs


#: Top-level names that mark a YAML document as an IDPConfig-shaped configuration.
#: Read from the model rather than listed, so a renamed block cannot make a document
#: invisible to the walk below; a document carrying none of them is something else
#: (a manifest, a test fixture, a CloudFormation template) and IDPConfig never sees it.
_CONFIG_MARKER_BLOCKS = ("classes", "ocr", "classification", "extraction")


@pytest.fixture(scope="module")
def other_config_documents() -> dict[str, dict]:
    """Tracked configuration documents OUTSIDE `config_library/`.

    The notebooks and the SDLC config directory carry their own configuration files,
    and a dead key in one of those is copied into a real deployment by whoever follows
    the walkthrough. Discovery is derived from `git ls-files` and from the shape of the
    document, not from a list of directories.
    """
    from idp_common.config.models import IDPConfig

    markers = {name for name in _CONFIG_MARKER_BLOCKS if name in IDPConfig.model_fields}
    assert len(markers) == len(_CONFIG_MARKER_BLOCKS), (
        f"one of {_CONFIG_MARKER_BLOCKS} is no longer an IDPConfig field, so this "
        "walk's idea of what a configuration document looks like is stale"
    )

    out = subprocess.run(
        ["git", "ls-files", "-z", "*.yaml", "*.yml"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    docs: dict[str, dict] = {}
    for rel in out.split("\0"):
        if not rel or rel.startswith(PRESET_DIR):
            continue
        try:
            doc = yaml.safe_load((REPO_ROOT / rel).read_text(encoding="utf-8"))
        except (yaml.YAMLError, UnicodeDecodeError):
            continue
        if isinstance(doc, dict) and markers & set(doc):
            docs[rel] = doc
    return docs


@pytest.mark.unit
def test_discovery_finds_the_presets(presets):
    """Not vacuous: the walk must reach real presets, or every assertion below passes
    trivially on an empty set."""
    assert len(presets) >= 10, sorted(presets)
    assert "config_library/unified/ocr-benchmark/config.yaml" in presets


@pytest.mark.unit
def test_the_excluded_lookup_tables_are_present(presets):
    """The exclusion list must describe files that exist, or it is silently dead and
    pre-exempting whatever next takes one of those names."""
    for name in PRESET_SCAN_EXCLUDED_LOOKUP_TABLES:
        matches = list((REPO_ROOT / PRESET_DIR).glob(f"**/{name}"))
        assert matches, f"{name} is excluded as a lookup table but no longer exists"


@pytest.mark.unit
def test_no_preset_carries_a_top_level_key_idpconfig_discards(presets):
    """A top-level block no field matches is dropped on load, so the file lies."""
    known = _idp_config_top_level_fields() | set(TOP_LEVEL_KEY_EXEMPT)
    offenders = {
        rel: sorted(set(doc) - known)
        for rel, doc in presets.items()
        if set(doc) - known
    }
    assert not offenders, (
        "these shipped presets carry top-level keys that IDPConfig discards on load, "
        "so the values read as configuration and have no effect. Three possibilities, "
        "and the third is easy to miss: the key belongs under a name the model reads; "
        "the block is dead and should go; or something OTHER than IDPConfig reads it, "
        "in which case register it in TOP_LEVEL_KEY_EXEMPT with the path of that "
        "reader. Do not delete a key before checking the third -- `description` is "
        "read by update_configuration and is invisible to this gate's question:\n"
        + "\n".join(f"  {rel}: {keys}" for rel, keys in sorted(offenders.items()))
    )


@pytest.mark.unit
def test_no_preset_carries_a_nested_key_idpconfig_discards(presets):
    """The same question one level down and further, where the typo is likelier.

    A misspelled or mis-nested key below the top level was dropped with no diagnostic
    anywhere until #1134, so a preset could carry one indefinitely — and a mis-nested
    key is the worse half, because it names a real field at the wrong depth and so
    bypasses the validator that would have rejected its value.

    The answer comes from the model's own walk rather than from a reimplementation
    here: what this gate calls dropped is exactly what a load drops.
    """
    import copy

    from idp_common.config.migrations import migrate_config
    from idp_common.config.models import IDPConfig, collect_ignored_config_keys

    offenders = {}
    for rel, doc in presets.items():
        findings = collect_ignored_config_keys(
            migrate_config(copy.deepcopy(doc)), IDPConfig
        )
        if findings:
            offenders[rel] = [f.describe() for f in findings]
    assert not offenders, (
        "these shipped presets carry nested keys IDPConfig discards on load, so the "
        "values read as configuration and have no effect. Either the key belongs at "
        "the path named in the suggestion, or it is dead and should go:\n"
        + "\n".join(f"  {rel}: {keys}" for rel, keys in sorted(offenders.items()))
    )


@pytest.mark.unit
def test_no_other_shipped_configuration_document_carries_a_nested_key_either(
    other_config_documents,
):
    """The same nested question, asked of the documents people copy from.

    `config_library/` is what `--custom-config` installs, but it is not where a reader
    starts: the notebooks carry configuration files of their own, and a dead key in one
    of those is copied into a real deployment by whoever follows the walkthrough. Five
    of them carried `extraction.max_tokens`, and one set `top_p`/`top_k` on the two Z3
    models, which declare neither — so a published example showed two decoding
    parameters that had never taken effect.

    **Nested keys only, deliberately.** A *top-level* key in one of these is a
    judgement call the presets do not need — a notebook document may legitimately
    carry scaffolding IDPConfig never sees — while a nested key under a block IDPConfig
    does model has an unambiguous answer.
    """
    import copy

    from idp_common.config.migrations import migrate_config
    from idp_common.config.models import IDPConfig, collect_ignored_config_keys

    offenders = {}
    for rel, doc in other_config_documents.items():
        findings = collect_ignored_config_keys(
            migrate_config(copy.deepcopy(doc)), IDPConfig
        )
        if findings:
            offenders[rel] = [f.describe() for f in findings]
    assert not offenders, (
        "these shipped configuration documents carry nested keys IDPConfig discards "
        "on load, so a reader copying them gets settings with no effect:\n"
        + "\n".join(f"  {rel}: {keys}" for rel, keys in sorted(offenders.items()))
    )


@pytest.mark.unit
def test_discovery_finds_the_other_configuration_documents(other_config_documents):
    """Non-vacuity for the test above, which is otherwise trivially green.

    The count is not pinned — these files come and go — but the walk must reach a
    realistic number of them and must include the notebook tree, which is the one
    that matters because its files are meant to be copied.
    """
    assert len(other_config_documents) >= 5, sorted(other_config_documents)
    assert any(rel.startswith("notebooks/") for rel in other_config_documents), sorted(
        other_config_documents
    )


@pytest.mark.unit
def test_the_excluded_extension_manifests_are_present():
    """Same non-vacuity argument as the lookup tables."""
    for name in PRESET_SCAN_EXCLUDED_NON_CONFIGS:
        assert (REPO_ROOT / PRESET_DIR / name).is_file(), (
            f"{name} is excluded as a non-IDPConfig document but no longer exists"
        )


@pytest.mark.unit
@pytest.mark.parametrize("key,entry", sorted(TOP_LEVEL_KEY_EXEMPT.items()))
def test_every_read_elsewhere_key_really_is_read(key, entry):
    """The premise, computed per key rather than asserted in a comment.

    A key allowed here because something else reads it must have that reader still
    present and still performing the read. Otherwise the allowance outlives its reason
    and silently pre-exempts the next dead block that happens to use the same name —
    which is the failure mode this gate exists to catch.

    The assertion is on the **read**, not on the key name appearing somewhere in the
    file. `description` occurs ten times in its reader, mostly as an unrelated
    function parameter, so matching the bare word would hold after the line that
    actually reads the config key was deleted.
    """
    reader, marker = entry
    path = REPO_ROOT / reader
    assert path.is_file(), (
        f"{key} is allowed because {reader} reads it; that file is gone"
    )
    assert marker in path.read_text(encoding="utf-8"), (
        f"{reader} no longer contains {marker!r}, so it no longer reads {key!r} and "
        "the reason for allowing that top-level key no longer holds"
    )


@pytest.mark.unit
def test_no_read_elsewhere_key_is_already_modelled():
    """An entry duplicating a real IDPConfig field would be dead weight."""
    overlap = set(TOP_LEVEL_KEY_EXEMPT) & _idp_config_top_level_fields()
    assert not overlap, (
        f"IDPConfig already models {sorted(overlap)}; drop them from TOP_LEVEL_KEY_EXEMPT"
    )
