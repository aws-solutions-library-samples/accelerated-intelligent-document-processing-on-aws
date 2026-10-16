# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""A tracked benchmark manifest must describe the checkout that holds it (#1393).

The committed ``benchmarks/corpus/manifest.yaml`` carried 69 of 69 paths as absolute
paths into a *sibling* checkout, because ``gen_corpus.py`` recorded whatever
``os.path.join`` produced on the machine that last ran it. Nothing read the committed
file and compared it with this tree, so review was the only control and review saw a
plausible path.

The failure that matters is the one that **works**. Where several checkouts sit side by
side — the ordinary layout here — a consumer resolves those paths, reads the documents
and the ground truth out of the other tree, and produces a grid that is internally
consistent, plausible, and about code that was never run. A machine without that
sibling directory fails immediately and misleads nobody. So the property asserted here
is not "the paths resolve" but "the paths *cannot* name another tree": they are
relative, and they are resolved against the manifest's own location.

Two shapes of this check, because they fail on different mistakes:

* over the **committed** manifest, which is the artifact the defect was in;
* over :mod:`corpus_manifest`'s refusals, which is what stops the next one. Those are
  unit-level on purpose — the interesting attacks do not share a spelling with a plain
  leading ``/``. ``~/x`` and ``$HOME/x`` are *relative* strings that become absolute
  only on expansion, ``C:\\x`` is absolute on a platform ``os.path.isabs`` is not being
  asked about, ``a/../../x`` escapes without ever being absolute, and a check written as
  ``"/" in value`` rejects every correct entry while calling it absolute.

