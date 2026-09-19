# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Which pipeline hook points each processing mode actually reaches.

GENERATED FILE — do not edit. Regenerate with:

    python3 scripts/generate_hook_point_reachability.py

Derived by walking patterns/unified/statemachine/workflow.asl.json from
`PreprocessingHook` down each side of the `RouteByProcessingMode` Choice
(`$.document.use_bda BooleanEquals true` -> `BDA_CheckExistingData`; Default ->
`OCRStep`), blocking re-entry to the Choice and descending into any Map
or Parallel. A hook point ahead of the Choice runs in both modes and so appears
under both.

#   postClassification   bda=NO   pipeline=yes
#   postExtraction       bda=NO   pipeline=yes
#   postOcr              bda=NO   pipeline=yes
#   postRuleValidation   bda=yes  pipeline=yes
#   postSummarization    bda=yes  pipeline=yes
#   postprocessing       bda=yes  pipeline=yes
#   preprocessing        bda=yes  pipeline=yes

BDA performs OCR, classification and extraction inside one Bedrock Data
Automation invocation, so the BDA branch has no separate step to hook after and
the three step-specific points do not exist there. A hook registered at one of
them under `use_bda: true` never runs, and neither does its `onError: fail`
gate — the dispatcher is never invoked, so nothing raises and nothing appears in
the execution history (#982). Callers use :func:`unreachable_hook_points` to say
so at registration time and at runtime instead of leaving it silent.
"""

from __future__ import annotations

# Hook `Task` states each branch reaches, qualified by their enclosing Map where
# there is one. Informational: the point sets below are what callers act on, but
# these names are what a reader checks against the state machine in the console.
HOOK_STATES_BY_MODE = {
    "bda": (
        "PostRuleValidationHook",
        "PostSummarizationHook",
        "PostprocessingHook",
        "PreprocessingHook",
    ),
    "pipeline": (
        "PostClassificationHook",
        "PostOcrHook",
        "PostRuleValidationHook",
        "PostSummarizationHook",
        "PostprocessingHook",
        "PreprocessingHook",
        "ProcessSections.PostExtractionHook",
    ),
}

# Hook points invoked in each mode. Mode names are this module's own labels for
# the two sides of the `RouteByProcessingMode` Choice.
HOOK_POINTS_BY_MODE = {
    "bda": frozenset(
        {
            "postRuleValidation",
            "postSummarization",
            "postprocessing",
            "preprocessing",
        }
    ),
    "pipeline": frozenset(
        {
            "postClassification",
            "postExtraction",
            "postOcr",
            "postRuleValidation",
            "postSummarization",
            "postprocessing",
            "preprocessing",
        }
    ),
}

# Every hook point the state machine invokes in at least one mode.
ALL_HOOK_POINTS = frozenset(
    {
        "postClassification",
        "postExtraction",
        "postOcr",
        "postRuleValidation",
        "postSummarization",
        "postprocessing",
        "preprocessing",
    }
)


def processing_mode(use_bda: bool) -> str:
    """This module's label for the branch `$.document.use_bda` selects."""
    return "bda" if use_bda else "pipeline"


def reachable_hook_points(use_bda: bool) -> frozenset[str]:
    """Hook points the chosen branch invokes."""
    return HOOK_POINTS_BY_MODE[processing_mode(use_bda)]


def unreachable_hook_points(use_bda: bool) -> frozenset[str]:
    """Hook points that exist in the OTHER branch but not in this one.

    A hook registered at one of these cannot run in this mode. Empty for a mode
    that reaches every point.
    """
    return ALL_HOOK_POINTS - reachable_hook_points(use_bda)
