# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Reading back what a child of another uid left in a directory it owns.

Render and the egress proxy each run as a user of their own, and each is handed a
directory to write its outputs into, so whatever either leaves under a name the
orchestrator reads back is that child's to choose, and the orchestrator reading it
is root. ``Path.read_text`` on such a name follows a link to any file of root's,
waits for as long as a FIFO has no writer, and reads a file of any size into
memory. So every such read goes through :func:`read` or :func:`copy`, which:

* open the directory itself with ``O_DIRECTORY | O_NOFOLLOW | O_NONBLOCK``, so a
  link in its place is refused, then open the name relative to that descriptor
  with ``O_NOFOLLOW | O_NONBLOCK | O_NOCTTY``, so nothing is resolved by path twice
  and a link, a pipe or a device under the name neither leads elsewhere nor holds
  the open;
* go on only when ``fstat`` of the opened file says it is a regular file of the
  expected uid within the caller's bound, which refuses a hard link the child made
  to another user's file and a file big enough to fill the job's memory or disk;
* read that descriptor, never the name again, and at most the size the check saw,
  so a file still growing cannot carry the read past its bound.

An absent name, or an absent directory, is ``None``, because what an output that
was never written means is the caller's to say. Anything else that is not such a
file is refused with the caller's own exception, in words naming the file and why
(:class:`Writer`), and nothing outside the directory is ever opened by name.
"""

from __future__ import annotations

import errno
import os
import stat
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import IO

_READ_CHUNK = 1024 * 1024


@dataclass(frozen=True)
class Writer:
    """The child whose outputs are read back, as a refusal names it.

    ``whose`` starts every refusal (``render's``), ``directory`` is what the
    directory it writes into is called in one (``session directory``), and
    ``error`` is the exception a refusal is raised as.
    """

    whose: str
    directory: str
    error: Callable[[str], Exception]


def _open_failure(error: OSError, *, link: bool = False) -> str:
    """Why an open was refused. ``link`` says the path is a symbolic link, which
    an ``O_DIRECTORY`` open reports as ``ENOTDIR`` rather than ``ELOOP``."""
    if link or error.errno in (errno.ELOOP, errno.EMLINK):
        return "it is a symbolic link, which is never followed"
    if error.errno == errno.ENOTDIR:
        return "it is not a directory"
    return f"it cannot be opened ({error.strerror or type(error).__name__})"


def _unfit(
    found: os.stat_result, *, writer: Writer, owner: int, limit: int
) -> str | None:
    if not stat.S_ISREG(found.st_mode):
        return "it is not a regular file"
    if found.st_uid != owner:
        return f"it belongs to uid {found.st_uid}, not to {writer.whose} uid {owner}"
    if found.st_size > limit:
        return f"it is {found.st_size} bytes, over its bound of {limit}"
    return None


def _opened(
    directory: Path, name: str, *, writer: Writer, owner: int, limit: int
) -> tuple[int, int] | None:
    """A read-only descriptor of ``name`` in ``directory`` and the size it was
    checked at, or ``None`` when there is no entry of that name.

    The module docstring says what is checked and how; each refusal is raised as
    ``writer.error``, naming the file and why.
    """
    if name in ("", ".", "..") or "/" in name:
        raise ValueError(
            f"{name!r} is not the name of a file in the {writer.directory}"
        )
    try:
        parent = os.open(
            directory,
            os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC,
        )
    except FileNotFoundError:
        return None
    except OSError as error:
        why = _open_failure(error, link=os.path.islink(directory))
        raise writer.error(
            f"{writer.whose} {writer.directory} is refused: {why}"
        ) from None
    try:
        descriptor = os.open(
            name,
            os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_NOCTTY | os.O_CLOEXEC,
            dir_fd=parent,
        )
    except FileNotFoundError:
        return None
    except OSError as error:
        raise writer.error(
            f"{writer.whose} {name} is refused: {_open_failure(error)}"
        ) from None
    finally:
        os.close(parent)
    try:
        found = os.fstat(descriptor)
    except OSError as error:
        os.close(descriptor)
        raise writer.error(
            f"{writer.whose} {name} is refused: it cannot be examined "
            f"({error.strerror or type(error).__name__})"
        ) from None
    unfit = _unfit(found, writer=writer, owner=owner, limit=limit)
    if unfit is not None:
        os.close(descriptor)
        raise writer.error(f"{writer.whose} {name} is refused: {unfit}")
    return descriptor, found.st_size


def _read_into(
    name: str,
    descriptor: int,
    size: int,
    write: Callable[[bytes], object],
    *,
    writer: Writer,
) -> int:
    """Pass at most ``size`` bytes of ``descriptor`` to ``write``; the count passed."""
    done = 0
    while done < size:
        try:
            chunk = os.read(descriptor, min(_READ_CHUNK, size - done))
        except OSError as error:
            raise writer.error(
                f"{writer.whose} {name} could not be read "
                f"({error.strerror or type(error).__name__})"
            ) from None
        if not chunk:
            break
        write(chunk)
        done += len(chunk)
    return done


def read(
    directory: Path, name: str, *, writer: Writer, owner: int, limit: int
) -> bytes | None:
    """The bytes of ``name`` in ``directory``, or ``None`` when it has none.

    Anything but a regular file of ``owner``'s directly in the directory, at most
    ``limit`` bytes, is refused as the module docstring says, and at most the size
    the check saw is read.
    """
    opened = _opened(directory, name, writer=writer, owner=owner, limit=limit)
    if opened is None:
        return None
    descriptor, size = opened
    data = bytearray()
    try:
        _read_into(name, descriptor, size, data.extend, writer=writer)
    finally:
        os.close(descriptor)
    return bytes(data)


def copy(
    directory: Path,
    name: str,
    open_target: Callable[[], IO[bytes]],
    *,
    writer: Writer,
    owner: int,
    limit: int,
) -> int | None:
    """Copy ``name`` in ``directory`` into the file ``open_target`` opens; the
    bytes copied, or ``None`` when there is no such name.

    The source is checked as :func:`read` checks it, and ``open_target`` is called
    only once it has passed, so neither a refusal nor an absent source leaves a
    target behind. The copy is read from the checked descriptor and is at most the
    size the check saw.
    """
    opened = _opened(directory, name, writer=writer, owner=owner, limit=limit)
    if opened is None:
        return None
    descriptor, size = opened
    try:
        with open_target() as target:
            return _read_into(name, descriptor, size, target.write, writer=writer)
    finally:
        os.close(descriptor)
