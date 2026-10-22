#!/usr/bin/env python3
"""Refuse a benchmark config whose classification settings a target release drops.

The cross-release A/B uploads one config file to a previously published release.
That release's ``IDPConfig`` ignores keys it does not declare, so a setting it does
not know is not rejected, it is silently replaced by the release's code default,
and the run still produces a complete set of numbers. This check loads the TARGET
release's own models and reports, as errors:

* every key under ``classification`` that the release's ``ClassificationConfig``
  would drop (walked recursively into its sub-models);
* when the file carries the back-compat flat keys written by
  ``classification_flat.with_flat_classification``, every flat setting whose value
  the old release resolves differently from what the file's nested one-stage
  blocks ask for (read by the current reader with the flat keys removed). In that
  case the v0.9 blocks themselves
  (``classification_flat.V09_BLOCKS``) are expected to be dropped by the old
  release, because the flat keys carry their content; every other dropped key is
  still an error.

The old release's code is taken from a git tag with ``git archive`` (no worktree,
nothing installed) and imported in a separate isolated interpreter, so it cannot be
confused with the current checkout's ``idp_common``. The child verifies that the
package it imported is the extracted one before it answers.

Usage:
  python3 benchmarks/harness/compat/release_model_check.py --release v0.6.12 \\
      <config.yaml> [<config.yaml> ...]
Exit status 1 if any config has an error.
"""

from __future__ import annotations

import argparse
import functools
import json
import os
import subprocess  # nosec B404 - runs git and this interpreter with fixed argv
import sys
import tempfile
import typing
from typing import Any, Dict, List, Tuple

_HERE = os.path.dirname(os.path.abspath(__file__))
_REPO = os.path.dirname(os.path.dirname(os.path.dirname(_HERE)))

if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

from classification_flat import METHOD_FOR_GRANULARITY, V09_BLOCKS  # noqa: E402


def dropped_keys(model_cls: Any, data: Any, prefix: str) -> List[str]:
    """Dotted paths in ``data`` that the pydantic model ``model_cls`` does not declare.

    Descends into fields typed as a sub-model (including ``Optional[Model]``).
    Stops at fields typed as a dict, a list or ``Any``: those are free-form, and
    a model with ``extra="allow"`` keeps unknown keys, so neither drops anything.
    """
    from pydantic import BaseModel

    if not isinstance(data, dict):
        return []
    if (getattr(model_cls, "model_config", {}) or {}).get("extra") == "allow":
        return []
    fields = model_cls.model_fields
    names = set(fields)
    names.update(f.alias for f in fields.values() if f.alias)
    out: List[str] = []
    for key, value in data.items():
        path = f"{prefix}.{key}"
        if key not in names:
            out.append(path)
            continue
        field = fields.get(key) or next(f for f in fields.values() if f.alias == key)
        annotation = field.annotation
        candidates = (annotation,) + tuple(typing.get_args(annotation) or ())
        sub = next(
            (c for c in candidates if isinstance(c, type) and issubclass(c, BaseModel)),
            None,
        )
        if sub is not None and isinstance(value, dict):
            out.extend(dropped_keys(sub, value, path))
    return sorted(out)


# The child: imports the TARGET release's idp_common from <lib_root>, applies that
# release's own migration chain (what its ConfigurationManager does on read),
# validates, and reports what it drops and what it resolves. Run under -I so
# neither PYTHONPATH nor the working directory can put the current checkout first.
_CHILD = r"""
import json, os, sys
harness_compat, lib_root = sys.argv[1], sys.argv[2]
sys.path.insert(0, lib_root)
sys.path.insert(1, harness_compat)
import idp_common
where = os.path.realpath(idp_common.__file__)
if not where.startswith(os.path.realpath(lib_root) + os.sep):
    print(json.dumps({"fatal": f"idp_common resolved to {where}, not {lib_root}"}))
    sys.exit(0)
from idp_common.config.models import IDPConfig
from idp_common.config import migrations
from release_model_check import dropped_keys
cfg = json.load(sys.stdin)
migrate = getattr(migrations, "migrate_config", None)
if migrate is not None:
    cfg = migrate(cfg)
annotation = IDPConfig.model_fields["classification"].annotation
dropped = dropped_keys(annotation, cfg.get("classification") or {}, "classification")
try:
    resolved = IDPConfig.model_validate(cfg).classification.model_dump(mode="json")
    error = None
except Exception as exc:  # report, do not crash: the caller turns it into an error
    resolved, error = None, f"{type(exc).__name__}: {exc}"
print(json.dumps({"dropped": dropped, "classification": resolved, "error": error}))
"""


