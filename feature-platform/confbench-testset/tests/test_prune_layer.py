# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Tests for shared/prune_layer.py — the SharedLayer size gate.

The point of this module is the *guards*, not the deletion. Pruning by filename
is silently fragile across pyarrow releases, so prune_layer.py refuses to build
when a rule stops matching or when a library it wants to remove turns out to be
load-bearing. These tests pin that behaviour.
"""

from __future__ import annotations

import importlib.util
import re
import struct
from pathlib import Path

import pytest
import yaml

_SHARED = Path(__file__).resolve().parent.parent / "shared"

# Imported by path with a distinct name: it is a build-time script beside the
# layer's importable content, not part of `shared/python` on sys.path.
_spec = importlib.util.spec_from_file_location(
    "confbench_prune_layer", _SHARED / "prune_layer.py"
)
assert _spec and _spec.loader
prune_layer = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(prune_layer)


def _write(path: Path, size: int = 0) -> Path:
    """Create a file of `size` bytes, making parents as needed."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"\0" * size)
    return path


def _elf64(needed: list[str]) -> bytes:
    """A minimal little-endian ELF64 with a .dynamic section listing DT_NEEDED.

    Hand-built rather than fixture'd so the test does not depend on a wheel
    being present, and so it exercises the same struct offsets as the real
    aarch64 objects the build parses.
    """
    strtab = b"\0"
    offsets = []
    for name in needed:
        offsets.append(len(strtab))
        strtab += name.encode() + b"\0"

    dynamic = b"".join(struct.pack("<qQ", 1, off) for off in offsets)
    dynamic += struct.pack("<qQ", 0, 0)  # DT_NULL

    ehdr_size, shentsize = 0x40, 0x40
    dyn_off = ehdr_size
    str_off = dyn_off + len(dynamic)
    sh_off = str_off + len(strtab)

    header = bytearray(ehdr_size)
    header[0:4] = b"\x7fELF"
    header[4] = 2  # ELFCLASS64
    header[5] = 1  # ELFDATA2LSB
    struct.pack_into("<Q", header, 0x28, sh_off)
    struct.pack_into("<H", header, 0x3A, shentsize)
    struct.pack_into("<H", header, 0x3C, 2)  # two sections: .dynamic, .dynstr

    def section(sh_type: int, offset: int, size: int, link: int) -> bytes:
        raw = bytearray(shentsize)
        struct.pack_into("<I", raw, 0x04, sh_type)
        struct.pack_into("<Q", raw, 0x18, offset)
        struct.pack_into("<Q", raw, 0x20, size)
        struct.pack_into("<I", raw, 0x28, link)
        return bytes(raw)

    sections = section(6, dyn_off, len(dynamic), 1)  # SHT_DYNAMIC, links -> idx 1
    sections += section(3, str_off, len(strtab), 0)  # SHT_STRTAB

    return bytes(header) + dynamic + strtab + sections


def _layer(
    root: Path, *, flight_needed_by_lib: bool = False, s3_needed_by_lib: bool = False
) -> Path:
    """A miniature stand-in for the built layer, matching pyarrow 26's layout."""
    pa = root / "python" / "pyarrow"
    _write(root / "python" / "variants.py", 11_010)

    # Prunable: build-time-only trees, Cython sources, C API headers.
    _write(pa / "include" / "arrow" / "api.h", 4_000)
    # From pyarrow 26 the C API headers live here, not at the package root.
    _write(pa / "include" / "arrow" / "python" / "lib.h", 500)
    _write(pa / "src" / "arrow" / "python" / "pyarrow.h", 1_000)
    _write(pa / "tests" / "test_table.py", 2_000)
    _write(pa / "table.pxi", 1_500)
    _write(pa / "lib.pyx", 1_500)
    _write(pa / "includes" / "libarrow.pxd", 1_500)

    # Prunable: the Flight RPC stack.
    _write(pa / "libarrow_flight.so.2600", 24_000)
    _write(pa / "libarrow_python_flight.so", 200)
    _write(pa / "_flight.cpython-312-aarch64-linux-gnu.so", 1_000)

    # Prunable: the S3 filesystem, which only `_s3fs` links.
    _write(pa / "libarrow_s3.so.2600", 16_000)
    (pa / "_s3fs.cpython-312-aarch64-linux-gnu.so").write_bytes(
        _elf64(["libarrow_s3.so.2600", "libarrow.so.2600"])
    )

    # Kept: the parquet read path. `lib` links the libraries that must survive.
    lib_needs = ["libarrow.so.2600", "libparquet.so.2600"]
    if flight_needed_by_lib:
        lib_needs.append("libarrow_flight.so.2600")
    if s3_needed_by_lib:
        lib_needs.append("libarrow_s3.so.2600")
    (pa / "lib.cpython-312-aarch64-linux-gnu.so").write_bytes(_elf64(lib_needs))
    _write(pa / "libarrow.so.2600", 45_000)
    _write(pa / "libparquet.so.2600", 11_000)
    _write(pa / "parquet" / "__init__.py", 100)
    return root


