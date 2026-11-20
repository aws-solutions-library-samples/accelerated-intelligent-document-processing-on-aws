# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Each default classification prompt lives in its own file (GitHub #1652).

``system_defaults/base-classification.yaml`` holds a ``{_file: <path>}`` mapping
where each prompt used to be written inline, and the defaults loader replaces the
mapping with the file's text before anything is merged. What has to hold:

* **The text is unchanged.** ``BASE_COMMIT_PROMPT_SHA256`` is the SHA-256 of every
  prompt as the inline YAML resolved it on the commit before the move, so the move
  is shown byte-identical rather than argued to be. When you change a default
  prompt on purpose, update its hash here in the same change.
* **Nothing downstream sees a reference.** Every preset is merged through the real
  ``merge_config_with_defaults`` and each prompt must come out as text: the
  preset's own when it sets one, the file's when it does not.
* **The files ship.** A wheel is built from a copy of the package and the defaults
  are loaded from that wheel alone; a prompt file the wheel lacks is a Lambda that
  cannot read its defaults (and ``ClassificationService`` swallows that failure).
* **The loader refuses what it cannot resolve safely**: a path outside
  ``prompts/``, a suffix the package does not ship, a mapping with another key.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import zipfile
from pathlib import Path
from typing import Any, Dict, Iterator, Tuple

import pytest
import yaml

from idp_common.config import merge_utils
from idp_common.config.merge_utils import (
    DEFAULT_PROMPT_SUFFIX,
    DEFAULT_PROMPTS_DIR,
    FILE_REFERENCE_KEY,
    _resolve_file_references,
    get_system_defaults_dir,
    load_system_defaults,
    load_system_defaults_file,
    merge_config_with_defaults,
)
from idp_common.config.migrations import migrate_config

pytestmark = pytest.mark.unit

_PKG_DIR = Path(merge_utils.__file__).resolve().parents[2]
_REPO_ROOT = _PKG_DIR.parents[1]
_PRESETS_DIR = _REPO_ROOT / "config_library" / "unified"
_CLASSIFICATION_YAML = "base-classification.yaml"

#: SHA-256 of each default classification prompt, keyed by its path under
#: ``classification``, measured from the inline YAML on commit 2745e9ea0 (the
#: commit this move was made on). Update an entry when you edit that prompt.
BASE_COMMIT_PROMPT_SHA256 = {
    "combined.page.system_prompt": "5a238d811b1a1a9bbc762b219f212cdeab65300a89834d10e25e7d4abbd3f7ea",
    "combined.page.task_prompt": "1897a0ae3626fbd0e07543a0697277b948187a3055d0ca5f0d0609e71490ed60",
    "combined.packet.system_prompt": "82c255fa20c8b90f1d6011001fb4d36669d28cad6d162bf9b3bc5e015c8fd403",
    "combined.packet.task_prompt": "4f346127ef6a3906751680f7587e6730148690f159d906e74918d18d2d304104",
    "combined.llm.confidence.task_prompt_topk": "9d842acec309d38f6797314d5679a5992ea5c49a718b02f478f437ede355465e",
    "combined.llm.confidence.task_prompt_verbalized": "ac378626ba610f62083c98a0807aeab83655a3388145d3b383d913f749c0f3ba",
    "combined.llm.confidence.task_prompt_topk_packet": "a412aa97331649cd139cf0d48d37c687c2115f3dccd1e5182d742c535eb6cf91",
    "combined.llm.confidence.task_prompt_verbalized_packet": "c41205c19bb0a45451ba5eb10c0f8db5f34fe3eb8013c2b78163ee58f70b07ff",
    "split_then_classify.splitting.llm.page.system_prompt": "4bee2cf5d71aa2cf95fae03c2ca33e8252e5c6fab154909cf8947c37b4495d9f",
    "split_then_classify.splitting.llm.page.task_prompt": "a35ccd3ca121bb688bf93fa9ca7ced1cadd9302b2e363d70b3e9f5cbb586e582",
    "split_then_classify.splitting.llm.packet.system_prompt": "0b67c79c10e7c7d028bd4f9273b3fc770d7e4e1e4d6929c6589ba38c3259cd7e",
    "split_then_classify.splitting.llm.packet.task_prompt": "6a458c60d1044a502c54b50a7c8178d9e468099341a6ef136878f5eaaaa578bf",
    "split_then_classify.section_classification.llm.system_prompt": "fadd7666974f248fe563537e88c3927490fc8a6e44940b9941d6abd8c6964a3e",
    "split_then_classify.section_classification.llm.task_prompt": "5fbb99e86699f07e7a84fd4440d8c0ae1bc575d40409f3c1117c514404184007",
    "split_then_classify.section_classification.llm.confidence.task_prompt_topk": "62141752d2a286b9de5e7087f88c0587234c7b482b5d6fee842ac973fc6bf22f",
    "split_then_classify.section_classification.llm.confidence.task_prompt_verbalized": "1166dfa47e83971fcf7bdafeadbec105d3a62884ad5c11042e4b4179567120d1",
}


