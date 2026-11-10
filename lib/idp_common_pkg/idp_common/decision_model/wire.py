# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""The System One API: the request and response a decision model speaks.

A decision model picks between options or rates on a scale, and returns a
probability for each option rather than generated text. This module is the wire
contract only, written from the published specification of the
``POST /v1/systemone`` request that the Strands Decider serves
(https://github.com/strands-labs/strands-decider). It deliberately does not import
that package: the contract is what is stable across the decider's releases, and
its v2 work keeps this request and response unchanged while replacing everything
behind them.

Three question types, one mechanism. Each is rendered into N options and read
back as a distribution over them:

    noul   -> 2 options (false, true)    -> P(true)
    choice -> N options (one per option) -> the argmax and every option's probability
    score  -> L ordered levels           -> the expected level index

The UI mirrors :data:`QUESTION_TYPES` in ``src/ui/src/constants/schemaConstants.ts``.
"""

from __future__ import annotations

from typing import Annotated, Any, Dict, List, Literal, Optional, Union

from pydantic import BaseModel, Field, field_validator

#: The question types, in the order the UI offers them.
QUESTION_TYPES = ("noul", "choice", "score")

#: Bounds the specification sets on a question's options.
MAX_CHOICE_OPTIONS = 255
MIN_SCORE_LEVELS = 2
MAX_SCORE_LEVELS = 10

#: The most images one request may carry, and the longest side, in pixels, the
#: decider reads an image at. Sending a larger image only spends payload: the
#: decider scales it down to this before reading it.
MAX_REQUEST_IMAGES = 4
IMAGE_MAX_SIDE = 448

#: A state or instructions value: a string, or structured data the decision model
#: renders deterministically.
Content = Union[str, Dict[str, Any], List[Any]]


def _require_instructions(v: Any) -> Any:
    """An empty question is refused at the contract rather than sent and billed."""
    if (
        v is None
        or (isinstance(v, (str, list, dict)) and not v)
        or (isinstance(v, str) and not v.strip())
    ):
        raise ValueError("a question's instructions must not be empty")
    return v


class NoulQuestion(BaseModel):
    """Yes or no. The answer is the probability that the statement is true."""

    type: Literal["noul"] = "noul"
    instructions: Content

    @field_validator("instructions")
    @classmethod
    def _instructions(cls, v):
        return _require_instructions(v)

    #: Optional ``{"true": ..., "false": ...}`` descriptions that sharpen the line.
    criteria: Optional[Dict[str, Optional[Content]]] = None

    @field_validator("criteria")
    @classmethod
    def _criteria_keys(cls, v):
        if v is not None and not set(v).issubset({"true", "false"}):
            raise ValueError("noul criteria keys must be a subset of {'true', 'false'}")
        return v


class ChoiceQuestion(BaseModel):
    """Pick one of N named options. ``criteria`` maps option name to description."""

    type: Literal["choice"] = "choice"
    instructions: Content

    @field_validator("instructions")
    @classmethod
    def _instructions(cls, v):
        return _require_instructions(v)

    criteria: Dict[str, Optional[Content]]

    @field_validator("criteria")
    @classmethod
    def _criteria_size(cls, v):
        if len(v) < 2:
            raise ValueError("choice needs at least 2 options")
        if len(v) > MAX_CHOICE_OPTIONS:
            raise ValueError(f"choice allows at most {MAX_CHOICE_OPTIONS} options")
        return v


class ScoreQuestion(BaseModel):
    """Rate against an ordered rubric. ``criteria`` is ascending: index 0 is lowest."""

    type: Literal["score"] = "score"
    instructions: Content

    @field_validator("instructions")
    @classmethod
    def _instructions(cls, v):
        return _require_instructions(v)

    criteria: List[str]

    @field_validator("criteria")
    @classmethod
    def _criteria_levels(cls, v):
        if not MIN_SCORE_LEVELS <= len(v) <= MAX_SCORE_LEVELS:
            raise ValueError(
                f"score needs between {MIN_SCORE_LEVELS} and {MAX_SCORE_LEVELS} levels"
            )
        return v


Question = Annotated[
    Union[NoulQuestion, ChoiceQuestion, ScoreQuestion], Field(discriminator="type")
]


class SystemOneRequest(BaseModel):
    state: Content
    questions: Dict[str, Question] = Field(..., min_length=1)
    #: Base64 images, part of the state. A text-only deployment refuses them.
    images: List[str] = Field(default_factory=list)

    @field_validator("images")
    @classmethod
    def _image_count(cls, v):
        if len(v) > MAX_REQUEST_IMAGES:
            raise ValueError(
                f"a request carries at most {MAX_REQUEST_IMAGES} images, got {len(v)}"
            )
        return v


class NoulAnswer(BaseModel):
    type: Literal["noul"] = "noul"
    noul: float


class ChoiceAnswer(BaseModel):
    type: Literal["choice"] = "choice"
    choice: str
    probabilities: Dict[str, float]
    #: Normalised max-probability, ``(N * p_max - 1) / (N - 1)``: 0 for a uniform
    #: distribution, 1 for a certain one. Not the same number as the chosen
    #: option's probability, which is ``probabilities[choice]``.
    confidence: float


class ScoreAnswer(BaseModel):
    type: Literal["score"] = "score"
    score: float
    legend: Dict[str, str]
    probabilities: Dict[str, float]
    confidence: float


Answer = Annotated[
    Union[NoulAnswer, ChoiceAnswer, ScoreAnswer], Field(discriminator="type")
]


class Usage(BaseModel):
    input_tokens: int = 0
    output_tokens: int = 0


class SystemOneResponse(BaseModel):
    model: str
    answers: Dict[str, Answer]
    usage: Usage = Field(default_factory=Usage)
    #: Set by the Lambda deployment: how long the evaluation took.
    latency_ms: Optional[float] = None
