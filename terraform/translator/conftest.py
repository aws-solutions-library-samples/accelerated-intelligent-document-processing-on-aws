# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Put the translator's own directory on ``sys.path`` so ``import idp_tf`` resolves.

``idp_tf`` is not installed anywhere; it is importable only from this directory.
``python -m pytest`` run from here adds the working directory to ``sys.path``, which
is why ``make test-packages-cicd`` (``cd terraform/translator && pytest tests``)
passes without this file. ``scripts/run_all_tests.py`` runs every root from the
repository root, so there the import failed at collection for every module that
imports ``idp_tf``. This makes the result independent of the working directory.
"""

import sys
from pathlib import Path

_TRANSLATOR = Path(__file__).resolve().parent
if str(_TRANSLATOR) not in sys.path:
    sys.path.insert(0, str(_TRANSLATOR))