@functools.lru_cache(maxsize=None)
def extract_release_lib(tag: str) -> str:
    """Extract ``lib/idp_common_pkg`` at git ``tag`` into a temp dir; return its root.

    Raises ``RuntimeError`` naming the tag if git cannot produce it (a shallow
    clone without tags, a mistyped tag).
    """
    dest = tempfile.mkdtemp(prefix=f"idp-release-{tag.replace('/', '-')}-")
    archive = subprocess.run(  # nosec B603 B607 - fixed argv, tag is a ref name
        ["git", "-C", _REPO, "archive", "--format=tar", tag, "lib/idp_common_pkg"],
        capture_output=True,
        check=False,
    )
    if archive.returncode != 0:
        raise RuntimeError(
            f"cannot read lib/idp_common_pkg at {tag!r}: "
            f"{archive.stderr.decode(errors='replace').strip()} "
            f"(fetch the tag with `git fetch --tags` and re-run)"
        )
    untar = subprocess.run(  # nosec B603 B607 - fixed argv
        ["tar", "-x", "-C", dest],
        input=archive.stdout,
        capture_output=True,
        check=False,
    )
    if untar.returncode != 0:
        raise RuntimeError(f"cannot unpack {tag!r}: {untar.stderr.decode().strip()}")
    return os.path.join(dest, "lib", "idp_common_pkg")


def resolve_in_release(cfg: Dict[str, Any], lib_root: str) -> Dict[str, Any]:
    """Run the child against the release at ``lib_root``; return its JSON answer."""
    child = subprocess.run(  # nosec B603 - fixed argv; config passed on stdin
        [sys.executable, "-I", "-c", _CHILD, _HERE, lib_root],
        input=json.dumps(cfg, default=str),
        capture_output=True,
        text=True,
        check=False,
    )
    lines = [ln for ln in child.stdout.splitlines() if ln.startswith("{")]
    if child.returncode != 0 or not lines:
        raise RuntimeError(
            f"release model check could not run against {lib_root}: "
            f"{child.stderr.strip()[-800:]}"
        )
    return json.loads(lines[-1])


def _get(node: Any, path: Tuple[str, ...]) -> Any:
    for key in path:
        if not isinstance(node, dict):
            return None
        node = node.get(key)
    return node


def resolved_flat_pairs(granularity: str) -> List[Tuple[str, Tuple[str, ...]]]:
    """(old flat field, current nested path) pairs a single-pass run reads."""
    llm = ("one_stage", "llm")
    pairs: List[Tuple[str, Tuple[str, ...]]] = [
        (key, llm + (key,))
        for key in (
            "model",
            "model_lambda_hook_arn",
            "temperature",
            "top_p",
            "top_k",
            "reasoning_effort",
            "max_tokens",
            "image",
        )
    ]
    pairs += [
        ("system_prompt", ("one_stage", granularity, "system_prompt")),
        ("task_prompt", ("one_stage", granularity, "task_prompt")),
        ("contextPagesCount", ("one_stage", "page", "contextPagesCount")),
        ("enforceValidClasses", llm + ("validation", "enforceValidClasses")),
        ("maxValidationRetries", llm + ("validation", "maxValidationRetries")),
        ("invalidClassFallback", ("fallback_class",)),
        ("sectionSplitting", ("file_shape", "sectionSplitting")),
        ("maxPagesForClassification", ("file_shape", "maxPagesForClassification")),
    ]
    return pairs


