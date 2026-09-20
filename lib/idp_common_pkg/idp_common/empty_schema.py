# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Why a section's effective extraction schema was empty.

``ExtractionService`` skips the model entirely for a section whose class resolves
to no extractable properties and writes a stub result flagged
``metadata.skipped_due_to_empty_attributes``. That single flag covered three
situations which have nothing in common except the empty result:

* the class **is** in configuration and deliberately declares no attributes — an
  authoring choice, and nothing is missing;
* **no class was determined** for the section at all, so its label is the
  ``unclassified`` sentinel. Ordinary for a blank page, a reportable failure for a
  page whose classification errored — a distinction only the classification stage
  can draw, and it draws it there;
* the section carries a **named class the configuration does not contain**. The
  section's fields were never extracted and no other stage reports it, so this one
  is a fault.

The confidence pass keys on the difference (``_extraction_declared_no_fields`` in
``idp_common.assessment.service``): it reports a section as left without
confidence only for the third case, because reporting the first two would put an
error indicator and an ``AssessmentConfidenceUnavailable`` data point on every
blank page — and an alarm that fires on healthy throughput is one operators
switch off (``#996``, ``#1006``).

These constants live in their own dependency-free module, as
``idp_common.section_exclusion`` does for the excluded-class vocabulary, so the
producing and consuming stages share one spelling without either importing the
other's package. Two copies of the string is how the producer and the predicate
drift apart and the fault becomes silent again.

A stub written before ``empty_schema_reason`` existed has no reason key at all;
readers treat that as :data:`EMPTY_SCHEMA_NO_ATTRIBUTES`, which keeps an old
result file reading back exactly as it did.
"""

#: The stub's ``metadata`` key carrying one of the values below.
EMPTY_SCHEMA_REASON_KEY = "empty_schema_reason"

#: The class is in configuration and declares no extractable properties.
EMPTY_SCHEMA_NO_ATTRIBUTES = "class_has_no_attributes"

#: Classification determined no class for the section.
EMPTY_SCHEMA_UNCLASSIFIED = "class_unclassified"

#: The section's named class is absent from the configuration in force.
EMPTY_SCHEMA_CLASS_NOT_CONFIGURED = "class_not_configured"
