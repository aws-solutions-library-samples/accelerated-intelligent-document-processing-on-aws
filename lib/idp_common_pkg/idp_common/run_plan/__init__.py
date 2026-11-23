# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Run plans: where a pipeline run starts and which steps it skips.

Every run the product starts carries a run plan. A sender builds it, clears the
outputs the run will produce again and queues the document through one function;
the queue processor turns the plan into the ``runPlan`` block of the execution
input; the state machine reads that block to jump to the step the run needs.

Import the submodule you need (``idp_common.run_plan.models``, ``.steps``,
``.asl``, ``.mode``, ``.clearing``, ``.eligibility``, ``.sender``,
``.reprocess``, ``.resolver_requests``, ``.restart``, ``.planned``). This
package imports nothing at load,
so the queue processor and the routing Lambda pay only for what they use. See
``README.md``.
"""
