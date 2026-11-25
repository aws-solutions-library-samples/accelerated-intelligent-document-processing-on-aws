// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
//
// The conditions of one routing rule. Each names an input, what the input reads when it
// has to be told (a field, a policy rule, a policy type, a class), how a list of values
// is read, and the value the input is compared with. All conditions must hold.
//
// Each condition is a box of its own: its number (the one a reason's placeholder names)
// with the menu of actions at the top right, then two lines of labelled fields: what is
// tested, then its value. Only the controls the chosen input uses are shown. The value
// is entered with guided controls when its test has one of the shapes they show, and as
// a DMN expression otherwise. The stored test changes only when one of its own controls
// is edited.
import React, { useEffect, useRef, useState } from 'react';
import {
  Box,
  Button,
  ButtonDropdown,
  Container,
  FormField,
  Grid,
  Input,
  Multiselect,
  SegmentedControl,
  Select,
  SpaceBetween,
  TokenGroup,
  type ButtonDropdownProps,
  type ButtonProps,
  type SelectProps,
} from '@cloudscape-design/components';
import { arrayMove } from '@dnd-kit/sortable';
import { defineMessages, useIntl, type MessageDescriptor, type NoMessageValues } from 'react-intl';

import {
  NUMBER_OPERATORS,
  formatGuided,
  guidedKind,
  isNumberText,
  parseGuided,
  type GuidedKind,
  type NumberOperator,
} from './routing-guided';
import { classNames, fieldPathsByClass, fieldType, policyRuleIds, policyTypes } from './routing-paths';
import {
  AGGREGATES,
  DEFAULT_AGGREGATE,
  MAX_CONDITIONS,
  ROUTING_INPUTS,
  routingInput,
  type PathKind,
  type RoutingCondition,
  type RoutingInput,
} from './routing-types';

const messages = defineMessages<{
  add: NoMessageValues;
  empty: NoMessageValues;
  limit: { readonly max: number };
  title: { readonly position: number };
  valueLabel: NoMessageValues;
  valueNoInput: NoMessageValues;
  inputColumn: NoMessageValues;
  inputPlaceholder: NoMessageValues;
  inputAria: { readonly position: number };
  pathField: NoMessageValues;
  pathPolicyRule: NoMessageValues;
  pathPolicyType: NoMessageValues;
  pathClass: NoMessageValues;
  pathPlaceholder: NoMessageValues;
  pathAll: NoMessageValues;
  pathAria: { readonly position: number };
  pathRequired: NoMessageValues;
  pathEmpty: NoMessageValues;
  aggregateColumn: NoMessageValues;
  aggregateAria: { readonly position: number };
  testAria: { readonly position: number };
  testRequired: NoMessageValues;
  inputRequired: NoMessageValues;
  examples: { readonly first: string; readonly second: string };
  actionsAria: { readonly position: number };
  duplicate: NoMessageValues;
  moveUp: NoMessageValues;
  moveDown: NoMessageValues;
  remove: NoMessageValues;
}>({
  add: { id: 'routing.conditions.add', defaultMessage: 'Add condition', description: 'Button that adds a condition to a routing rule.' },
  empty: {
    id: 'routing.conditions.empty',
    defaultMessage: 'No conditions. The routing rule matches every document.',
    description: 'Shown in the list of conditions of a routing rule when it has none.',
  },
  limit: {
    id: 'routing.conditions.limit',
    defaultMessage: 'A routing rule has at most {max, number} conditions.',
    description: 'Shown under the conditions of a routing rule once it has the most allowed. {max} is that number.',
  },
  title: {
    id: 'routing.conditions.title',
    defaultMessage: 'Condition {position, number}',
    description:
      'Title of the box of one condition of a routing rule. {position} is the number of the condition, from 1: the number a placeholder of the reason names.',
  },
  valueLabel: {
    id: 'routing.conditions.valueLabel',
    defaultMessage: 'Value',
    description: 'Label of the yes or no choice (or present or missing) that is the value of a routing condition.',
  },
  valueNoInput: {
    id: 'routing.conditions.valueNoInput',
    defaultMessage: 'Choose an input to enter its value.',
    description: 'Shown in place of the value of a routing condition that has no input chosen yet.',
  },
  inputColumn: {
    id: 'routing.conditions.inputColumn',
    defaultMessage: 'Input',
    description: 'Field label: what a routing condition tests.',
  },
  inputPlaceholder: {
    id: 'routing.conditions.inputPlaceholder',
    defaultMessage: 'Choose an input',
    description: 'Placeholder of the list of things a routing condition can test.',
  },
  inputAria: {
    id: 'routing.conditions.inputAria',
    defaultMessage: 'Input of condition {position, number}',
    description: 'Accessible name of the input list of one condition. {position} is the number of the condition, from 1.',
  },
  pathField: {
    id: 'routing.conditions.pathField',
    defaultMessage: 'Field',
    description: 'Field label: the extracted field a routing condition reads.',
  },
  pathPolicyRule: {
    id: 'routing.conditions.pathPolicyRule',
    defaultMessage: 'Policy rule',
    description: 'Field label: the policy rule whose verdict a routing condition reads.',
  },
  pathPolicyType: {
    id: 'routing.conditions.pathPolicyType',
    defaultMessage: 'Policy type',
    description: 'Field label: the policy type whose failed policy rules a routing condition counts.',
  },
  pathClass: {
    id: 'routing.conditions.pathClass',
    defaultMessage: 'Class',
    description: 'Field label: the document class a routing condition looks for among the sections of the document.',
  },
  pathPlaceholder: {
    id: 'routing.conditions.pathPlaceholder',
    defaultMessage: 'Choose',
    description: 'Placeholder of the list of fields, policy rules, policy types or classes of a routing condition.',
  },
  pathAll: {
    id: 'routing.conditions.pathAll',
    defaultMessage: 'All policy types',
    description: 'Option that leaves the policy type empty, so the condition counts failed policy rules of every policy type.',
  },
  pathAria: {
    id: 'routing.conditions.pathAria',
    defaultMessage: 'What the input of condition {position, number} reads',
    description:
      'Accessible name of the list of fields, policy rules, policy types or classes of one condition. {position} is the number of the condition, from 1.',
  },
  pathRequired: {
    id: 'routing.conditions.pathRequired',
    defaultMessage: 'Choose what this input reads.',
    description: 'Error under the list of a condition whose input needs a field, a policy rule or a class and has none.',
  },
  pathEmpty: {
    id: 'routing.conditions.pathEmpty',
    defaultMessage: 'This profile has nothing to choose from',
    description: 'Shown in a list of a routing condition when the profile has no fields, policy rules, policy types or classes to offer.',
  },
  aggregateColumn: {
    id: 'routing.conditions.aggregateColumn',
    defaultMessage: 'Match',
    description: 'Field label: how a condition on a list of values is read (any value, all values, or their count).',
  },
  aggregateAria: {
    id: 'routing.conditions.aggregateAria',
    defaultMessage: 'Match of condition {position, number}',
    description:
      'Accessible name of the list that says how a condition reads a list of values. {position} is the number of the condition, from 1.',
  },
  testAria: {
    id: 'routing.conditions.testAria',
    defaultMessage: 'Expression of condition {position, number}',
    description:
      'Accessible name of the field that holds the value of one condition as an expression. {position} is the number of the condition, from 1.',
  },
  testRequired: {
    id: 'routing.conditions.testRequired',
    defaultMessage: 'Enter a value.',
    description: 'Error under a routing condition whose value is empty or incomplete.',
  },
  inputRequired: {
    id: 'routing.conditions.inputRequired',
    defaultMessage: 'Choose an input.',
    description: 'Error under the input list of a routing condition with no input chosen.',
  },
  examples: {
    id: 'routing.conditions.examples',
    defaultMessage: 'For example {first} or {second}',
    description:
      'Help under the value of a routing condition edited as an expression. {first} and {second} are two example expressions for the type of the chosen input.',
  },
  actionsAria: {
    id: 'routing.conditions.actionsAria',
    defaultMessage: 'Actions for condition {position, number}',
    description: 'Accessible name of the menu of actions on one condition. {position} is the number of the condition, from 1.',
  },
  duplicate: {
    id: 'routing.conditions.duplicate',
    defaultMessage: 'Duplicate',
    description: 'Menu item that adds a copy of a routing condition under it.',
  },
  moveUp: {
    id: 'routing.conditions.moveUp',
    defaultMessage: 'Move up',
    description: 'Menu item that moves a routing condition one place up.',
  },
  moveDown: {
    id: 'routing.conditions.moveDown',
    defaultMessage: 'Move down',
    description: 'Menu item that moves a routing condition one place down.',
  },
  remove: {
    id: 'routing.conditions.remove',
    defaultMessage: 'Remove',
    description: 'Menu item that removes one condition of a routing rule.',
  },
});

