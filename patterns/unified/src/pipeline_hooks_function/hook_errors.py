# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""The one error type the state machine routes on, shared by the dispatcher and
any code shipped alongside it in this Lambda package.

Step Functions matches a Task's error against ``Catch[*].ErrorEquals`` by NAME.
For a Lambda invoked through the optimized ``states:::lambda:invoke`` integration
that name is the ``errorType`` field of the function-error payload, which the
Python runtime sets to the raised exception's class ``__name__`` (not its module
path — verified against ``awslambdaric.bootstrap.build_fault_result`` inside the
``public.ecr.aws/lambda/python:3.12`` image). So the class name below IS the
string in ``workflow.asl.json``: renaming this class without renaming it there
silently restores the fail-open behaviour of #919.

Why a distinct type rather than the ``RuntimeError`` this used to raise: every
post-step hook state catches ``States.ALL`` and routes FORWARD, because a hook
fault must not discard a document whose expensive steps already succeeded. That
catch-all also swallowed the ``onError: fail`` policy — the documented way for a
hook to GATE the pipeline — so a compliance or redaction hook that declared it
was ignored. A name of its own lets those states catch the fail policy FIRST and
route to a terminal Fail state, while still absorbing everything else.
"""

__all__ = ["HookFatalError"]


class HookFatalError(Exception):
    """A hook failed and its policy is ``onError: fail`` — abort the document.

    Raised ONLY for that policy. ``continue`` and ``skip-remaining`` never raise:
    they are recorded in the dispatcher's result and the pipeline proceeds.
    """
