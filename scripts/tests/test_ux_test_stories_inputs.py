# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Tests for scripts/ux_runner/story_inputs.py, the record an MR's stories select from.

What matters is that the record describes the MR head against its merge base and
was read from git objects alone: the CHANGELOG at both commits, the head's tree
ids, and the sections of the changed pages that differ, with a path an MR commits
used only as a key into git's own listing, never to open a file. The collect tests
run real git in throwaway repositories under ``tmp_path`` (an empty hooks
directory, commit signing off, git's global and system configuration switched
off, and the repository as its own ``origin``), so ``ls-tree``'s format and
pathspecs are the real ones. The bounds and the odd entries are driven through an
in-memory git that answers the same commands, which also records which blobs were
read. A record arriving from elsewhere is checked to be refused in every shape
:meth:`StoryInputs.to_json` would not write, and the orchestrator is checked to
read what the collector writes.
"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

SCRIPTS = Path(__file__).resolve().parents[1]
for _entry in (SCRIPTS, SCRIPTS / "sdlc"):
    if str(_entry) not in sys.path:
        sys.path.insert(0, str(_entry))

import ai_ux_test_stories as runner  # noqa: E402
from ux_runner import docs_index, mr_inputs, story_inputs, ui_change  # noqa: E402

IID = 812
FAKE_BASE = "fedcba9876543210" * 2 + "fedcba98"
FAKE_HEAD = "0123456789abcdef" * 2 + "01234567"
WEB_UI = (
    "# Web UI\n\n## Upload documents\n\nUpload a document from the Upload page.\n\n"
    "## Review documents\n\nOpen a document to review it.\n"
)
OLD_PAGE = "# Old page\n\n## Section one\n\nText one.\n"
ADDED_PAGE = "# Added page\n\n## Brand new\n\nNew text.\n"
BASE_CHANGELOG = "## [Unreleased]\n\n### Added\n\n- **An entry already there.** Text.\n"
HEAD_CHANGELOG = (
    BASE_CHANGELOG + "- **Configuration Profiles can be assigned by S3 prefix.** See "
    "[it](docs/web-ui.md).\n"
)
GIT_SUBCOMMANDS = frozenset(
    {"fetch", "rev-parse", "merge-base", "diff", "log", "ls-tree", "cat-file"}
)


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    ).stdout


def _write(repo: Path, files: dict[str, str | None]) -> None:
    for rel, content in files.items():
        path = repo / rel
        if content is None:
            _git(repo, "rm", "--quiet", rel)
            continue
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
        _git(repo, "add", rel)


def _commit(repo: Path, files: dict[str, str | None], message: str) -> str:
    _write(repo, files)
    _git(repo, "commit", "--quiet", "-m", message)
    return _git(repo, "rev-parse", "HEAD").strip()


def _repo(root: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", "/dev/null")
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    hooks = root / "empty-hooks"
    hooks.mkdir()
    repo = root / "repo"
    repo.mkdir()
    _git(repo, "init", "--quiet", "-b", "develop")
    _git(repo, "config", "core.hooksPath", str(hooks))
    _git(repo, "config", "commit.gpgsign", "false")
    _git(repo, "config", "core.symlinks", "true")
    _git(repo, "config", "user.name", "Jane Doe")
    _git(repo, "config", "user.email", "jane@example.invalid")
    _git(repo, "remote", "add", "origin", str(repo))
    return repo


def _mr(repo: Path, head: str) -> None:
    _git(repo, "update-ref", f"refs/merge-requests/{IID}/head", head)


def _recording(seen: list[tuple[str, ...]]) -> Callable[..., str]:
    def git(repo_dir: Path, *args: str, timeout: float = 120.0) -> str:
        seen.append(args)
        return ui_change.git(repo_dir, *args, timeout=timeout)

    return git


@pytest.fixture
def mr_repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Path, str, str]:
    """A develop branch, an MR branched off it, and develop moving on afterwards."""
    repo = _repo(tmp_path, monkeypatch)
    base = _commit(
        repo,
        {
            "CHANGELOG.md": BASE_CHANGELOG,
            "docs/web-ui.md": WEB_UI,
            "docs/old-page.md": OLD_PAGE,
            "docs/keep.md": "# Keep\n\nUnchanged.\n",
            "src/ui/src/App.tsx": "v1\n",
        },
        "base",
    )
    _git(repo, "checkout", "--quiet", "-b", "feature")
    _git(repo, "mv", "docs/old-page.md", "docs/new-page.md")
    head = _commit(
        repo,
        {
            "CHANGELOG.md": HEAD_CHANGELOG,
            "docs/web-ui.md": WEB_UI.replace(
                "Open a document to review it.", "Open a document, then each field."
            ),
            "docs/guide/added.md": ADDED_PAGE,
            "src/ui/src/App.tsx": "v2\n",
        },
        "Route by prefix",
    )
    _git(repo, "checkout", "--quiet", "develop")
    _commit(
        repo,
        {
            "CHANGELOG.md": BASE_CHANGELOG + "- **Landed later on develop.** Text.\n",
            "docs/keep.md": "# Keep\n\nChanged on develop.\n",
        },
        "later on develop",
    )
    _mr(repo, head)
    return repo, base, head


