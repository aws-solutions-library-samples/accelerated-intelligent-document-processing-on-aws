# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Unit tests for ``idp_sdk._core.async_utils``.

The module under test is a single four-line helper, ``run_async(coro)``. It
creates a brand new event loop, drives one coroutine to completion on it, and
closes the loop in a ``finally`` block. It exists so that synchronous SDK entry
points can call the handful of coroutine-based internals without each call site
having to manage a loop.

What shaped these tests is that all of the helper's behaviour is in what it does
to the *interpreter's* loop state rather than in its return value, and that state
is invisible unless a test looks for it. Three properties matter to a caller and
none of them is expressed in the return value:

* the loop is always closed, including when the coroutine raises — otherwise a
  long-lived process accumulates one unclosed loop (and its selector file
  descriptor) per call, and fails on file-descriptor exhaustion somewhere else
  entirely;
* every call gets its own loop rather than reusing a cached one, so a coroutine
  that leaves a loop in a bad state cannot poison the next call;
* the helper does **not** install its loop as the thread's current event loop, so
  it does not disturb a caller that is managing a loop of its own.

The tests therefore capture the loop object by wrapping ``asyncio.new_event_loop``
and assert against the loop, not against ``run_async``'s result alone.

``run_async`` has **no branch** for being called from inside an already-running
event loop; the last test records what actually happens in that case so the
absence is documented rather than assumed benign.
"""

import asyncio

import pytest

from idp_sdk._core.async_utils import run_async


@pytest.fixture
def captured_loops(monkeypatch):
    """Record every loop ``asyncio.new_event_loop`` hands out during a test.

    ``run_async`` never returns or exposes the loop it used, so wrapping the
    factory is the only way to assert anything about it.
    """
    created = []
    real_new_event_loop = asyncio.new_event_loop

    def recording_new_event_loop():
        loop = real_new_event_loop()
        created.append(loop)
        return loop

    monkeypatch.setattr(asyncio, "new_event_loop", recording_new_event_loop)
    return created


@pytest.mark.unit
class TestRunAsync:
    """Behaviour of the synchronous-to-async bridge."""

    def test_returns_the_coroutines_value(self):
        async def produce():
            await asyncio.sleep(0)
            return {"documents": 3}

        assert run_async(produce()) == {"documents": 3}

    def test_runs_the_coroutine_on_the_loop_it_created(self, captured_loops):
        """The coroutine must execute on the freshly created loop.

        A failure here would mean ``run_async`` is driving the coroutine on some
        other loop — most likely a cached global one — which is the situation the
        new-loop-per-call design exists to avoid.
        """
        seen = {}

        async def record_loop():
            seen["loop"] = asyncio.get_running_loop()

        run_async(record_loop())

        assert len(captured_loops) == 1
        assert seen["loop"] is captured_loops[0]

    def test_closes_the_loop_on_success(self, captured_loops):
        async def produce():
            return 1

        run_async(produce())

        assert captured_loops[0].is_closed()

    def test_closes_the_loop_when_the_coroutine_raises(self, captured_loops):
        """The exception must propagate *and* the loop must still be closed.

        This is the whole point of the ``finally``: a caller that retries a
        failing operation would otherwise leak one event loop and one selector
        file descriptor per attempt, and the eventual failure would surface far
        from here as an ``OSError: too many open files``.
        """

        class Boom(RuntimeError):
            pass

        async def explode():
            raise Boom("extraction failed")

        with pytest.raises(Boom, match="extraction failed"):
            run_async(explode())

        assert len(captured_loops) == 1
        assert captured_loops[0].is_closed()

    def test_each_call_gets_its_own_loop(self, captured_loops):
        async def produce(value):
            return value

        assert run_async(produce("a")) == "a"
        assert run_async(produce("b")) == "b"

        assert len(captured_loops) == 2
        first, second = captured_loops
        assert first is not second
        assert first.is_closed() and second.is_closed()

    def test_does_not_install_its_loop_as_the_threads_current_loop(
        self, captured_loops
    ):
        """``run_async`` must leave the thread's event-loop slot alone.

        It calls ``new_event_loop`` but never ``set_event_loop``. A caller that
        has its own loop registered for the thread would find it silently
        replaced by a closed one if that changed, so this pins the omission as
        deliberate. Asserted through ``asyncio.set_event_loop``, because whether
        ``get_event_loop`` raises or auto-creates on an empty slot differs
        between Python versions.
        """
        installed = []
        real_set_event_loop = asyncio.set_event_loop

        def recording_set_event_loop(loop):
            installed.append(loop)
            return real_set_event_loop(loop)

        with pytest.MonkeyPatch.context() as patch:
            patch.setattr(asyncio, "set_event_loop", recording_set_event_loop)

            async def produce():
                return None

            run_async(produce())

        assert installed == []

    def test_called_from_inside_a_running_loop_it_raises(self):
        """There is no already-running-loop branch, and this is what that costs.

        ``run_async`` unconditionally builds a second loop and calls
        ``run_until_complete`` on it, which asyncio refuses while another loop is
        running in the same thread. So a coroutine-based SDK internal that
        reaches ``run_async`` from inside an async caller fails with an asyncio
        ``RuntimeError`` about event loops rather than anything describing the
        operation. Pinned rather than fixed: the fix belongs in production code.
        """
        captured = {}

        async def outer():
            inner = self._never_awaited()
            try:
                run_async(inner)
            except RuntimeError as exc:
                captured["error"] = str(exc)
            finally:
                # The coroutine never ran, so close it explicitly instead of
                # leaving a "coroutine was never awaited" warning behind.
                inner.close()

        asyncio.run(outer())

        assert "another loop is running" in captured["error"]

    @staticmethod
    async def _never_awaited():
        return "unreachable"
