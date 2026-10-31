# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""A merge request's StoryInputs v1: what the first-time-user stories select from.

The record is the MR UX test's own (``mr_inputs.MrInputs``) plus what the story
selector reads: ``CHANGELOG.md`` at the merge base and at the head, the ids of
the head's ``docs`` and ``src/ui`` trees, and the documentation sections the MR
edits. :func:`collect` builds it from git and :meth:`StoryInputs.from_json`
reads it back, refusing any other shape, because the place that collects it is
not always the place that runs the stories: the CI precheck writes
``inputs.json`` and the run reads it, and the Docker runner collects on the host
and hands the file to a container.

Everything comes from git objects, never from the checkout's files. The MR head
and the merge base are ``mr_inputs.collect``'s. ``git ls-tree`` of each commit
names the blobs and trees by id, and each blob is then read by its id with
``git cat-file blob`` through ``ui_change.git``, so a path an MR commits, a
symlink included, is only ever a key into git's own listing and never picks a
file. Only a regular file counts as a CHANGELOG, and only a mode-100644 blob at a
path ``docs_index.DOC_PATH_RE`` matches counts as a page, as in
``docs_index.snapshot_from_git``.

The CHANGELOG texts, the title and the diff are MR-authored. The orchestrator
matches stories against the CHANGELOG entries the MR adds, its reply quotes their
leads only once ``story_section`` has sanitised them, and none of it reaches a
prompt. What is read is bounded before it is read, by the sizes ``ls-tree``
reports: a CHANGELOG to ``ui_change.MAX_OUTPUT_BYTES``; a page to the
``docs_index.PAGE_MAX_BYTES`` the index itself reads, so a page larger than that
at either commit adds no section; the changed pages of one commit together to the
``docs_index.SNAPSHOT_MAX_BYTES`` a docs snapshot takes; and at most
:data:`MAX_EDITED_PAGES` pages. A section id longer than :data:`SECTION_ID_CHARS`
is one no story can be asked about and is left out, and an MR that edits more
than :data:`MAX_EDITED_SECTIONS` sections is refused. The record as
``inputs.json`` holds it is at most :data:`INPUTS_MAX_BYTES`: the orchestrator
writes no larger file and reads none back, and the Docker runner holds the same
bound, so a record that one side takes is one the other side takes too.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ux_runner import docs_index, mr_inputs, ui_change

INPUTS_SCHEMA = "ux-test-stories/inputs"
INPUTS_VERSION = 1
#: The most bytes an ``inputs.json`` holds, whichever side writes or reads it.
INPUTS_MAX_BYTES = 32 * 1024 * 1024
CHANGELOG_PATH = "CHANGELOG.md"
DOCS_PATH = "docs"
UI_PATH = ui_change.UI_PREFIX.rstrip("/")
#: The longest section id the record carries: the most of an address the docs
#: server reads, so a longer id is one no story can be asked about.
SECTION_ID_CHARS = docs_index.REF_CHARS
#: The most changed documentation pages one MR's sections are computed over.
MAX_EDITED_PAGES = docs_index.SNAPSHOT_MAX_FILES
#: The most edited sections the record carries; renaming every published page
#: edits about twice the sections the site holds.
MAX_EDITED_SECTIONS = 10_000

_SHA_RE = re.compile(r"[0-9a-f]{40}")
_SIZE_RE = re.compile(r"[0-9]{1,18}")
_FILE_MODES = frozenset({"100644", "100755"})
_PAGE_MODES = frozenset({"100644"})
_RECORD_KEYS = frozenset(
    {"schema", "version", "mr", "changelog", "trees", "docs_sections_edited"}
)