# --- collect, against real git --------------------------------------------------


@pytest.mark.unit
def test_collect_reads_the_changelog_trees_and_edited_sections_at_the_merge_base(
    mr_repo: tuple[Path, str, str],
) -> None:
    repo, base, head = mr_repo
    seen: list[tuple[str, ...]] = []
    inputs = story_inputs.collect(repo, IID, "develop", git=_recording(seen))
    assert (inputs.mr.head_sha, inputs.mr.base_sha) == (head, base)
    assert inputs.mr.title == "Route by prefix" and inputs.mr.title_from_git
    assert inputs.changelog_base == BASE_CHANGELOG
    assert inputs.changelog_head == HEAD_CHANGELOG
    assert inputs.docs_tree == _git(repo, "rev-parse", f"{head}:docs").strip()
    assert inputs.src_ui_tree == _git(repo, "rev-parse", f"{head}:src/ui").strip()
    assert inputs.docs_sections_edited == (
        "guide/added.md#added-page",
        "guide/added.md#brand-new",
        "new-page.md#old-page",
        "new-page.md#section-one",
        "old-page.md#old-page",
        "old-page.md#section-one",
        "web-ui.md#review-documents",
    )
    assert {args[0] for args in seen} <= GIT_SUBCOMMANDS
    reads = [args for args in seen if args[0] == "cat-file"]
    assert reads and all(
        len(args) == 3 and args[1] == "blob" and len(args[2]) == 40 for args in reads
    )
    assert not any(
        ":" in arg for args in seen if args[0] != "fetch" for arg in args[1:]
    )


@pytest.mark.unit
def test_the_checkouts_own_files_are_never_read(
    mr_repo: tuple[Path, str, str],
) -> None:
    repo, _, _ = mr_repo
    (repo / "CHANGELOG.md").write_text("- **Only on disk.** Text.\n", "utf-8")
    (repo / "docs" / "web-ui.md").write_text("# Only on disk\n", "utf-8")
    inputs = story_inputs.collect(repo, IID, "develop")
    assert inputs.changelog_base == BASE_CHANGELOG
    assert "Only on disk" not in inputs.changelog_head
    assert not any("only-on-disk" in ident for ident in inputs.docs_sections_edited)


@pytest.mark.unit
def test_the_record_is_what_the_orchestrator_reads_back(
    mr_repo: tuple[Path, str, str],
) -> None:
    repo, _, _ = mr_repo
    record = runner.collect_story(repo, IID, "develop", remote="origin")
    assert json.loads(json.dumps(record)) == record
    read = runner.StoryInputs.from_json(record)
    assert read == story_inputs.collect(repo, IID, "develop", fetch=False)
    assert story_inputs.StoryInputs.from_json(read.to_json()) == read


