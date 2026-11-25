// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
//
// The reason of an outcome that sends a document to a review queue: the text the
// reviewer reads. A placeholder in braces is replaced, when the document is placed,
// with the value the document has; "Insert value" writes one where the cursor is. The
// server checks the placeholders and the size when the configuration is saved.
import React, { useRef } from 'react';
import {
  Box,
  ButtonDropdown,
  FormField,
  SpaceBetween,
  Textarea,
  type ButtonDropdownProps,
  type TextareaProps,
} from '@cloudscape-design/components';
import { defineMessages, useIntl, type NoMessageValues } from 'react-intl';

import { inputMessage } from './RoutingConditionsEditor';
import {
  CONFIDENCE_THRESHOLD,
  MAX_REASON_PLACEHOLDERS,
  ROUTING_INPUTS,
  reasonPlaceholders,
  routingInput,
  type RoutingCondition,
} from './routing-types';

const messages = defineMessages<{
  label: NoMessageValues;
  description: NoMessageValues;
  bytes: { readonly used: number; readonly max: number };
  tooLong: { readonly max: number };
  insert: NoMessageValues;
  placeholderLimit: { readonly max: number };
  conditionsGroup: NoMessageValues;
  documentGroup: NoMessageValues;
  conditionValue: { readonly position: number };
  confidenceThreshold: NoMessageValues;
  policyNote: NoMessageValues;
}>({
  label: {
    id: 'routing.reason.label',
    defaultMessage: 'Reason for the reviewer (optional)',
    description: 'Label of the field that holds the text a reviewer reads on a document a routing outcome placed in a review queue.',
  },
  description: {
    id: 'routing.reason.description',
    defaultMessage:
      'What the reviewer reads on a document placed in the queue. An inserted value shows what the document has. Without a reason, one is generated.',
    description: 'Help under the label of the reason of a routing outcome.',
  },
  bytes: {
    id: 'routing.reason.bytes',
    defaultMessage: '{used, number} of {max, number} bytes',
    description: 'Under the reason of a routing outcome: how much of the size limit is used. {used} and {max} are numbers of bytes.',
  },
  tooLong: {
    id: 'routing.reason.tooLong',
    defaultMessage: 'The reason is longer than {max, number} bytes.',
    description: 'Error under a reason of a routing outcome that is over the size limit. {max} is the limit in bytes.',
  },
  insert: {
    id: 'routing.reason.insert',
    defaultMessage: 'Insert value',
    description: 'Menu beside the reason of a routing outcome: writes a placeholder for a value of the document where the cursor is.',
  },
  placeholderLimit: {
    id: 'routing.reason.placeholderLimit',
    defaultMessage: 'A reason has at most {max, number} values.',
    description: 'Why the Insert value menu is disabled. {max} is the largest number of placeholders in a reason.',
  },
  conditionsGroup: {
    id: 'routing.reason.conditionsGroup',
    defaultMessage: "This routing rule's conditions",
    description: 'Group of the Insert value menu: what the conditions of the routing rule read and compare with.',
  },
  documentGroup: {
    id: 'routing.reason.documentGroup',
    defaultMessage: 'Document',
    description: 'Group of the Insert value menu: values of the document that need nothing else to be named.',
  },
  conditionValue: {
    id: 'routing.reason.conditionValue',
    defaultMessage: 'Comparison value of condition {position, number}',
    description:
      'Item of the Insert value menu: the value a condition of the routing rule compares its input with, for example the 1000 of "> 1000". {position} is the number of the condition, from 1.',
  },
  confidenceThreshold: {
    id: 'routing.reason.confidenceThreshold',
    defaultMessage: 'Confidence threshold',
    description: 'Item of the Insert value menu: the confidence threshold of the profile.',
  },
  policyNote: {
    id: 'routing.reason.policyNote',
    defaultMessage: 'Failed policy rules are listed for the reviewer automatically, with the reasons policy rule validation gave.',
    description: 'Note under the reason of a routing outcome.',
  },
});

const placeholder = (token: string): string => `{${token}}`;

/** The size of a text as the server counts it. */
export const byteLength = (text: string): number => new TextEncoder().encode(text).length;

interface ReasonFieldProps {
  value: string;
  onChange: (value: string) => void;
  readOnly: boolean;
  /** The conditions of the routing rule, which the menu offers first; none for the default outcome. */
  conditions: readonly RoutingCondition[];
  /** The most bytes a reason holds. */
  maxBytes: number;
}

