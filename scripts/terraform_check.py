#!/usr/bin/env python3
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Translate ``template.yaml`` to Terraform offline, then ``init`` and ``validate`` it.

This is the gate behind ``make terraform-check``. It answers one question: does
the module tree the translator in ``terraform/translator`` generates from the
**source** templates load and type-check under each pinned Terraform and OpenTofu
release? It does so in three steps:

1. Translate ``template.yaml`` (and its nested templates, read from source) into a
   temporary directory with ``idp_tf.cli --offline``. No AWS call is made.
2. For each binary in ``--binaries`` (``terraform@1.7.5``, ``tofu@1.7.10``, ...),
   install it into a cache directory, verifying the archive against BOTH the
   release's published ``SHA256SUMS`` file and the hash committed in
   ``scripts/terraform_binaries.lock.json``.
3. Copy the module into a fresh directory per binary and run
   ``init -backend=false`` then ``validate -json``. Errors fail the gate;
   warnings are counted and printed but do not.

What it does NOT prove, stated so a green run is not over-read: nothing is
planned or deployed, so no AWS-side validation (IAM policy grammar, a property
value the service rejects, quotas, cross-resource ordering at apply time)
happens here. ``validate`` checks HCL syntax, references, types, and every
attribute against the provider schemas. The translator's own unit tests run in
``make test`` and are not repeated here.

**Providers are pinned for the gate.** The generated ``versions.tf`` carries
ranges (``aws >= 6.0, < 7.0``), which is right for the module's users and wrong
for a blocking gate: a provider release would change the verdict with no code
change, the reason ``CFN_LINT_VERSION`` is pinned. So the gate writes an override
file, ``zz_gate_providers_override.tf``, holding exact versions. Terraform and
OpenTofu merge ``required_providers`` from an override file entry by entry, and
every nested module's own range must still be satisfied by the pinned version,
so the ranges are still exercised. ``awscc`` is pinned to the version of the
vendored schema the translator read (``terraform/translator/schemas/awscc.json``),
so ``validate`` checks the output against the same schema that produced it.

**Supply chain.** Downloads are HTTPS only, from the vendor's release host. An
archive is accepted only when its SHA256 equals the entry for it in the published
``SHA256SUMS`` *and* the entry committed in the lock file. The second check is what
makes this more than a corruption check: a release host that served a different
archive together with a matching ``SHA256SUMS`` would still be refused. A binary
reused from the cache is re-hashed against the digest recorded when it was
installed. Nothing is piped to a shell. ``--update-lock`` is the maintainer step
that records hashes for a new version; it reads them from ``SHA256SUMS``, so a
version bump should be reviewed against the vendor's signed release.
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import os
import platform
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.request
import zipfile
from dataclasses import dataclass
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
TRANSLATOR_DIR = REPO_ROOT / "terraform" / "translator"
VENDORED_SCHEMA = TRANSLATOR_DIR / "schemas" / "awscc.json"
LOCK_FILE = Path(__file__).resolve().with_name("terraform_binaries.lock.json")

#: Platforms recorded in the lock file: both CI runner architectures (GitHub's
#: ``ubuntu-latest`` is amd64, GitLab's fast_checks runners are arm64) and both
#: macOS architectures, for developers.
LOCKED_PLATFORMS = ("linux_amd64", "linux_arm64", "darwin_amd64", "darwin_arm64")

TOOLS = {
    "terraform": (
        "https://releases.hashicorp.com/terraform/{v}/",
        "terraform_{v}_{p}.zip",
        "terraform_{v}_SHA256SUMS",
    ),
    "tofu": (
        "https://github.com/opentofu/opentofu/releases/download/v{v}/",
        "tofu_{v}_{p}.zip",
        "tofu_{v}_SHA256SUMS",
    ),
}

OVERRIDE_FILE = "zz_gate_providers_override.tf"
DOWNLOAD_TIMEOUT_SECONDS = 120


class GateError(Exception):
    """A failure the gate reports by message rather than by traceback."""


@dataclass(frozen=True)
class Binary:
    tool: str
    version: str

    @classmethod
    def parse(cls, spec: str) -> Binary:
        tool, sep, version = spec.partition("@")
        if not sep or tool not in TOOLS or not version:
            raise GateError(
                f"bad binary spec {spec!r}: expected <tool>@<version> with tool "
                f"one of {sorted(TOOLS)}"
            )
        return cls(tool, version)

    def __str__(self) -> str:
        return f"{self.tool}@{self.version}"


