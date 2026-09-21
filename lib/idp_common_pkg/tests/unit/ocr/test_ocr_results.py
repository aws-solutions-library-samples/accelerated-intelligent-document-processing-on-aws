# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""
Unit tests for the OCR results module.
"""

import pytest


@pytest.mark.unit
class TestOcrResults:
    """Tests for the OCR results module."""

    def test_module_deprecation(self):
        """Test that the module is properly marked as deprecated."""
        import idp_common.ocr.results

        docstring = idp_common.ocr.results.__doc__
        assert docstring is not None, (
            "idp_common.ocr.results has no module docstring, so it no longer "
            "tells a reader that the module is deprecated"
        )

        # Check that the module docstring indicates it's deprecated
        assert "deprecated" in docstring.lower()

        # Check that the docstring mentions where functionality was moved
        assert "idp_common.models" in docstring
