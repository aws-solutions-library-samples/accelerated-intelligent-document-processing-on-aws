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
import struct
from pathlib import Path

import pytest

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


def _layer(root: Path, *, flight_needed_by_lib: bool = False) -> Path:
    """A miniature stand-in for the built layer, matching pyarrow's real layout."""
    pa = root / "python" / "pyarrow"
    _write(root / "python" / "variants.py", 11_010)

    # Prunable: build-time-only trees, Cython sources, C API headers.
    _write(pa / "include" / "arrow" / "api.h", 4_000)
    _write(pa / "src" / "arrow" / "python" / "pyarrow.h", 1_000)
    _write(pa / "tests" / "test_table.py", 2_000)
    _write(pa / "table.pxi", 1_500)
    _write(pa / "lib.pyx", 1_500)
    _write(pa / "includes" / "libarrow.pxd", 1_500)
    _write(pa / "lib.h", 500)

    # Prunable: the Flight RPC stack.
    _write(pa / "libarrow_flight.so.2300", 24_000)
    _write(pa / "libarrow_python_flight.so", 200)
    _write(pa / "_flight.cpython-312-aarch64-linux-gnu.so", 1_000)

    # Kept: the parquet read path. `lib` links the libraries that must survive.
    lib_needs = ["libarrow.so.2300", "libparquet.so.2300"]
    if flight_needed_by_lib:
        lib_needs.append("libarrow_flight.so.2300")
    (pa / "lib.cpython-312-aarch64-linux-gnu.so").write_bytes(_elf64(lib_needs))
    _write(pa / "libarrow.so.2300", 45_000)
    _write(pa / "libparquet.so.2300", 11_000)
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
        assert "libarrow_flight.so.2300" in names
        assert "table.pxi" in names
        assert "libarrow.so.2300" not in names  # load-bearing, must survive

    def test_reports_rules_that_match_nothing(self, tmp_path: Path) -> None:
        """A pyarrow layout change must surface as unmatched rules, not silence."""
        _write(tmp_path / "python" / "variants.py", 11_010)
        _, unmatched = prune_layer.collect(tmp_path)
        assert len(unmatched) == len(
            prune_layer.PRUNE_DIRS
            + prune_layer.PRUNE_GLOBS
            + prune_layer.PRUNE_GLOBS_FLIGHT
        )


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
        assert "libarrow_flight.so.2300" in problems[0]


class TestMain:
    def test_prunes_and_reports(self, tmp_path: Path, capsys) -> None:
        root = _layer(tmp_path)
        before = prune_layer.tree_bytes(root)
        assert prune_layer.main(["prune_layer.py", str(root)]) == 0
        after = prune_layer.tree_bytes(root)

        assert after < before
        assert not (root / "python" / "pyarrow" / "include").exists()
        assert not (root / "python" / "pyarrow" / "libarrow_flight.so.2300").exists()
        # The parquet read path and the catalog module survive.
        assert (root / "python" / "pyarrow" / "libparquet.so.2300").exists()
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
