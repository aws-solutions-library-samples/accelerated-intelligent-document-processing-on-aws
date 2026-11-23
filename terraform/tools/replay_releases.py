#!/usr/bin/env python3
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Replay the current translator over the templates of past releases.

For each release tag (``v0.5.<n>`` and ``v0.6.<n>`` by default), the templates
are extracted with ``git archive`` into a temporary directory, so no checkout
is touched, and translated with the current translator ``--offline --survey``.
Per release it records the resource types the template tree uses, the types
the translator cannot map, and every failure by class. That answers how much
mapping work a release has needed: a release whose template added a type or a
property the translator did not yet handle shows up as a failure of that
class, on the release that introduced it.

Failures are classed by what they need:

- ``unmapped type``, ``unmapped property``, ``unmapped attribute``,
  ``intrinsic``: mapping work in the translator (aws_mappers, the awscc
  schema, translate.py);
- ``create-only list``, ``custom-output edge``, ``completeness``: a check
  added with the upgrade-safety work, which needs a declaration or a fix;
- ``template shape``: the translator could not read the template at all (an
  old nested-stack layout, a SAM construct the expander rejects). These are not
  about mappings and are reported separately.

    python3 terraform/tools/replay_releases.py [--tags v0.6.13 v0.6.14] [--jobs 4]

It makes no AWS call. The output is a Markdown table on stdout and, with
``--json <path>``, the raw results.

