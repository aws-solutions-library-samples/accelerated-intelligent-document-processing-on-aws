# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""The pseudo-locale build marker reaches a deployed UI build and never reaches git.

`src/ui/.pseudo-locale` is an empty file. When it exists, the UI build compiles the
`en-XA` pseudo-locale (accented, bracket-padded English with every placeholder intact)
into the UI and lists it in the language switcher, so a test stack shows which text
bypasses the message catalog and which layouts clip (decision D21 of #1770). The
deployed UI is built by CodeBuild from a source zip of `src/ui` that the publisher
creates, and the contents of that zip, not the build's environment, decide what is
built; the marker is the one way to flag such a build without a template change:

    touch src/ui/.pseudo-locale && idp-cli deploy --stack-name <name> --region <region> --from-code . --wait
    rm src/ui/.pseudo-locale

That only works while three facts about the tree hold, and each is pinned here against
the code that owns it rather than restated:

1. git never tracks the marker and ignores it (`src/ui/.gitignore`), so it cannot be
   committed by accident and turn a production build into a pseudo-locale build;
2. the publisher's UI zip carries it: `package_ui` drops `node_modules`, `build`,
   `.aws-sam` and every `.env*` file, and the marker is none of those;
3. the publisher's UI source hash changes when the marker appears, so the zip gets a
   new name and the stack's `CodeBuildRun` rebuilds the UI instead of keeping the
   previous build.

The zip and hash tests run the real publisher methods against a synthetic `src/ui`
under a temporary directory, with the S3 client replaced by one that reports the zip as
already uploaded, so nothing here touches the network or the real tree.
"""

from __future__ import annotations

import subprocess
import zipfile
from pathlib import Path

import gate_premises
import pytest

from idp_sdk._core.publish import IDPPublisher

pytestmark = pytest.mark.unit

REPO_ROOT = gate_premises.REPO_ROOT

#: The marker, repo-relative, as the author's command creates it.
MARKER = "src/ui/.pseudo-locale"

#: The ignore file that must own the rule: the UI's own, beside `/build` and `.env*`.
UI_GITIGNORE = "src/ui/.gitignore"


class _ZipAlreadyUploaded:
    """An S3 client whose `head_object` finds every key, so `package_ui` uploads nothing."""

    def head_object(self, **_kwargs):
        return {}


def _publisher(monkeypatch: pytest.MonkeyPatch) -> IDPPublisher:
    publisher = IDPPublisher()
    monkeypatch.setattr(publisher, "s3_client", _ZipAlreadyUploaded())
    monkeypatch.setattr(publisher, "bucket", "bucket-under-test")
    monkeypatch.setattr(publisher, "prefix_and_version", "idp/0.0.0")
    return publisher


def _synthetic_ui(root: Path) -> Path:
    """A `src/ui` with one file of each kind the publisher treats differently."""
    ui = root / "src" / "ui"
    (ui / "src" / "i18n").mkdir(parents=True)
    (ui / "package.json").write_text("{}\n", encoding="utf-8")
    (ui / "src" / "i18n" / "en.json").write_text("{}\n", encoding="utf-8")
    (ui / "node_modules").mkdir()
    (ui / "node_modules" / "dependency.js").write_text(
        "module.exports = 1;\n", encoding="utf-8"
    )
    (ui / "build").mkdir()
    (ui / "build" / "index.html").write_text("<html></html>\n", encoding="utf-8")
    (ui / ".aws-sam").mkdir()
    (ui / ".aws-sam" / "stale.zip").write_bytes(b"")
    (ui / ".env").write_text("VITE_X=1\n", encoding="utf-8")
    (ui / ".env.production").write_text("VITE_X=1\n", encoding="utf-8")
    return ui


def test_marker_is_not_tracked():
    assert not gate_premises.is_tracked(MARKER), (
        f"git tracks {MARKER}: every build of this tree would be a pseudo-locale build. "
        "Remove it with `git rm --cached` and keep it ignored."
    )


def test_marker_is_ignored_by_the_ui_gitignore():
    probed = subprocess.run(
        ["git", "-C", str(REPO_ROOT), "check-ignore", "-v", "--no-index", MARKER],
        capture_output=True,
        text=True,
    )
    assert probed.returncode == 0, (
        f"no ignore rule covers {MARKER}; add `.pseudo-locale` to {UI_GITIGNORE} so the marker "
        "can never be committed by accident"
    )
    rule = probed.stdout.split("\t", 1)[0]
    assert rule.startswith(f"{UI_GITIGNORE}:"), (
        f"{MARKER} is ignored by {rule!r}, not by a rule in {UI_GITIGNORE}; the UI's own ignore "
        "file owns it so the rule travels with the UI"
    )
    assert rule.endswith(":.pseudo-locale"), rule


def test_publish_zip_carries_the_marker_and_still_drops_env_files(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
):
    ui = _synthetic_ui(tmp_path)
    (ui / ".pseudo-locale").touch()
    monkeypatch.chdir(tmp_path)

    _publisher(monkeypatch).package_ui()
    assert "src/ui/.pseudo-locale is present" in capsys.readouterr().out, (
        "the publisher must say the marker is riding along: the recipe's `rm` is the only "
        "thing keeping en-XA out of the next stack deployed from this checkout"
    )

    zips = sorted((tmp_path / ".aws-sam").glob("src-*.zip"))
    assert len(zips) == 1, zips
    with zipfile.ZipFile(zips[0]) as archive:
        names = set(archive.namelist())
    assert ".pseudo-locale" in names, (
        f"package_ui left the marker out of {zips[0].name}: a CodeBuild build from this zip "
        "would be an unflagged, production-shaped UI"
    )
    assert {"package.json", "src/i18n/en.json"} <= names
    for dropped in (
        ".env",
        ".env.production",
        "node_modules/dependency.js",
        "build/index.html",
        ".aws-sam/stale.zip",
    ):
        assert dropped not in names, (
            f"package_ui now ships {dropped}; the marker test's premise about the zip changed"
        )


def test_marker_changes_the_ui_source_hash_so_the_stack_rebuilds(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    ui = _synthetic_ui(tmp_path)
    monkeypatch.chdir(tmp_path)
    publisher = _publisher(monkeypatch)

    unflagged = publisher.compute_ui_hash()
    assert unflagged == publisher.compute_ui_hash(), (
        "the UI hash is not stable across two reads"
    )

    # Per-build artifacts the hash leaves out must not change it; the marker must.
    for artifact in (".checksum", ".build_checksum", "npm-debug.log", "stale.pyc"):
        (ui / artifact).write_text("noise\n", encoding="utf-8")
        assert publisher.compute_ui_hash() == unflagged, (
            f"{artifact} now changes the UI hash"
        )

    (ui / ".pseudo-locale").touch()
    flagged = publisher.compute_ui_hash()
    assert flagged != unflagged, (
        "an empty .pseudo-locale does not change the UI source hash, so the zip keeps its name and "
        "CodeBuildRun would not rebuild the UI for a flagged deploy"
    )

    # The zip name is the first 16 hex characters of that hash, so the two deploys upload two zips.
    _, flagged_zip = publisher.ui_changed()
    assert Path(flagged_zip).name == f"src-{flagged[:16]}.zip"
    (ui / ".pseudo-locale").unlink()
    _, unflagged_zip = publisher.ui_changed()
    assert Path(unflagged_zip).name == f"src-{unflagged[:16]}.zip"
    assert flagged_zip != unflagged_zip