/** The text of the guided controls of a condition's value. */
const guidedMessages = defineMessages<{
  editAsExpression: NoMessageValues;
  expressionLabel: NoMessageValues;
  useGuided: NoMessageValues;
  cannotShow: NoMessageValues;
  notAvailable: NoMessageValues;
  valueAria: { readonly position: number };
  yes: NoMessageValues;
  no: NoMessageValues;
  present: NoMessageValues;
  missing: NoMessageValues;
  comparisonLabel: NoMessageValues;
  comparisonAria: { readonly position: number };
  isOneOf: NoMessageValues;
  isNotOneOf: NoMessageValues;
  valuesLabel: NoMessageValues;
  valuesAria: { readonly position: number };
  valuesPlaceholder: NoMessageValues;
  valuesEmpty: NoMessageValues;
  typedPlaceholder: NoMessageValues;
  removeValue: { readonly value: string };
  equals: NoMessageValues;
  notEquals: NoMessageValues;
  greater: NoMessageValues;
  greaterOrEqual: NoMessageValues;
  less: NoMessageValues;
  lessOrEqual: NoMessageValues;
  between: NoMessageValues;
  numberLabel: NoMessageValues;
  numberAria: { readonly position: number };
  fromLabel: NoMessageValues;
  toLabel: NoMessageValues;
  toAria: { readonly position: number };
}>({
  editAsExpression: {
    id: 'routing.guided.editAsExpression',
    defaultMessage: 'Edit as expression',
    description: 'Link under the value of a routing condition: shows the value as the DMN expression that is stored, to edit it as text.',
  },
  expressionLabel: {
    id: 'routing.guided.expressionLabel',
    defaultMessage: 'Expression',
    description: 'Label of the field that holds the value of a routing condition as a DMN expression.',
  },
  useGuided: {
    id: 'routing.guided.useGuided',
    defaultMessage: 'Use guided controls',
    description: 'Link under the value of a routing condition edited as an expression: goes back to the pick lists and number fields.',
  },
  cannotShow: {
    id: 'routing.guided.cannotShow',
    defaultMessage: 'The guided controls cannot show this expression.',
    description: 'Why the link back to the guided controls is disabled: the expression is not one of the shapes they show.',
  },
  notAvailable: {
    id: 'routing.guided.notAvailable',
    defaultMessage: 'This value has no guided controls.',
    description: 'Why the link to the guided controls is disabled: the input, or the type of its field, is edited as an expression only.',
  },
  valueAria: {
    id: 'routing.guided.valueAria',
    defaultMessage: 'Value of condition {position, number}',
    description: 'Accessible name of the yes or no choice of one condition. {position} is the number of the condition, from 1.',
  },
  yes: { id: 'routing.guided.yes', defaultMessage: 'Yes', description: 'Value of a routing condition on a yes or no input: yes.' },
  no: { id: 'routing.guided.no', defaultMessage: 'No', description: 'Value of a routing condition on a yes or no input: no.' },
  present: {
    id: 'routing.guided.present',
    defaultMessage: 'Present',
    description: 'Value of a routing condition on a class: the document has a section of the class.',
  },
  missing: {
    id: 'routing.guided.missing',
    defaultMessage: 'Missing',
    description: 'Value of a routing condition on a class: the document has no section of the class.',
  },
  comparisonLabel: {
    id: 'routing.guided.comparisonLabel',
    defaultMessage: 'Comparison',
    description: 'Field label: how the input of a routing condition is compared with the value (is one of, greater than, between).',
  },
  comparisonAria: {
    id: 'routing.guided.comparisonAria',
    defaultMessage: 'Comparison of condition {position, number}',
    description: 'Accessible name of the list of comparisons of one condition. {position} is the number of the condition, from 1.',
  },
  isOneOf: {
    id: 'routing.guided.isOneOf',
    defaultMessage: 'Is one of',
    description: 'Comparison of a routing condition: the input equals one of the chosen values.',
  },
  isNotOneOf: {
    id: 'routing.guided.isNotOneOf',
    defaultMessage: 'Is none of',
    description: 'Comparison of a routing condition: the input equals none of the chosen values.',
  },
  valuesLabel: {
    id: 'routing.guided.valuesLabel',
    defaultMessage: 'Values',
    description: 'Field label: the values the input of a routing condition is compared with.',
  },
  valuesAria: {
    id: 'routing.guided.valuesAria',
    defaultMessage: 'Values of condition {position, number}',
    description: 'Accessible name of the list or field of values of one condition. {position} is the number of the condition, from 1.',
  },
  valuesPlaceholder: {
    id: 'routing.guided.valuesPlaceholder',
    defaultMessage: 'Choose values',
    description: 'Placeholder of the list of values of a routing condition.',
  },
  valuesEmpty: {
    id: 'routing.guided.valuesEmpty',
    defaultMessage: 'This profile has no values to choose from',
    description: 'Shown in the list of values of a routing condition when the profile has none to offer (no document classes).',
  },
  typedPlaceholder: {
    id: 'routing.guided.typedPlaceholder',
    defaultMessage: 'Type a value and press Enter',
    description: 'Placeholder of the field where the values of a routing condition on a text field are typed, one at a time.',
  },
  removeValue: {
    id: 'routing.guided.removeValue',
    defaultMessage: 'Remove {value}',
    description: 'Accessible name of the button that removes one typed value of a routing condition. {value} is the value.',
  },
  equals: { id: 'routing.guided.equals', defaultMessage: 'Equals', description: 'Comparison of a routing condition on a number.' },
  notEquals: {
    id: 'routing.guided.notEquals',
    defaultMessage: 'Does not equal',
    description: 'Comparison of a routing condition on a number.',
  },
  greater: {
    id: 'routing.guided.greater',
    defaultMessage: 'Greater than',
    description: 'Comparison of a routing condition on a number.',
  },
  greaterOrEqual: {
    id: 'routing.guided.greaterOrEqual',
    defaultMessage: 'At least',
    description: 'Comparison of a routing condition on a number: greater than or equal to.',
  },
  less: { id: 'routing.guided.less', defaultMessage: 'Less than', description: 'Comparison of a routing condition on a number.' },
  lessOrEqual: {
    id: 'routing.guided.lessOrEqual',
    defaultMessage: 'At most',
    description: 'Comparison of a routing condition on a number: less than or equal to.',
  },
  between: {
    id: 'routing.guided.between',
    defaultMessage: 'Between',
    description: 'Comparison of a routing condition on a number: from one number to another, both included.',
  },
  numberLabel: {
    id: 'routing.guided.numberLabel',
    defaultMessage: 'Number',
    description: 'Field label: the number the input of a routing condition is compared with.',
  },
  numberAria: {
    id: 'routing.guided.numberAria',
    defaultMessage: 'Number of condition {position, number}',
    description:
      'Accessible name of the number field of one condition (the lower number of a range). {position} is the number of the condition, from 1.',
  },
  fromLabel: {
    id: 'routing.guided.fromLabel',
    defaultMessage: 'From',
    description: 'Field label: the lower number of the range a routing condition tests, included.',
  },
  toLabel: {
    id: 'routing.guided.toLabel',
    defaultMessage: 'To',
    description: 'Field label: the upper number of the range a routing condition tests, included.',
  },
  toAria: {
    id: 'routing.guided.toAria',
    defaultMessage: 'Upper number of condition {position, number}',
    description:
      'Accessible name of the upper number field of a range of one condition. {position} is the number of the condition, from 1.',
  },
});

