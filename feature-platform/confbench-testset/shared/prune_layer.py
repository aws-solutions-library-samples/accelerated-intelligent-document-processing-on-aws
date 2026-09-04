# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Strip build-time-only files from the built SharedLayer, then assert it fits.

Run by `build-SharedLayer` in this directory's Makefile, against the layer SAM
has just populated. Removes ~35 MB of pyarrow that `pyarrow.parquet` cannot
reach at runtime (C++ headers, vendored sources, Cython sources, the Arrow
Flight RPC stack), then refuses to let the build proceed if the result would
break an in-place stack update.

Why a size ceiling at all
-------------------------
Lambda caps *code + all layers* at 262,144,000 bytes unzipped. The steady state
here is nowhere near it (layer ~101 MB + ingest code ~8.5 MB). The binding
constraint is the *transition*: when CloudFormation updates a function it calls
UpdateFunctionConfiguration (attaching the new layer) before UpdateFunctionCode,
so for one moment the new layer is combined with the OLD code. Feature versions
before 2026-08-13 bundled pyarrow into `ingest/` itself, so that old code is
144,555,306 bytes — and a stack still sitting on one of those versions has only
`262,144,000 - 144,555,306` of layer budget to cross the gap. Exceeding it is
exactly how the IDP1 update failed at 280,347,130 bytes.

