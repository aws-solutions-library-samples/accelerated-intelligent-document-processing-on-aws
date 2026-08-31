# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""IDP's LLM comparator + Stickler registry integration.

Owns two things:

1. ``LLMComparator`` — IDP's Bedrock-backed semantic comparator, wired into
   Stickler's ``BaseComparator`` protocol. Kept in-tree (not upstream's
   strands-based version) so ``idp_common.bedrock`` retry / throttling /
   cost-metering / GovCloud-endpoint / robust-JSON-parsing behavior applies.
2. ``register_idp_comparators()`` — idempotent registration of
   ``IDPLLMComparator`` with Stickler's global registry (via the public API,
   not the private ``_registry`` dict). Called by ``EvaluationService``
   initialization.

The name ``IDPLLMComparator`` is distinct from Stickler's own
``LLMComparator`` so we never need to overwrite an existing entry — Stickler's
registry raises on duplicates.
"""

import json
import logging
import re
import threading
from collections import OrderedDict
from typing import Any, Optional, Tuple

from idp_common.evaluation.contract import _is_match_true

logger = logging.getLogger(__name__)


# Cap on a comparator instance's memo table. Instances are cached per
# (model, field) for the life of a warm Lambda container, so an unbounded dict
# would grow with every distinct value pair the container ever sees. Well above
# the distinct pairs any single document produces, so the cache still does its
# job within a document.
_VERDICT_CACHE_MAX = 10_000


# Prefixes matching every error-path ``reason`` string ``compare_llm``
# emits (see the ``error_msg = ...`` assignments at approximately lines
# 472 / 558 / 596 / 602 below). ``LLMComparator.compare`` skips caching
# any verdict whose reason starts with one of these — a transient
# Bedrock throttle / 5xx / JSON-parse error must not poison the (v1, v2)
# pair for the warm container's lifetime. Update this tuple if a new
# ``error_msg`` shape is added in compare_llm.
_TRANSIENT_ERROR_PREFIXES = (
    "Task prompt formatting error",
    "Error parsing LLM response as JSON",
    "Unexpected error processing LLM response",
    "Error in LLM evaluation for ",
)


def _trivially_equal(a: str, b: str) -> bool:
    """True when two rendered values differ only by case or whitespace.

    Used to skip the LLM round trip for values a semantic judge could only ever
    call a match. Intentionally narrow — it folds case and collapses whitespace
    and nothing else, so every judgement that needs actual reasoning (formatting,
    abbreviation, word order, synonymy) still reaches the model.
    """
    return " ".join(a.split()).casefold() == " ".join(b.split()).casefold()


def _cache_key(value1: Any, value2: Any) -> Tuple[str, str]:
    """Build a stable cache key for a (value1, value2) pair.

    Uses ``json.dumps(sort_keys=True, default=str)`` so semantically-
    identical dicts hash to the same key regardless of insertion order
    (dict repr reflects insertion order in Py3.7+, which would cause
    cache misses precisely where Hungarian matching for structured
    lists needs hits). Non-JSON-serializable values fall through
    ``default=str`` to their string form; the pair still keys stably.
    """
    return (
        json.dumps(value1, sort_keys=True, default=str),
        json.dumps(value2, sort_keys=True, default=str),
    )


# Check if Stickler is available
try:
    from stickler.structured_object_evaluator.models.comparator_registry import (
        BaseComparator as SticklerBaseComparator,
    )

    STICKLER_AVAILABLE = True
    BaseComparator = SticklerBaseComparator  # type: ignore[misc, assignment]
except ImportError:
    STICKLER_AVAILABLE = False

    # Create a placeholder base class if Stickler is not available
    class BaseComparator:  # type: ignore
        """Placeholder BaseComparator base class."""

        pass


class LLMComparator(BaseComparator):
    """
    Stickler comparator that uses LLM-based semantic evaluation.

    This comparator wraps the existing IDP LLM comparison logic,
    allowing it to be used within the Stickler evaluation framework.
    """

    def __init__(
        self,
        model: Optional[str] = None,
        temperature: Optional[float] = None,
        top_k: Optional[int] = None,
        top_p: Optional[float] = None,
        max_tokens: Optional[int] = None,
        system_prompt: Optional[str] = None,
        task_prompt: Optional[str] = None,
        threshold: Optional[float] = None,
        document_class: Optional[str] = None,
        attribute_name: Optional[str] = None,
        attribute_description: Optional[str] = None,
        **kwargs,
    ):
        """
        Initialize the LLM comparator.

        All parameters come per-instance via Stickler's per-field
        ``x-aws-stickler-comparator-config`` channel — the caller
        (``SticklerConfigMapper``) surfaces ``evaluation.llm_method`` from IDP
        config as this config dict. Two ``EvaluationService`` instances in the
        same process can therefore use different LLM configs; there is no
        module-level state.

        Args:
            model: Bedrock model ID to use for evaluation
            temperature: Temperature for LLM generation (0.0-1.0)
            top_k: Top-k sampling parameter
            top_p: Top-p (nucleus) sampling parameter
            max_tokens: Maximum tokens for LLM response
            system_prompt: Custom system prompt for LLM
            task_prompt: Custom task prompt template for LLM
            threshold: Minimum score to consider a match (0.0-1.0)
            document_class: Document class name, for the prompt's
                ``{DOCUMENT_CLASS}`` placeholder.
            attribute_name: Field name, for ``{ATTRIBUTE_NAME}``.
            attribute_description: Field description, for
                ``{ATTRIBUTE_DESCRIPTION}``. This is the one the judge most needs:
                it is what makes a *semantic* decision possible instead of a guess
                between two bare strings.
            **kwargs: Additional parameters (ignored)
        """
        super().__init__()

        # Helper to convert string to proper type (Stickler may forward YAML
        # string values verbatim).
        def to_float(val):
            return float(val) if isinstance(val, str) else val

        def to_int(val):
            return int(val) if isinstance(val, str) else val

        self.llm_config = {
            "model": model or "us.anthropic.claude-3-sonnet-20240229-v1:0",
            "temperature": to_float(temperature if temperature is not None else 0.0),
            "top_k": to_int(top_k if top_k is not None else 5),
        }
        if top_p is not None:
            self.llm_config["top_p"] = to_float(top_p)
        if max_tokens is not None:
            self.llm_config["max_tokens"] = to_int(max_tokens)
        if system_prompt is not None:
            self.llm_config["system_prompt"] = str(system_prompt)
        if task_prompt is not None:
            self.llm_config["task_prompt"] = str(task_prompt)

        self.threshold = to_float(threshold if threshold is not None else 0.8)

        # Field context for the prompt's three context placeholders. Supplied
        # per-field by SticklerConfigMapper via
        # x-aws-stickler-comparator-config, because Stickler's comparator
        # protocol is compare(value1, value2) and carries no field context.
        self.document_class = document_class or ""
        self.attribute_name = attribute_name or ""
        self.attribute_description = attribute_description or ""

        # Memoize verdicts per (expected, actual) within this comparator's
        # lifetime. Structured-list matching compares the same value pairs
        # repeatedly across the assignment matrix and again when scoring the
        # matched pairs, and the judge is deterministic at temperature 0, so a
        # repeat is a wasted round trip.
        #
        # Bounded, LRU-evicting, thread-safe:
        # * Bounded because comparator instances are cached per (model, field)
        #   for the life of a warm Lambda container, which processes many
        #   documents — an unbounded dict would be a slow leak.
        # * LRU rather than "insert until full, then never evict" so a warm
        #   Lambda that filled the cache on one big document still caches
        #   later documents' pairs (finding from code review — earlier
        #   design froze the cache at 10 000 entries for the container's
        #   lifetime).
        # * Locked because Stickler's Hungarian matching drives ``compare``
        #   from a thread pool, and OrderedDict's move_to_end + popitem +
        #   __setitem__ combo used below is a three-op critical section.
        self._verdict_cache: "OrderedDict[Tuple[str, str], float]" = OrderedDict()
        self._verdict_cache_lock = threading.Lock()

        logger.debug(
            f"Initialized LLMComparator with model={self.llm_config['model']}, threshold={self.threshold}"
        )

    def compare(self, value1: Any, value2: Any) -> float:
        """
        Compare two values using LLM-based semantic evaluation.

        This method delegates to the module-level compare_llm function.

        Args:
            value1: First value to compare (expected)
            value2: Second value to compare (actual)

        Returns:
            Similarity score between 0.0 and 1.0
        """
        try:
            # Cache key uses JSON with ``sort_keys=True`` (not ``repr``)
            # so semantically-identical dicts from different JSON parses
            # hash to the SAME key — dict repr reflects insertion order
            # in Py3.7+, and Hungarian matching for structured lists
            # (the raison d'être of this cache) cross-compares dicts
            # whose key order may differ between the two sides. Fall
            # back to ``repr`` for non-JSON-serializable values.
            cache_key = _cache_key(value1, value2)
            with self._verdict_cache_lock:
                if cache_key in self._verdict_cache:
                    self._verdict_cache.move_to_end(cache_key)
                    return self._verdict_cache[cache_key]

            # Call the existing LLM comparison logic, WITH this field's context.
            # Bedrock invocation is outside the lock — locking across a
            # network call would serialize the Hungarian-matching thread pool.
            matched, score, reason = compare_llm(
                expected=value1,
                actual=value2,
                document_class=self.document_class,
                attr_name=self.attribute_name,
                attr_description=self.attribute_description,
                llm_config=self.llm_config,
            )

            logger.debug(
                f"LLM comparison: matched={matched}, score={score:.3f}, reason='{reason}'"
            )

            # Only cache successful verdicts — ``compare_llm`` returns
            # ``(False, 0.0, err_msg)`` on Bedrock throttle / 5xx / JSON
            # parse errors, and caching that permanently would freeze the
            # value pair at 0.0 for the warm container's lifetime after a
            # single transient failure. Prefixes taken from the four
            # actual ``error_msg = ...`` sites in ``compare_llm`` below
            # (lines 472/558/596/602). Update ``_TRANSIENT_ERROR_PREFIXES``
            # if a new error path is added.
            # Guard against a Stickler variant emitting ``reason=None``
            # from ``compare_llm`` — plain ``reason.startswith(...)``
            # would crash and the outer except would return 0.0
            # without caching, so every retry re-invokes Bedrock
            # and re-crashes. Coerce to str first.
            reason_str = reason if isinstance(reason, str) else ""
            is_transient_error = any(
                reason_str.startswith(p) for p in _TRANSIENT_ERROR_PREFIXES
            )
            if not is_transient_error:
                with self._verdict_cache_lock:
                    # Recheck after acquiring the lock: another thread may
                    # have scored the same pair while we were waiting on
                    # Bedrock.
                    if cache_key in self._verdict_cache:
                        self._verdict_cache.move_to_end(cache_key)
                    else:
                        if len(self._verdict_cache) >= _VERDICT_CACHE_MAX:
                            self._verdict_cache.popitem(last=False)
                        self._verdict_cache[cache_key] = score
            return score

        except Exception as e:
            logger.error(f"Error in LLM comparison: {str(e)}", exc_info=True)
            # Return 0.0 score on error to be conservative
            return 0.0

    def __repr__(self) -> str:
        """String representation of the comparator."""
        return f"LLMComparator(model={self.llm_config['model']}, threshold={self.threshold})"


def create_llm_comparator_from_config(config: dict) -> LLMComparator:
    """
    Create an LLM comparator from configuration dict.

    This is a convenience factory function for creating LLM comparators
    from configuration dictionaries.

    Args:
        config: Configuration dictionary with LLM parameters

    Returns:
        Configured LLMComparator instance
    """
    return LLMComparator(
        model=config.get("model", "us.anthropic.claude-3-sonnet-20240229-v1:0"),
        temperature=config.get("temperature", 0.0),
        top_k=config.get("top_k", 5),
        top_p=config.get("top_p"),
        max_tokens=config.get("max_tokens"),
        system_prompt=config.get("system_prompt"),
        task_prompt=config.get("task_prompt"),
        threshold=config.get("threshold", 0.8),
    )


def compare_llm(
    expected: Any,
    actual: Any,
    document_class: Optional[str] = None,
    attr_name: Optional[str] = None,
    attr_description: Optional[str] = None,
    llm_config: Optional[dict] = None,
    bedrock_invoker=None,
) -> Tuple[bool, float, Optional[str]]:
    """
    Compare values using an LLM to determine semantic equivalence.

    Invokes a Bedrock model with a JSON-returning prompt and parses out the
    match/score/reason. Used by LLMComparator.compare() (the Stickler-registered
    comparator for the LLM evaluation method).

    Args:
        expected: Expected value
        actual: Actual value
        document_class: Document class name
        attr_name: Attribute name
        attr_description: Attribute description
        llm_config: Configuration for LLM invocation
        bedrock_invoker: Function to invoke Bedrock models

    Returns:
        Tuple of (matched, score, reason)
    """
    if not bedrock_invoker:
        from idp_common import bedrock

        bedrock_invoker = bedrock.invoke_model

    try:
        # Format attribute description
        doc_class = document_class if document_class is not None else "unknown"
        name = attr_name if attr_name is not None else "attribute"
        desc = attr_description if attr_description is not None else ""

        # Default LLM configuration if not provided
        config = llm_config or {}
        model = config.get("model", "us.anthropic.claude-3-sonnet-20240229-v1:0")
        temperature = config.get("temperature", 0.0)
        top_k = config.get("top_k", 5)
        reasoning_effort = config.get("reasoning_effort")

        # Get system and task prompts from config or use defaults
        system_prompt = config.get(
            "system_prompt",
            """You are an evaluator that helps determine if the predicted and expected values match for document attribute extraction. You will consider the context and meaning rather than just exact string matching.""",
        )

        task_prompt_template = config.get(
            "task_prompt",
            """I need to evaluate attribute extraction for a document of class: {DOCUMENT_CLASS}.