/** One message per routing input (the name in lower camel case). */
const inputMessages = defineMessages<{
  documentClass: NoMessageValues;
  sectionClasses: NoMessageValues;
  classPresent: NoMessageValues;
  pageCount: NoMessageValues;
  sectionCount: NoMessageValues;
  confidenceAlertCount: NoMessageValues;
  missingRequiredFieldCount: NoMessageValues;
  extractionFailedCount: NoMessageValues;
  routeMissingRequiredFields: NoMessageValues;
  fieldValue: NoMessageValues;
  policyRuleVerdict: NoMessageValues;
  failedPolicyRuleCount: NoMessageValues;
  reviewed: NoMessageValues;
  processingMode: NoMessageValues;
}>({
  documentClass: {
    id: 'routing.input.documentClass',
    defaultMessage: 'Document class',
    description: 'Routing input: the class of the document.',
  },
  sectionClasses: {
    id: 'routing.input.sectionClasses',
    defaultMessage: 'Section classes',
    description: 'Routing input: the classes of the sections of the document.',
  },
  classPresent: {
    id: 'routing.input.classPresent',
    defaultMessage: 'Class is present',
    description: 'Routing input: whether the document has a section of the class the condition names.',
  },
  pageCount: {
    id: 'routing.input.pageCount',
    defaultMessage: 'Page count',
    description: 'Routing input: the number of pages of the document.',
  },
  sectionCount: {
    id: 'routing.input.sectionCount',
    defaultMessage: 'Section count',
    description: 'Routing input: the number of sections of the document.',
  },
  confidenceAlertCount: {
    id: 'routing.input.confidenceAlertCount',
    defaultMessage: 'Confidence alert count',
    description: 'Routing input: the number of extracted fields whose confidence is under their threshold.',
  },
  missingRequiredFieldCount: {
    id: 'routing.input.missingRequiredFieldCount',
    defaultMessage: 'Missing required field count',
    description: 'Routing input: the number of required fields that have no extracted value.',
  },
  extractionFailedCount: {
    id: 'routing.input.extractionFailedCount',
    defaultMessage: 'Failed extraction count',
    description: 'Routing input: the number of sections whose extraction failed, which have no extracted values.',
  },
  routeMissingRequiredFields: {
    id: 'routing.input.routeMissingRequiredFields',
    defaultMessage: 'Route missing required fields (setting)',
    description: 'Routing input: the profile setting that says a document with missing required fields goes to review.',
  },
  fieldValue: {
    id: 'routing.input.fieldValue',
    defaultMessage: 'Field value',
    description: 'Routing input: the extracted value of a field.',
  },
  policyRuleVerdict: {
    id: 'routing.input.policyRuleVerdict',
    defaultMessage: 'Policy rule verdict',
    description: 'Routing input: the verdict of one policy rule.',
  },
  failedPolicyRuleCount: {
    id: 'routing.input.failedPolicyRuleCount',
    defaultMessage: 'Failed policy rule count',
    description: 'Routing input: the number of policy rules that failed, of one policy type or of all.',
  },
  reviewed: {
    id: 'routing.input.reviewed',
    defaultMessage: 'Reviewed',
    description: 'Routing input: whether a review of the document has been completed.',
  },
  processingMode: {
    id: 'routing.input.processingMode',
    defaultMessage: 'Processing mode',
    description: 'Routing input: BDA mode or Pipeline mode.',
  },
});