``--check`` is the gate form ``make terraform-check`` runs. It replays the releases
listed in ``terraform/tools/replay_releases.json``, each at the commit recorded
there rather than at whatever a local tag names (the two forges' tags have disagreed
on one release), and exits 1 when any of them fails to translate, cannot be read, or
is not in the clone. It reads only the repository's own history, so it needs a clone
with the recorded commits in it (CI checks out full history for this job) and no
network of its own.
"""

from __future__ import annotations

import argparse
import concurrent.futures
import json
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
TRANSLATOR = REPO / "terraform" / "translator"
TAG = re.compile(r"^v0\.(5|6)\.(\d+)$")
RELEASES_FILE = Path(__file__).resolve().with_name("replay_releases.json")
_COMMIT = re.compile(r"^[0-9a-f]{40}$")

CLASSES = (
    ("unmapped type", re.compile(r"has no hashicorp/aws mapping|not in the vendored")),
    (
        "unmapped property",
        re.compile(
            r"unmapped property|has no awscc attribute in|"
            r"no awscc attribute for property|unsupported nested stack properties|unsupported resource keys"
        ),
    ),
    (
        "unmapped attribute",
        re.compile(
            r"unmapped GetAtt|awscc attribute for GetAtt|has no attribute|has no output"
        ),
    ),
    ("intrinsic", re.compile(r"unsupported intrinsic|must be literal|cannot render")),
    (
        "follows from an unmapped type",
        re.compile(r"on unknown |to unknown name "),
    ),
    ("create-only list", re.compile(r"create_only\.json")),
    ("custom-output edge", re.compile(r"STABLE_CUSTOM_OUTPUTS")),
    ("completeness", re.compile(r"not in the emitted Terraform")),
)
TYPE_IN_MESSAGE = re.compile(
    r": ((?:AWS|Custom)::[A-Za-z0-9:]+) has no hashicorp/aws mapping"
)


def release_tags() -> list[str]:
    out = subprocess.run(  # nosec B603 B607 - fixed argv
        ["git", "-C", str(REPO), "tag", "-l"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.split()
    tags = [t for t in out if TAG.match(t)]
    return sorted(tags, key=lambda t: tuple(int(x) for x in TAG.match(t).groups()))  # type: ignore[union-attr]


NESTED_URL = re.compile(r"\./([A-Za-z0-9_./-]+?)/\.aws-sam/packaged\.yaml")


def _git(*args: str) -> bytes:
    return subprocess.run(  # nosec B603 B607 - fixed argv
        ["git", "-C", str(REPO), *args], capture_output=True, check=True
    ).stdout


def extract(tag: str, dest: Path) -> None:
    """The main template and every directory a nested TemplateURL names,
    followed down the tree. ``tag`` is any revision ``git archive`` reads."""
    wanted = ["template.yaml"]
    done: set[str] = set()
    while wanted:
        paths = [p for p in wanted if p not in done]
        wanted = []
        if not paths:
            break
        done.update(paths)
        present = [
            p for p in paths if _git("ls-tree", "--name-only", tag, "--", p).strip()
        ]
        if not present:
            continue
        archive = _git("archive", "--format=tar", tag, "--", *present)
        subprocess.run(  # nosec B603 B607 - fixed argv, archive of this repository
            ["tar", "-x", "-C", str(dest)], input=archive, check=True
        )
        for p in present:
            for template in (
                [dest / p] if p.endswith(".yaml") else (dest / p).rglob("template.yaml")
            ):
                for d in NESTED_URL.findall(template.read_text(errors="replace")):
                    # TemplateURLs are relative to the repository root.
                    wanted.append(d)


def _env() -> dict[str, str]:
    roots = [str(TRANSLATOR)] + sorted(
        str(p) for p in (REPO / "lib").glob("*") if (p / "pyproject.toml").is_file()
    )
    return {**os.environ, "PYTHONPATH": os.pathsep.join(roots)}


# Every (type, top-level property) the release's template tree uses, read
# offline the way the translator reads it (SAM expanded, nested stacks local).
TYPES_SNIPPET = (
    "import json,sys\n"
    "from pathlib import Path\n"
    "from idp_tf.cli import local_loader\n"
    "from idp_tf.expand import expand, load_template, offline_placeholders\n"
    "root = Path(sys.argv[1])\n"
    "loader = local_loader(root.parent)\n"
    "pending = [expand(offline_placeholders(load_template(root)))]\n"
    "used = {}\n"
    "while pending:\n"
    "    t = pending.pop()\n"
    "    for r in t.get('Resources', {}).values():\n"
    "        used.setdefault(r['Type'], set()).update((r.get('Properties') or {}))\n"
    "        url = (r.get('Properties') or {}).get('TemplateURL')\n"
    "        if r['Type'] == 'AWS::CloudFormation::Stack' and isinstance(url, str):\n"
    "            pending.append(loader(url))\n"
    "print(json.dumps({k: sorted(v) for k, v in sorted(used.items())}))\n"
)


def classify(line: str) -> str:
    for name, pattern in CLASSES:
        if pattern.search(line):
            return name
    return "other"


def replay(tag: str, rev: str | None = None) -> dict:
    """One release's result. ``rev`` is the revision to read (default: ``tag``)."""
    with tempfile.TemporaryDirectory(prefix=f"idp-replay-{tag}-") as tmp:
        root = Path(tmp)
        extract(rev or tag, root)
        result: dict = {
            "tag": tag,
            "types": [],
            "failures": {},
            "unmapped": [],
            "shape": None,
        }
        types = subprocess.run(  # nosec B603 - fixed argv
            [sys.executable, "-c", TYPES_SNIPPET, str(root / "template.yaml")],
            capture_output=True,
            text=True,
            env=_env(),
            cwd=tmp,
            check=False,
        )
        if types.returncode == 0:
            used = json.loads(types.stdout.strip().splitlines()[-1])
            result["types"] = sorted(used)
            result["properties"] = used
        else:
            result["shape"] = _last_error(types.stderr)
        run = subprocess.run(  # nosec B603 - fixed argv
            [
                sys.executable,
                "-m",
                "idp_tf.cli",
                str(root / "template.yaml"),
                "--offline",
                "--survey",
                "--out",
                str(root / "out"),
            ],
            capture_output=True,
            text=True,
            env=_env(),
            cwd=tmp,
            check=False,
        )
        lines = [line for line in run.stdout.splitlines() if line.strip()]
        summary = [line for line in lines if re.match(r"^\d+ failure\(s\)", line)]
        if not summary:
            result["shape"] = result["shape"] or _last_error(run.stderr or run.stdout)
            return result
        result["reported"] = int(summary[-1].split()[0])
        for line in lines:
            if line is summary[-1] or line.startswith("stale declaration:"):
                continue
            cls = classify(line)
            result["failures"].setdefault(cls, []).append(line)
            m = TYPE_IN_MESSAGE.search(line)
            if m:
                result["unmapped"].append(m.group(1))
        result["unmapped"] = sorted(set(result["unmapped"]))
        return result


def _last_error(text: str) -> str:
    lines = [line for line in (text or "").strip().splitlines() if line.strip()]
    return lines[-1][:200] if lines else "no output"


def _hand_mapped() -> set[str]:
    sys.path.insert(0, str(TRANSLATOR))
    from idp_tf import aws_mappers, aws_mappers_storage

    return set(aws_mappers.MAPPERS) | set(aws_mappers_storage.STORAGE_MAPPERS)


def table(results: list[dict]) -> str:
    """One row per release. "New" columns compare with the release before."""
    hand = _hand_mapped()
    rows = [
        "| Release | Types | New types | New properties on hand-mapped types "
        "| Unmapped types | Failures by class |",
        "|---|---|---|---|---|---|",
    ]
    prev_types: set[str] | None = None
    prev_props: set[tuple[str, str]] | None = None
    for r in results:
        if r["shape"] and not r["failures"]:
            rows.append(
                f"| {r['tag']} | {len(r['types']) or '-'} | - | - | - "
                f"| template not read: {r['shape']} |"
            )
            continue
        types = set(r["types"])
        props = {(t, p) for t, ps in (r.get("properties") or {}).items() for p in ps}
        new_types = sorted(types - prev_types) if prev_types is not None else []
        new_props = (
            sorted((t, p) for t, p in props - prev_props if t in hand)
            if prev_props is not None
            else []
        )
        prev_types, prev_props = types, props
        fails = (
            ", ".join(f"{k} {len(v)}" for k, v in sorted(r["failures"].items()))
            or "none"
        )
        unmapped = ", ".join(f"`{t}`" for t in r["unmapped"]) or "none"
        newt = ", ".join(f"`{t}`" for t in new_types) or "-"
        newp = ", ".join(f"`{t.split('::')[-1]}.{p}`" for t, p in new_props) or "-"
        rows.append(
            f"| {r['tag']} | {len(types)} | {newt} | {newp} | {unmapped} | {fails} |"
        )
    return "\n".join(rows)


