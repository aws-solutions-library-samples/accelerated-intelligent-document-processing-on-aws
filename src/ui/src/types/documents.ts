// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0

/**
 * UI-facing document/section shapes.
 *
 * These are DERIVED from the generated GraphQL types in `graphql/generated/`
 * rather than hand-written copies of them (issue #711). Before that, adding a
 * field to `nested/api-resolvers/src/api/schema.graphql` and regenerating
 * changed nothing about whether the UI could read it — the hand-written
 * interfaces here silently lacked the field, and nothing failed. `Excluded` /
 * `ExclusionReason` had already drifted out of `Section` that way, and
 * `InstanceCount` had to be hand-wired into two separate Section shapes.
 *
 * The derivation keeps three properties:
 *  1. A new schema field is readable in the UI as soon as `npm run codegen` runs.
 *  2. A field the UI narrows (see `SectionOverrides`) that the schema renames or
 *     drops is a compile error, not a runtime blank.
 *  3. `SECTION_FIELDS` is an exhaustive runtime mirror of the schema's `Section`
 *     fields, which `graphql/__tests__/section-field-drift.test.ts` uses to
 *     assert every `Sections { … }` selection set actually requests them — the
 *     one link in the chain no TypeScript type can check.
 */

import type {
  ConfidenceThresholdAlert as GqlConfidenceThresholdAlert,
  Document as GqlDocument,
  Page as GqlPage,
  ProcessingIssue as GqlProcessingIssue,
  Section as GqlSection,
} from '../graphql/generated/schema-types';

/**
 * Strip codegen's `Maybe<>` from every field of a generated object type and make
 * each field required. The UI normalizes nulls to defaults in
 * `map-document-attributes.ts`, so its shapes are non-nullable.
 */
type NonNullFields<T> = { [K in keyof T]-?: NonNullable<T[K]> };

/** Same, but every field stays optional (absent rather than explicitly null). */
type OptionalNonNullFields<T> = { [K in keyof T]?: NonNullable<T[K]> };

/**
 * Compile error unless `Sub` is assignable to `Super`. Used below to pin the
 * hand-written override lists to the generated schema so they cannot name fields
 * the schema no longer has, and to assert the derived shapes cover every
 * generated field.
 */
type AssertAssignable<Sub extends Super, Super> = Sub;

export type ConfidenceThresholdAlert = NonNullFields<GqlConfidenceThresholdAlert>;

export type ProcessingIssue = OptionalNonNullFields<GqlProcessingIssue>;

/**
 * The `Section` fields the UI narrows from nullable to a non-null default.
 * Everything else — `Excluded`, `ExclusionReason`, `InstanceCount`, and anything
 * added to the schema next — flows straight through from the generated type.
 */
type SectionOverrides = {
  Id: string;
  PageIds: number[];
  Class: string;
  OutputJSONUri: string;
  ConfidenceThresholdAlerts: ConfidenceThresholdAlert[];
  ProcessingIssues?: ProcessingIssue[] | null;
};

/** UI-facing Section — the generated GraphQL `Section` with the UI's nullability. */
export type Section = Omit<GqlSection, keyof SectionOverrides> & SectionOverrides;

/** Every overridden key must still exist on the generated `Section`. */
type _SectionOverridesExistInSchema = AssertAssignable<keyof SectionOverrides, keyof GqlSection>;
/** ...and the derived `Section` must cover every generated field. */
type _SectionCoversSchema = AssertAssignable<keyof GqlSection, keyof Section>;

/**
 * A Section as held in the sections table's local edit state (`SectionsPanel`).
 * Every wire field is present — so a new schema field is readable there too —
 * plus the UI-only edit-tracking fields. The panel builds brand-new sections
 * client-side before processing, so the backend-populated fields are optional.
 */
export type EditableSection = Omit<Section, 'OutputJSONUri' | 'ConfidenceThresholdAlerts'> & {
  OutputJSONUri?: string;
  ConfidenceThresholdAlerts?: ConfidenceThresholdAlert[];
  /** Section Id before an in-place rename in the review UI. */
  OriginalId?: string | null;
  isModified?: boolean;
  isNew?: boolean;
};

