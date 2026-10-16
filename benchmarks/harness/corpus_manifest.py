#!/usr/bin/env python3
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Read ``benchmarks/corpus/manifest.yaml`` as a statement about *this* checkout.

The manifest is tracked, and it carries one ``pdf``, ``truth`` and ``out`` path per
synthetic document. It used to carry **absolute** paths, because ``gen_corpus.py``
wrote whatever ``os.path.join(BENCH, ...)`` produced on the machine that last ran it,
and the committed copy therefore named a sibling checkout: 69 of 69 paths pointed at
another project's ``benchmarks/corpus/docs/`` (GitHub #1393).

That is the dangerous shape rather than the loud one. On a machine where the named
tree does not exist, a consumer fails immediately and nobody is misled. On a machine
where it *does* — several checkouts side by side is the ordinary layout here — the
documents and the ground truth are read from the other tree, the run succeeds, the
numbers are plausible, and nothing in the output records which tree they describe. It
is the same family as the editable-install defect (#1094): a measurement that is
self-consistent and about the wrong checkout. It also means a benchmark result is not
attributable to the commit it was run at, because the corpus it read was not versioned
with it.

So the rule this module enforces is structural, not a convention to remember: a path
in the manifest is **relative to the repository root** and is resolved against the
location of this file, so the manifest *cannot* name a tree other than the one holding
it. That is the property ``pyrightconfig.json``'s ``extraPaths`` relies on, for the
same stated reason — an absolute entry resolves perfectly while saying nothing about
this tree.

The corpus itself genuinely does live in this repository: ``benchmarks/.gitignore``
ignores ``corpus/docs/`` because ``gen_corpus.py`` regenerates the PDFs and their
``.truth.json`` deterministically from the tracked ``matrices/doc_matrix.yaml`` and the
tracked generators. Nothing is fetched from elsewhere and no corpus file is tracked, so
no environment-supplied root is needed and none is accepted: a path that is absolute,
that expands to an absolute path, or that climbs out of the repository is **refused**
rather than resolved. An unresolvable corpus must fail, not fall back — a silent
fallback is the whole defect above.

``python3 benchmarks/harness/corpus_manifest.py --check`` reports the state of the
committed manifest and exits non-zero on any violation.
"""

from __future__ import annotations

import argparse
import os
import re
import sys

# Derived from this file, never from the working directory: a relative anchor would
# reintroduce the defect one level up, since the path a run resolves against would
# then depend on where the run was started.
HARNESS = os.path.dirname(os.path.abspath(__file__))
BENCH = os.path.dirname(HARNESS)
REPO = os.path.dirname(BENCH)

MANIFEST_PATH = os.path.join(BENCH, "corpus", "manifest.yaml")

#: The record keys that hold a path. Named here so the writer, the resolver and the
#: test all agree; a new path-bearing key added to a record without being listed here
#: is what ``benchmarks/tests/test_corpus_manifest_paths.py`` fails on.
PATH_KEYS = ("pdf", "truth", "out")

# A Windows drive-relative or drive-absolute path (``C:\x``, ``c:/x``). ``os.path.isabs``
# answers False for these on POSIX, so they would otherwise be accepted and then joined
# onto the repository root, producing a path that cannot exist and a confusing error far
# from the cause.
_DRIVE = re.compile(r"^[A-Za-z]:")


class CorpusManifestError(RuntimeError):
    """A manifest path that cannot be read as a statement about this checkout."""


def _rejection(value: object) -> str | None:
    """Why ``value`` is not usable as a repository-relative path, or None if it is.

    Every branch is a way a path can name something other than this tree. They are
    checked explicitly rather than by one "looks absolute" test because the
    interesting cases do not share a spelling: ``~`` and ``$VAR`` are *relative*
    strings that become absolute later, and a Windows drive path is absolute on a
    platform ``os.path.isabs`` is not being asked about.
    """
    if value is None:
        return "is null (a document with no path cannot be read)"
    if not isinstance(value, str):
        return f"is {type(value).__name__}, not a string"
    if not value.strip():
        return "is empty"
    # Anchored at position 0 on purpose. A separator *inside* a path is ordinary and
    # must stay acceptable: ``corpus/docs/x.pdf`` is the correct shape, and a check
    # that rejected any path containing a separator would reject every real entry
    # while a check for ``"/" in value`` would flag them as absolute.
    if value.startswith("/"):
        return "is an absolute POSIX path"
    if value.startswith("\\"):
        return "is an absolute or UNC Windows path"
    if _DRIVE.match(value):
        return "names a Windows drive"
    if "\\" in value:
        return "contains a backslash (use '/' so the path reads the same on every OS)"
    # Absolute only *after* expansion. The string itself is relative, so every
    # is-absolute test passes it, and the path it eventually names depends on the
    # environment of whoever runs the harness — which is exactly the property the
    # manifest must not have.
    if value.startswith("~"):
        return "starts with '~', which expands to a path outside this checkout"
    if "$" in value or "%" in value:
        return "contains a variable reference, whose expansion is not this checkout"
    # Lexical containment, against the manifest's own text rather than against what
    # the filesystem happens to hold: ``..`` is the one way a syntactically relative
    # path still escapes, and normpath collapses it without touching the disk.
    joined = os.path.normpath(os.path.join(REPO, value))
    if joined != REPO and not joined.startswith(REPO + os.sep):
        return "escapes the repository root"
    return None


def validate(value: object, *, key: str = "path", doc_id: str = "?") -> str:
    """Return ``value`` unchanged, or raise naming the record and the key."""
    reason = _rejection(value)
    if reason is not None:
        shown = value if isinstance(value, str) else repr(value)
        raise CorpusManifestError(
            f"corpus manifest: doc {doc_id!r} key {key!r} {reason}: {shown!r}\n"
            "Manifest paths are relative to the repository root so that the manifest "
            "cannot name a checkout other than the one holding it (#1393). "
            "Re-run `python3 benchmarks/harness/gen_corpus.py` to rewrite it."
        )
    assert isinstance(value, str)  # nosec B101 - narrowing for the type checker
    return value


def relative_to_repo(path: str) -> str:
    """``/abs/path/inside/the/repo`` -> ``inside/the/repo``, with '/' separators.

    Raises rather than emitting ``../``: a corpus path outside this repository is the
    defect, so the writer must fail at the point it would record one.
    """
    absolute = os.path.normpath(
        path if os.path.isabs(path) else os.path.join(REPO, path)
    )
    if absolute != REPO and not absolute.startswith(REPO + os.sep):
        raise CorpusManifestError(
            f"corpus manifest: refusing to record a path outside this checkout: "
            f"{path!r}. The corpus lives under benchmarks/corpus/docs/ in this "
            "repository and is regenerated by gen_corpus.py (#1393)."
        )
    return os.path.relpath(absolute, REPO).replace(os.sep, "/")


def resolve(value: object, *, key: str = "path", doc_id: str = "?") -> str:
    """Validate ``value`` and resolve it against the repository root."""
    return os.path.normpath(os.path.join(REPO, validate(value, key=key, doc_id=doc_id)))


def relativize_record(record: dict) -> dict:
    """Rewrite every path key of ``record`` in place to a repository-relative path.

    In place so the key order survives: the manifest is a reviewed, committed file and
    a rebuild that reordered its keys would make every regeneration a large diff.
    """
    for key in PATH_KEYS:
        if key in record and record[key] is not None:
            record[key] = relative_to_repo(record[key])
    return record


def _read_records(path: str, *, allow_empty: bool) -> list[dict]:
    """The manifest's records, structure checked, **paths not yet judged**.

    Separate from :func:`load_raw` so that :func:`check` can report *every* bad path
    rather than the first: validation that raises is right for a caller about to use
    the data and wrong for a caller whose job is to list the problems.
    """
    import yaml

    if not os.path.exists(path):
        raise CorpusManifestError(f"corpus manifest: no such file: {path}")
    with open(path) as handle:
        loaded = yaml.safe_load(handle) or {}
    if not isinstance(loaded, dict):
        raise CorpusManifestError(
            f"corpus manifest: {path} is {type(loaded).__name__}, expected a mapping"
        )
    docs = loaded.get("docs")
    if docs is None:
        docs = []
    if not isinstance(docs, list):
        raise CorpusManifestError(
            f"corpus manifest: {path} 'docs' is {type(docs).__name__}, expected a list"
        )
    # An empty list is a failure for a consumer rather than an empty corpus. A check
    # that iterates it reports nothing wrong, and a run that iterates it measures
    # nothing and still exits 0 — the vacuous-pass shape. Only the writer, which is
    # about to populate it, passes allow_empty.
    if not docs and not allow_empty:
        raise CorpusManifestError(
            f"corpus manifest: {path} declares no documents. Run "
            "`python3 benchmarks/harness/gen_corpus.py` to build the corpus; an empty "
            "manifest would otherwise let a check or a run pass over nothing."
        )
    records: list[dict] = []
    for index, record in enumerate(docs):
        if not isinstance(record, dict):
            raise CorpusManifestError(
                f"corpus manifest: docs[{index}] is {type(record).__name__}, "
                "expected a mapping"
            )
        records.append(record)
    return records


def load_raw(path: str = MANIFEST_PATH, *, allow_empty: bool = False) -> list[dict]:
    """The manifest's records with their paths validated but not resolved.

    For a caller that is going to write the file back — ``gen_corpus.py`` merges the
    documents it just built into the records it did not — so the paths it re-dumps are
    the relative ones it read.
    """
    records = _read_records(path, allow_empty=allow_empty)
    for index, record in enumerate(records):
        doc_id = str(record.get("id", f"docs[{index}]"))
        for key in PATH_KEYS:
            if key in record:
                validate(record[key], key=key, doc_id=doc_id)
    return records


def load(path: str = MANIFEST_PATH, *, require_exists: bool = True) -> list[dict]:
    """The manifest's records with every path resolved against this checkout.

    ``require_exists`` is the no-fallback half: a path that validates but names a file
    this tree does not hold raises, because ``corpus/docs/`` is gitignored and a fresh
    clone has none of it. The remedy is to generate the corpus, not to read somebody
    else's.
    """
    records = []
    for record in load_raw(path):
        doc_id = str(record.get("id", "?"))
        resolved = dict(record)
        for key in PATH_KEYS:
            if key in record and record[key] is not None:
                resolved[key] = resolve(record[key], key=key, doc_id=doc_id)
                if require_exists and not os.path.exists(resolved[key]):
                    raise CorpusManifestError(
                        f"corpus manifest: doc {doc_id!r} key {key!r} resolves to "
                        f"{resolved[key]}, which does not exist. benchmarks/corpus/docs/ "
                        "is gitignored and regenerable: run "
                        "`python3 benchmarks/harness/gen_corpus.py`."
                    )
        records.append(resolved)
    return records


def check(path: str = MANIFEST_PATH) -> list[str]:
    """Every problem in ``path``, rather than only the first — for the CLI.

    A caller fixing a regenerated manifest needs the whole list; reporting one bad
    path per run turns the fix into a loop and hides how widespread it is. (The
    committed file held 69 of them.)
    """
    problems: list[str] = []
    try:
        records = _read_records(path, allow_empty=False)
    except CorpusManifestError as exc:
        return [str(exc)]
    for index, record in enumerate(records):
        doc_id = str(record.get("id", f"docs[{index}]"))
        for key in PATH_KEYS:
            if key not in record:
                continue
            reason = _rejection(record[key])
            if reason is not None:
                problems.append(f"{doc_id}: {key} {reason}: {record[key]!r}")
    return problems


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--manifest", default=MANIFEST_PATH)
    parser.add_argument(
        "--check",
        action="store_true",
        help="report any path that is not relative to the repository root",
    )
    parser.add_argument(
        "--require-exists",
        action="store_true",
        help="also require every resolved path to exist in this checkout",
    )
    args = parser.parse_args(argv)
    problems = check(args.manifest)
    if problems:
        print(f"FAIL: {len(problems)} manifest path(s) do not describe this checkout:")
        for problem in problems:
            print(f"  {problem}")
        return 1
    if args.require_exists:
        try:
            load(args.manifest, require_exists=True)
        except CorpusManifestError as exc:
            print(f"FAIL: {exc}")
            return 1
    records = load_raw(args.manifest)
    paths = sum(1 for r in records for k in PATH_KEYS if k in r)
    print(
        f"OK: {len(records)} documents, {paths} paths, all relative to the "
        f"repository root ({REPO})"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