def _raw_classification() -> Dict[str, Any]:
    """``base-classification.yaml`` as written, references unresolved."""
    path = get_system_defaults_dir() / _CLASSIFICATION_YAML
    return yaml.safe_load(path.read_text(encoding="utf-8"))["classification"]


def _references(node: Any, path: Tuple[str, ...] = ()) -> Iterator[Tuple[str, str]]:
    """Every ``(dotted path, referenced file)`` pair in a raw defaults node."""
    if isinstance(node, dict):
        if FILE_REFERENCE_KEY in node:
            yield ".".join(path), node[FILE_REFERENCE_KEY]
            return
        for key, value in node.items():
            yield from _references(value, (*path, key))


def _at(node: Any, dotted: str) -> Any:
    for key in dotted.split("."):
        if not isinstance(node, dict) or key not in node:
            return _MISSING
        node = node[key]
    return node


_MISSING = object()


def _contains_reference(node: Any) -> bool:
    if isinstance(node, dict):
        return FILE_REFERENCE_KEY in node or any(
            _contains_reference(v) for v in node.values()
        )
    if isinstance(node, list):
        return any(_contains_reference(v) for v in node)
    return False


# ---------------------------------------------------------------------------
# Layout
# ---------------------------------------------------------------------------


def test_every_default_classification_prompt_is_a_file_reference():
    """No prompt is inline any more, and each names the file its path implies.

    The name is the prompt's key path under ``classification``, so a help-text link
    or a reader can find a prompt's file from its key alone.
    """
    refs = dict(_references(_raw_classification()))
    assert set(refs) == set(BASE_COMMIT_PROMPT_SHA256), (
        "the prompts referenced from base-classification.yaml are not the set this "
        f"file pins: only referenced {sorted(set(refs) - set(BASE_COMMIT_PROMPT_SHA256))}, "
        f"only pinned {sorted(set(BASE_COMMIT_PROMPT_SHA256) - set(refs))}"
    )
    for dotted, target in refs.items():
        assert target == (
            f"{DEFAULT_PROMPTS_DIR}/classification/{dotted}{DEFAULT_PROMPT_SUFFIX}"
        ), f"classification.{dotted} references {target!r}"


def test_no_prompt_is_still_inline():
    """A prompt key whose value is a string is a prompt nobody moved."""
    from idp_common.prompt_comments import is_prompt_key

    def inline(node: Any, path: Tuple[str, ...] = ()) -> Iterator[str]:
        if isinstance(node, dict):
            for key, value in node.items():
                if is_prompt_key(key) and isinstance(value, str) and value:
                    yield ".".join((*path, key))
                else:
                    yield from inline(value, (*path, key))

    assert not list(inline(_raw_classification()))


def test_every_prompt_file_is_referenced():
    """An orphaned prompt file would be a default nothing reads."""
    prompts = get_system_defaults_dir() / DEFAULT_PROMPTS_DIR / "classification"
    on_disk = {p.name for p in prompts.iterdir()}
    referenced = {Path(t).name for _, t in _references(_raw_classification())}
    assert on_disk, f"no prompt files under {prompts}"
    assert on_disk == referenced


# ---------------------------------------------------------------------------
# Same text as before the move
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("pattern", ["pattern-2"])
def test_resolved_prompts_are_byte_identical_to_the_inline_yaml(pattern):
    classification = load_system_defaults(pattern)["classification"]
    changed = {}
    for dotted, digest in BASE_COMMIT_PROMPT_SHA256.items():
        value = _at(classification, dotted)
        assert isinstance(value, str), f"classification.{dotted} resolved to {value!r}"
        actual = hashlib.sha256(value.encode("utf-8")).hexdigest()
        if actual != digest:
            changed[dotted] = actual
    assert not changed, (
        "these default prompts no longer resolve to the text they had: "
        f"{sorted(changed)}. If you edited one on purpose, update its entry in "
        "BASE_COMMIT_PROMPT_SHA256."
    )


def test_one_defaults_file_reads_with_its_references_resolved():
    """``load_system_defaults_file`` is how a reader gets one ``base-*.yaml``."""
    one = load_system_defaults_file(_CLASSIFICATION_YAML)
    assert not _contains_reference(one)
    merged = load_system_defaults("pattern-2")["classification"]
    for dotted in BASE_COMMIT_PROMPT_SHA256:
        assert _at(one["classification"], dotted) == _at(merged, dotted), dotted