@pytest.mark.unit
def test_a_symlink_an_mr_commits_is_never_followed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = _repo(tmp_path, monkeypatch)
    outside = tmp_path / "outside.md"
    outside.write_text("# Outside the repository\n\n## Secret\n\nNot a page.\n")
    _commit(
        repo,
        {"CHANGELOG.md": BASE_CHANGELOG, "docs/web-ui.md": WEB_UI},
        "base",
    )
    _git(repo, "checkout", "--quiet", "-b", "feature")
    _git(repo, "rm", "--quiet", "CHANGELOG.md")
    os.symlink(str(outside), repo / "CHANGELOG.md")
    os.symlink(str(outside), repo / "docs" / "linked.md")
    _git(repo, "add", "CHANGELOG.md", "docs/linked.md")
    _git(repo, "commit", "--quiet", "-m", "links")
    _mr(repo, _git(repo, "rev-parse", "HEAD").strip())
    inputs = story_inputs.collect(repo, IID, "develop")
    assert inputs.changelog_base == BASE_CHANGELOG
    assert inputs.changelog_head == ""
    assert inputs.docs_sections_edited == ()


@pytest.mark.unit
def test_what_a_commit_does_not_hold_is_empty_or_none(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = _repo(tmp_path, monkeypatch)
    _commit(repo, {"README.md": "no changelog yet\n"}, "base")
    _git(repo, "checkout", "--quiet", "-b", "feature")
    head = _commit(repo, {"CHANGELOG.md": HEAD_CHANGELOG}, "changelog")
    _mr(repo, head)
    inputs = story_inputs.collect(repo, IID, "develop")
    assert inputs.changelog_base == ""
    assert inputs.changelog_head == HEAD_CHANGELOG
    assert (inputs.docs_tree, inputs.src_ui_tree) == (None, None)
    assert inputs.docs_sections_edited == ()


@pytest.mark.unit
def test_a_head_that_does_not_exist_is_an_inputs_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = _repo(tmp_path, monkeypatch)
    _commit(repo, {"CHANGELOG.md": BASE_CHANGELOG}, "base")
    with pytest.raises(mr_inputs.InputsError):
        story_inputs.collect(repo, IID, "develop")


# --- bounds and odd entries, against an in-memory git ---------------------------


class FakeGit:
    """git as ``mr_inputs`` and ``story_inputs`` run it, from two in-memory commits."""

    def __init__(self) -> None:
        self.trees: dict[str, dict[str, tuple[str, str, str, int | None]]] = {
            FAKE_BASE: {},
            FAKE_HEAD: {},
        }
        self.blobs: dict[str, str] = {}
        self.changed: list[tuple[str, ...]] = []
        self.calls: list[tuple[str, ...]] = []
        self.listing: str | None = None

    def put(
        self,
        commit: str,
        path: str,
        text: str,
        *,
        mode: str = "100644",
        kind: str = "blob",
        size: int | None = None,
    ) -> str:
        oid = hashlib.sha256(f"{path}\0{text}".encode()).hexdigest()[:40]
        self.blobs[oid] = text
        measured = len(text.encode()) if size is None else size
        self.trees[commit][path] = (mode, kind, oid, measured)
        return oid

    def read(self) -> list[str]:
        return [args[2] for args in self.calls if args[0] == "cat-file"]

    def _ls_tree(self, args: tuple[str, ...]) -> str:
        flags = {arg for arg in args[1:] if arg.startswith("-") and arg != "--"}
        commit = next(arg for arg in args[1:] if not arg.startswith("-"))
        paths = args[args.index("--") + 1 :]
        if self.listing is not None:
            return self.listing
        lines = []
        for path, (mode, kind, oid, size) in sorted(self.trees[commit].items()):
            for wanted in paths:
                if path == wanted or ("-r" in flags and path.startswith(wanted + "/")):
                    shown = "-" if size is None else str(size)
                    lines.append(f"{mode} {kind} {oid} {shown:>7}\t{path}\0")
        if "-r" not in flags:
            for wanted in paths:
                if any(p.startswith(wanted + "/") for p in self.trees[commit]):
                    oid = hashlib.sha256(f"tree {wanted}".encode()).hexdigest()[:40]
                    lines.append(f"040000 tree {oid}       -\t{wanted}\0")
        return "".join(lines)

    def __call__(self, repo_dir: Path, *args: str, timeout: float = 120.0) -> str:
        self.calls.append(args)
        if args[0] == "rev-parse":
            return FAKE_HEAD + "\n"
        if args[0] == "merge-base":
            return FAKE_BASE + "\n"
        if args[:2] == ("diff", "--name-status"):
            return "".join(
                "\0".join(entry) + "\0"
                for entry in self.changed
                if entry[0] and len(entry) in (2, 3)
            )
        if args[0] == "log":
            return "Route by prefix\0Jane Doe\n"
        if args[0] == "ls-tree":
            return self._ls_tree(args)
        if args[:2] == ("cat-file", "blob"):
            return self.blobs[args[2]]
        raise AssertionError(f"FakeGit does not run git {' '.join(args)}")

    def prefix(self, repo_dir: Path, *args: str, keep: int, timeout: float = 120.0):
        return "", 0

    def collect(self) -> story_inputs.StoryInputs:
        return story_inputs.collect(
            Path("/nonexistent"),
            IID,
            "develop",
            fetch=False,
            git=self,
            git_prefix=self.prefix,
        )


@pytest.fixture
def fake() -> FakeGit:
    git = FakeGit()
    for commit in (FAKE_BASE, FAKE_HEAD):
        git.put(commit, "CHANGELOG.md", BASE_CHANGELOG)
        git.put(commit, "src/ui/src/App.tsx", "v1\n")
    return git


@pytest.mark.unit
def test_a_changelog_past_the_read_bound_is_refused_unread(fake: FakeGit) -> None:
    big = fake.put(FAKE_HEAD, "CHANGELOG.md", "x", size=ui_change.MAX_OUTPUT_BYTES + 1)
    with pytest.raises(mr_inputs.InputsError, match="CHANGELOG.md at 01234567"):
        fake.collect()
    assert big not in fake.read()


@pytest.mark.unit
@pytest.mark.parametrize(
    ("mode", "kind"),
    [("120000", "blob"), ("160000", "commit"), ("040000", "tree")],
    ids=["symlink", "submodule", "directory"],
)
def test_a_changelog_that_is_not_a_regular_file_is_read_as_empty(
    fake: FakeGit, mode: str, kind: str
) -> None:
    fake.put(FAKE_HEAD, "CHANGELOG.md", "../../etc/passwd", mode=mode, kind=kind)
    assert fake.collect().changelog_head == ""
    assert len(fake.read()) == 1


@pytest.mark.unit
def test_an_executable_changelog_is_still_the_changelog(fake: FakeGit) -> None:
    fake.put(FAKE_HEAD, "CHANGELOG.md", HEAD_CHANGELOG, mode="100755")
    assert fake.collect().changelog_head == HEAD_CHANGELOG


@pytest.mark.unit
def test_only_a_regular_page_at_a_page_path_adds_sections(fake: FakeGit) -> None:
    fake.put(FAKE_HEAD, "docs/page.md", ADDED_PAGE)
    script = fake.put(FAKE_HEAD, "docs/run.md", ADDED_PAGE, mode="100755")
    link = fake.put(FAKE_HEAD, "docs/link.md", "../CHANGELOG.md", mode="120000")
    fake.put(FAKE_HEAD, "docs/notes.txt", ADDED_PAGE)
    fake.put(FAKE_HEAD, "docs/space name.md", ADDED_PAGE)
    fake.changed = [
        ("A", "docs/page.md"),
        ("A", "docs/run.md"),
        ("A", "docs/link.md"),
        ("A", "docs/notes.txt"),
        ("A", "docs/space name.md"),
        ("A", "docs/../escape.md"),
        ("M", "src/ui/docs/page.md"),
    ]
    inputs = fake.collect()
    assert inputs.docs_sections_edited == ("page.md#added-page", "page.md#brand-new")
    assert script not in fake.read() and link not in fake.read()


@pytest.mark.unit
def test_a_page_too_large_for_the_index_at_either_commit_adds_no_section(
    fake: FakeGit,
) -> None:
    fake.put(FAKE_BASE, "docs/big.md", OLD_PAGE)
    big = fake.put(
        FAKE_HEAD, "docs/big.md", OLD_PAGE + "x", size=docs_index.PAGE_MAX_BYTES + 1
    )
    fake.put(FAKE_HEAD, "docs/page.md", ADDED_PAGE)
    fake.changed = [("M", "docs/big.md"), ("A", "docs/page.md")]
    inputs = fake.collect()
    assert inputs.docs_sections_edited == ("page.md#added-page", "page.md#brand-new")
    assert big not in fake.read()


@pytest.mark.unit
def test_changed_pages_past_a_snapshot_together_are_refused_unread(
    fake: FakeGit,
) -> None:
    pages = docs_index.SNAPSHOT_MAX_BYTES // docs_index.PAGE_MAX_BYTES + 1
    oids = [
        fake.put(FAKE_HEAD, f"docs/p{n}.md", ADDED_PAGE, size=docs_index.PAGE_MAX_BYTES)
        for n in range(pages)
    ]
    fake.changed = [("A", f"docs/p{n}.md") for n in range(pages)]
    with pytest.raises(mr_inputs.InputsError, match="a docs snapshot takes"):
        fake.collect()
    assert not set(oids) & set(fake.read())


@pytest.mark.unit
def test_more_changed_pages_than_a_snapshot_holds_are_refused(
    fake: FakeGit, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(story_inputs, "MAX_EDITED_PAGES", 2)
    fake.changed = [("A", f"docs/p{n}.md") for n in range(3)]
    with pytest.raises(mr_inputs.InputsError, match="3 documentation pages"):
        fake.collect()
    assert not any(args[0] == "ls-tree" and "-r" in args for args in fake.calls)


@pytest.mark.unit
def test_an_mr_editing_more_sections_than_the_record_carries_is_refused(
    fake: FakeGit, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(story_inputs, "MAX_EDITED_SECTIONS", 3)
    for n in range(2):
        fake.put(FAKE_HEAD, f"docs/p{n}.md", ADDED_PAGE)
    fake.changed = [("A", "docs/p0.md"), ("A", "docs/p1.md")]
    with pytest.raises(mr_inputs.InputsError, match="more than the 3"):
        fake.collect()


@pytest.mark.unit
def test_a_section_id_no_story_can_name_is_left_out(fake: FakeGit) -> None:
    heading = "w" * story_inputs.SECTION_ID_CHARS
    fake.put(FAKE_HEAD, "docs/long.md", f"# {heading}\n\ntext\n\n## Short\n\nmore\n")
    fake.changed = [("A", "docs/long.md")]
    assert fake.collect().docs_sections_edited == ("long.md#short",)


@pytest.mark.unit
def test_a_rename_edits_both_pages_and_a_copy_only_the_new_one(fake: FakeGit) -> None:
    fake.put(FAKE_BASE, "docs/a.md", OLD_PAGE)
    fake.put(FAKE_HEAD, "docs/b.md", OLD_PAGE)
    fake.put(FAKE_BASE, "docs/c.md", WEB_UI)
    fake.put(FAKE_HEAD, "docs/c.md", WEB_UI)
    fake.put(FAKE_HEAD, "docs/d.md", WEB_UI)
    fake.changed = [
        ("R100", "docs/a.md", "docs/b.md"),
        ("C100", "docs/c.md", "docs/d.md"),
    ]
    edited = fake.collect().docs_sections_edited
    assert {ident.split("#")[0] for ident in edited} == {"a.md", "b.md", "d.md"}


@pytest.mark.unit
def test_a_listing_git_prints_in_another_shape_is_an_inputs_error(
    fake: FakeGit,
) -> None:
    fake.listing = "100644 blob abc\0"
    with pytest.raises(mr_inputs.InputsError, match="ls-tree printed an entry"):
        fake.collect()


@pytest.mark.unit
def test_git_failing_after_the_mr_is_read_is_an_inputs_error(fake: FakeGit) -> None:
    real = fake.__call__

    def failing(repo_dir: Path, *args: str, timeout: float = 120.0) -> str:
        if args[0] == "cat-file":
            raise ui_change.UiChangeError("git cat-file failed: fatal: bad object")
        return real(repo_dir, *args, timeout=timeout)

    with pytest.raises(mr_inputs.InputsError, match="bad object"):
        story_inputs.collect(
            Path("/nonexistent"),
            IID,
            "develop",
            fetch=False,
            git=failing,
            git_prefix=fake.prefix,
        )


@pytest.mark.unit
def test_a_tree_id_is_kept_only_for_a_tree(fake: FakeGit) -> None:
    fake.put(FAKE_HEAD, "docs", "not a directory")
    inputs = fake.collect()
    assert inputs.docs_tree is None
    assert inputs.src_ui_tree is not None and len(inputs.src_ui_tree) == 40


# --- the record, read back ------------------------------------------------------


def _record() -> story_inputs.StoryInputs:
    return story_inputs.StoryInputs(
        mr=mr_inputs.MrInputs(
            iid=IID,
            title="Route by prefix",
            author="jdoe",
            web_url="",
            source_branch="",
            target_branch="develop",
            head_sha=FAKE_HEAD,
            base_sha=FAKE_BASE,
            changed=(ui_change.ChangedFile("M", "docs/web-ui.md"),),
            ui_diff="",
            ui_diff_truncated=False,
            ui_diff_bytes=0,
            title_from_git=True,
        ),
        changelog_base=BASE_CHANGELOG,
        changelog_head=HEAD_CHANGELOG,
        docs_tree="ab" * 20,
        src_ui_tree=None,
        docs_sections_edited=("web-ui.md#review-documents",),
    )


@pytest.mark.unit
def test_the_record_round_trips_and_names_its_schema() -> None:
    data = _record().to_json()
    assert list(data) == [
        "schema",
        "version",
        "mr",
        "changelog",
        "trees",
        "docs_sections_edited",
    ]
    assert (data["schema"], data["version"]) == ("ux-test-stories/inputs", 1)
    assert story_inputs.StoryInputs.from_json(json.loads(json.dumps(data))) == (
        _record()
    )


@pytest.mark.unit
@pytest.mark.parametrize(
    "mutate",
    [
        lambda d: d.clear(),
        lambda d: d.update(version=2),
        lambda d: d.pop("trees"),
        lambda d: d.update(mr="!812"),
        lambda d: d.update(changelog={"base": "", "head": 1}),
        lambda d: d.update(changelog={"base": "", "head": "", "extra": ""}),
        lambda d: d["changelog"].update(head="x" * (ui_change.MAX_OUTPUT_BYTES + 1)),
        lambda d: d.update(trees={"docs": None}),
        lambda d: d["trees"].update(src_ui="AB" * 20),
        lambda d: d.update(docs_sections_edited="web-ui.md"),
        lambda d: d.update(docs_sections_edited=["x" * 301]),
        lambda d: d.update(
            docs_sections_edited=["a.md"] * (story_inputs.MAX_EDITED_SECTIONS + 1)
        ),
        lambda d: d["mr"].update(head_sha="short"),
    ],
    ids=[
        "empty",
        "version",
        "missing-key",
        "mr-not-an-object",
        "changelog-not-text",
        "changelog-extra-key",
        "changelog-too-long",
        "trees-keys",
        "tree-not-lowercase-hex",
        "sections-not-a-list",
        "section-too-long",
        "too-many-sections",
        "mr-record",
    ],
)
def test_any_other_shape_is_refused(mutate: Callable[[dict[str, Any]], Any]) -> None:
    data = _record().to_json()
    mutate(data)
    with pytest.raises(mr_inputs.InputsError):
        story_inputs.StoryInputs.from_json(data)


@pytest.mark.unit
def test_what_is_not_a_json_object_is_refused() -> None:
    for value in (None, [], "inputs"):
        with pytest.raises(mr_inputs.InputsError, match="not a JSON object"):
            story_inputs.StoryInputs.from_json(value)
