# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Offline tests for ``scripts/terraform_check.py``, the ``make terraform-check`` gate.

Nothing here downloads anything: the fetcher is replaced, and ``init``/``validate``
are played by a stub executable. What is pinned is the part a green gate cannot
show on its own: that an archive whose hash disagrees with the committed lock is
refused, that a validation error fails the gate while warnings do not, and that
the pins in the lock still fall inside the ranges the generated module declares.
"""

from __future__ import annotations

import hashlib
import importlib.util
import io
import json
import re
import sys
import zipfile
from pathlib import Path

import pytest
from packaging.specifiers import SpecifierSet
from packaging.version import Version

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT = REPO_ROOT / "scripts" / "terraform_check.py"
STATIC_FILES = REPO_ROOT / "terraform" / "translator" / "idp_tf" / "static_files.py"
MAKEFILE = REPO_ROOT / "Makefile"


def _load():
    spec = importlib.util.spec_from_file_location("_terraform_check_under_test", SCRIPT)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    # Registered before it runs: its dataclasses resolve their own module through
    # sys.modules while the class body executes.
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


tc = _load()


def _zip_with(name: str, payload: bytes) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as zf:
        zf.writestr(name, payload)
    return buffer.getvalue()


def _fake_release(monkeypatch, tmp_path, *, published=None, served=None, locked=None):
    """Point the installer at an in-memory release of ``terraform@9.9.9``."""
    archive = _zip_with("terraform", b"#!/bin/sh\necho fake\n")
    digest = hashlib.sha256(archive).hexdigest()
    plat = "linux_amd64"
    lock = tmp_path / "lock.json"
    lock.write_text(
        json.dumps({"binaries": {"terraform@9.9.9": {plat: locked or digest}}})
    )
    monkeypatch.setattr(tc, "LOCK_FILE", lock)
    sums = f"{published or digest}  terraform_9.9.9_{plat}.zip\n".encode()
    fetched: list[str] = []

    def fetch(url: str) -> bytes:
        fetched.append(url)
        if url.endswith("_SHA256SUMS"):
            return sums
        return served if served is not None else archive

    monkeypatch.setattr(tc, "_fetch", fetch)
    return tc.Binary("terraform", "9.9.9"), plat, fetched


@pytest.mark.unit
def test_binary_specs_parse_and_bad_ones_are_refused() -> None:
    assert tc.Binary.parse("tofu@1.7.10") == tc.Binary("tofu", "1.7.10")
    for bad in ("terraform", "terraform@", "pulumi@3.0.0", "@1.0"):
        with pytest.raises(tc.GateError):
            tc.Binary.parse(bad)


@pytest.mark.unit
def test_install_verifies_and_then_reuses_the_cache(monkeypatch, tmp_path) -> None:
    binary, plat, fetched = _fake_release(monkeypatch, tmp_path)
    exe = tc.install(binary, tmp_path / "cache", plat)
    assert exe.read_bytes() == b"#!/bin/sh\necho fake\n"
    assert exe.stat().st_mode & 0o111
    count = len(fetched)
    assert tc.install(binary, tmp_path / "cache", plat) == exe
    assert len(fetched) == count, "a verified cached binary was downloaded again"


@pytest.mark.unit
def test_a_tampered_cached_binary_is_reinstalled(monkeypatch, tmp_path) -> None:
    binary, plat, fetched = _fake_release(monkeypatch, tmp_path)
    exe = tc.install(binary, tmp_path / "cache", plat)
    exe.write_bytes(b"something else")
    count = len(fetched)
    tc.install(binary, tmp_path / "cache", plat)
    assert exe.read_bytes() == b"#!/bin/sh\necho fake\n"
    assert len(fetched) > count


@pytest.mark.unit
def test_published_sums_disagreeing_with_the_lock_are_refused(
    monkeypatch, tmp_path
) -> None:
    binary, plat, _ = _fake_release(monkeypatch, tmp_path, published="0" * 64)
    with pytest.raises(tc.GateError, match="committed lock"):
        tc.install(binary, tmp_path / "cache", plat)


@pytest.mark.unit
def test_an_archive_matching_neither_hash_is_refused(monkeypatch, tmp_path) -> None:
    binary, plat, _ = _fake_release(monkeypatch, tmp_path, served=b"not the archive")
    with pytest.raises(tc.GateError, match="Refusing to install"):
        tc.install(binary, tmp_path / "cache", plat)
    assert not (
        tmp_path / "cache" / "bin" / "terraform" / "9.9.9" / plat / "terraform"
    ).exists()


@pytest.mark.unit
def test_a_platform_with_no_committed_hash_is_refused(monkeypatch, tmp_path) -> None:
    binary, _, _ = _fake_release(monkeypatch, tmp_path)
    with pytest.raises(tc.GateError, match="no committed checksum"):
        tc.install(binary, tmp_path / "cache", "darwin_arm64")


@pytest.mark.unit
def test_non_https_downloads_are_refused() -> None:
    with pytest.raises(tc.GateError, match="non-HTTPS"):
        tc._fetch("http://releases.hashicorp.com/terraform/")


def _stub_tool(tmp_path: Path, report: dict, validate_rc: int) -> Path:
    exe = tmp_path / "stub-terraform"
    exe.write_text(
        "#!/bin/sh\n"
        'if [ "$1" = init ]; then echo initialised; exit 0; fi\n'
        f"cat <<'JSON'\n{json.dumps(report)}\nJSON\n"
        f"exit {validate_rc}\n"
    )
    exe.chmod(0o755)
    return exe


def _run_stub(monkeypatch, tmp_path, report: dict, validate_rc: int):
    exe = _stub_tool(tmp_path, report, validate_rc)
    monkeypatch.setattr(tc, "install", lambda *_: exe)
    module = tmp_path / "module"
    module.mkdir()
    (module / "main.tf").write_text("")
    return tc.check_one(
        tc.Binary("terraform", "9.9.9"),
        module,
        tmp_path,
        tmp_path / "cache",
        "linux_amd64",
        {"hashicorp/aws": "6.0.0"},
    )


@pytest.mark.unit
def test_warnings_alone_pass(monkeypatch, tmp_path) -> None:
    report = {
        "valid": True,
        "diagnostics": [{"severity": "warning", "summary": "Argument is deprecated"}],
    }
    result = _run_stub(monkeypatch, tmp_path, report, 0)
    assert result.ok and result.warnings == 1 and result.errors == 0


@pytest.mark.unit
def test_an_error_fails(monkeypatch, tmp_path) -> None:
    report = {
        "valid": False,
        "diagnostics": [{"severity": "error", "summary": "Unsupported argument"}],
    }
    result = _run_stub(monkeypatch, tmp_path, report, 1)
    assert not result.ok and result.errors == 1


@pytest.mark.unit
def test_an_error_fails_even_if_the_exit_code_says_otherwise(
    monkeypatch, tmp_path
) -> None:
    report = {
        "valid": True,
        "diagnostics": [{"severity": "error", "summary": "Unsupported argument"}],
    }
    assert not _run_stub(monkeypatch, tmp_path, report, 0).ok


@pytest.mark.unit
def test_unparseable_validate_output_fails(monkeypatch, tmp_path) -> None:
    exe = tmp_path / "stub-terraform"
    exe.write_text("#!/bin/sh\necho not json\n")
    exe.chmod(0o755)
    monkeypatch.setattr(tc, "install", lambda *_: exe)
    module = tmp_path / "module"
    module.mkdir()
    result = tc.check_one(
        tc.Binary("terraform", "9.9.9"), module, tmp_path, tmp_path / "c", "x", {}
    )
    assert not result.ok


@pytest.mark.unit
def test_the_override_pins_every_provider_exactly() -> None:
    providers = tc.gate_providers([])
    text = tc.override_tf(providers)
    assert providers["hashicorp/awscc"] == tc.awscc_vendored_version()
    for source, version in providers.items():
        assert f'source  = "{source}"' in text
        assert f'version = "= {version}"' in text
    assert tc.gate_providers(["hashicorp/aws=6.1.0"])["hashicorp/aws"] == "6.1.0"
    with pytest.raises(tc.GateError):
        tc.gate_providers(["aws=6.1.0"])


def _versions_tf_ranges() -> dict[str, SpecifierSet]:
    """``{provider source: range}`` from the generated ``versions.tf`` template."""
    text = STATIC_FILES.read_text()
    block = text[
        text.index("VERSIONS_TF") : text.index('"""\n', text.index("VERSIONS_TF") + 20)
    ]
    ranges = {}
    for source, version in re.findall(
        r'source\s*=\s*"([^"]+)"(?:\s*#[^\n]*)*\s*version\s*=\s*"([^"]+)"', block
    ):
        ranges[source] = version
    return {
        s: SpecifierSet(v.replace("AWSCC_VERSION", tc.awscc_vendored_version()))
        for s, v in ranges.items()
    }


