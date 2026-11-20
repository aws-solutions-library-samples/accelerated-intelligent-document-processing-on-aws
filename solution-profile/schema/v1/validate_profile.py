#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.12"
# dependencies = ["pyyaml==6.0.3", "jsonschema==4.25.1"]
# ///
"""Validate a solution profile against the Solution Profile v1 contract.

This is the reference validator. A solution repository may copy it unchanged,
or call it, so every producer and consumer applies the same rules (README.md,
"Validation").

Usage:
    validate_profile.py PROFILE_DIR [--repo-root DIR] [--version-file FILE] [--release]

Exit status 0 means valid, 1 means invalid (each problem is printed), and 2
means bad arguments.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Any

import yaml
from jsonschema import Draft202012Validator

SCHEMA_PATH = Path(__file__).with_name("solution-profile.schema.json")

# Manifest key under `files` -> (schema $defs name, top-level list key, reference kind).
FILES: dict[str, tuple[str, str | None, str | None]] = {
    "capabilities": ("capabilitiesFile", "capabilities", "capability"),
    "extension_points": ("extensionPointsFile", "extension_points", "extension"),
    "limitations": ("limitationsFile", "limitations", "limitation"),
    "discovery_questions": ("discoveryQuestionsFile", "questions", "question"),
    "sizing": ("sizingFile", None, None),
    "deployment": ("deploymentFile", "options", "deployment"),
    "delivery": ("deliveryFile", None, None),
    "changes": ("changesFile", None, None),
}


class _StringDateLoader(yaml.SafeLoader):
    """A safe loader that keeps dates as strings, as the JSON Schema expects."""


_StringDateLoader.yaml_implicit_resolvers = {
    key: [(tag, rx) for tag, rx in resolvers if tag != "tag:yaml.org,2002:timestamp"]
    for key, resolvers in yaml.SafeLoader.yaml_implicit_resolvers.items()
}

REF = re.compile(r"^(capability|extension|limitation|question|deployment|prerequisite|delivery):(.+)$")


def load_yaml(path: Path, problems: list[str]) -> Any:
    """Return the parsed YAML file, or None after recording why it could not be read."""
    try:
        return yaml.load(path.read_text(encoding="utf-8"), Loader=_StringDateLoader)  # noqa: S506 - a SafeLoader subclass
    except FileNotFoundError:
        problems.append(f"{path.name}: missing")
    except yaml.YAMLError as exc:
        problems.append(f"{path.name}: not valid YAML: {exc}")
    return None


def schema_errors(schema: dict[str, Any], defname: str | None, data: Any, label: str) -> list[str]:
    """Return one message per schema violation, with the JSON path that failed."""
    target = {"$ref": f"#/$defs/{defname}"} if defname else {"$ref": "#/$defs/manifest"}
    validator = Draft202012Validator({**schema, **target})
    out = []
    for err in sorted(validator.iter_errors(data), key=lambda e: list(e.absolute_path)):
        where = "/".join(str(p) for p in err.absolute_path) or "(root)"
        out.append(f"{label}: {where}: {err.message}")
    return out


def walk(value: Any):
    """Yield every (key, value) pair in nested dicts and lists."""
    if isinstance(value, dict):
        for key, item in value.items():
            yield key, item
            yield from walk(item)
    elif isinstance(value, list):
        for item in value:
            yield from walk(item)


def main(argv: list[str]) -> int:
    """Validate the profile and print every problem found."""
    parser = argparse.ArgumentParser(
        description="Validate a solution profile against the Solution Profile v1 contract."
    )
    parser.add_argument("profile_dir", type=Path)
    parser.add_argument(
        "--repo-root", type=Path, help="root that evidence paths are relative to (default: parent of PROFILE_DIR)"
    )
    parser.add_argument("--version-file", type=Path, help="file whose stripped content must equal `version`")
    parser.add_argument(
        "--release", action="store_true", help="also apply the release rules: reviewed_for_version and a changes entry"
    )
    args = parser.parse_args(argv)

    profile_dir: Path = args.profile_dir
    repo_root: Path = args.repo_root or profile_dir.resolve().parent
    if not profile_dir.is_dir():
        print(f"not a directory: {profile_dir}", file=sys.stderr)
        return 2

    schema = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
    problems: list[str] = []

    manifest = load_yaml(profile_dir / "solution-profile.yaml", problems)
    if manifest is None:
        return report(problems)
    problems += schema_errors(schema, None, manifest, "solution-profile.yaml")

    ids: dict[str, set[str]] = {
        kind: set()
        for kind in ("capability", "extension", "limitation", "question", "deployment", "prerequisite", "delivery")
    }
    docs: dict[str, Any] = {}
    for key, (defname, list_key, kind) in FILES.items():
        name = (manifest.get("files") or {}).get(key)
        if not name:
            continue
        data = load_yaml(profile_dir / name, problems)
        if data is None:
            continue
        docs[name] = data
        problems += schema_errors(schema, defname, data, name)
        if list_key and kind and isinstance(data, dict):
            collect_ids(data.get(list_key) or [], kind, name, ids, problems)
        if key == "deployment" and isinstance(data, dict):
            collect_ids(data.get("prerequisites") or [], "prerequisite", name, ids, problems)
        if key == "delivery" and isinstance(data, dict):
            collect_ids((data.get("day_one") or []) + (data.get("typical_work") or []), "delivery", name, ids, problems)

    for name, data in docs.items():
        for key, value in walk(data):
            values = value if isinstance(value, list) else [value]
            for item in values:
                if not isinstance(item, str):
                    continue
                match = REF.match(item)
                if match and key in {
                    "related",
                    "detect_with",
                    "mitigations",
                    "tests",
                    "refs",
                    "affects",
                    "applies_to",
                    "question",
                }:
                    if match.group(2) not in ids[match.group(1)]:
                        problems.append(f"{name}: unresolved reference {item}")
                if key == "evidence" and not item.startswith("https://"):
                    path = item.split("#", 1)[0]
                    if not (repo_root / path).exists():
                        problems.append(f"{name}: evidence path does not exist: {path}")
        if name == (manifest.get("files") or {}).get("delivery") and isinstance(data, dict):
            known = ids["delivery"]
            for step in (data.get("walking_skeleton") or {}).get("steps", []):
                if step not in known:
                    problems.append(f"{name}: walking_skeleton step is not a delivery id: {step}")
            for work in data.get("typical_work") or []:
                for dep in work.get("depends_on") or []:
                    if dep not in known:
                        problems.append(f"{name}: {work.get('id')}: depends_on is not a delivery id: {dep}")

    questions = docs.get((manifest.get("files") or {}).get("discovery_questions", ""), {}) or {}
    for question in questions.get("questions") or []:
        for follow in question.get("follow_ups") or []:
            if follow not in ids["question"]:
                problems.append(f"discovery questions: {question.get('id')}: unknown follow-up {follow}")

    for skill in manifest.get("skills") or []:
        if not (profile_dir / skill / "SKILL.md").is_file():
            problems.append(f"solution-profile.yaml: skill has no SKILL.md: {skill}")

    version = manifest.get("version")
    if args.version_file:
        expected = args.version_file.read_text(encoding="utf-8").strip()
        if version != expected:
            problems.append(f"solution-profile.yaml: version {version} does not equal {args.version_file} ({expected})")
    if args.release:
        if manifest.get("reviewed_for_version") != version:
            problems.append(
                f"solution-profile.yaml: reviewed_for_version {manifest.get('reviewed_for_version')} "
                f"does not equal version {version}: review the curated files for this release"
            )
        changes = docs.get((manifest.get("files") or {}).get("changes", ""), {}) or {}
        if version not in {r.get("version") for r in changes.get("releases") or []}:
            problems.append(f"changes: no entry for version {version}")

    return report(problems)


def collect_ids(items: list[Any], kind: str, name: str, ids: dict[str, set[str]], problems: list[str]) -> None:
    """Record each item's id under its kind, and report duplicates."""
    for item in items:
        if not isinstance(item, dict) or "id" not in item:
            continue
        if item["id"] in ids[kind]:
            problems.append(f"{name}: duplicate {kind} id {item['id']}")
        ids[kind].add(item["id"])


def report(problems: list[str]) -> int:
    """Print the problems and return the exit status."""
    for problem in problems:
        print(problem)
    print("valid" if not problems else f"{len(problems)} problem(s)")
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
