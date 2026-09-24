# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""
Tests for `idp_cli.chat`, the body of `idp-cli chat`.

The module has three parts and each is tested against a different question.

`run_chat` builds an `IDPClient`, initialises the agent orchestrator, and then
either runs one prompt or loops reading from the terminal. Its riskiest line is
`logging.disable(logging.CRITICAL)`: it silences every logger in the process, and
the only thing that undoes it is the `finally` at the end of the function. If that
restore is ever skipped, a caller that imported this module keeps a silent logging
subsystem for the rest of the process -- a failure that shows up much later as
missing logs rather than as an error here -- so the restore is asserted both on the
success path and when the processor raises. The REPL's exits are also asserted one
by one, because a loop that fails to break on `/quit` or on Ctrl+C cannot be left
by the user at all.

`_handle_prompt` owns event-loop lifetime: it reuses the REPL's loop when given one
and creates and closes its own otherwise, and it must turn a streaming exception
into a printed line rather than a traceback that ends the session.

`_stream_response` is the only part with real text-processing logic. It accumulates
the streamed text, strips `<thinking>...</thinking>` from the accumulation on every
chunk, and prints whatever the strip newly revealed beyond what it has already
shown. That works when a thinking block arrives whole inside one chunk; when the
block is split across chunks it does not, and the two tests named
`..._defect_...` pin what happens instead.

