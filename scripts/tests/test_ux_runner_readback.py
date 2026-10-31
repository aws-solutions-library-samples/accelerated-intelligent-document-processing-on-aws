# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Unit tests for reading back another uid's files, ``ux_runner/readback.py``.

The refusals that matter most, of a link, a FIFO, an oversized file and another
uid's file, are pinned through each module that reads back this way: render's
outputs in ``test_ux_runner_recording.py`` and the egress proxy's in
``test_ux_runner_egress.py``. What is pinned here is the rest: that a refusal
names whichever child it is told to and is raised as that child's own exception,
and that an open, an ``fstat`` or a read the kernel fails is a refusal too, with
every descriptor closed, rather than an exception of its own. Those failures are
injected through ``readback``'s ``os``, because a test that may run as root cannot
make the real ones happen.
"""

from __future__ import annotations

import errno
import os
import sys
from pathlib import Path
from typing import Any

import pytest

SCRIPTS = Path(__file__).resolve().parents[1]
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

from ux_runner import readback  # noqa: E402


class Refused(Exception):
    """The exception the child in these tests has its refusals raised as."""


WRITER = readback.Writer("the tool's", "work directory", Refused)
NAME = "out.txt"


class FailingOs:
    """``readback``'s ``os``, failing one kind of call and recording descriptors.

    ``call`` is ``open-directory``, ``open-name``, ``fstat`` or ``read``; every
    other call, and every call of the other kinds, is the real one.
    """

    def __init__(self, call: str, error: OSError) -> None:
        self.call = call
        self.error = error
        self.opened: list[int] = []
        self.closed: list[int] = []

    def __getattr__(self, name: str) -> Any:
        return getattr(os, name)

    def open(
        self, path: Any, flags: int, mode: int = 0o777, *, dir_fd: int | None = None
    ) -> int:
        kind = "open-directory" if dir_fd is None else "open-name"
        if self.call == kind:
            raise self.error
        descriptor = os.open(path, flags, mode, dir_fd=dir_fd)
        self.opened.append(descriptor)
        return descriptor

    def close(self, descriptor: int) -> None:
        self.closed.append(descriptor)
        os.close(descriptor)

    def fstat(self, descriptor: int) -> os.stat_result:
        if self.call == "fstat":
            raise self.error
        return os.fstat(descriptor)

    def read(self, descriptor: int, length: int) -> bytes:
        if self.call == "read":
            raise self.error
        return os.read(descriptor, length)


def _directory(tmp_path: Path, content: bytes = b"written") -> Path:
    directory = tmp_path / "work"
    directory.mkdir()
    (directory / NAME).write_bytes(content)
    return directory


@pytest.mark.unit
def test_a_refusal_names_the_child_it_is_told_to_and_raises_its_exception(
    tmp_path: Path,
) -> None:
    directory = tmp_path / "work"
    directory.mkdir()
    os.mkfifo(directory / NAME)
    with pytest.raises(Refused) as raised:
        readback.read(directory, NAME, writer=WRITER, owner=os.geteuid(), limit=16)
    assert (
        str(raised.value) == "the tool's out.txt is refused: it is not a regular file"
    )
    link = tmp_path / "linked"
    link.symlink_to(directory, target_is_directory=True)
    with pytest.raises(Refused) as raised:
        readback.read(link, NAME, writer=WRITER, owner=os.geteuid(), limit=16)
    assert str(raised.value) == (
        "the tool's work directory is refused: it is a symbolic link, which is "
        "never followed"
    )
    with pytest.raises(ValueError, match="not the name of a file in the work dir"):
        readback.read(directory, "../out.txt", writer=WRITER, owner=0, limit=16)


@pytest.mark.unit
@pytest.mark.parametrize(
    ("call", "message"),
    [
        (
            "open-directory",
            "the tool's work directory is refused: it cannot be opened "
            "(Permission denied)",
        ),
        (
            "open-name",
            "the tool's out.txt is refused: it cannot be opened (Permission denied)",
        ),
        (
            "fstat",
            "the tool's out.txt is refused: it cannot be examined (Input/output error)",
        ),
        ("read", "the tool's out.txt could not be read (Input/output error)"),
    ],
)
def test_a_call_the_kernel_fails_is_a_refusal_and_leaves_nothing_open(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, call: str, message: str
) -> None:
    code = errno.EACCES if call.startswith("open") else errno.EIO
    failing = FailingOs(call, OSError(code, os.strerror(code)))
    monkeypatch.setattr(readback, "os", failing)
    directory = _directory(tmp_path)
    with pytest.raises(Refused) as raised:
        readback.read(directory, NAME, writer=WRITER, owner=os.geteuid(), limit=16)
    assert str(raised.value) == message
    assert sorted(failing.closed) == sorted(failing.opened)


@pytest.mark.unit
def test_a_file_that_shrank_after_its_check_is_read_as_far_as_it_goes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A child left behind can still be truncating the file, so the read stops
    at its end rather than waiting for the size the check saw."""

    class CheckedLarger(FailingOs):
        def fstat(self, descriptor: int) -> os.stat_result:
            found = tuple(os.fstat(descriptor))
            return os.stat_result((*found[:6], 64, *found[7:10]))

    monkeypatch.setattr(readback, "os", CheckedLarger("none", OSError()))
    directory = _directory(tmp_path)
    owner = os.geteuid()
    assert (
        readback.read(directory, NAME, writer=WRITER, owner=owner, limit=64)
        == b"written"
    )
    target = tmp_path / "copied.txt"
    copied = readback.copy(
        directory,
        NAME,
        lambda: target.open("wb"),
        writer=WRITER,
        owner=owner,
        limit=64,
    )
    assert (copied, target.read_bytes()) == (7, b"written")