And one vacuity guard in each direction: discovery must find the manifest and some
paths in it, and an empty manifest must be a failure rather than a silent pass over
nothing.
"""

from __future__ import annotations

import os
import subprocess  # nosec B404 - reads the tracked file list from git, no shell
import sys

import pytest

yaml = pytest.importorskip("yaml")

HERE = os.path.dirname(os.path.abspath(__file__))
BENCH = os.path.dirname(HERE)
REPO = os.path.dirname(BENCH)
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(BENCH, "harness"))

from harness_import import harness_module  # noqa: E402 - needs the bootstrap above

# Via ``harness_module`` rather than ``importorskip``: a harness module that will not
# import is the finding, and a skip here would read as a run (#1079).
corpus_manifest = harness_module("corpus_manifest")

# Paths that are absolute, or become absolute, or climb out of the tree. Each is a way
# the manifest could go back to describing a different checkout.
ILL_FORMED_PATHS = [
    pytest.param(
        "/home/someone/projects/other/benchmarks/corpus/docs/x.pdf", id="posix-abs"
    ),
    pytest.param("/benchmarks/corpus/docs/x.pdf", id="posix-abs-short"),
    pytest.param("C:/other/benchmarks/corpus/docs/x.pdf", id="windows-drive-fwd"),
    pytest.param("C:\\other\\benchmarks\\corpus\\docs\\x.pdf", id="windows-drive-back"),
    pytest.param("c:/other/x.pdf", id="windows-drive-lowercase"),
    pytest.param("\\\\server\\share\\x.pdf", id="windows-unc"),
    pytest.param("benchmarks\\corpus\\docs\\x.pdf", id="backslash-separators"),
    pytest.param("~/projects/other/benchmarks/corpus/docs/x.pdf", id="tilde"),
    pytest.param("~someone/benchmarks/corpus/docs/x.pdf", id="tilde-user"),
    pytest.param("$HOME/benchmarks/corpus/docs/x.pdf", id="dollar-var"),
    pytest.param("${HOME}/benchmarks/corpus/docs/x.pdf", id="braced-var"),
    pytest.param("benchmarks/corpus/docs/$DOC.pdf", id="var-in-tail"),
    pytest.param("%USERPROFILE%/x.pdf", id="windows-var"),
    pytest.param("../idp-other/benchmarks/corpus/docs/x.pdf", id="dotdot"),
    pytest.param("benchmarks/../../other/corpus/docs/x.pdf", id="dotdot-midway"),
    pytest.param("", id="empty"),
    pytest.param("   ", id="whitespace"),
    pytest.param(None, id="null"),
    pytest.param(17, id="not-a-string"),
]

# Correct shapes. These exist so that tightening the rule cannot be done by rejecting
# everything: a separator inside a path is ordinary, and a single-segment name is too.
WELL_FORMED_PATHS = [
    pytest.param("benchmarks/corpus/docs/x.pdf", id="the-real-shape"),
    pytest.param("benchmarks/corpus/docs/x.pdf.truth.json", id="truth-json"),
    pytest.param("benchmarks/corpus/docs/nested/deeper/x.pdf", id="deeper"),
    pytest.param("x.pdf", id="no-separator"),
    pytest.param("benchmarks/corpus/docs/./x.pdf", id="dot-segment"),
    pytest.param(
        "benchmarks/corpus/../corpus/docs/x.pdf", id="dotdot-that-stays-inside"
    ),
]


def tracked(pattern: str) -> list[str]:
    """Tracked paths matching ``pattern``, read from git rather than from a walk.

    A walk would also see the gitignored ``corpus/docs/`` and anything a local run
    left behind; the subject here is what is *committed*, since that is what a fresh
    clone and a reviewer get.
    """
    proc = subprocess.run(  # nosec B603,B607 - fixed argv, no shell, repo-local
        ["git", "ls-files", "-z", "--", pattern],
        cwd=REPO,
        capture_output=True,
        text=True,
    )
    if proc.returncode != 0:
        raise RuntimeError(
            "git ls-files failed, so this suite cannot tell which manifests are "
            f"tracked: {proc.stderr.strip()}. Skipping would report a pass over "
            "nothing."
        )
    return [p for p in proc.stdout.split("\0") if p]


def _walk_paths(node, trail=""):
    """Every ``(key-trail, value)`` under a path-bearing key, anywhere in ``node``.

    Recursive rather than "records under ``docs:``" so that a path-bearing key added
    at another nesting level, or a second manifest with a different top-level shape, is
    covered without this file being edited.
    """
    if isinstance(node, dict):
        for key, value in node.items():
            where = f"{trail}.{key}" if trail else str(key)
            if key in corpus_manifest.PATH_KEYS and not isinstance(value, (dict, list)):
                yield where, value
            else:
                yield from _walk_paths(value, where)
    elif isinstance(node, list):
        for index, value in enumerate(node):
            yield from _walk_paths(value, f"{trail}[{index}]")


def manifest_yaml_files() -> list[str]:
    """Tracked YAML under ``benchmarks/`` that carries at least one path key.

    Discovered by **content**, so a manifest added at a new path, or renamed, is
    covered. The name-based cross-check below is what fails if content discovery stops
    finding a file that is called a manifest.
    """
    found = []
    for rel in tracked("benchmarks/**"):
        if not rel.endswith((".yaml", ".yml")):
            continue
        with open(os.path.join(REPO, rel)) as handle:
            try:
                loaded = yaml.safe_load(handle)
            except yaml.YAMLError:  # a malformed YAML is another suite's finding
                continue
        if any(True for _ in _walk_paths(loaded)):
            found.append(rel)
    return sorted(found)


MANIFESTS = manifest_yaml_files()


def test_discovery_finds_the_corpus_manifest_and_some_paths_in_it():
    """Vacuity guard. Everything below iterates what this returns."""
    assert "benchmarks/corpus/manifest.yaml" in MANIFESTS, (
        "content discovery did not find benchmarks/corpus/manifest.yaml, so every "
        f"path assertion in this file passes over nothing. Found: {MANIFESTS}"
    )
    with open(corpus_manifest.MANIFEST_PATH) as handle:
        loaded = yaml.safe_load(handle)
    paths = list(_walk_paths(loaded))
    assert len(paths) >= 3, (
        f"only {len(paths)} path(s) found in the committed manifest; the shape this "
        "suite reads has changed and the checks below are near-vacuous"
    )


def test_every_tracked_file_named_manifest_is_discovered_by_content():
    """A manifest must not escape the check by being shaped differently.

    Discovery is by content; this is the cross-check in the other direction, so that a
    tracked ``manifest.yaml`` which carries no recognised path key fails here rather
    than being silently uncovered.
    """
    by_name = [
        p for p in tracked("benchmarks/**") if os.path.basename(p) == "manifest.yaml"
    ]
    assert by_name, "no tracked benchmarks manifest.yaml at all — discovery is broken"
    missed = sorted(set(by_name) - set(MANIFESTS))
    assert not missed, (
        f"tracked manifest(s) {missed} carry no key in "
        f"{corpus_manifest.PATH_KEYS}, so no path in them is checked. Either add the "
        "key to corpus_manifest.PATH_KEYS or say here why the file has no paths."
    )


@pytest.mark.parametrize("rel", MANIFESTS)
def test_no_tracked_manifest_carries_an_absolute_path(rel):
    """The defect itself: a committed path that names a tree other than this one."""
    with open(os.path.join(REPO, rel)) as handle:
        loaded = yaml.safe_load(handle)
    bad = []
    for where, value in _walk_paths(loaded):
        try:
            corpus_manifest.validate(value, key=where)
        except corpus_manifest.CorpusManifestError as exc:
            bad.append(f"{where}: {exc}".splitlines()[0])
    assert not bad, (
        f"{rel} carries {len(bad)} path(s) that do not describe this checkout:\n"
        + "\n".join(bad[:10])
    )


@pytest.mark.parametrize("rel", MANIFESTS)
def test_every_tracked_manifest_path_resolves_inside_the_repository(rel):
    """Relative is necessary but not sufficient: ``..`` is relative and still leaves."""
    with open(os.path.join(REPO, rel)) as handle:
        loaded = yaml.safe_load(handle)
    checked = 0
    for where, value in _walk_paths(loaded):
        resolved = corpus_manifest.resolve(value, key=where)
        assert resolved.startswith(REPO + os.sep), (
            f"{rel}: {where} resolves to {resolved}, outside {REPO}"
        )
        checked += 1
    assert checked, f"{rel} was discovered as holding paths but yielded none"


def test_the_committed_manifest_has_no_unrecognised_path_bearing_key():
    """A new key holding a path would be unchecked, which is how this recurs.

    ``PATH_KEYS`` is what every check above iterates, so a record that grows (say)
    ``classes:`` pointing at a file would be outside all of them. Rather than keeping a
    second list, this looks for *values that are shaped like paths* under keys that are
    not registered.
    """
    with open(corpus_manifest.MANIFEST_PATH) as handle:
        records = (yaml.safe_load(handle) or {}).get("docs", [])
    suspicious = []
    for record in records:
        for key, value in record.items():
            if key in corpus_manifest.PATH_KEYS or not isinstance(value, str):
                continue
            if "/" in value or value.endswith((".pdf", ".json", ".yaml", ".yml")):
                suspicious.append(f"{record.get('id')}: {key}={value!r}")
    assert not suspicious, (
        "manifest record key(s) hold something path-shaped but are not in "
        "corpus_manifest.PATH_KEYS, so nothing checks them:\n"
        + "\n".join(suspicious[:10])
    )


@pytest.mark.parametrize("value", ILL_FORMED_PATHS)
def test_validate_refuses_a_path_that_can_name_another_checkout(value):
    with pytest.raises(corpus_manifest.CorpusManifestError):
        corpus_manifest.validate(value, key="pdf", doc_id="probe")


@pytest.mark.parametrize("value", WELL_FORMED_PATHS)
def test_validate_accepts_an_ordinary_repository_relative_path(value):
    """Tightening the rule must not be achievable by refusing everything."""
    assert corpus_manifest.validate(value, key="pdf", doc_id="probe") == value


@pytest.mark.parametrize("value", WELL_FORMED_PATHS)
def test_an_accepted_path_resolves_under_the_repository_root(value):
    resolved = corpus_manifest.resolve(value, key="pdf", doc_id="probe")
    assert resolved.startswith(REPO + os.sep)


def test_the_refusal_message_names_the_record_and_the_key():
    """The remedy is a regeneration, so the message has to say which entry is wrong."""
    with pytest.raises(corpus_manifest.CorpusManifestError) as caught:
        corpus_manifest.validate("/elsewhere/x.pdf", key="truth", doc_id="tiny_form")
    text = str(caught.value)
    assert "tiny_form" in text and "truth" in text and "gen_corpus.py" in text


def test_an_empty_manifest_is_a_failure_not_a_pass(tmp_path):
    """The vacuous-pass shape: zero documents, zero violations, exit 0.

    A consumer iterating an empty ``docs:`` measures nothing and reports success, which
    is the same class of defect as reading the wrong tree — a self-consistent answer
    about no data. Only the writer, which is about to populate it, may pass
    ``allow_empty``.
    """
    path = tmp_path / "manifest.yaml"
    path.write_text("docs: []\n")
    with pytest.raises(corpus_manifest.CorpusManifestError, match="no documents"):
        corpus_manifest.load_raw(str(path))
    assert corpus_manifest.load_raw(str(path), allow_empty=True) == []

    path.write_text("{}\n")
    with pytest.raises(corpus_manifest.CorpusManifestError, match="no documents"):
        corpus_manifest.load_raw(str(path))

    path.write_text("")
    with pytest.raises(corpus_manifest.CorpusManifestError, match="no documents"):
        corpus_manifest.load_raw(str(path))


def test_the_check_itself_does_not_pass_over_an_empty_manifest(tmp_path, capsys):
    """ "Zero paths, zero violations, exit 0" is how a check stops being one.

    Checked separately from :func:`load_raw` because the two have different reasons to
    tolerate an empty list and the CLI is the half an operator reads: a gate that
    reports OK over nothing is worse than no gate, since it is evidence.
    """
    path = tmp_path / "manifest.yaml"
    path.write_text("docs: []\n")
    assert corpus_manifest.check(str(path)), (
        "check() found nothing wrong with an empty manifest"
    )
    assert corpus_manifest.main(["--manifest", str(path), "--check"]) == 1
    assert "no documents" in capsys.readouterr().out


def test_a_missing_manifest_is_a_failure(tmp_path):
    with pytest.raises(corpus_manifest.CorpusManifestError, match="no such file"):
        corpus_manifest.load_raw(str(tmp_path / "absent.yaml"))


def test_load_refuses_a_path_this_checkout_does_not_hold(tmp_path):
    """No fallback. ``corpus/docs/`` is gitignored, so absence is the normal state of a
    fresh clone and the remedy is to generate the corpus — never to read another tree's.
    """
    path = tmp_path / "manifest.yaml"
    path.write_text(
        "docs:\n- id: ghost\n  pdf: benchmarks/corpus/docs/does-not-exist.pdf\n"
    )
    with pytest.raises(corpus_manifest.CorpusManifestError, match="does not exist"):
        corpus_manifest.load(str(path))
    # ...and the same manifest is readable when existence is not being asserted, so the
    # test above is about existence rather than about the path being malformed.
    records = corpus_manifest.load(str(path), require_exists=False)
    assert records[0]["pdf"].startswith(REPO + os.sep)


def test_load_raw_refuses_an_absolute_path_so_a_partial_rebuild_cannot_carry_one(
    tmp_path,
):
    """``gen_corpus.py --only <id>`` merges the records it did not rebuild, verbatim.

    Reading that merge through ``load_raw`` is what stops a stale absolute entry from
    surviving a partial regeneration and being committed again.
    """
    path = tmp_path / "manifest.yaml"
    path.write_text(
        "docs:\n- id: stale\n  pdf: /elsewhere/benchmarks/corpus/docs/x.pdf\n"
    )
    for kwargs in ({}, {"allow_empty": True}):
        with pytest.raises(corpus_manifest.CorpusManifestError, match="absolute"):
            corpus_manifest.load_raw(str(path), **kwargs)


def test_the_writer_records_a_relative_path_for_every_path_key():
    """``out`` reaches the record from the generator, not from the two keys set beside
    it, so the writer relativizes the whole record."""
    absolute = os.path.join(BENCH, "corpus", "docs", "probe.pdf")
    record = {
        "id": "probe",
        "pdf": absolute,
        "truth": absolute + ".truth.json",
        "out": absolute,
        "rows": 5,
    }
    order_before = list(record)
    corpus_manifest.relativize_record(record)
    assert record["pdf"] == "benchmarks/corpus/docs/probe.pdf"
    assert record["truth"] == "benchmarks/corpus/docs/probe.pdf.truth.json"
    assert record["out"] == "benchmarks/corpus/docs/probe.pdf"
    assert record["rows"] == 5
    # Key order is part of the committed file's reviewability: a rebuild that reordered
    # keys would make every regeneration an unreadable diff.
    assert list(record) == order_before


def test_the_writer_refuses_to_record_a_path_outside_the_checkout():
    with pytest.raises(
        corpus_manifest.CorpusManifestError, match="outside this checkout"
    ):
        corpus_manifest.relative_to_repo("/elsewhere/benchmarks/corpus/docs/x.pdf")


def test_the_writer_leaves_an_already_relative_path_alone():
    record = {"pdf": "benchmarks/corpus/docs/x.pdf", "truth": None}
    corpus_manifest.relativize_record(record)
    assert record == {"pdf": "benchmarks/corpus/docs/x.pdf", "truth": None}


def test_gen_corpus_writes_through_the_relativizer():
    """A reintroduction guard on the writer.

    ``gen_corpus.py`` is the only thing that writes the tracked manifest, and the
    defect was in what it wrote. The round-trip itself needs the PDF generators, so
    what is pinned here is that the two calls that make the file relative — and the
    validated merge read — are still in the writer rather than having been dropped by
    a later edit.
    """
    source = open(os.path.join(BENCH, "harness", "gen_corpus.py")).read()
    assert "relativize_record" in source, (
        "gen_corpus.py no longer relativizes the records it writes, so the tracked "
        "manifest will go back to naming the machine that built it (#1393)"
    )
    assert "load_raw" in source, (
        "gen_corpus.py no longer reads the existing manifest through the validator, "
        "so a partial rebuild can carry a stale absolute path through unchecked"
    )
    assert "yaml.safe_load(open(man_path))" not in source, (
        "gen_corpus.py reads the manifest directly again, bypassing validation"
    )


STUB_GENERATOR = """
def build(out, **params):
    with open(out, "w") as handle:
        handle.write("pdf-bytes")
    with open(out + ".truth.json", "w") as handle:
        handle.write("{}")
    # ``out`` goes back absolute, exactly as the real generators return it. This is the
    # key that reaches the record through ``**info`` rather than being set beside it.
    return {"out": out, "rows": params.get("rows", 0)}