/** `EditableSection` must also cover every generated `Section` field. */
type _EditableSectionCoversSchema = AssertAssignable<keyof GqlSection, keyof EditableSection>;

/**
 * Every field of the generated GraphQL `Section`, as a runtime value.
 *
 * `Record<keyof GqlSection, true>` is exhaustive: adding a field to
 * `schema.graphql` and regenerating turns "the UI silently ignores it" into a
 * TypeScript error right here, and the drift test turns it into a failing test.
 */
export const SECTION_FIELDS: Record<keyof GqlSection, true> = {
  Class: true,
  Confidence: true,
  ConfidenceThresholdAlerts: true,
  Excluded: true,
  ExclusionReason: true,
  Id: true,
  InstanceCount: true,
  OutputJSONUri: true,
  PageIds: true,
  ProcessingIssues: true,
};

/** Field names of the generated GraphQL `Section`, for the drift test. */
export const SECTION_FIELD_NAMES = Object.keys(SECTION_FIELDS) as (keyof GqlSection)[];

/** The `Page` fields the UI narrows from nullable to a non-null default. */
type PageOverrides = {
  Id: number;
  Class: string;
  ImageUri: string;
  TextUri: string;
  TextConfidenceUri: string;
};

/** UI-facing Page — derived from the generated GraphQL `Page`. */
export type Page = Omit<GqlPage, keyof PageOverrides> & PageOverrides;

type _PageOverridesExistInSchema = AssertAssignable<keyof PageOverrides, keyof GqlPage>;
type _PageCoversSchema = AssertAssignable<keyof GqlPage, keyof Page>;

/**
 * The `Document` fields the UI narrows from nullable to a non-null default (or,
 * for `PK`/`SK`, widens because list rows are assembled client-side).
 */
type DocumentOverrides = {
  ObjectKey: string;
  ObjectStatus: string;
  InitialEventTime: string;
  QueuedTime: string;
  WorkflowStartTime: string;
  CompletionTime: string;
  WorkflowExecutionArn: string;
  WorkflowStatus: string;
  PageCount: number;
  Sections: Section[];
  Pages: Page[];
  Metering: string;
  EvaluationReportUri: string;
  EvaluationStatus: string;
  SummaryReportUri: string;
  RuleValidationResultUri: string;
  ExpiresAfter: number;
  HITLStatus: string;
  HITLTriggered: boolean;
  HITLCompleted: boolean;
  HITLReviewURL: string;
  HITLSectionsPending: string[];
  HITLSectionsCompleted: string[];
  HITLSectionsSkipped: string[];
  HITLReviewOwner: string;
  HITLReviewOwnerEmail: string;
  HITLReviewedBy: string;
  HITLReviewedByEmail: string;
  HITLReviewHistory: string;
  ProcessingIssueCount?: number;
  PK?: string;
  SK?: string;
};

type _DocumentOverridesExistInSchema = AssertAssignable<keyof DocumentOverrides, keyof GqlDocument>;

/** Fields the UI computes locally — they have no schema counterpart. */
type DocumentUiComputed = {
  ListPK?: string;
  ListSK?: string;
  DocumentClass?: string;
  pageCount?: number;
  // UI-computed fields from map-document-attributes.ts
  uniqueId?: string;
  hitlTriggered?: boolean;
  hitlCompleted?: boolean;
  duration?: string;
  metering?: Record<string, unknown>;
  hitlReviewHistory?: Record<string, unknown>[];
  confidenceAlertCount?: number;
  processingIssueCount?: number;
  executionArn?: string;
};

/**
 * UI-facing Document — the generated GraphQL `Document` with the UI's
 * nullability, plus the locally computed fields.
 */
export type Document = Omit<GqlDocument, keyof DocumentOverrides> & DocumentOverrides & DocumentUiComputed;

/** The derived `Document` must cover every generated field. */
type _DocumentCoversSchema = AssertAssignable<keyof GqlDocument, keyof Document>;
