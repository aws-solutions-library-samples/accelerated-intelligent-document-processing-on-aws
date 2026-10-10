# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""
Chat command for idp-cli.

Runs the Agent Companion Chat orchestrator locally, providing interactive
access to Analytics, Error Analyzer, and other agents from the terminal.
"""

import asyncio
import logging
import uuid
from typing import Optional

from rich.console import Console

logger = logging.getLogger(__name__)
console = Console()

# The tags agents wrap private reasoning in. Matched as exact literals — no
# attributes, no case folding — so exactly the text that used to be recognised as a
# reasoning block is recognised as one now.
_THINKING_OPEN = "<thinking>"
_THINKING_CLOSE = "</thinking>"


def run_chat(
    stack_name: str,
    region: Optional[str] = None,
    prompt: Optional[str] = None,
    enable_code_intelligence: bool = False,
):
    """
    Run the chat command — interactive REPL or single-shot.

    Args:
        stack_name: CloudFormation stack name
        region: AWS region
        prompt: If provided, run single-shot and exit
    """
    console.print("[bold blue]IDP Agent Chat[/bold blue]")
    console.print(f"[dim]Stack: {stack_name}[/dim]")
    console.print()

    # Use IDPClient to access chat processor internals
    from idp_sdk import IDPClient

    # Suppress all logging — agents are very chatty
    logging.disable(logging.CRITICAL)

    try:
        client = IDPClient(stack_name=stack_name, region=region)
        processor = client.chat._get_processor()

        with console.status("[bold]Discovering stack resources..."):
            processor._setup_env()

        session_id = f"cli-{uuid.uuid4().hex[:12]}"

        with console.status("[bold]Initializing agents..."):
            processor._ensure_orchestrator(
                session_id,
                enable_code_intelligence=enable_code_intelligence,
            )

        # Show available agents
        from idp_common.agents.factory import agent_factory

        agents = agent_factory.list_available_agents()
        agent_names = [a["agent_name"] for a in agents]
        console.print(
            f"[green]✓ Ready[/green]  [dim]Agents: {' · '.join(agent_names)}[/dim]"
        )
        console.print("[dim]Type /quit to exit[/dim]\n")

        if prompt:
            _handle_prompt(processor._orchestrator, prompt)
            return

        # Interactive REPL — reuse a single event loop for the session
        loop = asyncio.new_event_loop()
        try:
            while True:
                try:
                    user_input = console.input("[bold cyan]You:[/bold cyan] ")
                except (EOFError, KeyboardInterrupt):
                    console.print("\n[dim]Goodbye.[/dim]")
                    break

                text = user_input.strip()
                if not text:
                    continue
                if text.lower() in ("/quit", "/exit", "quit", "exit"):
                    console.print("[dim]Goodbye.[/dim]")
                    break

                _handle_prompt(processor._orchestrator, text, loop=loop)
        finally:
            loop.close()
    finally:
        # Restore logging so callers aren't permanently silenced
        logging.disable(logging.NOTSET)


def _handle_prompt(orchestrator, prompt: str, loop=None):
    """Send a prompt to the orchestrator and stream the response."""
    console.print()

    if loop is not None:
        # Reuse persistent loop (interactive REPL)
        try:
            loop.run_until_complete(_stream_response(orchestrator, prompt))
        except Exception as e:
            console.print(f"[red]Error: {e}[/red]")
    else:
        # One-shot mode — create and tear down a loop
        one_shot = asyncio.new_event_loop()
        try:
            one_shot.run_until_complete(_stream_response(orchestrator, prompt))
        except Exception as e:
            console.print(f"[red]Error: {e}[/red]")
        finally:
            one_shot.close()

    console.print()


def _longest_partial_tag_suffix(text: str, tag: str) -> int:
    """
    Length of the longest proper prefix of `tag` that `text` ends with.

    This is how many trailing characters cannot yet be classified: they may turn
    out to be the front of a tag that the next chunk completes. Zero means the
    whole of `text` is decided.
    """
    for length in range(min(len(tag) - 1, len(text)), 0, -1):
        if text.endswith(tag[:length]):
            return length
    return 0


class _ThinkingFilter:
    """
    Remove `<thinking>...</thinking>` from a stream that arrives in pieces.

    A chunk boundary can fall anywhere — a `{"data": ...}` event carries one
    Bedrock text delta verbatim, so the split point is chosen by the service and
    can land in the middle of either tag. Deciding what to show by re-running a
    regex over the accumulated text cannot cope with that: with no closing tag in
    the buffer yet the block matches nothing, so the reasoning is displayed, and
    when the closing tag finally arrives the cleaned text is *shorter* than what
    was already on screen, so the answer that follows is dropped or printed from
    the middle.

    So this holds a boundary instead of re-deriving one. Text is released only
    once it is known to sit outside a block, which means holding back any trailing
    run of characters that could still be the front of a tag. Whatever is released
    is final: the visible output only ever grows, and a caller can print it
    without keeping a cursor into a buffer that shrinks underneath it.
    """

    def __init__(self) -> None:
        self._held = ""
        self._pending_space = ""
        self._inside = False
        self._started = False

    def feed(self, chunk: str) -> str:
        """Add a streamed chunk and return the text that is now safe to display."""
        self._held += chunk
        released: list[str] = []

        while True:
            if self._inside:
                end = self._held.find(_THINKING_CLOSE)
                if end != -1:
                    self._held = self._held[end + len(_THINKING_CLOSE) :]
                    self._inside = False
                    continue
                # Still inside the block: this is reasoning and is discarded, bar
                # any tail that could be the start of the closing tag.
                keep = _longest_partial_tag_suffix(self._held, _THINKING_CLOSE)
                self._held = self._held[len(self._held) - keep :] if keep else ""
                break

            start = self._held.find(_THINKING_OPEN)
            if start != -1:
                released.append(self._held[:start])
                self._held = self._held[start + len(_THINKING_OPEN) :]
                self._inside = True
                continue
            keep = _longest_partial_tag_suffix(self._held, _THINKING_OPEN)
            if keep:
                released.append(self._held[: len(self._held) - keep])
                self._held = self._held[len(self._held) - keep :]
            else:
                released.append(self._held)
                self._held = ""
            break

        return self._present("".join(released))

    def close(self) -> str:
        """
        Return whatever is still held and is genuinely text, once per response.

        A response that ends mid-tag leaves a few held characters that no chunk
        will ever complete; they are ordinary text and are released. A response
        that ends inside an unterminated block leaves reasoning, which is
        discarded — an agent that stops mid-thought has no answer to show, and
        showing the thought is the defect this class exists to prevent.

        A filter is **single-use**: this drains what is held but does not put the
        instance back into its opening state, so `_stream_response` builds one per
        response rather than reusing one.
        """
        tail = "" if self._inside else self._held
        self._held = ""
        return self._present(tail)

    def _present(self, text: str) -> str:
        """
        Apply the whitespace trimming the previous `.strip()` of the buffer gave.

        Leading whitespace is dropped until the first real output. Trailing
        whitespace is *deferred* rather than dropped, because more text may follow
        it and the space between two words belongs on screen — so it is emitted in
        front of whatever comes next, and a run still deferred when the stream ends
        is never printed. That is what the old whole-buffer `.strip()` amounted to,
        and without it a response ending in blank lines pushes the shell prompt
        down the terminal.
        """
        text = self._pending_space + text
        self._pending_space = ""
        if text and not self._started:
            text = text.lstrip()
        visible = text.rstrip()
        self._pending_space = text[len(visible) :]
        if visible:
            self._started = True
        return visible


async def _stream_response(orchestrator, prompt: str) -> str:
    """Stream orchestrator response, printing chunks as they arrive."""
    thinking = _ThinkingFilter()
    displayed = ""
    current_subagent = None

    def show(text: str) -> None:
        nonlocal displayed
        if text:
            console.print(text, end="", highlight=False)
            displayed += text

    # The release of held text is in a `finally` because a stream can raise
    # part-way -- `_handle_prompt` catches that and prints the error -- and the few
    # characters being withheld at that moment are answer text the user should see
    # before the error rather than text the failure silently swallows.
    try:
        async for event in orchestrator.stream_async(prompt):
            if "data" in event:
                show(thinking.feed(event["data"]))

            elif "current_tool_use" in event:
                tool_name = event["current_tool_use"].get("name", "")
                if tool_name and tool_name != current_subagent:
                    current_subagent = tool_name
                    display_name = (
                        tool_name.replace("_agent", "").replace("_", " ").title()
                    )
                    console.print(f"\n[dim]⟶ {display_name}[/dim]", highlight=False)
    finally:
        show(thinking.close())

    console.print()
    return displayed.rstrip()