@pytest.mark.unit
def test_every_pinned_provider_satisfies_the_generated_range() -> None:
    """A pin outside the module's own range makes ``init`` fail for the wrong reason."""
    ranges = _versions_tf_ranges()
    providers = tc.gate_providers([])
    assert set(providers) == set(ranges), (
        f"the gate pins {sorted(providers)} but versions.tf declares {sorted(ranges)}"
    )
    for source, version in providers.items():
        assert Version(version) in ranges[source], (
            f"{source}={version} is outside versions.tf's range {ranges[source]}"
        )


def _makefile_pin(name: str) -> str:
    match = re.search(rf"^{name}\s*:=\s*(\S+)", MAKEFILE.read_text(), re.MULTILINE)
    assert match, f"{name} is not declared in the Makefile"
    return match.group(1)


@pytest.mark.unit
def test_the_floor_pins_are_the_floor_versions_tf_admits() -> None:
    """The floor release tested is the oldest one the generated module accepts.

    ``required_version = ">= 1.7.0"`` is a promise to users of Terraform 1.7 and
    OpenTofu 1.7. A floor pin on a later minor would stop testing that promise
    while the range kept making it.
    """
    match = re.search(r'required_version = ">= (\d+)\.(\d+)', STATIC_FILES.read_text())
    assert match, "versions.tf no longer declares a required_version floor"
    floor = match.groups()
    for name in ("TERRAFORM_FLOOR_VERSION", "OPENTOFU_FLOOR_VERSION"):
        assert tuple(_makefile_pin(name).split(".")[:2]) == floor, (
            f"{name}={_makefile_pin(name)} is not on the {'.'.join(floor)} line "
            "that versions.tf's required_version admits as its floor"
        )


@pytest.mark.unit
def test_every_locked_binary_covers_every_locked_platform() -> None:
    lock = json.loads(tc.LOCK_FILE.read_text())["binaries"]
    assert lock, "the lock file records no binaries"
    for spec, entry in lock.items():
        tc.Binary.parse(spec)
        assert set(entry) == set(tc.LOCKED_PLATFORMS), spec
        for digest in entry.values():
            assert re.fullmatch(r"[0-9a-f]{64}", digest), (spec, digest)