def current_platform() -> str:
    system = platform.system().lower()
    machine = platform.machine().lower()
    arch = {"x86_64": "amd64", "amd64": "amd64", "aarch64": "arm64", "arm64": "arm64"}
    if system not in ("linux", "darwin") or machine not in arch:
        raise GateError(f"unsupported platform {system}/{machine}")
    return f"{system}_{arch[machine]}"


def _fetch(url: str) -> bytes:
    if not url.startswith("https://"):
        raise GateError(f"refusing a non-HTTPS download: {url}")
    request = urllib.request.Request(url, headers={"User-Agent": "idp-terraform-check"})
    with urllib.request.urlopen(request, timeout=DOWNLOAD_TIMEOUT_SECONDS) as response:  # nosec B310 - https only, asserted above
        return response.read()


def published_sums(binary: Binary) -> dict[str, str]:
    """``{archive filename: sha256}`` from the release's published SHA256SUMS."""
    base, _, sums = TOOLS[binary.tool]
    text = _fetch(base.format(v=binary.version) + sums.format(v=binary.version))
    entries: dict[str, str] = {}
    for line in text.decode().splitlines():
        parts = line.split()
        if len(parts) == 2:
            entries[parts[1].lstrip("*")] = parts[0].lower()
    return entries


def load_lock() -> dict[str, dict[str, str]]:
    return json.loads(LOCK_FILE.read_text())["binaries"]


def locked_sha(binary: Binary, plat: str) -> str:
    entry = load_lock().get(str(binary), {})
    if plat not in entry:
        raise GateError(
            f"{binary} has no committed checksum for {plat} in {LOCK_FILE}. Run "
            f"`python3 scripts/terraform_check.py --update-lock {binary}` and review "
            "the recorded hashes against the vendor's signed release."
        )
    return entry[plat]


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def install(binary: Binary, cache: Path, plat: str) -> Path:
    """Return the path of a verified ``binary``, installing it into ``cache``."""
    target_dir = cache / "bin" / binary.tool / binary.version / plat
    exe = target_dir / binary.tool
    digest_file = target_dir / f"{binary.tool}.sha256"
    if exe.is_file() and digest_file.is_file():
        if _sha256(exe.read_bytes()) == digest_file.read_text().strip():
            return exe
        print(f"  cached {binary} does not match its recorded digest; reinstalling")

    expected = locked_sha(binary, plat)
    base, archive_name, _ = TOOLS[binary.tool]
    archive_name = archive_name.format(v=binary.version, p=plat)
    published = published_sums(binary).get(archive_name)
    if published != expected:
        raise GateError(
            f"{archive_name}: published SHA256SUMS says {published}, the committed "
            f"lock says {expected}. Refusing to install."
        )
    data = _fetch(base.format(v=binary.version) + archive_name)
    actual = _sha256(data)
    if actual != expected:
        raise GateError(
            f"{archive_name}: downloaded archive hashes to {actual}, expected "
            f"{expected}. Refusing to install."
        )

    target_dir.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(dir=target_dir))
    try:
        archive = staging / archive_name
        archive.write_bytes(data)
        with zipfile.ZipFile(archive) as zf:
            # Exactly one named member, written to a fixed path: no path from
            # inside the archive is ever joined onto the filesystem.
            payload = zf.read(binary.tool)
        staged_exe = staging / binary.tool
        staged_exe.write_bytes(payload)
        staged_exe.chmod(0o755)
        os.replace(staged_exe, exe)
        digest_file.write_text(_sha256(payload) + "\n")
    finally:
        shutil.rmtree(staging, ignore_errors=True)
    return exe


def update_lock(specs: list[str]) -> int:
    doc = json.loads(LOCK_FILE.read_text()) if LOCK_FILE.exists() else {}
    binaries = doc.setdefault("binaries", {})
    for spec in specs:
        binary = Binary.parse(spec)
        sums = published_sums(binary)
        _, archive_name, _ = TOOLS[binary.tool]
        entry = {}
        for plat in LOCKED_PLATFORMS:
            name = archive_name.format(v=binary.version, p=plat)
            if name not in sums:
                raise GateError(f"{name} is not in the published SHA256SUMS")
            entry[plat] = sums[name]
        binaries[str(binary)] = entry
        print(f"recorded {binary}")
    doc["binaries"] = dict(sorted(binaries.items()))
    LOCK_FILE.write_text(json.dumps(doc, indent=2) + "\n")
    return 0