class TestDtNeeded:
    def test_parses_dt_needed_from_elf64(self) -> None:
        blob = _elf64(["libarrow.so.2300", "libparquet.so.2300"])
        path = Path(self.tmp) / "x.so"
        path.write_bytes(blob)
        assert prune_layer.dt_needed(path) == [
            "libarrow.so.2300",
            "libparquet.so.2300",
        ]

    def test_non_elf_returns_empty(self) -> None:
        path = Path(self.tmp) / "notelf.so"
        path.write_bytes(b"\xcf\xfa\xed\xfe" + b"\0" * 128)  # Mach-O magic
        assert prune_layer.dt_needed(path) == []

    @pytest.fixture(autouse=True)
    def _tmp(self, tmp_path: Path) -> None:
        self.tmp = tmp_path


class TestCollect:
    def test_matches_every_rule_against_a_realistic_layout(
        self, tmp_path: Path
    ) -> None:
        targets, unmatched = prune_layer.collect(_layer(tmp_path))
        assert unmatched == []
        names = {p.name for p in targets}
        assert "libarrow_flight.so.2600" in names
        assert "libarrow_s3.so.2600" in names
        assert "_s3fs.cpython-312-aarch64-linux-gnu.so" in names
        assert "table.pxi" in names
        assert "libarrow.so.2600" not in names  # load-bearing, must survive

    def test_reports_rules_that_match_nothing(self, tmp_path: Path) -> None:
        """A pyarrow layout change must surface as unmatched rules, not silence."""
        _write(tmp_path / "python" / "variants.py", 11_010)
        _, unmatched = prune_layer.collect(tmp_path)
        assert len(unmatched) == len(
            prune_layer.PRUNE_DIRS
            + prune_layer.PRUNE_GLOBS
            + prune_layer.PRUNE_GLOBS_FLIGHT
            + prune_layer.PRUNE_GLOBS_S3
        )

    def test_a_root_level_header_rule_would_match_nothing(self, tmp_path: Path) -> None:
        """pyarrow 26 moved lib.h/lib_api.h under include/; pin that no rule
        still expects them at the package root."""
        root = _layer(tmp_path)
        assert not list((root / "python" / "pyarrow").glob("*.h"))
        _, unmatched = prune_layer.collect(root)
        assert unmatched == []


class TestLinkIntegrity:
    def test_passes_when_nothing_kept_links_a_pruned_library(
        self, tmp_path: Path
    ) -> None:
        root = _layer(tmp_path)
        targets, _ = prune_layer.collect(root)
        assert prune_layer.check_link_integrity(root, targets) == []

    def test_fails_when_a_pruned_library_is_load_bearing(self, tmp_path: Path) -> None:
        """The regression that would otherwise only show up as a cold-start crash."""
        root = _layer(tmp_path, flight_needed_by_lib=True)
        targets, _ = prune_layer.collect(root)
        problems = prune_layer.check_link_integrity(root, targets)
        assert len(problems) == 1
        assert "libarrow_flight.so.2600" in problems[0]

    def test_fails_when_a_kept_object_links_libarrow_s3(self, tmp_path: Path) -> None:
        """The S3 prune is safe only while `_s3fs` is the sole object linking it."""
        root = _layer(tmp_path, s3_needed_by_lib=True)
        targets, _ = prune_layer.collect(root)
        problems = prune_layer.check_link_integrity(root, targets)
        assert problems == [
            "python/pyarrow/lib.cpython-312-aarch64-linux-gnu.so -> libarrow_s3.so.2600"
        ]