def load_releases(path: Path = RELEASES_FILE) -> dict[str, str]:
    """``{tag: commit}`` from the releases file, refusing an empty or malformed one:
    a gate over no release would pass having replayed nothing."""
    data = json.loads(path.read_text())
    releases = data.get("releases") if isinstance(data, dict) else None
    if not isinstance(releases, dict) or not releases:
        raise ValueError(f"{path} lists no releases")
    for tag, commit in releases.items():
        if (
            not TAG.match(tag)
            or not isinstance(commit, str)
            or not _COMMIT.match(commit)
        ):
            raise ValueError(
                f"{path}: {tag!r} -> {commit!r} is not a v0.<5|6>.<n> tag and a "
                "40-character commit"
            )
    return dict(releases)


def commit_present(commit: str) -> bool:
    return (
        subprocess.run(  # nosec B603 B607 - fixed argv
            ["git", "-C", str(REPO), "cat-file", "-e", f"{commit}^{{commit}}"],
            capture_output=True,
            check=False,
        ).returncode
        == 0
    )


def check_problems(results: list[dict]) -> list[str]:
    """Why the replayed releases fail the gate; empty when every one translated.

    A release whose template was not read, or whose resource types could not be
    listed, fails as well as one with a translation failure: neither was checked."""
    problems = []
    for r in results:
        if r.get("missing"):
            problems.append(f"{r['tag']}: {r['missing']}")
            continue
        if r["shape"]:
            problems.append(f"{r['tag']}: template not read: {r['shape']}")
        elif not r["types"]:
            problems.append(f"{r['tag']}: no resource types were read")
        for cls, lines in sorted(r["failures"].items()):
            problems.append(
                f"{r['tag']}: {len(lines)} {cls} failure(s), first: {lines[0]}"
            )
        # The translator's own count against the lines read from its output. An
        # error may span lines, so more read than reported is normal; fewer means
        # a reported failure went where this does not read it (stderr, a line
        # shape the parse skips), and that must not pass.
        read = sum(len(lines) for lines in r["failures"].values())
        if r.get("reported", 0) > read:
            problems.append(
                f"{r['tag']}: the translator reported {r['reported']} failure(s) "
                f"and only {read} were read from its output"
            )
    return problems


def run_check(releases: dict[str, str], jobs: int) -> tuple[list[dict], list[str]]:
    """Replay every listed release at its recorded commit."""
    present = {
        tag: commit for tag, commit in releases.items() if commit_present(commit)
    }
    with concurrent.futures.ThreadPoolExecutor(max_workers=jobs) as pool:
        replayed = dict(
            zip(present, pool.map(lambda t: replay(t, present[t]), present))
        )
    results = []
    for tag, commit in releases.items():
        results.append(
            replayed.get(tag)
            or {
                "tag": tag,
                "missing": (
                    f"commit {commit} is not in this clone (fetch full history: "
                    "git fetch --unshallow)"
                ),
            }
        )
    return results, check_problems(results)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument(
        "--tags", nargs="*", help="release tags (default: every v0.5.n and v0.6.n)"
    )
    ap.add_argument("--jobs", type=int, default=4)
    ap.add_argument("--json", type=Path, help="also write the raw results here")
    ap.add_argument(
        "--check",
        action="store_true",
        help=f"replay the releases in {RELEASES_FILE.name} at their recorded "
        "commits and exit 1 on any failure",
    )
    args = ap.parse_args(argv)
    if args.check:
        if args.tags:
            ap.error("--check replays the releases file; it takes no --tags")
        results, problems = run_check(load_releases(), args.jobs)
        if args.json:
            args.json.write_text(json.dumps(results, indent=1) + "\n")
        print(table([r for r in results if not r.get("missing")]))
        for problem in problems:
            print(f"❌ {problem}")
        if problems:
            print(f"❌ {len(problems)} problem(s) replaying {len(results)} release(s)")
            return 1
        print(f"✅ {len(results)} release(s) translated with the current translator")
        return 0
    tags = args.tags or release_tags()
    with concurrent.futures.ThreadPoolExecutor(max_workers=args.jobs) as pool:
        results = list(pool.map(replay, tags))
    if args.json:
        args.json.write_text(json.dumps(results, indent=1) + "\n")
    print(table(results))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
