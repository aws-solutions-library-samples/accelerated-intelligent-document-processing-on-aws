# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Which agents an Agent Companion Chat orchestrator is built with, per session.

Two kinds of sub-agent are treated differently from the rest:

* **Data agents** (:data:`DATA_AGENT_IDS`: Analytics, Error Analyzer) read this
  deployment's own data — the reporting database, logs, execution records — and
  their results enter the orchestrator's context and, through memory, every later
  turn of the session (up to ``MAX_CONVERSATION_TURNS``).
* **Opt-in agents** send their query to a service outside the deployment: the
  Code Intelligence Agent (a public DeepWiki endpoint) and every External MCP
  agent registered from the Secrets Manager secret. Each is off unless the request
  opts in to its kind (:data:`OPT_IN_CODE_INTELLIGENCE`,
  :data:`OPT_IN_EXTERNAL_MCP`), and the registry records the kind on the agent
  (``opt_in``) when it is registered.

The rule is that the two never share a conversation. Once a data agent has
returned a result in a session, the session is flagged
(``idp_common.chat_session_owner.record_data_results``), and from then on:

* :func:`select_orchestrator_agent_ids` leaves every opt-in agent out of the
  orchestrator, whatever the request asked for;
* within the turn the flag was set in, :class:`SessionAgentGate` makes an opt-in
  agent the orchestrator was already built with answer with
  :data:`UNAVAILABLE_MESSAGE` instead of running;
* a request that opts in on a flagged session is moved to a new session with no
  memory (:func:`needs_new_session`), by whichever entry point persists the turn.

Imports nothing beyond the standard library, so both chat processors and their
tests load it without the agent framework.
"""

import logging
import threading
from typing import Any, Callable, Dict, Iterable, List, Optional

logger = logging.getLogger(__name__)

#: The agents whose results flag a session.
DATA_AGENT_IDS = frozenset({"Analytics-Agent", "Error-Analyzer-Agent"})

CODE_INTELLIGENCE_AGENT_ID = "Code-Intelligence-Agent"
QUICK_START_AGENT_ID = "Quick-Start-Agent"

#: ``opt_in`` value of the Code Intelligence Agent's registry entry.
OPT_IN_CODE_INTELLIGENCE = "code_intelligence"
#: ``opt_in`` value of every External MCP agent's registry entry.
OPT_IN_EXTERNAL_MCP = "external_mcp"

#: Stream event the processors send when a session is (or already was) flagged.
SESSION_DATA_RESULTS_EVENT = "session_data_results"
#: Stream event the processors send when a turn moved to a new session.
SESSION_REPLACED_EVENT = "session_replaced"

#: What an opt-in agent answers with in a turn where a data agent already ran.
UNAVAILABLE_MESSAGE = (
    "This agent is not available in a conversation that includes Analytics or "
    "Error Analyzer results. Start a new conversation to use it."
)


def opt_in_of(agent: Dict[str, Any]) -> Optional[str]:
    """The opt-in kind of a ``list_available_agents`` entry, or ``None``.

    The Code Intelligence Agent is recognised by id as well as by its registry
    entry, so an entry registered without ``opt_in`` is still opt-in.
    """
    kind = agent.get("opt_in")
    if kind:
        return str(kind)
    if agent.get("agent_id") == CODE_INTELLIGENCE_AGENT_ID:
        return OPT_IN_CODE_INTELLIGENCE
    return None


def requested_opt_ins(
    *, enable_code_intelligence: bool, enable_external_mcp_agents: bool
) -> frozenset:
    """The opt-in kinds a request asked for."""
    kinds = set()
    if enable_code_intelligence:
        kinds.add(OPT_IN_CODE_INTELLIGENCE)
    if enable_external_mcp_agents:
        kinds.add(OPT_IN_EXTERNAL_MCP)
    return frozenset(kinds)


def needs_new_session(*, holds_data_results: bool, opt_ins: Iterable[str]) -> bool:
    """Whether a turn asking for ``opt_ins`` must run in a new, empty session."""
    return bool(holds_data_results and frozenset(opt_ins))


def select_orchestrator_agent_ids(
    agents: Iterable[Dict[str, Any]],
    *,
    opt_ins: Iterable[str],
    holds_data_results: bool,
) -> List[str]:
    """The agent ids an orchestrator is built with.

    Args:
        agents: ``agent_factory.list_available_agents()``.
        opt_ins: the opt-in kinds the request asked for
            (:func:`requested_opt_ins`).
        holds_data_results: whether the session is flagged. When True no opt-in
            agent is included, whatever ``opt_ins`` says.

    The Quick Start agent is never included: it runs on its own surface.
    """
    allowed = frozenset() if holds_data_results else frozenset(opt_ins)
    selected = []
    for agent in agents:
        agent_id = agent["agent_id"]
        if agent_id == QUICK_START_AGENT_ID:
            continue
        kind = opt_in_of(agent)
        if kind is not None and kind not in allowed:
            continue
        selected.append(agent_id)
    return selected


class SessionAgentGate:
    """Per-turn state shared by an orchestrator's sub-agent tools.

    Built by the processor for one turn and passed to the orchestrator as
    ``session_gate``. A data agent's tool calls :meth:`note_data_result` before
    it hands its result to the orchestrator; an opt-in agent's tool asks
    :meth:`allows` before it runs.

    Args:
        holds_data_results: whether the session was already flagged when the
            turn began.
        opt_in_agent_ids: ids of the opt-in agents (any kind) known to the
            registry.
        recheck: called when an opt-in agent is about to run and the gate is
            still open; True if the session has been flagged since the turn
            began. This closes the gap where another turn on the same session
            records a data result after this turn read the flag but before it
            loaded memory: the flag is always written before the result reaches
            memory, so a flag still unset at this point means memory held no
            data result when it was loaded. ``None`` re-checks nothing.
        record: called once, the first time a data agent returns a result in a
            session that was not yet flagged. It persists the flag; if it
            raises, :meth:`note_data_result` raises too and the result is not
            handed on. ``None`` records nothing (a turn with no signed-in user,
            which has no memory to carry the result forward).
    """

    def __init__(
        self,
        *,
        holds_data_results: bool,
        opt_in_agent_ids: Iterable[str],
        record: Optional[Callable[[], None]] = None,
        recheck: Optional[Callable[[], bool]] = None,
    ):
        self._holds = bool(holds_data_results)
        self._opt_in = frozenset(opt_in_agent_ids)
        self._record = record
        self._recheck = recheck
        self._lock = threading.Lock()

    @property
    def holds_data_results(self) -> bool:
        return self._holds

    def allows(self, agent_id: str) -> bool:
        """False for an opt-in agent once the session holds data results."""
        if agent_id not in self._opt_in:
            return True
        if not self._holds and self._recheck is not None:
            try:
                flagged = bool(self._recheck())
            except Exception as e:
                logger.warning(f"Could not re-read the session flag: {e}")
                flagged = True
            if flagged:
                with self._lock:
                    self._holds = True
        return not self._holds

    def note_data_result(self, agent_id: str) -> None:
        """Flag the session when a data agent returns its first result."""
        if agent_id not in DATA_AGENT_IDS:
            return
        with self._lock:
            if self._holds:
                return
            # Set before recording: if recording fails the caller withholds the
            # result, and the opt-in agents stay closed for the rest of the turn
            # either way.
            self._holds = True
            if self._record is not None:
                self._record()