class TestMain:
    def test_prunes_and_reports(self, tmp_path: Path, capsys) -> None:
        root = _layer(tmp_path)
        before = prune_layer.tree_bytes(root)
        assert prune_layer.main(["prune_layer.py", str(root)]) == 0
        after = prune_layer.tree_bytes(root)

        assert after < before
        assert not (root / "python" / "pyarrow" / "include").exists()
        assert not (root / "python" / "pyarrow" / "libarrow_flight.so.2600").exists()
        assert not (root / "python" / "pyarrow" / "libarrow_s3.so.2600").exists()
        # The parquet read path and the catalog module survive.
        assert (root / "python" / "pyarrow" / "libparquet.so.2600").exists()
        assert (root / "python" / "variants.py").exists()
        assert "prune_layer:" in capsys.readouterr().out

    def test_refuses_a_layer_over_the_transition_ceiling(
        self, tmp_path: Path, capsys, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        root = _layer(tmp_path)
        monkeypatch.setattr(prune_layer, "LAYER_CEILING", 1_000)
        assert prune_layer.main(["prune_layer.py", str(root)]) == 1
        assert "over the" in capsys.readouterr().err

    def test_fails_on_unmatched_rules(self, tmp_path: Path, capsys) -> None:
        _write(tmp_path / "python" / "variants.py", 11_010)
        assert prune_layer.main(["prune_layer.py", str(tmp_path)]) == 1
        assert "matched nothing" in capsys.readouterr().err

    def test_ceiling_is_derived_from_the_lambda_limit(self) -> None:
        """The ceiling exists to survive the code->layer transition, not the
        steady state. Pin the derivation so it cannot drift into a magic number.
        """
        assert prune_layer.LAYER_CEILING == (
            prune_layer.LAMBDA_UNZIPPED_LIMIT - prune_layer.LEGACY_INGEST_CODE_BYTES
        )
        assert prune_layer.LAYER_CEILING == 117_588_694


# The layer prunes pyarrow's S3 filesystem (`PRUNE_GLOBS_S3`), which is only safe
# while nothing that mounts the layer reaches S3 through pyarrow. These names are
# how such code would spell it: the module, the class, the extension, or an
# `s3://` URI handed to a pyarrow reader (which resolves to S3FileSystem).
_PYARROW_S3_USE = re.compile(
    r"pyarrow\.fs|from\s+pyarrow\s+import\s+[^\n]*\bfs\b|S3FileSystem|_s3fs"
    r"|\b(?:pq|pa|pyarrow|ds)\.[\w.]+\([^)\n]*['\"]s3://"
)


class _CfnLoader(yaml.SafeLoader):
    """SafeLoader that reads CloudFormation short-form tags as plain values."""


_CfnLoader.add_multi_constructor(
    "!",
    lambda loader, _suffix, node: (
        loader.construct_scalar(node)
        if isinstance(node, yaml.ScalarNode)
        else loader.construct_sequence(node)
        if isinstance(node, yaml.SequenceNode)
        else loader.construct_mapping(node)
    ),
)


def _shared_layer_consumers() -> list[Path]:
    """CodeUri directories of every function in template.yaml that mounts SharedLayer."""
    template = yaml.load(
        (_SHARED.parent / "template.yaml").read_text(), Loader=_CfnLoader
    )
    dirs = []
    for resource in template["Resources"].values():
        props = resource.get("Properties") or {}
        if "SharedLayer" in (props.get("Layers") or []):
            dirs.append(_SHARED.parent / props["CodeUri"])
    return sorted(set(dirs))


def _pyarrow_s3_uses(root: Path) -> list[str]:
    hits = []
    for path in sorted(root.rglob("*.py")):
        for lineno, line in enumerate(path.read_text().splitlines(), 1):
            if _PYARROW_S3_USE.search(line):
                hits.append(
                    f"{path.relative_to(_SHARED.parent)}:{lineno}: {line.strip()}"
                )
    return hits


class TestS3PruneIsSafe:
    def test_consumers_are_found(self) -> None:
        """Non-vacuity: the scan below must be reading the code that imports pyarrow."""
        consumers = _shared_layer_consumers()
        assert _SHARED.parent / "ingest" in consumers
        assert (
            "import pyarrow.parquet"
            in (_SHARED.parent / "ingest" / "planner.py").read_text()
        )

    def test_no_layer_consumer_uses_pyarrow_s3(self) -> None:
        hits = [h for d in _shared_layer_consumers() for h in _pyarrow_s3_uses(d)]
        assert hits == [], (
            "shared/prune_layer.py removes pyarrow's S3 filesystem from the layer, "
            "but these lines in a function that mounts it would need it. Read the "
            "file through boto3, or drop PRUNE_GLOBS_S3 and re-check the size "
            "ceiling:\n  " + "\n  ".join(hits)
        )

    @pytest.mark.parametrize(
        "line",
        [
            "import pyarrow.fs",
            "from pyarrow import fs",
            "from pyarrow import parquet, fs",
            "fs = pyarrow.fs.S3FileSystem(region='us-east-1')",
            "table = pq.read_table('s3://bucket/key.parquet')",
        ],
    )
    def test_the_scan_recognises_s3_use(self, line: str) -> None:
        assert _PYARROW_S3_USE.search(line), line

    @pytest.mark.parametrize(
        "line",
        [
            "import pyarrow.parquet as pq",
            "table = pq.read_table(parquet_path)",
            'logger.info("Wrote rows to s3://%s/%s", bucket, key)',
        ],
    )
    def test_the_scan_ignores_local_parquet_reads(self, line: str) -> None:
        assert not _PYARROW_S3_USE.search(line), line