const ReasonField = ({ value, onChange, readOnly, conditions, maxBytes }: ReasonFieldProps): React.JSX.Element => {
  const intl = useIntl();
  const host = useRef<HTMLDivElement>(null);
  const textarea = useRef<TextareaProps.Ref>(null);
  // Where the cursor was when the field was last used; a value is inserted there. Before
  // the field is used, a value goes at the end.
  const caret = useRef<{ start: number; end: number } | null>(null);

  // The component's ref only focuses, so the element is found for its selection.
  const element = (): HTMLTextAreaElement | null => host.current?.querySelector('textarea') ?? null;
  const recordCaret = (): void => {
    const field = element();
    if (field) caret.current = { start: field.selectionStart, end: field.selectionEnd };
  };

  const insert = (text: string): void => {
    const start = Math.min(caret.current?.start ?? value.length, value.length);
    const end = Math.min(Math.max(caret.current?.end ?? value.length, start), value.length);
    onChange(value.slice(0, start) + text + value.slice(end));
    const position = start + text.length;
    caret.current = { start: position, end: position };
    // Once the new text is in the field, the cursor goes after what was inserted.
    requestAnimationFrame(() => {
      element()?.setSelectionRange(position, position);
      textarea.current?.focus();
    });
  };

  const inputLabel = (name: string): string => {
    const message = inputMessage(name);
    return message ? intl.formatMessage(message) : name;
  };

  // What each condition reads and what it compares with. Two conditions on one input
  // offer it once; a condition that still lacks its field or policy rule offers only
  // its value.
  const offered = new Set<string>();
  const conditionItems: ButtonDropdownProps.Item[] = conditions.flatMap((condition, index) => {
    const items: ButtonDropdownProps.Item[] = [];
    const input = routingInput(condition.input);
    const path = condition.path.trim();
    if (input && !(input.needs_path && !path) && !/[{}]/.test(path)) {
      const id = placeholder(path ? `${input.name}:${path}` : input.name);
      if (!offered.has(id)) {
        offered.add(id);
        items.push({ id, text: [inputLabel(input.name), path].filter(Boolean).join(' '), secondaryText: id });
      }
    }
    const valueId = placeholder(`condition_${index + 1}.value`);
    items.push({ id: valueId, text: intl.formatMessage(messages.conditionValue, { position: index + 1 }), secondaryText: valueId });
    return items;
  });
  const documentItems: ButtonDropdownProps.Item[] = [
    ...ROUTING_INPUTS.filter((input) => !input.needs_path && !offered.has(placeholder(input.name))).map((input) => ({
      id: placeholder(input.name),
      text: inputLabel(input.name),
      secondaryText: placeholder(input.name),
    })),
    {
      id: placeholder(CONFIDENCE_THRESHOLD),
      text: intl.formatMessage(messages.confidenceThreshold),
      secondaryText: placeholder(CONFIDENCE_THRESHOLD),
    },
  ];

  const used = byteLength(value);
  const atPlaceholderLimit = reasonPlaceholders(value).length >= MAX_REASON_PLACEHOLDERS;

  return (
    <SpaceBetween size="xs">
      <FormField
        label={intl.formatMessage(messages.label)}
        description={intl.formatMessage(messages.description)}
        constraintText={intl.formatMessage(messages.bytes, { used, max: maxBytes })}
        errorText={used > maxBytes ? intl.formatMessage(messages.tooLong, { max: maxBytes }) : undefined}
        stretch
        secondaryControl={
          readOnly ? undefined : (
            <ButtonDropdown
              expandToViewport
              disabled={atPlaceholderLimit}
              disabledReason={intl.formatMessage(messages.placeholderLimit, { max: MAX_REASON_PLACEHOLDERS })}
              items={[
                ...(conditionItems.length > 0 ? [{ text: intl.formatMessage(messages.conditionsGroup), items: conditionItems }] : []),
                { text: intl.formatMessage(messages.documentGroup), items: documentItems },
              ]}
              onItemClick={({ detail }) => insert(detail.id)}
            >
              {intl.formatMessage(messages.insert)}
            </ButtonDropdown>
          )
        }
      >
        <div ref={host}>
          <Textarea
            ref={textarea}
            value={value}
            rows={3}
            readOnly={readOnly}
            onChange={({ detail }) => onChange(detail.value)}
            onKeyUp={recordCaret}
            onBlur={recordCaret}
            nativeTextareaAttributes={{ onSelect: recordCaret, onClick: recordCaret }}
          />
        </div>
      </FormField>
      <Box variant="small" color="text-body-secondary">
        {intl.formatMessage(messages.policyNote)}
      </Box>
    </SpaceBetween>
  );
};

export default ReasonField;