def check_config(cfg: Dict[str, Any], lib_root: str) -> List[str]:
    """Errors for ``cfg`` against the release whose ``idp_common`` is at ``lib_root``.

    An empty list means every classification key in the file is one the release
    reads and, for a back-compat file, that the release resolves each flat setting
    to the value the file's nested one-stage blocks ask for.
    """
    answer = resolve_in_release(cfg, lib_root)
    if answer.get("fatal"):
        return [answer["fatal"]]
    errors: List[str] = []
    if answer.get("error"):
        errors.append(f"the release's IDPConfig rejects this config: {answer['error']}")

    classification = cfg.get("classification") or {}
    has_flat = "classificationMethod" in classification
    for path in answer["dropped"]:
        root = path.split(".")[1]
        if has_flat and root in V09_BLOCKS:
            continue  # carried by the flat keys; compared value by value below
        errors.append(
            f"{path} is not read by this release: its IDPConfig drops the key and "
            f"runs the code default instead"
        )
    if not has_flat or answer.get("classification") is None:
        return errors

    # A back-compat file: compare what the release resolves from the flat keys
    # with what the NESTED blocks ask for — the file with its flat keys removed,
    # read by the current reader. Comparing against the whole file instead would
    # pass a flat key that disagrees with its nested block, because the current
    # reader lets the flat key win too, so both sides would agree on the wrong value.
    sys.path.insert(0, os.path.join(_REPO, "lib", "idp_common_pkg"))
    import copy

    from idp_common.config.migrations import migrate_config
    from idp_common.config.migrations.v08_to_v09 import FLAT_KEYS
    from idp_common.config.models import IDPConfig

    nested_only = copy.deepcopy(cfg)
    for key in FLAT_KEYS | {"classificationMethod"}:
        nested_only["classification"].pop(key, None)
    current = IDPConfig.model_validate(
        migrate_config(nested_only)
    ).classification.model_dump(mode="json")
    old = answer["classification"]
    granularity = str(_get(current, ("one_stage", "granularity")) or "page")
    expected_method = METHOD_FOR_GRANULARITY.get(granularity)
    if old.get("classificationMethod") != expected_method:
        errors.append(
            f"classificationMethod resolves to {old.get('classificationMethod')!r} on "
            f"this release; one_stage.granularity {granularity!r} needs "
            f"{expected_method!r}"
        )
    for flat_key, nested in resolved_flat_pairs(granularity):
        if flat_key not in old:
            errors.append(f"classification.{flat_key} does not exist on this release")
            continue
        want = _get(current, nested)
        got = old[flat_key]
        if isinstance(got, dict) and isinstance(want, dict):
            want = {k: want.get(k) for k in got}
        if got != want:
            errors.append(
                f"classification.{flat_key} resolves to {_short(got)} on this release "
                f"but classification.{'.'.join(nested)} asks for {_short(want)}"
            )
    old_conf = old.get("confidence") or {}
    new_conf = _get(current, ("one_stage", "llm", "confidence")) or {}
    for key, got in old_conf.items():
        if got != new_conf.get(key):
            errors.append(
                f"classification.confidence.{key} resolves to {_short(got)} on this "
                f"release but one_stage.llm.confidence.{key} asks for "
                f"{_short(new_conf.get(key))}"
            )
    return errors


def _short(value: Any) -> str:
    text = repr(value)
    return text if len(text) <= 80 else text[:77] + "..."


def main(argv: List[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--release", required=True, help="git tag, e.g. v0.6.12")
    ap.add_argument("configs", nargs="+")
    a = ap.parse_args(argv)
    import yaml

    lib_root = extract_release_lib(a.release)
    failed = 0
    for path in a.configs:
        with open(path) as fh:
            errors = check_config(yaml.safe_load(fh), lib_root)
        status = "ok" if not errors else f"{len(errors)} error(s)"
        print(f"{os.path.basename(path)}: {status}")
        for err in errors:
            print(f"    {err}")
        failed += bool(errors)
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