def awscc_vendored_version() -> str:
    return json.loads(VENDORED_SCHEMA.read_text())["awscc_version"]


def override_tf(providers: dict[str, str]) -> str:
    lines = [
        "# Written by scripts/terraform_check.py: exact provider versions for the",
        "# offline gate. Not part of the generated module.",
        "terraform {",
        "  required_providers {",
    ]
    for source, version in sorted(providers.items()):
        name = source.rsplit("/", 1)[-1]
        lines.append(f'    {name} = {{\n      source  = "{source}"')
        lines.append(f'      version = "= {version}"\n    }}')
    lines += ["  }", "}", ""]
    return "\n".join(lines)


#: Import names of the translator's dependencies (terraform/translator/pyproject.toml).
#: In CI they arrive through the locked first-party install (moto's cloudformation
#: extra pulls cfn-lint, which pulls aws-sam-translator); checked up front so a
#: missing one is named instead of surfacing as a traceback from the translator.
TRANSLATOR_IMPORTS = ("samtranslator", "yaml")


def translate(out: Path) -> None:
    missing = [m for m in TRANSLATOR_IMPORTS if importlib.util.find_spec(m) is None]
    if missing:
        raise GateError(
            f"the translator's dependencies are not installed ({', '.join(missing)}). "
            "Run `make setup`, which installs them constrained to the lock."
        )
    command = [
        sys.executable,
        "-m",
        "idp_tf.cli",
        str(REPO_ROOT / "template.yaml"),
        "--offline",
        "--out",
        str(out),
        "--label-template",
        str(REPO_ROOT / "template.yaml"),
        "--version",
        "0.0.0-offline",
    ]
    result = subprocess.run(  # nosec B603 - fixed argv, no shell
        command, cwd=TRANSLATOR_DIR, capture_output=True, text=True, check=False
    )
    if result.returncode != 0:
        sys.stdout.write(result.stdout)
        sys.stderr.write(result.stderr)
        raise GateError(f"translation failed (exit {result.returncode})")
    last = result.stdout.strip().splitlines()[-1:] or [""]
    print(f"  {last[0]}")


def _format_diagnostic(diag: dict) -> str:
    where = ""
    rng = diag.get("range")
    if rng:
        where = f" ({rng.get('filename')}:{rng.get('start', {}).get('line')})"
    detail = (diag.get("detail") or "").strip().replace("\n", " ")
    text = f"{diag.get('severity', '?').upper()}: {diag.get('summary')}{where}"
    return f"{text}\n      {detail}" if detail else text


@dataclass
class Result:
    binary: Binary
    ok: bool
    install_s: float = 0.0
    init_s: float = 0.0
    validate_s: float = 0.0
    errors: int = 0
    warnings: int = 0


