// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
import React from 'react';

interface TruncatedTextProps {
  children: string;
  /** Characters shown before the text is cut off; without it, the text fills its column. */
  maxChars?: number;
  /** The hover text (defaults to the full `children`). */
  title?: string;
  /** Where the text is cut: at the end (CSS ellipsis), or in the middle (keeps the start and end; needs `maxChars`). */
  ellipsis?: 'end' | 'middle';
  /** `true` bold, `false` normal weight, unset inherits the surrounding weight. */
  bold?: boolean;
}

/** `text` cut to `maxChars` characters by replacing its middle with an ellipsis. */
const middleEllipsis = (text: string, maxChars: number): string => {
  if (text.length <= maxChars) return text;
  const keep = Math.max(maxChars - 1, 2);
  const head = Math.ceil(keep / 2);
  return `${text.slice(0, head)}…${text.slice(text.length - (keep - head))}`;
};

const fontWeight = (bold: boolean | undefined): React.CSSProperties['fontWeight'] => {
  if (bold === undefined) return undefined;
  return bold ? 'bold' : 'normal';
};

/** One line of text, cut off with an ellipsis (past `maxChars`, or at the column edge); the full text shows on hover. */
const TruncatedText = ({ children, maxChars, title, ellipsis = 'end', bold }: TruncatedTextProps): React.JSX.Element => (
  <span
    title={title ?? children}
    style={{
      display: maxChars ? 'inline-block' : 'block',
      maxWidth: maxChars ? `${maxChars}ch` : '100%',
      overflow: 'hidden',
      textOverflow: 'ellipsis',
      whiteSpace: 'nowrap',
      verticalAlign: 'bottom',
      fontWeight: fontWeight(bold),
    }}
  >
    {ellipsis === 'middle' && maxChars ? middleEllipsis(children, maxChars) : children}
  </span>
);

export default TruncatedText;