/** One message per way of reading a list of values. */
const aggregateMessages = defineMessages<{ any: NoMessageValues; all: NoMessageValues; count: NoMessageValues }>({
  any: {
    id: 'routing.aggregate.any',
    defaultMessage: 'Any value',
    description: 'How a condition reads a list: it holds when any value passes the test.',
  },
  all: {
    id: 'routing.aggregate.all',
    defaultMessage: 'All values',
    description: 'How a condition reads a list: it holds when every value passes the test.',
  },
  count: {
    id: 'routing.aggregate.count',
    defaultMessage: 'Number of values',
    description: 'How a condition reads a list: the test is applied to how many values there are.',
  },
});

const messageKey = (value: string): string => value.toLowerCase().replace(/_([a-z0-9])/g, (_match, letter: string) => letter.toUpperCase());

export const inputMessage = (name: string): MessageDescriptor | undefined =>
  (inputMessages as Record<string, MessageDescriptor>)[messageKey(name)];

export const aggregateMessage = (name: string): MessageDescriptor | undefined =>
  (aggregateMessages as Record<string, MessageDescriptor>)[name];

/** Two example tests for a type of value. They are DMN unary tests, not translated. */
const TEST_EXAMPLES: Record<string, [string, string]> = {
  string: ['"Invoice"', '"Invoice","Receipt"'],
  number: ['> 0', '[10..20]'],
  boolean: ['true', 'false'],
  field: ['> 1000', '"Approved"'],
};

/** The label of the list of what an input reads, by the kind of thing it reads. */
const PATH_LABELS: Record<Exclude<PathKind, ''>, MessageDescriptor> = {
  field: messages.pathField,
  policy_rule: messages.pathPolicyRule,
  policy_type: messages.pathPolicyType,
  class: messages.pathClass,
};

/** The inputs whose values are document classes of the profile. */
const CLASS_VALUE_INPUTS: readonly string[] = ['document_class', 'section_classes'];

const NUMBER_COMPARISONS: Record<NumberOperator, MessageDescriptor> = {
  '=': guidedMessages.equals,
  '!=': guidedMessages.notEquals,
  '>': guidedMessages.greater,
  '>=': guidedMessages.greaterOrEqual,
  '<': guidedMessages.less,
  '<=': guidedMessages.lessOrEqual,
};

/** What is wrong with a condition, as message descriptors; nothing when it can be saved. */
export interface ConditionProblems {
  input?: MessageDescriptor;
  path?: MessageDescriptor;
  test?: MessageDescriptor;
}

export const conditionProblems = (condition: RoutingCondition): ConditionProblems => {
  const input = routingInput(condition.input);
  if (!input) return { input: messages.inputRequired };
  const problems: ConditionProblems = {};
  if (input.needs_path && !condition.path.trim()) problems.path = messages.pathRequired;
  if (!condition.test.trim()) problems.test = messages.testRequired;
  return problems;
};

const BETWEEN = 'between';

/** What the number controls hold. It is a test only once every number is entered. */
interface NumberDraft {
  comparison: NumberOperator | typeof BETWEEN;
  /** The number compared with; the lower number of a range. */
  value: string;
  /** The upper number of a range. */
  high: string;
}