Why the guards, not just `rm`
-----------------------------
Pruning by filename is silently fragile across pyarrow releases: a rename makes
the rules match nothing (size regresses, build still "succeeds"), and a new
inter-library dependency makes a pruned `.so` load-bearing (import fails in
Lambda, not in CI). So each rule must match something, and every surviving ELF
object is checked to confirm it does not link anything we removed.
"""

from __future__ import annotations

import os
import shutil
import struct
import sys
from pathlib import Path
from typing import Iterable, List, Tuple

# Lambda's hard cap on code + layers, unzipped.
LAMBDA_UNZIPPED_LIMIT = 262_144_000

# Largest `ingest/` code artifact any deployed stack can still be sitting on:
# feature versions up to 0.6.4.dev4 bundled pyarrow into the function itself.
# Measured from idp-cli/0.6.3.dev6/cc4d0680c361ea83af1a90ae19b8fe7a.
LEGACY_INGEST_CODE_BYTES = 144_555_306

# The layer must be small enough to combine with that old code mid-update.
LAYER_CEILING = LAMBDA_UNZIPPED_LIMIT - LEGACY_INGEST_CODE_BYTES

# All rules are relative to the layer's ARTIFACTS_DIR, and Lambda mounts a
# layer's `python/` subdirectory onto sys.path — hence the prefix on every rule.

# Directories that exist only to compile *against* Arrow, never to run it.
PRUNE_DIRS = (
    "python/pyarrow/include",  # Arrow/Parquet C++ public headers
    "python/pyarrow/src",  # vendored arrow::py C++ sources and headers
    "python/pyarrow/tests",  # pyarrow's own test suite
)

# Cython inputs, already compiled into the shipped extension modules, plus the
# `lib.h` / `lib_api.h` C API headers third-party Cython code would #include.
PRUNE_GLOBS = (
    "python/pyarrow/**/*.pxi",
    "python/pyarrow/**/*.pyx",
    "python/pyarrow/**/*.pxd",
    "python/pyarrow/*.h",
)

# Arrow Flight — a gRPC client/server stack. `libarrow_flight` is the single
# largest prunable object (24 MB) and nothing on the parquet read path links it;
# `check_link_integrity` below is what keeps that claim honest release to
# release. `pyarrow/flight.py` is deliberately left in place: pyarrow's
# __init__ probes optional submodules defensively, so the stub failing to import
# is the documented behaviour for a build without Flight.
PRUNE_GLOBS_FLIGHT = (
    "python/pyarrow/libarrow_flight.so*",
    "python/pyarrow/libarrow_python_flight.so*",
    "python/pyarrow/_flight.*.so",
)


def tree_bytes(root: Path) -> int:
    """Total bytes of every regular file under `root`, as Lambda counts it."""
    total = 0
    for dirpath, _dirnames, filenames in os.walk(root):
        for name in filenames:
            path = Path(dirpath) / name
            if path.is_file():
                total += path.stat().st_size
    return total


def dt_needed(path: Path) -> List[str]:
    """DT_NEEDED entries of an ELF64 shared object; [] if not an ELF64 file.

    Hand-rolled because the build runs with no third-party deps available: the
    layer's own requirements.txt is the only thing pip installed.
    """
    data = path.read_bytes()
    if data[:4] != b"\x7fELF" or data[4] != 2:  # not ELF, or not 64-bit
        return []
    endian = "<" if data[5] == 1 else ">"

    (e_shoff,) = struct.unpack_from(endian + "Q", data, 0x28)
    (e_shentsize,) = struct.unpack_from(endian + "H", data, 0x3A)
    (e_shnum,) = struct.unpack_from(endian + "H", data, 0x3C)

    sections: List[Tuple[int, int, int, int]] = []
    for i in range(e_shnum):
        off = e_shoff + i * e_shentsize
        (sh_type,) = struct.unpack_from(endian + "I", data, off + 0x04)
        (sh_offset,) = struct.unpack_from(endian + "Q", data, off + 0x18)
        (sh_size,) = struct.unpack_from(endian + "Q", data, off + 0x20)
        (sh_link,) = struct.unpack_from(endian + "I", data, off + 0x28)
        sections.append((sh_type, sh_offset, sh_size, sh_link))

    needed: List[str] = []
    for sh_type, sh_offset, sh_size, sh_link in sections:
        if sh_type != 6:  # SHT_DYNAMIC
            continue
        strtab = sections[sh_link][1]
        pos = sh_offset
        while pos < sh_offset + sh_size:
            tag, val = struct.unpack_from(endian + "qQ", data, pos)
            pos += 16
            if tag == 0:  # DT_NULL
                break
            if tag == 1:  # DT_NEEDED
                end = data.index(b"\0", strtab + val)
                needed.append(data[strtab + val : end].decode())
    return needed


def collect(root: Path) -> Tuple[List[Path], List[str]]:
    """Resolve the prune rules against `root`.

    Returns the paths to remove and a list of rules that matched nothing —
    the signal that pyarrow's layout moved under us.
    """
    targets: List[Path] = []
    unmatched: List[str] = []

    for rel in PRUNE_DIRS:
        path = root / rel
        if path.is_dir():
            targets.append(path)
        else:
            unmatched.append(f"{rel}/")

    for pattern in PRUNE_GLOBS + PRUNE_GLOBS_FLIGHT:
        hits = [p for p in root.glob(pattern) if p.is_file()]
        if hits:
            targets.extend(hits)
        else:
            unmatched.append(pattern)

    return targets, unmatched


def check_link_integrity(root: Path, targets: Iterable[Path]) -> List[str]:
    """Report any *surviving* ELF object that links a library we are removing.

    Called before anything is deleted, so `targets` defines both what goes away
    (its shared-library basenames become the forbidden DT_NEEDED set) and which
    objects are exempt from the check.
    """
    doomed = {p.resolve() for p in targets}
    doomed_dirs = [p for p in doomed if p.is_dir()]
    removed_libs = {p.name for p in doomed if p.is_file() and ".so" in p.name}

    problems: List[str] = []
    for path in sorted(root.rglob("*.so*")):
        if not path.is_file():
            continue
        resolved = path.resolve()
        if resolved in doomed or any(d in resolved.parents for d in doomed_dirs):
            continue  # this object is going away too
        for lib in dt_needed(path):
            if lib in removed_libs:
                problems.append(f"{path.relative_to(root)} -> {lib}")
    return problems


def main(argv: List[str]) -> int:
    if len(argv) != 2:
        print(f"usage: {argv[0]} <layer-artifacts-dir>", file=sys.stderr)
        return 2

    root = Path(argv[1]).resolve()
    if not root.is_dir():
        print(f"prune_layer: not a directory: {root}", file=sys.stderr)
        return 2

    before = tree_bytes(root)
    targets, unmatched = collect(root)

    if unmatched:
        print(
            "prune_layer: these prune rules matched nothing, so the layer would\n"
            "ship larger than intended. pyarrow's layout has probably changed —\n"
            "re-derive the rules against the new wheel instead of relaxing this\n"
            "check:\n  " + "\n  ".join(unmatched),
            file=sys.stderr,
        )
        return 1

    # Verify BEFORE deleting: a pyarrow release that starts linking Flight from
    # the parquet read path must fail the build, not the Lambda cold start.
    survivors_problems = check_link_integrity(root, targets)
    if survivors_problems:
        print(
            "prune_layer: a shared library scheduled for removal is still\n"
            "load-bearing. Pruning it would break `import pyarrow` at runtime:\n"
            "  " + "\n  ".join(survivors_problems),
            file=sys.stderr,
        )
        return 1

    for path in targets:
        if path.is_dir():
            shutil.rmtree(path)
        elif path.exists():
            path.unlink()

    after = tree_bytes(root)
    print(
        f"prune_layer: {before:,} -> {after:,} bytes "
        f"(removed {before - after:,}); ceiling {LAYER_CEILING:,}, "
        f"headroom {LAYER_CEILING - after:,}"
    )

    if after > LAYER_CEILING:
        print(
            f"prune_layer: layer is {after:,} bytes, over the "
            f"{LAYER_CEILING:,}-byte ceiling by {after - LAYER_CEILING:,}.\n"
            f"Combined with the {LEGACY_INGEST_CODE_BYTES:,}-byte legacy ingest\n"
            f"code it would exceed Lambda's {LAMBDA_UNZIPPED_LIMIT:,}-byte limit,\n"
            "so an in-place update of any stack installed before 2026-08-13 would\n"
            "fail the way IDP1's did. See this file's module docstring.",
            file=sys.stderr,
        )
        return 1

    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