"""


@pytest.fixture()
def sandboxed_writer(tmp_path, monkeypatch):
    """Run ``gen_corpus.main()`` against a throwaway tree instead of the real corpus.

    The module's anchors are module-level constants derived from ``__file__``, so
    relocating them is what lets the writer be exercised for real — no stub stands in
    for the relativizer — without generating PDFs into the repository or rewriting the
    tracked manifest. ``corpus_manifest.REPO`` moves with them, which is also a
    statement about the design: the root is one constant, and everything resolves
    against it.
    """
    gen_corpus = harness_module("gen_corpus")
    root = tmp_path / "repo"
    docs = root / "benchmarks" / "corpus" / "docs"
    gendir = root / "benchmarks" / "corpus" / "generators"
    docs.mkdir(parents=True)  # pre-existing, as it is on any machine that has run this
    gendir.mkdir(parents=True)
    (gendir / "stub.py").write_text(STUB_GENERATOR)
    matrix = root / "benchmarks" / "matrices" / "doc_matrix.yaml"
    matrix.parent.mkdir(parents=True)
    matrix.write_text(
        "synthetic:\n"
        "- id: alpha\n  gen: stub\n  rows: 5\n  tag: t=a\n  series: null\n"
        "- id: beta\n  gen: stub\n  rows: 7\n  tag: t=b\n  series: scaling\n"
    )
    manifest = root / "benchmarks" / "corpus" / "manifest.yaml"
    monkeypatch.setattr(corpus_manifest, "REPO", str(root))
    monkeypatch.setattr(corpus_manifest, "MANIFEST_PATH", str(manifest))
    monkeypatch.setattr(gen_corpus, "DOCS", str(docs))
    monkeypatch.setattr(gen_corpus, "GENDIR", str(gendir))
    monkeypatch.setattr(gen_corpus, "MATRIX", str(matrix))
    return gen_corpus, manifest


def _run_writer(gen_corpus, monkeypatch, *args):
    monkeypatch.setattr(sys, "argv", ["gen_corpus.py", *args])
    gen_corpus.main()


def test_the_writer_produces_a_manifest_whose_paths_are_all_relative(
    sandboxed_writer, monkeypatch
):
    """End to end over the writer: what lands on disk is what gets committed."""
    gen_corpus, manifest = sandboxed_writer
    _run_writer(gen_corpus, monkeypatch)
    text = manifest.read_text()
    assert "/" in text, "sanity: the manifest should hold paths at all"
    records = (yaml.safe_load(text) or {}).get("docs", [])
    assert [r["id"] for r in records] == ["alpha", "beta"]
    for record in records:
        for key in corpus_manifest.PATH_KEYS:
            assert key in record, f"{record['id']} lost its {key} path"
            assert not record[key].startswith("/"), (
                f"{record['id']}.{key} is absolute: {record[key]!r}"
            )
            assert record[key].startswith("benchmarks/corpus/docs/"), record[key]
    # The per-document parameters must still reach the record, or the writer is
    # "relative" because it dropped everything.
    assert [r["rows"] for r in records] == [5, 7]
    assert [r["tag"] for r in records] == ["t=a", "t=b"]
    assert [r["series"] for r in records] == [None, "scaling"]
    # Key order, in the file rather than in the record: the manifest is reviewed as a
    # diff, and a rebuild that sorted or reordered keys would rewrite all 23 entries
    # and bury the one line that changed.
    for record in records:
        assert list(record)[:5] == ["id", "gen", "pdf", "truth", "out"], list(record)


def test_a_partial_rebuild_keeps_the_documents_it_did_not_rebuild(
    sandboxed_writer, monkeypatch
):
    """``--only`` and ``--series`` merge; the merge is the path a stale entry survives."""
    gen_corpus, manifest = sandboxed_writer
    _run_writer(gen_corpus, monkeypatch, "--only", "alpha")
    first = {r["id"]: r for r in yaml.safe_load(manifest.read_text())["docs"]}
    assert list(first) == ["alpha"], "the --only filter selected the wrong documents"
    _run_writer(gen_corpus, monkeypatch, "--series", "scaling")
    both = {r["id"]: r for r in yaml.safe_load(manifest.read_text())["docs"]}
    assert list(both) == ["alpha", "beta"], (
        "a partial rebuild clobbered or failed to merge the other documents"
    )
    assert both["alpha"] == first["alpha"], "the carried-over record was rewritten"
    for record in both.values():
        for key in corpus_manifest.PATH_KEYS:
            assert not record[key].startswith("/")


def test_the_writer_refuses_an_existing_manifest_that_holds_an_absolute_path(
    sandboxed_writer, monkeypatch
):
    """The records it carries over are re-dumped verbatim, so it must not read one.

    This is the shape that let the defect persist: a rebuild of one document would
    otherwise re-commit the other 22 entries exactly as it found them.
    """
    gen_corpus, manifest = sandboxed_writer
    manifest.write_text("docs:\n- id: stale\n  pdf: /elsewhere/corpus/docs/stale.pdf\n")
    with pytest.raises(corpus_manifest.CorpusManifestError, match="absolute"):
        _run_writer(gen_corpus, monkeypatch, "--only", "alpha")


def test_the_writer_accepts_a_manifest_that_has_no_documents_yet(
    sandboxed_writer, monkeypatch
):
    """``allow_empty`` is the writer's alone: it is the thing that populates one.

    Asserted because the refusal above and this acceptance are one decision, and
    getting it the other way round makes the first run on a fresh clone fail.
    """
    gen_corpus, manifest = sandboxed_writer
    manifest.write_text("docs: []\n")
    _run_writer(gen_corpus, monkeypatch, "--only", "beta")
    records = yaml.safe_load(manifest.read_text())["docs"]
    assert [r["id"] for r in records] == ["beta"]


def test_the_check_cli_passes_on_the_committed_manifest(capsys):
    assert corpus_manifest.main([]) == 0
    assert "OK:" in capsys.readouterr().out


def test_the_repository_root_is_anchored_on_this_file_not_on_the_working_directory():
    """Resolved against ``__file__``, so the answer does not depend on where you ran it.

    This has to be checked from a *different* working directory to mean anything:
    ``os.path.abspath("benchmarks/harness")`` is identical to the correct value when the
    command happens to be run from the repository root, which is where both CIs run
    pytest — so an in-process assertion passes on the broken form and the defect reaches
    only the developer who ran it from somewhere else. That is #1079 exactly, in the
    module whose whole purpose is to stop a path from naming the wrong tree.
    """
    harness_dir = os.path.join(BENCH, "harness")
    env = dict(os.environ, PYTHONPATH=harness_dir)
    proc = subprocess.run(  # nosec B603,B607 - fixed argv, no shell
        [
            sys.executable,
            "-c",
            "import corpus_manifest; print(corpus_manifest.REPO)",
        ],
        cwd=os.path.dirname(REPO),
        capture_output=True,
        text=True,
        env=env,
    )
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.strip() == REPO, (
        "corpus_manifest.REPO changes with the working directory: it answered "
        f"{proc.stdout.strip()!r} when run from {os.path.dirname(REPO)!r}, but "
        f"{REPO!r} is the checkout holding it"
    )


def test_the_cli_require_exists_flag_fails_on_a_corpus_this_checkout_lacks(tmp_path):
    """The no-fallback half, through the entry point an operator actually types."""
    path = tmp_path / "manifest.yaml"
    path.write_text(
        "docs:\n- id: ghost\n  pdf: benchmarks/corpus/docs/does-not-exist.pdf\n"
    )
    assert corpus_manifest.main(["--manifest", str(path), "--check"]) == 0
    assert corpus_manifest.main(["--manifest", str(path), "--require-exists"]) == 1, (
        "--require-exists accepted a path this checkout does not hold"
    )


def test_the_check_cli_fails_and_names_every_bad_path(tmp_path, capsys):
    path = tmp_path / "manifest.yaml"
    path.write_text(
        "docs:\n"
        "- id: a\n  pdf: /elsewhere/a.pdf\n  truth: benchmarks/corpus/docs/a.json\n"
        "- id: b\n  pdf: ~/b.pdf\n"
    )
    assert corpus_manifest.main(["--manifest", str(path), "--check"]) == 1
    out = capsys.readouterr().out
    assert "FAIL" in out and "a:" in out and "b:" in out
    # The one good path must not be reported, or the output stops being readable.
    assert "a.json" not in out