def test_pattern_one_inherits_no_classification_prompts():
    """pattern-1 (BDA) does not inherit base-classification.yaml at all."""
    assert not _contains_reference(load_system_defaults("pattern-1"))


def _preset_files():
    files = sorted(_PRESETS_DIR.rglob("*.yaml"))
    assert files, f"no presets under {_PRESETS_DIR}"
    return files


@pytest.mark.parametrize(
    "preset", _preset_files(), ids=lambda p: str(p.relative_to(_PRESETS_DIR))
)
def test_every_preset_merges_to_prompt_text(preset):
    """Each preset through the real merge: a prompt is the preset's or the file's."""
    user = yaml.safe_load(preset.read_text(encoding="utf-8")) or {}
    merged = merge_config_with_defaults(user, "pattern-2")
    assert not _contains_reference(merged), f"{preset} merged with a file reference"

    own = migrate_config(json.loads(json.dumps(user))).get("classification") or {}
    prompts = get_system_defaults_dir() / DEFAULT_PROMPTS_DIR / "classification"
    for dotted in BASE_COMMIT_PROMPT_SHA256:
        value = _at(merged["classification"], dotted)
        preset_value = _at(own, dotted)
        if preset_value is _MISSING:
            text = (prompts / f"{dotted}{DEFAULT_PROMPT_SUFFIX}").read_text("utf-8")
            assert value == text.removesuffix("\n"), dotted
        else:
            assert value == preset_value, dotted


# ---------------------------------------------------------------------------
# The loader's rules
# ---------------------------------------------------------------------------


@pytest.fixture
def defaults_dir(tmp_path: Path) -> Path:
    (tmp_path / DEFAULT_PROMPTS_DIR / "s").mkdir(parents=True)
    return tmp_path


@pytest.mark.parametrize(
    ("content", "resolved"),
    [("abc\n", "abc"), ("abc", "abc"), ("abc\n\n", "abc\n"), ("a\r\nb\r\n", "a\nb")],
)
def test_one_trailing_newline_is_removed(defaults_dir, content, resolved):
    """CRLF reads as LF, as a YAML block scalar does, so a checkout with
    ``core.autocrlf`` resolves to the same prompt as any other."""
    (defaults_dir / DEFAULT_PROMPTS_DIR / "s" / "p.txt").write_bytes(content.encode())
    node = {"x": {"y": {FILE_REFERENCE_KEY: f"{DEFAULT_PROMPTS_DIR}/s/p.txt"}}}
    assert _resolve_file_references(node, defaults_dir) == {"x": {"y": resolved}}


def test_references_inside_lists_resolve(defaults_dir):
    (defaults_dir / DEFAULT_PROMPTS_DIR / "s" / "p.txt").write_text("t\n")
    node = [{"a": {FILE_REFERENCE_KEY: f"{DEFAULT_PROMPTS_DIR}/s/p.txt"}}, 3]
    assert _resolve_file_references(node, defaults_dir) == [{"a": "t"}, 3]


@pytest.mark.parametrize(
    "reference",
    [
        "../outside.txt",
        f"{DEFAULT_PROMPTS_DIR}/../base.txt",
        f"{DEFAULT_PROMPTS_DIR}/s/p.md",
        f"{DEFAULT_PROMPTS_DIR}/s/p",
        "base.txt",
        "/etc/hostname",
        "",
        None,
        42,
        DEFAULT_PROMPTS_DIR,
    ],
)
def test_a_reference_outside_the_shipped_prompt_files_is_refused(
    defaults_dir, reference
):
    # Every candidate exists, so only the rule — not a missing file — can refuse it.
    (defaults_dir.parent / "outside.txt").write_text("x")
    (defaults_dir / "base.txt").write_text("x")
    (defaults_dir / DEFAULT_PROMPTS_DIR / "s" / "p.md").write_text("x")
    (defaults_dir / DEFAULT_PROMPTS_DIR / "s" / "p").write_text("x")
    with pytest.raises(ValueError):
        _resolve_file_references({FILE_REFERENCE_KEY: reference}, defaults_dir)


def test_a_symlink_out_of_the_prompts_directory_is_refused(defaults_dir):
    (defaults_dir.parent / "secret.txt").write_text("x")
    (defaults_dir / DEFAULT_PROMPTS_DIR / "s" / "link.txt").symlink_to(
        defaults_dir.parent / "secret.txt"
    )
    with pytest.raises(ValueError):
        _resolve_file_references(
            {FILE_REFERENCE_KEY: f"{DEFAULT_PROMPTS_DIR}/s/link.txt"}, defaults_dir
        )


def test_a_reference_beside_another_key_is_refused(defaults_dir):
    (defaults_dir / DEFAULT_PROMPTS_DIR / "s" / "p.txt").write_text("t")
    node = {FILE_REFERENCE_KEY: f"{DEFAULT_PROMPTS_DIR}/s/p.txt", "other": 1}
    with pytest.raises(ValueError, match="no other key"):
        _resolve_file_references(node, defaults_dir)