const numberDraft = (test: string): NumberDraft => {
  const parsed = parseGuided(test, 'number');
  if (parsed?.kind === 'number') return { comparison: parsed.op, value: parsed.value, high: '' };
  if (parsed?.kind === 'between') return { comparison: BETWEEN, value: parsed.low, high: parsed.high };
  return { comparison: '>', value: '', high: '' };
};

const numberTest = (draft: NumberDraft): string => {
  if (!isNumberText(draft.value)) return '';
  if (draft.comparison !== BETWEEN) return formatGuided({ kind: 'number', op: draft.comparison, value: draft.value });
  return isNumberText(draft.high) ? formatGuided({ kind: 'between', low: draft.value, high: draft.high }) : '';
};

/** What the choice controls hold. It is a test once it has a value. */
interface ChoiceDraft {
  values: string[];
  negated: boolean;
}

const choiceDraft = (test: string): ChoiceDraft => {
  const parsed = parseGuided(test, 'choice');
  return parsed?.kind === 'choice' ? { values: parsed.values, negated: parsed.negated } : { values: [], negated: false };
};

const choiceTest = (draft: ChoiceDraft): string => (draft.values.length > 0 ? formatGuided({ kind: 'choice', ...draft }) : '');

/**
 * What the controls of one value hold that is not part of the stored test: whether the
 * value is shown as an expression, and what the guided controls hold while that is not
 * yet a complete test (a comparison chosen before its number, a value half typed).
 */
interface ValueState {
  /** The input and the kind of control this state is for; it is not used for another. */
  scope: string;
  expression: boolean;
  number: NumberDraft;
  choice: ChoiceDraft;
  /** A value being typed, not yet entered. */
  typed: string;
}

/** The guided controls show an empty test (nothing entered yet) and a test they can read. */
const guidedFits = (test: string, kind: GuidedKind | null): boolean => kind !== null && (!test.trim() || parseGuided(test, kind) !== null);

/** The state of a value nobody has edited: guided when the controls can show the test, read from the test. */
const valueStateOf = (scope: string, test: string, kind: GuidedKind | null): ValueState => ({
  scope,
  expression: !guidedFits(test, kind),
  number: numberDraft(test),
  choice: choiceDraft(test),
  typed: '',
});

interface ConditionValueProps {
  /** The stored test of the condition. */
  test: string;
  /** The number of the condition, from 1, for accessible names. */
  position: number;
  /** The kind of guided control the value has; null when it is an expression only. */
  kind: GuidedKind | null;
  /** The values a choice offers; null when its values are typed. */
  options: readonly string[] | null;
  /** True when yes and no read as present and missing. */
  presence: boolean;
  /** The two examples shown under an expression; none when the type has no examples. */
  examples?: [string, string];
  readOnly: boolean;
  /** What is wrong with the value, shown under the field that holds it. */
  errorText?: string;
  state: ValueState;
  /** Called with the state to keep and, when a control changed the value, the test to store. */
  onChange: (state: ValueState, test?: string) => void;
}

/**
 * The value of one condition: guided controls, or the test as an expression. The test
 * is passed to `onChange` only by the control that was edited, so opening a routing
 * rule and saving it stores every test as it was.
 */