@dataclass(frozen=True)
class StoryInputs:
    """A merge request's StoryInputs v1, as ``inputs.json`` holds it.

    ``mr`` is the MR UX test's own record. The two CHANGELOG texts are the file at
    the merge base and at the head, which select stories and never reach a
    prompt; the trees are the head's ``docs`` and ``src/ui`` tree ids.
    """

    mr: mr_inputs.MrInputs
    changelog_base: str
    changelog_head: str
    docs_tree: str | None
    src_ui_tree: str | None
    docs_sections_edited: tuple[str, ...]

    def to_json(self) -> dict[str, Any]:
        """The ``inputs.json`` record."""
        return {
            "schema": INPUTS_SCHEMA,
            "version": INPUTS_VERSION,
            "mr": self.mr.to_json(),
            "changelog": {"base": self.changelog_base, "head": self.changelog_head},
            "trees": {"docs": self.docs_tree, "src_ui": self.src_ui_tree},
            "docs_sections_edited": list(self.docs_sections_edited),
        }

    @classmethod
    def from_json(cls, data: object) -> StoryInputs:
        """The record :meth:`to_json` writes, refusing any other shape."""
        if not isinstance(data, Mapping):
            raise mr_inputs.InputsError("inputs.json is not a JSON object")
        if set(data) != _RECORD_KEYS:
            raise mr_inputs.InputsError("inputs.json is not StoryInputs v1")
        version = data.get("version")
        if data.get("schema") != INPUTS_SCHEMA or version != INPUTS_VERSION:
            raise mr_inputs.InputsError("inputs.json is not StoryInputs v1")
        mr = data.get("mr")
        if not isinstance(mr, Mapping):
            raise mr_inputs.InputsError("inputs.json has no MR record")
        changelog = data.get("changelog")
        texts = (
            [changelog.get("base"), changelog.get("head")]
            if isinstance(changelog, Mapping) and set(changelog) == {"base", "head"}
            else []
        )
        if len(texts) != 2 or not all(
            isinstance(text, str) and len(text) <= ui_change.MAX_OUTPUT_BYTES
            for text in texts
        ):
            raise mr_inputs.InputsError("inputs.json's changelog is not two texts")
        trees = data.get("trees")
        if not isinstance(trees, Mapping) or set(trees) != {"docs", "src_ui"}:
            raise mr_inputs.InputsError("inputs.json's trees are not docs and src_ui")
        for name in ("docs", "src_ui"):
            value = trees.get(name)
            if value is not None and not (
                isinstance(value, str) and _SHA_RE.fullmatch(value)
            ):
                raise mr_inputs.InputsError(f"inputs.json's {name} tree is not a SHA")
        edited = data.get("docs_sections_edited")
        if (
            not isinstance(edited, list)
            or len(edited) > MAX_EDITED_SECTIONS
            or not all(
                isinstance(item, str) and 0 < len(item) <= SECTION_ID_CHARS
                for item in edited
            )
        ):
            raise mr_inputs.InputsError("inputs.json's docs sections are not ids")
        return cls(
            mr=mr_inputs.MrInputs.from_json(mr),
            changelog_base=str(texts[0]),
            changelog_head=str(texts[1]),
            docs_tree=trees.get("docs"),
            src_ui_tree=trees.get("src_ui"),
            docs_sections_edited=tuple(edited),
        )


@dataclass(frozen=True)
class _Entry:
    """One ``git ls-tree -l`` entry: its mode, type, object id and blob size."""

    mode: str
    kind: str
    oid: str
    size: int | None


def collect(
    repo_dir: Path,
    iid: int,
    target_branch: str,
    *,
    remote: str = "origin",
    fetch: bool = True,
    git: Callable[..., str] = ui_change.git,
    git_prefix: Callable[..., tuple[str, int]] = ui_change.git_prefix,
) -> StoryInputs:
    """Collect the MR's StoryInputs v1 from git.

    ``mr_inputs.collect`` fetches the MR head and the target branch (unless
    ``fetch`` is false) and finds the merge base; then come the CHANGELOG at the
    base and at the head, the head's ``docs`` and ``src/ui`` tree ids (None for a
    path the head does not hold as a tree), and the sections of every changed
    page that differ between the two (``docs_index.changed_sections``). Whatever
    git cannot answer is an ``mr_inputs.InputsError``.
    """
    mr = mr_inputs.collect(
        repo_dir,
        iid,
        target_branch,
        remote=remote,
        fetch=fetch,
        git=git,
        git_prefix=git_prefix,
    )
    try:
        head = _entries(
            repo_dir, mr.head_sha, (CHANGELOG_PATH, DOCS_PATH, UI_PATH), git
        )
        base = _entries(repo_dir, mr.base_sha, (CHANGELOG_PATH,), git)
        changelog_base = _changelog(repo_dir, mr.base_sha, base, git)
        changelog_head = _changelog(repo_dir, mr.head_sha, head, git)
        edited = _edited_sections(repo_dir, mr, git)
    except ui_change.UiChangeError as error:
        raise mr_inputs.InputsError(str(error)) from error
    return StoryInputs(
        mr=mr,
        changelog_base=changelog_base,
        changelog_head=changelog_head,
        docs_tree=_tree_id(head.get(DOCS_PATH)),
        src_ui_tree=_tree_id(head.get(UI_PATH)),
        docs_sections_edited=edited,
    )


def _entries(
    repo_dir: Path,
    commit: str,
    paths: Sequence[str],
    git: Callable[..., str],
    *,
    recursive: bool = False,
) -> dict[str, _Entry]:
    """``git ls-tree -l`` of ``paths`` at ``commit``, by path; an absent path is absent."""
    flags = ("-r", "-z", "-l") if recursive else ("-z", "-l")
    listing = git(repo_dir, "ls-tree", *flags, commit, "--", *paths)
    found: dict[str, _Entry] = {}
    for record in listing.split("\0"):
        if not record:
            continue
        meta, tab, path = record.partition("\t")
        fields = meta.split()
        if not tab or not path or len(fields) != 4:
            raise ui_change.UiChangeError(
                f"git ls-tree printed an entry this does not read: {record[:120]!r}"
            )
        mode, kind, oid, size = fields
        measured = int(size) if _SIZE_RE.fullmatch(size) else None
        found[path] = _Entry(mode, kind, oid, measured)
    return found