Nothing here touches AWS or the network. `idp_sdk.IDPClient` is replaced on the
source module, since `run_chat` imports it inside the function body, and
`idp_common.agents.factory` is replaced in `sys.modules` for the same reason and
one more: importing the real module registers every agent and reaches Secrets
Manager for the external-MCP credentials, which is both slow and exactly the kind
of call the `no_outbound_http` fixture exists to refuse.
"""

import asyncio
import contextlib
import logging
import sys
import types
from types import SimpleNamespace

import pytest

from idp_cli import chat


class RecordingConsole:
    """
    A stand-in for `rich.console.Console` that records prints and scripts input.

    The printed strings are recorded before Rich would resolve the markup in them,
    which is what these tests want: `_stream_response` prints partial text with
    `end=""`, so the sequence of print calls -- and not a rendered blob -- is where
    "printed once", "never printed" and "printed in this order" are visible.
    """

    def __init__(self, replies=()):
        self.printed: list[str] = []
        self.prompts: list[str] = []
        self._replies = list(replies)

    def print(self, *args, **kwargs) -> None:
        self.printed.append("" if not args else str(args[0]))

    def status(self, *args, **kwargs):
        """`run_chat` uses `console.status(...)` as a context manager."""
        return contextlib.nullcontext()

    def input(self, prompt: str = "", **kwargs) -> str:
        """
        Return the next scripted reply, or raise it if it is an exception class.

        Running out raises EOFError rather than blocking: a test that forgets to
        script a way out of the REPL then fails instead of hanging the suite.
        """
        self.prompts.append(prompt)
        if not self._replies:
            raise EOFError("scripted console ran out of replies")
        reply = self._replies.pop(0)
        if isinstance(reply, str):
            return reply
        # Anything else is an exception class the test scripted in place of a line
        # of input: EOFError for Ctrl+D, KeyboardInterrupt for Ctrl+C.
        raise reply()

    @property
    def text(self) -> str:
        return "".join(self.printed)


class FakeOrchestrator:
    """An orchestrator whose `stream_async` replays a scripted list of events."""

    def __init__(self, events):
        self.events = list(events)
        self.prompts: list[str] = []

    def stream_async(self, prompt: str):
        self.prompts.append(prompt)

        async def _generate():
            for event in self.events:
                yield event

        return _generate()


class ExplodingOrchestrator:
    """An orchestrator whose stream raises part-way through."""

    def __init__(self, message="stream broke"):
        self.message = message
        self.prompts: list[str] = []

    def stream_async(self, prompt: str):
        self.prompts.append(prompt)

        async def _generate():
            yield {"data": "partial"}
            raise RuntimeError(self.message)

        return _generate()


class FakeProcessor:
    """The chat processor `IDPClient.chat._get_processor()` returns."""

    def __init__(self, orchestrator=None, setup_error=None):
        self.calls: list[str] = []
        self.session_ids: list[str] = []
        self.code_intelligence_flags: list[bool] = []
        self.disable_level_during_setup: int | None = None
        self._orchestrator = (
            orchestrator if orchestrator is not None else (FakeOrchestrator([]))
        )
        self._setup_error = setup_error

    def _setup_env(self) -> None:
        self.calls.append("setup_env")
        # Recorded so a test can prove logging really was off *during* the
        # session, not merely restored after it.
        self.disable_level_during_setup = logging.root.manager.disable
        if self._setup_error is not None:
            raise self._setup_error

    def _ensure_orchestrator(
        self, session_id: str, enable_code_intelligence: bool = False
    ) -> None:
        self.calls.append("ensure_orchestrator")
        self.session_ids.append(session_id)
        self.code_intelligence_flags.append(enable_code_intelligence)


@pytest.fixture(autouse=True)
def restore_global_logging():
    """
    Undo `logging.disable` after every test in this module.

    `run_chat` disables logging process-wide, and a test that asserts the restore
    asserts it in its own body -- this is the safety net for a test that fails
    before reaching that assertion, so one red test does not silence the logging
    of every test that follows it.
    """
    yield
    logging.disable(logging.NOTSET)


@pytest.fixture
def recording_console(monkeypatch):
    """Pin `chat.console`, which has no conftest fixture covering it."""

    def _install(replies=()):
        console = RecordingConsole(replies)
        monkeypatch.setattr(chat, "console", console)
        return console

    return _install


@pytest.fixture
def stub_agent_factory(monkeypatch):
    """
    Put a stub `idp_common.agents.factory` in `sys.modules`.

    `run_chat` does `from idp_common.agents.factory import agent_factory` inside
    its body, and an entry already in `sys.modules` short-circuits the import
    machinery, so the real module is never loaded and never registers an agent or
    fetches a secret.
    """
    module = types.ModuleType("idp_common.agents.factory")
    module.agent_factory = SimpleNamespace(
        list_available_agents=lambda: [
            {"agent_name": "Analytics Agent"},
            {"agent_name": "Error Analyzer Agent"},
        ]
    )
    monkeypatch.setitem(sys.modules, "idp_common.agents.factory", module)
    return module.agent_factory


@pytest.fixture
def fake_idp_client(monkeypatch):
    """Replace `idp_sdk.IDPClient` and hand back what the command did with it."""
    import idp_sdk

    def _install(processor=None):
        processor = processor if processor is not None else FakeProcessor()
        built: list[dict] = []

        class _Client:
            def __init__(self, stack_name=None, region=None):
                built.append({"stack_name": stack_name, "region": region})
                self.chat = SimpleNamespace(_get_processor=lambda: processor)

        monkeypatch.setattr(idp_sdk, "IDPClient", _Client)
        return SimpleNamespace(processor=processor, built=built)

    return _install


@pytest.fixture
def created_loops(monkeypatch):
    """
    Record every event loop `asyncio.new_event_loop()` hands out.

    Loop lifetime is the contract being checked -- the REPL creates one loop for
    the whole session and closes it, and one-shot mode creates and closes its own
    -- and a leaked open loop is only visible by holding onto the objects.
    """
    loops: list[asyncio.AbstractEventLoop] = []
    real_new_event_loop = asyncio.new_event_loop

    def _record():
        loop = real_new_event_loop()
        loops.append(loop)
        return loop

    monkeypatch.setattr(asyncio, "new_event_loop", _record)
    yield loops
    for loop in loops:
        if not loop.is_closed():
            loop.close()


def _stream(console_replies_unused=None, *, orchestrator, prompt="hello") -> str:
    """Drive `_stream_response` to completion and return what it reports shown."""
    return asyncio.run(chat._stream_response(orchestrator, prompt))


# ---------------------------------------------------------------------------
# _stream_response
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_stream_prints_only_the_newly_revealed_suffix_of_each_chunk(recording_console):
    """
    Each chunk must print only the text the previous chunk did not.

    The function re-derives the whole cleaned answer on every chunk, so printing
    `clean` rather than the suffix would repeat the entire response once per chunk.
    """
    console = recording_console()
    orchestrator = FakeOrchestrator(
        [{"data": "Hello"}, {"data": " there"}, {"data": " operator"}]
    )

    shown = _stream(orchestrator=orchestrator, prompt="hi")

    assert orchestrator.prompts == ["hi"]
    assert console.printed[:3] == ["Hello", " there", " operator"]
    assert shown == "Hello there operator"


@pytest.mark.unit
def test_a_thinking_block_contained_in_one_chunk_is_never_printed(recording_console):
    console = recording_console()

    shown = _stream(
        orchestrator=FakeOrchestrator(
            [{"data": "<thinking>check the schema first</thinking>Answer: 42"}]
        )
    )

    assert "check the schema" not in console.text
    assert console.printed[0] == "Answer: 42"
    assert shown == "Answer: 42"


@pytest.mark.unit
def test_a_thinking_only_chunk_prints_nothing_and_the_next_chunk_prints_normally(
    recording_console,
):
    """
    The first chunk cleans to the empty string, which must not count as output.

    `len(clean) > len(displayed)` is what suppresses it; an unconditional print
    would emit a blank line before every answer.
    """
    console = recording_console()

    shown = _stream(
        orchestrator=FakeOrchestrator(
            [{"data": "<thinking>plan</thinking>"}, {"data": "Answer: 42"}]
        )
    )

    assert console.printed[0] == "Answer: 42"
    assert shown == "Answer: 42"


@pytest.mark.unit
def test_a_thinking_block_split_across_chunks_leaks_and_hides_the_answer_defect_1(
    recording_console,
):
    """
    DEFECT (pinned, not fixed): a `<thinking>` block split across two chunks is
    printed to the terminal, and the real answer that follows it is never printed.

    The strip runs against the text accumulated *so far*, so a chunk ending inside
    a thinking block has no closing tag to match and the raw block -- the model's
    private reasoning, including the tags -- goes straight to the user's terminal.
    Worse, `displayed` is then set to the length of that leaked text, and once the
    next chunk completes the block the cleaned answer is *shorter*, so the
    `len(clean) > len(displayed)` guard suppresses it. The user is shown the
    reasoning and never shown the answer. Streaming is chunked by the service, so
    which of these two paths a given response takes is not under the caller's
    control.
    """
    console = recording_console()

    shown = _stream(
        orchestrator=FakeOrchestrator(
            [
                {"data": "<thinking>I must check the schema</"},
                {"data": "thinking>Answer: 42"},
            ]
        )
    )

    assert console.printed[0] == "<thinking>I must check the schema</"
    assert "Answer: 42" not in console.text
    assert shown == "<thinking>I must check the schema</"


@pytest.mark.unit
def test_a_thinking_block_split_across_chunks_truncates_a_long_answer_defect_2(
    recording_console,
):
    """
    DEFECT (pinned, not fixed): the same split also mangles an answer long enough
    to get past the guard -- it is printed from the middle.

    `displayed` holds the length of the leaked thinking text, so the suffix that
    gets printed is `clean[len(leak):]`: the first characters of the genuine answer
    are silently dropped, and the number dropped is the length of the leak. This is
    the quieter half of the defect above, because the output looks like an answer.
    """
    console = recording_console()
    leak = "<thinking>short</"
    answer = "A: " + "x" * 60

    shown = _stream(
        orchestrator=FakeOrchestrator([{"data": leak}, {"data": "thinking>" + answer}])
    )

    assert console.printed[0] == leak
    assert console.printed[1] == answer[len(leak) :]
    assert not console.text.startswith(answer[:3])
    assert shown == answer


@pytest.mark.unit
def test_a_subagent_handoff_is_announced_once_for_a_repeated_tool_name(
    recording_console,
):
    """
    The orchestrator emits a `current_tool_use` event per streamed token of the
    tool call, so announcing every one of them would fill the terminal with the
    same line. Only a change of name may announce.
    """
    console = recording_console()

    _stream(
        orchestrator=FakeOrchestrator(
            [
                {"current_tool_use": {"name": "analytics_agent"}},
                {"current_tool_use": {"name": "analytics_agent"}},
                {"data": "42 documents"},
            ]
        )
    )

    announcements = [line for line in console.printed if "⟶" in line]
    assert announcements == ["\n[dim]⟶ Analytics[/dim]"]


@pytest.mark.unit
@pytest.mark.parametrize(
    ("tool_name", "expected"),
    [
        ("analytics_agent", "Analytics"),
        ("error_analyzer_agent", "Error Analyzer"),
        ("code_intelligence_agent", "Code Intelligence"),
        ("plain_tool", "Plain Tool"),
    ],
)
def test_the_announced_name_drops_the_agent_suffix_and_title_cases_the_rest(
    tool_name, expected, recording_console
):
    console = recording_console()

    _stream(orchestrator=FakeOrchestrator([{"current_tool_use": {"name": tool_name}}]))

    assert f"\n[dim]⟶ {expected}[/dim]" in console.printed


@pytest.mark.unit
def test_returning_to_an_earlier_subagent_announces_it_again(recording_console):
    """
    Only the immediately previous name is remembered, not the set of names seen.

    An orchestrator that hands off to analytics, then to the error analyzer, then
    back to analytics announces analytics twice -- which is the useful behaviour
    here, since the second handoff really is a new one.
    """
    console = recording_console()

    _stream(
        orchestrator=FakeOrchestrator(
            [
                {"current_tool_use": {"name": "analytics_agent"}},
                {"current_tool_use": {"name": "error_analyzer_agent"}},
                {"current_tool_use": {"name": "analytics_agent"}},
            ]
        )
    )

    assert [line for line in console.printed if "⟶" in line] == [
        "\n[dim]⟶ Analytics[/dim]",
        "\n[dim]⟶ Error Analyzer[/dim]",
        "\n[dim]⟶ Analytics[/dim]",
    ]


@pytest.mark.unit
def test_a_tool_event_with_no_name_yet_announces_nothing(recording_console):
    """
    The first `current_tool_use` event of a call carries an empty name while the
    tool block is still streaming, so the empty check is what stops a bare arrow
    appearing before the agent is known.
    """
    console = recording_console()

    _stream(
        orchestrator=FakeOrchestrator(
            [{"current_tool_use": {}}, {"current_tool_use": {"name": ""}}]
        )
    )

    assert not [line for line in console.printed if "⟶" in line]


@pytest.mark.unit
def test_an_event_that_is_neither_text_nor_a_tool_call_is_ignored(recording_console):
    """Lifecycle events (`init_event_loop`, `message`, ...) share the stream."""
    console = recording_console()

    shown = _stream(
        orchestrator=FakeOrchestrator(
            [{"init_event_loop": True}, {"message": {"role": "assistant"}}]
        )
    )

    assert shown == ""
    assert console.printed == [""]  # only the trailing newline print


@pytest.mark.unit
def test_the_stream_ends_with_a_newline_print(recording_console):
    """
    Chunks are printed with `end=""`, so without the final unconditional print the
    shell prompt would resume on the same line as the answer.
    """
    console = recording_console()

    _stream(orchestrator=FakeOrchestrator([{"data": "done"}]))

    assert console.printed[-1] == ""


# ---------------------------------------------------------------------------
# _handle_prompt
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_handle_prompt_without_a_loop_creates_one_and_closes_it(
    recording_console, created_loops
):
    console = recording_console()
    orchestrator = FakeOrchestrator([{"data": "Answer: 42"}])

    chat._handle_prompt(orchestrator, "how many?")

    assert orchestrator.prompts == ["how many?"]
    assert "Answer: 42" in console.text
    assert len(created_loops) == 1
    assert created_loops[0].is_closed(), "one-shot mode must not leak its loop"


@pytest.mark.unit
def test_handle_prompt_reuses_a_supplied_loop_and_leaves_it_open(
    recording_console, created_loops
):
    """
    The REPL owns its loop for the whole session, so `_handle_prompt` must not
    close it -- closing it would make the second prompt of the session fail.
    """
    recording_console()
    loop = asyncio.new_event_loop()
    orchestrator = FakeOrchestrator([{"data": "first"}])
    try:
        chat._handle_prompt(orchestrator, "one", loop=loop)
        chat._handle_prompt(orchestrator, "two", loop=loop)

        assert orchestrator.prompts == ["one", "two"]
        assert not loop.is_closed()
        assert len(created_loops) == 1  # the one this test made, none of its own
    finally:
        loop.close()


@pytest.mark.unit
@pytest.mark.parametrize("supply_loop", [False, True])
def test_handle_prompt_prints_a_streaming_error_instead_of_raising(
    supply_loop, recording_console, created_loops
):
    """
    An exception from the agent must not end the session.

    In the REPL this is the difference between one failed question and losing the
    whole conversation, and the same guard has to hold on the one-shot path, where
    it decides whether the user sees a message or a traceback.
    """
    console = recording_console()
    orchestrator = ExplodingOrchestrator("bedrock throttled")
    loop = asyncio.new_event_loop() if supply_loop else None
    try:
        chat._handle_prompt(orchestrator, "boom", loop=loop)
    finally:
        if loop is not None:
            loop.close()

    assert "[red]Error: bedrock throttled[/red]" in console.printed


@pytest.mark.unit
def test_handle_prompt_closes_its_own_loop_even_when_the_stream_raises(
    recording_console, created_loops
):
    """The `finally` is what makes a failing one-shot prompt not leak a loop."""
    recording_console()

    chat._handle_prompt(ExplodingOrchestrator(), "boom")

    assert len(created_loops) == 1
    assert created_loops[0].is_closed()


# ---------------------------------------------------------------------------
# run_chat -- single shot
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_single_shot_runs_the_prompt_once_and_never_reads_from_the_terminal(
    recording_console, fake_idp_client, stub_agent_factory, created_loops
):
    """
    `--prompt` is the non-interactive form, used from scripts and from CI.

    If it fell through into the REPL it would block on stdin forever in a context
    with no terminal, so "did not prompt" is as much of the contract as "ran the
    prompt".
    """
    console = recording_console()
    orchestrator = FakeOrchestrator([{"data": "42 documents"}])
    client = fake_idp_client(FakeProcessor(orchestrator=orchestrator))

    chat.run_chat(stack_name="my-stack", region="us-west-2", prompt="how many?")

    assert orchestrator.prompts == ["how many?"]
    assert console.prompts == []
    assert "42 documents" in console.text
    assert client.processor.calls == ["setup_env", "ensure_orchestrator"]
    # Only the loop _handle_prompt made for the single prompt; no REPL loop.
    assert len(created_loops) == 1


@pytest.mark.unit
def test_the_client_is_built_from_the_stack_name_and_region_it_was_given(
    recording_console, fake_idp_client, stub_agent_factory
):
    console = recording_console()
    client = fake_idp_client()

    chat.run_chat(stack_name="my-stack", region="eu-west-1", prompt="hi")

    assert client.built == [{"stack_name": "my-stack", "region": "eu-west-1"}]
    assert "Stack: my-stack" in console.text


@pytest.mark.unit
def test_the_session_id_is_prefixed_and_distinct_per_run(
    recording_console, fake_idp_client, stub_agent_factory
):
    """
    The session id keys the orchestrator's conversation state, so two runs must
    not share one; the `cli-` prefix is what distinguishes a terminal session from
    a web one in the same store.
    """
    recording_console()
    first = fake_idp_client()
    chat.run_chat(stack_name="s", prompt="hi")
    recording_console()
    second = fake_idp_client()
    chat.run_chat(stack_name="s", prompt="hi")

    (id_one,) = first.processor.session_ids
    (id_two,) = second.processor.session_ids
    assert id_one.startswith("cli-")
    assert len(id_one) == len("cli-") + 12
    assert int(id_one.removeprefix("cli-"), 16) >= 0  # hex, as uuid4().hex promises
    assert id_one != id_two


@pytest.mark.unit
@pytest.mark.parametrize("enabled", [False, True])
def test_the_code_intelligence_flag_reaches_the_orchestrator(
    enabled, recording_console, fake_idp_client, stub_agent_factory
):
    """
    This flag turns on an agent that talks to an external third-party MCP server,
    so it must never be enabled by anything other than the explicit request.
    """
    recording_console()
    client = fake_idp_client()

    chat.run_chat(stack_name="s", prompt="hi", enable_code_intelligence=enabled)

    assert client.processor.code_intelligence_flags == [enabled]


@pytest.mark.unit
def test_the_available_agents_are_listed_before_the_first_prompt(
    recording_console, fake_idp_client, stub_agent_factory
):
    console = recording_console()
    fake_idp_client()

    chat.run_chat(stack_name="s", prompt="hi")

    assert "Analytics Agent" in console.text
    assert "Error Analyzer Agent" in console.text
    assert "Type /quit to exit" in console.text


# ---------------------------------------------------------------------------
# run_chat -- the REPL
# ---------------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.parametrize(
    "word", ["/quit", "/exit", "quit", "exit", "QUIT", " /quit ", "Exit"]
)
def test_the_repl_exits_on_every_spelling_of_quit(
    word, recording_console, fake_idp_client, stub_agent_factory, monkeypatch
):
    """
    All four words, case-insensitively and with surrounding whitespace.

    The input is stripped and lowercased before the comparison; a user who types
    `Exit` and is not let out has no other way to leave short of Ctrl+C.
    """
    console = recording_console([word])
    fake_idp_client()
    handled: list[str] = []
    monkeypatch.setattr(
        chat, "_handle_prompt", lambda *args, **kwargs: handled.append(args[1])
    )

    chat.run_chat(stack_name="s")

    assert handled == []
    assert console.prompts == ["[bold cyan]You:[/bold cyan] "]
    assert "[dim]Goodbye.[/dim]" in console.printed


@pytest.mark.unit
@pytest.mark.parametrize("interrupt", [EOFError, KeyboardInterrupt])
def test_the_repl_exits_on_eof_and_on_ctrl_c(
    interrupt, recording_console, fake_idp_client, stub_agent_factory, monkeypatch
):
    """
    Ctrl+D and Ctrl+C must both end the session cleanly rather than raise.

    `KeyboardInterrupt` is not an `Exception`, so a bare `except Exception` here
    would let it escape as a traceback; that it is named explicitly is the thing
    being pinned.
    """
    console = recording_console([interrupt])
    fake_idp_client()
    monkeypatch.setattr(chat, "_handle_prompt", lambda *args, **kwargs: None)

    chat.run_chat(stack_name="s")

    assert "\n[dim]Goodbye.[/dim]" in console.printed


@pytest.mark.unit
def test_the_repl_skips_a_blank_line_without_calling_the_orchestrator(
    recording_console, fake_idp_client, stub_agent_factory, monkeypatch
):
    """
    A bare Enter, or a line of spaces, is not a question.

    Sending it on would spend a model invocation -- and real money -- on an empty
    prompt, so the skip is asserted by the orchestrator never being reached while
    the console is still read three times.
    """
    console = recording_console(["", "   ", "\t", "/quit"])
    fake_idp_client()
    handled: list[str] = []
    monkeypatch.setattr(
        chat, "_handle_prompt", lambda *args, **kwargs: handled.append(args[1])
    )

    chat.run_chat(stack_name="s")

    assert handled == []
    assert len(console.prompts) == 4


@pytest.mark.unit
def test_the_repl_forwards_each_prompt_stripped_and_reuses_one_loop(
    recording_console, fake_idp_client, stub_agent_factory, monkeypatch, created_loops
):
    """
    One loop for the session, and it is closed on the way out.

    Creating a loop per prompt leaks a file descriptor each time; not closing the
    session loop leaks one per `idp-cli chat` invocation. The same loop object must
    reach every `_handle_prompt` call and must be closed once the loop ends.
    """
    recording_console(["  how many?  ", "and yesterday?", "/quit"])
    fake_idp_client()
    seen: list[tuple[str, object]] = []

    def _record(orchestrator, prompt, loop=None):
        seen.append((prompt, loop))

    monkeypatch.setattr(chat, "_handle_prompt", _record)

    chat.run_chat(stack_name="s")

    assert [prompt for prompt, _ in seen] == ["how many?", "and yesterday?"]
    assert seen[0][1] is seen[1][1]
    assert len(created_loops) == 1
    assert created_loops[0].is_closed()


@pytest.mark.unit
def test_the_repl_passes_the_orchestrator_the_processor_built(
    recording_console, fake_idp_client, stub_agent_factory, monkeypatch
):
    recording_console(["question", "/quit"])
    orchestrator = FakeOrchestrator([])
    client = fake_idp_client(FakeProcessor(orchestrator=orchestrator))
    seen: list[object] = []
    monkeypatch.setattr(
        chat, "_handle_prompt", lambda orch, prompt, loop=None: seen.append(orch)
    )

    chat.run_chat(stack_name="s")

    assert seen == [client.processor._orchestrator]
    assert seen == [orchestrator]


# ---------------------------------------------------------------------------
# run_chat -- the logging switch
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_logging_is_silenced_during_the_session(
    recording_console, fake_idp_client, stub_agent_factory
):
    """
    The agents log heavily enough to make the chat unreadable, hence the blanket
    disable; this asserts it is actually in force while the session runs, which is
    the half the restore test cannot see.
    """
    recording_console()
    client = fake_idp_client()

    chat.run_chat(stack_name="s", prompt="hi")

    assert client.processor.disable_level_during_setup == logging.CRITICAL


@pytest.mark.unit
def test_logging_is_restored_after_a_successful_session(
    recording_console, fake_idp_client, stub_agent_factory
):
    recording_console()
    fake_idp_client()
    assert logging.root.manager.disable == 0, "precondition: logging starts enabled"

    chat.run_chat(stack_name="s", prompt="hi")

    assert logging.root.manager.disable == 0


@pytest.mark.unit
def test_logging_is_restored_when_the_processor_raises(
    recording_console, fake_idp_client, stub_agent_factory
):
    """
    The `finally` is the only thing that undoes a process-wide silence.

    Without it, a failed `idp-cli chat` would leave every logger in the process
    disabled -- and because `run_chat` is importable, that includes a caller that
    goes on to do something else. The symptom would be absent logs somewhere else
    entirely, which is why this is asserted rather than assumed.
    """
    recording_console()
    fake_idp_client(FakeProcessor(setup_error=RuntimeError("stack not found")))

    with pytest.raises(RuntimeError, match="stack not found"):
        chat.run_chat(stack_name="s", prompt="hi")

    assert logging.root.manager.disable == 0


@pytest.mark.unit
def test_logging_is_restored_when_the_repl_loop_body_raises(
    recording_console, fake_idp_client, stub_agent_factory, monkeypatch, created_loops
):
    """
    `_handle_prompt` swallows streaming errors, so anything that escapes the REPL
    body is unexpected -- and the nested `finally` that closes the loop must not
    get in the way of the outer one that restores logging.
    """
    recording_console(["question", "/quit"])
    fake_idp_client()

    def _explode(*args, **kwargs):
        raise RuntimeError("unexpected")

    monkeypatch.setattr(chat, "_handle_prompt", _explode)

    with pytest.raises(RuntimeError, match="unexpected"):
        chat.run_chat(stack_name="s")

    assert logging.root.manager.disable == 0
    assert created_loops[0].is_closed()