def test_a_missing_prompt_file_fails_loudly(defaults_dir):
    with pytest.raises(FileNotFoundError):
        _resolve_file_references(
            {FILE_REFERENCE_KEY: f"{DEFAULT_PROMPTS_DIR}/s/absent.txt"}, defaults_dir
        )


def test_an_operator_configuration_is_never_resolved(defaults_dir):
    """Only the shipped defaults are resolved; a merged-in reference stays inert."""
    user = {"classification": {"combined": {"page": {"task_prompt": "mine"}}}}
    user["notes"] = {FILE_REFERENCE_KEY: "prompts/classification/x.txt"}
    merged = merge_config_with_defaults(user, "pattern-2")
    assert merged["notes"] == {FILE_REFERENCE_KEY: "prompts/classification/x.txt"}
    assert merged["classification"]["combined"]["page"]["task_prompt"] == "mine"


# ---------------------------------------------------------------------------
# Packaging: the prompt files are in the wheel and load from it
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def built_wheel() -> Iterator[Path]:
    """A wheel built offline from a copy of the package (no build isolation).

    The same build ``scripts/tests/test_package_discovery.py`` uses: a copy, so no
    ``build/`` is left in the tree, and ``setuptools.build_meta`` directly, so
    nothing is downloaded.
    """
    tmp = Path(tempfile.mkdtemp(prefix="prompt-files-wheel-"))
    try:
        source = tmp / "src"
        shutil.copytree(
            _PKG_DIR,
            source,
            ignore=shutil.ignore_patterns(
                "build", "dist", "*.egg-info", "__pycache__", ".pytest_cache", "tests"
            ),
        )
        out = tmp / "wheel"
        out.mkdir()
        proc = subprocess.run(  # nosec B603 - fixed argv, no shell
            [
                sys.executable,
                "-c",
                "import sys; from setuptools import build_meta; "
                "build_meta.build_wheel(sys.argv[1])",
                str(out),
            ],
            cwd=source,
            capture_output=True,
            text=True,
            env={**os.environ, "PYTHONPATH": str(source)},
            timeout=300,
        )
        assert proc.returncode == 0, proc.stderr[-2000:]
        wheels = sorted(out.glob("*.whl"))
        assert len(wheels) == 1, wheels
        yield wheels[0]
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_every_referenced_prompt_file_is_in_the_wheel(built_wheel):
    with zipfile.ZipFile(built_wheel) as zf:
        names = set(zf.namelist())
    base = "idp_common/config/system_defaults/"
    targets = {t for _, t in _references(_raw_classification())}
    assert targets
    missing = sorted(t for t in targets if base + t not in names)
    assert not missing, f"the wheel does not ship {missing}"


def test_the_wheel_alone_loads_the_same_defaults(built_wheel, tmp_path):
    """Load the defaults from the extracted wheel in a fresh interpreter.

    ``-I`` and a ``sys.path`` holding the wheel first mean the import cannot fall
    back to this checkout; the subprocess asserts where it imported from.
    """
    site = tmp_path / "site"
    with zipfile.ZipFile(built_wheel) as zf:
        zf.extractall(site)
    script = (
        "import json, sys; sys.path.insert(0, sys.argv[1]); "
        "import idp_common; "
        "assert idp_common.__file__.startswith(sys.argv[1]), idp_common.__file__; "
        "from idp_common.config.merge_utils import load_system_defaults; "
        "print(json.dumps(load_system_defaults('pattern-2'), sort_keys=True))"
    )
    proc = subprocess.run(  # nosec B603 - fixed argv, no shell
        [sys.executable, "-I", "-c", script, str(site)],
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert proc.returncode == 0, proc.stderr[-2000:]
    assert json.loads(proc.stdout) == json.loads(
        json.dumps(load_system_defaults("pattern-2"), sort_keys=True)
    )


# ---------------------------------------------------------------------------
# The configuration editor links each prompt field to its own file
# ---------------------------------------------------------------------------


def test_every_classification_prompt_field_links_its_own_default_file():
    """The help text the editor renders comes from the unified pattern template."""
    template = (_REPO_ROOT / "patterns" / "unified" / "template.yaml").read_text(
        encoding="utf-8"
    )
    missing = [
        dotted
        for dotted in BASE_COMMIT_PROMPT_SHA256
        if f"/system_defaults/{DEFAULT_PROMPTS_DIR}/classification/{dotted}"
        f"{DEFAULT_PROMPT_SUFFIX}) (`{dotted}`)" not in template
    ]
    assert not missing, f"no help-text link to these prompts' files: {missing}"
    assert "system_defaults/base-classification.yaml)" not in template