def _tree_id(entry: _Entry | None) -> str | None:
    if entry is None or entry.kind != "tree" or not _SHA_RE.fullmatch(entry.oid):
        return None
    return entry.oid


def _blob(entry: _Entry | None, modes: frozenset[str]) -> tuple[str, int] | None:
    """The blob id and size of ``entry`` when it is a blob of one of ``modes``."""
    if (
        entry is None
        or entry.kind != "blob"
        or entry.mode not in modes
        or entry.size is None
        or not _SHA_RE.fullmatch(entry.oid)
    ):
        return None
    return entry.oid, entry.size


def _changelog(
    repo_dir: Path, commit: str, entries: Mapping[str, _Entry], git: Callable[..., str]
) -> str:
    """``CHANGELOG.md`` at ``commit``: ``""`` when it holds no such regular file."""
    blob = _blob(entries.get(CHANGELOG_PATH), _FILE_MODES)
    if blob is None:
        return ""
    oid, size = blob
    if size > ui_change.MAX_OUTPUT_BYTES:
        raise mr_inputs.InputsError(
            f"{CHANGELOG_PATH} at {commit[:8]} is {size} bytes, more than the "
            f"{ui_change.MAX_OUTPUT_BYTES} it is read to"
        )
    return git(repo_dir, "cat-file", "blob", oid)


def _changed_pages(changed: Sequence[ui_change.ChangedFile]) -> list[str]:
    """Every documentation page the diff names, on either side of a rename or copy."""
    named = {
        path
        for entry in changed
        for path in (entry.path, entry.old_path)
        if path is not None
        and docs_index.DOC_PATH_RE.fullmatch(path)
        and all(segment not in ("", ".", "..") for segment in path.split("/"))
    }
    return sorted(named)


def _page_blobs(
    repo_dir: Path, commit: str, pages: Sequence[str], git: Callable[..., str]
) -> dict[str, tuple[str, int]]:
    """Each of ``pages`` that ``commit`` holds as a page: its blob id and size."""
    listing = _entries(repo_dir, commit, (DOCS_PATH,), git, recursive=True)
    found: dict[str, tuple[str, int]] = {}
    for page in pages:
        blob = _blob(listing.get(page), _PAGE_MODES)
        if blob is not None:
            found[page] = blob
    return found


def _read_pages(
    repo_dir: Path,
    commit: str,
    blobs: Mapping[str, tuple[str, int]],
    git: Callable[..., str],
) -> dict[str, str]:
    """The text of each page in ``blobs``, which must fit in a docs snapshot together."""
    total = sum(size for _, size in blobs.values())
    if total > docs_index.SNAPSHOT_MAX_BYTES:
        raise mr_inputs.InputsError(
            f"the documentation pages this MR changes are {total} bytes at "
            f"{commit[:8]}, more than the {docs_index.SNAPSHOT_MAX_BYTES} a docs "
            "snapshot takes"
        )
    return {
        page: git(repo_dir, "cat-file", "blob", oid) for page, (oid, _) in blobs.items()
    }


def _edited_sections(
    repo_dir: Path, mr: mr_inputs.MrInputs, git: Callable[..., str]
) -> tuple[str, ...]:
    """The sorted union of each changed page's sections that differ, base to head."""
    pages = _changed_pages(mr.changed)
    if not pages:
        return ()
    if len(pages) > MAX_EDITED_PAGES:
        raise mr_inputs.InputsError(
            f"this MR changes {len(pages)} documentation pages, more than the "
            f"{MAX_EDITED_PAGES} its sections are computed over"
        )
    before = _page_blobs(repo_dir, mr.base_sha, pages, git)
    after = _page_blobs(repo_dir, mr.head_sha, pages, git)
    readable = [
        page
        for page in pages
        if all(
            blobs[page][1] <= docs_index.PAGE_MAX_BYTES
            for blobs in (before, after)
            if page in blobs
        )
    ]
    base_texts = _read_pages(
        repo_dir, mr.base_sha, {p: before[p] for p in readable if p in before}, git
    )
    head_texts = _read_pages(
        repo_dir, mr.head_sha, {p: after[p] for p in readable if p in after}, git
    )
    edited: set[str] = set()
    for path in readable:
        page = path[len(DOCS_PATH) + 1 :]
        sections = docs_index.changed_sections(
            page, base_texts.get(path), head_texts.get(path)
        )
        edited.update(ident for ident in sections if len(ident) <= SECTION_ID_CHARS)
        if len(edited) > MAX_EDITED_SECTIONS:
            raise mr_inputs.InputsError(
                f"this MR edits more than the {MAX_EDITED_SECTIONS} documentation "
                "sections the record carries"
            )
    return tuple(sorted(edited))