For the attribute named "{ATTRIBUTE_NAME}" described as "{ATTRIBUTE_DESCRIPTION}":
- Expected value: {EXPECTED_VALUE}
- Actual value: {ACTUAL_VALUE}

Do these values match in meaning, taking into account formatting differences, word order, abbreviations, and semantic equivalence?
Provide your assessment as a JSON with three fields:
- "match": boolean (true if they match, false if not)
- "score": number between 0 and 1 representing the confidence/similarity score
- "reason": brief explanation of your decision

Respond ONLY with the JSON and nothing else.  Here's the exact format:
{
  "match": true or false,
  "score": 0.0 to 1.0,
  "reason": "Your explanation here"
}
""",
        )

        # Log for debugging
        logger.debug(f"LLM evaluation starting for attribute: {name}")
        logger.debug(f"Document class: {doc_class}")
        logger.debug(f"Attribute description: {desc}")

        # Two renderings per side:
        # * ``expected_str`` / ``actual_str`` — bare ``str(v)`` for the
        #   trivial-equal short-circuit below (case/whitespace-normalized
        #   equality, works on plain strings without JSON quoting/spacing
        #   getting in the way).
        # * ``expected_display`` / ``actual_display`` — JSON-encoded for
        #   the LLM prompt so ``None`` renders as bare ``null`` and a
        #   legitimate string ``"None"`` renders quoted, giving the
        #   judge a distinct rendering for each. Any single string
        #   sentinel would collide with the same-named string value
        #   (finding from #625 review — ``<null>`` sentinel could
        #   itself be a legitimate ground-truth string).
        # The None-check above short-circuits before ANY comparison
        # touches these strings, so the None/"None" ambiguity that
        # ``_trivially_equal`` could otherwise fall for isn't reachable
        # — the split renderings are for prompt clarity only.
        expected_str = str(expected) if expected is not None else "None"
        actual_str = str(actual) if actual is not None else "None"

        logger.debug(f"Expected value: {expected_str}")
        logger.debug(f"Actual value: {actual_str}")

        # SHORT-CIRCUIT: values that are already equal after trivial normalization
        # need no judge. A semantic comparator asked whether "Florida Democratic
        # Party" matches "Florida Democratic Party" will always say yes, so the
        # round trip buys nothing and costs latency, tokens, and throttle budget.
        # This is free accuracy-wise (it can only agree with the model) and removes
        # the bulk of calls on real corpora, where most fields are extracted
        # correctly. Deliberately conservative: case, surrounding and repeated
        # whitespace only — no punctuation or abbreviation folding, since deciding
        # those is exactly the judge's job.
        #
        # Compare on the RAW values (not the display strings): the display path
        # renders ``None`` as the literal ``"None"`` for the LLM prompt, so
        # ``expected=None`` vs ``actual="None"`` (the string) would otherwise
        # short-circuit as a match — they are NOT equal, one is null and the
        # other is a literal string (finding from code review: real bug on any
        # document whose ground truth or extraction emits the string
        # ``"None"``).
        if expected is None or actual is None:
            if expected is None and actual is None:
                return (
                    True,
                    1.0,
                    "Both values are None (correctly-empty; no LLM call required).",
                )
            # Exactly one side is None: not equal, don't short-circuit — let
            # the judge decide (some comparators still want to reason about
            # partial-null matches).
        elif _trivially_equal(expected_str, actual_str):
            return (
                True,
                1.0,
                "Values are identical after case/whitespace normalization "
                "(no LLM call required).",
            )

        # JSON-encoded display for the LLM prompt so ``None`` renders as
        # bare ``null`` and a legitimate string ``"None"`` renders quoted,
        # giving the judge a distinct rendering for each. Computed AFTER
        # the None / trivial-equal short-circuits above so a
        # non-JSON-serializable value (Decimal, datetime, Pydantic
        # BaseModel, set) can never crash a matching pair: values that
        # short-circuit as matched never reach this line, and
        # ``default=str`` handles the residual non-serializable types
        # for the LLM prompt itself.
        expected_display = json.dumps(expected, default=str)
        actual_display = json.dumps(actual, default=str)

        # Create task_placeholders dictionary with all possible placeholders
        task_placeholders = {
            "DOCUMENT_CLASS": doc_class,
            "ATTRIBUTE_NAME": name,
            "ATTRIBUTE_DESCRIPTION": desc,
            "EXPECTED_VALUE": expected_display,
            "ACTUAL_VALUE": actual_display,
        }

        try:
            # Use the common format_prompt function from bedrock
            from idp_common.bedrock import format_prompt

            task_prompt = format_prompt(
                task_prompt_template,
                task_placeholders,
                required_placeholders=None,  # Don't validate specific placeholders as they may vary
            )
            logger.debug(
                f"Successfully formatted task prompt with {len(task_placeholders)} placeholders"
            )
        except Exception as e:
            error_msg = f"Task prompt formatting error: {str(e)}"
            logger.error(f"Prompt template: '{task_prompt_template}'")
            logger.error(f"Placeholders: '{task_placeholders}'")
            logger.error(error_msg)
            return False, 0.0, error_msg

        # Create content for LLM request
        content = [{"text": task_prompt}]

        # Log system prompt for debugging
        logger.debug(f"Calling Bedrock model: {model}")

        # Call Bedrock model
        response = bedrock_invoker(
            model_id=model,
            system_prompt=system_prompt,
            content=content,
            temperature=temperature,
            top_k=top_k,
            reasoning_effort=reasoning_effort,
        )

        # Extract and parse response
        from idp_common import bedrock

        result_text = bedrock.extract_text_from_response(response).strip()
        logger.debug(f"Raw LLM response: {result_text}")

        # Try to parse as JSON
        try:
            # First attempt to find JSON block within text using regex
            # This pattern looks for balanced braces to find JSON objects
            json_pattern = r"(\{(?:[^{}]|(?:\{(?:[^{}]|(?:\{[^{}]*\}))*\}))*\})"
            json_matches = re.findall(json_pattern, result_text)

            # Check for code blocks with ```json ... ``` pattern
            code_block_pattern = r"```json\s*([\s\S]*?)\s*```"
            code_blocks = re.findall(code_block_pattern, result_text)

            # Try to parse code blocks first if they exist
            for code_block in code_blocks:
                try:
                    result_json = json.loads(code_block)
                    # Check if the JSON has the expected fields
                    if "match" in result_json and "score" in result_json:
                        match_value = result_json.get("match", False)
                        score_value = result_json.get("score", 0.0)
                        reason = result_json.get("reason", "No reason provided")
                        logger.info(
                            f"LLM evaluation for {name} (from code block): match={match_value}, score={score_value}, reason={reason}"
                        )
                        return _is_match_true(match_value), float(score_value), reason
                except json.JSONDecodeError:
                    # This code block wasn't valid JSON, try next one
                    continue

            # If we found potential JSON blocks
            if json_matches:
                # Try each potential JSON block
                for json_block in json_matches:
                    try:
                        result_json = json.loads(json_block)
                        # Check if the JSON has the expected fields
                        if "match" in result_json and "score" in result_json:
                            match_value = result_json.get("match", False)
                            score_value = result_json.get("score", 0.0)
                            reason = result_json.get("reason", "No reason provided")
                            logger.info(
                                f"LLM evaluation for {name}: match={match_value}, score={score_value}, reason={reason}"
                            )
                            return (
                                _is_match_true(match_value),
                                float(score_value),
                                reason,
                            )
                    except json.JSONDecodeError:
                        # This particular block wasn't valid JSON, try next one
                        continue

            # If we didn't find a valid JSON block, try the entire text
            result_json = json.loads(result_text)
            # Extract values from JSON
            match_value = result_json.get("match", False)
            score_value = result_json.get("score", 0.0)
            reason = result_json.get("reason", "No reason provided")
            logger.info(
                f"LLM evaluation for {name}: match={match_value}, score={score_value}, reason={reason}"
            )
            return _is_match_true(match_value), float(score_value), reason
        except json.JSONDecodeError as e:
            error_msg = f"Error parsing LLM response as JSON: {str(e)}"
            logger.error(error_msg)
            logger.error(f"Raw response was: {result_text}")

            # Last-ditch effort: try a very flexible pattern to extract key information
            # Look for match/score/reason patterns directly
            try:
                match_pattern = r'"?match"?\s*[:=]\s*(true|false)'
                score_pattern = r'"?score"?\s*[:=]\s*([0-9]*\.?[0-9]+)'
                reason_pattern = r'"?reason"?\s*[:=]\s*"([^"]*)"'

                match_search = re.search(match_pattern, result_text.lower())
                score_search = re.search(score_pattern, result_text.lower())
                reason_search = re.search(reason_pattern, result_text)

                if match_search and score_search:
                    match_value = match_search.group(1).lower() == "true"
                    score_value = float(score_search.group(1))
                    reason = (
                        reason_search.group(1)
                        if reason_search
                        else "No reason extracted"
                    )

                    logger.info(
                        f"LLM evaluation for {name} (extracted from text): match={match_value}, score={score_value}"
                    )
                    return _is_match_true(match_value), float(score_value), reason
            except Exception as extract_error:
                logger.error(
                    f"Failed to extract values from malformed response: {str(extract_error)}"
                )

            logger.error(
                'Response from LLM must be JSON like: {"match": boolean, "score": float, "reason": string}'
            )
            return False, 0.0, error_msg
        except Exception as e:
            error_msg = f"Unexpected error processing LLM response: {str(e)}"
            logger.error(error_msg)
            logger.error(f"Raw response was: {result_text}")
            return False, 0.0, error_msg

    except Exception as e:
        error_msg = f"Error in LLM evaluation for {attr_name}: {str(e)}"
        logger.error(error_msg)
        return False, 0.0, error_msg


def register_idp_comparators() -> None:
    """Idempotently register IDP's comparators with Stickler's global registry.

    Uses Stickler's public ``register_comparator`` API rather than reaching
    into the private ``_registry`` dict. The registry raises on duplicate
    names — we guard with ``is_registered`` so this stays safe to call
    multiple times (multiple ``EvaluationService`` instances in one
    process — e.g. ``idp_cli`` batch loops, warm Lambdas after config
    updates — all share the registry).
    """
    try:
        from stickler.structured_object_evaluator.models.comparator_registry import (
            _global_registry,
            register_comparator,
        )
    except ImportError:  # pragma: no cover — stickler is a hard dep
        return

    if not _global_registry.is_registered("IDPLLMComparator"):
        register_comparator("IDPLLMComparator", LLMComparator)  # type: ignore[arg-type]
        logger.info("Registered IDPLLMComparator with Stickler via public API")