const ConditionValue = ({
  test,
  position,
  kind,
  options,
  presence,
  examples,
  readOnly,
  errorText,
  state,
  onChange,
}: ConditionValueProps): React.JSX.Element => {
  const intl = useIntl();
  const { number, choice } = state;

  const changeNumber = (changed: Partial<NumberDraft>): void => {
    const next = { ...number, ...changed };
    onChange({ ...state, number: next }, numberTest(next));
  };
  const changeChoice = (changed: Partial<ChoiceDraft>): void => {
    const next = { ...choice, ...changed };
    onChange({ ...state, choice: next, typed: '' }, choiceTest(next));
  };
  const addTyped = (): void => {
    const value = state.typed.trim();
    if (value && !choice.values.includes(value)) changeChoice({ values: [...choice.values, value] });
    else if (state.typed) onChange({ ...state, typed: '' });
  };

  const comparisonField = (offered: SelectProps.Option[], selected: string, onSelect: (value: string) => void): React.JSX.Element => (
    <FormField label={intl.formatMessage(guidedMessages.comparisonLabel)} stretch>
      <Select
        selectedOption={offered.find((option) => option.value === selected) ?? null}
        options={offered}
        readOnly={readOnly}
        ariaLabel={intl.formatMessage(guidedMessages.comparisonAria, { position })}
        onChange={({ detail }) => onSelect(detail.selectedOption.value ?? selected)}
      />
    </FormField>
  );

  let control: React.JSX.Element;
  if (state.expression || kind === null) {
    control = (
      <FormField
        stretch
        label={intl.formatMessage(guidedMessages.expressionLabel)}
        errorText={errorText}
        constraintText={examples ? intl.formatMessage(messages.examples, { first: examples[0], second: examples[1] }) : undefined}
      >
        <Input
          value={test}
          readOnly={readOnly}
          ariaLabel={intl.formatMessage(messages.testAria, { position })}
          // The state is kept as it is, so the value stays an expression while it is
          // typed, whatever the text reads as on the way.
          onChange={({ detail }) => onChange(state, detail.value)}
        />
      </FormField>
    );
  } else if (kind === 'boolean') {
    const parsed = parseGuided(test, 'boolean');
    control = (
      <FormField stretch label={intl.formatMessage(messages.valueLabel)} errorText={errorText}>
        <SegmentedControl
          selectedId={parsed?.kind === 'boolean' ? String(parsed.value) : null}
          label={intl.formatMessage(guidedMessages.valueAria, { position })}
          options={[
            { id: 'true', text: intl.formatMessage(presence ? guidedMessages.present : guidedMessages.yes), disabled: readOnly },
            { id: 'false', text: intl.formatMessage(presence ? guidedMessages.missing : guidedMessages.no), disabled: readOnly },
          ]}
          onChange={({ detail }) => onChange(state, formatGuided({ kind: 'boolean', value: detail.selectedId === 'true' }))}
        />
      </FormField>
    );
  } else if (kind === 'choice') {
    // A value the profile does not offer (a class since removed, a value typed in an
    // expression) stays chosen and can be removed.
    const offered = options ? [...choice.values.filter((value) => !options.includes(value)), ...options] : [];
    const valueOptions = offered.map((value) => ({ value, label: value }));
    control = (
      <Grid gridDefinition={[{ colspan: 4 }, { colspan: 8 }]}>
        {comparisonField(
          [
            { value: 'is', label: intl.formatMessage(guidedMessages.isOneOf) },
            { value: 'not', label: intl.formatMessage(guidedMessages.isNotOneOf) },
          ],
          choice.negated ? 'not' : 'is',
          (value) => changeChoice({ negated: value === 'not' }),
        )}
        <FormField label={intl.formatMessage(guidedMessages.valuesLabel)} errorText={errorText} stretch>
          {options ? (
            <Multiselect
              selectedOptions={choice.values.map((value) => ({ value, label: value }))}
              options={valueOptions}
              readOnly={readOnly}
              keepOpen
              filteringType="auto"
              placeholder={intl.formatMessage(guidedMessages.valuesPlaceholder)}
              empty={intl.formatMessage(guidedMessages.valuesEmpty)}
              ariaLabel={intl.formatMessage(guidedMessages.valuesAria, { position })}
              deselectAriaLabel={(option) => intl.formatMessage(guidedMessages.removeValue, { value: option.label ?? '' })}
              onChange={({ detail }) =>
                changeChoice({ values: detail.selectedOptions.flatMap((option) => (option.value === undefined ? [] : [option.value])) })
              }
            />
          ) : (
            <SpaceBetween size="xs">
              {!readOnly && (
                <Input
                  value={state.typed}
                  placeholder={intl.formatMessage(guidedMessages.typedPlaceholder)}
                  ariaLabel={intl.formatMessage(guidedMessages.valuesAria, { position })}
                  onChange={({ detail }) => onChange({ ...state, typed: detail.value })}
                  onKeyDown={({ detail }) => {
                    if (detail.key === 'Enter') addTyped();
                  }}
                  // A value typed and not entered is kept when the field is left, so that
                  // saving the routing rule does not drop it.
                  onBlur={addTyped}
                />
              )}
              <TokenGroup
                items={choice.values.map((value) => ({
                  label: value,
                  dismissLabel: intl.formatMessage(guidedMessages.removeValue, { value }),
                }))}
                readOnly={readOnly}
                onDismiss={({ detail }) => changeChoice({ values: choice.values.filter((_value, index) => index !== detail.itemIndex) })}
              />
            </SpaceBetween>
          )}
        </FormField>
      </Grid>
    );
  } else {
    const between = number.comparison === BETWEEN;
    const comparisons: SelectProps.Option[] = [
      ...NUMBER_OPERATORS.map((operator) => ({ value: operator, label: intl.formatMessage(NUMBER_COMPARISONS[operator]) })),
      { value: BETWEEN, label: intl.formatMessage(guidedMessages.between) },
    ];
    control = (
      <Grid gridDefinition={between ? [{ colspan: 4 }, { colspan: 4 }, { colspan: 4 }] : [{ colspan: 4 }, { colspan: 5 }]}>
        {comparisonField(comparisons, number.comparison, (value) => changeNumber({ comparison: value as NumberDraft['comparison'] }))}
        <FormField
          label={intl.formatMessage(between ? guidedMessages.fromLabel : guidedMessages.numberLabel)}
          errorText={errorText}
          stretch
        >
          <Input
            type="number"
            inputMode="decimal"
            value={number.value}
            readOnly={readOnly}
            ariaLabel={intl.formatMessage(guidedMessages.numberAria, { position })}
            onChange={({ detail }) => changeNumber({ value: detail.value })}
          />
        </FormField>
        {between && (
          <FormField label={intl.formatMessage(guidedMessages.toLabel)} stretch>
            <Input
              type="number"
              inputMode="decimal"
              value={number.high}
              readOnly={readOnly}
              ariaLabel={intl.formatMessage(guidedMessages.toAria, { position })}
              onChange={({ detail }) => changeNumber({ high: detail.value })}
            />
          </FormField>
        )}
      </Grid>
    );
  }

  return (
    <SpaceBetween size="xs">
      {control}
      {!readOnly &&
        (state.expression ? (
          <Button
            variant="inline-link"
            disabled={!guidedFits(test, kind)}
            disabledReason={intl.formatMessage(kind === null ? guidedMessages.notAvailable : guidedMessages.cannotShow)}
            // The controls show the test as it is now, whatever they held before.
            onClick={() => onChange({ ...valueStateOf(state.scope, test, kind), expression: false })}
          >
            {intl.formatMessage(guidedMessages.useGuided)}
          </Button>
        ) : (
          <Button variant="inline-link" onClick={() => onChange({ ...state, expression: true })}>
            {intl.formatMessage(guidedMessages.editAsExpression)}
          </Button>
        ))}
    </SpaceBetween>
  );
};

/**
 * The columns of the first line of a condition, of twelve: the input, then what it
 * reads and how a list is matched, for the inputs that have them. The input and the
 * field after it start where the two fields of the value line start.
 */
const testGrid = (input: RoutingInput | undefined): { colspan: number }[] => {
  const columns = [{ colspan: 4 }];
  if (input?.path_kind) columns.push({ colspan: input.is_list ? 5 : 8 });
  if (input?.is_list) columns.push({ colspan: input.path_kind ? 3 : 4 });
  return columns;
};