def check_one(
    binary: Binary,
    module: Path,
    work: Path,
    cache: Path,
    plat: str,
    providers: dict[str, str],
) -> Result:
    print(f"\n== {binary}")
    result = Result(binary, ok=False)
    started = time.monotonic()
    exe = install(binary, cache, plat)
    result.install_s = time.monotonic() - started

    workdir = work / str(binary).replace("@", "-")
    shutil.copytree(module, workdir)
    (workdir / OVERRIDE_FILE).write_text(override_tf(providers))
    plugin_cache = cache / "plugins"
    plugin_cache.mkdir(parents=True, exist_ok=True)
    env = {
        **os.environ,
        "TF_PLUGIN_CACHE_DIR": str(plugin_cache),
        # The working directory is fresh, so there is never a lock file to break,
        # and without this both tools re-download every provider on every run.
        "TF_PLUGIN_CACHE_MAY_BREAK_DEPENDENCY_LOCK_FILE": "true",
        "TF_IN_AUTOMATION": "1",
        "TF_INPUT": "0",
        "CHECKPOINT_DISABLE": "1",
    }
    env.pop("TF_CLI_ARGS", None)

    started = time.monotonic()
    init = subprocess.run(  # nosec B603 - fixed argv, no shell
        [str(exe), "init", "-backend=false", "-input=false", "-no-color"],
        cwd=workdir,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    result.init_s = time.monotonic() - started
    if init.returncode != 0:
        sys.stdout.write(init.stdout)
        sys.stdout.write(init.stderr)
        print(f"  init FAILED (exit {init.returncode})")
        return result

    started = time.monotonic()
    validate = subprocess.run(  # nosec B603 - fixed argv, no shell
        [str(exe), "validate", "-json", "-no-color"],
        cwd=workdir,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    result.validate_s = time.monotonic() - started
    try:
        report = json.loads(validate.stdout)
    except json.JSONDecodeError:
        sys.stdout.write(validate.stdout)
        sys.stdout.write(validate.stderr)
        print(f"  validate produced no JSON report (exit {validate.returncode})")
        return result

    diagnostics = report.get("diagnostics", [])
    result.errors = sum(d.get("severity") == "error" for d in diagnostics)
    result.warnings = sum(d.get("severity") == "warning" for d in diagnostics)
    for diag in diagnostics:
        if diag.get("severity") == "error":
            print("  " + _format_diagnostic(diag))
    if result.warnings:
        summaries: dict[str, int] = {}
        for diag in diagnostics:
            if diag.get("severity") == "warning":
                key = diag.get("summary") or "?"
                summaries[key] = summaries.get(key, 0) + 1
        for summary, count in sorted(summaries.items(), key=lambda kv: -kv[1]):
            print(f"  warning x{count}: {summary}")
    result.ok = (
        validate.returncode == 0 and result.errors == 0 and report.get("valid", False)
    )
    print(
        f"  {'valid' if result.ok else 'INVALID'}: {result.errors} error(s), "
        f"{result.warnings} warning(s)"
    )
    return result


def gate_providers(overrides: list[str]) -> dict[str, str]:
    """Exact provider pins: the lock file's, ``awscc`` from the vendored schema.

    ``overrides`` (``<ns>/<name>=<version>``) replace individual entries, for
    trying a provider bump locally before recording it in the lock file.
    """
    providers = dict(json.loads(LOCK_FILE.read_text()).get("providers", {}))
    providers["hashicorp/awscc"] = awscc_vendored_version()
    for spec in overrides:
        source, sep, version = spec.partition("=")
        if not sep or source.count("/") != 1 or not version:
            raise GateError(
                f"bad provider pin {spec!r}: expected <ns>/<name>=<version>"
            )
        providers[source] = version
    return providers


def run(args: argparse.Namespace) -> int:
    binaries = [Binary.parse(s) for s in args.binaries.split()]
    if not binaries:
        raise GateError("no binaries given")
    providers = gate_providers(args.providers.split())
    cache = Path(args.cache_dir).expanduser().resolve()
    plat = current_platform()
    print(f"terraform-check: {', '.join(map(str, binaries))} on {plat}")
    print(f"  providers: {', '.join(f'{k}={v}' for k, v in sorted(providers.items()))}")
    print(f"  cache: {cache}")

    results: list[Result] = []
    with tempfile.TemporaryDirectory(prefix="idp-terraform-check-") as tmp:
        module = Path(tmp) / "module"
        started = time.monotonic()
        translate(module)
        print(f"  translated in {time.monotonic() - started:.1f}s")
        for binary in binaries:
            results.append(check_one(binary, module, Path(tmp), cache, plat, providers))

    print("\nbinary              install   init  validate  errors  warnings  result")
    for r in results:
        print(
            f"{str(r.binary):<18} {r.install_s:7.1f}s {r.init_s:5.1f}s "
            f"{r.validate_s:8.1f}s {r.errors:7d} {r.warnings:9d}  "
            f"{'ok' if r.ok else 'FAILED'}"
        )
    failed = [str(r.binary) for r in results if not r.ok]
    if failed:
        print(f"\nterraform-check FAILED for {', '.join(failed)}")
        return 1
    print("\nterraform-check: every binary initialised and validated the module")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--binaries",
        default="",
        help="space-separated <tool>@<version> list (tool: terraform or tofu)",
    )
    parser.add_argument(
        "--providers",
        default="",
        help="space-separated <ns>/<name>=<version> pins overriding the lock file's, "
        "for trying a provider bump locally",
    )
    parser.add_argument(
        "--cache-dir",
        default="~/.cache/idp-terraform-check",
        help="where verified binaries and the provider plugin cache live",
    )
    parser.add_argument(
        "--update-lock",
        nargs="+",
        metavar="TOOL@VERSION",
        help="record published checksums for these binaries in the lock file, "
        "then exit",
    )
    args = parser.parse_args(argv)
    try:
        if args.update_lock:
            return update_lock(args.update_lock)
        return run(args)
    except GateError as exc:
        print(f"terraform-check: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
