// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0

import React from 'react';

/**
 * A Class/Type value rendered so that a long class name wraps inside its table
 * cell instead of overflowing into the next column.
 *
 * Class names come from configuration and are conventionally CamelCase with no
 * spaces (e.g. "Estimated2024AnnualTaxableDistributions"), so the browser has
 * no break opportunity to use and the text spills over the neighbouring column.
 * `overflow-wrap: anywhere` both permits the mid-word break and shrinks the
 * element's min-content width, which is what lets the surrounding flex layout
 * (Cloudscape `SpaceBetween`) give the text less than its full width.
 */
const ClassNameText = ({ children, color }: { children: React.ReactNode; color?: string }): React.JSX.Element => (
  <span style={{ overflowWrap: 'anywhere', minWidth: 0, ...(color ? { color } : {}) }}>{children}</span>
);

export default ClassNameText;
