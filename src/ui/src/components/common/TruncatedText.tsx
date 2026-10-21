// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
import React from 'react';

interface TruncatedTextProps {
  children: string;
  /** Characters shown before the text is cut off; without it, the text fills its column. */
  maxChars?: number;
  bold?: boolean;
}

/** One line of text, cut off with an ellipsis (past `maxChars`, or at the column edge); the full text shows on hover. */
const TruncatedText = ({ children, maxChars, bold = false }: TruncatedTextProps): React.JSX.Element => (
  <span
    title={children}
    style={{
      display: maxChars ? 'inline-block' : 'block',
      maxWidth: maxChars ? `${maxChars}ch` : '100%',
      overflow: 'hidden',
      textOverflow: 'ellipsis',
      whiteSpace: 'nowrap',
      verticalAlign: 'bottom',
      fontWeight: bold ? 'bold' : undefined,
    }}
  >
    {children}
  </span>
);

export default TruncatedText;
