// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
import React from 'react';

/**
 * A step name with a line-break opportunity at each word boundary.
 *
 * Step names are PascalCase with no spaces ("ApplyPreprocessingHookDocument"), so the
 * only place a narrow node could wrap them was mid-word ("ApplyPrepr / ocessingHo /
 * okDocument"). A `<wbr>` before each capital that starts a new word lets the browser
 * wrap between words instead, and changes nothing when the name fits.
 */
export const withWordBreaks = (name: string): React.ReactNode[] => {
  const nodes: React.ReactNode[] = [];
  let offset = 0;
  name.split(/(?<=[a-z0-9])(?=[A-Z])|(?<=[A-Z])(?=[A-Z][a-z])/).forEach((part) => {
    // Keyed by character offset, which is unique and stable for a given name.
    if (offset > 0) nodes.push(<wbr key={`wbr-${offset}`} />);
    nodes.push(part);
    offset += part.length;
  });
  return nodes;
};
