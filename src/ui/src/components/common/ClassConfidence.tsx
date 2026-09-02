// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0

import React from 'react';
import { Box, Popover, SpaceBetween } from '@cloudscape-design/components';
import ConfidenceDisplay from './ConfidenceDisplay';

interface ClassConfidenceProps {
  /** Confidence in the class (0-1). Null/undefined means NOT SCORED. */
  confidence?: number | null;
  /** The model's stated evidence for the class, when it gave one. */
  reason?: string | null;
}

/**
 * The classifier's confidence in a page's or section's Class, plus (for pages)
 * the model's reasoning behind it, rendered beside the class itself.
 *
 * Renders NOTHING when there is no confidence and no reason. That is the common
 * case: a score exists only when the classification prompt asks the model for
 * one and it answers (see docs/classification.md), and several paths never
 * produce one. Absence therefore has to read as absence — a "100%" or "0%"
 * placeholder would invent a certainty the classifier never expressed, which is
 * exactly the defect this replaces (GitHub #673).
 *
 * The percentage is deliberately un-colored: there is no classification
 * confidence threshold to compare against yet, so red/green would imply a
 * judgement the system has not made. `ConfidenceDisplay` handles that by
 * rendering neutrally when no threshold is supplied.
 */
const ClassConfidence = ({ confidence, reason }: ClassConfidenceProps): React.JSX.Element | null => {
  const hasConfidence = typeof confidence === 'number';
  const hasReason = typeof reason === 'string' && reason.trim().length > 0;

  if (!hasConfidence && !hasReason) {
    return null;
  }

  const confidenceEl = hasConfidence ? (
    <ConfidenceDisplay confidenceInfo={{ hasConfidenceInfo: true, confidence }} variant="inline" showThreshold={false} />
  ) : null;

  if (!hasReason) {
    return confidenceEl;
  }

  return (
    <Popover
      dismissButton={false}
      position="top"
      size="large"
      triggerType="custom"
      header="Why this class?"
      content={
        <SpaceBetween size="xs">
          <Box variant="p">{reason}</Box>
          <Box variant="small" color="text-body-secondary">
            The classifier&apos;s own explanation, recorded at classification time.
          </Box>
        </SpaceBetween>
      }
    >
      <span style={{ cursor: 'pointer', textDecoration: 'underline dotted' }}>
        {confidenceEl ?? (
          <Box variant="small" color="text-body-secondary" display="inline">
            why?
          </Box>
        )}
      </span>
    </Popover>
  );
};

export default ClassConfidence;