interface RoutingConditionsEditorProps {
  conditions: RoutingCondition[];
  onChange: (conditions: RoutingCondition[]) => void;
  /** The document classes of the profile being edited. */
  classes: unknown;
  /** The policy classes of the profile being edited. */
  policyClasses: unknown;
  /** Show what is wrong with each condition (after a save was attempted). */
  showProblems?: boolean;
  /** Show the conditions and offer no change. */
  readOnly?: boolean;
}

const RoutingConditionsEditor = ({
  conditions,
  onChange,
  classes,
  policyClasses,
  showProblems = false,
  readOnly = false,
}: RoutingConditionsEditorProps): React.JSX.Element => {
  const intl = useIntl();
  // The menus of the conditions, by position, and the Add button: where the focus goes
  // once a move or a removal is rendered.
  const menus = useRef<(ButtonDropdownProps.Ref | null)[]>([]);
  const addButton = useRef<ButtonProps.Ref>(null);

  // One key per condition, which follows it when conditions are moved or removed: its
  // box is rendered under it, and the state of its value's controls is kept under it.
  // A list replaced from outside (another number of conditions) gets new keys, and so
  // no state.
  const lastKey = useRef(0);
  const newKeys = (count: number): number[] =>
    Array.from({ length: count }, () => {
      lastKey.current += 1;
      return lastKey.current;
    });
  const [storedKeys, setStoredKeys] = useState(() => newKeys(conditions.length));
  let keys = storedKeys;
  if (keys.length !== conditions.length) {
    keys = newKeys(conditions.length);
    setStoredKeys(keys);
  }

  const [valueStates, setValueStates] = useState<Record<number, ValueState>>({});

  // The condition whose menu takes the focus once a move or a removal is rendered.
  const [focusRow, setFocusRow] = useState<number | null>(null);
  useEffect(() => {
    if (focusRow === null) return;
    if (conditions.length === 0) addButton.current?.focus();
    else menus.current[Math.min(focusRow, conditions.length - 1)]?.focus();
    setFocusRow(null);
  }, [focusRow, conditions.length]);

  const change = (nextConditions: RoutingCondition[], nextKeys: number[]): void => {
    setStoredKeys(nextKeys);
    onChange(nextConditions);
  };
  const update = (index: number, changed: Partial<RoutingCondition>): void =>
    onChange(conditions.map((condition, position) => (position === index ? { ...condition, ...changed } : condition)));
  const add = (): void =>
    change([...conditions, { input: '', path: '', aggregate: DEFAULT_AGGREGATE, test: '' }], [...keys, ...newKeys(1)]);
  const duplicate = (index: number): void =>
    change(
      [...conditions.slice(0, index + 1), { ...conditions[index] }, ...conditions.slice(index + 1)],
      [...keys.slice(0, index + 1), ...newKeys(1), ...keys.slice(index + 1)],
    );
  const move = (from: number, to: number): void => {
    change(arrayMove(conditions, from, to), arrayMove(keys, from, to));
    setFocusRow(to);
  };
  const remove = (index: number): void => {
    change(
      conditions.filter((_condition, position) => position !== index),
      keys.filter((_key, position) => position !== index),
    );
    setFocusRow(index);
  };

  const inputLabel = (name: string): string => {
    const message = inputMessage(name);
    return message ? intl.formatMessage(message) : name;
  };
  const inputOptions: SelectProps.Option[] = ROUTING_INPUTS.map((input) => ({ value: input.name, label: inputLabel(input.name) }));
  const profileClasses = classNames(classes);

  const pathOptions = (input: RoutingInput, current: string): SelectProps.Options => {
    let options: SelectProps.Options = [];
    if (input.path_kind === 'field') {
      options = fieldPathsByClass(classes).map((entry) => ({
        label: entry.className,
        options: entry.paths.map((path) => ({ value: path, label: path })),
      }));
    } else if (input.path_kind === 'policy_rule') {
      options = policyRuleIds(policyClasses).map((id) => ({ value: id, label: id }));
    } else if (input.path_kind === 'policy_type') {
      options = policyTypes(policyClasses).map((policyType) => ({ value: policyType, label: policyType }));
    } else if (input.path_kind === 'class') {
      options = profileClasses.map((name) => ({ value: name, label: name }));
    }
    const known = options.flatMap((option) => ('options' in option ? option.options : [option])).some((option) => option.value === current);
    return [
      ...(input.path_optional ? [{ value: '', label: intl.formatMessage(messages.pathAll) }] : []),
      // A path written by hand, or imported, that the profile does not offer stays choosable.
      ...(current && !known ? [{ value: current, label: current }] : []),
      ...options,
    ];
  };

  const selected = (options: SelectProps.Options, value: string): SelectProps.Option | null =>
    options.flatMap((option) => ('options' in option ? option.options : [option])).find((option) => option.value === value) ?? null;

  const problem = (condition: RoutingCondition, part: keyof ConditionProblems): string | undefined => {
    const found = showProblems ? conditionProblems(condition)[part] : undefined;
    return found ? intl.formatMessage(found) : undefined;
  };

  /** The values a choice on this input offers; null when they are typed. */
  const valueOptions = (input: RoutingInput): readonly string[] | null => {
    if (input.choices.length > 0) return input.choices;
    return CLASS_VALUE_INPUTS.includes(input.name) ? profileClasses : null;
  };

  const atLimit = conditions.length >= MAX_CONDITIONS;
  const aggregateOptions: SelectProps.Option[] = AGGREGATES.map((aggregate) => {
    const message = aggregateMessage(aggregate);
    return { value: aggregate, label: message ? intl.formatMessage(message) : aggregate };
  });

  return (
    <SpaceBetween size="m">
      {conditions.length === 0 && (
        <Box color="text-body-secondary" data-testid="routing-conditions-empty">
          {intl.formatMessage(messages.empty)}
        </Box>
      )}
      {conditions.map((condition, index) => {
        const position = index + 1;
        const input = routingInput(condition.input);
        const paths = input?.path_kind ? pathOptions(input, condition.path) : [];
        const kind = input
          ? guidedKind(input, condition.aggregate, input.type === 'field' ? fieldType(classes, condition.path) : undefined)
          : null;
        // The state held for another input, or another kind of control, is not this value's.
        const scope = input ? `${input.name}:${kind}` : '';
        const held = valueStates[keys[index]];
        return (
          <Container
            key={keys[index]}
            data-testid="routing-condition"
            header={
              // The number at the left, the menu at the top right.
              <Grid gridDefinition={[{ colspan: 9 }, { colspan: 3 }]} disableGutters>
                <Box variant="h4" padding={{ top: 'xxs' }}>
                  {intl.formatMessage(messages.title, { position })}
                </Box>
                {!readOnly && (
                  <Box textAlign="right">
                    <ButtonDropdown
                      ref={(menu) => {
                        menus.current[index] = menu;
                      }}
                      variant="icon"
                      expandToViewport
                      ariaLabel={intl.formatMessage(messages.actionsAria, { position })}
                      items={[
                        { id: 'duplicate', text: intl.formatMessage(messages.duplicate), disabled: atLimit },
                        { id: 'up', text: intl.formatMessage(messages.moveUp), disabled: index === 0 },
                        { id: 'down', text: intl.formatMessage(messages.moveDown), disabled: index === conditions.length - 1 },
                        { id: 'remove', text: intl.formatMessage(messages.remove) },
                      ]}
                      onItemClick={({ detail }) => {
                        if (detail.id === 'duplicate') duplicate(index);
                        if (detail.id === 'up') move(index, index - 1);
                        if (detail.id === 'down') move(index, index + 1);
                        if (detail.id === 'remove') remove(index);
                      }}
                    />
                  </Box>
                )}
              </Grid>
            }
          >
            <SpaceBetween size="s">
              {/* What is tested. The input keeps its width in every condition, and the
                  fields after it follow with no gap: what the input reads is wider when
                  no Match follows it, so a long policy rule id is shown whole. */}
              <div data-testid="routing-condition-input">
                <Grid gridDefinition={testGrid(input)}>
                  <FormField label={intl.formatMessage(messages.inputColumn)} errorText={problem(condition, 'input')} stretch>
                    <Select
                      selectedOption={selected(inputOptions, condition.input)}
                      options={inputOptions}
                      readOnly={readOnly}
                      placeholder={intl.formatMessage(messages.inputPlaceholder)}
                      ariaLabel={intl.formatMessage(messages.inputAria, { position })}
                      onChange={({ detail }) =>
                        update(index, { input: detail.selectedOption.value ?? '', path: '', aggregate: DEFAULT_AGGREGATE })
                      }
                    />
                  </FormField>
                  {/* Booleans, so that a part an input does not use takes no place in the grid. */}
                  {!!input?.path_kind && (
                    <FormField label={intl.formatMessage(PATH_LABELS[input.path_kind])} errorText={problem(condition, 'path')} stretch>
                      <Select
                        selectedOption={selected(paths, condition.path)}
                        options={paths}
                        readOnly={readOnly}
                        placeholder={intl.formatMessage(messages.pathPlaceholder)}
                        empty={intl.formatMessage(messages.pathEmpty)}
                        filteringType="auto"
                        ariaLabel={intl.formatMessage(messages.pathAria, { position })}
                        onChange={({ detail }) => update(index, { path: detail.selectedOption.value ?? '' })}
                      />
                    </FormField>
                  )}
                  {input?.is_list && (
                    <FormField label={intl.formatMessage(messages.aggregateColumn)} stretch>
                      <Select
                        selectedOption={selected(aggregateOptions, condition.aggregate || DEFAULT_AGGREGATE)}
                        options={aggregateOptions}
                        readOnly={readOnly}
                        ariaLabel={intl.formatMessage(messages.aggregateAria, { position })}
                        onChange={({ detail }) => update(index, { aggregate: detail.selectedOption.value ?? DEFAULT_AGGREGATE })}
                      />
                    </FormField>
                  )}
                </Grid>
              </div>
              {/* The value the input is compared with. */}
              <div data-testid="routing-condition-value">
                {input ? (
                  <ConditionValue
                    test={condition.test}
                    position={position}
                    kind={kind}
                    options={valueOptions(input)}
                    presence={input.path_kind === 'class'}
                    examples={TEST_EXAMPLES[input.type]}
                    readOnly={readOnly}
                    errorText={problem(condition, 'test')}
                    state={held?.scope === scope ? held : valueStateOf(scope, condition.test, kind)}
                    onChange={(state, test) => {
                      setValueStates((states) => ({ ...states, [keys[index]]: state }));
                      if (test !== undefined) update(index, { test });
                    }}
                  />
                ) : (
                  <Box color="text-body-secondary">{intl.formatMessage(messages.valueNoInput)}</Box>
                )}
              </div>
            </SpaceBetween>
          </Container>
        );
      })}
      {!readOnly && (
        <SpaceBetween size="xxs">
          <Button ref={addButton} disabled={atLimit} onClick={add} data-testid="routing-condition-add">
            {intl.formatMessage(messages.add)}
          </Button>
          {atLimit && (
            <Box variant="small" color="text-body-secondary" data-testid="routing-conditions-limit">
              {intl.formatMessage(messages.limit, { max: MAX_CONDITIONS })}
            </Box>
          )}
        </SpaceBetween>
      )}
    </SpaceBetween>
  );
};

export default RoutingConditionsEditor;
