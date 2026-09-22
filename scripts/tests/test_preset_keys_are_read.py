# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Every top-level key in a shipped configuration preset is one `IDPConfig` reads.

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

This gate closes the class from the **authoring** side. The loading side — a
misspelled or mis-nested key *below* the top level, which is dropped with no log line
at all — is
[#1134](https://github.com/aws-solutions-library-samples/accelerated-intelligent-document-processing-on-aws/issues/1134)
and is not what this file checks.

**Scope, deliberately narrow.** Only top-level keys, because that is exactly the set
`IDPConfig`'s own unknown-field check covers and therefore the set whose answer is
unambiguous. Going deeper would need this gate to reimplement Pydantic's nested
resolution, which is the thing #1134 proposes doing inside the model instead.

Discovery is derived, not listed: every tracked `.yaml`/`.yml` under
`config_library/` that parses to a mapping and looks like a preset. Two lookup
tables that live there and are not presets are excluded by name, and the exclusion
is asserted non-vacuous — if either stops being present, this file fails rather than
silently narrowing.
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
